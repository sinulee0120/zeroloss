"""UDP receive socket: join/leave, big buffer, arrival time, own drops (docs/05-standards.md 4).

One socket = one consumer. The analyzer and the player each open their own socket on the
same group and port (4.2, port sharing); the analyzer never relays packets to the player.

- Group membership uses the OS (``IP_ADD_MEMBERSHIP``); no IGMP packets are built here (4.1).
- The receive buffer request (default 16 MB) is checked; the applied size is kept so a
  short buffer can be reported (RCV-001).
- Linux: kernel arrival time (``SO_TIMESTAMPNS``) and this socket's own drop counter
  (``SO_RXQ_OVFL``, RCV-002). Elsewhere: monotonic clock right after receive, drops unknown.
  The kernel attaches the drop count to the next packet queued, so drops at the very end
  are only seen by ``own_drops()`` (read from ``/proc/net/udp``); call it once per second.
- ``receive()`` only receives. Counting and writing happen in another thread (4.2).
- ``join_on``: join the group on each of these card addresses instead of the one the
  route picks (a test whose receiving card is not known yet, 02 4 GAP-115).
  ``arrival_card``: each packet says which card it came in on (Linux ``IP_PKTINFO``).
"""

from __future__ import annotations

import contextlib
import errno
import os
import socket
import struct
import sys
import time
from dataclasses import dataclass

DEFAULT_RCVBUF_BYTES = 16 * 1024 * 1024
MAX_DATAGRAM = 65535
_LINUX = sys.platform.startswith("linux")
_SO_TIMESTAMPNS = 35  # Linux asm-generic/socket.h (not exported by the socket module)
_SO_RXQ_OVFL = 40
_IP_MULTICAST_ALL = 49  # Linux in.h: 0 = only groups this socket joined
_IP_PKTINFO = getattr(socket, "IP_PKTINFO", 8)


def clock_ns() -> int:
    """The clock ``Packet.arrival_ns`` uses, for second boundaries on the same scale."""
    return time.time_ns() if _LINUX else time.monotonic_ns()


@dataclass(frozen=True)
class Packet:
    data: bytes
    arrival_ns: int  # Linux: kernel real-time clock. Others: monotonic clock
    drops_total: int | None  # this socket's own buffer drops so far (None = unknown on this OS)
    card_index: int | None = None  # the card it came in on (``arrival_card``), else None
    card_ip: str | None = None  # that card's address


