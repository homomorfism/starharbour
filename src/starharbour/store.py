"""Event store — the durable history.

The store is append-only. State is *never* persisted directly; it is always
rebuilt by replaying events through ``workflow.apply``. That is what lets a
worker crash at any point and recover with no double-charge and no lost state
(requirement F6).

Two implementations:
  * ``InMemoryEventStore`` — for unit tests.
  * ``FileEventStore``     — appends one JSON line per event to disk, so a brand
    new process can reload the full history and continue.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict

from starharbour import events as ev
from starharbour.domain import LineItem


class InMemoryEventStore:
    def __init__(self):
        self._events: list[ev.Event] = []

    def append(self, event: ev.Event) -> ev.Event:
        stamped = _with_seq(event, len(self._events))
        self._events.append(stamped)
        return stamped

    def load(self) -> list[ev.Event]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)


class FileEventStore:
    """Persists events as JSON lines. Reopening the file replays history."""

    def __init__(self, path: str):
        self.path = path
        self._count = 0
        if os.path.exists(path):
            with open(path) as fh:
                self._count = sum(1 for _ in fh)

    def append(self, event: ev.Event) -> ev.Event:
        stamped = _with_seq(event, self._count)
        with open(self.path, "a") as fh:
            fh.write(json.dumps(_to_json(stamped)) + "\n")
        self._count += 1
        return stamped

    def load(self) -> list[ev.Event]:
        if not os.path.exists(self.path):
            return []
        out: list[ev.Event] = []
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(_from_json(json.loads(line)))
        return out

    def __len__(self) -> int:
        return self._count


# ---------------------------------------------------------------------------
# (de)serialisation
# ---------------------------------------------------------------------------
def _with_seq(event: ev.Event, seq: int) -> ev.Event:
    from dataclasses import replace

    return replace(event, seq=seq)


# Registry of event classes by name, for deserialisation.
_EVENT_CLASSES = {
    cls.__name__: cls
    for cls in vars(ev).values()
    if isinstance(cls, type) and issubclass(cls, ev.Event)
}


def _to_json(event: ev.Event) -> dict:
    data = asdict(event)
    data["__type__"] = type(event).__name__
    # LineItem tuples become lists of dicts via asdict already.
    return data


def _from_json(data: dict) -> ev.Event:
    type_name = data.pop("__type__")
    cls = _EVENT_CLASSES[type_name]
    kwargs = dict(data)
    if "items" in kwargs and kwargs["items"]:
        kwargs["items"] = tuple(LineItem(**it) for it in kwargs["items"])
    elif "items" in kwargs:
        kwargs["items"] = ()
    return cls(**kwargs)
