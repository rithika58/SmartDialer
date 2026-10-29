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
    # How much of the service-time estimate is observed turnover rather than the
    # prior, and how long a borrower will wait before we call it an abandon.
    service_confidence: float = 0.0
    patience_seconds: float = 5.0
    # Agents actually serving right now (CONNECTED + WRAP_UP), and answered
    # borrowers still holding for an agent.  Both are needed to turn the
    # steady-state pipeline level into a target for *this* instant.
    busy_agents: int = 0
    waiting_calls: int = 0
    # Full service time (talk + wrap) already measured.  Preferred over
    # avg_talk_seconds + avg_wrap_seconds, which double-counts wrap when the
    # caller passes a pre-combined estimate.
    mean_service_seconds: float = 0.0

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

    def __init__(self, aggressiveness: float = 1.0,
                 safety_stock_z: float = 2.25) -> None:
        self.aggressiveness = aggressiveness
        # How many standard deviations of agent-supply noise to hold back.
        # See `propose_predictive` for why this is the knob that long calls
        # care about.
        self.safety_stock_z = safety_stock_z

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
        """How many calls to keep unanswered, right now.

        Flow balance first.  Agents complete a call every `service` seconds, so
        the floor needs `busy / service` answers per second; each dial yields an
        answer after `ring` seconds with probability `p`, so sustaining `A`
        answers/sec means dialing `A/p` per second and holding `(A/p) * ring`
        calls unanswered:

            level = occupancy * busy * ring / (service * p)

        `occupancy` is a deliberate trim -- the raw flow balance targets rho=1,
        which is not an operating point but a queue that grows without bound.

        Then three corrections, each of which fixed a measured defect:

        1. Agents free RIGHT NOW need no prediction.  They can take an answer
           the instant it arrives, so each supports 1/p dialed calls.  Without
           this the level also acts as a refill *rate* limit, and since long
           calls end in synchronised waves, forty agents would free at once and
           the dialer could only replace them `level` at a time.

        2. Slack must be proportional to the SPREAD, not the mean.  At 180s
           service ~4 agents come back per ring window, so 10% of that is 0.4
           of an agent, while the standard deviation of that same count is ~2.0
           agents -- the margin was five times smaller than the noise it was
           meant to absorb.  This is solved in closed form below.

        3. The occupancy trim applies to the WHOLE supply, including the agents
           free right now.  Trimming only the freeing term let the sustained
           pipeline run ~13% above flow balance whenever the floor had any idle
           agents at all -- and a saturated floor always has two or three.

        The result is self-limiting: as the pipeline fills, the request goes to
        zero.
        """
        p = _clamp(inputs.p_answer, 0.02, 0.99)
        service = max(
            inputs.mean_service_seconds
            or (inputs.avg_talk_seconds + inputs.avg_wrap_seconds),
            1.0,
        )
        ring = max(inputs.avg_ring_seconds, 1.0)
        n = max(inputs.on_shift_agents, 0)
        busy = max(inputs.busy_agents, 0)
        occupancy = _clamp(inputs.target_occupancy, 0.30, 0.99)

        # Supply side.  `busy * ring / service` agents are expected to free
        # within a ring window; the count is roughly Binomial(busy,
        # ring/service), so it varies, and that variance is the whole
        # difficulty.  Its coefficient of variation, sqrt((1-q)/(busy*q)), rises
        # as service grows -- long calls are both slower and lumpier, so this
        # term makes the dialer conservative about them for free.
        q_ring = min(1.0, ring / service)
        raw_supply = busy * ring / service
        supply_var = max(busy, 0) * q_ring * (1.0 - q_ring)

        # Agents who are free RIGHT NOW need no prediction: they can take an
        # answer the moment it arrives, so each of them supports 1/p dialed
        # calls.  Borrowers already holding have first claim on them.
        #
        # Without this term the target was also a refill RATE limit, which is a
        # real bug when calls end in synchronised waves -- as they do when
        # service is long and a batch was dialed together.  In the 70%/180s
        # scenario forty agents would free at once after a wave and the dialer
        # could only replace them `frees/p` at a time, so the floor sat idle
        # for a hundred seconds while borrowers went uncalled.
        spare = max(0, inputs.available_agents - inputs.waiting_calls)

        # Demand side.  T calls in flight produce Binomial(T, p) answers, and
        # that spread matters most in the burst, which is where long calls do
        # all their dialing: dialing the naive 40/p calls for forty freed agents
        # produces 40 answers on average, a queue with no margin at all, and the
        # borrowers at the back of it are the ones who abandon.
        #
        # So solve for the largest pipeline keeping demand inside supply with a
        # margin of `z` standard deviations:
        #
        #     T*p + z*sqrt(T*p*(1-p) + V)  <=  A
        #
        # Substituting s = sqrt(T*p*(1-p) + V) and T*p = A - z*s gives a
        # quadratic in s, so this is closed-form rather than a search.  `z` is
        # calibrated (2.25): the point where measured utilisation stops
        # improving while measured abandonment keeps falling.
        A = occupancy * (spare + raw_supply)
        V = supply_var
        z = self.safety_stock_z
        disc = z * z * (1.0 - p) ** 2 + 4.0 * (A * (1.0 - p) + V)
        s = (-z * (1.0 - p) + math.sqrt(max(disc, 0.0))) / 2.0
        target_in_flight = max(0.0, (A - z * s) / p)

        scaled = target_in_flight * self.aggressiveness * inputs.aggressiveness
        wanted = int(math.ceil(scaled)) - inputs.ringing_calls
        wanted = min(wanted, inputs.queued_borrowers, inputs.max_batch)
        wanted = max(0, wanted)

        return PacingDecision(
            requested_calls=wanted,
            mode=PacingMode.PREDICTIVE,
            confidence=inputs.confidence,
            rationale={
                "rule": ("hold T calls in flight, where T is the largest count "
                         "with T*p + z*sqrt(T*p*(1-p) + Var[supply]) <= supply"),
                "on_shift_agents": n,
                "available_agents": inputs.available_agents,
                "ringing_calls": inputs.ringing_calls,
                "connected_calls": inputs.connected_calls,
                "p_answer": round(p, 4),
                "avg_ring_seconds": round(ring, 2),
                "mean_service_seconds": round(service, 2),
                "answers_needed_per_sec": round(n / service, 4),
                "target_occupancy": round(occupancy, 3),
                "spare_agents": spare,
                "supply_mean_raw": round(raw_supply, 3),
                "supply_sd": round(math.sqrt(max(supply_var, 0.0)), 3),
                "safety_stock_z": round(z, 3),
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
        # Fast path: back off when the hold queue reaches this depth for this
        # many consecutive observations, without waiting for the window to
        # close or for anyone to actually abandon.
        hold_pressure_trigger: int = 2,
        hold_pressure_ticks: int = 2,
        hold_decrease: float = 0.15,
        # Close the control window early once this many answers have landed, so
        # the loop reacts at a rate set by evidence rather than by the clock.
        window_answers_target: int = 20,
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
        self.hold_pressure_trigger = hold_pressure_trigger
        self.hold_pressure_ticks = hold_pressure_ticks
        self.hold_decrease = hold_decrease
        self.window_answers_target = window_answers_target
        self.clock = clock
        self._hold_streak = 0
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
        holding: int = 0,
    ) -> None:
        # ---- fast path: back off on the queue, not on the bill -------------
        # Two or more waiting at once is the threshold.  That is not a guess:
        # measured over an unfixed 70%/180s run, 76% of abandons happened at
        # queue depth >= 2, because a lone waiter is usually served by the very
        # next agent to free whereas a queue of three is not.
        if holding >= self.hold_pressure_trigger:
            self._hold_streak += 1
            if self._hold_streak >= self.hold_pressure_ticks:
                self._back_off(now, abandons_total, answers_total,
                               idle_seconds_total, on_shift_seconds,
                               f"fast-decrease (hold queue depth {holding})",
                               self.hold_decrease)
                self._hold_streak = 0
                return
        else:
            self._hold_streak = 0

        if self._window_start == 0.0:
            self._reset(now, abandons_total, answers_total,
                        idle_seconds_total, on_shift_seconds)
            return
        # Close the window on evidence as well as on the clock: waiting a flat
        # 30s means the loop takes twice as long to notice a problem on a busy
        # floor as on a quiet one.
        if (now - self._window_start < self.window_seconds
                and (answers_total - self._answers) < self.window_answers_target):
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
            old = self.aggressiveness
            self.aggressiveness = max(
                self.min, self.aggressiveness * (1.0 - self.decrease_strength * excess)
            )
            note = "decrease"
            self._record(now, old, note, rate, d_answers, idle_fraction)
            self._reset(now, abandons_total, answers_total,
                        idle_seconds_total, on_shift_seconds)
            return
        elif idle_fraction > self.idle_trigger_fraction and rate <= comfortable:
            # Only push harder when we are comfortably inside the budget.  The
            # gap between "comfortable" and "target" is a deadband: inside it we
            # hold, which is what stops the loop from hunting.
            step = self.increase_step * (1.0 + min(idle_fraction, 0.6) * 3.0)
            self.aggressiveness = min(self.max, self.aggressiveness + step)
            note = "increase"
        else:
            note = "hold"

        self._record(now, old, note, rate, d_answers, idle_fraction)
        self._reset(now, abandons_total, answers_total,
                    idle_seconds_total, on_shift_seconds)

    def _back_off(self, now, abandons, answers, idle, shift, note, strength) -> None:
        """Immediate multiplicative decrease, outside the control window."""
        old = self.aggressiveness
        self.aggressiveness = max(self.min, self.aggressiveness * (1.0 - strength))
        self._record(now, old, note, 0.0, 0, 0.0)
        self._reset(now, abandons, answers, idle, shift)

    def _record(self, now, old, note, rate, d_answers, idle_fraction) -> None:
        self.history.append({
            "at": round(now, 1),
            "from": round(old, 3),
            "to": round(self.aggressiveness, 3),
            "window_abandon_rate": round(rate, 4),
            "window_answers": d_answers,
            "idle_fraction": round(idle_fraction, 3),
            "action": note,
        })

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
