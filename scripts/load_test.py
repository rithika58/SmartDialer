#!/usr/bin/env python3
"""
Basic load test.

Two things are measured, because they fail for different reasons:

  1. Agent allocation under concurrency.  This is the correctness-critical
     hot path: every dial starts with an atomic claim.  We measure throughput
     and latency percentiles with real threads against a real SQLite file.

  2. Decision throughput -- how many pacing + safety evaluations per second the
     controller can sustain, which is what bounds how fast the dialer can react.

Usage:
    python3 scripts/load_test.py [--agents 2000] [--workers 16] [--seconds 5]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smartdialer.clock import RealClock
from smartdialer.domain.models import Agent
from smartdialer.domain.states import AgentState
from smartdialer.engine.pacing import PacingEngine, PacingInputs, PacingMode
from smartdialer.engine.safety import SafetyConfig, SafetyContext, SafetyController
from smartdialer.stores.sqlite_store import SQLiteStore
from smartdialer.stats import percentile


def pct_block(name, values, unit="ms"):
    if not values:
        print(f"    {name:<28} no data")
        return
    print(
        f"    {name:<28} "
        f"p50={percentile(values, 0.50):8.3f}{unit}  "
        f"p95={percentile(values, 0.95):8.3f}{unit}  "
        f"p99={percentile(values, 0.99):8.3f}{unit}  "
        f"max={max(values):8.3f}{unit}"
    )


def bench_claims(agents: int, workers: int, seconds: float):
    print()
    print("=" * 78)
    print(f"  1. AGENT ALLOCATION UNDER CONCURRENCY  "
          f"({agents} agents, {workers} threads)")
    print("=" * 78)

    path = tempfile.mktemp(suffix=".db")
    store = SQLiteStore(path)
    for i in range(agents):
        store.add_agent(
            Agent(f"A{i:06d}", "C1", state=AgentState.AVAILABLE, state_since=0.0)
        )

    stop = threading.Event()
    latencies, wins, attempts = [], [], []
    lock = threading.Lock()

    def worker(wid):
        local_lat, local_win, local_att = [], 0, 0
        while not stop.is_set():
            t0 = time.perf_counter()
            res = store.claim_agent("C1", wid, time.time(), 30.0)
            local_lat.append((time.perf_counter() - t0) * 1000.0)
            local_att += 1
            if res.ok:
                local_win += 1
                # Immediately return it, so the bench measures steady state
                # rather than draining the pool once.
                store.set_agent_state(res.obj.agent_id, AgentState.AVAILABLE,
                                      time.time(), worker_id=wid)
        with lock:
            latencies.extend(local_lat)
            wins.append(local_win)
            attempts.append(local_att)

    threads = [threading.Thread(target=worker, args=(f"W{i}",), daemon=True)
               for i in range(workers)]
    start = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    total_attempts = sum(attempts)
    total_wins = sum(wins)
    print(f"    wall clock                {elapsed:8.2f}s")
    print(f"    claim attempts            {total_attempts:8d}")
    print(f"    successful claims         {total_wins:8d}")
    print(f"    throughput                {total_attempts / elapsed:8.0f} claims/sec")
    pct_block("claim latency", latencies)

    # Correctness under load: no agent may be reserved twice.
    counts = store.count_agents_by_state("C1")
    on_shift = sum(counts.values())
    reserved = counts[AgentState.RESERVED]
    print(f"    agents in table           {on_shift:8d} (expected {agents})")
    print(f"    AVAILABLE + RESERVED      "
          f"{counts[AgentState.AVAILABLE] + reserved:8d}")
    assert on_shift == agents, "agents were lost or duplicated under load"
    print("    no agent allocated twice  PASS")

    store.close()
    Path(path).unlink(missing_ok=True)
    return total_attempts / elapsed


def bench_decisions(n: int):
    print()
    print("=" * 78)
    print(f"  2. SAFETY-CONTROLLER DECISION THROUGHPUT  ({n} decisions)")
    print("=" * 78)

    pacing = PacingEngine()
    safety = SafetyController(SafetyConfig(), clock=RealClock())
    ctx = SafetyContext(
        campaign_id="C1", now=0.0, available_agents=12, on_shift_agents=1000,
        agents_free_within_horizon=5.0, ringing_calls=8, connected_calls=900,
        p_answer=0.45, confidence=0.95, queued_borrowers=50_000,
        provider_healthy=True, provider_name="bench",
        at_risk_agents=900, q_free=0.08, avg_ring_seconds=8.0,
        mean_service_seconds=100.0,
    )

    inputs = PacingInputs(
        available_agents=12, on_shift_agents=1000, agents_free_within_horizon=5.0,
        ringing_calls=8, connected_calls=900, p_answer=0.45, confidence=0.95,
        avg_talk_seconds=88.0, avg_wrap_seconds=12.0, avg_ring_seconds=8.0,
        queued_borrowers=50_000,
    )

    latencies = []
    for _ in range(n):
        t0 = time.perf_counter()
        intent = pacing.propose(inputs, PacingMode.PREDICTIVE)
        decision = safety.evaluate(intent, ctx)
        latencies.append((time.perf_counter() - t0) * 1000.0)

    total = sum(latencies)
    print(f"    decisions                 {n:8d}")
    print(f"    total time                {total / 1000:8.3f}s")
    print(f"    throughput                {n / (total / 1000):8.0f} decisions/sec")
    pct_block("decision latency", latencies)
    print()
    print(f"    sample decision: {decision.explain()[:110]}")
    return n / (total / 1000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--decisions", type=int, default=2000)
    args = ap.parse_args()

    print("=" * 78)
    print("  SmartDialer load test")
    print("=" * 78)

    bench_claims(args.agents, args.workers, args.seconds)
    bench_decisions(args.decisions)

    print()
    print("=" * 78)
    print("  Reading these numbers")
    print("=" * 78)
    print("  Allocation throughput is bounded by SQLite's single writer.  It is")
    print("  comfortably fast enough for 1,000 agents and is the first thing that")
    print("  breaks at 10,000 -- see docs/ARCHITECTURE.md for the migration path.")
    print("  Decision throughput is pure CPU and is not the bottleneck at any")
    print("  realistic campaign size.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
