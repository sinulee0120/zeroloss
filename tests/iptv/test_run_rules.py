"""IPTV run_entry: plan check (16-settings, 05 3.3), start/stop order, cell judging."""

from __future__ import annotations

from typing import Any

import pytest

from core.blocks.color_judge import Color
from core.protocol.envelope import ZlError
from modules.iptv.defaults import DEFAULTS
from modules.iptv.run import (
    cell_color,
    mac_of,
    prepare_ahead,
    prepare_progress,
    prepare_steps,
    receiver_minimum_met,
    start_steps,
    stop_steps,
    validate,
)
from modules.iptv.summary_rules import RULES, shape


def plan(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "module": "iptv",
        "test_type": "pc",
        "devices": {"sender": "tx", "receivers": [{"device": "rx", "ch": 2}]},
        "quality": "HD",
        "duration_s": 600,
        "settings": {},
        "files": {"2": "/data/ch2.ts"},
    }
    base.update(over)
    return base


def test_defaults() -> None:
    p = validate(plan())
    assert (p.quality, p.muxrate_bps, p.ttl, p.warmup_max_s) == ("HD", 10_000_000, 16, 15)
    assert p.groups == {1: "239.1.1.1", 2: "239.1.1.2", 3: "239.1.1.3"} and p.port == 5004
    assert p.not_run == ()  # S-49 On: playback is measured (2026-09-25)


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"test_type": "tablet"}, "ZL-RUN-014"),  # tablets need host and serial
        ({"settings": {"S-21": "every 0 min"}}, "ZL-RUN-014"),
        ({"settings": {"S-21": "Every 121 min"}}, "ZL-RUN-014"),
        ({"settings": {"S-21": "sometimes"}}, "ZL-RUN-014"),
        ({"settings": {"S-21": "Every 1 min", "S-28": 31}}, "ZL-RUN-014"),  # K-10
        ({"settings": {"S-22": "On (4 s)"}}, "ZL-RUN-014"),
        ({"settings": {"S-22": "On (61 s)"}}, "ZL-RUN-014"),
        ({"settings": {"S-47": 0.4}}, "ZL-RUN-014"),
        ({"test_type": "x"}, "ZL-RUN-014"),
        ({"quality": "4K"}, "ZL-RUN-014"),
        ({"duration_s": 5}, "ZL-RUN-014"),
        ({"duration_s": 200 * 3600 + 1}, "ZL-RUN-014"),
        ({"settings": {"S-11": 25}}, "ZL-RUN-014"),  # HD 8..20
        ({"settings": {"S-11": 9.5}}, "ZL-RUN-014"),
        ({"settings": {"S-15": 4}}, "ZL-RUN-014"),
        ({"settings": {"S-28": 90}}, "ZL-RUN-014"),
        ({"devices": {"sender": "tx", "receivers": []}}, "ZL-RUN-014"),
        ({"devices": {"sender": "tx", "receivers": [{"device": "rx", "ch": 4}]}}, "ZL-RUN-014"),
    ],
)
def test_refusals(over: dict[str, Any], code: str) -> None:
    with pytest.raises(ZlError) as e:
        validate(plan(**over))
    assert e.value.code == code


@pytest.mark.parametrize(
    "groups",
    [
        ["239.1.1.1", "239.129.1.1", "239.1.1.3"],  # same MAC (low 23 bits)
        ["239.128.0.5", "239.1.1.2", "239.1.1.3"],  # MAC of 224.0.0.5
        ["239.0.0.9", "239.1.1.2", "239.1.1.3"],
        ["238.1.1.1", "239.1.1.2", "239.1.1.3"],  # outside 239/8
        ["239.1.1.1", "239.1.1.2"],
    ],
)
def test_group_address_rules(groups: list[str]) -> None:
    with pytest.raises(ZlError) as e:
        validate(plan(settings={"S-14": {"groups": groups, "port": 5004}}))
    assert e.value.code == "ZL-RUN-014"


