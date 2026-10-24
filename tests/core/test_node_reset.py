"""Node ``POST /v1/reset`` (02 4, F11 Reset All): stop everything, stop what a crash
left behind, report a clean state; something still running is not clean."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

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


def reset(node: Node) -> dict[str, Any]:
    c = NodeClient("127.0.0.1", node.api.port, "controller-boot")
    try:
        return c.command("/v1/reset", 5, None, timeout_s=30)
    finally:
        c.close()


def test_idle_node_is_clean(node: Node) -> None:
    r = reset(node)
    assert r["clean"] is True and r["boot_id"] == node.boot_id
    names = [c["name"] for c in r["checks"]]
    assert names == ["state", "processes", "modules", "network settings"]


def test_leftover_record_is_cleared_and_counted(node: Node) -> None:
    node.procs_dir.mkdir(parents=True, exist_ok=True)
    (node.procs_dir / "tsp_ch9.json").write_text(json.dumps({"pid": 999999, "token": "x"}))
    r = reset(node)
    left = {x.stem for x in node.procs_dir.glob("*.json")}
    assert r["clean"] is True and left <= {"iw-event"}  # only the node's own helper stays
    procs = next(c for c in r["checks"] if c["name"] == "processes")
    assert procs["detail"] == "1 leftover stopped"


def test_a_module_still_busy_is_not_clean(node: Node, monkeypatch: pytest.MonkeyPatch) -> None:
    iptv = next(m.instance for m in node.modules if m.id == "iptv")
    monkeypatch.setattr(iptv, "clean_check", lambda: ["CH1 sender"])
    r = reset(node)
    assert r["clean"] is False
    mods = next(c for c in r["checks"] if c["name"] == "modules")
    assert not mods["ok"] and mods["detail"] == "iptv: CH1 sender"


def test_reset_keeps_the_nodes_own_helper(node: Node) -> None:
    """Regression (found 2026-09-25): the node's own ``iw event`` helper is not a test
    program; Reset All must not stop it or count it as a leftover."""
    node.procs_dir.mkdir(parents=True, exist_ok=True)
    helper = node.procs_dir / "iw-event.json"
    had = helper.exists()
    if not had:
        helper.write_text(json.dumps({"pid": 999998, "token": "y"}))
        node._wifi_events = object()  # type: ignore[assignment]  # as if it were running
    try:
        r = reset(node)
        assert r["clean"] is True and helper.exists()
        procs = next(c for c in r["checks"] if c["name"] == "processes")
        assert procs["detail"] == "none left"
    finally:
        if not had:
            node._wifi_events = None
            helper.unlink(missing_ok=True)
