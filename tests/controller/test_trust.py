"""F25: measurement trust above the verdict (03 F25 완료 기준)."""

from __future__ import annotations

import random
from typing import Any

from controller.trust import CODE_CHECKS, line, measurement_trust


def cells(drops: int = 0, stamps: str = "kernel") -> dict[str, list[dict[str, Any]]]:
    return {"rx": [{"loss": {"host_drop_loss": drops}, "measure_method": {"timestamps": stamps}}]}


def problem(code: str, device: str = "rx", level: str = "warn") -> dict[str, Any]:
    return {"code": code, "level": level, "device": device}


def test_a_clean_run_is_trustworthy() -> None:
    t = measurement_trust({"problems": [], "devices": [{"device": "rx", "colors": {}}]}, cells())
    assert t["level"] == "ok" and all(c["ok"] for c in t["checks"])
    assert line(t) == "Measurement: ✔ Trustworthy"


def test_own_drops_and_a_silent_player_are_not_trustworthy() -> None:
    """F25 1-2: drops on the receiver itself (purple stays the verdict's), a player that
    does not play."""
    t = measurement_trust({"problems": [problem("ZL-RCV-020", "tab")]}, cells(drops=12))
    assert t["level"] == "bad"
    text = line(t) or ""
    assert text.startswith("Measurement: ✖ Not trustworthy")
    assert (
        "Receivers dropped packets themselves (rx)" in text and "Player not playing (tab)" in text
    )


def test_only_noted_problems_do_not_count_and_warnings_say_how_many() -> None:
    noted = {"problems": [problem("ZL-NET-015", level="info")]}  # a card not in the test
    assert measurement_trust(noted, cells())["level"] == "ok"
    t = measurement_trust({"problems": [problem("ZL-NET-016")],
                           "devices": [{"device": "tab", "colors": {"grey": 2}}]},
                          cells(stamps="user"))  # fmt: skip
    assert t["level"] == "warn" and line(t) == (
        "Measurement: ⚠ Check 3 items (Wi-Fi changed channel or access point (rx); "
        "Receive times from the user clock (rx); Cells with no data (tab))"
    )


def test_500_random_combinations_follow_the_rule_table() -> None:
    """F25 4: the level is the table's for any combination."""
    rnd = random.Random(25)
    for _ in range(500):
        codes = [c for _, _, cs, _, _ in CODE_CHECKS for c in cs if rnd.random() < 0.15]
        drops = rnd.choice([0, 0, 0, 5])
        grey = rnd.choice([0, 0, 0, 1])
        user = rnd.random() < 0.1
        t = measurement_trust(
            {"problems": [problem(c) for c in codes],
             "devices": [{"device": "rx", "colors": {"grey": grey}}]},
            cells(drops, "user" if user else "kernel"),
        )  # fmt: skip
        bad = drops > 0 or any(
            c in cs for _, lv, cs, _, _ in CODE_CHECKS if lv == "bad" for c in codes)  # fmt: skip
        warn = grey > 0 or user or any(
            c in cs for _, lv, cs, _, _ in CODE_CHECKS if lv == "warn" for c in codes)  # fmt: skip
        assert t["level"] == ("bad" if bad else "warn" if warn else "ok"), (codes, drops, grey)


def test_tablets_time_in_the_app_and_a_moment_behind_is_only_a_check() -> None:
    """2026-11-28 on the real runs: an Android app has no kernel receive times (every tablet
    run warned), and one slow second cleared at once made a 3-hour run "not trustworthy"."""
    tab = {"tab": [{"measure_method": {"timestamps": "app"}}]}
    t = measurement_trust({"problems": []}, tab, tablets={"tab"})
    assert t["level"] == "ok" and "tablets: the app's clock" in next(
        c["text"] for c in t["checks"] if c["id"] == "clock"
    )
    late = measurement_trust({"problems": [problem("ZL-SND-025", "tx")]}, cells())
    assert late["level"] == "warn" and "Sending fell behind for a moment (tx)" in (line(late) or "")
    stopped = measurement_trust({"problems": [problem("ZL-SND-005", "tx", "error")]}, cells())
    assert stopped["level"] == "bad"


def test_the_top_bar_mark_while_a_test_runs() -> None:
    """F25 "시험 중 화면: 상단 막대에 같은 표시(작게)" (2026-11-28)."""
    from controller.ui.store import Status
    from controller.ui.view import trust_mark

    idle = Status(True, run={"state": "IDLE"})
    assert trust_mark(idle) is None
    running = Status(True, run={"state": "RUNNING"}, problems={"problems": []})
    assert trust_mark(running)[:2] == ("✔ OK", "green")  # type: ignore[index]
    late = Status(True, run={"state": "RUNNING"},
                  problems={"problems": [problem("ZL-SND-025", "tx")]})  # fmt: skip
    mark = trust_mark(late)
    assert mark is not None and mark[:2] == ("⚠ 1", "amber") and "fell behind" in mark[2]
    dead = Status(True, run={"state": "RUNNING"},
                  problems={"problems": [problem("ZL-RCV-020", "rx", "error")]})  # fmt: skip
    assert trust_mark(dead)[:2] == ("✖ Not OK", "red")  # type: ignore[index]
