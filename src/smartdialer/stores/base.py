"""The store contract.

Two implementations exist:

* MemoryStore  -- used by the simulator and by fast unit tests.
* SQLiteStore  -- used by the concurrency tests and by the real worker.

They must behave identically.  The concurrency tests run the SAME race against
both, which is how we prove that the atomicity guarantee comes from the data
model and not from the CPython GIL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..domain.models import Agent, Borrower, Call, ProviderEvent
from ..domain.states import AgentState, CallState, RejectReason


@dataclass
class ClaimResult:
    ok: bool
    obj: Optional[object] = None
    reason: str = ""


@dataclass
class EventApplyResult:
    status: RejectReason
    call: Optional[Call] = None
    from_state: Optional[CallState] = None
    to_state: Optional[CallState] = None
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.status == RejectReason.APPLIED

    @property
    def connected_human(self) -> bool:
        """True when this event is the one that put a live borrower on the line."""
        return (
            self.applied
            and self.to_state in (CallState.ANSWERED, CallState.CONNECTED)
        )


@dataclass
class LeaseReclaimReport:
    """What the recovery loop found and fixed after workers died."""
    agents_released: List[str] = field(default_factory=list)
    borrowers_released: List[str] = field(default_factory=list)
    calls_cancelled: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "agents_released": len(self.agents_released),
            "borrowers_released": len(self.borrowers_released),
            "calls_cancelled": len(self.calls_cancelled),
        }


class DialerStore:
    """Interface every store implements."""

    # ---------------------------------------------------------------- agents
    def add_agent(self, agent: Agent) -> None:
        raise NotImplementedError

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        raise NotImplementedError

    def claim_agent(
        self, campaign_id: str, worker_id: str, now: float, lease_seconds: float
    ) -> ClaimResult:
        """Atomically move one AVAILABLE agent to RESERVED.

        Exactly one concurrent caller may win for a given agent.  This single
        method is the answer to "two workers see the same available agent".
        """
        raise NotImplementedError

    def set_agent_state(
        self,
        agent_id: str,
        target: AgentState,
        now: float,
        expected_states: Optional[Sequence[AgentState]] = None,
        worker_id: Optional[str] = None,
        lease_seconds: Optional[float] = None,
        call_id: Optional[str] = None,
        expected_free_at: Optional[float] = None,
    ) -> bool:
        """Conditional (compare-and-set) agent transition.

        When `expected_states` is given, the update only succeeds if the agent
        is currently in one of them -- this is what prevents a late provider
        event from resurrecting a reallocated agent.
        """
        raise NotImplementedError

    def heartbeat_agent(
        self, agent_id: str, worker_id: str, now: float, lease_seconds: float
    ) -> bool:
        raise NotImplementedError

    def count_agents_by_state(self, campaign_id: str) -> Dict[AgentState, int]:
        raise NotImplementedError

    def list_agents(self, campaign_id: str) -> List[Agent]:
        raise NotImplementedError

    def agents_free_within(self, campaign_id: str, horizon: float, now: float) -> int:
        """Agents currently busy but expected to be free within `horizon`."""
        raise NotImplementedError

    def agents_busy_profile(self, campaign_id: str, horizon: float, now: float):
        """(count, avg_remaining_seconds) for busy agents NOT expected to free
        within the horizon.  They might still finish early, which is the random
        component of agent availability."""
        raise NotImplementedError

    # ------------------------------------------------------------- borrowers
    def add_borrower(self, borrower: Borrower) -> None:
        raise NotImplementedError

    def claim_borrower(
        self, campaign_id: str, worker_id: str, now: float, lease_seconds: float
    ) -> ClaimResult:
        raise NotImplementedError

    def release_borrower(self, borrower_id: str, worker_id: str, now: float) -> bool:
        raise NotImplementedError

    def mark_borrower_contacted(self, borrower_id: str, now: float) -> None:
        raise NotImplementedError

    def queued_borrowers(self, campaign_id: str) -> int:
        raise NotImplementedError

    # ------------------------------------------------------------------ calls
    def create_call(self, call: Call) -> Call:
        raise NotImplementedError

    def get_call(self, call_id: str) -> Optional[Call]:
        raise NotImplementedError

    def apply_event(self, event: ProviderEvent, now: float) -> EventApplyResult:
        """Idempotent, order-aware application of one provider event."""
        raise NotImplementedError

    def list_calls(self, campaign_id: str) -> List[Call]:
        raise NotImplementedError

    def calls_in_states(self, campaign_id: str, states: Sequence[CallState]) -> List[Call]:
        raise NotImplementedError

    # -------------------------------------------------------------- recovery
    def expire_leases(self, now: float) -> LeaseReclaimReport:
        raise NotImplementedError
