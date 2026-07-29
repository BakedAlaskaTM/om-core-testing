from __future__ import annotations

import json
import logging
import os

from PySide6 import QtCore, QtGui, QtWidgets

from lib_gui.workspace_read_model import WorkspaceReadModel

DEBUG_GUI = os.environ.get("DEBUG_GUI", "false").lower() in ("true", "1", "yes")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Drag-and-drop MIME type
# ---------------------------------------------------------------------------
_MIME = "application/x-openm-dim"   # payload: JSON {"dim_id": "...", "source_zone": "row"|"col"|"page"}

# ---------------------------------------------------------------------------
# Stylesheets
# ---------------------------------------------------------------------------
_AXIS_CHIP_QSS = """
AxisChip, HeaderChip {
    background: #e4e8ef;
    border: 1px solid #b8c2d0;
    border-radius: 3px;
    padding: 1px 7px 1px 2px;
    font-size: 11px;
    font-weight: 600;
    color: #1e2d40;
    min-height: 20px;
    max-height: 24px;
}
AxisChip:hover, HeaderChip:hover  { background: #d0d8e8; border-color: #7890b8; }
AxisChip:pressed, HeaderChip:pressed { background: #b8c8dc; border-color: #4870a8; }
"""

_PAGE_CHIP_QSS = """
PageChip {
    background: #e4e8ef;
    border: 1px solid #b8c2d0;
    border-radius: 3px;
    padding: 1px 14px 1px 2px;
    font-size: 11px;
    font-weight: 600;
    color: #1e2d40;
    min-height: 20px;
    max-height: 24px;
}
PageChip:hover  { background: #d0d8e8; border-color: #7890b8; }
PageChip:pressed { background: #b8c8dc; border-color: #4870a8; }

PageChip::menu-indicator {
    image: none;
    width: 0px;
    height: 0px;
}
"""

_PLUS_QSS = """
QPushButton {
    background: transparent;
    border: 1px solid #b8c2d0;
    border-radius: 3px;
    color: #4870a8;
    font-size: 14px;
    font-weight: 700;
    padding: 0px 3px;
    min-width: 20px; max-width: 20px;
    min-height: 20px; max-height: 24px;
}
QPushButton:hover  { background: #d0d8e8; border-color: #7890b8; }
QPushButton:pressed { background: #b8c8dc; }
"""

_ZONE_HIGHLIGHT_QSS = "background: #d0e4f8; border: 1px dashed #4870a8; border-radius: 3px;"


# ---------------------------------------------------------------------------
# Drag-capable base mixin
# ---------------------------------------------------------------------------
class _DraggableChipMixin:
    """Adds drag initiation on mouse-press-move.  Mixed into AxisChip / PageChip.

    We block super() on press entirely to prevent InstantPopup/DelayedPopup from
    stealing the event.  On release without drag we manually show the menu.
    """

    _zone: str = ""   # "axis" or "page" — set by subclass

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._drag_start: QtCore.QPoint = event.pos()
            self._did_drag: bool = False
            # Do NOT call super() — that would fire InstantPopup immediately.
            event.accept()
        else:
            super().mousePressEvent(event)  # type: ignore[misc]

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        if not (event.buttons() & QtCore.Qt.MouseButton.LeftButton):
            return
        dist = (event.pos() - self._drag_start).manhattanLength()
        if dist < QtWidgets.QApplication.startDragDistance():
            return

        self._did_drag = True
        payload = json.dumps({"dim_id": self._dim_id, "source_zone": self._zone})  # type: ignore[attr-defined]
        mime = QtCore.QMimeData()
        mime.setData(_MIME, payload.encode())

        drag = QtGui.QDrag(self)  # type: ignore[arg-type]
        drag.setMimeData(mime)
        pm = self.grab()  # type: ignore[attr-defined]
        drag.setPixmap(pm)
        drag.setHotSpot(event.pos())
        drag.exec(QtCore.Qt.DropAction.MoveAction)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == QtCore.Qt.MouseButton.LeftButton and not getattr(self, "_did_drag", False):
            # Clean click — show menu if one is set, otherwise emit clicked
            menu = self.menu()  # type: ignore[attr-defined]
            if menu is not None:
                # Use the toolbutton's built-in menu popup so Qt handles focus/positioning.
                # (Avoid exec(), which can interfere with drag recognition on some platforms.)
                self.showMenu()  # type: ignore[attr-defined]
            else:
                self.clicked.emit()  # type: ignore[attr-defined]
        else:
            super().mouseReleaseEvent(event)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AxisChip  –  bottom-left, no dropdown arrow
