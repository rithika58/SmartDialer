"""
Call Allocator + Provider Gateway.

The gateway is the ONLY object in the system that holds a telecom provider.
It exposes exactly one way to reach it, and that way requires a valid,
single-use, unexpired SafetyDecision.

    PacingEngine --(PacingDecision)--> SafetyController --(SafetyDecision)-->
        CallAllocator --> ProviderGateway --> TelecomProvider

The PacingEngine is never given the gateway.  It is not a matter of
discipline; the object simply is not in scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..domain.models import (
    Call,
    PlaceCallRequest,
    ProviderError,
    ProviderEvent,
    new_id,
)
from ..domain.states import AgentState, CallState
from ..providers.base import TelecomProvider
from .safety import SafetyController, SafetyDecision


@dataclass
class DialAttempt:
    call_id: str
    ok: bool
    stage: str                 # borrower | agent | provider | placed
    detail: str = ""
    provider: Optional[str] = None
    agent_id: Optional[str] = None
    borrower_id: Optional[str] = None


@dataclass
class DialBatchResult:
    attempts: List[DialAttempt] = field(default_factory=list)

    @property
    def placed(self) -> int:
        return sum(1 for a in self.attempts if a.stage == "placed" and a.ok)

    def by_stage(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for a in self.attempts:
            key = a.stage if a.ok else f"{a.stage}_failed"
            out[key] = out.get(key, 0) + 1
        return out


class ProviderGateway:
    """Owns the providers.  Verifies every approval.  Then, and only then, dials."""

    def __init__(
        self,
        providers: List[TelecomProvider],
        controller: SafetyController,
    ) -> None:
        self._providers = {p.name: p for p in providers}
        self._order = [p.name for p in providers]
        self._controller = controller
        self.place_attempts = 0
        self.refused = 0

    # The providers are deliberately not exposed as a public attribute.
    @property
    def provider_names(self) -> List[str]:
        return list(self._order)

    def health_snapshot(self) -> List[dict]:
        return [self._providers[n].health.snapshot() for n in self._order]

    def is_healthy(self) -> bool:
        return any(self._providers[n].health.is_healthy for n in self._order)

    def healthy_provider_name(self) -> Optional[str]:
        for n in self._order:
            if self._providers[n].health.is_healthy:
                return n
        return None

    def select_provider(self) -> Optional[TelecomProvider]:
        n = self.healthy_provider_name()
        return self._providers.get(n) if n else None

    def place_call(
        self,
        call: Call,
        phone: str,
        approval: SafetyDecision,
        now: float,
        campaign_id: str,
    ) -> tuple[bool, Optional[str], str]:
        """Reach a provider -- but only with a verified approval."""
        self.place_attempts += 1
        if not self._controller.verify(approval, campaign_id, now):
            self.refused += 1
            return False, None, "approval rejected (unsigned, forged, replayed or stale)"

        provider = self.select_provider()
        if provider is None:
            return False, None, "no healthy provider"

        if not provider.health.allow_request():
            return False, None, f"{provider.name}: circuit breaker open"

        try:
            result = provider.place_call(
                PlaceCallRequest(
                    call_id=call.call_id,
                    phone=phone,
                    campaign_id=call.campaign_id,
                    metadata={"campaign": call.campaign_id},
                )
            )
        except ProviderError as exc:
            return False, provider.name, f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # a provider must never take the dialer down
            return False, provider.name, f"unexpected: {exc}"

        if not result.accepted:
            return False, provider.name, result.error or "provider refused"

        call.provider = provider.name
        call.provider_call_id = result.provider_call_id
        return True, provider.name, "placed"


class CallAllocator:
    """Turns an approved number into real calls.

    Progressive mode: the agent is reserved BEFORE we dial, so a connected
    borrower always has somebody to talk to.  The cost is agent idle time
    while the phone rings.

    Predictive mode: we reserve only the borrower and dial ahead.  The agent is
    bound when the borrower answers (see DialerWorker.on_event).  That is where
    the utilization comes from, and it is also the only way an abandoned call
    can occur -- which is exactly why the Safety Controller caps how far ahead
    we may dial.
    """

    def __init__(
        self,
        store,
        gateway: ProviderGateway,
        controller: SafetyController,
        clock,
        lease_seconds: float = 30.0,
        worker_id: str = "worker-0",
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.controller = controller
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.worker_id = worker_id

    def dial(
        self, approval: SafetyDecision, campaign_id: str, reserve_agent: bool
    ) -> DialBatchResult:
        result = DialBatchResult()
        now = self.clock.now()

        for _ in range(max(0, approval.allowed_calls)):
            attempt = self._dial_one(approval, campaign_id, reserve_agent, now)
            result.attempts.append(attempt)
            if not attempt.ok and attempt.stage == "provider":
                # Provider is failing; stop burning borrowers.
                break
        return result

    def _dial_one(
        self,
        approval: SafetyDecision,
        campaign_id: str,
        reserve_agent: bool,
        now: float,
    ) -> DialAttempt:
        wid = self.worker_id

        # ---- 1. claim a borrower ------------------------------------------
        b = self.store.claim_borrower(campaign_id, wid, now, self.lease_seconds)
        if not b.ok:
            return DialAttempt("-", False, "borrower", "no queued borrower")

        borrower = b.obj
        agent = None

        # ---- 2. claim an agent (progressive only) --------------------------
        if reserve_agent:
            a = self.store.claim_agent(campaign_id, wid, now, self.lease_seconds)
            if not a.ok:
                # No agent -> put the borrower back.  Nobody was dialed, so
                # there is no compliance exposure, only a retry.
                self.store.release_borrower(borrower.borrower_id, wid, now)
                return DialAttempt("-", False, "agent", "no available agent",
                                   borrower_id=borrower.borrower_id)
            agent = a.obj

        # ---- 3. create the call record -------------------------------------
        call = Call(
            call_id=new_id("call"),
            borrower_id=borrower.borrower_id,
            agent_id=agent.agent_id if agent else None,
            campaign_id=campaign_id,
            state=CallState.RESERVED,
            created_at=now,
            updated_at=now,
        )
        self.store.create_call(call)

        # ---- 4. hand it to the gateway (approval verified inside) ----------
        ok, provider_name, detail = self.gateway.place_call(
            call, borrower.phone, approval, now, campaign_id
        )

        if not ok:
            # Unwind: release everything.  The call stays on record as FAILED so
            # the borrower is not silently re-dialed forever.
            if agent is not None:
                self.store.set_agent_state(
                    agent.agent_id, AgentState.AVAILABLE, now, worker_id=wid
                )
            self.store.release_borrower(borrower.borrower_id, wid, now)
            # Go through the state machine rather than poking the record, so
            # the transition is validated and lands in the audit history.
            self.store.apply_event(
                ProviderEvent(
                    event_id=new_id("evt"),
                    call_id=call.call_id,
                    seq=1,
                    type="FAILED",
                    at=now,
                ),
                now,
            )
            updated = self.store.get_call(call.call_id)
            if updated is not None:
                updated.outcome = "PLACE_FAILED"
            return DialAttempt(call.call_id, False, "provider", detail,
                               provider=provider_name, borrower_id=borrower.borrower_id)

        # ---- 5. record success ---------------------------------------------
        self.store.mark_borrower_contacted(borrower.borrower_id, now)
        if agent is not None:
            self.store.set_agent_state(
                agent.agent_id, AgentState.DIALING, now,
                expected_states=[AgentState.RESERVED],
                worker_id=wid, lease_seconds=self.lease_seconds,
                call_id=call.call_id,
            )
        return DialAttempt(
            call.call_id, True, "placed", detail,
            provider=provider_name,
            agent_id=agent.agent_id if agent else None,
            borrower_id=borrower.borrower_id,
        )
