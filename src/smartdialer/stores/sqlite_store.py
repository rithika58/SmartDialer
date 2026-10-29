"""
SQLite-backed store.

WHY SQLITE
----------
The hardest requirement in the assignment is: "assume two workers see the same
available agent at almost the same time. Both must not be able to reserve that
agent."

That is a compare-and-set problem, and SQLite gives it to us for free inside a
single statement:

    UPDATE agents
       SET state = 'RESERVED', lease_owner = ?, lease_expires_at = ?
     WHERE agent_id = ?
       AND state = 'AVAILABLE'

`cursor.rowcount == 1` means *we* won the agent; 0 means somebody else did.
There is no read-then-write window, so there is no race -- not in one process,
not across processes, not across machines that share the file.

We run in WAL mode so readers never block the writer, with `BEGIN IMMEDIATE`
around multi-statement units of work so a worker never sees a half-applied
reservation.

WHAT THIS MAKES HARDER (stated honestly for the ADR)
----------------------------------------------------
* One writer at a time.  At ~10k agents doing ~1k state writes/sec, the single
  writer serialisation point becomes the bottleneck long before CPU does.
  See docs/ARCHITECTURE.md for the migration path.
* No cross-machine access without moving to a client/server database.
* Busy-timeouts under contention need tuning (we set 5s and retry).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Sequence

from ..domain.models import Agent, Borrower, Call, ProviderEvent
from ..domain.states import (
    AgentState,
    CallState,
    PROVIDER_EVENT_TO_CALL_STATE,
    RejectReason,
    evaluate_transition,
)
from .base import ClaimResult, DialerStore, EventApplyResult, LeaseReclaimReport

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS agents (
    agent_id          TEXT PRIMARY KEY,
    campaign_id       TEXT NOT NULL,
    state             TEXT NOT NULL,
    state_since       REAL NOT NULL,
    lease_owner       TEXT,
    lease_expires_at  REAL,
    current_call_id   TEXT,
    expected_free_at  REAL,
    version           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agents_claim
    ON agents (campaign_id, state, state_since);

CREATE TABLE IF NOT EXISTS borrowers (
    borrower_id       TEXT PRIMARY KEY,
    campaign_id       TEXT NOT NULL,
    phone             TEXT NOT NULL,
    priority          INTEGER NOT NULL DEFAULT 0,
    attempts          INTEGER NOT NULL DEFAULT 0,
    state             TEXT NOT NULL,
    last_attempt_at   REAL,
    lease_owner       TEXT,
    lease_expires_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_borrowers_claim
    ON borrowers (campaign_id, state, priority DESC);

CREATE TABLE IF NOT EXISTS calls (
    call_id           TEXT PRIMARY KEY,
    borrower_id       TEXT NOT NULL,
    agent_id          TEXT,          -- NULL until an agent is bound (predictive mode)
    campaign_id       TEXT NOT NULL,
    state             TEXT NOT NULL,
    provider          TEXT,
    provider_call_id  TEXT,
    last_seq          INTEGER NOT NULL DEFAULT 0,
    attempt           INTEGER NOT NULL DEFAULT 1,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    initiated_at      REAL,
    answered_at       REAL,
    connected_at      REAL,
    ended_at          REAL,
    outcome           TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_campaign_state
    ON calls (campaign_id, state);

-- Idempotency ledger.  The PRIMARY KEY is what makes a redelivered provider
-- event a no-op: the INSERT fails, we notice, we do not transition twice.
CREATE TABLE IF NOT EXISTS processed_events (
    event_id     TEXT PRIMARY KEY,
    call_id      TEXT,
    event_type   TEXT,
    result       TEXT,
    received_at  REAL
);

CREATE TABLE IF NOT EXISTS call_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      TEXT NOT NULL,
    from_state   TEXT NOT NULL,
    to_state     TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    event_id     TEXT,
    seq          INTEGER,
    at           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT NOT NULL,
    from_state   TEXT NOT NULL,
    to_state     TEXT NOT NULL,
    at           REAL NOT NULL
);
"""

_MAX_RETRIES = 25