# ---------------------------------------------------------------------------
class AxisChip(_DraggableChipMixin, QtWidgets.QToolButton):
    _zone = "row"

    def __init__(
        self,
        dim_id: str,
        dim_name: str,
        parent: QtWidgets.QWidget | None = None,
        is_seq: bool = False,
    ) -> None:
        super().__init__(parent)
        self._dim_id = dim_id
        self._dim_name = dim_name
        self._is_seq = is_seq
        self.setStyleSheet(_AXIS_CHIP_QSS)
        self.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.OpenHandCursor))
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setToolTip("Drag to top-right to make this a page (slice) axis")
        grip = "⣿" #"⠿"
        suffix = " 🔗" if self._is_seq else ""
        self.setText(f"{grip}  {dim_name}{suffix}")

    @property
    def dim_id(self) -> str:
        return self._dim_id


# ---------------------------------------------------------------------------
# HeaderChip  –  used for column placeholder (dimension name only)
# ---------------------------------------------------------------------------
class HeaderChip(_DraggableChipMixin, QtWidgets.QToolButton):
    _zone = "col"

    def __init__(
        self,
        dim_id: str,
        dim_name: str,
        parent: QtWidgets.QWidget | None = None,
        is_seq: bool = False,
    ) -> None:
        super().__init__(parent)
        self._dim_id = dim_id
        self._dim_name = dim_name
        self._is_seq = is_seq
        self.setStyleSheet(_AXIS_CHIP_QSS)
        self.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.OpenHandCursor))
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setToolTip("Drag to move/reorder")
        grip = "⣿" #"⠿"
        suffix = " 🔗" if self._is_seq else ""
        self.setText(f"{grip}  {dim_name}{suffix}")

    @property
    def dim_id(self) -> str:
        return self._dim_id


# ---------------------------------------------------------------------------
# PageChip  –  top-right, dropdown arrow + item picker
# ---------------------------------------------------------------------------
class PageChip(_DraggableChipMixin, QtWidgets.QToolButton):
    _zone = "page"

    item_selected = QtCore.Signal(str, str)  # (dim_id, item_id)

    def __init__(
        self,
        dim_id: str,
        dim_name: str,
        items: list[tuple[str, str]],
        current_id: str,
        parent: QtWidgets.QWidget | None = None,
        is_seq: bool = False,
    ) -> None:
        super().__init__(parent)
        self._dim_id = dim_id
        self._dim_name = dim_name
        self._items = items
        self._current_id = current_id
        self._is_seq = is_seq

        self.setStyleSheet(_PAGE_CHIP_QSS)
        self.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        # DelayedPopup prevents the menu from stealing the press/move gesture,
        # making drag-to-rearrange reliable.
        self.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.DelayedPopup)
        self.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.OpenHandCursor))
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setToolTip("Drag to bottom to make this a row/column axis")
        self._rebuild_menu()
        self._update_label()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # type: ignore[override]
        """Draw the chip normally, then add a solid downward triangle on the right.

        The native macOS QToolButton menu indicator is a thin chevron; we override
        it (hidden via QSS) with a filled triangle.
        """
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

    @property
    def dim_id(self) -> str:
        return self._dim_id

    def set_current(self, item_id: str) -> None:
        self._current_id = item_id
        self._update_label()
        self._rebuild_menu()

    def _update_label(self) -> None:
        name = next((n for iid, n in self._items if iid == self._current_id), "\u2014")
        if DEBUG_GUI and self._dim_id == "@":
            print(f"[DEBUG PageChip._update_label] current_id={self._current_id[:15] if self._current_id else None}, name='{name}'")
        grip = "⣿"  # U+28FF braille pattern
        suffix = " \ud83d\udd17" if self._is_seq else ""
        self.setText(f"{grip}  {self._dim_name}: {name}{suffix}")

    def _rebuild_menu(self) -> None:
        menu = QtWidgets.QMenu(self)
        if DEBUG_GUI and self._dim_id == "@":
            print(f"[DEBUG PageChip._rebuild_menu] @ dim items: {[(i[:8] + '...' if len(i) > 8 else i, n) for i, n in self._items[:3]]}...")
        for item_id, item_name in self._items:
            act = menu.addAction(item_name)
            act.setCheckable(True)
            act.setChecked(item_id == self._current_id)
            act.triggered.connect(lambda checked=False, iid=item_id: self._on_pick(iid))
        self.setMenu(menu)

    def _on_pick(self, item_id: str) -> None:
        self._current_id = item_id
        self._update_label()
        self._rebuild_menu()
        if DEBUG_GUI:
            print(f"[DEBUG PageChip._on_pick] dim={self._dim_id[:8]}, item={item_id[:8]}")
        self.item_selected.emit(self._dim_id, item_id)


