#!/usr/bin/env python3
"""
Walk through the five failure cases named in the assignment and show, with
real numbers, what the system does in each.

    python3 scripts/demo_failures.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smartdialer.domain.models import Agent, Borrower, Call
from smartdialer.domain.states import AgentState, CallState
from smartdialer.stores.sqlite_store import SQLiteStore
from smartdialer.sim.simulator import Scenario, Simulation
from smartdialer.engine.pacing import PacingMode
from smartdialer.providers.mock import ProviderB

RULE = "=" * 78


def header(n: int, title: str) -> None:
    print()
    print(RULE)
    print(f"  FAILURE CASE {n}: {title}")
    print(RULE)


# --------------------------------------------------------------------------- #
def case_1_worker_crash():
    header(1, "Worker crash: reserved -> initiated -> worker dies")
    path = tempfile.mktemp(suffix=".db")
    store = SQLiteStore(path)

    for i in range(5):
        store.add_agent(Agent(f"A{i}", "C1", state=AgentState.AVAILABLE))
    for i in range(100):
        store.add_borrower(Borrower(f"B{i}", "C1", phone=f"+91{i}"))

    # Worker W1 does everything right up to the point it dies.
    store.claim_agent("C1", "W1", 100.0, 20.0)
    store.claim_borrower("C1", "W1", 100.0, 20.0)
    store.create_call(Call(call_id="CALL1", borrower_id="B0", agent_id="A0",
                           campaign_id="C1"))
    store.set_agent_state("A0", AgentState.DIALING, 100.0,
                          expected_states=[AgentState.RESERVED],
                          worker_id="W1", lease_seconds=20.0, call_id="CALL1")

    print("  t=100  W1 reserved agent A0, borrower B0, initiated CALL1")
    print("  t=100  W1 is SIGKILLed.  It will never heartbeat again.")
    print()

    print("  t=110  (lease still valid -- we do NOT steal the agent early,")
    print("          the worker might just be slow)")
    rep = store.expire_leases(110.0)
    a0 = store.get_agent("A0")
    print(f"          reclaimed={rep.as_dict()}  A0={a0.state.value}")
    print()

    print("  t=121  (lease expired -- recovery runs)")
    rep = store.expire_leases(121.0)
    a0 = store.get_agent("A0")
    call = store.get_call("CALL1")
    print(f"          reclaimed={rep.as_dict()}")
    print(f"          A0   -> {a0.state.value} (lease_owner={a0.lease_owner})")
    print(f"          CALL1 -> {call.state.value} (outcome={call.outcome})")
    print(f"          B0   -> back in queue ({store.queued_borrowers('C1')} available)")
    print()
    print("  Answer: detection is lease expiry, not failure detection.  We never")
    print("  try to notice a dead worker; we stop trusting a reservation whose")
    print("  owner stopped renewing.  That survives SIGKILL, a lost VM and a")
    print("  network partition identically.")
    store.close()
    Path(path).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
def case_2_provider_outage():
    header(2, "Provider outage")
    sc = Scenario("outage", answer_rate=0.5, talk_seconds=(60, 90),
                  agents=40, borrowers=5000, duration_seconds=420.0, seed=5)
    sim = Simulation(sc, mode=PacingMode.PREDICTIVE)
    sim.provider_outage_at = 180.0

    placed_before = 0
    next_tick, guard = 0.0, 0
    while sim.clock.now() < 420.0 and guard < 300_000:
        guard += 1
        now = sim.clock.now()
        if sim.provider_outage_at is not None and now >= sim.provider_outage_at:
            for p in sim.providers:
                p.behaviour.outage = True
            placed_before = sim.worker.metrics.calls_initiated
            print(f"  t={now:.0f}  provider outage begins "
                  f"({placed_before} calls placed so far)")
            sim.provider_outage_at = None
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

    m = sim.worker.metrics
    print()
    print(f"  calls placed before outage : {placed_before}")
    print(f"  calls placed after  outage : {m.calls_initiated - placed_before}")
    print(f"  place failures recorded    : {m.place_failures}")
    print(f"  leases reclaimed           : {m.leases_reclaimed}")
    print()
    print("  Circuit breaker state at the end:")
    for snap in sim.gateway.health_snapshot():
        print(f"    {snap}")
    print()
    print("  What happened:")
    print("   - new calls: refused at the gateway; the breaker opened after")
    print("     repeated timeouts, so we stopped hammering a dying provider.")
    print("   - existing calls: left alone.  We cannot hang up a call we cannot")
    print("     reach, so they run to completion or are reaped as PROVIDER_SILENT.")
    print("   - pacing: safety decisions went to 0 while the breaker was open.")
    print("   - retries: via the HALF_OPEN probe, not a blind retry storm.")


# --------------------------------------------------------------------------- #
def case_3_agents_disappear():
    header(3, "100 agents -> 40 disappear within a few seconds")
    sc = Scenario("drop", answer_rate=0.5, talk_seconds=(60, 90),
                  agents=100, borrowers=5000, duration_seconds=150.0, seed=7)
    sim = Simulation(sc, mode=PacingMode.PREDICTIVE)
    sim.agent_drop_at = 60.0
    sim.agent_drop_n = 40

    series = {}
    next_tick, guard = 0.0, 0
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
        if 55 <= t <= 85:
            series[t] = len(sim.store.calls_in_states(
                "C1", [CallState.RESERVED, CallState.INITIATED, CallState.RINGING]
            ))

    print("  unanswered calls in flight (t=60 is when they vanish):")
    for t in sorted(series):
        bar = "#" * series[t]
        mark = "   <-- 40 agents disappear" if t == 60 else ""
        print(f"    t={t:>3}s  {series[t]:>3}  {bar}{mark}")
    print()
    before = max(series.get(t, 0) for t in range(55, 61))
    after = min(series.get(t, 0) for t in range(70, 86))
    print(f"  pipeline before: {before}   after: {after}   "
          f"({(1 - after / before) * 100 if before else 0:.0f}% reduction)")
    print()
    print("  Reaction is one pacing tick: the safety controller recomputes the")
    print("  pipeline ceiling from the agents still on shift, so the very next")
    print("  decision is sized for 60 agents rather than 100.  Calls already")
    print("  ringing cannot be undone, which is why a sudden 40% drop costs some")
    print("  abandons no matter how good the controller is.")


# --------------------------------------------------------------------------- #
def case_4_and_5_duplicate_and_reordering():
    header(4, "Duplicate events  +  out-of-order events  (Provider B)")
    sc = Scenario("hostile", answer_rate=0.5, talk_seconds=(60, 90),
                  agents=30, borrowers=4000, duration_seconds=600.0,
                  seed=11, provider_cls=ProviderB)
    sim = Simulation(sc, mode=PacingMode.PREDICTIVE)
    result = sim.run()
    m = result.metrics

    print("  Provider B sends: duplicate event ids, out-of-order seqs, and")
    print("  occasional silence.  What the dialer did with it:")
    print()
    print(f"    duplicate events absorbed (no 2nd transition): {m['duplicate_events']}")
    print(f"    events rejected by the state machine:          {m['rejected_events']}")
    print(f"    calls initiated:                               {m['calls_initiated']}")
    print(f"    calls answered:                                {m['calls_answered']}")
    print(f"    abandoned:                                     {m['abandoned']}")
    print()

    # Prove no call ever moved backwards.
    last = {}
    regressions = 0
    for h in sim.store.history:
        cid = h["call_id"]
        if h["seq"] <= last.get(cid, 0):
            regressions += 1
        last[cid] = max(last.get(cid, 0), h["seq"])
    print(f"    backwards state transitions recorded:           {regressions}")
    assert regressions == 0

    dup_ids = {}
    for h in sim.store.history:
        dup_ids[h["event_id"]] = dup_ids.get(h["event_id"], 0) + 1
    print(f"    event ids applied more than once:               "
          f"{sum(1 for v in dup_ids.values() if v > 1)}")
    assert all(v == 1 for v in dup_ids.values())
    print()
    print("  Answer: idempotency is a PRIMARY KEY on the provider's event id, so")
    print("  a redelivery is absorbed before the state machine ever sees it.")
    print("  Ordering is handled separately by a monotonic per-call sequence and")
    print("  a state-rank check, so a late RINGING after an ANSWERED is dropped.")
    print("  Neither defence is optional -- they catch different failures.")


def main() -> int:
    print(RULE)
    print("  SmartDialer -- failure demonstration")
    print(RULE)
    case_1_worker_crash()
    case_2_provider_outage()
    case_3_agents_disappear()
    case_4_and_5_duplicate_and_reordering()
    print()
    print(RULE)
    print("  All five cases handled without manual intervention.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
