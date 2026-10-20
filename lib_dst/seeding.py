"""Key-derived deterministic services for DST workers."""

from __future__ import annotations

import hashlib
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Sequence, TypeVar

from lib_runtime.providers import RuntimeProviders


T = TypeVar("T")


def derive_seed(master_seed: int | str, domain: str) -> int:
    """Derive an order-independent 128-bit seed for *domain*."""
    payload = f"om-dst-v1\0{master_seed}\0{domain}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:16], "big")


class SeedStreams:
    def __init__(self, master_seed: int | str) -> None:
        self.master_seed = master_seed

    def seed(self, domain: str) -> int:
        return derive_seed(self.master_seed, domain)

    def random(self, domain: str) -> random.Random:
        return random.Random(self.seed(domain))


class DeterministicClock:
    """Logical clock whose reads advance by one microsecond."""

    def __init__(self, seed: int) -> None:
        seconds = seed % (50 * 365 * 24 * 60 * 60)
        self._origin = datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(
            seconds=seconds
        )
        self._ticks = 0

    def _advance(self) -> float:
        value = self._ticks / 1_000_000
        self._ticks += 1
        return value

    def now_utc(self) -> datetime:
        return self._origin + timedelta(seconds=self._advance())

    def time(self) -> float:
        return self._origin.timestamp() + self._advance()

    def monotonic(self) -> float:
        return self._advance()

    def perf_counter(self) -> float:
        return self._advance()

    def sleep(self, seconds: float) -> None:
        self._ticks += max(0, round(seconds * 1_000_000))


class DeterministicIds:
    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._counter = 0

    def uuid4(self) -> uuid.UUID:
        payload = f"{self._seed}:{self._counter}".encode("ascii")
        self._counter += 1
        raw = bytearray(hashlib.sha256(payload).digest()[:16])
        raw[6] = (raw[6] & 0x0F) | 0x40
        raw[8] = (raw[8] & 0x3F) | 0x80
        return uuid.UUID(bytes=bytes(raw))

    def new_id(self, prefix: str | None = None) -> str:
        value = self.uuid4().hex
        return f"{prefix}_{value}" if prefix else value


class DeterministicRandom:
    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)

    def random(self) -> float:
        return self._random.random()

    def randint(self, lower: int, upper: int) -> int:
        return self._random.randint(lower, upper)

    def choice(self, values: Sequence[T]) -> T:
        return self._random.choice(values)


def providers_for_seed(master_seed: int | str) -> RuntimeProviders:
    streams = SeedStreams(master_seed)
    return RuntimeProviders(
        clock=DeterministicClock(streams.seed("runtime.clock")),
        ids=DeterministicIds(streams.seed("runtime.ids")),
        random=DeterministicRandom(streams.seed("runtime.random")),
    )
