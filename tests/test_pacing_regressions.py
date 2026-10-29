"""Regression tests for pacing and service-time estimation.

These exist because three separate defects in this area were each invisible to
unit tests and glaring in a 30-minute simulation:

  1. the service-time estimate stayed at its prior for the first ~10% of a
     shift, so a 180-second campaign was planned as though calls took 102s and
     the pipeline was sized ~1.9x too large;
  2. the pipeline level also acted as a refill *rate* limit, so a floor whose
     calls end in synchronised waves could not be refilled after a wave broke;
  3. the occupancy discount was applied to only part of the agent supply, so
     the sustained pipeline ran ~13% above flow balance and a standing hold
     queue formed.

Each is pinned below by the smallest test that would have caught it.  The slow
tests at the bottom re-run the simulator, because no unit test can tell you
whether a dialer is actually well-behaved over a shift.
"""

from __future__ import annotations

import math

import pytest

from smartdialer.engine.dialer import Metrics
from smartdialer.engine.pacing import (
    AdaptivePacer,
    PacingDecision,
    PacingEngine,
    PacingInputs,
    PacingMode,
)
from smartdialer.engine.safety import SafetyConfig, SafetyContext, SafetyController
from smartdialer.stats import ServiceEstimate

ABANDON_BUDGET = 0.03


# --------------------------------------------------------------------------- #
# Service-time estimation
# --------------------------------------------------------------------------- #
def test_service_estimate_recovers_a_known_mean():
    est = ServiceEstimate(prior_seconds=100.0)
    for _ in range(200):
        est.observe_completed(180.0)
    assert abs(est.completed_mean - 180.0) < 1.0
    # Once enough calls have finished, real measurements win.  The turnover
    # proxy is deliberately biased high by the warm-up period, so leaving it in
    # charge over-estimated service by 3x and throttled the dialer.
    assert est.plan == pytest.approx(est.completed_mean)


def test_service_estimate_is_conservative_before_any_agent_returns():
    """The warm-up bug, isolated.

    With 180s calls nothing completes for ~180s.  An estimator that only looks
    at finished calls therefore reports its prior for the whole of that period,
    and the dialer sizes a pipeline for 102s calls while running 192s ones.
    """
    est = ServiceEstimate(prior_seconds=102.0)
    for _ in range(170):
        est.observe_busy(100, 1.0)          # 100 agents busy, 170 seconds

    assert est.n_freed == 0 and est.n_completed == 0
    assert est.plan > 102.0, "must not claim the prior when nothing has returned"
    assert est.confidence < 0.35, "the cold-start gate must fire on this"

    # Agents start coming back -> the gate opens.
    for t in range(400):
        est.observe_busy(100, 1.0)
        if t % 4 == 0:
            est.observe_free(2)
    assert est.n_freed == 200
    assert est.confidence > 0.9


def test_service_estimate_only_counts_genuine_turnover():
    """An agent released because a call failed before it was answered was never
    tied up for a service duration.  Counting those would inflate the observed
    free rate and make the dialer think agents come back faster than they do.
    """
    est = ServiceEstimate(prior_seconds=102.0)
    for _ in range(100):
        est.observe_busy(50, 1.0)
    before = est.n_freed
    assert before == 0
    est.observe_free(1)
    assert est.n_freed == 1


# --------------------------------------------------------------------------- #
# Reaction speed
# --------------------------------------------------------------------------- #
def test_abandon_window_is_measured_in_seconds_not_answers():
    """Windowing on a count makes the brake's reaction time depend on how busy
    the floor is: about 50s in the 50%/90s scenario but about 200s in the
    70%/180s one.  It reacts slowest exactly where it matters most.
    """
    m = Metrics()
    for _ in range(10):
        m.record_answer(True, now=0.0)
    for _ in range(90):
        m.record_answer(False, now=1.0)

    assert m.window_abandon_rate(60.0) == pytest.approx(0.10)
    assert m.window_sample_size(60.0) == 100

    # Long after the window has passed, nothing is left inside it.
    assert m.window_abandon_rate(10_000.0) == 0.0
    assert m.window_sample_size(10_000.0) == 0


