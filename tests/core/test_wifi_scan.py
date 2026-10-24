"""What a Wi-Fi card sees, for the person to pick per band (core/blocks/wifi_join scan).

2026-10-17 user: "무선은 open으로 패스워드 없이 ... 2G/5G/6G중에 SSID클릭해주면 되잖아",
"와이파이도 카드 뭐로 할건지 사용자가 정할거야 넌 앱만 띄워".
"""

from __future__ import annotations

from core.blocks.wifi_join import WifiJoin, band_of

NMCLI = "\n".join(
    [
        r"ROUTER-TEST:02\:00\:5E\:10\:00\:2D:5745 MHz:100:",
        r"ROUTER-TEST:02\:00\:5E\:10\:00\:2C:2437 MHz:88:",
        r"ROUTER-TEST:02\:00\:5E\:10\:00\:2E:6115 MHz:61:--",
        r"home\:net:02\:00\:5E\:21\:00\:50:5180 MHz:70:WPA2",
        r":AA\:BB\:CC\:DD\:EE\:FF:2412 MHz:20:",
    ]
)  # nmcli -t: a colon inside a value comes as "\:"


def test_each_network_with_its_band_and_access_point() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], t: float) -> tuple[int, str]:
        calls.append(argv)
        return 0, NMCLI

    w = WifiJoin(run=run)
    w_supported = WifiJoin.supported
    try:
        WifiJoin.supported = property(lambda self: True)  # type: ignore[method-assign,assignment]
        got = w.scan("wlx02005e000001")
    finally:
        WifiJoin.supported = w_supported  # type: ignore[method-assign]
    assert [(n["band"], n["ssid"], n["bssid"], n["security"]) for n in got] == [
        ("2.4GHz", "ROUTER-TEST", "02:00:5E:10:00:2C", "Open"),
        ("5GHz", "ROUTER-TEST", "02:00:5E:10:00:2D", "Open"),
        ("5GHz", "home:net", "02:00:5E:21:00:50", "WPA2"),
        ("6GHz", "ROUTER-TEST", "02:00:5E:10:00:2E", "Open"),
    ]  # the hidden one (no name) is not offered
    assert calls[0][-2:] == ["ifname", "wlx02005e000001"] and "--rescan" in calls[0]
    assert (band_of(2412), band_of(5180), band_of(5955), band_of(0)) == ("2.4GHz", "5GHz",
                                                                        "6GHz", "")  # fmt: skip
