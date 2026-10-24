"""IPTV stream analyzer: one channel join, pure logic (docs/05-standards.md 2, 4.2).

Datagrams go in with their arrival time; once per second ``second()`` returns the
1-second record (docs/02-data.md 7) or None while warming up.

- Warm-up (2.1-2): the first packet is the reference for sequence, CC and timestamps.
  Nothing is judged until the gate is READY; then ``e`` = 0 starts. The warm-up itself
  is kept in ``startup`` for the report's Start-up item. If the gate fails, ``failed``
  says which conditions were not met (RUN-009).
- Channel change (``switch=True``, 05 2.1-2 rule 4, 20 GAP-90): the gate never fails.
  No first packet 5 s after the join = ``join_failed`` (NET-003); otherwise measuring
  starts at the latest 5 s after the first packet. Either way ``unmet`` says what was
  not met, and records follow (a failed join gives received 0, so the cell turns red).
- After warm-up every problem counts, including gaps first seen during warm-up that
  expire later (rule 7: warm-up never hides a problem).
- Non-RTP datagrams in RTP mode are counted (``not_rtp``), not silently dropped.
- Relative transit delay (arrival - RTP timestamp, relative to the first packet of the
  stream; restarts on a new SSRC) gives each second's low/high (``dlo``/``dhi``); the
  cell range max(dhi) - min(dlo) is the DVB 40 ms peak-to-peak check (RFC 5481 PDV).
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Any

from core.blocks import rtp_header
from core.blocks.jitter_calc import JitterCalc
from core.blocks.rate_meter import RateMeter
from core.blocks.seq_tracker import SeqTracker
from core.blocks.ts_analyzer import TsAnalyzer
from core.blocks.warmup_gate import GateState
from modules.iptv import record_keys as k
from modules.iptv.defaults import DEFAULTS
from modules.iptv.warmup import JOIN_WAIT_S, STABILIZE_MAX_S, START_MAX_S, make_gate, observe

NS_PER_S = 1_000_000_000
STOPPED_NS = 2 * NS_PER_S  # no packet for this long: the stream stopped


# Detail counters written only when not zero (02 7)
DETAIL_KEYS = (
    k.LOSS_EVENTS,
    k.LATE_DISCARDED,
    k.SYNC_LOSS,
    k.SYNC_BYTE_ERR,
    k.PAT_ERR,
    k.PMT_ERR,
    k.PID_ERR,
    k.TEI_ERR,
    k.CRC_ERR,
)


MP2T = 33  # RTP payload type of MPEG-2 TS (RFC 3551)
SKEW_MIN_S = 120  # seconds of data before the skew is estimated (05 2.2)
# beyond this it is not a clock: reported, not removed (500: the T460 ran 176-179 ppm with
# its time sync held, receiverPC 2026-10-23; a crystal stays well inside it)
SKEW_MAX_PPM = 500.0


PDV_WINDOW_S = 10  # seconds of delay variation in one window (dpw)


class _SkewFit:
    """Least-squares line through each second's minimum delay over the join segment
    (RFC 3393 5.2, RFC 5481 7). ``add`` returns the shift to remove from that second."""

    def __init__(self) -> None:
        self.n = 0
        self.t0: int | None = None
        self.st = self.sy = self.stt = self.sty = 0.0

    def add(self, elapsed_s: int, low_ms: float) -> float:
        if self.t0 is None:
            self.t0 = elapsed_s
        t = float(elapsed_s - self.t0)
        self.n += 1
        self.st += t
        self.sy += low_ms
        self.stt += t * t
        self.sty += t * low_ms
        slope = self.slope
        if slope is None or abs(slope * 1000) > SKEW_MAX_PPM:
            return 0.0
        return slope * t

    @property
    def slope(self) -> float | None:
        """ms per second (1 ms/s = 1,000 ppm), None before ``SKEW_MIN_S`` seconds."""
        if self.n < SKEW_MIN_S:
            return None
        den = self.n * self.stt - self.st * self.st
        return (self.n * self.sty - self.st * self.sy) / den if den else None

    @property
    def ppm(self) -> float | None:
        s = self.slope
        return s * 1000 if s is not None else None


@dataclass
class Startup:
    join_ns: int
    first_packet_ns: int | None = None
    ready_ns: int | None = None
    warmup_seconds: int = 0
    unmet: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        def ms(t: int | None) -> float | None:
            return None if t is None else round((t - self.join_ns) / 1e6, 1)

        return {
            "join_to_first_packet_ms": ms(self.first_packet_ns),
            "join_to_ready_ms": ms(self.ready_ns),
            "warmup_seconds": self.warmup_seconds,
            "unmet": self.unmet,
        }


class StreamAnalyzer:
    def __init__(
        self,
        ch: int,
        join_ns: int,
        playback: bool,
        warmup_max_s: float = START_MAX_S,
        reorder_window_ms: int = int(DEFAULTS["S-24"]),
        switch: bool = False,
    ) -> None:
        self.ch = ch
        self.switch = switch  # started by a channel change: never fails, gives up at 5 s
        self.join_failed = False  # no first packet within JOIN_WAIT_S of the join (NET-003)
        self.seq = SeqTracker(reorder_window_ms)
        self.jitter = JitterCalc()
        self.rate = RateMeter()
        self.ts = TsAnalyzer()
        self.gate = make_gate(warmup_max_s, playback)
        self.playback = playback
        self.startup = Startup(join_ns)
        self.not_rtp = 0
        self.other_pt = 0  # RTP with PT != 33 on our group/port (RFC 3551, 05 3.2)
        self.udp_ts = 0  # MPEG-TS straight on UDP (first byte 0x47, TS 102 034 7.1.3)
        self.skew_ppm: float | None = None  # sender vs receiver clock (05 2.2)
        self._fit = _SkewFit()
        self._pdv_win: collections.deque[tuple[int, float, float]] = collections.deque()
        self.lost_total = 0  # since measuring started (heartbeat, 02 5)
        self._elapsed = -1
        self._last: dict[str, int] = {}
        self._host_drops_base: int | None = None
        self._transit_ref: tuple[int, int, int] | None = None  # ssrc, arrival_ns, rtp_ts
        self._transit_ms = 0.0
        self._second_lo: float | None = None
        self._second_hi: float | None = None
        self._last_arrival_ns: int | None = None

    @property
    def measuring(self) -> bool:
        return self.gate.state is GateState.READY

    @property
    def failed(self) -> bool:
        return self.gate.state is GateState.FAILED

    def on_datagram(self, data: bytes, arrival_ns: int) -> None:
        self.rate.on_packet(len(data))
        h = rtp_header.parse(data)
        if h is None:
            self.not_rtp += 1
            if data[:1] == b"\x47":
                self.udp_ts += 1  # the sender sends TS without RTP (RCV-022)
            return
        if h.payload_type != MP2T:
            self.other_pt += 1  # someone else's flow: never this channel's packets
            return
        if self.startup.first_packet_ns is None:
            self.startup.first_packet_ns = arrival_ns
        self.seq.on_packet(h.seq, h.timestamp, h.ssrc, arrival_ns)
        self.jitter.on_packet(h.timestamp, h.ssrc, arrival_ns, discontinuity=h.marker)
        self._transit(h.ssrc, h.timestamp, arrival_ns, h.marker)
        last = self._last_arrival_ns
        if last is not None and arrival_ns - last > STOPPED_NS:
            self.ts.resync(arrival_ns)  # the stream was gone: that is not a P1 table error
        self.ts.feed(data[h.payload_offset : h.payload_end], arrival_ns)
        self._last_arrival_ns = arrival_ns

    def second(
        self,
        now_ns: int,
        host_drops_total: int | None = None,
        first_frame: bool | None = None,
        playback: dict[str, Any] | None = None,
        elapsed: int | None = None,
    ) -> dict[str, Any] | None:
        """Close one second. ``host_drops_total`` is the socket's own drop counter.
        ``elapsed`` puts the record on the session's clock (after a channel change the
        seconds of warm-up and of the switch are skipped, ``e`` goes on in real time)."""
        self.seq.advance(now_ns)
        # TR 101 290 section times by arrival (2026-10-16): a tick that ran late while
        # this PC wrote a 72 MB pcap found the last PAT "0.7 s ago" though every one
        # had come on time (max 84 ms apart in the pcap). A gap in the stream is found
        # by the next packet's arrival time; the tick's clock only for a stream that
        # has really stopped.
        last = self._last_arrival_ns
        if last is not None and now_ns - last <= STOPPED_NS:
            self.ts.check_timeouts(last)
        # nothing for STOPPED_NS: the stream is gone -- rx 0 says it; no P1 per second
        _, bps = self.rate.take_second()
        dlo, dhi = self._second_lo, self._second_hi
        self._second_lo = self._second_hi = None
        jitter_ms = round(self.jitter.take_period_max_ms(), 3)
        s, c = self.seq.stats, self.ts.counters
        now = {
            "rx": s.received,
            "lost": s.lost,
            "dup": s.duplicates,
            "reo": s.reordered_recovered,
            "cc": c.cc_err,
            "p1": c.p1_errors,
            k.LOSS_EVENTS: s.loss_events,
            k.LATE_DISCARDED: s.late_discarded,
            k.SYNC_LOSS: c.sync_loss,
            k.SYNC_BYTE_ERR: c.sync_byte_err,
            k.PAT_ERR: c.pat_err,
            k.PMT_ERR: c.pmt_err,
            k.PID_ERR: c.pid_err,
            k.TEI_ERR: c.tei_err,
            k.CRC_ERR: c.crc_err,
        }
        max_burst = self.seq.take_period_max_burst()
        delta = {key: now[key] - self._last.get(key, 0) for key in now}
        self._last = now
        if self.gate.state is GateState.WAITING:
            self.startup.warmup_seconds += 1
            t = (now_ns - self.startup.join_ns) / NS_PER_S
            seen = observe(
                packets=delta["rx"],
                lost=delta["lost"],
                jitter_max_ms=jitter_ms,
                pat_pmt=self.ts.pat_pmt_seen,
                first_frame=first_frame if self.playback else None,
            )
            state = self.gate.on_second(t, seen)
            if state is GateState.WAITING and self.switch:
                state = self._give_up_waiting(t, now_ns)
            if state is GateState.READY:
                self.startup.ready_ns = now_ns
                self._host_drops_base = host_drops_total
            elif state is GateState.FAILED:
                self.startup.unmet = self.gate.unmet()
            return None
        if self.gate.state is GateState.FAILED:
            return None
        self._elapsed = self._elapsed + 1 if elapsed is None else elapsed
        self.lost_total += delta["lost"]
        hd = None
        if host_drops_total is not None and self._host_drops_base is not None:
            hd = host_drops_total - self._host_drops_base
            self._host_drops_base = host_drops_total
        record: dict[str, Any] = {
            "type": "s",
            k.ELAPSED: self._elapsed,
            k.CHANNEL: self.ch,
            k.RECEIVED: delta["rx"],
            k.LOST: delta["lost"],
            k.DUPLICATES: delta["dup"],
            k.REORDERED: delta["reo"],
            k.JITTER_MS: jitter_ms,
            k.BITRATE_BPS: bps,
            k.CC_ERRORS: delta["cc"],
            k.P1_ERRORS: delta["p1"],
            k.PAT_SEEN: int(self.ts.pat_pmt_seen),
        }
        for key in DETAIL_KEYS:  # detail counters only when not zero (02 7)
            if delta[key]:
                record[key] = delta[key]
        if max_burst:
            record[k.MAX_BURST] = max_burst
        if hd is not None:
            record[k.HOST_DROPS] = hd
        if dlo is not None and dhi is not None:
            shift = self._fit.add(self._elapsed, dlo)
            self.skew_ppm = self._fit.ppm
            record[k.DELAY_LOW_MS] = round(dlo - shift, 3)
            record[k.DELAY_HIGH_MS] = round(dhi - shift, 3)
            # raw values: inside 10 s a clock skew adds <= 2 ms (200 ppm), while the fit's
            # shift steps when it starts (120 s) and moves as it refits (receiverPC review)
            record[k.DELAY_WINDOW_MS] = self._window_pdv(dlo, dhi)
            if self.skew_ppm is not None:
                record[k.SKEW_PPM] = round(self.skew_ppm, 1)
        if playback is not None:  # pb and p* keys from the player (02 7, iptv 3.1)
            record.update(playback)
        return record

    def _window_pdv(self, lo: float, hi: float) -> float:
        """Delay variation of the last PDV_WINDOW_S seconds (2026-10-22: the 5-minute
        max - min counted the PCs' clock slews as delay variation, yellow without a network
        cause; receiverPC matched three of five yellow cells to the T460's NTP polls)."""
        win = self._pdv_win
        win.append((self._elapsed, lo, hi))
        while win and win[0][0] <= self._elapsed - PDV_WINDOW_S:
            win.popleft()
        return round(max(x[2] for x in win) - min(x[1] for x in win), 3)

    def _give_up_waiting(self, t: float, now_ns: int) -> GateState:
        """Channel change: no first packet 5 s after the join, or 5 s of stabilizing
        after it, ends the wait (05 2.1-2 rule 4). What was not met stays in ``unmet``."""
        fp = self.startup.first_packet_ns
        if fp is None:
            if t < JOIN_WAIT_S:
                return GateState.WAITING
            self.join_failed = True
        elif (now_ns - fp) / NS_PER_S < STABILIZE_MAX_S:
            return GateState.WAITING
        self.startup.unmet = self.gate.unmet()
        self.gate.force_ready(t)
        return GateState.READY

    def _transit(self, ssrc: int, rtp_ts: int, arrival_ns: int, jump: bool = False) -> None:
        """Transit delay relative to the stream's first packet, in ms. Summing the
        per-packet differences keeps it right across timestamp wraparound. An announced
        timestamp jump (marker bit, RFC 2250 2.1) keeps the value: it is not a delay."""
        ref = self._transit_ref
        if ref is None or ref[0] != ssrc:
            self._transit_ms = 0.0
            self._fit = _SkewFit()  # a new source has its own clock
            self._pdv_win.clear()  # and its own delay baseline: no window across the jump
        elif jump:
            pass
        else:
            ts_step = (rtp_ts - ref[2]) % (1 << 32)
            if ts_step >= 1 << 31:
                ts_step -= 1 << 32
            self._transit_ms += (arrival_ns - ref[1]) / 1e6 - ts_step / 90.0
        self._transit_ref = (ssrc, arrival_ns, rtp_ts)
        v = self._transit_ms
        self._second_lo = v if self._second_lo is None else min(self._second_lo, v)
        self._second_hi = v if self._second_hi is None else max(self._second_hi, v)

    def finish(self, now_ns: int) -> dict[str, Any] | None:
        """End of measurement (stop order step 1): every open gap becomes loss and the
        last partial second is closed."""
        self.seq.close()
        return self.second(now_ns)
