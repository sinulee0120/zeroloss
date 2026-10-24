"""Send a small UDP datagram from a port another program is bound to (a primed stream).

One job: the prime of primed mode for a player (VLC) must leave from the player's own
receive port, so the router's NAT opens the path to that port. Binding a second socket
to that port would steal the incoming stream (measured 2026-09-25), so the datagram is
built here and sent through a raw IP socket (Linux, CAP_NET_RAW). Nothing is bound.
"""

from __future__ import annotations

import socket
import struct

IPPROTO_UDP = 17


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s: int = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def packet(src_ip: str, src_port: int, dst_ip: str, dst_port: int, payload: bytes) -> bytes:
    """IPv4 header + UDP header + payload, checksums filled (RFC 791, RFC 768)."""
    src, dst = socket.inet_aton(src_ip), socket.inet_aton(dst_ip)
    length = 8 + len(payload)
    pseudo = src + dst + struct.pack("!BBH", 0, IPPROTO_UDP, length)
    udp = struct.pack("!HHHH", src_port, dst_port, length, 0) + payload
    csum = checksum(pseudo + udp) or 0xFFFF  # 0 means "no checksum" in UDP
    udp = udp[:6] + struct.pack("!H", csum) + udp[8:]
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + length, 0, 0x4000, 64, IPPROTO_UDP, 0,
                     src, dst)  # fmt: skip
    ip = ip[:10] + struct.pack("!H", checksum(ip)) + ip[12:]
    return ip + udp


def source_ip_for(dst_ip: str) -> str:
    """This PC's address on the route to ``dst_ip`` (nothing is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((dst_ip, 9))
        return str(s.getsockname()[0])


def send_from_port(src_ip: str, src_port: int, dst_ip: str, dst_port: int, payload: bytes) -> None:
    """Raises PermissionError without CAP_NET_RAW, OSError on other send errors."""
    if not src_ip or src_ip == "0.0.0.0":
        src_ip = source_ip_for(dst_ip)
    with socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW) as s:
        s.sendto(packet(src_ip, src_port, dst_ip, dst_port, payload), (dst_ip, 0))
