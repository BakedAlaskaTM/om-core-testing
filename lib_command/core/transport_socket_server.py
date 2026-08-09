"""Socket transport server — Unix-domain or TCP backend."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from typing import Any, Optional

from .transport_base import TransportEndpoint
from .transport_serde import encode_envelope, decode_envelope
from .message_bus import MessageEnvelope
from .executor import ExecutionContext, ExecutionResult, ExecutionStatus
from lib_utils.config import gui

logger = logging.getLogger(__name__)

# Cap poll replies so a single event buffer cannot produce a multi-MB message.
_MAX_EVENTS_PER_POLL = 100
_MAX_EVENTS_PER_TOPIC_QUEUE = 5000


class SocketTransportServer:
    """Transport server that listens on a Unix-domain or TCP socket."""

    def __init__(
        self,
        endpoint: TransportEndpoint,
        timeout: Optional[float] = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout if timeout is not None else gui("transport", "server_timeout_seconds", 5.0)
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._active_conns: set[socket.socket] = set()
        self._conns_lock = threading.Lock()

    def start(self) -> None:
        """Start the server in a background thread.

        Bind is done synchronously so port/socket-in-use errors propagate
        immediately to the caller.
        """
        self._stop_event.clear()
        self._sock = self._bind()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the server and clean up."""
        self._stop_event.set()
        with self._conns_lock:
            conns = list(self._active_conns)
            self._active_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.endpoint.kind == "unix" and self.endpoint.path:
            try:
                os.unlink(self.endpoint.path)
            except FileNotFoundError:
                pass

    def _bind(self) -> socket.socket:
        """Create and bind the listening socket."""
        if self.endpoint.kind == "unix":
            path = self.endpoint.path
            assert path is not None
            # Remove stale socket
            if os.path.exists(path):
                try:
                    test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    test_sock.connect(path)
                    test_sock.close()
                    raise RuntimeError(f"Socket already in use: {path}")
                except (ConnectionRefusedError, FileNotFoundError):
                    os.unlink(path)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(path)
            sock.listen(5)
            return sock
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            host = self.endpoint.host
            port = self.endpoint.port
            assert port is not None
            sock.bind((host, port))
            sock.listen(5)
            return sock

    def _serve(self) -> None:
        """Server loop: accept connections and spawn handler threads."""
        logger.info("Transport server listening on %s", self.endpoint)

        while not self._stop_event.is_set():
            try:
                self._sock.settimeout(0.5)
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            logger.info("Transport client connected: %s", addr)
            with self._conns_lock:
                self._active_conns.add(conn)
            handler_thread = threading.Thread(
                target=self._handle_connection_thread,
                args=(conn,),
                daemon=True,
            )
            handler_thread.start()

    def _handle_connection_thread(self, conn: socket.socket) -> None:
        """Wrapper to handle a single connection and clean up."""
        try:
            self._handle_connection(conn)
        except Exception as exc:
            logger.error("Transport connection error: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            with self._conns_lock:
                self._active_conns.discard(conn)

    def _handle_connection(self, conn: socket.socket) -> None:
        """Handle a single client connection."""
        from .session_manager import get_session_manager
        from .session_gateway import get_session_gateway
        from .message_bus import get_message_bus

        session_mgr = get_session_manager()
        gateway = get_session_gateway()
        bus = get_message_bus()

        # Per-connection event subscription state (local to this thread)
        conn_event_queues: dict[str, list] = {}  # topic -> [MessageEnvelope]
        conn_bus_subs: dict[str, Any] = {}  # topic -> callback

        # Buffer for incomplete lines
        buf = b""

        while not self._stop_event.is_set():
            try:
                conn.settimeout(self.timeout)
                data = conn.recv(4096)
                if not data:
                    break
                buf += data

                # Process complete lines
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        request = decode_envelope(line)
                    except Exception as exc:
                        logger.warning("Malformed JSON from client: %s", exc)
                        self._send_reply(
                            conn,
                            topic="reply.error",
                            correlation_id="",
                            payload={"status": "failed", "error": f"Malformed JSON: {exc}"},
                        )
                        continue

                    try:
                        reply = self._handle_request(
                            request, session_mgr, gateway,
                            conn_event_queues, conn_bus_subs,
                        )
                    except Exception as exc:
                        logger.error("Request handling error: %s", exc)
                        reply = MessageEnvelope(
                            message_id=uuid.uuid4().hex,
                            message_type="reply",
                            topic=f"reply.error.{request.correlation_id}",
                            correlation_id=request.correlation_id,
                            session_id=request.session_id,
                            client_type=request.client_type,
                            workspace_id=None,
                            actor_id=None,
                            timestamp=time.perf_counter(),
                            payload={"status": "failed", "error": str(exc)},
                            context=None,
                            status="failed",
                        )

                    try:
                        conn.sendall(encode_envelope(reply))
                    except Exception as exc:
                        logger.error("Failed to send reply: %s", exc)
                        break

            except socket.timeout:
                continue
            except (ConnectionResetError, BrokenPipeError):
                break
            except OSError:
                break

        # Clean up per-connection bus subscriptions
        for t, cb in list(conn_bus_subs.items()):
            try:
                bus.unsubscribe(t, cb)
            except Exception:
                pass

    def _handle_request(
        self,
        request: MessageEnvelope,
        session_mgr: Any,
        gateway: Any,
        conn_event_queues: dict[str, list] | None = None,
        conn_bus_subs: dict[str, Any] | None = None,
    ) -> MessageEnvelope:
        """Dispatch a single request to the appropriate handler."""
        topic = request.topic
        correlation_id = request.correlation_id
        if conn_event_queues is None:
            conn_event_queues = {}
        if conn_bus_subs is None:
            conn_bus_subs = {}

        if topic == "request.session.open":
            return self._handle_session_open(request, session_mgr)

        if topic == "request.transport.ping":
            return self._make_reply(
                request,
                "reply.transport.ping",
                {"status": "succeeded", "data": {"pong": True}},
            )

        if topic == "request.poll_events":
            return self._handle_poll_events(request, conn_event_queues, conn_bus_subs)

        # For command/query, session must exist
        session_id = request.session_id
        if not session_id:
            return self._make_reply(
                request,
                f"reply.error.{correlation_id}",
                {"status": "failed", "error": "Missing session_id"},
                status="failed",
            )

        record = session_mgr.get_record(session_id)
        if record is None:
            return self._make_reply(
                request,
                f"reply.error.{correlation_id}",
                {"status": "failed", "error": f"Session '{session_id}' not found"},
                status="failed",
            )

        ctx = record.context

        if topic == "request.command":
            payload = request.payload or {}
            command_id = payload.get("command_id")
            if not command_id:
                return self._make_reply(
                    request,
                    f"reply.error.{correlation_id}",
                    {"status": "failed", "error": "Missing command_id"},
                    status="failed",
                )
            params = {k: v for k, v in payload.items() if k != "command_id"}
            result = gateway.send(session_id, command_id, **params)
            return self._result_to_reply(request, result)

        if topic == "request.query":
            payload = request.payload or {}
            query_id = payload.get("query_id")
            if not query_id:
                return self._make_reply(
                    request,
                    f"reply.error.{correlation_id}",
                    {"status": "failed", "error": "Missing query_id"},
                    status="failed",
                )
            params = {k: v for k, v in payload.items() if k != "query_id"}
            record = session_mgr.get_record(session_id)
            if record is None:
                return self._make_reply(
                    request,
                    f"reply.error.{correlation_id}",
                    {"status": "failed", "error": f"Session '{session_id}' not found"},
                    status="failed",
                )
            result = gateway._execute_query_through_bus(
                session_id=session_id,
                record=record,
                ctx=record.context,
                query_type=query_id,
                query_params=params,
            )
            if result.success:
                return self._make_reply(
                    request,
                    f"reply.query.{session_id}.{correlation_id}",
                    {"status": "succeeded", "data": result.data},
                )
            else:
                return self._make_reply(
                    request,
                    f"reply.query.{session_id}.{correlation_id}",
                    {"status": "failed", "data": None, "error": result.error},
                    status="failed",
                )

        return self._make_reply(
            request,
            f"reply.error.{correlation_id}",
            {"status": "failed", "error": f"Unknown topic: {topic}"},
            status="failed",
        )

    def _handle_poll_events(
        self,
        request: MessageEnvelope,
        conn_event_queues: dict[str, list],
        conn_bus_subs: dict[str, Any],
    ) -> MessageEnvelope:
        """Handle event poll request from a remote client.

        Subscribes to requested topics on the bus (if not already), returns
        buffered events, and clears the buffer.
        """
        from .message_bus import get_message_bus

        bus = get_message_bus()
        payload = request.payload or {}
        topics = payload.get("topics", [])

        # Ensure subscription for each topic
        for t in topics:
            if t not in conn_bus_subs:
                def _make_handler(topic_name: str, queues: dict[str, list] = conn_event_queues):
                    def handler(event: Any) -> None:
                        q = queues.setdefault(topic_name, [])
                        q.append(event)
                        if len(q) > _MAX_EVENTS_PER_TOPIC_QUEUE:
                            del q[:len(q) - _MAX_EVENTS_PER_TOPIC_QUEUE]
                    return handler

                cb = _make_handler(t)
                conn_bus_subs[t] = cb
                bus.subscribe(t, cb)

        # Collect buffered events, capping per poll to avoid huge replies.
        events: list[dict] = []
        remaining: dict[str, list] = {}
        profiler_returned = False
        for t in topics:
            queued = conn_event_queues.pop(t, [])
            for event in queued[:_MAX_EVENTS_PER_POLL]:
                from .transport_serde import envelope_to_wire
                try:
                    events.append(envelope_to_wire(event))
                    if t == "event.profiler.start":
                        profiler_returned = True
                except Exception:
                    # Skip unserializable events rather than failing the whole poll
                    pass
            if len(queued) > _MAX_EVENTS_PER_POLL:
                remaining[t] = queued[_MAX_EVENTS_PER_POLL:]
        if remaining:
            conn_event_queues.update(remaining)

        if profiler_returned:
            logger.warning("[transport-server] returning profiler start event to client")
        return self._make_reply(
            request,
            "reply.poll_events",
            {"status": "succeeded", "data": {"events": events}},
        )

    def _handle_session_open(
        self,
        request: MessageEnvelope,
        session_mgr: Any,
    ) -> MessageEnvelope:
        """Create a new session and return its ID.

        If a GUI session already exists, share its session ID so remote
        clients (REPL, macros, scripts) operate on the same session state
        as the GUI.  This ensures selection and active-view changes from
        any client are visible to all clients connected to the same GUI.
        """
        client_type = request.payload.get("client_type", "repl") if request.payload else "repl"

        # Share existing GUI session when available so all clients
        # connected to the same workspace see the same session state.
        for record in session_mgr.list_active():
            if record.client_type == "gui" and record.context:
                session_id = record.session_id
                return self._make_reply(
                    request,
                    f"reply.session.{request.correlation_id}",
                    {"status": "succeeded", "data": {"session_id": session_id}},
                    session_id=session_id,
                )

        # No GUI session yet — create a standalone session
        ctx = ExecutionContext()
        session_id = session_mgr.open_session(
            client_type=client_type,
            workspace_id=None,
            context=ctx,
        )
        return self._make_reply(
            request,
            f"reply.session.{request.correlation_id}",
            {"status": "succeeded", "data": {"session_id": session_id}},
            session_id=session_id,
        )

    def _result_to_reply(
        self,
        request: MessageEnvelope,
        result: ExecutionResult,
    ) -> MessageEnvelope:
        """Convert ExecutionResult to reply envelope."""
        session_id = request.session_id or ""
        topic = f"reply.command.{session_id}.{request.correlation_id}"
        return self._make_reply(
            request,
            topic,
            {
                "status": result.status.name.lower(),
                "data": result.data,
                "error": result.error,
            },
            status="succeeded" if result.success else "failed",
        )

    def _make_reply(
        self,
        request: MessageEnvelope,
        topic: str,
        payload: dict,
        session_id: Optional[str] = None,
        status: str = "succeeded",
    ) -> MessageEnvelope:
        return MessageEnvelope(
            message_id=uuid.uuid4().hex,
            message_type="reply",
            topic=topic,
            correlation_id=request.correlation_id,
            session_id=session_id or request.session_id,
            client_type=request.client_type,
            workspace_id=None,
            actor_id=None,
            timestamp=time.perf_counter(),
            payload=payload,
            context=None,
            status=status,
        )

    def _send_reply(
        self,
        conn: socket.socket,
        topic: str,
        correlation_id: str,
        payload: dict,
    ) -> None:
        reply = MessageEnvelope(
            message_id=uuid.uuid4().hex,
            message_type="reply",
            topic=topic,
            correlation_id=correlation_id,
            session_id=None,
            client_type=None,
            workspace_id=None,
            actor_id=None,
            timestamp=time.perf_counter(),
            payload=payload,
            context=None,
            status="failed",
        )
        try:
            conn.sendall(encode_envelope(reply))
        except Exception as exc:
            logger.error("Failed to send error reply: %s", exc)
