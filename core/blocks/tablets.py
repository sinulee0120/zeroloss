"""The receiving PC's tablets (01-overview D3, 02 4 ``/v1/tablets``): what each one is,
and the ZeroLoss receiver app on it.

One to three tablets, each by its serial, on the same channel or on different ones. Each
gets its own adb forward to the app's API (127.0.0.1:10995 on the tablet), made when first
needed and made again once when it broke (a replugged cable drops it); nothing here ever
touches another tablet's forward. Only the ZeroLoss app is installed, started or stopped.
"""

from __future__ import annotations

import builtins
import contextlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from core.blocks.adb import Adb, AdbError, check_serial

APP = "com.zeroloss.receiver"
APP_ACTIVITY = f"{APP}/.MainActivity"
APP_PORT = 10995  # 02 1: the app's API inside the tablet
API_TIMEOUT_S = 5.0
# Apps that make the tablet scan for Wi-Fi on a timer: a scan takes the radio off the
# channel for seconds and the live multicast meanwhile is lost (2026-10-24: Samsung's
# Context Engine, every 1800 s, lost 0.6 s on an S11 and 2 s on an S10; the router was
# fine). Paused for the test with the person's OK (TAB-018), always put back after.
TIMER_SCANNERS = ("com.samsung.android.mcfds",)
JOIN_WAIT_S = 30.0  # 09 TAB-014: the SSID and address are checked for 30 s
SCAN_EVERY_S = 30.0  # a tablet is asked for a fresh Wi-Fi scan at most this often
WIFI_HELD = "wifi-held.json"  # what hold_wifi changed on each tablet, to put back
# cmd wifi set-network-selection-config <screen off> <screen on> -a <0 none, 2 off>: the
# sufficiency checks stay on, the selection while associated is off for the test
SELECTION = ["set-network-selection-config", "enabled", "enabled", "-a"]
# what a person must do for each adb state (shown as the tablet's "why")
STATE_FIX = {
    "unauthorized": "Allow USB debugging on the tablet (tick 'Always allow from this computer').",
    "no permissions": "Update PCs (the tablet USB rule), then unplug and plug the tablet.",
    "offline": "Unplug the tablet's USB cable and plug it in again.",
}


def parse_battery(out: str) -> dict[str, Any]:
    """``dumpsys battery``: level %, and whether a charger or USB gives power."""
    values = dict(
        (k.strip(), v.strip())
        for k, v in (line.split(":", 1) for line in out.splitlines() if ":" in line)
    )
    level = values.get("level", "")
    powered = [k.split()[0] for k in values if k.endswith("powered") and values[k] == "true"]
    return {
        "level": int(level) if level.isdigit() else None,
        "charging": values.get("status") == "2",  # BatteryManager.BATTERY_STATUS_CHARGING
        "powered_by": powered,  # e.g. ["USB"], ["AC"]; [] = on battery only
    }


def parse_ipv4(out: str) -> str | None:
    m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)/", out)
    return m.group(1) if m else None


def parse_ssid(out: str) -> str | None:
    """``cmd wifi status``: the network it is on, None when on none."""
    m = re.search(r'connected to "([^"]*)"', out)
    return m.group(1) if m else None


# cmd wifi list-networks: "  <id>  <SSID>  <security>" (the SSID quoted on some builds)
_SAVED_RE = re.compile(r'^\s*(\d+)\s+"?([^"\n]*?)"?\s{2,}', re.M)

# cmd wifi list-scan-results: "  <bssid>  <MHz>  <dBm>  <age>  <SSID with spaces>  [flags]"
_SCAN_RE = re.compile(
    r"^\s*([0-9a-fA-F:]{17})\s+(\d+)\s+(-?\d+)\s+\S+\s+(.*?)\s*((?:\[[^\]]*\])*)\s*$"
)


