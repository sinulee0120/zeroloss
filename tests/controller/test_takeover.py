"""The Controller takes a test up again after it was away (F11, S-56, GAP-130)."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from controller.devices import DeviceList
from controller.run import HOLD_MARGIN_S, WAKE_GRACE_S, RunController, RunState
from core.events.bus import Event, EventBus

REPO = Path(__file__).resolve().parents[2]


def controller(tmp_path: Path, settings: dict[str, str]) -> tuple[RunController, list[Event]]:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    rc = RunController(DeviceList(tmp_path / "devices.json"), tmp_path / "rep",
                       REPO / "modules", bus)  # fmt: skip
    rc.state = RunState.RUNNING
    rc._plan = SimpleNamespace(duration_s=3600)
    rc._raw_plan = {"settings": settings}
    rc._measure_start = time.monotonic() - 600
    return rc, seen


def test_the_hold_reaches_the_planned_end_and_a_margin(tmp_path: Path) -> None:
    rc, _ = controller(tmp_path, {})
    hold = rc._hold_s()
    assert hold is not None and abs(hold - (3000 + HOLD_MARGIN_S)) <= 2
    rc, _ = controller(tmp_path, {"S-56": "Stop after 10 s"})
    assert rc._hold_s() is None  # the old rule: no hold sent


def test_coming_back_gives_the_pcs_time_to_answer(tmp_path: Path) -> None:
    """After a sleep the first heartbeats may fail while Wi-Fi reconnects; the PCs'
    silence while we were away is ours and never counts as lost."""
    rc, seen = controller(tmp_path, {})
    w = SimpleNamespace(last_ok=time.monotonic() - 1800)  # 30 min ago: we were asleep
    rc._watch = {"rx": w}  # type: ignore[dict-item]
    rc._came_back(1800)
    silent = time.monotonic() - w.last_ok
    assert silent < 0 and silent + WAKE_GRACE_S - rc.lost_s >= -1  # lost only after the grace
    assert any("taking the test up again" in e.text for e in seen)


def test_a_stop_that_waited_says_for_what(tmp_path: Path) -> None:
    """2026-10-09: a Stop waited 2.5 min for the run loop with nothing said anywhere."""
    import threading

    import controller.run as run_mod

    bus = EventBus()
    seen: list[Any] = []
    bus.subscribe(seen.append)
    rc = RunController(DeviceList(tmp_path / "devices.json"), tmp_path / "rep",
                       REPO / "modules", bus)  # fmt: skip
    rc.state = RunState.RUNNING
    rc._move = lambda s: None  # type: ignore[method-assign,assignment]
    rc._finish = lambda safe: {"stopped": True}  # type: ignore[method-assign]
    rc._doing = "getting the 5-minute results"
    old, run_mod.STOP_WAIT_LOG_S = run_mod.STOP_WAIT_LOG_S, 0.2
    held = threading.Event()

    def busy_round() -> None:  # the run loop in a slow round
        with rc._lock:
            held.set()
            time.sleep(0.5)

    t = threading.Thread(target=busy_round)
    try:
        t.start()
        held.wait(2)
        assert rc.stop() == {"stopped": True}
        t.join()
    finally:
        run_mod.STOP_WAIT_LOG_S = old
    assert any("Stop waited" in e.text and "5-minute results" in e.text for e in seen)
    assert not rc._stop_wanted.is_set()


def test_sender_slow_counts_after_three_seconds_in_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-09 (the reference's bitrate floor needs 3 windows in a row): one slow
    second at -1.0 % made a box yellow twice in a 40-minute run."""
    rc = RunController(DeviceList(tmp_path / "devices.json"), tmp_path / "rep",
                       REPO / "modules", EventBus())  # fmt: skip

    class Rules:
        trouble_min_run = {"sender_slow": 3}  # noqa: RUF012

        @staticmethod
        def trouble_of(p: Any, device: str, code: str, data: Any) -> tuple[str, bool]:
            return "sender_slow", False

    rc._rules, rc._plan = Rules(), object()
    clock = [1000.0]
    monkeypatch.setattr("controller.run.time.monotonic", lambda: clock[0])
    if True:
        rc._note_trouble("tx", "ZL-SND-017", {})  # one slow second: nothing
        clock[0] += 5
        rc._note_trouble("tx", "ZL-SND-017", {})
        clock[0] += 1
        rc._note_trouble("tx", "ZL-SND-017", {})
        assert rc._windows.get("tx", {}).get("sender_slow") is None  # two in a row: nothing
        clock[0] += 1
        rc._note_trouble("tx", "ZL-SND-017", {})  # the third in a row
        (win,) = rc._windows["tx"]["sender_slow"]
        assert win[0] <= clock[0] - 3 and win[1] > clock[0]
