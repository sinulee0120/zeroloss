"""Results storage (03 F15, 02 4 ``/v1/storage``·2-13, 13 ``zeroloss storage``, 20
GAP-79): each run's size (all and raw data), deleting a run or only its raw data on every
PC, never the test on now, never outside the run folder, and a device that does not
answer is written down instead of stopping the rest."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from controller.cli import main
from controller.service import ControllerService
from core.blocks import run_storage
from core.lifecycle.state_flow import State
from core.node import Node
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

REPO = Path(__file__).resolve().parents[2]


def make_run(root: Path, run: str, raw: int = 1000, rest: int = 100) -> Path:
    d = root / run
    (d / "raw" / "rx1").mkdir(parents=True)
    (d / "screens").mkdir()
    (d / "raw" / "rx1" / "h0.jsonl").write_bytes(b"r" * raw)
    (d / "screens" / "s.png").write_bytes(b"p" * 10)
    (d / "summary.json").write_bytes(b"s" * rest)
    return d


def test_sizes_and_what_raw_only_removes(tmp_path: Path) -> None:
    make_run(tmp_path, "R1")
    make_run(tmp_path, "R2", raw=50)
    outside = tmp_path.parent / "keep.txt"
    outside.write_text("x")
    (tmp_path / "R2" / "link").symlink_to(tmp_path.parent)  # a link: removed, never followed
    assert run_storage.list_runs(tmp_path)["R1"] == {"bytes": 1110, "raw_bytes": 1010}
    got = run_storage.delete(tmp_path, "R1", raw_only=True)
    assert got == {"deleted_bytes": 1010, "left_bytes": 100, "errors": []}
    assert sorted(p.name for p in (tmp_path / "R1").iterdir()) == ["summary.json"]
    got = run_storage.delete(tmp_path, "R2", raw_only=False)
    assert got["left_bytes"] == 0 and got["errors"] == [] and not (tmp_path / "R2").exists()
    assert outside.read_text() == "x"
    again = run_storage.delete(tmp_path, "R2", raw_only=False)  # twice: the same, nothing
    assert again == {"deleted_bytes": 0, "left_bytes": 0, "errors": []}
    for bad in ("..", ".", "../x", "a/b", ""):
        with pytest.raises(ZlError) as e:
            run_storage.delete(tmp_path, bad, raw_only=False)
        assert e.value.code == "ZL-DSC-012"
    assert outside.exists()


@pytest.fixture
def agent(tmp_path: Path) -> Iterator[Node]:
    n = Node(tmp_path / "agent", "agent", host="127.0.0.1", port=0, local_port=0,
             modules_dir=REPO / "modules", lost_after_s=60)  # fmt: skip
    n.start()
    yield n
    n.stop()


def test_node_never_deletes_the_run_it_works_for(agent: Node) -> None:
    runs = agent.data_dir / "runs"
    make_run(runs, "R-old", raw=10)
    make_run(runs, "R-now", raw=5000)
    c = NodeClient("127.0.0.1", agent.api.port, "ctl-boot")
    try:
        c.command("/v1/storage/delete", 0, "R-now", run="none")  # a command naming the run
        agent.flow.move(State.STARTING, reason="test")
        got = c.get("/v1/storage")
        assert [r["run"] for r in got["runs"]] == ["R-now", "R-old"]  # biggest first
        assert got["runs"][0]["in_use"] is True and got["runs"][1]["in_use"] is False
        with pytest.raises(ZlError) as e:
            c.command("/v1/storage/delete", 0, run="R-now", raw_only=True)
        assert e.value.code == "ZL-RUN-012" and (runs / "R-now" / "raw").is_dir()
        done = c.command("/v1/storage/delete", 0, run="R-old", raw_only=False)
        assert done["deleted_bytes"] == 120 and not (runs / "R-old").exists()
        with pytest.raises(ZlError) as e:
            c.command("/v1/storage/delete", 0, run="R-old", raw_only="yes")
        assert e.value.code == "ZL-DSC-012"
    finally:
        c.close()


def cli(svc: ControllerService, capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    code = main(["--json", "--port", str(svc.node.local.port), "storage", *argv])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_cli_lists_every_place_and_deletes_everywhere(
    agent: Node, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    svc = ControllerService(tmp_path / "ctl", tmp_path / "reports", REPO / "modules",
                            host="127.0.0.1", port=0, local_port=0)  # fmt: skip
    svc.start()
    try:
        make_run(tmp_path / "reports", "R1", raw=0, rest=300)  # the report folder
        make_run(agent.data_dir / "runs", "R1", raw=2000)
        make_run(agent.data_dir / "runs", "R2", raw=100)
        svc.devices.add("rx", "127.0.0.1", agent.api.port, "b")
        (tmp_path / "ctl" / "devices.json").write_text(json.dumps({"schema_version": 1, "devices": [
            *svc.devices.to_dict()["devices"],
            {"name": "gone", "host": "127.0.0.1", "port": 9, "device_id": "x", "role": "agent",
             "added_utc": "2026-09-26T00:00:00Z"}]}))  # fmt: skip
        from controller.devices import DeviceList

        svc.devices = DeviceList(tmp_path / "ctl" / "devices.json")
        code, doc = cli(svc, capsys, "list")
        assert code == 0, doc
        d = doc["data"]
        assert [r["run"] for r in d["runs"]] == ["R1", "R2"]
        r1 = d["runs"][0]
        assert r1["where"]["Reports"] == {"bytes": 310, "raw_bytes": 10}
        assert r1["where"]["rx"] == {"bytes": 2110, "raw_bytes": 2010}
        assert r1["bytes"] == 2420 and r1["in_use"] is False
        assert d["nodes"]["rx"] == "ok" and d["nodes"]["gone"].startswith("ZL-DSC-014")

        code, doc = cli(svc, capsys, "delete", "R1", "--raw-only")  # no --yes: nothing
        assert code == 4 and (agent.data_dir / "runs" / "R1" / "raw").is_dir()

        code, doc = cli(svc, capsys, "delete", "R1", "--raw-only", "--yes")
        d = doc["data"]
        assert code == 3  # one device did not answer
        assert d["deleted_bytes"] == 2020 and d["nodes"]["rx"] == "ok"
        assert d["nodes"]["gone"].startswith("ZL-DSC-014")
        assert (tmp_path / "reports" / "R1" / "summary.json").is_file()
        assert not (agent.data_dir / "runs" / "R1" / "raw").exists()
        db = sqlite3.connect(tmp_path / "ctl" / "experience.db")
        rows = db.execute("SELECT action_id, actor, outcome FROM actions").fetchall()
        db.close()
        assert rows == [("cli.storage.delete", "human", "not_resolved")]  # 14 1: recorded

        svc.run.run_id = "R2"  # the test on now
        svc.run.state = svc.run.state.__class__("RUNNING")
        code, doc = cli(svc, capsys, "delete", "R2", "--yes")
        assert code == 3 and doc["codes"] == ["ZL-RUN-012"]
        assert (agent.data_dir / "runs" / "R2").is_dir()
        code, doc = cli(svc, capsys, "list")
        assert [r["in_use"] for r in doc["data"]["runs"] if r["run"] == "R2"] == [True]
    finally:
        svc.run.state = svc.run.state.__class__("IDLE")
        svc.stop()
