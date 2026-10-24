"""Agent Desktop status window, offscreen (docs/03-features.md F20, 11-ui 1.11, 1.13, 6).

States drawn from made-up node answers: idle, sending, receiving fine / with loss /
player frozen, Controller lost in a test, node not answering, second Desktop. Close
hides; [Details] folds; nothing is redrawn when nothing changed; no text below 9 pt;
nothing wider than the window. Captures go to the test folder for the 1.11 review.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from agent.desktop.view import StatusView, status_view
from agent.desktop.window import Elided, StatusWindow
from core.ui_qt.palette import apply_palette
from core.ui_schema.design import MIN_FONT_PT
from modules.iptv.desktop import status_rows

ROWS: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]] = {"iptv": status_rows}


def node(state: str = "IDLE", connected: bool = True, **iptv: Any) -> dict[str, Any]:
    return {
        "node": {"state": state, "role": "agent", "boot_id": "b"},
        "controller": {"connected": connected, "last_seen_s": 0.4},
        "my_ips": [{"nic": "enp0s31f6", "ip": "10.0.0.2", "wireless": False},
                   {"nic": "wlp4s0", "ip": "192.168.1.23", "wireless": True}],
        "module_data": {"iptv": {"sender": {"channels": iptv.get("tx", [])},
                                 "receivers": iptv.get("rx", [])}},
        "problems": iptv.get("problems", []),
    }  # fmt: skip


TX = [{"ch": n, "group": f"239.1.1.{n}:5004", "state": "RUNNING", "tx_bitrate_bps": 35e6}
      for n in (1, 2, 3)]  # fmt: skip


def rx(lost: int = 0, pb: str = "ok", e: int = 10) -> list[dict[str, Any]]:
    last = {"e": e, "ch": 2, "jit": 2.1, "br": 35e6, "lost": lost, "pb": pb}
    return [{"id": "rx1", "ch": 2, "state": "RUNNING", "measuring": True, "lost_total": lost,
             "last_1s": last}]  # fmt: skip


CASES: dict[str, tuple[dict[str, Any] | None, str | None, str]] = {
    "idle": (node(), None, "grey"),
    "sending": (node("RUNNING", tx=TX), None, "green"),
    "receiving": (node("RUNNING", rx=rx()), None, "green"),
    "loss": (node("RUNNING", rx=rx(lost=3)), None, "red"),
    "frozen": (node("RUNNING", rx=rx(pb="freeze")), None, "yellow"),
    "controller_lost": (node("RUNNING", connected=False, rx=rx()), None, "red"),
    "node_down": (None, None, "red"),
    "second_desktop": (node(), "Another Agent Desktop is already connected", "red"),
}


@pytest.fixture(scope="module")
def app() -> QApplication:
    a = QApplication.instance() or QApplication([])
    assert isinstance(a, QApplication)
    apply_palette(a)
    return a


def make(status: dict[str, Any] | None, refused: str | None = None,
         tray: bool = True) -> tuple[StatusWindow, dict[str, Any]]:  # fmt: skip
    box: dict[str, Any] = {"status": status, "refused": refused, "saved": []}

    def view() -> StatusView:
        return status_view(box["status"], ROWS, box["refused"])

    win = StatusWindow(view, True, box["saved"].append, tray)
    win.timer.stop()  # the test drives refresh()
    return win, box


@pytest.mark.parametrize("name", list(CASES))
def test_each_state(app: QApplication, name: str, tmp_path: Path) -> None:
    status, refused, color = CASES[name]
    win, _ = make(status, refused)
    win.resize(320, 10)
    win.show()
    app.processEvents()
    assert win.view is not None and win.view.color == color, win.view
    texts = [w.full_text() for w in win.findChildren(Elided)]
    if name == "receiving":
        assert "Receiving CH2" in texts and "2.1 ms" in texts and "0" in texts
        assert "Player   Playing" in texts and "Controller: Connected" in texts
        assert "My IP: 10.0.0.2 (enp0s31f6)" in texts and "ZeroLoss Receiver" in texts
    if name == "sending":
        assert any("CH3 239.1.1.3:5004" in t and "35.0 Mbps" in t for t in texts)
        assert "ZeroLoss Sender" in texts
    if name == "controller_lost":
        assert "Controller connection lost" in texts
    if name == "node_down":
        assert "Node not answering on this PC" in texts
    for lab in win.findChildren(QLabel):  # 11-ui 1.11: 9 pt at least, never wider
        assert lab.font().pointSize() >= MIN_FONT_PT, lab.text()
        if lab.isVisible():
            assert lab.mapTo(win, lab.rect().topRight()).x() <= win.width(), lab.text()
    win.grab().save(str(tmp_path / f"desktop-{name}.png"))
    win.deleteLater()


def test_close_hides_and_details_fold(app: QApplication) -> None:
    win, box = make(node("RUNNING", rx=rx()))
    win.show()
    win.close()
    app.processEvents()
    assert not win.isVisible()  # F20: hidden, the Desktop goes on
    win.show()
    assert not win.list.isVisible()  # folded by default
    win.more.setChecked(True)
    for e in range(11, 14):
        box["status"] = node("RUNNING", rx=rx(e=e))
        win.refresh()
    lines = win.list.toPlainText().splitlines()
    assert len(lines) == 4 and "lost 0" in lines[-1]  # each second once, 10..13
    win.on_top.setChecked(False)
    assert box["saved"] == [False]
    win.deleteLater()


def test_no_tray_minimizes_instead(app: QApplication) -> None:
    win, _ = make(node(), tray=False)
    win.show()
    win.close()
    app.processEvents()
    assert win.isVisible() and win.isMinimized()
    win.deleteLater()


def test_no_redraw_when_nothing_changed(app: QApplication) -> None:
    win, box = make(node("RUNNING", rx=rx()))
    item = win.rows_box.itemAt(0)
    assert item is not None
    before = item.widget()
    win.refresh()
    again = win.rows_box.itemAt(0)
    assert again is not None and again.widget() is before  # same view: widgets kept
    box["status"] = node("RUNNING", rx=rx(lost=1))
    win.refresh()
    assert win.view is not None and win.view.color == "red"
    win.deleteLater()


def test_a_broken_module_row_is_shown_not_raised() -> None:
    def bad(_: dict[str, Any]) -> list[dict[str, Any]]:
        raise KeyError("x")

    v = status_view(node("RUNNING"), {"iptv": bad}, None, {"other": "ImportError"})
    titles = [r.title for r in v.rows]
    assert titles == ["iptv: cannot show", "other: not loaded"] and v.color == "yellow"


def test_isp_role_line_and_the_router_wan_card() -> None:
    """20 W-4: the sender PC's window says whether it gives the router WAN its address
    and which card faces the router; nothing on a PC without the role."""
    st = node()
    st["wan"] = {"mode": "auto", "state": "ready", "nic": "enp0s31f6", "why": None}
    footer = status_view(st, ROWS).footer
    assert "My IP: 10.0.0.2 (enp0s31f6, router WAN)" in footer
    assert "My IP: 192.168.1.23 (wlp4s0, Wi-Fi)" in footer
    assert "ISP role: ready on enp0s31f6 (router WAN gets its address here)" in footer
    st["wan"] = {"mode": "on", "state": "waiting_nic", "nic": None,
                 "why": "enp0s31f6 lost its link (cable out or moved)"}  # fmt: skip
    footer = status_view(st, ROWS).footer
    assert "ISP role: waiting — enp0s31f6 lost its link (cable out or moved)" in footer
    assert not any("router WAN" in x for x in footer if x.startswith("My IP"))
    st["wan"] = {"mode": "auto", "state": "off", "nic": None, "why": None}
    assert not any(x.startswith("ISP role") for x in status_view(st, ROWS).footer)
    del st["wan"]  # an older node without the field
    assert not any(x.startswith("ISP role") for x in status_view(st, ROWS).footer)


@pytest.mark.parametrize("name", list(CASES))
def test_every_state_fits_its_window_and_the_screen(app: QApplication, name: str) -> None:
    """11-ui 1.8 / 1.11 for the small status window: at its own size nothing is cut,
    no text under 9 pt, buttons as wide as their text, and placed in the corner it is
    fully on the screen's free area (not under the taskbar)."""
    from tests.controller_ui.test_capture_sweep import problems_of

    status, refused, _ = CASES[name]
    win, _box = make(status, refused)
    win.show()
    win.adjustSize()
    app.processEvents()
    size = (win.width(), win.height())
    assert not problems_of(win, size), problems_of(win, size)
    win.place_bottom_right()
    app.processEvents()
    free = win.screen().availableGeometry()
    assert free.contains(win.frameGeometry()), (free, win.frameGeometry())
    win.close()


def test_a_dark_os_theme_does_not_change_the_status_window(app: QApplication) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPalette

    from core.ui_schema.design import COLORS

    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    try:
        apply_palette(app)
        win, _box = make(*CASES["receiving"][:2])
        win.show()
        app.processEvents()
        assert win.palette().color(QPalette.ColorRole.Window) == QColor(COLORS["bg"])
        assert win.palette().color(QPalette.ColorRole.WindowText) == QColor(COLORS["text"])
        for label in win.findChildren(QLabel):  # no light-on-light text from the OS theme
            assert label.palette().color(QPalette.ColorRole.WindowText) != QColor("#ffffff")
        win.close()
    finally:
        app.styleHints().unsetColorScheme()
        apply_palette(app)
