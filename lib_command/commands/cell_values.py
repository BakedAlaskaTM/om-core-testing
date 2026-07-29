"""
Cell value command handlers for Phase 1A — Single Cell Value Commands.

These commands provide the canonical command-spine path for GUI cell value
mutations, replacing direct Engine calls with session.execute(...) routing.

Handlers receive (ctx, **kwargs) and return a dict result.
The CommandExecutor validates required parameters before calling the handler.
"""
from __future__ import annotations

from typing import Any

from lib_command.commands.rule import cmd_delete_rule
from lib_command.core.domain_event_publisher import publish_domain_event
from lib_command.core.message_bus import get_message_bus
from lib_utils.coerce import coerce_user_value


def cmd_set_cell_hardvalue_by_keys(
    ctx: Any,
    view_id: str,
    row_key: tuple[str, ...],
    col_key: tuple[str, ...],
    value: Any,
) -> dict:
    """Deprecated wrapper — use ``cmd_set_cell_hardvalue`` with ``cell_ref``."""
    return cmd_set_cell_hardvalue(
        ctx, view_id,
        {"kind": "ids", "row_key": row_key, "col_key": col_key},
        value,
    )


def cmd_set_cell_by_keys(
    ctx: Any,
    view_id: str,
    row_key: tuple[str, ...],
    col_key: tuple[str, ...],
    value: Any,
) -> dict:
    """Deprecated alias for ``cmd_set_cell_hardvalue_by_keys``."""
    return cmd_set_cell_hardvalue_by_keys(ctx, view_id, row_key, col_key, value)


def cmd_set_cell(
    ctx: Any,
    view_id: str,
    row: int,
    col: int,
    value: Any,
) -> dict:
    """Set a cell value by row/column indices.

    Macro-language compatibility command.  Converts row/column indices into a
    ``cell_ref`` and delegates to the canonical ``engine.set_cell_hardvalue``.
    """
    if not view_id:
        raise ValueError("view_id is required")
    if row < 0:
        raise ValueError("row must be non-negative")
    if col < 0:
        raise ValueError("col must be non-negative")

    coerced = coerce_user_value(value)
    cell_ref = {"kind": "idx", "row_idx": row, "col_idx": col}
    ctx.engine.set_cell_hardvalue(view_id, cell_ref, coerced)
    return {"affected": 1, "property": "value", "view_id": view_id}


def cmd_clear_cell_hardvalue_by_keys(
    ctx: Any,
    view_id: str,
    row_key: tuple[str, ...],
    col_key: tuple[str, ...],
) -> dict:
    """Deprecated wrapper — use ``cmd_clear_cell_hardvalue`` with ``cell_ref``."""
    return cmd_clear_cell_hardvalue(
        ctx, view_id,
        {"kind": "ids", "row_key": row_key, "col_key": col_key},
    )


def cmd_clear_cell_by_keys(
    ctx: Any,
    view_id: str,
    row_key: tuple[str, ...],
    col_key: tuple[str, ...],
) -> dict:
    """Deprecated alias for ``cmd_clear_cell_hardvalue_by_keys``."""
    return cmd_clear_cell_hardvalue_by_keys(ctx, view_id, row_key, col_key)


def cmd_clear_cell(
    ctx: Any,
    view_id: str,
    row: int,
    col: int,
) -> dict:
    """Clear a cell value by row/column indices.

    Macro-language compatibility command.  Removes the direct stored value via
    the canonical ``engine.clear_cell_hardvalue``.  Does NOT delete anchored
    rules.  If a rule covers the address, recalculation may reveal a
    rule-derived value.
    """
    if not view_id:
        raise ValueError("view_id is required")
    if row < 0:
        raise ValueError("row must be non-negative")
    if col < 0:
        raise ValueError("col must be non-negative")

    cell_ref = {"kind": "idx", "row_idx": row, "col_idx": col}
    ctx.engine.clear_cell_hardvalue(view_id, cell_ref)
    return {"affected": 1, "property": "cleared", "view_id": view_id}


