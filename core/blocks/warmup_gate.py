"""Start-up and channel-change warm-up gate (docs/05-standards.md 2.1-2).

The module supplies the conditions; this block only decides when they are all met.

- A ``stable_s == 0`` condition is latched: seen once is enough (first packet, first PAT).
- A ``stable_s > 0`` condition must hold for that many consecutive seconds (no loss and
  jitter within limit for 3 s). A False second resets it.
- READY when every condition is met in the same second. FAILED when ``max_s`` is reached
  first; the unmet conditions are reported, never hidden.
- Values seen during warm-up are not judged; ``met_at`` feeds the report's Start-up item.
- ``force_ready`` ends the wait without the conditions (channel change, 5 s limit).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum


class GateState(StrEnum):
    WAITING = "waiting"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class Condition:
    name: str
    stable_s: int = 0


class WarmupGate:
    def __init__(self, conditions: Iterable[Condition], max_s: float) -> None:
        self._conditions = {c.name: c for c in conditions}
        if not self._conditions:
            raise ValueError("warm-up needs at least one condition")
        if max_s <= 0:
            raise ValueError("max_s must be positive")
        self.max_s = max_s
        self.state = GateState.WAITING
        self.ready_at: float | None = None
        self.met_at: dict[str, float | None] = dict.fromkeys(self._conditions)
        self._run: dict[str, int] = dict.fromkeys(self._conditions, 0)

    def on_second(self, t_s: float, observed: Mapping[str, bool]) -> GateState:
        """Feed one second (``t_s`` = seconds since join). Missing names count as False."""
        if self.state is not GateState.WAITING:
            raise RuntimeError(f"warm-up already {self.state}")
        unknown = set(observed) - set(self._conditions)
        if unknown:
            raise ValueError(f"unknown warm-up conditions: {sorted(unknown)}")
        for name, cond in self._conditions.items():
            ok = observed.get(name, False)
            if cond.stable_s == 0:
                if ok and self.met_at[name] is None:
                    self.met_at[name] = t_s
            else:
                self._run[name] = self._run[name] + 1 if ok else 0
                if self._run[name] >= cond.stable_s and self.met_at[name] is None:
                    self.met_at[name] = t_s
        if not self.unmet():
            self.state = GateState.READY
            self.ready_at = t_s
        elif t_s >= self.max_s:
            self.state = GateState.FAILED
        return self.state

    def force_ready(self, t_s: float) -> None:
        """Start measuring although conditions are unmet (a channel change gives up
        waiting after 5 s, 05 2.1-2 rule 4). ``unmet()`` still names what was not met."""
        if self.state is not GateState.WAITING:
            raise RuntimeError(f"warm-up already {self.state}")
        self.state = GateState.READY
        self.ready_at = t_s

    def unmet(self) -> list[str]:
        """Conditions not satisfied right now (a stable one must still be holding)."""
        return [
            name
            for name, cond in self._conditions.items()
            if (
                self.met_at[name] is None if cond.stable_s == 0 else self._run[name] < cond.stable_s
            )
        ]
