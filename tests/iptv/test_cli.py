"""zeroloss CLI end to end (docs/13-ai-loop.md 2): commands, --json form, fixed exit codes.
The Controller service and one agent node run here; all test traffic stays on loopback."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from controller.cli import main
from controller.service import ControllerService
from core.node import Node
from modules.iptv.sender.commands import QUALITIES, prepare_command

REPO = Path(__file__).resolve().parents[2]
HAVE_TOOLS = bool(shutil.which("tsp") and shutil.which("ffmpeg"))


@pytest.fixture(scope="module")
def hd_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not HAVE_TOOLS:
        pytest.skip("needs ffmpeg and tsp")
    out = tmp_path_factory.mktemp("prep") / "ch1.ts"
    subprocess.run(
        prepare_command("ffmpeg", QUALITIES["HD"], 1, out, seconds=4, preset="ultrafast"),
        check=True,
        timeout=300,
    )
    return out


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[tuple[ControllerService, Node]]:
    svc = ControllerService(
        tmp_path / "ctl",
        tmp_path / "reports",
        REPO / "modules",
        host="127.0.0.1",
        port=0,
        local_port=0,
        lost_s=4,
    )
    agent = Node(
        tmp_path / "agent",
        "agent",
        host="127.0.0.1",
        port=0,
        local_port=0,
        modules_dir=REPO / "modules",
        lost_after_s=30,
    )
    svc.start()
    agent.start()
    yield svc, agent
    svc.stop()
    agent.stop()


def run_cli(
    svc: ControllerService, capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, Any]:
    code = main(["--json", "--port", str(svc.node.local.port), *args])
    out = capsys.readouterr().out.strip().splitlines()[-1]
    doc = json.loads(out)
    assert set(doc) == {"ok", "exit_code", "codes", "data"} and doc["exit_code"] == code
    return code, doc


def plan_file(tmp_path: Path, hd_file: Path, **over: Any) -> Path:
    plan = {
        "schema_version": 1,
        "module": "iptv",
        "test_type": "pc",
        "devices": {"sender": "local", "receivers": [{"device": "local", "ch": 1}]},
        "quality": "HD",
        "duration_s": 10,
        "settings": {
            "S-14": {"groups": ["239.255.91.1", "239.255.92.1", "239.255.93.1"], "port": 5004}
        },
        "nics": {"local": "127.0.0.1"},
        "files": {"1": str(hd_file)},
    }
    plan.update(over)
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan))
    return p


@pytest.mark.skipif(not HAVE_TOOLS, reason="needs ffmpeg and tsp")
def test_devices_run_wait_report_status(
    lab: Any, hd_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    svc, agent = lab
    code, doc = run_cli(
        svc, capsys, "devices", "add", "local", "127.0.0.1", "--node-port", str(agent.api.port)
    )
    assert code == 0 and doc["data"]["name"] == "local"
    code, doc = run_cli(svc, capsys, "devices", "list")
    assert code == 0 and [d["name"] for d in doc["data"]["devices"]] == ["local"]
    code, doc = run_cli(svc, capsys, "run", "--plan", str(plan_file(tmp_path, hd_file)), "--wait")
    assert code == 0 and doc["data"]["verdict"] == "PASS", doc
    run_id = doc["data"]["run_id"]
    code, doc = run_cli(svc, capsys, "report", "--run", run_id)
    assert code == 0 and doc["data"]["verdict"] == "PASS"
    code, doc = run_cli(svc, capsys, "status")
    assert code == 0 and doc["data"]["run"]["state"] == "IDLE"


def test_refusals_and_error_codes(
    lab: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    svc, _ = lab
    fake = tmp_path / "x.ts"
    fake.write_bytes(b"")
    code, doc = run_cli(
        svc, capsys, "run", "--plan", str(plan_file(tmp_path, fake, duration_s=90000))
    )
    assert code == 4  # longer than 24 h needs --yes
    code, doc = run_cli(
        svc, capsys, "run", "--plan", str(plan_file(tmp_path, fake, test_type="tablet"))
    )
    assert code == 6 and doc["codes"] == ["ZL-RUN-014"]  # a tablet test names its tablets
    code, doc = run_cli(svc, capsys, "run", "--plan", str(plan_file(tmp_path, fake, quality="8K")))
    assert code == 6 and doc["codes"] == ["ZL-RUN-014"]
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    code, _ = run_cli(svc, capsys, "run", "--plan", str(bad))
    assert code == 6
    code, doc = run_cli(svc, capsys, "stop")
    assert code == 3 and doc["codes"] == ["ZL-RUN-012"]  # nothing running
    code, doc = run_cli(svc, capsys, "report", "--run", "R20990101-000000")
    assert code == 3 and doc["codes"] == ["ZL-RPT-008"]
    code, doc = run_cli(svc, capsys, "report", "--run", "../../etc/passwd")
    assert code == 6  # run ID shape checked: no path escape


def test_no_controller_is_exit_5(capsys: pytest.CaptureFixture[str]) -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    code = main(["--json", "--port", str(port), "status"])
    doc = json.loads(capsys.readouterr().out.strip())
    assert code == 5 and doc["codes"] == ["ZL-DSC-014"]


def test_bad_command_is_exit_6(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["no-such-command"]) == 6


@pytest.mark.skipif(not HAVE_TOOLS, reason="needs ffmpeg and tsp")
def test_korean_file_name_end_to_end(
    lab: Any, hd_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """iptv 1-1-1: Korean, spaces, brackets, emoji; the plan names it in NFD (Mac copy)."""
    import unicodedata

    svc, agent = lab
    name = "시험 영상 HD (1) 🎬.ts"
    korean = tmp_path / "한글 폴더" / name
    korean.parent.mkdir()
    shutil.copy(hd_file, korean)
    nfd = unicodedata.normalize("NFD", str(korean))
    assert nfd != str(korean)
    run_cli(svc, capsys, "devices", "add", "local", "127.0.0.1", "--node-port", str(agent.api.port))
    plan = plan_file(tmp_path, hd_file, files={"1": nfd})
    code, doc = run_cli(svc, capsys, "run", "--plan", str(plan), "--wait")
    assert code == 0 and doc["data"]["verdict"] == "PASS", doc
    run_dir = svc.reports_dir / doc["data"]["run_id"]
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["source"] == {"1": name}  # shown in NFC, as given
    assert name in (run_dir / "summary.json").read_text(encoding="utf-8")  # not \uXXXX
    assert "시험 영상" in (run_dir / "report.html").read_text(encoding="utf-8")


def test_problems_and_actions_from_the_cli(lab: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """13-ai-loop: the same lists as the screen (F21, F22), and [Done] from the CLI."""
    from core.events.bus import Event

    svc, _ = lab
    svc.actions._grace = False  # GAP-135: shown at once here, not after its wait
    svc.node.bus.publish(Event("ZL-SYS-006", "error", "tsp did not stop", device="rx"))
    code, doc = run_cli(svc, capsys, "problems")
    assert code == 0 and doc["data"]["problems"][0]["code"] == "ZL-SYS-006"
    code, doc = run_cli(svc, capsys, "actions")
    a = doc["data"]["actions"][0]
    assert code == 0 and a["action_id"] == "human.pc.power_cycle" and a["state"] == "waiting"
    code, doc = run_cli(svc, capsys, "actions", "done", a["key"])
    assert code == 0
    code, doc = run_cli(svc, capsys, "actions")
    assert doc["data"]["waiting"] == 0


def test_prepare_from_the_cli(lab: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """13-ai-loop `zeroloss prepare` (02 2-4, iptv 1-1-2): bad input is exit 6, --wait
    returns when the video is made, --status shows it."""
    svc, agent = lab
    prep = next(m.instance for m in agent.modules if m.id == "iptv").preparer
    prep.seconds, prep.preset = 2.0, "ultrafast"
    run_cli(svc, capsys, "devices", "add", "tx", "127.0.0.1", "--node-port", str(agent.api.port))
    assert run_cli(svc, capsys, "prepare", "--sender", "tx")[0] == 6  # no --quality
    assert run_cli(svc, capsys, "prepare", "--sender", "tx", "--quality", "HD", "--ch", "x")[0] == 6
    code, doc = run_cli(svc, capsys, "prepare", "--sender", "tx", "--quality", "HD", "--ch", "4")
    assert code == 6 and doc["codes"] == ["ZL-RUN-014"]
    code, doc = run_cli(svc, capsys, "prepare", "--sender", "tx", "--quality", "HD", "--ch", "2",
                        "--wait")  # fmt: skip
    assert code == 0, doc
    assert [(c["ch"], c["state"]) for c in doc["data"]["channels"]] == [(2, "ready")]
    code, doc = run_cli(svc, capsys, "prepare", "--sender", "tx", "--status")
    assert code == 0 and doc["data"]["busy"] is False
    code, doc = run_cli(svc, capsys, "prepare", "--sender", "tx", "--quality", "HD", "--ch", "2")
    assert doc["data"]["channels"] == [{"ch": 2, "state": "ready", "source": "generated"}]


@pytest.mark.skipif(not HAVE_TOOLS, reason="needs ffmpeg and tsp")
def test_precheck_from_the_cli(
    lab: Any, hd_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """13-ai-loop: ``zeroloss precheck`` runs F17 1-5 (with the test send), starts no
    test, exit 0 when nothing failed and 3 with the failed step's code otherwise."""
    svc, agent = lab
    run_cli(svc, capsys, "devices", "add", "local", "127.0.0.1", "--node-port",
            str(agent.api.port))  # fmt: skip
    code, doc = run_cli(svc, capsys, "precheck", "--plan", str(plan_file(tmp_path, hd_file)))
    assert code == 0 and doc["data"]["kind"] == "precheck", doc
    states = [s["state"] for s in doc["data"]["steps"]]  # 4: PC speed still measuring (warn)
    assert states == ["ok", "ok", "ok", "warn", "ok", "skipped", "skipped"]
    assert svc.run.state.value == "IDLE" and svc.run.result is None
    agent.tools.find_all = lambda names: dict.fromkeys(names)  # nothing installed
    code, doc = run_cli(svc, capsys, "precheck", "--plan", str(plan_file(tmp_path, hd_file)))
    assert code == 3 and doc["codes"] == ["ZL-INS-029"] and not doc["ok"]
    main(["--port", str(svc.node.local.port), "precheck", "--plan",
          str(plan_file(tmp_path, hd_file))])  # fmt: skip
    text = capsys.readouterr().out
    assert "OK   1. Devices connected" in text and "FAIL 2. Tools ready: " in text
    assert "Fix: Install it" in text and "--   3. Network check" in text
    code, _ = run_cli(svc, capsys, "precheck", "--plan", str(tmp_path / "none.json"))
    assert code == 6


