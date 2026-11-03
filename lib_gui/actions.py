from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from lib_gui.icons import load_icon

if TYPE_CHECKING:
    from lib_gui.app import MainWindow


def _load_icon(icon_name: str, size: int = 20) -> QtGui.QIcon | None:
    """Load an SVG icon from the zipped icon bundle.

    icon_name is a zip-relative path such as "tabler/icons/outline/bold.svg"
    or a bare name that the bundle resolver can locate.
    """
    return load_icon(icon_name, size=size)


@dataclass
class MainWindowActions:
    """Container for all MainWindow QActions."""

    # File actions
    act_new: QtGui.QAction
    act_open: QtGui.QAction
    act_import_script: QtGui.QAction
    act_save: QtGui.QAction
    act_quit: QtGui.QAction

    # Edit actions
    act_undo: QtGui.QAction
    act_redo: QtGui.QAction
    act_copy: QtGui.QAction
    act_paste: QtGui.QAction
    act_paste_as_new_cube: QtGui.QAction
    act_convert_selection_to_dimension_labels: QtGui.QAction
    act_assign_item_labels_from_selection: QtGui.QAction
    act_delete_selected_dimension_items: QtGui.QAction
    act_clear_override: QtGui.QAction
    act_set_rule_body: QtGui.QAction
    act_focus_rule_input_bar: QtGui.QAction

    # Model actions
    act_create_dimension: QtGui.QAction
    act_create_dimension_item: QtGui.QAction
    act_delete_dimension: QtGui.QAction
    act_attach_dimension_to_cube: QtGui.QAction
    act_edit_view_axes: QtGui.QAction
    act_create_cube: QtGui.QAction
    act_delete_cube: QtGui.QAction
    act_add_view: QtGui.QAction

    # View actions (toggles)
    act_recalc: QtGui.QAction  # Shift+F9 - full recalc
    act_recalc_visible: QtGui.QAction  # F9 - recalculate visible cells only
    act_toolbox_editor: QtGui.QAction  # Toolbox Editor

    # Window actions
    act_new_workspace: QtGui.QAction
    act_close_workspace: QtGui.QAction

    # Developer actions
    act_toggle_debug_tooltips: QtGui.QAction

    # Help actions
    act_about: QtGui.QAction

    # Engine actions
    act_engine_python: QtGui.QAction
    act_engine_remote: QtGui.QAction
    engine_action_group: QtGui.QActionGroup

    # Tools actions
    act_options: QtGui.QAction


