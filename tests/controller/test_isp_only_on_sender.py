"""The ISP role (07 3.2 B) only on the test's sender (2026-11-05: a new router resetting
left the T460 receiver's LAN card without a DHCP answer; it turned itself into 10.0.0.1
there and lent 10.0.0.100 to the Controller PC; user: "버그네"). Made-up MACs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from controller import run as ctl
from core.protocol.envelope import ZlError


class FakeNode:
    def __init__(self, wan: dict[str, Any] | None) -> None:
        self.wan = wan
        self.sent: list[dict[str, Any]] = []

    def get(self, path: str, timeout_s: float = 0.0) -> dict[str, Any]:
        if self.wan is None:
            raise ZlError("DSC-012", "Request rejected", detail="no such path: /v1/wan")
        return self.wan

    def command(self, path: str, gen: int, run_id: Any, timeout_s: float = 0.0,
                **p: Any) -> dict[str, Any]:  # fmt: skip
        self.sent.append(p)
        return {}


def test_a_receiver_serving_by_itself_is_turned_off_and_the_sender_put_back() -> None:
    serving = {"mode": "auto", "state": "ready", "nic": "enp0s31f6",
               "lease": {"mac": "02:00:00:00:00:aa", "ip": "10.0.0.100"}}  # fmt: skip
    nodes = {"tx": FakeNode({"mode": "off", "state": "off"}), "rx": FakeNode(serving),
             "rx2": FakeNode({"mode": "auto", "state": "waiting"}),
             "ctl": FakeNode({"mode": "off", "state": "off"}), "old": FakeNode(None)}  # fmt: skip
    r = ctl.RunController.__new__(ctl.RunController)
    r._plan = SimpleNamespace(sender="tx")
    r._watch = {n: SimpleNamespace(client=c) for n, c in nodes.items()}  # type: ignore[misc]
    r.generation, r.run_id = 3, "R1"
    events: list[tuple[str, str]] = []
    r._event = lambda code, level, text, device="", **d: events.append((code, text))  # type: ignore[method-assign]
    said = r._isp_only_on_sender()
    assert nodes["rx"].sent == [{"on": False}] and nodes["rx2"].sent == [{"on": False}]
    assert nodes["tx"].sent == [{"auto": True}]  # a former receiver sends now
    assert nodes["ctl"].sent == [] and nodes["old"].sent == []
    assert len(said) == 1 and "rx had turned itself into the router's ISP" in said[0]
    assert "02:00:00:00:00:aa" in said[0] and events == [("ZL-INFO", said[0])]
