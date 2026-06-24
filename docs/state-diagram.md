# StarHarbour Order Workflow — State Diagram

The order is a single long-running state machine. **Non-terminal statuses double
as the program counter**: the decider looks at the status to choose the next
activity. Transitions are driven by three kinds of inputs:

- **activity results** (validate, reserve, payment, loyalty …)
- **external signals** (barista ready, customer collected, cancel, substitution answer)
- **timers** (substitution deadline, brew SLA, pickup expiry)

## Lifecycle

```mermaid
stateDiagram-v2
    [*] --> VALIDATING : OrderPlaced

    VALIDATING --> RESERVING_INVENTORY : OrderValidated
    VALIDATING --> COMPENSATING : ValidationFailed

    RESERVING_INVENTORY --> TAKING_PAYMENT : InventoryReserved
    RESERVING_INVENTORY --> AWAITING_SUBSTITUTION : InventoryOutOfStock (sub available)
    RESERVING_INVENTORY --> COMPENSATING : InventoryOutOfStock (no sub)

    AWAITING_SUBSTITUTION --> RESERVING_INVENTORY : SubstitutionAccepted
    AWAITING_SUBSTITUTION --> COMPENSATING : SubstitutionDeclined / Deadline(timer)

    TAKING_PAYMENT --> BREWING : PaymentTaken
    TAKING_PAYMENT --> COMPENSATING : PaymentDeclined (after retries)

    BREWING --> BREWING : BrewSlaElapsed(timer) → escalate (once)
    BREWING --> READY : BaristaMarkedReady (signal)

    READY --> ACCRUING_LOYALTY : CustomerCollected (signal)
    READY --> COMPENSATING : PickupExpired(timer) → ABANDONED

    ACCRUING_LOYALTY --> COMPLETED : LoyaltyAccrued

    state "any pre-READY state" as ANY
    ANY --> COMPENSATING : CustomerCancelled (signal)

    COMPENSATING --> FAILED : finalize (terminal_target=FAILED)
    COMPENSATING --> CANCELLED : finalize (terminal_target=CANCELLED)
    COMPENSATING --> ABANDONED : finalize (terminal_target=ABANDONED)

    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
    ABANDONED --> [*]
```

## The compensation sub-machine

`COMPENSATING` is entered from many places; it runs the *pending* undo actions
in a fixed order, then finalizes to the chosen terminal status. Each step is a
durable event, so compensation itself is crash-safe and idempotent.

```mermaid
stateDiagram-v2
    [*] --> COMPENSATING
    COMPENSATING --> refund : do_refund and payment_charged
    refund --> COMPENSATING : PaymentRefunded
    COMPENSATING --> release : inventory_reserved and not released
    release --> COMPENSATING : InventoryReleased
    COMPENSATING --> finalize : nothing left to undo
    finalize --> [*] : OrderFailed / OrderCancelled / OrderAbandoned
```

`do_refund` is decided at the moment we enter compensation:

| Trigger | terminal_target | do_refund | Why |
|---------|-----------------|-----------|-----|
| Validation failed | FAILED | no | nothing charged |
| Payment declined (F2) | FAILED | no | nothing charged; release inventory |
| Out of stock, no substitution / declined / timeout (F3) | CANCELLED | yes* | refund if anything was charged (nothing is, pre-payment) |
| Cancel **before** drinks made (F4) | CANCELLED | yes | money back, release inventory |
| Cancel **after** drinks made | ABANDONED | no | drinks wasted, charge stands |
| Pickup expired (F5) | ABANDONED | no | drinks made & paid → waste policy |

\* `do_refund=True` is harmless when there is no charge — the decider's
`needs_refund()` guard skips the refund step.

## Failure-scenario → path

| # | Path |
|---|------|
| **F1** | `TAKING_PAYMENT` retries `take_payment` w/ backoff (same Idempotency-Key) → `PaymentTaken` → `BREWING` |
| **F2** | `TAKING_PAYMENT` → `PaymentDeclined` → `COMPENSATING` → release → finalize → `FAILED` |
| **F3** | `RESERVING_INVENTORY` → `InventoryOutOfStock` → `AWAITING_SUBSTITUTION` → (accept→`RESERVING_INVENTORY`) or (decline/`SubstitutionDeadline`→`COMPENSATING`→`CANCELLED`) |
| **F4** | `…/BREWING` → `CustomerCancelled` → `COMPENSATING` → refund → release → `CANCELLED` |
| **F5** | `READY` → `PickupExpired` → `COMPENSATING` → release → `ABANDONED` |
| **F6** | crash at any point → reload event log → `apply`-fold to last state → continue; pending activities re-run idempotently |