def test_missing_file_and_nics() -> None:
    assert validate(plan(files={})).files == {}  # generated video (iptv 1-1-2)
    assert "file" not in start_steps(validate(plan(files={})))[0][0].params
    with pytest.raises(ZlError):
        validate(plan(files={"4": "/x.ts"}))
    p = validate(plan(nics={"tx": "10.0.0.2", "rx": "127.0.0.1"}))
    stages = start_steps(p)
    assert stages[0][0].params["local_address"] == "10.0.0.2"
    assert stages[1][0].params["interface_ip"] == "127.0.0.1"
    assert "local_address" not in start_steps(validate(plan()))[0][0].params


def test_mac_mapping() -> None:
    assert mac_of("239.1.1.1") == mac_of("239.129.1.1") == mac_of("224.1.1.1")
    assert mac_of("239.1.1.1") != mac_of("239.1.1.2")


def test_start_order_senders_then_receivers_and_reverse_stop() -> None:
    p = validate(plan())
    start = start_steps(p)
    assert [s.path.rsplit("/", 2)[-2] for stage in start for s in stage] == ["sender", "receiver"]
    assert start[0][0].params["dest_ip"] == "239.1.1.2" and start[0][0].timeout_s == 15
    assert start[1][0].params["group"] == "239.1.1.2" and start[1][0].device == "rx"
    stop = stop_steps(p)
    assert [s.path.split("/")[4] for stage in stop for s in stage] == [
        "receiver",
        "sender",
        "receiver",
    ]
    # a video being made goes on after a Stop: the next Start uses it (GAP-116)
    assert not any(s.path.endswith("/prepare/cancel") for stage in stop for s in stage)
    assert stop[2][0].path.endswith("/receiver/perfcheck/cancel") and stop[2][0].device == "rx"


def summary(duration: int, partial: bool, **values: Any) -> dict[str, Any]:
    base = {
        "received": 950 * duration,
        "lost": 0,
        "bitrate_avg_bps": 10e6,
        "jitter_max_ms": 2.0,
        "p1_errors": 0,
        "host_drops": 0,
        "delay_low_ms": 0.0,
        "delay_high_ms": 1.0,
    }
    flat = dict.fromkeys(RULES) | base | values
    piece = {
        "index": 0,
        "start_elapsed_s": 0,
        "duration_s": duration,
        "partial": partial,
        "join_segment": 0,
        "ch": 1,
        "values": flat,
    }
    return shape(piece, None, True)  # the real 02 6 form, as receivers send it


def test_partial_cell_uses_only_the_rate_rule() -> None:
    assert cell_color(summary(1, True), 50)[0] is Color.GREEN  # 950 packets, partial
    assert cell_color(summary(300, False, received=900), 50)[0] is Color.RED
    assert cell_color(summary(1, True, bitrate_avg_bps=50_000), 50)[0] is Color.RED


def test_cell_colors_from_summary_values() -> None:
    assert cell_color(summary(300, False, lost=1), 50)[0] is Color.RED
    assert cell_color(summary(300, False, delay_high_ms=45.0), 50)[0] is Color.YELLOW
    assert cell_color(summary(300, False, jitter_max_ms=30), 20)[0] is Color.YELLOW  # S-23
    assert cell_color({"duration_s": 0, "partial": True}, 50)[0] is Color.GREY


def test_receiver_minimum_for_the_whole_run() -> None:
    assert receiver_minimum_met([summary(1, True)])[0] is False  # 950 < 1,000
    assert receiver_minimum_met([summary(1, True), summary(1, True)])[0] is True


def test_link_need_per_device() -> None:
    """05 1.8 (decided 2026-09-24): sender WAN = all channels, wired receiver = its own
    channels, one PC doing both = the larger."""
    import dataclasses

    from modules.iptv.run import link_need

    # a PC test has one receiver; the two-receiver (tablet) shape is built by hand
    p = dataclasses.replace(
        validate(plan()), sender="tx", receivers=(("rx", 1), ("rx2", 2)), on_air=(1, 2)
    )
    mux = p.muxrate_bps
    assert link_need(p, "tx") == {"bps": 2 * mux, "label": "WAN"}
    assert link_need(p, "rx") == {"bps": mux, "label": "LAN"}
    assert link_need(p, "other") == {"bps": 0, "label": ""}
    loop = validate(plan(devices={"sender": "pc", "receivers": [{"device": "pc", "ch": 2}]}))
    assert link_need(loop, "pc") == {"bps": loop.muxrate_bps, "label": "WAN"}


