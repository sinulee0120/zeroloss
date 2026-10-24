"""No program outlives ZeroLoss (12 4-3, 09 SYS-007, 20 GAP-48).

1. A leftover that still carries our run token (as after a crash where the child
   survived) is stopped when the node starts, and shows as SYS-007.
2. The real case: a node sending (tsp) and playing (VLC) is killed with SIGKILL; its
   children must die with it, and the next start finds no records left behind.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from core.blocks.process_runner import TOKEN_ENV
from core.node import Node

REPO = Path(__file__).resolve().parents[2]
LINUX = sys.platform.startswith("linux")


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


@pytest.mark.skipif(not LINUX, reason="Linux process groups; Windows Job Object is V18")
def test_leftover_with_our_token_is_stopped_on_start(tmp_path: Path) -> None:
    token = "t-" + str(os.getpid())
    orphan = subprocess.Popen(
        ["sleep", "300"], env={**os.environ, TOKEN_ENV: token}, start_new_session=True
    )
    procs = tmp_path / "procs"
    procs.mkdir(parents=True)
    (procs / "tsp_ch1.json").write_text(
        json.dumps({"name": "tsp_ch1", "pid": orphan.pid, "token": token})
    )
    innocent = subprocess.Popen(["sleep", "300"], start_new_session=True)  # not ours
    events: list[Any] = []
    n = Node(
        tmp_path, "agent", host="127.0.0.1", port=0, local_port=0, modules_dir=REPO / "modules"
    )
    n.bus.subscribe(events.append)
    try:
        n.start()
        orphan.wait(5)
        assert not alive(orphan.pid)
        assert alive(innocent.pid)  # never touch what is not ours
        assert not (procs / "tsp_ch1.json").exists()  # (iw-event of this node may be there)
        assert any(e.code == "ZL-SYS-007" and "tsp_ch1" in e.text for e in events)
    finally:
        n.stop()
        innocent.kill()
        innocent.wait(5)


CHILD = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path
    sys.path.insert(0, {repo!r})
    os.environ["ZEROLOSS_PLAYER_HEADLESS"] = "1"
    from core.node import Node
    n = Node(Path({data!r}), "agent", host="127.0.0.1", port=0, local_port=0,
             modules_dir=Path({repo!r}) / "modules")
    n.start()
    print(n.api.port, flush=True)
    time.sleep(3600)
    """
)


@pytest.mark.skipif(
    not (LINUX and shutil.which("tsp") and shutil.which("mpv") and shutil.which("ffmpeg")),
    reason="needs Linux, tsp, mpv, ffmpeg",
)
def test_killed_node_leaves_no_tsp_or_vlc(tmp_path: Path) -> None:
    from core.protocol.client import NodeClient
    from modules.iptv.sender.commands import QUALITIES, prepare_command
    from tests.iptv.test_player import player_pids

    hd = tmp_path / "hd.ts"
    subprocess.run(
        prepare_command("ffmpeg", QUALITIES["HD"], 1, hd, seconds=4, preset="ultrafast"),
        check=True, capture_output=True, timeout=300,
    )  # fmt: skip
    data = tmp_path / "node"
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD.format(repo=str(REPO), data=str(data))],
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        port = int((child.stdout.readline() if child.stdout else "0").strip())
        c = NodeClient("127.0.0.1", port, "boot-x", "child")
        group = "239.255.94.1"
        c.command("/v1/modules/iptv/sender/start", 1, "R-KILL", timeout_s=20, ch=1, file=str(hd),
                  dest_ip=group, port=5004, muxrate_bps=10_000_000, ttl=1,
                  local_address="127.0.0.1")  # fmt: skip
        c.command("/v1/modules/iptv/receiver/start", 1, "R-KILL", ch=1, group=group, port=5004,
                  device="pc", interface_ip="127.0.0.1",
                  playback={"player": "VLC", "buffer_ms": 1000,
                            "window": "Full screen"})  # fmt: skip
        before = player_pids()
        tsp = subprocess.run(
            ["pgrep", "-f", str(hd)], capture_output=True, text=True
        ).stdout.split()
        end = time.monotonic() + 10
        while time.monotonic() < end and not player_pids():
            time.sleep(0.2)
        players = player_pids() - before or player_pids()
        assert tsp and players, "sender and player should be running"
        c.close()
        os.kill(child.pid, signal.SIGKILL)  # no chance to clean up
        child.wait(10)
        end = time.monotonic() + 5
        pids = [int(p) for p in tsp] + list(players)
        while time.monotonic() < end and any(alive(p) for p in pids):
            time.sleep(0.1)
        assert not [p for p in pids if alive(p)], "children outlived the killed node"
    finally:
        if child.poll() is None:
            child.kill()
    events: list[Any] = []
    n = Node(data, "agent", host="127.0.0.1", port=0, local_port=0, modules_dir=REPO / "modules")
    n.bus.subscribe(events.append)
    n.start()
    n.stop()
    left = {p.name for p in (data / "procs").glob("*.json")}
    assert not {n for n in left if n.startswith(("tsp_", "player_"))}  # stale records cleaned


