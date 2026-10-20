"""SessionGateway — thin ingress/egress layer for all client traffic."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

from .message_bus import MessageEnvelope, get_message_bus
from .executor import ExecutionResult, ExecutionStatus, get_executor
from .session_manager import get_session_manager
from .bus_transport import BusTransport
from .query_service import topic_for_query_type


class SessionGateway:
    """
    Thin ingress/egress layer. All clients enter here.

    - Attaches session context to every outgoing message
    - Publishes command and reply envelopes to the bus for observability
    - Phase 2 reply routing is synchronous: send() returns ExecutionResult to the caller
    - Asynchronous reply routing/callbacks are deferred
    - Bus observation is enabled for all commands and replies
    """

    _instance: Optional["SessionGateway"] = None

    def __new__(cls) -> "SessionGateway":
        current_bus = get_message_bus()
        if cls._instance is None or cls._instance.bus is not current_bus:
            cls._instance = super().__new__(cls)
            cls._instance.bus = current_bus
            cls._instance.session_mgr = get_session_manager()
            cls._instance.executor = get_executor()
            from .query_service import QueryService
            cls._instance.query_service = QueryService(bus=cls._instance.bus)
            cls._instance.query_service.subscribe()
            cls._instance._bus_transport = None
        return cls._instance

    def _ensure_bus_transport(self):
        """Recreate BusTransport if the underlying bus instance changed (e.g. test reset)."""
        if self._bus_transport is None or self._bus_transport.bus is not self.bus:
            self._bus_transport = BusTransport(bus=self.bus)

    def send(
        self,
        session_id: str,
        command_id: str,
        **params
    ) -> ExecutionResult:
        """Synchronous send. The default path for OpenM."""
        record = self.session_mgr.get_record(session_id)
        if record is None:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                command_id=command_id,
                error=f"Session '{session_id}' not found",
            )

        ctx = record.context

        # Phase 4: Fast read path for queries routes through the bus.
        if command_id == "query":
            query_params = dict(params)
            query_type = query_params.pop("type", None)
            if not query_type:
                return ExecutionResult(
                    status=ExecutionStatus.ERROR,
                    command_id="query",
                    error="Query type is required",
                )

            return self._execute_query_through_bus(
                session_id=session_id,
                record=record,
                ctx=ctx,
                query_type=query_type,
                query_params=query_params,
            )

        # Full write path: always route through BusTransport -> request.command -> CommandService
        self._ensure_bus_transport()

        # Long-running commands need a timeout that covers their own duration.
        timeout: float | None = None
        if command_id == "profile_gui":
            duration = float(params.get("duration_seconds", 0))
            # Cap at the command's own max plus generous headroom for the GUI to
            # report back (especially with the Julia engine, which may block the
            # runtime during recalculation before the profiler event is handled).
            from lib_command.commands.profile_gui import (
                MAX_PROFILE_DURATION_SECONDS,
                PROFILE_SHORT_HEADROOM_SECONDS,
                PROFILE_LONG_HEADROOM_SECONDS,
                PROFILE_LONG_THRESHOLD_SECONDS,
            )
            effective_duration = min(duration, MAX_PROFILE_DURATION_SECONDS)
            headroom = (
                PROFILE_LONG_HEADROOM_SECONDS
                if effective_duration >= PROFILE_LONG_THRESHOLD_SECONDS
                else PROFILE_SHORT_HEADROOM_SECONDS
            )
            timeout = effective_duration + headroom

        return self._bus_transport.execute(
            session_id=session_id,
            command_id=command_id,
            context=ctx,
            timeout=timeout,
            **params
        )

    def _execute_query_through_bus(
        self,
        session_id: str,
        record: Any,
        ctx: Any,
        query_type: str,
        query_params: dict,
        timeout: float = 5.0,
    ) -> ExecutionResult:
        """Publish a query envelope on the bus and return the correlated reply.

        This keeps the read spine consistent with the command spine: every
        query becomes a ``query.*`` bus message handled by QueryService.
        """
        correlation_id = str(uuid.uuid4())
        topic = topic_for_query_type(query_type)
        reply_topic = f"reply.query.{session_id}.{correlation_id}"
        workspace_id = None
        if ctx is not None:
            workspace = getattr(ctx, "workspace", None)
            if workspace is not None:
                workspace_id = getattr(workspace, "id", None)

        old_session_id = getattr(ctx, "session_id", None)
        ctx.session_id = session_id
        try:
            request = MessageEnvelope(
                message_id=str(uuid.uuid4()),
                message_type="query",
                topic=topic,
                correlation_id=correlation_id,
                session_id=session_id,
                client_type=record.client_type,
                workspace_id=workspace_id,
                actor_id=None,
                timestamp=time.perf_counter(),
                payload={"query_type": query_type, **query_params},
                context=ctx,
                reply_to=reply_topic,
            )

            reply = self.bus.request(
                topic=topic,
                event=request,
                reply_topic=reply_topic,
                timeout=timeout,
            )
        except Exception as exc:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                command_id="query",
                error=f"Query bus transport error: {exc}",
            )
        finally:
            ctx.session_id = old_session_id

        if reply is None:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                command_id="query",
                error=f"Query timed out after {timeout}s waiting for reply on {reply_topic}",
            )

        payload = reply.payload if isinstance(reply.payload, dict) else {}
        return ExecutionResult(
            status=ExecutionStatus.SUCCESS if reply.status == "succeeded" else ExecutionStatus.ERROR,
            command_id="query",
            data=payload.get("data"),
            error=payload.get("error"),
        )

    def query(
        self,
        session_id: str,
        query_type: str,
        **params
    ) -> Any:
        """Convenience: execute a query and return the data directly."""
        record = self.session_mgr.get_record(session_id)
        if record is None:
            return None
        ctx = record.context
        if ctx is None:
            return None

        result = self._execute_query_through_bus(
            session_id=session_id,
            record=record,
            ctx=ctx,
            query_type=query_type,
            query_params=params,
        )
        if result.success:
            return result.data
        logger.warning(
            "SessionGateway query failed: query_type=%s session_id=%s error=%s",
            query_type,
            session_id,
            result.error,
        )
        return None

    def create_session(
        self,
        client_type: str,
        engine: Any = None,
        workspace: Any = None,
        undo_manager: Any = None,
        gui_window: Any = None,
        workspace_id: Optional[str] = None,
        providers: Any = None,
    ) -> "CommandSession":
        """
        Create a client session with an internally constructed ExecutionContext.

        The GUI composition root passes minimal runtime dependencies; the
        Session Layer constructs ExecutionContext internally and never exposes it.
        """
        from .session import CommandSession
        from .executor import ExecutionContext

        if workspace_id is None and workspace is not None:
            workspace_id = getattr(workspace, "id", None)

        context = ExecutionContext(
            engine=engine,
            workspace=workspace,
            undo_manager=undo_manager,
            gui_window=gui_window,
            providers=providers,
        )
        session_id = self.session_mgr.open_session(
            client_type=client_type,
            workspace_id=workspace_id,
            context=context,
        )
        return CommandSession(self, session_id)


def get_session_gateway() -> SessionGateway:
    return SessionGateway()


def _reset_session_gateway() -> None:
    """Discard the process-wide gateway after its bus has been quiesced."""
    SessionGateway._instance = None
