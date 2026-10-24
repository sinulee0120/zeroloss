"""Synthetic packet tests for seq_tracker (docs/10-verification.md 1.1)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from core.blocks.packet_synth import NS_PER_S, Pkt, delay, drop, stream
from core.blocks.seq_tracker import SeqTracker


def run(pkts: list[Pkt], window_ms: int = 1000) -> SeqTracker:
    t = SeqTracker(reorder_window_ms=window_ms)
    for p in pkts:
        t.on_packet(p.seq, p.ts, p.ssrc, p.arrival_ns)
    t.advance(pkts[-1].arrival_ns + 10 * NS_PER_S)
    t.close()
    return t


def test_clean_stream() -> None:
    s = run(stream(10_000)).stats
    assert (s.received, s.lost, s.duplicates, s.loss_events) == (10_000, 0, 0, 0)


def test_scattered_and_burst_loss() -> None:
    scattered = {100, 900, 1700, 2500, 3300, 4100, 4900}
    burst = set(range(6000, 6050))
    s = run(drop(stream(10_000), scattered | burst)).stats
    assert (s.lost, s.loss_events, s.max_burst) == (57, 8, 50)


def test_reorder_inside_window_is_recovered() -> None:
    pkts = stream(5000)
    for i in (1000, 2000, 3000):
        pkts = delay(pkts, i, 200_000_000)  # 200 ms late, window is 1000 ms
    s = run(pkts).stats
    assert (s.reordered_recovered, s.lost, s.late_discarded) == (3, 0, 0)


def test_arrival_after_window_is_late_and_lost() -> None:
    pkts = delay(stream(10_000), 1000, 1_500 * 1_000_000)  # 1.5 s late
    s = run(pkts).stats
    assert (s.late_discarded, s.lost, s.reordered_recovered) == (1, 1, 0)


def test_duplicates_are_not_loss() -> None:
    pkts = stream(3000)
    pkts = sorted([*pkts, pkts[500], pkts[501], pkts[2999]], key=lambda p: p.arrival_ns)
    s = run(pkts).stats
    assert (s.duplicates, s.lost) == (3, 0)


def test_ssrc_change_is_new_segment_not_loss() -> None:
    a = stream(3000, first_seq=100)
    b = stream(3000, first_seq=40_000, ssrc=0x01000002, start_ns=a[-1].arrival_ns + 1000)
    s = run(a + b).stats
    assert (s.lost, s.segment_restarts, s.received) == (0, 1, 6000)


def test_same_ssrc_jump_counts_as_loss() -> None:
    pkts = drop(stream(20_000), set(range(5000, 8000)))  # about 0.9 s outage
    s = run(pkts).stats
    assert (s.lost, s.loss_events, s.max_burst) == (3000, 1, 3000)


def test_long_outage_longer_than_one_seq_cycle() -> None:
    pkts = drop(stream(200_000), set(range(10_000, 110_000)))  # 100,000 lost (~30 s)
    s = run(pkts).stats
    assert s.lost == 100_000


def test_outage_of_exactly_half_and_two_cycles() -> None:
    for lost in (32_768, 131_072 + 5):
        pkts = drop(stream(lost + 20_000), set(range(10_000, 10_000 + lost)))
        assert run(pkts).stats.lost == lost


def test_sequence_and_timestamp_wraparound() -> None:
    pkts = stream(20_000, first_seq=65_000, first_ts=(1 << 32) - 50_000)
    s = run(pkts).stats
    assert (s.lost, s.duplicates, s.received) == (0, 0, 20_000)


def test_mid_stream_join() -> None:
    s = run(stream(5000, first_seq=50_000)).stats
    assert (s.lost, s.received) == (0, 5000)


def test_first_loss_after_clean_period() -> None:
    s = run(drop(stream(10_000), {7000})).stats
    assert (s.lost, s.loss_events) == (1, 1)


@settings(max_examples=60, deadline=None)
@given(
    dropped=st.sets(st.integers(1, 4998), max_size=200),
    moved=st.sets(st.integers(1, 4998), max_size=30),
)
def test_property_loss_equals_dropped(dropped: set[int], moved: set[int]) -> None:
    """Random drops plus reordering inside the window: lost == dropped exactly."""
    pkts = stream(5000)
    keep_moved = moved - dropped
    for i in keep_moved:
        pkts = delay(pkts, i, 300_000_000)
    pkts = [p for p in pkts if p.seq not in dropped]
    s = run(pkts).stats
    assert s.lost == len(dropped)
    assert s.late_discarded == 0
    assert s.received == 5000 - len(dropped)
