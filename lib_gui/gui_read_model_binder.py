"""GUI Read Model Binder — subscribes to workspace events and updates GUIViewModel.

Thread safety: All callbacks MUST run on the GUI/main thread.
PySide/PyQt widgets only work on the GUI thread, and the ViewModel may be read
by UI refresh code. If bus events arrive from engine worker threads, the bus
implementation should marshal callbacks to the GUI thread before invoking them.

Modes:
- Simple events with DTO → patch ViewModel directly (GUI thread)
- Simple events with ID only → binder calls query API → patch ViewModel (GUI thread)
- Complex events (load/reset/undo/redo/delete) → binder calls query.workspace_snapshot
  → replace ViewModel wholesale (GUI thread)

Usage:
    from lib_gui.gui_read_model_binder import GUIReadModelBinder
    binder = GUIReadModelBinder(session, gui_view_model, on_ui_refresh=window._do_ui_refresh)
    # Register in MainWindow.__init__:
    self.gui_read_model_binder = binder
"""

from __future__ import annotations

from typing import Any, Callable

import threading


def _payload_value(payload: Any, key: str, default: Any = None) -> Any:
    """Read a value from a dict or dataclass payload."""
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


class GUIReadModelBinder:
    """Subscribe to workspace events and update GUIViewModel.

    Constructor:
        session: The client session for subscribing to events and querying DTOs
        gui_view_model: The ViewModel to update with DTO snapshots
        on_ui_refresh: Optional callback invoked after ViewModel changes.
                       Called on the GUI thread; no bus access.
    """

    def __init__(self, session, gui_view_model, on_ui_refresh: Callable[[], None] | None = None) -> None:
        self.session = session
        self.gui_view_model = gui_view_model
        self._on_ui_refresh = on_ui_refresh
        self._dim_refresh_pending: set[str] = set()
        self._dim_refresh_timer: Any = None
        self._dim_refresh_lock = threading.Lock()
        self._rebootstrap_timer: Any = None
        self._rebootstrap_lock = threading.Lock()
        self._register_subscribers()

    def _register_subscribers(self) -> None:
        """Subscribe to all workspace events that should update the ViewModel."""
        # Simple view/cube lifecycle events (engine emits event.* topics)
        self.session.subscribe("event.view.created", self._on_view_created)
        self.session.subscribe("event.view.updated", self._on_view_updated)
        self.session.subscribe("event.view.activated", self._on_view_activated)
        self.session.subscribe("event.view.deleted", self._on_view_deleted)
        self.session.subscribe("event.view.layout_changed", self._on_view_updated)
        self.session.subscribe("event.view.axes_changed", self._on_view_updated)
        self.session.subscribe("event.cube.created", self._on_cube_created)
        self.session.subscribe("event.cube.deleted", self._on_cube_deleted)
        self.session.subscribe("event.dimension.created", self._on_dimension_created)

        # Dimension rename events
        self.session.subscribe("event.dimension.renamed", self._on_dimension_renamed)
        self.session.subscribe("event.dimension_item.renamed", self._on_dimension_item_renamed)
        self.session.subscribe("event.dimension_item.created", self._on_dimension_item_created)
        self.session.subscribe("event.dimension.structure_changed", self._on_dimension_structure_changed)

        # Phase 4: Engine-emitted cell events (event-first, ViewModel does not cache cells)
        self.session.subscribe("event.cell.updated", self._on_cell_updated)
        self.session.subscribe("event.cells.updated", self._on_cells_updated)

        # Restore events: full rebootstrap because the engine workspace was replaced.
        self.session.subscribe("command.restore_checkpoint.succeeded", self._on_checkpoint_restored)

    # =========================================================================
    # Simple events with DTO (patch directly)
    # =========================================================================

    def _on_view_created(self, event) -> None:
        """Patch ViewModel from event DTO or re-query."""
        view_data = event.payload.get("view_data")
        view_id = event.payload.get("view_id")
        if view_id and view_data:
            self.gui_view_model.update_view_snapshot(view_id, view_data)
        elif view_id:
            result = self.session.execute("query", type="view_detail", view_id=view_id)
            if result.success and result.data:
                self.gui_view_model.update_view_snapshot(view_id, result.data)
        self._refresh_ui()

    def _on_view_updated(self, event) -> None:
        """Patch ViewModel when a view property changes (e.g., col_widths)."""
        view_id = event.payload.get("view_id")
        if view_id:
            result = self.session.execute("query", type="view_detail", view_id=view_id)
            if result.success and result.data:
                self.gui_view_model.update_view_snapshot(view_id, result.data)
        self._refresh_ui()

    def _on_view_activated(self, event) -> None:
        """Update current view ID in ViewModel."""
        view_id = event.payload.get("view_id")
        if view_id is not None:
            self.gui_view_model.set_current_view_id(view_id)

    def _on_view_deleted(self, event) -> None:
        """Full rebootstrap on view delete (avoids dangling active ID)."""
        self._rebootstrap()

    def _on_cube_created(self, event) -> None:
        """Patch ViewModel from event DTO or re-query."""
        cube_data = event.payload.get("cube_data")
        cube_id = event.payload.get("cube_id")
        if cube_id and cube_data:
            self.gui_view_model.update_cube_snapshot(cube_id, cube_data)
        elif cube_id:
            result = self.session.execute("query", type="cube_detail", cube_id=cube_id)
            if result.success and result.data:
                self.gui_view_model.update_cube_snapshot(cube_id, result.data)
        self._refresh_ui()

    def _on_cube_deleted(self, event) -> None:
        """Full rebootstrap on cube delete (avoids dangling active ID)."""
        self._rebootstrap()

    # =========================================================================
    # Active view/cube change events (allow None)
    # =========================================================================

    def _on_active_view_changed(self, event) -> None:
        """Update current view ID. Allows explicit None."""
        if "view_id" in event.payload:
            self.gui_view_model.set_current_view_id(event.payload.get("view_id"))

    def _on_active_cube_changed(self, event) -> None:
        """Update current cube ID. Allows explicit None."""
        if "cube_id" in event.payload:
            self.gui_view_model.set_current_cube_id(event.payload.get("cube_id"))

    # =========================================================================
    # Dimension events (Phase E)
    # =========================================================================

    def _on_dimension_created(self, event) -> None:
        """Patch ViewModel from event DTO or re-query."""
        dim_data = event.payload.get("dim_data")
        dim_id = event.payload.get("dim_id")
        if dim_id and dim_data:
            self.gui_view_model.update_dimension_snapshot(dim_id, dim_data)
        elif dim_id:
            result = self.session.execute("query", type="dimension_detail", dim_id=dim_id)
            if result.success and result.data:
                self.gui_view_model.update_dimension_snapshot(dim_id, result.data)
        self._refresh_ui()

    def _on_dimension_deleted(self, event) -> None:
        """Remove dimension snapshot from ViewModel."""
        dim_id = event.payload.get("dim_id")
        if dim_id:
            self.gui_view_model.remove_dimension_snapshot(dim_id)

    def _on_dimension_renamed(self, event) -> None:
        """Patch dimension snapshot with new name."""
        dim_id = event.payload.get("dimension_id")
        new_name = event.payload.get("new_name")
        if dim_id and new_name:
            snap = self.gui_view_model.get_dimension_snapshot(dim_id)
            if snap:
                snap["name"] = new_name
                self.gui_view_model.update_dimension_snapshot(dim_id, snap)

    def _on_dimension_item_renamed(self, event) -> None:
        """Re-query dimension detail to refresh item names."""
        dim_id = event.payload.get("dimension_id")
        if dim_id:
            # TODO(phase5): remove fallback once engine embeds full DTO in event
            result = self.session.execute(
                "query", type="dimension_detail", dim_id=dim_id
            )
            if result.success and result.data:
                self.gui_view_model.update_dimension_snapshot(dim_id, result.data)

    def _on_dimension_item_created(self, event) -> None:
        """Debounce dimension detail refresh for item creation."""
        dim_id = event.payload.get("dim_id")
        if dim_id:
            self._schedule_dim_refresh(dim_id)

    def _on_dimension_structure_changed(self, event) -> None:
        """Debounce dimension detail refresh for structure changes."""
        dim_id = _payload_value(event.payload, "dim_id")
        if dim_id:
            self._schedule_dim_refresh(dim_id)

    def _schedule_dim_refresh(self, dim_id: str) -> None:
        """Coalesce dimension detail refresh requests into a single batch query.

        Multiple events for the same dimension (or different dimensions) within
        a short window are merged into one query per dimension, avoiding
        hundreds of synchronous RPC round-trips during script sourcing.
        """
        with self._dim_refresh_lock:
            self._dim_refresh_pending.add(dim_id)
            if self._dim_refresh_timer is not None:
                return
            try:
                from PySide6 import QtCore
            except ImportError:
                self._flush_dim_refresh()
                return
            self._dim_refresh_timer = QtCore.QTimer()
            self._dim_refresh_timer.setSingleShot(True)
            self._dim_refresh_timer.timeout.connect(self._flush_dim_refresh)
            self._dim_refresh_timer.start(100)

    def _flush_dim_refresh(self) -> None:
        """Execute pending dimension detail queries and update ViewModel."""
        with self._dim_refresh_lock:
            dim_ids = self._dim_refresh_pending.copy()
            self._dim_refresh_pending.clear()
            self._dim_refresh_timer = None
        for dim_id in dim_ids:
            result = self.session.execute(
                "query", type="dimension_detail", dim_id=dim_id
            )
            if result.success and result.data:
                self.gui_view_model.update_dimension_snapshot(dim_id, result.data)
        self._refresh_ui()

    # =========================================================================
    # Helpers
    # =========================================================================

    def _refresh_ui(self) -> None:
        """Notify GUI widgets to rebuild from updated ViewModel.

        Uses the on_ui_refresh callback (GUI-local) instead of bus.publish.
        Skipped when the GUI window has suspended UI refreshes (e.g. during
        a source command) to avoid queuing unnecessary cross-thread calls.
        """
        gui_window = getattr(self._on_ui_refresh, "__self__", None)
        if gui_window is not None and getattr(gui_window, "_ui_refresh_suspended", False):
            return
        if self._on_ui_refresh is not None:
            self._on_ui_refresh()

    # =========================================================================
    # Complex events: full re-bootstrap
    # =========================================================================

    def _rebootstrap(self) -> None:
        """Re-bootstrap full ViewModel from query.workspace_snapshot.

        Debounced: multiple rapid calls (e.g. during workspace load when many
        events fire in succession) are collapsed into a single RPC via a short
        QTimer delay.
        """
        with self._rebootstrap_lock:
            if self._rebootstrap_timer is not None:
                return
            try:
                from PySide6 import QtCore
            except ImportError:
                self._do_rebootstrap()
                return
            self._rebootstrap_timer = QtCore.QTimer()
            self._rebootstrap_timer.setSingleShot(True)
            self._rebootstrap_timer.timeout.connect(self._do_rebootstrap)
            self._rebootstrap_timer.start(100)

    def _do_rebootstrap(self) -> None:
        """Execute the actual rebootstrap RPC and ViewModel replacement."""
        with self._rebootstrap_lock:
            self._rebootstrap_timer = None
        data = self.session.query("workspace_snapshot")
        if data:
            self.gui_view_model.replace_workspace_snapshot(data)
        active = self.session.query("active_view_current")
        if active:
            self.gui_view_model.set_current_view_id(active.get("view_id"))
        self._refresh_ui()

    def _on_workspace_loaded(self, event) -> None:
        """Full re-bootstrap after file load."""
        self._rebootstrap()

    def _on_workspace_reset(self, event) -> None:
        """Full re-bootstrap after workspace reset."""
        self._rebootstrap()

    def _on_workspace_undo(self, event) -> None:
        """Full re-bootstrap after undo."""
        self._rebootstrap()

    def _on_workspace_redo(self, event) -> None:
        """Full re-bootstrap after redo."""
        self._rebootstrap()

    def _on_checkpoint_restored(self, event) -> None:
        """Full re-bootstrap after a checkpoint restore."""
        self._rebootstrap()

    # =========================================================================
    # Phase 4: Engine cell events
    # =========================================================================

    def _on_cell_updated(self, event) -> None:
        """Handle single-cell update event from Engine.

        GUIViewModel does not cache cell values (they are read on-demand from
        the Engine via CellReadModel). Nothing to patch in the ViewModel.
        Future cell-level caching would go here.
        """
        # Event-first: payload contains cube_id, addr, value, display_value.
        # ViewModel has no cell cache, so this is intentionally a no-op.
        pass

    def _on_cells_updated(self, event) -> None:
        """Handle batch cell update event from Engine.

        Since GUIViewModel does not cache cell values, no ViewModel patch is
        needed. If structural metadata changed, a future handler could re-query.
        """
        # Event-first: payload contains cube_id, addresses, count.
        # ViewModel has no cell cache, so this is intentionally a no-op.
        pass