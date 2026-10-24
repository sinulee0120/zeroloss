"""Main screen values (docs/11-ui.md 3, 1.5): device rows, RSSI / Wi-Fi / link rules,
status line and progress, from the Store only."""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

import pytest

from controller.ui.store import Status
from controller.ui.texts import BANNED, QUALITY_LABELS, TEXT
from controller.ui.view import (
    MINUS,
    cell_rows,
    device_rows,
    finished_line,
    progress,
    setup_rows,
    status_line,
)
from core.ui_schema.design import ICONS
from modules.iptv.sender.commands import QUALITIES


def _result(verdict: str, cells: list[dict[str, Any]], lost: int = 0) -> dict[str, Any]:
    dev = {"device": "t460-rx", "loss_total": lost, "jitter_max_ms": 2.882, "cells": cells}
    return {"verdict": verdict, "verdict_reason": "planned_time_reached", "devices": [dev]}


def test_finished_line_puts_the_verdict_first_and_keeps_notes_from_reading_as_a_failure() -> None:
    """GAP-106 (user, real run R20261003-172831): PASS with a yellow cell looked failed."""
    note = "delay variation 84.1 ms > 40 ms (DVB)"
    color, text = finished_line(_result("PASS", [{"color": "yellow", "reasons": [note]}]))
    assert color == "green" and text.startswith("PASS — no packet lost, max jitter 2.9 ms.")
    assert f"1 note(s), the verdict stands: {note}." in text
    color, text = finished_line(_result("PASS", [{"color": "green", "reasons": []}]))
    assert text == "PASS — no packet lost, max jitter 2.9 ms."
    red = [{"color": "red", "reasons": ["network loss 2", "TR 101 290 P1 errors 1"]}]
    color, text = finished_line(_result("FAIL", red, lost=2))
    assert color == "red"
    assert text == "FAIL — 2 packets lost: network loss 2; TR 101 290 P1 errors 1."
    color, text = finished_line({"verdict": "INCOMPLETE", "verdict_reason": "stopped_by_user"})
    assert text == "INCOMPLETE — stopped by user." and color != "green"


def test_status_line_after_a_run_is_the_verdict_line() -> None:
    result = _result("PASS", [{"color": "yellow", "reasons": ["x"]}])
    st = Status(True, "b", {"state": "IDLE", "result": result}, ())
    assert status_line(st, True) == finished_line(result)


def test_source_video_lists_the_senders_files_per_channel() -> None:
    """GAP-110 (user: "송신 PC 영화 선택은 어떻게 해?"): Generated first, then the files;
    one that does not fit the quality is offered grey with why; only chosen channels
    are written into S-48."""
    from controller.ui.view import (
        GENERATED,
        source_change,
        source_channels,
        source_options,
        source_value,
    )

    up = "1280x720 is made larger to 1920x1080"
    answer = {"files": [
        {"name": "movie.mp4", "width": 1920, "height": 1080, "duration_s": 5400,
         "fits": {"HD": {"ok": True}, "UHD": {"ok": True, "upscaled": True}}},
        {"name": "broken.mkv", "fits": {"HD": {"ok": False, "why": "no video stream"}}},
        {"name": "small.mp4", "width": 1280, "height": 720,
         "fits": {"HD": {"ok": True, "upscaled": True, "why": up}}},
    ]}  # fmt: skip
    opts = source_options(answer, "HD")
    assert opts[0] == (TEXT["source_generated"], GENERATED, True, "")
    assert opts[1] == ("movie.mp4 · 1920×1080 · 90 min", "movie.mp4", True, "")  # noqa: RUF001
    assert opts[2] == ("broken.mkv · ?", "broken.mkv", False, "no video stream")  # grey
    assert opts[3] == ("small.mp4 · 1280×720 ⚠", "small.mp4", True, up)  # noqa: RUF001
    assert source_options(None, "HD") == opts[:1]
    # 2026-10-09: right after the sender restarted, the chosen movie showed as Generated
    waiting = source_options(None, "HD", ("movie.mp4", GENERATED))
    assert [o[:2] for o in waiting] == [(TEXT["source_generated"], GENERATED),
                                        ("movie.mp4 · checking…", "movie.mp4")]  # fmt: skip
    gone = source_options(answer, "HD", ("old.mp4",))
    assert gone[1][:2] == ("old.mp4 · not on the sender PC", "old.mp4") and len(gone) == 5
    assert source_options(answer, "HD", ("movie.mp4",)) == opts  # listed: no extra line
    setup = {"roles": {"receivers": [{"device": "a", "ch": 2}, {"device": "b", "ch": 1}]}}
    assert source_channels(setup) == [1, 2] and source_channels(None) == [1]
    s48 = source_change(GENERATED, 2, "movie.mp4")
    assert s48 == {"2": "movie.mp4"} and source_value(s48, 2) == "movie.mp4"
    assert source_value(s48, 1) == GENERATED
    assert source_change(s48, 2, GENERATED) == GENERATED


def test_yellow_is_a_note_not_an_exclamation_and_all_shapes_differ() -> None:
    """11-ui 1.7 (GAP-106): no exclamation mark on a note; five different shapes."""
    assert "!" not in ICONS["yellow"] and "⚠" not in ICONS["yellow"]
    assert len(set(ICONS.values())) == len(ICONS) == 5


