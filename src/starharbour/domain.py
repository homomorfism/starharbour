"""Domain model for the StarHarbour order-ahead workflow.

This module holds the *pure* data: the order's line items, the lifecycle
status enum, and the mutable workflow state that is rebuilt by replaying the
event log. None of this code performs IO — that is what makes replay
deterministic (see ``engine.py`` for the durability story).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace

# Catalogue of drinks the London store knows how to make. Anything outside this
# set fails validation (requirement step 1: "known coffee types").
KNOWN_DRINKS = {"latte", "cold_brew", "espresso", "cappuccino", "flat_white"}

# A small per-item substitution table used when an item is out of stock (F3).
SUBSTITUTIONS = {
    "cold_brew": "iced_americano",
    "latte": "flat_white",
}

# Unit prices in minor units (pence) to avoid floating point money bugs.
PRICES_PENCE = {
    "latte": 350,
    "cold_brew": 400,
    "espresso": 250,
    "cappuccino": 360,
    "flat_white": 360,
    "iced_americano": 380,
}


class OrderStatus(enum.StrEnum):
    """Where the order is in its lifecycle.

    The non-terminal statuses double as the workflow's *program counter*: the
    decider (``workflow.OrderWorkflow.decide``) looks at the status to decide
    which activity should run next. Terminal statuses stop the worker loop.
    """

    # --- in-flight ---
    PLACED = "PLACED"
    VALIDATING = "VALIDATING"
    RESERVING_INVENTORY = "RESERVING_INVENTORY"
    AWAITING_SUBSTITUTION = "AWAITING_SUBSTITUTION"
    TAKING_PAYMENT = "TAKING_PAYMENT"
    BREWING = "BREWING"
    READY = "READY"
    ACCRUING_LOYALTY = "ACCRUING_LOYALTY"
    COMPENSATING = "COMPENSATING"

    # --- terminal ---
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = {
    OrderStatus.COMPLETED,
    OrderStatus.FAILED,
    OrderStatus.CANCELLED,
    OrderStatus.ABANDONED,
}


@dataclass(frozen=True)
class LineItem:
    drink: str
    qty: int = 1


@dataclass(frozen=True)
class OrderState:
    """Immutable snapshot of an order, rebuilt by folding events.

    Treated as immutable: ``apply`` returns a new instance via
    :func:`dataclasses.replace`. Immutability keeps replay free of accidental
    in-place mutation bugs.
    """

    order_id: str
    store_id: str
    items: tuple[LineItem, ...]
    payment_method: str
    loyalty_id: str | None
    idempotency_key: str

    status: OrderStatus = OrderStatus.PLACED

    # filled in as the workflow progresses
    price_pence: int = 0
    reservation_id: str | None = None
    charge_id: str | None = None
    refund_id: str | None = None
    loyalty_points: int = 0

    # compensation bookkeeping — what must be undone if we fail/cancel
    inventory_reserved: bool = False
    inventory_released: bool = False
    payment_charged: bool = False
    payment_refunded: bool = False

    # substitution offer (F3)
    sub_item: str | None = None
    sub_offer: str | None = None
    sub_offered: bool = False

    # brewing bookkeeping
    ticket_queued: bool = False
    brew_escalated: bool = False

    # where COMPENSATING should land once undo is done, and whether to refund
    terminal_target: OrderStatus | None = None
    do_refund: bool = False
    outcome_reason: str | None = None

    # has the customer already been told the drinks are ready? (idempotent notify)
    notified_ready: bool = False

    def with_(self, **changes) -> OrderState:
        return replace(self, **changes)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def needs_release(self) -> bool:
        return self.inventory_reserved and not self.inventory_released

    def needs_refund(self) -> bool:
        return self.payment_charged and not self.payment_refunded


def price_order(items: tuple[LineItem, ...]) -> int:
    """Pure pricing function used by the validate-and-price activity."""
    total = 0
    for item in items:
        total += PRICES_PENCE[item.drink] * item.qty
    return total
