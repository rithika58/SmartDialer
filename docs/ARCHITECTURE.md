# Architecture decision record

---

## 1. System shape

```
                       +-----------------------------------+
                       |             Campaign              |
                       +-----------------+-----------------+
                                         |
                                         v
   +=========================+    proposes N      +=====================+
   |      PACING ENGINE      |  --------------->  |   SAFETY CONTROLLER |
   |  pure function of a     |                    |  approve / reduce /  |
   |  snapshot; no I/O, no   |  <---------------  |  reject / fall back  |
   |  store, no provider     |   PacingDecision   +----------+----------+
   +=========================+                               |
                                                             | SafetyDecision
                                                             | signed (HMAC),
                                                             | single-use budget
                                                             v
                                                  +----------------------+
                                                  |    CALL ALLOCATOR    |
                                                  |  claim borrower      |
                                                  |  claim agent         |
                                                  |  create call         |
                                                  +----------+-----------+
                                                             |
                                                             v
                                                  +----------------------+
                                                  |   PROVIDER GATEWAY   |
                                                  |  owns the providers  |
                                                  |  verifies approval   |
                                                  |  circuit breaker     |
                                                  +----------+-----------+
                                                             |
                                        +--------------------+--------------------+
                                        v                                         v
                                +---------------+                         +---------------+
                                |  Provider A   |                         |  Provider B   |
                                |  fast, clean  |                         |  slow, dupes, |
                                +---------------+                         |  out of order |
                                        |                                         |
                                        +--------------------+--------------------+
                                                             | async events
                                                             v
                                                  +----------------------+
                                                  |    DIALER WORKER     |
                                                  |  idempotent apply    |
                                                  |  order-aware         |
                                                  |  hold queue          |
                                                  |  lease recovery      |
                                                  +----------+-----------+
                                                             |
                                                             v
                                        +-----------------------------------+
                                        |   SQLite (WAL)  /  MemoryStore    |
                                        +-----------------------------------+
```

---

## 2. Stack decisions

The brief says there is no correct stack and asks: what did you choose, why,
what problem does it solve, what does it make harder?

### Python 3.13, standard library only

**Why** — a reviewer can run this with `python3` and nothing else. No Docker,
no broker, no database server. The brief explicitly warns against adding
technology for its own sake.

**Solves** — reviewability. Every line of the concurrency-critical path is
visible without framework indirection.

**Makes harder** — throughput. Python is ~50–100× slower than Go or Rust for
this kind of tight loop, so the per-decision cost matters at scale. Measured:
**7,051 safety decisions/sec**, which is not the bottleneck at any realistic
campaign size (a 10,000-agent floor needs ~1 decision per second per campaign).

### SQLite in WAL mode

**Why** — the hardest requirement is *"two workers see the same available agent
at almost the same time; both must not be able to reserve that agent."* That is
a compare-and-set problem, and SQLite solves it in one statement:

```sql
UPDATE agents
   SET state = 'RESERVED', lease_owner = ?, lease_expires_at = ?
 WHERE agent_id = (SELECT agent_id FROM agents
                    WHERE campaign_id = ? AND state = 'AVAILABLE'
                    ORDER BY state_since ASC LIMIT 1)
   AND state = 'AVAILABLE'
```

`rowcount == 1` means we won; `0` means someone else did. There is no
read-then-write window, so there is no race — not in one process, not across
processes. The race test runs against **both** the SQLite store and the
in-memory store, which proves the guarantee comes from the data model rather
than from SQLite's locking or the GIL.

**Solves** — agent allocation correctness, with zero operational surface.

**Makes harder** —
- One writer at a time. Measured **9,344 claims/sec** with 16 threads
  (p50 0.069 ms), which is ample, but p99 was 4.99 ms and the max 480 ms:
  latency gets ugly long before throughput does.
- No cross-machine access without moving to a client/server database.
- Busy-timeout tuning under contention.

### Injected `Clock` and `Scheduler`

