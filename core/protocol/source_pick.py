"""Which local address reaches a node (docs/20-spec-gaps.md GAP-153).

Windows picks the outgoing NIC from the route table alone. With two default routes (the
office or home Wi-Fi and the test router's LAN), the sender PC on the router WAN side
(10.0.0.1, 07 3.2 B) is asked through the Wi-Fi and never answers: 2026-10-10 the
Controller sat in FAULT "Cannot reach probook-sender" while the cable was fine.
Windows sends from the NIC that owns the source address (strong host model), so a
connection bound to the test NIC's address goes out through that NIC's gateway. No route
to add, no administrator. On Linux the Controller adds the route itself (wan_route.py)
and Linux sends by route, not by source address, so nothing here runs there.

Only after a connection fails: every local address is tried at once (1 s), the first
that connects is kept for that node address. A kept address that stops working is
forgotten, and the plain route is tried again. At most one search per node address every
``RETRY_S``.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

RETRY_S = 30.0
PROBE_TIMEOUT_S = 1.0

_lock = threading.Lock()
_chosen: dict[tuple[str, int], str] = {}
_searched_at: dict[tuple[str, int], float] = {}


def local_addresses(platform: str = sys.platform) -> list[str]:
    """This PC's IPv4 addresses (not loopback, not link-local); none off Windows."""
    if platform != "win32":
        return []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    found = dict.fromkeys(str(i[4][0]) for i in infos)
    return [a for a in found if not a.startswith(("127.", "169.254."))]


def _connects(source: str, host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), PROBE_TIMEOUT_S, (source, 0)):
            return True
    except OSError:
        return False


def source_for(host: str, port: int) -> str | None:
    with _lock:
        return _chosen.get((host, port))


def forget(host: str, port: int) -> None:
    with _lock:
        _chosen.pop((host, port), None)


def find(
    host: str,
    port: int,
    addresses: Callable[[], list[str]] | None = None,
    connects: Callable[[str, str, int], bool] = _connects,
    now: Callable[[], float] = time.monotonic,
) -> str | None:
    """The local address that reaches ``host:port``, or None (searched recently, one
    address only, or none connects)."""
    key = (host, port)
    with _lock:
        last = _searched_at.get(key)
        if last is not None and now() - last < RETRY_S:
            return None
        _searched_at[key] = now()
    tries = (addresses or local_addresses)()  # looked up now: tests stub it
    if len(tries) < 2:
        return None  # one NIC: the route has nothing to choose
    with ThreadPoolExecutor(len(tries)) as pool:
        ok = list(pool.map(lambda a: connects(a, host, port), tries))
    for address, good in zip(tries, ok, strict=True):
        if good:
            with _lock:
                _chosen[key] = address
            return address
    return None


def reset() -> None:
    """Tests: forget every choice and search."""
    with _lock:
        _chosen.clear()
        _searched_at.clear()