def test_each_receiver_gets_its_own_player_buffer() -> None:
    """2026-10-16 user: "수신기마다 다를거아니냐" -- a receiver's buffer_ms goes to its
    player only; without one it plays with S-25."""
    rx = {"device": "rx", "ch": 2, "buffer_ms": 3000}
    p = validate(plan(devices={"sender": "tx", "receivers": [rx]}, settings={"S-25": 800}))
    start = next(s for s in start_steps(p)[1] if s.device == "rx")
    assert start.params["playback"]["buffer_ms"] == 3000
    assert p.playback is not None and p.playback["buffer_ms"] == 800  # the test's
    plain = validate(plan(settings={"S-25": 800}))
    assert start_steps(plain)[1][0].params["playback"]["buffer_ms"] == 800
    with pytest.raises(ZlError):
        validate(plan(devices={"sender": "tx", "receivers": [{**rx, "buffer_ms": 50}]}))


def test_playback_settings() -> None:
    """S-49 On by default (16), S-26 mpv by default or VLC (GAP-152), S-25 range, S-50."""
    p = validate(plan())
    assert p.playback == {"player": "mpv", "buffer_ms": 1000, "window": DEFAULTS["S-50"]}
    assert validate(plan(settings={"S-26": "VLC"})).playback["player"] == "VLC"  # type: ignore[index]
    assert p.not_run == ()
    off = validate(plan(settings={"S-49": "Off"}))
    assert off.playback is None and off.not_run == ("playback",)
    for bad, code in [
        ({"S-26": "x"}, "ZL-RUN-014"),
        ({"S-25": 100}, "ZL-RUN-014"),
        ({"S-50": "Huge"}, "ZL-RUN-014"),
        ({"S-49": "Yes"}, "ZL-RUN-014"),
    ]:
        with pytest.raises(ZlError) as e:
            validate(plan(settings=bad))
        assert e.value.code == code, bad


def test_window_values_match_the_settings_table() -> None:
    import re
    from pathlib import Path

    from modules.iptv.run import WINDOW_VALUES

    table = (Path(__file__).resolve().parents[2] / "docs" / "16-settings.md").read_text(
        encoding="utf-8"
    )
    row = next(x for x in table.splitlines() if x.startswith("| S-50 |"))
    allowed = [v.strip() for v in row.split("|")[5].split("/")]
    assert list(WINDOW_VALUES) == allowed
    assert all(re.search("\\d+\u00d7\\d+", v) for v in WINDOW_VALUES[:2])


def test_mpeg_ts_over_udp_is_refused_in_this_build() -> None:
    """16 3: a setting this build cannot do is refused, never silently changed."""
    with pytest.raises(ZlError) as e:
        validate(plan(settings={"S-10": "MPEG-TS / UDP"}))
    assert e.value.code == "ZL-RUN-013"
    assert validate(plan(settings={"S-10": DEFAULTS["S-10"]})).quality == "HD"


def test_prepare_steps_ask_only_for_this_plan() -> None:
    (step,) = prepare_steps(validate(plan(files={})))
    assert step.device == "tx" and step.path.endswith("/sender/prepare")
    assert step.params == {"quality": "HD", "muxrate_bps": 10_000_000, "only": True,
                           "channels": [{"ch": 2, "file": None, "source": None}]}  # fmt: skip
    (step,) = prepare_steps(validate(plan()))
    assert step.params["channels"] == [{"ch": 2, "file": "/data/ch2.ts", "source": None}]


def test_whole_movie_reaches_the_making_and_the_sending() -> None:
    """S-55 (GAP-118): the whole movie is asked for by name in both steps."""
    raw = plan(files={})
    raw["settings"] = {**raw.get("settings", {}), "S-48": {"2": "movie.mp4"}, "S-55": "Whole movie"}
    p = validate(raw)
    (step,) = prepare_steps(p)
    assert step.params["length"] == "whole"
    send = start_steps(p)[0][0]
    assert send.params["source"] == "movie.mp4" and send.params["length"] == "whole"
    raw["settings"]["S-55"] = "10 min"
    assert "length" not in prepare_steps(validate(raw))[0].params
    raw["settings"]["S-55"] = "1 h"
    with pytest.raises(ZlError):
        validate(raw)


