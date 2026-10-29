"""Time is injected, never read directly.

This is the single decision that makes the whole system testable: the pacing
engine, the safety controller and the lease logic all work against a Clock.
Production uses RealClock; the simulator and the tests use VirtualClock, which
lets us run eight hours of dialing in under a second.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def __repr__(self) -> str:
        return "RealClock()"


class VirtualClock:
    """A monotonic clock you advance by hand.

    `sleep` is a no-op because in virtual time nothing ever waits -- the
    scheduler decides when the next thing happens.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        return None

    def advance(self, seconds: float) -> float:
        with self._lock:
            self._t += seconds
            return self._t

    def set(self, t: float) -> None:
        with self._lock:
            self._t = t

    def __repr__(self) -> str:
        return f"VirtualClock(t={self._t:.3f})"
