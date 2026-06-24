# Order-Ahead Fulfillment Workflow

**Durable Orchestration with Cadence or AWS Step Functions**

*Case study: the "StarHarbour" mobile order-ahead system*

---

## 1. Goal and Context

A real order is not a single call: it is a long-running business process that spans payment, inventory, a human barista, push notifications, timers, and the customer physically showing up — and it must survive process restarts, deploys, and partial failures without losing or double-charging an order.

In this task you design and implement that process as a durable workflow using a workflow engine. You may use **Cadence** ([get-started](https://cadenceworkflow.io)) or **Temporal** or **AWS Step Functions** or your own implementation — your choice.

> The point of this assignment is **workflow design**, not building another CRUD service. Spend your effort on the **state machine**: which steps are activities, what is signalled from outside, what is driven by timers, what is retried, and what must be compensated when things go wrong.

---

## 2. The Scenario

A customer opens the StarHarbour app and places an order ahead at `store-london-01`: 2× Latte, 1× Cold Brew, paying with a saved card and their loyalty card.

**The happy path the customer sees:**

> Order placed → Payment taken → "We're making your drinks" → "Ready for pickup!" → customer collects → loyalty points added → done.

Behind that, one Order workflow instance orchestrates the whole thing and stays alive — possibly for 30+ minutes — from "placed" until "collected" (or "abandoned"). The workflow is the single source of truth for where this order is.

### 2.1 Steps the workflow coordinates

1. **Validate & price** the order (known coffee types, store is open).
2. **Reserve inventory** at the store (beans, milk, cup sizes). May fail → out of stock.
3. **Take payment** by calling the hw1 Payments API (`POST /api/v1/payments` with an `Idempotency-Key`). May fail, time out, or be slow.
4. **Hand the ticket** to the barista queue and wait for the barista to start and then mark each drink ready.
5. **Notify the customer** "ready for pickup".
6. **Wait for pickup**, then accrue loyalty points (hw1 loyalty) and close the order.

### 2.2 Events the workflow must react to

These are why a plain request/response handler or a cron job is not enough:

- **Barista marks the order ready** — an external signal, arriving minutes later.
- **The customer collects the order** — an external signal.
- **Customer cancels** — can arrive at any point; behaviour differs before vs. after the drinks are made.
- **Item out of stock** — offer a substitution and wait for the customer to accept or decline (with a deadline).
- **Pickup never happens** — a timer fires after, say, 30 minutes and the order is abandoned.
- **The barista is too slow** — a brew-SLA timer (e.g. 10 minutes) escalates the order (comp / refund / notify manager).

---

## 3. Failure Scenarios You Must Handle

Each of these must be handled by the workflow and shown in the graded demo.

| # | Scenario | Expected behaviour |
|----|----------|--------------------|
| **F1** | Payment activity times out repeatedly, then succeeds | Retried with backoff; exactly one charge (idempotency); order proceeds |
| **F2** | Payment is permanently declined | Inventory released, customer notified, order → `FAILED`, no loyalty accrued |
| **F3** | Item out of stock | Customer signalled a substitution offer; if no response within the deadline → cancel + refund |
| **F4** | Customer cancels before drinks are made | Refund + release inventory; order → `CANCELLED` |
| **F5** | Customer never collects | Pickup-expiry timer fires; order → `ABANDONED`; apply waste / refund policy |
| **F6** | Worker process crashes mid-order | On restart the workflow resumes; no double charge, no lost state |

---

## 4. Deliverables

- The workflow + activity code or the Step Functions ASL + worker stubs.
- The **state diagram**.
- A short **report**: engine choice & justification, the determinism explanation, your compensation / cancellation policy.
- A **README** with exact commands to start the engine, start the worker, and trigger each demo scenario.

---

## 5. Engine & Concept Landscape

Review these before designing, so you can place the engines relative to each other:

- **Cadence** (Uber, open source) — fault-oblivious stateful code in Go/Java; durable virtual memory that preserves stacks and locals across failures; history in Cassandra/MySQL/Postgres; Web UI on `:8088`. Read Workflows, Activities, Task Lists, plus signals, queries, timers and child workflows. Start at the get-started guide and the Java hello world.
- **Temporal** — the actively-developed successor to Cadence (same model, multi-language); see *Cadence vs Temporal*. Allowed as an alternative to Cadence if you prefer.
- **AWS Step Functions** — a managed state machine defined in Amazon States Language (JSON), not code; great AWS/Lambda integration, Choice / Retry / Catch states, `.waitForTaskToken` for human steps. Run locally with Step Functions Local.
- **Azure Durable Functions** — durable execution in code on Azure serverless (orchestrator + activity functions, event-sourced replay). A useful comparison point even if you don't build on it.

> **The trade-off to internalize:** durable-code engines (Cadence / Temporal / Durable Functions) let you express complex, long-running logic in a normal programming language at the cost of the **determinism constraint**, while Step Functions keeps the workflow as declarative config that is easy to visualize but pushes real logic into Lambdas. Either way, **idempotency and compensation are still your job** — the engine gives you durability, not business rollback.
