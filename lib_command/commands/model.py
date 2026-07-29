"""
Model commands - CRUD operations for dimensions, cubes, items, views.

create, delete operations for model entities.
"""

from __future__ import annotations

from typing import Any, Optional

from lib_openm.model import OutlineNode, ViewLayout
from lib_command.commands.view_commands import validate_view_layout_for_cube
from lib_command.core.domain_event_publisher import publish_domain_event
from lib_command.core.message_bus import get_message_bus


def _build_default_layout(cube) -> ViewLayout:
    """Deterministic default layout rule from the view layout refactor plan.

    - Start with all cube dimensions on page.
    - Move the first non-system dimension to rows.
    - Move the second non-system dimension to cols.
    - Keep any remaining non-system dimensions on page.
    """
    all_dims = list(cube.dimension_ids)
    page = list(all_dims)
    rows: list[str] = []
    cols: list[str] = []
    non_system = [d for d in all_dims if not d.startswith("@")]
    if len(non_system) >= 1:
        rows = [non_system[0]]
        page.remove(non_system[0])
    if len(non_system) >= 2:
        cols = [non_system[1]]
        page.remove(non_system[1])
    return ViewLayout(rows=rows, cols=cols, page=page)


def cmd_create(
    ctx,
    type: str,
    name: str,
    **properties
) -> dict:
    """
    Create a new object.

    Args:
        type: "dimension", "cube", "item", "view"
        name: Name for the new object
        **properties: Additional properties
    """
    engine = ctx.engine
    ws = ctx.workspace

    if not engine or not ws:
        raise ValueError("No engine or workspace available")

    try:
        if type == "dimension":
            dim_type = properties.get("dim_type", "set")
            if dim_type not in ("set", "seq"):
                dim_type = "set"
            dim = engine.create_dimension(name, dim_type=dim_type)
            # Add items if provided
            items = properties.get("items", [])
            if isinstance(items, str):
                items = [item.strip() for item in items.split(",") if item.strip()]
            if items and isinstance(items, list):
                for item in items:
                    engine.create_dimension_item(dim.id, str(item))
            ctx.status(f"Created dimension: {name}")
            return {"type": type, "name": name, "id": dim.id}

        elif type == "cube":
            dim_ids = properties.get("dimensions", [])
            # Resolve dimension names to IDs (supports generated IDs from engine.create_dimension)
            resolved_ids = []
            for d in dim_ids:
                dim = ws.dimensions.get(d)
                if dim:
                    resolved_ids.append(d)
                else:
                    # Try name lookup
                    found = next(
                        (did for did, dim in ws.dimensions.items() if dim.name == d),
                        None,
                    )
                    if found:
                        resolved_ids.append(found)
                    else:
                        raise ValueError(f"Dimension not found: {d}")
            cube = engine.create_cube(name, resolved_ids)
            ctx.status(f"Created cube: {name}")
            return {"type": type, "name": name, "id": cube.id}

        elif type == "item":
            dim_id = properties.get("dimension")
            if not dim_id:
                raise ValueError("dimension required for item creation")
            dim = ws.dimensions.get(dim_id)
            if not dim:
                # Try name lookup
                dim = next(
                    (dim for dim in ws.dimensions.values() if dim.name == dim_id),
                    None,
                )
            if not dim:
                raise ValueError(f"Dimension not found: {dim_id}")
            item = dim.add_item(name)
            ctx.status(f"Created item: {name} in {dim.id}")
            return {"type": type, "name": name, "dimension": dim.id}

        elif type == "view":
            cube_id = properties.get("cube")
            if not cube_id:
                raise ValueError("cube required for view creation")

            cube = ws.cubes.get(cube_id)
            if cube is None:
                cube = engine.find_cube_by_name(cube_id)
            if cube is None:
                raise ValueError(f"Cube not found: {cube_id}")

            layout_raw = properties.get("layout")
            if layout_raw is not None:
                layout = ViewLayout(
                    rows=list(layout_raw.get("rows", [])),
                    cols=list(layout_raw.get("cols", [])),
                    page=list(layout_raw.get("page", [])),
                )
                # If page was not explicitly provided in role syntax,
                # auto-assign remaining cube dimensions to page.
                if not layout.page:
                    assigned = set(layout.rows) | set(layout.cols)
                    layout.page = [d for d in cube.dimension_ids if d not in assigned]
            else:
                row_dims = properties.get("row_dims", [])
                col_dims = properties.get("col_dims", [])
                page_dim_ids = properties.get("page_dim_ids")
                if row_dims or col_dims or page_dim_ids is not None:
                    if page_dim_ids is None:
                        assigned = set(row_dims) | set(col_dims)
                        page_dim_ids = [d for d in cube.dimension_ids if d not in assigned]
                    layout = ViewLayout(
                        rows=list(row_dims),
                        cols=list(col_dims),
                        page=list(page_dim_ids),
                    )
                else:
                    layout = _build_default_layout(cube)

            # Ensure every cube dimension is assigned to an axis.  Dimensions
            # not explicitly placed on rows/cols/page (notably the technical @
            # dimension when the script specifies only user dimensions on page)
            # are appended to page so the view covers the full cube shape.
            used = set(layout.rows) | set(layout.cols) | set(layout.page)
            for did in cube.dimension_ids:
                if did not in used:
                    layout.page.append(did)
                    used.add(did)

            validate_view_layout_for_cube(cube, layout)

            row_dim_id = layout.rows[0] if layout.rows else ""
            col_dim_id = layout.cols[0] if layout.cols else ""
            view = engine.create_view(
                name,
                cube_id,
                row_dim_id,
                col_dim_id,
                page_dim_ids=list(layout.page),
                layout=layout,
            )
            ctx.status(f"Created view: {name}")
            return {"type": type, "name": name, "id": view.id}

        else:
            raise ValueError(f"Unknown type: {type}")

    except Exception as e:
        ctx.status(f"Error creating {type}: {e}")
        raise


