"""
Bootstrap module - Registers default commands on startup.

Call register_default_commands() when initializing the application.
"""

from typing import Any, Optional
from .registry import CommandRegistry, CommandCategory, get_registry
from .executor import get_executor
from .command_service import CommandService
from ..commands import (
    cmd_set, cmd_navigate, cmd_create, cmd_delete,
    cmd_recalc, cmd_save, cmd_load, cmd_quit,
    cmd_rule, cmd_query,
    cmd_delete_dimension_items,
    cmd_cancel_recalc, cmd_set_dependency_tracking, cmd_clear_cache,
    cmd_undo, cmd_redo,
    cmd_checkpoint, cmd_restore,
    cmd_rename_checkpoint, cmd_delete_checkpoint,
    cmd_profiler_register, cmd_profiler_unregister,
    cmd_profile_gui, cmd_profiler_report,
)
from ..commands.model import (
    cmd_clear_dimension_outline,
    cmd_set_dimension_outline,
    cmd_ensure_group_in_graph,
    cmd_resolve_item_node_id,
    cmd_set_dimension_item_order,
)
from ..commands.navigation import (
    cmd_get_selection,
)
from ..commands.view_state import (
    cmd_set_active_view,
    cmd_move_selection,
    cmd_set_selection as _cmd_set_selection_v2,
)
from ..commands.rule import (
    cmd_apply_rule_batch,
    cmd_delete_rule,
    cmd_update_rule,
    cmd_set_rule_order,
    cmd_set_rule,
    cmd_list_rules,
)
from ..commands.system import (
    cmd_run_recalculation,
    cmd_cancel_recalculation,
    cmd_cancel_operation,
    cmd_save_workspace,
    cmd_load_workspace,
)
from ..commands.set_variable import cmd_set_variable
from ..commands.timeline import (
    cmd_create_checkpoint,
    cmd_restore_checkpoint,
)
from ..commands.udf_commands import (
    cmd_create_udf,
    cmd_delete_udf,
)
from ..commands.import_commands import (
    cmd_run_excel_import,
)


# Repository-tracked lifecycle metadata for deprecated aliases.
# Each retained alias must have: replacement, deprecated_since, remove_after, reason.
# This is the source of truth for the catalog/registry consistency check.
ALIAS_LIFECYCLE = {
    "recalc": {"replacement": "run_recalculation", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "abbreviated legacy alias"},
    "cancel_recalc": {"replacement": "cancel_recalculation", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "abbreviated legacy alias"},
    "save": {"replacement": "save_workspace", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "object-omitting legacy alias"},
    "load": {"replacement": "load_workspace", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "object-omitting legacy alias"},
    "checkpoint": {"replacement": "create_checkpoint", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "object-omitting legacy alias"},
    "restore": {"replacement": "restore_checkpoint", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "object-omitting legacy alias"},
    "rule": {"replacement": "set_rule", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "noun-only command replaced by verb-first set_rule"},
    "set_cell_value": {"replacement": "set_cell_hardvalue", "deprecated_since": "2026-06-12", "remove_after": "2026-08", "reason": "hardvalue/rule separation"},
    "clear_cell_value": {"replacement": "clear_cell_hardvalue", "deprecated_since": "2026-06-12", "remove_after": "2026-08", "reason": "hardvalue/rule separation"},
    "set_cell": {"replacement": "set_cell_hardvalue", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "row/col index addressing superseded by cell_ref"},
    "set_cell_by_keys": {"replacement": "set_cell_hardvalue", "deprecated_since": "2026-06-12", "remove_after": "2026-08", "reason": "addressing mode belongs in cell_ref"},
    "clear_cell": {"replacement": "clear_cell_hardvalue", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "row/col index addressing superseded by cell_ref"},
    "clear_cell_by_keys": {"replacement": "clear_cell_hardvalue", "deprecated_since": "2026-06-12", "remove_after": "2026-08", "reason": "addressing mode belongs in cell_ref"},
    "set_cell_rule_by_keys": {"replacement": "set_rule_anchored", "deprecated_since": "2026-06-12", "remove_after": "2026-08", "reason": "addressing mode belongs in cell_ref"},
    "delete_cell_rule_by_keys": {"replacement": "delete_rule_anchored", "deprecated_since": "2026-06-12", "remove_after": "2026-08", "reason": "addressing mode belongs in cell_ref"},
    "delete_cell_rule": {"replacement": "delete_rule", "deprecated_since": "2026-06-21", "remove_after": "2026-09", "reason": "rule surface consolidation"},
    "add_dimension_item": {"replacement": "create_dimension_item", "deprecated_since": "2026-06-12", "remove_after": "2026-08", "reason": "add_* is not canonical per a-06"},
    "add_aggregate_item": {"replacement": "create_aggregate_item", "deprecated_since": "2026-06-12", "remove_after": "2026-08", "reason": "add_* is not canonical per a-06"},
    "group_create": {"replacement": "create_group", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "family-prefix style, not verb-first"},
    "group_add_items": {"replacement": "move_items_to_group", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "add is ambiguous; use move or create"},
    "group_remove_items": {"replacement": "move_items_to_group", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "family-prefix style; action is moving to root"},
    "group_remove": {"replacement": "delete_group", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "family-prefix style, not verb-first"},
    "navigate": {"replacement": "move_selection", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "navigation command replaced by view-state move"},
    "get_selection": {"replacement": "selection_current query", "deprecated_since": "2026-06-07", "remove_after": "2026-09", "reason": "superseded by query spine"},
}


