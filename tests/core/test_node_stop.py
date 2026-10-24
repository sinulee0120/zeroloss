"""Stopping one node (02 4 ``POST /v1/stop``, 13 ``zeroloss agent stop``, 09 USR-007):
on the PC itself without a Controller, or from the Controller; modules stop in order,
the run folder's files are listed, and a stop on the PC during a test is a warning the
Controller gets."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from controller.cli import main
from core.events.bus import Event
from core.lifecycle.state_flow import State
from core.node import Node
from core.protocol.client import NodeClient

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    n = Node(tmp_path / "agent", "agent", host="127.0.0.1", port=0, local_port=0,
             modules_dir=REPO / "modules", lost_after_s=60)  # fmt: skip
    n.start()
    yield n
    n.stop()


def agent_stop(node: Node, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    code = main(["--json", "--port", str(node.local.port), "agent", "stop"])
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return code, doc


def test_agent_stop_on_this_pc_lists_the_run_files(
    node: Node, capsys: pytest.CaptureFixture[str]
) -> None:
    run = node.data_dir / "runs" / "R20260926-1"
    (run / "raw" / "rx").mkdir(parents=True)
    (run / "raw" / "rx" / "ch1.jsonl").write_text("{}\n" * 10)
    code, doc = agent_stop(node, capsys)
    assert code == 0, doc
    data = doc["data"]
    assert isinstance(data, dict)
    assert data["stopped"] is True and data["state"] == "IDLE" and data["run"] == "R20260926-1"
    assert data["files"] == [{"name": "raw/rx/ch1.jsonl", "bytes": 30}]
    code, again = agent_stop(node, capsys)  # twice: the same answer
    assert code == 0 and again["data"] == data


def test_stop_during_a_test_is_a_warning_for_the_controller(
    node: Node, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Event] = []
    node.bus.subscribe(seen.append)
    node.flow.move(State.STARTING, reason="test")
    node.flow.move(State.RUNNING, reason="test")
    code, _ = agent_stop(node, capsys)
    assert code == 0 and node.flow.state is State.IDLE
    (warn,) = [e for e in seen if e.code == "ZL-USR-007"]
    assert warn.level == "warn" and "this PC" in warn.text


def test_the_controller_can_stop_one_node(node: Node) -> None:
    seen: list[Event] = []
    node.bus.subscribe(seen.append)
    node.flow.move(State.STARTING, reason="test")
    node.flow.move(State.RUNNING, reason="test")
    c = NodeClient("127.0.0.1", node.api.port, "ctl-boot")
    try:
        got = c.command("/v1/stop", 0)
    finally:
        c.close()
    assert got["stopped"] is True and got["state"] == "IDLE" and got["files"] == []
    assert not [e for e in seen if e.code == "ZL-USR-007"]  # not a stop on the PC itself
