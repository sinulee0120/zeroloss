"""One-address DHCP server (07 3.2 B, RFC 2131) and the ISP role around it. Loopback
on high ports and a fake ``ip`` / fake NICs: nothing real on this PC is changed."""

from __future__ import annotations

import itertools
import json
import socket
import struct
import time
from pathlib import Path
from typing import Any

import pytest

from core.blocks import dhcp_server as d
from core.blocks.arp_probe import ProbeResult
from core.blocks.wan_isp import LEASE_IP, SERVER, WanIsp

_ports = itertools.count(16700, 2)
DUT = bytes.fromhex("020000000001")
OTHER = bytes.fromhex("020000000002")


class Client:
    def __init__(self, port: int) -> None:
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("127.0.0.1", port))
        self.s.settimeout(1.0)

    def ask(self, server_port: int, mac: bytes, kind: int, **opts: bytes) -> d.Message | None:
        options = {50: opts["req"]} if "req" in opts else {}
        if "sid" in opts:
            options[54] = opts["sid"]
        self.s.sendto(
            d.packet(1, 7, 0x8000, mac, kind, options=options), ("127.0.0.1", server_port)
        )
        try:
            data, _ = self.s.recvfrom(1500)
        except TimeoutError:
            return None
        return d.parse(data)

    def close(self) -> None:
        self.s.close()


def server(tmp: Path, **kw: Any) -> tuple[d.OneLeaseServer, int, int, list[tuple[str, str]]]:
    sp = next(_ports)
    events: list[tuple[str, str]] = []
    srv = d.OneLeaseServer("lo", "10.0.0.1", 24, "10.0.0.100", tmp / "wan-lease.json",
                           lambda c, lv, data: events.append((c, str(data.get("text")))),
                           server_port=sp, client_port=sp + 1, bind_device=False,
                           reply_to="127.0.0.1", **kw)  # fmt: skip
    srv.start()
    return srv, sp, sp + 1, events


def ip(v: str) -> bytes:
    return socket.inet_aton(v)


def test_discover_offer_request_ack_and_one_address_only(tmp_path: Path) -> None:
    srv, sp, cp, events = server(tmp_path)
    c = Client(cp)
    try:
        offer = c.ask(sp, DUT, d.DISCOVER)
        assert offer is not None and offer.kind == d.OFFER and offer.server_id == "10.0.0.1"
        ack = c.ask(sp, DUT, d.REQUEST, req=ip("10.0.0.100"), sid=ip("10.0.0.1"))
        assert ack is not None and ack.kind == d.ACK
        saved = json.loads((tmp_path / "wan-lease.json").read_text())["lease"]
        assert saved["mac"] == "02:00:00:00:00:01" and saved["ip"] == "10.0.0.100"
        assert any("got 10.0.0.100" in text for _, text in events)
        assert c.ask(sp, OTHER, d.DISCOVER) is None  # the one address is taken
        nak = c.ask(sp, OTHER, d.REQUEST, req=ip("10.0.0.100"), sid=ip("10.0.0.1"))
        assert nak is not None and nak.kind == d.NAK
        assert c.ask(sp, DUT, d.REQUEST, req=ip("10.0.0.100"), sid=ip("9.9.9.9")) is None
        again = c.ask(sp, DUT, d.REQUEST, req=ip("10.0.0.100"))  # renewal: same address
        assert again is not None and again.kind == d.ACK
        assert any("renewed" in text for _, text in events)
    finally:
        c.close()
        srv.stop()
    srv2, _, _, _ = server(tmp_path)  # a restarted node keeps the lease
    try:
        assert srv2.status()["lease"]["mac"] == "02:00:00:00:00:01"
    finally:
        srv2.stop()


def test_reply_options_and_release(tmp_path: Path) -> None:
    srv, sp, cp, _ = server(tmp_path)
    c = Client(cp)
    try:
        raw = srv.handle(d.Message(1, 9, 0, "0.0.0.0", DUT, d.DISCOVER, None, None))
        assert raw is not None
        opts = d._options(raw[240:])
        assert socket.inet_ntoa(raw[16:20]) == "10.0.0.100"  # yiaddr
        assert opts[1] == ip("255.255.255.0") and opts[3] == ip("10.0.0.1")
        assert struct.unpack("!I", opts[51])[0] == d.LEASE_S and 6 not in opts  # no DNS
        c.ask(sp, DUT, d.REQUEST, req=ip("10.0.0.100"), sid=ip("10.0.0.1"))
        c.ask(sp, DUT, d.RELEASE)
        time.sleep(0.2)
        assert srv.status()["lease"] is None
        assert c.ask(sp, OTHER, d.DISCOVER) is not None  # free again
    finally:
        c.close()
        srv.stop()


