"""Workspace read model — read-only query facade for workspace metadata.

This is NOT a cache. This is NOT a synchronized projection.
It delegates to query handlers which read engine state.

Usage:
    ws_read_model = WorkspaceReadModel(session)
    view = ws_read_model.get_view(view_id)
    cube = ws_read_model.get_cube(cube_id)
    dim = ws_read_model.get_dimension(dim_id)
    views = ws_read_model.list_views()
    cubes = ws_read_model.list_cubes()
    dims = ws_read_model.list_dimensions()
    saved_default_view_id = ws_read_model.saved_default_view_id()

Boundary:
    Only plain dicts/lists and primitive values cross this boundary.
    Never engine domain objects.
"""

from __future__ import annotations

from lib_utils.config import gui as gui_config


def _is_system_element(name: str) -> bool:
    """Return True if name identifies a system element (prefix '%')."""
    return isinstance(name, str) and name.startswith("%")


class WorkspaceReadModel:
    """Read-only query facade for workspace metadata.

    This is NOT a cache. This is NOT a synchronized projection.
    It delegates to query handlers which read engine state.

    When a gui_view_model is provided, get_view/get_cube/get_dimension
    check the GUIViewModel's cached snapshots first and fall back to a
    bus query on cache miss. This avoids redundant synchronous queries
    during grid reloads.
    """

    def __init__(self, session, gui_view_model=None) -> None:
        self.session = session
        self._gui_view_model = gui_view_model

    # -----------------------------------------------------------------
    # Single entity lookups
    # -----------------------------------------------------------------

    def get_view(self, view_id: str) -> dict | None:
        """Return view snapshot dict or None."""
        if self._gui_view_model is not None:
            snap = self._gui_view_model.get_view_snapshot(view_id)
            if snap:
                return snap
        data = self.session.query("view_detail", view_id=view_id)
        return data

    def get_cube(self, cube_id: str) -> dict | None:
        """Return cube snapshot dict or None."""
        if self._gui_view_model is not None:
            snap = self._gui_view_model.get_cube_snapshot(cube_id)
            if snap:
                return snap
        data = self.session.query("cube_detail", cube_id=cube_id)
        return data

    def get_dimension(self, dim_id: str) -> dict | None:
        """Return dimension snapshot dict or None."""
        if self._gui_view_model is not None:
            snap = self._gui_view_model.get_dimension_snapshot(dim_id)
            if snap and snap.get("items"):
                return snap
        data = self.session.query("dimension_detail", dim_id=dim_id)
        return data

    def dimension_items(self, dim_id: str) -> list[dict]:
        """Return list of dimension item dicts: [{"id": str, "name": str}, ...].

        This returns the canonical flat item list (dim.items).
        For display order that respects graph structure, use effective_order().
        """
        if self._gui_view_model is not None:
            snap = self._gui_view_model.get_dimension_snapshot(dim_id)
            if snap and snap.get("items"):
                return snap["items"]
        data = self.session.query("dimension_detail", dim_id=dim_id)
        if data:
            return data.get("items", [])
        return []

    def effective_order(self, dim_id: str) -> list[str]:
        """Return item IDs in effective display order.

        Merges graph-backed order with unmaterialized flat items.
        """
        data = self.session.query("dimension_effective_order", dim_id=dim_id)
        if data:
            return data.get("item_ids", [])
        return []

    # -----------------------------------------------------------------
    # Lists
    # -----------------------------------------------------------------

    def _show_system_elements(self, include_system: bool | None) -> bool:
        """Resolve the effective include_system flag.

        ``include_system=None`` reads the ``[gui] show_system_elements``
        setting (default ``False``).  System elements are identified by a
        leading ``%`` in their name.
        """
        if include_system is not None:
            return include_system
        return gui_config("gui", "show_system_elements", False)

    def _filter_by_visibility(self, items: list[dict], include_system: bool | None) -> list[dict]:
        """Filter a list of element dicts by system-element visibility."""
        if self._show_system_elements(include_system):
            return items
        return [item for item in items if not _is_system_element(item.get("name", ""))]

    def list_views(self, include_system: bool | None = None) -> list[dict]:
        """Return list of view summary dicts: [{"id": str, "name": str}, ...]."""
        if self._gui_view_model is not None:
            cached = self._gui_view_model.get_all_view_snapshots()
            if cached:
                items = [
                    {"id": vid, "name": v.get("name", vid)}
                    for vid, v in cached.items()
                ]
                return self._filter_by_visibility(items, include_system)
        data = self.session.query("view_list")
        items = data.get("views", []) if data else []
        return self._filter_by_visibility(items, include_system)

    def list_cubes(self, include_system: bool | None = None) -> list[dict]:
        """Return list of cube summary dicts: [{"id": str, "name": str, "dimensions": int}, ...]."""
        if self._gui_view_model is not None:
            cached = self._gui_view_model.get_all_cube_snapshots()
            if cached:
                items = [
                    {
                        "id": cid,
                        "name": c.get("name", cid),
                        "dimensions": len(c.get("dimension_ids", [])),
                    }
                    for cid, c in cached.items()
                ]
                return self._filter_by_visibility(items, include_system)
        data = self.session.query("cube_list")
        items = data.get("cubes", []) if data else []
        return self._filter_by_visibility(items, include_system)

    def list_dimensions(self, include_system: bool | None = None) -> list[dict]:
        """Return list of dimension summary dicts: [{"id": str, "name": str, "items": int}, ...]."""
        if self._gui_view_model is not None:
            cached = self._gui_view_model.get_all_dimension_snapshots()
            if cached:
                items = [
                    {
                        "id": did,
                        "name": d.get("name", did),
                        "items": len(d.get("items", [])),
                        "item_list": [
                            {"id": it.get("id", ""), "name": it.get("name", "")}
                            for it in d.get("items", [])
                        ],
                    }
                    for did, d in cached.items()
                ]
                return self._filter_by_visibility(items, include_system)
        data = self.session.query("dimension_list")
        items = data.get("dimensions", []) if data else []
        return self._filter_by_visibility(items, include_system)

    # -----------------------------------------------------------------
    # Workspace-level accessors
    # -----------------------------------------------------------------

    def saved_default_view_id(self) -> str | None:
        """Return the workspace's file-level saved default view ID."""
        data = self.session.query("workspace_summary")
        if data:
            return data.get("saved_default_view_id")
        return None

    def workspace_summary(self) -> dict | None:
        """Return lightweight workspace summary dict."""
        return self.session.query("workspace_summary")

    # -----------------------------------------------------------------
    # Workspace snapshot
    # -----------------------------------------------------------------

    def workspace_snapshot(self) -> dict | None:
        """Return full workspace snapshot dict with view/cube/dimension DTOs."""
        return self.session.query("workspace_snapshot")

    # -----------------------------------------------------------------
    # DTO list methods — full snapshots, not lightweight summaries
    # -----------------------------------------------------------------

    def list_view_dtos(self, include_system: bool | None = None) -> list[dict]:
        """Return list of full view snapshot dicts from workspace snapshot."""
        if self._gui_view_model is not None:
            items = list(self._gui_view_model.get_all_view_snapshots().values())
            if items:
                return self._filter_by_visibility(items, include_system)
        data = self.workspace_snapshot()
        items = list(data.get("view_snapshots", {}).values()) if data else []
        return self._filter_by_visibility(items, include_system)

    def list_cube_dtos(self, include_system: bool | None = None) -> list[dict]:
        """Return list of full cube snapshot dicts from workspace snapshot."""
        if self._gui_view_model is not None:
            items = list(self._gui_view_model.get_all_cube_snapshots().values())
            if items:
                return self._filter_by_visibility(items, include_system)
        data = self.workspace_snapshot()
        items = list(data.get("cube_snapshots", {}).values()) if data else []
        return self._filter_by_visibility(items, include_system)

    def list_dimension_dtos(self, include_system: bool | None = None) -> list[dict]:
        """Return list of full dimension snapshot dicts from workspace snapshot."""
        if self._gui_view_model is not None:
            items = list(self._gui_view_model.get_all_dimension_snapshots().values())
            if items:
                return self._filter_by_visibility(items, include_system)
        data = self.workspace_snapshot()
        items = list(data.get("dimension_snapshots", {}).values()) if data else []
        return self._filter_by_visibility(items, include_system)

    def has_system_graph_cubes(self) -> bool:
        """Check whether the system graph cubes exist in the workspace.

        Returns True if both %RECNODADR dimension and %RECNOD cube are present.
        """
        dims = self.list_dimensions(include_system=True)
        cubes = self.list_cubes(include_system=True)
        has_adr_dim = any(d.get("name") == "%RECNODADR" for d in dims)
        has_recnod_cube = any(c.get("name") == "%RECNOD" for c in cubes)
        return has_adr_dim and has_recnod_cube

    def get_view_state(self, view_id: str) -> dict | None:
        """Return view-level presentation state (selection, scroll, active cell).

        Returns None if session is unavailable or query fails.
        """
        if self.session is None:
            return None
        return self.session.query("view_state", view_id=view_id)

    def page_selection(self, view_id: str, dim_id: str) -> str | None:
        """Return the selected page item ID for a dimension in a view.

        Returns None if session is unavailable or query fails.
        """
        if self.session is None:
            return None
        data = self.session.query("page_selection", view_id=view_id, dim_id=dim_id)
        if data:
            return data.get("item_id")
        return None
