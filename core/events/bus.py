"""The one event flow every event passes through (docs/12-architecture.md 4-1).

Format is ``events.log`` (docs/02-data.md 8). Subscribers are the event log file, the
heartbeat's ``events`` list, the Problems window, and later the export module.
A subscriber that raises does not stop the others; its failure is published once as
an event of its own, so nothing is hidden.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal

Level = Literal["info", "warn", "error"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Event:
    code: str  # ZL-... from docs/09-exceptions.md, or ZL-INFO for plain information
    level: Level
    text: str
    device: str = ""
    elapsed_s: float | None = None
    ts_utc: str = field(default_factory=utc_now)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d["data"]:
            del d["data"]
        return d


Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        """Returns a function that unsubscribes."""
        with self._lock:
            self._subscribers.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return unsubscribe

    def publish(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(event)
            except Exception as e:  # one broken subscriber must not stop the others
                if event.code != "ZL-SYS-008":
                    self.publish(
                        Event(
                            "ZL-SYS-008",
                            "error",
                            f"Event subscriber failed: {e!r}",
                            data={"subscriber": getattr(fn, "__qualname__", repr(fn))},
                        )
                    )


class EventLog:
    """``events.log`` writer: one JSON line per event, flushed at once."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] | None = path.open("a", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()

    def __call__(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            if self._file is None:
                return
            self._file.write(line + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


class RecentEvents:
    """Events since the last heartbeat (docs/02-data.md 5 ``events``). Bounded."""

    def __init__(self, keep: int = 200) -> None:
        self._keep = keep
        self._items: list[Event] = []
        self.dropped = 0
        self._lock = threading.Lock()

    def __call__(self, event: Event) -> None:
        with self._lock:
            self._items.append(event)
            if len(self._items) > self._keep:
                self._items.pop(0)
                self.dropped += 1

    def take(self) -> list[Event]:
        with self._lock:
            items, self._items = self._items, []
            return items
