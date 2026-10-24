"""Every data format in docs/02-data.md matches what the code really writes (20 G-01, G-02).

One real test runs on loopback (sender + PC receiver + Controller service); every file
and answer it produces is compared, key by key, with the JSON example in the spec:
- a key in the spec but not in the output, or in the output but not in the spec, fails;
- ``null`` in the output is allowed (not measured in this phase) and is not descended;
- ``{…}`` in the spec means "free form" and is not descended;
- in a list, each output element is compared with the spec element that fits it best.
Adding or renaming a field means changing docs/02-data.md first.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.desktop.link import DesktopLink
from controller.service import ControllerService
from core.blocks.discovery import ask
from core.events.bus import Event
from core.node import Node
from core.protocol.client import NodeClient
from modules.iptv.defaults import DEFAULTS as IPTV_DEFAULTS
from modules.iptv.sender.commands import QUALITIES, prepare_command

ROOT = Path(__file__).resolve().parent.parent
HAVE_TOOLS = bool(shutil.which("tsp") and shutil.which("ffmpeg"))
FREE = {"__free__": True}  # marker for {…} in the spec
# Objects keyed by data (device names, channel numbers, setting IDs, IPs), not by
# field names: only the values' shape is compared.
MAPS = {
    "summary_json.devices_rtt_ms",
    "summary_json.receiver_baseline",
    "summary_json.source",
    "summary_json.settings_changed",
    "summary_json.versions.agents",
    "run_state.settings",
    "devices_json.devices[].best_link_mbps",
    "ctl_status.run.devices",  # entries: test_controller_status
    "ctl_status.run.cells",
    "ctl_status.run.plan.settings",  # setting id -> value, like run_state.settings
    "heartbeat.module_data.throughput.clients",  # card address -> its list (GAP-171)
}
# Paths whose inside another section defines (checked by their own tests).
ELSEWHERE = {
    "heartbeat.module_data.iptv.receivers[].last_1s",  # 02 7 (test_spec_consistency)
    "heartbeat.events",  # 02 8
    "desktop_status.problems",  # 02 8 (events)
}


def spec_example(heading: str) -> Any:
    """The first ```json block under a '## <heading>' line of docs/02-data.md."""
    text = (ROOT / "docs" / "02-data.md").read_text(encoding="utf-8")
    start = text.index(f"\n## {heading}")
    block = re.search(r"```json\n(.*?)\n```", text[start:], re.S)
    assert block, heading
    body = block.group(1)
    body = body.replace("{…}", '{"__free__": true}').replace("[…]", "[]")
    return json.loads(body)


def spec_example_n(heading: str, n: int) -> Any:
    """The n-th ```json block of a '## <heading>' section (0 = first)."""
    text = (ROOT / "docs" / "02-data.md").read_text(encoding="utf-8")
    start = text.index(f"\n## {heading}")
    end = text.find("\n## ", start + 4)
    blocks = re.findall(r"```json\n(.*?)\n```", text[start : end if end > 0 else None], re.S)
    body = blocks[n].replace("{…}", '{"__free__": true}').replace("[…]", "[]")
    return json.loads(body)


def spec_lines(heading: str) -> list[Any]:
    text = (ROOT / "docs" / "02-data.md").read_text(encoding="utf-8")
    start = text.index(f"\n## {heading}")
    block = re.search(r"```json\n(.*?)\n```", text[start:], re.S)
    assert block, heading
    return [json.loads(x) for x in block.group(1).splitlines() if x.strip()]


def diff(spec: Any, real: Any, path: str = "") -> list[str]:
    if path in ELSEWHERE or real is None or spec == FREE:
        return []
    if path in MAPS:
        return [] if isinstance(real, dict) else [f"{path}: spec has a map, output has {real!r}"]
    if isinstance(spec, dict):
        if not isinstance(real, dict):
            return [f"{path}: spec has an object, output has {type(real).__name__}"]
        at = f"{path}." if path else ""
        out = [f"{at}{k}: in spec, missing in output" for k in spec if k not in real]
        out += [f"{at}{k}: in output, missing in spec" for k in real if k not in spec]
        for k in spec:
            if k in real:
                out += diff(spec[k], real[k], f"{path}.{k}" if path else k)
        return out
    if isinstance(spec, list):
        if not isinstance(real, list):
            return [f"{path}: spec has a list, output has {type(real).__name__}"]
        if not spec or not real or not isinstance(spec[0], dict):
            return []
        out = []
        for item in real:
            best = min(spec, key=lambda s: len(diff(s, item, f"{path}[]")))
            out += diff(best, item, f"{path}[]")
        return out
    return []


@pytest.fixture(scope="module")
def outputs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    if not HAVE_TOOLS:
        pytest.skip("needs ffmpeg and tsp")
    t = tmp_path_factory.mktemp("formats")
    hd = t / "ch1.ts"
    subprocess.run(
        prepare_command("ffmpeg", QUALITIES["HD"], 1, hd, seconds=4, preset="ultrafast"),
        check=True,
        capture_output=True,
        timeout=300,
    )
    svc = ControllerService(
        t / "ctl", t / "reports", ROOT / "modules", host="127.0.0.1", port=0, local_port=0
    )
    agent = Node(
        t / "agent", "agent", host="127.0.0.1", port=0, local_port=0, modules_dir=ROOT / "modules"
    )
    svc.start()
    agent.start()
    out: dict[str, Any] = {}
    try:
        c = NodeClient("127.0.0.1", svc.node.local.port, "formats", "svc")

        def gen() -> int:
            return int(c.get("/v1/controller/status")["generation"])

        c.command(
            "/v1/controller/devices/add",
            gen(),
            name="local",
            host="127.0.0.1",
            port=agent.api.port,
        )
        plan = {
            "schema_version": 1,
            "module": "iptv",
            "test_type": "pc",
            "devices": {"sender": "local", "receivers": [{"device": "local", "ch": 1}]},
            "quality": "HD",
            "duration_s": 12,
            "settings": {
                "S-14": {"groups": ["239.255.91.1", "239.255.92.1", "239.255.93.1"], "port": 5004}
            },
            "nics": {"local": "127.0.0.1"},
            "files": {"1": str(hd)},
        }
        run_id = c.command("/v1/controller/run", gen(), timeout_s=120, plan=plan)["run_id"]
        time.sleep(8)
        a = NodeClient("127.0.0.1", agent.api.port, svc.run.boot_id, "agent")
        out["heartbeat"] = a.get("/v1/heartbeat", seq=1)
        out["health"] = a.get("/v1/health")
        out["ctl_status"] = c.get("/v1/controller/status")
        svc.node.bus.publish(  # one problem, so the list has a row to compare
            Event("ZL-RCV-020", "warn", "Player is not playing", device="local")
        )
        out["problems"] = c.get("/v1/controller/problems")
        svc.node.bus.publish(Event("ZL-NET-013", "error", "link short", device="local"))
        asked = svc.actions.clock
        svc.actions.clock = lambda: asked() + 60.0  # past grace_s (GAP-135): the row shows
        out["actions"] = c.get("/v1/controller/actions")
        svc.actions.clock = asked
        out["run_state_running"] = json.loads(
            (t / "reports" / run_id / "run-state.json").read_text()
        )
        end = time.monotonic() + 60
        while c.get("/v1/controller/status")["run"]["state"] != "IDLE":
            assert time.monotonic() < end, "run did not finish"
            time.sleep(0.5)
        rdir = t / "reports" / run_id
        out["summary_piece"] = [
            json.loads(x) for x in (rdir / "summaries.jsonl").read_text().splitlines()
        ]
        out["run_state"] = json.loads((rdir / "run-state.json").read_text())
        out["summary_json"] = json.loads((rdir / "summary.json").read_text())
        out["devices_json"] = json.loads((t / "ctl" / "devices.json").read_text())
        out["events"] = [
            json.loads(x) for x in (t / "agent" / "logs" / "events.log").read_text().splitlines()
        ]
        # 4-1: the discovery answer of the idle node, over UDP
        found = [b for b, _ in ask(["127.0.0.1"], agent.api.port, wait_s=1.0)
                 if b.get("device_id") == agent.device_id]  # fmt: skip
        out["discovery"] = found[0]  # asked 3 times (lost datagrams), answered each time
        # 2-12: what Agent Desktop reads and sends (a real DesktopLink, fake player part)
        agent.desktop.want("R1/local/ch1/player", "iptv", "player",
                           {"url": "rtp://@239.255.91.1:5004", "nic": "lo", "buffer_ms": 1000,
                            "window": str(IPTV_DEFAULTS["S-50"]), "headless": True,
                            "lowered": False, "flowing": True})  # fmt: skip
        sent: list[dict[str, Any]] = []
        link = DesktopLink(agent.local.port, {("iptv", "player"): _FakePlayer}, t / "desktop")
        real_command = link._client.command

        def spy(path: str, generation: int, **kw: Any) -> dict[str, Any]:
            if path == "/v1/desktop/report":
                sent.append({k: kw[k] for k in ("desktop_id", "reports")})
            return real_command(path, generation, **kw)

        link._client.command = spy  # type: ignore[method-assign,assignment]
        local = NodeClient("127.0.0.1", agent.local.port, "formats", "agent-local")
        out["desktop_jobs"] = local.get("/v1/desktop/jobs", desktop_id=link.desktop_id)
        link.step()
        link.step()
        out["desktop_report"] = sent[-1]
        out["desktop_status"] = link.status
        link.stop()
        yield out
    finally:
        svc.stop()
        agent.stop()


def check(spec: Any, real: Any, name: str) -> None:
    problems = diff(spec, real, name)
    assert not problems, "\n".join(problems)


def test_health(outputs: dict[str, Any]) -> None:
    documented = re.search(
        r"`GET /v1/health` \| [^|]*\| ([^|]*)\|",
        (ROOT / "docs" / "02-data.md").read_text(encoding="utf-8"),
    )
    assert documented
    keys = set(re.findall(r"`([a-z_]+)`", documented.group(1)))
    assert set(outputs["health"]) == keys


def test_heartbeat(outputs: dict[str, Any]) -> None:
    check(spec_example("5. heartbeat"), outputs["heartbeat"], "heartbeat")


def test_five_minute_summary(outputs: dict[str, Any]) -> None:
    spec = spec_example("6. 5분 요약") | {"seq": 0, "node": "", "trouble": None}  # 6: added
    assert outputs["summary_piece"]
    for piece in outputs["summary_piece"]:
        check(spec, piece, "summary")


def test_events_log(outputs: dict[str, Any]) -> None:
    (spec,) = spec_lines("8. 사건 기록")
    for e in outputs["events"]:
        extra = set(e) - set(spec)
        missing = set(spec) - set(e) - {"data"}  # data is left out when empty (02 8)
        assert not extra and not missing, (e, extra, missing)


def test_run_state(outputs: dict[str, Any]) -> None:
    spec = spec_example("9. 시험 상태 파일")
    check(spec, outputs["run_state_running"], "run_state")
    check(spec, outputs["run_state"], "run_state")


def test_summary_json(outputs: dict[str, Any]) -> None:
    check(spec_example("10. 결과 요약"), outputs["summary_json"], "summary_json")


def test_devices_json(outputs: dict[str, Any]) -> None:
    check(spec_example("11-0. 장비 목록"), outputs["devices_json"], "devices_json")


def test_controller_status(outputs: dict[str, Any]) -> None:
    """02 2-1, read while the run is measuring."""
    spec = spec_example("2-1. Controller 창구 상태")
    real = outputs["ctl_status"]
    check(spec, real, "ctl_status")
    assert real["run"]["state"] == "RUNNING"
    ((_, dev_spec),) = spec["run"]["devices"].items()
    ((_, cell_spec),) = spec["run"]["cells"].items()
    assert real["run"]["devices"]
    for name, dev in real["run"]["devices"].items():
        check(dev_spec, dev, f"ctl_status.run.devices.{name}")
        assert dev["row"]["items"], "a device in a test always has a row"
    for name, cells in real["run"]["cells"].items():
        check(cell_spec, cells, f"ctl_status.run.cells.{name}")


def test_problems_list(outputs: dict[str, Any]) -> None:
    """02 2-2 (F21)."""
    real = outputs["problems"]
    check(spec_example("2-2. 문제 목록"), real, "problems")
    assert any(r["code"] == "ZL-RCV-020" for r in real["problems"])


def test_actions_list(outputs: dict[str, Any]) -> None:
    """02 2-3 (F22)."""
    real = outputs["actions"]
    check(spec_example("2-3. 사람 할 일 목록"), real, "actions")
    assert real["waiting"] >= 1


def test_discovery_answer(outputs: dict[str, Any]) -> None:
    check(spec_example("4-1. 자동 발견"), outputs["discovery"], "discovery")


@pytest.mark.parametrize(("n", "name"), [(0, "desktop_status"), (1, "desktop_jobs"),
                                         (2, "desktop_report")])  # fmt: skip
def test_agent_desktop_formats(outputs: dict[str, Any], n: int, name: str) -> None:
    real = outputs[name]
    assert real, name
    check(spec_example_n("2-12. Agent Desktop", n), real, name)


def test_the_checker_itself() -> None:
    spec = {"a": 1, "b": {"c": 1}, "d": [{"x": 1}, {"y": 1}], "f": FREE}
    assert diff(spec, {"a": 1, "b": {"c": 2}, "d": [{"x": 2}, {"y": 3}], "f": {"z": 1}}) == []
    assert diff(spec, {"a": 1, "b": None, "d": [], "f": None}) == []
    got = diff(spec, {"a": 1, "b": {"c": 1, "e": 2}, "d": [{"q": 1}], "f": {}})
    assert got == [
        "b.e: in output, missing in spec",
        "d[].x: in spec, missing in output",
        "d[].q: in output, missing in spec",
    ]
    assert diff(spec, {"b": {"c": 1}, "d": [], "f": {}}) == ["a: in spec, missing in output"]


class _FakePlayer:
    """The Desktop's player part without a real player (the format is what is compared)."""

    def __init__(self, config: dict[str, Any], *_: Any) -> None:
        from core.lifecycle.state_flow import State, StateFlow

        self._flow = StateFlow("fake", lambda *a: None)
        self._flow.move(State.STARTING)
        self._flow.move(State.RUNNING)

    @property
    def state(self) -> str:
        return self._flow.state.value

    def start(self) -> None:
        return None

    def stop(self) -> None:
        from core.lifecycle.state_flow import State

        self._flow.move(State.STOPPING)
        self._flow.move(State.IDLE)

    def change(self, config: dict[str, Any]) -> None:
        return None

    def report(self) -> dict[str, Any]:
        return {"second": {"playback": "ok", "dropped": 0, "audio_gaps": 0,
                           "stall_started": False, "restarted": False, "decode_errors": 0,
                           "decoder_dropped": 0},
                "first_frame": True, "lowered": False}  # fmt: skip