class UdpReceiver:
    def __init__(
        self,
        port: int,
        group: str | None = None,
        interface_ip: str = "0.0.0.0",
        rcvbuf_bytes: int = DEFAULT_RCVBUF_BYTES,
        join_on: list[str] | None = None,
        arrival_card: bool = False,
    ) -> None:
        self.port = port
        self.group: str | None = None
        self.interface_ip = interface_ip
        self.join_on = list(join_on or [])
        self.arrival_card = arrival_card and _LINUX
        self.rcvbuf_requested = rcvbuf_bytes
        self.kernel_timestamps = False
        self._drops = 0 if _LINUX else None
        self._buf = bytearray(MAX_DATAGRAM)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf_bytes)
            if _LINUX:
                sock.setsockopt(socket.SOL_SOCKET, _SO_TIMESTAMPNS, 1)
                sock.setsockopt(socket.SOL_SOCKET, _SO_RXQ_OVFL, 1)
                self.kernel_timestamps = True
                # Without this, a socket on the port also gets groups joined by others.
                sock.setsockopt(socket.IPPROTO_IP, _IP_MULTICAST_ALL, 0)
                if self.arrival_card:
                    sock.setsockopt(socket.IPPROTO_IP, _IP_PKTINFO, 1)
            sock.bind(("" if group else interface_ip, port))
            self._sock: socket.socket | None = sock
            if group:
                self.join(group)
        except BaseException:
            sock.close()
            raise
        applied = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        # Linux reports double the usable size (socket(7)).
        self.rcvbuf_applied = applied // 2 if _LINUX else applied

    @property
    def rcvbuf_short(self) -> bool:
        """True when the OS gave less than requested (RCV-001)."""
        return self.rcvbuf_applied < self.rcvbuf_requested

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def join(self, group: str) -> None:
        sock = self._require_open()
        if self.group is not None:
            raise RuntimeError(f"already joined {self.group}; leave() first")
        for card in self.join_on or [self.interface_ip]:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, self._mreq(group, card))
        self.group = group

    def rejoin(self) -> str | None:
        """Join the group again on the same card address (2026-10-17: a USB Wi-Fi card
        was moved to another port; its membership went with the old card and nothing
        came any more). None = joined; else why not (the card has no address yet)."""
        sock = self._require_open()
        if self.group is None:
            return None
        for card in self.join_on or [self.interface_ip]:
            mreq = self._mreq(self.group, card)
            with contextlib.suppress(OSError):  # it went with the old card
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as e:
                if e.errno != errno.EADDRINUSE:
                    return f"{card}: {e.strerror or e}"
        return None

    def leave(self) -> None:
        sock = self._require_open()
        if self.group is None:
            return
        for card in self.join_on or [self.interface_ip]:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP,
                            self._mreq(self.group, card))  # fmt: skip
        self.group = None

    def receive(self, timeout_s: float) -> Packet | None:
        """Wait up to ``timeout_s`` for one datagram. None on timeout."""
        sock = self._require_open()
        sock.settimeout(timeout_s)
        try:
            card: tuple[int, str] | None = None
            if _LINUX:
                n, anc, _, _ = sock.recvmsg_into([self._buf], 128 if self.arrival_card else 64)
                arrival = self._read_ancillary(anc)
                if self.arrival_card:
                    card = _card_of(anc)
            else:
                n = sock.recv_into(self._buf)
                arrival = None
        except TimeoutError:
            return None
        if arrival is None:  # same clock as the kernel time on Linux
            arrival = clock_ns()
        if card is None:
            return Packet(bytes(self._buf[:n]), arrival, self._drops)
        return Packet(bytes(self._buf[:n]), arrival, self._drops, card[0], card[1])

    def send_to(self, data: bytes, host: str, port: int) -> None:
        """Send from this socket's own port (the prime of a primed stream); safe
        beside ``receive`` on another thread (UDP)."""
        self._require_open().sendto(data, (host, port))

    @property
    def local_port(self) -> int:
        return int(self._require_open().getsockname()[1])

    def own_drops(self) -> int | None:
        """This socket's buffer drops right now (Linux). None when the OS cannot tell."""
        sock = self._require_open()
        if not _LINUX:
            return None
        inode = str(os.fstat(sock.fileno()).st_ino)
        with open("/proc/net/udp", encoding="ascii") as f:
            for line in f:
                cols = line.split()
                if len(cols) > 12 and cols[9] == inode:
                    self._drops = max(self._drops or 0, int(cols[-1]))
                    return self._drops
        return None

    def close(self) -> None:
        """Leave the group and close. Safe to call more than once."""
        if self._sock is None:
            return
        try:
            self.leave()
        except OSError:
            pass  # interface already gone: closing the socket drops membership anyway
        finally:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> UdpReceiver:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---------------------------------------------------------------- helpers

    def _require_open(self) -> socket.socket:
        if self._sock is None:
            raise RuntimeError("receiver is closed")
        return self._sock

    @staticmethod
    def _mreq(group: str, card: str) -> bytes:
        return struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(card))

    def _read_ancillary(self, anc: list[tuple[int, int, bytes]]) -> int | None:
        arrival = None
        for level, kind, data in anc:
            if level != socket.SOL_SOCKET:
                continue
            if kind == _SO_TIMESTAMPNS and len(data) >= 16:
                sec, nsec = struct.unpack("qq", data[:16])
                arrival = sec * 1_000_000_000 + nsec
            elif kind == _SO_RXQ_OVFL and len(data) >= 4:
                self._drops = max(self._drops or 0, struct.unpack("I", data[:4])[0])
        return arrival


def _card_of(anc: list[tuple[int, int, bytes]]) -> tuple[int, str] | None:
    """``struct in_pktinfo`` (ip(7)): the card index and that card's address."""
    for level, kind, data in anc:
        if level == socket.IPPROTO_IP and kind == _IP_PKTINFO and len(data) >= 12:
            index, spec_dst, _ = struct.unpack("I4s4s", data[:12])
            return index, socket.inet_ntoa(spec_dst)
    return None