def _resolve_cell_ref(cell_ref: dict) -> dict:
    """Normalize a cell_ref dict for canonical engine methods.

    Returns a *cell_ref* dict guaranteed to have the ``kind`` key and
    the correct keys for that kind (``ids``, ``name``, or ``idx``).

    Accepts legacy ``"index"`` (maps to ``"idx"``) and ``"keys"``
    (maps to ``"ids"``) for backward compatibility.
    """
    kind = cell_ref.get("kind", "ids")
    value = cell_ref.get("value")

    # Legacy migration
    if kind == "index":
        return {"kind": "idx", "row_idx": value["row"], "col_idx": value["col"]}
    if kind == "keys":
        return {"kind": "ids", "row_key": tuple(value["row_key"]), "col_key": tuple(value["col_key"])}

    # Ensure tuples for ids kind
    if kind == "ids":
        cell_ref = dict(cell_ref)
        cell_ref["row_key"] = tuple(cell_ref.get("row_key", ()))
        cell_ref["col_key"] = tuple(cell_ref.get("col_key", ()))
        return cell_ref

    return cell_ref


def cmd_set_cell_hardvalue(
    ctx: Any,
    view_id: str,
    cell_ref: dict,
    value: Any,
) -> dict:
    """Set a cell hardvalue using a ``cell_ref`` for addressing.

    Canonical command.  Delegates to ``engine.set_cell_hardvalue``.
    """
    if not view_id:
        raise ValueError("view_id is required")

    resolved = _resolve_cell_ref(cell_ref)
    coerced = coerce_user_value(value)
    ctx.engine.set_cell_hardvalue(view_id, resolved, coerced)
    return {"affected": 1, "property": "value", "view_id": view_id}


def cmd_set_cell_value(
    ctx: Any,
    view_id: str,
    cell_ref: dict,
    value: Any,
) -> dict:
    """Deprecated alias for ``cmd_set_cell_hardvalue``."""
    return cmd_set_cell_hardvalue(ctx, view_id, cell_ref, value)


def cmd_clear_cell_hardvalue(
    ctx: Any,
    view_id: str,
    cell_ref: dict,
) -> dict:
    """Clear a cell hardvalue using a ``cell_ref`` for addressing.

    Canonical command.  Delegates to ``engine.clear_cell_hardvalue``.
    """
    if not view_id:
        raise ValueError("view_id is required")

    resolved = _resolve_cell_ref(cell_ref)
    ctx.engine.clear_cell_hardvalue(view_id, resolved)
    return {"affected": 1, "property": "cleared", "view_id": view_id}


def cmd_clear_cell_value(
    ctx: Any,
    view_id: str,
    cell_ref: dict,
) -> dict:
    """Deprecated alias for ``cmd_clear_cell_hardvalue``."""
    return cmd_clear_cell_hardvalue(ctx, view_id, cell_ref)


def cmd_set_range_values(
    ctx: Any,
    view_id: str,
    top: int,
    left: int,
    values: list[list[Any]],
) -> dict:
    """Set a rectangular range of cell values.

    Maps to engine.set_range(view_id, top, left, values).
    """
    if not view_id:
        raise ValueError("view_id is required")
    if not values:
        raise ValueError("values is required")
    if top < 0:
        raise ValueError("top must be non-negative")
    if left < 0:
        raise ValueError("left must be non-negative")

    ctx.engine.set_range(view_id, top, left, values)
    total = sum(len(row) for row in values)
    return {"affected": total, "property": "range", "view_id": view_id}


def cmd_update_cell_rule(
    ctx: Any,
    rule_id: str,
    expression: str,
) -> dict:
    """Update a cell rule expression by ID.

    Maps to engine.update_cell_rule(rule_id, expression).
    """
    if not rule_id:
        raise ValueError("rule_id is required")
    if not expression:
        raise ValueError("expression is required")

    ctx.engine.update_cell_rule(rule_id, expression)
    return {"affected": 1, "property": "cell_rule", "rule_id": rule_id}


