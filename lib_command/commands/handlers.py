"""Command handlers for Phase 8 GUI mutations.

Each handler receives (command, engine, bus) and returns _HandlerResult.
Handlers publish domain events (event.*) on success.
Exceptions are NOT caught here — the dispatcher catches them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class _HandlerResult(Generic[T]):
    ok: bool
    data: T | None = None
    error: str | None = None
    exception: Exception | None = None


from lib_command.commands.graph_mutations import (
    RenameGroupNodeCommand,
    DeleteGroupNodeCommand,
    MoveNodesCommand,
    AddAggregateItemCommand,
    PlaceItemNodesCommand,
    RenameDimensionItemCommand,
    RenameCubeCommand,
    RenameDimensionCommand,
    RenameViewCommand,
    DeleteViewCommand,
    DetachDimensionFromCubeCommand,
    AddDimensionItemCommand,
    CreateGroupCommand,
)
from lib_command.events.domain_events import DimensionStructureChangedEvent
from lib_command.core.message_bus import get_message_bus
from lib_command.core.domain_event_publisher import publish_domain_event
from lib_openm._engine_core import AddAggregateItemResult


def handle_rename_group_node(
    cmd: RenameGroupNodeCommand, engine: Any, bus: Any, ctx: Any = None
) -> _HandlerResult[None]:
    engine.rename_group_node(cmd.dim_id, cmd.node_id, cmd.new_label)
    publish_domain_event(
        bus,
        "event.dimension.structure_changed",
        DimensionStructureChangedEvent(
            dim_id=cmd.dim_id,
            reason="rename_group_node",
            affected_node_ids=[cmd.node_id],
        ),
        correlation_id=getattr(ctx, "correlation_id", None),
        session_id=getattr(ctx, "session_id", None),
        causation_id=getattr(ctx, "command_message_id", None),
    )
    return _HandlerResult(ok=True)


def handle_delete_group_node(
    cmd: DeleteGroupNodeCommand, engine: Any, bus: Any, ctx: Any = None
) -> _HandlerResult[None]:
    engine.delete_group_node(cmd.dim_id, cmd.node_id, promote_children=cmd.promote_children)
    publish_domain_event(
        bus,
        "event.dimension.structure_changed",
        DimensionStructureChangedEvent(
            dim_id=cmd.dim_id,
            reason="delete_group_node",
            affected_node_ids=[cmd.node_id],
        ),
        correlation_id=getattr(ctx, "correlation_id", None),
        session_id=getattr(ctx, "session_id", None),
        causation_id=getattr(ctx, "command_message_id", None),
    )
    return _HandlerResult(ok=True)


def handle_move_nodes(
    cmd: MoveNodesCommand, engine: Any, bus: Any, ctx: Any = None
) -> _HandlerResult[None]:
    engine.move_nodes(
        cmd.dim_id,
        cmd.node_ids,
        cmd.parent_node_id,
        anchor_node_id=cmd.anchor_node_id,
        position=cmd.position,
        move_empty_parents=cmd.move_empty_parents,
    )
    publish_domain_event(
        bus,
        "event.dimension.structure_changed",
        DimensionStructureChangedEvent(
            dim_id=cmd.dim_id,
            reason="move_nodes",
            affected_node_ids=cmd.node_ids,
        ),
        correlation_id=getattr(ctx, "correlation_id", None),
        session_id=getattr(ctx, "session_id", None),
        causation_id=getattr(ctx, "command_message_id", None),
    )
    return _HandlerResult(ok=True)


def handle_add_aggregate_item(
    cmd: AddAggregateItemCommand, engine: Any, bus: Any, ctx: Any = None
) -> _HandlerResult[Any]:
    result = engine.create_aggregate_item(cmd.dim_id, cmd.group_node_id, cmd.name)
    if isinstance(result, dict):
        result = AddAggregateItemResult(
            item_id=result["item_id"],
            item_name=result.get("item_name", cmd.name),
            item_node_id=result["item_node_id"],
            aggregate_edge_id=result.get("aggregate_edge_id", result.get("edge_id")),
            group_node_id=result["group_node_id"],
        )
    publish_domain_event(
        bus,
        "event.dimension.structure_changed",
        DimensionStructureChangedEvent(
            dim_id=cmd.dim_id,
            reason="add_aggregate_item",
            affected_node_ids=[cmd.group_node_id, result.item_id],
        ),
        correlation_id=getattr(ctx, "correlation_id", None),
        session_id=getattr(ctx, "session_id", None),
        causation_id=getattr(ctx, "command_message_id", None),
    )
    return _HandlerResult(
        ok=True,
        data={
            "item_id": result.item_id,
            "item_name": result.item_name,
            "item_node_id": result.item_node_id,
            "group_node_id": result.group_node_id,
        },
    )


def handle_place_item_nodes(
    cmd: PlaceItemNodesCommand, engine: Any, bus: Any, ctx: Any = None
) -> _HandlerResult[list[str]]:
    node_ids = engine.place_item_nodes(
        cmd.dim_id,
        cmd.item_ids,
        parent_node_id=cmd.parent_node_id,
        anchor_node_id=cmd.anchor_node_id,
        position=cmd.position,
    )
    publish_domain_event(
        bus,
        "event.dimension.structure_changed",
        DimensionStructureChangedEvent(
            dim_id=cmd.dim_id,
            reason="place_item_nodes",
            affected_node_ids=node_ids,
        ),
        correlation_id=getattr(ctx, "correlation_id", None),
        session_id=getattr(ctx, "session_id", None),
        causation_id=getattr(ctx, "command_message_id", None),
    )
    return _HandlerResult(ok=True, data=node_ids)


def handle_rename_dimension_item(
    cmd: RenameDimensionItemCommand, engine: Any, bus: Any, ctx: Any = None
) -> _HandlerResult[None]:
    engine.rename_dimension_item(cmd.dim_id, cmd.item_id, cmd.new_name)
    publish_domain_event(
        bus,
        "event.dimension.structure_changed",
        DimensionStructureChangedEvent(
            dim_id=cmd.dim_id,
            reason="rename_dimension_item",
            affected_node_ids=[cmd.item_id],
        ),
        correlation_id=getattr(ctx, "correlation_id", None),
        session_id=getattr(ctx, "session_id", None),
        causation_id=getattr(ctx, "command_message_id", None),
    )
    return _HandlerResult(ok=True)


def handle_rename_cube(
    cmd: RenameCubeCommand, engine: Any, bus: Any
) -> _HandlerResult[None]:
    engine.rename_cube(cmd.cube_id, cmd.new_name)
    return _HandlerResult(ok=True)


def handle_create_dimension_item(
    cmd: AddDimensionItemCommand, engine: Any, bus: Any, ctx: Any = None
) -> _HandlerResult[Any]:
    item = engine.create_dimension_item(cmd.dim_id, cmd.name, position=cmd.position)
    # RemoteEngine returns a dict; Python engine returns a DimensionItem object
    item_id = item["id"] if isinstance(item, dict) else item.id
    # Engine already publishes dimension_item.created via _publish_event.
    # No need to publish duplicate events here — that would flood the bus
    # with 2 extra events per call (3 total) during batch operations.
    return _HandlerResult(ok=True, data={"id": item_id})


def handle_create_dimension_items_batch(
    cmd, engine: Any, bus: Any, ctx: Any = None
) -> _HandlerResult[Any]:
    """Batch-create many dimension items with a single event publish."""
    items = [(e["dim_id"], e["name"], e.get("position", "append")) for e in cmd.entries]
    created = engine.batch_create_dimension_items(items)
    item_ids = [it["id"] if isinstance(it, dict) else it.id for it in created]
    return _HandlerResult(ok=True, data={"ids": item_ids})


def handle_create_group(
    cmd: CreateGroupCommand, engine: Any, bus: Any, ctx: Any = None
) -> _HandlerResult[str]:
    parent_group_id = cmd.parent_group_id

    # Resolve parent_group_label to parent_group_id if provided
    if cmd.parent_group_label is not None:
        if parent_group_id is not None:
            raise ValueError(
                "Cannot specify both parent_group_id and parent_group_label"
            )
        from lib_openm.outline_graph_bridge import find_group_node_id_by_label
        ws = engine.workspace
        resolved = find_group_node_id_by_label(cmd.dim_id, cmd.parent_group_label, ws)
        if resolved is None:
            raise ValueError(
                f"Group label not found: {cmd.parent_group_label!r}"
            )
        parent_group_id = resolved

    group_id = engine.create_group(
        cmd.dim_id, cmd.label, parent_group_id, cmd.child_item_ids
    )
    publish_domain_event(
        bus,
        "event.dimension.structure_changed",
        DimensionStructureChangedEvent(
            dim_id=cmd.dim_id,
            reason="create_group",
            affected_node_ids=[group_id],
        ),
        correlation_id=getattr(ctx, "correlation_id", None),
        session_id=getattr(ctx, "session_id", None),
        causation_id=getattr(ctx, "command_message_id", None),
    )
    return _HandlerResult(ok=True, data=group_id)


def handle_rename_dimension(
    cmd: RenameDimensionCommand, engine: Any, bus: Any, ctx: Any = None
) -> _HandlerResult[None]:
    engine.rename_dimension(cmd.dim_id, cmd.new_name)
    publish_domain_event(
        bus,
        "event.dimension.structure_changed",
        DimensionStructureChangedEvent(
            dim_id=cmd.dim_id,
            reason="rename_dimension",
            affected_node_ids=[cmd.dim_id],
        ),
        correlation_id=getattr(ctx, "correlation_id", None),
        session_id=getattr(ctx, "session_id", None),
        causation_id=getattr(ctx, "command_message_id", None),
    )
    return _HandlerResult(ok=True)


def handle_rename_view(
    cmd: RenameViewCommand, engine: Any, bus: Any
) -> _HandlerResult[None]:
    view = engine.workspace.views.get(cmd.view_id)
    if view is None:
        raise ValueError(f"View not found: {cmd.view_id}")
    view.name = cmd.new_name
    return _HandlerResult(ok=True)


def handle_delete_view(
    cmd: DeleteViewCommand, engine: Any, bus: Any
) -> _HandlerResult[None]:
    engine.workspace.views.pop(cmd.view_id, None)
    engine.workspace.views_order = [
        vid for vid in engine.workspace.views_order if vid != cmd.view_id
    ]
    publish_domain_event(
        bus,
        "event.view.deleted",
        {"view_id": cmd.view_id},
    )
    return _HandlerResult(ok=True)


def handle_detach_dimension_from_cube(
    cmd: DetachDimensionFromCubeCommand, engine: Any, bus: Any
) -> _HandlerResult[None]:
    engine.detach_dimension_from_cube(cmd.cube_id, cmd.dim_id)
    return _HandlerResult(ok=True)


# --- Adapters for CommandExecutor (ctx + kwargs) ---
# These wrap the Phase 8 typed handlers so they can be registered in
# CommandRegistry and invoked via session.execute(...).


def _adapt_result(result: _HandlerResult) -> Any:
    """Convert a _HandlerResult to a value or raise on failure."""
    if not result.ok:
        raise RuntimeError(result.error or "Command failed")
    return result.data


def handle_rename_group_node_adapter(
    ctx: Any, dim_id: str, node_id: str, new_label: str
) -> None:
    cmd = RenameGroupNodeCommand(dim_id=dim_id, node_id=node_id, new_label=new_label)
    bus = get_message_bus()
    result = handle_rename_group_node(cmd, ctx.engine, bus, ctx=ctx)
    _adapt_result(result)


def handle_delete_group_node_adapter(
    ctx: Any, dim_id: str, node_id: str, promote_children: str = "to_parent"
) -> None:
    cmd = DeleteGroupNodeCommand(dim_id=dim_id, node_id=node_id, promote_children=promote_children)
    bus = get_message_bus()
    result = handle_delete_group_node(cmd, ctx.engine, bus, ctx=ctx)
    _adapt_result(result)


def handle_move_nodes_adapter(
    ctx: Any,
    dim_id: str,
    node_ids: list[str],
    parent_node_id: str | None,
    anchor_node_id: str | None = None,
    position: str = "after",
    move_empty_parents: bool = True,
) -> None:
    cmd = MoveNodesCommand(
        dim_id=dim_id,
        node_ids=node_ids,
        parent_node_id=parent_node_id,
        anchor_node_id=anchor_node_id,
        position=position,
        move_empty_parents=move_empty_parents,
    )
    bus = get_message_bus()
    result = handle_move_nodes(cmd, ctx.engine, bus, ctx=ctx)
    _adapt_result(result)


def handle_add_aggregate_item_adapter(
    ctx: Any, dim_id: str, group_node_id: str, name: str
) -> Any:
    cmd = AddAggregateItemCommand(dim_id=dim_id, group_node_id=group_node_id, name=name)
    bus = get_message_bus()
    result = handle_add_aggregate_item(cmd, ctx.engine, bus, ctx=ctx)
    return _adapt_result(result)


def handle_place_item_nodes_adapter(
    ctx: Any,
    dim_id: str,
    item_ids: list[str],
    parent_node_id: str | None,
    anchor_node_id: str | None = None,
    position: str = "after",
) -> list[str]:
    cmd = PlaceItemNodesCommand(
        dim_id=dim_id,
        item_ids=item_ids,
        parent_node_id=parent_node_id,
        anchor_node_id=anchor_node_id,
        position=position,
    )
    bus = get_message_bus()
    result = handle_place_item_nodes(cmd, ctx.engine, bus, ctx=ctx)
    return _adapt_result(result)


def handle_rename_dimension_item_adapter(
    ctx: Any, dim_id: str, item_id: str, new_name: str
) -> None:
    cmd = RenameDimensionItemCommand(dim_id=dim_id, item_id=item_id, new_name=new_name)
    bus = get_message_bus()
    result = handle_rename_dimension_item(cmd, ctx.engine, bus, ctx=ctx)
    _adapt_result(result)


def handle_rename_cube_adapter(
    ctx: Any, cube_id: str, new_name: str
) -> None:
    cmd = RenameCubeCommand(cube_id=cube_id, new_name=new_name)
    bus = get_message_bus()
    result = handle_rename_cube(cmd, ctx.engine, bus)
    _adapt_result(result)


def handle_rename_dimension_adapter(
    ctx: Any, dim_id: str, new_name: str
) -> None:
    cmd = RenameDimensionCommand(dim_id=dim_id, new_name=new_name)
    bus = get_message_bus()
    result = handle_rename_dimension(cmd, ctx.engine, bus, ctx=ctx)
    _adapt_result(result)


def handle_rename_view_adapter(
    ctx: Any, view_id: str, new_name: str
) -> None:
    cmd = RenameViewCommand(view_id=view_id, new_name=new_name)
    bus = get_message_bus()
    result = handle_rename_view(cmd, ctx.engine, bus)
    _adapt_result(result)


def handle_delete_view_adapter(
    ctx: Any, view_id: str
) -> None:
    cmd = DeleteViewCommand(view_id=view_id)
    bus = get_message_bus()
    result = handle_delete_view(cmd, ctx.engine, bus)
    _adapt_result(result)


def handle_detach_dimension_from_cube_adapter(
    ctx: Any, cube_id: str, dim_id: str
) -> None:
    cmd = DetachDimensionFromCubeCommand(cube_id=cube_id, dim_id=dim_id)
    bus = get_message_bus()
    result = handle_detach_dimension_from_cube(cmd, ctx.engine, bus)
    _adapt_result(result)


def handle_create_dimension_item_adapter(
    ctx: Any, dim_id: str, name: str, position: str = "append"
) -> Any:
    cmd = AddDimensionItemCommand(dim_id=dim_id, name=name, position=position)
    bus = get_message_bus()
    result = handle_create_dimension_item(cmd, ctx.engine, bus, ctx=ctx)
    return _adapt_result(result)


def handle_create_group_adapter(
    ctx: Any,
    dim_id: str,
    label: str,
    parent_group_id: str | None = None,
    parent_group_label: str | None = None,
    child_item_ids: list[str] | None = None,
) -> str:
    cmd = CreateGroupCommand(
        dim_id=dim_id,
        label=label,
        parent_group_id=parent_group_id,
        parent_group_label=parent_group_label,
        child_item_ids=child_item_ids,
    )
    bus = get_message_bus()
    result = handle_create_group(cmd, ctx.engine, bus, ctx=ctx)
    return _adapt_result(result)


def handle_move_items_to_group_adapter(
    ctx: Any, dim_id: str, item_ids: list[str], group_node_id: str
) -> None:
    ctx.engine.move_items_to_group(dim_id, item_ids, group_node_id)


def handle_ungroup_items_adapter(
    ctx: Any, dim_id: str, item_ids: list[str]
) -> None:
    ctx.engine.ungroup_items(dim_id, item_ids)
    bus = get_message_bus()
    publish_domain_event(
        bus,
        "event.dimension.structure_changed",
        DimensionStructureChangedEvent(
            dim_id=dim_id,
            reason="ungroup_items",
            affected_node_ids=[],
        ),
        correlation_id=getattr(ctx, "correlation_id", None),
        session_id=getattr(ctx, "session_id", None),
        causation_id=getattr(ctx, "command_message_id", None),
    )


def cmd_create_dimension_item(
    ctx: Any, dim_id: str, name: str, position: str = "append"
) -> Any:
    """Create a new dimension item — canonical command.

    Thin wrapper around :func:`handle_create_dimension_item_adapter`.
    """
    return handle_create_dimension_item_adapter(ctx, dim_id, name, position)


def cmd_create_dimension_items_batch(
    ctx: Any, entries: list[dict]
) -> Any:
    """Batch-create many dimension items — canonical batch command.

    Each entry in *entries* is a dict with keys: dim_id, name, position (optional).
    """
    bus = get_message_bus()
    result = handle_create_dimension_items_batch(
        type("BatchCmd", (), {"entries": entries})(),
        ctx.engine, bus, ctx=ctx,
    )
    return _adapt_result(result)


def cmd_create_aggregate_item(
    ctx: Any, dim_id: str, group_node_id: str, name: str
) -> Any:
    """Create an aggregate item under a group — canonical command.

    Thin wrapper around :func:`handle_add_aggregate_item_adapter`.
    """
    return handle_add_aggregate_item_adapter(ctx, dim_id, group_node_id, name)
