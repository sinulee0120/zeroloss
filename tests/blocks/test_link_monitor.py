"""Link monitor (05 1.8, NET-013/014/015): reads sysfs, reports speed drops, down/up and
fast flaps. Uses a fake sysfs tree."""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path

from core.blocks.link_monitor import Link, LinkWatch, Wifi, band_of, parse_iw, read_links


def nic(
    root: Path,
    name: str,
    speed: str,
    oper: str = "up",
    carrier: int = 1,
    duplex: str = "full",
    wireless: bool = False,
    physical: bool = True,
) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if physical:
        (d / "device").mkdir(exist_ok=True)
    if wireless:
        (d / "wireless").mkdir(exist_ok=True)
    (d / "speed").write_text(speed + "\n")
    (d / "duplex").write_text(duplex + "\n")
    (d / "operstate").write_text(oper + "\n")
    (d / "carrier_changes").write_text(f"{carrier}\n")


WIFI = Wifi(bssid="02:00:00:00:00:01", band="5GHz", channel=36, width_mhz=80)


def read(root: Path, _proc: Path | None = None, wifi: Wifi | None = WIFI) -> list[Link]:
    return read_links(root, ip_of=lambda n: "10.0.0.2", wifi_of=lambda n: wifi)


# Shape of real `iw` output (checked on this PC, 2026-09-24); names and addresses made up.
IW_LINK = """Connected to 02:00:00:00:00:01 (on wlan0)
\tSSID: test-ap
\tfreq: 5180.0
\tsignal: -58 dBm
\trx bitrate: 130.0 MBit/s VHT-MCS 1 80MHz short GI VHT-NSS 2
\ttx bitrate: 390.0 MBit/s VHT-MCS 4 80MHz short GI VHT-NSS 2
\tdtim period: 1
\tbeacon int: 100
"""
IW_INFO = "\ttype managed\n\tchannel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz\n"


def test_reads_wired_wireless_and_skips_virtual(tmp_path: Path) -> None:
    root, proc = tmp_path / "net", tmp_path / "wireless"
    nic(root, "eth0", "2500")
    nic(root, "eth1", "-1", oper="down")
    nic(root, "wlp4s0", "0", wireless=True)
    nic(root, "lo", "0", physical=False)
    links = {x.nic: x for x in read(root, proc)}
    assert set(links) == {"eth0", "eth1", "wlp4s0"}
    assert links["eth0"].link_mbps == 2500 and links["eth0"].duplex == "full"
    assert not links["eth1"].up and links["eth1"].link_mbps is None
    assert links["wlp4s0"].wifi == WIFI and links["eth0"].wifi is None
    assert "wifi" not in links["eth0"].to_dict()
    wl = links["wlp4s0"].to_dict()
    assert "link_mbps" not in wl and wl["wifi"]["channel"] == 36


def test_parse_iw() -> None:
    w = parse_iw(IW_LINK, IW_INFO)
    assert w == Wifi("02:00:00:00:00:01", "5GHz", 36, 80, 5210, 5180, -58.0, 130.0, 390.0, 1)
    assert parse_iw("Not connected.", "") is None
    no_width = parse_iw(IW_LINK, "\tchannel 36 (5180 MHz)\n")
    assert no_width is not None and no_width.width_mhz == 80  # from the bitrate line
    assert [band_of(f) for f in (2412, 5180, 5955, 900, None)] == [
        "2.4GHz",
        "5GHz",
        "6GHz",
        None,
        None,
    ]


def test_wifi_channel_width_band_and_roam(tmp_path: Path) -> None:
    root = tmp_path / "net"
    nic(root, "wlan0", "0", wireless=True)
    w = LinkWatch()
    w.update(read(root, wifi=WIFI))
    cases = [
        (Wifi("02:00:00:00:00:01", "5GHz", 149, 80), ["wifi"]),  # router moved channel
        (Wifi("02:00:00:00:00:01", "5GHz", 149, 40), ["wifi"]),  # width shrank
        (Wifi("02:00:00:00:00:02", "2.4GHz", 6, 20), ["roam", "wifi"]),  # band steering
        (Wifi("02:00:00:00:00:02", "2.4GHz", 6, 20, rssi_dbm=-70.0), []),  # signal only
    ]
    for wifi, want in cases:
        assert [c.kind for c in w.update(read(root, wifi=wifi))] == want


