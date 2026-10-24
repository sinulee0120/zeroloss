"""Controller run flow end to end on one node (loopback NIC, nothing leaves the PC):
start order, warm-up, summaries, verdict, report; stop, start failures, device loss."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from controller.devices import DeviceList
from controller.run import RunController, RunState
from core.events.bus import Event, EventBus
from core.node import Node
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError
from modules.iptv.sender.commands import QUALITIES, prepare_command
from tests.conftest import _ours_alive

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not (shutil.which("tsp") and shutil.which("ffmpeg")), reason="needs ffmpeg and tsp"
)
_n = itertools.count(40)


@pytest.fixture(scope="module")
def hd_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("prep") / "ch1.ts"
    subprocess.run(
        prepare_command("ffmpeg", QUALITIES["HD"], 1, out, seconds=4, preset="ultrafast"),
        check=True,
        timeout=300,
    )
    return out


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[tuple[Node, RunController, list[Event]]]:
    node = Node(
        tmp_path / "agent",
        "sender+receiver",
        host="127.0.0.1",
        port=0,
        local_port=0,
        modules_dir=REPO / "modules",
        lost_after_s=30,
    )
    # these tests are not about SYS-005: a PC without logind keeps the same answers
    node.sleep_guard.probe = lambda: None  # type: ignore[method-assign]
    node.sleep_guard.state = "ok"
    node.start()
    devices = DeviceList(tmp_path / "devices.json")
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    rc = RunController(devices, tmp_path / "reports", REPO / "modules", bus, lost_s=4)
    devices.add("local", "127.0.0.1", node.api.port, rc.boot_id)
    yield node, rc, events
    if rc.state is RunState.RUNNING:
        rc.stop()
    node.stop()


def plan(hd_file: Path, duration: int = 12, **over: Any) -> dict[str, Any]:
    n = next(_n)
    groups = [f"239.255.81.{n}", f"239.255.82.{n}", f"239.255.83.{n}"]
    p: dict[str, Any] = {
        "schema_version": 1,
        "module": "iptv",
        "test_type": "pc",
        "devices": {"sender": "local", "receivers": [{"device": "local", "ch": 1}]},
        "quality": "HD",
        "duration_s": duration,
        "settings": {"S-14": {"groups": groups, "port": 5004}},
        "nics": {"local": "127.0.0.1"},
        "files": {"1": str(hd_file)},
    }
    p.update(over)
    return p


def wait_state(rc: RunController, st: RunState, timeout_s: float = 40) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if rc.state is st:
            return True
        time.sleep(0.2)
    return False


def player_running() -> bool:
    from tests.iptv.test_player import player_pids

    return bool(player_pids())


def tsp_running() -> bool:
    return bool(subprocess.run(["pgrep", "-x", "tsp"], capture_output=True).stdout.strip())


def test_full_run_passes_and_writes_the_report(lab: Any, hd_file: Path) -> None:
    _, rc, _ = lab
    run_id = rc.start(plan(hd_file, duration=12))
    assert rc.state is RunState.RUNNING
    assert wait_state(rc, RunState.IDLE)
    r = rc.result
    assert r is not None and r["verdict"] == "PASS", r
    assert r["verdict_reason"] == "planned_time_reached" and r["not_run"] == []
    dev = r["devices"][0]
    assert dev["playback"] == "on"  # S-49 On by default: VLC played (headless, iptv 3.2)
    assert dev["loss_total"] == 0 and dev["cells"] and dev["colors"]["green"] >= 1
    live = rc.status()["cells"][dev["device"]]  # screen cells agree with the report
    assert [c["color"] for c in live] == [c["color"] for c in dev["cells"]]
    run_dir = rc.reports_dir / run_id
    assert json.loads((run_dir / "summary.json").read_text())["verdict"] == "PASS"
    assert "✔" in (run_dir / "report.html").read_text()
    assert json.loads((run_dir / "run-state.json").read_text())["status"] == "finished"
    pieces = [json.loads(x) for x in (run_dir / "summaries.jsonl").read_text().splitlines()]
    assert pieces and all(s["playback"]["player_restarts"] == 0 for s in pieces)
    # RTP: VLC statistics mostly frozen (None); a second they moved is measured, 0 kept (GAP-180)
    assert all(s["playback"]["frame_drops"] in (None, 0) for s in pieces)
    raw = next((rc.reports_dir.parent / "agent").rglob("raw/*/*.jsonl")).read_text().splitlines()
    pbs = [json.loads(x).get("pb") for x in raw if '"type":"s"' in x.replace(" ", "")]
    assert pbs and set(pbs) == {"ok"}, pbs  # the player's own report, every second
    assert not tsp_running() and not player_running()


def test_records_are_fetched_after_the_report(lab: Any, hd_file: Path) -> None:
    """02 2-13 (20 GAP-80): the run's end hands over the run and its devices only after
    the report is written; fetching copies every 1-second record file."""
    from controller.records import collect

    _, rc, _ = lab
    calls: list[tuple[str, list[str], bool]] = []
    rc._on_report = lambda run, devs: calls.append(
        (run, devs, (rc.reports_dir / run / "summary.json").is_file()))  # fmt: skip
    run_id = rc.start(plan(hd_file, duration=12))
    assert wait_state(rc, RunState.IDLE)
    assert calls == [(run_id, ["local"], True)]
    got = collect(rc.reports_dir, run_id, rc.devices.all(), rc.boot_id)
    assert got["missing"] == {} and got["files"], got
    assert all(f["state"] == "got" for f in got["files"])
    fetched = list((rc.reports_dir / run_id / "raw").rglob("*.jsonl"))
    assert len(fetched) == len(got["files"])


def test_run_clock_events_timeline_and_both_analysis_passes(lab: Any, hd_file: Path) -> None:
    """iptv 2.4 (20 GAP-85): the run folder has events.log on the run clock with the
    receiver's join, timeline.json with the receiver's offset, a first analysis from
    the report and a second one once the records are fetched; the verdict stays."""
    from controller.analysis import reanalyze
    from controller.records import collect

    _, rc, _ = lab
    run_id = rc.start(plan(hd_file, duration=12))
    assert wait_state(rc, RunState.IDLE)
    run_dir = rc.reports_dir / run_id
    events = [json.loads(x) for x in (run_dir / "events.log").read_text().splitlines()]
    assert events and all(isinstance(e["run_s"], float) for e in events)
    joins = [e for e in events if str((e.get("data") or {}).get("step", "")).endswith(
        "/receiver/start")]  # fmt: skip
    assert len(joins) == 1 and joins[0]["device"] == "local" and joins[0]["data"]["ch"] == 1
    offsets = json.loads((run_dir / "timeline.json").read_text())
    assert list(offsets) == ["local|1"] and offsets["local|1"] > joins[0]["run_s"]
    first = json.loads((run_dir / "summary.json").read_text())
    assert first["analysis_pass"] == "report" and first["analysis"] == []
    assert "1-second records not fetched yet" in {m["why"] for m in first["analysis_missing"]}
    collect(rc.reports_dir, run_id, rc.devices.all(), rc.boot_id)
    assert reanalyze(run_dir, REPO / "modules") is True
    second = json.loads((run_dir / "summary.json").read_text())
    assert second["analysis_pass"] == "records" and second["verdict"] == first["verdict"]
    assert "1-second records not fetched yet" not in {m["why"] for m in second["analysis_missing"]}


def test_stop_early_is_incomplete(lab: Any, hd_file: Path) -> None:
    _, rc, _ = lab
    rc.start(plan(hd_file, duration=600))
    time.sleep(3)
    r = rc.stop()
    assert r["verdict"] == "INCOMPLETE" and r["verdict_reason"] == "stopped_by_user"
    assert rc.state is RunState.IDLE and not tsp_running()
    with pytest.raises(ZlError) as e:
        rc.stop()
    assert e.value.code == "ZL-RUN-012"


def iptv_of(node: Node) -> Any:
    return next(m.instance for m in node.modules if m.id == "iptv")


def fast_videos(node: Node, seconds: float = 3.0) -> Any:
    prep = iptv_of(node).preparer
    prep.seconds, prep.preset = seconds, "ultrafast"
    return prep


def start_in_thread(rc: RunController, p: dict[str, Any]) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []

    def go() -> None:
        try:
            out.append(rc.start(p))
        except ZlError as e:
            out.append(e)

    th = threading.Thread(target=go)
    th.start()
    return th, out


def test_plan_without_files_makes_the_video_then_runs(lab: Any, hd_file: Path) -> None:
    """iptv 1-1-2: no prepared file -> the start makes the generated video (progress in
    status ``preparing``), then sends it; a second start reuses it."""
    node, rc, _ = lab
    fast_videos(node)
    th, out = start_in_thread(rc, plan(hd_file, files={}))
    texts = set()
    while th.is_alive():
        prep = rc.status()["preparing"]
        if prep:
            texts.add(prep["text"])
        time.sleep(0.05)
    assert isinstance(out[0], str), out
    assert texts and all(t.startswith("CH1 HD") for t in texts)
    ch = rc.status()["devices"]["local"]["module_data"]["sender"]["channels"][0]
    assert ch["source"] == "generated"
    with pytest.raises(ZlError) as e:  # making videos only between tests (02 2-4)
        rc.prepare_ahead({"module": "iptv", "sender": "local", "quality": "HD"})
    assert e.value.code == "ZL-RUN-012"
    assert wait_state(rc, RunState.IDLE)
    assert rc.result is not None and rc.result["verdict"] == "PASS", rc.result
    assert rc.status()["preparing"] is None


def test_missing_prepared_file_falls_back_to_the_generated_video(
    lab: Any, hd_file: Path, tmp_path: Path
) -> None:
    """iptv 1-1 / SND-001: a missing file does not block the start; it is a warning."""
    node, rc, events = lab
    fast_videos(node)
    rc.start(plan(hd_file, files={"1": str(tmp_path / "nope.ts")}))
    ch = rc.status()["devices"]["local"]["module_data"]["sender"]["channels"][0]
    assert ch["source"] == "generated_fallback"
    assert wait_state(rc, RunState.IDLE)
    assert any(e.code == "ZL-SND-001" and e.level == "warn" for e in events)
    assert rc.result is not None and rc.result["verdict"] == "PASS"


def test_cancel_while_the_video_is_made(lab: Any, hd_file: Path) -> None:
    """USR-006 (GAP-116): Cancel rolls only the start back; the sender goes on making the
    file (the next Start is quick). Stopping that making leaves nothing half made."""
    node, rc, events = lab
    prep = fast_videos(node, seconds=300)
    th, out = start_in_thread(rc, plan(hd_file, files={}))
    end = time.monotonic() + 30
    while not rc.status()["preparing"]:
        assert time.monotonic() < end and th.is_alive(), out
        time.sleep(0.05)
    assert rc.stop() == {"cancelled": True}
    th.join(60)
    assert isinstance(out[0], ZlError) and out[0].code == "ZL-USR-006"
    assert rc.state is RunState.IDLE
    assert prep.status()["busy"]  # still making it (09 USR-006, GAP-116)
    assert not any(e.code == "ZL-USR-006" and e.level == "error" for e in events)
    prep.cancel()  # the node's own stop of the making (shutdown, 12 4-3)
    end = time.monotonic() + 15
    while prep.status()["busy"] and time.monotonic() < end:
        time.sleep(0.1)
    assert not [p for p in _ours_alive() if "ffmpeg" in p]
    assert [x for x in prep.folder.iterdir() if x.name.endswith((".part.ts", ".progress"))] == []


def test_video_that_cannot_be_made_fails_the_start(lab: Any, hd_file: Path, tmp_path: Path) -> None:
    node, rc, _ = lab
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\necho 'no encoder' >&2\nexit 1\n")
    fake.chmod(0o755)
    fast_videos(node).ffmpeg = str(fake)
    with pytest.raises(ZlError) as e:
        rc.start(plan(hd_file, files={}))
    assert e.value.code == "ZL-SND-020" and "no encoder" in e.value.why
    assert rc.state is RunState.IDLE and not tsp_running()


def test_prepare_ahead_then_three_uhd_channels_send_together(lab: Any) -> None:
    """zeroloss prepare (02 2-4) for UHD CH1..3, then three tsp at 35 Mbps at once
    (iptv 1: one process per channel). Loopback only."""
    node, rc, _ = lab
    fast_videos(node, seconds=2.0)
    r = rc.prepare_ahead({"module": "iptv", "sender": "local", "quality": "UHD"})
    assert [c["state"] for c in r["channels"]] == ["queued"] * 3
    end = time.monotonic() + 240
    while rc.prepare_state("iptv", "local")["busy"]:
        assert time.monotonic() < end
        time.sleep(0.5)
    st = rc.prepare_state("iptv", "local")
    assert [(c["ch"], c["state"]) for c in st["channels"]] == [
        (1, "ready"),
        (2, "ready"),
        (3, "ready"),
    ]
    client = rc._client("local")
    try:
        for ch in (1, 2, 3):
            got = client.command(
                "/v1/modules/iptv/sender/start", rc.generation, "R-3ch", timeout_s=20,
                ch=ch, quality="UHD", dest_ip="127.0.0.1", port=5100 + 2 * ch,
                muxrate_bps=35_000_000,
            )  # fmt: skip
            assert got["state"] == "RUNNING" and got["source"] == "generated"
        time.sleep(3)
        chans = iptv_of(node).status()["sender"]["channels"]
        assert [c["state"] for c in chans] == ["RUNNING"] * 3
        assert all(abs(c["tx_bitrate_bps"] - 35e6) <= 35e6 * 0.01 for c in chans), chans
    finally:
        for ch in (1, 2, 3):
            client.command("/v1/modules/iptv/sender/stop", rc.generation, "R-3ch", ch=ch)
    assert not tsp_running()


def test_receiver_gets_nothing_warmup_fails_and_sender_is_stopped(
    lab: Any, hd_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receiver joins a group nobody sends to. Both NICs stay on loopback, so no test
    traffic ever leaves this PC."""
    from modules.iptv.run import IptvRun, start_steps

    def wrong_group(p: Any) -> Any:
        stages = start_steps(p)
        for step in stages[1]:
            step.params["group"] = "239.255.99.99"
        return stages

    monkeypatch.setattr(IptvRun, "start_steps", staticmethod(wrong_group))
    _, rc, _ = lab
    with pytest.raises(ZlError) as e:
        rc.start(plan(hd_file, settings={"S-28": 5}))
    assert e.value.code == "ZL-RUN-009"
    assert rc.state is RunState.IDLE and not tsp_running()


