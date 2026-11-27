"""The Wi-Fi of a Windows card every second (core/blocks/win_link_wifi.py, 2026-11-28)."""

from __future__ import annotations

import sys
from typing import Any

import pytest

import core.blocks.win_link_wifi as wl
from core.blocks.link_monitor import Wifi, read_links


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_channel_and_rssi_each_second_the_connection_every_10_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The current connection costs 12 ms (measured), channel and RSSI 0.1 ms."""
    clock = Clock()
    r = wl.LinkWifi(clock)
    chan, rssi, full = [36], [-57], []

    def query(api: Any, handle: Any, it: Any, opcode: int, kind: Any) -> Any:
        v = {wl.CHANNEL_NUMBER: chan[0], wl.RSSI: rssi[0]}[opcode]
        return kind(v)

    def connection(api: Any, handle: Any, it: Any, channel: int | None) -> Wifi:
        full.append(clock.t)
        return Wifi(bssid="02:00:00:00:00:01", band="5GHz", channel=channel, rx_mbps=1729.0)

    monkeypatch.setattr(wl, "_query", query)
    monkeypatch.setattr(r, "_connection", connection)
    it = type("It", (), {"desc": "card"})()
    seen = []
    for t in range(25):
        clock.t = float(t)
        rssi[0] = -50 - t
        if t == 13:
            chan[0] = 149  # the router moved: read the connection again at once
        w = r._card(None, None, it)
        assert w is not None
        seen.append((w.channel, w.rssi_dbm))
    assert full == [0.0, 10.0, 13.0, 23.0]
    assert seen[5] == (36, -55.0) and seen[13] == (149, -63.0)


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows Wi-Fi API")
def test_a_connected_card_on_this_pc_says_its_wifi() -> None:
    links = [x for x in read_links() if x.wireless and x.up]
    if not links:
        pytest.skip("no connected Wi-Fi card on this PC")
    w = links[0].wifi
    assert w is not None and w.channel and w.rssi_dbm is not None and -100 < w.rssi_dbm < 0
    assert w.bssid and len(w.bssid) == 17 and w.band in ("2.4GHz", "5GHz", "6GHz", None)
