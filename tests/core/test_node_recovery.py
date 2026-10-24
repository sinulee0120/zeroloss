"""F12 (20 GAP-47): a node restarted after a crash closes the record files left open,
and its 5-minute summaries survive the restart with the same seq numbers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.node import Node
from core.protocol.client import NodeClient

REPO = Path(__file__).resolve().parents[2]


def node(tmp: Path) -> Node:
    return Node(tmp, "agent", host="127.0.0.1", port=0, local_port=0, modules_dir=REPO / "modules")


def test_open_record_files_are_closed_on_start(tmp_path: Path) -> None:
    raw = tmp_path / "runs" / "R1" / "raw" / "pc"
    raw.mkdir(parents=True)
    f = raw / "R1_h000.jsonl"
    lines = [{"type": "header", "run_id": "R1"}, {"type": "s", "e": 0}, {"type": "s", "e": 1}]
    f.write_text("\n".join(json.dumps(x) for x in lines) + '\n{"type":"s","e":2,"rx', "utf-8")
    n = node(tmp_path)
    n.start()
    n.stop()
    got = [json.loads(x) for x in f.read_text("utf-8").splitlines()]
    assert got[-1]["type"] == "footer" and got[-1]["end_reason"] == "recovered"
    assert got[-1]["lines"] == 2 and len(got) == 4  # the cut line is gone


def test_summaries_survive_a_restart(tmp_path: Path) -> None:
    pending: list[dict[str, Any]] = [{"device": "pc", "index": 0}, {"device": "pc", "index": 1}]

    def source() -> list[dict[str, Any]]:
        out, pending[:] = list(pending), []
        return out

    n = node(tmp_path)
    n.summary_sources.append(source)
    n.start()
    c = NodeClient("127.0.0.1", n.api.port, "boot-1", "n")
    first = c.get("/v1/summaries", after=0)
    c.close()
    n.stop()
    assert [s["seq"] for s in first["summaries"]] == [1, 2]
    n2 = node(tmp_path)  # restarted: nothing in memory any more
    pending.append({"device": "pc", "index": 2})
    n2.summary_sources.append(source)
    n2.start()
    c = NodeClient("127.0.0.1", n2.api.port, "boot-2", "n")
    again = c.get("/v1/summaries", after=1)
    c.close()
    n2.stop()
    assert [(s["seq"], s["index"]) for s in again["summaries"]] == [(2, 1), (3, 2)]
    assert again["last_seq"] == 3


def test_disk_low_warns_then_stops_the_node(tmp_path: Path) -> None:
    """09 RCV-008: below 1 GB a warning (cleared when it grows again), below 200 MB the
    node stops itself safely, even with no Controller."""
    from core.events.bus import Event
    from core.lifecycle.state_flow import State

    free = {"mb": 5000}
    n = node(tmp_path)
    n.disk_free_mb = lambda: free["mb"]
    n.disk_check_s = 0.1
    events: list[Event] = []
    n.bus.subscribe(events.append)
    n.start()
    try:
        n.flow.move(State.STARTING, reason="test")
        n.flow.move(State.RUNNING, reason="test")

        def wait(pred: Any) -> bool:
            import time

            end = time.monotonic() + 5
            while time.monotonic() < end:
                if pred():
                    return True
                time.sleep(0.05)
            return False

        free["mb"] = 800
        assert wait(lambda: any(e.code == "ZL-RCV-008" and e.level == "warn" for e in events))
        free["mb"] = 3000
        assert wait(lambda: any((e.data or {}).get("clears") == ["ZL-RCV-008"] for e in events))
        free["mb"] = 150
        assert wait(lambda: n.flow.state is State.IDLE)
        assert any(e.code == "ZL-RCV-008" and e.level == "error" for e in events)
    finally:
        n.stop()
