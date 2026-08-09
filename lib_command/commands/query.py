"""
Query commands - Read-only access to engine/workspace state.

These commands read from engine/workspace directly (acceptable per Phase A rules:
"Command/query handlers may read engine internals.")

Phase C additions: query.workspace_summary, query.workspace_snapshot,
                    query.view_detail, query.cube_detail
Produce TypedDict DTOs from lib_openm.dto.workspace — no engine objects cross
the boundary.

Phase D additions: query.cell_detail, query.cell_range, query.addr_resolve
Produce TypedDict DTOs from lib_openm.dto.cell — no engine objects cross the boundary.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import time
from typing import Any

from lib_openm.rule_eval.utils import CellError

logger = logging.getLogger(__name__)

DEBUG_ENGINE = False

from lib_contracts.errors import SnapshotInvariantError
from lib_openm import config as _om_config
from lib_openm.dto.cell import (
    CellAddressDTO,
    CellDTO,
    CellExplainDTO,
    CellKind,
    CellPrimitive,
    CellRangeDTO,
)
from lib_openm.dto.workspace import (
    CellFormatDTO,
    ViewLayoutDTO,
    ViewSnapshotDTO,
    CubeSnapshotDTO,
    DimensionSnapshotDTO,
    WorkspaceSummaryDTO,
    WorkspaceSnapshotDTO,
)
from lib_openm.model import view_layout_from_legacy
from lib_command.dto.timeline import TimelineSnapshotDTO
from .udf_commands import query_udf_list, query_udf_detail
from .profiler_register import query_profiler_list
from ..queries.solver_queries import (
    query_solver_status,
    query_solver_result,
    query_solver_backend_list,
    query_solver_algorithm_list,
    query_solver_job_list,
)
from .grid_helpers import (
    make_viewport_cell_key,
    parse_viewport_cell_key,
    resolve_addr,
    cell_format_to_dict,
    validate_channels,
    CHANNEL_ALLOWLIST,
    CellFormatDict,
)


def _cmd_query(
    ctx,
    type: str,
    **kwargs,
) -> dict:
    """
    Query the current state.

    Args:
        type: query type identifier

    Extra keyword arguments (Phase C):
        view_id: for view_detail query
        cube_id: for cube_detail query
    """
    engine = ctx.engine
    ws = None
    if engine is not None:
        try:
            ws = engine.workspace
        except (OSError, ConnectionError, AttributeError):
            ws = None
    if ws is None:
        ws = ctx.workspace

    if not engine or not ws:
        raise ValueError("No engine or workspace available")

    # ---- Phase C query handlers ----

    if type == "workspace_summary":
        return cmd_workspace_summary(ctx, ws)

    if type == "workspace_snapshot":
        return cmd_workspace_snapshot(ctx, engine, ws)

    if type == "view_detail":
        view_id = kwargs.get("view_id")
        if not view_id:
            raise ValueError("view_detail query requires view_id parameter")
        return cmd_view_detail(engine, view_id)

    if type == "cube_detail":
        cube_id = kwargs.get("cube_id")
        if not cube_id:
            raise ValueError("cube_detail query requires cube_id parameter")
        return cmd_cube_detail(engine, cube_id)

    if type == "cube_detach_impact":
        cube_id = kwargs.get("cube_id")
        dim_id = kwargs.get("dim_id")
        if not cube_id or not dim_id:
            raise ValueError("cube_detach_impact query requires cube_id and dim_id parameters")
        return engine.analyze_detach_dimension_from_cube(cube_id, dim_id)

    if type == "dimension_item_deletion_impact":
        dim_id = kwargs.get("dim_id")
        item_ids = kwargs.get("item_ids")
        if not dim_id or item_ids is None:
            raise ValueError("dimension_item_deletion_impact query requires dim_id and item_ids parameters")
        return engine.analyze_dimension_item_deletion(dim_id, item_ids)

    if type == "dimension_deletion_impact":
        dim_id = kwargs.get("dim_id")
        item_ids = kwargs.get("item_ids")
        if not dim_id or item_ids is None:
            raise ValueError("dimension_deletion_impact query requires dim_id and item_ids parameters")
        return engine.analyze_dimension_deletion_impact(dim_id, item_ids)

    if type == "view_state":
        view_id = kwargs.get("view_id")
        if not view_id:
            raise ValueError("view_state query requires view_id parameter")
        session_id = getattr(ctx, "session_id", None)
        if session_id:
            from lib_command.core.session_store import get_session_store
            vs = get_session_store().get_view_state(session_id)
            if vs is not None:
                return {
                    "type": "view_state",
                    "view_id": view_id,
                    "selection_mode": vs.selection_mode,
                    "selected_indices": list(vs.selected_indices),
                    "anchor_cell": vs.anchor_cell,
                    "active_cell": vs.active_cell,
                    "scroll_pos": vs.scroll_pos,
                }
        return {
            "type": "view_state",
            "view_id": view_id,
            "selection_mode": "cell",
            "selected_indices": [],
            "anchor_cell": None,
            "active_cell": None,
            "scroll_pos": None,
        }

    if type == "cell_viewport_range":
        view_id = kwargs.get("view_id")
        top = kwargs.get("top")
        left = kwargs.get("left")
        bottom = kwargs.get("bottom")
        right = kwargs.get("right")
        if not all(v is not None for v in [view_id, top, left, bottom, right]):
            raise ValueError("cell_viewport_range query requires view_id, top, left, bottom, right parameters")
        return engine.get_range(view_id, top, left, bottom, right)

    if type == "dimension_detail":
        dim_id = kwargs.get("dim_id")
        if not dim_id:
            raise ValueError("dimension_detail query requires dim_id parameter")
        return cmd_dimension_detail(engine, dim_id)

    if type == "dimension_effective_order":
        dim_id = kwargs.get("dim_id")
        if not dim_id:
            raise ValueError("dimension_effective_order query requires dim_id parameter")
        return {
            "type": "dimension_effective_order",
            "dim_id": dim_id,
            "item_ids": engine.dimension_effective_order(dim_id),
        }

    if type == "dimension_effective_order_window":
        dim_id = kwargs.get("dim_id")
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit")
        if not dim_id:
            raise ValueError("dimension_effective_order_window query requires dim_id parameter")
        return {
            "type": "dimension_effective_order_window",
            "dim_id": dim_id,
            "item_ids": engine.dimension_effective_order_window(dim_id, offset=offset, limit=limit),
        }

    # ---- Phase D cell query handlers ----

    if type == "cell_detail":
        view_id = kwargs.get("view_id")
        row_key = kwargs.get("row_key")
        col_key = kwargs.get("col_key")
        if not all(v is not None for v in [view_id, row_key, col_key]):
            raise ValueError("cell_detail query requires view_id, row_key, col_key parameters")
        return cmd_cell_detail(engine, view_id, row_key, col_key)

    if type == "cell_range":
        view_id = kwargs.get("view_id")
        row_keys = kwargs.get("row_keys")
        col_keys = kwargs.get("col_keys")
        if not all(v is not None for v in [view_id, row_keys, col_keys]):
            raise ValueError("cell_range query requires view_id, row_keys, col_keys parameters")
        return cmd_cell_range(engine, view_id, row_keys, col_keys)

    if type == "addr_resolve":
        view_id = kwargs.get("view_id")
        row_key = kwargs.get("row_key")
        col_key = kwargs.get("col_key")
        if not all(v is not None for v in [view_id, row_key, col_key]):
            raise ValueError("addr_resolve query requires view_id, row_key, col_key parameters")
        return cmd_addr_resolve(engine, view_id, row_key, col_key)

    # ---- Phase E grid/view shape query handlers ----

    if type == "view_row_keys":
        view_id = kwargs.get("view_id")
        if not view_id:
            raise ValueError("view_row_keys query requires view_id parameter")
        return cmd_view_row_keys(engine, view_id)

    if type == "view_col_keys":
        view_id = kwargs.get("view_id")
        if not view_id:
            raise ValueError("view_col_keys query requires view_id parameter")
        return cmd_view_col_keys(engine, view_id)

    if type == "view_row_header":
        view_id = kwargs.get("view_id")
        section = kwargs.get("section")
        if view_id is None or section is None:
            raise ValueError("view_row_header query requires view_id and section parameters")
        return cmd_view_row_header(engine, view_id, section)

    if type == "view_col_header":
        view_id = kwargs.get("view_id")
        section = kwargs.get("section")
        if view_id is None or section is None:
            raise ValueError("view_col_header query requires view_id and section parameters")
        return cmd_view_col_header(engine, view_id, section)

    # ---- Phase F outline query handlers ----

    if type == "outline_tree":
        view_id = kwargs.get("view_id")
        axis = kwargs.get("axis")
        if not view_id or axis not in ("row", "col"):
            raise ValueError("outline_tree query requires view_id and axis ('row' or 'col') parameters")
        return cmd_outline_tree(engine, view_id, axis)

    if type == "page_selection":
        view_id = kwargs.get("view_id")
        dim_id = kwargs.get("dim_id")
        if not view_id or not dim_id:
            raise ValueError("page_selection query requires view_id and dim_id parameters")
        return cmd_page_selection(engine, view_id, dim_id)

    # ---- Phase G rule query handlers ----

    if type == "cell_rule":
        cube_id = kwargs.get("cube_id")
        addr = kwargs.get("addr")
        if not cube_id or addr is None:
            raise ValueError("cell_rule query requires cube_id and addr parameters")
        return cmd_cell_rule(engine, cube_id, addr)

    if type == "rule_detail":
        cube_id = kwargs.get("cube_id")
        addr = kwargs.get("addr")
        if not cube_id or addr is None:
            raise ValueError("rule_detail query requires cube_id and addr parameters")
        return cmd_rule_detail(engine, cube_id, addr)

    if type == "cube_rule_counts":
        cube_id = kwargs.get("cube_id")
        if not cube_id:
            raise ValueError("cube_rule_counts query requires cube_id parameter")
        return cmd_cube_rule_counts(engine, cube_id)

    if type == "rule_target_resolve":
        cube_id = kwargs.get("cube_id")
        lhs = kwargs.get("lhs")
        if not cube_id or not lhs:
            raise ValueError("rule_target_resolve query requires cube_id and lhs parameters")
        return cmd_rule_target_resolve(engine, cube_id, lhs)

    if type == "workspace_rules":
        return cmd_workspace_rules(engine, ws)

    if type == "cell_value_by_ref":
        cube_name = kwargs.get("cube_name")
        channel = kwargs.get("channel")
        selectors = kwargs.get("selectors")
        if not cube_name or channel is None or selectors is None:
            raise ValueError(
                "cell_value_by_ref query requires cube_name, channel, and selectors"
            )
        return cmd_cell_value_by_ref(engine, cube_name, channel, selectors)

    # ---- Phase F2 diagnostic query handlers ----

    if type == "diagnostics_calculation_flow":
        cube_id = kwargs.get("cube_id")
        addr = kwargs.get("addr")
        max_depth = kwargs.get("max_depth")
        max_precedents = kwargs.get("max_precedents")
        if not cube_id or addr is None:
            raise ValueError("diagnostics_calculation_flow query requires cube_id and addr parameters")
        return engine.trace_calculation_flow(cube_id, tuple(addr), max_depth=max_depth, max_precedents_per_node=max_precedents)

    if type == "diagnostics_circular_references":
        cube_id = kwargs.get("cube_id")
        addr = kwargs.get("addr")
        max_depth = kwargs.get("max_depth")
        max_precedents = kwargs.get("max_precedents")
        max_cycles = kwargs.get("max_cycles")
        if not cube_id or addr is None:
            raise ValueError("diagnostics_circular_references query requires cube_id and addr parameters")
        return engine.trace_circular_references(cube_id, tuple(addr), max_depth=max_depth, max_precedents_per_node=max_precedents, max_cycles=max_cycles)

    if type == "diagnostics_dependency_tracking_state":
        return {"dependency_tracking_enabled": bool(getattr(engine, "_dep_tracking_enabled", False))}

    if type == "diagnostics_engine_backend":
        return engine.engine_info()

    if type == "diagnostics_dependency_metrics":
        return engine.dependency_metrics()

    if type == "diagnostics_rule_eval_profile":
        top_n = kwargs.get("top_n", 10)
        return engine.rule_eval_profile_snapshot(top_n=top_n)

    if type == "diagnostics_multithread_config":
        return engine.multithread_recompute_config()

    if type == "diagnostics_dirty_count":
        if kwargs.get("include_keys"):
            dirty_keys = engine.dirty_keys()
            return {"dirty_count": len(dirty_keys), "dirty_keys": dirty_keys}
        return {"dirty_count": engine.dirty_count()}

    # ---- Phase F5b grid snapshot query handlers ----

    if type == "grid_viewport_snapshot":
        view_id = kwargs.get("view_id")
        row_keys = kwargs.get("row_keys")
        col_keys = kwargs.get("col_keys")
        page_selections = kwargs.get("page_selections")
        channels = kwargs.get("channels")
        if not all(v is not None for v in [view_id, row_keys, col_keys, page_selections]):
            raise ValueError(
                "grid_viewport_snapshot query requires view_id, row_keys, col_keys, page_selections"
            )
        return cmd_grid_viewport_snapshot(
            engine,
            view_id,
            row_keys,
            col_keys,
            page_selections,
            channels,
            profiler=getattr(ctx, "profiler", None),
            allow_evaluation=bool(kwargs.get("allow_evaluation", False)),
        )

    if type == "grid_viewport_snapshot_batch":
        view_id = kwargs.get("view_id")
        tiles = kwargs.get("tiles")
        page_selections = kwargs.get("page_selections")
        channels = kwargs.get("channels")
        generation = kwargs.get("generation")
        if not all(v is not None for v in [view_id, tiles, page_selections]):
            raise ValueError(
                "grid_viewport_snapshot_batch query requires view_id, tiles, page_selections"
            )
        return cmd_grid_viewport_snapshot_batch(
            engine,
            view_id,
            tiles,
            page_selections,
            channels,
            generation=generation,
            profiler=getattr(ctx, "profiler", None),
            allow_evaluation=bool(kwargs.get("allow_evaluation", False)),
        )

    if type == "selection_stats":
        view_id = kwargs.get("view_id")
        mode = kwargs.get("mode")
        page_selections = kwargs.get("page_selections")
        if not all(v is not None for v in [view_id, mode, page_selections]):
            raise ValueError(
                "selection_stats query requires view_id, mode, page_selections"
            )
        return cmd_selection_stats(
            engine,
            view_id,
            mode,
            page_selections,
            cell_keys=kwargs.get("cell_keys"),
            row_keys=kwargs.get("row_keys"),
            col_keys=kwargs.get("col_keys"),
        )

    if type == "cell_channel_values":
        view_id = kwargs.get("view_id")
        row_key = kwargs.get("row_key")
        col_key = kwargs.get("col_key")
        page_selections = kwargs.get("page_selections")
        channels = kwargs.get("channels")
        if not all(v is not None for v in [view_id, row_key, col_key, page_selections, channels]):
            raise ValueError(
                "cell_channel_values query requires view_id, row_key, col_key, page_selections, channels"
            )
        return cmd_cell_channel_values(
            engine, view_id, row_key, col_key, page_selections, channels
        )

    # ---- Phase G2 undo/redo query handlers ----

    if type == "undo_state":
        return {
            "can_undo": engine.can_undo(),
            "can_redo": engine.can_redo(),
            "undo_description": engine.get_undo_description(),
            "redo_description": engine.get_redo_description(),
        }

    # ---- Session view-state query handlers (Phase 5A) ----

    if type == "selection_current":
        session_id = getattr(ctx, "session_id", None)
        if session_id:
            from lib_command.core.session_store import get_session_store
            vs = get_session_store().get_view_state(session_id)
            if vs is not None:
                return {
                    "type": "selection_current",
                    "session_id": vs.session_id,
                    "active_view_id": vs.active_view_id,
                    "cursor": (vs.cursor_row, vs.cursor_col),
                    "anchor": (vs.anchor_row, vs.anchor_col),
                    "mode": vs.selection_mode,
                    "ranges": [
                        (r.start_row, r.start_col, r.end_row, r.end_col)
                        for r in vs.selection_ranges
                    ],
                    "selected_indices": list(vs.selected_indices),
                    "page_selections": dict(vs.page_selections),
                    "scroll": (vs.scroll_x, vs.scroll_y),
                }
        return {
            "type": "selection_current",
            "session_id": session_id,
            "active_view_id": None,
            "cursor": (0, 0),
            "anchor": (0, 0),
            "mode": "cell",
            "ranges": [],
            "selected_indices": [],
            "page_selections": {},
            "scroll": (0, 0),
        }

    if type == "active_view_current":
        session_id = getattr(ctx, "session_id", None)
        if session_id:
            from lib_command.core.session_store import get_session_store
            vs = get_session_store().get_view_state(session_id)
            if vs is not None and vs.active_view_id:
                view_id = vs.active_view_id
                if view_id in ws.views:
                    view = ws.views[view_id]
                    return {
                        "type": "active_view_current",
                        "view_id": view_id,
                        "view_name": view.name if hasattr(view, "name") else view_id,
                    }
                return {"type": "active_view_current", "view_id": view_id}
        return {"type": "active_view_current", "view_id": None}

    # ---- Timeline query handlers ----

    if type == "timeline_snapshots":
        return cmd_timeline_snapshots(ctx)

    # ---- Legacy query handlers (deprecated) ----

    if type == "current_view":
        # Macro-language compatibility query. Reads per-session runtime state
        # from SessionStore; canonical callers should use query("active_view_current").
        session_id = getattr(ctx, "session_id", None)
        view_id = None
        if session_id:
            from lib_command.core.session_store import get_session_store
            vs = get_session_store().get_view_state(session_id)
            if vs is not None:
                view_id = vs.active_view_id
        if view_id and view_id in ws.views:
            view = ws.views[view_id]
            return {
                "type": "current_view",
                "view_id": view_id,
                "view_name": view.name if hasattr(view, 'name') else view_id
            }
        return {"type": "current_view", "view_id": None}

    if type == "view_list":
        views = []
        for view in engine.list_views():
            views.append({
                "id": view.id,
                "name": view.name if hasattr(view, 'name') else view.id
            })
        return {"type": "view_list", "views": views}

    if type == "current_cube":
        cube_id = ctx.variables.get('_current_cube')
        if cube_id and cube_id in ws.cubes:
            cube = ws.cubes[cube_id]
            return {
                "type": "current_cube",
                "cube_id": cube_id,
                "cube_name": cube.name if hasattr(cube, 'name') else cube_id
            }
        return {"type": "current_cube", "cube_id": None}

    if type == "cube_list":
        cubes = []
        for cid, cube in list(ws.cubes.items()):
            cubes.append({
                "id": cid,
                "name": cube.name if hasattr(cube, 'name') else cid,
                "dimensions": len(cube.dimension_ids) if hasattr(cube, 'dimension_ids') else 0
            })
        return {"type": "cube_list", "cubes": cubes}

    if type == "dimension_list":
        dims = []
        for did, dim in list(ws.dimensions.items()):
            dim_items = getattr(dim, 'items', [])
            dims.append({
                "id": did,
                "name": dim.name if hasattr(dim, 'name') else did,
                "items": len(dim_items),
                "item_list": [{"id": it.id, "name": it.name} for it in dim_items],
            })
        return {"type": "dimension_list", "dimensions": dims}

    # ---- UDF query handlers ----

    if type == "udf_list":
        return query_udf_list(ctx)

    if type == "udf_detail":
        name = kwargs.get("name")
        if not name:
            raise ValueError("udf_detail query requires name parameter")
        return query_udf_detail(ctx, name)

    if type == "profiler_list":
        return query_profiler_list(ctx)

    # ---- Solver query handlers ----

    if type == "solver_status":
        job_id = kwargs.get("job_id")
        if not job_id:
            raise ValueError("solver_status query requires job_id parameter")
        return query_solver_status(ctx, job_id=job_id)

    if type == "solver_result":
        job_id = kwargs.get("job_id")
        if not job_id:
            raise ValueError("solver_result query requires job_id parameter")
        return query_solver_result(ctx, job_id=job_id)

    if type == "solver_backend_list":
        return query_solver_backend_list(ctx, **kwargs)

    if type == "solver_job_list":
        return query_solver_job_list(ctx, **kwargs)

    if type == "solver_algorithm_list":
        backend_id = kwargs.pop("backend_id", "scipy")
        return query_solver_algorithm_list(ctx, backend_id=backend_id, **kwargs)

    raise ValueError(
        f"Unknown query type: {type}. "
        "Valid types: current_view, view_list, current_cube, cube_list, "
        "dimension_list, timeline_snapshots, workspace_summary, workspace_snapshot, "
        "view_detail, view_state, cube_detail, cube_detach_impact, "
        "dimension_item_deletion_impact, dimension_deletion_impact, "
        "dimension_detail, cell_detail, cell_range, cell_viewport_range, "
        "addr_resolve, view_row_keys, view_col_keys, view_row_header, view_col_header, "
        "outline_tree, page_selection, cell_rule, rule_detail, cube_rule_counts, "
        "rule_target_resolve, cell_value_by_ref, "
        "diagnostics_calculation_flow, diagnostics_circular_references, diagnostics_dependency_tracking_state, "
        "diagnostics_engine_backend, diagnostics_dependency_metrics, diagnostics_rule_eval_profile, diagnostics_multithread_config, diagnostics_dirty_count, "
        "grid_viewport_snapshot, cell_channel_values, selection_stats, workspace_rules, "
        "udf_list, udf_detail, profiler_list, "
        "solver_status, solver_result, solver_backend_list, solver_algorithm_list, solver_job_list"
    )


def cmd_query(
    ctx,
    type: str,
    **kwargs,
) -> dict:
    """Logged entry point for all runtime queries."""
    view_id = kwargs.get("view_id")
    label = view_id[:8] if isinstance(view_id, str) else str(view_id)
    if DEBUG_ENGINE:
        print(f"[QUERY-START] {type} view={label}", flush=True)
    t0 = time.perf_counter()
    try:
        return _cmd_query(ctx, type, **kwargs)
    finally:
        duration_ms = (time.perf_counter() - t0) * 1000
        if DEBUG_ENGINE:
            print(f"[QUERY-DONE] {type} view={label} duration={duration_ms:.1f} ms", flush=True)


# =============================================================================
# Timeline query handler implementation
# =============================================================================


def cmd_timeline_snapshots(ctx) -> list[TimelineSnapshotDTO]:
    """Return timeline snapshots for the current workspace.

    The result is a list of plain DTOs ordered oldest-first by `created_at`,
    then `snapshot_id` as a tie-breaker. If no timeline service is available
    (e.g. headless tests), an empty list is returned. In a GUI/runtime context
    this should be treated as a configuration error and logged.
    """
    import logging

    logger = logging.getLogger(__name__)

    timeline = getattr(ctx.services, "timeline", None)
    if timeline is None:
        logger.warning("Timeline service unavailable; returning empty snapshot list")
        return []

    snapshots = timeline.load_snapshots()
    dtos: list[TimelineSnapshotDTO] = []
    for snap in snapshots:
        dto: TimelineSnapshotDTO = {
            "snapshot_id": str(snap.snapshot_id),
            "parent_id": str(snap.parent_id) if snap.parent_id is not None else None,
            "description": snap.description,
            "branch_name": str(snap.branch_id) if snap.branch_id is not None else None,
            "created_at": snap.created_at.isoformat(),
            "snapshot_type": str(snap.snapshot_type.value),
            "is_delta": bool(snap.is_delta),
        }
        dtos.append(dto)

    dtos.sort(key=lambda d: (d["created_at"], d["snapshot_id"]))
    return dtos


# =============================================================================
# Phase D cell query handler implementations
# =============================================================================


def cmd_cell_detail(
    engine,
    view_id: str,
    row_key: tuple[str, ...],
    col_key: tuple[str, ...],
) -> dict:
    """Get single cell as CellDTO (returned as dict for TypedDict clarity)."""
    # print(f"[CELL-DETAIL-STEP] require_view view={view_id[:8]} row={row_key} col={col_key}", flush=True)
    view = engine.require_view_by_id(view_id)
    cube = engine.require_cube_by_id(view.cube_id)

    cell_ref = {"kind": "ids", "row_key": row_key, "col_key": col_key}
    # print(f"[CELL-DETAIL-STEP] get_cell_value view={view_id[:8]}", flush=True)
    cell = engine.get_cell_value(view_id, cell_ref)
    explain = cell.explain
    # print(f"[CELL-DETAIL-STEP] addr_for_view_ids view={view_id[:8]}", flush=True)
    addr = engine.addr_for_view_ids(view_id, row_key=row_key, col_key=col_key)
    value = cell.value
    # print(f"[CELL-DETAIL-STEP] classify value={value!r} source={getattr(explain, 'source', None)!r}", flush=True)
    kind, display_value = _classify_cell_value(value, explain)

    explain_dto: dict[str, Any] = {
        "source": getattr(explain, "source", "empty"),
        "rule_body": getattr(explain, "rule_body", None),
        "error": getattr(explain, "error", None),
        "depends": _serialize_depends(getattr(explain, "depends", None)),
    }

    return {
        "view_id": view_id,
        "cube_id": cube.id,
        "row_key": row_key,
        "col_key": col_key,
        "addr": addr,
        "value": _coerce_to_primitive(value),
        "display_value": display_value,
        "kind": kind,
        "explain": explain_dto,
    }


def cmd_cell_range(
    engine,
    view_id: str,
    row_keys: list[tuple[str, ...]],
    col_keys: list[tuple[str, ...]],
) -> dict:
    """Get rectangular range of cells as CellRangeDTO (returned as dict).

    NOTE: cell_range batches the GUI/query call boundary (one QueryService query).
    Engine-internal per-cell lookup optimization is deferred. The handler still loops:
        for rk in row_keys:
            for ck in col_keys:
                engine.get_cell_value(...)
    This is acceptable for Phase D — the query boundary optimization is the goal,
    not engine-internal optimization.
    """
    view = engine.require_view_by_id(view_id)
    cube = engine.require_cube_by_id(view.cube_id)

    cells: list[dict[str, Any]] = []
    for rk in row_keys:
        for ck in col_keys:
            cell_ref = {"kind": "ids", "row_key": rk, "col_key": ck}
            cell = engine.get_cell_value(view_id, cell_ref)
            explain = cell.explain
            addr = engine.addr_for_view_ids(view_id, row_key=rk, col_key=ck)
            value = cell.value
            kind, display_value = _classify_cell_value(value, explain)

            cells.append({
                "view_id": view_id,
                "cube_id": cube.id,
                "row_key": rk,
                "col_key": ck,
                "addr": addr,
                "value": _coerce_to_primitive(value),
                "display_value": display_value,
                "kind": kind,
                "explain": {
                    "source": getattr(explain, "source", "empty"),
                    "rule_body": getattr(explain, "rule_body", None),
                    "error": getattr(explain, "error", None),
                    "depends": _serialize_depends(getattr(explain, "depends", None)),
                },
            })

    return {
        "view_id": view_id,
        "cube_id": cube.id,
        "row_start": 0,
        "col_start": 0,
        # Empty ranges use row_end=-1, col_end=-1 by mathematical convention.
        "row_end": len(row_keys) - 1 if row_keys else -1,
        "col_end": len(col_keys) - 1 if col_keys else -1,
        "cells": cells,
        "row_keys": row_keys,
        "col_keys": col_keys,
    }


def cmd_addr_resolve(
    engine,
    view_id: str,
    row_key: tuple[str, ...],
    col_key: tuple[str, ...],
) -> dict:
    """Resolve view keys to full address. Returns CellAddressDTO as dict."""
    addr = engine.addr_for_view_ids(view_id, row_key=row_key, col_key=col_key)

    return {
        "view_id": view_id,
        "row_key": row_key,
        "col_key": col_key,
        "addr": addr,
    }


def _coerce_to_primitive(value: object) -> CellPrimitive:
    """Coerce engine cell value to CellPrimitive (str | int | float | bool | None).

    NaN and Infinity floats are coerced to None (not primitive).
    They are classified as kind="error" with display_value="#NUM!" by _classify_cell_value.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return None
    if isinstance(value, str):
        return value
    # Preserve CellError objects so the GUI can render them with error styling
    # rather than treating them as plain text.
    if hasattr(value, "code") and hasattr(value, "_VALID_CODES"):
        return str(value)
    return str(value)


