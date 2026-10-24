"""Leave watch (docs/03-features.md F19, 09 NET-005): old group after a Leave."""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from core.blocks.leave_watch import LeaveWatch, frame_is

GROUP = "239.255.78.1"
PORT = 46000
NS = 1_000_000_000


def frame(group: str = GROUP, port: int = PORT, proto: int = 17, frag: int = 0) -> bytes:
    udp = struct.pack("!HHHH", 5000, port, 8, 0)
    ip = struct.pack(
        "!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, frag, 8, proto, 0,
        socket.inet_aton("10.0.0.1"), socket.inet_aton(group),
    )  # fmt: skip
    return b"\x01\x00\x5e\x7f\x4e\x01" + b"\x02" * 6 + b"\x08\x00" + ip + udp


class Fake:
    """Frames at given times; the clock jumps to each frame so nothing really waits."""

    def __init__(self, items: list[tuple[bytes, int]], clock: list[int]) -> None:
        self.items = list(items)
        self.clock = clock
        self.closed = False

    def read(self, timeout_s: float) -> tuple[bytes, int] | None:
        if self.items:
            data, t = self.items.pop(0)
            self.clock[0] = max(self.clock[0], t)
            return data, t
        self.clock[0] += int(timeout_s * NS)
        return None

    def close(self) -> None:
        self.closed = True


def watch_with(items: list[tuple[bytes, int]]) -> tuple[str, Fake]:
    clock = [0]
    fake = Fake(items, clock)
    w = LeaveWatch(GROUP, PORT, "eth0", opener=lambda _nic: fake)
    assert w.open()
    res = w.watch(10 * NS, threading.Event(), clock=lambda: clock[0])
    return res.state, fake


def test_no_old_packets_is_ok() -> None:
    state, fake = watch_with([(frame("239.255.78.2"), 11 * NS)])
    assert state == "ok" and fake.closed


def test_packets_within_three_seconds_are_still_ok() -> None:
    state, _ = watch_with([(frame(), 11 * NS), (frame(), 12 * NS + NS // 2)])
    assert state == "ok"


def test_packet_after_three_seconds_is_not_processed() -> None:
    state, _ = watch_with([(frame(), 11 * NS), (frame(), 13 * NS + 1)])
    assert state == "not_processed"


def test_packets_before_the_leave_are_ignored() -> None:
    state, _ = watch_with([(frame(), 9 * NS)])
    assert state == "ok"


def test_no_permission_is_not_checked_never_a_fault() -> None:
    def deny(_nic: str) -> Fake:
        raise PermissionError

    w = LeaveWatch(GROUP, PORT, "eth0", opener=deny)
    assert not w.open()
    res = w.watch(0, threading.Event())
    assert res.state == "not_checked" and "CAP_NET_RAW" in res.reason


def test_unknown_nic_is_not_checked() -> None:
    w = LeaveWatch(GROUP, PORT, None)
    assert not w.open() and w.watch(0, threading.Event()).state == "not_checked"


def test_stop_ends_the_watch_early_and_closes_the_view() -> None:
    clock = [0]
    fake = Fake([], clock)
    w = LeaveWatch(GROUP, PORT, "eth0", opener=lambda _nic: fake)
    w.open()
    stop = threading.Event()
    stop.set()
    assert w.watch(0, stop, clock=lambda: clock[0]).state == "not_checked"
    assert fake.closed


@pytest.mark.parametrize(
    "kw",
    [{"group": "239.1.1.1"}, {"port": PORT + 1}, {"proto": 6}, {"frag": 100}],
)
def test_frame_filter_rejects_others(kw: dict[str, object]) -> None:
    assert frame_is(frame(), GROUP, PORT)
    assert not frame_is(frame(**kw), GROUP, PORT)  # type: ignore[arg-type]


def test_short_and_non_ip_frames_are_rejected() -> None:
    assert not frame_is(b"", GROUP, PORT)
    assert not frame_is(b"\0" * 60, GROUP, PORT)
