from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

DEBUG_GUI = os.environ.get("DEBUG_GUI", "false").lower() in ("true", "1", "yes")

if TYPE_CHECKING:
    from lib_gui.app import MainWindow
    from lib_gui.actions import MainWindowActions

from lib_utils.paths import DEFAULT_TOOLBOX_CONFIG_PATH

from lib_gui.menubuilder.models import WidgetType


def create_menus(main_window: MainWindow, actions: MainWindowActions) -> None:
    """Create all menus for the MainWindow."""
    mw = main_window
    act = actions  # shorthand

    # File menu
    menu = mw.menuBar().addMenu("File")
    menu.addAction(act.act_new)
    menu.addAction(act.act_open)
    menu.addAction(act.act_import_script)
    menu.addAction(act.act_save)
    menu.addSeparator()
    menu.addAction(act.act_quit)

    # Edit menu
    edit = mw.menuBar().addMenu("Edit")
    edit.addAction(act.act_undo)
    edit.addAction(act.act_redo)
    edit.addSeparator()
    edit.addAction(act.act_copy)
    edit.addAction(act.act_paste)
    edit.addAction(act.act_paste_as_new_cube)
    edit.addSeparator()
    edit.addAction(act.act_convert_selection_to_dimension_labels)
    edit.addAction(act.act_assign_item_labels_from_selection)
    edit.addAction(act.act_delete_selected_dimension_items)
    edit.addSeparator()
    edit.addAction(act.act_clear_override)
    edit.addSeparator()
    edit.addAction(act.act_set_rule_body)
    edit.addSeparator()
    edit.addAction(act.act_focus_rule_input_bar)

    # Model menu
    model = mw.menuBar().addMenu("Model")
    model.addAction(act.act_create_dimension)
    model.addAction(act.act_create_dimension_item)
    model.addAction(act.act_delete_dimension)
    model.addSeparator()
    model.addAction(act.act_create_cube)
    model.addAction(act.act_delete_cube)
    model.addSeparator()
    model.addAction(act.act_attach_dimension_to_cube)
    model.addSeparator()
    model.addAction(act.act_edit_view_axes)
    model.addSeparator()
    model.addAction(act.act_add_view)

    # View menu - use dock widget toggleViewAction for automatic sync
    view = mw.menuBar().addMenu("View")
    view.addSeparator()
    # Use toggleViewAction which automatically syncs with dock visibility
    view.addAction(mw._dock_browser.toggleViewAction())
    view.addAction(mw._dock_info.toggleViewAction())
    view.addAction(mw._dock_format.toggleViewAction())
    view.addAction(mw._dock_perf.toggleViewAction())
    view.addAction(mw._timeline_manager.toggleViewAction())
    view.addSeparator()
    # Toolbox Editor
    view.addAction(act.act_toolbox_editor)
    view.addSeparator()
    view.addAction(act.act_recalc_visible)
    view.addAction(act.act_recalc)

    # Plugins menu (stored on main_window for later loading by composition root)
    mw._plugins_menu = mw.menuBar().addMenu("Plugins")

    # Tools menu with Calculation Engine submenu and Options
    tools = mw.menuBar().addMenu("Tools")
    engine_menu = tools.addMenu("Calculation Engine")
    engine_menu.addAction(act.act_engine_python)
    engine_menu.addAction(act.act_engine_remote)
    tools.addSeparator()
    tools.addAction(act.act_options)

    # Window menu
    window = mw.menuBar().addMenu("Window")
    window.addAction(act.act_new_workspace)
    window.addAction(act.act_close_workspace)

    # Developer menu
    dev = mw.menuBar().addMenu("Developer")
    dev.addAction(act.act_toggle_debug_tooltips)

    # Help menu
    help_menu = mw.menuBar().addMenu("Help")
    help_menu.addAction(act.act_about)


