"""GUI View Model — small GUI-side state holder for UI state.

B.1: Minimal placeholder only. No state caching.
B.5: Expanded with selection, view_id, dirty state getters and update methods.
C.2: Expanded with view/cube DTO cache and replace_workspace_snapshot().

Not yet (defer to later phases):
- cell values
- cube contents (raw data, only metadata/cached snapshots)
- dimension contents
- rule body results
- table/grid backing data
"""

from __future__ import annotations

import copy


class GUIViewModel:
    """Small GUI-side state holder for UI state that should not live in the engine.

    Stores ONLY DTOs (plain dicts). Never receives or stores engine domain objects.

    B.5 scope (lightweight GUI session state only):
    - selection
    - current_view_id
    - dirty flag

    C.2 scope (cached DTO snapshots):
    - current_cube_id
    - view_snapshots: dict[str, dict] — view_id → DTO snapshot
    - cube_snapshots: dict[str, dict] — cube_id → DTO snapshot
    """

    def __init__(self) -> None:
        # B.5: GUI session state
        self._selection: list = []
        self._current_view_id: str | None = None
        self._is_dirty: bool = False

        # C.2: Cached DTO snapshots (never engine references)
        self._current_cube_id: str | None = None
        self._view_data: dict[str, dict] = {}  # view_id → DTO snapshot
        self._cube_data: dict[str, dict] = {}  # cube_id → DTO snapshot
        self._dim_data: dict[str, dict] = {}    # dim_id → DTO snapshot

    # =========================================================================
    # Selection (B.5)
    # =========================================================================

    def get_selection(self) -> list:
        """Return a copy of the current selection."""
        return list(self._selection)

    def update_selection(self, selection: list) -> None:
        """Update the cached selection."""
        self._selection = list(selection or [])

    def clear_selection(self) -> None:
        """Clear the current selection."""
        self._selection = []

    # =========================================================================
    # Current view (B.5 + C.2)
    # =========================================================================

    def get_current_view_id(self) -> str | None:
        """Return the current view ID."""
        return self._current_view_id

    def set_current_view_id(self, view_id: str | None) -> None:
        """Set the current view ID."""
        self._current_view_id = view_id

    # =========================================================================
    # Current cube (C.2)
    # =========================================================================

    def get_current_cube_id(self) -> str | None:
        """Return the current cube ID."""
        return self._current_cube_id

    def set_current_cube_id(self, cube_id: str | None) -> None:
        """Set the current cube ID."""
        self._current_cube_id = cube_id

    # =========================================================================
    # View snapshots (C.2)
    # =========================================================================

    def get_view_snapshot(self, view_id: str) -> dict | None:
        """Return a single view snapshot as deep copy, or None if not cached."""
        snap = self._view_data.get(view_id)
        return copy.deepcopy(snap) if snap is not None else None

    def get_all_view_snapshots(self) -> dict:
        """Return all view snapshots as deep copy (never reference to internal data)."""
        return copy.deepcopy(self._view_data)

    def update_view_snapshot(self, view_id: str, dto: dict) -> None:
        """Store single view snapshot as deep copy (for event-based updates)."""
        self._view_data[view_id] = copy.deepcopy(dto)

    def remove_view_snapshot(self, view_id: str) -> None:
        """Remove a view snapshot."""
        self._view_data.pop(view_id, None)

    # =========================================================================
    # Cube snapshots (C.2)
    # =========================================================================

    def get_cube_snapshot(self, cube_id: str) -> dict | None:
        """Return a single cube snapshot as deep copy, or None if not cached."""
        snap = self._cube_data.get(cube_id)
        return copy.deepcopy(snap) if snap is not None else None

    def get_all_cube_snapshots(self) -> dict:
        """Return all cube snapshots as deep copy (never reference to internal data)."""
        return copy.deepcopy(self._cube_data)

    def update_cube_snapshot(self, cube_id: str, dto: dict) -> None:
        """Store single cube snapshot as deep copy (for event-based updates)."""
        self._cube_data[cube_id] = copy.deepcopy(dto)

    def remove_cube_snapshot(self, cube_id: str) -> None:
        """Remove a cube snapshot."""
        self._cube_data.pop(cube_id, None)

    # =========================================================================
    # Dimension snapshots (E2)
    # =========================================================================

    def get_dimension_snapshot(self, dim_id: str) -> dict | None:
        """Return a single dimension snapshot as deep copy, or None if not cached."""
        snap = self._dim_data.get(dim_id)
        return copy.deepcopy(snap) if snap is not None else None

    def get_all_dimension_snapshots(self) -> dict:
        """Return all dimension snapshots as deep copy."""
        return copy.deepcopy(self._dim_data)

    def update_dimension_snapshot(self, dim_id: str, dto: dict) -> None:
        """Store single dimension snapshot as deep copy."""
        self._dim_data[dim_id] = copy.deepcopy(dto)

    def remove_dimension_snapshot(self, dim_id: str) -> None:
        """Remove a dimension snapshot."""
        self._dim_data.pop(dim_id, None)

    def replace_workspace_snapshot(self, snapshot: dict) -> None:
        """Atomically replace all view/cube/current data from a WorkspaceSnapshotDTO.

        Used for bootstrap, load, reset, undo, redo, and delete where
        incremental patching risks desync.

        NOTE: In a single-threaded GUI app (Python + PySide), these assignments
        are atomic enough. If bus events can arrive from another thread, marshal
        all binder updates onto the GUI/main thread using QMetaObject.callLater
        or similar mechanism.
        """
        self._current_view_id = snapshot.get("saved_default_view_id")
        # Derive current cube from active view (only views are physically visible)
        view_snapshots = snapshot.get("view_snapshots", {})
        active_view = view_snapshots.get(self._current_view_id) if self._current_view_id else None
        self._current_cube_id = active_view.get("cube_id") if active_view else None
        self._view_data = copy.deepcopy(view_snapshots)
        self._cube_data = copy.deepcopy(snapshot.get("cube_snapshots", {}))
        self._dim_data = copy.deepcopy(snapshot.get("dimension_snapshots", {}))

    # =========================================================================
    # Dirty state (B.5)
    # =========================================================================

    def is_dirty(self) -> bool:
        """Return whether the workspace has unsaved changes."""
        return self._is_dirty

    def set_dirty(self, is_dirty: bool) -> None:
        """Set the dirty flag."""
        self._is_dirty = bool(is_dirty)

    # =========================================================================
    # Dirty event handler (B.5)
    # =========================================================================

    def on_dirty_changed(self, event) -> None:
        """Handle workspace.dirty.changed events from the bus.

        Args:
            event: An event object with a `payload` dict containing `is_dirty`
        """
        is_dirty = event.payload.get("is_dirty", False) if hasattr(event, "payload") else False
        self.set_dirty(is_dirty)