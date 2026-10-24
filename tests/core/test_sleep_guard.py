"""No sleep and no automatic updates during a test (docs/09-exceptions.md SYS-005 Linux
구현, 02 5 ``power``, 02 15 ``updates_held``, 20 GAP-66).

A stand-in ``systemd-inhibit`` (a shell script that runs its command) keeps the tests
off the real login manager; the real one is only asked once, and either answer is
fine there. The Deployer's apt file goes to a test folder, never /etc.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent.deployer.update_hold import FILE, IDLE_S, TEXT, UpdateHold
from core.blocks.sleep_guard import SleepGuard
from core.lifecycle.state_flow import State
from core.node import Node


def fake_inhibit(folder: Path, refuse: bool = False) -> str:
    """Runs the command after the options, like systemd-inhibit; or refuses."""
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "systemd-inhibit"
    body = ('echo "Failed to inhibit: Access denied" >&2; exit 1' if refuse else
            'while [ "${1#--}" != "$1" ]; do shift; done; exec "$@"')  # fmt: skip
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def wait(cond: Any, limit_s: float = 10.0) -> bool:
    end = time.monotonic() + limit_s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_probe_hold_release(tmp_path: Path) -> None:
    g = SleepGuard(tmp_path / "procs", fake_inhibit(tmp_path / "bin"))
    assert g.status() == {"sleep_guard": "unknown", "why": None, "held": False}
    g.probe()
    assert g.state == "ok"
    assert g.hold() is None and g.held
    assert (tmp_path / "procs" / "sleep-guard.json").exists()  # bound to the node (12 4-3)
    assert g.hold() is None  # twice = once
    g.release()
    assert not g.held and not (tmp_path / "procs" / "sleep-guard.json").exists()
    g.release()  # twice is fine


def test_refused(tmp_path: Path) -> None:
    g = SleepGuard(tmp_path / "procs", fake_inhibit(tmp_path / "bin", refuse=True),
                   masked=lambda unit: False)  # fmt: skip
    g.probe()
    assert g.state == "unavailable" and g.why == "Failed to inhibit: Access denied"
    why = g.hold()
    assert why and not g.held  # the test goes on; the node warns (SYS-005)


def test_refused_but_sleep_is_off_is_no_warning(tmp_path: Path) -> None:
    """GAP-107 (real ProBook): the inhibitor is refused, but the PC cannot sleep (the
    package masked the targets; the user had also turned sleep off) → off, nothing to hold."""
    g = SleepGuard(tmp_path / "procs", fake_inhibit(tmp_path / "bin", refuse=True),
                   masked=lambda unit: True)  # fmt: skip
    g.probe()
    assert g.state == "off" and g.why == "sleep is turned off on this PC"
    assert g.hold() is None and not g.held  # no inhibitor process, no SYS-005
    half = SleepGuard(tmp_path / "p2", fake_inhibit(tmp_path / "b2", refuse=True),
                      masked=lambda unit: unit == "sleep.target")  # fmt: skip
    half.probe()
    assert half.state == "unavailable"  # one target can still sleep: warn as before


def test_sleep_off_also_without_systemd_inhibit(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    g = SleepGuard(tmp_path / "procs", "", masked=lambda unit: True)
    g.binary = ""
    g.probe()
    assert g.state == "off" and g.hold() is None


def test_missing_binary(tmp_path: Path) -> None:
    g = SleepGuard(tmp_path / "procs", "", masked=lambda unit: False)
    g.binary = ""
    g.probe()
    assert g.state == "unavailable" and "not installed" in str(g.why)
    assert g.hold() is not None


def test_the_real_one_answers_either_way(tmp_path: Path) -> None:
    g = SleepGuard(tmp_path / "procs")
    if not g.binary:
        pytest.skip("no systemd-inhibit here")
    g.probe()
    assert g.state in ("ok", "unavailable")


def test_node_holds_only_during_a_test(tmp_path: Path) -> None:
    node = Node(tmp_path / "node", "agent", host="127.0.0.1", port=0, local_port=0)
    node.sleep_guard.binary = fake_inhibit(tmp_path / "bin")
    node.start()
    try:
        assert wait(lambda: node.sleep_guard.state == "ok")
        assert node.sleep_guard.status()["held"] is False
        node.flow.move(State.STARTING)
        assert wait(lambda: node.sleep_guard.held)
        node.flow.move(State.RUNNING)
        assert node._test_records() == []  # its own helper, not a test program
        node.flow.move(State.STOPPING)
        assert node.sleep_guard.held  # awake until the stop is done
        node.flow.move(State.IDLE)
        assert wait(lambda: not node.sleep_guard.held)
    finally:
        node.stop()
    assert not (tmp_path / "node" / "procs" / "sleep-guard.json").exists()


def test_node_warns_when_it_cannot(tmp_path: Path) -> None:
    node = Node(tmp_path / "node", "agent", host="127.0.0.1", port=0, local_port=0)
    node.sleep_guard.binary = fake_inhibit(tmp_path / "bin", refuse=True)
    node.sleep_guard.masked = lambda unit: False  # this PC can sleep (not the test PC's state)
    got: list[Any] = []
    node.bus.subscribe(got.append)
    node.start()
    try:
        assert wait(lambda: node.sleep_guard.state == "unavailable")
        node.flow.move(State.STARTING)
        assert wait(lambda: any(e.code == "ZL-SYS-005" for e in got))
    finally:
        node.flow.move(State.STOPPING)
        node.flow.move(State.IDLE)
        node.stop()


# ---------------------------------------------------------------- Deployer: apt hold


def test_update_hold(tmp_path: Path) -> None:
    h = UpdateHold(tmp_path)
    h.seen("IDLE", 0.0)
    assert not h.held
    h.seen("RUNNING", 1.0)
    assert h.held and (tmp_path / FILE).read_text() == TEXT
    h.seen(None, 2.0)  # the node does not answer: keep it
    h.seen("IDLE", 3.0)
    assert h.held  # idle only just now
    h.seen("IDLE", 3.0 + IDLE_S)
    assert not h.held
    h.seen("RUNNING", 20.0)
    h.clear_leftover()  # a Deployer start after a power cut
    assert not h.held
    assert UpdateHold(tmp_path / "no-apt").path is None  # not Debian: nothing to do


def test_snaps_are_held_during_a_test_and_only_ours_lifted(tmp_path: Path) -> None:
    """2026-10-16 (user "다 승인"): snapd refreshed a snap on the T460 during a night run."""
    calls: list[list[str]] = []
    theirs = {"hold": ""}

    def snap(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, theirs["hold"] if argv[1] == "get" else ""

    h = UpdateHold(tmp_path, tmp_path / "deployer", snap, snap=True)
    h.seen("RUNNING", 0.0)
    assert ["snap", "refresh", "--hold=210h"] in calls and (
        tmp_path / "deployer" / "snap-held"
    ).exists()
    h.seen("RUNNING", 1.0)
    assert calls.count(["snap", "refresh", "--hold=210h"]) == 1  # once per test
    h.seen("IDLE", 2.0)
    h.seen("IDLE", 2.0 + IDLE_S)
    assert (
        calls[-1] == ["snap", "refresh", "--unhold"]
        and not (tmp_path / "deployer" / "snap-held").exists()
    )
    calls.clear()
    theirs["hold"] = "forever"  # the person's own hold
    h.seen("RUNNING", 20.0)
    h.seen("IDLE", 21.0)
    h.seen("IDLE", 21.0 + IDLE_S)
    assert calls == [["snap", "get", "system", "refresh.hold"]]  # looked, never touched


def test_update_hold_monkey(tmp_path: Path) -> None:
    """Random node states and gaps: the file is there whenever a test runs, and gone
    after IDLE_S of idle."""
    rnd = random.Random(66)
    h = UpdateHold(tmp_path)
    now, idle_since = 0.0, None
    for _ in range(500):
        now += rnd.choice([0.5, 1.0, 1.0, 3.0, 6.0])
        state = rnd.choice(["IDLE", "IDLE", "RUNNING", "STARTING", "STOPPING", "FAULT", None])
        h.seen(state, now)
        if state is None:
            continue
        if state != "IDLE":
            idle_since = None
            assert h.held
        else:
            idle_since = now if idle_since is None else idle_since
            if now - idle_since >= IDLE_S:
                assert not h.held


def test_deployer_holds_updates_while_the_node_tests(tmp_path: Path) -> None:
    from agent.deployer.agent_link import AgentLink
    from agent.deployer.modules import Layout
    from agent.deployer.server import Deployer
    from tests.agent.fake_node import FakeNode

    apt = tmp_path / "apt.conf.d"
    apt.mkdir()
    (apt / FILE).write_text("left by a power cut")
    node = FakeNode()
    layout = Layout(tmp_path / "data", tmp_path / "mods")
    layout.modules_dir.mkdir(parents=True)
    d = Deployer(layout, AgentLink(node, node.port, come_back_s=5), "127.0.0.1", 0,
                 build="20260925.1200", watch_s=5.0, poll_s=0.1, apt_dir=apt)  # fmt: skip
    d.update_hold._run = lambda argv: (0, "")  # never the real snap (it needs root)
    d.start()
    try:
        assert wait(lambda: not (apt / FILE).exists())  # leftover removed at start
        node.state = "RUNNING"
        assert wait(lambda: d.health()["updates_held"] is True)
        node.state = "IDLE"
        d.update_hold._idle_since = time.monotonic() - IDLE_S  # skip the 5 s wait
        assert wait(lambda: d.health()["updates_held"] is False)
        assert "ZL-SYS-005 automatic updates held" in (layout.deployer / "deployer.log").read_text()
    finally:
        d.stop()
        node.close()


def test_removal_takes_the_hold_file(tmp_path: Path) -> None:
    from agent.deployer.nodesetup import NodePaths, node_remove, node_setup
    from tests.agent.test_nodesetup import FakeSystem

    paths, fake = NodePaths(tmp_path), FakeSystem()
    node_setup(paths, fake)
    held = paths.at("/etc/apt/apt.conf.d") / FILE
    held.parent.mkdir(parents=True)
    held.write_text(TEXT)
    node_remove(paths, fake)
    assert not held.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows way (Controller PC, GAP-116)")
def test_windows_holds_the_system_awake_and_lets_go(tmp_path: Path) -> None:
    """GAP-116: the Controller PC slept during a 100 h test. While held, the system's
    execution state has ES_SYSTEM_REQUIRED; after release it is gone."""
    import ctypes

    def state() -> int:
        v = ctypes.c_ulong(0)
        getattr(ctypes, "windll").powrprof.CallNtPowerInformation(  # noqa: B009
            16, None, 0, ctypes.byref(v), ctypes.sizeof(v)
        )  # SystemExecutionState
        return int(v.value)

    before = state() & 0x3  # a real Controller running a test on this PC holds it too
    g = SleepGuard(tmp_path / "procs")
    g.probe()
    assert g.status()["sleep_guard"] == "ok"
    assert g.hold() is None and g.held
    time.sleep(0.2)
    assert state() & 0x3 == 0x3  # ES_SYSTEM_REQUIRED and ES_DISPLAY_REQUIRED (GAP-128)
    g.release()
    time.sleep(0.2)
    assert not g.held and state() & 0x3 == before