def parse_scan(out: str) -> list[dict[str, Any]]:
    """``cmd wifi list-scan-results``: each network once (its strongest AP), with the
    band and security the person picks by (S-51: Open / WPA2 / WPA3)."""
    best: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        m = _SCAN_RE.match(line)
        if not m or not m.group(4):
            continue
        freq, rssi, ssid, flags = int(m.group(2)), int(m.group(3)), m.group(4), m.group(5)
        if "SAE" in flags:
            security = "WPA3"
        elif "PSK" in flags:
            security = "WPA2"
        else:
            security = "other" if "EAP" in flags or "WEP" in flags else "Open"
        band = "2.4GHz" if freq < 3000 else "5GHz" if freq < 5925 else "6GHz"
        old = best.get(ssid)
        if old is None or rssi > old["rssi_dbm"]:
            best[ssid] = {"ssid": ssid, "security": security, "band": band, "rssi_dbm": rssi}
    return sorted(best.values(), key=lambda n: -int(n["rssi_dbm"]))


def parse_version(out: str) -> str | None:
    m = re.search(r"versionName=(\S+)", out)
    return m.group(1) if m else None


# 2026-10-24: the tablet's own receive counters, read through adb each second
# (an app cannot read /proc/net since Android 10; the shell can). A loss with one of them
# rising is the tablet's, not the router's.
NET_FILES = ("/proc/net/snmp", "/proc/net/udp", "/proc/net/udp6", "/proc/net/dev")
WIFI_CARD = "wlan0"


def parse_uids(out: str) -> set[int]:
    """``cmd package list packages -U <app>``: every uid of the app (a second user, like
    Secure Folder, has its own)."""
    return {int(u) for m in re.finditer(r"uid:([\d,]+)", out) for u in m.group(1).split(",")}


def parse_net_counters(out: str, port: int, uids: set[int]) -> dict[str, int]:
    """Cumulative counters from ``cat`` of NET_FILES: ``hd`` the app's own sockets on the
    channel's port (drops column, the PC's socket drops), ``nd``/``ne`` the Wi-Fi card's
    receive drops and errors, ``ue``/``ub`` the tablet-wide UDP InErrors/RcvbufErrors."""
    got: dict[str, int] = {}
    udp_head: list[str] | None = None
    want = f":{port:04X}"
    for line in out.splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "Udp:":
            if udp_head is None:
                udp_head = f
            elif len(f) == len(udp_head):
                row = dict(zip(udp_head[1:], f[1:], strict=True))
                with contextlib.suppress(KeyError, ValueError):
                    got["ue"], got["ub"] = int(row["InErrors"]), int(row["RcvbufErrors"])
        elif f[0].endswith(":") and f[0][:-1].isdigit() and len(f) >= 13:  # a socket line
            with contextlib.suppress(ValueError):
                if f[1].upper().endswith(want) and int(f[7]) in uids:
                    got["hd"] = got.get("hd", 0) + int(f[-1])
        elif line.strip().startswith(f"{WIFI_CARD}:"):
            nums = line.split(":", 1)[1].split()
            with contextlib.suppress(IndexError, ValueError):
                got["ne"], got["nd"] = int(nums[2]), int(nums[3])
    return got


# The Wi-Fi standard and receive rate the tablet is joined with (``cmd wifi status``, about
# 55 ms): the app sees neither the standard nor, without a location permission, the AP.
# The channel width is shown nowhere for the live link; it is known from the rate only when
# the rate is a top rate of exactly one width (2026-10-24, 11ax at 1201 Mbps = 80 MHz 2x2).
HE = (
    (143.4, 20),
    (286.8, 20),
    (286.8, 40),
    (573.5, 40),
    (600.5, 80),
    (1201.0, 80),
    (2402.0, 160),
)  # 1 and 2 streams, 0.8 us guard, top MCS; 286.8 is 2x20 or 1x40
TOP_MBPS = {"11ax": HE, "11be": HE,
            "11ac": ((86.7, 20), (173.3, 20), (200.0, 40), (400.0, 40), (433.3, 80),
                     (866.7, 80), (1733.3, 160))}  # fmt: skip


def parse_ipreach(out: str) -> str | None:
    """``cmd wifi get-ipreach-disconnect``: "enabled" / "disabled", None when not known.
    Android 17 says "IPREACH_DISCONNECT state is true" (receiverPC 2026-10-31); the setter
    takes enabled / disabled."""
    m = re.search(r"\b(enabled|disabled|true|false)\b", out.lower())
    if not m:
        return None
    return {"true": "enabled", "false": "disabled"}.get(m.group(1), m.group(1))