def _classify_cell_value(value: object, explain: Any) -> tuple[CellKind, str]:
    """Return (kind, display_value) for a cell.

    Pure helper — no engine or framework imports.

    Handles special cases:
    - NaN/Infinity → kind="error", display_value="#NUM!"
    - Error explain source → kind="error", display_value from explain.error
    """
    if isinstance(value, CellError):
        return ("error", value.code)

    if getattr(explain, "source", "") == "error":
        error_msg = getattr(explain, "error", str(value)) or str(value)
        return ("error", error_msg)

    if isinstance(value, float) and not math.isfinite(value):
        return ("error", "#NUM!")

    if value is None or value == "":
        return ("empty", "")

    if isinstance(value, bool):
        return ("bool", str(value))
    if isinstance(value, int):
        return ("number", str(value))
    if isinstance(value, float):
        return ("number", str(value))
    if isinstance(value, str):
        return ("text", value)

    return ("text", str(value))


def _serialize_depends(
    depends: list | tuple | None,
) -> list[tuple[str, ...]] | None:
    """Convert dependency list to tuple[str, ...] for TypedDict."""
    if depends is None:
        return None
    return [
        tuple(d) if isinstance(d, (list, tuple)) else (str(d),)
        for d in depends
    ]


# =============================================================================
# Phase C query handler implementations
# =============================================================================


