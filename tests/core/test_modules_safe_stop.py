"""Module registry (12 1-3, 2, INS-024) and safe stop (F11, CTL-008)."""

from __future__ import annotations

import http.client
import json
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import core.blocks.resource_monitor as rm
from core.lifecycle.state_flow import State
from core.modules.registry import CORE_API, discover
from core.node import Node
from core.protocol.envelope import new_command

REPO = Path(__file__).resolve().parents[2]


def write_module(root: Path, mid: str, manifest: dict[str, Any] | str, code: str = "") -> None:
    folder = root / mid
    folder.mkdir(parents=True)
    text = manifest if isinstance(manifest, str) else json.dumps(manifest)
    (folder / "module.json").write_text(text)
    if code:
        (folder / "__init__.py").write_text("")
        (folder / "module.py").write_text(textwrap.dedent(code))


GOOD_CODE = """
    STOPS = []
    class M:
        def attach(self, node):
            node.module_data[__name__.split(".")[-2]] = lambda: {"ok": 1}
        def safe_stop(self, reason):
            STOPS.append(reason)
    def create():
        return M()
"""


def manifest(mid: str, **over: Any) -> dict[str, Any]:
    return {
        "id": mid,
        "name": mid,
        "protocol_version": 1,
        "core_api_min": CORE_API,
        "core_api_max": CORE_API,
        "entry": f"{mid}.module:create",
        **over,
    }


@pytest.fixture
def mods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mods"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))
    return root


def test_real_iptv_module_loads() -> None:
    found = {m.id: m for m in discover(REPO / "modules")}
    assert found["iptv"].enabled, found["iptv"].reason


def test_broken_modules_are_disabled_alone(mods: Path) -> None:
    write_module(mods, "good", manifest("good"), GOOD_CODE)
    write_module(mods, "badjson", "{nope")
    write_module(mods, "missing", {"id": "missing"})
    write_module(mods, "future", manifest("future", core_api_min=CORE_API + 1, core_api_max=9))
    write_module(mods, "wrongid", manifest("other"))
    write_module(mods, "crash", manifest("crash"), "def create():\n    raise RuntimeError('x')\n")
    write_module(mods, "shape", manifest("shape"), "def create():\n    return object()\n")
    (mods / "notamodule").mkdir()
    found = {m.id: m for m in discover(mods)}
    by_folder = {m.folder.name: m for m in found.values()}
    assert found["good"].enabled
    for name, words in {
        "badjson": "cannot be read",
        "missing": "missing name",
        "future": "needs Core API",
        "wrongid": "folder name",
        "crash": "failed",
        "shape": "attach() and safe_stop()",
    }.items():
        m = by_folder[name]
        assert not m.enabled and words in m.reason, (name, m.reason)
    assert "notamodule" not in by_folder


@pytest.fixture
def node(tmp_path: Path, mods: Path) -> Iterator[Node]:
    write_module(mods, "good", manifest("good"), GOOD_CODE)
    write_module(mods, "crash", manifest("crash"), "def create():\n    raise RuntimeError('x')\n")
    n = Node(
        tmp_path / "data",
        "receiver",
        host="127.0.0.1",
        port=0,
        local_port=0,
        modules_dir=mods,
        lost_after_s=1.0,
    )
    n.start()
    yield n
    n.stop()
    sys.modules.pop("good.module", None)
    sys.modules.pop("good", None)


def get(n: Node, path: str) -> dict[str, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", n.api.port, timeout=5)
    conn.request("GET", path)
    data: dict[str, Any] = json.loads(conn.getresponse().read())["data"]
    return data


def state(n: Node) -> State:
    """Read the state fresh (other threads change it)."""
    return n.flow.state


def test_node_lists_modules_and_reports_disabled(node: Node) -> None:
    info = get(node, "/v1/info")
    assert {m["id"]: m["enabled"] for m in info["modules"]} == {"crash": False, "good": True}
    hb = get(node, "/v1/heartbeat?seq=1")
    assert hb["module_data"] == {"good": {"ok": 1}}
    assert any(e["code"] == "ZL-INS-024" for e in hb["events"])


def test_silent_controller_triggers_safe_stop(node: Node) -> None:
    stops = sys.modules["good.module"].STOPS
    node.flow.move(State.STARTING)
    node.flow.move(State.RUNNING)
    get(node, "/v1/heartbeat?seq=1")
    gen = node.guard.generation
    deadline = time.monotonic() + 5
    while node.flow.state is not State.IDLE and time.monotonic() < deadline:
        time.sleep(0.05)
    assert node.flow.state is State.IDLE
    assert len(stops) == 1 and "no request from the Controller" in stops[0]
    assert node.guard.generation == gen + 1
    old = node.guard.run(new_command(gen), lambda r: None)
    assert old["error"]["code"] == "ZL-DSC-011"
    codes = [e["code"] for e in get(node, "/v1/heartbeat?seq=2")["events"]]
    assert "ZL-CTL-008" in codes


def test_heartbeats_keep_the_run_alive(node: Node) -> None:
    node.flow.move(State.STARTING)
    node.flow.move(State.RUNNING)
    for i in range(8):  # 2 s of heartbeats, limit is 1 s
        get(node, f"/v1/heartbeat?seq={i}")
        time.sleep(0.25)
    assert node.flow.state is State.RUNNING


def test_idle_node_is_not_stopped(node: Node) -> None:
    time.sleep(1.8)
    assert sys.modules["good.module"].STOPS == []


def test_controller_restart_stops_the_run(node: Node) -> None:
    node.api.command("/v1/noop", lambda r: {})
    node.flow.move(State.STARTING)

    def post(boot: str, cid: str) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", node.api.port, timeout=5)
        body = json.dumps({"command_id": cid, "generation": node.guard.generation})
        conn.request("POST", "/v1/noop", body=body, headers={"X-ZL-Controller-Boot": boot})
        conn.getresponse().read()

    post("A", "1")
    assert node.flow.state is State.STARTING
    post("B", "2")
    assert state(node) is State.IDLE
    assert sys.modules["good.module"].STOPS == ["Controller restarted"]


def test_failing_module_stop_ends_in_fault(node: Node) -> None:
    good = next(m for m in node.modules if m.id == "good")
    assert good.instance is not None

    def broken(reason: str) -> None:
        raise OSError("device busy")

    good.instance.safe_stop = broken  # type: ignore[method-assign]
    node.flow.move(State.STARTING)
    assert node.safe_stop("test") is False
    assert node.flow.state is State.FAULT
    good.instance.safe_stop = lambda reason: None  # type: ignore[method-assign]
    assert node.safe_stop("retry") is True  # FAULT -> STOPPING -> IDLE
    assert state(node) is State.IDLE


def test_cpu_reading_keeps_last_value_within_one_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([(0, 100), (50, 200), (50, 200), (60, 300)])
    monkeypatch.setattr(rm, "_cpu_times", lambda: next(times))
    mon = rm.ResourceMonitor(Path("."))
    assert mon.read().cpu_pct is None
    assert mon.read().cpu_pct == 50.0
    assert mon.read().cpu_pct == 50.0  # no tick passed
    assert mon.read().cpu_pct == 10.0
