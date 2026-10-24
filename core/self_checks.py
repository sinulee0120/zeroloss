"""Core's own self checks (docs/10-verification.md 1.1 and 1.2, 02 2-13 ``verify``).

Packets whose right answer is known go through the real measuring blocks; the final
verdict table (05 5.4) goes through the real verdict frame. The tables here are the
one place for these rows: the tests use them too.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from core.blocks.color_judge import Color, Verdict, final_verdict
from core.blocks.jitter_calc import JitterCalc
from core.blocks.packet_synth import NS_PER_S, Pkt, delay, drop, stream
from core.blocks.seq_tracker import SeqStats, SeqTracker
from core.blocks.ts_analyzer import TsAnalyzer
from core.blocks.ts_synth import MS, Stream
from core.self_check import Check, CheckFn, expect, near


def _seq(pkts: list[Pkt], window_ms: int = 1000) -> SeqStats:
    t = SeqTracker(reorder_window_ms=window_ms)
    for p in pkts:
        t.on_packet(p.seq, p.ts, p.ssrc, p.arrival_ns)
    t.advance(pkts[-1].arrival_ns + 10 * NS_PER_S)
    t.close()
    return t.stats


def _reordered() -> list[Pkt]:
    pkts = stream(5000)
    for i in (1000, 2000, 3000):
        pkts = delay(pkts, i, 200_000_000)  # 200 ms late, the window is 1000 ms
    return pkts


def _duplicated() -> list[Pkt]:
    pkts = stream(3000)
    return sorted([*pkts, pkts[500], pkts[501], pkts[2999]], key=lambda p: p.arrival_ns)


def _new_ssrc() -> list[Pkt]:
    a = stream(3000, first_seq=100)
    return a + stream(3000, first_seq=40_000, ssrc=0x01000002, start_ns=a[-1].arrival_ns + 1000)


# (name, packets, fields of SeqStats, expected values) — docs/10 1.1 rows
SEQ_ROWS: list[tuple[str, Callable[[], SeqStats], tuple[str, ...], tuple[int, ...]]] = [
    ("clean stream", lambda: _seq(stream(10_000)), ("lost", "loss_events"), (0, 0)),
    ("7 scattered + burst of 50 lost",
     lambda: _seq(drop(stream(10_000), {100, 900, 1700, 2500, 3300, 4100, 4900}
                       | set(range(6000, 6050)))),
     ("lost", "loss_events", "max_burst"), (57, 8, 50)),
    ("reordered inside the 1,000 ms window", lambda: _seq(_reordered()),
     ("reordered_recovered", "lost", "late_discarded"), (3, 0, 0)),
    ("arrived after the window", lambda: _seq(delay(stream(10_000), 1000, 1_500_000_000)),
     ("late_discarded", "lost"), (1, 1)),
    ("duplicates", lambda: _seq(_duplicated()), ("duplicates", "lost"), (3, 0)),
    ("SSRC change and sequence jump", lambda: _seq(_new_ssrc()),
     ("lost", "segment_restarts"), (0, 1)),
    ("sequence and timestamp wrap around",
     lambda: _seq(stream(20_000, first_seq=65_000, first_ts=(1 << 32) - 50_000)),
     ("lost", "duplicates"), (0, 0)),
    ("mid-stream join at sequence 50,000", lambda: _seq(stream(5000, first_seq=50_000)),
     ("lost", "received"), (0, 5000)),
    ("first loss after a clean period", lambda: _seq(drop(stream(10_000), {7000})),
     ("lost", "loss_events"), (1, 1)),
]  # fmt: skip


def _seq_check(
    run: Callable[[], SeqStats], fields: tuple[str, ...], want: tuple[int, ...]
) -> CheckFn:
    def check() -> str:
        stats = run()
        got = tuple(getattr(stats, f) for f in fields)
        return expect(got, want, ", ".join(fields))

    return check


def _reference_jitter_ms(ts: list[int], arrival_ns: list[int]) -> float:
    """RFC 3550 A.8 written out in floating point, apart from the real block."""
    j = 0.0
    for i in range(1, len(ts)):
        r = (arrival_ns[i] - arrival_ns[i - 1]) * 90_000 / NS_PER_S
        j += (abs(r - (ts[i] - ts[i - 1])) - j) / 16
    return j / 90


def _jitter_rfc() -> str:
    worst = 0.0
    for seed, spread_ms in ((1, 5.0), (2, 40.0), (3, 180.0)):
        rng = random.Random(seed)
        pkts = stream(3000)
        arrivals = [p.arrival_ns + int(rng.uniform(0, spread_ms) * 1_000_000) for p in pkts]
        calc = JitterCalc()
        for p, a in zip(pkts, arrivals, strict=True):
            calc.on_packet(p.ts, p.ssrc, a)
        want = _reference_jitter_ms([p.ts for p in pkts], arrivals)
        near(calc.jitter_ms, want, 0.1, f"jitter with {spread_ms} ms spread")
        worst = max(worst, abs(calc.jitter_ms - want))
    return f"3 streams within {worst:.4f} ms of RFC 3550 (limit 0.1 ms)"


def _jitter_known() -> str:
    calc = JitterCalc()
    for i, p in enumerate(stream(20_000)):
        calc.on_packet(p.ts, p.ssrc, p.arrival_ns + (4_000_000 if i % 2 else 0))
    return near(calc.jitter_ms, 4.0, 0.1, "every other packet 4 ms late: jitter ms")


def _jitter_wrap() -> str:
    calc = JitterCalc()
    for p in stream(10_000, first_ts=(1 << 32) - 30_000):
        calc.on_packet(p.ts, p.ssrc, p.arrival_ns)
    return near(calc.jitter_ms, 0.0, 0.01, "timestamp wrap around: jitter ms")


def _ts(ticks: list[list[bytes]]) -> TsAnalyzer:
    a = TsAnalyzer(5.0)
    for i, pkts in enumerate(ticks):
        data, now = b"".join(pkts), (i + 1) * 100 * MS
        for off in range(0, len(data), 7 * 188):
            a.feed(data[off : off + 7 * 188], now)
        a.check_timeouts(now)
    return a


def _ts_late_pat() -> str:
    s = Stream(start_cc=9)
    ticks = [s.tick(with_pat=False, with_pmt=False) for _ in range(20)]
    ticks += [s.tick() for _ in range(30)]
    return expect(_ts(ticks).counters.p1_errors, 0, "mid-stream join, PAT after 2 s: P1 errors")


def _ts_pat_missing() -> str:
    s = Stream()
    ticks = [s.tick() for _ in range(10)] + [s.tick(with_pat=False) for _ in range(20)]
    ticks += [s.tick() for _ in range(10)]
    return expect(_ts(ticks).counters.pat_err, 4, "PAT missing 2.1 s: PAT errors")


def _ts_cc() -> str:
    s = Stream()
    ticks = [s.tick() for _ in range(30)]
    ticks[10] = [p for i, p in enumerate(ticks[10]) if i not in (5, 20)]  # 2 video packets
    return expect(_ts(ticks).counters.cc_err, 2, "2 video packets lost: CC errors")


G, Y, R, P, N = Color.GREEN, Color.YELLOW, Color.RED, Color.PURPLE, Color.GREY
# (cell colors, planned time reached, verdict, with warnings) — docs/05 5.4
VERDICT_ROWS: list[tuple[list[Color], bool, Verdict, bool]] = [
    ([G, G, G], True, Verdict.PASS, False),
    ([G, Y, G], True, Verdict.PASS, True),
    ([G, R, G], True, Verdict.FAIL, False),
    ([G, R], False, Verdict.FAIL, False),
    ([G, G], False, Verdict.INCOMPLETE, False),
    ([G, N, G], True, Verdict.INCOMPLETE, False),
    ([G, P, G], True, Verdict.INCOMPLETE, False),
    ([P, R], True, Verdict.FAIL, False),
    ([], True, Verdict.INCOMPLETE, False),
]


def _verdict_check(colors: list[Color], done: bool, verdict: Verdict, warn: bool) -> CheckFn:
    def check() -> str:
        r = final_verdict(colors, done)
        if r.verdict is not Verdict.PASS and not r.reasons:
            return expect("no reason", "a reason", "verdict other than PASS")
        return expect((r.verdict.value, r.with_warnings), (verdict.value, warn),
                      "verdict, with warnings")  # fmt: skip

    return check


def checks() -> list[Check]:
    out: list[Check] = [
        ("accuracy", f"loss count: {name}", _seq_check(run, fields, want))
        for name, run, fields, want in SEQ_ROWS
    ]
    out += [
        ("accuracy", "jitter matches RFC 3550 (+-0.1 ms)", _jitter_rfc),
        ("accuracy", "jitter of a known pattern", _jitter_known),
        ("accuracy", "jitter across timestamp wrap around", _jitter_wrap),
        ("accuracy", "TS: mid-stream join and late PAT are no error", _ts_late_pat),
        ("accuracy", "TS: PAT missing is counted", _ts_pat_missing),
        ("accuracy", "TS: lost packets are CC errors", _ts_cc),
    ]
    out += [
        ("judgement", f"verdict: {'/'.join(c.value for c in cs) or 'no cells'}"
         f"{'' if done else ', stopped early'}", _verdict_check(cs, done, v, w))
        for cs, done, v, w in VERDICT_ROWS
    ]  # fmt: skip
    return out
