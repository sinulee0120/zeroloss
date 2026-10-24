"""Screen size rules shared by the screen apps (docs/11-ui.md 1.8)."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication

MIN_USABLE = QSize(1024, 600)  # below this usable size: "Screen too small" (1.8)


def prepare_hidpi() -> None:
    """Before the app object: scales such as 125 % or 175 % are used as they are, never
    rounded (1.8, no blur or cut text)."""
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
