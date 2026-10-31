"""Design values: the one place for colors, fonts and spacing (docs/11-ui.md 1.7, 1.11).

Controller Desktop, Agent Desktop and the HTML report use these values. The app forces
this palette whatever the OS theme is (dark-mode trap, 1.11). Every text/background
pair used is listed in TEXT_PAIRS and checked for contrast by a test.
"""

from __future__ import annotations

from dataclasses import dataclass

GRID = 8  # every margin and gap is a multiple of this
FONT_PT = {"body": 10, "title": 12, "key": 18}  # key = Jitter / Loss values
MIN_FONT_PT = 9
BUTTON_MIN_HEIGHT = 32
BUTTON_PAD = 16

COLORS = {
    "bg": "#f5f5f5",  # neutral light grey background (ISA-101)
    "panel": "#ffffff",
    "text": "#1b1b1b",
    "text_muted": "#555555",
    "text_disabled": "#8a8a8a",
    "border": "#c8c8c8",
    "primary": "#1f5fbf",
    "on_primary": "#ffffff",
    # cell colors, report (clear) and live screen (muted normal, decided 2026-09-24).
    # 2026-10-24 user ("보라색도 빨간색처럼 보여 … 색맹도 고려하고"): red is the router
    # only; the sender's or receiver's side ("purple" by name) is blue and OK is bluish
    # green, so no pair meets on the red-green axis; shapes differ too. Chosen by a search:
    # every pair at least dE 20 apart in normal, protan, deutan and tritan vision
    # (Machado 2009 simulation; the old set had pairs at 13) and white text at 3:1 or more
    "green": "#0f7a4a",
    "green_live": "#cfe9db",
    "yellow": "#9e7a00",
    "red": "#a41919",
    "purple": "#0b57d0",
    "grey": "#4f5b62",
    "on_cell": "#ffffff",
    "on_green_live": "#0b4d2c",
}

# yellow is a note that does not change the verdict: no exclamation mark, which reads as
# a failure next to PASS (11-ui 1.7, GAP-106); every shape differs (black-and-white)
ICONS = {"green": "✔", "yellow": "△", "red": "✖", "purple": "◆", "grey": "○"}

# (text color, background color, minimum ratio): every pair the screens draw.
TEXT_PAIRS: list[tuple[str, str, float]] = [
    ("text", "bg", 4.5),
    ("text", "panel", 4.5),
    ("text_muted", "panel", 4.5),
    ("text_disabled", "panel", 3.0),
    ("on_primary", "primary", 4.5),
    ("on_cell", "green", 4.5),
    ("on_cell", "yellow", 3.0),  # icons only on cells (large glyph, 1.11)
    ("on_cell", "red", 4.5),
    ("on_cell", "purple", 4.5),
    ("on_cell", "grey", 4.5),
    ("on_green_live", "green_live", 4.5),
]


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(fg: str, bg: str) -> float:
    """WCAG 2.x contrast ratio."""
    a, b = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


@dataclass(frozen=True)
class Cell:
    color: str
    icon: str
    fg: str


def live_cell(color: str) -> Cell:
    """Live screen: normal cells are muted so problems stand out (ISA-101)."""
    if color == "green":
        return Cell(COLORS["green_live"], ICONS["green"], COLORS["on_green_live"])
    return Cell(COLORS[color], ICONS[color], COLORS["on_cell"])
