"""Discovery (06 10.2, 02 4-1): the node answers on UDP only between tests; the
Controller merges broadcast, sender tries and a range scan into one row per device."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from controller.devices import DeviceList
from controller.discover import discover
from core.blocks import discovery as disc
from core.blocks.discovery import ALL, Responder, ask, broadcast_addresses, nonce_of, request
from core.lifecycle.state_flow import State
from core.node import Node
from core.protocol.envelope import ZlError

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    n = Node(tmp_path / "agent", "agent", host="127.0.0.1", port=0, local_port=0,
             modules_dir=REPO / "modules", lost_after_s=60)  # fmt: skip
    n.start()
    yield n
    n.stop()


def test_request_format() -> None:
    assert nonce_of(request("abc123")) == "abc123"
    for bad in (b"", b"ZLDISCOVER 2 abc", b"ZLDISCOVER 1 ", b"ZLDISCOVER 1 a b", b"x" * 300):
        assert nonce_of(bad) is None


WIRED, WIFI = {"enp1": "192.168.45.5"}, {"wlp2": "192.168.1.9"}


@pytest.mark.parametrize(
    ("nics", "internet", "expected"),
    [
        ({**WIRED, **WIFI}, "wlp2", ["192.168.45.255"]),  # separate internet NIC: only the test one
        (WIRED, "enp1", ["192.168.45.255", ALL]),  # one NIC does both: nothing to keep off
        ({**WIRED, **WIFI}, None, ["192.168.45.255", "192.168.1.255", ALL]),  # no default route
    ],
)
def test_discovery_never_leaves_on_the_internet_nic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nics: dict[str, str],
    internet: str | None, expected: list[str],
) -> None:  # fmt: skip
    """13 1: with a separate internet NIC, neither it nor 255.255.255.255 (which would
    follow its default route) is used. A fake PC, never the machine the test runs on."""
    sys_root = tmp_path / "net"
    for name in nics:
        (sys_root / name).mkdir(parents=True)
    monkeypatch.setattr(disc, "ipv4_of", lambda n: nics.get(n))
    monkeypatch.setattr(disc, "ipv4_netmask", lambda n: "255.255.255.0" if n in nics else None)
    route = tmp_path / "route"
    default = f"{internet}\t00000000\t0100A8C0\t0003\n" if internet else ""
    route.write_text("Iface\tDestination\tGateway\tFlags\n" + default
                     + "lo\t0000007F\t00000000\t0001\n")  # fmt: skip
    assert broadcast_addresses(sys_root, route) == expected


def test_responder_answers_only_when_allowed() -> None:
    allowed = {"on": True}
    r = Responder(0, lambda: {"device_id": "d1"} if allowed["on"] else None)
    port = r._sock.getsockname()[1]
    r.start()
    try:
        got = ask(["127.0.0.1"], port, wait_s=0.8)
        assert got and got[0][0]["device_id"] == "d1" and got[0][1] == "127.0.0.1"
        assert len({b["nonce"] for b, _ in got}) == 1  # tries answered, same nonce
        allowed["on"] = False
        assert ask(["127.0.0.1"], port, wait_s=0.5) == []
    finally:
        r.stop()


def test_discover_merges_broadcast_try_and_scan(node: Node, tmp_path: Path) -> None:
    devices = DeviceList(tmp_path / "devices.json")
    port = node.api.port
    r = discover(devices, "b", port, scan="127.0.0.0/30", targets=lambda: ["127.0.0.1"],
                 sender_try=("127.0.0.1",))  # fmt: skip
    assert len(r["found"]) == 1 and r["sender_try"] == "found" and r["codes"] == []
    row = r["found"][0]
    assert row["device_id"] == node.device_id and row["how"] == "broadcast"
    assert row["address"] == "127.0.0.1" and row["port"] == port and not row["known"]
    assert row["role"] == "agent" and row["build"] == node.build and row["tcp_ok"] is True
    devices.add("rx-pc", "127.0.0.1", port, "b")
    again = discover(devices, "b", port, targets=lambda: ["127.0.0.1"], try_sender=False)
    assert again["found"][0]["known"] and again["found"][0]["name"] == "rx-pc"
    assert again["sender_try"] == "skipped"


def test_quiet_during_a_test_and_nothing_found(node: Node, tmp_path: Path) -> None:
    devices = DeviceList(tmp_path / "devices.json")
    node.flow.move(State.STARTING, reason="test")
    try:
        r = discover(devices, "b", node.api.port, targets=lambda: ["127.0.0.1"],
                     sender_try=("127.0.0.3",))  # fmt: skip
    finally:
        node.flow.move(State.STOPPING, reason="test")
        node.flow.move(State.IDLE, reason="test")
    assert r["found"] == [] and r["codes"] == ["ZL-DSC-001", "ZL-DSC-010"]
    for bad in ("10.0.0.0/16", "nope", "::1/128"):
        with pytest.raises(ZlError) as e:
            discover(devices, "b", node.api.port, scan=bad, targets=lambda: [])
        assert e.value.code == "ZL-RUN-014"


def test_found_but_control_port_closed_is_dsc_008(tmp_path: Path) -> None:
    """09 DSC-008: it answers discovery (UDP) but nothing answers on its TCP port."""
    import socket

    with socket.socket() as s:  # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    r = Responder(0, lambda: {"device_id": "d-fw", "host": "fw-pc", "role": "agent",
                              "build": "x", "port": closed})  # fmt: skip
    udp = r._sock.getsockname()[1]
    r.start()
    try:
        got = discover(DeviceList(tmp_path / "d.json"), "b", udp, try_sender=False,
                       targets=lambda: ["127.0.0.1"])  # fmt: skip
    finally:
        r.stop()
    (row,) = got["found"]
    assert row["tcp_ok"] is False and "ZL-DSC-008" in got["codes"]


def _registered_at(tmp_path: Path, node: Node, host: str) -> DeviceList:
    """A device list whose entry for ``node`` still has an old address."""
    import json

    path = tmp_path / "devices.json"
    DeviceList(path).add("tx-pc", "127.0.0.1", node.api.port, "b")
    doc = json.loads(path.read_text())
    doc["devices"][0]["host"] = host
    path.write_text(json.dumps(doc))
    return DeviceList(path)


def test_moved_device_gets_its_new_address(node: Node, tmp_path: Path) -> None:
    """06 10.2 (GAP-73 ④): installed on the router LAN, moved to its WAN port -> found at
    the sender address, the saved address follows; nobody registers it again."""
    devices = _registered_at(tmp_path, node, "127.0.0.9")  # the old LAN address: silent
    told: list[tuple[str, str, str]] = []
    r = discover(devices, "b", node.api.port, targets=lambda: [],
                 sender_try=("127.0.0.1",), on_move=lambda *a: told.append(a))  # fmt: skip
    new = f"127.0.0.1:{node.api.port}"
    assert r["moved"] == [{"name": "tx-pc", "from": f"127.0.0.9:{node.api.port}", "to": new}]
    assert told == [("tx-pc", f"127.0.0.9:{node.api.port}", new)]
    assert DeviceList(tmp_path / "devices.json").get("tx-pc").host == "127.0.0.1"  # saved
    assert r["found"][0]["known"] and r["found"][0]["name"] == "tx-pc"
    again = discover(devices, "b", node.api.port, targets=lambda: [], sender_try=("127.0.0.1",))
    assert again["moved"] == []  # nothing left to change


def test_no_move_when_the_old_address_still_answers(node: Node, tmp_path: Path) -> None:
    devices = DeviceList(tmp_path / "devices.json")
    devices.add("rx-pc", "127.0.0.1", node.api.port, "b")
    r = discover(devices, "b", node.api.port, targets=lambda: ["127.0.0.1"],
                 sender_try=("127.0.0.1",))  # fmt: skip
    assert r["moved"] == [] and devices.get("rx-pc").host == "127.0.0.1"


def test_no_move_onto_another_network(node: Node, tmp_path: Path) -> None:
    """GAP-153: this PC's test cable was out; the PCs answered only on the home Wi-Fi and
    were saved there, so the next test went around the router. A find on another network
    is reported (``kept``) but not saved; the WAN-side sender addresses still move."""
    devices = _registered_at(tmp_path, node, "10.9.9.9")  # the test network: silent now
    r = discover(devices, "b", node.api.port, targets=lambda: [],
                 sender_try=("127.0.0.1",))  # fmt: skip
    assert r["moved"]  # 127.0.0.1 is a sender address (router WAN side): it moves
    devices = _registered_at(tmp_path, node, "10.9.9.9")
    r = discover(devices, "b", node.api.port, targets=lambda: ["127.0.0.1"], sender_try=())
    assert r["moved"] == [] and devices.get("tx-pc").host == "10.9.9.9"
    assert r["kept"] == [{"name": "tx-pc", "at": "10.9.9.9", "seen": "127.0.0.1"}]


def test_same_network() -> None:
    from controller.discover import same_network

    assert same_network("192.168.45.177", "192.168.45.190")
    assert not same_network("192.168.45.177", "192.168.0.13")
    assert not same_network("10.0.0.1", "nonsense")