def cmd_create_view(
    ctx,
    name: str,
    cube_id: str,
    row_dims: list[str] | None = None,
    col_dims: list[str] | None = None,
    page_dim_ids: list[str] | None = None,
    layout: dict[str, list[str]] | None = None,
) -> dict:
    """Create a new view over a cube.

    Accepts either shorthand dimension lists (``row_dims``, ``col_dims``,
    ``page_dim_ids``) or a full ``layout`` dict.  If ``layout`` is provided,
    it takes precedence.  Unassigned dimensions are automatically placed
    in the page axis.  ``cube_id`` may be a stable cube ID or a cube name,
    which is resolved against the workspace.

    Args:
        ctx: Execution context (provides ``engine`` and ``workspace``).
        name: Human-readable view name.
        cube_id: Stable cube identifier or cube name.
        row_dims: Row dimension IDs (shorthand syntax).
        col_dims: Column dimension IDs (shorthand syntax).
        page_dim_ids: Page dimension IDs (shorthand syntax).
        layout: Full layout dict with ``rows``/``cols``/``page`` keys
            (role syntax).  Takes precedence over shorthand args.

    Returns:
        Dict with ``affected: 1``, ``property: "view"``, ``view_id``,
        and ``name``.

    Raises:
        ValueError: If no engine/workspace is available, if the cube
            cannot be found, or if the layout is invalid for the cube.
    """
    engine = ctx.engine
    ws = ctx.workspace
    if not engine or not ws:
        raise ValueError("No engine or workspace available")

    cube = ws.cubes.get(cube_id)
    if cube is None:
        cube = engine.find_cube_by_name(cube_id) if hasattr(engine, "find_cube_by_name") else None
    if cube is None:
        raise ValueError(f"Cube not found: {cube_id}")

    if layout is not None:
        view_layout = ViewLayout(
            rows=list(layout.get("rows", [])),
            cols=list(layout.get("cols", [])),
            page=list(layout.get("page", [])),
        )
        if not view_layout.page:
            assigned = set(view_layout.rows) | set(view_layout.cols)
            view_layout.page = [d for d in cube.dimension_ids if d not in assigned]
    else:
        row_dims = row_dims or []
        col_dims = col_dims or []
        if page_dim_ids is None:
            assigned = set(row_dims) | set(col_dims)
            page_dim_ids = [d for d in cube.dimension_ids if d not in assigned]
        view_layout = ViewLayout(
            rows=list(row_dims),
            cols=list(col_dims),
            page=list(page_dim_ids),
        )

    validate_view_layout_for_cube(cube, view_layout)

    row_dim_id = view_layout.rows[0] if view_layout.rows else ""
    col_dim_id = view_layout.cols[0] if view_layout.cols else ""
    view = engine.create_view(
        name,
        cube.id,
        row_dim_id,
        col_dim_id,
        page_dim_ids=list(view_layout.page),
        layout=view_layout,
    )
    ctx.status(f"Created view: {name}")
    return {"type": "view", "name": name, "id": view.id}


