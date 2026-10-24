"""Deployer on 10991 (docs/02-data.md 15, 06 11-12, 09 SYS-009 / INS-006 / INS-022 /
INS-027): the API, its data formats, one operation at a time, repeated command IDs,
and the watch that restarts a node that stopped answering."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.deployer.agent_link import AgentLink
from agent.deployer.modules import Layout
from agent.deployer.server import Deployer
from tests.agent.conftest import REPO, module_package
from tests.agent.fake_node import FakeNode
from tests.test_data_formats import check

SPEC = (REPO / "docs" / "02-data.md").read_text(encoding="utf-8")


def spec_json(heading: str, n: int = 0) -> Any:
    """The n-th ```json block of a '## <heading>' section of docs/02-data.md."""
    start = SPEC.index(f"\n## {heading}")
    end = SPEC.find("\n## ", start + 4)
    blocks = re.findall(r"```json\n(.*?)\n```", SPEC[start : end if end > 0 else None], re.S)
    body = blocks[n].replace("{…}", '{"__free__": true}').replace("[…]", "[]")
    return json.loads(body.replace('"…"', '"x"'))


class Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def call(self, method: str, path: str, body: bytes = b"", **headers: str) -> Any:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, body=body, headers=headers)
        out = json.loads(conn.getresponse().read())
        conn.close()
        return out

    def put(self, path: Path, sha: str | None = None) -> Any:
        data = path.read_bytes()
        return self.call("PUT", f"/v1/deployer/incoming/{path.name}", data,
                         **{"X-ZL-SHA256": sha or hashlib.sha256(data).hexdigest(),
                            "Content-Length": str(len(data))})  # fmt: skip

    def post(self, path: str, command_id: str | None = None, **params: Any) -> Any:
        body = json.dumps({"command_id": command_id or str(uuid.uuid4()), "params": params})
        return self.call("POST", path, body.encode(), **{"Content-Type": "application/json"})


@pytest.fixture
def dep(tmp_path: Path) -> Iterator[tuple[Deployer, FakeNode, Client]]:
    node = FakeNode()
    layout = Layout(tmp_path / "data", tmp_path / "program" / "modules")
    layout.modules_dir.mkdir(parents=True)
    d = Deployer(layout, AgentLink(node, node.port, come_back_s=5), "127.0.0.1", 0,
                 build="20260925.1200", watch_s=1.0, poll_s=0.1)  # fmt: skip
    d.start()
    yield d, node, Client(d.port)
    d.stop()
    node.close()


@pytest.fixture(scope="module")
def zlm(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return module_package(REPO / "modules" / "iptv", tmp_path_factory.mktemp("zlm"))


def test_health_matches_02(dep: Any) -> None:
    _, _, c = dep
    time.sleep(0.3)  # one watch round has seen the node
    h = c.call("GET", "/v1/deployer/health")
    assert h["ok"], h
    check(spec_json("15. 배포 API"), h["data"], "deployer_health")
    assert h["data"]["agent"] == {"responding": True, "build": "20260925.1200",
                                  "state": "IDLE", "restarts": 0}  # fmt: skip


def test_receive_install_list_remove(dep: Any, zlm: Path) -> None:
    d, node, c = dep
    bad = c.put(zlm, sha="0" * 64)
    assert bad["error"]["code"] == "ZL-INS-006"
    assert list(d.layout.incoming.iterdir()) == []  # nothing half received is kept
    got = c.put(zlm)
    assert got["ok"] and got["data"]["bytes"] == zlm.stat().st_size
    r = c.post("/v1/deployer/install", package=zlm.name)
    assert r["ok"], r
    check(spec_json("15. 배포 API", 1), r["data"], "deployer_install")
    assert r["data"]["result"] == "installed" and node.restarts == 1
    assert list(d.layout.incoming.iterdir()) == []  # 06 11.5 step 8: nothing left
    m = json.loads(d.layout.manifest("iptv").read_text())
    check(spec_json("14. 설치 목록"), m, "install_manifest")
    mods = c.call("GET", "/v1/deployer/modules")["data"]["modules"]
    assert [x["id"] for x in mods] == ["iptv"]
    dry = c.post("/v1/deployer/remove", kind="module", id="iptv", dry_run=True)
    assert dry["data"]["dry_run"] and (d.layout.modules_dir / "iptv").exists()
    r = c.post("/v1/deployer/remove", kind="module", id="iptv")
    assert r["ok"], r
    check(spec_json("15. 배포 API", 2), r["data"], "deployer_remove")
    assert not (d.layout.modules_dir / "iptv").exists()


def test_package_json_matches_02(zlm: Path) -> None:
    import zipfile

    info = json.loads(zipfile.ZipFile(zlm).read("package.json"))
    check(spec_json("13. 패키지"), info, "package_json")


def test_same_command_id_runs_once(dep: Any, zlm: Path) -> None:
    _, node, c = dep
    c.put(zlm)
    first = c.post("/v1/deployer/install", command_id="cmd-1", package=zlm.name)
    again = c.post("/v1/deployer/install", command_id="cmd-1", package=zlm.name)
    assert first == again and node.restarts == 1


def test_refusals(dep: Any, zlm: Path) -> None:
    d, node, c = dep
    c.put(zlm)
    node.state = "RUNNING"
    r = c.post("/v1/deployer/install", package=zlm.name)
    assert r["error"]["code"] == "ZL-INS-022"
    node.state = "IDLE"
    assert d._op.acquire(blocking=False)  # another operation holds the Deployer
    try:
        r = c.post("/v1/deployer/install", package=zlm.name)
        assert r["error"]["code"] == "ZL-INS-027"
    finally:
        d._op.release()
    for name in ("../x.zlm", "x.exe", ".zlm"):
        r = c.post("/v1/deployer/install", package=name)
        assert r["error"]["code"] == "ZL-DSC-012", name
    assert c.call("GET", "/v1/nope")["error"]["code"] == "ZL-DSC-012"
    broken = c.call("POST", "/v1/deployer/install", b"not json")
    assert broken["error"]["code"] == "ZL-DSC-012"


def test_silent_node_is_restarted_with_a_note(dep: Any) -> None:
    """SYS-009: 10 s (here 1 s) without an answer -> restart, count, note for the node."""
    d, node, c = dep
    node.answering = False
    end = time.monotonic() + 15
    while node.restarts == 0:
        assert time.monotonic() < end
        time.sleep(0.1)
    note = json.loads((d.layout.data_dir / "restart-note.json").read_text())
    assert note["restarts"] == 1 and "not answering" in note["reason"]
    assert "ZL-SYS-009" in (d.layout.deployer / "deployer.log").read_text()
    time.sleep(0.5)
    h = c.call("GET", "/v1/deployer/health")["data"]
    assert h["agent"]["responding"] and h["agent"]["restarts"] == 1


def test_no_restart_while_installing(dep: Any) -> None:
    """The Deployer's own restarts during an install are not a hang."""
    d, node, _ = dep
    with d._one_at_a_time():
        node.answering = False
        time.sleep(1.8)
    assert node.restarts == 0
    node.answering = True


def test_node_restart_stop_start(dep: Any) -> None:
    """02 15 ``/v1/deployer/node`` (F11): restart gives a new boot ID; stop keeps the
    watchdog from starting it again (Shut Down All); start brings it back; a node in a
    test is never touched (INS-022)."""
    d, node, c = dep
    before = node.boot_id
    r = c.post("/v1/deployer/node", action="restart")
    assert r["ok"] and r["data"]["done"] and node.boot_id != before and node.restarts == 1
    r = c.post("/v1/deployer/node", action="stop")
    assert r["ok"] and r["data"]["node"] is None and not node.answering
    assert c.call("GET", "/v1/deployer/health")["data"]["node_stopped"] is True
    time.sleep(1.8)  # longer than watch_s: a stopped node is not restarted (SYS-009)
    assert node.restarts == 1 and not node.answering and d.restarts == 0
    r = c.post("/v1/deployer/node", action="start")
    assert r["ok"] and node.answering and not d.node_stopped
    node.state = "RUNNING"
    r = c.post("/v1/deployer/node", action="restart")
    assert r["error"]["code"] == "ZL-INS-022" and node.restarts == 1
    assert c.post("/v1/deployer/node", action="explode")["error"]["code"] == "ZL-DSC-012"


def test_tools_come_as_debs_and_apt_puts_them_in_offline(
    dep: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-16 user: the program installs what it needs (iperf3) by itself, also on
    PCs without internet: a .zlt carries the .deb files, apt installs from them only."""
    import subprocess

    from agent.deployer.package import build_package

    d, node, c = dep
    src = tmp_path / "tools"
    src.mkdir()
    (src / "iperf3_3.16-1build2_amd64.deb").write_bytes(b"deb1")
    (src / "libiperf0_3.16-1build2_amd64.deb").write_bytes(b"deb2")
    zlt = build_package(src, tmp_path / "zeroloss-tools-iperf3-20261016.0026.zlt", {
        "schema_version": 1, "kind": "tools", "id": "iperf3", "build": "20261016.0026",
        "os": "linux"})  # fmt: skip
    ran: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        ran.append(list(argv))
        assert all(Path(a).exists() for a in argv if a.endswith(".deb"))  # unpacked first
        return subprocess.CompletedProcess(argv, 0, "Setting up iperf3", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert c.put(zlt)["ok"]
    r = c.post("/v1/deployer/install", package=zlt.name)
    assert r["ok"], r["error"].get("detail")
    assert r["data"]["result"] == "installed" and r["data"]["debs"] == 2, r
    (argv,) = ran
    assert argv[:2] == ["apt-get", "install"] and sum(a.endswith(".deb") for a in argv) == 2
    assert node.restarts == 0 and list(d.layout.incoming.iterdir()) == []
    assert not (d.layout.deployer / "tools").exists()  # the unpacked files are gone
