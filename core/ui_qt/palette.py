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
    font.setPointSize(FONT_PT["body"])
    app.setFont(font)
    app.setStyleSheet(
        f"""
        QPushButton {{ min-height: {BUTTON_MIN_HEIGHT}px; padding: 0 {BUTTON_PAD}px;
            border: 1px solid {COLORS["border"]}; border-radius: 4px;
            background: {COLORS["panel"]}; }}
        QPushButton#primary {{ background: {COLORS["primary"]}; color: {COLORS["on_primary"]};
            border: none; font-size: {FONT_PT["title"]}pt; }}
        QPushButton#primary[waiting="true"] {{ background: {COLORS["border"]};
            color: {COLORS["text"]}; }}
        QPushButton:disabled {{ color: {COLORS["text_disabled"]}; }}
        QGroupBox {{ font-size: {FONT_PT["title"]}pt; background: {COLORS["panel"]};
            border: 1px solid {COLORS["border"]}; border-radius: 4px;
            margin-top: {3 * GRID}px; padding: {GRID}px; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: {GRID}px; padding: 0 {GRID // 2}px; }}
        QToolTip {{ font-size: {FONT_PT["body"]}pt; }}
        """
    )