def test_own_nic_never_gets_the_routers_address(tmp_path: Path) -> None:
    srv, sp, cp, _ = server(tmp_path, ignore_macs=frozenset({"02:00:00:00:00:01"}))
    c = Client(cp)
    try:
        assert c.ask(sp, DUT, d.DISCOVER) is None and srv.status()["requests"] == 0
    finally:
        c.close()
        srv.stop()


class Holder:
    """The ARP check of 10.0.0.100 (07 3.2 B, GAP-112): what it answers, and its calls."""

    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, nic: str, own_ip: str, target_ip: str) -> bool | None:
        self.calls.append((nic, own_ip, target_ip))
        return self.answer


def moving(tmp: Path, holder: Holder, clock: list[float]) -> tuple[
        d.OneLeaseServer, list[tuple[str, str, dict[str, Any]]]]:  # fmt: skip
    """A server that is not started: ``handle`` only, the check runs inline."""
    events: list[tuple[str, str, dict[str, Any]]] = []
    srv = d.OneLeaseServer("eth9", "10.0.0.1", 24, "10.0.0.100", tmp / "wan-lease.json",
                           lambda c, lv, data: events.append((c, lv, data)), alive=holder,
                           run_async=lambda f: f(), clock=lambda: clock[0])  # fmt: skip
    return srv, events


def ask(srv: d.OneLeaseServer, mac: bytes, kind: int, req: str | None = None) -> int | None:
    raw = srv.handle(d.Message(1, 5, 0, "0.0.0.0", mac, kind, req, None))
    got = d.parse(raw) if raw else None
    return got.kind if got else None


def take(srv: d.OneLeaseServer, mac: bytes) -> None:
    assert ask(srv, mac, d.DISCOVER) == d.OFFER
    assert ask(srv, mac, d.REQUEST, "10.0.0.100") == d.ACK


def test_the_address_follows_the_cable_both_ways(tmp_path: Path) -> None:
    """GAP-112: router WAN -> the receiver PC of a baseline run -> the router again, no
    hand step: the old holder no longer answers ARP, so the new MAC gets 10.0.0.100."""
    holder, clock = Holder(False), [100.0]
    srv, events = moving(tmp_path, holder, clock)
    take(srv, DUT)  # the router WAN
    assert ask(srv, OTHER, d.DISCOVER) is None  # first the check, no answer yet
    assert holder.calls == [("eth9", "10.0.0.1", "10.0.0.100")]
    take(srv, OTHER)  # the receiver PC: nobody answered for the old holder
    moved = [e for e in events if "moved" in str(e[2].get("text"))]
    assert moved and moved[-1][2]["text"] == (
        "Router WAN address moved from 02:00:00:00:00:01 to 02:00:00:00:00:02"
    )
    assert moved[-1][2]["clears"] == ["ZL-SND-024"]
    saved = json.loads((tmp_path / "wan-lease.json").read_text())["lease"]
    assert saved["mac"] == "02:00:00:00:00:02"
    clock[0] += 3600  # the cables go back: the router asks for its old address first
    assert ask(srv, DUT, d.REQUEST, "10.0.0.100") == d.NAK  # checked, not yet known
    take(srv, DUT)
    assert srv.status()["lease"]["mac"] == "02:00:00:00:00:01"
    assert not any(c == "ZL-SND-024" for c, _, _ in events)


