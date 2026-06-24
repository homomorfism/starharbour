"""External service clients.

These stand in for the real downstream systems an order touches: the hw1
Payments API, the hw1 Loyalty API, store inventory, the barista queue, and the
push-notification service. They are deliberately *fakes* so the whole workflow
runs in-process and is fully testable, but each one models the failure mode the
requirements care about:

  * PaymentsClient — idempotent charge keyed by Idempotency-Key, plus
    configurable transient timeouts (F1) and permanent declines (F2).
  * InventoryClient — configurable out-of-stock per item (F3).
  * LoyaltyClient / Notifier — record calls so tests can assert on them.

Activity errors carry a ``retryable`` flag so the runtime knows whether to back
off and retry (transient) or give up and compensate (permanent).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field


class ActivityError(Exception):
    """Base for failures raised by clients. ``retryable`` drives retry policy."""

    retryable: bool = True


class PaymentTimeout(ActivityError):
    retryable = True


class PaymentDeclinedError(ActivityError):
    retryable = False


class OutOfStockError(ActivityError):
    retryable = False

    def __init__(self, item: str):
        super().__init__(f"out of stock: {item}")
        self.item = item


# ---------------------------------------------------------------------------
# Payments  (hw1 Payments API:  POST /api/v1/payments  with Idempotency-Key)
# ---------------------------------------------------------------------------
@dataclass
class _Charge:
    charge_id: str
    amount_pence: int
    refunded: bool = False
    refund_id: str | None = None


class PaymentsClient:
    """Idempotent fake of the Payments API.

    ``timeouts_before_success`` makes the first N attempts raise
    :class:`PaymentTimeout` then succeed — that is failure scenario F1. Set
    ``decline=True`` for a permanent decline — that is F2.

    Idempotency: charging the same ``idempotency_key`` twice returns the *same*
    charge and never moves money twice. This is what guarantees "exactly one
    charge" across retries (F1) and worker crashes (F6).
    """

    def __init__(self, timeouts_before_success: int = 0, decline: bool = False):
        self.timeouts_before_success = timeouts_before_success
        self.decline = decline
        self._charges_by_key: dict[str, _Charge] = {}
        self._ids = itertools.count(1)
        self._refund_ids = itertools.count(1)
        self.attempts = 0  # total charge attempts, for test assertions

    def charge(self, idempotency_key: str, amount_pence: int) -> _Charge:
        self.attempts += 1
        # Idempotent replay: a completed charge for this key is returned as-is,
        # regardless of how the workflow got here (retry or crash-recovery).
        existing = self._charges_by_key.get(idempotency_key)
        if existing is not None:
            return existing

        if self.attempts <= self.timeouts_before_success:
            raise PaymentTimeout("payment gateway timed out")
        if self.decline:
            raise PaymentDeclinedError("card declined")

        charge = _Charge(charge_id=f"ch_{next(self._ids)}", amount_pence=amount_pence)
        self._charges_by_key[idempotency_key] = charge
        return charge

    def refund(self, charge_id: str) -> str:
        for charge in self._charges_by_key.values():
            if charge.charge_id == charge_id:
                if not charge.refunded:
                    charge.refunded = True
                    charge.refund_id = f"rf_{next(self._refund_ids)}"
                return charge.refund_id  # type: ignore[return-value]
        raise ActivityError(f"unknown charge {charge_id}")


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
class InventoryClient:
    """Fake store inventory. ``out_of_stock`` is a set of drink names that
    cannot be reserved, triggering the substitution flow (F3)."""

    def __init__(self, out_of_stock: set[str] | None = None):
        self.out_of_stock = set(out_of_stock or ())
        self._ids = itertools.count(1)
        self.active_reservations: set[str] = set()

    def reserve(self, store_id: str, items) -> str:
        for item in items:
            if item.drink in self.out_of_stock:
                raise OutOfStockError(item.drink)
        reservation_id = f"rsv_{next(self._ids)}"
        self.active_reservations.add(reservation_id)
        return reservation_id

    def release(self, reservation_id: str) -> None:
        self.active_reservations.discard(reservation_id)


# ---------------------------------------------------------------------------
# Loyalty  (hw1 Loyalty API)
# ---------------------------------------------------------------------------
class LoyaltyClient:
    def __init__(self):
        self.accruals: list[tuple[str, int]] = []

    def accrue(self, loyalty_id: str, amount_pence: int) -> int:
        # 1 point per whole pound spent.
        points = amount_pence // 100
        self.accruals.append((loyalty_id, points))
        return points


# ---------------------------------------------------------------------------
# Barista queue + notifications
# ---------------------------------------------------------------------------
class BaristaQueue:
    def __init__(self):
        self.tickets: list[str] = []

    def enqueue(self, order_id: str) -> None:
        self.tickets.append(order_id)


class Notifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []  # (order_id, kind)

    def notify(self, order_id: str, kind: str) -> None:
        self.sent.append((order_id, kind))


@dataclass
class Clients:
    """Bundle of all downstream clients, injected into the runtime."""

    payments: PaymentsClient = field(default_factory=PaymentsClient)
    inventory: InventoryClient = field(default_factory=InventoryClient)
    loyalty: LoyaltyClient = field(default_factory=LoyaltyClient)
    barista: BaristaQueue = field(default_factory=BaristaQueue)
    notifier: Notifier = field(default_factory=Notifier)
