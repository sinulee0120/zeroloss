"""Is this IP address used by another machine on the link? (docs/09-exceptions.md SND-013)

One job: an ARP probe as RFC 5227 2.1.1 describes (sender IP 0.0.0.0, target = the
address, three probes) from the NIC that holds the address, then listen a moment. A
reply for that address from another MAC, or another machine probing / announcing it,
is a conflict. Needs a packet socket (Linux AF_PACKET, CAP_NET_RAW — the installed node
has it, 06 4.3); without it the answer is "not checked" with why, never "no conflict".
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.blocks.link_monitor import nic_of

ETH_P_ARP = 0x0806
BROADCAST = b"\xff" * 6
PROBES, PROBE_GAP_S, LISTEN_S = 3, 0.2, 1.0  # RFC 5227 uses 1-2 s gaps; a check is short


@dataclass
class ProbeResult:
    checked: bool
    conflict_macs: list[str] = field(default_factory=list)
    nic: str | None = None
    why: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"checked": self.checked, "conflict_macs": self.conflict_macs,
                "nic": self.nic, "why": self.why}  # fmt: skip


def probe_frame(own_mac: bytes, ip: str) -> bytes:
    """Ethernet broadcast + ARP request "who has <ip>" from 0.0.0.0 (RFC 5227 2.1.1)."""
    arp = struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, 1, own_mac, b"\0" * 4, b"\0" * 6,
                      socket.inet_aton(ip))  # fmt: skip
    return BROADCAST + own_mac + struct.pack("!H", ETH_P_ARP) + arp


def conflict_mac(frame: bytes, own_mac: bytes, ip: str) -> str | None:
    """The other machine's MAC when this frame says it uses ``ip`` (a reply for it, or a
    probe / announcement of it from someone else), else None."""
    if len(frame) < 42 or frame[12:14] != struct.pack("!H", ETH_P_ARP):
        return None
    _, ptype, _, _, op, sha, spa, _, tpa = struct.unpack("!HHBBH6s4s6s4s", frame[14:42])
    if ptype != 0x0800 or sha == own_mac:
        return None
    target = socket.inet_aton(ip)
    uses_it = (op == 2 and spa == target) or (op == 1 and (spa == target or (
        spa == b"\0" * 4 and tpa == target)))  # fmt: skip
    return ":".join(f"{b:02x}" for b in sha) if uses_it else None


def probe(ip: str, sys_root: Path = Path("/sys/class/net"), nic: str | None = None) -> ProbeResult:
    """``nic``: ask on this card although it does not hold ``ip`` yet (before adding it,
    07 3.2 B); by default the card that holds the address."""
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        return ProbeResult(False, why=f"not an IPv4 address: {ip!r}")
    nic = nic or nic_of(ip, sys_root)
    if nic is None:
        return ProbeResult(False, why=f"no network card holds {ip}")
    if not sys.platform.startswith("linux"):
        return ProbeResult(False, nic=nic, why="ARP check comes with V18 on this system")
    try:
        mac = bytes.fromhex((sys_root / nic / "address").read_text().strip().replace(":", ""))
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ARP))
    except (OSError, ValueError) as e:
        return ProbeResult(False, nic=nic, why=f"cannot open a packet socket: {e}")
    found: list[str] = []
    try:
        sock.bind((nic, 0))
        sock.settimeout(0.05)
        end = time.monotonic() + PROBE_GAP_S * PROBES + LISTEN_S
        sent, next_send = 0, time.monotonic()
        while time.monotonic() < end:
            if sent < PROBES and time.monotonic() >= next_send:
                sock.send(probe_frame(mac, ip))
                sent += 1
                next_send = time.monotonic() + PROBE_GAP_S
            try:
                other = conflict_mac(sock.recv(2048), mac, ip)
            except TimeoutError:
                continue
            if other and other not in found:
                found.append(other)
    except OSError as e:
        return ProbeResult(False, nic=nic, why=f"ARP probe failed: {e}")
    finally:
        sock.close()
    return ProbeResult(True, found, nic)


def request_frame(own_mac: bytes, own_ip: str, target_ip: str) -> bytes:
    """ARP "who has <target>? tell <own>" (a normal request, not a probe)."""
    arp = struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, 1, own_mac, socket.inet_aton(own_ip),
                      b"\0" * 6, socket.inet_aton(target_ip))  # fmt: skip
    return BROADCAST + own_mac + struct.pack("!H", ETH_P_ARP) + arp


def answers(frame: bytes, target_ip: str) -> bool:
    """An ARP reply from ``target_ip``."""
    if len(frame) < 42 or frame[12:14] != struct.pack("!H", ETH_P_ARP):
        return False
    op = struct.unpack("!H", frame[20:22])[0]
    return op == 2 and frame[28:32] == socket.inet_aton(target_ip)


def alive(nic: str, own_ip: str, target_ip: str, wait_s: float = 2.0,
          sys_root: Path = Path("/sys/class/net")) -> bool | None:  # fmt: skip
    """Does ``target_ip`` answer ARP on this link? None when it cannot be asked (no
    packet socket). Used to tell a stale address after a cable moved (07 3.2 B)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        mac = bytes.fromhex((sys_root / nic / "address").read_text().strip().replace(":", ""))
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ARP))
    except (OSError, ValueError):
        return None
    try:
        sock.bind((nic, 0))
        sock.settimeout(0.05)
        end, next_send = time.monotonic() + wait_s, 0.0
        while time.monotonic() < end:
            if time.monotonic() >= next_send:
                sock.send(request_frame(mac, own_ip, target_ip))
                next_send = time.monotonic() + 0.5
            try:
                if answers(sock.recv(2048), target_ip):
                    return True
            except TimeoutError:
                continue
    except OSError:
        return None
    finally:
        sock.close()
    return False