def _load_plugins_menu_entries(main_window: MainWindow) -> None:
    """Load plugin menu entries."""
    if not hasattr(main_window, "_plugins_menu"):
        return
    loaded, errors = load_plugins(main_window, main_window._plugins_menu)
    if not loaded and main_window._plugins_menu.isEmpty():
        main_window._plugins_menu.setEnabled(False)
    if errors:
        warnings.warn("Plugin load errors: " + "; ".join(errors), RuntimeWarning)


def create_toolbar(main_window: MainWindow, actions: MainWindowActions) -> None:
    """Create the main toolbar for the MainWindow."""
    mw = main_window
    act = actions  # shorthand

    tb = mw.addToolBar("Main")
    tb.setObjectName("MainToolBar")
    tb.setMovable(False)
    tb.setFloatable(False)
    tb.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)

    # Try to load toolbar from config file
    if DEFAULT_TOOLBOX_CONFIG_PATH.exists():
        try:
            from lib_gui.menubuilder.toolbox_editor import load_toolbox_config
            config = load_toolbox_config()
            # Get items from toolbar_layout (list of item IDs) and items dict
            toolbar_items = []
            for i, item_id in enumerate(config.toolbar_layout):
                if item_id in config.items:
                    item = config.items[item_id]
                    toolbar_items.append(item)
            _load_toolbar_from_config(tb, act, toolbar_items)
            return
        except Exception as e:
            warnings.warn(f"Failed to load toolbox config: {e}", RuntimeWarning)
            # Fall through to default toolbar

    # Default toolbar items
    tb.addAction(act.act_new)
    tb.addAction(act.act_open)
    tb.addAction(act.act_save)
    tb.addSeparator()
    tb.addAction(act.act_copy)
    tb.addAction(act.act_paste)


def _connect_widget_macro(
    widget: QtWidgets.QWidget,
    macro_id: str,
    widget_type: WidgetType,
    main_window: "MainWindow",
) -> None:
    """Connect a widget's value-change signal to run its assigned macro."""
    if not macro_id or not main_window or not hasattr(main_window, '_run_macro'):
        return

    def run_with_value(value, mid=macro_id, mw=main_window):
        if DEBUG_GUI:
            print(f"[DEBUG-BUTTON] Widget {widget_type.value} changed, running macro '{mid}' with value={value!r}")
        mw._run_macro(mid, value)

    if widget_type == WidgetType.FONT_NAME and hasattr(widget, 'font_selected'):
        widget.font_selected.connect(run_with_value)
    elif widget_type == WidgetType.FONT_SIZE and hasattr(widget, 'size_selected'):
        widget.size_selected.connect(run_with_value)
    elif widget_type == WidgetType.FONT_COLOR and hasattr(widget, 'color_changed'):
        widget.color_changed.connect(run_with_value)
        if hasattr(widget, 'color_applied'):
            widget.color_applied.connect(run_with_value)
    elif widget_type == WidgetType.COLOR_PICKER and hasattr(widget, 'color_changed'):
        widget.color_changed.connect(run_with_value)
        if hasattr(widget, 'color_applied'):
            widget.color_applied.connect(run_with_value)
    elif widget_type == WidgetType.TOGGLE and hasattr(widget, 'toggled'):
        widget.toggled.connect(run_with_value)


