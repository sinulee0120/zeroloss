"""Screen jobs a node wants from Agent Desktop (docs/03-features.md F20 구현 규칙, 02 2-12).

A node service has no screen (D38), so a job that needs one (the PC player) is put on
a list here; Agent Desktop, in the logged-in user's session, reads the whole list from
127.0.0.1:10994 every second, starts what is listed, stops what is not, and reports
each job back. A list, not messages: a missed poll or a restarted Desktop ends in the
same place. Bound both ways: a Desktop that hears nothing from the node for
``LINK_S`` stops its jobs; here a job without a report for ``LINK_S`` is taken as not
running. One Desktop per PC.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.events.bus import Event, Level
from core.protocol.envelope import ZlError

LINK_S = 3.0  # 03 F20: no poll / no report for this long = not connected / not running
STATES = ("IDLE", "STARTING", "RUNNING", "STOPPING", "FAULT")
MAX_SECONDS = 10  # seconds kept per job until taken (a late taker gets the newest)


@dataclass
class JobState:
    """What the Desktop last said about one job."""

    state: str = "IDLE"
    first_frame: bool = False
    lowered: bool = False
    at: float = 0.0  # monotonic time of the last report
    seconds: list[dict[str, Any]] = field(default_factory=list)  # not yet taken
    last_seq: int = -1


class DesktopJobs:
    def __init__(self, publish: Callable[[Event], None],
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self._publish = publish
        self._clock = clock
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._state: dict[str, JobState] = {}
        self._desktop: str | None = None
        self._seen_at = -1e9

    # ---------------------------------------------------------------- node side

    def want(self, job_id: str, module: str, kind: str, config: dict[str, Any]) -> None:
        with self._lock:
            self._jobs[job_id] = {"id": job_id, "module": module, "kind": kind,
                                  "config": dict(config)}  # fmt: skip
            self._state[job_id] = JobState(at=self._clock())

    def change(self, job_id: str, **config: Any) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["config"].update(config)

    def drop(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def forget(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            self._state.pop(job_id, None)

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._clock() - self._seen_at < LINK_S

    def wait_connected(self, limit_s: float) -> bool:
        end = time.monotonic() + limit_s  # waiting is real time; staleness is ``clock``
        with self._changed:
            while self._clock() - self._seen_at >= LINK_S:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._changed.wait(min(left, 0.2))
            return True

    def state(self, job_id: str) -> JobState | None:
        """The last report, or None; a report older than LINK_S reads as not running."""
        with self._lock:
            s = self._state.get(job_id)
            if s is None:
                return None
            fresh = self._clock() - s.at < LINK_S
            return JobState(s.state if fresh else "IDLE", s.first_frame and fresh, s.lowered,
                            s.at, [], s.last_seq)  # fmt: skip

    def take_seconds(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            s = self._state.get(job_id)
            if s is None:
                return []
            out, s.seconds = s.seconds, []
            return out

    def wait_gone(self, job_id: str, limit_s: float) -> bool:
        """After ``drop``: the Desktop said the job is no longer there, or it has been
        away for LINK_S (it stops its jobs itself then). False when neither in time."""
        end = time.monotonic() + limit_s
        with self._changed:
            while True:
                s = self._state.get(job_id)
                if s is None or s.state == "IDLE" or self._clock() - self._seen_at >= LINK_S:
                    self._state.pop(job_id, None)
                    return True
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._changed.wait(min(left, 0.2))

    # ---------------------------------------------------------------- Desktop side (02 2-12)

    def jobs(self, desktop_id: str) -> list[dict[str, Any]]:
        with self._changed:
            self._claim(desktop_id)
            self._changed.notify_all()
            return [{**j, "config": dict(j["config"])} for j in self._jobs.values()]

    def report(self, desktop_id: str, reports: list[dict[str, Any]]) -> list[str]:
        accepted, events = [], []
        with self._changed:
            self._claim(desktop_id)
            now = self._clock()
            listed = set(self._jobs)
            for r in reports:
                jid = str(r.get("id", ""))
                s = self._state.get(jid)
                if s is None:
                    continue  # not ours (an old run, G-13)
                state = str(r.get("state", "IDLE"))
                s.state = state if state in STATES else "FAULT"
                s.at = now
                if jid not in listed:
                    accepted.append(jid)  # stopping: only the state matters now
                    continue
                s.first_frame = bool(r.get("first_frame"))
                s.lowered = bool(r.get("lowered"))
                second, seq = r.get("second"), r.get("seq")
                if isinstance(second, dict) and isinstance(seq, int) and seq > s.last_seq:
                    s.last_seq = seq
                    s.seconds = [*s.seconds, second][-MAX_SECONDS:]
                for e in r.get("events") or []:
                    if isinstance(e, dict) and e.get("code"):
                        events.append((jid, e))
                accepted.append(jid)
            self._changed.notify_all()
        for jid, e in events:  # outside the lock: one event flow for everything (12 4-1)
            lv: Level = "error" if e.get("level") == "error" else "info" \
                if e.get("level") == "info" else "warn"  # fmt: skip
            data: dict[str, Any] = {"job": jid, "from": "desktop"}
            if isinstance(e.get("clears"), list):
                data["clears"] = [str(c) for c in e["clears"]]  # 02 8
            self._publish(Event(str(e["code"]), lv, str(e.get("text", "")), data=data))
        return accepted

    def _claim(self, desktop_id: str) -> None:
        """One Desktop per PC: another one is refused while the first is still seen."""
        now = self._clock()
        if not desktop_id:
            raise ZlError("DSC-012", "Request rejected", detail="desktop_id is required")
        if self._desktop not in (None, desktop_id) and now - self._seen_at < LINK_S:
            raise ZlError(
                "DSC-012", "Another Agent Desktop is already connected",
                "One Agent Desktop per PC (03 F20).", "Close the other one.",
                detail=f"connected: {self._desktop}",
            )  # fmt: skip
        self._desktop, self._seen_at = desktop_id, now
