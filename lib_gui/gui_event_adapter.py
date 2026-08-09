"""GUI Event Adapter — translates command bus events to GUI UI events.

B.1 scope:
- command.*.succeeded → ui.refresh / ui.grid.refresh
- command.*.failed    → ui.status.update

Deferred to later phases:
- ui.view.patch
- Full GUIViewModel behavior
- Removing engine callback wiring (gui_set_status_callback, etc.)
"""

from __future__ import annotations

import logging
from typing import Any

from shiboken6 import isValid

logger = logging.getLogger(__name__)


class GUIEventAdapter:
    """Translate command lifecycle events to GUI refresh/status actions.

    After Step 2 this is a GUI-local notification adapter using direct calls.
    It no longer publishes to the bus.
    """

    ALLOWED_COMMAND_TOPICS = [
        "command.*.succeeded",
        "command.*.failed",
    ]

    def __init__(self, gui_window: Any, session: Any) -> None:
        self.gui_window = gui_window
        self.session = session
        self._register_subscribers()

    def _register_subscribers(self) -> None:
        """Register subscribers through the session facade."""
        for topic in self.ALLOWED_COMMAND_TOPICS:
            self.session.subscribe(topic, lambda event, t=topic: self._on_bus_event(t, event))

    def _on_bus_event(self, topic: str, event: Any) -> None:
        """Handle incoming command events and translate to direct GUI calls.

        Args:
            topic: The bus topic (e.g., "command.set.succeeded")
            event: The event envelope with payload and optional command_id
        """
        command_id = event.payload.get("command_id") or getattr(event, "command_id", None) or self._command_id_from_topic(event.topic)

        if topic.endswith(".succeeded"):
            self._emit_ui_refresh(command_id)
        elif topic.endswith(".failed"):
            error = event.payload.get("error")
            if not error:
                error = f"command {command_id} failed"
            self._emit_ui_status(error)

    @staticmethod
    def _command_id_from_topic(topic: str) -> str | None:
        """Derive command_id from envelope topic.

        Handles: "command.set", "command.set.reply", "command.set.succeeded", etc.
        Returns None if topic does not start with "command.".
        """
        if not topic.startswith("command."):
            return None
        rest = topic.removeprefix("command.")
        for suffix in (".before", ".succeeded", ".failed", ".reply"):
            rest = rest.removesuffix(suffix)
        return rest

    _GRID_COMMANDS: set[str] = {
        "set",
        "navigate",
        "create",
        "delete",
        "recalc",
        "restore_checkpoint",
        "undo",
        "redo",
    }

    _GRID_REFRESH_PREFIXES: tuple[str, ...] = (
        "create_",
        "rename_",
        "delete_",
        "move_",
        "attach_",
        "detach_",
        "place_",
        "ungroup_",
        "set_",
        "clear_",
        "run_",
        "save_",
        "load_",
        "apply_",
    )

    # View-state commands that must NOT trigger full grid reload
    _GRID_REFRESH_EXCLUDES: set[str] = {
        "set_selection",
        "move_selection",
        "set_active_view",
        "create_view",
        "create_cube",
        "create_dimension",
        "run_recalculation",
        "clear_cache",
        "set_view_col_width",
        "set_view_row_header_width",
        "set_cell_hardvalue_by_addr",
        "set_cell_hardvalues_batch_by_addr",
        "create_dimension_items_batch",
        "set_cell_value",
        "hardcode_cell",
    }

    def _emit_ui_refresh(self, command_id: str) -> None:
        """Trigger GUI refresh directly based on command type.

        Events may arrive on the transport client's background poll thread.
        The refresh is dispatched through a Qt Signal on the real GUI window
        so it executes on the GUI thread; in unit tests with mocked gui_window
        it falls back to a direct call.
        """
        if command_id in self._GRID_REFRESH_EXCLUDES:
            return
        if command_id not in self._GRID_COMMANDS:
            if not any(command_id.startswith(p) for p in self._GRID_REFRESH_PREFIXES):
                return
        if getattr(self.gui_window, "_ui_refresh_suspended", False):
            return
        from PySide6 import QtCore
        if isinstance(self.gui_window, QtCore.QObject) and isValid(self.gui_window):
            self.gui_window._refresh_gui_requested.emit()
        else:
            try:
                self.gui_window.refresh_gui()
            except RuntimeError:
                pass

    def _emit_ui_status(self, error: str) -> None:
        """Show error status directly on the GUI window."""
        from PySide6 import QtCore
        if isinstance(self.gui_window, QtCore.QObject) and isValid(self.gui_window):
            self.gui_window._set_status_requested.emit("error", f"Error: {error}")
        else:
            try:
                self.gui_window._flash_status_message(f"Error: {error}")
            except RuntimeError:
                pass