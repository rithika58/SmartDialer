"""The five failure cases the assignment asks us to demonstrate."""

import pytest

from smartdialer.domain.models import Agent, Borrower, Call, ProviderEvent, new_id
from smartdialer.domain.states import AgentState, CallState
from smartdialer.stores.memory import MemoryStore
from smartdialer.stores.sqlite_store import SQLiteStore


# --------------------------------------------------------------------------- #
# 1. Worker crash
# --------------------------------------------------------------------------- #
def test_worker_crash_is_cleaned_up_by_lease_expiry():
    store = SQLiteStore(":memory:")
    store.add_agent(Agent(agent_id="A1", campaign_id="C1", state=AgentState.AVAILABLE))
    store.add_borrower(Borrower(borrower_id="B1", campaign_id="C1", phone="+1"))

    # Worker W1: reserve agent, reserve borrower, create the call, dial.
    assert store.claim_agent("C1", "W1", t0 := 100.0, 20.0).ok
    assert store.claim_borrower("C1", "W1", 100.0, 20.0).ok
    call = store.create_call(
        Call(call_id="CALL1", borrower_id="B1", agent_id="A1", campaign_id="C1")
    )
    store.set_agent_state("A1", AgentState.DIALING, 100.0,
                          expected_states=[AgentState.RESERVED],
                          worker_id="W1", lease_seconds=20.0, call_id="CALL1")

    # W1 is SIGKILLed here.  It never heartbeats again.

    # Before the lease expires the agent is still held -- we do not steal it
    # early, because the worker may just be slow.
    report = store.expire_leases(110.0)
    assert report.agents_released == []
    assert store.get_agent("A1").state == AgentState.DIALING

    # After the lease expires the agent comes back and the orphaned call dies.
    report = store.expire_leases(121.0)
    assert "A1" in report.agents_released
    assert store.get_agent("A1").state == AgentState.AVAILABLE
    assert store.get_agent("A1").lease_owner is None

    cancelled = store.get_call("CALL1")
    assert cancelled.state == CallState.CANCELLED
    assert cancelled.outcome == "WORKER_CRASH"

    # And the borrower is not lost: it goes back in the queue.
    assert store.queued_borrowers("C1") == 1
    store.close()


def test_crashed_worker_does_not_block_the_floor_forever():
    store = MemoryStore()
    for i in range(3):
        store.add_agent(Agent(f"A{i}", "C1", state=AgentState.AVAILABLE))
    for i in range(10):
        store.add_borrower(Borrower(f"B{i}", "C1", phone=f"+{i}"))

    for i in range(3):
        assert store.claim_agent("C1", "DEAD", 0.0, 10.0).ok
    # All three agents are held by a dead worker.
    assert not store.claim_agent("C1", "ALIVE", 5.0, 10.0).ok

    store.expire_leases(11.0)
    assert store.claim_agent("C1", "ALIVE", 11.0, 10.0).ok


