"""Activities — the side-effecting steps.

An activity takes the current state plus the client bundle, performs exactly one
unit of IO, and returns the *success* event. If the downstream call fails it
raises an :class:`~starharbour.clients.ActivityError`; the runtime decides
whether to retry (transient) or give up. When the runtime gives up it calls the
activity's *failure mapper* to turn the exception into the appropriate failure
event (e.g. a permanent payment decline → ``PaymentDeclined``).

Activities must be safe to run more than once, because retries and crash
recovery can re-invoke them. Idempotency lives in the clients (e.g. the payment
Idempotency-Key) — that is what guarantees exactly-one-charge (F1, F6).
"""

from __future__ import annotations

from collections.abc import Callable

from starharbour import events as ev
from starharbour.clients import (
    ActivityError,
    Clients,
    OutOfStockError,
)
from starharbour.domain import KNOWN_DRINKS, OrderState, price_order

# An activity: (state, clients) -> success Event
Activity = Callable[[OrderState, Clients], ev.Event]
# A failure mapper: (state, exc) -> failure Event
FailureMapper = Callable[[OrderState, Exception], ev.Event]


# ---------------------------------------------------------------------------
# Activity implementations
# ---------------------------------------------------------------------------
def validate(state: OrderState, clients: Clients) -> ev.Event:
    # Known coffee types only; store assumed open for store-london-01.
    for item in state.items:
        if item.drink not in KNOWN_DRINKS:
            raise _NonRetryable(f"unknown drink: {item.drink}")
        if item.qty <= 0:
            raise _NonRetryable(f"bad quantity for {item.drink}")
    return ev.OrderValidated(price_pence=price_order(state.items))


def reserve_inventory(state: OrderState, clients: Clients) -> ev.Event:
    reservation_id = clients.inventory.reserve(state.store_id, state.items)
    return ev.InventoryReserved(reservation_id=reservation_id)


def offer_substitution(state: OrderState, clients: Clients) -> ev.Event:
    clients.notifier.notify(state.order_id, "out_of_stock")
    return ev.SubstitutionOffered(item=state.sub_item or "", substitute=state.sub_offer or "")


def take_payment(state: OrderState, clients: Clients) -> ev.Event:
    # The Idempotency-Key is fixed for the life of the order, so any number of
    # retries / crash-replays collapse to a single charge.
    charge = clients.payments.charge(state.idempotency_key, state.price_pence)
    return ev.PaymentTaken(charge_id=charge.charge_id, amount_pence=charge.amount_pence)


def queue_ticket(state: OrderState, clients: Clients) -> ev.Event:
    clients.barista.enqueue(state.order_id)
    return ev.TicketQueued()


def notify_ready(state: OrderState, clients: Clients) -> ev.Event:
    clients.notifier.notify(state.order_id, "ready")
    return ev.CustomerNotified(kind="ready")


def escalate_brew(state: OrderState, clients: Clients) -> ev.Event:
    # SLA breach: comp / notify manager. Order keeps going (barista still owes
    # us the drinks) but the breach is recorded.
    clients.notifier.notify(state.order_id, "brew_sla_breach_manager")
    return ev.BrewEscalated()


def accrue_loyalty(state: OrderState, clients: Clients) -> ev.Event:
    if state.loyalty_id:
        points = clients.loyalty.accrue(state.loyalty_id, state.price_pence)
    else:
        points = 0
    return ev.LoyaltyAccrued(points=points)


def refund_payment(state: OrderState, clients: Clients) -> ev.Event:
    refund_id = clients.payments.refund(state.charge_id or "")
    return ev.PaymentRefunded(refund_id=refund_id)


def release_inventory(state: OrderState, clients: Clients) -> ev.Event:
    clients.inventory.release(state.reservation_id or "")
    return ev.InventoryReleased()


def finalize(state: OrderState, clients: Clients) -> ev.Event:
    """Notify the customer of the final outcome and emit the terminal event."""
    from starharbour.domain import OrderStatus

    target = state.terminal_target
    reason = state.outcome_reason or ""
    if target == OrderStatus.FAILED:
        clients.notifier.notify(state.order_id, "payment_failed")
        return ev.OrderFailed(reason=reason)
    if target == OrderStatus.ABANDONED:
        clients.notifier.notify(state.order_id, "abandoned")
        return ev.OrderAbandoned(reason=reason)
    # default: cancelled
    clients.notifier.notify(state.order_id, "cancelled")
    return ev.OrderCancelled(reason=reason)


# ---------------------------------------------------------------------------
# Failure mappers — turn an exhausted/permanent failure into the right event
# ---------------------------------------------------------------------------
def _fail_validation(state: OrderState, exc: Exception) -> ev.Event:
    return ev.ValidationFailed(reason=str(exc))


def _fail_reserve(state: OrderState, exc: Exception) -> ev.Event:
    if isinstance(exc, OutOfStockError):
        return ev.InventoryOutOfStock(item=exc.item)
    # Generic reservation failure with no substitution path → treat as out of
    # stock of the first item so the cancel path engages.
    item = state.items[0].drink if state.items else ""
    return ev.InventoryOutOfStock(item=item)


def _fail_payment(state: OrderState, exc: Exception) -> ev.Event:
    return ev.PaymentDeclined(reason=str(exc))


class _NonRetryable(ActivityError):
    retryable = False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def default_registry() -> dict[str, tuple[Activity, FailureMapper | None]]:
    """Map activity name → (implementation, failure-mapper).

    The failure mapper is used when retries are exhausted or the error is
    non-retryable. ``None`` means the activity is not expected to fail
    terminally (a bug if it does — it will surface as an exception).
    """
    return {
        "validate": (validate, _fail_validation),
        "reserve_inventory": (reserve_inventory, _fail_reserve),
        "offer_substitution": (offer_substitution, None),
        "take_payment": (take_payment, _fail_payment),
        "queue_ticket": (queue_ticket, None),
        "notify_ready": (notify_ready, None),
        "escalate_brew": (escalate_brew, None),
        "accrue_loyalty": (accrue_loyalty, None),
        "refund_payment": (refund_payment, None),
        "release_inventory": (release_inventory, None),
        "finalize": (finalize, None),
    }
