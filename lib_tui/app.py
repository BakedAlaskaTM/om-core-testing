"""prompt_toolkit Application-based TUI for OpenM.

Layout (top-to-bottom inside HSplit):
  - output pane   (flexible height, scrollable, read-only)
  - status line   (fixed height=1)
  - input line    (fixed height=1, "om> " prefix)

The prompt sits at the absolute bottom.  Completions unfold upward.
All output is routed into the output pane; print() is never used.
"""
from __future__ import annotations

import io
import logging
import os
import re
import signal
import sys
import textwrap
import threading
import time
from typing import TextIO, TYPE_CHECKING

from datetime import datetime

import asyncio

import blessed

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.cursor_shapes import CursorShape
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, DynamicContainer, Float, FloatContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.filters import Condition
from . import config as cfg
from .monitor import BusMonitorOverlay

if TYPE_CHECKING:
    from lib_repl import OpenMREPL

logger = logging.getLogger(__name__)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


class _StdoutProxy:
    """Captures writes and forwards them into a callback."""

    def __init__(self, callback):
        self._cb = callback
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._cb(line)
        return len(text)

    def flush(self) -> None:
        if self._buf:
            self._cb(self._buf)
            self._buf = ""

    def isatty(self) -> bool:
        return False


class _CmdCompleter(Completer):
    """Bridge cmd.Cmd completion methods to prompt_toolkit."""

    def __init__(self, cmd_instance) -> None:
        self.cmd = cmd_instance

    def get_completions(self, document, complete_event):
        text_before = document.text_before_cursor
        if not text_before:
            return
        line = text_before.lstrip()
        stripped = len(text_before) - len(line)
        last_space = text_before.rfind(" ")
        if last_space == -1:
            begidx = 0
            endidx = len(line)
            text = line
        elif last_space == len(text_before) - 1:
            begidx = len(line)
            endidx = begidx
            text = ""
        else:
            word_start = last_space + 1
            begidx = word_start - stripped
            endidx = len(line)
            text = text_before[word_start:]
        if begidx == 0:
            compfunc = self.cmd.completenames
        else:
            cmd_name, _, _ = self.cmd.parseline(line)
            if not cmd_name:
                compfunc = self.cmd.completedefault
            elif cmd_name == "help":
                compfunc = self.cmd.complete_help
            elif hasattr(self.cmd, "complete_" + cmd_name):
                compfunc = getattr(self.cmd, "complete_" + cmd_name)
            else:
                compfunc = self.cmd.completedefault
        matches = compfunc(text, line, begidx, endidx)
        if not matches:
            return
        for match in matches:
            if match.startswith(text):
                yield Completion(match, start_position=-len(text))


