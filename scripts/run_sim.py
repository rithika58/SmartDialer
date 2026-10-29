#!/usr/bin/env python3
"""
Run the simulator across the scenarios named in the assignment.

    Scenario   Answer rate    Avg talk time
    A          20%            120 sec
    B          50%            90 sec
    C          70%            180 sec
    D          changing       changing

Each is run in both PROGRESSIVE and PREDICTIVE mode, and (with --provider B)
against the badly-behaved provider, so the safety controller's behaviour is
visible under degradation.

A single seed proves nothing: these are stochastic runs and two seeds of the
same configuration differ by a percentage point or more.  Use --seeds to run
each cell several times and report the spread.

    python3 scripts/run_sim.py --seeds 5
    python3 scripts/run_sim.py --only C --seeds 10 --provider both
    python3 scripts/run_sim.py --seeds 5 --json > results.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smartdialer.engine.pacing import PacingMode
from smartdialer.providers.mock import ProviderA, ProviderB
from smartdialer.sim.simulator import Scenario, Simulation

# The compliance budget the Safety Controller is configured to defend.
ABANDON_BUDGET = 0.03


def changing_scenario(sim: "Simulation", now: float) -> None:
    """Scenario D: the world moves under the dialer.

    Answer rate ramps 60% -> 25% -> 55% and talk time doubles halfway.  This is
    the test of whether the answer-rate estimator and the safety controller can
    track a regime change instead of over-dialing into the new one.
    """
    total = sim.scenario.duration_seconds
    frac = now / total
    if frac < 0.33:
        rate = 0.60
    elif frac < 0.66:
        # sharp drop
        rate = 0.60 - 0.35 * ((frac - 0.33) / 0.33)
    else:
        rate = 0.25 + 0.30 * ((frac - 0.66) / 0.34)
    sim.line.answer_rate = rate
    if frac >= 0.5:
        sim.line.talk_seconds = (150.0, 240.0)
    else:
        sim.line.talk_seconds = (60.0, 110.0)


SCENARIOS = [
    Scenario("A: 20% / 120s", answer_rate=0.20, talk_seconds=(100.0, 140.0)),
    Scenario("B: 50% / 90s", answer_rate=0.50, talk_seconds=(75.0, 105.0)),
    Scenario("C: 70% / 180s", answer_rate=0.70, talk_seconds=(150.0, 210.0)),
    Scenario(
        "D: changing", answer_rate=0.60, talk_seconds=(60.0, 110.0),
        mutate=changing_scenario,
        note="answer rate and talk time drift mid-run",
    ),
]


def run_cell(sc: Scenario, mode: PacingMode, seeds, **kwargs):
    """One scenario x mode cell, repeated over `seeds` independent runs."""
    return [Simulation(replace(sc, seed=seed), mode=mode, **kwargs).run()
            for seed in seeds]


def _stat(values, fmt: str) -> str:
    if len(values) == 1:
        return format(values[0], fmt)
    return (f"{statistics.mean(values):{fmt}} "
            f"[{min(values):{fmt}}..{max(values):{fmt}}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["A", "B", "both"], default="both")
    ap.add_argument("--only", default="",
                    help="comma-separated scenario letters, e.g. C or A,C")
    ap.add_argument("--agents", type=int, default=100)
    ap.add_argument("--duration", type=float, default=1800.0)
    ap.add_argument("--seeds", type=int, default=1,
                    help="independent runs per cell; >1 reports mean and range")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    wanted = [s.strip().upper() for s in args.only.split(",") if s.strip()]
    scenarios = [sc for sc in SCENARIOS if not wanted or sc.name[0] in wanted]
    if not scenarios:
        print(f"no scenarios matched --only {args.only!r}", file=sys.stderr)
        return 2

    for sc in scenarios:
        sc.agents = args.agents
        sc.duration_seconds = args.duration

    provs = {"A": [ProviderA], "B": [ProviderB], "both": [ProviderA, ProviderB]}[args.provider]
    seeds = list(range(1, args.seeds + 1))

    header = (f"{'scenario':<14} {'mode':<12} {'prov':<5} {'talk-util':>18} "
              f"{'occupancy':>10} {'initiated':>10} {'connected':>10} "
              f"{'abandon':>18}")
    rows = []
    for cls in provs:
        print()
        print(f"=== Provider {cls.__name__} "
              f"({args.agents} agents, {args.duration/60:.0f} min, "
              f"{len(seeds)} seed{'s' if len(seeds) > 1 else ''}) ===")
        print(header)
        print("-" * len(header))
        for sc in scenarios:
            sc.provider_cls = cls
            cells = {}
            for mode in (PacingMode.PROGRESSIVE, PacingMode.PREDICTIVE):
                results = run_cell(sc, mode, seeds)
                m = {k: [float(r.metrics[k]) for r in results]
                     for k in ("talk_utilization", "occupancy", "abandon_rate")}
                i = [int(r.metrics["calls_initiated"]) for r in results]
                c = [int(r.metrics["calls_connected"]) for r in results]
                cells[mode] = statistics.mean(m["talk_utilization"])

                flag = ""
                if (mode is PacingMode.PREDICTIVE
                        and statistics.mean(m["abandon_rate"]) > ABANDON_BUDGET):
                    flag = "  <-- OVER BUDGET"
                print(f"{sc.name:<14} {mode.value:<12} {cls.__name__[-1]:<5} "
                      f"{_stat(m['talk_utilization'], '7.1%'):>18} "
                      f"{_stat(m['occupancy'], '7.1%'):>10} "
                      f"{statistics.mean(i):>10.0f} "
                      f"{statistics.mean(c):>10.0f} "
                      f"{_stat(m['abandon_rate'], '7.2%'):>18}{flag}")
                for r in results:
                    rows.append({
                        "provider_cls": cls.__name__,
                        "scenario": r.scenario,
                        "mode": r.mode,
                        "seed": sc.seed,
                        "metrics": r.metrics,
                        "safety": r.safety_counts,
                        "estimate": r.estimate,
                    })
            delta = cells[PacingMode.PREDICTIVE] - cells[PacingMode.PROGRESSIVE]
            print(f"{'':<14} {'-> predictive':<12} {'':<5} "
                  f"{delta:+18.1%} (utilization vs progressive)")

    # Budget summary across every predictive cell we just ran.
    over = [r for r in rows
            if r["mode"] == "PREDICTIVE"
            and float(r["metrics"]["abandon_rate"]) > ABANDON_BUDGET]
    print()
    if over:
        print(f"BUDGET: {len(over)}/{sum(1 for r in rows if r['mode'] == 'PREDICTIVE')} "
              f"predictive runs exceeded the {ABANDON_BUDGET:.0%} abandon budget:")
        for r in over:
            print(f"  {r['provider_cls']} {r['scenario']} seed={r['seed']} "
                  f"abandon={float(r['metrics']['abandon_rate']):.2%}")
    else:
        print(f"BUDGET: all {sum(1 for r in rows if r['mode'] == 'PREDICTIVE')} "
              f"predictive runs stayed inside the {ABANDON_BUDGET:.0%} "
              f"abandon budget.")

    if args.json:
        print()
        print(json.dumps(rows, indent=2))
    return 1 if over else 0


if __name__ == "__main__":
    raise SystemExit(main())