def cmd_workspace_summary(ctx, ws) -> WorkspaceSummaryDTO:
    """Get lightweight workspace state (IDs only, no DTOs)."""
    return WorkspaceSummaryDTO(
        saved_default_view_id=getattr(ws, 'saved_default_view_id', None),
        view_ids=list(getattr(ws, 'views_order', None) or ws.views.keys()),
        cube_ids=list(ws.cubes.keys()),
    )


def cmd_workspace_snapshot(ctx, engine, ws) -> WorkspaceSnapshotDTO:
    """Get full workspace state (IDs + view/cube DTOs) for bootstrap."""
    # Build view snapshots, preserving views_order from the workspace
    view_snapshots: dict[str, ViewSnapshotDTO] = {}
    ordered_view_ids = list(getattr(ws, 'views_order', None) or ws.views.keys())
    for view_id in ordered_view_ids:
        view = engine.require_view_by_id(view_id)
        layout = view_layout_from_legacy(view)
        view_snapshots[view_id] = ViewSnapshotDTO(
            id=view.id,
            cube_id=view.cube_id,
            row_dim_ids=list(view.row_dim_ids),
            col_dim_ids=list(view.col_dim_ids),
            page_dim_ids=list(view.page_dim_ids),
            layout=ViewLayoutDTO(
                rows=list(layout.rows),
                cols=list(layout.cols),
                page=list(layout.page),
            ),
            name=getattr(view, 'name', ''),
        )

    # Build cube snapshots
    cube_snapshots: dict[str, CubeSnapshotDTO] = {}
    for cube_id in list(ws.cubes.keys()):
        cube = engine.require_cube_by_id(cube_id)
        cube_snapshots[cube_id] = CubeSnapshotDTO(
            id=cube.id,
            dimension_ids=list(cube.dimension_ids),
            name=getattr(cube, 'name', ''),
            user_override_count=len(getattr(cube, 'user_override_addrs', set())),
        )

    # Build dimension snapshots
    dimension_snapshots: dict[str, DimensionSnapshotDTO] = {}
    for dim_id in list(ws.dimensions.keys()):
        dim = engine.require_dimension_by_id(dim_id)
        items = getattr(dim, 'items', [])
        fresh_outline: list = []
        try:
            fresh_outline = engine.dimension_outline_for_dim(dim_id) if hasattr(engine, 'dimension_outline_for_dim') else getattr(dim, 'outline', [])
        except Exception:
            pass
        dimension_snapshots[dim_id] = DimensionSnapshotDTO(
            id=dim.id,
            name=getattr(dim, 'name', ''),
            dim_type=getattr(dim, 'dim_type', 'set'),
            item_count=len(items),
            item_ids=[it.id for it in items],
            item_names=[it.name for it in items],
            items=[{"id": it.id, "name": it.name} for it in items],
            outline=_serialize_outline_nodes(fresh_outline),
        )

    # Use the snapshot dict keys as the source of truth for IDs.
    # Re-reading ws.views/cubes/dimensions keys would race with concurrent
    # in-place cache updates (e.g. create_cube adding to _cached_workspace.cubes).
    view_ids = list(view_snapshots.keys())
    cube_ids = list(cube_snapshots.keys())
    dimension_ids = list(dimension_snapshots.keys())

    return WorkspaceSnapshotDTO(
        id=ws.id,
        saved_default_view_id=getattr(ws, 'saved_default_view_id', None),
        view_ids=view_ids,
        cube_ids=cube_ids,
        dimension_ids=dimension_ids,
        view_snapshots=view_snapshots,
        cube_snapshots=cube_snapshots,
        dimension_snapshots=dimension_snapshots,
    )


