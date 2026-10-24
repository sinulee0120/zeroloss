"""Agent Desktop runs the PC player for the node service (docs/03-features.md F20 구현
규칙, 02 2-12, 09 RCV-010/019/020, 20 GAP-65).

Unit: the node's screen-job list and the stand-in player. Together: a real node with
``player_host="desktop"`` and a real ``DesktopLink`` with a fake player part, then the
real IPTV part (headless VLC) in a whole test run.
"""

from __future__ import annotations

import random
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.desktop.link import DesktopLink
from core.desktop_jobs import LINK_S, DesktopJobs
from core.events.bus import Event
from core.lifecycle.state_flow import State, StateFlow
from core.modules.registry import load_desktop_entries
from core.node import Node
from core.protocol.envelope import ZlError
from modules.iptv.receiver.player import PlayerConfig
from modules.iptv.receiver.remote_player import RemotePlayer

REPO = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def wait(cond: Any, limit_s: float = 10.0) -> bool:
    end = time.monotonic() + limit_s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- the node's job list


def test_jobs_list_report_and_one_desktop() -> None:
    clock, events = Clock(), list[Event]()
    jobs = DesktopJobs(events.append, clock)
    assert not jobs.connected and jobs.jobs("d1") == [] and jobs.connected
    jobs.want("r1/rx/ch1/player", "iptv", "player", {"url": "rtp://@239.1.1.1:5004"})
    (j,) = jobs.jobs("d1")
    assert (j["id"], j["module"], j["kind"]) == ("r1/rx/ch1/player", "iptv", "player")
    with pytest.raises(ZlError) as e:
        jobs.jobs("d2")  # one Desktop per PC
    assert e.value.code == "ZL-DSC-012"
    second = {"playback": "ok", "dropped": 0}
    rep = {"id": j["id"], "state": "RUNNING", "seq": 1, "second": second, "first_frame": True,
           "events": [{"code": "ZL-RCV-003", "level": "error", "text": "died"}]}  # fmt: skip
    assert jobs.report("d1", [rep, {**rep, "id": "old-run/x"}]) == [j["id"]]  # G-13
    assert jobs.report("d1", [rep]) == [j["id"]]  # the same seq twice counts once
    assert jobs.take_seconds(j["id"]) == [second] and jobs.take_seconds(j["id"]) == []
    assert [(x.code, x.data["from"]) for x in events] == [("ZL-RCV-003", "desktop")] * 2
    s = jobs.state(j["id"])
    assert s is not None and s.state == "RUNNING" and s.first_frame
    clock.t += LINK_S + 0.1  # no report: not running, and no longer connected
    s = jobs.state(j["id"])
    assert s is not None and s.state == "IDLE" and not s.first_frame and not jobs.connected
    assert jobs.jobs("d2") and jobs.connected  # the first one is gone: another may take it


def test_wait_gone_and_missing_desktop_id() -> None:
    clock = Clock()
    jobs = DesktopJobs(lambda e: None, clock)
    with pytest.raises(ZlError):
        jobs.jobs("")
    jobs.want("a", "iptv", "player", {})
    jobs.jobs("d1")
    jobs.report("d1", [{"id": "a", "state": "RUNNING"}])
    jobs.drop("a")
    assert jobs.jobs("d1") == []
    assert not jobs.wait_gone("a", 0.05)  # still RUNNING and the Desktop is there
    jobs.report("d1", [{"id": "a", "state": "IDLE"}])
    assert jobs.wait_gone("a", 0.05)


def remote(clock: Clock) -> tuple[RemotePlayer, DesktopJobs, list[tuple[str, str, str]]]:
    jobs = DesktopJobs(lambda e: None, clock)
    got: list[tuple[str, str, str]] = []
    cfg = PlayerConfig("rtp://@239.1.1.1:5004", Path("/tmp/unused"), headless=True)
    return RemotePlayer(cfg, "r/x/ch1/player", jobs, lambda *e: got.append(e), clock), jobs, got


