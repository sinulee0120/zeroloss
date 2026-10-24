"""First install of a Linux node over SSH (docs/06-install.md 11.3, 09 INS-002/003/005/
007~011/014/028) against a stand-in PC. The happy path ends with a real node and
Deployer: the device is registered and the IPTV module is installed through 10991.
The password never appears in a result, an error or a saved file."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.deployer.agent_link import AgentLink
from agent.deployer.modules import Layout
from agent.deployer.server import Deployer
from controller.devices import DeviceList
from controller.ssh_install import KnownHosts, LinuxInstaller, Target
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError
from tests.agent.conftest import REPO, module_package
from tests.agent.fake_node import FakeNode
from tests.agent.test_deployer_e2e import NodeProcess, free_port
from tests.controller.fake_ssh import FakeLinuxPC

PASSWORD = "s3cret-pw!"


@pytest.fixture
def pc(tmp_path: Path) -> Iterator[FakeLinuxPC]:
    root = tmp_path / "pc"
    root.mkdir()
    fake = FakeLinuxPC(root, {"tester": PASSWORD, "nosudo": PASSWORD}, {"tester"})
    yield fake
    fake.close()


def installer(tmp_path: Path, modules: list[Path] | None = None, **kw: Any) -> LinuxInstaller:
    deb = tmp_path / "zeroloss-agent_20260925.1200_amd64.deb"
    deb.write_bytes(b"not a real package, the stand-in PC does not open it")
    kw.setdefault("lock_wait_s", 0.05)
    known = KnownHosts(tmp_path / "ctl" / "ssh-known-hosts.json")
    devices = DeviceList(tmp_path / "devices.json")
    return LinuxInstaller(known, deb, modules or [], devices, "boot", **kw)


def target(pc: FakeLinuxPC, user: str = "tester", password: str = PASSWORD, **kw: Any) -> Target:
    return Target("127.0.0.1", user, password, name="lin1", ssh_port=pc.port, **kw)


@pytest.fixture
def answering() -> Iterator[tuple[int, int]]:
    """Something answers on the node and Deployer ports after the "install"."""
    node, dep = FakeNode(), FakeNode()
    yield node.port, dep.port
    node.close()
    dep.close()


def test_install_end_to_end(pc: FakeLinuxPC, tmp_path: Path) -> None:
    layout = Layout(tmp_path / "node-data", tmp_path / "node-mods")
    layout.modules_dir.mkdir(parents=True)
    node = NodeProcess(layout.data_dir, layout.modules_dir)
    running: list[Deployer] = []
    dep_port = free_port()

    def installed() -> None:  # what the package's postinst does: start both services
        node.start()
        d = Deployer(layout, AgentLink(node, node.port, come_back_s=30), "127.0.0.1", dep_port,
                     build="20260925.1200", watch=False)  # fmt: skip
        d.start()
        running.append(d)

    pc.on_dpkg = installed
    zlm = module_package(REPO / "modules" / "iptv", tmp_path / "pkg")
    inst = installer(tmp_path, [zlm])
    try:
        r = inst.install(target(pc, node_port=node.port, deployer_port=dep_port))
        names = [s["name"] for s in r["steps"]]
        assert names == ["host_key", "login", "checks", "sudo", "upload", "install",
                         "rollback_copy", "tools_install", "tools", "answer", "register",
                         "module"]  # fmt: skip
        kept = pc.at("/var/lib/zeroloss/deployer/packages/current.deb")
        assert kept.read_bytes() == inst.deb.read_bytes()  # the first update can go back
        assert r["host_key"]["status"] == "new" and r["tools_missing"] == []
        assert r["tools_installed"] == ["mpv", "vlc"]  # 06 11.3 4-3: in the same sudo
        dev = inst.devices.get("lin1")
        assert (dev.port, dev.deployer_port) == (node.port, dep_port)
        info = NodeClient("127.0.0.1", node.port, "t").get("/v1/info")
        assert info["modules"] == [{"id": "iptv", "enabled": True, "reason": ""}]
        assert not list((pc.root / "tmp").glob("zeroloss-install-*"))  # temp files removed
        saved = (tmp_path / "ctl" / "ssh-known-hosts.json").read_text()
        assert json.loads(saved)["hosts"]["127.0.0.1"]["sha256"] == r["host_key"]["sha256"]
        everything = json.dumps(r) + saved + (tmp_path / "devices.json").read_text()
        assert PASSWORD not in everything  # used once, never kept (06 11.3)
        assert pc.stdin_seen and set(pc.stdin_seen) == {PASSWORD}  # only on sudo's stdin
        assert not any(PASSWORD in c for c in pc.commands)  # never on a command line
    finally:
        for d in running:
            d.stop()
        node.stop()


def test_host_key_same_changed_and_accepted(pc: FakeLinuxPC, tmp_path: Path,
                                            answering: tuple[int, int]) -> None:  # fmt: skip
    inst = installer(tmp_path)
    t = target(pc, node_port=answering[0], deployer_port=answering[1])
    assert inst.install(t)["host_key"]["status"] == "new"
    assert inst.install(t)["host_key"]["status"] == "same"
    pc.new_key()
    with pytest.raises(ZlError) as e:
        inst.install(t)
    assert e.value.code == "ZL-INS-009" and not pc.commands[-1].startswith("dpkg")
    n = len(pc.commands)
    assert inst.install(t, accept_new_host_key=True)["host_key"]["status"] == "replaced"
    assert len(pc.commands) > n


@pytest.mark.parametrize(("user", "password", "code"), [
    ("tester", "not-the-pw-7Qx", "ZL-INS-007"),  # unique: must not match message words
    ("root", "anything", "ZL-INS-008"),
    ("nosudo", PASSWORD, "ZL-INS-010"),
])  # fmt: skip
def test_login_and_sudo_refusals(pc: FakeLinuxPC, tmp_path: Path, user: str, password: str,
                                 code: str) -> None:  # fmt: skip
    with pytest.raises(ZlError) as e:
        installer(tmp_path).install(target(pc, user, password))
    assert e.value.code == code
    assert password not in json.dumps(e.value.to_dict())
    assert not [c for c in pc.commands if "dpkg" in c]


def test_lock_is_waited_for(pc: FakeLinuxPC, tmp_path: Path, answering: tuple[int, int]) -> None:
    pc.locked_times = 2
    r = installer(tmp_path).install(target(pc, node_port=answering[0], deployer_port=answering[1]))
    assert sum("dpkg -i" in c for c in pc.commands) == 3 and "install" in [
        s["name"] for s in r["steps"]
    ]


def test_lock_that_never_ends(pc: FakeLinuxPC, tmp_path: Path) -> None:
    pc.locked_times = 10_000
    with pytest.raises(ZlError) as e:
        installer(tmp_path, lock_limit_s=0.3).install(target(pc))
    assert e.value.code == "ZL-INS-011"
    assert not list((pc.root / "tmp").glob("zeroloss-install-*"))


def test_dpkg_failure_is_rolled_back(pc: FakeLinuxPC, tmp_path: Path) -> None:
    pc.dpkg_error = "dpkg: dependency problems prevent configuration of zeroloss-agent"
    with pytest.raises(ZlError) as e:
        installer(tmp_path).install(target(pc))
    assert e.value.code == "ZL-INS-028" and "dependency problems" in e.value.why
    assert any(c.endswith("dpkg -r zeroloss-agent") for c in pc.commands)
    assert not list((pc.root / "tmp").glob("zeroloss-install-*"))


@pytest.mark.parametrize(("change", "code"), [
    ({"os_id": "ubuntu 22.04"}, "ZL-INS-005"),
    ({"arch": "aarch64"}, "ZL-INS-005"),
    ({"free_kb": 200_000}, "ZL-INS-002"),
    ({"listening": "LISTEN 0 5 0.0.0.0:10992 0.0.0.0:*\n"}, "ZL-INS-003"),
])  # fmt: skip
def test_checks_before_anything_is_sent(pc: FakeLinuxPC, tmp_path: Path, change: dict[str, Any],
                                        code: str) -> None:  # fmt: skip
    for k, v in change.items():
        setattr(pc, k, v)
    with pytest.raises(ZlError) as e:
        installer(tmp_path).install(target(pc))
    assert e.value.code == code
    assert not [c for c in pc.commands if c.startswith(("mkdir", "sudo -S -p '' dpkg"))]


def test_node_that_never_answers(pc: FakeLinuxPC, tmp_path: Path) -> None:
    with pytest.raises(ZlError) as e:
        installer(tmp_path, answer_limit_s=0.5).install(
            target(pc, node_port=free_port(), deployer_port=free_port())
        )
    assert e.value.code == "ZL-INS-014"


def test_unreachable_pc(tmp_path: Path) -> None:
    with pytest.raises(ZlError) as e:
        installer(tmp_path).install(Target("127.0.0.1", "tester", PASSWORD, ssh_port=free_port()))
    assert e.value.code == "ZL-DSC-014"


def test_cli_install_linux(pc: FakeLinuxPC, tmp_path: Path, answering: tuple[int, int],
                           capsys: pytest.CaptureFixture[str],
                           monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    """13-ai-loop: `zeroloss install linux` needs --yes, reads the password from stdin
    and goes through the Controller service; the newest packages/ .deb is the default."""
    import io

    from controller.cli import main
    from controller.service import ControllerService

    svc = ControllerService(tmp_path / "ctl", tmp_path / "rep", REPO / "modules",
                            host="127.0.0.1", port=0, local_port=0)  # fmt: skip
    (tmp_path / "ctl" / "packages").mkdir(parents=True)
    for build in ("20260924.0900", "20260925.1200"):
        (tmp_path / "ctl" / "packages" / f"zeroloss-agent_{build}_amd64.deb").write_bytes(b"x")
    svc.start()
    try:

        def cli(*args: str, stdin: str = "") -> tuple[int, Any]:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
            code = main(["--json", "--port", str(svc.node.local.port), *args])
            return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        base = ["install", "linux", "--host", "127.0.0.1", "--user", "tester", "--name", "lin1",
                "--ssh-port", str(pc.port), "--password-stdin"]  # fmt: skip
        code, _ = cli(*base, stdin=PASSWORD + "\n")
        assert code == 4 and not pc.commands  # no --yes: nothing was done
        ports = ["--node-port", str(answering[0]), "--deployer-port", str(answering[1])]
        code, doc = cli(*base, *ports, "--yes", stdin=PASSWORD + "\n")
        assert code == 0, doc
        assert doc["data"]["tools_missing"] == [] and doc["data"]["name"] == "lin1"
        uploaded = [c for c in pc.commands if "dpkg -i" in c]
        assert uploaded and "20260925.1200" in uploaded[0]  # the newest package
        code, doc = cli(*base, *ports, "--yes", stdin="not-the-pw-7Qx\n")
        assert code == 3 and doc["codes"] == ["ZL-INS-007"]
    finally:
        svc.stop()


@pytest.mark.parametrize(("gdm", "want"), [(True, "on for tester"), (False, "not set")])
def test_auto_login_asked_for(pc: FakeLinuxPC, tmp_path: Path, answering: tuple[int, int],
                              gdm: bool, want: str) -> None:  # fmt: skip
    """00-vision / GAP-67: --auto-login is the person's yes; no GDM is a note, not a fail."""
    pc.no_gdm = not gdm
    t = target(pc, node_port=answering[0], deployer_port=answering[1], auto_login=True)
    r = installer(tmp_path).install(t)
    assert r["auto_login"].startswith(want), r
    assert "auto_login" in [s["name"] for s in r["steps"]]
    assert pc.auto_login_user == ("tester" if gdm else None)
    assert not any(PASSWORD in c for c in pc.commands)  # sudo's stdin only


