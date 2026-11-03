"""Deterministic single-threaded scheduler with integer nanosecond time."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Callable, Protocol


EventId = str


class Scheduler(Protocol):
    @property
    def now_ns(self) -> int: ...

    def call_at(self, when_ns: int, callback: Callable[[], None]) -> EventId: ...

    def run_next(self) -> bool: ...

    def run_until(self, predicate: Callable[[], bool], deadline_ns: int) -> bool: ...


@dataclass(order=True)
class _ScheduledCall:
    when_ns: int
    sequence: int
    event_id: EventId
    callback: Callable[[], None] = field(compare=False, repr=False)


@dataclass(frozen=True)
class PendingCall:
    event_id: EventId
    when_ns: int
    sequence: int


class VirtualScheduler:
    """Run callbacks deterministically without wall-clock waits or threads."""

    def __init__(self, *, start_ns: int = 0) -> None:
        if start_ns < 0:
            raise ValueError("start_ns must be non-negative")
        self._now_ns = start_ns
        self._sequence = 0
        self._queue: list[_ScheduledCall] = []

    @property
    def now_ns(self) -> int:
        return self._now_ns

    def call_at(self, when_ns: int, callback: Callable[[], None]) -> EventId:
        if when_ns < self._now_ns:
            raise ValueError("Cannot schedule an event in the virtual past")
        sequence = self._sequence
        self._sequence += 1
        event_id = f"event-{sequence:08d}"
        heapq.heappush(
            self._queue,
            _ScheduledCall(when_ns, sequence, event_id, callback),
        )
        return event_id

    def run_next(self) -> bool:
        if not self._queue:
            return False
        scheduled = heapq.heappop(self._queue)
        self._now_ns = scheduled.when_ns
        scheduled.callback()
        return True

    def run_until(self, predicate: Callable[[], bool], deadline_ns: int) -> bool:
        if deadline_ns < self._now_ns:
            raise ValueError("Virtual deadline precedes current time")
        while not predicate():
            if not self._queue or self._queue[0].when_ns > deadline_ns:
                self._now_ns = deadline_ns
                return False
            self.run_next()
        return True

    def run_all(self, *, max_events: int = 100_000) -> int:
        processed = 0
        while self._queue:
            if processed >= max_events:
                raise RuntimeError("Virtual scheduler event limit exceeded")
            self.run_next()
            processed += 1
        return processed

    def pending(self) -> tuple[PendingCall, ...]:
        return tuple(
            PendingCall(call.event_id, call.when_ns, call.sequence)
            for call in sorted(self._queue)
        )
