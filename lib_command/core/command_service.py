"""
CommandService — thin bus-side adapter for command execution.

Phase 5: MessageBus command transport.

This is not a semantic command engine. It is a transport boundary that:
1. subscribes to the unified command ingress topic `request.command`;
2. validates envelope/session shape;
3. delegates execution to `CommandExecutor.execute(...)`;
4. packages the returned `ExecutionResult` as a reply envelope;
5. never contains command-specific business logic.
"""

from __future__ import annotations

import time as _time
import uuid as _uuid
from typing import Any, Optional

from .message_bus import MessageBus, MessageEnvelope, get_message_bus
from .executor import CommandExecutor, ExecutionResult, ExecutionStatus, get_executor
from lib_storeadapters.ports import WorkspacePersistenceAdapter


class CommandService:
    """
    Bus-side adapter that consumes command requests and returns replies.

    Responsibilities:
    - Subscribe to `request.command`
    - Validate incoming envelopes
    - Delegate to CommandExecutor
    - Publish reply envelopes

    Must NOT:
    - Contain command-specific business logic
    - Resolve labels, refs, or business semantics
    - Mutate Engine directly
    - Republish received requests onto `request.command`
    """

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        executor: Optional[CommandExecutor] = None,
        persistence_adapter: Optional[WorkspacePersistenceAdapter] = None,
    ) -> None:
        self.bus = bus or get_message_bus()
        self.executor = executor or get_executor()
        self.persistence_adapter = persistence_adapter
        self._subscribed = False

    def start(self) -> None:
        """Subscribe to the unified command request topic.

        If the bus was reset externally (e.g. test fixtures clearing
        _subscribers), the stored handler may have been dropped. Re-subscribe
        in that case so commands continue to be processed.
        """
        handlers = self.bus._subscribers.get("request.command", [])
        if self._on_command_request not in handlers:
            self.bus.subscribe("request.command", self._on_command_request)
        self._subscribed = True

    def stop(self) -> None:
        """Unsubscribe from the command request topic."""
        if self._subscribed:
            self.bus.unsubscribe("request.command", self._on_command_request)
            self._subscribed = False

    def _on_command_request(self, envelope: MessageEnvelope) -> None:
        """Handle an incoming command request envelope."""
        # Validate envelope shape
        if not isinstance(envelope, MessageEnvelope):
            # Cannot reply without a reply_to topic
            return

        payload = envelope.payload or {}
        command_id = payload.get("command_id")
        session_id = envelope.session_id
        ctx = envelope.context
        if ctx is None:
            from ..core.executor import ExecutionContext
            ctx = ExecutionContext()
        if self.persistence_adapter is not None and not hasattr(ctx, "persistence_adapter"):
            ctx.persistence_adapter = self.persistence_adapter
        reply_topic = envelope.reply_to

        if command_id is None:
            self._send_reply(
                reply_topic=reply_topic,
                correlation_id=envelope.correlation_id,
                session_id=session_id,
                result=ExecutionResult(
                    status=ExecutionStatus.ERROR,
                    command_id="unknown",
                    error="Missing command_id in payload",
                ),
                providers=getattr(ctx, "providers", None),
            )
            return

        # Delegate to CommandExecutor — this is the canonical execution path
        # The executor handles alias normalization, lifecycle events, etc.
        params = dict(payload)
        params.pop("command_id", None)

        try:
            result = self.executor.execute(
                command_id,
                context=ctx,
                correlation_id=envelope.correlation_id,
                session_id=envelope.session_id,
                causation_id=envelope.causation_id,
                **params
            )
        except Exception as exc:
            result = ExecutionResult(
                status=ExecutionStatus.ERROR,
                command_id=command_id,
                error=str(exc),
            )

        self._send_reply(
            reply_topic=reply_topic,
            correlation_id=envelope.correlation_id,
            session_id=session_id,
            result=result,
            original_command_id=command_id,
            providers=getattr(ctx, "providers", None),
        )

    def _send_reply(
        self,
        reply_topic: Optional[str],
        correlation_id: str,
        session_id: Optional[str],
        result: ExecutionResult,
        original_command_id: Optional[str] = None,
        providers: Any = None,
    ) -> None:
        """Publish a reply envelope. If reply_topic is None, reply is silently dropped."""
        if reply_topic is None:
            return

        ids = getattr(providers, "ids", None)
        clock = getattr(providers, "clock", None)
        reply = MessageEnvelope(
            message_id=(ids.uuid4().hex if ids is not None else _uuid.uuid4().hex),
            message_type="reply",
            topic=reply_topic,
            correlation_id=correlation_id,
            session_id=session_id,
            client_type=None,
            workspace_id=None,
            actor_id=None,
            timestamp=(clock.perf_counter() if clock is not None else _time.perf_counter()),
            payload={
                "success": result.success,
                "status": result.status.name,
                "data": result.data,
                "error": result.error,
                "command_id": result.command_id,
                "original_command_id": original_command_id or result.command_id,
            },
            context=None,
            status="succeeded" if result.success else "failed",
        )
        self.bus.publish(reply_topic, reply)
