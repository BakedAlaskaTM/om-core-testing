"""Cooperative cancellation token for solver jobs.

Implemented via ``threading.Event`` so that cancellation is visible to
worker threads without polling a Python flag.
"""

from __future__ import annotations

import threading


class CancellationToken:
    """Thread-safe cooperative cancellation signal."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)
