"""Event Bus for command lifecycle events.

Provides a centralized publish/subscribe system for command execution events.
All communication between major subsystems flows through the Event Bus.

Event naming convention:
- command.<id>.before → Specific command before (e.g., command.set.before)
- command.<id>.succeeded → Specific command succeeded
- command.<id>.failed  → Specific command failed

Wildcard subscription patterns:
- command.*.before    → matches all command pre-execution events
- command.*.succeeded → matches all command success events
- command.*.failed    → matches all command failure events
- * matches exactly one dot-delimited segment
"""

from __future__ import annotations

import logging
import uuid
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _topic_matches(pattern: str, topic: str) -> bool:
    """Match topic against pattern where * matches one dot-delimited segment.

    ** matches any topic regardless of segment count (catch-all).

    Examples:
        _topic_matches("command.*.succeeded", "command.set_cell_value.succeeded") → True
        _topic_matches("command.*.succeeded", "command.succeeded") → False
        _topic_matches("command.set_cell_value.succeeded", "command.set_cell_value.succeeded") → True
        _topic_matches("**", "command.set_cell_value.succeeded") → True
    """
    if pattern == "**":
        return True
    pattern_parts = pattern.split(".")
    topic_parts = topic.split(".")
    if len(pattern_parts) != len(topic_parts):
        return False
    return all(pp == "*" or pp == tp for pp, tp in zip(pattern_parts, topic_parts))


# Module-level bus configuration
_BUS_CONFIG = {
    "debug": False,     # Enable verbose logging of event traffic
    "enabled": True,    # Allow disabling bus entirely (e.g., legacy tests)
}

# Module-level singleton bus instance
_bus_instance: Optional[MessageBus] = None


@dataclass(frozen=True)
class MessageEnvelope:
    """
    Standard wrapper for all bus traffic. Carries session metadata alongside
    the legacy CommandEvent fields so existing subscribers continue to work.

    message_type: command | query | event | reply | error
    status: accepted | rejected | succeeded | failed
    """
    message_id: str
    message_type: str        # command | query | event | reply | error
    topic: str
    correlation_id: str
    session_id: Optional[str]
    client_type: Optional[str]   # gui | repl | cli | headless
    workspace_id: Optional[str]
    actor_id: Optional[str]
    timestamp: float
    payload: dict
    context: Any               # ExecutionContext
    causation_id: Optional[str] = None
    status: str = "accepted"   # accepted | rejected | succeeded | failed
    command_id: str = ""       # backward-compat with CommandEvent
    reply_to: Optional[str] = None  # reply topic for request/reply transport

    @property
    def params(self) -> dict:
        """Backward-compat property: returns payload dict."""
        return self.payload


