from __future__ import annotations

import sys
import os
import time
import logging
import faulthandler
from pathlib import Path


def _ensure_stdio() -> None:
    """Redirect stdout/stderr to a log file when running windowed (no console).

    PyInstaller --windowed builds set sys.stdout and sys.stderr to None,
    which causes faulthandler.enable() and any print() call to raise. We
    redirect both to log/startup.log next to the executable so startup errors
    are still captured and the app can launch.
    """
    if sys.stdout is None or sys.stderr is None:
        if getattr(sys, "frozen", False):
            base_dir = Path(sys.executable).resolve().parent
        else:
            base_dir = Path(__file__).resolve().parent
        log_dir = base_dir / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_stream = open(log_dir / "startup.log", "a", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = log_stream
        if sys.stderr is None:
            sys.stderr = log_stream


_ensure_stdio()
faulthandler.enable()
_start_time = time.perf_counter()

# Minimal early setup - delay logging import until path is set
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib_gui.splash import SplashScreen

def _run_gui_mode():
    """Run GUI in thread mode - GUI in background, REPL in main thread."""
    from lib_utils.logging import setup_logging, get_logger
    # In REPL+GUI mode, suppress INFO console output from the GUI background
    # thread so it doesn't corrupt readline's terminal state.
    logger = setup_logging(level=logging.DEBUG, console_level=logging.WARNING)
    startup_logger = get_logger("startup")
    
    # Start 4 independent service loggers (each with its own log file)
    from lib_command.core.repl_logger import get_repl_logger
    from lib_gui.gui_logger import get_gui_logger
    from lib_command.core.bus_logger import get_bus_logger
    from lib_openm.engine_logger import get_engine_logger

    get_repl_logger().start()
    get_gui_logger().start()
    get_bus_logger().start()
    get_engine_logger().start()
    startup_logger.info("Service loggers started — see log/{repl,gui,bus,engine}.log")

    startup_logger.info("Starting GUI+REPL mode")
    print("Starting OpenModeling with GUI + REPL...")
    print("GUI window opening in background thread.")
    print("Type commands below or 'help' for available commands.\n")

    # Initialize command layer BEFORE starting GUI thread (GUI needs commands during bootstrap)
    from lib_command.core.bootstrap import init_command_services
    from lib_storeadapters.json_file_adapter import JsonFileAdapter
    from lib_storeadapters.timeline_aware_workspace_adapter import TimelineAwareWorkspaceAdapter
    _workspace_adapter = TimelineAwareWorkspaceAdapter(JsonFileAdapter())
    init_command_services(persistence_adapter=_workspace_adapter)

    # Start GUI in background thread
    from lib_runtime.gui_host import run_gui_in_thread
    qt_thread, window, qt_app, exit_event = run_gui_in_thread()

    # Start REPL in main thread — share GUI's session so both use same engine/workspace
    from lib_repl import OpenMREPL
    repl = OpenMREPL(session=window.session)

    # Inject GUI references into REPL for command access
    repl.gui_window = window
    repl.gui_thread = qt_thread
    repl.gui_app = qt_app
    repl.gui_exit_event = exit_event
    from lib_gui.gui_interaction_port import GuiInteractionPort
    repl.gui_port = GuiInteractionPort(window)
    window.gui_port = repl.gui_port  # also expose on window for internal macro playback

    # NOTE: Session context mutation removed as part of H1 boundary cleanup.
    # GUI and REPL now share session state through the runtime transport layer,
    # not through direct context mutation. In combined mode, both use the
    # same local CommandSession created by lib_runtime.
    
    try:
        repl.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        # Check if restart was requested - skip cleanup since we're replacing process
        if repl.restart_requested:
            pass  # Skip cleanup - process will be replaced via execv
        # Check if GUI is still running before trying to quit
        elif qt_thread.is_alive():
            try:
                from PySide6 import QtCore, QtWidgets
                import time
                # Close the window - this triggers lastWindowClosed which calls quit()
                if window:
                    QtCore.QMetaObject.invokeMethod(
                        window,
                        "close",
                        QtCore.Qt.ConnectionType.BlockingQueuedConnection
                    )
                # Wait for exit_event to signal graceful shutdown
                exit_event.wait(timeout=2.0)
                # Delete QApplication to force cleanup before Python interpreter cleanup
                if qt_app:
                    qt_app.deleteLater()
                    QtWidgets.QApplication.instance().processEvents()
                    time.sleep(0.1)
                # Join thread to ensure it finishes before main thread exits
                qt_thread.join(timeout=3.0)
            except Exception:
                pass  # Silently ignore cleanup errors
        # Teardown command services regardless of restart
        from lib_command.core.bootstrap import teardown_command_services
        teardown_command_services()
        # Restore terminal echo in case the REPL exited abnormally
        if sys.platform != "win32":
            try:
                os.system("stty echo")
            except Exception:
                pass


def _run_headless_mode(script_path: str) -> int:
    """Run headless mode - execute script without GUI, return exit code.

    Delegates to cli_host.start_batch for remote client execution.

    Args:
        script_path: Path to OpenM script to execute

    Returns:
        Exit code (0 = success, 1 = error, 2 = assertion failure)
    """
    from lib_runtime.cli_host import start_batch
    return start_batch(script_path)


def _run_batch_mode(script_path: str) -> int:
    """Run batch mode - execute script as a remote client, return exit code.

    Delegates to lib_runtime.cli_host for remote client execution.
    Requires a runtime to be already running (--runtime).
    """
    from lib_runtime.cli_host import start_batch
    return start_batch(script_path)


def _resolve_endpoint(argv: list[str] | None = None) -> "TransportEndpoint":
    """Resolve transport endpoint from CLI args or environment variables.

    CLI args: --socket <path>, --host <addr>, --port <n>
    Env vars: OPENM_TRANSPORT_SOCKET, OPENM_TRANSPORT_HOST, OPENM_TRANSPORT_PORT
    Default: platform-specific socket (Unix-domain on Linux/macOS, TCP on Windows).
    """
    import os
    from lib_command.core.transport_base import TransportEndpoint

    args = argv if argv is not None else sys.argv

    # CLI --socket
    if '--socket' in args:
        idx = args.index('--socket')
        if idx + 1 < len(args):
            return TransportEndpoint(kind="unix", path=args[idx + 1])

    # CLI --host / --port
    host = None
    port = None
    if '--host' in args:
        idx = args.index('--host')
        if idx + 1 < len(args):
            host = args[idx + 1]
    if '--port' in args:
        idx = args.index('--port')
        if idx + 1 < len(args):
            try:
                port = int(args[idx + 1])
            except ValueError:
                print(f"Error: --port must be an integer, got '{args[idx + 1]}'", file=sys.stderr)
                sys.exit(1)

    if host is not None or port is not None:
        return TransportEndpoint(kind="tcp", host=host or "127.0.0.1", port=port or 17391)

    # Environment variables
    socket_path = os.environ.get("OPENM_TRANSPORT_SOCKET")
    if socket_path:
        return TransportEndpoint(kind="unix", path=socket_path)

    env_host = os.environ.get("OPENM_TRANSPORT_HOST")
    env_port = os.environ.get("OPENM_TRANSPORT_PORT")
    if env_host is not None or env_port is not None:
        try:
            p = int(env_port) if env_port else 17391
        except ValueError:
            p = 17391
        return TransportEndpoint(kind="tcp", host=env_host or "127.0.0.1", port=p)

    # Default
    if os.name == "nt":
        return TransportEndpoint(kind="tcp", host="127.0.0.1", port=17391)
    else:
        return TransportEndpoint(kind="unix", path=f"/tmp/openm-{os.environ.get('USER', 'unknown')}.sock")


def _run_gui_only():
    """Run GUI only mode — no REPL. Transport server is optional."""
    from lib_utils.logging import setup_logging, get_logger
    logger = setup_logging(level=logging.DEBUG)
    startup_logger = get_logger("startup")

    # Optional --no-transport disables the remote client socket so the binary
    # can be double-clicked without needing a free port or socket path.
    start_transport = '--no-transport' not in sys.argv
    if '--no-transport' in sys.argv:
        sys.argv.remove('--no-transport')

    # Optional --load <workspace.json> loads a workspace before the GUI opens.
    load_path: str | None = None
    if '--load' in sys.argv:
        idx = sys.argv.index('--load')
        if idx + 1 < len(sys.argv):
            load_path = sys.argv[idx + 1]
            sys.argv.pop(idx)
            sys.argv.pop(idx)

    # Default to the bundled financial demo when running from a PyInstaller bundle.
    if load_path is None and getattr(sys, 'frozen', False):
        bundle_dir = Path(sys.executable).parent
        demos = sorted(bundle_dir.glob("demos/DEMO-03--CALC-FLOW*.json"))
        if demos:
            load_path = str(demos[-1])

    _t0 = _start_time
    _t_prev = _t0
    def _mark(msg):
        nonlocal _t_prev
        _t_now = time.perf_counter()
        elapsed = _t_now - _t0
        delta = _t_now - _t_prev
        startup_logger.debug(f'{msg} (total={elapsed:.3f}s, +{delta:.3f}s)')
        _t_prev = _t_now

    _mark('main() entered')
    from PySide6 import QtWidgets, QtCore, QtGui
    _mark('PySide6 imported')
    # macOS derives the menu-bar title and About menu from the application name.
    # Set it statically before QApplication construction so Qt passes it to the
    # native menu bar instead of the Python process name.
    QtCore.QCoreApplication.setApplicationName("OM Core")
    if sys.platform == "darwin":
        QtGui.QGuiApplication.setApplicationDisplayName("OM Core")
    # macOS derives the app-menu name and menu-bar title from argv[0].
    # Replace the script name with OM Core so the app shows "OM Core" instead
    # of "python" / "main.py".
    argv = ["OM Core"] + sys.argv[1:]
    app = QtWidgets.QApplication(argv)
    app.setApplicationName("OM Core")
    if sys.platform == "darwin":
        app.setApplicationDisplayName("OM Core")
    _mark('QApplication created')

    # Set the bundled logo as the application icon so the splash/taskbar show it.
    try:
        if getattr(sys, 'frozen', False):
            icon_root = Path(sys._MEIPASS)
        else:
            icon_root = Path(__file__).resolve().parent
        icon_path = icon_root / "assets" / "logo" / "taskbar-icon.png"
        if icon_path.exists():
            app.setWindowIcon(QtGui.QIcon(str(icon_path)))
    except Exception:
        pass

    # Create splash with fast QWidget approach and show IMMEDIATELY
    splash = SplashScreen(app)
    cursor_pos = QtGui.QCursor.pos()
    screen = QtWidgets.QApplication.screenAt(cursor_pos)
    if screen is None:
        screen = QtWidgets.QApplication.primaryScreen()
    geom = screen.availableGeometry()
    splash.move(geom.x() + (geom.width() - 480) // 2, geom.y() + (geom.height() - 320) // 2)
    splash.show()
    _mark('Splash screen shown')

    splash.set_progress(10, 'Loading modules...')
    _mark('Loading modules')

    from lib_runtime.gui_host import run_with_splash
    splash.set_progress(20, 'Initializing...')
    _mark('Initializing GUI')
    from lib_command.core.bootstrap import init_command_services
    from lib_storeadapters.json_file_adapter import JsonFileAdapter
    from lib_storeadapters.timeline_aware_workspace_adapter import TimelineAwareWorkspaceAdapter
    _workspace_adapter = TimelineAwareWorkspaceAdapter(JsonFileAdapter())
    init_command_services(persistence_adapter=_workspace_adapter)

    transport_server = None
    if start_transport:
        # Start transport server for remote clients
        endpoint = _resolve_endpoint()
        from lib_command.core.transport_socket_server import SocketTransportServer
        try:
            transport_server = SocketTransportServer(endpoint)
            transport_server.start()
        except RuntimeError as exc:
            if endpoint.kind == "tcp" and "already in use" in str(exc).lower():
                print(f"Error: Port {endpoint.port} is already in use.", file=sys.stderr)
                print(f"  Start the GUI on a different port: ./start.sh --gui-only --port <other-port>", file=sys.stderr)
            elif endpoint.kind == "unix" and "already in use" in str(exc).lower():
                print(f"Error: Failed to start transport server: {exc}", file=sys.stderr)
                print(
                    f"  Another runtime process is already listening on {endpoint.path}. "
                    f"Kill it or use a different socket: ./start.sh --gui-only --socket <path>",
                    file=sys.stderr,
                )
            else:
                print(f"Error: Failed to start transport server: {exc}", file=sys.stderr)
            sys.exit(1)
        startup_logger.info("Transport server started on %s", endpoint)
        print(f"Transport endpoint: {endpoint}")
    else:
        startup_logger.info("Transport server disabled by --no-transport")

    run_with_splash(app, splash, load_path=load_path)

    if transport_server is not None:
        transport_server.stop()


def _run_repl_client():
    """Run REPL client mode — connect to transport server and start REPL."""
    from lib_runtime.repl_host import start_repl
    start_repl()


def _run_tui_client():
    """Run TUI client mode — connect to transport server and start prompt_toolkit TUI."""
    from lib_runtime.tui_host import start_tui

    monitor_log = None
    if '--log' in sys.argv:
        sys.argv.remove('--log')
        from datetime import datetime
        from pathlib import Path
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = Path(__file__).resolve().parent / f"monitor-{ts}.log"
        monitor_log = open(log_path, "a", encoding="utf-8")
        print(f"Monitor log: {log_path}")

    start_tui(log_file=monitor_log)


def _run_runtime_mode():
    """Start standalone runtime host (no GUI, no REPL)."""
    from lib_runtime.runtime_host import start_runtime
    start_runtime()


def _run_gui_client():
    """Run GUI as a pure client connecting to an existing runtime."""
    from lib_runtime.gui_host import start_gui
    start_gui()


def _print_usage() -> None:
    """Print usage message and exit."""
    print("Usage: python main.py [MODE] [OPTIONS]")
    print("")
    print("Modes:")
    print("  --runtime         Start runtime host only (no clients)")
    print("  --gui             Start GUI client (connects to --runtime)")
    print("  --gui-only        Start GUI with embedded runtime (transition)")
    print("  --repl            Start REPL client (connects to --runtime)")
    print("  --tui             Start TUI client (connects to --runtime)")
    print("  --tui --log       Start TUI with monitor logging to monitor-<timestamp>.log")
    print("  --batch <file>    Execute script as remote client")
    print("  --headless <file> Execute script as remote client (alias for --batch)")
    print("  --gui-with-repl   Legacy combined mode (deprecated)")
    print("")
    sys.exit(1)


def main():
    """Main entry point - thin launcher, delegates to lib_runtime host functions."""
    # Default to standalone GUI mode if the binary is invoked without any mode flag.
    # This makes the release build usable via double-click / direct launch.
    if len(sys.argv) == 1:
        sys.argv.extend(['--gui-only', '--no-transport'])

    # Check for --runtime flag (standalone runtime host)
    if '--runtime' in sys.argv:
        sys.argv.remove('--runtime')
        _run_runtime_mode()

    # Check for --headless flag
    elif '--headless' in sys.argv:
        idx = sys.argv.index('--headless')
        if idx + 1 < len(sys.argv):
            script_path = sys.argv[idx + 1]
            sys.argv.pop(idx)
            sys.argv.pop(idx)
            exit_code = _run_headless_mode(script_path)
            sys.exit(exit_code)
        else:
            print("Error: --headless requires a script file path", file=sys.stderr)
            sys.exit(1)

    # Check for --batch mode (script execution as remote client)
    elif '--batch' in sys.argv:
        idx = sys.argv.index('--batch')
        if idx + 1 < len(sys.argv):
            script_path = sys.argv[idx + 1]
            exit_code = _run_batch_mode(script_path)
            sys.exit(exit_code)
        else:
            print("Error: --batch requires a script file path", file=sys.stderr)
            sys.exit(1)

    # Check for --gui flag (pure client connecting to existing runtime)
    elif '--gui' in sys.argv:
        sys.argv.remove('--gui')
        _run_gui_client()

    # Check for --gui-only flag (GUI with embedded runtime, transitional)
    elif '--gui-only' in sys.argv:
        sys.argv.remove('--gui-only')
        _run_gui_only()

    # Check for --repl flag (REPL client connecting to existing runtime)
    elif '--repl' in sys.argv:
        sys.argv.remove('--repl')
        _run_repl_client()

    # Check for --tui flag (TUI client connecting to existing runtime)
    elif '--tui' in sys.argv:
        sys.argv.remove('--tui')
        _run_tui_client()

    # Check for --gui-with-repl flag (legacy combined mode, deprecated)
    elif '--gui-with-repl' in sys.argv:
        print("Warning: --gui-with-repl is deprecated. Use separate processes:")
        print("  ./start.sh --runtime &")
        print("  ./start.sh --gui")
        print("  ./start.sh --repl")
        sys.argv.remove('--gui-with-repl')
        _run_gui_mode()

    else:
        _print_usage()


if __name__ == '__main__':
    main()
