"""Whole-PC CPU overload during a test (docs/09-exceptions.md SYS-003)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from core.blocks.cpu_watch import CpuWatch, top_processes
from core.lifecycle.state_flow import State
from core.node import Node


def test_more_than_ten_seconds_above_ninety() -> None:
    w = CpuWatch()
    assert [w.update(95.0) for _ in range(10)] == [None] * 10  # 10 s is not "more than"
    assert w.update(95.0) == "start" and w.busy
    assert w.update(99.0) is None  # told once
    assert w.update(None) is None  # an unknown reading changes nothing
    assert w.update(50.0) == "end" and not w.busy
    for _ in range(5):
        w.update(95.0)
    assert w.update(80.0) is None  # a short burst is not overload
    for _ in range(11):
        w.update(92.0)
    assert w.reset() == "end" and w.reset() is None


def test_top_processes_names_a_busy_program() -> None:
    """The busy program is another process: a thread spinning in this one holds the GIL,
    so reading /proc here would wait until it stopped (the reading saw nothing)."""
    import subprocess
    import sys

    busy = subprocess.Popen([sys.executable, "-c", "import time\n"
                             "end = time.monotonic() + 3\n"
                             "while time.monotonic() < end:\n    pass"])  # fmt: skip
    try:
        time.sleep(0.2)  # started and spinning
        top = top_processes(n=3, sample_s=0.3)
    finally:
        busy.kill()
        busy.wait(5)
    assert top and all({"name", "pid", "pct"} <= set(p) for p in top)
    (mine,) = [p for p in top if p["pid"] == busy.pid]
    assert float(str(mine["pct"])) > 20  # one core spinning


def test_node_warns_and_clears_only_during_a_test(tmp_path: Path, monkeypatch: Any) -> None:
    node = Node(tmp_path / "node", "agent", host="127.0.0.1", port=0, local_port=0)
    got: list[Any] = []
    node.bus.subscribe(got.append)
    cpu = {"pct": 99.0}

    class Fake:
        def read(self) -> Any:
            class R:
                cpu_pct = cpu["pct"]

                def to_dict(self) -> dict[str, Any]:
                    return {"cpu_pct": cpu["pct"], "mem_free_pct": None,
                            "disk_free_mb": None, "proc_mem_mb": None}  # fmt: skip

            return R()

    monkeypatch.setattr(node, "resources", Fake())
    monkeypatch.setattr("core.node.top_processes", lambda: [{"name": "x", "pid": 1, "pct": 97.0}])
    for _ in range(15):  # idle: never
        node._watch_cpu()
    assert not any(e.code == "ZL-SYS-003" for e in got)
    node.flow.move(State.STARTING)
    for _ in range(11):
        node._watch_cpu()
    (warn,) = [e for e in got if e.code == "ZL-SYS-003"]
    assert warn.level == "warn" and "x 97%" in warn.text and warn.data["top"][0]["name"] == "x"
    node.flow.move(State.STOPPING)
    node.flow.move(State.IDLE)
    node._watch_cpu()  # the test ended: the busy period ends with it
    assert got[-1].data.get("clears") == ["ZL-SYS-003"]