def register_default_commands(registry: CommandRegistry | None = None) -> CommandRegistry:
    """
    Register all default commands with the registry.

    Call this once at application startup:
        from lib_command import register_default_commands
        register_default_commands()

    Safe to call multiple times - already registered commands are skipped.

    Returns:
        The populated registry
    """
    if registry is None:
        registry = get_registry()

    # === Scoped Property Set Command (replaces bare verb 'set') ===
    if not registry.is_registered("set_property"):
        registry.register(
            "set_property",
            "Set Property",
            CommandCategory.SYSTEM,
            cmd_set,
            description="Set a property on a target (cell, selection, view, model, session)",
            params={"target": str, "property": str, "value": object}
        )

    # === COMPATIBILITY ONLY — zero confirmed callers, kept for old macros/scripts ===
    # Phase 7B: deprecated handlers are quarantined here.
    # They do not touch GUI and do not mutate Engine.active_view_id.
    # Removal requires confirming zero external callers (macros, recorded sessions).

    # DEPRECATED: navigate is superseded by move_selection (view_state.py).
    # Zero confirmed callers as of Phase 7B audit.
    if not registry.is_registered("navigate"):
        registry.register(
            "navigate",
            "Navigate",
            CommandCategory.NAVIGATION,
            cmd_navigate,
            shortcut="Arrow Keys",
            description="Navigate in a direction (deprecated — use move_selection)",
            params={"direction": str, "amount": int}
        )

    # DEPRECATED: get_selection is superseded by query("selection_current").
    # Zero confirmed callers as of Phase 7B audit.
    if not registry.is_registered("get_selection"):
        registry.register(
            "get_selection",
            "Get Selection",
            CommandCategory.NAVIGATION,
            cmd_get_selection,
            description="Get current selection info (deprecated — use selection_current query)",
            needs_context=True,
        )

    if not registry.is_registered("set_selection"):
        registry.register(
            "set_selection",
            "Set Selection",
            CommandCategory.NAVIGATION,
            _cmd_set_selection_v2,
            description="Set grid selection to coordinates",
            params={"row": int, "col": int},
            needs_context=True,
        )

    # === View-State Commands (SessionStore-based) ===
    if not registry.is_registered("set_active_view"):
        registry.register(
            "set_active_view",
            "Set Active View",
            CommandCategory.NAVIGATION,
            cmd_set_active_view,
            description="Set the active view for this session",
            params={"view_id": str},
            needs_context=True,
        )

    if not registry.is_registered("move_selection"):
        registry.register(
            "move_selection",
            "Move Selection",
            CommandCategory.NAVIGATION,
            cmd_move_selection,
            description="Move selection in a direction",
            params={"direction": str, "amount": int},
            needs_context=True,
        )

    # === CRUD Commands ===
    # NOTE: bare verbs 'create' and 'delete' removed from canonical registry.
    # Use scoped commands: create_dimension, create_cube, create_dimension_item,
    # delete_dimension, delete_cube, delete_dimension_items, delete_rule, etc.

    # === System Commands ===
    # Canonical runtime/system commands
    if not registry.is_registered("run_recalculation"):
        registry.register(
            "run_recalculation",
            "Recalculate",
            CommandCategory.CALCULATION,
            cmd_run_recalculation,
            shortcut="F9",
            description="Recalculate the model",
            params={"scope": str}
        )

    if not registry.is_registered("cancel_operation"):
        registry.register(
            "cancel_operation",
            "Cancel Operation",
            CommandCategory.CALCULATION,
            cmd_cancel_operation,
            shortcut="Esc",
            description="Cancel an in-progress mutation, load, or recalculation",
        )

    if not registry.is_registered("cancel_recalculation"):
        registry.register(
            "cancel_recalculation",
            "Cancel Recalculation",
            CommandCategory.CALCULATION,
            cmd_cancel_recalculation,
            description="Legacy alias for cancel_operation",
        )

    if not registry.is_registered("save_workspace"):
        registry.register(
            "save_workspace",
            "Save Workspace",
            CommandCategory.SYSTEM,
            cmd_save_workspace,
            shortcut="Ctrl+S",
            description="Save the workspace",
            params={"path": str}
        )

    if not registry.is_registered("load_workspace"):
        registry.register(
            "load_workspace",
            "Load Workspace",
            CommandCategory.SYSTEM,
            cmd_load_workspace,
            shortcut="Ctrl+O",
            description="Load a workspace",
            params={"path": str}
        )

    if not registry.is_registered("create_new_workspace"):
        from ..commands.system import cmd_create_new_workspace
        registry.register(
            "create_new_workspace",
            "Create New Workspace",
            CommandCategory.SYSTEM,
            cmd_create_new_workspace,
            description="Create a new demo workspace",
            params={}
        )

    if not registry.is_registered("create_checkpoint"):
        registry.register(
            "create_checkpoint",
            "Create Checkpoint",
            CommandCategory.SYSTEM,
            cmd_create_checkpoint,
            description="Create a timeline checkpoint snapshot",
            params={"description": str}
        )

    if not registry.is_registered("restore_checkpoint"):
        registry.register(
            "restore_checkpoint",
            "Restore Checkpoint",
            CommandCategory.SYSTEM,
            cmd_restore_checkpoint,
            description="Restore workspace to a timeline snapshot",
            params={"snapshot_id": str}
        )

    if not registry.is_registered("rename_checkpoint"):
        registry.register(
            "rename_checkpoint",
            "Rename Checkpoint",
            CommandCategory.SYSTEM,
            cmd_rename_checkpoint,
            description="Rename a timeline checkpoint",
            params={"checkpoint_id": str, "description": str}
        )

    if not registry.is_registered("delete_checkpoint"):
        registry.register(
            "delete_checkpoint",
            "Delete Checkpoint",
            CommandCategory.SYSTEM,
            cmd_delete_checkpoint,
            description="Delete a timeline checkpoint",
            params={"checkpoint_id": str}
        )

    # Legacy aliases (same handler, mapped for lifecycle normalization)
    if not registry.is_registered("recalc"):
        registry.register(
            "recalc",
            "Recalculate",
            CommandCategory.CALCULATION,
            cmd_run_recalculation,
            shortcut="F9",
            description="Recalculate the model (legacy alias)",
            params={"scope": str}
        )

    if not registry.is_registered("cancel_recalc"):
        registry.register(
            "cancel_recalc",
            "Cancel Recalculation",
            CommandCategory.CALCULATION,
            cmd_cancel_recalculation,
            description="Cancel an in-progress recalculation (legacy alias)",
        )

    if not registry.is_registered("save"):
        registry.register(
            "save",
            "Save",
            CommandCategory.SYSTEM,
            cmd_save_workspace,
            shortcut="Ctrl+S",
            description="Save the workspace (legacy alias)",
            params={"path": str}
        )

    if not registry.is_registered("load"):
        registry.register(
            "load",
            "Load",
            CommandCategory.SYSTEM,
            cmd_load_workspace,
            shortcut="Ctrl+O",
            description="Load a workspace (legacy alias)",
            params={"path": str}
        )

    if not registry.is_registered("checkpoint"):
        registry.register(
            "checkpoint",
            "Create Checkpoint",
            CommandCategory.SYSTEM,
            cmd_create_checkpoint,
            description="Create a timeline checkpoint snapshot (legacy alias)",
            params={"description": str}
        )

    if not registry.is_registered("restore"):
        registry.register(
            "restore",
            "Restore Snapshot",
            CommandCategory.SYSTEM,
            cmd_restore_checkpoint,
            description="Restore workspace to a timeline snapshot (legacy alias)",
            params={"snapshot_id": str}
        )

    if not registry.is_registered("set_dependency_tracking"):
        registry.register(
            "set_dependency_tracking",
            "Set Dependency Tracking",
            CommandCategory.SYSTEM,
            cmd_set_dependency_tracking,
            description="Enable or disable dependency tracking",
            params={"enabled": bool}
        )

    if not registry.is_registered("set_multithread_recompute"):
        from ..commands.system import cmd_set_multithread_recompute
        registry.register(
            "set_multithread_recompute",
            "Set Multithread Recompute",
            CommandCategory.SYSTEM,
            cmd_set_multithread_recompute,
            description="Enable or disable multithreaded recalculation",
            params={"enabled": bool}
        )

    if not registry.is_registered("clear_profiler_snapshot"):
        from ..commands.system import cmd_clear_profiler_snapshot
        registry.register(
            "clear_profiler_snapshot",
            "Clear Profiler Snapshot",
            CommandCategory.SYSTEM,
            cmd_clear_profiler_snapshot,
            description="Reset profiler counters (dependency metrics and rule evaluation profile)",
            params={}
        )

    if not registry.is_registered("clear_cache"):
        from ..commands.system import cmd_clear_cache
        registry.register(
            "clear_cache",
            "Clear Cache",
            CommandCategory.SYSTEM,
            cmd_clear_cache,
            description="Clear internal evaluation caches",
            params={"scope": str}
        )

    if not registry.is_registered("undo"):
        from ..commands.system import cmd_undo
        registry.register(
            "undo",
            "Undo",
            CommandCategory.SYSTEM,
            cmd_undo,
            shortcut="Ctrl+Z",
            description="Undo the last action",
            params={}
        )

    if not registry.is_registered("redo"):
        from ..commands.system import cmd_redo
        registry.register(
            "redo",
            "Redo",
            CommandCategory.SYSTEM,
            cmd_redo,
            shortcut="Ctrl+Y",
            description="Redo the last undone action",
            params={}
        )

    if not registry.is_registered("set_engine"):
        from ..commands.system import cmd_set_engine
        registry.register(
            "set_engine",
            "Set Engine",
            CommandCategory.SYSTEM,
            cmd_set_engine,
            description="Switch the calculation engine for the current workspace",
            params={"engine_type": str}
        )

    # === UDF Commands ===
    if not registry.is_registered("create_udf"):
        registry.register(
            "create_udf",
            "Create UDF",
            CommandCategory.CALCULATION,
            cmd_create_udf,
            description="Register a user-defined function",
            params={"name": str, "params": list, "expression": str}
        )

    if not registry.is_registered("delete_udf"):
        registry.register(
            "delete_udf",
            "Delete UDF",
            CommandCategory.CALCULATION,
            cmd_delete_udf,
            description="Remove a user-defined function",
            params={"name": str}
        )

    if not registry.is_registered("run_excel_import"):
        registry.register(
            "run_excel_import",
            "Run Excel Import",
            CommandCategory.SYSTEM,
            cmd_run_excel_import,
            description="Import an Excel file into the current workspace",
            params={"path": str}
        )

    if not registry.is_registered("delete_dimension_items"):
        registry.register(
            "delete_dimension_items",
            "Delete Dimension Items",
            CommandCategory.MODEL,
            cmd_delete_dimension_items,
            description="Delete specific items from a dimension",
            params={"dim_id": str, "item_ids": list}
        )

    # === Graph Mutation Commands (Phase 8 adapters) ===
    from ..commands.handlers import (
        handle_rename_group_node_adapter,
        handle_delete_group_node_adapter,
        handle_move_nodes_adapter,
        handle_add_aggregate_item_adapter,
        handle_place_item_nodes_adapter,
        handle_rename_dimension_item_adapter,
        handle_rename_cube_adapter,
        handle_rename_dimension_adapter,
        handle_rename_view_adapter,
        handle_delete_view_adapter,
        handle_detach_dimension_from_cube_adapter,
        handle_create_dimension_item_adapter,
        handle_create_group_adapter,
        handle_move_items_to_group_adapter,
        handle_ungroup_items_adapter,
        cmd_create_dimension_item,
        cmd_create_dimension_items_batch,
        cmd_create_aggregate_item,
    )

    _GRAPH_MUTATIONS = [
        ("rename_group_node", "Rename Group Node", handle_rename_group_node_adapter,
         {"dim_id": str, "node_id": str, "new_label": str}),
        ("delete_group_node", "Delete Group Node", handle_delete_group_node_adapter,
         {"dim_id": str, "node_id": str}),
        ("move_nodes", "Move Nodes", handle_move_nodes_adapter,
         {"dim_id": str, "node_ids": list, "parent_node_id": object}),
        ("place_item_nodes", "Place Item Nodes", handle_place_item_nodes_adapter,
         {"dim_id": str, "item_ids": list, "parent_node_id": object}),
        ("rename_dimension_item", "Rename Dimension Item", handle_rename_dimension_item_adapter,
         {"dim_id": str, "item_id": str, "new_name": str}),
        ("rename_cube", "Rename Cube", handle_rename_cube_adapter,
         {"cube_id": str, "new_name": str}),
        ("rename_dimension", "Rename Dimension", handle_rename_dimension_adapter,
         {"dim_id": str, "new_name": str}),
        ("rename_view", "Rename View", handle_rename_view_adapter,
         {"view_id": str, "new_name": str}),
        ("delete_view", "Delete View", handle_delete_view_adapter,
         {"view_id": str}),
        ("detach_dimension_from_cube", "Detach Dimension From Cube", handle_detach_dimension_from_cube_adapter,
         {"cube_id": str, "dim_id": str}),
        ("create_group", "Create Group", handle_create_group_adapter,
         {"dim_id": str, "label": str}),
        ("ungroup_items", "Ungroup Items", handle_ungroup_items_adapter,
         {"dim_id": str, "item_ids": list}),
    ]

    for cmd_id, name, handler, params in _GRAPH_MUTATIONS:
        if not registry.is_registered(cmd_id):
            registry.register(
                cmd_id,
                name,
                CommandCategory.MODEL,
                handler,
                description=name,
                params=params,
            )

    # Canonical commands with clean naming
    if not registry.is_registered("create_dimension_item"):
        registry.register(
            "create_dimension_item",
            "Create Dimension Item",
            CommandCategory.MODEL,
            cmd_create_dimension_item,
            description="Create a new dimension item",
            params={"dim_id": str, "name": str},
        )

    if not registry.is_registered("create_dimension_items_batch"):
        registry.register(
            "create_dimension_items_batch",
            "Create Dimension Items Batch",
            CommandCategory.MODEL,
            cmd_create_dimension_items_batch,
            description="Batch-create many dimension items with a single event",
            params={"entries": list},
        )

    if not registry.is_registered("clear_dimension_outline"):
        registry.register(
            "clear_dimension_outline",
            "Clear Dimension Outline",
            CommandCategory.MODEL,
            cmd_clear_dimension_outline,
            description="Clear a dimension's outline",
            params={"dim_id": str},
        )

    if not registry.is_registered("set_dimension_outline"):
        registry.register(
            "set_dimension_outline",
            "Set Dimension Outline",
            CommandCategory.MODEL,
            cmd_set_dimension_outline,
            description="Set a dimension's outline",
            params={"dim_id": str, "outline": list | None},
        )

    if not registry.is_registered("ensure_group_in_graph"):
        registry.register(
            "ensure_group_in_graph",
            "Ensure Group in Graph",
            CommandCategory.MODEL,
            cmd_ensure_group_in_graph,
            description="Ensure a group node exists in the dimension graph",
            params={"dim_id": str, "label": str, "children": list | None, "parent_group_id": str | None},
        )

    if not registry.is_registered("resolve_item_node_id"):
        registry.register(
            "resolve_item_node_id",
            "Resolve Item Node ID",
            CommandCategory.MODEL,
            cmd_resolve_item_node_id,
            description="Resolve an item_id to its ITEM_REF node_id, creating if absent",
            params={"dim_id": str, "item_id": str},
        )

    if not registry.is_registered("set_dimension_item_order"):
        registry.register(
            "set_dimension_item_order",
            "Set Dimension Item Order",
            CommandCategory.MODEL,
            cmd_set_dimension_item_order,
            description="Replace the flat dimension item order exactly with item_ids",
            params={"dim_id": str, "item_ids": list},
        )

    if not registry.is_registered("create_aggregate_item"):
        registry.register(
            "create_aggregate_item",
            "Create Aggregate Item",
            CommandCategory.MODEL,
            cmd_create_aggregate_item,
            description="Create an aggregate item under a group",
            params={"dim_id": str, "group_node_id": str, "name": str},
        )

    # Legacy aliases
    if not registry.is_registered("add_dimension_item"):
        registry.register(
            "add_dimension_item",
            "Add Dimension Item",
            CommandCategory.MODEL,
            cmd_create_dimension_item,
            description="Create a new dimension item (legacy alias)",
            params={"dim_id": str, "name": str},
        )

    if not registry.is_registered("add_aggregate_item"):
        registry.register(
            "add_aggregate_item",
            "Add Aggregate Item",
            CommandCategory.MODEL,
            cmd_create_aggregate_item,
            description="Create an aggregate item under a group (legacy alias)",
            params={"dim_id": str, "group_node_id": str, "name": str},
        )

    # === Group Commands ===
    from lib_command.commands.group import (
        cmd_group_create,
        cmd_group_add_items,
        cmd_group_remove_items,
        cmd_group_remove,
        cmd_create_group,
        cmd_delete_group,
        cmd_move_items_to_group,
    )

    # Canonical group commands
    if not registry.is_registered("create_group"):
        registry.register(
            "create_group",
            "Create Group",
            CommandCategory.MODEL,
            cmd_create_group,
            description="Create a new group in a dimension",
            params={"dim_id": str, "label": str, "parent_group_node_id": str, "order": int}
        )

    if not registry.is_registered("delete_group"):
        registry.register(
            "delete_group",
            "Delete Group",
            CommandCategory.MODEL,
            cmd_delete_group,
            description="Delete a group node",
            params={"dim_id": str, "group_node_id": str, "cascade": bool}
        )

    if not registry.is_registered("move_items_to_group"):
        registry.register(
            "move_items_to_group",
            "Move Items to Group",
            CommandCategory.MODEL,
            cmd_move_items_to_group,
            description="Move dimension items to a group",
            params={"dim_id": str, "item_ids": list},
        )

    # Legacy aliases
    if not registry.is_registered("group_create"):
        registry.register(
            "group_create",
            "Create Group",
            CommandCategory.MODEL,
            cmd_create_group,
            description="Create a new group in a dimension (legacy alias)",
            params={"dim_id": str, "label": str, "parent_group_node_id": str, "order": int}
        )
    if not registry.is_registered("group_add_items"):
        registry.register(
            "group_add_items",
            "Add Items to Group",
            CommandCategory.MODEL,
            cmd_group_add_items,
            description="Add dimension items to a group (legacy alias)",
            params={"dim_id": str, "item_ids": list, "group_node_id": str, "order": int}
        )
    if not registry.is_registered("group_remove_items"):
        registry.register(
            "group_remove_items",
            "Remove Items from Group",
            CommandCategory.MODEL,
            cmd_group_remove_items,
            description="Remove dimension items from a group (legacy alias)",
            params={"dim_id": str, "item_ids": list, "group_node_id": str}
        )
    if not registry.is_registered("group_remove"):
        registry.register(
            "group_remove",
            "Remove Group",
            CommandCategory.MODEL,
            cmd_delete_group,
            description="Remove a group node (legacy alias)",
            params={"dim_id": str, "group_node_id": str, "cascade": bool}
        )

    if not registry.is_registered("quit"):
        registry.register(
            "quit",
            "Quit",
            CommandCategory.SYSTEM,
            cmd_quit,
            shortcut="Ctrl+Q",
            description="Quit the application"
        )

    # === Phase 1A: Cell Value Commands ===
    from lib_command.commands.cell_values import (
        cmd_set_cell,
        cmd_set_cell_by_keys,
        cmd_clear_cell,
        cmd_clear_cell_by_keys,
        cmd_set_range_values,
        cmd_set_cell_value,
        cmd_clear_cell_value,
        cmd_set_cell_hardvalue,
        cmd_clear_cell_hardvalue,
        cmd_update_cell_rule,
        cmd_delete_cell_rule,
        cmd_set_page_item_id,
        cmd_delete_cell_rule_by_keys,
        cmd_set_cell_rule_by_keys,
        cmd_set_rule_anchored,
        cmd_delete_rule_anchored,
        cmd_attach_dimension_to_cube,
        cmd_set_view_axes,
        cmd_delete_cube,
        cmd_delete_dimension,
        cmd_set_cell_hardvalue_by_addr,
        cmd_set_cell_hardvalues_batch_by_addr,
    )

    # Canonical cell value commands (hardvalue / rule separation)
    if not registry.is_registered("set_cell_hardvalue"):
        registry.register(
            "set_cell_hardvalue",
            "Set Cell Hardvalue",
            CommandCategory.DATA,
            cmd_set_cell_hardvalue,
            description="Set a user hardvalue that overrides rule computation",
            params={"view_id": str, "cell_ref": dict, "value": object},
        )

    if not registry.is_registered("clear_cell_hardvalue"):
        registry.register(
            "clear_cell_hardvalue",
            "Clear Cell Hardvalue",
            CommandCategory.DATA,
            cmd_clear_cell_hardvalue,
            description="Clear the user hardvalue, revealing the rule-computed value",
            params={"view_id": str, "cell_ref": dict},
        )

    if not registry.is_registered("set_rule_anchored"):
        registry.register(
            "set_rule_anchored",
            "Set Rule Anchored",
            CommandCategory.CALCULATION,
            cmd_set_rule_anchored,
            description="Attach an anchored rule to a specific cell",
            params={"view_id": str, "cell_ref": dict, "expression": str},
        )

    if not registry.is_registered("delete_rule_anchored"):
        registry.register(
            "delete_rule_anchored",
            "Delete Rule Anchored",
            CommandCategory.CALCULATION,
            cmd_delete_rule_anchored,
            description="Delete the rule anchored at a specific cell",
            params={"view_id": str, "cell_ref": dict},
        )

    # Transitional aliases
    if not registry.is_registered("set_cell_value"):
        registry.register(
            "set_cell_value",
            "Set Cell Value",
            CommandCategory.DATA,
            cmd_set_cell_value,
            description="Deprecated alias for set_cell_hardvalue",
            params={"view_id": str, "cell_ref": dict, "value": object},
        )

    if not registry.is_registered("clear_cell_value"):
        registry.register(
            "clear_cell_value",
            "Clear Cell Value",
            CommandCategory.DATA,
            cmd_clear_cell_value,
            description="Deprecated alias for clear_cell_hardvalue",
            params={"view_id": str, "cell_ref": dict},
        )

    # Legacy aliases (index and keys variants)
    if not registry.is_registered("set_cell"):
        registry.register(
            "set_cell",
            "Set Cell Value",
            CommandCategory.DATA,
            cmd_set_cell,
            description="Set a cell value by row/column indices (legacy alias)",
            params={"view_id": str, "row": int, "col": int, "value": object},
        )

    if not registry.is_registered("set_cell_by_keys"):
        registry.register(
            "set_cell_by_keys",
            "Set Cell Value by Keys",
            CommandCategory.DATA,
            cmd_set_cell_by_keys,
            description="Deprecated alias for set_cell_hardvalue (kind='ids')",
            params={"view_id": str, "row_key": tuple, "col_key": tuple, "value": object},
        )

    if not registry.is_registered("clear_cell"):
        registry.register(
            "clear_cell",
            "Clear Cell Value",
            CommandCategory.DATA,
            cmd_clear_cell,
            description="Clear a cell value by row/column indices (legacy alias)",
            params={"view_id": str, "row": int, "col": int},
        )

    if not registry.is_registered("clear_cell_by_keys"):
        registry.register(
            "clear_cell_by_keys",
            "Clear Cell Value by Keys",
            CommandCategory.DATA,
            cmd_clear_cell_by_keys,
            description="Deprecated alias for clear_cell_hardvalue (kind='ids')",
            params={"view_id": str, "row_key": tuple, "col_key": tuple},
        )

    # === Phase 1A.1: Cell Rule Commands ===
    if not registry.is_registered("update_cell_rule"):
        registry.register(
            "update_cell_rule",
            "Update Cell Rule",
            CommandCategory.DATA,
            cmd_update_cell_rule,
            description="Update a cell rule expression by ID",
            params={"rule_id": str, "expression": str},
        )

    if not registry.is_registered("delete_cell_rule"):
        registry.register(
            "delete_cell_rule",
            "Delete Cell Rule",
            CommandCategory.DATA,
            cmd_delete_cell_rule,
            description="Delete a cell rule by ID (delegates to canonical delete_rule)",
            params={"rule_id": str},
        )

    if not registry.is_registered("set_page_item_id"):
        registry.register(
            "set_page_item_id",
            "Set Page Item ID",
            CommandCategory.DATA,
            cmd_set_page_item_id,
            description="Set the active page item for a dimension in a view",
            params={"view_id": str, "dim_id": str, "item_id": str},
        )

    if not registry.is_registered("delete_cell_rule_by_keys"):
        registry.register(
            "delete_cell_rule_by_keys",
            "Delete Cell Rule by Keys",
            CommandCategory.DATA,
            cmd_delete_cell_rule_by_keys,
            description="Deprecated alias for delete_rule_anchored (kind='ids')",
            params={"view_id": str, "row_key": tuple, "col_key": tuple},
        )

    if not registry.is_registered("set_cell_rule_by_keys"):
        registry.register(
            "set_cell_rule_by_keys",
            "Set Cell Rule by Keys",
            CommandCategory.DATA,
            cmd_set_cell_rule_by_keys,
            description="Deprecated alias for set_rule_anchored (kind='ids')",
            params={"view_id": str, "row_key": tuple, "col_key": tuple, "expression": str},
        )

    if not registry.is_registered("attach_dimension_to_cube"):
        registry.register(
            "attach_dimension_to_cube",
            "Attach Dimension to Cube",
            CommandCategory.DATA,
            cmd_attach_dimension_to_cube,
            description="Attach a dimension to a cube",
            params={"cube_id": str, "dim_id": str, "default_item_id": object},
        )

    if not registry.is_registered("set_view_axes"):
        registry.register(
            "set_view_axes",
            "Set View Axes",
            CommandCategory.DATA,
            cmd_set_view_axes,
            description="Set the row and column dimension IDs for a view",
            params={"view_id": str, "row_dimension_id": str, "col_dimension_id": str},
        )

    if not registry.is_registered("delete_cube"):
        registry.register(
            "delete_cube",
            "Delete Cube",
            CommandCategory.DATA,
            cmd_delete_cube,
            description="Delete a cube and all views that reference it",
            params={"cube_id": str},
        )

    if not registry.is_registered("delete_dimension"):
        registry.register(
            "delete_dimension",
            "Delete Dimension",
            CommandCategory.DATA,
            cmd_delete_dimension,
            description="Delete a dimension, detaching it from all cubes and views first",
            params={"dim_id": str},
        )

    if not registry.is_registered("set_cell_hardvalue_by_addr"):
        registry.register(
            "set_cell_hardvalue_by_addr",
            "Set Cell Hardvalue By Address",
            CommandCategory.DATA,
            cmd_set_cell_hardvalue_by_addr,
            description="Set a user hardvalue using a full cube address tuple",
            params={"cube_id": str, "addr": list, "value": object},
        )

    if not registry.is_registered("set_cell_hardvalues_batch_by_addr"):
        registry.register(
            "set_cell_hardvalues_batch_by_addr",
            "Set Cell Hardvalues Batch By Address",
            CommandCategory.DATA,
            cmd_set_cell_hardvalues_batch_by_addr,
            description="Set many cell hardvalues in one call with a single undo group",
            params={"cube_id": str, "entries": list},
        )

    # === Phase 1C: Range / Paste Command ===
    if not registry.is_registered("set_range_values"):
        registry.register(
            "set_range_values",
            "Set Range Values",
            CommandCategory.DATA,
            cmd_set_range_values,
            description="Set a rectangular range of cell values",
            params={"view_id": str, "top": int, "left": int, "values": list},
        )

    # === Phase 1B: Rule Commands ===
    # Canonical
    if not registry.is_registered("set_rule"):
        registry.register(
            "set_rule",
            "Set Rule",
            CommandCategory.CALCULATION,
            cmd_set_rule,
            shortcut=None,
            description="Set a rule on a cube",
            params={"cube_id": str, "targets": list, "expression": str, "is_anchored": bool}
        )

    # Legacy alias
    if not registry.is_registered("rule"):
        registry.register(
            "rule",
            "Rule",
            CommandCategory.CALCULATION,
            cmd_set_rule,
            shortcut=None,
            description="Set a rule on a cube (legacy alias)",
            params={"cube_id": str, "targets": list, "expression": str, "is_anchored": bool}
        )

    if not registry.is_registered("apply_rule_batch"):
        registry.register(
            "apply_rule_batch",
            "Apply Rule Batch",
            CommandCategory.CALCULATION,
            cmd_apply_rule_batch,
            description="Apply multiple rules atomically (script/macro path)",
            params={"rules": list},
            needs_context=True,
        )

    if not registry.is_registered("delete_rule"):
        registry.register(
            "delete_rule",
            "Delete Rule",
            CommandCategory.CALCULATION,
            cmd_delete_rule,
            description="Delete a rule by ID",
            params={"rule_id": str},
        )

    if not registry.is_registered("update_rule"):
        registry.register(
            "update_rule",
            "Update Rule",
            CommandCategory.CALCULATION,
            cmd_update_rule,
            description="Update an existing rule's target and expression",
            params={"rule_id": str, "targets": list, "expression": str, "is_anchored": bool},
        )

    if not registry.is_registered("set_rule_order"):
        registry.register(
            "set_rule_order",
            "Set Rule Order",
            CommandCategory.CALCULATION,
            cmd_set_rule_order,
            description="Set the execution order of rules",
            params={"rule_ids": list},
        )

    if not registry.is_registered("list_rules"):
        registry.register(
            "list_rules",
            "List Rules",
            CommandCategory.CALCULATION,
            cmd_list_rules,
            description="List all rules for a cube",
            params={"cube_id": str},
            needs_context=True,
        )

    # === Phase 2: View Dimension Movement Commands ===
    from lib_command.commands.view_commands import (
        cmd_move_view_dimension,
        cmd_set_view_layout,
        cmd_set_view_col_width,
        cmd_set_view_row_header_width,
    )

    if not registry.is_registered("set_view_layout"):
        registry.register(
            "set_view_layout",
            "Set View Layout",
            CommandCategory.DATA,
            cmd_set_view_layout,
            description="Set the rows, cols, and page layout for a view",
            params={"view_id": str, "layout": dict},
        )

    if not registry.is_registered("move_view_dimension"):
        registry.register(
            "move_view_dimension",
            "Move View Dimension",
            CommandCategory.DATA,
            cmd_move_view_dimension,
            description="Move a dimension to a different axis (row, col, page) within a view",
            params={"view_id": str, "dim_id": str, "dest": str},
        )

    if not registry.is_registered("set_view_col_width"):
        registry.register(
            "set_view_col_width",
            "Set View Column Width",
            CommandCategory.VIEW,
            cmd_set_view_col_width,
            description="Set one persisted column width for a view",
            params={"view_id": str, "col_index": int, "width": int},
        )

    if not registry.is_registered("set_view_row_header_width"):
        registry.register(
            "set_view_row_header_width",
            "Set View Row Header Width",
            CommandCategory.VIEW,
            cmd_set_view_row_header_width,
            description="Set one persisted row-header width for a view",
            params={"view_id": str, "depth_or_index": int, "width": int},
        )

    # === Phase 3: Dimension / Cube / View Creation Commands ===
    from lib_command.commands.model import (
        cmd_create_dimension,
        cmd_create_cube,
        cmd_create_view,
    )

    if not registry.is_registered("create_dimension"):
        registry.register(
            "create_dimension",
            "Create Dimension",
            CommandCategory.MODEL,
            cmd_create_dimension,
            description="Create a new dimension",
            params={"name": str, "dim_type": str},
            record_policy="model_mutation",
        )

    if not registry.is_registered("create_cube"):
        registry.register(
            "create_cube",
            "Create Cube",
            CommandCategory.MODEL,
            cmd_create_cube,
            description="Create a new cube with given dimensions",
            params={"name": str, "dimension_ids": list},
        )

    if not registry.is_registered("create_view"):
        registry.register(
            "create_view",
            "Create View",
            CommandCategory.MODEL,
            cmd_create_view,
            description="Create a new view over a cube",
            params={"name": str, "cube_id": str},
        )

    # === Profiler Commands (Phase GUI profiler) ===
    if not registry.is_registered("register_profiler"):
        registry.register(
            "register_profiler",
            "Register Profiler",
            CommandCategory.SYSTEM,
            cmd_profiler_register,
            description="Register a GUI profiler endpoint ID",
            params={"endpoint_id": str},
        )

    if not registry.is_registered("unregister_profiler"):
        registry.register(
            "unregister_profiler",
            "Unregister Profiler",
            CommandCategory.SYSTEM,
            cmd_profiler_unregister,
            description="Unregister a GUI profiler endpoint ID",
            params={"endpoint_id": str},
        )

    if not registry.is_registered("profile_gui"):
        registry.register(
            "profile_gui",
            "Profile GUI",
            CommandCategory.SYSTEM,
            cmd_profile_gui,
            description="Request an on-demand profiling session from the GUI",
            params={"endpoint_id": str, "duration_seconds": float},
        )

    if not registry.is_registered("profiler_report"):
        registry.register(
            "profiler_report",
            "Profiler Report",
            CommandCategory.SYSTEM,
            cmd_profiler_report,
            description="Receive a profiler report snapshot from the GUI",
            params={"request_id": str, "snapshot": dict},
        )

    # === Alias lifecycle normalization ===
    executor = get_executor()
    # Executor alias map: only aliases whose lifecycle events should normalize
    # to the canonical command ID. Commands kept as direct registry entries
    # because their parameter shapes differ (set_cell, set_cell_by_keys, etc.)
    # are NOT listed here; they emit their own lifecycle events.
    _ALIASES = {
        "recalc": "run_recalculation",
        "cancel_recalc": "cancel_recalculation",
        "save": "save_workspace",
        "load": "load_workspace",
        "checkpoint": "create_checkpoint",
        "restore": "restore_checkpoint",
        "rule": "set_rule",
        "group_add_items": "move_items_to_group",
        "group_remove_items": "move_items_to_group",
        "group_create": "create_group",
        "group_remove": "delete_group",
        "add_dimension_item": "create_dimension_item",
        "add_aggregate_item": "create_aggregate_item",
    }

    # === Session/script commands (client-side, non-model) ===
    if not registry.is_registered("set_variable"):
        registry.register(
            "set_variable",
            "Set Script Variable",
            CommandCategory.SYSTEM,
            cmd_set_variable,
            description="Set a client-side scripting variable",
            params={"name": str, "value": object, "global_scope": bool},
            record_policy="session_replay",
        )

    for alias_id, canonical_id in _ALIASES.items():
        executor.add_alias(alias_id, canonical_id)

    # === Solver Commands ===
    if not registry.is_registered("run_solver"):
        from ..commands.solver_commands import cmd_run_solver
        registry.register(
            "run_solver",
            "Run Solver",
            CommandCategory.CALCULATION,
            cmd_run_solver,
            description="Run a solver optimization job asynchronously",
            params={"problem_spec": dict},
        )

    if not registry.is_registered("cancel_solver"):
        from ..commands.solver_commands import cmd_cancel_solver
        registry.register(
            "cancel_solver",
            "Cancel Solver",
            CommandCategory.CALCULATION,
            cmd_cancel_solver,
            description="Cancel a running solver job",
            params={"job_id": str},
        )

    if not registry.is_registered("apply_solver_result"):
        from ..commands.solver_commands import cmd_apply_solver_result
        registry.register(
            "apply_solver_result",
            "Apply Solver Result",
            CommandCategory.CALCULATION,
            cmd_apply_solver_result,
            description="Apply solver result values to the workspace",
            params={"job_id": str},
        )

    if not registry.is_registered("export_solver"):
        from ..commands.solver_commands import cmd_export_solver
        registry.register(
            "export_solver",
            "Export Solver Report",
            CommandCategory.CALCULATION,
            cmd_export_solver,
            description="Export a solver job diagnostic report as JSON",
            params={"job_id": str, "file_path": str},
        )

    return registry