def test_channel_change_across_a_drop_is_reported(tmp_path: Path) -> None:
    """2026-10-09 wireless run: off the air for 9 s, back on another channel (153 -> 40).
    The readings in between had no Wi-Fi values; the change still counts."""
    root = tmp_path / "net"
    nic(root, "wlan0", "0", wireless=True)
    w = LinkWatch()
    w.update(read(root, wifi=Wifi("02:00:00:00:00:01", "5GHz", 153, 80)))
    assert w.update(read(root, wifi=None)) == []
    back = w.update(read(root, wifi=Wifi("02:00:00:00:00:01", "5GHz", 40, 80)))
    assert [c.kind for c in back] == ["wifi"]
    assert back[0].before is not None and back[0].before.wifi is not None
    assert back[0].before.wifi.channel == 153


def test_real_cases_are_reported(tmp_path: Path) -> None:
    """2.5 G -> 1 G, 1 G -> 100 M, -> 10 M, down, up, and a flap within one second."""
    root, proc = tmp_path / "net", tmp_path / "w"
    w = LinkWatch()
    nic(root, "eth0", "2500")
    assert w.update(read(root, proc)) == []  # first reading is the baseline
    steps = [
        ("1000", "up", 1, ["speed"]),
        ("100", "up", 1, ["speed"]),
        ("10", "up", 1, ["speed"]),
        ("-1", "down", 2, ["down"]),
        ("1000", "up", 3, ["up"]),
        ("1000", "up", 5, ["flap"]),
        ("1000", "up", 5, []),
    ]
    for speed, oper, carrier, want in steps:
        nic(root, "eth0", speed, oper=oper, carrier=carrier)
        got = [c.kind for c in w.update(read(root, proc))]
        assert got == want, (speed, oper, got)


def test_random_readings_never_crash_and_report_every_speed_change(tmp_path: Path) -> None:
    rng = random.Random(1924)
    root, proc = tmp_path / "net", tmp_path / "w"
    w = LinkWatch()
    last: Link | None = None
    for _ in range(500):
        nic(
            root,
            "eth0",
            rng.choice(["10", "100", "1000", "2500", "-1", "junk"]),
            oper=rng.choice(["up", "up", "down"]),
            carrier=rng.randint(0, 9),
        )
        now = read(root, proc)[0]
        kinds = [c.kind for c in w.update([now])]
        if last and last.up and now.up and last.link_mbps != now.link_mbps:
            assert "speed" in kinds
        last = now


class FakeEvents:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.running = True

    def new_lines(self) -> list[str]:
        out, self.lines = self.lines, []
        return out


def test_wifi_reader_reads_fully_only_on_events_or_every_10_s() -> None:
    from core.blocks.link_monitor import WifiReader

    t = [0.0]
    ev = FakeEvents()
    reads: list[str] = []

    def full(nic: str) -> Wifi:
        reads.append(nic)
        return WIFI

    r = WifiReader(ev, full_read=full, rssi_read=lambda: {"wlan0": -61.0}, clock=lambda: t[0])  # type: ignore[arg-type]
    for _ in range(9):  # 9 seconds, no events: one full read, RSSI every second
        w = r("wlan0")
        assert w is not None and w.rssi_dbm == -61.0
        t[0] += 1
    assert len(reads) == 1
    ev.lines = ["1791532199.563301: wlan0 (phy #0): ch_switch_started_notify freq 5745"]
    r("wlan0")
    assert len(reads) == 2  # the event forced a fresh reading at once (`iw event -t` line)
    ev.lines = ["1791532199.753592: wlan1 (phy #1): connected to 02:00:00:00:00:09"]
    r("wlan0")
    assert len(reads) == 2  # another NIC's event does not
    t[0] += 10
    r("wlan0")
    assert len(reads) == 3  # safety net every 10 s
    ev.running = False
    r("wlan0")
    r("wlan0")
    assert len(reads) == 5  # event process gone: full reading every call, never blind


def test_wifi_events_log_has_times(tmp_path: Path) -> None:
    from core.blocks.link_monitor import WifiEvents

    ev = WifiEvents(tmp_path / "proc", tmp_path / "iw.log")
    assert ev.runner.argv[:3] == ["iw", "event", "-t"]  # a Wi-Fi drop can be timed later


