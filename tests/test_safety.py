"""The Safety Controller is the graded centrepiece: prove it cannot be
bypassed, and that it actually refuses unsafe requests."""

import dataclasses

import pytest

from smartdialer.engine.pacing import PacingEngine, PacingInputs, PacingMode
from smartdialer.engine.safety import (
    SafetyAction,
    SafetyConfig,
    SafetyContext,
    SafetyController,
    SafetyDecision,
)


def ctx(**kw):
    base = dict(
        campaign_id="C1", now=0.0, available_agents=10, on_shift_agents=50,
        p_answer=0.5, confidence=0.9, queued_borrowers=10000,
        provider_healthy=True, provider_name="ProviderA",
        # A mature floor by default: these tests are about the pipeline and
        # abandonment guards, so they must not trip the cold-start gate.
        service_confidence=1.0, service_observations=500,
    )
    base.update(kw)
    return SafetyContext(**base)


def intent(n, mode=PacingMode.PREDICTIVE):
    return PacingEngine().propose_predictive(
        PacingInputs(
            available_agents=10, on_shift_agents=50, p_answer=0.5,
            confidence=0.9, queued_borrowers=10000, max_batch=n,
        )
    ) if mode is PacingMode.PREDICTIVE else PacingEngine().propose_progressive(
        PacingInputs(available_agents=n, queued_borrowers=10000)
    )


# --------------------------------------------------------------------------- #
# Structural guarantee: the pacing engine cannot reach the provider
# --------------------------------------------------------------------------- #
def test_pacing_engine_has_no_way_to_place_a_call():
    engine = PacingEngine()
    decision = engine.propose_predictive(PacingInputs(on_shift_agents=10))
    # The decision is inert data.  It carries no callable, no provider and no
    # store reference -- there is literally nothing to invoke.
    for field_name in ("provider", "gateway", "store", "place_call", "dial"):
        assert not hasattr(decision, field_name)
        assert not hasattr(engine, field_name)
    assert dataclasses.is_dataclass(decision)


def test_pacing_is_a_pure_function_of_its_inputs():
    engine = PacingEngine()
    a = engine.propose_predictive(PacingInputs(on_shift_agents=20, p_answer=0.4))
    b = engine.propose_predictive(PacingInputs(on_shift_agents=20, p_answer=0.4))
    assert a.requested_calls == b.requested_calls
    assert a.rationale == b.rationale


# --------------------------------------------------------------------------- #
# Forged / replayed approvals
# --------------------------------------------------------------------------- #
def test_hand_made_approval_is_refused():
    controller = SafetyController()
    forged = SafetyDecision(
        action=SafetyAction.APPROVE, allowed_calls=999, requested_calls=999,
        mode=PacingMode.PREDICTIVE, decision_id="made-up",
        issued_at=0.0, campaign_id="C1", signature="deadbeef",
    )
    assert not controller.verify(forged, "C1", 0.0)


def test_approval_cannot_be_used_for_a_different_campaign():
    controller = SafetyController()
    decision = controller.evaluate(intent(5), ctx(campaign_id="C1"))
    assert controller.verify(decision, "C1", 0.0)
    assert not controller.verify(decision, "C2", 0.0)


def test_signed_approval_cannot_be_replayed_beyond_its_budget():
    controller = SafetyController()
    decision = controller.evaluate(intent(5), ctx())
    allowed = decision.allowed_calls
    assert allowed > 0
    for _ in range(allowed):
        assert controller.verify(decision, "C1", 0.0)
    # Budget exhausted.
    assert not controller.verify(decision, "C1", 0.0)


def test_stale_approval_expires():
    controller = SafetyController()
    decision = controller.evaluate(intent(5), ctx())
    assert controller.verify(decision, "C1", 0.0)
    assert not controller.verify(decision, "C1", 999.0)