ROW = {
    "color": "green",
    "why": "",
    "link_label": "LAN",
    "items": [
        {"label": "CH2", "value": None, "unit": "", "key": False},
        {"label": "Jitter", "value": 2.1, "unit": "ms", "key": True},
        {"label": "Loss", "value": 0, "unit": "", "key": True},
    ],
}
WIRED = {"nic": "eth0", "ip": "10.0.0.3", "up": True, "link_mbps": 2500, "duplex": "full"}


def wireless(rssi: int | None = -58, ch: int = 36, width: int = 80) -> dict[str, Any]:
    wifi = {"bssid": "b", "band": "5GHz", "channel": ch, "width_mhz": width, "rssi_dbm": rssi}
    return {"nic": "wlan0", "ip": "10.0.0.9", "up": True, "wifi": wifi}


def status(
    state: str = "RUNNING", age: float = 0.4, links: list[Any] | None = None, **dev: Any
) -> Status:
    d = {
        "suspect": False,
        "age_s": age,
        "module_data": {},
        "links": [WIRED] if links is None else links,
        "wifi_changes": [],
        "row": ROW,
    } | dev
    run = {
        "state": state,
        "elapsed_s": 3725.0,
        "planned_s": 36000,
        "cell_s": 300,
        "devices": {"rx": d} if state != "IDLE" else {},
        "cells": {"rx": [{"index": 0, "partial": False, "color": "green", "reasons": []}]},
        "result": None,
    }
    return Status(True, "b", run, ({"name": "rx", "host": "10.0.0.3"},))


def one(st: Status) -> Any:
    (row,) = device_rows(st)
    return row


def test_receiver_row_is_the_module_row() -> None:
    r = one(status())
    assert (r.name, r.color, r.icon) == ("rx", "green", "✔")
    assert [(p.text, p.key) for p in r.parts] == [
        ("CH2", False),
        ("Jitter 2.1 ms", True),
        ("Loss 0", True),
    ]


def test_wired_receiver_has_no_rssi_and_link_only_in_tooltip() -> None:
    r = one(status(link_checks=[check("ok", 2500, label="LAN")]))
    assert r.extra == ()
    assert "LAN link 2.5 Gbps full" in r.tooltip


def test_wireless_receiver_rssi_and_wifi() -> None:
    r = one(status(links=[wireless()]))
    assert [p.text for p in r.extra] == [f"RSSI {MINUS}58 dBm", "Wi-Fi 36 · 80 MHz"]
    assert not r.extra[0].warn


def test_wifi_width_not_reported_is_left_out() -> None:
    """2026-10-09 wireless run: the T460's USB card (rtl8814au) gives no width; the row
    said "Wi-Fi 153 · None MHz"."""
    card = wireless(ch=153)
    card["wifi"]["width_mhz"] = None
    r = one(status(links=[card]))
    assert [p.text for p in r.extra] == [f"RSSI {MINUS}58 dBm", "Wi-Fi 153"]


def test_wifi_width_not_reported_comes_from_the_rate() -> None:
    """2026-10-16 user: "bandwidth가 얼마야? 80Mhz?" -- the T460's USB card gave no width
    but a 867 Mbps link, more than 1~2 streams carry at 40 MHz."""
    card = wireless(ch=36)
    card["wifi"] |= {"width_mhz": None, "tx_mbps": 867.0}
    wifi = one(status(links=[card])).extra[1]
    assert wifi.text == "Wi-Fi 36 · ≥80 MHz" and "867 Mbps" in wifi.tooltip
    card["wifi"]["tx_mbps"] = 300.0
    assert one(status(links=[card])).extra[1].text == "Wi-Fi 36 · ≥40 MHz"
    card["wifi"]["tx_mbps"] = 150.0  # proves nothing
    assert one(status(links=[card])).extra[1].text == "Wi-Fi 36"


def test_only_the_test_card_shows_wifi_values() -> None:
    """GAP-105 1 (first wireless run): the PC's internet Wi-Fi showed next to the test
    card. With the test card known, the row has that card's values only."""
    home = {**wireless(rssi=-61, ch=36), "nic": "wlp4s0", "ip": "192.168.0.13"}
    test = {**wireless(rssi=-25, ch=44), "nic": "wlx0", "ip": "192.168.45.190"}
    r = one(status(links=[home, test], test_ip="192.168.45.190"))
    # the test card first, always (GAP-127), then that card's Wi-Fi values
    assert [p.text for p in r.extra] == ["Wi-Fi wlx0", f"RSSI {MINUS}25 dBm", "Wi-Fi 44 · 80 MHz"]
    wired_test = one(status(links=[WIRED, home], test_ip=WIRED["ip"]))
    # a wired test: its card only, the internet Wi-Fi is not shown at all
    assert [p.text for p in wired_test.extra] == [f"Wired {WIRED['nic']} · 2.5 Gbps"]
    unknown = one(status(links=[home, test]))
    assert len(unknown.extra) == 4  # no test card known yet: nothing hidden


def test_weak_rssi_warns_but_keeps_the_color() -> None:
    r = one(status(links=[wireless(-71)]))
    assert r.extra[0].warn == "yellow" and "below" in r.extra[0].tooltip
    assert r.color == "green"  # RSSI never changes the judgement (11-ui 3)
    assert not one(status(links=[wireless(-70)])).extra[0].warn