def test_remote_player_seconds() -> None:
    clock = Clock()
    p, jobs, got = remote(clock)
    p.start()
    assert p.take_second().pb == "none"  # nothing reported yet: never "fine"
    jid = p.job_id

    def report(seq: int, **second: Any) -> None:
        jobs.report("d", [{"id": jid, "state": "RUNNING", "seq": seq, "first_frame": True,
                           "second": second}])  # fmt: skip

    report(1, playback="ok", dropped=0)
    s = p.take_second()
    assert (s.pb, s.dropped) == ("ok", 0) and p.first_frame
    report(2, playback="drop", dropped=3)
    report(3, playback="freeze", dropped=1, stall_started=True)  # two seconds at once
    s = p.take_second()
    assert (s.pb, s.dropped, s.freeze_started) == ("freeze", 4, True)
    assert p.take_second().pb == "freeze"  # a late report repeats the last second
    clock.t += LINK_S + 0.1  # the Desktop went away
    s = p.take_second()
    assert s.pb == "none" and [c for c, _, _ in got] == ["ZL-RCV-020"]
    assert p.take_second().pb == "none" and len(got) == 1  # told once
    assert p.lower_priority() is False and jobs.jobs("d")[0]["config"]["lowered"] is True


def test_remote_player_stop() -> None:
    clock = Clock()
    p, jobs, _ = remote(clock)
    p.start()
    jobs.report("d", [{"id": p.job_id, "state": "RUNNING"}])

    def desktop_stops() -> None:
        time.sleep(0.2)
        jobs.report("d", [{"id": p.job_id, "state": "IDLE"}])

    threading.Thread(target=desktop_stops).start()
    p.stop()
    assert p.flow.state is State.IDLE and jobs.state(p.job_id) is None


def test_monkey_reports() -> None:
    """Random reports, late, doubled, out of order, foreign ids, Desktop away: the
    player never reads "ok" without a fresh report, and never raises."""
    for seed in range(40):
        rnd = random.Random(seed)
        clock = Clock()
        p, jobs, _ = remote(clock)
        p.start()
        fresh_until = -1.0
        seq = 0
        for _ in range(60):
            act = rnd.random()
            if act < 0.5:
                seq += rnd.choice([1, 1, 1, 0, -1])
                pb = rnd.choice(["ok", "drop", "freeze", "none", "bogus"])
                jid = p.job_id if rnd.random() < 0.9 else "other"
                jobs.report("d", [{"id": jid, "state": rnd.choice(["RUNNING", "RUNNING", "X"]),
                                   "seq": seq, "second": {"playback": pb}}])  # fmt: skip
                if jid == p.job_id:
                    fresh_until = clock.t + LINK_S
            clock.t += rnd.choice([0.2, 1.0, 1.0, 4.0])
            s = p.take_second()
            assert s.pb in ("ok", "drop", "freeze", "none", "restart", "audio_gap"), seed
            if clock.t >= fresh_until:
                assert s.pb == "none", seed


# ---------------------------------------------------------------- node + DesktopLink


class FakePart:
    def __init__(self, config: dict[str, Any], log: list[str]) -> None:
        self.config = config
        self.flow = StateFlow("fake", lambda *a: None)
        self.log = log
        self.n = 0

    @property
    def state(self) -> str:
        return self.flow.state.value

    def start(self) -> None:
        self.flow.move(State.STARTING)
        self.flow.move(State.RUNNING)
        self.log.append("start")

    def stop(self) -> None:
        self.flow.move(State.STOPPING)
        self.flow.move(State.IDLE)
        self.log.append("stop")

    def change(self, config: dict[str, Any]) -> None:
        self.log.append(f"change {sorted(config.items())}")

    def report(self) -> dict[str, Any]:
        self.n += 1
        return {"second": {"playback": "ok", "dropped": 0}, "first_frame": True, "lowered": False}


@pytest.fixture
def pair(tmp_path: Path) -> Iterator[tuple[Node, DesktopLink, list[str]]]:
    node = Node(tmp_path / "node", "agent", host="127.0.0.1", port=0, local_port=0,
                player_host="desktop")  # fmt: skip
    node.start()
    log: list[str] = []

    def make(c: dict[str, Any], w: Path, p: Path, e: Any) -> FakePart:
        w.mkdir(parents=True)  # as the real player does (pictures, record)
        (w / "f000010.png").write_bytes(b"x")
        return FakePart(c, log)

    link = DesktopLink(node.local.port, {("iptv", "player"): make}, tmp_path / "desktop")
    yield node, link, log
    link.stop()
    node.stop()


