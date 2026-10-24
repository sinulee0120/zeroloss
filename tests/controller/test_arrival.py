"""Step 5 tells a receiver's test card by where the packets came in (03 F17, GAP-115 ②).

Registered by an address that is not through the router (an office or home Wi-Fi), a
receiver's card cannot be told; the multicast test then listens on every card and the
receiver says which card got the packets (02 4 ``arrived_cards``). Works at any site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from controller.devices import DeviceList
from controller.run import RunController
from core.events.bus import Event, EventBus

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Plan:
    primed: bool = False
    nics: dict[str, str] = field(default_factory=dict)


def controller(tmp_path: Path, links: list[dict[str, Any]], arrived: dict[str, Any]) -> Any:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    rc = RunController(DeviceList(tmp_path / "devices.json"), tmp_path / "rep",
                       REPO / "modules", bus)  # fmt: skip
    rc._raw_plan = {"nics": {"tx": "10.0.0.1"}}
    rc._plan = Plan(primed=True, nics={"tx": "10.0.0.1"})  # S-18 Auto made it primed
    rc._watch = {"rx": SimpleNamespace(links=links)}  # type: ignore[dict-item]
    rc._by_arrival = {"rx": "reached over Wi-Fi wlp4s0 (192.168.0.13)"}
    rc._arrivals = {"rx": arrived}
    return rc, seen


T460: list[dict[str, Any]] = [{"nic": "enp0s31f6", "ip": "192.168.45.177"},
        {"nic": "wlx02005e000001", "ip": "192.168.45.190", "wifi": {"rssi_dbm": -33}},
        {"nic": "wlp4s0", "ip": "192.168.0.13", "wifi": {"rssi_dbm": -60}}]  # fmt: skip


def test_the_wired_card_wins_when_cable_and_test_wifi_both_got_packets(tmp_path: Path) -> None:
    """receiverPC: the T460 had the router LAN by cable and by the test Wi-Fi; "most
    packets" is a coin flip, so a wired receiver takes the wired card."""
    cards = [{"nic": "wlx02005e000001", "ip": "192.168.45.190", "packets": 200},
             {"nic": "enp0s31f6", "ip": "192.168.45.177", "packets": 190}]  # fmt: skip
    rc, seen = controller(tmp_path, T460, {"arrived_cards": cards, "arrived_on": cards[0]})
    rc._adopt_arrivals()
    assert rc._raw_plan["nics"] == {"tx": "10.0.0.1", "rx": "192.168.45.177"}
    assert rc._plan == Plan(primed=True, nics={"tx": "10.0.0.1", "rx": "192.168.45.177"})
    assert rc._not_via == [] and any("test card found" in e.text for e in seen)
    # 2026-10-09: the idle check's NET-022 for it stayed open all run; now it clears
    found = next(e for e in seen if "test card found" in e.text)
    assert (found.device, found.data["clears"]) == ("rx", ["ZL-NET-022"])


def test_nothing_came_in_keeps_the_old_warning(tmp_path: Path) -> None:
    rc, seen = controller(tmp_path, T460, {"arrived_cards": [], "arrived_on": None})
    rc._adopt_arrivals()
    assert "rx" not in rc._raw_plan["nics"] and rc._not_via
    assert [e.code for e in seen] == ["ZL-NET-022"]


def test_only_a_wifi_card_got_them_then_that_one(tmp_path: Path) -> None:
    only = {"nic": "wlx02005e000001", "ip": "192.168.45.190", "packets": 200}
    rc, _ = controller(tmp_path, T460, {"arrived_cards": [only], "arrived_on": only})
    rc._adopt_arrivals()
    assert rc._raw_plan["nics"]["rx"] == "192.168.45.190"


def test_a_home_wifi_card_in_heartbeat_form_is_never_the_test_card() -> None:
    """GAP-122 (real T460, 2026-10-09): heartbeat links mark Wi-Fi with a ``wifi`` entry,
    null until read; the check read only ``wireless``, so the home Wi-Fi the Controller
    reached became the receiver's test card and the probe joined there."""
    from controller.precheck import is_wireless, test_card

    home = {"nic": "wlp4s0", "ip": "192.168.0.13", "up": True, "wifi": {"rssi_dbm": -60}}
    probook_wifi = {"nic": "wlo1", "ip": "192.168.0.27", "up": True, "wifi": None}
    wired = {"nic": "enp0s31f6", "ip": "192.168.45.177", "up": True, "link_mbps": 1000}
    assert is_wireless(home) and is_wireless(probook_wifi) and not is_wireless(wired)
    ip, why = test_card("receiver", "192.168.0.13", [wired, home], None)
    assert ip is None and "Wi-Fi" in why  # left to step 5, which listens on every card