def test_rssi_not_available() -> None:
    r = one(status(links=[wireless(None)]))
    assert r.extra[0].text == TEXT["rssi_none"]
    assert r.extra[0].tooltip == TEXT["rssi_none_tip"]


def test_wifi_change_stays_with_full_history() -> None:
    ch = [
        {
            "elapsed_s": 10.0,
            "ts_utc": "2026-09-24T03:34:56Z",
            "nic": "wlan0",
            "from": {"channel": 36, "width_mhz": 80, "band": "5GHz", "bssid": "b"},
            "to": {"channel": 149, "width_mhz": 80, "band": "5GHz", "bssid": "b"},
        },
        {
            "elapsed_s": 20.0,
            "ts_utc": "2026-09-24T03:40:00Z",
            "nic": "wlan0",
            "from": {"channel": 149, "width_mhz": 80, "band": "5GHz", "bssid": "b"},
            "to": {"channel": 149, "width_mhz": 40, "band": "5GHz", "bssid": "b"},
        },
    ]
    r = one(status(links=[wireless(ch=149, width=40)], wifi_changes=ch))
    wifi = r.extra[1]
    assert wifi.warn == "yellow"
    assert re.fullmatch(r"Wi-Fi 36→149 · 80→40 MHz \(\d\d:\d\d\)", wifi.text)
    assert wifi.tooltip.count("\n") == 1  # both changes in the tooltip


def test_stale_device_goes_grey() -> None:
    r = one(status(age=3.0))
    assert r.color == "grey" and r.icon == "○" and r.dim
    assert r.parts[-1].text == "Last update 3 s ago"
    assert one(status(age=2.9)).color == "green"


def test_idle_rows_from_the_device_list() -> None:
    r = one(status("IDLE"))
    assert r.color == "grey" and r.dim
    assert [p.text for p in r.parts] == ["10.0.0.3", "Idle"]


def test_cells_have_icons_and_tooltips() -> None:
    (c,) = cell_rows(status())["rx"]
    assert c.icon == "✔" and "5-min result 0" in c.tooltip


def test_progress_and_status_line() -> None:
    st = status()
    assert progress(st) == "1h 02m / 10h 00m (10%)"
    assert status_line(st, True) == ("green", "Running normally. Next 5-min result in 2:55.")
    red = status(row=ROW | {"color": "red", "why": "3 packets lost in the last second"})
    assert status_line(red, True) == ("red", "rx: 3 packets lost in the last second")
    assert status_line(Status(False), True)[1] == TEXT["reconnecting"]
    idle = status("IDLE")
    assert status_line(idle, False)[1] == TEXT["status_need_plan"]
    assert status_line(idle, True)[1] == TEXT["status_ready"]
    done = Status(True, "b", {"state": "IDLE", "result": {"verdict": "FAIL"}}, ())
    assert status_line(done, True)[0] == "red"


def test_random_statuses_never_break_the_view() -> None:
    """Monkey: any mix of missing / odd fields gives rows, never an exception."""
    rnd = random.Random(7)
    states = ["IDLE", "PREPARING", "RUNNING", "STOPPING", "REPORTING", "SAFE_STOP", "FAULT", "?"]
    for _ in range(2000):
        dev: dict[str, Any] = {}
        options: dict[str, list[Any]] = {
            "age_s": [None, 0.0, 2.5, 3.0, 99.0],
            "links": [None, [], [WIRED], [wireless()], [wireless(None), WIRED | {"up": False}]],
            "row": [None, {}, ROW, ROW | {"items": []}, ROW | {"color": "purple"}],
            "wifi_changes": [None, []],
        }
        for key, choices in options.items():
            if rnd.random() < 0.8:
                dev[key] = rnd.choice(choices)
        run = {"state": rnd.choice(states), "devices": {"rx": dev}, "elapsed_s": rnd.random()}
        st = Status(rnd.random() < 0.9, "b", run, ())
        rows = device_rows(st)
        assert len(rows) == 1
        assert rows[0].icon in "".join(ICONS.values())
        status_line(st, rnd.random() < 0.5)
        progress(st)


def test_quality_labels_match_the_module() -> None:
    """P2 copy of the labels (11-ui 1.7-1) must not drift from the sender table."""
    for name, label in QUALITY_LABELS.items():
        q = QUALITIES[name]
        mbps = q.muxrate_bps // 1_000_000
        assert label == f"{name} · {q.size.replace('x', chr(0xD7))} · {mbps} Mbps"


def test_no_banned_words_alone() -> None:
    """11-ui 1.7-1: 'Resolution' / 'Channel' … never alone on screens."""
    allowed_before = ("Video ", "Screen ", "Wi-Fi ", "TV ")
    for key, text in TEXT.items():
        for word in BANNED:
            for m in re.finditer(rf"\b{word}\b", text, re.I):
                before = text[: m.start()]
                assert before.endswith(allowed_before), (key, text)


def check(level: str, mbps: int | None = 100, **over: Any) -> dict[str, Any]:
    return {
        "nic": "eth0",
        "label": "WAN",
        "level": level,
        "link_mbps": mbps,
        "duplex": "full",
        "needed_mbps": 115.5,
        "usual_mbps": 2500,
        "since_elapsed_s": 754.0,
        "since_utc": "2026-09-24T03:34:56Z",
    } | over


