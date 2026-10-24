"""Controller main screen at every screen size and display scale (11-ui 1.8, 7 "해상도·배율").

Run once per display scale (Qt fixes the scale when the app starts):
``QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=1.5 python -m tests.controller_ui.screen_sizes
--out DIR``. For each screen size and each screen state it sizes the window to what the
screen really offers (size ÷ scale, minus a taskbar), saves a capture, and prints one JSON
line per problem. No line means no problem.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRect, Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLabel,
    QScrollArea,
    QWidget,
)

from controller.ui.main_window import NARROW, MainWindow
from controller.ui.store import Status
from core.ui_qt.screen import MIN_USABLE, prepare_hidpi

SCREENS = ((1366, 768), (1920, 1080), (2560, 1440), (3840, 2160))  # 11-ui 7
SCALES = (1.0, 1.25, 1.5, 1.75, 2.0)
TASKBAR_PX = 48  # Windows taskbar at 100 %, in logical pixels (the window cannot use it)

ROW = {
    "color": "green", "why": "", "link_label": "LAN", "channel": 2,
    "items": [{"label": "Jitter", "value": 2.1, "unit": "ms", "key": True},
              {"label": "Loss", "value": 0, "unit": "", "key": True},
              {"label": "▶ Playing", "value": None, "unit": "", "key": False}],
}  # fmt: skip
TX_ROW = {
    "color": "green", "why": "", "link_label": "WAN", "channel": None,
    "items": [{"label": "Sending 3 ch ·", "value": 105.0, "unit": "Mbps", "key": False}],
}  # fmt: skip
BROADCAST = {"on": True, "since_utc": "2026-10-02T01:00:00Z", "channels": [1, 2, 3], "sender": "tx"}
PROBLEMS = {"counts": {"error": 2, "warn": 5}, "problems": []}


def _dev(row: dict[str, Any]) -> dict[str, Any]:
    links = [{"nic": "eth0", "label": "LAN", "level": "ok", "link_mbps": 1000}]
    return {"suspect": False, "age_s": 0.3, "module_data": {}, "links": [],
            "link_checks": links, "wifi_changes": [], "row": row}  # fmt: skip


def states() -> Iterator[tuple[str, Status]]:
    """A tablet test of three tablets: the most rows a screen gets (01)."""
    names = ["tx", "tablet-1", "tablet-2", "tablet-3"]
    devices = tuple({"name": n, "host": f"10.0.0.{i + 2}"} for i, n in enumerate(names))
    run_devices = {"tx": _dev(TX_ROW)} | {n: _dev(ROW) for n in names[1:]}
    cells = {n: [{"index": i, "partial": False, "color": "green", "reasons": []}
                 for i in range(20)] for n in names[1:]}  # fmt: skip
    channels: dict[str, Any] = {"on_air": [1, 2, 3], "auto": None, "next_in_s": {}}
    run = {"state": "RUNNING", "run_id": "R20261002-010000", "elapsed_s": 6012.0,
           "planned_s": 360000, "cell_s": 300, "devices": run_devices, "cells": cells,
           "channels": channels, "result": None}  # fmt: skip
    yield "running", Status(True, "b", run, devices, problems=PROBLEMS, broadcast=BROADCAST)
    yield "idle", Status(True, "b", {"state": "IDLE"}, devices, broadcast=BROADCAST)
    fault = {"state": "FAULT", "run_id": "R1", "devices": run_devices,
             "fault": {"device": "tablet-2", "reason": "tablet-2: no answer for 10 s",
                       "at_utc": "2026-10-02T01:00:10.000Z",
                       "last_ok_utc": "2026-10-02T01:00:00.000Z", "run_id": "R1"},
             "result": {"run_id": "R1", "verdict": "INCOMPLETE"}, "reset": None}  # fmt: skip
    yield "fault", Status(True, "b", fault, devices, problems=PROBLEMS)


def _rect_in(w: QWidget, top: QWidget) -> QRect:
    return QRect(w.mapTo(top, QPoint(0, 0)), w.size())


def _in_scroll(w: QWidget) -> bool:
    """Inside a part that scrolls by design (not the whole page)."""
    p = w.parentWidget()
    while p is not None:
        if isinstance(p, QScrollArea) and p.objectName() != "page":
            return True
        p = p.parentWidget()
    return False


def _side_locked(w: QWidget) -> QScrollArea | None:
    """The nearest area around it that scrolls only up and down (the Test area, the
    Advanced settings): what does not fit sideways there is cut, not scrolled (GAP-111)."""
    p = w.parentWidget()
    while p is not None:
        if isinstance(p, QScrollArea) and p.objectName() != "page":
            off = p.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            return p if off else None
        p = p.parentWidget()
    return None


def _elided(w: QWidget) -> bool:
    return type(w).__name__ == "Elided"  # long text ends in "…" + tooltip (1.11)


def check(win: MainWindow, usable: tuple[int, int]) -> list[str]:
    """What breaks 11-ui 1.8 / 1.11 at this window size. Empty list = fine."""
    out: list[str] = []
    w, h = usable
    if win.width() > w or win.height() > h:
        out.append(f"window {win.width()}x{win.height()} does not fit {w}x{h}")
    narrow = w < NARROW.width() or h < NARROW.height()
    if win.narrow != narrow:
        out.append(f"layout narrow={win.narrow}, expected {narrow}")
    small = w < MIN_USABLE.width() or h < MIN_USABLE.height()
    if win.l_too_small.isVisible() != small:
        out.append(f"'Screen too small' shown={win.l_too_small.isVisible()}, expected {small}")
    bars = (win.page.horizontalScrollBar(), win.page.verticalScrollBar())
    if not small and any(b.isVisible() for b in bars):
        out.append("the whole page scrolls on a supported screen size")
    if small:  # below 1024x600 parts may scroll; Start/Stop and the notice stay in view
        pinned: tuple[tuple[str, QWidget], ...] = (("Start/Stop", win.b_start),
                                                  ("notice", win.l_too_small))  # fmt: skip
        for key, part in pinned:
            shown = QRect(part.mapTo(win.page.viewport(), QPoint(0, 0)), part.sizeHint())
            if not win.page.viewport().rect().contains(shown):  # its text, not its stretch
                out.append(f"{key} is not in view on a small screen")
        return out
    whole = win.rect()
    keys: tuple[tuple[str, QWidget], ...] = (("Start/Stop", win.b_start),
                                            ("problems badge", win.b_problems))  # fmt: skip
    for key, widget in keys:
        if widget.isVisible() and not whole.contains(_rect_in(widget, win)):
            out.append(f"{key} is not fully on screen")
    for name, row in win._rows.items():
        if row.isVisible() and row.key.visibleRegion().isEmpty() and name == "tablet-1":
            out.append("Jitter / Loss of the first receiver are not on screen")
    for widget in win.findChildren(QWidget):
        if not widget.isVisible():
            continue
        area = _side_locked(widget)
        if area is not None and isinstance(widget, (QLabel, QAbstractButton, QComboBox,
                                                    QAbstractSpinBox)):  # fmt: skip
            r = QRect(widget.mapTo(area.viewport(), QPoint(0, 0)), widget.size())
            port = area.viewport().rect()
            if r.left() < port.left() or r.right() > port.right():
                out.append(f"{type(widget).__name__} '{_text(widget)}' is cut at the side")
            wraps = isinstance(widget, QLabel) and widget.wordWrap()
            if (isinstance(widget, (QLabel, QAbstractButton)) and not _elided(widget)
                    and not wraps and widget.width() + 1 < widget.sizeHint().width()):  # fmt: skip
                out.append(f"{type(widget).__name__} '{_text(widget)}' is narrower than its "
                           f"text ({widget.width()} < {widget.sizeHint().width()})")  # fmt: skip
            if isinstance(widget, QComboBox) and widget.width() < 8 * 8:  # GRID 8: a list
                out.append(f"QComboBox '{_text(widget)}' is too narrow to read ({widget.width()})")
        if _in_scroll(widget):
            continue
        if isinstance(widget, (QLabel, QAbstractButton, QComboBox)):
            if not whole.contains(_rect_in(widget, win)):
                out.append(f"{type(widget).__name__} '{_text(widget)}' is cut by the window")
            need = widget.minimumSizeHint().width() if isinstance(widget, QComboBox) else (
                widget.sizeHint().width())  # fmt: skip
            wraps = isinstance(widget, QLabel) and widget.wordWrap()
            if not _elided(widget) and not wraps and widget.width() + 1 < need:
                out.append(f"{type(widget).__name__} '{_text(widget)}' is narrower than its "
                           f"text ({widget.width()} < {need})")  # fmt: skip
    return out


def _text(w: QWidget) -> str:
    if isinstance(w, (QLabel, QAbstractButton)):
        return w.text()[:40]
    if isinstance(w, QComboBox):
        return w.currentText()[:40]
    return ""


REPO = Path(__file__).resolve().parents[2]
# the widest Source video list a person makes (real names from the sender PC, GAP-111)
MOVIES = {"files": [
    {"name": "[AV]The greatest showman_clip 01_UHD_Dolby Atmos.mp4", "width": 3840,
     "height": 2160, "duration_s": 101.1, "fits": {"UHD": {"ok": True}, "HD": {"ok": True}}},
    {"name": "2026.03.18 개봉 - [UHD 4K] 프로젝트 헤일메리.mp4", "width": 1280, "height": 720,
     "duration_s": 9382.4, "fits": {"UHD": {"ok": True}, "HD": {"ok": True}}},
]}  # fmt: skip


def advanced_data(win: MainWindow) -> None:
    """The real settings data (what the service sends, 16 1), three channels and a long
    movie list: Advanced as full as it gets — an empty one could not show a cut field."""
    from controller.ui.store import Done
    from core.settings import load_schema
    from tests.controller_ui.test_main_window import feed, setup_answer

    rx = [{"device": f"tablet-{n}", "ch": n, "network": "Wireless"} for n in (1, 2, 3)]
    feed(win, ("schema", load_schema(REPO / "modules")), ("setup", setup_answer(receivers=rx)),
         ("done", Done("sources", True, MOVIES)))  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    scale = float(os.environ.get("QT_SCALE_FACTOR", "1"))
    if sys.platform == "win32":  # offscreen with the fonts and UI font a Windows PC has
        os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ["WINDIR"]) / "Fonts"))
    prepare_hidpi()
    app = QApplication(sys.argv[:1])
    if sys.platform == "win32":
        from PySide6.QtGui import QFont

        app.setFont(QFont("Segoe UI", 9))
    from core.ui_qt.palette import apply_palette

    apply_palette(app)
    args.out.mkdir(parents=True, exist_ok=True)
    from tests.controller_ui.test_main_window import Fakes, feed

    f = Fakes()
    win = MainWindow(port=1, ask=f.ask, tell=f.tell, show_result=f.result, opener=f.open)
    win.show()
    advanced_data(win)
    for sw, sh in SCREENS:
        usable = (round(sw / scale), round(sh / scale) - TASKBAR_PX)
        for state, status in states():
            win.resize(*usable)
            feed(win, ("status", status))
            if state == "idle" and not win.x_advanced.isChecked():
                win.x_advanced.click()  # the most fields the Test area shows
            for _ in range(3):
                app.processEvents()
                app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)  # as the loop does
            tag = f"{sw}x{sh}@{round(scale * 100)}_{state}"
            win.grab().save(str(args.out / f"{tag}.png"))
            problems = check(win, usable)
            if state == "idle" and len(win._adv) < 5:  # G-18: the check saw a real Advanced
                problems.append(f"Advanced shows {len(win._adv)} settings: nothing to check")
            for problem in problems:
                print(json.dumps({"screen": f"{sw}x{sh}", "scale": scale, "state": state,
                                  "usable": usable, "problem": problem}), flush=True)  # fmt: skip
    # 2026-10-17: a Throughput test shows its own settings and the preview on the Test area
    from tests.controller_ui.test_main_window import setup_answer

    answer = setup_answer(module="throughput", test_type="Throughput · All")
    answer["preview"] = [
        "Each case is two separate iperf3 runs, one after the other (never both at once): "
        "LAN→WAN 30 s (the LAN-side PC sends to the WAN-side PC, iperf3), then WAN→LAN 30 s "
        "(the WAN-side PC sends to the LAN-side PC, iperf3 -R) (Advanced: Directions).",
        "Command: iperf3 -c 10.0.0.1 -p 5211 -t 30 -J -P 1 --connect-timeout 5000 "
        "-B <this card's address>   — receiving adds -R.",
    ]
    idle = next(st for name, st in states() if name == "idle")
    feed(win, ("status", idle), ("setup", answer))
    for sw, sh in SCREENS:
        usable = (round(sw / scale), round(sh / scale) - TASKBAR_PX)
        win.resize(*usable)
        for _ in range(3):
            app.processEvents()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        tag = f"{sw}x{sh}@{round(scale * 100)}_throughput"
        win.grab().save(str(args.out / f"{tag}.png"))
        problems = check(win, usable)
        if len(win._mod) < 6:  # G-18: the check saw the Throughput settings
            problems.append(f"the Test area shows {len(win._mod)} Throughput settings")
        for problem in problems:
            print(json.dumps({"screen": f"{sw}x{sh}", "scale": scale, "state": "throughput",
                              "usable": usable, "problem": problem}), flush=True)  # fmt: skip
    win.timer.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