# --------------------------------------------------------------------------- #
# 2. Provider outage
# --------------------------------------------------------------------------- #
def test_circuit_breaker_opens_and_recovers():
    from smartdialer.clock import VirtualClock
    from smartdialer.providers.base import VirtualScheduler, LineBehaviour
    from smartdialer.providers.mock import ProviderA
    from smartdialer.engine.safety import SafetyConfig, SafetyController
    from smartdialer.engine.allocator import ProviderGateway, CallAllocator
    from smartdialer.engine.pacing import PacingEngine, PacingInputs, PacingMode

    clock = VirtualClock(0.0)
    sched = VirtualScheduler(clock)
    provider = ProviderA(scheduler=sched, clock=clock,
                         line=LineBehaviour(answer_rate=0.5))

    # Simulate 8 consecutive timeouts.
    for _ in range(8):
        provider.health.record_failure(timeout=True)
    assert not provider.health.is_healthy
    assert not provider.health.allow_request()

    # The controller therefore refuses to dial at all.
    safety = SafetyController(SafetyConfig(), clock=clock)
    gateway = ProviderGateway([provider], safety)
    assert not gateway.is_healthy()

    from smartdialer.engine.safety import SafetyContext
    intent = PacingEngine().propose_predictive(
        PacingInputs(on_shift_agents=50, p_answer=0.5, confidence=0.9,
                     queued_borrowers=1000)
    )
    decision = safety.evaluate(intent, SafetyContext(
        campaign_id="C1", now=0.0, available_agents=10, on_shift_agents=50,
        p_answer=0.5, confidence=0.9, queued_borrowers=1000,
        provider_healthy=False, provider_name="ProviderA",
    ))
    assert decision.allowed_calls == 0
    assert decision.action.value == "REJECT"

    # After the recovery window the next allow_request() moves it to HALF_OPEN
    # and lets a probe through -- the breaker decides, not the caller.
    clock.set(100.0)
    assert provider.health.state.value == "OPEN"
    assert provider.health.allow_request()
    assert provider.health.state.value == "HALF_OPEN"
    # A success closes the breaker again.
    provider.health.record_success(0.1)
    assert provider.health.is_healthy
    assert provider.health.state.value == "CLOSED"


# --------------------------------------------------------------------------- #
# 3. Agent availability suddenly drops
# --------------------------------------------------------------------------- #
def test_dialer_reacts_when_agents_disappear():
    """100 agents, 40 vanish.  How fast does the dialer slow down?

    Reaction time is the property being tested, not the abandon rate: losing
    40% of the floor instantly means calls already ringing will be abandoned,
    and no controller can prevent that.  What it CAN do is stop making it
    worse, fast.
    """
    from smartdialer.sim.simulator import Scenario, Simulation
    from smartdialer.engine.pacing import PacingMode

    sc = Scenario("drop", answer_rate=0.5, talk_seconds=(60, 90),
                  agents=100, borrowers=5000, duration_seconds=150.0, seed=7)
    sim = Simulation(sc, mode=PacingMode.PREDICTIVE)
    sim.agent_drop_at = 60.0
    sim.agent_drop_n = 40

    next_tick, guard = 0.0, 0
    inflight_at = {}

    while sim.clock.now() < 150.0 and guard < 300_000:
        guard += 1
        now = sim.clock.now()
        if sim.agent_drop_at is not None and now >= sim.agent_drop_at:
            sim.worker.drop_agents(sim.agent_drop_n)
            sim.agent_drop_at = None
        nxt = sim.scheduler.next_time()
        tick_time = max(next_tick, now)
        if nxt is not None and nxt <= tick_time:
            sim.clock.set(max(now, nxt))
            sim.scheduler.run_until(sim.clock.now())
            continue
        sim.clock.set(tick_time)
        sim.scheduler.run_until(sim.clock.now())
        sim.worker.tick()
        next_tick = sim.clock.now() + 1.0
        t = int(sim.clock.now())
        if 50 <= t <= 100:
            inflight_at[t] = len(sim.store.calls_in_states(
                "C1", [CallState.RESERVED, CallState.INITIATED, CallState.RINGING]
            ))

    # Agents really did vanish.
    assert sim.store.count_agents_by_state("C1")[AgentState.OFFLINE] > 0

    before = max(inflight_at.get(t, 0) for t in range(55, 61))
    after = min(inflight_at.get(t, 0) for t in range(75, 95))

    # The dialer cut the pipeline after the floor lost 40% of its capacity.
    assert after < before, (
        f"dialer did not react: in-flight {before} before the drop, "
        f"{after} afterwards"
    )
    # And it did so within ~15 seconds, i.e. a few ticks, not minutes.
    settled = [t for t in range(61, 95) if inflight_at.get(t, 0) <= before * 0.75]
    assert settled, "pipeline never throttled down"
    assert min(settled) - 60 <= 15, f"took {min(settled) - 60}s to react"