def test_link_levels_on_the_row() -> None:
    """11-ui 3 link alerts: normal = tooltip only, below needed ✖, slower than usual ⚠."""
    r = one(status(link_checks=[check("ok", 2500)]))
    assert r.extra == () and "WAN link 2.5 Gbps full" in r.tooltip
    r = one(status(link_checks=[check("short", 100)]))
    assert [(p.text, p.warn) for p in r.extra] == [("WAN link 100 Mbps", "red")]
    assert "needs about 116 Mbps" in r.extra[0].tooltip
    r = one(status(link_checks=[check("slow", 1000)]))
    assert [(p.text, p.warn) for p in r.extra] == [("WAN link 1 Gbps", "yellow")]
    assert "usually 2.5 Gbps" in r.extra[0].tooltip
    r = one(status(link_checks=[check("down", None)]))
    assert [(p.text, p.warn) for p in r.extra] == [("WAN link down", "red")]


def test_link_problem_leads_the_status_line() -> None:
    red = ROW | {"color": "red", "why": "3 packets lost in the last second"}
    color, text = status_line(status(row=red, link_checks=[check("short", 100)]), True)
    assert color == "red"
    assert re.fullmatch(
        r"WAN link dropped to 100 Mbps at \d\d:\d\d:\d\d — losses after this point are "
        r"likely from the link\.",
        text,
    )
    color, text = status_line(status(link_checks=[check("slow", 1000)]), True)
    assert color == "yellow" and text.startswith("WAN link at 1 Gbps since ")
    color, text = status_line(status(link_checks=[check("down", None)]), True)
    assert color == "red" and text.startswith("WAN link down since ")


def test_result_popup_text() -> None:
    """03 F15: verdict + why first, how long it ran, red boxes, where the report is."""
    from controller.ui.view import result_view

    r = {
        "run_id": "R1",
        "verdict": "INCOMPLETE",
        "verdict_reason": "stopped_by_user",
        "planned_s": 360000,
        "actual_s": 45240,
        "devices": [{"colors": {"red": 0}}],
    }
    v = result_view(r, "/rep")
    assert v.title == "Test finished — INCOMPLETE (stopped by user)"
    assert v.color == "purple"  # INCOMPLETE is never green (05 5.4)
    assert v.body == "Ran 12h 34m of 100h 00m. No red boxes so far."
    assert v.report == str(Path("/rep/R1/report.html")) and v.folder == str(Path("/rep/R1"))
    fail = result_view(
        r
        | {
            "verdict": "FAIL",
            "verdict_reason": "planned_time_reached",
            "devices": [{"colors": {"red": 2}}, {"colors": {"red": 1}}],
        },
        "/rep",
    )
    assert fail.title.endswith("FAIL (planned time reached)") and fail.color == "red"
    assert "3 red boxes" in fail.body
    lost = result_view(r | {"verdict_reason": "device_lost: rx: no answer for 10 s"}, "/rep")
    assert lost.title.endswith("(a device stopped answering: rx: no answer for 10 s)")
    ok = result_view(r | {"verdict": "PASS", "verdict_reason": "planned_time_reached"}, "/rep")
    assert ok.color == "green"


def problems_status(flood: int | None = None) -> Status:
    rows = [
        {"code": "ZL-NET-013", "level": "error", "device": "tx", "what": "link 100 Mbps",
         "why": "w", "fix": "f", "active": True, "count": 2, "last_utc": "2026-09-25T03:00:00Z",
         "cleared_utc": None, "stale": False},
        {"code": "ZL-RCV-020", "level": "warn", "device": "rx", "what": "not playing",
         "why": "w", "fix": "f", "active": True, "count": 1, "last_utc": "2026-09-25T03:00:01Z",
         "cleared_utc": None, "stale": True},
        {"code": "ZL-DSC-014", "level": "warn", "device": "rx", "what": "no answer",
         "why": "w", "fix": "f", "active": False, "count": 1, "last_utc": "2026-09-25T02:00:00Z",
         "cleared_utc": "2026-09-25T02:01:00Z", "stale": False},
    ]  # fmt: skip
    p = {"counts": {"error": 1, "warn": 1}, "flood": {"new_in_10_min": flood} if flood else None,
         "problems": rows}  # fmt: skip
    return Status(True, "b", {"state": "RUNNING"}, (), problems=p)


def test_problems_badge_and_rows() -> None:
    """03 F21: badge counts open problems; resolved rows are grey below."""
    from controller.ui.view import problems_view

    v = problems_view(problems_status())
    assert v.badge == "2 problems" and v.badge_color == "red" and v.flood == ""
    assert [(r.code, r.color, r.icon) for r in v.rows] == [
        ("ZL-NET-013", "red", "✖"),
        ("ZL-RCV-020", "yellow", ICONS["yellow"]),
        ("ZL-DSC-014", "grey", "○"),
    ]
    assert v.rows[1].stale and v.rows[0].count == 2
    assert problems_view(Status(True, "b", {}, ())).badge == ""


def test_flood_shows_only_errors() -> None:
    from controller.ui.view import problems_view

    v = problems_view(problems_status(flood=12))
    assert v.flood.startswith("Many problems at once (12 in 10 min)")
    assert [r.code for r in v.rows if r.active] == ["ZL-NET-013"]  # warnings folded


