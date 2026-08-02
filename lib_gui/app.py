from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import uuid
from typing import Any
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid

from lib_utils.gui_profiler import GuiProfiler

from lib_utils.config import gui as gui_config, engine as engine_config, set_gui as gui_config_set
from lib_utils import gui_constants as _gui_constants

# Debug flag for GUI - can be overridden by environment variable or config
DEBUG_GUI = os.environ.get("DEBUG_GUI", "").lower() in ("true", "1", "yes") or gui_config("debug", "debug_gui", False)
logger = logging.getLogger(__name__)

from lib_gui import config as _gui_cfg
from lib_gui.format_toolbox import FormatToolboxDock
from lib_gui.info_toolbox import InfoToolboxDock
from lib_gui.model_browser import ModelBrowserDock
from lib_gui.performance_watch import PerformanceWatchDock
from lib_gui.status_manager import StatusManager
from lib_gui.status_owner import StatusOwner
from lib_gui.status_state import StatusState
from lib_timelinegui.panel import TimelineDockManager
from lib_gui.view_workspace import ViewWorkspacePane
from lib_gui.view_workspace_controller import ViewWorkspaceController
from lib_gui.view_workspace_window import ViewWorkspaceWindow
from lib_gui_elements.matrix_grid import MatrixGrid
# G6a.2: Engine/workspace construction moved to lib_runtime. All engine model
# imports that remain in this file are function-local inside transitional
# methods that will be commandified or removed in subsequent stages.
from lib_gui.dialogs.rule_editor import RuleEditorDialog
from lib_gui.dialogs.options_dialog import OptionsDialog
from lib_gui.workers.recalc import RecalcOverlay, RecalcWorker
from lib_gui.actions import create_actions, MainWindowActions
from lib_gui.menus import create_menus, create_toolbar
from lib_gui.menubuilder.models import ToolboxConfig, MenuItemDef, WidgetType, MenuLocation


def _outline_signature(nodes):
    """Return a cheap hashable signature of an outline tree.

    Used to detect outline mutations (item insertion, grouping, etc.)
    between successive workspace-change handlers.

    Accepts only plain dict snapshots (DTOs), never engine domain objects.
    """
    return tuple(
        (n.get("label"), n.get("item_id"), _outline_signature(n.get("children") or []))
        for n in nodes
    )


class ToolbarDropOverlay(QtWidgets.QWidget):
    """Transparent overlay widget that sits on top of toolbar to accept drops."""
    
    def __init__(self, target_toolbar: QtWidgets.QToolBar, main_window: 'MainWindow'):
        super().__init__(main_window)
        self._target_toolbar = target_toolbar
        self._main_window = main_window
        self._drop_indicator_pos = None
        self._deleting = False
        
        # Make semi-transparent for debugging
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, False)  # Allow background
        self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        # DEBUG: Semi-transparent blue with visible border
        self.setStyleSheet("background: rgba(59, 130, 246, 0.3); border: 2px solid #2563EB;")
        self.setAcceptDrops(True)
        
        # Position and size to cover toolbar
        self._update_geometry()
        
        print("[DEBUG] ToolbarDropOverlay created")
        
    def _update_geometry(self):
        """Update position to cover the target toolbar."""
        if self._target_toolbar and self._main_window:
            # Map toolbar geometry to main window coordinates
            toolbar_pos = self._target_toolbar.mapTo(self._main_window, QtCore.QPoint(0, 0))
            toolbar_size = self._target_toolbar.size()
            self.setGeometry(toolbar_pos.x(), toolbar_pos.y(), toolbar_size.width(), toolbar_size.height())
            print(f"[DEBUG] Overlay positioned at: ({toolbar_pos.x()}, {toolbar_pos.y()}) size: {toolbar_size.width()}x{toolbar_size.height()}")
        
    def paintEvent(self, event):
        """Draw drop indicator line."""
        super().paintEvent(event)
        if self._drop_indicator_pos is not None:
            painter = QtGui.QPainter(self)
            painter.setPen(QtGui.QPen(QtGui.QColor("#2563EB"), 3))
            painter.drawLine(self._drop_indicator_pos, 0, self._drop_indicator_pos, self.height())
        
    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.mimeData().hasText():
            event.setDropAction(QtCore.Qt.DropAction.MoveAction)
            event.accept()
            print("[DEBUG-OVERLAY] DragEnter accepted with MoveAction")
        else:
            event.ignore()
            
    def dragMoveEvent(self, event: QtGui.QDragMoveEvent):
        if getattr(self, '_deleting', False):
            event.ignore()
            return
        if event.mimeData().hasText():
            # Calculate drop position indicator
            pos = event.position().toPoint()
            self._drop_indicator_pos = self._calculate_insertion_x(pos.x())
            self.update()  # Trigger repaint
            # Set drop action to indicate this is a valid move target
            event.setDropAction(QtCore.Qt.DropAction.MoveAction)
            event.accept()
        else:
            self._drop_indicator_pos = None
            self.update()
            event.ignore()
            
    def dragLeaveEvent(self, event):
        self._drop_indicator_pos = None
        self.update()
        
    def _calculate_insertion_x(self, x: int) -> int:
        """Calculate X position for drop indicator based on toolbar item positions."""
        if not self._target_toolbar:
            return x
            
        # Map overlay position to toolbar coordinates
        toolbar_x = self._target_toolbar.mapFromGlobal(self.mapToGlobal(QtCore.QPoint(x, 0))).x()
        
        # Consistent spacing for drop indicator
        INDICATOR_OFFSET = 4  # Distance from item edge to indicator line
        
        # Find the nearest insertion point between toolbar actions.
        actions = self._target_toolbar.actions()
        for action in actions:
            rect = self._target_toolbar.actionGeometry(action)
            if toolbar_x < rect.center().x():
                return rect.x() - INDICATOR_OFFSET
                    
        # If past all actions, return position at end with consistent spacing
        if actions:
            last_action_rect = self._target_toolbar.actionGeometry(actions[-1])
            return last_action_rect.right() + INDICATOR_OFFSET
        return x
            
    def dropEvent(self, event: QtGui.QDropEvent):
        if getattr(self, '_deleting', False):
            event.ignore()
            return
        print(f"[DEBUG-OVERLAY] dropEvent - hasText: {event.mimeData().hasText()}")
        # Debug: Track drop position
        cursor_pos = QtGui.QCursor.pos()
        local_pos = self.mapFromGlobal(cursor_pos)
        in_toolbar = self.geometry().contains(local_pos)
        print(f"[DEBUG-DRAG] dropEvent: global=({cursor_pos.x()},{cursor_pos.y()}), local=({local_pos.x()},{local_pos.y()}), in_toolbar={in_toolbar}")
        self._drop_indicator_pos = None
        self.update()
        
        if event.mimeData().hasText():
            try:
                data = json.loads(event.mimeData().text())
                print(f"[DEBUG-OVERLAY] Dropped data: {data}")
                
                # Get drop position relative to toolbar
                drop_pos = self._target_toolbar.mapFromGlobal(QtGui.QCursor().pos())
                print(f"[DEBUG-OVERLAY] Drop position: {drop_pos}")
                
                # Process the drop through main window with position info
                self._main_window._process_toolbar_drop(data, drop_pos)
                event.acceptProposedAction()
                print(f"[DEBUG-OVERLAY] Drop accepted and processed")
            except Exception as e:
                print(f"[DEBUG-OVERLAY] Error: {e}")
                import traceback
                traceback.print_exc()
                event.ignore()
        else:
            print(f"[DEBUG-OVERLAY] Drop ignored - no text mime data")
            event.ignore()
            
    def showEvent(self, event):
        """Update geometry when shown."""
        super().showEvent(event)
        self._update_geometry()
        
    def resizeEvent(self, event):
        """Keep overlay positioned over toolbar on resize."""
        super().resizeEvent(event)
        self._update_geometry()


