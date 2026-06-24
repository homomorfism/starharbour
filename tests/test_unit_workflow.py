"""Unit tests for the pure workflow core: ``apply`` (evolve) and ``decide``.

These tests touch no IO, no clock, no clients — just the two pure functions.
That is the point of the decide/evolve split: the business logic is testable in
isolation and is deterministic.
"""

from starharbour import events as ev
from starharbour.domain import LineItem, OrderState, OrderStatus, price_order
from starharbour.workflow import (
    TIMER_BREW_SLA,
    TIMER_PICKUP,
    TIMER_SUBSTITUTION,
    OrderWorkflow,
    apply,
)

ITEMS = (LineItem("latte", 2), LineItem("cold_brew", 1))


def placed_state() -> OrderState:
    return apply(
        None,
        ev.OrderPlaced(
            order_id="o1",
            store_id="store-london-01",
            items=ITEMS,
            payment_method="card",
            loyalty_id="loy",
            idempotency_key="idem-o1",
        ),
    )


# --------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------
def test_price_order_sums_line_items():
    # latte 350 * 2 + cold_brew 400 * 1
    assert price_order(ITEMS) == 1100


# --------------------------------------------------------------------------
# evolve: apply
# --------------------------------------------------------------------------
def test_order_placed_creates_validating_state():
    s = placed_state()
    assert s.status == OrderStatus.VALIDATING
    assert s.order_id == "o1"
    assert s.items == ITEMS
    assert not s.is_terminal


def test_happy_path_status_progression():
    s = placed_state()
    s = apply(s, ev.OrderValidated(price_pence=1100))
    assert s.status == OrderStatus.RESERVING_INVENTORY
    assert s.price_pence == 1100

    s = apply(s, ev.InventoryReserved(reservation_id="rsv_1"))
    assert s.status == OrderStatus.TAKING_PAYMENT
    assert s.inventory_reserved and s.reservation_id == "rsv_1"

    s = apply(s, ev.PaymentTaken(charge_id="ch_1", amount_pence=1100))
    assert s.status == OrderStatus.BREWING
    assert s.payment_charged and s.charge_id == "ch_1"

    s = apply(s, ev.TicketQueued())
    assert s.ticket_queued

    s = apply(s, ev.BaristaMarkedReady())
    assert s.status == OrderStatus.READY

    s = apply(s, ev.CustomerNotified(kind="ready"))
    assert s.notified_ready

    s = apply(s, ev.CustomerCollected())
    assert s.status == OrderStatus.ACCRUING_LOYALTY

    s = apply(s, ev.LoyaltyAccrued(points=11))
    assert s.status == OrderStatus.COMPLETED
    assert s.is_terminal
    assert s.loyalty_points == 11


def test_apply_is_immutable():
    s = placed_state()
    s2 = apply(s, ev.OrderValidated(price_pence=1100))
    assert s.status == OrderStatus.VALIDATING  # original unchanged
    assert s2.status == OrderStatus.RESERVING_INVENTORY
    assert s is not s2


def test_terminal_state_ignores_further_events():
    s = placed_state().with_(status=OrderStatus.COMPLETED)
    assert apply(s, ev.CustomerCancelled()) == s
    assert apply(s, ev.BaristaMarkedReady()) == s


def test_out_of_context_events_are_noops():
    s = placed_state()  # VALIDATING
    # a payment event makes no sense yet → ignored
    assert apply(s, ev.PaymentTaken(charge_id="x", amount_pence=1)) == s
    # a barista signal makes no sense yet → ignored
    assert apply(s, ev.BaristaMarkedReady()) == s


def test_validation_failure_routes_to_compensation_failed():
    s = placed_state()
    s = apply(s, ev.ValidationFailed(reason="store closed"))
    assert s.status == OrderStatus.COMPENSATING
    assert s.terminal_target == OrderStatus.FAILED
    assert s.do_refund is False


def test_out_of_stock_with_substitution_awaits_substitution():
    s = placed_state().with_(status=OrderStatus.RESERVING_INVENTORY)
    s = apply(s, ev.InventoryOutOfStock(item="cold_brew"))
    assert s.status == OrderStatus.AWAITING_SUBSTITUTION
    assert s.sub_item == "cold_brew"
    assert s.sub_offer == "iced_americano"


def test_out_of_stock_without_substitution_cancels():
    s = placed_state().with_(status=OrderStatus.RESERVING_INVENTORY)
    s = apply(s, ev.InventoryOutOfStock(item="espresso"))  # no sub mapping
    assert s.status == OrderStatus.COMPENSATING
    assert s.terminal_target == OrderStatus.CANCELLED


def test_substitution_accepted_swaps_item_and_reserves_again():
    s = placed_state().with_(
        status=OrderStatus.AWAITING_SUBSTITUTION,
        sub_item="cold_brew",
        sub_offer="iced_americano",
        sub_offered=True,
    )
    s = apply(s, ev.SubstitutionAnswered(accepted=True))
    assert s.status == OrderStatus.RESERVING_INVENTORY
    drinks = {i.drink for i in s.items}
    assert "iced_americano" in drinks and "cold_brew" not in drinks
    assert s.sub_item is None


def test_substitution_declined_cancels_with_refund_flag():
    s = placed_state().with_(
        status=OrderStatus.AWAITING_SUBSTITUTION, sub_item="cold_brew", sub_offer="x"
    )
    s = apply(s, ev.SubstitutionAnswered(accepted=False))
    assert s.status == OrderStatus.COMPENSATING
    assert s.terminal_target == OrderStatus.CANCELLED
    assert s.do_refund is True


