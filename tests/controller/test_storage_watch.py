"""Storage low (03 F15, 09 SYS-010, 02 4 ``/v1/disk``·2-1 ``storage_low``, 20 GAP-82):
below 20 % or 50 GB free on any PC is told once per PC, cleared when it is back, a
device that does not answer keeps its state, and nothing is deleted by itself."""

from __future__ import annotations

import json
from collections import namedtuple
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from controller.devices import DeviceList
from controller.service import ControllerService
from core.events.bus import Event
from core.node import Node
from core.protocol.client import NodeClient

REPO = Path(__file__).resolve().parents[2]
GB = 1024**3
Usage = namedtuple("Usage", "total used free")


def disk(free_gb: float, total_gb: float) -> Any:
    return lambda _: Usage(int(total_gb * GB), int((total_gb - free_gb) * GB), int(free_gb * GB))


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
    s.node.disk_usage = disk(500, 1000)  # before the first check
    s.start()
    s.devices.add("rx", "127.0.0.1", agent.api.port, "b")
    yield s
    s.stop()


def test_disk_route_is_light_and_real(agent: Node) -> None:
    c = NodeClient("127.0.0.1", agent.api.port, "b")
    try:
        d = c.get("/v1/disk")
    finally:
        c.close()
    assert isinstance(d["free_mb"], int) and 0 <= d["free_mb"] <= d["total_mb"]


@pytest.mark.parametrize(("free", "total", "low"), [
    (60, 1000, True),  # 6 %
    (60, 200, False),  # 30 % and over 50 GB
    (40, 100, True),  # 40 % but under 50 GB
    (50, 250, False),  # exactly 20 % and 50 GB
])  # fmt: skip
def test_the_rule_is_20_percent_or_50_gb(
    svc: ControllerService, agent: Node, free: float, total: float, low: bool
) -> None:
    agent.disk_usage = disk(free, total)
    svc.check_storage()
    assert [x["device"] for x in svc.storage_low()] == (["rx"] if low else [])


def test_told_once_cleared_when_back_and_kept_when_silent(
    svc: ControllerService, agent: Node, tmp_path: Path
) -> None:
    seen: list[Event] = []
    svc.node.bus.subscribe(seen.append)
    agent.disk_usage = disk(10, 1000)
    svc.node.disk_usage = disk(20, 100)
    for _ in range(3):
        svc.check_storage()
    warns = [e for e in seen if e.code == "ZL-SYS-010"]
    assert sorted(e.device for e in warns) == ["controller", "rx"]  # once each
    assert "10.0 GB free (1%)" in next(e.text for e in warns if e.device == "rx")
    assert svc.storage_low() == [
        {"device": "controller", "free_mb": 20 * 1024, "total_mb": 100 * 1024},
        {"device": "rx", "free_mb": 10 * 1024, "total_mb": 1000 * 1024}]  # fmt: skip
    rows = svc.problems.snapshot()["problems"]
    open_codes = {(p["code"], p["device"]) for p in rows if p["active"]}
    assert ("ZL-SYS-010", "rx") in open_codes

    stopped = DeviceList(tmp_path / "ctl" / "devices.json")  # rx stops answering
    (tmp_path / "ctl" / "devices.json").write_text(json.dumps({"schema_version": 1, "devices": [
        {**stopped.to_dict()["devices"][0], "port": 9}]}))  # fmt: skip
    svc.devices = DeviceList(tmp_path / "ctl" / "devices.json")
    svc.check_storage()
    assert [x["device"] for x in svc.storage_low()] == ["controller", "rx"]  # kept

    svc.node.disk_usage = disk(500, 1000)  # space was freed on this PC
    svc.check_storage()
    clears = [e for e in seen if e.code == "ZL-INFO" and e.data.get("clears") == ["ZL-SYS-010"]]
    assert [e.device for e in clears] == ["controller"]
    assert [x["device"] for x in svc.storage_low()] == ["rx"]


def test_after_a_test_ends_it_is_told_again(svc: ControllerService, agent: Node) -> None:
    seen: list[Event] = []
    svc.node.bus.subscribe(seen.append)
    agent.disk_usage = disk(10, 1000)
    svc.check_storage()
    svc._test_ended()  # the end of a test closes every problem (F21)
    assert svc.storage_low() == []
    svc.check_storage()
    assert len([e for e in seen if e.code == "ZL-SYS-010"]) == 2


def test_status_carries_the_list(svc: ControllerService, agent: Node) -> None:
    agent.disk_usage = disk(10, 1000)
    svc.check_storage()
    c = NodeClient("127.0.0.1", svc.node.local.port, "cli")
    try:
        st = c.get("/v1/controller/status")
    finally:
        c.close()
    assert st["storage_low"] == [{"device": "rx", "free_mb": 10240, "total_mb": 1024000}]