# ---------------------------------------------------------------------------
# PivotBar  –  bottom strip  (axis chips + [+] buttons)
# ---------------------------------------------------------------------------
class PivotBar(QtWidgets.QWidget):
    """Bottom strip: row dimension chip + [+] only."""

    selection_changed = QtCore.Signal()
    add_item_requested = QtCore.Signal(str)   # dim_id (kept for compat)
    add_dim_requested = QtCore.Signal(str)    # axis label: "row"
    # Emitted when a chip is dropped here (move/reorder into the row placeholder)
    move_dim = QtCore.Signal(str, str, int)   # (dim_id, source_zone, insert_index)

    def __init__(self, *, view_id: str, parent: QtWidgets.QWidget | None = None, workspace_read_model=None, session: object = None) -> None:
        super().__init__(parent)
        self._view_id = view_id
        self._workspace_read_model = workspace_read_model
        self._session = session
        self._chips: list[AxisChip] = []
        self._plus_btns: list[QtWidgets.QPushButton] = []

        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QtGui.QColor(0xee, 0xf0, 0xf4))
        self.setPalette(pal)
        self.setFixedHeight(32)
        self.setAcceptDrops(True)

        self._layout = QtWidgets.QHBoxLayout(self)
        self._layout.setContentsMargins(8, 4, 8, 4)
        self._layout.setSpacing(4)
        self._layout.addStretch(1)

        self.rebuild(view_id)

    # -- rebuild --
    def rebuild(self, view_id: str) -> None:
        self._view_id = view_id
        if self._workspace_read_model is None and hasattr(self._session, 'execute'):
            self._workspace_read_model = WorkspaceReadModel(self._session)
        if self._workspace_read_model is None:
            return

        for w in list(self._chips) + list(self._plus_btns):
            self._layout.removeWidget(w)
            w.deleteLater()
        self._chips.clear()
        self._plus_btns.clear()

        view = self._workspace_read_model.get_view(self._view_id)
        if view is None:
            plus = QtWidgets.QPushButton("+", self)
            plus.setStyleSheet(_PLUS_QSS)
            plus.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
            plus.setToolTip("Add dimension")
            plus.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
            plus.clicked.connect(lambda checked=False: self.add_dim_requested.emit("row"))
            self._layout.insertWidget(0, plus)
            self._plus_btns.append(plus)
            return
        row_dim_ids = list(view.get("row_dim_ids", []) or [])

        insert_at = 0
        for dim_id in row_dim_ids:
            dim = self._workspace_read_model.get_dimension(dim_id)
            if dim is None:
                continue
            chip = AxisChip(dim_id, dim.get("name", ""), self, is_seq=(dim.get("dim_type", "set") == "seq"))
            chip._zone = "row"
            self._layout.insertWidget(insert_at, chip)
            insert_at += 1
            self._chips.append(chip)

        plus = QtWidgets.QPushButton("+", self)
        plus.setStyleSheet(_PLUS_QSS)
        plus.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
        plus.setToolTip("Add dimension")
        plus.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        plus.clicked.connect(lambda checked=False: self.add_dim_requested.emit("row"))
        self._layout.insertWidget(insert_at, plus)
        self._plus_btns.append(plus)

    # -- drop target --
    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(_MIME):
            data = json.loads(event.mimeData().data(_MIME).toStdString())
            if data.get("source_zone") in ("row", "col", "page"):
                event.acceptProposedAction()
                self.setStyleSheet(_ZONE_HIGHLIGHT_QSS)
                return
        event.ignore()

    def dragLeaveEvent(self, event: QtGui.QDragLeaveEvent) -> None:
        self.setStyleSheet("")

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        self.setStyleSheet("")
        data = json.loads(event.mimeData().data(_MIME).toStdString())
        dim_id = data.get("dim_id", "")
        source_zone = data.get("source_zone", "")
        if dim_id:
            # insert before the + button (which is the last widget)
            insert_index = max(0, len(self._chips))
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()  # type: ignore[attr-defined]
            for i, chip in enumerate(self._chips):
                if pos.x() < chip.x() + chip.width() // 2:
                    insert_index = i
                    break
            self.move_dim.emit(dim_id, source_zone, insert_index)
        event.acceptProposedAction()



