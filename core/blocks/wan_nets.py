"""The address ranges the sender PC can give the router's WAN side (docs/07-network.md 3.2 B).

2026-11-26 (a new router, user: "새 공유기 인식 못하는 버그잖아 / 앱이 다시 연결하게 해야지"):
the router used 10.0.0.x for something of its own (10.0.0.2, not on the WAN cable).
It took the sender's lease 10.0.0.100, but everything sent to 10.0.0.1 went to its own
10.0.0.x side ("10.0.0.2 reports: Destination host unreachable"), so nothing reached
the sender. The sender then moves to the next range here and the router asks again.
The first one is the documented default (07 3 table, B). No imports: the Windows
Controller and the Linux sender read the same list.
"""

from __future__ import annotations

import ipaddress

# (the sender's address with its prefix, the one address lent to the router WAN)
NETS: tuple[tuple[str, str], ...] = (
    ("10.0.0.1/24", "10.0.0.100"),  # 07 3 table, B (the default)
    ("100.64.0.1/24", "100.64.0.100"),  # RFC 6598, what real ISPs give a router WAN
    ("172.31.250.1/24", "172.31.250.100"),
)
FIXED_IP_SENDER = "10.0.0.2"  # 07 3 table, A: the fixed-IP way (router WAN 10.0.0.1)


def server_ips() -> tuple[str, ...]:
    """Every address the sender PC may have on the router WAN side."""
    return tuple(s.split("/")[0] for s, _ in NETS)


def net_of(ip: str) -> str | None:
    """The range (e.g. ``100.64.0.0/24``) ``ip`` is in, or None when it is in none."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for server, _ in NETS:
        net = ipaddress.ip_interface(server).network
        if addr in net:
            return str(net)
    return None


def index_of(server: str) -> int:
    """The place of ``server`` (with or without its prefix) in NETS; 0 when unknown."""
    bare = server.split("/")[0]
    return next((i for i, (s, _) in enumerate(NETS) if s.split("/")[0] == bare), 0)
