"""The one palette and style of every ZeroLoss window (docs/11-ui.md 1.11, 20 GAP-65).

Forced whatever the OS theme is (dark-mode trap). Values from ``core.ui_schema.design``.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from core.ui_schema.design import BUTTON_MIN_HEIGHT, BUTTON_PAD, COLORS, FONT_PT, GRID


def apply_palette(app: QApplication) -> None:
    """Our own light palette whatever the OS theme is (1.11 dark-mode trap)."""
    app.setStyle("Fusion")
    pal = QPalette()
    c = {k: QColor(v) for k, v in COLORS.items()}
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        pal.setColor(group, QPalette.ColorRole.Window, c["bg"])
        pal.setColor(group, QPalette.ColorRole.Base, c["panel"])
        pal.setColor(group, QPalette.ColorRole.AlternateBase, c["bg"])
        pal.setColor(group, QPalette.ColorRole.WindowText, c["text"])
        pal.setColor(group, QPalette.ColorRole.Text, c["text"])
        pal.setColor(group, QPalette.ColorRole.ButtonText, c["text"])
        pal.setColor(group, QPalette.ColorRole.Button, c["panel"])
        pal.setColor(group, QPalette.ColorRole.ToolTipBase, c["panel"])
        pal.setColor(group, QPalette.ColorRole.ToolTipText, c["text"])
        pal.setColor(group, QPalette.ColorRole.Highlight, c["primary"])
        pal.setColor(group, QPalette.ColorRole.HighlightedText, c["on_primary"])
        pal.setColor(group, QPalette.ColorRole.PlaceholderText, c["text_muted"])
    d = QPalette.ColorGroup.Disabled
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        pal.setColor(d, role, c["text_disabled"])
    pal.setColor(d, QPalette.ColorRole.Window, c["bg"])
    pal.setColor(d, QPalette.ColorRole.Base, c["panel"])
    pal.setColor(d, QPalette.ColorRole.Button, c["panel"])
    app.setPalette(pal)
    font = app.font()
    # 2026-11-27 user ("글자 안보임"): Korean movie names came out as loose jamo where the
    # UI font has no Hangul; the Korean UI fonts follow it, whichever the OS has
    font.setFamilies([font.family(), "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans CJK KR",
                      "NanumGothic", "Segoe UI Symbol", "Segoe UI Emoji", "DejaVu Sans",
                      "Noto Sans Symbols2"])  # fmt: skip  # ✔ △ ✖ ◆ ○ ⓘ too
    font.setPointSize(FONT_PT["body"])
    app.setFont(font)
    k = COLORS
    # 2026-11-27 user ("상용버전처럼", "촌스러워"): product-dashboard style -- hairline
    # cards with rounded corners, quiet section titles, fields and buttons alike
    app.setStyleSheet(
        f"""
        QPushButton {{ min-height: {BUTTON_MIN_HEIGHT}px; padding: 0 {BUTTON_PAD}px;
            border: 1px solid {k["border_strong"]}; border-radius: 6px;
            background: {k["panel"]}; }}
        QPushButton:hover {{ background: {k["hover"]}; }}
        QPushButton#primary {{ background: {k["primary"]}; color: {k["on_primary"]};
            border: none; font-size: {FONT_PT["title"]}pt; font-weight: 600; }}
        QPushButton#primary:hover {{ background: #1a52a6; }}
        QPushButton#primary[waiting="true"] {{ background: {k["border"]};
            color: {k["text"]}; }}
        QPushButton:disabled {{ color: {k["text_disabled"]}; border-color: {k["border"]}; }}
        QLineEdit, QSpinBox {{ min-height: {BUTTON_MIN_HEIGHT - 4}px;
            border: 1px solid {k["border_strong"]}; border-radius: 6px;
            padding: 0 {GRID}px; background: {k["panel"]}; }}
        QLineEdit:disabled {{ background: {k["bg"]};
            color: {k["text_disabled"]}; }}
        QPushButton#nav {{ border: none; background: transparent; color: {k["text_muted"]};
            padding: 0 {GRID + 4}px; }}
        QPushButton#nav:hover {{ background: {k["border"]}; color: {k["text"]}; }}
        QPushButton#danger {{ color: {k["red"]}; border-color: #e8b4b4; }}
        QPushButton#danger:hover {{ background: #fbecec; }}
        QWidget#tile {{ background: {k["panel"]}; border: 1px solid {k["border"]};
            border-radius: 8px; }}
        QLabel#muted {{ color: {k["text_muted"]}; }}
        QProgressBar {{ border: none; background: {k["border"]}; border-radius: 1px; }}
        QProgressBar::chunk {{ background: {k["primary"]}; border-radius: 1px; }}
        QGroupBox {{ font-size: {FONT_PT["body"]}pt; font-weight: 600; color: {k["text_muted"]};
            background: {k["panel"]}; border: 1px solid {k["border"]}; border-radius: 8px;
            margin-top: {3 * GRID}px; padding: {GRID}px; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: {GRID // 2}px;
            padding: 0 {GRID // 2}px; }}
        QToolTip {{ font-size: {FONT_PT["body"]}pt; background: {k["text"]};
            color: {k["panel"]}; border: none; padding: {GRID // 2}px; }}
        QScrollBar:vertical {{ width: {GRID + 2}px; background: transparent; }}
        QScrollBar::handle:vertical {{ background: {k["border_strong"]}; border-radius: 4px;
            min-height: {3 * GRID}px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        """
    )