def test_link_runs_what_the_node_lists(pair: Any) -> None:
    node, link, log = pair
    cfg = PlayerConfig("rtp://@239.1.1.1:5004", Path("/unused"), headless=True)
    p = RemotePlayer(cfg, "run1/rx/ch1/player", node.desktop, lambda *e: None)
    p.start()
    link.step()
    assert log == ["start"] and link.running_jobs == [p.job_id]
    link.step()
    assert p.first_frame and p.take_second().pb == "ok"
    st = link.status
    assert st is not None and st["node"]["boot_id"] == node.boot_id
    assert st["controller"]["connected"] is False  # no Controller has talked to it yet
    p.lower_priority()
    link.step()
    assert log[-1].startswith("change") and "('lowered', True)" in log[-1]
    stopper = threading.Thread(target=p.stop)
    stopper.start()
    assert wait(lambda: not node.desktop.jobs(link.desktop_id))
    for _ in range(3):
        link.step()
        time.sleep(0.05)
    stopper.join(5)
    assert p.flow.state is State.IDLE and log[-1] == "stop" and link.running_jobs == []
    assert list((link.work_dir / "jobs").iterdir()) == []  # the job's files went (G-11)


def test_players_end_with_the_node(pair: Any, monkeypatch: Any) -> None:
    """Bound: the node goes quiet -> after LINK_S the Desktop stops its players."""
    node, link, log = pair
    RemotePlayer(PlayerConfig("rtp://@239.1.1.1:5004", Path("/u"), headless=True),
                 "run1/rx/ch1/player", node.desktop, lambda *e: None).start()  # fmt: skip
    link.step()
    assert log == ["start"]
    node.local.stop()
    t = {"now": time.monotonic()}
    monkeypatch.setattr(link, "_clock", lambda: t["now"])
    link.step()
    assert log == ["start"]  # one missed poll is not the end
    t["now"] += LINK_S + 0.1
    link.step()
    assert wait(lambda: log[-1:] == ["stop"], 5) and link.status is None and not link.node_ok


def test_a_restarted_node_ends_the_old_players(pair: Any) -> None:
    node, link, log = pair
    RemotePlayer(PlayerConfig("rtp://@239.1.1.1:5004", Path("/u"), headless=True),
                 "run1/rx/ch1/player", node.desktop, lambda *e: None).start()  # fmt: skip
    link.step()
    node.boot_id = "a-new-boot"  # as a restarted node answers
    link.step()
    assert wait(lambda: log[-1:] == ["stop"], 5)


def test_second_desktop_is_refused(pair: Any, tmp_path: Path) -> None:
    node, link, _ = pair
    link.step()
    other = DesktopLink(node.local.port, {}, tmp_path / "d2")
    other.step()
    assert other.refused and "Another Agent Desktop" in other.refused
    other.stop()


def test_unknown_job_kind_is_reported_not_raised(pair: Any) -> None:
    node, link, _ = pair
    got: list[Event] = []
    node.bus.subscribe(got.append)
    node.desktop.want("run1/x/ch1/viewer", "other", "viewer", {})
    link.step()
    link.step()
    s = node.desktop.state("run1/x/ch1/viewer")
    assert s is not None and s.state == "FAULT"
    assert any(e.code == "ZL-RCV-019" and "no viewer" in e.text for e in got)


def test_iptv_desktop_entry_loads() -> None:
    entries, errors = load_desktop_entries(REPO / "modules")
    assert errors == {} and set(entries["iptv"].jobs) == {"player"}
    rows = entries["iptv"].status_rows({
        "sender": {"channels": [{"ch": 1, "group": "239.1.1.1:5004", "state": "RUNNING",
                                 "tx_bitrate_bps": 35_000_000}]},
        "receivers": [{"id": "rx", "ch": 2, "state": "RUNNING", "measuring": True,
                       "lost_total": 0, "last_1s": {"jit": 2.14, "br": 35e6, "pb": "ok",
                                                    "lost": 0}}],
    })  # fmt: skip
    assert rows[0]["color"] == "green" and "35.0 Mbps" in rows[0]["lines"][0]
    assert rows[1]["title"] == "Receiving CH2" and rows[1]["big"] == [["Jitter", "2.1 ms"],
                                                                     ["Loss", "0"]]  # fmt: skip
    assert "Player   Playing" in rows[1]["lines"]
    red = entries["iptv"].status_rows({"receivers": [{"ch": 1, "state": "RUNNING",
                                       "measuring": True, "last_1s": {"lost": 2}}]})  # fmt: skip
    assert red[0]["color"] == "red"


# ---------------------------------------------------------------- a whole run, real VLC

