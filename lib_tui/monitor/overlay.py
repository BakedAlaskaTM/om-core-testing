"""BusMonitorOverlay — transient two-pane overlay for the REPL TUI.

Replaces the output pane (between top label bar and bottom status bar)
with a topic-checklist / message-log split while active.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, TextIO, TYPE_CHECKING

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.mouse_events import MouseEventType

from .state import MonitorState
from .render import render_topics_text, render_log_text

if TYPE_CHECKING:
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import Container


class MonitorBufferControl(BufferControl):
    """BufferControl that skips prompt_toolkit's right-click context menu.

    When mouse support is on, prompt_toolkit's default context menu
    moves the cursor on right-click, which clears terminal text selection.
    This subclass ignores right-clicks so the terminal can show its
    own context menu (if supported) without clearing the selection.

    Also supports an optional on_click callback for left-clicks,
    called after the default cursor positioning.
    """

    def __init__(self, buffer, *args, on_click=None, **kwargs):
        super().__init__(buffer, *args, **kwargs)
        self.on_click = on_click

    def mouse_handler(self, mouse_event):
        # Scroll wheel: move buffer cursor so _update_buffers can detect bottom
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            if mouse_event.button == 4:
                self.buffer.cursor_up()
                return None
            if mouse_event.button == 5:
                self.buffer.cursor_down()
                return None
            if mouse_event.button == 2:
                return NotImplemented

        result = super().mouse_handler(mouse_event)

        if (
            mouse_event.event_type == MouseEventType.MOUSE_DOWN
            and mouse_event.button == 0
            and self.on_click is not None
        ):
            self.on_click()

        return result


class MonitorBuffer(Buffer):
    """Buffer that rejects user typing but allows programmatic text updates."""

    def insert_text(
        self, data: str, overwrite: bool = False, move_cursor: bool = True, fire_event: bool = True
    ) -> None:
        # Swallow all typed input; programmatic .text = ... still works.
        pass


class BusMonitorOverlay:
    """Manages the monitor overlay layout, focus, and visibility."""

    def __init__(self, app: "Application", log_file: TextIO | None = None) -> None:
        self.app = app
        self.visible = False
        self.state = MonitorState()
        self.state.set_change_callback(self._on_state_change)
        self._log_file = log_file
        self._log_lock = threading.Lock() if log_file is not None else None

        # Build scrollable BufferControl panes
        self.left_buffer = MonitorBuffer()
        self.right_buffer = MonitorBuffer()

        self.left_pane = Window(
            content=MonitorBufferControl(
                buffer=self.left_buffer, focusable=True, focus_on_click=True,
                on_click=self.action_click_toggle,
            ),
            width=40,
            wrap_lines=False,
            right_margins=[ScrollbarMargin(display_arrows=True)],
        )
        self.right_pane = Window(
            content=MonitorBufferControl(
                buffer=self.right_buffer, focusable=True, focus_on_click=True
            ),
            wrap_lines=False,
            right_margins=[ScrollbarMargin(display_arrows=True)],
        )

        # Pre-populate buffers so they aren't empty on first show
        self._update_buffers()

        # Monitor label bar inside the overlay (top of the overlay area)
        self.overlay_label = Window(
            content=FormattedTextControl(" Bus Monitor "),
            height=1,
            style="class:monitor-label",
            dont_extend_height=True,
        )

        # Footer hints inside overlay
        self.overlay_footer = Window(
            content=FormattedTextControl(
                " Esc/F2 close | Tab switch pane | Space toggle | ↑/↓ nav | PgUp/PgDn scroll | </> resize " + (" | LOG on" if self._log_file is not None else "")
            ),
            height=1,
            style="class:monitor-footer",
            dont_extend_height=True,
        )

        # The full overlay layout (replaces output pane + status line area)
        self.container: Container = HSplit([
            self.overlay_label,
            VSplit([
                self.left_pane,
                Window(width=1, char="│", style="class:monitor-divider"),
                self.right_pane,
            ]),
            self.overlay_footer,
        ])

    # ------------------------------------------------------------------
    # Visibility
    # ------------------------------------------------------------------

    def show(self) -> None:
        self.visible = True
        self.app.layout.focus(self.left_pane)
        self._invalidate()

    def hide(self) -> None:
        self.visible = False
        self._invalidate()

    def toggle(self) -> None:
        if self.visible:
            self.hide()
        else:
            self.show()

    # ------------------------------------------------------------------
    # Message reception
    # ------------------------------------------------------------------

    def on_bus_event(self, event: Any) -> None:
        """Callback passed to CommandSession.watch_all().

        Must be thread-safe — bus callbacks may arrive on any thread.
        """
        try:
            topic = getattr(event, "topic", "")
            payload = getattr(event, "payload", event)
        except Exception:
            topic = "unknown"
            payload = str(event)

        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        text = f"{topic} → {str(payload)}"
        self.state.add_message(ts, topic, text)

        if self._log_file is not None and self._log_lock is not None:
            full_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            line = f"{full_ts}  {text}\n"
            with self._log_lock:
                try:
                    self._log_file.write(line)
                    self._log_file.flush()
                except Exception:
                    pass

        # Refresh the overlay from the event loop thread (safe from any thread)
        def _refresh():
            self._update_buffers()
            self._invalidate()

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(_refresh)
                return
        except Exception:
            pass
        _refresh()

    # ------------------------------------------------------------------
    # Navigation / actions
    # ------------------------------------------------------------------

    def action_toggle_current(self) -> None:
        """Space: toggle the topic at cursor (or [All])."""
        if self.state.cursor == 0:
            # [All] row
            if self.state.are_all_enabled():
                self.state.disable_all()
            else:
                self.state.enable_all()
        else:
            topic = self.state.topic_at_cursor()
            if topic:
                self.state.toggle_topic(topic)

    def action_click_toggle(self) -> None:
        """Mouse click on left pane: move cursor to clicked line and toggle."""
        text = self.left_buffer.text
        pos = self.left_buffer.cursor_position
        line = text[:pos].count("\n")
        self.state.cursor = line
        self.action_toggle_current()

    def action_next_focus(self) -> None:
        """Tab: cycle focus between left pane and right pane."""
        current = self.app.layout.current_window
        if current == self.left_pane:
            self.app.layout.focus(self.right_pane)
        else:
            self.app.layout.focus(self.left_pane)

    def action_cursor_up(self) -> None:
        self.state.cursor_up()

    def action_cursor_down(self) -> None:
        self.state.cursor_down()

    def action_resize_left(self) -> None:
        self.state.resize_left()

    def action_resize_right(self) -> None:
        self.state.resize_right()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_state_change(self) -> None:
        """Called whenever topic list or enabled set changes."""
        self._update_buffers()
        self._update_width()
        self._invalidate()

    def _update_buffers(self) -> None:
        """Rebuild buffer text from current state."""
        left_text = render_topics_text(self.state)

        # Preserve right pane cursor and selection across text updates
        old_text = self.right_buffer.text
        right_cursor = self.right_buffer.cursor_position
        right_selection = self.right_buffer.selection_state
        old_total_lines = old_text.count("\n")
        # Cursor-based: within 5 lines or at the end
        old_cursor_line = old_text[:right_cursor].count("\n")
        cursor_at_bottom = (
            right_cursor >= len(old_text) - 1
            or old_cursor_line >= (old_total_lines - 5)
        )
        # Visual-based: the viewport shows the tail (within last 5 lines)
        render_info = self.right_pane.render_info
        visual_at_bottom = False
        if render_info is not None:
            visual_at_bottom = render_info.last_visible_line() >= (old_total_lines - 5)
        was_at_bottom = cursor_at_bottom or visual_at_bottom

        self.left_buffer.text = left_text
        self.right_buffer.text = render_log_text(self.state)

        # If user was viewing the bottom, keep them at the bottom; otherwise preserve position
        new_len = len(self.right_buffer.text)
        if was_at_bottom:
            self.right_buffer.cursor_position = new_len
        else:
            self.right_buffer.cursor_position = min(right_cursor, new_len)
        self.right_buffer.selection_state = right_selection

        # Sync left pane cursor to the '>' indicator line
        lines = left_text.split("\n")
        cursor_line = self.state.cursor
        pos = 0
        for i, line in enumerate(lines):
            if i == cursor_line:
                break
            pos += len(line) + 1  # +1 for newline
        self.left_buffer.cursor_position = pos

    def _update_width(self) -> None:
        """Apply current left pane width."""
        self.left_pane.width = self.state.left_width

    def _invalidate(self) -> None:
        """Trigger a prompt_toolkit re-render."""
        if hasattr(self.app, "invalidate"):
            self.app.invalidate()
