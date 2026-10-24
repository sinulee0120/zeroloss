"""Per-second counter deltas of one network card and of the host's UDP (02 7, GAP-148).

Linux only, plain file reads (all of them 0.2 ms on the T460, 2026-10-09; no process):
``/sys/class/net/<nic>/statistics`` and ``/proc/net/snmp``. A counter that cannot be
read gives no value: not measured is not 0. A counter that went down (driver restart,
card plugged in again) gives no value for that second either.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.blocks.m2u_check import is_wireless

SYS_NET = Path("/sys/class/net")
SNMP = Path("/proc/net/snmp")
NIC_FIELDS = ("rx_packets", "multicast", "rx_dropped", "rx_errors")
UDP_FIELDS = ("InErrors", "RcvbufErrors", "NoPorts")


def nic_counters(nic: str, sys_root: Path = SYS_NET) -> dict[str, int]:
    """The card's receive counters. ``multicast`` only for a wired card: Wi-Fi drivers
    (mac80211, rtl8814au) never count it, so their 0 is not a measurement."""
    fields = NIC_FIELDS if not is_wireless(nic, sys_root) else NIC_FIELDS[:1] + NIC_FIELDS[2:]
    out = {}
    for f in fields:
        try:
            out[f] = int((sys_root / nic / "statistics" / f).read_text())
        except (OSError, ValueError):
            continue
    return out


def udp_counters(path: Path = SNMP) -> dict[str, int]:
    """The host's UDP error counters (header line, then value line, both ``Udp:``)."""
    try:
        rows = [x.split()[1:] for x in path.read_text().splitlines() if x.startswith("Udp:")]
    except OSError:
        return {}
    if len(rows) < 2:
        return {}
    got = dict(zip(rows[0], rows[1], strict=False))
    out = {}
    for f in UDP_FIELDS:
        try:
            out[f] = int(got[f])
        except (KeyError, ValueError):
            continue
    return out


class Deltas:
    """What a counter reading grew by since the reading before. The first call has
    nothing to compare with, so it gives nothing."""

    def __init__(self, read: Callable[[], dict[str, int]]) -> None:
        self._read = read
        self._last: dict[str, int] | None = None

    def take(self) -> dict[str, int]:
        now = self._read()
        last, self._last = self._last, now
        if last is None:
            return {}
        return {k: v - last[k] for k, v in now.items() if k in last and v >= last[k]}
