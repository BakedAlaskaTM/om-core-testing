"""Header editing helpers for the matrix grid."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from PySide6 import QtCore, QtGui, QtWidgets

# Debug flag for GUI - set DEBUG_GUI=true to enable verbose logging
DEBUG_GUI = os.environ.get("DEBUG_GUI", "false").lower() in ("true", "1", "yes")

if TYPE_CHECKING:
    pass




class HeaderEditHelper:
    """Helper methods for header editing operations."""

    def __init__(self, grid: "MatrixGrid") -> None:
        self._grid = grid

    def header_hit(self, pos: QtCore.QPoint) -> tuple[str, str | tuple[int, ...] | None] | None:
        """Determine what header element (if any) is at position `pos`.

        Returns a tuple (kind, payload) where kind is one of:
        - "row_leaf": payload is (item_id, row_index)
        - "row_group": payload is (path, r0, r1, r)
        - "col_leaf": payload is col_index
        - "col_group": payload is (path, c0, c1)
        - "row_bg", "col_bg": background area (no payload)
        - None: not a header area
        """
        off = self._grid._geometry.scroll_offset()
        x_view = pos.x()
        y_view = pos.y()
        x = x_view + off.x()
        y = y_view + off.y()
        header_h = self._grid._m.col_header_h * max(1, self._grid._col_header_levels)
        row_header_w = self._grid._geometry.row_header_width()

        if x_view < row_header_w and y_view >= header_h:
            # Determine row-header level using actual widths per level
            cumulative = 0
            level = 0
            for lvl in range(max(1, self._grid._row_header_levels)):
                level_width = self._grid._geometry.row_header_level_width(lvl)
                cumulative += level_width
                print(f"[DEBUG header_hit] level calc: lvl={lvl}, level_width={level_width}, cumulative={cumulative}, x_view={x_view}")
                if x_view < cumulative:
                    level = lvl
                    break
            r = int((y - header_h) // self._grid._m.row_h)
            print(f"[DEBUG header_hit] final level={level}, r={r}, row_header_levels={self._grid._row_header_levels}")
            if level >= self._grid._row_band_levels:
                # Leaf column
                if 0 <= r < len(self._grid._rows):
                    row = self._grid._rows[r]
                    if row.get("is_leaf", False):
                        iid = row.get("item_id")
                        return ("row_leaf", (iid, r))
                return ("row_bg", None)
            else:
                # Band column — find which group spans this row at this level
                if 0 <= r < len(self._grid._rows):
                    for band in self._grid._row_bands:
                        if int(band.get("level", -1)) != level:
                            continue
                        r0 = int(band.get("r0", -1))
                        r1 = int(band.get("r1", -2))
                        if r0 <= r <= r1:
                            path = band.get("path")
                            label = band.get("label", "")
                            # DEBUG: Check what path actually is
                            print(f"[DEBUG header_hit] CHECKING: path={path}, type={type(path).__name__}, len={len(path) if isinstance(path, tuple) else 'N/A'}")
                            # Check if this is a leaf band (path len 1) vs group (path len > 1)
                            if isinstance(path, tuple) and len(path) > 1:
                                print(f"[DEBUG header_hit] row_group MATCHED: path={path}, label={label}, level={level}, r={r}, r range {r0}-{r1}")
                                return ("row_group", (path, r0, r1, r))
                            elif isinstance(path, tuple) and len(path) == 1:
                                # In stacked mode, single-element paths at band levels are group headers
                                # Only treat as leaf if we're at the leaf column level (level >= row_band_levels)
                                if level < self._grid._row_band_levels:
                                    # This is a group band in stacked mode
                                    print(f"[DEBUG header_hit] row_group (stacked) MATCHED: path={path}, label={label}, level={level}, r={r}, r range {r0}-{r1}")
                                    return ("row_group", (path, r0, r1, r))
                                # Leaf band - treat as leaf, not group
                                print(f"[DEBUG header_hit] row_leaf (from band) MATCHED: path={path}, label={label}, level={level}, r={r}")
                                row = self._grid._rows[r]
                                if row.get("is_leaf", False):
                                    iid = row.get("item_id")
                                    return ("row_leaf", (iid, r))
                            else:
                                print(f"[DEBUG header_hit] UNEXPECTED path type: {path}, skipping")
                    print(f"[DEBUG header_hit] row_group NO MATCH: r={r}, level={level}, bands={len(self._grid._row_bands)}")
                    return ("row_bg", None)

        if y_view < header_h and x_view >= row_header_w:
            # Determine column index using actual widths (handles repeating item_ids across groups)
            col_x = row_header_w
            c = -1
            for i in range(len(self._grid._cols)):
                col_w = self._grid._geometry.col_width(i)
                if col_x <= x < col_x + col_w:
                    c = i
                    break
                col_x += col_w

            if self._grid._col_band_levels > 0:
                level = int(y_view // self._grid._m.col_header_h)
                if level >= self._grid._col_band_levels:
                    # Leaf header row
                    if 0 <= c < len(self._grid._cols):
                        DEBUG_GUI and print(f"DEBUG header_hit: col_leaf c={c}, level={level}, x={x}, y={y}")
                        return ("col_leaf", c)
                    return ("col_bg", None)
                # Band row — find which group spans column c at this level (use actual widths for c0/c1)
                for band in self._grid._col_bands:
                    if int(band.get("level", -1)) != level:
                        continue
                    c0 = int(band.get("c0", -1))
                    c1 = int(band.get("c1", -2))
                    if c0 <= c <= c1:
                        path = band.get("path")
                        if isinstance(path, tuple) and path:
                            DEBUG_GUI and print(f"DEBUG header_hit: col_group path={path}, level={level}, c range {c0}-{c1}, x={x}, y={y}")
                            return ("col_group", (path, c0, c1))
                return ("col_bg", None)
            else:
                # No bands — entire header area is leaf row
                if 0 <= c < len(self._grid._cols):
                    DEBUG_GUI and print(f"DEBUG header_hit: col_leaf(no bands) c={c}, x={x}, y={y}")
                    return ("col_leaf", c)
                return ("col_bg", None)

        return None

    def rename_header_hit(self, hit: tuple[str, str | tuple[int, ...] | None]) -> bool:
        """Rename a header element based on hit result."""
        kind, payload = hit
        print(f"DEBUG rename_header_hit: kind={kind}, payload={payload}")

        if kind in {"row_group", "col_group", "row_leaf", "col_leaf"}:
            axis = "row" if kind in {"row_group", "row_leaf"} else "col"
            
            # Check if we're in stacked dimension mode
            view = self._grid._workspace_read_model.get_view(self._grid._view_id)
            dim_ids = list(view.get(f"{axis}_dim_ids", []) or []) if view else []
            
            # If stacked dimensions (more than 1 dimension), force popup dialog for all edits
            if len(dim_ids) > 1:
                print(f"DEBUG rename_header_hit: stacked mode detected ({len(dim_ids)} dims), forcing popup dialog")
                # Handle leaf clicks in stacked mode - open rename dialog for the underlying item
                if kind in {"row_leaf", "col_leaf"}:
                    return self._rename_leaf_in_stacked_mode(kind, payload, dim_ids)
                # For group clicks in stacked mode, use the popup dialog flow below
            axis = "row" if kind == "row_group" else "col"
            group_path = payload[0] if isinstance(payload[0], tuple) else payload
            if not isinstance(group_path, tuple) or not group_path:
                return False

            # Check if this is a "true" group (node without item_id)
            # SKIP outline check in stacked mode - outline structure doesn't match band structure
            root = self._grid._outline_root(axis)
            node = self._grid._get_node_at_path(root, group_path) if root else None
            print(f"DEBUG rename_header_hit: outline node check - root={root is not None}, node={node}, item_id={node.item_id if node else None}")
            
            # Only use outline-based rename for non-stacked dimensions (single dimension)
            if len(dim_ids) == 1 and node is not None and node.item_id is None:
                current = str(node.label or "")
                title = "Rename Row Group" if axis == "row" else "Rename Column Group"
                new_name, ok = QtWidgets.QInputDialog.getText(
                    self._grid, title, "Group name", text=current
                )
                if not ok or not new_name.strip() or new_name.strip() == current:
                    return True

                # Phase 8: command dispatcher handles mutation
                dim_id = self._grid._axis_dim_id(axis)
                if isinstance(dim_id, str):
                    node = self._grid._get_node_at_path(root, group_path)
                    node_id = getattr(node, 'node_id', None) if node else None
                    if node_id:
                        result = self._grid.execute_command(
                            "rename_group_node",
                            dim_id=dim_id,
                            node_id=node_id,
                            new_label=new_name.strip(),
                        )
                        if not result.success:
                            QtWidgets.QMessageBox.warning(
                                self._grid,
                                "Duplicate Group Label",
                                result.error or "Rename failed",
                                QtWidgets.QMessageBox.StandardButton.Ok,
                            )
                            return False
                    else:
                        # Fallback to old outline-tree path during transition
                        updated_root = self._grid._set_node_label_at_path(root, group_path, new_name.strip())
                        self._grid._set_outline_root(axis, updated_root)
                self._grid.reload()
                self._grid.outline_changed.emit()
                return True

            print(f"DEBUG rename_header_hit: falling through to stacked-dimension fallback")

            # Stacked-dimension band fallback: rename the group label or underlying dimension item
            view = self._grid._workspace_read_model.get_view(self._grid._view_id)
            dim_ids = list(view.get(f"{axis}_dim_ids", []) or []) if view else []
            if not dim_ids:
                return False

            # If path is a single-element tuple like (1,), this is actually a leaf-level item
            # being reported as "group" due to header hit detection. Route to leaf handler.
            if isinstance(group_path, tuple) and len(group_path) == 1:
                print(f"DEBUG rename_header_hit: single-element path {group_path}, routing to leaf handler")
                # For stacked mode, we need to determine which dimension was clicked based on level
                # and extract the correct item from the row_key tuple
                if axis == "row":
                    clicked_r = payload[3] if len(payload) > 3 and isinstance(payload[3], int) else None
                    if clicked_r is not None and 0 <= clicked_r < len(self._grid._rows):
                        # Get the row key tuple which contains items from all dimensions
                        if 0 <= clicked_r < len(self._grid._row_keys):
                            row_key = self._grid._row_keys[clicked_r]
                            # Determine which dimension index corresponds to the clicked level
                            # Level 0 = first dimension's groups, Level N-1 = last dimension's leaf
                            # For a single-element path with level info, we need to find which dim this is
                            # The level in stacked mode corresponds to the dimension index
                            level = self._get_level_from_header_pos(axis, clicked_r, group_path)
                            if level is not None and 0 <= level < len(row_key):
                                iid = row_key[level]
                                if isinstance(iid, str):
                                    return self._rename_leaf_in_stacked_mode("row_leaf", (iid, clicked_r), dim_ids)
                else:  # col
                    clicked_c = payload[2] if len(payload) > 2 and isinstance(payload[2], int) else None
                    if clicked_c is not None and 0 <= clicked_c < len(self._grid._col_keys):
                        col_key = self._grid._col_keys[clicked_c]
                        level = self._get_level_from_header_pos(axis, clicked_c, group_path)
                        if level is not None and 0 <= level < len(col_key):
                            iid = col_key[level]
                            if isinstance(iid, str):
                                return self._rename_leaf_in_stacked_mode("col_leaf", (iid, clicked_c), dim_ids)
                return False

            level: int | None = None
            item_id: str | None = None
            band_label: str = ""
            if axis == "row":
                r0 = payload[1] if len(payload) > 1 and isinstance(payload[1], int) else None
                r1 = payload[2] if len(payload) > 2 and isinstance(payload[2], int) else r0
                clicked_r = payload[3] if len(payload) > 3 and isinstance(payload[3], int) else r0
                
                # Find the correct band by matching path AND checking if clicked row is in band's range
                matching_band = None
                for band in self._grid._row_bands:
                    if band.get("path") == group_path:
                        band_r0 = band.get("r0", -1)
                        band_r1 = band.get("r1", -1)
                        # Check if this band contains the clicked row
                        if band_r0 <= clicked_r <= band_r1:
                            matching_band = band
                            break
                
                if matching_band:
                    level = int(matching_band.get("level", -1))
                    band_label = str(matching_band.get("label", ""))
                else:
                    # Fallback: just find by path (old behavior)
                    for band in self._grid._row_bands:
                        if band.get("path") == group_path:
                            level = int(band.get("level", -1))
                            band_label = str(band.get("label", ""))
                            break
                if level is None or not (0 <= level < len(dim_ids)) or clicked_r is None:
                    return False
                
                print(f"DEBUG rename_header_hit: dim_ids={dim_ids}, level={level}, band_label={band_label!r}")
                
                # Use label_paths to properly map band level to dimension
                row = self._grid._rows[clicked_r]
                label_paths = list(row.get("label_paths") or [])
                labels = list(row.get("labels") or [])
                
                print(f"DEBUG rename_header_hit: row_labels={labels}, label_paths={label_paths}, band_level={level}")
                
                # Find the correct index in labels that matches the band_label
                # The band_label is the actual text shown, find where it appears in labels
                # Use band_level to help identify which dimension this band corresponds to
                target_idx = None
                expected_dim_idx = level % len(dim_ids) if dim_ids else 0
                
                for i, lab in enumerate(labels):
                    if lab == band_label:
                        # Verify this is at the right band level by checking label_paths
                        if i < len(label_paths):
                            path = label_paths[i]
                            if isinstance(path, tuple) and len(path) > 0:
                                # Check if this path's dimension matches the expected level
                                if path[0] == expected_dim_idx:
                                    target_idx = i
                                    break
                
                # If not found with strict matching, try looser match
                if target_idx is None:
                    for i, lab in enumerate(labels):
                        if lab == band_label:
                            if i < len(label_paths):
                                path = label_paths[i]
                                if isinstance(path, tuple) and len(path) > 0:
                                    target_idx = i
                                    break
                
                if target_idx is None:
                    print(f"DEBUG rename_header_hit: could not find band_label={band_label!r} in labels")
                    return False
                    
                path = label_paths[target_idx]
                target_label = labels[target_idx]
                
                print(f"DEBUG rename_header_hit: target_idx={target_idx}, path={path}, target_label={target_label!r}")
                
                if not isinstance(path, tuple) or len(path) == 0:
                    return False
                    
                # Extract dimension index from path
                dim_idx = path[0]
                if not (0 <= dim_idx < len(dim_ids)):
                    return False
                    
                item_dim_id = dim_ids[dim_idx]
                
                # Check if this is a group label or item name
                is_group_label = len(path) == 2  # (dim_idx, group_depth) vs (dim_idx,)
                
                print(f"DEBUG rename_header_hit: dim_idx={dim_idx}, item_dim_id={item_dim_id}, is_group_label={is_group_label}")
                
                if is_group_label:
                    # In stacked mode, group labels correspond to dimension items
                    # Find the dimension item at this position and rename it
                    leaf_idx = self._grid._leaf_row_index(clicked_r)
                    if 0 <= leaf_idx < len(self._grid._row_keys):
                        row_key = self._grid._row_keys[leaf_idx]
                        if dim_idx < len(row_key):
                            item_id = row_key[dim_idx]
                            dim = self._grid._workspace_read_model.get_dimension(item_dim_id)
                            items = dim.get("items", []) if dim else []
                            item = next((it for it in items if it["id"] == item_id), None)
                            
                            if item is not None:
                                # Show dialog to rename the underlying item (not just the group label)
                                title = "Rename Item"
                                new_name, ok = QtWidgets.QInputDialog.getText(
                                    self._grid, title, "Name", text=item["name"]
                                )
                                if not ok or not new_name.strip() or new_name.strip() == item["name"]:
                                    return True
                                    
                                result = self._grid.execute_command(
                                    "rename_dimension_item",
                                    dim_id=item_dim_id,
                                    item_id=item["id"],
                                    new_name=new_name.strip(),
                                )
                                if not result.success:
                                    return False
                                self._grid.reload()
                                self._grid.outline_changed.emit()
                                return True
                else:
                    # It's an item name - rename the dimension item
                    dim = self._grid._workspace_read_model.get_dimension(item_dim_id)
                    items = dim.get("items", []) if dim else []
                    item = next((it for it in items if it["name"] == target_label), None)
                    
                    if item is not None:
                        title = "Rename Item"
                        new_name, ok = QtWidgets.QInputDialog.getText(
                            self._grid, title, "Name", text=target_label
                        )
                        if not ok or not new_name.strip() or new_name.strip() == target_label:
                            return True

                        result = self._grid.execute_command(
                            "rename_dimension_item",
                            dim_id=item_dim_id,
                            item_id=item["id"],
                            new_name=new_name.strip(),
                        )
                        if not result.success:
                            return False
                        self._grid.reload()
                        self._grid.outline_changed.emit()
                        return True

                # Fallback: try the original search method
                for did in dim_ids:
                    dim = self._grid._workspace_read_model.get_dimension(did)
                    items = dim.get("items", []) if dim else []
                    found_item = next((it for it in items if it["name"] == band_label), None)
                    if found_item is not None:
                        item = found_item
                        item_dim_id = did
                        break
                
                print(f"DEBUG rename_header_hit: found item.name={item['name']!r} in dim={item_dim_id}, band_label={band_label!r}")
                
                # Show dialog with the BAND LABEL (what user sees), but rename the ITEM
                title = "Rename Row" if axis == "row" else "Rename Column"
                new_name, ok = QtWidgets.QInputDialog.getText(self._grid, title, "Name", text=band_label)
                if not ok or not new_name.strip() or new_name.strip() == band_label:
                    return True
                    
                # Rename the item (not the band label)
                result = self._grid.execute_command(
                    "rename_dimension_item",
                    dim_id=item_dim_id,
                    item_id=item["id"],
                    new_name=new_name.strip(),
                )
                if not result.success:
                    return False
                self._grid.reload()
                self._grid.outline_changed.emit()
                return True
            else:
                c0 = payload[1] if len(payload) > 1 and isinstance(payload[1], int) else None
                c1 = payload[2] if len(payload) > 2 and isinstance(payload[2], int) else c0
                clicked_c = payload[3] if len(payload) > 3 and isinstance(payload[3], int) else c0
                
                # Find the correct band by matching path AND checking if clicked column is in band's range
                matching_band = None
                for band in self._grid._col_bands:
                    if band.get("path") == group_path:
                        band_c0 = band.get("c0", -1)
                        band_c1 = band.get("c1", -1)
                        # Check if this band contains the clicked column
                        if band_c0 <= clicked_c <= band_c1:
                            matching_band = band
                            break
                
                if matching_band:
                    level = int(matching_band.get("level", -1))
                    band_label = str(matching_band.get("label", ""))
                else:
                    # Fallback: just find by path (old behavior)
                    for band in self._grid._col_bands:
                        if band.get("path") == group_path:
                            level = int(band.get("level", -1))
                            band_label = str(band.get("label", ""))
                            break
                
                if level is None or not (0 <= level < len(dim_ids)) or clicked_c is None:
                    return False
                    
                print(f"DEBUG rename_header_hit: col dim_ids={dim_ids}, level={level}, band_label={band_label!r}")
                
                # Use label_paths to properly map band level to dimension
                col = self._grid._cols[clicked_c]
                label_paths = list(col.get("label_paths") or [])
                labels = list(col.get("labels") or [])
                
                print(f"DEBUG rename_header_hit: col_labels={labels}, label_paths={label_paths}, band_level={level}")
                
                # Find the correct index in labels that matches the band_label
                target_idx = None
                expected_dim_idx = level % len(dim_ids) if dim_ids else 0
                
                for i, lab in enumerate(labels):
                    if lab == band_label:
                        if i < len(label_paths):
                            path = label_paths[i]
                            if isinstance(path, tuple) and len(path) > 0:
                                if path[0] == expected_dim_idx:
                                    target_idx = i
                                    break
                
                # If not found with strict matching, try looser match
                if target_idx is None:
                    for i, lab in enumerate(labels):
                        if lab == band_label:
                            if i < len(label_paths):
                                path = label_paths[i]
                                if isinstance(path, tuple) and len(path) > 0:
                                    target_idx = i
                                    break
                
                if target_idx is None:
                    print(f"DEBUG rename_header_hit: could not find band_label={band_label!r} in col labels")
                    return False
                    
                path = label_paths[target_idx]
                target_label = labels[target_idx]
                
                print(f"DEBUG rename_header_hit: col target_idx={target_idx}, path={path}, target_label={target_label!r}")
                
                if not isinstance(path, tuple) or len(path) == 0:
                    return False
                    
                dim_idx = path[0]
                if not (0 <= dim_idx < len(dim_ids)):
                    return False
                    
                item_dim_id = dim_ids[dim_idx]
                
                is_group_label = len(path) == 2
                
                print(f"DEBUG rename_header_hit: col dim_idx={dim_idx}, item_dim_id={item_dim_id}, is_group_label={is_group_label}")
                
                if is_group_label:
                    # In stacked mode, group labels correspond to dimension items
                    # Find the dimension item at this position and rename it
                    col_key = self._grid._col_keys[clicked_c]
                    if dim_idx < len(col_key):
                        item_id = col_key[dim_idx]
                        dim = self._grid._workspace_read_model.get_dimension(item_dim_id)
                        items = dim.get("items", []) if dim else []
                        item = next((it for it in items if it["id"] == item_id), None)
                        
                        if item is not None:
                            # Show dialog to rename the underlying item (not just the group label)
                            title = "Rename Item"
                            new_name, ok = QtWidgets.QInputDialog.getText(
                                self._grid, title, "Name", text=item["name"]
                            )
                            if not ok or not new_name.strip() or new_name.strip() == item["name"]:
                                return True

                            result = self._grid.execute_command(
                                "rename_dimension_item",
                                dim_id=item_dim_id,
                                item_id=item["id"],
                                new_name=new_name.strip(),
                            )
                            if not result.success:
                                return False
                            self._grid.reload()
                            self._grid.outline_changed.emit()
                            return True
                else:
                    dim = self._grid._workspace_read_model.get_dimension(item_dim_id)
                    items = dim.get("items", []) if dim else []
                    item = next((it for it in items if it["name"] == target_label), None)
                    
                    if item is not None:
                        title = "Rename Item"
                        new_name, ok = QtWidgets.QInputDialog.getText(
                            self._grid, title, "Name", text=target_label
                        )
                        if not ok or not new_name.strip() or new_name.strip() == target_label:
                            return True

                        result = self._grid.execute_command(
                            "rename_dimension_item",
                            dim_id=item_dim_id,
                            item_id=item["id"],
                            new_name=new_name.strip(),
                        )
                        if not result.success:
                            return False
                        self._grid.reload()
                        self._grid.outline_changed.emit()
                        return True

                # Fallback
                for did in dim_ids:
                    dim = self._grid._workspace_read_model.get_dimension(did)
                    items = dim.get("items", []) if dim else []
                    found_item = next((it for it in items if it["name"] == band_label), None)
                    if found_item is not None:
                        item = found_item
                        item_dim_id = did
                        break
                
                print(f"DEBUG rename_header_hit: col found item.name={item['name']!r} in dim={item_dim_id}")
                
                title = "Rename Column"
                new_name, ok = QtWidgets.QInputDialog.getText(self._grid, title, "Name", text=band_label)
                if not ok or not new_name.strip() or new_name.strip() == band_label:
                    return True

                result = self._grid.execute_command(
                    "rename_dimension_item",
                    dim_id=item_dim_id,
                    item_id=item["id"],
                    new_name=new_name.strip(),
                )
                if not result.success:
                    return False
                self._grid.reload()
                self._grid.outline_changed.emit()
                return True

        elif kind in {"row_leaf", "col_leaf"}:
            # For leaf items, use the pending edit mechanism
            if kind == "row_leaf" and isinstance(payload, tuple) and len(payload) > 1:
                index = payload[1] if isinstance(payload[1], int) else 0
            elif isinstance(payload, int):
                index = payload
            else:
                index = 0

            self._grid._header_edit_ctx = {
                "axis": "row" if kind == "row_leaf" else "col",
                "index": index,
                "saved_sel_mode": self._grid._sel_mode,
                "saved_sel_row": self._grid._sel_row,
                "saved_sel_col": self._grid._sel_col,
                "saved_sel_indices": set(self._grid._sel_indices),
            }
            self._start_pending_header_edit()
            return True

        return False

    def _rename_leaf_in_stacked_mode(
        self, kind: str, payload: str | tuple[int, ...] | None, dim_ids: list[str]
    ) -> bool:
        """Rename a leaf header item in stacked dimension mode using a popup dialog.

        Args:
            kind: "row_leaf" or "col_leaf"
            payload: For row_leaf: (item_id, row_index); for col_leaf: col_index or item_id
            dim_ids: List of dimension IDs for this axis

        Returns:
            True if rename was successful, False otherwise
        """
        print(f"DEBUG _rename_leaf_in_stacked_mode: kind={kind}, payload={payload}")

        # Extract item_id from payload
        item_id: str | None = None
        current_name: str = ""

        if kind == "row_leaf":
            # payload is (item_id, row_index)
            if isinstance(payload, tuple) and len(payload) > 0 and isinstance(payload[0], str):
                item_id = payload[0]
            else:
                print(f"DEBUG _rename_leaf_in_stacked_mode: invalid row_leaf payload")
                return False
        else:  # col_leaf
            # payload is col_index (int) or item_id (str)
            if isinstance(payload, str):
                item_id = payload
            elif isinstance(payload, int):
                # Look up item_id from column
                if 0 <= payload < len(self._grid._cols):
                    item_id = self._grid._cols[payload].get("item_id")
            elif isinstance(payload, tuple) and len(payload) > 0:
                item_id = payload[0] if isinstance(payload[0], str) else None

        if not item_id:
            print(f"DEBUG _rename_leaf_in_stacked_mode: could not extract item_id")
            return False

        # Find which dimension this item belongs to
        target_dim_id: str | None = None
        item_name: str | None = None

        for dim_id in dim_ids:
            dim = self._grid._workspace_read_model.get_dimension(dim_id)
            items = dim.get("items", []) if dim else []
            for item in items:
                if item["id"] == item_id:
                    target_dim_id = dim_id
                    item_name = item["name"]
                    break
            if target_dim_id:
                break

        if not target_dim_id or not item_name:
            print(f"DEBUG _rename_leaf_in_stacked_mode: could not find item {item_id} in any dimension")
            return False

        print(f"DEBUG _rename_leaf_in_stacked_mode: found item {item_id} in dim {target_dim_id}, name={item_name!r}")

        # Save current selection state before showing dialog
        saved_sel_mode = self._grid._sel_mode
        saved_sel_row = self._grid._sel_row
        saved_sel_col = self._grid._sel_col
        saved_sel_indices = set(self._grid._sel_indices)

        # Show rename dialog
        axis = "row" if kind == "row_leaf" else "col"
        title = "Rename Row" if axis == "row" else "Rename Column"
        new_name, ok = QtWidgets.QInputDialog.getText(self._grid, title, "Name", text=item_name)

        if not ok or not new_name.strip() or new_name.strip() == item_name:
            return True  # Cancelled or no change

        result = self._grid.execute_command(
            "rename_dimension_item",
            dim_id=target_dim_id,
            item_id=item_id,
            new_name=new_name.strip(),
        )
        if not result.success:
            print(f"DEBUG _rename_leaf_in_stacked_mode: error renaming: {result.error}")
            return False
        self._grid.reload()
        # reload() already handles selection restoration by item_id, so no need to manually restore
        self._grid.outline_changed.emit()
        print(f"DEBUG _rename_leaf_in_stacked_mode: renamed {item_id} to {new_name!r}")
        return True

    def _get_level_from_header_pos(
        self, axis: str, index: int, group_path: tuple[int, ...]
    ) -> int | None:
        """Determine which dimension level corresponds to a clicked header position.

        In stacked mode, each header level corresponds to a different dimension.
        This method looks at the row/column structure to determine which dimension
        index matches the clicked level.

        Args:
            axis: "row" or "col"
            index: The row or column index that was clicked
            group_path: The path tuple from header hit detection

        Returns:
            The dimension index (0-based) or None if cannot determine
        """
        if axis == "row":
            if not (0 <= index < len(self._grid._rows)):
                return None
            row = self._grid._rows[index]
            labels = list(row.get("labels") or [])
            label_paths = list(row.get("label_paths") or [])

            # For a single-element path like (1,), find which label has this path
            # and determine its dimension from label_paths
            for i, path in enumerate(label_paths):
                if isinstance(path, tuple) and len(path) == 1:
                    # This is a leaf-level item - path[0] is the dimension index
                    if path == group_path:
                        return path[0] if len(path) > 0 else None

            # Fallback: try to find by matching the path value
            for i, path in enumerate(label_paths):
                if isinstance(path, tuple) and len(path) == 1:
                    if path[0] == group_path[0] if len(group_path) > 0 else -1:
                        return path[0]

            # Last resort: assume the path value IS the dimension index
            if len(group_path) == 1:
                return group_path[0]

        else:  # col
            if not (0 <= index < len(self._grid._cols)):
                return None
            col = self._grid._cols[index]
            labels = list(col.get("labels") or [])
            label_paths = list(col.get("label_paths") or [])

            for i, path in enumerate(label_paths):
                if isinstance(path, tuple) and len(path) == 1:
                    if path == group_path:
                        return path[0] if len(path) > 0 else None

            for i, path in enumerate(label_paths):
                if isinstance(path, tuple) and len(path) == 1:
                    if path[0] == group_path[0] if len(group_path) > 0 else -1:
                        return path[0]

            if len(group_path) == 1:
                return group_path[0]

        return None

    def start_pending_header_edit(self) -> None:
        """Start a pending header edit from the pending queue."""
        pending = self._grid._pending_header_edit
        self._grid._pending_header_edit = None
        if pending is None:
            return
        axis, idx, item_id = pending
        if isinstance(item_id, str):
            if axis == "row":
                for r, row in enumerate(self._grid._rows):
                    if row.get("is_leaf", False) and row.get("item_id") == item_id:
                        idx = r
                        break
            else:
                for c, col in enumerate(self._grid._cols):
                    if col.get("item_id") == item_id:
                        idx = c
                        break
        print(
            f"DEBUG edit_mode_switch: label_pending axis={axis} target_index={idx} "
            f"target_item_id={item_id}"
        )
        if not self._grid._start_header_leaf_edit(axis, idx):
            if self._grid.isVisible():
                self._grid.setFocus()

    def ensure_label_editor_visible_from_ctx(self, tag: str) -> bool:
        """Ensure label editor is visible from context."""
        ctx = self._grid._header_edit_ctx
        if ctx is None:
            return False
        axis = ctx.get("axis")
        index = ctx.get("index")
        if not isinstance(axis, str) or not isinstance(index, int):
            return False
        rect = self._grid._row_leaf_header_rect(index) if axis == "row" else self._grid._col_leaf_header_rect(index)
        self._grid._editor.setGeometry(rect.adjusted(1, 0, -1, 2))
        if not self._grid._editor.isVisible():
            print(f"DEBUG edit_mode_switch: label_editor_recover_show tag={tag} axis={axis} index={index}")
        self._grid._editor.show()
        self._grid._editor.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        return self._grid._editor.isVisible()

    def ensure_header_edit_ctx_from_editor(self) -> bool:
        """Ensure header edit context from editor position."""
        if self._grid._header_edit_ctx is not None or not self._grid._editor.isVisible():
            return self._grid._header_edit_ctx is not None
        center = self._grid._editor.geometry().center()
        hit = self.header_hit(center)
        if hit is None:
            return False
        kind, payload = hit
        if kind == "row_leaf":
            row_idx = payload[1] if isinstance(payload, tuple) and len(payload) > 1 and isinstance(payload[1], int) else None
            if row_idx is not None:
                self._grid._header_edit_ctx = {
                    "axis": "row",
                    "index": int(row_idx),
                    "saved_sel_mode": self._grid._sel_mode,
                    "saved_sel_row": self._grid._sel_row,
                    "saved_sel_col": self._grid._sel_col,
                    "saved_sel_indices": set(self._grid._sel_indices),
                }
                print(f"DEBUG edit_mode_switch: label_ctx_recovered axis=row row_idx={row_idx}")
                return True
        elif kind == "col_leaf":
            col_idx = payload if isinstance(payload, int) else None
            if col_idx is not None:
                self._grid._header_edit_ctx = {
                    "axis": "col",
                    "index": int(col_idx),
                    "saved_sel_mode": self._grid._sel_mode,
                    "saved_sel_row": self._grid._sel_row,
                    "saved_sel_col": self._grid._sel_col,
                    "saved_sel_indices": set(self._grid._sel_indices),
                }
                print(f"DEBUG edit_mode_switch: label_ctx_recovered axis=col col_idx={col_idx}")
                return True
        return False

    def should_navigate_on_arrow(self, key: QtCore.Qt.Key) -> bool:
        """Check if arrow key should trigger header navigation.

        Navigation should only happen when:
        - Cursor is at the left border and Left is pressed
        - Cursor is at the right border and Right is pressed
        - Up or Down is pressed (no text-navigation purpose in a single-line
          QLineEdit, so always commit + navigate)
        - All text is selected
        """
        if self._grid._header_edit_ctx is None:
            return False

        # Up/Down have no text-editing purpose in a single-line QLineEdit,
        # so they should always trigger commit + navigation.
        if key in (QtCore.Qt.Key.Key_Up, QtCore.Qt.Key.Key_Down):
            return True

        text = self._grid._editor.text()
        cursor_pos = self._grid._editor.cursorPosition()
        has_selection = self._grid._editor.hasSelectedText()
        selected_text = self._grid._editor.selectedText()

        # If all text is selected, allow navigation
        if has_selection and selected_text == text:
            return True

        # If there's a partial selection, don't navigate (user is editing)
        if has_selection and selected_text != text:
            return False

        # At left edge + Left arrow -> navigate
        if key == QtCore.Qt.Key.Key_Left and cursor_pos == 0:
            return True

        # At right edge + Right arrow -> navigate
        if key == QtCore.Qt.Key.Key_Right and cursor_pos >= len(text):
            return True

        return False

    @staticmethod
    def sanitize_label_text(text: str) -> str:
        """Sanitize label text for use as a header name."""
        if not text:
            return ""
        allowed_extras = set(" _-.,:/\\'\"()+[]{}<>!?@#$%^&*|`~")
        cleaned_chars: list[str] = []
        for ch in text:
            if ch.isalnum():
                cleaned_chars.append(ch)
            elif ch.isspace():
                cleaned_chars.append(" ")
            elif ch in allowed_extras:
                cleaned_chars.append(ch)
            # drop any other control/unsupported characters
        collapsed = "".join(cleaned_chars)
        collapsed = " ".join(collapsed.split())
        return collapsed.strip()

    def show_duplicate_name_warning(self, axis: str, new_name: str) -> None:
        """Show warning when duplicate name is detected."""
        title = "Duplicate Name"
        message = f"'{new_name}' already exists in this {'row' if axis == 'row' else 'column'} dimension."
        QtWidgets.QMessageBox.warning(self._grid, title, message)

    def _update_group_label_in_outline(self, dim_id: str, item_id: str, new_label: str) -> bool:
        """Update the group label containing a specific item in a dimension's outline.
        
        This finds the parent group node containing the item and updates its label.
        """
        from lib_contracts.types import OutlineNode
        
        dim = self._grid._workspace_read_model.get_dimension(dim_id)
        if not dim:
            return False
            
        outline = list(dim.get("outline", []) or [])
        if not outline:
            return False
        
        # Find the parent group containing this item and update its label
        def find_and_update_parent(nodes: list[OutlineNode], target_item_id: str, new_lbl: str) -> tuple[list[OutlineNode], bool]:
            updated_nodes: list[OutlineNode] = []
            found = False
            
            for node in nodes:
                node_item_id = getattr(node, "item_id", None)
                children = list(getattr(node, "children", []) or [])
                
                if node_item_id is None and children:
                    # This is a group node - check if any child is the target item
                    has_target = any(
                        getattr(child, "item_id", None) == target_item_id 
                        for child in children
                    )
                    
                    if has_target:
                        # Update this group's label
                        updated_nodes.append(OutlineNode(
                            label=new_lbl,
                            item_id=None,
                            children=children,
                            node_id=node.node_id,
                            is_aggregate=node.is_aggregate,
                        ))
                        found = True
                    else:
                        # Recursively check children
                        new_children, child_found = find_and_update_parent(children, target_item_id, new_lbl)
                        if child_found:
                            found = True
                        updated_nodes.append(OutlineNode(
                            label=getattr(node, "label", None),
                            item_id=None,
                            children=new_children,
                            node_id=node.node_id,
                            is_aggregate=node.is_aggregate,
                        ))
                else:
                    # This is a leaf node - keep it as is
                    updated_nodes.append(node)
            
            return updated_nodes, found
        
        new_outline, updated = find_and_update_parent(outline, item_id, new_label)
        
        if updated:
            # Update the dimension's outline
            object.__setattr__(dim, "outline", new_outline)
            return True
        
        return False

    def set_editor_focus_enabled(self, enabled: bool) -> None:
        """Enable or disable editor focus."""
        policy = QtCore.Qt.FocusPolicy.StrongFocus if enabled else QtCore.Qt.FocusPolicy.NoFocus
        if self._grid._editor.focusPolicy() != policy:
            self._grid._editor.setFocusPolicy(policy)
        if not enabled:
            self._grid._editor.clearFocus()

    def start_header_leaf_edit(self, axis: str, index: int) -> bool:
        """Start editing a header leaf item."""
        # Check if we're in stacked dimension mode - if so, don't use inline editor
        view = self._grid._workspace_read_model.get_view(self._grid._view_id)
        dim_ids = list(view.get(f"{axis}_dim_ids", []) or []) if view else []
        if len(dim_ids) > 1:
            print(f"DEBUG start_header_leaf_edit: stacked mode detected, skipping inline editor")
            return False
            
        target = self._grid._geometry.resolve_row_leaf_target(index) if axis == "row" else self._grid._geometry.resolve_col_leaf_target(index)
        if target is None:
            return False
        dim_id, item_id, current_name = target
        rect = self._grid._row_leaf_header_rect(index) if axis == "row" else self._grid._col_leaf_header_rect(index)

        # Preserve original selection state across multiple label edits
        if self._grid._header_edit_ctx is not None:
            saved_mode = self._grid._header_edit_ctx.get("saved_sel_mode", self._grid._sel_mode)
            saved_row = self._grid._header_edit_ctx.get("saved_sel_row", self._grid._sel_row)
            saved_col = self._grid._header_edit_ctx.get("saved_sel_col", self._grid._sel_col)
            saved_indices = self._grid._header_edit_ctx.get("saved_sel_indices", set(self._grid._sel_indices))
        else:
            saved_mode = self._grid._sel_mode
            saved_row = self._grid._sel_row
            saved_col = self._grid._sel_col
            saved_indices = set(self._grid._sel_indices)

        self._grid._header_edit_ctx = {
            "axis": axis,
            "index": int(index),
            "dim_id": dim_id,
            "item_id": item_id,
            "orig_name": current_name,
            "saved_sel_mode": saved_mode,
            "saved_sel_row": saved_row,
            "saved_sel_col": saved_col,
            "saved_sel_indices": saved_indices,
        }
        self._grid._editor.setText(current_name)
        self.set_editor_focus_enabled(True)
        self._grid._editor.setGeometry(rect.adjusted(1, 1, -1, -1))
        self._grid._set_edit_mode("label")
        self._grid.viewport().setFocusProxy(None)
        self._grid._editor.show()
        self._grid._editor.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        self._grid._editor.activateWindow()
        self._grid._editor.raise_()
        QtWidgets.QApplication.processEvents()
        if not self._grid._editor.hasFocus():
            grid = self._grid
            QtCore.QTimer.singleShot(0, lambda: grid._editor.setFocus(QtCore.Qt.FocusReason.OtherFocusReason) if grid.isVisible() else None)
        self._grid._editor.selectAll()
        print(
            f"DEBUG edit_mode_switch: -> label axis={axis} index={index} "
            f"item_id={item_id} name={target[2]!r}"
        )
        return True

    def start_header_leaf_edit_from_hit(self, hit: tuple[str, str | tuple[int, ...] | None]) -> bool:
        """Start editing a header leaf from a hit result."""
        kind, payload = hit
        if kind == "row_leaf":
            row_idx = payload[1] if isinstance(payload, tuple) and len(payload) > 1 and isinstance(payload[1], int) else None
            if row_idx is None and isinstance(payload, tuple) and payload and isinstance(payload[0], str):
                item_id = payload[0]
                for i, row in enumerate(self._grid._rows):
                    if row.get("item_id") == item_id and row.get("is_leaf", False):
                        row_idx = i
                        break
            if row_idx is None:
                return False
            return self.start_header_leaf_edit("row", row_idx)

        if kind == "col_leaf":
            col_idx = payload if isinstance(payload, int) else None
            if col_idx is None and isinstance(payload, str):
                for i, col in enumerate(self._grid._cols):
                    if col.get("item_id") == payload:
                        col_idx = i
                        break
            if col_idx is None:
                return False
            return self.start_header_leaf_edit("col", col_idx)

        return False

    def start_group_header_edit(self, axis: str, group_path: tuple[int, ...]) -> bool:
        """Start editing a group header."""
        # Check if we're in stacked dimension mode - if so, don't use inline editor
        view = self._grid._workspace_read_model.get_view(self._grid._view_id)
        dim_ids = list(view.get(f"{axis}_dim_ids", []) or []) if view else []
        if len(dim_ids) > 1:
            print(f"DEBUG start_group_header_edit: stacked mode detected, using popup dialog instead of inline editor")
            # Find the band to get the correct range
            bands = self._grid._row_bands if axis == "row" else self._grid._col_bands
            r0, r1, c0, c1 = 0, 0, 0, 0
            clicked_r, clicked_c = 0, 0
            for band in bands:
                if band.get("path") == group_path:
                    if axis == "row":
                        r0 = band.get("r0", 0)
                        r1 = band.get("r1", r0)
                        clicked_r = r0
                    else:
                        c0 = band.get("c0", 0)
                        c1 = band.get("c1", c0)
                        clicked_c = c0
                    break
            # Trigger the popup dialog rename with correct range
            hit = (f"{axis}_group", (group_path, r0 if axis == "row" else c0, r1 if axis == "row" else c1, clicked_r if axis == "row" else clicked_c))
            return self.rename_header_hit(hit)
        
        root = self._grid._outline_root(axis)
        node = self._grid._get_node_at_path(root, group_path) if root else None
        
        # Get the group label from outline node or from bands (for stacked mode)
        current_name = ""
        if node is not None and node.item_id is None:
            # It's an outline-based group
            current_name = str(node.label or "")
        else:
            # Try to get label from bands (for stacked mode groups)
            bands = self._grid._row_bands if axis == "row" else self._grid._col_bands
            print(f"DEBUG group_edit: looking for path={group_path} in {len(bands)} bands")
            for band in bands:
                if band.get("path") == group_path:
                    current_name = str(band.get("label", ""))
                    print(f"DEBUG group_edit: found band level={band.get('level')} label={current_name!r} path={band.get('path')}")
                    break
            
            # If still no label, check if this is a valid band path
            if not current_name:
                # Check if any band has this path - if so, it's a valid group
                has_band = any(band.get("path") == group_path for band in bands)
                if not has_band:
                    print(f"DEBUG group_edit: no band found for path={group_path}")
                    return False
                # Use a default label if none found
                current_name = ""
                print(f"DEBUG group_edit: band found but no label for path={group_path}")

        # Find the band for this group to get its geometry
        rect = None
        if axis == "row":
            for band in self._grid._row_bands:
                if band.get("path") == group_path:
                    level = int(band.get("level", -1))
                    r0 = int(band.get("r0", -1))
                    r1 = int(band.get("r1", -2))
                    if level < 0 or r0 < 0:
                        continue
                    # Calculate geometry
                    header_h = self._grid._m.col_header_h * max(1, self._grid._col_header_levels)
                    cumulative = 0
                    for lvl in range(level):
                        cumulative += self._grid._geometry.row_header_level_width(lvl)
                    x = cumulative
                    w = self._grid._geometry.row_header_level_width(level)
                    y = header_h + r0 * self._grid._m.row_h - self._grid._geometry.scroll_offset().y()
                    h = (r1 - r0 + 1) * self._grid._m.row_h
                    rect = QtCore.QRect(int(x), int(y), int(w), int(h))
                    break
        else:
            for band in self._grid._col_bands:
                if band.get("path") == group_path:
                    level = int(band.get("level", -1))
                    c0 = int(band.get("c0", -1))
                    c1 = int(band.get("c1", -2))
                    if level < 0 or c0 < 0:
                        continue
                    off = self._grid._geometry.scroll_offset()
                    row_header_w = self._grid._geometry.row_header_width()
                    x = row_header_w
                    for i in range(c0):
                        x += self._grid._geometry.col_width(i)
                    x -= off.x()
                    w = sum(self._grid._geometry.col_width(i) for i in range(c0, c1 + 1))
                    y = level * self._grid._m.col_header_h
                    h = self._grid._m.col_header_h
                    rect = QtCore.QRect(int(x), int(y), int(w), int(h))
                    break

        if rect is None:
            return False

        # Preserve selection state
        if self._grid._header_edit_ctx is not None:
            saved_mode = self._grid._header_edit_ctx.get("saved_sel_mode", self._grid._sel_mode)
            saved_row = self._grid._header_edit_ctx.get("saved_sel_row", self._grid._sel_row)
            saved_col = self._grid._header_edit_ctx.get("saved_sel_col", self._grid._sel_col)
            saved_indices = self._grid._header_edit_ctx.get("saved_sel_indices", set(self._grid._sel_indices))
        else:
            saved_mode = self._grid._sel_mode
            saved_row = self._grid._sel_row
            saved_col = self._grid._sel_col
            saved_indices = set(self._grid._sel_indices)

        self._grid._header_edit_ctx = {
            "type": "group",
            "axis": axis,
            "group_path": group_path,
            "orig_name": current_name,
            "saved_sel_mode": saved_mode,
            "saved_sel_row": saved_row,
            "saved_sel_col": saved_col,
            "saved_sel_indices": saved_indices,
        }

        self._grid._editor.setText(current_name)
        self.set_editor_focus_enabled(True)
        self._grid._editor.setGeometry(rect.adjusted(1, 0, -1, 2))
        self._grid._set_edit_mode("label")
        self._grid.viewport().setFocusProxy(None)
        self._grid._editor.show()
        self._grid._editor.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        self._grid._editor.activateWindow()
        self._grid._editor.raise_()
        QtWidgets.QApplication.processEvents()
        if not self._grid._editor.hasFocus():
            grid = self._grid
            QtCore.QTimer.singleShot(0, lambda: grid._editor.setFocus(QtCore.Qt.FocusReason.OtherFocusReason) if grid.isVisible() else None)
        self._grid._editor.selectAll()
        print(
            f"DEBUG edit_mode_switch: -> label group axis={axis} path={group_path} "
            f"name={current_name!r}"
        )
        return True

    def get_parent_header(self, axis: str) -> tuple[str, tuple[int, ...] | int] | None:
        """Get parent group from current editing context."""
        if self._grid._header_edit_ctx is None:
            return None

        ctx_type = self._grid._header_edit_ctx.get("type", "leaf")

        if ctx_type == "leaf":
            index = self._grid._header_edit_ctx.get("index")
            if axis == "row" and 0 <= index < len(self._grid._rows):
                path = self._grid._rows[index].get("path")
                if isinstance(path, tuple) and len(path) > 1:
                    return ("group", path[:-1])
            elif axis == "col" and 0 <= index < len(self._grid._cols):
                path = self._grid._cols[index].get("path")
                if isinstance(path, tuple) and len(path) > 1:
                    return ("group", path[:-1])
        elif ctx_type == "group":
            group_path = self._grid._header_edit_ctx.get("group_path")
            if isinstance(group_path, tuple) and len(group_path) > 1:
                return ("group", group_path[:-1])

        return None

    def get_first_child_header(self, axis: str) -> tuple[str, tuple[int, ...] | int] | None:
        """Get first child from current group context."""
        if self._grid._header_edit_ctx is None:
            return None

        ctx_type = self._grid._header_edit_ctx.get("type", "leaf")

        if ctx_type == "group":
            group_path = self._grid._header_edit_ctx.get("group_path")
            if not isinstance(group_path, tuple):
                return None

            root = self._grid._outline_root(axis)
            node = self._grid._get_node_at_path(root, group_path)
            if node and node.children:
                first_child = node.children[0]
                child_path = group_path + (0,)
                if first_child.item_id is None:
                    return ("group", child_path)
                else:
                    if axis == "row":
                        for i, row in enumerate(self._grid._rows):
                            if row.get("path") == child_path:
                                return ("leaf", i)
                    else:
                        for i, col in enumerate(self._grid._cols):
                            if col.get("path") == child_path:
                                return ("leaf", i)

        return None

    def debug_edit_state(self, tag: str) -> None:
        """Print debug information about edit state."""
        ctx = self._grid._header_edit_ctx
        ctx_axis = None if ctx is None else ctx.get("axis")
        ctx_index = None if ctx is None else ctx.get("index")
        print(
            "DEBUG edit_trace: "
            f"{tag} visible={self._grid._editor.isVisible()} focus={self._grid._editor.hasFocus()} "
            f"ctx_axis={ctx_axis} ctx_index={ctx_index} "
            f"text={self._grid._editor.text()!r}"
        )

    def commit_and_navigate_to(self, target: tuple[str, tuple[int, ...] | int]) -> None:
        """Navigate to target header after commit. Caller already committed changes."""
        if not self._grid._editor.isVisible() or self._grid._header_edit_ctx is None:
            return

        axis = str(self._grid._header_edit_ctx.get("axis") or "")

        self._grid._ignore_next_grid_enter = True
        self._grid._hide_editor(restore_grid_focus=False)
        self._grid.viewport().update()

        def _start_target_edit() -> None:
            target_type, target_ref = target
            started = False
            if target_type == "group":
                started = self._grid._start_group_header_edit(axis, target_ref)
            else:
                started = self._grid._start_header_leaf_edit(axis, target_ref)
            if not started:
                if self._grid.isVisible():
                    self._grid.setFocus()

        QtCore.QTimer.singleShot(0, _start_target_edit)

    def commit_header_editor(self, *, move_next: bool = False, move_prev: bool = False) -> None:
        """Commit header editor changes with optional navigation."""
        if not self._grid._editor.isVisible() or self._grid._header_edit_ctx is None:
            return
        self._grid._debug_edit_state(f"commit_header_start move_next={move_next} move_prev={move_prev}")
        ctx = dict(self._grid._header_edit_ctx)
        ctx_type = ctx.get("type", "leaf")
        axis = str(ctx.get("axis") or "")
        old_name = str(ctx.get("orig_name") or "")
        new_name = self._grid._sanitize_label_text(self._grid._editor.text())

        if not new_name:
            self._grid._hide_editor()
            self._grid.viewport().update()
            if self._grid.isVisible():
                self._grid.setFocus()
            return

        renamed = new_name != old_name

        # Commit changes based on context type
        if ctx_type == "group":
            group_path = ctx.get("group_path")
            if renamed:
                dim_id = self._grid._axis_dim_id(axis)
                if not isinstance(dim_id, str):
                    self._grid._hide_editor()
                    self._grid.viewport().update()
                    if self._grid.isVisible():
                        self._grid.setFocus()
                    return
                # Phase 8: command dispatcher handles mutation
                root = self._grid._outline_root(axis)
                node = self._grid._get_node_at_path(root, group_path)
                if node and getattr(node, 'node_id', None):
                    result = self._grid.execute_command(
                        "rename_group_node",
                        dim_id=dim_id,
                        node_id=node.node_id,
                        new_label=new_name,
                    )
                    if not result.success:
                        QtWidgets.QMessageBox.warning(
                            self._grid,
                            "Duplicate Group Label",
                            result.error or "Rename failed",
                            QtWidgets.QMessageBox.StandardButton.Ok,
                        )
                        self._grid._editor.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
                        self._grid._editor.selectAll()
                        return
                else:
                    updated_root = self._grid._set_node_label_at_path(root, group_path, new_name)
                    self._grid._set_outline_root(axis, updated_root)
                # Save scroll position before reload (to prevent jump to origin)
                saved_h_scroll = self._grid.horizontalScrollBar().value()
                saved_v_scroll = self._grid.verticalScrollBar().value()
                self._grid._preserve_scroll = True
                self._grid.reload()
                # Restore scroll position after reload
                def _restore_scroll():
                    self._grid.horizontalScrollBar().setValue(saved_h_scroll)
                    self._grid.verticalScrollBar().setValue(saved_v_scroll)
                    self._grid._preserve_scroll = False
                    DEBUG_GUI and print(f"DEBUG SCROLL: commit_header_editor (group) restored scroll to h={saved_h_scroll}, v={saved_v_scroll}")
                QtCore.QTimer.singleShot(0, _restore_scroll)
                # NOTE: do NOT emit outline_changed here - it triggers rebuild_tabs() which
                # destroys and recreates the entire ViewTab/MatrixGrid, wiping selection state.
                # The signal will be emitted after navigation completes or deferred.
        else:  # leaf
            index = int(ctx.get("index", -1))
            dim_id = str(ctx.get("dim_id") or "")
            item_id = str(ctx.get("item_id") or "")
            if renamed:
                result = self._grid.execute_command(
                    "rename_dimension_item",
                    dim_id=dim_id,
                    item_id=item_id,
                    new_name=new_name,
                )
                if not result.success:
                    self._grid._show_duplicate_name_warning(axis, new_name)
                    self._grid._editor.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
                    self._grid._editor.selectAll()
                    return

        # Find next/prev sibling for navigation
        next_idx = None
        next_item_id = None
        next_target = None

        if ctx_type == "leaf":
            index = int(ctx.get("index", -1))
            if move_next:
                next_idx = self._grid._next_header_leaf_index(axis, index)
                if next_idx is not None:
                    next_item_id = self._grid._header_leaf_item_id(axis, next_idx)
            elif move_prev:
                next_idx = self._grid._prev_header_leaf_index(axis, index)
                if next_idx is not None:
                    next_item_id = self._grid._header_leaf_item_id(axis, next_idx)
        elif ctx_type == "group":
            # For groups, prefer navigating to other groups at the same level before leaves
            group_path = ctx.get("group_path")
            print(f"DEBUG group_nav: group_path={group_path}, move_next={move_next}, move_prev={move_prev}")
            if isinstance(group_path, tuple) and len(group_path) > 0:
                parent_path = group_path[:-1]
                current_child_idx = group_path[-1]

                # Get parent node to find siblings
                root = self._grid._outline_root(axis)
                if parent_path:
                    parent_node = self._grid._get_node_at_path(root, parent_path)
                else:
                    parent_node = type('Node', (), {'children': root})()

                if parent_node and hasattr(parent_node, 'children'):
                    siblings = parent_node.children
                    print(f"DEBUG group_nav: parent_path={parent_path}, current_child_idx={current_child_idx}, siblings={len(siblings)}")

                    # First, look for another group at the same level
                    if move_next:
                        # Scan forward for next group sibling
                        for idx in range(current_child_idx + 1, len(siblings)):
                            sibling = siblings[idx]
                            sibling_path = parent_path + (idx,)
                            print(f"DEBUG group_nav: checking sibling {idx}, label={sibling.label}, is_group={sibling.item_id is None}")
                            if sibling.item_id is None:  # It's a group
                                next_target = ("group", sibling_path)
                                print(f"DEBUG group_nav: found next group at {sibling_path}")
                                break
                        else:
                            # No more groups at this level, fall back to first leaf sibling
                            for idx in range(current_child_idx + 1, len(siblings)):
                                sibling = siblings[idx]
                                sibling_path = parent_path + (idx,)
                                if sibling.item_id is not None:  # It's a leaf
                                    items = self._grid._rows if axis == "row" else self._grid._cols
                                    for i, item in enumerate(items):
                                        if item.get("path") == sibling_path:
                                            next_target = ("leaf", i)
                                            print(f"DEBUG group_nav: found leaf at index {i}")
                                            break
                                    break
                    elif move_prev:
                        # Scan backward for prev group sibling
                        for idx in range(current_child_idx - 1, -1, -1):
                            sibling = siblings[idx]
                            sibling_path = parent_path + (idx,)
                            print(f"DEBUG group_nav: checking sibling {idx}, label={sibling.label}, is_group={sibling.item_id is None}")
                            if sibling.item_id is None:  # It's a group
                                next_target = ("group", sibling_path)
                                print(f"DEBUG group_nav: found prev group at {sibling_path}")
                                break
                        else:
                            # No more groups at this level, fall back to first leaf sibling backward
                            for idx in range(current_child_idx - 1, -1, -1):
                                sibling = siblings[idx]
                                sibling_path = parent_path + (idx,)
                                if sibling.item_id is not None:  # It's a leaf
                                    items = self._grid._rows if axis == "row" else self._grid._cols
                                    for i, item in enumerate(items):
                                        if item.get("path") == sibling_path:
                                            next_target = ("leaf", i)
                                            print(f"DEBUG group_nav: found leaf at index {i}")
                                            break
                                    break

        if next_idx is None and next_target is None:
            self._grid._ignore_next_grid_enter = True
            saved_mode = ctx.get("saved_sel_mode", "cell")
            saved_row = ctx.get("saved_sel_row", self._grid._sel_row)
            saved_col = ctx.get("saved_sel_col", self._grid._sel_col)
            saved_indices = ctx.get("saved_sel_indices", set())
            DEBUG_GUI and print(f"DEBUG restore_selection: saved mode={saved_mode} row={saved_row} col={saved_col} indices={saved_indices}")
            self._grid._hide_editor()
            # Always restore original selection state when exiting label edit mode
            if renamed:
                # Save scroll position before reload (to prevent jump to origin)
                saved_h_scroll = self._grid.horizontalScrollBar().value()
                saved_v_scroll = self._grid.verticalScrollBar().value()
                self._grid._preserve_scroll = True
                self._grid.reload()
                # Restore scroll position after reload
                def _restore_scroll():
                    self._grid.horizontalScrollBar().setValue(saved_h_scroll)
                    self._grid.verticalScrollBar().setValue(saved_v_scroll)
                    self._grid._preserve_scroll = False
                    DEBUG_GUI and print(f"DEBUG SCROLL: commit_header_editor (leaf) restored scroll to h={saved_h_scroll}, v={saved_v_scroll}")
                QtCore.QTimer.singleShot(0, _restore_scroll)
                # NOTE: do NOT emit outline_changed here - it triggers rebuild_tabs() which
                # destroys and recreates the entire ViewTab/MatrixGrid, wiping selection state.
                # reload() is sufficient to update the grid display for label renames.
            # Restore selection state after reload (which may have reset coordinates)
            self._grid._sel_mode = saved_mode
            self._grid._sel_row = saved_row
            self._grid._sel_col = saved_col
            self._grid._sel_indices = set(saved_indices)
            DEBUG_GUI and print(f"DEBUG restore_selection: restored mode={self._grid._sel_mode} row={self._grid._sel_row} col={self._grid._sel_col} indices={self._grid._sel_indices}")
            self._grid.selection_changed.emit()
            self._grid.viewport().update()
            if self._grid.isVisible():
                self._grid.setFocus()
            self._grid._request_repaint("commit_header_no_next_restored")
            # Notify other windows to sync after rename completes (deferred to avoid wiping selection)
            if renamed and ctx_type == "leaf":
                QtCore.QTimer.singleShot(0, self._grid.outline_changed.emit)
            # Also defer outline_changed for group edits to avoid rebuild_tabs()
            if renamed and ctx_type == "group":
                QtCore.QTimer.singleShot(0, self._grid.outline_changed.emit)
            self._grid._debug_edit_state("commit_header_end route=no_next_restore_nav")
            return

        if renamed and ctx_type == "leaf":
            index = int(ctx.get("index", -1))
            self._grid._update_visible_leaf_label(axis, index, new_name)
        self._grid.viewport().update()

        if move_next or move_prev:
            # Handle navigation to next/prev sibling
            if next_target is not None:
                # Navigate to group or leaf target
                self._grid._commit_and_navigate_to(next_target)
                # Notify other windows to sync after rename completes (deferred)
                if renamed and ctx_type == "leaf":
                    QtCore.QTimer.singleShot(0, self._grid.outline_changed.emit)
                # Also defer for group edits to avoid rebuild_tabs
                if renamed and ctx_type == "group":
                    QtCore.QTimer.singleShot(0, self._grid.outline_changed.emit)
                return
            elif next_idx is not None:
                self._grid._ignore_next_grid_enter = True
                direction = "next" if move_next else "prev"
                print(
                    f"DEBUG edit_mode_switch: label -> label trigger=enter_{direction}_label "
                    f"axis={axis} current_index={index} next_index={next_idx} "
                    f"next_item_id={next_item_id}"
                )
                self._grid._pending_header_edit = (axis, next_idx, next_item_id)
                self._grid._start_pending_header_edit()
                # Notify other windows to sync after rename completes (deferred)
                if renamed and ctx_type == "leaf":
                    QtCore.QTimer.singleShot(0, self._grid.outline_changed.emit)
                if not self._grid._editor.isVisible() or self._grid._header_edit_ctx is None:
                    print(
                        f"DEBUG edit_mode_switch: label_{direction}_retry_scheduled "
                        f"axis={axis} next_index={next_idx} next_item_id={next_item_id}"
                    )
                    self._grid._pending_header_edit = (axis, next_idx, next_item_id)
                    QtCore.QTimer.singleShot(0, self._grid._start_pending_header_edit)
                self._grid._debug_edit_state(f"commit_header_end route={direction}_label")
                return
            # Only enter cell edit mode if moving forward (Enter key)
            if move_next:
                self._grid._ignore_next_grid_enter = True
                self._grid._enter_cell_edit_mode_from_header(axis, index)
                # Signal will be emitted by the focus-out handler (move_next=False path) after rename completes
                self._grid._debug_edit_state("commit_header_end route=cell_mode")
                return

        self._grid._ignore_next_grid_enter = True
        if self._grid.isVisible():
            self._grid.setFocus()
        # Notify other windows to sync outline changes after leaf rename (only when not navigating)
        if renamed and ctx_type == "leaf":
            self._grid.outline_changed.emit()
        self._grid._debug_edit_state("commit_header_end route=focus_grid")

    def enter_cell_edit_mode_from_header(self, axis: str, index: int) -> None:
        """Enter cell edit mode from header editing context."""
        if axis == "row":
            self._grid._sel_row = index
        else:
            self._grid._sel_col = index

        # DEBUG_GUI = self.DEBUG_GUI
        DEBUG_GUI and print(f"DEBUG enter_cell_from_header: before_clamp row={self._grid._sel_row} col={self._grid._sel_col}")
        self._grid._clamp_selection_to_leaf()
        DEBUG_GUI and print(f"DEBUG enter_cell_from_header: after_clamp row={self._grid._sel_row} col={self._grid._sel_col}")
        DEBUG_GUI and print(f"DEBUG SET CELL: line {__import__('inspect').currentframe().f_lineno} prev={self._grid._sel_mode}"); self._grid._sel_mode = "cell"
        self._grid._sel_indices.clear()
        self._grid._anchor_row, self._grid._anchor_col = self._grid._sel_row, self._grid._sel_col
        self._grid._hide_editor()
        self._grid.selection_changed.emit()
        self._grid.viewport().update()
        self._grid._request_repaint("enter_cell_from_header")
        print(
            f"DEBUG edit_mode_switch: cell_focus_restored row={self._grid._sel_row} col={self._grid._sel_col}"
        )