# --------------------------------------------------------------------------- #
# Behaviour under stress
# --------------------------------------------------------------------------- #
def test_open_circuit_breaker_stops_all_dialing():
    controller = SafetyController()
    decision = controller.evaluate(intent(500), ctx(provider_healthy=False))
    assert decision.action is SafetyAction.REJECT
    assert decision.allowed_calls == 0


def test_progressive_never_exceeds_available_agents():
    controller = SafetyController()
    decision = controller.evaluate(
        intent(5000, mode=PacingMode.PROGRESSIVE), ctx(available_agents=7)
    )
    assert decision.allowed_calls <= 7


def test_cold_start_falls_back_to_progressive():
    controller = SafetyController(SafetyConfig(min_confidence_for_predictive=0.5))
    decision = controller.evaluate(intent(500), ctx(confidence=0.05))
    assert decision.action is SafetyAction.FALLBACK_PROGRESSIVE
    assert decision.mode is PacingMode.PROGRESSIVE
    # ...and it still permits safe progressive dialing rather than nothing.
    assert decision.allowed_calls <= ctx().available_agents


def test_sustained_abandonment_trips_the_emergency_brake():
    controller = SafetyController(SafetyConfig(max_observed_abandon_rate=0.05))
    decision = controller.evaluate(
        intent(500),
        ctx(observed_abandon_rate=0.20, abandon_sample_size=100),
    )
    assert decision.action is SafetyAction.FALLBACK_PROGRESSIVE


def test_unknown_agent_turnover_forces_progressive():
    """Predicting that an agent will be free requires having watched some
    become free.  Before that, dialing ahead is a guess, so we fall back to the
    mode whose safety is arithmetic rather than statistical.

    This is the guard that makes the 70%/180s scenario conservative: long calls
    delay the first observed turnover, so the dialer keeps proving safety by
    construction for longer instead of by prediction.
    """
    controller = SafetyController()
    decision = controller.evaluate(
        intent(500),
        ctx(service_confidence=0.0, service_observations=0, available_agents=7),
    )
    assert decision.action is SafetyAction.FALLBACK_PROGRESSIVE
    assert decision.mode is PacingMode.PROGRESSIVE
    # Still allowed to dial 1:1 with the agents it can see.
    assert decision.allowed_calls == 7
    assert "turnover" in decision.explain()


def test_abandonment_guard_bounds_the_pipeline():
    """A wildly over-confident request must be cut down, not honoured."""
    controller = SafetyController()
    decision = controller.evaluate(
        intent(5000),
        ctx(available_agents=0, on_shift_agents=50, at_risk_agents=50, q_free=0.05),
    )
    assert decision.allowed_calls < 60
    assert decision.action in (SafetyAction.REDUCE, SafetyAction.REJECT)
    assert "pipeline ceiling" in decision.explain() or "abandonment risk" in decision.explain()


def test_every_decision_is_explainable():
    controller = SafetyController()
    for kw in (
        dict(),
        dict(provider_healthy=False),
        dict(confidence=0.0),
        dict(available_agents=0),
        dict(observed_abandon_rate=0.5, abandon_sample_size=100),
    ):
        decision = controller.evaluate(intent(20), ctx(**kw))
        assert decision.reasons, "a decision with no reason is not auditable"
        assert decision.action.value in decision.explain()


def test_controller_never_returns_a_negative_budget():
    controller = SafetyController()
    for n in (0, 1, 50, 5000):
        d = controller.evaluate(intent(n), ctx(available_agents=0, on_shift_agents=0))
        assert d.allowed_calls >= 0


def test_abandon_probability_is_monotonic_in_pipeline_size():
    """If it were not, the binary search in _max_safe_in_flight would be wrong."""
    from smartdialer.stats import abandon_probability

    prev = 0.0
    for n in range(0, 40):
        p = abandon_probability(n, 0.5, 5, 10, 0.1)
        assert p >= prev - 1e-12
        prev = p
