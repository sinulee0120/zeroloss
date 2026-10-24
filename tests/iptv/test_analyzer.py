"""IPTV stream analyzer (docs/10-verification.md 1.1 receiver rows, 05 2.1-2)."""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from modules.iptv.receiver.analyzer import StreamAnalyzer
from modules.iptv.rtp_synth import NS_PER_S, datagrams


def run(
    items: list[tuple[int, bytes, int]],
    seconds: int,
    playback: bool = False,
    drop: set[int] | None = None,
    frame_at_s: int = 1,
) -> tuple[StreamAnalyzer, list[dict[str, Any]]]:
    a = StreamAnalyzer(ch=1, join_ns=0, playback=playback)
    records = []
    it = iter(items)
    pending = next(it, None)
    for sec in range(1, seconds + 1):
        end = sec * NS_PER_S
        while pending is not None and pending[2] < end:
            i, data, t = pending
            if not drop or i not in drop:
                a.on_datagram(data, t)
            pending = next(it, None)
        rec = a.second(end, first_frame=sec >= frame_at_s)
        if rec:
            records.append(rec)
    last = a.finish(seconds * NS_PER_S + 1)
    if last:
        records.append(last)
    return a, records


def total(records: list[dict[str, Any]], key: str) -> int:
    return sum(r[key] for r in records)


def test_clean_stream_ready_after_three_seconds() -> None:
    a, recs = run(datagrams(20), 20)
    assert a.measuring and a.startup.warmup_seconds == 3
    assert recs[0]["e"] == 0 and recs[0]["rx"] == 500
    assert total(recs, "lost") == total(recs, "p1") == total(recs, "cc") == 0
    assert all(r["pat"] == 1 and r["ch"] == 1 for r in recs)
    assert recs[1]["br"] == 500 * (12 + 7 * 188) * 8
    st = a.startup.to_dict()
    assert st["join_to_first_packet_ms"] == 0.0 and st["join_to_ready_ms"] == 3000.0


def test_mid_stream_join_is_not_an_error() -> None:
    """First seq 50,000, first CC 9, PAT only 2 s after joining: no errors, later start."""
    a, recs = run(datagrams(20, first_seq=50_000, pat_after_s=2.0, start_cc=9), 20)
    assert a.measuring
    assert total(recs, "lost") == total(recs, "p1") == 0
    assert a.startup.warmup_seconds == 3  # PAT at 2 s, three clean seconds by 3 s


def test_first_loss_after_warmup_is_caught() -> None:
    _, recs = run(datagrams(20), 20, drop={5000})  # packet in second 10
    assert total(recs, "lost") == 1


def test_loss_inside_warmup_is_not_judged() -> None:
    a, recs = run(datagrams(20), 20, drop={100})  # second 0: gap expires in warm-up
    assert total(recs, "lost") == 0
    assert a.lost_total == 0  # warm-up loss is not counted
    # The gap is final only after the 1 s reorder window (in second 2), so the three
    # clean seconds are 3, 4 and 5.
    assert a.startup.warmup_seconds == 5


def test_no_pat_means_start_failure_with_reason() -> None:
    a, recs = run(datagrams(20, pat_after_s=99), 20)
    assert a.failed and recs == []
    assert a.startup.unmet == ["pat_pmt"]


def test_playback_needs_first_frame() -> None:
    a, _ = run(datagrams(20), 20, playback=True, frame_at_s=6)
    assert a.startup.warmup_seconds == 6


def test_non_rtp_datagrams_are_counted() -> None:
    a = StreamAnalyzer(1, 0, playback=False)
    a.on_datagram(b"\x47" * 1316, 1)
    assert a.not_rtp == 1


CLEAN_20S = datagrams(20)  # built once; the random test only drops from it


@settings(max_examples=60, deadline=None)
@given(drops=st.lists(st.tuples(st.integers(2500, 9400), st.integers(1, 30)), max_size=6))
def test_random_losses_after_warmup_are_counted_exactly(drops: list[tuple[int, int]]) -> None:
    """Invariant: after warm-up, lost in the records == packets removed; received adds up."""
    lost = set()
    for start, n in drops:
        lost |= set(range(start, start + n))
    items = CLEAN_20S
    a, recs = run(items, 20, drop=lost)
    assert a.measuring
    assert total(recs, "lost") == len(lost)
    assert a.lost_total == len(lost)  # heartbeat lost_total = sum of the records (02 5)
    assert total(recs, "p1") >= (1 if lost else 0)
    warm_packets = 3 * 500
    assert total(recs, "rx") == len(items) - len(lost) - warm_packets