def test_device_with_an_open_error_is_marked() -> None:
    """F21: the device's row gets a red border while it has an open error."""
    st = problems_status()
    run = dict(st.run) | {
        "devices": {"tx": {"row": ROW, "age_s": 0.1}, "rx": {"row": ROW, "age_s": 0.1}}
    }
    rows = {r.name: r.alarm for r in device_rows(Status(True, "b", run, (), problems=st.problems))}
    assert rows == {"tx": True, "rx": False}  # rx has only a warning


def test_a_start_that_stopped_at_a_check_is_never_ready() -> None:
    """GAP-115 (seen on the real window): Start stopped at step 5 (✖ in the list), yet the
    line said "Ready. Press Start." in green. It says what stopped it, in red."""
    steps = [{"n": 1, "name": "Devices connected", "state": "ok"},
             {"n": 5, "name": "Multicast test", "state": "failed",
              "what": "t460-rx received no test packets"}]  # fmt: skip
    run = {"state": "IDLE", "checklist": {"kind": "start", "ok": False, "steps": steps}}
    color, text = status_line(Status(True, "b", run, ()), True)
    assert color == "red"
    assert text.startswith("Did not start — Multicast test: t460-rx received no test packets")
    run_ok = {"state": "IDLE", "checklist": {"kind": "start", "ok": True, "steps": steps[:1]}}
    assert status_line(Status(True, "b", run_ok, ()), True)[1] == TEXT["status_ready"]


def test_baseline_rows_name_the_cable_not_the_router() -> None:
    """S-54 Baseline: there is no router, so no row says "router WAN/LAN side" (GAP-115)."""
    sides = {"tx": {"reachable": True, "side": "wan"}, "rx": {"reachable": True, "side": "lan"}}
    roles = {"sender": "tx", "receivers": [{"device": "rx", "ch": 1}]}
    base = {"sides": sides, "roles": roles}
    assert [r.side for r in setup_rows(base)] == [TEXT["side_lan"], TEXT["side_wan"]]
    direct = base | {"setup": {"settings": {"S-54": "Baseline (no router)"}}}
    assert {r.side for r in setup_rows(direct)} == {TEXT["side_direct"]}


def test_a_short_test_counts_down_to_its_end() -> None:
    """A 2-minute test never says "next 5-min result in 4:32" (GAP-115, real window)."""
    run = {"state": "RUNNING", "elapsed_s": 28.0, "planned_s": 120, "cell_s": 300}
    color, text = status_line(Status(True, "b", run, ()), True)
    assert (color, text) == (
        "green",
        "Running normally. The test ends and gives its result in 1:32.",
    )


def test_a_finished_test_has_no_so_far() -> None:
    """The popup of a test that ran to its end said "No red boxes so far." (GAP-115)."""
    from controller.ui.view import result_view

    r = {"run_id": "R2", "verdict": "PASS", "verdict_reason": "planned_time_reached",
         "planned_s": 120, "actual_s": 121, "devices": [{"colors": {"red": 0}}]}  # fmt: skip
    assert result_view(r, "/rep").body == "Ran 2m 01s of 2m 00s. No red boxes."


def test_a_cancel_is_not_a_failure() -> None:
    """GAP-125 (real window): after the person's own Cancel the screen said red "Did not
    start — … Fix it" with a ✖; it is a grey line and a paused step, no Why/Fix."""
    from controller.ui.view import checklist_lines

    steps = [{"n": 5, "name": "Multicast test", "state": "ok", "detail": "packets: rx 380"},
             {"n": 6, "name": "Start sending", "state": "failed", "code": "ZL-USR-006",
              "what": "Start cancelled", "why": "Cancel pressed", "fix": "Start"}]  # fmt: skip
    run = {"state": "IDLE", "checklist": {"kind": "start", "ok": False, "steps": steps}}
    st = Status(True, "b", run, ())
    assert status_line(st, True) == ("grey", TEXT["status_cancelled"])
    lines = checklist_lines(st, None)
    assert lines[-1] == f"⏸ Start sending — {TEXT['step_cancelled']}"
    assert not any("Why:" in x or "✖" in x for x in lines)


def test_the_row_says_which_card_the_test_uses() -> None:
    """GAP-127 (user: "유선인지 무선인지 어디서 알아?"): a row always names its test card."""
    from controller.ui.view import card_text

    links: list[dict[str, Any]] = [
        {"nic": "enp0s31f6", "ip": "192.168.45.177", "link_mbps": 1000},
        {"nic": "wlx58", "ip": "192.168.45.190", "wifi": {"ssid": "ZL_WIFI_IPTV"}},
    ]
    assert card_text(links, "192.168.45.177") == "Wired enp0s31f6 · 1 Gbps"
    assert card_text(links, "192.168.45.190") == "Wi-Fi wlx58 · ZL_WIFI_IPTV"
    assert card_text(links, "wifi") == TEXT["card_wifi_test"] and card_text(links, None) == ""


@pytest.mark.parametrize(
    ("text", "secs"),
    [("40m", 2400), ("1h 30m", 5400), ("1h30m", 5400), ("100h", 360000), ("90s", 90),
     ("1.5h", 5400), ("2:30", 9000), ("40", 2400), (" 2H ", 7200),
     ("5s", None), ("201h", None), ("abc", None), ("1x", None), ("", None), ("h", None)],
)  # fmt: skip
def test_a_typed_duration(text: str, secs: int | None) -> None:
    """2026-10-09 user: "you set any time, I cannot type one in the app"."""
    from controller.ui.view import parse_duration

    assert parse_duration(text) == secs