def job(ch: int, state: str, **kw: Any) -> dict[str, Any]:
    return {"ch": ch, "quality": "HD", "mbps": 10, "state": state, "percent": None,
            "left_s": None, "error": None, **kw}  # fmt: skip


def test_prepare_progress() -> None:
    p = validate(plan(files={}))
    prog = prepare_progress
    assert prog(p, {})["done"]  # nothing asked of the sender: the file was there
    assert prog(p, {"prepare": {"channels": [job(2, "ready")]}})["done"]
    other = job(1, "failed", error="x")  # another channel's result is not ours
    assert prog(p, {"prepare": {"channels": [other, job(2, "ready")]}})["done"]
    making = prog(p, {"prepare": {"channels": [job(2, "making", percent=42.0, left_s=2280)]}})
    assert not making["done"] and making["percent"] == 42.0 and making["left_s"] == 2280
    assert making["text"] == "CH2 HD 42% \u00b7 about 38 min left"
    failed = prog(p, {"prepare": {"channels": [job(2, "failed", error="no progress for 60 s")]}})
    assert failed["error"] == "CH2: no progress for 60 s"
    assert prog(p, {"prepare": {"channels": [job(2, "checking")]}})["text"] == "CH2 HD checking"


def test_prepare_ahead_checks_its_values() -> None:
    step = prepare_ahead({"sender": "tx", "quality": "UHD"})
    assert step.device == "tx" and step.params["muxrate_bps"] == 35_000_000
    assert [c["ch"] for c in step.params["channels"]] == [1, 2, 3]
    two = prepare_ahead({"sender": "tx", "quality": "HD", "bitrate_mbps": 12, "channels": [3, 1]})
    assert two.params["channels"] == [{"ch": 1, "file": None, "source": None},
                                      {"ch": 3, "file": None, "source": None}]  # fmt: skip
    # a movie picked on the screen (GAP-116): made at once, the newest pick wins
    picked = prepare_ahead({"sender": "tx", "quality": "HD", "channels": [1],
                            "sources": {"1": "그린랜드 2.mp4"}, "only": True}).params  # fmt: skip
    assert picked["channels"] == [{"ch": 1, "file": None, "source": "그린랜드 2.mp4"}]
    assert picked["only"] is True
    for bad in ({"sender": "tx", "quality": "4K"}, {"quality": "HD"},
                {"sender": "tx", "quality": "HD", "bitrate_mbps": 50},
                {"sender": "tx", "quality": "HD", "channels": [4]},
                {"sender": "tx", "quality": "HD", "sources": {"1": "../etc/passwd"}}):  # fmt: skip
        with pytest.raises(ZlError):
            prepare_ahead(bad)


def test_a_cell_after_the_planned_end_is_not_judged() -> None:
    """05 5.4 / GAP-105 3 (first real runs): a 300 s plan ran 0.7 s over and made a 1 s
    cell 300->301 that was judged. Only the planned cells count."""
    from controller.judge import judge_run
    from modules.iptv.run import create

    rules = create()
    p = validate(plan(duration_s=300))
    full = {**summary(300, False), "index": 0, "start_elapsed_s": 0}
    tail = {**summary(1, True), "index": 1, "start_elapsed_s": 300}
    r = judge_run(rules, p, {"rx": [full, tail]}, planned_s=300, measured_s=300.7, completed=True)
    cells = r["devices"][0]["cells"]
    assert [c["index"] for c in cells] == [0] and r["verdict"] == "PASS"


def test_live_view_names_only_real_record_keys() -> None:
    """02 12 /live (GAP-119): the live monitor draws what the module names; every name is a
    1-second record key (02 7), and the jitter line is this plan's warning level."""
    from modules.iptv import record_keys
    from modules.iptv.run import live_view

    known = {v for k, v in vars(record_keys).items() if k.isupper() and isinstance(v, str)}
    p = validate(plan())
    view = live_view(p)
    assert view["cell_s"] == p.cell_s and view["lines"]["jit"] == [p.jitter_warn_ms]
    assert all(c["key"] in known and c["label"] for c in view["columns"])
    assert {"jit", "br", "lost", "cc", "p1", "pb"} <= {c["key"] for c in view["columns"]}
