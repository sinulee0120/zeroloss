"""Manage storage (11-ui S14), the Storage button and [Export diagnostics] (03 F15·F21,
11-ui 1.10, 20 GAP-82), offscreen: answers go in through the same queue the workers
use, one test runs against a live Controller service, and a monkey clicks at random."""

from __future__ import annotations

import os
import random
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from controller.service import ControllerService
from controller.ui.control_state import storage_actions
from controller.ui.main_window import MainWindow
from controller.ui.store import Done, Status
from core.ui_qt.palette import apply_palette

REPO = Path(__file__).resolve().parents[2]
MB = 1024 * 1024
LISTING = {
    "reports_dir": "/r", "free_mb": 20_000, "nodes": {"rx": "ok", "gone": "ZL-DSC-014 no answer"},
    "free_mb_of": {"controller": 20_000, "rx": 900_000},
    "runs": [
        {"run": "R-big", "bytes": 3000 * MB, "raw_bytes": 2900 * MB, "in_use": False,
         "where": {"Reports": {"bytes": 100 * MB, "raw_bytes": 0}, "rx": {"bytes": 2900 * MB}}},
        {"run": "R-now", "bytes": 500 * MB, "raw_bytes": 400 * MB, "in_use": True,
         "where": {"rx": {"bytes": 500 * MB, "raw_bytes": 400 * MB}}},
    ],
}  # fmt: skip


@pytest.fixture(scope="module")
def app() -> QApplication:
    a = QApplication.instance() or QApplication([])
    assert isinstance(a, QApplication)
    apply_palette(a)
    return a


class Fakes:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.answer = True
        self.opened: list[str] = []

    def ask(self, title: str, text: str) -> bool:
        self.asked.append(f"{title} | {text}")
        return self.answer


@pytest.fixture
def win(app: QApplication) -> Iterator[tuple[MainWindow, Fakes]]:
    f = Fakes()
    w = MainWindow(
        port=1,
        ask=f.ask,
        tell=lambda t, x: None,
        show_result=lambda v: None,
        opener=f.opened.append,
    )  # workers not started: nothing is reached
    w.show()  # fmt: skip
    yield w, f
    w.timer.stop()
    w.close()


def feed(w: MainWindow, *msgs: tuple[str, Any]) -> None:
    for m in msgs:
        w.q.put(m)
    w.drain()
    QApplication.processEvents()


