"""GAP-158: the way from a LAN-side PC to the sender on the WAN side, for any router.

2026-10-15 user: "every time the router changes, 10.0.0.1 has to be pointed at the new
router — impossible without the AI?", "what about testing 10 different routers?".
"""

from __future__ import annotations

from core.blocks.wan_reach import WanReach, gateway_of


class FakeIp:
    def __init__(
        self,
        dev: str,
        gw: str = "IP4.GATEWAY:\nDHCP4.OPTION:routers = 192.168.1.1",
        via: str = "192.168.1.1",
    ) -> None:
        self.dev = dev
        self.gw = gw
        self.via = via
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], t: float) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[:3] == ["ip", "-o", "route"]:
            return 0, f"10.0.0.1 via {self.via} dev {self.dev} src 192.168.0.13 uid 999"
        if argv[:2] == ["nmcli", "-g"]:
            return 0, self.gw
        if argv[:3] in (["ip", "route", "replace"], ["ip", "route", "del"]):
            self.dev = argv[argv.index("dev") + 1] if argv[2] == "replace" else "wlp4s0"
            self.via = argv[argv.index("via") + 1]
            return 0, ""
        return 1, "unknown"


def test_a_route_that_is_already_right_is_left_alone() -> None:
    ip = FakeIp("enp0s31f6")
    got = WanReach(ip.run, "linux").ensure("10.0.0.1", "enp0s31f6")
    assert got["ok"] and "already" in got["done"]
    assert not [c for c in ip.calls if c[:3] == ["ip", "route", "replace"]]


def test_the_current_routers_gateway_carries_it_for_this_test_only() -> None:
    ip = FakeIp("wlp4s0", via="192.168.0.1")  # the home Wi-Fi's default route would take it
    r = WanReach(ip.run, "linux")
    got = r.ensure("10.0.0.1", "enp0s31f6")
    assert (
        got["ok"] and got["done"] == "route 10.0.0.0/24 via 192.168.1.1 on enp0s31f6 for this test"
    )
    # 2026-10-17: ip route, never nmcli -- a reapply asked the router for a new address
    assert ["ip", "route", "replace", "10.0.0.0/24", "via", "192.168.1.1", "dev", "enp0s31f6",
            "metric", "50"] in ip.calls  # fmt: skip
    assert not [c for c in ip.calls if c[:3] == ["nmcli", "device", "modify"]]
    assert not [c for c in ip.calls if "connection" in c]
    assert r.release() == []
    assert ip.calls[-1] == ["ip", "route", "del", "10.0.0.0/24", "via", "192.168.1.1", "dev",
                            "enp0s31f6", "metric", "50"]  # fmt: skip
    assert r.release() == []  # nothing left to take away


def test_another_router_another_gateway_nothing_to_set() -> None:
    ip = FakeIp("wlp4s0", gw="10.10.0.254\n")
    got = WanReach(ip.run, "linux").ensure("10.0.0.1", "eth0")
    assert "via 10.10.0.254 on eth0" in got["done"]


def test_no_gateway_says_why_and_changes_nothing() -> None:
    ip = FakeIp("wlp4s0", gw="\n")
    got = WanReach(ip.run, "linux").ensure("10.0.0.1", "enp0s31f6")
    assert not got["ok"] and "no gateway" in got["done"] and "wlp4s0" in got["why"]
    assert gateway_of(ip.run, "enp0s31f6") is None


def test_off_linux_nothing_is_touched() -> None:
    ip = FakeIp("x")
    assert WanReach(ip.run, "win32").ensure("10.0.0.1", "eth0")["ok"] and ip.calls == []


def test_a_saved_route_through_a_gateway_that_is_gone_gets_the_current_one() -> None:
    """2026-10-17: the T460's profile kept 10.0.0.0/24 via 192.168.1.1 from the last
    router; on the new one (LAN 192.168.45.x) it left on the right card through a gateway
    that is gone and counted as "already". User: "매번 지웠다 넣었다는 사용자도 할수 있나?"."""
    ip = FakeIp("enp0s31f6", gw="192.168.45.1\n", via="192.168.1.1")
    got = WanReach(ip.run, "linux").ensure("10.0.0.1", "enp0s31f6")
    assert got["ok"] and "via 192.168.45.1 on enp0s31f6" in got["done"]
    assert ["ip", "route", "replace", "10.0.0.0/24", "via", "192.168.45.1", "dev", "enp0s31f6",
            "metric", "50"] in ip.calls  # fmt: skip


def test_a_route_the_link_already_took_away_is_not_left() -> None:
    """2026-10-17: the T460's card went down and up in the speed cases; its route went
    with the link and the stop warned "RTNETLINK answers: No such process" (NET-023)."""
    ip = FakeIp("wlp4s0", via="192.168.0.1")
    r = WanReach(ip.run, "linux")
    r.ensure("10.0.0.1", "enp0s31f6")
    gone = ip.run

    def run(argv: list[str], t: float) -> tuple[int, str]:
        if argv[:3] == ["ip", "route", "del"]:
            return 2, "RTNETLINK answers: No such process\n"
        return gone(argv, t)

    r.run = run
    assert r.release() == []
