"""IPTV self checks (``run_entry.self_checks()``, docs/12-architecture.md 2, 02 2-13 ``verify``).

Synthetic RTP/TS datagrams whose right answer is known go through the real stream
analyzer (docs/10 1.1 receiver rows, 05 2.1-2), and every row of the 5-minute color
table (05 5.3) through the real cell rules. The tables here are the one place for
these rows: the tests use them too.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from core.blocks.color_judge import Color
from core.self_check import Check, CheckFn, expect
from modules.iptv.cell_rules import CellInput, judge_cell
from modules.iptv.receiver.analyzer import StreamAnalyzer
from modules.iptv.rtp_synth import NS_PER_S, datagrams


def analyze(
    items: list[tuple[int, bytes, int]], seconds: int, drop: set[int] | None = None
) -> tuple[StreamAnalyzer, list[dict[str, Any]]]:
    """Feed the datagrams second by second as the receiver does; the records made."""
    a = StreamAnalyzer(ch=1, join_ns=0, playback=False)
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
        rec = a.second(end, first_frame=True)
        if rec:
            records.append(rec)
    last = a.finish(seconds * NS_PER_S + 1)
    if last:
        records.append(last)
    return a, records


def _total(records: list[dict[str, Any]], key: str) -> int:
    return sum(int(r[key]) for r in records)


def _stream_check(first_seq: int, pat_after_s: float, start_cc: int, drop: set[int],
                  lost: int, warmup_s: int) -> CheckFn:  # fmt: skip
    def check() -> str:
        items = datagrams(20, first_seq=first_seq, pat_after_s=pat_after_s, start_cc=start_cc)
        a, recs = analyze(items, 20, drop)
        if not drop:  # a lost datagram is also CC errors (P1), so only clean ones
            expect(_total(recs, "p1"), 0, "P1 errors")
        got = (_total(recs, "lost"), a.startup.warmup_seconds)
        return expect(got, (lost, warmup_s), "lost, warm-up s")

    return check


# (name, first seq, PAT after s, first CC, dropped datagrams, lost, warm-up s) — 10 1.1
STREAM_ROWS: list[tuple[str, int, float, int, set[int], int, int]] = [
    ("clean stream is ready after 3 s", 0, 0.0, 0, set(), 0, 3),
    ("mid-stream join: seq 50,000, CC 9, PAT after 2 s", 50_000, 2.0, 9, set(), 0, 3),
    ("first loss after the warm-up is counted", 0, 0.0, 0, {5000}, 1, 3),
    ("a loss inside the warm-up is not judged", 0, 0.0, 0, {100}, 0, 5),
]


def _ok(**changes: Any) -> CellInput:
    """A healthy UHD cell, with the given fields changed."""
    return replace(CellInput(has_data=True, received=990_000, bitrate_avg_bps=35e6), **changes)


# (description, cell input, expected color) — docs/05-standards.md 5.3
CELL_ROWS: list[tuple[str, CellInput, Color]] = [
    ("clean", _ok(), Color.GREEN),
    ("jitter warning", _ok(jitter_max_ms=61), Color.YELLOW),
    ("delay variation over DVB 40 ms", _ok(pdv_pp_ms=41.0), Color.YELLOW),
    ("delay variation at 40 ms", _ok(pdv_pp_ms=40.0), Color.GREEN),
    ("Wi-Fi delay variation 82 ms", _ok(pdv_pp_ms=82.0, wireless=True), Color.GREEN),
    ("Wi-Fi delay variation over 100 ms", _ok(pdv_pp_ms=101.0, wireless=True), Color.YELLOW),
    ("warning flag", _ok(warnings=("tablet lock released",)), Color.YELLOW),
    ("jitter exactly at limit", _ok(jitter_max_ms=150), Color.YELLOW),
    ("jitter over limit", _ok(jitter_max_ms=150.1), Color.RED),
    ("one lost packet", _ok(lost=1), Color.RED),
    ("host drops explain all loss", _ok(lost=10, host_drops=10), Color.PURPLE),
    ("host drops explain part", _ok(lost=10, host_drops=5), Color.RED),
    ("host drops without loss", _ok(host_drops=3), Color.YELLOW),
    ("loss while the tablet scanned (R-25)", _ok(lost=2, scan_lost=2), Color.PURPLE),
    ("scan explains part, socket the rest", _ok(lost=3, scan_lost=2, host_drops=1), Color.PURPLE),
    ("scan explains part only", _ok(lost=3, scan_lost=2), Color.RED),
    ("loss inside a sender window", _ok(lost=40, sender_s=3, sender_lost=40), Color.PURPLE),
    ("loss partly outside", _ok(lost=41, sender_s=3, sender_lost=40), Color.RED),
    ("wired link down", _ok(lost=40, link_s=5, link_lost=40), Color.GREY),
    ("sender slow only", _ok(slow_s=2), Color.YELLOW),
    ("playback issue, clean packets", _ok(playback_issue_with_clean_packets=True), Color.YELLOW),
    ("too few packets", CellInput(has_data=True, received=999, bitrate_avg_bps=35e6), Color.RED),
    ("too low bitrate", CellInput(has_data=True, received=5000, bitrate_avg_bps=99_999),
     Color.RED),
    ("P1 error without loss is a warning (GAP-132)", _ok(p1_errors=1), Color.YELLOW),
    ("P1 error with loss", _ok(p1_errors=3, lost=1), Color.RED),
    ("channel join failed", _ok(join_failures=1), Color.RED),
    ("channel join failed in a sender window", _ok(join_failures=1, sender_s=5), Color.PURPLE),
    ("channel join failed, link down", _ok(join_failures=1, link_s=5), Color.GREY),
    ("leave not processed", _ok(leave_not_processed=1), Color.YELLOW),
    ("leave not processed with loss", _ok(lost=1, leave_not_processed=1), Color.RED),
    ("no data", CellInput(has_data=False), Color.GREY),
]  # fmt: skip


def _cell_check(cell: CellInput, want: Color) -> CheckFn:
    return lambda: expect(judge_cell(cell).color.value, want.value, "color")


def checks() -> list[Check]:
    out: list[Check] = [
        ("accuracy", f"IPTV analyzer: {name}", _stream_check(seq, pat, cc, drop, lost, warm))
        for name, seq, pat, cc, drop, lost, warm in STREAM_ROWS
    ]
    out += [("judgement", f"5-minute color: {name}", _cell_check(cell, want))
            for name, cell, want in CELL_ROWS]  # fmt: skip
    return out
