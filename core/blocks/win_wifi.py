"""This Windows PC's Wi-Fi card: what it sees, and joining one open network for a while.

The same contract as ``wifi_join.WifiJoin`` (Linux, NetworkManager) for a Windows PC:
``supported``, ``nics``, ``scan``, ``join``, ``connection_of``, ``back``. The scan asks
the card to look again (wlanapi ``WlanScan``) and reads every access point it sees
(``WlanGetNetworkBssList``: SSID, BSSID, centre frequency, signal, privacy bit). A join
adds a temporary profile for an open network (no password) and connects by its name;
Windows cannot be told one access point, so when a router uses one name on all bands
Windows picks the band. ``back`` reconnects the profile it was on and removes ours.

2026-10-17 user: "controller의 wifi는 비활성화로 되지도않고", "우선 기능 구현만 해놔 나중에
선택할 수 있게 리눅스 PC에서 하듯이".
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.blocks.wifi_join import JOIN_TIMEOUT_S, PROFILE, band_of
from core.protocol.envelope import ZlError

CMD_TIMEOUT_S = 15.0
SCAN_WAIT_S = 4.0  # WlanScan returns at once; the list fills within a few seconds

Run = Callable[[list[str], float], tuple[int, str]]


def _run(argv: list[str], timeout_s: float) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout_s,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout + p.stderr)


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                ("d4", ctypes.c_ubyte * 8)]  # fmt: skip


class _IfInfo(ctypes.Structure):
    _fields_ = [("guid", _GUID), ("desc", ctypes.c_wchar * 256), ("state", ctypes.c_int)]


class _IfList(ctypes.Structure):
    _fields_ = [("n", ctypes.c_ulong), ("index", ctypes.c_ulong), ("items", _IfInfo * 1)]


class _Ssid(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ulong), ("ssid", ctypes.c_ubyte * 32)]


class _Rates(ctypes.Structure):
    _fields_ = [("n", ctypes.c_ulong), ("rates", ctypes.c_ushort * 126)]


class _Bss(ctypes.Structure):
    _fields_ = [("ssid", _Ssid), ("phy_id", ctypes.c_ulong), ("bssid", ctypes.c_ubyte * 6),
                ("bss_type", ctypes.c_int), ("phy_type", ctypes.c_int), ("rssi", ctypes.c_long),
                ("quality", ctypes.c_ulong), ("in_reg", ctypes.c_ubyte),
                ("beacon", ctypes.c_ushort), ("timestamp", ctypes.c_ulonglong),
                ("host_ts", ctypes.c_ulonglong), ("capability", ctypes.c_ushort),
                ("freq_khz", ctypes.c_ulong), ("rates", _Rates),
                ("ie_offset", ctypes.c_ulong), ("ie_size", ctypes.c_ulong)]  # fmt: skip


class _BssList(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("n", ctypes.c_ulong), ("items", _Bss * 1)]


def _wlan_scan() -> list[dict[str, Any]]:
    """Every access point the PC's Wi-Fi cards see now (wlanapi; no rights needed)."""
    api = getattr(ctypes, "windll").wlanapi  # Windows only  # noqa: B009
    handle, ver = ctypes.c_void_p(), ctypes.c_ulong()
    if api.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)) != 0:
        raise OSError("the Windows Wi-Fi service does not answer (WlanOpenHandle)")
    found: list[dict[str, Any]] = []
    try:
        ifs = ctypes.POINTER(_IfList)()
        if api.WlanEnumInterfaces(handle, None, ctypes.byref(ifs)) != 0:
            raise OSError("no Wi-Fi card (WlanEnumInterfaces)")
        n = ifs.contents.n
        items = ctypes.cast(ctypes.byref(ifs.contents.items), ctypes.POINTER(_IfInfo * n)).contents
        guids = [it.guid for it in items]
        for g in guids:
            api.WlanScan(handle, ctypes.byref(g), None, None, None)
        time.sleep(SCAN_WAIT_S)
        for g in guids:
            bl = ctypes.POINTER(_BssList)()
            if api.WlanGetNetworkBssList(handle, ctypes.byref(g), None, 3, False, None,
                                         ctypes.byref(bl)) != 0:  # fmt: skip
                continue
            m = bl.contents.n
            entries = ctypes.cast(ctypes.byref(bl.contents.items),
                                  ctypes.POINTER(_Bss * m)).contents  # fmt: skip
            for e in entries:
                ssid = bytes(e.ssid.ssid[: e.ssid.len]).decode("utf-8", "replace")
                mhz = int(e.freq_khz // 1000)
                found.append({
                    "ssid": ssid, "bssid": ":".join(f"{b:02X}" for b in e.bssid),
                    "freq_mhz": mhz, "band": band_of(mhz), "signal": int(e.quality),
                    "security": "Open" if not (e.capability & 0x10) else "Protected",
                })  # fmt: skip
            api.WlanFreeMemory(bl)
        api.WlanFreeMemory(ifs)
    finally:
        api.WlanCloseHandle(handle, None)
    return found


class _Assoc(ctypes.Structure):
    _fields_ = [("ssid", _Ssid), ("bss_type", ctypes.c_int), ("bssid", ctypes.c_ubyte * 6),
                ("phy_type", ctypes.c_int), ("phy_index", ctypes.c_ulong),
                ("quality", ctypes.c_ulong), ("rx", ctypes.c_ulong),
                ("tx", ctypes.c_ulong)]  # fmt: skip


class _Conn(ctypes.Structure):
    _fields_ = [("state", ctypes.c_int), ("mode", ctypes.c_int),
                ("profile", ctypes.c_wchar * 256), ("assoc", _Assoc)]  # fmt: skip


def _wlan_cards() -> list[dict[str, Any]]:
    """Each Wi-Fi card: its name (as the adapter list calls it), connected or not, the
    SSID and profile it is on -- from the Wi-Fi API, not netsh's text (it answers in the
    PC's language: 2026-10-17 "이름 · 상태: 연결됨" on this PC)."""
    from core.blocks.win_links import adapters

    names = {str(a.get("description") or ""): str(a.get("nic")) for a in adapters()}
    api = getattr(ctypes, "windll").wlanapi  # Windows only  # noqa: B009
    handle, ver = ctypes.c_void_p(), ctypes.c_ulong()
    if api.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)) != 0:
        return []
    out: list[dict[str, Any]] = []
    try:
        ifs = ctypes.POINTER(_IfList)()
        if api.WlanEnumInterfaces(handle, None, ctypes.byref(ifs)) != 0:
            return []
        n = ifs.contents.n
        items = ctypes.cast(ctypes.byref(ifs.contents.items), ctypes.POINTER(_IfInfo * n)).contents
        for it in items:
            card: dict[str, Any] = {"nic": names.get(it.desc, it.desc), "state": "disconnected"}
            size, data = ctypes.c_ulong(), ctypes.c_void_p()
            if it.state == 1 and api.WlanQueryInterface(  # 1 = connected; 7 = current connection
                handle, ctypes.byref(it.guid), 7, None, ctypes.byref(size), ctypes.byref(data),
                None,
            ) == 0:  # fmt: skip
                c = ctypes.cast(data, ctypes.POINTER(_Conn)).contents
                ss = c.assoc.ssid
                card.update(state="connected", profile=c.profile,
                            ssid=bytes(ss.ssid[: ss.len]).decode("utf-8", "replace"))  # fmt: skip
                api.WlanFreeMemory(data)
            out.append(card)
        api.WlanFreeMemory(ifs)
    finally:
        api.WlanCloseHandle(handle, None)
    return out


