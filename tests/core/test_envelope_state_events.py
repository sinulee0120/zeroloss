"""Envelope rules (02 3), state flow (RUN-012), event bus (12 4-1)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.events.bus import Event, EventBus, EventLog, RecentEvents
from core.lifecycle.state_flow import ALLOWED, State, StateFlow
from core.protocol.envelope import CommandGuard, Request, ZlError, new_command

# ------------------------------------------------------------------ envelope


def test_parse_valid_and_invalid_requests() -> None:
    r = Request.parse({"command_id": "c1", "generation": 2, "run_id": "R1", "params": {"a": 1}})
    assert (r.command_id, r.generation, r.params) == ("c1", 2, {"a": 1})
    bad: object
    for bad in (
        [],
        {},
        {"command_id": "", "generation": 0},
        {"command_id": "c", "generation": -1},
        {"command_id": "c", "generation": True},
        {"command_id": "c", "generation": 0, "params": []},
        {"command_id": "c", "generation": 0, "run_id": 5},
    ):
        with pytest.raises(ZlError) as e:
            Request.parse(bad)
        assert e.value.code == "ZL-DSC-012"


def test_same_command_runs_once() -> None:
    g = CommandGuard()
    calls = []
    req = new_command(0, x=1)

    def handler(r: Request) -> dict[str, int]:
        calls.append(r)
        return {"n": len(calls)}

    first = g.run(req, handler)
    again = g.run(req, handler)
    assert first == again == {"ok": True, "data": {"n": 1}}
    assert len(calls) == 1


def test_error_result_is_remembered_too() -> None:
    g = CommandGuard()
    req = new_command(0)

    def boom(_: Request) -> None:
        raise ZlError("RUN-012", "no")

    assert g.run(req, boom)["error"]["code"] == "ZL-RUN-012"
    assert g.run(req, lambda r: {"x": 1})["ok"] is False


def test_old_generation_refused_newest_becomes_current() -> None:
    g = CommandGuard()
    assert g.run(new_command(3), lambda r: None)["ok"]
    assert g.generation == 3
    res = g.run(new_command(2), lambda r: None)
    assert res["error"]["code"] == "ZL-DSC-011"
    assert g.bump_generation() == 4
    assert g.run(new_command(3), lambda r: None)["ok"] is False


def test_controller_restart_detected_once() -> None:
    g = CommandGuard()
    assert g.controller_boot("A") is False  # first contact
    assert g.controller_boot("A") is False
    assert g.controller_boot("B") is True  # new boot ID: Controller restarted
    assert g.controller_boot("B") is False
    assert g.controller_boot(None) is False


def test_concurrent_repeats_run_once_and_unexpected_errors_do_not_hang() -> None:
    g = CommandGuard()
    req = new_command(0)
    calls = []
    started = threading.Event()

    def slow(_: Request) -> dict[str, Any]:
        calls.append(1)
        started.set()
        time.sleep(0.2)
        return {"done": True}

    results: list[dict[str, Any]] = []
    threads = [threading.Thread(target=lambda: results.append(g.run(req, slow))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(calls) == 1 and len(results) == 8
    assert all(r == {"ok": True, "data": {"done": True}} for r in results)

    req2 = new_command(0)

    def crash(_: Request) -> None:
        started.set()
        time.sleep(0.1)
        raise ValueError("bug")

    waiter_result: list[dict[str, Any]] = []
    started.clear()

    def first_caller() -> None:
        with pytest.raises(ValueError):
            g.run(req2, crash)

    t1 = threading.Thread(target=first_caller)
    t1.start()
    started.wait(2)
    t2 = threading.Thread(target=lambda: waiter_result.append(g.run(req2, lambda r: {"ok2": 1})))
    t2.start()
    t1.join(5)
    t2.join(5)
    assert not t2.is_alive()
    assert waiter_result == [{"ok": True, "data": {"ok2": 1}}]


def test_remembered_results_are_bounded() -> None:
    g = CommandGuard(remember=10)
    for _ in range(50):
        g.run(new_command(0), lambda r: None)
    assert len(g._results) == 10


# ------------------------------------------------------------------ state flow


def test_normal_cycle_and_refused_moves() -> None:
    seen: list[tuple[State, State]] = []
    f = StateFlow("sender", lambda n, a, b, r: seen.append((a, b)))
    f.move(State.STARTING)
    with pytest.raises(ZlError) as e:
        f.move(State.STARTING)  # duplicate start
    assert e.value.code == "ZL-RUN-012"
    f.move(State.RUNNING)
    with pytest.raises(ZlError):
        f.move(State.IDLE)  # must stop first
    f.move(State.STOPPING)
    with pytest.raises(ZlError):
        f.move(State.STARTING)  # no new start while stopping
    f.move(State.IDLE)
    assert seen == [
        (State.IDLE, State.STARTING),
        (State.STARTING, State.RUNNING),
        (State.RUNNING, State.STOPPING),
        (State.STOPPING, State.IDLE),
    ]


def test_fault_from_anywhere_left_only_by_stop() -> None:
    f = StateFlow("x")
    f.move(State.STARTING)
    f.move(State.FAULT)
    with pytest.raises(ZlError):
        f.move(State.IDLE)
    f.move(State.STOPPING)
    f.move(State.IDLE)


def test_expect_guards_stale_view() -> None:
    f = StateFlow("x")
    f.move(State.STARTING)
    with pytest.raises(ZlError):
        f.move(State.STOPPING, expect=State.RUNNING)
    assert f.state is State.STARTING


@settings(max_examples=300, deadline=None)
@given(moves=st.lists(st.sampled_from(list(State)), max_size=30))
def test_random_moves_follow_table(moves: list[State]) -> None:
    """Invariant: a move succeeds exactly when the table allows it; refused moves
    change nothing."""
    f = StateFlow("x")
    for to in moves:
        before = f.state
        allowed = to is not before and (to is State.FAULT or to in ALLOWED[before])
        if allowed:
            f.move(to)
            assert f.state is to
        else:
            with pytest.raises(ZlError):
                f.move(to)
            assert f.state is before


def test_racing_starts_only_one_wins() -> None:
    f = StateFlow("x")
    wins = []

    def go() -> None:
        try:
            f.move(State.STARTING)
            wins.append(1)
        except ZlError:
            pass

    threads = [threading.Thread(target=go) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1


# ------------------------------------------------------------------ event bus


def test_events_reach_log_and_heartbeat_list(tmp_path: Path) -> None:
    bus = EventBus()
    log = EventLog(tmp_path / "events.log")
    recent = RecentEvents(keep=3)
    bus.subscribe(log)
    bus.subscribe(recent)
    for i in range(5):
        bus.publish(Event("ZL-INFO", "info", f"e{i}", device="pc", elapsed_s=float(i)))
    log.close()
    lines = [json.loads(x) for x in (tmp_path / "events.log").read_text().splitlines()]
    assert [x["text"] for x in lines] == [f"e{i}" for i in range(5)]
    assert set(lines[0]) == {"code", "level", "text", "device", "elapsed_s", "ts_utc"}
    assert [e.text for e in recent.take()] == ["e2", "e3", "e4"]
    assert recent.dropped == 2
    assert recent.take() == []


def test_broken_subscriber_is_reported_and_others_still_get_events() -> None:
    bus = EventBus()
    got: list[Event] = []

    def broken(e: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe(broken)
    bus.subscribe(got.append)
    bus.publish(Event("ZL-INFO", "info", "hello"))
    assert [e.code for e in got] == ["ZL-SYS-008", "ZL-INFO"]


def test_unsubscribe() -> None:
    bus = EventBus()
    got: list[Event] = []
    off = bus.subscribe(got.append)
    off()
    off()
    bus.publish(Event("ZL-INFO", "info", "x"))
    assert got == []
