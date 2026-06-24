"""Shared test fixtures and helpers.

Import paths (``src`` and ``tests``) are configured via ``pyproject.toml``
``[tool.pytest.ini_options] pythonpath``.
"""

import pytest

from starharbour import (
    Clients,
    LineItem,
    ManualClock,
    WorkflowRuntime,
)

# The canonical order from the scenario: 2× Latte, 1× Cold Brew at London-01.
STANDARD_ITEMS = [LineItem("latte", 2), LineItem("cold_brew", 1)]


@pytest.fixture
def items():
    return list(STANDARD_ITEMS)


def make_runtime(clients=None, store=None):
    """A runtime wired to a virtual clock so timers are deterministic."""
    return WorkflowRuntime(
        store=store,
        clients=clients if clients is not None else Clients(),
        clock=ManualClock(),
    )


def place_standard_order(rt, order_id="order-1", loyalty_id="loyalty-1"):
    return rt.start(
        order_id=order_id,
        store_id="store-london-01",
        items=list(STANDARD_ITEMS),
        payment_method="card_saved",
        loyalty_id=loyalty_id,
        idempotency_key=f"idem-{order_id}",
    )
