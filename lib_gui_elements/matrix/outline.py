"""Outline/group manipulation helpers for the matrix grid."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from lib_contracts.types import OutlineNode

if TYPE_CHECKING:
    from PySide6 import QtCore, QtGui, QtWidgets

# Debug flag for outline operations - set DEBUG_GUI=1 to enable
DEBUG_GUI = os.environ.get("DEBUG_GUI", "0") == "1"


class OutlineHelper:
    """Helper methods for outline structure manipulation."""

    def __init__(self, grid: "MatrixGrid") -> None:
        self._grid = grid

    def _ensure_outline_axis(self, axis: str) -> bool:
        """Ensure outline exists for the given axis."""
        return self._grid._dimensions.ensure_outline_axis(axis)

    def _outline_root(self, axis: str) -> list["OutlineNode"]:
        """Get root outline for axis."""
        return self._grid._dimensions.outline_root(axis)

    def _set_outline_root(self, axis: str, root: list["OutlineNode"]) -> None:
        """Set root outline for axis."""
        self._grid._dimensions.set_outline_root(axis, root)

    def _prune_empty_groups(self, nodes: list["OutlineNode"]) -> list["OutlineNode"]:
        """Recursively remove empty groups."""
        return self._grid._dimensions.prune_empty_groups(nodes)

    def _collect_empty_group_labels(self, nodes: list["OutlineNode"]) -> set[str]:
        """Collect labels of empty groups that would be pruned."""
        return self._grid._dimensions.collect_empty_group_labels(nodes)

    def _axis_dim_id(self, axis: str) -> str | None:
        """Get dimension ID for axis."""
        return self._grid._axis_dim_id(axis)

    def _path_for_item_id(self, axis: str, item_id: str) -> tuple[int, ...] | None:
        """Find path for item ID in outline."""
        return self._grid._path_for_item_id(axis, item_id)

    def _remove_leaf_from_outline(self, nodes: list["OutlineNode"], item_id: str) -> tuple[list["OutlineNode"], "OutlineNode" | None]:
        """Remove leaf from outline (delegates to existing method)."""
        return self.remove_leaf_from_outline(nodes, item_id)

    def _leaf_node_for_item(self, axis: str, item_id: str) -> "OutlineNode" | None:
        """Get leaf node for item."""
        return self.leaf_node_for_item(axis, item_id)

    def _get_node_at_path(self, nodes: list["OutlineNode"], path: tuple[int, ...]) -> "OutlineNode" | None:
        """Get node at path."""
        return self.get_node_at_path(nodes, path)

    def _set_node_children_at_path(self, nodes: list["OutlineNode"], path: tuple[int, ...], children: list["OutlineNode"]) -> list["OutlineNode"]:
        """Set children at path."""
        return self.set_node_children_at_path(nodes, path, children)

    # Property accessors for grid attributes
    @property
    def _view_id(self):
        return self._grid._view_id

    @property
    def _sel_mode(self):
        return self._grid._sel_mode

    @_sel_mode.setter
    def _sel_mode(self, value):
        self._grid._sel_mode = value

    @property
    def _sel_indices(self):
        return self._grid._sel_indices

    @_sel_indices.setter
    def _sel_indices(self, value):
        self._grid._sel_indices = value

    @property
    def _sel_row(self):
        return self._grid._sel_row

    @_sel_row.setter
    def _sel_row(self, value):
        self._grid._sel_row = value

    @property
    def _sel_col(self):
        return self._grid._sel_col

    @_sel_col.setter
    def _sel_col(self, value):
        self._grid._sel_col = value

    @property
    def _anchor_row(self):
        return self._grid._anchor_row

    @_anchor_row.setter
    def _anchor_row(self, value):
        self._grid._anchor_row = value

    @property
    def _anchor_col(self):
        return self._grid._anchor_col

    @_anchor_col.setter
    def _anchor_col(self, value):
        self._grid._anchor_col = value

    @property
    def _rows(self):
        return self._grid._rows

    @property
    def _cols(self):
        return self._grid._cols

    @property
    def _row_keys(self):
        return self._grid._row_keys

    @property
    def _col_keys(self):
        return self._grid._col_keys

    @property
    def outline_changed(self):
        return self._grid.outline_changed

    def reload(self):
        return self._grid.reload()

    def remove_leaf_from_outline(
        self, nodes: list["OutlineNode"], item_id: str
    ) -> tuple[list["OutlineNode"], "OutlineNode" | None]:
        """Remove a leaf node with the given item_id from the outline tree.

        Returns (updated_nodes, removed_node) where removed_node is None if not found.
        """
        from lib_contracts.types import OutlineNode

        removed: "OutlineNode" | None = None

        def filter_nodes(node_list: list["OutlineNode"]) -> list["OutlineNode"]:
            nonlocal removed
            result: list["OutlineNode"] = []
            for node in node_list:
                if node.item_id == item_id:
                    removed = node
                    continue
                # Recursively filter children
                if node.children:
                    new_children = filter_nodes(list(node.children))
                    result.append(
                        OutlineNode(
                            label=node.label, item_id=node.item_id, children=new_children,
                            node_id=node.node_id, is_aggregate=node.is_aggregate,
                        )
                    )
                else:
                    result.append(node)
            return result

        return filter_nodes(nodes), removed

    def insert_group_at_path(
        self,
        nodes: list["OutlineNode"],
        path: tuple[int, ...],
        group_label: str,
        children: list["OutlineNode"] | None = None,
    ) -> list["OutlineNode"]:
        """Insert a new group node at the given path."""
        from lib_contracts.types import OutlineNode

        if not path:
            # Insert at root level
            new_group = OutlineNode(
                label=group_label,
                item_id=None,
                children=children or [],
            )
            return nodes + [new_group]

        def insert_at(
            node_list: list["OutlineNode"], remaining_path: tuple[int, ...]
        ) -> list["OutlineNode"]:
            if not remaining_path:
                return node_list

            idx = remaining_path[0]
            if not (0 <= idx < len(node_list)):
                return node_list

            node = node_list[idx]
            if len(remaining_path) == 1:
                # Insert as child of this node
                new_children = list(node.children or [])
                new_group = OutlineNode(
                    label=group_label,
                    item_id=None,
                    children=children or [],
                )
                new_children.append(new_group)
                updated_node = OutlineNode(
                    label=node.label,
                    item_id=node.item_id,
                    children=new_children,
                    node_id=node.node_id,
                    is_aggregate=node.is_aggregate,
                )
                return node_list[:idx] + [updated_node] + node_list[idx + 1 :]
            else:
                # Recurse deeper
                new_children = insert_at(list(node.children or []), remaining_path[1:])
                updated_node = OutlineNode(
                    label=node.label,
                    item_id=node.item_id,
                    children=new_children,
                    node_id=node.node_id,
                    is_aggregate=node.is_aggregate,
                )
                return node_list[:idx] + [updated_node] + node_list[idx + 1 :]

        return insert_at(nodes, path)
    def reorder_multiple_in_outline(self, axis: str, src_item_ids: list[str], dest_path: tuple[int, ...], insert_after: bool) -> None:
        """Reorder multiple selected items as a group within an outline structure.
        
        When items are dragged across group boundaries, they adopt the group membership
        of their destination location (based on the parent group at the drop position).
        """
        root = self._outline_root(axis)
        DEBUG_GUI and print(f"DEBUG _reorder_multi: ENTER src_ids={src_item_ids} dest_path={dest_path} insert_after={insert_after}")
        
        # Helper to get node at a specific path
        def _get_node_at_path(nodes: list[OutlineNode], path: tuple[int, ...]) -> OutlineNode | None:
            """Get the node at a specific path in the tree."""
            current = nodes
            for idx in path:
                if 0 <= idx < len(current):
                    if len(path) == 1 or idx == path[-1]:
                        return current[idx]
                    current = list(current[idx].children)
                else:
                    return None
            return None
        
        # Get the destination item ID from the original tree
        dest_node = _get_node_at_path(root, dest_path) if dest_path else None
        dest_item_id = dest_node.item_id if dest_node else None
        DEBUG_GUI and print(f"DEBUG _reorder_multi: dest_item_id={dest_item_id}")
        
        # dest_path is a tuple of indices representing the path to the destination item
        # The parent group is everything except the last element
        dest_parent_path = tuple(dest_path[:-1]) if len(dest_path) > 0 else ()
        dest_idx_in_parent = dest_path[-1] if len(dest_path) > 0 else 0
        
        # Calculate insertion index (adjust for insert_after)
        insert_idx = dest_idx_in_parent + (1 if insert_after else 0)
        DEBUG_GUI and print(f"DEBUG _reorder_multi: dest_parent_path={dest_parent_path} insert_idx={insert_idx}")
        DEBUG_GUI and print(f"DEBUG _reorder_multi: dest_parent_path={dest_parent_path} insert_idx={insert_idx}")
        
        # Collect all items to move with their original group info
        items_to_move: list[tuple[str, str | None, list[OutlineNode]]] = []
        
        def _find_and_extract(nodes: list[OutlineNode], target_id: str) -> tuple[list[OutlineNode], OutlineNode | None]:
            """Find and extract a leaf node by item_id, returning new nodes and the extracted node."""
            new_nodes: list[OutlineNode] = []
            extracted: OutlineNode | None = None
            for n in nodes:
                if extracted is None and n.item_id == target_id and not n.children:
                    extracted = n
                    continue
                if n.children:
                    new_kids, ext = _find_and_extract(list(n.children), target_id)
                    if ext is not None and extracted is None:
                        extracted = ext
                        new_nodes.append(OutlineNode(label=n.label, item_id=n.item_id, children=new_kids, node_id=n.node_id, is_aggregate=n.is_aggregate))
                    else:
                        new_nodes.append(n)
                else:
                    new_nodes.append(n)
            return new_nodes, extracted
        
        # Helper to count items in a path (for index adjustment)
        def _count_items_in_path(nodes: list[OutlineNode], path: tuple[int, ...]) -> int:
            """Count how many of the src_item_ids are in the given path."""
            if not path:
                return 0
            count = 0
            current = nodes
            for idx in path[:-1]:
                if 0 <= idx < len(current):
                    current = list(current[idx].children)
                else:
                    return 0
            # Now at the parent level
            if path and 0 <= path[-1] < len(current):
                target_idx = path[-1]
                for i, n in enumerate(current):
                    if i < target_idx and not n.children and n.item_id in src_item_ids:
                        count += 1
            return count
        
        # Count how many selected items are before the insertion point in the same parent
        items_before_insert = _count_items_in_path(root, dest_parent_path + (insert_idx,))
        
        # Helper to count items in a path (for index adjustment)
        def _count_items_in_path(nodes: list[OutlineNode], path: tuple[int, ...]) -> int:
            """Count how many of the src_item_ids are in the given path."""
            if not path:
                return 0
            count = 0
            current = nodes
            for idx in path[:-1]:
                if 0 <= idx < len(current):
                    current = list(current[idx].children)
                else:
                    return 0
            # Now at the parent level
            if path and 0 <= path[-1] < len(current):
                target_idx = path[-1]
                for i, n in enumerate(current):
                    if i < target_idx and not n.children and n.item_id in src_item_ids:
                        count += 1
            return count
        
        # Count how many selected items are before the insertion point in the same parent
        items_before_insert = _count_items_in_path(root, dest_parent_path + (insert_idx,))
        
        # Extract all selected items from the outline
        current_root = root
        for item_id in src_item_ids:
            new_root, extracted = _find_and_extract(current_root, item_id)
            if extracted is not None:
                items_to_move.append((item_id, extracted.label, list(extracted.children)))
                current_root = new_root
                DEBUG_GUI and print(f"DEBUG _reorder_multi: extracted {item_id}, label={extracted.label}")
            else:
                DEBUG_GUI and print(f"DEBUG _reorder_multi: FAILED to extract {item_id}")
                DEBUG_GUI and print(f"DEBUG _reorder_multi: extracted {item_id}, label={extracted.label}")
        
        if not items_to_move:
            DEBUG_GUI and print(f"DEBUG _reorder_multi: no items to move, returning")
            DEBUG_GUI and print(f"DEBUG _reorder_multi: no items to move, returning")
            return
        
        # Adjust insertion index based on how many selected items were before it
        # (since we removed them, the index shifts)
        adjusted_insert_idx = insert_idx - items_before_insert
        adjusted_insert_idx = max(0, adjusted_insert_idx)
        
        DEBUG_GUI and print(f"DEBUG _reorder_multi: items_before_insert={items_before_insert} adjusted_insert_idx={adjusted_insert_idx}")
        DEBUG_GUI and print(f"DEBUG _reorder_multi: final adjusted_insert_idx={adjusted_insert_idx}")
        
        # Helper to find the index of an item by ID within a parent at a given path
        def _find_item_index(nodes: list[OutlineNode], parent_path: tuple[int, ...], target_id: str) -> int | None:
            """Find the index of an item by ID within its parent's children."""
            if not parent_path:
                # At root level
                for i, n in enumerate(nodes):
                    if n.item_id == target_id:
                        return i
                return None
            
            # Navigate to parent
            current = nodes
            for path_idx in parent_path[:-1]:
                if 0 <= path_idx < len(current):
                    current = list(current[path_idx].children)
                else:
                    return None
            
            # Last level - find within parent's children
            if parent_path:
                last_idx = parent_path[-1]
                if 0 <= last_idx < len(current):
                    parent_node = current[last_idx]
                    for i, child in enumerate(parent_node.children):
                        if child.item_id == target_id:
                            return i
            return None
        
        # After extraction, find where the destination item is now
        # and recalculate the insertion point
        if dest_item_id:
            new_dest_idx = _find_item_index(current_root, dest_parent_path, dest_item_id)
            DEBUG_GUI and print(f"DEBUG _reorder_multi: dest_item_id={dest_item_id} new_dest_idx={new_dest_idx}")
            if new_dest_idx is not None:
                # Recalculate insert_idx based on the new position
                adjusted_insert_idx = new_dest_idx + (1 if insert_after else 0)
                DEBUG_GUI and print(f"DEBUG _reorder_multi: recalculated adjusted_insert_idx={adjusted_insert_idx}")
        
        # Adjust insertion index based on how many selected items were before it
        # (since we removed them, the index shifts)
        adjusted_insert_idx = insert_idx - items_before_insert
        adjusted_insert_idx = max(0, adjusted_insert_idx)
        
        DEBUG_GUI and print(f"DEBUG _reorder_multi: items_before_insert={items_before_insert} adjusted_insert_idx={adjusted_insert_idx}")
        DEBUG_GUI and print(f"DEBUG _reorder_multi: final adjusted_insert_idx={adjusted_insert_idx}")
        
        # Helper to find the index of an item by ID within a parent at a given path
        def _find_item_index(nodes: list[OutlineNode], parent_path: tuple[int, ...], target_id: str) -> int | None:
            """Find the index of an item by ID within its parent's children."""
            if not parent_path:
                # At root level
                for i, n in enumerate(nodes):
                    if n.item_id == target_id:
                        return i
                return None
            
            # Navigate to parent
            current = nodes
            for path_idx in parent_path[:-1]:
                if 0 <= path_idx < len(current):
                    current = list(current[path_idx].children)
                else:
                    return None
            
            # Last level - find within parent's children
            if parent_path:
                last_idx = parent_path[-1]
                if 0 <= last_idx < len(current):
                    parent_node = current[last_idx]
                    for i, child in enumerate(parent_node.children):
                        if child.item_id == target_id:
                            return i
            return None
        
        # After extraction, find where the destination item is now
        # and recalculate the insertion point
        if dest_item_id:
            new_dest_idx = _find_item_index(current_root, dest_parent_path, dest_item_id)
            DEBUG_GUI and print(f"DEBUG _reorder_multi: dest_item_id={dest_item_id} new_dest_idx={new_dest_idx}")
            if new_dest_idx is not None:
                # Recalculate insert_idx based on the new position
                adjusted_insert_idx = new_dest_idx + (1 if insert_after else 0)
                DEBUG_GUI and print(f"DEBUG _reorder_multi: recalculated adjusted_insert_idx={adjusted_insert_idx}")
        
        # Helper to insert items at a specific path
        def _insert_at_path(nodes: list[OutlineNode], parent_path: tuple[int, ...], idx: int, items: list[OutlineNode]) -> list[OutlineNode]:
            """Insert items at the specified index within the parent group."""
            DEBUG_GUI and print(f"DEBUG _insert_at_path: parent_path={parent_path} idx={idx} items={len(items)}")
            DEBUG_GUI and print(f"DEBUG _insert_at_path: parent_path={parent_path} idx={idx} items={len(items)}")
            if not parent_path:
                # Insert at root level
                result = nodes[:idx] + items + nodes[idx:]
                DEBUG_GUI and print(f"DEBUG _insert_at_path: root insert, result len={len(result)}")
                return result
            
            def _rebuild(ns: list[OutlineNode], p: tuple[int, ...]) -> list[OutlineNode]:
                if not p:
                    return ns
                path_idx = p[0]
                out_nodes: list[OutlineNode] = []
                for i, n in enumerate(ns):
                    if i != path_idx:
                        out_nodes.append(n)
                        continue
                    if len(p) == 1:
                        # This is the target parent - insert items into its children
                        new_kids = list(n.children)
                        new_kids = new_kids[:idx] + items + new_kids[idx:]
                        out_nodes.append(OutlineNode(label=n.label, item_id=n.item_id, children=new_kids, node_id=n.node_id, is_aggregate=n.is_aggregate))
                    else:
                        new_kids = _rebuild(list(n.children), p[1:])
                        out_nodes.append(OutlineNode(label=n.label, item_id=n.item_id, children=new_kids, node_id=n.node_id, is_aggregate=n.is_aggregate))
                return out_nodes
            
            return _rebuild(nodes, parent_path)
        
        # Create new leaf nodes for insertion (adopting destination group context)
        new_leaves = [OutlineNode(label=label or item_id, item_id=item_id, children=children)
                      for (item_id, label, children) in items_to_move]
        DEBUG_GUI and print(f"DEBUG _reorder_multi: creating {len(new_leaves)} new leaves")
        DEBUG_GUI and print(f"DEBUG _reorder_multi: creating {len(new_leaves)} new leaves")
        
        # Insert the items at the destination, adopting the new group membership
        final_root = _insert_at_path(current_root, dest_parent_path, adjusted_insert_idx, new_leaves)
        DEBUG_GUI and print(f"DEBUG _reorder_multi: final_root has {len(final_root)} nodes")
        
        # Prune empty groups after multi-item reorder (e.g., if items were extracted from a group)
        final_root = self._prune_empty_groups(final_root)
        DEBUG_GUI and print(f"DEBUG _reorder_multi: after pruning, final_root has {len(final_root)} nodes")
        
        self._set_outline_root(axis, final_root)
    

    def reorder_multiple_flat_items(self, axis: str, src_item_ids: list[str], dest_item_id: str, insert_after: bool) -> None:
        """Reorder multiple selected items as a group in the innermost flat dimension."""
        dim_id = self._axis_dim_id(axis)
        if dim_id is None:
            return

        # Guard against transient mode flips during drag/drop: rebuild axis
        # selection from dragged IDs before capture/restore.
        if self._sel_mode != axis:
            # Just fix the mode, don't change the selection - preserve existing indices
            self._sel_mode = axis
            if axis == "col":
                # Ensure we have a valid col selection
                if not self._sel_indices or not all(isinstance(i, int) and 0 <= i < len(self._cols) for i in self._sel_indices):
                    self._sel_indices = {self._sel_col} if 0 <= self._sel_col < len(self._cols) else {0} if self._cols else set()
            else:
                if not self._sel_indices or not all(isinstance(i, int) and 0 <= i < len(self._rows) for i in self._sel_indices):
                    self._sel_indices = {self._sel_row} if 0 <= self._sel_row < len(self._rows) else {0} if self._rows else set()

        dim = self._grid._workspace_read_model.get_dimension(dim_id)
        # Do not allow reordering for sequential dimensions.
        if not dim:
            return
        if dim.get("dim_type", "set") == "seq":
            return
        items = list(dim.get("items", []))
        
        # Find destination index
        dest_idx = None
        for i, item in enumerate(items):
            if item["id"] == dest_item_id:
                dest_idx = i
                break
        
        if dest_idx is None:
            return
        
        # Extract selected items in their current order
        selected_items = []
        remaining_items = []
        for item in items:
            if item["id"] in src_item_ids:
                selected_items.append(item)
            else:
                remaining_items.append(item)
        
        if not selected_items:
            return
        
        # Find new destination index in remaining items
        new_dest_idx = None
        for i, item in enumerate(remaining_items):
            if item["id"] == dest_item_id:
                new_dest_idx = i
                break
        
        if new_dest_idx is None:
            return
        
        # Insert selected items at new position
        insert_idx = new_dest_idx + (1 if insert_after else 0)
        for i, selected_item in enumerate(selected_items):
            remaining_items.insert(insert_idx + i, selected_item)
        
        # Update dimension via command spine
        new_order_ids = [it["id"] for it in remaining_items if it.get("id")]
        self._grid.execute_command(
            "set_dimension_item_order",
            dim_id=dim_id,
            item_ids=new_order_ids,
        )
        
        # Preserve selection across reload: save axis keys before reorder
        saved_sel_mode = self._sel_mode
        saved_sel_keys: set[tuple[str, ...]] = set()
        saved_sel_row = self._sel_row
        saved_sel_col = self._sel_col
        for idx in self._sel_indices:
            if isinstance(idx, int):
                if saved_sel_mode == "col" and 0 <= idx < len(self._cols):
                    if idx < len(self._col_keys):
                        key = self._col_keys[idx]
                        if isinstance(key, tuple):
                            saved_sel_keys.add(key)
                elif saved_sel_mode == "row" and 0 <= idx < len(self._rows):
                    if idx < len(self._row_keys):
                        key = self._row_keys[idx]
                        if isinstance(key, tuple):
                            saved_sel_keys.add(key)
        saved_anchor_row = self._anchor_row
        saved_anchor_col = self._anchor_col
        
        self.outline_changed.emit()
        self.reload()
        
        # Restore selection mode and indices (adjusted for new positions)
        if saved_sel_mode == "col":
            new_indices = set()
            for idx, key in enumerate(self._col_keys):
                if key in saved_sel_keys:
                    new_indices.add(idx)
            self._sel_mode = "col"
            self._sel_indices = new_indices
            if new_indices:
                self._sel_col = min(new_indices)
            elif self._cols:
                self._sel_col = min(saved_sel_col, max(0, len(self._cols) - 1))
                self._sel_indices = {self._sel_col}
        elif saved_sel_mode == "row":
            new_indices = set()
            for idx, key in enumerate(self._row_keys):
                if key in saved_sel_keys:
                    new_indices.add(idx)
            self._sel_mode = "row"
            self._sel_indices = new_indices
            if new_indices:
                self._sel_row = min(new_indices)
            elif self._rows:
                self._sel_row = min(saved_sel_row, max(0, len(self._rows) - 1))
                self._sel_indices = {self._sel_row}
        
        self._anchor_row = min(saved_anchor_row, max(0, len(self._rows) - 1)) if self._rows else 0
        self._anchor_col = min(saved_anchor_col, max(0, len(self._cols) - 1)) if self._cols else 0


    def ungroup_item(self, axis: str, item_id: str) -> None:
        """Remove item from group but preserve leaf ordering if at edge of group."""
        if not self._ensure_outline_axis(axis):
            return
        
        root = self._outline_root(axis)
        
        # Find the item's current path
        src_path = self._path_for_item_id(axis, item_id)
        if not src_path:
            # Item not found in outline, fall back to simple remove
            root2, leaf = self._remove_leaf_from_outline(root, item_id)
            if leaf is None:
                return
            root2.append(leaf)
            self._set_outline_root(axis, root2)
            return
        
        # Get parent path and check if at edge of group
        if len(src_path) < 2:
            # Already at root level, nothing to ungroup
            return
        
        parent_path = src_path[:-1]
        index_in_parent = src_path[-1]
        
        # Get parent node to check if at edge
        parent_node = self._get_node_at_path(root, parent_path)
        if parent_node is None or not parent_node.children:
            return
        
        is_at_edge = index_in_parent == 0 or index_in_parent == len(parent_node.children) - 1
        
        if is_at_edge:
            # Calculate insertion point in root to preserve leaf ordering
            # Count leaves before this position
            def count_leaves_before(nodes: list[OutlineNode], target_path: tuple[int, ...]) -> int:
                """Count leaves that appear before the target path."""
                count = 0
                current_path: list[int] = []
                
                def walk(ns: list[OutlineNode], depth: int) -> bool:
                    nonlocal count
                    for i, n in enumerate(ns):
                        current_path.append(i)
                        
                        # Check if we've passed the target
                        if len(current_path) == len(target_path):
                            if tuple(current_path) == target_path:
                                current_path.pop()
                                return True  # Found target, stop counting
                        
                        if n.children:
                            if walk(list(n.children), depth + 1):
                                current_path.pop()
                                return True
                        else:
                            count += 1
                        
                        current_path.pop()
                    return False
                
                walk(nodes, 0)
                return count
            
            leaves_before = count_leaves_before(root, src_path)
            
            # Remove the item from outline
            root2, leaf = self._remove_leaf_from_outline(root, item_id)
            if leaf is None:
                return
            
            # Insert at the calculated position to preserve ordering
            if leaves_before >= len(root2):
                root2.append(leaf)
            else:
                root2.insert(leaves_before, leaf)
            
            # Prune empty groups after removal
            root2 = self._prune_empty_groups(root2)
            self._set_outline_root(axis, root2)
        else:
            # Not at edge, use original behavior (append to root)
            root2, leaf = self._remove_leaf_from_outline(root, item_id)
            if leaf is None:
                return
            root2.append(leaf)
            # Prune empty groups after removal
            root2 = self._prune_empty_groups(root2)
            self._set_outline_root(axis, root2)


    def remove_any_node_from_outline(
        self, nodes: list[OutlineNode], path: tuple[int, ...]
    ) -> tuple[list[OutlineNode], OutlineNode | None]:
        if not path:
            return nodes, None
        i = path[0]
        if not (0 <= i < len(nodes)):
            return nodes, None
        if len(path) == 1:
            removed = nodes[i]
            return nodes[:i] + nodes[i + 1:], removed
        n = nodes[i]
        new_children, removed = self._remove_any_node_from_outline(list(n.children), path[1:])
        new_nodes = list(nodes)
        new_nodes[i] = OutlineNode(label=n.label, item_id=n.item_id, children=new_children, node_id=n.node_id, is_aggregate=n.is_aggregate)
        return new_nodes, removed


    def set_node_label_at_path(self, nodes: list[OutlineNode], path: tuple[int, ...], label: str) -> list[OutlineNode]:
        if not path:
            return nodes

        i = path[0]
        if not (0 <= i < len(nodes)):
            return nodes

        node = nodes[i]
        updated = list(nodes)
        if len(path) == 1:
            updated[i] = OutlineNode(label=label, item_id=node.item_id, children=list(node.children), node_id=node.node_id, is_aggregate=node.is_aggregate)
            return updated

        child_nodes = self._set_node_label_at_path(list(node.children), path[1:], label)
        updated[i] = OutlineNode(label=node.label, item_id=node.item_id, children=child_nodes, node_id=node.node_id, is_aggregate=node.is_aggregate)
        return updated


    def set_node_children_at_path(self, nodes: list[OutlineNode], path: tuple[int, ...], children: list[OutlineNode]) -> list[OutlineNode]:
        if not path:
            return nodes

        def _rebuild(ns: list[OutlineNode], p: tuple[int, ...]) -> list[OutlineNode]:
            idx = p[0]
            out_nodes: list[OutlineNode] = []
            for i, n in enumerate(ns):
                if i != idx:
                    out_nodes.append(n)
                    continue
                if len(p) == 1:
                    out_nodes.append(OutlineNode(label=n.label, item_id=n.item_id, children=children, node_id=n.node_id, is_aggregate=n.is_aggregate))
                else:
                    new_kids = _rebuild(list(n.children), p[1:])
                    out_nodes.append(OutlineNode(label=n.label, item_id=n.item_id, children=new_kids, node_id=n.node_id, is_aggregate=n.is_aggregate))
            return out_nodes

        return _rebuild(nodes, path)


    def get_node_at_path(self, nodes: list[OutlineNode], path: tuple[int, ...]) -> OutlineNode | None:
        arr = nodes
        cur: OutlineNode | None = None
        for i in path:
            if not (0 <= i < len(arr)):
                return None
            cur = arr[i]
            arr = list(cur.children)
        return cur


    def leaf_node_for_item(self, axis: str, item_id: str) -> OutlineNode | None:
        did = self._axis_dim_id(axis)
        if did is None:
            return None
        dim = self._grid._workspace_read_model.get_dimension(did)
        if not dim:
            return None
        items = dim.get("items", [])
        it = next((it for it in items if it["id"] == item_id), None)
        if it is None:
            return None
        return OutlineNode(label=it["name"], item_id=it["id"], children=[])


    def insert_group_at_path(
        self,
        root: list[OutlineNode],
        path: tuple[int, ...],
        node: OutlineNode,
    ) -> bool:
        if not path:
            return False
        i = path[0]
        if not (0 <= i < len(root)):
            return False
        n = root[i]
        if len(path) == 1:
            if n.item_id is None:
                n.children.append(node)
                return True
            return False
        children = list(n.children)
        if self._insert_group_at_path(children, path[1:], node):
            root[i] = OutlineNode(label=n.label, item_id=n.item_id, children=children)
            return True
        return False


    def move_item_to_group(self, axis: str, item_id: str, group_path: tuple[int, ...]) -> None:
        """Move a single item to a group. For multiple items, use move_multiple_items_to_group."""
        self.move_multiple_items_to_group(axis, [item_id], group_path)

    def move_multiple_items_to_group(
        self, axis: str, item_ids: list[str], group_path: tuple[int, ...]
    ) -> None:
        """Move multiple items to a group atomically.

        Graph-first: uses move_root_to_group / move_edge primitives.
        """
        DEBUG_GUI and print(f"DEBUG move_multiple: ENTER item_ids={item_ids} group_path={group_path}")
        if not item_ids:
            return
        if not self._ensure_outline_axis(axis):
            DEBUG_GUI and print(f"DEBUG move_multiple: ensure_outline_axis failed")
            return

        dim_id = self._axis_dim_id(axis)
        if not dim_id:
            return

        # Get target group node_id
        root = self._outline_root(axis)
        group_node = self._get_node_at_path(root, group_path)
        if group_node is None or getattr(group_node, 'node_id', None) is None:
            return  # Phase 4: graph is canonical, outline must be synced

        group_node_id = group_node.node_id
        self._grid.execute_command(
            "move_items_to_group",
            dim_id=dim_id,
            item_ids=item_ids,
            group_ref={"kind": "id", "value": group_node_id},
        )

    def _count_items_before_target(
        self, src_paths: list[tuple[int, ...] | None], target_path: tuple[int, ...]
    ) -> int:
        """Count how many source items are positioned before the target."""
        count = 0
        for src_path in src_paths:
            if src_path is None:
                continue
            # Must be at same depth as target
            if len(src_path) != len(target_path):
                continue
            # Must share same parent (for targets with depth > 0)
            if len(src_path) > 1 and src_path[:-1] != target_path[:-1]:
                continue
            # Check if source is before target
            if src_path[-1] < target_path[-1]:
                count += 1
        return count

    def _adjust_path_for_multiple_removals(
        self, target_path: tuple[int, ...], removals_before: int
    ) -> tuple[int, ...]:
        """Adjust target path after removing N items before it at the same level."""
        if removals_before <= 0:
            return target_path
        target_index = target_path[-1]
        new_index = max(0, target_index - removals_before)
        return target_path[:-1] + (new_index,)


    def remove_leaf_from_outline(self, nodes: list[OutlineNode], item_id: str) -> tuple[list[OutlineNode], OutlineNode | None]:
        removed: OutlineNode | None = None

        def _walk(ns: list[OutlineNode]) -> list[OutlineNode]:
            nonlocal removed
            new: list[OutlineNode] = []
            for n in ns:
                if removed is None and n.item_id == item_id and not n.children:
                    removed = n
                    continue
                if n.children:
                    kids = _walk(list(n.children))
                    new.append(OutlineNode(label=n.label, item_id=n.item_id, children=kids, node_id=n.node_id, is_aggregate=n.is_aggregate))
                else:
                    new.append(n)
            return new

        return _walk(nodes), removed


    def ungroup_group(self, axis: str, group_path: tuple[int, ...]) -> None:
        """Remove a group and move all its children to the parent level."""
        if not self._ensure_outline_axis(axis):
            return

        root = self._outline_root(axis)
        if not group_path:
            return

        # Get the group node and its parent path
        parent_path = tuple(group_path[:-1])
        group_index = group_path[-1]

        # Navigate to the parent node
        if parent_path:
            parent_node = self._get_node_at_path(root, parent_path)
            if parent_node is None or not parent_node.children:
                return
            children = parent_node.children
        else:
            # Group is at root level
            children = root

        if group_index >= len(children):
            return

        group_node = children[group_index]
        if group_node.item_id is not None:
            return  # Not a group, it's a leaf

        # Get the group label BEFORE removing it - needed for rule updates
        group_label = group_node.label or ""

        # Update rules to show #REF! for references to this group
        # Must be done BEFORE removing the group from the outline
        print(f"[UNGROUP_GROUP] group_label={group_label}, axis={axis}")
        if group_label:
            dim_id = self._axis_dim_id(axis)
            print(f"[UNGROUP_GROUP] dim_id={dim_id}")

        # Phase 6: delegate ungroup to Engine API
        if group_node and getattr(group_node, 'node_id', None) and dim_id:
            # Collect all item_ids inside this group and ungroup them via Engine
            item_ids = []
            for child in group_node.children:
                if child.item_id:
                    item_ids.append(child.item_id)
            if item_ids:
                self._grid.execute_command(
                    "ungroup_items",
                    dim_id=dim_id,
                    item_ids=item_ids,
                )
                # Delete the now-empty group node
                self._grid.execute_command(
                    "delete_group_node",
                    dim_id=dim_id,
                    node_id=group_node.node_id,
                    promote_children="to_parent",
                )
            else:
                # No items; just delete the empty group
                self._grid.execute_command(
                    "delete_group_node",
                    dim_id=dim_id,
                    node_id=group_node.node_id,
                    promote_children="to_parent",
                )
        else:
            # Fallback to old outline-tree path during transition
            group_children = list(group_node.children) if group_node.children else []
            new_children = []
            for i, child in enumerate(children):
                if i == group_index:
                    new_children.extend(group_children)
                else:
                    new_children.append(child)
            if parent_path:
                root = self._set_node_children_at_path(root, parent_path, new_children)
            else:
                root = new_children
            self._set_outline_root(axis, root)


    def insert_before(self, axis: str) -> None:
        self._insert_dimension_items_relative_to_selection(axis, insert_after=False)


    def insert_after(self, axis: str) -> None:
        self._insert_dimension_items_relative_to_selection(axis, insert_after=True)
    