PROFILE_XML = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{name}</name>
  <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security><authEncryption><authentication>{auth}</authentication>
    <encryption>{enc}</encryption><useOneX>false</useOneX></authEncryption>{key}</security></MSM>
</WLANProfile>
"""
KEY_XML = ("<sharedKey><keyType>passPhrase</keyType><protected>false</protected>"
           "<keyMaterial>{key}</keyMaterial></sharedKey>")  # fmt: skip
AUTH = {"Open": ("open", "none"), "WPA2": ("WPA2PSK", "AES"), "WPA3": ("WPA3SAE", "AES")}


def _xml(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))  # fmt: skip


class WinWifi:
    """``WifiJoin`` for Windows (netsh for profiles, wlanapi for the scan)."""

    def __init__(self, run: Run = _run, scan: Callable[[], list[dict[str, Any]]] = _wlan_scan,
                 ip_of: Callable[[str], str | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 cards: Callable[[], list[dict[str, Any]]] = _wlan_cards) -> None:  # fmt: skip
        self.run, self._scan, self.sleep, self._cards = run, scan, sleep, cards
        self.ip_of = ip_of or _ip_of

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def _interfaces(self) -> list[dict[str, Any]]:
        return self._cards() if self.supported else []

    def nics(self) -> list[Any]:
        from core.blocks.wifi_join import WifiNic

        return [WifiNic(c["nic"], None, c.get("ssid"), self.ip_of(c["nic"]))
                for c in self._interfaces()]  # fmt: skip

    def status(self) -> dict[str, Any]:
        return {"supported": self.supported, "nics": [n.to_dict() for n in self.nics()],
                "rfkill": [], "radio": None}  # fmt: skip

    def scan(self, nic: str) -> list[dict[str, Any]]:
        """What the PC's Wi-Fi sees (one card on a laptop: ``nic`` is that one)."""
        found = [n for n in self._scan() if n.get("ssid")]
        return sorted(found, key=lambda x: (x["band"], -x["signal"]))

    def connection_of(self, nic: str) -> str | None:
        c = next((x for x in self._interfaces() if x["nic"] == nic), {})
        return c.get("profile") if c.get("state") == "connected" else None

    def join(
        self, ssid: str, security: str, password: str, nic: str | None = None,
        timeout_s: float = JOIN_TIMEOUT_S, bssid: str | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """A network by name, open or with its password (``bssid`` cannot be given to
        Windows; see the top). The profile file holds the key for the moment it is added
        and is removed with its folder at once."""
        if security not in AUTH:
            raise ZlError("RCV-023", "Unknown Wi-Fi security", str(security))
        if security != "Open" and not password:
            raise ZlError("RCV-023", f"No password for Wi-Fi {ssid!r}",
                          "Type it in Devices, next to its band.")  # fmt: skip
        card = nic or next((c["nic"] for c in self._interfaces()), "")
        auth, enc = AUTH[security]
        key = "" if security == "Open" else KEY_XML.format(key=_xml(password))
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "zl.xml"
            xml.write_text(PROFILE_XML.format(name=PROFILE, ssid=_xml(ssid), auth=auth, enc=enc,
                                              key=key), encoding="utf-8")  # fmt: skip
            rc, out = self.run(["netsh", "wlan", "add", "profile", f"filename={xml}",
                                f"interface={card}", "user=current"], CMD_TIMEOUT_S)  # fmt: skip
        if rc != 0:
            raise ZlError("RCV-023", f"Cannot make the Wi-Fi profile for {ssid!r}", out.strip())
        rc, out = self.run(["netsh", "wlan", "connect", f"name={PROFILE}", f"ssid={ssid}",
                            f"interface={card}"], CMD_TIMEOUT_S)  # fmt: skip
        if rc != 0:
            self._drop()
            raise ZlError("RCV-023", f"Cannot connect to Wi-Fi {ssid!r}", out.strip())
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            now = next((c for c in self._interfaces() if c["nic"] == card), {})
            ip = self.ip_of(card)
            if now.get("state") == "connected" and now.get("ssid") == ssid and ip:
                return {"nic": card, "ssid": ssid, "ip": ip, "joined": True}
            self.sleep(1.0)
        self._drop()
        raise ZlError("RCV-023", f"No address on Wi-Fi {ssid!r} in {timeout_s:g} s",
                      "Connected, but the router gave no address.")  # fmt: skip

    def back(self, nic: str, connection: str | None,
             timeout_s: float = JOIN_TIMEOUT_S) -> dict[str, Any]:  # fmt: skip
        self._drop()
        if not connection or connection == PROFILE:
            return {"nic": nic, "back": False, "why": "it was on no network before"}
        rc, out = self.run(["netsh", "wlan", "connect", f"name={connection}", f"interface={nic}"],
                           CMD_TIMEOUT_S)  # fmt: skip
        if rc != 0:
            return {"nic": nic, "back": False, "why": out.strip()[-200:]}
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            ip = self.ip_of(nic)
            now = next((c for c in self._interfaces() if c["nic"] == nic), {})
            if ip and now.get("state") == "connected":
                return {"nic": nic, "back": True, "ip": ip}
            self.sleep(1.0)
        return {"nic": nic, "back": False, "why": "no address after going back"}

    def _drop(self) -> None:
        self.run(["netsh", "wlan", "delete", "profile", f"name={PROFILE}"], CMD_TIMEOUT_S)


def _ip_of(nic: str) -> str | None:
    """The card's IPv4 address (the node's own reading of its cards)."""
    from core.blocks.win_links import adapters

    for a in adapters():
        if a.get("nic") == nic and a.get("ip"):
            return str(a["ip"])
    return None
