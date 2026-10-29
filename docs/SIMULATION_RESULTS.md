# Simulation results and honest analysis

Reproduce with:

```bash
PYTHONPATH=src python3 scripts/run_sim.py --agents 100 --duration 1800
```

Every number below comes from the real pacing engine, safety controller,
allocator and providers. Only time is simulated — the components are the same
ones that would run in production.

**Definitions.** *Talk utilization* is the share of on-shift agent-time spent
actually talking to a borrower; it is the number a dialer is judged on.
*Occupancy* is the share of on-shift time an agent is not idle. *Abandoned*
means a borrower picked up, waited the full patience budget (5s) and no agent
came; they are then routed to the IVR with a callback queued, never silently
dropped.

---

## Headline: 100 agents, 30 minutes, both providers

### Provider A (fast, reliable, in-order)

| Scenario | Mode | Talk util | Occupancy | Initiated | Answered | Abandoned |
|---|---|---:|---:|---:|---:|---:|
| A: 20% / 120s | Progressive | 68.2% | 97.1% | 5,210 | 1,056 | 0 (0.00%) |
| A: 20% / 120s | **Predictive** | **74.1%** | 83.9% | 6,043 | 1,169 | 16 (**1.37%**) |
| B: 50% / 90s | Progressive | 74.8% | 98.2% | 3,198 | 1,530 | 0 (0.00%) |
| B: 50% / 90s | **Predictive** | **76.9%** | 88.7% | 3,181 | 1,596 | 11 (**0.69%**) |
| C: 70% / 180s | Progressive | **87.8%** | 99.3% | 1,325 | 916 | 0 (0.00%) |
| C: 70% / 180s | Predictive | 82.9% | 90.5% | 1,302 | 909 | 39 (**4.29%**) |
| D: changing | Progressive | 79.5% | 98.6% | 2,587 | 1,248 | 0 (0.00%) |
| D: changing | Predictive | 77.1% | 87.4% | 2,553 | 1,233 | 31 (**2.51%**) |

### Provider B (slow, timeouts, duplicates, out-of-order)

| Scenario | Mode | Talk util | Occupancy | Initiated | Answered | Abandoned |
|---|---|---:|---:|---:|---:|---:|
| A: 20% / 120s | Progressive | 68.5% | 96.4% | 4,799 | 927 | 0 (0.00%) |
| A: 20% / 120s | **Predictive** | **77.9%** | 86.2% | 5,547 | 1,098 | 15 (**1.37%**) |
| B: 50% / 90s | Progressive | 77.2% | 98.1% | 2,695 | 1,308 | 0 (0.00%) |
| B: 50% / 90s | **Predictive** | **78.1%** | 86.9% | 2,677 | 1,327 | 21 (**1.58%**) |
| C: 70% / 180s | Progressive | **88.4%** | 99.1% | 1,181 | 851 | 0 (0.00%) |
| C: 70% / 180s | Predictive | 85.2% | 93.7% | 1,261 | 874 | 60 (**6.86%**) |
| D: changing | Progressive | 80.4% | 98.4% | 2,362 | 1,121 | 0 (0.00%) |
| D: changing | Predictive | 80.1% | 89.2% | 2,270 | 1,119 | 24 (**2.14%**) |

---

## Reading these numbers

### Where predictive wins, and why

Predictive beats progressive by **+5.9pp** (A) and **+9.4pp** (A, Provider B),
and by **+2.1pp** / **+0.9pp** in B. That is the expected pattern, and the
reason is arithmetic.

Progressive holds an agent for the entire ring time of *every* dial, including
the ones nobody answers. Expected ring time wasted per completed conversation is
`ring / p`:

| Scenario | Ring waste per answer | As share of agent time | Predictive's ceiling gain |
|---|---|---|---|
| A: p=0.20, ring 8s | 40s | 40 / 172 = **23%** | large |
| B: p=0.50, ring 8s | 16s | 16 / 118 = **14%** | moderate |
| C: p=0.70, ring 8s | 11.4s | 11.4 / 203 = **5.6%** | small |

Predictive dialing pays off in proportion to how much agent time progressive
wastes on ringing. **At a 20% answer rate there is a lot to win; at 70% there
is very little.** That is the single most useful thing this simulator shows, and
it is a per-campaign property an operator should be able to see before choosing
a mode.

