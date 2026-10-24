"""core/blocks/tablets.py with a fake adb and a fake tablet app (a local HTTP server)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from core.blocks.adb import Adb
from core.blocks.tablets import TabletError, Tablets, parse_battery, parse_ipv4, parse_version

BATTERY = """Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  status: 2
  level: 23
"""


@pytest.fixture
def app() -> Iterator[tuple[int, list[dict[str, Any]]]]:
    """A tablet app answering like the real one (02 envelope)."""
    seen: list[dict[str, Any]] = []

    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._send(200, {"ok": True, "data": {"state": "IDLE"}})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            if not body["params"].get("group"):
                self._send(400, {"ok": False, "error": {"why": "params.group is required"}})
            else:
                self._send(200, {"ok": True, "data": {"receiving": True}})

        def log_message(self, *a: Any) -> None:
            pass

    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_address[1], seen
    s.shutdown()


class FakeAdb:
    def __init__(self, port: int, devices: str) -> None:
        self.port, self.devices = port, devices
        self.calls: list[str] = []
        self.forwards = 0

    def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        cmd = " ".join(argv[1:])
        self.calls.append(cmd)
        if cmd == "devices -l":
            return 0, self.devices
        if "forward tcp:0" in cmd:
            self.forwards += 1
            return 0, f"{self.port}\n"
        if cmd.endswith("dumpsys battery"):
            return 0, BATTERY
        if cmd.endswith("getprop ro.product.model"):
            return 0, "SM-X930"
        if cmd.endswith("addr show wlan0"):
            return (
                0,
                "3: wlan0: <UP>\n    inet 192.168.45.77/24 brd 192.168.45.255 scope global wlan0\n",
            )
        if cmd.endswith("dumpsys package com.zeroloss.receiver"):
            return (
                0,
                "Packages:\n  Package [com.zeroloss.receiver]\n    versionName=20261023.1333\n",
            )
        return 0, ""


def test_battery_wifi_and_version_are_read() -> None:
    assert parse_battery(BATTERY) == {"level": 23, "charging": True, "powered_by": ["USB"]}
    assert parse_ipv4("    inet 10.1.2.3/24 brd") == "10.1.2.3"
    assert parse_ipv4("") is None
    assert parse_version("    versionName=20261023.1333\n") == "20261023.1333"


def test_list_shows_each_tablet_and_what_to_do(app: tuple[int, list[Any]], tmp_path: Path) -> None:
    port, _ = app
    fake = FakeAdb(
        port, "List of devices attached\nA1 device usb:2-1 model:SM_X930\nB2 unauthorized usb:2-2\n"
    )
    rows = Tablets(Adb(lambda: "adb", fake), tmp_path / "x.apk").list()["tablets"]
    assert rows[0]["serial"] == "A1" and rows[0]["wifi_ip"] == "192.168.45.77"
    assert rows[0]["battery"]["level"] == 23 and rows[0]["app"]["version"] == "20261023.1333"
    assert rows[0]["model"] == "SM-X930"  # the maker's name, not adb's "SM_X930"
    assert rows[0]["api"] == {"state": "IDLE"}
    assert rows[1]["state"] == "unauthorized" and "Allow USB debugging" in rows[1]["why"]


def test_one_forward_per_tablet_kept(app: tuple[int, list[Any]], tmp_path: Path) -> None:
    port, seen = app
    fake = FakeAdb(port, "")
    t = Tablets(Adb(lambda: "adb", fake), tmp_path / "x.apk")
    t.call("A1", "POST", "/v1/receiver/start", {"group": "239.1.1.1", "ch": 1})
    t.call("A1", "GET", "/v1/status")
    assert fake.forwards == 1 and seen[0]["params"]["ch"] == 1


def test_the_apps_own_words_come_back(app: tuple[int, list[Any]], tmp_path: Path) -> None:
    port, _ = app
    t = Tablets(Adb(lambda: "adb", FakeAdb(port, "")), tmp_path / "x.apk")
    with pytest.raises(TabletError, match="group is required"):
        t.call("A1", "POST", "/v1/receiver/start", {})


def test_a_broken_forward_is_made_again_once(tmp_path: Path) -> None:
    fake = FakeAdb(1, "")  # nothing listens on port 1
    t = Tablets(Adb(lambda: "adb", fake), tmp_path / "x.apk")
    with pytest.raises(TabletError, match="not answering"):
        t.call("A1", "GET", "/v1/status")
    assert fake.forwards == 2
    assert any("forward --remove tcp:1" in c for c in fake.calls)


def test_install_without_the_app_file_says_update_pcs(tmp_path: Path) -> None:
    t = Tablets(Adb(lambda: "adb", FakeAdb(1, "")), tmp_path / "missing.apk")
    with pytest.raises(TabletError) as e:
        t.install_app("A1")
    assert "Update PCs" in e.value.fix


SCAN = """  BSSID  Frequency  RSSI  Age(sec)  SSID  Flags
  0a:00:5e:10:00:2d  5745  -38  2.513  ZL_WIFI_IPTV_TEST 1  [WPA2-PSK-CCMP][RSN-PSK-CCMP][ESS]
  0a:00:5e:10:00:2e  2437  -41  2.620  ZL_TEST_2.4G  [ESS]
  02:00:5e:10:00:2d  5745  -52  2.511  ZL_WIFI_IPTV_TEST 1  [WPA2-PSK-CCMP][ESS]
  02:00:5e:21:00:50  5180  -70  3.100  NbHome 5G  [RSN-SAE-CCMP][ESS]