def test_a_holder_that_still_answers_keeps_the_address(tmp_path: Path) -> None:
    """Two devices on one ISP card: never two holders of 10.0.0.100 (SND-024 once)."""
    holder, clock = Holder(True), [100.0]
    srv, events = moving(tmp_path, holder, clock)
    take(srv, DUT)
    for _ in range(3):
        assert ask(srv, OTHER, d.DISCOVER) is None
    assert len(holder.calls) == 1  # one check per 10 s, not one per request
    assert ask(srv, OTHER, d.REQUEST, "10.0.0.100") == d.NAK
    clock[0] += d.PROBE_EVERY_S
    assert ask(srv, OTHER, d.DISCOVER) is None and len(holder.calls) == 2
    warns = [e for e in events if e[0] == "ZL-SND-024"]
    assert len(warns) == 1 and warns[0][1] == "warn"
    assert warns[0][2]["text"] == "Another device holds 10.0.0.100 on eth9"
    assert "02:00:00:00:00:01 still answers" in warns[0][2]["why"]
    assert srv.status()["lease"]["mac"] == "02:00:00:00:00:01"
    holder.answer, clock[0] = False, clock[0] + d.PROBE_EVERY_S  # the router is unplugged
    assert ask(srv, OTHER, d.DISCOVER) is None
    take(srv, OTHER)
    assert srv.status()["lease"]["mac"] == "02:00:00:00:00:02"


def test_a_check_that_cannot_run_never_moves_the_address(tmp_path: Path) -> None:
    holder, clock = Holder(None), [100.0]  # no packet socket
    srv, events = moving(tmp_path, holder, clock)
    take(srv, DUT)
    assert ask(srv, OTHER, d.DISCOVER) is None and ask(srv, OTHER, d.DISCOVER) is None
    warns = [e for e in events if e[0] == "ZL-SND-024"]
    assert len(warns) == 1 and "cannot check" in warns[0][2]["why"]
    assert srv.status()["lease"]["mac"] == "02:00:00:00:00:01"


def test_the_holder_itself_and_a_free_address_are_never_checked(tmp_path: Path) -> None:
    holder, clock = Holder(False), [100.0]
    srv, _ = moving(tmp_path, holder, clock)
    take(srv, DUT)
    assert ask(srv, DUT, d.REQUEST, "10.0.0.100") == d.ACK  # renewal
    assert holder.calls == []


def test_other_server_found(tmp_path: Path) -> None:
    srv, sp, cp, _ = server(tmp_path)
    try:
        found = d.other_servers("lo", OTHER, 1.0, server_port=sp, client_port=cp,
                                bind_device=False, to="127.0.0.1")  # fmt: skip
        assert found == ["10.0.0.1"]
    finally:
        srv.stop()
    none = d.other_servers("lo", OTHER, 0.3, server_port=sp, client_port=cp,
                           bind_device=False, to="127.0.0.1")  # fmt: skip
    assert none == []
    for bad in (b"", b"x" * 300, bytes(240)):
        assert d.parse(bad) is None


# ---------------------------------------------------------------- the ISP role


class FakePC:
    def __init__(self, tmp: Path, nics: dict[str, dict[str, Any]]) -> None:
        self.root = tmp / "net"
        self.ips: dict[str, str | None] = {}
        for name, n in nics.items():
            (self.root / name / "device").mkdir(parents=True)
            if n.get("wireless"):
                (self.root / name / "wireless").mkdir()
            (self.root / name / "operstate").write_text("up" if n.get("up", True) else "down")
            (self.root / name / "address").write_text(n.get("mac", "aa:bb:cc:00:00:01"))
            self.ips[name] = n.get("ip")
        self.calls: list[list[str]] = []
        self.events: list[tuple[str, str]] = []
        self.probe_answer: list[str] | None = []
        self.arp_answer = ProbeResult(True)  # nobody else uses 10.0.0.1
        self.arp_asked: list[tuple[str, str]] = []
        self.servers: list[Any] = []

    def run(self, argv: list[str], timeout_s: float) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[:3] == ["ip", "addr", "add"]:
            self.ips[argv[-1]] = argv[3].split("/")[0]
        if argv[:3] == ["ip", "addr", "del"]:
            self.ips[argv[-1]] = None
        return 0, ""

    def factory(self, *a: Any, **kw: Any) -> Any:
        pc = self

        class Srv:
            def __init__(self) -> None:
                self.args, self.kw, self.running = a, kw, False
                self.st: dict[str, Any] = {"lease": None, "requests": 0}
                pc.servers.append(self)

            def start(self) -> None:
                self.running = True

            def stop(self) -> None:
                self.running = False

            def status(self) -> dict[str, Any]:
                return self.st

        return Srv()

    def arp(self, ip: str, nic: str) -> ProbeResult:
        self.arp_asked.append((ip, nic))
        return self.arp_answer

    def isp(self, tmp: Path, **kw: Any) -> WanIsp:
        kw.setdefault("arp", self.arp)
        kw.setdefault("nm", False)  # the plain ``ip`` way unless a test asks for NM
        kw.setdefault("gateway", lambda nic: None)
        return WanIsp(tmp, lambda c, lv, text, data: self.events.append((c, text)), self.run,
                      self.root, self.ips.get, lambda nic, mac: self.probe_answer,
                      self.factory, **kw)  # fmt: skip