def test_delay_range_follows_injected_delay() -> None:
    """A packet held back 45 ms shows up as a 45 ms transit range (DVB 40 ms p-p check)."""
    items = list(CLEAN_20S)
    i, data, t = items[5000]
    items[5000] = (i, data, t + 45_000_000)
    items.sort(key=lambda x: x[2])
    _, recs = run(items, 20)
    ranges = [r["dhi"] - r["dlo"] for r in recs if "dhi" in r]
    assert max(ranges) >= 44.9
    assert sorted(ranges)[len(ranges) // 2] < 1.0  # a perfect stream has no spread


def test_delay_range_crosses_timestamp_wraparound() -> None:
    from modules.iptv.rtp_synth import rtp

    a = StreamAnalyzer(1, 0, playback=False)
    start_ts = (1 << 32) - 900  # wraps after 10 ms
    for n in range(40):
        a.on_datagram(rtp(n, start_ts + n * 90, 7, b""), n * 1_000_000)
    rec_lo, rec_hi = a._second_lo, a._second_hi
    assert rec_lo is not None and rec_hi is not None and abs(rec_hi - rec_lo) < 0.001


def test_other_payload_types_are_kept_out_of_the_channel() -> None:
    """RFC 3551 / 05 3.2: RTP with PT != 33 is counted apart, never as this channel's
    packets (no loss, no duplicate, no jitter from it)."""
    items = datagrams(20)
    foreign = []
    for i, d, t in items[3000:3100]:
        x = bytearray(d)
        x[1] = 96  # another payload type on the same group and port
        x[2:4] = (40000 + i).to_bytes(2, "big")
        foreign.append((i, bytes(x), t + 1))
    a, recs = run(sorted(items + foreign, key=lambda x: x[2]), 20)
    assert a.other_pt == 100
    assert total(recs, "lost") == 0 and total(recs, "dup") == 0


def test_marker_bit_resets_the_delay_reference() -> None:
    """RFC 2250 2.1: an announced timestamp jump (M = 1) is not a delay change."""
    items = datagrams(20)
    shifted = []
    for i, d, t in items:
        if i >= 5000:
            x = bytearray(d)
            ts = (int.from_bytes(x[4:8], "big") + 900_000) % (1 << 32)  # +10 s
            x[4:8] = ts.to_bytes(4, "big")
            if i == 5000:
                x[1] |= 0x80  # M bit
            d = bytes(x)
        shifted.append((i, d, t))
    _, recs = run(shifted, 20)
    assert max(r.get("jit", 0) for r in recs) < 1.0
    assert max(r.get("dhi", 0) for r in recs) - min(r.get("dlo", 0) for r in recs) < 5.0


def skewed(
    seconds: int, ppm: float, bump: tuple[int, int, float] | None = None
) -> list[tuple[int, bytes, int]]:
    """50 pps stream whose arrival clock runs ``ppm`` fast; ``bump`` = (from s, to s,
    extra ms) adds a real queueing delay for a while."""
    out = []
    for i, d, t in datagrams(seconds, pps=50):
        t2 = int(t * (1 + ppm * 1e-6))
        if bump and bump[0] * NS_PER_S <= t < bump[1] * NS_PER_S:
            t2 += int(bump[2] * 1e6)
        out.append((i, d, t2))
    return out


def pp(recs: list[dict[str, Any]], lo_s: int, hi_s: int) -> float:
    cell = [r for r in recs if lo_s <= r["e"] < hi_s and "dlo" in r]
    return float(max(r["dhi"] for r in cell) - min(r["dlo"] for r in cell))


def test_clock_skew_is_estimated_and_removed() -> None:
    """05 2.2 (RFC 5481 7, RFC 3393 5.2): 50 ppm between the PCs would add 15 ms of
    delay variation to a 5-min cell; after correction it is gone, and the estimate
    is reported."""
    raw_a, raw = run(skewed(620, 50.0), 620)
    assert raw_a.skew_ppm is not None and abs(raw_a.skew_ppm - 50.0) < 1.0
    assert pp(raw, 300, 600) < 1.5  # was 15 ms without correction
    assert abs([r for r in raw if "sk" in r][-1]["sk"] - 50.0) < 1.0
    assert all("sk" not in r for r in raw if r["e"] < 119)  # needs 120 s of data


def test_skew_correction_keeps_a_real_delay_change() -> None:
    """A real 30 ms queueing bump for 10 s must stay visible (the fit uses the whole
    join segment, so minutes-long real changes are not taken for clock skew)."""
    _, recs = run(skewed(620, 50.0, bump=(400, 410, 30.0)), 620)
    assert pp(recs, 300, 600) > 28.0


def test_skew_beyond_500_ppm_is_not_corrected() -> None:
    a, recs = run(skewed(400, 800.0), 400)  # 2026-10-23: the cap is 500 ppm (T460 at 179)
    assert a.skew_ppm is not None and a.skew_ppm > 600  # reported
    assert pp(recs, 200, 390) > 100  # not taken for a clock: left as it is


def test_ts_without_rtp_is_detected() -> None:
    """TS 102 034 7.1.3: first byte 0x47 after UDP = MPEG-TS without RTP (RCV-022)."""
    a = StreamAnalyzer(1, 0, playback=False)
    for _, d, t in datagrams(2)[:100]:
        a.on_datagram(d[12:], t)  # strip the RTP header
    assert a.udp_ts == 100 and a.not_rtp == 100


def run_switch(
    items: list[tuple[int, bytes, int]], seconds: int, elapsed_from: int = 100
) -> tuple[StreamAnalyzer, list[dict[str, Any]]]:
    """A channel change: the analyzer starts at join 0 and gives up waiting (F19)."""
    a = StreamAnalyzer(ch=2, join_ns=0, playback=False, switch=True)
    recs: list[dict[str, Any]] = []
    it = iter(items)
    pending = next(it, None)
    for sec in range(1, seconds + 1):
        end = sec * NS_PER_S
        while pending is not None and pending[2] < end:
            a.on_datagram(pending[1], pending[2])
            pending = next(it, None)
        rec = a.second(end, elapsed=elapsed_from + len(recs))
        if rec:
            recs.append(rec)
    return a, recs


def test_switch_clean_stream_starts_like_a_normal_start() -> None:
    a, recs = run_switch(datagrams(20), 20)
    assert a.measuring and not a.join_failed and a.startup.warmup_seconds == 3
    assert recs[0]["e"] == 100 and recs[0]["ch"] == 2 and recs[0]["rx"] == 500


def test_switch_never_fails_it_starts_measuring_5s_after_the_first_packet() -> None:
    """PAT never comes: a normal start fails (RUN-009), a channel change measures anyway."""
    a, recs = run_switch(datagrams(20, pat_after_s=99.0), 20)
    assert a.measuring and not a.join_failed
    assert a.startup.unmet == ["pat_pmt"]
    assert a.startup.ready_ns == 5 * NS_PER_S  # first packet at 0, 5 s of stabilizing
    assert recs and recs[0]["pat"] == 0


def test_switch_without_a_first_packet_is_a_join_failure_with_empty_records() -> None:
    a, recs = run_switch([], 9)
    assert a.join_failed and a.measuring
    assert a.startup.unmet and "first_packet" in a.startup.unmet
    assert [r["e"] for r in recs] == [100, 101, 102, 103]  # ready at 5 s, then seconds 6..9
    assert all(r["rx"] == 0 and r["br"] == 0 for r in recs)


def test_switch_first_packet_at_4s_is_still_a_join() -> None:
    a, recs = run_switch(datagrams(20, start_ns=4 * NS_PER_S), 20)
    assert not a.join_failed and a.measuring
    assert a.startup.first_packet_ns == 4 * NS_PER_S and recs


def test_switch_first_packet_at_6s_is_a_join_failure() -> None:
    a, _ = run_switch(datagrams(20, start_ns=6 * NS_PER_S), 20)
    assert a.join_failed and a.measuring


def test_normal_start_still_fails_without_pat() -> None:
    a, recs = run(datagrams(20, pat_after_s=99.0), 20)
    assert not a.measuring and not recs and not a.join_failed


def test_a_late_tick_is_not_a_missing_pat() -> None:
    """2026-10-16 wireless run: the node wrote a 72 MB pcap, its receive loop fell ~0.7 s
    behind, and the tick found the last PAT "0.7 s ago" -- P1 PAT/PMT errors while the
    pcap showed every PAT on time (84 ms apart at most). Sections are timed by arrival."""
    a = StreamAnalyzer(ch=1, join_ns=0, playback=False)
    items = list(datagrams(20))
    k = 0
    pat = pmt = 0
    for sec in range(1, 21):
        end = sec * NS_PER_S
        fed_until = end - (NS_PER_S * 8 // 10 if sec == 12 else 0)  # second 12: 0.8 s behind
        while k < len(items) and items[k][2] < fed_until:
            a.on_datagram(items[k][1], items[k][2])
            k += 1
        rec = a.second(end, first_frame=True)
        if rec:
            pat, pmt = pat + rec.get("pate", 0), pmt + rec.get("pmte", 0)
    assert pat == 0 and pmt == 0
    # GAP-175: a stream that really stopped is rx 0, not P1 every second
    a.second(40 * NS_PER_S, first_frame=True)
    assert a.ts.counters.pat_err == 0 and a.ts.counters.pmt_err == 0
