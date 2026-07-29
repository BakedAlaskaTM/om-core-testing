"""Grid read model — read-only query facade for grid/view shape data.

This is NOT a cache. This is NOT a synchronized projection.
It delegates to query handlers which read engine state.

Usage:
    grid_read_model = GridReadModel(session)
    row_keys = grid_read_model.row_keys(view_id)
    col_keys = grid_read_model.col_keys(view_id)
    header = grid_read_model.row_header(view_id, section)
    header = grid_read_model.col_header(view_id, section)

Boundary:
    Only plain lists and strings cross this boundary — never engine objects.
"""

from __future__ import annotations


class GridReadModel:
    """Read-only query facade for grid/view shape data.

    This is NOT a cache. This is NOT a synchronized projection.
    It delegates to query handlers which read engine state.

    Usage:
        - Table row/column counts -> row_keys() / col_keys()
        - Header labels -> row_header() / col_header()

    A lightweight per-view key cache avoids redundant bus queries
    within a single reload cycle. Call invalidate_cache() when the
    view shape changes.
    """

    def __init__(self, session) -> None:
        self.session = session
        self._row_key_cache: dict[str, list[tuple[str, ...]]] = {}
        self._col_key_cache: dict[str, list[tuple[str, ...]]] = {}

    def invalidate_cache(self, view_id: str | None = None) -> None:
        if view_id is None:
            self._row_key_cache.clear()
            self._col_key_cache.clear()
        else:
            self._row_key_cache.pop(view_id, None)
            self._col_key_cache.pop(view_id, None)

    def row_keys(
        self,
        view_id: str,
    ) -> list[tuple[str, ...]]:
        """Get row keys for a view."""
        cached = self._row_key_cache.get(view_id)
        if cached is not None:
            return cached
        data = self.session.query("view_row_keys", view_id=view_id)
        keys = data.get("keys", []) if data else []
        self._row_key_cache[view_id] = keys
        return keys

    def col_keys(
        self,
        view_id: str,
    ) -> list[tuple[str, ...]]:
        """Get column keys for a view."""
        cached = self._col_key_cache.get(view_id)
        if cached is not None:
            return cached
        data = self.session.query("view_col_keys", view_id=view_id)
        keys = data.get("keys", []) if data else []
        self._col_key_cache[view_id] = keys
        return keys

    def row_header(
        self,
        view_id: str,
        section: int,
    ) -> str:
        """Get row header label for a given section."""
        data = self.session.query("view_row_header", view_id=view_id, section=section)
        if data:
            return data.get("header", "")
        return ""

    def addr_for_rc(
        self,
        view_id: str,
        row: int,
        col: int,
    ) -> tuple[str, ...]:
        """Resolve view row/col indices to full address tuple.

        Delegates to row_keys / col_keys queries, then addr_resolve.
        """
        row_keys = self.row_keys(view_id)
        col_keys = self.col_keys(view_id)
        if not row_keys or not col_keys:
            return ()
        if row < 0 or row >= len(row_keys) or col < 0 or col >= len(col_keys):
            return ()
        data = self.session.query(
            "addr_resolve",
            view_id=view_id,
            row_key=row_keys[row],
            col_key=col_keys[col],
        )
        if data:
            addr = data.get("addr", ())
            return tuple(addr) if isinstance(addr, list) else addr
        return ()

    def col_header(
        self,
        view_id: str,
        section: int,
    ) -> str:
        """Get column header label for a given section."""
        data = self.session.query("view_col_header", view_id=view_id, section=section)
        if data:
            return data.get("header", "")
        return ""