Every component takes a clock and a scheduler. Production uses `RealClock`; the
simulator and tests use `VirtualClock`.

**Why** — this is the decision that makes the system testable. Eight hours of
dialing runs in under a second, deterministically, with the *real* pacing
engine, safety controller and providers. Only time is fake.

**Makes harder** — nothing, beyond a little plumbing. This was the highest-value
decision in the project.

### No Kafka, Redis, or microservices

**Why** — the brief asks us not to add them unless they solve a problem we
actually have. At 100–1,000 agents a single process with one SQLite file has no
problem that a queue would fix; it would add three failure modes and a
deployment.

**Makes harder** — horizontal scaling (section 6).

### Store abstraction (`DialerStore`)

`MemoryStore` and `SQLiteStore` implement the same interface. The simulator uses
the in-memory one for speed; the concurrency tests use SQLite for realism.

**Makes harder** — a small risk of behavioural drift between the two. Mitigated
by parametrising the concurrency tests over both.

---

## 3. The safety boundary (three independent barriers)

The requirement is that the predictive algorithm **cannot** switch the safety
mechanism off, and must never place a call directly.

**Barrier 1 — structural.** `PacingEngine` is a pure function over a
`PacingInputs` snapshot. It holds no store, no provider, no gateway. Providers
live behind `ProviderGateway`, which is only ever handed to the allocator, and
`gateway.place_call()` requires an approval object. There is no code path from
pacing to provider. A test asserts this by inspecting the objects.

**Barrier 2 — cryptographic.** An approval is HMAC-SHA256 signed with a
per-controller secret generated at construction. Forged approvals fail
verification.

**Barrier 3 — single-use budget.** An approval authorises `allowed_calls`
calls — not one, not unlimited. Each placement draws down the budget; once
spent, the approval stops working, so it cannot be replayed to dial more than
the controller permitted. It also expires after `approval_ttl_seconds`.

There is deliberately **no** config flag, environment variable, or admin
endpoint that disables any of these. Turning them off would be a code change
with a reviewer, which is the point.

### What the controller enforces

Checked in order; the first failure decides.

| # | Check | Action |
|---|---|---|
| 1 | Provider circuit breaker open | reject everything |
| 2 | Windowed abandon rate above the brake | fall back to progressive |
| 3 | Answer-rate confidence below threshold (cold start) | fall back to progressive |
| 4 | Expected agent wait exceeds borrower patience | stop adding calls |
| 5 | Pipeline ceiling — the floor cannot drain more than `on_shift / service` answers/sec | reduce |
| 6 | Probabilistic abandonment guard — `P(answers > agents free) ≤ 3%` | reduce |
| 7 | Hard in-flight backstop — never more unanswered calls than agents | reduce |
| 8 | Concurrency ceiling, rate limit, queue depth | reduce |

Checks 4, 5 and 7 are **deterministic**. That matters, because checks 6 depends
on `p_answer` being roughly right — if the estimator is wrong, the
probabilistic guard is quietly wrong with it. The deterministic checks are the
backstop for exactly that case, and they are why the system degrades gracefully
instead of failing.

---

## 4. Pacing

### The arithmetic

The floor completes a call every `service` seconds, so it needs `N / service`
answers per second. Each dial yields an answer after `ring` seconds with
probability `p`. To sustain `A` answers/sec we must dial `A/p` per second, so:

```
target_in_flight = occupancy × N × ring / (service × p)
```

We then request only the difference between that target and what is already
ringing, so the loop is self-limiting.

**Two things I got wrong first, worth recording:**

1. My first version asked "how many calls would fill the agents I have?" and
   tried to place them all at once. That fills the entire pipeline in a single
   tick, so they are all answered at the same moment — which is precisely the
   burst that produces abandoned calls.

2. The raw flow balance targets **ρ = 1.0**. That is not an operating point; it
   is a queue that grows without bound. At ρ = 1 the expected wait for an agent
   diverges and every answered call is eventually abandoned. Hence the explicit
   `target_occupancy = 0.90`: the leftover is the slack that absorbs arrival
   lumpiness, and it is what turns a fantasy into a ~3% abandon reality.

