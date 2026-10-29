"""
Predictive pacing engine.

Design constraint we imposed on ourselves: this module is a PURE FUNCTION.

`PacingEngine.propose()` receives a plain dataclass snapshot of the world and
returns how many calls it *would like* to start.  It has no reference to the
store, no reference to a provider, and no way to perform I/O of any kind.

That is not stylistic tidiness -- it is the first half of the safety argument.
A pacing algorithm that physically cannot reach the telecom provider cannot
place an unsafe call, no matter how wrong its arithmetic is.  The second half
is the Safety Controller.

The arithmetic
--------------
We want the number of borrowers who pick up during the next window to be about
equal to the number of agents who will be free during that window.

    capacity  = agents free now + agents expected to free within the horizon
    committed = expected answers from calls already ringing
                = ringing_calls * p_answer
    headroom  = capacity - committed                  (in "answers")
    calls     = headroom / p_answer                   (in "calls to place")

The horizon is the time it takes a call we place right now to be answered
(roughly the ring time), because that is how long we have to find an agent.

p_answer comes from a Beta-binomial estimate with shrinkage towards a prior, so
three lucky calls in a row cannot convince the dialer that the answer rate is
100%.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class PacingMode(str, Enum):
    PROGRESSIVE = "PROGRESSIVE"
    PREDICTIVE = "PREDICTIVE"


@dataclass
class PacingInputs:
    """Everything the engine is allowed to know.  Nothing else."""
    # agents
    available_agents: int = 0
    on_shift_agents: int = 0
    agents_free_within_horizon: float = 0.0   # expected value, not a count
    connected_calls: int = 0
    # calls
    ringing_calls: int = 0              # dialed, not yet answered
    # estimates
    p_answer: float = 0.5
    confidence: float = 0.0
    avg_talk_seconds: float = 90.0
    avg_wrap_seconds: float = 15.0
    avg_ring_seconds: float = 8.0
    # knobs
    horizon_seconds: Optional[float] = None
    aggressiveness: float = 1.0
    target_occupancy: float = 0.90
    queued_borrowers: int = 0
    max_batch: int = 200

    def __post_init__(self) -> None:
        if self.horizon_seconds is None:
            # How long before a call placed now is answered.  That is the
            # deadline for having an agent ready.
            self.horizon_seconds = max(self.avg_ring_seconds, 1.0)


@dataclass
class PacingDecision:
    requested_calls: int
    mode: PacingMode
    rationale: Dict[str, object] = field(default_factory=dict)
    confidence: float = 0.0

    def explain(self) -> str:
        r = self.rationale
        return (
            f"{self.mode.value}: request {self.requested_calls} calls. "
            f"capacity={r.get('capacity')} agents "
            f"(free now={r.get('available_agents')}, "
            f"free within {r.get('horizon_seconds')}s={r.get('agents_free_within_horizon')}); "
            f"already ringing={r.get('ringing_calls')} "
            f"(expected answers={r.get('committed_expected_answers')}); "
            f"p_answer={r.get('p_answer')}; "
            f"headroom={r.get('headroom_answers')} answers "
            f"-> {r.get('raw_calls')} calls "
            f"x aggressiveness {r.get('aggressiveness')} "
            f"-> floor({r.get('scaled_calls')})"
        )


class PacingEngine:
    """Pure.  No I/O.  Cannot place a call by construction."""

    def __init__(self, aggressiveness: float = 1.0) -> None:
        self.aggressiveness = aggressiveness

    # -- Progressive: one available agent -> one outbound call. --------------
    def propose_progressive(self, inputs: PacingInputs) -> PacingDecision:
        wanted = min(inputs.available_agents, inputs.queued_borrowers, inputs.max_batch)
        return PacingDecision(
            requested_calls=max(0, int(wanted)),
            mode=PacingMode.PROGRESSIVE,
            confidence=1.0,
            rationale={
                "rule": "never more agent-bound calls than available agents",
                "available_agents": inputs.available_agents,
                "queued_borrowers": inputs.queued_borrowers,
            },
        )

    # -- Predictive: dial ahead of agent availability. -----------------------
    def propose_predictive(self, inputs: PacingInputs) -> PacingDecision:
        """Flow-balance pacing.

        The first version of this engine asked "how many calls would fill the
        agents I have?" and tried to place all of them at once.  That is wrong,
        and the failure is instructive: it fills the entire pipeline in a single
        tick, so every one of those calls is answered at roughly the same
        moment -- which is precisely the burst that produces abandoned calls.

        The right question is what LEVEL of unanswered calls to keep in flight:

            agents complete a call every  `service` seconds
            => the floor needs  N / service  answers per second
            each dial yields an answer after `ring` seconds, with probability p
            => to sustain A answers/sec we must dial A/p per second
            => unanswered calls in flight = (A / p) * ring

            target_in_flight = N * ring / (service * p)

        We then request only the difference between that target and what is
        already ringing.  This is self-limiting: as the pipeline fills, the
        request naturally goes to zero.
        """
        p = _clamp(inputs.p_answer, 0.02, 0.99)
        service = max(inputs.avg_talk_seconds + inputs.avg_wrap_seconds, 1.0)
        ring = max(inputs.avg_ring_seconds, 1.0)
        n = max(inputs.on_shift_agents, 0)
        occupancy = _clamp(inputs.target_occupancy, 0.30, 0.99)

        # The raw flow balance targets rho = 1.0, i.e. every agent busy from the
        # moment one call ends to the moment the next begins.  That is not an
        # operating point, it is a queue that grows without bound: at rho = 1 the
        # expected wait for an agent diverges and every answered call is
        # eventually abandoned.
        #
        # So we deliberately dial for something less than full occupancy.  The
        # leftover is the slack that absorbs arrival lumpiness, and it is what
        # converts a 100%-utilisation fantasy into a ~3% abandon reality.
        target_in_flight = occupancy * n * ring / (service * p)
        scaled = target_in_flight * self.aggressiveness * inputs.aggressiveness
        wanted = int(math.ceil(scaled)) - inputs.ringing_calls
        wanted = min(wanted, inputs.queued_borrowers, inputs.max_batch)
        wanted = max(0, wanted)

        return PacingDecision(
            requested_calls=wanted,
            mode=PacingMode.PREDICTIVE,
            confidence=inputs.confidence,
            rationale={
                "rule": "hold unanswered calls in flight at occupancy*N*ring/(service*p)",
                "on_shift_agents": n,
                "available_agents": inputs.available_agents,
                "ringing_calls": inputs.ringing_calls,
                "connected_calls": inputs.connected_calls,
                "p_answer": round(p, 4),
                "avg_ring_seconds": round(ring, 2),
                "mean_service_seconds": round(service, 2),
                "answers_needed_per_sec": round(n / service, 4),
                "target_occupancy": round(occupancy, 3),
                "target_in_flight": round(target_in_flight, 3),
                "aggressiveness": round(self.aggressiveness * inputs.aggressiveness, 3),
                "scaled_target": round(scaled, 3),
                "queued_borrowers": inputs.queued_borrowers,
                "confidence": round(inputs.confidence, 3),
            },
        )

    def propose(self, inputs: PacingInputs, mode: PacingMode) -> PacingDecision:
        if mode == PacingMode.PROGRESSIVE:
            return self.propose_progressive(inputs)
        return self.propose_predictive(inputs)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class AdaptivePacer:
    """A closed loop around the open-loop pacing arithmetic.

    The arithmetic in `PacingEngine` is a mean-flow estimate: it keeps the
    pipeline at exactly the level needed to replace calls as they end, with no
    safety stock.  Run it literally and agents sit idle waiting for the phone to
    be picked up; run it too hot and borrowers wait with nobody to talk to.  The
    useful operating point is in between, and where it sits depends on things we
    can only measure -- how lumpy arrivals are, how long calls really last.

    So we measure it.  The loop targets an abandon RATE rather than reacting to
    individual abandoned calls, because at ~0.5 answers/second a single abandon
    inside a 20-second window is a 10% rate and reacting to that makes the
    dialer oscillate: crash, starve the floor, crawl back, overshoot, crash.

        rate too high  -> multiplicative decrease
        agents idle    -> additive increase, scaled by how much capacity we
                          are wasting, so recovery from a deep backoff is fast
        otherwise      -> hold (the floor is saturated; dialing harder would
                          only manufacture abandons)
    """

    def __init__(
        self,
        engine: "PacingEngine",
        min_aggressiveness: float = 0.50,
        max_aggressiveness: float = 3.00,
        start_aggressiveness: float = 1.00,
        target_abandon_rate: float = 0.03,
        decrease_strength: float = 0.25,
        increase_step: float = 0.06,
        window_seconds: float = 30.0,
        min_window_answers: int = 8,
        idle_trigger_fraction: float = 0.01,
        deadband_fraction: float = 0.50,
        clock=None,
    ) -> None:
        self.engine = engine
        self.min = min_aggressiveness
        self.max = max_aggressiveness
        self.target_abandon_rate = target_abandon_rate
        self.decrease_strength = decrease_strength
        self.increase_step = increase_step
        self.window_seconds = window_seconds
        self.min_window_answers = min_window_answers
        self.idle_trigger_fraction = idle_trigger_fraction
        self.deadband_fraction = deadband_fraction
        self.clock = clock
        self.aggressiveness = start_aggressiveness
        self._window_start = 0.0
        self._abandons = 0
        self._answers = 0
        self._idle = 0.0
        self._shift = 0.0
        self.history: list = []

    def observe(
        self, now: float, abandons_total: int, answers_total: int,
        idle_seconds_total: float, on_shift_seconds: float,
    ) -> None:
        if self._window_start == 0.0:
            self._reset(now, abandons_total, answers_total,
                        idle_seconds_total, on_shift_seconds)
            return
        if now - self._window_start < self.window_seconds:
            return

        d_abandon = abandons_total - self._abandons
        d_answers = answers_total - self._answers
        d_idle = idle_seconds_total - self._idle
        d_shift = on_shift_seconds - self._shift
        idle_fraction = d_idle / d_shift if d_shift > 0 else 0.0
        rate = d_abandon / d_answers if d_answers > 0 else 0.0

        old = self.aggressiveness
        comfortable = self.target_abandon_rate * self.deadband_fraction
        if d_answers >= self.min_window_answers and rate > self.target_abandon_rate:
            # Scale the correction by how far over target we are, not just by
            # whether we went over.
            excess = min(4.0, (rate - self.target_abandon_rate)
                         / max(self.target_abandon_rate, 1e-6))
            self.aggressiveness = max(
                self.min, self.aggressiveness * (1.0 - self.decrease_strength * excess)
            )
            note = "decrease"
        elif idle_fraction > self.idle_trigger_fraction and rate <= comfortable:
            # Only push harder when we are comfortably inside the budget.  The
            # gap between "comfortable" and "target" is a deadband: inside it we
            # hold, which is what stops the loop from hunting.
            step = self.increase_step * (1.0 + min(idle_fraction, 0.6) * 3.0)
            self.aggressiveness = min(self.max, self.aggressiveness + step)
            note = "increase"
        else:
            note = "hold"

        self.history.append({
            "at": round(now, 1),
            "from": round(old, 3),
            "to": round(self.aggressiveness, 3),
            "window_abandon_rate": round(rate, 4),
            "window_answers": d_answers,
            "idle_fraction": round(idle_fraction, 3),
            "action": note,
        })
        self._reset(now, abandons_total, answers_total,
                    idle_seconds_total, on_shift_seconds)

    def _reset(self, now, abandons, answers, idle, shift) -> None:
        self._window_start = now
        self._abandons = abandons
        self._answers = answers
        self._idle = idle
        self._shift = shift

    def propose(self, inputs: PacingInputs, mode: PacingMode) -> PacingDecision:
        inputs.aggressiveness = self.aggressiveness
        decision = self.engine.propose(inputs, mode)
        decision.rationale["adaptive_aggressiveness"] = round(self.aggressiveness, 3)
        return decision
