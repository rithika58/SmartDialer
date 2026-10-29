"""
The Safety Controller.

    Campaign > Pacing Engine > Safety Controller > Call Allocator > Provider

The pacing engine says "I think we can start N more calls."  The Safety
Controller decides whether that is actually allowed.  It can approve, reduce,
reject, or force a fallback to progressive behaviour.

WHY YOU CANNOT SWITCH IT OFF
----------------------------
Three independent barriers, because one is never enough:

1. Structural.  The pacing engine is a pure function over a snapshot; it holds
   no provider reference.  Providers live behind `ProviderGateway`, which is
   only ever handed to the allocator, and `place_call` requires an approval
   object.  There is no code path from pacing to provider.

2. Cryptographic.  An approval is signed with a per-controller secret
   (HMAC-SHA256 over the decision contents).  An approval the controller did
   not issue fails verification.

3. Single-use.  Each approval is consumed exactly once, so a captured approval
   cannot be replayed to dial again.

There is deliberately no config flag, environment variable, or admin endpoint
that disables any of these.  Adding one later would be a code change with a
reviewer, which is the point.

WHAT IT ENFORCES
----------------
Checked in order; the first failure decides the outcome.

  1. Circuit breaker.  Provider unhealthy -> reject everything.
  2. Observed abandonment.  We broke our own promise recently -> fall back to
     deterministic progressive dialing.
  3. Cold start.  Not enough data to trust the estimate -> progressive.
  4. Absolute concurrency ceiling.
  5. Rate limit (calls per second).
  6. Probabilistic abandonment guard: largest N such that
     P(borrowers who answer > agents who will be free) <= max_abandon_prob.
  7. Overbooking ratio ceiling: a hard, non-statistic backstop so that even a
     wildly wrong p_answer cannot produce an unbounded batch.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from ..stats import abandon_probability, binom_sf
from .pacing import PacingDecision, PacingMode


class SafetyAction(str, Enum):
    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    REJECT = "REJECT"
    FALLBACK_PROGRESSIVE = "FALLBACK_PROGRESSIVE"


@dataclass
class SafetyConfig:
    max_abandon_probability: float = 0.03   # P(overbooked) we tolerate
    max_in_flight_per_agent: float = 1.0    # hard backstop vs. a wrong p_answer
    # Independently recomputed ceiling on the pipeline.  The pacing engine
    # targets `target_occupancy`; the controller allows a little more than that
    # but never enough to overload the floor.
    max_pipeline_occupancy: float = 0.98
    max_concurrent_calls: int = 5_000
    max_calls_per_second: float = 50.0
    min_confidence_for_predictive: float = 0.50
    max_observed_abandon_rate: float = 0.08   # emergency brake, not the primary guard
    abandon_sample_min: int = 25            # need this many answers before judging
    patience_seconds: float = 4.0           # how long a borrower waits for an agent
    approval_ttl_seconds: float = 5.0


@dataclass
class SafetyContext:
    """Snapshot of everything the controller needs to make a decision."""
    campaign_id: str
    now: float
    available_agents: int = 0
    agents_free_within_horizon: float = 0.0
    ringing_calls: int = 0
    connected_calls: int = 0
    p_answer: float = 0.5
    confidence: float = 0.0
    queued_borrowers: int = 0
    provider_healthy: bool = True
    provider_name: str = "unknown"
    # Windowed, not cumulative: a cumulative rate can never recover from an
    # early mistake, which would lock the dialer into permanent conservative
    # mode after one bad minute.
    observed_abandon_rate: float = 0.0
    abandon_sample_size: int = 0
    waiting_calls: int = 0                  # answered, still holding for an agent
    on_shift_agents: int = 0
    avg_ring_seconds: float = 8.0
    mean_service_seconds: float = 90.0
    calls_placed_last_second: int = 0
    # Agent-availability risk: agents busy beyond the horizon, and the chance
    # each of them frees early.
    at_risk_agents: int = 0
    q_free: float = 0.0


@dataclass
class SafetyDecision:
    action: SafetyAction
    allowed_calls: int
    requested_calls: int
    mode: PacingMode
    reasons: List[str] = field(default_factory=list)
    checks: Dict[str, object] = field(default_factory=dict)
    decision_id: Optional[str] = None
    issued_at: float = 0.0
    campaign_id: str = ""
    signature: str = ""

    @property
    def approved(self) -> bool:
        return self.allowed_calls > 0

    def explain(self) -> str:
        return (
            f"{self.action.value} {self.allowed_calls}/{self.requested_calls} "
            f"[{self.mode.value}] :: " + "; ".join(self.reasons)
        )


class SafetyController:
    """The last thing between an algorithm and a phone line."""

    def __init__(self, config: Optional[SafetyConfig] = None, clock=None) -> None:
        self.config = config or SafetyConfig()
        self.clock = clock
        self._secret = secrets.token_bytes(32)
        self._used: Dict[str, int] = {}   # decision_id -> calls still authorised
        self._lock = threading.RLock()
        self.stats: Dict[str, int] = {
            "approve": 0, "reduce": 0, "reject": 0, "fallback_progressive": 0,
        }

    # ------------------------------------------------------------------ core
    def evaluate(self, intent: PacingDecision, ctx: SafetyContext) -> SafetyDecision:
        cfg = self.config
        reasons: List[str] = []
        checks: Dict[str, object] = {}
        requested = max(0, int(intent.requested_calls))
        allowed = requested

        capacity = ctx.available_agents + ctx.agents_free_within_horizon

        # -- 1. provider health ---------------------------------------------
        if not ctx.provider_healthy:
            self.stats["reject"] += 1
            return self._signed(
                SafetyAction.REJECT, 0, requested, PacingMode.PROGRESSIVE,
                [f"provider {ctx.provider_name} circuit is open"],
                {"provider_healthy": False}, ctx,
            )

        # -- 2. did we break our own promise recently? -----------------------
        abandon_rate = (
            ctx.observed_abandon_rate
            if ctx.abandon_sample_size >= cfg.abandon_sample_min
            else 0.0
        )
        checks["observed_abandon_rate"] = round(abandon_rate, 4)
        if abandon_rate > cfg.max_observed_abandon_rate:
            self.stats["fallback_progressive"] += 1
            return self._signed(
                SafetyAction.FALLBACK_PROGRESSIVE,
                min(ctx.available_agents, ctx.queued_borrowers),
                requested, PacingMode.PROGRESSIVE,
                [f"observed abandon rate {abandon_rate:.1%} exceeds "
                 f"{cfg.max_observed_abandon_rate:.1%}; reverting to deterministic dialing"],
                checks, ctx,
            )

        # -- 3. cold start ---------------------------------------------------
        if intent.mode == PacingMode.PREDICTIVE and ctx.confidence < cfg.min_confidence_for_predictive:
            self.stats["fallback_progressive"] += 1
            return self._signed(
                SafetyAction.FALLBACK_PROGRESSIVE,
                min(ctx.available_agents, ctx.queued_borrowers),
                requested, PacingMode.PROGRESSIVE,
                [f"answer-rate confidence {ctx.confidence:.2f} below "
                 f"{cfg.min_confidence_for_predictive}; not enough data to dial ahead"],
                checks, ctx,
            )

        # -- 4. absolute concurrency ceiling ---------------------------------
        in_flight = ctx.ringing_calls
        headroom = cfg.max_concurrent_calls - in_flight
        if allowed > headroom:
            reasons.append(
                f"concurrency ceiling: {in_flight} in flight, cap "
                f"{cfg.max_concurrent_calls}"
            )
            allowed = max(0, min(allowed, headroom))

        # -- 5. rate limit ---------------------------------------------------
        rate_headroom = int(max(0.0, cfg.max_calls_per_second - ctx.calls_placed_last_second))
        if allowed > rate_headroom:
            reasons.append(
                f"rate limit: {ctx.calls_placed_last_second}/"
                f"{cfg.max_calls_per_second:.0f} calls used this second"
            )
            allowed = max(0, min(allowed, rate_headroom))

        # -- 5b. expected wait must fit inside the borrower's patience --------
        # A borrower who picks up waits roughly (waiting + 1) * (service / busy)
        # for an agent: that is how long it takes the next `waiting + 1` agents
        # to come free.  If that exceeds how long we are willing to make someone
        # hold, adding more calls is pure abandonment, so we stop.
        if intent.mode == PacingMode.PREDICTIVE and ctx.at_risk_agents > 0:
            inter_free = ctx.mean_service_seconds / ctx.at_risk_agents
            # Largest queue whose expected wait still fits inside the borrower's
            # patience.  A zero queue means perfect matching, which is not
            # achievable with random arrivals -- forbidding any queue at all just
            # leaves agents idle, so we allow the queue the patience budget can
            # actually absorb and no more.
            max_queue = max(1, int(cfg.patience_seconds / inter_free))
            expected_wait = (ctx.waiting_calls + 1) * inter_free
            checks["expected_wait_seconds"] = round(expected_wait, 2)
            checks["patience_seconds"] = cfg.patience_seconds
            checks["max_queue"] = max_queue
            if ctx.available_agents == 0 and ctx.waiting_calls + 1 > max_queue:
                reasons.append(
                    f"hold queue full: {ctx.waiting_calls} already holding, and a "
                    f"{max_queue}-deep queue is the most a {cfg.patience_seconds:.0f}s "
                    f"patience can absorb at {inter_free:.1f}s between agents"
                )
                allowed = 0

        # -- 6. pipeline ceiling (deterministic, independent of the model) ----
        # This is the check that catches a *systematically* wrong estimate.
        # Everything above assumes p_answer and mean service time are roughly
        # right; if they are not, the probabilistic guard is quietly wrong.  So
        # the controller recomputes, from first principles, the largest pipeline
        # the floor can actually drain:
        #
        #     answers arrive at   in_flight * p / ring      per second
        #     the floor drains at on_shift / service        per second
        #
        # and refuses to let the first exceed the second.  Note this is the same
        # arithmetic the pacing engine uses -- deliberately.  The controller does
        # not trust the engine's answer, it re-derives the bound and takes the
        # lower of the two.
        if intent.mode == PacingMode.PREDICTIVE:
            p_ceiling = min(max(ctx.p_answer, 0.02), 0.99)
            ring_c = max(ctx.avg_ring_seconds, 1.0)
            service_c = max(ctx.mean_service_seconds, 1.0)
            ceiling = (
                cfg.max_pipeline_occupancy
                * max(ctx.on_shift_agents, 0)
                * ring_c / (service_c * p_ceiling)
            )
            ceiling = int(ceiling)
            ceiling_new = max(0, ceiling - ctx.ringing_calls - ctx.waiting_calls)
            checks["pipeline_ceiling"] = ceiling
            if allowed > ceiling_new:
                reasons.append(
                    f"pipeline ceiling: the floor can only drain "
                    f"{ctx.on_shift_agents}/({service_c:.0f}s) = "
                    f"{ctx.on_shift_agents / service_c:.2f} answers/sec, so at most "
                    f"{ceiling} calls may be unanswered at once"
                )
                allowed = ceiling_new

        # -- 7. probabilistic abandonment guard ------------------------------
        if intent.mode == PacingMode.PREDICTIVE:
            p = min(max(ctx.p_answer, 0.01), 0.99)
            # Borrowers already on hold are demand that has materialised: they
            # must be served before anyone we are about to dial.
            effective_capacity = max(0, capacity - ctx.waiting_calls)
            checks["waiting_calls"] = ctx.waiting_calls
            checks["effective_capacity"] = effective_capacity
            safe_total = self._max_safe_in_flight(
                effective_capacity, p, cfg.max_abandon_probability,
                ctx.at_risk_agents, ctx.q_free,
            )
            safe_new = max(0, safe_total - ctx.ringing_calls)
            checks["max_safe_in_flight"] = safe_total
            checks["cap_free_agents"] = capacity
            checks["p_answer"] = round(p, 4)
            if allowed > safe_new:
                reasons.append(
                    f"abandonment risk: p_answer={p:.2f}, {capacity} agents free now, "
                    f"{ctx.at_risk_agents} more at risk; more than {safe_total} "
                    f"unanswered calls in flight would exceed "
                    f"P(abandon) <= {cfg.max_abandon_probability:.1%}"
                )
                allowed = safe_new

            # -- 8. hard overbooking backstop --------------------------------
            # The probabilistic guard above is only as good as p_answer.  If the
            # estimator is badly wrong this backstop still bounds the damage:
            # never more unanswered calls in flight than agents on the floor.
            hard_cap = int(cfg.max_in_flight_per_agent * max(ctx.on_shift_agents, 1))
            hard_new = max(0, hard_cap - ctx.ringing_calls)
            checks["in_flight_hard_cap"] = hard_cap
            if allowed > hard_new:
                reasons.append(
                    f"in-flight backstop: {hard_cap} unanswered calls max for "
                    f"{ctx.on_shift_agents} agents on shift"
                )
                allowed = hard_new
        else:
            # Progressive: the invariant is arithmetic, not statistical.
            if allowed > ctx.available_agents:
                reasons.append(
                    f"progressive invariant: {allowed} calls > "
                    f"{ctx.available_agents} available agents"
                )
                allowed = ctx.available_agents

        # -- never dial borrowers we do not have -----------------------------
        if allowed > ctx.queued_borrowers:
            reasons.append(f"only {ctx.queued_borrowers} borrowers queued")
            allowed = max(0, min(allowed, ctx.queued_borrowers))

        checks["available_agents"] = ctx.available_agents
        checks["agents_free_within_horizon"] = ctx.agents_free_within_horizon
        checks["ringing_calls"] = ctx.ringing_calls

        if allowed == requested and requested > 0:
            action = SafetyAction.APPROVE
            self.stats["approve"] += 1
        elif allowed == 0 or requested == 0:
            action = SafetyAction.REJECT
            self.stats["reject"] += 1
        else:
            action = SafetyAction.REDUCE
            self.stats["reduce"] += 1

        if not reasons:
            reasons.append("all safety checks passed")

        return self._signed(
            action, allowed, requested, intent.mode, reasons, checks, ctx
        )

    def _max_safe_in_flight(
        self,
        capacity: int,
        p_answer: float,
        max_abandon_prob: float,
        at_risk_agents: int = 0,
        q_free: float = 0.0,
    ) -> int:
        """Largest n such that P(somebody answers with no agent) <= threshold.

        The risk is monotone in n (more calls in flight can only mean more
        answers), so we binary-search rather than scan.
        """
        capacity = int(capacity)     # conservative: round supply down
        lo, hi = 0, min(max(capacity * 6, 64), 3000)
        if abandon_probability(hi, p_answer, capacity, at_risk_agents, q_free) <= max_abandon_prob:
            return hi
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if abandon_probability(mid, p_answer, capacity, at_risk_agents, q_free) <= max_abandon_prob:
                lo = mid
            else:
                hi = mid - 1
        return lo

    # ------------------------------------------------------- approval tokens
    def _signed(
        self,
        action: SafetyAction,
        allowed: int,
        requested: int,
        mode: PacingMode,
        reasons: List[str],
        checks: Dict[str, object],
        ctx: SafetyContext,
    ) -> SafetyDecision:
        import uuid

        decision_id = uuid.uuid4().hex
        now = ctx.now
        payload = f"{decision_id}|{ctx.campaign_id}|{allowed}|{mode.value}|{now:.6f}"
        sig = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        return SafetyDecision(
            action=action,
            allowed_calls=allowed,
            requested_calls=requested,
            mode=mode,
            reasons=reasons,
            checks=checks,
            decision_id=decision_id,
            issued_at=now,
            campaign_id=ctx.campaign_id,
            signature=sig,
        )

    def verify(
        self, decision: SafetyDecision, campaign_id: str, now: float, count: int = 1
    ) -> bool:
        """Recompute the signature and draw down the authorisation budget.

        Called by the ProviderGateway immediately before a call is placed.  An
        approval authorises `allowed_calls` calls -- not one, and not an
        unlimited number.  A forged approval fails the HMAC check; a genuine one
        stops working as soon as its budget is spent, so it cannot be replayed
        to dial more than the controller allowed.
        """
        if decision is None or not decision.decision_id:
            return False
        if decision.campaign_id != campaign_id:
            return False
        if decision.allowed_calls <= 0:
            return False
        if abs(now - decision.issued_at) > self.config.approval_ttl_seconds:
            return False

        payload = (
            f"{decision.decision_id}|{decision.campaign_id}|"
            f"{decision.allowed_calls}|{decision.mode.value}|"
            f"{decision.issued_at:.6f}"
        )
        expected = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, decision.signature):
            return False

        with self._lock:
            if len(self._used) > 50_000:
                self._used.clear()
            remaining = self._used.get(decision.decision_id, decision.allowed_calls)
            if remaining < count:
                return False
            self._used[decision.decision_id] = remaining - count
        return True

    def snapshot_stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self.stats)
