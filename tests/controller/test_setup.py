"""Screen choice (F14, 02 2-11): kept, only changed settings saved, roles found by
themselves (06 10.4: the WAN side PC sends), the same plan shape as a plan file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from controller.setup import SetupStore, plan_of, roles
from core.protocol.envelope import ZlError

DEFAULTS = {"S-03": "UHD", "S-05": 3600, "S-15": 16, "S-49": "On"}


def test_store_keeps_the_choice_and_only_changed_settings(tmp_path: Path) -> None:
    s = SetupStore(tmp_path / "controller.json", DEFAULTS)
    assert s.get()["quality"] == "UHD" and s.get()["duration_s"] == 3600
    s.update({"quality": "HD", "settings": {"S-15": 16, "S-49": "Off"}})
    again = SetupStore(tmp_path / "controller.json", DEFAULTS).get()  # next start (F14 rule 4)
    assert again["quality"] == "HD" and again["settings"] == {"S-49": "Off"}
    with pytest.raises(ZlError):
        s.update({"nonsense": 1})


def test_main_screen_settings_go_to_their_fields(tmp_path: Path) -> None:
    """GAP-99 (first real run): `setup set S-03=HD S-05=120` said Saved, the run used
    UHD 1 h. The four main-screen settings are fields of the choice."""
    s = SetupStore(tmp_path / "controller.json", DEFAULTS)
    got = s.update({"settings": {"S-03": "HD", "S-05": 120, "S-06": "Off",
                                 "S-01": "IPTV test", "S-49": "Off"}})  # fmt: skip
    assert (got["quality"], got["duration_s"], got["send_quality_reports"]) == ("HD", 120, False)
    assert got["test_type"] == "IPTV test" and got["settings"] == {"S-49": "Off"}
    plan = plan_of(got, {"sender": "tx", "receivers": [{"device": "rx", "ch": 1}]})
    assert (plan["quality"], plan["duration_s"]) == ("HD", 120)


def test_roles_are_only_what_the_person_chose() -> None:
    """2026-10-17 user: "니가 판단하지말고" -- no PC is made sender or receiver by its side."""
    found = {"tx": {"side": "wan"}, "rx": {"side": "lan"}, "old": {"side": "unknown"}}
    r = roles({"sender": None, "receivers": []}, found)
    assert r["sender"] is None and r["receivers"] == [] and "Choose the sender" in r["why"]
    chosen = roles({"sender": "tx", "receivers": [{"device": "rx", "ch": 1, "network": "Wired"}]},
                   found)  # fmt: skip
    assert chosen["sender"] == "tx" and chosen["why"] is None and not chosen["lan_sender"]
    no_rx = roles({"sender": "tx", "receivers": []}, found)
    assert "Choose the receiver" in no_rx["why"]
    lan = roles({"sender": "rx2", "receivers": [{"device": "rx", "ch": 2}]},
                {"rx": {"side": "lan"}, "rx2": {"side": "lan"}})  # fmt: skip
    assert lan["sender"] == "rx2" and lan["lan_sender"] and lan["why"] is None  # comparison
    gone = roles({"sender": "removed", "receivers": [{"device": "removed", "ch": 1}]}, found)
    assert gone["sender"] is None and gone["receivers"] == []


def test_plan_has_the_plan_file_shape() -> None:
    setup = {"module": "iptv", "test_type": "PC receiver test", "quality": "HD",
             "duration_s": 600, "send_quality_reports": True,
             "settings": {"S-49": "Off"}}  # fmt: skip
    chosen = {"sender": "tx", "receivers": [{"device": "rx", "ch": 2, "network": "Wireless"}]}
    p = plan_of(setup, chosen)
    assert p == {"schema_version": 1, "module": "iptv", "test_type": "pc",
                 "devices": {"sender": "tx", "receivers": [{"device": "rx", "ch": 2}]},
                 "quality": "HD", "duration_s": 600, "send_quality_reports": True,
                 "settings": {"S-49": "Off"}}  # fmt: skip
    rx = {"device": "rx", "ch": 2, "network": "Wireless", "buffer_ms": 2000}
    chosen = {"sender": "tx", "receivers": [rx]}  # its own player buffer (2026-10-16)
    assert plan_of(setup, chosen)["devices"]["receivers"] == [
        {"device": "rx", "ch": 2, "buffer_ms": 2000}]  # fmt: skip


def test_a_card_the_person_picked_goes_into_the_plan(tmp_path: Path) -> None:
    """DSC-006 / GAP-115: when Auto cannot tell the test card (or picks the wrong one) the
    person picks it on the device row; Auto ("") is not kept; a Wi-Fi receiver keeps "wifi";
    a PC that is not in the test brings no card into the plan. 2026-10-17: Wireless joins
    nothing by itself -- the address picked for the PC is its test card ("wifi" is gone)."""
    s = SetupStore(tmp_path / "controller.json", DEFAULTS)
    got = s.update({"nics": {"tx": "10.0.0.1", "rx": "10.0.0.100", "spare": "1.2.3.4", "x": ""}})
    assert got["nics"] == {"tx": "10.0.0.1", "rx": "10.0.0.100", "spare": "1.2.3.4"}
    assert SetupStore(tmp_path / "controller.json", DEFAULTS).get()["nics"] == got["nics"]
    wired = plan_of(got, {"sender": "tx", "receivers": [{"device": "rx", "ch": 1}]})
    assert wired["nics"] == {"tx": "10.0.0.1", "rx": "10.0.0.100"}
    rx_wifi = [{"device": "rx", "ch": 1, "network": "Wireless"}]
    wifi = plan_of(got, {"sender": "tx", "receivers": rx_wifi})
    assert wifi["nics"] == {"tx": "10.0.0.1", "rx": "10.0.0.100"}
    assert "nics" not in plan_of({}, {"sender": "tx", "receivers": [{"device": "rx", "ch": 1}]})
    # 2026-11-01: Wireless with a wired address -> its Wi-Fi card on the same router network
    # (made-up addresses); another network's Wi-Fi (the internet one) is not taken
    found = {"rx": {"labels": {"10.0.0.100": "LAN · eth0", "10.0.0.87": "Wi-Fi · wlx1",
                               "192.168.0.13": "Wi-Fi · wlan0"}}}  # fmt: skip
    wifi = plan_of(got, {"sender": "tx", "receivers": rx_wifi}, found)
    assert wifi["nics"] == {"tx": "10.0.0.1", "rx": "10.0.0.87"}
    found["rx"]["labels"].pop("10.0.0.87")
    assert plan_of(got, {"sender": "tx", "receivers": rx_wifi}, found)["nics"]["rx"] == "10.0.0.100"


def test_no_wan_side_pc_asks_to_move_the_cable_once_and_closes_itself() -> None:
    """20 W-2 (07 3.2 B): the automatic ISP role cannot ask for its own cable. No PC on
    the router WAN side and no sender chosen -> one Action Required "move the sender
    cable" (SND-022); a PC on the WAN side later -> it closes by itself."""
    from types import SimpleNamespace

    from controller.actions import ActionBoard
    from controller.problems import ProblemBoard
    from controller.service import ControllerService
    from core.events.bus import EventBus

    bus = EventBus()
    actions, problems = ActionBoard(), ProblemBoard()
    bus.subscribe(actions)
    bus.subscribe(problems)
    seen: list[str] = []
    bus.subscribe(lambda e: seen.append(e.code))
    svc: Any = SimpleNamespace(node=SimpleNamespace(bus=bus), _no_wan_told=False)
    notice = ControllerService._wan_side_notice
    lan_only = {"tx-pc": {"reachable": True, "side": "lan"},
                "rx-pc": {"reachable": True, "side": "lan"}}  # fmt: skip
    for _ in range(3):  # the screen asks every 10 s: one notice, not three
        notice(svc, {}, lan_only)
    assert seen == ["ZL-SND-022"]
    (row,) = actions.snapshot()["actions"]
    assert row["action_id"] == "human.cable.move_sender_to_wan" and row["state"] == "waiting"
    moved = {**lan_only, "tx-pc": {"reachable": True, "side": "wan"}}
    notice(svc, {}, moved)
    assert actions.snapshot()["waiting"] == 0 and seen[-1] == "ZL-INFO"
    assert not [p for p in problems.snapshot()["problems"] if p.get("active")]
    notice(svc, {"sender": "tx-pc"}, lan_only)  # a LAN sender chosen on purpose
    notice(svc, {}, {})  # nothing registered yet: another matter
    assert seen.count("ZL-SND-022") == 1


