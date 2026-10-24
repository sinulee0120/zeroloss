"""One channel change line and its keys (02 7-2, 03 F19, 20 GAP-90)."""

from __future__ import annotations

from typing import Any

import pytest

from modules.iptv import record_keys as k
from modules.iptv.receiver.zap import GIVE_UP_S, Zap
from modules.iptv.summary_rules import make_summarizer, shape

LINE_KEYS = [
    "device", "from", "to", "join_method", "mode", "t_cmd", "t_leave", "t_join",
    "t_first_packet", "t_first_frame", "t_normal_play", "basis", "timeout_s", "success",
    "join_failed", "leave_check", "t_last_old_packet", "end",
]  # fmt: skip


def zap(playback: bool = True, **kw: Any) -> Zap:
    z = Zap("pc", 1, 2, "A", "manual", 5.0, playback, t_cmd=100.0)
    for name, value in kw.items():
        setattr(z, name, value)
    return z


def test_line_has_every_02_key_in_order() -> None:
    assert list(zap().to_line()) == LINE_KEYS


def test_basis_follows_playback() -> None:
    on = zap(t_first_packet=100.2, t_normal_play=101.0)
    off = zap(playback=False, t_first_packet=100.2, t_normal_play=101.0)
    assert (on.basis, on.total_ms) == ("normal_play", 1000)
    assert (off.basis, off.total_ms) == ("first_packet", 200)


@pytest.mark.parametrize(
    ("kw", "want"),
    [
        ({"t_normal_play": 104.9}, True),
        ({"t_normal_play": 105.0}, True),  # at the limit still counts
        ({"t_normal_play": 105.1}, False),
        ({"join_failed": True, "t_normal_play": 101.0}, False),
        ({}, None),  # not known yet
        ({"end": "done"}, False),  # ended without a basis time (player never ok)
    ],
)
def test_success(kw: dict[str, Any], want: bool | None) -> None:
    assert zap(**kw).success is want


def test_ready_to_end_needs_join_leave_check_and_basis() -> None:
    z = zap(t_normal_play=101.0)
    assert not z.ready_to_end(101.0)  # not joined
    z.joined.set()
    assert not z.ready_to_end(101.0)  # Leave check still running
    z.leave_check = "ok"
    assert z.ready_to_end(101.0)
    late = zap(leave_check="ok")
    late.joined.set()
    assert not late.ready_to_end(100.0 + GIVE_UP_S - 0.1)
    assert late.ready_to_end(100.0 + GIVE_UP_S)  # the player never got ok
    failed = zap(leave_check="not_checked", join_failed=True)
    failed.joined.set()
    assert failed.ready_to_end(100.5)


def test_keys_leave_zero_values_out() -> None:
    z = zap(t_join=100.1, t_first_packet=100.3, t_normal_play=101.3, leave_check="ok")
    assert z.keys() == {
        k.ZAP_COUNT: 1, k.ZAP_OK: 1, k.ZAP_TOTAL_MS: 1300, k.ZAP_JOIN_MS: 200,
        k.ZAP_PLAY_MS: 1000,
    }  # fmt: skip
    bad = zap(join_failed=True, leave_check="not_processed")
    assert bad.keys() == {k.ZAP_COUNT: 1, k.ZAP_JOIN_FAILED: 1, k.ZAP_LEAVE_NOT_PROCESSED: 1}
    off = zap(playback=False, t_join=100.1, t_first_packet=100.3, t_normal_play=101.0)
    off_keys = off.keys()
    assert k.ZAP_PLAY_MS not in off_keys  # no playback: no play time


def sec(e: int, ch: int, **extra: Any) -> dict[str, Any]:
    return {"type": "s", "e": e, "ch": ch, "rx": 100, "lost": 0, "jit": 1.0, "br": 1_000_000,
            "hd": 0, **extra}  # fmt: skip


def test_summary_channel_values_and_segments() -> None:
    s = make_summarizer(cell_s=10)
    pieces: list[dict[str, Any]] = []
    for e in range(4):
        pieces += s.add(sec(e, 1))
    ok = zap(t_join=104.1, t_first_packet=104.3, leave_check="ok", playback=False).keys()
    pieces += s.add(sec(7, 2, **ok))  # the first line after the change
    pieces += s.add(sec(8, 2))
    failed = zap(join_failed=True, leave_check="not_processed").keys()
    pieces += s.add(sec(9, 3, **failed))
    pieces += s.flush()
    out = [shape(p, None, False) for p in pieces]
    assert [(x["ch"], x["join_segment"]) for x in out] == [(1, 0), (2, 1), (3, 2)]
    total = {name: sum(x["channel"][name] for x in out) for name in
             ("changes", "success", "join_failures", "leave_not_processed")}  # fmt: skip
    assert total == {"changes": 2, "success": 1, "join_failures": 1, "leave_not_processed": 1}
    ch2 = out[1]["channel"]
    assert ch2["zap_ms"]["total_max"] == ok[k.ZAP_TOTAL_MS]
    assert ch2["zap_ms"]["join_to_first_packet_max"] == 200
    assert out[0]["channel"]["zap_ms"]["total_max"] is None  # no change in that piece


def test_summary_wifi_from_the_seconds_and_none_when_wired() -> None:
    """02 6 ``wifi`` (GAP-104 3): min RSSI and rate, last band/channel/width, changes."""
    s = make_summarizer(cell_s=10)
    pieces: list[dict[str, Any]] = []
    w = {k.WIFI_BAND: "5GHz", k.WIFI_BW: 80}
    for e, (rssi, mbps, ch, extra) in enumerate([
        (-30, 867.0, 44, {}), (-41, 650.0, 44, {}), (-29, 867.0, 40, {k.WIFI_CH_CHANGED: 1}),
        (-33, 780.0, 40, {k.WIFI_BSSID_CHANGED: 1}),
    ]):  # fmt: skip
        pieces += s.add(sec(e, 1, **w, **{k.RSSI_DBM: rssi, k.WIFI_MBPS: mbps, k.WIFI_CH: ch},
                            **extra))  # fmt: skip
    pieces += s.flush()
    got = shape(pieces[0], None, False)["wifi"]
    want = {"rssi_dbm_min": -41, "link_mbps_min": 650.0, "band": "5GHz", "channel": 40,
            "width_mhz": 80, "channel_changes": 1, "bssid_changes": 1}  # fmt: skip
    assert got == want
    wired = make_summarizer(cell_s=10)
    parts = wired.add(sec(0, 1)) + wired.flush()
    assert shape(parts[0], None, False)["wifi"] is None