def test_wifi_events_process_reads_only_new_lines(tmp_path: Path) -> None:
    """Real ProcessRunner with a stand-in for `iw event` that prints two lines."""
    import sys
    import time

    from core.blocks.link_monitor import WifiEvents

    script = (
        "import time\n"
        "for x in ('wlan0 (phy #0): a', 'wlan0 (phy #0): b'):\n"
        "    print(x, flush=True); time.sleep(0.2)\n"
        "time.sleep(30)"
    )
    ev = WifiEvents(tmp_path / "proc", tmp_path / "iw.log", argv=(sys.executable, "-c", script))
    assert ev.start() and ev.running
    try:
        got: list[str] = []
        end = time.monotonic() + 5
        while len(got) < 2 and time.monotonic() < end:
            got += ev.new_lines()
            time.sleep(0.1)
        assert got == ["wlan0 (phy #0): a", "wlan0 (phy #0): b"]
        assert ev.new_lines() == []
    finally:
        ev.stop()
    assert not ev.running


def test_a_driver_quality_number_is_not_taken_as_dbm() -> None:
    """GAP-104 (first wireless run, rtl8814au): /proc/net/wireless said level 75, iw said
    -25 dBm. A positive /proc value never replaces iw's dBm."""
    from core.blocks.link_monitor import WifiReader

    full = replace(WIFI, rssi_dbm=-25.0)
    r = WifiReader(FakeEvents(), full_read=lambda nic: full,  # type: ignore[arg-type]
                   rssi_read=lambda: {"wlx0": 75.0}, clock=lambda: 0.0)  # fmt: skip
    got = r("wlx0")
    assert got is not None and got.rssi_dbm == -25.0
    r2 = WifiReader(FakeEvents(), full_read=lambda nic: full,  # type: ignore[arg-type]
                    rssi_read=lambda: {"wlx0": -61.0}, clock=lambda: 0.0)  # fmt: skip
    got2 = r2("wlx0")
    assert got2 is not None and got2.rssi_dbm == -61.0  # a real dBm still updates each second


def test_channel_from_the_frequency_when_iw_info_has_none() -> None:
    """GAP-104: the rtl8814au's `iw dev info` has no channel line."""
    from core.blocks.link_monitor import channel_of

    link = ("Connected to 02:00:5e:10:00:2d (on wlx0)\n\tSSID: X\n\tfreq: 5220.0\n"
            "\tsignal: -25 dBm\n\ttx bitrate: 867.0 MBit/s\n")  # fmt: skip
    w = parse_iw(link, "Interface wlx0\n\ttype managed\n\ttxpower 20.00 dBm\n")
    assert w is not None and (w.channel, w.band, w.freq_mhz) == (44, "5GHz", 5220)
    assert [channel_of(f) for f in (2412, 2437, 2472, 2484, 5180, 5745, 5955, 6115)] == [
        1, 6, 13, 14, 36, 149, 1, 33]  # fmt: skip
    assert channel_of(None) is None and channel_of(3000) is None


INTEL_STATION = """Station 02:00:5e:20:00:50 (on wlp4s0)
\tinactive time:\t37 ms
\trx bytes:\t988453
\trx packets:\t5161
\ttx packets:\t5297
\ttx retries:\t2453
\ttx failed:\t0
\tbeacon loss:\t0
\tsignal:  \t-48 [-51, -48] dBm
"""


def test_station_counters_of_the_ap_entry_or_nothing() -> None:
    from core.blocks.link_monitor import parse_station

    assert parse_station(INTEL_STATION) == {
        "rx_packets": 5161, "tx_retries": 2453, "tx_failed": 0, "beacon_loss": 0}  # fmt: skip
    assert parse_station("") == {}  # rtl8814au: an empty answer (T460, 2026-10-09)
    assert parse_station("\ttx retries:\tn/a\n") == {}


def test_a_wifi_rate_that_moves_is_not_a_speed_change() -> None:
    """2026-10-17: the Controller PC's Wi-Fi (Windows reports its rate as link speed)
    logged "Link Wi-Fi speed changed" twice a second. A cable's speed only."""
    from core.blocks.link_monitor import Link, LinkWatch

    w = LinkWatch()
    w.update([Link("Wi-Fi", "192.168.0.10", True, 817, None, None, wireless=True)])
    assert w.update([Link("Wi-Fi", "192.168.0.10", True, 1153, None, None, wireless=True)]) == []
    w.update([Link("eth0", "192.168.1.2", True, 1000, "full", 0)])
    (c,) = w.update([Link("eth0", "192.168.1.2", True, 100, "full", 0)])
    assert c.kind == "speed"