def test_no_auto_login_unless_asked(pc: FakeLinuxPC, tmp_path: Path,
                                    answering: tuple[int, int]) -> None:  # fmt: skip
    r = installer(tmp_path).install(target(pc, node_port=answering[0],
                                           deployer_port=answering[1]))  # fmt: skip
    assert "auto_login" not in r and pc.auto_login_user is None
    assert not any("auto-login" in c for c in pc.commands)


def test_tools_come_with_the_install(pc: FakeLinuxPC, tmp_path: Path,
                                     answering: tuple[int, int]) -> None:  # fmt: skip
    """06 11.3 4-3 / GAP-73 ①: missing tools are installed in the same SSH + sudo —
    Ubuntu's by apt, TSDuck from the Controller's packages/tools/. One password."""
    pc.tools = {"mpv", "vlc"}  # a fresh sender PC
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "tsduck_3.45-4798.ubuntu24_amd64.deb").write_bytes(b"tsduck")
    inst = installer(tmp_path, tools_dir=tools)
    r = inst.install(target(pc, node_port=answering[0], deployer_port=answering[1]))
    assert r["tools_missing"] == [] and sorted(r["tools_installed"]) == ["ffmpeg", "ffprobe",
                                                                         "tsp"]  # fmt: skip
    (apt,) = pc.apt_runs
    assert "ffmpeg" in apt and "libxcb-cursor0" in apt and "tsduck_3.45-4798" in apt
    assert "DPkg::Lock::Timeout=600" in apt  # INS-011: apt waits for the lock
    assert set(pc.stdin_seen) == {PASSWORD} and not any(PASSWORD in c for c in pc.commands)
    assert not list((pc.root / "tmp").glob("zeroloss-install-*"))  # uploads removed


def test_offline_pc_is_told_not_failed(pc: FakeLinuxPC, tmp_path: Path,
                                       answering: tuple[int, int]) -> None:  # fmt: skip
    pc.tools, pc.online = {"tsp"}, False
    r = installer(tmp_path).install(target(pc, node_port=answering[0],
                                           deployer_port=answering[1]))  # fmt: skip
    assert sorted(r["tools_missing"]) == ["ffmpeg", "ffprobe", "mpv", "vlc"]
    step = next(s for s in r["steps"] if s["name"] == "tools_install")
    assert "still missing" in step["detail"] and "no network" in step["detail"]
    assert "register" in [s["name"] for s in r["steps"]]  # the node is installed anyway
