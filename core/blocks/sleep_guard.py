"""Keep the PC awake during a test (docs/09-exceptions.md SYS-005 Linux 구현, 20 GAP-66).

One job: while the node is in a test, hold a systemd-logind "block" inhibitor for
sleep and idle (``systemd-inhibit`` running ``sleep infinity``, bound to the node by
process_runner), and let it go when the test is over. ``probe`` tries once whether
this account may take one (polkit rule of the installed node, 06 4.3) so the F17
step 4 can warn before a test instead of during it. Windows (the Controller PC,
2026-10-04 GAP-116: it slept during a 100 h test and every PC safe-stopped): a thread
that holds ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED |
ES_DISPLAY_REQUIRED)``. The screen stays on too (2026-10-09 GAP-128): on a Modern
Standby laptop ("S0 low power idle", like this Controller PC) the screen turning off
after its idle time is what takes the PC into standby. A closed lid or the power
button is the person's own choice and is not overridden.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.blocks.process_runner import ProcessRunner, ProcState, StartError, StopError

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001  # Windows SetThreadExecutionState
ES_DISPLAY_REQUIRED = 0x00000002
NAME = "sleep-guard"
WHAT = "sleep:idle"
PROBE_TIMEOUT_S = 5.0
# 06 4.3 (GAP-107): the node package masks these; all masked = this PC cannot sleep
SLEEP_TARGETS = ("sleep.target", "suspend.target", "hibernate.target", "hybrid-sleep.target")


def sleep_masked(unit: str) -> bool:
    try:
        done = subprocess.run(["systemctl", "is-enabled", unit], capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_S, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return done.stdout.strip() == "masked"


def inhibit_argv(binary: str, command: list[str]) -> list[str]:
    return [binary, f"--what={WHAT}", "--mode=block", "--who=ZeroLoss",
            "--why=A ZeroLoss test is running", *command]  # fmt: skip


class SleepGuard:
    def __init__(
        self, procs_dir: Path, binary: str | None = None,
        masked: Callable[[str], bool] = sleep_masked,
    ) -> None:  # fmt: skip
        self.procs_dir = procs_dir
        self.binary = binary or shutil.which("systemd-inhibit") or ""
        self.masked = masked
        self.state = "unknown"  # ok / off / unavailable / unknown (02 5 power.sleep_guard)
        self.why: str | None = None
        self._runner: ProcessRunner | None = None
        self._lock = threading.Lock()
        self._win_hold: threading.Event | None = None  # set = let go (Windows)

    def probe(self) -> None:
        """Try once (a few seconds, off the request path). When the inhibitor cannot be
        had but sleep is off on this PC anyway (06 4.3 masks), there is nothing to hold."""
        if sys.platform == "win32":
            self.state, self.why = "ok", None
            return
        if not sys.platform.startswith("linux"):
            self.state, self.why = "unavailable", "keeping this system awake is not built here"
            return
        self.state, self.why = self._try_inhibit()
        if self.state == "unavailable" and all(self.masked(t) for t in SLEEP_TARGETS):
            self.state, self.why = "off", "sleep is turned off on this PC"

    def _try_inhibit(self) -> tuple[str, str | None]:
        if not self.binary:
            return "unavailable", "systemd-inhibit is not installed"
        try:
            done = subprocess.run(inhibit_argv(self.binary, ["true"]), capture_output=True,
                                  text=True, timeout=PROBE_TIMEOUT_S, check=False)  # fmt: skip
        except (OSError, subprocess.SubprocessError) as e:
            return "unavailable", f"systemd-inhibit failed: {e}"
        if done.returncode == 0:
            return "ok", None
        text = (done.stderr or done.stdout).strip().splitlines()
        return "unavailable", (text[-1] if text else "refused")[:200]

    @property
    def held(self) -> bool:
        if self._win_hold is not None:
            return not self._win_hold.is_set()
        r = self._runner
        return r is not None and r.poll() is ProcState.RUNNING

    def hold(self) -> str | None:
        """Start holding; returns why when it could not (the test goes on, SYS-005)."""
        with self._lock:
            if self.held or self.state == "off":  # off: this PC cannot sleep (GAP-107)
                return None
            if sys.platform == "win32":
                return self._hold_windows()
            if not self.binary:
                return self.why or "systemd-inhibit is not installed"
            self._release_locked()
            runner = ProcessRunner(NAME, inhibit_argv(self.binary, ["sleep", "infinity"]),
                                   self.procs_dir, stop_timeout_s=5.0)  # fmt: skip
            since = time.monotonic()
            try:  # alive after a moment: a refused inhibitor exits at once
                runner.start(ready=lambda: time.monotonic() - since > 0.5, ready_timeout_s=2.0)
            except StartError as e:
                return str(e)
            self._runner = runner
        return None

    def release(self) -> None:
        with self._lock:
            self._release_locked()

    def _hold_windows(self) -> str | None:
        """The flag lives with the thread that set it: one thread holds it until let go."""
        import ctypes

        let_go, ready = threading.Event(), threading.Event()
        why: list[str] = []

        def keep() -> None:
            kernel = getattr(ctypes, "windll").kernel32  # noqa: B009 (Windows only)
            if not kernel.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            ):
                why.append("SetThreadExecutionState refused")
                ready.set()
                return
            ready.set()
            let_go.wait()
            kernel.SetThreadExecutionState(ES_CONTINUOUS)

        threading.Thread(target=keep, name=NAME, daemon=True).start()
        ready.wait(2.0)
        if why:
            return why[0]
        self._win_hold = let_go
        return None

    def _release_locked(self) -> None:
        if self._win_hold is not None:
            self._win_hold.set()
            self._win_hold = None
        r, self._runner = self._runner, None
        if r is not None and r.state in (ProcState.RUNNING, ProcState.FAULT):
            with contextlib.suppress(StopError):  # the leftover cleanup ends it (SYS-006)
                r.stop()

    def status(self) -> dict[str, Any]:
        return {"sleep_guard": self.state, "why": self.why, "held": self.held}
