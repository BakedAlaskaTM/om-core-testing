"""
REPL Core - Base class and infrastructure.

Provides the foundation for the OpenM REPL including:
- History management
- Context setup
- Status display
- Variable expansion
- Command substitution
"""

from __future__ import annotations

import cmd
import re
import sys
import time
import queue
import threading
from pathlib import Path
from typing import Optional

try:
    import readline
except ImportError:
    readline = None

from .status_display import StatusDisplay
from .repl_state import ReplState


# History file path (in user's home directory)
from lib_utils.paths import OM_HISTORY_FILE

HISTORY_FILE = OM_HISTORY_FILE
HISTORY_LENGTH = 10000


class OpenMREPLCore(cmd.Cmd):
    """
    Base REPL class with core functionality.
    Provides history, context, and command infrastructure.
    """

    intro = """
╔══════════════════════════════════════╗
║     Open Modeling Command Shell      ║
║  Type 'help' for available commands  ║
╚══════════════════════════════════════╝
"""
    # Plain text prompt — no ANSI colors to avoid readline corruption
    prompt = "om> "
    restart_requested = False  # Class-level flag for restart

    def __init__(
        self,
        session,
        registry=None,
        executor=None,
        help_system=None,
        command_categories=None,
        script_parser_module=None,
    ):
        super().__init__()

        if session is None:
            raise TypeError("OpenMREPLCore requires a session argument")

        self.registry = registry
        self.executor = executor
        self.help_system = help_system
        self.command_categories = command_categories
        self.script_parser_module = script_parser_module
        self.session = session

        # Async output queue for GUI thread and async event display
        self._output_queue: queue.Queue[str] = queue.Queue()

        # Thread-safe state used by the prompt_toolkit status bar
        self._repl_state = ReplState()

        # Subscribe to bus events for status/config display
        if hasattr(session, "subscribe"):
            self._status_display = StatusDisplay(
                session=session,
                prompt=self.prompt,
                output_queue=self._output_queue,
                repl_state=self._repl_state,
            )
        else:
            self._status_display = None

        # GUI reference (set externally if GUI is running)
        self.gui_window = None
        self.gui_thread = None
        self.gui_app = None
        self.gui_exit_event = None
        self.gui_port = None

        # Selection position for navigation commands
        self._sel_row = None
        self._sel_col = None

        # Load command history
        self._load_history()

    @property
    def variables(self) -> dict:
        """Variable store that works in both local and remote mode."""
        return self.session.get_variables()

    @property
    def global_vars(self) -> dict:
        """Global-variable store that works in both local and remote mode."""
        return self.session.get_global_vars()

    def _show_status(self, msg: str):
        """Display status message to stderr so it does not corrupt readline."""
        print(f"[status] {msg}", file=sys.stderr)

    def println(self, msg: str = "") -> None:
        """Print a line to stdout. Used by mixins (e.g. REPLSolverMixin)."""
        print(msg)

    def _load_history(self) -> None:
        """Load command history from file."""
        if readline is None:
            return
        try:
            if HISTORY_FILE.exists():
                readline.read_history_file(str(HISTORY_FILE))
                readline.set_history_length(HISTORY_LENGTH)
        except Exception:
            pass  # Silently ignore history loading errors

    def _save_history(self) -> None:
        """Save command history to file with deduplication and last-N retention."""
        try:
            # Collect existing + new entries from prompt_toolkit's in-memory history
            from prompt_toolkit.history import InMemoryHistory
            entries = []
            if hasattr(self, '_prompt_history') and self._prompt_history:
                entries = list(self._prompt_history.get_strings())
            # Fallback: read from readline if available
            if not entries and readline is not None:
                total_items = readline.get_current_history_length()
                for i in range(1, total_items + 1):
                    cmd = readline.get_history_item(i)
                    if cmd:
                        entries.append(cmd)

            if not entries:
                return

            # Deduplicate (keep most recent)
            seen = set()
            unique_commands = []
            for cmd in reversed(entries):
                if cmd not in seen:
                    seen.add(cmd)
                    unique_commands.append(cmd)
            unique_commands.reverse()
            last_n = unique_commands[-HISTORY_LENGTH:]

            # Write plain text (one entry per line) — compatible with InMemoryHistory loader
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                for cmd in last_n:
                    f.write(cmd + "\n")
            print(f"[history] Saved {len(last_n)} unique commands (deduped from {len(entries)}) to {HISTORY_FILE}", file=sys.stderr)
        except Exception as e:
            print(f"[history] Error saving: {e}", file=sys.stderr)

    def _close_gui_if_running(self) -> None:
        """Close GUI if it's running (thread-based, monolithic mode).

        No-op when no GUI is attached.
        """
        if not hasattr(self, 'gui_thread') or not self.gui_thread or not self.gui_thread.is_alive():
            return
        if hasattr(self, 'gui_thread') and self.gui_thread and self.gui_thread.is_alive():
            try:
                from PySide6 import QtWidgets
                if hasattr(self, 'gui_port') and self.gui_port:
                    self.gui_port.close_window()
                if hasattr(self, 'gui_exit_event') and self.gui_exit_event:
                    self.gui_exit_event.wait(timeout=2.0)
                if hasattr(self, 'gui_app') and self.gui_app:
                    self.gui_app.deleteLater()
                    QtWidgets.QApplication.instance().processEvents()
                    time.sleep(0.1)
                self.gui_thread.join(timeout=3.0)
            except Exception:
                pass

    def _expand_macro_placeholders(self, line: str) -> str:
        """Expand {{name}} placeholders.

        Looks up in context variables first; if not found,
        tries to run do_<name>() as a REPL command and
        substitutes the return value.
        """
        def replace_placeholder(match):
            var_name = match.group(1)
            variables = self.variables
            if var_name in variables:
                value = variables[var_name]
                if isinstance(value, list):
                    return ",".join(str(v) for v in value)
                return str(value)
            # Variable not found — try REPL command substitution
            cmd_method = f"do_{var_name}"
            if hasattr(self, cmd_method):
                import io
                from contextlib import redirect_stdout
                f = io.StringIO()
                try:
                    with redirect_stdout(f):
                        result = getattr(self, cmd_method)("")
                except Exception:
                    return match.group(0)
                output = f.getvalue().strip()
                # Prefer the method return value; fall back to captured stdout
                if result is not None and result != "":
                    return str(result)
                if output:
                    return output
                return match.group(0)
            return match.group(0)
        # Match {{VAR}}
        return re.sub(r'\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}', replace_placeholder, line)

    def _expand_variables(self, line: str) -> str:
        """Expand $var and ${var} to their stored values.

        Lists are expanded as comma-separated values, which enables batch
        operations in commands like `set`:

        >>> set $cells format.bold true
        >>> # $cells = "cell:A1,cell:B2" → applies to all three cells
        """
        def replace_var(match):
            var_name = match.group(1) or match.group(2)
            variables = self.variables
            if var_name in variables:
                value = variables[var_name]
                if isinstance(value, list):
                    # Comma-separated for batch command support
                    return ",".join(str(v) for v in value)
                return str(value)
            return match.group(0)  # Leave unchanged if not found
        # Match ${VAR} or $VAR
        return re.sub(r'\$\{([^}]+)\}|\$([a-zA-Z_][a-zA-Z0-9_]*)', replace_var, line)

    def _substitute_commands(self, line: str) -> str:
        """Replace $(command) with the command's return value."""
        def execute_and_capture(match):
            inner_cmd = match.group(1).strip()
            cmd_name, arg, _ = self.parseline(inner_cmd)
            if cmd_name and hasattr(self, f'do_{cmd_name}'):
                method = getattr(self, f'do_{cmd_name}')
                result = method(arg)
                if result is None:
                    return ""
                elif isinstance(result, list):
                    return " ".join(str(x) for x in result)
                else:
                    return str(result)
            return ""
        # Process substitutions repeatedly (for nested cases)
        max_iterations = 10
        for _ in range(max_iterations):
            new_line = re.sub(r'\$\(([^)]+)\)', execute_and_capture, line)
            if new_line == line:
                break
            line = new_line
        return line

    def _parse_value(self, value: str) -> any:
        """Parse a string value into appropriate type."""
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                if value.lower() in ('true', 'yes'):
                    return True
                elif value.lower() in ('false', 'no'):
                    return False
                elif value.startswith('"') and value.endswith('"'):
                    return value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    return value[1:-1]
                return value

    def _parse_assignment_value(self, value: str) -> any:
        """Parse a value for variable assignment (handles lists, quoted strings, etc.).

        Similar to _parse_value but also handles:
        - Space-separated lists: "a b c" -> ['a', 'b', 'c']
        - Quoted strings with spaces: '"hello world"' -> 'hello world'
        """
        value = value.strip()

        # Check for quoted string (single or double)
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            return value[1:-1]

        # Check for space-separated list (but not if it's just a single value)
        if ' ' in value and not value.startswith('('):
            parts = value.split()
            # Parse each part individually
            return [self._parse_assignment_value(part) for part in parts]

        # Use existing value parsing for single values
        return self._parse_value(value)

    def emptyline(self):
        """Do nothing on empty line (don't repeat last command)."""
        pass

    def default(self, line: str):
        """Handle unknown commands - try to execute as command ID."""
        if '.' in line and not line.startswith('.'):
            return self.do_exec(line)
        print(f"Unknown command: {line}")
        print("Type 'help' for available commands")

    def _start_gui_watcher(self) -> None:
        """Start a thread that monitors GUI exit and interrupts input() when closed.

        No-op when no GUI is attached.
        """
        if not hasattr(self, 'gui_exit_event') or not self.gui_exit_event:
            return

        def watcher():
            if hasattr(self, 'gui_exit_event') and self.gui_exit_event:
                self.gui_exit_event.wait()
                import os, signal
                os.kill(os.getpid(), signal.SIGINT)

        import threading
        t = threading.Thread(target=watcher, daemon=True)
        t.start()

    def _drain_output_queue(self) -> None:
        """Print any pending async output from GUI thread before showing prompt."""
        try:
            while True:
                msg = self._output_queue.get_nowait()
                print(msg)
        except queue.Empty:
            pass

    def _restore_terminal_echo(self) -> None:
        """Restore terminal echo in case the REPL exited abnormally.

        Tries termios first (signal-safe, no fork) then falls back to
        os.system('stty echo').
        """
        import sys
        if sys.platform == "win32":
            return
        try:
            import termios
            fd = sys.stdin.fileno()
            attrs = termios.tcgetattr(fd)
            attrs[3] |= termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            try:
                import os
                os.system("stty echo")
            except Exception:
                pass

    def postcmd(self, stop: bool, line: str) -> bool:
        """Hook called after each command - drain async output before showing prompt."""
        self._drain_output_queue()
        return stop

    def _make_prompt_history(self):
        """Read existing history file and return an InMemoryHistory."""
        from prompt_toolkit.history import InMemoryHistory
        entries = []
        try:
            if HISTORY_FILE.exists():
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            entries.append(line)
        except Exception:
            pass
        return InMemoryHistory(entries)

    def _start_status_poll(self) -> None:
        """Background thread that polls engine dirty count every second."""
        import threading
        import time

        def poll():
            while True:
                time.sleep(1)
                if self._repl_state is None:
                    continue
                try:
                    if hasattr(self.session, "is_connected"):
                        self._repl_state.connected = bool(self.session.is_connected)
                except Exception:
                    pass
                try:
                    result = self.session.query("diagnostics_dirty_count")
                    if isinstance(result, dict) and "dirty_count" in result:
                        self._repl_state.dirty_count = result["dirty_count"]
                except Exception:
                    pass

        threading.Thread(target=poll, daemon=True).start()

    def cmdloop(self, intro=None):
        """Override cmd.Cmd.cmdloop with a prompt_toolkit loop + status bar."""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.shortcuts import CompleteStyle
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.styles import Style
        from prompt_toolkit.layout import HSplit, Window, FormattedTextControl, Layout
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.filters import Condition
        from .config import TOOLBAR_FG, TOOLBAR_BG

        # TopBarPromptSession — commented out to disable the orange menu bar.
        # class TopBarPromptSession(PromptSession):
        #     """PromptSession with an additional top_toolbar window."""
        #     def __init__(self, *args, top_toolbar=None, **kwargs):
        #         self.top_toolbar = top_toolbar
        #         super().__init__(*args, **kwargs)
        #
        #     def _create_layout(self):
        #         import os
        #         original = super()._create_layout()
        #         try:
        #             cols = os.get_terminal_size().columns
        #         except Exception:
        #             cols = 80
        #         title = self.top_toolbar or ''
        #         if len(title) < cols:
        #             title = title + (' ' * (cols - len(title)))
        #         top_bar = Window(
        #             height=Dimension(min=1),
        #             content=FormattedTextControl(title),
        #             style='class:top-toolbar',
        #             dont_extend_height=True,
        #         )
        #         new_hsplit = HSplit([top_bar, *original.container.children])
        #         return Layout(new_hsplit, original.current_window)

        class CmdCompleter(Completer):
            """Bridge cmd.Cmd completion methods to prompt_toolkit."""
            def __init__(self, cmd_instance):
                self.cmd = cmd_instance
            def get_completions(self, document, complete_event):
                text_before = document.text_before_cursor
                if not text_before:
                    return
                line = text_before.lstrip()
                stripped = len(text_before) - len(line)
                last_space = text_before.rfind(' ')
                if last_space == -1:
                    begidx = 0
                    endidx = len(line)
                    text = line
                elif last_space == len(text_before) - 1:
                    begidx = len(line)
                    endidx = begidx
                    text = ''
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
                    elif cmd_name == 'help':
                        compfunc = self.cmd.complete_help
                    elif hasattr(self.cmd, 'complete_' + cmd_name):
                        compfunc = getattr(self.cmd, 'complete_' + cmd_name)
                    else:
                        compfunc = self.cmd.completedefault
                matches = compfunc(text, line, begidx, endidx)
                if not matches:
                    return
                for match in matches:
                    if match.startswith(text):
                        display = match.rsplit('/', 1)[-1] if '/' in match else match
                        yield Completion(match, start_position=-len(text), display=display)

        self.preloop()
        if intro is not None:
            self.intro = intro
        if self.intro:
            self.stdout.write(str(self.intro) + "\n")
            self.stdout.flush()

        self._start_status_poll()

        self._prompt_history = self._make_prompt_history()
        style = Style.from_dict({
            "bottom-toolbar": f"{TOOLBAR_FG} bg:{TOOLBAR_BG}",
            # "top-toolbar":    "fg:#000000 bg:#FFA500",
        })
        # Key bindings: Enter accepts completion if menu is open, otherwise submits.
        kb = KeyBindings()
        _session_ref = [None]

        @kb.add("enter", filter=Condition(lambda: _session_ref[0] is not None and _session_ref[0].app.current_buffer.complete_state is not None))
        def _accept_completion(event):
            buf = event.app.current_buffer
            comp = buf.complete_state.current_completion
            if comp is not None:
                buf.apply_completion(comp)
            buf.complete_state = None

        session = PromptSession(
            message=self.prompt,
            completer=CmdCompleter(self),
            complete_while_typing=True,
            complete_style=CompleteStyle.COLUMN,
            key_bindings=kb,
            # top_toolbar='  -= Open Modeling =-      File  Edit  View  Help',
            bottom_toolbar=lambda: HTML(self._repl_state.render()),
            history=self._prompt_history,
            refresh_interval=1,
            style=style,
        )
        _session_ref[0] = session

        stop = None
        while not stop:
            try:
                line = session.prompt()
            except EOFError:
                line = 'EOF'
            except KeyboardInterrupt:
                if (
                    hasattr(self, 'gui_exit_event')
                    and self.gui_exit_event
                    and self.gui_exit_event.is_set()
                ):
                    print("\n[GUI closed - shutting down REPL]")
                    break
                if not getattr(self, 'gui_exit_event', None):
                    print("\n^C")
                    break
                print("\nUse 'quit' or 'exit' to leave")
                self.run()
                return
            line = self.precmd(line)
            stop = self.onecmd(line)
            stop = self.postcmd(stop, line)

        self.postloop()

    def run(self):
        """Start the REPL loop. Saves history on any exit path."""
        import signal
        import sys

        self._start_gui_watcher()

        # Install signal handlers that restore terminal echo before dying.
        # SIGTERM bypasses finally blocks, so this is the only reliable path.
        # SIGHUP is Unix-only; skip it on Windows.
        signals = [signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            signals.append(signal.SIGHUP)
        for sig in signals:
            try:
                signal.signal(sig, self._make_exit_handler(sig))
            except Exception:
                pass

        try:
            self.cmdloop()
        except KeyboardInterrupt:
            if hasattr(self, 'gui_exit_event') and self.gui_exit_event and self.gui_exit_event.is_set():
                print("\n[GUI closed - shutting down REPL]")
                return
            if not getattr(self, 'gui_exit_event', None):
                return
            print("\nUse 'quit' or 'exit' to leave")
            self.run()
        finally:
            if getattr(self, "_status_display", None):
                self._status_display.close()
            self._save_history()
            self._restore_terminal_echo()

    def _make_exit_handler(self, sig: int):
        """Return a signal handler that restores echo then exits."""
        import sys
        def handler(signum, frame):
            self._restore_terminal_echo()
            sys.exit(128 + signum)
        return handler
