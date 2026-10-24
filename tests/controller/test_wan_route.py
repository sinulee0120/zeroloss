"""13 1 / 20 GAP-88: the Controller's route to the router WAN side (10.0.0.0/24) through
its test NIC — found at the first real WAN move (the sender at 10.0.0.1 unreachable)."""

from __future__ import annotations

from typing import Any

import pytest

from controller import wan_route as wr


@pytest.fixture
def pc(monkeypatch: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"via": "wlp4s0", "calls": []}

    def run(argv: list[str]) -> tuple[int, str]:
        state["calls"].append(argv)
        if argv[:3] == ["nmcli", "-g", "DHCP4.OPTION"]:
            return 0, "requested_routers = 1 | routers = 192.168.45.1"
        if argv[:3] == ["nmcli", "-g", "GENERAL.CONNECTION"]:
            return 0, "wired-1"
        if argv[:3] == ["nmcli", "device", "reapply"]:
            state["via"] = "enp0s31f6"
        return 0, ""

    monkeypatch.delenv(wr.NO_ROUTE_ENV, raising=False)
    monkeypatch.setattr(wr, "_run", run)
    monkeypatch.setattr(wr, "via", lambda target: state["via"])
    monkeypatch.setattr(wr, "lab_nics", lambda: ["enp0s31f6"])
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/nmcli")
    return state


def test_route_is_added_through_the_test_nics_router(pc: dict[str, Any]) -> None:
    assert wr.ensure_wan_route() == "10.0.0.0/24 now through 192.168.45.1 on enp0s31f6"
    assert ["nmcli", "connection", "modify", "wired-1", "+ipv4.routes",
            "10.0.0.0/24 192.168.45.1"] in pc["calls"]  # fmt: skip
    assert "already through" in wr.ensure_wan_route()  # nothing twice


def test_never_in_tests_and_never_without_a_test_nic(pc: dict[str, Any], monkeypatch: Any) -> None:
    monkeypatch.setenv(wr.NO_ROUTE_ENV, "1")
    assert "off (tests)" in wr.ensure_wan_route() and pc["calls"] == []
    monkeypatch.delenv(wr.NO_ROUTE_ENV)
    monkeypatch.setattr(wr, "lab_nics", lambda: [])
    assert wr.ensure_wan_route() == "no separate test network card" and pc["calls"] == []


def test_windows_routes_through_the_one_wired_card_with_a_router() -> None:
    """2026-10-22: after a restart the route was gone; the Wi-Fi carried 10.0.0.1."""
    lan = {"nic": "이더넷", "index": 2, "router": "192.168.45.1"}
    assert wr.win_plan({"via": 10, "cards": [lan]}) == ("add", lan)  # through the Wi-Fi now
    assert wr.win_plan({"via": 2, "cards": lan}) == ("ok", lan)  # one card: PowerShell's object
    other = {"nic": "이더넷 2", "index": 5, "router": "192.168.7.1"}
    assert wr.win_plan({"via": 10, "cards": [lan, other]}) == ("none", None)  # never guessed
    assert wr.win_plan({}) == ("none", None)


def test_windows_asks_once_per_start(monkeypatch: Any) -> None:
    lan = {"nic": "이더넷", "index": 2, "router": "192.168.45.1"}
    asked: list[str] = []
    monkeypatch.delenv(wr.NO_ROUTE_ENV, raising=False)
    monkeypatch.setattr(wr, "_asked", {})
    monkeypatch.setattr(wr, "_win_read", lambda probe: {"via": 10, "cards": [lan]})

    def declined(subnet: str, card: dict[str, Any]) -> bool:
        asked.append(subnet)
        return False

    monkeypatch.setattr(wr, "_elevated_route", declined)
    assert "permission declined" in wr._windows(wr.WAN_B)  # told once ("could not": an event)
    again = wr._windows(wr.WAN_B)
    assert asked == [wr.WAN_B] and "could not" not in again  # not asked, not told again


def test_an_unreachable_wan_pc_names_the_command_to_type(monkeypatch: Any) -> None:
    """2026-10-22 user: "AI없이도 사용자가 어떻게든 테스트가 되게" — not "check the cable"."""
    from controller import run as cr
    from core.protocol.envelope import ZlError

    monkeypatch.setattr(cr, "manual_fix", lambda subnet: "route add 10.0.0.0 ...")
    lost = ZlError("DSC-014", "Cannot reach probook-sender", "no answer in 5 s", "Check the PC")
    assert cr._unreachable(lost, "10.0.0.1").fix == "route add 10.0.0.0 ..."
    assert cr._unreachable(lost, "192.168.45.87").fix == "Check the PC"  # a LAN-side PC


def test_the_sender_wifi_is_only_advised_once_a_test() -> None:
    """2026-10-22 user: "송신 wifi끄는건 팝업만 띄워 툴이나 너가 하지말고 사용자가 끄고 싶으면
    끄게" -- NET-025 opens a popup; nothing is turned off, the test goes on."""
    from types import SimpleNamespace

    from controller import run as cr

    told: list[tuple[str, str]] = []
    r: Any = object.__new__(cr.RunController)
    r._plan = SimpleNamespace(sender="probook-sender")
    r._raw_plan = {"nics": {"probook-sender": "10.0.0.1"}}
    r._event = lambda code, level, text, device="", **d: told.append((code, text))
    links = [{"nic": "enp37s0", "ip": "10.0.0.1"},
             {"nic": "wlo1", "ip": "192.168.0.27", "wifi": {"band": "2.4GHz"}}]  # fmt: skip
    r._advise_sender_wifi({"links": [links[0]]}, "")  # only the test card: nothing
    assert told == []
    r._advise_sender_wifi({"links": links}, "")
    r._advise_sender_wifi({"links": links}, "CH1 sending fell behind real time")
    assert len(told) == 1 and told[0][0] == "ZL-NET-025" and "wlo1 (192.168.0.27)" in told[0][1]
