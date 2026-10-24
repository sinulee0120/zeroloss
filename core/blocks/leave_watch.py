"""Did the router stop sending the old group after our Leave? (docs/03-features.md F19, 09 NET-005)

One job: open a raw packet view of the receive NIC before the Leave, then for 4 s after it
look for packets of the old group:port. A packet later than 3 s after the Leave means the
router did not process it (``not_processed``). Without permission (CAP_NET_RAW), on a
system without AF_PACKET, or when something else still needs the group, the answer is
``not_checked`` — never a yellow cell (20 GAP-90).

The raw view only reads; the measurement socket is not touched. ALLMULTI makes the NIC
pass multicast it was not asked for, so a group just left is still visible.
"""

from __future__ import annotations

import socket
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from core.blocks.udp_receiver import clock_ns

WATCH_S = 4.0  # how long after the Leave we look
LIMIT_S = 3.0  # a packet later than this after the Leave = not processed (03 F19)
ETH_P_IP = 0x0800
ETH_HEADER = 14
SOL_PACKET = 263
PACKET_ADD_MEMBERSHIP = 1
PACKET_MR_ALLMULTI = 2
SO_TIMESTAMPNS = 35  # Linux asm-generic/socket.h
IPPROTO_UDP = 17


class FrameSource(Protocol):
    def read(self, timeout_s: float) -> tuple[bytes, int] | None:
        """One frame and its arrival time (clock of ``clock_ns``), None on timeout."""

    def close(self) -> None: ...


@dataclass(frozen=True)
class LeaveResult:
    state: str  # "ok" | "not_processed" | "not_checked"
    last_old_packet_ns: int | None = None
    reason: str = ""


def frame_is(frame: bytes, group: str, port: int) -> bool:
    """Ethernet II + IPv4 + UDP to ``group``:``port`` (first fragment only)."""
    if len(frame) < ETH_HEADER + 20 + 8 or frame[12:14] != b"\x08\x00":
        return False
    ip = frame[ETH_HEADER:]
    ihl = (ip[0] & 0x0F) * 4
    if ip[0] >> 4 != 4 or ihl < 20 or len(ip) < ihl + 8 or ip[9] != IPPROTO_UDP:
        return False
    if struct.unpack("!H", ip[6:8])[0] & 0x1FFF:  # not the first fragment: no UDP header
        return False
    return (
        ip[16:20] == socket.inet_aton(group)
        and struct.unpack("!H", ip[ihl + 2 : ihl + 4])[0] == port
    )


class PacketSource:
    """AF_PACKET on one NIC (Linux, CAP_NET_RAW). Raises OSError when it cannot open."""

    def __init__(self, nic: str) -> None:
        family = getattr(socket, "AF_PACKET", None)
        if family is None:
            raise OSError("raw packet view is not available on this system")
        sock = socket.socket(family, socket.SOCK_RAW, socket.htons(ETH_P_IP))
        try:
            sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
            sock.bind((nic, ETH_P_IP))
            mreq = struct.pack(
                "iHH8s", socket.if_nametoindex(nic), PACKET_MR_ALLMULTI, 0, b"\0" * 8
            )
            sock.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)  # dropped on close
        except BaseException:
            sock.close()
            raise
        self._sock: socket.socket | None = sock

    def read(self, timeout_s: float) -> tuple[bytes, int] | None:
        sock = self._sock
        if sock is None:
            return None
        sock.settimeout(timeout_s)
        try:
            data, anc, _, _ = sock.recvmsg(65535, 64)
        except TimeoutError:
            return None
        except OSError:
            return None  # closed under us
        arrival = None
        for level, kind, raw in anc:
            if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPNS and len(raw) >= 16:
                sec, nsec = struct.unpack("qq", raw[:16])
                arrival = sec * 1_000_000_000 + nsec
        return data, arrival if arrival is not None else clock_ns()

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None


class LeaveWatch:
    def __init__(
        self,
        group: str,
        port: int,
        nic: str | None,
        opener: Callable[[str], FrameSource] = PacketSource,
    ) -> None:
        self.group = group
        self.port = port
        self._nic = nic
        self._opener = opener
        self._source: FrameSource | None = None
        self.reason = ""

    def open(self) -> bool:
        """Call before the Leave. False (and ``reason``) when the watch cannot be made."""
        if not self._nic:
            self.reason = "the receive network card is not known"
            return False
        try:
            self._source = self._opener(self._nic)
        except PermissionError:
            self.reason = "no permission for the raw packet view (CAP_NET_RAW)"
            return False
        except OSError as e:
            self.reason = f"raw packet view not available: {e}"
            return False
        return True

    def watch(
        self, leave_ns: int, stop: threading.Event, clock: Callable[[], int] = clock_ns
    ) -> LeaveResult:
        """Blocks until ``WATCH_S`` after the Leave (or ``stop``), then closes the view."""
        src = self._source
        if src is None:
            return LeaveResult("not_checked", None, self.reason or "not opened")
        end_ns = leave_ns + int(WATCH_S * 1e9)
        last: int | None = None
        try:
            while not stop.is_set():
                left_s = (end_ns - clock()) / 1e9
                if left_s <= 0:
                    break
                got = src.read(min(0.2, left_s))
                if got is None:
                    continue
                frame, t = got
                if t >= leave_ns and frame_is(frame, self.group, self.port):
                    last = t
        finally:
            self.close()
        if stop.is_set():
            return LeaveResult("not_checked", last, "stopped before the check ended")
        if last is not None and (last - leave_ns) / 1e9 > LIMIT_S:
            return LeaveResult("not_processed", last)
        return LeaveResult("ok", last)

    def close(self) -> None:
        if self._source is not None:
            self._source.close()
            self._source = None
