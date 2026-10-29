# SmartDialer

A working prototype of a collections dialer with two pacing modes, an
unbypassable safety controller, and honest handling of everything that goes
wrong in a real telecom integration.

Built for the *Tech Assignment – Hiring 2026*.

---

## Quick start

```bash
cd smartdialer
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install pytest                                   # the ONLY dependency

pytest                                     # 49 tests
python3 scripts/run_sim.py                 # simulation matrix
python3 scripts/demo_failures.py           # the five failure cases
python3 scripts/load_test.py               # concurrency + throughput
python3 scripts/gen_dashboard.py           # writes dashboard.html
```

Python 3.10+. No database server, no Docker, no message broker, no network.
Every script adds `src` to `sys.path` itself, so **you do not need to set
`PYTHONPATH`** — `pytest` is configured the same way via `pyproject.toml`.

---

## What is actually here

| Requirement | Where |
|---|---|
| Agent state machine | `src/smartdialer/domain/states.py`, `docs/STATE_MACHINES.md` |
| Call state machine | `src/smartdialer/domain/states.py`, `docs/STATE_MACHINES.md` |
| Progressive dialer | `src/smartdialer/engine/pacing.py` + `allocator.py` (`reserve_agent=True`) |
| Predictive pacing engine | `src/smartdialer/engine/pacing.py` |
| Safety Controller | `src/smartdialer/engine/safety.py` |
| Call Allocator / provider gateway | `src/smartdialer/engine/allocator.py` |
| Mock telecom providers (A and B) | `src/smartdialer/providers/mock.py` |
| Worker, recovery, holding queue | `src/smartdialer/engine/dialer.py` |
| Tests | `tests/` (49) |
| Simulation | `src/smartdialer/sim/`, `scripts/run_sim.py` |
| Load test | `scripts/load_test.py` |
| Operations dashboard | `scripts/gen_dashboard.py` → `dashboard.html` |
| Architecture decision record | `docs/ARCHITECTURE.md` |
| Results and honest analysis | `docs/SIMULATION_RESULTS.md` |

```
src/smartdialer/
  domain/       states.py, models.py          -- the rules, with no I/O
  stores/       base.py, memory.py, sqlite_store.py
  providers/    base.py, mock.py              -- Provider A and Provider B
  engine/       pacing.py, safety.py, allocator.py, dialer.py
  sim/          simulator.py                  -- virtual clock, real components
  clock.py      Clock injection
  stats.py      beta-binomial estimate, binomial tails, abandon probability
```

---

## Running in VS Code

### 1. Open the folder, not a file

`File → Open Folder…` and pick **`smartdialer`** (the folder containing
`pyproject.toml`). Opening a single `.py` file instead will make the imports
resolve incorrectly — a very common VS Code Python gotcha.

### 2. Install the recommended extensions

VS Code will pop up a "recommended extensions" prompt from
`.vscode/extensions.json`. Accept it, or install manually:

- **Python** (`ms-python.python`)
- **Pylance** (`ms-python.vscode-pylance`)
- **Live Preview** (`ms-vscode.live-server`) — only needed to view the dashboard

### 3. Select your interpreter

`Ctrl/Cmd + Shift + P` → **Python: Select Interpreter**

- If you made a venv: pick `./.venv/bin/python` (`.venv\Scripts\python.exe` on Windows)
- If not: pick your system `python3` — that works fine, since the runtime is stdlib-only

Everything in `.vscode/settings.json` already points Pylance at `src`, so
`import smartdialer…` resolves and **go-to-definition works** without
installing anything.

### 4. Run things

**Tests** — click the flask icon in the Activity Bar, or `Ctrl/Cmd + ; P` →
*Tasks: Run Test Task*. The 49 tests are discovered from `tests/`.

**Anything else** — `Ctrl/Cmd + Shift + B` → *Tasks: Run Build Task*, or
`Ctrl/Cmd + Shift + P` → *Tasks: Run Task*. You get:

| Task | What it does |
|---|---|
| Tests | `pytest tests` |
| Simulation matrix | A–D, both modes, both providers |
| Failure demo | the five failure cases, narrated |
| Load test | 2,000 agents, 16 threads |
| Generate dashboard | writes `dashboard.html` |
| Install (editable, optional) | creates `.venv` and runs `pip install -e '.[dev]'` |

**Debugging** — press `F5` and pick a configuration. The `.vscode/launch.json`
has eight, including one for the whole matrix, one per script, both pytest
modes, and one titled **"Debug: the two-workers-one-agent race"**. That last one
is the one to reach for when you want to step through `SQLiteStore.claim_agent`
line by line ahead of the technical discussion.

### 5. View the dashboard