def test_lost_agent_safe_stops_to_fault_then_reset(lab: Any, hd_file: Path) -> None:
    node, rc, _ = lab
    rc.start(plan(hd_file, duration=600))
    time.sleep(2)
    node.stop()  # the agent PC disappears
    assert wait_state(rc, RunState.FAULT, 20)
    assert rc.result is not None and rc.result["verdict"] in ("INCOMPLETE", "FAIL")
    assert rc.result["verdict"] != "PASS"
    assert "device_lost" in rc.result["verdict_reason"]
    fault = rc.status()["fault"]
    assert fault["device"] == "local" and fault["last_ok_utc"] and fault["run_id"] == rc.run_id
    r = rc.reset()  # the PC is gone: not clean, FAULT stays, the fix names it (F11 5)
    assert not r["ok"] and rc.state is RunState.FAULT
    row = r["devices"][0]
    assert row["device"] == "local" and not row["answered"] and "On local" in row["fix"]
    rc.devices.remove("local")  # the operator took the dead PC out of the list
    assert rc.reset()["ok"] and rc.status()["state"] == "IDLE" and rc.status()["fault"] is None


def _link(mbps: int | None) -> Any:
    from core.blocks.link_monitor import Link

    return Link("eth9", "127.0.0.1", True, mbps, "full", 0)