class SQLiteStore(DialerStore):
    def __init__(self, path: str = ":memory:", timeout: float = 5.0) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, timeout=timeout, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA busy_timeout=5000")

    # ------------------------------------------------------------- plumbing
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _row_to_agent(self, row: sqlite3.Row) -> Agent:
        return Agent(
            agent_id=row["agent_id"],
            campaign_id=row["campaign_id"],
            state=AgentState(row["state"]),
            state_since=row["state_since"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            current_call_id=row["current_call_id"],
            expected_free_at=row["expected_free_at"],
            version=row["version"],
        )

    def _row_to_borrower(self, row: sqlite3.Row) -> Borrower:
        return Borrower(
            borrower_id=row["borrower_id"],
            campaign_id=row["campaign_id"],
            phone=row["phone"],
            priority=row["priority"],
            attempts=row["attempts"],
            state=row["state"],
            last_attempt_at=row["last_attempt_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
        )

    def _row_to_call(self, row: sqlite3.Row) -> Call:
        return Call(
            call_id=row["call_id"],
            borrower_id=row["borrower_id"],
            agent_id=row["agent_id"],
            campaign_id=row["campaign_id"],
            state=CallState(row["state"]),
            provider=row["provider"],
            provider_call_id=row["provider_call_id"],
            last_seq=row["last_seq"],
            attempt=row["attempt"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            initiated_at=row["initiated_at"],
            answered_at=row["answered_at"],
            connected_at=row["connected_at"],
            ended_at=row["ended_at"],
            outcome=row["outcome"],
        )

    # ---------------------------------------------------------------- agents
    def add_agent(self, agent: Agent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO agents VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    agent.agent_id, agent.campaign_id, agent.state.value,
                    agent.state_since, agent.lease_owner, agent.lease_expires_at,
                    agent.current_call_id, agent.expected_free_at, agent.version,
                ),
            )

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
            return self._row_to_agent(row) if row else None

    def claim_agent(
        self, campaign_id: str, worker_id: str, now: float, lease_seconds: float
    ) -> ClaimResult:
        """The single most important method in the system.

        One statement, guarded by `AND state = 'AVAILABLE'`.  If two workers
        execute it at the same instant, SQLite serialises them and the second
        one's WHERE clause no longer matches, so rowcount is 0 and it moves on
        to the next agent.
        """
        lease_expires = now + lease_seconds
        for _ in range(_MAX_RETRIES):
            with self._lock:
                cur = self._conn.execute(
                    """
                    UPDATE agents
                       SET state            = ?,
                           state_since      = ?,
                           lease_owner      = ?,
                           lease_expires_at = ?,
                           version          = version + 1
                     WHERE agent_id = (
                           SELECT agent_id FROM agents
                            WHERE campaign_id = ? AND state = ?
                            ORDER BY state_since ASC
                            LIMIT 1
                       )
                       AND state = ?
                    """,
                    (
                        AgentState.RESERVED.value, now, worker_id, lease_expires,
                        campaign_id, AgentState.AVAILABLE.value,
                        AgentState.AVAILABLE.value,
                    ),
                )
                if cur.rowcount == 1:
                    row = self._conn.execute(
                        "SELECT * FROM agents WHERE lease_owner=? AND lease_expires_at=? "
                        "AND state=? ORDER BY rowid DESC LIMIT 1",
                        (worker_id, lease_expires, AgentState.RESERVED.value),
                    ).fetchone()
                    if row is not None:
                        return ClaimResult(True, self._row_to_agent(row), "reserved")
                # rowcount 0 == somebody else took that agent between the
                # subselect and the update; loop and try the next one.
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
            sql = "UPDATE agents SET state=?, state_since=?, version=version+1"
            params: list = [target.value, now]

            if call_id is not None:
                sql += ", current_call_id=?"
                params.append(call_id)
            if target in (AgentState.AVAILABLE, AgentState.OFFLINE, AgentState.PAUSED):
                sql += ", lease_owner=NULL, lease_expires_at=NULL, current_call_id=NULL, expected_free_at=NULL"
            if lease_seconds is not None and worker_id is not None:
                sql += ", lease_owner=?, lease_expires_at=?"
                params.extend([worker_id, now + lease_seconds])
            if expected_free_at is not None:
                sql += ", expected_free_at=?"
                params.append(expected_free_at)

            sql += " WHERE agent_id=?"
            params.append(agent_id)

            if expected_states is not None:
                placeholders = ",".join("?" * len(expected_states))
                sql += f" AND state IN ({placeholders})"
                params.extend([s.value for s in expected_states])
            if worker_id is not None and target not in (
                AgentState.AVAILABLE, AgentState.OFFLINE, AgentState.PAUSED
            ):
                sql += " AND (lease_owner IS NULL OR lease_owner=?)"
                params.append(worker_id)

            cur = self._conn.execute(
                "INSERT INTO agent_history (agent_id, from_state, to_state, at) "
                "SELECT agent_id, state, ?, ? FROM agents WHERE agent_id=?",
                (target.value, now, agent_id),
            )
            cur = self._conn.execute(sql, params)
            return cur.rowcount == 1

    def heartbeat_agent(
        self, agent_id: str, worker_id: str, now: float, lease_seconds: float
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE agents SET lease_expires_at=? WHERE agent_id=? "
                "AND (lease_owner IS NULL OR lease_owner=?)",
                (now + lease_seconds, agent_id, worker_id),
            )
            return cur.rowcount == 1

    def count_agents_by_state(self, campaign_id: str) -> Dict[AgentState, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM agents WHERE campaign_id=? GROUP BY state",
                (campaign_id,),
            ).fetchall()
            out = {s: 0 for s in AgentState}
            for r in rows:
                out[AgentState(r["state"])] = r["n"]
            return out

    def list_agents(self, campaign_id: str) -> List[Agent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agents WHERE campaign_id=?", (campaign_id,)
            ).fetchall()
            return [self._row_to_agent(r) for r in rows]

    def agents_free_within(self, campaign_id: str, horizon: float, now: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM agents WHERE campaign_id=? "
                "AND state IN (?,?) AND expected_free_at IS NOT NULL "
                "AND expected_free_at <= ?",
                (campaign_id, AgentState.CONNECTED.value,
                 AgentState.WRAP_UP.value, now + horizon),
            ).fetchone()
            return int(row["n"]) if row else 0

    def agents_busy_profile(self, campaign_id: str, horizon: float, now: float):
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, AVG(expected_free_at - ?) AS rem FROM agents "
                "WHERE campaign_id=? AND state IN (?,?) AND expected_free_at IS NOT NULL "
                "AND expected_free_at > ?",
                (now, campaign_id, AgentState.CONNECTED.value,
                 AgentState.WRAP_UP.value, now + horizon),
            ).fetchone()
            if row is None or row["n"] == 0:
                return 0, 0.0
            return int(row["n"]), float(row["rem"] or 0.0)

    # ------------------------------------------------------------- borrowers
    def add_borrower(self, borrower: Borrower) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO borrowers VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    borrower.borrower_id, borrower.campaign_id, borrower.phone,
                    borrower.priority, borrower.attempts, borrower.state,
                    borrower.last_attempt_at, borrower.lease_owner,
                    borrower.lease_expires_at,
                ),
            )

    def claim_borrower(
        self, campaign_id: str, worker_id: str, now: float, lease_seconds: float
    ) -> ClaimResult:
        for _ in range(_MAX_RETRIES):
            with self._lock:
                cur = self._conn.execute(
                    """
                    UPDATE borrowers
                       SET state='RESERVED', lease_owner=?, lease_expires_at=?
                     WHERE borrower_id = (
                           SELECT borrower_id FROM borrowers
                            WHERE campaign_id=? AND state='QUEUED'
                            ORDER BY priority DESC, attempts ASC, borrower_id ASC
                            LIMIT 1
                       )
                       AND state='QUEUED'
                    """,
                    (worker_id, now + lease_seconds, campaign_id),
                )
                if cur.rowcount == 1:
                    row = self._conn.execute(
                        "SELECT * FROM borrowers WHERE lease_owner=? AND state='RESERVED' "
                        "ORDER BY rowid DESC LIMIT 1",
                        (worker_id,),
                    ).fetchone()
                    if row is not None:
                        return ClaimResult(True, self._row_to_borrower(row), "reserved")
        return ClaimResult(False, None, "no queued borrower")

    def release_borrower(self, borrower_id: str, worker_id: str, now: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE borrowers SET state='QUEUED', lease_owner=NULL, lease_expires_at=NULL "
                "WHERE borrower_id=? AND (lease_owner IS NULL OR lease_owner=?)",
                (borrower_id, worker_id),
            )
            return cur.rowcount == 1

    def mark_borrower_contacted(self, borrower_id: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE borrowers SET state='CONTACTED', attempts=attempts+1, "
                "last_attempt_at=?, lease_owner=NULL, lease_expires_at=NULL "
                "WHERE borrower_id=?",
                (now, borrower_id),
            )

    def queued_borrowers(self, campaign_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM borrowers WHERE campaign_id=? AND state='QUEUED'",
                (campaign_id,),
            ).fetchone()
            return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ calls
    def create_call(self, call: Call) -> Call:
        with self._lock:
            self._conn.execute(
                "INSERT INTO calls (call_id, borrower_id, agent_id, campaign_id, state, "
                "provider, provider_call_id, last_seq, attempt, created_at, updated_at, "
                "initiated_at, answered_at, connected_at, ended_at, outcome) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    call.call_id, call.borrower_id, call.agent_id, call.campaign_id,
                    call.state.value, call.provider, call.provider_call_id,
                    call.last_seq, call.attempt, call.created_at, call.updated_at,
                    call.initiated_at, call.answered_at, call.connected_at,
                    call.ended_at, call.outcome,
                ),
            )
            return call

    def get_call(self, call_id: str) -> Optional[Call]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM calls WHERE call_id=?", (call_id,)
            ).fetchone()
            return self._row_to_call(row) if row else None

    def apply_event(self, event: ProviderEvent, now: float) -> EventApplyResult:
        """Apply one provider event, idempotently and in order.

        Order of checks matters:
          1. Have we seen this exact event_id?      -> DUPLICATE
          2. Do we know this call?                  -> UNKNOWN_CALL
          3. Is the transition legal / forward?     -> ILLEGAL, OUT_OF_ORDER,
                                                       STALE_SEQ, TERMINAL
          4. Otherwise apply.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    existing = self._conn.execute(
                        "SELECT result FROM processed_events WHERE event_id=?",
                        (event.event_id,),
                    ).fetchone()
                    if existing is not None:
                        self._conn.execute("COMMIT")
                        call = self.get_call(event.call_id)
                        return EventApplyResult(
                            RejectReason.DUPLICATE, call,
                            call.state if call else None, None,
                            f"event_id {event.event_id} already processed",
                        )

                    row = self._conn.execute(
                        "SELECT * FROM calls WHERE call_id=?", (event.call_id,)
                    ).fetchone()
                    if row is None:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO processed_events VALUES (?,?,?,?,?)",
                            (event.event_id, event.call_id, event.type,
                             RejectReason.UNKNOWN_CALL.value, now),
                        )
                        self._conn.execute("COMMIT")
                        return EventApplyResult(
                            RejectReason.UNKNOWN_CALL, None, None, None,
                            f"unknown call {event.call_id}",
                        )

                    call = self._row_to_call(row)
                    target = PROVIDER_EVENT_TO_CALL_STATE.get(event.type)
                    if target is None:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO processed_events VALUES (?,?,?,?,?)",
                            (event.event_id, event.call_id, event.type,
                             RejectReason.ILLEGAL.value, now),
                        )
                        self._conn.execute("COMMIT")
                        return EventApplyResult(
                            RejectReason.ILLEGAL, call, call.state, None,
                            f"unknown provider event type {event.type!r}",
                        )

                    verdict = evaluate_transition(
                        call.state, target, event.seq, call.last_seq
                    )
                    if not verdict.applied:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO processed_events VALUES (?,?,?,?,?)",
                            (event.event_id, event.call_id, event.type,
                             verdict.reason.value, now),
                        )
                        self._conn.execute("COMMIT")
                        return EventApplyResult(
                            verdict.reason, call, call.state, None, verdict.detail
                        )

                    from_state = call.state
                    ts_col = {
                        CallState.INITIATED: "initiated_at",
                        CallState.ANSWERED: "answered_at",
                        CallState.CONNECTED: "connected_at",
                        CallState.COMPLETED: "ended_at",
                        CallState.FAILED: "ended_at",
                        CallState.CANCELLED: "ended_at",
                    }.get(target)

                    sql = "UPDATE calls SET state=?, updated_at=?, last_seq=MAX(last_seq,?)"
                    params: list = [target.value, now, event.seq or 0]
                    if ts_col:
                        sql += f", {ts_col}=?"
                        params.append(now)
                    if target in (CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED):
                        sql += ", outcome=?"
                        params.append(event.type)
                    sql += " WHERE call_id=? AND state=?"
                    params.extend([event.call_id, from_state.value])

                    cur = self._conn.execute(sql, params)
                    if cur.rowcount != 1:
                        # Lost a race with another worker applying a different
                        # event to this same call.  Roll back and let the caller
                        # retry with fresh state.
                        self._conn.execute("ROLLBACK")
                        return EventApplyResult(
                            RejectReason.STALE_SEQ, call, call.state, None,
                            "concurrent update; retry",
                        )

                    self._conn.execute(
                        "INSERT OR IGNORE INTO processed_events VALUES (?,?,?,?,?)",
                        (event.event_id, event.call_id, event.type,
                         RejectReason.APPLIED.value, now),
                    )
                    self._conn.execute(
                        "INSERT INTO call_history (call_id, from_state, to_state, "
                        "event_type, event_id, seq, at) VALUES (?,?,?,?,?,?,?)",
                        (event.call_id, from_state.value, target.value, event.type,
                         event.event_id, event.seq, now),
                    )
                    self._conn.execute("COMMIT")

                    updated = self.get_call(event.call_id)
                    return EventApplyResult(
                        RejectReason.APPLIED, updated, from_state, target, "ok"
                    )
                except Exception:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            except sqlite3.OperationalError as exc:
                # "database is locked" under heavy contention -- the caller's
                # retry policy handles it.
                return EventApplyResult(
                    RejectReason.STALE_SEQ, None, None, None, f"sqlite busy: {exc}"
                )

    def list_calls(self, campaign_id: str) -> List[Call]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM calls WHERE campaign_id=?", (campaign_id,)
            ).fetchall()
            return [self._row_to_call(r) for r in rows]

    def calls_in_states(self, campaign_id: str, states: Sequence[CallState]) -> List[Call]:
        if not states:
            return []
        placeholders = ",".join("?" * len(states))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM calls WHERE campaign_id=? AND state IN ({placeholders})",
                (campaign_id, *[s.value for s in states]),
            ).fetchall()
            return [self._row_to_call(r) for r in rows]

    # -------------------------------------------------------------- recovery
    def expire_leases(self, now: float) -> LeaseReclaimReport:
        """Reclaim everything a dead worker was holding.

        Detection is lease expiry, not failure detection: we do not try to
        notice that a worker died, we simply stop trusting it once it stops
        renewing.  That is the only approach that survives a worker being
        SIGKILLed, a VM disappearing, or a network partition.
        """
        report = LeaseReclaimReport()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT agent_id, current_call_id FROM agents "
                    "WHERE state IN (?,?) AND lease_expires_at IS NOT NULL "
                    "AND lease_expires_at <= ?",
                    (AgentState.RESERVED.value, AgentState.DIALING.value, now),
                ).fetchall()
                for r in rows:
                    report.agents_released.append(r["agent_id"])
                    if r["current_call_id"]:
                        self._conn.execute(
                            "UPDATE calls SET state=?, ended_at=?, outcome=? "
                            "WHERE call_id=? AND state IN (?,?,?)",
                            (
                                CallState.CANCELLED.value, now, "WORKER_CRASH",
                                r["current_call_id"], CallState.RESERVED.value,
                                CallState.INITIATED.value, CallState.RINGING.value,
                            ),
                        )
                        report.calls_cancelled.append(r["current_call_id"])

                self._conn.execute(
                    "UPDATE agents SET state=?, lease_owner=NULL, lease_expires_at=NULL, "
                    "current_call_id=NULL, expected_free_at=NULL, version=version+1 "
                    "WHERE state IN (?,?) AND lease_expires_at IS NOT NULL "
                    "AND lease_expires_at <= ?",
                    (AgentState.AVAILABLE.value, AgentState.RESERVED.value,
                     AgentState.DIALING.value, now),
                )

                rows = self._conn.execute(
                    "SELECT borrower_id FROM borrowers WHERE state='RESERVED' "
                    "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
                    (now,),
                ).fetchall()
                for r in rows:
                    report.borrowers_released.append(r["borrower_id"])
                self._conn.execute(
                    "UPDATE borrowers SET state='QUEUED', lease_owner=NULL, "
                    "lease_expires_at=NULL WHERE state='RESERVED' "
                    "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
                    (now,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return report
