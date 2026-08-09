from __future__ import annotations

import logging
import os

from PySide6 import QtCore, QtGui, QtWidgets

from lib_gui.pivot_bar import PageAxisBar, PivotBar, TopLeftChipBar
from lib_gui_elements.matrix_grid import MatrixGrid
from lib_command.core.session import CommandSession
from lib_gui.grid_read_model import GridReadModel
from lib_gui.outline_read_model import OutlineReadModel
from lib_gui.workspace_read_model import WorkspaceReadModel
from lib_utils.gui_profiler import GuiProfiler, NOOP_SPAN

# Debug flag for GUI - set DEBUG_GUI=true to enable verbose logging
DEBUG_GUI = os.environ.get("DEBUG_GUI", "false").lower() in ("true", "1", "yes")

_TAB_BG = "#d8dce4"          # grey tab background
_CANVAS_BORDER = "#a0a8b8"   # canvas frame border colour


class ViewTab(QtWidgets.QWidget):
    """Tab page: grey background with a bordered canvas frame containing the grid.

    Canvas layout (pivot-table-like):
    ┌─ canvas ──────────────────────────────────┬──────────┐
    │  [top-left chips: extra page dims]        │ col ▾    │
    ├───────────────────────────────────────────┤ page ▾   │
    │              GRID                         │  ...     │
    ├───────────────────────────────────────────┤          │
    │  [row chip] [+]                           │          │
    └───────────────────────────────────────────┴──────────┘
    """

    add_item_requested = QtCore.Signal(str)  # dim_id — forwarded from PivotBar
    # axis label for + buttons: "row", "col", or "page"
    add_dim_requested = QtCore.Signal(str)
    workspace_changed = QtCore.Signal()       # request MainWindow to rebuild model browser / tabs
    presentation_changed = QtCore.Signal()    # visual-only tweak (formats, selection styling, etc.)

    def __init__(
        self,
        *,
        view_id: str,
        session: CommandSession,
        parent: QtWidgets.QWidget | None = None,
        grid_read_model=None,
        workspace_read_model=None,
        outline_read_model=None,
        profiler: GuiProfiler | None = None,
        defer_table_reload: bool = False,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._view_id = view_id
        self._grid_read_model = grid_read_model or GridReadModel(session) if session is not None else None
        self._workspace_read_model = workspace_read_model or WorkspaceReadModel(session) if session is not None else None
        # Share the gui_view_model from workspace_read_model so OutlineReadModel
        # can use cached dimension snapshots instead of making RPC calls.
        _gvm = getattr(self._workspace_read_model, "_gui_view_model", None) if self._workspace_read_model is not None else None
        self._outline_read_model = outline_read_model or OutlineReadModel(session, gui_view_model=_gvm) if session is not None else None
        self._profiler = profiler
        self._span = profiler.span if profiler is not None else NOOP_SPAN
        self._tree_model: TreeSliceTableModel | None = None
        self._needs_full_reload: bool = defer_table_reload
        self._last_known_layout: dict[str, Any] | None = None

        # Grey tab background
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QtGui.QColor(_TAB_BG))
        self.setPalette(pal)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        # ── Canvas frame ──────────────────────────────────────────
        self._canvas = QtWidgets.QFrame(self)
        self._canvas.setFrameShape(QtWidgets.QFrame.Shape.Box)
        self._canvas.setLineWidth(1)
        self._canvas.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._canvas.setStyleSheet(
            f"QFrame {{ background: white; border: 1px solid {_CANVAS_BORDER}; }}"
        )

        # Widgets inside canvas
        self.table = MatrixGrid(
            view_id=view_id,
            session=session,
            parent=self._canvas,
            profiler=profiler,
        )

        self.top_left_bar = TopLeftChipBar(view_id=view_id, parent=self._canvas, workspace_read_model=self._workspace_read_model, session=session)
        self.top_left_bar.selection_changed.connect(self._on_selection_changed)
        self.top_left_bar.move_dim.connect(self._on_drop_page)
        self.top_left_bar.add_dim_requested.connect(self.add_dim_requested)

        self.pivot_bar = PivotBar(view_id=view_id, parent=self._canvas, workspace_read_model=self._workspace_read_model, session=session)
        self.pivot_bar.add_item_requested.connect(self.add_item_requested)
        self.pivot_bar.add_dim_requested.connect(self.add_dim_requested)
        self.pivot_bar.move_dim.connect(self._on_drop_row)

        self.page_axis_bar = PageAxisBar(view_id=view_id, parent=self._canvas, workspace_read_model=self._workspace_read_model, session=session)
        self.page_axis_bar.selection_changed.connect(self._on_selection_changed)
        self.page_axis_bar.move_dim.connect(self._on_drop_col)
        self.page_axis_bar.add_dim_requested.connect(self.add_dim_requested)

        # ── Canvas internal grid layout ────────────────────────────
        #   col 0: top-left chips + table + bottom pivot bar  (stretch)
        #   col 1: top-right page-axis chips (fixed width)
        canvas_layout = QtWidgets.QGridLayout(self._canvas)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(0)

        # top_left_bar spans full width so it's always a big, hittable drop target
        canvas_layout.addWidget(self.top_left_bar,  0, 0, 1, 2)
        canvas_layout.addWidget(self.table,         1, 0)
        canvas_layout.addWidget(self.pivot_bar,     2, 0)
        # PageAxisBar only spans row 1, not row 2, to exclude bottom right corner
        canvas_layout.addWidget(self.page_axis_bar, 1, 1, 1, 1)

        canvas_layout.setColumnStretch(0, 1)
        canvas_layout.setColumnStretch(1, 0)
        canvas_layout.setRowStretch(1, 1)

        # ── Outer layout: pad canvas in the grey tab area ──────────
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.addWidget(self._canvas)

        # Ensure the bars reflect the current view state.
        # When defer_table_reload is True (inactive tab), skip the table
        # reload to avoid triggering tile fetch queries for non-visible views.
        self._rebuild_bars(defer_table_reload=defer_table_reload)

    @property
    def view_id(self) -> str:
        return self._view_id

    def _page_dim_ids(self) -> list[str]:
        view = self._workspace_read_model.get_view(self._view_id)
        if view is None:
            return []
        return list(view.get("page_dim_ids", []) or [])

    def _rebuild_bars(self, *, invalidate_tiles: bool | str = False, defer_table_reload: bool = False) -> None:
        view = (
            self._workspace_read_model.get_view(self._view_id)
            if self._workspace_read_model is not None
            else None
        )
        self.table.selection_changed.connect(self._on_selection_changed, QtCore.Qt.ConnectionType.UniqueConnection)
        self.table.outline_changed.connect(self.workspace_changed, QtCore.Qt.ConnectionType.UniqueConnection)
        if not defer_table_reload:
            if invalidate_tiles:
                self.table.reload(invalidate_tiles=invalidate_tiles)
            else:
                self.table.reload()
        self.pivot_bar.rebuild(self._view_id)
        if view is not None:
            self._last_known_layout = {
                "row_dim_ids": list(view.get("row_dim_ids", []) or []),
                "col_dim_ids": list(view.get("col_dim_ids", []) or []),
                "page_dim_ids": list(view.get("page_dim_ids", []) or []),
            }

        # Until full placeholder refactor lands, we treat:
        # - top-left as page dims (filters)
        # - top-right as column dims + remaining page dims (for visibility)
        if view is None:
            tl_ids: list[str] = []
            tr_ids: list[str] = []
        else:
            tl_ids = list(view.get("page_dim_ids", []) or [])
            tr_ids = list(view.get("col_dim_ids", []) or [])
        self.top_left_bar.rebuild(self._view_id, tl_ids)
        self.page_axis_bar.rebuild(self._view_id, tr_ids)
        DEBUG_GUI and print(f"[REBUILD-BARS] done view={self._view_id[:8]}")

    @property
    def tree_model(self) -> TreeSliceTableModel | None:
        return self._tree_model

    @QtCore.Slot(QtCore.QPoint)
    def _on_tree_context_menu(self, pos: QtCore.QPoint) -> None:
        if not isinstance(self.table, QtWidgets.QTreeView):
            return

        if self._tree_model is None:
            return
        idx = self.table.indexAt(pos)
        m = QtWidgets.QMenu(self)

        act_add_group = m.addAction("Add Group…")
        act_add_item = m.addAction("Add Item…")
        act_move_sel = m.addAction("Move Selection To Group…")
        act_ungroup = m.addAction("Ungroup Selection")
        m.addSeparator()
        act_rename = m.addAction("Rename Group…")
        act_delete = m.addAction("Delete Group")

        node = self._tree_model.node_for_index(idx) if idx.isValid() else None
        is_group = node is not None and node.item_id is None and node.parent is not None

        act_move_sel.setEnabled(True)
        act_ungroup.setEnabled(True)
        act_rename.setEnabled(bool(is_group))
        act_delete.setEnabled(bool(is_group))

        chosen = m.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        if chosen == act_add_group:
            name, ok = QtWidgets.QInputDialog.getText(self, "Add Group", "Group name")
            if not ok or not name.strip():
                return
            self._tree_model.add_group(idx if is_group else QtCore.QModelIndex(), name.strip())
            self.table.expandAll()
            return

        if chosen == act_add_item:
            name, ok = QtWidgets.QInputDialog.getText(self, "Add Item", "Item name")
            if not ok or not name.strip():
                return
            parent_ix = idx if is_group else QtCore.QModelIndex()
            self._tree_model.add_item_under(parent_ix, name.strip())
            self.workspace_changed.emit()
            self.table.expandAll()
            return

        if chosen == act_move_sel:
            groups: list[tuple[str, QtCore.QModelIndex]] = [("(root)", QtCore.QModelIndex())]

            def _walk(parent_ix: QtCore.QModelIndex) -> None:
                rc = self._tree_model.rowCount(parent_ix)
                for r in range(rc):
                    ix = self._tree_model.index(r, 0, parent_ix)
                    n = self._tree_model.node_for_index(ix)
                    if n is not None and n.item_id is None:
                        groups.append((n.label, ix))
                        _walk(ix)

            _walk(QtCore.QModelIndex())
            labels = [g[0] for g in groups]
            choice, ok = QtWidgets.QInputDialog.getItem(self, "Move Selection", "Destination group", labels, 0, False)
            if not ok:
                return
            dest_ix = next((ix for (lab, ix) in groups if lab == choice), QtCore.QModelIndex())
            sm = self.table.selectionModel()
            sel = [] if sm is None else [i for i in sm.selectedIndexes() if i.isValid() and i.column() == 0]
            self._tree_model.move_selected_to_group(dest_ix, sel)
            self.table.expandAll()
            return

        if chosen == act_ungroup:
            sm = self.table.selectionModel()
            sel = [] if sm is None else [i for i in sm.selectedIndexes() if i.isValid() and i.column() == 0]
            self._tree_model.move_selected_to_root(sel)
            self.table.expandAll()
            return

        if chosen == act_rename and is_group:
            name, ok = QtWidgets.QInputDialog.getText(self, "Rename Group", "Group name", text=node.label)
            if not ok or not name.strip():
                return
            self._tree_model.rename_group(idx, name.strip())
            return

        if chosen == act_delete and is_group:
            self._tree_model.delete_group(idx)
            return

    def _selected_col_item_ids(self) -> list[str]:
        view = self._workspace_read_model.get_view(self._view_id)
        if view is None:
            return []
        col_dim_ids = view.get("col_dim_ids", [])
        if len(col_dim_ids) != 1:
            return []

        if self._grid_read_model is None:
            return []
        keys = self._grid_read_model.col_keys(self._view_id)

        sm = self.table.selectionModel() if isinstance(self.table, (QtWidgets.QTableView, QtWidgets.QTreeView)) else None
        if sm is None:
            return []

        cols: set[int] = set()
        for ix in sm.selectedIndexes():
            if not ix.isValid():
                continue
            c = ix.column()
            # Tree model has column 0 as label.
            if isinstance(self.table, QtWidgets.QTreeView):
                if c <= 0:
                    continue
                c = c - 1
            cols.add(c)

        out: list[str] = []
        for c in sorted(cols):
            if 0 <= c < len(keys) and len(keys[c]) == 1:
                out.append(keys[c][0])
        return out

    def _ensure_col_outline(self) -> bool:
        view = self._workspace_read_model.get_view(self._view_id)
        if view is None:
            return True
        col_dim_ids = view.get("col_dim_ids", [])
        if len(col_dim_ids) != 1:
            return True
        dim_id = col_dim_ids[0]
        # Phase F: prefer read-model outline snapshot
        outline = (
            self._outline_read_model.dimension_outline(dim_id)
            if self._outline_read_model is not None
            else None
        )
        if outline:
            return True
        if self._session is None:
            logger.warning("Cannot ensure column outline: no session available")
            return False
        result = self._session.execute(
            "set_dimension_outline",
            dim_id=dim_id,
            outline=None,
        )
        if not result.success:
            logger.warning("set_dimension_outline failed: %s", result.error)
            return False
        return True

    @QtCore.Slot(QtCore.QPoint)
    def _on_col_header_context_menu(self, pos: QtCore.QPoint) -> None:
        view = self._workspace_read_model.get_view(self._view_id)
        if view is None:
            return
        col_dim_ids = view.get("col_dim_ids", [])
        if len(col_dim_ids) != 1:
            return
        dim_id = col_dim_ids[0]
        dim = self._workspace_read_model.get_dimension(dim_id)
        if dim is None:
            return

        # Phase F: prefer read-model outline snapshot
        outline_nodes = (
            self._outline_read_model.dimension_outline(dim_id)
            if self._outline_read_model is not None
            else None
        )
        if outline_nodes is None:
            outline_nodes = list(dim.get("outline") or [])

        m = QtWidgets.QMenu(self)
        act_add_group = m.addAction("Add Column Group…")
        act_move_sel = m.addAction("Move Selected Columns To Group…")

        def _walk_groups(nodes: list, prefix: list[int]) -> list[tuple[str, list[int]]]:
            out: list[tuple[str, list[int]]] = []
            for i, n in enumerate(nodes):
                if isinstance(n, dict):
                    item_id = n.get("item_id")
                    label = n.get("label", "")
                    children = n.get("children", [])
                else:
                    item_id = getattr(n, "item_id", None)
                    label = getattr(n, "label", "")
                    children = getattr(n, "children", [])
                if item_id is None:
                    out.append((label, prefix + [i]))
                    out.extend(_walk_groups(children, prefix + [i]))
            return out

        if chosen == act_add_group:
            name, ok = QtWidgets.QInputDialog.getText(self, "Add Column Group", "Group name")
            if not ok or not name.strip():
                return
            if not self._ensure_col_outline():
                return
            if self._session is None:
                raise RuntimeError("No executor available for create_group")
            self._session.execute(
                "create_group",
                dim_id=dim_id,
                label=name.strip(),
            )
            self._rebuild_bars()
            return

        if chosen == act_move_sel:
            if not self._ensure_col_outline():
                return
            selected_item_ids = self._selected_col_item_ids()
            moved = set(selected_item_ids)
            if not moved:
                return
            groups = [("(root)", [])] + _walk_groups(list(outline_nodes), [])
            labels = [g[0] for g in groups]
            choice, ok = QtWidgets.QInputDialog.getItem(self, "Move Columns", "Destination group", labels, 0, False)
            if not ok:
                return
            rootish = choice == "(root)"
            if self._session is None:
                logger.warning("Cannot move items to group: no session available")
                return
            group_ref = {"kind": "root", "value": None} if rootish else {"kind": "label", "value": choice}
            result = self._session.execute(
                "move_items_to_group",
                dim_id=dim_id,
                item_ids=selected_item_ids,
                group_ref=group_ref,
            )
            if not result.success:
                logger.warning("move_items_to_group failed: %s", result.error)
                return
            self._rebuild_bars()
            return

    @QtCore.Slot(QtCore.QPoint)
    def _on_table_context_menu(self, pos: QtCore.QPoint) -> None:
        if not isinstance(self.table, QtWidgets.QTableView):
            return

        view = self._workspace_read_model.get_view(self._view_id)
        if view is None:
            return
        row_dim_ids = view.get("row_dim_ids", [])
        if not row_dim_ids:
            return

        m = QtWidgets.QMenu(self)
        act_add_group = m.addAction("Add Group…")

        chosen = m.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        def _ensure_outline() -> bool:
            row_dim_id = view["row_dim_ids"][0]
            if self._session is None:
                logger.warning("Cannot ensure outline: no session available")
                return False
            result = self._session.execute(
                "set_dimension_outline",
                dim_id=row_dim_id,
                outline=None,
            )
            if not result.success:
                logger.warning("set_dimension_outline failed: %s", result.error)
                return False
            return True

        if chosen == act_add_group:
            name, ok = QtWidgets.QInputDialog.getText(self, "Add Group", "Group name")
            if not ok or not name.strip():
                return
            if not _ensure_outline():
                return
            row_dim_id = view["row_dim_ids"][0]

            # If there is a row selection (single row-dim), move those items into the new group.
            selected_item_ids: list[str] = []
            try:
                sm = self.table.selectionModel()
                if sm is not None:
                    rows = {ix.row() for ix in sm.selectedIndexes() if ix.isValid()}
                    row_keys = self._grid_read_model.row_keys(self._view_id)
                    for r in sorted(rows):
                        if 0 <= r < len(row_keys) and len(row_keys[r]) >= 1:
                            selected_item_ids.append(row_keys[r][0])
            except Exception:
                selected_item_ids = []

            if self._session is None:
                raise RuntimeError("No executor available for create_group")
            self._session.execute(
                "create_group",
                dim_id=row_dim_id,
                label=name.strip(),
                child_item_ids=selected_item_ids if selected_item_ids else None,
            )

            self._rebuild_bars()
            if isinstance(self.table, QtWidgets.QTreeView):
                self.table.expandAll()
            return

    @QtCore.Slot()
    def _on_selection_changed(self) -> None:
        DEBUG_GUI and print(f"[DEBUG _view_tab] _on_selection_changed called, table type={type(self.table).__name__}")
        if isinstance(self.table, MatrixGrid):
            # Cell selection changes only need viewport refresh, never a full reload
            # Page dimension changes are handled separately by chip dropdown handlers
            DEBUG_GUI and print(f"[DEBUG _view_tab] Selection change - refreshing viewport only, mode={self.table._sel_mode}")
            self.table.viewport().update()
            return
        m = self.model
        rows, cols = m.rowCount(), m.columnCount()
        if rows > 0 and cols > 0:
            m.dataChanged.emit(
                m.index(0, 0),
                m.index(rows - 1, cols - 1),
                [QtCore.Qt.ItemDataRole.DisplayRole],
            )

    def _execute_move_dimension(self, dim_id: str, dest: str, index: int | None = None) -> None:
        """Route dimension movement through command spine."""
        if self._session is None:
            raise RuntimeError("No executor available for move_view_dimension")
        result = self._session.execute(
            "move_view_dimension",
            view_id=self._view_id,
            dim_id=dim_id,
            dest=dest,
            index=index,
        )
        if not result.success:
            return
        # Synchronously refresh the cached view snapshot so _rebuild_bars()
        # reads the new layout instead of stale data from before the move.
        gvm = getattr(self._workspace_read_model, "_gui_view_model", None) if self._workspace_read_model else None
        if gvm is not None:
            fresh = self._session.query("view_detail", view_id=self._view_id)
            if fresh:
                gvm.update_view_snapshot(self._view_id, fresh)

    @QtCore.Slot(str)
    def _on_move_to_topleft(self, dim_id: str) -> None:
        try:
            self._execute_move_dimension(dim_id, dest="page")
        except Exception:
            return
        self._rebuild_bars()
        self.workspace_changed.emit()

    @QtCore.Slot(str)
    def _on_move_to_topright(self, dim_id: str) -> None:
        try:
            self._execute_move_dimension(dim_id, dest="col")
        except Exception:
            return
        self._rebuild_bars()
        self.workspace_changed.emit()

    @QtCore.Slot(str, str, int)
    def _on_drop_row(self, dim_id: str, source_zone: str, insert_index: int) -> None:
        try:
            self._execute_move_dimension(dim_id, dest="row", index=insert_index)
        except Exception:
            return
        self._rebuild_bars()
        self.workspace_changed.emit()

    @QtCore.Slot(str, str, int)
    def _on_drop_col(self, dim_id: str, source_zone: str, insert_index: int) -> None:
        try:
            self._execute_move_dimension(dim_id, dest="col", index=insert_index)
        except Exception:
            return
        self._rebuild_bars()
        self.workspace_changed.emit()

    @QtCore.Slot(str, str, int)
    def _on_drop_page(self, dim_id: str, source_zone: str, insert_index: int) -> None:
        try:
            self._execute_move_dimension(dim_id, dest="page", index=insert_index)
        except Exception:
            return
        self._rebuild_bars()
        self.workspace_changed.emit()

    @QtCore.Slot(str)
    def _on_promote_to_page(self, axis_dim_id: str) -> None:
        view_data = (
            self._workspace_read_model.get_view(self._view_id)
            if self._workspace_read_model is not None
            else None
        )
        if not view_data:
            return
        row_dims = view_data.get("row_dim_ids", [])
        col_dims = view_data.get("col_dim_ids", [])
        if not row_dims or not col_dims:
            return
        result = self._session.execute(
            "set_view_axes",
            view_id=self._view_id,
            row_dimension_id=col_dims[0],
            col_dimension_id=row_dims[0],
        )
        if result.status.name != "SUCCESS":
            return
        self._rebuild_bars()

    @QtCore.Slot(str)
    def _on_promote_to_row(self, dim_id: str) -> None:
        view_data = (
            self._workspace_read_model.get_view(self._view_id)
            if self._workspace_read_model is not None
            else None
        )
        if not view_data:
            return
        row_dims = view_data.get("row_dim_ids", [])
        col_dims = view_data.get("col_dim_ids", [])
        page_dims = view_data.get("page_dim_ids", [])
        if not row_dims:
            return
        new_row = dim_id
        if col_dims and dim_id == col_dims[0]:
            new_col = row_dims[0]
        elif dim_id in page_dims:
            new_col = col_dims[0] if col_dims else ""
        else:
            return
        if not new_col:
            return
        result = self._session.execute(
            "set_view_axes",
            view_id=self._view_id,
            row_dimension_id=new_row,
            col_dimension_id=new_col,
        )
        if result.status.name != "SUCCESS":
            return
        self._rebuild_bars()

    @QtCore.Slot(str, object)
    def on_format_changed(self, format_type: str, value: object) -> None:
        """Handle format changes from the format toolbox."""
        if isinstance(self.table, MatrixGrid):
            self.table.apply_format_to_selection(format_type, value)
            self.presentation_changed.emit()
            # Mark workspace as dirty after format changes
            win = self.window()
            if hasattr(win, "_mark_dirty"):
                try:
                    win._mark_dirty(True)
                except Exception:
                    pass

    def reload(self, view_id: str | None = None, *, invalidate_tiles: bool | str = False) -> None:
        with self._span("ViewTab.reload"):
            if view_id is not None:
                self._view_id = view_id
            # Save scroll position before reload so we can restore it if requested
            saved_h = saved_v = None
            if isinstance(self.table, MatrixGrid):
                saved_h = self.table.horizontalScrollBar().value()
                saved_v = self.table.verticalScrollBar().value()
            # _rebuild_bars() already calls table.reload(); do not reload twice.
            self._rebuild_bars(invalidate_tiles=invalidate_tiles)
            # Restore scroll position if preservation was requested
            if isinstance(self.table, MatrixGrid) and getattr(self.table, '_preserve_scroll', False):
                self.table.horizontalScrollBar().setValue(saved_h)
                self.table.verticalScrollBar().setValue(saved_v)