def test_a_box_tooltip_has_its_max_jitter_and_loss() -> None:
    """GAP-146 (user: the reference board shows each 5 minutes' max jitter and loss)."""
    from controller.ui.view import cell_rows

    vals = [{"key": "lost", "label": "Lost", "unit": "", "value": 0, "bad": True},
            {"key": "jitter_max", "label": "Jitter max", "unit": "ms", "value": 3.2},
            {"key": "bitrate_avg", "label": "Bitrate", "unit": "Mbps", "value": None}]  # fmt: skip
    run = {"state": "RUNNING", "cells": {"rx": [{"index": 0, "color": "green", "reasons": [],
                                                 "values": vals}]}}  # fmt: skip
    (cell,) = cell_rows(Status(True, "b", run, ()))["rx"]
    assert cell.tooltip.splitlines()[1] == "Lost 0 · Jitter max 3.2 ms"


def test_the_source_list_says_which_videos_start_at_once() -> None:
    """2026-10-17 user: "변환이 필요하면 알려주고, 필요없으면 바로 시작하게 하든가 아니면 메뉴에서
    바로 변환없이 바로 사용가능한 파일은 별도로 알려주던가 직접 선택하게"."""
    from controller.ui.view import GENERATED, source_options

    made = {"quality": "UHD", "ch": 1, "mbps": 35, "whole": False}
    answer = {"generated_made": [], "files": [
        {"name": "sample_1_original.mp4", "width": 1724, "height": 720,
         "fits": {"UHD": {"ok": True}}, "made": [made]},
        {"name": "other.mp4", "width": 3840, "height": 2160, "fits": {"UHD": {"ok": True}},
         "made": []},
    ]}  # fmt: skip
    ch1 = source_options(answer, "UHD", (), 1, 35)
    assert ch1[0][0].startswith(TEXT["source_to_make"]) and ch1[0][1] == GENERATED
    assert ch1[1][0].startswith(TEXT["source_ready"]) and TEXT["source_ready_tip"] in ch1[1][3]
    assert ch1[2][0].startswith(TEXT["source_to_make"])
    # another channel, bitrate or the whole movie is another file: made first
    assert source_options(answer, "UHD", (), 2, 35)[1][0].startswith(TEXT["source_to_make"])
    assert source_options(answer, "UHD", (), 1, 30)[1][0].startswith(TEXT["source_to_make"])
    assert source_options(answer, "UHD", (), 1, 35, True)[1][0].startswith(TEXT["source_to_make"])
    # an older sender that does not say: no mark
    old = {"files": [{k: v for k, v in answer["files"][0].items() if k != "made"}]}
    assert "✔" not in source_options(old, "UHD", (), 1, 35)[1][0]


def test_a_throughput_run_shows_what_each_pc_does_and_one_line_per_case() -> None:
    """2026-10-17 user: "devices에도 컴퓨터 이름만 보여주고 뭐가 뭐하는 놈인지도 안적혀있어",
    "5-min result화면도 지금 이상황에선 안보여도 되는데 ... 다른걸 보여주던가"."""
    from controller.ui.view import case_table, device_rows

    old = {"items": [{"text": "LAN side · iperf3 client", "value": None}]}  # build 0033
    new = {"items": [{"label": "WAN side · iperf3 server", "value": None}]}
    cell = {"color": "red", "reasons": ["receive 681 Mbps < 900 Mbps"], "values": [
        {"key": "case", "label": "Case", "unit": "", "value": "Auto"},
        {"key": "send", "label": "LAN→WAN (send)", "unit": "Mbps", "value": 934.158},
        {"key": "receive", "label": "WAN→LAN (receive, -R)", "unit": "Mbps",
         "value": 681.1}]}  # fmt: skip
    run = {"state": "RUNNING", "devices": {"t460": {"row": old}, "probook": {"row": new}},
           "cells": {"t460": [cell]}}  # fmt: skip
    st = Status(True, "b", run, ())
    rows = {r.name: [p.text for p in r.parts] for r in device_rows(st)}
    assert rows == {"probook": ["WAN side · iperf3 server"], "t460": ["LAN side · iperf3 client"]}
    heads, cases = case_table(st)
    assert heads == ("Case", "LAN→WAN (send) (Mbps)", "WAN→LAN (receive, -R) (Mbps)")
    (c,) = cases
    assert (c.device, c.color, c.values) == ("t460", "red", ("Auto", "934", "681"))
    assert c.why == "receive 681 Mbps < 900 Mbps"


def test_the_case_table_keeps_details_off_the_columns() -> None:
    """2026-10-17 user ("아무말 대잔치 ... 누가 이해할까"): the main columns only; the iperf3
    command, retransmits and the rest are on the row's hover."""
    from controller.ui.view import case_table

    cell = {"color": "green", "reasons": [], "values": [
        {"key": "case", "label": "Case", "unit": "", "value": "Auto (both cards on Auto)"},
        {"key": "send", "label": "LAN→WAN", "unit": "Mbps", "value": 934.2},
        {"key": "command", "label": "iperf3 command", "unit": "", "value": "iperf3 -c 10.0.0.1",
         "detail": True}]}  # fmt: skip
    heads, (row,) = case_table(Status(True, "b", {"cells": {"t460-rx": [cell]}}, ()))
    assert heads == ("Case", "LAN→WAN (Mbps)")
    assert row.values == ("Auto (both cards on Auto)", "934")
    assert row.detail == "iperf3 command: iperf3 -c 10.0.0.1"


