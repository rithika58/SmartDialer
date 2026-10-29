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
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smartdialer.engine.pacing import PacingMode
from smartdialer.providers.mock import ProviderA, ProviderB
from smartdialer.sim.simulator import Scenario, Simulation, run_matrix


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["A", "B", "both"], default="both")
    ap.add_argument("--agents", type=int, default=100)
    ap.add_argument("--duration", type=float, default=1800.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    for sc in SCENARIOS:
        sc.agents = args.agents
        sc.duration_seconds = args.duration

    provs = {"A": [ProviderA], "B": [ProviderB], "both": [ProviderA, ProviderB]}[args.provider]

    rows = []
    for cls in provs:
        for sc in SCENARIOS:
            sc.provider_cls = cls
        results = run_matrix(SCENARIOS)
        print()
        print(f"=== Provider {cls.__name__} "
              f"({args.agents} agents, {args.duration/60:.0f} min) ===")
        print(f"{'scenario':<22} {'mode':<12} {'provider':<10} "
              f"{'talk-util':>10} {'occupancy':>10} {'initiated':>9} "
              f"{'answered':>9} {'connected':>10} {'abandon':>12}")
        print("-" * 100)
        for r in results:
            print(r.headline())
            rows.append({
                "provider_cls": cls.__name__,
                "scenario": r.scenario,
                "mode": r.mode,
                "metrics": r.metrics,
                "safety": r.safety_counts,
                "estimate": r.estimate,
            })

    if args.json:
        print()
        print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
