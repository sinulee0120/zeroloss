"""The Linux node package really built (docs/06-install.md 4.3, 4.3-1, 11.3): what
is inside, and that the frozen programs work the way the installed ones will — the
node loads a module from the Deployer's folder and runs its work (standard modules
that only the module uses are in the program), the Deployer answers on its port."""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from modules.iptv.sender.commands import QUALITIES, prepare_command
from tools.build import build_deb, build_node_zlp
from tools.versioning import read_version

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not (shutil.which("dpkg-deb") and shutil.which("ffmpeg") and shutil.which("tsp")),
    reason="needs dpkg-deb, ffmpeg, tsp",
)


@pytest.fixture(scope="module")
def deb(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_deb(tmp_path_factory.mktemp("deb"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def get(port: int, path: str) -> Any:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    body = json.loads(conn.getresponse().read())
    conn.close()
    return body["data"]


def post(api: int, path: str, **params: Any) -> Any:
    conn = http.client.HTTPConnection("127.0.0.1", api, timeout=20)
    body = json.dumps({"command_id": str(time.time()), "generation": 0, "params": params})
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    out = json.loads(conn.getresponse().read())
    conn.close()
    return out


def wait_for(port: int, path: str, limit: float = 20) -> Any:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        try:
            return get(port, path)
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"nothing on {port}{path}")


def test_package_contents(deb: Path) -> None:
    files = subprocess.run(["dpkg-deb", "-c", str(deb)], capture_output=True, text=True,
                           check=True).stdout  # fmt: skip
    for path in ("./opt/zeroloss/agent/zeroloss", "./opt/zeroloss/deployer/zeroloss-deployer",
                 "./lib/systemd/system/zeroloss-agent.service",
                 "./lib/systemd/system/zeroloss-deployer.service",
                 "./etc/zeroloss/zeroloss.conf"):  # fmt: skip
        assert path in files, path
    assert "./opt/zeroloss/desktop/zeroloss-agent-desktop" in files  # 03 F20
    assert "./etc/xdg/autostart/zeroloss-agent-desktop.desktop" in files
    qt = [x for x in files.splitlines() if "PySide6" in x]
    assert qt and all("./opt/zeroloss/desktop/" in x for x in qt)  # never in the node
    assert "/modules/" not in files  # modules come as .zlm
    assert "./etc/sysctl.d" not in files  # written by the scripts, not a conffile (4.3)
    info = subprocess.run(["dpkg-deb", "-f", str(deb)], capture_output=True, text=True,
                          check=True).stdout  # fmt: skip
    assert "Package: zeroloss-agent" in info and "Architecture: amd64" in info
    assert "Recommends: libxcb-cursor0" in info  # Agent Desktop's screen (GAP-73 ②)
    control = subprocess.run(["dpkg-deb", "-e", str(deb), str(deb.parent / "ctl")], check=True)
    assert control.returncode == 0
    assert (deb.parent / "ctl/conffiles").read_text() == "/etc/zeroloss/zeroloss.conf\n"
    for script in ("postinst", "prerm", "postrm"):
        assert (deb.parent / "ctl" / script).stat().st_mode & 0o111


@pytest.fixture
def frozen_node(deb: Path, tmp_path: Path) -> Iterator[tuple[int, Path]]:
    program = deb.parent / "pyinstaller/dist/zeroloss/zeroloss"
    mods = tmp_path / "opt-modules"
    shutil.copytree(REPO / "modules" / "iptv", mods / "iptv",
                    ignore=shutil.ignore_patterns("__pycache__"))  # fmt: skip
    port = free_port()
    proc = subprocess.Popen(
        [str(program), "--port", str(free_port()), "serve", "--role", "agent",
         "--data", str(tmp_path / "data"), "--modules", str(mods), "--node-port", str(port)],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )  # fmt: skip
    try:
        wait_for(port, "/v1/health")
        yield port, tmp_path / "data"
    finally:
        proc.terminate()
        proc.wait(15)


def test_node_zlp_holds_the_deb(deb: Path, tmp_path: Path) -> None:
    """06 11.5: the .deb goes over 10991 inside a .zlp; the Deployer's real dpkg-deb
    reading gives the name and build it checks."""
    from agent.deployer.node_update import Launcher
    from agent.deployer.package import open_package

    pkg = open_package(build_node_zlp(deb))
    assert (pkg.kind, pkg.id, pkg.info["os"], pkg.build) == ("node", "agent", "linux",
                                                            read_version())  # fmt: skip
    assert list(pkg.files) == [deb.name]
    pkg.extract(deb.name, tmp_path / "new.deb")
    fields = Launcher().fields(tmp_path / "new.deb")
    assert (fields["Package"], fields["Version"]) == ("zeroloss-agent", pkg.build)
    assert not (deb.parent / "zlp-stage").exists()


def test_frozen_node_loads_and_runs_a_module(frozen_node: tuple[int, Path], tmp_path: Path) -> None:
    """The module's own code runs inside the frozen program: tsp sending, the receive
    socket, RTP and TS checks (standard modules only the module uses are in the build,
    06 4.3-1). Loopback only. Making a 10-minute generated video frozen was checked
    once by hand on 2026-09-25 (spikes: 300 s, ready)."""
    port, _ = frozen_node
    info = get(port, "/v1/info")
    assert info["modules"] == [{"id": "iptv", "enabled": True, "reason": ""}]
    ts = tmp_path / "ch1.ts"
    subprocess.run(prepare_command("ffmpeg", QUALITIES["HD"], 1, ts, seconds=4, preset="ultrafast"),
                   check=True, timeout=300)  # fmt: skip
    group, media = "239.255.97.1", free_port()
    sent = post(port, "/v1/modules/iptv/sender/start", ch=1, quality="HD", file=str(ts),
                dest_ip=group, port=media, muxrate_bps=10_000_000, ttl=16,
                local_address="127.0.0.1")  # fmt: skip
    assert sent["ok"], sent
    try:
        got = post(port, "/v1/modules/iptv/receiver/start", ch=1, group=group, port=media,
                   interface_ip="127.0.0.1", device="pc", cell_s=300, warmup_max_s=15,
                   playback=None)  # fmt: skip
        assert got["ok"], got
        end = time.monotonic() + 30
        while True:
            recv = get(port, "/v1/heartbeat?seq=1")["module_data"]["iptv"]["receivers"]
            if recv and recv[0]["measuring"] and recv[0]["last_1s"]:
                break
            assert time.monotonic() < end, recv
            time.sleep(0.5)
        time.sleep(2)
        r = get(port, "/v1/heartbeat?seq=2")["module_data"]["iptv"]["receivers"][0]
        assert r["lost_total"] == 0 and r["last_1s"]["rx"] > 0, r
    finally:
        post(port, "/v1/modules/iptv/receiver/stop", ch=1)
        post(port, "/v1/modules/iptv/sender/stop", ch=1)


def test_frozen_deployer_answers(deb: Path, tmp_path: Path) -> None:
    program = deb.parent / "pyinstaller/dist/zeroloss-deployer/zeroloss-deployer"
    port = free_port()
    proc = subprocess.Popen(
        [str(program), "--data", str(tmp_path / "data"), "--modules", str(tmp_path / "mods"),
         "--host", "127.0.0.1", "--port", str(port), "--agent-port", str(free_port()),
         "--service", "systemd:zeroloss-test-none.service", "--no-watch"],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )  # fmt: skip
    try:
        h = wait_for(port, "/v1/deployer/health")
        assert h["deployer_api"] == 1 and h["agent"]["responding"] is False
    finally:
        proc.terminate()
        proc.wait(15)


def test_frozen_desktop_connects_and_loads_the_player(deb: Path, tmp_path: Path) -> None:
    """03 F20: the installed Agent Desktop (offscreen here) loads the module's player from
    the module folder and takes the node's job list (a second Desktop is refused)."""
    import os

    from core.node import Node
    from core.protocol.client import NodeClient
    from core.protocol.envelope import ZlError

    mods = tmp_path / "opt-modules"
    shutil.copytree(REPO / "modules" / "iptv", mods / "iptv",
                    ignore=shutil.ignore_patterns("__pycache__"))  # fmt: skip
    node = Node(tmp_path / "node", "agent", host="127.0.0.1", port=0, local_port=0,
                player_host="desktop")  # fmt: skip
    node.start()
    program = deb.parent / "pyinstaller/dist/zeroloss-agent-desktop/zeroloss-agent-desktop"
    # its own single-instance name: the PC's real Agent Desktop (same user) would otherwise
    # be asked to show itself and this one would quit at once (app.py _bring_up_the_first)
    me = f"zl-test-{os.getpid()}"
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "XDG_DATA_HOME": str(tmp_path / "x"),
           "USER": me, "LOGNAME": me}  # fmt: skip
    proc = subprocess.Popen(
        [str(program), "--port", str(node.local.port), "--modules", str(mods)],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    client = NodeClient("127.0.0.1", node.local.port, "t")
    try:
        assert node.desktop.wait_connected(30), "the frozen Desktop never polled"
        with pytest.raises(ZlError) as e:
            client.get("/v1/desktop/jobs", desktop_id="someone-else")
        assert "Another Agent Desktop" in e.value.what
        node.desktop.want("r/x/ch1/player", "iptv", "player",
                          {"url": "rtp://@239.255.9.9:5004", "headless": True})  # fmt: skip
        end = time.monotonic() + 20
        while time.monotonic() < end:
            s = node.desktop.state("r/x/ch1/player")
            if s is not None and s.state in ("RUNNING", "FAULT"):
                break
            time.sleep(0.2)
        assert s is not None and s.state == "RUNNING", s  # the module's player part loaded
        node.desktop.drop("r/x/ch1/player")
        assert node.desktop.wait_gone("r/x/ch1/player", 40)
        assert proc.poll() is None  # still running (closing the window only hides it)
    finally:
        client.close()
        proc.terminate()
        out = proc.communicate(timeout=15)[0]
        node.stop()
    assert b"Traceback" not in out, out[-2000:]
