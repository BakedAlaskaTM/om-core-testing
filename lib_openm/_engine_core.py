from __future__ import annotations

import logging
import math
import os
import re
import itertools
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Optional, Iterable

from lib_contracts.types import CircularReferenceError, RuleValidationError, CalculationCancelledError, SnapshotInvariantError
from lib_openm.engine_state import _EngineStateMachine
from lib_openm.rule_eval import CubeResolver, RuleEvaluator, parse_rule_target
from lib_openm.rule_eval.utils import CellError


def _normalize_ieee_special(value: Any) -> Any:
    """Normalize IEEE 754 special values (NaN, Inf) to CellError.

    Mirrors RuleEngine._normalize_ieee_special in engine.py.
    Returns CellError("#NUM!") for NaN, CellError("#RANGE!") for inf, otherwise the original value.
    """
    if isinstance(value, float):
        if math.isnan(value):
            return CellError("#NUM!")
        if math.isinf(value):
            return CellError("#RANGE!")
    return value


from lib_openm.model import (
    Cube,
    Dimension,
    DimensionItem,
    Rule,
    TableViewSpec,
    ViewLayout,
    Workspace,
    apply_layout_to_view,
)
from lib_openm.technical_ids import AT_PREFIX, CHANNEL_TO_AT_ID, normalize_technical_item_id
from lib_openm.undo import Action, CompositeAction, UndoManager
from lib_openm.deps import DependencyGraph
from lib_openm.config import SLOW_LOG_THRESHOLD
from lib_utils.coerce import coerce_user_value
from lib_openm.ports import (
    EVENT_CELL_UPDATED,
    EVENT_CELLS_UPDATED,
    EVENT_CUBE_CREATED,
    EVENT_DIMENSION_CREATED,
    EVENT_DIMENSION_DELETED,
    EVENT_DIMENSION_ITEM_CREATED,
    EVENT_DIMENSION_ITEM_RENAMED,
    EVENT_DIMENSION_RENAMED,
    EVENT_DIMENSION_STRUCTURE_CHANGED,
    EVENT_VIEW_CREATED,
    EVENT_VIEW_LAYOUT_CHANGED,
    EVENT_WORKSPACE_DIRTY_CHANGED,
)
from lib_utils.config import engine as engine_config, compute_trace, is_compute_trace_enabled


def _find_unquoted_equals(s: str) -> int:
    """Return the index of the first '=' that is not inside a single- or double-quoted string.

    Returns -1 if no unquoted '=' is found.
    """
    in_single = False
    in_double = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '\\' and i + 1 < len(s):
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == '=' and not in_single and not in_double:
            return i
        i += 1
    return -1


@dataclass
class AddAggregateItemResult:
    """Result of engine.add_aggregate_item()."""
    item_id: str
    item_name: str
    item_node_id: str
    aggregate_edge_id: str | None
    group_node_id: str


@dataclass
class RuleDiagnostic:
    """Structured diagnostic for a rule reference that can no longer be resolved."""
    original_ref: str
    kind: str
    deleted_id: str | None
    code: str


# Master debug flag from engine.conf (overridable by environment variable)
_DEBUG_ENABLED = bool(int(os.environ.get("OPENM_DEBUG", "0"))) or engine_config("debug", "debug_enabled", False)

_FLOW_TRACE_DEBUG = _DEBUG_ENABLED and (bool(int(os.environ.get("OPENM_DEBUG_CALC_FLOW", "0"))) or engine_config("debug", "flow_trace_debug", False))

# Debug flag for rule resolution tracing
_RULE_RESOLVE_DEBUG = _DEBUG_ENABLED and bool(int(os.environ.get("OPENM_RULE_EVAL_DEBUG", "0")))

# Debug flag for engine computation tracing
_DEBUG_ENGINE = _DEBUG_ENABLED and bool(int(os.environ.get("OPENM_DEBUG_ENGINE", "0")))

# Debug flag for set_cell operations
_DEBUG_SET_CELL = _DEBUG_ENABLED and bool(int(os.environ.get("OPENM_DEBUG_SET_CELL", "0")))


def _normalize_addr_for_cube(cube: Cube, addr: tuple[str, ...]) -> tuple[str, ...]:
    """Ensure address includes @ dimension coordinate for internal use.
    
    This is the SINGLE normalization point for all address handling.
    - Short addresses (N-1 elements) get @.value prepended
    - Full addresses (N elements) pass through unchanged
    - Anything else raises ValueError
    
    Args:
        cube: The cube the address belongs to
        addr: The address tuple (short or full)
        
    Returns:
        Full N-tuple address with @ dimension coordinate
        
    Raises:
        ValueError: If address length doesn't match expected dimensions
    """
    if "@" not in cube.dimension_ids:
        return addr  # Legacy cube without @ dimension
    
    expected_len = len(cube.dimension_ids)
    if len(addr) == expected_len:
        # Normalize @ dimension slot from legacy @.value to canonical at_value
        at_idx = cube.dimension_ids.index("@")
        if at_idx < len(addr) and addr[at_idx].startswith("@."):
            addr = addr[:at_idx] + (normalize_technical_item_id(addr[at_idx]),) + addr[at_idx + 1:]
        return addr
    
    if len(addr) == expected_len - 1:
        # Short address - prepend canonical at_value
        return (CHANNEL_TO_AT_ID["value"],) + addr
    
    # Mismatch - raise clear error
    raise ValueError(
        f"Address {addr} has {len(addr)} elements, "
        f"expected {expected_len} (full) or {expected_len - 1} (short) "
        f"for cube with dims {cube.dimension_ids}"
    )


@dataclass(frozen=True)
class Explain:
    source: str  # "input" | "rule" | "empty" | "error"
    cube_id: str
    addr: tuple[str, ...]
    rule_body: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CellValue:
    value: Any
    explain: Explain


@dataclass(frozen=True)
class CellMeta:
    """Read-only metadata for a cell, used by the snapshot path.

    This structure intentionally carries no evaluated value. It is produced by
    ``engine.resolve_cell_meta`` without triggering rule evaluation or dependency
    tracking mutations.
    """

    source: str          # "input" | "rule" | "override" | "empty"
    has_rule: bool       # True if a rule covers this address
    is_override: bool    # True if user hardvalue overrides the rule
    is_dirty: bool       # True if dependency graph marks this node dirty
    is_tracked: bool     # True if dependency edges have been recorded for this node
    error: str | None    # evaluation error code, if the cached value is a CellError


class _EngineCore:
    """Public API layer over the model.

    The GUI (and future CLI/tests) should depend on this class rather than touching
    Workspace internals directly.
    """

    def __init__(self, facade: 'Engine', workspace: Workspace, *, event_publisher=None) -> None:
        self._facade = facade
        self._ws = workspace
        self._event_publisher = event_publisher
        # Global lock that serializes engine mutations against read-only snapshot
        # queries that run on background GUI threads. Reentrant so that a public
        # engine method can safely call other engine methods while holding it.
        self._engine_lock = threading.RLock()

        # Per-cube locks for thread-safe access during parallel recompute.
        # A single global lock would serialize all workers even when they touch
        # different cubes; per-cube locks allow parallelism across cubes.
        self._cube_locks: dict[str, threading.Lock] = {}
        self._rule_evaluator = RuleEvaluator()
        self._undo = UndoManager()
        self._thread_eval_state = threading.local()
        self._eval_strict_mode = False
        self._dep_tracking_enabled = True  # Enable by default for proper recalculation
        self._generation = 0  # Global workspace-session generation for async tile consistency
        self._dep_metrics: dict[str, int] = {
            "slice_hits": 0,
            "slice_misses": 0,
            "func_hits": 0,
            "func_misses": 0,
            "mt_parallel_runs": 0,
            "mt_parallel_nodes": 0,
            "mt_parallel_frontiers": 0,
            "mt_last_run_ms": 0,
            "mt_last_run_nodes": 0,
            "mt_last_run_frontiers": 0,
            "mt_last_run_max_frontier": 0,
        }
        self._on_dimension_item_renamed: Callable[[], None] | None = None
        self._on_dimension_renamed: Callable[[], None] | None = None
        self._gui_ready = False
        self._resolver_cache: dict[str, Any] = {}
        # Multithreaded recompute is OFF by default and must be opted into
        # explicitly via enable_multithread_recompute().  This keeps the
        # default engine serial, predictable, and safe for tests and benchmarks.
        self._multithread_recompute_enabled = False
        self._multithread_recompute_workers = max(1, (os.cpu_count() or 2) // 2)
        # Threshold below which parallel recompute stays serial.
        # Default is effectively infinity because benchmarked ThreadPoolExecutor
        # overhead per future exceeds rule evaluation cost at every tested
        # frontier size (1–512).  Parallel backend is opt-in only.
        # Can be overridden in om-engine.conf under [performance] parallel_threshold.
        self._min_parallel_frontier_size = int(
            engine_config("performance", "parallel_threshold", 1_000_000_000)
        )
        self._reuse_worker_pool = bool(
            engine_config("performance", "reuse_worker_pool", True)
        )
        self._mt_batch_size = max(1, int(
            engine_config("performance", "mt_batch_size", 1)
        ))
        # Persistent worker pool (created on first MT recompute if reuse is enabled)
        self._worker_pool: Any | None = None
        self._calc_lock = threading.Lock()  # Lock for preventing concurrent recalculations
        # Canonical lifecycle state machine and serialized-command guard.
        self._state_machine = _EngineStateMachine(
            event_publisher=event_publisher, engine_facade=self._facade
        )
        from lib_openm.udf_registry import get_default_registry
        self.udf_registry = get_default_registry()
        # Reset all workspace-derived state from the initial workspace.
        self._reset_derived_state()

    def _publish_event(self, topic_suffix: str, payload: dict) -> None:
        """Publish an event envelope via injected publisher.

        If no publisher was injected (e.g., tests), events are silently dropped.
        This preserves backward compatibility for tests that create Engine directly.
        """
        if self._event_publisher is not None:
            try:
                self._event_publisher.publish(topic_suffix, payload, self._facade)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "Failed to publish engine event: event.%s", topic_suffix
                )

    @property
    def undo_manager(self) -> UndoManager:
        return self._undo

    @property
    def workspace(self) -> Workspace:
        return self._ws

    @property
    def is_calculating(self) -> bool:
        """True when a recalculation is currently in progress."""
        return self._calc_in_progress

    def list_views(self) -> list[TableViewSpec]:
        # Return views in the order specified by views_order
        ws = self._ws
        ordered = []
        seen = set()
        for vid in ws.views_order:
            view = ws.views.get(vid)
            if view is not None and vid not in seen:
                ordered.append(view)
                seen.add(vid)
        # Append any views not in views_order (for back-compat)
        for vid, view in ws.views.items():
            if vid not in seen:
                ordered.append(view)
        return ordered

    def require_view_by_id(self, view_id: str) -> TableViewSpec:
        """Canonical: return a view by stable ID. Raises KeyError if absent."""
        return self._ws.views[view_id]

    def require_dimension_by_id(self, dim_id: str) -> Dimension:
        """Canonical: return a dimension by stable ID. Raises KeyError if absent."""
        return self._ws.dimensions[dim_id]

    def dimension_outline_for_dim(self, dim_id: str) -> list[Any]:
        """Canonical: return cached outline for a dimension; rebuilds lazily if stale."""
        return self._ws.get_outline(dim_id)

    def require_cube_by_id(self, cube_id: str) -> Cube:
        """Canonical: return a cube by stable ID. Raises KeyError if absent."""
        return self._ws.cubes[cube_id]

    def find_cube_by_id(self, cube_id: str) -> Cube | None:
        """Canonical: return a cube by stable ID, or None if not found."""
        return self._ws.cubes.get(cube_id)

    def list_cube_ids(self) -> list[str]:
        """Return all cube IDs in the workspace."""
        return list(self._ws.cubes.keys())

    def has_system_graph_cubes(self) -> bool:
        """Check whether the system graph cubes (%RECNODADR and %RECNOD) exist."""
        return (
            any(d.name == "%RECNODADR" for d in self._ws.dimensions.values())
            and any(c.name == "%RECNOD" for c in self._ws.cubes.values())
        )

    def find_rule(self, cube_id: str, addr: tuple[str, ...], dimension_ids: list[str]) -> Any | None:
        """Return the best matching rule for a cube and address, or None."""
        return self._ws.find_rule(cube_id, addr, dimension_ids)

    def rule_counts_for_cube(self, cube_id: str) -> dict[str, int]:
        """Canonical: return counts of anchored rules and rules for a cube."""
        anchored_count = sum(
            1 for r in self._ws.rules.values()
            if r.cube_id == cube_id and r.is_anchored
        )
        rule_count = sum(1 for r in self._ws.rules.values() if r.cube_id == cube_id)
        return {"anchored_rules": anchored_count, "rules": rule_count}

    def _ensure_group_in_graph(
        self,
        dim_id: str,
        group_node: Any,
        parent_group_id: str | None = None,
    ) -> str:
        """Ensure a group node exists in the graph. Thin wrapper over bridge code."""
        from lib_openm.outline_graph_bridge import ensure_group_in_graph as _ensure
        return _ensure(dim_id, group_node, self._ws, parent_group_id)

    def _reset_derived_state(self) -> None:
        """Reset all engine state derived from the current workspace.

        This is the canonical post-load / post-replacement path. It must be
        called after the workspace reference changes.
        """
        # Rebuild in-memory ITEM_REF lookup index for fast node resolution
        self._ws.rebuild_item_ref_index()
        # Track current pivot view for each cube (for rules like Cube::*.*)
        self._cube_pivot_view: dict[str, str] = {}
        # Per-cube resolver cache to avoid rebuilding the closure-heavy resolver
        # on every rule evaluation during bootstrap and recalculation.
        self._resolver_cache: dict[str, Any] = {}
        # Per-view cache of computed CellValue keyed by addr to avoid recompute during a render pass.
        self._cell_cache: dict[tuple[str, str, tuple[str, ...]], CellValue] = {}
        self._slice_cache: dict[str, Any] = {}
        self._function_cache: dict[str, Any] = {}
        # Volatile functions (RAND, RANDBETWEEN) cache - cleared when dirty nodes detected
        # This ensures volatile functions return same values during paint but new values after changes
        self._volatile_func_cache: dict[str, Any] = {}
        # Track cells containing volatile functions (RAND, RANDBETWEEN) - need re-eval on any change
        # Key: (cube_id, addr_tuple), Value: set of volatile function names
        self._volatile_cells: dict[tuple[str, tuple[str, ...]], set[str]] = {}
        # Dependency graph for the workspace
        self._dep_graph = DependencyGraph()
        # Nodes that have been evaluated at least once with dependency tracking on.
        # Used to skip bootstrap for cells with zero precedents (e.g. RAND()).
        self._tracked_nodes: set[str] = set()
        self._recompute_counts: dict[str, int] = {}
        # Internal diagnostics for rule references that point to deleted objects.
        # Keyed by rule ID. Populated by _mark_deleted_* helpers; surfaced as
        # CellError("#REF!") when the rule is evaluated.
        self._rule_diagnostics: dict[str, list[RuleDiagnostic]] = {}
        self._rule_eval_profile_totals: dict[str, float] = {
            "eval_count": 0.0,
            "eval_ms_total": 0.0,
            "slow_eval_count": 0.0,
        }
        self._rule_eval_profile_hotspots: dict[str, float] = {}
        self._rule_eval_profile_by_expr: dict[str, dict[str, float]] = {}
        self._rule_eval_profile_by_fn: dict[str, dict[str, float]] = {}
        # Calculation state
        self._cancel_requested = False
        self._calc_in_progress = False
        self._recomputing = False
        # Scan existing rules for volatile functions (RAND, etc.)
        # This ensures rules loaded from file are properly tracked
        self._scan_all_rule_bodies_for_volatile()
        # GUI readiness is gated on a successful dependency-graph bootstrap.
        self._gui_ready = False

    def replace_workspace(self, ws: Any) -> None:
        """Replace the workspace and reset all derived engine state.

        If the reset path fails, the engine is restored to the previous workspace.
        """
        previous_ws = self._ws
        self._ws = ws
        try:
            self._reset_derived_state()
        except Exception:
            self._ws = previous_ws
            raise

    def _clear_caches(self) -> None:
        """Clear internal evaluation caches. Call after loading a new workspace."""
        self._cell_cache.clear()
        if hasattr(self, '_slice_cache'):
            self._slice_cache.clear()
        if hasattr(self, '_function_cache'):
            self._function_cache.clear()

    def _prune_outline_item(self, outline: list[Any], item_ids: set[str]) -> list[Any]:
        cleaned: list[Any] = []
        for node in outline:
            node_item_id = getattr(node, "item_id", None)
            if isinstance(node_item_id, str) and node_item_id in item_ids:
                continue
            children = list(getattr(node, "children", []) or [])
            pruned_children = self._prune_outline_item(children, item_ids) if children else []
            if hasattr(node, "children"):
                node.children = pruned_children
            cleaned.append(node)
        return cleaned

    def analyze_dimension_item_deletion(self, dim_id: str, item_ids: Iterable[str]) -> dict[str, Any]:
        dim = self.require_dimension_by_id(dim_id)
        ordered_item_ids: list[str] = []
        seen: set[str] = set()
        valid_item_ids = {it.id for it in dim.items}
        for item_id in item_ids:
            if item_id in seen or item_id not in valid_item_ids:
                continue
            seen.add(item_id)
            ordered_item_ids.append(item_id)

        item_names = [it.name for it in dim.items if it.id in seen]
        impacted_cubes: list[dict[str, Any]] = []
        total_data_cells = 0
        for cube in self._ws.cubes.values():
            if dim_id not in cube.dimension_ids:
                continue
            dim_slot = cube.dimension_ids.index(dim_id)
            count = sum(1 for addr in cube.data.keys() if len(addr) > dim_slot and addr[dim_slot] in seen)
            if count <= 0:
                continue
            impacted_cubes.append({"cube_id": cube.id, "cube_name": cube.name, "data_cell_count": count})
            total_data_cells += count

        impacted_cubes.sort(key=lambda entry: str(entry["cube_name"]).lower())

        # Count affected rules (anchored rules + general rules)
        affected_anchored = 0
        affected_rules = 0

        for rid, r in self._ws.rules.items():
            cube = self._ws.cubes.get(r.cube_id)
            if cube is None or dim_id not in cube.dimension_ids:
                continue
            dim_slot = cube.dimension_ids.index(dim_id)
            if r.is_anchored and r.addr_mask is not None and len(r.addr_mask) > dim_slot and r.addr_mask[dim_slot] in seen:
                affected_anchored += 1
                continue

        for rid, r in self._ws.rules.items():
            # Check if rule's addr_mask references any deleted items
            cube = self._ws.cubes.get(r.cube_id)
            if cube is None or dim_id not in cube.dimension_ids:
                continue
            if r.addr_mask is None:
                continue
            dim_slot = cube.dimension_ids.index(dim_id)
            if len(r.addr_mask) > dim_slot and r.addr_mask[dim_slot] in seen:
                affected_rules += 1

        return {
            "dim_id": dim.id,
            "dim_name": dim.name,
            "item_ids": ordered_item_ids,
            "item_names": item_names,
            "cube_count": len(impacted_cubes),
            "total_data_cell_count": total_data_cells,
            "impacted_cubes": impacted_cubes,
            "affected_anchored_rules": affected_anchored,
            "affected_rules": affected_rules,
            "total_affected_rules": affected_anchored + affected_rules,
        }

    def analyze_dimension_deletion_impact(self, dim_id: str, item_ids: list[str]) -> dict[str, Any]:
        """Analyze the impact of deleting an entire dimension (all its items + the dimension itself).
        
        This includes impact from deleting items AND impact from removing the dimension from cubes.
        """
        # First get the item deletion impact
        item_impact = self.analyze_dimension_item_deletion(dim_id, item_ids)
        
        # Calculate additional impact from removing the dimension itself
        total_data_cells = item_impact.get("total_data_cell_count", 0)
        anchored_rules = item_impact.get("affected_anchored_rules", 0)
        rules = item_impact.get("affected_rules", 0)

        # Check cubes that use this dimension - additional rules might be affected
        # when the dimension is removed entirely (not just items deleted)
        for cube in self._ws.cubes.values():
            if dim_id not in cube.dimension_ids:
                continue
            dim_slot = cube.dimension_ids.index(dim_id)
            # Anchored rules that reference this dimension anywhere
            for rid, r in self._ws.rules.items():
                if r.cube_id != cube.id:
                    continue
                if r.is_anchored and r.addr_mask is not None and len(r.addr_mask) > dim_slot:
                    if r.addr_mask[dim_slot] is not None:
                        anchored_rules += 1
            # Rules that target this dimension
            for rid, r in self._ws.rules.items():
                if r.cube_id != cube.id:
                    continue
                # Check if rule targets this dimension via addr_mask
                if r.addr_mask and dim_slot < len(r.addr_mask):
                    if r.addr_mask[dim_slot] is not None:
                        rules += 1
        
        return {
            "total_data_cells": total_data_cells,
            "anchored_rules": anchored_rules,
            "rules": rules,
        }

    def delete_dimension_items(self, dim_id: str, item_ids: Iterable[str]) -> dict[str, Any]:
        impact = self.analyze_dimension_item_deletion(dim_id, item_ids)
        delete_ids = set(impact["item_ids"])
        if not delete_ids:
            return impact

        dim = self.require_dimension_by_id(dim_id)

        # Save old column keys for views that use this dimension on the column axis
        affected_views: list[tuple[str, list[tuple[str, ...]], dict[int, int]]] = []
        for view in self._ws.views.values():
            if dim_id in view.col_dim_ids:
                old_keys = self.view_col_keys(view.id)
                old_widths = dict(view.col_widths)
                affected_views.append((view.id, old_keys, old_widths))
        
        # Get item names before deletion for rule updates
        deleted_item_names = {it.name for it in dim.items if it.id in delete_ids}
        
        dim.items = [it for it in dim.items if it.id not in delete_ids]

        # Remap persisted column widths to new indices after deletion
        for view_id, old_keys, old_widths in affected_views:
            self._remap_view_col_widths(view_id, old_keys, old_widths)

        # Phase 4: remove deleted items from graph, then rebuild outline
        from lib_openm.graph_mutation import (
            _find_item_ref_node_id,
            _display_parent_edge,
            _delete_edge_raw,
            _remove_node_raw,
            _dim_by_name,
            _cube_by_name,
        )
        from lib_openm.outline_graph_bridge import sync_graph_to_outline
        has_system_cubes = (
            _dim_by_name(self._ws, "%RECNODADR") is not None
            and _cube_by_name(self._ws, "%RECNOD") is not None
        )
        if has_system_cubes:
            for item_id in delete_ids:
                node_id = _find_item_ref_node_id(dim_id, item_id, self._ws)
                if node_id is None:
                    continue
                edge = _display_parent_edge(node_id, dim_id, self._ws)
                if edge is not None:
                    _delete_edge_raw(edge["edge_id"], self._ws)
                _remove_node_raw(node_id, self._ws)
            sync_graph_to_outline(dim, self._ws)

        for view in self._ws.views.values():
            if view.row_dim_ids and view.row_dim_ids[0] == dim_id and getattr(view, "row_outline", None):
                view.row_outline = self._prune_outline_item(list(view.row_outline), delete_ids)
            if view.col_dim_ids and view.col_dim_ids[0] == dim_id and getattr(view, "col_outline", None):
                view.col_outline = self._prune_outline_item(list(view.col_outline), delete_ids)

        for cube in self._ws.cubes.values():
            if dim_id not in cube.dimension_ids:
                continue
            dim_slot = cube.dimension_ids.index(dim_id)
            # Remove data cells with deleted items
            cube.data = {
                addr: value
                for addr, value in cube.data.items()
                if not (len(addr) > dim_slot and addr[dim_slot] in delete_ids)
            }
            # Also clean up user_override_addrs to remove addresses with deleted items
            if hasattr(cube, 'user_override_addrs'):
                cube.user_override_addrs = {
                    addr for addr in cube.user_override_addrs
                    if not (len(addr) > dim_slot and addr[dim_slot] in delete_ids)
                }

        # Record rule diagnostics for deleted items; evaluator surfaces #REF!.
        self._mark_deleted_item_in_all_rules(dim.name, deleted_item_names)

        # Drop page selections pointing to deleted items in this dimension.
        for view in self._ws.views.values():
            view.page_selections = {
                d_id: item_id
                for d_id, item_id in view.page_selections.items()
                if not (d_id == dim_id and item_id in delete_ids)
            }
        self._cell_cache.clear()
        self._invalidate_slice_dependent_rules()
        return impact

    def _add_rule_diagnostic(
        self, rule_id: str, original_ref: str, kind: str, deleted_id: str | None, code: str = "#REF!"
    ) -> None:
        """Record a diagnostic for a rule reference that can no longer be resolved."""
        self._rule_diagnostics.setdefault(rule_id, []).append(
            RuleDiagnostic(original_ref=original_ref, kind=kind, deleted_id=deleted_id, code=code)
        )

    def _mark_deleted_item_in_all_rules(self, dim_name: str, deleted_item_names: set[str]) -> None:
        """Record diagnostics for rules referencing deleted dimension items.

        Rule expression text is preserved; the evaluator surfaces CellError("#REF!")
        when the rule is evaluated and the reference cannot be resolved.
        """

        if not deleted_item_names:
            return

        import re

        changed_cell_addrs: set[tuple[str, tuple[str, ...]]] = set()
        changed_cube_ids: set[str] = set()

        def _references_deleted_item(expr: str, item_name: str) -> bool:
            pattern = rf'\b{re.escape(dim_name)}\s*[:.]\s*{re.escape(item_name)}\b'
            return bool(re.search(pattern, expr, flags=re.IGNORECASE))

        # Scan anchored cell rules for deleted references and record diagnostics
        for rid, r in list(self._ws.rules.items()):
            if not r.is_anchored:
                continue
            affected = False
            for item_name in deleted_item_names:
                if _references_deleted_item(r.expression, item_name):
                    self._add_rule_diagnostic(rid, f"{dim_name}.{item_name}", "dimension_item", item_name)
                    affected = True
            if r.targets:
                for t_dim_name, t_item_name in r.targets:
                    if t_dim_name.lower() == dim_name.lower() and t_item_name in deleted_item_names:
                        self._add_rule_diagnostic(rid, f"{t_dim_name}.{t_item_name}", "dimension_item", t_item_name)
                        affected = True
            if r.addr_mask:
                cube = self._ws.cubes.get(r.cube_id)
                if cube:
                    for slot, d_id in enumerate(cube.dimension_ids):
                        dim = self._ws.dimensions.get(d_id)
                        if dim and dim.name.lower() == dim_name.lower():
                            if slot < len(r.addr_mask) and r.addr_mask[slot] in deleted_item_names:
                                self._add_rule_diagnostic(rid, f"{dim_name}.{r.addr_mask[slot]}", "dimension_item", r.addr_mask[slot])
                                affected = True
                            break
            if affected:
                changed_cell_addrs.add((r.cube_id, r.addr_mask))

        # Scan non-anchored rules for deleted references and record diagnostics
        for rid, r in list(self._ws.rules.items()):
            if r.is_anchored:
                continue
            affected = False
            for item_name in deleted_item_names:
                if _references_deleted_item(r.expression, item_name):
                    self._add_rule_diagnostic(rid, f"{dim_name}.{item_name}", "dimension_item", item_name)
                    affected = True
            if r.targets:
                for t_dim_name, t_item_name in r.targets:
                    if t_dim_name.lower() == dim_name.lower() and t_item_name in deleted_item_names:
                        self._add_rule_diagnostic(rid, f"{t_dim_name}.{t_item_name}", "dimension_item", t_item_name)
                        affected = True
            if r.addr_mask:
                cube = self._ws.cubes.get(r.cube_id)
                if cube:
                    for slot, d_id in enumerate(cube.dimension_ids):
                        dim = self._ws.dimensions.get(d_id)
                        if dim and dim.name.lower() == dim_name.lower():
                            if slot < len(r.addr_mask) and r.addr_mask[slot] in deleted_item_names:
                                self._add_rule_diagnostic(rid, f"{dim_name}.{r.addr_mask[slot]}", "dimension_item", r.addr_mask[slot])
                                affected = True
                            break
            if affected:
                changed_cube_ids.add(r.cube_id)

        self._cell_cache.clear()

        # Mark affected cells as dirty so they recompute automatically
        for cube_id, addr in changed_cell_addrs:
            self._invalidate_cell_node(cube_id, addr)
        for cube_id in changed_cube_ids:
            self._invalidate_cube(cube_id)

    def _mark_deleted_group_in_all_rules(self, dim_name: str, deleted_group_names: set[str] | str) -> None:
        """Record diagnostics for rules referencing deleted outline groups.

        Rule expression text is preserved; the evaluator surfaces CellError("#REF!")
        when the rule is evaluated and the group reference cannot be resolved.
        """
        # Normalize to set - handle both single string and set of strings
        if isinstance(deleted_group_names, str):
            deleted_group_names = {deleted_group_names}
        if not deleted_group_names:
            return

        import re

        changed_cell_addrs: set[tuple[str, tuple[str, ...]]] = set()
        changed_cube_ids: set[str] = set()

        def _references_deleted_group(expr: str, group_name: str) -> bool:
            pattern = rf'\b{re.escape(dim_name)}\s*[:.]\s*{re.escape(group_name)}\b'
            return bool(re.search(pattern, expr, flags=re.IGNORECASE))

        # Scan all rules for deleted group references and record diagnostics
        for rid, r in list(self._ws.rules.items()):
            affected = False
            for group_name in deleted_group_names:
                if _references_deleted_group(r.expression, group_name):
                    self._add_rule_diagnostic(rid, f"{dim_name}.{group_name}", "group", group_name)
                    affected = True
            if r.targets:
                for t_dim_name, t_item_name in r.targets:
                    if t_dim_name.lower() == dim_name.lower() and t_item_name in deleted_group_names:
                        self._add_rule_diagnostic(rid, f"{t_dim_name}.{t_item_name}", "group", t_item_name)
                        affected = True
            if affected:
                if r.is_anchored:
                    changed_cell_addrs.add((r.cube_id, r.addr_mask))
                else:
                    changed_cube_ids.add(r.cube_id)

        self._cell_cache.clear()

        # Mark affected cells as dirty so they recompute automatically
        for cube_id, addr in changed_cell_addrs:
            self._invalidate_cell_node(cube_id, addr)
        for cube_id in changed_cube_ids:
            self._invalidate_cube(cube_id)

    def _format_addr_label(self, cube_id: str, addr: tuple[str, ...]) -> str:
        cube = self.require_cube_by_id(cube_id)
        parts: list[str] = []
        for dim_id, item_id in zip(cube.dimension_ids, addr):
            dim = self._ws.dimensions.get(dim_id)
            if dim is None:
                parts.append(item_id)
                continue
            item_name = next((it.name for it in dim.items if it.id == item_id), item_id)
            parts.append(f"{dim.name}.{item_name}")
        label = ", ".join(parts)
        if cube.name:
            return f"{cube.name}: {label}" if label else cube.name
        return label

    # ------------------------------------------------------------------
    # Dependency graph helpers (feature-flagged)

    def enable_dependency_tracking(self, enabled: bool = True) -> None:
        previously = self._dep_tracking_enabled
        if enabled == previously:
            return
        self._dep_tracking_enabled = enabled
        self._dep_graph = DependencyGraph()
        self._tracked_nodes.clear()
        self._reset_thread_eval_state()
        self._slice_cache.clear()
        self._function_cache.clear()
        self._cell_cache.clear()
        self._reset_dep_metrics()

    def _clear_cell_cache(self) -> None:
        """Drop cached cell values so subsequent reads recompute from rules."""
        self._cell_cache.clear()

    def _clear_cache(self) -> None:
        """Alias for clear_cell_cache() - clears all cached cell values."""
        self._cell_cache.clear()

    def _thread_eval_context(self) -> list[str]:
        ctx = getattr(self._thread_eval_state, "eval_context", None)
        if not isinstance(ctx, list):
            ctx = []
            self._thread_eval_state.eval_context = ctx
        return ctx

    def _thread_pending_precedents(self) -> dict[str, set[str]]:
        pending = getattr(self._thread_eval_state, "pending_precedents", None)
        if not isinstance(pending, dict):
            pending = {}
            self._thread_eval_state.pending_precedents = pending
        return pending

    def _thread_eval_stack(self) -> set[tuple[str, tuple[str, ...]]]:
        stack = getattr(self._thread_eval_state, "eval_stack", None)
        if not isinstance(stack, set):
            stack = set()
            self._thread_eval_state.eval_stack = stack
        return stack

    def _is_tracking_enabled(self) -> bool:
        """Check if dependency tracking is enabled for the current thread.
        
        This respects both the global flag and a thread-local override,
        allowing worker threads to disable tracking locally without
        affecting the main thread.
        """
        if not self._dep_tracking_enabled:
            return False
        # Check thread-local override (used by worker threads)
        thread_disabled = getattr(self._thread_eval_state, "tracking_disabled", False)
        return not thread_disabled

    @contextmanager
    def dependency_tracking_disabled(self):
        """Temporarily disable dependency tracking for the current thread.

        Use this around read-only bulk queries (e.g. tile fetches) to avoid
        forcing re-evaluation of cached cells solely to record dependency
        edges.  Values are still read from and written to the cell cache, but
        no graph edges are created while the context is active.
        """
        old_disabled = getattr(self._thread_eval_state, "tracking_disabled", False)
        self._thread_eval_state.tracking_disabled = True
        try:
            yield
        finally:
            self._thread_eval_state.tracking_disabled = old_disabled

    def _reset_thread_eval_state(self) -> None:
        self._thread_eval_state.eval_context = []
        self._thread_eval_state.pending_precedents = {}
        self._thread_eval_state.eval_stack = set()

    def _drop_cached_cell(self, cube_id: str, addr: tuple[str, ...]) -> None:
        dead_keys = [key for key in self._cell_cache if key[0] == cube_id and key[2] == addr]
        for key in dead_keys:
            self._cell_cache.pop(key, None)

    def _recompute_rule_at_addr(self, cube: Cube, addr: tuple[str, ...]) -> None:
        """Re-evaluate rule at address and store result in cube data."""
        # PRECEDENCE: Hard numbers > cell rules > rules
        # If this cell has a hard number (user override), never recompute
        if cube.is_user_override(addr):
            return

        # Check for cell rule first
        rule = self._ws.find_anchored_rule(cube.id, addr)
        if rule is not None:
            try:
                resolver = self._make_resolver(cube)
                expr = self._normalize_expression(rule.expression)
                result = self._rule_evaluator.eval(expr, resolver=resolver, base_addr=addr)
                cube.set(addr, result)
            except Exception:
                pass  # Ignore errors, leave as None
            return

        # Check for rule (only if no hard number or cell rule)
        rule = self._ws.find_rule(cube.id, addr, cube.dimension_ids)
        if rule is not None:
            try:
                resolver = self._make_resolver(cube)
                expr = self._normalize_expression(rule.expression)
                result = self._rule_evaluator.eval(expr, resolver=resolver, base_addr=addr)
                cube.set(addr, result)
            except Exception:
                pass  # Ignore errors, leave as None

    def _clear_cached_node(self, node_key: str) -> None:
        parsed = self._parse_cell_node_key(node_key)
        if parsed is not None:
            cube_id, addr = parsed
            self._drop_cached_cell(cube_id, addr)
            return
        if node_key.startswith("slice::"):
            self._slice_cache.pop(node_key, None)
        elif node_key.startswith("func::"):
            self._function_cache.pop(node_key, None)
            # Note: Re-evaluation should happen lazily when the cell is next accessed
            # Don't recompute here - that defeats the purpose of dirty marking

    def _mark_node_and_dependents_dirty(self, node_key: str) -> None:
        queue = [node_key]
        seen: set[str] = set()
        # Clear volatile function cache when actual changes happen
        # This ensures RAND() and similar functions generate new values after changes
        self._volatile_func_cache.clear()
        # Also mark all cells with volatile functions as dirty
        # This forces them to re-evaluate and get new random values
        for (cube_id, addr), _ in list(self._volatile_cells.items()):
            volatile_node_key = self._cell_node_key(cube_id, addr)
            self._dep_graph.mark_dirty(volatile_node_key)
            self._clear_cached_node(volatile_node_key)
            # Clear cube cell data to force recompute (unless it's a hardnumber)
            cube = self._ws.cubes.get(cube_id)
            if cube is not None and addr in cube.data:
                if addr not in cube.user_override_addrs:
                    cube.set(addr, None)
        while queue:
            key = queue.pop()
            if key in seen:
                continue
            seen.add(key)
            self._dep_graph.mark_dirty(key)
            self._clear_cached_node(key)
            # Clear cube cell data for rule cells to force recompute
            # Parse node key to get cube_id and addr
            is_rule_cell = False
            if key.startswith("cell::"):
                parts = key.split("::")
                if len(parts) >= 3:
                    cube_id = parts[1]
                    addr = tuple(parts[2].split(",")) if parts[2] else tuple()
                    cube = self._ws.cubes.get(cube_id)
                    # Normalize address to full format (with @.value prefix) for cube data lookup
                    if cube is not None and len(addr) == len(cube.dimension_ids) - 1:
                        addr = (CHANNEL_TO_AT_ID["value"],) + addr
                    if cube is not None and addr in cube.data:
                        # Only clear if it's a rule cell (has rule body or rule) and not a hardnumber override
                        if addr not in cube.user_override_addrs:
                            has_rule_body = self._ws.find_anchored_rule(cube_id, addr) is not None
                            has_rule = self._ws.find_rule(cube_id, addr, cube.dimension_ids) is not None
                            if has_rule_body or has_rule:
                                is_rule_cell = True
                                cube.set(addr, None)
            # CRITICAL: For rule cells, also mark all precedents as dirty.
            # This ensures that during parallel recompute, when the rule is evaluated,
            # all its dependencies will have up-to-date values.
            if is_rule_cell:
                preds = self._dep_graph.precedents_of(key)
                for pred_key in preds:
                    if pred_key not in seen:
                        queue.append(pred_key)
            deps = self._dep_graph.dependents_of(key)
            for dep_key in deps:
                queue.append(dep_key)
        # Debug output removed

    def _invalidate_cell_node(self, cube_id: str, addr: tuple[str, ...]) -> None:
        """Invalidate a cell node and all its dependents.

        Uses unified key format with @ dimension. The @ dimension is treated
        as just another dimension - @.value, @.fill, etc. are separate nodes.
        """
        if not self._dep_tracking_enabled:
            self._cell_cache.clear()
            return

        cube = self._ws.cubes.get(cube_id)
        # Normalize address to full format first (ensures @.value prefix if needed)
        if cube is not None:
            addr = _normalize_addr_for_cube(cube, addr)

        # Unified key format - always includes @ dimension
        node_key = self._cell_node_key(cube_id, addr)

        # Mark this node and all its dependents as dirty
        self._dep_graph.mark_dirty(node_key)
        self._mark_node_and_dependents_dirty(node_key)

        # Use dependency graph to invalidate dependent cells
        # Each @ channel is a separate node - dependencies are explicit
        if cube is not None:
            dependents = self._dep_graph.dependents_of(node_key)
            for dep_key in dependents:
                # Parse cell node keys and clear their cached values
                if dep_key[:7] == "cell::":
                    parsed = self._parse_cell_node_key(dep_key)
                    if parsed is not None:
                        dep_cube_id, dep_addr = parsed
                        dep_cube = self._ws.cubes.get(dep_cube_id)
                        if dep_cube is not None and dep_addr not in dep_cube.user_override_addrs:
                            dep_cube.set(dep_addr, None)
                            # Clear from cell cache
                            dead_keys = [key for key in self._cell_cache
                                         if key[0] == dep_cube_id and key[2] == dep_addr]
                            for key in dead_keys:
                                self._cell_cache.pop(key, None)

                # Also handle aggregation function nodes (slice::MIN, slice::MAX, etc.)
                if dep_key[:7] == "slice::" or dep_key[:6] == "func::":
                    self._dep_graph.mark_dirty(dep_key)
                    # Find cells that depend on this aggregation node
                    func_dependents = self._dep_graph.dependents_of(dep_key)
                    for func_dep_key in func_dependents:
                        if func_dep_key[:7] == "cell::":
                            parsed = self._parse_cell_node_key(func_dep_key)
                            if parsed is not None:
                                func_cube_id, func_addr = parsed
                                func_cube = self._ws.cubes.get(func_cube_id)
                                if func_cube is not None and func_addr not in func_cube.user_override_addrs:
                                    func_cube.set(func_addr, None)
                                    dead_keys = [key for key in self._cell_cache
                                                 if key[0] == func_cube_id and key[2] == func_addr]
                                    for key in dead_keys:
                                        self._cell_cache.pop(key, None)

        # Also clear cube cell data for rule cells to force recompute
        if cube is not None and addr in cube.data:
            if addr not in cube.user_override_addrs:
                has_rule_body = self._ws.find_anchored_rule(cube_id, addr) is not None
                has_rule = self._ws.find_rule(cube_id, addr, cube.dimension_ids) is not None
                if has_rule_body or has_rule:
                    cube.set(addr, None)

    def _invalidate_cube(self, cube_id: str) -> None:
        if not self._dep_tracking_enabled:
            self._cell_cache.clear()
            return
        prefix = f"cell::{cube_id}::"
        for node in list(self._dep_graph.nodes()):
            if node.key.startswith(prefix):
                self._mark_node_and_dependents_dirty(node.key)
        # Also clear cube data for rule cells to force recompute
        # This is needed for cross-cube computed values like EquityValue
        cube = self._ws.cubes.get(cube_id)
        if cube is not None and cube.data:
            for addr in list(cube.data.keys()):
                # Check if this address has a rule body or rule
                has_rule_body = self._ws.find_anchored_rule(cube_id, addr) is not None
                has_rule = self._ws.find_rule(cube_id, addr, cube.dimension_ids) is not None
                if has_rule_body or has_rule:
                    # Not a hardnumber - clear to force recompute
                    if addr not in cube.user_override_addrs:
                        cube.set(addr, None)

    def _invalidate_slice_dependent_rules(self) -> None:
        """Mark all slice/function nodes and their transitive dependents as dirty.

        Called after outline mutations (item insertion, grouping, etc.) so that
        SUM/AVG/MIN/MAX and other aggregation rules re-evaluate on next
        access and discover any newly-added or re-ordered cells.
        """
        self._slice_cache.clear()
        self._function_cache.clear()
        for node in list(self._dep_graph.nodes()):
            if node.key.startswith(("slice::", "func::")):
                self._mark_node_and_dependents_dirty(node.key)

    # Hierarchy functions that depend on outline structure
    _HIERARCHY_FUNCS = {"ANCE", "DESC", "PARE", "CHIL", "PEER", "SIBL"}

    def _rule_has_hierarchy_funcs(self, expr: str) -> bool:
        """Check if rule contains hierarchy function calls."""
        if not expr:
            return False
        expr_upper = expr.upper()
        return any(func in expr_upper for func in self._HIERARCHY_FUNCS)

    # Volatile functions that should recompute on any cell change (matches Excel behavior)
    _VOLATILE_FUNCS = {"RAND", "RANDBETWEEN", "XLS_RAND", "XLS_RANDBETWEEN", "XLS_OFFSET"}

    def _track_volatile_cell(self, cube_id: str, addr: tuple[str, ...], expression: str) -> None:
        """Track cells containing volatile functions that need re-eval on any change."""
        if not expression:
            return
        expr_upper = expression.upper()
        volatile_funcs = set()
        for func in self._VOLATILE_FUNCS:
            if func in expr_upper:
                volatile_funcs.add(func)
        if volatile_funcs:
            self._volatile_cells[(cube_id, addr)] = volatile_funcs
        else:
            self._volatile_cells.pop((cube_id, addr), None)

    def _rename_item_in_outline(self, dim, item_id: str, new_name: str) -> None:
        """Invalidate outline cache so next read rebuilds with updated item labels.

        rebuild_outline_from_graph derives ITEM_REF labels from dim.items,
        so invalidating the cache is sufficient after renaming an item.
        """
        dim.invalidate_outline_cache()

    def _rule_refs_dimension(self, expr: str, dim: Any) -> bool:
        """Check if rule references any item in the given dimension."""
        if not expr:
            return False
        expr_upper = expr.upper()
        # Check for any item name from this dimension
        for item in dim.items:
            if item.name.upper() in expr_upper:
                return True
        return False

    def _invalidate_hierarchy_rules_for_dim(self, dim_id: str) -> None:
        """Invalidate all rules with hierarchy functions in cubes using this dimension.
        
        ANCE, DESC, PARE, CHIL, PEER, SIBL functions depend on outline structure.
        When any item/group in a dimension changes, ALL hierarchy rules in cubes
        using that dimension must be invalidated, since any hierarchy traversal could
        reach the changed item even if not explicitly referenced in the rule text.
        """
        if not self._dep_tracking_enabled:
            self._cell_cache.clear()
            return
        
        # Find all cubes that use this dimension
        affected_cubes = [c for c in self._ws.cubes.values() if dim_id in c.dimension_ids]
        if not affected_cubes:
            return
        
        # Invalidate all hierarchy rules in affected cubes
        for cube in affected_cubes:
            for rid, r in self._ws.rules.items():
                if r.cube_id != cube.id:
                    continue
                if r.is_anchored and r.addr_mask is not None and self._rule_has_hierarchy_funcs(r.expression):
                    self._invalidate_cell_node(r.cube_id, r.addr_mask)

            # Check rules - if any rule has hierarchy funcs, invalidate whole cube
            for rid, r in self._ws.rules.items():
                if r.cube_id == cube.id and self._rule_has_hierarchy_funcs(r.expression):
                    self._invalidate_cube(cube.id)
                    break

    def _invalidate_all_rule_cells(self) -> None:
        """Mark all rule cells in all cubes as dirty.
        
        This is needed for cross-cube references where a cell in cube A
        references a cell in cube B (e.g., mref:BS::BS.WorkingCapital).
        When the referenced cell changes, we need to recalculate all
        rules that might reference it.
        """
        if not self._dep_tracking_enabled:
            self._cell_cache.clear()
            return
        # Mark ALL cell nodes as dirty - aggressive but ensures correctness
        for node in list(self._dep_graph.nodes()):
            if node.key.startswith("cell::"):
                self._dep_graph.mark_dirty(node.key)
                self._clear_cached_node(node.key)
        # Also invalidate all cubes to catch cross-cube computed values
        for cube in self._ws.cubes.values():
            self._invalidate_cube(cube.id)

    def _on_cell_value_changed(self, cube_id: str, addr: tuple[str, ...]) -> None:
        # Invalidate the changed cell and all its dependents via dependency graph
        # This properly handles both same-cube and cross-cube references (e.g., mref:BS::BS.WorkingCapital)
        # without unnecessarily invalidating unrelated rules
        self._invalidate_cell_node(cube_id, addr)
        # Phase 4: emit event for direct cell mutation
        cube = self._ws.cubes.get(cube_id)
        if cube is not None:
            value = cube.get(addr)
            self._publish_event(EVENT_CELL_UPDATED, {
                "cube_id": cube_id,
                "addr": addr,
                "value": value,
                "display_value": str(value) if value is not None else "",
                "changed_fields": ["value"],
            })

    def _cell_node_key(self, cube_id: str, addr: tuple[str, ...]) -> str:
        """Build cell node key for dependency graph.

        Uses unified format that ALWAYS includes the @ dimension.
        The @ dimension is treated as just another dimension, not a special case.
        """
        # Normalize to full address for consistent dependency graph keys
        cube = self._ws.cubes.get(cube_id)
        if cube is not None:
            addr = _normalize_addr_for_cube(cube, addr)
        addr_token = ",".join(addr)
        return f"cell::{cube_id}::{addr_token}"

    def _parse_cell_node_key(self, key: str) -> tuple[str, tuple[str, ...]] | None:
        """Parse a cell node key back into (cube_id, addr).

        Expects unified format: cell::cube_id::@.channel,item1,item2,...
        """
        if not key.startswith("cell::"):
            return None
        parts = key.split("::", 2)
        if len(parts) != 3:
            return None
        cube_id = parts[1]
        addr = tuple(parts[2].split(",")) if parts[2] else tuple()
        return cube_id, addr

    def list_precedents(self, cube_id: str, addr: tuple[str, ...]) -> list[tuple[str, tuple[str, ...]]]:
        if not self._dep_tracking_enabled:
            return []
        node_key = self._cell_node_key(cube_id, addr)
        out: list[tuple[str, tuple[str, ...]]] = []
        cube = self._ws.cubes.get(cube_id)
        for key in self._dep_graph.precedents_of(node_key):
            parsed = self._parse_cell_node_key(key)
            if parsed is not None:
                cid, parsed_addr = parsed
                # Normalize to full address format (with @.value prefix)
                parsed_cube = self._ws.cubes.get(cid)
                if parsed_cube is not None:
                    parsed_addr = _normalize_addr_for_cube(parsed_cube, parsed_addr)
                out.append((cid, parsed_addr))
        return out

    def list_dependents(self, cube_id: str, addr: tuple[str, ...]) -> list[tuple[str, tuple[str, ...]]]:
        if not self._dep_tracking_enabled:
            return []
        node_key = self._cell_node_key(cube_id, addr)
        out: list[tuple[str, tuple[str, ...]]] = []
        for key in self._dep_graph.dependents_of(node_key):
            parsed = self._parse_cell_node_key(key)
            if parsed is not None:
                cid, parsed_addr = parsed
                # Normalize to full address format (with @.value prefix)
                parsed_cube = self._ws.cubes.get(cid)
                if parsed_cube is not None:
                    parsed_addr = _normalize_addr_for_cube(parsed_cube, parsed_addr)
                out.append((cid, parsed_addr))
        return out

    def dependency_metrics(self) -> dict[str, int]:
        out = dict(self._dep_metrics)
        out["rule_eval_count"] = int(self._rule_eval_profile_totals.get("eval_count", 0.0))
        out["rule_eval_ms_total"] = int(self._rule_eval_profile_totals.get("eval_ms_total", 0.0))
        out["rule_slow_eval_count"] = int(self._rule_eval_profile_totals.get("slow_eval_count", 0.0))
        out["mt_enabled"] = int(self._multithread_recompute_enabled)
        out["mt_workers"] = int(self._multithread_recompute_workers)
        return out

    def reset_rule_eval_profile(self) -> None:
        self._rule_eval_profile_totals = {
            "eval_count": 0.0,
            "eval_ms_total": 0.0,
            "slow_eval_count": 0.0,
        }
        self._rule_eval_profile_by_expr = {}
        self._rule_eval_profile_by_fn = {}

    def reset_profiler_snapshot(self) -> None:
        """Reset counters shown in the Performance Watch profiler snapshot."""
        self._reset_dep_metrics()
        self.reset_rule_eval_profile()

    def rule_eval_profile_snapshot(self, *, top_n: int = 10) -> dict[str, Any]:
        eval_count = int(self._rule_eval_profile_totals.get("eval_count", 0.0))
        eval_ms_total = float(self._rule_eval_profile_totals.get("eval_ms_total", 0.0))
        slow_eval_count = int(self._rule_eval_profile_totals.get("slow_eval_count", 0.0))

        top_limit = max(1, int(top_n))
        expression_rows = [
            {
                "expression": expr,
                "count": int(stats.get("count", 0.0)),
                "total_ms": round(float(stats.get("total_ms", 0.0)), 3),
                "max_ms": round(float(stats.get("max_ms", 0.0)), 3),
                "slow_count": int(stats.get("slow_count", 0.0)),
            }
            for expr, stats in self._rule_eval_profile_by_expr.items()
        ]
        top_expressions_by_time = sorted(
            expression_rows,
            key=lambda row: (row["total_ms"], row["count"]),
            reverse=True,
        )[:top_limit]
        top_expressions_by_count = sorted(
            expression_rows,
            key=lambda row: (row["count"], row["total_ms"]),
            reverse=True,
        )[:top_limit]

        top_functions = sorted(
            (
                {
                    "function": fn,
                    "count": int(stats.get("count", 0.0)),
                    "total_ms": round(float(stats.get("total_ms", 0.0)), 3),
                    "max_ms": round(float(stats.get("max_ms", 0.0)), 3),
                }
                for fn, stats in self._rule_eval_profile_by_fn.items()
            ),
            key=lambda row: row["total_ms"],
            reverse=True,
        )[: max(1, int(top_n))]

        avg_ms = (eval_ms_total / eval_count) if eval_count > 0 else 0.0
        top_by_time_totals = {
            "count_total": int(sum(int(row.get("count", 0)) for row in top_expressions_by_time)),
            "eval_ms_total": round(sum(float(row.get("total_ms", 0.0)) for row in top_expressions_by_time), 3),
        }
        top_by_count_totals = {
            "count_total": int(sum(int(row.get("count", 0)) for row in top_expressions_by_count)),
            "eval_ms_total": round(sum(float(row.get("total_ms", 0.0)) for row in top_expressions_by_count), 3),
        }
        return {
            "eval_count": eval_count,
            "eval_ms_total": round(eval_ms_total, 3),
            "eval_ms_avg": round(avg_ms, 3),
            "slow_eval_count": slow_eval_count,
            # Backward-compat alias: this list is ordered by total eval time.
            "top_expressions": top_expressions_by_time,
            "top_expressions_by_time": top_expressions_by_time,
            "top_expressions_by_count": top_expressions_by_count,
            "top_expressions_by_time_totals": top_by_time_totals,
            "top_expressions_by_count_totals": top_by_count_totals,
            "top_functions": top_functions,
        }

    def _record_rule_eval_profile(self, expression: str, elapsed_ms: float) -> None:
        self._rule_eval_profile_totals["eval_count"] = float(self._rule_eval_profile_totals.get("eval_count", 0.0)) + 1.0
        self._rule_eval_profile_totals["eval_ms_total"] = float(
            self._rule_eval_profile_totals.get("eval_ms_total", 0.0)
        ) + float(elapsed_ms)
        slow = elapsed_ms > (SLOW_LOG_THRESHOLD * 1000.0)
        if slow:
            self._rule_eval_profile_totals["slow_eval_count"] = float(
                self._rule_eval_profile_totals.get("slow_eval_count", 0.0)
            ) + 1.0

        expr_stats = self._rule_eval_profile_by_expr.setdefault(
            expression,
            {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0, "slow_count": 0.0},
        )
        expr_stats["count"] += 1.0
        expr_stats["total_ms"] += float(elapsed_ms)
        expr_stats["max_ms"] = max(float(expr_stats.get("max_ms", 0.0)), float(elapsed_ms))
        if slow:
            expr_stats["slow_count"] = float(expr_stats.get("slow_count", 0.0)) + 1.0

        for fn_name in set(re.findall(r"([A-Z_][A-Z0-9_]*)\s*\(", expression.upper())):
            fn_stats = self._rule_eval_profile_by_fn.setdefault(
                fn_name,
                {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0},
            )
            fn_stats["count"] += 1.0
            fn_stats["total_ms"] += float(elapsed_ms)
            fn_stats["max_ms"] = max(float(fn_stats.get("max_ms", 0.0)), float(elapsed_ms))

    def request_cancel(self) -> None:
        """Request cancellation of the current long-running calculation."""
        self._cancel_requested = True
        print("[ENGINE] Cancellation requested")

    def is_cancel_requested(self) -> bool:
        """Check if cancellation has been requested."""
        return self._cancel_requested

    def reset_cancel(self) -> None:
        """Reset the cancellation flag."""
        self._cancel_requested = False

    def enable_multithread_recompute(self, enabled: bool = True, *, max_workers: int | None = None) -> None:
        self._multithread_recompute_enabled = bool(enabled)
        if max_workers is not None:
            new_w = max(1, int(max_workers))
            # Recreate persistent pool if worker count changed
            if (
                self._reuse_worker_pool
                and self._worker_pool is not None
                and new_w != self._multithread_recompute_workers
            ):
                self._worker_pool.shutdown(wait=False)
                self._worker_pool = None
            self._multithread_recompute_workers = new_w
        if not self._multithread_recompute_enabled and self._worker_pool is not None:
            self._worker_pool.shutdown(wait=False)
            self._worker_pool = None

    def _multithread_recompute_config(self) -> dict[str, int]:
        return {
            "enabled": int(self._multithread_recompute_enabled),
            "max_workers": int(self._multithread_recompute_workers),
        }

    def _reset_dep_metrics(self) -> None:
        self._dep_metrics = {
            "slice_hits": 0,
            "slice_misses": 0,
            "func_hits": 0,
            "func_misses": 0,
            "mt_parallel_runs": 0,
            "mt_parallel_nodes": 0,
            "mt_parallel_frontiers": 0,
            "mt_last_run_ms": 0,
            "mt_last_run_nodes": 0,
            "mt_last_run_frontiers": 0,
            "mt_last_run_max_frontier": 0,
        }

    def dirty_count(self) -> int:
        """Return the number of dirty nodes currently in the dependency graph."""
        return len(self._dep_graph.dirty_keys())

    def has_dirty_nodes(self) -> bool:
        """Return True if any dependency-graph node is currently dirty."""
        return any(self._dep_graph.dirty_keys())

    def recompute_dirty_nodes(self, *, include_all: bool = False, max_nodes: int | None = None, mode: str | None = None) -> int:
        """Re-evaluate dirty dependency graph nodes.

        When ``include_all`` is True, we mark every known node as dirty before
        recomputing.  This is useful for a full-workspace recalculation (e.g.
        Excel-style F9) so that even hidden views/cubes pick up the latest
        driver edits.

        *mode* controls which implementation is used:
        - ``None`` (default): ``_frontier_serial_recompute`` when MT is off;
          ``_frontier_auto_recompute`` when MT is on.
        - ``"legacy_serial"``: the original topo-sort serial loop.
        - ``"parallel_forced"``: always attempt parallel scheduling.
        """
        # Prevent recursive calls that could cause freezes
        if getattr(self, '_recomputing', False):
            return 0

        # Enable tracking temporarily if not enabled, so we can recompute
        old_tracking = self._dep_tracking_enabled
        if not self._dep_tracking_enabled:
            self._dep_tracking_enabled = True

        self._recomputing = True
        self._recompute_counts.clear()

        try:
            if include_all:
                for node in list(self._dep_graph.nodes()):
                    self._clear_cached_node(node.key)
                    self._dep_graph.mark_dirty(node.key)

            if mode == "legacy_serial":
                return self._legacy_serial_recompute(max_nodes=max_nodes)
            elif mode == "parallel_forced":
                return self._frontier_parallel_forced(max_nodes=max_nodes)
            elif self._multithread_recompute_enabled:
                processed_parallel = self._recompute_dirty_nodes_parallel(max_nodes=max_nodes)
                if processed_parallel > 0:
                    return processed_parallel
                # MT path returned 0 (e.g. XLS_OFFSET) — fall through to serial
                return self._frontier_serial_recompute(max_nodes=max_nodes)

            # Default when MT disabled: canonical serial frontier loop
            return self._frontier_serial_recompute(max_nodes=max_nodes)
        finally:
            self._recomputing = False
            self._dep_tracking_enabled = old_tracking

    def _legacy_serial_recompute(self, *, max_nodes: int | None = None) -> int:
        """Original serial recompute loop (topological sort, single pass)."""
        dirty_keys = list(self._dep_graph.dirty_keys())
        dirty_set = set(dirty_keys)
        indegree: dict[str, int] = {}
        dependents: dict[str, list[str]] = {}
        for key in dirty_keys:
            preds = [k for k in self._dep_graph.precedents_of(key) if k in dirty_set]
            indegree[key] = len(preds)
            dependents[key] = [k for k in self._dep_graph.dependents_of(key) if k in dirty_set]

        ready = [key for key in dirty_keys if indegree.get(key, 0) == 0]
        processed = 0
        while ready:
            key = ready.pop(0)
            processed += 1
            
            if key.startswith("cell::"):
                parsed = self._parse_cell_node_key(key)
                if parsed is not None:
                    cube_id, addr = parsed
                    cube = self._ws.cubes.get(cube_id)
                    if cube is not None:
                        if cube.is_user_override(addr):
                            pass
                        else:
                            has_rule_body = self._ws.find_anchored_rule(cube_id, addr) is not None
                            has_rule = self._ws.find_rule(cube_id, addr, cube.dimension_ids) is not None
                            if has_rule_body or has_rule:
                                try:
                                    cube.set(addr, None)
                                    result = self._get_cell_by_addr(cube, addr)
                                    cube.set(addr, result)
                                except Exception:
                                    logging.exception("Failed to recompute node %s", key)
            
            self._dep_graph.clear_dirty(key)
            for dep_key in dependents.get(key, []):
                indegree[dep_key] = max(0, indegree.get(dep_key, 0) - 1)
                if indegree[dep_key] == 0:
                    ready.append(dep_key)
            if max_nodes is not None and processed >= max_nodes:
                break

        from lib_openm.outline_graph_bridge import sync_workspace_graph_to_outline
        sync_workspace_graph_to_outline(self._ws)
        return processed

    def _frontier_serial_recompute(self, *, max_nodes: int | None = None) -> int:
        """Canonical serial recompute using frontier-based scheduling.

        This is the default recompute path when multithreading is disabled,
        and the fallback path when the parallel gate rejects a frontier.

        No ThreadPoolExecutor is created; evaluation runs on the calling thread.
        """
        dirty_keys = [key for key in self._dep_graph.dirty_keys() if key.startswith("cell::")]
        if not dirty_keys:
            return 0

        dirty_set = set(dirty_keys)
        indegree: dict[str, int] = {}
        dependents: dict[str, list[str]] = {}
        for key in dirty_keys:
            preds = [k for k in self._dep_graph.precedents_of(key) if k in dirty_set]
            indegree[key] = len(preds)
            dependents[key] = [k for k in self._dep_graph.dependents_of(key) if k in dirty_set]

        ready = [key for key in dirty_keys if indegree.get(key, 0) == 0]
        if not ready:
            return 0

        processed = 0
        while ready:
            if max_nodes is not None and processed >= max_nodes:
                break

            frontier = list(ready)
            ready = []

            # Evaluate all cells in the current frontier
            for key in frontier:
                if key.startswith("cell::"):
                    parsed = self._parse_cell_node_key(key)
                    if parsed is not None:
                        cube_id, addr = parsed
                        cube = self._ws.cubes.get(cube_id)
                        if cube is not None and not cube.is_user_override(addr):
                            has_rule_body = self._ws.find_anchored_rule(cube_id, addr) is not None
                            has_rule = self._ws.find_rule(cube_id, addr, cube.dimension_ids) is not None
                            if has_rule_body or has_rule:
                                try:
                                    cube.set(addr, None)
                                    result = self._get_cell_by_addr(cube, addr)
                                    cube.set(addr, result)
                                except Exception:
                                    logging.exception("Failed to recompute node %s", key)

            # Clear dirty flags and advance to next frontier
            for key in frontier:
                processed += 1
                self._dep_graph.clear_dirty(key)
                for dep_key in dependents.get(key, []):
                    indegree[dep_key] = max(0, indegree.get(dep_key, 0) - 1)
                    if indegree[dep_key] == 0:
                        ready.append(dep_key)

        from lib_openm.outline_graph_bridge import sync_workspace_graph_to_outline
        sync_workspace_graph_to_outline(self._ws)
        return processed

    def _recompute_dirty_nodes_parallel(self, *, max_nodes: int | None = None) -> int:
        """Backward-compatible wrapper.  Delegates to ``_frontier_auto_recompute``."""
        return self._frontier_auto_recompute(max_nodes=max_nodes)

    def _frontier_auto_recompute(self, *, max_nodes: int | None = None) -> int:
        """Gated cube-aware parallel recompute.

        Falls back to ``_frontier_serial_recompute`` when any of the
        conservative gate conditions are not met.
        """
        all_dirty = self._dep_graph.dirty_keys()
        dirty_keys = [key for key in all_dirty if key.startswith("cell::")]
        print(f"[MT] _frontier_auto_recompute: all_dirty={len(all_dirty)} cell_dirty={len(dirty_keys)} mt_enabled={self._multithread_recompute_enabled}")
        if not dirty_keys:
            print("[MT]   -> returning 0: no dirty cell keys")
            return 0

        # XLS_OFFSET check
        for key in dirty_keys:
            parsed = self._parse_cell_node_key(key)
            if parsed is None:
                continue
            cube_id, addr = parsed
            cube = self._ws.cubes.get(cube_id)
            if cube is None:
                continue
            rule = self._ws.find_anchored_rule(cube_id, addr)
            if rule is None:
                rule = self._ws.find_rule(cube_id, addr, cube.dimension_ids)
                expr_raw = rule.expression if rule is not None else ""
            else:
                expr_raw = rule.expression
            if "XLS_OFFSET(" in str(expr_raw).upper():
                print(f"[MT]   -> returning 0: XLS_OFFSET found in {cube_id}@{addr}")
                return 0

        # Bootstrap: evaluate cells without precedents serially on the main
        # thread (tracking ON) so the dependency graph gets built.
        bootstrap_count = 0
        for key in list(dirty_keys):
            parsed = self._parse_cell_node_key(key)
            if parsed is None:
                continue
            cube_id, addr = parsed
            cube = self._ws.cubes.get(cube_id)
            if cube is None:
                continue
            has_rule_body = self._ws.find_anchored_rule(cube_id, addr) is not None
            has_rule = self._ws.find_rule(cube_id, addr, cube.dimension_ids) is not None
            if (has_rule_body or has_rule) and key not in self._tracked_nodes:
                print(f"[MT]   -> bootstrapping {key}")
                addr = _normalize_addr_for_cube(cube, addr)
                try:
                    with self._cube_locks.setdefault(cube_id, threading.Lock()):
                        result = self._get_cell_by_addr(cube, addr)
                        if not cube.is_user_override(addr):
                            cube.set(addr, result)
                except Exception:
                    logging.exception("Failed to bootstrap node %s", key)
                bootstrap_count += 1

        # Rebuild dirty list after bootstrap
        all_dirty = self._dep_graph.dirty_keys()
        dirty_keys = [key for key in all_dirty if key.startswith("cell::")]
        print(f"[MT]   -> bootstrap done: {bootstrap_count} cells, remaining dirty={len(dirty_keys)}")
        if not dirty_keys:
            return bootstrap_count

        dirty_set = set(dirty_keys)
        indegree: dict[str, int] = {}
        dependents: dict[str, list[str]] = {}
        for key in dirty_keys:
            preds = [k for k in self._dep_graph.precedents_of(key) if k in dirty_set]
            indegree[key] = len(preds)
            dependents[key] = [k for k in self._dep_graph.dependents_of(key) if k in dirty_set]

        ready = [key for key in dirty_keys if indegree.get(key, 0) == 0]
        if not ready:
            return 0

        max_workers = max(1, int(self._multithread_recompute_workers))
        min_parallel_frontier_size = getattr(
            self, "_min_parallel_frontier_size", 1_000_000_000
        )

        # Early-exit simulation: compute max frontier size
        _sim_ready = list(ready)
        _sim_indegree = dict(indegree)
        _sim_max_frontier = len(_sim_ready)
        _sim_processed: set[str] = set()
        while _sim_ready:
            for key in _sim_ready:
                _sim_processed.add(key)
                for dep_key in dependents.get(key, []):
                    _sim_indegree[dep_key] = max(0, _sim_indegree.get(dep_key, 0) - 1)
            _sim_ready = [k for k in dirty_keys if _sim_indegree.get(k, 0) == 0 and k not in _sim_processed]
            if len(_sim_ready) > _sim_max_frontier:
                _sim_max_frontier = len(_sim_ready)

        _use_serial_only = _sim_max_frontier < min_parallel_frontier_size
        if _use_serial_only:
            print(f"[MT]   -> early serial: max_frontier={_sim_max_frontier} < threshold={min_parallel_frontier_size}")
            return self._frontier_serial_recompute(max_nodes=max_nodes)

        # Conservative parallel gate
        from collections import Counter
        _cube_counts = Counter()
        for _key in dirty_keys:
            _parsed = self._parse_cell_node_key(_key)
            if _parsed:
                _cube_counts[_parsed[0]] += 1
        _cube_count = len(_cube_counts)
        _max_cells_per_cube = max(_cube_counts.values()) if _cube_counts else 0
        _dominant_cube_ratio = _max_cells_per_cube / len(dirty_keys) if dirty_keys else 0.0
        _effective_workers = min(max_workers, _cube_count)

        print(f"[MT]   -> cube stats: cubes={_cube_count} max_per_cube={_max_cells_per_cube} dominant_ratio={_dominant_cube_ratio:.3f} effective_workers={_effective_workers}")

        # Gate conditions
        _dominant_threshold = 0.95
        if len(dirty_keys) < min_parallel_frontier_size:
            print(f"[MT]   -> gate serial: frontier_size={len(dirty_keys)} < threshold={min_parallel_frontier_size}")
            return self._frontier_serial_recompute(max_nodes=max_nodes)
        if _cube_count < 2:
            print(f"[MT]   -> gate serial: cube_count={_cube_count} < 2")
            return self._frontier_serial_recompute(max_nodes=max_nodes)
        if _dominant_cube_ratio > _dominant_threshold:
            print(f"[MT]   -> gate serial: dominant_ratio={_dominant_cube_ratio:.3f} > threshold={_dominant_threshold}")
            return self._frontier_serial_recompute(max_nodes=max_nodes)

        # Proceed with parallel scheduling
        return self._frontier_parallel_recompute(
            dirty_keys=dirty_keys,
            dirty_set=dirty_set,
            indegree=indegree,
            dependents=dependents,
            ready=ready,
            max_workers=max_workers,
            effective_workers=_effective_workers,
            bootstrap_count=bootstrap_count,
            max_nodes=max_nodes,
        )

    def _frontier_parallel_forced(self, *, max_nodes: int | None = None) -> int:
        """Benchmark/debug mode: forces parallel scheduling regardless of gate."""
        all_dirty = self._dep_graph.dirty_keys()
        dirty_keys = [key for key in all_dirty if key.startswith("cell::")]
        print(f"[MT] _frontier_parallel_forced: all_dirty={len(all_dirty)} cell_dirty={len(dirty_keys)}")
        if not dirty_keys:
            return 0

        # Bootstrap
        bootstrap_count = 0
        for key in list(dirty_keys):
            parsed = self._parse_cell_node_key(key)
            if parsed is None:
                continue
            cube_id, addr = parsed
            cube = self._ws.cubes.get(cube_id)
            if cube is None:
                continue
            has_rule_body = self._ws.find_anchored_rule(cube_id, addr) is not None
            has_rule = self._ws.find_rule(cube_id, addr, cube.dimension_ids) is not None
            if (has_rule_body or has_rule) and key not in self._tracked_nodes:
                addr = _normalize_addr_for_cube(cube, addr)
                try:
                    with self._cube_locks.setdefault(cube_id, threading.Lock()):
                        result = self._get_cell_by_addr(cube, addr)
                        if not cube.is_user_override(addr):
                            cube.set(addr, result)
                except Exception:
                    logging.exception("Failed to bootstrap node %s", key)
                bootstrap_count += 1

        all_dirty = self._dep_graph.dirty_keys()
        dirty_keys = [key for key in all_dirty if key.startswith("cell::")]
        if not dirty_keys:
            return bootstrap_count

        dirty_set = set(dirty_keys)
        indegree: dict[str, int] = {}
        dependents: dict[str, list[str]] = {}
        for key in dirty_keys:
            preds = [k for k in self._dep_graph.precedents_of(key) if k in dirty_set]
            indegree[key] = len(preds)
            dependents[key] = [k for k in self._dep_graph.dependents_of(key) if k in dirty_set]

        ready = [key for key in dirty_keys if indegree.get(key, 0) == 0]
        if not ready:
            return 0

        max_workers = max(1, int(self._multithread_recompute_workers))
        from collections import Counter
        _cube_counts = Counter()
        for _key in dirty_keys:
            _parsed = self._parse_cell_node_key(_key)
            if _parsed:
                _cube_counts[_parsed[0]] += 1
        _cube_count = len(_cube_counts)
        _effective_workers = min(max_workers, _cube_count)

        return self._frontier_parallel_recompute(
            dirty_keys=dirty_keys,
            dirty_set=dirty_set,
            indegree=indegree,
            dependents=dependents,
            ready=ready,
            max_workers=max_workers,
            effective_workers=_effective_workers,
            bootstrap_count=bootstrap_count,
            max_nodes=max_nodes,
        )

    def _frontier_parallel_recompute(self, *, dirty_keys, dirty_set, indegree, dependents, ready, max_workers, effective_workers, bootstrap_count, max_nodes):
        """Parallel frontier loop with per-cube task grouping."""
        t0 = time.perf_counter()
        processed = 0
        frontiers = 0
        max_frontier_size = 0

        # Timing accumulators (seconds)
        _acc_eval = 0.0
        _acc_lock_wait = 0.0
        _acc_lock_hold = 0.0
        _acc_dep_graph = 0.0
        _acc_serial_fallback = 0.0
        _acc_parallel_submit = 0.0
        _frontier_log: list[dict[str, Any]] = []

        def _eval_node(key: str) -> None:
            nonlocal _acc_eval, _acc_lock_wait, _acc_lock_hold
            _t_eval = time.perf_counter()
            parsed = self._parse_cell_node_key(key)
            if parsed is None:
                return
            cube_id, addr = parsed
            cube = self._ws.cubes.get(cube_id)
            if cube is None:
                return
            addr = _normalize_addr_for_cube(cube, addr)
            has_rule_body = self._ws.find_anchored_rule(cube_id, addr) is not None
            has_rule = self._ws.find_rule(cube_id, addr, cube.dimension_ids) is not None
            self._thread_eval_state.tracking_disabled = True
            try:
                _t_lock = time.perf_counter()
                with self._cube_locks.setdefault(cube_id, threading.Lock()):
                    _t_locked = time.perf_counter()
                    _acc_lock_wait += (_t_locked - _t_lock)
                    if not has_rule_body and not has_rule:
                        return
                    if not cube.is_user_override(addr):
                        cube.set(addr, None)
                    result = self._get_cell_by_addr(cube, addr)
                    if not cube.is_user_override(addr):
                        cube.set(addr, result)
                    _acc_lock_hold += (time.perf_counter() - _t_locked)
                self._tracked_nodes.add(key)
            except Exception:
                logging.exception("Failed to recompute node %s", key)
            finally:
                self._thread_eval_state.tracking_disabled = False
                _acc_eval += (time.perf_counter() - _t_eval)

        def _eval_batch(keys: list[str]) -> None:
            for key in keys:
                _eval_node(key)

        # Pool setup
        _t_pool_start = time.perf_counter()
        pool: Any | None = None
        pool_needs_shutdown = False
        if self._reuse_worker_pool:
            if self._worker_pool is None:
                self._worker_pool = ThreadPoolExecutor(max_workers=max_workers)
            pool = self._worker_pool
            _acc_pool_overhead = time.perf_counter() - _t_pool_start
        else:
            pool = ThreadPoolExecutor(max_workers=max_workers)
            pool_needs_shutdown = True
            _acc_pool_overhead = time.perf_counter() - _t_pool_start

        try:
            while ready:
                if max_nodes is not None and processed >= max_nodes:
                    break

                remaining = None if max_nodes is None else max_nodes - processed
                frontier = list(ready if remaining is None else ready[:remaining])
                ready = [] if remaining is None else ready[remaining:]
                if not frontier:
                    break

                frontiers += 1
                if len(frontier) > max_frontier_size:
                    max_frontier_size = len(frontier)

                from collections import Counter
                _cube_counts = Counter()
                for _key in frontier:
                    _parsed = self._parse_cell_node_key(_key)
                    if _parsed:
                        _cube_counts[_parsed[0]] += 1
                _cube_count = len(_cube_counts)
                _max_cells_per_cube = max(_cube_counts.values()) if _cube_counts else 0
                _dominant_cube_ratio = _max_cells_per_cube / len(frontier) if frontier else 0.0

                _eval_before = _acc_eval
                _lock_wait_before = _acc_lock_wait
                _lock_hold_before = _acc_lock_hold
                _t_frontier_start = time.perf_counter()

                # Even in forced mode, single-cube frontiers are serialised by the
                # per-cube lock — scheduling them as parallel tasks is pure overhead.
                if _cube_count < 2:
                    decision = "serial"
                    _t_fallback = time.perf_counter()
                    for key in frontier:
                        _eval_node(key)
                    _dispatch_ms = 0.0
                    _wait_ms = 0.0
                    _batches = 1
                    _cells_per_batch = len(frontier)
                    _acc_serial_fallback += (time.perf_counter() - _t_fallback)
                else:
                    decision = "parallel"
                    cube_groups: dict[str, list[str]] = {}
                    for key in frontier:
                        _parsed = self._parse_cell_node_key(key)
                        if _parsed:
                            cube_groups.setdefault(_parsed[0], []).append(key)

                    _t_dispatch = time.perf_counter()
                    futures = []
                    for _cube_id, keys in cube_groups.items():
                        futures.append(pool.submit(_eval_batch, keys))
                    _dispatch_ms = (time.perf_counter() - _t_dispatch) * 1000.0

                    _t_wait = time.perf_counter()
                    for fut in futures:
                        fut.result()
                    _wait_ms = (time.perf_counter() - _t_wait) * 1000.0

                    _batches = len(futures)
                    _cells_per_batch = max(len(v) for v in cube_groups.values()) if cube_groups else 0
                    _acc_parallel_submit += (time.perf_counter() - _t_dispatch)

                _eval_ms = (_acc_eval - _eval_before) * 1000.0
                _lock_wait_ms = (_acc_lock_wait - _lock_wait_before) * 1000.0
                _lock_hold_ms = (_acc_lock_hold - _lock_hold_before) * 1000.0
                _frontier_total_ms = (time.perf_counter() - _t_frontier_start) * 1000.0

                _t_dep = time.perf_counter()
                for key in frontier:
                    processed += 1
                    self._dep_graph.clear_dirty(key)
                    for dep_key in dependents.get(key, []):
                        indegree[dep_key] = max(0, indegree.get(dep_key, 0) - 1)
                        if indegree[dep_key] == 0:
                            ready.append(dep_key)
                _commit_ms = (time.perf_counter() - _t_dep) * 1000.0
                _acc_dep_graph += (_commit_ms / 1000.0)

                _frontier_log.append({
                    "frontier_size": len(frontier),
                    "decision": decision,
                    "batches": _batches,
                    "cells_per_batch": _cells_per_batch,
                    "cube_count": _cube_count,
                    "max_cells_per_cube": _max_cells_per_cube,
                    "dominant_cube_ratio": round(_dominant_cube_ratio, 3),
                    "effective_workers": effective_workers,
                    "dispatch_ms": round(_dispatch_ms, 3),
                    "wait_ms": round(_wait_ms, 3),
                    "eval_ms": round(_eval_ms, 3),
                    "lock_wait_ms": round(_lock_wait_ms, 3),
                    "lock_hold_ms": round(_lock_hold_ms, 3),
                    "commit_ms": round(_commit_ms, 3),
                    "total_ms": round(_frontier_total_ms, 3),
                })
        finally:
            if pool_needs_shutdown and pool is not None:
                pool.shutdown(wait=False)

        total_processed = processed + bootstrap_count
        dt_ms = int((time.perf_counter() - t0) * 1000.0)
        self._dep_metrics["mt_parallel_runs"] = int(self._dep_metrics.get("mt_parallel_runs", 0)) + 1
        self._dep_metrics["mt_parallel_nodes"] = int(self._dep_metrics.get("mt_parallel_nodes", 0)) + total_processed
        self._dep_metrics["mt_parallel_frontiers"] = int(self._dep_metrics.get("mt_parallel_frontiers", 0)) + frontiers
        self._dep_metrics["mt_last_run_ms"] = dt_ms
        self._dep_metrics["mt_last_run_nodes"] = total_processed
        self._dep_metrics["mt_last_run_frontiers"] = frontiers
        self._dep_metrics["mt_last_run_max_frontier"] = max_frontier_size
        self._dep_metrics["mt_eval_ms"] = int(_acc_eval * 1000.0)
        self._dep_metrics["mt_lock_wait_ms"] = int(_acc_lock_wait * 1000.0)
        self._dep_metrics["mt_lock_hold_ms"] = int(_acc_lock_hold * 1000.0)
        self._dep_metrics["mt_dep_graph_ms"] = int(_acc_dep_graph * 1000.0)
        self._dep_metrics["mt_pool_overhead_ms"] = int(_acc_pool_overhead * 1000.0)
        self._dep_metrics["mt_serial_fallback_ms"] = int(_acc_serial_fallback * 1000.0)
        self._dep_metrics["mt_parallel_submit_ms"] = int(_acc_parallel_submit * 1000.0)
        self._dep_metrics["mt_frontier_log"] = _frontier_log
        print(f"[MT]   -> parallel success: processed={total_processed} (bootstrap={bootstrap_count}) frontiers={frontiers} max_frontier={max_frontier_size} dt={dt_ms}ms")
        return total_processed
    def evaluate_all_cubes_bruteforce(self) -> None:
        """Force evaluation of every cell in every cube (within defined items)."""
        import time
        
        print(f"[ENGINE] evaluate_all_cubes_bruteforce started - {len(self._ws.cubes)} cubes to process")
        total_cells = 0
        total_start = time.perf_counter()
        
        # Keep dependency tracking enabled to record edges during F9
        # This ensures the graph is up-to-date for incremental recalculation
        old_tracking = self._dep_tracking_enabled
        self._dep_tracking_enabled = True
        self._dep_tracking_enabled = True

        for cube_idx, cube in enumerate(self._ws.cubes.values()):
            cube_start = time.perf_counter()
            cube_name = cube.name
            
            if not cube.dimension_ids:
                self._get_cell_by_addr(cube, tuple())
                cube_end = time.perf_counter()
                print(f"[ENGINE]   Cube {cube_idx+1}/{len(self._ws.cubes)} '{cube_name}': no dims, {(cube_end-cube_start)*1000:.1f} ms")
                continue
            dim_items = []
            for dim_id in cube.dimension_ids:
                dim = self._ws.get_dimension(dim_id)
                if dim:
                    dim_items.append([item.id for item in dim.items])
                elif dim_id == "@":
                    # @ dimension always has value channel
                    dim_items.append([CHANNEL_TO_AT_ID["value"]])
                else:
                    dim_items.append([])
            
            expected_cells = 1
            for items in dim_items:
                expected_cells *= len(items)
            
            cells_processed = 0
            cell_start = time.perf_counter()
            last_progress_time = cell_start
            for addr in itertools.product(*dim_items):
                # Check for cancellation request
                if self._cancel_requested:
                    print(f"[ENGINE] Calculation cancelled after {cells_processed:,} cells")
                    raise CalculationCancelledError("Calculation cancelled by user")
                self._get_cell_by_addr(cube, tuple(addr))
                cells_processed += 1
                # Print progress every 10k cells for large cubes
                if cells_processed % 10000 == 0:
                    # Process Qt events to allow Esc key detection
                    from PySide6 import QtWidgets
                    QtWidgets.QApplication.processEvents()
                    elapsed = (time.perf_counter() - cell_start)
                    elapsed_ms = elapsed * 1000
                    cells_per_sec = cells_processed / elapsed if elapsed > 0 else 0
                    remaining_cells = expected_cells - cells_processed
                    eta_seconds = remaining_cells / cells_per_sec if cells_per_sec > 0 else 0
                    eta_mins = eta_seconds / 60
                    
                    # Format ETA nicely
                    if eta_mins > 60:
                        eta_str = f"{eta_mins/60:.1f}h"
                    elif eta_mins > 1:
                        eta_str = f"{eta_mins:.1f}m"
                    else:
                        eta_str = f"{eta_seconds:.0f}s"
                    
                    pct = (cells_processed / expected_cells) * 100
                    print(f"[ENGINE]     ...{cells_processed:,}/{expected_cells:,} cells ({pct:.2f}%), {elapsed_ms:.1f} ms elapsed, ~{eta_str} remaining")
            
            cube_end = time.perf_counter()
            cube_time = (cube_end - cube_start) * 1000
            total_cells += cells_processed
            print(f"[ENGINE]   Cube {cube_idx+1}/{len(self._ws.cubes)} '{cube_name}': {cells_processed} cells in {cube_time:.1f} ms")
        
        total_end = time.perf_counter()
        print(f"[ENGINE] evaluate_all_cubes_bruteforce complete: {total_cells} total cells in {(total_end-total_start)*1000:.1f} ms")
        
        # Restore dependency tracking
        self._dep_tracking_enabled = old_tracking

    def _begin_tracking_node(self, node_key: str) -> None:
        eval_context = self._thread_eval_context()
        pending_precedents = self._thread_pending_precedents()
        parent_key = eval_context[-1] if eval_context else None
        if _DEBUG_ENGINE:
            print(f"[DEBUG DEPS] _begin_tracking_node: ctx_len={len(eval_context)}, node={node_key[:40]}..., parent={parent_key[:40] if parent_key else None}")
        eval_context.append(node_key)
        pending_precedents.setdefault(node_key, set())
        if parent_key and parent_key != node_key:
            pending_precedents.setdefault(parent_key, set()).add(node_key)
            if _DEBUG_ENGINE:
                print(f"[DEBUG DEPS] _begin_tracking_node: {node_key[:40]}... added as precedent to parent {parent_key[:40]}...")

    def _end_tracking_node(self, node_key: str, *, success: bool, had_rule_body: bool) -> None:
        eval_context = self._thread_eval_context()
        pending_precedents = self._thread_pending_precedents()
        precedents = pending_precedents.pop(node_key, set())
        parent_key = eval_context[-2] if len(eval_context) >= 2 else None
        if _DEBUG_ENGINE:
            print(f"[DEBUG DEPS] _end_tracking_node: ctx_len={len(eval_context)}, node={node_key[:40]}..., parent={parent_key[:40] if parent_key else None}, precedents={[p[:30]+'...' for p in precedents]}")
        if success:
            if had_rule_body:
                # For rule cells: commit precedents to graph
                self._dep_graph.replace_precedents(node_key, precedents)
                self._dep_graph.clear_dirty(node_key)
            else:
                # For non-rule cells (hardcoded/slice/function):
                # DO NOT call replace_precedents(node_key, []) - it would wipe out
                # edges added via add_edge (like hardcoded cell -> SUM function).
                # Add edge from this node to parent for dirty propagation up the chain
                if parent_key:
                    self._dep_graph.add_edge(node_key, parent_key)
                # Record precedents for this node so it gets marked dirty when they change
                if precedents:
                    self._dep_graph.replace_precedents(node_key, precedents)
                    if _DEBUG_ENGINE:
                        print(f"[DEBUG DEPS] _end_tracking_node: recorded {[p[:30]+'...' for p in precedents]} for func/slice node {node_key[:40]}...")
                # Propagate this node's precedents up to parent as well
                if parent_key and precedents:
                    pending_precedents.setdefault(parent_key, set()).update(precedents)
                    if _DEBUG_ENGINE:
                        print(f"[DEBUG DEPS] _end_tracking_node: propagated {[p[:30]+'...' for p in precedents]} to parent {parent_key[:40]}...")
                # Clear dirty flag for function/slice nodes after successful eval
                self._dep_graph.clear_dirty(node_key)
        if eval_context and eval_context[-1] == node_key:
            eval_context.pop()

    def _slice_node_key(self, cube_id: str, agg: str, axes: list[list[str]]) -> str:
        axis_tokens: list[str] = []
        for idx, items in enumerate(axes):
            axis_tokens.append(f"{idx}=" + ",".join(items))
        axes_token = "|".join(axis_tokens)
        return f"slice::{agg}::{cube_id}::{axes_token}"

    def _evaluate_slice_node(
        self,
        agg: str,
        cube: Cube,
        axes: list[list[str]],
        compute: Callable[[], Any],
    ) -> Any:
        if not self._dep_tracking_enabled:
            return compute()
        node_key = self._slice_node_key(cube.id, agg, axes)
        if node_key in self._slice_cache and not self._dep_graph.is_dirty(node_key):
            self._dep_metrics["slice_hits"] = self._dep_metrics.get("slice_hits", 0) + 1
            # Even for cache hits, we need to establish dependency edges
            # so that the parent cell depends on this slice node.
            # This ensures that when the slice's precedents change,
            # all dependent cells (not just the first one) are invalidated.
            self._begin_tracking_node(node_key)
            # Slice nodes are not rules - they should propagate precedents to parent
            self._end_tracking_node(node_key, success=True, had_rule_body=False)
            return self._slice_cache[node_key]
        self._dep_metrics["slice_misses"] = self._dep_metrics.get("slice_misses", 0) + 1
        self._begin_tracking_node(node_key)
        success = False
        try:
            result = compute()
            success = True
            self._slice_cache[node_key] = result
            return result
        finally:
            # Slice nodes are not rules - they should propagate precedents to parent
            self._end_tracking_node(node_key, success=success, had_rule_body=False)

    def _function_node_key(self, fn_name: str, call_key: str, base_addr: tuple[str, ...], cube_id: str = "") -> str:
        addr_token = ",".join(base_addr)
        return f"func::{cube_id}::{fn_name}::{addr_token}::{call_key}"

    def _evaluate_function_node(
        self,
        fn_name: str,
        call_key: str,
        base_addr: tuple[str, ...],
        compute: Callable[[], Any],
        cube_id: str = "",
    ) -> Any:
        if not self._dep_tracking_enabled:
            return compute()
        node_key = self._function_node_key(fn_name, call_key, base_addr, cube_id)
        if node_key in self._function_cache and not self._dep_graph.is_dirty(node_key):
            self._dep_metrics["func_hits"] = self._dep_metrics.get("func_hits", 0) + 1
            # Even for cache hits, we need to establish dependency edges
            # so that the parent cell depends on this function node.
            # This ensures that when the function's precedents change,
            # all dependent cells (not just the first one) are invalidated.
            self._begin_tracking_node(node_key)
            # Function nodes are not rules - they should propagate precedents to parent
            self._end_tracking_node(node_key, success=True, had_rule_body=False)
            return self._function_cache[node_key]
        self._dep_metrics["func_misses"] = self._dep_metrics.get("func_misses", 0) + 1
        self._begin_tracking_node(node_key)
        success = False
        try:
            result = compute()
            success = True
            self._function_cache[node_key] = result
            return result
        finally:
            # Function nodes are not rules - they should propagate precedents to parent
            self._end_tracking_node(node_key, success=success, had_rule_body=False)

    def _find_cube_by_name(self, name: str) -> Cube | None:
        lowered = name.strip().lower()
        if not lowered:
            return None
        for cube in self._ws.cubes.values():
            if cube.name.lower() == lowered:
                return cube
        return None

    def find_cube_by_name(self, name: str) -> Cube | None:
        """Public: find a cube by its human-readable name.

        Returns the Cube object, or ``None`` if no match is found.
        Matching is case-insensitive and ignores leading/trailing whitespace.
        """
        return self._find_cube_by_name(name)

    def resolve_cube_id_by_name(self, name: str) -> str | None:
        """Public: resolve a cube name to its stable cube ID.

        Returns the cube's stable ``id``, or ``None`` if no cube with that
        name exists.  Matching is case-insensitive.
        """
        cube = self._find_cube_by_name(name)
        return cube.id if cube is not None else None

    def _resolve_dim_item_ids_for_trace(
        self,
        dim: Dimension,
        item_name: str,
        *,
        max_results: int,
        base_item_id: str | None = None,
    ) -> list[str]:
        token = item_name.strip()
        if not token:
            return []

        def _normalize_bound(bound: str) -> str:
            text = bound.strip()
            if "." in text:
                maybe_dim, maybe_item = text.split(".", 1)
                if maybe_dim.strip().lower() == dim.name.lower() and maybe_item.strip():
                    return maybe_item.strip()
            return text

        def _item_index_by_name(name: str) -> int | None:
            name_lower = name.strip().lower()
            if not name_lower:
                return None
            for idx, item in enumerate(dim.items):
                if item.name.lower() == name_lower or item.id.lower() == name_lower:
                    return idx
            return None

        def _item_index_by_id(item_id: str) -> int | None:
            for idx, item in enumerate(dim.items):
                if item.id == item_id:
                    return idx
            return None

        # Handle sequential accessors
        token_upper = token.upper()
        if token_upper in ("PREV", "NEXT", "THIS", "FIRST", "LAST"):
            if not dim.items:
                return []
            if token_upper == "FIRST":
                return [dim.items[0].id]
            elif token_upper == "LAST":
                return [dim.items[-1].id]
            elif base_item_id is not None:
                current_idx = _item_index_by_id(base_item_id)
                if current_idx is None:
                    return []
                if token_upper == "THIS":
                    return [base_item_id]
                elif token_upper == "PREV":
                    if current_idx > 0:
                        return [dim.items[current_idx - 1].id]
                    return []
                elif token_upper == "NEXT":
                    if current_idx < len(dim.items) - 1:
                        return [dim.items[current_idx + 1].id]
                    return []
            return []

        item_ids: list[str] = []
        if token == "*":
            item_ids = [it.id for it in dim.items]
        elif ".." in token and not token.startswith("$<"):
            start_raw, end_raw = (part.strip() for part in token.split("..", 1))
            if start_raw and end_raw:
                start_idx = _item_index_by_name(_normalize_bound(start_raw))
                end_idx = _item_index_by_name(_normalize_bound(end_raw))
                if start_idx is not None and end_idx is not None and dim.items:
                    rng = range(start_idx, end_idx + 1) if start_idx <= end_idx else range(end_idx, start_idx + 1)
                    item_ids = [dim.items[i].id for i in rng]
        else:
            idx = _item_index_by_name(token)
            if idx is not None:
                item_ids = [dim.items[idx].id]
            else:
                # Try to resolve as outline group label
                group_ids = self._find_outline_group_item_ids(dim, token.lower())
                if group_ids is not None:
                    item_ids = group_ids

        return item_ids[:max_results]

    def _find_outline_group_item_ids(self, dim: Dimension, label_lower: str) -> list[str] | None:
        """Find group by label and return leaf item IDs.

        Tries canonical graph store first, falls back to dim.outline."""
        from lib_openm.outline_graph_bridge import (
            find_group_node_id_by_label,
            get_group_all_leaf_items,
        )

        ws = self.workspace
        # 1. Try canonical graph store first
        group_id = find_group_node_id_by_label(dim.id, label_lower, ws)
        if group_id is not None:
            item_ids = get_group_all_leaf_items(dim.id, group_id, ws)
            if item_ids:
                # Deduplicate while preserving order
                seen: list[str] = []
                for item_id in item_ids:
                    if item_id not in seen:
                        seen.append(item_id)
                return seen
            raise KeyError(
                f"Group {label_lower!r} in dimension {dim.name!r} contains no leaf items"
            )

        # 2. Fallback to dim.outline (backward compatibility)
        if not dim.outline:
            return None

        def _node_label(node: Any) -> str | None:
            if hasattr(node, "label"):
                return getattr(node, "label")
            if isinstance(node, dict):
                return node.get("label")
            return None

        def _node_children(node: Any) -> list[Any]:
            if hasattr(node, "children"):
                children = getattr(node, "children")
            elif isinstance(node, dict):
                children = node.get("children")
            else:
                children = None
            return list(children or [])

        def _node_item_id(node: Any) -> str | None:
            if hasattr(node, "item_id"):
                return getattr(node, "item_id")
            if isinstance(node, dict):
                return node.get("item_id")
            return None

        def _collect_outline_leaf_item_ids(node: Any) -> list[str]:
            ids: list[str] = []
            item_id = _node_item_id(node)
            if item_id:
                ids.append(item_id)
            for child in _node_children(node):
                ids.extend(_collect_outline_leaf_item_ids(child))
            return ids

        def _search(nodes: list[Any]) -> list[str] | None:
            for node in nodes:
                node_label = (_node_label(node) or "").strip().lower()
                if node_label == label_lower:
                    ids = [item_id for item_id in _collect_outline_leaf_item_ids(node) if item_id]
                    if not ids:
                        raise KeyError(
                            f"Group {label_lower!r} in dimension {dim.name!r} contains no leaf items"
                        )
                    seen: list[str] = []
                    for item_id in ids:
                        if item_id not in seen:
                            seen.append(item_id)
                    return seen
                found = _search(_node_children(node))
                if found is not None:
                    return found
            return None

        return _search(list(dim.outline))

    def _resolve_ref_addrs_for_trace(
        self,
        base_cube: Cube,
        base_addr: tuple[str, ...],
        pairs: list[tuple[str, str]],
        *,
        cube_name_hint: str | None,
        max_results: int,
    ) -> list[tuple[Cube, tuple[str, ...]]]:
        target_cube = base_cube
        if cube_name_hint:
            hinted = self._find_cube_by_name(cube_name_hint)
            if hinted is not None:
                target_cube = hinted

        base_dim_items: dict[str, str] = {}
        for dim_id, item_id in zip(base_cube.dimension_ids, base_addr):
            dim = self._ws.dimensions.get(dim_id)
            if dim is not None:
                base_dim_items[dim.name.lower()] = item_id

        if target_cube.id == base_cube.id:
            addr_states: list[list[str | None]] = [list(base_addr)]
        else:
            seed: list[str | None] = []
            for dim_id in target_cube.dimension_ids:
                dim = self._ws.dimensions.get(dim_id)
                if dim is None:
                    seed.append(None)
                    continue
                seed.append(base_dim_items.get(dim.name.lower()))
            addr_states = [seed]

        for dim_name, item_name in pairs:
            # Whole-cube wildcard ("*.*") does not constrain any specific
            # dimension; keep the current seeded coordinates intact.
            if dim_name == "*" and item_name == "*":
                continue
            slot: int | None = None
            dim_obj: Dimension | None = None
            dim_name_lower = dim_name.strip().lower()
            for idx, dim_id in enumerate(target_cube.dimension_ids):
                dim = self._ws.dimensions.get(dim_id)
                if dim is None:
                    continue
                if dim.name.lower() == dim_name_lower:
                    slot = idx
                    dim_obj = dim
                    break
            if slot is None or dim_obj is None:
                continue

            base_item_id = base_dim_items.get(dim_obj.name.lower())
            item_ids = self._resolve_dim_item_ids_for_trace(dim_obj, item_name, max_results=max_results, base_item_id=base_item_id)
            if not item_ids:
                continue

            new_states: list[list[str | None]] = []
            for state in addr_states:
                for item_id in item_ids:
                    new_addr = list(state)
                    if slot >= len(new_addr):
                        continue
                    new_addr[slot] = item_id
                    new_states.append(new_addr)
                    if len(new_states) >= max_results:
                        break
                if len(new_states) >= max_results:
                    break
            addr_states = new_states or addr_states
            if not addr_states:
                break

        out: list[tuple[Cube, tuple[str, ...]]] = []
        for state in addr_states:
            final = list(state)
            for idx, dim_id in enumerate(target_cube.dimension_ids):
                if idx < len(final) and final[idx] is None:
                    dim = self._ws.dimensions.get(dim_id)
                    if dim and dim.items:
                        final[idx] = dim.items[0].id
            if any(part is None for part in final):
                continue
            out.append((target_cube, tuple(final)))
            if len(out) >= max_results:
                break
        return out

    def _extract_trace_refs(self, expression: str) -> list[tuple[str | None, list[tuple[str, str]]]]:
        from lib_openm.rule_eval.deps import extract_trace_refs as _extract_trace_refs_fn
        return _extract_trace_refs_fn(expression)

    def _is_whole_cube_rule_mask(
        self,
        cube: Cube,
        addr_mask: tuple[str | None, ...] | None,
        targets: tuple[tuple[str, str], ...] | None,
    ) -> bool:
        if targets and len(targets) == 1 and targets[0] == ("*", "*"):
            return True
        if addr_mask is None:
            return False
        aligned = list(addr_mask)
        if len(aligned) < len(cube.dimension_ids):
            aligned.extend([None] * (len(cube.dimension_ids) - len(aligned)))
        return all(item is None for item in aligned[: len(cube.dimension_ids)])

    def _validate_no_bidirectional_recurrence(self, expression: str) -> None:
        """Check that no single dimension has both PREV and NEXT in the rule body.

        Bidirectional recurrence on the same dimension creates unresolvable
        circular dependencies.  PREV on one dimension and NEXT on a different
        dimension is a valid cross-dimensional reference and is allowed.
        """
        from .rule_eval import _tokenise, _Parser, _SEQ_KEYWORDS, _AstRef, _AstMultiRef, _AstDynamicMultiRef, _AstBinOp, _AstUnOp, _AstCall, RuleValidationError

        tokens = _tokenise(expression)
        ast_node = _Parser(tokens).parse()

        dim_keywords: dict[str, set[str]] = {}

        def _collect_keywords(n: Any) -> None:
            if isinstance(n, _AstRef):
                if not getattr(n, "allow_seq_keywords", False):
                    return
                item_upper = n.item_name.upper()
                if item_upper in _SEQ_KEYWORDS:
                    dim_keywords.setdefault(n.dim_name, set()).add(item_upper)
            elif isinstance(n, _AstMultiRef):
                if not getattr(n, "allow_seq_keywords", False):
                    return
                for dim_name, item_name in n.pairs:
                    item_upper = item_name.upper()
                    if item_upper in _SEQ_KEYWORDS:
                        dim_keywords.setdefault(dim_name, set()).add(item_upper)
            elif isinstance(n, _AstDynamicMultiRef):
                for dim_name, item_name in n.pairs:
                    item_upper = item_name.upper()
                    if item_upper in _SEQ_KEYWORDS:
                        dim_keywords.setdefault(dim_name, set()).add(item_upper)
                for dc in n.dynamic_calls:
                    _collect_keywords(dc)
            elif isinstance(n, _AstBinOp):
                _collect_keywords(n.l)
                _collect_keywords(n.r)
            elif isinstance(n, _AstUnOp):
                _collect_keywords(n.operand)
            elif isinstance(n, _AstCall):
                for arg in n.args:
                    _collect_keywords(arg)

        _collect_keywords(ast_node)

        for dim_name, keywords in dim_keywords.items():
            if "PREV" in keywords and "NEXT" in keywords:
                raise RuleValidationError(
                    f"Bidirectional recurrence detected on dimension '{dim_name}': "
                    f"cannot use both PREV and NEXT for the same dimension in a single rule. "
                    f"Recurrence must calculate in one direction only per dimension."
                )

    def _validate_cross_cube_wildcard_mapping_dims(
        self,
        target_cube: Cube,
        expression: str,
        *,
        target_dim_ids: list[str] | None = None,
    ) -> None:
        target_ids = list(target_dim_ids) if target_dim_ids is not None else list(target_cube.dimension_ids)
        target_dim_set = set(target_ids)
        refs = self._extract_trace_refs(expression)
        checked_source_ids: set[str] = set()
        violations: list[str] = []

        for cube_name, pairs in refs:
            if cube_name is None:
                continue
            if not any(dim_name == "*" and item_name == "*" for dim_name, item_name in pairs):
                continue
            source_cube = self._find_cube_by_name(cube_name)
            if source_cube is None or source_cube.id in checked_source_ids:
                continue
            checked_source_ids.add(source_cube.id)
            if source_cube.id == target_cube.id:
                continue
            missing_dim_ids = [did for did in source_cube.dimension_ids if did not in target_dim_set]
            if not missing_dim_ids:
                continue
            missing_dim_names = [self.require_dimension_by_id(did).name for did in missing_dim_ids if did in self._ws.dimensions]
            missing_text = ", ".join(missing_dim_names or missing_dim_ids)
            violations.append(f"{source_cube.name} requires [{missing_text}]")

        if violations:
            joined = "; ".join(violations)
            raise RuleValidationError(
                "Whole-cube mapping requires every source-cube dimension to exist in the target cube. "
                f"Target cube {target_cube.name!r} is missing dimensions: {joined}."
            )

    def _dependents_from_graph(
        self,
        cube_id: str,
        addr: tuple[str, ...],
        *,
        max_results: int,
    ) -> list[dict[str, Any]]:
        dependents: list[dict[str, Any]] = []
        if not self._dep_tracking_enabled:
            return dependents

        start_key = self._cell_node_key(cube_id, addr)
        queue: list[str] = [start_key]
        visited: set[str] = {start_key}

        while queue and len(dependents) < max_results:
            node_key = queue.pop(0)
            for dep_key in self._dep_graph.dependents_of(node_key):
                if dep_key in visited:
                    continue
                visited.add(dep_key)

                parsed = self._parse_cell_node_key(dep_key)
                if parsed is not None:
                    dep_cube_id, dep_addr = parsed
                    # Normalize to full address format (with @.value prefix)
                    dep_cube = self._ws.cubes.get(dep_cube_id)
                    if dep_cube is not None:
                        dep_addr = _normalize_addr_for_cube(dep_cube, dep_addr)
                    try:
                        label = self._format_addr_label(dep_cube_id, dep_addr)
                    except Exception:
                        continue
                    if not any(entry["label"] == label for entry in dependents):
                        dependents.append({
                            "label": label,
                            "cube_id": dep_cube_id,
                            "addr": dep_addr,
                        })
                    if len(dependents) >= max_results:
                        break
                    continue

                queue.append(dep_key)
        return dependents

    def _precedents_from_graph(
        self,
        cube_id: str,
        addr: tuple[str, ...],
        *,
        max_results: int,
    ) -> list[tuple[str, tuple[str, ...]]]:
        if not self._dep_tracking_enabled:
            return []

        start_key = self._cell_node_key(cube_id, addr)
        queue: list[str] = [start_key]
        visited: set[str] = {start_key}
        seen_cells: set[tuple[str, tuple[str, ...]]] = set()
        precedents: list[tuple[str, tuple[str, ...]]] = []

        while queue and len(precedents) < max_results:
            node_key = queue.pop(0)
            for pred_key in self._dep_graph.precedents_of(node_key):
                if pred_key in visited:
                    continue
                visited.add(pred_key)

                parsed = self._parse_cell_node_key(pred_key)
                if parsed is not None:
                    cid, parsed_addr = parsed
                    # Normalize to full address format (with @.value prefix)
                    parsed_cube = self._ws.cubes.get(cid)
                    if parsed_cube is not None:
                        parsed_addr = _normalize_addr_for_cube(parsed_cube, parsed_addr)
                    normalized = (cid, parsed_addr)
                    if normalized in seen_cells:
                        continue
                    seen_cells.add(normalized)
                    precedents.append(normalized)
                    if len(precedents) >= max_results:
                        break
                    continue

                queue.append(pred_key)

        return precedents

    def _is_cell_node_tracked(self, cube_id: str, addr: tuple[str, ...]) -> bool:
        if not self._dep_tracking_enabled:
            return False
        node_key = self._cell_node_key(cube_id, addr)
        return node_key in self._dep_graph._nodes  # type: ignore[attr-defined]

    def _find_dependents_in_cube(
        self,
        cube: Cube,
        addr: tuple[str, ...],
        *,
        max_results: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        if _FLOW_TRACE_DEBUG:
            print(
                "FLOW_TRACE: scan_dependents_in_cube",
                f"cube={cube.name}",
                f"addr={self._format_addr_label(cube.id, addr)}",
                f"max_results={max_results}",
            )
        cross_dependents: list[dict[str, Any]] = []
        same_dependents: list[dict[str, Any]] = []
        truncated = False

        def _record_dependent(entry: dict[str, Any], cube_id: str | None) -> bool:
            nonlocal truncated
            is_cross = isinstance(cube_id, str) and cube_id != cube.id
            bucket = cross_dependents if is_cross else same_dependents
            if len(bucket) >= max_results:
                truncated = True
                if _FLOW_TRACE_DEBUG:
                    print(
                        "FLOW_TRACE: dependent_budget_exceeded",
                        f"is_cross={is_cross}",
                        f"label={entry.get('label')}",
                        f"cross_count={len(cross_dependents)}",
                        f"same_count={len(same_dependents)}",
                    )
                return len(cross_dependents) >= max_results and len(same_dependents) >= max_results
            bucket.append(entry)
            if _FLOW_TRACE_DEBUG:
                print(
                    "FLOW_TRACE: dependent_recorded",
                    f"is_cross={is_cross}",
                    f"label={entry.get('label')}",
                    f"cross_count={len(cross_dependents)}",
                    f"same_count={len(same_dependents)}",
                )
            return False

        def _dim_item_ids(dim_id: str) -> list[str]:
            dim = self._ws.dimensions.get(dim_id)
            if dim is None:
                return []
            return [it.id for it in dim.items]

        def _rule_target_axes(rule: Rule, rule_cube: Cube) -> list[list[str]]:
            axes: list[list[str]] = []
            mask = rule.addr_mask
            for idx, dim_id in enumerate(rule_cube.dimension_ids):
                items = _dim_item_ids(dim_id)
                if not items:
                    axes.append([])
                    continue
                if mask is not None and idx < len(mask):
                    token = mask[idx]
                    if token and token != "*":
                        axes.append([token])
                        continue
                axes.append(list(items))
            return axes

        def _iter_rule_addrs(
            rule: Rule,
            rule_cube: Cube,
            prefer_addr: tuple[str | None, ...],
            limit: int,
        ) -> list[tuple[str, ...]]:
            axes = _rule_target_axes(rule, rule_cube)
            if not axes or any(not axis for axis in axes):
                return []
            prioritized: list[list[str]] = []
            for idx, axis in enumerate(axes):
                values = list(axis)
                preferred = prefer_addr[idx] if idx < len(prefer_addr) else None
                if preferred in values:
                    values.remove(preferred)
                    values.insert(0, preferred)
                prioritized.append(values)

            results: list[tuple[str, ...]] = []
            for combo in itertools.product(*prioritized):
                results.append(tuple(combo))
                if len(results) >= limit:
                    break
            return results

        def _scan_expression(
            expr_cube: Cube,
            expr_base_addr: tuple[str, ...],
            expr: str,
            *,
            label_cube: Cube,
            label_addr: tuple[str, ...],
        ) -> bool:
            refs = self._extract_trace_refs(expr)
            if not refs:
                return False
            if _FLOW_TRACE_DEBUG:
                print(
                    "FLOW_TRACE: scan_expression",
                    f"expr_cube={expr_cube.name}",
                    f"expr_base={self._format_addr_label(expr_cube.id, expr_base_addr)}",
                    f"label_cube={label_cube.name}",
                    f"label_addr={self._format_addr_label(label_cube.id, label_addr)}",
                    f"expr={expr!r}",
                    f"refs={refs!r}",
                )
            for cube_name_hint, pairs in refs:
                resolved = self._resolve_ref_addrs_for_trace(
                    expr_cube,
                    expr_base_addr,
                    pairs,
                    cube_name_hint=cube_name_hint,
                    max_results=max_results,
                )
                if _FLOW_TRACE_DEBUG:
                    print(
                        "FLOW_TRACE: resolved_ref",
                        f"cube_hint={cube_name_hint!r}",
                        f"pairs={pairs!r}",
                        f"resolved={[(c.name, self._format_addr_label(c.id, a)) for c, a in resolved]!r}",
                    )
                for target_cube, target_addr in resolved:
                    if target_cube.id != cube.id or target_addr != addr:
                        continue
                    dep_label = self._format_addr_label(label_cube.id, label_addr)
                    target_entry = {
                        "label": dep_label,
                        "cube_id": label_cube.id,
                        "addr": label_addr,
                    }
                    if any(
                        entry["label"] == dep_label and entry.get("cube_id") == label_cube.id
                        for entry in (cross_dependents + same_dependents)
                    ):
                        continue
                    if _record_dependent(target_entry, label_cube.id):
                        return True
            return False

        def _preferred_addr_for_cube(target_cube: Cube, source_cube: Cube, source_addr: tuple[str, ...]) -> tuple[str | None, ...]:
            name_to_item: dict[str, str] = {}
            for dim_id, item_id in zip(source_cube.dimension_ids, source_addr):
                dim = self._ws.dimensions.get(dim_id)
                if dim is not None:
                    name_to_item[dim.name.lower()] = item_id
            aligned: list[str | None] = []
            for dim_id in target_cube.dimension_ids:
                dim = self._ws.dimensions.get(dim_id)
                aligned.append(name_to_item.get(dim.name.lower()) if dim is not None else None)
            return tuple(aligned)

        for r in self._ws.rules.values():
            if not r.is_anchored or r.addr_mask is None:
                continue
            dep_cube = self.require_cube_by_id(r.cube_id)
            expr = self._normalize_expression(r.expression)
            base_addr = r.addr_mask
            if _scan_expression(dep_cube, base_addr, expr, label_cube=dep_cube, label_addr=base_addr):
                break

        if not (len(cross_dependents) >= max_results and len(same_dependents) >= max_results):
            for rule in self._ws.rules.values():
                dep_cube = self.require_cube_by_id(rule.cube_id)
                if rule.addr_mask is None:
                    continue
                expr = self._normalize_expression(rule.expression)
                prefer = _preferred_addr_for_cube(dep_cube, cube, addr)
                if all(item is None for item in prefer):
                    prefer = tuple(dep_cube.dimension_ids)
                for addr_candidate in _iter_rule_addrs(rule, dep_cube, prefer, max_results):
                    if _scan_expression(dep_cube, addr_candidate, expr, label_cube=dep_cube, label_addr=addr_candidate):
                        break
                if len(cross_dependents) >= max_results and len(same_dependents) >= max_results:
                    break

        dependents = cross_dependents + same_dependents
        if _FLOW_TRACE_DEBUG:
            print(
                "FLOW_TRACE: scan_dependents_in_cube_done",
                f"cube={cube.name}",
                f"addr={self._format_addr_label(cube.id, addr)}",
                f"cross={len(cross_dependents)}",
                f"same={len(same_dependents)}",
                f"truncated={truncated}",
            )
        return dependents, truncated

    def _find_dependents_for_addr(
        self,
        cube: Cube,
        addr: tuple[str, ...],
        *,
        max_results: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        graph_dependents = self._dependents_from_graph(cube.id, addr, max_results=max_results)
        fallback_dependents, fallback_truncated = self._find_dependents_in_cube(cube, addr, max_results=max_results)

        if _FLOW_TRACE_DEBUG:
            print(
                "FLOW_TRACE: find_dependents_for_addr",
                f"cube={cube.name}",
                f"addr={self._format_addr_label(cube.id, addr)}",
                f"graph_count={len(graph_dependents)}",
                f"fallback_count={len(fallback_dependents)}",
                f"fallback_truncated={fallback_truncated}",
                f"graph_labels={[e.get('label') for e in graph_dependents]!r}",
                f"fallback_labels={[e.get('label') for e in fallback_dependents]!r}",
            )

        merged: list[dict[str, Any]] = []
        seen: set[tuple[str | None, str | None, tuple]] = set()

        def _sorted_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
            cross: list[dict[str, Any]] = []
            same: list[dict[str, Any]] = []
            for entry in entries:
                cid = entry.get("cube_id")
                (cross if isinstance(cid, str) and cid != cube.id else same).append(entry)
            return cross + same

        def _add_entries(entries: list[dict[str, Any]]) -> bool:
            for entry in entries:
                label = entry.get("label")
                cube_id = entry.get("cube_id")
                raw_addr = entry.get("addr")
                addr_key = tuple(raw_addr) if isinstance(raw_addr, (tuple, list)) else tuple()
                key = (label, cube_id, addr_key)
                if key in seen:
                    continue
                merged.append(entry)
                seen.add(key)
                if len(merged) >= max_results:
                    return True
            return False

        # Prioritise expression-based (cross-cube) dependents, then graph ones.
        _add_entries(_sorted_entries(fallback_dependents))
        _add_entries(graph_dependents)

        truncated = fallback_truncated or len(merged) >= max_results
        if _FLOW_TRACE_DEBUG:
            print(
                "FLOW_TRACE: find_dependents_for_addr_done",
                f"cube={cube.name}",
                f"addr={self._format_addr_label(cube.id, addr)}",
                f"merged_count={len(merged)}",
                f"truncated={truncated}",
                f"merged_labels={[e.get('label') for e in merged]!r}",
            )
        return merged, truncated

    def trace_calculation_flow(
        self,
        cube_id: str,
        addr: tuple[str, ...],
        *,
        max_depth: int | None = None,
        max_precedents_per_node: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return a lightweight ordered trace for a cell's calculation flow.

        Each entry includes source type, expression (if any), and the resolved
        precedent addresses discovered from explicit references.
        """
        # Use config defaults if not specified
        if max_depth is None:
            max_depth = engine_config("limits", "default_calculation_flow_depth", 2)
        if max_precedents_per_node is None:
            max_precedents_per_node = engine_config("limits", "max_precedents_per_node", 12)

        cube = self.require_cube_by_id(cube_id)
        if _FLOW_TRACE_DEBUG:
            print(
                "FLOW_TRACE: trace_start",
                f"cube={cube.name}",
                f"addr={self._format_addr_label(cube.id, addr)}",
                f"max_depth={max_depth}",
                f"max_precedents_per_node={max_precedents_per_node}",
            )
        queue: list[tuple[Cube, tuple[str, ...], int]] = [(cube, addr, 0)]
        seen: set[tuple[str, tuple[str, ...]]] = set()
        flow: list[dict[str, Any]] = []

        while queue:
            curr_cube, curr_addr, depth = queue.pop(0)
            key = (curr_cube.id, curr_addr)
            if key in seen:
                continue
            seen.add(key)

            source = "input"
            expression: str | None = None
            rule = self._ws.find_anchored_rule(curr_cube.id, curr_addr)
            if rule is not None:
                source = "cell_rule"
                expression = self._normalize_expression(rule.expression)
            else:
                rule = self._ws.find_rule(curr_cube.id, curr_addr, curr_cube.dimension_ids)
                if rule is not None:
                    source = "rule"
                    expression = self._normalize_expression(rule.expression)
                elif curr_cube.get(curr_addr) is None:
                    source = "empty"

            precedents: list[str] = []
            precedent_targets: list[dict[str, Any]] = []
            seen_precedent_labels: set[str] = set()
            if expression is not None and depth < max_depth:
                refs = self._extract_trace_refs(expression)
                # Normalize address for correct dimension slot indexing
                norm_curr_addr = _normalize_addr_for_cube(curr_cube, curr_addr)
                for cube_name, pairs in refs:
                    resolved = self._resolve_ref_addrs_for_trace(
                        curr_cube,
                        norm_curr_addr,
                        pairs,
                        cube_name_hint=cube_name,
                        max_results=max_precedents_per_node,
                    )
                    if not resolved:
                        if pairs:
                            dim_name, item_name = pairs[0]
                            label = f"{dim_name}.{item_name} (unresolved)"
                        else:
                            label = "(unresolved)"
                        if label in seen_precedent_labels:
                            continue
                        precedents.append(label)
                        precedent_targets.append({"label": label})
                        seen_precedent_labels.add(label)
                        continue
                    for target_cube, dep_addr in resolved:
                        if target_cube.id == curr_cube.id and dep_addr == curr_addr:
                            continue
                        # Normalize address for consistent label formatting
                        norm_dep_addr = _normalize_addr_for_cube(target_cube, dep_addr)
                        label = self._format_addr_label(target_cube.id, norm_dep_addr)
                        if label in seen_precedent_labels:
                            continue
                        precedents.append(label)
                        precedent_targets.append(
                            {
                                "label": label,
                                "cube_id": target_cube.id,
                                "addr": norm_dep_addr,
                            }
                        )
                        seen_precedent_labels.add(label)
                        queue.append((target_cube, norm_dep_addr, depth + 1))

            dependent_entries: list[dict[str, Any]] = []
            dependent_targets: list[dict[str, Any]] = []
            dependents: list[str] = []
            dependents_truncated = False
            if depth < max_depth:
                dependent_entries, dependents_truncated = self._find_dependents_for_addr(
                    curr_cube,
                    curr_addr,
                    max_results=max_precedents_per_node,
                )
                dependent_targets = [entry for entry in dependent_entries if entry.get("label")]
                dependents = [entry.get("label", "") for entry in dependent_targets]
                for target in dependent_targets:
                    tgt_cube_id = target.get("cube_id")
                    tgt_addr_raw = target.get("addr")
                    if not isinstance(tgt_cube_id, str) or not isinstance(tgt_addr_raw, (tuple, list)):
                        continue
                    tgt_cube = self.require_cube_by_id(tgt_cube_id)
                    tgt_addr = tuple(tgt_addr_raw)
                    queue.append((tgt_cube, tgt_addr, depth + 1))

            flow.append(
                {
                    "cube_id": curr_cube.id,
                    "cube_name": curr_cube.name,
                    "addr": curr_addr,
                    "addr_label": self._format_addr_label(curr_cube.id, curr_addr),
                    "depth": depth,
                    "source": source,
                    "expression": expression,
                    "precedents": precedents,
                    "precedent_targets": precedent_targets,
                    "dependents": dependents,
                    "dependent_targets": dependent_targets,
                    "dependents_truncated": dependents_truncated if depth < max_depth else False,
                    "dependents_limit": max_precedents_per_node if depth < max_depth else None,
                }
            )

        if _FLOW_TRACE_DEBUG:
            print(
                "FLOW_TRACE: trace_done",
                f"cube={cube.name}",
                f"addr={self._format_addr_label(cube.id, addr)}",
                f"nodes={len(flow)}",
                f"node_labels={[row.get('addr_label') for row in flow]!r}",
            )
        return flow

    def trace_circular_references(
        self,
        cube_id: str,
        addr: tuple[str, ...],
        *,
        max_depth: int = 12,
        max_precedents_per_node: int = 16,
        max_cycles: int = 10,
    ) -> dict[str, Any]:
        """Return circular-reference cycle paths reachable from a selected cell."""

        root_cube = self.require_cube_by_id(cube_id)
        root_key = (root_cube.id, _normalize_addr_for_cube(root_cube, tuple(addr)))

        root_value: Any = None
        try:
            # Prime dependency graph edges for the selected node before tracing.
            root_value = self._get_cell_by_addr(root_cube, tuple(addr))
        except Exception:
            root_value = None

        queue: list[tuple[Cube, tuple[str, ...], int]] = [(root_cube, root_key[1], 0)]
        seen: set[tuple[str, tuple[str, ...]]] = set()
        node_meta: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
        adjacency: dict[tuple[str, tuple[str, ...]], list[tuple[str, tuple[str, ...]]]] = {}
        node_value_cache: dict[tuple[str, tuple[str, ...]], Any] = {root_key: root_value}

        def _node_value(node_key: tuple[str, tuple[str, ...]]) -> Any:
            if node_key in node_value_cache:
                return node_value_cache[node_key]
            try:
                node_cube = self.require_cube_by_id(node_key[0])
                node_value_cache[node_key] = self._get_cell_by_addr(node_cube, node_key[1])
            except Exception:
                node_value_cache[node_key] = None
            return node_value_cache[node_key]

        while queue:
            curr_cube, curr_addr, depth = queue.pop(0)
            key = (curr_cube.id, curr_addr)
            if key in seen:
                continue
            seen.add(key)

            source = "input"
            expression: str | None = None
            rule = self._ws.find_anchored_rule(curr_cube.id, curr_addr)
            if rule is not None:
                source = "cell_rule"
                expression = self._normalize_expression(rule.expression)
            else:
                rule = self._ws.find_rule(curr_cube.id, curr_addr, curr_cube.dimension_ids)
                if rule is not None:
                    source = "rule"
                    expression = self._normalize_expression(rule.expression)
                elif curr_cube.get(curr_addr) is None:
                    source = "empty"

            node_meta[key] = {
                "cube_id": curr_cube.id,
                "addr": curr_addr,
                "addr_label": self._format_addr_label(curr_cube.id, curr_addr),
                "source": source,
                "expression": expression,
                "editable_rule": source in {"cell_rule", "rule"},
            }
            adjacency.setdefault(key, [])

            if depth >= max_depth:
                continue

            next_nodes: list[tuple[str, tuple[str, ...]]] = []
            graph_precedents = self._precedents_from_graph(
                curr_cube.id,
                curr_addr,
                max_results=max_precedents_per_node,
            )
            use_graph_precedents = self._is_cell_node_tracked(curr_cube.id, curr_addr)
            allow_expr_fallback = (not use_graph_precedents) or (
                use_graph_precedents and not graph_precedents and isinstance(_node_value(key), CellError) and _node_value(key).code == "#CIRC!"
            )
            if use_graph_precedents:
                next_nodes.extend((dep_cube_id, tuple(dep_addr)) for dep_cube_id, dep_addr in graph_precedents)
            if allow_expr_fallback and expression is not None:
                refs = self._extract_trace_refs(expression)
                for cube_name, pairs in refs:
                    resolved = self._resolve_ref_addrs_for_trace(
                        curr_cube,
                        curr_addr,
                        pairs,
                        cube_name_hint=cube_name,
                        max_results=max_precedents_per_node,
                    )
                    for target_cube, dep_addr in resolved:
                        dep_key = (target_cube.id, tuple(dep_addr))
                        next_nodes.append(dep_key)

            for dep_key in next_nodes:
                if dep_key not in seen and depth < max_depth:
                    dep_cube = self.require_cube_by_id(dep_key[0])
                    queue.append((dep_cube, dep_key[1], depth + 1))

            uniq_next: list[tuple[str, tuple[str, ...]]] = []
            seen_next: set[tuple[str, tuple[str, ...]]] = set()
            for dep_key in next_nodes:
                if dep_key in seen_next:
                    continue
                seen_next.add(dep_key)
                uniq_next.append(dep_key)
            adjacency[key] = uniq_next

        cycles: list[list[tuple[str, tuple[str, ...]]]] = []
        seen_signatures: set[tuple[str, ...]] = set()

        def _node_token(node_key: tuple[str, tuple[str, ...]]) -> str:
            return f"{node_key[0]}::{'|'.join(node_key[1])}"

        def _cycle_signature(cycle_nodes: list[tuple[str, tuple[str, ...]]]) -> tuple[str, ...]:
            if not cycle_nodes:
                return tuple()
            tokens = [_node_token(node) for node in cycle_nodes]
            best = tokens
            for idx in range(1, len(tokens)):
                rotated = tokens[idx:] + tokens[:idx]
                if rotated < best:
                    best = rotated
            return tuple(best)

        def _dfs(curr: tuple[str, tuple[str, ...]], path: list[tuple[str, tuple[str, ...]]]) -> None:
            if len(cycles) >= max_cycles:
                return
            path.append(curr)
            for nxt in adjacency.get(curr, []):
                if nxt in path:
                    start_idx = path.index(nxt)
                    cycle_nodes = path[start_idx:]
                    if root_key not in cycle_nodes:
                        continue
                    sig = _cycle_signature(cycle_nodes)
                    if sig and sig not in seen_signatures:
                        seen_signatures.add(sig)
                        cycles.append(cycle_nodes)
                        if len(cycles) >= max_cycles:
                            path.pop()
                            return
                    continue
                if len(path) >= max_depth:
                    continue
                _dfs(nxt, path)
                if len(cycles) >= max_cycles:
                    path.pop()
                    return
            path.pop()

        _dfs(root_key, [])

        cycle_rows: list[dict[str, Any]] = []
        for idx, cycle_nodes in enumerate(cycles):
            nodes = [dict(node_meta.get(node, {})) for node in cycle_nodes]
            labels = [str(row.get("addr_label", "")) for row in nodes]
            path_text = " -> ".join(labels + ([labels[0]] if labels else []))
            cycle_rows.append(
                {
                    "index": idx,
                    "length": len(cycle_nodes),
                    "nodes": nodes,
                    "path": path_text,
                }
            )

        confidence = "high" if (isinstance(root_value, CellError) and root_value.code == "#CIRC!") and cycle_rows else "medium"
        return {
            "root": {
                "cube_id": root_cube.id,
                "addr": tuple(addr),
                "addr_label": self._format_addr_label(root_cube.id, tuple(addr)),
                "value": root_value,
            },
            "cycle_count": len(cycle_rows),
            "cycles": cycle_rows,
            "confidence": confidence,
        }

    def create_dimension(self, name: str, dim_type: str = "set") -> Dimension:
        # Check for duplicate dimension names (case-insensitive, trimmed)
        name_clean = name.strip().casefold()
        for existing_dim in self._ws.dimensions.values():
            if existing_dim.name.strip().casefold() == name_clean:
                raise ValueError(f"Dimension with name '{name}' already exists")
        dim = Dimension.create(name.strip(), dim_type=dim_type)
        self._ws.add_dimension(dim)
        self._publish_event(EVENT_DIMENSION_CREATED, {
            "dim_id": dim.id,
            "name": dim.name,
            "dim_type": dim.dim_type,
        })
        return dim

    def create_cube(self, name: str, dimension_ids: list[str]) -> Cube:
        if not dimension_ids:
            raise ValueError("Cube must have at least one dimension")
        # Check for duplicate cube names (case-insensitive, trimmed)
        name_clean = name.strip().casefold()
        for existing_cube in self._ws.cubes.values():
            if existing_cube.name.strip().casefold() == name_clean:
                raise ValueError(f"Cube with name '{name}' already exists")
        # validate no duplicate dimensions
        seen = set()
        for did in dimension_ids:
            if did in seen:
                dim_name = self._ws.dimensions.get(did, "Unknown")
                if hasattr(dim_name, 'name'):
                    dim_name = dim_name.name
                raise ValueError(f"Duplicate dimension in cube '{name}': dimension '{dim_name}' (id={did}) used more than once")
            seen.add(did)
        # validate dimensions exist
        for did in dimension_ids:
            if did not in self._ws.dimensions:
                raise KeyError(f"Unknown dimension id {did}")
        cube = Cube.create(name.strip(), dimension_ids)
        self._ws.add_cube(cube)
        self._publish_event(EVENT_CUBE_CREATED, {
            "cube_id": cube.id,
            "name": cube.name,
            "dimension_ids": cube.dimension_ids,
        })
        return cube

    def create_view(
        self,
        name: str,
        cube_id: str,
        row_dim_id: str,
        col_dim_id: str,
        page_dim_ids: list[str] | None = None,
        layout: ViewLayout | None = None,
    ) -> TableViewSpec:
        """Create a new view with duplicate name checking.

        The `layout` parameter is an internal command-layer integration point.
        REPL/CLI/GUI callers must continue to use `session.execute(...)` / command
        payloads rather than direct `engine.create_view(..., layout=...)` calls.
        """
        # Check for duplicate view names (case-insensitive, trimmed)
        name_clean = name.strip().casefold()
        for existing_view in self._ws.views.values():
            if existing_view.name.strip().casefold() == name_clean:
                raise ValueError(f"View with name '{name}' already exists")
        view = TableViewSpec.create(
            name=name.strip(),
            cube_id=cube_id,
            row_dimension_id=row_dim_id,
            col_dimension_id=col_dim_id or "",
            page_dim_ids=page_dim_ids,
        )
        if not col_dim_id:
            view.col_dim_ids = []
        if layout is not None:
            apply_layout_to_view(view, layout)
        self._ws.add_view(view)
        self._publish_event(EVENT_VIEW_CREATED, {
            "view_id": view.id,
            "name": view.name,
            "cube_id": view.cube_id,
        })
        return view

    def set_view_layout(self, view_id: str, layout: ViewLayout) -> None:
        """Apply an already-validated layout to an existing view.

        The caller (command layer) is responsible for semantic validation.
        This method performs only the canonical mutation and emits the
        corresponding domain event through BusEventPublisher.
        """
        view = self._ws.views[view_id]
        apply_layout_to_view(view, layout)
        self._publish_event(EVENT_VIEW_LAYOUT_CHANGED, {
            "view_id": view_id,
            "cube_id": view.cube_id,
        })

    def _remap_view_col_widths(self, view_id: str, old_keys: list[tuple[str, ...]], old_widths: dict[int, int]) -> None:
        """Rebuild view.col_widths after column keys have changed.

        Maps widths from old column indices to new indices by matching
        column keys (tuples of item IDs). Columns whose keys no longer
        exist (deleted items) lose their width entries.
        """
        view = self.require_view_by_id(view_id)
        new_keys = self.view_col_keys(view_id)
        new_widths: dict[int, int] = {}
        old_key_to_idx = {k: i for i, k in enumerate(old_keys)}
        for new_idx, new_key in enumerate(new_keys):
            old_idx = old_key_to_idx.get(new_key)
            if old_idx is not None:
                w = old_widths.get(old_idx)
                if w is not None:
                    new_widths[new_idx] = w
        view.col_widths = new_widths

    def delete_cube(self, cube_id: str) -> bool:
        """Delete a cube and all views that reference it."""
        cube = self._ws.cubes.get(cube_id)
        if cube is None:
            return False
        # Remove views using this cube
        views_to_remove = [v for v in self._ws.views.values() if v.cube_id == cube_id]
        for v in views_to_remove:
            self._ws.views.pop(v.id, None)
        self._ws.cubes.pop(cube_id, None)
        return True

    def delete_dimension(self, dim_id: str) -> bool:
        """Delete a dimension, detaching it from all cubes and views first."""
        dim = self._ws.dimensions.get(dim_id)
        if dim is None:
            return False
        # Detach from all cubes that use it
        for cube in list(self._ws.cubes.values()):
            if dim_id in cube.dimension_ids:
                self.detach_dimension_from_cube(cube.id, dim_id)
        # Remove from all views
        affected_view_ids = []
        for view in self._ws.views.values():
            if dim_id in view.row_dim_ids or dim_id in view.col_dim_ids or dim_id in view.page_dim_ids:
                affected_view_ids.append(view.id)
            view.row_dim_ids = [d for d in view.row_dim_ids if d != dim_id]
            view.col_dim_ids = [d for d in view.col_dim_ids if d != dim_id]
            view.page_dim_ids = [d for d in view.page_dim_ids if d != dim_id]
        del self._ws.dimensions[dim_id]
        self._publish_event(EVENT_DIMENSION_DELETED, {
            "dim_id": dim_id,
            "affected_view_ids": affected_view_ids,
        })
        return True

    def _add_dimension_item(self, dim_id: str, name: str, position: str = "append") -> DimensionItem:
        dim = self.require_dimension_by_id(dim_id)

        # Save old column keys for views that use this dimension on the column axis
        # so we can remap col_widths after the dimension changes.
        affected_views: list[tuple[str, list[tuple[str, ...]], dict[int, int]]] = []
        for view in self._ws.views.values():
            if dim_id in view.col_dim_ids:
                old_keys = self.view_col_keys(view.id)
                old_widths = dict(view.col_widths)
                affected_views.append((view.id, old_keys, old_widths))

        item = dim.add_item(name, position=position)

        # Remap persisted column widths to new indices
        for view_id, old_keys, old_widths in affected_views:
            self._remap_view_col_widths(view_id, old_keys, old_widths)

        # Update affected view outlines that use this dimension as their first row dimension
        for view in self._ws.views.values():
            if getattr(view, "row_outline", None) and getattr(view, "row_dim_ids", None):
                if view.row_dim_ids and view.row_dim_ids[0] == dim_id:
                    from lib_openm.model import OutlineNode
                    view.row_outline.append(OutlineNode(label=item.name, item_id=item.id, children=[]))
        self._publish_event(EVENT_DIMENSION_ITEM_CREATED, {
            "dim_id": dim_id,
            "item_id": item.id,
            "name": item.name,
        })
        self._invalidate_slice_dependent_rules()
        return item

    def create_dimension_item(self, dim_id: str, name: str, position: str = "append") -> DimensionItem:
        """Canonical public method for creating a dimension item."""
        return self._add_dimension_item(dim_id, name, position=position)

    def batch_create_dimension_items(
        self, items: list[tuple[str, str, str]]
    ) -> list[DimensionItem]:
        """Create many dimension items in one call with a single event publish.

        Args:
            items: list of (dim_id, name, position) tuples.

        Returns:
            list of created DimensionItem objects (or dicts in remote mode).

        This avoids per-item event publishing and cache invalidation, which
        is the main bottleneck when sourcing scripts that create hundreds of
        dimension items.
        """
        created: list[DimensionItem] = []
        affected_dims: set[str] = set()
        for dim_id, name, position in items:
            dim = self.require_dimension_by_id(dim_id)
            item = dim.add_item(name, position=position)
            created.append(item)
            affected_dims.add(dim_id)
            # Update affected view outlines (same as _add_dimension_item)
            for view in self._ws.views.values():
                if getattr(view, "row_outline", None) and getattr(view, "row_dim_ids", None):
                    if view.row_dim_ids and view.row_dim_ids[0] == dim_id:
                        from lib_openm.model import OutlineNode
                        view.row_outline.append(OutlineNode(label=item.name, item_id=item.id, children=[]))
        # Single cache invalidation + event publish for the whole batch
        if affected_dims:
            self._invalidate_slice_dependent_rules()
            for dim_id in affected_dims:
                self._publish_event(EVENT_DIMENSION_ITEM_CREATED, {
                    "dim_id": dim_id,
                    "item_id": None,
                    "name": None,
                    "batch": True,
                })
        return created

    def set_dimension_item_order(self, dim_id: str, item_ids: list[str]) -> None:
        """Replace the flat dimension item order exactly with item_ids.

        Validates that item_ids is a permutation of the current dimension
        item IDs. Rejects sequential dimensions. Emits domain event.

        With sparse graph overlay: if the dimension has graph data, this
        stores sparse root-order overrides for ungrouped items instead of
        clearing the outline.
        """
        dim = self.require_dimension_by_id(dim_id)
        if getattr(dim, "dim_type", "set") == "seq":
            raise ValueError(f"Cannot reorder sequential dimension: {dim_id}")
        current_ids = [it.id for it in dim.items]
        if set(item_ids) != set(current_ids):
            raise ValueError(
                f"item_ids must be a permutation of current dimension items for {dim_id}"
            )
        if len(item_ids) != len(current_ids):
            raise ValueError(
                f"item_ids length mismatch for {dim_id}: got {len(item_ids)}, expected {len(current_ids)}"
            )

        # Save old column keys for views that use this dimension on the column axis
        # so we can remap col_widths after reordering.
        affected_views: list[tuple[str, list[tuple[str, ...]], dict[int, int]]] = []
        for view in self._ws.views.values():
            if dim_id in view.col_dim_ids:
                old_keys = self.view_col_keys(view.id)
                old_widths = dict(view.col_widths)
                affected_views.append((view.id, old_keys, old_widths))

        from lib_openm.outline_graph_bridge import _has_graph_data

        if _has_graph_data(dim_id, self._ws):
            # Sparse mode: store root-order overrides for ungrouped items.
            # Find which items are ungrouped (not in any graph edge).
            from lib_openm.graph_mutation import _all_nodes_for_dim, _all_edges_for_dim
            all_nodes = {n["node_id"]: n for n in _all_nodes_for_dim(dim_id, self._ws)}
            grouped_ids: set[str] = set()
            for edge in _all_edges_for_dim(dim_id, self._ws):
                if edge["kind"] in ("MEMBER_OF", "AGGREG_OF"):
                    src_node = all_nodes.get(edge["src"], {})
                    if src_node.get("kind") == "ITEM_REF":
                        ref = src_node.get("ref")
                        if ref:
                            grouped_ids.add(ref)

            # Build sparse overrides for ungrouped items only
            override: dict[str, int] = {}
            for i, iid in enumerate(item_ids):
                if iid not in grouped_ids:
                    override[iid] = i

            # Still reorder dim.items to keep flat canonical state in sync
            id_to_item = {it.id: it for it in dim.items}
            dim.items = [id_to_item[iid] for iid in item_ids]

            # Store sparse override, preserve outline
            object.__setattr__(dim, "_root_order_override", override)
            dim.invalidate_outline_cache()
        else:
            # No graph data: classic flat reorder, clear outline
            id_to_item = {it.id: it for it in dim.items}
            dim.items = [id_to_item[iid] for iid in item_ids]
            object.__setattr__(dim, "_outline_cache", None)
            object.__setattr__(dim, "outline", [])

        # Remap persisted column widths to new indices after reordering
        for view_id, old_keys, old_widths in affected_views:
            self._remap_view_col_widths(view_id, old_keys, old_widths)

        self._publish_event(EVENT_DIMENSION_STRUCTURE_CHANGED, {
            "dim_id": dim_id,
            "change_type": "item_order",
            "item_ids": item_ids,
        })
        self._invalidate_slice_dependent_rules()

    def _dimension_effective_order(self, dim_id: str) -> list[str]:
        """Return item IDs in effective display order.

        For flat dimensions with no graph data, returns ``dim.items`` order.
        For dimensions with graph data, flattens the outline tree and
        appends unmaterialized items in ``dim.items`` order.

        Architecture debt: this is a direct Engine read until QueryService
        exposes ``query.dimension.effective_order``.
        """
        dim = self._ws.get_dimension(dim_id)
        if dim is None:
            return []

        from lib_openm.outline_graph_bridge import _has_graph_data, rebuild_outline_from_graph
        if not _has_graph_data(dim_id, self._ws):
            return [it.id for it in dim.items]

        outline = rebuild_outline_from_graph(dim, self._ws)

        def _extract(nodes) -> list[str]:
            result: list[str] = []
            for node in nodes:
                if node.item_id:
                    result.append(node.item_id)
                result.extend(_extract(list(node.children)))
            return result

        graphed_ids = _extract(outline)
        graphed_set = set(graphed_ids)

        # Collect unmaterialized items and sort by sparse override
        ungraphed = [it for it in dim.items if it.id not in graphed_set]
        override = getattr(dim, "_root_order_override", {}) or {}
        if override:
            ungraphed.sort(key=lambda it: override.get(it.id, 999999))
        graphed_ids.extend(it.id for it in ungraphed)

        return graphed_ids

    def _dimension_effective_order_window(
        self, dim_id: str, offset: int = 0, limit: int | None = None
    ) -> list[str]:
        """Paginated slice of ``dimension_effective_order``.

        Returns ``limit`` items starting at ``offset`` (0-based).
        """
        all_ids = self._dimension_effective_order(dim_id)
        start = offset
        end = offset + limit if limit is not None else None
        return all_ids[start:end]

    def _compact_dimension_graph(self, dim_id: str) -> int:
        """Remove orphaned ITEM_REF nodes with no edges and no root order.

        Preserves GROUP nodes, edges, and any ITEM_REF nodes that are
        connected to the graph or have a root_ord override.

        Returns the number of nodes removed.
        """
        from lib_openm.graph_mutation import _cleanup_orphan_item_ref_nodes
        from lib_openm.outline_graph_bridge import _has_graph_data

        if not _has_graph_data(dim_id, self._ws):
            return 0

        before = len([
            n for n in self._ws.dimensions[dim_id].items
        ])  # rough proxy; real count via %RECNOD

        _cleanup_orphan_item_ref_nodes(dim_id, self._ws)

        # Rebuild ITEM_REF index after cleanup
        self._ws.rebuild_item_ref_index()

        # Report approximate count (not critical for correctness)
        return 0  # count not tracked; caller checks via node queries

    # ------------------------------------------------------------------
    # Hierarchy operations (Phase 6: GUI must not touch graph_mutation directly)
    # ------------------------------------------------------------------

    def _reduce_node_set(
        self, dim_id: str, node_ids: list[str]
    ) -> list[str]:
        """Replace fully-enclosed child groups with their parent.

        If every child of a parent group is in node_ids, replace those
        children with the parent.  Repeat until stable so nested groups
        bubble up correctly.
        """
        from lib_openm.graph_mutation import _all_edges_for_dim

        # Build parent -> children map
        parent_to_children: dict[str, list[str]] = {}
        for edge in _all_edges_for_dim(dim_id, self._ws):
            if edge["kind"] == "MEMBER_OF":
                parent_to_children.setdefault(edge["tgt"], []).append(edge["src"])

        node_set = set(node_ids)

        changed = True
        while changed:
            changed = False
            for parent_id, children in parent_to_children.items():
                if not children:
                    continue
                if parent_id in node_set:
                    continue
                if all(c in node_set for c in children):
                    for c in children:
                        node_set.discard(c)
                    node_set.add(parent_id)
                    changed = True
                    break

        return list(node_set)

    def move_nodes(
        self,
        dim_id: str,
        node_ids: list[str],
        new_parent_node_id: str | None,
        anchor_node_id: str | None = None,
        position: str = "last",
        reduce_enclosed_groups: bool = False,
        move_empty_parents: bool = True,
    ) -> None:
        """Move nodes to a new parent with optional sibling positioning.

        Validation:
        - dimension exists
        - every node_id exists in this dimension
        - new_parent_node_id exists and is a GROUP node (or None for root)
        - operation does not create a cycle
        - before/after require anchor_node_id
        - first/last require anchor_node_id=None
        """
        from lib_openm.graph_mutation import (
            graph_mutation,
            _all_nodes_for_dim,
            _all_edges_for_dim,
            _root_level_nodes,
            _display_parent_edge,
            _create_edge_raw,
            _delete_edge_raw,
            _set_node_root_ord,
            _read_node_meta,
        )
        from lib_utils.ids import new_id

        dim = self.require_dimension_by_id(dim_id)
        if dim is None:
            raise ValueError(f"Dimension {dim_id} not found")

        # Validate position/anchor consistency
        if position in ("before", "after") and anchor_node_id is None:
            raise ValueError(f"position='{position}' requires anchor_node_id")
        if position in ("first", "last") and anchor_node_id is not None:
            raise ValueError(f"position='{position}' requires anchor_node_id=None")

        # Build node lookup for this dimension
        all_nodes = {n["node_id"]: n for n in _all_nodes_for_dim(dim_id, self._ws)}

        # If all children of a group are selected, move the group instead
        if reduce_enclosed_groups:
            node_ids = self._reduce_node_set(dim_id, node_ids)

        # Validate all source nodes exist
        for node_id in node_ids:
            if node_id not in all_nodes:
                raise ValueError(f"Node {node_id} not found in dimension {dim_id}")

        # Validate target group if provided
        if new_parent_node_id is not None:
            if new_parent_node_id not in all_nodes:
                raise ValueError(f"Target group {new_parent_node_id} not found")
            if all_nodes[new_parent_node_id].get("kind") != "GROUP":
                raise ValueError(f"Target {new_parent_node_id} is not a GROUP node")

        # Validate anchor if provided
        if anchor_node_id is not None and anchor_node_id not in all_nodes:
            raise ValueError(f"Anchor {anchor_node_id} not found in dimension {dim_id}")

        # Cycle detection: no moved node may be an ancestor of new_parent
        if new_parent_node_id is not None:
            def _is_ancestor(ancestor_id: str, descendant_id: str) -> bool:
                """Check if ancestor_id is in the parent chain of descendant_id."""
                parent_edge = _display_parent_edge(descendant_id, dim_id, self._ws)
                if parent_edge is None:
                    return False
                parent_id = parent_edge["tgt"]
                if parent_id == ancestor_id:
                    return True
                return _is_ancestor(ancestor_id, parent_id)

            for node_id in node_ids:
                if node_id == new_parent_node_id or _is_ancestor(node_id, new_parent_node_id):
                    raise ValueError(f"Cannot move node {node_id} under itself or its descendant")

        with graph_mutation(dim_id, self._ws):
            # Step 1: Detach all selected nodes from current parent
            former_parents: set[str] = set()
            for node_id in node_ids:
                parent_edge = _display_parent_edge(node_id, dim_id, self._ws)
                if parent_edge:
                    _delete_edge_raw(parent_edge["edge_id"], self._ws)
                    former_parents.add(parent_edge["tgt"])
                else:
                    _set_node_root_ord(node_id, None, self._ws)

            # Step 1b: If any former parent is now empty (all children moved),
            # also move that former parent under the new group.
            if move_empty_parents:
                extra_parents_to_move: list[str] = []
                for parent_id in former_parents:
                    if parent_id == new_parent_node_id:
                        continue
                    remaining_children = [
                        e["src"] for e in _all_edges_for_dim(dim_id, self._ws)
                        if e["tgt"] == parent_id and e["kind"] == "MEMBER_OF"
                    ]
                    if not remaining_children:
                        extra_parents_to_move.append(parent_id)
                if extra_parents_to_move:
                    node_ids = node_ids + extra_parents_to_move

            # Step 2: Compute target order and attach
            if new_parent_node_id is None:
                # Root-level placement
                roots = [n["node_id"] for n in _root_level_nodes(dim_id, self._ws)]
                # Remove moved nodes from current position
                for node_id in node_ids:
                    if node_id in roots:
                        roots.remove(node_id)

                if position == "first":
                    insert_idx = 0
                elif position == "last":
                    insert_idx = len(roots)
                elif position == "before" and anchor_node_id in roots:
                    insert_idx = roots.index(anchor_node_id)
                elif position == "after" and anchor_node_id in roots:
                    insert_idx = roots.index(anchor_node_id) + 1
                else:
                    insert_idx = len(roots)

                for node_id in reversed(node_ids):
                    roots.insert(insert_idx, node_id)
                for i, rid in enumerate(roots):
                    _set_node_root_ord(rid, i, self._ws)
            else:
                # Group-level placement
                # Collect existing edges, delete them all, then rebuild
                existing_edges = [e for e in _all_edges_for_dim(dim_id, self._ws)
                                  if e["tgt"] == new_parent_node_id and e["kind"] == "MEMBER_OF"]
                print(f"[move_nodes] new_parent={new_parent_node_id}, existing_edges_to_parent={len(existing_edges)}")
                existing_edges.sort(key=lambda e: e["ord"] if e["ord"] is not None else 0)
                children = [e["src"] for e in existing_edges]
                for edge in existing_edges:
                    print(f"[move_nodes] deleting existing edge to parent: {edge['edge_id']}")
                    _delete_edge_raw(edge["edge_id"], self._ws)

                for node_id in node_ids:
                    if node_id in children:
                        children.remove(node_id)

                if position == "first":
                    insert_idx = 0
                elif position == "last":
                    insert_idx = len(children)
                elif position == "before" and anchor_node_id in children:
                    insert_idx = children.index(anchor_node_id)
                elif position == "after" and anchor_node_id in children:
                    insert_idx = children.index(anchor_node_id) + 1
                else:
                    insert_idx = len(children)

                for node_id in reversed(node_ids):
                    children.insert(insert_idx, node_id)

                for i, cid in enumerate(children):
                    _create_edge_raw(new_id("edg"), "MEMBER_OF", cid, new_parent_node_id, dim_id, i, self._ws)
            self._invalidate_slice_dependent_rules()

    def reorder_nodes(
        self,
        dim_id: str,
        parent_node_id: str | None,
        node_ids: list[str],
        anchor_node_id: str,
        position: str,
    ) -> None:
        """Reorder existing sibling nodes within a parent.

        All nodes must already share the same parent.
        """
        if position not in ("before", "after"):
            raise ValueError("reorder_nodes only supports before/after")
        self.move_nodes(dim_id, node_ids, parent_node_id, anchor_node_id, position)

    def delete_group_node(
        self,
        dim_id: str,
        group_node_id: str,
        promote_children: str = "to_parent",
    ) -> None:
        """Delete a group and promote its children.

        promote_children:
        - "to_parent": children replace group's position as contiguous block
        - "to_root": children appended to root
        - "delete": children deleted (admin only)
        """
        from lib_openm.graph_mutation import (
            graph_mutation,
            _all_nodes_for_dim,
            _all_edges_for_dim,
            _display_parent_edge,
            _delete_edge_raw,
            _create_edge_raw,
            _set_node_root_ord,
            _root_level_nodes,
        )
        from lib_utils.ids import new_id

        dim = self.require_dimension_by_id(dim_id)
        if dim is None:
            raise ValueError(f"Dimension {dim_id} not found")

        all_nodes = {n["node_id"]: n for n in _all_nodes_for_dim(dim_id, self._ws)}
        if group_node_id not in all_nodes:
            raise ValueError(f"Group {group_node_id} not found")
        if all_nodes[group_node_id].get("kind") != "GROUP":
            raise ValueError(f"Node {group_node_id} is not a GROUP")

        # Remember label before deletion so rule references can be cleaned up
        deleted_group_label = all_nodes[group_node_id].get("label", "")

        # Get group parent edge
        parent_edge = _display_parent_edge(group_node_id, dim_id, self._ws)
        group_parent_id = parent_edge["tgt"] if parent_edge else None
        group_edge_kind = parent_edge["kind"] if parent_edge else None

        # Get group children
        children_edges = [e for e in _all_edges_for_dim(dim_id, self._ws)
                          if e["tgt"] == group_node_id and e["kind"] in ("MEMBER_OF", "AGGREG_OF")]
        children_edges.sort(key=lambda e: e["ord"] if e["ord"] is not None else 0)
        child_node_ids = [e["src"] for e in children_edges]

        # Pre-compute insertion position before any mutations
        if group_parent_id is not None:
            siblings = [e for e in _all_edges_for_dim(dim_id, self._ws)
                        if e["tgt"] == group_parent_id and e["kind"] == group_edge_kind]
            siblings.sort(key=lambda e: e["ord"] if e["ord"] is not None else 0)
            sibling_order = [e["src"] for e in siblings]
            group_insert_idx = sibling_order.index(group_node_id) if group_node_id in sibling_order else len(sibling_order)
        else:
            roots = [n["node_id"] for n in _root_level_nodes(dim_id, self._ws)]
            group_insert_idx = roots.index(group_node_id) if group_node_id in roots else len(roots)

        with graph_mutation(dim_id, self._ws):
            # Delete group from its parent
            if parent_edge:
                _delete_edge_raw(parent_edge["edge_id"], self._ws)
            else:
                _set_node_root_ord(group_node_id, None, self._ws)

            # Delete group's internal edges
            for edge in children_edges:
                _delete_edge_raw(edge["edge_id"], self._ws)

            # Delete the group node itself from %RECNOD
            from lib_openm.graph_mutation import _remove_node_raw
            _remove_node_raw(group_node_id, self._ws)

            if promote_children == "delete":
                # Children are orphaned; caller must handle cleanup
                pass
            elif promote_children == "to_root":
                roots = [n["node_id"] for n in _root_level_nodes(dim_id, self._ws)]
                for cid in child_node_ids:
                    roots.append(cid)
                for i, rid in enumerate(roots):
                    _set_node_root_ord(rid, i, self._ws)
            else:
                # "to_parent" - children replace group's position
                if group_parent_id is not None:
                    # Collect existing siblings, then delete old edges, then rebuild
                    existing_edges = [e for e in _all_edges_for_dim(dim_id, self._ws)
                                      if e["tgt"] == group_parent_id and e["kind"] == group_edge_kind]
                    existing_edges.sort(key=lambda e: e["ord"] if e["ord"] is not None else 0)
                    all_siblings = [e["src"] for e in existing_edges]
                    # Delete all old edges to this parent
                    for edge in existing_edges:
                        _delete_edge_raw(edge["edge_id"], self._ws)
                    # Build new sibling order: insert children at group's former position
                    sibling_order = []
                    for i, sid in enumerate(all_siblings):
                        if i == group_insert_idx:
                            for cid in child_node_ids:
                                sibling_order.append(cid)
                        sibling_order.append(sid)
                    if group_insert_idx >= len(all_siblings):
                        for cid in child_node_ids:
                            sibling_order.append(cid)
                    for i, sid in enumerate(sibling_order):
                        _create_edge_raw(new_id("edg"), group_edge_kind, sid, group_parent_id, dim_id, i, self._ws)
                else:
                    # Group was at root; children become root at group's position
                    roots = [n["node_id"] for n in _root_level_nodes(dim_id, self._ws)]
                    for cid in reversed(child_node_ids):
                        roots.insert(group_insert_idx, cid)
                    for i, rid in enumerate(roots):
                        _set_node_root_ord(rid, i, self._ws)

        # Update rules that reference the deleted group label
        if deleted_group_label:
            self._mark_deleted_group_in_all_rules(dim.name, deleted_group_label)

    # -- Wrappers (convenience for GUI callers) --

    def move_items_to_group(self, dim_id: str, item_ids: list[str], group_node_id: str) -> None:
        """Move items under a group. Resolves item IDs to node IDs, then calls move_nodes."""
        from lib_openm.graph_mutation import _find_item_ref_node_id, _ensure_item_ref_node_raw
        node_ids = []
        for item_id in item_ids:
            node_id = _find_item_ref_node_id(dim_id, item_id, self._ws)
            if not node_id:
                node_id = _ensure_item_ref_node_raw(dim_id, item_id, self._ws)
            if node_id:
                node_ids.append(node_id)
        if node_ids:
            self.move_nodes(dim_id, node_ids, group_node_id, position="last", reduce_enclosed_groups=True)

    def ungroup_items(self, dim_id: str, item_ids: list[str]) -> None:
        """Detach items from parent group and promote to root."""
        from lib_openm.graph_mutation import _find_item_ref_node_id
        node_ids = []
        for item_id in item_ids:
            node_id = _find_item_ref_node_id(dim_id, item_id, self._ws)
            if node_id:
                node_ids.append(node_id)
        if node_ids:
            self.move_nodes(dim_id, node_ids, None, position="last")

    # ------------------------------------------------------------------
    # Rename helpers (dimensions, items, cubes) with rule propagation
    # ------------------------------------------------------------------

    def _rename_dimension_in_expression(self, expr: str, old_name: str, new_name: str) -> str:
        """Rewrite explicit Dim.Item / Dim:Item refs and function args that use a given dimension.

        We touch occurrences where the name is immediately followed by ':', '.', ')', or ']',
        to avoid changing unrelated text. This handles both references like Dim.Item and
        function arguments like pos(Dimension).
        """

        if not expr or not old_name or old_name.lower() == new_name.lower():
            return expr
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(old_name) + r"(?=\s*[:.\]\)])"
        return re.sub(pattern, new_name, expr, flags=re.IGNORECASE)

    def _rename_dimension_item_in_expression(
        self,
        expr: str,
        dim_name: str,
        old_item_name: str,
        new_item_name: str,
    ) -> str:
        """Rewrite explicit refs to a particular Dim.Item and bare item name.

        This updates:

          Dim.OldItem        -> Dim.NewItem
          Dim:OldItem        -> Dim:NewItem
          [Dim.OldItem]      -> [Dim.NewItem]
          [Dim:OldItem, ...] -> [Dim:NewItem, ...]

        and also bare contextual uses of the item name ("OldItem"), which are
        resolved at eval time based on the cube's dimensions.
        """

        if (
            not expr
            or not dim_name
            or not old_item_name
            or old_item_name.lower() == new_item_name.lower()
        ):
            return expr

        dim_pat = re.escape(dim_name)
        item_pat = re.escape(old_item_name)

        # Explicit refs with a dimension prefix: Dim.OldItem / Dim:OldItem.
        # Keep any surrounding whitespace and the original ':' or '.'
        # separator, only swapping the item portion.
        explicit_pattern = re.compile(
            rf"(?<![A-Za-z0-9_])("  # boundary before dimension
            rf"{dim_pat}"
            rf")("  # separator group (':', '.') plus whitespace
            rf"\s*[:.]\s*"
            rf")("  # item name group
            rf"{item_pat}"
            rf")(?=(\s*($|[\]\+\-*/^(),%<>!=])))",  # stop before op, ']' or end
            re.IGNORECASE,
        )

        def _repl_explicit(m: re.Match[str]) -> str:
            return m.group(1) + m.group(2) + new_item_name

        expr2 = explicit_pattern.sub(_repl_explicit, expr)

        # Bare contextual item references: OldItem
        bare_pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){item_pat}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        expr2 = bare_pattern.sub(new_item_name, expr2)
        return expr2

    def _rename_cube_prefix_in_expression(self, expr: str, old_name: str, new_name: str) -> str:
        """Rewrite optional sheet/cube prefixes like Cube::Dim.Item.

        The evaluator ignores the sheet/cube prefix today, but we still keep
        rules textually consistent when cubes are renamed.
        """

        if not expr or not old_name or old_name.lower() == new_name.lower():
            return expr
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(old_name)}(\s*::)",
            re.IGNORECASE,
        )
        return pattern.sub(lambda m: new_name + m.group(1), expr)

    def _rename_dimension_in_all_rules(self, old_name: str, new_name: str) -> None:
        if not old_name or old_name.lower() == new_name.lower():
            return
        for rid, r in list(self._ws.rules.items()):
            if not r.is_anchored:
                continue
            expr = r.expression
            new_expr = self._rename_dimension_in_expression(expr, old_name, new_name)
            if new_expr != expr:
                self._ws.rules[rid] = type(r)(
                    id=r.id,
                    cube_id=r.cube_id,
                    expression=new_expr,
                    addr_mask=r.addr_mask,
                    targets=r.targets,
                    is_anchored=r.is_anchored,
                )
        for rid, r in list(self._ws.rules.items()):
            expr = r.expression
            new_expr = self._rename_dimension_in_expression(expr, old_name, new_name)
            if new_expr != expr:
                self._ws.rules[rid] = type(r)(
                    id=r.id,
                    cube_id=r.cube_id,
                    expression=new_expr,
                    addr_mask=r.addr_mask,
                )
        self._cell_cache.clear()

    def _rename_dimension_item_in_all_rules(
        self,
        dim_name: str,
        old_item_name: str,
        new_item_name: str,
    ) -> None:
        if not old_item_name or old_item_name.lower() == new_item_name.lower():
            return

        def _rename_targets(
            targets: tuple[tuple[str, str], ...] | None,
        ) -> tuple[tuple[str, str], ...] | None:
            if not targets:
                return targets
            dim_cf = dim_name.casefold()
            old_cf = old_item_name.casefold()
            new_targets: list[tuple[str, str]] = []
            changed = False
            for d, item in targets:
                if d.casefold() == dim_cf and item.casefold() == old_cf:
                    new_targets.append((d, new_item_name))
                    changed = True
                else:
                    new_targets.append((d, item))
            return tuple(new_targets) if changed else targets

        updated_count = 0
        for rid, r in list(self._ws.rules.items()):
            if not r.is_anchored:
                continue
            expr = r.expression
            new_expr = self._rename_dimension_item_in_expression(expr, dim_name, old_item_name, new_item_name)
            new_targets = _rename_targets(r.targets)
            if new_expr != expr or new_targets != r.targets:
                _DEBUG_ENGINE and print(f"DEBUG rule_rename: '{expr}' -> '{new_expr}' (dim={dim_name}, old={old_item_name}, new={new_item_name})")
                self._ws.rules[rid] = type(r)(
                    id=r.id,
                    cube_id=r.cube_id,
                    expression=new_expr,
                    addr_mask=r.addr_mask,
                    targets=new_targets,
                    is_anchored=r.is_anchored,
                )
                updated_count += 1
        _DEBUG_ENGINE and print(f"DEBUG rule_rename: Updated {updated_count} anchored rules for {dim_name}.{old_item_name} -> {new_item_name}")
        for rid, r in list(self._ws.rules.items()):
            expr = r.expression
            new_expr = self._rename_dimension_item_in_expression(expr, dim_name, old_item_name, new_item_name)
            new_targets = _rename_targets(r.targets)
            if new_expr != expr or new_targets != r.targets:
                self._ws.rules[rid] = type(r)(
                    id=r.id,
                    cube_id=r.cube_id,
                    expression=new_expr,
                    addr_mask=r.addr_mask,
                    targets=new_targets,
                    is_anchored=r.is_anchored,
                )
        self._cell_cache.clear()

    def _rename_cube_in_all_rules(self, old_name: str, new_name: str) -> None:
        if not old_name or old_name.lower() == new_name.lower():
            return
        for rid, r in list(self._ws.rules.items()):
            if not r.is_anchored:
                continue
            expr = r.expression
            new_expr = self._rename_cube_prefix_in_expression(expr, old_name, new_name)
            if new_expr != expr:
                self._ws.rules[rid] = type(r)(
                    id=r.id,
                    cube_id=r.cube_id,
                    expression=new_expr,
                    addr_mask=r.addr_mask,
                    targets=r.targets,
                    is_anchored=r.is_anchored,
                )
        for rid, r in list(self._ws.rules.items()):
            expr = r.expression
            new_expr = self._rename_cube_prefix_in_expression(expr, old_name, new_name)
            if new_expr != expr:
                self._ws.rules[rid] = type(r)(
                    id=r.id,
                    cube_id=r.cube_id,
                    expression=new_expr,
                    addr_mask=r.addr_mask,
                )
        self._cell_cache.clear()

    def rename_dimension(self, dim_id: str, new_name: str) -> None:
        dim = self.require_dimension_by_id(dim_id)
        new_name = new_name.strip()
        if not new_name or dim.name == new_name:
            return
        # Check for duplicate dimension names (case-insensitive)
        new_name_lower = new_name.lower()
        for existing_dim in self._ws.dimensions.values():
            if existing_dim.id != dim_id and existing_dim.name.strip().lower() == new_name_lower:
                raise ValueError(f"Dimension with name '{new_name}' already exists")
        old_name = dim.name
        dim.name = new_name
        self._rename_dimension_in_all_rules(old_name, new_name)
        if self._on_dimension_renamed is not None:
            self._on_dimension_renamed()
        # Phase E: Publish domain event for GUI adapter
        self._publish_event(EVENT_DIMENSION_RENAMED, {
            "dimension_id": dim_id,
            "old_name": old_name,
            "new_name": new_name,
        })

    def rename_dimension_item(self, dim_id: str, item_id: str, new_name: str) -> None:
        """Rename a dimension item while respecting frozen DimensionItem instances.

        DimensionItem is a frozen dataclass, so we cannot mutate fields in-place.
        Instead, replace the entry in Dimension.items with a new instance that
        carries the same id and updated name, and propagate the change into all
        rule and rule expressions.
        """

        dim = self.require_dimension_by_id(dim_id)
        new_name = new_name.strip()
        if not new_name:
            return
        with self._engine_lock:
            for it in dim.items:
                if it.id != item_id and it.name.strip().casefold() == new_name.casefold():
                    raise ValueError(f"Duplicate item name in dimension '{dim.name}': {new_name}")
            # Also check against group labels in the graph (if bootstrapped)
            try:
                from lib_openm.graph_mutation import _all_nodes_for_dim
                for node in _all_nodes_for_dim(dim_id, self._ws):
                    if node["kind"] == "GROUP":
                        if str(node.get("label", "")).strip().casefold() == new_name.casefold():
                            raise ValueError(f"Duplicate item name in dimension '{dim.name}': {new_name}")
            except RuntimeError:
                pass  # System cubes not bootstrapped; skip graph check
            old_item_name: str | None = None
            for it in dim.items:
                if it.id == item_id:
                    if it.name == new_name:
                        return
                    old_item_name = it.name
                    break
            if old_item_name is None:
                raise KeyError(item_id)

            # Graph-first: use rename_dimension_item primitive when graph is in sync
            from lib_openm.graph_mutation import rename_dimension_item as _rename_graph
            from lib_openm.graph_mutation import _dim_by_name, _cube_by_name
            has_system_cubes = (
                _dim_by_name(self._ws, "%RECNODADR") is not None
                and _cube_by_name(self._ws, "%RECNOD") is not None
            )
            if has_system_cubes:
                _rename_graph(dim_id, item_id, new_name, self._ws)
            else:
                # No graph yet; rename in dim.items and dim.outline
                for idx, it in enumerate(dim.items):
                    if it.id == item_id:
                        dim.items[idx] = DimensionItem(id=it.id, name=new_name)
                        break
                self._rename_item_in_outline(dim, item_id, new_name)

            self._rename_dimension_item_in_all_rules(dim.name, old_item_name, new_name)
            self._function_cache.clear()
            self._invalidate_hierarchy_rules_for_dim(dim_id)
            if hasattr(self, '_invalidate_view_model'):
                self._invalidate_view_model()
            if self._on_dimension_item_renamed is not None:
                self._on_dimension_item_renamed(dim_id, item_id, new_name)
            # Phase E: Publish domain event for GUI adapter
            self._publish_event(
                EVENT_DIMENSION_ITEM_RENAMED,
                {
                    "dimension_id": dim_id,
                    "item_id": item_id,
                    "old_label": old_item_name,
                    "new_label": new_name,
                },
            )

    def _rename_outline_group_label(self, dim_id: str, old_label: str, new_label: str) -> None:
        """Rename an outline group label and update all rules that reference it.
        
        This is used when renaming group headers in the matrix grid. Group labels
        can be referenced in rules (e.g., Dimension.GroupName), so we need to
        update all rule expressions when they change.
        """
        dim = self.require_dimension_by_id(dim_id)
        new_label = new_label.strip()
        old_label = old_label.strip()
        if not new_label or not old_label or old_label.lower() == new_label.lower():
            return
        
        # Check for duplicates: group label cannot match any item name
        for it in dim.items:
            if it.name.strip().casefold() == new_label.casefold():
                raise ValueError(f"Group label '{new_label}' conflicts with existing item name in dimension '{dim.name}'")
        
        # Check for duplicates: group label must be unique among all group labels in the outline
        def _get_all_group_labels(nodes: list[Any]) -> set[str]:
            labels: set[str] = set()
            for n in nodes:
                label = getattr(n, "label", None)
                children = getattr(n, "children", None)
                if isinstance(label, str) and label:
                    labels.add(label.strip().casefold())
                if isinstance(children, list) and children:
                    labels.update(_get_all_group_labels(children))
            return labels
        
        all_group_labels = _get_all_group_labels(dim.outline)
        # Remove old_label from the check since we're renaming FROM it
        all_group_labels.discard(old_label.strip().casefold())
        if new_label.casefold() in all_group_labels:
            raise ValueError(f"Duplicate group label in dimension '{dim.name}': {new_label}")
        
        # Check for duplicates: group label cannot match another item name (case-insensitive)
        for it in dim.items:
            if it.name.strip().casefold() == new_label.casefold():
                raise ValueError(f"Group label '{new_label}' conflicts with existing item name in dimension '{dim.name}'")
        
        # Update rules using the same logic as dimension item renames
        self._rename_dimension_item_in_all_rules(dim.name, old_label, new_label)
        # Invalidate hierarchy rules that may reference this group
        self._invalidate_hierarchy_rules_for_dim(dim_id)

    def rename_group_node(self, dim_id: str, node_id: str, new_label: str) -> None:
        """Rename a GROUP node in the graph and update all rule references.

        Validates:
        - node exists and is a GROUP
        - new_label is non-empty
        - new_label does not duplicate another group label in the dimension

        Preserves old label internally so rule reference updates have old → new.
        """
        from lib_openm.graph_mutation import rename_group_node as _rename_group_node_raw
        from lib_openm.graph_mutation import _read_node_meta

        meta = _read_node_meta(node_id, self._ws)
        if meta is None:
            raise ValueError(f"Node not found: {node_id}")
        if meta["kind"] != "GROUP":
            raise ValueError(f"Node {node_id} is not a GROUP")
        if meta.get("dim_id") != dim_id:
            raise ValueError(f"Node {node_id} does not belong to dimension {dim_id}")

        old_label = meta.get("label", "")
        dim = self._ws.get_dimension(dim_id)
        if not dim:
            raise ValueError(f"Dimension not found: {dim_id}")

        _rename_group_node_raw(node_id, new_label, self._ws)

        # Update rule references that use the old label
        self._rename_dimension_item_in_all_rules(dim.name, old_label, new_label)
        self._invalidate_hierarchy_rules_for_dim(dim_id)
        if hasattr(self, '_invalidate_view_model'):
            self._invalidate_view_model()

    def resolve_item_node_id(self, dim_id: str, item_id: str) -> str:
        """Resolve an item_id to its ITEM_REF node_id, creating the node if absent.

        Phase 7 bridge: GUI must not call ensure_item_node directly.
        This Engine method owns the resolution so the boundary stays clean.
        """
        from lib_openm.outline_graph_bridge import ensure_item_node
        return ensure_item_node(dim_id, item_id, self._ws)

    def place_item_nodes(
        self,
        dim_id: str,
        item_ids: list[str],
        parent_node_id: str | None,
        anchor_node_id: str | None = None,
        position: str = "after",
    ) -> list[str]:
        """Resolve item_ids to ITEM_REF graph nodes and place them in the hierarchy.

        Internally: item_ids → ensure_item_nodes → move_nodes(...)
        Returns created/resolved node_ids.
        """
        from lib_openm.outline_graph_bridge import ensure_item_node
        node_ids = []
        for item_id in item_ids:
            node_id = ensure_item_node(dim_id, item_id, self._ws)
            node_ids.append(node_id)
        self.move_nodes(dim_id, node_ids, parent_node_id, anchor_node_id=anchor_node_id, position=position, reduce_enclosed_groups=True)
        return node_ids

    def create_group(
        self,
        dim_id: str,
        label: str,
        parent_group_id: str | None = None,
        child_item_ids: list[str] | None = None,
    ) -> str:
        """Create a new GROUP node in the graph.

        Validates that the label does not conflict with existing group labels
        or dimension item names. Optionally attaches the group under a parent
        and places child items under it.

        Returns the new group node_id.
        """
        from lib_openm.graph_mutation import (
            create_group_node as _create_group_node,
            attach_edge,
            _root_level_nodes,
            _set_node_root_ord,
        )

        dim = self._ws.get_dimension(dim_id)
        if not dim:
            raise ValueError(f"Dimension not found: {dim_id}")

        # Validate against dimension item names
        clean_name = label.strip().casefold()
        for it in dim.items:
            if it.name.strip().casefold() == clean_name:
                raise ValueError(f"An item named '{label}' already exists in this dimension.")

        # Ensure all dimension items have graph nodes before mutating.
        # If an outline exists but no graph data yet, migrate it to preserve
        # existing structure. Otherwise leave flat items unmaterialized;
        # they remain in dim.items and do not require graph nodes.
        outline = list(getattr(dim, "outline", []) or [])
        from lib_openm.outline_graph_bridge import _has_graph_data
        if outline and not _has_graph_data(dim_id, self._ws):
            from lib_openm.outline_graph_bridge import migrate_outline_to_graph
            migrate_outline_to_graph(dim, self._ws)

        # Capture root-level order before any mutations so we can preserve it.
        roots_before = [n["node_id"] for n in _root_level_nodes(dim_id, self._ws)]

        group_id = _create_group_node(dim_id, label, self._ws)

        if parent_group_id is not None:
            attach_edge(dim_id, group_id, parent_group_id, "MEMBER_OF", 0, ws=self._ws)

        if child_item_ids:
            self.place_item_nodes(dim_id, child_item_ids, parent_node_id=group_id, position="last")

        # For root-level groups, preserve the original root order.
        # The group should appear where the first selected item was.
        if parent_group_id is None:
            from lib_openm.outline_graph_bridge import ensure_item_node
            grouped_node_ids = set()
            if child_item_ids:
                for cid in child_item_ids:
                    nid = ensure_item_node(dim_id, cid, self._ws)
                    grouped_node_ids.add(nid)

            # Build desired root order: replace grouped root nodes with the group
            desired_roots = []
            for node_id in roots_before:
                if node_id in grouped_node_ids:
                    if group_id not in desired_roots:
                        desired_roots.append(group_id)
                elif node_id != group_id:
                    desired_roots.append(node_id)

            # If the group wasn't inserted (e.g., children were not at root), append it
            if group_id not in desired_roots:
                desired_roots.append(group_id)

            # Apply explicit root ORD so the rebuild preserves visual order
            for i, node_id in enumerate(desired_roots):
                _set_node_root_ord(node_id, i, self._ws)

        # Graph mutations write directly to Cube.set() bypassing the Engine
        # cache. Ensure views reading %RECNOD / %RECEDG get fresh data.
        self._clear_cell_cache()

        return group_id

    def create_aggregate_item(self, dim_id: str, group_node_id: str, name: str) -> AddAggregateItemResult:
        """Create a new dimension item that aggregates a group.

        Canonical method. Validates naming, creates the item, ensures graph
        node, and creates the AGGREG_OF edge. Returns AddAggregateItemResult
        with item, node, edge, and group identifiers.
        Raises ValueError on naming conflict.
        """
        from lib_openm.outline_graph_bridge import ensure_item_node, _add_aggregate_edge
        from lib_openm.graph_mutation import (
            _all_edges_for_dim,
            _read_node_meta,
        )
        from lib_utils.ids import new_id

        dim = self._ws.get_dimension(dim_id)
        if not dim:
            raise ValueError(f"Dimension not found: {dim_id}")

        # Validate: must not conflict with existing group
        clean_name = name.casefold().strip()
        def _collect_group_labels(nodes):
            for n in nodes:
                if n.item_id is None:
                    if n.label.casefold() == clean_name:
                        raise ValueError(f"A group named '{name}' already exists in this dimension.")
                    if n.children:
                        yield from _collect_group_labels(n.children)
        outline = getattr(dim, 'outline', None) or []
        for _ in _collect_group_labels(outline):
            pass  # exception raised inside

        # Validate: must not conflict with existing dimension item
        for it in dim.items:
            if it.name.strip().casefold() == clean_name:
                raise ValueError(f"An item named '{name}' already exists in this dimension.")

        # Create dimension item
        item = dim.add_item(name)

        # Ensure graph node
        item_node_id = ensure_item_node(dim_id, item.id, self._ws, label=name)

        # Check for existing AGGREG_OF edge to avoid duplicates
        for edge in _all_edges_for_dim(dim_id, self._ws):
            if edge["kind"] == "AGGREG_OF" and edge["src"] == item_node_id and edge["tgt"] == group_node_id:
                raise ValueError(f"'{name}' is already an aggregate item for this group.")

        # Determine order: place after existing children
        order = 0
        for edge in _all_edges_for_dim(dim_id, self._ws):
            if edge["tgt"] == group_node_id and edge["kind"] in ("MEMBER_OF", "AGGREG_OF"):
                order = max(order, (edge["ord"] if edge["ord"] is not None else 0) + 1)

        aggregate_edge_id = _add_aggregate_edge(item_node_id, group_node_id, dim_id, order, self._ws)

        # Invalidate cache so next read rebuilds from graph
        dim.invalidate_outline_cache()

        return AddAggregateItemResult(
            item_id=item.id,
            item_name=item.name,
            item_node_id=item_node_id,
            aggregate_edge_id=aggregate_edge_id,
            group_node_id=group_node_id,
        )

    def rename_cube(self, cube_id: str, new_name: str) -> None:
        cube = self.require_cube_by_id(cube_id)
        new_name = new_name.strip()
        if not new_name or cube.name == new_name:
            return
        old_name = cube.name
        cube.name = new_name
        self._rename_cube_in_all_rules(old_name, new_name)

    def attach_dimension_to_cube(
        self,
        cube_id: str,
        dim_id: str,
        default_item_id: str | None = None,
    ) -> None:
        cube = self.require_cube_by_id(cube_id)
        if dim_id in cube.dimension_ids:
            dim = self.require_dimension_by_id(dim_id)
            raise ValueError(f"Dimension '{dim.name}' (id={dim_id}) is already attached to cube '{cube.name}'")

        proposed_source_dims = list(cube.dimension_ids) + [dim_id]
        for rule in self._ws.rules.values():
            target_cube = self.require_cube_by_id(rule.cube_id)
            if not self._is_whole_cube_rule_mask(target_cube, rule.addr_mask, rule.targets):
                continue
            expr = self._normalize_expression(rule.expression)
            refs = self._extract_trace_refs(expr)
            for cube_name, pairs in refs:
                if cube_name is None:
                    continue
                if not any(dim_name == "*" and item_name == "*" for dim_name, item_name in pairs):
                    continue
                source_cube = self._find_cube_by_name(cube_name)
                if source_cube is None or source_cube.id != cube.id:
                    continue
                if target_cube.id == cube.id:
                    continue
                missing_dim_ids = [did for did in proposed_source_dims if did not in set(target_cube.dimension_ids)]
                if not missing_dim_ids:
                    continue
                missing_dim_names = [self.require_dimension_by_id(did).name for did in missing_dim_ids if did in self._ws.dimensions]
                missing_text = ", ".join(missing_dim_names or missing_dim_ids)
                raise RuleValidationError(
                    f"Cannot attach dimension to cube {cube.name!r}: whole-cube mapping rule in {target_cube.name!r} "
                    f"(rule {rule.id}) would become invalid because target cube is missing [{missing_text}]."
                )

        dim = self.require_dimension_by_id(dim_id)
        if not dim.items:
            raise ValueError(f"Dimension {dim.name!r} has no items")

        if default_item_id is None:
            default_item_id = dim.items[0].id

        if default_item_id not in {it.id for it in dim.items}:
            raise KeyError(default_item_id)

        # Append the dimension at the end to keep existing addresses stable.
        cube.dimension_ids.append(dim_id)

        # Ensure every view on this cube includes the new dimension. If the view
        # does not already use the dimension on rows or columns, place it on the
        # page axis so other views inherit the new structural dimension.
        for view in self._ws.views.values():
            if view.cube_id != cube_id:
                continue
            if dim_id in view.row_dim_ids or dim_id in view.col_dim_ids or dim_id in view.page_dim_ids:
                continue
            view.page_dim_ids.append(dim_id)
            view.page_selections[dim_id] = default_item_id
            self._publish_event(EVENT_VIEW_LAYOUT_CHANGED, {
                "view_id": view.id,
                "cube_id": view.cube_id,
            })

        # Migrate sparse data: only set for DEFAULT (first) item of new dimension
        # Data values should only appear at the default intersection, not replicated
        if cube.data:
            new_data: dict[tuple[str, ...], Any] = {}
            dim = self._ws.dimensions[dim_id]
            default_item_id = dim.items[0].id if dim.items else ""
            for addr, v in cube.data.items():
                # Only add data for the default item, not all items
                new_addr = tuple(addr) + (default_item_id,)
                new_data[new_addr] = v
            cube.data = new_data

        # Migrate user_override_addrs: only set for DEFAULT (first) item of new dimension
        # Hardcoded values should only appear at the default intersection, not all items
        if cube.user_override_addrs:
            new_overrides: set[tuple[str, ...]] = set()
            dim = self._ws.dimensions[dim_id]
            default_item_id = dim.items[0].id if dim.items else ""
            for addr in cube.user_override_addrs:
                # Only add override for the default item, not all items
                new_overrides.add(tuple(addr) + (default_item_id,))
            cube.user_override_addrs = new_overrides

        # Migrate anchored rules for this cube similarly.
        dim = self._ws.dimensions.get(dim_id)
        for rid, r in list(self._ws.rules.items()):
            if not r.is_anchored or r.cube_id != cube_id:
                continue
            if r.addr_mask is None or len(r.addr_mask) != len(cube.dimension_ids) - 1:
                continue
            self._ws.rules[rid] = type(r)(
                id=r.id,
                cube_id=r.cube_id,
                expression=r.expression,
                addr_mask=tuple(r.addr_mask) + (default_item_id,),
                targets=r.targets,
                is_anchored=r.is_anchored,
            )

        # Dimension count changed and rules were updated; stale cached masks
        # must be rebuilt before the next rule lookup.
        self._ws._invalidate_rule_index()

        # The dependency graph nodes for this cube are keyed by the old
        # dimensionality and will no longer match the new addresses. Remove
        # them and re-evaluate the cube's rule cells so the snapshot path
        # can read clean, tracked values across the new dimension.
        self._dep_graph.remove_nodes_with_prefix(f"cell::{cube_id}::")
        self._tracked_nodes = {
            k for k in self._tracked_nodes if not k.startswith(f"cell::{cube_id}::")
        }
        self._cell_cache.clear()
        self._bootstrap_dependency_graph(only_missing=True)

    def analyze_detach_dimension_from_cube(self, cube_id: str, dim_id: str) -> dict[str, Any]:
        """Analyze the impact of detaching a dimension from a cube.

        Returns counts of data cells, anchored rules, and rules that will be affected.
        """
        cube = self.require_cube_by_id(cube_id)
        if dim_id not in cube.dimension_ids:
            return {"data_cells": 0, "anchored_rules": 0, "rules": 0}

        dim_slot = cube.dimension_ids.index(dim_id)

        # Count data cells that have this dimension
        data_cells = sum(
            1 for addr in cube.data.keys()
            if len(addr) > dim_slot
        )

        # Count anchored rules that reference this dimension slot
        anchored_rules = 0
        for rid, r in self._ws.rules.items():
            if r.cube_id != cube_id:
                continue
            if not r.is_anchored or r.addr_mask is None:
                continue
            if len(r.addr_mask) > dim_slot:
                anchored_rules += 1
        
        # Count rules that target this dimension
        rules = 0
        for rid, r in self._ws.rules.items():
            if r.cube_id != cube_id:
                continue
            # Check if this rule's target involves this dimension
            for target_dim_id, _ in (r.targets or []):
                if target_dim_id == dim_id:
                    rules += 1
                    break
        
        return {
            "data_cells": data_cells,
            "anchored_rules": anchored_rules,
            "rules": rules,
        }

    def detach_dimension_from_cube(self, cube_id: str, dim_id: str) -> None:
        """Detach a dimension from a specific cube, deleting all associated data."""
        cube = self.require_cube_by_id(cube_id)
        if dim_id not in cube.dimension_ids:
            return
        
        # Get dimension info early for later use
        dim = self._ws.dimensions.get(dim_id)

        # Debug: check user_override_addrs state

        proposed_target_dims = [did for did in cube.dimension_ids if did != dim_id]
        proposed_target_dim_set = set(proposed_target_dims)
        for rule in self._ws.rules.values():
            if rule.cube_id != cube.id:
                continue
            if not self._is_whole_cube_rule_mask(cube, rule.addr_mask, rule.targets):
                continue
            expr = self._normalize_expression(rule.expression)
            refs = self._extract_trace_refs(expr)
            for cube_name, pairs in refs:
                if cube_name is None:
                    continue
                if not any(dim_name == "*" and item_name == "*" for dim_name, item_name in pairs):
                    continue
                source_cube = self._find_cube_by_name(cube_name)
                if source_cube is None:
                    continue
                source_dim_ids = proposed_target_dims if source_cube.id == cube.id else list(source_cube.dimension_ids)
                missing_dim_ids = [did for did in source_dim_ids if did not in proposed_target_dim_set]
                if not missing_dim_ids:
                    continue
                missing_dim_names = [self.require_dimension_by_id(did).name for did in missing_dim_ids if did in self._ws.dimensions]
                missing_text = ", ".join(missing_dim_names or missing_dim_ids)
                raise RuleValidationError(
                    f"Cannot remove dimension from cube {cube.name!r}: whole-cube mapping rule {rule.id} depends on "
                    f"source cube {source_cube.name!r} dimensions [{missing_text}] being present in {cube.name!r}."
                )
        idx = cube.dimension_ids.index(dim_id)
        removed_dim_id = dim_id  # Save for later use in rule updates
        cube.dimension_ids.pop(idx)

        # Delete all data cells that reference the detached dimension
        # This is like deleting the dimension globally, but only for this cube
        if cube.data:
            prev_count = len(cube.data)
            # Keep only addresses that DON'T have the detached dimension (shorter addresses)
            cube.data = {
                addr: v for addr, v in cube.data.items()
                if len(addr) <= idx  # Only keep addresses shorter than detached dim position
            }

        # Record diagnostics for anchored rules that reference the removed dimension.
        # Expression text is preserved; the evaluator surfaces CellError("#REF!")
        # when the detached dimension can no longer be resolved.
        if dim and dim.name:
            import re
            dim_pattern = rf'\b{re.escape(dim.name)}\.([A-Za-z_][A-Za-z0-9_]*)\b'
            for rid, r in list(self._ws.rules.items()):
                if not r.is_anchored or r.cube_id != cube_id:
                    continue
                for match in re.finditer(dim_pattern, r.expression, flags=re.IGNORECASE):
                    ref = match.group(0)
                    self._add_rule_diagnostic(rid, ref, "dimension", dim.id)


        # Update views to drop the dimension and ensure row/col axes remain populated
        for view in self._ws.views.values():
            if view.cube_id != cube_id:
                continue
            view.row_dim_ids = [d for d in view.row_dim_ids if d != dim_id]
            view.col_dim_ids = [d for d in view.col_dim_ids if d != dim_id]
            view.page_dim_ids = [d for d in view.page_dim_ids if d != dim_id]

            remaining = [d for d in cube.dimension_ids if d not in view.page_dim_ids]
            used = set(view.row_dim_ids) | set(view.col_dim_ids) | set(view.page_dim_ids)
            if not view.row_dim_ids and remaining:
                for d in remaining:
                    if d not in used:
                        view.row_dim_ids = [d]
                        used.add(d)
                        break
            if not view.col_dim_ids and len(remaining) > 1:
                for d in remaining:
                    if d not in used:
                        view.col_dim_ids = [d]
                        break

        # Record diagnostics for rules referencing the detached dimension and
        # update addr_mask so it stays aligned with the new cube.dimension_ids.
        if dim and dim.name and self._ws.rules:
            import re
            dim_pattern = rf'\b{re.escape(dim.name)}\.([A-Za-z_][A-Za-z0-9_]*)\b'
            for rid, r in list(self._ws.rules.items()):
                if r.cube_id != cube_id:
                    continue

                needs_update = False
                new_addr_mask = r.addr_mask
                new_targets = r.targets

                # Record diagnostics for targets that reference the detached dimension.
                if r.targets and dim and dim.name:
                    for t_dim_name, t_item_name in r.targets:
                        if t_dim_name.lower() == dim.name.lower():
                            self._add_rule_diagnostic(rid, f"{t_dim_name}.{t_item_name}", "dimension", dim.id)

                # Record diagnostics for expression references to the detached dimension.
                for match in re.finditer(dim_pattern, r.expression, flags=re.IGNORECASE):
                    ref = match.group(0)
                    self._add_rule_diagnostic(rid, ref, "dimension", dim.id)

                # Update addr_mask to remove the detached dimension's slot.
                if r.addr_mask and len(r.addr_mask) > idx:
                    mask_list = list(r.addr_mask)
                    mask_list.pop(idx)
                    new_addr_mask = tuple(mask_list)
                    needs_update = True

                if needs_update:
                    self._ws.rules[rid] = type(r)(
                        id=r.id,
                        cube_id=r.cube_id,
                        expression=r.expression,
                        addr_mask=new_addr_mask,
                        targets=new_targets,
                    )

        # Drop page selections pointing to this dimension
        for view in self._ws.views.values():
            view.page_selections = {
                d_id: item_id
                for d_id, item_id in view.page_selections.items()
                if d_id != dim_id
            }

        # Clear caches to ensure UI reflects changes
        self._cell_cache.clear()
        
        # Invalidate cube to trigger rule re-evaluation on next access
        self._invalidate_cube(cube.id)

        # Dimension count changed and rules were updated; stale cached masks
        # must be rebuilt before the next rule lookup.
        self._ws._invalidate_rule_index()

        # The dependency graph nodes for this cube are keyed by the old
        # dimensionality and will no longer match the new addresses. Remove them
        # so stale nodes do not cause SnapshotInvariantError, and clear caches so
        # rule cells are re-evaluated on next access with the updated dimensionality.
        # We intentionally do NOT run a full bootstrap here; detach is expected to
        # delete data referencing the removed dimension and leave the cube empty
        # until a user access or recalculation triggers evaluation.
        self._dep_graph.remove_nodes_with_prefix(f"cell::{cube_id}::")
        self._tracked_nodes = {
            k for k in self._tracked_nodes if not k.startswith(f"cell::{cube_id}::")
        }
        self._cell_cache.clear()

        print(f"[DEBUG detach_dim END] cube.data={len(cube.data)}, overrides={len(cube.user_override_addrs) if hasattr(cube, 'user_override_addrs') else 'N/A'}")

    def _promote_row_to_col(self, view_id: str) -> None:
        """Drag row chip up to top-right: row dim becomes the new col; old col stays top-right.

        After this: new row dim = old col dim; new col dim = old row dim.
        i.e. the two simply swap, which is the natural rotate-axes operation.
        """
        view = self.require_view_by_id(view_id)
        # Back-compat: swap the primary (first) row/col dims.
        if not view.row_dim_ids or not view.col_dim_ids:
            return
        view.row_dim_ids[0], view.col_dim_ids[0] = (view.col_dim_ids[0], view.row_dim_ids[0])

    def _promote_topright_to_row(self, view_id: str, dim_id: str) -> None:
        """Drag a top-right chip down to the bottom row slot.

        The dragged dim becomes the new row dim.  The old row dim becomes col.
        Any extra page dims stay as top-right chips unchanged.
        Raises KeyError if dim_id is not a top-right (col or page) dim.
        """
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)
        top_right_dims = [
            did for did in cube.dimension_ids
            if did not in set(view.row_dim_ids)
        ]
        if dim_id not in top_right_dims:
            raise KeyError(f"{dim_id} is not a top-right dimension of this view")
        if not view.row_dim_ids:
            view.row_dim_ids = [dim_id]
            return
        old_row = view.row_dim_ids[0]
        view.row_dim_ids[0] = dim_id
        if view.col_dim_ids and dim_id == view.col_dim_ids[0]:
            view.col_dim_ids[0] = old_row

    def set_view_axes(self, view_id: str, row_dimension_id: str, col_dimension_id: str) -> None:
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)
        if row_dimension_id == col_dimension_id:
            raise ValueError("Row and column dimensions must be different")
        if row_dimension_id not in cube.dimension_ids:
            raise KeyError(row_dimension_id)
        if col_dimension_id not in cube.dimension_ids:
            raise KeyError(col_dimension_id)
        view.row_dim_ids = [row_dimension_id]
        view.col_dim_ids = [col_dimension_id]

    def _axis_items(self, dim_id: str) -> list[DimensionItem]:
        return list(self.require_dimension_by_id(dim_id).items)

    def view_row_items(self, view_id: str) -> list[DimensionItem]:
        view = self.require_view_by_id(view_id)
        if not view.row_dim_ids:
            return []
        return self._axis_items(view.row_dim_ids[0])

    def view_row_dim_ids(self, view_id: str) -> list[str]:
        return list(self.require_view_by_id(view_id).row_dim_ids)

    def view_col_dim_ids(self, view_id: str) -> list[str]:
        return list(self.require_view_by_id(view_id).col_dim_ids)

    def view_page_dim_ids(self, view_id: str) -> list[str]:
        return list(self.require_view_by_id(view_id).page_dim_ids)

    def move_view_dimension(
        self,
        view_id: str,
        dim_id: str,
        dest: str,
        index: int | None = None,
    ) -> None:
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)
        if dim_id not in cube.dimension_ids:
            raise KeyError(dim_id)
        if dest not in ("row", "col", "page"):
            raise ValueError(dest)

        def _remove(lst: list[str]) -> None:
            while dim_id in lst:
                lst.remove(dim_id)

        _remove(view.row_dim_ids)
        _remove(view.col_dim_ids)
        _remove(view.page_dim_ids)

        target = view.row_dim_ids if dest == "row" else view.col_dim_ids if dest == "col" else view.page_dim_ids
        if index is None or index < 0 or index > len(target):
            target.append(dim_id)
        else:
            target.insert(index, dim_id)

        used = set(view.row_dim_ids) | set(view.col_dim_ids) | set(view.page_dim_ids)
        for did in cube.dimension_ids:
            if did not in used:
                view.page_dim_ids.append(did)

        self._publish_event(EVENT_VIEW_LAYOUT_CHANGED, {
            "view_id": view_id,
            "cube_id": view.cube_id,
        })

    def view_col_items(self, view_id: str) -> list[DimensionItem]:
        view = self.require_view_by_id(view_id)
        if not view.col_dim_ids:
            return []
        return self._axis_items(view.col_dim_ids[0])

    def view_col_count(self, view_id: str) -> int:
        return len(self.view_col_keys(view_id))

    def view_page_dimensions(self, view_id: str) -> list[Dimension]:
        view = self.require_view_by_id(view_id)
        return [self.require_dimension_by_id(did) for did in view.page_dim_ids if did in self._ws.dimensions]

    def _cartesian_item_ids(self, dim_ids: list[str]) -> list[tuple[str, ...]]:
        if not dim_ids:
            return [()]
        dims = [self.require_dimension_by_id(did) for did in dim_ids]
        result = [()]
        for d in dims:
            result = [(*k, it.id) for k in result for it in d.items]
        return result

    def _outline_leaf_order(self, nodes: list[Any]) -> list[str]:
        out: list[str] = []

        def _walk(ns: list[Any]) -> None:
            for n in ns:
                item_id = getattr(n, "item_id", None)
                children = getattr(n, "children", None)
                if isinstance(children, list) and children:
                    _walk(children)
                elif isinstance(item_id, str):
                    out.append(item_id)

        if isinstance(nodes, list):
            _walk(nodes)
        return out

    def _sort_keys_by_axis_order(
        self, keys: list[tuple[str, ...]], dim_ids: list[str]
    ) -> list[tuple[str, ...]]:
        """Sort Cartesian keys by each dimension's outline leaf order.

        The outermost dimension (first in dim_ids) is the primary sort key.
        Dimensions without an outline fall back to their item order.
        """
        positions: list[dict[str, int]] = []
        for dim_id in dim_ids:
            dim = self.require_dimension_by_id(dim_id)
            outline = list(self._ws.get_outline(dim.id) or [])
            if outline:
                order = self._outline_leaf_order(outline)
                if order:
                    positions.append({iid: i for i, iid in enumerate(order)})
                    continue
            positions.append({it.id: i for i, it in enumerate(dim.items)})

        def _sort_key(key: tuple[str, ...]) -> tuple[int, ...]:
            return tuple(
                positions[i].get(key[i], len(positions[i])) for i in range(len(dim_ids))
            )

        return sorted(keys, key=_sort_key)

    def _outline_label_paths(self, nodes: list[Any]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}

        def _walk(ns: list[Any], prefix: list[str]) -> None:
            for n in ns:
                label = getattr(n, "label", None)
                item_id = getattr(n, "item_id", None)
                children = getattr(n, "children", None)
                next_prefix = prefix + ([label] if isinstance(label, str) and label else [])
                if isinstance(children, list) and children:
                    _walk(children, next_prefix)
                elif isinstance(item_id, str):
                    out[item_id] = next_prefix

        if isinstance(nodes, list):
            _walk(nodes, [])
        return out

    def view_row_keys(self, view_id: str) -> list[tuple[str, ...]]:
        view = self.require_view_by_id(view_id)
        keys = self._cartesian_item_ids(list(view.row_dim_ids))
        dim_ids = list(view.row_dim_ids)
        if len(dim_ids) == 1:
            dim = self.require_dimension_by_id(dim_ids[0])
            # Use get_outline() so stale cache is rebuilt; dim.outline may lag behind _outline_cache
            outline = list(self._ws.get_outline(dim.id) or []) or list(getattr(view, "row_outline", None) or [])
            if outline:
                order = self._outline_leaf_order(outline)
                if order:
                    pos = {iid: i for i, iid in enumerate(order)}
                    if all(len(k) == 1 for k in keys):
                        keys = sorted(keys, key=lambda k: pos.get(k[0], 10**9))
            return keys

        if len(dim_ids) > 1:
            keys = self._sort_keys_by_axis_order(keys, dim_ids)
        return keys

    def view_col_keys(self, view_id: str) -> list[tuple[str, ...]]:
        view = self.require_view_by_id(view_id)
        keys = self._cartesian_item_ids(list(view.col_dim_ids))
        dim_ids = list(view.col_dim_ids)
        if len(dim_ids) == 1:
            dim = self.require_dimension_by_id(dim_ids[0])
            # Use get_outline() so stale cache is rebuilt; dim.outline may lag behind _outline_cache
            outline = list(self._ws.get_outline(dim.id) or []) or list(getattr(view, "col_outline", None) or [])
            if outline:
                order = self._outline_leaf_order(outline)
                if order:
                    pos = {iid: i for i, iid in enumerate(order)}
                    if all(len(k) == 1 for k in keys):
                        keys = sorted(keys, key=lambda k: pos.get(k[0], 10**9))
            return keys

        if len(dim_ids) > 1:
            keys = self._sort_keys_by_axis_order(keys, dim_ids)
        return keys

    def view_col_header(self, view_id: str, col_index: int) -> str:
        """Return the header label for a given column index."""
        view = self.require_view_by_id(view_id)
        col_keys = self.view_col_keys(view_id)
        if col_index >= len(col_keys):
            return ""
        col_key = col_keys[col_index]
        # Build label from dimension item names
        labels: list[str] = []
        for dim_id in view.col_dim_ids:
            dim = self.require_dimension_by_id(dim_id)
            idx = view.col_dim_ids.index(dim_id)
            if idx < len(col_key):
                item_id = col_key[idx]
                item = next((it for it in dim.items if it.id == item_id), None)
                if item:
                    labels.append(item.name)
        return " | ".join(labels) if labels else ""

    def _get_page_item_id(self, view_id: str, dim_id: str) -> str | None:
        """Return the current page item for a dimension, reading from the workspace view.

        Page selections are canonical workspace metadata; the Engine does not
        cache them. This is a private helper for internal address construction.
        """
        view = self._ws.views.get(view_id)
        if view is None:
            return None
        if dim_id == "@":
            return view.page_selections.get("@", CHANNEL_TO_AT_ID["value"])
        item_id = view.page_selections.get(dim_id)
        if item_id is not None:
            return item_id
        dim = self.require_dimension_by_id(dim_id)
        if not dim.items:
            return None
        return dim.items[0].id

    def _addr_for_view_rc(self, view_id: str, row: int, col: int) -> tuple[str, ...]:
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)

        row_keys = self.view_row_keys(view_id)
        col_keys = self.view_col_keys(view_id)
        row_key = row_keys[row]
        col_key = col_keys[col]

        return self._addr_for_view_ids(view_id, row_key=row_key, col_key=col_key)

    def _addr_for_view_ids(
        self,
        view_id: str,
        row_key: tuple[str, ...],
        col_key: tuple[str, ...],
        channel: str | None = None,
    ) -> tuple[str, ...]:
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)

        # Build an address aligned to the cube's dimension order.
        # Row/col dims come from the table coordinates; any other dims use the current page selection.
        row_index = {did: i for i, did in enumerate(view.row_dim_ids)}
        col_index = {did: i for i, did in enumerate(view.col_dim_ids)}
        addr: list[str] = []
        for dim_id in cube.dimension_ids:
            if dim_id in row_index:
                i = row_index[dim_id]
                if 0 <= i < len(row_key):
                    addr.append(row_key[i])
                else:
                    addr.append(self._get_page_item_id(view_id, dim_id))
            elif dim_id in col_index:
                i = col_index[dim_id]
                if 0 <= i < len(col_key):
                    addr.append(col_key[i])
                else:
                    addr.append(self._get_page_item_id(view_id, dim_id))
            elif dim_id == "@":
                # @ is a page dimension — caller can override channel
                if channel:
                    addr.append(CHANNEL_TO_AT_ID.get(channel, CHANNEL_TO_AT_ID["value"]))
                else:
                    addr.append(self._get_page_item_id(view_id, dim_id))
            else:
                addr.append(self._get_page_item_id(view_id, dim_id))
        return tuple(addr)

    def _get_cell_value_by_ids(
        self,
        view_id: str,
        row_key: tuple[str, ...],
        col_key: tuple[str, ...],
        channel: str | None = None,
    ) -> CellValue:
        # Defensive: clear any stale eval stack state
        self._thread_eval_stack().clear()
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)
        addr = self._addr_for_view_ids(view_id, row_key=row_key, col_key=col_key, channel=channel)
        # Always fetch fresh value from cube - no caching to avoid stale data
        v = cube.get(addr)
        if v is not None:
            rule = self._ws.find_anchored_rule(cube.id, addr)
            if rule is None:
                rule = self._ws.find_rule(cube.id, addr, cube.dimension_ids)
            # Check if this is a manual override (user explicitly set this cell)
            is_override = cube.is_user_override(addr)
            if rule is not None:
                expr = self._normalize_expression(rule.expression)
                # Only "override" if user manually entered; otherwise it's "rule"
                source = "override" if is_override else "rule"
                return CellValue(
                    value=v,
                    explain=Explain(source=source, cube_id=cube.id, addr=addr, rule_body=expr),
                )
            return CellValue(value=v, explain=Explain(source="input", cube_id=cube.id, addr=addr))

        rule = self._ws.find_anchored_rule(cube.id, addr)
        if rule is None:
            rule = self._ws.find_rule(cube.id, addr, cube.dimension_ids)
            if rule is None:
                return CellValue(value=None, explain=Explain(source="empty", cube_id=cube.id, addr=addr))
            resolver = self._make_resolver(cube)
            expr = self._normalize_expression(rule.expression)
            key = (cube.id, addr)
            eval_stack = self._thread_eval_stack()
            eval_stack.add(key)
            try:
                computed = self._rule_evaluator.eval(expr, resolver=resolver, base_addr=addr)
                return CellValue(
                    value=computed,
                    explain=Explain(source="rule", cube_id=cube.id, addr=addr, rule_body=expr),
                )
            except ZeroDivisionError:
                return CellValue(
                    value=CellError("#DIV/0!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#DIV/0!"),
                )
            except OverflowError:
                return CellValue(
                    value=CellError("#RANGE!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#RANGE!"),
                )
            except TypeError:
                return CellValue(
                    value=CellError("#VALUE!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#VALUE!"),
                )
            except CircularReferenceError:
                return CellValue(
                    value=CellError("#CIRC!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#CIRC!"),
                )
            except SyntaxError:
                return CellValue(
                    value=CellError("#SYNTAX!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#SYNTAX!"),
                )
            except RuleValidationError:
                return CellValue(
                    value=CellError("#EXPRESSION!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#EXPRESSION!"),
                )
            except Exception:
                return CellValue(
                    value=CellError("#EXPRESSION!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#EXPRESSION!"),
                )
            finally:
                eval_stack.discard(key)

        # Cell rule evaluation path
        resolver = self._make_resolver(cube)
        expr = self._normalize_expression(rule.expression)
        key = (cube.id, addr)
        eval_stack = self._thread_eval_stack()
        eval_stack.add(key)
        try:
            computed = self._rule_evaluator.eval(expr, resolver=resolver, base_addr=addr)
            return CellValue(
                value=computed,
                explain=Explain(source="rule", cube_id=cube.id, addr=addr, rule_body=expr),
            )
        except ZeroDivisionError:
            return CellValue(
                value=CellError("#DIV/0!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#DIV/0!"),
            )
        except OverflowError:
            return CellValue(
                value=CellError("#NUM!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#NUM!"),
            )
        except TypeError:
            return CellValue(
                value=CellError("#VALUE!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#VALUE!"),
            )
        except CircularReferenceError:
            return CellValue(
                value=CellError("#CIRC!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#CIRC!"),
            )
        except SyntaxError:
            return CellValue(
                value=CellError("#SYNTAX!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#SYNTAX!"),
            )
        except RuleValidationError:
            return CellValue(
                value=CellError("#EXPRESSION!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#EXPRESSION!"),
            )
        except Exception:
            return CellValue(
                value=CellError("#EXPRESSION!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#EXPRESSION!"),
            )
        finally:
            eval_stack.discard(key)

    def _set_cell_hardvalue_by_ids(
        self, view_id: str, row_key: tuple[str, ...], col_key: tuple[str, ...], value: Any,
        channel: str | None = None,
    ) -> None:
        if _DEBUG_SET_CELL:
            print(f"DEBUG SET_CELL_BY_KEYS: called view_id={view_id}, row_key={row_key}, col_key={col_key}, value={value}")
            import traceback
            traceback.print_stack(limit=5)
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)
        addr = self._addr_for_view_ids(view_id, row_key=row_key, col_key=col_key, channel=channel)
        if _DEBUG_SET_CELL:
            print(f"DEBUG SET_CELL_BY_KEYS: addr={addr}")
        prev = cube.get(addr)
        has_override = cube.is_user_override(addr)
        if prev == value and not has_override:
            if _DEBUG_SET_CELL:
                print(f"DEBUG SET_CELL_BY_KEYS: prev == value, returning")
            return
        if _DEBUG_SET_CELL:
            print(f"DEBUG SET_CELL_BY_KEYS: pushing undo action, prev={prev}, after={value}")
        self._undo.push_and_do(_CellEditAction(engine=self, cube=cube, addr=addr, before=prev, after=value))

    def _clear_cell_hardvalue_by_ids(
        self, view_id: str, row_key: tuple[str, ...], col_key: tuple[str, ...],
        channel: str | None = None,
    ) -> None:
        """
        Remove any explicit override so the underlying rule body/rule (if any) is revealed.
        """
        self._set_cell_hardvalue_by_ids(view_id, row_key=row_key, col_key=col_key, value=None, channel=channel)

    # ------------------------------------------------------------------
    # Private helpers: name-based lookup
    # ------------------------------------------------------------------

    def _resolve_item_ids_by_name(
        self, view_id: str, axis: str, names: list[str]
    ) -> tuple[str, ...]:
        """Resolve human-readable item names to stable item IDs for a view axis.

        Args:
            view_id: Stable view identifier.
            axis: "row" or "col".
            names: List of item names, one per dimension on the axis, in view order.

        Returns:
            Tuple of stable item IDs aligned to the axis dimension order.

        Raises:
            ValueError: If a name is ambiguous or not found in its dimension.
        """
        view = self.require_view_by_id(view_id)
        dim_ids = view.row_dim_ids if axis == "row" else view.col_dim_ids
        if len(names) != len(dim_ids):
            raise ValueError(
                f"Expected {len(dim_ids)} {axis} names, got {len(names)}"
            )
        item_ids: list[str] = []
        for dim_id, name in zip(dim_ids, names):
            if dim_id == "@":
                item_ids.append(CHANNEL_TO_AT_ID.get(name, name))
                continue
            dim = self._ws.dimensions.get(dim_id)
            if dim is None:
                raise ValueError(f"Dimension not found: {dim_id}")
            matches = [it.id for it in dim.items if it.name == name]
            if len(matches) == 1:
                item_ids.append(matches[0])
            elif len(matches) > 1:
                raise ValueError(f"Ambiguous item name {name!r} in dimension {dim.name}")
            else:
                raise ValueError(f"Item {name!r} not found in dimension {dim.name}")
        return tuple(item_ids)

    def _addr_for_view_name(
        self, view_id: str, row_names: list[str], col_names: list[str]
    ) -> tuple[str, ...]:
        """Resolve view-relative names to a canonical cube address."""
        row_key = self._resolve_item_ids_by_name(view_id, "row", row_names)
        col_key = self._resolve_item_ids_by_name(view_id, "col", col_names)
        return self._addr_for_view_ids(view_id, row_key=row_key, col_key=col_key)

    def _get_cell_value_by_name(
        self, view_id: str, row_names: list[str], col_names: list[str]
    ) -> CellValue:
        row_key = self._resolve_item_ids_by_name(view_id, "row", row_names)
        col_key = self._resolve_item_ids_by_name(view_id, "col", col_names)
        return self._get_cell_value_by_ids(view_id, row_key=row_key, col_key=col_key)

    def _set_cell_hardvalue_by_name(
        self, view_id: str, row_names: list[str], col_names: list[str], value: Any
    ) -> None:
        row_key = self._resolve_item_ids_by_name(view_id, "row", row_names)
        col_key = self._resolve_item_ids_by_name(view_id, "col", col_names)
        self._set_cell_hardvalue_by_ids(view_id, row_key=row_key, col_key=col_key, value=value)

    def _clear_cell_hardvalue_by_name(
        self, view_id: str, row_names: list[str], col_names: list[str]
    ) -> None:
        row_key = self._resolve_item_ids_by_name(view_id, "row", row_names)
        col_key = self._resolve_item_ids_by_name(view_id, "col", col_names)
        self._set_cell_hardvalue_by_ids(view_id, row_key=row_key, col_key=col_key, value=None)

    def _set_rule_anchored_by_name(
        self, view_id: str, row_names: list[str], col_names: list[str], expression: str
    ) -> None:
        row_key = self._resolve_item_ids_by_name(view_id, "row", row_names)
        col_key = self._resolve_item_ids_by_name(view_id, "col", col_names)
        self._set_rule_anchored_by_ids(view_id, row_key=row_key, col_key=col_key, expression=expression)

    def _delete_rule_anchored_by_name(
        self, view_id: str, row_names: list[str], col_names: list[str]
    ) -> bool:
        row_key = self._resolve_item_ids_by_name(view_id, "row", row_names)
        col_key = self._resolve_item_ids_by_name(view_id, "col", col_names)
        return self._delete_rule_anchored_by_ids(view_id, row_key=row_key, col_key=col_key)

    # ------------------------------------------------------------------
    # Private helpers: index-based lookup
    # ------------------------------------------------------------------

    def _get_cell_value_by_idx(self, view_id: str, row_idx: int, col_idx: int) -> CellValue:
        return self.cell_value_for_view_rc(view_id, row=row_idx, col=col_idx)

    def _set_cell_hardvalue_by_idx(self, view_id: str, row_idx: int, col_idx: int, value: Any) -> None:
        cube = self.require_cube_by_id(self.require_view_by_id(view_id).cube_id)
        addr = self._addr_for_view_rc(view_id, row_idx, col_idx)
        prev = cube.get(addr)
        if prev == value:
            return
        self._undo.push_and_do(_CellEditAction(engine=self, cube=cube, addr=addr, before=prev, after=value))

    def _clear_cell_hardvalue_by_idx(self, view_id: str, row_idx: int, col_idx: int) -> None:
        self._set_cell_hardvalue_by_idx(view_id, row_idx, col_idx, value=None)

    def _set_rule_anchored_by_idx(
        self, view_id: str, row_idx: int, col_idx: int, expression: str
    ) -> None:
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)
        addr = self._addr_for_view_rc(view_id, row=row_idx, col=col_idx)
        targets: list[tuple[str, str]] = []
        for dim_id, item_id in zip(cube.dimension_ids, addr):
            if dim_id == "@":
                continue
            dim = self._ws.dimensions.get(dim_id)
            if dim is None:
                continue
            item_name = next((it.name for it in dim.items if it.id == item_id), item_id)
            targets.append((dim.name, item_name))
        self.set_rule(cube.id, targets, expression, is_anchored=True)

    def _delete_rule_anchored_by_idx(self, view_id: str, row_idx: int, col_idx: int) -> bool:
        return self._delete_cell_rule(view_id, row=row_idx, col=col_idx)

    # ------------------------------------------------------------------
    # Public canonical methods (cell_ref dispatch)
    # ------------------------------------------------------------------

    def get_cell_value(self, view_id: str, cell_ref: dict) -> CellValue:
        """Read the current scalar value of a cell (hardvalue or computed)."""
        kind = cell_ref.get("kind", "ids")
        if kind == "ids":
            return self._get_cell_value_by_ids(
                view_id,
                row_key=tuple(cell_ref["row_key"]),
                col_key=tuple(cell_ref["col_key"]),
                channel=cell_ref.get("channel"),
            )
        elif kind == "name":
            return self._get_cell_value_by_name(
                view_id,
                row_names=cell_ref["row_names"],
                col_names=cell_ref["col_names"],
            )
        elif kind == "idx":
            return self._get_cell_value_by_idx(
                view_id, row_idx=cell_ref["row_idx"], col_idx=cell_ref["col_idx"]
            )
        else:
            raise ValueError(f"Unknown cell_ref kind: {kind}")

    def set_cell_hardvalue(self, view_id: str, cell_ref: dict, value: Any) -> None:
        """Set a user hardvalue that overrides rule computation."""
        kind = cell_ref.get("kind", "ids")
        if kind == "ids":
            self._set_cell_hardvalue_by_ids(
                view_id,
                row_key=tuple(cell_ref["row_key"]),
                col_key=tuple(cell_ref["col_key"]),
                value=value,
                channel=cell_ref.get("channel"),
            )
        elif kind == "name":
            self._set_cell_hardvalue_by_name(
                view_id,
                row_names=cell_ref["row_names"],
                col_names=cell_ref["col_names"],
                value=value,
            )
        elif kind == "idx":
            self._set_cell_hardvalue_by_idx(
                view_id, row_idx=cell_ref["row_idx"], col_idx=cell_ref["col_idx"], value=value
            )
        else:
            raise ValueError(f"Unknown cell_ref kind: {kind}")

    def clear_cell_hardvalue(self, view_id: str, cell_ref: dict) -> None:
        """Clear the user hardvalue, revealing the rule-computed value."""
        kind = cell_ref.get("kind", "ids")
        if kind == "ids":
            self._clear_cell_hardvalue_by_ids(
                view_id,
                row_key=tuple(cell_ref["row_key"]),
                col_key=tuple(cell_ref["col_key"]),
                channel=cell_ref.get("channel"),
            )
        elif kind == "name":
            self._clear_cell_hardvalue_by_name(
                view_id,
                row_names=cell_ref["row_names"],
                col_names=cell_ref["col_names"],
            )
        elif kind == "idx":
            self._clear_cell_hardvalue_by_idx(
                view_id, row_idx=cell_ref["row_idx"], col_idx=cell_ref["col_idx"]
            )
        else:
            raise ValueError(f"Unknown cell_ref kind: {kind}")

    def set_rule_anchored(self, view_id: str, cell_ref: dict, expression: str) -> None:
        """Attach a rule anchored to a specific cell."""
        kind = cell_ref.get("kind", "ids")
        if kind == "ids":
            self._set_rule_anchored_by_ids(
                view_id,
                row_key=tuple(cell_ref["row_key"]),
                col_key=tuple(cell_ref["col_key"]),
                expression=expression,
            )
        elif kind == "name":
            self._set_rule_anchored_by_name(
                view_id,
                row_names=cell_ref["row_names"],
                col_names=cell_ref["col_names"],
                expression=expression,
            )
        elif kind == "idx":
            self._set_rule_anchored_by_idx(
                view_id, row_idx=cell_ref["row_idx"], col_idx=cell_ref["col_idx"], expression=expression
            )
        else:
            raise ValueError(f"Unknown cell_ref kind: {kind}")

    def delete_rule_anchored(self, view_id: str, cell_ref: dict) -> bool:
        """Delete the rule whose anchor is this specific cell.

        Only succeeds if a rule is anchored (attached) to this exact cell address.
        If the cell's value is computed by a range rule anchored elsewhere,
        this operation returns ``False`` and does not modify that rule.
        """
        kind = cell_ref.get("kind", "ids")
        if kind == "ids":
            return self._delete_rule_anchored_by_ids(
                view_id,
                row_key=tuple(cell_ref["row_key"]),
                col_key=tuple(cell_ref["col_key"]),
            )
        elif kind == "name":
            return self._delete_rule_anchored_by_name(
                view_id,
                row_names=cell_ref["row_names"],
                col_names=cell_ref["col_names"],
            )
        elif kind == "idx":
            return self._delete_rule_anchored_by_idx(
                view_id, row_idx=cell_ref["row_idx"], col_idx=cell_ref["col_idx"]
            )
        else:
            raise ValueError(f"Unknown cell_ref kind: {kind}")

    def get_cells_batch(
        self,
        cube_id: str,
        addresses: list[tuple[tuple[str, ...], tuple[str, ...]]],
        view_id: str | None = None,
    ) -> dict[tuple[tuple[str, ...], tuple[str, ...]], Any]:
        """Batch fetch multiple cells in a single round-trip (100x faster)."""
        result = {}

        # Get view context if provided, to properly build addresses with page dimensions
        view = self.require_view_by_id(view_id) if view_id else None
        cube = self._ws.cubes.get(cube_id) if view else None
        
        # Debug: show first address being built
        if addresses and view_id:
            row_key, col_key = addresses[0][0], addresses[0][1]
            full_addr = self._addr_for_view_ids(view_id, row_key=row_key, col_key=col_key)
            print(f"[DEBUG get_cells_batch] view_id={view_id[:8]}, first addr: {row_key}+{col_key} -> {full_addr}")
        
        for addr in addresses:
            try:
                row_key, col_key = addr[0], addr[1]
                
                if view and cube:
                    # Use proper address building that includes page dimensions
                    full_addr = self._addr_for_view_ids(view_id, row_key=row_key, col_key=col_key)
                else:
                    # Legacy: just concatenate row + col (doesn't work for 3D+ cubes)
                    full_addr = row_key + col_key
                
                cb = self._ws.cubes.get(cube_id)
                v = cb.get(full_addr) if cb else None
                
                # Debug: show what we're looking up
                if len(addresses) <= 9:  # Only for small batches (grid rendering)
                    print(f"[DEBUG get_cells_batch] addr={addr} -> full_addr={full_addr}, value={v!r}")
                
                # Determine source based on whether cell has a raw value
                source = "input" if v is not None else "empty"
                result[addr] = CellValue(
                    value=v,
                    explain=Explain(source=source, cube_id=cube_id, addr=full_addr)
                )
            except Exception as e:
                print(f"[DEBUG get_cells_batch] ERROR for addr={addr}: {e}")
                result[addr] = None
        return result

    def _addr_sort_key(self, cube: Cube, addr: tuple[str, ...]) -> tuple:
        """Stable sort key for cell addresses based on dimension item indices.

        This ensures ``bootstrap_dependency_graph`` evaluates cells in a
        deterministic order regardless of UUID-based item IDs, making
        volatile-function results reproducible across workspaces with the
        same structure.
        """
        key: list = []
        for dim_id, item_id in zip(cube.dimension_ids, addr):
            if dim_id == "@":
                key.append((0, item_id))
                continue
            dim = self._ws.dimensions.get(dim_id)
            if dim is None:
                key.append((999, item_id))
                continue
            for idx, item in enumerate(dim.items):
                if item.id == item_id:
                    key.append((idx, item_id))
                    break
            else:
                key.append((999, item_id))
        return tuple(key)

    def _extract_rule_cube_refs(
        self, rule: Any
    ) -> tuple[set[str], bool]:
        """Return (cross_cube_refs, has_same_cube_ref) for a rule expression.

        Cross-cube references (e.g. ``Cube::[...]`` or ``Cube::Dim.Item``) are
        returned as a set of cube IDs. Same-cube references (refs without a cube
        qualifier) are not returned as IDs, but the boolean flag is set so the
        caller can order the rule after other rules in the same cube.
        """
        ws = self._ws
        cube_names = {c.name.lower() for c in ws.cubes.values()}
        cross_refs: set[str] = set()
        has_same_cube_ref = False
        try:
            from lib_openm.rule_eval.tokenizer import _tokenise, _TT_REF

            tokens = _tokenise(rule.expression.strip())
            for t in tokens:
                if t.kind == _TT_REF:
                    parts = t.value.split(":", 1)
                    if len(parts) == 2:
                        name = parts[0].strip()
                        if name.lower() in cube_names:
                            # Cross-cube reference; find the cube ID by name.
                            cube_id = next(
                                c.id for c in ws.cubes.values() if c.name.lower() == name.lower()
                            )
                            cross_refs.add(cube_id)
                        else:
                            # Same-cube reference (e.g., Dim:Item or Dim.Item).
                            has_same_cube_ref = True
                    else:
                        # Bare reference token without a cube qualifier.
                        has_same_cube_ref = True
        except (ValueError, TypeError):
            pass
        return cross_refs, has_same_cube_ref

    def _bootstrap_dependency_graph(self, only_missing: bool = False) -> int:
        """Evaluate only cells that must exist in the dependency graph.

        Collects hardvalues and rule-covered addresses per cube, deduplicates,
        then evaluates each target cell once with dependency tracking enabled.
        Empty cells (no rule, no hardvalue) are skipped entirely.

        When *only_missing* is True, addresses that are already cached and have
        recorded dependency edges are skipped.  This is used after a full
        recalculation to fill in newly-visible addresses without re-evaluating
        the entire workspace.

        Returns the total number of target cells evaluated.
        """
        total_evaluated = 0
        cube_list = list(self._ws.cubes.values())
        cube_idx = {c.id: i for i, c in enumerate(cube_list)}

        # 1. Build cube-level dependency graph from cross-cube references in rules.
        cube_refs: dict[str, set[str]] = {c.id: set() for c in cube_list}
        rule_cross_refs: dict[str, set[str]] = {}
        rule_same_cube_ref: dict[str, bool] = {}
        for cube in cube_list:
            for rid in self._ws._cube_ordered_rule_ids(cube.id):
                rule = self._ws.rules[rid]
                cross_refs, has_same = self._extract_rule_cube_refs(rule)
                rule_cross_refs[rule.id] = cross_refs
                rule_same_cube_ref[rule.id] = has_same
                cube_refs[cube.id].update(cross_refs)

        # 2. Compute cube depths: a cube is deeper than any earlier cube it references.
        # This follows the workspace order as a hint, which is usually inputs-first.
        cube_depths: dict[str, int] = {c.id: 0 for c in cube_list}
        for cube in cube_list:
            for ref in cube_refs[cube.id]:
                if ref in cube_idx and cube_idx[ref] < cube_idx[cube.id]:
                    cube_depths[cube.id] = max(cube_depths[cube.id], cube_depths[ref] + 1)

        # 3. Compute per-rule external depth from the deepest external cube it references.
        external_rule_depths: dict[str, int] = {}
        for cube in cube_list:
            for rid in self._ws._cube_ordered_rule_ids(cube.id):
                rule = self._ws.rules[rid]
                cross_refs = rule_cross_refs[rule.id]
                external_refs = {r for r in cross_refs if r != cube.id}
                if external_refs:
                    external_rule_depths[rule.id] = max(cube_depths[r] for r in external_refs) + 1
                else:
                    external_rule_depths[rule.id] = 0

        # 4. Rules that reference other cells in the same cube must be evaluated after
        # all externally-referencing rules in that cube, so the referenced value exists.
        rule_depths: dict[str, int] = {}
        for cube in cube_list:
            cube_external_max = 0
            for rid in self._ws._cube_ordered_rule_ids(cube.id):
                rule = self._ws.rules[rid]
                cube_external_max = max(cube_external_max, external_rule_depths[rule.id])
            for rid in self._ws._cube_ordered_rule_ids(cube.id):
                rule = self._ws.rules[rid]
                if rule_same_cube_ref.get(rule.id, False):
                    rule_depths[rule.id] = cube_external_max + 1
                else:
                    rule_depths[rule.id] = external_rule_depths[rule.id]

        # 5. Collect all target cells across all cubes with their topological depth.
        all_targets: list[tuple[int, int, tuple, Cube, Any, tuple[str, ...]]] = []
        for cube in cube_list:
            targets: set[tuple[str, ...]] = set()
            addr_to_rule: dict[tuple[str, ...], Any] = {}

            # Hardvalues
            for addr in cube.user_override_addrs:
                targets.add(addr)

            # Rule-covered addresses (expand each rule's mask)
            cube_rule_ids = self._ws._cube_ordered_rule_ids(cube.id)
            cube_rule_masks = self._ws._cube_rule_masks.get(cube.id, []) if self._ws._cube_rule_masks is not None else []
            for idx, rid in enumerate(cube_rule_ids):
                rule = self._ws.rules[rid]
                mask = cube_rule_masks[idx] if idx < len(cube_rule_masks) else self._ws._effective_rule_mask(rule, cube.dimension_ids, cube)
                if mask is None:
                    continue
                wildcard_dims = [i for i, m in enumerate(mask) if m is None]
                if not wildcard_dims:
                    targets.add(mask)
                    addr_to_rule[mask] = rule
                else:
                    item_lists = [
                        [item.id for item in self._ws.dimensions[cube.dimension_ids[dim_idx]].items]
                        for dim_idx in wildcard_dims
                    ]
                    for combo in itertools.product(*item_lists):
                        addr = list(mask)
                        for dim_idx, item_id in zip(wildcard_dims, combo):
                            addr[dim_idx] = item_id
                        taddr = tuple(addr)
                        targets.add(taddr)
                        addr_to_rule[taddr] = rule

            for addr in targets:
                rule = addr_to_rule.get(addr)
                if rule is not None and cube.is_user_override(addr):
                    rule = None
                depth = rule_depths.get(rule.id, 0) if rule is not None else 0
                all_targets.append((depth, cube_idx[cube.id], self._addr_sort_key(cube, addr), cube, rule, addr))

        # 5. Evaluate in topological order: depth, then cube order, then address order.
        all_targets.sort(key=lambda t: (t[0], t[1], t[2]))
        old_tracking = self._dep_tracking_enabled
        self._dep_tracking_enabled = True
        try:
            for depth, idx, sort_key, cube, rule, addr in all_targets:
                if only_missing:
                    # Skip cells already computed and tracked.
                    if cube.get(addr) is not None:
                        node_key = self._cell_node_key(cube.id, addr)
                        if self._dep_graph.has_precedents(node_key):
                            continue
                self._get_cell_by_addr(cube, addr, rule=rule)
                total_evaluated += 1
        finally:
            self._dep_tracking_enabled = old_tracking

        return total_evaluated

    def recalculate_all(self, *, include_all: bool = True) -> None:
        """Force a full recalculation sweep of the workspace."""
        import time

        # Try to acquire lock without blocking - if calculation is already in progress, skip this call
        if not self._calc_lock.acquire(blocking=False):
            print("[ENGINE] recalculate_all skipped - calculation already in progress")
            return

        try:
            self._calc_in_progress = True
            print(f"\n[ENGINE] recalculate_all started (include_all={include_all})")
            print(f"         Workspace: {len(self._ws.cubes)} cubes, {len(self._ws.dimensions)} dimensions")

            # Phase 1: Clear ALL caches to ensure fresh recalculation
            # This MUST happen before recompute_dirty_nodes to avoid stale cached values
            t0 = time.perf_counter()
            self._clear_cell_cache()
            self._function_cache.clear()
            self._slice_cache.clear()
            # Clear volatile cache so RAND/RANDBETWEEN generate new values on recalculate
            self._volatile_func_cache.clear()
            # CRITICAL: Clear rule cell values from cube.data to force re-evaluation
            # with dependency tracking enabled.
            for cube in self._ws.cubes.values():
                for addr in list(cube.data.keys()):
                    if addr not in cube.user_override_addrs:
                        has_rule_body = self._ws.find_anchored_rule(cube.id, addr) is not None
                        has_rule = self._ws.find_rule(cube.id, addr, cube.dimension_ids) is not None
                        if has_rule_body or has_rule:
                            cube.set(addr, None)
            t1 = time.perf_counter()
            print(f"[ENGINE] Phase 1 - clear caches: {(t1-t0)*1000:.1f} ms")

            # Phase 2: Recompute all dirty dependency-graph nodes in topological order.
            # When include_all=True every graph node is marked dirty, so this covers
            # all rule cells, slices, and functions already known to the graph.
            t0 = time.perf_counter()
            nodes_recomputed = self.recompute_dirty_nodes(include_all=include_all)
            t1 = time.perf_counter()
            print(f"[ENGINE] Phase 2 - recompute_dirty_nodes: {(t1-t0)*1000:.1f} ms ({nodes_recomputed} nodes)")

            # Phase 3: Cold-start bootstrap.
            # If the dependency graph is empty (no cells ever evaluated), build it
            # by evaluating only hardvalues and rule-covered addresses.
            if nodes_recomputed == 0:
                t0 = time.perf_counter()
                bootstrapped = self._bootstrap_dependency_graph()
                t1 = time.perf_counter()
                print(f"[ENGINE] Phase 3 - _bootstrap_dependency_graph (cold-start): {(t1-t0)*1000:.1f} ms ({bootstrapped} cells)")

            print(f"[ENGINE] recalculate_all completed\n")
        finally:
            self._calc_in_progress = False
            self._calc_lock.release()

    def bootstrap_dependency_graph(self) -> dict[str, Any]:
        """Fully build the dependency graph on workspace load.

        Evaluates every hardvalue and rule-covered cell once with tracking
        enabled so the GUI can paint read-only snapshots without evaluating
        rules. Bumps the workspace generation to 1.

        Returns a dict with the number of cells evaluated and the duration.
        """
        t0 = time.perf_counter()
        evaluated = self._bootstrap_dependency_graph(only_missing=False)
        self.bump_generation()
        self._gui_ready = True
        dt_ms = (time.perf_counter() - t0) * 1000.0
        self._dep_metrics["bootstrap_cells"] = evaluated
        self._dep_metrics["bootstrap_ms"] = int(dt_ms)
        return {
            "evaluated": evaluated,
            "duration_ms": dt_ms,
        }

    def _make_resolver(self, cube: Cube) -> CubeResolver:
        """Build a CubeResolver bound to this workspace and cube."""
        cached = self._resolver_cache.get(cube.id)
        if cached is not None:
            return cached
        ws = self._ws
        engine_ref = self

        # Precompute workspace-wide case-insensitive lookups to avoid repeated str.lower().
        cube_name_lower_to_cube = {c.name.lower(): c for c in ws.cubes.values()}
        dim_name_lower_to_dim = {d.name.lower(): d for d in ws.dimensions.values()}
        dim_item_name_to_id_lower: dict[str, dict[str, str]] = {}
        dim_item_id_set: dict[str, set[str]] = {}
        for d in ws.dimensions.values():
            name_map: dict[str, str] = {}
            for it in d.items:
                name_map[it.name.lower()] = it.id
            dim_item_name_to_id_lower[d.id] = name_map
            dim_item_id_set[d.id] = {it.id for it in d.items}

        # Precompute source-cube slot maps for cross-cube address seeding.
        source_id_to_slot_full = {dim_id: idx for idx, dim_id in enumerate(cube.dimension_ids)}
        source_name_to_slot_full: dict[str, int] = {}
        for idx, dim_id in enumerate(cube.dimension_ids):
            dim_obj = ws.dimensions.get(dim_id)
            if dim_obj is not None:
                source_name_to_slot_full[dim_obj.name.lower()] = idx
        source_id_to_slot_no_at = source_id_to_slot_full
        source_name_to_slot_no_at = source_name_to_slot_full
        if "@" in cube.dimension_ids:
            non_at_dim_ids = [dim_id for dim_id in cube.dimension_ids if dim_id != "@"]
            source_id_to_slot_no_at = {
                dim_id: idx for idx, dim_id in enumerate(non_at_dim_ids)
            }
            source_name_to_slot_no_at = {
                dim_obj.name.lower(): idx
                for idx, dim_id in enumerate(non_at_dim_ids)
                for dim_obj in [ws.dimensions.get(dim_id)]
                if dim_obj is not None
            }
        source_name_lower = cube.name.lower()

        def _select_cube_and_seed_addr(
            cube_name: str | None,
            base_addr: tuple[str, ...],
        ) -> tuple[Cube, list[str]]:
            """Return (target_cube, addr_vec) for a possibly cross-cube reference.

            For same-cube references this simply returns ``(cube, list(base_addr))``.
            For cross-cube references it constructs an address aligned to the
            target cube's ``dimension_ids`` by copying coordinates for any
            shared dimensions (by id) and defaulting to the first item of
            dimensions that are not present in the source cube.
            """

            if cube_name is None or not cube_name.strip():
                # For backward compatibility: pad shorter addresses with @.value
                addr_vec = list(base_addr)
                if len(addr_vec) < len(cube.dimension_ids) and "@" in cube.dimension_ids:
                    at_dim = ws.dimensions.get("@")
                    if at_dim and at_dim.items:
                        addr_vec.insert(0, at_dim.items[0].id)
                return cube, addr_vec

            # Handle wildcard cube prefix
            if cube_name == "*":
                addr_vec = list(base_addr)
                if len(addr_vec) < len(cube.dimension_ids) and "@" in cube.dimension_ids:
                    at_dim = ws.dimensions.get("@")
                    if at_dim and at_dim.items:
                        addr_vec.insert(0, at_dim.items[0].id)
                return cube, addr_vec

            if cube_name.lower() == source_name_lower:
                addr_vec = list(base_addr)
                if len(addr_vec) < len(cube.dimension_ids) and "@" in cube.dimension_ids:
                    at_dim = ws.dimensions.get("@")
                    if at_dim and at_dim.items:
                        addr_vec.insert(0, at_dim.items[0].id)
                return cube, addr_vec

            target_cube = cube_name_lower_to_cube.get(cube_name.lower())
            if target_cube is None:
                raise KeyError(f"Unknown cube: {cube_name!r}")

            # Pick precomputed source-cube slot maps. For shorter base_addr skip @
            # so the remaining slots line up with the source address elements.
            if len(base_addr) < len(cube.dimension_ids) and "@" in cube.dimension_ids:
                id_to_slot = source_id_to_slot_no_at
                name_to_slot = source_name_to_slot_no_at
            else:
                id_to_slot = source_id_to_slot_full
                name_to_slot = source_name_to_slot_full

            addr_vec: list[str] = []
            for dim_id in target_cube.dimension_ids:
                dim_obj = ws.dimensions[dim_id]
                # Prefer exact dimension id match (same cube or shared dimension ids).
                src_slot = id_to_slot.get(dim_id)
                # Fallback to matching on dimension name when ids differ between cubes.
                if src_slot is None and dim_obj is not None:
                    src_slot = name_to_slot.get(dim_obj.name.lower())

                # For @ dimension with shorter address, use default; otherwise use src_slot if valid
                if src_slot is not None and len(base_addr) == len(id_to_slot):
                    addr_vec.append(base_addr[src_slot])
                    continue

                if not dim_obj.items:
                    raise KeyError(
                        f"Dimension {dim_obj.name!r} in cube {target_cube.name!r} has no items"
                    )
                addr_vec.append(dim_obj.items[0].id)
            
            # Ensure returned address is full length (includes @ dimension)
            if len(addr_vec) < len(target_cube.dimension_ids) and "@" in target_cube.dimension_ids:
                at_dim = ws.dimensions.get("@")
                if at_dim and at_dim.items:
                    addr_vec.insert(0, at_dim.items[0].id)
            
            return target_cube, addr_vec

        def _node_label(node: Any) -> str | None:
            if hasattr(node, "label"):
                return getattr(node, "label")
            if isinstance(node, dict):
                return node.get("label")
            return None

        def _node_children(node: Any) -> list[Any]:
            if hasattr(node, "children"):
                children = getattr(node, "children")
            elif isinstance(node, dict):
                children = node.get("children")
            else:
                children = None
            return list(children or [])

        def _node_item_id(node: Any) -> str | None:
            if hasattr(node, "item_id"):
                return getattr(node, "item_id")
            if isinstance(node, dict):
                return node.get("item_id")
            return None

        def _collect_outline_leaf_item_ids(node: Any) -> list[str]:
            ids: list[str] = []
            item_id = _node_item_id(node)
            if item_id:
                ids.append(item_id)
            for child in _node_children(node):
                ids.extend(_collect_outline_leaf_item_ids(child))
            return ids

        def _find_outline_group_item_ids(dim: Dimension, label_lower: str) -> list[str] | None:
            """Find group by label and return leaf item IDs.

            Tries canonical graph store first, falls back to dim.outline."""
            from lib_openm.outline_graph_bridge import (
                find_group_node_id_by_label,
                get_group_all_leaf_items,
            )

            # 1. Try canonical graph store first
            group_id = find_group_node_id_by_label(dim.id, label_lower, ws)
            if group_id is not None:
                item_ids = get_group_all_leaf_items(dim.id, group_id, ws)
                if item_ids:
                    seen: list[str] = []
                    for item_id in item_ids:
                        if item_id not in seen:
                            seen.append(item_id)
                    return seen
                raise KeyError(
                    f"Group {label_lower!r} in dimension {dim.name!r} contains no leaf items"
                )

            # 2. Fallback to dim.outline (backward compatibility)
            if not dim.outline:
                return None

            def _search(nodes: list[Any]) -> list[str] | None:
                for node in nodes:
                    node_label = (_node_label(node) or "").strip().lower()
                    if node_label == label_lower:
                        ids = [item_id for item_id in _collect_outline_leaf_item_ids(node) if item_id]
                        if not ids:
                            raise KeyError(
                                f"Group {label_lower!r} in dimension {dim.name!r} contains no leaf items"
                            )
                        seen: list[str] = []
                        for item_id in ids:
                            if item_id not in seen:
                                seen.append(item_id)
                        return seen
                    found = _search(_node_children(node))
                    if found is not None:
                        return found
                return None

            return _search(list(dim.outline))

        def _dimension_item_ids_for_name(dim: Dimension, item_name: str) -> list[str] | None:
            token = item_name.strip()
            if not token:
                return None
            if token == "*":
                return [it.id for it in dim.items]
            token_lower = token.lower()
            # Fast lookup by precomputed name/id maps for this dimension.
            name_map = dim_item_name_to_id_lower.get(dim.id)
            if name_map is not None:
                item_id = name_map.get(token_lower)
                if item_id is not None:
                    return [item_id]
                if token in dim_item_id_set.get(dim.id, set()):
                    return [token]
            return _find_outline_group_item_ids(dim, token_lower)

        def _label_for_slot(target_cube: Cube, seeded_addr: list[str], slot: int) -> str:
            if not (0 <= slot < len(target_cube.dimension_ids)):
                raise KeyError(f"Invalid dimension slot: {slot}")
            if not (0 <= slot < len(seeded_addr)):
                raise KeyError(f"Address slot out of range: {slot}")
            dim_id = target_cube.dimension_ids[slot]
            dim = ws.dimensions[dim_id]
            item_id = seeded_addr[slot]
            target_item = next((it for it in dim.items if it.id == item_id), None)
            if target_item is None:
                raise KeyError(
                    f"Unknown item id {item_id!r} for dimension {dim.name!r} in cube {target_cube.name!r}",
                )
            return target_item.name

        class _Resolver(CubeResolver):
            def _split_range_item_name(self, item: str) -> tuple[str, str]:
                dyn_depth = 0
                i = 0
                n = len(item)
                while i < n - 1:
                    ch = item[i]
                    nxt = item[i + 1]

                    if ch == "$" and nxt == "<":
                        dyn_depth += 1
                        i += 2
                        continue

                    if ch == ">" and dyn_depth > 0:
                        dyn_depth -= 1
                        i += 1
                        continue

                    if ch == "." and nxt == "." and dyn_depth == 0:
                        start_raw = item[:i].strip()
                        end_raw = item[i + 2 :].strip()
                        return start_raw, end_raw

                    i += 1

                parts = [s.strip() for s in item.split("..", 1)]
                if len(parts) == 2:
                    return parts[0], parts[1]
                return item, ""

            def _resolve_range_bound(
                self,
                raw: str,
                dim_name_for_error: str,
                base_addr: tuple[str, ...],
            ) -> str:
                text = raw.strip()
                if text.startswith("$<") and text.endswith(">"):
                    inner = text[2:-1].strip()
                    if not inner:
                        raise RuleValidationError(
                            f"Empty dynamic range bound {raw!r} in dimension {dim_name_for_error!r}",
                        )

                    if ".." in inner:
                        raise RuleValidationError(
                            f"Dynamic bound $<{inner}> must resolve to a single cell; "
                            f"range syntax not allowed in dimension {dim_name_for_error!r}"
                        )
                    import re as _re

                    if _re.search(r"[:\[,]\s*\*\s*(?:[,\]]|$)", inner):
                        raise RuleValidationError(
                            f"Dynamic bound $<{inner}> must resolve to a single cell; "
                            f"wildcard not allowed in dimension {dim_name_for_error!r}"
                        )
                    prev_flag = getattr(self, "_in_dynamic_bound", False)
                    self._in_dynamic_bound = True
                    try:
                        try:
                            value = engine_ref._rule_evaluator.eval(
                                inner,
                                resolver=self,
                                base_addr=base_addr,
                            )
                        except Exception as e:  # pragma: no cover
                            try:
                                print(
                                    "DEBUG dynamic_bound_eval_error:",
                                    f"inner={inner!r}",
                                    f"error={type(e).__name__}: {e}",
                                )
                            except Exception:
                                pass
                            raise
                    finally:
                        self._in_dynamic_bound = prev_flag

                    if isinstance(value, CellError):
                        return value
                    if value is None:
                        raise RuleValidationError(
                            f"Dynamic range bound {raw!r} in dimension {dim_name_for_error!r} "
                            f"evaluated to an empty value",
                        )

                    if isinstance(value, (int, float)):
                        if isinstance(value, float) and value.is_integer():
                            value = int(value)
                        return str(value)
                    return str(value)

                return text

            def memoize_function_call(
                self,
                fn_name: str,
                signature: str,
                base_addr: tuple[str, ...],
                compute: Callable[[], Any],
            ) -> Any:
                return engine_ref._evaluate_function_node(fn_name, signature, base_addr, compute, cube.id)

            def cache_volatile_call(
                self,
                fn_name: str,
                signature: str,
                base_addr: tuple[str, ...],
                call_number: int,
                compute: Callable[[], Any],
            ) -> Any:
                """Cache volatile function results (RAND, RANDBETWEEN) across paint events.

                Volatile functions return cached values until dirty nodes are detected,
                ensuring consistent values during rendering while still updating on changes.
                Each call site gets a unique sequence number for unique values.
                """
                # Include cell address and call_number in cache key so each cell gets
                # its own random value, and multiple RAND() calls in same rule
                # get different values
                addr_token = ",".join(base_addr) if base_addr else ""
                cache_key = f"{fn_name}::{signature}::{addr_token}::{call_number}"
                if cache_key in engine_ref._volatile_func_cache:
                    return engine_ref._volatile_func_cache[cache_key]
                result = compute()
                engine_ref._volatile_func_cache[cache_key] = result
                return result

            def resolve_ref(
                self,
                dim_name: str,
                item_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> float:
                _DEBUG_ENGINE and print(f"DEBUG resolve_ref ENTRY: dim_name={dim_name!r}, item_name={item_name!r}, cube_name={cube_name!r}")
                # When called from a dynamic bound ($<...>), enforce single-cell resolution.
                if getattr(self, "_in_dynamic_bound", False):
                    if ".." in item_name:
                        raise RuleValidationError(
                            f"Dynamic bound $<...> must resolve to a single cell; "
                            f"range syntax not allowed in dimension {dim_name!r}"
                        )
                    if item_name == "*" and dim_name != "*":
                        raise RuleValidationError(
                            f"Dynamic bound $<...> must resolve to a single cell; "
                            f"wildcard not allowed in dimension {dim_name!r}"
                        )
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)

                # If @ dimension exists and is NOT explicitly specified (dim_name != "@"),
                # default to @.value instead of inheriting from base_addr
                if "@" in target_cube.dimension_ids and dim_name.lower() != "@":
                    at_slot = target_cube.dimension_ids.index("@")
                    at_dim = ws.dimensions.get("@")
                    if at_dim and at_dim.items:
                        seeded_addr = list(seeded_addr)
                        while len(seeded_addr) < len(target_cube.dimension_ids):
                            seeded_addr.append("")
                        seeded_addr[at_slot] = at_dim.items[0].id  # @.value

                same_cube = target_cube is cube

                if dim_name == "*" and item_name == "*":
                    # Whole-cube wildcard reference (Cube::*.*) — re-use the
                    # caller's coordinates mapped into the target cube. This
                    # is primarily used for cross-cube lookups where the
                    # caller wants "whatever Drivers has at this address".
                    if same_cube:
                        return _coerce_num(cube.get(base_addr))
                    # Pad address for backward compatibility
                    lookup_addr_seeded = tuple(seeded_addr)
                    if len(seeded_addr) < len(target_cube.dimension_ids) and "@" in target_cube.dimension_ids:
                        at_dim = ws.dimensions.get("@")
                        if at_dim and at_dim.items:
                            lookup_addr_seeded = (at_dim.items[0].id, *seeded_addr)
                    return _coerce_num(engine_ref._get_cell_by_addr(target_cube, lookup_addr_seeded))

                dim = next(
                    (d for d in ws.dimensions.values() if d.name.lower() == dim_name.lower()),
                    None,
                )
                # Fallback: if dimension not found, try interpreting dim_name as a cube name
                # This handles bare Cube.Item syntax (e.g., "valuation.equityvalue")
                _DEBUG_ENGINE and print(f"[DEBUG REF] dim_name={dim_name!r} item_name={item_name!r} - trying as cube reference")
                if dim is None:
                    # Try to find a cube with this name
                    target_cube_by_name = next(
                        (c for c in ws.cubes.values() if c.name.lower() == dim_name.lower()),
                        None,
                    )
                    _DEBUG_ENGINE and print(f"DEBUG resolve_ref: found cube={target_cube_by_name.name if target_cube_by_name else None!r} for dim_name={dim_name!r}")
                    if target_cube_by_name is not None:
                        # Found a cube - treat item_name as an item reference in that cube
                        
                        # First, find which dimension contains the target item
                        target_dim_id = None
                        target_item_id = None
                        for dim_id in target_cube_by_name.dimension_ids:
                            dim = ws.dimensions[dim_id]
                            if dim is None:
                                continue
                            target_item = next(
                                (it for it in dim.items if it.name.lower() == item_name.lower()),
                                None,
                            )
                            if target_item is not None:
                                target_dim_id = dim_id
                                target_item_id = target_item.id
                                _DEBUG_ENGINE and print(f"DEBUG resolve_ref: found item {item_name!r} in dim {dim.name!r} (id={dim_id!r}), item_id={target_item_id!r}")
                                break
                        
                        if target_item_id is None:
                            # Item not found in any dimension of the cube
                            _DEBUG_ENGINE and print(f"DEBUG: Item {item_name!r} not found in cube {dim_name!r}")
                            return 0.0
                        
                        # Build address: for the dimension containing the item, use that item.
                        # For other dimensions, try to match from base_addr if dimensions are shared,
                        # otherwise use the first item as default.
                        new_addr = []
                        for dim_id in target_cube_by_name.dimension_ids:
                            if dim_id == target_dim_id:
                                # This is the dimension containing our target item
                                new_addr.append(target_item_id)
                            else:
                                # Try to find this dimension in the source cube's dimensions
                                target_dim = ws.dimensions[dim_id]
                                src_slot = None
                                if target_dim is not None:
                                    # Try match by dimension ID
                                    for src_idx, src_dim_id in enumerate(cube.dimension_ids):
                                        if src_dim_id == dim_id:
                                            src_slot = src_idx
                                            break
                                    # Try match by dimension name
                                    if src_slot is None:
                                        for src_idx, src_dim_id in enumerate(cube.dimension_ids):
                                            src_dim = ws.dimensions.get(src_dim_id)
                                            if src_dim is not None and src_dim.name.lower() == target_dim.name.lower():
                                                src_slot = src_idx
                                                break
                                
                                if src_slot is not None and len(base_addr) == len(cube.dimension_ids):
                                    new_addr.append(base_addr[src_slot])
                                elif target_dim is not None and target_dim.items:
                                    new_addr.append(target_dim.items[0].id)
                                else:
                                    new_addr.append("")
                        
                        _DEBUG_ENGINE and print(f"DEBUG resolve_ref: built new_addr={new_addr!r} for cube {target_cube_by_name.name!r}")
                        lookup_addr = tuple(new_addr)
                        if len(new_addr) < len(target_cube_by_name.dimension_ids) and "@" in target_cube_by_name.dimension_ids:
                            at_dim = ws.dimensions.get("@")
                            if at_dim and at_dim.items:
                                lookup_addr = (at_dim.items[0].id, *new_addr)
                        raw_val = engine_ref._get_cell_by_addr(target_cube_by_name, lookup_addr)
                        _DEBUG_ENGINE and print(f"DEBUG resolve_ref: _get_cell_by_addr returned {raw_val!r}")
                        return _coerce_num(raw_val)
                    # No cube found either - raise original error
                    raise KeyError(f"Unknown dimension: {dim_name!r}")
                if dim.id not in target_cube.dimension_ids:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                slot = target_cube.dimension_ids.index(dim.id)

                item_upper = item_name.upper()
                target_item = None

                seq_keywords_enabled = getattr(self, "_allow_seq_keywords", False)

                if seq_keywords_enabled and item_upper in {"THIS", "PREV", "NEXT", "FIRST", "LAST"}:
                    if dim.dim_type != "seq":
                        raise ValueError(
                            f"Dimension {dim.name!r} is not sequential (dim_type='seq' required for THIS/PREV/NEXT/FIRST/LAST)"
                        )
                    curr_id = (base_addr if same_cube else seeded_addr)[slot]
                    idx = dim.item_index(curr_id)
                    if item_upper == "THIS":
                        # THIS must return the raw stored value when
                        # referencing the same cube (to avoid false circular
                        # references) but should evaluate normally for
                        # cross-cube references.
                        if same_cube:
                            new_addr = list(base_addr)
                            new_addr[slot] = dim.items[idx].id
                            return _coerce_num(cube.get(tuple(new_addr)))
                        new_addr = list(seeded_addr)
                        new_addr[slot] = dim.items[idx].id
                        lookup_addr = tuple(new_addr)
                        if len(new_addr) < len(target_cube.dimension_ids) and "@" in target_cube.dimension_ids:
                            at_dim = ws.dimensions.get("@")
                            if at_dim and at_dim.items:
                                lookup_addr = (at_dim.items[0].id, *new_addr)
                        return _coerce_num(engine_ref._get_cell_by_addr(target_cube, lookup_addr))
                    elif item_upper == "PREV":
                        if idx - 1 < 0:
                            return CellError("#REF!")
                        target_item = dim.items[idx - 1]
                    elif item_upper == "NEXT":
                        if idx + 1 >= len(dim.items):
                            return CellError("#REF!")
                        target_item = dim.items[idx + 1]
                    elif item_upper in {"FIRST", "LAST"}:
                        if not dim.items:
                            target_item = None
                        else:
                            target_item = dim.items[0] if item_upper == "FIRST" else dim.items[-1]

                if target_item is None:
                    target_item = next(
                        (it for it in dim.items if it.name.lower() == item_name.lower()),
                        None,
                    )
                if target_item is None:
                    raise KeyError(f"Unknown item {item_name!r} in dimension {dim_name!r}")

                new_addr = list(seeded_addr)
                new_addr[slot] = target_item.id

                # Detect explicit self-references (e.g., [Quarter:Q2] in Q2 cell)
                # as circular errors only within the same cube. Cross-cube
                # references are allowed to reuse the same address tuple.
                if (
                    target_cube is cube
                    and tuple(new_addr) == base_addr
                    and item_upper not in {"THIS", "PREV", "NEXT"}
                ):
                    raise CircularReferenceError(f"Circular reference: [{dim_name}:{item_name}] refers to itself")

                # Pad address to match target cube dimensions for backward compatibility
                lookup_addr = tuple(new_addr)
                if len(new_addr) < len(target_cube.dimension_ids) and "@" in target_cube.dimension_ids:
                    at_dim = ws.dimensions.get("@")
                    if at_dim and at_dim.items:
                        lookup_addr = (at_dim.items[0].id, *new_addr)

                raw_val = engine_ref._get_cell_by_addr(target_cube, lookup_addr)
                # In a dynamic bound ($<...>) context, an empty cell (None)
                # cannot be used as a range bound; treat this as a validation
                # failure rather than silently coercing it to 0.0.
                if getattr(self, "_in_dynamic_bound", False) and raw_val is None:
                    raise RuleValidationError(
                        f"Dynamic range bound in dimension {dim_name!r} evaluated to an empty value",
                    )

                return _coerce_num(raw_val)

            def _resolve_seq_keyword_item_ids(
                self,
                dim: Dimension,
                keyword: str,
                base_addr: tuple[str, ...],
                seeded_addr: list[str],
                target_cube: Cube,
                same_cube: bool,
            ) -> list[str] | None:
                slot = target_cube.dimension_ids.index(dim.id)
                source_addr = base_addr if same_cube else tuple(seeded_addr)
                if slot >= len(source_addr):
                    raise KeyError(
                        f"Address slot out of range for dimension {dim.name!r} in cube {target_cube.name!r}"
                    )
                curr_id = source_addr[slot]
                idx = dim.item_index(curr_id)
                keyword = keyword.upper()

                def _id_for_index(position: int) -> list[str] | None:
                    if position < 0 or position >= len(dim.items):
                        return None
                    return [dim.items[position].id]

                if keyword == "THIS":
                    return _id_for_index(idx)
                if keyword == "PREV":
                    return _id_for_index(idx - 1)
                if keyword == "NEXT":
                    return _id_for_index(idx + 1)
                if keyword == "FIRST":
                    return _id_for_index(0)
                if keyword == "LAST":
                    return _id_for_index(len(dim.items) - 1)
                raise ValueError(f"Unsupported sequential keyword {keyword!r}")

            def slice_over_ref(
                self,
                pairs: list[tuple[str, str]],
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[float]:
                """Return list of values from a reference slice for SLICE() function.
                
                Similar to sum_over_ref but returns the list of values instead of summing.
                """
                target_cube, _ = _select_cube_and_seed_addr(cube_name, base_addr)
                
                # Build axes for iteration
                entries: list[dict[str, Any]] = []
                has_wildcard_star_star = False
                for dim_name, item_name in pairs:
                    # Whole-cube wildcard (*.*) does not constrain any specific dimension
                    if dim_name == "*" and item_name == "*":
                        has_wildcard_star_star = True
                        continue
                    dim = next(
                        (ws.dimensions[dim_id] for dim_id in target_cube.dimension_ids
                         if ws.dimensions[dim_id].name.lower() == dim_name.lower()),
                        None,
                    )
                    if dim is None:
                        raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                    
                    entry: dict[str, Any] = {"dim": dim, "item_name": item_name}
                    
                    # Handle range syntax
                    if ".." in item_name:
                        if dim.dim_type != "seq":
                            raise ValueError(f"Range only supported for sequential dimensions")
                        start_raw, end_raw = self._split_range_item_name(item_name)
                        entry["range_bounds"] = (start_raw, end_raw)
                    else:
                        item_ids = _dimension_item_ids_for_name(dim, item_name)
                        if item_ids is None:
                            raise KeyError(f"Unknown item {item_name!r}")
                        entry["item_ids"] = item_ids
                    entries.append(entry)
                
                # Build fixed axes
                fixed_axes: dict[str, list[str]] = {}
                for entry in entries:
                    dim = entry["dim"]
                    if "range_bounds" in entry:
                        start_raw, end_raw = entry["range_bounds"]
                        start_label = self._resolve_range_bound(start_raw, dim.name, base_addr)
                        end_label = self._resolve_range_bound(end_raw, dim.name, base_addr)
                        items = list(dim.items)
                        try:
                            start_idx = next(i for i, it in enumerate(items) if it.name.lower() == str(start_label).lower())
                            end_idx = next(i for i, it in enumerate(items) if it.name.lower() == str(end_label).lower())
                        except StopIteration:
                            raise KeyError(f"Unknown item in range {start_label!r}..{end_label!r}")
                        if start_idx <= end_idx:
                            rng = range(start_idx, end_idx + 1)
                        else:
                            rng = range(end_idx, start_idx + 1)
                        fixed_axes[dim.id] = [items[i].id for i in rng]
                    else:
                        fixed_axes[dim.id] = entry["item_ids"]
                
                # Build axes - iterate over ALL items for unconstrained dimensions
                axes: list[list[str]] = []
                for dim_id in target_cube.dimension_ids:
                    if dim_id in fixed_axes:
                        axes.append(fixed_axes[dim_id])
                    elif dim_id == "@":
                        # @ dimension always defaults to @.value when not explicitly
                        # constrained, regardless of *.* wildcard. This prevents
                        # slice_over_ref from iterating over all 28 @ channels
                        # (style, format_number, etc.) which would dilute aggregates
                        # and potentially trigger errors from non-value channels.
                        dim_obj = ws.dimensions[dim_id]
                        value_item = next((it.id for it in dim_obj.items if it.name == "value"), None)
                        axes.append([value_item] if value_item else [it.id for it in dim_obj.items])
                    else:
                        dim_obj = ws.dimensions[dim_id]
                        axes.append([it.id for it in dim_obj.items])
                
                # Collect all values
                values: list[float] = []
                for raw_addr in itertools.product(*axes):
                    addr = tuple(raw_addr)
                    v = engine_ref._get_cell_by_addr(target_cube, addr)
                    if isinstance(v, CellError):
                        return [v]
                    num = _coerce_num(v)
                    if isinstance(num, CellError):
                        return [num]
                    if num is not None:
                        try:
                            values.append(float(num))
                        except (ValueError, TypeError):
                            pass  # Skip non-numeric
                
                return values

            def sum_over_ref(
                self,
                pairs: list[tuple[str, str]],
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> float:
                """Aggregate over a reference slice described by (dim,item) pairs.

                Behaviour:
                - For simple (non-range) refs in the *same cube*, keep legacy
                  semantics by delegating to ``resolve_ref`` / ``resolve_multi_ref``
                  so SUM behaves like a scalar sum.
                - When any ``item_name`` uses range syntax ``start..end``, or
                  when aggregating across cubes, treat the pairs as a slice in
                  the target cube and sum across any remaining unconstrained
                  dimensions.
                - Range syntax is only valid for sequential dimensions
                  (``dim.dim_type == "seq"``); using ``start..end`` on a
                  non-sequential dimension raises ``RuleValidationError``.
                - Range bounds may be dynamic ``$<...>`` expressions. These are
                  evaluated at runtime (using the same resolver and base
                  address) and must reduce to a single-dimension reference from
                  anywhere in the model. The resulting cell value is converted
                  to a string and matched against the sequential dimension's
                  item names.
                """

                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                same_cube = target_cube is cube

                needs_slice = not same_cube
                entries: list[dict[str, Any]] = []

                # Precompute dimension slot mapping for seeded_addr lookup
                dim_id_to_slot = {dim_id: i for i, dim_id in enumerate(target_cube.dimension_ids)}

                # Determine which dimensions are covered by the reference pairs
                covered_dim_ids: set[str] = set()
                for dim_name, _ in pairs:
                    dim = next(
                        (
                            ws.dimensions[dim_id]
                            for dim_id in target_cube.dimension_ids
                            if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                        ),
                        None,
                    )
                    if dim is not None:
                        covered_dim_ids.add(dim.id)

                # If not all dimensions are covered, we need to use slice path for aggregation
                if len(covered_dim_ids) < len(target_cube.dimension_ids):
                    needs_slice = True

                has_wildcard_star_star = False
                for dim_name, item_name in pairs:
                    # Whole-cube wildcard (*.*) does not constrain any specific dimension
                    if dim_name == "*" and item_name == "*":
                        has_wildcard_star_star = True
                        continue
                    dim = next(
                        (
                            ws.dimensions[dim_id]
                            for dim_id in target_cube.dimension_ids
                            if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                        ),
                        None,
                    )
                    if dim is None:
                        raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")

                    entry: dict[str, Any] = {"dim": dim, "item_name": item_name}

                    # Check for range syntax BEFORE seq keyword handling (important!)
                    if ".." in item_name:
                        needs_slice = True
                        if dim.dim_type != "seq":
                            raise ValueError(
                                f"Range '{dim.name}.{item_name}' is only supported for sequential dimensions; "
                                f"dimension {dim.name!r} is not sequential",
                            )
                        start_raw, end_raw = self._split_range_item_name(item_name)
                        if not start_raw or not end_raw:
                            raise RuleValidationError(
                                f"Invalid range syntax {item_name!r} in dimension {dim_name!r}",
                            )
                        entry["range_bounds"] = (start_raw, end_raw)
                        entries.append(entry)
                        continue

                    item_upper = item_name.upper()
                    seq_keywords_enabled = getattr(self, "_allow_seq_keywords", False)
                    if seq_keywords_enabled and item_upper in {"THIS", "PREV", "NEXT", "FIRST", "LAST"}:
                        if same_cube:
                            return self.resolve_multi_ref(pairs, base_addr, cube_name)
                        if dim.dim_type != "seq":
                            raise ValueError(
                                f"Dimension {dim.name!r} is not sequential (dim_type='seq' required for sequential keywords)"
                            )
                        seq_item_ids = self._resolve_seq_keyword_item_ids(
                            dim,
                            item_upper,
                            base_addr,
                            seeded_addr,
                            target_cube,
                            same_cube,
                        )
                        if seq_item_ids is None:
                            return CellError("#REF!")
                        entry["item_ids"] = seq_item_ids
                        entries.append(entry)
                        continue

                    item_ids = _dimension_item_ids_for_name(dim, item_name)
                    if item_ids is None:
                        raise KeyError(f"Unknown item {item_name!r} in dimension {dim_name!r}")
                    if len(item_ids) != 1:
                        needs_slice = True
                    entry["item_ids"] = item_ids
                    entries.append(entry)

                if same_cube and not needs_slice:
                    if len(pairs) == 1:
                        dim_name, item_name = pairs[0]
                        result = self.resolve_ref(dim_name, item_name, base_addr, cube_name)
                        # Treat non-numeric text as 0 (Excel/Calc behavior)
                        if isinstance(result, str):
                            try:
                                return float(result)
                            except (ValueError, TypeError):
                                return 0.0
                        return result
                    result = self.resolve_multi_ref(pairs, base_addr, cube_name)
                    # Treat non-numeric text as 0 (Excel/Calc behavior)
                    if isinstance(result, str):
                        try:
                            return float(result)
                        except (ValueError, TypeError):
                            return 0.0
                    return result

                fixed_axes: dict[str, list[str]] = {}
                for entry in entries:
                    dim = entry["dim"]
                    if "range_bounds" in entry:
                        start_raw, end_raw = entry["range_bounds"]
                        start_label = self._resolve_range_bound(start_raw, dim_name, base_addr)
                        if isinstance(start_label, CellError):
                            return start_label
                        end_label = self._resolve_range_bound(end_raw, dim_name, base_addr)
                        if isinstance(end_label, CellError):
                            return end_label

                        items = list(dim.items)
                        try:
                            start_idx = next(
                                i for i, it in enumerate(items) if it.name.lower() == start_label.lower()
                            )
                            end_idx = next(
                                i for i, it in enumerate(items) if it.name.lower() == end_label.lower()
                            )
                        except StopIteration:
                            raise KeyError(
                                f"Unknown item in range {start_label!r}..{end_label!r} for dimension {dim_name!r}",
                            )
                        if start_idx <= end_idx:
                            rng = range(start_idx, end_idx + 1)
                        else:
                            rng = range(end_idx, start_idx + 1)
                        fixed_axes[dim.id] = [items[i].id for i in rng]
                        continue

                    item_ids = entry["item_ids"]

                    fixed_axes[dim.id] = item_ids

                # Build axes for the slice. Shared dimensions not explicitly
                # constrained inherit the value from seeded_addr (mapped from
                # caller's base_addr). Unshared dimensions (present only in
                # target cube) aggregate over all items. The *.* wildcard is
                # the only case where shared dimensions also aggregate over all
                # items — it explicitly means "sum everything".
                axes: list[list[str]] = []
                source_dim_ids = set(cube.dimension_ids)
                for dim_id in target_cube.dimension_ids:
                    dim_obj = ws.dimensions[dim_id]
                    if dim_id in fixed_axes:
                        axes.append(fixed_axes[dim_id])
                    elif dim_id == "@" and has_wildcard_star_star:
                        # When *.* wildcard is used, @ dimension defaults to @.value only
                        # (not all channels) to avoid circular references when aggregating
                        # from a @.fill rule
                        value_item = next((it.id for it in dim_obj.items if it.name == "value"), None)
                        axes.append([value_item] if value_item else [it.id for it in dim_obj.items])
                    elif has_wildcard_star_star:
                        # *.* wildcard: aggregate over all items for this dimension
                        axes.append([it.id for it in dim_obj.items])
                    elif dim_id in source_dim_ids:
                        # Shared dimension: inherit from seeded_addr (base_addr for same-cube)
                        slot = dim_id_to_slot[dim_id]
                        if slot < len(seeded_addr):
                            axes.append([seeded_addr[slot]])
                        else:
                            axes.append([it.id for it in dim_obj.items])
                    else:
                        # Unshared dimension: aggregate over all items
                        axes.append([it.id for it in dim_obj.items])

                axes_snapshot = [list(axis) for axis in axes]
                _UNSET = object()  # sentinel for "not read via bulk fast path"

                def _compute_sum() -> float | str:
                    compute_trace(f"SUM over {len(axes_snapshot)} axes, {target_cube.name}")
                    return _compute_sum_python()

                def _compute_sum_python() -> float | str:
                    total: float | str = 0.0
                    count = 0
                    tracking = engine_ref._is_tracking_enabled()
                    parent_key: str | None = None
                    pending_precedents: dict[str, set[str]] | None = None
                    if tracking:
                        eval_context = engine_ref._thread_eval_context()
                        if eval_context:
                            parent_key = eval_context[-1]
                            pending_precedents = engine_ref._thread_pending_precedents()
                    for raw_addr in itertools.product(*axes):
                        addr = tuple(raw_addr)
                        v: Any = _UNSET
                        if tracking and parent_key and pending_precedents is not None:
                            node_key = engine_ref._cell_node_key(target_cube.id, addr)
                            if (
                                node_key in engine_ref._tracked_nodes
                                and not engine_ref._dep_graph.is_dirty(node_key)
                            ):
                                pending_precedents.setdefault(parent_key, set()).add(node_key)
                                v = target_cube.get(addr)
                        if v is _UNSET:
                            v = engine_ref._get_cell_by_addr(target_cube, addr)
                        count += 1
                        num = _coerce_num(v)
                        # Propagate error sentinels if any contributing cell is an error.
                        if isinstance(num, CellError):
                            return num
                        if num is None:
                            continue
                        # Treat non-numeric text as 0 (Excel/Calc behavior)
                        if isinstance(num, str):
                            try:
                                num = float(num)
                            except (ValueError, TypeError):
                                continue  # Skip text values in sum
                        total = float(total) + num

                    return _normalize_ieee_special(float(total))

                return engine_ref._evaluate_slice_node("SUM", target_cube, axes_snapshot, _compute_sum)

            def aggregate_over_ref(
                self,
                pairs: list[tuple[str, str]],
                base_addr: tuple[str, ...],
                cube_name: str | None,
                fn: str,
            ) -> float | str | type(NotImplemented):
                """Aggregate over a reference slice for MIN/MAX/AVG/AVERAGE/COUNT.

                This mirrors ``sum_over_ref``'s slice semantics but applies a
                different aggregation function.  To preserve existing
                same-cube, non-range behaviour for SUM, callers for other
                aggregates delegate here explicitly instead of reusing
                ``sum_over_ref``.
                """

                # Dynamic-bounds ($<...>) must always resolve to a single cell.
                # When evaluating inner expressions for a dynamic bound, fall
                # back to scalar semantics instead of aggregating over a
                # slice.
                if getattr(self, "_in_dynamic_bound", False):
                    return NotImplemented

                # Determine the target cube. For cross-cube references, we need
                # the seeded address to properly constrain dimensions not covered
                # by the (dim,item) pairs using the caller's base_addr values.
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                same_cube = target_cube is cube

                fixed_axes: dict[str, list[str]] = {}
                has_wildcard_star_star = False
                for dim_name, item_name in pairs:
                    # Whole-cube wildcard (*.*) does not constrain any specific dimension
                    if dim_name == "*" and item_name == "*":
                        has_wildcard_star_star = True
                        continue
                    dim = next(
                        (
                            ws.dimensions[dim_id]
                            for dim_id in target_cube.dimension_ids
                            if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                        ),
                        None,
                    )
                    if dim is None:
                        raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")

                    item_upper = item_name.upper()
                    seq_keywords_enabled = getattr(self, "_allow_seq_keywords", False)
                    if seq_keywords_enabled and item_upper in {"THIS", "PREV", "NEXT", "FIRST", "LAST"}:
                        return NotImplemented

                    if ".." in item_name:
                        if dim.dim_type != "seq":
                            raise ValueError(
                                f"Range '{dim.name}.{item_name}' is only supported for sequential dimensions; "
                                f"dimension {dim.name!r} is not sequential",
                            )
                        start_raw, end_raw = self._split_range_item_name(item_name)
                        if not start_raw or not end_raw:
                            raise RuleValidationError(
                                f"Invalid range syntax {item_name!r} in dimension {dim_name!r}",
                            )
                        start_label = self._resolve_range_bound(start_raw, dim_name, base_addr)
                        if isinstance(start_label, CellError):
                            return start_label
                        end_label = self._resolve_range_bound(end_raw, dim_name, base_addr)
                        if isinstance(end_label, CellError):
                            return end_label

                        items = list(dim.items)
                        try:
                            start_idx = next(
                                i for i, it in enumerate(items) if it.name.lower() == start_label.lower()
                            )
                            end_idx = next(
                                i for i, it in enumerate(items) if it.name.lower() == end_label.lower()
                            )
                        except StopIteration:
                            raise KeyError(
                                f"Unknown item in range {start_label!r}..{end_label!r} for dimension {dim_name!r}",
                            )
                        if start_idx <= end_idx:
                            rng = range(start_idx, end_idx + 1)
                        else:
                            rng = range(end_idx, start_idx + 1)
                        fixed_axes[dim.id] = [items[i].id for i in rng]
                        continue

                    item_ids = _dimension_item_ids_for_name(dim, item_name)
                    if item_ids is None:
                        raise KeyError(f"Unknown item {item_name!r} in dimension {dim_name!r}")
                    fixed_axes[dim.id] = item_ids

                # Build axes for the slice. Shared dimensions not explicitly
                # constrained inherit the value from seeded_addr (mapped from
                # caller's base_addr). Unshared dimensions (present only in
                # target cube) aggregate over all items. The *.* wildcard is
                # the only case where shared dimensions also aggregate over all
                # items — it explicitly means "sum everything".
                axes: list[list[str]] = []
                dim_id_to_slot = {dim_id: i for i, dim_id in enumerate(target_cube.dimension_ids)}
                source_dim_ids = set(cube.dimension_ids)
                for dim_id in target_cube.dimension_ids:
                    dim_obj = ws.dimensions[dim_id]
                    if dim_id in fixed_axes:
                        axes.append(fixed_axes[dim_id])
                    elif dim_id == "@" and has_wildcard_star_star:
                        # When *.* wildcard is used, @ dimension defaults to @.value only
                        # (not all channels) to avoid circular references when aggregating
                        # from a @.fill rule
                        value_item = next((it.id for it in dim_obj.items if it.name == "value"), None)
                        axes.append([value_item] if value_item else [it.id for it in dim_obj.items])
                    elif has_wildcard_star_star:
                        # *.* wildcard: aggregate over all items for this dimension
                        axes.append([it.id for it in dim_obj.items])
                    elif dim_id in source_dim_ids:
                        # Shared dimension: inherit from seeded_addr (base_addr for same-cube)
                        slot = dim_id_to_slot[dim_id]
                        if slot < len(seeded_addr):
                            axes.append([seeded_addr[slot]])
                        else:
                            axes.append([it.id for it in dim_obj.items])
                    else:
                        # Unshared dimension: aggregate over all items
                        axes.append([it.id for it in dim_obj.items])

                axes_snapshot = [list(axis) for axis in axes]

                def _compute_agg() -> float | str | type(NotImplemented):
                    return _compute_agg_python()

                def _compute_agg_python() -> float | str | type(NotImplemented):
                    tracking = engine_ref._is_tracking_enabled()
                    parent_key: str | None = None
                    pending_precedents: dict[str, set[str]] | None = None
                    if tracking:
                        eval_context = engine_ref._thread_eval_context()
                        if eval_context:
                            parent_key = eval_context[-1]
                            pending_precedents = engine_ref._thread_pending_precedents()

                    def _read_cell(addr: tuple[str, ...]) -> Any:
                        if tracking and parent_key and pending_precedents is not None:
                            node_key = engine_ref._cell_node_key(target_cube.id, addr)
                            if (
                                node_key in engine_ref._tracked_nodes
                                and not engine_ref._dep_graph.is_dirty(node_key)
                            ):
                                pending_precedents.setdefault(parent_key, set()).add(node_key)
                                return target_cube.get(addr)
                        return engine_ref._get_cell_by_addr(target_cube, addr)

                    # COUNTA counts all non-empty cells (text, numbers, etc.)
                    if fn == "COUNTA":
                        count = 0
                        for raw_addr in itertools.product(*axes):
                            addr = tuple(raw_addr)
                            v = _read_cell(addr)
                            if v is not None and v != "":
                                count += 1
                        return float(count)

                    values: list[float] = []
                    debug_count = 0
                    for raw_addr in itertools.product(*axes):
                        addr = tuple(raw_addr)
                        v = _read_cell(addr)
                        # Debug: Check hardnumber status for first few values
                        if debug_count < 3 and fn in ("MIN", "MAX"):
                            is_hard = target_cube.is_user_override(addr)
                            print(f"[DEBUG {fn}] addr={addr[-2:] if len(addr) > 2 else addr}, value={v}, is_hardnumber={is_hard}")
                            debug_count += 1
                        num = _coerce_num(v)
                        if isinstance(num, CellError):
                            return num
                        if num is None:
                            continue
                        values.append(float(num))

                    if not values:
                        if fn in ("MIN", "MAX"):
                            return 0.0
                        if fn in ("AVG", "AVERAGE"):
                            return CellError("#DIV/0!")
                        if fn in ("COUNT", "COUNTA"):
                            return 0.0

                    if fn == "MIN":
                        return _normalize_ieee_special(min(values))
                    if fn == "MAX":
                        return _normalize_ieee_special(max(values))
                    if fn in ("AVG", "AVERAGE"):
                        return _normalize_ieee_special(sum(values) / len(values))
                    if fn == "COUNT":
                        return float(len(values))

                    return NotImplemented

                return engine_ref._evaluate_slice_node(fn.upper(), target_cube, axes_snapshot, _compute_agg)

            def resolve_multi_ref(
                self,
                pairs: list[tuple[str, str]],
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> float:
                # When called from a dynamic bound ($<...>), enforce single-cell resolution.
                if getattr(self, "_in_dynamic_bound", False):
                    # Ensure the reference resolves to exactly one cell by checking that
                    # all dimensions are fully constrained (no ranges, wildcards, or groups).
                    for dim_name, item_name in pairs:
                        if ".." in item_name:
                            raise RuleValidationError(
                                f"Dynamic bound $<...> must resolve to a single cell; "
                                f"range syntax not allowed in dimension {dim_name!r}"
                            )
                        if item_name == "*":
                            raise RuleValidationError(
                                f"Dynamic bound $<...> must resolve to a single cell; "
                                f"wildcard not allowed in dimension {dim_name!r}"
                            )
                        dim = dim_name_lower_to_dim.get(dim_name.lower())
                        if dim is None:
                            continue
                        item_ids = _dimension_item_ids_for_name(dim, item_name)
                        if item_ids is not None and len(item_ids) != 1:
                            raise RuleValidationError(
                                f"Dynamic bound $<...> must resolve to a single cell; "
                                f"group reference not allowed in dimension {dim_name!r}"
                            )

                # Check for range syntax - if present, delegate to sum_over_ref which handles ranges
                has_range = any(".." in item_name for _, item_name in pairs)
                if has_range:
                    return self.sum_over_ref(pairs, base_addr, cube_name)

                target_cube, new_addr = _select_cube_and_seed_addr(cube_name, base_addr)

                # If @ dimension exists and is NOT explicitly specified in pairs,
                # default to @.value instead of inheriting from base_addr
                if "@" in target_cube.dimension_ids:
                    at_explicit = any(dim_name.lower() == "@" for dim_name, _ in pairs)
                    if not at_explicit:
                        at_slot = target_cube.dimension_ids.index("@")
                        at_dim = ws.dimensions.get("@")
                        if at_dim and at_dim.items:
                            # Ensure new_addr is a list and has correct length
                            new_addr = list(new_addr)
                            while len(new_addr) < len(target_cube.dimension_ids):
                                new_addr.append("")
                            new_addr[at_slot] = at_dim.items[0].id  # @.value

                # Ensure new_addr has the correct length and alignment to match target_cube.dimension_ids.
                # If base_addr is shorter (missing @ dimension), build a full address by inserting
                # @.value at the correct slot position.
                if len(new_addr) < len(target_cube.dimension_ids):
                    aligned_addr: list[str] = []
                    for dim_id in target_cube.dimension_ids:
                        slot = target_cube.dimension_ids.index(dim_id)
                        if dim_id == "@":
                            aligned_addr.append(CHANNEL_TO_AT_ID["value"])
                        elif slot < len(new_addr):
                            # Adjust index: if @ is before this slot, account for the extra element
                            at_offset = 1 if "@" in target_cube.dimension_ids and target_cube.dimension_ids.index("@") < slot else 0
                            aligned_addr.append(new_addr[slot - at_offset])
                        else:
                            dim_obj = ws.dimensions.get(dim_id)
                            if dim_obj and dim_obj.items:
                                aligned_addr.append(dim_obj.items[0].id)
                            else:
                                aligned_addr.append("")
                    new_addr = aligned_addr
                saw_seq_keyword = False
                for dim_name, item_name in pairs:
                    dim = dim_name_lower_to_dim.get(dim_name.lower())
                    if dim is None:
                        raise KeyError(f"Unknown dimension: {dim_name!r}")
                    if dim.id not in target_cube.dimension_ids:
                        raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                    slot = target_cube.dimension_ids.index(dim.id)
                    item_upper = item_name.upper()
                    target_item = None
                    seq_keywords_enabled = getattr(self, "_allow_seq_keywords", False)
                    if seq_keywords_enabled and item_upper in {"THIS", "PREV", "NEXT", "FIRST", "LAST"}:
                        saw_seq_keyword = True
                        if dim.dim_type != "seq":
                            raise ValueError(
                                f"Dimension {dim.name!r} is not sequential (dim_type='seq' required for THIS/PREV/NEXT/FIRST/LAST)"
                            )
                        curr_id = new_addr[slot]
                        idx = dim.item_index(curr_id)
                        if item_upper == "THIS":
                            target_item = dim.items[idx]
                        elif item_upper == "PREV":
                            if idx - 1 < 0:
                                return CellError("#REF!")
                            target_item = dim.items[idx - 1]
                        elif item_upper == "NEXT":
                            if idx + 1 >= len(dim.items):
                                return CellError("#REF!")
                            target_item = dim.items[idx + 1]
                        elif item_upper == "FIRST":
                            target_item = dim.items[0] if dim.items else None
                        elif item_upper == "LAST":
                            target_item = dim.items[-1] if dim.items else None
                    if item_name == "*":
                        continue
                    if target_item is None:
                        target_item = next(
                            (it for it in dim.items if it.name.lower() == item_name.lower()),
                            None,
                        )
                    if target_item is None:
                        raise KeyError(f"Unknown item {item_name!r} in dimension {dim_name!r}")
                    new_addr[slot] = target_item.id

                lookup_addr = tuple(new_addr)
                if len(new_addr) < len(target_cube.dimension_ids) and "@" in target_cube.dimension_ids:
                    at_dim = ws.dimensions.get("@")
                    if at_dim and at_dim.items:
                        lookup_addr = (at_dim.items[0].id, *new_addr)
                raw_val = engine_ref._get_cell_by_addr(target_cube, lookup_addr)

                if getattr(self, "_in_dynamic_bound", False) and raw_val is None:
                    raise RuleValidationError(
                        "Dynamic range bound evaluated to an empty value",
                    )

                return _coerce_num(raw_val)

            def resolve_ctx(self, item_name: str, base_addr: tuple[str, ...]) -> float:
                for dim_id in cube.dimension_ids:
                    dim = ws.dimensions[dim_id]
                    slot = cube.dimension_ids.index(dim_id)
                    item = next((it for it in dim.items if it.name.lower() == item_name.lower()), None)
                    if item is not None:
                        new_addr = list(base_addr)
                        new_addr[slot] = item.id
                        return _coerce_num(engine_ref._get_cell_by_addr(cube, tuple(new_addr)))
                raise KeyError(f"Item {item_name!r} not found in any dimension of this cube")

            def dim_item_names(self, dim_name: str) -> list[str]:
                dim = next(
                    (d for d in ws.dimensions.values() if d.name.lower() == dim_name.lower()),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Unknown dimension: {dim_name!r}")
                return [it.name for it in dim.items]

            def label_for_dim(
                self,
                dim_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> str:
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                slot = target_cube.dimension_ids.index(dim.id)
                return _label_for_slot(target_cube, seeded_addr, slot)

            def label_for_addr(
                self,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> str:
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                if not target_cube.dimension_ids:
                    return ""
                return _label_for_slot(target_cube, seeded_addr, len(target_cube.dimension_ids) - 1)

            def pos_for_dim(
                self,
                dim_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> float:
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                slot = target_cube.dimension_ids.index(dim.id)
                if not (0 <= slot < len(seeded_addr)):
                    raise KeyError(f"Address slot out of range for dimension {dim_name!r}")
                item_id = seeded_addr[slot]
                idx = dim.item_index(item_id)
                return float(idx + 1)

            def posmax_for_dim(
                self,
                dim_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> float:
                """Return the maximum position (count of items) in a sequential dimension."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                return float(len(dim.items))

            def ancestors_for_dim(
                self,
                dim_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return ordered list of ancestor labels from parent to ROOT."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                slot = target_cube.dimension_ids.index(dim.id)
                if not (0 <= slot < len(seeded_addr)):
                    raise KeyError(f"Address slot out of range for dimension {dim_name!r}")
                item_id = seeded_addr[slot]
                
                # Build lookup maps for outline navigation
                item_to_label = {it.id: it.name for it in dim.items}
                
                # Find the outline node for current item and traverse up
                ancestors: list[str] = []
                
                def find_parent_and_label(target_id: str, outline_nodes: list) -> tuple[str | None, str | None]:
                    """Find parent item_id and label for a target item_id."""
                    for node in outline_nodes:
                        for child in node.children:
                            if child.item_id == target_id:
                                # Found the target as a child of this node
                                # Use item_id if available, otherwise use the node's label
                                parent_id = node.item_id
                                parent_label = item_to_label.get(parent_id) if parent_id else node.label
                                return parent_id, parent_label
                        # Recurse into children
                        result = find_parent_and_label(target_id, node.children)
                        if result[0] is not None or result[1] is not None:
                            return result
                    return None, None
                
                # Traverse up the hierarchy
                visited = set()
                current_item_id = item_id
                while current_item_id:
                    if current_item_id in visited:
                        break  # Prevent infinite loops
                    visited.add(current_item_id)
                    
                    parent_id, parent_label = find_parent_and_label(current_item_id, dim.outline)
                    if parent_label:
                        ancestors.append(parent_label)
                    if parent_id is None:
                        break  # Reached a label-only node, stop here
                    current_item_id = parent_id
                
                return ancestors

            def peers_for_dim(
                self,
                dim_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return list of peer labels (same parent), including the current item."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                slot = target_cube.dimension_ids.index(dim.id)
                if not (0 <= slot < len(seeded_addr)):
                    raise KeyError(f"Address slot out of range for dimension {dim_name!r}")
                item_id = seeded_addr[slot]
                
                item_to_label = {it.id: it.name for it in dim.items}
                
                def find_peers(target_id: str, outline_nodes: list) -> list[str] | None:
                    for node in outline_nodes:
                        if node.item_id == target_id:
                            # Target is a direct outline node - peers are other direct nodes with item_id
                            return [item_to_label.get(n.item_id) for n in outline_nodes if n.item_id and n.item_id != target_id]
                        for child in node.children:
                            if child.item_id == target_id:
                                # Target is a child - peers are siblings (only those with item_id)
                                peer_labels = [item_to_label.get(c.item_id) for c in node.children if c.item_id]
                                return [l for l in peer_labels if l]
                        result = find_peers(target_id, node.children)
                        if result is not None:
                            return result
                    return None
                
                peers = find_peers(item_id, dim.outline)
                if peers is None:
                    # Item not in outline, return all items as peers
                    return [it.name for it in dim.items]
                
                # Include current item
                current_label = item_to_label.get(item_id)
                if current_label and current_label not in peers:
                    peers.append(current_label)
                
                return peers

            def siblings_for_dim(
                self,
                dim_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return list of sibling labels (same parent), excluding the current item."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                slot = target_cube.dimension_ids.index(dim.id)
                if not (0 <= slot < len(seeded_addr)):
                    raise KeyError(f"Address slot out of range for dimension {dim_name!r}")
                item_id = seeded_addr[slot]
                
                item_to_label = {it.id: it.name for it in dim.items}
                
                def find_siblings(target_id: str, outline_nodes: list) -> list[str] | None:
                    for node in outline_nodes:
                        if node.item_id == target_id:
                            # Target is a direct outline node - siblings are other direct nodes with item_id
                            return [item_to_label.get(n.item_id) for n in outline_nodes if n.item_id and n.item_id != target_id]
                        for child in node.children:
                            if child.item_id == target_id:
                                # Target is a child - siblings are other children with item_id
                                sibling_labels = [item_to_label.get(c.item_id) for c in node.children if c.item_id and c.item_id != target_id]
                                return [l for l in sibling_labels if l]
                        result = find_siblings(target_id, node.children)
                        if result is not None:
                            return result
                    return None
                
                siblings = find_siblings(item_id, dim.outline)
                if siblings is None:
                    # Item not in outline, return all other items as siblings
                    return [it.name for it in dim.items if it.id != item_id]
                
                return siblings

            def descendants_for_dim(
                self,
                dim_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return all descendants in display order (depth-first traversal)."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                slot = target_cube.dimension_ids.index(dim.id)
                if not (0 <= slot < len(seeded_addr)):
                    raise KeyError(f"Address slot out of range for dimension {dim_name!r}")
                item_id = seeded_addr[slot]
                
                item_to_label = {it.id: it.name for it in dim.items}
                
                def find_descendants(target_id: str, outline_nodes: list) -> list[str] | None:
                    for node in outline_nodes:
                        if node.item_id == target_id:
                            # Found the target - collect all descendants via depth-first traversal
                            descendants: list[str] = []
                            
                            def collect_descendants(n):
                                for child in n.children:
                                    # Add child label if it has item_id, otherwise just recurse
                                    if child.item_id:
                                        label = item_to_label.get(child.item_id)
                                        if label:
                                            descendants.append(label)
                                    collect_descendants(child)
                            
                            collect_descendants(node)
                            return descendants
                        # Check if this is a label-only node that might contain the target
                        if node.item_id is None:
                            result = find_descendants(target_id, node.children)
                            if result is not None:
                                return result
                        result = find_descendants(target_id, node.children)
                        if result is not None:
                            return result
                    return None
                
                descendants = find_descendants(item_id, dim.outline)
                if descendants is None:
                    # Item not in outline, return empty list
                    return []
                
                return descendants

            def children_for_dim(
                self,
                dim_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return direct children of the current item."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                slot = target_cube.dimension_ids.index(dim.id)
                if not (0 <= slot < len(seeded_addr)):
                    raise KeyError(f"Address slot out of range for dimension {dim_name!r}")
                item_id = seeded_addr[slot]
                
                item_to_label = {it.id: it.name for it in dim.items}
                
                def find_children(target_id: str, outline_nodes: list) -> list[str] | None:
                    for node in outline_nodes:
                        if node.item_id == target_id:
                            # Found the target - return direct children (only those with item_id)
                            return [item_to_label.get(c.item_id) for c in node.children if c.item_id]
                        # Check label-only nodes too
                        if node.item_id is None:
                            result = find_children(target_id, node.children)
                            if result is not None:
                                return result
                        result = find_children(target_id, node.children)
                        if result is not None:
                            return result
                    return None
                
                children = find_children(item_id, dim.outline)
                if children is None:
                    # Item not in outline, return empty list
                    return []
                
                return [c for c in children if c]

            def parent_for_dim(
                self,
                dim_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return direct parent(s) of the current item (empty list if at root)."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")
                slot = target_cube.dimension_ids.index(dim.id)
                if not (0 <= slot < len(seeded_addr)):
                    raise KeyError(f"Address slot out of range for dimension {dim_name!r}")
                item_id = seeded_addr[slot]
                
                item_to_label = {it.id: it.name for it in dim.items}
                
                def find_parent(target_id: str, outline_nodes: list) -> tuple[str | None, str | None]:
                    """Find parent item_id and label for a target item_id."""
                    for node in outline_nodes:
                        for child in node.children:
                            if child.item_id == target_id:
                                # Found the target as a child of this node
                                parent_id = node.item_id
                                parent_label = item_to_label.get(parent_id) if parent_id else node.label
                                return parent_id, parent_label
                        # Recurse into children
                        result = find_parent(target_id, node.children)
                        if result[0] is not None or result[1] is not None:
                            return result
                    return None, None
                
                parent_id, parent_label = find_parent(item_id, dim.outline)
                if parent_label:
                    return [parent_label]

                return []

            def ancestors_for_dim_item(
                self,
                dim_name: str,
                item_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return ordered list of ancestor labels from parent to ROOT for a specific item or group."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")

                # Build lookup maps
                item_to_label = {it.id: it.name for it in dim.items}
                item_name_lower = item_name.lower()

                # Check if it's an item
                item_id = next((it.id for it in dim.items if it.name.lower() == item_name_lower), None)
                is_group_label = item_id is None

                if is_group_label:
                    # It's a group label - find the outline node with this label and item_id=None
                    def find_group_node(outline_nodes: list) -> tuple[str | None, str | None]:
                        """Find a label-only node and its parent."""
                        for node in outline_nodes:
                            if node.label.lower() == item_name_lower and node.item_id is None:
                                # Found the label-only node
                                return None, node.label
                            for child in node.children:
                                if child.label.lower() == item_name_lower and child.item_id is None:
                                    # Found the label-only node, parent is node
                                    parent_label = item_to_label.get(node.item_id) if node.item_id else node.label
                                    return node.item_id, parent_label
                            # Recurse
                            parent_id, parent_label = find_group_node(node.children)
                            if parent_id is not None or parent_label is not None:
                                return parent_id, parent_label
                        return None, None

                    # Traverse up from the group label
                    ancestors: list[str] = []
                    visited_labels = set()
                    current_label = item_name

                    def find_parent_of_label(target_label: str, outline_nodes: list) -> tuple[str | None, str | None]:
                        """Find parent of a label-only node."""
                        for node in outline_nodes:
                            for child in node.children:
                                if child.label.lower() == target_label.lower() and child.item_id is None:
                                    parent_label = item_to_label.get(node.item_id) if node.item_id else node.label
                                    return node.item_id, parent_label
                            result = find_parent_of_label(target_label, node.children)
                            if result[0] is not None or result[1] is not None:
                                return result
                        return None, None

                    while current_label:
                        if current_label.lower() in visited_labels:
                            break
                        visited_labels.add(current_label.lower())

                        parent_id, parent_label = find_parent_of_label(current_label, dim.outline)
                        if parent_label:
                            ancestors.append(parent_label)
                            # Continue with parent label for next iteration
                            # (works for both items and group labels)
                            current_label = parent_label
                        else:
                            break

                    return ancestors

                # Original item-based logic - modified to handle label-only ancestors
                def find_parent_and_label(target_id: str, outline_nodes: list) -> tuple[str | None, str | None]:
                    """Find parent item_id and label for a target item_id."""
                    for node in outline_nodes:
                        for child in node.children:
                            if child.item_id == target_id:
                                parent_id = node.item_id
                                parent_label = item_to_label.get(parent_id) if parent_id else node.label
                                return parent_id, parent_label
                        result = find_parent_and_label(target_id, node.children)
                        if result[0] is not None or result[1] is not None:
                            return result
                    return None, None

                def find_parent_of_label(target_label: str, outline_nodes: list) -> tuple[str | None, str | None]:
                    """Find parent of a label-only node."""
                    for node in outline_nodes:
                        for child in node.children:
                            if child.label.lower() == target_label.lower() and child.item_id is None:
                                parent_label = item_to_label.get(node.item_id) if node.item_id else node.label
                                return node.item_id, parent_label
                        result = find_parent_of_label(target_label, node.children)
                        if result[0] is not None or result[1] is not None:
                            return result
                    return None, None

                ancestors: list[str] = []
                visited_ids = set()
                visited_labels = set()
                current_item_id = item_id
                current_label = None
                
                while current_item_id or current_label:
                    if current_item_id and current_item_id in visited_ids:
                        break
                    if current_label and current_label.lower() in visited_labels:
                        break
                    
                    if current_item_id:
                        visited_ids.add(current_item_id)
                        parent_id, parent_label = find_parent_and_label(current_item_id, dim.outline)
                    else:
                        # Continue from label-only parent
                        visited_labels.add(current_label.lower())
                        parent_id, parent_label = find_parent_of_label(current_label, dim.outline)
                    
                    if parent_label:
                        ancestors.append(parent_label)
                    
                    if parent_id is None:
                        # Parent is label-only, continue with label traversal
                        current_item_id = None
                        current_label = parent_label
                    else:
                        # Parent is an item, continue with item traversal
                        current_item_id = parent_id
                        current_label = None

                return ancestors

            def peers_for_dim_item(
                self,
                dim_name: str,
                item_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return list of peer labels (same parent), including the specified item or group."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")

                item_to_label = {it.id: it.name for it in dim.items}
                item_name_lower = item_name.lower()

                # Check if it's an item
                item_id = next((it.id for it in dim.items if it.name.lower() == item_name_lower), None)
                is_group_label = item_id is None

                if is_group_label:
                    # It's a group label - find peers among label-only siblings
                    def find_group_peers(target_label: str, outline_nodes: list) -> list[str] | None:
                        for node in outline_nodes:
                            for child in node.children:
                                if child.label.lower() == target_label.lower() and child.item_id is None:
                                    # Return all label-only siblings at this level
                                    peer_labels = [c.label for c in node.children if c.item_id is None]
                                    return peer_labels if peer_labels else None
                            result = find_group_peers(target_label, node.children)
                            if result is not None:
                                return result
                        return None

                    peers = find_group_peers(item_name, dim.outline)
                    if peers is None:
                        # Return all top-level label-only nodes
                        return [node.label for node in dim.outline if node.item_id is None]
                    return peers

                # Original item-based logic
                def find_peers(target_id: str, outline_nodes: list) -> list[str] | None:
                    for node in outline_nodes:
                        if node.item_id == target_id:
                            return [item_to_label.get(n.item_id) for n in outline_nodes if n.item_id]
                        for child in node.children:
                            if child.item_id == target_id:
                                peer_labels = [item_to_label.get(c.item_id) for c in node.children if c.item_id]
                                return [l for l in peer_labels if l]
                        result = find_peers(target_id, node.children)
                        if result is not None:
                            return result
                    return None

                peers = find_peers(item_id, dim.outline)
                if peers is None:
                    return [it.name for it in dim.items]

                return peers

            def siblings_for_dim_item(
                self,
                dim_name: str,
                item_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return list of sibling labels (same parent), excluding the specified item or group."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")

                item_to_label = {it.id: it.name for it in dim.items}
                item_name_lower = item_name.lower()

                # Check if it's an item
                item_id = next((it.id for it in dim.items if it.name.lower() == item_name_lower), None)
                is_group_label = item_id is None

                if is_group_label:
                    # It's a group label - find siblings among label-only nodes
                    def find_group_siblings(target_label: str, outline_nodes: list) -> list[str] | None:
                        for node in outline_nodes:
                            for child in node.children:
                                if child.label.lower() == target_label.lower() and child.item_id is None:
                                    # Return all label-only siblings except target
                                    sibling_labels = [c.label for c in node.children if c.item_id is None and c.label.lower() != target_label.lower()]
                                    return sibling_labels if sibling_labels else None
                            result = find_group_siblings(target_label, node.children)
                            if result is not None:
                                return result
                        return None

                    siblings = find_group_siblings(item_name, dim.outline)
                    if siblings is None:
                        # Return all top-level label-only nodes except target
                        return [node.label for node in dim.outline if node.item_id is None and node.label.lower() != item_name_lower]
                    return siblings

                # Original item-based logic
                def find_siblings(target_id: str, outline_nodes: list) -> list[str] | None:
                    for node in outline_nodes:
                        if node.item_id == target_id:
                            return [item_to_label.get(n.item_id) for n in outline_nodes if n.item_id and n.item_id != target_id]
                        for child in node.children:
                            if child.item_id == target_id:
                                sibling_labels = [item_to_label.get(c.item_id) for c in node.children if c.item_id and c.item_id != target_id]
                                return [l for l in sibling_labels if l]
                        result = find_siblings(target_id, node.children)
                        if result is not None:
                            return result
                    return None

                siblings = find_siblings(item_id, dim.outline)
                if siblings is None:
                    return [it.name for it in dim.items if it.id != item_id]

                return siblings

            def descendants_for_dim_item(
                self,
                dim_name: str,
                item_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return all descendants in display order (depth-first traversal) for a specific item or group."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")

                item_to_label = {it.id: it.name for it in dim.items}
                item_name_lower = item_name.lower()

                # Check if it's an item
                item_id = next((it.id for it in dim.items if it.name.lower() == item_name_lower), None)
                is_group_label = item_id is None

                if is_group_label:
                    # It's a group label - find descendants including items under label-only nodes
                    def find_group_descendants(target_label: str, outline_nodes: list) -> list[str] | None:
                        for node in outline_nodes:
                            if node.label.lower() == target_label.lower() and node.item_id is None:
                                descendants: list[str] = []
                                def collect_descendants(n):
                                    for child in n.children:
                                        if child.item_id:
                                            label = item_to_label.get(child.item_id)
                                            if label:
                                                descendants.append(label)
                                        # Always recurse into children
                                        collect_descendants(child)
                                collect_descendants(node)
                                return descendants
                            if node.item_id is None:
                                result = find_group_descendants(target_label, node.children)
                                if result is not None:
                                    return result
                            result = find_group_descendants(target_label, node.children)
                            if result is not None:
                                return result
                        return None

                    descendants = find_group_descendants(item_name, dim.outline)
                    if descendants is None:
                        return []
                    return descendants

                # Original item-based logic
                def find_descendants(target_id: str, outline_nodes: list) -> list[str] | None:
                    for node in outline_nodes:
                        if node.item_id == target_id:
                            descendants: list[str] = []
                            def collect_descendants(n):
                                for child in n.children:
                                    if child.item_id:
                                        label = item_to_label.get(child.item_id)
                                        if label:
                                            descendants.append(label)
                                    collect_descendants(child)
                            collect_descendants(node)
                            return descendants
                        if node.item_id is None:
                            result = find_descendants(target_id, node.children)
                            if result is not None:
                                return result
                        result = find_descendants(target_id, node.children)
                        if result is not None:
                            return result
                    return None

                descendants = find_descendants(item_id, dim.outline)
                if descendants is None:
                    return []

                return descendants

            def children_for_dim_item(
                self,
                dim_name: str,
                item_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return direct children of the specified item or group."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")

                item_to_label = {it.id: it.name for it in dim.items}
                item_name_lower = item_name.lower()

                # Check if it's an item
                item_id = next((it.id for it in dim.items if it.name.lower() == item_name_lower), None)
                is_group_label = item_id is None

                if is_group_label:
                    # It's a group label - find direct children (items or label-only nodes)
                    def find_group_children(target_label: str, outline_nodes: list) -> list[str] | None:
                        for node in outline_nodes:
                            if node.label.lower() == target_label.lower() and node.item_id is None:
                                # Return direct children (item labels for items, labels for groups)
                                child_labels = []
                                for c in node.children:
                                    if c.item_id:
                                        label = item_to_label.get(c.item_id)
                                        if label:
                                            child_labels.append(label)
                                    elif c.item_id is None:
                                        child_labels.append(c.label)
                                return child_labels
                            if node.item_id is None:
                                result = find_group_children(target_label, node.children)
                                if result is not None:
                                    return result
                            result = find_group_children(target_label, node.children)
                            if result is not None:
                                return result
                        return None

                    children = find_group_children(item_name, dim.outline)
                    if children is None:
                        return []
                    return children

                # Original item-based logic
                def find_children(target_id: str, outline_nodes: list) -> list[str] | None:
                    for node in outline_nodes:
                        if node.item_id == target_id:
                            return [item_to_label.get(c.item_id) for c in node.children if c.item_id]
                        if node.item_id is None:
                            result = find_children(target_id, node.children)
                            if result is not None:
                                return result
                        result = find_children(target_id, node.children)
                        if result is not None:
                            return result
                    return None

                children = find_children(item_id, dim.outline)
                if children is None:
                    return []

                return [c for c in children if c]

            def parent_for_dim_item(
                self,
                dim_name: str,
                item_name: str,
                base_addr: tuple[str, ...],
                cube_name: str | None = None,
            ) -> list[str]:
                """Return direct parent(s) of the specified item or group (empty list if at root)."""
                target_cube, seeded_addr = _select_cube_and_seed_addr(cube_name, base_addr)
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in target_cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    raise KeyError(f"Dimension {dim_name!r} not in cube {target_cube.name!r}")

                item_to_label = {it.id: it.name for it in dim.items}
                item_name_lower = item_name.lower()

                # Check if it's an item
                item_id = next((it.id for it in dim.items if it.name.lower() == item_name_lower), None)
                is_group_label = item_id is None

                if is_group_label:
                    # It's a group label - find its parent (label-only node or item)
                    def find_group_parent(target_label: str, outline_nodes: list) -> str | None:
                        for node in outline_nodes:
                            for child in node.children:
                                if child.label.lower() == target_label.lower() and child.item_id is None:
                                    # Parent is this node
                                    parent_label = item_to_label.get(node.item_id) if node.item_id else node.label
                                    return parent_label
                            result = find_group_parent(target_label, node.children)
                            if result is not None:
                                return result
                        return None

                    parent_label = find_group_parent(item_name, dim.outline)
                    if parent_label:
                        return [parent_label]
                    return []

                # Original item-based logic
                def find_parent(target_id: str, outline_nodes: list) -> tuple[str | None, str | None]:
                    for node in outline_nodes:
                        for child in node.children:
                            if child.item_id == target_id:
                                parent_id = node.item_id
                                parent_label = item_to_label.get(parent_id) if parent_id else node.label
                                return parent_id, parent_label
                        result = find_parent(target_id, node.children)
                        if result[0] is not None or result[1] is not None:
                            return result
                    return None, None

                parent_id, parent_label = find_parent(item_id, dim.outline)
                if parent_label:
                    return [parent_label]

                return []

            def get_item_name_by_id(self, dim_name: str, item_id: str) -> str | None:
                """Return item name for a given dimension and item ID."""
                dim = next(
                    (
                        ws.dimensions[dim_id]
                        for dim_id in cube.dimension_ids
                        if ws.dimensions[dim_id].name.lower() == dim_name.lower()
                    ),
                    None,
                )
                if dim is None:
                    return None
                item = next((it for it in dim.items if it.id == item_id), None)
                return item.name if item else None

        resolver = _Resolver()
        resolver._engine = engine_ref
        resolver._cube = cube
        self._resolver_cache[cube.id] = resolver
        return resolver

    def _get_cell_by_addr(
        self,
        cube: Cube,
        addr: tuple[str, ...],
        *,
        rule: Any | None = None,
    ) -> Any:
        """Low-level: get a cell value by raw address (no rule fan-out loop).

        If *rule* is supplied, it is used instead of re-deriving the covering
        rule via find_anchored_rule/find_rule. This is useful for callers that
        already know the rule (e.g., the bootstrap expansion) and want to avoid
        repeated lookup cost.
        """
        # SINGLE NORMALIZATION POINT: Ensure full address internally
        addr = _normalize_addr_for_cube(cube, addr)

        tracking = self._is_tracking_enabled()
        # Fast read-only path: bulk tile queries disable dependency tracking.
        # If the value is already cached, return it immediately without
        # checking dirty flags, looking up rules, or re-evaluating.
        if not tracking:
            v = cube.get(addr)
            if v is not None:
                return v

        eval_context = self._thread_eval_context()
        pending_precedents = self._thread_pending_precedents()
        eval_stack = self._thread_eval_stack()
        parent_key: str | None = None
        node_key: str | None = None
        if tracking:
            # Unified key format - always includes @ dimension
            # The @ dimension is treated as just another dimension
            node_key = self._cell_node_key(cube.id, addr)
            parent_key = eval_context[-1] if eval_context else None
            eval_context.append(node_key)
            if parent_key and parent_key != node_key:
                pending_precedents.setdefault(parent_key, set()).add(node_key)
            # Fast path: already evaluated and tracked, value is cached and not dirty,
            # and either it is a hardvalue or its dependency edges are already recorded.
            # Rule cells with no recorded edges must fall through so the existing
            # needs_dep_record logic can force a re-evaluation to rebuild edges.
            v = cube.get(addr)
            if (
                v is not None
                and not isinstance(v, CellError)
                and node_key in self._tracked_nodes
                and not self._dep_graph.is_dirty(node_key)
                and (cube.is_user_override(addr) or self._dep_graph.has_precedents(node_key))
            ):
                eval_context.pop()
                return v

        def _commit_precedents(success: bool, had_rule_body: bool) -> None:
            if not tracking or node_key is None:
                return
            precedents = pending_precedents.pop(node_key, set())
            self._tracked_nodes.add(node_key)
            if success:
                if had_rule_body:
                    # Rule cell: update its precedents (cells it references)
                    self._dep_graph.replace_precedents(node_key, precedents)
                elif parent_key:
                    # Hardcoded cell referenced by a rule: add edge from this cell to parent
                    # so when this hardcoded cell changes, the parent rule gets recalculated
                    self._dep_graph.add_edge(node_key, parent_key)
                else:
                    pass  # No edge to create

        success = False
        had_rule_body = False
        v = cube.get(addr)

        # PRECEDENCE: Hard numbers > cell rules > rules
        # Check if this cell has a hard number (user override) - highest precedence.
        # Do this FIRST to avoid wasted rule lookups for hardvalue cells.
        is_hardnumber = cube.is_user_override(addr)
        if is_hardnumber and v is not None:
            # Hard number always wins - never override with rule body or rule
            success = True
            # Commit precedents to record dependency edges
            # This ensures that when this hard number changes, dependent rules are invalidated
            if tracking and node_key is not None:
                _commit_precedents(success, had_rule_body)
            if tracking and node_key is not None and eval_context:
                eval_context.pop()
            return v

        # Pre-check if this cell has a rule - needed for correct edge creation.
        # If the caller already supplied the rule, skip the lookup.
        if rule is not None:
            has_rule_body = True
            has_rule = True
        else:
            has_rule_body = self._ws.find_anchored_rule(cube.id, addr) is not None
            has_rule = self._ws.find_rule(cube.id, addr, cube.dimension_ids) is not None

        if has_rule_body or has_rule:
            had_rule_body = True  # Mark early so edge creation works even for cached values
            # Check if dirty - force re-evaluation (regardless of tracking state)
            # Use unified node_key format (always includes @ dimension)
            is_dirty = self._dep_graph.is_dirty(node_key)
            if is_dirty:
                cube.set(addr, None)
                v = None
            elif isinstance(v, CellError):
                # Force re-evaluation of error values during recalculation
                cube.set(addr, None)
                v = None
            
            if v is not None:
                # Check if we need to force re-evaluation to record dependencies
                # This happens when a rule has a cached value but no dependency edges recorded yet
                # (e.g., after loading from file where computed values are cached)
                needs_dep_record = False
                if tracking and node_key and not self._dep_graph.has_precedents(node_key):
                    # No dependencies recorded for this rule cell - need to re-evaluate to record them
                    needs_dep_record = True
                if not needs_dep_record:
                    success = True
                    # Only commit precedents if there are pending precedents to commit.
                    if tracking and node_key and pending_precedents.get(node_key):
                        _commit_precedents(success, had_rule_body)
                    # Track this node even when _commit_precedents is skipped
                    # (e.g. cached value with no pending precedents).
                    if tracking and node_key:
                        self._tracked_nodes.add(node_key)
                    if tracking and node_key is not None and eval_context:
                        eval_context.pop()
                    return v
                # Otherwise, force re-evaluation by clearing the cached value
                cube.set(addr, None)
                v = None
        # Recursion guard before rule evaluation.  Key by (cube_id,
        # addr) so that cross-cube lookups can safely reuse the same address
        # tuple without being treated as circular.
        key = (cube.id, addr)
        if key in eval_stack:
            # Record dependency edge before raising, so the adjacency graph is
            # complete for cycle detection. The parent depends on this cell even
            # though it creates a circular reference.
            if tracking and parent_key and node_key:
                pending_precedents.setdefault(parent_key, set()).add(node_key)
                # Commit parent's precedents immediately so the edge is recorded
                # before the exception propagates
                parent_precedents = pending_precedents.pop(parent_key, set())
                if parent_precedents:
                    self._dep_graph.replace_precedents(parent_key, parent_precedents)
            # Clean up eval_context before raising.  The append at line 8240
            # is not covered by the try/finally below, so without this cleanup
            # the stale node_key would remain in eval_context and corrupt
            # subsequent evaluations' dependency tracking.  Do NOT touch
            # pending_precedents or _tracked_nodes here — the victim's own
            # try/finally will handle those when it completes.
            if tracking and node_key is not None and eval_context and eval_context[-1] == node_key:
                eval_context.pop()
            raise CircularReferenceError(f"Circular reference at {(cube.id, addr)}")
        eval_stack.add(key)
        try:
            if rule is not None:
                # Caller supplied the rule; use it directly.
                pass
            else:
                anchored = self._ws.find_anchored_rule(cube.id, addr)
                if anchored is not None:
                    rule = anchored
                else:
                    rule = self._ws.find_rule(cube.id, addr, cube.dimension_ids)
            if rule is None:
                success = True
                # Always commit for non-rule cells when tracking.
                # Even empty cells must record the edge to their parent rule
                # so that future edits to this cell invalidate dependents.
                if tracking and node_key:
                    _commit_precedents(success, had_rule_body)
                return v
            resolver = self._make_resolver(cube)
            t0 = time.perf_counter()
            try:
                expr = self._normalize_expression(rule.expression)
                had_rule_body = True
                result = self._rule_evaluator.eval(expr, resolver=resolver, base_addr=addr)
                if isinstance(result, complex):
                    result = CellError("#NUM!")
                if node_key is not None:
                    self._recompute_counts[node_key] = self._recompute_counts.get(node_key, 0) + 1
                dt = time.perf_counter() - t0
                self._record_rule_eval_profile(expr, dt * 1000.0)
                if dt > SLOW_LOG_THRESHOLD and _DEBUG_ENGINE:
                    print(f"[timing] rule eval {rule.id} at {addr}: {dt*1000:.1f} ms", flush=True)
                cube.set(addr, result)
                success = True
                # Commit precedents to record dependency edges for the rule
                if tracking and node_key:
                    _commit_precedents(success, had_rule_body)
                return result
            except ZeroDivisionError:
                err = CellError("#DIV/0!")
                cube.set(addr, err)
                success = True
                return err
            except OverflowError:
                err = CellError("#NUM!")
                cube.set(addr, err)
                success = True
                return err
            except CircularReferenceError:
                err = CellError("#CIRC!")
                cube.set(addr, err)
                success = True
                return err
            except TypeError:
                err = CellError("#VALUE!")
                cube.set(addr, err)
                success = True
                return err
            except SyntaxError:
                err = CellError("#SYNTAX!")
                cube.set(addr, err)
                success = True
                return err
            except RuleValidationError:
                # Surface validation failures (e.g. invalid dynamic bounds)
                # to callers instead of mapping them to EXPRESSION!.
                raise
            except Exception:
                err = CellError("#EXPRESSION!")
                cube.set(addr, err)
                success = True
                return err
        finally:
            eval_stack.discard(key)
            # Only commit precedents if there are pending precedents to commit.
            thread_disabled = getattr(self._thread_eval_state, "tracking_disabled", False)
            if self._dep_tracking_enabled and not thread_disabled and node_key is not None:
                precedents = pending_precedents.pop(node_key, set())
                if precedents:
                    self._dep_graph.replace_precedents(node_key, precedents)
                # Track this node even when _commit_precedents was skipped
                # (e.g. anchored rules, cached values with no pending precedents).
                self._tracked_nodes.add(node_key)
            if tracking and node_key is not None and eval_context:
                eval_context.pop()
            if success:
                    # Clear dirty flag using unified key format (always includes @ dimension)
                    if node_key is not None:
                        self._dep_graph.clear_dirty(node_key)

    def get_cell_by_addr(self, cube: Cube, addr: tuple[str, ...]) -> Any:
        """Public: get a cell value by raw address.

        Resolves rules, tracks dependencies, and returns the computed or
        hard-coded value.  Returns a ``CellError`` sentinel (e.g.
        ``#DIV/0!``, ``#CIRC!``, ``#EXPRESSION!``) on evaluation failure.
        Raises ``CircularReferenceError`` for unresolvable cycles.
        """
        return self._get_cell_by_addr(cube, addr)

    def resolve_cell_meta(self, cube: Cube, addr: tuple[str, ...]) -> CellMeta:
        """Read-only metadata lookup for a cell.

        Does not evaluate rules, record dependency edges, or mutate the engine.
        """
        addr = _normalize_addr_for_cube(cube, addr)
        with self._cube_locks.setdefault(cube.id, threading.Lock()):
            v = cube.get(addr)
            is_override = cube.is_user_override(addr)
            has_rule_body = self._ws.find_anchored_rule(cube.id, addr) is not None
            has_rule = self._ws.find_rule(cube.id, addr, cube.dimension_ids) is not None
            has_rule = has_rule_body or has_rule

            if has_rule and is_override:
                source = "override"
            elif has_rule:
                source = "rule"
            elif is_override:
                source = "override"
            elif v is None:
                source = "empty"
            else:
                source = "input"

            error = v.code if isinstance(v, CellError) else None
            node_key = self._cell_node_key(cube.id, addr)
            is_dirty = self._dep_graph.is_dirty(node_key)
            is_tracked = node_key in self._tracked_nodes
            return CellMeta(
                source=source,
                has_rule=has_rule,
                is_override=is_override,
                is_dirty=is_dirty,
                is_tracked=is_tracked,
                error=error,
            )

    def get_cached_cell_value_by_addr(self, cube: Cube, addr: tuple[str, ...]) -> Any:
        """Read-only value access from the cube cache.

        Never evaluates rules, looks up rules, or touches the dependency graph.
        """
        addr = _normalize_addr_for_cube(cube, addr)
        with self._cube_locks.setdefault(cube.id, threading.Lock()):
            return cube.get(addr)

    @property
    def generation(self) -> int:
        """Global generation counter for the workspace session."""
        return self._generation

    @property
    def is_gui_ready(self) -> bool:
        """True after the load-time dependency-graph bootstrap has completed."""
        return getattr(self, "_gui_ready", False)

    def bump_generation(self) -> int:
        """Increment the workspace generation and return the new value."""
        self._generation += 1
        return self._generation

    def cell_value_for_view_rc(self, view_id: str, row: int, col: int) -> CellValue:
        # Defensive: clear any stale eval stack state
        self._thread_eval_stack().clear()
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)
        addr = self._addr_for_view_rc(view_id, row, col)
        v = cube.get(addr)

        if v is not None:
            rule = self._ws.find_anchored_rule(cube.id, addr)
            rule = self._ws.find_rule(cube.id, addr, cube.dimension_ids)
            # Check if this is a manual override (user explicitly set this cell)
            is_override = cube.is_user_override(addr)
            if rule is not None:
                expr = self._normalize_expression(rule.expression)
                # Only "override" if user manually entered; otherwise it's "rule"
                source = "override" if is_override else "rule"
                return CellValue(
                    value=v,
                    explain=Explain(source=source, cube_id=cube.id, addr=addr, rule_body=expr),
                )
            if rule is not None:
                expr = self._normalize_expression(rule.expression)
                # Only "override" if user manually entered; otherwise it's "rule"
                source = "override" if is_override else "rule"
                return CellValue(
                    value=v,
                    explain=Explain(source=source, cube_id=cube.id, addr=addr, rule_body=expr),
                )
            return CellValue(value=v, explain=Explain(source="input", cube_id=cube.id, addr=addr))

        rule = self._ws.find_anchored_rule(cube.id, addr)
        if rule is None:
            rule = self._ws.find_rule(cube.id, addr, cube.dimension_ids)
            if rule is None:
                return CellValue(value=None, explain=Explain(source="empty", cube_id=cube.id, addr=addr))
            compute_trace(f"EVAL rule {rule.id[:8]} at {addr} = {rule.expression}")
            resolver = self._make_resolver(cube)
            expr = self._normalize_expression(rule.expression)
            key = (cube.id, addr)
            eval_stack = self._thread_eval_stack()
            eval_stack.add(key)
            try:
                computed = self._rule_evaluator.eval(expr, resolver=resolver, base_addr=addr)
                return CellValue(
                    value=computed,
                    explain=Explain(source="rule", cube_id=cube.id, addr=addr, rule_body=expr),
                )
            except ZeroDivisionError:
                return CellValue(
                    value=CellError("#DIV/0!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#DIV/0!"),
                )
            except OverflowError:
                return CellValue(
                    value=CellError("#NUM!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#NUM!"),
                )
            except TypeError:
                return CellValue(
                    value=CellError("#VALUE!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#VALUE!"),
                )
            except CircularReferenceError:
                return CellValue(
                    value=CellError("#CIRC!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#CIRC!"),
                )
            except Exception:
                return CellValue(
                    value=CellError("#EXPRESSION!"),
                    explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#EXPRESSION!"),
                )
            finally:
                eval_stack.discard(key)

        compute_trace(f"EVAL cell rule at {addr} = {rule.expression}")
        resolver = self._make_resolver(cube)
        expr = self._normalize_expression(rule.expression)
        key = (cube.id, addr)
        eval_stack = self._thread_eval_stack()
        eval_stack.add(key)
        try:
            computed = self._rule_evaluator.eval(expr, resolver=resolver, base_addr=addr)
            return CellValue(
                value=computed,
                explain=Explain(source="rule", cube_id=cube.id, addr=addr, rule_body=expr),
            )
        except ZeroDivisionError:
            return CellValue(
                value=CellError("#DIV/0!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#DIV/0!"),
            )
        except OverflowError:
            return CellValue(
                value=CellError("#NUM!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#NUM!"),
            )
        except TypeError:
            return CellValue(
                value=CellError("#VALUE!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#VALUE!"),
            )
        except CircularReferenceError:
            return CellValue(
                value=CellError("#CIRC!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#CIRC!"),
            )
        except Exception:
            return CellValue(
                value=CellError("#EXPRESSION!"),
                explain=Explain(source="error", cube_id=cube.id, addr=addr, rule_body=expr, error="#EXPRESSION!"),
            )
        finally:
            eval_stack.discard(key)

    def get_range(self, view_id: str, top: int, left: int, bottom: int, right: int) -> list[list[Any]]:
        out: list[list[Any]] = []
        for r in range(top, bottom + 1):
            row: list[Any] = []
            for c in range(left, right + 1):
                row.append(self.cell_value_for_view_rc(view_id, r, c).value)
            out.append(row)
        return out

    def set_range(self, view_id: str, top: int, left: int, values: list[list[Any]]) -> None:
        """Set a rectangular range starting at (top,left).

        This is a single undo step.
        """

        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)

        actions: list[Action] = []
        for dr, row in enumerate(values):
            for dc, v in enumerate(row):
                addr = self._addr_for_view_rc(view_id, top + dr, left + dc)
                prev = cube.get(addr)
                if prev == v:
                    continue
                actions.append(_CellEditAction(engine=self, cube=cube, addr=addr, before=prev, after=v))

        if not actions:
            return

        self._undo.push_and_do(CompositeAction(actions))

    def _validate_cell_rule_addr(self, cube: Cube, addr: tuple[str, ...], expression: str) -> None:
        # Check for bidirectional recurrence rules before anything else
        self._validate_no_bidirectional_recurrence(expression)

        prev_anchored = self._ws.find_anchored_rule(cube.id, addr)
        prev_value = cube.get(addr)
        # CRITICAL: Preserve user_override_addrs during validation
        # upsert_cell_rule clears overrides, but validation is temporary
        prev_override_addrs = set(cube.user_override_addrs)
        normalized = self._normalize_expression(expression)
        self._ws.upsert_cell_rule(cube.id, addr, normalized)
        cube.set(addr, None)
        self._invalidate_cell_node(cube.id, addr)
        self._eval_strict_mode = True
        try:
            resolver = self._make_resolver(cube)
            expr = self._normalize_expression(expression)
            key = (cube.id, addr)
            eval_stack = self._thread_eval_stack()
            eval_stack.add(key)
            try:
                self._rule_evaluator.eval(expr, resolver=resolver, base_addr=addr)
            except CircularReferenceError:
                # Allow committing rules that participate in circular
                # references; they will surface as CIRC! at evaluation time
                # instead of being rejected at entry.
                pass
            except ZeroDivisionError:
                pass
            except Exception:
                pass
            finally:
                eval_stack.discard(key)
        finally:
            self._eval_strict_mode = False
            cube.set(addr, prev_value)
            if prev_anchored is None:
                self._ws.delete_cell_rule(cube.id, addr)
            else:
                self._ws.upsert_cell_rule(cube.id, addr, prev_anchored.expression)
            # Restore user_override_addrs that were cleared during validation
            cube.user_override_addrs = prev_override_addrs
            self._cell_cache.clear()
            # Clear function cache to prevent stale entries from validation
            # Validation evaluates rules without proper eval_context, so function
            # nodes may be cached without parent cell edges
            self._function_cache.clear()

    def _validate_rule_entry(
        self,
        cube: Cube,
        dim_id: str,
        item_id: str,
        expression: str,
        addr_mask: tuple[str | None, ...] | None = None,
        max_cells: int | None = None,
    ) -> None:
        # Build effective mask to find existing rule
        if addr_mask is not None and len(addr_mask) == len(cube.dimension_ids):
            lookup_mask = addr_mask
        else:
            # Legacy: build mask from dim_id/item_id
            mask_list: list[str | None] = [None] * len(cube.dimension_ids)
            try:
                slot = cube.dimension_ids.index(dim_id)
                mask_list[slot] = item_id
            except ValueError:
                pass
            lookup_mask = tuple(mask_list)
        
        prev_rule = next(
            (
                r
                for r in self._ws.rules.values()
                if r.cube_id == cube.id and r.addr_mask == lookup_mask
            ),
            None,
        )
        prev_order = list(self._ws.rule_order)
        cleared_values: dict[tuple[str, ...], Any] = {}
        applied_rule = self._ws.upsert_rule(cube.id, dim_id, item_id, expression, addr_mask=addr_mask)
        self._invalidate_cube(cube.id)
        try:
            expr = self._normalize_expression(expression)
            resolver = self._make_resolver(cube)

            # Determine the effective mask we should validate across. If a
            # full multi-dimension ``addr_mask`` was supplied, use that;
            # otherwise constrain only the primary ``dim_id``/``item_id`` and
            # wildcard all other dimensions.
            effective_mask: list[str | None]
            if addr_mask is not None and len(addr_mask) == len(cube.dimension_ids):
                effective_mask = list(addr_mask)
            else:
                effective_mask = [None] * len(cube.dimension_ids)
                try:
                    slot = cube.dimension_ids.index(dim_id)
                except ValueError:
                    slot = -1
                if 0 <= slot < len(effective_mask):
                    effective_mask[slot] = item_id

            axis_item_ids: list[list[str]] = []
            for idx, cube_dim_id in enumerate(cube.dimension_ids):
                # Handle special @ dimension (technical dimension for value/fill channels)
                if cube_dim_id == "@":
                    fixed_item = effective_mask[idx]
                    if fixed_item is not None:
                        axis_item_ids.append([fixed_item])
                    else:
                        axis_item_ids.append([CHANNEL_TO_AT_ID["value"]])
                    continue
                dim = self.require_dimension_by_id(cube_dim_id)
                fixed_item = effective_mask[idx]
                if fixed_item is not None:
                    axis_item_ids.append([fixed_item])
                else:
                    axis_item_ids.append([it.id for it in dim.items])
            # Enable strict mode so any recursive evaluation that comes back to
            # the same address is treated as a hard circular-reference error,
            # not silently coerced to 0.0.
            prev_strict = getattr(self, "_eval_strict_mode", False)
            self._eval_strict_mode = True
            try:
                evaluated = 0
                for raw_addr in itertools.product(*axis_item_ids):
                    addr = tuple(raw_addr)
                    if addr_mask is not None and len(addr) == len(addr_mask):
                        masked_addr = tuple(a if m is not None else None for a, m in zip(addr, addr_mask))
                        if masked_addr != addr_mask:
                            continue
                    # PRECEDENCE: Hard numbers > cell rules > rules
                    # Skip validation for cells with hard numbers or cell rules
                    if cube.is_user_override(addr):
                        continue
                    if self._ws.find_anchored_rule(cube.id, addr) is not None:
                        continue
                    evaluated += 1
                    if max_cells is not None and evaluated > max_cells:
                        break
                    prev_val = cube.get(addr)
                    cleared_values[addr] = prev_val
                    cube.set(addr, None)
                    key = (cube.id, addr)
                    eval_stack = self._thread_eval_stack()
                    eval_stack.add(key)
                    node_key = None
                    try:
                        # Set up tracking context for rule cell before evaluation
                        if self._dep_tracking_enabled:
                            node_key = self._cell_node_key(cube.id, addr)
                            self._thread_eval_context().append(node_key)
                            self._thread_pending_precedents().setdefault(node_key, set())
                        result = self._rule_evaluator.eval(expr, resolver=resolver, base_addr=addr)
                        # Store the computed result in the cube so it persists
                        cube.set(addr, result)
                        # Validation is effectively a recompute for this cell;
                        # mark it tracked/clean so the read-only snapshot path
                        # does not reject the freshly computed value.
                        if self._dep_tracking_enabled and node_key is not None:
                            self._tracked_nodes.add(node_key)
                            self._dep_graph.clear_dirty(node_key)
                    except CircularReferenceError:
                        # Do not prevent the rule from being committed if it
                        # participates in a circular dependency; the runtime
                        # evaluation path will surface CIRC! in affected
                        # cells instead.
                        pass
                    except ZeroDivisionError:
                        pass
                    except Exception:
                        pass
                    finally:
                        eval_stack.discard(key)
                        # Commit precedents and clean up tracking context
                        # Check both global flag and thread-local override (worker threads disable tracking)
                        thread_disabled = getattr(self._thread_eval_state, "tracking_disabled", False)
                        if self._dep_tracking_enabled and not thread_disabled and node_key is not None:
                            precedents = self._thread_pending_precedents().pop(node_key, set())
                            self._dep_graph.replace_precedents(node_key, precedents)
                            if self._thread_eval_context() and self._thread_eval_context()[-1] == node_key:
                                self._thread_eval_context().pop()
            finally:
                self._eval_strict_mode = prev_strict
        except Exception:
            # Only roll back on validation failure
            for addr, prev_value in cleared_values.items():
                cube.set(addr, prev_value)
            if prev_rule is None:
                self._ws.delete_rule(applied_rule.id)
            else:
                self._ws.rules[prev_rule.id] = prev_rule
            self._ws.rule_order = prev_order
            self._cell_cache.clear()
            raise

    def set_cell_hardvalue_by_addr(
        self, cube_id: str, addr: tuple[str, ...], value: Any
    ) -> None:
        """Set a cell hardvalue given a full cube address tuple.

        This is the canonical replacement for the deprecated
        ``set_cell_value_by_addr``.  It normalizes the address, resolves or
        creates a default view, and delegates to ``set_cell_hardvalue``.
        """
        cube = self.require_cube_by_id(cube_id)
        full_addr = _normalize_addr_for_cube(cube, addr)
        view_id = self._find_or_create_default_view(cube_id)
        view = self.require_view_by_id(view_id)
        row_dim_count = len(view.row_dim_ids)
        body = tuple(item_id for dim_id, item_id in zip(cube.dimension_ids, full_addr) if dim_id != "@")
        channel = next(
            (full_addr[i] for i, dim_id in enumerate(cube.dimension_ids) if dim_id == "@"),
            None,
        )
        if channel is not None:
            channel = channel.replace("at_", "")
        cell_ref = {
            "kind": "ids",
            "row_key": body[:row_dim_count],
            "col_key": body[row_dim_count:],
            "channel": channel,
        }
        if value is None:
            self.clear_cell_hardvalue(view_id, cell_ref)
        else:
            self.set_cell_hardvalue(view_id, cell_ref, value)

    def _find_or_create_default_view(self, cube_id: str) -> str:
        """Return a view for the cube, creating a default one if necessary.

        Default layout: first dimension as rows, remaining as columns.
        This is an internal helper; public callers should use
        ``resolve_default_view_id_by_cube`` and ``create_default_view_for_cube``.
        """
        existing_id = self.resolve_default_view_id_by_cube(cube_id)
        if existing_id is not None:
            return existing_id
        return self.create_default_view_for_cube(cube_id)

    def resolve_default_view_id_by_cube(self, cube_id: str) -> str | None:
        """Return the ID of an existing view for the cube, or None if absent."""
        for view in self.list_views():
            view_id = getattr(view, "id", view)
            view_obj = self.require_view_by_id(view_id)
            if getattr(view_obj, "cube_id", None) == cube_id:
                return view_id
        return None

    def create_default_view_for_cube(self, cube_id: str) -> str:
        """Create a default view for the cube and return its ID.

        Default layout: first dimension as rows, remaining as columns.
        """
        cube = self.require_cube_by_id(cube_id)
        dim_ids = [d for d in cube.dimension_ids if d != "@"]
        if not dim_ids:
            raise ValueError(f"Cube {cube_id} has no dimensions; cannot create a default view")

        view = self.create_view(
            name=f"default_view_{cube_id[:12]}",
            cube_id=cube_id,
            row_dim_id=dim_ids[0],
            col_dim_id=dim_ids[1] if len(dim_ids) > 1 else None,
            page_dim_ids=dim_ids[2:] if len(dim_ids) > 2 else [],
        )
        return view.id

    def addr_to_cell_ref(self, cube_id: str, addr: tuple[str, ...]) -> dict:
        """Split a positional tuple into a channel-aware cell_ref.

        Uses the cube's default view to determine the row/col split.
        """
        cube = self.require_cube_by_id(cube_id)
        full_addr = _normalize_addr_for_cube(cube, addr)
        view_id = self._find_or_create_default_view(cube_id)
        view = self.require_view_by_id(view_id)
        row_dim_count = len(view.row_dim_ids)
        body = tuple(item_id for dim_id, item_id in zip(cube.dimension_ids, full_addr) if dim_id != "@")
        channel = next(
            (full_addr[i] for i, dim_id in enumerate(cube.dimension_ids) if dim_id == "@"),
            None,
        )
        if channel is not None:
            channel = channel.replace("at_", "")
        return {
            "kind": "ids",
            "row_key": body[:row_dim_count],
            "col_key": body[row_dim_count:],
            "channel": channel,
        }

    def set_rule_anchored_by_addr(self, cube_id: str, addr: tuple[str, ...], expression: str) -> None:
        """Attach an anchored rule to a cell given by a positional cube address."""
        view_id = self._find_or_create_default_view(cube_id)
        cell_ref = self.addr_to_cell_ref(cube_id, addr)
        self.set_rule_anchored(view_id, cell_ref, expression)

    def _set_rule_anchored_by_ids(
        self, view_id: str, row_key: tuple[str, ...], col_key: tuple[str, ...], expression: str
    ) -> None:
        """Create an anchored rule for a specific cell.
        
        This converts cell rule entry into an anchored rule, which is the
        modern OpenM approach (cell rules are deprecated).
        """
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)
        expression = self._normalize_expression(expression)
        addr = self._addr_for_view_ids(view_id, row_key=row_key, col_key=col_key)
        
        # Build targets from the address: (dim_name, item_name) pairs
        targets: list[tuple[str, str]] = []
        for dim_id, item_id in zip(cube.dimension_ids, addr):
            if dim_id == "@":
                continue  # Skip @ dimension - it's the value channel implicitly
            dim = self._ws.dimensions.get(dim_id)
            if dim is None:
                continue
            # Get item name from ID
            item_name = next((it.name for it in dim.items if it.id == item_id), item_id)
            targets.append((dim.name, item_name))
        
        # Create anchored rule
        self.set_rule(cube.id, targets, expression, is_anchored=True)

    def _scan_all_rule_bodies_for_volatile(self) -> None:
        """Scan all existing rules and track those with volatile functions.

        Call this after loading a workspace to ensure volatile tracking is complete.
        """
        self._volatile_cells.clear()
        for rid, r in self._ws.rules.items():
            if r.is_anchored and r.addr_mask is not None:
                self._track_volatile_cell(r.cube_id, r.addr_mask, r.expression)

    def batch_set_cell_data(
        self,
        cube_id: str,
        values: dict[tuple[str, ...], Any] | None = None,
        rules: dict[tuple[str, ...], str] | None = None,
    ) -> None:
        """Batch set cell values and rules efficiently.

        This is optimized for bulk imports - it clears caches only once at the end
        and suppresses per-cell change notifications.

        Args:
            cube_id: Target cube ID
            values: Mapping of address -> value to set
            rules: Mapping of address -> rule expression to set
        """
        cube = self.require_cube_by_id(cube_id)
        values = values or {}
        rules = rules or {}

        # Set all values first (bypassing per-cell cache clear)
        for addr, val in values.items():
            if len(addr) != len(cube.dimension_ids):
                continue
            if val is None:
                if addr in cube.data:
                    del cube.data[addr]
            else:
                cube.data[addr] = val
            cube.user_override_addrs.add(addr)

        # Set all rules
        for addr, expr in rules.items():
            if len(addr) != len(cube.dimension_ids):
                continue
            expr = self._normalize_expression(expr)
            self._ws.upsert_cell_rule(cube.id, addr, expr)
            cube.user_override_addrs.discard(addr)

        # Single cache clear and notification at the end
        self._cell_cache.clear()
        # Phase 4: emit batch cell update event
        changed_addrs = list(values.keys()) + list(rules.keys())
        if changed_addrs:
            self._publish_event(EVENT_CELLS_UPDATED, {
                "cube_id": cube_id,
                "addresses": changed_addrs,
                "count": len(changed_addrs),
                "changed_fields": ["value", "rule"],
            })

    def batch_set_cell_hardvalues_by_addr(
        self,
        cube_id: str,
        entries: list[tuple[tuple[str, ...], Any]],
    ) -> int:
        """Set many cell hardvalues in one call with a single undo action.

        Each entry is ``(addr, value)``.  Addresses are normalised to the
        cube's full dimension order.  A single undo action, a single cache
        clear, and a single ``event.cells.updated`` publication replace
        the per-cell overhead of calling ``set_cell_hardvalue_by_addr`` in a
        loop.

        Returns the number of cells actually set.
        """
        cube = self.require_cube_by_id(cube_id)
        changes: list[tuple[tuple[str, ...], Any, Any]] = []
        for addr, value in entries:
            full_addr = _normalize_addr_for_cube(cube, tuple(addr))
            if value is None:
                continue
            coerced = coerce_user_value(value)
            before = cube.get(full_addr)
            if coerced == before and full_addr in cube.user_override_addrs:
                continue
            changes.append((full_addr, before, coerced))
        if not changes:
            return 0
        action = _BatchCellEditAction(
            engine=self,
            cube=cube,
            changes=changes,
        )
        self._undo.push_and_do(action)
        return len(changes)

    def _delete_cell_rule(self, view_id: str, row: int, col: int) -> bool:
        """Delete the cell-level rule at the given row/column coordinates.

        Resolves ``(row, col)`` to a canonical cell address via
        ``_addr_for_view_rc``, then removes any rule attached to that cell.
        If a rule was removed, the computed value is cleared (unless the cell
        holds a user hardnumber), and the cell cache is invalidated.

        Prefer ``delete_cell_rule_by_keys`` for stable addressing; this
        index-based method is sensitive to view layout changes.

        Args:
            view_id: Stable view identifier.
            row: Zero-based row index in the view grid.
            col: Zero-based column index in the view grid.

        Returns:
            ``True`` if a rule existed and was removed; ``False`` otherwise.
        """
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)
        addr = self._addr_for_view_rc(view_id, row=row, col=col)
        removed = self._ws.delete_cell_rule(cube.id, addr)
        if removed:
            # Clear the computed value (unless it's a user hardnumber)
            if addr not in cube.user_override_addrs:
                cube.set(addr, None)
            self._cell_cache.clear()
        return removed

    def _delete_rule_anchored_by_ids(
        self, view_id: str, row_key: tuple[str, ...], col_key: tuple[str, ...]
    ) -> bool:
        """Delete the cell-level rule at the address resolved from dimension item keys.

        Resolves ``row_key`` and ``col_key`` to a canonical cell address via
        ``_addr_for_view_ids``, then removes any rule attached to that cell.
        If a rule was removed, the computed value is cleared (unless the cell
        holds a user hardnumber), and the cell cache is invalidated.

        Args:
            view_id: Stable view identifier.
            row_key: Tuple of dimension item IDs identifying the row.
            col_key: Tuple of dimension item IDs identifying the column.

        Returns:
            ``True`` if a rule existed and was removed; ``False`` otherwise.
        """
        view = self.require_view_by_id(view_id)
        cube = self.require_cube_by_id(view.cube_id)
        addr = self._addr_for_view_ids(view_id, row_key=row_key, col_key=col_key)
        removed = self._ws.delete_cell_rule(cube.id, addr)
        if removed:
            # Clear the computed value (unless it's a user hardnumber)
            if addr not in cube.user_override_addrs:
                cube.set(addr, None)
            self._cell_cache.clear()
        return removed

    def delete_rule(self, rule_id: str) -> bool:
        removed = self._ws.delete_rule(rule_id)
        if removed:
            self._cell_cache.clear()
        return removed

    # Alias for Phase 5A migration
    delete_rule = delete_rule

    def _delete_cell_rule_by_id(self, rule_id: str) -> bool:
        r = self._ws.rules.get(rule_id)
        removed = self._ws.delete_cell_rule_by_id(rule_id)
        if removed and r is not None:
            cube = self.require_cube_by_id(r.cube_id)
            # Clear the computed value (unless it's a user hardnumber)
            if r.addr_mask is not None and r.addr_mask not in cube.user_override_addrs:
                cube.set(r.addr_mask, None)
            self._cell_cache.clear()
        return removed

    def update_cell_rule(self, rule_id: str, expression: str) -> None:
        expression = self._normalize_expression(expression)
        r = self._ws.rules.get(rule_id)
        if r is None:
            raise KeyError(rule_id)
        cube = self.require_cube_by_id(r.cube_id)
        if r.addr_mask is not None:
            self._validate_cell_rule_addr(cube, r.addr_mask, expression)
        self._ws.rules[rule_id] = r.__class__(
            id=r.id, cube_id=r.cube_id, expression=expression,
            addr_mask=r.addr_mask, targets=r.targets, is_anchored=r.is_anchored,
        )
        self._cell_cache.clear()

    def update_rule(self, rule_id: str, expression: str) -> None:
        """Update only the expression of an existing rule, preserving its mask.

        This is used for non-LHS edits where the rule's target address mask
        does not change.
        """

        expression = self._normalize_expression(expression)
        r = self._ws.rules.get(rule_id)
        if r is None:
            raise KeyError(rule_id)
        cube = self.require_cube_by_id(r.cube_id)
        if self._is_whole_cube_rule_mask(cube, r.addr_mask, r.targets):
            self._validate_cross_cube_wildcard_mapping_dims(cube, expression)
        self._validate_rule_entry(cube, "", "", expression, addr_mask=r.addr_mask)
        self._ws.rules[rule_id] = r.__class__(
            id=r.id,
            cube_id=r.cube_id,
            expression=expression,
            addr_mask=r.addr_mask,
            targets=r.targets,
            is_anchored=r.is_anchored,
        )
        self._cell_cache.clear()

    def update_rule_full(
        self,
        rule_id: str,
        targets: list[tuple[str, str]],
        expression: str,
        is_anchored: bool = False,
    ) -> None:
        """Update a rule's target (possibly multi-dim) and expression.

        ``targets`` is a list of (dim_name, item_name) pairs that define the
        rule's constrained dimensions. The first pair is treated as the
        "primary" dimension/item for grouping/back-compat purposes.
        """

        from lib_openm.rule_eval import _parse_ref_segment  # type: ignore[attr-defined]

        r = self._ws.rules.get(rule_id)
        if r is None:
            raise KeyError(rule_id)

        cube = self.require_cube_by_id(r.cube_id)
        expression = self._normalize_expression(expression)

        # Reuse the same wildcard-aware resolution logic as set_rule.
        addr_mask, primary_dim_id, primary_item_id = self._resolve_rule_targets(
            cube, targets, use_defaults_for_unspecified=is_anchored
        )
        if self._is_whole_cube_rule_mask(cube, addr_mask, tuple(targets)):
            self._validate_cross_cube_wildcard_mapping_dims(cube, expression)

        # Prevent two different rules from targeting the same cube/mask.
        for other in self._ws.rules.values():
            if other.id == rule_id or other.cube_id != cube.id:
                continue
            other_mask = other.addr_mask
            if other_mask is None:
                # Rule has no mask - cannot conflict
                continue
            if other_mask == addr_mask:
                raise RuleValidationError(
                    "A rule for this multi-dimension target already exists. "
                    "Edit or delete that rule instead."
                )

        # Validate the new expression against the proposed target mask.
        # _validate_rule_entry side-effects the workspace via upsert_rule.
        # When the mask changes, upsert_rule creates a NEW rule with a new ID.
        # We must clean up that temporary rule before committing the in-place update.
        prev_order = list(self._ws.rule_order)
        self._validate_rule_entry(cube, primary_dim_id, primary_item_id, expression, addr_mask=addr_mask)

        # Remove any temporary rule created during validation that is not the original.
        for temp_rule in list(self._ws.rules.values()):
            if (
                temp_rule.cube_id == cube.id
                and temp_rule.addr_mask == addr_mask
                and temp_rule.id != rule_id
            ):
                del self._ws.rules[temp_rule.id]
                if temp_rule.id in self._ws.rule_order:
                    self._ws.rule_order.remove(temp_rule.id)
        # Restore original order (validation may have appended a new rule ID).
        self._ws.rule_order = prev_order

        # Commit the updated rule, preserving its id (and therefore order).
        self._ws.rules[rule_id] = r.__class__(
            id=r.id,
            cube_id=cube.id,
            expression=expression,
            addr_mask=addr_mask,
            targets=tuple(targets),
            is_anchored=is_anchored,
        )
        # Validation may have rebuilt the rule index while the temporary rule
        # still existed.  Since we just removed that temp rule, the index is
        # stale and must be invalidated so the next lookup rebuilds cleanly.
        self._ws._invalidate_rule_index()
        self._cell_cache.clear()

    def _normalize_expression(self, expression: str) -> str:
        """
        Accept assignment-like input (e.g., "Quarter[THIS]=Quarter[PREV]*1.05") and keep only the RHS.
        Avoid touching comparison operators and '=' inside quoted strings.
        """
        expr = expression.strip()
        if not expr:
            return expr
        # If user typed an assignment with a single '=', treat RHS as the expression.
        # Skip if equality/inequality tokens are present.
        if any(tok in expr for tok in ("==", "<=", ">=", "!=", "<>")):
            return expr
        # Find the first '=' that is NOT inside a quoted string.
        eq_pos = _find_unquoted_equals(expr)
        if eq_pos >= 0:
            # If there's an opening paren before '=', assume it's a comparison (e.g., IF(cond)) not an assignment.
            if "(" not in expr[:eq_pos]:
                rhs = expr[eq_pos + 1:]
                if rhs.strip():
                    expr = rhs.strip()
        return expr

    # ------------------------------------------------------------------
    # Rule target resolution (including wildcard sugar)
    # ------------------------------------------------------------------

    def _get_default_item(self, dim_id: str) -> Any:
        """Return the default item for a dimension (first item)."""
        if dim_id == "@":
            # Return a mock object with id=CHANNEL_TO_AT_ID["value"] for the @ dimension
            return SimpleNamespace(id=CHANNEL_TO_AT_ID["value"])
        dim = self.require_dimension_by_id(dim_id)
        return dim.items[0] if dim.items else None

    def _resolve_rule_targets(
        self,
        cube: Cube,
        targets: list[tuple[str, str]],
        use_defaults_for_unspecified: bool = False,
    ) -> tuple[tuple[str | None, ...], str, str]:
        """Resolve textual rule targets into (addr_mask, primary_dim_id, primary_item_id).

        Supported wildcard sugar (produced by ``parse_rule_target``):

        - ``*`` → ``[("*", "*")]``: whole-cube wildcard; all dimensions are
          unconstrained. The rule is grouped under the cube's first dimension
          for back-compat purposes.
        - ``Dim.*`` → ``[("Dim", "*")]``: dimension-level wildcard. The rule
          is grouped under ``Dim`` but does not constrain that dimension in the
          address mask (all its items are included).
        - ``*.Item`` → ``[("*", "Item")]``: infer the unique dimension in
          this cube that contains ``Item``; equivalent to ``Dim.Item`` for that
          dimension. If zero or multiple dimensions contain such an item, a
          ``RuleValidationError`` is raised.

        Args:
            use_defaults_for_unspecified: If True, use default items for dimensions
                not explicitly specified (creating a "full mask" for anchored rules).
                If False, use None/wildcard (standard rule behavior).
        """

        if not targets:
            raise RuleValidationError("Rule target must specify at least one Dim.Item")

        # Whole-cube wildcard: "*" on its own.
        if len(targets) == 1 and targets[0][0] == "*" and targets[0][1] == "*":
            if use_defaults_for_unspecified:
                # For anchored whole-cube: use defaults for all dimensions
                mask: list[str | None] = []
                for dim_id in cube.dimension_ids:
                    default_item = self._get_default_item(dim_id)
                    mask.append(default_item.id if default_item else None)
                primary_dim_id = cube.dimension_ids[0] if cube.dimension_ids else ""
                primary_item_id = mask[0] if mask else "*"
                return tuple(mask), primary_dim_id, primary_item_id
            else:
                mask = [None] * len(cube.dimension_ids)
                primary_dim_id = cube.dimension_ids[0] if cube.dimension_ids else ""
                primary_item_id = "*"
                return tuple(mask), primary_dim_id, primary_item_id

        mask: list[str | None] = [None] * len(cube.dimension_ids)
        dim_ids: list[str] = []
        item_ids: list[str] = []

        for dim_name, item_name in targets:
            # Infer dimension when using ``*.Item`` syntax.
            if dim_name == "*" and item_name != "*":
                item_lower = item_name.lower()
                candidate_dims: list[Any] = []
                for dim_id in cube.dimension_ids:
                    dim = self.require_dimension_by_id(dim_id)
                    if any(it.name.lower() == item_lower for it in dim.items):
                        candidate_dims.append(dim)
                if not candidate_dims:
                    raise RuleValidationError(
                        f"Item {item_name!r} not found in any dimension of cube {cube.name!r}"
                    )
                if len(candidate_dims) > 1:
                    dim_names = ", ".join(d.name for d in candidate_dims)
                    raise RuleValidationError(
                        f"Ambiguous *.{item_name}: item exists in multiple dimensions: {dim_names}"
                    )
                dim = candidate_dims[0]
            elif dim_name == "*" and item_name == "*":
                # Redundant whole-cube wildcard inside a larger target list;
                # it does not constrain the mask.
                continue
            else:
                dim = next(
                    (d for d in self._ws.dimensions.values() if d.name.lower() == dim_name.lower()),
                    None,
                )
                if dim is None:
                    raise RuleValidationError(f"Unknown dimension: {dim_name!r}")
                if dim.id not in cube.dimension_ids:
                    raise RuleValidationError(f"Dimension {dim_name!r} is not part of cube {cube.name!r}")

            slot = cube.dimension_ids.index(dim.id)

            # Dimension-level wildcard: "Dim.*" — record the dimension for
            # grouping, but leave its mask entry unconstrained.
            if item_name == "*":
                dim_ids.append(dim.id)
                item_ids.append("*")
                continue

            # Sequential keywords (THIS, PREV, NEXT, FIRST, LAST) in rule targets
            # Only THIS is allowed on LHS (treated as wildcard).
            # FIRST, LAST, PREV, NEXT are NOT allowed on LHS - throw error.
            item_upper = item_name.upper()
            if item_upper in {"FIRST", "LAST", "PREV", "NEXT"}:
                raise RuleValidationError(
                    f"Sequential keyword '{item_name}' is not allowed on the left-hand side of a rule. "
                    f"Only 'THIS' can be used on the LHS."
                )
            if item_upper == "THIS":
                dim_ids.append(dim.id)
                item_ids.append("*")
                continue

            item = next((it for it in dim.items if it.name.lower() == item_name.lower()), None)
            if item is None:
                raise RuleValidationError(f"Unknown item {item_name!r} in dimension {dim.name!r}")

            if mask[slot] is not None and mask[slot] != item.id:
                raise RuleValidationError(f"Duplicate or conflicting dimension in rule target: {dim.name!r}")
            mask[slot] = item.id
            dim_ids.append(dim.id)
            item_ids.append(item.id)

        # For anchored rules: fill unspecified dimensions with default items
        if use_defaults_for_unspecified:
            for i, dim_id in enumerate(cube.dimension_ids):
                if mask[i] is None:
                    default_item = self._get_default_item(dim_id)
                    mask[i] = default_item.id if default_item else None

        addr_mask = tuple(mask)
        if dim_ids:
            primary_dim_id = dim_ids[0]
            primary_item_id = item_ids[0]
        else:
            # All targets were of the form Dim.* with no fixed items. Treat
            # this as a whole-cube wildcard grouped under the first cube
            # dimension.
            primary_dim_id = cube.dimension_ids[0] if cube.dimension_ids else ""
            primary_item_id = "*"

        return addr_mask, primary_dim_id, primary_item_id

    def set_rule(
        self,
        cube_id: str,
        targets: list[tuple[str, str]],
        expression: str,
        is_anchored: bool = False,
        max_cells: int | None = None,
    ) -> None:
        """Create or update a (possibly multi-dimension) rule.

        ``targets`` is a list of (dim_name, item_name) pairs describing the
        constrained dimensions on the rule's left-hand side. The first pair is
        treated as the primary dimension/item for grouping/back-compat.

        Args:
            is_anchored: If True, the rule is "anchored" to exactly one cell.
                Unspecified dimensions use their default items instead of wildcards.
                This provides cell-rule-like behavior within the rule system.
        """

        cube = self.require_cube_by_id(cube_id)
        expression = self._normalize_expression(expression)

        # Validate expression for bidirectional recurrence before any evaluation.
        self._validate_no_bidirectional_recurrence(expression)
        addr_mask, primary_dim_id, primary_item_id = self._resolve_rule_targets(
            cube, targets, use_defaults_for_unspecified=is_anchored
        )
        if self._is_whole_cube_rule_mask(cube, addr_mask, tuple(targets)):
            self._validate_cross_cube_wildcard_mapping_dims(cube, expression)

        self._validate_rule_entry(
            cube, primary_dim_id, primary_item_id, expression, addr_mask=addr_mask, max_cells=max_cells
        )
        rule = self._ws.upsert_rule(
            cube.id, primary_dim_id, primary_item_id, expression,
            addr_mask=addr_mask, targets=tuple(targets), is_anchored=is_anchored
        )
        self._cell_cache.clear()
        
        # For anchored rules, invalidate the specific cell to trigger format updates
        if is_anchored and rule.addr_mask is not None:
            # Build full address from mask (replace None with default items)
            full_addr: tuple[str, ...] = tuple(
                item_id if item_id is not None else (
                    self._get_default_item(dim_id).id if self._get_default_item(dim_id) else ""
                )
                for dim_id, item_id in zip(cube.dimension_ids, rule.addr_mask)
            )
            # CRITICAL: Clear hardnumber override BEFORE so rule takes precedence
            cube.user_override_addrs.discard(full_addr)
            # CRITICAL: Clear the cell value so the rule computes fresh
            cube.set(full_addr, None)
            self._on_cell_value_changed(cube.id, full_addr)
            # Compute the rule value immediately so it's available without requiring cell access first
            self._get_cell_by_addr(cube, full_addr)
            # Track volatile cells just like the deprecated set_cell_rule_by_addr path
            self._track_volatile_cell(cube.id, full_addr, expression)

    def apply_rule_batch(
        self,
        rules: list[dict],
    ) -> tuple[bool, str | None]:
        """Apply multiple set-rule operations atomically.

        Used by script/macro paths to avoid one transport round-trip per rule.
        Validation is sampled (max_cells=1) to keep script execution fast.
        On failure, applied rules are rolled back so the script does not leave
        a partial state.
        """
        parsed_rules: list[tuple[Cube, list[tuple[str, str]], str, bool, tuple[str | None, ...], str, str]] = []
        for rule in rules:
            cube_id = rule["cube_id"]
            targets = rule["targets"]
            expression = rule["expression"]
            is_anchored = rule.get("is_anchored", False)
            cube = self.require_cube_by_id(cube_id)
            expression = self._normalize_expression(expression)
            addr_mask, primary_dim_id, primary_item_id = self._resolve_rule_targets(
                cube, targets, use_defaults_for_unspecified=is_anchored
            )
            if self._is_whole_cube_rule_mask(cube, addr_mask, tuple(targets)):
                self._validate_cross_cube_wildcard_mapping_dims(cube, expression)
            parsed_rules.append(
                (cube, targets, expression, is_anchored, addr_mask, primary_dim_id, primary_item_id)
            )

        applied_rules: list[tuple[Cube, Any]] = []
        try:
            for cube, targets, expression, is_anchored, addr_mask, primary_dim_id, primary_item_id in parsed_rules:
                self._validate_rule_entry(
                    cube, primary_dim_id, primary_item_id, expression, addr_mask=addr_mask, max_cells=1
                )
                rule = self._ws.upsert_rule(
                    cube.id, primary_dim_id, primary_item_id, expression,
                    addr_mask=addr_mask, targets=tuple(targets), is_anchored=is_anchored
                )
                applied_rules.append((cube, rule))
                if is_anchored and rule.addr_mask is not None:
                    full_addr: tuple[str, ...] = tuple(
                        item_id if item_id is not None else (
                            self._get_default_item(dim_id).id if self._get_default_item(dim_id) else ""
                        )
                        for dim_id, item_id in zip(cube.dimension_ids, rule.addr_mask)
                    )
                    cube.user_override_addrs.discard(full_addr)
                    cube.set(full_addr, None)
                    self._on_cell_value_changed(cube.id, full_addr)
                    self._get_cell_by_addr(cube, full_addr)
        except Exception as e:
            for cube, rule in applied_rules:
                self._ws.delete_rule(rule.id)
            self._cell_cache.clear()
            return False, str(e)

        self._cell_cache.clear()
        return True, None

    def set_rule_order(self, rule_ids: list[str]) -> None:
        self._ws.set_rule_order(rule_ids)
        self._cell_cache.clear()
        # Invalidate cubes with rules so cells with overlapping rules
        # are recomputed using the new precedence order.
        for cube in self._ws.cubes.values():
            has_rule = any(r.cube_id == cube.id for r in self._ws.rules.values())
            if has_rule:
                self._invalidate_cube(cube.id)

    def can_undo(self) -> bool:
        return self._undo.can_undo()

    def can_redo(self) -> bool:
        return self._undo.can_redo()

    def undo(self) -> str | None:
        """Undo last action and return its description."""
        action = self._undo.undo()
        if action:
            self._mark_dirty()
            return getattr(action, 'description', 'Unknown action')
        return None

    def redo(self) -> str | None:
        """Redo last undone action and return its description."""
        action = self._undo.redo()
        if action:
            self._mark_dirty()
            return getattr(action, 'description', 'Unknown action')
        return None

    def _execute_command(self, command: Action) -> None:
        """Execute a command and add it to undo history."""
        self._undo.push_and_do(command)
        self._mark_dirty()

    def get_undo_description(self) -> str | None:
        """Get description of next undoable action for UI."""
        return self._undo.get_undo_description()

    def get_redo_description(self) -> str | None:
        """Get description of next redoable action for UI."""
        return self._undo.get_redo_description()

    def _mark_dirty_hook(self) -> None:
        """Mark workspace as dirty (called after any undoable operation).

        This is a private hook that the GUI can override on the core to track
        unsaved changes.
        """
        pass

    def _mark_dirty(self) -> None:
        """Internal: Mark workspace as dirty (called after any undoable operation)."""
        self._mark_dirty_hook()
        # Phase E: Publish domain event for GUI adapter
        self._publish_event(EVENT_WORKSPACE_DIRTY_CHANGED, {
            "is_dirty": True,
            "source": "undoable_operation",
        })

@dataclass(frozen=True)
class _CellEditAction(Action):
    engine: _EngineCore
    cube: Cube
    addr: tuple[str, ...]
    before: Any
    after: Any
    description: str = "Change cell value"

    def do(self) -> None:
        self.cube.set(self.addr, self.after)
        if self.after is not None:
            self.cube.user_override_addrs.add(self.addr)
        else:
            self.cube.user_override_addrs.discard(self.addr)
        self.engine._cell_cache.clear()
        self.engine._on_cell_value_changed(self.cube.id, self.addr)

    def undo(self) -> None:
        self.cube.set(self.addr, self.before)
        if self.before is not None:
            self.cube.user_override_addrs.add(self.addr)
        else:
            self.cube.user_override_addrs.discard(self.addr)
        self.engine._cell_cache.clear()
        self.engine._on_cell_value_changed(self.cube.id, self.addr)


@dataclass(frozen=True)
class _BatchCellEditAction(Action):
    """Set many cell hardvalues with a single cache clear and dep-graph pass."""
    engine: _EngineCore
    cube: Cube
    changes: list[tuple[tuple[str, ...], Any, Any]]  # (addr, before, after)
    description: str = "Batch cell value change"

    def do(self) -> None:
        for addr, _before, after in self.changes:
            self.cube.set(addr, after)
            if after is not None:
                self.cube.user_override_addrs.add(addr)
            else:
                self.cube.user_override_addrs.discard(addr)
        self.engine._cell_cache.clear()
        for addr, _before, _after in self.changes:
            self.engine._invalidate_cell_node(self.cube.id, addr)
        self.engine._publish_event(EVENT_CELLS_UPDATED, {
            "cube_id": self.cube.id,
            "count": len(self.changes),
            "changed_fields": ["value"],
        })

    def undo(self) -> None:
        for addr, before, _after in self.changes:
            self.cube.set(addr, before)
            if before is not None:
                self.cube.user_override_addrs.add(addr)
            else:
                self.cube.user_override_addrs.discard(addr)
        self.engine._cell_cache.clear()
        for addr, _before, _after in self.changes:
            self.engine._invalidate_cell_node(self.cube.id, addr)
        self.engine._publish_event(EVENT_CELLS_UPDATED, {
            "cube_id": self.cube.id,
            "count": len(self.changes),
            "changed_fields": ["value"],
        })


def _coerce_num(value: Any) -> float:
    """Best-effort numeric coercion for arithmetic contexts.

    Behaviour:
    - ``None`` → ``0.0``
    - CellError objects are passed through unchanged so they can propagate.
    - Error strings (all valid CellError codes) are passed
      through unchanged so they can propagate.
    - Other *text* values are left as-is so string literals like
      ``"test"`` survive round-trips through the resolver and can be
      compared by the rule engine (e.g. ``IF(ref="test", TRUE, FALSE)``).
    - Non-string values fall back to ``float(..)`` with ``0.0`` on
      failure.
    """

    if value is None:
        return 0.0

    # Preserve CellError objects so they propagate through rules
    if isinstance(value, CellError):
        return value  # type: ignore[return-value]

    if isinstance(value, str):
        # Preserve error sentinels exactly using CellError's valid codes.
        if value in CellError._VALID_CODES:
            return value  # type: ignore[return-value]
        # Treat numeric-looking strings as numbers, but keep arbitrary
        # text (e.g. "test") as a real string so equality comparisons
        # work as expected.
        try:
            return float(value)
        except (TypeError, ValueError):
            return value  # type: ignore[return-value]

    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def iter_view_ids(ws: Workspace) -> Iterable[str]:
    return ws.views.keys()


def iter_view_ids(ws: Workspace) -> Iterable[str]:
    return ws.views.keys()
