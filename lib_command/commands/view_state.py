"""View-state commands — session-local active view, cursor, and selection.

These commands read/write SessionStore, not Engine canonical state.
See plan-20260605-0027--session-view-state-refactor.md.
"""

from __future__ import annotations

from typing import Any


def _get_session_store() -> Any:
    from lib_command.core.session_store import get_session_store
    return get_session_store()


def _get_message_bus() -> Any:
    from lib_command.core.message_bus import get_message_bus
    return get_message_bus()


def _publish_session_event(topic: str, payload: dict) -> None:
    """Publish a session/UI state event via the message bus."""
    bus = _get_message_bus()
    from lib_command.core.message_bus import MessageEnvelope
    import uuid, time
    envelope = MessageEnvelope(
        message_id=str(uuid.uuid4()),
        message_type="event",
        topic=topic,
        correlation_id=str(uuid.uuid4()),
        session_id=None,
        client_type=None,
        workspace_id=None,
        actor_id=None,
        timestamp=time.perf_counter(),
        payload=payload,
        context=None,
    )
    bus.publish(topic, envelope)


def cmd_set_active_view(ctx, view_id: str) -> dict:
    """Set the active view for the current session.

    Stores the active view in the session store and publishes an
    ``event.active_view.changed`` domain event.  Runtime active view is
    session state, not Engine or Workspace state.

    Args:
        ctx: Execution context (provides ``session_id`` and ``engine``).
        view_id: Stable view identifier to activate.

    Returns:
        Dict with ``view_id`` and ``success: True``.  If the view does
        not exist in the workspace, returns ``{"error": ..., "success": False}``.
    """
    session_id = getattr(ctx, "session_id", None)
    engine = getattr(ctx, "engine", None)

    # Validate view exists using the canonical Engine API.
    if engine is not None:
        try:
            engine.require_view_by_id(view_id)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"View '{view_id}' not found: {exc}", "success": False}

    if session_id:
        store = _get_session_store()
        store.set_active_view(session_id, view_id)
        _publish_session_event("event.active_view.changed", {
            "session_id": session_id,
            "view_id": view_id,
        })

    ctx.status(f"Active view set to {view_id}")
    return {"view_id": view_id, "success": True}


def cmd_set_selection(
    ctx,
    row: int,
    col: int,
    mode: str = "cell",
    anchor_row: int | None = None,
    anchor_col: int | None = None,
    selected_indices: list[tuple[int, int] | int] | None = None,
    view_id: str | None = None,
) -> dict:
    """Set grid selection to absolute coordinates for this session.

    Args:
        row: Target row index
        col: Target column index
        mode: Selection mode ("cell", "row", "col", "all")
        anchor_row: Anchor row for range selection (defaults to row)
        anchor_col: Anchor column for range selection (defaults to col)
        selected_indices: List of selected indices for multi-selection
        view_id: Optional view ID for bounds validation.  When provided,
            this is used instead of the session's active_view_id, ensuring
            bounds are checked against the correct view even when the
            session's active view is stale or not yet updated.
    """
    session_id = getattr(ctx, "session_id", None)
    engine = getattr(ctx, "engine", None)

    # Validate bounds via engine/view.  Prefer the explicit view_id
    # parameter (sent by the grid that owns the selection); fall back to
    # the session's active_view_id for backward compatibility.
    max_row, max_col = 0, 0
    view_id_for_bounds = view_id
    if not view_id_for_bounds and session_id:
        vs = _get_session_store().get_view_state(session_id)
        if vs is not None:
            view_id_for_bounds = vs.active_view_id
    if view_id_for_bounds and engine is not None:
        try:
            row_keys = engine.view_row_keys(view_id_for_bounds)
            col_keys = engine.view_col_keys(view_id_for_bounds)
            max_row = max(0, len(row_keys) - 1)
            max_col = max(0, len(col_keys) - 1)
        except Exception:
            pass

    row = max(0, min(row, max_row))
    col = max(0, min(col, max_col))
    a_row = max(0, min(anchor_row if anchor_row is not None else row, max_row))
    a_col = max(0, min(anchor_col if anchor_col is not None else col, max_col))

    # Write to SessionStore
    if session_id:
        store = _get_session_store()
        store.set_selection(
            session_id,
            cursor_row=row,
            cursor_col=col,
            anchor_row=a_row,
            anchor_col=a_col,
            selection_mode=mode,
            selected_indices=selected_indices,
        )
        _publish_session_event("event.selection.changed", {
            "session_id": session_id,
            "view_id": view_id_for_bounds,
            "cursor": (row, col),
            "anchor": (a_row, a_col),
            "mode": mode,
            "selected_indices": selected_indices,
        })

    # Phase 5E: command handlers no longer mutate GUI directly.
    # GUI refreshes from event.selection.changed or query.selection_current.
    ctx.status(f"Selected ({row}, {col})")
    return {"position": (row, col), "success": True}


def cmd_move_selection(ctx, direction: str, amount: int = 1, view_id: str | None = None) -> dict:
    """Move selection in a direction for this session.

    Args:
        direction: "up", "down", "left", "right", "first", "last"
        amount: How many steps
        view_id: Optional view ID for bounds validation.
    """
    session_id = getattr(ctx, "session_id", None)
    engine = getattr(ctx, "engine", None)

    # Read current position from SessionStore (source of truth)
    row, col = 0, 0
    if session_id:
        store = _get_session_store()
        vs = store.get_view_state(session_id)
        if vs is not None:
            row, col = vs.cursor_row, vs.cursor_col

    # Validate bounds via engine/view.  Prefer explicit view_id parameter.
    max_row, max_col = 0, 0
    view_id_for_bounds = view_id
    if not view_id_for_bounds and session_id:
        vs = _get_session_store().get_view_state(session_id)
        if vs is not None:
            view_id_for_bounds = vs.active_view_id
    if view_id_for_bounds and engine is not None:
        try:
            row_keys = engine.view_row_keys(view_id_for_bounds)
            col_keys = engine.view_col_keys(view_id_for_bounds)
            max_row = max(0, len(row_keys) - 1)
            max_col = max(0, len(col_keys) - 1)
        except Exception:
            pass

    if direction == "right":
        col = min(col + amount, max_col)
    elif direction == "left":
        col = max(col - amount, 0)
    elif direction == "down":
        row = min(row + amount, max_row)
    elif direction == "up":
        row = max(row - amount, 0)
    elif direction == "first":
        col = 0
    elif direction == "last":
        col = max_col

    # Write to SessionStore
    if session_id:
        store = _get_session_store()
        store.set_selection(
            session_id,
            cursor_row=row,
            cursor_col=col,
            anchor_row=row,
            anchor_col=col,
            selection_mode="cell",
        )
        _publish_session_event("event.selection.changed", {
            "session_id": session_id,
            "view_id": view_id_for_bounds,
            "cursor": (row, col),
            "anchor": (row, col),
            "mode": "cell",
            "direction": direction,
            "amount": amount,
        })

    # Phase 5E: command handlers no longer mutate GUI directly.
    ctx.status(f"Navigated {direction} to ({row}, {col})")
    return {"direction": direction, "amount": amount, "position": (row, col)}
