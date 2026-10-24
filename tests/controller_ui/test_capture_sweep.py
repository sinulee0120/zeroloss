"""Screen size x display scale sweep (docs/11-ui.md 1.11, 7, 20 GAP-82/83), offscreen.

Every window is laid out at the real size the user gets (resolution / scale, 11-ui 1.11)
and checked by machine: nothing sticks out of the window, no text under 9 pt, every
button as wide as its text, the window can shrink to that size, and the capture is not
blank. A forced dark OS theme must not change our palette. Failing sizes are listed
together so one run shows every broken combination.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QLabel,
    QPushButton,
    QWidget,
)

from controller.ui.main_window import MainWindow
from controller.ui.store import Done, Status
from core.ui_qt.palette import apply_palette
from core.ui_schema.design import BUTTON_MIN_HEIGHT, COLORS, MIN_FONT_PT
from tests.controller_ui.test_main_window import running
from tests.controller_ui.test_reports_window import LISTING as REPORTS
from tests.controller_ui.test_storage_window import LISTING as STORAGE

SCREENS = [(1280, 720), (1366, 768), (1920, 1080), (2560, 1440), (3840, 2160)]
SCALES = [1.0, 1.25, 1.5, 1.75, 2.0]  # 11-ui 1.8
SMALLEST = (1024, 600)  # 11-ui 1.8: below this only a notice is promised


def sizes() -> list[tuple[int, int]]:
    out = {(round(w / s), round(h / s)) for w, h in SCREENS for s in SCALES}
    return sorted(x for x in out if x[0] >= SMALLEST[0] and x[1] >= SMALLEST[1])


@pytest.fixture(scope="module")
def app() -> QApplication:
    a = QApplication.instance() or QApplication([])
    assert isinstance(a, QApplication)
    apply_palette(a)
    return a


@pytest.fixture
def win(app: QApplication) -> Iterator[MainWindow]:
    w = MainWindow(port=1, ask=lambda t, x: True, tell=lambda t, x: None,
                   show_result=lambda v: None, opener=lambda p: None)  # fmt: skip
    w.show()
    yield w
    w.timer.stop()
    for sub in (w.storage_window, w.reports_window):
        if sub is not None:
            sub.close()
    w.close()


def feed(w: MainWindow, *msgs: tuple[str, Any]) -> None:
    for m in msgs:
        w.q.put(m)
    w.drain()
    QApplication.processEvents()


def answer(w: MainWindow, name: str, data: dict[str, Any]) -> None:
    jobs = []
    while not w.commands._jobs.empty():
        job = w.commands._jobs.get_nowait()
        assert job is not None
        jobs.append(job)
    for j in jobs:
        if j[0] != name:
            w.commands._jobs.put(j)
    w.commands._busy.discard(name)
    feed(w, ("done", Done(name, True, data, None)))


def clipped(widget: QWidget, window: QWidget) -> bool:
    """True when the widget (not inside a scrolling area) sticks out of the window."""
    p = widget.parentWidget()
    while p is not None and p is not window:
        if isinstance(p, QAbstractScrollArea):
            return False  # a scrolling area clips on purpose
        p = p.parentWidget()
    top_left = widget.mapTo(window, widget.rect().topLeft())
    box = widget.rect().translated(top_left)
    return not window.rect().adjusted(-1, -1, 1, 1).contains(box)


def problems_of(window: QWidget, logical: tuple[int, int]) -> list[str]:
    bad: list[str] = []
    if window.minimumSizeHint().width() > logical[0]:
        bad.append(f"cannot shrink to {logical[0]} px (needs {window.minimumSizeHint().width()})")
    for widget in window.findChildren(QWidget):
        if not widget.isVisible():
            continue
        name = type(widget).__name__ + ":" + (getattr(widget, "text", lambda: "")() or "")[:20]
        if clipped(widget, window):
            bad.append(f"sticks out: {name}")
        if isinstance(widget, QLabel) and widget.font().pointSizeF() < MIN_FONT_PT:
            bad.append(f"font under 9 pt: {name}")
        if isinstance(widget, QPushButton | QCheckBox):
            if widget.fontMetrics().horizontalAdvance(widget.text()) > widget.width():
                bad.append(f"text wider than the button: {name}")
            if isinstance(widget, QPushButton) and widget.height() < BUTTON_MIN_HEIGHT:
                bad.append(f"button lower than {BUTTON_MIN_HEIGHT} px: {name}")
    return bad


def blank(window: QWidget, path: Path) -> bool:
    img: QImage = window.grab().toImage()
    img.save(str(path))
    first = img.pixel(0, 0)
    return all(img.pixel(x, y) == first
               for x in range(0, img.width(), 17) for y in range(0, img.height(), 17))  # fmt: skip


def sweep(window: QWidget, tmp: Path, label: str) -> list[str]:
    out: list[str] = []
    for w, h in sizes():
        window.resize(w, h)
        QApplication.processEvents()
        for p in problems_of(window, (w, h)):
            out.append(f"{label} {w}x{h}: {p}")
        if blank(window, tmp / f"{label}-{w}x{h}.png"):
            out.append(f"{label} {w}x{h}: blank capture")
    return out


@pytest.mark.parametrize("state", ["IDLE", "RUNNING", "FAULT"])
def test_main_window_at_every_size(win: MainWindow, state: str, tmp_path: Path) -> None:
    feed(
        win, ("status", running(state) if state != "IDLE" else Status(True, "b", {"state": "IDLE"}))
    )
    found = sweep(win, tmp_path, f"main-{state}")
    assert not found, "\n".join(found)


def test_below_the_smallest_size_a_notice_says_so_and_start_stays(win: MainWindow) -> None:
    feed(win, ("status", running("RUNNING")))
    win.resize(*SMALLEST)
    QApplication.processEvents()
    assert win.l_too_small.isHidden()
    for w, h in [(683, 384), (800, 450), (1023, 700), (1100, 599)]:
        win.resize(w, h)
        QApplication.processEvents()
        assert win.l_too_small.isVisible() and win.b_start.isVisible(), (w, h)
    win.resize(1280, 720)
    QApplication.processEvents()
    assert win.l_too_small.isHidden()


def test_storage_and_reports_windows_at_every_size(win: MainWindow, tmp_path: Path) -> None:
    feed(win, ("status", Status(True, "b", {"state": "IDLE"})))
    win.b_storage.click()
    answer(win, "storage_list", STORAGE)
    win.b_reports.click()
    answer(win, "reports_list", REPORTS)
    assert win.storage_window is not None and win.reports_window is not None
    found = sweep(win.storage_window, tmp_path, "storage")
    found += sweep(win.reports_window, tmp_path, "reports")
    assert not found, "\n".join(found)


def test_a_dark_os_theme_does_not_change_our_palette(app: QApplication) -> None:
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    try:
        apply_palette(app)
        w = MainWindow(port=1, ask=lambda t, x: True, tell=lambda t, x: None,
                       show_result=lambda v: None, opener=lambda p: None)  # fmt: skip
        w.show()
        QApplication.processEvents()
        pal = w.palette()
        assert pal.color(QPalette.ColorRole.Window) == QColor(COLORS["bg"])
        assert pal.color(QPalette.ColorRole.WindowText) == QColor(COLORS["text"])
        w.timer.stop()
        w.close()
    finally:
        app.styleHints().unsetColorScheme()
        apply_palette(app)


def test_problems_and_action_windows_at_every_size(win: MainWindow, tmp_path: Path) -> None:
    """The two windows opened from the badges (F21 Problems, F22 Action Required)."""
    from tests.controller_ui.test_main_window import actions_status
    from tests.controller_ui.test_view import problems_status

    feed(win, ("status", problems_status()))
    win.b_problems.click()
    feed(win, ("status", actions_status()))
    win.b_actions.click()
    QApplication.processEvents()
    assert win.problems_window is not None and win.actions_window is not None
    found = sweep(win.problems_window, tmp_path, "problems")
    found += sweep(win.actions_window, tmp_path, "actions")
    win.problems_window.close()
    win.actions_window.close()
    assert not found, "\n".join(found)


def test_a_dark_os_theme_does_not_change_the_other_windows(
    app: QApplication, win: MainWindow
) -> None:
    """Every window, not only the main one, keeps our palette (11-ui 1.11, 7)."""
    from tests.controller_ui.test_main_window import actions_status
    from tests.controller_ui.test_view import problems_status

    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    try:
        apply_palette(app)
        feed(win, ("status", problems_status()))
        win.b_problems.click()
        feed(win, ("status", actions_status()))
        win.b_actions.click()
        win.b_storage.click()
        answer(win, "storage_list", STORAGE)
        win.b_reports.click()
        answer(win, "reports_list", REPORTS)
        windows = [win.problems_window, win.actions_window, win.storage_window,
                   win.reports_window]  # fmt: skip
        for w in windows:
            assert w is not None
            pal = w.palette()
            assert pal.color(QPalette.ColorRole.Window) == QColor(COLORS["bg"]), w
            assert pal.color(QPalette.ColorRole.WindowText) == QColor(COLORS["text"]), w
            w.close()
    finally:
        app.styleHints().unsetColorScheme()
        apply_palette(app)
