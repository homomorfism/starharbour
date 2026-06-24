"""Runnable demo of every graded scenario.

Usage:
    uv run starharbour-demo <scenario>

where <scenario> is one of:
    happy   F1   F1-fail   F2   F3-accept   F3-decline   F3-timeout
    F4   F5   F6   sla   all

Each scenario builds a fresh runtime with a virtual clock, drives the order
through the workflow, and prints the event log plus the final state so you can
see the state machine move. F6 uses a real on-disk event log and a simulated
worker crash.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from starharbour import (
    BaristaMarkedReady,
    Clients,
    CustomerCancelled,
    CustomerCollected,
    FileEventStore,
    InventoryClient,
    LineItem,
    ManualClock,
    PaymentsClient,
    WorkflowRuntime,
)
from starharbour.events import SubstitutionAnswered
from starharbour.workflow import (
    BREW_SLA_SECONDS,
    PICKUP_EXPIRY_SECONDS,
    SUBSTITUTION_DEADLINE_SECONDS,
)

ITEMS = [LineItem("latte", 2), LineItem("cold_brew", 1)]


def _runtime(clients=None, store=None):
    return WorkflowRuntime(
        store=store,
        clients=clients if clients is not None else Clients(),
        clock=ManualClock(),
    )


def _start(rt, order_id="order-1"):
    return rt.start(
        order_id=order_id,
        store_id="store-london-01",
        items=list(ITEMS),
        payment_method="card_saved",
        loyalty_id="loyalty-1",
        idempotency_key=f"idem-{order_id}",
    )


def _dump(title: str, rt: WorkflowRuntime) -> None:
    print(f"\n=== {title} ===")
    for event in rt.store.load():
        print(f"  [{event.seq:>2}] {type(event).__name__}")
    s = rt.state
    assert s is not None
    print(
        f"  --> status={s.status.value}  charge={s.charge_id}  "
        f"refund={s.refund_id}  released={s.inventory_released}  "
        f"points={s.loyalty_points}  reason={s.outcome_reason}"
    )
    print(f"  notifications: {rt.clients.notifier.sent}")
    print(f"  payment attempts: {rt.clients.payments.attempts}")


def happy():
    rt = _runtime()
    _start(rt)
    rt.signal(BaristaMarkedReady())
    rt.signal(CustomerCollected())
    _dump("HAPPY PATH", rt)


def f1():
    rt = _runtime(Clients(payments=PaymentsClient(timeouts_before_success=3)))
    _start(rt)
    rt.signal(BaristaMarkedReady())
    rt.signal(CustomerCollected())
    _dump("F1 — payment times out 3x then succeeds (one charge)", rt)


def f1_fail():
    rt = _runtime(Clients(payments=PaymentsClient(timeouts_before_success=99)))
    _start(rt)
    _dump("F1b — payment never recovers, retries exhausted → FAILED", rt)


def f2():
    rt = _runtime(Clients(payments=PaymentsClient(decline=True)))
    _start(rt)
    _dump("F2 — payment permanently declined → inventory released, FAILED", rt)


def f3_accept():
    rt = _runtime(Clients(inventory=InventoryClient(out_of_stock={"cold_brew"})))
    _start(rt)
    rt.signal(SubstitutionAnswered(accepted=True))
    rt.signal(BaristaMarkedReady())
    rt.signal(CustomerCollected())
    _dump("F3 — out of stock, substitution accepted → COMPLETED", rt)


def f3_decline():
    rt = _runtime(Clients(inventory=InventoryClient(out_of_stock={"cold_brew"})))
    _start(rt)
    rt.signal(SubstitutionAnswered(accepted=False))
    _dump("F3 — out of stock, substitution declined → CANCELLED", rt)


def f3_timeout():
    rt = _runtime(Clients(inventory=InventoryClient(out_of_stock={"cold_brew"})))
    _start(rt)
    rt.advance_time(SUBSTITUTION_DEADLINE_SECONDS + 1)
    _dump("F3 — out of stock, no answer before deadline → CANCELLED", rt)


def f4():
    rt = _runtime()
    _start(rt)
    rt.signal(CustomerCancelled())
    _dump("F4 — cancel before drinks made → refund + release → CANCELLED", rt)


def f5():
    rt = _runtime()
    _start(rt)
    rt.signal(BaristaMarkedReady())
    rt.advance_time(PICKUP_EXPIRY_SECONDS + 1)
    _dump("F5 — customer never collects → ABANDONED (waste policy, no refund)", rt)


def sla():
    rt = _runtime()
    _start(rt)
    rt.advance_time(BREW_SLA_SECONDS + 1)
    rt.signal(BaristaMarkedReady())
    rt.signal(CustomerCollected())
    _dump("SLA — barista too slow → escalate manager, order still COMPLETED", rt)


def f6():
    tmp = Path(tempfile.mkdtemp()) / "events.jsonl"
    print(f"\n=== F6 — worker crash mid-order (durable log: {tmp}) ===")

    # Process 1: drive to BREWING, then "crash".
    pay1 = PaymentsClient()
    rt1 = _runtime(Clients(payments=pay1), store=FileEventStore(str(tmp)))
    s1 = _start(rt1, "order-6")
    print(
        f"  process-1: status={s1.status.value} charge={s1.charge_id} "
        f"payment_attempts={pay1.attempts}"
    )
    print("  *** worker crashes — runtime object discarded ***")

    # Process 2: fresh runtime + fresh payment client recovers from the log.
    pay2 = PaymentsClient()
    rt2 = _runtime(Clients(payments=pay2), store=FileEventStore(str(tmp)))
    s2 = rt2.state
    assert s2 is not None
    print(
        f"  process-2 recovered: status={s2.status.value} "
        f"charge={s2.charge_id} new_payment_attempts={pay2.attempts} "
        f"(no re-charge!)"
    )
    rt2.signal(BaristaMarkedReady())
    s2 = rt2.signal(CustomerCollected())
    print(f"  process-2 finished: status={s2.status.value} points={s2.loyalty_points}")


SCENARIOS = {
    "happy": happy,
    "F1": f1,
    "F1-fail": f1_fail,
    "F2": f2,
    "F3-accept": f3_accept,
    "F3-decline": f3_decline,
    "F3-timeout": f3_timeout,
    "F4": f4,
    "F5": f5,
    "sla": sla,
    "F6": f6,
}


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    if arg == "all":
        for fn in SCENARIOS.values():
            fn()
        return 0
    fn = SCENARIOS.get(arg)
    if fn is None:
        print(f"unknown scenario: {arg}")
        print("choices:", ", ".join(["all", *SCENARIOS]))
        return 2
    fn()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
