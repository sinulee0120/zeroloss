"""ⓘ help on every input (CLAUDE.md, 03 F14, 11-ui 1.10, 20 GAP-84), offscreen: every
input on the main screen, with the device rows and Advanced open, has its help as a
tooltip while idle and while locked in a test; a grey one says why on the first line."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QWidget,
)

from controller.ui.main_window import MainWindow
from controller.ui.store import Status
from controller.ui.texts import TEXT
from core.ui_qt.palette import apply_palette
from tests.controller_ui.test_main_window import SCHEMA, feed, setup_answer

DEVICES = ({"name": "rx", "host": "10.0.0.3"}, {"name": "tx", "host": "10.0.0.2"})


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
    w.close()


def inputs(w: MainWindow) -> list[QWidget]:
    found: list[QWidget] = []
    for cls in (QComboBox, QCheckBox, QAbstractSpinBox, QLineEdit):
        for x in w.findChildren(cls):
            inside_spin = isinstance(x, QLineEdit) and isinstance(  # its box has the help
                x.parent(), QAbstractSpinBox | QComboBox
            )
            if x.isVisibleTo(w) and not inside_spin:
                found.append(x)
    return found


def check(w: MainWindow) -> int:
    n = 0
    for x in inputs(w):
        help_text = str(x.property("help") or "")
        name = f"{type(x).__name__} {x.property('text') or getattr(x, 'currentText', lambda: '')()}"
        assert help_text, f"no ⓘ help: {name}"
        tip = x.toolTip()
        assert tip.endswith(help_text), (name, tip)
        if not x.isEnabled():  # grey: why first, then the help
            reason = tip.removesuffix(help_text).strip()
            assert reason, f"grey without a reason: {name}"
        n += 1
    return n


def test_every_input_has_help_idle_and_locked(win: MainWindow) -> None:
    w = win
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, DEVICES)),
         ("setup", setup_answer()), ("schema", SCHEMA))  # fmt: skip
    w.x_advanced.setChecked(True)
    QApplication.processEvents()
    idle = check(w)
    assert idle >= 12  # 5 main + 2 device rows (role, TV channel, network) + Advanced
    running: dict[str, Any] = {"state": "RUNNING", "devices": {}, "cells": {}, "run_id": "R1"}
    feed(w, ("status", Status(True, "b", running, DEVICES)))
    assert not w.c_type.isEnabled() and w.c_type.toolTip().startswith("Locked during test\n")
    check(w)


def test_help_texts_are_current() -> None:
    tips = {k: v for k, v in TEXT.items() if k.startswith("tip_")}
    assert tips and not [k for k, v in tips.items() if "plan file sets it" in v]  # F14
    assert all(len(v) > 20 for v in tips.values())
