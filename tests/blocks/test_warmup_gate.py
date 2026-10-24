"""Warm-up gate (docs/05-standards.md 2.1-2, docs/10-verification.md 1.1 mid-stream join)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.blocks.warmup_gate import Condition, GateState, WarmupGate
from modules.iptv.warmup import CHANNEL_CHANGE_MAX_S, START_MAX_S, make_gate, observe

GOOD = {"first_packet": True, "pat_pmt": True, "clean": True, "first_frame": True}


def test_ready_after_three_clean_seconds() -> None:
    g = make_gate(START_MAX_S, playback=True)
    assert g.on_second(1, GOOD) is GateState.WAITING
    assert g.on_second(2, GOOD) is GateState.WAITING
    assert g.on_second(3, GOOD) is GateState.READY
    assert g.ready_at == 3
    assert g.met_at["first_packet"] == 1


def test_loss_resets_clean_run() -> None:
    g = make_gate(START_MAX_S, playback=True)
    g.on_second(1, GOOD)
    g.on_second(2, GOOD)
    g.on_second(3, {**GOOD, "clean": False})
    for t in (4, 5):
        assert g.on_second(t, GOOD) is GateState.WAITING
    assert g.on_second(6, GOOD) is GateState.READY


def test_late_pat_and_first_frame_are_latched() -> None:
    g = make_gate(START_MAX_S, playback=True)
    g.on_second(1, {"first_packet": True, "clean": True})
    g.on_second(2, {"first_packet": False, "pat_pmt": True, "clean": True})
    assert g.on_second(3, {"clean": True, "first_frame": True}) is GateState.READY


def test_timeout_fails_and_names_unmet() -> None:
    g = make_gate(CHANNEL_CHANGE_MAX_S, playback=True)
    for t in range(1, 5):
        assert g.on_second(t, {**GOOD, "first_frame": False}) is GateState.WAITING
    assert g.on_second(5, {**GOOD, "first_frame": False}) is GateState.FAILED
    assert g.unmet() == ["first_frame"]


def test_ready_on_the_last_second_wins_over_timeout() -> None:
    g = WarmupGate([Condition("a", stable_s=3)], max_s=3)
    for t in (1, 2):
        g.on_second(t, {"a": True})
    assert g.on_second(3, {"a": True}) is GateState.READY


def test_playback_off_has_no_first_frame_condition() -> None:
    g = make_gate(START_MAX_S, playback=False)
    seen = observe(packets=3300, lost=0, jitter_max_ms=2.0, pat_pmt=True, first_frame=None)
    assert "first_frame" not in seen
    for t in (1, 2):
        g.on_second(t, seen)
    assert g.on_second(3, seen) is GateState.READY


def test_observe_jitter_limit() -> None:
    assert observe(1, 0, 150.0, True, True)["clean"]
    assert not observe(1, 0, 150.1, True, True)["clean"]
    assert not observe(1, 1, 0.0, True, True)["clean"]
    assert not observe(0, 0, 0.0, True, True)["clean"]


def test_rejects_unknown_names_and_calls_after_finish() -> None:
    g = make_gate(START_MAX_S, playback=False)
    with pytest.raises(ValueError):
        g.on_second(1, {"first_frame": True})
    g2 = WarmupGate([Condition("a")], max_s=1)
    g2.on_second(1, {"a": True})
    with pytest.raises(RuntimeError):
        g2.on_second(2, {"a": True})
    with pytest.raises(ValueError):
        WarmupGate([], max_s=1)


@settings(max_examples=200, deadline=None)
@given(
    seconds=st.lists(
        st.fixed_dictionaries({k: st.booleans() for k in GOOD}), min_size=1, max_size=40
    ),
    max_s=st.integers(min_value=3, max_value=30),
)
def test_random_seconds_match_reference(seconds: list[dict[str, bool]], max_s: int) -> None:
    """Invariant: the gate decides exactly like a direct reading of rule 2."""
    g = make_gate(max_s, playback=True)
    expected = GateState.WAITING
    for t, seen in enumerate(seconds, start=1):
        history = seconds[:t]
        latched = all(
            any(s[k] for s in history) for k in ("first_packet", "pat_pmt", "first_frame")
        )
        clean = t >= 3 and all(s["clean"] for s in history[-3:])
        if latched and clean:
            expected = GateState.READY
        elif t >= max_s:
            expected = GateState.FAILED
        assert g.on_second(t, seen) is expected
        if expected is not GateState.WAITING:
            break
    if g.state is GateState.READY:
        assert g.ready_at is not None and g.ready_at <= max_s
        assert g.unmet() == []
    if g.state is GateState.FAILED:
        assert g.unmet()


def test_force_ready_starts_measuring_and_keeps_the_unmet_list() -> None:
    g = make_gate(CHANNEL_CHANGE_MAX_S, playback=True)
    g.on_second(1, {**GOOD, "first_frame": False})
    g.force_ready(5.0)
    assert g.state is GateState.READY and g.ready_at == 5.0
    assert g.unmet() == ["clean", "first_frame"]  # one clean second of three, no frame


def test_force_ready_only_from_waiting() -> None:
    g = make_gate(CHANNEL_CHANGE_MAX_S, playback=True)
    for t in (1, 2, 3):
        g.on_second(t, GOOD)
    with pytest.raises(RuntimeError):
        g.force_ready(4.0)