def test_a_choice_runs_only_what_the_action_offered() -> None:
    """20 W-1: pressing a card = `devices wan <device> --on --nic <card>`; anything not
    offered is refused; the action is done when the node took it, else it stays."""
    from types import SimpleNamespace

    from controller.actions import ActionBoard
    from controller.service import ControllerService
    from core.events.bus import Event
    from core.protocol.envelope import Request

    board = ActionBoard()
    cards = [{"id": "enp1s0"}, {"id": "enp2s0"}]
    board(Event("ZL-DSC-006", "warn", "which card?", device="tx-pc",
                data={"choose": "wan_nic", "choices": cards}))  # fmt: skip
    key = board.snapshot()["actions"][0]["key"]
    calls: list[tuple[str, dict[str, Any]]] = []
    answer = {"state": "conflict"}

    def wan(name: str, p: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, p))
        return dict(answer)

    svc: Any = SimpleNamespace(actions=board, _wan=wan)
    choose = ControllerService.action_choose

    def req(**params: Any) -> Request:
        return Request(command_id="c", generation=0, run_id=None, params=params)

    for bad in ({"key": key, "choice": "wlo1"}, {"key": "nope", "choice": "enp1s0"}):
        with pytest.raises(ZlError):
            choose(svc, req(**bad))
    assert calls == []
    got = choose(svc, req(key=key, choice="enp2s0"))  # another DHCP server there: stays open
    assert calls == [("tx-pc", {"on": True, "nic": "enp2s0"})] and got["wan"]["state"] == "conflict"
    assert board.waiting_row(key) is not None
    answer["state"] = "ready"
    choose(svc, req(key=key, choice="enp1s0"))
    assert board.waiting_row(key) is None and board.snapshot()["actions"][0]["done_by"] == "human"