class MessageBus:
    """Central publish/subscribe message router.

    All communication between major subsystems flows through the Message Bus.
    No direct links between clients and engine, engine and middleware, etc.

    The bus supports exact topic matching. Wildcard matching is not required
    for step 1.

    Example usage:
        bus = get_message_bus()
        bus.subscribe("command.set.before", handler)
        bus.publish("command.set.before", MessageEnvelope(...))
        bus.unsubscribe("command.set.before", handler)
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[Any], None]]] = {}
        self._publish_hooks: list[Callable[[str, Any], None]] = []

    def subscribe(self, topic: str, callback: Callable[[Any], None]) -> None:
        """Subscribe a callback to a topic.

        Args:
            topic: The topic name (e.g., 'command.set.before')
            callback: Function that receives the event
        """
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        self._subscribers[topic].append(callback)
        if _BUS_CONFIG["debug"]:
            logger.debug(f"EventBus: subscribed to topic '{topic}'")

    def unsubscribe(self, topic: str, callback: Callable[[Any], None]) -> None:
        """Unsubscribe a callback from a topic.

        Args:
            topic: The topic name
            callback: The callback to remove
        """
        if topic in self._subscribers:
            try:
                self._subscribers[topic].remove(callback)
            except ValueError:
                pass  # Callback was not subscribed
            if not self._subscribers[topic]:
                del self._subscribers[topic]

    def add_publish_hook(self, hook: Callable[[str, Any], None]) -> None:
        """Register a hook that receives every publish(topic, event) call.

        Hooks run before subscribers and are called regardless of whether
        any subscriber exists for the topic.  Useful for audit logging,
        tracing, and read-model projections.
        """
        self._publish_hooks.append(hook)

    def remove_publish_hook(self, hook: Callable[[str, Any], None]) -> None:
        """Unregister a publish hook."""
        try:
            self._publish_hooks.remove(hook)
        except ValueError:
            pass

    def publish(self, topic: str, event: Any) -> None:
        """Publish an event to all subscribers.

        Exceptions raised by subscribers are caught and logged.
        This ensures one broken subscriber doesn't break the bus or command execution.

        Args:
            topic: The topic name
            event: The event to publish (must be MessageEnvelope)
        """
        if not _BUS_CONFIG["enabled"]:
            return  # Bus is disabled

        for hook in list(self._publish_hooks):
            try:
                hook(topic, event)
            except Exception as e:
                logger.error(f"Publish hook error on topic '{topic}': {e}", exc_info=True)

        # Collect matching subscribers, deduplicated by callback identity.
        # Bound methods create a new temporary object on each access, so
        # id(callback) is unreliable — use (id(__self__), id(__func__)) instead.
        seen: set[tuple] = set()
        callbacks: list[Callable[[Any], None]] = []
        for pattern, subs in list(self._subscribers.items()):
            if _topic_matches(pattern, topic):
                for callback in list(subs):
                    if hasattr(callback, "__self__") and hasattr(callback, "__func__"):
                        key: tuple = (id(callback.__self__), id(callback.__func__))
                    else:
                        key = (id(callback),)
                    if key in seen:
                        continue
                    seen.add(key)
                    callbacks.append(callback)

        if not callbacks:
            if topic == "event.profiler.start":
                logger.warning("EventBus: publish '%s' has NO matching subscribers", topic)
            return

        if _BUS_CONFIG["debug"] or topic == "event.profiler.start":
            logger.warning("EventBus: publishing to '%s' (%d matching subscribers)", topic, len(callbacks))

        for callback in callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"Subscriber error on topic '{topic}': {e}", exc_info=True)

    def request(
        self,
        topic: str,
        event: Any,
        reply_topic: str,
        timeout: float = 5.0,
    ) -> Optional[Any]:
        """Publish a request and block until a correlated reply is received.

        This method is transport-only: it does not validate, resolve, or
        execute.  It adds request/reply semantics on top of the existing
        pub/sub bus using a one-shot subscriber and a local threading.Event.

        The current in-process MessageBus is synchronous: subscribers run
        inline during publish() before it returns.  Therefore the reply may
        already be satisfied before wait() is called.  The threading.Event
        pattern is kept intentionally because it is forward-compatible with
        threaded, async, or remote transport later.

        Args:
            topic: The topic to publish the request to (e.g. 'request.command')
            event: The request envelope (typically MessageEnvelope)
            reply_topic: The topic to wait for a reply on
            timeout: Maximum seconds to wait for a reply

        Returns:
            The reply envelope, or None on timeout.
        """
        import threading

        reply_container: list[Any] = [None]
        event_flag = threading.Event()

        def _reply_handler(reply_event: Any) -> None:
            reply_container[0] = reply_event
            event_flag.set()

        self.subscribe(reply_topic, _reply_handler)
        try:
            self.publish(topic, event)
            if event_flag.wait(timeout=timeout):
                return reply_container[0]
            return None
        finally:
            self.unsubscribe(reply_topic, _reply_handler)


def get_message_bus() -> MessageBus:
    """Get the global message bus instance.

    Returns a singleton MessageBus instance. Created on first call.

    This design:
    - Allows the executor to import and publish to the bus
    - Allows the GUI to subscribe to the bus
    - Avoids threading executor references through multiple layers
    - Makes the message bus a first-class dependency accessible from anywhere
    """
    global _bus_instance
    if _bus_instance is None:
        _bus_instance = MessageBus()
    return _bus_instance


def set_bus_debug(enabled: bool) -> None:
    """Enable or disable verbose event bus logging."""
    _BUS_CONFIG["debug"] = enabled


def set_bus_enabled(enabled: bool) -> None:
    """Enable or disable the event bus entirely.

    When disabled, publish() returns immediately without calling subscribers.
    Useful for legacy test paths.
    """
    _BUS_CONFIG["enabled"] = enabled


def _reset_bus_for_testing() -> None:
    """Reset bus to clean state for testing.

    Use in test fixtures to ensure isolation between tests.
    """
    global _bus_instance
    if _bus_instance is not None:
        _bus_instance._subscribers.clear()
        _bus_instance._publish_hooks.clear()
        _bus_instance = None