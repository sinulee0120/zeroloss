"""1 s -> 5 min summaries (docs/02-data.md 6)."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.blocks.summarizer import Agg, Summarizer
from modules.iptv.summary_rules import make_summarizer


def sec(e: int, ch: int = 1, **v: Any) -> dict[str, Any]:
    return {"type": "s", "e": e, "ch": ch, "rx": 3300, "lost": 0, "jit": 2.0, "br": 35e6, **v}


def run(s: Summarizer, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in records:
        out += s.add(r)
    return out + s.flush()


def test_full_cells_and_partial_tail() -> None:
    cells = run(make_summarizer(), [sec(e) for e in range(650)])
    assert [(c["index"], c["duration_s"], c["partial"]) for c in cells] == [
        (0, 300, False),
        (1, 300, False),
        (2, 50, True),
    ]
    assert cells[0]["values"]["received"] == 300 * 3300
    assert cells[0]["start_elapsed_s"] == 0 and cells[1]["start_elapsed_s"] == 300


def test_values_sum_max_min_avg() -> None:
    recs = [sec(0, jit=1.0, lost=2, br=30e6), sec(1, jit=5.0, br=40e6), sec(2, jit=3.0)]
    v = run(make_summarizer(), recs)[0]["values"]
    assert v["lost"] == 2
    assert v["jitter_max_ms"] == 5.0 and v["jitter_avg_ms"] == pytest.approx(3.0)
    assert v["bitrate_min_bps"] == 30e6 and v["bitrate_max_bps"] == 40e6


def test_missing_field_is_none_not_zero() -> None:
    v = run(make_summarizer(), [sec(0)])[0]["values"]
    assert v["delay_low_ms"] is None  # no transit reading in that second: unknown, not 0


def test_shape_follows_the_data_document() -> None:
    """02 6: detail counters absent from every second count as 0; phase-later groups null."""
    from modules.iptv.summary_rules import shape

    piece = run(make_summarizer(), [sec(0, lost=3), sec(1), {**sec(2), "lev": 1, "mb": 3}])[0]
    s = shape(piece, rcvbuf_bytes=212992, kernel_timestamps=True)
    assert s["loss"]["lost"] == 3 and s["loss"]["expected"] == s["loss"]["received"] + 3
    assert s["loss"]["loss_events"] == 1 and s["loss"]["max_burst"] == 3
    assert s["loss"]["late_discarded"] == 0 and s["ts"]["pat_err"] == 0
    assert s["receiver"]["rcvbuf_bytes"] == 212992
    assert s["measure_method"]["timestamps"] == "kernel"
    assert s["wifi"] is None and s["playback"] is None and s["mdi"]["df_max_ms"] is None


def test_channel_change_splits_the_cell() -> None:
    recs = [sec(e, ch=1) for e in range(100)] + [sec(e, ch=2) for e in range(100, 300)]
    cells = run(make_summarizer(), recs)
    assert [(c["index"], c["ch"], c["join_segment"], c["duration_s"]) for c in cells] == [
        (0, 1, 0, 100),
        (0, 2, 1, 200),
    ]
    assert all(c["partial"] for c in cells)


def test_gap_makes_cell_partial() -> None:
    cells = run(make_summarizer(), [sec(e) for e in range(300) if e != 42])
    assert cells[0]["duration_s"] == 299 and cells[0]["partial"]


def test_rejects_repeated_or_backward_seconds() -> None:
    s = make_summarizer()
    s.add(sec(5))
    with pytest.raises(ValueError):
        s.add(sec(5))
    with pytest.raises(ValueError):
        s.add(sec(4))


def test_flush_twice_is_empty() -> None:
    s = make_summarizer()
    s.add(sec(0))
    assert len(s.flush()) == 1
    assert s.flush() == []


@settings(max_examples=150, deadline=None)
@given(
    steps=st.lists(
        st.tuples(st.integers(1, 120), st.integers(1, 3), st.integers(0, 5)),
        min_size=1,
        max_size=60,
    ),
    cell_s=st.sampled_from([10, 60, 300]),
)
def test_random_stream_every_second_in_exactly_one_piece(
    steps: list[tuple[int, int, int]], cell_s: int
) -> None:
    """Invariants: totals are kept, pieces never cross a cell or a channel, order kept."""
    s = Summarizer({"lost": ("lost", Agg.SUM), "n": ("one", Agg.SUM)}, cell_s, "ch")
    recs, e = [], 0
    for gap, ch, lost in steps:
        e += gap
        recs.append({"e": e, "ch": ch, "lost": lost, "one": 1})
    cells = run(s, recs)
    assert sum(c["values"]["lost"] for c in cells) == sum(r["lost"] for r in recs)
    assert sum(c["duration_s"] for c in cells) == len(recs)
    assert all(c["values"]["n"] == c["duration_s"] <= cell_s for c in cells)
    starts = [c["start_elapsed_s"] for c in cells]
    assert starts == sorted(starts)
    pos = 0
    for c in cells:
        piece = recs[pos : pos + c["duration_s"]]
        pos += c["duration_s"]
        assert {r["e"] // cell_s for r in piece} == {c["index"]}
        assert {r["ch"] for r in piece} == {c["ch"]}
    segments = [c["join_segment"] for c in cells]
    assert segments == sorted(segments)
    changes = sum(1 for a, b in pairwise(recs) if a["ch"] != b["ch"])
    assert segments[-1] == changes
