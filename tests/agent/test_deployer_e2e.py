"""End to end on this PC: a real node process, the Deployer, a module package
(docs/06-install.md 12). The node starts with an empty module folder, gets IPTV
through 10991, restarts with it, loses it again on removal; a node that hangs is
restarted and tells why (SYS-009)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.deployer.agent_link import AgentLink, ServiceControl
from agent.deployer.modules import Layout
from agent.deployer.server import Deployer
from core.protocol.client import NodeClient
from tests.agent.conftest import REPO, module_package
from tests.agent.test_deployer_service import Client

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the Deployer runs on the Linux nodes"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class NodeProcess(ServiceControl):
    """The node as a child process (what systemd does for the installed node)."""

    def __init__(self, data: Path, modules: Path) -> None:
        self.port, self.local = free_port(), free_port()
        self.argv = [sys.executable, "-m", "controller.cli", "--port", str(self.local), "serve",
                     "--role", "agent", "--data", str(data), "--modules", str(modules),
                     "--node-port", str(self.port)]  # fmt: skip
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        env = dict(os.environ, PYTHONPATH=str(REPO))
        self.proc = subprocess.Popen(
            self.argv, cwd=REPO, env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip

    def restart(self) -> None:
        self.stop()
        self.start()

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(10)

    def freeze(self) -> None:
        assert self.proc is not None
        os.killpg(self.proc.pid, signal.SIGSTOP)  # alive but not answering (a hang)


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[tuple[Deployer, NodeProcess, Client]]:
    layout = Layout(tmp_path / "data", tmp_path / "program" / "modules")
    layout.modules_dir.mkdir(parents=True)
    node = NodeProcess(layout.data_dir, layout.modules_dir)
    node.start()
    link = AgentLink(node, node.port, come_back_s=30)
    end = time.monotonic() + 30
    while link.health() is None:
        assert time.monotonic() < end, "node did not start"
        time.sleep(0.2)
    dep = Deployer(layout, link, "127.0.0.1", 0, build="20260925.1200", watch_s=3.0, poll_s=0.5)
    dep.start()
    yield dep, node, Client(dep.port)
    dep.stop()
    node.stop()


def node_modules(node: NodeProcess) -> list[dict[str, Any]]:
    info = NodeClient("127.0.0.1", node.port, "test").get("/v1/info")
    return list(info["modules"])


def test_module_goes_in_and_out_of_a_running_node(lab: Any, tmp_path: Path) -> None:
    dep, node, c = lab
    assert node_modules(node) == []
    zlm = module_package(REPO / "modules" / "iptv", tmp_path / "pkg")
    assert c.put(zlm)["ok"]
    r = c.post("/v1/deployer/install", package=zlm.name)
    assert r["ok"], r
    assert node_modules(node) == [{"id": "iptv", "enabled": True, "reason": ""}]
    cache = dep.layout.module_root("iptv") / "cache"
    assert cache.is_dir()  # the module's cache is where the node gives it (06 12)
    r = c.post("/v1/deployer/remove", kind="module", id="iptv")
    assert r["ok"] and r["data"]["left"] == [], r
    assert node_modules(node) == []
    assert not (dep.layout.modules_dir / "iptv").exists() and not cache.exists()


def test_hung_node_is_restarted_and_says_so(lab: Any) -> None:
    """SYS-009 end to end: the node process is frozen; the Deployer restarts it; the new
    node's first warning explains why."""
    dep, node, c = lab
    before = node.proc.pid if node.proc else None
    node.freeze()
    end = time.monotonic() + 30
    while node.proc is None or node.proc.pid == before:  # counted first, then restarted
        assert time.monotonic() < end, "not restarted"
        time.sleep(0.2)
    assert dep.restarts == 1
    end = time.monotonic() + 30
    while not c.call("GET", "/v1/deployer/health")["data"]["agent"]["responding"]:
        assert time.monotonic() < end
        time.sleep(0.2)
    log = (dep.layout.data_dir / "logs" / "events.log").read_text(encoding="utf-8")
    assert "ZL-SYS-009" in log
    assert not (dep.layout.data_dir / "restart-note.json").exists()


