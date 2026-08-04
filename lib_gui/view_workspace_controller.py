from __future__ import annotations

import logging
import os
from PySide6 import QtCore, QtWidgets

logger = logging.getLogger(__name__)

# Debug flag for GUI - set DEBUG_GUI=true to enable verbose logging
DEBUG_GUI = os.environ.get("DEBUG_GUI", "false").lower() in ("true", "1", "yes")

from lib_gui._view_tab import ViewTab
from lib_gui.cell_read_model import CellReadModel
from lib_gui.view_workspace import ViewWorkspacePane
from lib_gui.workspace_read_model import WorkspaceReadModel
from lib_gui_elements.matrix_grid import MatrixGrid
from lib_utils.gui_profiler import GuiProfiler, NOOP_SPAN


class ViewWorkspaceController(QtCore.QObject):
    """Controller for a ViewWorkspacePane (tabs + rule panel)."""

    view_changed = QtCore.Signal(str)
    status_changed = QtCore.Signal(str, str)
    request_status_flash = QtCore.Signal(str)
    table_selection_changed = QtCore.Signal()
    table_focus_requested = QtCore.Signal()
    workspace_changed = QtCore.Signal()
    rules_changed = QtCore.Signal()
    data_changed = QtCore.Signal()
    undo_state_changed = QtCore.Signal()
    copy_paste_state_changed = QtCore.Signal()
    mark_dirty_requested = QtCore.Signal()  # Request to mark workspace dirty without full rebuild

    def __init__(
        self,
        *,
        session: object,
        pane: ViewWorkspacePane,
        cell_read_model: CellReadModel,
        workspace_read_model: WorkspaceReadModel,
        parent: QtCore.QObject | None = None,
        profiler: GuiProfiler | None = None,
    ) -> None:
        super().__init__(parent)
        self._pane = pane
        self._session = session
        self.cell_read_model = cell_read_model
        self.workspace_read_model = workspace_read_model
        self._profiler = profiler
        self._span = profiler.span if profiler is not None else NOOP_SPAN
        self._tabs = pane.tabs
        self._rule_panel = pane.rule_panel
        self._flow_panel = pane.flow_panel
        self._circular_refs_panel = pane.circular_refs_panel
        self._rule_panel.rules_changed.connect(self.rules_changed.emit)
        self._rule_panel.rule_reordered.connect(self._on_rule_reordered)
        self._view_tabs: list[ViewTab] = []
        self._active_view_id: str | None = None
        self._connected_table: QtWidgets.QWidget | None = None
        self._format_connected_tab: ViewTab | None = None
        self._tabs_signal_connected = False
        self._showing_rename_error = False
        self._in_tab_change = False
        self._in_connect_table_signals = False

    @property
    def current_tab(self) -> ViewTab:
        if not self._view_tabs:
            raise IndexError("No view tabs available")
        try:
            idx = self._tabs.currentIndex()
        except RuntimeError:
            raise IndexError("Tab widget no longer exists")

        if idx < 0 or idx >= len(self._view_tabs):
            idx = 0
        return self._view_tabs[idx]

    @property
    def active_table(self) -> QtWidgets.QWidget | None:
        try:
            table = self.current_tab.table
            DEBUG_GUI and print(f"DEBUG active_table: returning {table} id={id(table) if table else None}")
            return table
        except (IndexError, RuntimeError):
            return None

    @property
    def active_model(self) -> QtCore.QAbstractItemModel | None:
        try:
            return getattr(self.current_tab, "model", None)
        except IndexError:
            return None

    @property
    def active_view_id(self) -> str | None:
        return self._active_view_id

    @property
    def has_tabs(self) -> bool:
        return bool(self._view_tabs)

    @property
    def selection_stats(self) -> str | None:
        return None

    def initialize(self) -> None:
        self.rebuild_tabs()
        self.connect_table_signals()
        # Connect rule input bar returnPressed to handler
        self._pane.rule_bar.returnPressed.connect(self._on_rule_bar_enter)

    def rebuild_tabs(self) -> None:
        logger.info("[ViewWorkspaceController] rebuild_tabs active=%s", self._active_view_id[:8] if self._active_view_id else None)
        if self._tabs_signal_connected:
            try:
                self._tabs.currentChanged.disconnect(self.on_tab_changed)
            except (TypeError, RuntimeError):
                pass
            try:
                self._tabs.tabBar().tabMoved.disconnect(self.on_tab_moved)
            except (TypeError, RuntimeError):
                pass
            try:
                self._tabs.tabBar().tab_renamed.disconnect(self.on_tab_renamed)
            except (TypeError, RuntimeError):
                pass
            self._tabs_signal_connected = False

        prev_active_view_id = self._active_view_id
        
        # Save scroll position from current table before removing tabs
        saved_scroll: dict[str, int] = {}
        if self._active_view_id is not None:
            try:
                old_tab = self.current_tab
                old_table = old_tab.table
                if isinstance(old_table, MatrixGrid):
                    saved_scroll = {
                        'h': old_table.horizontalScrollBar().value(),
                        'v': old_table.verticalScrollBar().value(),
                    }
                    DEBUG_GUI and print(f"DEBUG SCROLL: rebuild_tabs saved scroll h={saved_scroll['h']}, v={saved_scroll['v']}")
            except Exception as e:
                DEBUG_GUI and print(f"DEBUG SCROLL: rebuild_tabs error saving scroll: {e}")
        
        # Save selection state and debug tooltip state from current active table before removing tabs
        saved_selection: dict[str, object] = {}
        saved_sel_keys: set[tuple[str, ...]] = set()
        saved_debug_tooltips = False
        if self._active_view_id is not None:
            try:
                old_tab = self.current_tab
                old_table = old_tab.table
                if hasattr(old_table, '_sel_mode') and hasattr(old_table, '_sel_indices'):
                    # Save selection mode and indices directly (more reliable than keys in stacked mode)
                    saved_selection = {
                        '_sel_mode': old_table._sel_mode,
                        '_sel_row': old_table._sel_row,
                        '_sel_col': old_table._sel_col,
                        '_anchor_row': old_table._anchor_row,
                        '_anchor_col': old_table._anchor_col,
                        '_sel_indices': set(old_table._sel_indices),  # Save indices directly
                    }
                    DEBUG_GUI and print(f"DEBUG rebuild_tabs: saved selection {saved_selection}")
                    DEBUG_GUI and print(f"DEBUG rebuild_tabs: row count={len(old_table._rows)}, col count={len(old_table._cols)}")
                # Save debug tooltip state
                saved_debug_tooltips = getattr(old_table, '_debug_tooltips_enabled', False)
                DEBUG_GUI and print(f"DEBUG rebuild_tabs: saved debug tooltips={saved_debug_tooltips}")
            except Exception as e:
                DEBUG_GUI and print(f"DEBUG rebuild_tabs: error saving selection: {e}")
        
        # Hide old tabs before removing them; Qt/hb c-05 says hide not delete.
        # deleteLater() defers C++ destruction and races with pending timers,
        # causing orphaned widgets to pop up as top-level windows.
        for vt in self._view_tabs:
            try:
                vt.hide()  # TODO: investigate further (to prevent excessive memory consumption)
            except RuntimeError:
                pass
        self._view_tabs.clear()
        while self._tabs.count():
            self._tabs.removeTab(0)

        views = self.workspace_read_model.list_views()
        if not views:
            # Fallback: if every view is hidden, show them so the UI is never empty.
            views = self.workspace_read_model.list_views(include_system=True)

        # Resolve the active view ID *before* creating tabs so we can defer
        # table reload (and thus tile fetch queries) for non-active tabs.
        active_view_id: str | None = None
        # Session runtime state is the source of truth for the active view.
        if self._session is not None:
            try:
                current = self._session.query("active_view_current")
                session_view_id = current.get("view_id") if current else None
            except Exception:
                session_view_id = None
            if session_view_id is not None and any(v["id"] == session_view_id for v in views):
                active_view_id = session_view_id
        # Preserve the local controller cache if the session query is unavailable.
        if active_view_id is None and prev_active_view_id is not None and any(
            v["id"] == prev_active_view_id for v in views
        ):
            active_view_id = prev_active_view_id
        # Initial seed from workspace saved default only when no session state exists yet.
        if active_view_id is None:
            ws_saved_default = self.workspace_read_model.saved_default_view_id()
            if ws_saved_default is not None and any(v["id"] == ws_saved_default for v in views):
                active_view_id = ws_saved_default
                logging.debug("[DEBUG rebuild_tabs] Seeded active view from workspace: %s", active_view_id[:8])
        # Fall back to first view if no active view could be resolved.
        if active_view_id is None and views:
            active_view_id = views[0]["id"]

        host = self.parent()
        for view in views:
            is_active = view["id"] == active_view_id
            vt = ViewTab(
                view_id=view["id"],
                session=self.cell_read_model.session,
                parent=self._tabs,
                workspace_read_model=self.workspace_read_model,
                profiler=self._profiler,
                defer_table_reload=not is_active,
            )
            vt.pivot_bar.selection_changed.connect(self.refresh_table)
            vt.page_axis_bar.selection_changed.connect(self.refresh_table)
            if host is not None and hasattr(host, "_on_add_item_to_dim"):
                vt.add_item_requested.connect(getattr(host, "_on_add_item_to_dim"))
            if host is not None and hasattr(host, "_on_attach_dimension_to_cube"):
                vt.add_dim_requested.connect(getattr(host, "_on_attach_dimension_to_cube"))
            vt.workspace_changed.connect(self.workspace_changed)
            vt.presentation_changed.connect(self._on_presentation_changed)
            self._tabs.addTab(vt, view["name"])
            self._view_tabs.append(vt)

        # Restore selection state for ALL views (not just the active one)
        # This ensures when user switches views, the selection is preserved
        DEBUG_GUI and print(f"[DEBUG rebuild_tabs] Restoring selection for {len(self._view_tabs)} view tabs")
        for vt in self._view_tabs:
            try:
                view = self.workspace_read_model.get_view(vt.view_id)
                DEBUG_GUI and print(f"[DEBUG rebuild_tabs] View {vt.view_id[:8]}: view={view is not None}")
                if view is None:
                    continue
                table = vt.table
                selection_mode = view.get("selection_mode", "cell")
                selected_indices = view.get("selected_indices", [])
                anchor_cell = view.get("anchor_cell")
                active_cell = view.get("active_cell")
                DEBUG_GUI and print(f"[DEBUG rebuild_tabs] View {vt.view_id[:8]}: table type={type(table).__name__}, mode={selection_mode}, indices={selected_indices}, row_keys={len(table._row_keys) if isinstance(table, MatrixGrid) else 'N/A'}")
                if isinstance(table, MatrixGrid):
                    # Restore selection mode
                    if selection_mode in ("cell", "row", "col", "all"):
                        table._sel_mode = selection_mode
                    
                    # Restore selected indices
                    if selected_indices:
                        valid_indices = set()
                        for idx in selected_indices:
                            if selection_mode == "cell" and isinstance(idx, (list, tuple)) and len(idx) == 2:
                                r, c = idx
                                if 0 <= r < len(table._row_keys) and 0 <= c < len(table._col_keys):
                                    valid_indices.add((r, c))
                            elif selection_mode == "row" and isinstance(idx, int):
                                if 0 <= idx < len(table._row_keys):
                                    DEBUG_GUI and print(f"[DEBUG rebuild_tabs] Adding row idx {idx}")
                                    valid_indices.add(idx)
                                else:
                                    DEBUG_GUI and print(f"[DEBUG rebuild_tabs] Rejecting row idx {idx} (out of bounds, row_keys={len(table._row_keys)})")
                            elif selection_mode == "col" and isinstance(idx, int):
                                if 0 <= idx < len(table._col_keys):
                                    DEBUG_GUI and print(f"[DEBUG rebuild_tabs] Adding col idx {idx}")
                                    valid_indices.add(idx)
                                else:
                                    DEBUG_GUI and print(f"[DEBUG rebuild_tabs] Rejecting col idx {idx} (out of bounds, col_keys={len(table._col_keys)})")
                        table._sel_indices = valid_indices
                        DEBUG_GUI and print(f"[DEBUG rebuild_tabs] Restored {len(valid_indices)} indices: {valid_indices}")
                    else:
                        table._sel_indices = set()
                        DEBUG_GUI and print(f"[DEBUG rebuild_tabs] No selected_indices to restore")
                    
                    # Restore anchor cell
                    if anchor_cell is not None:
                        row, col = anchor_cell
                        if 0 <= row < len(table._row_keys) and 0 <= col < len(table._col_keys):
                            table._anchor_row = row
                            table._anchor_col = col
                    
                    # Restore active/focus cell
                    if active_cell is not None:
                        row, col = active_cell
                        if 0 <= row < len(table._row_keys) and 0 <= col < len(table._col_keys):
                            table._sel_row = row
                            table._sel_col = col
                            # If no anchor set, use active cell as anchor
                            if table._anchor_row < 0 or table._anchor_col < 0:
                                table._anchor_row = row
                                table._anchor_col = col
            except Exception:
                pass

        if active_view_id is not None:
            self._active_view_id = active_view_id
            if self._session is None or not hasattr(self._session, "execute"):
                raise RuntimeError("Session required for set_active_view")
            self._session.execute("set_active_view", view_id=active_view_id)
            self.view_changed.emit(active_view_id)
            try:
                active_view = self.workspace_read_model.get_view(active_view_id)
                if active_view:
                    active_cube = self.workspace_read_model.get_cube(active_view.get("cube_id", ""))
                    if active_cube:
                        cube_id = active_cube.get("id")
                        logging.debug("[DEBUG rebuild_tabs] Setting active cube: %s...", cube_id[:8] if cube_id else "")
                        self._rule_panel.set_active_cube(cube_id)
                        self._flow_panel.set_active_cube(cube_id)
                        self._circular_refs_panel.set_active_cube(cube_id)
                    else:
                        self._rule_panel.set_active_cube(None)
                        self._flow_panel.set_active_cube(None)
                        self._circular_refs_panel.set_active_cube(None)
                else:
                    self._rule_panel.set_active_cube(None)
                    self._flow_panel.set_active_cube(None)
                    self._circular_refs_panel.set_active_cube(None)
            except Exception as e:
                logging.debug("[DEBUG rebuild_tabs] Error setting active cube: %s", e)
                self._rule_panel.set_active_cube(None)
                self._flow_panel.set_active_cube(None)
                self._circular_refs_panel.set_active_cube(None)

            for i, vt in enumerate(self._view_tabs):
                if vt.view_id == active_view_id:
                    self._tabs.setCurrentIndex(i)
                    break
            
            # Restore selection state to new active table.
            # MatrixGrid.reload() already restores the index set by full row/col
            # keys for row/col/cell modes, so we only restore the mode, anchor,
            # and active cell here. Rebuilding the index set manually collided
            # with reload() and produced bogus {0} for cell mode.
            if saved_selection and self._active_view_id == prev_active_view_id:
                try:
                    new_table = self.current_tab.table
                    new_table._sel_mode = saved_selection['_sel_mode']
                    new_table._sel_row = saved_selection['_sel_row']
                    new_table._sel_col = saved_selection['_sel_col']
                    new_table._anchor_row = saved_selection['_anchor_row']
                    new_table._anchor_col = saved_selection['_anchor_col']
                    # For cell mode, keep _sel_indices consistent with the active cell
                    # because MatrixGrid.reload() may have left it empty or restored
                    # a different set via item-id fallback.
                    if saved_selection['_sel_mode'] == 'cell':
                        if 0 <= new_table._sel_row < len(new_table._rows) and 0 <= new_table._sel_col < len(new_table._cols):
                            new_table._sel_indices = {(new_table._sel_row, new_table._sel_col)}
                        else:
                            new_table._sel_indices = set()
                    # Prevent showEvent from resetting to cell mode after we restore
                    new_table._did_initial_focus = True
                    # Persist restored selection so _apply_session_selection in other
                    # grids and future rebuilds see the correct state.
                    if hasattr(new_table, '_write_selection_to_session'):
                        new_table._write_selection_to_session()
                    DEBUG_GUI and print(f"DEBUG rebuild_tabs: restored active cell to ({new_table._sel_row}, {new_table._sel_col})")
                except Exception as e:
                    DEBUG_GUI and print(f"DEBUG rebuild_tabs: error restoring selection: {e}")
            
            # Restore debug tooltip state to all MatrixGrid widgets
            if saved_debug_tooltips:
                try:
                    for vt in self._view_tabs:
                        if isinstance(vt.table, MatrixGrid):
                            vt.table.set_debug_tooltips_enabled(saved_debug_tooltips)
                    DEBUG_GUI and print(f"DEBUG rebuild_tabs: restored debug tooltips to all tabs")
                except Exception as e:
                    DEBUG_GUI and print(f"DEBUG rebuild_tabs: error restoring debug tooltips: {e}")
            
            # Restore scroll position to new active table if same view
            if saved_scroll and self._active_view_id == prev_active_view_id:
                try:
                    new_table = self.current_tab.table
                    if isinstance(new_table, MatrixGrid):
                        # Suppress the new table's own reload() scroll timers so
                        # only rebuild_tabs' restoration runs (prevents race).
                        new_table._preserve_scroll = True
                        # Defer scroll restoration to ensure table is fully loaded
                        def _restore_scroll(saved_h, saved_v):
                            new_table.horizontalScrollBar().setValue(saved_h)
                            new_table.verticalScrollBar().setValue(saved_v)
                            new_table._preserve_scroll = False
                            DEBUG_GUI and print(f"DEBUG SCROLL: rebuild_tabs restored scroll to h={saved_h}, v={saved_v}")
                        QtCore.QTimer.singleShot(0, lambda: _restore_scroll(saved_scroll['h'], saved_scroll['v']))
                except Exception as e:
                    DEBUG_GUI and print(f"DEBUG SCROLL: rebuild_tabs error restoring scroll: {e}")
        else:
            self._active_view_id = None
            self._rule_panel.set_active_cube(None)
            self._flow_panel.set_active_cube(None)
            self._circular_refs_panel.set_active_cube(None)

        self._tabs.currentChanged.connect(self.on_tab_changed)
        self._tabs.tabBar().tabMoved.connect(self.on_tab_moved)
        self._tabs.tabBar().tab_renamed.connect(self.on_tab_renamed)
        self._tabs_signal_connected = True
        self._sync_flow_panel_from_current()

    @QtCore.Slot(int, int)
    def on_tab_moved(self, from_index: int, to_index: int) -> None:
        if from_index == to_index or not self._view_tabs:
            return
        if not (0 <= from_index < len(self._view_tabs) and 0 <= to_index < len(self._view_tabs)):
            return
        vt = self._view_tabs.pop(from_index)
        self._view_tabs.insert(to_index, vt)
        # Update the workspace views_order to match the new tab order
        if self._session:
            self._session.execute("set_property", target="workspace", property="views_order", value=[tab.view_id for tab in self._view_tabs])
        # Mark workspace as dirty so the order is saved
        win = self._pane.window()
        if hasattr(win, "_mark_dirty"):
            try:
                win._mark_dirty(True)
            except Exception:
                pass
        self.on_tab_changed(self._tabs.currentIndex())

    @QtCore.Slot(int, str)
    def on_tab_renamed(self, index: int, new_name: str) -> None:
        if not (0 <= index < len(self._view_tabs)):
            return
        vt = self._view_tabs[index]
        try:
            view = self.workspace_read_model.get_view(vt.view_id)
            if view is None:
                return
            view_id = view.get("id", "")
            view_name = view.get("name", "")
            # Check for duplicate view names
            existing_views = self.workspace_read_model.list_views()
            if any(v.get("name") == new_name and v.get("id") != view_id for v in existing_views):
                # Revert tab text to original name and show error (only once)
                self._tabs.setTabText(index, view_name)
                if not self._showing_rename_error:
                    self._showing_rename_error = True
                    QtWidgets.QMessageBox.warning(
                        self._tabs,
                        "Rename View",
                        f"A view named '{new_name}' already exists. Please choose a different name.",
                    )
                    self._showing_rename_error = False
                return
            if self._session:
                self._session.execute("set_property", target=f"view:{view_id}", property="view.name", value=new_name)
            # Mark as dirty when view is renamed
            win = self.window()
            if hasattr(win, "_mark_dirty"):
                try:
                    win._mark_dirty(True)
                except Exception:
                    pass
        except Exception:
            pass

    @QtCore.Slot(int)
    def on_tab_changed(self, index: int) -> None:
        if not (0 <= index < len(self._view_tabs)):
            return
        if self._in_tab_change:
            return
        self._in_tab_change = True
        logger.info("[ViewWorkspaceController] on_tab_changed index=%d view=%s", index, self._view_tabs[index].view_id[:8])
        try:
            vt = self._view_tabs[index]
            if self._active_view_id != vt.view_id:
                self._active_view_id = vt.view_id
                if self._session is None or not hasattr(self._session, "execute"):
                    raise RuntimeError("Session required for set_active_view")
                self._session.execute("set_active_view", view_id=vt.view_id)
            # Refresh the view so data mutated while this tab was inactive is visible
            if getattr(vt, '_needs_full_reload', False):
                vt.reload(vt.view_id, invalidate_tiles="all")
                vt._needs_full_reload = False
            else:
                vt.reload(vt.view_id)
            try:
                view = self.workspace_read_model.get_view(vt.view_id)
                cube = self.workspace_read_model.get_cube(view.get("cube_id", "")) if view else None
                if cube:
                    self._rule_panel.set_active_cube(cube.get("id"))
                    self._flow_panel.set_active_cube(cube.get("id"))
                    self._circular_refs_panel.set_active_cube(cube.get("id"))
            except Exception:
                self._rule_panel.set_active_cube(None)
                self._flow_panel.set_active_cube(None)
                self._circular_refs_panel.set_active_cube(None)
            self.view_changed.emit(vt.view_id)
            self.connect_table_signals()
            self._sync_flow_panel_from_current()
        finally:
            self._in_tab_change = False
        # Explicitly focus the active grid after the tab change is committed.
        self.focus_active_grid()

    def activate_view(self, view_id: str) -> None:
        """Switch to the given view tab if it exists; otherwise trigger full rebuild."""
        for i, vt in enumerate(self._view_tabs):
            if vt.view_id == view_id:
                self._active_view_id = view_id
                self._tabs.setCurrentIndex(i)
                return
        self.rebuild_tabs()

    def add_view_tab(self, view_id: str) -> None:
        """Add a single new view tab incrementally without full rebuild."""
        if any(vt.view_id == view_id for vt in self._view_tabs):
            return
        view = self.workspace_read_model.get_view(view_id)
        if view is None:
            return
        host = self.parent()
        vt = ViewTab(
            view_id=view_id,
            session=self.cell_read_model.session,
            parent=self._tabs,
            workspace_read_model=self.workspace_read_model,
            profiler=self._profiler,
        )
        vt.pivot_bar.selection_changed.connect(self.refresh_table)
        vt.page_axis_bar.selection_changed.connect(self.refresh_table)
        if host is not None and hasattr(host, "_on_add_item_to_dim"):
            vt.add_item_requested.connect(getattr(host, "_on_add_item_to_dim"))
        if host is not None and hasattr(host, "_on_attach_dimension_to_cube"):
            vt.add_dim_requested.connect(getattr(host, "_on_attach_dimension_to_cube"))
        vt.workspace_changed.connect(self.workspace_changed)
        vt.presentation_changed.connect(self._on_presentation_changed)
        self._tabs.addTab(vt, view.get("name", ""))
        self._view_tabs.append(vt)
        idx = len(self._view_tabs) - 1
        self._active_view_id = view_id
        self._tabs.setCurrentIndex(idx)
        self._tabs.tabBar().update()
        self._tabs.update()

    def _disconnect_table_signals(self) -> None:
        table = self._connected_table
        if table is None:
            self._connected_table = None
            return

        # Disconnect controller-owned listeners first (independent from host).
        try:
            if isinstance(table, MatrixGrid):
                table.content_changed.disconnect(self._on_local_data_changed)
                table.presentation_changed.disconnect(self._on_presentation_changed)
                table.outline_changed.disconnect(self._on_outline_changed)
            else:
                model = getattr(table, "model", lambda: None)()
                if model is not None:
                    model.dataChanged.disconnect(self._on_local_data_changed)
        except Exception:
            pass

        host = self.parent()
        if host is None:
            self._connected_table = None
            return

        try:
            if isinstance(table, MatrixGrid):
                if hasattr(host, "_on_selection_changed"):
                    table.selection_changed.disconnect(getattr(host, "_on_selection_changed"))
                if hasattr(host, "_on_matrix_content_changed"):
                    table.content_changed.disconnect(getattr(host, "_on_matrix_content_changed"))
            else:
                sm = table.selectionModel()  # type: ignore[union-attr]
                if sm is not None:
                    if hasattr(host, "_on_current_changed"):
                        sm.currentChanged.disconnect(getattr(host, "_on_current_changed"))
                    if hasattr(host, "_on_selection_changed"):
                        sm.selectionChanged.disconnect(getattr(host, "_on_selection_changed"))
                model = getattr(table, "model", lambda: None)()
                if model is not None and hasattr(host, "_on_any_data_changed"):
                    model.dataChanged.disconnect(getattr(host, "_on_any_data_changed"))
        except Exception:
            pass
        self._connected_table = None

    def connect_table_signals(self) -> None:
        if not self._view_tabs or self._in_connect_table_signals:
            return
        # Block outline-changed propagation while reconnecting signals, otherwise
        # connect_table_signals -> outline_changed -> workspace_changed ->
        # reload_active_view -> connect_table_signals loops at launch.
        self._in_connect_table_signals = True
        was_in_tab_change = self._in_tab_change
        self._in_tab_change = True
        try:
            self._connect_table_signals_body()
        finally:
            self._in_tab_change = was_in_tab_change
            self._in_connect_table_signals = False

    def _connect_table_signals_body(self) -> None:
        self._disconnect_table_signals()

        host = self.parent()
        table = self.active_table
        if host is None or table is None:
            return

        current_tab = self.current_tab
        dock_format = getattr(host, "_dock_format", None)
        if dock_format is not None and self._format_connected_tab is not current_tab:
            if self._format_connected_tab is not None:
                try:
                    dock_format.format_changed.disconnect(self._format_connected_tab.on_format_changed)
                except (TypeError, RuntimeError):
                    pass
            try:
                dock_format.format_changed.connect(current_tab.on_format_changed)
            except (TypeError, RuntimeError):
                pass
            self._format_connected_tab = current_tab

        if isinstance(table, MatrixGrid):
            table.content_changed.connect(
                self._on_local_data_changed,
                QtCore.Qt.ConnectionType.UniqueConnection,
            )
            table.selection_changed.connect(
                self._on_local_selection_changed,
                QtCore.Qt.ConnectionType.UniqueConnection,
            )
            table.presentation_changed.connect(
                self._on_presentation_changed,
                QtCore.Qt.ConnectionType.UniqueConnection,
            )
            table.outline_changed.connect(
                self._on_outline_changed,
                QtCore.Qt.ConnectionType.UniqueConnection,
            )
            if hasattr(host, "_on_selection_changed"):
                table.selection_changed.connect(
                    getattr(host, "_on_selection_changed"),
                    QtCore.Qt.ConnectionType.UniqueConnection,
                )
            if hasattr(host, "_on_matrix_content_changed"):
                table.content_changed.connect(
                    getattr(host, "_on_matrix_content_changed"),
                    QtCore.Qt.ConnectionType.UniqueConnection,
                )
        else:
            sm = table.selectionModel()  # type: ignore[union-attr]
            if sm is not None:
                if hasattr(host, "_on_current_changed"):
                    sm.currentChanged.connect(
                        getattr(host, "_on_current_changed"),
                        QtCore.Qt.ConnectionType.UniqueConnection,
                    )
                if hasattr(host, "_on_selection_changed"):
                    sm.selectionChanged.connect(
                        getattr(host, "_on_selection_changed"),
                        QtCore.Qt.ConnectionType.UniqueConnection,
                    )
                sm.currentChanged.connect(
                    self._on_local_selection_changed,
                    QtCore.Qt.ConnectionType.UniqueConnection,
                )
                sm.selectionChanged.connect(
                    self._on_local_selection_changed,
                    QtCore.Qt.ConnectionType.UniqueConnection,
                )
            model = getattr(table, "model", lambda: None)()
            if model is not None:
                model.dataChanged.connect(
                    self._on_local_data_changed,
                    QtCore.Qt.ConnectionType.UniqueConnection,
                )
                if hasattr(host, "_on_any_data_changed"):
                    model.dataChanged.connect(
                        getattr(host, "_on_any_data_changed"),
                        QtCore.Qt.ConnectionType.UniqueConnection,
                    )

        self._connected_table = table
        self.undo_state_changed.emit()
        self.copy_paste_state_changed.emit()
        self.table_selection_changed.emit()
        self.table_focus_requested.emit()
        self._sync_flow_panel_from_current()

    def reload_workspace(self) -> None:
        """Rebuild tabs and reconnect signals to reflect current workspace state."""
        self.rebuild_tabs()
        self.connect_table_signals()

    @QtCore.Slot()
    def _on_presentation_changed(self) -> None:
        """Propagate visual-only changes without forcing a full recompute.

        Note: We do NOT emit data_changed here because presentation changes
        (column widths, scroll position) are visual-only and shouldn't trigger
        rule input bar syncs or other data-dependent UI updates.
        """
        pass

    @QtCore.Slot()
    def _on_outline_changed(self) -> None:
        """Propagate outline structure changes to other workspace windows."""
        # connect_table_signals sets _in_tab_change while reconnecting signals to
        # prevent the outline_changed -> workspace_changed -> reload loop.
        if self._in_tab_change:
            return
        # Defer the workspace_changed signal to allow any pending scroll
        # restoration to complete first. This prevents reloads triggered
        # by workspace_changed from resetting the scroll position.
        QtCore.QTimer.singleShot(0, self.workspace_changed.emit)

    @QtCore.Slot()
    @QtCore.Slot(QtCore.QModelIndex, QtCore.QModelIndex, list)
    def _on_local_data_changed(self, *args: object) -> None:
        sender = self.sender()
        DEBUG_GUI and print(f"[DEBUG] _on_local_data_changed called, sender={type(sender).__name__ if sender else None}, args={args}")
        DEBUG_GUI and print(f"DEBUG _on_local_data_changed: emitting signals and rebuilding rule panel")
        self.undo_state_changed.emit()
        self.copy_paste_state_changed.emit()
        self.data_changed.emit()
        self._sync_rule_bar_from_current()
        # Rebuild this controller's rule panel
        self.rebuild_rule_panel()
        # Invalidate cell cache and repaint to show updated values
        current = self.current_tab
        if current and isinstance(current.table, MatrixGrid):
            current.table.viewport().update()
        # Also sync after a short delay to handle race condition with refresh_all_views
        QtCore.QTimer.singleShot(0, self._sync_rule_bar_from_current)

    def focus_active_grid(self) -> None:
        table = self.active_table
        if table is None:
            return
        if isinstance(table, MatrixGrid):
            if table.isVisible():
                # Defer focus so any in-flight tab transition finishes first.
                QtCore.QTimer.singleShot(0, lambda: self._focus_matrix_grid(table))
            return
        # Only steal focus for non-grid tables if window is already active
        win = self._pane.window() if self._pane else None
        if win is not None and not win.isActiveWindow():
            return
        try:
            table.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)  # type: ignore[union-attr]
        except Exception:
            pass

    def _focus_matrix_grid(self, table: MatrixGrid) -> None:
        if table.isVisible():
            table.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
            table.viewport().update()

    def refresh_table(self) -> None:
        with self._span("ViewWorkspaceController.refresh_table"):
            logger.info("[ViewWorkspaceController] refresh_table active=%s", self._active_view_id[:8] if self._active_view_id else None)
            if not self._view_tabs:
                return
            current = self.current_tab
            current.reload(invalidate_tiles="data")
            self.table_selection_changed.emit()
            self.copy_paste_state_changed.emit()
            self._sync_flow_panel_from_current()

    def refresh_all_views(self) -> None:
        with self._span("ViewWorkspaceController.refresh_all_views"):
            import time
            t0 = time.perf_counter()
            logger.info("[ViewWorkspaceController] refresh_all_views called tabs=%d", len(self._view_tabs))
            if not self._view_tabs:
                DEBUG_GUI and print(f"[REFRESH] No view tabs, returning immediately")
                return

            active_idx = self._tabs.currentIndex()
            DEBUG_GUI and print(f"[REFRESH] Lazy refresh of {len(self._view_tabs)} view tabs; active={active_idx}")
            for i, vt in enumerate(self._view_tabs):
                if i == active_idx:
                    vt_t0 = time.perf_counter()
                    vt.reload(vt.view_id, invalidate_tiles="all")
                    vt_t1 = time.perf_counter()
                    DEBUG_GUI and print(f"[REFRESH]   Active tab {i+1}/{len(self._view_tabs)} ({vt.view_id}): {(vt_t1-vt_t0)*1000:.1f} ms")
                else:
                    vt._needs_full_reload = True

            t1 = time.perf_counter()
            DEBUG_GUI and print(f"[REFRESH] Active tab reloaded; {len(self._view_tabs)-1} deferred: {(t1-t0)*1000:.1f} ms")

            sig_t0 = time.perf_counter()
            self.table_selection_changed.emit()
            self.copy_paste_state_changed.emit()
            self._sync_flow_panel_from_current()
            sig_t1 = time.perf_counter()
            DEBUG_GUI and print(f"[REFRESH] Signals and sync: {(sig_t1-sig_t0)*1000:.1f} ms")

            t2 = time.perf_counter()
            DEBUG_GUI and print(f"[REFRESH] TOTAL: {(t2-t0)*1000:.1f} ms")

    def rebuild_rule_panel(self) -> None:
        import time
        t0 = time.perf_counter()
        
        # Only rebuild panels that are currently visible
        current_tab = self._pane.lower_tabs.currentIndex()
        
        fp_t0 = time.perf_counter()
        self._rule_panel.rebuild()
        fp_t1 = time.perf_counter()
        DEBUG_GUI and print(f"[REBUILD] Rule panel: {(fp_t1-fp_t0)*1000:.1f} ms")
        
        if current_tab == 1:  # Calculation Flow tab visible
            flow_t0 = time.perf_counter()
            self._flow_panel.rebuild()
            flow_t1 = time.perf_counter()
            DEBUG_GUI and print(f"[REBUILD] Flow panel: {(flow_t1-flow_t0)*1000:.1f} ms")
        
        if current_tab == 2:  # Circular References tab visible
            circ_t0 = time.perf_counter()
            self._circular_refs_panel.rebuild()
            circ_t1 = time.perf_counter()
            DEBUG_GUI and print(f"[REBUILD] Circular refs panel: {(circ_t1-circ_t0)*1000:.1f} ms")
        
        t1 = time.perf_counter()
        DEBUG_GUI and print(f"[REBUILD] TOTAL: {(t1-t0)*1000:.1f} ms")

    def _active_view_layout(self, view_id: str | None) -> dict[str, Any] | None:
        if view_id is None:
            return None
        view = self.workspace_read_model.get_view(view_id)
        if view is None:
            return None
        return {
            "row_dim_ids": list(view.get("row_dim_ids", []) or []),
            "col_dim_ids": list(view.get("col_dim_ids", []) or []),
            "page_dim_ids": list(view.get("page_dim_ids", []) or []),
        }

    def reload_active_view(self) -> None:
        import time
        t0 = time.perf_counter()
        with self._span("ViewWorkspaceController.reload_active_view"):
            logger.info("[ViewWorkspaceController] reload_active_view")
            if getattr(self, '_in_reload_active_view', False):
                return
            self._in_reload_active_view = True
            try:
                # Skip rebuild if header editor is active, but defer reload for after editor closes
                table = self.active_table
                DEBUG_GUI and print(f"DEBUG reload_active_view: table={type(table).__name__ if table else None}")
                if isinstance(table, MatrixGrid):
                    editor_visible = table._editor.isVisible()
                    has_ctx = table._header_edit_ctx is not None
                    DEBUG_GUI and print(f"DEBUG reload_active_view: editor_visible={editor_visible} has_ctx={has_ctx}")
                    if editor_visible and has_ctx:
                        DEBUG_GUI and print(f"DEBUG reload_active_view: deferring reload due to active header editor")
                        # Defer reload until after current event loop iteration
                        QtCore.QTimer.singleShot(0, self._deferred_reload_active_view)
                        return

                # Only rebuild tabs when views have actually changed structurally.
                # Data-only changes (cell edits) just need a table refresh, not a
                # full tab rebuild that destroys grids and causes visible flicker.
                views = self.workspace_read_model.list_views()
                current_ids = {vt.view_id for vt in self._view_tabs}
                new_ids = {v.get("id") for v in views if v.get("id")}
                if not self._view_tabs and not views:
                    DEBUG_GUI and print(f"DEBUG reload_active_view: no tabs and no views, skipping")
                    return
                if current_ids != new_ids or len(self._view_tabs) != len(views):
                    DEBUG_GUI and print(f"DEBUG reload_active_view: view list changed, calling rebuild_tabs")
                    self.rebuild_tabs()
                else:
                    current = self.current_tab
                    current_layout = self._active_view_layout(self._active_view_id)
                    last_layout = getattr(current, "_last_known_layout", None)
                    DEBUG_GUI and print(f"[RELOAD] layout check active={self._active_view_id[:8] if self._active_view_id else None} current={current_layout} last={last_layout}")
                    if current_layout is not None and current_layout == last_layout:
                        DEBUG_GUI and print(f"[RELOAD] layout unchanged, refreshing table")
                        self.refresh_table()
                    else:
                        DEBUG_GUI and print(f"[RELOAD] layout changed, full rebuild via _rebuild_bars")
                        current._rebuild_bars()
                self.connect_table_signals()
            finally:
                self._in_reload_active_view = False
        DEBUG_GUI and print(f"[RELOAD] reload_active_view total: {(time.perf_counter() - t0)*1000:.1f} ms")
        DEBUG_GUI and print(f"[RELOAD] reload_active_view returning")

    def _deferred_reload_active_view(self) -> None:
        """Deferred reload that runs after editor closes."""
        table = self.active_table
        if isinstance(table, MatrixGrid):
            editor_visible = table._editor.isVisible()
            has_ctx = table._header_edit_ctx is not None
            if editor_visible and has_ctx:
                # Editor still active, defer again
                QtCore.QTimer.singleShot(50, self._deferred_reload_active_view)
                return
        DEBUG_GUI and print(f"DEBUG _deferred_reload_active_view: executing deferred reload")
        # Delegate to reload_active_view so we only rebuild tabs when the
        # view list has actually changed.  Previously this unconditionally
        # called rebuild_tabs(), which destroyed and recreated the grid
        # and wiped in-memory selection state after dimension item renames.
        self.reload_active_view()

    def active_view_has_errors(self) -> bool:
        return False

    def use_matrix(self, enabled: bool) -> None:
        if not self._view_tabs:
            return
        self.current_tab.set_prefer_matrix(enabled)
        self.connect_table_signals()

    def focus_view(self, view_id: str) -> bool:
        # First check if view exists in current tabs
        for i, vt in enumerate(self._view_tabs):
            if vt.view_id == view_id:
                self._active_view_id = view_id
                self._tabs.setCurrentIndex(i)
                return True
        # View not found - rebuild tabs from engine to pick up new views
        self.rebuild_tabs()
        # Try again after rebuild
        for i, vt in enumerate(self._view_tabs):
            if vt.view_id == view_id:
                self._active_view_id = view_id
                self._tabs.setCurrentIndex(i)
                return True
        return False

    @QtCore.Slot()
    def _focus_view_slot(self) -> None:
        """Thread-safe slot for REPL to request view focus on GUI thread."""
        view_id = getattr(self, '_pending_focus_view', None)
        if view_id:
            self.focus_view(view_id)
            self._pending_focus_view = None

    def _selected_cell_cube_and_addr(self) -> tuple[str, tuple[str, ...]] | None:
        if not self._active_view_id:
            return None
        table = self.active_table
        if table is None:
            return None

        view = self.workspace_read_model.get_view(self._active_view_id)
        cube_id = None
        if view is not None:
            cube_id = view.get("cube_id", "")

        addr: tuple[str, ...] | None = None
        if isinstance(table, MatrixGrid):
            keys = table.selected_keys()
            if keys is None:
                return None
            row_key, col_key = keys
            addr = self.cell_read_model.addr_for_view_keys(self._active_view_id, row_key, col_key)
        else:
            idx = table.currentIndex()  # type: ignore[union-attr]
            if not idx.isValid():
                return None
            tm = self.current_tab.tree_model
            if tm is not None:
                if idx.column() <= 0:
                    return None
                row_key = tm.row_key_for_index(idx)
                col_key = tm.col_key_for_column(idx.column())
                if row_key is None or col_key is None:
                    return None
                addr = self.cell_read_model.addr_for_view_keys(self._active_view_id, row_key, col_key)
            else:
                # D.5: tree_model is None — cannot resolve keys; skip instead of engine fallback
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
        current_tab = self._pane.lower_tabs.currentIndex()
        if current_tab == 1:  # Calculation Flow tab
            self._flow_panel.set_focus_cell(cube_id, addr)
        elif current_tab == 2:  # Circular References tab
            self._circular_refs_panel.set_focus_cell(cube_id, addr)

    def _sync_rule_bar_from_current(self) -> None:
        """Synchronize the rule input bar with the currently selected cell.
        
        This mirrors the behavior of MainWindow._sync_rule_bar_from_current
        for secondary workspace windows.
        """
        DEBUG_GUI and print(f"DEBUG _sync_rule_bar: _active_view_id={self._active_view_id}")
        DEBUG_GUI and print(f"DEBUG _sync_rule_bar: controller id={id(self)}, pane id={id(self._pane) if self._pane else None}")
        if self._active_view_id is None:
            DEBUG_GUI and print("DEBUG _sync_rule_bar: no active view, returning")
            return
        
        table = self.active_table
        DEBUG_GUI and print(f"DEBUG _sync_rule_bar: table={type(table).__name__ if table else None}")
        if table is None:
            DEBUG_GUI and print("DEBUG _sync_rule_bar: no table, returning")
            return
        
        # Get the rule input bar from the pane
        rule_bar = self._pane.rule_bar
        DEBUG_GUI and print(f"DEBUG _sync_rule_bar: rule_bar={rule_bar}, text before='{rule_bar.text()}'")
        
        # Get cell info
        cell_dto: dict | None = None
        cube_id: str | None = None
        addr: tuple[str, ...] | None = None
        
        if isinstance(table, MatrixGrid):
            keys = table.selected_keys()
            DEBUG_GUI and print(f"DEBUG _sync_rule_bar: keys={keys}")
            if keys is None:
                DEBUG_GUI and print("DEBUG _sync_rule_bar: no keys, returning")
                return
            row_key, col_key = keys
            cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
            cube_id = cell_dto.get("cube_id")
            addr = self.cell_read_model.addr_for_view_keys(self._active_view_id, row_key, col_key)
        else:
            idx = table.currentIndex()  # type: ignore[union-attr]
            if not idx.isValid():
                DEBUG_GUI and print("DEBUG _sync_rule_bar: invalid index, returning")
                return
            tm = self.current_tab.tree_model
            if tm is not None:
                if idx.column() <= 0:
                    DEBUG_GUI and print("DEBUG _sync_rule_bar: col <= 0, returning")
                    return
                row_key = tm.row_key_for_index(idx)
                col_key = tm.col_key_for_column(idx.column())
                if row_key is None or col_key is None:
                    DEBUG_GUI and print("DEBUG _sync_rule_bar: no row/col key, returning")
                    return
                cell_dto = self.cell_read_model.get_cell(self._active_view_id, row_key, col_key)
                cube_id = cell_dto.get("cube_id")
                addr = self.cell_read_model.addr_for_view_keys(self._active_view_id, row_key, col_key)
            else:
                # D.5: tree_model is None — cannot resolve keys; skip instead of engine fallback
                DEBUG_GUI and print("DEBUG _sync_rule_bar: tree_model is None, skipping")
                return
        
        if cell_dto is None:
            DEBUG_GUI and print("DEBUG _sync_rule_bar: no cell, returning")
            return
        
        # Prefer anchored rule first, then read-model fallback.
        expr_text: str | None = None
        DEBUG_GUI and print(f"DEBUG _sync_rule_bar: cube_id={cube_id}, addr={addr}")
        if cube_id is not None and addr is not None:
            expr_text = self.cell_read_model.rule_detail(cube_id, addr)
            if expr_text:
                DEBUG_GUI and print(f"DEBUG _sync_rule_bar: rule expression={expr_text}")
        
        # If no per-cell rule, fall back to showing the expression for error cells
        cell_explain = cell_dto.get("explain")
        if expr_text is None and isinstance(cell_explain, dict):
            if cell_explain.get("source") == "error" and cell_explain.get("rule_body") is not None:
                expr_text = cell_explain["rule_body"]
        
        # Update the rule input bar
        DEBUG_GUI and print(f"DEBUG _sync_rule_bar: expr_text={expr_text}, setting rule_bar text")
        if expr_text is not None:
            rule_bar.setText(f"={expr_text}")
        else:
            cell_value = cell_dto.get("value")
            rule_bar.setText("" if cell_value is None else str(cell_value))
        DEBUG_GUI and print(f"DEBUG _sync_rule_bar: text after='{rule_bar.text()}'")

    @QtCore.Slot()
    @QtCore.Slot(object)
    @QtCore.Slot(QtCore.QModelIndex, QtCore.QModelIndex)
    def _on_local_selection_changed(self, *args: object) -> None:
        # Only propagate signal if this is the active table
        sender = self.sender()
        active = self.active_table
        DEBUG_GUI and print(f"DEBUG _on_local_sel_changed: sender={id(sender) if sender else None} active={id(active) if active else None} match={sender==active}")
        if sender is not None and sender != active:
            return
        self.table_selection_changed.emit()
        self._sync_flow_panel_from_current()
        self._sync_rule_bar_from_current()

    @QtCore.Slot()
    def _on_rules_changed(self) -> None:
        """Emit rules_changed signal to notify MainWindow of rule changes."""
        self.rules_changed.emit()

    def _on_rule_reordered(self) -> None:
        """Handle rule reorder via drag-drop - engine already recalculated."""
        # Engine already invalidated cubes and recomputed dirty nodes in
        # cmd_set_rule_order.  Just mark dirty and request grid reload.
        self.mark_dirty_requested.emit()
        self.data_changed.emit()

    @QtCore.Slot()
    def _on_rule_bar_enter(self) -> None:
        """Handle rule input bar Enter key - process rules, cell rules, or values."""
        text = self._pane.rule_bar.text().strip()
        if not text:
            return
        # Allow user to paste with surrounding quotes
        if text.startswith('"') and text.endswith('"') and len(text) >= 2:
            text = text[1:-1].strip()

        # Rule syntax: LHS = expr, where LHS follows Improv-style ref rules
        import re
        # Split only on the first '=' so expressions may contain '=' in IF, etc.
        m = None if text.lstrip().startswith("=") else re.match(r"^=?\s*(.*?)\s*=\s*(.+)$", text)
        DEBUG_GUI and print(f"DEBUG VWCTRL: m={m}, text='{text}'")
        if m:
            lhs_raw = m.group(1).strip()
            DEBUG_GUI and print(f"DEBUG VWCTRL: lhs_raw='{lhs_raw}'")
            if lhs_raw == "*":
                lhs_raw = "*.*"
            expr = m.group(2).strip()
            DEBUG_GUI and print(f"DEBUG VWCTRL: expr='{expr}', lhs_raw='{lhs_raw}'")
            if lhs_raw:
                # Slice 3: GUI no longer parses rule targets locally.
                view = self.workspace_read_model.get_view(self._active_view_id)
                if view is None:
                    return
                cube_id = view.get("cube_id", "")
                resolve = self.cell_read_model.session.query("rule_target_resolve", cube_id=cube_id, lhs=lhs_raw)
                if resolve.get("error"):
                    DEBUG_GUI and print(f"DEBUG VWCTRL: rule_target_resolve failed: {resolve['error']}")
                    # If the user did not start with '=', they most likely
                    # intended a rule; in that case surface the parse error.
                    if not text.lstrip().startswith("="):
                        QtWidgets.QMessageBox.critical(self._pane, "Rule error", resolve["error"])
                        return
                    # Otherwise, treat as normal cell entry (fall through below)
                else:
                    targets = resolve["targets"]
                    DEBUG_GUI and print(f"DEBUG VWCTRL: rule_target_resolve succeeded: {targets}")
                    try:
                        DEBUG_GUI and print(f"DEBUG VWCTRL: calling set_rule command")
                        result = self.cell_read_model.session.execute(
                            "set_rule",
                            cube_id=cube_id,
                            targets=targets,
                            expression=expr,
                            is_anchored=False,
                        )
                        if result.status.name == "ERROR":
                            raise RuntimeError(result.error or "rule failed")
                        DEBUG_GUI and print(f"DEBUG VWCTRL: rule command succeeded")
                    except Exception as e:
                        DEBUG_GUI and print(f"DEBUG VWCTRL: rule command failed: {e}")
                        QtWidgets.QMessageBox.critical(self._pane, "Rule error", str(e))
                        self._on_rules_changed()
                        return
                    if isinstance(self.active_table, MatrixGrid):
                        self.refresh_table()
                    self.rebuild_rule_panel()
                    self._sync_rule_bar_from_current()
                    self._on_rules_changed()
                    # Mark workspace as dirty
                    win = self._pane.window()
                    if hasattr(win, "_mark_dirty"):
                        win._mark_dirty(True)
                    DEBUG_GUI and print(f"DEBUG VWCTRL: returning after rule creation")
                    return
            else:
                DEBUG_GUI and print(f"DEBUG VWCTRL: lhs_raw is falsy, skipping rule handling")
        else:
            DEBUG_GUI and print(f"DEBUG VWCTRL: m is None, falling through")

        table = self.active_table
        if table is None:
            return

        if isinstance(table, MatrixGrid):
            keys = table.selected_keys()
            if keys is None:
                return
            row_key, col_key = keys
            if text.startswith("="):
                expr = text[1:]
                try:
                    self._execute_rule_for_cell(
                        self._active_view_id, row_key=row_key, col_key=col_key, expression=expr
                    )
                except Exception as e:
                    QtWidgets.QMessageBox.critical(self._pane, "Rule error", str(e))
                    self._on_rules_changed()
                    return
            else:
                self.cell_read_model.session.execute(
                    "set_cell_hardvalue",
                    view_id=self._active_view_id,
                    cell_ref={
                        "kind": "keys",
                        "value": {"row_key": list(row_key), "col_key": list(col_key)},
                    },
                    value=text,
                )
            self.refresh_table()
            self.rebuild_rule_panel()
            self._sync_rule_bar_from_current()
            self._on_rules_changed()
            # Mark workspace as dirty
            win = self._pane.window()
            if hasattr(win, "_mark_dirty"):
                win._mark_dirty(True)
            return

        # Handle non-MatrixGrid tables (TreeView)
        idx = table.currentIndex()
        if not idx.isValid():
            return
        tm = self.current_tab.tree_model
        if tm is not None:
            if idx.column() <= 0:
                return
            row_key = tm.row_key_for_index(idx)
            col_key = tm.col_key_for_column(idx.column())
            if row_key is None or col_key is None:
                return
            if text.startswith("="):
                try:
                    self._execute_rule_for_cell(
                        self._active_view_id, row_key=row_key, col_key=col_key, expression=text[1:]
                    )
                except Exception as e:
                    QtWidgets.QMessageBox.critical(self._pane, "Rule error", str(e))
                    self._on_rules_changed()
                    return
            else:
                self.cell_read_model.session.execute(
                    "set_cell_hardvalue",
                    view_id=self._active_view_id,
                    cell_ref={
                        "kind": "keys",
                        "value": {"row_key": list(row_key), "col_key": list(col_key)},
                    },
                    value=text,
                )
            tm.dataChanged.emit(idx, idx, [QtCore.Qt.ItemDataRole.DisplayRole])
        else:
            # D.5: tree_model is None — cannot resolve keys; skip instead of engine fallback
            DEBUG_GUI and print("DEBUG _on_rule_bar_enter: tree_model is None, skipping non-MatrixGrid path")
            return

        self._sync_rule_bar_from_current()
        self._on_rules_changed()
        # Mark workspace as dirty
        win = self._pane.window()
        if hasattr(win, "_mark_dirty"):
            win._mark_dirty(True)

    def _resolve_rule_targets(
        self, view_id: str, row_key: tuple[str, ...] | None = None, col_key: tuple[str, ...] | None = None
    ) -> list[tuple[str, str]]:
        """Resolve view keys to rule targets: list of (dim_name, item_name) pairs."""
        view = self.workspace_read_model.get_view(view_id)
        if view is None:
            return []
        cube = self.workspace_read_model.get_cube(view.get("cube_id", ""))
        if cube is None:
            return []
        if row_key is not None and col_key is not None:
            addr = self.cell_read_model.addr_for_view_keys(view_id, row_key, col_key)
        else:
            addr = ()
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
        targets = self._resolve_rule_targets(view_id, row_key=row_key, col_key=col_key)
        view = self.workspace_read_model.get_view(view_id)
        if view is None:
            raise RuntimeError("View not found for rule execution")
        cube_id = view.get("cube_id", "")
        result = self.cell_read_model.session.execute(
            "set_rule",
            cube_id=cube_id,
            targets=targets,
            expression=expression,
            is_anchored=True,
        )
        if result.status.name == "ERROR":
            raise RuntimeError(result.error or "rule failed")
