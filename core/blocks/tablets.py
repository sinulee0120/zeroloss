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


class TabletError(Exception):
    def __init__(self, what: str, why: str, fix: str = "") -> None:
        super().__init__(f"{what}: {why}")
        self.what, self.why, self.fix = what, why, fix


class Tablets:
    def __init__(self, adb: Adb, apk: Path) -> None:
        self.adb = adb
        self.apk = apk  # the app this build carries (Update PCs puts it here)
        self._ports: dict[str, int] = {}
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
        return {"tablets": rows, "adb": None}

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
            self.adb.shell(serial, ["cmd", "wifi", "connect-network", ssid, kind, *secret])
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                on = parse_ssid(self.adb.shell(serial, ["cmd", "wifi", "status"]))
                ip = self._ip(serial)
                if on == ssid and ip:
                    return {"joined": True, "ssid": ssid, "ip": ip}
                time.sleep(1.0)
        except AdbError as e:
            raise TabletError("join the Wi-Fi", e.why, "Join it on the tablet by hand.") from e
        raise TabletError("join the Wi-Fi", f"not on {ssid!r} after {wait_s:.0f} s",
                          "Check the password, or join it on the tablet by hand.")  # fmt: skip

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

    def forget(self, serial: str) -> None:
        """Remove this tablet's forward (the test is over or the tablet went away)."""
        with self._lock:
            port = self._ports.pop(serial, None)
        if port is not None:
            with contextlib.suppress(AdbError):
                self.adb.unforward(serial, port)

    # ------------------------------------------------------------------ actions

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