# --------------------------------------------------------------------------- #
# 4 & 5. Duplicate and out-of-order events under a hostile provider
# --------------------------------------------------------------------------- #
def test_hostile_provider_never_corrupts_call_state():
    from smartdialer.sim.simulator import Scenario, Simulation
    from smartdialer.engine.pacing import PacingMode
    from smartdialer.providers.mock import ProviderB

    sc = Scenario("hostile", answer_rate=0.5, talk_seconds=(60, 90),
                  agents=30, borrowers=4000, duration_seconds=600.0,
                  seed=11, provider_cls=ProviderB)
    sim = Simulation(sc, mode=PacingMode.PREDICTIVE)
    result = sim.run()
    m = result.metrics

    # Provider B is configured to duplicate and reorder, so we must have seen both.
    assert m["duplicate_events"] > 0, "expected duplicate deliveries"
    assert m["rejected_events"], "expected some rejected events"

    # Every call ends in a coherent state.
    from smartdialer.domain.states import CALL_TERMINAL_STATES, CallState
    for call in sim.store.calls.values():
        assert call.state in CallState
        if call.is_terminal():
            assert call.ended_at is not None

    # No call ever went backwards: replay the audit history and check monotonicity.
    last_seq = {}
    for h in sim.store.history:
        cid = h["call_id"]
        seq = h["seq"]
        assert seq > last_seq.get(cid, 0), f"non-monotonic seq for {cid}"
        last_seq[cid] = seq

    # The state machine rejected the nonsense rather than applying it.
    bad = {k: v for k, v in m["rejected_events"].items() if k != "APPLIED"}
    assert bad, "nothing was rejected -- is ProviderB actually misbehaving?"


def test_a_silent_provider_does_not_leak_agents():
    """Provider never sends a terminal event -> the reaper must free the agent."""
    from smartdialer.clock import VirtualClock
    from smartdialer.providers.base import VirtualScheduler
    from smartdialer.providers.mock import ProviderA
    from smartdialer.engine.safety import SafetyController
    from smartdialer.engine.allocator import CallAllocator, ProviderGateway
    from smartdialer.engine.pacing import PacingEngine
    from smartdialer.engine.dialer import DialerWorker

    store = MemoryStore()
    store.add_agent(Agent("A1", "C1", state=AgentState.AVAILABLE))
    store.add_borrower(Borrower("B1", "C1", phone="+1"))

    clock = VirtualClock(0.0)
    sched = VirtualScheduler(clock)
    provider = ProviderA(scheduler=sched, clock=clock)
    provider.on_event(lambda e: None)   # swallow every event: total silence

    safety = SafetyController(clock=clock)
    gateway = ProviderGateway([provider], safety)
    allocator = CallAllocator(store, gateway, safety, clock, worker_id="W1")
    worker = DialerWorker(
        store=store, gateway=gateway, allocator=allocator,
        pacing=PacingEngine(), safety=safety, clock=clock, scheduler=sched,
        campaign_id="C1", stuck_call_timeout=30.0, lease_seconds=600.0,
    )

    # A call is ringing and will never resolve.
    store.claim_agent("C1", "W1", 0.0, 600.0)
    store.set_agent_state("A1", AgentState.DIALING, 0.0,
                          expected_states=[AgentState.RESERVED],
                          worker_id="W1", lease_seconds=600.0, call_id="CALL1")
    call = store.create_call(
        Call(call_id="CALL1", borrower_id="B1", agent_id="A1", campaign_id="C1",
             state=CallState.RESERVED)
    )
    store.apply_event(
        ProviderEvent(event_id="E1", call_id="CALL1", seq=1, type="INITIATED"), 0.0
    )
    assert store.get_agent("A1").state == AgentState.DIALING

    # Still inside the timeout: nothing is reaped, we do not panic early.
    worker.recover()
    assert store.get_agent("A1").state == AgentState.DIALING

    # Past the timeout, the reaper gives up on the provider and frees the agent.
    clock.set(31.0)
    worker.recover()
    assert store.get_agent("A1").state == AgentState.AVAILABLE
    assert store.get_call("CALL1").outcome == "PROVIDER_SILENT"
    assert store.get_call("CALL1").is_terminal()
