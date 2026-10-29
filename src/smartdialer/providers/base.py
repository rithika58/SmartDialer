"""The telecom provider boundary.

The dialer must not know anything about how a provider works internally.  It
only knows:

  * place_call(request) -> ack   (may raise ProviderError)
  * events arrive later, asynchronously, on the event sink

Two mock providers are provided, and they are deliberately *not* well behaved.
"""

from __future__ import annotations

import heapq
import itertools
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Protocol

from ..domain.models import (
    PlaceCallRequest,
    PlaceCallResult,
    ProviderError,
    ProviderEvent,
    ProviderTimeout,
    ProviderUnavailable,
    new_id,
)

EventSink = Callable[[ProviderEvent], None]


# --------------------------------------------------------------------------- #
# Scheduling: real threads for the demo, a virtual heap for the simulator.
# --------------------------------------------------------------------------- #
class Scheduler(Protocol):
    def schedule(self, delay: float, fn: Callable[[], None]) -> None: ...


class VirtualScheduler:
    """Fires callbacks against a VirtualClock when the simulator advances it."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self._heap: list = []
        self._counter = itertools.count()

    def schedule(self, delay: float, fn: Callable[[], None]) -> None:
        heapq.heappush(self._heap, (self.clock.now() + delay, next(self._counter), fn))

    def pending(self) -> int:
        return len(self._heap)

    def run_until(self, t: float) -> int:
        """Fire everything scheduled at or before t.  Returns how many fired."""
        fired = 0
        while self._heap and self._heap[0][0] <= t:
            _, _, fn = heapq.heappop(self._heap)
            fn()
            fired += 1
        return fired

    def next_time(self) -> Optional[float]:
        return self._heap[0][0] if self._heap else None


class ThreadScheduler:
    """Real background timers.  Used by the live demo scripts."""

    def __init__(self, clock=None) -> None:
        self.clock = clock
        self._timers: List[threading.Timer] = []

    def schedule(self, delay: float, fn: Callable[[], None]) -> None:
        t = threading.Timer(max(delay, 0.0), fn)
        t.daemon = True
        self._timers.append(t)
        t.start()

    def shutdown(self) -> None:
        for t in self._timers:
            t.cancel()


# --------------------------------------------------------------------------- #
# Provider behaviour configuration
# --------------------------------------------------------------------------- #
@dataclass
class LineBehaviour:
    """What the *borrowers* on this campaign do.  Supplied by the scenario."""
    answer_rate: float = 0.5
    ring_seconds: tuple = (4.0, 12.0)
    talk_seconds: tuple = (60.0, 120.0)
    bridge_seconds: float = 0.5


@dataclass
class ProviderBehaviour:
    """How the *provider itself* misbehaves.  This is what the dialer must
    survive; it is not something the dialer is allowed to depend on."""
    place_latency: tuple = (0.05, 0.20)
    place_failure_rate: float = 0.0      # hard reject at placement
    place_timeout_rate: float = 0.0      # slow / no answer to our API call
    duplicate_event_rate: float = 0.0    # same event_id delivered twice
    out_of_order_rate: float = 0.0       # events delivered in the wrong order
    drop_event_rate: float = 0.0         # provider never sends a terminal event
    outage: bool = False                 # provider is down


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class ProviderHealthMonitor:
    """Tracks one provider's health and refuses traffic when it is sick.

    Why a circuit breaker and not just retries: when a provider starts timing
    out, retrying harder is what turns a slow provider into a dead one.  The
    breaker sheds load so the provider can recover, and lets a small probe
    through to find out when it has.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_seconds: float = 15.0,
        half_open_probes: int = 2,
        clock=None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.half_open_probes = half_open_probes
        self.clock = clock
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.total_calls = 0
        self.total_failures = 0
        self.total_timeouts = 0
        self.opened_at: Optional[float] = None
        self.probes_allowed = 0
        self.latencies: List[float] = []
        self._lock = threading.Lock()

    def _now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    def allow_request(self) -> bool:
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            now = self._now()
            if self.state == CircuitState.OPEN:
                if self.opened_at is not None and now - self.opened_at >= self.recovery_seconds:
                    self.state = CircuitState.HALF_OPEN
                    self.probes_allowed = self.half_open_probes
                    return True
                return False
            # HALF_OPEN
            if self.probes_allowed > 0:
                self.probes_allowed -= 1
                return True
            return False

    def record_success(self, latency: float) -> None:
        with self._lock:
            self.total_calls += 1
            self.latencies.append(latency)
            if len(self.latencies) > 1000:
                self.latencies = self.latencies[-1000:]
            self.consecutive_failures = 0
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                self.opened_at = None

    def record_failure(self, timeout: bool = False) -> None:
        with self._lock:
            self.total_calls += 1
            self.total_failures += 1
            if timeout:
                self.total_timeouts += 1
            self.consecutive_failures += 1
            if self.state == CircuitState.HALF_OPEN:
                self._trip()
            elif self.consecutive_failures >= self.failure_threshold:
                self._trip()

    def _trip(self) -> None:
        self.state = CircuitState.OPEN
        self.opened_at = self._now()
        self.probes_allowed = 0

    @property
    def is_healthy(self) -> bool:
        return self.state != CircuitState.OPEN

    def snapshot(self) -> dict:
        with self._lock:
            avg = sum(self.latencies) / len(self.latencies) if self.latencies else 0.0
            return {
                "provider": self.name,
                "state": self.state.value,
                "total_calls": self.total_calls,
                "failures": self.total_failures,
                "timeouts": self.total_timeouts,
                "consecutive_failures": self.consecutive_failures,
                "avg_latency": round(avg, 4),
            }


# --------------------------------------------------------------------------- #
# Provider interface
# --------------------------------------------------------------------------- #
class TelecomProvider(ABC):
    """Everything the dialer is allowed to know about a telecom provider."""

    def __init__(
        self,
        name: str,
        behaviour: ProviderBehaviour,
        line: LineBehaviour,
        scheduler: Scheduler,
        clock,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.name = name
        self.behaviour = behaviour
        self.line = line
        self.scheduler = scheduler
        self.clock = clock
        self.rng = rng or random.Random(1234)
        self.health = ProviderHealthMonitor(name, clock=clock)
        self._sink: Optional[EventSink] = None
        self._seq: Dict[str, itertools.count] = {}

    def on_event(self, sink: EventSink) -> None:
        self._sink = sink

    def _emit(
        self,
        call_id: str,
        event_type: str,
        seq: int,
        event_id: Optional[str] = None,
        delay: float = 0.0,
        duplicate_of: Optional[str] = None,
    ) -> str:
        eid = event_id or duplicate_of or new_id("evt")
        event = ProviderEvent(
            event_id=eid,
            call_id=call_id,
            seq=seq,
            type=event_type,
            at=self.clock.now() + delay,
        )
        if self._sink is None:
            return eid

        if delay <= 0:
            self._sink(event)
        else:
            self.scheduler.schedule(delay, lambda: self._sink(event))
        return eid

    def _next_seq(self, call_id: str) -> int:
        if call_id not in self._seq:
            self._seq[call_id] = itertools.count(1)
        return next(self._seq[call_id])

    @abstractmethod
    def place_call(self, req: PlaceCallRequest) -> PlaceCallResult:
        """Start an outbound call.  Raises ProviderError on failure."""
        ...

    def cancel_call(self, provider_call_id: str) -> bool:
        return True
