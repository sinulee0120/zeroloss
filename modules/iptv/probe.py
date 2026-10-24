"""Multicast test before a test (03 F17 step 5, 02 4 ``…/probe`` routes).

The receiver starts listening first and answers at once with a probe ID; then the
sender sends a short stream of null TS packets as RTP (1 Mbps, no video file needed);
then the receiver's count is fetched. Nothing here is measured or judged: it only
shows whether the multicast path works at all (0 packets -> NET-003).
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.blocks.link_monitor import nic_of, read_links
from core.blocks.m2u_check import capture, is_wireless, verdict
from core.blocks.process_runner import ProcessRunner, ProcState, StartError, StopError
from core.blocks.udp_receiver import UdpReceiver
from core.protocol.envelope import ZlError
from modules.iptv.sender.prime import parse_prime

PROBE_BPS = 1_000_000
TS_BITS = 188 * 8
SEND_S_MAX = 10
LISTEN_S_MAX = 30
KEEP = 16  # finished probes waiting for their result call
TAIL_S = 0.3
PRIME_PROBE_S = 0.5  # primed test: prime this often while listening


def probe_command(
    tsp: str, dest_ip: str, port: int, seconds: float, ttl: int, local_address: str | None,
    local_port: int | None = None,
) -> list[str]:  # fmt: skip
    """A fixed number of null packets, paced at 1 Mbps, sent as RTP like the real stream."""
    count = max(1, int(PROBE_BPS * seconds) // TS_BITS)
    cmd = [tsp, "-I", "null", str(count)]
    cmd += ["-P", "regulate", "--bitrate", str(PROBE_BPS), "--packet-burst", "7"]
    cmd += ["-O", "ip", f"{dest_ip}:{port}", "--rtp", "--payload-type", "33"]
    cmd += ["--packet-burst", "7", "--enforce-burst", "--ttl", str(ttl)]
    if local_address:
        cmd += ["--local-address", local_address]
    if local_port:
        cmd += ["--local-port", str(local_port)]
    return cmd


def wait_prime(
    local_address: str | None, q: int, run_id: str, ch: int, wait_s: float
) -> tuple[str, int]:
    """Primed test send (iptv 4.5): the first matching prime on Q gives the target
    (router WAN address and NAT port). Raises ZlError NET-003 when none comes."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((local_address or "0.0.0.0", q))
        end = time.monotonic() + wait_s
        while (left := end - time.monotonic()) > 0:
            s.settimeout(min(left, 0.2))
            try:
                data, src = s.recvfrom(512)
            except TimeoutError:
                continue
            if parse_prime(data) == (run_id, ch, 0):
                return str(src[0]), int(src[1])
    raise ZlError(
        "NET-003", f"CH{ch}: no prime from the receiver in {wait_s:g} s",
        "The primed path did not open: the receiver's small packet did not reach the sender.",
        "Check that the router is in NAT mode and the receiver PC is on its LAN.",
    )  # fmt: skip


