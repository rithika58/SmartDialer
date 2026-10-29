"""Two mock providers that behave differently on purpose.

Provider A -- the one that makes you feel good in a demo.
    fast, reliable, low failure rate, events in order, no duplicates.

Provider B -- the one that exists in production.
    slower, occasional timeouts, duplicate events, events out of order,
    sometimes never sends a terminal event at all.

The dialer talks to both through the identical TelecomProvider interface and
must behave correctly against either.  Nothing in the dialer is allowed to
branch on `provider.name`.
"""

from __future__ import annotations

import random
from typing import Optional

from ..domain.models import (
    PlaceCallRequest,
    PlaceCallResult,
    ProviderRejected,
    ProviderTimeout,
    new_id,
)
from .base import (
    LineBehaviour,
    ProviderBehaviour,
    TelecomProvider,
)


class _BaseMockProvider(TelecomProvider):
    """Shared happy-path machinery; the two subclasses only differ in how they
    corrupt it."""

    def place_call(self, req: PlaceCallRequest) -> PlaceCallResult:
        b = self.behaviour
        now = self.clock.now()

        if b.outage:
            self.health.record_failure(timeout=False)
            raise ProviderTimeout(f"{self.name}: provider outage")

        # 1. placement latency
        latency = self.rng.uniform(*b.place_latency)
        if self.rng.random() < b.place_timeout_rate:
            # We never even get an ack.  From the dialer's point of view this is
            # indistinguishable from a crash: it must fall back on the lease.
            self.health.record_failure(timeout=True)
            raise ProviderTimeout(f"{self.name}: place_call timed out")
        self.health.record_success(latency)

        if self.rng.random() < b.place_failure_rate:
            self.health.record_failure(timeout=False)
            raise ProviderRejected(f"{self.name}: rejected the call")

        provider_call_id = new_id("pc")
        call_id = req.call_id
        self._emit(call_id, "INITIATED", self._next_seq(call_id), delay=latency)
        self._schedule_ringing(call_id, provider_call_id, latency)
        return PlaceCallResult(True, provider_call_id)

    def _schedule_ringing(self, call_id: str, provider_call_id: str, base_delay: float) -> None:
        ring = self.rng.uniform(*self.line.ring_seconds)
        self.scheduler.schedule(
            base_delay + ring * 0.15,
            lambda: self._on_ringing(call_id, provider_call_id, ring),
        )

    def _on_ringing(self, call_id: str, provider_call_id: str, ring: float) -> None:
        seq = self._next_seq(call_id)
        eid = self._emit(call_id, "RINGING", seq)
        self._maybe_duplicate(call_id, "RINGING", seq, eid)

        answered = self.rng.random() < self.line.answer_rate
        remaining_ring = ring * 0.85
        self.scheduler.schedule(
            remaining_ring,
            lambda: self._on_ring_resolved(call_id, answered, seq),
        )

    def _on_ring_resolved(self, call_id: str, answered: bool, last_seq: int) -> None:
        b = self.behaviour
        seq = last_seq + 1

        if answered and self.rng.random() < b.out_of_order_rate:
            # Deliver ANSWERED *before* RINGING.  A dialer that trusts arrival
            # order will go ANSWERED -> RINGING and corrupt its own state.
            self._emit(call_id, "ANSWERED", seq)
            self._emit(call_id, "RINGING", last_seq, delay=0.05)
            self._schedule_connected(call_id, seq)
            return

        if answered:
            self._emit(call_id, "ANSWERED", seq)
            self._schedule_connected(call_id, seq)
        else:
            self._emit(call_id, "NO_ANSWER", seq)

    def _schedule_connected(self, call_id: str, last_seq: int) -> None:
        bridge = self.line.bridge_seconds
        talk = self.rng.uniform(*self.line.talk_seconds)
        self.scheduler.schedule(
            bridge,
            lambda: self._on_connected(call_id, last_seq + 1, talk),
        )

    def _on_connected(self, call_id: str, seq: int, talk: float) -> None:
        eid = self._emit(call_id, "CONNECTED", seq)
        self._maybe_duplicate(call_id, "CONNECTED", seq, eid)

        if self.rng.random() < self.behaviour.drop_event_rate:
            return  # provider never tells us the call ended

        self.scheduler.schedule(talk, lambda: self._end_call(call_id, seq + 1))

    def _end_call(self, call_id: str, seq: int) -> None:
        eid = self._emit(call_id, "COMPLETED", seq)
        self._maybe_duplicate(call_id, "COMPLETED", seq, eid)

    def _maybe_duplicate(self, call_id: str, event_type: str, seq: int, event_id: str) -> None:
        """Redeliver the SAME event_id.  The store's idempotency ledger absorbs it."""
        if self.rng.random() < self.behaviour.duplicate_event_rate:
            self.scheduler.schedule(
                self.rng.uniform(0.01, 0.4),
                lambda: self._emit(
                    call_id, event_type, seq, event_id=event_id
                ),
            )


class ProviderA(_BaseMockProvider):
    """Fast, reliable, low failure rate, well ordered."""

    def __init__(self, scheduler, clock, rng: Optional[random.Random] = None,
                 line: Optional[LineBehaviour] = None) -> None:
        super().__init__(
            name="ProviderA",
            behaviour=ProviderBehaviour(
                place_latency=(0.05, 0.20),
                place_failure_rate=0.005,
                place_timeout_rate=0.002,
                duplicate_event_rate=0.0,
                out_of_order_rate=0.0,
                drop_event_rate=0.0,
            ),
            line=line or LineBehaviour(),
            scheduler=scheduler,
            clock=clock,
            rng=rng,
        )


class ProviderB(_BaseMockProvider):
    """Slower, occasional timeouts, duplicate events, events out of order."""

    def __init__(self, scheduler, clock, rng: Optional[random.Random] = None,
                 line: Optional[LineBehaviour] = None) -> None:
        super().__init__(
            name="ProviderB",
            behaviour=ProviderBehaviour(
                place_latency=(0.30, 1.20),
                place_failure_rate=0.03,
                place_timeout_rate=0.05,
                duplicate_event_rate=0.35,
                out_of_order_rate=0.25,
                drop_event_rate=0.02,
            ),
            line=line or LineBehaviour(),
            scheduler=scheduler,
            clock=clock,
            rng=rng,
        )
