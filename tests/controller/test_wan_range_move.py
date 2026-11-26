"""2026-11-26, a new router that used 10.0.0.x itself (user: "새 공유기 인식 못하는 버그잖아 /
앱이 다시 연결하게 해야지"): the Controller sees another device answer for the sender's
address, tells the sender (found on its other card) to move, and saves it there."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

import controller.service as service_mod
from controller import wan_route
from controller.run import RunState
from controller.service import ControllerService
from controller.wan_route import range_taken
from core.blocks.wan_nets import NETS, net_of, server_ips

WIN_EN = """Pinging 10.0.0.1 with 32 bytes of data:
Reply from 10.0.0.2: Destination host unreachable.
Reply from 10.0.0.2: Destination host unreachable."""
WIN_KO = """Ping 10.0.0.1 32바이트 데이터 사용:
10.0.0.2의 응답: 대상 호스트에 연결할 수 없습니다."""
LINUX = """PING 10.0.0.1 (10.0.0.1) 56(84) bytes of data.
From 10.0.0.2 icmp_seq=1 Destination Host Unreachable"""
FINE = """Reply from 10.0.0.1: bytes=32 time<1ms TTL=63"""
FAR = """Reply from 172.30.1.254: Destination host unreachable."""


@pytest.mark.parametrize(("out", "taken"), [
    (WIN_EN, "10.0.0.2"), (WIN_KO, "10.0.0.2"), (LINUX, "10.0.0.2"),
    (FINE, None), (FAR, None), ("Request timed out.", None),
])  # fmt: skip
def test_another_device_answering_for_the_sender_is_read(out: str, taken: str | None) -> None:
    assert range_taken("10.0.0.1", run=lambda argv: (1, out)) == taken


def test_the_ranges() -> None:
    assert server_ips()[0] == "10.0.0.1" and len(set(server_ips())) == len(NETS)
    assert net_of("100.64.0.1") == "100.64.0.0/24" and net_of("192.168.1.5") is None


class FakeNode:
    """The sender answering on its home Wi-Fi card."""

    def __init__(self) -> None:
        self.moved: list[dict[str, Any]] = []

    def __call__(self, host: str, port: int, boot: str, name: str) -> Any:
        node = self

        class Client:
            def get(self, path: str, timeout_s: float = 0) -> dict[str, Any]:
                net = NETS[1][0] if node.moved else NETS[0][0]
                lease = {"mac": "02:00:5e:10:00:64", "ip": "10.0.0.100"}
                return {"state": "ready", "server_ip": net.split("/")[0], "net": net,
                        "lease": lease}  # fmt: skip

            def command(self, path: str, gen: int, run: Any, timeout_s: float = 0,
                        **params: Any) -> dict[str, Any]:  # fmt: skip
                node.moved.append(params)
                return {"state": "waiting_nic", "net": NETS[1][0]}

            def close(self) -> None:
                pass

        return Client()


def _svc(taken: str | None, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakeNode, list[Any]]:
    node = FakeNode()
    monkeypatch.setattr(service_mod, "NodeClient", node)
    monkeypatch.setattr(wan_route, "range_taken", lambda host, besides=(): taken)
    dev = SimpleNamespace(name="probook-sender", device_id="d1", host="10.0.0.1", port=10992)
    said: list[Any] = []
    saved: list[str] = []
    routes: list[str] = []
    svc = SimpleNamespace(
        devices=SimpleNamespace(all=lambda: [dev]), _closing=threading.Event(),
        run=SimpleNamespace(state=RunState.IDLE, boot_id="b", generation=1,
                            wan_route_to=routes.append),
        node=SimpleNamespace(bus=SimpleNamespace(publish=said.append)),
        _set_address=lambda d, host, port: saved.append(host),
        saved=saved, routes=routes,
    )  # fmt: skip
    return svc, node, said


ROW = {"device_id": "d1", "addresses": ["192.168.0.27"], "port": 10992}


def test_the_sender_moves_when_the_router_uses_its_range(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, node, said = _svc("10.0.0.2", monkeypatch)
    ControllerService._wan_range_move(svc, ROW)
    assert node.moved and node.moved[0]["move"] is True and "10.0.0.2" in node.moved[0]["why"]
    assert svc.saved == ["100.64.0.1"] and svc.routes[-1] == "100.64.0.0/24"
    assert [e.code for e in said] == ["ZL-SND-028", "ZL-INFO"]


def test_nothing_moves_without_another_device_answering(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, node, said = _svc(None, monkeypatch)
    ControllerService._wan_range_move(svc, ROW)
    assert not node.moved and not svc.saved and not said


def test_never_during_a_test_nor_for_a_pc_not_on_the_wan_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, node, _ = _svc("10.0.0.2", monkeypatch)
    svc.run.state = RunState.RUNNING
    ControllerService._wan_range_move(svc, ROW)
    assert not node.moved
    svc, node, _ = _svc("10.0.0.2", monkeypatch)
    svc.devices.all()[0].host = "192.168.1.237"  # a LAN-side receiver
    ControllerService._wan_range_move(svc, ROW)
    assert not node.moved


def test_windows_asks_once_for_every_range() -> None:
    from controller.wan_route import elevated_args

    program, args = elevated_args("10.0.0.0/24", {"router": "172.30.1.254", "index": 7})
    assert program == "cmd.exe" and args.startswith("/c route add 10.0.0.0 mask 255.255.255.0")
    assert args.count("route add") == len(NETS)
    assert "route add 100.64.0.0 mask 255.255.255.0 172.30.1.254 if 7" in args


def test_the_routers_own_wan_address_never_moves_the_range() -> None:
    """A sender that is gone (cable out): the router's own WAN address 10.0.0.100 says
    "unreachable" for 10.0.0.1. That is not the router using our range."""
    gone = "Reply from 10.0.0.100: Destination host unreachable."
    assert range_taken("10.0.0.1", run=lambda argv: (1, gone), besides=("10.0.0.100",)) is None
    assert range_taken("10.0.0.1", run=lambda argv: (1, WIN_EN), besides=("10.0.0.100",)) == (
        "10.0.0.2"
    )


def test_a_route_to_an_earlier_routers_gateway_is_not_fine() -> None:
    """2026-11-26 review: "10.0.0.0/24 via 192.168.1.1" left from the earlier router on
    the same card counted as "ok"; Windows sent everything to a gateway that is gone."""
    from controller import wan_route as wr

    lan = {"nic": "이더넷", "index": 7, "router": "172.30.1.254"}
    assert wr.win_plan({"via": 7, "hop": "172.30.1.254", "cards": [lan]}) == ("ok", lan)
    assert wr.win_plan({"via": 7, "hop": "192.168.1.1", "cards": [lan]}) == ("add", lan)
    _, args = wr.elevated_args("10.0.0.0/24", {**lan, "stale": "192.168.1.1"})
    first = args.split(" & ")[:2]
    assert first == ["/c route delete 10.0.0.0 mask 255.255.255.0 192.168.1.1",
                     "route add 10.0.0.0 mask 255.255.255.0 172.30.1.254 if 7"]  # fmt: skip
