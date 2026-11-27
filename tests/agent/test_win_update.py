"""Node update on Windows over 10991 (09 RUN-013, 06 11.5; 2026-11-28 user "Run 13도 하고").

The flow is the Linux one (tests/agent/test_node_update.py) with the Windows setup in
place of the .deb: a fake launcher stands in for the detached setup. On Windows the job
script itself runs for real against a stand-in setup (G-18: the test tool is checked too).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent.deployer.errors import DeployError
from agent.deployer.modules import Layout
from agent.deployer.node_update import NodeUpdate
from agent.deployer.package import build_package, open_package
from agent.deployer.win_update import WindowsLauncher, default_launcher, job_script
from tests.agent.test_node_update import Node

OLD, NEW = "20261127.0941", "20261128.1200"
SETUP = f"ZeroLoss-Agent-Setup-{NEW}.exe"


class FakeSetup(WindowsLauncher):
    """``plan``: one (exit code or None, node build after) per launch, in order."""

    def __init__(self, folder: Path, node: Node, plan: list[tuple[int | None, str]]) -> None:
        super().__init__(folder)
        self.node, self.plan = node, list(plan)
        self.launched: list[tuple[str, str]] = []
        self.afters = 0

    def launch(self, job_id: str, deb: Path, log: Path, status: Path) -> None:
        self.launched.append((job_id, deb.name))
        code, build = self.plan.pop(0)
        log.write_text("Setup started\nLog closed.\n")
        if code is not None:
            status.write_text(f"{code}\r\n")
        self.node.build = build

    def running(self, job_id: str) -> bool:
        return False

    def after(self) -> str | None:
        self.afters += 1
        return None


def win_zlp(folder: Path, name: str = SETUP, body: bytes = b"MZ\x90\x00", os_name: str = "windows",
            build: str = NEW) -> Path:  # fmt: skip
    src = folder / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / name).write_bytes(body)
    info = {"schema_version": 1, "kind": "node", "id": "agent", "build": build, "os": os_name,
            "core_api_min": None, "core_api_max": None}  # fmt: skip
    return build_package(src, folder / "incoming" / f"zeroloss-node-agent-windows-{build}.zlp",
                         info)  # fmt: skip


def make(tmp_path: Path, plan: list[tuple[int | None, str]], current: bool = True,
         ) -> tuple[NodeUpdate, Node, FakeSetup]:  # fmt: skip
    layout = Layout(tmp_path / "data", tmp_path / "mods")
    layout.incoming.mkdir(parents=True)
    node = Node(OLD)
    up_folder = layout.deployer / "packages"
    up_folder.mkdir(parents=True)
    la = FakeSetup(up_folder, node, plan)
    up = NodeUpdate(layout, node, OLD, la, lambda *_: None, exit_wait_s=0.5,
                    node_wait_s=0.3, poll_s=0.02)  # fmt: skip
    if current:
        (up.folder / "current.exe").write_bytes(b"MZ old")
    return up, node, la


def test_a_windows_update_is_done_and_desktop_comes_back(tmp_path: Path) -> None:
    up, _node, la = make(tmp_path, [(0, NEW)])
    r = up.start(open_package(win_zlp(tmp_path)))
    assert r["result"] == "started" and (r["from_build"], r["to_build"]) == (OLD, NEW)
    assert la.launched[0][1] == "new.exe"
    assert "done" in (up.resume() or "")
    assert up.record()["state"] == "done" and la.afters == 1  # type: ignore[index]
    assert sorted(p.name for p in up.folder.iterdir()) == ["current.exe"]
    assert (up.folder / "current.exe").read_bytes() == b"MZ\x90\x00"


def test_a_failed_setup_goes_back_with_its_reason(tmp_path: Path) -> None:
    up, _node, la = make(tmp_path, [(7, OLD), (0, OLD)])
    up.start(open_package(win_zlp(tmp_path)))
    up.resume()
    doc = up.record()
    assert doc is not None and doc["state"] == "rolled_back"
    assert doc["why"].startswith("the setup ended with 7 (it refused before installing")
    assert [n for _, n in la.launched] == ["new.exe", "previous.exe"] and la.afters == 1
    assert sorted(p.name for p in up.folder.iterdir()) == ["current.exe"]


@pytest.mark.parametrize(("kw", "code"), [
    ({"os_name": "linux"}, "ZL-INS-005"),  # the .deb's package sent to a Windows PC
    ({"name": "setup.exe"}, "ZL-INS-006"),
    ({"name": "ZeroLoss-Agent-Setup-20261128.0000.exe"}, "ZL-INS-006"),  # name and build differ
    ({"body": b"#!/bin/sh"}, "ZL-INS-006"),  # not a Windows program
])  # fmt: skip
def test_refused(tmp_path: Path, kw: dict[str, Any], code: str) -> None:
    up, _node, la = make(tmp_path, [])
    with pytest.raises(DeployError) as e:
        up.start(open_package(win_zlp(tmp_path, **kw)))
    assert e.value.code == code and not la.launched and up.record() is None
    assert sorted(p.name for p in up.folder.iterdir()) == ["current.exe"]


def test_the_launcher_is_the_systems() -> None:
    la = default_launcher(Path("x"))
    assert (la.os_name, la.ext) == (("windows", ".exe") if sys.platform == "win32"
                                    else ("linux", ".deb"))  # fmt: skip


def test_the_job_script_takes_only_paths() -> None:
    s = job_script()
    assert '"%~1" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /NOCANCEL /LOG="%~2"' in s
    assert '>"%~3.tmp" echo %c%' in s and "\r\n" in s
    assert "taskkill /F /T /IM zeroloss-agent-desktop.exe" in s.split("%~1")[0]


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe runs the Windows job")
def test_the_job_script_runs_and_writes_the_setups_exit_code(tmp_path: Path) -> None:
    """A stand-in setup: python.exe, which cannot open the file "/VERYSILENT" (exit 2)."""
    la = WindowsLauncher(tmp_path)
    log, status = tmp_path / "update-j1.log", tmp_path / "update-j1.exit"
    la.launch("j1", Path(sys.executable), log, status)
    end = time.monotonic() + 30
    while not status.is_file() and time.monotonic() < end:
        time.sleep(0.1)
    assert status.read_text(encoding="ascii").strip() == "2"
    end = time.monotonic() + 10
    while la.running("j1") and time.monotonic() < end:
        time.sleep(0.1)
    assert not la.running("j1") and not la.running("none")
    assert not (tmp_path / "update-j1.exit.tmp").exists()
