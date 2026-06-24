"""Unit tests for the fake clients — focused on the idempotency and failure
semantics the workflow relies on."""

import pytest

from starharbour.clients import (
    InventoryClient,
    LoyaltyClient,
    OutOfStockError,
    PaymentDeclinedError,
    PaymentsClient,
    PaymentTimeout,
)
from starharbour.domain import LineItem


# --------------------------------------------------------------------------
# Payments idempotency
# --------------------------------------------------------------------------
def test_charge_is_idempotent_per_key():
    pay = PaymentsClient()
    c1 = pay.charge("idem-1", 1100)
    c2 = pay.charge("idem-1", 1100)
    assert c1 is c2  # same charge returned
    assert len(pay._charges_by_key) == 1


def test_different_keys_create_different_charges():
    pay = PaymentsClient()
    c1 = pay.charge("idem-1", 1100)
    c2 = pay.charge("idem-2", 1100)
    assert c1.charge_id != c2.charge_id


def test_timeouts_then_success():
    pay = PaymentsClient(timeouts_before_success=2)
    with pytest.raises(PaymentTimeout):
        pay.charge("k", 100)
    with pytest.raises(PaymentTimeout):
        pay.charge("k", 100)
    charge = pay.charge("k", 100)  # third attempt succeeds
    assert charge.charge_id is not None
    assert pay.attempts == 3


def test_timeout_is_retryable_decline_is_not():
    assert PaymentTimeout().retryable is True
    assert PaymentDeclinedError().retryable is False


def test_permanent_decline_raises():
    pay = PaymentsClient(decline=True)
    with pytest.raises(PaymentDeclinedError):
        pay.charge("k", 100)


def test_refund_is_idempotent():
    pay = PaymentsClient()
    charge = pay.charge("k", 100)
    r1 = pay.refund(charge.charge_id)
    r2 = pay.refund(charge.charge_id)
    assert r1 == r2


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
def test_reserve_then_release():
    inv = InventoryClient()
    rid = inv.reserve("store-london-01", [LineItem("latte", 2)])
    assert rid in inv.active_reservations
    inv.release(rid)
    assert rid not in inv.active_reservations


def test_out_of_stock_raises_with_item():
    inv = InventoryClient(out_of_stock={"cold_brew"})
    with pytest.raises(OutOfStockError) as exc:
        inv.reserve("store-london-01", [LineItem("latte", 1), LineItem("cold_brew", 1)])
    assert exc.value.item == "cold_brew"
    assert OutOfStockError("x").retryable is False


# --------------------------------------------------------------------------
# Loyalty
# --------------------------------------------------------------------------
def test_loyalty_one_point_per_pound():
    loy = LoyaltyClient()
    assert loy.accrue("member", 1100) == 11
    assert loy.accruals == [("member", 11)]