# Delegate delete_cell_rule to the canonical cmd_delete_rule handler.
# Both delete a rule by ID; delete_cell_rule is kept as a compatibility
# command ID for scripts/macros that may still use it.
cmd_delete_cell_rule = cmd_delete_rule


def cmd_set_page_item_id(
    ctx: Any,
    view_id: str,
    dim_id: str,
    item_id: str,
) -> dict:
    """Set the active page item for a dimension in a view.

    Mutates the workspace view's page_selections through TableViewSpec.
    """
    if not view_id:
        raise ValueError("view_id is required")
    if not dim_id:
        raise ValueError("dim_id is required")
    if not item_id:
        raise ValueError("item_id is required")

    view = ctx.engine.workspace.views[view_id]
    view.set_page_item_id(dim_id, item_id)
    return {"affected": 1, "property": "page_selection", "view_id": view_id, "dim_id": dim_id}


def cmd_delete_rule_anchored(
    ctx: Any,
    view_id: str,
    cell_ref: dict,
) -> dict:
    """Delete the rule anchored at a specific cell.

    Canonical command handler for removing a rule from a single cell.
    The cell is identified by ``cell_ref`` (``ids``, ``name``, or ``idx``).

    Args:
        ctx: Execution context (provides ``engine``).
        view_id: Stable view identifier.
        cell_ref: Address dict with ``kind`` and axis keys.

    Returns:
        Dict with ``affected`` (``1`` if a rule was removed, else ``0``),
        ``property: "cell_rule_deleted"``, and ``view_id``.
    """
    if not view_id:
        raise ValueError("view_id is required")

    resolved = _resolve_cell_ref(cell_ref)
    removed = ctx.engine.delete_rule_anchored(view_id, resolved)
    return {"affected": 1 if removed else 0, "property": "cell_rule_deleted", "view_id": view_id}


def cmd_delete_cell_rule_by_keys(
    ctx: Any,
    view_id: str,
    row_key: tuple[str, ...],
    col_key: tuple[str, ...],
) -> dict:
    """Deprecated wrapper — use ``cmd_delete_rule_anchored`` with ``cell_ref``."""
    return cmd_delete_rule_anchored(
        ctx, view_id,
        {"kind": "ids", "row_key": row_key, "col_key": col_key},
    )


def cmd_set_rule_anchored(
    ctx: Any,
    view_id: str,
    cell_ref: dict,
    expression: str,
) -> dict:
    """Attach an anchored rule to a specific cell.

    Canonical command.  Delegates to ``engine.set_rule_anchored``.
    """
    if not view_id:
        raise ValueError("view_id is required")
    if expression is None:
        raise ValueError("expression is required")

    resolved = _resolve_cell_ref(cell_ref)
    ctx.engine.set_rule_anchored(view_id, resolved, expression)
    return {"affected": 1, "property": "cell_rule", "view_id": view_id}


def cmd_set_cell_rule_by_keys(
    ctx: Any,
    view_id: str,
    row_key: tuple[str, ...],
    col_key: tuple[str, ...],
    expression: str,
) -> dict:
    """Deprecated wrapper — use ``cmd_set_rule_anchored`` with ``cell_ref``."""
    return cmd_set_rule_anchored(
        ctx, view_id,
        {"kind": "ids", "row_key": row_key, "col_key": col_key},
        expression,
    )


