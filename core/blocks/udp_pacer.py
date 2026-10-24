"""Even datagram spacing for a stream that arrives in bursts (20 GAP-160).

2026-10-16: the stream left the sender as 30~33 datagrams back to back every ~9 ms
(the playout program's own pacing granularity on Linux, measured with every option it
has); a router busy for a moment lost the tail of such a burst, every 125 s, on every
router. A carrier's playout sends one datagram every 1316 x 8 / rate seconds (0.30 ms
at 35 Mbps). This relay takes the datagrams on a loopback port and sends each one,
byte for byte, to the real destination on that even schedule.

- The rate comes from the bytes: each datagram waits ``len x 8 / rate`` after the one
  before. The source keeps the long-term rate; a queue that grows beyond
  ``CATCH_UP_QUEUE`` makes the spacing slightly shorter so the relay never falls behind.
- Waiting: sleep until ``SPIN_S`` before the time, then spin (sleep alone is too coarse).
- Live, like a broadcast (2026-10-23 user: "실제 iptv는 실시간 방송이라 보낼때 늦게보내고
  천천히 보내고 이런걸 하지 않아"): a datagram that has waited here longer than
  ``LIVE_LATE_S`` is dropped, not sent late. 2026-10-22 a Wi-Fi driver reset storm starved
  this relay for 20 s; it kept 15.9 k datagrams and sent them 1.1 % fast for 7 minutes, so
  the stream reached the receiver 4.8 s late -- a playout never does that.
- One line of JSON a second on stdout: datagrams sent, dropped as too late, the deepest
  queue, the latest send behind schedule (for the run record; the sender's rate report
  stays the source's).

Run as its own process (``zeroloss pace …``) so nothing else in the node delays a send.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import socket
import struct
import sys
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from core.blocks import priority

SPIN_S = 0.0001  # measured on the T460: p99 0.41 ms, 29 % of a core (0.0002: 0.32 ms, 57 %)
CATCH_UP_QUEUE = 64  # datagrams waiting: send a little faster until it drains
CATCH_UP = 0.98  # spacing factor while catching up
LIVE_LATE_S = 0.1  # waited longer than this: too late for live, dropped (usual wait ~25 ms)
REPORT_S = 1.0
RCVBUF = 8 << 20


def spacing_s(size: int, rate_bps: float, queued: int) -> float:
    gap = size * 8 / rate_bps
    return gap * CATCH_UP if queued > CATCH_UP_QUEUE else gap


class Pacer:
    def __init__(
        self,
        listen: socket.socket,
        send: socket.socket,
        dest: tuple[str, int],
        rate_bps: float,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.listen, self.send, self.dest, self.rate = listen, send, dest, rate_bps
        self.clock, self.sleep = clock, sleep
        self.queue: deque[tuple[float, bytes]] = deque()  # (arrived, datagram)
        self.sent = 0
        self.dropped = 0  # too late for live, this second
        self.max_queue = 0
        self.late_s = 0.0

    def _drain(self) -> None:
        while True:
            try:
                self.queue.append((self.clock(), self.listen.recv(65535)))
            except (BlockingIOError, InterruptedError):
                return

    def run(self, until: Callable[[], bool], report: Callable[[dict[str, float]], None]) -> None:
        self.listen.setblocking(False)
        due = self.clock()
        next_report = due + REPORT_S
        while not until():
            self._drain()
            if not self.queue:
                self.sleep(SPIN_S / 2)
                due = max(due, self.clock())  # an empty queue does not bank time
                continue
            self.max_queue = max(self.max_queue, len(self.queue))
            wait = due - self.clock()
            if wait > SPIN_S:
                self.sleep(wait - SPIN_S)
            while self.clock() < due:
                pass
            now = self.clock()
            self.late_s = max(self.late_s, now - due)
            arrived, data = self.queue.popleft()
            if now - arrived > LIVE_LATE_S:
                self.dropped += 1  # live: too late to send, its slot is gone
                due = max(due, now - 0.002)
            else:
                self.send.sendto(data, self.dest)
                self.sent += 1
                due = max(due + spacing_s(len(data), self.rate, len(self.queue)), now - 0.002)
            if now >= next_report:
                report({"sent": self.sent, "dropped": self.dropped, "max_queue": self.max_queue,
                        "late_us": round(self.late_s * 1e6)})  # fmt: skip
                self.max_queue, self.late_s, self.dropped = 0, 0.0, 0
                next_report = now + REPORT_S


def open_sockets(listen_port: int, dest: str, ttl: int, local_address: str | None,
                 local_port: int | None) -> tuple[socket.socket, socket.socket]:  # fmt: skip
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
    rx.bind(("127.0.0.1", listen_port))
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if local_address or local_port:
        tx.bind((local_address or "0.0.0.0", local_port or 0))
    if dest.split(".")[0].isdigit() and 224 <= int(dest.split(".")[0]) <= 239:
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", ttl))
        if local_address:
            tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                          socket.inet_aton(local_address))  # fmt: skip
    else:
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
    return rx, tx


def command(
    listen: int,
    dest: str,
    port: int,
    rate_bps: int,
    ttl: int,
    local_address: str | None = None,
    local_port: int | None = None,
) -> list[str]:
    """The pacer as its own process: the installed node program's ``pace`` subcommand,
    or this module under the interpreter in a source checkout."""
    head = ([sys.executable, "pace"] if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "core.blocks.udp_pacer"])  # fmt: skip
    argv = [*head, "--listen", str(listen), "--to", f"{dest}:{port}", "--rate", str(rate_bps),
            "--ttl", str(ttl)]  # fmt: skip
    if local_address:
        argv += ["--local-address", local_address]
    if local_port:
        argv += ["--local-port", str(local_port)]
    return argv


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zeroloss pace", description=__doc__.splitlines()[0])
    ap.add_argument("--listen", type=int, required=True, help="loopback port to take from")
    ap.add_argument("--to", required=True, help="destination address:port")
    ap.add_argument("--rate", type=float, required=True, help="bits per second")
    ap.add_argument("--ttl", type=int, default=16)
    ap.add_argument("--local-address")
    ap.add_argument("--local-port", type=int)
    a = ap.parse_args(argv)
    host, _, port = a.to.rpartition(":")
    rx, tx = open_sockets(a.listen, host, a.ttl, a.local_address, a.local_port)

    def report(d: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps({"pace": d}) + "\n")
        sys.stdout.flush()

    # off the CPUs busy with interrupts, and not on another channel's sender's CPU
    cpu = priority.pin(priority.claim_quiet_cpu())
    got = priority.first()  # the sending path goes first on this PC
    report({"sent": 0, "max_queue": 0, "late_us": 0, "listening": a.listen, "priority": got,
            "cpu": cpu})  # fmt: skip
    with contextlib.suppress(KeyboardInterrupt):
        Pacer(rx, tx, (host, int(port)), a.rate).run(lambda: False, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
