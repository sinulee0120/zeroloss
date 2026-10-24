"""GAP-153: between tests the Controller asks every device by itself.

2026-10-10 the operator alone could start nothing: the Controller stayed in FAULT from a
Reset All the sender did not answer (it was asked through the wrong NIC), and the
sender's Agent Desktop said "not connected" because nobody asked it within 3 s.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import controller.service as service_mod
from controller.run import RunState
from controller.service import ControllerService
from core.node import Node
from core.protocol.client import NodeClient

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def agent(tmp_path: Path) -> Iterator[Node]:
    n = Node(tmp_path / "agent", "agent", host="127.0.0.1", port=0, local_port=0,
             modules_dir=REPO / "modules", lost_after_s=60)  # fmt: skip
    n.start()
    yield n
    n.stop()


@pytest.fixture
def svc(tmp_path: Path, agent: Node) -> Iterator[ControllerService]:
    s = ControllerService(tmp_path / "ctl", tmp_path / "reports", REPO / "modules",
                          host="127.0.0.1", port=0, local_port=0)  # fmt: skip
    s.start()
    s.devices.add("rx", "127.0.0.1", agent.api.port, "b")
    yield s
    s.stop()


def _unanswered_fault(svc: ControllerService) -> None:
    svc.run._move(RunState.RESETTING)  # as Reset All does
    svc.run._move(RunState.FAULT)
    svc.run.reset_result = {"ok": False, "updated_utc": "2026-10-10T03:30:20.251Z", "devices": [
        {"device": "rx", "answered": False, "clean": False, "checks": [],
         "why": "Cannot reach rx"}]}  # fmt: skip


def test_a_round_asks_each_device_and_keeps_its_desktop_connected(
    svc: ControllerService, agent: Node
) -> None:
    clients: dict[str, NodeClient] = {}
    with ThreadPoolExecutor(2) as pool:
        assert svc._idle_round(clients, pool) == {"rx": True}
    assert agent.desktop_status(None)["controller"]["connected"]  # type: ignore[arg-type]
    for c in clients.values():
        c.close()


def test_a_device_that_does_not_answer_is_false(svc: ControllerService, agent: Node) -> None:
    clients: dict[str, NodeClient] = {}
    agent.stop()
    with ThreadPoolExecutor(2) as pool:
        assert svc._idle_round(clients, pool) == {"rx": False}
    for c in clients.values():
        c.close()


def test_only_a_reset_nobody_answered_is_retried_by_itself(svc: ControllerService) -> None:
    assert not svc._only_unanswered_fault()  # IDLE
    _unanswered_fault(svc)
    assert svc._only_unanswered_fault()
    svc.run.reset_result["devices"][0]["answered"] = True  # type: ignore[index]
    assert not svc._only_unanswered_fault()  # it answered and is not clean: a person looks
    svc.run.reset_result["devices"][0]["answered"] = False  # type: ignore[index]
    svc.run.fault = {"device": "rx", "reason": "x", "at_utc": "2026-10-10T04:00:00.000Z"}
    assert not svc._only_unanswered_fault()  # a test fault came after it


def test_the_fault_clears_once_every_device_answers(
    svc: ControllerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_mod, "IDLE_LINK_S", 0.05)
    _unanswered_fault(svc)
    t = threading.Thread(target=svc._idle_watch, daemon=True)
    t.start()
    end = time.monotonic() + 30
    while svc.run.state is not RunState.IDLE and time.monotonic() < end:
        time.sleep(0.05)
    assert svc.run.state is RunState.IDLE
    assert svc.run.reset_result and svc.run.reset_result["ok"]


def test_a_pc_that_answers_again_closes_its_restart_action(svc: ControllerService) -> None:
    """2026-10-10 user: "still waiting": the start failed with DSC-014 while this PC's
    cable was out; once the PC answers again, the problem and "Restart the PC" close."""
    from core.events.bus import Event

    svc.actions._grace = False
    svc.node.bus.publish(Event("ZL-DSC-014", "error", "Start failed: Cannot reach rx",
                               device="rx"))  # fmt: skip
    assert svc.actions.snapshot()["waiting"] == 1
    svc._answers_again({"rx": False})
    assert svc.actions.snapshot()["waiting"] == 1  # still silent: still waiting
    svc._answers_again({"rx": True})
    assert svc.actions.snapshot()["waiting"] == 0
    assert not [p for p in svc.problems.snapshot()["problems"]
                if p["active"] and p["code"].endswith("DSC-014")]  # fmt: skip


def test_a_restart_action_opened_without_a_pc_closes_when_all_answer(
    svc: ControllerService,
) -> None:
    """2026-10-10: "Start failed: Cannot reach t460-rx" opened "Restart the PC" with no
    PC; it stayed Waiting. It closes once every PC answers."""
    from core.events.bus import Event

    svc.actions._grace = False
    svc.node.bus.publish(Event("ZL-DSC-014", "error", "Start failed: Cannot reach rx"))
    assert svc.actions.snapshot()["waiting"] == 1
    svc._answers_again({"rx": False})
    assert svc.actions.snapshot()["waiting"] == 1
    svc._answers_again({"rx": True})
    assert svc.actions.snapshot()["waiting"] == 0


def test_records_a_silent_pc_still_owes_are_fetched_when_it_answers(
    svc: ControllerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GAP-156: 10-11 the receiver did not answer at the end of the run; its records and
    the cause analysis never came. They are fetched once it answers again."""
    asked: list[list[str]] = []
    answers = [{"files": [], "missing": {"rx": "ZL-DSC-014 Cannot reach rx"}},
               {"files": [{"device": "rx"}], "missing": {}}]  # fmt: skip

    def collect(reports: object, run: str, devices: list[object], boot: str,
                stopping: object = None) -> dict[str, object]:  # fmt: skip
        asked.append([d.name for d in devices])  # type: ignore[attr-defined]
        return {"run": run, "files": [], **answers[len(asked) - 1]}  # type: ignore[dict-item]

    monkeypatch.setattr(service_mod, "collect_records", collect)
    monkeypatch.setattr(service_mod, "reanalyze", lambda *a: True)
    svc._collect("R1", ["rx"])
    assert svc._owed == {"R1": {"rx"}}
    svc._fetch_owed({"rx": False})  # still silent: nothing asked
    assert len(asked) == 1
    svc._fetch_owed({"rx": True})
    assert svc._records_thread is not None
    svc._records_thread.join(5)
    assert asked == [["rx"], ["rx"]] and svc._owed == {}