def test_role_on_picks_the_one_free_wired_nic_and_off_undoes_it(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {"mac": "aa:bb:cc:00:00:01"}, "enp2s0": {"ip": "192.168.1.5"},
                           "wlp3s0": {"wireless": True}})  # fmt: skip
    isp = pc.isp(tmp_path)
    st = isp.set(True)
    assert st["state"] == "ready" and st["nic"] == "enp1s0" and st["server_ip"] == "10.0.0.1"
    assert pc.calls == [["ip", "addr", "add", SERVER, "dev", "enp1s0"]]
    srv = pc.servers[0]
    assert srv.running and srv.args[3] == LEASE_IP
    assert srv.kw["ignore_macs"] == frozenset({"aa:bb:cc:00:00:01"})  # our own NIC
    assert srv.kw["alive"] is isp.alive  # GAP-112: the server can check the held address
    assert json.loads((tmp_path / "agent.json").read_text())["wan_isp"] == {"on": True, "nic": None}
    doc = json.loads((tmp_path / "wan-lease.json").read_text())
    assert doc["nic"] == "enp1s0" and doc["added_ip"] == SERVER
    isp.close()  # node shutdown: address stays, role stays on
    assert pc.ips["enp1s0"] == "10.0.0.1" and isp.config["on"]
    again = pc.isp(tmp_path)  # restarted node: our own address is not a reason to skip it
    assert again.set(True)["state"] == "ready" and len(pc.calls) == 1  # no second add
    off = again.set(False)
    assert off["state"] == "off" and pc.calls[-1] == ["ip", "addr", "del", SERVER, "dev", "enp1s0"]
    assert pc.ips["enp1s0"] is None and not pc.servers[-1].running
    assert json.loads((tmp_path / "wan-lease.json").read_text())["added_ip"] is None
    # receiverPC 2026-11-05: off forgets the lease too (it kept the Controller PC's MAC)
    doc = json.loads((tmp_path / "wan-lease.json").read_text())
    (tmp_path / "wan-lease.json").write_text(json.dumps(
        {**doc, "lease": {"mac": "02:00:00:00:00:aa", "ip": LEASE_IP}}))  # fmt: skip
    again.set(False)
    assert json.loads((tmp_path / "wan-lease.json").read_text())["lease"] is None


def test_two_candidates_wait_and_tell_once_then_a_set_nic_works(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {}, "enp2s0": {}})
    isp = pc.isp(tmp_path)
    st = isp.set(True)
    for _ in range(2):  # the background retries
        isp._bring_up()
    assert st["state"] == "waiting_nic" and "2 network cards" in st["why"]
    assert [c for c, _ in pc.events].count("ZL-DSC-006") == 1 and pc.calls == []
    assert isp.set(True, "enp2s0")["nic"] == "enp2s0"


def test_conflict_and_the_cable_moved_later(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {"ip": "192.168.1.9"}})  # still on the router LAN
    pc.probe_answer = ["10.0.0.254"]
    isp = pc.isp(tmp_path, retry_s=0.05)
    assert isp.set(True)["state"] == "waiting_nic"
    isp.start()
    pc.ips["enp1s0"] = None  # moved to the WAN port: the LAN address went away
    end = time.monotonic() + 3
    while isp.state != "conflict" and time.monotonic() < end:
        time.sleep(0.05)
    assert isp.state == "conflict" and ("ZL-SND-012" in [c for c, _ in pc.events])
    assert pc.calls == []  # nothing added next to another DHCP server
    pc.probe_answer = []
    isp.set(True)
    assert isp.state == "ready"
    isp.close()


def test_no_request_warns_once(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {}})
    isp = pc.isp(tmp_path, no_request_s=0.0)
    isp.set(True)
    isp._check_requests()
    isp._check_requests()
    assert [c for c, _ in pc.events].count("ZL-SND-011") == 1