def cmd_delete(ctx, target: str) -> dict:
    """
    Delete a target.

    Args:
        target: Target to delete (e.g., "dimension:Q1", "item:Q1.Jan")
    """
    target_type, target_id = _parse_target(target)

    engine = ctx.engine
    ws = ctx.workspace

    if not engine or not ws:
        raise ValueError("No engine or workspace available")

    if target_type == "dimension":
        if target_id in ws.dimensions:
            del ws.dimensions[target_id]
            ctx.status(f"Deleted dimension: {target_id}")
        else:
            raise ValueError(f"Dimension not found: {target_id}")

    elif target_type == "cube":
        if target_id in ws.cubes:
            del ws.cubes[target_id]
            ctx.status(f"Deleted cube: {target_id}")
        else:
            raise ValueError(f"Cube not found: {target_id}")

    elif target_type == "item":
        # Parse Dim.Item format
        if "." in target_id:
            dim_id, item_id = target_id.split(".", 1)
            dim = ws.dimensions.get(dim_id)
            if dim:
                # Remove item from dimension
                dim.items = [i for i in dim.items if i.id != item_id]
                ctx.status(f"Deleted item: {item_id} from {dim_id}")
            else:
                raise ValueError(f"Dimension not found: {dim_id}")
        else:
            raise ValueError("Item must be specified as Dim.Item")

    elif target_type == "view":
        if target_id in ws.views:
            del ws.views[target_id]
            ctx.status(f"Deleted view: {target_id}")
        else:
            raise ValueError(f"View not found: {target_id}")

    else:
        raise ValueError(f"Unknown target type: {target_type}")

    return {"deleted": target, "type": target_type}


def cmd_delete_dimension_items(ctx, dim_id: str, item_ids: list[str]) -> dict:
    """Delete dimension items using current engine semantics.

    Args:
        dim_id: ID of the dimension containing the items
        item_ids: List of item IDs to delete
    """
    engine = ctx.engine
    if not engine:
        raise ValueError("No engine available")

    if hasattr(engine, 'delete_dimension_items'):
        result = engine.delete_dimension_items(dim_id, item_ids)
        ctx.status(f"Deleted {len(item_ids)} items from dimension {dim_id}")
        publish_domain_event(
            get_message_bus(),
            "event.dimension_item.deleted",
            {"dim_id": dim_id, "item_ids": item_ids},
            correlation_id=getattr(ctx, "correlation_id", None),
            session_id=getattr(ctx, "session_id", None),
            causation_id=getattr(ctx, "command_message_id", None),
        )
        return {"deleted": item_ids, "dimension": dim_id, "engine_result": result}
    else:
        # Fallback: manually remove items from dimension
        ws = ctx.workspace
        if not ws:
            raise ValueError("No workspace available")
        dim = ws.dimensions.get(dim_id)
        if not dim:
            raise ValueError(f"Dimension not found: {dim_id}")
        deleted = []
        for item_id in item_ids:
            dim.items = [it for it in dim.items if it.id != item_id]
            deleted.append(item_id)
        ctx.status(f"Deleted {len(deleted)} items from dimension {dim_id}")
        publish_domain_event(
            get_message_bus(),
            "event.dimension_item.deleted",
            {"dim_id": dim_id, "item_ids": deleted},
            correlation_id=getattr(ctx, "correlation_id", None),
            session_id=getattr(ctx, "session_id", None),
            causation_id=getattr(ctx, "command_message_id", None),
        )
        return {"deleted": deleted, "dimension": dim_id}


