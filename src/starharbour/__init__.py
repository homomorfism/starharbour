"""StarHarbour order-ahead durable workflow.

A self-contained, event-sourced durable workflow engine implementing the
order-ahead fulfilment process from docs/requirements.md.

Public surface:
  * ``WorkflowRuntime`` — the durable runtime (start/signal/advance_time).
  * ``ManualClock`` / ``RealClock`` — clocks for tests vs production.
  * ``Clients`` and the individual fake clients — downstream systems.
  * ``LineItem`` / ``OrderStatus`` — domain types.
  * event classes (``events``) for sending signals.
"""

from starharbour import events
from starharbour.clients import (
    BaristaQueue,
    Clients,
    InventoryClient,
    LoyaltyClient,
    Notifier,
    PaymentsClient,
)
from starharbour.domain import LineItem, OrderState, OrderStatus
from starharbour.engine import ManualClock, RealClock, WorkflowRuntime

# Convenience signal builders.
from starharbour.events import (
    BaristaMarkedReady,
    CustomerCancelled,
    CustomerCollected,
    SubstitutionAnswered,
)
from starharbour.store import FileEventStore, InMemoryEventStore

__all__ = [
    "WorkflowRuntime",
    "ManualClock",
    "RealClock",
    "Clients",
    "PaymentsClient",
    "InventoryClient",
    "LoyaltyClient",
    "BaristaQueue",
    "Notifier",
    "LineItem",
    "OrderState",
    "OrderStatus",
    "InMemoryEventStore",
    "FileEventStore",
    "events",
    "BaristaMarkedReady",
    "CustomerCollected",
    "CustomerCancelled",
    "SubstitutionAnswered",
]