needs_tools = pytest.mark.skipif(
    not (shutil.which("tsp") and shutil.which("ffmpeg") and shutil.which("mpv")),
    reason="needs tsp, ffmpeg and mpv",
)


@needs_tools
def test_whole_run_with_the_player_in_agent_desktop(tmp_path: Path, hd_file: Path) -> None:
    from controller.devices import DeviceList
    from controller.run import RunController, RunState
    from core.events.bus import EventBus
    from tests.iptv.test_player import player_pids
    from tests.iptv.test_run_controller import plan, wait_state

    node = Node(tmp_path / "agent", "sender+receiver", host="127.0.0.1", port=0, local_port=0,
                modules_dir=REPO / "modules", lost_after_s=30, player_host="desktop")  # fmt: skip
    node.start()
    entries, _ = load_desktop_entries(REPO / "modules")
    factories = {(m, k): f for m, e in entries.items() for k, f in e.jobs.items()}
    link = DesktopLink(node.local.port, factories, tmp_path / "desktop")
    link.start()
    devices = DeviceList(tmp_path / "devices.json")
    bus = EventBus()
    rc = RunController(devices, tmp_path / "reports", REPO / "modules", bus, lost_s=4)
    devices.add("local", "127.0.0.1", node.api.port, rc.boot_id)
    before = player_pids()
    try:
        rc.start(plan(hd_file, duration=12))
        assert player_pids() - before, "Agent Desktop started no player"
        assert wait_state(rc, RunState.IDLE)
        r = rc.result
        assert r is not None and r["verdict"] == "PASS", r
        dev = r["devices"][0]
        assert dev["playback"] == "on" and dev["loss_total"] == 0
        assert wait(lambda: not (player_pids() - before), 10)  # stopped with the receiver
        assert link.running_jobs == []
    finally:
        link.stop()
        if rc.state is RunState.RUNNING:
            rc.stop()
        node.stop()


@needs_tools
def test_no_desktop_fails_the_start_rcv_010(tmp_path: Path, hd_file: Path) -> None:
    from controller.devices import DeviceList
    from controller.run import RunController, RunState
    from core.events.bus import EventBus
    from tests.iptv.test_run_controller import plan

    node = Node(tmp_path / "agent", "sender+receiver", host="127.0.0.1", port=0, local_port=0,
                modules_dir=REPO / "modules", lost_after_s=30, player_host="desktop")  # fmt: skip
    node.start()
    devices = DeviceList(tmp_path / "devices.json")
    events: list[Event] = []
    bus = EventBus()
    bus.subscribe(events.append)
    rc = RunController(devices, tmp_path / "reports", REPO / "modules", bus, lost_s=4)
    devices.add("local", "127.0.0.1", node.api.port, rc.boot_id)
    link = DesktopLink(node.local.port, {}, tmp_path / "desktop")
    try:
        with pytest.raises(ZlError) as e:
            rc.start(plan(hd_file, duration=12))
        assert e.value.code == "ZL-RCV-010" and rc.state is RunState.IDLE
        assert rc.checklist is not None
        step4 = rc.checklist.to_dict()["steps"][3]  # F17 step 4: stopped before sending
        assert (step4["state"], step4["code"], step4["device"]) == ("failed", "ZL-RCV-010",
                                                                    "local")  # fmt: skip
        assert any(x.code == "ZL-RCV-010" and x.device == "local" for x in events)
        idle = rc.precheck(plan(hd_file, duration=12), full=False)  # still not logged in
        assert idle["ok"] is False and not any(x.data.get("clears") for x in events)
        link.step()  # the user logged in: Agent Desktop is there
        assert rc.precheck(plan(hd_file, duration=12), full=False)["ok"] is True
        cleared = [x for x in events if x.data.get("clears") == ["ZL-RCV-010"]]
        assert len(cleared) == 1 and cleared[0].device == "local"  # Action closes by itself
    finally:
        link.stop()
        node.stop()


@pytest.fixture(scope="module")
def hd_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not (shutil.which("tsp") and shutil.which("ffmpeg")):
        pytest.skip("needs ffmpeg and tsp")
    import subprocess

    from modules.iptv.sender.commands import QUALITIES, prepare_command

    out = tmp_path_factory.mktemp("prep") / "ch1.ts"
    subprocess.run(prepare_command("ffmpeg", QUALITIES["HD"], 1, out, seconds=4,
                                   preset="ultrafast"), check=True, timeout=300)  # fmt: skip
    return out
