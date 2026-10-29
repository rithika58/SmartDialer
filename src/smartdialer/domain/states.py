"""
Explicit state machines for agents and calls.

Why this module exists
----------------------
The assignment asks for an explicit agent lifecycle and an explicit call
lifecycle, and then asks what happens when the telecom provider sends events
that are duplicated, out of order, or simply impossible.

The rules that matter:

1. A transition is legal only if it appears in the transition table.
2. A transition to a NON-TERMINAL state is applied only if it moves the call
   FORWARD.  Every state has a rank; going backwards is rejected as a stale /
   out-of-order event.  This is what makes "COMPLETED, ANSWERED, RINGING"
   harmless: after COMPLETED the call is terminal and nothing can move it.
3. Terminal states (COMPLETED / FAILED / CANCELLED) are absorbing.  A late
   ANSWERED after a COMPLETED is dropped, not applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Optional, Tuple


class AgentState(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"


# Agent lifecycle.  Anything not listed here is illegal and will be refused.
AGENT_TRANSITIONS: Dict[AgentState, FrozenSet[AgentState]] = {
    AgentState.OFFLINE: frozenset({AgentState.AVAILABLE}),
    AgentState.AVAILABLE: frozenset(
        {AgentState.RESERVED, AgentState.PAUSED, AgentState.OFFLINE}
    ),
    AgentState.RESERVED: frozenset(
        {
            AgentState.DIALING,
            AgentState.AVAILABLE,  # reservation released (no borrower, lease expiry)
            AgentState.PAUSED,
            AgentState.OFFLINE,    # agent disappeared mid-setup
        }
    ),
    AgentState.DIALING: frozenset(
        {
            AgentState.CONNECTED,
            AgentState.AVAILABLE,  # call failed before answer
            AgentState.OFFLINE,    # agent disappeared mid-setup
        }
    ),
    AgentState.CONNECTED: frozenset(
        {AgentState.WRAP_UP, AgentState.OFFLINE}
    ),
    AgentState.WRAP_UP: frozenset(
        {AgentState.AVAILABLE, AgentState.PAUSED, AgentState.OFFLINE}
    ),
    AgentState.PAUSED: frozenset({AgentState.AVAILABLE, AgentState.OFFLINE}),
    AgentState.OFFLINE: frozenset({AgentState.AVAILABLE}),
}

# States in which an agent is genuinely able to take another call.
AGENT_IDLE_STATES = frozenset({AgentState.AVAILABLE})

# States that consume agent capacity (used by the Safety Controller).
AGENT_BUSY_STATES = frozenset(
    {AgentState.RESERVED, AgentState.DIALING, AgentState.CONNECTED, AgentState.WRAP_UP}
)


class CallState(str, Enum):
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


CALL_TERMINAL_STATES = frozenset(
    {CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}
)

# Monotonic rank.  Used to reject out-of-order / late events.
CALL_STATE_RANK: Dict[CallState, int] = {
    CallState.QUEUED: 0,
    CallState.RESERVED: 1,
    CallState.INITIATED: 2,
    CallState.RINGING: 3,
    CallState.ANSWERED: 4,
    CallState.CONNECTED: 5,
    CallState.COMPLETED: 6,
    CallState.FAILED: 6,
    CallState.CANCELLED: 6,
}

CALL_TRANSITIONS: Dict[CallState, FrozenSet[CallState]] = {
    CallState.QUEUED: frozenset({CallState.RESERVED, CallState.CANCELLED}),
    CallState.RESERVED: frozenset({CallState.INITIATED, CallState.CANCELLED, CallState.FAILED}),
    CallState.INITIATED: frozenset(
        {CallState.RINGING, CallState.ANSWERED, CallState.COMPLETED,
         CallState.FAILED, CallState.CANCELLED}
    ),
    CallState.RINGING: frozenset(
        {CallState.ANSWERED, CallState.CONNECTED, CallState.COMPLETED,
         CallState.FAILED, CallState.CANCELLED}
    ),
    CallState.ANSWERED: frozenset(
        {CallState.CONNECTED, CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}
    ),
    CallState.CONNECTED: frozenset(
        {CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}
    ),
    # Terminal states are absorbing.
    CallState.COMPLETED: frozenset(),
    CallState.FAILED: frozenset(),
    CallState.CANCELLED: frozenset(),
}

# Provider event type -> target call state.
# The dialer never trusts the provider's vocabulary; it translates it.
PROVIDER_EVENT_TO_CALL_STATE: Dict[str, CallState] = {
    "INITIATED": CallState.INITIATED,
    "RINGING": CallState.RINGING,
    "ANSWERED": CallState.ANSWERED,
    "CONNECTED": CallState.CONNECTED,
    "COMPLETED": CallState.COMPLETED,
    "FAILED": CallState.FAILED,
    "NO_ANSWER": CallState.FAILED,
    "BUSY": CallState.FAILED,
    "CANCELLED": CallState.CANCELLED,
}

# Events that mean "a human picked up".  These are the compliance-critical ones:
# if we reach this state without an agent, the call is abandoned.
CONNECT_EVENTS = frozenset({"ANSWERED", "CONNECTED"})


class RejectReason(str, Enum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"          # identical event_id already processed
    STALE_SEQ = "STALE_SEQ"          # seq not newer than last applied seq
    ILLEGAL = "ILLEGAL"              # transition not in the table
    OUT_OF_ORDER = "OUT_OF_ORDER"    # would move the call backwards
    TERMINAL = "TERMINAL"            # call already finished
    UNKNOWN_CALL = "UNKNOWN_CALL"


@dataclass(frozen=True)
class TransitionResult:
    applied: bool
    reason: RejectReason
    from_state: Optional[CallState] = None
    to_state: Optional[CallState] = None
    detail: str = ""


def can_transition_agent(current: AgentState, target: AgentState) -> bool:
    return target in AGENT_TRANSITIONS.get(current, frozenset())


def evaluate_transition(
    current: CallState, target: CallState, seq: Optional[int], last_seq: int
) -> TransitionResult:
    """Pure function: decide what to do with a provider-driven state change.

    Deliberately pure so it can be unit-tested exhaustively and reasoned about
    without a database.  The store is responsible for durability; this decides
    legality.
    """
    if current in CALL_TERMINAL_STATES:
        return TransitionResult(
            False, RejectReason.TERMINAL, current, target,
            f"call already {current.value}; late events are ignored",
        )

    if seq is not None and seq <= last_seq:
        return TransitionResult(
            False, RejectReason.STALE_SEQ, current, target,
            f"event seq {seq} <= last applied seq {last_seq}",
        )

    if target not in CALL_TRANSITIONS.get(current, frozenset()):
        return TransitionResult(
            False, RejectReason.ILLEGAL, current, target,
            f"{current.value} -> {target.value} is not a legal transition",
        )

    # Non-terminal targets must move the call forward.  A terminal target is
    # always allowed from any live state (a call can die at any moment).
    if target not in CALL_TERMINAL_STATES:
        if CALL_STATE_RANK[target] <= CALL_STATE_RANK[current]:
            return TransitionResult(
                False, RejectReason.OUT_OF_ORDER, current, target,
                f"{current.value} -> {target.value} would move the call backwards",
            )

    return TransitionResult(True, RejectReason.APPLIED, current, target, "ok")


def agent_state_after_call(call_state: CallState) -> Optional[AgentState]:
    """Mirror a call transition onto the agent lifecycle.

    Returns None when the agent's state should not change.
    """
    mapping = {
        CallState.INITIATED: AgentState.DIALING,
        CallState.ANSWERED: AgentState.DIALING,   # ringing stopped, bridging
        CallState.CONNECTED: AgentState.CONNECTED,
        CallState.COMPLETED: AgentState.WRAP_UP,
        CallState.FAILED: AgentState.AVAILABLE,
        CallState.CANCELLED: AgentState.AVAILABLE,
    }
    return mapping.get(call_state)
