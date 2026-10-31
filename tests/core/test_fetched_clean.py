"""A run the Controller has fetched is removed from the node at its next start or test
(2026-10-31 user: "다시 시작하면 기존 저장된 캡춰화면이나 정보들 다 지워 메모리 쌓이지 않게")."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.node import FETCHED, Node
from core.protocol.envelope import Request


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    n = Node(tmp_path / "agent", "agent", host="127.0.0.1", port=0, local_port=0)
    yield n


def _run(node: Node, run: str) -> Path:
    raw = node.data_dir / "runs" / run / "raw" / "tablet-1"
    raw.mkdir(parents=True)
    (raw / f"{run}_h000.jsonl").write_text("{}\n")
    (raw / f"{run}_ev_e000012_ch1.jpg").write_bytes(b"grid")
    return raw.parent


def test_only_fetched_runs_are_cleaned(node: Node) -> None:
    got = _run(node, "R20261031-100000")
    kept = _run(node, "R20261031-110000")  # not fetched yet: stays until it is
    r = node.records_fetched(Request("ctl", 1, None, {"run": "R20261031-100000"}))
    assert r["marked"] and (got / FETCHED).is_file()
    assert node.clean_fetched() == ["R20261031-100000"]
    assert not any(got.rglob("*.*")) and list(kept.rglob("*.jpg"))
    assert node.clean_fetched() == []  # nothing left to clean
    assert not node.records_fetched(Request("ctl", 1, None, {"run": "R20261031-120000"}))["marked"]
