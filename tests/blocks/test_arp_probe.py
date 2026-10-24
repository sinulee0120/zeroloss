"""ARP probe for a WAN address conflict (docs/09-exceptions.md SND-013, RFC 5227 2.1.1)."""

from __future__ import annotations

import os
import socket
import struct
from pathlib import Path

import pytest

from core.blocks.arp_probe import conflict_mac, probe, probe_frame

MINE = bytes.fromhex("020000000001")
OTHER = bytes.fromhex("020000000002")
IP = "10.0.0.2"


def arp(op: int, sha: bytes, spa: str, tpa: str) -> bytes:
    body = struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, op, sha, socket.inet_aton(spa),
                       b"\0" * 6, socket.inet_aton(tpa))  # fmt: skip
    return b"\xff" * 6 + sha + struct.pack("!H", 0x0806) + body


def test_probe_frame_is_rfc_5227() -> None:
    f = probe_frame(MINE, IP)
    assert f[:6] == b"\xff" * 6 and f[6:12] == MINE and f[12:14] == b"\x08\x06"
    _, _, _, _, op, sha, spa, tha, tpa = struct.unpack("!HHBBH6s4s6s4s", f[14:42])
    assert (op, sha, spa, tha, tpa) == (1, MINE, b"\0" * 4, b"\0" * 6, socket.inet_aton(IP))


@pytest.mark.parametrize(("frame", "conflict"), [
    (arp(2, OTHER, IP, "0.0.0.0"), True),  # someone answers for our address
    (arp(1, OTHER, IP, "10.0.0.100"), True),  # someone announces / uses it
    (arp(1, OTHER, "0.0.0.0", IP), True),  # someone else probes for it too
    (arp(2, MINE, IP, "0.0.0.0"), False),  # our own frame
    (arp(2, OTHER, "10.0.0.100", IP), False),  # the router answering for itself
    (arp(1, OTHER, "10.0.0.100", IP), False),  # the router asking for us: normal
    (b"\x00" * 20, False),
])  # fmt: skip
def test_what_counts_as_a_conflict(frame: bytes, conflict: bool) -> None:
    got = conflict_mac(frame, MINE, IP)
    assert (got == "02:00:00:00:00:02") if conflict else got is None


def test_not_checked_is_never_no_conflict(tmp_path: Path) -> None:
    assert probe("not-an-ip").checked is False
    r = probe("10.9.9.9", sys_root=tmp_path)  # no NIC has it
    assert not r.checked and "no network card" in str(r.why)


@pytest.mark.skipif(os.geteuid() == 0, reason="root may open packet sockets")
def test_without_the_right_it_says_why() -> None:
    import subprocess

    ip = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
    if not ip:
        pytest.skip("no IPv4 address on this PC")
    r = probe(ip[0])
    if r.checked:
        pytest.skip("this process may open packet sockets")
    assert r.nic and "packet socket" in str(r.why)