def test_substitution_deadline_cancels():
    s = placed_state().with_(
        status=OrderStatus.AWAITING_SUBSTITUTION, sub_item="cold_brew", sub_offer="x"
    )
    s = apply(s, ev.SubstitutionDeadline())
    assert s.status == OrderStatus.COMPENSATING
    assert s.outcome_reason == "substitution_timeout"


def test_payment_declined_routes_to_failed():
    s = placed_state().with_(
        status=OrderStatus.TAKING_PAYMENT, inventory_reserved=True, reservation_id="r1"
    )
    s = apply(s, ev.PaymentDeclined(reason="card declined"))
    assert s.status == OrderStatus.COMPENSATING
    assert s.terminal_target == OrderStatus.FAILED
    assert s.needs_release() is True
    assert s.do_refund is False


def test_cancel_before_drinks_is_refundable():
    s = placed_state().with_(
        status=OrderStatus.BREWING,
        inventory_reserved=True,
        reservation_id="r1",
        payment_charged=True,
        charge_id="ch1",
    )
    s = apply(s, ev.CustomerCancelled())
    assert s.status == OrderStatus.COMPENSATING
    assert s.terminal_target == OrderStatus.CANCELLED
    assert s.do_refund is True
    assert s.needs_refund() and s.needs_release()


def test_cancel_after_drinks_made_is_abandoned_no_refund():
    s = placed_state().with_(
        status=OrderStatus.READY,
        inventory_reserved=True,
        payment_charged=True,
        charge_id="ch1",
    )
    s = apply(s, ev.CustomerCancelled())
    assert s.status == OrderStatus.COMPENSATING
    assert s.terminal_target == OrderStatus.ABANDONED
    assert s.do_refund is False


def test_pickup_expired_abandons_without_refund():
    s = placed_state().with_(status=OrderStatus.READY, payment_charged=True, charge_id="ch1")
    s = apply(s, ev.PickupExpired())
    assert s.status == OrderStatus.COMPENSATING
    assert s.terminal_target == OrderStatus.ABANDONED
    assert s.do_refund is False


# --------------------------------------------------------------------------
# decide
# --------------------------------------------------------------------------
def test_decide_validating_runs_validate():
    eff = OrderWorkflow.decide(placed_state())
    assert eff.activity.name == "validate"


def test_decide_reserving_runs_reserve():
    s = placed_state().with_(status=OrderStatus.RESERVING_INVENTORY)
    assert OrderWorkflow.decide(s).activity.name == "reserve_inventory"


def test_decide_taking_payment_uses_retry_policy():
    s = placed_state().with_(status=OrderStatus.TAKING_PAYMENT)
    eff = OrderWorkflow.decide(s)
    assert eff.activity.name == "take_payment"
    assert eff.activity.retry.max_attempts >= 2  # payment must retry


def test_decide_awaiting_substitution_first_offers_then_arms_timer():
    s = placed_state().with_(status=OrderStatus.AWAITING_SUBSTITUTION, sub_offered=False)
    assert OrderWorkflow.decide(s).activity.name == "offer_substitution"
    s2 = s.with_(sub_offered=True)
    eff = OrderWorkflow.decide(s2)
    assert eff.activity is None
    assert TIMER_SUBSTITUTION in eff.timers


def test_decide_brewing_queues_then_arms_sla_timer():
    s = placed_state().with_(status=OrderStatus.BREWING, ticket_queued=False)
    assert OrderWorkflow.decide(s).activity.name == "queue_ticket"
    s2 = s.with_(ticket_queued=True)
    eff = OrderWorkflow.decide(s2)
    assert eff.activity is None
    assert TIMER_BREW_SLA in eff.timers


def test_decide_brewing_after_escalation_disarms_timer():
    s = placed_state().with_(status=OrderStatus.BREWING, ticket_queued=True, brew_escalated=True)
    eff = OrderWorkflow.decide(s)
    assert eff.activity is None
    assert eff.timers == {}


def test_decide_ready_notifies_then_arms_pickup_timer():
    s = placed_state().with_(status=OrderStatus.READY, notified_ready=False)
    assert OrderWorkflow.decide(s).activity.name == "notify_ready"
    s2 = s.with_(notified_ready=True)
    eff = OrderWorkflow.decide(s2)
    assert eff.activity is None
    assert TIMER_PICKUP in eff.timers


def test_decide_compensation_order_refund_then_release_then_finalize():
    s = placed_state().with_(
        status=OrderStatus.COMPENSATING,
        terminal_target=OrderStatus.CANCELLED,
        do_refund=True,
        inventory_reserved=True,
        reservation_id="r1",
        payment_charged=True,
        charge_id="ch1",
    )
    assert OrderWorkflow.decide(s).activity.name == "refund_payment"
    s = s.with_(payment_refunded=True)
    assert OrderWorkflow.decide(s).activity.name == "release_inventory"
    s = s.with_(inventory_released=True)
    assert OrderWorkflow.decide(s).activity.name == "finalize"


def test_decide_compensation_skips_refund_when_not_flagged():
    s = placed_state().with_(
        status=OrderStatus.COMPENSATING,
        terminal_target=OrderStatus.FAILED,
        do_refund=False,
        inventory_reserved=True,
        reservation_id="r1",
    )
    # No refund even though nothing charged; go straight to release.
    assert OrderWorkflow.decide(s).activity.name == "release_inventory"


def test_decide_terminal_does_nothing():
    s = placed_state().with_(status=OrderStatus.COMPLETED)
    eff = OrderWorkflow.decide(s)
    assert eff.activity is None and eff.timers == {}