### The closed loop

The arithmetic is a mean-flow estimate, so it is systematically wrong in the
ways that matter (arrival lumpiness, talk-time variance, estimator bias). Rather
than pretend to model all of that, we measure it — the same way TCP discovers a
bandwidth-delay product it was never told:

- abandon **rate** above target → multiplicative decrease
- agents idle → additive increase, scaled by how much capacity is wasted
- otherwise → hold, with a deadband so the loop does not hunt

It targets a *rate*, not individual abandoned calls: at ~0.5 answers/sec a
single abandon inside a 30-second window is a 6% rate, and reacting to that
makes the dialer oscillate.

---

## 5. Failure handling

| Failure | Mechanism | Recovery |
|---|---|---|
| Worker crash | Lease expiry, not failure detection | `expire_leases()` returns the agent, cancels the call as `WORKER_CRASH`, requeues the borrower |
| Provider outage | Circuit breaker (closed → open → half-open) | New calls refused; in-flight left alone; probe traffic resumes automatically |
| Agents vanish | Next tick recomputes the ceiling from agents still on shift | Pipeline resizes within one tick (measured: 15 → 9 within 3s of a 40% drop) |
| Duplicate events | `event_id` PRIMARY KEY ledger | Absorbed before the state machine sees it |
| Out-of-order events | Monotonic per-call `seq` + forward-only state rank | Rejected as `STALE_SEQ` / `OUT_OF_ORDER` |
| Provider goes silent | Stuck-call reaper after `stuck_call_timeout` | Call ends `PROVIDER_SILENT`, agent freed |
| Borrower answers, no agent | Hold queue with patience budget, then IVR + callback | Never silently dropped |

**On leases:** we never try to *detect* a dead worker. We stop trusting a
reservation whose owner stopped renewing. That is the only approach that
survives SIGKILL, a lost VM and a network partition identically — all three
look exactly the same from the outside.

---

## 6. Scale: 100 → 1,000 → 10,000 agents

### 100 agents — no problem
~1 claim/sec, ~5 state writes/sec. SQLite is idle. Everything is decided by one
worker on one node.

### 1,000 agents — still fine, but the shape changes
~10 claims/sec, ~50 event writes/sec. SQLite in WAL handles this with orders of
magnitude to spare. What starts to matter:

- **One worker is now a single point of failure.** The fix is not Kafka; it is
  N stateless workers against the same SQLite file, which the atomic claim
  already supports. Add a lease-renewal heartbeat and let any worker recover any
  lease.
- **Provider rate limits** become the real ceiling. External, and no amount of
  local cleverness changes that.

### 10,000 agents — what breaks first

**First: the `agents` table index becomes a hot spot.** Every claim runs the
same subselect against `idx_agents_claim (campaign_id, state, state_since)`.
All workers contend for the same narrow index range and, critically, the same
single writer lock. Measured p99 claim latency was already 4.99 ms at 9.3k
claims/sec with max 480 ms — throughput is fine, **latency is the casualty**.

Fix: stop treating agent allocation as a global query.
- Partition agents into shards (by `campaign_id`, then `agent_id % k`) and have
  each worker own shards, so claims touch disjoint index ranges.
- Then move to PostgreSQL and `SELECT ... FOR UPDATE SKIP LOCKED`, which lets
  many workers pull from one queue without blocking each other. Same
  compare-and-set semantics, no global lock.
- If even that is not enough, hold availability in Redis (a set per campaign,
  `SPOP` for atomic claim) with Postgres as the durable source of truth and the
  lease table as reconciliation. Note this reintroduces a consistency gap
  between cache and database, and you must decide which one wins — see below.

**Second: the in-memory hold queue.** `self.holding` is per-worker. With one
worker that is correct; with N workers, a borrower holding on worker 1 cannot be
served by an agent freed on worker 2. This is a genuine design flaw that only
appears once you scale out, and it is the thing I would fix first.

