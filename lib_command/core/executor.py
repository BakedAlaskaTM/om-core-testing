"""
Command Executor - Runs commands with proper context and error handling.

Fresh implementation - no dependencies on existing systems.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Callable
from enum import Enum, auto
import time
import traceback

from .registry import CommandRegistry, get_registry
from lib_command.core.message_bus import (
    get_message_bus,
    MessageEnvelope,
)


class ExecutionStatus(Enum):
    """Result status of command execution."""
    SUCCESS = auto()
    ERROR = auto()
    CANCELLED = auto()
    NOT_FOUND = auto()


@dataclass
class ExecutionResult:
    """Result of executing a command."""
    status: ExecutionStatus
    command_id: str
    data: Any = None           # Return value from command
    error: Optional[str] = None
    duration_ms: float = 0.0    # Execution time

    @property
    def success(self) -> bool:
        return self.status == ExecutionStatus.SUCCESS


@dataclass
class ExecutionContext:
    """
    Context passed to commands during execution.
    Commands receive this to interact with the application state.
    """
    # References to application state (populated by app at startup)
    engine: Any = None          # The OpenM engine
    active_view: Any = None     # Currently active view
    selection: Any = None       # Current cell/region selection
    workspace: Any = None       # Current workspace
    undo_manager: Any = None    # Engine UndoManager for grouping command-level undo
    gui_window: Any = None      # MainWindow reference for GUI-aware commands

    # Shell scripting variables storage
    variables: dict = None      # User-defined shell variables ($var)
    
    # Global variables (persist across macro playback)
    global_vars: dict = None

    # History control flag
    skip_history: bool = False  # If True, history writes are skipped

    # Runtime-wired services (e.g. TimelineService) accessible via ctx.services
    services: Any = None

    # Optional profiler for instrumenting command/query handlers from the GUI.
    profiler: Any = None

    # Command metadata (populated by CommandExecutor during command execution)
    correlation_id: Optional[str] = None
    session_id: Optional[str] = None
    causation_id: Optional[str] = None
    command_message_id: Optional[str] = None

    # Callbacks that commands can use
    on_refresh: Optional[Callable[[], None]] = None
    on_status_update: Optional[Callable[[str], None]] = None

    def __post_init__(self):
        if self.variables is None:
            self.variables = {}
        if self.global_vars is None:
            self.global_vars = {}

    def refresh(self):
        """Request UI refresh after state change."""
        if self.on_refresh:
            self.on_refresh()

    def status(self, message: str):
        """Update status bar/message."""
        if self.on_status_update:
            self.on_status_update(message)


class CommandExecutor:
    """
    Executes commands with proper context injection and error handling.
    """

    _instance: Optional[CommandExecutor] = None

    def __new__(cls) -> CommandExecutor:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._context = ExecutionContext()
            cls._instance._registry = get_registry()
            cls._instance._pre_hooks: list[Callable] = []
            cls._instance._post_hooks: list[Callable] = []
            cls._instance._alias_to_canonical: dict[str, str] = {}
        return cls._instance

    def set_context(self, context: ExecutionContext):
        """Set the execution context (call once at app startup)."""
        self._context = context

    def get_context(self) -> ExecutionContext:
        """Get the current execution context."""
        return self._context

    def add_pre_hook(self, hook: Callable[[str, dict], None]):
        """Add a hook that runs before each command (for logging, etc)."""
        self._pre_hooks.append(hook)

    def add_post_hook(self, hook: Callable[[str, ExecutionResult], None]):
        """Add a hook that runs after each command."""
        self._post_hooks.append(hook)

    def add_alias(self, alias_id: str, canonical_id: str) -> None:
        """Register a command alias for lifecycle event normalization.

        When ``alias_id`` is executed, lifecycle events (before, succeeded,
        failed) are published under ``command.{canonical_id}.*`` topics.
        """
        self._alias_to_canonical[alias_id] = canonical_id

    def execute(
        self,
        command_id: str,
        context: Optional[ExecutionContext] = None,
        correlation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        **params
    ) -> ExecutionResult:
        """
        Execute a command by ID with given parameters.

        Args:
            command_id: The registered command ID
            context: Required explicit ExecutionContext. Must not be None.
            **params: Parameters to pass to the command handler

        Returns:
            ExecutionResult with status and data/error
        """
        # Reject queries — they must route through QueryService
        if command_id == "query":
            raise ValueError(
                "Queries must route through QueryService, not CommandExecutor. "
                "Use session.query() or query_service.execute() instead."
            )

        import time
        start = time.perf_counter()

        # Use explicit context if provided, otherwise fall back to internal.
        # The fallback preserves backward compatibility for legacy callers.
        if context is None:
            import warnings
            warnings.warn(
                "execute() without explicit context is deprecated",
                DeprecationWarning,
                stacklevel=2,
            )
            context = self._context
        ctx = context

        # Resolve canonical command ID for lifecycle events
        canonical_id = self._alias_to_canonical.get(command_id, command_id)

        # Get event bus
        bus = get_message_bus()

        # Create command envelope
        event = MessageEnvelope(
            message_id=__import__('uuid').uuid4().hex,
            message_type="command",
            topic=f"command.{canonical_id}",
            correlation_id=correlation_id or __import__('uuid').uuid4().hex,
            causation_id=causation_id,
            session_id=session_id,
            client_type=None,
            workspace_id=None,
            actor_id=None,
            timestamp=__import__('time').perf_counter(),
            payload=dict(params, command_id=canonical_id),
            context=context,
            status="accepted",
            command_id=canonical_id,
        )

        # Publish before event — update envelope topic so remote clients can
        # match it against wildcard subscriptions (e.g. command.*.succeeded).
        from dataclasses import replace
        event = replace(event, topic=f"command.{canonical_id}.before")
        bus.publish(event.topic, event)

        # Look up command
        cmd = self._registry.get(command_id)
        if cmd is None:
            error_msg = f"Command '{command_id}' not found"
            fail_params = dict(params)
            fail_params["__error"] = error_msg
            # Publish failure events for not found
            event_fail = MessageEnvelope(
                message_id=__import__('uuid').uuid4().hex,
                message_type="command",
                topic=f"command.{canonical_id}.failed",
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                session_id=event.session_id,
                client_type=None,
                workspace_id=None,
                actor_id=None,
                timestamp=__import__('time').perf_counter(),
                payload=dict(fail_params, command_id=canonical_id, error=error_msg),
                context=ctx,
                status="failed",
                command_id=canonical_id,
            )
            bus.publish(event_fail.topic, event_fail)
            return ExecutionResult(
                status=ExecutionStatus.NOT_FOUND,
                command_id=canonical_id,
                error=error_msg
            )

        # Run pre-hooks
        for hook in self._pre_hooks:
            try:
                hook(command_id, params)
            except Exception:
                pass  # Don't let hooks break execution

        # Start undo grouping if undo_manager is available
        undo_mgr = getattr(ctx, 'undo_manager', None)
        if undo_mgr is not None and hasattr(undo_mgr, 'start_group'):
            undo_mgr.start_group(f"command.{canonical_id}")

        # Attach command metadata to execution context for handler events
        ctx.correlation_id = event.correlation_id
        ctx.session_id = session_id
        ctx.causation_id = causation_id
        ctx.command_message_id = event.message_id

        try:
            # Execute command
            try:
                # Prepare arguments
                handler = cmd.handler
                call_args = dict(params)

                # Inject context if needed
                if cmd.needs_context:
                    call_args['ctx'] = ctx

                # Validate required params
                for param_name, param_type in cmd.params.items():
                    if param_name not in call_args:
                        if undo_mgr is not None:
                            undo_mgr.cancel_group()
                        return ExecutionResult(
                            status=ExecutionStatus.ERROR,
                            command_id=canonical_id,
                            error=f"Missing required parameter: {param_name}"
                        )

                # Call the handler
                result = handler(**call_args)

                duration = (time.perf_counter() - start) * 1000

                exec_result = ExecutionResult(
                    status=ExecutionStatus.SUCCESS,
                    command_id=canonical_id,
                    data=result,
                    duration_ms=duration
                )

                # End undo grouping on success
                if undo_mgr is not None:
                    undo_mgr.end_group()

                # Publish succeeded event — update envelope topic so remote
                # clients can match it against wildcard subscriptions.
                event = replace(event, topic=f"command.{canonical_id}.succeeded")
                bus.publish(event.topic, event)

                # Spine recording hook: record canonical command if policy allows
                try:
                    cmd_def = self._registry.get(command_id)
                    if cmd_def is not None and cmd_def.record_policy != "never":
                        from lib_utils.macro_recorder import get_recorder
                        recorder = get_recorder()
                        if recorder.is_recording():
                            recorder.record_canonical(canonical_id, call_args)
                except Exception:
                    pass  # Recording failures must not affect command execution

            except (Exception, KeyboardInterrupt) as e:
                duration = (time.perf_counter() - start) * 1000

                # Cancel undo grouping on failure
                if undo_mgr is not None:
                    undo_mgr.cancel_group()

                exec_result = ExecutionResult(
                    status=ExecutionStatus.ERROR,
                    command_id=canonical_id,
                    error=str(e),
                    duration_ms=duration
                )

                # Publish failed event
                fail_params = dict(params)
                fail_params["__error"] = str(e)
                event_fail = MessageEnvelope(
                    message_id=__import__('uuid').uuid4().hex,
                    message_type="command",
                    topic=f"command.{canonical_id}.failed",
                    correlation_id=event.correlation_id,
                    causation_id=event.causation_id,
                    session_id=event.session_id,
                    client_type=None,
                    workspace_id=None,
                    actor_id=None,
                    timestamp=__import__('time').perf_counter(),
                    payload=dict(fail_params, command_id=canonical_id, error=str(e)),
                    context=ctx,
                    status="failed",
                    command_id=canonical_id,
                )
                bus.publish(event_fail.topic, event_fail)

        finally:
            # Clear transient command metadata from context
            ctx.correlation_id = None
            ctx.session_id = None
            ctx.causation_id = None
            ctx.command_message_id = None

        # Run post-hooks
        for hook in self._post_hooks:
            try:
                hook(canonical_id, exec_result)
            except Exception:
                pass

        return exec_result

    def execute_batch(self, commands: list[tuple[str, dict]]) -> list[ExecutionResult]:
        """
        Execute multiple commands in sequence.

        Args:
            commands: List of (command_id, params_dict) tuples

        Returns:
            List of ExecutionResults
        """
        results = []
        for cmd_id, params in commands:
            result = self.execute(cmd_id, **params)
            results.append(result)
            if not result.success:
                # Stop on first error? Or continue? For now, continue.
                pass
        return results

    def try_execute(self, command_id: str, **params) -> Any:
        """
        Execute and return data directly, or None on error.
        Convenience method for scripts.
        """
        result = self.execute(command_id, **params)
        if result.success:
            return result.data
        return None


# Module-level accessor
def get_executor() -> CommandExecutor:
    """Get the global command executor."""
    return CommandExecutor()


def _reset_executor() -> None:
    """Discard executor hooks, fallback context, and alias state."""
    CommandExecutor._instance = None