### Progressive's 0% abandon rate is structural, not luck

Progressive reserves the agent *before* dialing, so a borrower who answers
always has somebody. Abandonment is not merely unlikely, it is impossible by
construction. That guarantee is worth a lot, and the results show it.

### Occupancy is the tell

Progressive shows 97–99% occupancy but only 68–88% talk utilization: agents are
"busy" while the phone rings. Predictive shows lower occupancy (84–94%) and
higher talk utilization in A and B: less busy-waiting, more actual talking.

---

## Where this falls short

### Scenario C misses the 3% abandonment target

At a 70% answer rate with 180s conversations, abandonment runs **4.29%**
(Provider A) and **6.86%** (Provider B) against a 3% target, while *losing*
4.9pp / 3.2pp of utilization. This is the weakest result in the submission and I
am not going to dress it up.

**Why it happens.** With 180s conversations, each agent is occupied for ~192s,
so on a 100-agent floor an agent frees only every ~1.9s and the pipeline that
keeps the floor busy is very small (ceiling ≈ 6 unanswered calls). Running the
floor near capacity means ρ ≈ 0.98, and at ρ that close to 1 the queueing delay
is extremely sensitive to arrival lumpiness: two borrowers answering a few
hundred milliseconds apart is enough for the second to exceed a 5s patience
budget.

**Why the controller does not catch it.** Three compounding reasons:

1. The per-decision probabilistic guard assumes agent availability is known. It
   is not; it is itself random, and modelling only the answer side
   under-estimates the tail.
2. The talk-time estimate is survivor-biased. Calls still running are, on
   average, the long ones, so a mean taken over finished calls under-states
   service time and over-states how fast the floor drains. I partially correct
   this by folding in partial durations, but the correction is crude.
3. The AIMD loop reacts over a 30s window, which in scenario C is only ~15
   answers. The sample is too small to distinguish a bad window from a bad
   policy, so it dithers.

**What I would try next,** in order:

- Model *both* sides of the queue explicitly. The controller already convolves
  answers with agent-freeing, but the agent side uses a crude uniform
  approximation of remaining service time; a proper Erlang-A abandon model would
  predict the tail far better.
- Make `target_occupancy` adaptive to `service / ring`. Long calls with short
  rings produce lumpy arrivals and need more slack; that ratio is exactly the
  knob.
- Widen the AIMD window in proportion to answer volume, so the loop is not
  reacting to noise.

I did check the obvious cheap fixes: raising the pipeline ceiling (abandonment
goes to 5–7% for no reliable utilization gain) and lowering the controller's
target abandon rate (no measurable effect on C). It is a modelling problem, not
a tuning problem.

### Seed sensitivity

Scenario A varied from +4.8pp to +6.2pp across random seeds, with abandonment
between 0.6% and 3.1%. The single-seed headline above is representative, not
precise. Averages over 5 seeds at 50 agents: A +4.8pp, B −4.6pp, C −9.1pp.

Note that at 50 agents predictive loses in B; at 100 agents it wins. **Floor
size matters** — larger floors are less lumpy, so the queueing cost of
overbooking falls relative to its benefit. Any claim about "predictive is
better" is meaningless without naming the floor size.

### Honest summary

- Abandonment target (≤3%) is **met** in scenarios A, B and D.
- It is **missed** in scenario C.
- Predictive beats progressive where it should (low answer rate) and loses where
  the theory says it should (high answer rate, long talks).
- The safety machinery holds: 0 backwards transitions, 0 double-applied event
  ids, and no double-allocated agents under any scenario or load test.

---

## Load

```
python3 scripts/load_test.py --agents 2000 --workers 16 --seconds 5
```

| Metric | Value |
|---|---|
| Claim throughput | **9,344 claims/sec** |
| Claim latency | p50 0.069 ms · p95 0.107 ms · p99 4.99 ms · max 480 ms |
| Agents lost or duplicated under load | **0** |
| Safety decisions | **7,051/sec**, p99 0.22 ms |

Throughput is fine; latency is what degrades first under contention. See the
scale discussion in `docs/ARCHITECTURE.md`.