def test_a_throughput_screen_has_no_video_boxes_and_no_5_minute_line() -> None:
    """2026-10-17, as the user saw it: the rows offered Receiver, CH1, Wired and Buffer, and
    the status said "Next 5-min result in 4:38" during iperf."""
    from controller.ui.view import setup_rows, status_line

    def answer(module: str) -> dict[str, Any]:
        return {"setup": {"module": module, "settings": {}, "nics": {}},
                "roles": {"sender": "pb", "receivers": [{"device": "t460", "ch": 1}]},
                "sides": {"pb": {"reachable": True, "side": "wan"},
                          "t460": {"reachable": True, "side": "lan"}}}  # fmt: skip

    assert all(not r.video for r in setup_rows(answer("throughput")))
    assert all(r.video for r in setup_rows(answer("iptv")))
    run = {"state": "RUNNING", "elapsed_s": 22, "planned_s": 2530, "cell_s": 0, "devices": {}}
    color, text = status_line(Status(True, "b", run, ()), True)
    assert color == "green" and text == TEXT["status_running_cases"] and "5-min" not in text


def test_each_card_is_a_line_to_tick_and_nothing_is_ticked_for_the_person() -> None:
    """2026-10-17 user: "연결된 포트가 있으면 그만큼 row가 늘어나야지 그래야 사용자가 고르지",
    "와이파이도 카드 뭐로 할건지 사용자가 정할거야", "2G/5G/6G중에 SSID클릭해주면 되잖아"."""
    from controller.ui.view import setup_rows, ticks_change

    setup = {"setup": {"module": "throughput", "settings": {}, "nics": {}},
             "roles": {"sender": "pb", "receivers": []}, "sides": {
        "pb": {"reachable": True, "side": "wan", "host": "10.0.0.1",
               "links": [{"nic": "enp37s0", "ip": "10.0.0.1", "link_mbps": 1000}]},
        "t460": {"reachable": True, "side": "lan", "host": "192.168.45.87", "links": [
            {"nic": "enp0s31f6", "ip": "192.168.45.139", "link_mbps": 1000},
            {"nic": "wlx58", "ip": "192.168.45.87", "wifi": {"band": "5GHz", "channel": 149}},
            {"nic": "wlp4s0", "ip": "192.168.0.13", "wifi": {"band": "5GHz"}}]},
        "controller": {"reachable": True, "side": "lan", "host": "192.168.45.190",
                       "this_pc": True, "links": [
            {"nic": "이더넷", "ip": "192.168.45.190", "link_mbps": 1000},
            {"nic": "Wi-Fi", "ip": "192.168.0.10", "wifi": None}]}}}  # fmt: skip
    rows = {r.name: r for r in setup_rows(setup)}
    lines = {r: [(c.label, c.tickable, c.ticked) for c in rows[r].cards_lines] for r in rows}
    assert lines["pb"] == [("enp37s0 · 10.0.0.1 · WAN · 1 Gbps", False, False)]
    assert lines["t460"] == [
        ("enp0s31f6 · 192.168.45.139 · LAN · 1 Gbps", True, False),
        ("wlx58 · 192.168.45.87 · Wi-Fi 5GHz ch 149", True, False),
        ("wlp4s0 · 192.168.0.13 · Wi-Fi 5GHz", True, False),
    ]  # every card a line, nothing ticked
    assert lines["controller"] == [
        ("이더넷 · 192.168.45.190 · LAN · 1 Gbps", True, False),
        ("Wi-Fi · 192.168.0.10 · Wi-Fi", True, False),  # its Wi-Fi too, like a Linux PC's
    ]
    picks = ticks_change({}, "t460", "wlx58", tick=True)
    scan = {"t460|wlx58": {"networks": [
        {"band": "5GHz", "ssid": "R-5G", "bssid": "AA:00:00:00:00:02", "signal": 90,
         "security": "Open"},
        {"band": "2.4GHz", "ssid": "home", "bssid": "BB:00:00:00:00:01", "signal": 70,
         "security": "WPA2"}]}}  # fmt: skip
    (wlx,) = [c for c in {r.name: r for r in setup_rows(setup, None, picks, scan)}["t460"]
              .cards_lines if c.nic == "wlx58"]  # fmt: skip
    bands = {b: (opts, chosen, box) for b, opts, chosen, box, _ssid in wlx.bands}
    assert wlx.ticked and bands["5GHz"] == ((("R-5G · AA:00:00:00:00:02 · 90 % · open",
                                             "AA:00:00:00:00:02", True, ""),), "", "")  # fmt: skip
    assert bands["2.4GHz"][0][0][2] is True  # a password network is pickable too
    assert bands["6GHz"] == ((), "", "")  # nothing picked: no password box
    nets = scan["t460|wlx58"]["networks"]
    picks = ticks_change(picks, "t460", "wlx58", band="5GHz", net=nets[0])
    assert picks == {"t460": {"wlx58": {"bands": {
        "5GHz": {"ssid": "R-5G", "bssid": "AA:00:00:00:00:02", "security": "Open"}}}}}  # fmt: skip
    # the box beside a band: "open", empty (no password typed = none), or saved
    both = ticks_change(picks, "t460", "wlx58", band="2.4GHz", net=nets[1])

    def boxes(saved: list[str]) -> dict[str, tuple[str, str]]:
        rows = {r.name: r for r in setup_rows({**setup, "wifi_passwords": saved}, None, both,
                                              scan)}  # fmt: skip
        card = next(c for c in rows["t460"].cards_lines if c.nic == "wlx58")
        return {b: (box, ssid) for b, _o, _c, box, ssid in card.bands}

    assert boxes([])["5GHz"] == ("open", "R-5G")
    assert boxes([])["2.4GHz"] == ("empty", "home")
    assert boxes(["home"])["2.4GHz"] == ("saved", "home")
    assert ticks_change(picks, "t460", "wlx58", tick=False) == {}