def test_fix_route_choice_changes_this_pcs_card() -> None:
    """NET-019 [Fix] (13 1, GAP-73 5): the offered card is set by nmcli; done by a
    person when it worked; when it did not, NET-019 with how to do it by hand."""
    from types import SimpleNamespace

    from controller.actions import ActionBoard
    from controller.service import ControllerService
    from core.events.bus import Event
    from core.protocol.envelope import Request

    board = ActionBoard()
    board(Event("ZL-NET-019", "warn", "test card carries the default route",
                data={"choose": "test_route", "choices": [{"id": "enp0s31f6"}]}))  # fmt: skip
    key = board.snapshot()["actions"][0]["key"]
    asked: list[str] = []
    result = {"ok": False}

    def fix(nic: str) -> tuple[bool, str]:
        asked.append(nic)
        return result["ok"], "not authorized" if not result["ok"] else f"{nic} fixed"

    svc: Any = SimpleNamespace(actions=board, fix_route=fix)

    def req(**params: Any) -> Request:
        return Request(command_id="c", generation=0, run_id=None, params=params)

    with pytest.raises(ZlError) as e:
        ControllerService.action_choose(svc, req(key=key, choice="enp0s31f6"))
    assert e.value.code == "ZL-NET-019" and "nmcli connection modify" in e.value.fix
    assert board.waiting_row(key) is not None
    result["ok"] = True
    got = ControllerService.action_choose(svc, req(key=key, choice="enp0s31f6"))
    assert got["route"] == "enp0s31f6 fixed" and asked == ["enp0s31f6", "enp0s31f6"]
    assert board.waiting_row(key) is None


def test_each_address_says_wan_lan_or_wifi() -> None:
    """2026-10-17 user: "wan ip, lan ip, wifi ip" -- the IP box says what each address is."""
    from controller.setup import address_labels

    probook: list[dict[str, Any]] = [
        {"nic": "enp37s0", "ip": "10.0.0.1"},
        {"nic": "wlo1", "ip": "192.168.0.27", "wifi": None},
    ]
    t460: list[dict[str, Any]] = [{"nic": "enp0s31f6", "ip": "192.168.45.20"},
            {"nic": "wlx58", "ip": "192.168.45.87", "wifi": {"band": "5GHz"}},
            {"nic": "lo", "ip": "127.0.0.1"}]  # fmt: skip
    assert address_labels(probook, "10.0.0.1") == {"10.0.0.1": "WAN · enp37s0",
                                                   "192.168.0.27": "Wi-Fi · wlo1"}  # fmt: skip
    assert address_labels(t460) == {"192.168.45.20": "LAN · enp0s31f6",
                                    "192.168.45.87": "Wi-Fi · wlx58"}  # fmt: skip


def test_a_choice_saved_before_the_merge_opens_as_the_iptv_test(tmp_path: Path) -> None:
    """2026-10-24 user: one menu; saved "PC receiver test" / "Tablet test" keep their
    receivers and open as the IPTV test, whose plan is a tablet test when a tablet receives."""
    import json

    path = tmp_path / "controller.json"
    t = {"device": "tablet-1", "ch": 2, "host": "rx", "serial": "S1"}
    path.write_text(json.dumps({"schema_version": 1, "setup": {
        "test_type": "Tablet test", "receivers": [{"device": "rx", "ch": 1}, t]}}))  # fmt: skip
    got = SetupStore(path, DEFAULTS).get()
    assert got["test_type"] == "IPTV test" and len(got["receivers"]) == 2
    both = plan_of(got, {"sender": "tx", "receivers": got["receivers"]})
    assert both["test_type"] == "tablet"
    pc_only = plan_of(got, {"sender": "tx", "receivers": [{"device": "rx", "ch": 1}]})
    assert pc_only["test_type"] == "pc"


def test_the_tablets_of_a_test_are_numbered_without_gaps() -> None:
    """2026-10-31 user: "왜 태블릿 한개인데 이름이 tablet 3이야"."""
    from controller.setup import roles

    setup = {"test_type": "IPTV test", "sender": "tx", "receivers": [
        {"device": "tablet-3", "ch": 1, "host": "rx", "serial": "T3"},
        {"device": "tablet-1", "ch": 2, "host": "rx", "serial": "T1"}]}  # fmt: skip
    found: dict[str, dict[str, Any]] = {
        "tx": {"side": "wan", "tablets": []},
        "rx": {"side": "lan", "tablets": [{"serial": "T3", "state": "device"}]},
    }
    got = roles(setup, found)["receivers"]
    assert [(r["device"], r["serial"]) for r in got] == [("tablet-1", "T3")]  # T1 is off USB
