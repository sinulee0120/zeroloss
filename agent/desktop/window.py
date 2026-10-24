"""The Agent Desktop status window (docs/03-features.md F20, 11-ui 1.x and 6).

View only: it draws ``StatusView`` (view.py) and nothing else, redraws only when that
changed (11-ui 1.13), and never touches the network (the link has its own thread).
Closing hides it; the tray menu shows it again or quits (F20 구현 규칙). Layouts only,
8 px grid, design values from ``core.ui_schema.design``, long text elided with a tooltip.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QFont, QFontMetrics, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from agent.desktop.view import Details, Row, StatusView
from core.ui_schema.design import COLORS, FONT_PT, GRID, ICONS

BADGE = {**ICONS, "grey": ICONS["grey"]}


class Elided(QLabel):
    """A label that never forces the window wider: "…" and the full text as tooltip."""

    def __init__(self, text: str = "", pt: int = FONT_PT["body"], bold: bool = False) -> None:
        super().__init__()
        self._full = ""
        f = QFont(self.font())
        f.setPointSize(pt)
        f.setBold(bold)
        self.setFont(f)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(GRID * 4)
        self.set(text)

    def set(self, text: str) -> None:
        if text != self._full:
            self._full = text
            self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, e: QResizeEvent) -> None:
        super().resizeEvent(e)
        self._elide()

    def _elide(self) -> None:
        shown = QFontMetrics(self.font()).elidedText(
            self._full, Qt.TextElideMode.ElideRight, max(self.width(), GRID * 4)
        )
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")


def badge_text(color: str) -> str:
    return BADGE.get(color, ICONS["grey"])


class StatusWindow(QWidget):
    def __init__(self, view_of: Callable[[], StatusView], on_top: bool,
                 save_on_top: Callable[[bool], None], tray: bool) -> None:  # fmt: skip
        super().__init__()
        self._view_of = view_of
        self._save_on_top = save_on_top
        self._tray = tray  # closing hides only when a tray icon can bring it back
        self.view: StatusView | None = None
        self.details = Details()
        self.setWindowTitle("ZeroLoss Agent")
        self.setMinimumWidth(GRID * 36)
        self.setStyleSheet(f"background: {COLORS['bg']}; color: {COLORS['text']};")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(GRID * 2, GRID * 2, GRID * 2, GRID * 2)
        outer.setSpacing(GRID)
        head = QHBoxLayout()
        self.title = Elided("ZeroLoss", FONT_PT["title"], bold=True)
        self.badge = QLabel()
        head.addWidget(self.title, 1)
        head.addWidget(self.badge)
        outer.addLayout(head)
        self.notice = Elided()
        self.notice.setStyleSheet(f"color: {COLORS['red']}; font-weight: bold;")
        outer.addWidget(self.notice)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(GRID)
        outer.addLayout(self.rows_box)
        self.footer = QVBoxLayout()
        outer.addLayout(self.footer)
        bar = QHBoxLayout()
        self.on_top = QCheckBox("Always on top")
        self.on_top.setChecked(on_top)
        self.on_top.setToolTip("Keep this window above other windows. Kept on this PC only.")
        self.on_top.toggled.connect(self._toggle_on_top)
        self.more = QPushButton("Details ▾")
        self.more.setCheckable(True)
        self.more.setToolTip("Show the last 1,000 one-second values measured on this PC.")
        self.more.toggled.connect(self._toggle_details)
        bar.addWidget(self.on_top)
        bar.addStretch(1)
        bar.addWidget(self.more)
        outer.addLayout(bar)
        self.list = QPlainTextEdit()
        self.list.setReadOnly(True)
        self.list.setMaximumBlockCount(Details.KEEP)
        self.list.setMinimumHeight(GRID * 20)
        self.list.hide()  # F20: folded by default
        outer.addWidget(self.list)
        self._apply_on_top(on_top)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    # ---------------------------------------------------------------- drawing

    def refresh(self) -> None:
        v = self._view_of()
        if self.details.add(v) and self.list.isVisible():
            self._fill_list()
        if v == self.view:
            return  # nothing changed: no redraw (11-ui 1.13)
        self.view = v
        self.title.set(v.title)
        self.badge.setText(badge_text(v.color))
        self.badge.setStyleSheet(f"color: {COLORS.get(v.color, COLORS['grey'])};"
                                 f" font-size: {FONT_PT['title']}pt;")  # fmt: skip
        self.notice.set(v.notice or "")
        self.notice.setVisible(bool(v.notice))
        _clear(self.rows_box)
        for r in v.rows:
            self.rows_box.addWidget(_row_widget(r))
        _clear(self.footer)
        for line in (*v.footer, *v.problems):
            lab = Elided(line)
            lab.setStyleSheet(f"color: {COLORS['text_muted']};")
            self.footer.addWidget(lab)

    def _fill_list(self) -> None:
        self.list.setPlainText("\n".join(self.details.lines))
        self.list.verticalScrollBar().setValue(self.list.verticalScrollBar().maximum())

    def _toggle_details(self, on: bool) -> None:
        self.more.setText("Details ▴" if on else "Details ▾")
        self.list.setVisible(on)
        if on:
            self._fill_list()
        self.adjustSize()

    def _toggle_on_top(self, on: bool) -> None:
        self._apply_on_top(on)
        self._save_on_top(on)
        self.show()

    def _apply_on_top(self, on: bool) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)

    # ---------------------------------------------------------------- window

    def place_bottom_right(self) -> None:
        """11-ui 1.x: bottom right of the screen, off the task bar."""
        screen = self.screen().availableGeometry()
        self.adjustSize()
        g = self.frameGeometry()
        self.move(screen.right() - g.width() - GRID * 2, screen.bottom() - g.height() - GRID * 2)

    def closeEvent(self, e: QCloseEvent) -> None:
        """F20: closing hides; the Desktop and its player go on (Quit ends them)."""
        e.ignore()
        if self._tray:
            self.hide()
        else:
            self.showMinimized()


def _row_widget(r: Row) -> QWidget:
    w = QWidget()
    w.setStyleSheet(f"background: {COLORS['panel']}; border: 1px solid {COLORS['border']};"
                    " border-radius: 4px;")  # fmt: skip
    box = QVBoxLayout(w)
    box.setContentsMargins(GRID, GRID, GRID, GRID)
    box.setSpacing(GRID // 2)
    head = QHBoxLayout()
    title = Elided(r.title, bold=True)
    title.setStyleSheet("border: none;")
    mark = QLabel(badge_text(r.color))
    mark.setStyleSheet(f"border: none; color: {COLORS.get(r.color, COLORS['grey'])};")
    head.addWidget(title, 1)
    head.addWidget(mark)
    box.addLayout(head)
    if r.big:
        big = QHBoxLayout()
        for name, value in r.big:  # F20: Jitter and Packet Loss on top, large
            cell = QVBoxLayout()
            n = Elided(name)
            n.setStyleSheet(f"border: none; color: {COLORS['text_muted']};")
            val = Elided(value, FONT_PT["key"], bold=True)
            val.setStyleSheet("border: none;")
            cell.addWidget(n)
            cell.addWidget(val)
            big.addLayout(cell, 1)
        box.addLayout(big)
    for line in r.lines:
        lab = Elided(line)
        lab.setStyleSheet("border: none;")
        box.addWidget(lab)
    return w


def _clear(box: QVBoxLayout) -> None:
    while box.count():
        item = box.takeAt(0)
        w = item.widget() if item is not None else None
        if w is not None:
            w.deleteLater()
