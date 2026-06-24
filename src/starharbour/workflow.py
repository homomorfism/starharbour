"""The Order workflow: a deterministic state machine.

Two pure functions make up the whole business logic, and neither one performs
IO — that is what keeps replay deterministic (the determinism constraint the
requirements ask us to explain):

  * ``apply(state, event)``  — the *evolve* half of event sourcing. Folds one
    event into the state. Given the same event log it always rebuilds the same
    state, so a crashed worker can recover by replaying history (F6).

  * ``OrderWorkflow.decide(state)`` — the *decide* half. Looks only at the
    current state and returns the side effects that should happen next: which
    activity to run and which timers should be armed. It never calls a client
    directly; it just expresses intent. The runtime (``engine.py``) turns that
    intent into real IO and feeds the results back as new events.

That decide/evolve split is the core idea: business decisions are a pure
function of durable state, so the engine can re-run them after any failure
without re-doing side effects or making different choices.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from starharbour import events as ev
from starharbour.domain import (
    SUBSTITUTIONS,
    LineItem,
    OrderState,
    OrderStatus,
)

# Timer durations (seconds). Small-ish defaults; tests override via Runtime.
BREW_SLA_SECONDS = 10 * 60  # barista too slow → escalate
PICKUP_EXPIRY_SECONDS = 30 * 60  # customer never collects → abandon
SUBSTITUTION_DEADLINE_SECONDS = 2 * 60  # no answer to substitution → cancel

TIMER_BREW_SLA = "brew_sla"
TIMER_PICKUP = "pickup"
TIMER_SUBSTITUTION = "substitution"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    base_backoff_s: float = 1.0
    max_backoff_s: float = 60.0


@dataclass(frozen=True)
class ActivitySpec:
    """An activity the runtime should execute. ``name`` is looked up in the
    runtime's activity registry."""

    name: str
    retry: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass(frozen=True)
class Effects:
    """What the decider wants to happen next, given the current state."""

    activity: ActivitySpec | None = None
    # timers that should be armed right now (name -> duration seconds). The
    # runtime arms any that aren't already running and cancels running timers
    # that are absent here.
    timers: dict[str, float] = field(default_factory=dict)


# Retry policies per activity. Payment gets generous retries (transient gateway
# timeouts, F1); reservation/loyalty get a few; pure-local steps get one.
_PAYMENT_RETRY = RetryPolicy(max_attempts=5, base_backoff_s=1.0, max_backoff_s=30.0)
_INVENTORY_RETRY = RetryPolicy(max_attempts=3, base_backoff_s=1.0)
_LOYALTY_RETRY = RetryPolicy(max_attempts=3, base_backoff_s=1.0)
_LOCAL_RETRY = RetryPolicy(max_attempts=1)


