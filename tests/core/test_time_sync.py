"""Automatic time sync held during a test and put back after (SYS-012, 2026-10-22).

Eight yellow cells of a 100 h run were the receiving PC's NTP slews read as delay variation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.blocks.time_sync import TimeSyncGuard


class Timedate:
    def __init__(self, ntp: bool = True, allowed: bool = True) -> None:
        self.ntp, self.allowed = ntp, allowed
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[1] == "show":
            return 0, "yes" if self.ntp else "no"
        if not self.allowed:
            return 1, "Access denied"
        self.ntp = argv[-1] == "true"
        return 0, ""


@pytest.fixture(autouse=True)
def timedatectl_here(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.blocks.time_sync.shutil.which", lambda name: "/usr/bin/" + name)


def guard(tmp_path: Path, t: Timedate) -> TimeSyncGuard:
    return TimeSyncGuard(tmp_path / "held.json", t, platform="linux")


def test_held_during_the_test_and_back_after(tmp_path: Path) -> None:
    t = Timedate()
    g = guard(tmp_path, t)
    assert g.hold() is None and t.ntp is False and g.status()["state"] == "held"
    assert g.release()["ok"] and t.ntp is True and not (tmp_path / "held.json").exists()


def test_off_before_stays_off(tmp_path: Path) -> None:
    t = Timedate(ntp=False)
    g = guard(tmp_path, t)
    assert g.hold() is None and g.release()["why"] == "nothing was changed" and t.ntp is False


def test_after_a_crash_it_goes_back_on(tmp_path: Path) -> None:
    t = Timedate()
    guard(tmp_path, t).hold()
    assert t.ntp is False
    assert guard(tmp_path, t).release()["ok"] and t.ntp is True  # a new node reads the file


def test_not_allowed_is_a_reason_not_a_failure(tmp_path: Path) -> None:
    t = Timedate(allowed=False)
    g = guard(tmp_path, t)
    why = g.hold()
    assert why and "Access denied" in why and t.ntp is True and g.status()["state"] == "not held"