@pytest.mark.parametrize("on", [1, "yes", None])
def test_node_route_checks_params(on: Any, tmp_path: Path) -> None:
    from core.node import Node
    from core.protocol.client import NodeClient
    from core.protocol.envelope import ZlError

    n = Node(tmp_path / "n", "agent", host="127.0.0.1", port=0, local_port=0)
    n.start()
    c = NodeClient("127.0.0.1", n.api.port, "b")
    try:
        assert c.get("/v1/wan")["state"] == "off"
        with pytest.raises(ZlError) as e:
            c.command("/v1/wan", 0, None, on=on)
        assert e.value.code == "ZL-DSC-012"
    finally:
        c.close()
        n.stop()


@pytest.mark.parametrize(("fails", "state"), [(1, "off"), (2, "error")])
def test_address_that_will_not_go_is_snd_014(tmp_path: Path, fails: int, state: str) -> None:
    """09 SND-014: taking 10.0.0.1 off fails -> one more try -> ``error`` + the command."""
    pc = FakePC(tmp_path, {"enp1s0": {"mac": "aa:bb:cc:00:00:01"}})
    isp = pc.isp(tmp_path)
    isp.undo_wait_s = 0.0
    isp.set(True)
    real, left = pc.run, {"n": fails}

    def flaky(argv: list[str], timeout_s: float) -> tuple[int, str]:
        if argv[:3] == ["ip", "addr", "del"] and left["n"]:
            left["n"] -= 1
            pc.calls.append(argv)
            return 2, "RTNETLINK answers: Operation not permitted"
        return real(argv, timeout_s)

    isp.run = flaky
    st = isp.set(False)
    assert st["state"] == state
    dels = [c for c in pc.calls if c[:3] == ["ip", "addr", "del"]]
    assert len(dels) == 2  # the first try and one more
    lease = json.loads((tmp_path / "wan-lease.json").read_text())
    if state == "error":
        ((_, text),) = [e for e in pc.events if e[0] == "ZL-SND-014"]
        assert "Operation not permitted" in text and lease["added_ip"] == SERVER  # kept
        assert "not permitted" in st["why"]
    else:
        assert not [e for e in pc.events if e[0] == "ZL-SND-014"] and lease["added_ip"] is None


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def auto_isp(pc: FakePC, tmp: Path, clock: Clock, **kw: Any) -> WanIsp:
    return pc.isp(tmp, settle_s=20.0, clock=clock, **kw)


def test_auto_starts_on_a_router_wan_port_after_settling(tmp_path: Path) -> None:
    """07 3.2 B automatic (GAP-75): nothing chosen, the one wired NIC stays up without an
    address and no DHCP server answers -> the role starts there by itself."""
    pc = FakePC(tmp_path, {"enp37s0": {}, "wlo1": {"wireless": True, "ip": "192.168.0.27"}})
    clock = Clock()
    isp = auto_isp(pc, tmp_path, clock)
    assert isp.status()["mode"] == "auto" and not isp.status()["on"]
    isp._auto_step()
    assert isp.state == "off" and pc.calls == []  # not settled yet: a LAN may still answer
    clock.t += 20
    isp._auto_step()
    assert isp.state == "ready" and isp.nic == "enp37s0"
    assert pc.calls == [["ip", "addr", "add", SERVER, "dev", "enp37s0"]]
    st = isp.status()
    assert st["on"] and st["mode"] == "auto"  # 06 10.4: this PC is on the router WAN side
    assert not (tmp_path / "agent.json").exists()  # nothing saved: still automatic
    assert "ZL-SND-022" not in [c for c, _ in pc.events]


def test_auto_leaves_a_router_lan_alone(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {}})
    pc.probe_answer = ["192.168.1.1"]  # the router LAN's own DHCP answers
    clock = Clock()
    isp = auto_isp(pc, tmp_path, clock)
    isp._auto_step()
    clock.t += 30
    for _ in range(3):
        isp._auto_step()
    assert isp.state == "off" and pc.calls == [] and pc.events == []  # silent, no ask


