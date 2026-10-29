"""Call and agent lifecycle tests, including the hostile event sequences the
assignment lists explicitly."""

import pytest

from smartdialer.domain.models import Call, ProviderEvent, new_id
from smartdialer.domain.states import (
    AGENT_TRANSITIONS,
    AgentState,
    CallState,
    RejectReason,
    agent_state_after_call,
    can_transition_agent,
    evaluate_transition,
)
from smartdialer.stores.memory import MemoryStore


def ev(call_id, seq, type_, eid=None):
    return ProviderEvent(
        event_id=eid or new_id("evt"), call_id=call_id, seq=seq, type=type_
    )


def seeded(store, state=CallState.RESERVED, call_id="C1"):
    store.create_call(
        Call(call_id=call_id, borrower_id="B1", campaign_id="C1", state=state)
    )
    return call_id


# --------------------------------------------------------------------------- #
# The three sequences named in the assignment
# --------------------------------------------------------------------------- #
def test_duplicate_answered_then_completed(store=None):
    """ANSWERED, ANSWERED, ANSWERED, COMPLETED -> exactly one transition."""
    s = MemoryStore()
    seeded(s)
    assert s.apply_event(ev("C1", 1, "INITIATED"), 1.0).applied
    assert s.apply_event(ev("C1", 2, "RINGING"), 2.0).applied

    assert s.apply_event(ev("C1", 3, "ANSWERED"), 3.0).applied
    # The repeats are semantically impossible: ANSWERED -> ANSWERED is not a
    # legal transition *and* it would not move the call forward, so it is
    # refused on both counts.  Which check fires first is an implementation
    # detail; what matters is that nothing is applied.
    for seq in (4, 5):
        result = s.apply_event(ev("C1", seq, "ANSWERED"), float(seq))
        assert not result.applied
        assert result.status in (
            RejectReason.OUT_OF_ORDER, RejectReason.ILLEGAL,
            RejectReason.STALE_SEQ,
        )

    assert s.apply_event(ev("C1", 6, "COMPLETED"), 6.0).applied
    call = s.get_call("C1")
    assert call.state == CallState.COMPLETED
    # One transition per state change and no more.
    assert len(s.history) == 4


def test_completed_then_late_ringing_is_ignored():
    """COMPLETED, ANSWERED, RINGING -> the call stays COMPLETED."""
    s = MemoryStore()
    seeded(s)
    s.apply_event(ev("C1", 1, "INITIATED"), 1.0)
    s.apply_event(ev("C1", 2, "RINGING"), 2.0)
    s.apply_event(ev("C1", 3, "ANSWERED"), 3.0)
    assert s.apply_event(ev("C1", 4, "COMPLETED"), 4.0).applied

    late = s.apply_event(ev("C1", 5, "ANSWERED"), 5.0)
    assert late.status == RejectReason.TERMINAL
    late2 = s.apply_event(ev("C1", 6, "RINGING"), 6.0)
    assert late2.status == RejectReason.TERMINAL

    assert s.get_call("C1").state == CallState.COMPLETED
    assert len(s.history) == 4  # no phantom transitions were recorded


def test_out_of_order_delivery_does_not_move_the_call_backwards():
    """Provider B delivers ANSWERED before RINGING.  Order must not win."""
    s = MemoryStore()
    seeded(s)
    s.apply_event(ev("C1", 1, "INITIATED"), 1.0)

    # ANSWERED arrives first (seq 3), which is legal from INITIATED.
    assert s.apply_event(ev("C1", 3, "ANSWERED"), 2.0).applied
    # RINGING arrives late (seq 2): legal in isolation, but it would move the
    # call backwards, so it is dropped.
    stale = s.apply_event(ev("C1", 2, "RINGING"), 3.0)
    # The sequence guard catches it first (2 <= 3); the rank guard would reject
    # it too.  Either way it must not be applied.
    assert not stale.applied
    assert stale.status in (RejectReason.STALE_SEQ, RejectReason.OUT_OF_ORDER)
    assert s.get_call("C1").state == CallState.ANSWERED


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_same_event_id_redelivered_is_a_no_op():
    s = MemoryStore()
    seeded(s)
    e = ev("C1", 1, "INITIATED", eid="EVT-1")
    assert s.apply_event(e, 1.0).applied

    again = s.apply_event(e, 2.0)
    assert again.status == RejectReason.DUPLICATE
    assert not again.applied
    assert len(s.history) == 1


