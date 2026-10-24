"""The report next to its verdict (02 10, F15)."""

from __future__ import annotations

from pathlib import Path


def test_each_box_that_is_not_green_says_why_next_to_the_verdict() -> None:
    """GAP-116: the user saw a red box with no loss and normal jitter and no reason —
    the report lists every box that is not green with its time and reasons."""
    from controller.report import cell_whys

    r = {"devices": [{"device": "t460-rx", "ch": 1, "cells": [
        {"start_elapsed_s": 600, "duration_s": 300, "color": "green", "reasons": []},
        {"start_elapsed_s": 900, "duration_s": 300, "color": "red",
         "reasons": ["TR 101 290 P1 errors 7", "sender trouble for 8 s (lost 0, P1 2)"]},
    ]}]}  # fmt: skip
    assert cell_whys(r) == ["t460-rx CH1 0h 15m to 0h 20m red: TR 101 290 P1 errors 7; "
                            "sender trouble for 8 s (lost 0, P1 2)"]  # fmt: skip


def test_the_range_takes_clock_times_and_a_problems_only_filter() -> None:
    """GAP-129 (user: "5분단위 결과 리스트도 시간.날짜 입력하면 필터링", "문제되는 부분만"): the
    range view takes a date and time as well as a run time, and lists the boxes that are
    not green with when they began and why."""
    from controller.report_page import SCRIPT, section

    page = section({"devices": [], "end_s": 600, "started_utc": "2026-10-04T01:15:40Z",
                    "minutes": {}, "hours": [], "analysis": [], "verdict": "PASS"})  # fmt: skip
    assert 'data-q="bad"' in page and 'id="list"' in page and "2026-10-09 14:30" in page
    assert "function clock(t)" in SCRIPT and "function troubleList()" in SCRIPT


def test_the_report_graph_has_axes_and_the_clock() -> None:
    """GAP-139 (user: "the final report must show the whole test from start to end"):
    charts drawn to the pixel with grid values and clock times, not one stretched line."""
    from controller.report_page import SCRIPT, section

    html = section({"devices": [], "minutes": {}, "hours": [], "analysis": [],
                    "verdict": "PASS", "end_s": 0})  # fmt: skip
    assert '<div id="graph" class="graphs">' in html and 'preserveAspectRatio="none"' not in html
    assert "function chart(" in SCRIPT and "function clk(" in SCRIPT
    assert '<div id="fine" class="fine">' in html and "function fineDraw(" in SCRIPT  # GAP-147


def test_a_piece_after_the_end_that_was_never_judged_is_not_a_box(tmp_path: Path) -> None:
    """2026-10-09 user: the list ended with a grey box "no reason recorded": a 1 s
    summary after the planned end, never judged."""
    import json as _json

    from controller.report_page import page_data

    rows = [{"device": "rx", "ch": 1, "index": i, "start_elapsed_s": i * 300,
             "duration_s": 300 if i < 2 else 1, "loss": {"lost": 0}, "jitter": {}}
            for i in range(3)]  # fmt: skip
    (tmp_path / "summaries.jsonl").write_text(
        "".join(_json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    r = {"devices": [{"device": "rx", "ch": 1, "cells": [
        {"index": 0, "color": "green", "reasons": []},
        {"index": 1, "color": "yellow", "reasons": ["delay variation"]}]}]}  # fmt: skip
    (dev,) = page_data(tmp_path, r)["devices"]
    assert [c["color"] for c in dev["cells"]] == ["green", "yellow"]


def test_loss_bursts_join_a_blackout_with_the_second_that_counts_it() -> None:
    """GAP-147 (the reference board's loss bursts), the 2026-10-09 wireless run: 47 s with
    no packet, a good second, then the second carrying 45,517 lost is one burst; the
    heaviest bursts are tested for a period."""
    from controller.report_page import burst_view

    one = burst_view([[107.0, 48, 45517, 47]])
    assert (one["count"], one["lost"], one["period"]["tested"]) == (1, 45517, 0)
    steady = burst_view([[float(t), 1, 5, 0] for t in (100, 3743, 7386, 11030, 14673)])
    assert steady["period"]["fit"] == steady["period"]["tested"] == 3  # the heaviest 4: 3 gaps
    assert steady["period"]["seconds"] == 3643.0 and steady["gaps"]["median"] == 3643.0


def test_the_report_clock_starts_at_run_time_zero(tmp_path: Path) -> None:
    """2026-10-09: report times were 13 s late (run time added to the RUNNING moment)."""
    from controller.report_page import run_clock_zero

    (tmp_path / "events.log").write_text(
        '{"ts_utc": "2026-10-09T07:47:51.844Z", "run_s": 0.344, "text": "x"}\n',
        encoding="utf-8",
    )
    assert run_clock_zero(tmp_path) == "2026-10-09T07:47:51.500Z"
    assert run_clock_zero(tmp_path / "none") is None


def test_a_burst_carries_what_this_pc_counted(tmp_path: Path) -> None:
    """GAP-149: card drops/errors, UDP buffer and stream frames summed over a burst;
    a receiver with records and no trouble has 0 bursts, not "not here"."""
    import json as _json

    from controller.report_page import page_data

    raw = tmp_path / "raw" / "rx"
    raw.mkdir(parents=True)
    lines = [{"type": "s", "e": e, "ch": 1, "rx": 950, "lost": 0, "nd": 0, "ne": 0, "ub": 0,
              "fm": 0, "fu": 950} for e in range(10)]  # fmt: skip
    lines[4] = {**lines[4], "rx": 0, "fu": 0}
    lines[5] = {**lines[5], "lost": 9, "nd": 2}
    (raw / "R1_h000.jsonl").write_text("".join(_json.dumps(x) + "\n" for x in lines), "utf-8")
    (other := tmp_path / "raw" / "quiet").mkdir()
    (other / "R1_h000.jsonl").write_text(_json.dumps({"type": "s", "e": 0, "ch": 1, "rx": 9,
                                                       "lost": 0}) + "\n", "utf-8")  # fmt: skip
    bursts = page_data(tmp_path, {"devices": []})["bursts"]
    (b,) = bursts["rx|1"]["list"]
    assert b[1:4] == [2, 9, 1] and b[4] == {"nd": 2, "ne": 0, "ub": 0, "fm": 0, "fu": 950}
    assert bursts["quiet|1"]["count"] == 0
