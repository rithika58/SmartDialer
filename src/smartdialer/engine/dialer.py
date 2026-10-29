"""
The dialer worker: one campaign loop.

A tick does exactly this, in this order:

    snapshot -> pacing engine proposes -> safety controller disposes ->
    allocator dials (through the gateway, which verifies the approval)

The worker never decides how many calls to make.  It only gathers the snapshot
and carries the decision to the allocator.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..domain.models import Call, ProviderEvent, new_id
from ..domain.states import (
    AGENT_BUSY_STATES,
    AgentState,
    CallState,
    RejectReason,
)
from ..providers.base import Scheduler
from ..stats import BetaBinomialEstimate, ServiceEstimate, ewma
from .allocator import CallAllocator, DialBatchResult, ProviderGateway
from .pacing import AdaptivePacer, PacingEngine, PacingInputs, PacingMode
from .safety import SafetyContext, SafetyController, SafetyDecision


@dataclass
class Metrics:
    calls_initiated: int = 0
    calls_ringing: int = 0
    calls_answered: int = 0
    calls_connected: int = 0
    calls_completed: int = 0
    calls_failed: int = 0
    place_failures: int = 0
    abandoned: int = 0
    duplicate_events: int = 0
    rejected_events: Dict[str, int] = field(default_factory=dict)
    leases_reclaimed: int = 0
    safety: Dict[str, int] = field(default_factory=dict)

    # Agent-time accounting, in agent-seconds.
    recent_outcomes: deque = field(default_factory=lambda: deque(maxlen=1000))
    # Last value computed by window_abandon_rate(), for reporting.
    _window_rate_cache: float = 0.0
    talk_seconds: float = 0.0        # CONNECTED: actually talking to a borrower
    wrap_seconds: float = 0.0        # WRAP_UP
    setup_seconds: float = 0.0       # RESERVED + DIALING: waiting for a borrower
    idle_seconds: float = 0.0        # AVAILABLE: on shift, doing nothing

    @property
    def on_shift_seconds(self) -> float:
        return (
            self.talk_seconds + self.wrap_seconds
            + self.setup_seconds + self.idle_seconds
        )

    @property
    def busy_seconds(self) -> float:
        return self.talk_seconds + self.wrap_seconds + self.setup_seconds

    @property
    def talk_utilization(self) -> float:
        """Fraction of on-shift time spent actually talking.
        This is the number a dialer is judged on."""
        total = self.on_shift_seconds
        return self.talk_seconds / total if total > 0 else 0.0

    @property
    def occupancy(self) -> float:
        """Fraction of on-shift time the agent is not idle."""
        total = self.on_shift_seconds
        return self.busy_seconds / total if total > 0 else 0.0

    @property
    def answer_rate(self) -> float:
        return self.calls_answered / self.calls_initiated if self.calls_initiated else 0.0

    @property
    def abandon_rate(self) -> float:
        return self.abandoned / self.calls_answered if self.calls_answered else 0.0

    def window_abandon_rate(self, now: float, seconds: float = 60.0) -> float:
        """Abandon rate over the last `seconds`, not over the last N answers.

        A cumulative rate can never recover from a bad first minute; a windowed
        one can, which is what lets the dialer return to aggressive pacing once
        conditions improve.

        The window is measured in TIME rather than in answers, which is the
        whole point.  Windowing on a count means the window's width in seconds
        depends on how fast answers arrive: about 50s in the 50%/90s scenario
        but about 200s in the 70%/180s one.  A brake whose reaction time scales
        with how busy the floor is reacts slowest exactly where it matters
        most -- and 200s is longer than the entire incident it is meant to
        catch.
        """
        self._trim(now, seconds)
        if not self.recent_outcomes:
            self._window_rate_cache = 0.0
            return 0.0
        rate = sum(v for _, v in self.recent_outcomes) / len(self.recent_outcomes)
        self._window_rate_cache = rate
        return rate

    def window_sample_size(self, now: float, seconds: float = 60.0) -> int:
        self._trim(now, seconds)
        return len(self.recent_outcomes)

    def _trim(self, now: float, seconds: float) -> None:
        cutoff = now - seconds
        d = self.recent_outcomes
        while d and d[0][0] < cutoff:
            d.popleft()

    def record_answer(self, abandoned: bool, now: float = 0.0) -> None:
        self.recent_outcomes.append((now, 1 if abandoned else 0))

    def as_dict(self) -> dict:
        return {
            "calls_initiated": self.calls_initiated,
            "calls_answered": self.calls_answered,
            "calls_connected": self.calls_connected,
            "calls_completed": self.calls_completed,
            "calls_failed": self.calls_failed,
            "place_failures": self.place_failures,
            "abandoned": self.abandoned,
            "abandon_rate": round(self.abandon_rate, 4),
            "window_abandon_rate": round(self._window_rate_cache, 4),
            "observed_answer_rate": round(self.answer_rate, 4),
            "duplicate_events": self.duplicate_events,
            "rejected_events": dict(self.rejected_events),
            "leases_reclaimed": self.leases_reclaimed,
            "talk_utilization": round(self.talk_utilization, 4),
            "occupancy": round(self.occupancy, 4),
            "idle_seconds": round(self.idle_seconds, 1),
            "safety_decisions": dict(self.safety),
        }


class DialerWorker:
    def __init__(
        self,
        store,
        gateway: ProviderGateway,
        allocator: CallAllocator,
        pacing: PacingEngine,
        safety: SafetyController,
        clock,
        scheduler: Scheduler,
        campaign_id: str,
        mode: PacingMode = PacingMode.PREDICTIVE,
        wrap_seconds: float = 12.0,
        abandon_window_seconds: float = 60.0,
        stuck_call_timeout: float = 90.0,
        lease_seconds: float = 30.0,
        patience_seconds: float = 5.0,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.allocator = allocator
        self.pacing = pacing
        self.safety = safety
        self.clock = clock
        self.scheduler = scheduler
        self.campaign_id = campaign_id
        self.mode = mode
        self.wrap_seconds = wrap_seconds
        self.stuck_call_timeout = stuck_call_timeout
        self.lease_seconds = lease_seconds
        # How long a borrower who has picked up waits for an agent before we
        # call it an abandon.  Real dialers use a short grace period; it is the
        # cheapest safety margin there is, because it costs nothing when the
        # pacing is right.
        self.patience_seconds = patience_seconds
        # Width of the sliding window used to judge recent abandonment.
        # Measured in seconds, not answers -- see Metrics.window_abandon_rate.
        self.abandon_window_seconds = abandon_window_seconds
        # FIFO of (call_id, answered_at) for borrowers on hold.
        self.holding: List[tuple] = []

        self.metrics = Metrics()
        self.pacer = AdaptivePacer(pacing, clock=clock)
        self.answer_estimate = BetaBinomialEstimate()
        # Replaces the old plain `avg_talk` float.  The prior is the same 90s of
        # talk plus wrap, but the estimator now also uses agent turnover, so it
        # stops claiming 102s of service when calls really take 192s.
        self.service = ServiceEstimate(prior_seconds=90.0 + wrap_seconds)
        self.avg_ring = 8.0
        self._last_tick: Optional[float] = None
        self._placed_this_second: Dict[int, int] = {}
        self.decisions: List[SafetyDecision] = []
        self.pending_callbacks: List[str] = []

    @property
    def avg_talk(self) -> float:
        """Point estimate of talk time from completed calls.

        Kept for the per-agent `expected_free_at` hint.  Capacity planning must
        use `self.service.plan`, which is pessimistic, not this.
        """
        return self.service.completed_mean

    def _release_agent(self, agent_id: str, now: float, **kwargs) -> None:
        """Return an agent to AVAILABLE, recording a service completion.

        Only agents that were genuinely serving (CONNECTED / WRAP_UP) count as
        turnover.  An agent released because the call failed before it was
        answered was never tied up for a service duration, and counting it would
        inflate the observed free rate.
        """
        agent = self.store.get_agent(agent_id)
        if agent is not None and agent.state in (AgentState.CONNECTED, AgentState.WRAP_UP):
            self.service.observe_free()
        self.store.set_agent_state(
            agent_id, AgentState.AVAILABLE, now,
            worker_id=self.allocator.worker_id, **kwargs,
        )

    # ------------------------------------------------------------------ tick
    def tick(self) -> SafetyDecision:
        now = self.clock.now()
        if self._last_tick is None:
            self._last_tick = now
        dt = max(0.0, now - self._last_tick)
        self._last_tick = now

        # Borrowers already on the line get first claim on any free agent.
        self._service_holding(now)

        counts = self.store.count_agents_by_state(self.campaign_id)
        available = counts.get(AgentState.AVAILABLE, 0)
        connected_agents = counts.get(AgentState.CONNECTED, 0)
        ringing = len(
            self.store.calls_in_states(
                self.campaign_id, [CallState.INITIATED, CallState.RINGING]
            )
        )
        horizon = max(self.avg_ring, 1.0)

        # ---- estimating how many agents become free -------------------------
        # We deliberately do NOT use each agent's individually predicted finish
        # time.  Talk time varies, so any agent whose predicted finish has
        # already passed but who is still talking would be counted as "free
        # soon" -- and on a busy floor that is roughly half the agents, which
        # makes the dialer chronically over-dial.
        #
        # Instead we use the aggregate rate: if `busy` agents are each serving
        # for `mean_service` seconds, then busy/mean_service agents free per
        # second, so busy * (window / mean_service) free inside the window.
        # The per-agent error averages out instead of accumulating.
        busy_agents = (
            counts.get(AgentState.CONNECTED, 0) + counts.get(AgentState.WRAP_UP, 0)
        )
        # Accumulate agent-seconds of service before reading the estimate: this
        # is the denominator of the turnover estimator.
        self.service.observe_busy(busy_agents, dt)
        mean_service = max(self.service.plan, 1.0)
        supply_window = horizon + self.patience_seconds
        q_free = max(0.0, min(1.0, supply_window / mean_service))
        expected_frees = busy_agents * q_free
        at_risk = busy_agents

        # ---- agent-time accounting --------------------------------------
        self.metrics.talk_seconds += connected_agents * dt
        self.metrics.wrap_seconds += counts.get(AgentState.WRAP_UP, 0) * dt
        self.metrics.setup_seconds += (
            counts.get(AgentState.RESERVED, 0) + counts.get(AgentState.DIALING, 0)
        ) * dt
        self.metrics.idle_seconds += available * dt

        # decay the answer-rate estimate so it tracks drift
        if dt > 0:
            self.answer_estimate.decay(math.exp(-dt / 240.0))

        on_shift = sum(
            counts.get(s_, 0)
            for s_ in (AgentState.AVAILABLE, AgentState.RESERVED, AgentState.DIALING,
                       AgentState.CONNECTED, AgentState.WRAP_UP)
        )
        inputs = PacingInputs(
            available_agents=available,
            on_shift_agents=on_shift,
            agents_free_within_horizon=expected_frees,
            connected_calls=connected_agents,
            ringing_calls=ringing,
            p_answer=self.answer_estimate.p,
            confidence=self.answer_estimate.confidence,
            avg_talk_seconds=self.service.plan,
            avg_wrap_seconds=self.wrap_seconds,
            avg_ring_seconds=self.avg_ring,
            horizon_seconds=horizon,
            queued_borrowers=self.store.queued_borrowers(self.campaign_id),
            service_confidence=self.service.confidence,
            patience_seconds=self.patience_seconds,
            busy_agents=busy_agents,
            waiting_calls=len(self.holding),
            mean_service_seconds=mean_service,
        )
        # ---- closed-loop correction -------------------------------------
        self.pacer.observe(
            now, self.metrics.abandoned, self.metrics.calls_answered,
            self.metrics.idle_seconds, self.metrics.on_shift_seconds,
            holding=len(self.holding),
        )
        intent = self.pacer.propose(inputs, self.mode)

        second_bucket = int(now)
        placed = self._placed_this_second.get(second_bucket, 0)

        ctx = SafetyContext(
            campaign_id=self.campaign_id,
            now=now,
            available_agents=available,
            agents_free_within_horizon=expected_frees,
            ringing_calls=ringing,
            connected_calls=connected_agents,
            p_answer=self.answer_estimate.p,
            confidence=self.answer_estimate.confidence,
            queued_borrowers=inputs.queued_borrowers,
            provider_healthy=self.gateway.is_healthy(),
            provider_name=self.gateway.healthy_provider_name() or "none",
            observed_abandon_rate=self.metrics.window_abandon_rate(
                now, self.abandon_window_seconds),
            abandon_sample_size=self.metrics.window_sample_size(
                now, self.abandon_window_seconds),
            waiting_calls=len(self.holding),
            on_shift_agents=on_shift,
            avg_ring_seconds=self.avg_ring,
            mean_service_seconds=mean_service,
            service_confidence=self.service.confidence,
            service_observations=self.service.n_freed,
            calls_placed_last_second=placed,
            at_risk_agents=at_risk,
            q_free=q_free,
        )
        decision = self.safety.evaluate(intent, ctx)
        self.decisions.append(decision)

        reserve_agent = decision.mode == PacingMode.PROGRESSIVE
        if decision.allowed_calls > 0:
            batch: DialBatchResult = self.allocator.dial(
                decision, self.campaign_id, reserve_agent=reserve_agent
            )
            self.metrics.calls_initiated += batch.placed
            self.metrics.place_failures += sum(
                1 for a in batch.attempts if not a.ok and a.stage == "provider"
            )
            self._placed_this_second[second_bucket] = placed + batch.placed

        return decision

    # ---------------------------------------------------------------- events
    def on_event(self, event: ProviderEvent) -> None:
        now = self.clock.now()
        res = self.store.apply_event(event, now)

        if res.status == RejectReason.DUPLICATE:
            self.metrics.duplicate_events += 1
            return
        if not res.applied:
            key = res.status.value
            self.metrics.rejected_events[key] = self.metrics.rejected_events.get(key, 0) + 1
            return
        if res.call is None:
            return

        call = res.call
        if res.to_state == CallState.ANSWERED:
            self.metrics.calls_answered += 1
            self.answer_estimate.update(True)
            self._bind_agent_on_answer(call, now)
        elif res.to_state == CallState.CONNECTED:
            self.metrics.calls_connected += 1
            self._agent_connected(call, now)
        elif res.to_state == CallState.COMPLETED:
            self.metrics.calls_completed += 1
            self._finish_call(call, now)
        elif res.to_state in (CallState.FAILED, CallState.CANCELLED):
            self.metrics.calls_failed += 1
            # Only a human outcome tells us something about borrower behaviour.
            # A telephony failure is not evidence about the answer rate.
            if event.type in ("NO_ANSWER", "BUSY"):
                self.answer_estimate.update(False)
            self._finish_call(call, now)

    def _bind_agent_on_answer(self, call: Call, now: float) -> None:
        """A human picked up.  Get them an agent, or put them on hold.

        We do NOT hang up immediately when no agent is free.  The borrower waits
        up to `patience_seconds`; if an agent frees in that window they are
        connected and nothing happened.  Only if the wait expires do we record an
        abandon -- and even then the borrower is handed to the IVR and a
        callback is queued, never silently dropped.
        """
        if call.agent_id:
            # Progressive: the agent was reserved before we dialed.
            self.store.set_agent_state(
                call.agent_id, AgentState.DIALING, now,
                expected_states=[AgentState.RESERVED, AgentState.DIALING],
                worker_id=self.allocator.worker_id,
                lease_seconds=self.lease_seconds,
                call_id=call.call_id,
            )
            self.metrics.record_answer(False, now)
            return

        if self._bind_free_agent(call, now):
            self.metrics.record_answer(False, now)
            return

        # No agent right now -> hold, in FIFO order.
        self.holding.append((call.call_id, now))

    def _bind_free_agent(self, call: Call, now: float) -> bool:
        """Try to attach an available agent to this call."""
        claimed = self.store.claim_agent(
            self.campaign_id, self.allocator.worker_id, now, self.lease_seconds
        )
        if not claimed.ok:
            return False
        agent = claimed.obj
        call.agent_id = agent.agent_id
        if call.state == CallState.CONNECTED:
            self.store.set_agent_state(
                agent.agent_id, AgentState.CONNECTED, now,
                expected_states=[AgentState.RESERVED],
                worker_id=self.allocator.worker_id,
                lease_seconds=self.lease_seconds,
                call_id=call.call_id,
                expected_free_at=now + self.avg_talk + self.wrap_seconds,
            )
        else:
            self.store.set_agent_state(
                agent.agent_id, AgentState.DIALING, now,
                expected_states=[AgentState.RESERVED],
                worker_id=self.allocator.worker_id,
                lease_seconds=self.lease_seconds,
                call_id=call.call_id,
            )
        return True

    def _service_holding(self, now: float) -> None:
        """Give free agents to waiting borrowers, FIFO; time out the rest."""
        if not self.holding:
            return
        still_holding: List[tuple] = []
        for call_id, queued_at in self.holding:
            call = self.store.get_call(call_id)
            if call is None or call.is_terminal():
                continue
            if self._bind_free_agent(call, now):
                self.metrics.record_answer(False, now)
                continue
            waited = now - queued_at
            if waited >= self.patience_seconds:
                self._abandon(call, now)
                continue
            still_holding.append((call_id, queued_at))
        self.holding = still_holding

    def _abandon(self, call: Call, now: float) -> None:
        """A borrower waited too long and no agent came.

        This is the failure the Safety Controller exists to make rare.  Note we
        never simply drop the call: it is routed to the IVR and a callback is
        queued, so the borrower is always spoken to by something.
        """
        self.metrics.abandoned += 1
        self.metrics.record_answer(True, now)
        call.abandoned = True
        self.store.apply_event(
            ProviderEvent(
                event_id=new_id("evt"),
                call_id=call.call_id,
                seq=call.last_seq + 1,
                type="COMPLETED",
                at=now,
            ),
            now,
        )
        updated = self.store.get_call(call.call_id)
        if updated is not None:
            updated.outcome = "ABANDONED_ROUTED_IVR"
        self.pending_callbacks.append(call.borrower_id)

    def _agent_connected(self, call: Call, now: float) -> None:
        if not call.agent_id:
            return
        expected_end = now + self.avg_talk + self.wrap_seconds
        self.store.set_agent_state(
            call.agent_id, AgentState.CONNECTED, now,
            expected_states=[AgentState.DIALING, AgentState.CONNECTED],
            worker_id=self.allocator.worker_id,
            lease_seconds=self.lease_seconds,
            call_id=call.call_id,
            expected_free_at=expected_end,
        )

    def _finish_call(self, call: Call, now: float) -> None:
        # Talk time runs from CONNECTED to COMPLETED.  (ANSWERED -> CONNECTED is
        # the sub-second bridge, not the conversation -- measuring that instead
        # makes mean service time look ~100x too small and the dialer then
        # tries to hold a wildly oversized pipeline.)
        if call.ended_at and call.connected_at:
            talk = call.ended_at - call.connected_at
            if 1.0 <= talk <= 3600.0:
                self.service.observe_completed(talk + self.wrap_seconds)
        # NOTE: this used to correct survivorship bias by feeding still-running
        # calls in as `partial * 1.6` at a tiny EWMA weight.  That is arbitrary
        # and far too slow -- it left the estimate at 102s for the first 180s of
        # the 70%/180s scenario, when the truth was 192s.  The turnover
        # estimator in `ServiceEstimate` does this job properly, by using the
        # rate at which agents are actually observed coming back.
        if call.answered_at and call.initiated_at:
            ring = call.answered_at - call.initiated_at
            self.avg_ring = ewma(self.avg_ring, max(ring, 1.0), 0.2)

        if not call.agent_id:
            return

        if call.outcome in ("ABANDONED_ROUTED_IVR",) or call.state == CallState.COMPLETED:
            self.store.set_agent_state(
                call.agent_id, AgentState.WRAP_UP, now,
                expected_states=[AgentState.CONNECTED, AgentState.DIALING],
                worker_id=self.allocator.worker_id,
                lease_seconds=self.lease_seconds,
            )
            self.scheduler.schedule(
                self.wrap_seconds,
                lambda aid=call.agent_id: self._end_wrap(aid),
            )
        else:
            self._release_agent(call.agent_id, now)

    def _end_wrap(self, agent_id: str) -> None:
        now = self.clock.now()
        self._release_agent(agent_id, now, expected_states=[AgentState.WRAP_UP])
        # An agent just freed up -- hand them straight to whoever is holding
        # rather than waiting for the next tick.
        self._service_holding(now)

    # -------------------------------------------------------------- recovery
    def recover(self) -> dict:
        """Called on startup and on a timer.

        Recovery is lease-expiry driven.  We never try to *detect* a dead
        worker; we simply stop trusting a reservation whose owner stopped
        renewing it.  That survives SIGKILL, a lost VM, and a network partition
        equally well.
        """
        now = self.clock.now()
        report = self.store.expire_leases(now)
        self.metrics.leases_reclaimed += len(report.agents_released)
        self._reap_stuck_calls(now)
        return report.as_dict()

    def _reap_stuck_calls(self, now: float) -> int:
        """A call the provider forgot to tell us about.

        We cannot hang up a call we cannot see, so after a generous timeout we
        mark it FAILED and free the agent.  If a late COMPLETED arrives
        afterwards the state machine drops it as TERMINAL, which is safe.
        """
        reaped = 0
        for call in self.store.calls_in_states(
            self.campaign_id, [CallState.INITIATED, CallState.RINGING, CallState.ANSWERED]
        ):
            if now - call.updated_at <= self.stuck_call_timeout:
                continue
            self.store.apply_event(
                ProviderEvent(
                    event_id=new_id("evt"),
                    call_id=call.call_id,
                    seq=call.last_seq + 1,
                    type="FAILED",
                    at=now,
                ),
                now,
            )
            updated = self.store.get_call(call.call_id)
            if updated is not None:
                updated.outcome = "PROVIDER_SILENT"
                if call.agent_id:
                    self._release_agent(call.agent_id, now)
            reaped += 1
        return reaped

    # ----------------------------------------------------------------- misc
    def drop_agents(self, n: int) -> int:
        """Failure drill: n agents vanish.

        We take idle agents first (AVAILABLE / RESERVED / DIALING) because that
        is the benign version of the failure.  If the floor is saturated there
        will not be enough of those, and then we start losing agents who are
        mid-conversation -- which is the harsh version, and the one that really
        tests whether caller state survives.
        """
        now = self.clock.now()
        dropped = 0
        preferred = (AgentState.AVAILABLE, AgentState.RESERVED, AgentState.DIALING)
        fallback = (AgentState.CONNECTED, AgentState.WRAP_UP)

        for wanted in (preferred, fallback):
            if dropped >= n:
                break
            for agent in self.store.list_agents(self.campaign_id):
                if dropped >= n:
                    break
                if agent.state not in wanted:
                    continue
                if self.store.set_agent_state(agent.agent_id, AgentState.OFFLINE, now):
                    dropped += 1
        return dropped

    def snapshot(self) -> dict:
        counts = self.store.count_agents_by_state(self.campaign_id)
        return {
            "agents": {s.value: counts.get(s, 0) for s in AgentState},
            "estimate": self.answer_estimate.as_dict(),
            "holding": len(self.holding),
            "metrics": self.metrics.as_dict(),
            "providers": self.gateway.health_snapshot(),
            "aggressiveness": round(self.pacer.aggressiveness, 3),
        }