def test_auto_never_on_the_controller_nor_with_an_address(tmp_path: Path) -> None:
    cases: list[tuple[dict[str, dict[str, Any]], bool]] = [
        ({"enp1s0": {}}, False), ({"enp1s0": {"ip": "192.168.1.5"}}, True),
        ({"enp1s0": {"up": False}}, True),
    ]  # fmt: skip
    for nics, allowed in cases:
        d = tmp_path / f"{sorted(nics['enp1s0'])}{allowed}"
        pc = FakePC(d, nics)
        isp = auto_isp(pc, d, Clock(), auto_allowed=allowed)
        if allowed:  # the loop never calls _auto_step on the Controller
            isp._auto_step()
            isp.clock = lambda: 1e9
            isp._auto_step()
        assert isp.state == "off" and pc.calls == []
        assert isp.status()["mode"] == ("auto" if allowed else "off")


def test_auto_two_free_nics_ask_once(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {}, "enp2s0": {}})
    clock = Clock()
    isp = auto_isp(pc, tmp_path, clock)
    isp._auto_step()
    clock.t += 20
    isp._auto_step()
    isp._auto_step()
    assert isp.state == "waiting_nic" and pc.calls == []
    assert [c for c, _ in pc.events].count("ZL-DSC-006") == 1


def test_link_lost_pauses_and_a_lan_after_it_takes_our_address_off(tmp_path: Path) -> None:
    """The cable comes out of the WAN port: stop answering at once. Put in a router LAN
    port: a DHCP server answers there -> take 10.0.0.1 off, role off (never two servers)."""
    pc = FakePC(tmp_path, {"enp1s0": {}})
    clock = Clock()
    isp = auto_isp(pc, tmp_path, clock)
    isp._auto_step()
    clock.t += 20
    isp._auto_step()
    assert isp.state == "ready" and pc.servers[-1].running
    (pc.root / "enp1s0" / "operstate").write_text("down")
    isp._watch_link(auto=True)
    assert isp.state == "waiting_nic" and not pc.servers[-1].running
    assert "ZL-SND-022" not in [c for c, _ in pc.events]  # automatic: nothing to ask
    (pc.root / "enp1s0" / "operstate").write_text("up")
    pc.probe_answer = ["192.168.1.1"]
    isp._auto_step()
    clock.t += 20
    isp._auto_step()
    assert isp.state == "off" and pc.calls[-1] == ["ip", "addr", "del", SERVER, "dev", "enp1s0"]
    assert pc.ips["enp1s0"] is None and len(pc.servers) == 1


def test_link_lost_when_set_on_tells_to_connect(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {}})
    isp = pc.isp(tmp_path)
    isp.set(True)
    (pc.root / "enp1s0" / "operstate").write_text("down")
    isp._watch_link(auto=False)
    isp._bring_up()  # the background retry: no link -> waits, does not start a server
    assert isp.state == "waiting_nic" and len(pc.servers) == 1
    assert [c for c, _ in pc.events].count("ZL-SND-022") == 1


def test_back_to_auto_keeps_the_role_running(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {}})
    isp = pc.isp(tmp_path)
    isp.set(False)
    assert isp.status()["mode"] == "off"
    st = isp.set(None)
    assert st["mode"] == "auto"
    assert json.loads((tmp_path / "agent.json").read_text())["wan_isp"]["on"] is None


def test_auto_after_a_restart_comes_back_at_once(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {}})
    clock = Clock()
    isp = auto_isp(pc, tmp_path, clock)
    isp._auto_step()
    clock.t += 20
    isp._auto_step()
    isp.close()  # node restart: our address stays on the NIC
    again = auto_isp(pc, tmp_path, Clock())
    again._auto_step()  # no 20 s wait: the router WAN keeps reaching this PC
    assert again.state == "ready" and len(pc.calls) == 1  # no second add


def test_auto_two_pcs_claiming_at_once_back_off(tmp_path: Path) -> None:
    """Two PCs on one link without DHCP (a bridge-mode router, a switch) settle together:
    the ARP probe sees the other asking for 10.0.0.1 (RFC 5227) -> neither adds it, each
    waits a random while and looks again; the one that goes first is then seen."""
    pc = FakePC(tmp_path, {"enp1s0": {}})
    pc.arp_answer = ProbeResult(True, ["02:00:00:00:00:02"])  # the other PC probing too
    clock = Clock()
    isp = auto_isp(pc, tmp_path, clock, backoff=lambda: 7.0)
    isp._auto_step()
    clock.t += 20
    isp._auto_step()
    assert isp.state == "off" and pc.calls == [] and pc.arp_asked == [("10.0.0.1", "enp1s0")]
    pc.arp_answer = ProbeResult(True)  # the other one gave way
    clock.t += 20  # settle again, not yet past the random wait (20 + 7)
    isp._auto_step()
    assert pc.calls == []
    clock.t += 7
    isp._auto_step()
    assert isp.state == "ready" and pc.calls == [["ip", "addr", "add", SERVER, "dev", "enp1s0"]]


