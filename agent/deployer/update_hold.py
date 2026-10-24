"""No automatic updates or reboots while a test runs (docs/09-exceptions.md SYS-005 Linux
구현, 06 4.3, 20 GAP-66).

The Deployer is root and already sees the node's state every second: while the node
is in a test it keeps one apt setting file that turns off unattended upgrades and
their automatic reboot, and removes it once the node has been idle for ``IDLE_S``.
A file left by a power cut goes at the next Deployer start. Nothing else in the
system is touched; without apt (not Debian) there is nothing to do.

Snaps too (2026-10-16, user "다 승인"): snapd refreshed a snap on the T460 46 s before
a Wi-Fi hold-and-release in a night run. While a test runs ``snap refresh --hold=SNAP_HOLD``
(bounded: a Deployer that dies cannot hold them for ever), ``--unhold`` after, only when
this hold set it; a marker file says so across a Deployer restart. A hold someone else
set is left alone.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

APT_DIR = Path("/etc/apt/apt.conf.d")
FILE = "99zeroloss-test-hold"
IDLE_S = 5.0
TEXT = """// ZeroLoss: a test is running on this PC (docs/09-exceptions.md SYS-005).
// The ZeroLoss Deployer removes this file when the test ends.
Unattended-Upgrade::Automatic-Reboot "false";
APT::Periodic::Unattended-Upgrade "0";
"""
TEST_RUNNING = ("STARTING", "RUNNING", "STOPPING", "FAULT")
SNAP_HOLD = "210h"  # longer than the longest test (200 h), then snapd goes on by itself
SNAP_MARKER = "snap-held"

Run = Callable[[list[str]], tuple[int, str]]


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


class UpdateHold:
    def __init__(self, apt_dir: Path | None = APT_DIR, state_dir: Path | None = None,
                 run: Run = _run, snap: bool | None = None) -> None:  # fmt: skip
        self.path = apt_dir / FILE if apt_dir is not None and apt_dir.is_dir() else None
        self._idle_since: float | None = None
        self.error: str | None = None
        self._run = run
        has_snap = shutil.which("snap") is not None if snap is None else snap
        self.marker = state_dir / SNAP_MARKER if state_dir is not None and has_snap else None

    @property
    def held(self) -> bool:
        return self.path is not None and self.path.exists()

    def clear_leftover(self) -> None:
        """Deployer start: a file from before (power cut) goes; a running test puts it
        back within a second."""
        self._write(False)

    def seen(self, state: str | None, now: float | None = None) -> None:
        """The node's state from the watch (None = not answering: keep as it is)."""
        now = time.monotonic() if now is None else now
        if state is None:
            return
        if state in TEST_RUNNING:
            self._idle_since = None
            if not self.held:
                self._write(True)
            return
        if self._idle_since is None:
            self._idle_since = now
        if self.held and now - self._idle_since >= IDLE_S:
            self._write(False)

    def _write(self, on: bool) -> None:
        self._apt(on)
        self._snap(on)  # after: its error is not wiped by the apt file's success

    def _apt(self, on: bool) -> None:
        if self.path is None:
            return
        try:
            if on:
                tmp = self.path.with_name(FILE + ".tmp")
                tmp.write_text(TEXT, encoding="utf-8")
                tmp.replace(self.path)
            else:
                self.path.unlink(missing_ok=True)
            self.error = None
        except OSError as e:
            self.error = f"{self.path}: {e}"

    def _snap(self, on: bool) -> None:
        m = self.marker
        if m is None or on == m.exists():
            return
        if on:
            rc, out = self._run(["snap", "get", "system", "refresh.hold"])
            if rc == 0 and out.strip():
                return  # someone's own hold: theirs to keep and to lift
            rc, out = self._run(["snap", "refresh", f"--hold={SNAP_HOLD}"])
            if rc == 0:
                m.parent.mkdir(parents=True, exist_ok=True)
                m.write_text(SNAP_HOLD + "\n", encoding="utf-8")
            else:
                self.error = f"snap refresh --hold: {out}"
            return
        rc, out = self._run(["snap", "refresh", "--unhold"])
        if rc == 0:
            m.unlink(missing_ok=True)
        else:
            self.error = f"snap refresh --unhold: {out}"
