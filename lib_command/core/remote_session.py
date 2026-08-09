"""RemoteCommandSession — client session that routes through a TransportClientProtocol."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .executor import ExecutionResult, ExecutionStatus
from .transport_base import TransportClientProtocol

logger = logging.getLogger(__name__)

DEBUG_SESSION = False

# Commands that invoke expensive Zig RPC calls (recalculate_all, apply_rule_batch
# on large workspaces, save/load) and can far exceed the default 30s transport
# timeout.  The Zig RPC timeout for recalculate_all is 300s, so we use 310s to
# give the server a chance to respond before the transport gives up.
_LONG_RUNNING_COMMANDS: frozenset[str] = frozenset({
    "apply_rule_batch",
    "run_recalculation",
    "recalculate_all",
    "recompute_dirty",
    "save_workspace",
    "load_workspace",
    "create_new_workspace",
    "replace_workspace",
    "set_cell_hardvalues_batch_by_addr",
})
_LONG_RUNNING_TIMEOUT: float = 310.0


class RemoteCommandSession:
    """Mirrors CommandSession but depends only on TransportClientProtocol."""

    def __init__(
        self,
        transport_client: TransportClientProtocol,
        session_id: str,
        heartbeat_interval: float = 5.0,
    ):
        self.transport_client = transport_client
        self.session_id = session_id
        self._connected = True
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        # Client-side scripting variables (local, never cross remote transport)
        self._local_vars: dict = {}
        self._global_vars: dict = {}
        if heartbeat_interval > 0:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True
            )
            self._heartbeat_thread.start()

    @property
    def is_connected(self) -> bool:
        # Prefer the transport client's actual socket state; fall back to the
        # local flag for transports that don't expose it.
        if hasattr(self.transport_client, "is_connected"):
            return self.transport_client.is_connected
        return self._connected

    def execute(self, command_id: str, **params) -> ExecutionResult:
        """Execute a command through the transport."""
        logger.info("[GUI-session] execute -> %s params=%r", command_id, {k: v for k, v in params.items() if k != "payload"})
        t0 = time.perf_counter()
        # Intercept client-side script commands before remote transport
        if command_id == "set_variable":
            name = params.get("name")
            value = params.get("value")
            global_scope = params.get("global_scope", False)
            if not name:
                result = ExecutionResult(
                    status=ExecutionStatus.ERROR,
                    command_id=command_id,
                    error="set_variable requires a name",
                )
            else:
                store = self._global_vars if global_scope else self._local_vars
                store[name] = value
                result = ExecutionResult(
                    status=ExecutionStatus.SUCCESS,
                    command_id=command_id,
                    data={"name": name, "value": value, "global": global_scope},
                )
        elif not self.is_connected:
            result = ExecutionResult(
                status=ExecutionStatus.ERROR,
                command_id=command_id,
                error="Transport disconnected",
            )
        else:
            try:
                timeout = None
                if command_id == "profile_gui":
                    from lib_command.commands.profile_gui import (
                        MAX_PROFILE_DURATION_SECONDS,
                        PROFILE_SHORT_HEADROOM_SECONDS,
                        PROFILE_LONG_HEADROOM_SECONDS,
                        PROFILE_LONG_THRESHOLD_SECONDS,
                    )
                    duration = float(params.get("duration_seconds", 0))
                    effective_duration = min(duration, MAX_PROFILE_DURATION_SECONDS)
                    # Generous headroom: the GUI may be blocked by engine recalculation
                    # before it can process the profiler event and report back.
                    headroom = (
                        PROFILE_LONG_HEADROOM_SECONDS
                        if effective_duration >= PROFILE_LONG_THRESHOLD_SECONDS
                        else PROFILE_SHORT_HEADROOM_SECONDS
                    )
                    timeout = effective_duration + headroom
                elif command_id in _LONG_RUNNING_COMMANDS:
                    # These commands invoke expensive Zig RPC calls (e.g.
                    # recalculate_all with timeout=300s, apply_rule_batch on
                    # large workspaces) that can far exceed the default 30s
                    # transport timeout.
                    timeout = _LONG_RUNNING_TIMEOUT
                result = self.transport_client.send(
                    self.session_id, command_id, timeout=timeout, **params
                )
            except TimeoutError as exc:
                # A reply timeout is a slow/unresponsive server, not a closed socket.
                # Keep the socket open so further commands can retry.
                try:
                    logger.warning("Transport timeout during execute: %s", exc)
                except ValueError:
                    pass  # stdout/stderr may be closed during test teardown
                result = ExecutionResult(
                    status=ExecutionStatus.ERROR,
                    command_id=command_id,
                    error=f"Transport reply timeout: {exc}",
                )
            except (ConnectionError, OSError) as exc:
                self._connected = False
                try:
                    logger.warning("Transport error during execute: %s", exc)
                except ValueError:
                    pass  # stdout/stderr may be closed during test teardown
                result = ExecutionResult(
                    status=ExecutionStatus.ERROR,
                    command_id=command_id,
                    error=f"Transport disconnected: {exc}",
                )
        logger.info("[GUI-session] execute <- %s status=%s duration=%.3f ms", command_id, result.status, (time.perf_counter() - t0) * 1000)
        return result

    def query(self, query_id: str, **params) -> Any:
        """Execute a query through the transport and return data."""
        logger.info("[GUI-session] query -> %s", query_id)
        view_id = params.get("view_id")
        if DEBUG_SESSION:
            print(f"[SESSION-QUERY] -> {query_id} view={view_id[:8] if isinstance(view_id, str) else view_id}", flush=True)
        t0 = time.perf_counter()
        if not self.is_connected:
            return None
        try:
            result = self.transport_client.query(self.session_id, query_id, **params)
            logger.info("[GUI-session] query <- %s duration=%.3f ms", query_id, (time.perf_counter() - t0) * 1000)
            return result
        except TimeoutError as exc:
            # A reply timeout is a slow/unresponsive server, not a closed socket.
            try:
                logger.warning("Transport timeout during query %s: %s", query_id, exc)
            except ValueError:
                pass  # stdout/stderr may be closed during test teardown
            if DEBUG_SESSION:
                print(f"[SESSION-QUERY] TIMEOUT {query_id} view={view_id[:8] if isinstance(view_id, str) else view_id}", flush=True)
            return None
        except (ConnectionError, OSError) as exc:
            self._connected = False
            try:
                logger.warning("Transport error during query %s: %s", query_id, exc)
            except ValueError:
                pass  # stdout/stderr may be closed during test teardown
            if DEBUG_SESSION:
                print(f"[SESSION-QUERY] ERROR {query_id} exc={exc}", flush=True)
            return None
        except RuntimeError as exc:
            # Server-side query failure (e.g., entity not found).
            try:
                logger.warning("Query %s failed: %s", query_id, exc)
            except ValueError:
                pass
            return None

    def subscribe(self, topic: str, callback: Any) -> None:
        """Subscribe to a bus topic through the transport."""
        self.transport_client.subscribe(self.session_id, topic, callback)

    def unsubscribe(self, topic: str, callback: Any | None = None) -> None:
        """Unsubscribe from a bus topic through the transport."""
        self.transport_client.unsubscribe(self.session_id, topic, callback)

    def watch_all(self, callback: Any) -> None:
        """Subscribe to '**' catch-all pattern over remote transport."""
        self.transport_client.subscribe(self.session_id, "**", callback)

    def unwatch_all(self, callback: Any) -> None:
        """Unsubscribe from '**' catch-all pattern over remote transport."""
        self.transport_client.unsubscribe(self.session_id, "**", callback)

    def get_variables(self) -> dict:
        """Return client-local variables (never cross remote transport)."""
        return self._local_vars

    def get_global_vars(self) -> dict:
        """Return client-local global variables (never cross remote transport)."""
        return self._global_vars

    def get_workspace_snapshot(self) -> dict | None:
        """Return a workspace snapshot DTO via remote query."""
        return self.query("workspace_snapshot")

    def close(self) -> None:
        """Stop the heartbeat thread and disconnect."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=1.0)
            self._heartbeat_thread = None
        if hasattr(self.transport_client, "close"):
            self.transport_client.close()

    def _heartbeat_loop(self) -> None:
        """Background thread: ping the server periodically."""
        while not self._heartbeat_stop.is_set():
            self._heartbeat_stop.wait(self._heartbeat_interval)
            if self._heartbeat_stop.is_set():
                break
            if not self._connected:
                continue
            if not self._ping_once():
                self._connected = False
                # Intentionally silent — logging to a closed stream during
                # test teardown produces un-catchable traceback noise.

    def _ping_once(self) -> bool:
        """Send a single ping and return whether the server responded."""
        try:
            if hasattr(self.transport_client, "ping"):
                return self.transport_client.ping(self.session_id)
            return False
        except Exception:
            return False