def test_auto_without_an_address_check_does_nothing(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {}})
    pc.arp_answer = ProbeResult(False, why="cannot open a packet socket: no rights")
    clock = Clock()
    isp = auto_isp(pc, tmp_path, clock)
    isp._auto_step()
    clock.t += 20
    isp._auto_step()
    assert isp.state == "off" and pc.calls == [] and "no rights" in isp.why


def test_auto_after_a_restart_gives_way_to_another_isp(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {}})
    clock = Clock()
    isp = auto_isp(pc, tmp_path, clock)
    isp._auto_step()
    clock.t += 20
    isp._auto_step()
    isp.close()  # node restart; meanwhile another PC took the link
    pc.arp_answer = ProbeResult(True, ["02:00:00:00:00:02"])
    again = auto_isp(pc, tmp_path, Clock())
    again._auto_step()
    assert again.state == "off" and pc.calls[-1] == ["ip", "addr", "del", SERVER, "dev", "enp1s0"]


def test_two_cards_ask_with_one_choice_per_card(tmp_path: Path) -> None:
    """20 W-1: DSC-006 carries the cards (name, link speed, MAC) so the Action Required
    can offer one button each; pressing one = set(True, nic)."""
    pc = FakePC(tmp_path, {"enp1s0": {"mac": "aa:bb:cc:00:00:01"},
                           "enp2s0": {"mac": "aa:bb:cc:00:00:02"}})  # fmt: skip
    (pc.root / "enp1s0" / "speed").write_text("1000")
    (pc.root / "enp2s0" / "speed").write_text("-1")  # unknown speed
    got: list[dict[str, Any]] = []
    isp = WanIsp(tmp_path, lambda c, lv, text, data: got.append({"code": c, **data}), pc.run,
                 pc.root, pc.ips.get, lambda nic, mac: [], pc.factory, arp=pc.arp)  # fmt: skip
    isp.set(True)
    (ask,) = [e for e in got if e["code"] == "ZL-DSC-006"]
    assert ask["choose"] == "wan_nic" and ask["choices"] == [
        {"id": "enp1s0", "label": "enp1s0 · 1000 Mbps · aa:bb:cc:00:00:01"},
        {"id": "enp2s0", "label": "enp2s0 · speed ? · aa:bb:cc:00:00:02"}]  # fmt: skip
    assert isp.set(True, "enp2s0")["state"] == "ready"


def test_moved_cable_keeps_an_old_lease_and_is_taken_over(tmp_path: Path) -> None:
    """20 GAP-88 (first real move): the only wired NIC kept 192.168.45.58 after the
    cable moved to the WAN port; its gateway no longer answers -> the role takes it,
    through a NetworkManager profile that replaces the old lease."""
    nics: dict[str, dict[str, Any]] = {
        "enp37s0": {"ip": "192.168.45.58"},
        "wlo1": {"wireless": True, "ip": "192.168.0.27"},
    }
    pc = FakePC(tmp_path, nics)
    asked: list[tuple[str, str, str]] = []

    def gw_alive(nic: str, ip: str, gw: str) -> bool:
        asked.append((nic, ip, gw))
        return False  # the router LAN is not on this cable any more

    def run(argv: list[str], timeout_s: float) -> tuple[int, str]:
        pc.calls.append(argv)
        if argv[:3] == ["nmcli", "connection", "up"]:
            pc.ips["enp37s0"] = "10.0.0.1"
        if argv[:3] == ["nmcli", "connection", "delete"]:
            pc.ips["enp37s0"] = None
        return 0, ""

    isp = pc.isp(tmp_path, nm=True, gateway=lambda nic: "192.168.45.1", alive=gw_alive)
    isp.run = run
    st = isp.set(True)
    assert st["state"] == "ready" and st["nic"] == "enp37s0", st
    assert asked == [("enp37s0", "192.168.45.58", "192.168.45.1")]
    add = next(c for c in pc.calls if c[:3] == ["nmcli", "connection", "add"])
    assert "zeroloss-wan" in add and "10.0.0.1/24" in add and "ipv4.never-default" in add
    assert any("the cable moved to the WAN port" in text for _, text in pc.events)
    doc = json.loads((tmp_path / "wan-lease.json").read_text())
    assert doc["nm_profile"] == "zeroloss-wan"
    off = isp.set(False)  # the NIC's own profile comes back by itself
    assert off["state"] == "off" and pc.calls[-1] == ["nmcli", "connection", "delete",
                                                      "zeroloss-wan"]  # fmt: skip


