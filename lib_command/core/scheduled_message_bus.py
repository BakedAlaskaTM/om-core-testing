"""Message bus whose delivery and request deadlines use a virtual scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from lib_runtime.virtual_scheduler import VirtualScheduler

from .message_bus import MessageBus


class DeliveryPolicy(Protocol):
    def delay_ns(self, topic: str, event: Any) -> int: ...


class ImmediateDeliveryPolicy:
    def delay_ns(self, topic: str, event: Any) -> int:
        return 0


@dataclass
class DeliveryRecord:
    event_id: str
    sequence: int
    scheduled_ns: int
    topic: str
    message_type: str | None
    message_id: str | None
    correlation_id: str | None
    causation_id: str | None
    delivered_ns: int | None = None


@dataclass(frozen=True)
class RequestRecord:
    request_message_id: str | None
    topic: str
    reply_topic: str
    started_ns: int
    deadline_ns: int
    completed: bool
    completed_ns: int


class ScheduledMessageBus(MessageBus):
    """Queue all publications and pump them during virtual request waits."""

    def __init__(
        self,
        scheduler: VirtualScheduler,
        policy: DeliveryPolicy | None = None,
    ) -> None:
        super().__init__()
        self.scheduler = scheduler
        self.policy = policy or ImmediateDeliveryPolicy()
        self.delivery_records: list[DeliveryRecord] = []
        self.request_records: list[RequestRecord] = []
        self._current_message_id: str | None = None

    def publish(self, topic: str, event: Any) -> None:
        delay_ns = self.policy.delay_ns(topic, event)
        if delay_ns < 0:
            raise ValueError("Delivery delay must be non-negative")
        scheduled_ns = self.scheduler.now_ns + delay_ns
        sequence = len(self.delivery_records)
        record = DeliveryRecord(
            event_id="",
            sequence=sequence,
            scheduled_ns=scheduled_ns,
            topic=topic,
            message_type=getattr(event, "message_type", None),
            message_id=getattr(event, "message_id", None),
            correlation_id=getattr(event, "correlation_id", None),
            causation_id=(
                getattr(event, "causation_id", None) or self._current_message_id
            ),
        )

        def deliver() -> None:
            record.delivered_ns = self.scheduler.now_ns
            previous_message_id = self._current_message_id
            self._current_message_id = record.message_id
            try:
                super(ScheduledMessageBus, self).publish(topic, event)
            finally:
                self._current_message_id = previous_message_id

        record.event_id = self.scheduler.call_at(scheduled_ns, deliver)
        self.delivery_records.append(record)

    def request(
        self,
        topic: str,
        event: Any,
        reply_topic: str,
        timeout: float = 5.0,
    ) -> Any | None:
        reply_container: list[Any] = []

        def receive(reply_event: Any) -> None:
            if not reply_container:
                reply_container.append(reply_event)

        started_ns = self.scheduler.now_ns
        timeout_ns = max(0, round(timeout * 1_000_000_000))
        deadline_ns = started_ns + timeout_ns
        self.subscribe(reply_topic, receive)
        try:
            self.publish(topic, event)
            completed = self.scheduler.run_until(
                lambda: bool(reply_container), deadline_ns
            )
            self.request_records.append(
                RequestRecord(
                    request_message_id=getattr(event, "message_id", None),
                    topic=topic,
                    reply_topic=reply_topic,
                    started_ns=started_ns,
                    deadline_ns=deadline_ns,
                    completed=completed,
                    completed_ns=self.scheduler.now_ns,
                )
            )
            return reply_container[0] if completed else None
        finally:
            self.unsubscribe(reply_topic, receive)