def answer(w: MainWindow, name: str, ok: bool, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Plays the command worker for one queued job: takes it, then the answer comes."""
    jobs = []
    while not w.commands._jobs.empty():
        job = w.commands._jobs.get_nowait()
        assert job is not None
        jobs.append(job)
    mine = [j for j in jobs if j[0] == name]
    for j in jobs:
        if j[0] != name:
            w.commands._jobs.put(j)
    assert len(mine) == 1, (name, jobs)
    w.commands._busy.discard(name)
    error = None if ok else {"code": "ZL-DSC-014", "what": "rx did not answer"}
    feed(w, ("done", Done(name, ok, data, error)))
    return mine[0][1], mine[0][2]


def test_storage_button_says_when_a_pc_is_short(win: tuple[MainWindow, Fakes]) -> None:
    w, _ = win
    feed(w, ("status", Status(True, "b", {"state": "IDLE"})))
    assert w.b_storage.text() == "Storage" and w.b_storage.isEnabled()
    low = ({"device": "rx", "free_mb": 10 * 1024, "total_mb": 1000 * 1024},)
    feed(w, ("status", Status(True, "b", {"state": "RUNNING"}, storage_low=low)))
    assert w.b_storage.text() == "⚠ Storage low" and w.b_storage.isEnabled()  # also in a test
    assert "rx (10.0 GB free)" in w.b_storage.toolTip()


def test_manage_storage_lists_confirms_deletes_and_lists_again(
    win: tuple[MainWindow, Fakes],
) -> None:
    w, f = win
    feed(w, ("status", Status(True, "b", {"state": "IDLE"})))
    w.b_storage.click()
    s = w.storage_window
    assert s is not None and s.isVisible()
    feed(w)
    assert s.notes.text().startswith("Reading") and not s.b_refresh.isEnabled()
    path, _ = answer(w, "storage_list", True, LISTING)
    assert path == "GET /v1/controller/storage"
    assert [r.run for r in s.model.rows] == ["R-big", "R-now"]  # biggest first
    assert s.model.rows[0].size == "2.9 GB" and "rx 2.8 GB" in s.model.rows[0].where
    assert "Not read from gone" in s.notes.text() and "controller 19.5 GB" in s.notes.text()
    assert not s.b_raw.isEnabled() and s.b_raw.toolTip() == "Choose a run first"
    s.table.selectRow(1)  # the test running now
    QApplication.processEvents()
    assert not s.b_run.isEnabled() and s.b_run.toolTip() == "This test is running"
    s.table.selectRow(0)
    QApplication.processEvents()
    assert s.b_raw.isEnabled() and s.b_run.isEnabled()
    f.answer = False
    s.b_raw.click()  # cancelled in the confirmation: nothing sent
    assert not w.commands.busy("storage_delete")
    assert "The report stays" in f.asked[-1] and "2.8 GB" in f.asked[-1]
    f.answer = True
    for _ in range(3):  # double clicks: one deletion (11-ui 1.12)
        s.b_raw.click()
    assert s.b_raw.text() == "Deleting…" and not s.b_raw.isEnabled() and not s.b_run.isEnabled()
    nodes = {"Reports": "ok", "rx": "ok", "gone": "ZL-DSC-014 no answer"}
    path, params = answer(w, "storage_delete", True, {
        "run": "R-big", "raw_only": True, "deleted_bytes": 2900 * MB, "nodes": nodes,
        "errors": []})  # fmt: skip
    assert path == "/v1/controller/storage/delete" and params == {"run": "R-big", "raw_only": True}
    assert "Deleted 2.8 GB of R-big" in s.notes.text() and "gone" in s.notes.text()
    assert w.commands.busy("storage_list")  # listed again after the deletion
    s.table.selectRow(0)
    QApplication.processEvents()
    s.b_run.click()
    assert "the report too" in f.asked[-1]


def test_export_diagnostics_once_then_the_path(win: tuple[MainWindow, Fakes]) -> None:
    w, f = win
    feed(w, ("status", Status(True, "b", {"state": "RUNNING"})))
    w.on_problems()
    p = w.problems_window
    assert p is not None and p.b_export.isEnabled() and p.b_export.text() == "Export diagnostics"
    for _ in range(3):
        p.b_export.click()
    assert p.b_export.text() == "Exporting…" and not p.b_export.isEnabled()
    assert w.commands._jobs.qsize() == 1
    nodes = {"rx": "ok", "tab": "ZL-DSC-014 no answer"}
    answer(w, "diagnostics", True, {"path": "/r/R1/diagnostics-x.zip", "bytes": 3 * MB,
                                    "nodes": nodes})  # fmt: skip
    assert (
        p.b_export.isEnabled() and "Saved: /r/R1/diagnostics-x.zip (3 MB)" in p.export_note.text()
    )
    assert "Not from tab" in p.export_note.text()
    p.b_folder.click()
    assert f.opened == [str(Path("/r/R1"))]  # this OS's separators
    p.b_export.click()
    answer(w, "diagnostics", False, {})
    assert "Could not make the file: rx did not answer" in p.export_note.text()
    assert p.b_folder.isHidden()


def test_monkey_storage_window_follows_the_table(win: tuple[MainWindow, Fakes]) -> None:
    """Random clicks, selections and answers: the buttons always equal the 1.10 rule."""
    w, f = win
    rng = random.Random(7)
    feed(w, ("status", Status(True, "b", {"state": "IDLE"})))
    w.b_storage.click()
    s = w.storage_window
    assert s is not None
    for _ in range(200):
        step = rng.randrange(6)
        if step == 0:
            s.b_refresh.click()
        elif step == 1 and s.model.rows:
            s.table.selectRow(rng.randrange(len(s.model.rows)))
        elif step == 2:
            f.answer = rng.random() < 0.5
            rng.choice([s.b_raw, s.b_run]).click()
        elif step == 3 and w.commands.busy("storage_list"):
            answer(w, "storage_list", rng.random() < 0.8, LISTING)
        elif step == 4 and w.commands.busy("storage_delete"):
            answer(w, "storage_delete", rng.random() < 0.8,
                   {"run": "R-big", "deleted_bytes": 1, "nodes": {}, "errors": []})  # fmt: skip
        else:
            time.sleep(0.001)
        QApplication.processEvents()
        w.redraw()
        chosen = s.selected()
        want = storage_actions(chosen.run if chosen else None, bool(chosen and chosen.in_use),
                               w.store.pending)  # fmt: skip
        assert s.b_raw.isEnabled() == want["delete_raw"].enabled
        assert s.b_run.isEnabled() == want["delete_run"].enabled
        assert s.b_raw.text() == want["delete_raw"].label
        assert w.commands._jobs.qsize() <= 2  # one list and one deletion at most


def test_against_a_live_service(app: QApplication, tmp_path: Path) -> None:
    svc = ControllerService(tmp_path / "ctl", tmp_path / "rep", REPO / "modules",
                            host="127.0.0.1", port=0, local_port=0)  # fmt: skip
    run = tmp_path / "rep" / "R-old"
    (run / "raw" / "rx").mkdir(parents=True)
    (run / "raw" / "rx" / "R-old_h000.jsonl").write_bytes(b"x" * 2 * MB)
    (run / "summary.json").write_text("{}")
    svc.start()
    asked: list[str] = []

    def ask(title: str, text: str) -> bool:
        asked.append(title)
        return True

    w = MainWindow(svc.node.local.port, feed_interval_s=0.2, ask=ask)
    try:
        w.start()
        w.on_storage()
        s = w.storage_window
        assert s is not None
        end = time.monotonic() + 10
        while not s.model.rows and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.02)
        assert [r.run for r in s.model.rows] == ["R-old"] and s.model.rows[0].raw == "2 MB"
        s.table.selectRow(0)
        QApplication.processEvents()
        s.b_raw.click()
        assert asked == ["Delete the raw data of R-old?"]
        end = time.monotonic() + 10
        while (run / "raw").exists() and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.02)
        assert not (run / "raw").exists() and (run / "summary.json").is_file()
        end = time.monotonic() + 10
        while "Deleted" not in s.notes.text() and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.02)
        assert "Deleted 2 MB of R-old" in s.notes.text()
    finally:
        w.shutdown()
        svc.stop()
