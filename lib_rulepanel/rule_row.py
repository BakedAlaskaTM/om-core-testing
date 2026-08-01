"""Rule row widget for display and editing."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, TYPE_CHECKING

from PySide6.QtWidgets import (
    QFrame, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QMenu, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QPoint, QMimeData, QPropertyAnimation, QEasingCurve, QTimer
from PySide6.QtGui import QIcon, QPixmap, QColor, QPainter, QDrag, QKeyEvent
from PySide6.QtSvg import QSvgRenderer

from .rule_models import RuleData

if TYPE_CHECKING:
    from .rule_list import RuleListWidget


class EditableRuleRow(QFrame):
    """A rule row that can be clicked to edit.

    Shows as two lines:
      Line 1: LHS = RHS (rule target = rule body)
      Line 2: Status (metadata)

    Supports drag-and-drop reordering.
    """

    drag_started = Signal(object)  # Emitted when drag starts
    dropped_on = Signal(object, object)  # Emitted when something dropped on this row
    context_menu_requested = Signal(QPoint)  # Emitted when right-clicked
    
    # Class-level: track which row started the drag (if any)
    _global_drag_source: Optional["EditableRuleRow"] = None

    def __init__(self, rule: RuleData, is_selected: bool = False, 
                 is_even: bool = True, row_number: int = 0, parent=None):
        super().__init__(parent)
        self.rule_body = rule
        self.is_selected = is_selected
        self.is_editing = False
        self.is_even = is_even  # For alternating background
        self.row_number = row_number  # Line number to display (1-based)

        # Drag-and-drop state
        self.drag_start_pos: Optional[QPoint] = None
        self._drag_active = False

        # Enable drag and drop and context menu
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        # Enable mouse tracking to ensure we get mouseMoveEvents even without buttons
        self.setMouseTracking(True)

        self.setup_ui()

    def _on_context_menu(self, pos: QPoint) -> None:
        """Emit signal when context menu requested."""
        # Convert to global coordinates
        global_pos = self.mapToGlobal(pos)
        self.context_menu_requested.emit(global_pos)
        
    def setup_ui(self):
        """Set up the row with display or edit mode."""
        # Only create layout once - use VBox for two-line layout
        if self.layout() is None:
            main_layout = QVBoxLayout(self)
            main_layout.setContentsMargins(0, 3, 0, 3)
            main_layout.setSpacing(0)

            # Line 1: Rule body content (HBox) with line number
            self.rule_body_line = QWidget()
            self.rule_body_line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            self.row_layout = QHBoxLayout(self.rule_body_line)
            self.row_layout.setContentsMargins(4, 4, 8, 2)
            self.row_layout.setSpacing(6)

            # Line number label
            self.num_label = QLabel(f"{self.row_number}." if self.row_number > 0 else "")
            self.num_label.setStyleSheet("color: #374151; font-size: 11px; min-width: 20px;")
            self.num_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.row_layout.addWidget(self.num_label)

            main_layout.addWidget(self.rule_body_line)

            # Line 2: Metadata
            self.meta_line = QWidget()
            self.meta_layout = QHBoxLayout(self.meta_line)
            self.meta_layout.setContentsMargins(28, 2, 8, 2)
            self.meta_layout.setSpacing(6)

            self.meta_label = QLabel()
            self.meta_label.setStyleSheet("color: #9CA3AF; font-size: 11px;")
            self.meta_layout.addWidget(self.meta_label, stretch=1)
            main_layout.addWidget(self.meta_line)

        # Set metadata text
        self.meta_label.setText(self.rule_body.status)

        # Build tooltip
        mask_display = self.rule_body.mask if self.rule_body.mask else "(none)"
        tooltip = f"Mask: {mask_display}\n"
        tooltip += f"Specificity: {self.rule_body.specificity}\n\n"
        tooltip += "Precedence:\n"
        tooltip += "  - More constrained dimensions (higher specificity) win.\n"
        tooltip += "  - If specificity ties and rules overlap, later rules override earlier ones."
        self.setToolTip(tooltip)

        self.update_style()
        
        # Clear existing widgets
        self.clear_layout()
        
        if self.is_editing:
            self.setup_edit_mode()
        else:
            self.setup_display_mode()

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            
    def _load_svg_icon(self, icon_name: str, size: int = 14) -> QPixmap:
        """Load a Lucide SVG icon and render it to a pixmap."""
        try:
            # Try project assets first
            icon_path = Path(__file__).parent.parent / "assets" / "icons" / "lucide" / "icons" / f"{icon_name}.svg"
            if not icon_path.exists():
                return QPixmap()
            renderer = QSvgRenderer(str(icon_path))
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            return pixmap
        except Exception:
            return QPixmap()

    def update_style(self):
        """Update alternating background and selection styling."""
        if self.is_selected:
            bg_color = "#BFDBFE"  # Blue selection
        elif self.is_even:
            bg_color = "#FFFFFF"  # White
        else:
            bg_color = "#E5E7EB"  # Darker gray for contrast
        self.setStyleSheet(f"background-color: {bg_color}; border-radius: 4px;")
        
        # Update line number
        try:
            self.num_label.setText(f"{self.row_number}." if self.row_number > 0 else "")
        except RuntimeError:
            pass

    def setup_display_mode(self):
        """Show rule body as read-only label with monospace font."""
        # Show meta line only if status is not empty
        if self.rule_body.status:
            self.meta_line.show()
        else:
            self.meta_line.hide()

        try:
            self.num_label.show()
        except RuntimeError:
            self.num_label = QLabel(f"{self.row_number}." if self.row_number > 0 else "")
            self.num_label.setStyleSheet("color: #374151; font-size: 11px; min-width: 20px;")
            self.num_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.row_layout.insertWidget(0, self.num_label)
            self.num_label.show()

        # Handle multi-line RHS
        rhs_lines = self.rule_body.rhs.split("\n")
        if len(rhs_lines) > 1:
            indented_lines = [rhs_lines[0]] + ["&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;" + line for line in rhs_lines[1:]]
            rhs_html = "<br>".join(indented_lines)
        else:
            rhs_html = rhs_lines[0]

        rule_body_text = f"<span style='color: #1E40AF; font-weight: 500; font-family: monospace;'>{self.rule_body.lhs}</span>"
        rule_body_text += f"<span style='color: #6B7280; font-family: monospace;'> = </span>"
        rule_body_text += f"<span style='color: #111827; font-family: monospace;'>{rhs_html}</span>"

        self.rule_body_label = QLabel()
        self.rule_body_label.setTextFormat(Qt.TextFormat.RichText)
        self.rule_body_label.setWordWrap(True)
        self.rule_body_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.rule_body_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.rule_body_label.setText(rule_body_text)
        self.row_layout.addWidget(self.rule_body_label, stretch=1)

        # Edit button
        edit_btn = QPushButton()
        edit_btn.setFixedSize(20, 20)
        edit_btn.setIcon(QIcon(self._load_svg_icon("pencil")))
        edit_btn.setIconSize(QPixmap(14, 14).size())
        edit_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: rgba(0,0,0,0.05);
            }
        """)
        edit_btn.clicked.connect(self.start_edit)
        self.row_layout.addWidget(edit_btn)

        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def hideEvent(self, event):
        """Release mouse grab when widget is hidden."""
        # Release mouse if we have it grabbed
        try:
            self.releaseMouse()
        except Exception:
            pass
        EditableRuleRow._global_drag_source = None
        super().hideEvent(event)

    def leaveEvent(self, event):
        """Release mouse grab when cursor leaves the widget."""
        # If we're the drag source but not actively dragging, release
        if EditableRuleRow._global_drag_source is self and not getattr(self, '_drag_active', False):
            try:
                self.releaseMouse()
            except Exception:
                pass
            EditableRuleRow._global_drag_source = None
        super().leaveEvent(event)

    def focusOutEvent(self, event):
        """Release mouse grab when widget loses focus."""
        if EditableRuleRow._global_drag_source is self:
            try:
                self.releaseMouse()
            except Exception:
                pass
            EditableRuleRow._global_drag_source = None
        super().focusOutEvent(event)

    def resizeEvent(self, event):
        """Defer height update to break the layout feedback loop.

        setMinimumHeight triggers an async LayoutRequest which fires another
        resizeEvent.  Doing the update synchronously creates an oscillating
        cycle (height bounces every paint).  Deferring to the next event-loop
        iteration lets the layout settle before we touch minimumHeight again.
        """
        super().resizeEvent(event)
        if self.is_editing:
            return
        if getattr(self, "_height_timer", None) is not None:
            return
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._update_label_height)
        self._height_timer.start(0)

    def _update_label_height(self):
        """Set the label's minimum height to fit its wrapped text."""
        self._height_timer = None
        label = getattr(self, "rule_body_label", None)
        if not isinstance(label, QLabel):
            return
        width = label.width()
        if width <= 0:
            return
        old_min = label.minimumHeight()
        label.setMinimumHeight(0)
        target_height = label.heightForWidth(width)
        if target_height > 0 and old_min != target_height:
            label.setMinimumHeight(target_height)

    def setup_edit_mode(self):
        """Show inline editor with multi-line support."""
        self.meta_line.hide()
        try:
            self.num_label.hide()
        except RuntimeError:
            pass

        self.editor = QTextEdit()
        text = f"{self.rule_body.lhs} = {self.rule_body.rhs}"
        self.editor.setPlainText(text)
        self.editor.setStyleSheet("""
            QTextEdit {
                background-color: white;
                border: 1px solid #3B82F6;
                border-radius: 3px;
                padding: 2px 4px;
                font-family: monospace;
                font-size: 13px;
            }
        """)
        
        line_count = text.count('\n') + 1
        min_height = max(48, line_count * 18 + 8)
        
        parent_scroll = self.parent()
        while parent_scroll and not hasattr(parent_scroll, 'viewport'):
            parent_scroll = parent_scroll.parent()
        
        if parent_scroll and hasattr(parent_scroll, 'viewport'):
            max_height = int(parent_scroll.viewport().height() * 0.8)
        else:
            max_height = 400
        
        target_height = min(max_height, max(min_height, 48))
        
        self.editor.setMaximumHeight(max_height)
        self.editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.row_layout.addWidget(self.editor, stretch=1)
        
        self._editor_max_height = max_height
        self.editor.textChanged.connect(self._update_editor_height)
        self.editor.document().adjustSize()
        self._update_editor_height()
        
        # Save/cancel buttons
        save_btn = QPushButton("✓")
        save_btn.setFixedSize(22, 22)
        save_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        save_btn.setToolTip("Save")
        save_btn.setStyleSheet("""
            QPushButton {
                color: white;
                background-color: #22C55E;
                border: none;
                border-radius: 3px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #16A34A; }
        """)
        save_btn.clicked.connect(self.save_edit)
        self.row_layout.addWidget(save_btn)

        cancel_btn = QPushButton("✕")
        cancel_btn.setFixedSize(22, 22)
        cancel_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cancel_btn.setToolTip("Cancel")
        cancel_btn.setStyleSheet("""
            QPushButton {
                color: white;
                background-color: #EF4444;
                border: none;
                border-radius: 3px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #DC2626; }
        """)
        cancel_btn.clicked.connect(self.cancel_edit)
        self.row_layout.addWidget(cancel_btn)
        
        self.editor.setFocus()
        self.editor.selectAll()
        self.editor.installEventFilter(self)
        
    def _update_editor_height(self):
        """Dynamically adjust editor height to fit content."""
        if not hasattr(self, 'editor') or not self.editor:
            return
        
        doc = self.editor.document()
        doc_height = doc.size().height()
        target_height = int(doc_height + 8)
        
        max_height = getattr(self, '_editor_max_height', 400)
        target_height = max(48, min(target_height, max_height))
        
        current_min = self.editor.minimumHeight()
        if target_height != current_min:
            self.editor.setMinimumHeight(target_height)
        
    def start_edit(self):
        """Switch to edit mode."""
        parent = self.parent()
        while parent and not hasattr(parent, 'start_row_edit'):
            parent = parent.parent()
        if parent and hasattr(parent, 'start_row_edit'):
            parent.start_row_edit(self)
        else:
            # Standalone mode - just start editing
            self.is_editing = True
            self.clear_layout()
            self.setup_edit_mode()
        
    def save_edit(self):
        """Save the edited rule body."""
        new_text = self.editor.toPlainText()
        original_text = None
        if self.rule_body.lhs:
            original_text = f"{self.rule_body.lhs} = {self.rule_body.rhs}"

        changed = True
        if " = " in new_text:
            lhs, rhs = new_text.split(" = ", 1)
            lhs = lhs.strip()
            rhs = rhs.strip()
            if original_text is not None:
                changed = new_text.strip() != original_text.strip()
            self.rule_body.lhs = lhs
            self.rule_body.rhs = rhs

        parent = self.parent()
        while parent and not hasattr(parent, 'end_row_edit'):
            parent = parent.parent()
        if parent and hasattr(parent, 'end_row_edit'):
            parent.end_row_edit(self, cancelled=not changed)

        self.is_editing = False
        self.clear_layout()
        self.setup_display_mode()
        
    def cancel_edit(self):
        """Cancel editing and revert."""
        parent = self.parent()
        while parent and not hasattr(parent, 'end_row_edit'):
            parent = parent.parent()
        if parent and hasattr(parent, 'end_row_edit'):
            parent.end_row_edit(self, cancelled=True)

        self.is_editing = False
        self.clear_layout()
        self.setup_display_mode()

    def eventFilter(self, obj, event):
        """Handle Enter/Esc/Alt+Enter and focus loss in the editor."""
        if obj == self.editor and self.is_editing:
            if event.type() == event.Type.FocusOut:
                # Clicking outside the editor (e.g. on the grid) abandons the edit.
                self.cancel_edit()
                return True
            if event.type() == event.Type.KeyPress:
                key = event.key()
                if key == Qt.Key.Key_Return:
                    if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                        self.editor.insertPlainText("\n")
                        return True
                    else:
                        self.save_edit()
                        return True
                elif key == Qt.Key.Key_Escape:
                    self.cancel_edit()
                    return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QKeyEvent):
        """Handle key presses at row level."""
        if event.key() == Qt.Key.Key_Escape and self.is_editing:
            self.cancel_edit()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Handle double-click to start editing."""
        if event.button() == Qt.MouseButton.LeftButton and not self.is_editing:
            # Get parent list widget and start edit
            parent = self.parent()
            while parent and not hasattr(parent, 'start_row_edit'):
                parent = parent.parent()
            if parent:
                parent.start_row_edit(self)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def clear_layout(self):
        """Clear all widgets from layout, but preserve num_label."""
        if self.row_layout:
            widgets_to_remove = []
            for i in range(self.row_layout.count()):
                item = self.row_layout.itemAt(i)
                if item.widget() and item.widget() != self.num_label:
                    widgets_to_remove.append(item.widget())

            for widget in widgets_to_remove:
                self.row_layout.removeWidget(widget)
                widget.deleteLater()

    def mousePressEvent(self, event):
        """Handle click to initiate drag - selection happens on release if not dragging."""
        if event.button() == Qt.MouseButton.LeftButton:
            # Grab mouse immediately so we receive ALL mouse events
            # (prevents child widgets or other widgets from stealing them)
            try:
                self.grabMouse()
            except Exception:
                pass
            old_source = EditableRuleRow._global_drag_source
            EditableRuleRow._global_drag_source = self
            grab_pos = event.position().toPoint()
            self.drag_start_pos = grab_pos
            self._drag_grab_pos = grab_pos
            self._drag_active = False
            self._pending_select = True
            
        child = self.childAt(event.position().toPoint())
        if isinstance(child, QPushButton):
            super().mousePressEvent(event)
            return

    def mouseReleaseEvent(self, event):
        """Clean up drag state and select row if we didn't drag."""
        if event.button() == Qt.MouseButton.LeftButton:
            
            # If we didn't drag and have pending selection, select the row now
            if not self._drag_active and getattr(self, '_pending_select', False):
                parent = self.parent()
                while parent and not hasattr(parent, 'select_row'):
                    parent = parent.parent()
                if parent and hasattr(parent, 'select_row') and not self.is_editing:
                    parent.select_row(self)
            
            # Always release our mouse grab on release (unless drag is active - drag releases it)
            if not self._drag_active:
                try:
                    self.releaseMouse()
                except Exception:
                    pass

            # Only clear global source if it's still us (we were the drag source)
            if EditableRuleRow._global_drag_source is self:
                EditableRuleRow._global_drag_source = None
            self.drag_start_pos = None
            self._drag_active = False
            self._pending_select = False
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        """Handle drag initiation."""
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        
        if EditableRuleRow._global_drag_source is not self:
            return
        
        pos = event.position().toPoint()
        drag_start = getattr(self, 'drag_start_pos', None)
        if drag_start:
            distance = (pos - drag_start).manhattanLength()

        self._handle_drag_move(pos)

    def _handle_drag_move(self, pos: QPoint):
        """Handle drag move - called to check threshold."""
        drag_start = getattr(self, 'drag_start_pos', None)
        if drag_start is None:
            return
        if getattr(self, '_drag_active', False):
            return
        if self.is_editing:
            self.drag_start_pos = None
            EditableRuleRow._global_drag_source = None
            return

        from PySide6.QtWidgets import QApplication
        distance = (pos - drag_start).manhattanLength()
        threshold = max(QApplication.startDragDistance(), 8)
        if distance < threshold:
            return

        # Drag threshold met - start drag (mouse already grabbed in press)
        self._start_drag()

    def _start_drag(self):
        """Actually start the QDrag operation."""
        self._drag_active = True
        self._pending_select = False
        self.drag_start_pos = None
        # Release our explicit mouse grab before QDrag takes over
        # (QDrag will do its own grabbing)
        try:
            self.releaseMouse()
        except Exception:
            pass

        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setText(str(self.row_number))
        drag.setMimeData(mime_data)

        pixmap = self.grab()
        transparent_pixmap = QPixmap(pixmap.size())
        transparent_pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(transparent_pixmap)
        painter.setOpacity(0.5)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        
        drag.setPixmap(transparent_pixmap)
        hotspot = getattr(self, '_drag_grab_pos', QPoint(pixmap.width() // 2, 0))
        drag.setHotSpot(hotspot)

        self.drag_started.emit(self)
        drag.exec(Qt.DropAction.MoveAction)

        self._drag_active = False
        # Only clear global source if it's still us (don't clear another row's state)
        if EditableRuleRow._global_drag_source is self:
            EditableRuleRow._global_drag_source = None

    def dragEnterEvent(self, event):
        """Accept drag events from other rows."""
        source = event.source()
        if source != self and isinstance(source, EditableRuleRow):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        """Show drop indicator."""
        source = event.source()
        if source != self and isinstance(source, EditableRuleRow):
            event.acceptProposedAction()
            parent = self.parent()
            while parent and not hasattr(parent, 'show_drop_indicator_at'):
                parent = parent.parent()
            if parent and hasattr(parent, 'show_drop_indicator_at'):
                mouse_pos = event.position().toPoint()
                content = parent.widget() if hasattr(parent, 'widget') else None
                if content:
                    mouse_in_content = self.mapTo(content, mouse_pos)
                    parent.show_drop_indicator_at(mouse_in_content.y())

    def dropEvent(self, event):
        """Handle drop."""
        source = event.source()
        if source and isinstance(source, EditableRuleRow):
            parent = self.parent()
            while parent and not hasattr(parent, '_insert_index'):
                parent = parent.parent()
            if parent:
                insert_idx = getattr(parent, '_insert_index', -1)
                if insert_idx >= 0:
                    self.dropped_on.emit(source, insert_idx)
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        """Hide indicator when leaving."""
        parent = self.parent()
        while parent and not hasattr(parent, 'hide_drop_indicator'):
            parent = parent.parent()
        if parent and hasattr(parent, 'hide_drop_indicator'):
            parent.hide_drop_indicator()


# Keep backward compatibility
RuleRow = EditableRuleRow
