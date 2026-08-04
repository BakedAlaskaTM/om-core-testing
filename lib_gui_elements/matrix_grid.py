from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import random
import string
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid

# Debug flag for GUI - set DEBUG_GUI=true to enable verbose logging
DEBUG_GUI = os.environ.get("DEBUG_GUI", "false").lower() in ("true", "1", "yes")
logger = logging.getLogger(__name__)

from lib_utils.coerce import coerce_user_value
from lib_contracts.types import CellFormat, OutlineNode
from lib_utils.config import gui as gui_config
from lib_utils.ids import new_id
from lib_utils.gui_profiler import GuiProfiler, NOOP_SPAN
from lib_contracts.gui_read_models import CellReadModel, GridReadModel, WorkspaceReadModel
from .matrix.metrics import GridMetrics
from .matrix.selection import SelectionManager
from .matrix.events import EventHelper
from .matrix.renderer import GridRenderer
from .matrix.grid import GridGeometry
from .matrix.outline import OutlineHelper
from .matrix.clipboard import ClipboardHelper
from .matrix.formatting import FormattingHelper, get_contrast_font_color
from .matrix.navigation import NavigationHelper
from .matrix.dimensions import DimensionHelper
from .matrix.banding import BandingHelper
from .matrix.header_edit import HeaderEditHelper
from .matrix.tooltips import TooltipHelper
from .matrix.group_drag import (
    get_descendant_sets,
    resolve_group_node_id,
    classify_drop_zone,
    count_leaf_descendants,
    is_noop_move,
    node_at_path,
    _parent_path_of,
)


def _cell_format_from_dict(fmt_dict: dict[str, Any]) -> CellFormat:
    """Deserialize a plain-dict format back into a CellFormat dataclass.

    Handles missing keys via CellFormat defaults.
    """
    if not fmt_dict:
        return CellFormat()
    return CellFormat(**fmt_dict)


class TileFetchThread(QtCore.QThread):
    """Fetch grid_viewport_snapshot_batch off the main thread and emit per-tile results."""

    tile_ready = QtCore.Signal(dict, tuple, int, bool, int)  # snapshot, bounds, generation, is_plain, data_gen

    def __init__(
        self,
        session: Any,
        view_id: str,
        tiles: list[tuple[tuple[int, int, int, int], list[tuple[str, ...]], list[tuple[str, ...]]]],
        page_selections: dict[str, str],
        channels: list[str],
        generation: int,
        data_gen: int = 0,
        plain: bool = False,
        parent: QtCore.QObject | None = None,
        profiler: GuiProfiler | None = None,
        parent_span_name: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._view_id = view_id
        self._tiles = tiles
        self._page_selections = page_selections
        self._channels = channels
        self._generation = generation
        self._data_gen = data_gen
        self._plain = plain
        self._profiler = profiler
        self._parent_span_name = parent_span_name

    def _do_batch_query(self) -> dict[str, Any]:
        """Blocking batch query wrapper — runs once per viewport update."""
        span = NOOP_SPAN
        if self._profiler is not None:
            span = self._profiler.span("TileFetchThread._do_batch_query", parent=self._parent_span_name)
        with span:
            tiles_payload = [
                {"bounds": list(bounds), "row_keys": row_keys, "col_keys": col_keys}
                for bounds, row_keys, col_keys in self._tiles
            ]
            cells = sum(len(rk) * len(ck) for _, rk, ck in self._tiles)
            DEBUG_GUI and print(
                f"[TILE-QUERY] start view={self._view_id[:8] if self._view_id else None} "
                f"tiles={len(self._tiles)} cells={cells} plain={self._plain}"
            )
            t0 = time.perf_counter()
            try:
                result = self._session.query(
                    "grid_viewport_snapshot_batch",
                    view_id=self._view_id,
                    tiles=tiles_payload,
                    page_selections=self._page_selections,
                    channels=self._channels,
                    generation=self._generation,
                    allow_evaluation=True,
                )
            except Exception as exc:
                logger.warning(
                    "[TileFetchThread] batch query failed view=%s exc=%s",
                    self._view_id[:8] if self._view_id else None, exc,
                )
                DEBUG_GUI and print(f"[TILE-QUERY] FAILED view={self._view_id[:8] if self._view_id else None} exc={exc}")
                return {}
            duration_ms = (time.perf_counter() - t0) * 1000
            DEBUG_GUI and print(
                f"[TILE-QUERY] done view={self._view_id[:8] if self._view_id else None} "
                f"tiles={len(self._tiles)} cells={cells} duration={duration_ms:.1f} ms"
            )
            if not isinstance(result, dict):
                logger.warning(
                    "[TileFetchThread] batch query returned non-dict %s for view=%s",
                    type(result).__name__,
                    self._view_id[:8] if self._view_id else None,
                )
                return {}
            if DEBUG_GUI:
                result_tiles = result.get("tiles", {})
                total_cells = sum(len(t.get("cells", {})) for t in result_tiles.values())
                DEBUG_GUI and print(
                    f"[TILE-QUERY] result view={self._view_id[:8] if self._view_id else None} "
                    f"tiles={len(result_tiles)} total_cells={total_cells} plain={self._plain}"
                )
            return result

    def run(self) -> None:
        """Fetch all tiles in one batch query and emit per-tile results."""
        DEBUG_GUI and print(
            f"[TILE-THREAD] start view={self._view_id[:8] if self._view_id else None} "
            f"tiles={len(self._tiles)} plain={self._plain} gen={self._generation} data_gen={self._data_gen}"
        )
        t0 = time.perf_counter()
        result = self._do_batch_query()
        # A generation mismatch means the request was superseded before the
        # batch completed; treat the whole response as stale so tiles are not
        # painted with outdated data.
        result_gen = result.get("generation")
        if result_gen != self._generation:
            DEBUG_GUI and print(
                f"[TILE-THREAD] discarding stale batch gen={result_gen} "
                f"current={self._generation} view={self._view_id[:8] if self._view_id else None}"
            )
            result_tiles = {}
        else:
            result_tiles = result.get("tiles", {})
        for bounds, row_keys, col_keys in self._tiles:
            if self.isInterruptionRequested():
                break
            bounds_key = json.dumps(list(bounds), separators=(",", ":"))
            snapshot = result_tiles.get(bounds_key, {})
            self.tile_ready.emit(snapshot or {}, bounds, self._generation, self._plain, self._data_gen)
            # Yield so the GUI thread and transport command handlers are not
            # starved while a long background prefetch run is in progress.
            time.sleep(0)
        dur = (time.perf_counter() - t0) * 1000
        DEBUG_GUI and print(
            f"[TILE-THREAD] finish view={self._view_id[:8] if self._view_id else None} "
            f"tiles={len(self._tiles)} duration={dur:.1f} ms"
        )


class RendererSignals(QtCore.QObject):
    rendered = QtCore.Signal(tuple, int, object, bool, int)  # bounds, generation, qimage, is_plain, data_gen


class TileRenderer(QtCore.QRunnable):
    """Render ONE tile's snapshot to QImage in a pool thread (up to 16 concurrent)."""

    def __init__(
        self,
        grid: "MatrixGrid",
        bounds: tuple[int, int, int, int],
        snapshot: dict[str, Any],
        generation: int,
        data_gen: int = 0,
        plain: bool = False,
    ) -> None:
        super().__init__()
        self.signals = RendererSignals()
        self._grid = grid
        self._bounds = bounds
        self._snapshot = snapshot
        self._generation = generation
        self._data_gen = data_gen
        self._plain = plain

    def run(self) -> None:
        # Reject stale tiles: plain tiles check _plain_generation; formatted check _tile_generation
        if self._plain:
            if self._generation != self._grid._plain_generation:
                return
        else:
            if self._generation != self._grid._tile_generation:
                return
        img = self._grid._render_tile_image(self._bounds, self._snapshot, self._plain)
        self.signals.rendered.emit(self._bounds, self._generation, img, self._plain, self._data_gen)


class MatrixGrid(QtWidgets.QAbstractScrollArea):
    """Minimal custom grid widget.

    Phase 1 goals (foundation):
    - Render row/col headers and cells using Engine view APIs
    - Scrollbars
    - Single-cell selection
    - Inline editing via QLineEdit

    Outline rendering (row_outline/col_outline) will be layered on top next.
    """

    selection_changed = QtCore.Signal()
    outline_changed = QtCore.Signal()
    content_changed = QtCore.Signal()
    presentation_changed = QtCore.Signal()  # Visual changes (widths, formats) that need sync
    cell_value_changed = QtCore.Signal(int, int, str)  # row, col, value - for recording
    tile_fetch_started = QtCore.Signal(str, str)  # view_id, reason
    tile_fetch_finished = QtCore.Signal(str)  # view_id

    @QtCore.Slot()
    def _do_navigate_slot(self) -> None:
        """Slot for thread-safe navigation from REPL. Executes _pending_navigate if set."""
        if hasattr(self, '_pending_navigate') and self._pending_navigate:
            self._pending_navigate()
            self._pending_navigate = None

    @QtCore.Slot()
    def _reload_slot(self) -> None:
        """Slot for thread-safe reload from REPL."""
        self.reload()

    @QtCore.Slot()
    def _on_group_drag_timer(self) -> None:
        """200 ms timer fired — group drag mode is now active."""
        self._group_drag_ready = True
        if self._drag_is_group and self._drag_group_node_id:
            self.viewport().setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            self.viewport().update()

    def __init__(
        self,
        *,
        view_id: str,
        session: Any,
        parent: QtWidgets.QWidget | None = None,
        grid_read_model=None,
        workspace_read_model=None,
        profiler: GuiProfiler | None = None,
    ) -> None:
        super().__init__(parent)
        if session is None:
            raise RuntimeError(
                "MatrixGrid requires session from the GUI composition root"
            )
        self._session = session
        self._view_id = view_id
        self._grid_read_model = grid_read_model or GridReadModel(session)
        self._workspace_read_model = workspace_read_model or WorkspaceReadModel(session)
        self._cell_read_model = CellReadModel(session)
        self._profiler = profiler
        self._span = profiler.span if profiler is not None else NOOP_SPAN
        self._m = GridMetrics()
        
        # Recursion guard to prevent signal storms
        self._reloading = False

        # Visible leaf keys for addressing.
        self._row_keys: list[tuple[str, ...]] = []
        self._col_keys: list[tuple[str, ...]] = []

        # Display rows include group rows too.
        self._rows: list[dict[str, Any]] = []
        self._cols: list[dict[str, Any]] = []

        # Multi-level column header bands derived from col_outline.
        # Each band item: {"level": int, "c0": int, "c1": int, "label": str, "path": tuple[int, ...]}
        self._col_bands: list[dict[str, Any]] = []
        # Total header rows to render (bands + optional leaf row).
        self._col_header_levels: int = 1
        # Number of band levels (excludes leaf row).
        self._col_band_levels: int = 0

        # Row bands (left-side spanning blocks), mirroring the col band structure.
        self._row_bands: list[dict[str, Any]] = []
        self._row_header_levels: int = 1
        self._row_band_levels: int = 0

        # Collapsed groups tracked by outline path.
        self._row_collapsed: set[tuple[int, ...]] = set()
        self._col_collapsed: set[tuple[int, ...]] = set()

        self._sel_row = 0
        self._sel_col = 0
        self._sel_mode: str = "cell"  # "cell", "row", "col"
        self._sel_indices: set[int] = set()  # For multi-selection of rows or cols
        self._sel_group_path: tuple[str, tuple[int, ...]] | None = None
        # Cross-mode selection storage (for lazy expansion)
        self._col_sel_indices: set[int] = set()
        self._row_sel_indices: set[int] = set()
        # Anchor for range selections (Shift+arrow)
        self._anchor_row = 0
        self._anchor_col = 0

        # Tile fetch thread (initialised to None; set when fetch starts)
        self._tile_fetch_thread = None

        # Flag to prevent selection reset during insert operations
        self._preserving_selection = False
        self._preserve_scroll = False  # Flag to prevent scroll during drag/drop

        # Selection manager (wraps selection state and operations)
        self._selection = SelectionManager(self)

        # Event helper (wraps event handling utilities)
        self._events = EventHelper(self)

        # Renderer helper (wraps rendering utilities)
        self._renderer = GridRenderer(self)

        # Geometry helper (coordinate transformations)
        self._geometry = GridGeometry(self)

        # Outline helper (outline structure manipulation)
        self._outline = OutlineHelper(self)

        # Clipboard helper (copy/paste operations)
        self._clipboard = ClipboardHelper(self)

        # Formatting helper (cell formatting operations)
        self._formatting = FormattingHelper(self)

        # Navigation helper (cell lookup and navigation)
        self._navigation = NavigationHelper(self)

        # Dimension helper (dimension/outline operations)
        self._dimensions = DimensionHelper(self)

        # Banding helper (band calculations)
        self._banding = BandingHelper(self)
        self._tooltips = TooltipHelper(self)

        # Header edit helper (header editing operations)
        self._header_edit = HeaderEditHelper(self)

        # Initialize these before editor setup to avoid AttributeError in eventFilter
        self._edit_mode: str = "navigation"
        self._suppress_editor_event_filter = False

        self._editor = QtWidgets.QLineEdit(self.viewport())
        self._editor.hide()
        self._editor.setFrame(False)
        self._editor.installEventFilter(self)
        self._editor.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self._editor.setStyleSheet(
            "QLineEdit {"
            "  border: 2px solid #2a76d2;"
            "  border-radius: 2px;"
            "  background: white;"
            "  padding: 1px 4px;"
            "}"
        )
        self._header_edit_ctx: dict[str, Any] | None = None
        self._pending_header_edit: tuple[str, int, str | None] | None = None
        self._ignore_next_grid_enter: bool = False

        self._drag_start_pos: QtCore.QPoint | None = None
        self._drag_item_id: str | None = None
        self._drag_group_path: tuple[int, ...] | None = None
        self._drag_group_level: int | None = None
        self._drag_group_first: int | None = None
        self._drag_group_last: int | None = None
        self._drag_axis: str | None = None  # "row" | "col"

        # Group-drag state (Phase 8)
        self._drag_is_group: bool = False
        self._drag_group_node_id: str | None = None
        self._group_drag_timer: QtCore.QTimer | None = None
        self._group_drag_ready: bool = False
        self._group_drag_highlight_rows: set[int] = set()
        self._group_drag_anchor_band_path: tuple[int, ...] | None = None
        self._group_drag_badge_count: int = 0

        # Drop hover: (mode, payload, rect)
        # mode: "col_into"|"row_into" → highlight; "col_reorder"|"row_reorder" → insert line
        # payload for "into": outline path tuple
        # payload for "reorder": (dest_index: int, after: bool)
        self._drop_hover: tuple[str, Any, QtCore.QRect] | None = None
        
        # Column resizing
        self._resize_col: int | None = None  # Column being resized
        self._resize_start_x: int | None = None  # Starting X position for resize
        self._resize_start_width: int | None = None  # Starting width
        self._col_widths: dict[int, int] = {}  # Custom widths per column index
        
        # Row header resizing
        self._resize_row_level: int | None = None  # Row header level being resized
        self._resize_start_width_row: int | None = None  # Starting width for row header
        self._row_header_widths: dict[int, int] = {}  # Custom widths per row header level

        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.viewport().setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.viewport().setFocusProxy(self)  # Forward focus from viewport to scroll area
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._did_initial_focus = False

        self._pending_repaint_tag = None
        self._pending_navigate = None

        # F5c: multi-tile snapshot cache for paint path
        # dict: tile_bounds -> snapshot dict (each tile is 1/4 viewport size)
        self._tile_cache: dict[tuple[int, int, int, int], dict[str, Any]] = {}
        self._tile_cache_gen: int = 0  # bumped on every formatted tile write
        # Pre-rendered QImage per tile (cells only, no selection overlay)
        self._tile_image_cache: dict[tuple[int, int, int, int], QtGui.QImage] = {}
        # Plain (value-only, unformatted) pre-rendered QImage per tile
        self._tile_plain_cache: dict[tuple[int, int, int, int], QtGui.QImage] = {}
        # Pending cell values: draw immediately after commit until new tile arrives
        self._pending_cell_values: dict[tuple[int, int], Any] = {}
        # Tile fetch thread state (background QThread)
        self._tile_fetch_thread: TileFetchThread | None = None
        self._tile_fetch_thread_plain: TileFetchThread | None = None
        self._tile_generation: int = 0
        self._plain_generation: int = 0
        self._data_generation: int = 0
        self._formatted_tile_data_gens: dict[tuple[int, int, int, int], int] = {}
        self._plain_image_data_gens: dict[tuple[int, int, int, int], int] = {}
        self._image_data_gens: dict[tuple[int, int, int, int], int] = {}
        # Fallback caches: previous tile images kept briefly during data reloads so
        # the grid never shows blank cells while new tiles are still rendering.
        self._tile_image_cache_fallback: dict[tuple[int, int, int, int], QtGui.QImage] = {}
        self._tile_plain_cache_fallback: dict[tuple[int, int, int, int], QtGui.QImage] = {}
        self._image_data_gens_fallback: dict[tuple[int, int, int, int], int] = {}
        self._plain_image_data_gens_fallback: dict[tuple[int, int, int, int], int] = {}
        self._pending_tile_fetch: bool = False
        self._tile_fetch_suppressed: bool = False
        self._local_selection_change_in_progress: bool = False
        # Parallel pre-render pool (size from config, default half CPU cores)
        self._tile_render_pool = QtCore.QThreadPool()
        self._tile_render_pool.setMaxThreadCount(
            gui_config("performance", "prerender_thread_pool_size", (os.cpu_count() or 4) // 2)
        )
        # 1-second debounce timer before any tile fetch starts
        self._tile_debounce_timer: QtCore.QTimer | None = None
        # Debounce timer for viewport updates after tile renders — batches
        # multiple tile completions into a single repaint to avoid tile-by-tile
        # flicker (especially jarring with volatile functions like RAND()).
        self._viewport_update_timer: QtCore.QTimer | None = None
        # Coalesce tile_ready signals on the GUI thread so a burst of tiles
        # does not schedule one QThreadPool runnable at a time.
        self._tile_ready_batch: list[tuple[dict[str, Any], tuple[int, int, int, int], int, bool, int]] = []
        self._tile_ready_batch_timer: QtCore.QTimer | None = None
        # F5c: cached view metadata (set during _do_reload, used in paintEvent)
        self._cached_view_meta: dict[str, Any] | None = None

        # Phase 5F: subscribe to session events for selection refresh.
        self._subscribe_session_events()

        self.reload()
        # Phase 5C: read session selection state into local rendering cache on init.
        self._apply_session_selection()

    def _is_system_cube_view(self) -> bool:
        """Check if this grid is showing a system cube view (%RECNOD, %RECEDG, etc.)."""
        view = self._workspace_read_model.get_view(self._view_id) if self._view_id else None
        if view is None:
            return False
        name = view.get("name", "")
        return isinstance(name, str) and name.startswith("%")

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        # Restore focus policies before the standard show logic runs.
        if hasattr(self, '_saved_focus_policy'):
            self.setFocusPolicy(self._saved_focus_policy)
        else:
            self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.viewport().setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self._editor.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        # Restore the focus proxy that hideEvent removed. Without this, the
        # viewport cannot receive focus on behalf of the grid and Qt's focus
        # manager may oscillate between tabs while resolving the next focus
        # target after a tab switch.
        self.viewport().setFocusProxy(self)
        super().showEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        # Defocus every focusable descendant so a pending timer or Qt focus
        # restoration cannot later steal focus and trigger a tab switch.
        self._editor.clearFocus()
        self.clearFocus()
        # Lock focus policies so Qt cannot deliver focus to hidden widgets.
        self._saved_focus_policy = self.focusPolicy()
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.viewport().setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._editor.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        super().hideEvent(event)

    def focusInEvent(self, event: QtGui.QFocusEvent) -> None:
        super().focusInEvent(event)

    def focusOutEvent(self, event: QtGui.QFocusEvent) -> None:
        super().focusOutEvent(event)

    def setFocus(self, reason: QtCore.Qt.FocusReason = QtCore.Qt.FocusReason.OtherFocusReason) -> None:
        if not self.isVisible():
            return
        self.viewport().setFocusProxy(self)
        super().setFocus(reason)

    def activateWindow(self) -> None:
        super().activateWindow()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Cleanly shut down background threads and timers to avoid segfaults."""
        self._invalidate_snapshot_cache()
        if self._tile_debounce_timer is not None:
            self._tile_debounce_timer.stop()
        if self._viewport_update_timer is not None:
            self._viewport_update_timer.stop()
        if self._tile_render_pool is not None:
            self._tile_render_pool.waitForDone(1000)
        super().closeEvent(event)

    def _mime_type(self) -> str:
        return "application/x-openm-dim"

    def _set_edit_mode(self, mode: str) -> None:
        mode = mode or "navigation"
        if mode == self._edit_mode:
            return
        self._edit_mode = mode
        print(f"=== EDIT MODE: {self.edit_mode_label().upper()} ===")

    def edit_mode_label(self) -> str:
        mapping = {
            "label": "Label mode",
            "cell": "Data mode",
            "navigation": "Navigation mode",
        }
        return mapping.get(self._edit_mode, "Navigation mode")
    
    def _col_width(self, col_idx: int) -> int:
        """Get the width of a column (custom or default)."""
        return self._geometry.col_width(col_idx)

    def _row_header_level_width(self, level: int) -> int:
        """Get the width of a row header level (custom or default 80px)."""
        return self._geometry.row_header_level_width(level)
    
    def _scroll_offset(self) -> QtCore.QPoint:
        """Get scroll offset - delegated to helper."""
        return self._geometry.scroll_offset()

    def _get_resize_row_level(self, pos: QtCore.QPoint) -> int | None:
        """Detect if mouse is near a row header level edge for resizing. Returns level index or None."""
        x = pos.x()
        y = pos.y()
        header_h = self._m.col_header_h * max(1, self._col_header_levels)
        
        # Only allow resizing in row header area (left side, below column headers)
        if y < header_h:
            return None
        
        # Check if near right edge of any row header level (within 6 pixels)
        level_x = 0
        for level in range(self._row_band_levels + 1):  # +1 for leaf column
            level_w = self._row_header_level_width(level)
            level_x += level_w
            if abs(x - level_x) <= 6:
                return level
        
        return None
    
    def _get_resize_col(self, pos: QtCore.QPoint) -> int | None:
        """Detect if mouse is near a column edge for resizing. Returns column index or None."""
        off = self._scroll_offset()
        x_view = pos.x()
        x = x_view + off.x()
        y = pos.y()
        header_h = self._m.col_header_h * max(1, self._col_header_levels)
        row_header_w = self._row_header_width()

        # Allow resizing in entire header area (including group bands)
        if y >= header_h or x_view < row_header_w:
            return None
        
        # Check if near right edge of any column (within 6 pixels for easier targeting)
        # This works for both leaf headers and group band edges
        col_x = row_header_w
        for c in range(len(self._cols)):
            col_w = self._col_width(c)
            col_x += col_w
            if abs(x - col_x) <= 6:
                return c
        
        return None

    def _header_hit(self, pos: QtCore.QPoint) -> tuple[str, str | tuple[int, ...] | None] | None:
        off = self._scroll_offset()
        x_view = pos.x()
        y_view = pos.y()
        x = x_view + off.x()
        y = y_view + off.y()
        header_h = self._m.col_header_h * max(1, self._col_header_levels)
        row_header_w = self._row_header_width()
        rpw = 80  # pixels per row-header level

        if x_view < row_header_w and y_view >= header_h:
            # Determine row-header level using actual widths per level
            cumulative = 0
            level = 0
            for lvl in range(max(1, self._row_header_levels)):
                cumulative += self._row_header_level_width(lvl)
                if x_view < cumulative:
                    level = lvl
                    break
            r = int((y - header_h) // self._m.row_h)
            if level >= self._row_band_levels:
                # Leaf column
                if 0 <= r < len(self._rows):
                    row = self._rows[r]
                    if row.get("is_leaf", False):
                        iid = row.get("item_id")
                        return ("row_leaf", (iid, r))
                return ("row_bg", None)
            else:
                # Band column — find which group spans this row at this level
                if 0 <= r < len(self._rows):
                    for band in self._row_bands:
                        if int(band.get("level", -1)) != level:
                            continue
                        r0 = int(band.get("r0", -1))
                        r1 = int(band.get("r1", -2))
                        if r0 <= r <= r1:
                            path = band.get("path")
                            label = band.get("label", "")
                            if isinstance(path, tuple) and path:
                                print(f"[DEBUG header_hit] row_group MATCHED: path={path}, label={label}, level={level}, r={r}, r range {r0}-{r1}")
                                return ("row_group", (path, r0, r1, r))
                    print(f"[DEBUG header_hit] row_group NO MATCH: r={r}, level={level}, bands={len(self._row_bands)}")
                return ("row_bg", None)

        if y_view < header_h and x_view >= row_header_w:
            # Determine column index using actual widths (handles repeating item_ids across groups)
            col_x = row_header_w
            c = -1
            for i in range(len(self._cols)):
                col_w = self._col_width(i)
                if col_x <= x < col_x + col_w:
                    c = i
                    break
                col_x += col_w

            if self._col_band_levels > 0:
                level = int(y_view // self._m.col_header_h)
                if level >= self._col_band_levels:
                    # Leaf header row
                    if 0 <= c < len(self._cols):
                        DEBUG_GUI and print(f"DEBUG header_hit: col_leaf c={c}, level={level}, x={x}, y={y}")
                        return ("col_leaf", c)
                    return ("col_bg", None)
                # Band row — find which group spans column c at this level (use actual widths for c0/c1)
                for band in self._col_bands:
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
                if 0 <= c < len(self._cols):
                    DEBUG_GUI and print(f"DEBUG header_hit: col_leaf(no bands) c={c}, x={x}, y={y}")
                    return ("col_leaf", c)
                return ("col_bg", None)

        return None

    @property
    def view_id(self) -> str:
        return self._view_id

    @property
    def session(self) -> CommandSession:
        """Read-only public session property for boundary decoupling.

        Replaces private ``_grid._session`` access from formatting.py.
        """
        return self._session

    def _build_page_selections(self) -> dict[str, str]:
        """Build page_selections dict from GUI view-state cache.

        Returns a ``dict[str, str]`` mapping dimension ID to selected item ID,
        including ``@`` and any page dimensions.  If no GUI view-state cache
        exists, this helper falls back to the workspace read model.
        """
        page_selections: dict[str, str] = {}
        # @ dimension page selection
        at_item = self._workspace_read_model.page_selection(self._view_id, "@")
        page_selections["@"] = at_item if at_item else "at_value"
        # Other page dimensions via cached view metadata (avoids paint-path query)
        view_meta = self._cached_view_meta or {}
        for dim_id in view_meta.get("page_dim_ids", []):
            item_id = self._workspace_read_model.page_selection(self._view_id, dim_id)
            if item_id:
                page_selections[dim_id] = item_id
        if self._is_system_cube_view() and DEBUG_GUI:
            print(f"[SYS-CUBE] _build_page_selections: {page_selections}", flush=True)
        return page_selections

    def _is_thread_alive(self, thread: QtCore.QThread | None) -> bool:
        """Return True if thread is non-None and its C++ object still exists."""
        if thread is None:
            return False
        try:
            return thread.isRunning()
        except RuntimeError:
            # C++ object has already been deleted; treat as not alive.
            return False

    def formatted_fetch_running(self) -> bool:
        """Return True if the formatted (visible) tile fetch is in progress."""
        return self._is_thread_alive(self._tile_fetch_thread)

    def formatted_fetch_active(self) -> bool:
        """Return True if a formatted tile fetch is running or has been scheduled.

        This also covers the gap between reload() scheduling a fetch and the
        actual TileFetchThread starting.
        """
        return (
            self._is_thread_alive(self._tile_fetch_thread)
            or getattr(self, '_force_tile_refetch', False)
            or getattr(self, '_pending_tile_fetch', False)
        )

    def _invalidate_snapshot_cache(self) -> None:
        """Invalidate the disposable paint cache (full: plain + formatted)."""
        self._tile_cache.clear()
        self._tile_image_cache.clear()
        self._tile_plain_cache.clear()
        self._formatted_tile_data_gens.clear()
        self._plain_image_data_gens.clear()
        self._image_data_gens.clear()
        self._pending_cell_values.clear()
        self._pending_tile_fetch = False
        if self._is_thread_alive(self._tile_fetch_thread):
            self._tile_fetch_thread.requestInterruption()
        if self._is_thread_alive(self._tile_fetch_thread_plain):
            self._tile_fetch_thread_plain.requestInterruption()
            self._plain_generation += 1
        if self._tile_render_pool is not None:
            self._tile_render_pool.clear()
        if self._tile_debounce_timer is not None:
            self._tile_debounce_timer.stop()
        if self._tile_ready_batch_timer is not None:
            self._tile_ready_batch_timer.stop()
        self._tile_ready_batch.clear()

    def _invalidate_tile_images(self) -> None:
        """Invalidate all rendered tile images (used for row-header resize).

        Row header width changes shift the entire grid horizontally, so
        every tile's x-positions are stale.
        """
        self._tile_image_cache.clear()
        self._tile_plain_cache.clear()
        self._image_data_gens.clear()
        self._plain_image_data_gens.clear()

    def _invalidate_tile_images_for_cols(self, affected_cols: set[int]) -> None:
        """Invalidate tiles that overlap any of the affected columns.

        When a column is resized, only tiles that actually contain that column
        need re-rendering (the cell width inside the tile image is stale).
        Tiles entirely to the left or right of the affected column(s) are
        still valid; paintEvent places them correctly via tile_x.
        """
        for bounds in list(self._tile_image_cache.keys()):
            tile_first_col, tile_last_col = bounds[2], bounds[3]
            if any(tile_first_col <= c <= tile_last_col for c in affected_cols):
                self._tile_image_cache.pop(bounds, None)
                self._image_data_gens.pop(bounds, None)
        for bounds in list(self._tile_plain_cache.keys()):
            tile_first_col, tile_last_col = bounds[2], bounds[3]
            if any(tile_first_col <= c <= tile_last_col for c in affected_cols):
                self._tile_plain_cache.pop(bounds, None)
                self._plain_image_data_gens.pop(bounds, None)
        # Also evict snapshot data for affected tiles so coverage check
        # triggers a re-fetch + re-render instead of leaving stale images missing
        for bounds in list(self._tile_cache.keys()):
            tile_first_col, tile_last_col = bounds[2], bounds[3]
            if any(tile_first_col <= c <= tile_last_col for c in affected_cols):
                self._tile_cache.pop(bounds, None)
                self._formatted_tile_data_gens.pop(bounds, None)

    def _invalidate_tile_images_for_structural_change(
        self,
        old_col_keys: list[tuple[str, ...]],
        old_row_keys: list[tuple[str, ...]],
    ) -> None:
        """Invalidate tiles affected by row/column insertion, deletion, or reorder.

        Tiles entirely to the left of the first changed column and entirely
        above the first changed row are preserved.  Everything else is stale
        because cell positions or content shifted inside the tile image.
        """
        first_diff_col: int | None = None
        for i in range(max(len(old_col_keys), len(self._col_keys))):
            if (
                i >= len(old_col_keys)
                or i >= len(self._col_keys)
                or old_col_keys[i] != self._col_keys[i]
            ):
                first_diff_col = i
                break

        first_diff_row: int | None = None
        for i in range(max(len(old_row_keys), len(self._row_keys))):
            if (
                i >= len(old_row_keys)
                or i >= len(self._row_keys)
                or old_row_keys[i] != self._row_keys[i]
            ):
                first_diff_row = i
                break

        if first_diff_col is None and first_diff_row is None:
            return  # No structural change

        for bounds in list(self._tile_image_cache.keys()):
            _, last_row, _, last_col = bounds
            if (
                (first_diff_col is not None and last_col >= first_diff_col)
                or (first_diff_row is not None and last_row >= first_diff_row)
            ):
                self._tile_image_cache.pop(bounds, None)
                self._image_data_gens.pop(bounds, None)

        for bounds in list(self._tile_plain_cache.keys()):
            _, last_row, _, last_col = bounds
            if (
                (first_diff_col is not None and last_col >= first_diff_col)
                or (first_diff_row is not None and last_row >= first_diff_row)
            ):
                self._tile_plain_cache.pop(bounds, None)
                self._plain_image_data_gens.pop(bounds, None)
        # Also evict snapshot data so coverage check triggers re-fetch + re-render
        for bounds in list(self._tile_cache.keys()):
            _, last_row, _, last_col = bounds
            if (
                (first_diff_col is not None and last_col >= first_diff_col)
                or (first_diff_row is not None and last_row >= first_diff_row)
            ):
                self._tile_cache.pop(bounds, None)
                self._formatted_tile_data_gens.pop(bounds, None)

    def _invalidate_formatted_cache(self) -> None:
        """Interrupt the formatted fetch thread only. Caches are preserved for reuse."""
        self._pending_tile_fetch = False
        if self._is_thread_alive(self._tile_fetch_thread):
            self._tile_fetch_thread.requestInterruption()

    def _compute_visible_bounds(self) -> tuple[int, int, int, int]:
        """Return (first_row, last_row, first_col, last_col) for current viewport."""
        if not self._rows or not self._cols:
            return (0, 0, 0, 0)
        off = self._scroll_offset()
        vp = self.viewport().rect()
        x0 = off.x()
        y0 = off.y()
        header_h = self._m.col_header_h * max(1, self._col_header_levels)
        row_header_w = self._row_header_width()
        first_col = 0
        last_col = len(self._cols) - 1
        col_x = row_header_w
        for c in range(len(self._cols)):
            col_w = self._col_width(c)
            if col_x + col_w >= x0:
                first_col = c
                break
            col_x += col_w
        col_x = row_header_w
        for c in range(len(self._cols)):
            col_w = self._col_width(c)
            col_x += col_w
            if col_x >= x0 + vp.width():
                last_col = c
                break
        first_row = max(0, (y0 - header_h) // self._m.row_h)
        last_row = min(len(self._rows) - 1, (y0 - header_h + vp.height()) // self._m.row_h)
        return (first_row, last_row, first_col, last_col)

    def set_view(self, view_id: str) -> None:
        self._view_id = view_id
        self._invalidate_snapshot_cache()
        self.reload()
        # Phase 5C: read session selection state into local rendering cache.
        self._apply_session_selection()

    def _apply_session_selection(self) -> None:
        """Read session selection state via query and apply to local rendering cache.

        Phase 5C: MatrixGrid read path.  This is a rendering-cache update only;
        SessionStore remains the source of truth.
        """
        try:
            data = self._session.query("selection_current")
            if not data or data.get("type") != "selection_current":
                return
            cursor = data.get("cursor", (0, 0))
            anchor = data.get("anchor", (0, 0))
            mode = data.get("mode", "cell")
            selected_indices = data.get("selected_indices", [])

            max_row = max(0, len(self._rows) - 1)
            max_col = max(0, len(self._cols) - 1)

            self._sel_row = max(0, min(cursor[0], max_row))
            self._sel_col = max(0, min(cursor[1], max_col))
            self._anchor_row = max(0, min(anchor[0], max_row))
            self._anchor_col = max(0, min(anchor[1], max_col))

            if mode in ("cell", "row", "col", "all"):
                self._sel_mode = mode
            if self._sel_mode == "all":
                self._sel_indices.clear()  # Empty = all visible cells
            elif self._sel_mode == "cell":
                self._sel_indices.clear()
                if selected_indices:
                    for item in selected_indices:
                        if isinstance(item, (list, tuple)) and len(item) == 2:
                            r, c = item
                            if 0 <= r <= max_row and 0 <= c <= max_col:
                                self._sel_indices.add((r, c))
                else:
                    if 0 <= self._sel_row < len(self._rows) and 0 <= self._sel_col < len(self._cols):
                        self._sel_indices.add((self._sel_row, self._sel_col))
            elif self._sel_mode == "row":
                if selected_indices:
                    self._sel_indices = {
                        int(i) for i in selected_indices
                        if isinstance(i, int) and 0 <= i <= max_row
                    }
                else:
                    self._sel_indices = {self._sel_row} if 0 <= self._sel_row < len(self._rows) else set()
                self._sel_col = 0
            elif self._sel_mode == "col":
                if selected_indices:
                    self._sel_indices = {
                        int(i) for i in selected_indices
                        if isinstance(i, int) and 0 <= i <= max_col
                    }
                else:
                    self._sel_indices = {self._sel_col} if 0 <= self._sel_col < len(self._cols) else set()
                self._sel_row = 0
        except Exception:
            # TEMP BRIDGE: failures fall back to existing selection preservation.
            pass

    def _subscribe_session_events(self) -> None:
        """Subscribe to session selection events for this grid's session.

        Phase 5F: when another client (REPL, macro, remote) changes
        selection, the grid refreshes from SessionStore via query.
        """
        try:
            self._session_event_handler = lambda event: self._on_session_event(event)
            self._session.subscribe("event.selection.changed", self._session_event_handler)
            self._session.subscribe("event.active_view.changed", self._session_event_handler)
        except Exception:
            pass

    def _on_session_event(self, event) -> None:
        """Handle session events: refresh grid from query if for our session.

        This is read-only: it calls query("selection_current") and applies
        the DTO to local rendering cache. It does NOT emit set_selection.
        Skips events triggered by the grid's own local selection writes.
        """
        if self._local_selection_change_in_progress:
            return
        try:
            payload = getattr(event, "payload", {}) or {}
            event_session_id = payload.get("session_id")
            our_session_id = getattr(self._session, "session_id", None)
            if event_session_id is None or event_session_id != our_session_id:
                return
            event_view_id = payload.get("view_id")
            if event_view_id is not None and event_view_id != self._view_id:
                return
            topic = getattr(event, "topic", "")
            if topic == "event.active_view.changed":
                # Do not apply global session selection on view switch:
                # the global store still holds the *old* view's selection.
                return
            self._apply_session_selection()
            self.viewport().update()
        except Exception:
            pass

    def apply_format_to_selection(self, format_type: str, value: object) -> None:
        """Apply a format change to the current selection - only to cells, not headers."""
        return self._formatting.apply_format_to_selection(format_type, value)

    def _update_format(self, fmt: CellFormat, format_type: str, value: object) -> CellFormat:
        """Update a CellFormat with a new format value."""
        return self._formatting.update_format(fmt, format_type, value)

    def _draw_diagonal_shading(self, p: QtGui.QPainter, rect: QtCore.QRect, color: str = "#e8e8e8") -> None:
        """Draw diagonal lines (45 degrees, bottom-left to top-right) for group shading."""
        p.save()
        pen = QtGui.QPen(QtGui.QColor(color), 1)
        pen.setStyle(QtCore.Qt.PenStyle.SolidLine)
        p.setPen(pen)
        
        left = rect.left()
        right = rect.right()
        top = rect.top()
        bottom = rect.bottom()
        
        # Draw diagonal lines from bottom-left to top-right
        spacing = 8  # pixels between lines
        
        # Start from left edge, bottom area
        for start_x in range(left - (bottom - top), right, spacing):
            # Calculate intersection points
            # Line: from (start_x, bottom) going up-right at 45 degrees
            x1 = max(left, start_x)
            y1 = bottom - (x1 - start_x)
            
            x2 = min(right, start_x + (bottom - top))
            y2 = bottom - (x2 - start_x)
            
            # Clip to rectangle
            if y1 > bottom:
                x1 += (y1 - bottom)
                y1 = bottom
            if y2 < top:
                x2 -= (top - y2)
                y2 = top
            
            if x1 < right and x2 > left and y1 > top and y2 < bottom:
                p.drawLine(QtCore.QPointF(x1, y1), QtCore.QPointF(x2, y2))
        
        p.restore()

    def _draw_cell_borders(self, p: QtGui.QPainter, rect: QtCore.QRect, fmt: CellFormat) -> None:
        """Draw custom borders on a cell based on its format."""
        # Helper to create pen with style
        def create_border_pen(thickness: str) -> QtGui.QPen:
            width = 2 if thickness == "thick" else 1
            pen = QtGui.QPen(QtGui.QColor(fmt.border_color), width)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.FlatCap)
            
            # Set pen style based on border_style
            if fmt.border_style == "dashed":
                pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            elif fmt.border_style == "dotted":
                pen.setStyle(QtCore.Qt.PenStyle.DotLine)
            else:  # solid
                pen.setStyle(QtCore.Qt.PenStyle.SolidLine)
            
            return pen

        def border_width(thickness: str) -> int:
            return 2 if thickness == "thick" else 1

        def draw_corner(x: float, y: float, thickness_a: str, thickness_b: str) -> None:
            width = max(border_width(thickness_a), border_width(thickness_b))
            size = float(width)
            half = size / 2.0
            p.save()
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QBrush(QtGui.QColor(fmt.border_color)))
            p.drawRect(QtCore.QRectF(x - half, y - half, size, size))
            p.restore()

        left = float(rect.left())
        right = float(rect.right())
        top = float(rect.top())
        bottom = float(rect.bottom())
        inner_left = left + 1.0
        inner_right = right - 1.0
        inner_top = top + 1.0
        inner_bottom = bottom - 1.0
        corner_right = right - 0.5
        corner_bottom = bottom - 0.5
        corner_left = left + 0.5
        corner_top = top + 0.5
        edge_right = right
        edge_bottom = bottom
        
        # Draw top border
        if fmt.border_top != "none":
            pen = create_border_pen(fmt.border_top)
            p.setPen(pen)
            p.drawLine(
                QtCore.QPointF(corner_left, inner_top),
                QtCore.QPointF(corner_right, inner_top),
            )
        
        # Draw bottom border
        if fmt.border_bottom != "none":
            pen = create_border_pen(fmt.border_bottom)
            p.setPen(pen)
            p.drawLine(
                QtCore.QPointF(corner_left, edge_bottom),
                QtCore.QPointF(right, edge_bottom),
            )
        
        # Draw left border
        if fmt.border_left != "none":
            pen = create_border_pen(fmt.border_left)
            p.setPen(pen)
            p.drawLine(
                QtCore.QPointF(inner_left, corner_top),
                QtCore.QPointF(inner_left, corner_bottom),
            )
        
        # Draw right border
        if fmt.border_right != "none":
            pen = create_border_pen(fmt.border_right)
            p.setPen(pen)
            p.drawLine(
                QtCore.QPointF(edge_right, corner_top),
                QtCore.QPointF(edge_right, bottom),
            )

        if fmt.border_top != "none" and fmt.border_left != "none":
            draw_corner(inner_left, inner_top, fmt.border_top, fmt.border_left)
        if fmt.border_top != "none" and fmt.border_right != "none":
            draw_corner(edge_right, inner_top, fmt.border_top, fmt.border_right)
        if fmt.border_bottom != "none" and fmt.border_left != "none":
            draw_corner(inner_left, edge_bottom, fmt.border_bottom, fmt.border_left)
        if fmt.border_bottom != "none" and fmt.border_right != "none":
            draw_corner(edge_right, edge_bottom, fmt.border_bottom, fmt.border_right)

    def _get_text_alignment(self, h_align: str, v_align: str) -> QtCore.Qt.AlignmentFlag:
        """Convert format alignment strings to Qt alignment flags."""
        # Horizontal alignment
        if h_align == "center":
            h_flag = QtCore.Qt.AlignmentFlag.AlignHCenter
        elif h_align == "right":
            h_flag = QtCore.Qt.AlignmentFlag.AlignRight
        else:  # left
            h_flag = QtCore.Qt.AlignmentFlag.AlignLeft
        
        # Vertical alignment
        if v_align == "top":
            v_flag = QtCore.Qt.AlignmentFlag.AlignTop
        elif v_align == "bottom":
            v_flag = QtCore.Qt.AlignmentFlag.AlignBottom
        else:  # middle
            v_flag = QtCore.Qt.AlignmentFlag.AlignVCenter
        
        return h_flag | v_flag

    def _get_cell_format(self, r: int, c: int) -> CellFormat:
        """Get the CellFormat for a cell at (r, c)."""
        return self._formatting.get_cell_format(r, c)
    
    def _format_value(self, value: str, format_number: str) -> str:
        """Format a value according to the numeric format pattern."""
        return self._formatting.format_value(value, format_number)

    def _format_null(self, format_null: str) -> str:
        """Format null/empty value according to format pattern."""
        if format_null == "na":
            return "N/A"
        elif format_null == "dash":
            return "—"
        return ""

    def _format_text(self, value: str, format_text: str) -> str:
        """Format text value according to format pattern."""
        from .matrix.value_format import _format_text
        return _format_text(value, format_text)

    def _fetch_all_cells_batch(self) -> None:
        """Batch fetch all visible cells to avoid N round-trips."""
        if not self._row_keys or not self._col_keys:
            return
        
        # Build list of all (row_key, col_key) pairs
        addresses = []
        addr_to_rc = {}
        for r_i, row in enumerate(self._rows):
            if not row.get("is_leaf", False):
                continue
            try:
                row_key = self._row_keys[self._leaf_row_index(r_i)]
            except Exception:
                continue
            for c in range(len(self._col_keys)):
                col_key = self._col_keys[c]
                addr = (row_key, col_key)
                addresses.append(addr)
                addr_to_rc[addr] = (r_i, c)
        
    def _get_cached_cell(self, r: int, c: int) -> Any | None:
        """Get cell snapshot from any cached tile (no engine reads)."""
        try:
            from lib_utils.viewport_keys import make_viewport_cell_key
            row_key = self._row_keys[self._leaf_row_index(r)]
            col_key = self._col_keys[c]
            cell_key = make_viewport_cell_key(row_key, col_key)
            for snapshot in self._tile_cache.values():
                cell = snapshot.get("cells", {}).get(cell_key)
                if cell is not None:
                    return cell
            return None
        except Exception:
            return None

    def _fetch_visible_cells(self, first_row: int, last_row: int, first_col: int, last_col: int) -> tuple[dict[tuple[int, int], Any], set[tuple[str, ...]], dict[tuple[int, int], str | None], dict[tuple[int, int], str | None]]:
        """Read-only: unpack visible cells from multiple cached tiles."""
        with self._span("MatrixGrid._fetch_visible_cells"):
            cells: dict[tuple[int, int], Any] = {}
            hard_addrs: set[tuple[str, ...]] = set()
            font_colors: dict[tuple[int, int], str | None] = {}
            fills: dict[tuple[int, int], str | None] = {}

            from lib_utils.viewport_keys import make_viewport_cell_key
            for tile_bounds, snapshot in self._tile_cache.items():
                # NEW: skip stale snapshot data
                if self._formatted_tile_data_gens.get(tile_bounds, -1) != self._data_generation:
                    continue
                t_first, t_last, t_fc, t_lc = tile_bounds
                # Skip tiles that don't overlap the visible range
                if t_last < first_row or t_first > last_row or t_lc < first_col or t_fc > last_col:
                    continue
                snapshot_cells = snapshot.get("cells", {})
                snapshot_channels = snapshot.get("channels", {})
                hard_addrs.update(
                    {tuple(a) if isinstance(a, list) else a for a in snapshot.get("user_override_addrs", [])}
                )
                for r in range(max(first_row, t_first), min(last_row, t_last) + 1):
                    try:
                        row_key = self._row_keys[self._leaf_row_index(r)]
                    except Exception:
                        continue
                    for c in range(max(first_col, t_fc), min(last_col, t_lc) + 1):
                        try:
                            col_key = self._col_keys[c]
                            cell_key = make_viewport_cell_key(row_key, col_key)
                            cell = snapshot_cells.get(cell_key)
                            if cell is not None:
                                cells[(r, c)] = cell
                            font_colors[(r, c)] = snapshot_channels.get("font_color", {}).get(cell_key)
                            fills[(r, c)] = snapshot_channels.get("fill", {}).get(cell_key)
                        except Exception:
                            pass

            # If any visible area is not covered by cached tiles, schedule a fetch.
            # Use the same tile grid as _start_tile_fetch_impl and treat the area as
            # covered only when every intersecting tile is present and fresh.
            tile_size = max(10, gui_config("performance", "prefetch_max_tile_size", 10))
            tile_size = min(tile_size, 256)
            fresh_bounds = {
                bounds for bounds in self._tile_cache
                if self._formatted_tile_data_gens.get(bounds, -1) == self._data_generation
            }
            missing_tiles = self._build_tile_list(
                first_row, last_row, first_col, last_col, tile_size, tile_size, fresh_bounds
            )
            if missing_tiles:
                self._schedule_tile_fetch()

            is_sys = self._is_system_cube_view()
            if is_sys and DEBUG_GUI:
                total_tiles = len(self._tile_cache)
                fresh_tiles = len(fresh_bounds)
                stale_tiles = total_tiles - fresh_tiles
                visible_cells_count = len(cells)
                visible_range = (first_row, last_row, first_col, last_col)
                print(
                    f"[SYS-CUBE] _fetch_visible_cells view={self._view_id[:8] if self._view_id else None} "
                    f"range={visible_range} data_gen={self._data_generation} "
                    f"tiles={total_tiles} fresh={fresh_tiles} stale={stale_tiles} "
                    f"missing={len(missing_tiles)} cells_found={visible_cells_count} "
                    f"row_keys={len(self._row_keys)} col_keys={len(self._col_keys)}",
                    flush=True,
                )
                if total_tiles == 0:
                    print(f"[SYS-CUBE]   -> tile_cache is EMPTY, no tiles fetched yet", flush=True)
                elif fresh_tiles == 0:
                    print(f"[SYS-CUBE]   -> all tiles are STALE (data_gen mismatch)", flush=True)
                if visible_cells_count > 0:
                    for (r_v, c_v), cell_v in list(cells.items())[:5]:
                        print(
                            f"[SYS-CUBE]   -> cell ({r_v},{c_v}) value={cell_v.get('value')!r} "
                            f"source={cell_v.get('source')} error={cell_v.get('error')}",
                            flush=True,
                        )
                if visible_cells_count == 0 and fresh_tiles > 0:
                    for tile_bounds_check, snapshot_check in self._tile_cache.items():
                        if self._formatted_tile_data_gens.get(tile_bounds_check, -1) != self._data_generation:
                            continue
                        sc = snapshot_check.get("cells", {})
                        print(
                            f"[SYS-CUBE]   -> fresh tile {tile_bounds_check} has {len(sc)} cells, "
                            f"sample keys: {list(sc.keys())[:3]}",
                            flush=True,
                        )
                        if sc:
                            for r_check in range(max(first_row, tile_bounds_check[0]), min(last_row, tile_bounds_check[1]) + 1):
                                try:
                                    rk = self._row_keys[self._leaf_row_index(r_check)]
                                except Exception:
                                    print(f"[SYS-CUBE]   -> leaf_row_index({r_check}) FAILED", flush=True)
                                    continue
                                for c_check in range(max(first_col, tile_bounds_check[2]), min(last_col, tile_bounds_check[3]) + 1):
                                    try:
                                        ck = self._col_keys[c_check]
                                        ck_str = make_viewport_cell_key(rk, ck)
                                        has = ck_str in sc
                                        if not has:
                                            print(f"[SYS-CUBE]   -> cell_key MISS r={r_check} c={c_check} rk={rk} ck={ck} key={ck_str[:60]}", flush=True)
                                    except Exception as e:
                                        print(f"[SYS-CUBE]   -> cell_key ERROR r={r_check} c={c_check}: {e}", flush=True)
                                    break
                                break

            return cells, hard_addrs, font_colors, fills

    def _schedule_tile_fetch(self) -> None:
        """Restart the 50ms debounce timer. Coalesces rapid paint-path requests."""
        if self._tile_debounce_timer is None:
            self._tile_debounce_timer = QtCore.QTimer(self)
            self._tile_debounce_timer.setSingleShot(True)
            self._tile_debounce_timer.timeout.connect(self._start_tile_fetch)
        self._tile_debounce_timer.stop()
        self._tile_debounce_timer.start(50)

    def _schedule_viewport_update(self) -> None:
        """Coalesce viewport updates from rapid tile renders into one repaint.

        A 50 ms debounce batches multiple tile completions (common after a
        data change where every cell contains a volatile function) so the
        grid updates in one smooth frame instead of flickering tile-by-tile.
        """
        if self._viewport_update_timer is None:
            self._viewport_update_timer = QtCore.QTimer(self)
            self._viewport_update_timer.setSingleShot(True)
            self._viewport_update_timer.timeout.connect(self.viewport().update)
        self._viewport_update_timer.stop()
        self._viewport_update_timer.start(50)

    def _build_tile_list(
        self,
        area_first_r: int,
        area_last_r: int,
        area_first_c: int,
        area_last_c: int,
        tile_h: int,
        tile_w: int,
        skip_cache: set[tuple[int, int, int, int]],
    ) -> list[tuple[tuple[int, int, int, int], list[tuple[str, ...]], list[tuple[str, ...]]]]:
        """Build tile specs for a given area, skipping tiles already in skip_cache.
        Tile boundaries are aligned to fixed grid positions (multiples of tile_h/tile_w)
        so the same tile is always fetched regardless of viewport position."""
        tiles: list[tuple[tuple[int, int, int, int], list[tuple[str, ...]], list[tuple[str, ...]]]] = []
        aligned_first_r = (area_first_r // tile_h) * tile_h
        aligned_first_c = (area_first_c // tile_w) * tile_w
        for tr in range(aligned_first_r, area_last_r + 1, tile_h):
            for tc in range(aligned_first_c, area_last_c + 1, tile_w):
                t_first = tr
                t_last = min(tr + tile_h - 1, len(self._rows) - 1)
                t_fc = tc
                t_lc = min(tc + tile_w - 1, len(self._cols) - 1)
                bounds = (t_first, t_last, t_fc, t_lc)
                if bounds in skip_cache:
                    continue
                row_keys = []
                for r in range(t_first, t_last + 1):
                    try:
                        row_keys.append(self._row_keys[self._leaf_row_index(r)])
                    except Exception:
                        continue
                col_keys = []
                for c in range(t_fc, t_lc + 1):
                    try:
                        col_keys.append(self._col_keys[c])
                    except Exception:
                        continue
                if row_keys and col_keys:
                    tiles.append((bounds, row_keys, col_keys))
        return tiles

    def set_tile_fetch_suppressed(self, suppressed: bool) -> None:
        """Suppress or resume background tile fetches.

        While suppressed, tile fetch requests are remembered but not started.
        When suppression is lifted, any pending request is started
        immediately.  The GUI uses this to prevent tile fetches between a
        model mutation and the completion of `run_recalculation(scope="dirty")`.
        """
        self._tile_fetch_suppressed = suppressed
        if not suppressed and getattr(self, '_pending_tile_fetch', False):
            self._start_tile_fetch()

    def refresh_for_generation(self, generation: int) -> None:
        """Invalidate cached tiles and schedule a fetch for the given engine generation."""
        self._data_generation = generation
        self._tile_generation += 1
        self._plain_generation += 1
        self._formatted_tile_data_gens.clear()
        self._plain_image_data_gens.clear()
        self._image_data_gens.clear()
        self._tile_cache.clear()
        self._tile_image_cache.clear()
        self._tile_plain_cache.clear()
        self._tile_image_cache_fallback.clear()
        self._tile_plain_cache_fallback.clear()
        self._image_data_gens_fallback.clear()
        self._plain_image_data_gens_fallback.clear()
        self._pending_cell_values.clear()
        self._force_tile_refetch = True
        self._pending_tile_fetch = True
        self.viewport().update()
        self._start_tile_fetch()

    def _start_tile_fetch(self) -> None:
        """Start formatted thread immediately (viewport-specific); plain thread is background prefetch."""
        if not isValid(self):
            return
        with self._span("MatrixGrid._start_tile_fetch"):
            self._start_tile_fetch_impl()

    def _start_tile_fetch_impl(self) -> None:
        self._pending_tile_fetch = False
        is_sys = self._is_system_cube_view()
        if not self._rows or not self._cols or not self._view_id or self._session is None:
            if is_sys and DEBUG_GUI:
                print(
                    f"[SYS-CUBE] _start_tile_fetch_impl EARLY RETURN: "
                    f"rows={len(self._rows)} cols={len(self._cols)} "
                    f"view_id={self._view_id[:8] if self._view_id else None} "
                    f"session={self._session is not None}",
                    flush=True,
                )
            return
        if getattr(self, '_tile_fetch_suppressed', False):
            self._pending_tile_fetch = True
            if is_sys and DEBUG_GUI:
                print(
                    f"[SYS-CUBE] _start_tile_fetch_impl SUPPRESSED: "
                    f"tile_fetch_suppressed=True, deferring",
                    flush=True,
                )
            return

        # Refresh runtime config that may have changed via Options dialog
        import os
        new_alpha = gui_config("appearance", "selection_alpha", 120)
        try:
            new_alpha = int(new_alpha)
        except (ValueError, TypeError):
            new_alpha = 120
        if self._m.sel_bg.alpha() != new_alpha:
            self._m.sel_bg.setAlpha(max(0, min(255, new_alpha)))
        new_pool_size = max(1, gui_config("performance", "prerender_thread_pool_size", (os.cpu_count() or 4) // 2))
        if self._tile_render_pool.maxThreadCount() != new_pool_size:
            self._tile_render_pool.setMaxThreadCount(new_pool_size)
            # Changing pool size may release queued stale renderers; purge everything
            self._tile_render_pool.clear()
            self._invalidate_snapshot_cache()

        # Formatted thread is viewport-specific: interrupt and restart on every scroll
        if self._is_thread_alive(self._tile_fetch_thread):
            self._tile_fetch_thread.requestInterruption()
            self._pending_tile_fetch = True
            return

        first_row, last_row, first_col, last_col = self._compute_visible_bounds()
        visible_rows = max(1, last_row - first_row + 1)
        visible_cols = max(1, last_col - first_col + 1)
        # Tile size from config (1 to 256). Very small tiles create a huge number
        # of per-tile signals and queries that can overwhelm the GUI thread, so
        # clamp to a sane minimum regardless of the config file.
        tile_size = max(10, gui_config("performance", "prefetch_max_tile_size", 10))
        tile_size = min(tile_size, 256)
        tile_h = tile_size
        tile_w = tile_size

        # Plain preload: a quarter viewport in each direction gives enough nearby
        # data without generating enough background queries to starve the engine/GUI.
        preload_margin_r = max(1, visible_rows // 4)
        preload_margin_c = max(1, visible_cols // 4)
        plain_first_r = max(0, first_row - preload_margin_r)
        plain_last_r = min(len(self._rows) - 1, last_row + preload_margin_r)
        plain_first_c = max(0, first_col - preload_margin_c)
        plain_last_c = min(len(self._cols) - 1, last_col + preload_margin_c)
        # Align to tile grid so plain and formatted tiles share the same grid
        plain_first_r = (plain_first_r // tile_h) * tile_h
        plain_first_c = (plain_first_c // tile_w) * tile_w

        # Viewport center for distance sorting
        vc_r = (first_row + last_row) / 2
        vc_c = (first_col + last_col) / 2

        page_selections = self._build_page_selections()
        # Fetch all format/visual/alignment channels the formatted renderer uses.
        # The value/source/addr come from the cells dict; view-level static formats
        # come from the workspace read model.  @-dimension format values
        # (font_weight, format_number, etc.) must be requested explicitly or they
        # will be missing from the tile snapshot channels.
        fmt_channels = [
            "fill",
            "font_color",
            "format_number",
            "format_text",
            "format_null",
            "format_error",
            "font_family",
            "font_size",
            "font_weight",
            "font_italic",
            "text_h_align",
            "text_v_align",
            "text_indent",
            "text_wrap",
        ]

        plain_enabled = gui_config("performance", "prerender_plain_data", False)

        def _tile_priority(item: tuple) -> tuple[int, float]:
            bounds, _, _ = item
            overlaps = not (
                bounds[1] < first_row or bounds[0] > last_row
                or bounds[3] < first_col or bounds[2] > last_col
            )
            tc_r = (bounds[0] + bounds[1]) / 2
            tc_c = (bounds[2] + bounds[3]) / 2
            dist = abs(vc_r - tc_r) + abs(vc_c - tc_c)
            return (0 if overlaps else 1, dist)

        self._tile_generation += 1
        # When force-refetching, also bump plain generation and interrupt the
        # old plain thread so stale plain tiles cannot arrive after cache clear
        # and pop pending cell values, which would leave edited cells blank.
        if getattr(self, '_force_tile_refetch', False):
            self._plain_generation += 1
            if self._is_thread_alive(self._tile_fetch_thread_plain):
                self._tile_fetch_thread_plain.requestInterruption()
        gen = self._tile_generation

        # --- Plain fetch (value-only, large preload area, background, best-effort) ---
        if plain_enabled:
            # Only start plain thread if not already running; it is a background prefetcher
            if not self._is_thread_alive(self._tile_fetch_thread_plain):
                plain_skip = set(self._tile_plain_cache.keys()) | set(self._tile_image_cache.keys())
                plain_tiles = self._build_tile_list(plain_first_r, plain_last_r, plain_first_c, plain_last_c, tile_h, tile_w, plain_skip)
                if plain_tiles:
                    plain_tiles.sort(key=_tile_priority)
                    plain_thread = TileFetchThread(
                        session=self._session,
                        view_id=self._view_id,
                        tiles=plain_tiles,
                        page_selections=page_selections,
                        channels=[],  # value-only, no extra channels
                        generation=self._plain_generation,
                        data_gen=self._data_generation,
                        plain=True,
                        parent=None,
                        profiler=self._profiler,
                        parent_span_name=self._profiler.current_span_name() if self._profiler is not None else None,
                    )
                    plain_thread.tile_ready.connect(self._on_tile_ready)
                    plain_thread.finished.connect(self._on_plain_thread_finished)
                    plain_thread.finished.connect(plain_thread.deleteLater)
                    self._tile_fetch_thread_plain = plain_thread
                    plain_thread.start()

        # Determine the reason for the formatted fetch before consuming flags.
        if getattr(self, '_force_tile_refetch', False):
            tile_reason = "data_change"
        elif not self._tile_cache:
            tile_reason = "initial"
        else:
            tile_reason = "scroll"

        # --- Formatted fetch (full channels, visible area only) ---
        # After reload _tile_cache is empty, so refetch even if old images exist.
        # During scroll _tile_cache has data, so skip tiles that have both snapshot and image.
        # When _force_tile_refetch is set (data changed), ignore the skip and
        # refetch everything so stale values are replaced.
        if getattr(self, '_force_tile_refetch', False):
            fmt_skip: set = set()
            self._force_tile_refetch = False
        else:
            # Tiles are skippable only if they have fresh data AND a rendered image
            fmt_skip = {
                bounds for bounds in (set(self._tile_image_cache.keys()) & set(self._tile_cache.keys()))
                if self._formatted_tile_data_gens.get(bounds, -1) == self._data_generation
                and self._image_data_gens.get(bounds, -1) == self._data_generation
            }
        fmt_tiles = self._build_tile_list(first_row, last_row, first_col, last_col, tile_h, tile_w, fmt_skip)
        if is_sys and DEBUG_GUI:
            print(
                f"[SYS-CUBE] _start_tile_fetch_impl LAUNCH: "
                f"reason={tile_reason} fmt_tiles={len(fmt_tiles)} "
                f"skip={len(fmt_skip)} gen={gen} data_gen={self._data_generation} "
                f"page_sel={page_selections}",
                flush=True,
            )
        if fmt_tiles:
            fmt_tiles.sort(key=_tile_priority)
            fmt_thread = TileFetchThread(
                session=self._session,
                view_id=self._view_id,
                tiles=fmt_tiles,
                page_selections=page_selections,
                channels=fmt_channels,
                generation=gen,
                data_gen=self._data_generation,
                plain=False,
                parent=None,
                profiler=self._profiler,
                parent_span_name=self._profiler.current_span_name() if self._profiler is not None else None,
            )
            fmt_thread.tile_ready.connect(self._on_tile_ready)
            fmt_thread.finished.connect(self._on_tile_thread_finished)
            fmt_thread.finished.connect(fmt_thread.deleteLater)
            self._tile_fetch_thread = fmt_thread
            self.tile_fetch_started.emit(self._view_id, tile_reason)
            fmt_thread.start()

    @QtCore.Slot()
    def _on_tile_ready(self, snapshot: dict, bounds: tuple, generation: int, is_plain: bool = False, data_gen: int = 0) -> None:
        """Receive one small tile from background thread; queue pre-render to pool."""
        is_sys = self._is_system_cube_view()
        if is_plain:
            if generation != self._plain_generation:
                if is_sys and DEBUG_GUI:
                    print(f"[SYS-CUBE] _on_tile_ready REJECTED plain gen={generation} current={self._plain_generation} bounds={bounds}", flush=True)
                DEBUG_GUI and print(f"DEBUG _on_tile_ready: REJECTED plain gen={generation} current={self._plain_generation}")
                return
        elif generation != self._tile_generation:
            if is_sys and DEBUG_GUI:
                print(f"[SYS-CUBE] _on_tile_ready REJECTED fmt gen={generation} current={self._tile_generation} bounds={bounds}", flush=True)
            DEBUG_GUI and print(f"DEBUG _on_tile_ready: REJECTED fmt gen={generation} current={self._tile_generation}")
            return
        # NEW: reject tile fetched before a mutation but delivered after it
        if data_gen != self._data_generation:
            if is_sys and DEBUG_GUI:
                print(f"[SYS-CUBE] _on_tile_ready REJECTED stale data_gen={data_gen} current={self._data_generation} bounds={bounds}", flush=True)
            DEBUG_GUI and print(f"DEBUG _on_tile_ready: REJECTED stale data_gen={data_gen} current={self._data_generation}")
            return
        if not is_plain:
            self._tile_cache[bounds] = snapshot
            self._tile_cache_gen += 1
            self._formatted_tile_data_gens[bounds] = data_gen
            self._tile_image_cache.pop(bounds, None)   # force re-render
            self._image_data_gens.pop(bounds, None)
        if is_sys and DEBUG_GUI:
            cell_count = len(snapshot.get("cells", {}))
            print(f"[SYS-CUBE] _on_tile_ready ACCEPTED bounds={bounds} cells={cell_count} plain={is_plain} gen={generation} data_gen={data_gen}", flush=True)
        # Coalesce rapid tile arrivals on the GUI thread and schedule renderers
        # in one pass rather than one QThreadPool.start() per signal.
        self._tile_ready_batch.append((snapshot, bounds, generation, is_plain, data_gen))
        if self._tile_ready_batch_timer is None:
            self._tile_ready_batch_timer = QtCore.QTimer(self)
            self._tile_ready_batch_timer.setSingleShot(True)
            self._tile_ready_batch_timer.timeout.connect(self._flush_tile_ready_batch)
        self._tile_ready_batch_timer.stop()
        self._tile_ready_batch_timer.start(0)

    @QtCore.Slot()
    def _flush_tile_ready_batch(self) -> None:
        """Schedule renderers for all tiles that arrived since the last flush."""
        batch = self._tile_ready_batch
        self._tile_ready_batch = []
        if not batch:
            return
        fmt_count = sum(1 for _, _, _, is_plain, _ in batch if not is_plain)
        plain_count = len(batch) - fmt_count
        DEBUG_GUI and print(f"DEBUG _on_tile_ready: flush batch size={len(batch)} fmt={fmt_count} plain={plain_count}")
        for snapshot, bounds, generation, is_plain, data_gen in batch:
            renderer = TileRenderer(
                grid=self,
                bounds=bounds,
                snapshot=snapshot,
                generation=generation,
                data_gen=data_gen,
                plain=is_plain,
            )
            renderer.signals.rendered.connect(self._on_tile_rendered)
            self._tile_render_pool.start(renderer)

    @QtCore.Slot()
    def _on_tile_rendered(self, bounds: tuple, generation: int, img: object, is_plain: bool = False, data_gen: int = 0) -> None:
        """Receive pre-rendered QImage from renderer worker."""
        if is_plain:
            if generation != self._plain_generation:
                DEBUG_GUI and print(f"DEBUG _on_tile_rendered: REJECTED plain gen={generation}")
                return
        elif generation != self._tile_generation:
            DEBUG_GUI and print(f"DEBUG _on_tile_rendered: REJECTED fmt gen={generation} current={self._tile_generation}")
            return
        # NEW: reject image rendered from stale data snapshot
        if data_gen != self._data_generation:
            DEBUG_GUI and print(f"DEBUG _on_tile_rendered: REJECTED stale data_gen={data_gen} current={self._data_generation}")
            return
        if img is not None and isinstance(img, QtGui.QImage):
            if is_plain:
                self._tile_plain_cache[bounds] = img
                self._plain_image_data_gens[bounds] = data_gen
                # If this plain tile covers any pending cell, evict the stale
                # formatted tile for the same bounds so paintEvent falls back to
                # this fresh plain data instead of drawing a pre-edit formatted tile.
                t_first, t_last, t_fc, t_lc = bounds
                for r in range(t_first, t_last + 1):
                    for c in range(t_fc, t_lc + 1):
                        if (r, c) in self._pending_cell_values:
                            self._tile_image_cache.pop(bounds, None)
                            self._image_data_gens.pop(bounds, None)
                            break
                # A fresh plain tile also evicts the previous fallback image.
                self._tile_plain_cache_fallback.pop(bounds, None)
                self._plain_image_data_gens_fallback.pop(bounds, None)
            else:
                self._tile_image_cache[bounds] = img
                self._image_data_gens[bounds] = data_gen
                # Evict plain fallback so plain tiles are only shown while formatted is not ready
                self._tile_plain_cache.pop(bounds, None)
                self._plain_image_data_gens.pop(bounds, None)
                # A fresh formatted tile replaces the previous generation's fallback.
                self._tile_image_cache_fallback.pop(bounds, None)
                self._image_data_gens_fallback.pop(bounds, None)
                self._tile_plain_cache_fallback.pop(bounds, None)
                self._plain_image_data_gens_fallback.pop(bounds, None)
                DEBUG_GUI and print(
                    f"DEBUG _on_tile_rendered: STORED fmt bounds={bounds} "
                    f"gen={generation} data_gen={data_gen} img_cache={len(self._tile_image_cache)}"
                )
        else:
            DEBUG_GUI and print(f"DEBUG _on_tile_rendered: BLANK img bounds={bounds} gen={generation} img={img}")
        # Remove pending values for cells now covered by a rendered tile
        t_first, t_last, t_fc, t_lc = bounds
        for r in range(t_first, t_last + 1):
            for c in range(t_fc, t_lc + 1):
                self._pending_cell_values.pop((r, c), None)
        # Only trigger repaint if this tile overlaps the current viewport
        first_row, last_row, first_col, last_col = self._compute_visible_bounds()
        if bounds[1] < first_row or bounds[0] > last_row or bounds[3] < first_col or bounds[2] > last_col:
            return
        self._schedule_viewport_update()

    @QtCore.Slot()
    def _on_tile_thread_finished(self) -> None:
        """When formatted thread finishes, restart if a new viewport fetch was queued."""
        thread = self.sender()
        if self._tile_fetch_thread is thread:
            self._tile_fetch_thread = None
        self.tile_fetch_finished.emit(self._view_id)
        fmt_running = self._is_thread_alive(self._tile_fetch_thread)
        if self._pending_tile_fetch and not fmt_running:
            self._start_tile_fetch()

    @QtCore.Slot()
    def _on_plain_thread_finished(self) -> None:
        """Release the plain tile thread reference after it finishes."""
        thread = self.sender()
        if self._tile_fetch_thread_plain is thread:
            self._tile_fetch_thread_plain = None

    def _render_tile_image(self, bounds: tuple[int, int, int, int], snapshot: dict[str, Any], plain: bool = False) -> QtGui.QImage | None:
        """Pre-render a tile's cells to a QImage. No selection overlay — that is drawn in paintEvent."""
        first_row, last_row, first_col, last_col = bounds
        if not self._rows or not self._cols:
            return None

        # Compute pixel dimensions
        w = sum(self._col_width(c) for c in range(first_col, last_col + 1))
        h = (last_row - first_row + 1) * self._m.row_h
        if w <= 0 or h <= 0:
            return None

        img = QtGui.QImage(w, h, QtGui.QImage.Format.Format_ARGB32)
        img.fill(QtGui.QColor("white"))
        p = QtGui.QPainter(img)
        p.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
        try:
            snapshot_cells = snapshot.get("cells", {})
            snapshot_channels = snapshot.get("channels", {})
            if not isinstance(snapshot_cells, dict):
                print(f"[RENDER] cells is not a dict ({type(snapshot_cells).__name__}), treating as empty")
                snapshot_cells = {}
            if not isinstance(snapshot_channels, dict):
                print(f"[RENDER] channels is not a dict ({type(snapshot_channels).__name__}), treating as empty")
                snapshot_channels = {}
            hard_addrs = {tuple(a) if isinstance(a, list) else a for a in snapshot.get("user_override_addrs", [])}
            from lib_utils.viewport_keys import make_viewport_cell_key
            from lib_contracts.types import get_value_type

            custom_borders: list[tuple[QtCore.QRect, CellFormat]] = []

            y = 0
            for r_i in range(first_row, last_row + 1):
                if r_i >= len(self._rows):
                    y += self._m.row_h
                    continue
                row = self._rows[r_i]
                if not row.get("is_leaf", False):
                    y += self._m.row_h
                    continue

                x = 0
                for c in range(first_col, last_col + 1):
                    col_w = self._col_width(c)
                    cell_r = QtCore.QRect(x, y, col_w, self._m.row_h)

                    # Resolve cell key
                    try:
                        row_key = self._row_keys[self._leaf_row_index(r_i)]
                        col_key = self._col_keys[c]
                        cell_key = make_viewport_cell_key(row_key, col_key)
                    except Exception:
                        x += col_w
                        continue

                    if plain:
                        # Plain: white background, gridline, simple text
                        p.fillRect(cell_r, QtGui.QColor("white"))
                        p.setPen(self._m.gridline)
                        p.drawRect(cell_r)

                        cell = snapshot_cells.get(cell_key)
                        if cell is not None:
                            cell_value = cell.get("error") or cell.get("value")
                            p.setPen(QtGui.QColor("#202020"))
                            default_font = QtGui.QFont("sans-serif", 9)
                            p.setFont(default_font)
                            v = "" if cell_value is None else str(cell_value)
                            align = self._get_text_alignment("left", "center")
                            p.drawText(cell_r.adjusted(4, 0, -4, 0), align, v)

                        x += col_w
                        continue

                    # --- Formatted path below ---
                    # Format
                    try:
                        fmt = self._get_cell_format(r_i, c)
                    except Exception:
                        fmt = CellFormat()

                    # Background
                    engine_fill = snapshot_channels.get("fill", {}).get(cell_key)
                    bg_color = engine_fill if engine_fill else (fmt.bg_color if fmt.bg_color else "white")
                    try:
                        if isinstance(bg_color, str) and bg_color.startswith("#") and len(bg_color) in (4, 7, 9):
                            color = QtGui.QColor(bg_color)
                        elif isinstance(bg_color, str):
                            color = QtGui.QColor(bg_color)
                        else:
                            color = QtGui.QColor("white")
                    except Exception:
                        color = QtGui.QColor("white")
                    p.fillRect(cell_r, color)

                    # Gridline
                    p.setPen(self._m.gridline)
                    p.drawRect(cell_r)

                    # Cell data
                    cell = snapshot_cells.get(cell_key)
                    if cell is None:
                        if self._is_system_cube_view() and DEBUG_GUI:
                            print(f"[SYS-CUBE] _render_tile_image cell_key MISS: r={r_i} c={c} key={cell_key[:60]}", flush=True)
                        x += col_w
                        continue

                    cell_addr = cell.get("addr")
                    if isinstance(cell_addr, list):
                        cell_addr = tuple(cell_addr)
                    is_hardnumber = cell_addr in hard_addrs if cell_addr else False
                    cell_value = cell.get("value")
                    cell_source = cell.get("source")
                    is_hard_value = is_hardnumber or (cell_source == "override" and cell_value is not None)

                    if self._is_system_cube_view() and DEBUG_GUI:
                        _vt = get_value_type(cell_value)
                        print(
                            f"[SYS-CUBE] _render_tile_image CELL r={r_i} c={c} key={cell_key[:60]} "
                            f"value={cell_value!r} type={_vt} source={cell_source} error={cell.get('error')}",
                            flush=True,
                        )

                    if is_hard_value:
                        p.fillRect(cell_r.adjusted(1, 1, 0, 0), QtGui.QColor("#ffff99"))
                        # Red triangle
                        ts = 6
                        tri = QtGui.QPolygon([
                            QtCore.QPoint(cell_r.right() - ts + 1, cell_r.top() + 1),
                            QtCore.QPoint(cell_r.right() + 1, cell_r.top() + 1),
                            QtCore.QPoint(cell_r.right() + 1, cell_r.top() + ts + 1),
                        ])
                        p.setBrush(QtGui.QColor("#ff0000"))
                        p.setPen(QtCore.Qt.PenStyle.NoPen)
                        p.drawPolygon(tri)
                        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)

                    # Text
                    engine_font_color = snapshot_channels.get("font_color", {}).get(cell_key)
                    effective_bg = engine_fill or fmt.bg_color or "white"
                    text_pen = QtGui.QColor(
                        engine_font_color if engine_font_color else get_contrast_font_color(effective_bg)
                    )
                    p.setPen(text_pen)

                    value_type = get_value_type(cell_value)
                    cell_error = cell.get("error")
                    if cell_error:
                        value_type = "error"
                        cell_value = cell_error
                    try:
                        if value_type == "null":
                            v = self._format_null(fmt.format_null)
                        elif value_type == "error":
                            v = str(cell_value) if not fmt.format_error else f"[{cell_value}]"
                        elif value_type == "numeric":
                            v = "" if cell_value is None else str(cell_value)
                            v = self._format_value(v, fmt.format_number)
                        else:
                            v = "" if cell_value is None else str(cell_value)
                            v = self._format_text(v, fmt.format_text)
                    except Exception:
                        v = "" if cell_value is None else str(cell_value)

                    if self._is_system_cube_view() and DEBUG_GUI:
                        print(
                            f"[SYS-CUBE]   -> rendered v={v!r} value_type={value_type} "
                            f"format_null={fmt.format_null!r} font_color={engine_font_color!r} "
                            f"bg={effective_bg!r}",
                            flush=True,
                        )

                    font = QtGui.QFont(
                        str(fmt.font_family) if fmt.font_family else "sans-serif",
                        int(fmt.font_size) if fmt.font_size else 9,
                    )
                    if fmt.font_weight:
                        try:
                            fw = int(fmt.font_weight)
                            fw = max(1, min(1000, fw))
                            font.setWeight(QtGui.QFont.Weight(fw))
                        except (ValueError, TypeError):
                            pass
                    font.setItalic(bool(fmt.font_italic))
                    p.setFont(font)

                    h_align = fmt.text_h_align
                    if h_align == "left" and value_type == "numeric":
                        h_align = "right"
                    align = self._get_text_alignment(h_align, fmt.text_v_align)
                    p.drawText(cell_r.adjusted(4, 0, -4, 0), align, v)

                    if (
                        fmt.border_top != "none"
                        or fmt.border_bottom != "none"
                        or fmt.border_left != "none"
                        or fmt.border_right != "none"
                    ):
                        custom_borders.append((QtCore.QRect(cell_r), fmt))

                    x += col_w
                y += self._m.row_h

            # Custom borders
            if custom_borders:
                for cell_r, fmt in custom_borders:
                    self._draw_cell_borders(p, cell_r, fmt)
        except Exception as exc:
            print(f"[RENDER] exception in tile render view={self._view_id[:8] if self._view_id else None}: {exc}")
        finally:
            p.end()
        return img

    def _cell_in_cached_tile_image(self, r: int, c: int) -> bool:
        """Return True if cell (r, c) is inside any FRESH pre-rendered tile image (formatted or plain)."""
        for bounds in self._tile_image_cache:
            if bounds[0] <= r <= bounds[1] and bounds[2] <= c <= bounds[3]:
                if self._image_data_gens.get(bounds, -1) == self._data_generation:
                    return True
        for bounds in self._tile_plain_cache:
            if bounds[0] <= r <= bounds[1] and bounds[2] <= c <= bounds[3]:
                if self._plain_image_data_gens.get(bounds, -1) == self._data_generation:
                    return True
        # During data reloads the previous generation's tile images act as a
        # fallback so selection overlay and gridlines are still drawn.
        for bounds in self._tile_image_cache_fallback:
            if bounds[0] <= r <= bounds[1] and bounds[2] <= c <= bounds[3]:
                return True
        for bounds in self._tile_plain_cache_fallback:
            if bounds[0] <= r <= bounds[1] and bounds[2] <= c <= bounds[3]:
                return True
        return False

    def execute_command(self, command_id: str, **kwargs: Any) -> Any:
        """Route a graph mutation command through the session executor.

        This replaces the old direct CommandDispatcher.dispatch path with
        the canonical session.execute(...) boundary.
        """
        if self._session is None:
            raise RuntimeError(
                "MatrixGrid has no session. "
                "Pass session when creating the grid."
            )
        return self._session.execute(command_id, **kwargs)

    def reload(self, *, invalidate_tiles: bool | str = False) -> None:
        with self._span("MatrixGrid.reload"):
            logger.info("[MatrixGrid] reload view=%s invalidate_tiles=%r", self._view_id[:8] if self._view_id else None, invalidate_tiles)
            t0 = time.perf_counter()
            self._reload_impl(invalidate_tiles=invalidate_tiles)
            dur = (time.perf_counter() - t0) * 1000
            logger.info("[MatrixGrid] reload done view=%s duration=%.3f ms", self._view_id[:8] if self._view_id else None, dur)
            DEBUG_GUI and print(f"[RELOAD] MatrixGrid.reload view={self._view_id[:8] if self._view_id else None} invalidate={invalidate_tiles!r} duration={dur:.1f} ms")

    def _reload_impl(self, *, invalidate_tiles: bool | str = False) -> None:
        # Prevent recursive reload calls that could cause freezes
        DEBUG_GUI and print(f"[RELOAD-IMPL] view={self._view_id[:8] if self._view_id else None} invalidate={invalidate_tiles!r} _reloading={self._reloading}")
        if self._reloading:
            DEBUG_GUI and print(f"[RELOAD-IMPL] view={self._view_id[:8] if self._view_id else None} already reloading, returning")
            return
        self._grid_read_model.invalidate_cache(self._view_id)
        if invalidate_tiles is True or invalidate_tiles == "all":
            # Structure/outline or width change: _do_reload() below rebuilds
            # rows/cols and decides whether _tile_cache (snapshot data) must
            # be cleared.  We only clear it when keys actually change;
            # for a pure width change the snapshot data is still valid so
            # paintEvent can draw cells directly while new tile images are
            # being fetched, avoiding white gaps.
            self._data_generation += 1
            self._tile_generation += 1
            self._plain_generation += 1
            self._formatted_tile_data_gens.clear()
            self._plain_image_data_gens.clear()
            self._image_data_gens.clear()
            self._tile_image_cache_fallback.clear()
            self._tile_plain_cache_fallback.clear()
            self._image_data_gens_fallback.clear()
            self._plain_image_data_gens_fallback.clear()
            self._pending_cell_values.clear()
            self._pending_tile_fetch = False
            self._force_tile_refetch = True
        elif invalidate_tiles == "data":
            # Data-only change: skip the expensive _do_reload() rebuild.
            # Bump data generation so paint path skips stale cached tiles
            # while new tiles are being fetched.
            self._data_generation += 1
            self._tile_generation += 1
            self._plain_generation += 1
            # Preserve the previous rendered tiles as a fallback so the grid
            # does not go blank while the new tile batch is still rendering.
            self._tile_image_cache_fallback = dict(self._tile_image_cache)
            self._tile_plain_cache_fallback = dict(self._tile_plain_cache)
            self._image_data_gens_fallback = dict(self._image_data_gens)
            self._plain_image_data_gens_fallback = dict(self._plain_image_data_gens)
            self._formatted_tile_data_gens.clear()
            self._plain_image_data_gens.clear()
            self._image_data_gens.clear()
            self._force_tile_refetch = True
            self._pending_tile_fetch = False
            pending_rows_cols = set(self._pending_cell_values.keys())
            if pending_rows_cols:
                for bounds in list(self._tile_image_cache.keys()):
                    t_first, t_last, t_fc, t_lc = bounds
                    if any(
                        t_first <= r <= t_last and t_fc <= c <= t_lc
                        for r, c in pending_rows_cols
                    ):
                        self._tile_image_cache.pop(bounds, None)
                        self._image_data_gens.pop(bounds, None)
            DEBUG_GUI and print(
                f"DEBUG reload(data): img_cache={len(self._tile_image_cache)} "
                f"snapshot_cache={len(self._tile_cache)} "
                f"plain_cache={len(self._tile_plain_cache)}"
            )
            if self._viewport_update_timer is not None:
                self._viewport_update_timer.stop()
            if self._is_thread_alive(self._tile_fetch_thread):
                self._tile_fetch_thread.requestInterruption()
            if self._is_thread_alive(self._tile_fetch_thread_plain):
                self._tile_fetch_thread_plain.requestInterruption()
                self._plain_generation += 1
            if self._tile_debounce_timer is not None:
                self._tile_debounce_timer.stop()
            # Refresh cached metadata in case formats/col-widths changed
            view = (
                self._workspace_read_model.get_view(self._view_id)
                if self._workspace_read_model is not None
                else None
            )
            if view is not None:
                self._cached_view_meta = {
                    "view_id": self._view_id,
                    "row_dim_ids": list(view.get("row_dim_ids", []) or []),
                    "col_dim_ids": list(view.get("col_dim_ids", []) or []),
                    "page_dim_ids": list(view.get("page_dim_ids", []) or []),
                    "item_formats": {k: _cell_format_from_dict(v) for k, v in view.get("item_formats", {}).items()},
                    "group_formats": {k: _cell_format_from_dict(v) for k, v in view.get("group_formats", {}).items()},
                    "cell_formats": {k: _cell_format_from_dict(v) for k, v in view.get("cell_formats", {}).items()},
                    "col_widths": dict(view.get("col_widths", {})),
                    "row_header_widths": dict(view.get("row_header_widths", {})),
                }
                # Only overwrite the width map that is not currently being resized.
                # ARCHITECTURE_DEBT: This mixes presentation-state (drag in progress) into
                # data-refresh logic. Longer-term, drag-local presentation state should be
                # isolated from model reload state (e.g., via a PresentationStateGuard).
                # Normalize keys to int because JSON persistence returns string keys.
                if self._resize_col is None:
                    self._col_widths = {int(k): v for k, v in view.get("col_widths", {}).items()}
            # Kick off background tile fetch; old images remain visible until
            # new tiles arrive and the viewport-update debounce fires.
            QtCore.QTimer.singleShot(0, self._start_tile_fetch)
            return
        else:
            # Scroll/structure change: keep rendered tile images visible
            # during reload to prevent white-flash.
            # Only clear snapshot data so _start_tile_fetch() knows to refetch.
            self._tile_cache.clear()
            self._pending_tile_fetch = False
        if self._is_thread_alive(self._tile_fetch_thread):
            self._tile_fetch_thread.requestInterruption()
        if self._tile_debounce_timer is not None:
            self._tile_debounce_timer.stop()
        DEBUG_GUI and print(f"DEBUG SCROLL: reload() called, _preserve_scroll={self._preserve_scroll}")
        self._reloading = True
        try:
            self._do_reload()
        finally:
            self._reloading = False

    def current_view_meta(self) -> dict[str, Any] | None:
        """Return cached view metadata dict without calling Engine.

        Returns None when no active view metadata exists or, where the
        cached metadata carries a view_id, when it does not match the active view.
        """
        view = getattr(self, "_cached_view_meta", None)
        if not view:
            return None
        active_view_id = getattr(self, "_view_id", None)
        cached_view_id = view.get("view_id")
        if cached_view_id is not None and cached_view_id != active_view_id:
            return None
        return view

    def _do_reload(self) -> None:
        view = self._workspace_read_model.get_view(self._view_id)
        meta = {
            "row_dim_ids": list(view.get("row_dim_ids", []) or []) if view else None,
            "col_dim_ids": list(view.get("col_dim_ids", []) or []) if view else None,
            "page_dim_ids": list(view.get("page_dim_ids", []) or []) if view else None,
        }
        is_sys = self._is_system_cube_view()
        if is_sys and DEBUG_GUI:
            view_name = view.get("name", "?") if view else "?"
            print(f"[SYS-CUBE] _do_reload view={view_name} id={self._view_id[:8] if self._view_id else None} meta={meta}", flush=True)
        DEBUG_GUI and print(f"[DO-RELOAD] view={self._view_id[:8] if self._view_id else None} meta={meta}")
        if view is None:
            if is_sys and DEBUG_GUI:
                print(f"[SYS-CUBE] _do_reload: view is None, returning early", flush=True)
            return

        # F5c: cache view metadata for paintEvent (avoids paint-path get_view)
        self._cached_view_meta = {
            "view_id": self._view_id,  # F6b: enables current_view_meta() staleness guard
            "row_dim_ids": list(view.get("row_dim_ids", []) or []),
            "col_dim_ids": list(view.get("col_dim_ids", []) or []),
            "page_dim_ids": list(view.get("page_dim_ids", []) or []),
            "item_formats": {k: _cell_format_from_dict(v) for k, v in view.get("item_formats", {}).items()},
            "group_formats": {k: _cell_format_from_dict(v) for k, v in view.get("group_formats", {}).items()},
            "cell_formats": {k: _cell_format_from_dict(v) for k, v in view.get("cell_formats", {}).items()},
            "col_widths": dict(view.get("col_widths", {})),
            "row_header_widths": dict(view.get("row_header_widths", {})),
        }

        saved_sel_mode = self._sel_mode
        saved_sel_indices = set(self._sel_indices)
        saved_anchor_row = self._anchor_row
        saved_anchor_col = self._anchor_col
        saved_debug_tooltips = getattr(self, '_debug_tooltips_enabled', False)
        
        # Save leaf item IDs and full keys for rows/columns to restore selection after rebuild
        saved_row_item_ids: set[str] = set()
        saved_col_item_ids: set[str] = set()
        saved_cell_items: set[tuple[str, str]] = set()  # (row_item_id, col_item_id)
        saved_cell_keys: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()  # (row_key, col_key)
        saved_row_keys: set[tuple[str, ...]] = set()
        saved_col_keys: set[tuple[str, ...]] = set()
        
        if saved_sel_mode == "row":
            for idx in saved_sel_indices:
                if isinstance(idx, int) and 0 <= idx < len(self._rows):
                    row = self._rows[idx]
                    if row.get("is_leaf", False):
                        iid = row.get("item_id")
                        if isinstance(iid, str):
                            saved_row_item_ids.add(iid)
                    if 0 <= idx < len(self._row_keys):
                        key = self._row_keys[idx]
                        if isinstance(key, (tuple, list)):
                            saved_row_keys.add(tuple(key))
        elif saved_sel_mode == "col":
            for idx in saved_sel_indices:
                if isinstance(idx, int) and 0 <= idx < len(self._cols):
                    col = self._cols[idx]
                    if col.get("is_leaf", False):
                        iid = col.get("item_id")
                        if isinstance(iid, str):
                            saved_col_item_ids.add(iid)
                    if 0 <= idx < len(self._col_keys):
                        key = self._col_keys[idx]
                        if isinstance(key, (tuple, list)):
                            saved_col_keys.add(tuple(key))
        elif saved_sel_mode == "cell":
            for idx in saved_sel_indices:
                if isinstance(idx, tuple) and len(idx) == 2:
                    r, c = idx
                    if isinstance(r, int) and isinstance(c, int):
                        if 0 <= r < len(self._rows) and 0 <= c < len(self._cols):
                            row = self._rows[r]
                            col = self._cols[c]
                            if row.get("is_leaf", False) and col.get("is_leaf", False):
                                row_iid = row.get("item_id")
                                col_iid = col.get("item_id")
                                if isinstance(row_iid, str) and isinstance(col_iid, str):
                                    saved_cell_items.add((row_iid, col_iid))
                                if 0 <= r < len(self._row_keys) and 0 <= c < len(self._col_keys):
                                    saved_cell_keys.add((tuple(self._row_keys[r]), tuple(self._col_keys[c])))
        
        # Load saved column widths and row header widths from view.
        # Only overwrite the width map that is not currently being resized.
        # ARCHITECTURE_DEBT: This mixes presentation-state (drag in progress) into
        # data-refresh logic. Longer-term, drag-local presentation state should be
        # isolated from model reload state (e.g., via a PresentationStateGuard).
        # Normalize keys to int because JSON persistence returns string keys,
        # but the grid uses int keys throughout (_resize_col, _geometry.col_width).
        engine_col_widths = {int(k): v for k, v in view.get("col_widths", {}).items()}
        engine_row_widths = {int(k): v for k, v in view.get("row_header_widths", {}).items()}
        if self._resize_col is None:
            self._col_widths = engine_col_widths
        if self._resize_row_level is None:
            self._row_header_widths = engine_row_widths
        
        raw_row_keys = self._grid_read_model.row_keys(self._view_id)
        raw_col_keys = self._grid_read_model.col_keys(self._view_id)

        # Remember pre-rebuild keys so we can do targeted tile invalidation
        # after structural changes (insertion, deletion, reorder).
        old_row_keys = list(self._row_keys)
        old_col_keys = list(self._col_keys)

        # Build row display
        self._rows = []
        self._row_keys = []
        self._build_rows(view, raw_row_keys)
        self._geometry._rebuild_leaf_index_cache()

        # Build column display + bands
        self._cols = []
        self._col_keys = []
        self._col_bands = []
        self._col_header_levels = 1
        self._col_band_levels = 0
        self._build_cols(view, raw_col_keys)

        if is_sys and DEBUG_GUI:
            print(
                f"[SYS-CUBE] _do_reload built: rows={len(self._rows)} cols={len(self._cols)} "
                f"row_keys={len(self._row_keys)} col_keys={len(self._col_keys)} "
                f"raw_row_keys={len(raw_row_keys)} raw_col_keys={len(raw_col_keys)}",
                flush=True,
            )
            if self._row_keys:
                print(f"[SYS-CUBE]   sample row_keys: {self._row_keys[:3]}", flush=True)
            if self._col_keys:
                print(f"[SYS-CUBE]   sample col_keys: {self._col_keys[:3]}", flush=True)
            if not self._row_keys or not self._col_keys:
                print(f"[SYS-CUBE]   *** EMPTY KEYS after build ***", flush=True)

        # Structural change (insert/delete/reorder): snapshot data and tile
        # images to the right of the first change are stale.  For a pure
        # width change the snapshot data stays valid and only the tile
        # images containing the resized columns were already invalidated
        # during drag, so nothing extra to clear here.
        has_structural_change = (
            old_row_keys != self._row_keys or old_col_keys != self._col_keys
        )
        if has_structural_change:
            self._tile_cache.clear()
            self._invalidate_tile_images_for_structural_change(old_col_keys, old_row_keys)

        self._sel_row = min(self._sel_row, max(0, len(self._rows) - 1))
        self._sel_col = min(self._sel_col, max(0, len(self._cols) - 1))

        # Build item_id -> index mappings for the new rows/cols
        row_item_to_idx: dict[str, int] = {}
        for idx, row in enumerate(self._rows):
            if row.get("is_leaf", False):
                iid = row.get("item_id")
                if isinstance(iid, str):
                    row_item_to_idx[iid] = idx
        
        col_item_to_idx: dict[str, int] = {}
        for idx, col in enumerate(self._cols):
            if col.get("is_leaf", False):
                iid = col.get("item_id")
                if isinstance(iid, str):
                    col_item_to_idx[iid] = idx

        if saved_sel_mode == "row":
            # Restore by key matching first (robust across layout changes)
            new_indices = set()
            for idx, key in enumerate(self._row_keys):
                check_key = tuple(key) if isinstance(key, list) else key
                if check_key in saved_row_keys:
                    new_indices.add(idx)
            # Fallback: verify old indices still point to same items
            if not new_indices:
                for idx in saved_sel_indices:
                    if isinstance(idx, int) and 0 <= idx < len(self._rows):
                        iid = self._rows[idx].get("item_id")
                        if isinstance(iid, str) and iid in saved_row_item_ids:
                            new_indices.add(idx)
            # Final fallback to item_id-based search
            if not new_indices:
                for iid in saved_row_item_ids:
                    if iid in row_item_to_idx:
                        new_indices.add(row_item_to_idx[iid])
            self._sel_mode = "row"
            self._sel_indices = new_indices
            if new_indices:
                self._sel_row = min(new_indices)
        elif saved_sel_mode == "col":
            # Restore by key matching first (robust across layout changes)
            new_indices = set()
            for idx, key in enumerate(self._col_keys):
                check_key = tuple(key) if isinstance(key, list) else key
                if check_key in saved_col_keys:
                    new_indices.add(idx)
            # Fallback: verify old indices still point to same items
            if not new_indices:
                for idx in saved_sel_indices:
                    if isinstance(idx, int) and 0 <= idx < len(self._cols):
                        iid = self._cols[idx].get("item_id")
                        if isinstance(iid, str) and iid in saved_col_item_ids:
                            new_indices.add(idx)
            # Final fallback to item_id-based search
            if not new_indices:
                for iid in saved_col_item_ids:
                    if iid in col_item_to_idx:
                        new_indices.add(col_item_to_idx[iid])
            self._sel_mode = "col"
            self._sel_indices = new_indices
            if new_indices:
                self._sel_col = min(new_indices)
        elif saved_sel_mode == "cell":
            # Restore cell selection by full (row_key, col_key) first; leaf item
            # IDs alone are not unique in stacked dimension views.
            valid_cells: set[tuple[int, int]] = set()
            row_key_to_idx: dict[tuple[str, ...], int] = {}
            for idx, key in enumerate(self._row_keys):
                if self._rows[idx].get("is_leaf", False):
                    row_key_to_idx[tuple(key) if isinstance(key, list) else key] = idx
            col_key_to_idx: dict[tuple[str, ...], int] = {}
            for idx, key in enumerate(self._col_keys):
                if self._cols[idx].get("is_leaf", False):
                    col_key_to_idx[tuple(key) if isinstance(key, list) else key] = idx
            for row_key, col_key in saved_cell_keys:
                if row_key in row_key_to_idx and col_key in col_key_to_idx:
                    valid_cells.add((row_key_to_idx[row_key], col_key_to_idx[col_key]))
            # Fallback to (row_item_id, col_item_id) pairs if no full key matches
            if not valid_cells:
                for row_iid, col_iid in saved_cell_items:
                    if row_iid in row_item_to_idx and col_iid in col_item_to_idx:
                        valid_cells.add((row_item_to_idx[row_iid], col_item_to_idx[col_iid]))
            # Fallback to index-based only if key-based restore found nothing
            if not valid_cells:
                for idx in saved_sel_indices:
                    if not isinstance(idx, tuple) or len(idx) != 2:
                        continue
                    r, c = idx
                    if not isinstance(r, int) or not isinstance(c, int):
                        continue
                    if 0 <= r < len(self._rows) and 0 <= c < len(self._cols):
                        valid_cells.add((r, c))
            DEBUG_GUI and print(f"DEBUG SET CELL: line {__import__('inspect').currentframe().f_lineno} prev={self._sel_mode}"); self._sel_mode = "cell"
            self._sel_indices = valid_cells
            # Keep the active/focus cell consistent with the restored selection
            # so paint logic does not highlight a different cell.
            if len(valid_cells) == 1:
                self._sel_row, self._sel_col = next(iter(valid_cells))
            elif len(valid_cells) > 1:
                # Multi-cell selection: keep the active cell inside the restored set
                # if the old active cell is no longer valid.
                if (self._sel_row, self._sel_col) not in valid_cells:
                    self._sel_row, self._sel_col = min(valid_cells)

        self._anchor_row = min(saved_anchor_row, max(0, len(self._rows) - 1)) if self._rows else 0
        self._anchor_col = min(saved_anchor_col, max(0, len(self._cols) - 1)) if self._cols else 0

        # During explicit insert preservation we keep restored row/col mode untouched.
        if not self._preserving_selection and self._sel_mode == "cell":
            self._clamp_selection_to_leaf()
        
        # Save scroll position before _update_scrollbars which can reset it
        saved_h_scroll = self.horizontalScrollBar().value()
        saved_v_scroll = self.verticalScrollBar().value()
        
        self._update_scrollbars()
        
        # Restore scroll position after scrollbar update using deferred execution
        # to ensure it happens after all pending Qt events are processed.
        # Skip when _preserve_scroll is True so callers (rebuild_tabs, dropEvent,
        # header edit) own the final scroll position without timer races.
        def _restore_scroll():
            if not isValid(self):
                return
            if self._preserve_scroll:
                return
            self.horizontalScrollBar().setValue(saved_h_scroll)
            self.verticalScrollBar().setValue(saved_v_scroll)
        QtCore.QTimer.singleShot(0, _restore_scroll)
        
        self.viewport().update()
        # Trigger immediate tile fetch so first paint has data (non-blocking).
        DEBUG_GUI and print(f"[DO-RELOAD] scheduling tile fetch view={self._view_id[:8] if self._view_id else None}")
        QtCore.QTimer.singleShot(0, self._start_tile_fetch)

        # Debug: print grid layout
        if DEBUG_GUI:
            self._debug_print_layout()

        # Restore debug tooltip state
        self._debug_tooltips_enabled = saved_debug_tooltips
        if self._debug_tooltips_enabled:
            self.setMouseTracking(True)

    # ------------------------------------------------------------
    # Debug helpers
    # ------------------------------------------------------------
    
    def _debug_print_layout(self) -> None:
        """Print grid layout to terminal for debugging."""
        if not DEBUG_GUI:
            return
        print("\n" + "="*60)
        # Determine if axes are stacked (multi-dimension) or unstacked (single dimension)
        row_stacked = False
        col_stacked = False
        view_meta = self._cached_view_meta or {}
        row_dim_ids = list(view_meta.get("row_dim_ids", []) or [])
        col_dim_ids = list(view_meta.get("col_dim_ids", []) or [])
        row_stacked = len(row_dim_ids) > 1
        col_stacked = len(col_dim_ids) > 1
        
        # Get max indices for unstacked logic
        max_row_idx = len(self._rows) - 1 if self._rows else 0
        max_col_idx = len(self._cols) - 1 if self._cols else 0
        
        print(f"GRID LAYOUT: {len(self._rows)} rows x {len(self._cols)} cols")
        print(f"ROW MODE: {'stacked' if row_stacked else 'unstacked'}")
        print(f"COL MODE: {'stacked' if col_stacked else 'unstacked'}")
        print("-"*60)
        
        if self._rows:
            print("ROWS:")
            for i, r in enumerate(self._rows[:10]):  # Limit to first 10
                labels = r.get("labels", [])
                paths = r.get("label_paths", [])
                print(f"  {i}: labels={labels}, paths={paths}")
            if len(self._rows) > 10:
                print(f"  ... ({len(self._rows) - 10} more rows)")
        
        if self._cols:
            print("\nCOLUMNS:")
            for i, c in enumerate(self._cols[:10]):  # Limit to first 10
                labels = c.get("labels", [])
                paths = c.get("label_paths", [])
                print(f"  {i}: labels={labels}, paths={paths}")
            if len(self._cols) > 10:
                print(f"  ... ({len(self._cols) - 10} more cols)")
        
        if self._col_bands:
            print("\nCOLUMN BANDS:")
            for b in self._col_bands[:15]:  # Limit to first 15
                label = b.get("label", "")
                path = b.get("path")
                c0, c1 = b.get("c0", 0), b.get("c1", 0)
                path_str = str(path) if path else "None"
                c0, c1 = b.get("c0", 0), b.get("c1", 0)
                # Determine if this band would have shading
                # Unstacked: shade if label is not empty
                is_empty = not label and path is None
                is_group = False
                if not is_empty:
                    if col_stacked:
                        # Stacked: shade if path has more than one element
                        if isinstance(path, tuple) and len(path) > 1:
                            is_group = True
                    else:
                        # Unstacked: shade if label not empty
                        if label:
                            is_group = True
                shaded = "Y" if is_group else "N"
                print(f"  level={b.get('level')} c0={c0} c1={c1} label={label!r} path={path_str} shaded={shaded}")
            if len(self._col_bands) > 15:
                print(f"  ... ({len(self._col_bands) - 15} more bands)")
        
        if self._row_bands:
            print("\nROW BANDS:")
            for b in self._row_bands[:15]:  # Limit to first 15
                label = b.get("label", "")
                path = b.get("path")
                r0, r1 = b.get("r0", 0), b.get("r1", 0)
                path_str = str(path) if path else "None"
                r0, r1 = b.get("r0", 0), b.get("r1", 0)
                # Determine if this band would have shading
                # Unstacked: shade if label is not empty
                is_empty = not label and path is None
                is_group = False
                if not is_empty:
                    if row_stacked:
                        # Stacked: shade if path has more than one element
                        if isinstance(path, tuple) and len(path) > 1:
                            is_group = True
                    else:
                        # Unstacked: shade if label not empty
                        if label:
                            is_group = True
                shaded = "Y" if is_group else "N"
                print(f"  level={b.get('level')} r0={r0} r1={r1} label={label!r} path={path_str} shaded={shaded}")
            if len(self._row_bands) > 15:
                print(f"  ... ({len(self._row_bands) - 15} more bands)")
        
        print("="*60 + "\n", flush=True)

    # ------------------------------------------------------------
    # Debug Tooltip System
    # ------------------------------------------------------------

    def set_debug_tooltips_enabled(self, enabled: bool) -> None:
        """Enable or disable debug tooltips for all grid elements."""
        self._debug_tooltips_enabled = enabled
        if enabled:
            self.setMouseTracking(True)
        print(f"[MatrixGrid] Debug tooltips {'enabled' if enabled else 'disabled'}")

    def _get_debug_tooltip(self, pos: QtCore.QPoint) -> str | None:
        """Generate debug tooltip for the element at the given position."""
        if not getattr(self, '_debug_tooltips_enabled', False):
            return None
        
        # Use header_hit to determine what element is at this position
        hit = self._header_edit.header_hit(pos)
        if hit is None:
            # Not a header element - check if it's a cell
            return self._tooltips.get_cell_debug_tooltip(pos)
        
        kind, payload = hit
        
        # Generate tooltip based on element type
        if kind == "row_leaf":
            return self._tooltips.get_row_leaf_tooltip(payload)
        elif kind == "row_group":
            return self._tooltips.get_row_group_tooltip(payload)
        elif kind == "col_leaf":
            return self._tooltips.get_col_leaf_tooltip(payload)
        elif kind == "col_group":
            return self._tooltips.get_col_group_tooltip(payload)
        elif kind == "row_bg":
            return "Row Background Area"
        elif kind == "col_bg":
            return "Column Background Area"
        
        return None

    # ------------------------------------------------------------
    # Outline helpers (shared by context menus + drag/drop)
    # ------------------------------------------------------------

    def _axis_dim_id(self, axis: str) -> str | None:
        """Get dimension ID for the given axis."""
        return self._dimensions.axis_dim_id(axis)

    def _normalized_outline(self, did: str, outline: list[OutlineNode]) -> list[OutlineNode]:
        """Normalize outline structure - ensure all nodes are OutlineNode instances."""
        return self._dimensions.normalized_outline(did, outline)

    def _axis_outline(self, axis: str) -> list[OutlineNode]:
        """Get the outline for a given axis."""
        return self._dimensions.axis_outline(axis)

    def _ensure_outline_axis(self, axis: str) -> bool:
        """Ensure outline exists for the given axis."""
        return self._dimensions.ensure_outline_axis(axis)

    def _outline_root(self, axis: str) -> list[OutlineNode]:
        """Get root outline for axis (same as _axis_outline but with normalization)."""
        return self._dimensions.outline_root(axis)

    def _set_outline_root(self, axis: str, root: list[OutlineNode]) -> None:
        """Set root outline for axis."""
        self._dimensions.set_outline_root(axis, root)

    def _prune_empty_groups(self, nodes: list[OutlineNode]) -> list[OutlineNode]:
        """Recursively remove empty groups (nodes with no children and no item_id)."""
        return self._dimensions.prune_empty_groups(nodes)

    def _remove_leaf_from_outline(self, nodes: list[OutlineNode], item_id: str) -> tuple[list[OutlineNode], OutlineNode | None]:
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
                    new.append(OutlineNode(label=n.label, item_id=n.item_id, children=kids, node_id=n.node_id, display_edge_kind=n.display_edge_kind, is_aggregate=n.is_aggregate))
                else:
                    new.append(n)
            return new

        return _walk(nodes), removed

    def _get_node_at_path(self, nodes: list[OutlineNode], path: tuple[int, ...]) -> OutlineNode | None:
        arr = nodes
        cur: OutlineNode | None = None
        for i in path:
            if not (0 <= i < len(arr)):
                return None
            cur = arr[i]
            arr = list(cur.children)
        return cur

    def _set_node_children_at_path(self, nodes: list[OutlineNode], path: tuple[int, ...], children: list[OutlineNode]) -> list[OutlineNode]:
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
                    out_nodes.append(OutlineNode(label=n.label, item_id=n.item_id, children=children, node_id=n.node_id, display_edge_kind=n.display_edge_kind, is_aggregate=n.is_aggregate))
                else:
                    new_kids = _rebuild(list(n.children), p[1:])
                    out_nodes.append(OutlineNode(label=n.label, item_id=n.item_id, children=new_kids, node_id=n.node_id, display_edge_kind=n.display_edge_kind, is_aggregate=n.is_aggregate))
            return out_nodes

        return _rebuild(nodes, path)

    def _leaf_node_for_item(self, axis: str, item_id: str) -> OutlineNode | None:
        did = self._axis_dim_id(axis)
        if did is None:
            return None
        dim = self._workspace_read_model.get_dimension(did)
        if dim is None:
            return None
        it = next((it for it in dim.get("items", []) if it.get("id") == item_id), None)
        if it is None:
            return None
        return OutlineNode(label=it["name"], item_id=it["id"], children=[])

    def _insert_group_at_path(
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
            root[i] = OutlineNode(label=n.label, item_id=n.item_id, children=children, node_id=n.node_id, display_edge_kind=n.display_edge_kind, is_aggregate=n.is_aggregate)
            return True
        return False

    def _remove_any_node_from_outline(
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
        new_nodes[i] = OutlineNode(label=n.label, item_id=n.item_id, children=new_children, node_id=n.node_id, display_edge_kind=n.display_edge_kind, is_aggregate=n.is_aggregate)
        return new_nodes, removed

    def _insert_node_at_index(
        self,
        nodes: list[OutlineNode],
        parent_path: tuple[int, ...],
        index: int,
        node: OutlineNode,
    ) -> list[OutlineNode]:
        if not parent_path:
            new = list(nodes)
            new.insert(max(0, min(len(new), index)), node)
            return new
        i = parent_path[0]
        if not (0 <= i < len(nodes)):
            new = list(nodes)
            new.append(node)
            return new
        n = nodes[i]
        new_children = self._insert_node_at_index(list(n.children), parent_path[1:], index, node)
        new_nodes = list(nodes)
        new_nodes[i] = OutlineNode(label=n.label, item_id=n.item_id, children=new_children, node_id=n.node_id, display_edge_kind=n.display_edge_kind, is_aggregate=n.is_aggregate)
        return new_nodes

    def _path_for_item_id(self, axis: str, item_id: str) -> tuple[int, ...] | None:
        outline = self._axis_outline(axis)

        def _walk(nodes: list[OutlineNode], path: tuple[int, ...]) -> tuple[int, ...] | None:
            for i, n in enumerate(nodes):
                cur = path + (i,)
                if n.item_id == item_id:
                    return cur
                if n.children:
                    res = _walk(n.children, cur)
                    if res is not None:
                        return res
            return None

        return _walk(outline, tuple())

    def _key_for_item_id(self, axis: str, item_id: str) -> tuple[str, ...] | None:
        """Return the key tuple containing the given item ID for the specified axis."""
        keys = self._col_keys if axis == "col" else self._row_keys
        entries = self._cols if axis == "col" else self._rows
        for idx, entry in enumerate(entries):
            iid = entry.get("item_id")
            if iid == item_id and 0 <= idx < len(keys):
                key = keys[idx]
                if isinstance(key, (tuple, list)):
                    return tuple(key)
        return None

    def _reorder_in_outline(
        self, axis: str, src_path: tuple[int, ...], dest_path: tuple[int, ...], insert_after: bool
    ) -> None:
        """Single-item drag-and-drop reorder/move.

        Phase 6: single Engine.move_nodes call handles all cases
        (same-parent reorder, cross-parent move, root-to-group, group-to-root).
        """
        root = self._outline_root(axis)
        src_node = self._get_node_at_path(root, src_path)
        if src_node is None:
            return
        dest_node = self._get_node_at_path(root, dest_path)
        if dest_node is None:
            return

        dim_id = self._axis_dim_id(axis)
        if not dim_id:
            return

        src_node_id = getattr(src_node, 'node_id', None)
        dest_node_id = getattr(dest_node, 'node_id', None)
        if not src_node_id or not dest_node_id:
            return  # Phase 4: graph is canonical, outline must be synced

        # Determine destination parent
        dest_is_root = len(dest_path) == 1
        dest_parent_id = None
        dest_parent_node = None
        if not dest_is_root:
            dest_parent_path = dest_path[:-1]
            dest_parent_node = self._get_node_at_path(root, dest_parent_path)
            if dest_parent_node:
                dest_parent_id = getattr(dest_parent_node, 'node_id', None)

        # Detect boundary drop: after last child or before first child of a group
        is_first_in_group = False
        is_last_in_group = False
        if dest_parent_node:
            dest_idx = dest_path[-1]
            is_first_in_group = dest_idx == 0
            is_last_in_group = dest_idx == len(dest_parent_node.children) - 1

        position = "after" if insert_after else "before"
        if insert_after and is_last_in_group and not dest_is_root:
            # Place outside group after the group
            grandparent_path = dest_path[:-2] if len(dest_path) > 2 else ()
            grandparent_node = self._get_node_at_path(root, grandparent_path) if grandparent_path else None
            dest_parent_id = getattr(grandparent_node, 'node_id', None) if grandparent_node else None
            anchor_node_id = getattr(dest_parent_node, 'node_id', None)
        elif not insert_after and is_first_in_group and not dest_is_root:
            # Place outside group before the group
            grandparent_path = dest_path[:-2] if len(dest_path) > 2 else ()
            grandparent_node = self._get_node_at_path(root, grandparent_path) if grandparent_path else None
            dest_parent_id = getattr(grandparent_node, 'node_id', None) if grandparent_node else None
            anchor_node_id = getattr(dest_parent_node, 'node_id', None)
        else:
            anchor_node_id = dest_node_id

        self.execute_command(
            "move_nodes",
            dim_id=dim_id,
            node_ids=[src_node_id],
            parent_node_id=dest_parent_id,
            anchor_node_id=anchor_node_id,
            position=position,
        )

        # Prune empty groups after reorder
        dim = self._workspace_read_model.get_dimension(dim_id)
        if dim:
            root = self._outline_root(axis)
            def _find_empty_groups(nodes):
                for node in nodes:
                    if node.item_id is None and not node.children and getattr(node, 'node_id', None):
                        yield node.node_id
                    if node.children:
                        yield from _find_empty_groups(node.children)

            while True:
                root = self._outline_root(axis)
                empty = list(_find_empty_groups(root))
                if not empty:
                    break
                for gid in empty:
                    self.execute_command(
                        "delete_group_node",
                        dim_id=dim_id,
                        node_id=gid,
                        promote_children="to_parent",
                    )

    def _reorder_flat_dimension_for_dim(
        self, dim_id: str, src_item_id: str, dest_item_id: str, insert_after: bool
    ) -> None:
        """Reorder items inside a specific Dimension using effective display order."""
        if not isinstance(dim_id, str):
            return

        # Get effective display order (merges graph + flat state)
        item_ids = self._workspace_read_model.effective_order(dim_id)
        if not item_ids:
            return

        # Do not allow reordering for sequential dimensions.
        dim = self._workspace_read_model.get_dimension(dim_id)
        if dim and dim.get("dim_type") == "seq":
            return

        # Find source and destination indices
        try:
            src_idx = item_ids.index(src_item_id)
            dest_idx = item_ids.index(dest_item_id)
        except ValueError:
            return

        if src_idx == dest_idx:
            return

        # Remove source item
        item_ids.pop(src_idx)

        # Adjust destination index if needed
        if src_idx < dest_idx:
            dest_idx -= 1

        # Insert at new position
        insert_idx = dest_idx + (1 if insert_after else 0)
        item_ids.insert(insert_idx, src_item_id)

        # Route through command spine
        self._session.execute("set_dimension_item_order", dim_id=dim_id, item_ids=item_ids)

        # Preserve selection across reload: save axis keys before reorder
        saved_sel_mode = self._sel_mode
        saved_sel_keys: set[tuple[str, ...]] = set()
        saved_sel_row = self._sel_row
        saved_sel_col = self._sel_col
        DEBUG_GUI and print(f"DEBUG _reorder_flat: BEFORE _sel_mode={saved_sel_mode} _sel_indices={self._sel_indices} cols={len(self._cols)} rows={len(self._rows)}")
        for idx in self._sel_indices:
            if isinstance(idx, int):
                if saved_sel_mode == "col" and 0 <= idx < len(self._cols):
                    if idx < len(self._col_keys):
                        key = self._col_keys[idx]
                        DEBUG_GUI and print(f"DEBUG _reorder_flat: col idx={idx} key={key} is_tuple={isinstance(key, tuple)}")
                        if isinstance(key, (tuple, list)):
                            saved_sel_keys.add(tuple(key))
                elif saved_sel_mode == "row" and 0 <= idx < len(self._rows):
                    if idx < len(self._row_keys):
                        key = self._row_keys[idx]
                        if isinstance(key, (tuple, list)):
                            saved_sel_keys.add(tuple(key))
        DEBUG_GUI and print(f"DEBUG _reorder_flat: saved keys={saved_sel_keys}")
        saved_anchor_row = self._anchor_row
        saved_anchor_col = self._anchor_col

        self.reload()

        # Restore selection on this grid (for tests that call directly)
        # When called via UI, rebuild_tabs will overwrite this with correct new indices
        DEBUG_GUI and print(f"DEBUG _reorder_flat: RESTORE saved_mode={saved_sel_mode} saved_keys={saved_sel_keys}")
        if saved_sel_mode == "col":
            new_indices = set()
            for idx, key in enumerate(self._col_keys):
                check_key = tuple(key) if isinstance(key, list) else key
                if check_key in saved_sel_keys:
                    new_indices.add(idx)
            self._sel_mode = "col"
            self._sel_indices = new_indices
            if new_indices:
                self._sel_col = min(new_indices)
        elif saved_sel_mode == "row":
            new_indices = set()
            for idx, key in enumerate(self._row_keys):
                check_key = tuple(key) if isinstance(key, list) else key
                if check_key in saved_sel_keys:
                    new_indices.add(idx)
            self._sel_mode = "row"
            self._sel_indices = new_indices
            if new_indices:
                self._sel_row = min(new_indices)

        self._anchor_row = min(saved_anchor_row, max(0, len(self._rows) - 1)) if self._rows else 0
        self._anchor_col = min(saved_anchor_col, max(0, len(self._cols) - 1)) if self._cols else 0
        self.selection_changed.emit()

        # Emit outline_changed AFTER reload + restore so that any synchronous
        # rebuild_tabs observer sees the already-corrected selection, not stale
        # indices from before the reorder.
        self.outline_changed.emit()

    def _reorder_flat_dimension(self, axis: str, src_item_id: str, dest_item_id: str, insert_after: bool) -> None:
        """Reorder items in the innermost dimension for a given axis (no outline)."""
        dim_id = self._axis_dim_id(axis)
        if dim_id is None:
            return

        # Guard against transient mode flips during drag/drop: ensure flat reorder
        # starts from axis selection so restore logic cannot collapse to cell mode.
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

        self._reorder_flat_dimension_for_dim(dim_id, src_item_id, dest_item_id, insert_after)
    
    def _reorder_multiple_in_outline(self, axis: str, src_item_ids: list[str], dest_path: tuple[int, ...], insert_after: bool) -> None:
        """Reorder multiple selected items as a group within an outline structure.

        Phase 6: delegates to Engine.move_nodes; GUI never touches graph primitives.
        """
        root = self._outline_root(axis)
        if not root:
            return

        dim_id = self._axis_dim_id(axis)
        if not dim_id:
            return

        # Collect node_ids for selected items from the outline
        node_id_map = {}
        for item_id in src_item_ids:
            for row in (self._rows if axis == "row" else self._cols):
                if row.get("item_id") == item_id:
                    path = row.get("path")
                    if path:
                        node = self._get_node_at_path(root, path)
                        if node and getattr(node, 'node_id', None):
                            node_id_map[item_id] = node.node_id
                    break

        dest_node = self._get_node_at_path(root, dest_path) if dest_path else None
        dest_id_ok = getattr(dest_node, 'node_id', None) if dest_node else False

        if len(node_id_map) != len(src_item_ids) or not dest_id_ok:
            return  # Phase 4: graph is canonical, outline must be synced

        # Determine destination parent and anchor
        dest_is_root = len(dest_path) == 1
        dest_parent_id = None
        dest_parent_node = None
        if not dest_is_root:
            dest_parent_path = dest_path[:-1]
            dest_parent_node = self._get_node_at_path(root, dest_parent_path)
            if dest_parent_node and getattr(dest_parent_node, 'node_id', None):
                dest_parent_id = dest_parent_node.node_id

        # Detect boundary drop: after last child or before first child of a group
        is_first_in_group = False
        is_last_in_group = False
        if dest_parent_node:
            dest_idx = dest_path[-1]
            is_first_in_group = dest_idx == 0
            is_last_in_group = dest_idx == len(dest_parent_node.children) - 1

        position = "after" if insert_after else "before"
        if insert_after and is_last_in_group and not dest_is_root:
            # Place outside group after the group
            grandparent_path = dest_path[:-2] if len(dest_path) > 2 else ()
            grandparent_node = self._get_node_at_path(root, grandparent_path) if grandparent_path else None
            dest_parent_id = getattr(grandparent_node, 'node_id', None) if grandparent_node else None
            anchor_node_id = getattr(dest_parent_node, 'node_id', None)
        elif not insert_after and is_first_in_group and not dest_is_root:
            # Place outside group before the group
            grandparent_path = dest_path[:-2] if len(dest_path) > 2 else ()
            grandparent_node = self._get_node_at_path(root, grandparent_path) if grandparent_path else None
            dest_parent_id = getattr(grandparent_node, 'node_id', None) if grandparent_node else None
            anchor_node_id = getattr(dest_parent_node, 'node_id', None)
        else:
            anchor_node_id = dest_node.node_id

        detached = [node_id_map[item_id] for item_id in src_item_ids if node_id_map.get(item_id)]
        if not detached:
            return

        self.execute_command(
            "move_nodes",
            dim_id=dim_id,
            node_ids=detached,
            parent_node_id=dest_parent_id,
            anchor_node_id=anchor_node_id,
            position=position,
        )

        # Prune empty groups after multi-item reorder
        dim = self._workspace_read_model.get_dimension(dim_id)
        if dim:
            root = self._outline_root(axis)
            def _find_empty_groups(nodes):
                for node in nodes:
                    if node.item_id is None and not node.children and getattr(node, 'node_id', None):
                        yield node.node_id
                    if node.children:
                        yield from _find_empty_groups(node.children)

            while True:
                root = self._outline_root(axis)
                empty = list(_find_empty_groups(root))
                if not empty:
                    break
                for gid in empty:
                    self.execute_command(
                        "delete_group_node",
                        dim_id=dim_id,
                        node_id=gid,
                        promote_children="to_parent",
                    )

    def _reorder_multiple_flat_items(self, axis: str, src_item_ids: list[str], dest_item_id: str, insert_after: bool) -> None:
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

        # Get effective display order (merges graph + flat state)
        item_ids = self._workspace_read_model.effective_order(dim_id)
        if not item_ids:
            return

        # Do not allow reordering for sequential dimensions.
        dim = self._workspace_read_model.get_dimension(dim_id)
        if dim and dim.get("dim_type") == "seq":
            return

        # Find destination index
        try:
            dest_idx = item_ids.index(dest_item_id)
        except ValueError:
            return

        # Extract selected items in their current order
        selected = []
        remaining = []
        for iid in item_ids:
            if iid in src_item_ids:
                selected.append(iid)
            else:
                remaining.append(iid)

        if not selected:
            return

        # Find new destination index in remaining items
        try:
            new_dest_idx = remaining.index(dest_item_id)
        except ValueError:
            return

        # Insert selected items at new position
        insert_idx = new_dest_idx + (1 if insert_after else 0)
        for i, iid in enumerate(selected):
            remaining.insert(insert_idx + i, iid)

        # Route through command spine
        self._session.execute("set_dimension_item_order", dim_id=dim_id, item_ids=remaining)

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
                        if isinstance(key, (tuple, list)):
                            saved_sel_keys.add(tuple(key))
                elif saved_sel_mode == "row" and 0 <= idx < len(self._rows):
                    if idx < len(self._row_keys):
                        key = self._row_keys[idx]
                        if isinstance(key, (tuple, list)):
                            saved_sel_keys.add(tuple(key))
        saved_anchor_row = self._anchor_row
        saved_anchor_col = self._anchor_col

        self.reload()

        # Restore selection mode and indices (adjusted for new positions)
        if saved_sel_mode == "col":
            new_indices = set()
            for idx, key in enumerate(self._col_keys):
                check_key = tuple(key) if isinstance(key, list) else key
                if check_key in saved_sel_keys:
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
                check_key = tuple(key) if isinstance(key, list) else key
                if check_key in saved_sel_keys:
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

        # Emit outline_changed AFTER reload + restore so that any synchronous
        # rebuild_tabs observer sees the already-corrected selection, not stale
        # indices from before the reorder.
        self.outline_changed.emit()

    def _selected_contiguous_leaf_indices(self, axis: str) -> list[int] | None:
        if self._sel_mode != axis:
            return None
        indices = sorted(idx for idx in self._sel_indices if isinstance(idx, int))
        if not indices:
            return None
        entries = self._rows if axis == "row" else self._cols
        for idx in indices:
            if not (0 <= idx < len(entries)):
                return None
            if not entries[idx].get("is_leaf", False):
                return None
        if indices != list(range(indices[0], indices[-1] + 1)):
            return None
        return indices

    def _random_unique_dimension_item_name(self, dim_id: str) -> str:
        dim = self._workspace_read_model.get_dimension(dim_id)
        existing_names = {str(it["name"]) for it in dim.get("items", [])} if dim else set()
        alphabet = string.ascii_lowercase + string.digits
        while True:
            name = "".join(random.choice(alphabet) for _ in range(5))
            if name not in existing_names:
                return name


    def _insert_before(self, axis: str) -> None:
        self._selection.insert_dimension_items_relative_to_selection(axis, insert_after=False)

    def _insert_after(self, axis: str) -> None:
        self._selection.insert_dimension_items_relative_to_selection(axis, insert_after=True)

    def _add_aggregate_item(self, axis: str, group_path: tuple[int, ...]) -> None:
        """Add an aggregate item to a group via AGGREG_OF edge."""
        dim_id = self._axis_dim_id(axis)
        if dim_id is None:
            return

        # Find the group node in the outline
        root = self._outline_root(axis)
        group_node = self._get_node_at_path(root, group_path)
        if group_node is None or group_node.item_id is not None:
            return  # Not a group

        group_node_id = group_node.node_id
        if group_node_id is None:
            # Group exists in outline but not in graph (e.g. created by outline-only
            # grouping). Ensure the full chain from root to target is synced first.
            parent_id = None
            for depth in range(1, len(group_path) + 1):
                path_segment = group_path[:depth]
                node = self._get_node_at_path(root, path_segment)
                if node and node.item_id is None:
                    nid = getattr(node, "node_id", None)
                    if nid is None:
                        children = [
                            {"label": c.label, "item_id": c.item_id}
                            for c in node.children
                        ]
                        result = self.execute_command(
                            "ensure_group_in_graph",
                            dim_id=dim_id,
                            label=node.label,
                            children=children,
                            parent_group_id=parent_id,
                        )
                        nid = result.data.get("group_node_id") if result.success else None
                    parent_id = nid
            group_node_id = parent_id

        # Prompt for name
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Add Aggregate Item", "Aggregate item name:", text="Total"
        )
        if not ok or not name.strip():
            return

        name = name.strip()

        # Phase 8: command dispatcher handles mutation
        result = self.execute_command(
            "create_aggregate_item",
            dim_id=dim_id,
            group_node_id=group_node_id,
            name=name,
        )
        if not result.success:
            QtWidgets.QMessageBox.warning(self, "Name Conflict", result.error or "Add failed")
            return

        self.outline_changed.emit()

    def _ungroup_item(self, axis: str, item_id: str) -> None:
        """Remove item from group and promote to root level."""
        if not self._ensure_outline_axis(axis):
            return

        dim_id = self._axis_dim_id(axis)
        if not dim_id:
            return

        # Phase 7: resolve item_id → node_id via command spine
        result = self.execute_command(
            "resolve_item_node_id", dim_id=dim_id, item_id=item_id
        )
        if not result.success:
            return
        node_id = result.data.get("node_id")
        if not node_id:
            return

        # Compute desired root insertion position from outline leaf order
        root = self._outline_root(axis)
        src_path = self._path_for_item_id(axis, item_id)

        # Track the parent group node so we can delete it if it becomes empty
        parent_group_node_id = None
        if src_path and len(src_path) > 1:
            parent_path = tuple(src_path[:-1])
            parent_node = self._get_node_at_path(root, parent_path)
            if parent_node is not None:
                parent_group_node_id = getattr(parent_node, 'node_id', None)

        def _count_leaves_before(nodes, target_path):
            count = 0
            current_path = []
            def walk(ns):
                nonlocal count
                for i, n in enumerate(ns):
                    current_path.append(i)
                    if len(current_path) == len(target_path):
                        if tuple(current_path) == target_path:
                            current_path.pop()
                            return True
                    if n.children:
                        if walk(n.children):
                            current_path.pop()
                            return True
                    else:
                        count += 1
                    current_path.pop()
                return False
            walk(nodes)
            return count

        leaves_before = _count_leaves_before(root, src_path) if src_path else len(root)

        # Determine anchor based on desired root position
        if leaves_before == 0:
            position = "first"
            anchor_id = None
        elif leaves_before >= len(root):
            position = "last"
            anchor_id = None
        else:
            position = "before"
            anchor_node = root[leaves_before] if leaves_before < len(root) else None
            anchor_id = getattr(anchor_node, 'node_id', None) if anchor_node else None
            if not anchor_id:
                position = "last"
                anchor_id = None

        self.execute_command(
            "move_nodes",
            dim_id=dim_id,
            node_ids=[node_id],
            parent_node_id=None,
            anchor_node_id=anchor_id,
            position=position,
        )

        # Delete the parent group if it is now empty (only child was the moved item)
        if parent_group_node_id:
            parent_node = self._get_node_at_path(root, tuple(src_path[:-1])) if src_path else None
            if parent_node is not None:
                remaining_children = [c for c in (parent_node.children or []) if c.item_id != item_id]
                if not remaining_children:
                    self.execute_command(
                        "delete_group_node",
                        dim_id=dim_id,
                        node_id=parent_group_node_id,
                        promote_children="to_parent",
                    )

    def _set_node_label_at_path(self, nodes: list[OutlineNode], path: tuple[int, ...], label: str) -> list[OutlineNode]:
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

    def _row_leaf_header_rect(self, row_idx: int) -> QtCore.QRect:
        """Calculate rect for row leaf header at given index."""
        return self._renderer.row_leaf_header_rect(row_idx)

    def _col_leaf_header_rect(self, col_idx: int) -> QtCore.QRect:
        """Calculate rect for column leaf header at given index."""
        return self._renderer.col_leaf_header_rect(col_idx)

    def _resolve_row_leaf_target(self, row_idx: int) -> tuple[str, str, str] | None:
        view = self._workspace_read_model.get_view(self._view_id)
        dim_ids = list(view.get("row_dim_ids", []) or []) if view else []
        if not dim_ids or not (0 <= row_idx < len(self._rows)):
            return None
        if not self._rows[row_idx].get("is_leaf", False):
            return None
        leaf_idx = self._leaf_row_index(row_idx)
        if not (0 <= leaf_idx < len(self._row_keys)):
            return None
        row_key = self._row_keys[leaf_idx]
        if not row_key or len(row_key) != len(dim_ids):
            return None
        dim_id = dim_ids[-1]
        item_id = row_key[-1]
        if not isinstance(item_id, str):
            return None
        dim = self._workspace_read_model.get_dimension(dim_id)
        if dim is None:
            return None
        item = next((it for it in dim.get("items", []) if it.get("id") == item_id), None)
        if item is None:
            return None
        return dim_id, item["id"], item["name"]

    def _resolve_col_leaf_target(self, col_idx: int) -> tuple[str, str, str] | None:
        view = self._workspace_read_model.get_view(self._view_id)
        dim_ids = list(view.get("col_dim_ids", []) or []) if view else []
        if not dim_ids or not (0 <= col_idx < len(self._cols)):
            return None
        col_key = self._col_keys[col_idx] if 0 <= col_idx < len(self._col_keys) else tuple()
        if not col_key or len(col_key) != len(dim_ids):
            return None
        dim_id = dim_ids[-1]
        item_id = col_key[-1]
        if not isinstance(item_id, str):
            return None
        dim = self._workspace_read_model.get_dimension(dim_id)
        if dim is None:
            return None
        item = next((it for it in dim.get("items", []) if it.get("id") == item_id), None)
        if item is None:
            return None
        return dim_id, item["id"], item["name"]

    def _start_header_leaf_edit(self, axis: str, index: int) -> bool:
        """Start editing a header leaf item."""
        return self._header_edit.start_header_leaf_edit(axis, index)

    def _start_header_leaf_edit_from_hit(self, hit: tuple[str, str | tuple[int, ...] | None]) -> bool:
        """Start editing a header leaf from a hit result."""
        return self._header_edit.start_header_leaf_edit_from_hit(hit)

    def _start_group_header_edit(self, axis: str, group_path: tuple[int, ...]) -> bool:
        """Start editing a group header."""
        return self._header_edit.start_group_header_edit(axis, group_path)

    def _get_parent_header(self, axis: str) -> tuple[str, tuple[int, ...] | int] | None:
        """Get parent group from current editing context."""
        return self._header_edit.get_parent_header(axis)

    def _should_navigate_on_arrow(self, key: QtCore.Qt.Key) -> bool:
        """Check if arrow key should trigger header navigation."""
        return self._header_edit.should_navigate_on_arrow(key)

    def _get_first_child_header(self, axis: str) -> tuple[str, tuple[int, ...] | int] | None:
        """Get first child from current group context."""
        return self._header_edit.get_first_child_header(axis)

    def _debug_edit_state(self, tag: str) -> None:
        """Print debug information about edit state."""
        self._header_edit.debug_edit_state(tag)

    def _commit_and_navigate_to(self, target: tuple[str, tuple[int, ...] | int]) -> None:
        """Navigate to target header after commit. Caller already committed changes."""
        self._header_edit.commit_and_navigate_to(target)

    def _set_editor_focus_enabled(self, enabled: bool) -> None:
        """Enable or disable editor focus."""
        self._header_edit.set_editor_focus_enabled(enabled)

    def _show_duplicate_name_warning(self, axis: str, new_name: str) -> None:
        """Show warning when duplicate name is detected."""
        self._header_edit.show_duplicate_name_warning(axis, new_name)

    @staticmethod
    def _sanitize_label_text(text: str) -> str:
        """Sanitize label text for use as a header name."""
        return HeaderEditHelper.sanitize_label_text(text)

    def _post_grid_key_event(self, source_event: QtGui.QKeyEvent) -> None:
        """Post a key event to the grid itself (for navigation)."""
        self._events.post_grid_key_event(source_event)

    def _send_editor_key_event(self, source_event: QtGui.QKeyEvent) -> None:
        """Forward a key event to the editor widget."""
        clone = QtGui.QKeyEvent(
            source_event.type(),
            source_event.key(),
            source_event.modifiers(),
            source_event.text(),
            source_event.isAutoRepeat(),
            source_event.count(),
        )
        self._suppress_editor_event_filter = True
        try:
            QtWidgets.QApplication.sendEvent(self._editor, clone)
        finally:
            self._suppress_editor_event_filter = False

    def _ensure_label_editor_visible_from_ctx(self, tag: str) -> bool:
        """Ensure label editor is visible from context."""
        return self._header_edit.ensure_label_editor_visible_from_ctx(tag)

    def _ensure_header_edit_ctx_from_editor(self) -> bool:
        """Ensure header edit context from editor position."""
        return self._header_edit.ensure_header_edit_ctx_from_editor()

    def _start_pending_header_edit(self) -> None:
        """Start a pending header edit from the pending queue."""
        self._header_edit.start_pending_header_edit()

    def _rename_header_hit(self, hit: tuple[str, str | tuple[int, ...] | None]) -> bool:
        """Rename a header element based on hit result."""
        return self._header_edit.rename_header_hit(hit)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        view = self._workspace_read_model.get_view(self._view_id)
        menu = QtWidgets.QMenu(self)

        header_h = self._m.col_header_h * max(1, self._col_header_levels)
        off = self._scroll_offset()
        x = event.pos().x() + off.x()
        y = event.pos().y() + off.y()

        def _axis_under_cursor() -> str:
            rhw = self._row_header_width()
            if y < header_h and x >= rhw:
                return "col"
            if x < rhw and y >= header_h:
                return "row"
            return "both"

        def _row_dim_id() -> str | None:
            ids = list(view.get("row_dim_ids", []) or []) if view else []
            return ids[0] if len(ids) == 1 else None

        def _col_dim_id() -> str | None:
            ids = list(view.get("col_dim_ids", []) or []) if view else []
            return ids[0] if len(ids) == 1 else None

        def _ensure_outline(axis: str) -> None:
            self._ensure_outline_axis(axis)

        def _clear_outline(axis: str) -> None:
            did = self._axis_dim_id(axis)
            if did is None:
                return
            result = self.execute_command("clear_dimension_outline", dim_id=did)
            if not result.success:
                logger.warning("clear_dimension_outline failed: %s", result.error)

        def _add_group(axis: str) -> None:
            name, ok = QtWidgets.QInputDialog.getText(self, "Add Group", "Group name")
            if not ok or not name.strip():
                return
            _ensure_outline(axis)

            did = self._axis_dim_id(axis)
            if did is None:
                return

            root = self._outline_root(axis)
            existing_groups = _collect_groups(root)

            # Validate: group name must be unique (case-insensitive)
            clean_name = name.strip().casefold()
            for group_label, _ in existing_groups:
                if group_label.casefold() == clean_name:
                    QtWidgets.QMessageBox.warning(
                        self, "Duplicate Group Name",
                        f"A group named '{name}' already exists in this dimension."
                    )
                    return

            # Validate: group name must not conflict with any item name
            dim = self._workspace_read_model.get_dimension(did)
            dim_items = dim.get("items", []) if dim else []
            for it in dim_items:
                if it.get("name", "").strip().casefold() == clean_name:
                    QtWidgets.QMessageBox.warning(
                        self, "Name Conflict",
                        f"The name '{name}' is already used by an item in this dimension."
                    )
                    return

            # Ask where to place the new group when there are existing groups.
            parent_group_id = None
            parent_group_label = None
            if existing_groups:
                options = ["(root)"] + [g[0] for g in existing_groups]
                chosen_label, ok2 = QtWidgets.QInputDialog.getItem(
                    self, "Add Group", "Add inside:", options, 0, False
                )
                if not ok2:
                    return
                if chosen_label != "(root)":
                    parent_group_label = chosen_label

            # Collect selected item IDs to place under the new group
            item_ids = _selected_item_ids(axis)

            # Dispatch command — graph mutation is the canonical path
            result = self.execute_command(
                "create_group",
                dim_id=did,
                label=name.strip(),
                parent_group_id=parent_group_id,
                parent_group_label=parent_group_label,
                child_item_ids=item_ids if item_ids else None,
            )
            if not result.success:
                QtWidgets.QMessageBox.warning(self, "Error", str(result.error))
                return

            # Trigger sync + refresh so system cubes (e.g. %RECEDG) and all views update
            self.content_changed.emit()
            self.outline_changed.emit()
            self.reload()

        def _selected_item_id(axis: str) -> str | None:
            if axis == "row":
                if not (0 <= self._sel_row < len(self._rows)):
                    return None
                row = self._rows[self._sel_row]
                if not row.get("is_leaf", False):
                    return None
                iid = row.get("item_id")
                return iid if isinstance(iid, str) else None
            else:
                if not (0 <= self._sel_col < len(self._cols)):
                    return None
                col = self._cols[self._sel_col]
                iid = col.get("item_id")
                return iid if isinstance(iid, str) else None

        def _selected_item_ids(axis: str) -> list[str]:
            """Get all selected item IDs based on selection mode."""
            if self._sel_mode == "row" and axis == "row":
                ids = []
                for idx in self._sel_indices:
                    if 0 <= idx < len(self._rows):
                        row = self._rows[idx]
                        if row.get("is_leaf", False):
                            iid = row.get("item_id")
                            if isinstance(iid, str):
                                ids.append(iid)
                return ids
            elif self._sel_mode == "col" and axis == "col":
                ids = []
                for idx in self._sel_indices:
                    if 0 <= idx < len(self._cols):
                        col = self._cols[idx]
                        if col.get("is_leaf", False):
                            iid = col.get("item_id")
                            if isinstance(iid, str):
                                ids.append(iid)
                return ids
            elif self._sel_mode in ("cell", "all"):
                # Extract dimension items from selected cell addresses
                ids: list[str] = []
                cells = self._sel_indices if self._sel_indices else {(self._sel_row, self._sel_col)}
                if axis == "row":
                    for item in cells:
                        if isinstance(item, tuple) and len(item) == 2:
                            r, _ = item
                            if 0 <= r < len(self._rows):
                                row = self._rows[r]
                                if row.get("is_leaf", False):
                                    iid = row.get("item_id")
                                    if isinstance(iid, str) and iid not in ids:
                                        ids.append(iid)
                        elif isinstance(item, int) and 0 <= item < len(self._rows):
                            # Handle case where selection might be row indices
                            row = self._rows[item]
                            if row.get("is_leaf", False):
                                iid = row.get("item_id")
                                if isinstance(iid, str) and iid not in ids:
                                    ids.append(iid)
                elif axis == "col":
                    for item in cells:
                        if isinstance(item, tuple) and len(item) == 2:
                            _, c = item
                            if 0 <= c < len(self._cols):
                                col = self._cols[c]
                                if col.get("is_leaf", False):
                                    iid = col.get("item_id")
                                    if isinstance(iid, str) and iid not in ids:
                                        ids.append(iid)
                        elif isinstance(item, int) and 0 <= item < len(self._cols):
                            col = self._cols[item]
                            if col.get("is_leaf", False):
                                iid = col.get("item_id")
                                if isinstance(iid, str) and iid not in ids:
                                    ids.append(iid)
                return ids
            else:
                # Fallback for unknown selection mode
                iid = _selected_item_id(axis)
                return [iid] if iid else []

        def _collect_groups(nodes: list[OutlineNode], prefix: str = "") -> list[tuple[str, tuple[int, ...]]]:
            out: list[tuple[str, tuple[int, ...]]] = []

            def _walk(ns: list[OutlineNode], path: tuple[int, ...], pfx: str) -> None:
                for i, n in enumerate(ns):
                    cur = path + (i,)
                    label = n.label or ""
                    cur_pfx = (pfx + " / " + label) if pfx else label
                    if n.item_id is None:
                        out.append((cur_pfx, cur))
                        if n.children:
                            _walk(n.children, cur, cur_pfx)

            _walk(nodes, tuple(), prefix)
            return out

        def _move_selected_to_group(axis: str) -> None:
            selected_ids = _selected_item_ids(axis)
            if not selected_ids:
                return
            _ensure_outline(axis)
            root = self._outline_root(axis)
            groups = _collect_groups(root)
            if not groups:
                return
            labels = [g[0] for g in groups]
            chosen, ok = QtWidgets.QInputDialog.getItem(self, "Move To Group", "Group", labels, 0, False)
            if not ok or not chosen:
                return
            gpath = next((p for (lab, p) in groups if lab == chosen), None)
            if gpath is None:
                return
            # Use atomic multi-item move to handle path adjustments correctly
            self._outline.move_multiple_items_to_group(axis, selected_ids, gpath)

        def _ungroup_selected(axis: str) -> None:
            selected_ids = _selected_item_ids(axis)
            if not selected_ids:
                return
            for iid in selected_ids:
                self._ungroup_item(axis, iid)

        def _add_axis_actions(axis: str, parent: QtWidgets.QMenu, action_map: dict[QtGui.QAction, Any]) -> None:
            did = _row_dim_id() if axis == "row" else _col_dim_id()
            if did is None:
                return
            contiguous_indices = self._selected_contiguous_leaf_indices(axis)

            selected_ids = _selected_item_ids(axis)
            sel_count = len(selected_ids)
            
            if sel_count > 1:
                act_add_group = parent.addAction(f"Add Group… ({sel_count} items)")
                act_move = parent.addAction(f"Move Selection To Group… ({sel_count} items)")
                act_ungroup = parent.addAction(f"Ungroup Selection ({sel_count} items)")
            else:
                act_add_group = parent.addAction("Add Group…")
                act_move = parent.addAction("Move Selection To Group…")
                act_ungroup = parent.addAction("Ungroup Selection")

            act_insert_before = parent.addAction("Insert Before")
            act_insert_after = parent.addAction("Insert After")

            act_add_group.setEnabled(True)
            act_move.setEnabled(True)
            act_ungroup.setEnabled(True)
            act_insert_before.setEnabled(bool(contiguous_indices))
            act_insert_after.setEnabled(bool(contiguous_indices))

            action_map[act_add_group] = lambda: _add_group(axis)
            action_map[act_move] = lambda: _move_selected_to_group(axis)
            action_map[act_ungroup] = lambda: _ungroup_selected(axis)
            action_map[act_insert_before] = lambda: self._insert_before(axis)
            action_map[act_insert_after] = lambda: self._insert_after(axis)

        action_map: dict[QtGui.QAction, Any] = {}
        
        # Check if clicking on a specific group header
        hh = self._header_hit(event.pos())
        if hh is not None:
            kind, payload = hh
            if kind == "row_group" and isinstance(payload, tuple) and len(payload) > 1:
                # Right-clicked on a row group header - add aggregate and ungroup options
                group_path = payload[0] if payload and isinstance(payload[0], tuple) else payload
                act_add_agg = menu.addAction("Add Aggregate Item...")
                action_map[act_add_agg] = lambda gp=group_path: self._add_aggregate_item("row", gp)
                act_ungroup_this = menu.addAction("Ungroup This Group")
                action_map[act_ungroup_this] = lambda gp=group_path: self._outline.ungroup_group("row", gp)
                menu.addSeparator()
            elif kind == "col_group" and isinstance(payload, tuple) and len(payload) > 1:
                # Right-clicked on a column group header - add aggregate and ungroup options
                group_path = payload[0] if payload and isinstance(payload[0], tuple) else payload
                act_add_agg = menu.addAction("Add Aggregate Item...")
                action_map[act_add_agg] = lambda gp=group_path: self._add_aggregate_item("col", gp)
                act_ungroup_this = menu.addAction("Ungroup This Group")
                action_map[act_ungroup_this] = lambda gp=group_path: self._outline.ungroup_group("col", gp)
                menu.addSeparator()
        
        axis = _axis_under_cursor()
        if axis == "row":
            _add_axis_actions("row", menu, action_map)
        elif axis == "col":
            _add_axis_actions("col", menu, action_map)
        else:
            mrow = menu.addMenu("Row")
            mcol = menu.addMenu("Column")
            mdesign = menu.addMenu("Mode")
            _add_axis_actions("row", mrow, action_map)
            _add_axis_actions("col", mcol, action_map)
            # Placeholder for Design view mode actions
            mdesign.addAction("Block Mode").setEnabled(False)
            mdesign.addAction("Outline Mode").setEnabled(False)
            mdesign.addAction("Design Mode").setEnabled(False)

        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        fn = action_map.get(chosen)
        if fn is None:
            return
        fn()
        if self._preserving_selection:
            return
        self.content_changed.emit()
        self.reload()
        self.outline_changed.emit()

    def _clamp_selection_to_leaf(self) -> None:
        """Ensure selection is on a leaf row/col if possible."""
        self._sel_row, self._sel_col = self._navigation.clamp_selection_to_leaf(
            self._sel_row, self._sel_col, self._rows, self._cols
        )

    def _find_next_leaf_row(self, start: int) -> int:
        return self._navigation.find_next_leaf(self._rows, start)

    def _find_next_leaf_col(self, start: int) -> int:
        return self._navigation.find_next_leaf(self._cols, start)

    def _build_rows(self, view: Any, raw_row_keys: list[tuple[str, ...]]) -> None:
        row_dim_ids = list(
            getattr(view, "row_dim_ids", None)
            or (view.get("row_dim_ids") if isinstance(view, dict) else None)
            or []
        )
        outline = self._axis_outline("row")

        if self._is_system_cube_view() and DEBUG_GUI:
            print(
                f"[SYS-CUBE] _build_rows: row_dim_ids={row_dim_ids} "
                f"raw_row_keys={len(raw_row_keys)} outline={len(outline) if outline else 0} nodes",
                flush=True,
            )

        # No row dimensions: treat as a single "value" row (or N generic rows) so
        # 1D cubes with their only dimension on the X-axis still display data.
        if not row_dim_ids:
            row_count = max(1, len(raw_row_keys))
            self._rows = []
            self._row_keys = list(raw_row_keys)
            for r_i in range(row_count):
                self._rows.append(
                    {
                        "is_leaf": True,
                        "item_id": None,
                        "labels": [""],
                        "label_paths": [],
                        "path": (r_i,),
                    }
                )
            if not self._row_keys:
                # Align with Engine.view_row_keys() which returns [()] when there are
                # no row dims; this keeps addressing consistent with QTableView.
                self._row_keys = [tuple()]
            self._row_header_levels = 1
            self._row_band_levels = 0
            self._row_bands = []
            self._geometry._rebuild_leaf_index_cache()
            return

        if len(row_dim_ids) == 1 and outline:
            # Pre-compute max group depth from outline structure, independent of key_map.
            # This keeps band levels stable across all reload() calls.
            def _max_outline_depth(nodes: list, d: int) -> int:
                mx = d
                for n in nodes:
                    if n.item_id is None and n.children:
                        mx = max(mx, _max_outline_depth(n.children, d + 1))
                    else:
                        mx = max(mx, d)
                return mx

            outline_band_levels = max(0, _max_outline_depth(outline, 1) - 1)

            dim = self._workspace_read_model.get_dimension(row_dim_ids[0])
            name_by_id = {it["id"]: it["name"] for it in dim.get("items", [])} if dim else {}
            key_map: dict[str, tuple[str, ...]] = {}
            for k in raw_row_keys:
                if len(k) == 1:
                    key_map[k[0]] = k

            leaves: list[dict[str, Any]] = []
            max_label_len = 1
            any_match = False

            def _walk(
                nodes: list[OutlineNode],
                prefix_labels: list[str],
                prefix_label_paths: list[tuple[int, ...]],
                prefix_path: tuple[int, ...],
                hidden: bool,
                depth: int,
            ) -> None:
                nonlocal max_label_len, any_match
                for i, n in enumerate(nodes):
                    path = prefix_path + (i,)
                    collapsed = path in self._row_collapsed
                    is_group = n.item_id is None
                    add_label = is_group and isinstance(n.label, str) and bool(n.label)
                    labels = prefix_labels + ([n.label] if add_label else [])
                    label_paths = prefix_label_paths + ([path] if add_label else [])
                    max_label_len = max(max_label_len, len(labels))

                    if is_group and n.children:
                        _walk(n.children, labels, label_paths, path, hidden or collapsed, depth + 1)
                        continue

                    if isinstance(n.item_id, str) and n.item_id in key_map:
                        any_match = True
                        if not hidden:
                            leaf_label = name_by_id.get(n.item_id, str(n.item_id))
                            final_labels = labels + [leaf_label]
                            final_label_paths = label_paths + [path]
                            max_label_len = max(max_label_len, len(final_labels))
                            leaves.append(
                                {
                                    "node_id": n.node_id,
                                    "item_id": n.item_id,
                                    "labels": final_labels,
                                    "label_paths": final_label_paths,
                                    "path": path,
                                    "display_edge_kind": n.display_edge_kind,
                                    "is_aggregate": getattr(n, "is_aggregate", False),
                                }
                            )

            _walk(outline, [], [], tuple(), False, 1)

            if leaves:
                added_ids = set()
                row_keys_from_outline: list[tuple[str, ...]] = []
                for leaf in leaves:
                    item_id = leaf["item_id"]
                    if item_id not in key_map:
                        continue
                    # Skip if already added (prevents duplicates from outline)
                    if item_id in added_ids:
                        continue
                    self._rows.append(
                        {
                            "is_leaf": True,
                            "node_id": leaf.get("node_id"),
                            "item_id": item_id,
                            "labels": leaf["labels"],
                            "label_paths": leaf.get("label_paths") or [],
                            "path": leaf["path"],
                            "display_edge_kind": leaf.get("display_edge_kind"),
                            "is_aggregate": leaf.get("is_aggregate", False),
                        }
                    )
                    row_keys_from_outline.append(key_map[item_id])
                    added_ids.add(item_id)
                # Append ungraphed items (in dim.items order) that have no
                # node in the outline graph.  This matches the engine's
                # _dimension_effective_order which appends ungraphed items
                # after graphed ones.
                if dim is not None:
                    for it in dim.get("items", []):
                        iid = it["id"]
                        if iid in key_map and iid not in added_ids:
                            label = name_by_id.get(iid, str(iid))
                            self._rows.append(
                                {
                                    "is_leaf": True,
                                    "node_id": None,
                                    "item_id": iid,
                                    "labels": [label],
                                    "label_paths": [],
                                    "path": (len(self._rows),),
                                    "display_edge_kind": None,
                                    "is_aggregate": False,
                                }
                            )
                            row_keys_from_outline.append(key_map[iid])
                            added_ids.add(iid)
                # Use outline order rows and bands
                self._row_band_levels = outline_band_levels
                self._row_header_levels = max(1, outline_band_levels + 1)
                self._row_bands = self._compute_row_bands(self._rows, self._row_band_levels)
                self._row_keys = row_keys_from_outline
                self._geometry._rebuild_leaf_index_cache()
                return
        
        # True fallback: no outline
        if len(row_dim_ids) == 1:
            dim = self._workspace_read_model.get_dimension(row_dim_ids[0])
            name_by_id = {it["id"]: it["name"] for it in dim.get("items", [])} if dim else {}
            for r_i, k in enumerate(raw_row_keys):
                iid = k[0] if k else None
                label = name_by_id.get(iid, "") if isinstance(iid, str) else ""
                if not label:
                    label = self._grid_read_model.row_header(self._view_id, r_i)
                self._rows.append(
                    {
                        "is_leaf": True,
                        "item_id": iid,
                        "labels": [label],
                        "label_paths": [],
                        "path": (r_i,),
                    }
                )
            self._row_keys = list(raw_row_keys)
            self._row_header_levels = 1
            self._row_band_levels = 0
            self._row_bands = []
            self._geometry._rebuild_leaf_index_cache()
            return

        # Multiple row dimensions: preserve grouping for any outlined dimension,
        # regardless of stack position (first/middle/last).
        dim_name_maps: list[dict[str, str]] = []
        dim_group_label_maps: list[dict[str, list[str]]] = []
        dim_aggregate_sets: list[set[str]] = []

        def _group_labels_by_item(nodes: list[Any]) -> dict[str, list[str]]:
            out: dict[str, list[str]] = {}

            def _node_item_id(n):
                return n.get("item_id") if isinstance(n, dict) else getattr(n, "item_id", None)

            def _node_children(n):
                return n.get("children") if isinstance(n, dict) else getattr(n, "children", None)

            def _node_label(n):
                return n.get("label") if isinstance(n, dict) else getattr(n, "label", None)

            def _walk(ns: list[Any], prefix: list[str]) -> None:
                for n in ns:
                    item_id = _node_item_id(n)
                    children = _node_children(n)
                    is_group = item_id is None and isinstance(children, list) and bool(children)
                    label = _node_label(n)
                    next_prefix = prefix
                    if is_group and isinstance(label, str) and label:
                        next_prefix = prefix + [label]
                    if is_group:
                        _walk(children, next_prefix)
                    elif isinstance(item_id, str):
                        out[item_id] = list(next_prefix)

            _walk(nodes, [])
            return out

        def _aggregate_items(nodes: list[Any]) -> set[str]:
            out: set[str] = set()

            def _node_item_id(n):
                return n.get("item_id") if isinstance(n, dict) else getattr(n, "item_id", None)

            def _node_children(n):
                return n.get("children") if isinstance(n, dict) else getattr(n, "children", None)

            def _node_is_aggregate(n):
                return n.get("is_aggregate") if isinstance(n, dict) else getattr(n, "is_aggregate", False)

            def _walk(ns: list[Any]) -> None:
                for n in ns:
                    item_id = _node_item_id(n)
                    children = _node_children(n)
                    is_group = item_id is None and isinstance(children, list) and bool(children)
                    if is_group:
                        _walk(children)
                    elif isinstance(item_id, str) and _node_is_aggregate(n):
                        out.add(item_id)

            _walk(nodes)
            return out

        for did in row_dim_ids:
            dim = self._workspace_read_model.get_dimension(did)
            dim_name_maps.append({it["id"]: it["name"] for it in dim.get("items", [])} if dim else {})
            outline_nodes = list(dim.get("outline", []) if dim else [])
            dim_group_label_maps.append(_group_labels_by_item(outline_nodes) if outline_nodes else {})
            dim_aggregate_sets.append(_aggregate_items(outline_nodes) if outline_nodes else set())

        # Pre-compute max group depth per dimension
        dim_max_depths: list[int] = []
        for group_map in dim_group_label_maps:
            max_depth = 0
            for group_labels in group_map.values():
                max_depth = max(max_depth, len(group_labels))
            dim_max_depths.append(max_depth)

        for r_i, key in enumerate(raw_row_keys):
            labels: list[str] = []
            label_paths: list[tuple[int, ...] | None] = []
            is_aggregate = False

            for dim_idx, (iid, name_map, group_map, agg_set) in enumerate(zip(key, dim_name_maps, dim_group_label_maps, dim_aggregate_sets)):
                if isinstance(iid, str) and iid in agg_set:
                    is_aggregate = True
                group_labels = list(group_map.get(iid, [])) if isinstance(iid, str) else []
                max_depth = dim_max_depths[dim_idx]
                
                # Pad with empty strings to match max depth for this dimension
                for d in range(max_depth):
                    if d < len(group_labels):
                        labels.append(group_labels[d])
                        label_paths.append((dim_idx, d))
                    else:
                        labels.append("")
                        label_paths.append(None)  # Padding has no path
                
                # Add leaf label
                if isinstance(iid, str):
                    labels.append(name_map.get(iid, ""))
                else:
                    labels.append("")
                label_paths.append((dim_idx,))

            self._rows.append(
                {
                    "is_leaf": True,
                    "item_id": key[-1] if key else None,
                    "labels": labels,
                    "label_paths": label_paths,
                    "path": (r_i,),
                    "is_aggregate": is_aggregate,
                }
            )

        self._row_keys = list(raw_row_keys)
        self._row_header_levels = max(1, max((len(r.get("labels") or []) for r in self._rows), default=len(row_dim_ids)))
        self._row_band_levels = max(0, self._row_header_levels - 1)
        self._row_bands = self._compute_row_bands(self._rows, self._row_band_levels)
        self._geometry._rebuild_leaf_index_cache()

    def _build_cols(self, view: Any, raw_col_keys: list[tuple[str, ...]]) -> None:
        col_dim_ids = list(
            getattr(view, "col_dim_ids", None)
            or (view.get("col_dim_ids") if isinstance(view, dict) else None)
            or []
        )
        outline = self._axis_outline("col")

        if self._is_system_cube_view() and DEBUG_GUI:
            print(
                f"[SYS-CUBE] _build_cols: col_dim_ids={col_dim_ids} "
                f"raw_col_keys={len(raw_col_keys)} outline={len(outline) if outline else 0} nodes",
                flush=True,
            )

        if len(col_dim_ids) == 1 and outline:
            def _max_outline_depth(nodes: list, d: int) -> int:
                mx = d
                for n in nodes:
                    if n.item_id is None and n.children:
                        mx = max(mx, _max_outline_depth(n.children, d + 1))
                    else:
                        mx = max(mx, d)
                return mx

            outline_band_levels = max(0, _max_outline_depth(outline, 0))

            dim = self._workspace_read_model.get_dimension(col_dim_ids[0])
            name_by_id = {it["id"]: it["name"] for it in dim.get("items", [])} if dim else {}
            key_map: dict[str, tuple[str, ...]] = {}
            for k in raw_col_keys:
                if len(k) == 1:
                    key_map[k[0]] = k

            leaves: list[dict[str, Any]] = []
            max_label_len = 1
            any_match = False

            def _walk(
                nodes: list[OutlineNode],
                prefix_labels: list[str],
                prefix_label_paths: list[tuple[int, ...]],
                prefix_path: tuple[int, ...],
                hidden: bool,
                depth: int,
            ) -> None:
                nonlocal max_label_len, any_match
                for i, n in enumerate(nodes):
                    path = prefix_path + (i,)
                    collapsed = path in self._col_collapsed
                    add_label = isinstance(n.label, str) and bool(n.label)
                    labels = prefix_labels + ([n.label] if add_label else [])
                    label_paths = prefix_label_paths + ([path] if add_label else [])
                    max_label_len = max(max_label_len, len(labels))

                    if n.item_id is None and n.children:
                        _walk(n.children, labels, label_paths, path, hidden or collapsed, depth + 1)
                        continue

                    if isinstance(n.item_id, str) and n.item_id in key_map:
                        any_match = True
                        if not hidden:
                            leaf_label = name_by_id.get(n.item_id, str(n.item_id))
                            leaves.append(
                                {
                                    "node_id": n.node_id,
                                    "item_id": n.item_id,
                                    "labels": prefix_labels + [leaf_label],
                                    "label_paths": label_paths + [path],
                                    "path": path,
                                    "display_edge_kind": n.display_edge_kind,
                                    "is_aggregate": getattr(n, "is_aggregate", False),
                                }
                            )

            _walk(outline, [], [], tuple(), False, 1)

            # If the outline doesn't match the current axis (e.g. after flipping row/col),
            # it can yield zero matching leaves. In that case, fall back to flat keys.
            if leaves:
                added_ids = set()
                for leaf in leaves:
                    item_id = leaf["item_id"]
                    # Skip if already added (prevents duplicates from outline)
                    if item_id in added_ids:
                        continue
                    self._cols.append(
                        {
                            "is_leaf": True,
                            "node_id": leaf.get("node_id"),
                            "item_id": item_id,
                            "labels": leaf["labels"],
                            "label_paths": leaf.get("label_paths") or [],
                            "path": leaf["path"],
                            "display_edge_kind": leaf.get("display_edge_kind"),
                            "is_aggregate": leaf.get("is_aggregate", False),
                        }
                    )
                    self._col_keys.append(key_map[item_id])
                    added_ids.add(item_id)
                
                # Append any items from raw_col_keys that aren't in the outline
                # to prevent items from disappearing when they're not yet grouped
                for iid, key in key_map.items():
                    if iid not in added_ids:
                        label = name_by_id.get(iid, str(iid))
                        self._cols.append(
                            {
                                "is_leaf": True,
                                "item_id": iid,
                                "labels": [label],
                                "label_paths": [],
                                "path": (len(outline) + len(self._cols),),
                            }
                        )
                        self._col_keys.append(key)
                
                # Use pre-computed outline depth to keep bands stable even when groups become empty
                self._col_band_levels = outline_band_levels
                self._col_header_levels = max(1, outline_band_levels + 1)
                self._col_bands = self._compute_col_bands(self._cols, self._col_band_levels)
                return

            if any_match:
                # Outline matches this axis, but all leaves are currently hidden (likely collapsed).
                # Keep empty display rather than falling back to flat path-joined headers.
                self._col_band_levels = outline_band_levels
                self._col_header_levels = max(1, outline_band_levels + 1)
                self._col_bands = []
                return

        # Enhanced: support stacked groupings for multiple dimensions, preserving
        # outlined groups for any dimension position.
        if len(col_dim_ids) > 1:
            dim_name_maps: list[dict[str, str]] = []
            dim_group_label_maps: list[dict[str, list[str]]] = []
            dim_aggregate_sets: list[set[str]] = []

            def _group_labels_by_item(nodes: list[Any]) -> dict[str, list[str]]:
                out: dict[str, list[str]] = {}

                def _node_item_id(n):
                    return n.get("item_id") if isinstance(n, dict) else getattr(n, "item_id", None)

                def _node_children(n):
                    return n.get("children") if isinstance(n, dict) else getattr(n, "children", None)

                def _node_label(n):
                    return n.get("label") if isinstance(n, dict) else getattr(n, "label", None)

                def _walk(ns: list[Any], prefix: list[str]) -> None:
                    for n in ns:
                        item_id = _node_item_id(n)
                        children = _node_children(n)
                        is_group = item_id is None and isinstance(children, list) and bool(children)
                        label = _node_label(n)
                        next_prefix = prefix
                        if is_group and isinstance(label, str) and label:
                            next_prefix = prefix + [label]
                        if is_group:
                            _walk(children, next_prefix)
                        elif isinstance(item_id, str):
                            out[item_id] = list(next_prefix)

                _walk(nodes, [])
                return out

            def _aggregate_items(nodes: list[Any]) -> set[str]:
                out: set[str] = set()

                def _node_item_id(n):
                    return n.get("item_id") if isinstance(n, dict) else getattr(n, "item_id", None)

                def _node_children(n):
                    return n.get("children") if isinstance(n, dict) else getattr(n, "children", None)

                def _node_is_aggregate(n):
                    return n.get("is_aggregate") if isinstance(n, dict) else getattr(n, "is_aggregate", False)

                def _walk(ns: list[Any]) -> None:
                    for n in ns:
                        item_id = _node_item_id(n)
                        children = _node_children(n)
                        is_group = item_id is None and isinstance(children, list) and bool(children)
                        if is_group:
                            _walk(children)
                        elif isinstance(item_id, str) and _node_is_aggregate(n):
                            out.add(item_id)

                _walk(nodes)
                return out

            for did in col_dim_ids:
                dim = self._workspace_read_model.get_dimension(did)
                dim_name_maps.append({it["id"]: it["name"] for it in dim.get("items", [])} if dim else {})
                outline_nodes = list(dim.get("outline", []) if dim else [])
                dim_group_label_maps.append(_group_labels_by_item(outline_nodes) if outline_nodes else {})
                dim_aggregate_sets.append(_aggregate_items(outline_nodes) if outline_nodes else set())

            # Pre-compute max group depth per dimension
            dim_max_depths: list[int] = []
            for group_map in dim_group_label_maps:
                max_depth = 0
                for group_labels in group_map.values():
                    max_depth = max(max_depth, len(group_labels))
                dim_max_depths.append(max_depth)

            for c_i, k in enumerate(raw_col_keys):
                labels: list[str] = []
                label_paths: list[tuple[int, ...] | None] = []
                is_aggregate = False
                
                for dim_idx, (iid, name_map, group_map, agg_set) in enumerate(zip(k, dim_name_maps, dim_group_label_maps, dim_aggregate_sets)):
                    if isinstance(iid, str) and iid in agg_set:
                        is_aggregate = True
                    group_labels = list(group_map.get(iid, [])) if isinstance(iid, str) else []
                    max_depth = dim_max_depths[dim_idx]
                    
                    # Pad with empty strings to match max depth for this dimension
                    for d in range(max_depth):
                        if d < len(group_labels):
                            labels.append(group_labels[d])
                            label_paths.append((dim_idx, d))
                        else:
                            labels.append("")
                            label_paths.append(None)  # Padding has no path
                    
                    # Add leaf label
                    if isinstance(iid, str):
                        labels.append(name_map.get(iid, ""))
                    else:
                        labels.append("")
                    label_paths.append((dim_idx,))

                self._cols.append(
                    {
                        "is_leaf": True,
                        "item_id": k[-1] if k else None,
                        "labels": labels,
                        "label_paths": label_paths,
                        "path": (c_i,),
                        "is_aggregate": is_aggregate,
                    }
                )

            self._col_header_levels = max(1, max((len(c.get("labels") or []) for c in self._cols), default=len(col_dim_ids)))
            self._col_band_levels = max(0, self._col_header_levels - 1)
            self._col_bands = self._compute_col_bands(self._cols, self._col_band_levels)
            self._col_keys = list(raw_col_keys)
            return
        
        # Fallback: flat columns for single dimension
        name_by_id: dict[str, str] = {}
        if len(col_dim_ids) == 1:
            dim = self._workspace_read_model.get_dimension(col_dim_ids[0])
            name_by_id = {it["id"]: it["name"] for it in dim.get("items", [])} if dim else {}
        for c_i, k in enumerate(raw_col_keys):
            iid = k[0] if k else None
            label = ""
            if isinstance(iid, str):
                label = name_by_id.get(iid, "")
            if not label:
                label = self._grid_read_model.col_header(self._view_id, c_i)
            self._cols.append(
                {
                    "is_leaf": True,
                    "item_id": iid,
                    "labels": [label],
                    "label_paths": [],
                    "path": (c_i,),
                }
            )
        self._col_keys = list(raw_col_keys)
        self._col_header_levels = 1
        self._col_band_levels = 0
        self._col_bands = []

    def _compute_col_bands(self, cols: list[dict[str, Any]], levels: int) -> list[dict[str, Any]]:
        """Compute column bands, filling gaps with empty placeholders for alignment."""
        return self._banding.compute_col_bands()

    def _compute_row_bands(self, rows: list[dict[str, Any]], levels: int) -> list[dict[str, Any]]:
        """Compute row bands, filling gaps with empty placeholders for alignment."""
        return self._banding.compute_row_bands()

    # ------------------------------------------------------------
    # Geometry / scrolling
    # ------------------------------------------------------------

    def _row_header_width(self) -> int:
        """Calculate dynamic row header width based on number of levels and custom widths."""
        total_width = 0
        for level in range(max(1, self._row_header_levels)):
            total_width += self._row_header_level_width(level)
        return total_width

    def _content_size(self) -> QtCore.QSize:
        # Use actual per-column widths (including user-resized columns) instead of
        # assuming a fixed default width for all columns. Otherwise the content
        # width is underestimated and the horizontal scrollbar cannot reach the
        # true rightmost edge.
        row_header_w = self._row_header_width()
        w = row_header_w
        for c in range(len(self._cols)):
            w += self._col_width(c)
        header_h = self._m.col_header_h * max(1, self._col_header_levels)
        h = header_h + len(self._rows) * self._m.row_h
        return QtCore.QSize(w, h)

    def _update_scrollbars(self) -> None:
        sz = self._content_size()
        vp = self.viewport().size()

        self.horizontalScrollBar().setPageStep(vp.width())
        self.verticalScrollBar().setPageStep(vp.height())

        self.horizontalScrollBar().setRange(0, max(0, sz.width() - vp.width()))
        self.verticalScrollBar().setRange(0, max(0, sz.height() - vp.height()))

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_scrollbars()
        self._invalidate_formatted_cache()
        self._start_tile_fetch()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        # Reposition inline editor if visible to keep it tied to the canvas
        if self._editor is not None and self._editor.isVisible():
            geom = self._editor.geometry()
            self._editor.setGeometry(geom.adjusted(dx, dy, dx, dy))
        # Keep plain cache across scrolls for instant visibility; only invalidate formatted
        self._invalidate_formatted_cache()
        self._start_tile_fetch()
        self.viewport().update()

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # type: ignore[override]
        """Handle wheel events with configurable scroll sensitivity."""
        sensitivity = gui_config("behavior", "mouse_scroll_sensitivity", 1.0)
        if sensitivity != 1.0:
            # Scale the scroll delta by the sensitivity factor
            delta = event.angleDelta()
            scaled_delta = QtCore.QPoint(
                int(delta.x() * sensitivity),
                int(delta.y() * sensitivity)
            )
            # Create a new wheel event with the scaled delta
            new_event = QtGui.QWheelEvent(
                event.position(),
                event.globalPosition(),
                event.pixelDelta(),
                scaled_delta,
                event.buttons(),
                event.modifiers(),
                event.phase(),
                event.inverted()
            )
            super().wheelEvent(new_event)
        else:
            super().wheelEvent(event)

    # ------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        with self._span("MatrixGrid.paintEvent"):
            self._paint_event_impl(event)

    def _paint_event_impl(self, event: QtGui.QPaintEvent) -> None:
        # Avoid calling super().paintEvent(event) — QAbstractScrollArea::paintEvent
        # is empty, and QFrame::paintEvent can create an internal painter that
        # conflicts with our viewport painter in some Qt6/Fusion paths.
        p = QtGui.QPainter(self.viewport())
        p.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
        try:

            # F5c: use cached view metadata instead of paint-path get_view
            view_meta = self._cached_view_meta or {}
            row_dim_ids = list(view_meta.get("row_dim_ids", []) or [])
            col_dim_ids = list(view_meta.get("col_dim_ids", []) or [])
            row_stacked = len(row_dim_ids) > 1
            col_stacked = len(col_dim_ids) > 1
        
            # Get max indices for unstacked logic
            max_row_idx = len(self._rows) - 1 if self._rows else 0
            max_col_idx = len(self._cols) - 1 if self._cols else 0

            off = self._scroll_offset()
            vp = self.viewport().rect()

            # Fill background
            p.fillRect(vp, QtGui.QColor("white"))

            # Visible ranges
            x0 = off.x()
            y0 = off.y()

            header_h = self._m.col_header_h * max(1, self._col_header_levels)
            row_header_w = self._row_header_width()
            col_header_clip = QtCore.QRect(row_header_w, 0, max(0, vp.width() - row_header_w), header_h)
            row_header_clip = QtCore.QRect(0, header_h, row_header_w, max(0, vp.height() - header_h))

            # Calculate visible column range using custom widths
            first_col = 0
            last_col = len(self._cols) - 1
            col_x = row_header_w
            for c in range(len(self._cols)):
                col_w = self._col_width(c)
                if col_x + col_w >= x0:
                    first_col = c
                    break
                col_x += col_w
            col_x = row_header_w
            for c in range(len(self._cols)):
                col_w = self._col_width(c)
                col_x += col_w
                if col_x >= x0 + vp.width():
                    last_col = c
                    break
            first_row = max(0, (y0 - header_h) // self._m.row_h)
            last_row = min(len(self._rows) - 1, (y0 - header_h + vp.height()) // self._m.row_h)

            # Batch fetch all visible cells via snapshot query (F5c: no per-cell engine lookups)
            visible_cells, hardnumber_addrs, visible_font_colors, visible_fills = self._fetch_visible_cells(first_row, last_row, first_col, last_col)
            DEBUG_GUI and print(
                f"DEBUG paintEvent: img_cache={len(self._tile_image_cache)} "
                f"snapshot_cache={len(self._tile_cache)} "
                f"visible_cells={len(visible_cells)} "
                f"bounds=({first_row}-{last_row},{first_col}-{last_col})"
            )

            # Headers backgrounds
            header_br = QtCore.QRect(0, 0, vp.width(), header_h)
            p.fillRect(header_br, self._m.header_bg)
            row_header_br = QtCore.QRect(0, 0, row_header_w, vp.height())
            p.fillRect(row_header_br, self._m.header_bg)

            # Column headers
            p.setPen(self._m.header_fg)
            p.save()
            p.setClipRect(col_header_clip)
            if self._col_band_levels <= 0:
                col_x = row_header_w
                for c in range(first_col + 1):
                    col_x += self._col_width(c - 1) if c > 0 else 0
                for c in range(first_col, last_col + 1):
                    col_w = self._col_width(c)
                    x = col_x - x0
                    r = QtCore.QRect(x, 0, col_w, self._m.col_header_h)
                    col_x += col_w
                
                    # Get item format
                    item_fmt = CellFormat()
                    if 0 <= c < len(self._cols):
                        col = self._cols[c]
                        item_id = col.get("item_id")
                        col_dim_ids = view_meta.get("col_dim_ids", [])
                        if item_id and col_dim_ids:
                            dim_id = col_dim_ids[0]
                            item_key = f"{dim_id}:{item_id}"
                            item_fmt = view_meta.get("item_formats", {}).get(item_key, CellFormat())
                
                    # Highlight selected / related column headers
                    if self._sel_mode == "col" and c in self._sel_indices:
                        p.fillRect(r, self._m.sel_bg)
                    elif self._sel_mode == "cell" and c == self._sel_col:
                        p.fillRect(r, self._m.sel_bg)
                    elif self._sel_mode == "all":
                        p.fillRect(r, self._m.sel_bg)  # All columns selected
                    elif self._sel_mode == "col" and self._is_related_col(c):
                        p.fillRect(r, self._m.related_bg)
                    elif item_fmt.bg_color:
                        try:
                            p.fillRect(r, QtGui.QColor(item_fmt.bg_color))
                        except Exception:
                            pass  # Skip invalid color

                    p.setPen(self._m.gridline)
                    p.drawRect(r)

                    if (self._sel_mode == "col" and c in self._sel_indices) or (self._sel_mode == "cell" and c == self._sel_col) or self._sel_mode == "all":
                        p.setPen(self._m.sel_fg)
                    else:
                        # Set font color from format (auto-contrast if not explicitly set)
                        text_color = item_fmt.font_color if item_fmt.font_color else get_contrast_font_color(item_fmt.bg_color)
                        p.setPen(QtGui.QColor(text_color))

                    # Set font from format
                    font = QtGui.QFont(str(item_fmt.font_family) if item_fmt.font_family else "sans-serif",
                                       int(item_fmt.font_size) if item_fmt.font_size else 9)
                    if item_fmt.font_weight:
                        try:
                            fw = int(item_fmt.font_weight)
                            fw = max(1, min(1000, fw))
                            font.setWeight(QtGui.QFont.Weight(fw))
                        except (ValueError, TypeError):
                            pass
                    font.setItalic(bool(item_fmt.font_italic))
                    p.setFont(font)

                    # Show leaf label only (not group path like "Group / Jan").
                    txt = ""
                    if 0 <= c < len(self._cols):
                        labs = list(self._cols[c].get("labels") or [])
                        txt = str(labs[-1]) if labs else ""
                    if not txt:
                        txt = self._grid_read_model.col_header(self._view_id, c)
                    align = self._get_text_alignment(item_fmt.text_h_align, item_fmt.text_v_align)
                    p.drawText(r.adjusted(4, 0, -4, 0), align, txt)
            else:
                # Bands (top rows)
                for band in self._col_bands:
                    level = int(band["level"])
                    y = level * self._m.col_header_h
                    c0 = int(band["c0"])
                    c1 = int(band["c1"])
                    # skip if outside visible
                    if c1 < first_col or c0 > last_col:
                        continue
                    # Calculate x position and width using custom column widths
                    x = row_header_w
                    for i in range(c0):
                        x += self._col_width(i)
                    x -= x0
                    w = 0
                    for i in range(c0, c1 + 1):
                        w += self._col_width(i)
                    r = QtCore.QRect(x, y, w, self._m.col_header_h)
                
                    # Get group format
                    path = band.get("path")
                    group_key = ",".join(str(i) for i in path) if isinstance(path, tuple) else ""
                    group_fmt = view_meta.get("group_formats", {}).get(group_key, CellFormat())
                
                    # Check if this is an empty placeholder band
                    is_empty_band = not band.get("label") and band.get("path") is None
                
                    # Check if this band represents an actual group (has outline path with length >= 1)
                    path = band.get("path")
                    is_group_band = (not is_empty_band and isinstance(path, tuple) and len(path) >= 1)
                
                    # Apply background color from format
                    bg_color = group_fmt.bg_color if group_fmt.bg_color else self._m.header_bg
                    try:
                        p.fillRect(r, QtGui.QColor(bg_color))
                    except Exception:
                        p.fillRect(r, self._m.header_bg)  # Fallback to default
                
                    # Draw diagonal shading based on stacked/unstacked mode
                    if is_group_band:
                        should_shade = False
                        if col_stacked:
                            # Stacked: shade if path has more than one element
                            should_shade = isinstance(path, tuple) and len(path) > 1
                        else:
                            # Unstacked: shade if label not empty
                            should_shade = bool(band.get("label"))
                        if should_shade:
                            self._draw_diagonal_shading(p, r, "#d0d0d0")
                
                    p.setPen(self._m.gridline)
                    p.drawRect(r)
                
                    # Draw band label
                    if not is_empty_band:
                        text_color = group_fmt.font_color if group_fmt.font_color else get_contrast_font_color(group_fmt.bg_color)
                        p.setPen(QtGui.QColor(text_color))
                        font = QtGui.QFont(str(group_fmt.font_family) if group_fmt.font_family else "sans-serif",
                                           int(group_fmt.font_size) if group_fmt.font_size else 9)
                        if group_fmt.font_weight:
                            try:
                                fw = int(group_fmt.font_weight)
                                fw = max(1, min(1000, fw))
                                font.setWeight(QtGui.QFont.Weight(fw))
                            except (ValueError, TypeError):
                                pass
                        font.setItalic(bool(group_fmt.font_italic))
                        p.setFont(font)
                        align = self._get_text_alignment(group_fmt.text_h_align, group_fmt.text_v_align)
                        p.drawText(r.adjusted(4, 0, -4, 0), align, str(band.get("label") or ""))

                    # Selection/drag outline for selected or dragged column group
                    if (
                        isinstance(path, tuple)
                        and (
                            (self._drag_is_group and self._drag_axis == "col" and self._group_drag_anchor_band_path == path)
                            or self._sel_group_path == ("col", path)
                        )
                    ):
                        p.save()
                        p.setPen(QtGui.QPen(QtGui.QColor(25, 118, 210), 2))
                        p.drawRect(r.adjusted(1, 1, -1, -1))
                        p.restore()

                # Leaf row (bottom header row)
                y_leaf = self._col_band_levels * self._m.col_header_h
                col_x = row_header_w
                for c in range(first_col + 1):
                    col_x += self._col_width(c - 1) if c > 0 else 0
                for c in range(first_col, last_col + 1):
                    col_w = self._col_width(c)
                    x = col_x - x0
                    r = QtCore.QRect(x, y_leaf, col_w, self._m.col_header_h)
                    col_x += col_w
                
                    # Get item format
                    item_fmt = CellFormat()
                    if 0 <= c < len(self._cols):
                        col = self._cols[c]
                        item_id = col.get("item_id")
                        col_dim_ids = view_meta.get("col_dim_ids", [])
                        if item_id and col_dim_ids:
                            dim_id = col_dim_ids[0]
                            item_key = f"{dim_id}:{item_id}"
                            item_fmt = view_meta.get("item_formats", {}).get(item_key, CellFormat())
                
                    # Highlight selected / related column headers
                    if self._sel_mode == "col" and c in self._sel_indices:
                        p.fillRect(r, self._m.sel_bg)
                    elif self._sel_mode == "cell" and c == self._sel_col:
                        p.fillRect(r, self._m.sel_bg)
                    elif self._sel_mode == "all":
                        p.fillRect(r, self._m.sel_bg)  # All columns selected
                    elif self._sel_mode == "col" and self._is_related_col(c):
                        p.fillRect(r, self._m.related_bg)
                    elif item_fmt.bg_color:
                        try:
                            p.fillRect(r, QtGui.QColor(item_fmt.bg_color))
                        except Exception:
                            pass  # Skip invalid color

                    p.setPen(self._m.gridline)
                    p.drawRect(r)

                    if (self._sel_mode == "col" and c in self._sel_indices) or (self._sel_mode == "cell" and c == self._sel_col) or self._sel_mode == "all":
                        p.setPen(self._m.sel_fg)
                    else:
                        # Set font color from format (auto-contrast if not explicitly set)
                        text_color = item_fmt.font_color if item_fmt.font_color else get_contrast_font_color(item_fmt.bg_color)
                        p.setPen(QtGui.QColor(text_color))

                    # Set font from format
                    font = QtGui.QFont(str(item_fmt.font_family) if item_fmt.font_family else "sans-serif",
                                       int(item_fmt.font_size) if item_fmt.font_size else 9)
                    if item_fmt.font_weight:
                        try:
                            fw = int(item_fmt.font_weight)
                            fw = max(1, min(1000, fw))
                            font.setWeight(QtGui.QFont.Weight(fw))
                        except (ValueError, TypeError):
                            pass
                    font.setItalic(bool(item_fmt.font_italic))
                    p.setFont(font)

                    txt = ""
                    if 0 <= c < len(self._cols):
                        labs = list(self._cols[c].get("labels") or [])
                        txt = str(labs[-1]) if labs else ""
                    align = self._get_text_alignment(item_fmt.text_h_align, item_fmt.text_v_align)
                    p.drawText(r.adjusted(4, 0, -4, 0), align, txt)
                    # Draw sigma for aggregate columns
                    if 0 <= c < len(self._cols) and self._cols[c].get("is_aggregate"):
                        small_font = QtGui.QFont(p.font())
                        small_font.setPointSize(max(6, int(item_fmt.font_size or 9) - 2))
                        p.setFont(small_font)
                        p.drawText(r, QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignRight, "\u03A3")
                        p.setFont(font)
            p.restore()

            # Row headers: bands (left columns) + leaf column (rightmost)
            # Row bands (left side spanning blocks)
            p.save()
            p.setClipRect(row_header_clip)
            for band in self._row_bands:
                level = int(band["level"])
                # Calculate x position using custom widths
                x = 0
                for i in range(level):
                    x += self._row_header_level_width(i)
                level_w = self._row_header_level_width(level)
                r0 = int(band["r0"])
                r1 = int(band["r1"])
                # skip if outside visible
                if r1 < first_row or r0 > last_row:
                    continue
                y = header_h + r0 * self._m.row_h - y0
                h = (r1 - r0 + 1) * self._m.row_h
                r = QtCore.QRect(x, y, level_w, h)
            
                # Get group format
                path = band.get("path")
                group_key = ",".join(str(i) for i in path) if isinstance(path, tuple) else ""
                group_fmt = view_meta.get("group_formats", {}).get(group_key, CellFormat())
            
                # Check if this is an empty placeholder band
                is_empty_band = not band.get("label") and band.get("path") is None
            
                # Check if this band represents an actual group (has outline path with length >= 1)
                path = band.get("path")
                is_group_band = (not is_empty_band and isinstance(path, tuple) and len(path) >= 1)
            
                # Apply background color from format
                bg_color = group_fmt.bg_color if group_fmt.bg_color else self._m.header_bg
                try:
                    p.fillRect(r, QtGui.QColor(bg_color))
                except Exception:
                    p.fillRect(r, self._m.header_bg)  # Fallback to default
            
                # Draw diagonal shading based on stacked/unstacked mode
                if is_group_band:
                    should_shade = False
                    if row_stacked:
                        # Stacked: shade if path has more than one element
                        should_shade = isinstance(path, tuple) and len(path) > 1
                    else:
                        # Unstacked: shade if label not empty
                        should_shade = bool(band.get("label"))
                    if should_shade:
                        self._draw_diagonal_shading(p, r, "#d0d0d0")
            
                p.setPen(self._m.gridline)
                p.drawRect(r)
            
                # Skip text for empty placeholder bands
                if not is_empty_band:
                    # Set font color from format (auto-contrast if not explicitly set)
                    text_color = group_fmt.font_color if group_fmt.font_color else get_contrast_font_color(group_fmt.bg_color)
                    p.setPen(QtGui.QColor(text_color))
                    font = QtGui.QFont(str(group_fmt.font_family) if group_fmt.font_family else "sans-serif",
                                       int(group_fmt.font_size) if group_fmt.font_size else 9)
                    if group_fmt.font_weight:
                        try:
                            fw = int(group_fmt.font_weight)
                            fw = max(1, min(1000, fw))
                            font.setWeight(QtGui.QFont.Weight(fw))
                        except (ValueError, TypeError):
                            pass
                    font.setItalic(bool(group_fmt.font_italic))
                    p.setFont(font)
                
                    align = self._get_text_alignment(group_fmt.text_h_align, group_fmt.text_v_align)
                    p.drawText(r.adjusted(4, 0, -4, 0), align, str(band.get("label") or ""))

                    # Phase 8: stronger highlight for dragged anchor band or selected group
                    if (
                        isinstance(path, tuple)
                        and (
                            (self._drag_is_group and self._drag_axis == "row" and self._group_drag_anchor_band_path == path)
                            or self._sel_group_path == ("row", path)
                        )
                    ):
                        p.save()
                        p.setPen(QtGui.QPen(QtGui.QColor(25, 118, 210), 2))
                        p.drawRect(r.adjusted(1, 1, -1, -1))
                        p.restore()

                    # Phase 8: badge count for collapsed groups with hidden leaf descendants
                    if is_group_band and self._group_drag_badge_count > 0:
                        collapsed = False
                        if isinstance(path, tuple):
                            collapsed = path in self._row_collapsed
                        if collapsed:
                            badge_text = str(self._group_drag_badge_count)
                            p.save()
                            badge_font = QtGui.QFont(p.font())
                            badge_font.setPointSize(max(7, int(group_fmt.font_size or 9) - 1))
                            p.setFont(badge_font)
                            fm = QtGui.QFontMetrics(p.font())
                            tw = fm.horizontalAdvance(badge_text) + 8
                            th = fm.height()
                            bx = r.right() - tw - 2
                            by = r.top() + 2
                            badge_rect = QtCore.QRect(bx, by, tw, th)
                            p.fillRect(badge_rect, QtGui.QColor(220, 220, 220))
                            p.setPen(QtGui.QPen(QtGui.QColor(100, 100, 100)))
                            p.drawRect(badge_rect)
                            p.setPen(QtGui.QColor(50, 50, 50))
                            p.drawText(badge_rect, QtCore.Qt.AlignmentFlag.AlignCenter, badge_text)
                            p.restore()

            # Leaf column (rightmost column of row header)
            x_leaf = 0
            for i in range(self._row_band_levels):
                x_leaf += self._row_header_level_width(i)
            leaf_w = self._row_header_level_width(self._row_band_levels)
            for r in range(first_row, last_row + 1):
                y = header_h + r * self._m.row_h - y0
                rect = QtCore.QRect(x_leaf, y, leaf_w, self._m.row_h)
            
                # Get item format
                item_fmt = CellFormat()
                if 0 <= r < len(self._rows):
                    row = self._rows[r]
                    item_id = row.get("item_id")
                    row_dim_ids = view_meta.get("row_dim_ids", [])
                    if item_id and row_dim_ids:
                        dim_id = row_dim_ids[0]
                        item_key = f"{dim_id}:{item_id}"
                        item_fmt = view_meta.get("item_formats", {}).get(item_key, CellFormat())
            
                # Highlight selected / related row headers
                if self._sel_mode == "row" and r in self._sel_indices:
                    p.fillRect(rect, self._m.sel_bg)
                elif self._sel_mode == "cell" and r == self._sel_row:
                    p.fillRect(rect, self._m.sel_bg)
                elif self._sel_mode == "all":
                    p.fillRect(rect, self._m.sel_bg)  # All rows selected
                elif self._sel_mode == "row" and self._is_related_row(r):
                    p.fillRect(rect, self._m.related_bg)
                elif item_fmt.bg_color:
                    try:
                        p.fillRect(rect, QtGui.QColor(item_fmt.bg_color))
                    except Exception:
                        pass  # Skip invalid color

                p.setPen(self._m.gridline)
                p.drawRect(rect)

                if (self._sel_mode == "row" and r in self._sel_indices) or (self._sel_mode == "cell" and r == self._sel_row) or self._sel_mode == "all":
                    p.setPen(self._m.sel_fg)
                else:
                    # Set font color from format (auto-contrast if not explicitly set)
                    text_color = item_fmt.font_color if item_fmt.font_color else get_contrast_font_color(item_fmt.bg_color)
                    p.setPen(QtGui.QColor(text_color))

                # Set font from format
                font = QtGui.QFont(str(item_fmt.font_family) if item_fmt.font_family else "sans-serif",
                                   int(item_fmt.font_size) if item_fmt.font_size else 9)
                if item_fmt.font_weight:
                        try:
                            fw = int(item_fmt.font_weight)
                            fw = max(1, min(1000, fw))
                            font.setWeight(QtGui.QFont.Weight(fw))
                        except (ValueError, TypeError):
                            pass
                font.setItalic(bool(item_fmt.font_italic))
                p.setFont(font)

                txt = ""
                if 0 <= r < len(self._rows):
                    labs = list(self._rows[r].get("labels") or [])
                    txt = str(labs[-1]) if labs else ""
                align = self._get_text_alignment(item_fmt.text_h_align, item_fmt.text_v_align)
                p.drawText(rect.adjusted(4, 0, -4, 0), align, txt)
                # Draw sigma for aggregate rows
                if 0 <= r < len(self._rows) and self._rows[r].get("is_aggregate"):
                    small_font = QtGui.QFont(p.font())
                    small_font.setPointSize(max(6, int(item_fmt.font_size or 9) - 2))
                    p.setFont(small_font)
                    p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignRight, "\u03A3")
                    p.setFont(font)
            p.restore()

            # Top-left corner (paint last so it always sits above scrolling headers)
            corner_rect = QtCore.QRect(0, 0, row_header_w, header_h)
            if self._sel_mode == "all":
                p.fillRect(corner_rect, self._m.sel_bg)  # Highlight when all selected
            else:
                p.fillRect(corner_rect, self._m.header_bg)
            p.setPen(self._m.gridline)
            p.drawRect(corner_rect)

            # Reset brush state before painting cells
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)

            # Save current clip and set clip region to content area (below headers)
            content_clip = QtCore.QRect(row_header_w, header_h, vp.width() - row_header_w, vp.height() - header_h)
            p.save()
            p.setClipRect(content_clip)

            # Blit pre-rendered tile images for visible tiles (base cell content)
            # Formatted tiles first (full styling), then plain tiles as fallback,
            # then the previous data generation's tiles as a last resort so the
            # grid never goes blank while a fresh batch is still rendering.
            drawn_bounds: set[tuple[int, int, int, int]] = set()
            for tile_bounds, img in self._tile_image_cache.items():
                t_first, t_last, t_fc, t_lc = tile_bounds
                if t_last < first_row or t_first > last_row or t_lc < first_col or t_fc > last_col:
                    continue
                # NEW: skip images rendered from stale data
                if self._image_data_gens.get(tile_bounds, -1) != self._data_generation:
                    continue
                tile_x = row_header_w + sum(self._col_width(i) for i in range(t_fc)) - x0
                tile_y = header_h + t_first * self._m.row_h - y0
                p.drawImage(tile_x, tile_y, img)
                drawn_bounds.add(tile_bounds)
            for tile_bounds, img in self._tile_plain_cache.items():
                t_first, t_last, t_fc, t_lc = tile_bounds
                if t_last < first_row or t_first > last_row or t_lc < first_col or t_fc > last_col:
                    continue
                # NEW: skip plain tiles with stale data
                if self._plain_image_data_gens.get(tile_bounds, -1) != self._data_generation:
                    continue
                if tile_bounds in drawn_bounds:
                    continue  # formatted already drawn
                tile_x = row_header_w + sum(self._col_width(i) for i in range(t_fc)) - x0
                tile_y = header_h + t_first * self._m.row_h - y0
                p.drawImage(tile_x, tile_y, img)
                drawn_bounds.add(tile_bounds)
            for tile_bounds, img in self._tile_image_cache_fallback.items():
                if tile_bounds in drawn_bounds:
                    continue
                t_first, t_last, t_fc, t_lc = tile_bounds
                if t_last < first_row or t_first > last_row or t_lc < first_col or t_fc > last_col:
                    continue
                tile_x = row_header_w + sum(self._col_width(i) for i in range(t_fc)) - x0
                tile_y = header_h + t_first * self._m.row_h - y0
                p.drawImage(tile_x, tile_y, img)
                drawn_bounds.add(tile_bounds)
            for tile_bounds, img in self._tile_plain_cache_fallback.items():
                if tile_bounds in drawn_bounds:
                    continue
                t_first, t_last, t_fc, t_lc = tile_bounds
                if t_last < first_row or t_first > last_row or t_lc < first_col or t_fc > last_col:
                    continue
                tile_x = row_header_w + sum(self._col_width(i) for i in range(t_fc)) - x0
                tile_y = header_h + t_first * self._m.row_h - y0
                p.drawImage(tile_x, tile_y, img)
                drawn_bounds.add(tile_bounds)

            custom_borders: list[tuple[QtCore.QRect, CellFormat]] = []

            # Cells (only for leaf rows - group rows have no data cells)
            for r_i in range(first_row, last_row + 1):
                if r_i >= len(self._rows):
                    continue
                row = self._rows[r_i]
                # Skip group rows entirely - they should not render any data cells
                # Only render cells for rows that are explicitly marked as leaves
                if not row.get("is_leaf", False):
                    continue
                y = header_h + r_i * self._m.row_h - y0
                col_x = row_header_w
                for c in range(first_col + 1):
                    col_x += self._col_width(c - 1) if c > 0 else 0
                for c in range(first_col, last_col + 1):
                    col_w = self._col_width(c)
                    x = col_x - x0
                    cell_r = QtCore.QRect(x, y, col_w, self._m.row_h)
                    col_x += col_w

                    # Determine if this cell is selected based on mode
                    is_sel = False
                    is_related = False
                    if self._sel_mode == "cell":
                        # Check both single cell selection and multi-cell range selection
                        is_sel = (r_i == self._sel_row and c == self._sel_col) or ((r_i, c) in self._sel_indices)
                    elif self._sel_mode == "row":
                        is_sel = r_i in self._sel_indices
                    elif self._sel_mode == "col":
                        is_sel = c in self._sel_indices
                    elif self._sel_mode == "all":
                        is_sel = True  # All cells selected when corner clicked

                    in_cached = self._cell_in_cached_tile_image(r_i, c)
                    # Selected cells are painted fresh so the cached tile's text
                    # is not double-drawn under the translucent selection overlay.
                    if in_cached and not is_sel:
                        # Always redraw gridline for cached cells so shared borders survive overlays
                        p.setPen(self._m.gridline)
                        p.drawRect(cell_r)
                        # Overlay pending value so cell never appears blank after commit
                        pending = self._pending_cell_values.get((r_i, c))
                        if pending is not None:
                            # Hardvalue styling: yellow background + red triangle
                            override_color = QtGui.QColor("#ffff99")
                            if is_sel:
                                override_color.setAlpha(160)
                            p.fillRect(cell_r.adjusted(1, 1, 0, 0), override_color)
                            tri_size = 6
                            tri = QtGui.QPolygon([
                                QtCore.QPoint(cell_r.right() - tri_size + 1, cell_r.top() + 1),
                                QtCore.QPoint(cell_r.right() + 1, cell_r.top() + 1),
                                QtCore.QPoint(cell_r.right() + 1, cell_r.top() + tri_size + 1),
                            ])
                            p.setBrush(QtGui.QColor("#ff0000"))
                            p.setPen(QtCore.Qt.PenStyle.NoPen)
                            p.drawPolygon(tri)
                            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                            # Text with formatting (same logic as non-cached paint path)
                            try:
                                fmt = self._get_cell_format(r_i, c)
                            except Exception:
                                fmt = CellFormat()
                            from lib_contracts.types import get_value_type
                            value_type = get_value_type(pending)
                            try:
                                if value_type == "null":
                                    v = self._format_null(fmt.format_null)
                                elif value_type == "error":
                                    v = str(pending) if not fmt.format_error else f"[{pending}]"
                                elif value_type == "numeric":
                                    v = "" if pending is None else str(pending)
                                    v = self._format_value(v, fmt.format_number)
                                else:
                                    v = "" if pending is None else str(pending)
                                    v = self._format_text(v, fmt.format_text)
                            except Exception:
                                v = "" if pending is None else str(pending)
                            text_pen = self._m.sel_fg if is_sel else QtGui.QColor("#202020")
                            p.setPen(text_pen)
                            font = QtGui.QFont(
                                str(fmt.font_family) if fmt.font_family else "sans-serif",
                                int(fmt.font_size) if fmt.font_size else 9,
                            )
                            p.setFont(font)
                            align = self._get_text_alignment(
                                "right" if value_type == "numeric" and fmt.text_h_align == "left" else fmt.text_h_align,
                                fmt.text_v_align,
                            )
                            p.drawText(cell_r.adjusted(4, 0, -4, 0), align, v)
                        elif is_sel:
                            # Selected cell in cached tile: redraw text in white so
                            # the cached tile's original font color doesn't show through
                            cell = visible_cells.get((r_i, c))
                            if cell is not None:
                                try:
                                    fmt = self._get_cell_format(r_i, c)
                                except Exception:
                                    fmt = CellFormat()
                                from lib_contracts.types import get_value_type
                                cell_value = cell.get("value")
                                cell_error = cell.get("error")
                                if cell_error:
                                    value_type = "error"
                                    cell_value = cell_error
                                else:
                                    value_type = get_value_type(cell_value)
                                try:
                                    if value_type == "null":
                                        v = self._format_null(fmt.format_null)
                                    elif value_type == "error":
                                        v = str(cell_value) if not fmt.format_error else f"[{cell_value}]"
                                    elif value_type == "numeric":
                                        v = "" if cell_value is None else str(cell_value)
                                        v = self._format_value(v, fmt.format_number)
                                    else:
                                        v = "" if cell_value is None else str(cell_value)
                                        v = self._format_text(v, fmt.format_text)
                                except Exception:
                                    v = "" if cell_value is None else str(cell_value)
                                p.setPen(self._m.sel_fg)
                                font = QtGui.QFont(
                                    str(fmt.font_family) if fmt.font_family else "sans-serif",
                                    int(fmt.font_size) if fmt.font_size else 9,
                                )
                                p.setFont(font)
                                align = self._get_text_alignment(
                                    "right" if value_type == "numeric" and fmt.text_h_align == "left" else fmt.text_h_align,
                                    fmt.text_v_align,
                                )
                                p.drawText(cell_r.adjusted(4, 0, -4, 0), align, v)
                        continue

                    # Get cell format (legacy) for properties not yet migrated to @ dimension rules
                    try:
                        fmt = self._get_cell_format(r_i, c)
                    except Exception:
                        fmt = CellFormat()

                    # Always paint an explicit base background for data cells.
                    # Prefer engine @.fill rule; fall back to legacy CellFormat.bg_color
                    engine_fill = visible_fills.get((r_i, c))
                    bg_color = engine_fill if engine_fill else (fmt.bg_color if fmt.bg_color else "white")
                    # Validate bg_color is a valid hex string before creating QColor
                    try:
                        if isinstance(bg_color, str) and bg_color.startswith("#") and len(bg_color) in (4, 7, 9):
                            color = QtGui.QColor(bg_color)
                        elif isinstance(bg_color, str):
                            # Named color or other string
                            color = QtGui.QColor(bg_color)
                        else:
                            color = QtGui.QColor("white")
                    except Exception:
                        color = QtGui.QColor("white")
                    p.fillRect(cell_r, color)
                    if is_sel:
                        p.fillRect(cell_r, self._m.sel_bg)
                        p.setPen(self._m.sel_fg)
                    elif is_related:
                        p.fillRect(cell_r, self._m.related_bg)
                        p.setPen(QtGui.QColor("#202020"))
                    else:
                        p.setPen(QtGui.QColor("#202020"))

                    # grid
                    p.setPen(self._m.gridline)
                    p.drawRect(cell_r)
                    if not (0 <= c < len(self._col_keys)):
                        continue
                    # background (override highlight)
                    # Use batch-fetched cell data from snapshot (no per-cell engine lookups)
                    cell = visible_cells.get((r_i, c))
                    pending = self._pending_cell_values.get((r_i, c))
                    if cell is None and pending is None:
                        _is_sys_paint = self._is_system_cube_view()
                        if _is_sys_paint and DEBUG_GUI:
                            print(
                                f"[SYS-CUBE] paintEvent SKIP r={r_i} c={c} cell=None pending=None "
                                f"in_cached={in_cached} visible_cells_count={len(visible_cells)}",
                                flush=True,
                            )
                        continue
                    if pending is not None:
                        cell = {"value": pending, "source": "override", "addr": None}
                    # Check if this is a hardnumber using cell addr from snapshot
                    cell_addr = cell.get("addr")
                    if isinstance(cell_addr, list):
                        cell_addr = tuple(cell_addr)
                    is_hardnumber = cell_addr in hardnumber_addrs if cell_addr else False
                    # Check for hard value (user override) - show yellow
                    cell_value = cell.get("value")
                    cell_source = cell.get("source")
                    is_hard_value = is_hardnumber or (cell_source == "override" and cell_value is not None)
                    if is_hard_value:
                        # Show overrides even when selected by using a translucent overlay.
                        override_color = QtGui.QColor("#ffff99")
                        if is_sel:
                            override_color.setAlpha(160)
                        p.fillRect(cell_r.adjusted(1,1,0,0), override_color)
                    
                        # Draw red triangle in top-right corner to indicate hard override
                        triangle_size = 6
                        triangle = QtGui.QPolygon([
                            QtCore.QPoint(cell_r.right() - triangle_size + 1, cell_r.top() + 1),
                            QtCore.QPoint(cell_r.right() + 1, cell_r.top() + 1),
                            QtCore.QPoint(cell_r.right() + 1, cell_r.top() + triangle_size + 1),
                        ])
                        p.setBrush(QtGui.QColor("#ff0000"))
                        p.setPen(QtCore.Qt.PenStyle.NoPen)
                        p.drawPolygon(triangle)
                        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)  # Reset brush

                    # text with formatting (auto-contrast if font_color not explicitly set)
                    engine_font_color = visible_font_colors.get((r_i, c))
                    effective_bg = visible_fills.get((r_i, c)) or fmt.bg_color or "white"
                    text_pen = self._m.sel_fg if is_sel else QtGui.QColor(
                        engine_font_color if engine_font_color else get_contrast_font_color(effective_bg)
                    )
                    p.setPen(text_pen)

                    # Determine value type and apply appropriate format
                    from lib_contracts.types import get_value_type
                    cell_error = cell.get("error")
                    if cell_error:
                        value_type = "error"
                        cell_value = cell_error
                    else:
                        value_type = get_value_type(cell_value)

                    try:
                        if value_type == "null":
                            v = self._format_null(fmt.format_null)
                        elif value_type == "error":
                            v = str(cell_value) if not fmt.format_error else f"[{cell_value}]"
                        elif value_type == "numeric":
                            v = "" if cell_value is None else str(cell_value)
                            v = self._format_value(v, fmt.format_number)
                        else:  # text
                            v = "" if cell_value is None else str(cell_value)
                            v = self._format_text(v, fmt.format_text)
                    except Exception:
                        v = "" if cell_value is None else str(cell_value)

                    # Set font
                    font = QtGui.QFont(
                        str(fmt.font_family) if fmt.font_family else "sans-serif",
                        int(fmt.font_size) if fmt.font_size else 9,
                    )
                    if fmt.font_weight:
                        try:
                            fw = int(fmt.font_weight)
                            fw = max(1, min(1000, fw))
                            font.setWeight(QtGui.QFont.Weight(fw))
                        except (ValueError, TypeError):
                            pass
                    font.setItalic(bool(fmt.font_italic))
                    p.setFont(font)

                    # Default alignment: text left, numbers right. Honour any
                    # explicit horizontal alignment set in the CellFormat; only
                    # auto-switch when still using the default "left".
                    h_align = fmt.text_h_align
                    if h_align == "left" and value_type == "numeric":
                        h_align = "right"
                    align = self._get_text_alignment(h_align, fmt.text_v_align)
                    p.drawText(cell_r.adjusted(4, 0, -4, 0), align, v)
                    if (
                        fmt.border_top != "none"
                        or fmt.border_bottom != "none"
                        or fmt.border_left != "none"
                        or fmt.border_right != "none"
                    ):
                        custom_borders.append((QtCore.QRect(cell_r), fmt))

            # Restore painter state (remove clip)
            p.restore()

            if custom_borders:
                p.save()
                p.setClipRect(content_clip)
                for cell_r, fmt in custom_borders:
                    self._draw_cell_borders(p, cell_r, fmt)
                p.restore()

            # Drag/drop hover highlight (draw last so it overlays)
            if self._drop_hover is not None:
                mode, _payload, rr = self._drop_hover
                if mode.endswith("_reorder"):
                    # Insert line: solid blue bar
                    p.fillRect(rr, QtGui.QColor(42, 118, 210, 220))
                else:
                    # "Into group" highlight: translucent blue
                    p.fillRect(rr, QtGui.QColor(42, 118, 210, 60))

            # Draw selection region lasso and anchor cell indicator
            if self._sel_mode == "cell" and self._sel_indices:
                p.save()
                p.setClipRect(content_clip)
                # Calculate bounding box of selection from cell coordinates
                min_r = min_c = float('inf')
                max_r = max_c = float('-inf')
                for idx in self._sel_indices:
                    if isinstance(idx, tuple) and len(idx) == 2:
                        r, c = idx
                        min_r = min(min_r, r)
                        max_r = max(max_r, r)
                        min_c = min(min_c, c)
                        max_c = max(max_c, c)
            
                # Check if selection is a contiguous rectangle
                expected_cells = (max_r - min_r + 1) * (max_c - min_c + 1)
                is_rectangular = len(self._sel_indices) == expected_cells
            
                if is_rectangular:
                    # Draw lasso border around the selection region
                    # Calculate pixel coordinates for the bounding box
                    header_h = self._m.col_header_h * max(1, self._col_header_levels)
                    row_header_w = self._row_header_width()
                    off = self._scroll_offset()
                    x0, y0 = off.x(), off.y()
                
                    # Calculate top-left corner
                    y_top = header_h + min_r * self._m.row_h - y0
                    col_x = row_header_w
                    for c in range(int(min_c) + 1):
                        col_x += self._col_width(c - 1) if c > 0 else 0
                    x_left = col_x - x0
                
                    # Calculate width and height
                    width = sum(self._col_width(c) for c in range(int(min_c), int(max_c) + 1))
                    height = (max_r - min_r + 1) * self._m.row_h
                
                    # Draw thick border around selection region
                    lasso_rect = QtCore.QRect(x_left, y_top, width, height)
                    pen = QtGui.QPen(QtGui.QColor(42, 118, 210), 2)  # 2px blue border
                    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.MiterJoin)  # Sharp corners
                    p.setPen(pen)
                    p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                    p.drawRect(lasso_rect)
                p.restore()
        
            # Draw current cell indicator (thick border) - this is the active cell for editing/navigation
            if self._sel_mode == "cell":
                p.save()
                p.setClipRect(content_clip)
                # Calculate pixel coordinates for current cell
                header_h = self._m.col_header_h * max(1, self._col_header_levels)
                row_header_w = self._row_header_width()
                off = self._scroll_offset()
                x0, y0 = off.x(), off.y()
            
                # Check if current cell is visible
                if 0 <= self._sel_row < len(self._rows) and 0 <= self._sel_col < len(self._cols):
                    current_visual_r = self._geometry.visual_row_for_leaf(self._sel_row)

                    if current_visual_r is not None:
                        y_current = header_h + current_visual_r * self._m.row_h - y0
                    
                        # Calculate x position for current column
                        col_x = row_header_w
                        for c in range(self._sel_col + 1):
                            col_x += self._col_width(c - 1) if c > 0 else 0
                        x_current = col_x - x0
                    
                        col_w = self._col_width(self._sel_col)
                        current_rect = QtCore.QRect(x_current, y_current, col_w, self._m.row_h)
                    
                        # Draw border around current cell (2px dark gray, sharp corners)
                        pen = QtGui.QPen(QtGui.QColor(80, 80, 80), 2)  # 2px dark gray border
                        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.MiterJoin)  # Sharp corners
                        p.setPen(pen)
                        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                        p.drawRect(current_rect)
                p.restore()

        finally:
            p.end()
            if self._pending_repaint_tag is not None:
                DEBUG_GUI and print(
                    "DEBUG repaint_painted:"
                    f" tag={self._pending_repaint_tag}"
                    f" updatesEnabled={self.viewport().updatesEnabled()}"
                )
                self._pending_repaint_tag = None

    # ------------------------------------------------------------
    # Hit testing
    # ------------------------------------------------------------

    def _cell_at(self, pos: QtCore.QPoint) -> tuple[int, int] | None:
        """Get cell coordinates at a given point position."""
        return self._navigation.cell_at(pos)

    def _cell_rect(self, r: int, c: int) -> QtCore.QRect:
        """Get the rectangle for a cell at (r, c)."""
        return self._navigation.cell_rect(r, c)

    def _row_header_rect(self, r: int) -> QtCore.QRect:
        """Get the rectangle for a row header at row r."""
        return self._navigation.row_header_rect(r)

    def _leaf_row_index(self, display_row: int) -> int:
        """Map visible display row -> leaf index in _row_keys (group rows don't count)."""
        return self._geometry.leaf_row_index(display_row)

    # ------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------

    def event(self, event: QtCore.QEvent) -> bool:
        """Override event to catch Delete key and handle debug tooltips."""
        if event.type() == QtCore.QEvent.Type.KeyPress:
            key_event = event
            key = key_event.key()
            if key == QtCore.Qt.Key.Key_Delete:
                self.keyPressEvent(key_event)
                return True
        elif event.type() == QtCore.QEvent.Type.ToolTip:
            # Handle debug tooltips
            tooltip_event = event
            pos = tooltip_event.pos()
            tooltip_text = self._get_debug_tooltip(pos)
            if tooltip_text:
                QtWidgets.QToolTip.showText(tooltip_event.globalPos(), tooltip_text, self)
            else:
                QtWidgets.QToolTip.hideText()
            return True
        return super().event(event)

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if obj == self._editor and self._suppress_editor_event_filter:
            return False
        if obj == self._editor and self._edit_mode == "navigation":
            if event.type() == QtCore.QEvent.Type.FocusIn:
                # If this grid is hidden, swallow the editor FocusIn event.
                # Letting it through causes Qt to auto-switch to this tab.
                # The singleShot guard below is backed by setFocus() isVisible().
                if not self.isVisible():
                    return True
                grid = self
                QtCore.QTimer.singleShot(0, lambda: grid.setFocus(QtCore.Qt.FocusReason.OtherFocusReason) if grid.isVisible() else None)
                return True
            if event.type() == QtCore.QEvent.Type.KeyPress:
                key = event.key()
                if key in (
                    QtCore.Qt.Key.Key_Left,
                    QtCore.Qt.Key.Key_Right,
                    QtCore.Qt.Key.Key_Up,
                    QtCore.Qt.Key.Key_Down,
                ):
                    self._post_grid_key_event(event)
                    return True
        if obj == self._editor and event.type() == QtCore.QEvent.Type.FocusOut:
            self._debug_edit_state("editor_focus_out")
        if obj == self._editor and event.type() == QtCore.QEvent.Type.FocusIn:
            if self._header_edit_ctx is not None and not self._editor.isVisible():
                self._ensure_label_editor_visible_from_ctx("focus_in_hidden")
            self._debug_edit_state("editor_focus_in")
        if obj == self._editor and event.type() == QtCore.QEvent.Type.Hide:
            self._debug_edit_state("editor_hide")
        if obj == self._editor and event.type() == QtCore.QEvent.Type.Show:
            self._debug_edit_state("editor_show")
        if obj == self._editor and event.type() == QtCore.QEvent.Type.ShortcutOverride:
            if self._header_edit_ctx is not None and not self._editor.isVisible():
                self._ensure_label_editor_visible_from_ctx("shortcut_override_hidden")
            key_event = event
            key = key_event.key()
            if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                self._debug_edit_state("editor_shortcut_override_enter")
                event.accept()
                return True
        if obj == self._editor and event.type() == QtCore.QEvent.Type.KeyPress:
            if self._header_edit_ctx is not None and not self._editor.isVisible():
                self._ensure_label_editor_visible_from_ctx("keypress_hidden")
            key_event = event
            key = key_event.key()
            self._debug_edit_state(f"editor_keypress key={int(key)}")
            
            # Handle cursor keys during label editing with hierarchical navigation
            if self._header_edit_ctx is not None and key in (
                QtCore.Qt.Key.Key_Left,
                QtCore.Qt.Key.Key_Right,
                QtCore.Qt.Key.Key_Up,
                QtCore.Qt.Key.Key_Down,
            ) and not (
                key_event.modifiers()
                & (
                    QtCore.Qt.KeyboardModifier.ControlModifier
                    | QtCore.Qt.KeyboardModifier.ShiftModifier
                    | QtCore.Qt.KeyboardModifier.AltModifier
                    | QtCore.Qt.KeyboardModifier.MetaModifier
                )
            ):
                axis = str(self._header_edit_ctx.get("axis") or "")
                
                # Row labels: Left=parent, Right=child, Up/Down=siblings
                if axis == "row":
                    if key == QtCore.Qt.Key.Key_Left:
                        if self._should_navigate_on_arrow(key):
                            # Navigate to parent group
                            parent = self._get_parent_header("row")
                            if parent:
                                self._debug_edit_state("editor_arrow_route=navigate_to_parent")
                                self._commit_and_navigate_to(parent)
                                return True
                        # Let default handler move cursor within text
                        return False
                    elif key == QtCore.Qt.Key.Key_Right:
                        if self._should_navigate_on_arrow(key):
                            # Navigate to first child
                            child = self._get_first_child_header("row")
                            if child:
                                self._debug_edit_state("editor_arrow_route=navigate_to_child")
                                self._commit_and_navigate_to(child)
                                return True
                        # Let default handler move cursor within text
                        return False
                    elif key == QtCore.Qt.Key.Key_Up:
                        if self._should_navigate_on_arrow(key):
                            # Navigate to previous sibling
                            self._debug_edit_state("editor_arrow_route=label_commit_prev")
                            self._commit_header_editor(move_prev=True)
                            return True
                        # Let default handler move cursor within text
                        return False
                    elif key == QtCore.Qt.Key.Key_Down:
                        if self._should_navigate_on_arrow(key):
                            # Navigate to next sibling
                            self._debug_edit_state("editor_arrow_route=label_commit_next")
                            self._commit_header_editor(move_next=True)
                            return True
                        # Let default handler move cursor within text
                        return False
                
                # Column labels: Up=parent, Down=child, Left/Right=siblings
                elif axis == "col":
                    if key == QtCore.Qt.Key.Key_Up:
                        if self._should_navigate_on_arrow(key):
                            # Navigate to parent group
                            parent = self._get_parent_header("col")
                            if parent:
                                self._debug_edit_state("editor_arrow_route=navigate_to_parent")
                                self._commit_and_navigate_to(parent)
                                return True
                        # Let default handler move cursor within text
                        return False
                    elif key == QtCore.Qt.Key.Key_Down:
                        if self._should_navigate_on_arrow(key):
                            # Navigate to first child
                            child = self._get_first_child_header("col")
                            if child:
                                self._debug_edit_state("editor_arrow_route=navigate_to_child")
                                self._commit_and_navigate_to(child)
                                return True
                        # Let default handler move cursor within text
                        return False
                    elif key == QtCore.Qt.Key.Key_Left:
                        if self._should_navigate_on_arrow(key):
                            # Navigate to previous sibling
                            self._debug_edit_state("editor_arrow_route=label_commit_prev")
                            self._commit_header_editor(move_prev=True)
                            return True
                        # Let default handler move cursor within text
                        return False
                    elif key == QtCore.Qt.Key.Key_Right:
                        if self._should_navigate_on_arrow(key):
                            # Navigate to next sibling
                            self._debug_edit_state("editor_arrow_route=label_commit_next")
                            self._commit_header_editor(move_next=True)
                            return True
                        # Let default handler move cursor within text
                        return False
                
                return True
            
            # Handle cursor keys during cell editing
            if self._header_edit_ctx is None and key in (
                QtCore.Qt.Key.Key_Left,
                QtCore.Qt.Key.Key_Right,
                QtCore.Qt.Key.Key_Up,
                QtCore.Qt.Key.Key_Down,
            ) and not (
                key_event.modifiers()
                & (
                    QtCore.Qt.KeyboardModifier.ControlModifier
                    | QtCore.Qt.KeyboardModifier.ShiftModifier
                    | QtCore.Qt.KeyboardModifier.AltModifier
                    | QtCore.Qt.KeyboardModifier.MetaModifier
                )
            ):
                self._debug_edit_state("editor_arrow_route=cell_commit_navigate")
                self._commit_editor()
                self._post_grid_key_event(key_event)
                return True
            if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                self._ensure_header_edit_ctx_from_editor()
                if self._header_edit_ctx is not None:
                    self._debug_edit_state("editor_enter_route=header_commit")
                    self._commit_header_editor(move_next=True)
                else:
                    # Check for Ctrl+Enter to fill all selected cells
                    modifiers = key_event.modifiers()
                    ctrl_pressed = bool(modifiers & QtCore.Qt.KeyboardModifier.ControlModifier)
                    if ctrl_pressed and self._sel_mode == "cell":
                        has_multi_selection = (
                            self._sel_indices and len(self._sel_indices) > 1
                        ) or (
                            (self._sel_row, self._sel_col) not in self._sel_indices
                            if self._sel_indices else False
                        )
                        if has_multi_selection:
                            self._debug_edit_state("editor_enter_route=cell_commit_fill_selection")
                            self._commit_editor(fill_selection=True)
                        else:
                            self._debug_edit_state("editor_enter_route=cell_commit")
                            self._commit_editor()
                    else:
                        self._debug_edit_state("editor_enter_route=cell_commit")
                        self._commit_editor()
                    # After commit, move selection down one cell (like in nav mode)
                    if self._rows:
                        nr = self._sel_row + 1
                        # Skip non-leaf rows
                        while nr < len(self._rows) and not self._rows[nr].get("is_leaf", False):
                            nr += 1
                        if nr < len(self._rows):
                            self._sel_row = nr
                            self._clamp_selection_to_leaf()
                            self._sel_mode = "cell"
                            self._sel_indices.clear()
                            self._anchor_row, self._anchor_col = self._sel_row, self._sel_col
                            self.selection_changed.emit()
                            self.viewport().update()
                            self._request_repaint("enter_move_down_after_commit")
                            self._ensure_visible(self._sel_row, self._sel_col)
                return True
            if key == QtCore.Qt.Key.Key_Escape:
                self._cancel_edit()
                DEBUG_GUI and print(f"DEBUG SET CELL: line {__import__('inspect').currentframe().f_lineno} prev={self._sel_mode}"); self._sel_mode = "cell"
                self._sel_indices.clear()
                self._edit_mode = "navigation"
                return True
            self._send_editor_key_event(event)
            return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        super().mousePressEvent(event)
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return

        # Check if starting a row header resize
        resize_row_level = self._get_resize_row_level(event.position().toPoint())
        if resize_row_level is not None:
            self._resize_row_level = resize_row_level
            self._resize_start_x = event.position().toPoint().x()
            self._resize_start_width_row = self._row_header_level_width(resize_row_level)
            return

        # Check if starting a column resize
        resize_col = self._get_resize_col(event.position().toPoint())
        if resize_col is not None:
            self._resize_col = resize_col
            self._resize_start_x = event.position().toPoint().x()
            self._resize_start_width = self._col_width(resize_col)
            return

        self._drag_start_pos = event.position().toPoint()
        self._drag_item_id = None
        self._drag_group_path = None
        self._drag_group_level = None
        self._drag_group_first = None
        self._drag_group_last = None
        self._drag_axis = None
        
        ctrl_held = bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier)
        shift_held = bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
        
        # Click in the very top-left corner (above row headers and left of
        # column headers): select the entire grid of data cells.
        x = event.position().toPoint().x()
        y = event.position().toPoint().y()
        header_h = self._m.col_header_h * max(1, self._col_header_levels)
        row_header_w = self._row_header_width()
        if x < row_header_w and y < header_h:
            total_rows = len(self._rows)
            total_cols = len(self._cols)
            if total_rows and total_cols:
                self._sel_mode = "all"
                self._sel_indices.clear()  # Empty = all visible cells
                first_leaf_r: int | None = None
                for r in range(total_rows):
                    if self._rows[r].get("is_leaf", False):
                        first_leaf_r = r
                        break
                if first_leaf_r is None:
                    first_leaf_r = 0
                self._sel_row = first_leaf_r
                self._sel_col = 0
                self._sel_group_path = None
                self._anchor_row, self._anchor_col = self._sel_row, self._sel_col
                if self.isVisible():
                    self.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
                self._hide_editor()
                self._clamp_selection_to_leaf()
                self.selection_changed.emit()
                self.viewport().update()
            return

        hh = self._header_hit(event.position().toPoint())
        if hh is not None:
            kind, payload = hh
            if kind == "row_leaf" and (isinstance(payload, tuple) or isinstance(payload, str)):
                # payload may be (iid, row_idx) or legacy iid
                if isinstance(payload, tuple) and len(payload) == 2:
                    iid, row_idx = payload
                else:
                    iid, row_idx = payload, None
                if row_idx is None or not isinstance(row_idx, int):
                    # Fallback to first match
                    for j, row in enumerate(self._rows):
                        if row.get("item_id") == iid:
                            row_idx = j
                            break
                if row_idx is None or not (0 <= row_idx < len(self._rows)):
                    return
                self._drag_axis = "row"
                self._drag_item_id = iid if isinstance(iid, str) else None
                if shift_held and self._sel_mode == "row":
                    # Extend selection from existing bounds to clicked row
                    if self._sel_indices:
                        current_min = min(self._sel_indices)
                        current_max = max(self._sel_indices)
                        # Extend to include the gap between current selection and new row
                        if row_idx < current_min:
                            r0, r1 = row_idx, current_max
                        elif row_idx > current_max:
                            r0, r1 = current_min, row_idx
                        else:
                            r0, r1 = current_min, current_max
                    else:
                        r0, r1 = min(self._sel_row, row_idx), max(self._sel_row, row_idx)
                    self._sel_mode = "row"
                    self._sel_indices = set(range(r0, r1 + 1))
                    self._sel_row = row_idx
                elif ctrl_held:
                    if self._sel_mode == "col":
                        # Cross-mode: add all cells at intersection of selected columns and this row
                        # OPTIMIZED: Don't expand to all cells - use generator pattern
                        # Store as combined selection that can be iterated lazily
                        self._sel_mode = "cell"
                        # Keep column indices but mark as needing expansion
                        # The _col_sel_indices stores which columns are selected
                        self._col_sel_indices = set(self._sel_indices)
                        self._row_sel_indices = {row_idx}
                        # Only add visible cells to _sel_indices for immediate painting
                        first_row = self.verticalScrollBar().value() // self._m.row_h
                        last_row = first_row + self.viewport().height() // self._m.row_h + 2
                        cell_selection: set[tuple[int, int]] = set()
                        for c in self._sel_indices:
                            if 0 <= c < len(self._cols):
                                for r in range(max(0, first_row), min(len(self._rows), last_row)):
                                    if self._rows[r].get("is_leaf", False):
                                        cell_selection.add((r, c))
                        # Add the newly selected row cells
                        if 0 <= row_idx < len(self._rows):
                            for c in range(len(self._cols)):
                                if self._cols[c].get("is_leaf", False):
                                    cell_selection.add((row_idx, c))
                        self._sel_indices = cell_selection
                        self._sel_row = row_idx
                    elif self._sel_mode == "cell":
                        # Add all cells in this row to existing cell selection
                        for c in range(len(self._cols)):
                            if self._cols[c].get("is_leaf", False):
                                self._sel_indices.add((row_idx, c))
                        self._sel_row = row_idx
                    else:
                        # Already in row mode, toggle this row
                        if row_idx in self._sel_indices:
                            self._sel_indices.discard(row_idx)
                        else:
                            self._sel_indices.add(row_idx)
                        self._sel_row = row_idx
                    self._sel_col = 0
                else:
                    # Normal click: if clicking on already-selected row, preserve selection for drag
                    # Otherwise, reset to single row selection
                    if self._sel_mode == "row" and row_idx in self._sel_indices and len(self._sel_indices) > 1:
                        # Preserve multi-selection - just update the active row
                        self._sel_row = row_idx
                    else:
                        self._sel_mode = "row"
                        self._sel_indices = {row_idx}
                        self._sel_row = row_idx
                self._sel_group_path = None
                self._hide_editor()
                self.selection_changed.emit()
                self.viewport().update()
                return
            elif kind == "col_leaf" and (isinstance(payload, int) or isinstance(payload, str)):
                # payload may be column index (int) or item_id (str); resolve to column index
                col_idx = -1
                if isinstance(payload, int):
                    col_idx = payload
                else:
                    for i, col in enumerate(self._cols):
                        if col.get("item_id") == payload:
                            col_idx = i
                            break
                if not (0 <= col_idx < len(self._cols)):
                    DEBUG_GUI and print(f"DEBUG col_leaf click: invalid col_idx={col_idx} payload={payload}")
                    return

                self._drag_axis = "col"
                self._drag_item_id = self._cols[col_idx].get("item_id") if col_idx < len(self._cols) else None
                DEBUG_GUI and print(f"DEBUG col_leaf click: col_idx={col_idx}, item_id={self._drag_item_id}, shift={shift_held}, ctrl={ctrl_held}")

                # Clicking col header: select entire column (reset selection unless shift/ctrl says otherwise)
                i = col_idx
                if shift_held and self._sel_mode == "col":
                    # Extend selection from existing bounds to clicked column
                    if self._sel_indices:
                        current_min = min(self._sel_indices)
                        current_max = max(self._sel_indices)
                        # Extend to include the gap between current selection and new column
                        if i < current_min:
                            c0, c1 = i, current_max
                        elif i > current_max:
                            c0, c1 = current_min, i
                        else:
                            c0, c1 = current_min, current_max
                    else:
                        c0, c1 = min(self._sel_col, i), max(self._sel_col, i)
                    self._sel_mode = "col"
                    self._sel_indices = set(range(c0, c1 + 1))
                    self._sel_col = i
                    DEBUG_GUI and print(f"DEBUG col_leaf shift-select: range {c0}-{c1}")
                elif ctrl_held:
                    if self._sel_mode == "row":
                        # Cross-mode: convert row selection to cell selection, add column cells
                        # OPTIMIZED: Store as combined selection, only expand visible cells
                        self._sel_mode = "cell"
                        self._row_sel_indices = set(self._sel_indices)
                        self._col_sel_indices = {i}
                        # Only expand visible cells for immediate painting
                        cell_selection: set[tuple[int, int]] = set()
                        first_row = self.verticalScrollBar().value() // self._m.row_h
                        last_row = first_row + self.viewport().height() // self._m.row_h + 2
                        for r in self._sel_indices:
                            if 0 <= r < len(self._rows):
                                for c in range(len(self._cols)):
                                    if self._cols[c].get("is_leaf", False):
                                        cell_selection.add((r, c))
                        if 0 <= i < len(self._cols):
                            for r in range(max(0, first_row), min(len(self._rows), last_row)):
                                if self._rows[r].get("is_leaf", False):
                                    cell_selection.add((r, i))
                        self._sel_indices = cell_selection
                    elif self._sel_mode == "cell":
                        # Add column cells to existing cell selection - OPTIMIZED
                        self._col_sel_indices = getattr(self, '_col_sel_indices', set()) | {i}
                        first_row = self.verticalScrollBar().value() // self._m.row_h
                        last_row = first_row + self.viewport().height() // self._m.row_h + 2
                        for r in range(max(0, first_row), min(len(self._rows), last_row)):
                            if self._rows[r].get("is_leaf", False):
                                self._sel_indices.add((r, i))
                    else:
                        # Already in col mode, toggle this column
                        if i in self._sel_indices:
                            self._sel_indices.discard(i)
                        else:
                            self._sel_indices.add(i)
                    self._sel_col = i
                else:
                    # Normal click: if clicking on already-selected column, preserve selection for drag
                    # Otherwise, reset to single column selection
                    if self._sel_mode == "col" and i in self._sel_indices and len(self._sel_indices) > 1:
                        # Preserve multi-selection - just update the active column
                        self._sel_col = i
                    else:
                        self._sel_mode = "col"
                        self._sel_indices = {i}
                        self._sel_col = i
                    # Clear cell row selection to avoid confusion
                    self._sel_row = 0
                self._sel_group_path = None
                self._hide_editor()
                self.selection_changed.emit()
                self.viewport().update()
                return
            elif kind == "row_group" and isinstance(payload, tuple):
                # payload may be (path, r0, r1, clicked_r) or legacy (path, r0, r1) or (path)
                band_path: tuple[int, ...] | None = None
                r0_span = r1_span = None
                clicked_r = None
                if len(payload) == 4 and isinstance(payload[0], tuple):
                    band_path = payload[0]
                    r0_span = payload[1]
                    r1_span = payload[2]
                    clicked_r = payload[3]
                elif len(payload) == 3 and isinstance(payload[0], tuple):
                    band_path = payload[0]
                    r0_span = payload[1]
                    r1_span = payload[2]
                elif len(payload) >= 1 and isinstance(payload[0], int):
                    band_path = payload

                print(f"[DEBUG row_group] payload={payload}, extracted band_path={band_path}, r0={r0_span}, r1={r1_span}, clicked_r={clicked_r}")

                # Setup group drag state (Phase 8)
                self._drag_is_group = False
                self._drag_group_node_id = None
                self._group_drag_ready = False
                self._group_drag_highlight_rows.clear()
                self._group_drag_anchor_band_path = None
                self._group_drag_badge_count = 0
                if self._group_drag_timer is not None and self._group_drag_timer.isActive():
                    self._group_drag_timer.stop()
                self._group_drag_timer = None

                # For stacked row dimensions without an outline, treat this band as a
                # higher-level dimension header so we can reorder that dimension.
                self._drag_group_level = None
                self._drag_group_first = None
                self._drag_group_last = None
                try:
                    view = self._workspace_read_model.get_view(self._view_id)
                    row_dim_ids = list(view.get("row_dim_ids", []) or []) if view else []
                    if isinstance(band_path, tuple) and len(band_path) == 1 and len(row_dim_ids) > 1 and r0_span is not None and r1_span is not None:
                        dim_level = band_path[0]
                        if isinstance(dim_level, int) and 0 <= dim_level < len(row_dim_ids):
                            dim = self._workspace_read_model.get_dimension(row_dim_ids[dim_level])
                            outline = list(dim.get("outline", []) if dim else [])
                            if not outline:
                                self._drag_group_level = dim_level
                                self._drag_group_first = r0_span
                                self._drag_group_last = r1_span
                                DEBUG_GUI and print(f"DEBUG stacked_row_band drag: level={dim_level} first={r0_span} last={r1_span} dim_id={row_dim_ids[dim_level]}")
                except Exception:
                    # Fallback: keep drag_group_level as None if anything fails.
                    self._drag_group_level = None
                    self._drag_group_first = None
                    self._drag_group_last = None
                # --- Phase 8: outline group drag setup ---
                if (
                    band_path is not None
                    and self._drag_group_level is None
                    and not shift_held
                    and not ctrl_held
                ):
                    outline = self._outline_root("row")
                    group_node_id = resolve_group_node_id(outline, band_path)
                    if group_node_id:
                        self._drag_is_group = True
                        self._drag_group_path = band_path
                        self._drag_group_node_id = group_node_id
                        self._drag_axis = "row"
                        self._drag_start_pos = event.position().toPoint()
                        self._group_drag_anchor_band_path = band_path
                        # Compute descendant closure for visual highlighting
                        subtree_set, descendant_set, leaf_item_ref_set = get_descendant_sets(
                            outline, band_path
                        )
                        # Map ITEM_REF node_ids to row indices for cell highlighting
                        item_ids_in_closure: set[str] = set()
                        for node in outline:
                            def _collect(n):
                                if n.item_id and getattr(n, "node_id", None) in leaf_item_ref_set:
                                    item_ids_in_closure.add(n.item_id)
                                for c in n.children:
                                    _collect(c)
                            _collect(node)
                        for r_i, row in enumerate(self._rows):
                            if row.get("item_id") in item_ids_in_closure:
                                self._group_drag_highlight_rows.add(r_i)
                        # Badge count for collapsed groups
                        self._group_drag_badge_count = count_leaf_descendants(outline, band_path)
                        # Start 200 ms drag timer
                        timer = QtCore.QTimer(self)
                        timer.setSingleShot(True)
                        timer.timeout.connect(self._on_group_drag_timer)
                        self._group_drag_timer = timer
                        timer.start(500)
                        DEBUG_GUI and print(
                            f"DEBUG group_drag setup: node_id={group_node_id}, "
                            f"badge={self._group_drag_badge_count}, "
                            f"highlight_rows={len(self._group_drag_highlight_rows)}"
                        )

                DEBUG_GUI and print(f"DEBUG row_group click: payload={payload}, path={band_path}, r0={r0_span}, r1={r1_span}, clicked_r={clicked_r}, shift={shift_held}, ctrl={ctrl_held}")
                # Select all items under this group (including nested groups)
                group_indices = set()
                if r0_span is not None and r1_span is not None and r0_span >= 0 and r1_span >= r0_span:
                    for i in range(r0_span, r1_span + 1):
                        if 0 <= i < len(self._rows) and self._rows[i].get("is_leaf", False):
                            group_indices.add(i)
                    print(f"[DEBUG row_group] Using band span: r0={r0_span}, r1={r1_span}, group_indices={sorted(group_indices)}")
                if not group_indices and band_path is not None:
                    for i, row in enumerate(self._rows):
                        row_path = row.get("path")
                        if isinstance(row_path, tuple) and len(row_path) >= len(band_path):
                            if row_path[:len(band_path)] == band_path and row.get("is_leaf", False):
                                group_indices.add(i)
                    print(f"[DEBUG row_group] Fallback path match: group_indices={sorted(group_indices)}")
                if group_indices:
                    if ctrl_held:
                        # Ctrl+click: add/remove group items to/from selection
                        if self._sel_mode != "row":
                            self._sel_mode = "row"
                            self._sel_indices = group_indices
                        else:
                            if group_indices.issubset(self._sel_indices):
                                self._sel_indices -= group_indices
                            else:
                                self._sel_indices |= group_indices
                    else:
                        self._sel_mode = "row"
                        self._sel_indices = group_indices
                    if clicked_r is not None and clicked_r in group_indices:
                        self._sel_row = clicked_r
                    else:
                        self._sel_row = min(self._sel_indices) if self._sel_indices else 0
                    self._sel_group_path = ("row", band_path)
                    DEBUG_GUI and print(f"DEBUG row_group apply: sel_indices={sorted(self._sel_indices)}, sel_row={self._sel_row}")
                    self._hide_editor()
                    self.selection_changed.emit()
                    self.viewport().update()
                    return
                return
            elif kind == "col_group" and isinstance(payload, tuple) and len(payload) > 1:
                # payload may be (path) or (path, c0, c1)
                band_path: tuple[int, ...] | None = None
                c0 = c1 = None
                if len(payload) == 3 and isinstance(payload[0], tuple):
                    band_path = payload[0]
                    c0 = payload[1]
                    c1 = payload[2]
                elif len(payload) >= 1 and isinstance(payload[0], int):
                    # legacy path-only tuple
                    band_path = payload

                # Setup group drag state (Phase 8)
                self._drag_is_group = False
                self._drag_group_node_id = None
                self._group_drag_ready = False
                self._group_drag_highlight_rows.clear()
                self._group_drag_anchor_band_path = None
                self._group_drag_badge_count = 0
                if self._group_drag_timer is not None and self._group_drag_timer.isActive():
                    self._group_drag_timer.stop()
                self._group_drag_timer = None

                # For stacked column dimensions without an outline, treat this band as a
                # higher-level dimension header so we can reorder that dimension.
                self._drag_group_level = None
                self._drag_group_first = None
                self._drag_group_last = None
                try:
                    view = self._workspace_read_model.get_view(self._view_id)
                    col_dim_ids = list(view.get("col_dim_ids", []) or []) if view else []
                    if isinstance(band_path, tuple) and len(band_path) == 1 and len(col_dim_ids) > 1 and c0 is not None and c1 is not None:
                        dim_level = band_path[0]
                        if isinstance(dim_level, int) and 0 <= dim_level < len(col_dim_ids):
                            dim = self._workspace_read_model.get_dimension(col_dim_ids[dim_level])
                            outline = list(dim.get("outline", []) if dim else [])
                            if not outline:
                                self._drag_group_level = dim_level
                                self._drag_group_first = c0
                                self._drag_group_last = c1
                                DEBUG_GUI and print(f"DEBUG stacked_col_band drag: level={dim_level} first={c0} last={c1} dim_id={col_dim_ids[dim_level]}")
                except Exception:
                    self._drag_group_level = None
                    self._drag_group_first = None
                    self._drag_group_last = None
                # --- Phase 8: outline group drag setup ---
                if (
                    band_path is not None
                    and self._drag_group_level is None
                    and not shift_held
                    and not ctrl_held
                ):
                    outline = self._outline_root("col")
                    group_node_id = resolve_group_node_id(outline, band_path)
                    if group_node_id:
                        self._drag_is_group = True
                        self._drag_group_path = band_path
                        self._drag_group_node_id = group_node_id
                        self._drag_axis = "col"
                        self._drag_start_pos = event.position().toPoint()
                        self._group_drag_anchor_band_path = band_path
                        # Badge count for collapsed groups
                        self._group_drag_badge_count = count_leaf_descendants(outline, band_path)
                        # Start 200 ms drag timer
                        timer = QtCore.QTimer(self)
                        timer.setSingleShot(True)
                        timer.timeout.connect(self._on_group_drag_timer)
                        self._group_drag_timer = timer
                        timer.start(500)
                        DEBUG_GUI and print(
                            f"DEBUG col_group_drag setup: node_id={group_node_id}, "
                            f"badge={self._group_drag_badge_count}"
                        )
                DEBUG_GUI and print(f"DEBUG col_group click: payload={payload}, path={band_path}, c0={c0}, c1={c1}, shift={shift_held}, ctrl={ctrl_held}")
                # Select all items under this group (including nested groups)
                group_indices = set()
                # Prefer explicit band span when provided (more reliable than path-only when paths collapse)
                if c0 is not None and c1 is not None and c0 >= 0 and c1 >= c0:
                    for i in range(c0, c1 + 1):
                        if 0 <= i < len(self._cols) and self._cols[i].get("is_leaf", False):
                            group_indices.add(i)
                    DEBUG_GUI and print(f"DEBUG col_group using band span: c0={c0}, c1={c1}, group_indices={sorted(group_indices)}")
                if not group_indices and band_path is not None:
                    for i, col in enumerate(self._cols):
                        col_path = col.get("path")
                        if isinstance(col_path, tuple) and len(col_path) >= len(band_path):
                            if col_path[:len(band_path)] == band_path and col.get("is_leaf", False):
                                group_indices.add(i)
                    DEBUG_GUI and print(f"DEBUG col_group path match: group_indices={sorted(group_indices)}")
                if group_indices:
                    if ctrl_held:
                        # Ctrl+click: add/remove group items to/from selection
                        if self._sel_mode != "col":
                            self._sel_mode = "col"
                            self._sel_indices = group_indices
                        else:
                            # Toggle: if all items in group are selected, remove them; otherwise add them
                            if group_indices.issubset(self._sel_indices):
                                self._sel_indices -= group_indices
                            else:
                                self._sel_indices |= group_indices
                    else:
                        # Normal click: select all children of this group
                        self._sel_mode = "col"
                        self._sel_indices = group_indices
                    self._sel_col = min(self._sel_indices) if self._sel_indices else 0
                    self._sel_group_path = ("col", band_path)
                    DEBUG_GUI and print(f"DEBUG col_group apply: sel_indices={sorted(self._sel_indices)}")
                    self._hide_editor()
                    self.selection_changed.emit()
                    self.viewport().update()
                    return
                return
            elif kind in {"row_bg", "col_bg"}:
                # Clicked empty area in header — clear group selection to prevent
                # stale group outline from persisting.
                self._sel_group_path = None
                self._drag_item_id = None
                self._drag_group_path = None
                self._drag_axis = None
                self.viewport().update()
                return

        hit = self._cell_at(event.position().toPoint())
        if hit is None:
            return
        r, c = hit
        
        # Clear header drag variables since we're clicking on a cell, not a header
        self._drag_item_id = None
        self._drag_group_path = None
        self._drag_axis = None
        self._sel_group_path = None
        
        if shift_held and self._sel_mode == "cell":
            # Shift+click: select range from anchor to current cell
            r0, r1 = min(self._sel_row, r), max(self._sel_row, r)
            c0, c1 = min(self._sel_col, c), max(self._sel_col, c)
            self._sel_indices.clear()
            for ri in range(r0, r1 + 1):
                for ci in range(c0, c1 + 1):
                    self._sel_indices.add((ri, ci))
            print(f"[DEBUG shift+click] range=({r0},{c0}) to ({r1},{c1}), selected {len(self._sel_indices)} cells: {sorted(self._sel_indices)[:5]}...")
            # Keep anchor point, update current position
            self._sel_row, self._sel_col = r, c
        else:
            # Remember previous active cell so Ctrl+click can extend from it.
            prev_row, prev_col = self._sel_row, self._sel_col
            self._sel_row, self._sel_col = r, c
            # Only clear multi-selection if Ctrl is not held
            if not ctrl_held:
                DEBUG_GUI and print(f"DEBUG SET CELL: line {__import__('inspect').currentframe().f_lineno} prev={self._sel_mode}"); self._sel_mode = "cell"
                self._sel_indices.clear()
            else:
                # Ctrl+click on cell: extend selection by adding the clicked cell
                if self._sel_mode != "cell":
                    DEBUG_GUI and print(f"DEBUG SET CELL: line {__import__('inspect').currentframe().f_lineno} prev={self._sel_mode}"); self._sel_mode = "cell"
                    self._sel_indices.clear()
                # Add the clicked cell to selection (never deselect)
                cell_key = (r, c)
                self._sel_indices.add(cell_key)
                print(f"[DEBUG ctrl+click] added cell ({r},{c}) to selection, now {len(self._sel_indices)} cells selected")
                # Ensure the prior active cell is also part of the selection
                if 0 <= prev_row < len(self._rows) and 0 <= prev_col < len(self._cols):
                    self._sel_indices.add((prev_row, prev_col))
            # Update anchor when not extending via Shift
            if not shift_held:
                self._anchor_row, self._anchor_col = self._sel_row, self._sel_col

        # Ensure the grid receives focus for keyboard actions (e.g., Delete)
        if self.isVisible():
            self.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        self._hide_editor()
        self._clamp_selection_to_leaf()
        self.selection_changed.emit()
        self.viewport().update()
        # Phase 5D: write local cache to SessionStore source of truth.
        self._write_selection_to_session()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        super().mouseMoveEvent(event)
        
        # Show debug tooltip immediately if enabled (no delay)
        if getattr(self, '_debug_tooltips_enabled', False):
            pos = event.position().toPoint()
            tooltip_text = self._get_debug_tooltip(pos)
            if tooltip_text:
                QtWidgets.QToolTip.showText(event.globalPosition().toPoint(), tooltip_text, self)
            else:
                QtWidgets.QToolTip.hideText()
        
        # Update cursor when hovering over resize edge
        if not (event.buttons() & QtCore.Qt.MouseButton.LeftButton):
            resize_row_level = self._get_resize_row_level(event.position().toPoint())
            resize_col = self._get_resize_col(event.position().toPoint())
            if resize_row_level is not None or resize_col is not None:
                self.viewport().setCursor(QtCore.Qt.CursorShape.SplitHCursor)
            else:
                self.viewport().setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            return
        
        # Handle row header resizing
        if self._resize_row_level is not None and self._resize_start_x is not None and self._resize_start_width_row is not None:
            delta = event.position().toPoint().x() - self._resize_start_x
            new_width = max(40, self._resize_start_width_row + delta)  # Minimum width of 40
            self._row_header_widths[self._resize_row_level] = new_width
            # Pre-rendered tile images have stale x-positions; invalidate them.
            self._invalidate_tile_images()

            # Update editor width if editing a row header element at this level
            if self._editor is not None and self._editor.isVisible() and self._header_edit_ctx is not None:
                axis = self._header_edit_ctx.get("axis")
                ctx_type = self._header_edit_ctx.get("type", "leaf")
                
                if axis == "row":
                    if ctx_type == "leaf":
                        # Row leaf is at the rightmost level
                        leaf_level = self._row_band_levels
                        if self._resize_row_level == leaf_level:
                            geom = self._editor.geometry()
                            self._editor.setGeometry(geom.x(), geom.y(), new_width - 2, geom.height())
                    elif ctx_type == "group":
                        # Row group: check if editing a group at this level
                        group_path = self._header_edit_ctx.get("group_path")
                        if isinstance(group_path, tuple) and len(group_path) > 0:
                            # The level is the depth in the path minus 1
                            group_level = len(group_path) - 1
                            if self._resize_row_level == group_level:
                                geom = self._editor.geometry()
                                self._editor.setGeometry(geom.x(), geom.y(), new_width - 2, geom.height())
            
            # Row header width affects overall content width; update scrollbars in real time.
            self._update_scrollbars()
            self.viewport().update()
            return
        
        # Handle column resizing
        if self._resize_col is not None and self._resize_start_x is not None and self._resize_start_width is not None:
            delta = event.position().toPoint().x() - self._resize_start_x
            new_width = max(20, self._resize_start_width + delta)  # Minimum width of 20
            
            # If resizing a selected column and multiple columns are selected, resize all selected columns
            if self._sel_mode == "col" and self._resize_col in self._sel_indices and len(self._sel_indices) > 1:
                for col_idx in self._sel_indices:
                    self._col_widths[col_idx] = new_width
            else:
                self._col_widths[self._resize_col] = new_width
            # Pre-rendered tile images have stale cell widths; invalidate only
            # tiles that actually contain the affected column(s) for live
            # visual feedback during the drag.
            is_multi = (
                self._sel_mode == "col"
                and self._resize_col in self._sel_indices
                and len(self._sel_indices) > 1
            )
            affected = set(self._sel_indices) if is_multi else {self._resize_col}
            self._invalidate_tile_images_for_cols(affected)

            # Update editor width if editing a header element and visible
            if self._editor is not None and self._editor.isVisible() and self._header_edit_ctx is not None:
                axis = self._header_edit_ctx.get("axis")
                ctx_type = self._header_edit_ctx.get("type", "leaf")
                index = self._header_edit_ctx.get("index")
                
                if axis == "col":
                    # Column header: check if editing this column
                    if ctx_type == "leaf" and index == self._resize_col:
                        geom = self._editor.geometry()
                        self._editor.setGeometry(geom.x(), geom.y(), new_width - 2, geom.height())
                    elif ctx_type == "group":
                        # Column group: check if resize column is within group's range
                        group_path = self._header_edit_ctx.get("group_path")
                        for band in self._col_bands:
                            if band.get("path") == group_path:
                                c0 = int(band.get("c0", -1))
                                c1 = int(band.get("c1", -1))
                                if c0 <= self._resize_col <= c1:
                                    # Recalculate group width
                                    group_width = sum(self._col_width(c) for c in range(c0, c1 + 1))
                                    geom = self._editor.geometry()
                                    self._editor.setGeometry(geom.x(), geom.y(), group_width - 2, geom.height())
                                break
                elif axis == "row":
                    # Row header width changes affect all row headers at that level
                    if ctx_type == "leaf":
                        # Row leaf: check if this is the column being resized (row header width)
                        # Row headers don't use _resize_col, they use the row header structure
                        pass  # Row headers resized via different mechanism

            # Column widths directly change content width; update scrollbars as we drag so
            # the horizontal bar can reach the new rightmost edge immediately.
            self._update_scrollbars()
            self.viewport().update()
            return
        
        has_drag = self._drag_item_id is not None or self._drag_group_path is not None
        
        # If dragging a header, handle dimension drag-and-drop
        if self._drag_start_pos is not None and has_drag and self._drag_axis is not None:
            if (event.position().toPoint() - self._drag_start_pos).manhattanLength() < QtWidgets.QApplication.startDragDistance():
                return

            # Phase 8: group drag requires 200 ms hold
            if self._drag_is_group and not self._group_drag_ready:
                return

            drag = QtGui.QDrag(self)
            md = QtCore.QMimeData()
            data: dict[str, Any] = {"axis": self._drag_axis, "item_id": self._drag_item_id}
            if self._drag_group_path is not None:
                data["group_path"] = list(self._drag_group_path)
            if self._drag_is_group and self._drag_group_node_id is not None:
                data["group_node_id"] = self._drag_group_node_id
            
            # Include all selected items if dragging from a multi-selection
            if self._drag_axis == "row" and self._sel_mode == "row" and len(self._sel_indices) > 1:
                selected_item_ids = []
                for idx in sorted(self._sel_indices):
                    if 0 <= idx < len(self._rows):
                        item_id = self._rows[idx].get("item_id")
                        if isinstance(item_id, str):
                            selected_item_ids.append(item_id)
                if selected_item_ids:
                    data["selected_items"] = selected_item_ids
            elif self._drag_axis == "col" and self._sel_mode == "col" and len(self._sel_indices) > 1:
                selected_item_ids = []
                for idx in sorted(self._sel_indices):
                    if 0 <= idx < len(self._cols):
                        item_id = self._cols[idx].get("item_id")
                        if isinstance(item_id, str):
                            selected_item_ids.append(item_id)
                if selected_item_ids:
                    data["selected_items"] = selected_item_ids
            
            md.setData(self._mime_type(), json.dumps(data).encode("utf-8"))
            drag.setMimeData(md)
            drag.exec(QtCore.Qt.DropAction.MoveAction)
            self._drag_start_pos = None
            self._drag_item_id = None
            self._drag_group_path = None
            self._drag_group_level = None
            self._drag_group_first = None
            self._drag_group_last = None
            self._drag_axis = None
            # Phase 8: reset group drag state
            self._drag_is_group = False
            self._drag_group_node_id = None
            self._group_drag_ready = False
            self._group_drag_highlight_rows.clear()
            self._group_drag_anchor_band_path = None
            self._group_drag_badge_count = 0
            if self._group_drag_timer is not None and self._group_drag_timer.isActive():
                self._group_drag_timer.stop()
            self._group_drag_timer = None
            self.viewport().setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            if self._drop_hover is not None:
                self._drop_hover = None
                self.viewport().update()
            return
        
        # Otherwise, handle cell range selection by dragging
        if self._sel_mode == "cell" and self._drag_start_pos is not None:
            hit = self._cell_at(event.position().toPoint())
            if hit is not None:
                r, c = hit
                # Select range from anchor to current cell
                r0, r1 = min(self._sel_row, r), max(self._sel_row, r)
                c0, c1 = min(self._sel_col, c), max(self._sel_col, c)

                # Check if Ctrl is held - if so, add to existing selection instead of replacing
                ctrl_held = bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier)
                
                if not ctrl_held:
                    # Normal drag: replace selection with new range
                    self._sel_indices.clear()
                
                # Add the dragged range to selection
                for ri in range(r0, r1 + 1):
                    for ci in range(c0, c1 + 1):
                        self._sel_indices.add((ri, ci))

                # Notify listeners (e.g. status-bar stats) that the effective
                # selection has changed to this full dragged range.
                self.selection_changed.emit()
                self.viewport().update()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            DEBUG_GUI and print(f"DEBUG mouseReleaseEvent: START _sel_mode={self._sel_mode} _sel_indices={self._sel_indices}")
            # Finish row header resizing and save to view
            if self._resize_row_level is not None:
                if (
                    self._resize_start_x is not None
                    and self._resize_start_width_row is not None
                ):
                    delta = event.position().toPoint().x() - self._resize_start_x
                    new_width = max(40, self._resize_start_width_row + delta)
                    self._session.execute(
                        "set_view_row_header_width",
                        view_id=self._view_id,
                        depth_or_index=self._resize_row_level,
                        width=new_width,
                    )
                self._resize_row_level = None
                self._resize_start_x = None
                self._resize_start_width_row = None
                # Mark as dirty when column sizes change
                win = self.window()
                if hasattr(win, "_mark_dirty"):
                    try:
                        win._mark_dirty(True)
                    except Exception:
                        pass
                # Notify other windows to sync presentation changes
                self.presentation_changed.emit()
            # Finish column resizing and save to view
            if self._resize_col is not None:
                if (
                    self._resize_start_x is not None
                    and self._resize_start_width is not None
                ):
                    delta = event.position().toPoint().x() - self._resize_start_x
                    new_width = max(20, self._resize_start_width + delta)
                    # Multi-select resize: apply the same width to all selected columns
                    is_multi = (
                        self._sel_mode == "col"
                        and self._resize_col in self._sel_indices
                        and len(self._sel_indices) > 1
                    )
                    targets = sorted(self._sel_indices) if is_multi else [self._resize_col]
                    for col_idx in targets:
                        self._session.execute(
                            "set_view_col_width",
                            view_id=self._view_id,
                            col_index=col_idx,
                            width=new_width,
                        )
                self._resize_col = None
                self._resize_start_x = None
                self._resize_start_width = None
                # Mark as dirty when column sizes change
                win = self.window()
                if hasattr(win, "_mark_dirty"):
                    try:
                        win._mark_dirty(True)
                    except Exception:
                        pass
                # Notify other windows to sync presentation changes
                self.presentation_changed.emit()
            # Phase 8: cancel group drag timer if released before 200 ms
            if self._group_drag_timer is not None and self._group_drag_timer.isActive():
                self._group_drag_timer.stop()
                self._group_drag_timer = None
                self._drag_is_group = False
                self._drag_group_node_id = None
                self._group_drag_highlight_rows.clear()
                self._group_drag_anchor_band_path = None
                self._group_drag_badge_count = 0
                self.viewport().setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            DEBUG_GUI and print(f"DEBUG mouseReleaseEvent: END _sel_mode={self._sel_mode} _sel_indices={self._sel_indices}")

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(self._mime_type()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def _drop_hover_for_pos(self, pt: QtCore.QPoint) -> tuple[str, Any, QtCore.QRect] | None:
        off = self._scroll_offset()
        x = pt.x() + off.x()
        y = pt.y() + off.y()
        header_h = self._m.col_header_h * max(1, self._col_header_levels)
        row_header_w = self._row_header_width()

        # --- Col header area ---
        if y < header_h and x >= row_header_w:
            # Find column index and its left edge using actual column widths
            col_x = row_header_w
            c = -1
            for i in range(len(self._cols)):
                col_w = self._col_width(i)
                if col_x <= x < col_x + col_w:
                    c = i
                    break
                col_x += col_w
            if c == -1:
                return None
            col_w = self._col_width(c)
            frac_x = (x - col_x) / max(1, col_w)
            if self._col_band_levels > 0:
                level = int(y // self._m.col_header_h)
                if level >= self._col_band_levels:
                    # Leaf row → reorder insert before/after col
                    if 0 <= c < len(self._cols):
                        after = frac_x >= 0.5
                        insert_col = c + (1 if after else 0)
                        lx = row_header_w
                        for i in range(insert_col):
                            lx += self._col_width(i)
                        lx -= off.x()
                        rect = QtCore.QRect(int(lx) - 1, 0, 3, header_h)
                        return ("col_reorder", (c, after), rect)
                else:
                    # Band row
                    for band in self._col_bands:
                        if int(band.get("level", -1)) != level:
                            continue
                        c0 = int(band.get("c0", -1))
                        c1 = int(band.get("c1", -2))
                        if not (c0 <= c <= c1):
                            continue
                        path = band.get("path")
                        if not isinstance(path, tuple):
                            continue
                        bx = row_header_w
                        for i in range(c0):
                            bx += self._col_width(i)
                        bw = 0
                        for i in range(c0, c1 + 1):
                            bw += self._col_width(i)
                        bx_vp = bx - off.x()
                        by = level * self._m.col_header_h
                        band_rect = QtCore.QRect(int(bx_vp), int(by), int(bw), self._m.col_header_h)

                        # For stacked column dimensions without an outline, treat the entire band
                        # as a reorder target (no "into group" semantics).
                        is_stacked_band = False
                        try:
                            if isinstance(path, tuple) and len(path) == 1:
                                view = self._workspace_read_model.get_view(self._view_id)
                                col_dim_ids = list(view.get("col_dim_ids", []) or []) if view else []
                                if len(col_dim_ids) > 1:
                                    dim_level = path[0]
                                    if isinstance(dim_level, int) and 0 <= dim_level < len(col_dim_ids):
                                        dim = self._workspace_read_model.get_dimension(col_dim_ids[dim_level])
                                        outline = list(dim.get("outline", []) if dim else [])
                                        if not outline:
                                            is_stacked_band = True
                        except Exception:
                            is_stacked_band = False

                        if is_stacked_band:
                            # Use left/right half of the band to decide before/after, so any
                            # drop within the band reorders the higher-level dimension.
                            band_mid = bx_vp + bw / 2.0
                            after = x >= band_mid
                            dest_col = c1 if after else c0
                            lx = row_header_w
                            for i in range(dest_col + (1 if after else 0)):
                                lx += self._col_width(i)
                            lx -= off.x()
                            return ("col_reorder", (dest_col, after), QtCore.QRect(int(lx) - 1, 0, 3, header_h))

                        # Default behavior (outline groups): left/right quarter → reorder; center → into group.
                        if frac_x < 0.25:
                            lx = bx_vp
                            return ("col_reorder", (c0, False), QtCore.QRect(int(lx) - 1, 0, 3, header_h))
                        elif frac_x > 0.75:
                            lx = bx_vp + bw
                            return ("col_reorder", (c1, True), QtCore.QRect(int(lx) - 1, 0, 3, header_h))
                        else:
                            return ("col_into", path, band_rect)
            else:
                # No bands → reorder by leaf position
                if 0 <= c < len(self._cols):
                    after = frac_x >= 0.5
                    insert_col = c + (1 if after else 0)
                    lx = row_header_w
                    for i in range(insert_col):
                        lx += self._col_width(i)
                    lx -= off.x()
                    rect = QtCore.QRect(int(lx) - 1, 0, 3, header_h)
                    return ("col_reorder", (c, after), rect)

        # --- Row header area ---
        if x < row_header_w and y >= header_h:
            r = int((y - header_h) // self._m.row_h)
            row_y = header_h + r * self._m.row_h
            frac_y = (y - row_y) / max(1, self._m.row_h)
            # Determine level using actual widths (same as _header_hit)
            level = 0
            cumulative = 0
            for lvl in range(max(1, self._row_header_levels)):
                cumulative += self._row_header_level_width(lvl)
                if x < cumulative:
                    level = lvl
                    break
            if level >= self._row_band_levels:
                # Leaf column → reorder insert before/after row
                if 0 <= r < len(self._rows):
                    after = frac_y >= 0.5
                    ly = header_h + (r + (1 if after else 0)) * self._m.row_h - off.y()
                    rect = QtCore.QRect(0, ly - 1, row_header_w, 3)
                    return ("row_reorder", (r, after), rect)
            else:
                # Band column
                bx = 0
                for i in range(level):
                    bx += self._row_header_level_width(i)
                level_w = self._row_header_level_width(level)
                for band in self._row_bands:
                    if int(band.get("level", -1)) != level:
                        continue
                    r0 = int(band.get("r0", -1))
                    r1 = int(band.get("r1", -2))
                    if not (r0 <= r <= r1):
                        continue
                    path = band.get("path")
                    if not isinstance(path, tuple):
                        continue
                    bh = (r1 - r0 + 1) * self._m.row_h
                    by = header_h + r0 * self._m.row_h - off.y()
                    band_rect = QtCore.QRect(int(bx), int(by), level_w, int(bh))

                    # For stacked row dimensions without an outline, treat the entire band
                    # as a reorder target (no "into group" semantics).
                    is_stacked_band = False
                    try:
                        if isinstance(path, tuple) and len(path) == 1:
                            view = self._workspace_read_model.get_view(self._view_id)
                            row_dim_ids = list(view.get("row_dim_ids", []) or []) if view else []
                            if len(row_dim_ids) > 1:
                                dim_level = path[0]
                                if isinstance(dim_level, int) and 0 <= dim_level < len(row_dim_ids):
                                    dim = self._workspace_read_model.get_dimension(row_dim_ids[dim_level])
                                    outline = list(dim.get("outline", []) if dim else [])
                                    if not outline:
                                        is_stacked_band = True
                    except Exception:
                        is_stacked_band = False

                    if is_stacked_band:
                        # Use top/bottom half of the band to decide before/after, so any
                        # drop within the band reorders the higher-level dimension.
                        band_mid = by + bh / 2.0
                        after = (header_h + r * self._m.row_h - off.y()) >= band_mid
                        dest_row = r1 if after else r0
                        ly = header_h + (dest_row + (1 if after else 0)) * self._m.row_h - off.y()
                        return ("row_reorder", (dest_row, after), QtCore.QRect(0, ly - 1, row_header_w, 3))

                    # Default behavior (outline groups): top/bottom quarter → reorder; center → into group.
                    if frac_y < 0.25:
                        ly = header_h + r0 * self._m.row_h - off.y()
                        return ("row_reorder", (r0, False, path), QtCore.QRect(0, ly - 1, row_header_w, 3))
                    elif frac_y > 0.75:
                        ly = header_h + (r1 + 1) * self._m.row_h - off.y()
                        return ("row_reorder", (r1, True, path), QtCore.QRect(0, ly - 1, row_header_w, 3))
                    else:
                        return ("row_into", path, band_rect)
        return None

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        if event.mimeData().hasFormat(self._mime_type()):
            hover = self._drop_hover_for_pos(event.position().toPoint())
            if hover != self._drop_hover:
                self._drop_hover = hover
                self.viewport().update()
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event: QtGui.QDragLeaveEvent) -> None:
        self._drop_hover = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        DEBUG_GUI and print(f"DEBUG dropEvent: ENTRY _sel_mode={self._sel_mode} _sel_indices={self._sel_indices} _sel_row={self._sel_row} _sel_col={self._sel_col}")
        
        # Save scroll position and set flag to prevent scroll resets during drop
        saved_h_scroll = self.horizontalScrollBar().value()
        saved_v_scroll = self.verticalScrollBar().value()
        self._preserve_scroll = True
        DEBUG_GUI and print(f"DEBUG SCROLL: dropEvent saving scroll position h={saved_h_scroll}, v={saved_v_scroll}")
        
        self._drop_hover = None
        self.viewport().update()
        if not event.mimeData().hasFormat(self._mime_type()):
            super().dropEvent(event)
            return
        try:
            payload = json.loads(bytes(event.mimeData().data(self._mime_type())).decode("utf-8"))
        except Exception:
            return
        axis = payload.get("axis")
        if axis not in ("row", "col"):
            return
        # Do not allow reordering of items for sequential dimensions.
        dim_id = self._axis_dim_id(axis)
        if dim_id is not None:
            dim = self._workspace_read_model.get_dimension(dim_id)
            if dim and dim.get("dim_type", "set") == "seq":
                super().dropEvent(event)
                return
        item_id: str | None = payload.get("item_id")
        raw_gp = payload.get("group_path")
        src_group_path: tuple[int, ...] | None = tuple(raw_gp) if isinstance(raw_gp, list) else None
        selected_items: list[str] | None = payload.get("selected_items")

        hover = self._drop_hover_for_pos(event.position().toPoint())
        if hover is None:
            super().dropEvent(event)
            return
        mode, hover_payload, _ = hover

        def _indices_for_item_ids(drop_axis: str, ids: list[str]) -> set[int]:
            entries = self._cols if drop_axis == "col" else self._rows
            out: set[int] = set()
            seen: set[str] = set()
            for idx, entry in enumerate(entries):
                iid = entry.get("item_id")
                if not isinstance(iid, str):
                    continue
                if iid in ids and iid not in seen:
                    out.add(idx)
                    seen.add(iid)
                    if len(seen) == len(set(ids)):
                        break
            return out

        # Guard against transient mode flips during drag/drop: ensure reorders
        # start from axis selection so downstream restore logic cannot collapse
        # to cell mode (top-left).
        if mode == f"{axis}_reorder" and self._sel_mode != axis:
            desired: set[int] = set()
            if selected_items:
                desired = _indices_for_item_ids(axis, [i for i in selected_items if isinstance(i, str)])
            elif isinstance(item_id, str):
                desired = _indices_for_item_ids(axis, [item_id])
            if desired:
                self._sel_mode = axis
                self._sel_indices = desired
                if axis == "col":
                    self._sel_col = min(desired)
                else:
                    self._sel_row = min(desired)

        changed = False
        selection_already_restored = False

        DEBUG_GUI and print(f"DEBUG dropEvent: ENTRY mode={mode} axis={axis} item_id={item_id} selected_items={selected_items} hover_payload={hover_payload}")

        DEBUG_GUI and print(f"DEBUG dropEvent: ENTRY mode={mode} axis={axis} item_id={item_id} selected_items={selected_items} hover_payload={hover_payload}")
        
        # --- Multi-item reorder (when dragging a selection)
        # Skip this when dragging a stacked-dimension band header (drag_group_level set),
        # so that band drags can target the higher-level dimension instead of the innermost one.
        # Also skip when dragging an outline group header (_drag_is_group) so the group reorder
        # path (MoveNodesCommand) handles the drop instead of moving individual leaf items.
        if (
            mode == f"{axis}_reorder"
            and selected_items
            and len(selected_items) > 1
            and self._drag_group_level is None
            and not self._drag_is_group
        ):
            DEBUG_GUI and print(f"DEBUG dropEvent: taking MULTI-ITEM reorder path")
            DEBUG_GUI and print(f"DEBUG dropEvent: taking MULTI-ITEM reorder path")
            dest_col_or_row, after = hover_payload[:2]
            # Get destination item ID and path
            dest_item_id = None
            dest_path = None
            if axis == "col" and 0 <= dest_col_or_row < len(self._cols):
                dest_item_id = self._cols[dest_col_or_row].get("item_id")
                dest_path = self._cols[dest_col_or_row].get("path")
            elif axis == "row" and 0 <= dest_col_or_row < len(self._rows):
                dest_item_id = self._rows[dest_col_or_row].get("item_id")
                dest_path = self._rows[dest_col_or_row].get("path")
            
            if isinstance(dest_item_id, str):
                # Use outline reorder only when ALL selected items have paths.
                # With sparse graph, a dimension may have groups while some
                # items remain ungrouped (no path).
                all_have_paths = bool(dest_path)
                if all_have_paths and selected_items:
                    all_have_paths = all(
                        self._path_for_item_id(axis, sid) is not None
                        for sid in selected_items
                    )

                _outline_nodes = self._outline_root(axis)
                _has_graph = bool(_outline_nodes) and any(
                    getattr(n, 'node_id', None) is not None for n in _outline_nodes
                )
                if all_have_paths and _has_graph:
                    self._reorder_multiple_in_outline(axis, selected_items, dest_path, after)
                else:
                    self._reorder_multiple_flat_items(axis, selected_items, dest_item_id, after)
                    selection_already_restored = True
                changed = True
        
        # --- Single leaf reorder ---
        elif mode == f"{axis}_reorder" and isinstance(item_id, str):
            dest_col_or_row, after = hover_payload[:2]
            DEBUG_GUI and print(f"DEBUG dropEvent: SINGLE LEAF REORDER axis={axis} item_id={item_id} dest={dest_col_or_row}")
            DEBUG_GUI and print(f"DEBUG dropEvent: SINGLE LEAF REORDER axis={axis} item_id={item_id} dest={dest_col_or_row}")
            src_path = self._path_for_item_id(axis, item_id)
            _outline_nodes = self._outline_root(axis)
            has_outline = bool(_outline_nodes) and any(
                getattr(n, 'node_id', None) is not None for n in _outline_nodes
            )
            
            # Try outline-based reordering first
            if axis == "col" and 0 <= dest_col_or_row < len(self._cols):
                dest_path = self._cols[dest_col_or_row].get("path")
                DEBUG_GUI and print(f"DEBUG dropEvent: col dest_path={dest_path}")
                DEBUG_GUI and print(f"DEBUG dropEvent: col dest_path={dest_path}")
                if has_outline and isinstance(dest_path, tuple) and src_path and dest_path != src_path:
                    DEBUG_GUI and print(f"DEBUG dropEvent: calling _reorder_in_outline")
                    # Save selection keys BEFORE reorder (item will move)
                    saved_sel_keys: set[tuple[str, ...]] = set()
                    if isinstance(item_id, str):
                        key = self._key_for_item_id(axis, item_id)
                        if isinstance(key, tuple):
                            saved_sel_keys.add(key)
                    DEBUG_GUI and print(f"DEBUG dropEvent: calling _reorder_in_outline")
                    # Save selection keys BEFORE reorder (item will move)
                    saved_sel_keys: set[tuple[str, ...]] = set()
                    if isinstance(item_id, str):
                        key = self._key_for_item_id(axis, item_id)
                        if isinstance(key, tuple):
                            saved_sel_keys.add(key)
                    self._reorder_in_outline(axis, src_path, dest_path, after)
                    # Reload to refresh grid state BEFORE restoring selection
                    self.reload()
                    # Now restore selection based on new grid state
                    if saved_sel_keys:
                        new_indices = set()
                        keys = self._col_keys if axis == "col" else self._row_keys
                        entries = self._cols if axis == "col" else self._rows
                        for idx, entry in enumerate(entries):
                            iid = entry.get("item_id")
                            if isinstance(iid, str) and 0 <= idx < len(keys):
                                check_key = tuple(keys[idx]) if isinstance(keys[idx], list) else keys[idx]
                                if check_key in saved_sel_keys:
                                    new_indices.add(idx)
                        if new_indices:
                            self._sel_mode = axis
                            self._sel_indices = new_indices
                            if axis == "col":
                                self._sel_col = min(new_indices)
                            else:
                                self._sel_row = min(new_indices)
                            DEBUG_GUI and print(f"DEBUG dropEvent: restored selection after reload, indices={new_indices}")
                    selection_already_restored = True
                    changed = True
                else:
                    # No outline - reorder flat dimension items
                    dest_item_id = self._cols[dest_col_or_row].get("item_id")
                    if isinstance(dest_item_id, str):
                        self._reorder_flat_dimension(axis, item_id, dest_item_id, after)
                        selection_already_restored = True
                        changed = True
            elif axis == "row" and 0 <= dest_col_or_row < len(self._rows):
                dest_path = self._rows[dest_col_or_row].get("path")
                DEBUG_GUI and print(f"DEBUG dropEvent: row dest_path={dest_path}")
                DEBUG_GUI and print(f"DEBUG dropEvent: row dest_path={dest_path}")
                if has_outline and isinstance(dest_path, tuple) and src_path and dest_path != src_path:
                    DEBUG_GUI and print(f"DEBUG dropEvent: calling _reorder_in_outline")
                    # Save selection keys BEFORE reorder (item will move)
                    saved_sel_keys: set[tuple[str, ...]] = set()
                    if isinstance(item_id, str):
                        key = self._key_for_item_id(axis, item_id)
                        if isinstance(key, tuple):
                            saved_sel_keys.add(key)
                    DEBUG_GUI and print(f"DEBUG dropEvent: calling _reorder_in_outline")
                    # Save selection keys BEFORE reorder (item will move)
                    saved_sel_keys: set[tuple[str, ...]] = set()
                    if isinstance(item_id, str):
                        key = self._key_for_item_id(axis, item_id)
                        if isinstance(key, tuple):
                            saved_sel_keys.add(key)
                    self._reorder_in_outline(axis, src_path, dest_path, after)
                    # Reload to refresh grid state BEFORE restoring selection
                    self.reload()
                    # Now restore selection based on new grid state
                    if saved_sel_keys:
                        new_indices = set()
                        keys = self._col_keys if axis == "col" else self._row_keys
                        entries = self._cols if axis == "col" else self._rows
                        for idx, entry in enumerate(entries):
                            iid = entry.get("item_id")
                            if isinstance(iid, str) and 0 <= idx < len(keys):
                                check_key = tuple(keys[idx]) if isinstance(keys[idx], list) else keys[idx]
                                if check_key in saved_sel_keys:
                                    new_indices.add(idx)
                        if new_indices:
                            self._sel_mode = axis
                            self._sel_indices = new_indices
                            if axis == "col":
                                self._sel_col = min(new_indices)
                            else:
                                self._sel_row = min(new_indices)
                            DEBUG_GUI and print(f"DEBUG dropEvent: restored selection after reload, indices={new_indices}")
                    selection_already_restored = True
                    changed = True
                    # Restore selection immediately since reload() hasn't been called yet
                    if saved_sel_keys:
                        new_indices = set()
                        keys = self._col_keys if axis == "col" else self._row_keys
                        entries = self._cols if axis == "col" else self._rows
                        for idx, entry in enumerate(entries):
                            iid = entry.get("item_id")
                            if isinstance(iid, str) and 0 <= idx < len(keys):
                                check_key = tuple(keys[idx]) if isinstance(keys[idx], list) else keys[idx]
                                if check_key in saved_sel_keys:
                                    new_indices.add(idx)
                        if new_indices:
                            self._sel_mode = axis
                            self._sel_indices = new_indices
                            if axis == "col":
                                self._sel_col = min(new_indices)
                            else:
                                self._sel_row = min(new_indices)
                            DEBUG_GUI and print(f"DEBUG dropEvent: restored selection after _reorder_in_outline, indices={new_indices}")
                    # Restore selection immediately since reload() hasn't been called yet
                    if saved_sel_keys:
                        new_indices = set()
                        keys = self._col_keys if axis == "col" else self._row_keys
                        entries = self._cols if axis == "col" else self._rows
                        for idx, entry in enumerate(entries):
                            iid = entry.get("item_id")
                            if isinstance(iid, str) and 0 <= idx < len(keys):
                                check_key = tuple(keys[idx]) if isinstance(keys[idx], list) else keys[idx]
                                if check_key in saved_sel_keys:
                                    new_indices.add(idx)
                        if new_indices:
                            self._sel_mode = axis
                            self._sel_indices = new_indices
                            if axis == "col":
                                self._sel_col = min(new_indices)
                            else:
                                self._sel_row = min(new_indices)
                            DEBUG_GUI and print(f"DEBUG dropEvent: restored selection after _reorder_in_outline, indices={new_indices}")
                else:
                    # No outline - reorder flat dimension items
                    dest_item_id = self._rows[dest_col_or_row].get("item_id")
                    if isinstance(dest_item_id, str):
                        self._reorder_flat_dimension(axis, item_id, dest_item_id, after)
                        selection_already_restored = True
                        changed = True

        # --- Group reorder ---
        elif mode == f"{axis}_reorder" and src_group_path is not None:
            dest_col_or_row, after = hover_payload[:2]

            # First, try stacked-dimension band reorder (multiple dims on axis, no outline).
            stacked_handled = False
            if self._drag_group_level is not None and self._drag_group_first is not None:
                try:
                    view = self._workspace_read_model.get_view(self._view_id)
                    axis_dim_ids: list[str] = list(view.get(f"{axis}_dim_ids", []) or []) if view else []
                    dim_level = self._drag_group_level
                    if 0 <= dim_level < len(axis_dim_ids):
                        # Use engine keys so we can map rows/cols back to per-dimension item IDs.
                        if axis == "row":
                            keys = self._grid_read_model.row_keys(self._view_id)
                        else:
                            keys = self._grid_read_model.col_keys(self._view_id)

                        src_index = self._drag_group_first
                        dst_index = dest_col_or_row
                        if 0 <= src_index < len(keys) and 0 <= dst_index < len(keys):
                            src_key = keys[src_index]
                            dst_key = keys[dst_index]
                            if (
                                isinstance(src_key, tuple)
                                and isinstance(dst_key, tuple)
                                and dim_level < len(src_key)
                                and dim_level < len(dst_key)
                            ):
                                src_item_id = src_key[dim_level]
                                dst_item_id = dst_key[dim_level]
                                if (
                                    isinstance(src_item_id, str)
                                    and isinstance(dst_item_id, str)
                                    and src_item_id != dst_item_id
                                ):
                                    dim_id = axis_dim_ids[dim_level]
                                    print(
                                        f"DEBUG stacked_{axis}_reorder: level={dim_level} dim_id={dim_id} "
                                        f"src_item={src_item_id} dst_item={dst_item_id} after={after}"
                                    )
                                    self._reorder_flat_dimension_for_dim(
                                        dim_id, src_item_id, dst_item_id, after
                                    )
                                    selection_already_restored = True
                                    stacked_handled = True
                                    changed = True
                except Exception:
                    stacked_handled = False

            # Phase 8: group drag via graph node_id
            if not stacked_handled:
                group_node_id = payload.get("group_node_id")
                if group_node_id:
                    outline = self._outline_root(axis)
                    dim_id = self._axis_dim_id(axis)
                    if dim_id:
                        # Build descendant set for constraint checks
                        _, descendant_set, _ = get_descendant_sets(outline, src_group_path)

                        # Resolve target info from hover payload
                        target_node_id: str | None = None
                        target_parent_id: str | None = None
                        anchor_id: str | None = None
                        position: str = "last"

                        if len(hover_payload) >= 3:
                            # Band column drop: payload is (dest_row, after, target_band_path)
                            _, _, target_band_path = hover_payload
                            target_group = node_at_path(outline, target_band_path)
                            if target_group is not None:
                                target_node_id = getattr(target_group, "node_id", None)
                                parent_path = _parent_path_of(target_band_path)
                                if parent_path is not None:
                                    parent_group = node_at_path(outline, parent_path)
                                    target_parent_id = getattr(parent_group, "node_id", None) if parent_group else None
                                else:
                                    target_parent_id = None
                                anchor_id = target_node_id
                                position = "after" if after else "before"
                        else:
                            # Leaf column drop: payload is (dest_row, after)
                            entries = self._cols if axis == "col" else self._rows
                            if 0 <= dest_col_or_row < len(entries):
                                dest_leaf_path = entries[dest_col_or_row].get("path")
                            else:
                                dest_leaf_path = None
                            if isinstance(dest_leaf_path, tuple):
                                dest_node = node_at_path(outline, dest_leaf_path)
                                if dest_node is not None:
                                    # For group drag, keep the dragged group at its
                                    # original depth.  Find the anchor at the same
                                    # depth as the source group so we do not nest
                                    # the dragged group inside the leaf's parent.
                                    src_depth = len(src_group_path)
                                    anchor_path = dest_leaf_path
                                    if len(dest_leaf_path) > src_depth:
                                        anchor_path = dest_leaf_path[:src_depth]
                                        anchor_node = node_at_path(outline, anchor_path)
                                        if anchor_node is not None:
                                            dest_node = anchor_node
                                    anchor_id = getattr(dest_node, "node_id", None)
                                    parent_path = _parent_path_of(anchor_path)
                                    if parent_path is not None:
                                        parent_group = node_at_path(outline, parent_path)
                                        target_parent_id = getattr(parent_group, "node_id", None) if parent_group else None
                                    else:
                                        target_parent_id = None
                                    position = "after" if after else "before"

                        # Constraint checks
                        if target_node_id and target_node_id == group_node_id:
                            DEBUG_GUI and print("DEBUG group drop: rejected self-drop")
                        elif target_node_id and target_node_id in descendant_set:
                            DEBUG_GUI and print("DEBUG group drop: rejected drop into own subtree")
                        elif target_parent_id is not None or anchor_id is not None:
                            if not is_noop_move(
                                group_node_id=group_node_id,
                                new_parent_node_id=target_parent_id,
                                anchor_node_id=anchor_id,
                                position=position,
                                outline_nodes=outline,
                            ):
                                self.execute_command(
                                    "move_nodes",
                                    dim_id=dim_id,
                                    node_ids=[group_node_id],
                                    parent_node_id=target_parent_id,
                                    anchor_node_id=anchor_id,
                                    position=position,
                                    move_empty_parents=False,
                                )
                                changed = True
                            else:
                                DEBUG_GUI and print("DEBUG group drop: no-op move skipped")
                elif not group_node_id:
                    # Fallback: existing outline-based group reorder
                    if axis == "col" and 0 <= dest_col_or_row < len(self._cols):
                        dest_leaf_path = self._cols[dest_col_or_row].get("path")
                    elif axis == "row" and 0 <= dest_col_or_row < len(self._rows):
                        dest_leaf_path = self._rows[dest_col_or_row].get("path")
                    else:
                        dest_leaf_path = None
                    if isinstance(dest_leaf_path, tuple) and len(dest_leaf_path) >= len(src_group_path):
                        dest_group_path = dest_leaf_path[: len(src_group_path)]
                        if dest_group_path != src_group_path:
                            self._reorder_in_outline(axis, src_group_path, dest_group_path, after)
                            changed = True

        # --- Drop leaf into group ---
        elif mode == f"{axis}_into" and isinstance(item_id, str) and isinstance(hover_payload, tuple):
            self._outline.move_item_to_group(axis, item_id, hover_payload)
            changed = True

        # --- Drop group into group ---
        elif mode == f"{axis}_into" and src_group_path is not None and isinstance(hover_payload, tuple):
            group_node_id = payload.get("group_node_id")
            if group_node_id:
                outline = self._outline_root(axis)
                dim_id = self._axis_dim_id(axis)
                if dim_id:
                    _, descendant_set, _ = get_descendant_sets(outline, src_group_path)
                    target_group = node_at_path(outline, hover_payload)
                    target_node_id = getattr(target_group, "node_id", None) if target_group else None
                    if target_node_id == group_node_id:
                        DEBUG_GUI and print("DEBUG group drop into: rejected self-drop")
                    elif target_node_id and target_node_id in descendant_set:
                        DEBUG_GUI and print("DEBUG group drop into: rejected own subtree")
                    elif target_node_id and not is_noop_move(
                        group_node_id=group_node_id,
                        new_parent_node_id=target_node_id,
                        anchor_node_id=None,
                        position="last",
                        outline_nodes=outline,
                    ):
                        self.execute_command(
                            "move_nodes",
                            dim_id=dim_id,
                            node_ids=[group_node_id],
                            parent_node_id=target_node_id,
                            position="last",
                            move_empty_parents=False,
                        )
                        changed = True
                    else:
                        DEBUG_GUI and print("DEBUG group drop into: no-op move skipped")
            else:
                target_path = hover_payload
                if target_path[:len(src_group_path)] != src_group_path:
                    root = self._outline_root(axis)
                    root2, removed = self._remove_any_node_from_outline(root, src_group_path)
                    if removed is not None:
                        adj = list(target_path)
                        if (len(src_group_path) == len(target_path)
                                and src_group_path[:-1] == tuple(adj[:-1])
                                and src_group_path[-1] < adj[-1]):
                            adj[-1] -= 1
                        gnode = self._get_node_at_path(root2, tuple(adj))
                        if gnode is not None:
                            kids = list(gnode.children) + [removed]
                            root3 = self._set_node_children_at_path(root2, tuple(adj), kids)
                            self._set_outline_root(axis, root3)
                            changed = True

        if changed and selection_already_restored:
            self.content_changed.emit()
            DEBUG_GUI and print(f"DEBUG dropEvent: early_return path, mode={mode}, axis={axis}, _sel_mode={self._sel_mode}, _sel_indices={self._sel_indices}")
            event.acceptProposedAction()
            DEBUG_GUI and print(f"DEBUG dropEvent: EXIT via early_return, _sel_mode={self._sel_mode}, _sel_indices={self._sel_indices}")
            # Check state right before returning
            DEBUG_GUI and print(f"DEBUG dropEvent: FINAL CHECK _sel_mode={self._sel_mode} _sel_row={self._sel_row} _sel_col={self._sel_col}")
            # Restore scroll position and clear flag before returning
            self._preserve_scroll = False
            self.horizontalScrollBar().setValue(saved_h_scroll)
            self.verticalScrollBar().setValue(saved_v_scroll)
            DEBUG_GUI and print(f"DEBUG SCROLL: dropEvent early_return restored scroll to h={saved_h_scroll}, v={saved_v_scroll}")
            self.selection_changed.emit()
            # Notify other windows to sync after drag/drop reordering (deferred to avoid wiping scroll)
            def _emit_outline_changed(saved_h, saved_v):
                # Preserve scroll when emitting to prevent other reloads from resetting it
                self._preserve_scroll = True
                self.outline_changed.emit()
                # Restore scroll after signal handlers complete
                def _restore():
                    self.horizontalScrollBar().setValue(saved_h)
                    self.verticalScrollBar().setValue(saved_v)
                    self._preserve_scroll = False
                    # Force viewport update to ensure scroll is visually applied
                    self.viewport().update()
                    DEBUG_GUI and print(f"DEBUG SCROLL: outline_changed deferred restore to h={saved_h}, v={saved_v}")
                QtCore.QTimer.singleShot(0, _restore)
            QtCore.QTimer.singleShot(0, lambda: _emit_outline_changed(saved_h_scroll, saved_v_scroll))
            return

        if changed:
            self.content_changed.emit()
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
                            if isinstance(key, (tuple, list)):
                                saved_sel_keys.add(tuple(key))
                    elif saved_sel_mode == "row" and 0 <= idx < len(self._rows):
                        if idx < len(self._row_keys):
                            key = self._row_keys[idx]
                            if isinstance(key, (tuple, list)):
                                saved_sel_keys.add(tuple(key))

            # Reorder operations must restore as axis selection even if transient
            # event handling briefly switched mode to cell.
            if mode == f"{axis}_reorder" and (saved_sel_mode != axis or not saved_sel_keys):
                saved_sel_mode = axis
                desired_ids: list[str] = []
                if selected_items:
                    desired_ids = [i for i in selected_items if isinstance(i, str)]
                elif isinstance(item_id, str):
                    desired_ids = [item_id]

                keys = self._col_keys if axis == "col" else self._row_keys
                entries = self._cols if axis == "col" else self._rows
                rebuilt_keys: set[tuple[str, ...]] = set()
                for idx, entry in enumerate(entries):
                    iid = entry.get("item_id")
                    if not isinstance(iid, str) or iid not in desired_ids:
                        continue
                    if 0 <= idx < len(keys):
                        key = keys[idx]
                        if isinstance(key, (tuple, list)):
                            rebuilt_keys.add(tuple(key))

                if not rebuilt_keys and keys:
                    fallback_idx = self._sel_col if axis == "col" else self._sel_row
                    if 0 <= fallback_idx < len(keys):
                        key = keys[fallback_idx]
                        if isinstance(key, (tuple, list)):
                            rebuilt_keys.add(tuple(key))

                if rebuilt_keys:
                    saved_sel_keys = rebuilt_keys
            saved_anchor_row = self._anchor_row
            saved_anchor_col = self._anchor_col
            # Save scroll position before reload (using top-level variables)
            saved_h_scroll = self.horizontalScrollBar().value()
            saved_v_scroll = self.verticalScrollBar().value()
            
            self.reload()
            
            # Restore scroll position
            self.horizontalScrollBar().setValue(saved_h_scroll)
            self.verticalScrollBar().setValue(saved_v_scroll)
            # Clear preserve flag
            self._preserve_scroll = False
            DEBUG_GUI and print(f"DEBUG SCROLL: dropEvent final restore to h={saved_h_scroll}, v={saved_v_scroll}")
            
            # Restore selection mode and indices (adjusted for new positions)
            if saved_sel_mode == "col":
                new_indices = set()
                for idx, key in enumerate(self._col_keys):
                    check_key = tuple(key) if isinstance(key, list) else key
                    if check_key in saved_sel_keys:
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
                    check_key = tuple(key) if isinstance(key, list) else key
                    if check_key in saved_sel_keys:
                        new_indices.add(idx)
                self._sel_mode = "row"
                self._sel_indices = new_indices
                if new_indices:
                    self._sel_row = min(new_indices)
                elif self._rows:
                    self._sel_row = min(saved_sel_row, max(0, len(self._rows) - 1))
            
            self._anchor_row = min(saved_anchor_row, max(0, len(self._rows) - 1)) if self._rows else 0
            self._anchor_col = min(saved_anchor_col, max(0, len(self._cols) - 1)) if self._cols else 0

            # Persist restored selection so subsequent reloads/rebuilds see correct state
            self._write_selection_to_session()

            # Notify other windows to sync after drag/drop reordering (deferred to avoid wiping scroll)
            QtCore.QTimer.singleShot(0, self.outline_changed.emit)
            event.acceptProposedAction()
            DEBUG_GUI and print(f"DEBUG dropEvent: EXIT via final_restore, _sel_mode={self._sel_mode}, _sel_indices={self._sel_indices}")
            return
        super().dropEvent(event)
        DEBUG_GUI and print(f"DEBUG dropEvent: EXIT via super(), _sel_mode={self._sel_mode}, _sel_indices={self._sel_indices}, _sel_row={self._sel_row}, _sel_col={self._sel_col}")

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        super().mouseDoubleClickEvent(event)
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return

        hh = self._header_hit(event.position().toPoint())
        print(f"DEBUG mouseDoubleClick: hh={hh}")
        if hh is not None:
            kind, payload = hh
            print(f"DEBUG mouseDoubleClick: kind={kind}, payload={payload}")
            if kind in {"row_leaf", "col_leaf"}:
                # Set selection to the clicked header before editing
                if kind == "row_leaf" and isinstance(payload, tuple) and len(payload) > 1:
                    clicked_row = payload[1]
                    self._sel_mode = "row"
                    self._sel_indices = {clicked_row}
                    self._sel_row = clicked_row
                    self.viewport().update()
                elif kind == "col_leaf":
                    if isinstance(payload, int):
                        clicked_col = payload
                    elif isinstance(payload, tuple) and len(payload) > 0 and isinstance(payload[0], str):
                        # Find column by item_id
                        clicked_col = None
                        for i, col in enumerate(self._cols):
                            if col.get("item_id") == payload[0]:
                                clicked_col = i
                                break
                    else:
                        clicked_col = None
                    if clicked_col is not None:
                        self._sel_mode = "col"
                        self._sel_indices = {clicked_col}
                        self._sel_col = clicked_col
                        self.viewport().update()
                result = self._start_header_leaf_edit_from_hit(hh)
                print(f"DEBUG mouseDoubleClick: _start_header_leaf_edit_from_hit returned {result}")
                if not result:
                    # Fallback to rename dialog for stacked mode
                    self._rename_header_hit(hh)
            elif kind in {"row_group", "col_group"} and isinstance(payload, tuple) and len(payload) > 1:
                # Start inline editing for outline-based groups, dialog for band-based (stacked) groups
                axis = "row" if kind == "row_group" else "col"
                group_path = payload[0] if isinstance(payload[0], tuple) else payload
                if isinstance(group_path, tuple) and group_path:
                    # Check if this is an outline-based group (has outline structure)
                    root = self._outline_root(axis)
                    if root:
                        node = self._get_node_at_path(root, group_path)
                        if node and node.item_id is None:  # It's an outline-based group
                            if self._start_group_header_edit(axis, group_path):
                                return
                    # For band-based groups in stacked mode, use dialog (renames underlying item)
                self._rename_header_hit(hh)
            else:
                self._rename_header_hit(hh)
            return

        hit = self._cell_at(event.position().toPoint())
        if hit is None:
            return
        r, c = hit
        self._sel_row, self._sel_col = r, c
        self._clamp_selection_to_leaf()
        self._start_editing(self._sel_row, self._sel_col)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        key = event.key()
        if key in (QtCore.Qt.Key.Key_Up, QtCore.Qt.Key.Key_Down, QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right):
            print(f"DEBUG GRID KEY: key={key} hasFocus={self.hasFocus()}")
        ctrl_held = bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier)
        shift_held = bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)

        if self._header_edit_ctx is not None and not self._editor.isVisible():
            self._ensure_label_editor_visible_from_ctx("grid_keypress_hidden")

        if self._editor.isVisible() and key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            self._debug_edit_state("grid_keypress_enter_while_editor_visible")

        if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter) and self._ignore_next_grid_enter:
            self._ignore_next_grid_enter = False
            print("DEBUG edit_mode_switch: grid_enter_ignored reason=post_label_enter")
            return
        
        # Always handle Escape to cancel edit mode, even when editor is visible
        if key == QtCore.Qt.Key.Key_Escape:
            self._cancel_edit()
            self._edit_mode = "navigation"
            DEBUG_GUI and print(f"DEBUG edit_mode_switch: -> navigation (escape)")
            return
        
        # Ignore lone modifier keys to avoid clearing selection (e.g., Ctrl before Ctrl+C)
        if key in (
            QtCore.Qt.Key.Key_Control,
            QtCore.Qt.Key.Key_Shift,
            QtCore.Qt.Key.Key_Alt,
            QtCore.Qt.Key.Key_Meta,
        ):
            return

        # Handle Enter to commit edit when editor is visible
        if self._editor.isVisible():
            # For label editing, eventFilter handles all keys including cursor navigation
            if self._header_edit_ctx is not None:
                # Only handle Enter here; eventFilter handles everything else
                if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                    self._commit_header_editor(move_next=True)
                return

            # For cell editing: Plain arrow keys while editing: commit current edit and then
            # fall through to normal navigation handling (move selection in
            # the direction of the arrow). This matches spreadsheet-style
            # behaviour where pressing an arrow after typing both commits and
            # moves.
            if key in (
                QtCore.Qt.Key.Key_Left,
                QtCore.Qt.Key.Key_Right,
                QtCore.Qt.Key.Key_Up,
                QtCore.Qt.Key.Key_Down,
            ) and not (ctrl_held or shift_held):
                self._commit_editor()
                # Editor is now hidden; continue below so the arrow key is
                # processed by the normal navigation logic.
            else:
                if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                    self._commit_editor()
                    # After commit, move selection down one cell (like in nav mode)
                    if self._rows:
                        nr = self._sel_row + 1
                        # Skip non-leaf rows
                        while nr < len(self._rows) and not self._rows[nr].get("is_leaf", False):
                            nr += 1
                        if nr < len(self._rows):
                            self._sel_row = nr
                            self._clamp_selection_to_leaf()
                            self._sel_mode = "cell"
                            self._sel_indices.clear()
                            self._anchor_row, self._anchor_col = self._sel_row, self._sel_col
                            self.selection_changed.emit()
                            self.viewport().update()
                            self._request_repaint("enter_move_down_after_commit")
                            self._ensure_visible(self._sel_row, self._sel_col)
                    return
                if key == QtCore.Qt.Key.Key_C and ctrl_held:
                    self._send_editor_key_event(event)
                    return
                txt = event.text()
                if txt and txt.isprintable() and not (
                    event.modifiers()
                    & (
                        QtCore.Qt.KeyboardModifier.ControlModifier
                        | QtCore.Qt.KeyboardModifier.AltModifier
                        | QtCore.Qt.KeyboardModifier.MetaModifier
                    )
                ):
                    self._send_editor_key_event(event)
                    return
                # For other keys (including arrow keys with modifiers), let
                # the base implementation or the editor handle them.
                super().keyPressEvent(event)
                return

        if key == QtCore.Qt.Key.Key_F2:
            if self._sel_mode == "row" and self._sel_indices:
                target_idx = min(self._sel_indices)
                target_idx = max(0, min(target_idx, len(self._rows) - 1))
                if not self._rows[target_idx].get("is_leaf", False):
                    target_idx = self._find_next_leaf_row(target_idx)
                if 0 <= target_idx < len(self._rows) and self._rows[target_idx].get("is_leaf", False):
                    if self._start_header_leaf_edit("row", target_idx):
                        return
            elif self._sel_mode == "col" and self._sel_indices:
                target_idx = min(self._sel_indices)
                target_idx = max(0, min(target_idx, len(self._cols) - 1))
                if not self._cols[target_idx].get("is_leaf", False):
                    target_idx = self._find_next_leaf_col(target_idx)
                if 0 <= target_idx < len(self._cols) and self._cols[target_idx].get("is_leaf", False):
                    if self._start_header_leaf_edit("col", target_idx):
                        return
            # Fallback to cell editing when no header context
            self._clamp_selection_to_leaf()
            self._start_editing(self._sel_row, self._sel_col)
            return

        if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            # In navigation mode, Enter moves selection down one cell
            if self._rows:
                # Move to next row (similar to Down arrow)
                nr = self._sel_row + 1
                # Skip non-leaf rows
                while nr < len(self._rows) and not self._rows[nr].get("is_leaf", False):
                    nr += 1
                if nr < len(self._rows):
                    self._sel_row = nr
                    self._clamp_selection_to_leaf()
                    self._sel_mode = "cell"
                    self._sel_indices.clear()
                    self._anchor_row, self._anchor_col = self._sel_row, self._sel_col
                    self.selection_changed.emit()
                    self.viewport().update()
                    self._request_repaint("enter_move_down")
                    self._ensure_visible(self._sel_row, self._sel_col)
            return

        # Ctrl+A: select all cells
        if key == QtCore.Qt.Key.Key_A and (event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier):
            self._sel_mode = "all"
            self._sel_indices.clear()  # Empty = all visible cells
            self._sel_row = 0
            self._sel_col = 0
            self._anchor_row, self._anchor_col = self._sel_row, self._sel_col
            self.selection_changed.emit()
            self.viewport().update()
            return

        # Shift+Space: select entire current row
        if shift_held and not ctrl_held and key == QtCore.Qt.Key.Key_Space:
            if not self._rows:
                return
            self._clamp_selection_to_leaf()
            target_rows: set[int] = set()
            if self._sel_mode == "row" and self._sel_indices:
                candidates = self._sel_indices
            elif self._sel_mode == "cell" and self._sel_indices:
                candidates = {r for (r, _) in self._sel_indices}
            elif self._sel_mode == "col" and self._sel_indices:
                candidates = {
                    idx
                    for idx, row in enumerate(self._rows)
                    if row.get("is_leaf", False)
                }
            else:
                candidates = {self._sel_row}

            for row in candidates:
                if not self._rows:
                    break
                row = max(0, min(row, len(self._rows) - 1))
                if not self._rows[row].get("is_leaf", False):
                    row = self._find_next_leaf_row(row)
                target_rows.add(max(0, min(row, len(self._rows) - 1)))

            if not target_rows:
                row = max(0, min(self._sel_row, len(self._rows) - 1))
                if not self._rows[row].get("is_leaf", False):
                    row = self._find_next_leaf_row(row)
                target_rows.add(max(0, min(row, len(self._rows) - 1)))

            self._sel_mode = "row"
            self._sel_indices = target_rows
            self._sel_row = min(target_rows) if target_rows else 0
            self._anchor_row = self._sel_row
            self.selection_changed.emit()
            self.viewport().update()
            self._request_repaint("shortcut_shift_space_row_select")
            self._ensure_visible(self._sel_row, self._sel_col)
            return

        # Ctrl+Space: select entire current column
        if ctrl_held and not shift_held and key == QtCore.Qt.Key.Key_Space:
            if not self._cols:
                return
            self._clamp_selection_to_leaf()
            target_cols: set[int] = set()
            if self._sel_mode == "col" and self._sel_indices:
                candidates = self._sel_indices
            elif self._sel_mode == "cell" and self._sel_indices:
                candidates = {c for (_, c) in self._sel_indices}
            elif self._sel_mode == "row" and self._sel_indices:
                candidates = {
                    idx
                    for idx, col in enumerate(self._cols)
                    if col.get("is_leaf", False)
                }
            else:
                candidates = {self._sel_col}

            for col in candidates:
                if not self._cols:
                    break
                col = max(0, min(col, len(self._cols) - 1))
                if not self._cols[col].get("is_leaf", False):
                    col = self._find_next_leaf_col(col)
                target_cols.add(max(0, min(col, len(self._cols) - 1)))

            if not target_cols:
                col = max(0, min(self._sel_col, len(self._cols) - 1))
                if not self._cols[col].get("is_leaf", False):
                    col = self._find_next_leaf_col(col)
                target_cols.add(max(0, min(col, len(self._cols) - 1)))

            self._sel_mode = "col"
            self._sel_indices = target_cols
            self._sel_col = min(target_cols) if target_cols else 0
            self._anchor_col = self._sel_col
            self.selection_changed.emit()
            self.viewport().update()
            self._request_repaint("shortcut_ctrl_space_col_select")
            self._ensure_visible(self._sel_row, self._sel_col)
            return

        # Ctrl+Shift+Arrow: jump to edge and extend selection from anchor
        if ctrl_held and shift_held and key in (
            QtCore.Qt.Key.Key_Left,
            QtCore.Qt.Key.Key_Right,
            QtCore.Qt.Key.Key_Up,
            QtCore.Qt.Key.Key_Down,
        ):
            if key == QtCore.Qt.Key.Key_Left:
                self._sel_col = 0
            elif key == QtCore.Qt.Key.Key_Right:
                self._sel_col = max(0, len(self._cols) - 1)
            elif key == QtCore.Qt.Key.Key_Up:
                self._sel_row = self._find_next_leaf_row(0)
            elif key == QtCore.Qt.Key.Key_Down:
                self._sel_row = self._find_next_leaf_row(len(self._rows) - 1)

            self._clamp_selection_to_leaf()

            # Ensure anchor is within bounds and on a leaf row/col
            if self._rows:
                self._anchor_row = max(0, min(self._anchor_row, len(self._rows) - 1))
                if not self._rows[self._anchor_row].get("is_leaf", False):
                    self._anchor_row = self._find_next_leaf_row(self._anchor_row)
            else:
                self._anchor_row = 0
            if self._cols:
                self._anchor_col = max(0, min(self._anchor_col, len(self._cols) - 1))
                if not self._cols[self._anchor_col].get("is_leaf", False):
                    self._anchor_col = self._find_next_leaf_col(self._anchor_col)
            else:
                self._anchor_col = 0

            r0, r1 = min(self._anchor_row, self._sel_row), max(self._anchor_row, self._sel_row)
            c0, c1 = min(self._anchor_col, self._sel_col), max(self._anchor_col, self._sel_col)
            DEBUG_GUI and print(f"DEBUG SET CELL: line {__import__('inspect').currentframe().f_lineno} prev={self._sel_mode}"); self._sel_mode = "cell"
            self._sel_indices.clear()
            for ri in range(r0, r1 + 1):
                if not self._rows[ri].get("is_leaf", False):
                    continue
                for ci in range(c0, c1 + 1):
                    self._sel_indices.add((ri, ci))
            self.selection_changed.emit()
            self.viewport().update()
            self._request_repaint("ctrl_shift_arrow")
            self._ensure_visible(self._sel_row, self._sel_col)
            return

        # Shift+Arrow: extend selection from anchor
        if shift_held and not ctrl_held and key in (
            QtCore.Qt.Key.Key_Left,
            QtCore.Qt.Key.Key_Right,
            QtCore.Qt.Key.Key_Up,
            QtCore.Qt.Key.Key_Down,
        ):
            # Handle row selection mode with Up/Down
            if self._sel_mode == "row" and key in (QtCore.Qt.Key.Key_Up, QtCore.Qt.Key.Key_Down):
                dr = -1 if key == QtCore.Qt.Key.Key_Up else 1
                nr = max(0, min(len(self._rows) - 1, self._sel_row + dr)) if self._rows else 0
                
                # Skip non-leaf rows
                while 0 <= nr < len(self._rows) and not self._rows[nr].get("is_leaf", False):
                    nr += dr
                    if nr < 0 or nr >= len(self._rows):
                        break
                
                if 0 <= nr < len(self._rows):
                    self._sel_row = nr
                    # Extend selection from anchor to current position
                    r0, r1 = min(self._anchor_row, self._sel_row), max(self._anchor_row, self._sel_row)
                    self._sel_indices = set(range(r0, r1 + 1))
                    self.selection_changed.emit()
                    self.viewport().update()
                    self._request_repaint("shift_arrow_row")
                    self._ensure_visible(self._sel_row, self._sel_col)
                return
            
            # Handle column selection mode with Left/Right
            if self._sel_mode == "col" and key in (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right):
                dc = -1 if key == QtCore.Qt.Key.Key_Left else 1
                nc = max(0, min(len(self._cols) - 1, self._sel_col + dc)) if self._cols else 0
                
                # Skip non-leaf columns
                while 0 <= nc < len(self._cols) and not self._cols[nc].get("is_leaf", False):
                    nc += dc
                    if nc < 0 or nc >= len(self._cols):
                        break
                
                if 0 <= nc < len(self._cols):
                    self._sel_col = nc
                    # Extend selection from anchor to current position
                    c0, c1 = min(self._anchor_col, self._sel_col), max(self._anchor_col, self._sel_col)
                    self._sel_indices = set(range(c0, c1 + 1))
                    self.selection_changed.emit()
                    self.viewport().update()
                    self._request_repaint("shift_arrow_col")
                    self._ensure_visible(self._sel_row, self._sel_col)
                return
            
            # Default cell selection mode behavior
            dr = dc = 0
            if key == QtCore.Qt.Key.Key_Left:
                dc = -1
            elif key == QtCore.Qt.Key.Key_Right:
                dc = 1
            elif key == QtCore.Qt.Key.Key_Up:
                dr = -1
            elif key == QtCore.Qt.Key.Key_Down:
                dr = 1

            nr = max(0, min(len(self._rows) - 1, self._sel_row + dr)) if self._rows else 0
            nc = max(0, min(len(self._cols) - 1, self._sel_col + dc)) if self._cols else 0
            self._sel_row, self._sel_col = nr, nc
            self._clamp_selection_to_leaf()

            # Ensure anchor is within bounds and on a leaf row/col
            if self._rows:
                self._anchor_row = max(0, min(self._anchor_row, len(self._rows) - 1))
                if not self._rows[self._anchor_row].get("is_leaf", False):
                    self._anchor_row = self._find_next_leaf_row(self._anchor_row)
            else:
                self._anchor_row = 0
            if self._cols:
                self._anchor_col = max(0, min(self._anchor_col, len(self._cols) - 1))
                if not self._cols[self._anchor_col].get("is_leaf", False):
                    self._anchor_col = self._find_next_leaf_col(self._anchor_col)
            else:
                self._anchor_col = 0

            r0, r1 = min(self._anchor_row, self._sel_row), max(self._anchor_row, self._sel_row)
            c0, c1 = min(self._anchor_col, self._sel_col), max(self._anchor_col, self._sel_col)
            DEBUG_GUI and print(f"DEBUG SET CELL: line {__import__('inspect').currentframe().f_lineno} prev={self._sel_mode}"); self._sel_mode = "cell"
            self._sel_indices.clear()
            for ri in range(r0, r1 + 1):
                if not self._rows[ri].get("is_leaf", False):
                    continue
                for ci in range(c0, c1 + 1):
                    self._sel_indices.add((ri, ci))
            self.selection_changed.emit()
            self.viewport().update()
            self._request_repaint("shift_arrow")
            self._ensure_visible(self._sel_row, self._sel_col)
            return

        # Ctrl+Arrow: jump to edges (only when key is an arrow)
        if (event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier) and key in (
            QtCore.Qt.Key.Key_Left,
            QtCore.Qt.Key.Key_Right,
            QtCore.Qt.Key.Key_Up,
            QtCore.Qt.Key.Key_Down,
        ):
            if key == QtCore.Qt.Key.Key_Left:
                self._sel_col = 0
            elif key == QtCore.Qt.Key.Key_Right:
                self._sel_col = max(0, len(self._cols) - 1)
            elif key == QtCore.Qt.Key.Key_Up:
                self._sel_row = self._find_next_leaf_row(0)
            elif key == QtCore.Qt.Key.Key_Down:
                self._sel_row = self._find_next_leaf_row(len(self._rows) - 1)
            self._clamp_selection_to_leaf()
            DEBUG_GUI and print(f"DEBUG SET CELL: line {__import__('inspect').currentframe().f_lineno} prev={self._sel_mode}"); self._sel_mode = "cell"
            self._sel_indices.clear()
            self._anchor_row, self._anchor_col = self._sel_row, self._sel_col
            self.selection_changed.emit()
            self.viewport().update()
            self._request_repaint("ctrl_arrow")
            self._ensure_visible(self._sel_row, self._sel_col)
            return

        # Ctrl+Home/End: jump to top-left / bottom-right
        if ctrl_held and key in (QtCore.Qt.Key.Key_Home, QtCore.Qt.Key.Key_End):
            if key == QtCore.Qt.Key.Key_Home:
                self._sel_row = self._find_next_leaf_row(0) if self._rows else 0
                self._sel_col = 0
            else:
                self._sel_row = self._find_next_leaf_row(len(self._rows) - 1) if self._rows else 0
                self._sel_col = max(0, len(self._cols) - 1)
            self._clamp_selection_to_leaf()
            DEBUG_GUI and print(f"DEBUG SET CELL: line {__import__('inspect').currentframe().f_lineno} prev={self._sel_mode}"); self._sel_mode = "cell"
            self._sel_indices.clear()
            self._anchor_row, self._anchor_col = self._sel_row, self._sel_col
            self.selection_changed.emit()
            self.viewport().update()
            self._ensure_visible(self._sel_row, self._sel_col)
            return

        # Copy selection (Ctrl+C or Ctrl+Insert)
        if (key == QtCore.Qt.Key.Key_C and ctrl_held) or (key == QtCore.Qt.Key.Key_Insert and ctrl_held):
            self._copy_selection_to_clipboard()
            return

        # Clipboard paste (Ctrl+V or Shift+Insert)
        if (key == QtCore.Qt.Key.Key_V and (event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier)) or (
            key == QtCore.Qt.Key.Key_Insert and (event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
        ):
            self._paste_clipboard()
            return

        # Clear cells with Delete when not editing (Backspace only inside editor)
        if key == QtCore.Qt.Key.Key_Delete:
            self._clear_selection_values()
            return

        # Printable key starts editing and inserts the character
        txt = event.text()
        if txt and txt.isprintable() and not (event.modifiers() & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.AltModifier | QtCore.Qt.KeyboardModifier.MetaModifier)):
            self._clamp_selection_to_leaf()
            self._start_editing(self._sel_row, self._sel_col)
            if self._editor.isVisible():
                self._editor.setText(txt)
                self._editor.setCursorPosition(len(txt))
            return

        dr = 0
        dc = 0
        if key == QtCore.Qt.Key.Key_Left:
            dc = -1
        elif key == QtCore.Qt.Key.Key_Right:
            dc = 1
        elif key == QtCore.Qt.Key.Key_Up:
            dr = -1
        elif key == QtCore.Qt.Key.Key_Down:
            dr = 1
        else:
            super().keyPressEvent(event)
            return

        nr = max(0, min(len(self._rows) - 1, self._sel_row + dr))
        nc = max(0, min(len(self._cols) - 1, self._sel_col + dc))
        if (nr, nc) != (self._sel_row, self._sel_col):
            self._sel_row, self._sel_col = nr, nc
            self._clamp_selection_to_leaf()
            # Navigating without Shift collapses to single-cell selection
            DEBUG_GUI and print(f"DEBUG SET CELL: line {__import__('inspect').currentframe().f_lineno} prev={self._sel_mode}"); self._sel_mode = "cell"
            self._sel_indices.clear()
            self._anchor_row, self._anchor_col = self._sel_row, self._sel_col
            self.selection_changed.emit()
            self.viewport().update()
            self._request_repaint("arrow_nav")
            self._ensure_visible(self._sel_row, self._sel_col)
            # Phase 5D: write local cache to SessionStore source of truth.
            self._write_selection_to_session()
        return

    def _write_selection_to_session(self) -> None:
        """Write current local selection cache to SessionStore via command.

        Phase 5D: MatrixGrid write path.  Local cache updates first for
        responsiveness; this call makes SessionStore the source of truth.
        The TEMP BRIDGE in cmd_set_selection may asynchronously update the
        grid again, but the position is idempotent.
        """
        self._local_selection_change_in_progress = True
        try:
            if hasattr(self._session, "execute"):
                indices: list[tuple[int, int] | int] = []
                if self._sel_indices:
                    if self._sel_mode == "cell":
                        indices = sorted(self._sel_indices)
                    elif self._sel_mode in ("row", "col"):
                        indices = sorted(self._sel_indices)
                self._session.execute(
                    "set_selection",
                    row=self._sel_row,
                    col=self._sel_col,
                    mode=self._sel_mode,
                    anchor_row=self._anchor_row,
                    anchor_col=self._anchor_col,
                    selected_indices=indices,
                )
        except Exception:
            # TEMP BRIDGE: failures are non-fatal; local cache remains valid.
            pass
        finally:
            self._local_selection_change_in_progress = False

    def _ensure_visible(self, r: int, c: int) -> None:
        """Ensure cell at (r, c) is visible by scrolling if needed.
        
        Skips scrolling if _preserve_scroll is set (e.g., during drag/drop).
        """
        if self._preserve_scroll:
            h = self.horizontalScrollBar().value()
            v = self.verticalScrollBar().value()
            DEBUG_GUI and print(f"DEBUG SCROLL: _ensure_visible SKIPPED for cell ({r},{c}) - _preserve_scroll=True, scroll preserved at h={h}, v={v}")
            return
        return self._navigation.ensure_visible(r, c)

    # ------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------

    def _start_editing(self, r: int, c: int) -> None:
        if self._ignore_next_grid_enter:
            DEBUG_GUI and print("DEBUG edit_mode_switch: cell_edit_request_ignored reason=post_label_enter")
            self._ignore_next_grid_enter = False
            return
        if not (0 <= r < len(self._rows)):
            return
        if not (0 <= c < len(self._cols)):
            return
        if not self._rows[r].get("is_leaf", False):
            return
        try:
            row_key = self._row_keys[self._leaf_row_index(r)]
            col_key = self._col_keys[c]
            cell = self._session.query(
                "cell_detail",
                view_id=self._view_id,
                row_key=row_key,
                col_key=col_key,
            )
        except Exception as e:
            print(f"ERROR: Failed to get cell for editing: {e}")
            return

        # Store original value for cancel support
        cell_value = cell.get("value") if cell else None
        self._edit_orig_value = "" if cell_value is None else str(cell_value)
        self._editor.setText(self._edit_orig_value)
        self._set_editor_focus_enabled(True)
        self._editor.selectAll()
        rect = self._cell_rect(r, c).adjusted(1, 0, -1, 2)
        self._editor.setGeometry(rect)
        self._set_edit_mode("cell")
        self.viewport().setFocusProxy(None)
        self._editor.show()
        self._editor.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        self._editor.activateWindow()
        self._editor.raise_()
        DEBUG_GUI and print(f"DEBUG edit_mode_switch: -> cell row={r} col={c}")

    def _hide_editor(self, *, restore_grid_focus: bool = True) -> None:
        if self._editor.isVisible():
            mode = "label" if self._header_edit_ctx is not None else "cell"
            DEBUG_GUI and print(f"DEBUG edit_mode_switch: {mode} -> none")
        if self._editor.isVisible():
            self._editor.clearFocus()
            self._editor.hide()
        self._set_editor_focus_enabled(False)
        self._header_edit_ctx = None
        if self.isVisible():
            self.viewport().setFocusProxy(self)
        self.viewport().update()
        if restore_grid_focus:
            if self.isVisible():
                self.viewport().setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
                self.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
            grid = self
            QtCore.QTimer.singleShot(0, lambda: grid.viewport().setFocus(QtCore.Qt.FocusReason.OtherFocusReason) if grid.isVisible() else None)
            QtCore.QTimer.singleShot(0, lambda: grid.setFocus(QtCore.Qt.FocusReason.OtherFocusReason) if grid.isVisible() else None)
            self._set_edit_mode("navigation")
        self._request_repaint("hide_editor")
        if restore_grid_focus:
            DEBUG_GUI and print("DEBUG edit_mode_switch: grid_focus_restored_after_hide")

    # ------------------------------------------------------------
    # Selection helpers for edits/paste
    # ------------------------------------------------------------

    def _iter_selected_cells(self) -> list[tuple[int, int]]:
        """Return list of (r, c) tuples for all selected cells."""
        return self._selection.iter_selected_cells()

    def _request_repaint(self, tag: str) -> None:
        """Request a repaint of the grid viewport."""
        vp = self.viewport()
        if vp is None:
            return
        self._pending_repaint_tag = tag
        DEBUG_GUI and print(
            f"DEBUG repaint_request: tag={tag} updatesEnabled={vp.updatesEnabled()} isActiveWindow={self.isActiveWindow()}"
        )
        vp.update()

    def _active_col_item_ids(self) -> set[str]:
        """Return all item IDs for the currently selected column (for stacked dimensions)."""
        return self._selection._active_col_item_ids()

    def _active_row_item_ids(self) -> set[str]:
        """Return all item IDs for the currently selected row (for stacked dimensions)."""
        return self._selection._active_row_item_ids()

    def _col_item_ids(self, col_idx: int) -> set[str]:
        """Return all item IDs for a column (for stacked dimensions)."""
        return self._geometry.col_item_ids(col_idx)

    def _row_item_ids(self, row_idx: int) -> set[str]:
        """Return all item IDs for a specific row (for stacked dimensions)."""
        return self._geometry.row_item_ids(row_idx)

    def _active_row_leaf_item_id(self) -> str | None:
        """Return only the leaf item ID for the currently selected row."""
        return self._selection._active_row_leaf_item_id()

    def _active_col_leaf_item_id(self) -> str | None:
        """Return only the leaf item ID for the currently selected column."""
        return self._selection._active_col_leaf_item_id()

    def _row_leaf_item_id(self, row_idx: int) -> str | None:
        """Return only the leaf item ID for a specific row."""
        return self._geometry.row_leaf_item_id(row_idx)

    def _col_leaf_item_id(self, col_idx: int) -> str | None:
        """Return only the leaf item ID for a specific column."""
        return self._geometry.col_leaf_item_id(col_idx)

    def _header_leaf_item_id(self, axis: str, index: int) -> str | None:
        """Return leaf item ID for header at given axis and index."""
        return self._navigation.header_leaf_item_id(axis, index)

    def _is_related_col(self, col_idx: int) -> bool:
        """Check if column is related to current selection (same dimension item)."""
        return self._selection.is_related_col(col_idx)

    def _is_related_row(self, row_idx: int) -> bool:
        """Check if row is related to current selection (same dimension item)."""
        return self._selection.is_related_row(row_idx)

    def _clear_selection_values(self) -> None:
        """Clear values in all selected cells."""
        self._clipboard.clear_selection()

    def _copy_selection_to_clipboard(self) -> None:
        """Copy selected cells to clipboard as tab-separated values."""
        self._clipboard.copy_selection()

    def _paste_clipboard(self) -> None:
        """Paste clipboard content into grid starting at current selection."""
        self._clipboard.paste_clipboard()


    def _cancel_edit(self) -> None:
        """Cancel editing and revert to original value."""
        if not self._editor.isVisible():
            return
        if self._header_edit_ctx is not None:
            saved_mode = self._header_edit_ctx.get("saved_sel_mode", "cell")
            saved_row = self._header_edit_ctx.get("saved_sel_row", self._sel_row)
            saved_col = self._header_edit_ctx.get("saved_sel_col", self._sel_col)
            saved_indices = self._header_edit_ctx.get("saved_sel_indices", set())
            self._hide_editor()
            self._sel_mode = saved_mode
            self._sel_row = saved_row
            self._sel_col = saved_col
            self._sel_indices = set(saved_indices)
            self.viewport().update()
            if self.isVisible():
                self.setFocus()
            return
        # Restore original value
        if hasattr(self, '_edit_orig_value') and self._edit_orig_value is not None:
            self._editor.setText(self._edit_orig_value)
        self._hide_editor()
        if self.isVisible():
            self.setFocus()

    def _next_header_leaf_index(self, axis: str, current_index: int) -> int | None:
        """Find the next leaf header index after current_index."""
        return self._navigation.next_header_leaf_index(axis, current_index)

    def _prev_header_leaf_index(self, axis: str, current_index: int) -> int | None:
        """Find the previous leaf header index before current_index."""
        return self._navigation.prev_header_leaf_index(axis, current_index)

    def _update_visible_leaf_label(self, axis: str, index: int, new_name: str) -> None:
        if axis == "row":
            if 0 <= index < len(self._rows):
                labels = list(self._rows[index].get("labels") or [])
                if labels:
                    labels[-1] = new_name
                    self._rows[index]["labels"] = labels
        elif axis == "col":
            if 0 <= index < len(self._cols):
                labels = list(self._cols[index].get("labels") or [])
                if labels:
                    labels[-1] = new_name
                    self._cols[index]["labels"] = labels

    def _enter_cell_edit_mode_from_header(self, axis: str, index: int) -> None:
        if not self._rows or not self._cols:
            if self.isVisible():
                self.setFocus()
            return
        print(
            f"DEBUG edit_mode_switch: label -> cell trigger=enter_no_next_label "
            f"axis={axis} index={index}"
        )
        if axis == "row":
            self._sel_row = max(0, min(index, len(self._rows) - 1))
            if self._sel_col < 0 or self._sel_col >= len(self._cols):
                self._sel_col = 0
        elif axis == "col":
            self._sel_col = max(0, min(index, len(self._cols) - 1))
            if self._sel_row < 0 or self._sel_row >= len(self._rows):
                self._sel_row = 0
        DEBUG_GUI and print(f"DEBUG enter_cell_from_header: before_clamp row={self._sel_row} col={self._sel_col}")
        self._clamp_selection_to_leaf()
        DEBUG_GUI and print(f"DEBUG enter_cell_from_header: after_clamp row={self._sel_row} col={self._sel_col}")
        DEBUG_GUI and print(f"DEBUG SET CELL: line {__import__('inspect').currentframe().f_lineno} prev={self._sel_mode}"); self._sel_mode = "cell"
        self._sel_indices.clear()
        self._anchor_row, self._anchor_col = self._sel_row, self._sel_col
        self._hide_editor()
        self.selection_changed.emit()
        self.viewport().update()
        self._request_repaint("enter_cell_from_header")
        print(
            f"DEBUG edit_mode_switch: cell_focus_restored row={self._sel_row} col={self._sel_col}"
        )

    def _row_label_at(self, index: int) -> str:
        """Get display label for row at given index."""
        return self._geometry.row_label_at(index)

    def _col_label_at(self, index: int) -> str:
        """Get display label for column at given index."""
        return self._geometry.col_label_at(index)

    def focus_location_description(self) -> str | None:
        DEBUG_GUI and print(f"DEBUG focus_desc: _sel_mode={self._sel_mode} _sel_row={self._sel_row} _sel_col={self._sel_col} _sel_indices={self._sel_indices}")
        if self._header_edit_ctx is not None:
            axis = self._header_edit_ctx.get("axis")
            index = int(self._header_edit_ctx.get("index", -1))
            label = (
                self._row_label_at(index)
                if axis == "row"
                else self._col_label_at(index)
            )
            prefix = "Row Header" if axis == "row" else "Col Header"
            return f"{prefix}: {label} ({self.edit_mode_label()})"
        if self._sel_mode == "cell":
            row_label = self._row_label_at(self._sel_row)
            col_label = self._col_label_at(self._sel_col)
            return f"Cell: {row_label} × {col_label} ({self.edit_mode_label()})"
        if self._sel_mode == "row":
            return f"Row: {self._row_label_at(self._sel_row)} ({self.edit_mode_label()})"
        if self._sel_mode == "col":
            return f"Col: {self._col_label_at(self._sel_col)} ({self.edit_mode_label()})"
        return f"Navigation ({self.edit_mode_label()})"

    def _commit_header_editor(self, *, move_next: bool = False, move_prev: bool = False) -> None:
        """Commit header editor changes with optional navigation."""
        self._header_edit.commit_header_editor(move_next=move_next, move_prev=move_prev)

    def _commit_editor(self, *, fill_selection: bool = False) -> None:
        if not self._editor.isVisible():
            return
        if self._header_edit_ctx is not None:
            self._commit_header_editor(move_next=False)
            return
        text = self._editor.text()
        self._hide_editor()
        applied = False
        try:
            if not self._rows[self._sel_row].get("is_leaf", False):
                return
            
            is_rule = text.startswith("=") or "=" in text
            
            # Determine which cells to fill
            if fill_selection and self._sel_indices:
                target_cells = list(self._sel_indices)
            else:
                target_cells = [(self._sel_row, self._sel_col)]
            
            # Filter to only leaf cells
            target_cells = [
                (r, c) for r, c in target_cells 
                if 0 <= r < len(self._rows) and self._rows[r].get("is_leaf", False)
                and 0 <= c < len(self._cols)
            ]
            
            if not target_cells:
                return
            
            # Route cell fill through command spine
            for r, c in target_cells:
                row_key = self._row_keys[self._leaf_row_index(r)]
                col_key = self._col_keys[c]

                if is_rule:
                    expr = text[1:] if text.startswith("=") else text
                    self._session.execute(
                        "set_cell_rule_by_keys",
                        view_id=self._view_id,
                        row_key=row_key,
                        col_key=col_key,
                        expression=expr,
                    )
                else:
                    val = coerce_user_value(text)
                    self._session.execute(
                        "set_cell_value",
                        view_id=self._view_id,
                        cell_ref={"kind": "keys", "value": {"row_key": row_key, "col_key": col_key}},
                        value=val,
                    )
            applied = True

            # Emit signal for recording (emit first cell for single edits)
            if target_cells and not fill_selection:
                r, c = target_cells[0]
                self.cell_value_changed.emit(r, c, text)

            print(
                f"[DEBUG] multi-cell fill: view={self._view_id}, "
                f"cells={len(target_cells)}, rule={is_rule}, text={text!r}"
            )

        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Rule error", str(e))
            if self.isVisible():
                self.setFocus()
            return
        if applied:
            # Cache committed values so paintEvent can draw them immediately
            # before background tile fetch completes.
            for r, c in target_cells:
                self._pending_cell_values[(r, c)] = val if not is_rule else text
            # Signal will trigger recompute and view refresh in app.py
            self.content_changed.emit()

        if self.isVisible():
            self.setFocus()

    # ------------------------------------------------------------
    # Integration helpers
    # ------------------------------------------------------------

    def selected_rc(self) -> tuple[int, int]:
        """Return current selection as (row, col) tuple."""
        return self._selection.get_selected_rc()

    def selected_keys(self) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
        """Return current selection as (row_key, col_key) tuple."""
        return self._selection.get_selected_keys()

    def selected_cell_coords(self, visible_only: bool = True) -> list[tuple[int, int]]:
        """Return display row/col pairs for the current selection.
        
        Args:
            visible_only: If True (default), only return cells in the visible viewport.
                         This is O(1) and prevents GUI freeze with large selections.
                         Set to False if you truly need ALL cells (e.g., for copy/paste).
        """
        if visible_only:
            return list(self._iter_visible_selected_cell_coords())
        # Full iteration - may be slow for large selections
        return list(self._iter_selected_cell_coords())

    def selected_cell_coords_all(self) -> list[tuple[int, int]]:
        """Return ALL selected cell coordinates - may be slow for large selections.
        Use this only when you truly need all cells (e.g., copy/paste operations).
        For UI operations, use selected_cell_coords() with default visible_only=True.
        """
        return list(self._iter_selected_cell_coords())

    def _iter_visible_selected_cell_coords(self):
        """Generator that yields only visible (r, c) tuples - O(1) for UI responsiveness."""
        # Calculate visible range
        first_row = self.verticalScrollBar().value() // self._m.row_h
        last_row = first_row + self.viewport().height() // self._m.row_h + 2
        first_col = self.horizontalScrollBar().value() // 80
        last_col = first_col + self.viewport().width() // 80 + 2
        
        if self._sel_mode == "cell":
            for item in self._sel_indices:
                if isinstance(item, tuple) and len(item) == 2:
                    r, c = item[0], item[1]
                    if (0 <= r < len(self._rows) and 0 <= c < len(self._cols) and
                        first_row <= r <= last_row and first_col <= c <= last_col):
                        yield (r, c)
            if (first_row <= self._sel_row <= last_row and 
                first_col <= self._sel_col <= last_col):
                yield (self._sel_row, self._sel_col)
        elif self._sel_mode == "row":
            for r in self._sel_indices:
                if first_row <= r <= last_row and 0 <= r < len(self._rows):
                    for c in range(max(0, first_col), min(len(self._cols), last_col)):
                        yield (r, c)
        elif self._sel_mode == "col":
            for c in self._sel_indices:
                if first_col <= c <= last_col and 0 <= c < len(self._cols):
                    for r in range(max(0, first_row), min(len(self._rows), last_row)):
                        yield (r, c)
        elif self._sel_mode == "all":
            for r in range(max(0, first_row), min(len(self._rows), last_row + 1)):
                for c in range(max(0, first_col), min(len(self._cols), last_col + 1)):
                    yield (r, c)
        else:
            if (first_row <= self._sel_row <= last_row and 
                first_col <= self._sel_col <= last_col and
                0 <= self._sel_row < len(self._rows) and 0 <= self._sel_col < len(self._cols)):
                yield (self._sel_row, self._sel_col)

    def _iter_selected_cell_coords(self):
        """Generator that yields (r, c) tuples lazily - no massive list buildup."""
        if self._sel_mode == "cell":
            seen = set()
            for item in self._sel_indices:
                if isinstance(item, tuple) and len(item) == 2:
                    r, c = item[0], item[1]
                    if (r, c) not in seen and 0 <= r < len(self._rows) and 0 <= c < len(self._cols):
                        seen.add((r, c))
                        yield (r, c)
            if (self._sel_row, self._sel_col) not in seen:
                if 0 <= self._sel_row < len(self._rows) and 0 <= self._sel_col < len(self._cols):
                    yield (self._sel_row, self._sel_col)
        elif self._sel_mode == "row":
            for r in sorted(self._sel_indices):
                if not (0 <= r < len(self._rows)):
                    continue
                for c in range(len(self._cols)):
                    yield (r, c)
        elif self._sel_mode == "col":
            for c in sorted(self._sel_indices):
                if not (0 <= c < len(self._cols)):
                    continue
                for r in range(len(self._rows)):
                    yield (r, c)
        elif self._sel_mode == "all":
            for r in range(len(self._rows)):
                for c in range(len(self._cols)):
                    yield (r, c)
        else:
            if 0 <= self._sel_row < len(self._rows) and 0 <= self._sel_col < len(self._cols):
                yield (self._sel_row, self._sel_col)

    def selected_cell_keys_many(self, visible_only: bool = True) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
        """Return (row_key, col_key) for selected data cells.
        
        Args:
            visible_only: If True (default), only return cells in the visible viewport.
                         This is O(1) and prevents GUI freeze with large selections.
        """
        keys: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        for r, c in self.selected_cell_coords(visible_only=visible_only):
            if not (0 <= r < len(self._rows)) or not (0 <= c < len(self._cols)):
                continue
            if not self._rows[r].get("is_leaf", False):
                continue
            leaf_i = self._leaf_row_index(r)
            if not (0 <= leaf_i < len(self._row_keys)):
                continue
            if not (0 <= c < len(self._col_keys)):
                continue
            keys.append((self._row_keys[leaf_i], self._col_keys[c]))
        return keys

    def selected_cell_keys_many_all(self) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
        """Return ALL selected cell keys - may be slow for large selections.
        Use this only for operations that truly need all cells (e.g., copy/paste).
        """
        return self.selected_cell_keys_many(visible_only=False)

    def selected_cell_count(self) -> int:
        """Return the number of selected cells - O(1) calculation, no iteration.
        
        This gives the accurate total count without freezing the GUI.
        """
        if self._sel_mode == "cell":
            # Count stored indices plus active cell (if not already in set)
            count = 0
            seen_active = False
            for item in self._sel_indices:
                if isinstance(item, tuple) and len(item) == 2:
                    count += 1
                    if item[0] == self._sel_row and item[1] == self._sel_col:
                        seen_active = True
            if not seen_active:
                count += 1
            return count
        elif self._sel_mode == "row":
            # Each selected row has all columns
            def _row_idx(item):
                return item[0] if isinstance(item, tuple) else item
            valid_rows = sum(1 for r in self._sel_indices if 0 <= _row_idx(r) < len(self._rows))
            return valid_rows * len(self._cols)
        elif self._sel_mode == "col":
            # Each selected column has all rows
            def _col_idx(item):
                return item[1] if isinstance(item, tuple) else item
            valid_cols = sum(1 for c in self._sel_indices if 0 <= _col_idx(c) < len(self._cols))
            return valid_cols * len(self._rows)
        else:
            return 1

    def selected_addresses(self) -> list[str]:
        """Return semantic address strings for the current selection.

        Each string has the form ``Cube::Dim.Item:Dim.Item`` with ``*``
        used for dimensions that are not explicitly selected:

        - **cell** mode → all dimensions constrained
        - **row** mode → row dimensions constrained, col dimensions = ``*``
        - **col** mode → col dimensions constrained, row dimensions = ``*``

        This format is universal: the ``rule`` command, ``set`` command,
        and copy/paste can all consume it.
        """
        if not self._view_id:
            return []

        try:
            view = self._workspace_read_model.get_view(self._view_id)
            if view is None:
                return []
            cube = self._workspace_read_model.get_cube(view.get("cube_id"))
        except Exception:
            return []

        if cube is None:
            return []

        cube_name = cube.get("name", "")
        row_dim_ids = list(view.get("row_dim_ids", []) or [])
        col_dim_ids = list(view.get("col_dim_ids", []) or [])
        all_dim_ids = list(cube.get("dimension_ids", []))

        # Build name lookup maps
        dim_names: dict[str, str] = {}
        item_names: dict[str, dict[str, str]] = {}
        for dim_id in all_dim_ids:
            dim = self._workspace_read_model.get_dimension(dim_id)
            if dim is None:
                continue
            dim_names[dim_id] = dim.get("name", dim_id)
            item_names[dim_id] = {it["id"]: it["name"] for it in dim.get("items", [])}

        def _format_key(key: tuple[str, ...], dim_ids: list[str]) -> list[str]:
            """Convert a key tuple to ['Dim.Item', ...] strings."""
            parts = []
            for dim_id, item_id in zip(dim_ids, key):
                dname = dim_names.get(dim_id, dim_id)
                iname = item_names.get(dim_id, {}).get(item_id, item_id)
                parts.append(f"{dname}.{iname}")
            return parts

        results: list[str] = []

        if self._sel_mode == "cell":
            for r, c in self.selected_cell_coords_all():
                if not (0 <= r < len(self._rows) and 0 <= c < len(self._cols)):
                    continue
                row_key = self._row_keys[r] if r < len(self._row_keys) else ()
                col_key = self._col_keys[c] if c < len(self._col_keys) else ()
                parts = _format_key(row_key, row_dim_ids) + _format_key(col_key, col_dim_ids)
                if parts:
                    results.append(f"{cube_name}::{':'.join(parts)}")

        elif self._sel_mode == "row":
            for r in sorted(idx for idx in self._sel_indices if isinstance(idx, int)):
                if not (0 <= r < len(self._rows) and r < len(self._row_keys)):
                    continue
                row_key = self._row_keys[r]
                parts = _format_key(row_key, row_dim_ids)
                # Wildcard all column dimensions
                for dim_id in col_dim_ids:
                    dname = dim_names.get(dim_id, dim_id)
                    parts.append(f"{dname}.*")
                if parts:
                    results.append(f"{cube_name}::{':'.join(parts)}")

        elif self._sel_mode == "col":
            for c in sorted(idx for idx in self._sel_indices if isinstance(idx, int)):
                if not (0 <= c < len(self._cols) and c < len(self._col_keys)):
                    continue
                col_key = self._col_keys[c]
                parts = []
                # Wildcard all row dimensions
                for dim_id in row_dim_ids:
                    dname = dim_names.get(dim_id, dim_id)
                    parts.append(f"{dname}.*")
                parts.extend(_format_key(col_key, col_dim_ids))
                if parts:
                    results.append(f"{cube_name}::{':'.join(parts)}")

        elif self._sel_mode == "all":
            # Whole-grid selection → wildcard rows/cols, fix page dims to current values
            parts = []
            for dim_id in row_dim_ids:
                dname = dim_names.get(dim_id, dim_id)
                parts.append(f"{dname}.*")
            for dim_id in col_dim_ids:
                dname = dim_names.get(dim_id, dim_id)
                parts.append(f"{dname}.*")
            # Non-technical page dimensions get their current page item (not wildcard)
            page_dim_ids = [d for d in all_dim_ids if d not in row_dim_ids and d not in col_dim_ids and d != "@"]
            for dim_id in page_dim_ids:
                dname = dim_names.get(dim_id, dim_id)
                page_result = self._session.query(
                    "page_selection", view_id=self._view_id, dim_id=dim_id
                )
                current_item_id = page_result.get("item_id") if page_result else None
                iname = item_names.get(dim_id, {}).get(current_item_id, current_item_id)
                parts.append(f"{dname}.{iname}")
            if parts:
                results.append(f"{cube_name}::{':'.join(parts)}")

        return results
