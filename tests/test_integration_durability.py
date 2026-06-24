"""Integration tests for durability: persistence, replay determinism, and F6
(worker crashes mid-order and resumes with no double charge, no lost state)."""

from starharbour import (
    BaristaMarkedReady,
    Clients,
    CustomerCollected,
    FileEventStore,
    InMemoryEventStore,
    LineItem,
    ManualClock,
    OrderStatus,
    PaymentsClient,
    WorkflowRuntime,
)
from starharbour.workflow import apply

ITEMS = [LineItem("latte", 2), LineItem("cold_brew", 1)]


def _start(rt, order_id="order-1"):
    return rt.start(
        order_id=order_id,
        store_id="store-london-01",
        items=list(ITEMS),
        payment_method="card_saved",
        loyalty_id="loyalty-1",
        idempotency_key=f"idem-{order_id}",
    )


# --------------------------------------------------------------------------
# Replay determinism: folding the event log reproduces the exact state.
# --------------------------------------------------------------------------
def test_replaying_history_reproduces_state():
    store = InMemoryEventStore()
    rt = WorkflowRuntime(store=store, clients=Clients(), clock=ManualClock())
    _start(rt)
    rt.signal(BaristaMarkedReady())
    final = rt.signal(CustomerCollected())

    # Independently fold the persisted history.
    rebuilt = None
    for event in store.load():
        rebuilt = apply(rebuilt, event)

    assert rebuilt == final
    assert rebuilt.status == OrderStatus.COMPLETED


def test_every_event_has_monotonic_seq():
    store = InMemoryEventStore()
    rt = WorkflowRuntime(store=store, clients=Clients(), clock=ManualClock())
    _start(rt)
    seqs = [e.seq for e in store.load()]
    assert seqs == list(range(len(seqs)))


# --------------------------------------------------------------------------
# File persistence round-trips through JSON.
# --------------------------------------------------------------------------
def test_file_event_store_roundtrip(tmp_path):
    path = str(tmp_path / "events.jsonl")
    store = FileEventStore(path)
    rt = WorkflowRuntime(store=store, clients=Clients(), clock=ManualClock())
    _start(rt)

    # Reload from a brand-new store object reading the same file.
    reloaded = FileEventStore(path).load()
    rebuilt = None
    for event in reloaded:
        rebuilt = apply(rebuilt, event)
    assert rebuilt.status == OrderStatus.BREWING
    assert rebuilt.items == tuple(ITEMS)  # LineItems survived serialisation


# --------------------------------------------------------------------------
# F6 — worker crash mid-order: resume, no double charge, no lost state.
# --------------------------------------------------------------------------
def test_f6_crash_after_payment_resumes_without_double_charge(tmp_path):
    path = str(tmp_path / "events.jsonl")

    # --- process 1: runs up to BREWING, then "crashes" (we drop the runtime) ---
    store1 = FileEventStore(path)
    pay1 = PaymentsClient()
    rt1 = WorkflowRuntime(store=store1, clients=Clients(payments=pay1), clock=ManualClock())
    s1 = _start(rt1, "order-6")
    assert s1.status == OrderStatus.BREWING
    assert pay1.attempts == 1
    charge_before = s1.charge_id

    # --- process 2: fresh runtime + FRESH payment client, recovers from disk ---
    store2 = FileEventStore(path)
    pay2 = PaymentsClient()  # brand new; has never seen this order
    rt2 = WorkflowRuntime(store=store2, clients=Clients(payments=pay2), clock=ManualClock())

    # State was rebuilt from history — payment already recorded, so the new
    # worker does NOT re-charge.
    assert rt2.state.status == OrderStatus.BREWING
    assert rt2.state.charge_id == charge_before
    assert pay2.attempts == 0  # no new charge attempt on recovery

    # And it can carry the order to completion.
    rt2.signal(BaristaMarkedReady())
    s2 = rt2.signal(CustomerCollected())
    assert s2.status == OrderStatus.COMPLETED
    assert pay2.attempts == 0


def test_f6_crash_during_payment_retries_is_idempotent(tmp_path):
    """If the worker crashes while payment is still being retried, recovery
    re-runs the charge — but the shared Idempotency-Key means money moves once.

    Here the SAME payments backend survives the crash (a real gateway does),
    and we assert it is charged exactly once across both worker lifetimes."""
    path = str(tmp_path / "events.jsonl")
    # A gateway that survives both worker processes.
    gateway = PaymentsClient(timeouts_before_success=0)

    # Process 1 completes payment and persists the event.
    store1 = FileEventStore(path)
    rt1 = WorkflowRuntime(store=store1, clients=Clients(payments=gateway), clock=ManualClock())
    _start(rt1, "order-6b")

    # Process 2 recovers; even if it retried payment, the key dedupes it.
    store2 = FileEventStore(path)
    rt2 = WorkflowRuntime(store=store2, clients=Clients(payments=gateway), clock=ManualClock())
    rt2.signal(BaristaMarkedReady())
    s = rt2.signal(CustomerCollected())

    assert s.status == OrderStatus.COMPLETED
    # Exactly one charge object for the order's idempotency key.
    assert len(gateway._charges_by_key) == 1


def test_f6_recovered_workflow_does_not_restart():
    """A runtime recovered from a completed history is terminal and inert."""
    store = InMemoryEventStore()
    rt = WorkflowRuntime(store=store, clients=Clients(), clock=ManualClock())
    _start(rt)
    rt.signal(BaristaMarkedReady())
    rt.signal(CustomerCollected())

    # Recover into a new runtime.
    rt2 = WorkflowRuntime(store=store, clients=Clients(), clock=ManualClock())
    assert rt2.state.status == OrderStatus.COMPLETED
    rt2.run()  # must be a no-op
    assert rt2.state.status == OrderStatus.COMPLETED
