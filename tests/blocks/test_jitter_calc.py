"""Jitter must match the RFC 3550 formula within +-0.1 ms (docs/10-verification.md 1.1)."""

from __future__ import annotations

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from core.blocks.jitter_calc import JitterCalc
from core.blocks.packet_synth import NS_PER_S, stream


def reference_jitter_ms(ts: list[int], arrival_ns: list[int]) -> float:
    """Straight transcription of RFC 3550 A.8 in floating point, for comparison."""
    j = 0.0
    for i in range(1, len(ts)):
        r = (arrival_ns[i] - arrival_ns[i - 1]) * 90_000 / NS_PER_S
        s = ts[i] - ts[i - 1]
        j += (abs(r - s) - j) / 16
    return j / 90


def test_perfect_stream_has_zero_jitter() -> None:
    calc = JitterCalc()
    for p in stream(5000):
        calc.on_packet(p.ts, p.ssrc, p.arrival_ns)
    assert calc.jitter_ms < 0.01


def test_constant_offset_converges_to_known_value() -> None:
    # Every other packet arrives 4 ms late: |D| alternates 4 ms -> J converges to 4 ms.
    calc = JitterCalc()
    for i, p in enumerate(stream(20_000)):
        calc.on_packet(p.ts, p.ssrc, p.arrival_ns + (4_000_000 if i % 2 else 0))
    assert abs(calc.jitter_ms - 4.0) < 0.1


def test_timestamp_wraparound_is_not_a_spike() -> None:
    calc = JitterCalc()
    for p in stream(10_000, first_ts=(1 << 32) - 30_000):
        calc.on_packet(p.ts, p.ssrc, p.arrival_ns)
    assert calc.jitter_ms < 0.01


def test_ssrc_change_restarts_estimator() -> None:
    calc = JitterCalc()
    for i, p in enumerate(stream(5000)):
        calc.on_packet(p.ts, p.ssrc, p.arrival_ns + (8_000_000 if i % 2 else 0))
    assert calc.jitter_ms > 5
    calc.on_packet(123, 0x01000002, 10 * NS_PER_S)
    assert calc.jitter_ms == 0.0


def test_period_max_is_reset_after_take() -> None:
    calc = JitterCalc()
    pkts = stream(4000)
    for i, p in enumerate(pkts[:2000]):
        calc.on_packet(p.ts, p.ssrc, p.arrival_ns + (20_000_000 if i == 1000 else 0))
    first = calc.take_period_max_ms()
    for p in pkts[2000:]:
        calc.on_packet(p.ts, p.ssrc, p.arrival_ns)
    second = calc.take_period_max_ms()
    assert first > second


@settings(max_examples=50, deadline=None)
@given(seed=st.integers(0, 2**32 - 1), spread_ms=st.floats(0.0, 200.0))
def test_matches_rfc_reference(seed: int, spread_ms: float) -> None:
    rng = random.Random(seed)
    pkts = stream(3000)
    arrivals = [p.arrival_ns + int(rng.uniform(0, spread_ms) * 1_000_000) for p in pkts]
    calc = JitterCalc()
    for p, a in zip(pkts, arrivals, strict=True):
        calc.on_packet(p.ts, p.ssrc, a)
    expected = reference_jitter_ms([p.ts for p in pkts], arrivals)
    assert abs(calc.jitter_ms - expected) <= 0.1


def test_marker_bit_timestamp_jump_is_not_network_jitter() -> None:
    """RFC 2250 2.1: M = 1 marks a timestamp discontinuity; the receiver ignores that
    difference. Without M the same jump is real (a sender fault shows as jitter)."""
    pps, step = 500, 180  # 90 kHz / 500 pps
    for marker, expect_spike in [(True, False), (False, True)]:
        j = JitterCalc()
        for i in range(200):
            ts = i * step + (5_000_000 if i >= 100 else 0)  # jump of ~55 s at packet 100
            j.on_packet(ts, 1, i * NS_PER_S // pps, discontinuity=marker and i == 100)
        assert (j.take_period_max_ms() > 1000) is expect_spike, marker
