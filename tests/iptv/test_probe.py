"""F17 step 5 multicast test and step 2 tools over the node API (loopback only)."""

from __future__ import annotations

import itertools
import shutil
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.node import Node
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError
from modules.iptv.probe import PROBE_BPS, probe_command

REPO = Path(__file__).resolve().parents[2]
S = "/v1/modules/iptv"
_groups = itertools.count(20)
needs_tsp = pytest.mark.skipif(not shutil.which("tsp"), reason="needs tsp")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[NodeClient]:
    n = Node(
        tmp_path / "agent", "sender+receiver", host="127.0.0.1", port=0, local_port=0,
        modules_dir=REPO / "modules", lost_after_s=60,
    )  # fmt: skip
    n.start()
    c = NodeClient("127.0.0.1", n.api.port, "controller-boot-1")
    yield c
    c.close()
    n.stop()
    assert not list((tmp_path / "agent" / "procs").glob("*.json"))  # no process left


def test_probe_command_is_a_bounded_null_stream() -> None:
    argv = probe_command("tsp", "239.1.1.1", 5004, 2, 4, None)
    assert argv[:4] == ["tsp", "-I", "null", str(2 * PROBE_BPS // (188 * 8))]
    assert "--rtp" in argv and "--local-address" not in argv
    assert argv[argv.index("--ttl") + 1] == "4"


@needs_tsp
def test_probe_arrives_and_wrong_group_gets_nothing(client: NodeClient) -> None:
    group, other = (f"239.255.81.{next(_groups)}" for _ in range(2))
    rx: dict[str, Any] = {"ch": 1, "port": 5006, "interface_ip": "127.0.0.1", "seconds": 4}
    good = client.command(f"{S}/receiver/probe", 0, None, group=group, **rx)["probe_id"]
    bad = client.command(f"{S}/receiver/probe", 0, None, group=other, **rx)["probe_id"]
    sent = client.command(
        f"{S}/sender/probe", 0, None, timeout_s=15, ch=1, dest_ip=group, port=5006,
        seconds=2, ttl=1, local_address="127.0.0.1",
    )  # fmt: skip
    assert 1.5 <= sent["sent_s"] < 6
    got = client.command(f"{S}/receiver/probe/result", 0, None, timeout_s=10, probe_id=good)
    datagrams = 2 * PROBE_BPS // (188 * 8) // 7
    assert got["packets"] >= datagrams * 0.9 and got["first_ms"] is not None
    none = client.command(f"{S}/receiver/probe/result", 0, None, timeout_s=10, probe_id=bad)
    assert none["packets"] == 0 and none["first_ms"] is None


def test_probe_params_checked(client: NodeClient) -> None:
    for seconds in (0, 31, True, "4"):
        with pytest.raises(ZlError) as e:
            client.command(
                f"{S}/receiver/probe", 0, None, ch=1, group="239.1.1.1", port=5006,
                seconds=seconds,
            )  # fmt: skip
        assert e.value.code == "ZL-DSC-012"
    with pytest.raises(ZlError) as e:
        client.command(f"{S}/receiver/probe/result", 0, None, probe_id="nope")
    assert e.value.code == "ZL-DSC-012"


def test_info_lists_the_module_tools(client: NodeClient) -> None:
    t0 = time.monotonic()
    tools = client.get("/v1/info")["tools"]
    assert set(tools) == {"ffmpeg", "ffprobe", "tsp", "mpv", "vlc", "iperf3"}  # + throughput
    for name in tools:
        if shutil.which(name) or (name == "vlc" and shutil.which("cvlc")):
            assert tools[name]["path"] and tools[name]["version"], name
        else:
            assert tools[name] is None
    assert client.get("/v1/info")["tools"] == tools
    assert time.monotonic() - t0 < 5


def test_an_unknown_card_listens_on_every_card_and_names_the_one_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """02 4 (GAP-115): no card in the plan -> join on every card; the result says which
    card the test packets came in on (``arrived_on``), or null when none came."""
    import socket as so

    from modules.iptv import probe as pr

    monkeypatch.setattr(pr, "every_card", lambda: ["127.0.0.1"])  # this PC's cards, here lo
    lis = pr.Listener()
    group, port = "239.255.78.1", 45900
    pid = lis.start(group, port, "0.0.0.0", 2)
    with so.socket(so.AF_INET, so.SOCK_DGRAM) as s:
        s.setsockopt(so.IPPROTO_IP, so.IP_MULTICAST_TTL, 0)
        s.setsockopt(so.IPPROTO_IP, so.IP_MULTICAST_IF, so.inet_aton("127.0.0.1"))
        for _ in range(20):
            s.sendto(b"\x47" + bytes(187), (group, port))
            time.sleep(0.01)
    got = lis.result(pid)
    assert got["packets"] == 20 and got["arrived_on"] == {"nic": "lo", "ip": "127.0.0.1"}
    assert got["arrived_cards"] == [{"nic": "lo", "ip": "127.0.0.1", "packets": 20}]
    none = lis.result(lis.start(group, port + 1, "0.0.0.0", 1))
    assert none["packets"] == 0 and none["arrived_on"] is None and none["arrived_cards"] == []


def test_two_cards_on_one_lan_are_both_listed_most_first() -> None:
    from modules.iptv.probe import arrived_cards, arrived_on

    lo = socket.if_nametoindex("lo")
    cards = {lo: ["127.0.0.1", 3], 999_999: ["192.168.45.190", 40]}  # an unknown index too
    assert arrived_cards(cards) == [{"nic": "999999", "ip": "192.168.45.190", "packets": 40},
                                    {"nic": "lo", "ip": "127.0.0.1", "packets": 3}]  # fmt: skip
    assert arrived_on(cards) == {"nic": "999999", "ip": "192.168.45.190"}
