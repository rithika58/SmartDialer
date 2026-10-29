"""The two-workers-one-agent problem.

This is the single correctness claim the assignment cares most about, so it is
tested against BOTH store implementations, with real OS threads, with more
workers than agents, and with more attempts than agents.

The point of running it against MemoryStore as well is to show the guarantee
comes from the compare-and-set data model and not from SQLite's locking.
"""

import threading
import tempfile
import os

import pytest

from smartdialer.domain.models import Agent, Borrower
from smartdialer.domain.states import AgentState
from smartdialer.stores.memory import MemoryStore
from smartdialer.stores.sqlite_store import SQLiteStore


def _make_store(cls, agents=50):
    if cls is SQLiteStore:
        path = tempfile.mktemp(suffix=".db")
        store = SQLiteStore(path)
        store._tmp_path = path
    else:
        store = MemoryStore()
    for i in range(agents):
        store.add_agent(
            Agent(agent_id=f"A{i:03d}", campaign_id="C1",
                  state=AgentState.AVAILABLE, state_since=0.0)
        )
    return store


ALL_STORES = [MemoryStore, SQLiteStore]


@pytest.mark.parametrize("cls", ALL_STORES, ids=["memory", "sqlite"])
def test_no_agent_is_ever_allocated_twice(cls):
    store = _make_store(cls, agents=50)
    workers, attempts_each = 12, 25
    won = []
    lock = threading.Lock()
    barrier = threading.Barrier(workers)

    def worker(wid):
        barrier.wait()  # maximise the chance they collide
        for _ in range(attempts_each):
            res = store.claim_agent("C1", wid, 0.0, 30.0)
            if res.ok:
                with lock:
                    won.append((wid, res.obj.agent_id))

    threads = [threading.Thread(target=worker, args=(f"W{i}",)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    agent_ids = [a for _, a in won]

    # The core invariant.
    assert len(agent_ids) == len(set(agent_ids)), "an agent was allocated twice"

    # And we did not lose or invent any agents.
    assert len(agent_ids) == 50
    counts = store.count_agents_by_state("C1")
    assert counts[AgentState.RESERVED] == 50
    assert counts[AgentState.AVAILABLE] == 0

    if hasattr(store, "_tmp_path"):
        store.close()
        os.unlink(store._tmp_path)


@pytest.mark.parametrize("cls", ALL_STORES, ids=["memory", "sqlite"])
def test_claim_is_atomic_even_when_contention_is_extreme(cls):
    """1 worker per agent, all starting on the same barrier."""
    store = _make_store(cls, agents=40)
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(40)

    def worker(wid):
        barrier.wait()
        res = store.claim_agent("C1", wid, 0.0, 30.0)
        with lock:
            results.append(res)

    threads = [threading.Thread(target=worker, args=(f"W{i}",)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [r for r in results if r.ok]
    assert len(ok) == 40
    assert len({r.obj.agent_id for r in ok}) == 40

    if hasattr(store, "_tmp_path"):
        store.close()
        os.unlink(store._tmp_path)


def test_borrowers_are_not_double_dialed():
    store = MemoryStore()
    for i in range(30):
        store.add_borrower(
            Borrower(borrower_id=f"B{i:03d}", campaign_id="C1", phone=f"+{i}")
        )
    won = []
    lock = threading.Lock()
    barrier = threading.Barrier(10)

    def worker(wid):
        barrier.wait()
        for _ in range(10):
            res = store.claim_borrower("C1", wid, 0.0, 30.0)
            if res.ok:
                with lock:
                    won.append(res.obj.borrower_id)

    threads = [threading.Thread(target=worker, args=(f"W{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(won) == len(set(won))


def test_conditional_agent_transition_rejects_stale_writers():
    """A worker that lost its agent must not be able to move it afterwards."""
    store = MemoryStore()
    store.add_agent(Agent(agent_id="A1", campaign_id="C1", state=AgentState.AVAILABLE))

    assert store.claim_agent("C1", "W1", 0.0, 30.0).ok
    # W2 never got the agent, so its conditional write must fail.
    assert not store.set_agent_state(
        "A1", AgentState.DIALING, 1.0,
        expected_states=[AgentState.RESERVED], worker_id="W2",
    )
    # And W1's write succeeds.
    assert store.set_agent_state(
        "A1", AgentState.DIALING, 1.0,
        expected_states=[AgentState.RESERVED], worker_id="W1",
    )
    assert store.get_agent("A1").state == AgentState.DIALING


def test_expired_lease_is_reclaimable_by_someone_else():
    store = MemoryStore()
    store.add_agent(Agent(agent_id="A1", campaign_id="C1", state=AgentState.AVAILABLE))
    store.claim_agent("C1", "W1", t0 := 0.0, 30.0)

    # Worker W1 died.  Before the lease expires, nobody else may take the agent.
    assert not store.claim_agent("C1", "W2", 10.0, 30.0).ok

    report = store.expire_leases(31.0)
    assert "A1" in report.agents_released

    assert store.claim_agent("C1", "W2", 31.0, 30.0).ok
    assert store.get_agent("A1").lease_owner == "W2"
