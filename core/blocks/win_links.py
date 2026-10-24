"""The PC's network cards on Windows (link_monitor's ``read_links`` on Linux reads /sys).

2026-10-16 (throughput: "the Controller PC also works as a LAN-side PC, so every LAN
port is checked"): the Controller's own node had no cards on Windows, so it could not be
a measured card. ``GetAdaptersAddresses`` (iphlpapi, no program started): physical
Ethernet and Wi-Fi adapters with their IPv4 address, up/down and link speed. Wi-Fi
details (signal, band) are not read here; the card is marked wireless.
"""

from __future__ import annotations

import ctypes
import socket
import sys
from ctypes import wintypes
from typing import Any

IF_TYPE_ETHERNET = 6
IF_TYPE_WIFI = 71
OPER_UP = 1
AF_INET = 2
GAA_FLAG_SKIP = 0x0002 | 0x0004 | 0x0008  # anycast, multicast, DNS servers
ERROR_BUFFER_OVERFLOW = 111
VIRTUAL = ("Virtual", "Bluetooth", "Hyper-V", "VPN", "TAP-", "Loopback", "WAN Miniport")


class SOCKADDR(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("data", ctypes.c_ubyte * 14)]


class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("sockaddr", ctypes.POINTER(SOCKADDR)), ("length", ctypes.c_int)]


class UNICAST(ctypes.Structure):
    pass


UNICAST._fields_ = [  # IP_ADAPTER_UNICAST_ADDRESS_LH (the part read here)
    ("length", ctypes.c_ulong), ("flags", wintypes.DWORD),
    ("next", ctypes.POINTER(UNICAST)), ("address", SOCKET_ADDRESS),
]  # fmt: skip


class ADAPTER(ctypes.Structure):
    pass


ADAPTER._fields_ = [  # IP_ADAPTER_ADDRESSES_LH up to the link speeds
    ("length", ctypes.c_ulong), ("if_index", wintypes.DWORD),
    ("next", ctypes.POINTER(ADAPTER)), ("adapter_name", ctypes.c_char_p),
    ("first_unicast", ctypes.POINTER(UNICAST)), ("first_anycast", ctypes.c_void_p),
    ("first_multicast", ctypes.c_void_p), ("first_dns", ctypes.c_void_p),
    ("dns_suffix", ctypes.c_wchar_p), ("description", ctypes.c_wchar_p),
    ("friendly_name", ctypes.c_wchar_p), ("physical_address", ctypes.c_ubyte * 8),
    ("physical_address_length", wintypes.DWORD), ("flags", wintypes.DWORD),
    ("mtu", wintypes.DWORD), ("if_type", wintypes.DWORD), ("oper_status", ctypes.c_int),
    ("ipv6_if_index", wintypes.DWORD), ("zone_indices", wintypes.DWORD * 16),
    ("first_prefix", ctypes.c_void_p), ("transmit_link_speed", ctypes.c_ulonglong),
    ("receive_link_speed", ctypes.c_ulonglong),
]  # fmt: skip


def _ipv4(first: Any) -> str | None:
    node = first
    while node:
        sa = node.contents.address.sockaddr
        if sa and sa.contents.family == AF_INET:
            ip = socket.inet_ntoa(bytes(sa.contents.data[2:6]))
            if not ip.startswith("169.254."):
                return ip
        node = node.contents.next
    return None


def adapters() -> list[dict[str, Any]]:
    """Ethernet and Wi-Fi adapters: name, ip, up, link Mbps, wireless. [] off Windows."""
    if sys.platform != "win32":
        return []
    gaa = ctypes.WinDLL("iphlpapi.dll").GetAdaptersAddresses
    size = wintypes.ULONG(16 * 1024)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        rc = gaa(AF_INET, GAA_FLAG_SKIP, None, buf, ctypes.byref(size))
        if rc != ERROR_BUFFER_OVERFLOW:
            break
    if rc != 0:
        return []
    out = []
    node = ctypes.cast(buf, ctypes.POINTER(ADAPTER))
    while node:
        a = node.contents
        virtual = any(w in (a.description or "") for w in VIRTUAL)
        if a.if_type in (IF_TYPE_ETHERNET, IF_TYPE_WIFI) and not virtual:
            up = a.oper_status == OPER_UP
            speed = int(a.transmit_link_speed // 1_000_000) if up else None
            out.append({"nic": a.friendly_name, "ip": _ipv4(a.first_unicast), "up": up,
                        "link_mbps": speed or None, "wireless": a.if_type == IF_TYPE_WIFI,
                        "description": a.description, "index": int(a.if_index)})  # fmt: skip
        node = a.next
    return out