def test_reset_all_brings_back_a_node_that_was_shut_down(
    svc: ControllerService, agent: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-17 user: "shutdown all도 안되고 reset all도 안되고 이게 리셋이냐 바로 deinit든
    재시작이 되게 깔끔하게": Shut Down All had left the ProBook's node off and Reset All only
    reported it. Now the PC's Deployer starts (or restarts) it and the reset goes on."""
    asked: list[str] = []
    again: list[Node] = []

    def revive(name: str) -> str:  # what the Deployer's "start" does: the node runs again
        asked.append(name)
        n = Node(agent.data_dir, "agent", host="127.0.0.1", port=0, local_port=0,
                 modules_dir=REPO / "modules", lost_after_s=60)  # fmt: skip
        n.start()
        again.append(n)
        dev = svc.devices.get(name)
        svc.devices.move(dev.device_id, "127.0.0.1", n.api.port)
        return "start"

    agent.stop()
    svc.run.revive = revive
    try:
        r = svc.run.reset()
        assert asked == ["rx"] and r["ok"], r
        assert r["devices"][0]["revived"] == "start" and svc.run.state is RunState.IDLE
    finally:
        for n in again:
            n.stop()


def test_a_test_with_nothing_to_make_ahead_says_so(svc: ControllerService) -> None:
    """2026-10-17: with Throughput · All picked, the screen asked to prepare and got
    SYS-008 ("'ThroughputRun' object has no attribute 'prepare_ahead'")."""
    got = svc.run.prepare_ahead({"module": "throughput", "sender": "rx", "quality": "UHD"})
    assert got == {"needed": False, "why": "this test makes nothing ahead"}


def test_reset_all_reaches_a_pc_on_its_other_card(svc: ControllerService, agent: Node) -> None:
    """2026-10-17: the sender's WAN card (the address in use) had no link and its node
    still ran an old test, which kept that card down; it answered on its home Wi-Fi.
    Reset All resets it there instead of reporting "did not answer"."""
    dev = svc.devices.get("rx")
    svc.devices.move(dev.device_id, "127.0.0.2", agent.api.port)  # nothing answers there
    svc.devices.cards["rx"] = [{"nic": "wlo1", "ip": "127.0.0.1"}]
    svc.run.revive = None
    r = svc.run.reset()
    assert r["ok"] and r["devices"][0]["answered"], r


def test_this_pcs_own_node_does_not_echo_the_buses_events(svc: ControllerService) -> None:
    """2026-10-17: this PC's node shares the Controller's bus; its heartbeat carried every
    PC's warnings back and they were published again as "this-pc" (count 43,433)."""
    from controller.run import DeviceWatch

    assert svc.run.own is not None
    me, _ = svc.run.own()
    got: list[str] = []
    svc.node.bus.subscribe(lambda e: got.append(f"{e.device}|{e.code}"))
    w = DeviceWatch(me, NodeClient("127.0.0.1", 1, "b", me.name))
    svc.run._forward_events(w, [
        {"code": "ZL-NET-014", "level": "warn", "text": "probook ↔ router link changed",
         "device": "probook-sender", "data": {"from_node": True}},
        {"code": "ZL-NET-015", "level": "error", "text": "Link eth0 went down", "data": {}},
    ])  # fmt: skip
    assert got == []


def test_a_band_password_goes_only_to_the_join_and_an_empty_one_means_none(
    svc: ControllerService,
) -> None:
    """2026-10-17 user: "암호가 있으면 암호도 넣게 해", "빈칸이라고 또 테스트 못하게 하지 말고
    ... 빈칸이면 암호 없음을 의미"."""
    svc.band_passwords.save("R-5G", "secret-pass-1")
    run = svc.run
    with_pw = run._with_password({"band": "5GHz", "ssid": "R-5G", "security": "WPA2"})
    assert with_pw["password"] == "secret-pass-1" and with_pw["security"] == "WPA2"
    none = run._with_password({"band": "2.4GHz", "ssid": "R-2G", "security": "WPA2"})
    assert none["security"] == "Open" and "password" not in none  # no box filled: no password
    open_net = {"ssid": "x", "security": "Open"}
    assert run._with_password(open_net) == open_net
    assert "R-5G" in svc.band_passwords.ssids()
    assert "secret" not in str(svc.setup_get(None))  # type: ignore[arg-type]
    svc.band_passwords.save("R-5G", "")
    assert svc.band_passwords.secret("R-5G") is None
