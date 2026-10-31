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


def test_cell_colors_stay_apart_for_colour_blind_eyes() -> None:
    """2026-10-24 user ("보라색도 빨간색처럼 보여 … 색맹도 고려하고"): every pair of cell
    colors at least dE 20 apart in normal, protan, deutan and tritan vision (Machado 2009,
    severity 1); shapes differ as well (ICONS)."""
    import itertools
    import math

    from core.ui_schema.design import COLORS, ICONS

    sims: list[list[list[float]]] = [
        [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216],
         [-0.003882, -0.048116, 1.051998]],
        [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413],
         [-0.011820, 0.042940, 0.968881]],
        [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602],
         [0.004733, 0.691367, 0.303900]],
    ]  # fmt: skip

    def lin(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def lab(h: str, m: list[list[float]]) -> tuple[float, float, float]:
        v = [lin(int(h[i : i + 2], 16)) for i in (1, 3, 5)]
        r, g, b = (max(0.0, min(1.0, sum(m[i][k] * v[k] for k in range(3)))) for i in range(3))
        x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
        y = 0.2126 * r + 0.7152 * g + 0.0722 * b
        z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

        def f(t: float) -> float:
            return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

        return (116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z)))

    cells = {k: COLORS[k] for k in ("green", "yellow", "red", "purple", "grey")}
    for m in sims:
        for a, b in itertools.combinations(cells, 2):
            assert math.dist(lab(cells[a], m), lab(cells[b], m)) >= 20, (a, b)
    assert len(set(ICONS.values())) == len(ICONS)  # every shape differs