# ---------------------------------------------------------------------------
# _ChipZoneBase  –  shared logic for PageAxisBar and TopLeftChipBar
# ---------------------------------------------------------------------------
class _ChipZoneBase(QtWidgets.QWidget):
    """Base for top-right and top-left chip zones."""

    selection_changed = QtCore.Signal()
    move_dim = QtCore.Signal(str, str, int)  # (dim_id, source_zone, insert_index)
    # axis label is the zone this bar represents: "page" or "col".
    add_dim_requested = QtCore.Signal(str)

    # Subclasses set these:
    _layout_direction: str = "vertical"    # "vertical" | "horizontal"
    _accept_from: list[str]                # source_zone values we accept drops from
    _own_zone: str = ""                    # zone label for chips we create

    def __init__(self, *, view_id: str, parent: QtWidgets.QWidget | None = None, workspace_read_model: WorkspaceReadModel | None = None, session: object = None) -> None:
        super().__init__(parent)
        self._view_id = view_id
        self._workspace_read_model = workspace_read_model
        self._session = session
        self._chips: list[PageChip] = []

        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QtGui.QColor(0xf4, 0xf5, 0xf8))
        self.setPalette(pal)
        self.setAcceptDrops(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        # Make the bar expand vertically to fill the available space
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(120)

        if self._layout_direction == "horizontal":
            self._layout: QtWidgets.QBoxLayout = QtWidgets.QHBoxLayout(self)
        else:
            self._layout = QtWidgets.QVBoxLayout(self)
            # Align content to the top for vertical layout
            self._layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(4)

        self._plus_btn: QtWidgets.QPushButton | None = None
        self._base_qss: str = ""  # saved for drag highlight restore
        self.rebuild(view_id)

    def _dim_ids_for_zone(self, view_id: str) -> list[str]:
        raise NotImplementedError

    def rebuild(self, view_id: str, dim_ids: list[str] | None = None) -> None:
        self._view_id = view_id
        if self._workspace_read_model is None:
            return

        # Remove + button from layout temporarily (will re-add at end)
        if self._plus_btn is not None:
            self._layout.removeWidget(self._plus_btn)

        for chip in self._chips:
            self._layout.removeWidget(chip)
            chip.deleteLater()
        self._chips.clear()

        if dim_ids is None:
            dim_ids = self._dim_ids_for_zone(view_id)
        self._update_placeholder(bool(dim_ids))

        for dim_id in dim_ids:
            dim_dto = self._workspace_read_model.get_dimension(dim_id)
            if dim_dto is None:
                continue
            is_seq = dim_dto.get("dim_type", "set") == "seq"
            if self._own_zone == "page":
                items = [(it.get("id"), it.get("name")) for it in dim_dto.get("items", [])]
                current_id = self._workspace_read_model.page_selection(view_id, dim_id)
                if current_id is None or not any(iid == current_id for iid, _ in items):
                    if items:
                        current_id = items[0][0]
                chip = PageChip(dim_id, dim_dto.get("name", ""), items, current_id, self, is_seq=is_seq)
                chip._zone = self._own_zone   # override zone so drag knows origin
                chip.item_selected.connect(self._on_chip_selected)
            else:
                chip = HeaderChip(dim_id, dim_dto.get("name", ""), self, is_seq=is_seq)
                chip._zone = self._own_zone
            self._layout.addWidget(chip)
            self._chips.append(chip)

        # Create + button once; always place at end
        if self._plus_btn is None:
            self._plus_btn = QtWidgets.QPushButton("+", self)
            self._plus_btn.setStyleSheet(_PLUS_QSS)
            self._plus_btn.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
            self._plus_btn.setToolTip("Add dimension")
            self._plus_btn.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            self._plus_btn.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed
            )
            # Emit which zone requested the new dimension (e.g. "col" or "page").
            self._plus_btn.clicked.connect(
                lambda checked=False, zone=self._own_zone: self.add_dim_requested.emit(zone)
            )
        self._layout.addWidget(self._plus_btn)
        self._plus_btn.show()

        self.adjustSize()

    def _update_placeholder(self, has_chips: bool) -> None:
        """Subclasses may override. Default: always visible."""
        self.setVisible(True)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(_MIME):
            data = json.loads(event.mimeData().data(_MIME).toStdString())
            if data.get("source_zone") in self._accept_from:
                event.acceptProposedAction()
                self._base_qss = self.styleSheet()
                self.setStyleSheet(_ZONE_HIGHLIGHT_QSS)
                return
        event.ignore()

    def dragLeaveEvent(self, event: QtGui.QDragLeaveEvent) -> None:
        self.setStyleSheet(self._base_qss)

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        self.setStyleSheet(self._base_qss)
        data = json.loads(event.mimeData().data(_MIME).toStdString())
        dim_id = data.get("dim_id", "")
        source_zone = data.get("source_zone", "")
        if dim_id:
            insert_index = len(self._chips)
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()  # type: ignore[attr-defined]
            if self._layout_direction == "vertical":
                for i, chip in enumerate(self._chips):
                    if pos.y() < chip.y() + chip.height() // 2:
                        insert_index = i
                        break
            else:
                for i, chip in enumerate(self._chips):
                    if pos.x() < chip.x() + chip.width() // 2:
                        insert_index = i
                        break
            self.move_dim.emit(dim_id, source_zone, insert_index)
        event.acceptProposedAction()

    @QtCore.Slot(str, str)
    def _on_chip_selected(self, dim_id: str, item_id: str) -> None:
        print(f"[DEBUG _ChipZoneBase._on_chip_selected] view={self._view_id[:8]}, dim={dim_id[:8]}, item={item_id[:8]}")
        import time
        logger.info("[pivot_bar] chip selected view=%s dim=%s item=%s", self._view_id[:8], dim_id[:8], item_id[:8])
        if self._session is None:
            raise RuntimeError("No session available for set_page_item_id")
        t0 = time.perf_counter()
        self._session.execute("set_page_item_id", view_id=self._view_id, dim_id=dim_id, item_id=item_id)
        logger.info(
            "[pivot_bar] set_page_item_id done view=%s duration=%.3f ms",
            self._view_id[:8], (time.perf_counter() - t0) * 1000,
        )
        # Page selections only change the visible slice; they do not dirty
        # computed values.  With the read-only tile fast path in place, there
        # is no need to block the GUI for a full recalc here.
        self.selection_changed.emit()
        logger.info("[pivot_bar] chip selection done view=%s", self._view_id[:8])

    def _reload_grid(self) -> None:
        """Trigger grid reload - call this after structural changes."""
        # Find parent ViewTab and trigger rebuild
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, '_rebuild_bars'):
                parent._rebuild_bars()
                break
            parent = parent.parent()


