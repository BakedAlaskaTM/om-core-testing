"""
lib_repl - Interactive REPL for OpenM commands.

Provides a shell-like interface for exploring and executing commands.
Formerly located in lib_command/interfaces/.
"""

from __future__ import annotations

from .repl_core import OpenMREPLCore
from .repl_commands import REPLCommandMixin
from .repl_model import REPLModelMixin
from .repl_group import REPLGroupMixin
from .repl_file import REPLFileMixin
from .repl_recording import REPLRecordingMixin
from .repl_help import REPLHelpMixin
from .repl_navigation import REPLNavigationMixin
from .repl_scripting import REPLScriptingMixin
from .repl_controlflow import REPLControlFlowMixin
from .repl_udf import REPLUDFMixin
from .repl_solver import REPLSolverMixin


class OpenMREPL(
    REPLScriptingMixin,
    REPLControlFlowMixin,
    REPLUDFMixin,
    REPLSolverMixin,
    REPLNavigationMixin,
    REPLHelpMixin,
    REPLRecordingMixin,
    REPLFileMixin,
    REPLModelMixin,
    REPLGroupMixin,        # Group / outline operations
    REPLCommandMixin,
    OpenMREPLCore
):
    """Interactive REPL for OpenM command system."""
    pass


__all__ = ['OpenMREPL']
