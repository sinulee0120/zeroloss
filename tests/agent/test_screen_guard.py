"""No screen blanking while the player plays (docs/09-exceptions.md RCV-006, 03 F20)."""

from __future__ import annotations

import os
from typing import Any

import pytest

from agent.desktop.screen_guard import ScreenGuard
from core.desktop_jobs import DesktopJobs
from core.events.bus import Event


class FakeBus:
    ok = True

    def __init__(self) -> None:
        self.held: set[int] = set()
        self.next = 7
        self.locked = False

    def inhibit(self) -> int:
        self.next += 1
        self.held.add(self.next)
        return self.next

    def uninhibit(self, cookie: int) -> None:
        self.held.discard(cookie)

    def active(self) -> bool:
        return self.locked


def test_held_only_while_playing_and_lock_is_told_and_cleared() -> None:
    bus, playing, told = FakeBus(), {"on": False}, []
    g = ScreenGuard(bus, lambda: playing["on"], lambda *a: told.append(a))
    g.check()
    assert bus.held == set() and told == []
    playing["on"] = True
    g.check()
    g.check()
    assert len(bus.held) == 1  # once, not every check
    bus.locked = True
    g.check()
    g.check()
    assert [t[0] for t in told] == ["ZL-RCV-006"]  # told once
    bus.locked = False
    g.check()
    assert told[-1] == ("ZL-INFO", "info", "Screen is back", ["ZL-RCV-006"])
    playing["on"] = False
    g.check()
    assert bus.held == set()
    playing["on"] = True
    g.check()
    g.release()
    assert bus.held == set()


def test_no_screen_saver_no_calls() -> None:
    class Missing:
        ok = False

    g = ScreenGuard(Missing(), lambda: True, lambda *a: pytest.fail("told"))
    g.check()
    g.release()


def test_the_desktops_clear_reaches_the_node_bus() -> None:
    got: list[Event] = []
    jobs = DesktopJobs(got.append)
    jobs.want("r/x/ch1/player", "iptv", "player", {})
    jobs.jobs("d")
    jobs.report("d", [{"id": "r/x/ch1/player", "state": "RUNNING", "events": [
        {"code": "ZL-INFO", "level": "info", "text": "Screen is back", "clears": ["ZL-RCV-006"]}
    ]}])  # fmt: skip
    assert got[-1].data["clears"] == ["ZL-RCV-006"]


@pytest.mark.skipif(not os.environ.get("DBUS_SESSION_BUS_ADDRESS"), reason="no session bus")
def test_the_real_screen_saver_gives_a_cookie() -> None:
    """G-18: the D-Bus names used are the real ones on this desktop."""
    from agent.desktop.screen_guard import Bus

    bus: Any = Bus()
    if not bus.ok:
        pytest.skip("no org.freedesktop.ScreenSaver here")
    cookie = bus.inhibit()
    assert isinstance(cookie, int)
    bus.uninhibit(cookie)
    assert bus.active() in (True, False)


class Reply:
    def __init__(self, kind: str, args: list[Any]) -> None:
        self.kind, self.args = kind, args

    def type(self) -> str:
        return self.kind

    def arguments(self) -> list[Any]:
        return self.args


def test_an_error_reply_is_not_locked() -> None:
    """GNOME answers org.freedesktop.ScreenSaver.GetActive with NotSupported; its text was
    read as True, so a whole run was "locked" (2026-10-10 T460, UHD run)."""
    from agent.desktop.screen_guard import answer

    text = "This method is not part of the idle inhibition specification"
    assert answer(Reply("error", [text]), "error") is None
    assert answer(Reply("reply", [False]), "error") is False
    assert answer(Reply("reply", [True]), "error") is True
    assert answer(Reply("reply", []), "error") is None


def test_the_real_session_bus_does_not_say_locked() -> None:
    """On this desktop (when there is one) the screen is not locked while tests run."""
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        pytest.skip("no session bus")
    from agent.desktop.screen_guard import Bus

    bus = Bus()
    if not bus.ok:
        pytest.skip("no screen saver on this desktop")
    assert bus.active() in (False, None)