class PromptToolkitTUI:
    """Full-screen TUI built on prompt_toolkit.Application."""

    def __init__(self, repl: "OpenMREPL", monitor_log_file: TextIO | None = None) -> None:
        self.repl = repl
        self._monitor_log_file = monitor_log_file
        self._running = True
        self._busy = False
        self._busy_command: str | None = None
        self._loop = None

        # -- output pane --------------------------------------------------
        self.output_buffer = Buffer()
        self.output_window = Window(
            content=BufferControl(buffer=self.output_buffer),
            height=Dimension(min=1, weight=1),
            wrap_lines=False,
            right_margins=[ScrollbarMargin(display_arrows=True)],
        )

        # -- status line --------------------------------------------------
        self.status_window = VSplit([
            Window(
                content=FormattedTextControl(self._status_fragments),
                height=1,
                style="class:status",
                dont_extend_height=True,
            ),
            Window(
                content=FormattedTextControl(self._version_text),
                height=1,
                style="class:status",
                dont_extend_height=True,
                dont_extend_width=True,
            ),
        ], height=1)

        # -- input line ---------------------------------------------------
        # History: try the REPL's own history builder, fall back to empty
        history = InMemoryHistory()
        if hasattr(repl, "_make_prompt_history"):
            try:
                history = repl._make_prompt_history()
            except Exception:
                pass

        self.cmd_buffer = Buffer(
            multiline=False,
            accept_handler=self._on_accept,
            completer=_CmdCompleter(repl),
            # Must match the REPL's PromptSession default: completions only on
            # Tab, not on every keystroke.  Setting True floods the remote
            # engine with completion queries (dimension_list, cube_list, etc.)
            # on each keypress, overwhelming the single-threaded server.
            complete_while_typing=False,
            history=history,
        )
        repl._prompt_history = history

        def _prompt_prefix(line: int, wrap_count: int):
            # Only the first line (the input line) gets the prompt prefix.
            if line == 0:
                return [("class:om-prompt", "om> ")]
            return []

        self.input_window = Window(
            content=BufferControl(
                buffer=self.cmd_buffer,
            ),
            height=1,
            wrap_lines=False,
            dont_extend_height=True,
            get_line_prefix=_prompt_prefix,
        )

        # -- key bindings -------------------------------------------------
        self.kb = KeyBindings()
        self._setup_keys()

        # -- monitor overlay (placeholder; created after self.app) -------
        self.monitor: BusMonitorOverlay | None = None
        self._monitor_sub_handle: object | None = None

        # -- top label bar -----------------------------------------------
        self.label_bar = VSplit([
            Window(
                content=FormattedTextControl(
                    [("class:label-bar bold", " OM Core ", self._on_omcore_label_click)],
                ),
                height=1,
                style="class:label-bar",
                dont_extend_height=True,
                dont_extend_width=True,
            ),
            Window(
                content=FormattedTextControl(
                    lambda: [(
                        "class:monitor-label" if (self.monitor is not None and self.monitor.visible) else "class:label-bar",
                        " [Monitor(F2)] ",
                        self._on_monitor_label_click,
                    )],
                ),
                height=1,
                style="class:label-bar",
                dont_extend_height=True,
                dont_extend_width=True,
            ),
            Window(width=Dimension(weight=1), style="class:label-bar"),  # spacer
            Window(
                content=FormattedTextControl(lambda: [("class:label-bar bold", self._clock_text())]),
                height=1,
                style="class:label-bar",
                dont_extend_height=True,
                dont_extend_width=True,
            ),
        ], height=1)

        # -- layout -------------------------------------------------------
        self._main_body = HSplit([
            self.output_window,
            self.status_window,
            self.input_window,
        ])

        def _get_body():
            if self.monitor is not None and self.monitor.visible:
                return self.monitor.container
            return self._main_body

        body = HSplit([
            self.label_bar,
            DynamicContainer(_get_body),
        ])

        root = FloatContainer(
            body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(),
                ),
            ],
        )

        style = Style.from_dict({
            "status":        f"{cfg.STATUS_FG} bg:{cfg.STATUS_BG}",
            "om-prompt":     f"{cfg.PROMPT_FG} bg:{cfg.PROMPT_BG} bold",
            "completion-menu":         f"{cfg.COMPLETION_FG} bg:{cfg.COMPLETION_BG}",
            "completion-menu.completion": f"{cfg.COMPLETION_FG} bg:{cfg.COMPLETION_BG}",
            "completion-menu.completion.current": f"{cfg.COMPLETION_SELECTED_FG} bg:{cfg.COMPLETION_SELECTED_BG}",
            "label-bar":    f"{cfg.LABEL_FG} bg:{cfg.LABEL_BG}",
            "monitor-label":     f"{cfg.MONITOR_LABEL_FG} bg:{cfg.MONITOR_LABEL_BG}",
            "monitor-selected":  f"{cfg.MONITOR_SELECTED_FG} bg:{cfg.MONITOR_SELECTED_BG}",
            "monitor-normal":    f"{cfg.MONITOR_NORMAL_FG} bg:{cfg.MONITOR_NORMAL_BG}",
            "monitor-log":       f"{cfg.MONITOR_LOG_FG} bg:{cfg.MONITOR_LOG_BG}",
            "monitor-footer":    f"{cfg.MONITOR_FOOTER_FG} bg:{cfg.MONITOR_FOOTER_BG}",
            "monitor-divider":   f"{cfg.MONITOR_DIVIDER_FG} bg:{cfg.MONITOR_DIVIDER_BG}",
        })

        self.app = Application(
            layout=Layout(root, focused_element=self.input_window),
            key_bindings=self.kb,
            full_screen=True,
            style=style,
            mouse_support=cfg.MOUSE_SUPPORT,
            cursor=CursorShape.BLINKING_BLOCK,
            refresh_interval=1,
        )

        # -- create monitor overlay now that self.app exists ---------------
        self.monitor = BusMonitorOverlay(self.app, log_file=self._monitor_log_file)

        # -- splash banner --------------------------------------------------
        self._draw_banner()

    # ------------------------------------------------------------------
    # Key bindings
    # ------------------------------------------------------------------

    def _setup_keys(self) -> None:
        @self.kb.add("c-c")
        def _exit(event):
            self._running = False
            event.app.exit()

        @self.kb.add("c-d")
        def _eof(event):
            self._running = False
            event.app.exit()

        @self.kb.add("tab")
        def _complete(event):
            event.app.current_buffer.complete_next()

        @self.kb.add("pageup")
        def _pageup(event):
            if self.monitor is not None and self.monitor.visible:
                current = event.app.layout.current_window
                if current == self.monitor.left_pane:
                    try:
                        rows, _ = os.get_terminal_size()
                        page = max(1, rows - 5)
                    except Exception:
                        page = 10
                    for _ in range(page):
                        self.monitor.left_buffer.cursor_up()
                    text = self.monitor.left_buffer.text
                    pos = self.monitor.left_buffer.cursor_position
                    line = text[:pos].count("\n")
                    self.monitor.state.cursor = line
                    return
                elif current == self.monitor.right_pane:
                    buf = self.monitor.right_buffer
                else:
                    buf = self.output_buffer
            else:
                buf = self.output_buffer
            try:
                rows, _ = os.get_terminal_size()
                page = max(1, rows - 3)
            except Exception:
                page = 10
            for _ in range(page):
                buf.cursor_up()

        @self.kb.add("pagedown")
        def _pagedown(event):
            if self.monitor is not None and self.monitor.visible:
                current = event.app.layout.current_window
                if current == self.monitor.left_pane:
                    try:
                        rows, _ = os.get_terminal_size()
                        page = max(1, rows - 5)
                    except Exception:
                        page = 10
                    for _ in range(page):
                        self.monitor.left_buffer.cursor_down()
                    text = self.monitor.left_buffer.text
                    pos = self.monitor.left_buffer.cursor_position
                    line = text[:pos].count("\n")
                    self.monitor.state.cursor = line
                    return
                elif current == self.monitor.right_pane:
                    buf = self.monitor.right_buffer
                else:
                    buf = self.output_buffer
            else:
                buf = self.output_buffer
            try:
                rows, _ = os.get_terminal_size()
                page = max(1, rows - 3)
            except Exception:
                page = 10
            for _ in range(page):
                buf.cursor_down()

        @self.kb.add("f12")
        def _toggle_mouse(event):
            app = event.app
            app.mouse_support = not app.mouse_support
            # Emit escape sequences directly so the terminal reacts immediately
            try:
                out = app.renderer.output
                if app.mouse_support:
                    out.enable_mouse_support()
                else:
                    out.disable_mouse_support()
            except Exception:
                pass
            state = "ON" if app.mouse_support else "OFF"
            self._append_text(f"[Mouse support: {state}]")

        # -- monitor overlay keybindings --------------------------------
        @self.kb.add("f2")
        def _toggle_monitor(event):
            if self.monitor is None:
                return
            self.monitor.toggle()
            if not self.monitor.visible:
                event.app.layout.focus(self.input_window)

        @self.kb.add("escape")
        def _monitor_esc(event):
            if self.monitor is not None and self.monitor.visible:
                self.monitor.hide()
                event.app.layout.focus(self.input_window)

        @self.kb.add("tab")
        def _monitor_tab(event):
            if self.monitor is not None and self.monitor.visible:
                self.monitor.action_next_focus()
            else:
                event.app.current_buffer.complete_next()

        @Condition
        def _monitor_visible():
            return self.monitor is not None and self.monitor.visible

        @self.kb.add("space", filter=_monitor_visible)
        def _monitor_space(event):
            if self.monitor is not None and self.monitor.visible:
                self.monitor.action_toggle_current()

        @self.kb.add("up", filter=_monitor_visible)
        def _monitor_up(event):
            if self.monitor is not None and self.monitor.visible:
                self.monitor.action_cursor_up()

        @self.kb.add("down", filter=_monitor_visible)
        def _monitor_down(event):
            if self.monitor is not None and self.monitor.visible:
                self.monitor.action_cursor_down()

        @self.kb.add("k", filter=_monitor_visible)
        def _monitor_k(event):
            if self.monitor is not None and self.monitor.visible:
                self.monitor.action_cursor_up()

        @self.kb.add("j", filter=_monitor_visible)
        def _monitor_j(event):
            if self.monitor is not None and self.monitor.visible:
                self.monitor.action_cursor_down()

        @self.kb.add("<", filter=_monitor_visible)
        def _monitor_shrink(event):
            if self.monitor is not None and self.monitor.visible:
                self.monitor.action_resize_left()

        @self.kb.add(">", filter=_monitor_visible)
        def _monitor_grow(event):
            if self.monitor is not None and self.monitor.visible:
                self.monitor.action_resize_right()

        # -- shift+arrow selection in right pane / navigation in left pane --
        @Condition
        def _monitor_right_focused():
            return (
                self.monitor is not None
                and self.monitor.visible
                and self.app.layout.current_window == self.monitor.right_pane
            )

        @Condition
        def _monitor_left_focused():
            return (
                self.monitor is not None
                and self.monitor.visible
                and self.app.layout.current_window == self.monitor.left_pane
            )

        @self.kb.add("G", filter=_monitor_right_focused)
        def _monitor_jump_bottom(event):
            buf = self.monitor.right_buffer
            buf.cursor_position = len(buf.text)

        @self.kb.add(Keys.End, filter=_monitor_right_focused)
        def _monitor_end(event):
            buf = self.monitor.right_buffer
            buf.cursor_position = len(buf.text)

        def _copy_to_system_clipboard(buf):
            """Copy selected text from *buf* to the OS clipboard."""
            if buf.selection_state is None:
                return
            a = buf.selection_state.original_cursor_position
            b = buf.cursor_position
            from_pos, to_pos = (a, b) if a < b else (b, a)
            text = buf.text[from_pos:to_pos]
            if not text:
                return
            import subprocess
            try:
                import pyperclip
                pyperclip.copy(text)
                return
            except Exception:
                pass
            for cmd in (
                ["xclip", "-selection", "clipboard"],
                ["wl-copy"],
            ):
                try:
                    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                    p.communicate(text.encode("utf-8"))
                    return
                except Exception:
                    continue

        @self.kb.add(Keys.ControlInsert, filter=_monitor_right_focused)
        def _monitor_copy(event):
            _copy_to_system_clipboard(self.monitor.right_buffer)

    def _on_omcore_label_click(self, mouse_event) -> None:
        """Clicking the OM Core label hides the monitor (Esc equivalent)."""
        if (
            mouse_event.event_type == MouseEventType.MOUSE_DOWN
            and self.monitor is not None
            and self.monitor.visible
        ):
            self.monitor.hide()
            self.app.layout.focus(self.input_window)
        return None

    def _on_monitor_label_click(self, mouse_event) -> None:
        """Clicking the [Monitor(F2)] label toggles the overlay."""
        if (
            mouse_event.event_type == MouseEventType.MOUSE_DOWN
            and self.monitor is not None
        ):
            self.monitor.toggle()
            if not self.monitor.visible:
                self.app.layout.focus(self.input_window)
        return None

    def _on_accept(self, buf: Buffer) -> bool:
        """Called when Enter is pressed. History is appended manually; returns False."""
        line = buf.text.strip()

        if not line:
            return False  # accepted but empty — no history entry

        if self._busy:
            if self._busy_command:
                snippet = self._busy_command[:60]
                if len(self._busy_command) > 60:
                    snippet += "..."
                self._append_text(f"om> busy (waiting for: {snippet})")
            else:
                self._append_text("om> busy (previous command still running)")
            return False

        # Manually append to history, then clear buffer ourselves.
        # (prompt_toolkit's auto-history only works when it clears the buffer;
        #  we clear it ourselves so the prompt is ready for the next command.)
        if buf.history is not None:
            buf.history.append_string(line)
        buf.text = ""
        buf.cursor_position = 0

        if cfg.COMMAND_SEPARATOR == "rule":
            try:
                cols, _ = os.get_terminal_size()
                self._append_text("─" * max(10, cols))
            except Exception:
                self._append_text("─" * 60)
        elif cfg.COMMAND_SEPARATOR == "blank":
            self._append_text("")
        self._append_text(f"om> {line}")

        if line in ("quit", "exit"):
            self._running = False
            self.app.exit()
            return False

        self._busy = True
        self._busy_command = line

        def _run_command():
            # Capture stdout while the command runs; background threads schedule
            # output into the prompt_toolkit output pane via _append_text.
            proxy = _StdoutProxy(self._append_text)
            old_stdout = sys.stdout
            old_repl_stdout = self.repl.stdout
            sys.stdout = proxy
            self.repl.stdout = proxy

            try:
                processed = self.repl.precmd(line)
                stop = self.repl.onecmd(processed)
                proxy.flush()
                stop = self.repl.postcmd(stop, processed)
                if stop:
                    self._running = False
                    try:
                        loop = self._loop or asyncio.get_running_loop()
                        loop.call_soon_threadsafe(self.app.exit)
                    except Exception:
                        pass
            except SystemExit:
                self._running = False
                try:
                    loop = self._loop or asyncio.get_running_loop()
                    loop.call_soon_threadsafe(self.app.exit)
                except Exception:
                    pass
            except Exception as exc:
                proxy.flush()
                self._append_text(f"Error: {exc}")
            finally:
                sys.stdout = old_stdout
                self.repl.stdout = old_repl_stdout
                self._busy = False
                self._busy_command = None

        try:
            loop = self._loop or asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.run_in_executor(None, _run_command)
        else:
            self._busy = False
            self._busy_command = None
            self._append_text("Error: no event loop available")

        return False

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    def _draw_banner(self) -> None:
        """Render startup splash banner centered in the output pane."""
        try:
            cols, _ = os.get_terminal_size()
        except Exception:
            cols = 80

        # Box width (leave margins)
        box_width = min(64, cols - 4)
        inner = box_width - 2  # inside the ║ borders
        left_pad = max(0, (cols - box_width) // 2)
        pad = " " * left_pad

        def center(text: str, width: int) -> str:
            if len(text) >= width:
                return text[:width]
            p = (width - len(text)) // 2
            return " " * p + text + " " * (width - len(text) - p)

        def hfill(ch: str, width: int) -> str:
            return ch * width

        # Title lines
        title1 = "Open Modeling Core"
        title2 = "(OM Core™)"
        title3 = "a numerical modeling environment"

        # Build banner
        try:
            _, lines_count = os.get_terminal_size()  # columns, lines
        except Exception:
            lines_count = 24
        pane_height = max(10, lines_count - 3)  # label bar, status bar, input
        banner_height = 16  # box (9) + guide (6) + separators
        top_blank = max(0, (pane_height - banner_height) // 2)
        bottom_blank = max(0, pane_height - banner_height - top_blank)

        lines = []
        for _ in range(top_blank):
            lines.append("")
        lines.append(pad + "╔" + hfill("═", box_width - 2) + "╗")
        lines.append(pad + "║" + center("", inner) + "║")
        lines.append(pad + "║" + center(title1, inner) + "║")
        lines.append(pad + "║" + center(title2, inner) + "║")
        lines.append(pad + "║" + center(title3, inner) + "║")
        lines.append(pad + "║" + center("", inner) + "║")
        lines.append(pad + "╚" + hfill("═", box_width - 2) + "╝")
        lines.append("")

        # Guide (also centered under the box)
        guide_left = left_pad + 2
        guide_pad = " " * guide_left
        lines.append(guide_pad + "Quick guide:")
        lines.append(guide_pad + "  • help   → type 'help' in the prompt")
        lines.append(guide_pad + "  • select → Shift + click/drag with mouse")
        lines.append(guide_pad + "  • scroll → mouse wheel or PgUp / PgDn")
        lines.append(guide_pad + "  • quit   → type 'exit' or 'quit'")
        lines.append("")
        for _ in range(bottom_blank):
            lines.append("")

        for ln in lines:
            self._append_text(ln)

    def _append_text(self, text: str) -> None:
        """Append a line to the output pane. Safe from any thread."""
        def _wrap_line(line: str, width: int) -> str:
            if len(line) <= width:
                return line
            return textwrap.fill(
                line,
                width=width,
                break_long_words=False,
                break_on_hyphens=False,
            )

        def _insert():
            try:
                cols, _ = os.get_terminal_size()
                wrap_width = max(10, cols - 2)
            except Exception:
                wrap_width = 78
            lines = text.splitlines()
            wrapped = "\n".join(_wrap_line(ln, wrap_width) for ln in lines)
            self.output_buffer.text += wrapped + "\n"
            # Trim oldest lines if buffer exceeds limit
            all_lines = self.output_buffer.text.splitlines()
            if len(all_lines) > cfg.MAX_BUFFER_LINES:
                keep = all_lines[-cfg.MAX_BUFFER_LINES:]
                self.output_buffer.text = "\n".join(keep) + "\n"
            self.output_buffer.cursor_position = len(self.output_buffer.text)

        loop = self._loop
        if loop is None:
            # get_running_loop does not warn; if there is no running loop, fall
            # back to a direct update (safe during construction or tests).
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

        if loop is not None and loop.is_running():
            # If we're already on the event loop thread, update immediately
            # so output streams while commands run. Background threads still
            # use the thread-safe schedule path.
            if loop._thread_id == threading.current_thread().ident:
                _insert()
                try:
                    self.app.invalidate()
                except Exception:
                    pass
                return
            loop.call_soon_threadsafe(_insert)
            return
        _insert()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _status_fragments(self):
        """Return HTML-formatted status for the status line."""
        # Sync the thread-safe ReplState with the actual session connection state
        # so the status bar reflects disconnections immediately.
        try:
            if hasattr(self.repl, "session") and hasattr(self.repl.session, "is_connected"):
                connected = bool(self.repl.session.is_connected)
                if hasattr(self.repl, "_repl_state"):
                    self.repl._repl_state.connected = connected
        except Exception:
            pass
        if hasattr(self.repl, "_repl_state"):
            raw = self.repl._repl_state.render()
            return HTML(raw)
        return HTML("READY")

    def _clock_text(self):
        """Return current date+time as MMM-DD  HH:MM:SS."""
        return datetime.now().strftime(" %b-%d  %H:%M:%S ")

    def _version_text(self):
        """Return build version string."""
        from lib_utils.version import om_version
        return f" {om_version()} "

    # ------------------------------------------------------------------
    # Async queue
    # ------------------------------------------------------------------

    def _start_async_poll(self) -> None:
        """Background thread drains the REPL output queue."""
        def poll():
            q = getattr(self.repl, "_output_queue", None)
            if q is None:
                return
            while self._running:
                try:
                    msg = q.get(timeout=0.2)
                    self._append_text(msg)
                except Exception:
                    pass
        threading.Thread(target=poll, daemon=True).start()

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def run(self) -> None:
        # Store the event loop for thread-safe scheduling later.
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
            except Exception:
                self._loop = None

        # SIGHUP is Unix-only; skip it on Windows.
        signals = [signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            signals.append(signal.SIGHUP)
        for sig in signals:
            try:
                signal.signal(sig, lambda _s, _f: self.app.exit())
            except Exception:
                pass

        self.repl.preloop()
        self._start_async_poll()

        # Subscribe to bus messages for the monitor overlay
        session = getattr(self.repl, "session", None)
        if session is not None and self.monitor is not None and hasattr(session, "watch_all"):
            try:
                session.watch_all(self.monitor.on_bus_event)
            except Exception:
                pass

        try:
            with patch_stdout():
                self.app.run()
        except KeyboardInterrupt:
            pass
        finally:
            if session is not None and self.monitor is not None and hasattr(session, "unwatch_all"):
                try:
                    session.unwatch_all(self.monitor.on_bus_event)
                except Exception:
                    pass
            self.repl.postloop()
            self.repl._save_history()
            # Ensure terminal is clean
            term = blessed.Terminal()
            print(term.normal, end="", flush=True)