Fix: move the queue into the store — a `waiting_calls` table keyed by
`answered_at`, claimed atomically exactly like agents. Then any agent can serve
any waiting borrower, and the queue survives a worker crash.

**Third: `expire_leases` and the per-tick agent scans become O(n).**
`count_agents_by_state` and `agents_busy_profile` run every tick. At 10,000
agents that is a full pass per second per campaign. Fix: an index on
`lease_expires_at`, plus maintaining counters incrementally (or in Redis) rather
than recounting.

**Fourth: the single pacing decision per campaign** is a serialisation point and
a SPOF. Fix: shard by campaign, or elect a leader per campaign; the decision is
cheap enough that leader election (a lease row) is simpler than a consensus
protocol.

### "Your database says AVAILABLE but your cache says RESERVED. Which wins?"

The **database** wins, because the cache is a hint about who *might* be free and
the database is the record of who actually is. But it is not that simple, and
the answer I would give in the discussion is: **the lease is the tiebreaker, not
the state.**

An agent is genuinely allocatable only when the database says `AVAILABLE` **and**
no unexpired lease exists. A cache that says `RESERVED` with an expired lease is
stale and must be ignored. A cache that says `AVAILABLE` while the database holds
a live lease must not be trusted either, because the lease means another worker
is mid-allocation. In this design there is no cache, which is why the question
does not arise — if I added one, the lease table would be the arbiter, and the
cache would be rebuilt from it on any disagreement.

---

## 7. The final question

> *How would you build a SmartDialer that gets as much of the utilization
> benefit of predictive dialing as possible, while retaining the deterministic
> safety characteristics of progressive dialing?*

**Separate who is exposed to risk from how much risk there is, and make the
worst case a known constant rather than a probability.**

1. **Reserve on answer, not on dial.** Bind the agent only once a human is on
   the line, but pre-position the pipeline so the wait is normally zero. That is
   where all the utilization comes from, and it costs nothing in safety.

2. **Make the safety net deterministic even though the pacing is
   probabilistic.** Allow a hold queue, but bound it: at most `max_queue`
   borrowers, each for at most `patience` seconds, each with a guaranteed
   destination (IVR + queued callback). Now the worst case is a *constant you
   can state in advance* rather than a tail probability. You have bought a
   deterministic guarantee about the tail using a probabilistic controller in
   the middle.

3. **Keep a firebreak.** Never expose more than `1 − f` of the floor to
   predictive overbooking; the remainder runs pure progressive forever. If the
   estimator is catastrophically wrong, only a bounded slice of agents is
   affected and the campaign degrades instead of failing.

4. **Never let the estimator hold the safety guarantee.** Every probabilistic
   bound needs a deterministic backstop that does not read `p_answer` — here,
   the pipeline ceiling, the expected-wait check, and "in-flight ≤ agents on
   shift". If the model is wrong, these still hold.

5. **Fail closed.** Any doubt — cold start, provider sick, abandonment above
   budget, metric missing — resolves to progressive. The safe mode must be the
   default, not something you have to remember to switch to.

The short version: *use probability to decide how fast to go, and use hard
limits to decide where the wall is.*

---

## 8. What I would do with another week

1. **Move the hold queue into the store.** It is currently per-worker, which is
   wrong the moment you run two workers. This is the real bug in the system.
2. **Fix scenario C.** Abandonment exceeds the 3% target when the answer rate is
   high and talk times are long. See `docs/SIMULATION_RESULTS.md`.
3. **Cross-worker lease renewal.** Heartbeats and a recovery loop that runs
   independently of the dialer, so a lease can be reclaimed by a different
   worker.
4. **Per-campaign mode selection.** The simulator shows predictive wins at low
   answer rates and loses at high ones. That is a per-campaign policy decision,
   and it should be automatic rather than an operator toggle.
5. **Property-based tests** (Hypothesis) over random event orderings, rather
   than the hand-written sequences in `tests/test_state_machines.py`.