def send_probe(
    argv: list[str], procs_dir: Path, log_path: Path, seconds: float, ch: int
) -> dict[str, float]:
    """Run the probe sender to its end (bounded). Raises ZlError SND-005 on failure."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    runner = ProcessRunner(f"tsp_probe_ch{ch}", argv, procs_dir, log_path)
    t0 = time.monotonic()
    try:
        runner.start()
    except StartError as e:
        raise ZlError("SND-005", f"CH{ch} test send did not start", str(e)) from e
    deadline = t0 + seconds + 5.0
    while runner.poll() is ProcState.RUNNING and time.monotonic() < deadline:
        time.sleep(0.05)
    finished = runner.state is ProcState.FAULT  # exited by itself
    try:
        runner.stop()
    except StopError as e:
        raise ZlError("SYS-006", f"CH{ch} test sender did not stop", str(e)) from e
    if not finished or runner.exit_code != 0:
        why = "it did not end in time" if not finished else f"exit code {runner.exit_code}"
        raise ZlError(
            "SND-005", f"CH{ch} test send failed", why, "Check tsp on the sender PC.",
            detail=str(log_path),
        )  # fmt: skip
    return {"sent_s": round(time.monotonic() - t0, 2)}


@dataclass
class _Listen:
    seconds: float
    packets: int = 0
    first_ms: float | None = None
    error: str = ""
    prime: tuple[str, int, int, bytes] | None = None  # (sender_ip, p, q, payload)
    m2u: dict[str, Any] | None = None  # 03 F17 "무선 수신 PC": M2U seen on this NIC
    m2u_done: threading.Event = field(default_factory=threading.Event)
    cards: dict[int, list[Any]] = field(default_factory=dict)  # card index -> [ip, packets]
    done: threading.Event = field(default_factory=threading.Event)
    enough: threading.Event = field(default_factory=threading.Event)  # asked, got some


class Listener:
    """Receiver side: one thread per probe, each with its own socket."""

    def __init__(self) -> None:
        self._probes: dict[str, _Listen] = {}
        self._lock = threading.Lock()

    def start(
        self, group: str, port: int, interface_ip: str, seconds: float, m2u: bool = False,
        prime: tuple[str, int, int, bytes] | None = None,
    ) -> str:  # fmt: skip
        """``prime`` (sender_ip, P, Q, payload): primed mode — listen on ``port`` as unicast
        and prime every 0.5 s while listening (the sender may open Q a little later)."""
        join_on = None
        if prime is None and interface_ip in ("", "0.0.0.0"):
            join_on = every_card() or None  # 02 4 (GAP-115): the card is not known yet
        try:
            rx = UdpReceiver(port, None if prime else group, interface_ip or "0.0.0.0",
                             join_on=join_on, arrival_card=True)  # fmt: skip
        except OSError as e:
            raise ZlError(
                "RCV-017", f"Cannot join {group}:{port}", str(e),
                "Check the receiver PC's test NIC address.",
            ) from e  # fmt: skip
        probe_id = uuid.uuid4().hex[:12]
        job = _Listen(seconds, prime=prime)
        with self._lock:
            while len(self._probes) >= KEEP:  # results nobody asked for
                self._probes.pop(next(iter(self._probes)))
            self._probes[probe_id] = job
        threading.Thread(target=_count, args=(rx, job), name="iptv-probe", daemon=True).start()
        if m2u and prime is None:  # its own packet socket beside the UDP one (10 4 method A)
            nic = nic_of(interface_ip)
            args = (job, nic, group, port)
            threading.Thread(target=_m2u, args=args, name="iptv-m2u", daemon=True).start()
        else:
            job.m2u_done.set()
        return probe_id

    def result(self, probe_id: str) -> dict[str, object]:
        with self._lock:
            job = self._probes.get(probe_id)
        if job is None:
            raise ZlError("DSC-012", "Request rejected", detail=f"no probe {probe_id}")
        if job.packets:  # the sender has finished: the tail is in within a moment
            time.sleep(TAIL_S)
            job.enough.set()
        job.done.wait(job.seconds + 2.0)
        job.m2u_done.wait(job.seconds + 2.0)
        with self._lock:
            self._probes.pop(probe_id, None)
        return {"packets": job.packets, "first_ms": job.first_ms, "error": job.error or None,
                "m2u": job.m2u, "arrived_on": arrived_on(job.cards),
                "arrived_cards": arrived_cards(job.cards)}  # fmt: skip

    def busy(self) -> bool:
        with self._lock:
            return any(not j.done.is_set() for j in self._probes.values())


def every_card() -> list[str]:
    """The addresses of the physical cards that are up (no loopback, no virtual ones)."""
    return [x.ip for x in read_links(wifi_of=lambda _: None) if x.up and x.ip]


def arrived_cards(cards: dict[int, list[Any]]) -> list[dict[str, Any]]:
    """02 4 ``arrived_cards``: every card the packets came in on, most first (two cards
    of one PC on the same router LAN both receive)."""
    out = []
    for index, (ip, packets) in sorted(cards.items(), key=lambda kv: -kv[1][1]):
        try:
            nic = socket.if_indextoname(index)
        except OSError:
            nic = str(index)
        out.append({"nic": nic, "ip": ip, "packets": packets})
    return out


def arrived_on(cards: dict[int, list[Any]]) -> dict[str, str] | None:
    """02 4 ``arrived_on``: the card most of the packets came in on."""
    got = arrived_cards(cards)
    return {"nic": got[0]["nic"], "ip": got[0]["ip"]} if got else None


def _m2u(job: _Listen, nic: str | None, group: str, port: int) -> None:
    try:
        if nic is None:
            job.m2u = verdict([], None, "the receiving network card was not found")
        elif not is_wireless(nic):
            job.m2u = None  # wired: no M2U, IGMP snooping only (03 F17, 2026-09-25)
        else:
            job.m2u = capture(nic, group, port, job.seconds)
    finally:
        job.m2u_done.set()


def _count(rx: UdpReceiver, job: _Listen) -> None:
    t0 = time.monotonic()
    next_prime = t0
    try:
        with rx:
            while (left := t0 + job.seconds - time.monotonic()) > 0 and not job.enough.is_set():
                if job.prime is not None and time.monotonic() >= next_prime:
                    ip, p, q, payload = job.prime
                    for port in (p, q):
                        rx.send_to(payload, ip, port)
                    next_prime = time.monotonic() + PRIME_PROBE_S
                if (pkt := rx.receive(min(left, 0.2, PRIME_PROBE_S))) is not None:
                    job.packets += 1
                    if pkt.card_index is not None:
                        job.cards.setdefault(pkt.card_index, [pkt.card_ip, 0])[1] += 1
                    if job.first_ms is None:
                        job.first_ms = round((time.monotonic() - t0) * 1000, 1)
    except OSError as e:
        job.error = str(e)
    finally:
        job.done.set()
