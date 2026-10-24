"""Whole-PC CPU overload during a test (docs/09-exceptions.md SYS-003).

One job: from one CPU reading a second, say when the PC has been above ``LIMIT_PCT``
for more than ``OVER_S`` seconds (``start``) and when it is below again (``end``), and
name the programs using the most CPU at that moment. Linux reads /proc; elsewhere the
list is empty (V18).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

LIMIT_PCT = 90.0
OVER_S = 10


class CpuWatch:
    def __init__(self, limit_pct: float = LIMIT_PCT, over_s: int = OVER_S) -> None:
        self.limit_pct, self.over_s = limit_pct, over_s
        self._above = 0
        self.busy = False

    def update(self, cpu_pct: float | None) -> str | None:
        """Once a second. ``start`` / ``end`` on a change, else None."""
        if cpu_pct is None:
            return None
        if cpu_pct >= self.limit_pct:
            self._above += 1
            if not self.busy and self._above > self.over_s:
                self.busy = True
                return "start"
            return None
        self._above = 0
        if self.busy:
            self.busy = False
            return "end"
        return None

    def reset(self) -> str | None:
        """The test ended: a busy period ends with it."""
        self._above = 0
        was, self.busy = self.busy, False
        return "end" if was else None


def top_processes(n: int = 3, sample_s: float = 0.5,
                  proc: Path = Path("/proc")) -> list[dict[str, object]]:  # fmt: skip
    """The ``n`` programs with the most CPU over ``sample_s`` (``name``, ``pid``, ``pct``
    of one core)."""
    first = _ticks(proc)
    if not first:
        return []
    time.sleep(sample_s)
    second = _ticks(proc)
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    used = []
    for pid, (name, t) in second.items():
        if pid in first:
            used.append((t - first[pid][1], pid, name))
    used.sort(reverse=True)
    return [{"name": name, "pid": pid, "pct": round(100.0 * d / hz / sample_s, 1)}
            for d, pid, name in used[:n] if d > 0]  # fmt: skip


def _ticks(proc: Path) -> dict[int, tuple[str, int]]:
    out: dict[int, tuple[str, int]] = {}
    if not proc.is_dir():
        return out
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            raw = (d / "stat").read_text()
        except OSError:
            continue
        name = raw[raw.find("(") + 1 : raw.rfind(")")]
        fields = raw[raw.rfind(")") + 2 :].split()
        try:
            out[int(d.name)] = (name, int(fields[11]) + int(fields[12]))  # utime + stime
        except (IndexError, ValueError):
            continue
    return out