def parse_saved_security(out: str, ssid: str) -> str | None:
    """``cmd wifi list-networks``: how ``ssid`` is saved, as join_wifi names it ("Open"
    for open / owe, "WPA2", "WPA3"); None when it is not saved. An open network is two
    rows with one id (open, then owe); the first row says it."""
    for line in out.replace(chr(13), "").splitlines():
        m = _SAVED_RE.match(line + "  ")
        if not m or m.group(2).strip() != ssid:
            continue
        kind = line.split()[-1].lower()
        if kind in ("open", "owe"):
            return "Open"
        return "WPA3" if kind.startswith("wpa3") or kind == "sae" else "WPA2"
    return None


def parse_wifi_status(out: str) -> dict[str, Any]:
    """``cmd wifi status``: {"std": "11ax", "freq": 5745, "rx_mbps": 1201, "ssid": "TV"}
    (what it says; "ssid" None when on no network)."""
    got: dict[str, Any] = {"ssid": parse_ssid(out)}
    if m := re.search(r"Wi-Fi standard: (\w+)", out):
        got["std"] = m.group(1)
    if m := re.search(r"Frequency: (\d+)MHz", out):
        got["freq"] = int(m.group(1))
    if m := re.search(r"Rx Link speed: (\d+)Mbps", out):
        got["rx_mbps"] = int(m.group(1))
    return got


def width_from_rate(std: str | None, mbps: float | None) -> int | None:
    """The width a rate can only come from (a top rate of that width), else None."""
    if mbps is None:
        return None
    widths = {w for top, w in TOP_MBPS.get(std or "", ()) if abs(mbps - top) <= 1.0}
    return widths.pop() if len(widths) == 1 else None  # two widths fit: not known


USB_DEVICES = Path("/sys/bus/usb/devices")


def usb_adb_devices(root: Path = USB_DEVICES) -> int:
    """USB devices that offer an adb interface (class ff, subclass 42, protocol 01): what
    adb should list (receiverPC 2026-10-31: a tablet on a flaky cable or a dock was on USB
    and missing from adb, and nothing on the screen said so). 0 where there is no /sys."""
    found: set[str] = set()
    try:
        interfaces = [d for d in root.iterdir() if ":" in d.name]  # "<device>:<cfg>.<if>"
    except OSError:
        return 0
    try:
        for d in interfaces:
            try:
                cls = (d / "bInterfaceClass").read_text().strip()
                sub = (d / "bInterfaceSubClass").read_text().strip()
                proto = (d / "bInterfaceProtocol").read_text().strip()
            except OSError:
                continue
            if (cls, sub, proto) == ("ff", "42", "01"):
                found.add(d.name.split(":", 1)[0])
    except OSError:
        return 0
    return len(found)


class TabletError(Exception):
    def __init__(self, what: str, why: str, fix: str = "") -> None:
        super().__init__(f"{what}: {why}")
        self.what, self.why, self.fix = what, why, fix


