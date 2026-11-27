"""Windows Update paused for a test (SYS-005 on Windows, 2026-11-28 "안만든거 다 만들어")."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.deployer.update_hold import UpdateHold
from agent.deployer.win_update_pause import END, START, WindowsUpdatePause

NOW = datetime(2026, 11, 28, 1, 0, 0, tzinfo=UTC)


class Reg:
    """HKLM ...\\WindowsUpdate\\UX\\Settings as a dict."""

    def __init__(self, **values: str) -> None:
        self.values = dict(values)

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, values: dict[str, str]) -> None:
        self.values.update(values)

    def delete(self, names: tuple[str, ...]) -> None:
        for n in names:
            self.values.pop(n, None)


def hold(tmp_path: Path, reg: Reg) -> UpdateHold:
    pause = WindowsUpdatePause(tmp_path / "deployer", reg, now=lambda: NOW)
    return UpdateHold(None, None, snap=False, win_pause=pause)


def test_paused_for_the_test_and_back_after(tmp_path: Path) -> None:
    reg = Reg()
    h = hold(tmp_path, reg)
    h.seen("RUNNING", now=0.0)
    assert h.held and h.error is None
    assert reg.values[END[0]] == "2026-12-06T19:00:00Z"  # 210 h, bounded
    assert all(reg.values[n] == "2026-11-28T01:00:00Z" for n in START)
    h.seen("IDLE", now=1.0)
    assert h.held  # not at the first idle second
    h.seen("IDLE", now=7.0)
    assert not h.held and reg.values == {}


def test_a_pause_the_person_set_is_left_alone(tmp_path: Path) -> None:
    theirs = (NOW + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reg = Reg(PauseUpdatesExpiryTime=theirs)
    h = hold(tmp_path, reg)
    h.seen("RUNNING", now=0.0)
    h.seen("IDLE", now=0.0)
    h.seen("IDLE", now=10.0)
    assert reg.values == {"PauseUpdatesExpiryTime": theirs}


def test_a_pause_changed_meanwhile_is_not_taken_off(tmp_path: Path) -> None:
    """The person lengthened it in Settings during the test: theirs now."""
    reg = Reg()
    h = hold(tmp_path, reg)
    h.seen("RUNNING", now=0.0)
    reg.values[END[0]] = "2027-01-01T00:00:00Z"
    h.seen("IDLE", now=0.0)
    h.seen("IDLE", now=10.0)
    assert not h.held and reg.values[END[0]] == "2027-01-01T00:00:00Z"


def test_a_registry_that_refuses_is_said(tmp_path: Path) -> None:
    class NoRights(Reg):
        def set(self, values: dict[str, str]) -> None:
            raise PermissionError("Access is denied")

    h = hold(tmp_path, NoRights())
    h.seen("RUNNING", now=0.0)
    assert not h.held and h.error is not None and "Access is denied" in h.error
