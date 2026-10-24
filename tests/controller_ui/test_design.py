"""Design values: contrast of every drawn text pair (11-ui 1.11), grid, font floor."""

import pytest

from core.ui_schema import design


@pytest.mark.parametrize(("fg", "bg", "need"), design.TEXT_PAIRS)
def test_contrast(fg: str, bg: str, need: float) -> None:
    ratio = design.contrast(design.COLORS[fg], design.COLORS[bg])
    assert ratio >= need, f"{fg} on {bg}: {ratio:.2f} < {need}"


def test_contrast_reference_values() -> None:
    assert design.contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert design.contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)


def test_every_cell_color_has_icon_and_pair() -> None:
    drawn_bgs = {bg for _, bg, _ in design.TEXT_PAIRS}
    for color in design.ICONS:
        cell = design.live_cell(color)
        assert cell.icon
        assert cell.color in {design.COLORS[b] for b in drawn_bgs}


def test_sizes() -> None:
    assert min(design.FONT_PT.values()) >= design.MIN_FONT_PT >= 9
    assert design.BUTTON_MIN_HEIGHT % design.GRID == 0
    assert design.BUTTON_PAD % design.GRID == 0
