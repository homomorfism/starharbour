# StarHarbour — Order-Ahead Fulfilment Workflow

A durable, long-running order workflow for a mobile coffee order-ahead system,
implemented as a **self-contained event-sourced workflow engine in Python**.

One `Order` workflow instance orchestrates the whole business process — validate,
reserve inventory, take payment, hand off to a barista, notify, wait for pickup,
accrue loyalty — and stays alive (possibly 30+ minutes) reacting to external
signals and timers, surviving process restarts without losing state or
double-charging.

> Implements `docs/requirements.md`. See `docs/REPORT.md` for the engine-choice
> justification, the determinism explanation, and the compensation policy, and
> `docs/state-diagram.md` for the state machine.

---

## Why a custom engine (short version)

The assignment allows Cadence / Temporal / Step Functions / *your own*. I built a
small **decider + event-sourcing** engine — the same model Temporal and Cadence
use internally — because:

- it makes the **determinism constraint** and **replay/recovery** explicit and
  testable in-process, with zero external infrastructure (no Cassandra, no
  Docker, no AWS);
- the grading is about **workflow design** (the state machine, signals, timers,
  retries, compensation), not about operating a cluster;
- idempotency and compensation are the engineer's job under *any* engine, so the
  design transfers directly to Temporal/Cadence.

Full justification and the comparison to the real engines is in
[`docs/REPORT.md`](docs/REPORT.md).

---

## Architecture at a glance

```
            external signals / timers
                      │
                      ▼
   ┌──────────────────────────────────────────┐
   │  WorkflowRuntime  (engine.py)             │   ← non-deterministic: IO,
   │  • runs activities with retry/backoff     │     clock, retries, persistence
   │  • arms/fires timers                      │
   │  • appends every event to the EventStore  │
   └───────────────┬──────────────────────────┘
        decide()   │   apply()          append / load
        (intent)   ▼   (evolve)              │
   ┌──────────────────────────────┐   ┌──────▼─────────┐
   │  OrderWorkflow  (workflow.py) │   │  EventStore    │
   │  PURE: decide(state)->Effects │   │  (store.py)    │
   │        apply(state,ev)->state │   │  in-mem / file │
   └──────────────────────────────┘   └────────────────┘
```

The split is the whole idea: **all business logic is a pure function of durable
state** (`decide`/`apply`, no IO), and the runtime is the only thing that touches
the world. State is never stored directly — it is rebuilt by folding the event
log, so a crashed worker recovers by replay.

| File | Responsibility |
|------|----------------|
| `src/starharbour/domain.py`     | `OrderState`, `OrderStatus`, line items, pricing |
| `src/starharbour/events.py`     | the append-only event types (the durable history) |
| `src/starharbour/workflow.py`   | **pure** `apply` (evolve) + `OrderWorkflow.decide` |
| `src/starharbour/activities.py` | side-effecting steps + failure mappers |
| `src/starharbour/clients.py`    | fake downstreams: Payments (idempotent), Inventory, Loyalty, Barista, Notifier |
| `src/starharbour/engine.py`     | the durable runtime: retries, timers, persistence, recovery |
| `src/starharbour/store.py`      | in-memory & JSON-file event stores |
| `src/starharbour/demo.py`       | runnable demo of every scenario |

---

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```bash
uv sync          # create the venv and install dev deps (pytest)
```

## Run the demos

The "engine" and "worker" are the same in-process runtime; each demo starts a
fresh order, drives it, and prints the event log + final state.

```bash
uv run starharbour-demo all          # run every scenario

# or one at a time:
uv run starharbour-demo happy        # happy path → COMPLETED
uv run starharbour-demo F1           # payment times out 3× then succeeds (one charge)
uv run starharbour-demo F1-fail      # payment never recovers → FAILED
uv run starharbour-demo F2           # payment permanently declined → FAILED
uv run starharbour-demo F3-accept    # out of stock, substitution accepted → COMPLETED
uv run starharbour-demo F3-decline   # out of stock, substitution declined → CANCELLED
uv run starharbour-demo F3-timeout   # out of stock, no answer in time → CANCELLED
uv run starharbour-demo F4           # cancel before drinks → refund+release → CANCELLED
uv run starharbour-demo F5           # never collected → ABANDONED
uv run starharbour-demo sla          # barista too slow → escalate, still COMPLETED
uv run starharbour-demo F6           # worker crash mid-order → resume, no double charge
```

## Run the tests

```bash
uv run pytest            # full suite (unit + integration)
uv run pytest -q
uv run pytest tests/test_unit_workflow.py        # pure state-machine units
uv run pytest tests/test_integration_scenarios.py # F1–F5, SLA, validation
uv run pytest tests/test_integration_durability.py # replay determinism + F6
```

### Test layout

| File | Covers |
|------|--------|
| `tests/test_unit_workflow.py`        | the pure `apply`/`decide` core — every transition |
| `tests/test_unit_clients.py`         | idempotent charge/refund, out-of-stock, retryability |
| `tests/test_integration_scenarios.py`| F1–F5, brew-SLA escalation, late signals, validation, happy path end-to-end |
| `tests/test_integration_durability.py`| replay determinism, JSON persistence round-trip, **F6** crash recovery |

---

## Scenario → behaviour mapping

| # | Scenario | Terminal status | Key guarantees |
|---|----------|-----------------|----------------|
| happy | normal order | `COMPLETED` | one charge, loyalty accrued |
| F1 | payment times out, then succeeds | `COMPLETED` | retried w/ backoff, **exactly one charge** |
| F2 | payment permanently declined | `FAILED` | inventory released, customer notified, **no loyalty** |
| F3 | item out of stock | `COMPLETED` / `CANCELLED` | substitution offered; accept → continue; decline/timeout → cancel |
| F4 | cancel before drinks made | `CANCELLED` | refund + release inventory |
| F5 | customer never collects | `ABANDONED` | pickup timer fires; waste policy (no refund) |
| F6 | worker crash mid-order | resumes | replay from log, **no double charge, no lost state** |
| SLA | barista too slow | `COMPLETED` | brew-SLA timer escalates (notify manager), order continues |