def test_hold_queue_triggers_an_immediate_backoff():
    """The fast path.  A borrower sitting in the hold queue is an abandon that
    has not been billed yet, and it is visible seconds earlier.  Reacting only
    to recorded abandons means every correction lands a patience window late,
    after the next batch has been dialed into the same jam.
    """
    pacer = AdaptivePacer(PacingEngine(), window_seconds=30.0)
    pacer.observe(0.0, 0, 0, 0.0, 100.0)                 # prime the window
    before = pacer.aggressiveness

    pacer.observe(1.0, 0, 0, 0.0, 100.0, holding=2)      # streak 1
    assert pacer.aggressiveness == before, "one tick is not yet a queue"

    pacer.observe(2.0, 0, 0, 0.0, 100.0, holding=2)      # streak 2 -> fire
    assert pacer.aggressiveness < before
    assert pacer.history[-1]["action"].startswith("fast-decrease")


def test_a_lone_waiter_does_not_trigger_the_fast_path():
    """Depth 2 is the threshold because that is where abandons actually
    happen: measured over an unfixed 70%/180s run, 76% of abandons occurred at
    queue depth >= 2, a lone waiter usually being served by the next agent to
    free.  Backing off on depth 1 would cost utilisation for no safety.
    """
    pacer = AdaptivePacer(PacingEngine(), window_seconds=30.0)
    pacer.observe(0.0, 0, 0, 0.0, 100.0)
    before = pacer.aggressiveness
    for t in range(1, 6):
        pacer.observe(float(t), 0, 0, 0.0, 100.0, holding=1)
    assert pacer.aggressiveness == before


def test_hold_queue_pressure_is_graded_not_binary():
    """A gate that only trips once the queue is already full is too late to
    help anybody: the borrowers at the back have effectively abandoned the
    moment they joined, and cutting the *next* batch to zero does nothing for
    them.  The response must be proportional.
    """
    controller = SafetyController(SafetyConfig(patience_seconds=5.0))
    # A raw intent, so this isolates what the controller does to a large
    # request rather than what the pacer happened to ask for.
    intent = PacingDecision(requested_calls=200, mode=PacingMode.PREDICTIVE,
                            confidence=1.0, rationale={})

    def _ctx(waiting):
        return SafetyContext(
            campaign_id="C1", now=0.0,
            available_agents=1, on_shift_agents=100, at_risk_agents=90,
            q_free=0.068, p_answer=0.7, confidence=1.0,
            queued_borrowers=10_000, waiting_calls=waiting,
            mean_service_seconds=192.0, avg_ring_seconds=8.0,
            service_confidence=1.0, service_observations=500,
        )

    clear = controller.evaluate(intent, _ctx(0))
    one_waiting = controller.evaluate(intent, _ctx(1))

    assert "hold queue pressure" not in clear.explain()
    assert "hold queue pressure" in one_waiting.explain()
    assert one_waiting.checks["hold_pressure"] == pytest.approx(0.5)
    # Reduced, but not to nothing -- there is still a chance to serve the
    # borrower who is waiting, so the response must not be a cliff.
    assert 0 < one_waiting.allowed_calls < clear.allowed_calls


# --------------------------------------------------------------------------- #
# The pacing arithmetic itself
# --------------------------------------------------------------------------- #
def _closed_form_target(A: float, V: float, p: float, z: float) -> float:
    disc = z * z * (1.0 - p) ** 2 + 4.0 * (A * (1.0 - p) + V)
    s = (-z * (1.0 - p) + math.sqrt(max(disc, 0.0))) / 2.0
    return max(0.0, (A - z * s) / p)


def test_safety_stock_closed_form_matches_brute_force():
    """Largest T with T*p + z*sqrt(T*p*(1-p) + V) <= A, solved as a quadratic.
    If this algebra is wrong the engine silently paces to the wrong target.
    """
    for A in (2.0, 5.0, 20.0, 60.0):
        for V in (0.0, 1.0, 4.0, 12.0):
            for p in (0.2, 0.5, 0.7, 0.9):
                for z in (0.5, 1.0, 2.25, 3.0):
                    want = _closed_form_target(A, V, p, z)
                    # brute force on a 0.1 grid
                    best = 0.0
                    step = 0.1
                    k = 0
                    while k < 200_000:
                        T = k * step
                        if T * p + z * math.sqrt(T * p * (1.0 - p) + V) <= A:
                            best = T
                        else:
                            break
                        k += 1
                    assert abs(want - best) <= step + 1e-9, (A, V, p, z)


def _target(service: float, p_answer: float, spare: int = 10) -> float:
    engine = PacingEngine()
    d = engine.propose_predictive(
        PacingInputs(
            on_shift_agents=100, busy_agents=100, available_agents=spare,
            p_answer=p_answer, avg_ring_seconds=8.0,
            mean_service_seconds=service, queued_borrowers=10 ** 6,
            max_batch=10 ** 6,
        )
    )
    return d.rationale["target_in_flight"]