# Global singleton for CommandService lifetime management
_command_service_singleton: Optional[CommandService] = None


def init_command_services(
    persistence_adapter: Optional[Any] = None,
) -> CommandService:
    """
    Initialize the command layer: register default commands and start CommandService.

    This is the preferred shared bootstrap entry point. Call it once per
    application lifecycle from the composition root (main.py, GUI app, REPL, etc.).
    It is safe to call multiple times; subsequent calls return the existing instance.
    If the bus was reset (e.g. in tests), CommandService is restarted on the new bus.
    """
    global _command_service_singleton

    # Register commands first (idempotent)
    register_default_commands()

    from .message_bus import get_message_bus
    current_bus = get_message_bus()

    # If the bus was reset, tear down the old CommandService so it restarts fresh
    if _command_service_singleton is not None and _command_service_singleton.bus is not current_bus:
        teardown_command_services()

    # Create CommandService once, then re-start it so it re-subscribes if the
    # bus was reset externally.
    if _command_service_singleton is None:
        _command_service_singleton = CommandService(persistence_adapter=persistence_adapter)

    # If the bus was reset externally (e.g. test fixtures), the subscription may
    # have been dropped. Re-starting re-subscribes when needed.
    _command_service_singleton.start()

    return _command_service_singleton


def teardown_command_services() -> None:
    """Stop CommandService. Call once on application teardown."""
    global _command_service_singleton
    if _command_service_singleton is not None:
        _command_service_singleton.stop()
        _command_service_singleton = None


# Convenience: Register format property commands (sugar over 'set')
def register_format_commands(registry: CommandRegistry | None = None) -> None:
    """Register convenience commands for common format operations."""
    if registry is None:
        registry = get_registry()

    # These are just wrappers around 'set' for common use cases
    def make_format_setter(property_path: str, shortcut: str | None = None):
        def setter(ctx, target="selection", value=True):
            return cmd_set(ctx, target, property_path, value)
        return setter

    format_props = [
        ("format.bold", "Bold", "Ctrl+B"),
        ("format.italic", "Italic", "Ctrl+I"),
        ("format.underline", "Underline", "Ctrl+U"),
    ]

    for prop_path, name, shortcut in format_props:
        if not registry.is_registered(prop_path):
            registry.register(
                prop_path,
                name,
                CommandCategory.FORMAT,
                make_format_setter(prop_path, shortcut),
                shortcut=shortcut,
                description=f"Toggle {name.lower()}",
                params={"target": str, "value": bool}
            )