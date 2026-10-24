"""A card's byte counters (received, sent), to show a rate every second while traffic runs.

Linux: ``/sys/class/net/<nic>/statistics``. Windows: ``GetIfEntry2`` (iphlpapi, 64-bit
counters; no program started). The card is named, or found by its IPv4 address. None
when the card or its counters cannot be read: the caller then shows no rate.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from core.blocks.link_monitor import nic_of
from core.blocks.win_links import adapters

SYS_NET = Path("/sys/class/net")


def bytes_of(card: str, sys_root: Path = SYS_NET) -> tuple[int, int] | None:
    """(received, sent) bytes of the card named ``card`` or holding that address."""
    if not card:
        return None
    if sys.platform == "win32":
        return _windows(card)
    nic = card if (sys_root / card).is_dir() else nic_of(card, sys_root)
    if not nic:
        return None
    try:
        stats = sys_root / nic / "statistics"
        return int((stats / "rx_bytes").read_text()), int((stats / "tx_bytes").read_text())
    except (OSError, ValueError):
        return None


def rate_mbps(before: tuple[int, int], after: tuple[int, int], seconds: float,
              received: bool) -> float | None:  # fmt: skip
    """Mbps between two readings, one direction; None for no time or a counter reset."""
    i = 0 if received else 1
    moved = after[i] - before[i]
    if seconds <= 0 or moved < 0:
        return None
    return moved * 8 / seconds / 1_000_000


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]


class _ROW2(ctypes.Structure):  # MIB_IF_ROW2 (netioapi.h) up to the octet counters
    _fields_ = [
        ("luid", ctypes.c_ulonglong), ("index", wintypes.ULONG), ("guid", _GUID),
        ("alias", ctypes.c_wchar * 257), ("description", ctypes.c_wchar * 257),
        ("phys_len", wintypes.ULONG), ("phys", ctypes.c_ubyte * 32),
        ("perm_phys", ctypes.c_ubyte * 32), ("mtu", wintypes.ULONG), ("type", wintypes.ULONG),
        ("tunnel_type", ctypes.c_int), ("media_type", ctypes.c_int),
        ("physical_medium_type", ctypes.c_int), ("access_type", ctypes.c_int),
        ("direction_type", ctypes.c_int), ("flags", ctypes.c_ubyte),
        ("oper_status", ctypes.c_int), ("admin_status", ctypes.c_int),
        ("media_connect_state", ctypes.c_int), ("network_guid", _GUID),
        ("connection_type", ctypes.c_int), ("tx_speed", ctypes.c_ulonglong),
        ("rx_speed", ctypes.c_ulonglong), ("in_octets", ctypes.c_ulonglong),
        ("in_ucast_pkts", ctypes.c_ulonglong), ("in_nucast_pkts", ctypes.c_ulonglong),
        ("in_discards", ctypes.c_ulonglong), ("in_errors", ctypes.c_ulonglong),
        ("in_unknown", ctypes.c_ulonglong), ("in_ucast_octets", ctypes.c_ulonglong),
        ("in_mcast_octets", ctypes.c_ulonglong), ("in_bcast_octets", ctypes.c_ulonglong),
        ("out_octets", ctypes.c_ulonglong), ("rest", ctypes.c_ulonglong * 16),
    ]  # fmt: skip


def _windows(card: str) -> tuple[int, int] | None:
    index = next((a.get("index") for a in adapters() if card in (a.get("nic"), a.get("ip"))), None)
    if not index:
        return None
    row = _ROW2()
    row.index = int(index)
    try:
        rc = ctypes.WinDLL("iphlpapi.dll").GetIfEntry2(ctypes.byref(row))  # type: ignore[attr-defined]
    except OSError:
        return None
    return (int(row.in_octets), int(row.out_octets)) if rc == 0 else None