def test_a_second_controller_stops_the_node_and_the_run_is_never_pass(
    lab: Any, hd_file: Path
) -> None:
    """GAP-100 (first real run): another Controller's request made the receiver stop
    itself (CTL-008); ours ran 112 s more and said PASS on 9 s. Now it ends at once."""
    node, rc, _ = lab
    rc.start(plan(hd_file, duration=120))
    time.sleep(3)
    other = NodeClient("127.0.0.1", node.api.port, "another-controller-boot")
    other.get("/v1/heartbeat", seq=1)  # the node sees a new Controller: safe stop
    other.close()
    assert wait_state(rc, RunState.FAULT, 20)
    r = rc.result
    assert r is not None and r["verdict"] != "PASS"
    assert "stopped its part" in r["verdict_reason"]
    assert all(s["run_id"] == rc.run_id for v in rc._summaries.values() for s in v)


def test_slow_link_blocks_the_start_net_012(lab: Any, hd_file: Path) -> None:
    """05 1.8: HD 1 ch needs ~11 Mbps x 1.1; a 10 Mbps link stops the start before
    anything is sent (10 링크 속도 변화)."""
    node, rc, _ = lab
    node.links_reader = lambda: [_link(10)]
    time.sleep(1.5)  # the node reads links once a second
    with pytest.raises(ZlError) as e:
        rc.start(plan(hd_file))
    assert e.value.code == "ZL-NET-012"
    assert rc.state is RunState.IDLE and not tsp_running()


def test_test_card_rules() -> None:
    """03 F17 / GAP-99 2: which card a device uses when the plan names none."""
    from controller.precheck import test_card

    wired: list[dict[str, Any]] = [{"nic": "enp0s31f6", "ip": "192.168.45.177"},
             {"nic": "wlp4s0", "ip": "192.168.0.13", "wireless": True}]  # fmt: skip
    assert test_card("sender", "192.168.0.27", [], "10.0.0.1") == ("10.0.0.1", "")
    assert test_card("receiver", "192.168.45.177", wired, None) == ("192.168.45.177", "")
    ip, why = test_card("receiver", "192.168.0.13", wired, None)
    assert ip is None and "Wi-Fi wlp4s0" in why
    assert test_card("receiver", "127.0.0.1", [], None) == ("127.0.0.1", "")
    assert test_card("sender", None, wired, None)[0] is None
    assert test_card("receiver", "10.9.9.9", wired, None)[0] is None
    # baseline (S-54, GAP-115): no router; reached over home Wi-Fi, the receiver's card is
    # the one the sender's cable leased (10.0.0.0/24), whatever card the Controller reached
    home: list[dict[str, Any]] = [{"nic": "wlp4s0", "ip": "192.168.0.13", "wireless": True},
            {"nic": "enp0s31f6", "ip": "10.0.0.100"}]  # fmt: skip
    assert test_card("receiver", "192.168.0.13", home, "10.0.0.1", baseline=True) == (
        "10.0.0.100",
        "",
    )
    assert test_card("receiver", "192.168.0.13", home, "10.0.0.1")[0] is None  # router: unknown
    ip, why = test_card("receiver", "192.168.0.13", home[:1], "10.0.0.1", baseline=True)
    assert ip is None and "10.0.0.0/24" in why


