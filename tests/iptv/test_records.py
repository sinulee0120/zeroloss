"""1-second record retrieval (02 4 ``/v1/records``·2-13, 03 F15, 09 RPT-005, 20 GAP-80):
the node lists and sends copies of its record files, the Controller fetches a run's
files into the report folder after the report (and again on ``zeroloss records``),
skipping files it already has, and a device that does not answer is written down."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from controller.cli import main
from controller.devices import DeviceList
from controller.service import ControllerService
from core.events.bus import Event
from core.lifecycle.state_flow import State
from core.node import Node
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

REPO = Path(__file__).resolve().parents[2]


def put_records(node: Node, run: str) -> dict[str, bytes]:
    raw = node.data_dir / "runs" / run / "raw" / "rx1"
    raw.mkdir(parents=True)
    files = {f"{run}_h000.jsonl": b'{"type":"h"}\n' + b'{"type":"s","t":1}\n' * 50,
             f"{run}_h001_r1.jsonl": b'{"type":"h"}\n{"type":"f"}\n'}  # fmt: skip
    for name, data in files.items():
        (raw / name).write_bytes(data)
    (raw / "notes.txt").write_text("not a record file")
    return files


@pytest.fixture
def agent(tmp_path: Path) -> Iterator[Node]:
    n = Node(tmp_path / "agent", "agent", host="127.0.0.1", port=0, local_port=0,
             modules_dir=REPO / "modules", lost_after_s=60)  # fmt: skip
    n.start()
    yield n
    n.stop()


def test_node_lists_and_sends_copies(agent: Node, tmp_path: Path) -> None:
    files = put_records(agent, "R1")
    c = NodeClient("127.0.0.1", agent.api.port, "ctl-boot")
    try:
        index = c.get("/v1/records/index", run="R1")
        assert [(f["device"], f["hour"], f["name"]) for f in index["files"]] == [
            ("rx1", 0, "R1_h000.jsonl"), ("rx1", 1, "R1_h001_r1.jsonl")]  # fmt: skip
        assert c.get("/v1/records/index", run="R9")["files"] == []
        got = c.download("/v1/records", tmp_path / "h0.jsonl", run="R1", device="rx1",
                         name="R1_h000.jsonl")  # fmt: skip
        assert got.read_bytes() == files["R1_h000.jsonl"]
        src = agent.data_dir / "runs" / "R1" / "raw" / "rx1" / "R1_h000.jsonl"
        assert src.read_bytes() == files["R1_h000.jsonl"]  # the node keeps its own (F15)
        cases: list[tuple[dict[str, str], str]] = [
            ({"run": "R1", "device": "rx1", "name": "R1_h007.jsonl"}, "ZL-RPT-005"),
            ({"run": "R1", "device": "..", "name": "R1_h000.jsonl"}, "ZL-DSC-012"),
            ({"run": "R1", "device": "rx1", "name": "../R1_h000.jsonl"}, "ZL-DSC-012"),
            ({"run": "R1", "device": "rx1", "name": "R2_h000.jsonl"}, "ZL-DSC-012"),
            ({"run": "..", "device": "rx1", "name": "R1_h000.jsonl"}, "ZL-DSC-012"),
        ]
        for q, code in cases:
            with pytest.raises(ZlError) as e:
                c.download("/v1/records", tmp_path / "x.jsonl", run=q["run"],
                           device=q["device"], name=q["name"])  # fmt: skip
            assert e.value.code == code, q
        c.command("/v1/storage/delete", 0, "R1", run="none")  # a command naming the run
        agent.flow.move(State.STARTING, reason="test")  # still writing R1
        with pytest.raises(ZlError) as e:
            c.download("/v1/records", tmp_path / "x.jsonl", run="R1", device="rx1",
                       name="R1_h000.jsonl")  # fmt: skip
        assert e.value.code == "ZL-RUN-012"
        assert not list(tmp_path.glob("x.jsonl*"))
    finally:
        c.close()


@pytest.fixture
def svc(tmp_path: Path, agent: Node) -> Iterator[ControllerService]:
    s = ControllerService(tmp_path / "ctl", tmp_path / "reports", REPO / "modules",
                          host="127.0.0.1", port=0, local_port=0)  # fmt: skip
    s.start()
    s.devices.add("rx", "127.0.0.1", agent.api.port, "b")
    yield s
    s.stop()


def add_gone(svc: ControllerService, tmp_path: Path) -> None:
    (tmp_path / "ctl" / "devices.json").write_text(json.dumps({"schema_version": 1, "devices": [
        *svc.devices.to_dict()["devices"],
        {"name": "gone", "host": "127.0.0.1", "port": 9, "device_id": "x", "role": "agent",
         "added_utc": "2026-09-27T00:00:00Z"}]}))  # fmt: skip
    svc.devices = DeviceList(tmp_path / "ctl" / "devices.json")


def records_cli(svc: ControllerService, capsys: pytest.CaptureFixture[str], run: str) -> Any:
    code = main(["--json", "--port", str(svc.node.local.port), "records", "--run", run])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]


def test_cli_fetches_skips_what_is_here_and_names_the_silent_device(
    agent: Node, svc: ControllerService, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = put_records(agent, "R1")
    add_gone(svc, tmp_path)
    seen: list[Event] = []
    svc.node.bus.subscribe(seen.append)
    code, d = records_cli(svc, capsys, "R1")
    assert code == 3, d  # one device did not answer
    assert [(f["hour"], f["state"]) for f in d["files"]] == [(0, "got"), (1, "got")]
    assert d["missing"]["gone"].startswith("ZL-DSC-014") and "rx" not in d["missing"]
    here = tmp_path / "reports" / "R1" / "raw" / "rx1"
    assert {p.name: p.read_bytes() for p in here.iterdir()} == files  # no .part left
    assert [e.device for e in seen if e.code == "ZL-RPT-005"] == ["gone"]
    code, d = records_cli(svc, capsys, "R1")  # again: nothing fetched twice
    assert [f["state"] for f in d["files"]] == ["had", "had"]


def test_after_the_report_the_controller_fetches_by_itself(
    agent: Node, svc: ControllerService, tmp_path: Path
) -> None:
    files = put_records(agent, "R2")
    assert svc.run._on_report == svc._records_later  # wired to the run's end (02 2-13)
    svc._records_later("R2", ["rx"])
    assert svc._records_thread is not None
    svc._records_thread.join(timeout=30)
    here = tmp_path / "reports" / "R2" / "raw" / "rx1"
    assert {p.name: p.read_bytes() for p in here.iterdir()} == files
    svc._closing.set()  # the Controller is closing: no new retrieval starts
    svc._records_thread = None
    svc._records_later("R2", ["rx"])
    assert svc._records_thread is None


def test_the_test_on_now_is_not_fetched(
    svc: ControllerService, capsys: pytest.CaptureFixture[str]
) -> None:
    svc.run.run_id = "R3"
    svc.run.state = svc.run.state.__class__("RUNNING")
    try:
        code = main(["--json", "--port", str(svc.node.local.port), "records", "--run", "R3"])
        doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert code == 3 and doc["codes"] == ["ZL-RUN-012"]
    finally:
        svc.run.state = svc.run.state.__class__("IDLE")
