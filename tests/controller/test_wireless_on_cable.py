"""NET-026 (2026-11-06 user: "경고 추가해"): right after the router came back, the T460's
test Wi-Fi card had no address yet, so that test received on its cable and nothing said
so. Start now warns; the test still runs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from controller import run as ctl
from controller.setup import plan_of

WIFI = {"nic": "wlx0", "ip": "192.168.77.114", "up": True, "wifi": {"band": "5GHz"}}
CABLE = {"nic": "eth0", "ip": "192.168.77.237", "up": True, "link_mbps": 1000}


def _plan(network: str) -> dict[str, Any]:
    setup = {"test_type": "IPTV test", "quality": "UHD", "duration_s": 600, "settings": {}}
    chosen = {"sender": "tx", "receivers": [
        {"device": "rx", "ch": 1, "network": network},
        {"device": "tablet-1", "ch": 1, "host": "rx", "serial": "TAB0001", "network": "Wireless"},
    ]}  # fmt: skip
    return plan_of(setup, chosen)


def test_the_plan_names_the_pc_rows_set_to_wireless_only() -> None:
    assert _plan("Wireless")["wireless_pcs"] == ["rx"]  # not the tablet
    assert "wireless_pcs" not in _plan("Wired")


def _ctl(links: list[dict[str, Any]]) -> ctl.RunController:
    r = ctl.RunController.__new__(ctl.RunController)
    r._raw_plan = {"wireless_pcs": ["rx"]}
    r._watch = {"rx": SimpleNamespace(links=links)}  # type: ignore[dict-item]
    return r


def test_a_wireless_pc_on_its_cable_is_named_and_on_its_wifi_is_not() -> None:
    assert _ctl([WIFI, CABLE])._wireless_on_cable({"rx": CABLE["ip"]}) == [
        ("rx", "eth0", "192.168.77.237")
    ]
    assert _ctl([WIFI, CABLE])._wireless_on_cable({"rx": WIFI["ip"]}) == []
    assert _ctl([CABLE])._wireless_on_cable({}) == []  # no card known: nothing guessed
