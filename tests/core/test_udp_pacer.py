"""GAP-160: a stream that arrives in bursts leaves with even spacing, byte for byte.

2026-10-16: the sender's playout left 30~33 datagrams back to back every ~9 ms; on every
router a moment of business every 125 s lost the tail of a burst. A carrier sends one
datagram every 1316 x 8 / rate seconds.
"""

from __future__ import annotations

import itertools
import socket
import statistics
import subprocess
import threading
import time

from core.blocks import udp_pacer


def test_spacing_follows_the_bytes_and_catches_up() -> None:
    assert abs(udp_pacer.spacing_s(1328, 35e6, 0) - 1328 * 8 / 35e6) < 1e-12
    assert udp_pacer.spacing_s(1328, 35e6, 100) < udp_pacer.spacing_s(1328, 35e6, 0)


def test_the_installed_program_runs_it_as_its_own_process(monkeypatch: object) -> None:
    import sys

    argv = udp_pacer.command(5998, "239.1.1.1", 5004, 35_000_000, 16, "10.0.0.1", 40000)
    assert argv[1:3] == ["-m", "core.blocks.udp_pacer"]  # a source checkout
    assert argv[3:] == [
        *("--listen", "5998", "--to", "239.1.1.1:5004", "--rate", "35000000", "--ttl", "16"),
        *("--local-address", "10.0.0.1", "--local-port", "40000"),
    ]
    setattr(sys, "frozen", True)  # noqa: B010 (PyInstaller's flag)
    try:
        assert udp_pacer.command(1, "a", 2, 3, 4)[:2] == [sys.executable, "pace"]
    finally:
        delattr(sys, "frozen")


def test_bursts_in_even_spacing_out_and_nothing_changed() -> None:
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.bind(("127.0.0.1", 0))
    out.settimeout(5)
    out.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    listen = udp_pacer.free_loopback_port()
    rate = 10_000_000  # 1 ms per 1328-byte datagram: easy to see on any OS
    argv = udp_pacer.command(listen, "127.0.0.1", out.getsockname()[1], rate, 1)
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    try:
        assert '"listening"' in (p.stdout.readline() if p.stdout else "")
        got: list[bytes] = []
        times: list[float] = []

        def take() -> None:  # timestamps as they arrive, not after
            for _ in range(60):
                got.append(out.recv(2048))
                times.append(time.perf_counter())

        t = threading.Thread(target=take)
        t.start()
        src = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sent = [i.to_bytes(4, "big") + bytes(1324) for i in range(60)]
        for chunk in (sent[:30], sent[30:]):  # two bursts, back to back
            for d in chunk:
                src.sendto(d, ("127.0.0.1", listen))
            time.sleep(0.03)
        t.join(10)
    finally:
        p.terminate()
        p.wait(5)
    assert got == sent  # byte for byte, in order
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert statistics.median(gaps) > 0.0008  # spread out, not 30 at once
    assert sum(1 for g in gaps if g < 0.0002) < 10


def test_a_datagram_too_late_for_live_is_dropped_not_sent_late() -> None:
    """2026-10-23 user: "실제 iptv는 실시간 방송이라 보낼때 늦게보내고 천천히 보내고 이런걸
    하지 않아" -- a stalled relay must not keep a backlog and send it later."""
    from collections import deque

    from core.blocks.udp_pacer import LIVE_LATE_S, Pacer

    t = [0.0]
    sent: list[bytes] = []

    class Sock:
        def sendto(self, data: bytes, dest: tuple[str, int]) -> None:
            sent.append(data)

        def setblocking(self, flag: bool) -> None:
            pass

    p = Pacer(Sock(), Sock(), ("239.1.1.1", 5000), 35e6,  # type: ignore[arg-type]
              clock=lambda: t[0], sleep=lambda s: None)  # fmt: skip
    p.queue = deque([(0.0, b"old"), (LIVE_LATE_S + 0.05, b"fresh")])
    p._drain = lambda: None  # type: ignore[method-assign]
    t[0] = LIVE_LATE_S + 0.06  # the relay was held up: "old" waited too long
    rounds = iter([False, False, True])
    reports: list[dict[str, float]] = []
    p.run(lambda: next(rounds), reports.append)
    assert sent == [b"fresh"] and (p.dropped == 1 or reports[-1]["dropped"] == 1)