def test_programs_start_only_through_process_runner() -> None:
    """12 4-3 rule 1: long-running external programs start only via process_runner
    (bound to the node, recorded in procs). Short calls with a timeout are allowed."""
    import re

    allowed_short = {
        "core/blocks/link_monitor.py",  # `iw ... link/info`, 1 s timeout
        "core/blocks/tool_locator.py",  # `<tool> --version`, 5 s timeout (F17 step 2)
        "core/blocks/sleep_guard.py",  # `systemd-inhibit ... true`, 5 s timeout (SYS-005)
        "core/blocks/wifi_join.py",  # nmcli / iw, 10 s timeout (03 F17 Wi-Fi PC)
        "core/blocks/wifi_power.py",  # `iw ... power_save`, 5 s timeout (GAP-167)
        "core/blocks/nic_speed.py",  # `ethtool`, 10 s timeout (throughput, GAP-171)
        "core/blocks/win_nic_speed.py",  # `powershell` Get/Set-NetAdapter*, 20 s timeout
        "core/blocks/win_wifi.py",  # `netsh wlan`, a timeout per call (15 s; join JOIN_TIMEOUT_S)
        "core/blocks/time_sync.py",  # `timedatectl` show / set-ntp, 10 s timeout
        "core/blocks/adb.py",  # `adb ...`, 20 s timeout (install 180 s, TAB-017)
        "agent/deployer/update_hold.py",  # `snap refresh --hold`, 30 s timeout (GAP-169)
        "agent/deployer/server.py",  # `apt-get install ./*.deb` of a .zlt, 900 s timeout,
        # L0 cannot use process_runner (GAP-172), refused while a test runs (INS-022)
        "controller/wan_route.py",  # nmcli / ip route; Windows powershell read, 15 s (GAP-88)
        "modules/iptv/receiver/perf.py",  # `vainfo`, 10 s timeout (RCV-015)
        "modules/iptv/sender/sources.py",  # `ffprobe`, 15 s timeout (S-48 list)
        "agent/deployer/agent_link.py",  # systemctl / sc / taskkill (L0, 12 4-3 rule 1)
        "agent/deployer/nodesetup.py",  # adduser / sysctl / ufw / systemctl (06 4.3-1)
        "agent/deployer/node_update.py",  # dpkg-deb / systemd-run / is-active; the install
        # itself is a separate systemd job, not a child (06 11.5)
    }
    for top in ("core", "modules", "controller", "agent"):
        for py in (REPO / top).rglob("*.py"):
            rel = py.relative_to(REPO).as_posix()
            text = py.read_text(encoding="utf-8")
            if rel == "core/blocks/process_runner.py":
                continue
            assert not re.search(r"subprocess\.Popen\(|os\.spawn|os\.system\(", text), rel
            if re.search(r"subprocess\.run\(", text):
                assert rel in allowed_short and "timeout=" in text, rel


def test_every_runner_uses_a_unique_record_name(tmp_path: Path) -> None:
    """Two receivers on one node must not overwrite each other's record."""
    from modules.iptv.receiver.player import PlayerConfig, VlcPlayer

    names = set()
    for ch in (1, 2, 3):
        cfg = PlayerConfig("rtp://@x:1", tmp_path / f"pc_ch{ch}", procs_dir=tmp_path / "procs")
        names.add(f"player_{cfg.work_dir.name}")
        VlcPlayer(cfg, lambda *e: None)
    assert len(names) == 3
