from __future__ import annotations

import math
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Set, Tuple

import shiboken6

from PySide6 import QtCore, QtGui, QtWidgets

from lib_utils.config import gui as gui_config

_FLOW_DEBUG = bool(int(os.environ.get("OPENM_DEBUG_FLOW_GRAPH", "0"))) or gui_config("debug", "debug_flow_graph", False)
_FLOW_PANEL_DEBUG = bool(int(os.environ.get("OPENM_DEBUG_CALC_FLOW", "0"))) or gui_config("debug", "debug_calc_flow", False)
KeyType = Tuple[str, Tuple[str, ...]]


@dataclass
class _EdgeGraphics:
    lane: QtWidgets.QGraphicsPathItem
    stroke: QtWidgets.QGraphicsPathItem
    head: QtWidgets.QGraphicsPolygonItem
    base_color: QtGui.QColor
    highlight_color: QtGui.QColor

    def is_valid(self) -> bool:
        return all(
            shiboken6.isValid(item)
            for item in (self.lane, self.stroke, self.head)
        )

    def set_state(self, state: str) -> None:
        color = self.highlight_color if state == "primary" else self.base_color
        if state == "primary":
            lane_width, lane_alpha = 11.0, 215
            stroke_width, stroke_alpha = 3.4, 255
            head_alpha = 255
        elif state == "secondary":
            lane_width, lane_alpha = 8.0, 165
            stroke_width, stroke_alpha = 2.5, 230
            head_alpha = 230
        elif state == "dim":
            lane_width, lane_alpha = 5.5, 45
            stroke_width, stroke_alpha = 1.4, 85
            head_alpha = 85
        else:  # normal / default
            lane_width, lane_alpha = 6.3, 100
            stroke_width, stroke_alpha = 2.1, 180
            head_alpha = 180

        lane_pen = QtGui.QPen(color)
        lane_color = QtGui.QColor(color)
        lane_color.setAlpha(lane_alpha)
        lane_pen.setColor(lane_color)
        lane_pen.setWidthF(lane_width)
        lane_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        lane_pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        self.lane.setPen(lane_pen)

        stroke_pen = QtGui.QPen(color)
        stroke_color = QtGui.QColor(color)
        stroke_color.setAlpha(stroke_alpha)
        stroke_pen.setColor(stroke_color)
        stroke_pen.setWidthF(stroke_width)
        stroke_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        stroke_pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        self.stroke.setPen(stroke_pen)

        head_color = QtGui.QColor(color)
        head_color.setAlpha(head_alpha)
        head_pen = QtGui.QPen(head_color)
        head_pen.setWidthF(1.0)
        self.head.setPen(head_pen)
        self.head.setBrush(QtGui.QBrush(head_color))


class _GraphNodeItem(QtWidgets.QGraphicsPathItem):
    def __init__(
        self,
        size_rect: QtCore.QRectF,
        brush_color: QtGui.QColor,
        key: Tuple[str, Tuple[str, ...]],
        owner: "CalculationFlowPanel",
    ) -> None:
        path = QtGui.QPainterPath()
        path.addRoundedRect(size_rect, 10.0, 10.0)
        super().__init__(path)
        self._owner = owner
        self._key = key
        self._base_pen = QtGui.QPen(QtGui.QColor("#7a8599"))
        self._base_brush = QtGui.QBrush(brush_color)
        lighter = QtGui.QColor(brush_color)
        lighter = lighter.lighter(108)
        self._primary_brush = QtGui.QBrush(lighter)
        secondary = QtGui.QColor(brush_color)
        secondary = secondary.lighter(120)
        self._secondary_brush = QtGui.QBrush(secondary)
        self._dim_brush = QtGui.QBrush(QtGui.QColor("#e9ecf3"))
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.set_state("normal")

    def set_state(self, state: str) -> None:
        pen = QtGui.QPen(self._base_pen)
        opacity = 1.0
        if state == "primary":
            brush = self._primary_brush
            pen.setWidthF(2.2)
        elif state == "secondary":
            brush = self._secondary_brush
            pen.setWidthF(1.6)
        elif state == "dim":
            brush = self._dim_brush
            opacity = 0.55
            pen.setWidthF(1.0)
        else:  # normal
            brush = self._base_brush
            pen.setWidthF(1.4)
        self.setBrush(brush)
        self.setPen(pen)
        self.setOpacity(opacity)

    def hoverEnterEvent(self, event: QtWidgets.QGraphicsSceneHoverEvent) -> None:  # type: ignore[override]
        self._owner._handle_node_hover(self._key)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: QtWidgets.QGraphicsSceneHoverEvent) -> None:  # type: ignore[override]
        self._owner._handle_node_hover(None)
        super().hoverLeaveEvent(event)

