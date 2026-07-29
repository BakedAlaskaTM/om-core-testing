"""OutlineReadModel — GUI-facing read-model wrapper for outline queries.

Returns plain DTO/snapshot data for view axis outlines and dimension outlines.
"""

from __future__ import annotations

from typing import Any


class OutlineReadModel:
    """Read-only facade for querying outline trees via session query boundary.

    When a gui_view_model is provided, dimension_detail and dimension_outline
    check the cached snapshots first, avoiding redundant RPC calls.
    """

    def __init__(self, session, gui_view_model=None) -> None:
        self.session = session
        self._gui_view_model = gui_view_model

    def outline_tree(self, view_id: str, axis: str) -> dict[str, Any] | None:
        """Return outline tree for a view axis (row or col).

        Returns a dict with keys: type, axis, nodes.
        Returns None if session is unavailable or query fails.
        """
        if self.session is None:
            return None
        result = self.session.query("outline_tree", view_id=view_id, axis=axis)
        if result is None:
            return None
        return result

    def row_outline_tree(self, view_id: str) -> dict[str, Any] | None:
        """Convenience: row axis outline tree."""
        return self.outline_tree(view_id, "row")

    def col_outline_tree(self, view_id: str) -> dict[str, Any] | None:
        """Convenience: column axis outline tree."""
        return self.outline_tree(view_id, "col")

    def dimension_outline(self, dim_id: str) -> list[dict] | None:
        """Return dimension outline nodes from dimension_detail query.

        Returns None if session is unavailable or query fails.
        """
        if self._gui_view_model is not None:
            snap = self._gui_view_model.get_dimension_snapshot(dim_id)
            if snap:
                return snap.get("outline")
        if self.session is None:
            return None
        result = self.session.query("dimension_detail", dim_id=dim_id)
        if result is None:
            return None
        return result.get("outline")

    def dimension_detail(self, dim_id: str) -> dict[str, Any] | None:
        """Return full dimension detail DTO.

        Returns None if session is unavailable or query fails.
        """
        if self._gui_view_model is not None:
            snap = self._gui_view_model.get_dimension_snapshot(dim_id)
            if snap:
                return snap
        if self.session is None:
            return None
        return self.session.query("dimension_detail", dim_id=dim_id)