def cmd_view_detail(engine, view_id: str) -> ViewSnapshotDTO:
    """Get single view snapshot as TypedDict."""
    view = engine.require_view_by_id(view_id)
    layout = view_layout_from_legacy(view)
    return ViewSnapshotDTO(
        id=view.id,
        cube_id=view.cube_id,
        row_dim_ids=list(view.row_dim_ids),
        col_dim_ids=list(view.col_dim_ids),
        page_dim_ids=list(view.page_dim_ids),
        layout=ViewLayoutDTO(
            rows=list(layout.rows),
            cols=list(layout.cols),
            page=list(layout.page),
        ),
        name=getattr(view, 'name', ''),
        item_formats={k: cell_format_to_dict(v) for k, v in getattr(view, 'item_formats', {}).items()},
        group_formats={k: cell_format_to_dict(v) for k, v in getattr(view, 'group_formats', {}).items()},
        cell_formats={k: cell_format_to_dict(v) for k, v in getattr(view, 'cell_formats', {}).items()},
        col_widths=dict(getattr(view, 'col_widths', {})),
        row_header_widths=dict(getattr(view, 'row_header_widths', {})),
    )


def cmd_cube_detail(engine, cube_id: str) -> CubeSnapshotDTO:
    """Get single cube snapshot as TypedDict."""
    cube = engine.require_cube_by_id(cube_id)
    return CubeSnapshotDTO(
        id=cube.id,
        dimension_ids=list(cube.dimension_ids),
        name=getattr(cube, 'name', ''),
        user_override_count=len(getattr(cube, 'user_override_addrs', set())),
    )