class _GraphView(QtWidgets.QGraphicsView):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._zoom_step = 1.18
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            for it in self.items(event.pos()):
                if isinstance(it, _GraphNodeItem):
                    print(f"[DEBUG flow] _GraphView clicked node: {it._key}")
                    it.setSelected(True)
                    cube_id, addr = it._key
                    it._owner.navigate_requested.emit(cube_id, addr)
                    return
            print("[DEBUG flow] _GraphView clicked empty space, starting pan")
        super().mousePressEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # type: ignore[override]
        if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta == 0:
                event.accept()
                return
            factor = self._zoom_step if delta > 0 else 1 / self._zoom_step
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)


class CalculationFlowPanel(QtWidgets.QWidget):
    """List-style browser for calculation flow of the focused cell."""

    navigate_requested = QtCore.Signal(str, tuple)

    def __init__(
        self,
        *,
        session=None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._active_cube_id: str | None = None
        self._focus_cube_id: str | None = None
        self._focus_addr: tuple[str, ...] | None = None

        self._title = QtWidgets.QLabel("Calculation Flow", self)
        font = self._title.font()
        font.setBold(True)
        self._title.setFont(font)

        self._depth = QtWidgets.QSpinBox(self)
        max_depth = gui_config("panels", "calculation_flow_max_depth", 10)
        default_depth = gui_config("panels", "calculation_flow_default_depth", 2)
        self._depth.setRange(1, max_depth)
        self._depth.setValue(default_depth)
        self._depth.setPrefix("Depth ")
        self._depth.valueChanged.connect(self.rebuild)

        self._view_mode = QtWidgets.QComboBox(self)
        self._view_mode.addItem("List")
        self._view_mode.addItem("Graph")
        self._view_mode.currentIndexChanged.connect(self._on_view_mode_changed)

        self._btn_refresh = QtWidgets.QToolButton(self)
        self._btn_refresh.setText("Refresh")
        self._btn_refresh.clicked.connect(self.rebuild)

        controls = QtWidgets.QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(self._title)
        controls.addStretch(1)
        controls.addWidget(self._view_mode)
        controls.addWidget(self._depth)
        controls.addWidget(self._btn_refresh)

        self._list = QtWidgets.QListWidget(self)
        self._list.setUniformItemSizes(False)
        self._list.itemActivated.connect(self._on_item_activated)

        self._graph_scene = QtWidgets.QGraphicsScene(self)
        self._graph_view = _GraphView(self)
        self._graph_view.setScene(self._graph_scene)
        self._graph_view.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        self._graph_view.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        self._graph_view.setMouseTracking(True)
        self._graph_node_items: Dict[KeyType, _GraphNodeItem] = {}
        self._graph_edges: list[tuple[KeyType, KeyType, _EdgeGraphics]] = []
        self._graph_adjacent: Dict[KeyType, Set[KeyType]] = defaultdict(set)
        self._suspend_hover_updates = False

        self._stack = QtWidgets.QStackedWidget(self)
        self._stack.addWidget(self._list)
        self._stack.addWidget(self._graph_view)
        self._stack.setCurrentIndex(0)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(controls)
        layout.addWidget(self._stack, 1)

    @QtCore.Slot(int)
    def _on_view_mode_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(0 if index <= 0 else 1)

    def set_active_cube(self, cube_id: str | None) -> None:
        if cube_id != self._active_cube_id and self._focus_cube_id is not None and self._focus_cube_id != cube_id:
            self._focus_cube_id = None
            self._focus_addr = None
        self._active_cube_id = cube_id
        self.rebuild()

    def set_focus_cell(self, cube_id: str | None, addr: tuple[str, ...] | None) -> None:
        self._focus_cube_id = cube_id
        self._focus_addr = addr
        self.rebuild()

    def rebuild(self) -> None:
        self._list.clear()
        self._graph_scene.clear()

        if self._session is not None and self._focus_cube_id is not None:
            try:
                # Use gui_view_model cache if available to avoid cube_list RPC
                _gvm = getattr(self._session, "_gui_view_model", None)
                if _gvm is not None:
                    snap = _gvm.get_cube_snapshot(self._focus_cube_id)
                    cube_exists = snap is not None
                else:
                    cube_list = self._session.query("cube_list") or {}
                    cube_ids = {c["id"] for c in cube_list.get("cubes", [])}
                    cube_exists = self._focus_cube_id in cube_ids
                if not cube_exists:
                    self._focus_cube_id = None
                    self._focus_addr = None
            except Exception:
                pass

        cube_id = self._focus_cube_id or self._active_cube_id
        addr = self._focus_addr
        if not cube_id or addr is None:
            item = QtWidgets.QListWidgetItem("Select a data cell to inspect calculation flow.")
            item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            self._list.addItem(item)
            self._graph_scene.addText("Select a data cell to inspect calculation flow.")
            return

        try:
            if self._session is None:
                raise RuntimeError("No session available for diagnostics_calculation_flow")
            flow = self._session.query(
                "diagnostics_calculation_flow",
                cube_id=cube_id,
                addr=addr,
                max_depth=int(self._depth.value()),
            )
            if flow is None:
                flow = []
        except Exception as exc:
            item = QtWidgets.QListWidgetItem(f"Trace unavailable: {exc}")
            item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            self._list.addItem(item)
            self._graph_scene.addText(f"Trace unavailable: {exc}")
            return

        if not flow:
            item = QtWidgets.QListWidgetItem("No flow information available.")
            item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            self._list.addItem(item)
            self._graph_scene.addText("No flow information available.")
            return

        if _FLOW_PANEL_DEBUG:
            print(
                "FLOW_PANEL: rebuild",
                f"cube={cube_id}",
                f"addr={addr}",
                f"rows={len(flow)}",
                f"labels={[row.get('addr_label') for row in flow]!r}",
            )

        try:
            root_row = next((row for row in flow if int(row.get("depth", 0)) == 0), flow[0])
        except (IndexError, StopIteration):
            root_row = None
        if root_row and root_row.get("dependents_truncated"):
            limit = root_row.get("dependents_limit", "?")
            warning_color = gui_config("appearance", "truncation_warning_color", "#d32f2f")
            self._title.setText(f'<span style="color: black;">Calculation Flow</span> <span style="color: {warning_color};">— WARNING: TRUNCATED TO {limit}</span>')
            self._title.setTextFormat(QtCore.Qt.TextFormat.RichText)
        else:
            self._title.setText("Calculation Flow")
            self._title.setTextFormat(QtCore.Qt.TextFormat.PlainText)

        for row in flow:
            depth = int(row.get("depth", 0))
            indent = "  " * depth
            source = str(row.get("source", ""))
            addr_label = str(row.get("addr_label", ""))
            expr = row.get("expression")

            head_text = f"{indent}• {addr_label}"
            if source:
                head_text = f"{head_text}  [{source}]"

            head_item = QtWidgets.QListWidgetItem(head_text)
            head_item.setData(
                QtCore.Qt.ItemDataRole.UserRole,
                (str(row.get("cube_id", cube_id)), tuple(row.get("addr", addr))),
            )
            self._list.addItem(head_item)

            if isinstance(expr, str) and expr:
                expr_item = QtWidgets.QListWidgetItem(f"{indent}    = {expr}")
                expr_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                expr_item.setForeground(QtGui.QColor("#4d5b73"))
                self._list.addItem(expr_item)

            def _add_section(
                title: str,
                labels: list[str],
                targets: list[dict[str, object]],
                color: str,
                bullet: str,
            ) -> None:
                if not labels:
                    return
                header = QtWidgets.QListWidgetItem(f"{indent}    {title}")
                header.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                header.setForeground(QtGui.QColor(color))
                header_font = header.font()
                header_font.setItalic(True)
                header.setFont(header_font)
                self._list.addItem(header)
                for idx, label in enumerate(labels):
                    entry_item = QtWidgets.QListWidgetItem(f"{indent}      {bullet} {label}")
                    entry_flags = QtCore.Qt.ItemFlag.ItemIsEnabled
                    target_data: dict[str, object] | None = None
                    if idx < len(targets):
                        maybe_target = targets[idx]
                        if isinstance(maybe_target, dict) and {
                            "cube_id",
                            "addr",
                        } <= set(maybe_target.keys()):
                            target_data = maybe_target
                    if target_data is not None:
                        entry_flags |= QtCore.Qt.ItemFlag.ItemIsSelectable
                        cube_id = target_data.get("cube_id")
                        addr = target_data.get("addr")
                        if isinstance(cube_id, str) and isinstance(addr, (tuple, list)):
                            entry_item.setData(
                                QtCore.Qt.ItemDataRole.UserRole,
                                (cube_id, tuple(addr)),
                            )
                    entry_item.setFlags(entry_flags)
                    entry_item.setForeground(QtGui.QColor(color))
                    self._list.addItem(entry_item)

            precedents = list(row.get("precedents") or [])
            precedent_targets = list(row.get("precedent_targets") or [])
            _add_section(
                "Upstream (precedents)",
                precedents,
                precedent_targets,
                "#2166b5",
                "↳",
            )

            dependents = list(row.get("dependents") or [])
            dependent_targets = list(row.get("dependent_targets") or [])
            _add_section(
                "Downstream (dependents)",
                dependents,
                dependent_targets,
                "#6d4cc2",
                "↰",
            )

            spacer = QtWidgets.QListWidgetItem("")
            spacer.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            spacer.setForeground(QtGui.QColor("#b5bccb"))
            self._list.addItem(spacer)

        self._render_graph(flow)

    def _render_graph(self, flow: list[dict[str, object]]) -> None:
        self._suspend_hover_updates = True
        try:
            self._graph_scene.clear()
            if not flow:
                self._graph_scene.addText("No flow information available.")
                return

            self._graph_node_items.clear()
            self._graph_edges.clear()
            self._graph_adjacent = defaultdict(set)

            node_rows: Dict[KeyType, dict[str, Any]] = {}
            edges: list[tuple[KeyType, KeyType, str]] = []
            root_key: KeyType | None = None
            for row in flow:
                cube_id = row.get("cube_id")
                addr = tuple(row.get("addr") or ())
                if not isinstance(cube_id, str) or not addr:
                    continue
                key = (cube_id, addr)
                node_rows[key] = row
                depth = int(row.get("depth", 0))
                row["_graph_layer"] = -depth
                if depth == 0 and root_key is None:
                    root_key = key
                if _FLOW_DEBUG:
                    print("FLOW_GRAPH: node", key, "depth", depth)

            if not node_rows:
                self._graph_scene.addText("No flow information available.")
                return

            if root_key is None:
                root_key = next(iter(node_rows))
                node_rows[root_key]["_graph_layer"] = 0

            for row in flow:
                cube_id = row.get("cube_id")
                addr = tuple(row.get("addr") or ())
                if not isinstance(cube_id, str) or not addr:
                    continue
                curr_key = (cube_id, addr)
                for target in row.get("precedent_targets") or []:
                    tgt_cube = target.get("cube_id")
                    tgt_addr_raw = target.get("addr")
                    if not isinstance(tgt_cube, str) or not isinstance(tgt_addr_raw, (tuple, list)):
                        continue
                    tgt_key = (tgt_cube, tuple(tgt_addr_raw))
                    edges.append((tgt_key, curr_key, "upstream"))

                for target in row.get("dependent_targets") or []:
                    tgt_cube = target.get("cube_id")
                    tgt_addr_raw = target.get("addr")
                    if not isinstance(tgt_cube, str) or not isinstance(tgt_addr_raw, (tuple, list)):
                        continue
                    tgt_key = (tgt_cube, tuple(tgt_addr_raw))
                    edges.append((curr_key, tgt_key, "downstream"))

            if _FLOW_DEBUG:
                print("FLOW_GRAPH: root", root_key)

            depth_limit = int(self._depth.value())
            self._augment_upstream_nodes(node_rows, edges, root_key)
            self._augment_downstream_nodes(node_rows, edges, depth_limit, root_key)

            if _FLOW_DEBUG:
                print("FLOW_GRAPH: edges total", len(edges))
                for start, end, direction in edges:
                    if direction == "downstream":
                        print("FLOW_GRAPH: downstream edge", start, "->", end)

            upstream_graph: Dict[KeyType, List[KeyType]] = defaultdict(list)
            for start_key, end_key, direction in edges:
                if direction == "upstream":
                    upstream_graph[end_key].append(start_key)

            queue_up: list[tuple[KeyType, int]] = []
            if root_key is not None:
                queue_up.append((root_key, int(node_rows[root_key].get("_graph_layer", 0))))
            seen_up: set[KeyType] = set()
            while queue_up:
                curr_key, curr_layer = queue_up.pop(0)
                if curr_key in seen_up:
                    continue
                seen_up.add(curr_key)
                for pred_key in upstream_graph.get(curr_key, []):
                    pred_row = node_rows.get(pred_key)
                    if pred_row is None:
                        continue
                    desired_layer = curr_layer - 1
                    existing_layer = int(pred_row.get("_graph_layer", desired_layer))
                    if desired_layer < existing_layer:
                        pred_row["_graph_layer"] = desired_layer
                    queue_up.append((pred_key, desired_layer))

            for key, row in node_rows.items():
                if key == root_key:
                    continue
                if row.get("_is_downstream") and row.get("_graph_layer", 0) <= 0:
                    row["_graph_layer"] = max(1, abs(int(row.get("_graph_layer", 0))) + 1)

            downstream_graph: Dict[KeyType, List[KeyType]] = defaultdict(list)
            for start_key, end_key, direction in edges:
                if direction == "downstream":
                    downstream_graph[start_key].append(end_key)

            queue: List[KeyType] = [root_key]
            seen_down: set[KeyType] = set()
            while queue:
                curr_key = queue.pop(0)
                if curr_key in seen_down:
                    continue
                seen_down.add(curr_key)
                curr_row = node_rows.get(curr_key)
                if curr_row is None:
                    continue
                curr_layer = int(curr_row.get("_graph_layer", 0))
                children = downstream_graph.get(curr_key, [])
                if _FLOW_DEBUG and children:
                    print("FLOW_GRAPH: advancing", curr_key, "layer", curr_layer, "children", children)
                for child_key in children:
                    child_row = node_rows.get(child_key)
                    if child_row is None:
                        continue
                    desired_layer = curr_layer + 1
                    existing_layer = int(child_row.get("_graph_layer", desired_layer))
                    if desired_layer > existing_layer:
                        child_row["_graph_layer"] = desired_layer
                        queue.append(child_key)
                    elif child_key not in seen_down:
                        queue.append(child_key)

            if _FLOW_DEBUG:
                print("FLOW_GRAPH: node layers after propagation")
                for key, row in node_rows.items():
                    print("   ", key, row.get("_graph_layer"))

            layers = sorted({int(row.get("_graph_layer", 0)) for row in node_rows.values()})
            neg_layers = [layer for layer in layers if layer < 0]
            pos_layers = [layer for layer in layers if layer > 0]

            ordered_layers: list[int] = []
            ordered_layers.extend(neg_layers)
            if 0 in layers:
                ordered_layers.append(0)
            ordered_layers.extend(pos_layers)

            layer_to_column: Dict[int, int] = {layer: idx for idx, layer in enumerate(ordered_layers)}

            default_column = layer_to_column.get(0, 0)
            for row in node_rows.values():
                layer = int(row.get("_graph_layer", 0))
                row["_graph_column"] = layer_to_column.get(layer, default_column)

            node_w = 320.0
            x_step = node_w + 48.0
            margin = 24.0
            vertical_gap = 16.0

            column_buckets: Dict[int, List[KeyType]] = defaultdict(list)
            for key, row in node_rows.items():
                column = int(row.get("_graph_column", default_column))
                column_buckets[column].append(key)

            node_rects: Dict[KeyType, QtCore.QRectF] = {}

            for column in sorted(column_buckets.keys()):
                keys = column_buckets[column]
                keys.sort(key=lambda k: node_rows[k].get("addr_label", ""))
                y_cursor = margin
                for key in keys:
                    row = node_rows[key]
                    x = margin + column * x_step

                    node_h, title_item, expr_item = self._build_text_items(row, node_w - 16.0)
                    rect = QtCore.QRectF(x, y_cursor, node_w, node_h)

                    brush_color = QtGui.QColor("#f5f7fb")
                    if key == root_key:
                        brush_color = QtGui.QColor("#fff7da")

                    rect_item = self._add_node_rect(rect, brush_color, key)

                    title_item.setParentItem(rect_item)
                    title_item.setPos(8.0, 8.0)

                    if expr_item is not None:
                        expr_bounds = expr_item.boundingRect()
                        expr_item.setParentItem(rect_item)
                        expr_item.setPos(8.0, node_h - expr_bounds.height() - 8.0)

                    node_rects[key] = rect
                    y_cursor += node_h + vertical_gap

            for start_key, end_key, direction in edges:
                start_rect = node_rects.get(start_key)
                end_rect = node_rects.get(end_key)
                if start_rect is None or end_rect is None or start_rect == end_rect:
                    continue
                base_color = QtGui.QColor("#2166b5" if direction == "upstream" else "#6d4cc2")
                highlight_color = QtGui.QColor("#00b8d4" if direction == "upstream" else "#d04aff")
                edge_graphics = self._draw_connection(start_rect, end_rect, base_color, highlight_color)
                self._graph_edges.append((start_key, end_key, edge_graphics))
                self._graph_adjacent[start_key].add(end_key)
                self._graph_adjacent[end_key].add(start_key)

        finally:
            self._suspend_hover_updates = False
            self._graph_scene.setSceneRect(self._graph_scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))
            self._handle_node_hover(None)

    def _build_text_items(
        self,
        row: dict[str, Any],
        text_width: float,
    ) -> tuple[float, QtWidgets.QGraphicsTextItem, QtWidgets.QGraphicsTextItem | None]:
        title = f"{row.get('addr_label', '')}\n[{row.get('source', '')}]"
        title_item = QtWidgets.QGraphicsTextItem(title)
        title_item.setDefaultTextColor(QtGui.QColor("#1d2533"))
        title_item.setTextWidth(text_width)
        title_item.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        title_item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        title_height = title_item.boundingRect().height()

        expr_item: QtWidgets.QGraphicsTextItem | None = None
        expr_height = 0.0
        expr = row.get("expression")
        if isinstance(expr, str) and expr:
            expr_item = QtWidgets.QGraphicsTextItem(f"= {expr}")
            expr_item.setDefaultTextColor(QtGui.QColor("#4d5b73"))
            expr_item.setTextWidth(text_width)
            expr_item.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            expr_item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
            expr_height = expr_item.boundingRect().height()

        total_height = title_height + expr_height + 24.0
        if expr_item is not None:
            total_height += 4.0
        total_height = max(total_height, 62.0)
        return total_height, title_item, expr_item

    def _fetch_node_row(self, cube_id: str, addr: tuple[str, ...]) -> dict[str, Any] | None:
        try:
            if self._session is None:
                return None
            trace = self._session.query(
                "diagnostics_calculation_flow",
                cube_id=cube_id,
                addr=addr,
                max_depth=1,
            )
        except Exception:
            return None
        if not trace:
            return None
        row = trace[0].copy()
        row["_graph_layer"] = 0
        return row

    def _augment_downstream_nodes(
        self,
        node_rows: Dict[Tuple[str, Tuple[str, ...]], dict[str, Any]],
        edges: list[tuple[Tuple[str, Tuple[str, ...]], Tuple[str, Tuple[str, ...]], str]],
        depth_limit: int,
        root_key: Tuple[str, Tuple[str, ...]],
    ) -> None:
        queue: list[tuple[Tuple[str, Tuple[str, ...]], int]] = [(root_key, 0)]
        visited: set[Tuple[str, Tuple[str, ...]]] = set()

        while queue:
            curr_key, depth = queue.pop(0)
            if curr_key in visited:
                continue
            visited.add(curr_key)
            row = node_rows.get(curr_key)
            if row is None:
                continue
            if depth == 0:
                row["_graph_layer"] = 0
            for target in row.get("dependent_targets") or []:
                cube_id = target.get("cube_id")
                addr_raw = target.get("addr")
                if not isinstance(cube_id, str) or not isinstance(addr_raw, (tuple, list)):
                    continue
                tgt_key = (cube_id, tuple(addr_raw))
                if tgt_key not in node_rows:
                    node_row = self._fetch_node_row(cube_id, tgt_key[1])
                    if node_row is None:
                        continue
                    node_rows[tgt_key] = node_row
                if tgt_key == curr_key:
                    continue
                next_depth = depth + 1
                node_rows[tgt_key]["_graph_layer"] = max(node_rows[tgt_key].get("_graph_layer", next_depth), next_depth)
                node_rows[tgt_key]["_is_downstream"] = True
                edges.append((curr_key, tgt_key, "downstream"))
                if next_depth < depth_limit:
                    queue.append((tgt_key, next_depth))

    def _augment_upstream_nodes(
        self,
        node_rows: Dict[KeyType, dict[str, Any]],
        edges: list[tuple[KeyType, KeyType, str]],
        root_key: KeyType,
    ) -> None:
        """Fetch missing upstream (precedent) nodes that aren't in the flow data.

        For global wildcard references (*.*), precedent nodes aren't traced deeper
        and thus not included in the flow data. This method fetches them for
        graph visualization.
        """
        # Collect all upstream keys from edges that don't exist in node_rows
        upstream_keys_to_fetch: set[KeyType] = set()
        for start_key, end_key, direction in edges:
            if direction == "upstream" and start_key not in node_rows:
                upstream_keys_to_fetch.add(start_key)

        if _FLOW_DEBUG:
            print(f"FLOW_GRAPH: fetching {len(upstream_keys_to_fetch)} missing upstream nodes")

        for cube_id, addr in upstream_keys_to_fetch:
            node_row = self._fetch_node_row(cube_id, addr)
            if node_row is not None:
                node_rows[(cube_id, addr)] = node_row
                if _FLOW_DEBUG:
                    print(f"FLOW_GRAPH: fetched upstream node {cube_id}, {addr}")

    def _draw_connection(
        self,
        start_rect: QtCore.QRectF,
        end_rect: QtCore.QRectF,
        base_color: QtGui.QColor,
        highlight_color: QtGui.QColor,
    ) -> _EdgeGraphics:
        start = QtCore.QPointF(start_rect.right(), start_rect.center().y())
        end = QtCore.QPointF(end_rect.left(), end_rect.center().y())
        if start.x() >= end.x():
            start = start_rect.center()
            end = end_rect.center()

        path = QtGui.QPainterPath(start)
        offset = max(50.0, (end.x() - start.x()) * 0.4)
        c1 = QtCore.QPointF(start.x() + offset, start.y())
        c2 = QtCore.QPointF(end.x() - offset, end.y())
        path.cubicTo(c1, c2, end)

        lane_item = QtWidgets.QGraphicsPathItem(path)
        lane_item.setZValue(-2)
        self._graph_scene.addItem(lane_item)

        stroke_item = QtWidgets.QGraphicsPathItem(path)
        stroke_item.setZValue(-1)
        self._graph_scene.addItem(stroke_item)

        angle = math.atan2(end.y() - c2.y(), end.x() - c2.x())
        arrow_size = 9.0
        p1 = QtCore.QPointF(
            end.x() - arrow_size * math.cos(angle - math.pi / 6),
            end.y() - arrow_size * math.sin(angle - math.pi / 6),
        )
        p2 = QtCore.QPointF(
            end.x() - arrow_size * math.cos(angle + math.pi / 6),
            end.y() - arrow_size * math.sin(angle + math.pi / 6),
        )
        arrow_head = QtGui.QPolygonF([end, p1, p2])
        head_item = QtWidgets.QGraphicsPolygonItem(arrow_head)
        head_item.setBrush(QtGui.QBrush(base_color))
        head_item.setZValue(0.5)
        self._graph_scene.addItem(head_item)

        graphics = _EdgeGraphics(
            lane=lane_item,
            stroke=stroke_item,
            head=head_item,
            base_color=base_color,
            highlight_color=highlight_color,
        )
        graphics.set_state("normal")
        return graphics

    def _handle_node_hover(self, key: KeyType | None) -> None:
        if self._suspend_hover_updates:
            return
        if not self._graph_node_items:
            return
        if key is None:
            for node_key, item in list(self._graph_node_items.items()):
                if not shiboken6.isValid(item):
                    self._graph_node_items.pop(node_key, None)
                    continue
                item.set_state("normal")
            for idx, (start, end, edge) in enumerate(list(self._graph_edges)):
                if not edge.is_valid():
                    self._graph_edges.pop(idx)
                    continue
                edge.set_state("normal")
            return

        neighbors = self._graph_adjacent.get(key, set())
        for node_key, item in list(self._graph_node_items.items()):
            if not shiboken6.isValid(item):
                self._graph_node_items.pop(node_key, None)
                continue
            if node_key == key:
                item.set_state("primary")
            elif node_key in neighbors:
                item.set_state("secondary")
            else:
                item.set_state("dim")

        new_edges: list[tuple[KeyType, KeyType, _EdgeGraphics]] = []
        for start, end, edge in self._graph_edges:
            if not edge.is_valid():
                continue
            if start == key or end == key:
                edge.set_state("primary")
            elif start in neighbors or end in neighbors:
                edge.set_state("secondary")
            else:
                edge.set_state("dim")
            new_edges.append((start, end, edge))
        self._graph_edges = new_edges

    @QtCore.Slot(QtWidgets.QListWidgetItem)
    def _on_item_activated(self, item: QtWidgets.QListWidgetItem) -> None:
        data = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not (isinstance(data, tuple) and len(data) == 2):
            return
        cube_id, addr = data
        if not isinstance(cube_id, str) or not isinstance(addr, tuple):
            return
        self.navigate_requested.emit(cube_id, addr)

    def _add_node_rect(
        self,
        rect: QtCore.QRectF,
        brush_color: QtGui.QColor,
        key: KeyType,
    ) -> _GraphNodeItem:
        local_rect = QtCore.QRectF(0.0, 0.0, rect.width(), rect.height())
        item = _GraphNodeItem(local_rect, brush_color, key, self)
        item.setData(0, key)
        item.setPos(rect.topLeft())
        self._graph_scene.addItem(item)
        self._graph_node_items[key] = item
        return item
