"""Manual pictures from the real screens (docs/08-manual.md rule 7, 20 GAP-94).

Runs the Controller screens offscreen with fake states and saves one PNG per screen, so
the manual's pictures are always the current build. ``python -m tools.manual_shots OUT``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from controller.ui.main_window import MainWindow
from controller.ui.store import Done, Status
from core.ui_qt.palette import apply_palette

STORAGE = {"reports_dir": "/r", "free_mb": 20000, "nodes": {"rx": "ok"},
           "free_mb_of": {"controller": 20000}, "runs": []}  # fmt: skip
SIZE = (1280, 720)  # the standard layout (11-ui 1.8)
ROW = {"color": "green", "why": "", "link_label": "LAN", "items": [
    {"label": "CH1", "value": None, "unit": "", "key": False},
    {"label": "Jitter", "value": 2.1, "unit": "ms", "key": True},
    {"label": "Loss", "value": 0, "unit": "", "key": True}]}  # fmt: skip


def _status(state: str) -> Status:
    if state == "IDLE":
        return Status(True, "b", {"state": "IDLE"})
    dev = {"suspect": False, "age_s": 0.2, "module_data": {}, "links": [], "wifi_changes": [],
           "row": ROW}  # fmt: skip
    run: dict[str, Any] = {
        "state": state, "run_id": "R1", "elapsed_s": 12.0, "planned_s": 600, "cell_s": 300,
        "devices": {"rx": dev},
        "cells": {"rx": [{"index": 0, "partial": False, "color": "green", "reasons": []}]},
        "result": None,
    }  # fmt: skip
    return Status(True, "b", run, ({"name": "rx", "host": "10.0.0.3"},))


def _feed(w: MainWindow, *msgs: tuple[str, Any]) -> None:
    for m in msgs:
        w.q.put(m)
    w.drain()
    QApplication.processEvents()


def shoot(out: Path) -> list[Path]:
    """One PNG per screen; the files are named after the manual's picture names."""
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    apply_palette(app)
    w = MainWindow(port=1, ask=lambda t, x: True, tell=lambda t, x: None,
                   show_result=lambda v: None, opener=lambda p: None)  # fmt: skip
    w.resize(*SIZE)
    w.show()
    saved: list[Path] = []

    def save(name: str, widget: Any = w) -> None:
        QApplication.processEvents()
        path = out / f"{name}.png"
        widget.grab().save(str(path))
        saved.append(path)

    for state in ("IDLE", "RUNNING", "FAULT"):
        _feed(w, ("status", _status(state)))
        save(f"main-{state.lower()}")
    _feed(w, ("status", _status("IDLE")))
    w.b_storage.click()
    while not w.commands._jobs.empty():  # the screen's own list read: answered here
        w.commands._jobs.get_nowait()
    w.commands._busy.clear()
    _feed(w, ("done", Done("storage_list", True, STORAGE, None)))
    assert w.storage_window is not None
    save("storage", w.storage_window)
    w.storage_window.close()
    w.timer.stop()
    w.close()
    return saved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="manual_shots")
    ap.add_argument("out", type=Path)
    for p in shoot(ap.parse_args(argv).out):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