def cmd_attach_dimension_to_cube(
    ctx: Any,
    cube_id: str,
    dim_id: str,
    default_item_id: str | None = None,
) -> dict:
    """Attach a dimension to a cube.

    Maps to engine.attach_dimension_to_cube(cube_id, dim_id, default_item_id).
    Also migrates existing cube data to the new dimensionality.
    """
    if not cube_id:
        raise ValueError("cube_id is required")
    if not dim_id:
        raise ValueError("dim_id is required")

    ctx.engine.attach_dimension_to_cube(cube_id, dim_id, default_item_id=default_item_id)

    # Migrate data and user_override_addrs to new dimensionality
    ws = ctx.engine.workspace if hasattr(ctx.engine, "workspace") else None
    if ws is not None:
        cube = ws.cubes.get(cube_id)
        if cube is not None and hasattr(cube, "migrate_data_for_new_dimensions"):
            cube.migrate_data_for_new_dimensions(ws)

    return {"affected": 1, "property": "cube_dimension", "cube_id": cube_id, "dim_id": dim_id}


def cmd_set_view_axes(
    ctx: Any,
    view_id: str,
    row_dimension_id: str,
    col_dimension_id: str,
) -> dict:
    """Set the row and column dimension IDs for a view.

    Maps to engine.set_view_axes(view_id, row_dimension_id, col_dimension_id).
    """
    if not view_id:
        raise ValueError("view_id is required")
    if not row_dimension_id:
        raise ValueError("row_dimension_id is required")
    if not col_dimension_id:
        raise ValueError("col_dimension_id is required")

    ctx.engine.set_view_axes(view_id, row_dimension_id, col_dimension_id)
    return {"affected": 1, "property": "view_axes", "view_id": view_id}


def cmd_delete_cube(
    ctx: Any,
    cube_id: str,
) -> dict:
    """Delete a cube and all views that reference it.

    Maps to engine.delete_cube(cube_id).
    """
    if not cube_id:
        raise ValueError("cube_id is required")

    removed = ctx.engine.delete_cube(cube_id)
    if removed:
        bus = get_message_bus()
        publish_domain_event(
            bus,
            "event.cube.deleted",
            {"cube_id": cube_id},
        )
    return {"affected": 1 if removed else 0, "property": "cube_deleted", "cube_id": cube_id}


def cmd_delete_dimension(
    ctx: Any,
    dim_id: str,
) -> dict:
    """Delete a dimension, detaching it from all cubes and views first.

    Maps to engine.delete_dimension(dim_id).
    """
    if not dim_id:
        raise ValueError("dim_id is required")

    removed = ctx.engine.delete_dimension(dim_id)
    # Engine already publishes event.dimension.deleted with affected_view_ids.
    # Do NOT republish here — the duplicate causes double processing in binder.
    return {"affected": 1 if removed else 0, "property": "dimension_deleted", "dim_id": dim_id}


def cmd_set_cell_hardvalue_by_addr(
    ctx: Any,
    cube_id: str,
    addr: list,
    value: Any,
) -> dict:
    """Set a cell hardvalue using a full cube address tuple.

    Canonical command for address-based hardvalue assignment.
    Delegates to ``engine.set_cell_hardvalue_by_addr``.
    """
    if not cube_id:
        raise ValueError("cube_id is required")
    if addr is None:
        raise ValueError("addr is required")

    coerced = coerce_user_value(value)
    ctx.engine.set_cell_hardvalue_by_addr(cube_id, tuple(addr), coerced)
    return {"affected": 1, "property": "value", "cube_id": cube_id}


def cmd_set_cell_hardvalues_batch_by_addr(
    ctx: Any,
    cube_id: str,
    entries: list,
) -> dict:
    """Set many cell hardvalues in one call with a single undo group.

    Each entry is ``{"addr": [...], "value": ...}``.
    Delegates to ``engine.batch_set_cell_hardvalues_by_addr``.
    """
    if not cube_id:
        raise ValueError("cube_id is required")
    if not entries:
        raise ValueError("entries is required")

    parsed = [(tuple(e["addr"]), e["value"]) for e in entries]
    count = ctx.engine.batch_set_cell_hardvalues_by_addr(cube_id, parsed)
    return {"affected": count, "property": "value", "cube_id": cube_id}
