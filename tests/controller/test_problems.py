"""Problems (03 F21, 02 2-2): catalog = 09 codes, one row per code+device, clears,
chattering, flood, stale, run end, bounded memory."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from controller.problems import CATALOG, ProblemBoard
from controller.ui.texts import BANNED
from core.events.bus import Event

ROOT = Path(__file__).resolve().parents[2]


def codes_in_09() -> set[str]:
    text = (ROOT / "docs" / "09-exceptions.md").read_text(encoding="utf-8")
    return {"ZL-" + m for m in re.findall(r"^\| ([A-Z]{2,4}-\d{3}) \|", text, re.M)}


def test_catalog_matches_09_exactly() -> None:
    """F21 completion: every 09 code has What/Why/Fix; no code that 09 does not have."""
    assert set(CATALOG) == codes_in_09()
    for code, e in CATALOG.items():
        assert e["what"] and e["why"] and e["fix"], code


def test_catalog_is_plain_english_screen_text() -> None:
    """11-ui 1.7 / 1.7-1: English only, banned words never alone."""
    allowed_before = ("Video ", "Screen ", "Wi-Fi ", "TV ")
    for code, e in CATALOG.items():
        for field in ("what", "why", "fix"):
            text = e[field]
            assert not re.search(r"[가-힣]", text), (code, text)  # no Korean
            for word in BANNED:
                for m in re.finditer(rf"\b{word}\b", text, re.I):
                    assert text[: m.start()].endswith(allowed_before), (code, field, text)
        assert e["fix"].rstrip().endswith((".", ")")), code  # ends with an action


def ev(
    code: str, level: str = "error", device: str = "rx", text: str = "", **data: object
) -> Event:
    return Event(code, level, text or code, device=device, data=dict(data))  # type: ignore[arg-type]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def board() -> tuple[ProblemBoard, Clock]:
    c = Clock()
    return ProblemBoard(clock=c), c


def test_one_row_per_code_and_device_with_count() -> None:
    b, c = board()
    for _ in range(3):
        b(ev("ZL-NET-013", text="link 100 Mbps"))
        c.t += 60
    b(ev("ZL-NET-013", device="tx"))
    b(ev("ZL-INFO", level="info"))  # information is not a problem
    rows = b.snapshot()["problems"]
    assert [(r["device"], r["count"]) for r in rows] == [("tx", 1), ("rx", 3)]
    r = rows[1]
    assert r["what"] == "link 100 Mbps" and r["fix"] == CATALOG["ZL-NET-013"]["fix"]
    assert b.snapshot()["counts"] == {"error": 2, "warn": 0}


def test_event_why_fix_win_over_the_catalog() -> None:
    b, _ = board()
    b(ev("ZL-NET-013", why="needs 116 Mbps", fix="Replace the cable."))
    r = b.snapshot()["problems"][0]
    assert (r["why"], r["fix"]) == ("needs 116 Mbps", "Replace the cable.")


def test_clears_move_it_to_resolved_and_chattering_is_one_row() -> None:
    b, c = board()
    b(ev("ZL-NET-013"))
    c.t += 5
    b(ev("ZL-INFO", level="info", clears=["ZL-NET-013", "ZL-NET-014"]))
    r = b.snapshot()["problems"][0]
    assert not r["active"] and r["cleared_utc"]
    c.t += 3  # back within 10 s: the same row again, not a new alarm
    b(ev("ZL-NET-013"))
    s = b.snapshot()
    assert len(s["problems"]) == 1 and s["problems"][0]["active"] and s["problems"][0]["count"] == 2
    assert b.new_in(600) == 1


def test_run_end_and_reset_resolve_everything() -> None:
    b, _ = board()
    b(ev("ZL-NET-013"))
    b(ev("ZL-RCV-020", level="warn"))
    b.resolve_all("test ended")
    assert all(not r["active"] for r in b.snapshot()["problems"])
    assert b.snapshot()["counts"] == {"error": 0, "warn": 0}


def test_flood_summary_and_stale() -> None:
    b, c = board()
    for i in range(12):
        b(ev("ZL-RCV-020", level="warn", device=f"d{i}"))
        c.t += 10
    assert b.snapshot()["flood"] == {"new_in_10_min": 12}
    c.t += 601
    assert b.snapshot()["flood"] is None
    c.t += 24 * 3600
    assert all(r["stale"] for r in b.snapshot()["problems"] if r["active"])


def test_unknown_code_still_shows() -> None:
    b, _ = board()
    b(ev("ZL-XYZ-999", text="Something odd"))
    r = b.snapshot()["problems"][0]
    assert r["what"] == "Something odd" and r["why"] == "Unknown cause"
    assert "Export diagnostics" in r["fix"]


def test_random_events_keep_invariants_and_memory_bounded() -> None:
    """Monkey: rows unique by key, active first, counts match, resolved rows bounded."""
    rnd = random.Random(3)
    b, c = board()
    codes = list(CATALOG)[:20]
    for _ in range(4000):
        c.t += rnd.choice([0.1, 1, 5, 30])
        k = rnd.random()
        code, dev = rnd.choice(codes), f"d{rnd.randrange(60)}"
        if k < 0.6:
            b(ev(code, level=rnd.choice(["warn", "error"]), device=dev))
        elif k < 0.95:
            b(ev("ZL-INFO", level="info", device=dev, clears=[code]))
        else:
            b.resolve_all("test ended")
        s = b.snapshot()
        rows = s["problems"]
        keys = [r["key"] for r in rows]
        assert len(keys) == len(set(keys))
        flags = [r["active"] for r in rows]
        assert flags == sorted(flags, reverse=True)  # active first
        act = [r for r in rows if r["active"]]
        assert s["counts"]["error"] == sum(r["level"] == "error" for r in act)
        assert sum(not r["active"] for r in rows) <= 500
    json.dumps(b.snapshot())  # the answer is plain JSON


def test_run_report_lists_the_runs_problems_and_alarm_stats() -> None:
    """F21: the report's "Problems during the test" and alarm counts (02 10)."""
    b, c = board()
    b(Event("ZL-NET-013", "error", "old", device="tx", data={"run_id": "R0"}))
    for _ in range(3):
        b(Event("ZL-RCV-020", "warn", "not playing", device="rx", data={"run_id": "R1"}))
        c.t += 5
        b(Event("ZL-INFO", "info", "ok", device="rx", data={"clears": ["ZL-RCV-020"]}))
        c.t += 300  # far apart: each is a new alarm
    b(Event("ZL-DSC-014", "warn", "no answer", device="tx", data={"run_id": "R1"}))
    rep = b.run_report("R1", duration_s=1200)
    assert [(p["code"], p["count"], p["open_at_end"]) for p in rep["problems"]] == [
        ("ZL-DSC-014", 1, True),
        ("ZL-RCV-020", 3, False),
    ]
    s = rep["alarm_stats"]
    assert s["total"] == 4 and s["per_10_min_max"] == 2 and s["flood_time_ratio"] == 0.0
    assert s["top"][0] == {"code": "ZL-RCV-020", "device": "rx", "count": 3}
    assert abs(s["per_10_min_avg"] - 4 / 2) < 1e-9  # 4 alarms over 1200 s = 2 windows


def test_flood_time_ratio_and_speed_over_100_hours() -> None:
    """A 20-minute flood in a 100 h run: ratio right, and the report stays quick."""
    import time as _t

    b, c = board()
    start = c.t
    c.t = start + 3600
    for i in range(300):  # 300 new alarms within 20 min
        b(Event("ZL-RCV-020", "warn", "x", device=f"d{i}", data={"run_id": "R"}))
        c.t += 4
    c.t = start + 100 * 3600
    t0 = _t.monotonic()
    s = b.run_report("R", duration_s=100 * 3600)["alarm_stats"]
    assert _t.monotonic() - t0 < 2.0
    assert s["total"] == 300 and s["per_10_min_max"] == 151  # 0..600 s both ends included
    assert 0.003 < s["flood_time_ratio"] < 0.01  # about 30 min of 100 h