"""


def test_scan_and_status_are_read() -> None:
    from core.blocks.tablets import parse_scan, parse_ssid

    nets = {n["ssid"]: n for n in parse_scan(SCAN)}
    assert nets["ZL_WIFI_IPTV_TEST 1"] == {"ssid": "ZL_WIFI_IPTV_TEST 1", "security": "WPA2",
                                           "band": "5GHz", "rssi_dbm": -38}  # fmt: skip
    assert nets["ZL_TEST_2.4G"]["security"] == "Open" and nets["ZL_TEST_2.4G"]["band"] == "2.4GHz"
    assert nets["NbHome 5G"]["security"] == "WPA3"
    assert parse_ssid('Wifi is connected to "ZL_TEST_5G"\nsomething') == "ZL_TEST_5G"
    assert parse_ssid("Wifi is not connected") is None


def test_join_asks_adb_and_waits_for_the_address(tmp_path: Path) -> None:
    state = {"on": "Wifi is not connected"}

    class JoinAdb(FakeAdb):
        def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
            cmd = " ".join(argv[1:])
            if "connect-network" in cmd:
                self.calls.append(cmd)
                state["on"] = 'Wifi is connected to "Lab 5G"'
                return 0, ""
            if cmd.endswith("cmd wifi status"):
                return 0, state["on"]
            return super().__call__(argv, timeout)

    fake = JoinAdb(1, "")
    t = Tablets(Adb(lambda: "adb", fake), tmp_path / "x.apk")
    got = t.join_wifi("A1", "Lab 5G", "WPA2", "secret12")
    assert got == {"joined": True, "ssid": "Lab 5G", "ip": "192.168.45.77"}
    assert "-s A1 shell cmd wifi connect-network Lab 5G wpa2 secret12" in fake.calls
    assert t.join_wifi("A1", "Lab 5G", "WPA2", "secret12")["joined"] is False  # on it already


def test_the_app_of_this_build_is_installed_only_when_it_differs(tmp_path: Path) -> None:
    apk = tmp_path / "zeroloss-receiver.apk"
    apk.write_bytes(b"apk")
    (tmp_path / "build.txt").write_text("20261023.1333", encoding="ascii")
    fake = FakeAdb(1, "")
    t = Tablets(Adb(lambda: "adb", fake), apk)
    assert t.ensure_app("A1") == {"installed": False, "version": "20261023.1333"}
    (tmp_path / "build.txt").write_text("20261024.0900", encoding="ascii")
    got = t.ensure_app("A1")
    assert got["installed"] and got["was"] == "20261023.1333"
    assert any(c.startswith("-s A1 install -r -g") for c in fake.calls)


# real output, 2026-10-23: SM-X930 (Android 16) and SM-X926B (Android 15)
REAL_SCAN = (
    "      BSSID              Frequency      RSSI           Age(sec)     SSID      Flags\n"
    "    02:00:5e:30:00:7c       2417        -65            >1000.0    NbHome    "
    "[WPA2-PSK-CCMP-128][RSN-PSK-CCMP-128][ESS][WPS]\n"
    "    02:00:5e:31:00:7c       5745        -79            >1000.0    NbHome 5G    "
    "[WPA2-PSK-CCMP-128][RSN-PSK-CCMP-128][ESS][WPS]\n"
    "    02:00:5e:40:00:43       2412        -63            407.473    NB_0000_2.4G    "
    "[WPA2-PSK-CCMP-128][RSN-PSK-CCMP-128][ESS][WPS][MFPC]\n"
    "    02:00:5e:10:00:2d       5745        -32            >1000.0    ZL_WIFI_IPTV_TEST    [ESS]\n"
)
REAL_STATUS_15 = (
    "Wifi is enabled\nWifi scanning is only available when wifi is enabled\n"
    "==== ClientModeManager instance: ConcreteClientModeManager{id=8031146833 iface=wlan0 "
    'role=ROLE_CLIENT_PRIMARY} ====\nWifi is connected to "HOMEWIFI_000000_2.4G"\n'
)


def test_the_real_tablet_outputs_are_read() -> None:
    from core.blocks.tablets import parse_scan, parse_ssid

    nets = {n["ssid"]: n for n in parse_scan(REAL_SCAN)}
    assert set(nets) == {"NbHome", "NbHome 5G", "NB_0000_2.4G", "ZL_WIFI_IPTV_TEST"}
    assert nets["ZL_WIFI_IPTV_TEST"]["security"] == "Open" and nets["NbHome 5G"]["band"] == "5GHz"
    assert nets["NB_0000_2.4G"]["security"] == "WPA2"
    assert parse_ssid(REAL_STATUS_15) == "HOMEWIFI_000000_2.4G"


def test_timer_scans_are_paused_kept_and_put_back(tmp_path: Path) -> None:
    """TAB-018 (2026-10-24): Samsung's Context Engine scanned every 1800 s; paused for
    the test, recorded on the PC (a crash still puts it back), enabled again after."""

    class ScanAdb(FakeAdb):
        def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
            cmd = " ".join(argv[1:])
            if "pm list packages -e" in cmd:
                return 0, "package:com.samsung.android.mcfds\n"
            if " pm " in f" {cmd} ":
                self.calls.append(cmd)
                ok = "Package com.samsung.android.mcfds new state: disabled-user"
                return 0, ok if "disable-user" in cmd else ""
            return super().__call__(argv, timeout)

    fake = ScanAdb(1, "")
    t = Tablets(Adb(lambda: "adb", fake), tmp_path / "x.apk")
    assert t.pause_timer_scans("A1") == ["com.samsung.android.mcfds"]
    assert "-s A1 shell pm disable-user --user 0 com.samsung.android.mcfds" in fake.calls
    assert (tmp_path / "scans-paused.json").is_file()  # what to put back, kept on disk
    again = Tablets(Adb(lambda: "adb", fake), tmp_path / "x.apk")  # after a restart
    assert [r["ok"] for r in again.resume_timer_scans()] == [True]
    assert "-s A1 shell pm enable com.samsung.android.mcfds" in fake.calls
    assert not (tmp_path / "scans-paused.json").exists()


def test_a_refused_pause_is_not_recorded_as_paused(tmp_path: Path) -> None:
    class Refuse(FakeAdb):
        def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
            cmd = " ".join(argv[1:])
            if "pm list packages -e" in cmd:
                return 0, "package:com.samsung.android.mcfds"
            if "disable-user" in cmd:
                return 0, "Error: java.lang.SecurityException: Cannot disable a protected package"
            return super().__call__(argv, timeout)

    t = Tablets(Adb(lambda: "adb", Refuse(1, "")), tmp_path / "x.apk")
    with pytest.raises(TabletError, match="protected"):
        t.pause_timer_scans("A1")
    assert not (tmp_path / "scans-paused.json").exists()  # nothing to put back


def test_a_forward_left_by_an_earlier_run_is_used_and_extras_removed(tmp_path: Path) -> None:
    class Left(FakeAdb):
        def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
            cmd = " ".join(argv[1:])
            if cmd == "forward --list":
                rows = ["A1 tcp:41000 tcp:10995", "A1 tcp:41001 tcp:10995"]
                return 0, "\n".join([*rows, "B2 tcp:41002 tcp:10995"])
            return super().__call__(argv, timeout)

    fake = Left(1, "")
    t = Tablets(Adb(lambda: "adb", fake), tmp_path / "x.apk")
    assert t._forward("A1", fresh=False) == 41000
    assert fake.forwards == 0  # no new one
    assert "-s A1 forward --remove tcp:41001" in fake.calls
    assert not any("tcp:41002" in c for c in fake.calls)  # another tablet's stays