@pytest.mark.parametrize("answer", [True, None])
def test_a_live_lan_is_never_taken(tmp_path: Path, answer: bool | None) -> None:
    """Gateway answers (really on a LAN) or cannot be asked: keep waiting (SND-022)."""
    pc = FakePC(tmp_path, {"enp37s0": {"ip": "192.168.45.58"}})
    isp = pc.isp(tmp_path, nm=True, gateway=lambda nic: "192.168.45.1",
                 alive=lambda nic, ip, gw: answer)  # fmt: skip
    assert isp.set(True)["state"] == "waiting_nic" and pc.calls == []


def test_two_wired_nics_are_never_guessed(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {"ip": "192.168.45.58"}, "enp2s0": {"ip": "192.168.1.5"}})
    isp = pc.isp(tmp_path, nm=True, gateway=lambda nic: "192.168.45.1",
                 alive=lambda nic, ip, gw: False)  # fmt: skip
    assert isp.set(True)["state"] == "waiting_nic"


def test_a_record_without_the_address_adds_it_again_after_a_restart(tmp_path: Path) -> None:
    """GAP-98 (first real run): the lease record says added, the NIC has nothing (another
    program took it off). The restarted node must not trust the record."""
    pc = FakePC(tmp_path, {"enp1s0": {}})
    clock = Clock()
    isp = auto_isp(pc, tmp_path, clock)
    isp._auto_step()
    clock.t += 20
    isp._auto_step()
    assert isp.state == "ready" and pc.ips["enp1s0"] == "10.0.0.1"
    isp.close()
    pc.ips["enp1s0"] = None  # NetworkManager's own profile flushed it
    again = auto_isp(pc, tmp_path, Clock())
    again._auto_step()
    assert again.state == "ready" and pc.ips["enp1s0"] == "10.0.0.1"
    assert [c[:3] for c in pc.calls].count(["ip", "addr", "add"]) == 2


def test_an_address_taken_off_while_ready_comes_back_then_the_role_stops(tmp_path: Path) -> None:
    """09 SND-023: put back and warn; more than three times in 10 min = stop, tell how
    to stop the other program, and no endless add/remove fight."""
    pc = FakePC(tmp_path, {"enp1s0": {}})
    clock = Clock()
    isp = auto_isp(pc, tmp_path, clock)
    isp._auto_step()
    clock.t += 20
    isp._auto_step()
    for n in range(3):
        pc.ips["enp1s0"] = None
        clock.t += 60
        isp._watch_address()
        assert isp.state == "ready" and pc.ips["enp1s0"] == "10.0.0.1", n
    warns = [e for e in pc.events if e[0] == "ZL-SND-023"]
    assert len(warns) == 3 and "added again" in warns[0][1]
    pc.ips["enp1s0"] = None
    clock.t += 60
    isp._watch_address()
    assert isp.state == "error" and isp._fighting and not pc.servers[-1].running
    assert "keeps taking" in pc.events[-1][1]
    adds = [c[:3] for c in pc.calls].count(["ip", "addr", "add"])
    isp._auto_step() if not isp._fighting else None  # the loop skips steps while fighting
    assert [c[:3] for c in pc.calls].count(["ip", "addr", "add"]) == adds
    isp.set(True)  # the user changed something and turns it on: try again
    assert isp.state == "ready" and pc.ips["enp1s0"] == "10.0.0.1"
    isp.close()


def test_losses_spread_over_more_than_10_minutes_only_warn(tmp_path: Path) -> None:
    pc = FakePC(tmp_path, {"enp1s0": {}})
    clock = Clock()
    isp = auto_isp(pc, tmp_path, clock)
    isp._auto_step()
    clock.t += 20
    isp._auto_step()
    for _ in range(6):
        pc.ips["enp1s0"] = None
        clock.t += 301
        isp._watch_address()
    assert isp.state == "ready" and not isp._fighting
    isp.close()
