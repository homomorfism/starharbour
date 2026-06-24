"""The durable runtime (a tiny Temporal/Cadence in ~150 lines).

The runtime is the only component that touches the outside world. It:

  1. drives the decider/evolve loop — ask ``decide`` what to do, do it, record
     the result event, fold it into state, repeat;
  2. runs activities with retry + exponential backoff, mapping permanent
     failures to failure events;
  3. arms and fires timers;
  4. accepts external signals;
  5. persists every event and can rebuild state from history after a crash.

Determinism note: the runtime is the *non-deterministic* part (clocks, IO,
retries). The workflow (``decide``/``apply``) is pure. Keeping them apart is
what lets us replay history to recover: replay only re-runs the pure code, and
only re-issues activities that have no recorded result yet — and those are
idempotent. That is the whole game (F6).

The loop is synchronous and single-stepped so it is fully controllable from
tests: ``start`` runs until the workflow blocks on an external input; then a
test calls ``signal(...)`` to deliver an event or ``advance_time(...)`` to fire
timers, and the loop continues.
"""

from __future__ import annotations

import time

from starharbour import events as ev
from starharbour.activities import default_registry
from starharbour.clients import ActivityError, Clients
from starharbour.domain import OrderState
from starharbour.store import InMemoryEventStore
from starharbour.workflow import (
    TIMER_BREW_SLA,
    TIMER_PICKUP,
    TIMER_SUBSTITUTION,
    OrderWorkflow,
    apply,
)


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------
class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def advance(self, seconds: float) -> None:  # no-op for real time
        pass


class ManualClock:
    """Virtual clock for tests. ``sleep`` and ``advance`` both move time
    forward; only the runtime decides when timers actually fire."""

    def __init__(self, start: float = 0.0):
        self._now = start

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds

    def advance(self, seconds: float) -> None:
        self._now += seconds


# maps a timer name to the event it produces when it fires
_TIMER_EVENT = {
    TIMER_PICKUP: ev.PickupExpired,
    TIMER_SUBSTITUTION: ev.SubstitutionDeadline,
}


class WorkflowRuntime:
    def __init__(
        self,
        store=None,
        clients: Clients | None = None,
        clock=None,
        registry=None,
    ):
        self.store = store if store is not None else InMemoryEventStore()
        self.clients = clients if clients is not None else Clients()
        self.clock = clock if clock is not None else RealClock()
        self.activities = registry if registry is not None else default_registry()
        self.timers: dict[str, float] = {}  # name -> absolute fire time
        self.state: OrderState | None = None
        # Rebuild from any existing history (crash recovery).
        self._recover()

    # ------------------------------------------------------------------ API
    def start(
        self,
        order_id: str,
        store_id: str,
        items,
        payment_method: str,
        loyalty_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> OrderState:
        if self.state is not None:
            raise RuntimeError("workflow already started")
        placed = ev.OrderPlaced(
            order_id=order_id,
            store_id=store_id,
            items=tuple(items),
            payment_method=payment_method,
            loyalty_id=loyalty_id,
            idempotency_key=idempotency_key or f"idem-{order_id}",
        )
        self._commit(placed)
        self.run()
        return self.state  # type: ignore[return-value]

    def signal(self, event: ev.Event) -> OrderState:
        """Deliver an external signal (barista ready, customer collected,
        cancel, substitution answer) and continue the workflow."""
        self._commit(event)
        self.run()
        return self.state  # type: ignore[return-value]

    def advance_time(self, seconds: float) -> OrderState:
        """Advance the (virtual) clock and fire any timers now due."""
        self.clock.advance(seconds)
        self._fire_due_timers()
        return self.state  # type: ignore[return-value]

    # --------------------------------------------------------------- engine
    def run(self) -> None:
        """Drive decide→act→evolve until the workflow blocks or terminates."""
        while True:
            state = self.state
            assert state is not None
            if state.is_terminal:
                self.timers.clear()
                return
            effects = OrderWorkflow.decide(state)
            self._reconcile_timers(effects.timers)
            if effects.activity is None:
                return  # blocked on an external input (signal or timer)
            event = self._execute(effects.activity, state)
            self._commit(event)

    # ------------------------------------------------------------- internal
    def _execute(self, spec, state: OrderState) -> ev.Event:
        impl, failure_mapper = self.activities[spec.name]
        attempt = 0
        backoff = spec.retry.base_backoff_s
        while True:
            attempt += 1
            try:
                return impl(state, self.clients)
            except ActivityError as exc:
                exhausted = attempt >= spec.retry.max_attempts
                if not exc.retryable or exhausted:
                    if failure_mapper is None:
                        raise
                    return failure_mapper(state, exc)
                self.clock.sleep(min(backoff, spec.retry.max_backoff_s))
                backoff = min(backoff * 2, spec.retry.max_backoff_s)

    def _reconcile_timers(self, wanted: dict[str, float]) -> None:
        now = self.clock.now()
        for name, duration in wanted.items():
            if name not in self.timers:
                self.timers[name] = now + duration  # arm once
        for name in list(self.timers):
            if name not in wanted:
                del self.timers[name]  # cancel timers no longer desired

    def _fire_due_timers(self) -> None:
        now = self.clock.now()
        while True:
            due = sorted((t, n) for n, t in self.timers.items() if t <= now)
            if not due:
                self.run()  # make sure we re-arm after any state changes
                return
            _, name = due[0]
            del self.timers[name]
            self._fire_timer(name)
            self.run()

    def _fire_timer(self, name: str) -> None:
        state = self.state
        assert state is not None
        if name == TIMER_BREW_SLA:
            # SLA breach escalation is itself an activity.
            if OrderWorkflow.wants_brew_escalation(state):
                spec = _ESCALATE_SPEC
                event = self._execute(spec, state)
                self._commit(event)
            return
        event_cls = _TIMER_EVENT.get(name)
        if event_cls is not None:
            self._commit(event_cls())

    def _commit(self, event: ev.Event) -> None:
        stamped = self.store.append(event)
        self.state = apply(self.state, stamped)

    def _recover(self) -> None:
        history = self.store.load()
        for event in history:
            self.state = apply(self.state, event)


# A throwaway spec for the SLA-escalation activity (single attempt).
from starharbour.workflow import ActivitySpec, RetryPolicy  # noqa: E402

_ESCALATE_SPEC = ActivitySpec("escalate_brew", RetryPolicy(max_attempts=1))
