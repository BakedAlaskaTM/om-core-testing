"""SolverEventPort implementation: bounded in-memory enqueue with background drain.

Events are enqueued into a bounded FIFO queue.  A background thread
drains the queue to the runtime EventPublisher (if available).  Queue
overflow drops events and logs a diagnostic.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

logger = logging.getLogger(__name__)

_MAX_QUEUE_SIZE = 256
_DRAIN_TIMEOUT = 0.1


class RuntimeEventPublisher:
    """Bounded in-memory event queue with background drain.

    Implements the ``SolverEventPort`` protocol.
    """

    def __init__(self, event_publisher: Any = None) -> None:
        self._event_publisher = event_publisher
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._drain_thread: threading.Thread | None = None
        self._stopped = False

        if event_publisher is not None:
            self._drain_thread = threading.Thread(
                target=self._drain_loop,
                name="solver-event-drain",
                daemon=True,
            )
            self._drain_thread.start()

    def publish_event(self, event: dict) -> None:
        """Enqueue a solver event. Returns immediately.

        Queue overflow drops the event and logs a diagnostic.
        """
        if self._stopped:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning(
                "Solver event queue overflow — dropping event: %s",
                event.get("event_id", "unknown"),
            )

    def stop(self) -> None:
        """Signal the drain thread to stop."""
        self._stopped = True
        self._queue.put(None)  # Sentinel to wake the drain thread.
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=2.0)

    def _drain_loop(self) -> None:
        """Background drain loop."""
        while not self._stopped:
            try:
                event = self._queue.get(timeout=_DRAIN_TIMEOUT)
            except queue.Empty:
                continue
            if event is None:
                break
            try:
                if self._event_publisher is not None:
                    self._event_publisher.publish(
                        event.get("event_id", "event.solver"),
                        event,
                        None,
                    )
            except Exception:
                logger.exception("Failed to drain solver event: %s", event)
