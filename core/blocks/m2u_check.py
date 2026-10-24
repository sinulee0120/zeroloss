"""Is the router turning multicast into unicast on Wi-Fi (M2U)? (docs/10-verification.md 4 A)

One job: look at the destination MAC of frames that carry a given group:port on one
NIC. The IP stays 239.x either way; only the MAC tells: this NIC's own MAC = the router
sent it as unicast (M2U active), 01:00:5e:… = still multicast (not active). Needs a
packet socket (Linux AF_PACKET, CAP_NET_RAW); anything else is "unknown" with why.
Never used for the verdict, only for cause analysis and the report.
"""

from __future__ import annotations

import ctypes
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

ETH_P_IP = 0x0800
MAX_FRAMES = 50
MULTICAST_PREFIX = "01:00:5e"


def mac_text(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def frame_dest(frame: bytes, group: str, port: int) -> str | None:
    """Destination MAC of an Ethernet frame carrying UDP to group:port, else None."""
    if len(frame) < 14 + 20 + 8 or frame[12:14] != b"\x08\x00":
        return None
    ip = frame[14:]
    ihl = (ip[0] & 0x0F) * 4
    if ip[0] >> 4 != 4 or ihl < 20 or len(ip) < ihl + 8 or ip[9] != 17:
        return None
    if socket.inet_ntoa(ip[16:20]) != group:
        return None
    (dport,) = struct.unpack("!H", ip[ihl + 2 : ihl + 4])
    return mac_text(frame[:6]) if dport == port else None


def verdict(dests: Iterable[str], own_mac: str | None, why_none: str = "") -> dict[str, Any]:
    """02 10 ``devices[].m2u``: state active / not_active / unknown, why, frames seen."""
    seen = list(dests)
    own = (own_mac or "").lower()
    unicast = sum(1 for d in seen if own and d == own)
    multicast = sum(1 for d in seen if d.startswith(MULTICAST_PREFIX))
    if not seen:
        return {"state": "unknown", "why": why_none or "no test frames seen", "frames": 0}
    if unicast and not multicast:
        why = f"{unicast} frames sent to this PC's own MAC (unicast)"
        return {"state": "active", "why": why, "frames": len(seen)}
    if multicast and not unicast:
        why = f"{multicast} frames still sent to the multicast MAC"
        return {"state": "not_active", "why": why, "frames": len(seen)}
    why = f"mixed: {unicast} unicast, {multicast} multicast of {len(seen)} frames"
    return {"state": "unknown", "why": why, "frames": len(seen)}


def is_wireless(nic: str, sys_root: Path = Path("/sys/class/net")) -> bool:
    """Only Wi-Fi can have M2U; a wired port gets multicast as is (IGMP snooping only).
    ``phy80211`` too: a kernel without wireless extensions has no ``wireless`` folder."""
    return (sys_root / nic / "wireless").exists() or (sys_root / nic / "phy80211").exists()


def own_mac(nic: str, sys_root: Path = Path("/sys/class/net")) -> str | None:
    try:
        return (sys_root / nic / "address").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def capture(nic: str, group: str, port: int, seconds: float) -> dict[str, Any]:
    """Watch ``nic`` for up to ``seconds`` or MAX_FRAMES matching frames."""
    if not sys.platform.startswith("linux"):
        return verdict([], None, "packet capture is Linux only")
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_IP))
    except PermissionError:
        return verdict([], None, "no permission to capture (CAP_NET_RAW)")
    except OSError as e:
        return verdict([], None, f"cannot capture: {e.strerror or e}")
    dests: list[str] = []
    try:
        sock.bind((nic, 0))
        end = time.monotonic() + seconds
        while len(dests) < MAX_FRAMES and (left := end - time.monotonic()) > 0:
            sock.settimeout(min(left, 0.2))
            try:
                frame, addr = sock.recvfrom(2048)
            except TimeoutError:
                continue
            if addr[2] == socket.PACKET_OUTGOING:  # frames this PC sends: not received
                continue
            d = frame_dest(frame, group, port)
            if d is not None:
                dests.append(d)
    except OSError as e:
        return verdict(dests, own_mac(nic), f"capture failed: {e.strerror or e}")
    finally:
        sock.close()
    return verdict(dests, own_mac(nic))


