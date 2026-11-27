"""Windows Update paused while a test runs (docs/09-exceptions.md SYS-005, 06 4.2).

2026-11-28 (user "안만든거 다 만들어"; 04-phases: "남은 것: Windows 업데이트 일시 중지"): on
Ubuntu the Deployer holds apt and snaps for the test (update_hold.py); a Windows sender or
receiver could install updates and restart in the middle of a night run. The Deployer
runs as LocalSystem and does what Settings > Windows Update > "Pause updates" does: the
pause times under ``HKLM\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings``, bounded
(``PAUSE_H``, as SNAP_HOLD: a Deployer that dies cannot pause them for ever, and Windows
lets a pause be at most 35 days), taken off after the test. Only a pause this hold made is
taken off (a marker file says so across a Deployer restart); a pause the person set is
left alone.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

KEY = r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
PAUSE_H = 210  # longer than the longest test (200 h); Windows' own limit is 35 days
MARKER = "windows-update-paused"
START = ("PauseUpdatesStartTime", "PauseFeatureUpdatesStartTime",
         "PauseQualityUpdatesStartTime")  # fmt: skip
END = ("PauseUpdatesExpiryTime", "PauseFeatureUpdatesEndTime", "PauseQualityUpdatesEndTime")


class Registry:
    """The few calls on HKLM ``KEY`` this needs (tests bring a dict)."""

    def get(self, name: str) -> str | None:
        if sys.platform != "win32":
            return None
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, KEY) as k:
                value, _kind = winreg.QueryValueEx(k, name)
        except OSError:
            return None
        return str(value) if value else None

    def set(self, values: dict[str, str]) -> None:
        if sys.platform != "win32":
            raise OSError("not Windows")
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, KEY, 0, winreg.KEY_SET_VALUE) as k:
            for name, value in values.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def delete(self, names: tuple[str, ...]) -> None:
        if sys.platform != "win32":
            raise OSError("not Windows")
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, KEY, 0, winreg.KEY_SET_VALUE) as k:
            for name in names:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(k, name)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


class WindowsUpdatePause:
    def __init__(self, state_dir: Path, reg: Any = None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:  # fmt: skip
        self.marker = Path(state_dir) / MARKER
        self.reg = reg if reg is not None else Registry()
        self.now = now

    @property
    def paused(self) -> bool:
        """Paused by this hold (a pause the person set is not ours)."""
        return self.marker.exists()

    def set(self, on: bool) -> str | None:
        """Pause for the test (on) or take this hold's pause off; an error text or None."""
        try:
            if on:
                return self._pause()
            return self._resume()
        except OSError as e:
            return f"Windows Update pause: {e}"

    def _pause(self) -> str | None:
        if self.paused:
            return None
        now = self.now()
        theirs = _when(self.reg.get(END[0]))
        if theirs is not None and theirs > now:
            return None  # the person paused it: theirs to keep and to lift
        end = now + timedelta(hours=PAUSE_H)
        self.reg.set({**{n: _iso(now) for n in START}, **{n: _iso(end) for n in END}})
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(_iso(end) + "\n", encoding="utf-8")
        return None

    def _resume(self) -> str | None:
        if not self.paused:
            return None
        mine = self.marker.read_text(encoding="utf-8").strip()
        if self.reg.get(END[0]) == mine:  # still the pause this hold set
            self.reg.delete(START + END)
        self.marker.unlink(missing_ok=True)
        return None


def for_this_pc(state_dir: Path | None) -> WindowsUpdatePause | None:
    if sys.platform != "win32" or state_dir is None:
        return None
    return WindowsUpdatePause(state_dir)