# ===========================================================================
# evolve:  apply(state, event) -> state
# ===========================================================================
def apply(state: OrderState | None, event: ev.Event) -> OrderState:
    """Fold one event into the order state. Pure and total.

    Events that don't make sense for the current status are ignored (returned
    unchanged) so that late/duplicate signals are harmless.
    """
    # --- creation ---
    if isinstance(event, ev.OrderPlaced):
        return OrderState(
            order_id=event.order_id,
            store_id=event.store_id,
            items=event.items,
            payment_method=event.payment_method,
            loyalty_id=event.loyalty_id,
            idempotency_key=event.idempotency_key,
            status=OrderStatus.VALIDATING,
        )

    assert state is not None, "first event must be OrderPlaced"

    if state.is_terminal:
        return state  # nothing changes once we're done

    # --- a cancel signal can arrive at almost any time ---
    if isinstance(event, ev.CustomerCancelled):
        return _on_cancel(state, event.reason)

    # --- validation ---
    if isinstance(event, ev.OrderValidated) and state.status == OrderStatus.VALIDATING:
        return state.with_(
            price_pence=event.price_pence,
            status=OrderStatus.RESERVING_INVENTORY,
        )
    if isinstance(event, ev.ValidationFailed) and state.status == OrderStatus.VALIDATING:
        return state.with_(
            status=OrderStatus.COMPENSATING,
            terminal_target=OrderStatus.FAILED,
            do_refund=False,
            outcome_reason=event.reason,
        )

    # --- inventory ---
    if isinstance(event, ev.InventoryReserved) and state.status == OrderStatus.RESERVING_INVENTORY:
        return state.with_(
            reservation_id=event.reservation_id,
            inventory_reserved=True,
            status=OrderStatus.TAKING_PAYMENT,
        )
    if (
        isinstance(event, ev.InventoryOutOfStock)
        and state.status == OrderStatus.RESERVING_INVENTORY
    ):
        substitute = SUBSTITUTIONS.get(event.item)
        if substitute is None:
            # No substitution to offer → cancel the order.
            return _on_cancel(state, f"out_of_stock:{event.item}")
        return state.with_(
            status=OrderStatus.AWAITING_SUBSTITUTION,
            sub_item=event.item,
            sub_offer=substitute,
            sub_offered=False,
        )
    if isinstance(event, ev.InventoryReleased):
        return state.with_(inventory_released=True)

    # --- substitution branch ---
    if (
        isinstance(event, ev.SubstitutionOffered)
        and state.status == OrderStatus.AWAITING_SUBSTITUTION
    ):
        return state.with_(sub_offered=True)
    if (
        isinstance(event, ev.SubstitutionAnswered)
        and state.status == OrderStatus.AWAITING_SUBSTITUTION
    ):
        if event.accepted:
            substitute = state.sub_offer or ""
            new_items = tuple(
                LineItem(substitute, item.qty) if item.drink == state.sub_item else item
                for item in state.items
            )
            return state.with_(
                items=new_items,
                status=OrderStatus.RESERVING_INVENTORY,
                sub_item=None,
                sub_offer=None,
                sub_offered=False,
            )
        return _on_cancel(state, "substitution_declined")
    if (
        isinstance(event, ev.SubstitutionDeadline)
        and state.status == OrderStatus.AWAITING_SUBSTITUTION
    ):
        return _on_cancel(state, "substitution_timeout")

    # --- payment ---
    if isinstance(event, ev.PaymentTaken) and state.status == OrderStatus.TAKING_PAYMENT:
        return state.with_(
            charge_id=event.charge_id,
            payment_charged=True,
            status=OrderStatus.BREWING,
        )
    if isinstance(event, ev.PaymentDeclined) and state.status == OrderStatus.TAKING_PAYMENT:
        return state.with_(
            status=OrderStatus.COMPENSATING,
            terminal_target=OrderStatus.FAILED,
            do_refund=False,
            outcome_reason=event.reason,
        )
    if isinstance(event, ev.PaymentRefunded):
        return state.with_(payment_refunded=True, refund_id=event.refund_id)

    # --- brewing ---
    if isinstance(event, ev.TicketQueued) and state.status == OrderStatus.BREWING:
        return state.with_(ticket_queued=True)
    if isinstance(event, ev.BrewEscalated) and state.status == OrderStatus.BREWING:
        return state.with_(brew_escalated=True)
    if isinstance(event, ev.BrewSlaElapsed) and state.status == OrderStatus.BREWING:
        # SLA breach: escalate once, but keep waiting for the barista. The
        # escalation itself (notify manager / comp) is an activity.
        return state  # handled by decide → BrewEscalated event
    if isinstance(event, ev.BaristaMarkedReady) and state.status == OrderStatus.BREWING:
        return state.with_(status=OrderStatus.READY)

    # --- ready / pickup ---
    if isinstance(event, ev.CustomerNotified):
        if event.kind == "ready":
            return state.with_(notified_ready=True)
        return state
    if isinstance(event, ev.CustomerCollected) and state.status == OrderStatus.READY:
        return state.with_(status=OrderStatus.ACCRUING_LOYALTY)
    if isinstance(event, ev.PickupExpired) and state.status == OrderStatus.READY:
        # Customer no-show: drinks are made and paid for → waste policy, no
        # refund. Abandon the order (F5).
        return state.with_(
            status=OrderStatus.COMPENSATING,
            terminal_target=OrderStatus.ABANDONED,
            do_refund=False,
            outcome_reason="pickup_expired",
        )

    # --- loyalty / completion ---
    if isinstance(event, ev.LoyaltyAccrued) and state.status == OrderStatus.ACCRUING_LOYALTY:
        return state.with_(loyalty_points=event.points, status=OrderStatus.COMPLETED)

    # --- terminal markers (emitted by the finalize activity) ---
    if isinstance(event, ev.OrderFailed):
        return state.with_(
            status=OrderStatus.FAILED, outcome_reason=event.reason or state.outcome_reason
        )
    if isinstance(event, ev.OrderCancelled):
        return state.with_(
            status=OrderStatus.CANCELLED, outcome_reason=event.reason or state.outcome_reason
        )
    if isinstance(event, ev.OrderAbandoned):
        return state.with_(
            status=OrderStatus.ABANDONED, outcome_reason=event.reason or state.outcome_reason
        )

    # Unknown / out-of-context event → no-op (idempotent, tolerant of replays).
    return state


