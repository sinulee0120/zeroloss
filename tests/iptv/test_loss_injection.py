"""Trouble put in on purpose between a real sender and a real receiver is counted
exactly, and the measuring comes back after it (2026-11-21 user: "임의로 패킷 로스 내보면서도
테스트해", "오류패킷도 보내보고", "보내다 멈출경우도 어떻게 회복이 되는지 다시 시작하는지도
보고"). The product's own parts: the sender session (tsp and the even-spacing sender),
the receive socket joined on the group, the analyzer. A relay in between drops, swaps,
doubles, holds and damages datagrams at known places once the receiver measures, or
goes silent for a while. Runs on Linux and on Windows (05 2.1, 10 1.1)."""

from __future__ import annotations

import random
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.blocks.udp_receiver import UdpReceiver, clock_ns
from core.events.bus import EventBus
from modules.iptv.receiver.analyzer import StreamAnalyzer
from modules.iptv.sender.commands import QUALITIES, prepare_command
from modules.iptv.sender.session import SenderConfig, SenderSession

HAVE_TOOLS = bool(shutil.which("tsp") and shutil.which("ffmpeg"))
live = pytest.mark.skipif(not HAVE_TOOLS, reason="needs ffmpeg and tsp")
IN_PORT = 47311  # the sender sends here (unicast, loopback)
GROUP, OUT_PORT = "239.255.97.3", 5032  # the relay sends the stream on to the group
RTP = 12  # tsp's RTP header: no CSRC, no extension

# Each plan gets ``i`` (datagram number since ``arm``, None before) and the datagram, and
# returns what to send now: [] drops it, [d, d] doubles it, None = the plan keeps it to
# send later itself (not dropped).
Plan = Callable[[int | None, bytes], list[bytes] | None]