class Tablets:
    def __init__(self, adb: Adb, apk: Path) -> None:
        self.adb = adb
        self.apk = apk  # the app this build carries (Update PCs puts it here)
        self._ports: dict[str, int] = {}
        self._scanned: dict[str, float] = {}  # serial -> when it was last asked to scan
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ reading

    def list(self) -> dict[str, Any]:
        try:
            found = self.adb.devices()
        except AdbError as e:
            return {"tablets": [], "adb": e.why}
        rows = []
        for t in found:
            row: dict[str, Any] = {**t.to_dict(), "why": STATE_FIX.get(t.state, "")}
            if t.state == "device":
                row.update(self._details(t.serial))
            rows.append(row)
        return {"tablets": rows, "adb": None,
                "usb_unseen": max(0, usb_adb_devices() - len(found))}  # fmt: skip

    def _details(self, serial: str) -> dict[str, Any]:
        d: dict[str, Any] = {"battery": None, "wifi_ip": None, "wifi_ssid": None,
                             "networks": [], "app": None, "api": None}  # fmt: skip
        try:
            d["battery"] = parse_battery(self.adb.shell(serial, ["dumpsys", "battery"]))
            # the name the maker gives it ("SM-X926B"); adb's list writes "SM_X926B"
            model = self.adb.shell(serial, ["getprop", "ro.product.model"]).strip()
            if model:
                d["model"] = model
            d["wifi_ip"] = self._ip(serial)
            d["wifi_ssid"] = parse_ssid(self.adb.shell(serial, ["cmd", "wifi", "status"]))
            # 2026-10-30 user ("scan눌러도 scanning으로 안바뀜"): a fresh scan now and then (the
            # list is read only between tests), the results come with the next read
            if time.monotonic() - self._scanned.get(serial, -SCAN_EVERY_S) >= SCAN_EVERY_S:
                self._scanned[serial] = time.monotonic()
                self.adb.shell(serial, ["cmd", "wifi", "start-scan"])
            scan = self.adb.shell(serial, ["cmd", "wifi", "list-scan-results"])
            d["networks"] = parse_scan(scan)  # what this tablet sees, to pick from (S-51)
            version = parse_version(self.adb.shell(serial, ["dumpsys", "package", APP]))
            d["app"] = {"installed": version is not None, "version": version}
        except AdbError as e:
            d["why"] = e.why
            return d
        if version is not None:
            try:
                d["api"] = self.call(serial, "GET", "/v1/status")
            except TabletError as e:
                d["api"] = None
                d["why"] = "" if "not answering" in e.why else e.why
        return d

    def _ip(self, serial: str) -> str | None:
        return parse_ipv4(self.adb.shell(serial, ["ip", "-f", "inet", "addr", "show", "wlan0"]))

    def saved_networks(self, serial: str) -> builtins.list[str]:
        """``cmd wifi list-networks``: the SSIDs the tablet keeps (to forget only ours)."""
        try:
            out = self.adb.shell(serial, ["cmd", "wifi", "list-networks"])
        except AdbError as e:
            raise TabletError("read the saved Wi-Fi networks", e.why) from e
        return [m.group(2) for m in _SAVED_RE.finditer(out)]

    def back_wifi(self, serial: str, ssid: str | None, added: builtins.list[str],
                  wait_s: float = JOIN_WAIT_S) -> dict[str, Any]:  # fmt: skip
        """After the throughput bands (2026-10-29): forget only the networks the test added,
        so the tablet joins its own saved network again by itself; back when it is on
        ``ssid`` with an address."""
        try:
            out = self.adb.shell(serial, ["cmd", "wifi", "list-networks"])
            # one saved network may be two rows with one id (open + owe, wpa2 + wpa3; the
            # second ends in a carriage return: receiverPC, Android 17): each id forgotten once
            ids = {m.group(1) for m in _SAVED_RE.finditer(out.replace(chr(13), ""))
                   if m.group(2).strip() in added and m.group(2).strip() != ssid}  # fmt: skip
            for nid in sorted(ids, key=int):
                self.adb.shell(serial, ["cmd", "wifi", "forget-network", nid])
            if ssid is None:  # it was on no network: ours forgotten is back as it was
                return {"back": True, "ssid": None, "ip": None}
            deadline = time.monotonic() + wait_s
            while True:
                on = parse_ssid(self.adb.shell(serial, ["cmd", "wifi", "status"]))
                ip = self._ip(serial)
                if on == ssid and ip:
                    return {"back": True, "ssid": on, "ip": ip}
                if time.monotonic() >= deadline:
                    return {"back": False, "why": f"on {on!r}, not {ssid!r}, after {wait_s:.0f} s"}
                time.sleep(1.0)
        except AdbError as e:
            return {"back": False, "why": e.why}

    def join_wifi(self, serial: str, ssid: str, security: str, password: str = "",
                  wait_s: float = JOIN_WAIT_S) -> dict[str, Any]:  # fmt: skip
        """S-51 / TAB-014: the tablet onto the test Wi-Fi by adb (Android 11 and later).
        Nothing to do when it is on that network already."""
        kind = {"Open": "open", "WPA2": "wpa2", "WPA3": "wpa3"}.get(security)
        try:
            if parse_ssid(self.adb.shell(serial, ["cmd", "wifi", "status"])) == ssid:
                return {"joined": False, "ssid": ssid, "ip": self._ip(serial)}
            if kind is None:
                raise TabletError("join the Wi-Fi", f"{security!r} cannot be joined by adb",
                                  "Join that network on the tablet by hand.")  # fmt: skip
            secret = [password] if kind != "open" else []
            # 2026-10-31 user ("임의 랜덤 안쓰게 해", "MAC을 등록해야 … SSID를 쓸수있어"): the
            # router's TV SSID admits registered MACs only; a random MAC got no answer to
            # its association (receiverPC: ASSOC-REJECT 1033). The tablet's own MAC, always
            self.adb.shell(serial, ["cmd", "wifi", "connect-network", ssid, kind, *secret,
                                    "-r", "none"])  # fmt: skip
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                on = parse_ssid(self.adb.shell(serial, ["cmd", "wifi", "status"]))
                ip = self._ip(serial)
                if on == ssid and ip:
                    return {"joined": True, "ssid": ssid, "ip": ip}
                time.sleep(1.0)
        except AdbError as e:
            raise TabletError("join the Wi-Fi", e.why, "Join it on the tablet by hand.") from e
        mac = self._mac(serial)
        if self._assoc_refused(serial):
            raise TabletError("join the Wi-Fi",
                              f"the access point of {ssid!r} did not answer the tablet's "
                              f"association (it admits listed devices only?)",
                              f"Register this tablet's MAC {mac or '(see its row)'} for "
                              f"{ssid!r} on the router, then press Start again.")  # fmt: skip
        raise TabletError("join the Wi-Fi", f"not on {ssid!r} after {wait_s:.0f} s",
                          "Check the password, or join it on the tablet by hand.")  # fmt: skip

    def _mac(self, serial: str) -> str | None:
        """The tablet's factory Wi-Fi MAC, the one it joins with (-r none) and the one to
        register on the router (receiverPC 2026-10-31: wlan0's address is often the random
        one of the network it is on; Android keeps the factory one in its Wi-Fi state)."""
        try:
            out = self.adb.shell(serial, ["dumpsys", "wifi"], 15.0)
        except AdbError:
            return None
        m = re.search(r"wifi_sta_factory_mac_address=([0-9a-fA-F:]{17})", out)
        return m.group(1).lower() if m else None

    def _assoc_refused(self, serial: str) -> bool:
        """The last minute of the tablet's log says the access point did not take it."""
        try:
            out = self.adb.shell(serial, ["logcat", "-d", "-t", "400"], 15.0)
        except AdbError:
            return False
        return "ASSOC-REJECT" in out

    # ------------------------------------------------------------------ the app's API

    def call(
        self, serial: str, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        """The app's answer (its ``data``); a broken forward is made again once."""
        check_serial(serial)
        for attempt in (1, 2):
            port = self._forward(serial, fresh=attempt == 2)
            try:
                return _http(port, method, path, params)
            except _NoAnswer as e:
                if attempt == 2:
                    raise TabletError("tablet app", f"not answering ({e})",
                                      "Press Start app for this tablet.") from e  # fmt: skip
        raise AssertionError("unreachable")

    def _forward(self, serial: str, fresh: bool) -> int:
        with self._lock:
            port = self._ports.get(serial)
            if port is not None and not fresh:
                return port
            if port is not None:
                with contextlib.suppress(AdbError):  # already gone with the cable
                    self.adb.unforward(serial, port)  # only this tablet's own forward
            elif not fresh:  # 2026-10-24: each restart of this service left one more
                port = self._adopt_forward(serial)
                if port is not None:
                    self._ports[serial] = port
                    return port
            try:
                port = self.adb.forward(serial, APP_PORT)
            except AdbError as e:
                raise TabletError("forward to the tablet", e.why) from e
            self._ports[serial] = port
            return port

    def _adopt_forward(self, serial: str) -> int | None:
        """A forward to this tablet's app left by an earlier run of this service: the
        first is used, the others removed (never another tablet's or another program's
        port: only this serial to the app's own port)."""
        try:
            mine = [p for sn, p, far in self.adb.forwards()
                    if sn == serial and far == f"tcp:{APP_PORT}"]  # fmt: skip
        except AdbError:
            return None
        for extra in mine[1:]:
            with contextlib.suppress(AdbError):
                self.adb.unforward(serial, extra)
        return mine[0] if mine else None

    # ------------------------------------------------------------------ timer scans (TAB-018)

    @property
    def _held_file(self) -> Path:
        return self.apk.parent / "scans-paused.json"  # what to put back, also after a crash

    def _held(self) -> dict[str, builtins.list[str]]:
        try:
            got = json.loads(self._held_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return (
            {str(k): [str(x) for x in v] for k, v in got.items()} if isinstance(got, dict) else {}
        )

    def _keep_held(self, held: dict[str, builtins.list[str]]) -> None:
        held = {k: v for k, v in held.items() if v}
        try:
            if held:
                self._held_file.parent.mkdir(parents=True, exist_ok=True)
                self._held_file.write_text(json.dumps(held), encoding="utf-8")
            else:
                self._held_file.unlink(missing_ok=True)
        except OSError:
            pass  # the put-back still runs at the end of this test

    def pause_timer_scans(self, serial: str) -> builtins.list[str]:
        """Pause the apps that scan on a timer (only those enabled now; a person's own
        choice to have one off stays theirs). Returns what was paused."""
        held = self._held()
        paused = list(held.get(serial, []))
        for pkg in TIMER_SCANNERS:
            try:
                enabled = self.adb.shell(serial, ["pm", "list", "packages", "-e", pkg])
                if f"package:{pkg}" not in enabled.split() or pkg in paused:
                    continue
                paused.append(pkg)  # recorded first: a crash in between still puts it back
                held[serial] = paused
                self._keep_held(held)
                said = self.adb.shell(serial, ["pm", "disable-user", "--user", "0", pkg])
                if "disabled-user" not in said:  # e.g. "Cannot disable a protected package"
                    paused.remove(pkg)
                    held[serial] = paused
                    self._keep_held(held)
                    raise AdbError("pm disable-user", said or "no answer")
            except AdbError as e:
                raise TabletError("pause the timer Wi-Fi scan", e.why,
                                  "The test goes on; that tablet may lose a few seconds "
                                  "every 30 minutes (TAB-018).") from e  # fmt: skip
        return paused

    def resume_timer_scans(self, serial: str | None = None) -> builtins.list[dict[str, Any]]:
        """Put back what this program paused: one tablet, or all (Reset All, a restart)."""
        held = self._held()
        out = []
        for sn in [serial] if serial else list(held):
            left = []
            for pkg in held.get(sn, []):
                try:
                    self.adb.shell(sn, ["pm", "enable", pkg])
                    out.append({"what": f"{sn} {pkg}", "ok": True, "detail": "put back"})
                except AdbError as e:
                    left.append(pkg)
                    out.append({"what": f"{sn} {pkg}", "ok": False, "detail": e.why})
            held[sn] = left
        self._keep_held(held)
        return out

    # ------------------------------------------------------------------ Wi-Fi held (TAB-019)

    def _wifi_held(self) -> dict[str, dict[str, Any]]:
        try:
            got = json.loads((self.apk.parent / WIFI_HELD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): v for k, v in got.items() if isinstance(v, dict)} if isinstance(
            got, dict) else {}  # fmt: skip

    def _keep_wifi_held(self, held: dict[str, dict[str, Any]]) -> None:
        f = self.apk.parent / WIFI_HELD
        held = {k: v for k, v in held.items() if v.get("ipreach") or v.get("selection")}
        try:
            if held:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(held), encoding="utf-8")
            else:
                f.unlink(missing_ok=True)
        except OSError:
            pass  # the put-back still runs at the end of this test

    def hold_wifi(self, serial: str, ssid: str) -> builtins.list[str]:
        """Keep the tablet on ``ssid`` for the test (2026-10-31 user: "설정에서 내가 일부러
        IPTV로 설정 바꿨으면 그대로 테스트동안 유지하도록 adb로 고쳐"). A TV network has no
        internet and its gateway may not answer pings: Android then drops it and joins
        another saved network (ipreach disconnect). That is turned off for the test,
        recorded on the PC first so the end, Reset All or a restart puts it back.
        Android also picks another saved network while on this one (network selection
        while associated): overridden off for the test (receiverPC 2026-10-31: the TV
        SSID was marked "no internet, permanently disabled", so autojoin never comes back
        to it by itself). Returns what was changed."""
        try:
            state = parse_ipreach(self.adb.shell(serial, ["cmd", "wifi",
                                                          "get-ipreach-disconnect"]))  # fmt: skip
        except AdbError as e:
            raise TabletError("read the Wi-Fi reachability setting", e.why) from e
        held = self._wifi_held()
        h: dict[str, Any] = {**held.get(serial, {}), "ssid": ssid}
        changed = []
        steps = [("ipreach", ["set-ipreach-disconnect", "disabled"], "ipreach disconnect off")]
        if state != "enabled":
            steps = []  # off already (or this Android has none): left as it is
        # no getter for it: put back to Android's own (checks on, no override)
        steps.append(("selection", [*SELECTION, "2"], "no switching to another saved network"))
        for key, args, said in steps:
            h[key] = True if key == "selection" else "enabled"
            held[serial] = h
            self._keep_wifi_held(held)  # recorded before it is changed
            try:
                self.adb.shell(serial, ["cmd", "wifi", *args])
            except AdbError as e:
                h.pop(key)
                self._keep_wifi_held(held)
                raise TabletError("keep the tablet on its Wi-Fi", e.why) from e
            changed.append(said)
        return changed

    def release_wifi(self, serial: str | None = None) -> builtins.list[dict[str, Any]]:
        """Put back what hold_wifi changed: one tablet, or all (Reset All, a restart)."""
        held = self._wifi_held()
        out = []
        for sn in [serial] if serial else list(held):
            h = held.get(sn) or {}
            for key, args, what in (
                ("ipreach", ["set-ipreach-disconnect", "enabled"], "ipreach disconnect"),
                ("selection", [*SELECTION, "0"], "network selection"),
            ):
                if not h.get(key):
                    continue
                try:
                    self.adb.shell(sn, ["cmd", "wifi", *args])
                    h.pop(key)
                    out.append({"what": f"{sn} {what}", "ok": True, "detail": "put back"})
                except AdbError as e:
                    out.append({"what": f"{sn} {what}", "ok": False, "detail": e.why})
        self._keep_wifi_held(held)
        return out

    def saved_security(self, serial: str, ssid: str) -> str | None:
        """How the tablet keeps ``ssid`` ("Open", "WPA2", "WPA3"; None: not saved)."""
        try:
            return parse_saved_security(self.adb.shell(serial, ["cmd", "wifi", "list-networks"]),
                                        ssid)  # fmt: skip
        except AdbError as e:
            raise TabletError("read the saved Wi-Fi networks", e.why) from e

    def rejoin(self, serial: str, ssid: str, security: str, password: str) -> dict[str, Any]:
        """Back onto the held network after Android moved the tablet off it."""
        return self.join_wifi(serial, ssid, security, password)

    def forget(self, serial: str) -> None:
        """Remove this tablet's forward (the test is over or the tablet went away)."""
        with self._lock:
            port = self._ports.pop(serial, None)
        if port is not None:
            with contextlib.suppress(AdbError):
                self.adb.unforward(serial, port)

    # ------------------------------------------------------------------ actions

    def app_uids(self, serial: str) -> set[int]:
        try:
            return parse_uids(self.adb.shell(serial, ["cmd", "package", "list", "packages",
                                                      "-U", APP]))  # fmt: skip
        except AdbError as e:
            raise TabletError("read the app's user id", e.why) from e

    def net_counters(self, serial: str, port: int, uids: set[int]) -> dict[str, int]:
        """One adb call (about 50 ms): see ``parse_net_counters``."""
        try:
            return parse_net_counters(self.adb.shell(serial, ["cat", *NET_FILES]), port, uids)
        except AdbError as e:
            raise TabletError("read the receive counters", e.why) from e

    def wifi_status(self, serial: str) -> dict[str, Any]:
        try:
            return parse_wifi_status(self.adb.shell(serial, ["cmd", "wifi", "status"]))
        except AdbError as e:
            raise TabletError("read the Wi-Fi status", e.why) from e

    def install_app(self, serial: str) -> dict[str, Any]:
        if not self.apk.is_file():
            raise TabletError("install the app", f"no app file at {self.apk}",
                              "Update PCs, so this PC has the tablet app.")  # fmt: skip
        try:
            out = self.adb.install(serial, str(self.apk))
        except AdbError as e:
            fix = "If the tablet shows a question, allow the install, then try again."
            raise TabletError("install the app", e.why, fix) from e
        return {"installed": True, "adb": out.splitlines()[-1] if out else ""}

    def screenshot(self, serial: str, out: Path) -> None:
        """What the tablet's screen shows now, as a PNG on this PC (F23 for tablets,
        2026-10-31 user: "태블릿 깨진거 스크린샷은 왜 없냐고"). Only its own temporary file
        on the tablet is written and removed."""
        remote = "/data/local/tmp/zeroloss-screen.png"
        try:
            self.adb.shell(serial, ["screencap", "-p", remote], 20.0)
            self.adb.pull(serial, remote, str(out))
        except AdbError as e:
            raise TabletError("take a screenshot", e.why) from e
        finally:
            with contextlib.suppress(AdbError):
                self.adb.shell(serial, ["rm", "-f", remote])

    def uninstall_app(self, serial: str) -> dict[str, Any]:
        """06 7 / 2026-10-31 (the remove buttons): the ZeroLoss app off the tablet, nothing
        else; a tablet without it is fine as it is."""
        try:
            if parse_version(self.adb.shell(serial, ["dumpsys", "package", APP])) is None:
                return {"removed": False, "why": "not on this tablet"}
            out = self.adb.uninstall(serial, APP)
        except AdbError as e:
            raise TabletError("remove the app", e.why) from e
        self.forget(serial)
        if "Success" not in out:
            raise TabletError("remove the app", out.strip() or "no answer")
        return {"removed": True}

    def ensure_app(self, serial: str) -> dict[str, Any]:
        """The app of this build on the tablet (Update PCs brought it with ``build.txt``
        beside it): installed when missing or another version, else left as it is."""
        try:
            want = (self.apk.parent / "build.txt").read_text(encoding="ascii").strip()
        except OSError:
            want = None
        try:
            have = parse_version(self.adb.shell(serial, ["dumpsys", "package", APP]))
        except AdbError as e:
            raise TabletError("read the app version", e.why) from e
        if have is not None and (want is None or have == want):
            return {"installed": False, "version": have}
        if want is None:  # no app file here: one on the tablet is used as it is
            raise TabletError("install the app", "this PC has no tablet app",
                              "Update PCs, so this PC has the tablet app.")  # fmt: skip
        self.install_app(serial)
        return {"installed": True, "version": want, "was": have}

    def start_app(self, serial: str) -> dict[str, Any]:
        """The app in front: its service then keeps the Wi-Fi awake (05 4.5)."""
        try:
            self.adb.shell(serial, ["am", "start", "-n", APP_ACTIVITY])
        except AdbError as e:
            raise TabletError("start the app", e.why, "Install the app first.") from e
        return {"started": True}

    def stop_app(self, serial: str) -> dict[str, Any]:
        self.forget(serial)
        try:
            self.adb.shell(serial, ["am", "force-stop", APP])
        except AdbError as e:
            raise TabletError("stop the app", e.why) from e
        return {"stopped": True}


class _NoAnswer(Exception):
    pass


def _http(port: int, method: str, path: str, params: dict[str, Any] | None) -> Any:
    body = json.dumps({"params": params or {}}).encode() if method == "POST" else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method,
                                 headers={"Content-Type": "application/json"})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as r:
            answer = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:  # the app said no: its own words, no retry
        try:
            err = json.loads(e.read() or b"{}").get("error", {})
        except ValueError:
            err = {}
        raise TabletError("tablet app", str(err.get("why") or err.get("what") or e),
                          str(err.get("fix") or "")) from e  # fmt: skip
    except (OSError, ValueError) as e:
        raise _NoAnswer(str(e)) from e
    if not answer.get("ok", False):
        err = answer.get("error") or {}
        raise TabletError("tablet app", str(err.get("why") or err), str(err.get("fix") or ""))
    return answer.get("data")