def test_reset_and_nodes_from_the_cli(lab: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """13-ai-loop: Reset All and Shut Down All need --yes; Reset All from IDLE with a
    clean device is exit 0; a node action whose Deployer does not answer is exit 3."""
    svc, agent = lab
    run_cli(svc, capsys, "devices", "add", "local", "127.0.0.1", "--node-port",
            str(agent.api.port), "--deployer-port", "1")  # fmt: skip
    code, _ = run_cli(svc, capsys, "reset")
    assert code == 4 and svc.run.state.value == "IDLE"
    code, doc = run_cli(svc, capsys, "reset", "--yes")
    assert code == 0 and doc["data"]["ok"] and doc["data"]["devices"][0]["clean"], doc
    assert run_cli(svc, capsys, "nodes", "stop")[0] == 4
    code, doc = run_cli(svc, capsys, "nodes", "restart", "--node", "local")
    assert code == 3 and doc["codes"] == ["ZL-INS-014"] and not doc["data"]["ok"]


def test_discover_from_the_cli(lab: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """06 10.2: ``zeroloss discover`` lists the node (broadcast or the range scan),
    NEW until it is added, then known under its name; a bad range is exit 6."""
    svc, agent = lab
    port = str(agent.api.port)
    code, doc = run_cli(svc, capsys, "discover", "--scan", "127.0.0.0/30", "--node-port", port,
                        "--no-try-sender")  # fmt: skip
    mine = [d for d in doc["data"]["found"] if d["device_id"] == agent.device_id]
    assert code == 0 and len(mine) == 1 and not mine[0]["known"], doc
    run_cli(svc, capsys, "devices", "add", "rx", "127.0.0.1", "--node-port", port)
    code, doc = run_cli(svc, capsys, "discover", "--scan", "127.0.0.0/30", "--node-port", port,
                        "--no-try-sender")  # fmt: skip
    mine = [d for d in doc["data"]["found"] if d["device_id"] == agent.device_id]
    assert mine[0]["known"] and mine[0]["name"] == "rx"
    code, doc = run_cli(svc, capsys, "discover", "--scan", "10.0.0.0/8")
    assert code == 6 and doc["codes"] == ["ZL-RUN-014"]


def test_sources_from_the_cli(lab: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """13-ai-loop: ``zeroloss sources --node`` lists the sender's media folder (S-48)."""
    svc, agent = lab
    run_cli(svc, capsys, "devices", "add", "tx", "127.0.0.1", "--node-port", str(agent.api.port))
    (agent.data_dir / "media").mkdir(parents=True, exist_ok=True)
    (agent.data_dir / "media" / "broken.mp4").write_bytes(b"not a video")
    code, doc = run_cli(svc, capsys, "sources", "--node", "tx")
    assert code == 0 and [f["name"] for f in doc["data"]["files"]] == ["broken.mp4"]
    assert doc["data"]["files"][0]["error"]


@pytest.mark.skipif(not HAVE_TOOLS, reason="needs ffmpeg and tsp")
def test_run_from_the_saved_choice(lab: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """F14: without a plan file the saved screen choice is the plan; one PC on the
    LAN sending to itself is the comparison test (06 10.4)."""
    svc, agent = lab
    run_cli(svc, capsys, "devices", "add", "local", "127.0.0.1", "--node-port", str(agent.api.port))
    code, doc = run_cli(svc, capsys, "setup", "show")
    # 2026-10-17 user ("니가 판단하지말고"): the roles are what the person picks
    assert code == 0 and "Choose the sender" in doc["data"]["roles"]["why"]
    code, _ = run_cli(svc, capsys, "run")
    assert code == 3  # not clear who sends yet
    groups = '{"groups": ["239.255.95.1", "239.255.96.1", "239.255.97.1"], "port": 5004}'
    code, doc = run_cli(svc, capsys, "setup", "set", "sender=local", "receiver=local:1",
                        "quality=HD", "duration_s=10", "S-49=Off", f"S-14={groups}")  # fmt: skip
    assert code == 0 and doc["data"]["setup"]["settings"]["S-49"] == "Off"
    code, doc = run_cli(svc, capsys, "run", "--wait")
    assert code == 0 and doc["data"]["verdict"] == "PASS", doc
    assert (
        doc["data"]["dut"]["multicast_path"] == "lan"
        and doc["data"]["dut"]["mode_detected"] is None
    )
