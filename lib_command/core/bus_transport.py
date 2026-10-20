"""
BusTransport — transport boundary for Phase 5 bus-routed commands.

Publishes command requests to `request.command` and waits for correlated replies.
Converts reply envelopes back into `ExecutionResult`.

This is transport-only: it does not validate, resolve, or execute.
"""

from __future__ import annotations

import time as _time
import uuid as _uuid
from typing import Any, Optional

from .message_bus import MessageBus, MessageEnvelope, get_message_bus
from .executor import ExecutionResult, ExecutionStatus
from lib_utils.config import gui


class BusTransport:
    """
    Transport that routes commands through the MessageBus.

    Responsibilities:
    - Build request envelope with correlation_id and reply_to
    - Publish to `request.command`
    - Wait for reply on `reply.command.<session_id>.<correlation_id>`
    - Convert reply envelope back to ExecutionResult

    Must NOT:
    - Validate command semantics
    - Resolve aliases (CommandExecutor does that)
    - Publish lifecycle events
    - Mutate engine state
    """

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.bus = bus or get_message_bus()
        self.timeout = timeout if timeout is not None else gui("transport", "bus_transport_timeout_seconds", 5.0)

    def execute(
        self,
        session_id: str,
        command_id: str,
        context: Any,
        timeout: Optional[float] = None,
        **params
    ) -> ExecutionResult:
        """
        Send a command through the bus and return the ExecutionResult.

        Args:
            session_id: The session context ID
            command_id: The command to execute (may be an alias)
            context: ExecutionContext to pass through
            **params: Command payload parameters

        Returns:
            ExecutionResult from the reply, or a timeout error result.
        """
        providers = getattr(context, "providers", None)
        ids = getattr(providers, "ids", None)
        clock = getattr(providers, "clock", None)
        new_message_id = ids.uuid4 if ids is not None else _uuid.uuid4
        perf_counter = clock.perf_counter if clock is not None else _time.perf_counter
        correlation_id = new_message_id().hex
        reply_topic = f"reply.command.{session_id}.{correlation_id}"

        # Build workspace_id from context if available
        workspace_id = None
        if context is not None:
            workspace = getattr(context, "workspace", None)
            if workspace is not None:
                workspace_id = getattr(workspace, "id", None)

        request = MessageEnvelope(
            message_id=new_message_id().hex,
            message_type="command",
            topic="request.command",
            correlation_id=correlation_id,
            session_id=session_id,
            client_type=getattr(context, "client_type", None) if context else None,
            workspace_id=workspace_id,
            actor_id=None,
            timestamp=perf_counter(),
            payload={
                "command_id": command_id,
                **params,
            },
            context=context,
            reply_to=reply_topic,
        )

        effective_timeout = timeout if timeout is not None else self.timeout
        try:
            reply = self.bus.request(
                topic="request.command",
                event=request,
                reply_topic=reply_topic,
                timeout=effective_timeout,
            )
        except Exception as exc:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                command_id=command_id,
                error=f"Bus transport error: {exc}",
            )

        if reply is None:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                command_id=command_id,
                error=f"Command timed out after {self.timeout}s waiting for reply on {reply_topic}",
            )

        # Convert reply envelope back to ExecutionResult
        payload = reply.payload
        if not isinstance(payload, dict):
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                command_id=command_id,
                error="Invalid reply payload shape from bus",
            )

        success = payload.get("success", False)
        status_name = payload.get("status", "ERROR")
        try:
            status = ExecutionStatus[status_name]
        except (KeyError, TypeError):
            status = ExecutionStatus.ERROR if not success else ExecutionStatus.SUCCESS

        return ExecutionResult(
            status=status,
            command_id=payload.get("command_id", command_id),
            data=payload.get("data"),
            error=payload.get("error"),
            duration_ms=payload.get("duration_ms"),
        )

    def execute_batch(
        self,
        session_id: str,
        commands: list[dict[str, Any]],
        context: Any,
    ) -> list[ExecutionResult]:
        """
        Execute a batch of commands sequentially through the bus.

        Each command is independent: it gets its own correlation_id,
        lifecycle events, and rollback scope.  Commands execute in order
        and all results are returned.

        Args:
            session_id: The session context ID
            commands: List of dicts, each with keys 'command_id' and 'params'
            context: ExecutionContext to pass through

        Returns:
            List of ExecutionResult, one per command.
        """
        results: list[ExecutionResult] = []
        for cmd in commands:
            command_id = cmd.get("command_id")
            params = cmd.get("params", {})
            if command_id is None:
                results.append(ExecutionResult(
                    status=ExecutionStatus.ERROR,
                    command_id="unknown",
                    error="Missing command_id in batch item",
                ))
                continue
            result = self.execute(
                session_id=session_id,
                command_id=command_id,
                context=context,
                **params
            )
            results.append(result)
        return results
