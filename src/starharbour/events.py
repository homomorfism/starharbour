"""Events — the append-only facts that make the workflow durable.

Everything that *happens* to an order is recorded as one of these events. The
current :class:`~starharbour.domain.OrderState` is never stored directly; it is
recomputed by folding the event log with ``engine.apply``. This is the same
event-sourcing model Temporal/Cadence use: the history is the source of truth,
and the worker can crash and rebuild state by replaying it (requirement F6).

Events fall into three buckets:
  * lifecycle        — OrderPlaced, OrderValidated, ...
  * activity results — PaymentTaken, InventoryReserved, ... (produced by the
                       runtime after it runs an activity)
  * external signals — BaristaMarkedReady, CustomerCollected, CustomerCancelled,
                       SubstitutionAnswered (sent from outside the workflow)
  * timer firings    — BrewSlaElapsed, PickupExpired, SubstitutionDeadline
"""

from __future__ import annotations

from dataclasses import dataclass, field

from starharbour.domain import LineItem


@dataclass(frozen=True)
class Event:
    """Base class. ``seq`` is assigned by the event store on append."""

    seq: int = field(default=-1, kw_only=True)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderPlaced(Event):
    order_id: str = ""
    store_id: str = ""
    items: tuple[LineItem, ...] = ()
    payment_method: str = ""
    loyalty_id: str | None = None
    idempotency_key: str = ""


@dataclass(frozen=True)
class OrderValidated(Event):
    price_pence: int = 0


@dataclass(frozen=True)
class ValidationFailed(Event):
    reason: str = ""


# ---------------------------------------------------------------------------
# Activity results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InventoryReserved(Event):
    reservation_id: str = ""


@dataclass(frozen=True)
class InventoryOutOfStock(Event):
    item: str = ""


@dataclass(frozen=True)
class InventoryReleased(Event):
    pass


@dataclass(frozen=True)
class PaymentTaken(Event):
    charge_id: str = ""
    amount_pence: int = 0


@dataclass(frozen=True)
class PaymentDeclined(Event):
    reason: str = ""


@dataclass(frozen=True)
class PaymentRefunded(Event):
    refund_id: str = ""


@dataclass(frozen=True)
class TicketQueued(Event):
    pass


@dataclass(frozen=True)
class CustomerNotified(Event):
    kind: str = ""  # "ready" | "out_of_stock" | "payment_failed" | "abandoned"


@dataclass(frozen=True)
class LoyaltyAccrued(Event):
    points: int = 0


@dataclass(frozen=True)
class SubstitutionOffered(Event):
    item: str = ""
    substitute: str = ""


# ---------------------------------------------------------------------------
# External signals
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BaristaMarkedReady(Event):
    """Barista has finished all drinks (requirement: external signal)."""


@dataclass(frozen=True)
class CustomerCollected(Event):
    """Customer physically picked up the order (external signal)."""


@dataclass(frozen=True)
class CustomerCancelled(Event):
    """Customer asked to cancel; may arrive at any point."""

    reason: str = "customer_request"


@dataclass(frozen=True)
class SubstitutionAnswered(Event):
    accepted: bool = False


# ---------------------------------------------------------------------------
# Timer firings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BrewSlaElapsed(Event):
    """Brew SLA timer fired — barista too slow, escalate (comp/notify)."""


@dataclass(frozen=True)
class PickupExpired(Event):
    """Pickup timer fired — customer never collected, abandon the order."""


@dataclass(frozen=True)
class SubstitutionDeadline(Event):
    """Customer didn't answer the substitution offer in time."""


# ---------------------------------------------------------------------------
# Terminal markers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderCompleted(Event):
    pass


@dataclass(frozen=True)
class OrderFailed(Event):
    reason: str = ""


@dataclass(frozen=True)
class OrderCancelled(Event):
    reason: str = ""


@dataclass(frozen=True)
class OrderAbandoned(Event):
    reason: str = ""


@dataclass(frozen=True)
class BrewEscalated(Event):
    """SLA breach handled (manager notified / comp applied) — non-terminal."""