def test_a_throughput_result_says_its_cases_not_packet_loss() -> None:
    """2026-10-17: the iperf result line read "FAIL — no packet lost: WAN→LAN 693 ..."."""
    from controller.ui.view import finished_line

    cells = [{"color": "green", "reasons": []}, {"color": "yellow", "reasons": ["check"]},
             {"color": "red", "reasons": ["WAN→LAN 693 Mbps, below 900"]}]  # fmt: skip
    result = {"verdict": "FAIL", "devices": [{"device": "t460-rx", "cells": cells}]}
    color, text = finished_line(result)
    assert color == "red" and "packet" not in text
    assert (
        text == "FAIL — 3 cases: 1 passed, 1 check needed, 1 failed: WAN→LAN 693 Mbps, below 900."
    )


def test_one_wifi_card_per_pc() -> None:
    """2026-10-17 user: "무선은 어떻게 골라 두개 있는 경우 하나만 선택할건데"."""
    from controller.ui.view import ticks_change

    v = ticks_change({}, "t460", "enp0s31f6", tick=True)
    v = ticks_change(v, "t460", "wlx58", tick=True, drop=("wlp4s0",))
    v = ticks_change(v, "t460", "wlp4s0", tick=True, drop=("wlx58",))
    assert v == {"t460": {"enp0s31f6": {}, "wlp4s0": {}}}  # the wired one stays


def test_a_failed_wifi_scan_says_why_under_the_card() -> None:
    """A scan never fails silently; a PC on an older build is told to update."""
    from controller.ui.view import scan_note

    assert scan_note({"networks": []}) == ""
    old = {"error": {"code": "ZL-DSC-012", "what": "Request rejected",
                     "detail": "no such path: /v1/wifi/scan"}}  # fmt: skip
    assert "Update PCs" in scan_note(old)
    busy = {"error": {"code": "ZL-RCV-023", "what": "Wi-Fi card busy", "why": "Try again."}}
    assert scan_note(busy) == "Scan failed: Wi-Fi card busy: Try again."


def test_a_band_picked_before_shows_without_a_scan() -> None:
    """2026-10-22: a new app showed "not tested" though the picks were saved."""
    from controller.ui.view import ticks_change

    setup = {"setup": {"module": "throughput", "settings": {}, "nics": {}},
             "roles": {"sender": "pb", "receivers": []}, "sides": {
        "t460": {"reachable": True, "side": "lan", "host": "192.168.45.87", "links": [
            {"nic": "wlx58", "ip": "192.168.45.87", "wifi": {"band": "5GHz"}}]}}}  # fmt: skip
    net = {"band": "5GHz", "ssid": "ZL_TEST_5G", "bssid": "AA:00:00:00:00:02",
           "signal": 90, "security": "WPA2"}  # fmt: skip
    picks = ticks_change(ticks_change({}, "t460", "wlx58", tick=True), "t460", "wlx58",
                         band="5GHz", net=net)  # fmt: skip
    rows = {r.name: r for r in setup_rows(setup, None, picks, {})}  # no scan yet
    (line,) = rows["t460"].cards_lines
    band = next(b for b in line.bands if b[0] == "5GHz")
    assert band[2] == "AA:00:00:00:00:02" and "picked before" in band[1][0][0]
    open_now = {"t460|wlx58": {"networks": [{**net, "security": "Open"}]}}
    rows = {r.name: r for r in setup_rows(setup, None, picks, open_now)}
    band = next(b for b in rows["t460"].cards_lines[0].bands if b[0] == "5GHz")
    assert band[3] == "open"  # the scan says open now: no password box


def test_movies_made_already_come_first_and_one_is_the_default() -> None:
    """2026-10-24 user: start without making anything -- the made movies first, and a
    channel nobody picked a movie for gets the one made for it."""
    from controller.ui.view import ready_source, source_options

    made = {"quality": "UHD", "ch": 2, "mbps": 35, "whole": False}
    answer = {"files": [
        {"name": "a.mp4", "width": 1280, "height": 720, "duration_s": 600, "made": []},
        {"name": "b.mp4", "width": 1280, "height": 720, "duration_s": 600, "made": [made]},
    ], "generated_made": []}  # fmt: skip
    opts = source_options(answer, "UHD", (), ch=2, mbps=35)
    assert [o[1] for o in opts][:2] == ["Generated", "b.mp4"]  # made one right after Generated
    assert ready_source(answer, "UHD", 2, 35, False) == "b.mp4"
    assert ready_source(answer, "UHD", 1, 35, False) is None  # none made for CH1