SOL_PACKET = 263
PACKET_STATISTICS = 6
SO_ATTACH_FILTER = 26
SNAP = 64  # enough for the Ethernet, IP and UDP headers; the rest stays in the kernel
MULTICAST_MAC = bytes.fromhex("01005e")


def stream_filter(group: str, port: int) -> list[tuple[int, int, int, int]]:
    """Classic BPF (code, jt, jf, k): IPv4 UDP to group:port, first fragment, cut to SNAP
    bytes; everything else stays in the kernel. Offsets are of an Ethernet frame."""
    (g,) = struct.unpack("!I", socket.inet_aton(group))
    return [
        (0x28, 0, 0, 12),  # ldh [12]            EtherType
        (0x15, 0, 10, ETH_P_IP),  # jeq IPv4, else drop
        (0x20, 0, 0, 30),  # ld [30]             IP destination
        (0x15, 0, 8, g),  # jeq group, else drop
        (0x30, 0, 0, 23),  # ldb [23]            IP protocol
        (0x15, 0, 6, 17),  # jeq UDP, else drop
        (0x28, 0, 0, 20),  # ldh [20]            fragment offset
        (0x45, 4, 0, 0x1FFF),  # jset: a later fragment has no UDP header -> drop
        (0xB1, 0, 0, 14),  # ldxb 4*([14]&0xf)   IP header length
        (0x48, 0, 0, 16),  # ldh [x+16]          UDP destination port
        (0x15, 0, 1, port),  # jeq port, else drop
        (0x06, 0, 0, SNAP),  # keep SNAP bytes
        (0x06, 0, 0, 0),  # drop
    ]


def _open_stream_socket(nic: str, group: str, port: int) -> socket.socket:
    """AF_PACKET on ``nic`` with ``stream_filter`` (Linux, CAP_NET_RAW). Raises OSError."""
    family = getattr(socket, "AF_PACKET", None)
    if family is None:
        raise OSError("packet capture is Linux only")
    sock = socket.socket(family, socket.SOCK_RAW, 0)  # protocol 0: nothing comes in yet
    try:
        prog = b"".join(struct.pack("HBBI", *ins) for ins in stream_filter(group, port))
        buf = ctypes.create_string_buffer(prog)
        fprog = struct.pack("HL", len(prog) // 8, ctypes.addressof(buf))
        sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, fprog)
        sock.bind((nic, ETH_P_IP))  # only now frames come, already filtered
    except BaseException:
        sock.close()
        raise
    return sock


class M2uCounter:
    """02 7 ``fm``/``fu`` (GAP-148): of the stream's frames on one Wi-Fi card, how many
    came to the multicast MAC and how many to this PC's own MAC (the router turned them
    into unicast, M2U). One thread drains a filtered packet socket; ``take`` gives the
    counts since the last call, or nothing when the kernel dropped frames of the view in
    that time (a partial count is not a measurement). Raises OSError when it cannot open
    (no CAP_NET_RAW, not Linux): the keys are then absent."""

    def __init__(
        self,
        nic: str,
        group: str,
        port: int,
        opener: Callable[[str, str, int], Any] = _open_stream_socket,
    ) -> None:
        own = own_mac(nic)
        self._own = bytes.fromhex(own.replace(":", "")) if own else None
        self._sock = opener(nic, group, port)
        self._lock = threading.Lock()
        self._multicast = 0
        self._unicast = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="m2u-1s", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        sock = self._sock
        sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                frame, addr = sock.recvfrom(SNAP)
            except TimeoutError:
                continue
            except OSError:
                return  # closed under us
            if addr[2] == socket.PACKET_OUTGOING:
                continue
            dest = frame[:6]
            with self._lock:
                if dest[:3] == MULTICAST_MAC:
                    self._multicast += 1
                elif self._own is not None and dest == self._own:
                    self._unicast += 1

    def take(self) -> dict[str, int]:
        try:  # read-and-reset counters of the view: tpacket_stats {packets, drops}
            _, drops = struct.unpack("II", self._sock.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8))
        except OSError:
            drops = 1
        with self._lock:
            got = {"fm": self._multicast, "fu": self._unicast}
            self._multicast = self._unicast = 0
        return {} if drops else got

    def close(self) -> None:
        self._stop.set()
        self._thread.join(1.0)
        self._sock.close()
