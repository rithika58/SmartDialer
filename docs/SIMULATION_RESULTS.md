# Simulation results and honest analysis

Reproduce with:

```bash
python3 scripts/run_sim.py --seeds 4                 # the headline matrix
python3 scripts/run_sim.py --only C --seeds 6        # the hard case, more seeds
python3 scripts/run_sim.py --agents 50 --seeds 4     # a smaller floor
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

## Headline: 100 agents, 30 minutes, 4 seeds, both providers

Abandonment budget is 3%. Ranges are min..max across seeds.

### Provider A (fast, reliable, in-order)

| Scenario | Progressive | Predictive | Gain | Abandoned (predictive) |
|---|---:|---:|---:|---:|
| A: 20% / 120s | 67.7% | **82.3%** | **+14.5pp** | 0.43% (0.23–0.95%) |
| B: 50% / 90s | 75.2% | **81.1%** | **+5.9pp** | 0.76% (0.30–1.14%) |
| C: 70% / 180s | 88.0% | **89.4%** | **+1.5pp** | 0.77% (0.64–1.06%) |
| D: changing | 79.4% | **84.6%** | **+5.2pp** | 0.99% (0.74–1.18%) |

### Provider B (slow, timeouts, duplicates, out-of-order)

| Scenario | Progressive | Predictive | Gain | Abandoned (predictive) |
|---|---:|---:|---:|---:|
| A: 20% / 120s | 68.5% | **83.1%** | **+14.6pp** | 0.62% (0.36–0.98%) |
| B: 50% / 90s | 77.0% | **82.3%** | **+5.3pp** | 0.83% (0.63–1.02%) |
| C: 70% / 180s | 88.2% | **89.9%** | **+1.7pp** | 0.83% (0.56–1.13%) |
| D: changing | 80.4% | **85.7%** | **+5.3pp** | 1.29% (0.93–1.66%) |

Predictive now wins on utilization in **all eight** scenario/provider
combinations, and **all 32 predictive runs** stay inside the 3% abandonment
budget. Worst individual run: 1.66%.

### Smaller floor: 50 agents, Provider A, 4 seeds

| Scenario | Progressive | Predictive | Gain | Abandoned |
|---|---:|---:|---:|---:|
| A: 20% / 120s | 67.5% | **80.7%** | +13.2pp | 1.26% |
| B: 50% / 90s | 75.3% | **79.4%** | +4.1pp | 1.57% |
| C: 70% / 180s | 87.9% | **88.8%** | +0.9pp | 0.32% |
| D: changing | 79.4% | **83.2%** | +3.8pp | 0.73% |

Smaller floors are lumpier, so gains are smaller and abandonment is higher —
but the ordering is unchanged.

---

## Reading these numbers

### Where predictive wins, and why

The gain is largest where progressive wastes the most agent time. Progressive
holds an agent for the entire ring time of *every* dial, including the ones
nobody answers, so expected ring time wasted per completed conversation is
`ring / p`:

| Scenario | Ring waste per answer | As share of agent time | Gain available |
|---|---|---|---|
| A: p=0.20, ring 8s | 40s | 40 / 172 = **23%** | large (+14.5pp observed) |
| B: p=0.50, ring 8s | 16s | 16 / 118 = **14%** | moderate (+5.9pp) |
| C: p=0.70, ring 8s | 11.4s | 11.4 / 203 = **5.6%** | small (+1.5pp) |

The observed gains track that table closely. **At a 20% answer rate there is a
lot to win; at 70% there is very little.** That is the single most useful thing
this simulator shows, and it is a per-campaign property an operator should be
able to see before choosing a mode.

### Progressive's 0% abandon rate is structural, not luck

Progressive reserves the agent *before* dialing, so a borrower who answers
always has somebody. Abandonment is not merely unlikely, it is impossible by
construction. Predictive gives that up and buys it back with a statistical
guard, which is why the numbers above are a trade and not a free win.

### Occupancy is the tell

Progressive shows 97–99% occupancy but only 68–88% talk utilization: agents are
"busy" while the phone rings. Predictive shows lower occupancy (92–96%) with
*higher* talk utilization: less busy-waiting, more actual talking. Occupancy is
a vanity metric; talk utilization is the one that pays.

---

## How scenario C got fixed

Scenario C (70% answer rate, 180s conversations) previously ran at **4.29%**
abandoned on Provider A and **6.86%** on Provider B, while *losing* 4.9pp and
3.2pp of utilization against progressive. It was the weakest result in the
submission. It is now the second-best gain in the matrix.

Five defects were found by instrumenting the run rather than by reasoning about
it. They are listed here because the diagnosis is more interesting than the fix.

### 1. The service-time estimate was wrong for the first 10% of every shift

The estimator was an EWMA over *completed* calls. With 180s conversations
nothing completes for ~180s, so for the whole of that period the estimate was
simply its prior: 90s talk + 12s wrap = **102s, when the truth was 192s**. Every
capacity calculation was therefore sized for a pipeline 1.88× too large, and
that is exactly where the first 13 abandons of the run came from.

*Fix* — `stats.ServiceEstimate` runs two estimators and plans against the
pessimistic one. `completed_mean` is the EWMA over finished calls; `turnover` is
`busy_agent_seconds / agents_freed`, the reciprocal of the observed rate at
which agents come back. Turnover needs no completed call, and it is *enormous*
while no agent has freed — which is the honest belief, because we have not yet
watched a single agent return and have no evidence that any will. Once enough
calls have finished, the measured mean takes over: turnover is a lagging proxy,
biased high by the very warm-up it exists to cover, and leaving it in charge
over-estimated service by 3× and throttled the dialer into near-idleness.

The Safety Controller also gained a matching cold-start gate: predicting when an
agent will be free requires having watched some become free. Long calls delay
that evidence, so this gate is what makes the dialer conservative *specifically*
in the long-call regime.

### 2. The pipeline level was also acting as a refill rate limit

`target_in_flight` was a *level*. With long calls, calls end in synchronised
waves — a batch dialed together finishes together — so forty agents would free
at once and the dialer could only replace them `level` at a time. The floor sat
idle for a hundred seconds while borrowers went uncalled. This was the single
largest source of the utilization deficit.

*Fix* — agents who are free *right now* need no prediction at all: they can take
an answer the moment it arrives, so each one supports `1/p` dialed calls. The
target became `(spare + supply) / p`. The same term was added to the
controller's independently-recomputed pipeline ceiling, since otherwise the
controller would simply veto the burst.

### 3. Flow balance was matching means with no margin

Flow balance sets `target_in_flight` so that expected answers equal expected
agent frees. That leaves *zero* safety stock, and the variance is what actually
produces abandons. At 180s service only ~4 agents come back per ring window, so
10% of that is 0.4 of an agent — while the standard deviation of that same
count is ~2.0 agents. The margin was five times smaller than the noise it was
supposed to absorb. That is why C abandoned 5% while B, at the identical
nominal occupancy, abandoned 0.7%.

*Fix* — buy slack proportional to the spread, solved in closed form: the largest
pipeline `T` with

```
T·p + z·sqrt(T·p·(1-p) + Var[agents freeing])  ≤  agents available
```

Substituting `s = sqrt(T·p·(1-p) + V)` and `T·p = A - z·s` gives a quadratic in
`s`, so this costs a square root rather than a search. It is conservative about
long calls *for free*: the count of agents finishing in a ring window is roughly
Binomial(busy, ring/service), whose coefficient of variation rises as service
grows, so lengthening calls both slows the supply rate and makes it lumpier and
the safety stock grows on both counts.

### 4. The Safety Controller counted agent supply twice

`capacity` already included `agents_free_within_horizon`, and then
`at_risk_agents`/`q_free` modelled the *same* agents again inside
`abandon_probability`. On a 100-agent floor this pushed `max_safe_in_flight` to
**69** unanswered calls. Only the pipeline ceiling was preventing it.

*Fix* — supply is modelled once. `capacity` is the agents free right now, minus
borrowers already holding who have first claim; the agents who will free within
the horizon are modelled exclusively by the `at_risk`/`q_free` convolution.

### 5. Reaction signals were too slow

The emergency brake windowed on **the last 100 answers**, which is ~50s in
scenario B but **~200s** in scenario C — longer than the incident it exists to
catch. And every correction was triggered by a *recorded* abandon, so it landed
a full patience window after the over-dial that caused it, by which time the
next batch had been dialed into the same jam.

*Fixes* —
- the abandon window is now measured in seconds, not answers;
- the controller's hold-queue response is **graded** rather than binary: braking
  in proportion to how much of the patience budget the queue has already eaten
  starts several seconds earlier, while the people waiting can still be served.
  A gate that only trips once the queue is full is too late to help anyone in
  it;
- the adaptive loop gained a fast path that backs off on the **hold queue**
  rather than on the bill. Depth 2 is the threshold because that is where
  abandons actually happen: measured over the unfixed run, 76% of abandons
  occurred with two or more borrowers already waiting, a lone waiter usually
  being served by the next agent to free.

The emergency brake went from firing **453 times** in a 30-minute scenario C run
to **3** — it is now a genuine last resort rather than the primary control.

### What I tried and rejected

**Erlang-A.** The obvious model — `P(abandon) = C(c,a)·exp(-(c-a)·patience/service)`
— predicts 3% abandonment at ρ≈0.83 for every scenario and ~17% at the ρ=0.9 the
dialer actually runs at. It is wrong by 3–5×, and the reason is instructive:
Erlang-A assumes arrivals continue at a fixed rate regardless of congestion,
but the dialer's whole job is to *stop dialing* when the floor is busy. Any
open-loop queueing model is blind to the feedback loop that is the product. I
kept the measurement-based safety stock instead.

---

## Where this still falls short

- **The safety-stock constant `z` is calibrated, not derived.** It is 2.25
  standard deviations, chosen because it is the point where measured utilisation
  stops improving while measured abandonment keeps falling. It is one number,
  it is in `PacingEngine.__init__`, and it is covered by a test that checks the
  closed form against a brute-force search — but it is tuned on these four
  scenarios and would want re-checking against real traffic.
- **Agent supply is modelled as Binomial, not by residual service time.** We
  know how long each agent has been talking; using per-agent elapsed time
  against an empirical service distribution would sharpen the estimate further.
  The aggregate rate was chosen because it is O(1) and does not drift, and
  because the alternative needs a service-time histogram the current store does
  not carry.
- **`DialerWorker.holding` is per-worker in memory.** With N workers, a borrower
  holding on worker 1 cannot be served by an agent freed on worker 2. This is
  the first thing I would change; see `docs/ARCHITECTURE.md`.
- **Scenario D's regime change is tracked, not anticipated.** The estimator
  follows the drift within ~30s; it does not detect that conditions have changed
  and reset faster.
- **One seed proves nothing.** Every table above is a mean over 4 seeds with the
  range shown. Individual runs vary by a point or more.

---

## Load

```
python3 scripts/load_test.py --agents 2000 --workers 16 --seconds 5
```

| Metric | Value |
|---|---|
| Claim throughput | **11,278 claims/sec** (400 agents / 4 workers) |
| Safety decisions | **6,339/sec**, p99 0.029 ms |
| Agents lost or duplicated under load | **0** |

Throughput is fine; latency is what degrades first under contention. See the
scale discussion in `docs/ARCHITECTURE.md`.
