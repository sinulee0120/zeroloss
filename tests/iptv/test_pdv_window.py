"""Delay variation over 10-second windows (2026-10-22, R20261022-235424).

Eight yellow cells of a 100 h run said "delay variation 50-125 ms" with no loss and normal
jitter: the PCs' clocks were being slewed (NTP), which the cell's max - min counted. A
10-second window leaves a slew of ≤ 0.5 ms/s out and keeps a real delay burst.
"""

from __future__ import annotations

from typing import Any

from core.blocks.color_judge import Color
from modules.iptv.receiver.analyzer import PDV_WINDOW_S, StreamAnalyzer
from modules.iptv.run import cell_color


def analyzer() -> Any:
    a = object.__new__(StreamAnalyzer)
    a._pdv_win = __import__("collections").deque()
    return a


def run(a: Any, delays: list[float]) -> list[float]:
    out = []
    for e, d in enumerate(delays):
        a._elapsed = e
        out.append(a._window_pdv(d - 0.5, d + 0.5))
    return out


def test_a_slow_clock_slew_is_not_delay_variation() -> None:
    slew = [-0.46 * e for e in range(300)]  # 02:34 on the T460: 11 -> -127 ms in 5 min
    assert max(run(analyzer(), slew)) < 6  # whole cell: 138 ms; any 10 s: about 5 ms
    assert PDV_WINDOW_S == 10


def test_a_delay_burst_still_counts() -> None:
    burst = [0.0] * 100 + [60.0, 120.0, 60.0] + [0.0] * 100  # a 120 ms spike
    assert max(run(analyzer(), burst)) >= 120


def test_a_new_stream_starts_a_new_window() -> None:
    a = analyzer()
    run(a, [500.0] * 5)
    a._pdv_win.clear()  # what _transit does for a new SSRC
    assert run(a, [0.0] * 3)[-1] == 1.0  # no jump across the old and the new baseline


def summary(window: float | None, lo: float, hi: float) -> dict[str, Any]:
    jit = {"avg_ms": 0.7, "max_ms": 1.2, "delay_low_ms": lo, "delay_high_ms": hi}
    if window is not None:
        jit["pdv_window_ms"] = window
    return {"duration_s": 300, "partial": False, "jitter": jit,
            "loss": {"received": 997000, "lost": 0},
            "bitrate_bps": {"avg": 35_000_000}}  # fmt: skip


def test_the_cell_is_judged_on_the_window() -> None:
    assert cell_color(summary(5.0, -127.0, 11.0), 30.0)[0] is Color.GREEN  # the slew cell
    assert cell_color(summary(None, -127.0, 11.0), 30.0)[0] is Color.YELLOW  # old summary


def test_a_skewed_clock_across_the_fit_start_stays_small() -> None:
    """receiverPC review: the window takes raw delays; the skew fit starts at 120 s and
    would put a step into a window. 100 ppm over 300 s: any 10 s window <= 2 ms."""
    skewed = [0.1 * e for e in range(300)]  # 100 ppm = 0.1 ms/s
    assert max(run(analyzer(), skewed)) <= 2.0


def test_delay_variation_while_the_sender_sent_late_is_the_senders() -> None:
    """2026-10-23 R20261023-111542 11:40:23: the pacer stalled 171 ms (SND-025); the cell
    said "delay variation 174.5 ms" yellow against the router."""
    s = summary(174.5, -10.0, 164.5)
    s["trouble"] = {"sender_s": 1}
    color, why = cell_color(s, 30.0)
    assert color is Color.PURPLE and "during sender trouble" in why[0]
