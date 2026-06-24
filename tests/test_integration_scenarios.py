"""Integration tests: the full runtime driving each required scenario.

These exercise the real ``WorkflowRuntime`` — activities, retries, timers,
persistence, signals — against the fake clients, and assert the end-to-end
behaviour the requirements demand (F1–F6 plus the happy path and brew-SLA
escalation).
"""

from conftest import make_runtime, place_standard_order
from starharbour import (
    BaristaMarkedReady,
    Clients,
    CustomerCancelled,
    CustomerCollected,
    InventoryClient,
    LineItem,
    OrderStatus,
    PaymentsClient,
)
from starharbour.events import SubstitutionAnswered
from starharbour.workflow import (
    BREW_SLA_SECONDS,
    PICKUP_EXPIRY_SECONDS,
    SUBSTITUTION_DEADLINE_SECONDS,
)


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
def test_happy_path_end_to_end():
    clients = Clients()
    rt = make_runtime(clients)
    s = place_standard_order(rt)
    # placed → validated → reserved → paid → brewing (waiting for barista)
    assert s.status == OrderStatus.BREWING
    assert s.price_pence == 1100
    assert s.charge_id is not None
    assert clients.barista.tickets == ["order-1"]

    s = rt.signal(BaristaMarkedReady())
    assert s.status == OrderStatus.READY
    assert ("order-1", "ready") in clients.notifier.sent

    s = rt.signal(CustomerCollected())
    assert s.status == OrderStatus.COMPLETED
    assert s.loyalty_points == 11  # 1100 pence → 11 points
    assert clients.loyalty.accruals == [("loyalty-1", 11)]
    assert clients.payments.attempts == 1  # exactly one charge


# --------------------------------------------------------------------------
# F1 — payment times out repeatedly, then succeeds
# --------------------------------------------------------------------------
def test_f1_payment_retries_then_succeeds_exactly_once():
    clients = Clients(payments=PaymentsClient(timeouts_before_success=3))
    rt = make_runtime(clients)
    s = place_standard_order(rt)

    assert s.status == OrderStatus.BREWING  # got past payment
    assert s.charge_id is not None
    # 3 timeouts + 1 success = 4 attempts, but only ONE charge recorded.
    assert clients.payments.attempts == 4
    assert len(clients.payments._charges_by_key) == 1


def test_f1_payment_exhausts_retries_then_fails():
    # More timeouts than the retry budget (5 attempts) → permanent failure path.
    clients = Clients(payments=PaymentsClient(timeouts_before_success=99))
    rt = make_runtime(clients)
    s = place_standard_order(rt)
    assert s.status == OrderStatus.FAILED
    assert s.inventory_released is True


# --------------------------------------------------------------------------
# F2 — payment permanently declined
# --------------------------------------------------------------------------
def test_f2_payment_declined_releases_inventory_notifies_fails():
    clients = Clients(payments=PaymentsClient(decline=True))
    rt = make_runtime(clients)
    s = place_standard_order(rt)

    assert s.status == OrderStatus.FAILED
    assert s.inventory_released is True
    assert clients.inventory.active_reservations == set()  # released
    assert ("order-1", "payment_failed") in clients.notifier.sent
    assert clients.loyalty.accruals == []  # no loyalty accrued
    assert s.refund_id is None  # nothing to refund
    assert clients.payments.attempts == 1  # declined on first try


# --------------------------------------------------------------------------
# F3 — item out of stock → substitution
# --------------------------------------------------------------------------
def test_f3_substitution_accepted_continues_order():
    clients = Clients(inventory=InventoryClient(out_of_stock={"cold_brew"}))
    rt = make_runtime(clients)
    s = place_standard_order(rt)
    assert s.status == OrderStatus.AWAITING_SUBSTITUTION
    assert s.sub_offer == "iced_americano"
    assert ("order-1", "out_of_stock") in clients.notifier.sent

    s = rt.signal(SubstitutionAnswered(accepted=True))
    assert s.status == OrderStatus.BREWING
    drinks = {i.drink for i in s.items}
    assert "iced_americano" in drinks and "cold_brew" not in drinks
    assert s.charge_id is not None  # paid for the substituted order


def test_f3_substitution_declined_cancels_and_refunds():
    clients = Clients(inventory=InventoryClient(out_of_stock={"cold_brew"}))
    rt = make_runtime(clients)
    s = place_standard_order(rt)
    s = rt.signal(SubstitutionAnswered(accepted=False))

    assert s.status == OrderStatus.CANCELLED
    assert s.outcome_reason == "substitution_declined"
    # Out of stock happens before payment, so there is nothing to refund, but
    # the order is cleanly cancelled and the customer notified.
    assert ("order-1", "cancelled") in clients.notifier.sent


