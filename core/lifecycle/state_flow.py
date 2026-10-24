"""Explicit state flow for long-running parts (CLAUDE.md checklist, RUN-012).

IDLE -> STARTING -> RUNNING -> STOPPING -> IDLE, and FAULT from anywhere.
- STARTING -> STOPPING: a failed or cancelled start rolls back what was started.
- FAULT -> STOPPING: cleanup after a fault; FAULT is left only through a confirmed stop.
- A change outside this table is refused and changes nothing.
Every change is published with the new state, so screens and the event log follow it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from enum import StrEnum

from core.protocol.envelope import ZlError

# the safe-stop reason when the Controller came back with a new boot (F11); a module may
# keep work that is not part of a test then (GAP-124: a movie being made)
CONTROLLER_RESTARTED = "Controller restarted"


class State(StrEnum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAULT = "FAULT"


ALLOWED: dict[State, frozenset[State]] = {
    State.IDLE: frozenset({State.STARTING}),
    State.STARTING: frozenset({State.RUNNING, State.STOPPING}),
    State.RUNNING: frozenset({State.STOPPING}),
    State.STOPPING: frozenset({State.IDLE}),
    State.FAULT: frozenset({State.STOPPING}),
}


class StateFlow:
    def __init__(
        self, name: str, on_change: Callable[[str, State, State, str], None] | None = None
    ) -> None:
        self.name = name
        self._state = State.IDLE
        self._on_change = on_change
        self._lock = threading.Lock()

    @property
    def state(self) -> State:
        return self._state

    def move(self, to: State, reason: str = "", expect: State | None = None) -> State:
        """Change state. ``expect`` guards against acting on a stale view.
        Raises ZlError RUN-012 when the change is not allowed."""
        with self._lock:
            now = self._state
            allowed = to is State.FAULT or to in ALLOWED[now]
            if (expect is not None and now is not expect) or not allowed or now is to:
                raise ZlError(
                    "RUN-012",
                    f"{self.name} cannot go to {to} now",
                    f"It is {now}.",
                    detail=reason,
                )
            self._state = to
        if self._on_change:
            self._on_change(self.name, now, to, reason)
        return now
