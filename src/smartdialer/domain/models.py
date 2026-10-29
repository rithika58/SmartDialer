"""Plain data structures shared by every layer.

Nothing in here knows about SQLite, threads, or telecom.  That keeps the
domain testable and keeps the store implementations swappable.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .states import AgentState, CallState


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class Agent:
    agent_id: str
    campaign_id: str
    state: AgentState = AgentState.OFFLINE
    state_since: float = field(default_factory=time.time)
    # Lease: who owns this reservation and until when.  If the owner dies the
    # lease expires and the recovery loop reclaims the agent.  This is the
    # entire mechanism behind "agent disappears during call setup".
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[float] = None
    current_call_id: Optional[str] = None
    # When the pacing engine estimates this agent will be free again.  Set when
    # the agent goes CONNECTED; used to compute "agents becoming free soon".
    expected_free_at: Optional[float] = None
    version: int = 0

    def is_available(self) -> bool:
        return self.state == AgentState.AVAILABLE

    def lease_active(self, now: float) -> bool:
        return (
            self.lease_owner is not None
            and self.lease_expires_at is not None
            and self.lease_expires_at > now
        )


@dataclass
class Borrower:
    borrower_id: str
    campaign_id: str
    phone: str
    priority: int = 0
    attempts: int = 0
    state: str = "QUEUED"           # QUEUED | RESERVED | CONTACTED | CLOSED
    last_attempt_at: Optional[float] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[float] = None


@dataclass
class Call:
    call_id: str
    borrower_id: str
    campaign_id: str
    # Predictive mode dials before an agent is bound, so this stays None until
    # the borrower actually answers (see DialerWorker._bind_agent_on_answer).
    agent_id: Optional[str] = None
    state: CallState = CallState.RESERVED
    provider: Optional[str] = None
    provider_call_id: Optional[str] = None
    # Highest provider sequence number already applied.  Guards against
    # out-of-order delivery.
    last_seq: int = 0
    attempt: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    initiated_at: Optional[float] = None
    answered_at: Optional[float] = None
    connected_at: Optional[float] = None
    ended_at: Optional[float] = None
    outcome: Optional[str] = None
    abandoned: bool = False

    def is_terminal(self) -> bool:
        return self.state in (
            CallState.COMPLETED,
            CallState.FAILED,
            CallState.CANCELLED,
        )


@dataclass
class ProviderEvent:
    """A single event emitted by a telecom provider.

    event_id is the provider's idempotency key -- the SAME event redelivered
    carries the SAME event_id.
    seq is the provider's ordering key for that call.
    """
    event_id: str
    call_id: str
    seq: int
    type: str
    at: float = field(default_factory=time.time)
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlaceCallRequest:
    call_id: str
    phone: str
    campaign_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlaceCallResult:
    accepted: bool
    provider_call_id: Optional[str] = None
    error: Optional[str] = None


class ProviderError(Exception):
    """Base class for provider failures."""


class ProviderTimeout(ProviderError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class ProviderRejected(ProviderError):
    pass
