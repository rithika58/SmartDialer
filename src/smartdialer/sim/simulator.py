"""
Discrete-event simulator.

Real dialers run in real time, which makes them slow and non-deterministic to
evaluate.  Because every component takes an injected Clock and Scheduler, we
can run the *same* pacing engine, safety controller, allocator and providers
against a virtual clock and simulate a full shift in under a second.

Nothing in the simulation is a mock of the dialer -- only time is fake.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Type

from ..clock import VirtualClock
from ..domain.models import Agent, Borrower
from ..domain.states import AgentState
from ..engine.allocator import CallAllocator, ProviderGateway
from ..engine.dialer import DialerWorker
from ..engine.pacing import PacingEngine, PacingMode
from ..engine.safety import SafetyConfig, SafetyController
from ..providers.base import LineBehaviour, VirtualScheduler
from ..providers.mock import ProviderA, ProviderB
from ..stores.memory import MemoryStore


@dataclass
class Scenario:
    name: str
    answer_rate: float
    talk_seconds: tuple
    ring_seconds: tuple = (4.0, 12.0)
    agents: int = 100
    borrowers: int = 20_000
    duration_seconds: float = 1800.0
    tick_interval: float = 1.0
    seed: int = 42
    provider_cls: Type = ProviderA
    # Optional hook to mutate the world mid-run (used by scenario D).
    mutate: Optional[Callable[["Simulation", float], None]] = None
    note: str = ""


@dataclass
class SimResult:
    scenario: str
    mode: str
    provider: str
    metrics: Dict[str, object] = field(default_factory=dict)
    safety_counts: Dict[str, int] = field(default_factory=dict)
    agent_totals: Dict[str, int] = field(default_factory=dict)
    sample_decisions: List[str] = field(default_factory=list)
    estimate: Dict[str, object] = field(default_factory=dict)

    def headline(self) -> str:
        m = self.metrics
        return (
            f"{self.scenario:<22} {self.mode:<12} {self.provider:<10} "
            f"talk-util={float(m['talk_utilization']):6.1%}  "
            f"occupancy={float(m['occupancy']):6.1%}  "
            f"initiated={int(m['calls_initiated']):6d}  "
            f"answered={int(m['calls_answered']):6d}  "
            f"connected={int(m['calls_connected']):6d}  "
            f"abandon={int(m['abandoned']):4d} ({float(m['abandon_rate']):.2%})"
        )


class Simulation:
    def __init__(
        self,
        scenario: Scenario,
        mode: PacingMode = PacingMode.PREDICTIVE,
        safety_config: Optional[SafetyConfig] = None,
        aggressiveness: float = 1.0,
        wrap_seconds: float = 12.0,
        patience_seconds: float = 5.0,
        sample_interval: float = 10.0,
    ) -> None:
        self.scenario = scenario
        self.mode = mode
        self.sample_interval = sample_interval
        self.samples: list = []
        self.rng = random.Random(scenario.seed)

        self.clock = VirtualClock(0.0)
        self.scheduler = VirtualScheduler(self.clock)
        self.store = MemoryStore()

        self.line = LineBehaviour(
            answer_rate=scenario.answer_rate,
            ring_seconds=scenario.ring_seconds,
            talk_seconds=scenario.talk_seconds,
            bridge_seconds=0.5,
        )

        provider = scenario.provider_cls(
            scheduler=self.scheduler, clock=self.clock,
            rng=random.Random(scenario.seed + 1), line=self.line,
        )
        # Always stand up a second provider so failover is exercised.
        other_cls = ProviderB if scenario.provider_cls is ProviderA else ProviderA
        self.backup = other_cls(
            scheduler=self.scheduler, clock=self.clock,
            rng=random.Random(scenario.seed + 2), line=self.line,
        )
        self.providers = [provider, self.backup]

        provider.on_event(self._on_event)
        self.backup.on_event(self._on_event)

        self.safety = SafetyController(safety_config or SafetyConfig(
            max_calls_per_second=500.0,
            max_concurrent_calls=20_000,
        ), clock=self.clock)
        self.gateway = ProviderGateway(self.providers, self.safety)
        self.allocator = CallAllocator(
            store=self.store, gateway=self.gateway, controller=self.safety,
            clock=self.clock, lease_seconds=20.0, worker_id="sim-worker",
        )
        self.pacing = PacingEngine(aggressiveness=aggressiveness)
        self.worker = DialerWorker(
            store=self.store, gateway=self.gateway, allocator=self.allocator,
            pacing=self.pacing, safety=self.safety, clock=self.clock,
            scheduler=self.scheduler, campaign_id="C1", mode=mode,
            wrap_seconds=wrap_seconds, stuck_call_timeout=120.0,
            lease_seconds=20.0, patience_seconds=patience_seconds,
        )

        self._seed_population()
        self.agent_drop_at: Optional[float] = None
        self.agent_drop_n: int = 0
        self.provider_outage_at: Optional[float] = None

    # ------------------------------------------------------------------ setup
    def _seed_population(self) -> None:
        s = self.scenario
        for i in range(s.agents):
            self.store.add_agent(
                Agent(agent_id=f"AG{i:05d}", campaign_id="C1",
                      state=AgentState.AVAILABLE, state_since=0.0)
            )
        for i in range(s.borrowers):
            self.store.add_borrower(
                Borrower(borrower_id=f"BO{i:06d}", campaign_id="C1",
                         phone=f"+91{i:010d}", priority=0)
            )

    def _on_event(self, event) -> None:
        self.worker.on_event(event)

    # ------------------------------------------------------------- telemetry
    def _record_sample(self) -> None:
        """Snapshot the world for the dashboard.

        Deliberately cheap and side-effect free: sampling must never change how
        the dialer behaves, or the dashboard would be measuring itself.
        """
        from ..domain.states import CallState

        now = self.clock.now()
        agents = self.store.count_agents_by_state("C1")
        calls = {st: 0 for st in CallState}
        for c in self.store.calls.values():
            calls[c.state] += 1

        m = self.worker.metrics
        self.samples.append({
            "t": round(now, 1),
            "agents": {st.value: agents.get(st, 0) for st in agents},
            "calls": {st.value: calls.get(st, 0) for st in CallState},
            "talk_util": round(m.talk_utilization, 4),
            "occupancy": round(m.occupancy, 4),
            "window_abandon_rate": round(m.window_abandon_rate, 4),
            "cum_abandon_rate": round(m.abandon_rate, 4),
            "p_answer": round(self.worker.answer_estimate.p, 4),
            "aggressiveness": round(self.worker.pacer.aggressiveness, 3),
            "holding": len(self.worker.holding),
            "initiated": m.calls_initiated,
            "answered": m.calls_answered,
            "connected": m.calls_connected,
            "abandoned": m.abandoned,
            "duplicates": m.duplicate_events,
            "rejected": dict(m.rejected_events),
            "in_flight": (
                calls.get(CallState.RESERVED, 0)
                + calls.get(CallState.INITIATED, 0)
                + calls.get(CallState.RINGING, 0)
            ),
            "safety": dict(self.safety.snapshot_stats()),
            "providers": self.gateway.health_snapshot(),
        })

    def telemetry(self) -> dict:
        m = self.worker.metrics
        return {
            "scenario": self.scenario.name,
            "mode": self.mode.value,
            "duration": round(self.clock.now(), 1),
            "agents_on_shift": self.scenario.agents,
            "config": {
                "patience_seconds": self.worker.patience_seconds,
                "wrap_seconds": self.worker.wrap_seconds,
                "target_occupancy": 0.90,
                "max_abandon_probability": self.safety.config.max_abandon_probability,
                "max_observed_abandon_rate": self.safety.config.max_observed_abandon_rate,
                "min_confidence_for_predictive": (
                    self.safety.config.min_confidence_for_predictive
                ),
            },
            "summary": m.as_dict(),
            "providers": self.gateway.health_snapshot(),
            "aggressiveness_history": self.worker.pacer.history[-40:],
            "samples": self.samples,
        }

    # -------------------------------------------------------------------- run
    def run(self, verbose: bool = False) -> SimResult:
        s = self.scenario
        next_tick = 0.0
        next_recover = 10.0
        next_sample = 0.0
        guard = 0

        while self.clock.now() < s.duration_seconds:
            guard += 1
            if guard > 5_000_000:
                break

            now = self.clock.now()

            if s.mutate is not None:
                s.mutate(self, now)

            if self.agent_drop_at is not None and now >= self.agent_drop_at:
                self.worker.drop_agents(self.agent_drop_n)
                self.agent_drop_at = None

            if self.provider_outage_at is not None and now >= self.provider_outage_at:
                for p in self.providers:
                    p.behaviour.outage = True
                self.provider_outage_at = None

            nxt = self.scheduler.next_time()
            tick_time = max(next_tick, now)

            if nxt is not None and nxt <= tick_time:
                self.clock.set(max(now, nxt))
                self.scheduler.run_until(self.clock.now())
                continue

            self.clock.set(tick_time)
            self.scheduler.run_until(self.clock.now())

            self.worker.tick()
            next_tick = self.clock.now() + s.tick_interval

            if self.clock.now() >= next_recover:
                self.worker.recover()
                next_recover = self.clock.now() + 10.0

            if self.clock.now() >= next_sample:
                self._record_sample()
                next_sample = self.clock.now() + self.sample_interval

        # drain anything still scheduled at the end
        self.scheduler.run_until(self.clock.now())
        self.worker.recover()
        self._record_sample()

        counts = self.store.count_agents_by_state("C1")
        return SimResult(
            scenario=s.name,
            mode=self.mode.value,
            provider=self.providers[0].name,
            metrics=self.worker.metrics.as_dict(),
            safety_counts=self.safety.snapshot_stats(),
            agent_totals={k.value: v for k, v in counts.items()},
            sample_decisions=[d.explain() for d in self.worker.decisions[:3]],
            estimate=self.worker.answer_estimate.as_dict(),
        )


def run_matrix(scenarios: List[Scenario], modes=None, **kwargs) -> List[SimResult]:
    modes = modes or [PacingMode.PROGRESSIVE, PacingMode.PREDICTIVE]
    out: List[SimResult] = []
    for sc in scenarios:
        for mode in modes:
            sim = Simulation(sc, mode=mode, **kwargs)
            out.append(sim.run())
    return out
