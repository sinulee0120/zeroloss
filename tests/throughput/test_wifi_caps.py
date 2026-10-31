"""The Wi-Fi settings a measuring PC's card offers per OS (throughput.md 1.2), from made-up
driver and ``iw`` output in the shape real cards give (2026-10-29 user: "a/b/g/n/ac/be/bn
설정 변경은 안되나?", "연결된 컴퓨터들에 따라 윈도우,리눅스별로 해야할거야")."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.blocks import wifi_caps as lin
from core.blocks import win_wifi_caps as win

# an Intel AX201 as Get-NetAdapterAdvancedProperty gives it (Korean names, as on the PC)
INTEL = [
    {"key": "ChannelWidth24", "value": "1", "values": ["0", "1"], "names": ["20 MHz", "자동"]},
    {"key": "ChannelWidth52", "value": "1", "values": ["0", "1"], "names": ["20 MHz", "자동"]},
    {"key": "IEEE11nMode", "value": "3", "values": ["0", "1", "2", "3"],
     "names": ["1. 비활성화함", "2. 802.11n", "3. 802.11ac", "4. 802.11ax"]},
    {"key": "WirelessMode", "value": "34", "values": ["17", "0", "16", "32", "18", "34"],
     "names": ["1. 5GHz 802.11a", "2. 2.4GHz 802.11b", "3. 2.4GHz 802.11g",
               "4. 2.4GHz 802.11b/g", "5. 듀얼 밴드 802.11a/g", "6. 듀얼 밴드 802.11a/b/g"]},
    {"key": "*PacketCoalescing", "value": "1", "values": ["0", "1"], "names": ["off", "on"]},
]  # fmt: skip


def labels(opts: list[dict[str, Any]], band: str, fits: Any) -> list[str]:
    return [o["label"] for o in opts if fits(o, band)]


def test_windows_offers_the_cards_own_widths_modes_and_abg_per_band() -> None:
    opts = win.options(INTEL)
    assert labels(opts, "5GHz", win.fits) == [
        "width capped at 20 MHz", "mode capped at 802.11n", "mode capped at 802.11ac",
        "802.11a only"]  # fmt: skip
    assert labels(opts, "2.4GHz", win.fits) == [
        "width capped at 20 MHz",
        "mode capped at 802.11n",
        "802.11b only",
        "802.11g only",
    ]
    g = next(o for o in opts if o["label"] == "802.11g only")
    assert g["set"] == [("IEEE11nMode", "0"), ("WirelessMode", "16")]  # 11n/ac/ax off, then g
    be = [*INTEL[:2], {"key": "IEEE11nMode", "value": "4", "values": ["1", "2", "3", "4"],
                       "names": ["802.11n", "802.11ac", "802.11ax", "802.11be"]}]  # fmt: skip
    assert "mode capped at 802.11ax" in labels(win.options(be), "5GHz", win.fits)


def test_windows_checks_what_the_card_joined_with() -> None:
    mode = {"axis": "mode", "mode": "802.11ac"}
    assert win.mismatch(mode, {"mode": "802.11ac"}) is None
    assert win.mismatch(mode, {"mode": "802.11ax"}) == "it joined with 802.11ax, not 802.11ac"
    width = {"axis": "width", "mhz": 20}
    assert win.mismatch(width, {"rx_mbps": 286.8}) is None
    assert "more than a 20 MHz link can" in str(win.mismatch(width, {"rx_mbps": 1201.0}))
    assert (
        win.joined(
            [
                {
                    "nic": "Wi-Fi",
                    "state": "connected",
                    "mode": "802.11ax",
                    "channel": 36,
                    "rx_mbps": 1201.0,
                }
            ],
            "Wi-Fi",
        )["band"]
        == "5GHz"
    )


class Helper:
    def __init__(self) -> None:
        self.applied: list[tuple[str, str]] = []

    def _apply(self, nic: str, value: str, keyword: str = "") -> str | None:
        self.applied.append((keyword, value))
        return None


def test_windows_sets_one_case_and_puts_every_value_back(tmp_path: Path) -> None:
    props = {p["key"]: dict(p) for p in INTEL}
    helper = Helper()

    def read(nic: str) -> list[dict[str, Any]]:
        for k, v in helper.applied:
            props[k]["value"] = v
        return list(props.values())

    caps = win.WinWifiCaps(tmp_path / "caps.json", helper, read,  # type: ignore[arg-type]
                           cards=lambda: [{"nic": "Wi-Fi", "state": "connected", "channel": 1,
                                           "mode": "802.11g", "rx_mbps": 54.0}],
                           sleep=lambda s: None)  # fmt: skip
    g = next(o for o in win.options(INTEL) if o["label"] == "802.11g only")
    got = caps.set_option("Wi-Fi", g)
    assert got["readback_ok"] and got["link"] and "why" not in got and got["band"] == "2.4GHz"
    assert (tmp_path / "caps.json").exists()  # a restart would put it back
    assert caps.restore("Wi-Fi")["ok"]
    assert (props["IEEE11nMode"]["value"], props["WirelessMode"]["value"]) == ("3", "34")
    assert not (tmp_path / "caps.json").exists()


IW_PHY = """Wiphy phy0
\tBand 1:
\t\tCapabilities: 0x9ff
\t\t\tHT20/HT40
\t\tHE Iftypes: managed
\tBand 2:
\t\tVHT Capabilities (0x0391f9b2):
"""


def test_linux_offers_what_the_card_can_per_band() -> None:
    caps = lin.phy_caps(IW_PHY)
    assert caps == {"ht40", "vht", "he"}
    assert [o["label"] for o in lin.options(caps, "5GHz")] == [
        "width capped at 20 MHz", "mode capped at 802.11n", "mode capped at 802.11ac",
        "802.11a only"]  # fmt: skip
    assert [o["label"] for o in lin.options(caps, "2.4GHz")] == [
        "width capped at 20 MHz",
        "mode capped at 802.11n",
        "802.11g only",
    ]
    ac = next(o for o in lin.options(caps, "5GHz") if o.get("mode") == "802.11ac")
    assert ac["set"] == ["disable_he=1", "disable_eht=1"]


def test_linux_reads_width_and_standard_from_iw_link() -> None:
    he = (
        "Connected to 02:00:5e:00:00:01 (on wlx58)\n\tSSID: ZL_TEST\n\tfreq: 5745.0\n"
        "\trx bitrate: 1201.0 MBit/s 80MHz HE-MCS 11 HE-NSS 2 HE-GI 0 HE-DCM 0\n"
    )
    assert lin.link_now(he) == {"connected": True, "band": "5GHz", "mode": "802.11ax",
                                "rx_mbps": 1201.0, "width_mhz": 80}  # fmt: skip
    ht20 = "Connected to x\n\tfreq: 2437\n\trx bitrate: 144.4 MBit/s MCS 15 short GI\n"
    assert lin.link_now(ht20)["mode"] == "802.11n" and lin.link_now(ht20)["width_mhz"] == 20
    legacy = "Connected to x\n\tfreq: 2412\n\trx bitrate: 54.0 MBit/s\n"
    assert lin.link_now(legacy)["mode"] is None  # a bare rate names no standard
    assert lin.mismatch({"axis": "mode", "mode": "802.11g"}, lin.link_now(legacy)) is None
    # 2026-10-29, the T460's RTL8814AU: no rx line, a bare tx rate -> checked by the rate
    bare = "Connected to x (on wlx58)\n\tfreq: 5745.0\n\ttx bitrate: 867.0 MBit/s\n"
    got = lin.link_now(bare)
    assert (got["rx_mbps"], got["mode"], got["width_mhz"]) == (867.0, None, None)
    assert lin.mismatch({"axis": "width", "mhz": 20}, got) == (
        "it moves 867 Mbps, more than a 20 MHz link can")  # fmt: skip
    assert lin.mismatch({"axis": "mode", "mode": "802.11n"}, got) == (
        "it moves 867 Mbps, more than 802.11n can")  # fmt: skip
    assert lin.mismatch({"axis": "width", "mhz": 20}, {**got, "rx_mbps": 173.3}) is None
    assert lin.link_now("Not connected.") == {"connected": False}
    assert lin.mismatch({"axis": "width", "mhz": 20}, {"width_mhz": 80}) == (
        "it joined at 80 MHz, not 20 MHz or less")  # fmt: skip


def test_linux_case_joins_with_the_options_and_hands_the_card_back(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], timeout: float) -> tuple[int, str]:
        calls.append(argv)
        if argv[:3] == ["iw", "dev", "wlx58"] and argv[3] == "link":
            return 0, "Connected to x\n\tfreq: 5745\n\trx bitrate: 286.7 MBit/s 20MHz HE-MCS 11\n"
        return 0, ""

    caps = lin.WifiCaps(tmp_path, run, binary="true", sleep=lambda s: None)
    opt = {"axis": "width", "mhz": 20, "set": ["disable_ht40=1"], "label": "w"}
    net = {"ssid": "ZL_TEST_5G", "bssid": "02:00:5e:00:00:01", "security": "WPA2",
           "password": "secret", "ip": "192.168.45.88"}  # fmt: skip
    got = caps.set_option("wlx58", opt, net)
    assert got["readback_ok"] and got["link"] and "why" not in got and got["width_mhz"] == 20
    assert ["nmcli", "device", "set", "wlx58", "managed", "no"] in calls
    assert ["ip", "addr", "replace", "192.168.45.88/24", "dev", "wlx58"] in calls
    assert not (tmp_path / "wpa-case.conf").exists()  # the password is not left on disk
    assert not any("secret" in " ".join(c) for c in calls)  # never on a command line
    assert caps.restore("wlx58")["ok"]
    assert calls[-1] == ["nmcli", "device", "set", "wlx58", "managed", "yes"]
    assert caps.restore_all() == {}
