"""Reports list window S8 (03 F15, 11-ui S8, 20 GAP-83), offscreen: the list comes in
through the same queue the workers use, one test runs against a live service, and a
monkey clicks at random."""

from __future__ import annotations

import json
import os
import random
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from controller.service import ControllerService
from controller.ui.main_window import MainWindow
from controller.ui.store import Done, Status
from core.ui_qt.palette import apply_palette
from core.ui_schema.design import COLORS

REPO = Path(__file__).resolve().parents[2]
LISTING = {
    "reports_dir": "/r",
    "reports": [
        {"run": "R2", "started": "2026-09-27 10:15", "test_type": "pc", "quality": "HD",
         "planned_s": 36000, "actual_s": 3600, "verdict": "INCOMPLETE",
         "verdict_reason": "stopped_by_user", "with_warnings": False,
         "report": "/r/R2/report.html", "folder": "/r/R2", "why": None},
        {"run": "R1", "started": "2026-09-26 12:00", "test_type": None, "quality": None,
         "planned_s": None, "actual_s": None, "verdict": None, "verdict_reason": None,
         "with_warnings": None, "report": "/r/R1/report.html", "folder": "/r/R1",
         "why": "ZL-RPT-008 No report for R1"},
        {"run": "R0", "started": "2026-09-25 09:00", "test_type": "pc", "quality": "UHD",
         "planned_s": 600, "actual_s": 600, "verdict": "PASS",
         "verdict_reason": "planned_time_reached", "with_warnings": False,
         "report": "/r/R0/report.html", "folder": "/r/R0", "why": None},
    ],
}  # fmt: skip


@pytest.fixture(scope="module")
def app() -> QApplication:
    a = QApplication.instance() or QApplication([])
    assert isinstance(a, QApplication)
    apply_palette(a)
    return a


@pytest.fixture
def win(app: QApplication) -> Iterator[tuple[MainWindow, list[str]]]:
    opened: list[str] = []
    w = MainWindow(port=1, ask=lambda t, x: True, tell=lambda t, x: None,
                   show_result=lambda v: None, opener=opened.append)  # fmt: skip
    w.show()
    yield w, opened
    w.timer.stop()
    w.close()


def feed(w: MainWindow, *msgs: tuple[str, Any]) -> None:
    for m in msgs:
        w.q.put(m)
    w.drain()
    QApplication.processEvents()


def answer(w: MainWindow, ok: bool, data: dict[str, Any]) -> None:
    """Plays the command worker for the queued list read."""
    job = w.commands._jobs.get_nowait()
    assert job is not None and job[0] == "reports_list" and job[1] == "GET /v1/controller/reports"
    w.commands._busy.discard("reports_list")
    error = None if ok else {"code": "ZL-DSC-014", "what": "service did not answer"}
    feed(w, ("done", Done("reports_list", ok, data, error)))


def test_list_colors_buttons_and_double_click(win: tuple[MainWindow, list[str]]) -> None:
    w, opened = win
    feed(w, ("status", Status(True, "b", {"state": "RUNNING"})))
    assert w.b_reports.isEnabled()  # also during a test (1.10)
    w.b_reports.click()
    r = w.reports_window
    assert r is not None and r.isVisible()
    feed(w)
    assert r.notes.text() == "Reading the reports…" and not r.b_refresh.isEnabled()
    answer(w, True, LISTING)
    m = r.model
    assert [x.run for x in m.rows] == ["R2", "R1", "R0"]
    assert m.data(m.index(0, 2), Qt.ItemDataRole.DisplayRole) == "IPTV test · HD"
    assert m.data(m.index(0, 3), Qt.ItemDataRole.DisplayRole) == "1h 00m / 10h 00m"
    assert m.data(m.index(0, 5), Qt.ItemDataRole.DisplayRole) == "stopped by user"
    assert m.data(m.index(2, 4), Qt.ItemDataRole.DisplayRole) == "✔ PASS"
    assert m.data(m.index(2, 4), Qt.ItemDataRole.ForegroundRole).name() == COLORS["green"]
    assert m.data(m.index(1, 4), Qt.ItemDataRole.DisplayRole) == "○ ZL-RPT-008 No report for R1"
    assert r.notes.text() == "Reports are saved in /r"
    show1, folder1 = r.row_buttons[1]
    assert not show1.isEnabled() and show1.toolTip() == "This run has no report"
    assert folder1.isEnabled()
    folder1.click()
    r.row_buttons[0][0].click()
    r.table.doubleClicked.emit(m.index(2, 0))
    r.table.doubleClicked.emit(m.index(1, 0))  # no report: nothing opens
    assert opened == ["/r/R1", "/r/R2/report.html", "/r/R0/report.html"]
    for _ in range(3):  # refresh clicks: one read at a time
        r.b_refresh.click()
    assert w.commands._jobs.qsize() == 1
    answer(w, False, {})
    assert r.notes.text() == "Could not read the reports: service did not answer"
    assert m.rows == ()


def test_monkey_reports_window(win: tuple[MainWindow, list[str]]) -> None:
    w, opened = win
    rng = random.Random(3)
    feed(w, ("status", Status(True, "b", {"state": "IDLE"})))
    w.b_reports.click()
    r = w.reports_window
    assert r is not None
    for _ in range(200):
        step = rng.randrange(5)
        if step == 0:
            r.b_refresh.click()
        elif step == 1 and w.commands.busy("reports_list"):
            answer(w, rng.random() < 0.8, LISTING)
        elif step == 2 and r.row_buttons:
            rng.choice(r.row_buttons)[rng.randrange(2)].click()
        elif step == 3 and r.model.rows:
            r.table.selectRow(rng.randrange(len(r.model.rows)))
        else:
            w.b_reports.click()
        QApplication.processEvents()
        assert w.commands._jobs.qsize() <= 1
        assert len(r.row_buttons) == len(r.model.rows)
        for (show, _), row in zip(r.row_buttons, r.model.rows, strict=True):
            assert show.isEnabled() == row.has_report
    assert all(p.startswith("/r/") for p in opened)


def test_against_a_live_service(app: QApplication, tmp_path: Path) -> None:
    rep = tmp_path / "rep" / "R20260927-080000"
    rep.mkdir(parents=True)
    (rep / "summary.json").write_text(json.dumps({"verdict": "PASS", "test_type": "pc",
                                                  "quality": "HD", "planned_s": 600,
                                                  "actual_s": 600}))  # fmt: skip
    svc = ControllerService(tmp_path / "ctl", tmp_path / "rep", REPO / "modules",
                            host="127.0.0.1", port=0, local_port=0)  # fmt: skip
    svc.start()
    w = MainWindow(svc.node.local.port, feed_interval_s=0.2)
    try:
        w.start()
        w.on_reports()
        r = w.reports_window
        assert r is not None
        end = time.monotonic() + 10
        while not r.model.rows and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.02)
        assert [(x.run, x.verdict, x.started) for x in r.model.rows] == [
            ("R20260927-080000", "PASS", "2026-09-27 08:00")]  # fmt: skip
    finally:
        w.shutdown()
        svc.stop()