def create_actions(main_window: MainWindow) -> MainWindowActions:
    """Create and configure all QActions for the MainWindow."""
    mw = main_window  # shorthand

    # File actions
    act_new = QtGui.QAction("New", mw)
    act_new.setIcon(_load_icon("file-plus.svg"))
    act_new.triggered.connect(mw._on_new)

    act_open = QtGui.QAction("Open…", mw)
    act_open.setIcon(_load_icon("folder-open.svg"))
    act_open.triggered.connect(mw._on_open)

    act_import_script = QtGui.QAction("Import OpenM Script", mw)
    act_import_script.setIcon(_load_icon("file-import.svg"))
    act_import_script.triggered.connect(mw._on_import_script)

    act_save = QtGui.QAction("Save…", mw)
    act_save.setIcon(_load_icon("device-floppy.svg"))
    act_save.triggered.connect(mw._on_save)

    act_quit = QtGui.QAction("Quit", mw)
    act_quit.triggered.connect(mw.close)

    # Edit actions
    act_undo = QtGui.QAction("Undo", mw)
    act_undo.setIcon(_load_icon("arrow-back-up.svg"))
    act_undo.setShortcut(QtGui.QKeySequence.StandardKey.Undo)
    act_undo.triggered.connect(mw._on_undo)

    act_redo = QtGui.QAction("Redo", mw)
    act_redo.setIcon(_load_icon("arrow-forward-up.svg"))
    act_redo.setShortcut(QtGui.QKeySequence.StandardKey.Redo)
    act_redo.triggered.connect(mw._on_redo)

    act_copy = QtGui.QAction("Copy", mw)
    act_copy.setIcon(_load_icon("copy.svg"))
    act_copy.setShortcut(QtGui.QKeySequence.StandardKey.Copy)
    act_copy.triggered.connect(mw._on_copy)

    act_paste = QtGui.QAction("Paste", mw)
    act_paste.setIcon(_load_icon("clipboard.svg"))
    act_paste.setShortcut(QtGui.QKeySequence.StandardKey.Paste)
    act_paste.triggered.connect(mw._on_paste)

    act_paste_as_new_cube = QtGui.QAction("Paste as New Cube…", mw)
    act_paste_as_new_cube.triggered.connect(mw._on_paste_as_new_cube)

    act_convert_selection_to_dimension_labels = QtGui.QAction(
        "Convert Selected Data to New Dimension Item Labels…", mw
    )
    act_convert_selection_to_dimension_labels.triggered.connect(
        mw._on_convert_selected_data_to_dimension_item_labels
    )

    act_assign_item_labels_from_selection = QtGui.QAction(
        "Assign Item Labels from Selected Rows/Columns…", mw
    )
    act_assign_item_labels_from_selection.triggered.connect(
        mw._on_assign_item_labels_from_selected_rows_or_columns
    )

    act_delete_selected_dimension_items = QtGui.QAction(
        "Delete Selected Dimension Items…", mw
    )
    act_delete_selected_dimension_items.triggered.connect(
        mw._on_delete_selected_dimension_items
    )

    act_clear_override = QtGui.QAction("Clear Override", mw)
    act_clear_override.triggered.connect(mw._on_clear_override)

    act_set_rule_body = QtGui.QAction("Set Rule…", mw)
    act_set_rule_body.setShortcut(QtGui.QKeySequence("Ctrl+="))
    act_set_rule_body.triggered.connect(mw._on_set_rule_body)

    act_focus_rule_input_bar = QtGui.QAction("Focus Rule Bar", mw)
    act_focus_rule_input_bar.setShortcut(QtGui.QKeySequence("Ctrl+L"))
    act_focus_rule_input_bar.triggered.connect(mw._rule_bar.setFocus)

    # Model actions
    act_add_view = QtGui.QAction("New View…", mw)
    act_add_view.triggered.connect(mw._on_add_view)

    act_create_dimension = QtGui.QAction("New Dimension…", mw)
    act_create_dimension.triggered.connect(mw._on_create_dimension)

    act_create_dimension_item = QtGui.QAction("Create Dimension Item…", mw)
    act_create_dimension_item.triggered.connect(mw._on_create_dimension_item)

    act_delete_dimension = QtGui.QAction("Delete Dimension…", mw)
    act_delete_dimension.triggered.connect(mw._on_delete_dimension)

    act_attach_dimension_to_cube = QtGui.QAction("Attach Dimension To Cube…", mw)
    act_attach_dimension_to_cube.triggered.connect(mw._on_attach_dimension_to_cube)

    act_edit_view_axes = QtGui.QAction("Edit View Axes…", mw)
    act_edit_view_axes.triggered.connect(mw._on_edit_view_axes)

    act_create_cube = QtGui.QAction("New Cube…", mw)
    act_create_cube.triggered.connect(mw._on_create_cube)

    act_delete_cube = QtGui.QAction("Delete Cube…", mw)
    act_delete_cube.triggered.connect(mw._on_delete_cube)

    # View actions
    act_recalc = QtGui.QAction("Recalculate Full Model", mw)
    act_recalc.setShortcut(QtGui.QKeySequence("Shift+F9"))
    act_recalc.setShortcutContext(QtCore.Qt.ShortcutContext.ApplicationShortcut)
    act_recalc.triggered.connect(mw._on_recalculate)
    mw.addAction(act_recalc)  # Add to main window for global shortcut

    act_recalc_visible = QtGui.QAction("Recalculate View", mw)
    act_recalc_visible.setShortcut(QtGui.QKeySequence("F9"))
    act_recalc_visible.setShortcutContext(QtCore.Qt.ShortcutContext.ApplicationShortcut)
    act_recalc_visible.triggered.connect(mw._on_recalculate_visible)
    mw.addAction(act_recalc_visible)  # Add to main window for global shortcut

    # Toolbox Editor action
    act_toolbox_editor = QtGui.QAction("Toolbox Editor…", mw)
    act_toolbox_editor.setIcon(_load_icon("tool.svg"))
    act_toolbox_editor.triggered.connect(mw._on_toolbox_editor)

    # Window actions
    act_new_workspace = QtGui.QAction("New Window", mw)
    act_new_workspace.triggered.connect(mw._on_new_workspace)

    act_close_workspace = QtGui.QAction("Close Window", mw)
    act_close_workspace.triggered.connect(mw._on_close_workspace)

    # Developer actions
    act_toggle_debug_tooltips = QtGui.QAction("GUI Debug Tooltips", mw)
    act_toggle_debug_tooltips.setCheckable(True)
    act_toggle_debug_tooltips.setChecked(False)
    act_toggle_debug_tooltips.triggered.connect(mw._on_toggle_debug_tooltips)

    # Help actions
    act_about = QtGui.QAction("About OM", mw)
    act_about.triggered.connect(mw._show_about_dialog)

    # Calculation Engine actions
    act_engine_python = QtGui.QAction("Python", mw)
    act_engine_python.setCheckable(True)
    act_engine_python.triggered.connect(lambda: mw._on_engine_changed("python"))

    act_engine_remote = QtGui.QAction("Remote", mw)
    act_engine_remote.setCheckable(True)
    act_engine_remote.triggered.connect(lambda: mw._on_engine_changed("remote"))

    engine_action_group = QtGui.QActionGroup(mw)
    engine_action_group.addAction(act_engine_python)
    engine_action_group.addAction(act_engine_remote)
    engine_action_group.setExclusive(True)

    # Set initial checked state based on loaded preference
    preferred = getattr(mw, '_preferred_engine', 'python')
    act_engine_python.setChecked(preferred == "python")
    act_engine_remote.setChecked(preferred == "remote")

    # Tools actions
    act_options = QtGui.QAction("Options…", mw)
    act_options.triggered.connect(mw._on_show_options)

    return MainWindowActions(
        act_new=act_new,
        act_open=act_open,
        act_import_script=act_import_script,
        act_save=act_save,
        act_quit=act_quit,
        act_undo=act_undo,
        act_redo=act_redo,
        act_copy=act_copy,
        act_paste=act_paste,
        act_paste_as_new_cube=act_paste_as_new_cube,
        act_convert_selection_to_dimension_labels=act_convert_selection_to_dimension_labels,
        act_assign_item_labels_from_selection=act_assign_item_labels_from_selection,
        act_delete_selected_dimension_items=act_delete_selected_dimension_items,
        act_clear_override=act_clear_override,
        act_set_rule_body=act_set_rule_body,
        act_focus_rule_input_bar=act_focus_rule_input_bar,
        act_create_dimension=act_create_dimension,
        act_create_dimension_item=act_create_dimension_item,
        act_delete_dimension=act_delete_dimension,
        act_attach_dimension_to_cube=act_attach_dimension_to_cube,
        act_edit_view_axes=act_edit_view_axes,
        act_create_cube=act_create_cube,
        act_delete_cube=act_delete_cube,
        act_add_view=act_add_view,
        act_recalc=act_recalc,
        act_recalc_visible=act_recalc_visible,
        act_toolbox_editor=act_toolbox_editor,
        act_new_workspace=act_new_workspace,
        act_close_workspace=act_close_workspace,
        act_about=act_about,
        act_engine_python=act_engine_python,
        act_engine_remote=act_engine_remote,
        engine_action_group=engine_action_group,
        act_toggle_debug_tooltips=act_toggle_debug_tooltips,
        act_options=act_options,
    )