def _serialize_outline_nodes(nodes: list) -> list[dict]:
    """Serialize outline nodes to plain dict snapshots."""
    out: list[dict] = []
    for n in nodes:
        out.append({
            "label": getattr(n, "label", ""),
            "item_id": getattr(n, "item_id", None),
            "node_id": getattr(n, "node_id", None),
            "is_aggregate": getattr(n, "is_aggregate", False),
            "children": _serialize_outline_nodes(getattr(n, "children", [])),
        })
    return out


def cmd_dimension_detail(engine, dim_id: str) -> DimensionSnapshotDTO:
    """Get single dimension snapshot as TypedDict."""
    dim = engine.require_dimension_by_id(dim_id)
    items = getattr(dim, 'items', [])
    # Use engine.get_dimension_outline to get lazily-rebuilt outline.
    # Catch exceptions so outline RPC failures don't prevent the dimension
    # snapshot (with items) from being returned — page chips need items.
    try:
        fresh_outline = engine.dimension_outline_for_dim(dim_id) if hasattr(engine, 'dimension_outline_for_dim') else getattr(dim, 'outline', [])
    except Exception:
        fresh_outline = []
    return DimensionSnapshotDTO(
        id=dim.id,
        name=getattr(dim, 'name', ''),
        dim_type=getattr(dim, 'dim_type', 'set'),
        item_count=len(items),
        item_ids=[it.id for it in items],
        item_names=[it.name for it in items],
        items=[{"id": it.id, "name": it.name} for it in items],
        outline=_serialize_outline_nodes(fresh_outline),
    )


def cmd_page_selection(engine, view_id: str, dim_id: str) -> dict:
    """Return the currently selected page item ID for a dimension in a view.

    Returns {"type": "page_selection", "view_id": str, "dim_id": str, "item_id": str | None}.
    """
    item_id = engine.get_page_item_id(view_id, dim_id)
    return {"type": "page_selection", "view_id": view_id, "dim_id": dim_id, "item_id": item_id}


def cmd_outline_tree(engine, view_id: str, axis: str) -> dict:
    """Get outline tree for a view axis as plain dict snapshot."""
    view = engine.require_view_by_id(view_id)
    if axis == "row":
        nodes = getattr(view, "row_outline", None) or []
    elif axis == "col":
        nodes = getattr(view, "col_outline", None) or []
    else:
        return {"type": "outline_tree", "axis": axis, "nodes": []}
    return {"type": "outline_tree", "axis": axis, "nodes": _serialize_outline_nodes(list(nodes))}


# =============================================================================
# Phase G rule query handler implementations
# =============================================================================


def cmd_cell_rule(engine, cube_id: str, addr: tuple[str, ...]) -> dict:
    """Get exact-cell rule expression for a given cube and address.

    Returns {"type": "cell_rule", "cube_id": str, "expression": str | None}.
    """
    anchored = engine.find_anchored_rule(cube_id, addr)
    return {
        "type": "cell_rule",
        "cube_id": cube_id,
        "expression": anchored.expression if anchored else None,
    }


def cmd_rule_detail(engine, cube_id: str, addr: tuple[str, ...]) -> dict:
    """Get best matching rule expression for a given cube and address.

    Derives dimension_ids from the cube. Returns
    {"type": "rule_detail", "cube_id": str, "expression": str | None}.
    """
    cube = engine.require_cube_by_id(cube_id)
    dimension_ids = list(cube.dimension_ids)
    rule = engine.find_rule(cube_id, addr, dimension_ids)
    return {
        "type": "rule_detail",
        "cube_id": cube_id,
        "expression": rule.expression if rule else None,
    }


def cmd_cube_rule_counts(engine, cube_id: str) -> dict:
    """Get rule counts for a cube.

    Returns {"type": "cube_rule_counts", "cube_id": str,
             "cell_rules": int, "rules": int}.
    """
    counts = engine.rule_counts_for_cube(cube_id)
    return {
        "type": "cube_rule_counts",
        "cube_id": cube_id,
        "cell_rules": counts.get("anchored_rules", 0),
        "rules": counts.get("rules", 0),
    }