def test_redelivered_rejected_event_stays_rejected():
    """A rejected event must not become applicable just because it comes back."""
    s = MemoryStore()
    seeded(s)
    bad = ev("C1", 9, "RINGING", eid="EVT-STUCK")
    # Can't skip straight to RINGING from RESERVED.
    first = s.apply_event(bad, 1.0)
    assert first.status == RejectReason.ILLEGAL
    second = s.apply_event(bad, 2.0)
    assert second.status == RejectReason.DUPLICATE
    assert len(s.history) == 0


def test_unknown_call_is_reported_not_created():
    s = MemoryStore()
    res = s.apply_event(ev("NOPE", 1, "RINGING"), 1.0)
    assert res.status == RejectReason.UNKNOWN_CALL
    assert s.get_call("NOPE") is None


# --------------------------------------------------------------------------- #
# Pure transition table
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "current,target,expected",
    [
        (CallState.RESERVED, CallState.INITIATED, True),
        (CallState.INITIATED, CallState.RINGING, True),
        (CallState.RINGING, CallState.ANSWERED, True),
        (CallState.ANSWERED, CallState.CONNECTED, True),
        (CallState.CONNECTED, CallState.COMPLETED, True),
        (CallState.QUEUED, CallState.COMPLETED, False),   # can't finish before dialing
        (CallState.COMPLETED, CallState.ANSWERED, False),  # terminal is absorbing
    ],
)
def test_legality(current, target, expected):
    verdict = evaluate_transition(current, target, seq=99, last_seq=0)
    assert verdict.applied is expected


def test_terminal_states_are_absorbing():
    for terminal in (CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED):
        for other in CallState:
            if other is terminal:
                continue
            assert not evaluate_transition(terminal, other, 99, 0).applied


# --------------------------------------------------------------------------- #
# Agent lifecycle
# --------------------------------------------------------------------------- #
def test_agent_lifecycle_happy_path():
    for a, b in [
        (AgentState.OFFLINE, AgentState.AVAILABLE),
        (AgentState.AVAILABLE, AgentState.RESERVED),
        (AgentState.RESERVED, AgentState.DIALING),
        (AgentState.DIALING, AgentState.CONNECTED),
        (AgentState.CONNECTED, AgentState.WRAP_UP),
        (AgentState.WRAP_UP, AgentState.AVAILABLE),
    ]:
        assert can_transition_agent(a, b), f"{a} -> {b} should be legal"


def test_agent_can_disappear_from_reserved_and_dialing():
    """'What happens when the agent disappears during call setup?'"""
    assert can_transition_agent(AgentState.RESERVED, AgentState.OFFLINE)
    assert can_transition_agent(AgentState.DIALING, AgentState.OFFLINE)
    # But not from the middle of a conversation without going through wrap-up.
    assert not can_transition_agent(AgentState.CONNECTED, AgentState.AVAILABLE)


def test_agent_state_mirrors_call_state():
    assert agent_state_after_call(CallState.INITIATED) == AgentState.DIALING
    assert agent_state_after_call(CallState.CONNECTED) == AgentState.CONNECTED
    assert agent_state_after_call(CallState.COMPLETED) == AgentState.WRAP_UP
    assert agent_state_after_call(CallState.FAILED) == AgentState.AVAILABLE


def test_every_agent_state_is_reachable_and_leaveable():
    for state in AgentState:
        assert state in AGENT_TRANSITIONS, f"{state} missing from the table"
        if state is not AgentState.OFFLINE:
            assert AGENT_TRANSITIONS[state], f"{state} is a dead end"