def test_longer_calls_are_paced_more_conservatively():
    """Identical floors differing only in call length: the long-call floor must
    get the smaller pipeline.  Long calls both slow the agent supply and make
    it lumpier -- the coefficient of variation of the number of agents
    finishing inside a ring window is sqrt((1-q)/(busy*q)) with q =
    ring/service -- so the safety stock rises on two counts at once.
    """
    short, long_ = _target(90.0, 0.7), _target(180.0, 0.7)
    assert long_ < short
    assert long_ < 0.85 * short


def test_higher_answer_rates_are_paced_more_conservatively():
    """At 70% nearly every dial turns into a conversation that must be served,
    so the pipeline has to be smaller than at 20%, where most dials simply
    fail and cost an agent nothing.
    """
    low, high = _target(180.0, 0.20), _target(180.0, 0.70)
    assert high < low
    assert high < 0.5 * low


# --------------------------------------------------------------------------- #
# End-to-end: these are the ones that would have caught all three bugs.
# --------------------------------------------------------------------------- #
def _changing(sim, now):
    total = sim.scenario.duration_seconds
    frac = now / total
    if frac < 0.33:
        rate = 0.60
    elif frac < 0.66:
        rate = 0.60 - 0.35 * ((frac - 0.33) / 0.33)
    else:
        rate = 0.25 + 0.30 * ((frac - 0.66) / 0.34)
    sim.line.answer_rate = rate
    sim.line.talk_seconds = (150.0, 240.0) if frac >= 0.5 else (60.0, 110.0)


SCENARIOS = {
    "A": dict(answer_rate=0.20, talk_seconds=(100.0, 140.0)),
    "B": dict(answer_rate=0.50, talk_seconds=(75.0, 105.0)),
    "C": dict(answer_rate=0.70, talk_seconds=(150.0, 210.0)),
    "D": dict(answer_rate=0.60, talk_seconds=(60.0, 110.0)),
}


def _run(name, provider_cls, seed, mode, duration, agents=100):
    from smartdialer.sim.simulator import Scenario, Simulation

    sc = Scenario(name, agents=agents, duration_seconds=duration, seed=seed,
                  provider_cls=provider_cls,
                  mutate=_changing if name == "D" else None, **SCENARIOS[name])
    return Simulation(sc, mode=mode).run()


@pytest.mark.slow
@pytest.mark.parametrize("provider_name", ["A", "B"])
@pytest.mark.parametrize("seed", [1, 2])
def test_scenario_c_stays_inside_the_abandon_budget(provider_name, seed):
    """Scenario C (70% answer, 180s talk) is the hard case: long calls make
    the agent supply slow and lumpy, and a high answer rate means almost every
    dial has to be served.

    Before the fixes this ran at 4.3% abandoned calls on Provider A and 6.9% on
    Provider B, both well outside the 3% budget the Safety Controller claims to
    defend.  It must now stay inside it on both providers.
    """
    from smartdialer.providers.mock import ProviderA, ProviderB

    cls = ProviderA if provider_name == "A" else ProviderB
    result = _run("C", cls, seed, PacingMode.PREDICTIVE, duration=900.0)
    rate = float(result.metrics["abandon_rate"])
    assert rate <= ABANDON_BUDGET, (
        f"Provider {provider_name} seed {seed}: abandon {rate:.2%} "
        f"over {ABANDON_BUDGET:.0%} budget"
    )
    # ...and it must not have bought that safety by going idle.
    assert float(result.metrics["talk_utilization"]) > 0.80


@pytest.mark.slow
@pytest.mark.parametrize("name", ["A", "B", "D"])
def test_predictive_beats_progressive_on_utilisation(name):
    """The point of predictive dialing is to recover the agent time progressive
    mode wastes while the phone rings.  If it does not beat progressive on
    utilisation it is not worth its risk, and the honest move is to run
    progressive.
    """
    from smartdialer.providers.mock import ProviderA

    prog = _run(name, ProviderA, 1, PacingMode.PROGRESSIVE, duration=600.0)
    pred = _run(name, ProviderA, 1, PacingMode.PREDICTIVE, duration=600.0)
    p = float(prog.metrics["talk_utilization"])
    q = float(pred.metrics["talk_utilization"])
    assert q > p, f"scenario {name}: predictive {q:.1%} <= progressive {p:.1%}"
    assert float(pred.metrics["abandon_rate"]) <= ABANDON_BUDGET
