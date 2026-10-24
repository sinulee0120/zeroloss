"""The Controller's test network card never carries the default route (docs/13-ai-loop.md 1,
docs/09-exceptions.md NET-019, 20 GAP-73 5).

One job: say which card the default route leaves by (Linux ``/proc/net/route``, lowest
metric wins), and, only when a person asks (the Action Required [Fix]), set that card's
NetworkManager connection so it never takes the default route or DNS again and apply it
without dropping the link. Windows comes with V18.
"""

from __future__ import annotations

import sys
from pathlib import Path

from core.blocks.wifi_join import Runner, run_command

ROUTE_FILE = Path("/proc/net/route")
NMCLI_TIMEOUT_S = 20.0
NEVER_DEFAULT = (
    "ipv4.never-default", "yes", "ipv4.ignore-auto-dns", "yes",
    "ipv6.never-default", "yes", "ipv6.ignore-auto-dns", "yes",
)  # fmt: skip


def default_route_nic(route_file: Path = ROUTE_FILE) -> str | None:
    """The card of the IPv4 default route with the lowest metric; None when there is
    none or it cannot be read (not Linux)."""
    try:
        lines = route_file.read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        return None
    best: tuple[int, str] | None = None
    for line in lines:
        f = line.split()
        if len(f) < 8 or f[1] != "00000000" or f[7] != "00000000":  # destination and mask 0
            continue
        try:
            flags, metric = int(f[3], 16), int(f[6])
        except ValueError:
            continue
        if flags & 0x1 and (best is None or metric < best[0]):  # RTF_UP
            best = (metric, f[0])
    return best[1] if best else None


def never_default(nic: str, run: Runner = run_command) -> tuple[bool, str]:
    """Make ``nic``'s NetworkManager connection leave the default route and DNS alone,
    then re-apply it (the link stays up). Returns (done, what happened)."""
    if not sys.platform.startswith("linux"):
        return False, "this comes with V18 on this system"
    rc, conn = run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", nic], NMCLI_TIMEOUT_S)
    conn = conn.strip()
    if rc != 0 or not conn:
        return False, f"no NetworkManager connection on {nic}: {conn[-200:]}"
    rc, out = run(["nmcli", "connection", "modify", conn, *NEVER_DEFAULT], NMCLI_TIMEOUT_S)
    if rc != 0:
        return False, f"cannot change {conn}: {out[-200:]}"
    rc, out = run(["nmcli", "device", "reapply", nic], NMCLI_TIMEOUT_S)
    if rc != 0:
        return False, f"changed {conn}, but it did not apply: {out[-200:]}"
    return True, f"{nic} ({conn}) no longer takes the default route or DNS"