def cmd_create_dimension(
    ctx,
    name: str,
    dim_type: str = "set",
) -> dict:
    """Create a new dimension.

    Maps to engine.create_dimension(name, dim_type).
    """
    engine = ctx.engine
    if not engine:
        raise ValueError("No engine available")
    if not name:
        raise ValueError("name is required")

    dim = engine.create_dimension(name, dim_type=dim_type)
    ctx.status(f"Created dimension: {name}")
    # Engine already publishes event.dimension.created with DTO enrichment
    # via EventDTOProjector. Do NOT republish here — the duplicate unenriched
    # event triggers redundant dimension_detail queries in GUIReadModelBinder.
    return {"type": "dimension", "name": name, "id": dim.id}


def cmd_create_cube(
    ctx,
    name: str,
    dimension_ids: list[str],
) -> dict:
    """Create a new cube with the given dimensions.

    Maps to engine.create_cube(name, dimension_ids).
    Accepts dimension IDs or dimension names (resolved to IDs).
    """
    engine = ctx.engine
    ws = ctx.workspace or getattr(engine, "workspace", None)
    if not engine:
        raise ValueError("No engine available")
    if not name:
        raise ValueError("name is required")
    if not dimension_ids:
        raise ValueError("dimension_ids is required")

    # Resolve dimension names to IDs (supports name aliases)
    resolved_ids = []
    for d in dimension_ids:
        if ws and d in ws.dimensions:
            resolved_ids.append(d)
        else:
            found = next(
                (did for did, dim in (ws.dimensions or {}).items() if dim.name == d),
                None,
            )
            if found:
                resolved_ids.append(found)
            else:
                raise ValueError(f"Dimension not found: {d}")

    cube = engine.create_cube(name, resolved_ids)
    ctx.status(f"Created cube: {name}")
    # Engine already publishes event.cube.created with DTO enrichment
    # via EventDTOProjector. Do NOT republish here — the duplicate unenriched
    # event triggers redundant cube_detail queries in GUIReadModelBinder.
    return {"type": "cube", "name": name, "id": cube.id}


def cmd_clear_dimension_outline(
    ctx,
    dim_id: str,
) -> dict:
    """Clear a dimension's outline.

    Validates that dim_id exists, clears the outline list, and emits
    event.dimension.structure_changed.
    """
    engine = ctx.engine
    if not engine:
        raise ValueError("No engine available")
    if not dim_id:
        raise ValueError("dim_id is required")

    ws = engine.workspace
    dim = ws.dimensions.get(dim_id)
    if dim is None:
        raise ValueError(f"Unknown dimension: {dim_id}")

    # Record previous outline for undo support (deferred)
    previous_outline = list(getattr(dim, "outline", []) or [])

    # Bypass the Phase 4 read-only guard — this is canonical engine mutation
    object.__setattr__(dim, "outline", [])
    dim.invalidate_outline_cache()

    ctx.status(f"Cleared outline for dimension: {dim.name}")

    from lib_command.core.domain_event_publisher import publish_domain_event
    from lib_command.core.message_bus import get_message_bus
    from lib_command.events.domain_events import DimensionStructureChangedEvent

    bus = get_message_bus()
    publish_domain_event(
        bus,
        "event.dimension.structure_changed",
        DimensionStructureChangedEvent(
            dim_id=dim_id,
            reason="clear_dimension_outline",
            affected_node_ids=[],
        ),
    )

    return {
        "affected": 1,
        "property": "dimension_outline",
        "dim_id": dim_id,
        "previous_outline_length": len(previous_outline),
    }


