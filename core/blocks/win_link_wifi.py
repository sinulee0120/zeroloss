"""The Wi-Fi of each connected Windows card, every second (05 1.9, 02 5 ``links[].wifi``).

2026-11-28 (user "안만든거 다 만들어"; link_monitor said "Windows ... V16"): a Windows
wireless receiver PC recorded no RSSI, channel, band or BSSID, so a loss on it could not
be told from a channel move or a roam (NET-016/017) as on Ubuntu. The Wi-Fi API answers in
the process, no program started and nothing sent on the air: the current connection
(BSSID, link rates, standard), the channel and the RSSI of each connected card. The
frequency (for the band; 6 GHz channels reuse the numbers) comes from the card's own
cached list of access points, read again only when the BSSID changes. Width is not in
this API: None, as for drivers that do not say it on Ubuntu.

Cost, measured on the Controller PC: opening the API 8 ms, the current connection 12 ms,
channel and RSSI 0.1 ms each. So the handle stays open, channel and RSSI are read every
second and the connection (BSSID, rates) every ``FULL_S`` or when the channel changes --
the rule of Ubuntu's full ``iw`` reading (link_monitor ``WIFI_REFRESH_S``).
"""

from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import replace
from typing import Any

from core.blocks.link_monitor import Wifi, band_of
from core.blocks.win_wifi import _Bss, _BssList, _Conn, _IfInfo, _IfList

CURRENT_CONNECTION, CHANNEL_NUMBER, RSSI = 7, 8, 0x10000102  # WLAN_INTF_OPCODE
CONNECTED = 1  # wlan_interface_state_connected
FULL_S = 10.0


def _mac(raw: Any) -> str:
    return ":".join(f"{b:02x}" for b in raw)  # lower case, as iw says it on Ubuntu


class LinkWifi:
    """``read(names)`` -> {adapter name: Wifi} for the connected cards. ``names`` maps the
    Wi-Fi API's card description to the adapter list's name (win_links)."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._handle: Any = None
        self._freq: dict[str, int | None] = {}  # bssid -> MHz, from the cached AP list
        self._full: dict[str, tuple[float, Wifi]] = {}  # card -> (read at, full reading)

    def _api(self) -> tuple[Any, Any] | None:
        api = getattr(ctypes, "windll").wlanapi  # Windows only  # noqa: B009
        if self._handle is None:
            handle, ver = ctypes.c_void_p(), ctypes.c_ulong()
            if api.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)) != 0:
                return None
            self._handle = handle
        return api, self._handle

    def close(self) -> None:
        if self._handle is not None and sys.platform == "win32":
            getattr(ctypes, "windll").wlanapi.WlanCloseHandle(self._handle, None)  # noqa: B009
        self._handle = None

    def read(self, names: dict[str, str]) -> dict[str, Wifi]:
        if sys.platform != "win32":
            return {}
        got = self._api()
        if got is None:
            return {}
        api, handle = got
        out: dict[str, Wifi] = {}
        ifs = ctypes.POINTER(_IfList)()
        if api.WlanEnumInterfaces(handle, None, ctypes.byref(ifs)) != 0:
            self.close()  # the Wi-Fi service restarted: a new handle next second
            return {}
        try:
            n = ifs.contents.n
            items = ctypes.cast(ctypes.byref(ifs.contents.items),
                                ctypes.POINTER(_IfInfo * n)).contents  # fmt: skip
            for it in items:
                if it.state != CONNECTED:
                    self._full.pop(it.desc, None)
                    continue
                w = self._card(api, handle, it)
                if w is not None:
                    out[names.get(it.desc, it.desc)] = w
        finally:
            api.WlanFreeMemory(ifs)
        return out

    def _card(self, api: Any, handle: Any, it: Any) -> Wifi | None:
        chan = _query(api, handle, it, CHANNEL_NUMBER, ctypes.c_ulong)
        rssi = _query(api, handle, it, RSSI, ctypes.c_long)
        channel = int(chan.value) if chan else None
        now = self._clock()
        seen = self._full.get(it.desc)
        if seen is None or now - seen[0] >= FULL_S or seen[1].channel != channel:
            full = self._connection(api, handle, it, channel)
            if full is None:
                self._full.pop(it.desc, None)
                return None
            self._full[it.desc] = seen = (now, full)
        return replace(seen[1], channel=channel,
                       rssi_dbm=float(rssi.value) if rssi is not None else None)  # fmt: skip

    def _connection(self, api: Any, handle: Any, it: Any, channel: int | None) -> Wifi | None:
        conn = _query(api, handle, it, CURRENT_CONNECTION, _Conn)
        if conn is None:
            return None
        a = conn.assoc
        bssid = _mac(a.bssid)
        if bssid not in self._freq:
            self._freq = {bssid: self._freq_of(api, handle, it, bssid)}  # one AP at a time
        freq = self._freq[bssid]
        return Wifi(bssid=bssid, band=band_of(freq), channel=channel, freq_mhz=freq,
                    rx_mbps=a.rx / 1000 or None, tx_mbps=a.tx / 1000 or None)  # fmt: skip

    @staticmethod
    def _freq_of(api: Any, handle: Any, it: Any, bssid: str) -> int | None:
        """The connected AP's centre frequency from the card's cached list (no scan)."""
        bl = ctypes.POINTER(_BssList)()
        if api.WlanGetNetworkBssList(handle, ctypes.byref(it.guid), None, 3, False, None,
                                     ctypes.byref(bl)) != 0:  # fmt: skip
            return None
        try:
            m = bl.contents.n
            entries = ctypes.cast(ctypes.byref(bl.contents.items),
                                  ctypes.POINTER(_Bss * m)).contents  # fmt: skip
            for e in entries:
                if _mac(e.bssid) == bssid:
                    return int(e.freq_khz // 1000) or None
            return None
        finally:
            api.WlanFreeMemory(bl)


def _query(api: Any, handle: Any, it: Any, opcode: int, kind: Any) -> Any:
    """One WlanQueryInterface value copied out, or None."""
    size, data = ctypes.c_ulong(), ctypes.c_void_p()
    if api.WlanQueryInterface(handle, ctypes.byref(it.guid), opcode, None, ctypes.byref(size),
                              ctypes.byref(data), None) != 0 or not data:  # fmt: skip
        return None
    try:
        if size.value < ctypes.sizeof(kind):
            return None
        out = kind()
        ctypes.memmove(ctypes.byref(out), data, ctypes.sizeof(kind))
        return out
    finally:
        api.WlanFreeMemory(data)
