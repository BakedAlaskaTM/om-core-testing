"""lib_runtime.tui_host — TUI client entry point.

Connects to a running runtime via transport and starts the prompt_toolkit TUI.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


def _create_script_parser_module() -> Any:
    """Bundle script-parser classes/functions into a module-like object."""
    import types
    from lib_command.core.script_parser import (
        ScriptLexer, ScriptParser, ExpressionEvaluator, execute_script, parse_script
    )
    mod = types.ModuleType("script_parser_module")
    mod.ScriptLexer = ScriptLexer
    mod.ScriptParser = ScriptParser
    mod.ExpressionEvaluator = ExpressionEvaluator
    mod.execute_script = execute_script
    mod.parse_script = parse_script
    return mod


def _wire_tui_deps() -> dict[str, Any]:
    """Create the local command-layer dependencies needed by the TUI."""
    from lib_command.core.bootstrap import register_default_commands
    from lib_command.core.registry import get_registry, CommandCategory
    from lib_command.core.executor import get_executor
    from lib_command.support.help_system import get_help

    register_default_commands()
    return {
        "registry": get_registry(),
        "executor": get_executor(),
        "help_system": get_help(),
        "command_categories": CommandCategory,
        "script_parser_module": _create_script_parser_module(),
    }


def start_tui(endpoint: Any | None = None, log_file: Any | None = None) -> None:
    """Start TUI as a pure client connecting to an existing runtime.

    If *log_file* is an open text file handle, every bus message captured by
    the F2 monitor overlay is also written there.
    """
    from lib_command.core.transport_socket_client import SocketTransportClient
    from lib_command.core.remote_session import RemoteCommandSession
    from lib_tui import PromptToolkitTUI
    from lib_repl import OpenMREPL
    from lib_command.core.transport_base import TransportEndpoint

    if endpoint is None:
        endpoint = _resolve_client_endpoint()

    deps = _wire_tui_deps()

    client = SocketTransportClient(endpoint)
    try:
        client.connect()
        session_id = client.open_session(client_type="tui")
        logger.info("TUI session opened: %s", session_id)

        session = RemoteCommandSession(client, session_id)

        if endpoint.kind == "unix":
            conn_info = f"UDS:{endpoint.path}"
        else:
            conn_info = f"{endpoint.host}:{endpoint.port}"
        print(f"Connected to {conn_info}. Type 'help' for commands.")

        if log_file is not None:
            print(f"Monitor logging to: {getattr(log_file, 'name', '?')}")

        repl = OpenMREPL(
            session=session,
            registry=deps["registry"],
            executor=deps["executor"],
            help_system=deps["help_system"],
            command_categories=deps["command_categories"],
            script_parser_module=deps["script_parser_module"],
        )

        tui = PromptToolkitTUI(repl=repl, monitor_log_file=log_file)
        tui.run()
    except Exception as exc:
        print(f"Error: Cannot connect to OM runtime at {endpoint}.")
        print("Start the runtime first with: python main.py --runtime")
        print(f"  Detail: {exc}")
        sys.exit(1)
    finally:
        client.close()
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass
        if sys.platform != "win32":
            try:
                import os
                os.system("stty echo")
            except Exception:
                pass


def _resolve_client_endpoint() -> Any:
    """Resolve transport endpoint for the TUI client."""
    import os
    from lib_command.core.transport_base import TransportEndpoint

    args = sys.argv

    if "--socket" in args:
        idx = args.index("--socket")
        if idx + 1 < len(args):
            return TransportEndpoint(kind="unix", path=args[idx + 1])

    host = None
    port = None
    if "--host" in args:
        idx = args.index("--host")
        if idx + 1 < len(args):
            host = args[idx + 1]
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            try:
                port = int(args[idx + 1])
            except ValueError:
                port = None

    if host is not None or port is not None:
        return TransportEndpoint(kind="tcp", host=host or "127.0.0.1", port=port or 17391)

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

    if os.name == "nt":
        return TransportEndpoint(kind="tcp", host="127.0.0.1", port=17391)
    else:
        return TransportEndpoint(
            kind="unix",
            path=f"/tmp/openm-{os.environ.get('USER', 'unknown')}.sock",
        )
