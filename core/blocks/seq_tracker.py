"""Sequence tracker: loss, duplicates, reordering with a time window, late packets.

Rules: docs/05-standards.md 2.1 (RFC 3550 A.3, RFC 7003 late packets, RFC 3357 bursts).

- A gap is not lost at once. Missing numbers wait in a reorder window (default 1,000 ms).
  Arriving inside the window -> reordered_recovered (not an error).
  Window expired -> lost. Arriving after that -> late_discarded (stays counted as lost).
- A new SSRC starts a new segment (sender restart); it is never counted as loss.
- Same SSRC with a big forward jump is real loss. Outages longer than one 16-bit cycle are
  resolved with the RTP timestamp and the recent packet rate.

Pure logic: the caller passes arrival times, so tests run without a network or clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEQ_MOD = 1 << 16
TS_MOD = 1 << 32
NS_PER_MS = 1_000_000
RTP_CLOCK_HZ = 90_000


@dataclass
class Burst:
    """One run of consecutive lost packets."""

    start_ext_seq: int
    length: int
    detected_ns: int


@dataclass
class SeqStats:
    received: int = 0
    lost: int = 0
    duplicates: int = 0
    reordered_recovered: int = 0
    late_discarded: int = 0
    loss_events: int = 0
    max_burst: int = 0
    segment_restarts: int = 0
    bursts: list[Burst] = field(default_factory=list)


@dataclass
class _Gap:
    first: int
    last: int
    deadline_ns: int
    detected_ns: int
    missing: set[int]


class SeqTracker:
    def __init__(self, reorder_window_ms: int = 1000, keep_bursts: int = 1000) -> None:
        self._period_max_burst = 0
        self._window_ns = reorder_window_ms * NS_PER_MS
        self._keep_bursts = keep_bursts
        self.stats = SeqStats()
        self._ssrc: int | None = None
        self._reset_segment()

    def _reset_segment(self) -> None:
        self._max_ext: int | None = None
        self._max_ts: int = 0
        self._seen: set[int] = set()
        self._lost_final: set[int] = set()
        self._gaps: list[_Gap] = []
        # Recent packet rate for long-outage estimation (packets per RTP tick).
        self._rate_ref: tuple[int, int] | None = None  # (ext_seq, ts_unwrapped)
        self._ts_unwrapped: int = 0
        self._pkts_per_tick: float | None = None

    # ------------------------------------------------------------------ input

    def on_packet(self, seq: int, rtp_ts: int, ssrc: int, arrival_ns: int) -> None:
        """Feed one RTP packet in arrival order."""
        self.advance(arrival_ns)
        if self._ssrc is None:
            self._ssrc = ssrc
        elif ssrc != self._ssrc:
            self._close_gaps()
            self._ssrc = ssrc
            self._reset_segment()
            self.stats.segment_restarts += 1

        if self._max_ext is None:
            self._start(seq, rtp_ts)
            return

        max_seq = self._max_ext % SEQ_MOD
        delta = (seq - max_seq) % SEQ_MOD
        ts_step = self._ts_step(rtp_ts)
        advance = self._long_outage_advance(delta, ts_step)
        if advance is not None:
            self._forward(advance, rtp_ts, ts_step, arrival_ns, long_outage=True)
        elif delta == 0:
            self._old_packet(self._max_ext)
        elif delta < SEQ_MOD // 2:
            self._forward(delta, rtp_ts, ts_step, arrival_ns, long_outage=False)
        else:
            self._old_packet(self._max_ext - (SEQ_MOD - delta))

    def advance(self, now_ns: int) -> None:
        """Finalize gaps whose reorder window has expired. Call at least once per second."""
        while self._gaps and self._gaps[0].deadline_ns <= now_ns:
            self._finalize(self._gaps.pop(0))

    def close(self) -> None:
        """End of measurement: anything still missing is lost."""
        self._close_gaps()

    # --------------------------------------------------------------- helpers

    def _start(self, seq: int, rtp_ts: int) -> None:
        self._max_ext = seq
        self._max_ts = rtp_ts
        self._ts_unwrapped = rtp_ts
        self._rate_ref = (seq, rtp_ts)
        self._mark_received(seq)

    def _ts_step(self, rtp_ts: int) -> int:
        """Forward RTP timestamp distance; 0 if the timestamp went backwards."""
        step = (rtp_ts - self._max_ts) % TS_MOD
        return 0 if step >= TS_MOD // 2 else step

    def _long_outage_advance(self, delta: int, ts_step: int) -> int | None:
        """Sequence advance when elapsed time says we skipped half a cycle or more.

        16-bit numbers alone cannot tell direction or cycles after a long gap, so the RTP
        timestamp and the recent packet rate pick the advance closest to the expected one.
        """
        if not self._pkts_per_tick:
            return None
        expected = ts_step * self._pkts_per_tick
        if expected < SEQ_MOD // 2:
            return None
        cycles = round((expected - delta) / SEQ_MOD)
        return delta + max(0, cycles) * SEQ_MOD

    def _forward(
        self, advance: int, rtp_ts: int, ts_step: int, arrival_ns: int, long_outage: bool
    ) -> None:
        assert self._max_ext is not None
        new_ext = self._max_ext + advance
        if new_ext > self._max_ext + 1:
            missing_first, missing_last = self._max_ext + 1, new_ext - 1
            self._gaps.append(
                _Gap(
                    first=missing_first,
                    last=missing_last,
                    deadline_ns=arrival_ns + self._window_ns,
                    detected_ns=arrival_ns,
                    missing=set(range(missing_first, missing_last + 1)),
                )
            )
        self._max_ext = new_ext
        self._max_ts = rtp_ts
        self._ts_unwrapped += ts_step
        self._update_rate(new_ext, self._ts_unwrapped, long_outage)
        self._mark_received(new_ext)

    def _update_rate(self, ext: int, ts_unwrapped: int, long_outage: bool) -> None:
        assert self._rate_ref is not None
        if long_outage:
            self._rate_ref = (ext, ts_unwrapped)  # keep the pre-outage rate, restart the sample
            return
        ref_ext, ref_ts = self._rate_ref
        ticks = ts_unwrapped - ref_ts
        if ticks >= RTP_CLOCK_HZ:  # refresh about once per second
            self._pkts_per_tick = (ext - ref_ext) / ticks
            self._rate_ref = (ext, ts_unwrapped)

    def _old_packet(self, ext: int) -> None:
        if ext in self._seen:
            self.stats.duplicates += 1
            return
        for gap in self._gaps:
            if gap.first <= ext <= gap.last and ext in gap.missing:
                gap.missing.discard(ext)
                self.stats.reordered_recovered += 1
                self._mark_received(ext)
                return
        if ext in self._lost_final:
            self._lost_final.discard(ext)
            self.stats.late_discarded += 1
            self._seen.add(ext)
            return
        # Older than anything we track (before the join point): ignore, not an error.

    def _mark_received(self, ext: int) -> None:
        self._seen.add(ext)
        self.stats.received += 1
        self._prune()

    def _prune(self) -> None:
        if self._max_ext is None or len(self._seen) < 2 * SEQ_MOD:
            return
        floor = self._max_ext - SEQ_MOD
        self._seen = {s for s in self._seen if s >= floor}
        self._lost_final = {s for s in self._lost_final if s >= floor}

    def _close_gaps(self) -> None:
        while self._gaps:
            self._finalize(self._gaps.pop(0))

    def _finalize(self, gap: _Gap) -> None:
        if not gap.missing:
            return
        self.stats.lost += len(gap.missing)
        self._lost_final.update(gap.missing)
        run_start: int | None = None
        run_len = 0
        for s in range(gap.first, gap.last + 2):
            if s <= gap.last and s in gap.missing:
                if run_start is None:
                    run_start = s
                run_len += 1
            elif run_start is not None:
                self._add_burst(Burst(run_start, run_len, gap.detected_ns))
                run_start, run_len = None, 0

    def take_period_max_burst(self) -> int:
        """Longest burst finalized since the last call (per-second records, 02 7 ``mb``)."""
        out, self._period_max_burst = self._period_max_burst, 0
        return out

    def _add_burst(self, burst: Burst) -> None:
        self.stats.loss_events += 1
        self.stats.max_burst = max(self.stats.max_burst, burst.length)
        self._period_max_burst = max(self._period_max_burst, burst.length)
        if len(self.stats.bursts) < self._keep_bursts:
            self.stats.bursts.append(burst)
