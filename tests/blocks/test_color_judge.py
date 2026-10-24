"""Table tests for 5-minute colors and the final verdict (docs/10-verification.md 1.2)."""

from __future__ import annotations

import pytest

from core.blocks.color_judge import Color, Verdict, final_verdict
from core.self_checks import VERDICT_ROWS
from modules.iptv.cell_rules import CellInput, judge_cell
from modules.iptv.self_checks import CELL_ROWS
from modules.iptv.self_checks import _ok as ok

CELLS = CELL_ROWS  # the one table (docs/05 5.3), also run by zeroloss verify


@pytest.mark.parametrize(("name", "cell", "expected"), CELLS, ids=[c[0] for c in CELLS])
def test_cell_colors(name: str, cell: CellInput, expected: Color) -> None:
    assert judge_cell(cell).color == expected, name


def test_network_loss_is_split_from_receiver_loss() -> None:
    r = judge_cell(ok(lost=10, host_drops=5))
    assert (r.network_loss, r.receiver_loss) == (5, 5)


VERDICTS = VERDICT_ROWS  # docs/05 5.4, also run by zeroloss verify


@pytest.mark.parametrize(("colors", "complete", "verdict", "warn"), VERDICTS)
def test_final_verdict(colors: list[Color], complete: bool, verdict: Verdict, warn: bool) -> None:
    r = final_verdict(colors, complete)
    assert (r.verdict, r.with_warnings) == (verdict, warn)
    if verdict is not Verdict.PASS:
        assert r.reasons


def test_p1_without_loss_passes_with_a_reason() -> None:
    """2026-10-09 user: pass is jitter and loss only (GAP-132)."""
    r = judge_cell(ok(p1_errors=7))
    assert r.color == Color.YELLOW
    assert r.reasons == ("TR 101 290 P1 errors 7 without packet loss",)
