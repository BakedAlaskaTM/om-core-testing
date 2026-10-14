"""Explicit teardown for process-wide local runtime state."""

from __future__ import annotations

from lib_command.core.bootstrap import teardown_command_services
from lib_command.core.executor import _reset_executor
from lib_command.core.message_bus import reset_message_bus
from lib_command.core.session_gateway import _reset_session_gateway
from lib_command.core.session_manager import _reset_session_manager
from lib_command.core.session_store import _reset_session_store


def reset_runtime_state(context=None) -> None:
    """Return the local runtime command layer to a clean process state.

    This is intended for isolated hosts and deterministic test workers. Callers
    must not invoke it while another runtime in the same process is active.
    """
    if context is not None:
        context.close()
    teardown_command_services()
    _reset_session_gateway()
    _reset_session_manager()
    _reset_session_store()
    _reset_executor()
    reset_message_bus()
