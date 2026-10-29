"""In-memory store.  Used by the simulator and by fast tests.

The locking here is a plain mutex, which is honest: in-memory state is only
ever shared between threads inside one process.  The *atomicity* guarantee we
care about (compare-and-set on agent state) is implemented identically here and
in SQLiteStore.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Sequence

from ..domain.models import Agent, Borrower, Call, ProviderEvent
from ..domain.states import (
    AGENT_BUSY_STATES,
    AgentState,
    CallState,
    PROVIDER_EVENT_TO_CALL_STATE,
    RejectReason,
    evaluate_transition,
)
from .base import ClaimResult, DialerStore, EventApplyResult, LeaseReclaimReport


class MemoryStore(DialerStore):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.agents: Dict[str, Agent] = {}
        self.borrowers: Dict[str, Borrower] = {}
        self.calls: Dict[str, Call] = {}
        self._seen_events: Dict[str, float] = {}
        self.history: List[dict] = []

    # ---------------------------------------------------------------- agents
    def add_agent(self, agent: Agent) -> None:
        with self._lock:
            self.agents[agent.agent_id] = agent

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        with self._lock:
            return self.agents.get(agent_id)

    def claim_agent(
        self, campaign_id: str, worker_id: str, now: float, lease_seconds: float
    ) -> ClaimResult:
        with self._lock:
            for agent in self.agents.values():
                if agent.campaign_id != campaign_id:
                    continue
                if agent.state != AgentState.AVAILABLE:
                    continue
                # compare-and-set: the state check and the write are atomic
                # because they happen inside the lock, in one step.
                agent.state = AgentState.RESERVED
                agent.state_since = now
                agent.lease_owner = worker_id
                agent.lease_expires_at = now + lease_seconds
                agent.version += 1
                return ClaimResult(True, agent, "reserved")
            return ClaimResult(False, None, "no agent available")

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
        with self._lock:
            agent = self.agents.get(agent_id)
            if agent is None:
                return False
            if expected_states is not None and agent.state not in expected_states:
                return False
            if worker_id is not None and agent.lease_owner not in (None, worker_id):
                return False

            agent.state = target
            agent.state_since = now
            agent.version += 1
            if call_id is not None:
                agent.current_call_id = call_id
            if target == AgentState.AVAILABLE:
                agent.lease_owner = None
                agent.lease_expires_at = None
                agent.current_call_id = None
                agent.expected_free_at = None
            if target in (AgentState.OFFLINE, AgentState.PAUSED):
                agent.lease_owner = None
                agent.lease_expires_at = None
                agent.current_call_id = None
                agent.expected_free_at = None
            if lease_seconds is not None and worker_id is not None:
                agent.lease_owner = worker_id
                agent.lease_expires_at = now + lease_seconds
            if expected_free_at is not None:
                agent.expected_free_at = expected_free_at
            return True

    def heartbeat_agent(
        self, agent_id: str, worker_id: str, now: float, lease_seconds: float
    ) -> bool:
        with self._lock:
            agent = self.agents.get(agent_id)
            if agent is None:
                return False
            if agent.lease_owner is not None and agent.lease_owner != worker_id:
                return False
            agent.lease_expires_at = now + lease_seconds
            return True

    def count_agents_by_state(self, campaign_id: str) -> Dict[AgentState, int]:
        with self._lock:
            out: Dict[AgentState, int] = {s: 0 for s in AgentState}
            for agent in self.agents.values():
                if agent.campaign_id == campaign_id:
                    out[agent.state] += 1
            return out

    def list_agents(self, campaign_id: str) -> List[Agent]:
        with self._lock:
            return [a for a in self.agents.values() if a.campaign_id == campaign_id]

    def agents_free_within(self, campaign_id: str, horizon: float, now: float) -> int:
        with self._lock:
            n = 0
            for agent in self.agents.values():
                if agent.campaign_id != campaign_id:
                    continue
                if agent.state not in (AgentState.CONNECTED, AgentState.WRAP_UP):
                    continue
                if agent.expected_free_at is not None and agent.expected_free_at <= now + horizon:
                    n += 1
            return n

    def agents_busy_profile(self, campaign_id: str, horizon: float, now: float):
        with self._lock:
            n = 0
            total = 0.0
            for a in self.agents.values():
                if a.campaign_id != campaign_id:
                    continue
                if a.state not in (AgentState.CONNECTED, AgentState.WRAP_UP):
                    continue
                if a.expected_free_at is None or a.expected_free_at <= now + horizon:
                    continue
                n += 1
                total += a.expected_free_at - now
            return n, (total / n if n else 0.0)

    # ------------------------------------------------------------- borrowers
    def add_borrower(self, borrower: Borrower) -> None:
        with self._lock:
            self.borrowers[borrower.borrower_id] = borrower

    def claim_borrower(
        self, campaign_id: str, worker_id: str, now: float, lease_seconds: float
    ) -> ClaimResult:
        with self._lock:
            best: Optional[Borrower] = None
            for b in self.borrowers.values():
                if b.campaign_id != campaign_id or b.state != "QUEUED":
                    continue
                if best is None or b.priority > best.priority:
                    best = b
            if best is None:
                return ClaimResult(False, None, "no queued borrower")
            best.state = "RESERVED"
            best.lease_owner = worker_id
            best.lease_expires_at = now + lease_seconds
            return ClaimResult(True, best, "reserved")

    def release_borrower(self, borrower_id: str, worker_id: str, now: float) -> bool:
        with self._lock:
            b = self.borrowers.get(borrower_id)
            if b is None:
                return False
            b.state = "QUEUED"
            b.lease_owner = None
            b.lease_expires_at = None
            return True

    def mark_borrower_contacted(self, borrower_id: str, now: float) -> None:
        with self._lock:
            b = self.borrowers.get(borrower_id)
            if b is None:
                return
            b.state = "CONTACTED"
            b.attempts += 1
            b.last_attempt_at = now
            b.lease_owner = None
            b.lease_expires_at = None

    def queued_borrowers(self, campaign_id: str) -> int:
        with self._lock:
            return sum(
                1
                for b in self.borrowers.values()
                if b.campaign_id == campaign_id and b.state == "QUEUED"
            )

    # ------------------------------------------------------------------ calls
    def create_call(self, call: Call) -> Call:
        with self._lock:
            self.calls[call.call_id] = call
            return call

    def get_call(self, call_id: str) -> Optional[Call]:
        with self._lock:
            return self.calls.get(call_id)

    def apply_event(self, event: ProviderEvent, now: float) -> EventApplyResult:
        with self._lock:
            # 1. idempotency: same event_id seen before -> no second transition
            if event.event_id in self._seen_events:
                call = self.calls.get(event.call_id)
                return EventApplyResult(
                    RejectReason.DUPLICATE,
                    call,
                    call.state if call else None,
                    None,
                    f"event_id {event.event_id} already processed",
                )

            call = self.calls.get(event.call_id)
            if call is None:
                return EventApplyResult(
                    RejectReason.UNKNOWN_CALL, None, None, None,
                    f"unknown call {event.call_id}",
                )

            target = PROVIDER_EVENT_TO_CALL_STATE.get(event.type)
            if target is None:
                self._seen_events[event.event_id] = now
                return EventApplyResult(
                    RejectReason.ILLEGAL, call, call.state, None,
                    f"unknown provider event type {event.type!r}",
                )

            verdict = evaluate_transition(call.state, target, event.seq, call.last_seq)
            if not verdict.applied:
                # Still record the event id: a rejected event that is redelivered
                # must stay rejected rather than becoming a duplicate we retry.
                self._seen_events[event.event_id] = now
                return EventApplyResult(
                    verdict.reason, call, call.state, None, verdict.detail
                )

            from_state = call.state
            call.state = target
            call.updated_at = now
            call.last_seq = max(call.last_seq, event.seq or 0)
            self._seen_events[event.event_id] = now

            if target == CallState.INITIATED:
                call.initiated_at = now
            elif target == CallState.ANSWERED:
                call.answered_at = now
            elif target == CallState.CONNECTED:
                call.connected_at = now
            elif target in (CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED):
                call.ended_at = now
                call.outcome = event.type

            self.history.append(
                {
                    "call_id": call.call_id,
                    "from": from_state.value,
                    "to": target.value,
                    "event": event.type,
                    "event_id": event.event_id,
                    "seq": event.seq,
                    "at": now,
                }
            )
            return EventApplyResult(RejectReason.APPLIED, call, from_state, target, "ok")

    def list_calls(self, campaign_id: str) -> List[Call]:
        with self._lock:
            return [c for c in self.calls.values() if c.campaign_id == campaign_id]

    def calls_in_states(self, campaign_id: str, states: Sequence[CallState]) -> List[Call]:
        wanted = set(states)
        with self._lock:
            return [
                c
                for c in self.calls.values()
                if c.campaign_id == campaign_id and c.state in wanted
            ]

    # -------------------------------------------------------------- recovery
    def expire_leases(self, now: float) -> LeaseReclaimReport:
        report = LeaseReclaimReport()
        with self._lock:
            for agent in self.agents.values():
                if agent.state in (AgentState.RESERVED, AgentState.DIALING):
                    if agent.lease_expires_at is not None and agent.lease_expires_at <= now:
                        agent.state = AgentState.AVAILABLE
                        agent.lease_owner = None
                        agent.lease_expires_at = None
                        agent.current_call_id = None
                        agent.expected_free_at = None
                        agent.version += 1
                        report.agents_released.append(agent.agent_id)

            for b in self.borrowers.values():
                if b.state == "RESERVED" and b.lease_expires_at is not None:
                    if b.lease_expires_at <= now:
                        b.state = "QUEUED"
                        b.lease_owner = None
                        b.lease_expires_at = None
                        report.borrowers_released.append(b.borrower_id)

            for call in self.calls.values():
                if call.state in (CallState.RESERVED, CallState.INITIATED) and call.agent_id:
                    agent = self.agents.get(call.agent_id)
                    agent_free = agent is not None and agent.state == AgentState.AVAILABLE
                    if agent_free and call.agent_id in report.agents_released:
                        call.state = CallState.CANCELLED
                        call.ended_at = now
                        call.outcome = "WORKER_CRASH"
                        report.calls_cancelled.append(call.call_id)
        return report