def test_a_plan_without_nics_gets_them_or_never_passes(
    lab: Any, hd_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GAP-99 2 (user decision "1"): filled when the Controller can tell; when it cannot,
    the run goes on with NET-022, the report says so and PASS becomes INCOMPLETE."""
    _, rc, events = lab
    p = plan(hd_file, duration=10)
    p.pop("nics")
    p["settings"] = {**p["settings"], "S-49": "Off"}
    rc.start(p)
    assert wait_state(rc, RunState.IDLE, 60)
    r = rc.result
    assert r is not None and r["verdict"] == "PASS" and r["dut"]["through_router"] is True
    assert json.loads((rc.reports_dir / r["run_id"] / "plan.json").read_text())["nics"] == {
        "local": "127.0.0.1"}  # fmt: skip
    real = rc._fill_test_cards

    def not_told(infos: Any) -> None:  # the plan has the card; the Controller says it cannot
        real(infos)  # tell (Wi-Fi) — the stream itself stays on loopback
        rc._not_via = ["local: reached over Wi-Fi wlx0, which may not be the router"]
        rc._event("ZL-NET-022", "warn", "local: the test may not go through the router", "local")

    monkeypatch.setattr(rc, "_fill_test_cards", not_told)
    p2 = plan(hd_file, duration=10)
    p2["settings"] = {**p2["settings"], "S-49": "Off"}
    rc.start(p2)
    assert wait_state(rc, RunState.IDLE, 60)
    r2 = rc.result
    assert r2 is not None and r2["verdict"] == "INCOMPLETE", r2
    assert r2["dut"]["through_router"] is False and "wlx0" in r2["dut"]["not_through_router_why"]
    assert any(x.code == "ZL-NET-022" for x in events)
    html = (rc.reports_dir / r2["run_id"] / "report.html").read_text()
    assert "Not through the router under test" in html


def test_a_test_ip_no_card_has_stops_at_step_3_net_021(lab: Any, hd_file: Path) -> None:
    """GAP-99 (first real run): 10.0.0.1 was gone from the sender, step 3 said links ok
    and step 5 blamed tsp. Now step 3 says which address is missing, nothing is sent."""
    _, rc, _ = lab
    with pytest.raises(ZlError) as e:
        rc.start(plan(hd_file, nics={"local": "10.255.255.1"}))
    assert e.value.code == "ZL-NET-021" and "10.255.255.1" in e.value.what
    assert rc.state is RunState.IDLE and not tsp_running()
    steps = {s["n"]: s for s in (rc.checklist.to_dict()["steps"] if rc.checklist else [])}
    assert steps[3]["state"] == "failed"


def test_link_drop_during_the_test_net_013(lab: Any, hd_file: Path) -> None:
    node, rc, events = lab
    speed = {"mbps": 1000}
    node.links_reader = lambda: [_link(speed["mbps"])]
    time.sleep(1.5)
    rc.start(plan(hd_file, duration=30))
    (check,) = rc.status()["devices"]["local"]["link_checks"]
    assert check["level"] == "ok" and check["usual_mbps"] == 1000
    speed["mbps"] = 10
    end = time.monotonic() + 10
    while time.monotonic() < end and not any(e.code == "ZL-NET-013" for e in events):
        time.sleep(0.2)
    assert [e.code for e in events].count("ZL-NET-013") == 1
    (check,) = rc.status()["devices"]["local"]["link_checks"]
    assert check["level"] == "short" and check["link_mbps"] == 10
    speed["mbps"] = 100  # enough, but below the usual 1 Gbps
    end = time.monotonic() + 10
    while time.monotonic() < end and not any(e.code == "ZL-NET-014" for e in events):
        time.sleep(0.2)
    assert rc.status()["devices"]["local"]["link_checks"][0]["level"] == "slow"
    rc.stop()


def test_link_down_of_a_nic_the_test_does_not_use_is_only_noted(lab: Any, hd_file: Path) -> None:
    """09 NET-015: the PC's internet Wi-Fi going down is no problem of the test (info);
    the test NIC going down is the error."""
    from core.blocks.link_monitor import Link

    node, rc, events = lab
    state = {"wifi": True, "test": True}

    def links() -> list[Link]:
        return [
            Link("eth9", "127.0.0.1", state["test"], 1000, "full", 0),
            Link("wlp9", "192.168.0.13", state["wifi"], None, None, 0),
        ]

    node.links_reader = links
    time.sleep(1.5)
    rc.start(plan(hd_file, duration=30))

    def wait_for(want: int) -> list[Event]:
        end = time.monotonic() + 10
        while time.monotonic() < end:
            got = [e for e in events if e.code == "ZL-NET-015"]
            if len(got) >= want:
                return got
            time.sleep(0.2)
        return [e for e in events if e.code == "ZL-NET-015"]

    state["wifi"] = False
    (other,) = wait_for(1)
    assert other.level == "info" and "not a test network card" in other.text
    state["test"] = False
    mine = wait_for(2)[-1]
    assert mine.level == "error" and "eth9" in mine.text
    state["test"] = state["wifi"] = True
    rc.stop()


def test_player_that_cannot_start_fails_the_start_rcv_019(
    lab: Any, hd_file: Path, monkeypatch: Any
) -> None:
    """10 "재생기가 안 켜짐" ①: playback on but no player -> start fails, rolled back."""
    from modules.iptv.receiver import player as pl

    monkeypatch.setattr(pl, "mpv_binary", lambda: "/nonexistent/mpv")
    _, rc, events = lab
    with pytest.raises(ZlError) as e:
        rc.start(plan(hd_file, settings={**plan(hd_file)["settings"], "S-28": 6}))
    assert e.value.code == "ZL-RUN-009"
    assert rc.state is RunState.IDLE and not tsp_running()
    node = [ev for ev in events if ev.data.get("from_node")]
    assert any(ev.code == "ZL-RCV-019" and ev.device == "local" for ev in node)  # S-40
    assert "first_frame" in e.value.detail  # the node's reason reaches the user


def test_killed_player_does_not_touch_the_measurement(lab: Any, hd_file: Path) -> None:
    """10 "재생기 강제 종료": measurement goes on, the player is restarted, no loss."""
    from tests.iptv.test_player import player_pids

    _, rc, _ = lab
    run_id = rc.start(plan(hd_file, duration=16))
    (pid,) = player_pids()
    os.kill(pid, signal.SIGKILL)
    assert wait_state(rc, RunState.IDLE)
    r = rc.result
    assert r is not None and r["devices"][0]["loss_total"] == 0, r
    run_dir = rc.reports_dir.parent / "agent"
    raw = next(run_dir.rglob(f"raw/*/{run_id}_h000.jsonl")).read_text().splitlines()
    secs = [json.loads(x) for x in raw if '"type": "s"' in x or '"type":"s"' in x]
    assert [s["e"] for s in secs] == list(range(len(secs)))  # no second missing
    assert any(s.get("prs") == 1 for s in secs)  # restart recorded
    assert not player_running()


def test_start_stop_20_times_without_a_wrong_start_or_stop(lab: Any, hd_file: Path) -> None:
    """04 P2 completion: Start/Stop 20 times -> no wrong start/stop. Random waits, double
    Start / Stop, and a Start while stopping (playbook: random + repeated + idle)."""
    import random
    import threading

    from tests.iptv.test_player import player_pids

    _, rc, events = lab
    rnd = random.Random(20260925)
    seen: set[str] = set()
    for i in range(20):
        run_id = rc.start(plan(hd_file, duration=600))
        assert run_id not in seen and rc.state is RunState.RUNNING, i
        seen.add(run_id)
        with pytest.raises(ZlError) as e:  # double Start
            rc.start(plan(hd_file, duration=600))
        assert e.value.code == "ZL-RUN-012"
        time.sleep(rnd.choice([0.0, 0.3, 1.0, 2.5]))
        late: list[BaseException] = []

        def start_while_stopping(out: list[BaseException] = late) -> None:
            try:
                rc.start(plan(hd_file, duration=600))
            except BaseException as ex:
                out.append(ex)

        th = threading.Thread(target=start_while_stopping)
        th.start()  # races the Stop below: refused, or starts only after IDLE
        r = rc.stop()
        th.join(60)
        if rc.state is RunState.RUNNING:  # the late Start won the queue after the stop
            seen.add(str(rc.run_id))
            rc.stop()
        else:
            assert late and isinstance(late[0], ZlError), late
        # stopped early: INCOMPLETE, or FAIL when it got under 1,000 packets (03 F10)
        assert r["run_id"] == run_id and r["verdict"] in ("INCOMPLETE", "FAIL"), r
        assert r["verdict_reason"] == "stopped_by_user"
        assert rc.state.value == "IDLE"  # (mypy keeps the narrowing from above)
        with pytest.raises(ZlError):  # double Stop
            rc.stop()
        assert not tsp_running() and not player_pids(), i
    wrong = [e for e in events if e.code in ("ZL-DSC-014", "ZL-RCV-018", "ZL-SYS-006")]
    assert not wrong, wrong


def test_problems_follow_the_link_and_the_run(lab: Any, hd_file: Path) -> None:
    """F21: NET-013 shows while the link is short, moves to resolved when it is back
    (02 8 clears), and nothing stays active after the test ends."""
    from controller.problems import ProblemBoard

    node, rc, _ = lab
    board = ProblemBoard()
    rc.bus.subscribe(board)
    rc._on_finished = lambda: board.resolve_all("test ended")
    speed = {"mbps": 1000}
    node.links_reader = lambda: [_link(speed["mbps"])]
    time.sleep(1.5)
    rc.start(plan(hd_file, duration=60))

    def row(code: str) -> dict[str, Any] | None:
        return next((r for r in board.snapshot()["problems"] if r["code"] == code), None)

    speed["mbps"] = 10
    end = time.monotonic() + 10
    while time.monotonic() < end and not (row("ZL-NET-013") or {}).get("active"):
        time.sleep(0.2)
    r = row("ZL-NET-013")
    assert r and r["active"] and r["device"] == "local" and "needs about" in r["why"]
    speed["mbps"] = 1000
    end = time.monotonic() + 10
    while time.monotonic() < end and (row("ZL-NET-013") or {}).get("active"):
        time.sleep(0.2)
    assert not row("ZL-NET-013")["active"]  # type: ignore[index]
    rc.stop()
    assert board.snapshot()["counts"] == {"error": 0, "warn": 0}


def test_report_has_the_problems_of_the_run(lab: Any, hd_file: Path) -> None:
    """F21: summary.json problems / alarm_stats (02 10) and the report table."""
    from controller.problems import ProblemBoard

    _, rc, _ = lab
    board = ProblemBoard()
    rc.bus.subscribe(board)
    rc._problems_report = board.run_report
    run_id = rc.start(plan(hd_file, duration=30))
    rc.bus.publish(Event("ZL-RCV-020", "warn", "Player is not playing", device="local",
                         data={"run_id": run_id}))  # fmt: skip
    r = rc.stop()
    got = {(p["code"], p["open_at_end"]) for p in r["problems"]}
    assert ("ZL-RCV-020", True) in got  # this PC's small rmem_max also shows RCV-001 here
    assert r["alarm_stats"]["total"] == len(r["problems"])
    html_text = (rc.reports_dir / run_id / "report.html").read_text()
    assert "Problems during the test" in html_text and "ZL-RCV-020" in html_text


def test_controller_records_events_in_the_experience_db(tmp_path: Path) -> None:
    """14 1 / 20 GAP-46: every event on the Controller bus becomes a situation."""
    import sqlite3

    from controller.service import ControllerService

    svc = ControllerService(
        tmp_path / "ctl", tmp_path / "rep", REPO / "modules", host="127.0.0.1", port=0, local_port=0
    )
    svc.start()
    try:
        svc.node.bus.publish(Event("ZL-NET-013", "error", "link short", device="tx"))
    finally:
        svc.stop()
    rows = (
        sqlite3.connect(tmp_path / "ctl" / "experience.db")
        .execute("SELECT code, device FROM situations WHERE code = 'ZL-NET-013'")
        .fetchall()
    )
    assert rows == [("ZL-NET-013", "tx")]


def test_human_action_opens_closes_and_is_recorded(tmp_path: Path) -> None:
    """F22 + 14 1: a cable problem asks a person; the link coming back closes it; the
    experience DB gets the action with actor auto, outcome resolved."""
    import sqlite3

    from controller.service import ControllerService
    from core.protocol.client import NodeClient

    svc = ControllerService(
        tmp_path / "ctl", tmp_path / "rep", REPO / "modules", host="127.0.0.1", port=0, local_port=0
    )
    svc.actions._grace = False  # GAP-135 grace_s is test_actions' subject, not this one's
    svc.start()
    try:
        c = NodeClient("127.0.0.1", svc.node.local.port, "t", "svc")
        svc.node.bus.publish(Event("ZL-NET-013", "error", "link short", device="tx"))
        s = c.get("/v1/controller/actions")
        assert s["waiting"] == 1 and s["actions"][0]["action_id"] == "human.cable.check"
        svc.node.bus.publish(
            Event("ZL-INFO", "info", "back", device="tx", data={"clears": ["ZL-NET-013"]})
        )
        assert c.get("/v1/controller/actions")["waiting"] == 0
        svc.node.bus.publish(Event("ZL-SYS-006", "error", "tsp did not stop", device="rx"))
        key = c.get("/v1/controller/actions")["actions"][0]["key"]
        gen = int(c.get("/v1/controller/status")["generation"])
        assert c.command("/v1/controller/actions/done", gen, key=key) == {"done": key}
        with pytest.raises(ZlError):
            c.command("/v1/controller/actions/done", gen, key=key)  # nothing waiting
    finally:
        svc.stop()
    rows = (
        sqlite3.connect(tmp_path / "ctl" / "experience.db")
        .execute("SELECT action_id, actor, outcome FROM actions ORDER BY id")
        .fetchall()
    )
    assert rows == [
        ("human.cable.check", "auto", "resolved"),
        ("human.pc.power_cycle", "human", "resolved"),
    ]


def test_controller_crash_is_recovered_on_the_next_start(lab: Any, hd_file: Path) -> None:
    """F10 / CTL-003: the Controller dies mid-test. The next Controller finds the
    unfinished run, stops it on the node, collects the summaries and writes a
    recovery report: INCOMPLETE, recovered, reason controller_restarted."""
    node, rc, _ = lab
    run_id = rc.start(plan(hd_file, duration=600))
    time.sleep(3)
    rc._halt.set()  # the Controller process "dies": its loop stops, nothing is closed
    if rc._loop is not None:
        rc._loop.join(5)
    state = json.loads((rc.reports_dir / run_id / "run-state.json").read_text())
    assert state["status"] == "running"
    assert (rc.reports_dir / run_id / "plan.json").exists()  # 02 9 copy for recovery

    events: list[Event] = []
    bus = EventBus()
    bus.subscribe(events.append)
    rc2 = RunController(rc.devices, rc.reports_dir, REPO / "modules", bus, lost_s=4)
    assert rc2.boot_id != rc.boot_id
    done = rc2.recover_unfinished()
    assert done == [run_id]
    summary = json.loads((rc.reports_dir / run_id / "summary.json").read_text())
    assert summary["verdict"] == "INCOMPLETE" and summary["recovered"] is True
    assert summary["verdict_reason"] == "controller_restarted"
    assert json.loads((rc.reports_dir / run_id / "run-state.json").read_text())["status"] == (
        "recovered"
    )
    assert any(e.code == "ZL-CTL-003" for e in events)
    end = time.monotonic() + 10  # the node saw a new Controller and stopped everything
    while time.monotonic() < end and (tsp_running() or node.flow.state.value != "IDLE"):
        time.sleep(0.2)
    assert not tsp_running() and node.flow.state.value == "IDLE"
    assert rc2.recover_unfinished() == []  # once only


def test_disk_full_on_a_node_ends_the_test_with_a_report(lab: Any, hd_file: Path) -> None:
    """F10 automatic stop / RCV-008: a node below 200 MB stops itself; the Controller
    ends the whole test normally and writes the report (INCOMPLETE, disk_low)."""
    node, rc, _ = lab
    free = {"mb": 50_000}
    node.disk_free_mb = lambda: free["mb"]
    node.disk_check_s = 0.2
    rc.start(plan(hd_file, duration=600))
    time.sleep(2)
    free["mb"] = 150
    assert wait_state(rc, RunState.IDLE, timeout_s=30)
    r = rc.result
    assert r is not None and r["verdict"] == "INCOMPLETE", r
    assert r["verdict_reason"] == "disk_low: local"
    assert not tsp_running() and not player_running()


def test_node_shutdown_stops_a_video_being_made(tmp_path: Path) -> None:
    """20 G-12 / 12 4-3: making a video does not make the node active, yet stopping
    the node (or a new Controller) must stop ffmpeg too."""
    node = Node(tmp_path / "n", "sender", host="127.0.0.1", port=0, local_port=0,
                modules_dir=REPO / "modules")  # fmt: skip
    node.start()
    prep = fast_videos(node, seconds=300)
    prep.request("HD", 10_000_000, [1])
    end = time.monotonic() + 30
    while not (prep.status()["channels"][0]["percent"] or 0) > 0:
        assert time.monotonic() < end
        time.sleep(0.1)
    node.stop()
    assert not [p for p in _ours_alive() if "ffmpeg" in p]
    assert prep.status()["channels"][0]["state"] == "cancelled"
    assert list(prep.folder.iterdir()) == []


def test_failed_start_leaves_the_last_run_alone(lab: Any, hd_file: Path, tmp_path: Path) -> None:
    """20 G-13: a start that fails before its own folder exists must not write into the
    folder of the run before it."""
    node, rc, _ = lab
    last = tmp_path / "reports" / "R-last"
    last.mkdir(parents=True)
    before = '{"run_id": "R-last", "status": "finished"}'
    (last / "run-state.json").write_text(before)
    rc._run_dir = last  # what the previous run left behind
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    fast_videos(node).ffmpeg = str(fake)
    with pytest.raises(ZlError):
        rc.start(plan(hd_file, files={}))
    assert (last / "run-state.json").read_text() == before


# ---------------------------------------------------------------- F17 checklist


def steps_of(c: dict[str, Any]) -> list[tuple[int, str]]:
    return [(s["n"], s["state"]) for s in c["steps"]]


def test_start_fills_the_whole_checklist(lab: Any, hd_file: Path) -> None:
    _, rc, _ = lab
    rc.start(plan(hd_file, duration=600))
    c = rc.status()["checklist"]
    assert c["kind"] == "start" and c["ok"] and c["run_id"] == rc.run_id
    # step 4: the first Start finds no decoding sample yet: a warning, never a stop
    assert steps_of(c) == [(n, "warn" if n == 4 else "ok") for n in range(1, 8)]
    by = {s["n"]: s for s in c["steps"]}
    assert by[4]["code"] == "ZL-RCV-015" and by[4]["what"] == "Measuring the receiver PC"
    assert "tsp 3." in by[2]["detail"] and by[3]["detail"].startswith("links ok")
    assert by[5]["detail"].startswith("packets: local ")
    rc.stop()


def test_light_check_sends_nothing_and_changes_no_run(lab: Any, hd_file: Path) -> None:
    node, rc, _ = lab
    before = (rc.state, rc.generation, rc.run_id, rc.status()["devices"])
    c = rc.precheck(plan(hd_file), full=False)
    assert c["kind"] == "idle" and c["ok"]
    assert steps_of(c) == [(1, "ok"), (2, "ok"), (3, "ok"), (4, "ok"), (5, "skipped"),
                           (6, "skipped"), (7, "skipped")]  # fmt: skip
    assert (rc.state, rc.generation, rc.run_id, rc.status()["devices"]) == before
    assert rc.status()["precheck"] == c and rc.status()["checklist"] is None
    assert not tsp_running() and not list(rc.reports_dir.glob("R*"))  # no run folder
    assert not list((node.data_dir / "logs").glob("tsp_probe*"))  # no test send
    full = rc.precheck(plan(hd_file), full=True)
    assert full["kind"] == "precheck" and full["ok"]
    states = [s for n, s in steps_of(full)][:5]  # 4: the PC speed sample is being made
    assert states == ["ok", "ok", "ok", "warn", "ok"] and not tsp_running()
    assert rc.status()["checklist"] == full and rc.state is RunState.IDLE


def test_missing_tool_fails_step_2_ins_029(lab: Any, hd_file: Path) -> None:
    node, rc, _ = lab
    real = node.tools.find_all
    node.tools.find_all = lambda names: {**real(names), "tsp": None}
    with pytest.raises(ZlError) as e:
        rc.start(plan(hd_file))
    assert e.value.code == "ZL-INS-029" and "tsp" in e.value.what
    c = rc.status()["checklist"]
    assert steps_of(c)[:3] == [(1, "ok"), (2, "failed"), (3, "skipped")]
    assert c["steps"][1]["device"] == "local" and c["steps"][1]["fix"]
    assert rc.state is RunState.IDLE and not tsp_running()
    light = rc.precheck(plan(hd_file), full=False)
    assert not light["ok"] and light["steps"][1]["code"] == "ZL-INS-029"


def test_low_disk_fails_step_4_with_the_longest_possible_test(lab: Any, hd_file: Path) -> None:
    from core.blocks.resource_monitor import Resources

    node, rc, _ = lab
    node.resources.read = lambda: Resources(1.0, 50.0, 1100, 10.0)
    c = rc.precheck(plan(hd_file, duration=100 * 3600), full=False)
    s = c["steps"][3]
    assert s["state"] == "failed" and s["code"] == "ZL-RCV-008" and "1100 MB" in s["what"]
    assert "at most " in s["fix"] and not c["ok"]
    assert rc.precheck(plan(hd_file, duration=60), full=False)["ok"]  # a short one fits


def test_other_build_or_protocol_fails_step_1(lab: Any, hd_file: Path) -> None:
    _, rc, _ = lab
    rc.build = "20990101.0000"
    c = rc.precheck(plan(hd_file), full=False)
    assert c["steps"][0]["code"] == "ZL-INS-015" and c["steps"][0]["device"] == "local"
    rc.build = ""
    c = rc.precheck({**plan(hd_file), "module": "nope"}, full=False)
    assert c["steps"][0]["state"] == "failed" and c["steps"][0]["code"] == "ZL-INS-024"
    c = rc.precheck(plan(hd_file, duration=1), full=False)  # the plan check is step 1 too
    assert c["steps"][0]["code"] == "ZL-RUN-014"


def test_test_send_that_does_not_arrive_fails_step_5_net_003(
    lab: Any, hd_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    from modules.iptv import run as iptv_run

    _, rc, _ = lab
    real = iptv_run.probe_steps

    def elsewhere(p: Any) -> Any:
        listen, send, result = real(p)
        moved = [dataclasses.replace(s, params={**s.params, "group": "239.255.99.99"})
                 for s in listen]  # fmt: skip
        return [moved, send, result]

    monkeypatch.setattr(iptv_run.IptvRun, "probe_steps", staticmethod(elsewhere))
    with pytest.raises(ZlError) as e:
        rc.start(plan(hd_file))
    assert e.value.code == "ZL-NET-003"
    c = rc.status()["checklist"]
    assert steps_of(c)[4:] == [(5, "failed"), (6, "skipped"), (7, "skipped")]
    assert rc.state is RunState.IDLE and not tsp_running()


def test_checks_are_refused_during_a_test(lab: Any, hd_file: Path) -> None:
    _, rc, _ = lab
    rc.start(plan(hd_file, duration=600))
    with pytest.raises(ZlError) as e:
        rc.precheck(plan(hd_file), full=False)
    assert e.value.code == "ZL-RUN-012"
    rc.stop()


# ---------------------------------------------------------------- Wi-Fi receiver PC


def fake_wifi(node: Node, tmp: Path, connected: str | None = None,
              ip: str = "127.0.0.1") -> list[list[str]]:  # fmt: skip
    """The node's Wi-Fi as a fake NetworkManager whose Wi-Fi address is 127.0.0.1, so
    the real test stream still runs on loopback. Returns the commands it got."""
    from core.blocks.wifi_join import WifiJoin

    root = tmp / "net"
    (root / "wlp9s0" / "wireless").mkdir(parents=True)
    (root / "wlp9s0" / "address").write_text("aa:bb:cc:00:11:22\n")
    state = {"ssid": connected, "pending": None}
    calls: list[list[str]] = []

    def run(argv: list[str], timeout_s: float) -> tuple[int, str]:
        calls.append(list(argv))
        if argv[:2] == ["iw", "dev"]:
            return 0, f"\tSSID: {state['ssid']}\n" if state["ssid"] else ""
        if "add" in argv:
            state["pending"] = argv[argv.index("ssid") + 1]
        if "up" in argv:
            state["ssid"] = state["pending"]
        return 0, ""

    node.wifi = WifiJoin(run, root, lambda n: ip if state["ssid"] else None, tmp,
                         sleep=lambda s: None)  # fmt: skip
    return calls


def test_a_plans_wifi_card_is_not_joined_the_address_in_use_is_the_test_card(
    lab: Any, hd_file: Path, tmp_path: Path
) -> None:
    """2026-10-17 user: the address picked or typed in a PC's row is used as it is, for
    control and for the test; no card is guessed and no Wi-Fi is joined by the tool (it
    was joined to S-51 before). A plan's ``nics: "wifi"`` therefore changes nothing."""
    from controller.wifi_store import WifiConfig

    node, rc, _ = lab
    calls = fake_wifi(node, tmp_path, connected="HomeNet")
    rc._wifi = lambda: WifiConfig("ZL-TEST", "WPA2", "Zl-test-pass-9")
    p = plan(hd_file, duration=600, nics={"local": "wifi"})
    c = rc.precheck(p, full=True)
    assert c["ok"], c
    assert not any("up" in x or "add" in x for x in calls)  # nothing joined or switched
    assert "S-51" not in str(c["steps"][2])


def test_a_card_already_on_the_router_under_test_is_left_on_it(
    lab: Any, hd_file: Path, tmp_path: Path
) -> None:
    """GAP-159 (2026-10-15): S-51 still held the old router's SSID; the start knocked the
    T460's test card off the new router's Wi-Fi to join it and failed. A card on the LAN
    of the address the Controller uses for the PC is the router under test: kept as it is."""
    from controller.wifi_store import WifiConfig

    node, rc, _ = lab
    calls = fake_wifi(node, tmp_path, connected="AP_NEW", ip="127.0.0.2")
    rc._wifi = lambda: WifiConfig("SK_OLD_ROUTER", "Open", "")
    c = rc.precheck(plan(hd_file, nics={"local": "wifi"}), full=False)
    s3 = c["steps"][2]
    assert "stays on 'AP_NEW'" in s3["what"] and "SK_OLD_ROUTER" in s3["why"]
    assert not any("up" in x or "add" in x for x in calls)  # nothing switched


def test_the_card_the_pc_is_reached_at_is_kept_when_its_cable_shares_the_lan(
    lab: Any, hd_file: Path, tmp_path: Path
) -> None:
    """2026-10-16: t460-rx's address was its Wi-Fi card on the new router (picked in the
    IP box); that card was skipped as "the address itself" and the start joined the old
    SSID, knocking it off. Its cable on the same LAN says it is the router under test."""
    from controller.wifi_store import WifiConfig

    node, rc, _ = lab
    calls = fake_wifi(node, tmp_path, connected="AP_NEW", ip="127.0.0.1")  # == the host
    rc.devices.cards["local"] = [{"nic": "eth0", "ip": "127.0.0.9"}, {"nic": "wlp9s0"}]
    rc._wifi = lambda: WifiConfig("SK_OLD_ROUTER", "Open", "")
    c = rc.precheck(plan(hd_file, nics={"local": "wifi"}), full=False)
    assert "stays on 'AP_NEW'" in c["steps"][2]["what"], c["steps"][2]
    assert not any("up" in x or "add" in x for x in calls)  # nothing switched


def test_wifi_join_refused_during_a_test(lab: Any, hd_file: Path) -> None:
    node, rc, _ = lab
    rc.start(plan(hd_file, duration=600))
    c = NodeClient("127.0.0.1", node.api.port, rc.boot_id)
    with pytest.raises(ZlError) as e:
        c.command("/v1/wifi/join", rc.generation, None, ssid="x", security="Open")
    assert e.value.code == "ZL-RUN-012"
    c.close()
    rc.stop()


# ---------------------------------------------------------------- primed mode (iptv 4.5)


def primed_plan(hd_file: Path, prime: str, playback: str = "Off", **over: Any) -> dict[str, Any]:
    p = plan(hd_file, **over)
    p["settings"] = {**p["settings"], "S-18": prime, "S-49": playback}
    return p


def test_primed_run_sends_only_after_the_prime(lab: Any, hd_file: Path) -> None:
    """S-18 On: the test send and the test itself run primed; the report says so."""
    _, rc, _ = lab
    rc.start(primed_plan(hd_file, "On", duration=600))
    s5 = rc.status()["checklist"]["steps"][4]
    assert s5["state"] == "ok" and "primed unicast" in s5["detail"], s5
    time.sleep(3)
    row = rc.status()["devices"]["local"]["module_data"]
    tx = row["sender"]["channels"][0]
    assert tx["prime"]["dest"].startswith("127.0.0.1:") and tx["tx_bitrate_bps"]
    r = rc.stop()
    assert r["dut"]["multicast_path"] == "primed" and r["dut"]["mode_detected"] == "Bridge"
    assert r["devices"][0]["loss_total"] == 0 and not tsp_running()


def test_auto_falls_back_to_primed_behind_a_nat(
    lab: Any, hd_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-18 Auto: multicast reaches nobody and a NAT sits between -> primed (iptv 4.5)."""
    import dataclasses

    from controller import run as controller_run
    from modules.iptv import run as iptv_run

    _, rc, events = lab
    monkeypatch.setattr(controller_run, "own_address", lambda h, p: "10.9.9.9")  # NAT seen
    real = iptv_run.probe_steps

    def no_multicast(p: Any) -> Any:  # the router drops multicast (no IGMP proxy)
        listen, send, result = real(p)
        if not p.primed:
            listen = [dataclasses.replace(s, params={**s.params, "group": "239.255.99.98"})
                      for s in listen]  # fmt: skip
        return [listen, send, result]

    monkeypatch.setattr(iptv_run.IptvRun, "probe_steps", staticmethod(no_multicast))
    rc.start(primed_plan(hd_file, "Auto", duration=600))
    s5 = rc.status()["checklist"]["steps"][4]
    assert "no multicast came through" in s5["detail"], s5
    assert any("trying primed" in e.text for e in events)
    r = rc.stop()
    assert r["dut"]["multicast_path"] == "primed" and r["dut"]["mode_detected"] == "NAT"
    assert r["dut"]["wan_ip"] == "127.0.0.1" and r["dut"]["wan_subnet"] == "127.0.0.0/8"


def test_auto_without_a_nat_does_not_prime(
    lab: Any, hd_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    from modules.iptv import run as iptv_run

    _, rc, _ = lab
    real = iptv_run.probe_steps

    def no_multicast(p: Any) -> Any:
        listen, send, result = real(p)
        moved = [dataclasses.replace(s, params={**s.params, "group": "239.255.99.97"})
                 for s in listen]  # fmt: skip
        return [moved, send, result]

    monkeypatch.setattr(iptv_run.IptvRun, "probe_steps", staticmethod(no_multicast))
    with pytest.raises(ZlError) as e:
        rc.start(primed_plan(hd_file, "Auto"))
    assert e.value.code == "ZL-NET-003" and not tsp_running()


def test_primed_with_playback_needs_the_raw_prime(lab: Any, hd_file: Path) -> None:
    """The player's prime leaves from its port by a raw socket (CAP_NET_RAW): without
    the right the start fails cleanly with RCV-024; with it the test runs."""
    import socket as s

    try:
        s.socket(s.AF_INET, s.SOCK_RAW, s.IPPROTO_RAW).close()
        allowed = True
    except PermissionError:
        allowed = False
    _, rc, _ = lab
    if allowed:
        rc.start(primed_plan(hd_file, "On", playback="On", duration=600))
        rc.stop()
    else:
        with pytest.raises(ZlError) as e:
            rc.start(primed_plan(hd_file, "On", playback="On"))
        assert e.value.code == "ZL-RCV-024"
    assert rc.state is RunState.IDLE and not tsp_running() and not player_running()


def test_router_wan_outside_the_sender_subnet_is_snd_010(lab: Any) -> None:
    """07 3.3: behind the NAT the router WAN IP must be in the sender PC's subnet."""
    from controller.precheck import DeviceError

    _, rc, _ = lab
    rc._same_subnet("tx", "10.0.0.1", {"reached_ip": "10.0.0.2", "reached_prefix": 24})
    assert rc._wan_subnet == "10.0.0.0/24"
    with pytest.raises(DeviceError) as e:
        rc._same_subnet("tx", "10.0.1.1", {"reached_ip": "10.0.0.2", "reached_prefix": 24})
    assert e.value.code == "ZL-SND-010" and e.value.device == "tx" and "10.0.0.0/24" in e.value.what
    rc._same_subnet("tx", "10.9.9.9", {"reached_ip": None, "reached_prefix": None})  # unknown


def test_pc_speed_check_makes_its_sample_once_then_measures(lab: Any, hd_file: Path) -> None:
    """05 4.2-1 / RCV-015: the receiver PC makes the 10 s sample itself (never through
    the Controller), then decodes it; the idle check only reads the kept result."""
    node, rc, _ = lab
    p = plan(hd_file)
    first = rc.precheck(p, full=True)["steps"][3]
    assert first["state"] == "warn" and first["what"] == "Measuring the receiver PC"
    iptv = next(m.instance for m in node.modules if m.id == "iptv")
    end = time.monotonic() + 240
    while iptv.perf.busy() and time.monotonic() < end:
        time.sleep(0.5)
    sample = iptv.perf.sample("HD", 10_000_000)
    assert sample.exists() and sample.stat().st_size > 1_000_000
    c = rc.precheck(p, full=True)
    s4 = c["steps"][3]
    assert c["ok"] and "decodes at" in s4["detail"], s4
    light = rc.precheck(p, full=False)["steps"][3]  # the idle check: kept result only
    assert "decodes at" in light["detail"]
    assert not list(node.procs_dir.glob("perf_*.json"))  # nothing left running


def test_player_gives_way_when_the_pc_is_saturated(lab: Any, hd_file: Path) -> None:
    """05 4.2-1 / RCV-016: CPU >= 90% for more than 10 s -> the player is niced once."""
    import os

    from core.blocks.resource_monitor import Resources

    node, rc, events = lab
    node.resources.read = lambda: Resources(97.0, 50.0, 50_000, 10.0)  # saturated PC
    rc.start(plan(hd_file, duration=600))
    iptv = next(m.instance for m in node.modules if m.id == "iptv")
    rx = next(iter(iptv.receivers.values()))
    end = time.monotonic() + 20
    while rx.protected_at is None and time.monotonic() < end:
        time.sleep(0.2)
    assert rx.protected_at is not None and rx.player is not None
    pid = rx.player._runner.pid
    assert os.getpriority(os.PRIO_PROCESS, pid) >= 10
    end = time.monotonic() + 5  # node events reach the Controller with the next heartbeat
    while not [e for e in events if e.code == "ZL-RCV-016"] and time.monotonic() < end:
        time.sleep(0.2)
    assert [e for e in events if e.code == "ZL-RCV-016"]
    rc.stop()


# ---------------------------------------------------------------- S-48 user videos (iptv 1-1)


def user_video(folder: Path, name: str, audio: bool = False) -> Path:
    """A short small video like one a user drops into the media folder."""
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / name
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25"]  # fmt: skip
    if audio:
        argv += ["-f", "lavfi", "-i", "sine=frequency=440"]
    argv += ["-t", "4", "-c:v", "libx264", "-preset", "ultrafast"]
    argv += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    argv.append(str(out))
    subprocess.run(argv, check=True, timeout=120)
    return out


def test_user_video_is_made_for_sending_then_used(lab: Any, hd_file: Path) -> None:
    """S-48: a file in the media folder (Korean name, no sound, smaller than HD) is made
    into the channel's video once (upscaled, channel tone added) and sent."""
    node, rc, _ = lab
    iptv = iptv_of(node)
    fast_videos(node)
    name = "시험 영상 (1).mp4"
    src = user_video(iptv.media_dir, name)
    p = plan(hd_file, files={})
    p["settings"] = {**p["settings"], "S-48": {"1": name}}
    run_id = rc.start(p)
    ch = rc.status()["devices"]["local"]["module_data"]["sender"]["channels"][0]
    assert ch["source"] == "media", ch
    made = iptv.preparer.ready("HD", 1, 10_000_000, src)
    assert made is not None and made.name.startswith("ch1_HD_10M_f")
    desc = json.loads(made.with_suffix(".json").read_text())
    assert (
        desc["source_name"] == name
        and desc["upscaled"] is True
        and desc["source_size"] == [640, 360]
    )
    r = rc.stop()
    assert r["run_id"] == run_id and r["source"] == {"1": name} and r["source_fallback"] == []
    listed = iptv.sources(None)["files"]
    assert [f["name"] for f in listed] == [name] and listed[0]["fits"]["HD"]["upscaled"]


def test_missing_user_video_falls_back_to_generated(lab: Any, hd_file: Path) -> None:
    node, rc, events = lab
    fast_videos(node)
    p = plan(hd_file, files={})
    p["settings"] = {**p["settings"], "S-48": {"1": "gone.mp4"}}
    rc.start(p)
    ch = rc.status()["devices"]["local"]["module_data"]["sender"]["channels"][0]
    assert ch["source"] == "generated_fallback"
    r = rc.stop()
    assert r["source"] == {"1": "gone.mp4"} and r["source_fallback"] == [1]
    assert any(e.code == "ZL-SND-001" for e in events)
    for bad in ({"1": "../x.mp4"}, {"1": "a/b.mp4"}, {"9": "x.mp4"}, "movie.mp4"):
        q = plan(hd_file)
        q["settings"] = {**q["settings"], "S-48": bad}
        assert rc.precheck(q, full=False)["steps"][0]["code"] == "ZL-RUN-014", bad


def test_a_new_controller_goes_above_an_idle_nodes_generation(lab: Any, hd_file: Path) -> None:
    """GAP-111 (real case: the Windows exe started with fresh data at generation 2 while
    the nodes were at 11, so every command was DSC-011): step 1 takes the count over."""
    node, rc, _ = lab
    node.guard.generation = before = rc.generation + 9
    r = rc.precheck(plan(hd_file), full=False)
    assert r["steps"][0]["state"] == "ok"
    assert rc.generation == before + 1  # the node then takes the new count from commands
    assert node.guard.generation <= rc.generation
    assert int((rc.reports_dir / ".generation").read_text()) == rc.generation  # kept
    node.guard.generation = now = rc.generation  # it took our count: nothing to take over
    rc.precheck(plan(hd_file), full=False)
    assert rc.generation == now


def test_a_pc_that_may_sleep_is_a_warning_sys_005(lab: Any, hd_file: Path) -> None:
    """09 SYS-005: F17 step 4 warns (never stops) when a node cannot hold off sleep."""
    node, rc, _ = lab
    node.sleep_guard.state, node.sleep_guard.why = "unavailable", "Access denied"
    r = rc.precheck(plan(hd_file), full=False)
    step4 = r["steps"][3]
    assert r["ok"] is True and step4["state"] == "warn" and step4["code"] == "ZL-SYS-005"
    assert step4["device"] == "local" and step4["why"] == "Access denied"
    node.sleep_guard.state, node.sleep_guard.why = "off", "sleep is turned off on this PC"
    step4 = rc.precheck(plan(hd_file), full=False)["steps"][3]
    assert step4["state"] == "ok" and step4["code"] is None  # GAP-107: cannot sleep = fine


def test_a_killed_sender_is_the_tools_fault_not_the_routers(lab: Any, hd_file: Path) -> None:
    """05 5.3 / GAP-68: tsp dies mid-test (SND-005) -> what the receiver lost then is
    purple (tool), never red (router). Only the window, so the verdict is INCOMPLETE."""
    _, rc, events = lab
    run_id = rc.start(plan(hd_file, duration=20))
    time.sleep(4)
    pid = int(subprocess.run(["pgrep", "-x", "tsp"], capture_output=True, text=True)
              .stdout.split()[0])  # fmt: skip
    os.kill(pid, signal.SIGKILL)
    assert wait_state(rc, RunState.IDLE, 60)
    r = rc.result
    assert r is not None
    assert any(e.code == "ZL-SND-005" for e in events)
    lines = [json.loads(x) for x in (rc.reports_dir / run_id / "summaries.jsonl")
             .read_text().splitlines()]  # fmt: skip
    trouble = [x["trouble"] for x in lines if x.get("trouble")]
    assert trouble and trouble[0]["sender_s"] > 0, lines
    # the restart really hurt the stream (here CC/PAT/PMT errors; before GAP-68 -> red)
    assert sum(x["sender_lost"] + x["sender_p1"] for x in trouble) > 0, trouble
    colors = r["devices"][0]["colors"]
    assert colors["red"] == 0, r["devices"][0]
    assert r["verdict"] != "FAIL", r["verdict"]


@pytest.mark.parametrize(("free", "ok", "state"), [(8.0, True, "warn"), (4.0, False, "failed")])
def test_low_memory_sys_004(lab: Any, hd_file: Path, monkeypatch: Any, free: float, ok: bool,
                            state: str) -> None:  # fmt: skip
    """09 SYS-004: free memory below 10% warns, below 5% stops the start (F17 step 4)."""
    node, rc, _ = lab
    real = node.resources.read

    def low() -> Any:
        import dataclasses

        return dataclasses.replace(real(), mem_free_pct=free)

    monkeypatch.setattr(node.resources, "read", low)
    r = rc.precheck(plan(hd_file), full=False)
    step4 = r["steps"][3]
    assert r["ok"] is ok and step4["state"] == state and step4["code"] == "ZL-SYS-004"
    assert f"{free:.0f}% free" in step4["what"]


def test_record_that_cannot_be_written_stops_the_test_rcv_009(
    lab: Any, hd_file: Path, monkeypatch: Any
) -> None:
    """09 RCV-009: the record fails and a new file fails too -> the receiver faults and
    the Controller stops the test (file_write_failed), never PASS."""
    from core.records import recorder as rec_mod

    _, rc, events = lab
    rc.start(plan(hd_file, duration=30))
    real = rec_mod.Recorder._write

    def broken(self: Any, obj: dict[str, Any]) -> None:
        if obj.get("type") == "s":
            raise OSError(28, "No space left on device")
        real(self, obj)

    monkeypatch.setattr(rec_mod.Recorder, "_write", broken)
    assert wait_state(rc, RunState.IDLE, 60) or rc.state is RunState.FAULT
    r = rc.result
    assert r is not None and r["verdict"] != "PASS"
    assert r["verdict_reason"] == "file_write_failed: local", r["verdict_reason"]
    codes = [e.code for e in events]
    assert "ZL-RCV-009" in codes and "ZL-RCV-018" not in codes


def test_wan_address_used_twice_is_snd_013(lab: Any, monkeypatch: Any) -> None:
    """09 SND-013: the sender's WAN address answered by another MAC blocks the start;
    "not checked" (no packet socket) never blocks and says why."""
    from controller.precheck import DeviceError
    from controller.run import DeviceWatch
    from core.blocks import arp_probe as ap

    _, rc, _ = lab
    dev = rc.devices.get("local")
    w = DeviceWatch(dev, NodeClient(dev.host, dev.port, rc.boot_id, "local"))
    answers = {"r": ap.ProbeResult(True, ["02:00:00:00:00:09"], "enp0s31f6")}
    monkeypatch.setattr("core.node.arp_probe", lambda ip: answers["r"])
    with pytest.raises(DeviceError) as e:
        rc._no_ip_conflict("local", w, {"reached_ip": "10.0.0.2"})
    assert e.value.code == "ZL-SND-013" and "02:00:00:00:00:09" in e.value.why
    answers["r"] = ap.ProbeResult(True, [], "enp0s31f6")
    assert (
        rc._no_ip_conflict("local", w, {"reached_ip": "10.0.0.2"}) == "10.0.0.2 used by local only"
    )
    answers["r"] = ap.ProbeResult(False, why="cannot open a packet socket")
    assert "not checked" in rc._no_ip_conflict("local", w, {"reached_ip": "10.0.0.2"})
