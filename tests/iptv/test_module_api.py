"""IPTV module over the node API: sender + PC receiver on one node, multicast kept on
the loopback interface (never leaves this PC)."""

from __future__ import annotations

import itertools
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.lifecycle.state_flow import State
from core.node import Node
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError
from modules.iptv.sender.commands import QUALITIES, prepare_command

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not (shutil.which("tsp") and shutil.which("ffmpeg")), reason="needs ffmpeg and tsp"
)
_groups = itertools.count(20)
S = "/v1/modules/iptv"


@pytest.fixture(scope="module")
def hd_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("prep") / "ch1.ts"
    subprocess.run(
        prepare_command("ffmpeg", QUALITIES["HD"], 1, out, seconds=4, preset="ultrafast"),
        check=True,
        timeout=300,
    )
    return out


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    n = Node(
        tmp_path / "agent",
        "sender+receiver",
        host="127.0.0.1",
        port=0,
        local_port=0,
        modules_dir=REPO / "modules",
        lost_after_s=60,
    )
    n.start()
    yield n
    n.stop()


def plan(hd_file: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    group = f"239.255.80.{next(_groups)}"
    tx = {
        "ch": 1,
        "file": str(hd_file),
        "dest_ip": group,
        "port": 5004,
        "muxrate_bps": 10_000_000,
        "ttl": 1,
        "local_address": "127.0.0.1",
    }
    rx = {
        "ch": 1,
        "group": group,
        "port": 5004,
        "interface_ip": "127.0.0.1",
        "device": "pc",
        "cell_s": 5,
    }
    return tx, rx


def wait(pred: Any, timeout_s: float = 20) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def test_full_cycle_over_http(node: Node, hd_file: Path) -> None:
    c = NodeClient("127.0.0.1", node.api.port, "controller-boot-1")
    tx, rx = plan(hd_file)
    started = c.command(f"{S}/sender/start", 0, "R1", timeout_s=15, **tx)
    assert started["state"] == "RUNNING" and started["ssrc"] >> 24 == 1
    c.command(f"{S}/receiver/start", 0, "R1", **rx)

    def measuring() -> bool:
        hb = c.get("/v1/heartbeat", seq=1)
        recv = hb["module_data"]["iptv"]["receivers"]
        return bool(recv and recv[0]["measuring"] and recv[0]["last_1s"])

    assert wait(measuring)
    assert node.flow.state is State.RUNNING
    time.sleep(7)
    hb = c.get("/v1/heartbeat", seq=2)
    last = hb["module_data"]["iptv"]["receivers"][0]["last_1s"]
    assert last["lost"] == 0 and last["rx"] > 800
    tx_rate = hb["module_data"]["iptv"]["sender"]["channels"][0]["tx_bitrate_bps"]
    assert abs(tx_rate - 10_000_000) < 200_000
    got = c.get("/v1/summaries", after=0)["summaries"]
    assert got and all(s["run_id"] == "R1" and s["device"] == "pc" for s in got)
    assert [s["seq"] for s in got] == sorted(s["seq"] for s in got)
    assert c.get("/v1/summaries", after=got[-1]["seq"])["summaries"] == []
    stopped = c.command(f"{S}/receiver/stop", 0, "R1", ch=1)
    assert stopped["startup"]["join_to_ready_ms"] is not None
    c.command(f"{S}/sender/stop", 0, "R1", ch=1)
    assert State(node.flow.state.value) is State.IDLE
    final = c.get("/v1/summaries", after=0)["summaries"]
    assert len(final) > len(got)  # the last partial cell made at stop is not lost
    assert sum(s["loss"]["lost"] for s in final) == 0
    c.close()


def test_duplicate_start_refused_and_bad_params(node: Node, hd_file: Path) -> None:
    c = NodeClient("127.0.0.1", node.api.port, "b")
    tx, _ = plan(hd_file)
    c.command(f"{S}/sender/start", 0, "R2", timeout_s=15, **tx)
    with pytest.raises(ZlError) as e:
        c.command(f"{S}/sender/start", 0, "R2", timeout_s=15, **tx)
    assert e.value.code == "ZL-RUN-012"
    with pytest.raises(ZlError) as e2:
        c.command(f"{S}/receiver/start", 0, "R2", ch="one")
    assert e2.value.code == "ZL-DSC-012"
    c.command(f"{S}/sender/stop", 0, "R2", ch=1)
    c.close()


def test_silent_controller_safe_stops_sender_and_receiver(tmp_path: Path, hd_file: Path) -> None:
    n = Node(
        tmp_path / "a",
        "x",
        host="127.0.0.1",
        port=0,
        local_port=0,
        modules_dir=REPO / "modules",
        lost_after_s=2,
    )
    n.start()
    try:
        c = NodeClient("127.0.0.1", n.api.port, "b")
        tx, rx = plan(hd_file)
        c.command(f"{S}/sender/start", 0, "R3", timeout_s=15, **tx)
        c.command(f"{S}/receiver/start", 0, "R3", **rx)
        c.close()  # Controller goes silent
        assert wait(lambda: n.flow.state is State.IDLE, 10)
        out = subprocess.run(["pgrep", "-x", "tsp"], capture_output=True, text=True).stdout
        assert out.strip() == ""  # tsp stopped and confirmed
        mod = next(m for m in n.modules if m.id == "iptv").instance
        assert mod is not None and not mod.senders and not mod.receivers  # type: ignore[attr-defined]
    finally:
        n.stop()


def test_channel_change_over_http(node: Node, hd_file: Path) -> None:
    """03 F19 through ``receiver/channel`` (02 4): answer at the first packet, the slot
    stays the key, the heartbeat shows the new channel, the zaps file is in the index."""
    c = NodeClient("127.0.0.1", node.api.port, "controller-boot-1")
    tx1, rx = plan(hd_file)
    tx2, rx2 = plan(hd_file)
    c.command(f"{S}/sender/start", 0, "R4", timeout_s=15, **tx1)
    c.command(f"{S}/sender/start", 0, "R4", timeout_s=15, **{**tx2, "ch": 2})
    c.command(f"{S}/receiver/start", 0, "R4", **rx)

    def receiver() -> dict[str, Any]:
        recv: list[dict[str, Any]] = c.get("/v1/heartbeat", seq=1)["module_data"]["iptv"][
            "receivers"
        ]
        return recv[0]

    assert wait(lambda: receiver()["measuring"])
    with pytest.raises(ZlError) as e:
        c.command(f"{S}/receiver/channel", 0, "R4", device="pc", ch=2, group=rx2["group"],
                  port=5004, join_method="C")  # fmt: skip
    assert e.value.code == "ZL-DSC-012"
    with pytest.raises(ZlError) as e:
        c.command(f"{S}/receiver/channel", 0, "R4", device="tab", ch=2, group=rx2["group"],
                  port=5004)  # fmt: skip
    assert e.value.code == "ZL-RUN-012"
    got = c.command(f"{S}/receiver/channel", 0, "R4", device="pc", ch=2, group=rx2["group"],
                    port=5004, join_method="B", success_timeout_s=5.0)  # fmt: skip
    assert got["changed"] and got["zap"]["t_first_packet"] is not None
    assert got["zap"]["join_method"] == "B" and got["zap"]["end"] in (None, "done")
    r = receiver()
    assert (r["ch"], r["slot"], r["measuring"]) == (2, 1, True)
    assert wait(lambda: receiver()["last_1s"].get("ch") == 2, 15)
    time.sleep(2)
    c.command(f"{S}/receiver/stop", 0, "R4", ch=1)  # the slot, not the channel now
    for ch in (1, 2):
        c.command(f"{S}/sender/stop", 0, "R4", ch=ch)
    names = {f["name"]: f["hour"] for f in c.get("/v1/records/index", run="R4")["files"]}
    assert names.get("R4_zaps.jsonl", "missing") is None and names.get("R4_h000.jsonl") == 0
    c.close()