class ButtonDragOverlay(QtWidgets.QWidget):
    """Transparent overlay on top of each toolbar button to capture drag events."""

    def __init__(self, target_button: QtWidgets.QToolButton, action: QtGui.QAction, main_window: 'MainWindow'):
        # Create as child of main window, not the button, to stay on top
        super().__init__(main_window)
        self._toolbar = main_window._edit_toolbar  # Store toolbar reference
        self._action = action
        self._main_window = main_window
        self._deleting = False
        
        # DEBUG: Make visible semi-transparent green overlay
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, False)
        self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        # Semi-transparent green with visible border and label
        self.setStyleSheet("""
            background: rgba(34, 197, 94, 0.5);
            border: 2px solid #16A34A;
            color: #14532D;
            font-size: 8px;
        """)
        self.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.OpenHandCursor))
        self.setAcceptDrops(True)
        # Add label showing action name
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        label = QtWidgets.QLabel(action.text()[:8] if action.text() else "BTN")
        label.setStyleSheet("background: transparent; border: none; font-size: 7px; color: #14532D;")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        
        # Position over the button (will retry if geometry not ready)
        self._position_timer = QtCore.QTimer(self)
        self._position_timer.timeout.connect(self._try_position)
        self._position_retry_count = 0
        self._try_position()
        
        print(f"[DEBUG] ButtonDragOverlay created for '{action.text()}' at geometry: {self.geometry()}")
        
    def _get_target_button(self):
        """Get the current widget for this action (may change after reorder)."""
        if self._toolbar:
            return self._toolbar.widgetForAction(self._action)
        return None
        
    def _try_position(self):
        """Try to position overlay, retry if button not ready."""
        # Guard against running after the overlay has been scheduled for deletion
        if getattr(self, '_deleting', False):
            return
        try:
            # Safety check: ensure main window and toolbar still exist
            if not self._main_window or getattr(self._main_window, '_edit_toolbar', None) is None:
                print("[DEBUG] Main window or toolbar no longer available, deleting overlay")
                self._position_timer.stop()
                self.deleteLater()
                return
                
            target_button = self._get_target_button()
            if target_button and self._main_window:
                btn_pos = target_button.mapTo(self._main_window, QtCore.QPoint(0, 0))
                btn_size = target_button.size()
                if btn_size.width() > 0 and btn_size.height() > 0:
                    self.setGeometry(btn_pos.x(), btn_pos.y(), btn_size.width(), btn_size.height())
                    print(f"[DEBUG] Button overlay positioned at ({btn_pos.x()}, {btn_pos.y()}) size {btn_size.width()}x{btn_size.height()}")
                    self._position_timer.stop()
                    return
        except RuntimeError:
            self._position_timer.stop()
            self._deleting = True
            self.hide()
            return
            
        self._position_retry_count += 1
        if self._position_retry_count < 10:
            if not self._position_timer.isActive():
                self._position_timer.start(50)  # Retry every 50ms
            print(f"[DEBUG] Button overlay retry {self._position_retry_count}, waiting for valid geometry")
        else:
            # Give up after 10 retries
            self._position_timer.stop()
        
    def paintEvent(self, event):
        """Draw the overlay for debugging."""
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        # Draw semi-transparent green fill
        painter.fillRect(self.rect(), QtGui.QColor(34, 197, 94, 128))
        # Draw border
        painter.setPen(QtGui.QPen(QtGui.QColor(22, 163, 74), 2))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        # Draw text
        painter.setPen(QtGui.QColor(20, 83, 45))
        painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, self._action.text()[:6])
        
    def mousePressEvent(self, event: QtGui.QMouseEvent):
        """Start drag when button is pressed."""
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            # Guard against running after the overlay has been scheduled for deletion
            if getattr(self, '_deleting', False):
                return
            # Safety check: ensure main window and toolbar still exist
            if not self._main_window or getattr(self._main_window, '_edit_toolbar', None) is None:
                print("[DEBUG] Main window no longer available, hiding overlay")
                self._position_timer.stop()
                self._deleting = True
                self.hide()
                return
                
            print(f"[DEBUG] Button overlay mouse press on {self._action.text()}")
            
            try:
                # Verify target button still exists
                target_button = self._get_target_button()
                if target_button:
                    target_button.size()
                else:
                    print("[DEBUG] Target button not found, hiding overlay")
                    self._deleting = True
                    self.hide()
                    return
            except RuntimeError:
                print("[DEBUG] Target button was deleted, hiding overlay")
                self._deleting = True
                self.hide()
                return
            
            # Create drag data for reordering
            data = {
                "type": "reorder",
                "action_text": self._action.text(),
                "action_data": self._action.data() if self._action.data() else None
            }
            
            drag = QtGui.QDrag(self)
            mime = QtCore.QMimeData()
            mime.setText(json.dumps(data))
            drag.setMimeData(mime)
            
            # Set drag pixmap from button - same as toolbox editor
            try:
                target_button = self._get_target_button()
                if target_button:
                    pixmap = target_button.grab()
                    if pixmap and not pixmap.isNull():
                        drag.setPixmap(pixmap)
                        drag.setHotSpot(QtCore.QPoint(pixmap.width()//2, pixmap.height()//2))
            except RuntimeError:
                pass
            
            result = drag.exec(QtCore.Qt.DropAction.MoveAction)
            
            # Check cursor position to determine if we should delete or reorder
            cursor_pos = QtGui.QCursor.pos()
            toolbar = self._main_window._edit_toolbar
            toolbar_rect = toolbar.geometry()
            toolbar_top = toolbar.mapToGlobal(QtCore.QPoint(0, 0)).y()
            toolbar_bottom = toolbar_top + toolbar_rect.height()
            cursor_y = cursor_pos.y()
            
            # Delete only if cursor is vertically outside toolbar (above or below)
            vertically_outside = cursor_y < toolbar_top or cursor_y > toolbar_bottom
            
            if result == QtCore.Qt.DropAction.IgnoreAction and vertically_outside:
                # Dropped outside toolbar and not accepted - delete the item
                self._main_window._delete_toolbar_item(self._action.text())
            elif result == QtCore.Qt.DropAction.IgnoreAction:
                # Drop was not accepted by any target, but inside toolbar - reorder manually
                drop_pos = toolbar.mapFromGlobal(cursor_pos)
                self._main_window._reorder_toolbar_button(self._action.text(), drop_pos)
            # If result == MoveAction, the drop was accepted by ToolbarDropOverlay which already handled it
            
    def showEvent(self, event):
        """Update position when shown."""
        super().showEvent(event)
        self._try_position()
        
    def resizeEvent(self, event):
        """Keep overlay matching button size."""
        super().resizeEvent(event)
        self._try_position()


class StatsWorkerThread(QtCore.QThread):
    """Build payload and run selection_stats query off the main thread."""

    result_ready = QtCore.Signal(dict)

    def __init__(
        self,
        mainwindow: Any,
        view_id: str,
        generation: int,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._mainwindow = mainwindow
        self._view_id = view_id
        self._generation = generation

    def _build_payload(self, table: Any) -> dict[str, Any]:
        """Mirror of MainWindow._build_selection_stats_payload for the thread."""
        import time

        mode = table._sel_mode
        page_selections = table._build_page_selections()
        payload: dict[str, Any] = {
            "view_id": self._view_id,
            "mode": mode,
            "page_selections": page_selections,
        }
        MAX_PAYLOAD_KEYS = _gui_cfg.SELECTION_STATS_MAX_PAYLOAD_KEYS
        _YIELD_EVERY = 100
        _proc = 0
        if mode == "cell":
            cell_keys = []
            seen = 0
            for r, c in table._iter_selected_cell_coords():
                if not (0 <= r < len(table._rows)) or not (0 <= c < len(table._cols)):
                    continue
                if not table._rows[r].get("is_leaf", False):
                    continue
                leaf_i = table._leaf_row_index(r)
                if not (0 <= leaf_i < len(table._row_keys)):
                    continue
                if not (0 <= c < len(table._col_keys)):
                    continue
                cell_keys.append((table._row_keys[leaf_i], table._col_keys[c]))
                seen += 1
                _proc += 1
                if _proc >= _YIELD_EVERY:
                    _proc = 0
                    time.sleep(0)
                if seen >= MAX_PAYLOAD_KEYS:
                    print(f"selection limit of {MAX_PAYLOAD_KEYS} exceeded")
                    break
            payload["cell_keys"] = cell_keys
        elif mode == "row":
            row_keys = []
            for r in table._sel_indices:
                if not (0 <= r < len(table._rows)):
                    continue
                if not table._rows[r].get("is_leaf", False):
                    continue
                leaf_i = table._leaf_row_index(r)
                if not (0 <= leaf_i < len(table._row_keys)):
                    continue
                row_keys.append(table._row_keys[leaf_i])
                _proc += 1
                if _proc >= _YIELD_EVERY:
                    _proc = 0
                    time.sleep(0)
                if len(row_keys) >= MAX_PAYLOAD_KEYS:
                    print(f"selection limit of {MAX_PAYLOAD_KEYS} exceeded")
                    break
            payload["row_keys"] = row_keys
        elif mode == "col":
            col_keys = []
            for c in table._sel_indices:
                if not (0 <= c < len(table._cols)):
                    continue
                col_keys.append(table._col_keys[c])
                _proc += 1
                if _proc >= _YIELD_EVERY:
                    _proc = 0
                    time.sleep(0)
                if len(col_keys) >= MAX_PAYLOAD_KEYS:
                    print(f"selection limit of {MAX_PAYLOAD_KEYS} exceeded")
                    break
            payload["col_keys"] = col_keys
        return payload

    def run(self) -> None:
        try:
            table = self._mainwindow._table
            if not isinstance(table, MatrixGrid):
                # Legacy table: compute locally on the thread
                sm = table.selectionModel() if hasattr(table, "selectionModel") else None
                selected_values = [idx.data() for idx in sm.selectedIndexes() if idx.isValid()] if sm else []
                non_empty = [v for v in selected_values if v not in (None, "")]
                numeric = [n for n in (self._mainwindow._coerce_numeric_for_stats(v) for v in non_empty) if n is not None]
                result = {
                    "total_count": len(selected_values),
                    "count": len(numeric),
                    "counta": len(non_empty),
                    "sum": sum(numeric) if numeric else 0.0,
                    "avg": sum(numeric) / len(numeric) if numeric else 0.0,
                    "min": min(numeric) if numeric else None,
                    "max": max(numeric) if numeric else None,
                }
                self.result_ready.emit({"_generation": self._generation, "result": result})
                return

            payload = self._build_payload(table)
            result = self._mainwindow.session.query("selection_stats", **payload)
        except Exception as exc:
            logger.warning("Stats query failed: %s", exc)
            result = None
        self.result_ready.emit({"_generation": self._generation, "result": result or {}})


class _StatsDropdownButton(QtWidgets.QToolButton):
    """Status-bar dropdown button that uses a filled downward triangle.

    Mirrors the PageChip arrow style so the selection-stats menu looks like
    the page-dimension picker chips.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("""
            _StatsDropdownButton {
                padding-right: 20px;
            }
            _StatsDropdownButton::menu-indicator {
                image: none;
                width: 0px;
                height: 0px;
            }
        """)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # type: ignore[override]
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(QtGui.QColor("#1e2d40"))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        arrow_w = 8.0
        arrow_h = 5.0
        margin = 6.0
        x = self.width() - margin - arrow_w
        y = (self.height() - arrow_h) / 2.0
        triangle = QtGui.QPolygonF([
            QtCore.QPointF(x, y),
            QtCore.QPointF(x + arrow_w, y),
            QtCore.QPointF(x + arrow_w / 2.0, y + arrow_h),
        ])
        painter.drawPolygon(triangle)
        painter.end()


class MainWindow(QtWidgets.QMainWindow):
    # Signals for thread-safe GUI operations (emitted from any thread, handled on GUI thread)
    open_file_requested = QtCore.Signal(str)
    view_tab_requested = QtCore.Signal(str)         # view_id
    view_activation_requested = QtCore.Signal(str)  # view_id
    tabs_rebuild_requested = QtCore.Signal()         # view or cube deleted → rebuild tabs
    model_browser_rebuild_requested = QtCore.Signal()  # dimension/item changes → rebuild model browser
    dimension_renamed_requested = QtCore.Signal()    # dimension rename → rebuild rule panels + browser
    dimension_item_renamed_requested = QtCore.Signal()  # dimension item rename → rebuild rule panels
    selection_changed_requested = QtCore.Signal(int, int, int, int)  # row, col, anchor_row, anchor_col
    _refresh_gui_requested = QtCore.Signal()          # internal: GUIEventAdapter → refresh_gui
    _set_status_requested = QtCore.Signal(str, str)  # internal: GUIEventAdapter → _on_status_flash_requested
    timeline_switch_requested = QtCore.Signal()         # workspace loaded → switch timeline session file
    _profiler_start_requested = QtCore.Signal(dict)  # internal: queued profiler start from any thread
    _status_owner_request = QtCore.Signal(str, str, str)  # action, owner, state

    def __init__(self, progress_callback: Any = None, defer_window_restore: bool = False, session: Any = None, recorder: Any = None, macro_runner: Any = None) -> None:
        super().__init__()
        # Keep window hidden during initialization to prevent resize flicker
        self.hide()
        self._progress_callback = progress_callback
        self._defer_window_restore = defer_window_restore
        self._filepath: str | None = None
        self._dirty = False
        self._workspace_number = 0  # 0 = main window, 1+ = additional windows
        self._update_window_title()
        self._setup_window_icon()

        if session is None:
            raise ValueError("MainWindow requires a session. Use lib_runtime to create one.")

        # G6a.2b: host-provided session path — runtime composition lives in lib_runtime
        self.session = session
        self._recorder = recorder
        self._macro_runner = macro_runner
        from lib_command.core.remote_session import RemoteCommandSession
        self.is_remote = isinstance(self.session, RemoteCommandSession)

        # Phase 2: GUI profiler endpoint for this window.
        self.profiler = GuiProfiler(endpoint_id=f"gui:{uuid.uuid4()}")
        self._profiler_start_requested.connect(self._do_profiler_start)

        # Remote mode: session has no .context or .gateway.
        # Bus caching removed — read model binder uses GUI-local callback.

        # GUI config settings that are normally set during composition
        self._preferred_engine = self._load_engine_preference()
        if not self._preferred_engine:
            self._preferred_engine = engine_config("engine", "default_engine", "python")
        self._initial_dep_tracking = gui_config("behavior", "default_dep_tracking", True)

        # Phase D: Wire CellReadModel for cell read query facade
        from lib_gui.cell_read_model import CellReadModel
        from lib_gui.grid_read_model import GridReadModel
        from lib_gui.workspace_read_model import WorkspaceReadModel
        self.cell_read_model = CellReadModel(self.session)
        self.grid_read_model = GridReadModel(self.session)
        self.workspace_read_model = WorkspaceReadModel(self.session)
        
        # Initialize status manager and widget BEFORE event subscriptions to prevent
        # AttributeError when events fire during engine/workspace initialization
        self._status_manager = StatusManager(self)
        self._status_manager.changed.connect(self._on_status_changed)
        self._status_state: str = self._status_manager.current().value
        self._status_indicator: QtWidgets.QLabel | None = None
        # Unique owner IDs for recalc lifecycles that span async signals.
        self._status_owner_id = 0
        self._dirty_recompute_owner: str | None = None
        self._dirty_recompute_complete_pending: bool = False
        self._recompute_error_detail: str | None = None
        self._tile_fetch_grid: "MatrixGrid" | None = None

        # Debounce timer for UI refresh requests. A burst of bus events (e.g.
        # a script creating many model objects) is coalesced into a single
        # browser rebuild + view refresh to avoid overwhelming the GUI thread.
        self._ui_refresh_timer: QtCore.QTimer | None = None
        self._ui_refresh_needs_browser: bool = False
        self._ui_refresh_interval_ms: int = 50
        self._ui_refresh_suspended: bool = False

        # Debounce timer for model browser rebuilds.  A burst of
        # dimension.structure_changed / dimension_item.created events (e.g.
        # during script sourcing) is coalesced into a single rebuild.
        self._browser_rebuild_timer: QtCore.QTimer | None = None
        self._browser_rebuild_interval_ms: int = 100
        
        # Domain event subscriptions (work in both local and remote mode)
        self.session.subscribe("event.profiler.start", self._on_profiler_start)
        self.session.subscribe("event.workspace.dirty_changed", self._on_workspace_dirty_changed)
        self.session.subscribe("event.workspace.loading", self._on_workspace_loading_event)
        self.session.subscribe("event.workspace.loaded", self._on_workspace_loaded_event)
        self.session.subscribe("event.workspace.created", self._on_workspace_loaded_event)
        self.session.subscribe("event.workspace.load_failed", self._on_workspace_load_failed_event)
        self.session.subscribe("event.workspace.saving", self._on_workspace_saving_event)
        self.session.subscribe("event.workspace.saved", self._on_workspace_saved_event)
        self.session.subscribe("event.workspace.save_failed", self._on_workspace_save_failed_event)
        self.session.subscribe("event.calculation.started", self._on_calculation_started_event)
        self.session.subscribe("event.calculation.finished", self._on_calculation_finished_event)
        self.session.subscribe("event.workspace.checkpoint_created", self._on_checkpoint_created_event)
        self.session.subscribe("event.workspace.checkpoint_restored", self._on_checkpoint_restored_event)
        self.session.subscribe("event.dimension.renamed", self._on_dimension_renamed_event)
        self.session.subscribe("event.dimension_item.renamed", self._on_dimension_item_renamed_event)
        self.session.subscribe("event.dimension.structure_changed", self._on_dimension_structure_changed_event)
        self.session.subscribe("event.view.created", self._on_view_created_event)
        self.session.subscribe("event.view.activated", self._on_view_activated_event)
        self.session.subscribe("event.view.deleted", self._on_view_deleted_event)
        self.session.subscribe("event.cube.deleted", self._on_cube_deleted_event)
        self.session.subscribe("event.cube.created", self._on_cube_created_event)
        self.session.subscribe("event.dimension.created", self._on_dimension_created_event)
        self.session.subscribe("event.dimension.deleted", self._on_dimension_deleted_event)
        self.session.subscribe("event.dimension_item.created", self._on_dimension_item_created_event)
        self.session.subscribe("event.dimension_item.deleted", self._on_dimension_item_deleted_event)
        self.session.subscribe("event.selection.changed", self._on_selection_changed_event)
        self.session.subscribe("event.engine.state_changed", self._on_engine_state_changed)

        # Phase B: Register GUI event adapter for bus-first communication.
        # This must happen in BOTH local and remote mode so that command
        # lifecycle events (e.g. command.restore_checkpoint.succeeded) trigger
        # GUI refreshes regardless of whether the session is in-process or
        # connected over the transport.
        from lib_gui.gui_event_adapter import GUIEventAdapter
        self.gui_event_adapter = GUIEventAdapter(self, self.session)

        if not self.is_remote:
            # Phase B: Replace engine status callback with UI event subscription
            from lib_gui.ui_topics import UITopic
            self.session.subscribe(UITopic.STATUS_UPDATE.value, self._on_ui_status_update)
            self.session.subscribe(UITopic.GRID_REFRESH.value, self._on_ui_grid_refresh)
            active = self.session.query("active_view_current")
            self._active_view_id = active.get("view_id") if active else None
            if not self._active_view_id:
                views = self.workspace_read_model.list_views()
                if views:
                    self._active_view_id = views[0]["id"]

            # B.6: Initialize GUIViewModel with current view ID
            from lib_gui.gui_view_model import GUIViewModel
            self.gui_view_model = GUIViewModel()
            self.gui_view_model.set_current_view_id(self._active_view_id)
            self.workspace_read_model._gui_view_model = self.gui_view_model
            # Also attach to session so downstream widgets (e.g. CalculationFlowPanel)
            # can access the cache without constructor changes.
            setattr(self.session, "_gui_view_model", self.gui_view_model)

            # C.3 + C.4: Bootstrap ViewModel and register read model binder
            self._bootstrap_view_model()
            from lib_gui.gui_read_model_binder import GUIReadModelBinder
            self.gui_read_model_binder = GUIReadModelBinder(
                self.session, self.gui_view_model,
                on_ui_refresh=self._do_ui_refresh,
            )
        else:
            # Remote mode: skip local-only bus subscriptions and ViewModel bootstrap.
            self._active_view_id = None
            self.gui_event_adapter = None
            self.gui_view_model = None
            self.gui_read_model_binder = None

        self._report_progress(30, "Creating workspace pane...")
        # Shared view workspace (tabs + rule panel) managed via controller
        self._workspace_pane = ViewWorkspacePane(
            session=self.session,
            parent=self,
        )
        self._workspace = ViewWorkspaceController(
            session=self.session,
            pane=self._workspace_pane,
            cell_read_model=self.cell_read_model,
            workspace_read_model=self.workspace_read_model,
            parent=self,
            profiler=self.profiler,
        )
        self._workspace.view_changed.connect(self._on_active_view_changed)
        self._workspace.status_changed.connect(self._on_status_flash_requested)
        self._workspace.request_status_flash.connect(self._flash_status_message)
        self._workspace.table_selection_changed.connect(self._on_table_selection_changed)
        self._workspace.table_focus_requested.connect(self._focus_active_grid)
        self._workspace.table_focus_requested.connect(self._update_focus_indicator)
        self._workspace.workspace_changed.connect(self._on_workspace_changed)
        self._workspace.data_changed.connect(self._on_workspace_data_changed)
        self._workspace.rules_changed.connect(self._on_rules_changed)
        self._workspace.mark_dirty_requested.connect(lambda: self._mark_dirty(True))
        self._workspace.undo_state_changed.connect(self._update_undo_redo_actions)
        self._workspace.copy_paste_state_changed.connect(self._update_copy_paste_actions)
        self._workspace_windows: list[ViewWorkspaceWindow] = []
        self._recalculating = False  # Guard to prevent recursive signal cascades during F9
        self._cancel_requested = False  # Flag to cancel long-running calculations

        # Async dirty recalculation state (Phase 3)
        self._dirty_recompute_in_progress: bool = False
        self._recompute_pending_again: bool = False
        self._dirty_recompute_thread: QtCore.QThread | None = None
        self._dirty_recompute_worker: RecalcWorker | None = None

        # Recalculation thread management
        self._recalc_thread: QtCore.QThread | None = None
        self._recalc_worker: RecalcWorker | None = None

        # Handle SIGINT (Ctrl+C) and SIGTERM (pkill) to save window state
        # Only works in main thread - skip if running in background thread
        import threading
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
            # Create a timer to allow Python to process signals while Qt event loop runs
            self._signal_timer = QtCore.QTimer(self)
            self._signal_timer.timeout.connect(lambda: None)
            self._signal_timer.start(100)  # 100ms interval to process signals
        else:
            # In background thread - signal handling is done by main thread (REPL)
            self._signal_timer = None

        self._name_box = self._workspace_pane.name_box
        self._rule_bar = self._workspace_pane.rule_bar
        self._tabs = self._workspace_pane.tabs
        self._rule_panel = self._workspace_pane.rule_panel
        self._flow_panel = self._workspace_pane.flow_panel
        self._circular_refs_panel = self._workspace_pane.circular_refs_panel
        self._lower_tabs = self._workspace_pane.lower_tabs
        self._splitter = self._workspace_pane.splitter

        # Track rule panel edit mode for the status bar focus indicator.
        self._rule_panel_editing = False
        self._rule_panel._list.edit_started.connect(self._on_rule_panel_edit_started)
        self._rule_panel._list.edit_ended.connect(self._on_rule_panel_edit_ended)

        self._rule_bar.returnPressed.connect(self._on_rule_bar_enter)
        self._rule_bar.installEventFilter(self)
        self._flow_panel.navigate_requested.connect(self._on_flow_navigate_requested)
        self._circular_refs_panel.navigate_requested.connect(self._on_flow_navigate_requested)
        self._circular_refs_panel.open_trace_requested.connect(self._on_open_trace_requested)

        # Connect thread-safe file opening signal
        self.open_file_requested.connect(self._do_open_file)
        self.view_tab_requested.connect(self._do_add_view_tab)
        self.view_activation_requested.connect(self._do_activate_view)
        self.tabs_rebuild_requested.connect(self._do_rebuild_tabs)
        self.model_browser_rebuild_requested.connect(self._schedule_browser_rebuild)
        self.dimension_renamed_requested.connect(self._do_dimension_renamed)
        self.dimension_item_renamed_requested.connect(self._do_dimension_item_renamed)
        self._refresh_gui_requested.connect(self.refresh_gui)
        self._set_status_requested.connect(self._on_status_flash_requested)
        self._status_owner_request.connect(self._on_status_owner_request)
        self.selection_changed_requested.connect(self._do_update_selection)
        self.timeline_switch_requested.connect(self._do_switch_timeline_session)

        self.setCentralWidget(self._workspace_pane)

        self._report_progress(40, "Creating model browser...")
        self._dock_browser = ModelBrowserDock(session=self.session, parent=self)  # type: ignore[attr-defined]
        self._dock_browser.setObjectName("ModelBrowserDock")
        self.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, self._dock_browser)
        self._dock_browser.setFeatures(
            QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self._dock_browser.tree_widget.currentItemChanged.connect(
            lambda *_: self._update_focus_indicator(self._dock_browser.tree_widget)
        )

        self._report_progress(50, "Creating info toolbox...")
        self._dock_info = InfoToolboxDock(cell_read_model=self.cell_read_model, workspace_read_model=self.workspace_read_model, parent=self)
        self._dock_info.setObjectName("InfoToolboxDock")
        self.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, self._dock_info)
        self._dock_info.setFeatures(
            QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self._dock_browser.tree_widget.currentItemChanged.connect(self._on_browser_selection_changed)

        self._report_progress(60, "Creating format toolbox...")
        self._dock_format = FormatToolboxDock(self)
        self._dock_format.setObjectName("FormatToolboxDock")
        self.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, self._dock_format)
        self._dock_format.setFeatures(
            QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self.splitDockWidget(self._dock_browser, self._dock_info, QtCore.Qt.Orientation.Vertical)
        self.splitDockWidget(self._dock_info, self._dock_format, QtCore.Qt.Orientation.Vertical)

        self._report_progress(70, "Creating performance monitor...")
        self._dock_perf = PerformanceWatchDock(
            self,
            session=self.session,
            refresh_callback=self._on_recalculate,
            desired_state=self._initial_dep_tracking,
        )
        self._dock_perf.setObjectName("PerformanceWatchDock")
        self._dock_perf.setFeatures(
            QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self._dock_perf)

        self._report_progress(80, "Creating timeline panel...")
        # Timeline dock manager - creates and adds panel to dock area
        self._timeline_manager = TimelineDockManager(self)
        self._dock_timeline = self._timeline_manager.get_panel()

        # Wire timeline panel to command session so checkpoint/restore go via
        # message bus.  This must happen in BOTH local and remote mode.
        self._dock_timeline.set_session(self.session)

        if not self.is_remote:
            # Wire up timeline checkpoint/restore signals
            self._connect_timeline_signals()

        # Tabify timeline with performance toolbox on right side
        self.tabifyDockWidget(self._dock_perf, self._dock_timeline)

        # Switch timeline to a workspace-specific session file so that
        # multiple GUI instances (local or remote) share the same timeline.
        self._switch_timeline_to_workspace_session()

        self._status_indicator = QtWidgets.QLabel()
        self._status_indicator.setContentsMargins(
            gui_config("status_bar", "indicator_contents_margins_left", 6),
            gui_config("status_bar", "indicator_contents_margins_top", 0),
            gui_config("status_bar", "indicator_contents_margins_right", 0),
            gui_config("status_bar", "indicator_contents_margins_bottom", 0)
        )
        self._status_indicator.setMinimumWidth(gui_config("status_bar", "indicator_min_width", 220))
        self._status_indicator.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft
        )
        # Attach the custom status indicator to the QStatusBar using the
        # public API so it shows up consistently across styles.
        sb = self.statusBar()
        # Force status-bar text (including our status message) to render in
        # black; the coloured-circle icon is provided by the emoji glyphs
        # themselves so only the icon, not the text, changes colour.
        sb.setStyleSheet(f"QStatusBar {{ color: {gui_config('appearance', 'status_bar_text_color', '#000000')}; }}")
        sb.addWidget(self._status_indicator)
        self._status_indicator.show()

        self._focus_indicator = QtWidgets.QLabel("Focus: —")
        self._focus_indicator.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        focus_container = QtWidgets.QWidget()
        focus_container.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        focus_layout = QtWidgets.QHBoxLayout(focus_container)
        focus_layout.setContentsMargins(0, 0, 0, 0)
        focus_layout.addWidget(self._focus_indicator)
        sb.addPermanentWidget(focus_container, 1)
        try:
            print(
                "DEBUG status_init: added status indicator widget to QStatusBar",
                f"bar={sb!r}",
            )
        except Exception:
            pass
        self._on_status_changed(self._status_manager.current())

        # Selection statistics widget on the right side of the status bar.
        self._selection_stats_actions: dict[str, QtGui.QAction] = {}
        # Whether to show stats in scientific notation (off by default).
        self._selection_stats_use_scientific: bool = False

        # Stats-spinner state.
        self._stats_spinner_frames: list[str] = _gui_constants.LINE_SPINNER_FRAMES
        self._stats_spinner_frames_dense: list[str] = _gui_constants.BRAILLE_SPINNER_FRAMES_DENSE
        self._stats_spinner_running: bool = False
        self._stats_spinner_index: int = 0
        self._stats_thread: QtCore.QThread | None = None
        self._stats_pending: bool = False
        self._stats_generation: int = 0
        # Debounce timer: waits 100ms after the last selection change before
        # starting the stats query, so drag selection doesn't flood the thread
        # pool and block the main rendering thread.
        self._stats_debounce_timer = QtCore.QTimer(self)
        self._stats_debounce_timer.setSingleShot(True)
        self._stats_debounce_timer.timeout.connect(self._do_update_selection_stats)

        self._selection_stats_button = _StatsDropdownButton(self)
        self._selection_stats_button.setAutoRaise(True)
        # Use the Qt6 ToolButtonPopupMode enum for instant popup behaviour.
        self._selection_stats_button.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self._selection_stats_button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly
        )

        stats_menu = QtWidgets.QMenu(self._selection_stats_button)
        stat_defs: list[tuple[str, str]] = [
            ("avg", "Average"),
            ("counta", "CountA"),
            ("count", "Count"),
            ("max", "Maximum"),
            ("min", "Minimum"),
            ("sum", "Sum"),
            ("selcount", "Selection count"),
        ]
        for key, label in stat_defs:
            act = stats_menu.addAction(label)
            act.setCheckable(True)
            act.toggled.connect(lambda checked, k=key: self._on_selection_stat_toggled(k, checked))
            self._selection_stats_actions[key] = act

        # Formatting options
        stats_menu.addSeparator()
        act_fmt_sci = stats_menu.addAction("Scientific format")
        act_fmt_sci.setCheckable(True)
        act_fmt_sci.toggled.connect(self._on_selection_stats_format_toggled)

        # Visibility options
        stats_menu.addSeparator()
        act_none = stats_menu.addAction("None")
        act_none.setCheckable(True)
        act_none.toggled.connect(lambda checked, k="none": self._on_selection_stat_toggled(k, checked))
        self._selection_stats_actions["none"] = act_none

        # Default: Average and Sum enabled.
        self._selection_stats_actions["avg"].setChecked(True)
        self._selection_stats_actions["sum"].setChecked(True)

        self._selection_stats_button.setMenu(stats_menu)
        # Initial label; will be updated on first selection change.
        self._render_stats_from_dto({
            "total_count": 0, "count": 0, "counta": 0,
            "sum": 0.0, "avg": 0.0, "min": None, "max": None,
        })
        # Place stats widget as a permanent item on the far right.
        sb.addPermanentWidget(self._selection_stats_button)

        # --- TEST SPINNER ---
        # A dedicated spinner to prove the timer chain works when decoupled
        # from the selection-stats flow.
        self._test_spinner_label = QtWidgets.QLabel(self)
        self._test_spinner_label.setFixedWidth(16)
        self._test_spinner_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        sb.addPermanentWidget(self._test_spinner_label)
        self._test_spinner_index = 0
        self._test_spinner_frames = ["/", "-", "\\", "|"]
        self._tick_test_spinner()
        # --- END TEST SPINNER ---

        # Engine indicator removed.

        self._report_progress(90, "Creating menus and toolbars...")
        self._actions = create_actions(self)
        create_menus(self, self._actions)
        create_toolbar(self, self._actions)

        self._report_progress(95, "Initializing workspace controller...")
        qapp = QtWidgets.QApplication.instance()
        if qapp is not None:
            qapp.focusChanged.connect(self._on_global_focus_changed)
            # Install application-level event filter to catch Esc even when UI is frozen
            qapp.installEventFilter(self)

        with self._status_manager.hold(
            StatusOwner.load(self._next_status_owner_id()), StatusState.LOADING
        ):
            self._workspace.initialize()
            self._refresh_error_status(allow_from_computing=True)
            self._update_focus_indicator()

        # Register the GUI profiler endpoint so the TUI can target this window.
        try:
            self.session.execute("register_profiler", endpoint_id=self.profiler.endpoint_id)
        except Exception as exc:
            logging.warning("Failed to register GUI profiler endpoint: %s", exc)

        # Restore window state (geometry, dock positions, visibility)
        self._report_progress(100, "Ready")
        # Defer window state restoration if requested (for splash screen flow)
        if not self._defer_window_restore:
            self._restore_window_state()

    def restore_window_state_now(self) -> None:
        """Restore window state - call after splash closes, before showing."""
        self._restore_window_state()
        # Focus the matrix grid so keyboard navigation starts in the grid,
        # not in the rule bar.
        self._workspace.focus_active_grid()

    def _report_progress(self, value: int, message: str = "") -> None:
        """Report initialization progress to the splash screen callback."""
        if self._progress_callback is not None:
            self._progress_callback(value, message)

    # =========================================================================
    # Phase C: Bootstrap GUIViewModel from workspace_snapshot query
    # =========================================================================

    def _bootstrap_view_model(self) -> None:
        """Hydrate GUIViewModel from query.workspace_snapshot (single batch).

        Called after ViewModel and binder creation during MainWindow.__init__.
        Fetches all view/cube metadata in one query and atomically replaces
        the ViewModel's cached DTOs.
        """
        data = self.session.query("workspace_snapshot")
        if data:
            self.gui_view_model.replace_workspace_snapshot(data)

    # ------------------------------------------------------------------
    # Window title helpers
    # ------------------------------------------------------------------

    def _update_window_title(self) -> None:
        """Update window title based on filepath, dirty state, and workspace number."""
        if self._filepath:
            filename = os.path.basename(self._filepath)
        else:
            # Generate timestamped default name
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"New_Model_{timestamp}.json"
        
        dirty_marker = "*" if self._dirty else ""
        
        prefix = "OM Core"  # \u2122"
        if self._workspace_number > 0:
            title = f"{prefix}: {dirty_marker}{filename} (Workspace {self._workspace_number})"
        else:
            title = f"{prefix}: {dirty_marker}{filename}"
        
        self.setWindowTitle(title)

    def _setup_window_icon(self) -> None:
        """Set the window icon from the cube logo PNG."""
        try:
            import os
            icon_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "assets", "logo", "taskbar-icon.png"
            )
            if os.path.exists(icon_path):
                icon = QtGui.QIcon(icon_path)
                self.setWindowIcon(icon)
                # Also set on the application for taskbar consistency
                app = QtWidgets.QApplication.instance()
                if app is not None:
                    app.setWindowIcon(icon)
        except Exception:
            pass  # Silently ignore icon setup errors

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Handle SIGINT signal to save window state before exiting."""
        self._save_window_state()
        QtWidgets.QApplication.quit()

    def _save_window_state(self) -> None:
        """Save window geometry and dock widget states to config."""
        # Save maximized state
        is_maximized = self.isMaximized()
        gui_config_set("window_state", "maximized", is_maximized)

        # Get current screen info
        screen = self.screen()
        if screen:
            screen_name = screen.name()
            screen_serial = screen.serialNumber()
            screen_model = screen.model()
            # Use serial if available (more stable), otherwise use model+name combo
            screen_id = screen_serial if screen_serial else f"{screen_model}_{screen_name}"
            gui_config_set("window_state", "screen_id", screen_id)

            # Save relative position within the screen (percentage-based for stability)
            screen_geo = screen.geometry()
            window_geo = self.geometry()
            rel_x = (window_geo.x() - screen_geo.x()) / screen_geo.width() if screen_geo.width() > 0 else 0
            rel_y = (window_geo.y() - screen_geo.y()) / screen_geo.height() if screen_geo.height() > 0 else 0
            rel_width = window_geo.width() / screen_geo.width() if screen_geo.width() > 0 else 0.7
            rel_height = window_geo.height() / screen_geo.height() if screen_geo.height() > 0 else 0.7

            gui_config_set("window_state", "rel_x", rel_x)
            gui_config_set("window_state", "rel_y", rel_y)
            gui_config_set("window_state", "rel_width", rel_width)
            gui_config_set("window_state", "rel_height", rel_height)

            # Also save absolute position as fallback
            gui_config_set("window_state", "abs_x", window_geo.x())
            gui_config_set("window_state", "abs_y", window_geo.y())
            gui_config_set("window_state", "abs_width", window_geo.width())
            gui_config_set("window_state", "abs_height", window_geo.height())

        # Only save geometry if not maximized (maximized geometry is not useful)
        if not is_maximized:
            geometry = self.saveGeometry()
            if geometry:
                gui_config_set("window_state", "geometry", geometry.toBase64().data().decode())

        # Save dock widget states (includes visibility and positions)
        dock_state = self.saveState()
        if dock_state:
            gui_config_set("window_state", "dock_state", dock_state.toBase64().data().decode())

        # Save model browser dock width
        if self._dock_browser:
            width = self._dock_browser.width()
            gui_config_set("window_state", "dock_browser_width", width)

        # Save timeline panel width (always save if dock exists)
        if self._dock_timeline:
            width = self._dock_timeline.width()
            gui_config_set("window_state", "timeline_width", width)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # type: ignore[override]
        """Handle window close - save window state and cancel pending timers."""
        # Cancel width restore timers FIRST - before saving state or deleting widgets
        self._cancel_width_restore_timers()
        self._save_window_state()
        event.accept()

    def _find_target_screen(self, saved_screen_id: str | None) -> QtGui.QScreen | None:
        """Find the best screen to restore window on.

        Priority:
        1. Screen matching saved ID
        2. Screen with mouse cursor
        3. Primary screen
        4. First available screen
        """
        screens = QtWidgets.QApplication.screens()
        if not screens:
            return None

        # 1. Try to find screen by saved ID
        if saved_screen_id:
            for screen in screens:
                screen_serial = screen.serialNumber()
                screen_id = screen_serial if screen_serial else f"{screen.model()}_{screen.name()}"
                if screen_id == saved_screen_id:
                    return screen

        # 2. Try screen with mouse cursor
        cursor_pos = QtGui.QCursor.pos()
        for screen in screens:
            if screen.geometry().contains(cursor_pos):
                return screen

        # 3. Try primary screen
        primary = QtWidgets.QApplication.primaryScreen()
        if primary:
            return primary

        # 4. Return first available screen
        return screens[0] if screens else None

    def _restore_window_state(self) -> None:
        """Restore window geometry and dock widget states from config."""
        # Restore maximized state first
        was_maximized = gui_config("window_state", "maximized", False)

        # Get saved screen info and find target screen
        saved_screen_id = gui_config("window_state", "screen_id", None)
        target_screen = self._find_target_screen(saved_screen_id)

        if target_screen:
            screen_geo = target_screen.availableGeometry()

            # Try to restore using relative position if we have the data
            rel_x = gui_config("window_state", "rel_x", None)
            rel_y = gui_config("window_state", "rel_y", None)
            rel_width = gui_config("window_state", "rel_width", None)
            rel_height = gui_config("window_state", "rel_height", None)

            if all(v is not None for v in [rel_x, rel_y, rel_width, rel_height]):
                # Calculate geometry based on relative position within target screen
                new_x = screen_geo.x() + int(rel_x * screen_geo.width())
                new_y = screen_geo.y() + int(rel_y * screen_geo.height())
                new_width = int(rel_width * screen_geo.width())
                new_height = int(rel_height * screen_geo.height())

                # Clamp to available geometry
                new_x = max(screen_geo.x(), min(new_x, screen_geo.right() - 100))
                new_y = max(screen_geo.y(), min(new_y, screen_geo.bottom() - 100))
                new_width = min(new_width, screen_geo.width())
                new_height = min(new_height, screen_geo.height())

                self.setGeometry(new_x, new_y, new_width, new_height)
            else:
                # Fallback: try legacy geometry or absolute position
                self._restore_legacy_geometry(was_maximized)

            # If maximized, restore position first then maximize on the target screen
            if was_maximized:
                self.showMaximized()
        else:
            # No screens available, fallback to legacy restore
            self._restore_legacy_geometry(was_maximized)

        # Restore dock widget states (includes visibility and positions)
        dock_state_data = gui_config("window_state", "dock_state", None)
        if dock_state_data:
            try:
                dock_state = QtCore.QByteArray.fromBase64(dock_state_data.encode())
                self.restoreState(dock_state)
            except Exception:
                pass  # Use default if restore fails
        # If no saved state, show docks by default
        if not dock_state_data:
            self._dock_browser.show()
            self._dock_info.hide()
            self._dock_format.hide()
            self._dock_perf.show()
            self._dock_timeline.show()

        # Restore model browser dock width (if saved separately)
        browser_width = gui_config("window_state", "dock_browser_width", None)
        if browser_width is not None and self._dock_browser:
            try:
                width = int(browser_width)
                # Store QTimer reference to cancel during cleanup
                timer = QtCore.QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(lambda w=width: self._apply_dock_browser_width(w))
                timer.start(500)
                # Store timer reference as attribute on self for cleanup
                self._width_restore_timers = getattr(self, '_width_restore_timers', [])
                self._width_restore_timers.append(timer)
            except Exception:
                pass

        # Restore timeline panel width (if saved separately)
        timeline_width = gui_config("window_state", "timeline_width", None)
        if timeline_width is not None and self._dock_timeline:
            try:
                width = int(timeline_width)
                # Store QTimer reference to cancel during cleanup
                timer = QtCore.QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(lambda w=width: self._apply_timeline_width(w))
                timer.start(500)
                self._width_restore_timers.append(timer)
            except Exception:
                pass
            if not dock_state_data:
                self._dock_timeline.show()
        elif not dock_state_data and self._dock_timeline:
            self._dock_timeline.show()

    def _restore_legacy_geometry(self, was_maximized: bool) -> None:
        """Restore geometry using legacy methods (absolute position or saved geometry)."""
        geometry_data = gui_config("window_state", "geometry", None)

        if geometry_data and not was_maximized:
            try:
                geometry = QtCore.QByteArray.fromBase64(geometry_data.encode())
                self.restoreGeometry(geometry)
            except Exception:
                was_maximized = False

        if not was_maximized and not geometry_data:
            # Try absolute position fallback
            abs_x = gui_config("window_state", "abs_x", None)
            abs_y = gui_config("window_state", "abs_y", None)
            abs_width = gui_config("window_state", "abs_width", None)
            abs_height = gui_config("window_state", "abs_height", None)

            if all(v is not None for v in [abs_x, abs_y, abs_width, abs_height]):
                # Find screen nearest to saved absolute rect
                target = self._find_nearest_screen(QtCore.QRect(abs_x, abs_y, abs_width, abs_height))
                if target:
                    avail = target.availableGeometry()
                    # Clamp to available geometry
                    new_x = max(avail.x(), min(abs_x, avail.right() - 100))
                    new_y = max(avail.y(), min(abs_y, avail.bottom() - 100))
                    new_width = min(abs_width, avail.width())
                    new_height = min(abs_height, avail.height())
                    self.setGeometry(new_x, new_y, new_width, new_height)
                else:
                    self.setGeometry(abs_x, abs_y, abs_width, abs_height)
            else:
                # Use default size from config
                default_width = gui_config("window", "default_width", 1400)
                default_height = gui_config("window", "default_height", 900)
                self.resize(default_width, default_height)

        # Clamp to ensure visibility on any available screen
        self._clamp_window_to_available_geometry()

    def _find_nearest_screen(self, rect: QtCore.QRect) -> QtGui.QScreen | None:
        """Find the screen nearest to the given rectangle."""
        screens = QtWidgets.QApplication.screens()
        if not screens:
            return None
        if len(screens) == 1:
            return screens[0]

        center = rect.center()
        nearest = None
        min_dist = float('inf')

        for screen in screens:
            screen_geo = screen.geometry()
            screen_center = screen_geo.center()
            dist = (center.x() - screen_center.x()) ** 2 + (center.y() - screen_center.y()) ** 2
            if dist < min_dist:
                min_dist = dist
                nearest = screen

        return nearest

    def _clamp_window_to_available_geometry(self) -> None:
        """Ensure window is fully visible within available screen geometry."""
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        if not screen:
            return

        avail = screen.availableGeometry()
        geo = self.geometry()

        # Ensure minimum visibility (100x100 window must be visible)
        min_visible = 100

        new_x = max(avail.x(), min(geo.x(), avail.right() - min_visible))
        new_y = max(avail.y(), min(geo.y(), avail.bottom() - min_visible))
        new_width = min(geo.width(), avail.width())
        new_height = min(geo.height(), avail.height())

        self.setGeometry(new_x, new_y, new_width, new_height)

    def __del__(self) -> None:
        """Cancel width restore timers when MainWindow is destroyed."""
        try:
            self._cancel_width_restore_timers()
        except Exception:
            pass  # Object may already be partially deleted

    def _cancel_width_restore_timers(self) -> None:
        """Cancel all pending width restore timers to prevent accessing deleted objects.
        
        Called during cleanup/test teardown to prevent QTimer callbacks
        from firing after C++ objects have been deleted.
        """
        timers = getattr(self, '_width_restore_timers', [])
        for timer in timers:
            try:
                timer.stop()
                timer.deleteLater()
            except Exception:
                pass
        self._width_restore_timers = []

    def _apply_dock_browser_width(self, width: int) -> None:
        """Apply the saved width to the model browser dock."""
        # Check if QApplication is still alive (prevents firing during test teardown)
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        
        # Check if MainWindow instance is still valid
        try:
            _ = self.objectName()
        except RuntimeError:
            # MainWindow has been deleted
            return
            
        try:
            # Check if this widget is still valid
            if not self._dock_browser:
                return
            # Check if the C++ object is still valid
            if not self._dock_browser.isVisible():
                self._dock_browser.show()
            
            # Use resizeDocks to set dock widget width in dock area
            # This is the proper Qt way to resize dock widgets
            self.resizeDocks([self._dock_browser], [width], QtCore.Qt.Orientation.Horizontal)
        except (RuntimeError, AttributeError):
            # C++ object already deleted (e.g., during test teardown) - silently ignore
            pass

    def _connect_timeline_signals(self) -> None:
        """Wire up timeline checkpoint/restore signals and payload callbacks.

        This enables the timeline panel to save and restore workspace state
        when creating checkpoints and restoring snapshots.
        """
        if not self._dock_timeline:
            return

        # Connect permission signals - auto-approve for now
        self._dock_timeline.checkpoint_permission_requested.connect(
            self._on_checkpoint_permission_requested
        )
        self._dock_timeline.restore_permission_requested.connect(
            self._on_restore_permission_requested
        )

        # Timeline payload callbacks are now wired by command handlers
        # (lib_command/commands/timeline.py::_setup_timeline_callbacks).
        # GUI must not set composition-root payload generators directly.

    @QtCore.Slot(str, object)
    def _on_checkpoint_permission_requested(self, description: str, callback: Any) -> None:
        """Handle checkpoint permission request - auto-approve."""
        print(f"[Timeline] Checkpoint permission requested: {description!r}")
        callback(True)

    @QtCore.Slot(str, object)
    def _on_restore_permission_requested(self, snapshot_id: str, callback: Any) -> None:
        """Handle restore permission request - auto-approve for now."""
        print(f"[Timeline] Restore permission requested: {snapshot_id}")
        callback(True)

    def _apply_timeline_width(self, width: int) -> None:
        """Apply the saved width to the timeline panel."""
        # Check if QApplication is still alive (prevents firing during test teardown)
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        
        # Check if MainWindow instance is still valid
        try:
            _ = self.objectName()
        except RuntimeError:
            # MainWindow has been deleted
            return
            
        try:
            # Check if this widget is still valid
            if not self._dock_timeline:
                return
            # Check if the C++ object is still valid
            if not self._dock_timeline.isVisible():
                self._dock_timeline.show()
            
            # Use resizeDocks to set dock widget width in dock area
            self.resizeDocks([self._dock_timeline], [width], QtCore.Qt.Orientation.Horizontal)
        except (RuntimeError, AttributeError):
            # C++ object already deleted (e.g., during test teardown) - silently ignore
            pass


    def _switch_timeline_to_workspace_session(self) -> None:
        """Request a timeline refresh for the current workspace.

        The panel is a pure client: it reads snapshots through the query spine
        and does not manage the timeline SQLite file path directly. We just ask
        it to reload.
        """
        if not self._dock_timeline:
            return
        try:
            self._dock_timeline.reload()
        except Exception:
            pass  # Timeline falls back to fresh Session Start

    def _mark_dirty(self, dirty: bool = True) -> None:
        """Mark workspace as having unsaved changes."""
        if self._dirty != dirty:
            self._dirty = dirty
            self._update_window_title()

    def _check_unsaved_changes(self) -> bool:
        """Check if there are unsaved changes and prompt user if needed.
        
        Returns True if it's safe to proceed (user saved or discarded changes),
        False if user cancelled.
        """
        if not self._dirty:
            return True
        
        reply = QtWidgets.QMessageBox.question(
            self,
            "Unsaved Changes",
            "The current workspace has unsaved changes. Do you want to save them?",
            QtWidgets.QMessageBox.StandardButton.Save 
            | QtWidgets.QMessageBox.StandardButton.Discard 
            | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Save,
        )
        
        if reply == QtWidgets.QMessageBox.StandardButton.Save:
            self._on_save()
            # After save, check if still dirty (save may have been cancelled)
            return not self._dirty
        elif reply == QtWidgets.QMessageBox.StandardButton.Discard:
            return True
        else:  # Cancel
            return False

    # ------------------------------------------------------------------
    # Tab helpers
    # ------------------------------------------------------------------

    def _build_tabs(self) -> None:
        self._workspace.rebuild_tabs()

    def _active_view_exists(self) -> bool:
        if not isinstance(self._active_view_id, str):
            return False
        summary = self.workspace_read_model.workspace_summary()
        return summary is not None and self._active_view_id in summary.get("view_ids", [])

    @QtCore.Slot()
    def _on_table_selection_changed(self) -> None:
        # self._workspace (ViewWorkspaceController) emits this signal;
        # self._table is the active grid widget.  They are never the same
        # object, so a sender==active check would always skip.  Just guard
        # that a table exists.
        if self._table is None:
            return
        self._update_selection_stats()
        self._update_focus_indicator()
        DEBUG_GUI and print(f"DEBUG _on_table_selection_changed: END")

    @QtCore.Slot(QtWidgets.QWidget, QtWidgets.QWidget)
    def _on_global_focus_changed(
        self,
        old: QtWidgets.QWidget | None,
        new: QtWidgets.QWidget | None,
    ) -> None:
        # Only update focus indicator if new focus belongs to active table
        active_grid = self._table
        DEBUG_GUI and print(f"DEBUG focus_changed: old={old} new={new} active_grid={active_grid}")
        if active_grid is None:
            DEBUG_GUI and print(f"DEBUG focus_changed: SKIPPED - no active grid")
            return
        if new is None:
            DEBUG_GUI and print(f"DEBUG focus_changed: SKIPPED - new is None")
            return
        # Check if new widget is or belongs to the active grid
        parent = new
        while parent is not None:
            if parent == active_grid:
                break
            parent = parent.parent()
        if parent != active_grid:
            DEBUG_GUI and print(f"DEBUG focus_changed: SKIPPED - not active grid")
            return
        self._update_focus_indicator(new)

    @QtCore.Slot(int, int)
    def _on_tab_moved(self, from_index: int, to_index: int) -> None:
        self._workspace.on_tab_moved(from_index, to_index)

    @property
    def _current_tab(self) -> ViewTab:
        return self._workspace.current_tab

    @property
    def _table(self) -> QtWidgets.QWidget | None:
        return self._workspace.active_table

    @property
    def _model(self) -> QtCore.QAbstractItemModel | None:
        return self._workspace.active_model

    def _table_is_matrix(self) -> bool:
        return isinstance(self._table, MatrixGrid)

    def _matrix_selected_keys(self) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
        if not isinstance(self._table, MatrixGrid):
            return None
        return self._table.selected_keys()

    def _parse_clipboard_tsv(self) -> list[list[str]]:
        text = QtWidgets.QApplication.clipboard().text()
        if not text:
            return []
        rows = text.splitlines()
        if not rows:
            return []
        return [[part.rstrip("\r") for part in row.split("\t")] for row in rows]

    def _matrix_selection_is_rectangular(self) -> bool:
        """Return True if the visible selection is a single full rectangle.

        This intentionally does NOT query the engine; it is used only to
        enable/disable menu actions. The expensive value lookups are deferred
        until the action is actually triggered.
        """
        if not isinstance(self._table, MatrixGrid):
            return False
        coords = self._table.selected_cell_coords()
        if not coords:
            return False
        rows = [r for r, _ in coords]
        cols = [c for _, c in coords]
        top, bottom = min(rows), max(rows)
        left, right = min(cols), max(cols)
        expected = (bottom - top + 1) * (right - left + 1)
        if len(coords) != expected:
            return False
        coord_set = set(coords)
        return all(
            (r, c) in coord_set
            for r in range(top, bottom + 1)
            for c in range(left, right + 1)
        )

    def _matrix_selected_rect_values(self) -> tuple[list[tuple[int, int]], list[list[object]], tuple[int, int, int, int]] | None:
        if not isinstance(self._table, MatrixGrid):
            return None
        if not self._active_view_id:
            return None
        if not self.workspace_read_model.get_view(self._active_view_id):
            return None
        coords = self._table.selected_cell_coords()
        if not coords:
            return None
        rows = [r for r, _ in coords]
        cols = [c for _, c in coords]
        top, bottom = min(rows), max(rows)
        left, right = min(cols), max(cols)
        expected = (bottom - top + 1) * (right - left + 1)
        if len(coords) != expected:
            return None
        coord_set = set(coords)
        if any((r, c) not in coord_set for r in range(top, bottom + 1) for c in range(left, right + 1)):
            return None
        values: list[list[object]] = []
        for r in range(top, bottom + 1):
            row_values: list[object] = []
            for c in range(left, right + 1):
                keys_many = self._table.selected_cell_keys_many()
                lookup = {(rr, cc): idx for idx, (rr, cc) in enumerate(coords)}
                idx = lookup.get((r, c))
                if idx is None or idx >= len(keys_many):
                    return None
                row_key, col_key = keys_many[idx]
                row_values.append(self.cell_read_model.cell_value(self._active_view_id, row_key, col_key))
            values.append(row_values)
        return coords, values, (top, left, bottom, right)

    def _matrix_selection_is_axis_assignment_eligible(self) -> bool:
        """Return True if the current selection could be used for axis label assignment.

        This is a cheap, engine-free check used only for enabling/disabling the
        action. The actual cell reads happen when the action is invoked.
        """
        if not isinstance(self._table, MatrixGrid):
            return False
        if not self._active_view_id:
            return False
        view = self.workspace_read_model.get_view(self._active_view_id)
        if not view:
            return False
        sel_mode = getattr(self._table, "_sel_mode", None)
        sel_indices = getattr(self._table, "_sel_indices", set())
        if not isinstance(sel_indices, set):
            return False
        if sel_mode == "row":
            if not view.get("col_dim_ids"):
                return False
            ordered_rows = sorted(idx for idx in sel_indices if isinstance(idx, int))
            if len(ordered_rows) != 1:
                return False
            rows = getattr(self._table, "_rows", [])
            if not (0 <= ordered_rows[0] < len(rows)):
                return False
            return bool(rows[ordered_rows[0]].get("is_leaf", False))
        if sel_mode == "col":
            if not view.get("row_dim_ids"):
                return False
            ordered_cols = sorted(idx for idx in sel_indices if isinstance(idx, int))
            if len(ordered_cols) != 1:
                return False
            cols = getattr(self._table, "_cols", [])
            if not (0 <= ordered_cols[0] < len(cols)):
                return False
            return bool(cols[ordered_cols[0]].get("is_leaf", False))
        return False

    def _matrix_selected_axis_assignment(self) -> tuple[str, str, list[str], list[str]] | None:
        if not isinstance(self._table, MatrixGrid):
            return None
        if not self._active_view_id:
            return None
        view = self.workspace_read_model.get_view(self._active_view_id)
        if not view:
            return None
        sel_mode = getattr(self._table, "_sel_mode", None)
        sel_indices = getattr(self._table, "_sel_indices", set())
        if not isinstance(sel_indices, set):
            return None
        if sel_mode == "row":
            if not view.get("col_dim_ids"):
                return None
            ordered_rows = sorted(idx for idx in sel_indices if isinstance(idx, int))
            if len(ordered_rows) != 1:
                return None
            row_idx = ordered_rows[0]
            rows = getattr(self._table, "_rows", [])
            cols = getattr(self._table, "_cols", [])
            if not (0 <= row_idx < len(rows)):
                return None
            row = rows[row_idx]
            if not row.get("is_leaf", False):
                return None
            row_key = self._table._row_keys[self._table._leaf_row_index(row_idx)]
            item_ids: list[str] = []
            labels: list[str] = []
            for col_idx, col in enumerate(cols):
                if not col.get("is_leaf", False):
                    continue
                item_id = col.get("item_id")
                if not isinstance(item_id, str):
                    return None
                col_key = self._table._col_keys[col_idx]
                cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
                value = cell_dto.get("value")
                label = "" if value is None else str(value).strip()
                item_ids.append(item_id)
                labels.append(label)
            return ("col", view.get("col_dim_ids")[0], item_ids, labels)
        if sel_mode == "col":
            if not view.get("row_dim_ids"):
                return None
            ordered_cols = sorted(idx for idx in sel_indices if isinstance(idx, int))
            if len(ordered_cols) != 1:
                return None
            col_idx = ordered_cols[0]
            rows = getattr(self._table, "_rows", [])
            cols = getattr(self._table, "_cols", [])
            if not (0 <= col_idx < len(cols)) or not cols[col_idx].get("is_leaf", False):
                return None
            col_key = self._table._col_keys[col_idx]
            item_ids: list[str] = []
            labels: list[str] = []
            for row_idx, row in enumerate(rows):
                if not row.get("is_leaf", False):
                    continue
                item_id = row.get("item_id")
                if not isinstance(item_id, str):
                    return None
                row_key = self._table._row_keys[self._table._leaf_row_index(row_idx)]
                cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
                value = cell_dto.get("value")
                label = "" if value is None else str(value).strip()
                item_ids.append(item_id)
                labels.append(label)
            return ("row", view.get("row_dim_ids", [None])[0], item_ids, labels)
        return None

    def _matrix_selected_dimension_items(self) -> tuple[str, list[str]] | None:
        if not isinstance(self._table, MatrixGrid):
            return None
        if not self._active_view_id:
            return None
        view = self.workspace_read_model.get_view(self._active_view_id)
        if not view:
            return None
        sel_mode = getattr(self._table, "_sel_mode", None)
        sel_indices = getattr(self._table, "_sel_indices", set())
        if not isinstance(sel_indices, set):
            return None
        if sel_mode == "row":
            if not view.get("row_dim_ids"):
                return None
            rows = getattr(self._table, "_rows", [])
            item_ids: list[str] = []
            for row_idx in sorted(idx for idx in sel_indices if isinstance(idx, int)):
                if not (0 <= row_idx < len(rows)):
                    return None
                row = rows[row_idx]
                if not row.get("is_leaf", False):
                    return None
                item_id = row.get("item_id")
                if not isinstance(item_id, str):
                    return None
                item_ids.append(item_id)
            return (view.get("row_dim_ids")[0], item_ids)
        if sel_mode == "col":
            if not view.get("col_dim_ids"):
                return None
            cols = getattr(self._table, "_cols", [])
            item_ids = []
            for col_idx in sorted(idx for idx in sel_indices if isinstance(idx, int)):
                if not (0 <= col_idx < len(cols)):
                    return None
                col = cols[col_idx]
                if not col.get("is_leaf", False):
                    return None
                item_id = col.get("item_id")
                if not isinstance(item_id, str):
                    return None
                item_ids.append(item_id)
            return (view.get("col_dim_ids")[0], item_ids)
        return None

    def _confirm_and_delete_dimension_items(self, dim_id: str, item_ids: list[str], skip_confirm: bool = False) -> bool:
        impact = self.session.query("dimension_item_deletion_impact", dim_id=dim_id, item_ids=item_ids) or {}
        delete_item_ids = impact.get("item_ids", [])

        # Show info if protected items were skipped
        skipped_ids = impact.get("skipped_protected_ids", [])
        if skipped_ids and not skip_confirm:
            dim_dto = None
            if self.gui_view_model is not None:
                dim_dto = self.gui_view_model.get_dimension_snapshot(dim_id)
            if not dim_dto:
                dim_dto = self.session.query("dimension_detail", dim_id=dim_id)
                if dim_dto and self.gui_view_model is not None:
                    self.gui_view_model.update_dimension_snapshot(dim_id, dim_dto)
            item_names_map = dict(zip(dim_dto.get("item_ids", []), dim_dto.get("item_names", []))) if dim_dto else {}
            skipped_names = [item_names_map.get(iid, iid) for iid in skipped_ids]
            skipped_list = "\n".join(f"- {name}" for name in skipped_names[:10])
            if len(skipped_names) > 10:
                skipped_list += f"\n- … and {len(skipped_names) - 10} more"
            QtWidgets.QMessageBox.information(
                self,
                "Protected Items",
                f"The following built-in technical channels cannot be deleted:\n\n{skipped_list}"
            )

        if not delete_item_ids:
            # Only show "no items" dialog if we didn't already show protected items dialog
            if not skip_confirm and not skipped_ids:
                QtWidgets.QMessageBox.information(self, "Delete Dimension Items", "No dimension items are selected for deletion.")
            return False

        if not skip_confirm:
            dim_name = str(impact.get("dim_name", "Dimension"))
            item_names = impact.get("item_names", [])
            impacted_cubes = impact.get("impacted_cubes", [])
            total_data_cells = int(impact.get("total_data_cell_count", 0))
            cube_count = int(impact.get("cube_count", 0))

            item_lines = "\n".join(f"- {name}" for name in item_names[:12])
            if len(item_names) > 12:
                item_lines += f"\n- … and {len(item_names) - 12} more"
            cube_lines = "\n".join(
                f"- {entry['cube_name']}: {entry['data_cell_count']} data cells"
                for entry in impacted_cubes
            )
            if not cube_lines:
                cube_lines = "- No stored data cells found"

            # Build rule impact section
            total_rules = int(impact.get("total_affected_rules", 0))
            anchored_rules = int(impact.get("affected_anchored_rules", 0))
            rules = int(impact.get("affected_rules", 0))

            if total_rules > 0:
                rule_lines = f"- Anchored rules: {anchored_rules}\n- Rules: {rules}"
            else:
                rule_lines = "- No rules will be affected"

            message = (
                f"Delete {len(delete_item_ids)} item(s) from dimension '{dim_name}'?\n\n"
                f"Items:\n{item_lines}\n\n"
                f"WARNING: This will delete all data across all cubes using this dimension for each deleted item.\n\n"
                f"Affected cubes ({cube_count}) and data cells to be deleted ({total_data_cells} total):\n{cube_lines}\n\n"
                f"Rules to be affected ({total_rules} total):\n{rule_lines}\n\n"
                f"Note: Rules referencing deleted items will show #REF! errors."
            )
            resp = QtWidgets.QMessageBox.warning(
                self,
                "Delete Dimension Items",
                message,
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            if resp != QtWidgets.QMessageBox.StandardButton.Yes:
                return False

        self.session.execute("delete_dimension_items", dim_id=dim_id, item_ids=delete_item_ids)
        self._finalize_structure_change()
        self._rule_panel.rebuild()
        
        # Refresh all views that might be affected by this dimension deletion
        self._refresh_views_for_dimension(dim_id)
        return True

    def _create_default_view_for_cube(self, cube_name: str, cube_id: str, dim_ids: list[str]) -> dict | None:
        user_dim_ids = [d for d in dim_ids if d != "@"]
        row_dim = user_dim_ids[0]
        col_dim = user_dim_ids[1] if len(user_dim_ids) > 1 else None
        page_dims = user_dim_ids[2:] if len(user_dim_ids) > 2 else []
        result = self.session.execute(
            "create_view",
            name=f"View of {cube_name}",
            cube_id=cube_id,
            row_dims=[row_dim],
            col_dims=[col_dim] if col_dim else [],
            page_dim_ids=["@"] + page_dims,
        )
        if not result.success:
            QtWidgets.QMessageBox.warning(self, "Create View", str(result.error or "View creation failed"))
            return None
        view_id = result.data.get("id") if result.data else None
        if not view_id:
            return None
        return self.workspace_read_model.get_view(view_id)

    def _finalize_structure_change(self, focus_view_id: str | None = None) -> None:
        self._dock_browser.rebuild()
        self._reload_active_view()
        if isinstance(focus_view_id, str):
            self._workspace.focus_view(focus_view_id)

    def _refresh_views_for_dimension(self, dim_id: str) -> None:
        """Refresh all views that use the given dimension.
        
        Called after dimension item deletion to ensure views are updated.
        """
        # Find all cubes that use this dimension (including system cubes)
        affected_cube_ids: set[str] = set()
        for cube in self.workspace_read_model.list_cube_dtos(include_system=True):
            if dim_id in cube.get("dimension_ids", []):
                affected_cube_ids.add(cube.get("id"))

        if not affected_cube_ids:
            return

        # Refresh all views for affected cubes (including system views)
        for view in self.workspace_read_model.list_view_dtos(include_system=True):
            if view.get("cube_id") in affected_cube_ids:
                # Clear cached data for this view
                self.session.execute("clear_cache", scope="cell")

        # Reload the active view if it uses an affected cube
        if self._active_view_id:
            try:
                active_view = self.workspace_read_model.get_view(self._active_view_id)
                if active_view and active_view.get("cube_id") in affected_cube_ids:
                    self._reload_active_view()
            except Exception:
                pass  # View may no longer exist

    def _current_cell_explain(self) -> tuple[str, str | None]:
        """Return (source, rule_expr) for the currently selected data cell.

        Works for both table and tree-table. If selection is not on a data cell, returns ("", None).
        """
        if isinstance(self._table, MatrixGrid):
            keys = self._table.selected_keys()
            if keys is None:
                return ("", None)
            row_key, col_key = keys
            cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
            explain = cell_dto.get("explain", {})
            if self._cell_error_text(cell_dto) is not None:
                return ("error", explain.get("rule_body"))
            return (explain.get("source", ""), explain.get("rule_body"))

        idx = self._table.currentIndex()  # type: ignore[union-attr]
        if not idx.isValid():
            return ("", None)

        tm = self._current_tab.tree_model
        if tm is not None:
            if idx.column() <= 0:
                return ("", None)
            row_key = tm.row_key_for_index(idx)
            col_key = tm.col_key_for_column(idx.column())
            if row_key is None or col_key is None:
                return ("", None)
            cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
            explain = cell_dto.get("explain", {})
            if self._cell_error_text(cell_dto) is not None:
                return ("error", explain.get("rule_body"))
            return (explain.get("source", ""), explain.get("rule_body"))

        # Phase E: Index-based reads removed. Model must provide key access.
        return ("", None)

    def _cell_error_text(self, cell: dict) -> str | None:
        explain = cell.get("explain", {})
        if not isinstance(explain, dict):
            return None
        canonical_codes = {"#VALUE!", "#NUM!", "#DIV/0!", "#CIRC!", "#EXPRESSION!", "#REF!", "#NAME!", "#N/A", "#SHAPE!", "#SYNTAX!"}
        if explain.get("source") == "error":
            err = explain.get("error")
            if isinstance(err, str) and err.strip():
                return err.strip()
            return "Error"
        # Some error paths (e.g. function-level normalization) store the
        # canonical code as the cell value while keeping source="rule".
        value = cell.get("value")
        if isinstance(value, str) and value in canonical_codes:
            return value
        return None

    def _selected_cell_addr_label(self) -> str | None:
        if self._table is None:
            return None

        view = self.workspace_read_model.get_view(self._active_view_id)
        cube = self.workspace_read_model.get_cube(view.get("cube_id")) if view else None

        addr: tuple[str, ...] | None = None
        if isinstance(self._table, MatrixGrid):
            keys = self._table.selected_keys()
            if keys is None:
                return None
            row_key, col_key = keys
            addr = self.cell_read_model.addr_for_view_keys(self._active_view_id, row_key, col_key)
        else:
            idx = self._table.currentIndex()  # type: ignore[union-attr]
            if not idx.isValid():
                return None
            tm = self._current_tab.tree_model
            if tm is not None:
                if idx.column() <= 0:
                    return None
                row_key = tm.row_key_for_index(idx)
                col_key = tm.col_key_for_column(idx.column())
                if row_key is None or col_key is None:
                    return None
                addr = self.cell_read_model.addr_for_view_keys(self._active_view_id, row_key, col_key)
            else:
                # Phase E: Index-based reads removed. Model must provide key access.
                return None

        if addr is None:
            return None

        label = self._addr_label_for_cube_addr(cube, addr)
        return label or None

    def _current_focus_description(
        self,
        widget: QtWidgets.QWidget | None = None,
    ) -> str | None:
        if widget is None:
            widget = QtWidgets.QApplication.focusWidget()
        if widget is not None and not isinstance(widget, QtWidgets.QWidget):
            widget = None
        # Use the focused widget's grid directly, not self._table
        # This ensures we read from the correct grid after reload
        grid = self._locate_matrix_grid(widget)
        if grid is not None:
            path = self._widget_path_parts(grid)
            label = grid.focus_location_description()
            DEBUG_GUI and print(f"DEBUG _current_focus_desc: grid_id={id(grid)} label={label}")
            breadcrumbs = " > ".join(path) if path else "MatrixGrid"
            if label:
                return f"{breadcrumbs} | {label}"
            return breadcrumbs
        tree = getattr(self._dock_browser, "tree_widget", None)
        node = widget
        while isinstance(tree, QtWidgets.QWidget) and node is not None:
            if node is tree:
                return self._dock_browser.focus_description()
            node = node.parentWidget()
        format_dock = getattr(self, "_dock_format", None)
        if isinstance(format_dock, FormatToolboxDock) and format_dock.contains_widget(widget):
            return format_dock.focus_description(widget)
        if widget is not None:
            return self._widget_path_description(widget)
        return None

    def _locate_matrix_grid(
        self,
        widget: QtWidgets.QWidget | None,
    ) -> MatrixGrid | None:
        node = widget
        while node is not None:
            if isinstance(node, MatrixGrid):
                return node
            node = node.parentWidget()
        return None

    def _on_rule_panel_edit_started(self) -> None:
        """Mark that a rule is being edited so the focus indicator reflects it."""
        self._rule_panel_editing = True
        self._update_focus_indicator()

    def _on_rule_panel_edit_ended(self) -> None:
        """Clear rule-edit mode, restore focus indicator, and return focus to grid."""
        self._rule_panel_editing = False
        self._update_focus_indicator()
        self._focus_active_window_grid()

    def _focus_active_window_grid(self) -> None:
        """Move focus to the active grid in whichever window is currently active."""
        active_window = QtWidgets.QApplication.activeWindow()
        if active_window is self:
            self._focus_active_grid()
        elif active_window is not None and hasattr(active_window, 'controller'):
            controller = active_window.controller
            if hasattr(controller, 'focus_active_grid'):
                controller.focus_active_grid()

    def _update_focus_indicator(
        self,
        widget: QtWidgets.QWidget | None = None,
    ) -> None:
        if getattr(self, '_rule_panel_editing', False):
            self._focus_indicator.setText("Focus: Rule Panel | editing rule")
            return
        desc = self._current_focus_description(widget)
        text = f"Focus: {desc}" if desc else "Focus: —"
        self._focus_indicator.setText(text)

    def _widget_path_description(self, widget: QtWidgets.QWidget) -> str:
        parts = self._widget_path_parts(widget)
        if parts:
            return " > ".join(parts)
        return widget.__class__.__name__

    def _widget_path_parts(self, widget: QtWidgets.QWidget | None) -> list[str]:
        if not isinstance(widget, QtWidgets.QWidget):
            return [self._pretty_widget_name(self)]
        parts: list[str] = []
        node: QtWidgets.QWidget | None = widget
        seen: set[int] = set()
        while node is not None and id(node) not in seen:
            seen.add(id(node))
            parts.append(self._pretty_widget_name(node))
            if node is self:
                break
            node = node.parentWidget()
        if parts and parts[-1] != self._pretty_widget_name(self):
            parts.append(self._pretty_widget_name(self))
        ordered = [p for p in reversed(parts) if p]
        return ordered

    def _pretty_widget_name(self, widget: QtWidgets.QWidget) -> str:
        if isinstance(widget, QtWidgets.QDockWidget):
            return widget.windowTitle() or "Dock"
        if isinstance(widget, MatrixGrid):
            return "MatrixGrid"
        if isinstance(widget, QtWidgets.QPushButton):
            return widget.text() or "Button"
        if isinstance(widget, QtWidgets.QToolButton):
            return widget.text() or "ToolButton"
        if isinstance(widget, QtWidgets.QLabel):
            return widget.text() or "Label"
        if isinstance(widget, QtWidgets.QLineEdit):
            placeholder = widget.placeholderText()
            return placeholder or "LineEdit"
        name = widget.accessibleName()
        if name:
            return name
        name = widget.objectName()
        if name:
            return name
        title = getattr(widget, "windowTitle", lambda: "")()
        if isinstance(title, str) and title.strip():
            return title
        parent = widget.parentWidget()
        if isinstance(parent, MatrixGrid) and parent.viewport() is widget:
            return "MatrixGrid.viewport"
        return widget.__class__.__name__

    def _selected_cell_cube_and_addr(self) -> tuple[str, tuple[str, ...]] | None:
        if self._table is None or not self._active_view_exists():
            return None

        view = self.workspace_read_model.get_view(self._active_view_id)
        if not view:
            return None
        cube_id = view.get("cube_id")

        addr: tuple[str, ...] | None = None
        if isinstance(self._table, MatrixGrid):
            keys = self._table.selected_keys()
            if keys is None:
                return None
            row_key, col_key = keys
            addr = self.cell_read_model.addr_for_view_keys(self._active_view_id, row_key, col_key)
        else:
            idx = self._table.currentIndex()  # type: ignore[union-attr]
            if not idx.isValid():
                return None
            tm = self._current_tab.tree_model
            if tm is not None:
                if idx.column() <= 0:
                    return None
                row_key = tm.row_key_for_index(idx)
                col_key = tm.col_key_for_column(idx.column())
                if row_key is None or col_key is None:
                    return None
                addr = self.cell_read_model.addr_for_view_keys(self._active_view_id, row_key, col_key)
            else:
                # Phase E: Index-based reads removed. Model must provide key access.
                return None

        if addr is None:
            return None
        return cube_id, addr

    def _sync_flow_panel_from_current(self) -> None:
        selected = self._selected_cell_cube_and_addr()
        if selected is None:
            self._flow_panel.set_focus_cell(None, None)
            self._circular_refs_panel.set_focus_cell(None, None)
            return
        cube_id, addr = selected
        # Only rebuild visible panels to avoid expensive trace operations
        current_tab = self._lower_tabs.currentIndex()
        if current_tab == 1:  # Calculation Flow tab
            self._flow_panel.set_focus_cell(cube_id, addr)
        elif current_tab == 2:  # Circular References tab
            self._circular_refs_panel.set_focus_cell(cube_id, addr)

    def _select_addr_in_current_view(self, addr: tuple[str, ...]) -> bool:
        print(f"[DEBUG app] _select_addr_in_current_view: addr={addr}")
        table = self._table
        if table is None or not self._active_view_exists():
            print("[DEBUG app]   -> table is None or no active view")
            return False

        view = self.workspace_read_model.get_view(self._active_view_id)
        if not view:
            print("[DEBUG app]   -> no view")
            return False
        cube = self.workspace_read_model.get_cube(view.get("cube_id"))
        if not cube:
            print("[DEBUG app]   -> no cube")
            return False
        dim_index = {did: i for i, did in enumerate(cube.get("dimension_ids", []))}
        row_key = tuple(addr[dim_index[did]] for did in view.get("row_dim_ids", []))
        col_key = tuple(addr[dim_index[did]] for did in view.get("col_dim_ids", []))
        print(f"[DEBUG app]   -> cube_dims={cube.get('dimension_ids')}, view_rows={view.get('row_dim_ids')}, view_cols={view.get('col_dim_ids')}, view_page={view.get('page_dim_ids')}")
        print(f"[DEBUG app]   -> row_key={row_key}, col_key={col_key}")

        if isinstance(table, MatrixGrid):
            print(f"[DEBUG app]   -> _row_keys[:5]={list(table._row_keys)[:5]}, _col_keys[:5]={list(table._col_keys)[:5]}")
            try:
                # _row_keys may contain lists (from JSON deserialization) instead of tuples
                leaf_index = next(i for i, k in enumerate(table._row_keys) if tuple(k) == row_key)
                col_index = next(i for i, k in enumerate(table._col_keys) if tuple(k) == col_key)
            except StopIteration as e:
                print(f"[DEBUG app]   -> row/col key not found: {e}")
                return False

            display_row: int | None = None
            for r in range(len(table._rows)):
                if not table._rows[r].get("is_leaf", False):
                    continue
                try:
                    if table._leaf_row_index(r) == leaf_index:
                        display_row = r
                        break
                except Exception:
                    continue
            if display_row is None:
                print("[DEBUG app]   -> display_row not found")
                return False

            table._sel_row = display_row
            table._sel_col = col_index
            table._sel_mode = "cell"
            table._sel_indices.clear()
            table._anchor_row, table._anchor_col = table._sel_row, table._sel_col
            table.selection_changed.emit()
            table.viewport().update()
            table.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
            print(f"[DEBUG app]   -> SUCCESS: row={display_row}, col={col_index}")
            return True

        tm = self._current_tab.tree_model
        if tm is not None:
            return False

        row_keys = self.grid_read_model.row_keys(self._active_view_id)
        col_keys = self.grid_read_model.col_keys(self._active_view_id)
        try:
            row_idx = row_keys.index(row_key)
            col_idx = col_keys.index(col_key)
        except ValueError:
            return False

        model = getattr(table, "model", lambda: None)()
        if model is None:
            return False
        idx = model.index(row_idx, col_idx)
        if not idx.isValid():
            return False
        table.setCurrentIndex(idx)
        table.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
        return True

    def _get_current_cell_selection(self) -> tuple[tuple[str, ...], tuple[str, ...]] | tuple[int, int] | None:
        """Capture current cell selection for restoration after reload.
        
        Returns row_key/col_key tuple for MatrixGrid, or (row, col) for regular tables.
        """
        table = self._table
        if table is None:
            return None
            
        if isinstance(table, MatrixGrid):
            return table.selected_keys()
        else:
            idx = table.currentIndex()
            if idx.isValid():
                return (idx.row(), idx.column())
        return None

    def _restore_cell_selection(self, selection: tuple[tuple[str, ...], tuple[str, ...]] | tuple[int, int]) -> None:
        """Restore cell selection after view reload."""
        table = self._table
        if table is None or not self._active_view_exists():
            return
            
        try:
            if isinstance(table, MatrixGrid):
                # selection is (row_key, col_key)
                row_key, col_key = selection
                self._select_addr_in_current_view(tuple(row_key) + tuple(col_key))
            else:
                # selection is (row, col)
                row, col = selection
                model = getattr(table, "model", lambda: None)()
                if model is not None:
                    idx = model.index(row, col)
                    if idx.isValid():
                        table.setCurrentIndex(idx)
        except Exception:
            pass  # Silently fail if cell no longer exists

    @QtCore.Slot(str, tuple)
    def _on_flow_navigate_requested(self, cube_id: str, addr: tuple[str, ...]) -> None:
        print(f"[DEBUG app] _on_flow_navigate_requested: cube_id={cube_id}, addr={addr}")
        view_id: str | None = None
        # Stay in current view if it's already showing the target cube
        if self._active_view_id is not None:
            current_view = self.workspace_read_model.get_view(self._active_view_id)
            if current_view and current_view.get("cube_id") == cube_id:
                view_id = self._active_view_id
        if view_id is None:
            for view in self.workspace_read_model.list_view_dtos():
                if view.get("cube_id") == cube_id:
                    view_id = view.get("id")
                    break

        if view_id is None:
            self._flash_status_message("No view found for traced cube")
            return

        if not self._workspace.focus_view(view_id):
            self._flash_status_message("Unable to focus traced view")
            return

        if not self._select_addr_in_current_view(addr):
            self._flash_status_message("Traced cell is not visible in current view axes")
            return

        self._sync_rule_bar_from_current()
        self._sync_flow_panel_from_current()

    @QtCore.Slot(str, tuple)
    def _on_open_trace_requested(self, cube_id: str, addr: tuple[str, ...]) -> None:
        self._on_flow_navigate_requested(cube_id, addr)
        flow_idx = self._lower_tabs.indexOf(self._flow_panel)
        if flow_idx >= 0:
            self._lower_tabs.setCurrentIndex(flow_idx)

    def _addr_label_for_cube_addr(self, cube: "Cube" | dict, addr: tuple[str, ...]) -> str:
        parts: list[str] = []
        dimension_ids = cube.get("dimension_ids") if isinstance(cube, dict) else getattr(cube, "dimension_ids", [])
        for dim_id, item_id in zip(dimension_ids, addr):
            dim_dto = None
            if self.gui_view_model is not None:
                dim_dto = self.gui_view_model.get_dimension_snapshot(dim_id)
            if not dim_dto and self.session is not None:
                dim_dto = self.session.query("dimension_detail", dim_id=dim_id)
                if dim_dto and self.gui_view_model is not None:
                    self.gui_view_model.update_dimension_snapshot(dim_id, dim_dto)
            if not dim_dto:
                parts.append(item_id)
                continue
            item_names = dict(zip(dim_dto.get("item_ids", []), dim_dto.get("item_names", [])))
            item_name = item_names.get(item_id, item_id)
            parts.append(f"{dim_dto.get('name', dim_id)}.{item_name}")

        addr_label = ", ".join(parts)
        cube_name = cube.get("name") if isinstance(cube, dict) else getattr(cube, "name", getattr(cube, "id", ""))
        if not cube_name:
            return addr_label
        if not addr_label:
            return cube_name
        # Include the cube name so error messages clearly identify which cube
        # the address belongs to.
        return f"{cube_name}: {addr_label}"

    def _first_error_detail(self) -> str | None:
        """Return a representative "<ERROR> @ Dim.Item, ..." string for the
        first error cell in the active view, or None if no detailed error is
        available.

        This is used when the view is (re)loaded and we know there is at least
        one error somewhere, but the user has not yet placed the selection on
        a specific error cell.
        """
        if self._recompute_error_detail is not None:
            return self._recompute_error_detail
        if not self._active_view_exists():
            return None
        try:
            if self._table is None:
                return None
        except RuntimeError:
            return None

        view = self.workspace_read_model.get_view(self._active_view_id)
        cube = self.workspace_read_model.get_cube(view.get("cube_id")) if view else None
        if not view or not cube:
            return None

        def _detail_for_addr(addr: tuple[str, ...], error_text: str | None) -> str:
            err = error_text or "Error"
            addr_label = self._addr_label_for_cube_addr(cube, addr)
            return f"{err} @ {addr_label}" if addr_label else err

        if isinstance(self._table, MatrixGrid):
            keys = self._table.selected_keys()
            if keys is None:
                return None
            row_key, col_key = keys
            cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
            err_text = self._cell_error_text(cell_dto)
            if err_text is not None:
                addr = cell_dto.get("addr", ())
                return _detail_for_addr(addr, err_text)
            return None

        tm = self._current_tab.tree_model
        if tm is not None:
            for row in range(tm.rowCount()):
                idx0 = tm.index(row, 0)
                row_key = tm.row_key_for_index(idx0)
                if row_key is None:
                    continue
                for col in range(1, tm.columnCount()):
                    col_key = tm.col_key_for_column(col)
                    if col_key is None:
                        continue
                    try:
                        cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
                    except Exception:
                        continue
                    err_text = self._cell_error_text(cell_dto)
                    if err_text is not None:
                        addr = cell_dto.get("addr", ())
                        return _detail_for_addr(addr, err_text)
            return None

        # Phase E: Index-based reads removed. Model must provide key access.
        return None

    def _on_toggle_debug_tooltips(self, checked: bool) -> None:
        """Toggle GUI debug tooltips for all MatrixGrid widgets."""
        from lib_gui_elements.matrix_grid import MatrixGrid
        for grid in self.findChildren(MatrixGrid):
            grid.set_debug_tooltips_enabled(checked)
        print(f"[DEBUG] GUI Debug Tooltips {'enabled' if checked else 'disabled'}")

    def _on_browser_selection_changed(self, current: QtWidgets.QTreeWidgetItem | None, previous: QtWidgets.QTreeWidgetItem | None) -> None:
        """Update InfoToolbox when selection changes in Model Browser."""
        DEBUG_GUI and print(f"[INFO TOOLBOX] selection changed: current={current}")
        if current is None:
            self._dock_info.clear()
            return
        
        data = current.data(0, QtCore.Qt.ItemDataRole.UserRole)
        DEBUG_GUI and print(f"[INFO TOOLBOX] data={data}")
        if not isinstance(data, tuple) or not data:
            self._dock_info.clear()
            return
        
        tag = data[0]
        payload = data[1:]
        DEBUG_GUI and print(f"[INFO TOOLBOX] tag={tag}, payload={payload}")
        
        if tag == "dim" and payload:
            self._dock_info.show_dimension(payload[0])
        elif tag == "dim_item" and len(payload) >= 2:
            self._dock_info.show_dimension_item(payload[0], payload[1])
        elif tag == "cube" and payload:
            self._dock_info.show_cube(payload[0])
        elif tag == "cube_dim" and len(payload) >= 2:
            self._dock_info.show_cube_dimension(payload[0], payload[1])
        elif tag == "view" and payload:
            self._dock_info.show_view(payload[0])
        else:
            self._dock_info.clear()

    @QtCore.Slot()
    def _on_cancel_requested(self) -> None:
        """Handle Esc key press to cancel long-running calculations or exit edit mode."""
        if getattr(self, '_recalculating', False) and self.session is not None:
            self.session.execute("cancel_recalculation")
            # Stop the background thread
            self._stop_recalc_thread()
            self._flash_status_message("Recalculation cancelled")
        else:
            # Cancel an active rule panel inline edit first.
            if hasattr(self, '_rule_panel') and self._rule_panel.is_editing():
                self._rule_panel.cancel_edit()
                return
            # Not recalculating - forward Esc to active grid to cancel editing
            grid = self._table
            if isinstance(grid, MatrixGrid) and grid._editor.isVisible():
                grid._cancel_edit()
                grid._edit_mode = "navigation"
                return

    def _freeze_ui_for_calculation(self) -> None:
        """Freeze all UI except Esc key during calculation."""
        # Disable all MatrixGrid widgets to prevent editing
        for grid in self.findChildren(QtWidgets.QWidget):
            if type(grid).__name__ == 'MatrixGrid':
                grid.setEnabled(False)
        # Disable rule bar and rule panel
        self._rule_bar.setEnabled(False)
        self._rule_panel.setEnabled(False)
        print("[GUI] UI frozen - grids, rule bar, rule panel disabled")
    
    def _thaw_ui_after_calculation(self) -> None:
        """Re-enable all UI after calculation completes."""
        # Re-enable all MatrixGrid widgets
        for grid in self.findChildren(QtWidgets.QWidget):
            if type(grid).__name__ == 'MatrixGrid':
                grid.setEnabled(True)
        # Re-enable rule bar and rule panel
        self._rule_bar.setEnabled(True)
        self._rule_panel.setEnabled(True)
        print("[GUI] UI thawed - all controls enabled")

    @QtCore.Slot()
    def _on_recalculate(self) -> None:
        """Start recalculation in background thread."""
        # Prevent recursive signal cascades during F9
        if self._recalculating:
            return
        
        # Stop any existing thread
        self._stop_recalc_thread()
        
        self._recalculating = True
        # Freeze UI - only Esc key works
        self._freeze_ui_for_calculation()

        # Create and start worker thread
        self._recalc_thread = QtCore.QThread(self)
        self._recalc_worker = RecalcWorker(self.session)
        self._recalc_worker.moveToThread(self._recalc_thread)

        # Connect signals
        self._recalc_thread.started.connect(self._recalc_worker.run)
        self._recalc_worker.finished.connect(self._on_recalc_finished)
        self._recalc_worker.error.connect(self._on_recalc_error)
        self._recalc_worker.finished.connect(self._recalc_thread.quit)
        self._recalc_worker.error.connect(self._recalc_thread.quit)
        # Do NOT connect finished to a cleanup slot.  A stale queued cleanup
        # could delete a newer thread created immediately after.  Threads are
        # cleaned up safely by _stop_recalc_thread on the next F9 press.

        # Start the thread
        self._recalc_thread.start()

    @QtCore.Slot()
    def _on_recalculate_visible(self) -> None:
        """Recalculate only the currently visible cells in the active view (Shift+F9).

        This is faster than F9 because it only marks visible cells as dirty,
        forcing re-evaluation only for cells currently in view.
        """
        if self.is_remote:
            # Visible-cell invalidation requires direct engine access.
            # A `recalculate_visible_cells` command is deferred to F6k follow-up.
            self._flash_status_message("F9 not supported in remote mode")
            return
        AT_VALUE = "at_value"

        if not isinstance(self._table, MatrixGrid):
            print("[F9] No active matrix grid")
            return

        if not self._active_view_id:
            print("[F9] No active view")
            return

        view = self.workspace_read_model.get_view(self._active_view_id)
        cube = self.workspace_read_model.get_cube(view.get("cube_id")) if view else None
        if not view or not cube:
            print(f"[F9] Cube not found: {view.get('cube_id') if view else None}")
            return

        grid = self._table
        row_keys = getattr(grid, '_row_keys', [])
        col_keys = getattr(grid, '_col_keys', [])

        if not row_keys or not col_keys:
            print("[F9] No visible cells to recalculate")
            return

        print(f"\n{'='*60}")
        print(f"[F9] Recalculating visible cells in view: {self._active_view_id}")
        print(f"[F9] Grid dimensions: {len(row_keys)} rows x {len(col_keys)} cols = {len(row_keys) * len(col_keys)} cells")
        print(f"{'='*60}")

        # Get page dimensions and their current items
        page_dim_ids = view.get("page_dim_ids", [])
        page_items = {}
        for dim_id in page_dim_ids:
            item_id = self.workspace_read_model.page_selection(view.get("id"), dim_id)
            if item_id:
                page_items[dim_id] = item_id

        # Build dimension index maps for row/col
        row_index = {dim_id: i for i, dim_id in enumerate(view.get("row_dim_ids", []))}
        col_index = {dim_id: i for i, dim_id in enumerate(view.get("col_dim_ids", []))}

        # Trigger incremental dirty-scope recalculation through command spine
        t0 = time.perf_counter()
        result = self.session.execute("run_recalculation", scope="dirty")
        t1 = time.perf_counter()
        invalidated_count = len(row_keys) * len(col_keys)
        print(f"[F9] Triggered dirty recalculation in {(t1-t0)*1000:.1f}ms")

        # Refresh the active view to trigger re-evaluation of visible cells
        grid.reload(invalidate_tiles="data")
        grid.update()

        # Update dependent UI panels
        self._sync_flow_panel_from_current()
        self._refresh_error_status(allow_from_computing=True)

        print(f"[Shift+F9] COMPLETED")
        print(f"{'='*60}\n")

    @QtCore.Slot(bool)
    def _on_recalc_finished(self, success: bool) -> None:
        """Handle successful recalculation completion."""
        logger.info("[MainWindow] recalc_finished success=%s", success)
        self._thaw_ui_after_calculation()
        if not success:
            self._recalculating = False
            return
        for controller in self._iter_workspace_controllers():
            controller.refresh_all_views()
            controller.rebuild_rule_panel()
        self._sync_flow_panel_from_current()
        self._refresh_error_status(allow_from_computing=True)
        self._mark_dirty(True)
        self._recalculating = False

    @QtCore.Slot(str)
    def _on_recalc_error(self, error_msg: str) -> None:
        """Handle recalculation error/cancellation."""
        self._thaw_ui_after_calculation()
        self._flash_status_message(error_msg)
        self._recalculating = False

    def _stop_recalc_thread(self, *, force: bool = False) -> None:
        """Stop the recalculation thread if running.

        Threads are no longer connected to a finished-cleanup slot; they
        are deleted here or by Qt parent cleanup on shutdown.

        Args:
            force: If True, terminate immediately; otherwise wait up to 1s.
        """
        if self._recalc_worker is not None:
            self._recalc_worker.request_cancel()

        thread = self._recalc_thread
        worker = self._recalc_worker

        if thread is not None:
            if thread.isRunning():
                thread.quit()
                wait_ms = 500 if force else 1000
                if not thread.wait(wait_ms):
                    print("[GUI] Force terminating recalculation thread")
                    thread.terminate()
                    thread.wait(500)
            if self._recalc_thread is thread:
                self._recalc_thread = None
            thread.deleteLater()

        if worker is not None:
            if self._recalc_worker is worker:
                self._recalc_worker = None
            worker.deleteLater()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Ensure background threads are stopped when closing."""
        self._is_closing = True
        # Stop stats worker thread if still running (it has no event loop,
        # so quit() is ineffective; wait() is the safe path).
        if getattr(self, '_stats_thread', None) is not None and self._stats_thread.isRunning():
            self._stats_thread.wait(300)
        self._stats_thread = None
        # Check if calculation is running
        if getattr(self, '_recalculating', False) and self._recalc_thread is not None and self._recalc_thread.isRunning():
            # Request cancel first
            if self._recalc_worker is not None:
                self._recalc_worker.request_cancel()
            # Try to stop thread gracefully
            self._recalc_thread.quit()
            self._recalc_thread.wait(100)  # Brief wait
            # Force kill the entire Python process - this stops all threads immediately
            import os
            os._exit(0)
        else:
            # Normal close - just stop thread if running
            print("[GUI] Normal window close")
            self._stop_recalc_thread()
        event.accept()

    def _on_active_view_changed(self, view_id: str) -> None:
        logger.info("[MainWindow] active_view_changed view=%s", view_id[:8])
        self._active_view_id = view_id
        self._sync_tile_fetch_signals()
        self._sync_flow_panel_from_current()
        self._update_selection_stats()

    def _flash_status_message(self, text: str) -> None:
        self.statusBar().showMessage(text, 1500)

    @QtCore.Slot(str, str)
    def _on_status_flash_requested(self, state: str, text: str) -> None:
        """Handle legacy two-argument status requests from workspace or event adapter."""
        self._flash_status_message(text)

    @QtCore.Slot(str, str, str)
    def _on_status_owner_request(self, action: str, owner: str, state: str) -> None:
        """Push, update, or pop a status owner from a thread-safe signal."""
        if action == "begin":
            if not self._status_manager.has(owner):
                self._status_manager.begin(owner, StatusState(state))
        elif action == "update":
            if self._status_manager.has(owner):
                self._status_manager.update(owner, StatusState(state))
        elif action == "end":
            if self._status_manager.has(owner):
                self._status_manager.end(owner)
        elif action == "sync":
            # Synchronise a single owner to an engine state string. Used by the
            # event.engine.state_changed bridge so that the StatusManager is
            # always touched on the GUI thread.
            logger.info("[StatusOwner] sync owner=%s to state=%s", owner, state)
            if state in ("idle", "shutting_down"):
                if self._status_manager.has(owner):
                    self._status_manager.end(owner)
                return
            mapped = {
                "mutating": StatusState.CALCULATING,
                "recalculating": StatusState.CALCULATING,
                "cancelling": StatusState.CANCELLING,
                "loading": StatusState.LOADING,
                "saving": StatusState.SAVING,
                "faulted": StatusState.ERROR,
            }.get(state)
            if mapped is None:
                return
            if self._status_manager.has(owner):
                self._status_manager.update(owner, mapped)
            else:
                self._status_manager.begin(owner, mapped)

    @QtCore.Slot(str, str)
    def _on_tile_fetch_started(self, view_id: str, reason: str) -> None:
        """Push a visible_refresh owner while the active grid fetches formatted tiles."""
        if reason in ("scroll", "prefetch"):
            return
        owner = StatusOwner.visible_refresh(view_id)
        state = StatusState.LOADING if reason == "initial" else StatusState.CALCULATING
        if not self._status_manager.has(owner):
            self._status_manager.begin(owner, state)

    @QtCore.Slot(str)
    def _on_tile_fetch_finished(self, view_id: str) -> None:
        """Pop the visible_refresh owner when the formatted tile thread finishes."""
        owner = StatusOwner.visible_refresh(view_id)
        if self._status_manager.has(owner):
            self._status_manager.end(owner)

    def _sync_tile_fetch_signals(self) -> None:
        """Connect tile fetch signals to the current active grid and disconnect from the previous one."""
        grid = self._table if isinstance(self._table, MatrixGrid) else None
        if grid is self._tile_fetch_grid:
            return
        if self._tile_fetch_grid is not None:
            try:
                self._tile_fetch_grid.tile_fetch_started.disconnect(self._on_tile_fetch_started)
            except RuntimeError:
                pass
            try:
                self._tile_fetch_grid.tile_fetch_finished.disconnect(self._on_tile_fetch_finished)
            except RuntimeError:
                pass
            # End any dangling visible_refresh owner for the previous grid's view.
            try:
                old_view_id = self._tile_fetch_grid._view_id
            except AttributeError:
                old_view_id = None
            if old_view_id:
                owner = StatusOwner.visible_refresh(old_view_id)
                if self._status_manager.has(owner):
                    self._status_manager.end(owner)
            self._tile_fetch_grid = None
        if grid is not None:
            grid.tile_fetch_started.connect(self._on_tile_fetch_started)
            grid.tile_fetch_finished.connect(self._on_tile_fetch_finished)
            self._tile_fetch_grid = grid

    def _on_status_changed(self, state: StatusState) -> None:
        """Render the current StatusState on the status indicator."""
        self._status_state = state.value
        logger.info("[StatusBar] state changed to %s", state.value)
        if self._status_indicator is None:
            return
        labels = {
            StatusState.WAITING_FOR_COMMAND: "Waiting for command",
            StatusState.LOADING: "Loading…",
            StatusState.SAVING: "Saving…",
            StatusState.CALCULATING: "Calculating…",
            StatusState.CANCELLING: "Cancelling…",
            StatusState.UPDATING: "Updating…",
            StatusState.ERROR: "Error",
        }
        label = labels[state]
        if state == StatusState.ERROR:
            detail = self._first_error_detail()
            if detail:
                label = detail
        icon = (
            "🟢"
            if state == StatusState.WAITING_FOR_COMMAND
            else "🟠"
            if state in (StatusState.LOADING, StatusState.SAVING, StatusState.CALCULATING)
            else "🔴"
        )
        try:
            text = f"{icon} {label}"
            self._status_indicator.setText(text)
            self._status_indicator.show()
            self._status_indicator.repaint()
            logger.debug("[StatusBar] indicator text=%r visible=%s", text, self._status_indicator.isVisible())
        except RuntimeError:
            return
        try:
            sb = self.statusBar()
            if isinstance(sb, QtWidgets.QStatusBar):
                sb.clearMessage()
        except RuntimeError:
            return

    def _next_status_owner_id(self) -> str:
        """Return a monotonic correlation ID for unique status owners."""
        self._status_owner_id += 1
        return str(self._status_owner_id)

    def _refresh_error_status(self, allow_from_computing: bool = False) -> None:
        """Push/pop the error_tracker owner based on whether any view has errors."""
        if self._status_manager.current() == StatusState.CALCULATING and not allow_from_computing:
            return
        if self._active_view_has_errors():
            if not self._status_manager.has(StatusOwner.error_tracker()):
                self._status_manager.begin(StatusOwner.error_tracker(), StatusState.ERROR)
            elif self._status_manager.current() == StatusState.ERROR:
                # Selection/viewport changed while already in ERROR; refresh label.
                self._on_status_changed(StatusState.ERROR)
        else:
            if self._status_manager.has(StatusOwner.error_tracker()):
                self._status_manager.end(StatusOwner.error_tracker())

    # Phase B: GUI event bus subscribers for ui.* events
    def _on_ui_status_update(self, event):
        """Handle ui.status.update events from the GUIEventAdapter."""
        message = event.payload.get("message", "") if hasattr(event, "payload") else ""
        self._flash_status_message(message)

    def _on_ui_refresh(self, event):
        """Handle ui.refresh events from the GUIEventAdapter.

        Marshals to the GUI thread because bus callbacks may arrive from
        worker or REPL threads; Qt widgets must be mutated on the GUI
        thread.  QueuedConnection ensures the slot runs on self's thread.
        """
        QtCore.QMetaObject.invokeMethod(
            self, "_do_ui_refresh", QtCore.Qt.QueuedConnection
        )

    def _is_gui_thread(self) -> bool:
        """Return True if the current thread is the object's owning GUI thread."""
        try:
            return QtCore.QThread.currentThread() == self.thread()
        except Exception:
            # Tests with mocked windows may not have a real QThread; fall back to
            # synchronous execution so unit tests still exercise the logic.
            return True

    @QtCore.Slot()
    def _do_ui_refresh(self):
        """Request debounced browser + view refresh.

        May be called from the bus/transport thread, so marshal to the GUI
        thread before touching Qt widgets or QTimer.
        """
        if not self._is_gui_thread():
            QtCore.QMetaObject.invokeMethod(
                self, "_do_ui_refresh", QtCore.Qt.QueuedConnection
            )
            return
        self._schedule_ui_refresh(needs_browser=True)

    def _on_ui_grid_refresh(self, event):
        """Handle ui.grid.refresh events from the GUIEventAdapter."""
        if not self._is_gui_thread():
            QtCore.QMetaObject.invokeMethod(
                self, "_on_ui_grid_refresh", QtCore.Qt.QueuedConnection
            )
            return
        self._schedule_ui_refresh(needs_browser=False)

    def refresh_gui(self) -> None:
        """Refresh the GUI view. Called by ui.* event handlers."""
        self._schedule_ui_refresh(needs_browser=False)

    def suspend_ui_refresh(self) -> None:
        """Suppress debounced UI refreshes until resume_ui_refresh is called.

        Used by batch operations (e.g. `source` command) that emit many events
        in a tight loop.  Each event would otherwise restart the debounce timer
        and trigger a full browser + view rebuild every 50 ms.
        """
        self._ui_refresh_suspended = True
        if self._ui_refresh_timer is not None:
            self._ui_refresh_timer.stop()

    def resume_ui_refresh(self) -> None:
        """Resume UI refresh and fire a single coalesced refresh if one is pending."""
        self._ui_refresh_suspended = False
        if self._ui_refresh_needs_browser:
            self._schedule_ui_refresh(needs_browser=True)

    def _schedule_ui_refresh(self, needs_browser: bool) -> None:
        """Coalesce rapid refresh requests into a single GUI update.

        When a burst of bus events arrives (e.g. a script creating many model
        objects), each event would otherwise rebuild the model browser and/or
        reload all views. We instead set a flag and restart a short debounce
        timer; the actual work happens once, when the timer fires.
        """
        self._ui_refresh_needs_browser |= needs_browser
        if self._ui_refresh_suspended:
            return
        if self._ui_refresh_timer is None:
            self._ui_refresh_timer = QtCore.QTimer(self)
            self._ui_refresh_timer.setSingleShot(True)
            self._ui_refresh_timer.timeout.connect(self._execute_ui_refresh)
        self._ui_refresh_timer.start(self._ui_refresh_interval_ms)

    @QtCore.Slot()
    def _execute_ui_refresh(self) -> None:
        """Perform the coalesced browser rebuild, view refresh, and rule panel rebuild."""
        needs_browser = self._ui_refresh_needs_browser
        self._ui_refresh_needs_browser = False
        logger.info("[MainWindow] execute_ui_refresh needs_browser=%s", needs_browser)
        t0 = time.perf_counter()
        with self._status_manager.hold(StatusOwner.ui_refresh(), StatusState.UPDATING):
            if needs_browser:
                try:
                    self._dock_browser.rebuild()
                except RuntimeError:
                    pass  # C++ object may have been destroyed (e.g., supplementary window closed)
            try:
                self._workspace.refresh_all_views()
            except Exception:
                pass
            for win in list(self._workspace_windows):
                try:
                    win.controller.refresh_all_views()
                except Exception:
                    pass
            try:
                self._rule_panel.rebuild()
            except Exception:
                pass
            for win in list(self._workspace_windows):
                try:
                    win.controller.rebuild_rule_panel()
                except Exception:
                    pass
        if self._dirty_recompute_complete_pending and self._dirty_recompute_owner is not None:
            logger.info("[DirtyRecalc] UI refresh ending kept-alive owner=%s", self._dirty_recompute_owner)
            self._status_manager.end(self._dirty_recompute_owner)
            self._dirty_recompute_owner = None
            self._dirty_recompute_complete_pending = False
        logger.info("[MainWindow] execute_ui_refresh done duration=%.3f ms", (time.perf_counter() - t0) * 1000)


    # Phase B.7: GUI event bus subscribers for workspace.* events
    def _on_workspace_dirty_changed(self, event):
        """Handle event.workspace.dirty_changed events from the bus."""
        is_dirty = event.payload.get("is_dirty", False) if hasattr(event, "payload") else False
        self._mark_dirty(is_dirty)
        if getattr(self, "gui_view_model", None) is not None:
            self.gui_view_model.set_dirty(is_dirty)

    def _on_workspace_loading_event(self, event):
        """Handle event.workspace.loading — push a LOADING owner."""
        correlation_id = getattr(event, "correlation_id", None)
        if not correlation_id:
            return
        owner = StatusOwner.load(correlation_id)
        self._status_owner_request.emit("begin", owner, StatusState.LOADING.value)

    def _on_workspace_loaded_event(self, event):
        """Handle event.workspace.loaded — end load owner and switch timeline session."""
        logger.info("[MainWindow] event workspace.loaded")
        correlation_id = getattr(event, "correlation_id", None)
        if correlation_id:
            owner = StatusOwner.load(correlation_id)
            self._status_owner_request.emit("end", owner, "")
        self.timeline_switch_requested.emit()

    def _on_workspace_load_failed_event(self, event):
        """Handle event.workspace.load_failed — end load owner and flash error."""
        correlation_id = getattr(event, "correlation_id", None)
        if correlation_id:
            owner = StatusOwner.load(correlation_id)
            self._status_owner_request.emit("end", owner, "")
        error = event.payload.get("error", "") if hasattr(event, "payload") else ""
        self._set_status_requested.emit("error", f"Load failed: {error}")

    def _on_workspace_saving_event(self, event):
        """Handle event.workspace.saving — push a SAVING owner."""
        correlation_id = getattr(event, "correlation_id", None)
        if not correlation_id:
            return
        owner = StatusOwner.save(correlation_id)
        self._status_owner_request.emit("begin", owner, StatusState.SAVING.value)

    def _on_workspace_saved_event(self, event):
        """Handle event.workspace.saved — end save owner."""
        correlation_id = getattr(event, "correlation_id", None)
        if not correlation_id:
            return
        owner = StatusOwner.save(correlation_id)
        self._status_owner_request.emit("end", owner, "")

    def _on_workspace_save_failed_event(self, event):
        """Handle event.workspace.save_failed — end save owner and flash error."""
        correlation_id = getattr(event, "correlation_id", None)
        if correlation_id:
            owner = StatusOwner.save(correlation_id)
            self._status_owner_request.emit("end", owner, "")
        error = event.payload.get("error", "") if hasattr(event, "payload") else ""
        self._set_status_requested.emit("error", f"Save failed: {error}")

    def _emit_status_owner_request(self, action: str, owner: str, state: str) -> None:
        """Safely emit a status-owner request, swallowing errors if the window is destroyed."""
        try:
            if not isValid(self):
                return
            self._status_owner_request.emit(action, owner, state)
        except RuntimeError:
            pass

    def _on_calculation_started_event(self, event):
        """Handle event.calculation.started — push a CALCULATING owner."""
        correlation_id = getattr(event, "correlation_id", None)
        if not correlation_id:
            return
        owner = StatusOwner.recalc(correlation_id)
        self._emit_status_owner_request("begin", owner, StatusState.CALCULATING.value)

    def _on_calculation_finished_event(self, event):
        """Handle event.calculation.finished — end recalc owner."""
        correlation_id = getattr(event, "correlation_id", None)
        if not correlation_id:
            return
        owner = StatusOwner.recalc(correlation_id)
        self._emit_status_owner_request("end", owner, "")

    def _on_checkpoint_created_event(self, event):
        """Handle event.workspace.checkpoint_created — emit signal so slot runs on GUI thread."""
        self.timeline_switch_requested.emit()

    def _on_checkpoint_restored_event(self, event):
        """Handle event.workspace.checkpoint_restored — emit signals so slots run on GUI thread."""
        logger.info("[MainWindow] event workspace.checkpoint_restored")
        self.tabs_rebuild_requested.emit()
        self.timeline_switch_requested.emit()

    def _on_engine_state_changed(self, event):
        """Handle event.engine.state_changed events from the bus.

        The actual StatusManager update is delegated to _status_owner_request so
        it always runs on the GUI thread, even when the event is published from
        a background worker.
        """
        payload = event.payload if hasattr(event, "payload") else {}
        new_state = payload.get("new_state", "idle")
        logger.info("[EngineState] event.engine.state_changed -> %s", new_state)
        self._emit_status_owner_request("sync", StatusOwner.engine(), new_state)

    def _on_profiler_start(self, event: Any) -> None:
        """Handle event.profiler.start — marshal to GUI thread via signal."""
        payload = event.payload if hasattr(event, "payload") else event
        if not isinstance(payload, dict):
            logging.warning("[profiler] Ignoring non-dict profiler start payload: %r", payload)
            return
        logging.warning("[profiler] Received start request for endpoint=%s", payload.get("endpoint_id"))
        self._profiler_start_requested.emit(payload)

    def _do_profiler_start(self, event: dict) -> None:
        """Start profiling on the GUI thread, stop after duration, report."""
        endpoint_id = event.get("endpoint_id")
        logging.warning("[profiler] GUI start handler endpoint=%s expected=%s", endpoint_id, self.profiler.endpoint_id)
        if endpoint_id != self.profiler.endpoint_id:
            return
        request_id = event.get("request_id")
        if request_id is None:
            logging.warning("[profiler] Start request missing request_id")
            return
        duration_seconds = event.get("duration_seconds", 0.0)
        if self.profiler.is_active():
            logging.warning("[profiler] Restarting profiling for new request (request_id=%s)", request_id)
            self.profiler.stop_profiling()
        else:
            logging.warning("[profiler] Starting profiling for %.2fs (request_id=%s)", duration_seconds, request_id)
        self.profiler.start_profiling()
        QtCore.QTimer.singleShot(
            max(0, int(duration_seconds * 1000)),
            lambda: self._stop_profiler_and_report(request_id),
        )

    def _stop_profiler_and_report(self, request_id: str) -> None:
        """Stop profiling and deliver the snapshot to the waiting command."""
        logging.warning("[profiler] Stopping profiling and reporting (request_id=%s)", request_id)
        self.profiler.stop_profiling()
        snapshot = self.profiler.snapshot()
        logging.warning("[profiler] Snapshot has %d span(s)", len(snapshot))
        try:
            self.session.execute("profiler_report", request_id=request_id, snapshot=snapshot)
            logging.warning("[profiler] Report delivered")
        except Exception as exc:
            logging.warning("[profiler] Failed to deliver profiler report: %s", exc)

    def _on_dimension_renamed_event(self, event):
        """Handle event.dimension.renamed — emit signal so slot runs on GUI thread."""
        logger.info("[MainWindow] event dimension.renamed")
        self.dimension_renamed_requested.emit()

    def _on_dimension_item_renamed_event(self, event):
        """Handle event.dimension_item.renamed — emit signal so slot runs on GUI thread."""
        logger.info("[MainWindow] event dimension_item.renamed")
        self.dimension_item_renamed_requested.emit()

    def _on_dimension_structure_changed_event(self, event) -> None:
        """Handle event.dimension.structure_changed — debounce browser rebuild."""
        logger.debug("[MainWindow] event dimension.structure_changed")
        self._schedule_browser_rebuild()

    def _on_view_created_event(self, event):
        """Handle event.view.created — emit signal so slot runs on GUI thread."""
        view_id = event.payload.get("view_id") if hasattr(event, "payload") else None
        logger.info("[MainWindow] event view.created view=%s", view_id[:8] if view_id else None)
        if view_id:
            self.view_tab_requested.emit(view_id)
            self._schedule_browser_rebuild()

    def _do_add_view_tab(self, view_id: str) -> None:
        try:
            self._workspace.add_view_tab(view_id)
        except Exception:
            logging.exception("[_do_add_view_tab] failed for %s", view_id[:8])
        self._sync_tile_fetch_signals()

    def _on_view_activated_event(self, event):
        """Handle event.view.activated — emit signal so slot runs on GUI thread."""
        view_id = event.payload.get("view_id") if hasattr(event, "payload") else None
        if view_id:
            self.view_activation_requested.emit(view_id)

    def _do_activate_view(self, view_id: str) -> None:
        try:
            self._workspace.activate_view(view_id)
        except Exception:
            logging.exception("[_do_activate_view] failed for %s", view_id[:8])

    @QtCore.Slot()
    def _schedule_browser_rebuild(self) -> None:
        """Debounce model browser rebuilds to coalesce event bursts."""
        if not self._is_gui_thread():
            QtCore.QTimer.singleShot(0, self._schedule_browser_rebuild)
            return
        if self._browser_rebuild_timer is None:
            self._browser_rebuild_timer = QtCore.QTimer(self)
            self._browser_rebuild_timer.setSingleShot(True)
            self._browser_rebuild_timer.timeout.connect(self._do_rebuild_model_browser)
        self._browser_rebuild_timer.start(self._browser_rebuild_interval_ms)

    def _do_rebuild_model_browser(self) -> None:
        try:
            self._dock_browser.rebuild()
        except RuntimeError:
            pass

    def _do_dimension_renamed(self) -> None:
        """Slot: rebuild rule panels and model browser after a dimension rename (GUI thread)."""
        try:
            self._on_dimension_renamed()
        except Exception:
            logging.exception("[_do_dimension_renamed] failed")

    def _do_dimension_item_renamed(self) -> None:
        """Slot: rebuild rule panels after a dimension item rename (GUI thread)."""
        try:
            self._on_dimension_item_renamed()
        except Exception:
            logging.exception("[_do_dimension_item_renamed] failed")

    def _do_switch_timeline_session(self) -> None:
        """Slot: switch timeline to the current workspace session file (GUI thread)."""
        try:
            self._switch_timeline_to_workspace_session()
        except Exception:
            logging.exception("[_do_switch_timeline_session] failed")

    def _on_view_deleted_event(self, event) -> None:
        """Handle event.view.deleted — emit signal so slot runs on GUI thread."""
        self.tabs_rebuild_requested.emit()

    def _on_cube_deleted_event(self, event) -> None:
        """Handle event.cube.deleted — emit signal so slot runs on GUI thread."""
        self.tabs_rebuild_requested.emit()

    def _on_cube_created_event(self, event) -> None:
        """Handle event.cube.created — emit signal so slot runs on GUI thread."""
        self.model_browser_rebuild_requested.emit()

    def _on_dimension_created_event(self, event) -> None:
        """Handle event.dimension.created — emit signal so slot runs on GUI thread."""
        self.model_browser_rebuild_requested.emit()

    def _on_dimension_deleted_event(self, event) -> None:
        """Handle event.dimension.deleted — emit signal so slot runs on GUI thread."""
        self.model_browser_rebuild_requested.emit()

    def _on_dimension_item_created_event(self, event) -> None:
        """Handle event.dimension_item.created — debounce browser rebuild."""
        self._schedule_browser_rebuild()

    def _on_dimension_item_deleted_event(self, event) -> None:
        """Handle event.dimension_item.deleted — debounce browser rebuild."""
        self._schedule_browser_rebuild()

    def _on_selection_changed_event(self, event) -> None:
        """Handle event.selection.changed — emit signal so slot runs on GUI thread."""
        payload = event.payload if hasattr(event, "payload") else {}
        cursor = payload.get("cursor", (0, 0))
        anchor = payload.get("anchor", (0, 0))
        self._pending_selection_event_view_id = payload.get("view_id")
        self.selection_changed_requested.emit(cursor[0], cursor[1], anchor[0], anchor[1])

    def _do_rebuild_tabs(self) -> None:
        """Rebuild tabs and model browser on GUI thread after view/cube deletion."""
        try:
            self._workspace.rebuild_tabs()
        except Exception:
            logging.exception("[_do_rebuild_tabs] failed")
        self._sync_tile_fetch_signals()
        try:
            self._dock_browser.rebuild()
        except RuntimeError:
            pass

    def _do_update_selection(self, row: int, col: int, anchor_row: int, anchor_col: int) -> None:
        """Update active grid selection on GUI thread after remote selection change.
        Skips if the grid already has the same selection (avoids redundant query+repaint
        when the grid itself initiated the change)."""
        try:
            event_view_id = getattr(self, '_pending_selection_event_view_id', None)
            self._pending_selection_event_view_id = None
            table = self._table
            if table is None or not hasattr(table, "_apply_session_selection"):
                return
            if event_view_id is not None and event_view_id != self._active_view_id:
                return
            # Skip redundant update when selection already matches local cache
            if (getattr(table, "_sel_row", None) == row and
                getattr(table, "_sel_col", None) == col and
                getattr(table, "_anchor_row", None) == anchor_row and
                getattr(table, "_anchor_col", None) == anchor_col):
                return
            # Read full selection state (mode + selected_indices) from SessionStore;
            # the manual single-cell reset that was here discarded multi-selection.
            table._apply_session_selection()
            table.selection_changed.emit()
            table.viewport().update()
            self._update_selection_stats()
        except Exception:
            logging.exception("[_do_update_selection] failed")

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        """Handle key press events, including F9 and Esc."""
        # F9 - Recalculate (direct capture at window level)
        if event.key() == QtCore.Qt.Key.Key_F9:
            event.accept()
            self._on_recalculate()
            return
        # Esc - Cancel recalculation
        if event.key() == QtCore.Qt.Key.Key_Escape:
            if getattr(self, '_recalculating', False) and self.session is not None:
                self.session.execute("cancel_recalculation")
                self._flash_status_message("Recalculation cancelled")
                event.accept()
                return
        super().keyPressEvent(event)

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:  # type: ignore[override]
        """Application-level event filter - intercepts F9 and blocks events during recalc."""
        # Intercept F9 at the earliest possible level (before child widgets see it)
        if event.type() in (
            QtCore.QEvent.Type.KeyPress,
            QtCore.QEvent.Type.ShortcutOverride,
        ):
            key_event = QtGui.QKeyEvent(event)  # type: ignore[arg-type]
            if key_event.key() == QtCore.Qt.Key.Key_F9:
                # F9 always triggers recalculation, regardless of focus
                self._on_recalculate()
                return True  # Event handled, stop propagation
            # Cancel an active rule panel edit before any child widget (grid
            # editor, line edit, etc.) swallows the Escape key. This must work
            # in both the main window and any secondary workspace window.
            if key_event.key() == QtCore.Qt.Key.Key_Escape:
                from lib_gui.rule_panel import RulePanel
                active_window = QtWidgets.QApplication.activeWindow()
                rule_panel = None
                if active_window is not None:
                    rule_panel = active_window.findChild(RulePanel)
                if rule_panel is None and hasattr(self, '_rule_panel'):
                    rule_panel = self._rule_panel
                if rule_panel is not None and rule_panel.is_editing():
                    rule_panel.cancel_edit()
                    return True
        
        # When recalculating, block ALL events except Esc
        if self._recalculating:
            # Check for Esc key - handle it directly
            if event.type() == QtCore.QEvent.Type.KeyPress:
                key_event = QtGui.QKeyEvent(event)  # type: ignore[arg-type]
                if key_event.key() == QtCore.Qt.Key.Key_Escape:
                    self._on_cancel_requested()
                    return True  # Event handled
            # Block all other key events
            if event.type() in (QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.KeyRelease):
                return True  # Block all other keys
            # Block mouse events
            if event.type() in (QtCore.QEvent.Type.MouseButtonPress, 
                                QtCore.QEvent.Type.MouseButtonRelease,
                                QtCore.QEvent.Type.MouseButtonDblClick,
                                QtCore.QEvent.Type.MouseMove,
                                QtCore.QEvent.Type.Wheel):
                return True  # Block mouse
            # Block focus changes
            if event.type() == QtCore.QEvent.Type.FocusIn:
                return True  # Block focus changes
            # Let other events through (paint, timer, etc.)
            
        # Handle rule bar key events when NOT recalculating
        if obj is self._rule_bar and event.type() == QtCore.QEvent.Type.KeyPress and not self._recalculating:
            key_event = QtGui.QKeyEvent(event)  # type: ignore[arg-type]
            if key_event.key() == QtCore.Qt.Key.Key_Delete:
                self._delete_current_rule()
                return True
            if key_event.key() == QtCore.Qt.Key.Key_Escape:
                self._focus_active_grid()
                return True
        return super().eventFilter(obj, event)

    def _on_dimension_item_renamed(self) -> None:
        """Rebuild all rule panels when dimension item names change.
        
        This ensures item name updates are reflected in rule addresses
        across all workspace windows.
        """
        # Rebuild main window rule panel
        self._rule_panel.rebuild()
        # Rebuild all secondary window rule panels
        for controller in self._iter_workspace_controllers():
            controller.rebuild_rule_panel()

    def _on_dimension_renamed(self) -> None:
        """Rebuild all rule panels when dimension names change.
        
        This ensures dimension name updates are reflected in rule expressions
        (e.g., Dim.Item references) across all workspace windows.
        """
        # Rebuild main window rule panel
        self._rule_panel.rebuild()
        # Rebuild all secondary window rule panels
        for controller in self._iter_workspace_controllers():
            controller.rebuild_rule_panel()
        # Also rebuild the model browser to show updated dimension names
        self._dock_browser.rebuild()
        # Mark workspace as dirty
        self._mark_dirty(True)

    def _on_rules_changed(self) -> None:
        self._rule_panel.rebuild()
        for controller in self._iter_workspace_controllers():
            controller.rebuild_rule_panel()
        self._after_mutation_enqueue_recompute()
        self._sync_rule_bar_from_current()
        self._sync_flow_panel_from_current()
        self._update_undo_redo_actions()
        self._refresh_error_status(allow_from_computing=True)
        # Mark as dirty when rules change
        self._mark_dirty(True)

    def _delete_current_rule(self) -> None:
        owner = StatusOwner.recalc(self._next_status_owner_id())
        self._status_manager.begin(owner, StatusState.CALCULATING)
        try:
            removed = False
            if isinstance(self._table, MatrixGrid):
                keys = self._table.selected_keys()
                if keys is None:
                    return
                row_key, col_key = keys
                result = self.session.execute(
                    "delete_rule_anchored",
                    view_id=self._active_view_id,
                    cell_ref={
                        "kind": "ids",
                        "row_key": row_key,
                        "col_key": col_key,
                    },
                )
                removed = result.success
            else:
                # Phase E: Index-based mutations removed. Model must provide key access.
                return

            if removed:
                self._on_rules_changed()
            else:
                self._refresh_error_status(allow_from_computing=True)
        finally:
            self._status_manager.end(owner)

    @QtCore.Slot()
    def _on_clear_override(self) -> None:
        if isinstance(self._table, MatrixGrid):
            keys = self._table.selected_keys()
            if keys is None:
                return
            row_key, col_key = keys
            cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
            if cell_dto.get("explain", {}).get("source") != "override":
                return
            self.session.execute(
                "clear_cell_hardvalue",
                view_id=self._active_view_id,
                cell_ref={
                    "kind": "keys",
                    "value": {"row_key": list(row_key), "col_key": list(col_key)},
                },
            )
            self._table.reload(invalidate_tiles="data")
            self._update_undo_redo_actions()
            self._sync_rule_bar_from_current()
            self._on_rules_changed()
            self._refresh_error_status()
            return

        idx = self._table.currentIndex()  # type: ignore[union-attr]
        if not idx.isValid():
            return
        tm = self._current_tab.tree_model
        if tm is not None:
            if idx.column() <= 0:
                return
            row_key = tm.row_key_for_index(idx)
            col_key = tm.col_key_for_column(idx.column())
            if row_key is None or col_key is None:
                return
            cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
            if cell_dto.get("explain", {}).get("source") != "override":
                return
            self.session.execute(
                "clear_cell_hardvalue",
                view_id=self._active_view_id,
                cell_ref={
                    "kind": "keys",
                    "value": {"row_key": list(row_key), "col_key": list(col_key)},
                },
            )
            self._current_tab.model.dataChanged.emit(idx, idx, [QtCore.Qt.ItemDataRole.DisplayRole])
        else:
            # Phase E: Index-based reads/mutations removed. Model must provide key access.
            return
        self._update_undo_redo_actions()
        self._sync_rule_bar_from_current()
        self._on_rules_changed()
        self._refresh_error_status()

    def _update_undo_redo_actions(self) -> None:
        """Update undo/redo action state and show descriptions."""
        state = self.session.query("undo_state") or {}
        can_undo = state.get("can_undo", False)
        can_redo = state.get("can_redo", False)
        undo_desc = state.get("undo_description")
        redo_desc = state.get("redo_description")
        
        self._actions.act_undo.setEnabled(can_undo)
        self._actions.act_undo.setText(f"Undo {undo_desc}" if undo_desc else "Undo")
        
        self._actions.act_redo.setEnabled(can_redo)
        self._actions.act_redo.setText(f"Redo {redo_desc}" if redo_desc else "Redo")

    def _update_copy_paste_actions(self) -> None:
        if not self._workspace.has_tabs:
            return
        if isinstance(self._table, MatrixGrid):
            self._actions.act_copy.setEnabled(False)
            self._actions.act_paste.setEnabled(False)
            self._actions.act_paste_as_new_cube.setEnabled(bool(QtWidgets.QApplication.clipboard().text()))
            # Use engine-free eligibility checks; the actions themselves read
            # cells when invoked, but enabling/disabling them must not block the
            # main thread (especially during slow engine operations).
            self._actions.act_convert_selection_to_dimension_labels.setEnabled(self._matrix_selection_is_rectangular())
            self._actions.act_assign_item_labels_from_selection.setEnabled(self._matrix_selection_is_axis_assignment_eligible())
            self._actions.act_delete_selected_dimension_items.setEnabled(self._matrix_selected_dimension_items() is not None)
            return
        sm = self._table.selectionModel()  # type: ignore[union-attr]
        has_selection = sm is not None and len(sm.selectedIndexes()) > 0
        self._actions.act_copy.setEnabled(has_selection)
        self._actions.act_paste.setEnabled(self._table.currentIndex().isValid())  # type: ignore[union-attr]
        self._actions.act_paste_as_new_cube.setEnabled(bool(QtWidgets.QApplication.clipboard().text()))
        self._actions.act_convert_selection_to_dimension_labels.setEnabled(False)
        self._actions.act_assign_item_labels_from_selection.setEnabled(False)
        self._actions.act_delete_selected_dimension_items.setEnabled(False)

    def _on_selection_stat_toggled(self, key: str, checked: bool) -> None:
        actions = getattr(self, "_selection_stats_actions", None)
        if not isinstance(actions, dict):
            return

        if key == "none" and checked:
            for k, a in actions.items():
                if k != "none":
                    a.blockSignals(True)
                    a.setChecked(False)
                    a.blockSignals(False)
        elif key != "none" and checked:
            none_action = actions.get("none")
            if none_action is not None and none_action.isChecked():
                none_action.blockSignals(True)
                none_action.setChecked(False)
                none_action.blockSignals(False)

        self._update_selection_stats()

    def _on_selection_stats_format_toggled(self, checked: bool) -> None:
        self._selection_stats_use_scientific = checked
        self._update_selection_stats()

    def _coerce_numeric_for_stats(self, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return None
        return None

    def _start_stats_spinner(self) -> None:
        """Show a crawling spinner while stats are computing."""
        if getattr(self, "_stats_spinner_running", False):
            return
        self._stats_spinner_running = True
        self._stats_spinner_index = -1
        self._tick_stats_spinner()

    def _stop_stats_spinner(self) -> None:
        """Stop the spinner chain."""
        self._stats_spinner_running = False

    def _tick_stats_spinner(self) -> None:
        """Advance the spinner one frame and schedule the next tick."""
        if not getattr(self, "_stats_spinner_running", False):
            return
        frames = self._stats_spinner_frames
        if not frames:
            return
        self._stats_spinner_index = (self._stats_spinner_index + 1) % len(frames)
        self._selection_stats_button.setText(
            self._build_spinner_text(frames[self._stats_spinner_index])
        )
        self._selection_stats_button.repaint()
        QtCore.QTimer.singleShot(200, self._tick_stats_spinner)

    def _tick_test_spinner(self) -> None:
        """Advance the test spinner and schedule the next tick (never stops)."""
        frames = self._test_spinner_frames
        if not frames:
            return
        self._test_spinner_index = (self._test_spinner_index + 1) % len(frames)
        self._test_spinner_label.setText(frames[self._test_spinner_index])
        self._test_spinner_label.repaint()
        QtCore.QTimer.singleShot(200, self._tick_test_spinner)

    def _update_selection_stats(self) -> None:
        """Debounced entry: restart the timer instead of starting work immediately.

        This prevents a flood of thread creation during drag selection,
        which blocks the Qt main rendering thread.
        """
        self._stats_debounce_timer.stop()
        self._stats_debounce_timer.start(100)

    def _do_update_selection_stats(self) -> None:
        """Actual stats computation, called after debounce delay."""
        view_id = self._active_view_id
        if view_id is None and isinstance(self._table, MatrixGrid):
            view_id = self._table._view_id
        if view_id is None or self._table is None:
            self._stop_stats_spinner()
            self._render_stats_from_dto({
                "total_count": 0, "count": 0, "counta": 0,
                "sum": 0.0, "avg": 0.0, "min": None, "max": None,
            })
            return

        # Show spinner immediately so the user sees activity while the
        # background thread builds the payload and runs the query.
        self._start_stats_spinner()

        actions = self._selection_stats_actions
        if actions.get("none") is not None and actions["none"].isChecked():
            self._stop_stats_spinner()
            self._selection_stats_button.setText("")
            return

        # Everything else — payload build + engine query — runs off the
        # main thread so the GUI stays responsive and the spinner animates.
        self._stats_generation += 1
        gen = self._stats_generation
        if self._stats_thread is not None and self._stats_thread.isRunning():
            # A query is already in flight; remember that the selection changed
            # and start a fresh one when it finishes. This prevents multiple
            # StatsWorkerThread objects from piling up while the engine is busy.
            self._stats_pending = True
            return

        thread = StatsWorkerThread(self, view_id, gen, None)
        thread.result_ready.connect(self._on_stats_result)
        thread.finished.connect(self._on_stats_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self._stats_thread = thread

    @QtCore.Slot()
    def _on_stats_thread_finished(self) -> None:
        """Clean up the stats worker thread and restart if a new request queued."""
        thread = self.sender()
        if self._stats_thread is thread:
            self._stats_thread = None
        if isinstance(thread, QtCore.QThread):
            thread.deleteLater()
        if self._stats_pending:
            self._stats_pending = False
            self._do_update_selection_stats()

    def _on_stats_result(self, envelope: dict) -> None:
        """Receive result from StatsQueryThread and render if still current."""
        generation = envelope.get("_generation", 0)
        result = envelope.get("result", {})
        if generation != self._stats_generation:
            return  # stale result – selection changed while query was in flight
        self._stop_stats_spinner()
        if result:
            self._render_stats_from_dto(result)
        else:
            self._render_stats_from_dto({
                "total_count": 0, "count": 0, "counta": 0,
                "sum": 0.0, "avg": 0.0, "min": None, "max": None,
            })

    def _build_selection_stats_payload(self) -> dict | None:
        """Build payload for selection_stats query from current grid selection."""
        if not isinstance(self._table, MatrixGrid):
            return None
        table = self._table
        mode = table._sel_mode
        page_selections = table._build_page_selections()
        payload: dict[str, Any] = {
            "view_id": self._active_view_id,
            "mode": mode,
            "page_selections": page_selections,
        }
        # Cap the number of keys we send to the engine so the GUI main thread
        # never blocks for long on massive selections.
        MAX_PAYLOAD_KEYS = _gui_cfg.SELECTION_STATS_MAX_PAYLOAD_KEYS
        # Process pending events every N iterations so the spinner timer
        # gets a chance to fire during long payload builds.
        _PROCESS_EVERY = 500
        _proc = 0
        if mode == "cell":
            cell_keys = []
            seen = 0
            for r, c in table._iter_selected_cell_coords():
                if not (0 <= r < len(table._rows)) or not (0 <= c < len(table._cols)):
                    continue
                if not table._rows[r].get("is_leaf", False):
                    continue
                leaf_i = table._leaf_row_index(r)
                if not (0 <= leaf_i < len(table._row_keys)):
                    continue
                if not (0 <= c < len(table._col_keys)):
                    continue
                cell_keys.append((table._row_keys[leaf_i], table._col_keys[c]))
                seen += 1
                _proc += 1
                if _proc >= _PROCESS_EVERY:
                    _proc = 0
                    QtWidgets.QApplication.processEvents(
                        QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
                    )
                if seen >= MAX_PAYLOAD_KEYS:
                    print(f"selection limit of {MAX_PAYLOAD_KEYS} exceeded")
                    break
            payload["cell_keys"] = cell_keys
        elif mode == "row":
            row_keys = []
            for r in table._sel_indices:
                if not (0 <= r < len(table._rows)):
                    continue
                if not table._rows[r].get("is_leaf", False):
                    continue
                leaf_i = table._leaf_row_index(r)
                if not (0 <= leaf_i < len(table._row_keys)):
                    continue
                row_keys.append(table._row_keys[leaf_i])
                _proc += 1
                if _proc >= _PROCESS_EVERY:
                    _proc = 0
                    QtWidgets.QApplication.processEvents(
                        QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
                    )
                if len(row_keys) >= MAX_PAYLOAD_KEYS:
                    print(f"selection limit of {MAX_PAYLOAD_KEYS} exceeded")
                    break
            payload["row_keys"] = row_keys
        elif mode == "col":
            col_keys = []
            for c in table._sel_indices:
                if not (0 <= c < len(table._cols)):
                    continue
                col_keys.append(table._col_keys[c])
                _proc += 1
                if _proc >= _PROCESS_EVERY:
                    _proc = 0
                    QtWidgets.QApplication.processEvents(
                        QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
                    )
                if len(col_keys) >= MAX_PAYLOAD_KEYS:
                    print(f"selection limit of {MAX_PAYLOAD_KEYS} exceeded")
                    break
            payload["col_keys"] = col_keys
        # all mode: no keys needed
        return payload

    def _build_spinner_text(self, frame: str) -> str:
        """Build status text with spinner characters in place of numeric values."""
        actions = self._selection_stats_actions
        if actions.get("none") is not None and actions["none"].isChecked():
            return ""
        chunks: list[str] = []
        if actions.get("selcount") is not None and actions["selcount"].isChecked():
            chunks.append(f"Selection count: {frame}")
        if actions.get("count") is not None and actions["count"].isChecked():
            chunks.append(f"Count: {frame}")
        if actions.get("counta") is not None and actions["counta"].isChecked():
            chunks.append(f"CountA: {frame}")
        if actions.get("sum") is not None and actions["sum"].isChecked():
            chunks.append(f"Sum: {frame}")
        if actions.get("avg") is not None and actions["avg"].isChecked():
            chunks.append(f"Average: {frame}")
        if actions.get("min") is not None and actions["min"].isChecked():
            chunks.append(f"Min: {frame}")
        if actions.get("max") is not None and actions["max"].isChecked():
            chunks.append(f"Max: {frame}")
        return "  ".join(chunks)

    def _render_stats_from_dto(self, stats: dict) -> None:
        """Render stats DTO returned by selection_stats query."""
        actions = self._selection_stats_actions
        if actions.get("none") is not None and actions["none"].isChecked():
            self._selection_stats_button.setText("")
            return

        def _fmt(v: float | None) -> str:
            if v is None:
                return "0"
            if self._selection_stats_use_scientific:
                return f"{v:.6e}"
            if float(v).is_integer():
                return str(int(v))
            return f"{v:.6f}".rstrip("0").rstrip(".")

        chunks: list[str] = []
        if actions.get("selcount") is not None and actions["selcount"].isChecked():
            chunks.append(f"Selection count: {stats.get('total_count', 0)}")
        if actions.get("count") is not None and actions["count"].isChecked():
            chunks.append(f"Count: {stats['count']}")
        if actions.get("counta") is not None and actions["counta"].isChecked():
            chunks.append(f"CountA: {stats['counta']}")
        if actions.get("sum") is not None and actions["sum"].isChecked():
            chunks.append(f"Sum: {_fmt(stats['sum'])}")
        if actions.get("avg") is not None and actions["avg"].isChecked():
            chunks.append(f"Average: {_fmt(stats['avg'])}")
        if actions.get("min") is not None and actions["min"].isChecked():
            chunks.append(f"Min: {_fmt(stats['min'])}")
        if actions.get("max") is not None and actions["max"].isChecked():
            chunks.append(f"Max: {_fmt(stats['max'])}")

        self._selection_stats_button.setText("  ".join(chunks))

    def _active_view_has_errors(self) -> bool:
        return self._first_error_detail() is not None

    def _connect_table_signals(self) -> None:
        self._workspace.connect_table_signals()

    def _focus_active_grid(self) -> None:
        self._workspace.focus_active_grid()

    def _on_matrix_content_changed(self) -> None:
        # Note: recompute and view refresh are already handled by
        # _on_workspace_data_changed -> _do_deferred_recalculation.
        # This path only updates panels that need immediate sync.
        self._rule_panel.rebuild()
        self._sync_rule_bar_from_current()
        self._sync_flow_panel_from_current()
        # Mark workspace as dirty when matrix content changes
        self._mark_dirty(True)

    def _refresh_table(self) -> None:
        self._workspace.refresh_table()

    def _reload_active_view(self) -> None:
        owner = StatusOwner.load(self._next_status_owner_id())
        self._status_manager.begin(owner, StatusState.LOADING)
        try:
            self._workspace.reload_active_view()
            # Derive current view from session state, not workspace saved default.
            result = self.session.query("active_view_current")
            view_id = result.get("view_id") if result else None
            if view_id is not None:
                self._active_view_id = view_id
            self._sync_tile_fetch_signals()
            self._refresh_error_status(allow_from_computing=True)
        finally:
            self._status_manager.end(owner)

    def _on_workspace_changed(self, event=None) -> None:
        if getattr(self, '_in_workspace_changed', False):
            return
        self._in_workspace_changed = True
        logger.info("[MainWindow] workspace_changed start")
        t0 = time.perf_counter()
        try:
            with self._status_manager.hold(
                StatusOwner.load(self._next_status_owner_id()), StatusState.LOADING
            ):
                self._suppress_tile_fetches(True)
                self._dock_browser.rebuild()
                self._reload_active_view()
                for win in list(self._workspace_windows):
                    DEBUG_GUI and print(f"DEBUG _on_workspace_changed: reloading window {win._workspace_number if hasattr(win, '_workspace_number') else 'unknown'}")
                    win.controller.reload_active_view()
                self._rule_panel.rebuild()
                self._after_mutation_enqueue_recompute()
                self._refresh_error_status(allow_from_computing=True)
        finally:
            logger.info("[MainWindow] workspace_changed done duration=%.3f ms", (time.perf_counter() - t0) * 1000)
            self._in_workspace_changed = False

    def _iter_workspace_controllers(self) -> list[ViewWorkspaceController]:
        controllers = [self._workspace]
        for win in list(self._workspace_windows):
            controllers.append(win.controller)
        return controllers

    def _iter_matrix_grids(self):
        """Yield every MatrixGrid across the main and secondary workspaces."""
        from lib_gui_elements.matrix_grid import MatrixGrid
        for controller in self._iter_workspace_controllers():
            table = controller.active_table
            if isinstance(table, MatrixGrid):
                yield table

    def _suppress_tile_fetches(self, suppressed: bool) -> None:
        """Suppress or resume tile fetches on all visible grids."""
        for grid in self._iter_matrix_grids():
            grid.set_tile_fetch_suppressed(suppressed)

    def _after_mutation_enqueue_recompute(self) -> None:
        """After any model mutation, enqueue an async dirty recompute if needed."""
        if self._dirty_recompute_in_progress:
            self._recompute_pending_again = True
            return
        try:
            result = self.session.query("diagnostics_dirty_count")
            dirty_count = result.get("dirty_count", 0) if result else 0
        except Exception as exc:
            logger.warning("[MainWindow] dirty_count query failed: %s", exc)
            dirty_count = 0
        if dirty_count > 0:
            self._start_dirty_recompute()
        else:
            self._refresh_table()
            self._suppress_tile_fetches(False)

    def _start_dirty_recompute(self) -> None:
        """Start a background dirty recalculation and suppress tile fetches."""
        if self._dirty_recompute_in_progress:
            return
        self._dirty_recompute_in_progress = True
        self._recompute_pending_again = False
        self._recompute_error_detail = None
        self._dirty_recompute_owner = StatusOwner.recalc(self._next_status_owner_id())
        logger.info("[DirtyRecalc] begin owner=%s", self._dirty_recompute_owner)
        self._status_manager.begin(self._dirty_recompute_owner, StatusState.CALCULATING)
        self._suppress_tile_fetches(True)

        self._dirty_recompute_thread = QtCore.QThread(self)
        self._dirty_recompute_worker = RecalcWorker(self.session, scope="dirty")
        self._dirty_recompute_worker.moveToThread(self._dirty_recompute_thread)

        self._dirty_recompute_thread.started.connect(self._dirty_recompute_worker.run)
        self._dirty_recompute_worker.result_ready.connect(self._on_dirty_recompute_result)
        self._dirty_recompute_worker.error.connect(self._on_dirty_recompute_error)
        self._dirty_recompute_worker.finished.connect(self._dirty_recompute_thread.quit)
        self._dirty_recompute_worker.error.connect(self._dirty_recompute_thread.quit)

        self._dirty_recompute_thread.start()

    @QtCore.Slot(dict)
    def _on_dirty_recompute_result(self, result: dict) -> None:
        """Handle successful dirty recalculation result on the GUI thread."""
        if not result.get("ok"):
            self._on_dirty_recompute_error(result.get("error") or "Recompute failed")
            return

        self._recompute_error_detail = None
        generation = result.get("generation")

        if self._recompute_pending_again:
            self._recompute_pending_again = False
            # The current worker has already quit; create a new one.
            self._dirty_recompute_in_progress = False
            self._suppress_tile_fetches(False)
            if self._dirty_recompute_owner is not None:
                self._status_manager.end(self._dirty_recompute_owner)
                self._dirty_recompute_owner = None
            self._start_dirty_recompute()
            return

        self._dirty_recompute_in_progress = False
        self._suppress_tile_fetches(False)
        for grid in self._iter_matrix_grids():
            if generation is not None:
                grid.refresh_for_generation(generation)
            else:
                grid.reload(invalidate_tiles="data")
        self._sync_rule_bar_from_current()
        self._sync_flow_panel_from_current()
        self._refresh_error_status(allow_from_computing=True)

        # Keep the status owner alive until the coalesced UI refresh has run,
        # so the bar does not drop back to "Waiting for command" while the
        # GUI is still rebuilding. If no UI refresh is pending, end it now.
        if self._ui_refresh_timer is None or not self._ui_refresh_timer.isActive():
            if self._dirty_recompute_owner is not None:
                logger.info("[DirtyRecalc] result ending owner=%s (no pending UI refresh)", self._dirty_recompute_owner)
                self._status_manager.end(self._dirty_recompute_owner)
                self._dirty_recompute_owner = None
        else:
            logger.info("[DirtyRecalc] result keeping owner=%s alive until UI refresh", self._dirty_recompute_owner)
            self._dirty_recompute_complete_pending = True

    @QtCore.Slot(str)
    def _on_dirty_recompute_error(self, error_msg: str) -> None:
        """Handle dirty recalculation failure on the GUI thread."""
        # Keep a concise headline for the status indicator; log the full traceback.
        headline = error_msg.splitlines()[0] if error_msg else "Recompute failed"
        self._recompute_error_detail = headline
        logger.error("[MainWindow] dirty recompute error: %s\n%s", headline, error_msg)
        if self._dirty_recompute_owner is not None:
            self._status_manager.end(self._dirty_recompute_owner)
            self._dirty_recompute_owner = None
        self._dirty_recompute_in_progress = False
        self._recompute_pending_again = False
        self._suppress_tile_fetches(False)
        # Show the headline in the status bar long enough to read.
        self.statusBar().showMessage(headline, 10000)
        self._refresh_error_status(allow_from_computing=True)

    def _sync_view_state_to_session_store(self) -> None:
        """Sync runtime UI state (active cell, selection, scroll) to SessionStore.

        Called before saving to persist the current session view state.
        Page selections are canonical workspace metadata and do not need syncing.
        """
        # Sync selection state for ALL views (not just the active one)
        # We need to save each view's individual selection state
        for view in self.workspace_read_model.list_view_dtos():
            view_id = view.get("id")
            # Find the table for this view through controllers
            table = None
            for controller in self._iter_workspace_controllers():
                for vt in controller._view_tabs:
                    if vt.view_id == view_id:
                        table = vt.table
                        break
                if table is not None:
                    break
            
            if isinstance(table, MatrixGrid):
                # Save active cell (focus cell in multi-selection)
                active_cell = (table._sel_row, table._sel_col) if table._sel_row >= 0 and table._sel_col >= 0 else None
                self.session.execute("set_property", target=f"view:{view_id}", property="view.active_cell", value=active_cell)

                # Save full selection state
                self.session.execute("set_property", target=f"view:{view_id}", property="view.selection_mode", value=table._sel_mode)
                if table._sel_indices:
                    # Convert selection to list of (row, col) tuples based on mode
                    if table._sel_mode == "cell":
                        selected_indices = [idx if isinstance(idx, tuple) else (idx, 0) for idx in table._sel_indices]
                    elif table._sel_mode == "row":
                        selected_indices = [r for r in table._sel_indices if isinstance(r, int)]
                    elif table._sel_mode == "col":
                        selected_indices = [c for c in table._sel_indices if isinstance(c, int)]
                    else:
                        selected_indices = []
                else:
                    selected_indices = []
                self.session.execute("set_property", target=f"view:{view_id}", property="view.selected_indices", value=selected_indices)

                anchor_cell = (table._anchor_row, table._anchor_col) if table._anchor_row >= 0 and table._anchor_col >= 0 else None
                self.session.execute("set_property", target=f"view:{view_id}", property="view.anchor_cell", value=anchor_cell)

                # Save scroll position
                h_scroll = table.horizontalScrollBar().value()
                v_scroll = table.verticalScrollBar().value()
                self.session.execute("set_property", target=f"view:{view_id}", property="view.scroll_pos", value=(h_scroll, v_scroll))
                if DEBUG_GUI:
                    print(f"[DEBUG _sync_view_state] view={view_id[:8]}: active_cell={active_cell}, selected_indices={selected_indices}")

        # Save active view ID to workspace (only views are physically active and visible)
        for controller in self._iter_workspace_controllers():
            view_id = controller.active_view_id
            if view_id is None:
                continue
            view = self.workspace_read_model.get_view(view_id)
            if view is None:
                continue
            # Update active view through command spine (not direct workspace mutation)
            self.session.execute("set_active_view", view_id=view_id)
            if DEBUG_GUI:
                print(f"[DEBUG _sync_view_state] workspace: saved active_view_id={view_id[:8]}")

    def _restore_view_state_from_session_store(self) -> None:
        """Restore runtime UI state (active cell, scroll) from SessionStore after loading.

        Called after loading to restore the per-session view state.
        Page selections are canonical workspace metadata and are already loaded.
        """
        print(f"\n\n=== RESTORE VIEW STATE CALLED ===\n\n")
        print(f"[DEBUG _restore_view_state] Restoring view state...")
        views = self.workspace_read_model.list_views()
        print(f"[DEBUG _restore_view_state] Workspace has {len(views)} views")
        for v in views:
            vid = v.get("id", "")
            state = self.workspace_read_model.get_view_state(vid) or {}
            print(f"[DEBUG _restore_view_state] View {vid[:8]}: selection_mode={state.get('selection_mode')}, selected_indices={state.get('selected_indices')}, active_cell={state.get('active_cell')}, anchor_cell={state.get('anchor_cell')}")
        for controller in self._iter_workspace_controllers():
            view_id = controller.active_view_id
            print(f"[DEBUG _restore_view_state] Controller active_view_id={view_id[:8] if view_id else None}")
            if view_id is None:
                continue
            state = self.workspace_read_model.get_view_state(view_id) or {}
            if not state:
                print(f"[DEBUG _restore_view_state] View state for {view_id[:8]} not found")
                continue

            table = controller.active_table
            print(f"[DEBUG _restore_view_state] Controller active_table type={type(table).__name__}")
            if isinstance(table, MatrixGrid):
                # Restore scroll position
                scroll_pos = state.get("scroll_pos")
                if scroll_pos is not None:
                    h_scroll, v_scroll = scroll_pos
                    table.horizontalScrollBar().setValue(h_scroll)
                    table.verticalScrollBar().setValue(v_scroll)
                    print(f"[DEBUG _restore_view_state] view={view_id[:8]}: restored scroll=({h_scroll}, {v_scroll})")

                # Restore full selection state
                selection_mode = state.get("selection_mode", "cell")
                if selection_mode in ("cell", "row", "col", "all"):
                    table._sel_mode = selection_mode

                selected_indices = state.get("selected_indices", [])
                if selected_indices:
                    valid_indices = set()
                    print(f"[DEBUG] Restoring {len(selected_indices)} indices, mode={selection_mode}, row_keys={len(table._row_keys)}, col_keys={len(table._col_keys)}")
                    for idx in selected_indices:
                        print(f"[DEBUG] Processing idx={idx}, type={type(idx).__name__}")
                        if isinstance(idx, (list, tuple)) and len(idx) == 2:
                            r, c = idx
                            if selection_mode == "cell":
                                if 0 <= r < len(table._row_keys) and 0 <= c < len(table._col_keys):
                                    valid_indices.add((r, c))
                            elif selection_mode == "row":
                                if 0 <= r < len(table._row_keys):
                                    valid_indices.add(r)
                            elif selection_mode == "col":
                                if 0 <= c < len(table._col_keys):
                                    valid_indices.add(c)
                        elif isinstance(idx, int):
                            if selection_mode == "row" and 0 <= idx < len(table._row_keys):
                                print(f"[DEBUG] Adding row idx {idx}")
                                valid_indices.add(idx)
                            elif selection_mode == "col" and 0 <= idx < len(table._col_keys):
                                print(f"[DEBUG] Adding col idx {idx}")
                                valid_indices.add(idx)
                            else:
                                print(f"[DEBUG] REJECTED: mode={selection_mode}, idx={idx}, row_keys={len(table._row_keys)}, col_keys={len(table._col_keys)}")
                    table._sel_indices = valid_indices
                    print(f"[DEBUG] Restored {len(valid_indices)} valid indices: {valid_indices}")
                else:
                    table._sel_indices = set()

                anchor_cell = state.get("anchor_cell")
                if anchor_cell is not None:
                    row, col = anchor_cell
                    if 0 <= row < len(table._row_keys) and 0 <= col < len(table._col_keys):
                        table._anchor_row = row
                        table._anchor_col = col

                active_cell = state.get("active_cell")
                if active_cell is not None:
                    row, col = active_cell
                    if 0 <= row < len(table._row_keys) and 0 <= col < len(table._col_keys):
                        table._sel_row = row
                        table._sel_col = col
                        if table._anchor_row < 0 or table._anchor_col < 0:
                            table._anchor_row = row
                            table._anchor_col = col

                table.selection_changed.emit()
                table.viewport().update()
                print(f"[DEBUG _restore_view_state] view={view_id[:8]}: restored selection_mode={table._sel_mode}, selected_count={len(table._sel_indices)}, active_cell=({table._sel_row}, {table._sel_col})")

    @QtCore.Slot()
    def _on_workspace_data_changed(self) -> None:
        # Skip updates if we're already recalculating (F9 in progress)
        if getattr(self, '_recalculating', False):
            logger.info("[MainWindow] workspace_data_changed ignored (recalculating)")
            return
        logger.info("[MainWindow] workspace_data_changed")
        # If a dirty recompute is already running, mark it pending again so
        # the result handler starts a fresh pass before fetching tiles.
        if getattr(self, '_dirty_recompute_in_progress', False):
            self._recompute_pending_again = True
            return
        # Defer recalculation to prevent UI freezing during header edits
        # Batch multiple rapid changes into a single recalculation
        if not hasattr(self, '_pending_recalc_timer'):
            self._pending_recalc_timer: Optional[QtCore.QTimer] = None

        if self._pending_recalc_timer is not None:
            self._pending_recalc_timer.stop()
        else:
            self._pending_recalc_timer = QtCore.QTimer(self)
            self._pending_recalc_timer.setSingleShot(True)
            self._pending_recalc_timer.timeout.connect(self._do_deferred_recalculation)

        # Start timer with 0ms delay - allows UI to process pending events first
        self._pending_recalc_timer.start(0)

    def _do_deferred_recalculation(self) -> None:
        """Coalesce rapid workspace changes into a single async dirty recompute."""
        logger.info("[MainWindow] deferred recalculation start")
        self._after_mutation_enqueue_recompute()
        # The file has changed even if no engine nodes are currently dirty.
        self._mark_dirty(True)
        logger.info("[MainWindow] deferred recalculation done")

    @QtCore.Slot()
    def _on_new_workspace(self) -> None:
        # Assign the next available workspace number
        next_number = len(self._workspace_windows) + 1
        win = ViewWorkspaceWindow(
            on_workspace_changed=self._on_workspace_changed,
            parent=self,
            session=self.session,
        )
        win._workspace_number = next_number  # type: ignore[attr-defined]
        win._update_window_title()  # type: ignore[attr-defined]
        win.controller.data_changed.connect(self._on_workspace_data_changed)
        win.controller.rules_changed.connect(self._on_rules_changed)
        win.controller.mark_dirty_requested.connect(lambda: self._mark_dirty(True))
        win.destroyed.connect(self._on_workspace_window_destroyed)
        self._workspace_windows.append(win)
        win.show()
        win.raise_()
        win.activateWindow()

    @QtCore.Slot(object)
    def _on_workspace_window_destroyed(self, obj: object) -> None:
        self._workspace_windows = [w for w in self._workspace_windows if w is not obj]

    @QtCore.Slot()
    def _on_close_workspace(self) -> None:
        if not self._workspace_windows:
            return
        active = QtWidgets.QApplication.activeWindow()
        for win in reversed(self._workspace_windows):
            if win is active:
                win.close()
                return
        self._workspace_windows[-1].close()

    def _show_about_dialog(self) -> None:
        """Show the About dialog."""
        import os
        from PySide6 import QtGui

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("About OM Core")
        dialog.setFixedSize(480, 480)

        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(40, 35, 40, 30)
        layout.setSpacing(15)

        logo_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "assets", "logo", "om-core-logo-transparent.png",
        )
        logo_label = QtWidgets.QLabel()
        logo_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        logo_label.setFixedSize(360, 120)
        if os.path.exists(logo_path):
            pixmap = QtGui.QPixmap(logo_path)
            if not pixmap.isNull():
                logo_label.setPixmap(pixmap.scaled(
                    360, 120,
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation
                ))
        layout.addWidget(logo_label, alignment=QtCore.Qt.AlignmentFlag.AlignCenter)

        title_label = QtWidgets.QLabel("OM Core")
        title_font = title_label.font()
        title_font.setPointSize(10)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)

        description = QtWidgets.QLabel(
            "OM Core is a multidimensional modeling engine\n"
            "for building structured financial, operational,\n"
            "and analytical models.\n\n"
            "Copyright © 2026 Alexander Bikeyev.\n"
            "Published by Cloudcell Limited.\n\n"
            "Licensed under the GNU Affero General Public License v3.0.\n"
            "Commercial licensing may be available separately."
        )

        description.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        description.setWordWrap(True)
        layout.addWidget(description)

        from lib_utils.version import om_version
        version_label = QtWidgets.QLabel(f"Version: {om_version()}")
        version_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        version_font = version_label.font()
        version_font.setPointSize(8)
        version_label.setFont(version_font)
        layout.addWidget(version_label)

        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn, alignment=QtCore.Qt.AlignmentFlag.AlignCenter)

        dialog.exec()

    def _on_show_options(self) -> None:
        """Show the Options dialog."""
        dialog = OptionsDialog(self)
        result = dialog.exec()
        if result == int(QtWidgets.QDialog.DialogCode.Accepted):
            # System-element visibility may have changed; refresh browsers and tabs.
            try:
                self._dock_browser.rebuild()
            except Exception:
                pass
            try:
                self._workspace.rebuild_tabs()
            except Exception:
                pass

    def _on_toolbox_editor(self) -> None:
        """Show the Toolbox Editor as a floating dock palette."""
        from lib_gui.menubuilder.toolbox_editor import ToolboxEditorPalette

        # Guard against multiple simultaneous launches
        if getattr(self, '_toolbox_editor_launching', False):
            print("[DEBUG] Toolbox Editor already launching, ignoring request")
            return

        # If palette already exists and is valid, just raise it
        if hasattr(self, '_toolbox_palette') and self._toolbox_palette is not None:
            try:
                # Check if palette is still valid (not deleted)
                _ = self._toolbox_palette.isVisible()
                palette = self._toolbox_palette
                palette.show()
                palette.raise_()
                palette.activateWindow()
                print("[DEBUG] Toolbox Editor already exists, raising to front")
                return
            except RuntimeError:
                # Palette was deleted, clear reference and create new
                print("[DEBUG] Toolbox Editor was deleted, creating new one")
                self._toolbox_palette = None

        # Set guard flag while creating
        self._toolbox_editor_launching = True
        print("[DEBUG] Creating new Toolbox Editor")

        try:
            self._toolbox_palette = ToolboxEditorPalette(self, self)
            self._toolbox_palette.close_requested.connect(self._disable_toolbar_edit_mode)

            # Tabify with model browser (appear as tab in same dock widget)
            self.tabifyDockWidget(self._dock_browser, self._toolbox_palette)

            # Save local reference to prevent race condition with _disable_toolbar_edit_mode
            palette = self._toolbox_palette
            if palette:
                palette.show()
                palette.raise_()

            # Enable toolbar edit mode
            self._enable_toolbar_edit_mode()
        finally:
            # Clear guard flag
            self._toolbox_editor_launching = False

    def _enable_toolbar_edit_mode(self) -> None:
        """Enable edit mode on the main toolbar - allows drag-and-drop and selection."""
        self._toolbar_edit_mode = True
        
        # Find the main toolbar
        toolbars = self.findChildren(QtWidgets.QToolBar)
        print(f"[DEBUG] Found {len(toolbars)} toolbars")
        
        for toolbar in toolbars:
            print(f"[DEBUG] Toolbar objectName: {toolbar.objectName()!r}")
            if toolbar.objectName() == "MainToolBar":
                self._edit_toolbar = toolbar  # Store reference
                print(f"[DEBUG] Found MainToolBar, creating drop overlay")
                toolbar.setStyleSheet("""
                    QToolBar {
                        background: #DBEAFE;
                        border: 2px dashed #3B82F6;
                        padding: 4px;
                    }
                    QToolButton {
                        border: 1px solid transparent;
                    }
                    QToolButton:hover {
                        border: 1px solid #3B82F6;
                        background: #BFDBFE;
                    }
                    QToolButton:selected, QToolButton:checked {
                        border: 2px solid #2563EB;
                        background: #93C5FD;
                    }
                """)
                
                # Create transparent overlay for drop target
                self._toolbar_drop_overlay = ToolbarDropOverlay(toolbar, self)
                self._toolbar_drop_overlay.show()
                print("[DEBUG] Drop overlay created and shown!")
                
                # Make toolbar buttons and separators selectable and draggable
                self._button_overlays = []  # Store overlays to raise later
                print(f"[DEBUG] Creating button overlays for {len(toolbar.actions())} actions")
                for action in toolbar.actions():
                    widget = toolbar.widgetForAction(action)
                    if action.isSeparator():
                        # Separators don't have widgets, but we can still make them draggable
                        # by finding their geometry from the toolbar layout
                        print(f"[DEBUG] Creating overlay for separator at index {toolbar.actions().index(action)}")
                        # Get separator geometry from toolbar
                        separator_overlay = self._create_separator_overlay(toolbar, action)
                        if separator_overlay:
                            separator_overlay.show()
                            separator_overlay.raise_()
                            self._button_overlays.append(separator_overlay)
                            print(f"[DEBUG] Separator overlay created and shown, geometry: {separator_overlay.geometry()}")
                        else:
                            print(f"[DEBUG] Failed to create separator overlay")
                        continue
                    if widget and isinstance(widget, QtWidgets.QToolButton):
                        print(f"[DEBUG-BUTTON] Creating overlay for button: '{action.text()}', widget={widget}, pos={widget.pos()}, size={widget.size()}")
                        widget.setCheckable(True)
                        widget.setMouseTracking(True)
                        
                        # Connect with debug wrapper
                        def on_button_clicked_edit(checked, a=action):
                            print(f"[DEBUG-BUTTON] Button clicked in edit mode: '{a.text()}'")
                            self._on_toolbar_button_clicked(a)
                        widget._edit_mode_clicked_slot = on_button_clicked_edit
                        widget.clicked.connect(widget._edit_mode_clicked_slot)
                        # Make draggable for reordering
                        widget.installEventFilter(self)
                        widget.setProperty("_toolbar_action", action)
                        widget.setProperty("_is_draggable_button", True)
                        # Add transparent drag overlay on top of button
                        drag_overlay = ButtonDragOverlay(widget, action, self)
                        drag_overlay.show()
                        drag_overlay.raise_()
                        self._button_overlays.append(drag_overlay)
                        print(f"[DEBUG] Button overlay created and shown for {action.text()}")
                    elif widget:
                        # Non-QToolButton widget (QComboBox, etc.) - create WidgetDragOverlay
                        label = widget.property("_toolbar_widget_label") or action.text() or "Widget"
                        self._add_widget_overlay(widget, label)
                        print(f"[DEBUG] Widget overlay created and shown for {label}")

                print(f"[DEBUG] Total overlays: {len(self._button_overlays)}")
                        
                break
                        
        self.statusBar().showMessage("Toolbar Edit Mode: Drag widgets from palette to toolbar, click items to edit", 5000)

    def _disable_toolbar_edit_mode(self) -> None:
        """Disable toolbar edit mode and remove overlays."""
        print("[DEBUG] Disabling toolbar edit mode, removing overlays")
        
        if not self._toolbar_edit_mode:
            print("[DEBUG] Edit mode already disabled, nothing to do")
            return
        
        self._toolbar_edit_mode = False
        
        # Stop timers on overlays before deleting them
        if hasattr(self, '_button_overlays') and self._button_overlays:
            print(f"[DEBUG] Stopping timers on {len(self._button_overlays)} overlays")
            for overlay in self._button_overlays:
                try:
                    if hasattr(overlay, '_position_timer'):
                        overlay._position_timer.stop()
                    if hasattr(overlay, '_retry_timer'):
                        overlay._retry_timer.stop()
                    overlay._deleting = True
                except RuntimeError:
                    pass  # Overlay was already deleted
        
        # Clear palette reference - just hide, let Qt manage dock widget lifecycle
        if hasattr(self, '_toolbox_palette') and self._toolbox_palette is not None:
            try:
                self._toolbox_palette.hide()
            except RuntimeError:
                pass
            self._toolbox_palette = None

        # Remove drop overlay - hide only, don't delete to avoid C++ races
        if hasattr(self, '_toolbar_drop_overlay') and self._toolbar_drop_overlay:
            self._toolbar_drop_overlay._deleting = True
            self._toolbar_drop_overlay.hide()
            self._toolbar_drop_overlay = None

        # Remove button drag overlays - hide only, don't delete to avoid C++ races
        if hasattr(self, '_button_overlays') and self._button_overlays:
            print(f"[DEBUG] Hiding {len(self._button_overlays)} overlays")
            overlays = self._button_overlays
            self._button_overlays = []
            for overlay in overlays:
                overlay._deleting = True
                overlay.hide()

        # Reset toolbar style
        print("[DEBUG] Resetting toolbar style")
        for toolbar in self.findChildren(QtWidgets.QToolBar):
            if toolbar.objectName() == "MainToolBar":
                print("[DEBUG] Clearing toolbar stylesheet")
                toolbar.setStyleSheet("")  # Reset to default
                
                # Remove selection from buttons and clean up edit-mode connections/filters
                print("[DEBUG] Cleaning up toolbar buttons")
                for action in toolbar.actions():
                    if action.isSeparator():
                        continue
                    widget = toolbar.widgetForAction(action)
                    if widget and isinstance(widget, QtWidgets.QToolButton):
                        print(f"[DEBUG] Cleaning button: {action.text()}")
                        widget.setCheckable(False)
                        widget.setMouseTracking(False)
                        widget.removeEventFilter(self)
                        if hasattr(widget, '_edit_mode_clicked_slot'):
                            try:
                                widget.clicked.disconnect(widget._edit_mode_clicked_slot)
                            except RuntimeError:
                                pass
                            delattr(widget, '_edit_mode_clicked_slot')
                        widget.setProperty("_is_draggable_button", None)
                print("[DEBUG] Toolbar buttons cleaned")
                        
        print("[DEBUG] Clearing edit toolbar reference")
        self._edit_toolbar = None
        self._toolbox_palette = None
        print("[DEBUG] Showing status message")
        self.statusBar().showMessage("Toolbar Edit Mode disabled", 3000)
        print("[DEBUG] Toolbar edit mode disabled successfully")

    def _process_toolbar_drop(self, data: dict, drop_pos: QtCore.QPoint = None) -> None:
        """Process a drop on the toolbar (called by overlay)."""
        print(f"[DEBUG] Processing drop: {data}, pos: {drop_pos}")
        try:
            # Handle reordering of existing buttons
            if data.get("type") == "reorder":
                self._reorder_toolbar_button(
                    data.get("action_text"),
                    drop_pos,
                    is_separator=data.get("is_separator", False),
                    separator_idx=data.get("separator_idx", None)
                )
                return

            # Handle reordering of existing widgets (dropdowns, etc.)
            if data.get("type") == "widget_reorder":
                self._reorder_toolbar_widget(
                    data.get("widget_label"),
                    drop_pos
                )
                return

            # Handle new item from palette
            item = self._create_menu_item_from_drop(data)
            print(f"[DEBUG] Created item: {item}")
            if item and item.macro_id:
                print(f"[DEBUG] Item has macro_id: {item.macro_id}")
            if item:
                insert_position = None
                if drop_pos is not None:
                    insert_position = self._calculate_toolbar_insert_position(drop_pos)
                    print(f"[DEBUG] Drop will insert at config position: {insert_position}")

                # Add to palette's config in the same logical insertion position
                if self._toolbox_palette:
                    self._toolbox_palette.add_item_to_config("toolbar", item, position=insert_position)
                # Add to actual toolbar at drop position
                self._add_item_to_main_toolbar(item, drop_pos)
                self.statusBar().showMessage(f"Added '{item.label}' to toolbar", 3000)
                print(f"[DEBUG] Successfully added '{item.label}' to toolbar")
            else:
                print("[DEBUG] Item was None")
        except Exception as e:
            print(f"[DEBUG] Error processing drop: {e}")
            import traceback
            traceback.print_exc()
            
    def _reorder_toolbar_button(self, action_text: str, drop_pos: QtCore.QPoint, is_separator: bool = False, separator_idx: int = None) -> None:
        """Reorder a toolbar button or separator to the specified position."""
        print(f"[DEBUG] Reordering {'separator' if is_separator else 'button ' + repr(action_text)} to pos {drop_pos}")
        
        toolbar = self._edit_toolbar
        if not toolbar or not drop_pos:
            return
            
        # Find the action being moved
        moving_action = None
        if is_separator:
            # For separators, find by index if provided, otherwise find first separator
            actions = toolbar.actions()
            if separator_idx is not None and 0 <= separator_idx < len(actions):
                action = actions[separator_idx]
                if action.isSeparator():
                    moving_action = action
                    print(f"[DEBUG] Found separator at index {separator_idx}")
                else:
                    # Index no longer points to a separator, fall back to finding first
                    for a in actions:
                        if a.isSeparator():
                            moving_action = a
                            break
            else:
                # No index provided, find first separator
                for action in actions:
                    if action.isSeparator():
                        moving_action = action
                        break
        else:
            for action in toolbar.actions():
                if action.text() == action_text:
                    moving_action = action
                    break
                
        if not moving_action:
            print(f"[DEBUG] Could not find action with text '{action_text}'")
            return
            
        # Find the action before which we should insert
        actions = toolbar.actions()
        insert_before = None
        
        for action in actions:
            if action == moving_action:
                continue
            widget = toolbar.widgetForAction(action)
            if widget:
                # Get widget geometry in toolbar coordinates
                widget_rect = widget.geometry()
                # Check if drop is to the left of this widget's center
                if drop_pos.x() < widget_rect.center().x():
                    insert_before = action
                    print(f"[DEBUG] Inserting before action: {action.text()}")
                    break
            elif action.isSeparator():
                # For separators without widgets, check against separator overlay position
                # Find the separator overlay to get its geometry
                for overlay in getattr(self, '_button_overlays', []):
                    if hasattr(overlay, '_action') and overlay._action == action:
                        overlay_rect = overlay.geometry()
                        if drop_pos.x() < overlay_rect.center().x():
                            insert_before = action
                            print(f"[DEBUG] Inserting before separator")
                            break
                if insert_before:
                    break
        
        # Remove and re-insert at new position
        toolbar.removeAction(moving_action)
        if insert_before:
            toolbar.insertAction(insert_before, moving_action)
        else:
            toolbar.addAction(moving_action)
        
        # Use a longer delay to ensure toolbar layout has fully settled
        text = action_text if not is_separator else "Separator"
        QtCore.QTimer.singleShot(100, lambda: self._delayed_refresh_overlays(text))
        
        # Save the new order to config
        QtCore.QTimer.singleShot(150, self._save_toolbar_order_to_config)

    def _calculate_toolbar_insert_position(self, drop_pos: QtCore.QPoint) -> int:
        """Calculate the insertion index in toolbar_layout based on drop position."""
        toolbar = self._edit_toolbar
        if not toolbar:
            return -1  # Append at end
        
        # Find which action the drop is closest to
        actions = toolbar.actions()
        for i, action in enumerate(actions):
            action_rect = toolbar.actionGeometry(action)
            if drop_pos.x() < action_rect.center().x():
                return i  # Insert before this position

        return len(actions)  # Append at end

    def _find_toolbar_insert_action(self, toolbar: QtWidgets.QToolBar, drop_pos: QtCore.QPoint) -> QtGui.QAction | None:
        """Return the action before which a dropped item should be inserted."""
        if not toolbar or not drop_pos:
            return None

        for action in toolbar.actions():
            action_rect = toolbar.actionGeometry(action)
            if drop_pos.x() < action_rect.center().x():
                return action

        return None

    def _save_toolbar_order_to_config(self):
        """Save current toolbar action order to config file."""
        try:
            from lib_gui.menubuilder.toolbox_editor import load_toolbox_config, save_toolbox_config
            from lib_gui.menubuilder.models import MenuLocation
            from PySide6 import QtWidgets
            
            # Use palette's config if available (has newly added items), otherwise load from file
            if self._toolbox_palette and hasattr(self._toolbox_palette, '_config'):
                config = self._toolbox_palette._config
            else:
                config = load_toolbox_config()
            
            toolbar = self._edit_toolbar
            if not toolbar:
                return
            
            print(f"[DEBUG-SAVE] Starting save, toolbar has {len(toolbar.actions())} actions")
            
            # Build new toolbar_layout from current action order
            new_layout = []
            for i, action in enumerate(toolbar.actions()):
                is_sep = action.isSeparator()
                text = action.text() if not is_sep else "(separator)"
                action_type = type(action).__name__
                print(f"[DEBUG-SAVE] Action {i}: type={action_type}, isSeparator={is_sep}, text='{text}'")
                
                # Find the item_id for this action
                found = False
                
                # First try to match by stored item_id property on the action
                action_item_id = action.property("item_id")
                print(f"[DEBUG-SAVE]   -> action.property('item_id')={action_item_id}")
                if action_item_id and action_item_id in config.items:
                    new_layout.append(action_item_id)
                    print(f"[DEBUG-SAVE]   -> MATCHED by action item_id: {action_item_id}")
                    found = True
                # For widgets, check if it's a QWidgetAction and get the widget's item_id
                elif not is_sep and isinstance(action, QtWidgets.QWidgetAction):
                    widget = action.defaultWidget()
                    widget_type = type(widget).__name__ if widget else "None"
                    print(f"[DEBUG-SAVE]   -> QWidgetAction, widget type={widget_type}")
                    if widget:
                        widget_item_id = widget.property("item_id")
                        print(f"[DEBUG-SAVE]   -> widget.property('item_id')={widget_item_id}")
                        if widget_item_id and widget_item_id in config.items:
                            new_layout.append(widget_item_id)
                            print(f"[DEBUG-SAVE]   -> MATCHED by widget item_id: {widget_item_id}")
                            found = True
                
                if not found:
                    print(f"[DEBUG-SAVE]   -> NO MATCH FOUND - item_id property missing!")
            
            # Update config and save
            print(f"[DEBUG-SAVE] BEFORE: toolbar_layout has {len(config.toolbar_layout)} items")
            config.toolbar_layout = new_layout
            print(f"[DEBUG-SAVE] AFTER: toolbar_layout now has {len(config.toolbar_layout)} items")
            
            # Check if all items in new_layout exist in config.items
            missing_items = [item_id for item_id in new_layout if item_id not in config.items]
            if missing_items:
                print(f"[DEBUG-SAVE] ERROR: Missing items in config: {missing_items}")
            
            save_toolbox_config(config)
            print(f"[DEBUG-SAVE] Saved to disk: {len(new_layout)} items")
            print(f"[DEBUG-SAVE] Order: {new_layout}")
        except Exception as e:
            print(f"[DEBUG-SAVE] Failed to save toolbar order: {e}")

    def _reorder_toolbar_widget(self, widget_label: str, drop_pos: QtCore.QPoint) -> None:
        """Reorder a toolbar widget (dropdown, etc.) to the specified position."""
        print(f"[DEBUG] Reordering widget '{widget_label}' to pos {drop_pos}")

        toolbar = self._edit_toolbar
        if not toolbar or not drop_pos:
            return

        # Find the widget being moved by its label property
        moving_widget = None
        print(f"[DEBUG] Searching for widget with label '{widget_label}' among {len(toolbar.actions())} actions")
        for action in toolbar.actions():
            widget = toolbar.widgetForAction(action)
            if widget:
                label = widget.property("_toolbar_widget_label")
                print(f"[DEBUG]   Widget action='{action.text()}', label_property={label}")
                if label == widget_label:
                    moving_widget = widget
                    moving_action = action
                    print(f"[DEBUG]   -> MATCH FOUND!")
                    break

        if not moving_widget:
            print(f"[DEBUG] Could not find widget with label '{widget_label}'")
            return

        # Find the action before which we should insert
        actions = toolbar.actions()
        insert_before = None

        for action in actions:
            if action == moving_action:
                continue
            widget = toolbar.widgetForAction(action)
            if widget:
                widget_rect = widget.geometry()
                if drop_pos.x() < widget_rect.center().x():
                    insert_before = action
                    print(f"[DEBUG-WIDGET] Inserting before widget action")
                    break
            elif action.isSeparator():
                # For separators without widgets, check against separator overlay position
                for overlay in getattr(self, '_button_overlays', []):
                    if hasattr(overlay, '_action') and overlay._action == action:
                        overlay_rect = overlay.geometry()
                        if drop_pos.x() < overlay_rect.center().x():
                            insert_before = action
                            print(f"[DEBUG-WIDGET] Inserting before separator")
                            break
                if insert_before:
                    break
        
        # If no insert position found (dropped at end), append to end
        if not insert_before:
            # Just use addAction to append - the widget will be at the end
            pass

        # Remove and re-insert at new position
        toolbar.removeAction(moving_action)
        if insert_before:
            toolbar.insertAction(insert_before, moving_action)
        else:
            toolbar.addAction(moving_action)

        # Refresh overlays after layout settles
        QtCore.QTimer.singleShot(100, lambda: self._delayed_refresh_overlays(widget_label))
        
        # Save the new order to config
        QtCore.QTimer.singleShot(150, self._save_toolbar_order_to_config)

    def _delete_toolbar_widget(self, widget_label: str) -> None:
        """Delete a toolbar widget (dropdown, color picker, etc.) from the toolbar."""
        print(f"[DEBUG] Deleting widget '{widget_label}' from toolbar")
        
        toolbar = self._edit_toolbar
        if not toolbar:
            return
            
        # Find and remove the widget by its label property
        for action in toolbar.actions():
            widget = toolbar.widgetForAction(action)
            if widget:
                label = widget.property("_toolbar_widget_label")
                if label == widget_label:
                    item_id = widget.property("item_id")
                    toolbar.removeAction(action)
                    print(f"[DEBUG] Deleted widget '{widget_label}' (item_id={item_id}) from toolbar")
                    
                    # Remove from palette config
                    if self._toolbox_palette and item_id:
                        self._toolbox_palette.remove_item_from_config("toolbar", item_id)
                    
                    # Save the updated config
                    QtCore.QTimer.singleShot(150, self._save_toolbar_order_to_config)
                    
                    # Refresh overlays after deletion
                    QtCore.QTimer.singleShot(100, lambda: self._delayed_refresh_overlays(widget_label))
                    return
                    
        print(f"[DEBUG] Could not find widget '{widget_label}' to delete")

    def _delete_toolbar_item(self, action_text: str, is_separator: bool = False, separator_idx: int = None) -> None:
        """Delete a toolbar button/item or separator."""
        if is_separator:
            print(f"[DEBUG] Deleting separator at index {separator_idx}")
        else:
            print(f"[DEBUG] Deleting item '{action_text}' from toolbar")
        
        toolbar = self._edit_toolbar
        if not toolbar:
            print("[DEBUG] No toolbar to delete from")
            return
            
        if is_separator:
            # Delete separator by index
            actions = toolbar.actions()
            removed_item_id = None
            if separator_idx is not None and 0 <= separator_idx < len(actions):
                action = actions[separator_idx]
                if action.isSeparator():
                    removed_item_id = action.property("item_id")
                    toolbar.removeAction(action)
                    print(f"[DEBUG] Removed separator at index {separator_idx}, item_id={removed_item_id}")
            if not removed_item_id:
                # Fall back: find and remove first separator
                for action in actions:
                    if action.isSeparator():
                        removed_item_id = action.property("item_id")
                        toolbar.removeAction(action)
                        print(f"[DEBUG] Removed first available separator, item_id={removed_item_id}")
                        break
            if removed_item_id:
                # Remove from palette config
                if self._toolbox_palette:
                    self._toolbox_palette.remove_item_from_config("toolbar", removed_item_id)
                # Save the updated config
                QtCore.QTimer.singleShot(150, self._save_toolbar_order_to_config)
                QtCore.QTimer.singleShot(100, self._refresh_button_overlays)
            else:
                print("[DEBUG] Could not find separator to delete")
            return
        else:
            # Delete button by action text
            for action in toolbar.actions():
                if action.text() == action_text:
                    toolbar.removeAction(action)
                    print(f"[DEBUG] Removed action '{action_text}' from toolbar")
                    
                    # Remove from palette config if present
                    if self._toolbox_palette:
                        self._toolbox_palette.remove_item_from_config("toolbar", action_text)
                    
                    # Save the updated config
                    QtCore.QTimer.singleShot(150, self._save_toolbar_order_to_config)
                    
                    # Refresh overlays
                    QtCore.QTimer.singleShot(100, self._refresh_button_overlays)
                    return
                    
            print(f"[DEBUG] Could not find action '{action_text}' to delete")

    def _delayed_refresh_overlays(self, action_text="", attempt=0):
        """Refresh overlays after toolbar layout has settled."""
        QtCore.QCoreApplication.processEvents()
        # Force toolbar layout update
        if self._edit_toolbar:
            self._edit_toolbar.adjustSize()
            self._edit_toolbar.updateGeometry()
            self._edit_toolbar.repaint()
        QtCore.QCoreApplication.processEvents()
        
        # Check if all button widgets have valid geometry before refreshing
        all_valid = True
        if self._edit_toolbar:
            for action in self._edit_toolbar.actions():
                if not action.isSeparator():
                    widget = self._edit_toolbar.widgetForAction(action)
                    if not widget or widget.width() <= 0:
                        all_valid = False
                        break
        
        if all_valid or attempt >= 10:
            self._refresh_with_status(action_text)
        else:
            # Retry after 100ms
            QtCore.QTimer.singleShot(100, lambda: self._delayed_refresh_overlays(action_text, attempt + 1))
        
    def _refresh_with_status(self, action_text=""):
        """Refresh overlays and show status."""
        self._refresh_button_overlays()
        if action_text:
            msg = f"Reordered '{action_text}'" if action_text != "Separator" else "Reordered separator"
            self.statusBar().showMessage(msg, 3000)
            print(f"[DEBUG] Successfully reordered {action_text}")
        
    def _create_separator_overlay(self, toolbar: QtWidgets.QToolBar, action: QtGui.QAction) -> QtWidgets.QWidget:
        """Create a draggable overlay for a toolbar separator."""
        class SeparatorOverlay(QtWidgets.QWidget):
            def __init__(self, target_toolbar, separator_action, main_window):
                super().__init__(main_window)
                self._toolbar = target_toolbar
                self._action = separator_action
                self._main_window = main_window
                self._deleting = False

                self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
                self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.WindowStaysOnTopHint)
                self.setStyleSheet("background: rgba(234, 179, 8, 0.5); border: 2px solid #CA8A04;")
                self.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.OpenHandCursor))
                self.setAcceptDrops(True)
                # Use instance timer so we can stop it during cleanup
                self._retry_timer = QtCore.QTimer(self)
                self._retry_timer.setSingleShot(True)
                self._retry_timer.timeout.connect(lambda: self._update_position(0))
                self._update_position()

            def _update_position(self, retry_count=0):
                if getattr(self, '_deleting', False):
                    return
                try:
                    # Find separator position by calculating from actual widget positions
                    actions = self._toolbar.actions()
                    idx = actions.index(self._action)
                    
                    if idx >= 0:
                        # Find the previous button widget and use its right edge as separator position
                        prev_widget = None
                        for i in range(idx - 1, -1, -1):
                            w = self._toolbar.widgetForAction(actions[i])
                            if w and w.isVisible():
                                prev_widget = w
                                break
                        
                        # Find the next button widget to use its left edge
                        next_widget = None
                        for i in range(idx + 1, len(actions)):
                            w = self._toolbar.widgetForAction(actions[i])
                            if w and w.isVisible():
                                next_widget = w
                                break
                        
                        toolbar_pos = self._toolbar.mapTo(self._main_window, QtCore.QPoint(0, 0))
                        
                        # Check if we have valid widget positions
                        if prev_widget and prev_widget.width() <= 0:
                            if retry_count < 5:
                                self._retry_timer.start(100)
                                return
                        if next_widget and next_widget.width() <= 0:
                            if retry_count < 5:
                                self._retry_timer.start(100)
                                return
                        
                        # Calculate the gap between previous and next buttons
                        prev_right = None
                        next_left = None
                        
                        if prev_widget:
                            prev_pos = prev_widget.mapTo(self._main_window, QtCore.QPoint(0, 0))
                            prev_right = prev_pos.x() + prev_widget.width()
                            print(f"[DEBUG]   prev_widget: pos={prev_pos.x()}, w={prev_widget.width()}, right={prev_right}")
                        else:
                            print(f"[DEBUG]   prev_widget: None")
                        
                        if next_widget:
                            next_pos = next_widget.mapTo(self._main_window, QtCore.QPoint(0, 0))
                            next_left = next_pos.x()
                            print(f"[DEBUG]   next_widget: pos={next_pos.x()}")
                        else:
                            print(f"[DEBUG]   next_widget: None")
                        
                        # Position exactly over the separator line
                        # The separator is centered in the gap between buttons
                        # Make overlay wider for easier grabbing (12px) while visual line stays centered
                        OVERLAY_WIDTH = 12  # Wider for easy grabbing
                        LINE_WIDTH = 3      # Actual separator line width
                        
                        if prev_right is not None and next_left is not None:
                            # Center overlay symmetrically in the gap
                            # Ensure equal distance from prev_right and next_left
                            gap_width = next_left - prev_right
                            # Position so overlay is centered, with equal space on both sides
                            sep_x = prev_right + (gap_width - OVERLAY_WIDTH) // 2
                            sep_width = OVERLAY_WIDTH
                            left_space = sep_x - prev_right
                            right_space = next_left - (sep_x + OVERLAY_WIDTH)
                            print(f"[DEBUG]   Case: symmetric in gap {prev_right}-{next_left}, gap={gap_width}, left_space={left_space}, right_space={right_space}, sep_x={sep_x}")
                        elif prev_right is not None:
                            # At end - position after last button with small gap
                            # Use same spacing logic: place overlay with equal small spacing
                            SPACING = 3  # Small spacing after last button
                            sep_x = prev_right + SPACING
                            sep_width = OVERLAY_WIDTH
                            print(f"[DEBUG]   Case: end position, x={sep_x}, spacing={SPACING}")
                        elif next_left is not None:
                            # At start - position before first button with small gap
                            SPACING = 3  # Small spacing before first button
                            sep_x = next_left - SPACING - OVERLAY_WIDTH
                            sep_width = OVERLAY_WIDTH
                            print(f"[DEBUG]   Case: start position, x={sep_x}, spacing={SPACING}")
                        else:
                            # Fallback - no neighbors, place at start of toolbar
                            SPACING = 3
                            sep_x = toolbar_pos.x() + SPACING
                            sep_width = OVERLAY_WIDTH
                            print(f"[DEBUG]   Case: fallback (no neighbors), x={sep_x}")
                        
                        final_y = toolbar_pos.y()
                        final_h = self._toolbar.height()
                        self.setGeometry(sep_x, final_y, sep_width, final_h)
                        print(f"[DEBUG] Separator overlay final: ({sep_x}, {final_y}) size {sep_width}x{final_h}")
                except (RuntimeError, ValueError) as e:
                    print(f"[DEBUG] Separator overlay _update_position error: {e}")
                    self._deleting = True
                    self.hide()
                    
            def mousePressEvent(self, event):
                if event.button() == QtCore.Qt.MouseButton.LeftButton:
                    if getattr(self, '_deleting', False):
                        return
                    if not self._main_window or getattr(self._main_window, '_edit_toolbar', None) is None:
                        return
                    # Store separator index to uniquely identify which separator is being dragged
                    separator_idx = self._toolbar.actions().index(self._action)
                    data = {"type": "reorder", "action_text": "", "is_separator": True, "separator_idx": separator_idx}
                    drag = QtGui.QDrag(self)
                    mime = QtCore.QMimeData()
                    mime.setText(json.dumps(data))
                    drag.setMimeData(mime)

                    # Set drag pixmap from separator visualization
                    sep_pixmap = QtGui.QPixmap(12, 32)
                    sep_pixmap.fill(QtCore.Qt.GlobalColor.transparent)
                    painter = QtGui.QPainter(sep_pixmap)
                    painter.fillRect(4, 0, 4, 32, QtGui.QColor(234, 179, 8))
                    painter.end()
                    drag.setPixmap(sep_pixmap)
                    drag.setHotSpot(QtCore.QPoint(6, 16))

                    result = drag.exec(QtCore.Qt.DropAction.MoveAction)

                    # Check cursor position to determine if we should delete or reorder
                    cursor_pos = QtGui.QCursor.pos()
                    toolbar = self._main_window._edit_toolbar
                    toolbar_rect = toolbar.geometry()
                    toolbar_top = toolbar.mapToGlobal(QtCore.QPoint(0, 0)).y()
                    toolbar_bottom = toolbar_top + toolbar_rect.height()
                    cursor_y = cursor_pos.y()
                    vertically_outside = cursor_y < toolbar_top or cursor_y > toolbar_bottom
                    
                    if result == QtCore.Qt.DropAction.IgnoreAction:
                        if vertically_outside:
                            self._main_window._delete_toolbar_item("", is_separator=True, separator_idx=separator_idx)
                        else:
                            drop_pos = toolbar.mapFromGlobal(cursor_pos)
                            self._main_window._reorder_toolbar_button("", drop_pos, is_separator=True, separator_idx=separator_idx)
                    else:
                        print(f"[DEBUG] Separator drop handled by overlay (MoveAction), no action needed")
                    
            def paintEvent(self, event):
                """Paint visible separator line in center of wider overlay."""
                from PySide6 import QtGui
                painter = QtGui.QPainter(self)
                # Fill with semi-transparent yellow background (full width is draggable)
                painter.fillRect(self.rect(), QtGui.QColor(234, 179, 8, 100))
                # Draw border around the overlay
                painter.setPen(QtGui.QPen(QtGui.QColor(202, 138, 4), 1))
                painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
                # Draw the actual separator line in the center (3px wide)
                center_x = self.width() // 2
                painter.setPen(QtGui.QPen(QtGui.QColor(120, 53, 15), 3))
                painter.drawLine(center_x, 6, center_x, self.height() - 6)
                # Draw subtle vertical lines at edges to show draggable area
                painter.setPen(QtGui.QPen(QtGui.QColor(234, 179, 8, 150), 1))
                painter.drawLine(2, 10, 2, self.height() - 10)
                painter.drawLine(self.width() - 3, 10, self.width() - 3, self.height() - 10)
                    
            def showEvent(self, event):
                super().showEvent(event)
                self._update_position()
                
        return SeparatorOverlay(toolbar, action, self)
        
    def _refresh_button_overlays(self) -> None:
        """Recreate button drag overlays after reorder."""
        # Remove old overlays
        if hasattr(self, '_button_overlays') and self._button_overlays:
            old_overlays = self._button_overlays
            self._button_overlays = []
            for overlay in old_overlays:
                overlay._deleting = True
                if hasattr(overlay, '_position_timer'):
                    overlay._position_timer.stop()
                if hasattr(overlay, '_retry_timer'):
                    overlay._retry_timer.stop()
                overlay.hide()
        
        self._button_overlays = []
        
        toolbar = self._edit_toolbar
        if not toolbar:
            return
            
        # Create new overlays for all buttons and separators
        for action in toolbar.actions():
            if action.isSeparator():
                print(f"[DEBUG] Creating overlay for separator at index {toolbar.actions().index(action)}")
                # Create draggable overlay for separator
                separator_overlay = self._create_separator_overlay(toolbar, action)
                if separator_overlay:
                    separator_overlay.show()
                    separator_overlay.raise_()
                    self._button_overlays.append(separator_overlay)
                    print(f"[DEBUG] Separator overlay created and shown, geometry: {separator_overlay.geometry()}")
                else:
                    print(f"[DEBUG] Failed to create separator overlay")
                continue
            widget = toolbar.widgetForAction(action)
            if widget:
                # Only configure QToolButton widgets (not QComboBox, etc.)
                if isinstance(widget, QtWidgets.QToolButton):
                    widget.setCheckable(True)
                    widget.setMouseTracking(True)
                    # Disconnect any previous edit-mode slot before reconnecting
                    if hasattr(widget, '_edit_mode_clicked_slot'):
                        try:
                            widget.clicked.disconnect(widget._edit_mode_clicked_slot)
                        except RuntimeError:
                            pass
                        delattr(widget, '_edit_mode_clicked_slot')
                    def on_button_clicked_edit(checked, a=action):
                        print(f"[DEBUG-BUTTON] Button clicked in edit mode: '{a.text()}'")
                        self._on_toolbar_button_clicked(a)
                    widget._edit_mode_clicked_slot = on_button_clicked_edit
                    widget.clicked.connect(widget._edit_mode_clicked_slot)
                    widget.installEventFilter(self)
                    widget.setProperty("_toolbar_action", action)
                    widget.setProperty("_is_draggable_button", True)
                    # Add transparent drag overlay on top of button
                    drag_overlay = ButtonDragOverlay(widget, action, self)
                    drag_overlay.show()
                    self._button_overlays.append(drag_overlay)
                else:
                    # Create overlay for non-QToolButton widgets (QComboBox, etc.)
                    label = widget.property("_toolbar_widget_label") or action.text() or "Widget"
                    self._add_widget_overlay(widget, label)

        # Wait for widgets to settle then position overlays
        QtCore.QTimer.singleShot(50, self._update_overlay_positions)
        
    def _update_overlay_positions(self):
        """Update all overlay positions to match their widgets."""
        print("[DEBUG] ===== UPDATING OVERLAY POSITIONS =====")
        toolbar = self._edit_toolbar
        if not toolbar:
            print("[DEBUG] No toolbar!")
            return
        
        # Print current toolbar state
        print(f"[DEBUG] Toolbar has {len(toolbar.actions())} actions:")
        for i, action in enumerate(toolbar.actions()):
            if action.isSeparator():
                print(f"[DEBUG]   [{i}] SEPARATOR")
            else:
                w = toolbar.widgetForAction(action)
                if w:
                    print(f"[DEBUG]   [{i}] '{action.text()}' widget at {w.pos()} size {w.size()}")
                else:
                    print(f"[DEBUG]   [{i}] '{action.text()}' NO WIDGET")
            
        # Update each overlay by looking up its current widget from the action
        updated = 0
        for overlay in self._button_overlays:
            widget = None
            action_text = "Unknown"

            if hasattr(overlay, '_action'):
                # ButtonDragOverlay - has _action attribute
                action = overlay._action
                action_text = action.text() if not action.isSeparator() else "SEPARATOR"
                if action.isSeparator():
                    print(f"[DEBUG] Overlay for separator - handled separately")
                    continue
                # Get current widget for this action
                widget = toolbar.widgetForAction(action)
            elif hasattr(overlay, '_target_widget'):
                # WidgetDragOverlay - has _target_widget attribute
                widget = overlay._target_widget
                action_text = overlay._label if hasattr(overlay, '_label') else "Widget"
            else:
                print(f"[DEBUG] Overlay has no _action or _target_widget attribute, skipping")
                continue

            if widget and widget.width() > 0:
                # Stop the overlay's internal positioning timer
                if hasattr(overlay, '_position_timer') and overlay._position_timer.isActive():
                    overlay._position_timer.stop()
                    print(f"[DEBUG] Stopped timer for '{action_text}'")
                # Get current widget position
                btn_pos = widget.mapTo(self, QtCore.QPoint(0, 0))
                old_geo = overlay.geometry()
                overlay.setGeometry(btn_pos.x(), btn_pos.y(), widget.width(), widget.height())
                new_geo = overlay.geometry()
                print(f"[DEBUG] UPDATED '{action_text}': {old_geo} -> {new_geo} (widget at {btn_pos.x()},{btn_pos.y()})")
                updated += 1
            else:
                print(f"[DEBUG] NO WIDGET for '{action_text}' - cannot position!")
        
        # Raise button overlays above the main toolbar overlay
        for overlay in self._button_overlays:
            overlay.raise_()
        print(f"[DEBUG] ===== UPDATED {updated}/{len(self._button_overlays)} OVERLAYS =====")

    def eventFilter(self, obj, event) -> bool:
        """Handle events for toolbar during edit mode."""
        if not self._toolbar_edit_mode:
            return super().eventFilter(obj, event)
        
        # Debug: print events for draggable buttons
        if obj.property("_is_draggable_button"):
            action = obj.property("_toolbar_action")
            action_text = action.text() if action else "unknown"
            if event.type() in [QtCore.QEvent.Type.MouseButtonPress, 
                               QtCore.QEvent.Type.MouseButtonRelease,
                               QtCore.QEvent.Type.MouseMove,
                               QtCore.QEvent.Type.DragEnter]:
                print(f"[DEBUG-BUTTON] Event {event.type().name} on draggable button '{action_text}'")
            elif event.type() == QtCore.QEvent.Type.MouseButtonDblClick:
                print(f"[DEBUG-BUTTON] DOUBLE CLICK on draggable button '{action_text}'")
        
        # Handle drag from existing toolbar buttons
        if event.type() == QtCore.QEvent.Type.MouseButtonPress:
            # Check if this is a toolbar button being dragged
            if obj.property("_is_draggable_button"):
                mouse_event = event
                if mouse_event.button() == QtCore.Qt.MouseButton.LeftButton:
                    action = obj.property("_toolbar_action")
                    if action:
                        print(f"[DEBUG] Starting drag of existing toolbar button: {action.text()}")
                        
                        # Create drag data for reordering
                        data = {
                            "type": "reorder",
                            "action_text": action.text(),
                            "action_data": action.data() if action.data() else None
                        }
                        
                        drag = QtGui.QDrag(obj)
                        mime = QtCore.QMimeData()
                        mime.setText(json.dumps(data))
                        drag.setMimeData(mime)
                        
                        # Set drag pixmap to show what we're dragging
                        pixmap = obj.grab() if hasattr(obj, 'grab') else None
                        if pixmap and not pixmap.isNull():
                            drag.setPixmap(pixmap)
                            drag.setHotSpot(QtCore.QPoint(pixmap.width()//2, pixmap.height()//2))
                        
                        result = drag.exec(QtCore.Qt.DropAction.MoveAction)
                        print(f"[DEBUG] Reorder drag result: {result}")
                        return True
                    
        return super().eventFilter(obj, event)
            
    def _on_toolbar_drag_move(self, event: QtGui.QDragMoveEvent) -> None:
        """Handle drag moving over toolbar."""
        if event.mimeData().hasText():
            event.acceptProposedAction()
            
    def _on_toolbar_drop(self, event: QtGui.QDropEvent) -> None:
        """Handle drop on toolbar - adds widget to toolbar."""
        if not event.mimeData().hasText():
            return
            
        try:
            data = json.loads(event.mimeData().text())
            item = self._create_menu_item_from_drop(data)
            if item:
                # Get drop position and calculate insertion index
                drop_pos = event.pos()
                insert_position = self._calculate_toolbar_insert_position(drop_pos)
                
                # Add to palette's config at correct position
                if self._toolbox_palette:
                    self._toolbox_palette.add_item_to_config("toolbar", item, position=insert_position)
                # Add to actual toolbar at correct position
                self._add_item_to_main_toolbar(item, drop_pos=drop_pos)
                # Save the toolbar order (new items need to have their position saved)
                QtCore.QTimer.singleShot(100, self._save_toolbar_order_to_config)
                event.acceptProposedAction()
        except json.JSONDecodeError:
            pass
            
    def _create_menu_item_from_drop(self, data: dict) -> MenuItemDef | None:
        """Create a MenuItemDef from drag data."""
        from lib_gui.menubuilder.models import MenuItemDef, WidgetType, MenuLocation
        import uuid
        
        item_id = f"item_{uuid.uuid4().hex[:8]}"
        
        if data.get("type") == "button":
            from lib_gui.menubuilder.models import BUTTON_LIBRARY
            btn_id = data.get("button_id")
            if btn_id in BUTTON_LIBRARY:
                btn_def = BUTTON_LIBRARY[btn_id]
                return MenuItemDef(
                    id=item_id,
                    label=btn_def.label,
                    widget_type=WidgetType.BUTTON,
                    location=MenuLocation.TOOLBAR,
                    icon=btn_def.icon,
                    tooltip=btn_def.tooltip,
                    command_id=btn_def.command.id
                )
        elif data.get("type") == "macro":
            return MenuItemDef(
                id=item_id,
                label=data.get("macro_name", "Macro"),
                widget_type=WidgetType.BUTTON,
                location=MenuLocation.TOOLBAR,
                icon="player-play",
                macro_id=data.get("macro_id")
            )
        elif data.get("type") == "widget":
            print(f"[DEBUG-DROP] Creating widget item with macro_id={data.get('macro_id')}")
            return MenuItemDef(
                id=item_id,
                label=data.get("widget_label", "Item"),
                widget_type=WidgetType(data.get("widget_type")),
                location=MenuLocation.TOOLBAR,
                icon=data.get("widget_id"),  # widget_id contains the icon name
                macro_id=data.get("macro_id")  # Include assigned macro
            )
        elif data.get("widget_type") and data.get("id"):
            # Data is already a MenuItemDef (custom buttons, etc.)
            print(f"[DEBUG-DROP] Creating item from MenuItemDef data: {data.get('label')}")
            try:
                print(f"[DEBUG-DROP] Entered try block")
                # Reuse the existing ID or generate new one
                existing_id = data.get("id")
                print(f"[DEBUG-DROP] existing_id: {existing_id}")
                if existing_id and existing_id.startswith("custom_"):
                    # For custom buttons, generate new ID on each drop
                    item_id = f"item_{uuid.uuid4().hex[:8]}"
                else:
                    item_id = existing_id or f"item_{uuid.uuid4().hex[:8]}"
                print(f"[DEBUG-DROP] item_id: {item_id}")
                
                widget_type = WidgetType(data.get("widget_type"))
                print(f"[DEBUG-DROP] widget_type: {widget_type}")
                
                location = MenuLocation(data.get("location", "toolbar"))
                print(f"[DEBUG-DROP] location: {location}")
                
                item = MenuItemDef(
                    id=item_id,
                    label=data.get("label", "Item"),
                    widget_type=widget_type,
                    location=location,
                    icon=data.get("icon"),
                    tooltip=data.get("tooltip"),
                    command_id=data.get("command_id"),
                    macro_id=data.get("macro_id")
                )
                print(f"[DEBUG-DROP] Created MenuItemDef: {item}")
                return item
            except Exception as e:
                import traceback
                print(f"[DEBUG-DROP] Error creating MenuItemDef: {e}")
                traceback.print_exc()
                return None
        print(f"[DEBUG-DROP] No matching condition for data: {data}")
        return None
        
    def _add_item_to_main_toolbar(self, item: MenuItemDef, drop_pos: QtCore.QPoint = None) -> None:
        """Add a menu item to the main toolbar at optional drop position."""
        from lib_gui.menubuilder.widgets import load_svg_icon
        from lib_gui.menubuilder.models import WidgetType
        
        for toolbar in self.findChildren(QtWidgets.QToolBar):
            if toolbar.objectName() == "MainToolBar":
                # Handle separator specially
                if item.widget_type == WidgetType.SEPARATOR:
                    # Find insertion position if drop_pos provided
                    insert_before = self._find_toolbar_insert_action(toolbar, drop_pos)
                    if insert_before:
                        print(f"[DEBUG] New separator inserting before: {insert_before.text()}")

                    # Create and add separator action
                    sep_action = toolbar.addSeparator()
                    if sep_action:
                        sep_action.setProperty("item_id", item.id)
                    if insert_before:
                        # Remove and re-insert at correct position
                        toolbar.removeAction(sep_action)
                        toolbar.insertAction(insert_before, sep_action)
                    
                    print(f"[DEBUG] Added separator to toolbar: {item.id}")
                    
                    # Refresh overlays to include new separator
                    QtCore.QTimer.singleShot(150, self._refresh_button_overlays)
                    return
                
                # Handle spacer (stretch) specially
                if item.widget_type == WidgetType.SPACER:
                    spacer = QtWidgets.QWidget()
                    spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
                    toolbar.addWidget(spacer)
                    print(f"[DEBUG] Added spacer to toolbar")
                    return
                
                # Handle font name dropdown - shows fonts with preview
                if item.widget_type == WidgetType.FONT_NAME:
                    from lib_gui.widgets import FontNameDropdown
                    font_combo = FontNameDropdown()
                    font_combo.setToolTip(item.label)
                    font_combo.setProperty("_toolbar_widget_label", item.label)
                    font_combo.setProperty("item_id", item.id)
                    font_combo.font_selected.connect(lambda font_name: self._on_font_selected(font_name, item))
                    
                    # Find insertion position if drop_pos provided
                    insert_before = self._find_toolbar_insert_action(toolbar, drop_pos)
                    if insert_before:
                        toolbar.insertWidget(insert_before, font_combo)
                    else:
                        toolbar.addWidget(font_combo)
                    print(f"[DEBUG] Added font name dropdown '{item.label}' to toolbar")

                    # Add overlay for the font dropdown widget after layout settles
                    QtCore.QTimer.singleShot(300, lambda: self._add_widget_overlay_and_refresh(font_combo, item.label))
                    return

                # Handle font size dropdown
                if item.widget_type == WidgetType.FONT_SIZE:
                    from lib_gui.widgets import FontSizeDropdown
                    font_size_combo = FontSizeDropdown()
                    font_size_combo.setToolTip(item.label)
                    font_size_combo.setProperty("_toolbar_widget_label", item.label)
                    font_size_combo.setProperty("item_id", item.id)
                    font_size_combo.size_selected.connect(lambda size: self._on_font_size_selected(size, item))
                    
                    # Find insertion position if drop_pos provided
                    insert_before = self._find_toolbar_insert_action(toolbar, drop_pos)
                    if insert_before:
                        toolbar.insertWidget(insert_before, font_size_combo)
                    else:
                        toolbar.addWidget(font_size_combo)
                    print(f"[DEBUG] Added font size dropdown '{item.label}' to toolbar")

                    # Add overlay for the font size widget after layout settles
                    QtCore.QTimer.singleShot(300, lambda: self._add_widget_overlay_and_refresh(font_size_combo, item.label))
                    return

                # Handle font color picker - "A" with colored underline
                if item.widget_type == WidgetType.FONT_COLOR:
                    from lib_gui.mini_color_picker import MiniFontColorButton
                    font_color_btn = MiniFontColorButton()
                    font_color_btn.setToolTip(item.label)
                    font_color_btn.setProperty("_toolbar_widget_label", item.label)
                    font_color_btn.setProperty("item_id", item.id)
                    font_color_btn.color_changed.connect(lambda color: self._on_font_color_changed(color, item))
                    
                    # Find insertion position if drop_pos provided
                    insert_before = self._find_toolbar_insert_action(toolbar, drop_pos)
                    if insert_before:
                        toolbar.insertWidget(insert_before, font_color_btn)
                    else:
                        toolbar.addWidget(font_color_btn)
                    print(f"[DEBUG] Added font color picker '{item.label}' to toolbar")

                    # Add overlay for the font color widget
                    QtCore.QTimer.singleShot(300, lambda: self._add_widget_overlay_and_refresh(font_color_btn, item.label))
                    return

                # Handle color picker (cell fill) - paint bucket with colored block
                if item.widget_type == WidgetType.COLOR_PICKER:
                    from lib_gui.mini_color_picker import MiniCellFillButton
                    fill_btn = MiniCellFillButton()
                    fill_btn.setToolTip(item.label)
                    fill_btn.setProperty("_toolbar_widget_label", item.label)
                    fill_btn.setProperty("item_id", item.id)
                    fill_btn.color_changed.connect(lambda color: self._on_cell_fill_changed(color, item))
                    
                    # Find insertion position if drop_pos provided
                    insert_before = self._find_toolbar_insert_action(toolbar, drop_pos)
                    if insert_before:
                        toolbar.insertWidget(insert_before, fill_btn)
                    else:
                        toolbar.addWidget(fill_btn)
                    print(f"[DEBUG] Added cell fill color picker '{item.label}' to toolbar")

                    # Add overlay for the color picker widget
                    QtCore.QTimer.singleShot(300, lambda: self._add_widget_overlay_and_refresh(fill_btn, item.label))
                    return

                # Create action with icon
                action = QtGui.QAction(self)
                action.setText(item.label)
                
                # Set icon if available
                if item.icon:
                    try:
                        icon = load_svg_icon(item.icon, 16, "#374151")
                        action.setIcon(icon)
                        action.setToolTip(item.label)
                    except Exception as e:
                        print(f"[DEBUG] Failed to load icon {item.icon}: {e}")
                
                # Set tooltip if provided
                if item.tooltip:
                    action.setToolTip(item.tooltip)
                
                # Connect action based on type
                if item.macro_id:
                    macro_id = item.macro_id
                    action.setData(macro_id)
                    print(f"[DEBUG-BUTTON] Connecting macro '{macro_id}' to action '{item.label}'")
                    
                    # Create a wrapper to add debug
                    def run_macro_with_debug(mid):
                        print(f"[DEBUG-BUTTON] Action triggered for macro '{mid}'")
                        self._run_macro(mid)
                    
                    action.triggered.connect(lambda checked, mid=macro_id: run_macro_with_debug(mid))
                elif item.command_id:
                    action.setData(item.command_id)
                
                # Set item_id property before adding to toolbar
                action.setProperty("item_id", item.id)
                
                # Find insertion position if drop_pos provided
                insert_before = self._find_toolbar_insert_action(toolbar, drop_pos)
                if insert_before:
                    print(f"[DEBUG] New item inserting before: {insert_before.text()}")

                # Add action at position or at end
                if insert_before:
                    toolbar.insertAction(insert_before, action)
                else:
                    toolbar.addAction(action)
                
                # Get the toolbutton widget and configure it (may not exist immediately)
                widget = toolbar.widgetForAction(action)
                if widget:
                    # Show only icon, not text
                    widget.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
                    
                    # Make selectable and draggable in edit mode
                    if self._toolbar_edit_mode:
                        widget.setCheckable(True)
                        widget.setMouseTracking(True)
                        
                        # Connect click with debug
                        def on_edit_mode_click(checked, a=action, w=widget):
                            print(f"[DEBUG-BUTTON] Edit mode click on '{a.text()}', checked={checked}")
                            self._on_toolbar_button_clicked(a)
                        widget.clicked.connect(on_edit_mode_click)
                        
                        # Make draggable for reordering
                        widget.installEventFilter(self)
                        widget.setProperty("_toolbar_action", action)
                        widget.setProperty("_is_draggable_button", True)
                        widget.setProperty("item_id", item.id)
                        print(f"[DEBUG-BUTTON] Configured button '{action.text()}' for edit mode (checkable, mouse tracking, event filter)")
                        # Create overlay and add to list immediately
                        if hasattr(self, '_button_overlays'):
                            drag_overlay = ButtonDragOverlay(widget, action, self)
                            drag_overlay.show()
                            drag_overlay.raise_()
                            self._button_overlays.append(drag_overlay)
                            print(f"[DEBUG] Created overlay for new button: {action.text()} at {drag_overlay.geometry()}")
                    else:
                        # Not in edit mode - add click debug to trace button presses
                        def on_button_click(checked, act=action, lbl=item.label):
                            print(f"[DEBUG-BUTTON] Widget clicked: '{lbl}', action text='{act.text()}', macro_id='{act.data()}'")
                        widget.clicked.connect(on_button_click)
                        print(f"[DEBUG-BUTTON] Added click debug to widget for '{item.label}'")
                
                # Refresh all overlays after toolbar layout settles
                QtCore.QTimer.singleShot(200, lambda: self._add_new_button_overlay(action))
                break
                
    def _add_new_button_overlay(self, action: QtGui.QAction) -> None:
        """Add or update overlay for a new button after layout settles."""
        if not self._edit_toolbar or not hasattr(self, '_button_overlays'):
            return
        
        toolbar = self._edit_toolbar
        widget = toolbar.widgetForAction(action)
        
        if not widget or widget.width() <= 0:
            print(f"[DEBUG] New button widget not ready, retrying...")
            QtCore.QTimer.singleShot(100, lambda: self._add_new_button_overlay(action))
            return
        
        # Check if overlay already exists for this action
        existing_overlay = None
        for overlay in self._button_overlays:
            if hasattr(overlay, '_action') and overlay._action == action:
                existing_overlay = overlay
                break
        
        if not existing_overlay:
            print(f"[DEBUG] Creating new overlay for '{action.text()}'")
            # Stop any internal timer
            overlay = ButtonDragOverlay(widget, action, self)
            overlay.show()
            overlay.raise_()
            self._button_overlays.append(overlay)
        
        # Now refresh all overlays to ensure proper positioning
        print(f"[DEBUG] Refreshing all overlays for new button '{action.text()}'")
        self._refresh_button_overlays()
        
    def _add_widget_overlay(self, widget: QtWidgets.QWidget, label: str) -> None:
        """Add overlay for a generic widget (like QComboBox dropdown)."""
        print(f"[DEBUG] _add_widget_overlay called for '{label}'")
        if not self._edit_toolbar:
            print(f"[DEBUG] No _edit_toolbar, skipping")
            return
        if not hasattr(self, '_button_overlays'):
            print(f"[DEBUG] No _button_overlays, skipping")
            return
        
        # Check if widget is ready
        print(f"[DEBUG] Widget '{label}' width={widget.width()}, height={widget.height()}")
        if widget.width() <= 0:
            print(f"[DEBUG] Widget '{label}' not ready, retrying...")
            QtCore.QTimer.singleShot(100, lambda: self._add_widget_overlay(widget, label))
            return
        
        # Create a wrapper overlay for the widget
        from PySide6 import QtWidgets, QtCore, QtGui
        
        class WidgetDragOverlay(QtWidgets.QWidget):
            """Overlay for generic toolbar widgets."""
            
            def __init__(self, target_widget: QtWidgets.QWidget, label: str, main_window: 'MainWindow'):
                super().__init__(main_window)
                self._target_widget = target_widget
                self._label = label
                self._main_window = main_window
                self._deleting = False

                # Make visible semi-transparent red overlay
                self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
                self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, False)
                self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.WindowStaysOnTopHint)
                self.setStyleSheet("""
                    background: rgba(239, 68, 68, 0.5);
                    border: 2px solid #DC2626;
                    color: #991B1B;
                    font-size: 8px;
                """)
                self.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.OpenHandCursor))

                # Add label
                layout = QtWidgets.QVBoxLayout(self)
                layout.setContentsMargins(2, 2, 2, 2)
                label_widget = QtWidgets.QLabel(label[:8] if label else "WIDGET")
                label_widget.setStyleSheet("background: transparent; border: none; font-size: 7px; color: #991B1B;")
                label_widget.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(label_widget)
                
                # Position overlay
                self._position_timer = QtCore.QTimer(self)
                self._position_timer.timeout.connect(self._update_position)
                self._update_position()
                
            def _update_position(self):
                if getattr(self, '_deleting', False):
                    return
                try:
                    if self._target_widget and self._main_window:
                        widget_pos = self._target_widget.mapTo(self._main_window, QtCore.QPoint(0, 0))
                        widget_size = self._target_widget.size()
                        if widget_size.width() > 0 and widget_size.height() > 0:
                            self.setGeometry(widget_pos.x(), widget_pos.y(), widget_size.width(), widget_size.height())
                            self._position_timer.stop()
                            print(f"[DEBUG] Widget overlay positioned at ({widget_pos.x()}, {widget_pos.y()}) size {widget_size.width()}x{widget_size.height()}")
                        elif not self._position_timer.isActive():
                            self._position_timer.start(50)
                except RuntimeError:
                    self._deleting = True
                    self.hide()

            def paintEvent(self, event):
                """Draw the red overlay rectangle."""
                super().paintEvent(event)
                painter = QtGui.QPainter(self)
                # Draw semi-transparent red fill
                painter.fillRect(self.rect(), QtGui.QColor(239, 68, 68, 128))
                # Draw border
                painter.setPen(QtGui.QPen(QtGui.QColor(220, 38, 38), 2))
                painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
                # Draw text label
                painter.setPen(QtGui.QColor(153, 27, 27))
                painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, self._label[:8])

            def mousePressEvent(self, event: QtGui.QMouseEvent):
                from PySide6 import QtCore, QtGui
                if event.button() == QtCore.Qt.MouseButton.LeftButton:
                    if getattr(self, '_deleting', False):
                        return
                    if not self._main_window or getattr(self._main_window, '_edit_toolbar', None) is None:
                        return
                    drag = QtGui.QDrag(self)
                    mime = QtCore.QMimeData()
                    # Store widget info for drag - use 'widget_reorder' to indicate moving existing widget
                    data = {
                        'type': 'widget_reorder',
                        'widget_type': 'dropdown',
                        'widget_id': self._label.lower().replace(' ', '_'),
                        'widget_label': self._label
                    }
                    import json
                    mime.setText(json.dumps(data))
                    drag.setMimeData(mime)

                    # Set drag pixmap from widget - same as toolbox editor
                    pixmap = self.grab()
                    if pixmap and not pixmap.isNull():
                        drag.setPixmap(pixmap)
                        drag.setHotSpot(QtCore.QPoint(pixmap.width()//2, pixmap.height()//2))

                    result = drag.exec(QtCore.Qt.DropAction.MoveAction)

                    # Check cursor position to determine if we should delete or reorder
                    cursor_pos = QtGui.QCursor.pos()
                    toolbar_rect = self._main_window._edit_toolbar.geometry()
                    toolbar_top = self._main_window._edit_toolbar.mapToGlobal(QtCore.QPoint(0, 0)).y()
                    toolbar_bottom = toolbar_top + toolbar_rect.height()
                    cursor_y = cursor_pos.y()
                    
                    # Delete only if cursor is vertically outside toolbar (above or below)
                    vertically_outside = cursor_y < toolbar_top or cursor_y > toolbar_bottom
                    
                    if vertically_outside and result == QtCore.Qt.DropAction.IgnoreAction:
                        # Dropped outside toolbar and not accepted - delete the widget
                        self._main_window._delete_toolbar_widget(self._label)
                    elif result == QtCore.Qt.DropAction.IgnoreAction:
                        # Drop was not accepted by any target, but inside toolbar - reorder manually
                        toolbar = self._main_window._edit_toolbar
                        drop_pos = toolbar.mapFromGlobal(cursor_pos)
                        self._main_window._reorder_toolbar_widget(self._label, drop_pos)
                    # If result == MoveAction, the drop was accepted by ToolbarDropOverlay which already handled it
        
        # Create and show overlay
        overlay = WidgetDragOverlay(widget, label, self)
        overlay.show()
        overlay.raise_()
        self._button_overlays.append(overlay)
        print(f"[DEBUG] Added widget overlay for '{label}'")

    def _add_widget_overlay_and_refresh(self, widget: QtWidgets.QWidget, label: str) -> None:
        """Add overlay for a widget and refresh all overlays to ensure proper alignment."""
        self._add_widget_overlay(widget, label)
        # Force refresh of all overlays after adding a new widget to prevent misalignment
        QtCore.QTimer.singleShot(50, self._refresh_button_overlays)
                
    _NO_WIDGET_VALUE = object()

    def _run_macro(self, macro_id: str, widget_value: Any = _NO_WIDGET_VALUE) -> None:
        """Run a recorded macro, optionally feeding it a widget value."""
        print(f"[DEBUG-MACRO] _run_macro called with macro_id='{macro_id}'")
        from pathlib import Path

        from lib_utils.paths import OM_MACROS_DIR
        macros_dir = OM_MACROS_DIR
        macro_file = macros_dir / f"{macro_id}.openm"
        print(f"[DEBUG-MACRO] Looking for macro file: {macro_file}")

        if not macro_file.exists():
            print(f"[DEBUG-MACRO] Macro file NOT FOUND: {macro_file}")
            QtWidgets.QMessageBox.warning(self, "Macro Not Found", f"Macro not found: {macro_id}")
            return

        print(f"[DEBUG-MACRO] Macro file found, using injected runner")
        runner = self._macro_runner
        if runner is None:
            print("[DEBUG-MACRO] No macro runner injected — cannot play macro")
            QtWidgets.QMessageBox.warning(self, "Macro Error", "Macro runner not available")
            return

        # Build context values: widget value + current selection
        context_values: dict[str, Any] = {}
        if widget_value is not self._NO_WIDGET_VALUE:
            context_values["widget_value"] = widget_value   # public macro placeholder
            context_values["_widget_value"] = widget_value  # legacy/internal compatibility
            print(f"[DEBUG-MACRO] Injecting widget value: {widget_value!r}")

        # Inject current GUI selection so {{selection}} resolves in headless runner
        selection_addrs: list[str] = []
        gui_port = getattr(self, 'gui_port', None)
        if gui_port is not None:
            selection_addrs = gui_port.selection_addresses()
        else:
            # Fallback: direct grid access (should only happen in tests without gui_port)
            grid = getattr(self, '_table', None)
            if grid is not None and hasattr(grid, 'selected_addresses'):
                selection_addrs = grid.selected_addresses()
        if selection_addrs:
            # For single-cell macros, inject the first address
            context_values["selection"] = selection_addrs[0]
            print(f"[DEBUG-MACRO] Injecting selection: {selection_addrs[0]!r}")
        else:
            print("[DEBUG-MACRO] No GUI selection available")

        # Play the macro through the recorder
        print(f"[DEBUG-MACRO] Playing macro '{macro_id}' through recorder")
        recorder = self._recorder
        if recorder is None:
            print("[DEBUG-MACRO] No recorder injected — cannot play macro")
            QtWidgets.QMessageBox.warning(self, "Macro Error", "Macro recorder not available")
            return
        errors = recorder.play_macro(macro_id, runner, context_values=context_values if context_values else None)
        print(f"[DEBUG-MACRO] Macro execution completed, errors: {errors}")

        if errors:
            QtWidgets.QMessageBox.warning(self, "Macro Error", f"Macro errors:\n" + "\n".join(errors))
        else:
            self.statusBar().showMessage(f"Macro completed: {macro_id}", 3000)
            # Refresh rule panel and views so macro mutations are visible
            self._rule_panel.rebuild()
            self._sync_rule_bar_from_current()
            for controller in self._iter_workspace_controllers():
                controller.refresh_table()
                controller.rebuild_rule_panel()

    def _on_toolbar_button_clicked(self, action: QtGui.QAction) -> None:
        """Handle toolbar button click in edit mode - opens properties."""
        print(f"[DEBUG-BUTTON] _on_toolbar_button_clicked ENTER - action='{action.text()}', edit_mode={self._toolbar_edit_mode}")
        
        if not self._toolbar_edit_mode:
            print(f"[DEBUG-BUTTON] Not in edit mode, returning")
            return

        # Notify palette to show properties for this item
        if self._toolbox_palette:
            # Get the item_id from the property (not data which holds macro_id/command_id)
            item_id = action.property("item_id")
            print(f"[DEBUG-BUTTON] Opening properties for item_id={item_id} (from property)")
            print(f"[DEBUG-BUTTON]   action.data()={action.data()}")
            if item_id:
                self._toolbox_palette.set_selected_item(item_id)
        else:
            print(f"[DEBUG-BUTTON] No toolbox palette available")
            
        print(f"[DEBUG-BUTTON] _on_toolbar_button_clicked EXIT")

    def _on_font_color_changed(self, color_hex: str, item: MenuItemDef) -> None:
        """Handle font color selection from MiniFontColorButton."""
        if self._toolbar_edit_mode:
            return

        if color_hex:
            print(f"[DEBUG] Font color selected: {color_hex}")
            # TODO: Apply font color to selected cells/text
        else:
            print("[DEBUG] Font color set to automatic")
            # TODO: Reset font color to automatic/default

    def _on_cell_fill_changed(self, color_hex: str, item: MenuItemDef) -> None:
        """Handle cell fill/background color selection from MiniCellFillButton."""
        if self._toolbar_edit_mode:
            return

        if color_hex:
            print(f"[DEBUG] Cell fill color selected: {color_hex}")
            # TODO: Apply background color to selected cells
        else:
            print("[DEBUG] Cell fill cleared (no fill)")
            # TODO: Clear cell background color

    def _on_font_selected(self, font_name: str, item: MenuItemDef) -> None:
        """Handle font selection from FontNameDropdown."""
        if self._toolbar_edit_mode:
            return

        print(f"[DEBUG] Font selected: {font_name}")
        # TODO: Apply font to selected cells/text

    def _on_font_size_selected(self, size: float, item: MenuItemDef) -> None:
        """Handle font size selection from FontSizeDropdown."""
        if self._toolbar_edit_mode:
            return

        print(f"[DEBUG] Font size selected: {size}")
        # TODO: Apply font size to selected cells/text

    def _refresh_toolbar_from_config(self, config: ToolboxConfig) -> None:
        """Refresh toolbar display from config (after property update)."""
        # Find and update the toolbar button label
        for toolbar in self.findChildren(QtWidgets.QToolBar):
            if toolbar.objectName() == "MainToolBar":
                for action in toolbar.actions():
                    # Update labels from config
                    pass  # Implementation depends on action data storage

    def _ensure_default_view(self) -> None:
        """Ensure the workspace has at least one TableViewSpec to display.

        When loading a workspace from JSON that contains cubes but no views,
        the GUI would otherwise come up with an empty main area and no tabs.
        To make such workspaces usable out of the box, we create a simple
        default view for the first cube using the same conventions as
        _on_create_cube/_on_add_view.
        """

        snapshot = self.workspace_read_model.workspace_snapshot()
        if snapshot and snapshot.get("view_snapshots"):
            return

        cube_snapshots = list(snapshot.get("cube_snapshots", {}).values()) if snapshot else []
        if not cube_snapshots:
            return

        cube = cube_snapshots[0]
        dim_ids = [d for d in cube.get("dimension_ids", []) if d != "@"]
        if not dim_ids:
            return

        row_dim = dim_ids[0]
        col_dim = dim_ids[1] if len(dim_ids) > 1 else None
        page_dims = dim_ids[2:] if len(dim_ids) > 2 else []
        self.session.execute(
            "create_view",
            name=f"View of {cube.get('name', '')}",
            cube_id=cube.get("id"),
            row_dims=[row_dim],
            col_dims=[col_dim] if col_dim else [],
            page_dim_ids=["@"] + page_dims,
        )

    @QtCore.Slot()
    def _on_create_cube(self) -> None:
        # Dialog: cube name + multi-select dimensions
        dims = self.workspace_read_model.list_dimensions()
        dims.sort(key=lambda d: d.get("name", ""))
        if not dims:
            QtWidgets.QMessageBox.information(self, "New Cube", "Create at least one dimension first.")
            return

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("New Cube")
        form = QtWidgets.QFormLayout(dlg)

        le_name = QtWidgets.QLineEdit(dlg)
        le_name.setPlaceholderText("Cube name")
        form.addRow("Name", le_name)

        dim_list = QtWidgets.QListWidget(dlg)
        dim_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.MultiSelection)
        for d in dims:
            item = QtWidgets.QListWidgetItem(d.get("name", ""), dim_list)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, d.get("id", ""))
        form.addRow("Dimensions", dim_list)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return

        name = le_name.text().strip()
        selected = [dim_list.item(i) for i in range(dim_list.count()) if dim_list.item(i).isSelected()]
        dim_ids = [it.data(QtCore.Qt.ItemDataRole.UserRole) for it in selected if isinstance(it.data(QtCore.Qt.ItemDataRole.UserRole), str)]

        if not name or not dim_ids:
            QtWidgets.QMessageBox.warning(self, "New Cube", "Please provide a name and select at least one dimension.")
            return

        # Create cube and a default view.
        # For cubes with a single dimension, we want a 1D view: that dimension on rows only.
        # For cubes with 2+ dimensions, use row=first dim, col=second dim, page=rest.
        result = self.session.execute(
            "create_cube",
            name=name,
            dimension_ids=dim_ids,
        )
        if not result.success:
            QtWidgets.QMessageBox.warning(self, "New Cube", f"Failed to create cube: {result.error or 'create_cube failed'}")
            return
        cube_id = result.data.get("id") if result.data else None
        if not cube_id:
            QtWidgets.QMessageBox.warning(self, "New Cube", "Cube creation returned no ID")
            return

        self._create_default_view_for_cube(name, cube_id, dim_ids)
        self._finalize_structure_change()

    @QtCore.Slot()
    def _on_delete_cube(self) -> None:
        cubes = self.workspace_read_model.list_cubes()
        if not cubes:
            QtWidgets.QMessageBox.information(self, "Delete Cube", "No cubes to delete.")
            return

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Delete Cube")
        form = QtWidgets.QFormLayout(dlg)

        cb_cube = QtWidgets.QComboBox(dlg)
        for c in cubes:
            cb_cube.addItem(c.get("name", ""), c.get("id", ""))

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow("Cube", cb_cube)
        form.addRow(btns)

        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return

        cube_id = cb_cube.currentData()
        if not isinstance(cube_id, str):
            return

        cube = self.workspace_read_model.get_cube(cube_id)
        cube_name = cube.get("name", "") if cube else ""
        reply = QtWidgets.QMessageBox.question(
            self,
            "Confirm Delete",
            f"Delete cube '{cube_name}' and all its views?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.session.execute("delete_cube", cube_id=cube_id)

        self._dock_browser.rebuild()
        self._reload_active_view()

    @QtCore.Slot(str)
    def _on_add_item_to_dim(self, dim_id: str) -> None:
        dim_dto = None
        if self.gui_view_model is not None:
            dim_dto = self.gui_view_model.get_dimension_snapshot(dim_id)
        if not dim_dto:
            dim_dto = self.session.query("dimension_detail", dim_id=dim_id)
            if dim_dto and self.gui_view_model is not None:
                self.gui_view_model.update_dimension_snapshot(dim_id, dim_dto)
        dim_name = dim_dto.get("name", dim_id) if dim_dto else dim_id
        name, ok = QtWidgets.QInputDialog.getText(
            self, f"Add item to '{dim_name}'", "Item name"
        )
        if not ok or not name.strip():
            return
        result = self.session.execute(
            "create_dimension_item",
            dim_id=dim_id,
            name=name.strip(),
        )
        if not result.success:
            QtWidgets.QMessageBox.warning(self, "Add Item", result.error or "create_dimension_item failed")
            return
        item_id = result.data.get("id") if result.data else None

        self._dock_browser.rebuild()
        self._reload_active_view()
        # Mark workspace as dirty after adding dimension item
        self._mark_dirty(True)

    @QtCore.Slot()
    def _on_add_view(self) -> None:
        cubes = self.workspace_read_model.list_cubes()
        if not cubes:
            QtWidgets.QMessageBox.information(self, "New View", "No cubes in workspace yet.")
            return
        
        while True:
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("New View")
            form = QtWidgets.QFormLayout(dlg)
            le_name = QtWidgets.QLineEdit(dlg)
            le_name.setText("New View")
            cb_cube = QtWidgets.QComboBox(dlg)
            for c in cubes:
                cb_cube.addItem(c.get("name", ""), c.get("id", ""))
            btns = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel, dlg
            )
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            form.addRow("Name", le_name)
            form.addRow("Cube", cb_cube)
            form.addRow(btns)
            if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
                return
            name = le_name.text().strip() or "View"
            
            # Check for duplicate view names (case-insensitive)
            name_clean = name.casefold()
            existing_views = self.workspace_read_model.list_views()
            if any(v.get("name", "").casefold() == name_clean for v in existing_views):
                QtWidgets.QMessageBox.warning(
                    self,
                    "New View",
                    f"A view named '{name}' already exists. Please choose a different name.",
                )
                continue

            cube_id = cb_cube.currentData()
            if not isinstance(cube_id, str):
                return
            cube = self.workspace_read_model.get_cube(cube_id)
            if not cube:
                QtWidgets.QMessageBox.warning(self, "New View", "Cube not found.")
                return
            dim_ids = [d for d in cube.get("dimension_ids", []) if d != "@"]
            if not dim_ids:
                QtWidgets.QMessageBox.warning(self, "New View", "Cube has no dimensions.")
                return

            row_dim = dim_ids[0]
            col_dim = dim_ids[1] if len(dim_ids) > 1 else None
            page_dims = dim_ids[2:] if len(dim_ids) > 2 else []
            result = self.session.execute(
                "create_view",
                name=name,
                cube_id=cube_id,
                row_dims=[row_dim],
                col_dims=[col_dim] if col_dim else [],
                page_dim_ids=["@"] + page_dims,
            )
            if not result.success:
                QtWidgets.QMessageBox.warning(self, "New View", str(result.error or "View creation failed"))
                continue
            view_id = result.data.get("id") if result.data else None
            if not view_id:
                return
            break
        self._dock_browser.rebuild()
        self._reload_active_view()
        # Switch to the new tab.
        self._workspace.focus_view(view_id)

    @QtCore.Slot()
    def _on_new(self) -> None:
        # Check for unsaved changes first
        if not self._check_unsaved_changes():
            return
        result = self.session.execute("create_new_workspace")
        if not result.success:
            logger.error("New workspace failed: %s", result.error)
            self._flash_status_message(f"New workspace failed: {result.error}")
            return
        # Preserve dependency tracking state from checkbox
        desired_tracking = self._dock_perf._toggle.isChecked()
        desired_mt = self._dock_perf._mt_toggle.isChecked()
        self.session.execute("set_dependency_tracking", enabled=desired_tracking)
        # Re-bootstrap DTO cache so Model Browser and view tabs show cubes/views
        # from the newly-created workspace instead of stale snapshots.
        if self.gui_view_model is not None:
            self._bootstrap_view_model()
        self.grid_read_model.invalidate_cache()
        self._dock_browser.rebuild()
        self._workspace.reload_workspace()
        for win in list(self._workspace_windows):
            win.reload_workspace()
        self._switch_timeline_to_workspace_session()
        self._dock_perf._session = self.session
        self._dock_perf._refresh_callback = self._on_recalculate
        self._dock_perf._apply_state(desired_tracking, desired_mt)
        # Reset file tracking
        self._filepath = None
        self._mark_dirty(False)
        self._update_window_title()
        self._reload_active_view()

        # Sync rule bar to show content of active cell
        self._sync_rule_bar_from_current()

    def open_file(self, path: str) -> bool:
        """Open a file programmatically (used by REPL and GUI).

        Returns True on success, False on failure.
        Thread-safe: can be called from any thread.
        """
        from pathlib import Path
        path_obj = Path(path)
        if not path_obj.exists():
            print(f"File not found: {path}")
            return False

        # Check if we're on the main GUI thread
        if QtCore.QThread.currentThread() != self.thread():
            # Defer to main thread via signal
            self.open_file_requested.emit(str(path_obj))
            return True  # Async - actual result comes later

        return self._do_open_file(str(path_obj))

    def _do_open_file(self, path: str) -> bool:
        """Actual file opening implementation (must run on GUI thread)."""
        open_profile: dict[str, object] = {"path": path}
        open_t0 = time.perf_counter()

        t0 = time.perf_counter()
        result = self.session.execute("load_workspace", path=path)
        if not result.success:
            logger.error("Load failed: %s", result.error)
            self._flash_status_message(f"Load failed: {result.error}")
            return False
        load_profile = getattr(result, "data", {}) or {}

        open_profile["load_workspace"] = load_profile
        open_profile["timings_ms"] = {
            "load_workspace": int((time.perf_counter() - t0) * 1000.0),
        }

        # Preserve dependency tracking state from checkbox
        desired_tracking = self._dock_perf._toggle.isChecked()
        desired_mt = self._dock_perf._mt_toggle.isChecked()

        t0 = time.perf_counter()
        self.session.execute("set_dependency_tracking", enabled=desired_tracking)
        # Re-bootstrap DTO cache so Model Browser shows cubes/views from the
        # newly-loaded workspace instead of stale snapshots.
        if self.gui_view_model is not None:
            self._bootstrap_view_model()
        # Workspaces persisted without any TableViewSpec definitions would
        # otherwise load with an empty main area. Create a default view so the
        # user immediately sees a grid for at least one cube.
        self._ensure_default_view()
        self._dock_browser.rebuild()
        self._workspace.reload_workspace()
        for win in list(self._workspace_windows):
            win.reload_workspace()
        self._dock_perf._session = self.session
        self._dock_perf._refresh_callback = self._on_recalculate
        self._dock_perf._apply_state(desired_tracking, desired_mt)
        open_profile["timings_ms"]["wire_ui"] = int((time.perf_counter() - t0) * 1000.0)

        t0 = time.perf_counter()
        self._reload_active_view()
        open_profile["timings_ms"]["reload_active_view"] = int((time.perf_counter() - t0) * 1000.0)

        if not self.is_remote:
            # Restore active cell and scroll position from SessionStore (local-only)
            self._restore_view_state_from_session_store()

        # Sync rule bar to show content of restored active cell
        self._sync_rule_bar_from_current()

        # If any cells with errors were skipped during load, trigger recalculation
        skipped_errors = load_profile.get("counts", {}).get("cube_cells_skipped_errors", 0)
        if skipped_errors > 0:
            self.session.execute("run_recalculation", scope="all")
            open_profile["timings_ms"]["recalc_after_load"] = int((time.perf_counter() - t0) * 1000.0)

        open_profile["timings_ms"]["total"] = int((time.perf_counter() - open_t0) * 1000.0)
        open_profile["dependency_metrics"] = self.session.query("diagnostics_dependency_metrics") or {}
        open_profile["rule_eval_profile"] = self.session.query("diagnostics_rule_eval_profile", top_n=10) or {}

        logging.info("[open_profile] %s", json.dumps(open_profile, separators=(",", ":"), default=str))

        # Update file tracking
        self._filepath = path
        self._mark_dirty(False)
        self._update_window_title()

        # Switch timeline to workspace-specific session so checkpoints persist
        self._switch_timeline_to_workspace_session()

        return True

    @QtCore.Slot()
    def _on_open(self) -> None:
        # Check for unsaved changes first
        if not self._check_unsaved_changes():
            return

        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open Workspace", filter="OpenM JSON (*.json);;All Files (*)")
        if not path:
            return

        self.open_file(path)

    @QtCore.Slot()
    def _on_create_dimension(self) -> None:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("New Dimension")
        form = QtWidgets.QFormLayout(dlg)

        le_name = QtWidgets.QLineEdit(dlg)
        cb_type = QtWidgets.QComboBox(dlg)
        cb_type.addItem("Set (unordered)", "set")
        cb_type.addItem("Sequence (ordered)", "seq")

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)

        form.addRow("Name", le_name)
        form.addRow("Type", cb_type)
        form.addRow(btns)

        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return

        name = le_name.text().strip()
        dim_type = cb_type.currentData()
        if not name:
            return

        result = self.session.execute(
            "create_dimension",
            name=name,
            dim_type=dim_type,
        )
        if not result.success:
            QtWidgets.QMessageBox.warning(self, "Create Dimension", result.error or "create_dimension failed")
            return
        self._dock_browser.rebuild()

    @QtCore.Slot()
    def _on_create_dimension_item(self) -> None:
        dims = self.workspace_read_model.list_dimensions()
        dims.sort(key=lambda d: d.get("name", ""))
        if not dims:
            QtWidgets.QMessageBox.information(self, "Add Dimension Item", "No dimensions exist yet.")
            return

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Add Dimension Item")
        form = QtWidgets.QFormLayout(dlg)
        cb_dim = QtWidgets.QComboBox(dlg)
        for d in dims:
            cb_dim.addItem(d.get("name", ""), d.get("id", ""))
        le_name = QtWidgets.QLineEdit(dlg)
        chk_prepend = QtWidgets.QCheckBox("Prepend (only for sequence dims)", dlg)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow("Dimension", cb_dim)
        form.addRow("Item name", le_name)
        form.addRow("Position", chk_prepend)
        form.addRow(btns)
        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return

        dim_id = cb_dim.currentData()
        item_name = le_name.text().strip()
        if not isinstance(dim_id, str) or not item_name:
            return

        position = "prepend" if chk_prepend.isChecked() else "append"
        result = self.session.execute(
            "create_dimension_item",
            dim_id=dim_id,
            name=item_name,
            position=position,
        )
        if not result.success:
            QtWidgets.QMessageBox.warning(self, "Add Item", result.error or "create_dimension_item failed")
            return
        self._dock_browser.rebuild()
        self._reload_active_view()

    @QtCore.Slot()
    def _on_delete_dimension(self) -> None:
        dims = self.workspace_read_model.list_dimensions()
        dims.sort(key=lambda d: d.get("name", ""))
        if not dims:
            QtWidgets.QMessageBox.information(self, "Delete Dimension", "No dimensions exist yet.")
            return

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Delete Dimension")
        form = QtWidgets.QFormLayout(dlg)
        cb_dim = QtWidgets.QComboBox(dlg)
        for d in dims:
            cb_dim.addItem(d.get("name", ""), d.get("id", ""))
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow("Dimension", cb_dim)
        form.addRow(btns)
        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return

        dim_id = cb_dim.currentData()
        if not isinstance(dim_id, str):
            return

        dim_dto = None
        if self.gui_view_model is not None:
            dim_dto = self.gui_view_model.get_dimension_snapshot(dim_id)
        if not dim_dto:
            dim_dto = self.session.query("dimension_detail", dim_id=dim_id)
            if dim_dto and self.gui_view_model is not None:
                self.gui_view_model.update_dimension_snapshot(dim_id, dim_dto)

        dim_items = dim_dto.get("item_names", []) if dim_dto else [] 

        # Analyze impact before deleting
        item_ids = dim_dto.get("item_ids", [])
        dim_name = dim_dto.get("name", dim_id)
        if not self.is_remote:
            impact = self.session.query("dimension_deletion_impact", dim_id=dim_id, item_ids=item_ids) or {}
            data_cells = impact.get('total_data_cells', 0)
            anchored_rules = impact.get('anchored_rules', 0)
            rules = impact.get('rules', 0)
            if data_cells > 0 or anchored_rules > 0 or rules > 0:
                msg = f"Delete dimension '{dim_name}'?\n\n"
                msg += f"This will permanently delete:\n"
                msg += f"• {data_cells} data cell(s) (hard numbers)\n"
                msg += f"• {anchored_rules} anchored rule(s)\n"
                msg += f"• {rules} rule(s)\n\n"
                msg += "This action cannot be undone."

                resp = QtWidgets.QMessageBox.warning(
                    self,
                    "Confirm Delete Dimension",
                    msg,
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
                    QtWidgets.QMessageBox.StandardButton.Cancel,
                )
                if resp != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
        else:
            resp = QtWidgets.QMessageBox.warning(
                self,
                "Confirm Delete Dimension",
                f"Delete dimension '{dim_name}'?\n\nThis action cannot be undone.",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            if resp != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        
        # Delete items first
        if item_ids:
            deleted = self._confirm_and_delete_dimension_items(dim_id, item_ids, skip_confirm=True)
            if not deleted:
                return
        
        # Remove dimension from all cubes that use it
        cubes_using_dim = [
            cube for cube in self.workspace_read_model.list_cube_dtos()
            if dim_id in cube.get("dimension_ids", [])
        ]
        for cube in cubes_using_dim:
            self.session.execute("detach_dimension_from_cube", cube_id=cube.get("id"), dim_id=dim_id)
        
        # Engine.delete_dimension handles removing the dimension from affected views.
        self.session.execute("delete_dimension", dim_id=dim_id)
        
        # Refresh UI
        self._finalize_structure_change()
        self._rule_panel.rebuild()

    @QtCore.Slot(object)
    def _on_attach_dimension_to_cube(self, axis: object = "row") -> None:
        axis_str = axis if isinstance(axis, str) and axis in ("row", "col", "page") else "row"
        view = self.workspace_read_model.get_view(self._active_view_id)
        cube = self.workspace_read_model.get_cube(view.get("cube_id")) if view else None
        if not view or not cube:
            QtWidgets.QMessageBox.warning(self, "Add Dimension", "Cannot get current cube")
            return

        dims = self.workspace_read_model.list_dimension_dtos()
        dims.sort(key=lambda d: d.get("name", ""))
        available = [d for d in dims if d.get("id") not in cube.get("dimension_ids", [])]

        _NEW_SENTINEL = "__new__"

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Add Dimension to {cube.get('name', '')}")
        form = QtWidgets.QFormLayout(dlg)

        cb_dim = QtWidgets.QComboBox(dlg)
        cb_dim.addItem("— create new dimension —", _NEW_SENTINEL)
        for d in available:
            cb_dim.addItem(d.get("name", ""), d.get("id"))

        # Fields for new-dim path
        le_new_dim = QtWidgets.QLineEdit(dlg)
        le_new_dim.setPlaceholderText("Dimension name")
        le_new_items = QtWidgets.QLineEdit(dlg)
        le_new_items.setPlaceholderText("Item names, comma-separated (e.g. Jan,Feb,Mar)")

        cb_new_type = QtWidgets.QComboBox(dlg)
        cb_new_type.addItem("Set (unordered)", "set")
        cb_new_type.addItem("Sequence (ordered)", "seq")

        # Widget for existing-dim default-item
        cb_default = QtWidgets.QComboBox(dlg)

        row_new_dim = form.rowCount()
        form.addRow("Dimension", cb_dim)
        row_new_name = form.rowCount()
        form.addRow("New name", le_new_dim)
        row_new_type = form.rowCount()
        form.addRow("Type", cb_new_type)
        row_new_items = form.rowCount()
        form.addRow("Items", le_new_items)
        row_default = form.rowCount()
        form.addRow("Default item", cb_default)

        def _refresh() -> None:
            is_new = cb_dim.currentData() == _NEW_SENTINEL
            form.itemAt(row_new_name, QtWidgets.QFormLayout.ItemRole.LabelRole).widget().setVisible(is_new)
            le_new_dim.setVisible(is_new)
            form.itemAt(row_new_type, QtWidgets.QFormLayout.ItemRole.LabelRole).widget().setVisible(is_new)
            cb_new_type.setVisible(is_new)
            form.itemAt(row_new_items, QtWidgets.QFormLayout.ItemRole.LabelRole).widget().setVisible(is_new)
            le_new_items.setVisible(is_new)
            has_default = not is_new
            lbl_default = form.itemAt(row_default, QtWidgets.QFormLayout.ItemRole.LabelRole).widget()
            lbl_default.setVisible(has_default)
            cb_default.setVisible(has_default)
            if has_default:
                cb_default.clear()
                dim_id = cb_dim.currentData()
                if isinstance(dim_id, str) and dim_id != _NEW_SENTINEL:
                    # Look up from the already-fetched available list first;
                    # it contains the full dimension DTO with items.
                    dim_dto = next((d for d in available if d.get("id") == dim_id), None)
                    if not dim_dto and self.gui_view_model is not None:
                        dim_dto = self.gui_view_model.get_dimension_snapshot(dim_id)
                    if not dim_dto:
                        dim_dto = self.session.query("dimension_detail", dim_id=dim_id)
                        if dim_dto and self.gui_view_model is not None:
                            self.gui_view_model.update_dimension_snapshot(dim_id, dim_dto)
                    if dim_dto:
                        item_ids = dim_dto.get("item_ids", [])
                        item_names = dim_dto.get("item_names", [])
                        if not item_ids:
                            cb_default.addItem("— no items —", "")
                        for iid, iname in zip(item_ids, item_names):
                            cb_default.addItem(iname, iid)

        cb_dim.currentIndexChanged.connect(_refresh)
        _refresh()

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return

        dim_id = cb_dim.currentData()

        if dim_id == _NEW_SENTINEL:
            # Create new dimension flow
            new_name = le_new_dim.text().strip()
            if not new_name:
                QtWidgets.QMessageBox.warning(self, "Add Dimension", "Dimension name is required.")
                return
            dim_type = cb_new_type.currentData()
            if dim_type not in ("set", "seq"):
                QtWidgets.QMessageBox.warning(self, "Add Dimension", "Dimension type is required.")
                return
            result = self.session.execute(
                "create_dimension",
                name=new_name,
                dim_type=dim_type,
            )
            if not result.success:
                QtWidgets.QMessageBox.warning(self, "Add Dimension", result.error or "create_dimension failed")
                return
            new_dim_id = result.data.get("id") if isinstance(result.data, dict) else None
            if not new_dim_id:
                QtWidgets.QMessageBox.warning(self, "Add Dimension", "create_dimension returned no dimension id")
                return
            item_names = [s.strip() for s in le_new_items.text().split(",") if s.strip()]
            if not item_names:
                item_names = ["Item 1"]
            first_item_id = None
            for iname in item_names:
                item_result = self.session.execute(
                    "create_dimension_item",
                    dim_id=new_dim_id,
                    name=iname,
                )
                if not item_result.success:
                    QtWidgets.QMessageBox.warning(self, "Add Dimension", item_result.error or f"Failed to add item {iname}")
                    return
                if first_item_id is None and item_result.data is not None:
                    if isinstance(item_result.data, dict):
                        first_item_id = item_result.data.get("id")
                    else:
                        first_item_id = getattr(item_result.data, "id", None)
            default_item_id = first_item_id
            if not default_item_id:
                QtWidgets.QMessageBox.warning(self, "Add Dimension", "No items were created")
                return
            try:
                self.session.execute(
                    "attach_dimension_to_cube",
                    cube_id=cube.get("id"),
                    dim_id=new_dim_id,
                    default_item_id=default_item_id,
                )
                # Place the new dimension on the requested axis for the active view.
                result = self.session.execute(
                    "move_view_dimension",
                    view_id=self._active_view_id,
                    dim_id=new_dim_id,
                    dest=axis_str,
                )
                if not result.success:
                    QtWidgets.QMessageBox.warning(self, "Add Dimension", result.error or "move_view_dimension failed")
                    return
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Add Dimension", str(e))
                return
        else:
            if not isinstance(dim_id, str):
                return
            item_id = cb_default.currentData()
            if not isinstance(item_id, str) or not item_id:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Add Dimension",
                    "The selected dimension has no items. Please add items to the dimension before attaching it to a cube.",
                )
                return
            try:
                self.session.execute(
                    "attach_dimension_to_cube",
                    cube_id=cube.get("id"),
                    dim_id=dim_id,
                    default_item_id=item_id,
                )
                # Move the attached dimension onto the requested axis for this view.
                result = self.session.execute(
                    "move_view_dimension",
                    view_id=self._active_view_id,
                    dim_id=dim_id,
                    dest=axis_str,
                )
                if not result.success:
                    QtWidgets.QMessageBox.warning(self, "Add Dimension", result.error or "move_view_dimension failed")
                    return
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Add Dimension", str(e))
                return

        self._dock_browser.rebuild()
        self._reload_active_view()

    @QtCore.Slot()
    def _on_edit_view_axes(self) -> None:
        view = self.workspace_read_model.get_view(self._active_view_id)
        cube = self.workspace_read_model.get_cube(view.get("cube_id")) if view else None
        dim_dtos: list[dict] = []
        for did in cube.get("dimension_ids", []):
            dto = None
            if self.gui_view_model is not None:
                dto = self.gui_view_model.get_dimension_snapshot(did)
            if not dto:
                dto = self.session.query("dimension_detail", dim_id=did)
                if dto and self.gui_view_model is not None:
                    self.gui_view_model.update_dimension_snapshot(did, dto)
            if dto:
                dim_dtos.append(dto)
        if len(dim_dtos) < 2:
            QtWidgets.QMessageBox.information(self, "Edit View Axes", "Cube needs at least 2 dimensions.")
            return

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Edit View Axes")
        form = QtWidgets.QFormLayout(dlg)

        cb_row = QtWidgets.QComboBox(dlg)
        cb_col = QtWidgets.QComboBox(dlg)
        for d in dim_dtos:
            cb_row.addItem(d.get("name", d["id"]), d["id"])
            cb_col.addItem(d.get("name", d["id"]), d["id"])

        cur_row = view.get("row_dim_ids", [None])[0] if view.get("row_dim_ids") else None
        cur_col = view.get("col_dim_ids", [None])[0] if view.get("col_dim_ids") else None
        irow = cb_row.findData(cur_row)
        icol = cb_col.findData(cur_col)
        if irow >= 0:
            cb_row.setCurrentIndex(irow)
        if icol >= 0:
            cb_col.setCurrentIndex(icol)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow("Rows", cb_row)
        form.addRow("Columns", cb_col)
        form.addRow(btns)
        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return

        row_dim_id = cb_row.currentData()
        col_dim_id = cb_col.currentData()
        if not isinstance(row_dim_id, str) or not isinstance(col_dim_id, str):
            return
        try:
            self.session.execute(
                "set_view_axes",
                view_id=self._active_view_id,
                row_dimension_id=row_dim_id,
                col_dimension_id=col_dim_id,
            )
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Edit View Axes", str(e))
            return

        self._dock_browser.rebuild()
        self._reload_active_view()

    @QtCore.Slot()
    def _on_save(self) -> None:
        # Determine default filename to pre-fill in the save dialog
        if self._filepath:
            default_name = self._filepath
        else:
            # Generate timestamped default name
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            default_name = f"New_Model_{timestamp}.json"

        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save Workspace",
            default_name,  # Pre-filled filename
            filter="OpenM JSON (*.json);;All Files (*)",
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        result = self.session.execute("save_workspace", path=path)
        if not result.success:
            print(f"[SAVE] Failed: {result.error}")
            logger.error("Save failed: %s", result.error)
            self._flash_status_message(f"Save failed: {result.error}")
            return
        # Update file tracking and clear dirty flag only on success
        self._filepath = path
        self._mark_dirty(False)
        self._update_window_title()
        # Associate timeline session with the workspace (by ID, not file path)
        self._switch_timeline_to_workspace_session()

    @QtCore.Slot()
    def _on_any_data_changed(self, *args: object) -> None:
        self._update_undo_redo_actions()
        self._update_copy_paste_actions()

    @QtCore.Slot()
    def _on_selection_changed(self, *args: object) -> None:
        # Keep clipboard actions and the rule bar in sync with the current
        # selection. For legacy table views, the primary selection change
        # signal is ``currentChanged`` (handled in ``_on_current_changed``),
        # but for MatrixGrid we only get a custom ``selection_changed``
        # signal, so we also refresh the rule bar here.
        self._update_copy_paste_actions()
        self._sync_rule_bar_from_current()
        self._sync_flow_panel_from_current()

        # Always refresh selection statistics when the selection changes.
        self._update_selection_stats()

    @QtCore.Slot(QtCore.QModelIndex, QtCore.QModelIndex)
    def _on_current_changed(self, current: QtCore.QModelIndex, previous: QtCore.QModelIndex) -> None:
        if not current.isValid():
            self._name_box.setText("")
            self._rule_bar.setText("")
            self._sync_flow_panel_from_current()
            return

        source, rule_expr = self._current_cell_explain()

        if source == "rule":
            pass
        elif source == "override":
            pass
        else:
            # Non-error sources leave the left indicator in its previous
            # state; we don't show a textual message in the standard status
            # field at all.
            pass

        self._actions.act_clear_override.setEnabled(source == "override")

        self._update_copy_paste_actions()
        self._sync_rule_bar_from_current()

    def _sync_rule_bar_from_current(self) -> None:
        """Synchronise the rule bar with the currently selected cell.

        Behaviour:
        - If the cell has an explicit *cell rule*, show that as "=expr".
        - If the cell is showing an *error* with an associated rule
          (cell rule or rule), show "=expr" for debugging.
        - Otherwise, show the stored cell value (or blank) and leave the
          placeholder to describe rule/cell entry syntax.

        In particular, cells whose values come purely from *rules* but have no
        per-cell rule will display their computed value here, not the rule
        expression, avoiding the impression that the user has a cell rule
        where none exists.
        """

        if self._table is None:
            return
        if not self._active_view_exists():
            self._rule_bar.setText("")
            self._rule_bar.setPlaceholderText("Rule: Dim.Item = expr  •  Cell: =expr")
            return

        sel_suffix = ""
        cell_dto: dict | None = None
        cube_id: str | None = None
        addr: tuple[str, ...] | None = None

        if isinstance(self._table, MatrixGrid):
            count = self._table.selected_cell_count()
            if count > 1:
                sel_suffix = f"  •  {count} cells selected"
            keys = self._table.selected_keys()
            if keys is None:
                self._rule_bar.setText("")
                self._rule_bar.setPlaceholderText(f"Rule: Dim.Item = expr  •  Cell: =expr{sel_suffix}")
                return
            row_key, col_key = keys
            cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
            cube_id = cell_dto.get("cube_id")
            addr = self.cell_read_model.addr_for_view_keys(self._active_view_id, row_key, col_key)
        else:
            idx = self._table.currentIndex()  # type: ignore[union-attr]
            if not idx.isValid():
                self._rule_bar.setText("")
                self._rule_bar.setPlaceholderText("Rule: Dim.Item = expr  •  Cell: =expr")
                return
            tm = self._current_tab.tree_model
            if tm is not None:
                if idx.column() <= 0:
                    self._rule_bar.setText("")
                    self._rule_bar.setPlaceholderText("Rule: Dim.Item = expr  •  Cell: =expr")
                    return
                row_key = tm.row_key_for_index(idx)
                col_key = tm.col_key_for_column(idx.column())
                if row_key is None or col_key is None:
                    self._rule_bar.setText("")
                    self._rule_bar.setPlaceholderText("Rule: Dim.Item = expr  •  Cell: =expr")
                    return
                cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
                cube_id = cell_dto.get("cube_id")
                addr = self.cell_read_model.addr_for_view_keys(self._active_view_id, row_key, col_key)
            else:
                # Phase E: Index-based reads removed. Model must provide key access.
                self._rule_bar.setText("")
                self._rule_bar.setPlaceholderText("Rule: Dim.Item = expr  •  Cell: =expr")
                return

        expr_text: str | None = None
        if cube_id is not None and addr is not None:
            expr_text = self.cell_read_model.rule_detail(cube_id, addr)

        # If no per-cell rule, fall back to showing the expression for
        # error cells (to aid debugging), but *not* for plain rule-driven
        # cells that evaluated successfully.
        if isinstance(cell_dto, dict):
            explain = cell_dto.get("explain", {})
            if (
                expr_text is None
                and explain.get("source") == "error"
                and explain.get("rule_body") is not None
            ):
                expr_text = explain["rule_body"]
        elif cell_dto is not None:
            if (
                expr_text is None
                and cell_dto.explain.source == "error"
                and cell_dto.explain.rule_body is not None
            ):
                expr_text = cell_dto.explain.rule_body

        if expr_text is not None:
            self._rule_bar.setText(f"={expr_text}")
        else:
            if isinstance(cell_dto, dict):
                self._rule_bar.setText("" if cell_dto.get("value") is None else str(cell_dto.get("value")))
            elif cell_dto is not None:
                self._rule_bar.setText("" if cell_dto.value is None else str(cell_dto.value))

        self._rule_bar.setPlaceholderText(f"Rule: Dim.Item = expr  •  Cell: =expr{sel_suffix}")
        
        # Update info toolbox with cell metadata
        if isinstance(self._table, MatrixGrid) and keys is not None:
            row_key, col_key = keys
            self._dock_info.show_cell(self._active_view_id, row_key, col_key)
        
        return

    def _confirm_multi_apply(self, count: int, what: str) -> bool:
        """Warn before applying to many cells. Default is No."""
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("Apply to multiple cells?")
        box.setText(f"You have {count} cells selected. Apply this {what} to all of them?")
        box.setStandardButtons(
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        return box.exec() == QtWidgets.QMessageBox.StandardButton.Yes

    @QtCore.Slot()
    def _on_rule_bar_enter(self) -> None:
        if isinstance(self._table, MatrixGrid):
            owner = StatusOwner.recalc(self._next_status_owner_id())
            self._status_manager.begin(owner, StatusState.CALCULATING)
            try:
                text = self._rule_bar.text().strip()
                print(f"DEBUG _on_rule_bar_enter: text='{text}'")
                # Allow user to paste with surrounding quotes
                if text.startswith("\"") and text.endswith("\"") and len(text) >= 2:
                    text = text[1:-1].strip()

                # Rule syntax: LHS = expr, where LHS follows Improv-style ref rules
                # (Dim.Item, [Dim1.Item1, Dim2.Item2, ...], Sheet::Dim.Item, ...).
                import re
                # Split only on the first '=' so expressions may contain '=' in IF, etc.
                m = None if text.lstrip().startswith("=") else re.match(r"^=?\s*(.*?)\s*=\s*(.+)$", text)
                print(f"DEBUG _on_rule_bar_enter: m={m}, text.lstrip().startswith('=')={text.lstrip().startswith('=')}")
                if m:
                    lhs_raw = m.group(1).strip()
                    print(f"DEBUG: raw lhs before processing: {repr(lhs_raw)}")
                    if lhs_raw == "*":
                        lhs_raw = "*.*"

                    # Detect $ anchor prefix for anchored rules
                    is_anchored = False
                    print(f"DEBUG: checking if lhs starts with $: {lhs_raw.startswith('$')}")
                    if lhs_raw.startswith("$"):
                        is_anchored = True
                        lhs_raw = lhs_raw[1:].strip()
                        print(f"DEBUG: stripped lhs after $: {repr(lhs_raw)}")

                    expr = m.group(2).strip()
                    if lhs_raw:
                        # Slice 3: GUI no longer parses rule targets locally.
                        # Use QueryService to resolve raw LHS string engine-side.
                        view = self.workspace_read_model.get_view(self._active_view_id)
                        if not view:
                            return
                        resolve = self.session.query("rule_target_resolve", cube_id=view.get("cube_id"), lhs=lhs_raw)
                        if resolve.get("error"):
                            print(f"DEBUG: rule_target_resolve failed: {resolve['error']}")
                            # If the user did not start with '=', they most likely
                            # intended a rule; in that case surface the parse error.
                            if not text.lstrip().startswith("="):
                                QtWidgets.QMessageBox.critical(self, "Rule error", resolve["error"])
                                self._refresh_error_status(allow_from_computing=True)
                                return
                            # Otherwise, treat this as a normal cell entry.
                            self._refresh_error_status(allow_from_computing=True)
                        else:
                            targets = resolve["targets"]
                            print(f"DEBUG: rule_target_resolve succeeded: {targets}")
                            try:
                                print(f"DEBUG: calling set_rule with targets={targets}, expr='{expr}', is_anchored={is_anchored}")
                                result = self.session.execute(
                                    "rule",
                                    cube_id=view.get("cube_id"),
                                    targets=targets,
                                    expression=expr,
                                    is_anchored=is_anchored,
                                )
                                if result.status.name == "ERROR":
                                    raise RuntimeError(result.error)
                                print(f"DEBUG: set_rule succeeded")
                            except Exception as e:
                                print(f"DEBUG: set_rule failed: {e}")
                                QtWidgets.QMessageBox.critical(self, "Rule error", str(e))
                                self._refresh_error_status(allow_from_computing=True)
                                self._on_rules_changed()
                                return
                            if isinstance(self._table, MatrixGrid):
                                self._table.reload(invalidate_tiles="data")
                            self._update_undo_redo_actions()
                            self._sync_rule_bar_from_current()
                            self._on_rules_changed()
                            self._refresh_error_status(allow_from_computing=True)
                            # Clear rule bar to prevent view_workspace_controller from re-processing
                            self._rule_bar.setText("")
                            return
                count = self._table.selected_cell_count()
                if count == 0:
                    return
                if count > 1:
                    QtWidgets.QMessageBox.warning(
                        self,
                        "Single-cell only",
                        f"You have {count} cells selected. Rule bar entry now only applies to one cell. Select a single cell and try again.",
                    )
                    return
                keys_many = self._table.selected_cell_keys_many()
                if text.startswith("="):
                    expr = text[1:]
                    try:
                        for row_key, col_key in keys_many:
                            self._execute_rule_for_cell(
                                self._active_view_id, row_key=row_key, col_key=col_key, expression=expr
                            )
                    except Exception as e:
                        QtWidgets.QMessageBox.critical(self, "Rule error", str(e))
                        # Rule failed to parse/apply; restore status based on
                        # the current engine errors rather than a sticky generic
                        # "Rule error".
                        self._refresh_error_status(allow_from_computing=True)
                        self._on_rules_changed()
                        return
                else:
                    print(f"DEBUG: FALLTHROUGH to hard number insertion with text='{text}'")
                    val = text
                    print(f"DEBUG: raw val={val}")
                    for row_key, col_key in keys_many:
                        result = self.session.execute(
                            "set_cell_hardvalue",
                            view_id=self._active_view_id,
                            cell_ref={
                                "kind": "ids",
                                "row_key": row_key,
                                "col_key": col_key,
                            },
                            value=val,
                        )
                        if not result.success:
                            raise RuntimeError(result.error or "set_cell_hardvalue failed")
                self._table.reload(invalidate_tiles="data")
                self._update_undo_redo_actions()
                self._sync_rule_bar_from_current()
                self._on_rules_changed()
                self._refresh_error_status(allow_from_computing=True)
                self._mark_dirty(True)
                return
            finally:
                self._status_manager.end(owner)

        idx = self._table.currentIndex()  # type: ignore[union-attr]
        text = self._rule_bar.text().strip()
        # Rule entry: LHS = expr with Improv-style punctuation on LHS.
        import re

        m = None if text.lstrip().startswith("=") else re.match(r"^=?\s*(.*?)\s*=\s*(.+)$", text)
        try:
            if m:
                lhs_raw = m.group(1).strip()
                if lhs_raw == "*":
                    lhs_raw = "*.*"

                # Detect $ anchor prefix for anchored rules
                is_anchored = False
                if lhs_raw.startswith("$"):
                    is_anchored = True
                    lhs_raw = lhs_raw[1:].strip()

                expr = m.group(2).strip()
                if lhs_raw:
                    # Slice 3: GUI no longer parses rule targets locally.
                    view = self.workspace_read_model.get_view(self._active_view_id)
                    if not view:
                        raise RuntimeError("No active view")
                    resolve = self.session.query("rule_target_resolve", cube_id=view.get("cube_id"), lhs=lhs_raw)
                    if resolve.get("error"):
                        raise RuntimeError(resolve["error"])
                    targets = resolve["targets"]
                    result = self.session.execute(
                        "rule",
                        cube_id=view.get("cube_id"),
                        targets=targets,
                        expression=expr,
                        is_anchored=is_anchored,
                    )
                    if result.status.name == "ERROR":
                        raise RuntimeError(result.error or "rule failed")
                else:
                    # Fall back to cell input if no LHS before '='
                    if not idx.isValid():
                        return
                    if text.startswith("="):
                        row_key = self._current_tab.tree_model.row_key_for_index(idx) if self._current_tab.tree_model else None
                        col_key = self._current_tab.tree_model.col_key_for_column(idx.column()) if self._current_tab.tree_model else None
                        if row_key and col_key:
                            self._execute_rule_for_cell(
                                self._active_view_id, row_key=row_key, col_key=col_key, expression=text[1:]
                            )
                        else:
                            # No tree model keys — resolve from row/col directly
                            addr = self.grid_read_model.addr_for_rc(self._active_view_id, idx.row(), idx.column())
                            view = self.workspace_read_model.get_view(self._active_view_id)
                            cube = self.workspace_read_model.get_cube(view.get("cube_id")) if view else None
                            if not view or not cube:
                                return
                            targets: list[tuple[str, str]] = []
                            for dim_id, item_id in zip(cube.get("dimension_ids", []), addr):
                                if dim_id == "@":
                                    continue
                                dim = self.workspace_read_model.get_dimension(dim_id)
                                if dim is None:
                                    continue
                                item_name = next((it.get("name") for it in dim.get("items", []) if it.get("id") == item_id), item_id)
                                targets.append((dim.get("name"), item_name))
                            result = self.session.execute(
                                "rule",
                                cube_id=cube.get("id"),
                                targets=targets,
                                expression=text[1:],
                                is_anchored=True,
                            )
                            if result.status.name == "ERROR":
                                raise RuntimeError(result.error or "rule failed")
                    else:
                        result = self.session.execute(
                            "set_cell",
                            view_id=self._active_view_id,
                            row=idx.row(),
                            col=idx.column(),
                            value=text,
                        )
                        if not result.success:
                            raise RuntimeError(result.error or "set_cell failed")
            else:
                if not idx.isValid():
                    return
                tm = self._current_tab.tree_model
                if tm is not None:
                    if idx.column() <= 0:
                        return
                    row_key = tm.row_key_for_index(idx)
                    col_key = tm.col_key_for_column(idx.column())
                    if row_key is None or col_key is None:
                        return
                    if text.startswith("="):
                        return
                    result = self.session.execute(
                        "set_cell_hardvalue",
                        view_id=self._active_view_id,
                        cell_ref={
                            "kind": "ids",
                            "row_key": row_key,
                            "col_key": col_key,
                        },
                        value=text,
                    )
                    if not result.success:
                        raise RuntimeError(result.error or "set_cell_hardvalue failed")
                else:
                    if text.startswith("="):
                        addr = self.grid_read_model.addr_for_rc(self._active_view_id, idx.row(), idx.column())
                        view = self.workspace_read_model.get_view(self._active_view_id)
                        cube = self.workspace_read_model.get_cube(view.get("cube_id")) if view else None
                        if not view or not cube:
                            return
                        targets: list[tuple[str, str]] = []
                        for dim_id, item_id in zip(cube.get("dimension_ids", []), addr):
                            if dim_id == "@":
                                continue
                            dim = self.workspace_read_model.get_dimension(dim_id)
                            if dim is None:
                                continue
                            item_name = next((it.get("name") for it in dim.get("items", []) if it.get("id") == item_id), item_id)
                            targets.append((dim.get("name"), item_name))
                        result = self.session.execute(
                            "rule",
                            cube_id=cube.get("id"),
                            targets=targets,
                            expression=text[1:],
                            is_anchored=True,
                        )
                        if result.status.name == "ERROR":
                            raise RuntimeError(result.error or "rule failed")
                    else:
                        result = self.session.execute(
                            "set_cell",
                            view_id=self._active_view_id,
                            row=idx.row(),
                            col=idx.column(),
                            value=text,
                        )
                        if not result.success:
                            raise RuntimeError(result.error or "set_cell failed")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Rule error", str(e))
            # As above, failed rule entry should not leave a sticky generic
            # "Rule error"; fall back to the true engine error state.
            self._refresh_error_status(allow_from_computing=True)
            self._on_rules_changed()
            return

        self._current_tab.model.dataChanged.emit(idx, idx, [QtCore.Qt.ItemDataRole.DisplayRole])
        self._update_undo_redo_actions()
        self._on_rules_changed()
        self._refresh_error_status()
        self._mark_dirty(True)
        return

    def _resolve_rule_targets(
        self, view_id: str, row_key: tuple[str, ...], col_key: tuple[str, ...]
    ) -> list[tuple[str, str]]:
        """Resolve view keys to rule targets: list of (dim_name, item_name) pairs."""
        view = self.workspace_read_model.get_view(view_id)
        cube = self.workspace_read_model.get_cube(view.get("cube_id")) if view else None
        if not view or not cube:
            return []
        addr = self.cell_read_model.addr_for_view_keys(view_id, row_key=row_key, col_key=col_key)
        targets: list[tuple[str, str]] = []
        for dim_id, item_id in zip(cube.get("dimension_ids", []), addr):
            if dim_id == "@":
                continue
            dim = self.workspace_read_model.get_dimension(dim_id)
            if dim is None:
                continue
            item_name = next((it.get("name") for it in dim.get("items", []) if it.get("id") == item_id), item_id)
            targets.append((dim.get("name"), item_name))
        return targets

    def _execute_rule_for_cell(
        self, view_id: str, row_key: tuple[str, ...], col_key: tuple[str, ...], expression: str
    ) -> None:
        """Route cell rule entry through the rule command spine (anchored rule)."""
        targets = self._resolve_rule_targets(view_id, row_key, col_key)
        view = self.workspace_read_model.get_view(view_id)
        if not view:
            return
        result = self.session.execute(
            "rule",
            cube_id=view.get("cube_id"),
            targets=targets,
            expression=expression,
            is_anchored=True,
        )
        if result.status.name == "ERROR":
            raise RuntimeError(result.error or "rule failed")

    @QtCore.Slot()
    def _on_set_rule_body(self) -> None:
        """Fallback menu action: prompt for a value or =rule body and apply to selection."""
        owner = StatusOwner.recalc(self._next_status_owner_id())
        self._status_manager.begin(owner, StatusState.CALCULATING)
        try:
            if isinstance(self._table, MatrixGrid):
                count = self._table.selected_cell_count()
                if count == 0:
                    return
                if count > 1:
                    QtWidgets.QMessageBox.warning(
                        self,
                        "Single-cell only",
                        f"You have {count} cells selected. Set Rule now only applies to one cell. Select a single cell and try again.",
                    )
                    return
                keys_many = self._table.selected_cell_keys_many()
                text, ok = QtWidgets.QInputDialog.getText(
                    self,
                    "Set Value or Rule",
                    "Enter a value or =rule to apply",
                )
                if not ok:
                    return
                text = text.strip()
                is_rule = text.startswith("=") or "=" in text
                if is_rule:
                    expr = text[1:] if text.startswith("=") else text
                    try:
                        for row_key, col_key in keys_many:
                            self._execute_rule_for_cell(
                                self._active_view_id, row_key=row_key, col_key=col_key, expression=expr
                            )
                    except Exception as e:
                        QtWidgets.QMessageBox.critical(self, "Rule error", str(e))
                        self._refresh_error_status(allow_from_computing=True)
                        self._on_rules_changed()
                        return
                else:
                    val = text
                    for row_key, col_key in keys_many:
                        result = self.session.execute(
                            "set_cell_hardvalue",
                            view_id=self._active_view_id,
                            cell_ref={
                                "kind": "ids",
                                "row_key": row_key,
                                "col_key": col_key,
                            },
                            value=val,
                        )
                        if not result.success:
                            raise RuntimeError(result.error or "set_cell_hardvalue failed")
                self._table.reload(invalidate_tiles="data")
                self._update_undo_redo_actions()
                self._sync_rule_bar_from_current()
                self._on_rules_changed()
                self._refresh_error_status(allow_from_computing=True)
                return

            idx = self._table.currentIndex()  # type: ignore[union-attr]
            if not idx.isValid():
                return
            text, ok = QtWidgets.QInputDialog.getText(
                self,
                "Set Value or Rule",
                "Enter a value or =rule to apply",
            )
            if not ok:
                return
            text = text.strip()
            is_rule = text.startswith("=") or "=" in text
            if is_rule:
                expr = text[1:] if text.startswith("=") else text
                try:
                    for row_key, col_key in keys_many:
                        self._execute_rule_for_cell(
                            self._active_view_id, row_key=row_key, col_key=col_key, expression=expr
                        )
                except Exception as e:
                    QtWidgets.QMessageBox.critical(self, "Rule error", str(e))
                    self._refresh_error_status(allow_from_computing=True)
                    self._on_rules_changed()
                    return
            else:
                idx = self._table.currentIndex()  # type: ignore[union-attr]
                self._current_tab.model.dataChanged.emit(idx, idx, [QtCore.Qt.ItemDataRole.DisplayRole])  # type: ignore[union-attr]
            self._on_rules_changed()
            self._sync_rule_bar_from_current()
            self._refresh_error_status()
        finally:
            self._status_manager.end(owner)

    @QtCore.Slot()
    def _on_copy(self) -> None:
        if isinstance(self._table, MatrixGrid):
            return
        if self.is_remote:
            # cell_range query does not support rectangular TSV export yet.
            self._flash_status_message("Copy not supported in remote mode")
            return
        sm = self._table.selectionModel()  # type: ignore[union-attr]
        if sm is None:
            return
        indexes = sm.selectedIndexes()
        if not indexes:
            return

        rows = [i.row() for i in indexes]
        cols = [i.column() for i in indexes]
        top, bottom = min(rows), max(rows)
        left, right = min(cols), max(cols)

        grid = self.session.query("cell_viewport_range", view_id=self._active_view_id, top=top, left=left, bottom=bottom, right=right) or []

        def fmt(v: object) -> str:
            return "" if v is None else str(v)

        tsv = "\n".join("\t".join(fmt(v) for v in r) for r in grid)
        QtWidgets.QApplication.clipboard().setText(tsv)

    @QtCore.Slot()
    def _on_paste(self) -> None:
        if isinstance(self._table, MatrixGrid):
            return
        idx = self._table.currentIndex()  # type: ignore[union-attr]
        if not idx.isValid():
            return

        text = QtWidgets.QApplication.clipboard().text()
        if not text:
            return

        # Parse TSV/CSV-ish clipboard. We treat tabs as columns and newlines as rows.
        lines = [ln for ln in text.splitlines() if ln is not None]
        values: list[list[object]] = []
        for ln in lines:
            parts = ln.split("\t")
            values.append(parts)

        result = self.session.execute(
            "set_range_values",
            view_id=self._active_view_id,
            top=idx.row(),
            left=idx.column(),
            values=values,
        )
        if not result.success:
            QtWidgets.QMessageBox.critical(
                self, "Paste error", result.error or "set_range_values failed"
            )
            return
        self._refresh_table()
        self._update_undo_redo_actions()

    @QtCore.Slot()
    def _on_paste_as_new_cube(self) -> None:
        values = self._parse_clipboard_tsv()
        if not values:
            QtWidgets.QMessageBox.information(self, "Paste as New Cube", "Clipboard is empty.")
            return
        if not values or max((len(row) for row in values), default=0) <= 0:
            QtWidgets.QMessageBox.warning(self, "Paste as New Cube", "Clipboard must contain at least one value.")
            return
        width = max(len(row) for row in values)
        normalized = [row + [""] * (width - len(row)) for row in values]
        row_labels = [str(i) for i in range(1, len(normalized) + 1)]
        col_labels = [str(i) for i in range(1, width + 1)]

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Paste as New Cube")
        form = QtWidgets.QFormLayout(dlg)
        le_cube_name = QtWidgets.QLineEdit(dlg)
        le_cube_name.setText("Pasted Cube")
        le_row_dim_name = QtWidgets.QLineEdit(dlg)
        le_row_dim_name.setText("Rows")
        chk_row_ordered = QtWidgets.QCheckBox("Ordered Set", dlg)
        chk_row_ordered.setChecked(False)
        le_col_dim_name = QtWidgets.QLineEdit(dlg)
        le_col_dim_name.setText("Columns")
        chk_col_ordered = QtWidgets.QCheckBox("Ordered Set", dlg)
        chk_col_ordered.setChecked(False)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow("Cube name", le_cube_name)
        form.addRow("Row dimension", le_row_dim_name)
        form.addRow("", chk_row_ordered)
        form.addRow("Column dimension", le_col_dim_name)
        form.addRow("", chk_col_ordered)
        form.addRow(btns)
        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return

        cube_name = le_cube_name.text().strip()
        row_dim_name = le_row_dim_name.text().strip()
        col_dim_name = le_col_dim_name.text().strip()
        if not cube_name or not row_dim_name or not col_dim_name:
            QtWidgets.QMessageBox.warning(self, "Paste as New Cube", "Cube and dimension names are required.")
            return

        existing_dim_names = {d.get("name", "").casefold() for d in self.workspace_read_model.list_dimension_dtos()}
        if row_dim_name.casefold() in existing_dim_names or col_dim_name.casefold() in existing_dim_names:
            QtWidgets.QMessageBox.warning(self, "Paste as New Cube", "Dimension names must be unique in the workspace.")
            return

        row_dim_type = "seq" if chk_row_ordered.isChecked() else "set"
        col_dim_type = "seq" if chk_col_ordered.isChecked() else "set"
        row_result = self.session.execute(
            "create_dimension",
            name=row_dim_name,
            dim_type=row_dim_type,
        )
        if not row_result.success:
            QtWidgets.QMessageBox.warning(self, "Paste as New Cube", row_result.error or "create_dimension failed for row dimension")
            return
        row_dim_id = row_result.data.get("id") if isinstance(row_result.data, dict) else None
        if not row_dim_id:
            QtWidgets.QMessageBox.warning(self, "Paste as New Cube", "create_dimension returned no row dimension id")
            return

        col_result = self.session.execute(
            "create_dimension",
            name=col_dim_name,
            dim_type=col_dim_type,
        )
        if not col_result.success:
            QtWidgets.QMessageBox.warning(self, "Paste as New Cube", col_result.error or "create_dimension failed for column dimension")
            return
        col_dim_id = col_result.data.get("id") if isinstance(col_result.data, dict) else None
        if not col_dim_id:
            QtWidgets.QMessageBox.warning(self, "Paste as New Cube", "create_dimension returned no column dimension id")
            return

        row_item_ids = []
        for label in row_labels:
            item_result = self.session.execute(
                "create_dimension_item",
                dim_id=row_dim_id,
                name=label,
            )
            if not item_result.success:
                QtWidgets.QMessageBox.warning(self, "Paste as New Cube", item_result.error or f"Failed to add row item {label}")
                return
            item_id = getattr(item_result.data, "id", None) if item_result.data else None
            if not item_id:
                QtWidgets.QMessageBox.warning(self, "Paste as New Cube", f"create_dimension_item returned no id for {label}")
                return
            row_item_ids.append(item_id)

        col_item_ids = []
        for label in col_labels:
            item_result = self.session.execute(
                "create_dimension_item",
                dim_id=col_dim_id,
                name=label,
            )
            if not item_result.success:
                QtWidgets.QMessageBox.warning(self, "Paste as New Cube", item_result.error or f"Failed to add col item {label}")
                return
            item_id = getattr(item_result.data, "id", None) if item_result.data else None
            if not item_id:
                QtWidgets.QMessageBox.warning(self, "Paste as New Cube", f"create_dimension_item returned no id for {label}")
                return
            col_item_ids.append(item_id)

        try:
            result = self.session.execute(
                "create_cube",
                name=cube_name,
                dimension_ids=[row_dim_id, col_dim_id],
            )
            cube_id = result.data.get("id") if result.data else None
            if not cube_id:
                raise RuntimeError("create_cube returned no cube id")
            cube = self.workspace_read_model.get_cube(cube_id)
        except Exception as e:
            self.session.execute("delete_dimension", dim_id=row_dim_id)
            self.session.execute("delete_dimension", dim_id=col_dim_id)
            QtWidgets.QMessageBox.warning(self, "Paste as New Cube", f"Failed to create cube: {e}")
            return

        view = self._create_default_view_for_cube(cube_name, cube.get("id") if cube else cube_id, [row_dim_id, col_dim_id])
        if view is None:
            return
        for r_idx, row_item_id in enumerate(row_item_ids):
            for c_idx, col_item_id in enumerate(col_item_ids):
                raw = normalized[r_idx][c_idx]
                value = raw
                result = self.session.execute(
                    "set_cell_hardvalue",
                    view_id=view.get("id"),
                    cell_ref={
                        "kind": "ids",
                        "row_key": (row_item_id,),
                        "col_key": (col_item_id,),
                    },
                    value=value,
                )
                if not result.success:
                    raise RuntimeError(result.error or "set_cell_hardvalue failed")

        self._finalize_structure_change(view.get("id"))
        self._update_undo_redo_actions()

    @QtCore.Slot()
    def _on_convert_selected_data_to_dimension_item_labels(self) -> None:
        selection = self._matrix_selected_rect_values()
        if selection is None:
            QtWidgets.QMessageBox.warning(self, "Convert Selected Data", "Select a contiguous rectangular span of data cells.")
            return
        _, values, _ = selection
        labels: list[str] = []
        for row in values:
            for value in row:
                label = "" if value is None else str(value).strip()
                if not label:
                    QtWidgets.QMessageBox.warning(self, "Convert Selected Data", "Selected cells must all contain non-empty values.")
                    return
                labels.append(label)
        if len(set(label.lower() for label in labels)) != len(labels):
            QtWidgets.QMessageBox.warning(self, "Convert Selected Data", "Selected values must be unique.")
            return

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Convert Selected Data to New Dimension Item Labels")
        form = QtWidgets.QFormLayout(dlg)
        le_dim_name = QtWidgets.QLineEdit(dlg)
        le_dim_name.setText("New Dimension")
        cb_type = QtWidgets.QComboBox(dlg)
        cb_type.addItem("Set (unordered)", "set")
        cb_type.addItem("Sequence (ordered)", "seq")
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow("Dimension name", le_dim_name)
        form.addRow("Type", cb_type)
        form.addRow(btns)
        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return

        dim_name = le_dim_name.text().strip()
        dim_type = cb_type.currentData()
        if not dim_name:
            QtWidgets.QMessageBox.warning(self, "Convert Selected Data", "Dimension name is required.")
            return
        if any(d.get("name", "").casefold() == dim_name.casefold() for d in self.workspace_read_model.list_dimension_dtos()):
            QtWidgets.QMessageBox.warning(self, "Convert Selected Data", "Dimension name must be unique in the workspace.")
            return

        try:
            result = self.session.execute(
                "create_dimension",
                name=dim_name,
                dim_type=dim_type,
            )
            dim_id = result.data.get("id") if result.data else None
            if not dim_id:
                raise RuntimeError("create_dimension returned no dimension id")
            dim = self.workspace_read_model.get_dimension(dim_id)
            if dim is None:
                raise RuntimeError("create_dimension returned unknown dimension id")
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Convert Selected Data", str(e))
            return
        for label in labels:
            self.session.execute("create_dimension_item", dim_id=dim.get("id"), name=label)
        self._finalize_structure_change()

    @QtCore.Slot()
    def _on_assign_item_labels_from_selected_rows_or_columns(self) -> None:
        assignment = self._matrix_selected_axis_assignment()
        if assignment is None:
            QtWidgets.QMessageBox.warning(
                self,
                "Assign Item Labels",
                "Select exactly one row to assign visible column labels, or exactly one column to assign visible row labels.",
            )
            return
        axis, dim_id, item_ids, labels = assignment
        if len(item_ids) != len(labels):
            QtWidgets.QMessageBox.warning(self, "Assign Item Labels", "Selected items and aligned data values do not match in count.")
            return
        if any(not label for label in labels):
            QtWidgets.QMessageBox.warning(self, "Assign Item Labels", "Aligned data values must all be non-empty.")
            return
        if len(set(label.casefold() for label in labels)) != len(labels):
            QtWidgets.QMessageBox.warning(self, "Assign Item Labels", "Aligned data values must be unique.")
            return

        if len(item_ids) != len(set(item_ids)):
            QtWidgets.QMessageBox.warning(self, "Assign Item Labels", "Selected rows/columns must refer to unique dimension items.")
            return

        try:
            for item_id, label in zip(item_ids, labels, strict=False):
                self.session.execute(
                    "rename_dimension_item",
                    dim_id=dim_id,
                    item_id=item_id,
                    new_name=label,
                )
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Assign Item Labels", str(e))
            return
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Assign Item Labels", f"Failed to assign labels: {e}")
            return

        self._finalize_structure_change()

    @QtCore.Slot()
    def _on_delete_selected_dimension_items(self) -> None:
        selection = self._matrix_selected_dimension_items()
        if selection is None:
            QtWidgets.QMessageBox.warning(
                self,
                "Delete Dimension Items",
                "Select row headers or column headers in the active view to delete their dimension items.",
            )
            return
        dim_id, item_ids = selection
        self._confirm_and_delete_dimension_items(dim_id, item_ids)

    @QtCore.Slot()
    def _on_undo(self) -> None:
        """Undo last action and refresh UI while preserving selection."""
        selected_cell = self._get_current_cell_selection()

        result = self.session.execute("undo")
        if result.success and result.data and result.data.get("changed"):
            desc = result.data.get("description")
            # Rebootstrap the GUI view model cache because the Zig server
            # does not emit events on undo/redo, leaving cached view
            # snapshots stale.
            if self.gui_read_model_binder is not None:
                self.gui_read_model_binder._do_rebootstrap()
            self._dock_browser.rebuild()
            self._suppress_tile_fetches(True)
            self._reload_active_view()
            if selected_cell:
                self._restore_cell_selection(selected_cell)
            self._mark_dirty(True)
            self._after_mutation_enqueue_recompute()
            if desc:
                logger.info("Undone: %s", desc)

    @QtCore.Slot()
    def _on_redo(self) -> None:
        """Redo last undone action and refresh UI while preserving selection."""
        selected_cell = self._get_current_cell_selection()

        result = self.session.execute("redo")
        if result.success and result.data and result.data.get("changed"):
            desc = result.data.get("description")
            if self.gui_read_model_binder is not None:
                self.gui_read_model_binder._do_rebootstrap()
            self._dock_browser.rebuild()
            self._suppress_tile_fetches(True)
            self._reload_active_view()
            if selected_cell:
                self._restore_cell_selection(selected_cell)
            self._mark_dirty(True)
            self._after_mutation_enqueue_recompute()
            if desc:
                logger.info("Redone: %s", desc)

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:  # type: ignore[override]
        """Handle events for the rule bar and other watched objects.

        Allows normal editing keys (Delete, Backspace, arrows, etc.) to work
        in the rule bar by not intercepting them.

        On Escape or Enter, returns focus to the matrix grid/table.
        """
        if obj is self._rule_bar and event.type() == QtCore.QEvent.Type.KeyPress:
            key_event = QtGui.QKeyEvent(event)  # type: ignore[arg-type]
            key = key_event.key()
            # Let normal editing keys pass through to the QLineEdit
            if key in {
                QtCore.Qt.Key.Key_Delete,
                QtCore.Qt.Key.Key_Backspace,
                QtCore.Qt.Key.Key_Left,
                QtCore.Qt.Key.Key_Right,
                QtCore.Qt.Key.Key_Home,
                QtCore.Qt.Key.Key_End,
                QtCore.Qt.Key.Key_Insert,
            }:
                return False  # Let the QLineEdit handle it
            # Return focus to grid after Escape or Enter
            if key in {QtCore.Qt.Key.Key_Escape, QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter}:
                if self._table is not None:
                    self._table.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
                return False  # Let the QLineEdit handle the key normally too
        # Guard against C++ object already deleted during shutdown
        try:
            return super().eventFilter(obj, event)
        except RuntimeError:
            # Object already deleted, ignore
            return False

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # type: ignore[override]
        """Check for unsaved changes before closing the application."""
        # Unregister the GUI profiler endpoint so the TUI no longer targets this window.
        if hasattr(self, 'profiler') and hasattr(self, 'session'):
            try:
                self.session.execute("unregister_profiler", endpoint_id=self.profiler.endpoint_id)
            except Exception as exc:
                logging.warning("Failed to unregister GUI profiler endpoint: %s", exc)

        # Stop all timers to prevent "cannot stop timer from another thread" warnings
        if hasattr(self, '_signal_timer') and self._signal_timer:
            self._signal_timer.stop()
            self._signal_timer = None
        if hasattr(self, '_pending_recalc_timer') and self._pending_recalc_timer:
            self._pending_recalc_timer.stop()
            self._pending_recalc_timer = None
        if hasattr(self, '_pending_ready') and self._pending_ready:
            self._pending_ready = None

        # Stop child dock widget timers
        if hasattr(self, '_dock_perf') and self._dock_perf:
            if hasattr(self._dock_perf, '_refresh_timer') and self._dock_perf._refresh_timer:
                self._dock_perf._refresh_timer.stop()
        if hasattr(self, '_rule_panel') and self._rule_panel:
            if hasattr(self._rule_panel, '_blink_timer') and self._rule_panel._blink_timer:
                self._rule_panel._blink_timer.stop()

        # Check if any window (main or workspace) has unsaved changes
        has_dirty_main = self._dirty
        has_dirty_workspace = any(
            hasattr(win, "_dirty") and win._dirty for win in list(self._workspace_windows)
        )

        if not has_dirty_main and not has_dirty_workspace:
            # No unsaved changes anywhere - allow close
            event.accept()
            return
        
        # Show single confirmation dialog
        reply = QtWidgets.QMessageBox.question(
            self,
            "Unsaved Changes",
            "The workspace has unsaved changes. Do you want to save them?",
            QtWidgets.QMessageBox.StandardButton.Save
            | QtWidgets.QMessageBox.StandardButton.Discard
            | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Save,
        )
        
        if reply == QtWidgets.QMessageBox.StandardButton.Save:
            self._on_save()
            # After save, check if still dirty
            if not self._dirty:
                event.accept()
            else:
                event.ignore()
        elif reply == QtWidgets.QMessageBox.StandardButton.Discard:
            event.accept()
        else:  # Cancel
            event.ignore()

    # ------------------------------------------------------------------
    # Calculation Engine switching
    # ------------------------------------------------------------------

    def _show_engine_menu(self) -> None:
        """Show the engine selection menu at cursor."""
        menu = QtWidgets.QMenu(self)
        menu.addAction(self._actions.act_engine_python)
        menu.addAction(self._actions.act_engine_remote)
        menu.exec(QtGui.QCursor.pos())

    def _on_engine_changed(self, engine_type: str) -> None:
        """Handle engine switch request from user."""
        # Get current engine type
        current_type = self._get_current_engine_type()
        if engine_type == current_type:
            return

        # Prompt user about switching
        reply = QtWidgets.QMessageBox.question(
            self,
            "Switch Calculation Engine",
            f"Switch from {current_type.capitalize()} to {engine_type.capitalize()}?\n\n"
            "This will reload the workspace and trigger recalculation.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            # Revert menu selection
            self._update_engine_indicator(current_type)
            return

        # Save preference
        self._save_engine_preference(engine_type)

        # Perform the switch
        self._switch_to_engine(engine_type)

    def _get_current_engine_type(self) -> str:
        """Return the current engine type as string."""
        import os
        if os.environ.get("OMENGINE_MODE") == "remote":
            return "remote"
        return "python"

    def _switch_to_engine(self, engine_type: str) -> None:
        """Perform the actual engine switch."""
        if self.is_remote:
            self._flash_status_message("Engine switch not supported in remote mode")
            return
        owner = StatusOwner.load(self._next_status_owner_id())
        self._status_manager.begin(owner, StatusState.LOADING)
        try:
            # Preserve current selection for restoration
            selected_cell = self._get_current_cell_selection()

            # Transfer settings from old engine
            desired_tracking = self._dock_perf._toggle.isChecked()
            desired_mt = self._dock_perf._mt_toggle.isChecked()

            # Switch engine through command spine (dependency_tracking applied by handler)
            result = self.session.execute(
                "set_engine",
                engine_type=engine_type,
                dependency_tracking=desired_tracking,
            )
            if not result.success:
                raise RuntimeError(f"set_engine command failed: {result.error}")

            self._workspace.reload_workspace()
            for win in list(self._workspace_windows):
                win.reload_workspace()
            self._dock_perf._session = self.session
            self._dock_perf._refresh_callback = self._on_recalculate
            self._dock_perf._apply_state(desired_tracking, desired_mt)

            # Ensure menu checkbox is correct
            self._update_engine_indicator(engine_type)

            # Recalculate and refresh
            self.session.execute("run_recalculation", scope="all")
            self._dock_browser.rebuild()
            self._reload_active_view()
            if selected_cell:
                self._restore_cell_selection(selected_cell)

            logger.info("Switched to %s engine", engine_type)

        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "Engine Switch Failed", f"Failed to switch engine:\n{str(e)}"
            )
            logger.error("Engine switch failed: %s", e)
            # Revert to current engine in UI
            current = self._get_current_engine_type()
            self._update_engine_indicator(current)
        finally:
            self._status_manager.end(owner)

    def _update_engine_indicator(self, engine_type: str) -> None:
        """Update the engine menu checkbox."""
        self._actions.act_engine_python.setChecked(engine_type == "python")
        self._actions.act_engine_remote.setChecked(engine_type == "remote")

    def _load_engine_preference(self) -> str:
        """Load preferred engine from QSettings or env."""
        import os
        if os.environ.get("OMENGINE_MODE") == "remote":
            return "remote"
        settings = QtCore.QSettings("OM Studio", "Application")
        return settings.value("calculation_engine", "python")  # type: ignore[return-value]

    def _save_engine_preference(self, engine_type: str) -> None:
        """Save engine preference to QSettings."""
        settings = QtCore.QSettings("OM Studio", "Application")
        settings.setValue("calculation_engine", engine_type)

# Entry points (run_with_splash, run, run_gui_in_thread) moved to lib_runtime/gui_host.py
# as part of G6a.1 composition-root relocation. lib_gui/app.py defines GUI classes only.
