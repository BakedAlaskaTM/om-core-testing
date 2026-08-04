"""
View commands for Phase 2 — View Dimension Movement.

These commands provide the canonical command-spine path for GUI view
configuration mutations, replacing direct Engine calls with
session.execute(...) routing.
"""
from __future__ import annotations

from typing import Any

from lib_openm.model import ViewLayout


def validate_view_layout_for_cube(cube, layout: ViewLayout) -> None:
    """Validate that a ViewLayout is consistent with a cube's dimensions.

    Raises ValueError with a descriptive message on any violation.
    """
    cube_dim_ids = set(cube.dimension_ids)
    seen: set[str] = set()
    for axis_name in ("rows", "cols", "page"):
        axis_dims = getattr(layout, axis_name, [])
        for dim_id in axis_dims:
            if dim_id not in cube_dim_ids:
                raise ValueError(
                    f"Dimension '{dim_id}' on axis '{axis_name}' does not belong to cube '{cube.name}'"
                )
            if dim_id in seen:
                raise ValueError(
                    f"Dimension '{dim_id}' appears on multiple axes"
                )
            seen.add(dim_id)


def cmd_set_view_layout(
    ctx: Any,
    view_id: str,
    layout: dict,
) -> dict:
    """Set the layout of an existing view.

    Payload ``layout`` is a dict with keys ``rows``, ``cols``, and ``page``,
    each mapping to a list of dimension IDs.
    """
    if not view_id:
        raise ValueError("view_id is required")
    if not isinstance(layout, dict):
        raise ValueError("layout must be a dict")

    view = ctx.engine.require_view_by_id(view_id)
    cube = ctx.engine.require_cube_by_id(view.cube_id)

    vl = ViewLayout(
        rows=list(layout.get("rows", [])),
        cols=list(layout.get("cols", [])),
        page=list(layout.get("page", [])),
    )
    used = set(vl.rows) | set(vl.cols) | set(vl.page)
    for did in cube.dimension_ids:
        if did not in used:
            vl.page.append(did)
            used.add(did)
    validate_view_layout_for_cube(cube, vl)
    ctx.engine.set_view_layout(view_id, vl)
    return {"affected": 1, "property": "view_layout", "view_id": view_id}


def cmd_move_view_dimension(
    ctx: Any,
    view_id: str,
    dim_id: str,
    dest: str,
    index: int | None = None,
) -> dict:
    """Move a dimension to a different axis (row, col, page) within a view.

    Maps to engine.move_view_dimension(view_id, dim_id, dest, index).

    Args:
        view_id: Target view ID
        dim_id: Dimension ID to move
        dest: Destination axis — "row", "col", or "page"
        index: Optional insertion index within the destination axis list
    """
    if not view_id:
        raise ValueError("view_id is required")
    if not dim_id:
        raise ValueError("dim_id is required")
    if not dest:
        raise ValueError("dest is required")
    if dest not in ("row", "col", "page"):
        raise ValueError(f"dest must be 'row', 'col', or 'page', got: {dest}")
    if index is not None and index < 0:
        raise ValueError("index must be non-negative")

    ctx.engine.move_view_dimension(view_id, dim_id, dest=dest, index=index)
    return {"affected": 1, "property": "view_dimension", "view_id": view_id, "dim_id": dim_id, "dest": dest}


def _persist_view_col_width(engine: Any, view_id: str, col_index: int, width: int) -> None:
    """Persist a column width on the engine (local or remote)."""
    if hasattr(engine, "set_view_col_width"):
        engine.set_view_col_width(view_id, col_index, width)
    else:
        view = engine.require_view_by_id(view_id)
        view.set_col_width(col_index, width)


def _persist_view_row_header_width(engine: Any, view_id: str, depth_or_index: int, width: int) -> None:
    """Persist a row-header width on the engine (local or remote)."""
    if hasattr(engine, "set_view_row_header_width"):
        engine.set_view_row_header_width(view_id, depth_or_index, width)
    else:
        view = engine.require_view_by_id(view_id)
        view.set_row_header_width(depth_or_index, width)


def cmd_set_view_col_width(
    ctx: Any,
    view_id: str,
    col_index: int,
    width: int,
) -> dict:
    """Set one persisted column width for a view."""
    if not view_id:
        raise ValueError("view_id is required")
    if col_index < 0:
        raise ValueError("col_index must be non-negative")
    if width < 0:
        raise ValueError("width must be non-negative")
    _persist_view_col_width(ctx.engine, view_id, col_index, width)
    return {
        "affected": 1,
        "property": "col_widths",
        "view_id": view_id,
        "col_index": col_index,
        "width": width,
    }


def cmd_set_view_row_header_width(
    ctx: Any,
    view_id: str,
    depth_or_index: int,
    width: int,
) -> dict:
    """Set one persisted row-header width for a view."""
    if not view_id:
        raise ValueError("view_id is required")
    if depth_or_index < 0:
        raise ValueError("depth_or_index must be non-negative")
    if width < 0:
        raise ValueError("width must be non-negative")
    _persist_view_row_header_width(ctx.engine, view_id, depth_or_index, width)
    return {
        "affected": 1,
        "property": "row_header_widths",
        "view_id": view_id,
        "depth_or_index": depth_or_index,
        "width": width,
    }