def cmd_cell_value_by_ref(
    engine,
    cube_name: str,
    channel: str,
    selectors: list[dict],
) -> dict:
    """Get cell value by semantic cube reference.

    Args:
        engine: Engine instance (query handler may read engine internals per Phase A)
        cube_name: Human-readable cube name (resolved via engine workspace)
        channel: Channel name without @ prefix, e.g. "value", "font_color"
        selectors: List of {"dimension": str, "item": str} pairs in address order

    Returns {"type": "cell_value_by_ref", "cube_id": str, "cube_name": str,
             "addr": list[str], "value": CellPrimitive, "display_value": str,
             "kind": CellKind, "error": str | None}.
    """
    # 1. Resolve cube name to cube object
    ws = getattr(engine, "workspace", None)
    if not ws:
        return {"type": "cell_value_by_ref", "error": "No workspace available"}

    cube = None
    cube_id = engine.resolve_cube_id_by_name(cube_name)
    if cube_id:
        cube = ws.cubes.get(cube_id)
    if not cube:
        cube = ws.cubes.get(cube_name)
    if not cube:
        for c in list(ws.cubes.values()):
            if getattr(c, "name", None) == cube_name:
                cube = c
                break
    if not cube:
        return {
            "type": "cell_value_by_ref",
            "error": f"Cube not found: {cube_name}",
        }

    # 2. Validate selector count against cube dimensions
    cube_dim_ids = [d for d in cube.dimension_ids if d != "@"]
    if len(selectors) != len(cube_dim_ids):
        return {
            "type": "cell_value_by_ref",
            "error": (
                f"Selector count mismatch: expected {len(cube_dim_ids)}, "
                f"got {len(selectors)}"
            ),
        }

    # 3. Resolve selectors to item IDs with explicit ambiguity detection
    addr_items: list[str] = []
    for i, sel in enumerate(selectors):
        dim_name = sel.get("dimension", "")
        item_name = sel.get("item", "")
        if not dim_name or not item_name:
            return {
                "type": "cell_value_by_ref",
                "error": f"Incomplete selector at position {i}",
            }

        matching_dims = []
        for d_id in cube_dim_ids:
            dim = ws.dimensions.get(d_id)
            if dim and getattr(dim, "name", None) == dim_name:
                matching_dims.append(dim)

        if len(matching_dims) > 1:
            return {
                "type": "cell_value_by_ref",
                "error": f"Ambiguous dimension/item name: {dim_name}",
            }
        if not matching_dims:
            return {
                "type": "cell_value_by_ref",
                "error": f"Dimension/item not found: {dim_name}.{item_name}",
            }

        dim = matching_dims[0]
        matching_items = [item for item in dim.items if item.name == item_name]

        if len(matching_items) > 1:
            return {
                "type": "cell_value_by_ref",
                "error": f"Ambiguous dimension/item name: {item_name}",
            }
        if not matching_items:
            return {
                "type": "cell_value_by_ref",
                "error": f"Dimension/item not found: {dim_name}.{item_name}",
            }

        addr_items.append(matching_items[0].id)

    # 4. Build engine address
    addr = (f"@.{channel}",) + tuple(addr_items)

    # 5. Read cell
    try:
        cell_value = engine.get_cell_by_addr(cube, addr)
    except Exception as exc:
        return {
            "type": "cell_value_by_ref",
            "error": f"Cell read failed: {exc}",
        }

    # get_cell_by_addr returns a raw value, not a CellValue object.
    value = getattr(cell_value, "value", cell_value)
    explain = getattr(cell_value, "explain", None)
    kind, display_value = _classify_cell_value(value, explain)

    return {
        "type": "cell_value_by_ref",
        "cube_id": cube.id,
        "cube_name": cube_name,
        "addr": list(addr),
        "value": _coerce_to_primitive(value),
        "display_value": display_value,
        "kind": kind,
        "error": None,
    }


def cmd_rule_target_resolve(engine, cube_id: str, lhs: str) -> dict:
    """Resolve a raw rule LHS string into a parsed targets list.

    Query ID: rule_target_resolve
    Payload:  { "cube_id": str, "lhs": str }
    Result:   { "type": "rule_target_resolve", "cube_id": str,
               "targets": list[tuple[str, str]] | None,
               "error": str | None }

    Note: parse_rule_target is a pure string parser; it does not require
    cube context. Cube existence is validated by the subsequent rule command.
    """
    from lib_openm.rule_eval import parse_rule_target

    try:
        targets = parse_rule_target(lhs)
    except Exception as e:
        return {
            "type": "rule_target_resolve",
            "cube_id": cube_id,
            "targets": None,
            "error": str(e),
        }

    return {
        "type": "rule_target_resolve",
        "cube_id": cube_id,
        "targets": targets,
        "error": None,
    }


def cmd_workspace_rules(engine, ws) -> dict:
    """Return all workspace rules + rule_order as plain dict DTOs.

    Query ID: workspace_rules
    Bus topic: query.workspace.rules
    Payload:  {}  (no filter; returns all workspace rules)
    Result:   { "type": "workspace_rules",
                "rules": list[dict], "rule_order": list[str],
                "cube_ids": list[str] }

    Each rule dict contains: id, cube_id, expression, targets, addr_mask,
    specificity, is_anchored.  No engine domain objects cross the boundary.
    """
    rules = []
    cube_ids: set[str] = set()
    for rid, r in list(ws.rules.items()):
        addr_mask = getattr(r, "addr_mask", None)
        specificity = sum(1 for v in (addr_mask or ()) if v is not None) if addr_mask else 0
        rules.append({
            "id": rid,
            "cube_id": r.cube_id,
            "expression": r.expression,
            "targets": list(r.targets) if r.targets else [],
            "addr_mask": list(addr_mask) if addr_mask else [],
            "specificity": specificity,
            "is_anchored": bool(getattr(r, "is_anchored", False)),
        })
        cube_ids.add(r.cube_id)

    order = list(getattr(ws, "rule_order", []) or [])
    return {
        "type": "workspace_rules",
        "rules": rules,
        "rule_order": order,
        "cube_ids": sorted(cube_ids),
    }


# =============================================================================
# Phase E grid/view shape query handler implementations
# =============================================================================


def cmd_view_row_keys(engine, view_id: str) -> dict:
    """Get view row keys as list of tuples."""
    keys = engine.view_row_keys(view_id)
    return {"type": "view_row_keys", "keys": keys}


def cmd_view_col_keys(engine, view_id: str) -> dict:
    """Get view column keys as list of tuples."""
    keys = engine.view_col_keys(view_id)
    return {"type": "view_col_keys", "keys": keys}


def cmd_view_row_header(engine, view_id: str, section: int) -> dict:
    """Get header label for a given row section.

    Mirrors engine.view_col_header logic but uses row_dim_ids.
    """
    view = engine.require_view_by_id(view_id)
    row_keys = engine.view_row_keys(view_id)
    if section >= len(row_keys):
        return {"type": "view_row_header", "header": ""}
    row_key = row_keys[section]
    labels: list[str] = []
    for dim_id in view.row_dim_ids:
        dim = engine.require_dimension_by_id(dim_id)
        idx = view.row_dim_ids.index(dim_id)
        if idx < len(row_key):
            item_id = row_key[idx]
            item = next((it for it in dim.items if it.id == item_id), None)
            if item:
                labels.append(item.name)
    return {"type": "view_row_header", "header": " | ".join(labels) if labels else ""}


def cmd_view_col_header(engine, view_id: str, section: int) -> dict:
    """Get header label for a given column section.

    Delegates to engine.view_col_header.
    """
    header = engine.view_col_header(view_id, section)
    return {"type": "view_col_header", "header": header}


# =============================================================================
# Phase F5b grid snapshot query handler implementations
# =============================================================================


def _compute_viewport_snapshot(
    engine,
    view,
    cube,
    row_keys: list[tuple[str, ...]],
    col_keys: list[tuple[str, ...]],
    page_selections: dict[str, str],
    requested_channels: list[str],
    allow_evaluation: bool = False,
) -> tuple[dict[str, dict], dict[str, dict[str, Any]], set[tuple[str, ...]]]:
    """Locking entry point for read-only snapshot computation.

    The engine is not generally thread-safe, but GUI tile queries run on
    background QThreads. Acquiring the engine's global reentrant lock here
    serializes those queries against engine mutations (e.g. rename) on the
    main thread and prevents concurrent reads from seeing torn workspace state.
    """
    with engine.engine_lock:
        return _compute_viewport_snapshot_locked(
            engine,
            view,
            cube,
            row_keys,
            col_keys,
            page_selections,
            requested_channels,
            allow_evaluation=allow_evaluation,
        )


