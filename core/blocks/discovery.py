"""Find nodes on the test network by UDP (docs/06-install.md 10.2, 02 4-1).

One job, both sides: a node's ``Responder`` answers ``ZLDISCOVER 1 <nonce>`` with one
JSON line (only when the node says it may, i.e. between tests); the Controller's
``ask`` sends the request to broadcast addresses and gathers the answers with the
same nonce. Nothing here opens a TCP connection or runs while a test runs.
"""

from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.blocks.link_monitor import ipv4_netmask, ipv4_of

TAG = b"ZLDISCOVER 1 "
ALL = "255.255.255.255"


def request(nonce: str) -> bytes:
    return TAG + nonce.encode("ascii")


def nonce_of(data: bytes) -> str | None:
    if not data.startswith(TAG) or len(data) > 128:
        return None
    try:
        text = data[len(TAG) :].decode("ascii")
    except UnicodeDecodeError:
        return None
    return text if text and text.isalnum() else None


class Responder:
    """UDP answerer beside the node's TCP port. ``answer()`` returns the reply body or
    None to stay quiet (e.g. during a test, 07 7)."""

    def __init__(self, port: int, answer: Callable[[], dict[str, Any] | None]) -> None:
        self.answer = answer
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", port))
        self._sock.settimeout(0.5)
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="discovery", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        self._thread.join(2)
        self._sock.close()

    def _loop(self) -> None:
        while not self._halt.is_set():
            try:
                data, src = self._sock.recvfrom(256)
            except TimeoutError:
                continue
            except OSError:
                return
            nonce = nonce_of(data)
            body = self.answer() if nonce else None
            if body is None:
                continue
            reply = json.dumps({"zl": "discover", "v": 1, "nonce": nonce, **body})
            try:
                self._sock.sendto(reply.encode("utf-8"), src)
            except OSError:
                continue  # the asker is gone; nothing to do


def default_route_nics(route: Path = Path("/proc/net/route")) -> set[str]:
    """NICs holding an IPv4 default route (the internet side of a Controller)."""
    try:
        lines = route.read_text().splitlines()[1:]
    except OSError:
        return set()
    return {f[0] for f in (x.split() for x in lines) if len(f) > 2 and f[1] == "00000000"}


def _windows_broadcasts() -> list[str]:
    """Windows (GAP-155, 2026-10-15): 255.255.255.255 follows the default route only (the
    home Wi-Fi), so a PC on the router's LAN was never found. Each card's /24 broadcast
    goes out on that card (the subnet is on-link there)."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return [ALL]
    nets = dict.fromkeys(
        ".".join([*str(i[4][0]).split(".")[:3], "255"])
        for i in infos
        if not str(i[4][0]).startswith(("127.", "169.254."))
    )
    return [*nets, ALL]


def broadcast_addresses(sys_root: Path = Path("/sys/class/net"),
                        route: Path = Path("/proc/net/route")) -> list[str]:  # fmt: skip
    """The directed broadcast address of every test-network IPv4 NIC (Linux). 13 1:
    discovery never leaves on the internet NIC (the one with the default route) when
    another NIC exists — nor does 255.255.255.255 then, which would follow that route."""
    if sys.platform == "win32" and not sys_root.is_dir():
        return _windows_broadcasts()
    nics: list[tuple[str, str]] = []
    for d in sorted(sys_root.iterdir()) if sys_root.is_dir() else []:
        ip, mask = ipv4_of(d.name), ipv4_netmask(d.name)
        if not ip or not mask or ip.startswith("127."):
            continue
        a, m = (
            struct.unpack("!I", socket.inet_aton(ip))[0],
            struct.unpack("!I", socket.inet_aton(mask))[0],
        )
        nics.append((d.name, socket.inet_ntoa(struct.pack("!I", (a & m) | (~m & 0xFFFFFFFF)))))
    internet = default_route_nics(route)
    test = [(n, b) for n, b in nics if n not in internet]
    if test and len(test) < len(nics):  # a separate internet NIC: keep off it (13 1)
        return list(dict.fromkeys(b for _, b in test))
    return [*dict.fromkeys(b for _, b in nics), ALL]


def ask(
    targets: list[str], port: int, wait_s: float = 1.5, tries: int = 3, gap_s: float = 0.3
) -> list[tuple[dict[str, Any], str]]:
    """Send the request ``tries`` times to every target, gather answers for ``wait_s``.
    Returns (answer, source address) pairs; answers without our nonce are dropped."""
    nonce = uuid.uuid4().hex
    got: list[tuple[dict[str, Any], str]] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", 0))
        end = time.monotonic() + wait_s
        next_send, sent = time.monotonic(), 0
        while (left := end - time.monotonic()) > 0:
            if sent < tries and time.monotonic() >= next_send:
                for t in targets:
                    try:
                        s.sendto(request(nonce), (t, port))
                    except OSError:
                        continue  # a NIC without a route: the others still go
                sent += 1
                next_send = time.monotonic() + gap_s
            s.settimeout(max(0.01, min(left, gap_s)))
            try:
                data, src = s.recvfrom(4096)
            except TimeoutError:
                continue
            try:
                body = json.loads(data)
            except ValueError:
                continue
            if (
                isinstance(body, dict)
                and body.get("zl") == "discover"
                and body.get("nonce") == nonce
            ):
                got.append((body, str(src[0])))
    return got