class Relay:
    """Takes the sender's datagrams and sends them on to the group, through ``plan``."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.rx.bind(("127.0.0.1", IN_PORT))
        self.rx.settimeout(0.1)
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 1))
        self.tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        self.tx.bind(("127.0.0.1", 0))  # out on loopback (Windows: not IP_MULTICAST_IF)
        self.armed_at: int | None = None
        self.n = 0  # datagrams taken so far
        self.dropped = 0
        self.later: list[tuple[float, bytes]] = []  # (when, datagram) held back
        self.halt = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def arm(self) -> None:
        self.armed_at = self.n

    def since_arm(self) -> int:
        return 0 if self.armed_at is None else self.n - self.armed_at

    def _send(self, data: bytes) -> None:
        self.tx.sendto(data, (GROUP, OUT_PORT))

    def _run(self) -> None:
        while not self.halt.is_set():
            now = time.monotonic()
            for item in [x for x in self.later if x[0] <= now]:
                self.later.remove(item)
                self._send(item[1])
            try:
                data = self.rx.recv(65535)
            except (TimeoutError, OSError):
                continue
            self.n += 1
            out = self.plan(None if self.armed_at is None else self.n - self.armed_at, data)
            if out is None:
                continue  # held by the plan
            if not out:
                self.dropped += 1
            for d in out:
                self._send(d)

    def close(self) -> None:
        self.halt.set()
        self.t.join(3)
        self.rx.close()
        self.tx.close()


class Receiver:
    """The receive socket on the group (as the receiver PC) and the analyzer."""

    def __init__(self) -> None:
        self.sock = UdpReceiver(OUT_PORT, GROUP, "127.0.0.1", rcvbuf_bytes=8 << 20)
        self.a = StreamAnalyzer(1, clock_ns(), playback=False)
        self.records: list[dict[str, Any]] = []
        self.halt = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self) -> None:
        nxt = time.monotonic() + 1
        while not self.halt.is_set():
            p = self.sock.receive(0.05)
            if p:
                self.a.on_datagram(p.data, p.arrival_ns)
            if time.monotonic() >= nxt:
                nxt += 1
                rec = self.a.second(clock_ns())
                if rec:
                    self.records.append(rec)

    def close(self) -> None:
        self.halt.set()
        self.t.join(3)
        last = self.a.finish(clock_ns())
        if last:
            self.records.append(last)
        self.sock.close()

    def total(self, *keys: str) -> dict[str, int]:
        return {key: sum(int(r.get(key) or 0) for r in self.records) for key in keys}


@pytest.fixture(scope="module")
def hd(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not HAVE_TOOLS:
        pytest.skip("needs ffmpeg and tsp")
    out = tmp_path_factory.mktemp("inject") / "ch1.ts"
    subprocess.run(prepare_command("ffmpeg", QUALITIES["HD"], 1, out, seconds=4,
                                   preset="ultrafast"),
                   check=True, capture_output=True, timeout=300)  # fmt: skip
    return out


def _sender(hd: Path, tmp: Path) -> SenderSession:
    return SenderSession(SenderConfig(1, hd, "127.0.0.1", IN_PORT, 10_000_000, ttl=1),
                         tmp, EventBus())  # fmt: skip


def _measuring(rx: Receiver, within_s: float = 20.0) -> None:
    end = time.monotonic() + within_s
    while not rx.a.measuring and time.monotonic() < end:
        time.sleep(0.2)
    assert rx.a.measuring, "the receiver never finished warm-up"


def _until(relay: Relay, n: int, within_s: float = 30.0) -> None:
    end = time.monotonic() + within_s
    while relay.since_arm() < n and time.monotonic() < end:
        time.sleep(0.2)
    assert relay.since_arm() >= n, f"only {relay.since_arm()} of {n} datagrams came"


# ---------------------------------------------------------------- loss


SINGLE = 300
BURST_5 = range(700, 705)
SWAP = (1100, 1101)
DOUBLE = 1500
BURST_50 = range(1900, 1950)
LATE = 2300  # held past the 1,000 ms reorder window: lost, then late
SCATTER = sorted(random.Random(7).sample(range(2600, 7000, 3), 20))  # never neighbours


@live
def test_loss_put_in_on_purpose_is_counted_exactly(hd: Path, tmp_path: Path) -> None:
    drop = {SINGLE, *BURST_5, *BURST_50, *SCATTER}
    held: dict[str, bytes] = {}
    relay: Relay

    def plan(i: int | None, d: bytes) -> list[bytes] | None:
        if i in drop:
            return []
        if i == SWAP[0]:
            held["swap"] = d  # sent right after the next one
            return None
        if i == SWAP[1]:
            return [d, held.pop("swap")]
        if i == LATE:
            relay.later.append((time.monotonic() + 1.6, d))
            return None
        if i == DOUBLE:
            return [d, d]
        return [d]

    relay, rx = Relay(plan), Receiver()
    s = _sender(hd, tmp_path)
    try:
        s.start()
        _measuring(rx)
        time.sleep(1.0)
        relay.arm()
        _until(relay, SCATTER[-1] + 2000)  # past the last place and its reorder window
        time.sleep(2.0)
    finally:
        s.stop()
        relay.close()
        rx.close()
    planned = 1 + len(BURST_5) + len(BURST_50) + len(SCATTER)
    assert relay.dropped == planned
    t = rx.total("lost", "dup", "reo", "late", "lev")
    assert t["lost"] == planned + 1, t  # the held one too: its window ended first
    assert t["late"] == 1, t  # ... and then it came
    assert t["dup"] == 1, t
    assert t["reo"] == 1, t  # the swapped pair: in order again, not lost
    assert t["lev"] == 1 + 1 + 1 + len(SCATTER) + 1, t  # separate loss events


# ---------------------------------------------------------------- damaged datagrams

BAD_SYNC = 400  # the first TS packet's sync byte is not 0x47
BAD_TEI = 800  # transport error indicator set
GARBAGE = 1200  # a datagram that is not RTP at all, sent next to the stream


def _ts_byte(d: bytes, offset: int, value: int) -> bytes:
    b = bytearray(d)
    b[RTP + offset] = value
    return bytes(b)


def _tei_on_a_real_packet(d: bytes) -> bytes:
    """TEI set on the datagram's first TS packet that is not stuffing (a null packet,
    PID 0x1FFF, is skipped by the analyzer as TR 101 290 says)."""
    b = bytearray(d)
    for off in range(RTP, len(b) - 187, 188):
        if ((b[off + 1] & 0x1F) << 8 | b[off + 2]) != 0x1FFF:
            b[off + 1] |= 0x80
            return bytes(b)
    raise AssertionError("a datagram of stuffing only")


@live
def test_damaged_datagrams_are_errors_never_loss(hd: Path, tmp_path: Path) -> None:
    def plan(i: int | None, d: bytes) -> list[bytes]:
        if i == BAD_SYNC:
            return [_ts_byte(d, 0, 0x46)]
        if i == BAD_TEI:
            return [_tei_on_a_real_packet(d)]
        if i == GARBAGE:
            return [d, b"\x00" * 200]
        return [d]

    relay, rx = Relay(plan), Receiver()
    s = _sender(hd, tmp_path)
    try:
        s.start()
        _measuring(rx)
        time.sleep(1.0)
        relay.arm()
        _until(relay, GARBAGE + 1500)
        time.sleep(1.5)
    finally:
        s.stop()
        relay.close()
        rx.close()
    t = rx.total("lost", "dup", "sbe", "tei", "p1")
    assert t["lost"] == 0 and t["dup"] == 0, t  # every datagram came: none is lost
    assert t["sbe"] >= 1 and t["p1"] >= 1, t  # TR 101 290 1.2 Sync_byte, priority 1
    assert t["tei"] >= 1, t  # 2.1, counted only
    assert rx.a.not_rtp == 1  # counted, not silently dropped (05 3.2)


# ---------------------------------------------------------------- the stream stops

GAP_FROM, GAP_N = 500, 2850  # about 3 s of a 10 Mbps stream: nothing comes


@live
def test_a_silent_network_is_counted_and_the_measuring_comes_back(hd: Path, tmp_path: Path) -> None:
    def plan(i: int | None, d: bytes) -> list[bytes]:
        return [] if i is not None and GAP_FROM <= i < GAP_FROM + GAP_N else [d]

    relay, rx = Relay(plan), Receiver()
    s = _sender(hd, tmp_path)
    try:
        s.start()
        _measuring(rx)
        time.sleep(1.0)
        relay.arm()
        _until(relay, GAP_FROM + GAP_N + 3000)  # 3 s of stream again after the gap
        time.sleep(1.5)
    finally:
        s.stop()
        relay.close()
        rx.close()
    assert relay.dropped == GAP_N
    t = rx.total("lost", "p1", "cc", "pate", "pmte", "pide")
    assert t["lost"] == GAP_N, t  # exactly what did not come
    silent = [r for r in rx.records if r["rx"] == 0]
    assert len(silent) >= 2  # the seconds without a packet say so (rx 0)
    after = rx.records[-4:-1]  # the last one is the cut-off second of the stop
    assert all(r["rx"] > 500 and r["lost"] == 0 for r in after), after  # measuring again
    # the lost datagrams break each stream's continuity counter once (real, with the
    # loss); the gap itself never reads as a missing table (resync, 05 2.1-2)
    assert t["pate"] == t["pmte"] == t["pide"] == 0, t
    assert 1 <= t["cc"] <= 8 and t["p1"] == t["cc"], t


@live
def test_a_sender_that_stops_and_starts_again_is_a_new_segment_not_loss(
    hd: Path, tmp_path: Path
) -> None:
    relay, rx = Relay(lambda i, d: [d]), Receiver()
    s = _sender(hd, tmp_path / "a")
    try:
        s.start()
        _measuring(rx)
        time.sleep(2.0)
        s.stop()  # the sender stops (a new tsp, a new SSRC when it comes back)
        time.sleep(3.0)
        s = _sender(hd, tmp_path / "b")
        s.start()
        time.sleep(4.0)
    finally:
        s.stop()
        relay.close()
        rx.close()
    t = rx.total("lost")
    assert t["lost"] == 0, t  # a restarted sender is a new segment (RFC 3550), not loss
    assert rx.a.seq.stats.segment_restarts == 1
    assert any(r["rx"] == 0 for r in rx.records)  # the stopped seconds: nothing came
    assert all(r["rx"] > 500 for r in rx.records[-3:-1]), rx.records[-4:]  # measuring again