def _compute_viewport_snapshot_locked(
    engine,
    view,
    cube,
    row_keys: list[tuple[str, ...]],
    col_keys: list[tuple[str, ...]],
    page_selections: dict[str, str],
    requested_channels: list[str],
    allow_evaluation: bool = False,
) -> tuple[dict[str, dict], dict[str, dict[str, Any]], set[tuple[str, ...]]]:
    """Read-only cell computation shared by single and batch snapshot commands.

    Returns ``(cells, channel_values, visible_addrs)``.  By default it never
    evaluates rules or records dependency edges, and raises ``SnapshotInvariantError``
    if a visible rule cell is dirty or untracked.  When ``allow_evaluation`` is
    True, dirty/untracked rule cells are evaluated first so the GUI can paint a
    tile even when the dependency graph has not yet caught up.
    """
    visible_addrs: set[tuple[str, ...]] = set()
    cells: dict[str, dict] = {}
    channel_values: dict[str, dict[str, Any]] = {}
    for ch in requested_channels:
        channel_values[ch] = {}

    addr_list: list[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str]] = []
    channel_addr_map: dict[str, dict[str, tuple[str, ...]]] = {}
    if requested_channels:
        for ch in requested_channels:
            channel_addr_map[ch] = {}

    with engine.dependency_tracking_disabled():
        for rk in row_keys:
            for ck in col_keys:
                addr = resolve_addr(view, cube, rk, ck, page_selections)
                visible_addrs.add(addr)
                key = make_viewport_cell_key(rk, ck)
                addr_list.append((rk, ck, addr, key))

                for ch in requested_channels:
                    channel_addr_map[ch][key] = resolve_addr(
                        view, cube, rk, ck, page_selections, channel=ch
                    )

    # Safety net for GUI tile fetches: rule-covered cells that are not yet
    # clean/tracked in the dependency graph are evaluated now so the read-only
    # path below can read their values.  This prevents the GUI from showing blank
    # tiles when the engine graph state lags behind structural changes (e.g.
    # attaching a new dimension) or when dependency tracking was toggled off.
    if allow_evaluation:
        needs_eval: set[tuple[str, ...]] = set()
        for rk, ck, addr, key in addr_list:
            meta = engine.resolve_cell_meta(cube, addr)
            if meta.source == "rule" and (meta.is_dirty or not meta.is_tracked):
                needs_eval.add(addr)
        for ch in requested_channels:
            ch_addr_map = channel_addr_map[ch]
            for _rk, _ck, _addr, key in addr_list:
                ch_addr = ch_addr_map[key]
                meta = engine.resolve_cell_meta(cube, ch_addr)
                if meta.source == "rule" and (meta.is_dirty or not meta.is_tracked):
                    needs_eval.add(ch_addr)

        tracking_enabled = engine.is_dependency_tracking_enabled()
        for addr in needs_eval:
            try:
                engine.get_cell_by_addr(cube, addr)
            except SnapshotInvariantError:
                raise
            except Exception:
                # Surface evaluation errors as CellError values below instead of
                # failing the whole viewport.
                pass
    else:
        tracking_enabled = engine.is_dependency_tracking_enabled()

    with engine.dependency_tracking_disabled():
        # --- Batch value fetch ---
        # When the engine supports get_cached_cell_values_batch (e.g.
        # RemoteEngine), use a single RPC to fetch all cell values instead
        # of one call per cell.  This is critical for remote engines where
        # each per-cell call is a network round-trip.
        #
        # Optimization: combine main cell addresses AND all channel addresses
        # into a single batch_fn call.  This reduces RPC calls from
        # (1 + len(channels)) per tile to just 1 per tile — a 15x reduction
        # for the typical 14-channel formatted tile fetch.
        batch_fn = getattr(engine, "get_cached_cell_values_batch", None)

        # Main cell values
        all_addrs = [addr for _rk, _ck, addr, _key in addr_list]

        if batch_fn is not None and all_addrs:
            # Collect channel addresses in the same order as channels
            ch_addr_lists: list[list[tuple[str, ...]]] = []
            for ch in requested_channels:
                ch_addr_map = channel_addr_map[ch]
                ch_addr_lists.append([ch_addr_map[key] for _rk, _ck, _addr, key in addr_list])

            # Combine main + all channel addresses into one flat list
            combined_addrs = list(all_addrs)
            for ch_addrs in ch_addr_lists:
                combined_addrs.extend(ch_addrs)

            combined_values = batch_fn(cube, combined_addrs)

            # Split combined results back into main and per-channel slices
            n_main = len(all_addrs)
            batch_values = combined_values[:n_main] if combined_values else [None] * n_main
            ch_batch_values: dict[str, list] = {}
            offset = n_main
            for ch_idx, ch in enumerate(requested_channels):
                n_ch = len(ch_addr_lists[ch_idx])
                if combined_values:
                    ch_batch_values[ch] = combined_values[offset:offset + n_ch]
                else:
                    ch_batch_values[ch] = [None] * n_ch
                offset += n_ch
        else:
            batch_values = None
            ch_batch_values = {}

        for idx, (rk, ck, addr, key) in enumerate(addr_list):
            meta = engine.resolve_cell_meta(cube, addr)
            if (
                tracking_enabled
                and meta.source == "rule"
                and (meta.is_dirty or not meta.is_tracked)
            ):
                raise SnapshotInvariantError(
                    f"grid_viewport_snapshot observed dirty/untracked rule cell at {addr}"
                )
            if batch_values is not None:
                value = batch_values[idx] if idx < len(batch_values) else None
            else:
                value = engine.get_cached_cell_value_by_addr(cube, addr)
            cells[key] = {
                "value": _coerce_to_primitive(value),
                "source": meta.source,
                "cube_id": cube.id,
                "addr": addr,
                "error": meta.error,
            }

        # Requested channels — use pre-fetched batch values
        for ch in requested_channels:
            ch_addr_map = channel_addr_map[ch]
            ch_batch = ch_batch_values.get(ch)

            for idx, (_rk, _ck, _addr, key) in enumerate(addr_list):
                if ch_batch is not None:
                    ch_value = ch_batch[idx] if idx < len(ch_batch) else None
                else:
                    ch_addr = ch_addr_map[key]
                    ch_value = engine.get_cached_cell_value_by_addr(cube, ch_addr)
                channel_values[ch][key] = _coerce_to_primitive(ch_value)

    return cells, channel_values, visible_addrs


def cmd_grid_viewport_snapshot(
    engine,
    view_id: str,
    row_keys: list[tuple[str, ...]],
    col_keys: list[tuple[str, ...]],
    page_selections: dict[str, str],
    channels: list[str] | None,
    profiler: Any | None = None,
    allow_evaluation: bool = False,
) -> dict:
    """Batch query for visible viewport cells, metadata, and channel values.

    Returns a plain ``ViewportSnapshotDTO`` dict.  No engine objects cross the
    boundary.  Addresses are resolved deterministically from ``page_selections``
    without consulting hidden GUI/session state.
    """
    _span = profiler.span if profiler is not None else lambda name, parent=None: contextlib.nullcontext()
    t0 = time.perf_counter()

    view = engine.require_view_by_id(view_id)
    cube = engine.require_cube_by_id(view.cube_id)

    # If channels is None, only value data is returned.
    requested_channels = channels or []
    if requested_channels:
        validate_channels(requested_channels)

    cell_count = len(row_keys) * len(col_keys)
    if DEBUG_ENGINE:
        print(
            f"[SNAPSHOT-START] view={view_id[:8] if view_id else None} "
            f"cells={cell_count} channels={requested_channels}",
            flush=True,
        )
    with _span("grid_viewport_snapshot.compute_cells"):
        cells, channel_values, visible_addrs = _compute_viewport_snapshot_locked(
            engine, view, cube, row_keys, col_keys, page_selections, requested_channels,
            allow_evaluation=allow_evaluation,
        )

    # Viewport-relevant metadata filtering (F5a non-blocking caution).
    # For correctness, include all; optimisation to filter may follow later.
    with _span("grid_viewport_snapshot.build_response"):
        item_formats: dict[str, CellFormatDict] = {}
        view_item_formats = getattr(view, "item_formats", {}) or {}
        for fmt_key, fmt in view_item_formats.items():
            item_formats[fmt_key] = cell_format_to_dict(fmt)

        group_formats: dict[str, CellFormatDict] = {}
        view_group_formats = getattr(view, "group_formats", {}) or {}
        for grp_key, fmt in view_group_formats.items():
            group_formats[grp_key] = cell_format_to_dict(fmt)

        # Filter user_override_addrs to visible addresses.
        visible_override_addrs = [
            addr for addr in getattr(cube, "user_override_addrs", set())
            if addr in visible_addrs
        ]

        duration_ms = (time.perf_counter() - t0) * 1000
        cell_count = len(row_keys) * len(col_keys)
        if DEBUG_ENGINE:
            print(
                f"[SNAPSHOT] view={view_id[:8] if view_id else None} cells={cell_count} "
                f"channels={requested_channels} duration={duration_ms:.1f} ms",
                flush=True,
            )
        if duration_ms > 50:
            logger.info(
                "[query] SLOW grid_viewport_snapshot view=%s cells=%d channels=%r duration=%.3f ms",
                view_id[:8] if view_id else None,
                cell_count,
                requested_channels,
                duration_ms,
            )
        return {
            "view_id": view_id,
            "cube_id": cube.id,
            "row_dim_ids": list(view.row_dim_ids),
            "col_dim_ids": list(view.col_dim_ids),
            "page_dim_ids": list(view.page_dim_ids),
            "item_formats": item_formats,
            "group_formats": group_formats,
            "user_override_addrs": visible_override_addrs,
            "cells": cells,
            "channels": channel_values,
        }


