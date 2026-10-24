"""RFC 3550 6.4.1 interarrival jitter (docs/05-standards.md 2.2).

    D(i,j) = (Rj - Ri) - (Sj - Si)      R = arrival, S = RTP timestamp, both in RTP ticks
    J      = J + (|D| - J) / 16

Arrival times come from the caller (kernel timestamps in production), so tests are exact.
A new SSRC restarts the estimator. Values are reported in milliseconds.
"""

from __future__ import annotations

TS_MOD = 1 << 32
NS_PER_S = 1_000_000_000


class JitterCalc:
    def __init__(self, clock_hz: int = 90_000) -> None:
        self._clock_hz = clock_hz
        self._ssrc: int | None = None
        self._last_arrival_ns: int | None = None
        self._last_ts = 0
        self._jitter_ticks = 0.0
        self._period_max_ticks = 0.0

    def on_packet(
        self, rtp_ts: int, ssrc: int, arrival_ns: int, discontinuity: bool = False
    ) -> None:
        """``discontinuity``: the RTP marker bit of MPEG-TS (RFC 2250 2.1) says the
        timestamp jumped on purpose; that pair is skipped, J is kept (same source)."""
        if ssrc != self._ssrc:
            self._ssrc = ssrc
            self._last_arrival_ns = None
            self._jitter_ticks = 0.0
        if self._last_arrival_ns is not None and not discontinuity:
            arrival_ticks = (arrival_ns - self._last_arrival_ns) * self._clock_hz / NS_PER_S
            ts_ticks = _signed_ts_diff(rtp_ts, self._last_ts)
            d = arrival_ticks - ts_ticks
            self._jitter_ticks += (abs(d) - self._jitter_ticks) / 16
            self._period_max_ticks = max(self._period_max_ticks, self._jitter_ticks)
        self._last_arrival_ns = arrival_ns
        self._last_ts = rtp_ts

    @property
    def jitter_ms(self) -> float:
        return self._to_ms(self._jitter_ticks)

    def take_period_max_ms(self) -> float:
        """Maximum jitter since the last call (the 1-second record), then reset."""
        value = self._to_ms(max(self._period_max_ticks, self._jitter_ticks))
        self._period_max_ticks = self._jitter_ticks
        return value

    def _to_ms(self, ticks: float) -> float:
        return ticks * 1000 / self._clock_hz


def _signed_ts_diff(a: int, b: int) -> int:
    """a - b for 32-bit RTP timestamps, correct across wraparound."""
    diff = (a - b) % TS_MOD
    return diff - TS_MOD if diff >= TS_MOD // 2 else diff
