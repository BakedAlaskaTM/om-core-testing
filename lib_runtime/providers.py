"""Runtime-level providers for nondeterministic system services.

Phase 0 defines and carries these dependencies through runtime composition. Later DST
phases replace direct call sites with these providers and add seeded implementations.
"""

from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, Sequence, TypeVar


T = TypeVar("T")


class ClockProvider(Protocol):
    def now_utc(self) -> datetime: ...

    def time(self) -> float: ...

    def monotonic(self) -> float: ...

    def perf_counter(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class IdProvider(Protocol):
    def uuid4(self) -> uuid.UUID: ...

    def new_id(self, prefix: str | None = None) -> str: ...


class RandomProvider(Protocol):
    def random(self) -> float: ...

    def randint(self, lower: int, upper: int) -> int: ...

    def choice(self, values: Sequence[T]) -> T: ...


class SystemClock:
    """Default clock backed by Python's system clock."""

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def perf_counter(self) -> float:
        return time.perf_counter()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class SystemIds:
    """Default ID source preserving current UUID4 behavior."""

    def uuid4(self) -> uuid.UUID:
        return uuid.uuid4()

    def new_id(self, prefix: str | None = None) -> str:
        value = self.uuid4().hex
        return f"{prefix}_{value}" if prefix else value


class SystemRandom:
    """Default random source backed by Python's module-level generator."""

    def random(self) -> float:
        return random.random()

    def randint(self, lower: int, upper: int) -> int:
        return random.randint(lower, upper)

    def choice(self, values: Sequence[T]) -> T:
        return random.choice(values)


@dataclass(frozen=True)
class RuntimeProviders:
    """Nondeterministic services accepted by the runtime composition root."""

    clock: ClockProvider
    ids: IdProvider
    random: RandomProvider

    @classmethod
    def system(cls) -> "RuntimeProviders":
        return cls(clock=SystemClock(), ids=SystemIds(), random=SystemRandom())