# ---------------------------------------------------------------------------
# PageAxisBar  –  top-right column  (col dim + stacked page dims)
# ---------------------------------------------------------------------------
class PageAxisBar(_ChipZoneBase):
    """Top-right: col dimension chip + any extra page dims stacked below it.
    Accepts drops from axis (bottom) zone and from top-left zone.
    """

    _layout_direction = "vertical"
    _accept_from = ["row", "page", "col"]
    _own_zone = "col"

    def _dim_ids_for_zone(self, view_id: str) -> list[str]:
        """Default: col dim first, then remaining non-row dims."""
        view = self._workspace_read_model.get_view(view_id)
        if view is None:
            return []
        cube = self._workspace_read_model.get_cube(view.get("cube_id", ""))
        if cube is None:
            return []
        row_set = set(view.get("row_dim_ids", []) or [])
        col_dim_ids = list(view.get("col_dim_ids", []) or [])
        page_dim_ids = list(view.get("page_dim_ids", []) or [])
        cube_dim_ids = list(cube.get("dimension_ids", []) or [])
        # Fallback for legacy views: treat all non-row dims as col/page stack
        if not col_dim_ids and not page_dim_ids:
            return [d for d in cube_dim_ids if d not in row_set]
        # Keep col dims first (in their configured order), then page dims.
        ordered: list[str] = []
        for did in col_dim_ids + page_dim_ids:
            if did in cube_dim_ids and did not in row_set and did not in ordered:
                ordered.append(did)
        return ordered


