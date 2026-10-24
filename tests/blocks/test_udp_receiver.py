"""UDP receiver on local multicast (docs/05-standards.md 4.1-4.4, P1 V2 in small).

Test packets use TTL 0 so they never leave this PC.
"""

from __future__ import annotations

import itertools
import socket
import sys
import threading
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.blocks.udp_receiver import UdpReceiver

LINUX = sys.platform.startswith("linux")
G1, G2 = "239.255.77.1", "239.255.77.2"
_ports = itertools.count(45000)


def sender() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    return s


def send(group: str, port: int, n: int, size: int = 1328, pps: float = 0) -> None:
    """Send n numbered packets; ``pps`` > 0 paces them like a real stream."""
    start = time.monotonic()
    with sender() as s:
        for i in range(n):
            if pps:
                while time.monotonic() - start < i / pps:
                    pass
            s.sendto(i.to_bytes(4, "big") + bytes(size - 4), (group, port))


def drain(r: UdpReceiver, idle_s: float = 0.2) -> list[int]:
    got = []
    while (p := r.receive(idle_s)) is not None:
        got.append(int.from_bytes(p.data[:4], "big"))
    return got


def multicast_works() -> bool:
    port = next(_ports)
    try:
        with UdpReceiver(port, G1, rcvbuf_bytes=1 << 20) as r:
            send(G1, port, 1)
            return r.receive(1.0) is not None
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not multicast_works(), reason="no multicast route on this host")


def test_two_sockets_on_one_port_each_get_every_packet() -> None:
    port = next(_ports)
    with UdpReceiver(port, G1) as analyzer, UdpReceiver(port, G1) as player:
        got: dict[str, list[int]] = {}
        readers = [
            threading.Thread(target=lambda n=n, r=r: got.__setitem__(n, drain(r, 0.5)))
            for n, r in (("analyzer", analyzer), ("player", player))
        ]
        for t in readers:
            t.start()
        send(G1, port, 1000, pps=2000)
        for t in readers:
            t.join()
        # Each socket gets its own copy: every packet is either received or this
        # socket's own counted drop (a slow reader must never go unnoticed).
        for name, r in (("analyzer", analyzer), ("player", player)):
            assert got[name] == sorted(set(got[name]))
            assert len(got[name]) + (r.own_drops() or 0) == 1000


def test_socket_only_gets_its_own_group() -> None:
    port = next(_ports)
    with UdpReceiver(port, G1) as a, UdpReceiver(port, G2) as b:
        send(G1, port, 10)
        send(G2, port, 7)
        assert len(drain(a)) == 10
        assert len(drain(b)) == 7


def test_channel_change_leave_then_join() -> None:
    port = next(_ports)
    with UdpReceiver(port, G1) as r:
        r.leave()
        r.join(G2)
        send(G1, port, 5)
        send(G2, port, 6)
        assert len(drain(r)) == 6
        assert r.group == G2


def test_receive_buffer_applied_value_is_reported() -> None:
    port = next(_ports)
    with UdpReceiver(port, G1, rcvbuf_bytes=64 * 1024) as small:
        assert small.rcvbuf_applied >= 64 * 1024
        assert not small.rcvbuf_short
    with UdpReceiver(port, G1) as big:
        assert big.rcvbuf_short == (big.rcvbuf_applied < 16 * 1024 * 1024)


@pytest.mark.skipif(not LINUX, reason="kernel timestamps and drop counter are Linux-only")
def test_kernel_time_and_own_drops_add_up() -> None:
    port = next(_ports)
    with UdpReceiver(port, G1, rcvbuf_bytes=8 * 1024) as r:
        before = time.time_ns()
        send(G1, port, 2000)  # not reading: the small buffer overflows
        got: list[int] = []
        last_drops = 0
        while (p := r.receive(0.2)) is not None:
            got.append(int.from_bytes(p.data[:4], "big"))
            assert before - 10**9 < p.arrival_ns < time.time_ns() + 10**9
            assert p.drops_total is not None
            last_drops = p.drops_total
        assert r.kernel_timestamps
        drops = r.own_drops()
        assert drops is not None and drops > 0
        # Loopback does not lose packets: all missing ones are this socket's own drops.
        assert len(got) + drops == 2000
        assert got == sorted(got)
        assert last_drops <= drops
        send(G1, port, 1)  # the next queued packet carries the full count
        p = r.receive(1.0)
        assert p is not None and p.drops_total == drops


def test_closed_receiver_rejects_use_and_close_is_repeatable() -> None:
    r = UdpReceiver(next(_ports), G1)
    with pytest.raises(RuntimeError):
        r.join(G2)  # already joined
    r.close()
    r.close()
    assert not r.is_open
    with pytest.raises(RuntimeError):
        r.receive(0.01)


@settings(max_examples=25, deadline=None)
@given(
    ops=st.lists(
        st.one_of(
            st.tuples(st.just("join"), st.sampled_from([G1, G2])),
            st.tuples(st.just("leave"), st.just("")),
            st.tuples(st.just("send"), st.sampled_from([G1, G2])),
        ),
        max_size=12,
    )
)
def test_random_join_leave_send_matches_membership(ops: list[tuple[str, str]]) -> None:
    """Invariant: a socket gets exactly the packets sent to the group it is in at that time."""
    port = next(_ports)
    with UdpReceiver(port, rcvbuf_bytes=1 << 20) as r:
        for op, group in ops:
            if op == "join":
                if r.group is not None:
                    r.leave()
                r.join(group)
            elif op == "leave":
                r.leave()
            else:
                send(group, port, 3)
                assert len(drain(r, 0.05)) == (3 if r.group == group else 0)


def _default_card_ip() -> str | None:
    """This PC's address on its default route (no packet is sent by connect)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            ip = str(s.getsockname()[0])
        except OSError:
            return None
    return None if ip.startswith("127.") else ip


def _send_from(card_ip: str, group: str, port: int) -> None:
    with sender() as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(card_ip))
        s.sendto(b"x" * 188, (group, port))


@pytest.mark.skipif(not LINUX, reason="IP_PKTINFO arrival card is Linux only")
def test_join_on_every_card_and_tell_which_card_a_packet_came_in_on() -> None:
    """02 4 GAP-115: the receiving card is found by where the packets arrive, not by an
    address guessed from the registration (a home Wi-Fi address chose the wrong card)."""
    other = _default_card_ip()
    cards = ["127.0.0.1", *([other] if other else [])]
    port = next(_ports)
    with UdpReceiver(port, G1, join_on=cards, arrival_card=True) as r:
        for ip in cards:
            _send_from(ip, G1, port)
            p = r.receive(1.0)
            assert p is not None and p.card_ip == ip, (ip, p)
            assert p.card_index is not None and socket.if_indextoname(p.card_index) != ""
        assert drain(r) == []
    lo = socket.if_nametoindex("lo")
    with UdpReceiver(port, G1, join_on=["127.0.0.1"], arrival_card=True) as r:
        _send_from("127.0.0.1", G1, port)
        p = r.receive(1.0)
        assert p is not None and (p.card_index, p.card_ip) == (lo, "127.0.0.1")
    with UdpReceiver(port, G1, join_on=["127.0.0.1"]) as r:  # not asked: nothing extra
        _send_from("127.0.0.1", G1, port)
        p = r.receive(1.0)
        assert p is not None and p.card_index is None and p.card_ip is None
