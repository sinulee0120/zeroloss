"""1-second records -> 5-minute summaries (docs/02-data.md 6).

The module gives the rules ``output name -> (1-second key, Agg)``; this block only groups
seconds, so one key can feed several outputs (jitter avg and max).

- Cells are aligned on elapsed time: ``index = e // cell_s`` (e counts from measurement start).
- When ``segment_key`` changes (channel change), the cell is split: each join segment is
  sent on its own with the same ``index`` and a new ``join_segment``.
- A piece with fewer than ``cell_s`` seconds is ``partial`` (start, stop, gaps, split).
- Fields that are missing or None in a second are skipped for that second.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class Agg(StrEnum):
    SUM = "sum"
    MAX = "max"
    MIN = "min"
    AVG = "avg"
    LAST = "last"


def _combine(agg: Agg, values: list[Any]) -> Any:
    if not values:
        return None
    if agg is Agg.SUM:
        return sum(values)
    if agg is Agg.MAX:
        return max(values)
    if agg is Agg.MIN:
        return min(values)
    if agg is Agg.AVG:
        return sum(values) / len(values)
    return values[-1]


class Summarizer:
    def __init__(
        self,
        rules: Mapping[str, tuple[str, Agg]],
        cell_s: int = 300,
        segment_key: str | None = None,
    ) -> None:
        if cell_s <= 0:
            raise ValueError("cell_s must be positive")
        self._rules = dict(rules)
        self._cell_s = cell_s
        self._segment_key = segment_key
        self._join_segment = 0
        self._segment_value: Any = None
        self._last_e: int | None = None
        self._index: int | None = None
        self._start_e = 0
        self._seconds = 0
        self._values: dict[str, list[Any]] = {}

    def add(self, record: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Add one second. Returns the summaries closed by it (0, 1 or 2 when split)."""
        e = int(record["e"])
        if self._last_e is not None and e <= self._last_e:
            raise ValueError(f"elapsed second {e} after {self._last_e}")
        self._last_e = e
        closed: list[dict[str, Any]] = []
        index = e // self._cell_s
        segment = record.get(self._segment_key) if self._segment_key else None
        new_segment = (
            self._segment_key is not None and self._seconds > 0 and (segment != self._segment_value)
        )
        if self._index is not None and (index != self._index or new_segment):
            closed.append(self._close())
        if new_segment:
            self._join_segment += 1
        if self._seconds == 0:
            self._index, self._start_e, self._segment_value = index, e, segment
            self._values = {name: [] for name in self._rules}
        self._seconds += 1
        for name, values in self._values.items():
            v = record.get(self._rules[name][0])
            if v is not None:
                values.append(v)
        return closed

    def flush(self) -> list[dict[str, Any]]:
        """On stop, fault or disconnect: close the open piece (always partial or full)."""
        return [self._close()] if self._seconds else []

    def _close(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "index": self._index,
            "start_elapsed_s": self._start_e,
            "duration_s": self._seconds,
            "partial": self._seconds < self._cell_s,
            "join_segment": self._join_segment,
        }
        if self._segment_key:
            summary[self._segment_key] = self._segment_value
        summary["values"] = {
            name: _combine(agg, self._values[name]) for name, (_, agg) in self._rules.items()
        }
        self._seconds = 0
        self._index = None
        return summary