# ---------------------------------------------------------------------------
# TopLeftChipBar  –  top-left strip  (extra page dims beyond col)
# ---------------------------------------------------------------------------
_TOPLEFT_PLACEHOLDER_QSS = """
    QLabel {
        color: #a0a8b8;
        font-size: 10px;
        font-style: italic;
        padding: 2px 6px;
    }
"""

class TopLeftChipBar(_ChipZoneBase):
    """Top-left: extra page-axis dims pinned here. Horizontal layout.
    Always visible as a drop target; shows placeholder when empty.
    Accepts drops from top-right zone.
    """

    _layout_direction = "horizontal"
    _accept_from = ["row", "page", "col"]
    _own_zone = "page"

    def __init__(self, *, view_id: str, parent=None, workspace_read_model=None, session: object = None) -> None:
        self._placeholder: QtWidgets.QLabel | None = None
        super().__init__(view_id=view_id, parent=parent, workspace_read_model=workspace_read_model, session=session)
        self.setFixedHeight(32)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.setStyleSheet(
            "background: #eef0f6; border-bottom: 1px solid #c8ccd8;"
        )
        # Ensure content is left-aligned
        self._layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)

    def rebuild(self, view_id: str, dim_ids: list[str] | None = None) -> None:
        """Override to position + button immediately after placeholder."""
        self._view_id = view_id
        if self._workspace_read_model is None:
            return

        # Remove + button from layout temporarily (will re-add in correct position)
        if self._plus_btn is not None:
            self._layout.removeWidget(self._plus_btn)

        for chip in self._chips:
            self._layout.removeWidget(chip)
            chip.deleteLater()
        self._chips.clear()

        if dim_ids is None:
            dim_ids = self._dim_ids_for_zone(view_id)
        self._update_placeholder(bool(dim_ids))

        # Ensure layout aligns content to the left
        self._layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)

        for dim_id in dim_ids:
            dim_dto = self._workspace_read_model.get_dimension(dim_id)
            if dim_dto is None:
                continue
            is_seq = dim_dto.get("dim_type", "set") == "seq"
            if self._own_zone == "page":
                items = [(it.get("id"), it.get("name")) for it in dim_dto.get("items", [])]
                current_id = self._workspace_read_model.page_selection(view_id, dim_id)
                if current_id is None or not any(iid == current_id for iid, _ in items):
                    if items:
                        current_id = items[0][0]
                chip = PageChip(dim_id, dim_dto.get("name", ""), items, current_id, self, is_seq=is_seq)
                chip._zone = self._own_zone
                chip.item_selected.connect(self._on_chip_selected)
            else:
                chip = HeaderChip(dim_id, dim_dto.get("name", ""), self, is_seq=is_seq)
                chip._zone = self._own_zone
            self._layout.addWidget(chip)
            self._chips.append(chip)

        # Create + button once; place immediately after chips (on the left)
        if self._plus_btn is None:
            self._plus_btn = QtWidgets.QPushButton("+", self)
            self._plus_btn.setStyleSheet(_PLUS_QSS)
            self._plus_btn.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
            self._plus_btn.setToolTip("Add dimension")
            self._plus_btn.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed
            )
            # Top-left bar always represents the "page" axis.
            self._plus_btn.clicked.connect(lambda checked=False: self.add_dim_requested.emit("page"))
        # Add + button right after chips
        self._layout.addWidget(self._plus_btn)
        self._plus_btn.show()

        self.adjustSize()

    def _update_placeholder(self, has_chips: bool) -> None:
        self.setVisible(True)  # always visible as drop target
        if not has_chips:
            if self._placeholder is None:
                self._placeholder = QtWidgets.QLabel("drag dims here", self)
                self._placeholder.setStyleSheet(_TOPLEFT_PLACEHOLDER_QSS)
                self._layout.addWidget(self._placeholder)
        else:
            if self._placeholder is not None:
                self._layout.removeWidget(self._placeholder)
                self._placeholder.deleteLater()
                self._placeholder = None

    def _dim_ids_for_zone(self, view_id: str) -> list[str]:
        return []  # always driven by ViewTab via explicit dim_ids arg