def _on_cancel(state: OrderState, reason: str) -> OrderState:
    """Route a cancel/abort into the compensation state.

    Behaviour differs before vs. after the drinks are made:
      * before READY  → refundable cancel  → CANCELLED
      * at/after READY → drinks are wasted → ABANDONED, no refund
    """
    if state.status in (OrderStatus.READY, OrderStatus.ACCRUING_LOYALTY):
        return state.with_(
            status=OrderStatus.COMPENSATING,
            terminal_target=OrderStatus.ABANDONED,
            do_refund=False,
            outcome_reason=reason,
        )
    return state.with_(
        status=OrderStatus.COMPENSATING,
        terminal_target=OrderStatus.CANCELLED,
        do_refund=True,
        outcome_reason=reason,
    )


# ===========================================================================
# decide:  state -> Effects
# ===========================================================================
class OrderWorkflow:
    """Stateless decider. All methods are pure functions of the passed state."""

    @staticmethod
    def decide(state: OrderState) -> Effects:
        s = state.status

        if s == OrderStatus.VALIDATING:
            return Effects(activity=ActivitySpec("validate", _LOCAL_RETRY))

        if s == OrderStatus.RESERVING_INVENTORY:
            return Effects(activity=ActivitySpec("reserve_inventory", _INVENTORY_RETRY))

        if s == OrderStatus.AWAITING_SUBSTITUTION:
            if not state.sub_offered:
                return Effects(activity=ActivitySpec("offer_substitution", _LOCAL_RETRY))
            # Offer is out; wait for the customer, bounded by a deadline.
            return Effects(timers={TIMER_SUBSTITUTION: SUBSTITUTION_DEADLINE_SECONDS})

        if s == OrderStatus.TAKING_PAYMENT:
            return Effects(activity=ActivitySpec("take_payment", _PAYMENT_RETRY))

        if s == OrderStatus.BREWING:
            if not state.ticket_queued:
                return Effects(activity=ActivitySpec("queue_ticket", _LOCAL_RETRY))
            if not state.brew_escalated:
                # Wait for the barista, guarded by the brew-SLA timer.
                return Effects(timers={TIMER_BREW_SLA: BREW_SLA_SECONDS})
            # Already escalated → keep waiting, timer disarmed.
            return Effects()

        if s == OrderStatus.READY:
            if not state.notified_ready:
                return Effects(activity=ActivitySpec("notify_ready", _LOCAL_RETRY))
            # Wait for pickup, guarded by the pickup-expiry timer.
            return Effects(timers={TIMER_PICKUP: PICKUP_EXPIRY_SECONDS})

        if s == OrderStatus.ACCRUING_LOYALTY:
            return Effects(activity=ActivitySpec("accrue_loyalty", _LOYALTY_RETRY))

        if s == OrderStatus.COMPENSATING:
            # Undo in order: refund money, release inventory, then finalize.
            if state.do_refund and state.needs_refund():
                return Effects(activity=ActivitySpec("refund_payment", _PAYMENT_RETRY))
            if state.needs_release():
                return Effects(activity=ActivitySpec("release_inventory", _INVENTORY_RETRY))
            return Effects(activity=ActivitySpec("finalize", _LOCAL_RETRY))

        # Terminal or transient PLACED → nothing to do.
        return Effects()

    # --- SLA escalation is a separate decision triggered by the timer event ---
    @staticmethod
    def wants_brew_escalation(state: OrderState) -> bool:
        return state.status == OrderStatus.BREWING and not state.brew_escalated