Right-click `dashboard.html` → **Show Preview** (Live Preview extension), or
just open the file in a browser. It is a single self-contained file — no server,
no network, no build step.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: smartdialer` | You opened a file instead of the folder, or your interpreter is wrong. Re-open the folder and re-select the interpreter. |
| Tests found but imports underlined in red | Pylance is using the wrong interpreter. `Python: Select Interpreter` again. |
| `pytest: command not found` | Use `python3 -m pytest` instead, or activate your venv first. |
| Tests pass in terminal but not in the test explorer | `Python: Clear Cache and Reload Window`. |
| Live Preview shows a blank page | Open `dashboard.html` directly in a browser instead. |

### You do not need the editable install

`pip install -e '.[dev]'` is included as a convenience task, but it is
**optional**. The scripts self-configure `sys.path` and `pyproject.toml` sets
`pythonpath = ["src"]` for pytest, so nothing depends on installation.

---

## The pipeline

```
   Campaign
      |
      v
 +-----------------+      proposes N calls       +--------------------+
 |  Pacing Engine  |  -------------------------> |  Safety Controller |
 |  (pure function |      "I think we can        |  approve / reduce / |
 |   over a        |       start N more"         |  reject / fall back |
 |   snapshot)     |                             +--------------------+
 +-----------------+                                       |
                                                           | signed, single-use
                                                           | approval (budget N)
                                                           v
                                                  +--------------------+
                                                  |   Call Allocator   |
                                                  +--------------------+
                                                           |
                                                           v
                                                  +--------------------+
                                                  |  Provider Gateway  |  <-- owns the providers
                                                  +--------------------+      verifies every approval
                                                           |
                                                           v
                                                  +--------------------+
                                                  |  Telecom Provider  |
                                                  +--------------------+
                                                           |
                                                     async events
                                                           |
                                                           v
                                                     DialerWorker
                                                      (idempotent,
                                                       order-aware)
```

The pacing engine has no reference to the gateway, the store, or the network.
It is a pure function of a snapshot. That is not tidiness — it is the first
of three independent barriers that make the safety boundary unbypassable. See
`docs/ARCHITECTURE.md`.

---

## The two modes in one sentence each

**Progressive** — reserve the agent *before* dialing, so a borrower who picks up
always has someone to talk to. Guaranteed safe; the cost is that agents sit
idle while the phone rings.

**Predictive** — reserve only the borrower and dial ahead, binding an agent
when the borrower answers. That is where the utilization comes from, and it is
also the only way an abandoned call can occur — which is exactly why the Safety
Controller exists.

---

## Running the pieces

```bash
# Full scenario matrix (A/B/C/D, both modes, both providers)
python3 scripts/run_sim.py --agents 100 --duration 1800
python3 scripts/run_sim.py --provider B        # only the hostile provider
python3 scripts/run_sim.py --json > results.json

# The five failure cases, with narrative output
python3 scripts/demo_failures.py

# Load: 9.3k atomic claims/sec with 16 threads, no double-allocation
python3 scripts/load_test.py --agents 2000 --workers 16 --seconds 5
```

---

## Test suite

```
tests/test_concurrency.py     two workers, one agent; leases; conditional writes
tests/test_state_machines.py  duplicate / out-of-order / terminal event handling
tests/test_safety.py          forged + replayed approvals, every refusal path
tests/test_providers.py       Provider B is provably worse than Provider A
tests/test_failures.py        all five failure cases end-to-end
```

`test_concurrency.py` runs the same race against **both** store
implementations. That is deliberate: it proves the guarantee comes from the
compare-and-set data model, not from SQLite's locking and not from the GIL.

---

## Design decisions worth arguing about

1. **Leases, not failure detection.** We never try to notice a dead worker; we
   stop trusting a reservation whose owner stopped renewing. Survives SIGKILL,
   a lost VM and a network partition identically.
2. **Rate-based agent availability, not per-agent finish predictions.** Any
   agent predicted to finish at T but still talking at T would otherwise be
   counted as "free soon", and on a busy floor that is about half the agents.
3. **A hold queue with a patience budget.** A borrower who answers is not hung
   up on; they wait up to `patience_seconds` for an agent. Only then is it an
   abandon — and even then they are routed to the IVR with a callback queued,
   never silently dropped.
4. **A closed loop on top of the open loop.** The pacing arithmetic is a
   mean-flow estimate, which is systematically wrong in the ways that matter,
   so an AIMD controller measures the error and corrects for it. See
   `docs/ARCHITECTURE.md`.

## Known limitations

Read `docs/SIMULATION_RESULTS.md` before judging the numbers. In short:
abandonment meets the 3% target in scenarios A, B and D, and **exceeds it in
scenario C** (70% answer rate, 180s talk). That is the part of this system I am
least confident about, and the write-up explains why.