def _load_toolbar_from_config(tb: QtWidgets.QToolBar, act: MainWindowActions, items: list) -> None:
    """Load toolbar items from config."""
    from lib_gui.menubuilder.widgets import load_svg_icon

    # Map command_id to actions
    action_map = {
        'new': act.act_new,
        'open': act.act_open,
        'save': act.act_save,
        'file_new': act.act_new,
        'file_open': act.act_open,
        'file_save': act.act_save,
        'copy': act.act_copy,
        'paste': act.act_paste,
        'quit': act.act_quit,
        'undo': act.act_undo,
        'redo': act.act_redo,
        'set_rule_body': act.act_set_rule_body,
        'focus_rule_input_bar': act.act_focus_rule_input_bar,
    }

    main_window = tb.parent()

    for item in items:
        # Separators have widget_type == WidgetType.SEPARATOR
        if item.widget_type == WidgetType.SEPARATOR:
            sep_action = tb.addSeparator()
            # Store item_id for identification during save
            if sep_action:
                sep_action.setProperty("item_id", item.id)
        elif item.command_id and item.command_id in action_map:
            action = action_map[item.command_id]
            action.setProperty("item_id", item.id)
            tb.addAction(action)
            
            # Add click debug to widget
            widget = tb.widgetForAction(action)
            if widget:
                if DEBUG_GUI:
                    print(f"[DEBUG-BUTTON] Widget created for command '{item.command_id}': type={type(widget).__name__}")
                def on_action_click(checked, cmd=item.command_id, lbl=item.label or action.text()):
                    if DEBUG_GUI:
                        print(f"[DEBUG-BUTTON] Mapped action clicked: command='{cmd}', label='{lbl}'")
                widget.clicked.connect(on_action_click)
            else:
                if DEBUG_GUI:
                    print(f"[DEBUG-BUTTON] WARNING: No widget created for command '{item.command_id}'")
        elif item.command_id:
            # Create placeholder action for unknown command_ids (will be tied to macros later)
            placeholder = QtGui.QAction(item.label or item.command_id, tb)
            placeholder.setEnabled(True)
            placeholder.setProperty("item_id", item.id)
            if item.icon:
                placeholder.setIcon(load_svg_icon(item.icon, 16, "#4B5563"))
            
            # If this item has a macro_id, connect it to run the macro
            if item.macro_id and main_window and hasattr(main_window, '_run_macro'):
                macro_id = item.macro_id
                placeholder.setData(macro_id)
                
                def run_macro_from_placeholder(checked, mid=macro_id, mw=main_window):
                    if DEBUG_GUI:
                        print(f"[DEBUG-BUTTON] Placeholder action triggered for macro '{mid}'")
                    mw._run_macro(mid)
                
                placeholder.triggered.connect(run_macro_from_placeholder)
            
            tb.addAction(placeholder)
            
            # Add click debug to placeholder widget
            widget = tb.widgetForAction(placeholder)
            if widget:
                if DEBUG_GUI:
                    print(f"[DEBUG-BUTTON] Widget created for placeholder command '{item.command_id}': type={type(widget).__name__}")
                def on_placeholder_click(checked, cmd=item.command_id, lbl=item.label or item.command_id):
                    if DEBUG_GUI:
                        print(f"[DEBUG-BUTTON] Placeholder widget clicked: command='{cmd}', label='{lbl}'")
                widget.clicked.connect(on_placeholder_click)
            else:
                if DEBUG_GUI:
                    print(f"[DEBUG-BUTTON] WARNING: No widget created for placeholder command '{item.command_id}'")
        elif item.widget_type == WidgetType.FONT_NAME:
            # IMPORTANT: Check widget types BEFORE generic label check
            # because widgets like Font Name have labels but should be widgets, not buttons
            # Create font name dropdown
            from lib_gui.widgets import FontNameDropdown
            font_combo = FontNameDropdown()
            font_combo.setToolTip(item.label or "Font Name")
            font_combo.setProperty("item_id", item.id)
            font_combo.setProperty("_toolbar_widget_label", item.label)
            tb.addWidget(font_combo)
            _connect_widget_macro(font_combo, item.macro_id, WidgetType.FONT_NAME, main_window)

        elif item.widget_type == WidgetType.FONT_SIZE:
            # Create font size dropdown
            from lib_gui.widgets import FontSizeDropdown
            font_size_combo = FontSizeDropdown()
            font_size_combo.setToolTip(item.label or "Font Size")
            font_size_combo.setProperty("item_id", item.id)
            font_size_combo.setProperty("_toolbar_widget_label", item.label)
            tb.addWidget(font_size_combo)
            _connect_widget_macro(font_size_combo, item.macro_id, WidgetType.FONT_SIZE, main_window)

        elif item.widget_type == WidgetType.FONT_COLOR:
            # Create font color picker button
            from lib_gui.mini_color_picker import MiniFontColorButton
            font_color_btn = MiniFontColorButton()
            font_color_btn.setToolTip(item.label or "Font Color")
            font_color_btn.setProperty("item_id", item.id)
            font_color_btn.setProperty("_toolbar_widget_label", item.label)
            tb.addWidget(font_color_btn)
            _connect_widget_macro(font_color_btn, item.macro_id, WidgetType.FONT_COLOR, main_window)

        elif item.widget_type == WidgetType.COLOR_PICKER:
            # Create cell fill color picker button
            from lib_gui.mini_color_picker import MiniCellFillButton
            cell_fill_btn = MiniCellFillButton()
            cell_fill_btn.setToolTip(item.label or "Cell Fill")
            cell_fill_btn.setProperty("item_id", item.id)
            cell_fill_btn.setProperty("_toolbar_widget_label", item.label)
            tb.addWidget(cell_fill_btn)
            _connect_widget_macro(cell_fill_btn, item.macro_id, WidgetType.COLOR_PICKER, main_window)
        elif item.macro_id:
            # Create button for macro execution
            macro_action = QtGui.QAction(item.label or "Macro", tb)
            macro_action.setEnabled(True)
            macro_action.setProperty("item_id", item.id)
            macro_action.setData(item.macro_id)
            if item.icon:
                try:
                    macro_action.setIcon(load_svg_icon(item.icon, 16, "#4B5563"))
                except Exception:
                    pass
            # Connect to macro runner
            if main_window and hasattr(main_window, '_run_macro'):
                
                def run_macro_with_debug(checked, mid=item.macro_id, mw=main_window):
                    if DEBUG_GUI:
                        print(f"[DEBUG-BUTTON] Action triggered for macro '{mid}' (from config load)")
                    mw._run_macro(mid)
                
                macro_action.triggered.connect(run_macro_with_debug)
            else:
                pass
            tb.addAction(macro_action)
            
            # Add click debug to widget (for tracing)
            widget = tb.widgetForAction(macro_action)
            if widget:
                def on_widget_click(checked, lbl=item.label or "Macro"):
                    if DEBUG_GUI:
                        print(f"[DEBUG-BUTTON] Widget clicked: '{lbl}' (from config load)")
                widget.clicked.connect(on_widget_click)
            
        elif item.label:
            # Create button with just a label (for macro-triggered buttons)
            # This comes AFTER widget type checks so widgets with labels don't become buttons
            placeholder = QtGui.QAction(item.label, tb)
            placeholder.setEnabled(True)
            placeholder.setProperty("item_id", item.id)
            if item.icon:
                placeholder.setIcon(load_svg_icon(item.icon, 16, "#4B5563"))
            
            # If this item has a macro_id, connect it to run the macro
            if item.macro_id and main_window and hasattr(main_window, '_run_macro'):
                macro_id = item.macro_id
                placeholder.setData(macro_id)
                
                def run_macro_from_label_button(checked, mid=macro_id, mw=main_window):
                    if DEBUG_GUI:
                        print(f"[DEBUG-BUTTON] Label button triggered for macro '{mid}'")
                    mw._run_macro(mid)
                
                placeholder.triggered.connect(run_macro_from_label_button)
            
            tb.addAction(placeholder)
            
            # Add click debug to widget
            widget = tb.widgetForAction(placeholder)
            if widget:
                def on_regular_button_click(checked, lbl=item.label):
                    if DEBUG_GUI:
                        print(f"[DEBUG-BUTTON] Regular button clicked: '{lbl}'")
                widget.clicked.connect(on_regular_button_click)
            else:
                pass
        else:
            pass  # Widget items are not supported in initial load
