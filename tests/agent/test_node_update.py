"""Node package update over 10991 on Linux (docs/06-install.md 11.5 "Linux 구현 규칙",
02 15 ``update``, 09 INS-006/011/013/022/026/027/028).

A fake launcher stands in for ``systemd-run`` and dpkg: each launch is told what the
install job does (exit code, or nothing for a power cut) and which build the node
answers with afterwards. The job's shell script itself runs for real against a fake
``dpkg`` and ``systemctl`` (G-18: the test tool is checked too).
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.deployer.agent_link import AgentLink
from agent.deployer.errors import DeployError
from agent.deployer.modules import Agent, Layout
from agent.deployer.node_update import FINAL, Launcher, NodeUpdate, job_script
from agent.deployer.package import build_package, open_package
from agent.deployer.server import Deployer
from tests.agent.fake_node import FakeNode
from tests.agent.test_deployer_service import Client

OLD, NEW = "20260925.1200", "20260926.0900"


class Node(Agent):
    def __init__(self, build: str = OLD) -> None:
        self.build: str | None = build
        self.running_test = False

    def busy(self) -> bool:
        return self.running_test

    def restart(self) -> bool:
        return True

    def health(self, timeout_s: float = 2.0) -> dict[str, Any] | None:
        return None if self.build is None else {"build": self.build, "state": "IDLE"}


class FakeLauncher(Launcher):
    """``plan``: one (exit code or None, node build after) per launch, in order."""

    def __init__(self, node: Any, plan: list[tuple[int | None, str | None]]) -> None:
        super().__init__()
        self.node, self.plan = node, list(plan)
        self.launched: list[tuple[str, bytes]] = []
        self.fail_launch = False

    def fields(self, deb: Path) -> dict[str, str]:
        text = deb.read_text(encoding="utf-8")
        return dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)

    def launch(self, job_id: str, deb: Path, log: Path, status: Path) -> None:
        if self.fail_launch:
            raise DeployError("INS-028", "The install could not be started", "systemd-run: no")
        self.launched.append((job_id, deb.read_bytes()))
        code, build = self.plan.pop(0)
        log.write_text("Unpacking zeroloss-agent ...\ndpkg: error processing package\n")
        if code is not None:
            status.write_text(f"{code}\n")
        self.node.build = build

    def running(self, job_id: str) -> bool:
        return False  # the fake job is over as soon as it was started


def deb_bytes(build: str, name: str = "zeroloss-agent") -> bytes:
    return f"Package: {name}\nVersion: {build}\n".encode()


def node_zlp(folder: Path, build: str = NEW, deb: bytes | None = None, **info: Any) -> Path:
    src = folder / f"src-{build}"
    src.mkdir(parents=True, exist_ok=True)
    (src / f"zeroloss-agent_{build}_amd64.deb").write_bytes(deb or deb_bytes(build))
    body = {"schema_version": 1, "kind": "node", "id": "agent", "build": build, "os": "linux",
            "core_api_min": None, "core_api_max": None, **info}  # fmt: skip
    return build_package(src, folder / "incoming" / f"zeroloss-node-agent-{build}.zlp", body)


def make(tmp_path: Path, plan: list[tuple[int | None, str | None]], current: bool = True,
         ) -> tuple[NodeUpdate, Node, FakeLauncher, Layout]:  # fmt: skip
    layout = Layout(tmp_path / "data", tmp_path / "mods")
    layout.incoming.mkdir(parents=True)
    node = Node()
    launcher = FakeLauncher(node, plan)
    up = NodeUpdate(layout, node, OLD, launcher, lambda *_: None, exit_wait_s=0.5,
                    node_wait_s=0.3, poll_s=0.02)  # fmt: skip
    up.folder.mkdir(parents=True)
    if current:
        (up.folder / "current.deb").write_bytes(deb_bytes(OLD))
    return up, node, launcher, layout


def left(up: NodeUpdate, layout: Layout) -> list[str]:
    return sorted(p.name for p in [*up.folder.iterdir(), *layout.incoming.iterdir()])


def test_update_done(tmp_path: Path) -> None:
    up, _node, _launcher, layout = make(tmp_path, [(0, NEW)])
    r = up.start(open_package(node_zlp(tmp_path)))
    assert r["result"] == "started" and (r["from_build"], r["to_build"]) == (OLD, NEW)
    assert up.open and up.record()["state"] == "running"  # type: ignore[index]
    assert "done" in (up.resume() or "")
    doc = up.record()
    assert doc is not None and doc["state"] == "done" and doc["why"] is None
    assert doc["id"] == r["update_id"] and doc["ended_utc"]
    assert (up.folder / "current.deb").read_bytes() == deb_bytes(NEW)
    assert left(up, layout) == ["current.deb"]  # 11.5 8: no package left behind
    assert not up.open


@pytest.mark.parametrize(("first", "why"), [
    ((1, OLD), "dpkg -i ended with 1: dpkg: error processing package"),
    ((None, None), "the install stopped part way"),  # power cut (INS-013)
    ((0, OLD), f"the node did not answer with build {NEW}"),
])  # fmt: skip
def test_failed_install_goes_back(tmp_path: Path, first: Any, why: str) -> None:
    up, _node, launcher, layout = make(tmp_path, [first, (0, OLD)])
    up.start(open_package(node_zlp(tmp_path)))
    up.resume()
    doc = up.record()
    assert doc is not None and doc["state"] == "rolled_back" and why in doc["why"]
    assert [d for _, d in launcher.launched] == [deb_bytes(NEW), deb_bytes(OLD)]
    assert launcher.launched[1][0] == doc["id"] + "b" == doc["job"]
    assert (up.folder / "current.deb").read_bytes() == deb_bytes(OLD)
    assert left(up, layout) == ["current.deb"]


def test_going_back_fails_too(tmp_path: Path) -> None:
    up, _node, _launcher, layout = make(tmp_path, [(1, OLD), (1, None)])
    up.start(open_package(node_zlp(tmp_path)))
    up.resume()
    doc = up.record()
    assert doc is not None and doc["state"] == "failed" and "going back" in doc["why"]
    assert left(up, layout) == ["current.deb"]


def test_no_previous_package(tmp_path: Path) -> None:
    up, _node, launcher, layout = make(tmp_path, [(1, OLD)], current=False)
    r = up.start(open_package(node_zlp(tmp_path)))
    assert r["no_rollback"] is True
    up.resume()
    doc = up.record()
    assert doc is not None and doc["state"] == "failed"
    assert "no previous package to go back to" in doc["why"] and len(launcher.launched) == 1
    assert left(up, layout) == []


def test_launch_fails(tmp_path: Path) -> None:
    up, _node, launcher, layout = make(tmp_path, [])
    launcher.fail_launch = True
    with pytest.raises(DeployError) as e:
        up.start(open_package(node_zlp(tmp_path)))
    assert e.value.code == "ZL-INS-028"
    assert up.record()["state"] == "failed" and not up.open  # type: ignore[index]
    assert left(up, layout) == ["current.deb"]  # 11.5 9: the received package goes too


@pytest.mark.parametrize(("kw", "code"), [
    ({"build": "20260901.0000"}, "ZL-INS-026"),  # older than installed
    ({"deb": deb_bytes(NEW, "other-package")}, "ZL-INS-006"),
    ({"deb": deb_bytes("20260930.0000")}, "ZL-INS-006"),  # .deb and package.json differ
    ({"id": "controller"}, "ZL-INS-006"),
    ({"os": "windows"}, "ZL-INS-006"),
])  # fmt: skip
def test_refused(tmp_path: Path, kw: dict[str, Any], code: str) -> None:
    up, _node, launcher, _layout = make(tmp_path, [])
    with pytest.raises(DeployError) as e:
        up.start(open_package(node_zlp(tmp_path, **kw)))
    assert e.value.code == code and not launcher.launched and up.record() is None
    assert sorted(p.name for p in up.folder.iterdir()) == ["current.deb"]


def test_same_build_is_a_repair(tmp_path: Path) -> None:
    up, _node, _launcher, _layout = make(tmp_path, [(0, OLD)])
    up.start(open_package(node_zlp(tmp_path, OLD)))
    up.resume()
    assert up.record()["state"] == "done"  # type: ignore[index]


def test_resume_of_nothing(tmp_path: Path) -> None:
    up, *_ = make(tmp_path, [])
    assert up.resume() is None
    up.record_path.write_text("{broken")
    assert up.resume() is None and not up.open


def test_monkey_outcomes(tmp_path: Path) -> None:
    """Random job outcomes, with and without a previous package: it always ends, leaves
    only the installed .deb, and ``current.deb`` is the build that runs."""
    for seed in range(60):
        rnd = random.Random(seed)
        plan = [(rnd.choice([0, 0, 1, None]), rnd.choice([NEW, OLD, None])) for _ in range(2)]
        up, _node, launcher, layout = make(tmp_path / str(seed), plan, current=rnd.random() < 0.8)
        had = (up.folder / "current.deb").is_file()
        up.start(open_package(node_zlp(tmp_path / str(seed))))
        for _ in range(rnd.randint(1, 3)):  # a second Deployer start finds nothing to do
            up.resume()
        doc = up.record()
        assert doc is not None and doc["state"] in FINAL, seed
        names = left(up, layout)
        if doc["state"] == "done":
            assert (up.folder / "current.deb").read_bytes() == deb_bytes(NEW), seed
        elif had:
            assert (up.folder / "current.deb").read_bytes() == deb_bytes(OLD), seed
        assert names == (["current.deb"] if had or doc["state"] == "done" else []), seed
        assert len(launcher.launched) <= 2, seed


# ---------------------------------------------------------------- the job script itself


def fake_bin(folder: Path, dpkg_fails_with_lock: int, final_code: int) -> Path:
    folder.mkdir(parents=True)
    count = folder / "count"
    count.write_text("0")
    (folder / "dpkg").write_text(
        "#!/bin/sh\n"
        f'n=$(cat {count}); echo $((n+1)) > {count}\n'
        f'if [ "$n" -lt {dpkg_fails_with_lock} ]; then\n'
        '  echo "dpkg: error: dpkg frontend lock was locked by another process"; exit 2; fi\n'
        f'echo "Setting up zeroloss-agent ($1): unlocking nothing"; exit {final_code}\n'
    )  # fmt: skip
    (folder / "systemctl").write_text(f'#!/bin/sh\necho "$@" > {folder}/systemctl.args\n')
    for name in ("dpkg", "systemctl"):
        (folder / name).chmod(0o755)
    return folder


@pytest.mark.parametrize(("locks", "code", "runs", "want"), [
    (0, 0, 1, 0), (2, 0, 3, 0), (0, 1, 1, 1), (9, 0, 4, 2),  # 3 retries allowed here
])  # fmt: skip
def test_job_script(tmp_path: Path, locks: int, code: int, runs: int, want: int) -> None:
    bin_dir = fake_bin(tmp_path / "bin", locks, code)
    log, status = tmp_path / "log", tmp_path / "exit"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    subprocess.run(["/bin/sh", "-c", job_script(tries=3, wait_s=0), "sh", "/x/new.deb",
                    str(log), str(status)], env=env, check=True, timeout=20)  # fmt: skip
    assert int(status.read_text()) == want
    assert int((bin_dir / "count").read_text()) == runs
    assert not status.with_name("exit.tmp").exists()
    # both services come back even when dpkg failed (the Deployer must live, 11.5)
    assert (bin_dir / "systemctl.args").read_text().split() == [
        "start", "zeroloss-deployer.service", "zeroloss-agent.service"]  # fmt: skip


def test_launcher_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    Launcher().launch("abc", tmp_path / "new.deb", tmp_path / "l", tmp_path / "s")
    argv = seen[0]
    assert argv[:5] == ["systemd-run", "--unit=zeroloss-update-abc", "--collect", "--quiet",
                        "--"]  # fmt: skip
    assert argv[5:7] == ["/bin/sh", "-c"] and argv[-3:] == [
        str(tmp_path / "new.deb"), str(tmp_path / "l"), str(tmp_path / "s")]  # fmt: skip


def test_record_matches_02(tmp_path: Path) -> None:
    """02 15 lists the ``update`` fields: the record has exactly those."""
    text = (Path(__file__).resolve().parents[2] / "docs" / "02-data.md").read_text("utf-8")
    line = next(x for x in text.splitlines() if x.startswith("- `update`: 마지막 노드"))
    listed = line.split("`{")[1].split("}`")[0]
    names = {p.split(":")[0].strip().strip('"') for p in listed.split(", ")}
    up, *_ = make(tmp_path, [(0, NEW)])
    up.start(open_package(node_zlp(tmp_path)))
    doc = up.record()
    assert doc is not None
    assert set(doc) == names, (sorted(doc), sorted(names))
    assert json.dumps(doc)


# ---------------------------------------------------------------- through the Deployer (10991)


class HeldLauncher(FakeLauncher):
    """The job keeps running until ``release`` (to see the Deployer while it is open)."""

    def __init__(self, node: Any, plan: list[tuple[int | None, str | None]]) -> None:
        super().__init__(node, plan)
        self.held = threading.Event()
        self.pending: list[tuple[Path, int | None, str | None]] = []

    def launch(self, job_id: str, deb: Path, log: Path, status: Path) -> None:
        self.launched.append((job_id, deb.read_bytes()))
        code, build = self.plan.pop(0)
        self.pending.append((status, code, build))

    def release(self) -> None:
        for status, code, build in self.pending:
            if code is not None:
                status.write_text(f"{code}\n")
            self.node.build = build
        self.pending.clear()
        self.held.set()

    def running(self, job_id: str) -> bool:
        return not self.held.is_set()


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[Deployer, FakeNode, Client, HeldLauncher]]:
    node = FakeNode()
    layout = Layout(tmp_path / "data", tmp_path / "program" / "modules")
    layout.modules_dir.mkdir(parents=True)
    launcher = HeldLauncher(node, [(0, NEW)])
    d = Deployer(layout, AgentLink(node, node.port, come_back_s=5), "127.0.0.1", 0, build=OLD,
                 watch_s=0.5, poll_s=0.1, launcher=launcher)  # fmt: skip
    d.updates.exit_wait_s, d.updates.node_wait_s, d.updates.poll_s = 10.0, 2.0, 0.05
    d.start()
    (d.updates.folder / "current.deb").write_bytes(deb_bytes(OLD))
    yield d, node, Client(d.port), launcher
    launcher.held.set()
    d.stop()
    node.close()


def wait_state(c: Client, states: tuple[str, ...], limit_s: float = 10.0) -> dict[str, Any]:
    end = time.monotonic() + limit_s
    while time.monotonic() < end:
        up = c.call("GET", "/v1/deployer/health")["data"]["update"]
        if up and up["state"] in states:
            return dict(up)
        time.sleep(0.05)
    raise AssertionError(f"update never reached {states}")


def test_update_over_10991(served: Any, tmp_path: Path) -> None:
    d, node, c, launcher = served
    zlp = node_zlp(tmp_path / "pkg")
    assert c.put(zlp)["ok"]
    r = c.post("/v1/deployer/install", package=zlp.name)
    assert r["ok"], r
    assert r["data"]["result"] == "started" and r["data"]["kind"] == "node"
    h = c.call("GET", "/v1/deployer/health")["data"]
    assert h["busy"] is True and h["update"]["state"] == "running"
    assert list(d.layout.incoming.iterdir()) == []  # the .deb was taken out
    other = c.put(node_zlp(tmp_path / "pkg2"))
    assert other["ok"]
    again = c.post("/v1/deployer/install", package=other["data"]["name"])
    assert again["error"]["code"] == "ZL-INS-027"  # one at a time, also across restarts
    stop = c.post("/v1/deployer/node", action="restart")
    assert stop["error"]["code"] == "ZL-INS-027"
    node.answering = False  # the job stopped the node: the watch must not restart it
    time.sleep(1.0)
    assert node.restarts == 0
    node.answering = True
    launcher.release()
    up = wait_state(c, ("done", "rolled_back", "failed"))
    assert up["state"] == "done" and up["id"] == r["data"]["update_id"]
    assert c.call("GET", "/v1/deployer/health")["data"]["busy"] is False
    assert (d.updates.folder / "current.deb").read_bytes() == deb_bytes(NEW)
    assert sorted(p.name for p in d.updates.folder.iterdir()) == ["current.deb"]


def test_update_refused_during_a_test(served: Any, tmp_path: Path) -> None:
    d, node, c, launcher = served
    node.state = "RUNNING"
    zlp = node_zlp(tmp_path / "pkg")
    c.put(zlp)
    r = c.post("/v1/deployer/install", package=zlp.name)
    assert r["error"]["code"] == "ZL-INS-022" and not launcher.launched
    assert list(d.layout.incoming.iterdir()) == []  # 11.5 9: sent again next time
    assert c.call("GET", "/v1/deployer/health")["data"]["update"] is None


def test_new_deployer_carries_the_update_on(tmp_path: Path) -> None:
    """The job stopped the old Deployer; the one it started finds ``running`` (06 11.5)."""
    node = FakeNode()
    node.build = NEW
    layout = Layout(tmp_path / "data", tmp_path / "program" / "modules")
    layout.modules_dir.mkdir(parents=True)
    folder = layout.deployer / "packages"
    folder.mkdir(parents=True)
    (folder / "current.deb").write_bytes(deb_bytes(OLD))
    (folder / "previous.deb").write_bytes(deb_bytes(OLD))
    (folder / "new.deb").write_bytes(deb_bytes(NEW))
    (folder / "update-j1.exit").write_text("0\n")
    (layout.deployer / "update.json").write_text(json.dumps({
        "id": "j1", "job": "j1", "from_build": OLD, "to_build": NEW, "state": "running",
        "why": None, "started_utc": "x", "ended_utc": None, "no_rollback": False}))  # fmt: skip
    d = Deployer(layout, AgentLink(node, node.port, come_back_s=5), "127.0.0.1", 0, build=NEW,
                 watch=False, launcher=FakeLauncher(node, []))  # fmt: skip
    d.updates.poll_s = 0.05
    d.start()
    try:
        up = wait_state(Client(d.port), ("done", "rolled_back", "failed"))
        assert up["state"] == "done"
        assert sorted(p.name for p in folder.iterdir()) == ["current.deb"]
        assert (folder / "current.deb").read_bytes() == deb_bytes(NEW)
    finally:
        d.stop()
        node.close()


# ---------------------------------------------------------------- the Controller waits


@pytest.mark.parametrize(("plan", "final", "code"), [
    ([(0, NEW)], "updated", None),
    ([(1, OLD), (0, OLD)], "rolled_back", "ZL-INS-028"),
    ([(1, OLD), (1, None)], "failed", "ZL-INS-013"),
])  # fmt: skip
def test_controller_waits_through_the_deployer_restart(
    tmp_path: Path, plan: list[tuple[int | None, str | None]], final: str, code: str | None
) -> None:
    """``zeroloss deploy`` with a .zlp: "started", then the old Deployer goes away (the job
    stopped it), a new one on the same port carries the update on; the Controller keeps
    waiting through the gap and returns the end (06 11.5, 02 15)."""
    from controller.deploy import Deploy
    from controller.devices import Device, DeviceList
    from core.protocol.envelope import ZlError
    from tests.agent.test_deployer_e2e import free_port

    node = FakeNode()
    layout = Layout(tmp_path / "data", tmp_path / "program" / "modules")
    layout.modules_dir.mkdir(parents=True)
    port = free_port()
    launcher = HeldLauncher(node, plan)
    running: list[Deployer] = []

    def deployer(build: str, held: HeldLauncher) -> Deployer:
        d = Deployer(layout, AgentLink(node, node.port, come_back_s=5), "127.0.0.1", port,
                     build=build, watch=False, launcher=held)  # fmt: skip
        d.updates.exit_wait_s, d.updates.node_wait_s, d.updates.poll_s = 10.0, 1.0, 0.05
        d.start()
        running.append(d)
        return d

    first = deployer(OLD, launcher)
    (first.updates.folder / "current.deb").write_bytes(deb_bytes(OLD))

    def job_replaces_the_deployer() -> None:
        while not launcher.launched:
            time.sleep(0.02)
        first.stop()  # prerm stopped the service
        time.sleep(0.5)  # nobody answers on 10991 for a while
        launcher.release()
        second = HeldLauncher(node, launcher.plan)  # the rest of the plan (going back)
        second.held.set()
        second.pending = []
        orig = second.launch

        def launch_and_end(job_id: str, deb: Path, log: Path, status: Path) -> None:
            orig(job_id, deb, log, status)
            second.release()

        second.launch = launch_and_end  # type: ignore[method-assign]
        deployer(NEW, second)

    threading.Thread(target=job_replaces_the_deployer, daemon=True).start()
    devices = DeviceList(tmp_path / "devices.json")
    devices._devices["tx"] = Device("tx", "127.0.0.1", 1, "id", "sender", "now", {}, port)
    try:
        dep = Deploy(devices, "boot", update_wait_s=20.0, poll_s=0.1)
        if code is None:
            r = dep.install("tx", node_zlp(tmp_path / "pkg"))
            assert r["result"] == final and (r["from_build"], r["build"]) == (OLD, NEW)
            assert r["update"]["state"] == "done"
        else:
            with pytest.raises(ZlError) as e:
                dep.install("tx", node_zlp(tmp_path / "pkg"))
            assert e.value.code == code and "dpkg -i ended with 1" in e.value.why
        assert sorted(p.name for p in (layout.deployer / "packages").iterdir()) == [
            "current.deb"]  # fmt: skip
        assert list(layout.incoming.iterdir()) == []
    finally:
        for d in running:
            d.stop()
        node.close()


def test_a_stopped_deployer_leaves_the_update_to_the_next(served: Any, tmp_path: Path) -> None:
    """Deployer stop while it waits on the job: nothing is written, the record stays
    ``running`` for the Deployer the job starts (G-11: every end cleans up)."""
    d, _node, c, _launcher = served
    zlp = node_zlp(tmp_path / "pkg")
    c.put(zlp)
    assert c.post("/v1/deployer/install", package=zlp.name)["ok"]
    d.stop()
    time.sleep(0.2)
    assert d.updates.record()["state"] == "running"
    assert (d.updates.folder / "new.deb").exists() and (d.updates.folder / "previous.deb").exists()
