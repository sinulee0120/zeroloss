"""Background watches never die of one error (2026-11-21 review): the node's watchdog
(CTL-008: a test whose Controller went silent stops by itself; RCV-008 disk) and the
sleep / time-sync hold (SYS-005, SYS-012) went on silently dead after one exception."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from core.events.bus import Event
from core.node import Node


def _watching(safe_stop: Any) -> tuple[SimpleNamespace, list[Event], threading.Event]:
    said: list[Event] = []
    closing = threading.Event()
    calls = {"n": 0}

    def silence_stop(now: float) -> str | None:
        calls["n"] += 1
        return "no request from the Controller for 11 s"

    node = SimpleNamespace(
        _closing=closing, awake_until=0.0, disk_check_s=0.02, silence_stop=silence_stop,
        safe_stop=safe_stop, _check_disk=lambda: None,
        bus=SimpleNamespace(publish=said.append), calls=calls,
    )  # fmt: skip
    return node, said, closing


def test_the_node_watchdog_goes_on_after_an_error_and_still_stops_the_test() -> None:
    stopped: list[str] = []

    def safe_stop(why: str) -> None:
        if not stopped:
            stopped.append("failed once")
            raise RuntimeError("a module did not answer its stop")
        stopped.append(why)

    node, said, closing = _watching(safe_stop)
    t = threading.Thread(target=Node._watchdog, args=(node,), daemon=True)
    t.start()
    end = time.monotonic() + 5
    while len(stopped) < 2 and time.monotonic() < end:
        time.sleep(0.02)
    closing.set()
    t.join(3)
    assert stopped[:2] == ["failed once", "no request from the Controller for 11 s"]
    assert [e.code for e in said] == ["ZL-SYS-008"]  # said once


def test_the_desktop_link_goes_on_after_an_error(tmp_path: Any, monkeypatch: Any) -> None:
    """Agent Desktop's link: one bad round ended it for good (RCV-020 on the node, the
    players never stopped)."""
    from agent.desktop import link as link_mod

    monkeypatch.setattr(link_mod, "POLL_S", 0.01)
    link = link_mod.DesktopLink(1, {}, tmp_path)
    rounds = {"n": 0}

    def step() -> None:
        rounds["n"] += 1
        if rounds["n"] == 1:
            raise KeyError("jobs")
        if rounds["n"] >= 3:
            link._halt.set()

    told: list[str] = []
    link.step = step  # type: ignore[method-assign]
    link.tell = lambda code, level, text, clears=None: told.append(code)  # type: ignore[method-assign]
    link._loop()  # returns once halted
    assert rounds["n"] >= 3 and told == ["ZL-SYS-008"]


def test_the_sleep_hold_goes_on_after_an_error() -> None:
    said: list[Event] = []
    changed = threading.Event()
    held: list[str] = []
    state = {"n": 0}

    def hold() -> str | None:
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("systemd-inhibit was not found")
        held.append("held")
        return None

    node = SimpleNamespace(
        _awake_changed=changed, _closing=threading.Event(), _awake_wanted=True,
        sleep_guard=SimpleNamespace(probe=lambda: None, hold=hold, release=lambda: None),
        time_sync=SimpleNamespace(hold=lambda: None, release=lambda: {"ok": True}),
        bus=SimpleNamespace(publish=said.append),
    )  # fmt: skip
    node._awake_step = lambda: Node._awake_step(node)  # type: ignore[arg-type]
    node._time_sync_back = lambda: Node._time_sync_back(node)  # type: ignore[arg-type]
    t = threading.Thread(target=Node._awake_loop, args=(node,), daemon=True)
    t.start()
    changed.set()  # the first hold fails
    time.sleep(0.2)
    changed.set()  # the next works
    end = time.monotonic() + 3
    while not held and time.monotonic() < end:
        time.sleep(0.02)
    node._closing.set()
    changed.set()
    t.join(3)
    assert held == ["held"] and not t.is_alive()
    assert [e.code for e in said] == ["ZL-SYS-008"]
