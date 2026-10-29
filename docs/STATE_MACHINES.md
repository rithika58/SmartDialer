# State machines

Both machines live in `src/smartdialer/domain/states.py`, and the transition
logic is a pure function so it can be tested exhaustively without a database.

---

## Agent lifecycle

```
                    +---------------------------+
                    |         OFFLINE           |
                    +-------------+-------------+
                                  | agent logs in
                                  v
   +---------------------------------------------------------------+
   |                          AVAILABLE                            |
   +---+-------------+-------------+--------------+----------------+
       |             |             |              ^
       | reserve     | pause       | log off      | wrap-up done
       v             v             v              |
  +----------+  +---------+   (OFFLINE)      +---------+
  | RESERVED |  | PAUSED  |                  | WRAP_UP |
  +---+--+---+  +----+----+                  +----^----+
      |  |           |                            |
      |  | release   | resume                     | call ends
      |  | (no       +----------------------------+
      |  | borrower,                              |
      |  | lease                            CONNECTED
      |  | expiry)                                ^
      |  v                                        | bridged
      | (AVAILABLE)                               |
      |                                      +----+----+
      +----------> DIALING ----------------> |          |
                    |  |                     +----------+
                    |  | call failed /            ^
                    |  | no answer                |
                    |  +--> AVAILABLE             |
                    |                             |
                    +--> OFFLINE  (agent vanished |
                         during setup) -------------+
```

| From | To | Why |
|---|---|---|
| `OFFLINE` | `AVAILABLE` | agent logs in |
| `AVAILABLE` | `RESERVED` | claimed for a call (**the atomic step**) |
| `AVAILABLE` | `PAUSED`, `OFFLINE` | break, end of shift |
| `RESERVED` | `DIALING` | provider accepted the call |
| `RESERVED` | `AVAILABLE` | no borrower to call, or lease expired |
| `RESERVED` | `OFFLINE` | **agent disappears during setup** |
| `DIALING` | `CONNECTED` | borrower answered and was bridged |
| `DIALING` | `AVAILABLE` | call failed before answer |
| `DIALING` | `OFFLINE` | **agent disappears mid-dial** |
| `CONNECTED` | `WRAP_UP` | conversation ended, notes to write |
| `WRAP_UP` | `AVAILABLE` | ready again |
| `PAUSED` | `AVAILABLE`, `OFFLINE` | back, or done |

Two things are deliberate:

- `CONNECTED → AVAILABLE` is **not** legal. An agent cannot teleport out of a
  live conversation; they must go through `WRAP_UP`. This is what stops a late
  event from silently recycling an agent who is still talking.
- `RESERVED → OFFLINE` and `DIALING → OFFLINE` are legal, because agents really
  do vanish, and the machine has to be able to represent it.

---

## Call lifecycle

```
  QUEUED
    |  borrower claimed
    v
  RESERVED
    |  handed to the provider
    v
  INITIATED
    |
    v
  RINGING ----------> ANSWERED ----------> CONNECTED
    |                    |                     |
    |                    |                     v
    +--------------------+----------------> COMPLETED
    |                    |                     ^
    +--------------------+----------------> FAILED
    |                    |                     ^
    +--------------------+----------------> CANCELLED
```

| From | Allowed next |
|---|---|
| `QUEUED` | `RESERVED`, `CANCELLED` |
| `RESERVED` | `INITIATED`, `FAILED`, `CANCELLED` |
| `INITIATED` | `RINGING`, `ANSWERED`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `RINGING` | `ANSWERED`, `CONNECTED`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `ANSWERED` | `CONNECTED`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `CONNECTED` | `COMPLETED`, `FAILED`, `CANCELLED` |
| `COMPLETED` / `FAILED` / `CANCELLED` | *(nothing — absorbing)* |

`INITIATED → ANSWERED` and `RINGING → CONNECTED` are both allowed because
real providers skip `RINGING` or collapse `ANSWERED` and `CONNECTED`. We do not
get to choose the provider's vocabulary.

---

## Three rules that make hostile events harmless

Every provider event is filtered through three independent checks, in this
order. They catch different failures, so none of them is optional.

### 1. Idempotency — same `event_id` seen before

The provider's own event id is the PRIMARY KEY of a ledger table:

```sql
CREATE TABLE processed_events (event_id TEXT PRIMARY KEY, ...);
```

A redelivery fails the insert and never reaches the state machine.

```
ANSWERED (id=E7)  -> APPLIED
ANSWERED (id=E7)  -> DUPLICATE   (no second transition)
```

### 2. Ordering — monotonic per-call sequence

```python
if seq <= call.last_seq: reject as STALE_SEQ
```

### 3. Legality — the transition table plus a rank check

Every non-terminal state has a rank, and a transition may only move the call
**forward**. Terminal states are absorbing.

```
rank:  QUEUED 0  RESERVED 1  INITIATED 2  RINGING 3  ANSWERED 4  CONNECTED 5  terminal 6
```

---

## The three sequences from the assignment, traced

**`ANSWERED, ANSWERED, ANSWERED, COMPLETED`**

| Event | Current | Verdict |
|---|---|---|
| `ANSWERED` seq 3 | `RINGING` (3) | APPLIED → `ANSWERED` (4) |
| `ANSWERED` seq 4 | `ANSWERED` (4) | rejected — not a legal transition, and would not move forward |
| `ANSWERED` seq 5 | `ANSWERED` (4) | rejected — same reason |
| `COMPLETED` seq 6 | `ANSWERED` (4) | APPLIED → `COMPLETED` (6) |

One transition is recorded. `tests/test_state_machines.py::test_duplicate_answered_then_completed`.

**`COMPLETED, ANSWERED, RINGING`**

`COMPLETED` is terminal. Both later events are rejected as `TERMINAL`. The call
stays `COMPLETED` and the audit log gains no phantom rows.

**`COMPLETED, ANSWERED, RINGING` where the worker crashes after `ANSWERED`**

The worker never processes anything, so the store still shows `RINGING`. The
lease expires, recovery cancels the call as `WORKER_CRASH`, and the late
`COMPLETED` from the provider is then rejected as `TERMINAL`. Final state is
consistent, and the agent is back in `AVAILABLE`.

**Provider B's out-of-order delivery** (`ANSWERED` before `RINGING`)

| Event | Current | Verdict |
|---|---|---|
| `INITIATED` seq 1 | `RESERVED` | APPLIED |
| `ANSWERED` seq 3 | `INITIATED` (2) | APPLIED → `ANSWERED` (4) — legal, moves forward |
| `RINGING` seq 2 | `ANSWERED` (4) | rejected — `STALE_SEQ` (2 ≤ 3); the rank check would reject it too |

Order of arrival never wins over the provider's own sequence number.

---

## Mirroring calls onto agents

When a call moves, the agent usually moves with it:

| Call reaches | Agent becomes |
|---|---|
| `INITIATED` | `DIALING` |
| `ANSWERED` | `DIALING` (bridging) |
| `CONNECTED` | `CONNECTED` |
| `COMPLETED` | `WRAP_UP` (then `AVAILABLE` after wrap) |
| `FAILED` / `CANCELLED` | `AVAILABLE` immediately |

In **predictive** mode the agent is bound at `ANSWERED`, not at dial time —
that is the whole difference between the two modes.
