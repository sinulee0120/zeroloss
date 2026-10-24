"""Problems board (docs/03-features.md F21, 02 2-2): every warning and error event
becomes one row per code + device, with What / Why / Fix.

The board subscribes to the Controller's event bus (its own events and the node events
it forwards, 02 5 ``events``), so it is the one list screens and the CLI read (11-ui 1.1).
Rules (F21, ISA-18.2): one row per code + device with a count; a problem that comes
back within ``CHATTER_S`` after it was cleared is the same alarm again; more than
``FLOOD_N`` new problems in ``FLOOD_S`` is a flood; active for ``STALE_S`` is stale.
A problem is cleared by an event carrying ``data.clears`` (02 8) or when the test ends.
Resolved rows are kept up to ``KEEP_RESOLVED`` (memory stays flat over 100 h, 11-ui 1.4).
Texts come from ``catalog.json`` (one English entry per 09-exceptions code).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.events.bus import Event, utc_now

_FILE = Path(__file__).with_name("catalog.json")
CATALOG: dict[str, dict[str, str]] = json.loads(_FILE.read_text(encoding="utf-8"))["codes"]
UNKNOWN = {
    "what": "Unknown problem",
    "why": "Unknown cause",
    "fix": "Press Export diagnostics and send the file.",
}
CHATTER_S = 10.0
FLOOD_N, FLOOD_S = 10, 600.0
STALE_S = 24 * 3600.0
KEEP_RESOLVED = 500


@dataclass
class Problem:
    key: str
    code: str
    level: str
    device: str
    what: str
    why: str
    fix: str
    active: bool
    count: int
    first_utc: str
    last_utc: str
    cleared_utc: str | None
    stale: bool
    run_id: str
    started: float  # monotonic time it last became active (not sent)
    cleared_at: float | None  # monotonic (not sent)
    last_at: float = 0.0  # monotonic time of the last event (order; not sent)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        del d["started"], d["cleared_at"], d["last_at"]
        return d


class ProblemBoard:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: dict[str, Problem] = {}
        self._new: deque[float] = deque()  # times new alarms were raised (flood)
        self._runs: dict[str, dict[str, dict[str, Any]]] = {}  # run -> key -> report row
        self._run_raises: dict[str, list[float]] = {}  # run -> times of new alarms

    def __call__(self, e: Event) -> None:
        """Event bus subscriber. Never raises (SYS-008 would loop)."""
        clears = (e.data or {}).get("clears")
        with self._lock:
            if isinstance(clears, list):
                for code in clears:
                    self._clear(f"{code}|{e.device}")
            if e.level in ("warn", "error") and e.code != "ZL-INFO":
                self._raise(e)

    def resolve_all(self, why: str) -> None:
        """Test ended (IDLE) or Reset All: every active problem is resolved."""
        with self._lock:
            for key in [k for k, r in self._rows.items() if r.active]:
                self._clear(key)

    def new_in(self, seconds: float) -> int:
        with self._lock:
            return self._count_new(seconds)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            rows = list(self._rows.values())
            for r in rows:
                r.stale = r.active and now - r.started > STALE_S
            active = sorted((r for r in rows if r.active), key=lambda r: r.last_at, reverse=True)
            done = sorted(
                (r for r in rows if not r.active), key=lambda r: r.cleared_at or 0.0, reverse=True
            )
            n = self._count_new(FLOOD_S)
            return {
                "counts": {
                    "error": sum(r.level == "error" for r in active),
                    "warn": sum(r.level == "warn" for r in active),
                },
                "flood": {"new_in_10_min": n} if n > FLOOD_N else None,
                "problems": [r.to_dict() for r in active + done],
            }

    # ---------------------------------------------------------------- inside the lock

    def _raise(self, e: Event) -> None:
        now = self._clock()
        key = f"{e.code}|{e.device}"
        text = CATALOG.get(e.code, UNKNOWN)
        data = e.data or {}
        rid = str(data.get("run_id") or "")
        r = self._rows.get(key)
        new_alarm = r is None or (
            not r.active and not (r.cleared_at is not None and now - r.cleared_at <= CHATTER_S)
        )
        if r is None:
            r = self._rows[key] = Problem(
                key, e.code, e.level, e.device, "", "", "", True, 0,
                e.ts_utc, e.ts_utc, None, False, str(data.get("run_id", "")), now, None,
            )  # fmt: skip
            self._new.append(now)
        elif not r.active:
            again_soon = r.cleared_at is not None and now - r.cleared_at <= CHATTER_S
            if not again_soon:
                self._new.append(now)
                r.started = now
            r.active, r.cleared_utc, r.cleared_at = True, None, None
        r.count += 1
        r.last_utc, r.last_at = e.ts_utc, now
        if e.level == "error":
            r.level = "error"  # a warning that turned into an error stays an error
        r.what = e.text or text["what"]
        r.why = str(data.get("why") or text["why"])
        r.fix = str(data.get("fix") or text["fix"])
        if rid:
            r.run_id = rid
            self._note_run(rid, r, e, now, new_alarm)
        self._trim()

    def _note_run(self, rid: str, r: Problem, e: Event, now: float, new_alarm: bool) -> None:
        rows = self._runs.setdefault(rid, {})
        row = rows.setdefault(
            r.key,
            {"code": r.code, "level": r.level, "device": r.device, "what": r.what, "count": 0,
             "first_utc": e.ts_utc, "last_utc": e.ts_utc},
        )  # fmt: skip
        row["count"] += 1
        row["last_utc"], row["what"] = e.ts_utc, r.what
        if r.level == "error":
            row["level"] = "error"
        if new_alarm:
            raises = self._run_raises.setdefault(rid, [])
            if len(raises) < 100_000:  # bounded even for an endless flood
                raises.append(now)
        while len(self._runs) > 20:  # only recent runs are kept for reports
            old = next(iter(self._runs))
            self._runs.pop(old)
            self._run_raises.pop(old, None)

    def run_report(self, run_id: str, duration_s: float) -> dict[str, Any]:
        """02 10 ``problems`` and ``alarm_stats`` for one run (F21 report table)."""
        with self._lock:
            rows = []
            for key, row in (self._runs.get(run_id) or {}).items():
                cur = self._rows.get(key)
                open_now = bool(cur and cur.active and cur.run_id == run_id)
                cleared = None if open_now or cur is None else cur.cleared_utc
                rows.append({**row, "cleared_utc": cleared, "open_at_end": open_now})
            rows.sort(key=lambda x: (not x["open_at_end"], x["first_utc"]), reverse=False)
            rows.sort(key=lambda x: x["open_at_end"], reverse=True)
            times = sorted(self._run_raises.get(run_id) or [])
            end = self._clock()
            return {"problems": rows, "alarm_stats": _stats(rows, times, end, duration_s)}

    def _clear(self, key: str) -> None:
        r = self._rows.get(key)
        if r is not None and r.active:
            r.active, r.cleared_utc, r.cleared_at = False, utc_now(), self._clock()
            self._trim()

    def _count_new(self, seconds: float) -> int:
        now = self._clock()
        while self._new and now - self._new[0] > FLOOD_S:
            self._new.popleft()
        return sum(1 for t in self._new if now - t <= seconds)

    def _trim(self) -> None:
        done = [r for r in self._rows.values() if not r.active]
        if len(done) > KEEP_RESOLVED:
            done.sort(key=lambda r: r.cleared_at or 0.0)
            for r in done[: len(done) - KEEP_RESOLVED]:
                del self._rows[r.key]


def _stats(
    rows: list[dict[str, Any]], times: list[float], end: float, duration_s: float
) -> dict[str, Any]:
    """Alarm counts (F21, ISA-18.2): not part of any verdict."""
    windows = max(duration_s / FLOOD_S, 1.0)
    most, j = 0, 0
    for i, t in enumerate(times):  # largest number of new alarms in any 10 min
        while t - times[j] > FLOOD_S:
            j += 1
        most = max(most, i - j + 1)
    flood_s = 0.0
    start = end - duration_s
    lo = hi = 0
    for sec in range(int(duration_s)):  # seconds spent in a flood; both pointers only move on
        now = start + sec
        while hi < len(times) and times[hi] <= now:
            hi += 1
        while lo < hi and now - times[lo] > FLOOD_S:
            lo += 1
        if hi - lo > FLOOD_N:
            flood_s += 1
    top = sorted(rows, key=lambda x: x["count"], reverse=True)[:10]
    return {
        "total": len(times),
        "per_10_min_avg": round(len(times) / windows, 3),
        "per_10_min_max": most,
        "flood_time_ratio": round(flood_s / duration_s, 4) if duration_s > 0 else 0.0,
        "top": [{"code": x["code"], "device": x["device"], "count": x["count"]} for x in top],
    }