def cmd_grid_viewport_snapshot_batch(
    engine,
    view_id: str,
    tiles: list[dict[str, Any]],
    page_selections: dict[str, str],
    channels: list[str] | None,
    generation: int | None = None,
    profiler: Any | None = None,
    allow_evaluation: bool = False,
) -> dict:
    """Batch query for multiple viewport tiles in one command.

    Each tile is a dict with ``bounds`` (list of 4 ints), ``row_keys``, and
    ``col_keys``. The response is keyed by the JSON-serialized bounds and tagged
    with the caller-supplied ``generation`` so the GUI can discard stale batches.

    Returns a plain dict of the form ``{"generation": int, "tiles": {}}``.
    No engine objects cross the boundary.
    """
    _span = profiler.span if profiler is not None else lambda name, parent=None: contextlib.nullcontext()

    view = engine.require_view_by_id(view_id)
    cube = engine.require_cube_by_id(view.cube_id)

    requested_channels = channels or []
    if requested_channels:
        validate_channels(requested_channels)

    # Build view-level metadata once; each tile snapshot includes it so that
    # TileRenderer can treat each tile as a standalone ViewportSnapshotDTO.
    item_formats: dict[str, CellFormatDict] = {}
    view_item_formats = getattr(view, "item_formats", {}) or {}
    for fmt_key, fmt in view_item_formats.items():
        item_formats[fmt_key] = cell_format_to_dict(fmt)

    group_formats: dict[str, CellFormatDict] = {}
    view_group_formats = getattr(view, "group_formats", {}) or {}
    for grp_key, fmt in view_group_formats.items():
        group_formats[grp_key] = cell_format_to_dict(fmt)

    tile_payloads: dict[str, dict] = {}

    with _span("grid_viewport_snapshot_batch.compute_cells"):
        for tile in tiles:
            bounds = tile["bounds"]
            row_keys = tile["row_keys"]
            col_keys = tile["col_keys"]
            cells, channel_values, visible_addrs = _compute_viewport_snapshot_locked(
                engine, view, cube, row_keys, col_keys, page_selections, requested_channels,
                allow_evaluation=allow_evaluation,
            )
            visible_override_addrs = [
                addr for addr in getattr(cube, "user_override_addrs", set())
                if addr in visible_addrs
            ]
            bounds_key = json.dumps(bounds, separators=(",", ":"))
            tile_payloads[bounds_key] = {
                "view_id": view_id,
                "cube_id": cube.id,
                "row_dim_ids": list(view.row_dim_ids),
                "col_dim_ids": list(view.col_dim_ids),
                "page_dim_ids": list(view.page_dim_ids),
                "item_formats": item_formats,
                "group_formats": group_formats,
                "user_override_addrs": visible_override_addrs,
                "cells": cells,
                "channels": channel_values,
            }

    return {
        "generation": generation,
        "tiles": tile_payloads,
    }


def cmd_cell_channel_values(
    engine,
    view_id: str,
    row_key: tuple[str, ...],
    col_key: tuple[str, ...],
    page_selections: dict[str, str],
    channels: list[str],
) -> dict:
    """Single-cell channel value query.

    Returns a plain ``CellChannelValuesDTO`` dict.  No engine objects cross
    the boundary.
    """
    view = engine.require_view_by_id(view_id)
    cube = engine.require_cube_by_id(view.cube_id)

    validate_channels(channels)

    addr = resolve_addr(view, cube, row_key, col_key, page_selections)
    ch_data: dict[str, Any] = {}
    for ch in channels:
        ch_addr = resolve_addr(view, cube, row_key, col_key, page_selections, channel=ch)
        ch_data[ch] = _coerce_to_primitive(cube.get(ch_addr))

    return {
        "view_id": view_id,
        "cube_id": cube.id,
        "addr": addr,
        "channels": ch_data,
    }


def cmd_selection_stats(
    engine,
    view_id: str,
    mode: str,
    page_selections: dict[str, str],
    cell_keys: list[tuple[tuple[str, ...], tuple[str, ...]]] | None = None,
    row_keys: list[tuple[str, ...]] | None = None,
    col_keys: list[tuple[str, ...]] | None = None,
    max_cells: int | None = None,
    max_time: float = 3.0,
) -> dict:
    """Compute statistics for selected cells in a view.

    The engine resolves addresses using the supplied ``page_selections`` so
    stats match what the GUI renders (fixes the page-selection mismatch bug
    where cell_detail used engine-internal page state).

    Iteration is capped by both ``max_cells`` (default from
    ``lib_openm.config.SELECTION_STATS_MAX_CELLS``) and ``max_time``
    (default 3.0 s).  When either limit is exceeded the function samples
    uniformly and scales the aggregates back up so the status bar stays
    responsive on huge grids with rule-evaluated cells.

    This function never raises; it returns a zeroed DTO on any failure so
    callers can always render safely.
    """
    if max_cells is None:
        max_cells = _om_config.SELECTION_STATS_MAX_CELLS

    import time

    try:
        view = engine.require_view_by_id(view_id)
        cube = engine.require_cube_by_id(view.cube_id)

        all_row_keys = engine.view_row_keys(view_id)
        all_col_keys = engine.view_col_keys(view_id)

        # Build the list of (row_key, col_key) pairs to iterate
        if mode == "cell" and cell_keys:
            keys_to_iterate = cell_keys
        elif mode == "row" and row_keys:
            keys_to_iterate = [
                (rk, ck) for rk in row_keys for ck in all_col_keys
            ]
        elif mode == "col" and col_keys:
            keys_to_iterate = [
                (rk, ck) for rk in all_row_keys for ck in col_keys
            ]
        elif mode == "all":
            keys_to_iterate = [
                (rk, ck) for rk in all_row_keys for ck in all_col_keys
            ]
        else:
            keys_to_iterate = []

        total_count = len(keys_to_iterate)
        sampled = False

        # Cap iteration to keep the query responsive on large grids.
        if total_count > max_cells:
            step = max(2, total_count // max_cells)
            keys_to_iterate = keys_to_iterate[::step]
            sampled = True

        count = 0
        counta = 0
        total = 0.0
        min_val: float | None = None
        max_val: float | None = None

        start = time.perf_counter()
        iterated = 0
        for rk, ck in keys_to_iterate:
            if time.perf_counter() - start > max_time:
                sampled = True
                break
            iterated += 1
            # Yield the GIL every 50 iterations so the GUI main thread
            # can process spinner signals even under heavy CPU load.
            if iterated % 50 == 0:
                time.sleep(0)
            try:
                addr = resolve_addr(view, cube, rk, ck, page_selections)
            except Exception:
                continue
            try:
                value = engine.get_cell_by_addr(cube, addr)
            except Exception:
                value = None

            if value is not None and value != "":
                counta += 1
                try:
                    num = float(value)
                    count += 1
                    total += num
                    if min_val is None or num < min_val:
                        min_val = num
                    if max_val is None or num > max_val:
                        max_val = num
                except (ValueError, TypeError):
                    pass

        # Scale aggregates back up when sampling so they reflect the full selection.
        if sampled and count > 0 and iterated > 0:
            scale = total_count / iterated
            total *= scale
            count = int(count * scale)
            counta = int(counta * scale)

        return {
            "total_count": total_count,
            "count": count,
            "counta": counta,
            "sum": total,
            "avg": total / count if count > 0 else 0.0,
            "min": min_val,
            "max": max_val,
            "sampled": sampled,
        }
    except Exception:
        return {
            "total_count": 0,
            "count": 0,
            "counta": 0,
            "sum": 0.0,
            "avg": 0.0,
            "min": None,
            "max": None,
            "sampled": False,
        }

