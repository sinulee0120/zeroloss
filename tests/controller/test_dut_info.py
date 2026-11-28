"""The router under test's UPnP description (controller/dut_info.py, 03 F14 "DUT 정보")."""

from __future__ import annotations

import io
from typing import Any

import controller.dut_info as di
from controller.changes import same_router

XML = b"""<?xml version="1.0"?><root><device>
<manufacturer>Maker</manufacturer><modelName>AX-1</modelName><modelNumber>v2</modelNumber>
<modelDescription>Wireless Router</modelDescription><friendlyName>Lab router</friendlyName>
<UDN>uuid:0000-test</UDN><firmwareVersion>1.2.4</firmwareVersion></device></root>"""

# this PC: the house network on Wi-Fi and the test router's LAN on the cable (2026-11-28)
CARDS = [("192.168.0.10", 24), ("172.30.1.95", 24)]


def answers(*rows: tuple[str, str, str]) -> Any:
    return lambda cards: {src: (loc, card) for src, loc, card in rows}


def test_only_the_receivers_router_counts() -> None:
    """The house router answered on Wi-Fi and the router under test on the cable."""
    seen: list[str] = []

    def read(url: str) -> dict[str, str]:
        seen.append(url)
        return di.describe(url, opener=lambda u, timeout: io.BytesIO(XML))

    lab = "http://172.30.1.254:5000/rootDesc.xml"
    find = answers(("192.168.0.1", "http://192.168.0.1:1900/igd.xml", "192.168.0.10"),
                   ("172.30.1.254", lab, "172.30.1.95"))  # fmt: skip
    got = di.dut_info(["172.30.1.11"], CARDS, find, read)
    assert seen == ["http://172.30.1.254:5000/rootDesc.xml"]
    assert got == {"address": "172.30.1.254", "model": "Maker AX-1 v2", "firmware_build": "1.2.4",
                   "description": "Wireless Router", "name": "Lab router",
                   "udn": "uuid:0000-test"}  # fmt: skip


def test_never_a_guess() -> None:
    read = lambda url: {"modelName": "x"}  # noqa: E731
    two = answers(("172.30.1.254", "http://172.30.1.254/a.xml", "172.30.1.95"),
                  ("172.30.1.1", "http://172.30.1.1/b.xml", "172.30.1.95"))  # fmt: skip
    assert di.dut_info(["172.30.1.11"], CARDS, two, read) is None  # two routers there
    away = answers(("172.30.1.254", "http://10.9.9.9/a.xml", "172.30.1.95"))
    assert di.dut_info(["172.30.1.11"], CARDS, away, read) is None  # described elsewhere
    house = answers(("192.168.0.1", "http://192.168.0.1/a.xml", "192.168.0.10"))
    assert di.dut_info(["172.30.1.11"], CARDS, house, read) is None  # not the receivers'
    assert di.dut_info([], CARDS, house, read) is None

    def broken(url: str) -> dict[str, str]:
        raise OSError("timed out")

    one = answers(("172.30.1.254", "http://172.30.1.254/a.xml", "172.30.1.95"))
    assert di.dut_info(["172.30.1.11"], CARDS, one, broken) is None


def test_f24_knows_the_router_by_its_upnp_id() -> None:
    def run(udn: str | None, model: str | None = "IGD 1.0", sender: str = "tx") -> Any:
        dut = {"model": model, "wan_ip_way": "dhcp", "upnp": {"udn": udn} if udn else None}
        return {"dut": dut}, {"devices": {"sender": sender}}

    assert same_router(run("uuid:a"), run("uuid:a", sender="other"))
    assert not same_router(run("uuid:a"), run("uuid:b"))  # a generic model name, two routers
    assert same_router(run(None), run("uuid:b"))  # an older run: the model then
    assert not same_router(run(None, None), run(None, None, sender="other"))


def test_s71_off_asks_the_router_nothing(monkeypatch: Any) -> None:
    """12 4-2 (receiverPC 2026-11-28): the read is F14's and on by default; S-71 Off
    turns it off, and then nothing goes to the router."""
    import threading
    from types import SimpleNamespace

    import controller.run as run_mod

    asked: list[list[str]] = []

    def fake(hosts: list[str]) -> dict[str, str]:
        asked.append(hosts)
        return {"model": "X"}

    monkeypatch.setattr(run_mod, "dut_info", fake)
    dev = SimpleNamespace(host="172.30.1.11")
    plan: dict[str, Any] = {"devices": {"receivers": [{"device": "rx"}]}, "settings": {}}
    me = SimpleNamespace(_raw_plan=plan, devices=SimpleNamespace(get=lambda name: dev))
    run_mod.RunController._look_up_router(me)  # type: ignore[arg-type]
    assert isinstance(me._dut_done, threading.Event) and me._dut_done.wait(5)
    assert asked == [["172.30.1.11"]] and me._dut_info == {"model": "X"}
    plan["settings"] = {"S-71": "Off"}
    run_mod.RunController._look_up_router(me)  # type: ignore[arg-type]
    assert asked == [["172.30.1.11"]] and me._dut_done is None and me._dut_info is None
