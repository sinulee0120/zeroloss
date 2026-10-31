"""Hold this PC's automatic time sync while it is in a test, and put it back after.

2026-10-22 (R20261022-235424, 100 h over Wi-Fi): eight 5-minute cells were yellow for
"delay variation 50-125 ms" with no loss and 1-2 ms jitter. The one-way delay ramped
slowly at the receiving PC's NTP polls: systemd-timesyncd slews the clock (up to 500 ppm)
and arrival times follow it. A step (not a slew) would jump every delay value at once.
Users: "공유기 문제가 아닌데 공유기 문제로 돌리지 않게", "다른 pc들로 새 환경에서 테스트할때도
문제가 나오지 않도록".

One job, like the sleep guard: ``hold`` turns automatic time sync off when it is on and
writes that down on disk; ``release`` turns it back on only when this guard turned it off
(also after a crash: ``release`` at node start reads the file). Linux:
``timedatectl set-ntp`` (polkit org.freedesktop.timedate1.set-ntp for the node account,
06 4.3). Windows (the Controller PC): changing w32time needs an administrator, so only
its state is told. Holding for 100 h lets the clock drift a few seconds; results use the
Controller's counters, so that is harmless. Short OS calls with a time limit only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

TIMEOUT_S = 10.0
NTP_READ_S = 60.0  # how often the heartbeat's "ntp" is read again (a short OS call)

Run = Callable[[list[str]], tuple[int, str]]


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S,
                              check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return done.returncode, (done.stdout + done.stderr).strip()


class TimeSyncGuard:
    def __init__(self, custody: Path, run: Run = _run, platform: str = sys.platform) -> None:
        self.custody = custody  # {"turned_off": true} while this guard holds it off
        self.run = run
        self.platform = platform
        self._lock = threading.Lock()
        self._state = "unknown"  # held / on / off / not held
        self._why: str | None = None
        self._ntp: bool | None = None  # last read, for the heartbeat
        self._ntp_at = -NTP_READ_S

    def _ntp_on(self) -> bool | None:
        if self.platform == "win32" or shutil.which("timedatectl") is None:
            return None
        rc, out = self.run(["timedatectl", "show", "-p", "NTP", "--value"])
        return {"yes": True, "no": False}.get(out.strip()) if rc == 0 else None

    def hold(self) -> str | None:
        """Time sync off for the test; None = held (or nothing to hold), else why not."""
        with self._lock:
            if self.custody.exists():
                self._state, self._why = "held", None
                return None
            on = self._ntp_on()
            if on is None:
                self._state = "not held"
                self._why = ("Windows: time sync is not changed (needs an administrator)"
                             if self.platform == "win32" else "no timedatectl here")  # fmt: skip
                return self._why
            if not on:
                self._state, self._why = "off", None  # already off: nothing to put back
                return None
            rc, out = self.run(["timedatectl", "set-ntp", "false"])
            if rc != 0:
                self._state, self._why = "not held", f"timedatectl set-ntp false: {out[-200:]}"
                return self._why
            try:
                self.custody.write_text(json.dumps({"turned_off": True}), encoding="utf-8")
            except OSError as e:
                self.run(["timedatectl", "set-ntp", "true"])  # cannot remember it: undo
                self._state, self._why = "not held", f"cannot keep the record: {e}"
                return self._why
            self._state, self._why = "held", None
            return None

    def release(self) -> dict[str, Any]:
        """Time sync back on when this guard turned it off (now or before a crash)."""
        with self._lock:
            if not self.custody.exists():
                if self._state == "held":
                    self._state = "unknown"
                return {"ok": True, "why": "nothing was changed"}
            rc, out = self.run(["timedatectl", "set-ntp", "true"])
            if rc != 0:
                self._why = f"timedatectl set-ntp true: {out[-200:]}"
                return {"ok": False, "why": self._why}
            self.custody.unlink(missing_ok=True)
            self._state, self._why = "unknown", None
            return {"ok": True, "why": "time sync on again"}

    def turn_on(self) -> dict[str, Any]:
        """Automatic time sync on, between tests, from the screen's button (2026-10-31
        user: "명령어로 나한테 시키지 말고 앱에 반영할수있나?": the T460 had it off and ran
        10 s fast). Not remembered as this guard's: a later test holds and puts it back."""
        with self._lock:
            on = self._ntp_on()
            if on is None:
                return {"ok": False, "why": "Windows: needs an administrator" if
                        self.platform == "win32" else "no timedatectl here"}  # fmt: skip
            if on:
                return {"ok": True, "why": "time sync was on already"}
            rc, out = self.run(["timedatectl", "set-ntp", "true"])
            if rc != 0:
                return {"ok": False, "why": f"timedatectl set-ntp true: {out[-200:]}"}
            self._ntp, self._ntp_at = True, time.monotonic()
            return {"ok": True, "why": "time sync on; the clock is set within a minute"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            if time.monotonic() - self._ntp_at >= NTP_READ_S:
                self._ntp, self._ntp_at = self._ntp_on(), time.monotonic()
            return {"state": self._state, "why": self._why, "ntp": self._ntp}
