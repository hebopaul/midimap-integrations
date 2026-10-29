"""Monotonic time and deadline callbacks for the worker runtime (injectable)."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol


class TimerHandle(Protocol):
    def cancel(self) -> None: ...


class Scheduler(Protocol):
    """Monotonic seconds and callbacks at a monotonic time, on the worker loop."""

    def now(self) -> float: ...

    def call_at(self, when: float, callback: Callable[[], None]) -> TimerHandle: ...


class LoopScheduler:
    """`Scheduler` on an asyncio loop's monotonic clock."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def now(self) -> float:
        return self._loop.time()

    def call_at(self, when: float, callback: Callable[[], None]) -> TimerHandle:
        return self._loop.call_at(when, callback)