def test_cli_deploy_list_remove(
    lab: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """13-ai-loop: zeroloss deploy / modules list / modules remove through the Controller
    service (02 2-5); removal needs --yes; a Deployer that does not answer is exit 5."""
    import json as _json

    from controller.cli import main
    from controller.run import RunState
    from controller.service import ControllerService
    from tools.build import build_module

    dep, node, _ = lab
    monkeypatch.setattr("controller.deploy.RETRY_DELAYS_S", (0.1, 0.1, 0.1))
    svc = ControllerService(tmp_path / "ctl", tmp_path / "rep", REPO / "modules",
                            host="127.0.0.1", port=0, local_port=0)  # fmt: skip
    svc.start()
    try:

        def cli(*args: str) -> tuple[int, Any]:
            code = main(["--json", "--port", str(svc.node.local.port), *args])
            doc = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
            return code, doc

        code, doc = cli("devices", "add", "tx", "127.0.0.1", "--node-port", str(node.port),
                        "--deployer-port", str(dep.port))  # fmt: skip
        assert code == 0, doc
        zlm = build_module("iptv", tmp_path / "dist")
        code, doc = cli("deploy", "--node", "tx", "--package", str(zlm))
        assert code == 0 and doc["data"]["result"] == "installed", doc
        code, doc = cli("modules", "list", "--node", "tx")
        assert code == 0 and [m["id"] for m in doc["data"]["modules"]] == ["iptv"]
        assert doc["data"]["deployer"]["agent"]["responding"]
        code, doc = cli("modules", "remove", "iptv", "--node", "tx")
        assert code == 4 and (dep.layout.modules_dir / "iptv").exists()  # no --yes, nothing done
        code, doc = cli("modules", "remove", "iptv", "--node", "tx", "--dry-run")
        assert code == 0 and doc["data"]["dry_run"] and (dep.layout.modules_dir / "iptv").exists()
        svc.run.state = RunState.RUNNING  # a test is running: installs wait for later
        code, doc = cli("modules", "remove", "iptv", "--node", "tx", "--yes")
        assert code == 3 and doc["codes"] == ["ZL-INS-022"]
        svc.run.state = RunState.IDLE
        code, doc = cli("modules", "remove", "iptv", "--node", "tx", "--yes")
        assert code == 0 and doc["data"]["left"] == []
        assert node_modules(node) == []
        cli("devices", "add", "gone", "127.0.0.1", "--node-port", str(node.port),
            "--deployer-port", str(free_port()))  # fmt: skip
        code, doc = cli("modules", "list", "--node", "gone")
        assert code == 5 and doc["codes"] == ["ZL-INS-014"]
    finally:
        svc.stop()


def test_a_deployer_that_comes_up_late_is_retried(tmp_path: Path) -> None:
    """09 INS-014 / 0.3: a Deployer that does not answer yet is tried again 1, 2, 4 s later;
    the install goes through once it is up, and only once (one command ID)."""
    import threading

    from controller.deploy import Deploy
    from controller.devices import Device, DeviceList
    from tests.agent.fake_node import FakeNode

    node = FakeNode()
    layout = Layout(tmp_path / "data", tmp_path / "program" / "modules")
    layout.modules_dir.mkdir(parents=True)
    port = free_port()
    started: list[Deployer] = []

    def come_up() -> None:
        d = Deployer(layout, AgentLink(node, node.port, come_back_s=5), "127.0.0.1", port,
                     build="20260925.1200", watch=False)  # fmt: skip
        d.start()
        started.append(d)

    threading.Timer(1.5, come_up).start()
    devices = DeviceList(tmp_path / "devices.json")
    devices._devices["tx"] = Device("tx", "127.0.0.1", 1, "id", "sender", "now", {}, port)
    zlm = module_package(REPO / "modules" / "iptv", tmp_path / "pkg")
    try:
        r = Deploy(devices, "boot").install("tx", zlm)
        assert r["result"] == "installed" and node.restarts == 1
        assert list(layout.incoming.iterdir()) == []
    finally:
        for d in started:
            d.stop()
        node.close()