def cmd_set_dimension_outline(
    ctx,
    dim_id: str,
    outline: list[dict] | None = None,
) -> dict:
    """Set a dimension's outline.

    Validates that dim_id exists, computes or validates the outline, and emits
    event.dimension.structure_changed.

    - outline=None: generate a flat default outline from current dimension items.
    - outline=[]: clear the outline (matches clear_dimension_outline behavior).
    - outline=[...]: set the outline to the provided list.
    """
    engine = ctx.engine
    if not engine:
        raise ValueError("No engine available")
    if not dim_id:
        raise ValueError("dim_id is required")

    ws = engine.workspace
    dim = ws.dimensions.get(dim_id)
    if dim is None:
        raise ValueError(f"Unknown dimension: {dim_id}")

    # Record previous outline for undo support (deferred)
    previous_outline = list(getattr(dim, "outline", []) or [])

    if outline is None:
        # Generate flat outline: all items as leaves at root.
        new_outline = [OutlineNode(label=it.name, item_id=it.id, children=[]) for it in dim.items]
    else:
        # Convert dict payloads to OutlineNode objects if needed.
        def _to_node(obj: dict | OutlineNode) -> OutlineNode:
            if isinstance(obj, OutlineNode):
                return obj
            if isinstance(obj, dict):
                return OutlineNode(
                    label=obj["label"],
                    item_id=obj.get("item_id"),
                    children=[_to_node(c) for c in obj.get("children", [])],
                )
            raise ValueError(f"Invalid outline node type: {type(obj)}")

        new_outline = [_to_node(node) for node in outline]

    # Bypass the Phase 4 read-only guard — transitional command-layer debt.
    # Preferred future target: Engine.set_dimension_outline(dim_id, outline).
    object.__setattr__(dim, "outline", new_outline)
    dim.invalidate_outline_cache()

    ctx.status(f"Set outline for dimension: {dim.name}")

    from lib_command.core.domain_event_publisher import publish_domain_event
    from lib_command.core.message_bus import get_message_bus
    from lib_command.events.domain_events import DimensionStructureChangedEvent

    bus = get_message_bus()
    publish_domain_event(
        bus,
        "event.dimension.structure_changed",
        DimensionStructureChangedEvent(
            dim_id=dim_id,
            reason="set_dimension_outline",
            affected_node_ids=[],
        ),
    )

    return {
        "affected": 1,
        "property": "dimension_outline",
        "dim_id": dim_id,
        "previous_outline_length": len(previous_outline),
    }


def cmd_ensure_group_in_graph(
    ctx,
    dim_id: str,
    label: str,
    children: list[dict] | None = None,
    parent_group_id: str | None = None,
) -> dict:
    """Ensure a group node exists in the dimension graph.

    Transitional command for F6e.2 engine passthrough removal.
    Serializes plain-dict children back into OutlineNode before delegating.
    """
    engine = ctx.engine
    from lib_openm.model import OutlineNode

    child_nodes = [
        OutlineNode(label=c.get("label", ""), item_id=c.get("item_id"), children=[])
        for c in (children or [])
    ]
    group_node = OutlineNode(label=label, children=child_nodes)
    group_node_id = engine.ensure_group_in_graph(
        dim_id, group_node, parent_group_id=parent_group_id
    )
    return {
        "affected": 1,
        "property": "group_node",
        "group_node_id": group_node_id,
        "dim_id": dim_id,
    }


def cmd_resolve_item_node_id(
    ctx,
    dim_id: str,
    item_id: str,
) -> dict:
    """Resolve an item_id to its ITEM_REF node_id, creating the node if absent.

    Transitional command for F6e.2 engine passthrough removal.
    """
    engine = ctx.engine
    node_id = engine.resolve_item_node_id(dim_id, item_id)
    return {
        "affected": 1,
        "property": "item_node",
        "node_id": node_id,
        "dim_id": dim_id,
        "item_id": item_id,
    }


def cmd_set_dimension_item_order(
    ctx,
    dim_id: str,
    item_ids: list[str],
) -> dict:
    """Replace the flat dimension item order exactly with item_ids.

    Validates that item_ids is a permutation of the current dimension
    item IDs. Rejects sequential dimensions.
    """
    if not dim_id:
        raise ValueError("dim_id is required")
    if not item_ids:
        raise ValueError("item_ids is required")
    ctx.engine.set_dimension_item_order(dim_id, item_ids)
    return {
        "affected": len(item_ids),
        "dim_id": dim_id,
        "item_ids": item_ids,
    }


def _parse_target(target: str) -> tuple[str, Optional[str]]:
    """Parse target string into (type, id)."""
    if ":" in target:
        parts = target.split(":", 1)
        return parts[0], parts[1]
    return target, None