def test_f3_no_substitution_answer_times_out_and_cancels():
    clients = Clients(inventory=InventoryClient(out_of_stock={"cold_brew"}))
    rt = make_runtime(clients)
    s = place_standard_order(rt)
    assert s.status == OrderStatus.AWAITING_SUBSTITUTION

    # Customer never answers; deadline fires.
    s = rt.advance_time(SUBSTITUTION_DEADLINE_SECONDS + 1)
    assert s.status == OrderStatus.CANCELLED
    assert s.outcome_reason == "substitution_timeout"


# --------------------------------------------------------------------------
# F4 — customer cancels before drinks are made
# --------------------------------------------------------------------------
def test_f4_cancel_before_drinks_refunds_and_releases():
    clients = Clients()
    rt = make_runtime(clients)
    s = place_standard_order(rt)
    assert s.status == OrderStatus.BREWING  # paid, not yet ready

    s = rt.signal(CustomerCancelled())
    assert s.status == OrderStatus.CANCELLED
    assert s.refund_id is not None  # refunded
    assert s.inventory_released is True  # inventory released
    assert clients.inventory.active_reservations == set()
    assert ("order-1", "cancelled") in clients.notifier.sent


# --------------------------------------------------------------------------
# F5 — customer never collects
# --------------------------------------------------------------------------
def test_f5_pickup_never_happens_abandons():
    clients = Clients()
    rt = make_runtime(clients)
    s = place_standard_order(rt)
    s = rt.signal(BaristaMarkedReady())
    assert s.status == OrderStatus.READY

    s = rt.advance_time(PICKUP_EXPIRY_SECONDS + 1)
    assert s.status == OrderStatus.ABANDONED
    assert s.outcome_reason == "pickup_expired"
    # Waste policy: drinks were made & paid for → no refund.
    assert s.refund_id is None
    assert ("order-1", "abandoned") in clients.notifier.sent


# --------------------------------------------------------------------------
# Brew SLA escalation (barista too slow)
# --------------------------------------------------------------------------
def test_brew_sla_escalates_then_order_still_completes():
    clients = Clients()
    rt = make_runtime(clients)
    s = place_standard_order(rt)

    s = rt.advance_time(BREW_SLA_SECONDS + 1)  # barista is slow
    assert s.brew_escalated is True
    assert ("order-1", "brew_sla_breach_manager") in clients.notifier.sent
    assert s.status == OrderStatus.BREWING  # still waiting, not terminal

    # Barista eventually delivers; order finishes normally.
    s = rt.signal(BaristaMarkedReady())
    s = rt.signal(CustomerCollected())
    assert s.status == OrderStatus.COMPLETED


def test_brew_sla_escalates_only_once():
    clients = Clients()
    rt = make_runtime(clients)
    place_standard_order(rt)
    rt.advance_time(BREW_SLA_SECONDS + 1)
    rt.advance_time(BREW_SLA_SECONDS + 1)  # advancing again must not re-escalate
    breaches = [n for n in clients.notifier.sent if n[1] == "brew_sla_breach_manager"]
    assert len(breaches) == 1


# --------------------------------------------------------------------------
# Late / out-of-context signals are ignored
# --------------------------------------------------------------------------
def test_collect_signal_before_ready_is_ignored():
    rt = make_runtime()
    s = place_standard_order(rt)  # BREWING
    s = rt.signal(CustomerCollected())  # too early
    assert s.status == OrderStatus.BREWING


def test_cancel_after_completion_is_ignored():
    rt = make_runtime()
    place_standard_order(rt)
    rt.signal(BaristaMarkedReady())
    s = rt.signal(CustomerCollected())
    assert s.status == OrderStatus.COMPLETED
    s = rt.signal(CustomerCancelled())  # too late
    assert s.status == OrderStatus.COMPLETED


# --------------------------------------------------------------------------
# Validation failure (unknown drink)
# --------------------------------------------------------------------------
def test_unknown_drink_fails_validation():
    rt = make_runtime()
    s = rt.start(
        order_id="bad",
        store_id="store-london-01",
        items=[LineItem("unicorn_frappe", 1)],
        payment_method="card",
        loyalty_id="loy",
    )
    assert s.status == OrderStatus.FAILED
    # Never reserved or charged.
    assert s.charge_id is None
    assert s.inventory_reserved is False
