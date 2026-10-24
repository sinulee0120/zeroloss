"""The Controller's test network card never carries the default route (13 1, 09 NET-019,
20 GAP-73 5): which card the default route uses, the judgement, the [Fix] commands,
asked once and closed when fine. Never runs the real nmcli."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from controller.precheck import Checklist, controller_route
from core.blocks.route_guard import NEVER_DEFAULT, default_route_nic, never_default
from core.events.bus import Event

HEAD = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"


def route(tmp: Path, *rows: str) -> Path:
    f = tmp / "route"
    f.write_text(HEAD + "".join(r + "\n" for r in rows), encoding="ascii")
    return f


def test_default_route_card_lowest_metric_up_only(tmp_path: Path) -> None:
    # 2026-09-26 the dev PC: the test cable (metric 100) took over from Wi-Fi (600)
    f = route(tmp_path,
              "wlo1\t00000000\t0100A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0",
              "enp0s31f6\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0",
              "enp0s31f6\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0",
              "eth9\t00000000\t0101A8C0\t0002\t0\t0\t1\t00000000\t0\t0\t0")  # not up  # fmt: skip
    assert default_route_nic(f) == "enp0s31f6"
    assert (
        default_route_nic(
            route(tmp_path, "eth0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0")
        )
        is None
    )  # no default at all
    assert default_route_nic(tmp_path / "missing") is None


def test_judgement() -> None:
    assert controller_route("enp0s31f6", "enp0s31f6", ["wlo1"]) is not None  # the incident
    assert controller_route("enp0s31f6", "wlo1", ["wlo1"]) is None  # internet stays on Wi-Fi
    assert controller_route("enp0s31f6", "enp0s31f6", []) is None  # one card: nothing else
    assert controller_route(None, "enp0s31f6", ["wlo1"]) is None  # all devices on this PC


class FakeNm:
    def __init__(self, conn: str = "Wired connection 1", fail: str = "") -> None:
        self.calls: list[list[str]] = []
        self.conn, self.fail = conn, fail

    def __call__(self, argv: list[str], timeout_s: float) -> tuple[int, str]:
        self.calls.append(argv)
        if self.fail and self.fail in argv:
            return 10, "Error: not authorized"
        if argv[:2] == ["nmcli", "-g"]:
            return (0, self.conn) if self.conn else (10, "Error: no connection")
        return 0, ""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="nmcli is Linux (V18 Windows)")
def test_fix_sets_never_default_and_reapplies() -> None:
    nm = FakeNm()
    done, what = never_default("enp0s31f6", nm)
    assert done and "no longer takes the default route" in what
    assert nm.calls == [
        ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", "enp0s31f6"],
        ["nmcli", "connection", "modify", "Wired connection 1", *NEVER_DEFAULT],
        ["nmcli", "device", "reapply", "enp0s31f6"],
    ]
    assert never_default("enp9", FakeNm(conn=""))[0] is False  # not NetworkManager's
    done, what = never_default("enp0s31f6", FakeNm(fail="modify"))
    assert not done and "not authorized" in what
    done, what = never_default("enp0s31f6", FakeNm(fail="reapply"))
    assert not done and "did not apply" in what


def test_asked_once_then_closed_when_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every idle check sees it: one NET-019 with a [Fix] choice, then a clears event
    when a later check finds the default route back on the other card."""
    from controller.run import RunController

    seen: list[Event] = []
    dev = SimpleNamespace(device=SimpleNamespace(host="127.0.0.1", port=1))
    state = {"default": "enp0s31f6"}
    rc: Any = SimpleNamespace(
        _watch={"rx": dev}, _route_told=False,
        nic_of=lambda ip: "enp0s31f6", local_ips=lambda: {"enp0s31f6": "192.168.1.10",
                                                          "wlo1": "172.30.1.5"},
        default_route_nic=lambda: state["default"],
        _event=lambda code, level, text, device="", **data: seen.append(
            Event(code, level, text, device=device, data=data)),
    )  # fmt: skip
    import controller.run as run

    monkeypatch.setattr(run, "own_address", lambda host, port: "192.168.1.10")
    for _ in range(3):
        cl = Checklist("idle", 4)
        out = RunController._check_route(rc, cl, cl.steps[2])
        assert out and cl.steps[2].state == "warn" and cl.steps[2].code == "ZL-NET-019"
    assert [e.code for e in seen] == ["ZL-NET-019"]
    assert seen[0].data["choose"] == "test_route"
    assert seen[0].data["choices"][0]["id"] == "enp0s31f6"
    state["default"] = "wlo1"  # fixed
    cl = Checklist("idle", 4)
    assert RunController._check_route(rc, cl, cl.steps[2]) == []
    assert seen[-1].code == "ZL-INFO" and seen[-1].data["clears"] == ["ZL-NET-019"]
