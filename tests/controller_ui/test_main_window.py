"""Main screen S1 with real widgets, offscreen (docs/11-ui.md 3, 1.6, 1.8, 1.10, 1.11, 7).

Statuses go in through the same queue the workers use, so the window is tested the way
it really runs; one test also runs against a live Controller service.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QComboBox,
    QLabel,
    QPushButton,
    QSpinBox,
    QWidget,
)

from controller.precheck import STEPS
from controller.service import ControllerService
from controller.ui.main_window import MainWindow
from controller.ui.store import Done, Status
from controller.ui.texts import TEXT
from controller.ui.view import parse_address
from core.ui_qt.palette import apply_palette
from core.ui_schema.design import BUTTON_MIN_HEIGHT, FONT_PT, MIN_FONT_PT

REPO = Path(__file__).resolve().parents[2]
STATES = ["IDLE", "PREPARING", "RUNNING", "STOPPING", "REPORTING", "SAFE_STOP", "FAULT"]
ROW = {
    "color": "green",
    "why": "",
    "link_label": "LAN",
    "items": [
        {"label": "CH1", "value": None, "unit": "", "key": False},
        {"label": "Jitter", "value": 2.1, "unit": "ms", "key": True},
        {"label": "Loss", "value": 0, "unit": "", "key": True},
    ],
}
PLAN = {
    "schema_version": 1,
    "module": "iptv",
    "test_type": "pc",
    "quality": "HD",
    "duration_s": 600,
}
SCHEMA: dict[str, dict[str, Any]] = {  # a few rows of each kind (16 1, defaults.json ui)
    "S-01": {"value": "IPTV test", "label": "Test type", "where": "main", "kind": "choice",
             "choices": ["IPTV test"], "help": "Which test.",
             "lock": True, "available": True,
             "unavailable_choices": {"Tablet test": "comes with the tablet app"}},
    "S-11": {"value": {"UHD": 35, "HD": 10}, "label": "Bitrate", "where": "adv",
             "kind": "per_quality_int", "ranges": {"UHD": [30, 40], "HD": [8, 20]},
             "unit": "Mbps", "help": "Video bitrate.", "lock": True, "available": True},
    "S-14": {"value": {"groups": ["239.1.1.1"], "port": 5004}, "label": "Multicast groups · port",
             "where": "adv", "kind": "groups", "help": "Addresses.", "lock": True,
             "available": "Set in the plan file in this build"},
    "S-15": {"value": 16, "label": "TTL", "where": "adv", "kind": "int", "min": 8, "max": 64,
             "help": "Hops.", "lock": True, "available": True},
    "S-16": {"value": 0, "label": "DSCP", "where": "adv", "kind": "int", "min": 0, "max": 63,
             "help": "Priority.", "lock": True, "available": "comes in a later build"},
    "S-52": {"value": "Off", "label": "Problem sound", "where": "adv", "kind": "choice",
             "choices": ["Off", "On"], "help": "A sound.", "lock": False, "available": True},
}  # fmt: skip


def setup_answer(ok: bool = True, **choice: Any) -> dict[str, Any]:
    """02 2-11 answer: the saved choice, the sides found, the roles, the plan."""
    rx = [{"device": "rx", "ch": 1, "network": "Wired"}]
    ch = {"module": "iptv", "test_type": "IPTV test", "quality": "HD", "duration_s": 600,
          "send_quality_reports": True, "sender": None, "receivers": rx,
          "settings": {}} | choice  # fmt: skip
    roles = {"sender": "tx" if ok else None, "receivers": ch["receivers"],
             "why": None if ok else "No PC on the router WAN side: choose the sender",
             "lan_sender": False}  # fmt: skip
    sides = {"tx": {"reachable": True, "side": "wan" if ok else "lan"},
             "rx": {"reachable": True, "side": "lan"}}  # fmt: skip
    return {"setup": ch, "sides": sides, "roles": roles, "plan": dict(PLAN) if ok else None}


@pytest.fixture(scope="module")
def app() -> QApplication:
    a = QApplication.instance() or QApplication([])
    assert isinstance(a, QApplication)
    apply_palette(a)
    return a


class Fakes:
    def __init__(self) -> None:
        self.told: list[str] = []
        self.answer = True
        self.results: list[Any] = []
        self.opened: list[str] = []

    def result(self, v: Any) -> None:
        self.results.append(v)

    def open(self, path: str) -> None:
        self.opened.append(path)

    def ask(self, title: str, text: str) -> bool:
        self.told.append(title)
        return self.answer

    def tell(self, title: str, text: str) -> None:
        self.told.append(text)


@pytest.fixture
def win(app: QApplication) -> Iterator[tuple[MainWindow, Fakes]]:
    f = Fakes()
    w = MainWindow(  # workers not started: nothing to reach
        port=1, ask=f.ask, tell=f.tell, show_result=f.result, opener=f.open
    )
    w.show()
    yield w, f
    w.timer.stop()
    w.close()


def running(state: str = "RUNNING", **dev: Any) -> Status:
    d = {"suspect": False, "age_s": 0.2, "module_data": {}, "links": [], "wifi_changes": []}
    d |= {"row": ROW} | dev
    run = {
        "state": state,
        "run_id": "R1",
        "elapsed_s": 12.0,
        "planned_s": 600,
        "cell_s": 300,
        "devices": {"rx": d},
        "cells": {"rx": [{"index": 0, "partial": False, "color": "red", "reasons": ["lost"]}]},
        "result": None,
    }
    return Status(True, "b", run, ({"name": "rx", "host": "10.0.0.3"},))


def feed(w: MainWindow, *msgs: tuple[str, Any]) -> None:
    for m in msgs:
        w.q.put(m)
    w.drain()
    QApplication.processEvents()


def check_controls(w: MainWindow) -> None:
    """Every widget shows exactly what the 1.10 table function says."""
    ctl = w.store.screen_controls()
    pairs: dict[str, QWidget] = {
        "start_stop": w.b_start,
        "test_type": w.c_type,
        "quality": w.c_quality,
        "duration": w.c_duration,
        "send_reports": w.x_reports,
        "advanced_toggle": w.x_advanced,
        "follow_live": w.b_follow,
        "reports": w.b_reports,
        "storage": w.b_storage,
        "last_report": w.b_last,
        "reset_all": w.b_reset,
    }
    for name, widget in pairs.items():
        c = ctl[name]
        assert widget.isEnabled() == (c.enabled or bool(c.popup)), name
        assert widget.isHidden() != c.visible, name
        if not c.enabled:  # a reason, always, on the first line; the ⓘ help after it
            assert widget.toolTip().split("\n")[0] == c.tooltip and c.tooltip, name
        if c.label:
            assert isinstance(widget, QPushButton) and widget.text() == c.label


def test_idle_before_the_choice_is_read_explains_start(win: tuple[MainWindow, Fakes]) -> None:
    w, f = win
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, ())))
    check_controls(w)
    assert w.b_start.text() == "Start" and w.b_start.property("waiting") == "true"
    w.b_start.click()
    assert f.told == [TEXT["setup_loading"]]  # grey but clickable: says why (1.10)
    assert w.no_devices.isVisible()
    assert w.status.full() == TEXT["setup_loading"]


def test_screen_choice_goes_through_the_store(win: tuple[MainWindow, Fakes]) -> None:
    """F14: the saved choice is shown; a change shows at once (store) and is sent to
    the service in click order; during a test it is refused when handled (1.12)."""
    w, _ = win
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, ())), ("setup", setup_answer()))
    assert w.c_quality.currentData() == "HD" and w.c_duration.currentData() == 600
    assert w.c_type.currentData() == "IPTV test"
    w.c_quality.setCurrentIndex(w.c_quality.findData("UHD"))
    w.c_quality.activated.emit(w.c_quality.currentIndex())
    assert w.store.choice["quality"] == "UHD" and "setup:1" in w.commands._busy
    assert w.store.plan is not None  # kept while saved: Start does not flicker (2026-10-31)
    check_controls(w)
    feed(w, ("setup", setup_answer(quality="UHD")))
    feed(w, ("status", running()), ("edit", {"duration_s": 3600}))
    assert w.store.choice["duration_s"] == 600 and w.c_duration.currentData() == 600


def test_unclear_roles_grey_start_with_the_reason(win: tuple[MainWindow, Fakes]) -> None:
    """06 10.4: no PC on the WAN side -> Start says to choose the sender; picking one
    in the device rows is a change like any other."""
    w, _ = win
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, ())), ("setup", setup_answer(False)))
    why = "No PC on the router WAN side: choose the sender"
    assert w.b_start.toolTip() == why and w.status.full() == why
    rows = w._setup_rows
    assert set(rows) == {"tx", "rx"} and rows["rx"].role.currentData() == "receiver"
    assert rows["tx"].role.currentData() == "unused" and not rows["tx"].ch.isVisibleTo(w)
    rows["tx"].role.setCurrentIndex(rows["tx"].role.findData("sender"))
    rows["tx"].role.activated.emit(0)
    assert w.store.choice["sender"] == "tx"
    rows["rx"].net.setCurrentIndex(rows["rx"].net.findData("Wireless"))
    rows["rx"].net.activated.emit(0)
    assert w.store.choice["receivers"][0]["network"] == "Wireless"
    # 2026-10-16: the player buffer is the receiver's own
    assert rows["rx"].buffer.isVisibleTo(w) and not rows["tx"].buffer.isVisibleTo(w)
    rows["rx"].buffer.setCurrentIndex(rows["rx"].buffer.findData(2000))
    rows["rx"].buffer.activated.emit(0)
    assert w.store.choice["receivers"][0]["buffer_ms"] == 2000


def test_advanced_settings_from_the_settings_data(win: tuple[MainWindow, Fakes]) -> None:
    """F14 / 16 1: Advanced shows the settings data; grey + reason for what this build
    has not got; a change keeps only differences and the count is shown."""
    w, _ = win
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, ())), ("schema", SCHEMA),
         ("setup", setup_answer()))  # fmt: skip
    assert list(w._adv) == ["S-11", "S-14", "S-15", "S-16", "S-52"]
    w.x_advanced.setChecked(True)
    assert w.adv_scroll.isVisibleTo(w)
    ttl = w._adv["S-15"][1]
    assert isinstance(ttl, QSpinBox) and ttl.value() == 16 and ttl.isEnabled()
    assert (ttl.minimum(), ttl.maximum()) == (8, 64)
    dscp, groups = w._adv["S-16"][1], w._adv["S-14"][1]
    assert not dscp.isEnabled() and "comes in a later build" in dscp.toolTip()
    assert not groups.isEnabled() and "plan file" in groups.toolTip()
    rate = w._adv["S-11"][1]
    assert isinstance(rate, QSpinBox) and (rate.minimum(), rate.maximum(), rate.value()) == (
        8,
        20,
        10,
    )
    ttl.setValue(32)
    ttl.editingFinished.emit()
    assert w.store.choice["settings"] == {"S-15": 32}
    assert w.x_advanced.text() == TEXT["advanced_changed"].format(n=1)
    # 2026-10-24: one IPTV test (the rows pick a PC and/or tablets), no Tablet test item
    assert w.c_type.findData("Tablet test") < 0 and w.c_type.findData("PC receiver test") < 0
    feed(w, ("status", running()))
    assert not ttl.isEnabled()  # locked during a test (1.10)


def test_running_row_cells_status(win: tuple[MainWindow, Fakes]) -> None:
    w, _ = win
    feed(w, ("status", running()))
    row = w._rows["rx"]
    assert row.texts() == "✔ rx CH1 Jitter 2.1 ms   Loss 0"
    assert row.key.font().pointSize() >= FONT_PT["key"] and row.key.font().bold()
    assert w.grid.rows["rx"][0].icon == "✖"
    assert w.progress.text() == "0m 12s / 10m 00s (2%)"
    assert w.b_start.text() == "Stop"
    check_controls(w)
    feed(w, ("status", running(age_s=4.0)))
    assert row.texts().startswith("○ rx") and "Last update 4 s ago" in row.texts()


def channel_status(auto: bool = False, ch: int = 1) -> Status:
    st = running(row={**ROW, "channel": ch})
    chans = {"on_air": [1, 2, 3], "next_in_s": {},
             "auto": {"every_s": 600.0, "stagger_s": 10.0} if auto else None}  # fmt: skip
    return Status(True, "b", {**st.run, "channels": chans}, st.devices)


def test_channel_box_changes_the_tv_channel_once(win: tuple[MainWindow, Fakes]) -> None:
    """03 F19 / 11-ui 1.10: the row's box lists the channels on air, shows the one now,
    sends one change per pick (double picks wait), and locks while S-21 is on (K-11)."""
    w, _ = win
    feed(w, ("status", running()))
    row = w._rows["rx"]
    assert row.chan.isHidden()  # no channel data (another module, or not measuring)
    feed(w, ("status", channel_status()))
    assert not row.chan.isHidden() and row.chan.isEnabled()
    assert [row.chan.itemText(i) for i in range(row.chan.count())] == ["CH1", "CH2", "CH3"]
    assert row.texts() == "✔ rx Jitter 2.1 ms   Loss 0 CH1"  # the box, not the text twice
    row.chan.setCurrentIndex(1)
    row.chan.activated.emit(1)
    assert w.commands.busy("channel:rx") and w.commands._jobs.qsize() == 1
    feed(w, ("sent", "channel:rx"))
    assert not row.chan.isEnabled()  # one change at a time
    row.chan.activated.emit(2)
    assert w.commands._jobs.qsize() == 1
    feed(w, ("done", Done("channel:rx", True, {})), ("status", channel_status(ch=2)))
    assert row.chan.currentText() == "CH2" and row.chan.isEnabled()
    row.chan.activated.emit(1)  # the channel it is on: nothing to send
    assert w.commands._jobs.qsize() == 1
    row.chan.setCurrentIndex(2)
    row.chan.activated.emit(2)
    err = {"code": "ZL-RUN-014", "what": "CH3 is not on air in this test"}
    feed(w, ("done", Done("channel:rx", False, {}, err)), ("status", channel_status(ch=2)))
    assert row.chan.currentText() == "CH2"  # back to the channel it really gets
    assert "not on air" in w.failed_text
    feed(w, ("status", channel_status(auto=True, ch=2)))
    assert not row.chan.isEnabled() and "S-21" in row.chan.toolTip()


def test_broadcast_banner_and_stop_broadcast(win: tuple[MainWindow, Fakes]) -> None:
    """03 F18 / 11-ui 1.10: banner and button only while on; the button works between
    tests, asks first, sends once; during a test Stop says it stops measurement only."""
    w, f = win
    bc = {"on": True, "since_utc": "2026-10-02T01:00:00Z", "channels": [1, 2, 3], "sender": "tx"}
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, ())))
    assert w.l_broadcast.isHidden() and w.b_broadcast.isHidden()
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, (), broadcast=bc)))
    assert w.l_broadcast.text() == "Broadcast: ON (3 TV channels)" and w.b_broadcast.isEnabled()
    f.answer = False
    w.b_broadcast.click()
    assert not w.commands.busy("broadcast")
    f.answer = True
    for _ in range(3):
        w.b_broadcast.click()
    assert w.commands.busy("broadcast") and w.commands._jobs.qsize() == 1
    st = running()
    feed(w, ("status", Status(True, "b", st.run, st.devices, broadcast=bc)))
    assert not w.b_broadcast.isEnabled() and w.b_start.toolTip().startswith("Stops measurement")
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, (), broadcast={"on": False})))
    assert w.l_broadcast.isHidden() and w.b_broadcast.isHidden()


def test_the_test_panel_shows_the_running_plan(win: tuple[MainWindow, Fakes]) -> None:
    """GAP-105 4 (first real runs): a test started with --plan (HD, 10 min) showed the
    saved choice (UHD, 2 min) in the Test panel. During a test it shows what runs."""
    from controller.ui.main_window import shown_choice

    saved = {"test_type": "IPTV test", "quality": "UHD", "duration_s": 120}
    run = {"plan": {"test_type": "pc", "quality": "HD", "duration_s": 600}}
    assert shown_choice(saved, run, "RUNNING") == {**saved, "quality": "HD", "duration_s": 600}
    assert shown_choice(saved, run, "IDLE") == saved  # between tests: the saved choice
    w, _ = win
    feed(w, ("setup", setup_answer(quality="UHD", duration_s=120)))
    st = running()
    feed(w, ("status", Status(True, "b", {**st.run, "plan": run["plan"]}, st.devices)))
    assert w.c_quality.currentData() == "HD" and w.c_duration.currentData() == 600


def test_stop_asks_first_then_sends_once(win: tuple[MainWindow, Fakes]) -> None:
    w, f = win
    feed(w, ("status", running()))
    f.answer = False
    w.b_start.click()
    assert not w.commands.busy("stop")
    f.answer = True
    for _ in range(5):  # double clicks (U10)
        w.b_start.click()
    assert w.commands.busy("stop") and w.commands._jobs.qsize() == 1
    assert w.b_start.text() == "Stopping…"  # reacts at once (1.14)


def test_disconnected_locks_everything(win: tuple[MainWindow, Fakes]) -> None:
    w, _ = win
    feed(w, ("status", running()), ("status", Status(False, error="down")))
    check_controls(w)
    assert not w.b_start.isEnabled()
    assert w.status.full() == TEXT["reconnecting"]


def test_failed_command_shows_what(win: tuple[MainWindow, Fakes]) -> None:
    w, _ = win
    err = {"code": "ZL-RUN-014", "what": "quality must be UHD or HD"}
    feed(
        w,
        ("status", Status(True, "b", {"state": "IDLE"}, ())),
        ("done", Done("run", False, {}, err)),
    )
    assert w.status.full() == "quality must be UHD or HD"


def test_narrow_layout_below_1280x720(win: tuple[MainWindow, Fakes]) -> None:
    w, _ = win
    w.resize(1366, 768)
    QApplication.processEvents()
    assert w.body.direction() == QBoxLayout.Direction.LeftToRight
    w.resize(1093, 614)  # 1366x768 at 125% (1.8)
    QApplication.processEvents()
    assert w.body.direction() == QBoxLayout.Direction.TopToBottom
    assert w.b_start.isVisible()


def test_fonts_and_buttons_fit(win: tuple[MainWindow, Fakes]) -> None:
    """1.11: no text under 9 pt, buttons as big as their text needs."""
    w, _ = win
    feed(w, ("status", running()))
    for label in w.findChildren(QLabel):
        assert label.font().pointSizeF() >= MIN_FONT_PT, label.text()
    for b in w.findChildren(QPushButton):
        if b.isVisible():
            assert b.height() >= BUTTON_MIN_HEIGHT, b.text()
            assert b.width() >= b.fontMetrics().horizontalAdvance(b.text()), b.text()


def test_redraw_only_on_change(win: tuple[MainWindow, Fakes]) -> None:
    w, _ = win
    count = 0
    real = w.redraw

    def counted() -> None:
        nonlocal count
        count += 1
        real()

    w.redraw = counted  # type: ignore[method-assign]
    st = running()
    feed(w, ("status", st))
    for _ in range(20):
        feed(w, ("status", st))  # same status every second: nothing redraws (1.13)
    assert count == 1


def test_monkey_messages_keep_the_screen_equal_to_the_store(
    win: tuple[MainWindow, Fakes],
) -> None:
    """Random statuses, settings, plans, clicks: after every step the widgets equal the
    1.10 function and nothing raises (playbook: random + repeated + idle)."""
    w, _ = win
    for seed in range(3):
        rnd = random.Random(seed)
        for _ in range(300):
            pick = rnd.random()
            if pick < 0.45:
                dev = {
                    "age_s": rnd.choice([0.1, 3.5]),
                    "row": ROW | {"color": rnd.choice(["green", "red", "yellow", "purple"])},
                }
                st = running(rnd.choice(STATES), **dev) if rnd.random() < 0.9 else Status(False)
                feed(w, ("status", st))
            elif pick < 0.6:  # screen changes, also while a test runs (refused then)
                key = rnd.choice(["quality", "duration_s", "sender"])
                value = {"quality": rnd.choice(["HD", "UHD"]),
                         "duration_s": rnd.choice([600, 3600]),
                         "sender": rnd.choice(["tx", "rx", None])}[key]  # fmt: skip
                w.edit(**{key: value})
                feed(w)
            elif pick < 0.7:
                feed(w, ("setup", setup_answer(rnd.random() < 0.8)), ("schema", SCHEMA))
            elif pick < 0.75:  # light check results, current or for an old plan
                key = w.store.check_key if rnd.random() < 0.7 else "old"
                feed(w, ("idlecheck", (key, idle_check(rnd.choice([None, 2, 4])))))
            elif pick < 0.85:
                rnd.choice([w.b_start, w.b_last, w.b_reports]).click()
                feed(w)
            else:
                w.resize(rnd.choice([(1024, 600), (1280, 720), (1920, 1080)])[0], 700)
                feed(w)
            check_controls(w)


def test_against_a_live_service(app: QApplication, tmp_path: Path) -> None:
    svc = ControllerService(
        tmp_path / "ctl", tmp_path / "rep", REPO / "modules", host="127.0.0.1", port=0, local_port=0
    )
    svc.start()
    w = MainWindow(svc.node.local.port, feed_interval_s=0.2)
    try:
        w.start()
        end = time.monotonic() + 5
        while not w.store.status.connected and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.02)
        assert w.store.status.connected
        QApplication.processEvents()
        check_controls(w)
        end = time.monotonic() + 10  # the saved choice and its roles come by a worker
        while w.store.setup is None and time.monotonic() < end:
            w.drain()
            time.sleep(0.05)
        # no devices yet: the status says what to do (06 10.4)
        assert w.status.full() == "Choose the sender: set its row to Sender"
    finally:
        w.shutdown()
        svc.stop()
    # the service is gone: the screen locks as Reconnecting
    w2 = MainWindow(svc.node.local.port, feed_interval_s=0.2)
    try:
        w2.start()
        end = time.monotonic() + 5
        while w2.store.status.error == "Connecting…" and time.monotonic() < end:
            w2.drain()
            time.sleep(0.02)
        assert not w2.store.status.connected
        assert w2.status.full() == TEXT["reconnecting"]
    finally:
        w2.shutdown()


def test_result_popup_once_and_last_report(win: tuple[MainWindow, Fakes]) -> None:
    """03 F15: the popup comes once when a watched run finishes; Last report opens it."""
    w, f = win
    idle = Status(True, "b", {"state": "IDLE", "result": None}, (), reports_dir="/rep")
    feed(w, ("status", idle), ("status", running()))
    done = {
        "run_id": "R9",
        "verdict": "PASS",
        "verdict_reason": "planned_time_reached",
        "planned_s": 600,
        "actual_s": 600,
        "devices": [],
    }
    end = Status(True, "b", {"state": "IDLE", "result": done}, (), reports_dir="/rep")
    feed(w, ("status", end))
    feed(w, ("status", end))
    assert [v.title for v in f.results] == ["Test finished — PASS (planned time reached)"]
    w.b_last.click()
    assert f.opened == [str(Path("/rep/R9/report.html"))]  # this OS's separators


def test_result_dialog_buttons(app: QApplication) -> None:
    from controller.ui.main_window import ResultDialog
    from controller.ui.view import result_view

    v = result_view(
        {
            "run_id": "R3",
            "verdict": "FAIL",
            "verdict_reason": "stopped_by_user",
            "planned_s": 60,
            "actual_s": 30,
            "devices": [],
        },
        "/rep",
    )
    opened: list[str] = []
    parent = QWidget()
    d = ResultDialog(v, opened.append, parent)
    d.b_show.click()
    assert opened == [str(Path("/rep/R3/report.html"))] and d.result() == 1
    d = ResultDialog(v, opened.append, parent)
    d.b_folder.click()
    assert opened[-1] == str(Path("/rep/R3"))
    d = ResultDialog(v, opened.append, parent)
    d.b_copy.click()
    assert QApplication.clipboard().text() == str(Path("/rep/R3"))
    d.b_later.click()
    assert d.result() == 0 and len(opened) == 2
    # buttons: bottom right, the main one at the right end (11-ui 1.11)
    d.show()
    QApplication.processEvents()
    assert d.b_later.x() < d.b_folder.x() < d.b_show.x()


def test_problems_badge_and_window(win: tuple[MainWindow, Fakes]) -> None:
    """03 F21: the badge shows open problems; the window lists them without blocking."""
    from tests.controller_ui.test_view import problems_status

    w, _ = win
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, ())))
    assert w.b_problems.isHidden()
    feed(w, ("status", problems_status()))
    assert w.b_problems.isVisible() and w.b_problems.text() == "2 problems"
    w.b_problems.click()
    pw = w.problems_window
    assert pw is not None and pw.isVisible() and not pw.isModal()
    assert pw.model.rowCount() == 3
    first = pw.model.data(pw.model.index(0, 3), 0)
    assert first == TEXT["problems_repeat"].format(what="link 100 Mbps", n=2)
    # 2026-10-09 user: "columns cannot be resized, so I cannot read it all"
    from PySide6.QtWidgets import QHeaderView

    head = pw.table.horizontalHeader()
    assert all(head.sectionResizeMode(c) == QHeaderView.ResizeMode.Interactive
               for c in range(head.count() - 1)) and head.stretchLastSection()  # fmt: skip
    assert pw.table.wordWrap() and pw.detail.isHidden()
    # 2026-10-10 user: "What to do" was a sliver: the text columns share the width
    texts = [head.sectionSize(c) for c in range(3, head.count())]
    assert min(texts) >= 20 * 8 and max(texts) - min(texts) <= 2
    head.resizeSection(3, 400)  # the person drags What wider: kept at the next resize
    pw.resize(pw.width() + 80, pw.height())
    assert head.sectionSize(3) == 400
    pw.table.selectRow(0)
    assert not pw.detail.isHidden() and "What to do:" in pw.detail.text()
    assert "link 100 Mbps" in pw.detail.text()
    feed(w, ("status", problems_status(flood=12)))
    assert pw.flood.isVisible() and pw.model.rowCount() == 2  # warning folded
    check_controls(w)  # the main window still works (not modal)


def test_new_error_blinks_then_stops(win: tuple[MainWindow, Fakes]) -> None:
    from tests.controller_ui.test_view import problems_status

    w, _ = win
    feed(w, ("status", problems_status()))
    assert w.blink.isActive()  # a new error
    feed(w, ("status", Status(True, "b", {"state": "RUNNING"}, (), problems={})))
    assert not w.blink.isActive()  # the error is gone: the timer goes with it


def test_problem_sound_only_when_s52_is_on(win: tuple[MainWindow, Fakes], monkeypatch: Any) -> None:
    """16 S-52 (default Off): a new error beeps only when switched on."""
    from tests.controller_ui.test_view import problems_status

    w, _ = win
    beeps: list[int] = []
    monkeypatch.setattr(QApplication, "beep", lambda: beeps.append(1))
    feed(w, ("status", problems_status()))
    assert beeps == []
    feed(w, ("status", Status(True, "b", {"state": "RUNNING"}, (), problems={})))
    feed(w, ("setup", setup_answer(settings={"S-52": "On"})))
    feed(w, ("status", problems_status()))
    assert beeps == [1]


def actions_status() -> Status:
    rows = [
        {"key": "human.pc.power_cycle|rx", "action_id": "human.pc.power_cycle", "device": "rx",
         "what": "Restart the PC", "how": "Restart that PC.", "why": "tsp did not stop",
         "opened_by": "ZL-SYS-006", "state": "waiting", "done_by": None, "auto_done": False,
         "repeat": "every", "requested_utc": "2026-09-25T03:00:00Z",
         "done_utc": None, "run_id": ""},
        {"key": "human.cable.check|tx", "action_id": "human.cable.check", "device": "tx",
         "what": "Check the cable and port", "how": "Reseat it.", "why": "link short",
         "opened_by": "ZL-NET-013", "state": "waiting", "done_by": None, "auto_done": True,
         "repeat": "every", "requested_utc": "2026-09-25T03:00:01Z",
         "done_utc": None, "run_id": ""},
    ]  # fmt: skip
    return Status(True, "b", {"state": "RUNNING"}, (), actions={"waiting": 2, "actions": rows})


def test_action_required_window(win: tuple[MainWindow, Fakes]) -> None:
    """03 F22: a new action brings its own window to the front (on top, not modal);
    [Done] works on every waiting row, the first one when none is picked (GAP-116: a row
    ZeroLoss could not close was stuck), and it sends the command."""
    from PySide6.QtCore import Qt

    w, _ = win
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, ())))
    assert w.actions_window is None and w.b_actions.isHidden()
    feed(w, ("status", actions_status()))
    aw = w.actions_window
    assert aw is not None and aw.isVisible() and not aw.isModal()
    # above ZeroLoss (its parent), not above every program (2026-10-17 user)
    assert not aw.windowFlags() & Qt.WindowType.WindowStaysOnTopHint and aw.parent() is w
    assert w.b_actions.text() == "🖐 2 actions needed"
    assert aw.model.rowCount() == 2 and aw.b_done.isEnabled()  # the first waiting row
    aw.table.selectRow(1)  # the cable: ZeroLoss sees it too, a person may still close it
    assert aw.b_done.isEnabled()
    aw.table.selectRow(0)  # restart the PC: needs a person
    assert aw.b_done.isEnabled()
    aw.b_done.click()
    assert w.commands.busy("action_done:human.pc.power_cycle|rx")
    check_controls(w)  # the main window is still usable


def test_actions_all_done_shows_close_not_a_grey_done(win: tuple[MainWindow, Fakes]) -> None:
    """2026-10-09 user: "if all are done, why a Done button at the bottom?" With nothing
    waiting the corner says so and offers Close; Done comes back with a waiting row."""
    w, _ = win
    feed(w, ("status", actions_status()))
    aw = w.actions_window
    assert aw is not None
    assert not aw.b_done.isHidden() and aw.b_close.isHidden() and aw.all_done.isHidden()
    st = actions_status()
    rows = [{**r, "state": "done", "done_by": "auto", "done_utc": "2026-09-25T03:00:05Z"}
            for r in st.actions["actions"]]  # fmt: skip
    feed(w, ("status", replace(st, actions={"waiting": 0, "actions": rows})))
    assert aw.b_done.isHidden() and not aw.b_close.isHidden() and aw.b_close.isEnabled()
    assert not aw.all_done.isHidden() and "All done" in aw.all_done.text()
    aw.b_close.click()
    assert not aw.isVisible()


def test_action_with_choices_shows_one_button_each(win: tuple[MainWindow, Fakes]) -> None:
    """20 W-1: selecting "choose the WAN card" shows a button per card; pressing one
    sends `actions/choose`; other rows show no choice buttons."""
    from PySide6.QtWidgets import QPushButton

    w, _ = win
    st = actions_status()
    choose = {"key": "human.pc.choose_wan_nic|tx", "action_id": "human.pc.choose_wan_nic",
              "device": "tx", "what": "Choose the network card that goes to the router WAN",
              "how": "Press the button of the card.", "why": "2 cards", "opened_by": "ZL-DSC-006",
              "state": "waiting", "done_by": None, "auto_done": True, "repeat": "once",
              "requested_utc": "2026-09-25T03:00:02Z", "done_utc": None, "run_id": "",
              "choose": "wan_nic", "choices": [
                  {"id": "enp1s0", "label": "enp1s0 · 1000 Mbps"},
                  {"id": "enp2s0", "label": "enp2s0 · 100 Mbps"}]}  # fmt: skip
    rows = [*st.actions["actions"], choose]
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, (),
                              actions={"waiting": 3, "actions": rows})))  # fmt: skip
    aw = w.actions_window
    assert aw is not None and aw.choice_box.isHidden()
    aw.table.selectRow(2)
    buttons = [b for b in aw.choice_box.findChildren(QPushButton)]
    assert not aw.choice_box.isHidden() and [b.text() for b in buttons] == [
        "enp1s0 · 1000 Mbps",
        "enp2s0 · 100 Mbps",
    ]
    buttons[1].click()
    assert w.commands.busy("action_choose:human.pc.choose_wan_nic|tx")
    aw.table.selectRow(0)  # a row without choices
    assert aw.choice_box.isHidden()


def idle_check(
    failed_step: int | None, kind: str = "idle", when: str = "2026-09-25T01:00:00Z"
) -> dict[str, Any]:
    steps = []
    for n, name in enumerate(STEPS, 1):
        state = "ok" if n <= 4 else "skipped"
        if failed_step is not None and n >= failed_step and n <= 4:
            state = "failed" if n == failed_step else "skipped"
        bad = state == "failed"
        steps.append({
            "n": n, "name": name, "state": state, "detail": "fine", "device": "rx" if bad else None,
            "code": "ZL-INS-029" if bad else None,
            "what": "tsp is not installed on rx" if bad else None,
            "why": "The test needs this program on that PC." if bad else None,
            "fix": "Install it." if bad else None,
        })  # fmt: skip
    ok = failed_step is None
    return {"kind": kind, "run_id": None, "updated_utc": when, "ok": ok, "steps": steps}


def test_failed_light_check_greys_start_with_the_reason(win: tuple[MainWindow, Fakes]) -> None:
    """03 F17 / 11-ui 1.10 'IDLE (pre-check failed)': Start grey with the step and what,
    the Status box lists the steps with Why / Fix; a result for an old plan is ignored."""
    w, f = win
    idle = Status(True, "b", {"state": "IDLE"}, ({"name": "rx", "host": "10.0.0.3"},))
    feed(w, ("status", idle), ("setup", setup_answer()))
    assert w.b_start.property("waiting") == "false" and not w.checklist.isVisibleTo(w)
    feed(w, ("idlecheck", ("old plan", idle_check(2))))
    assert w.b_start.property("waiting") == "false"  # not about what is shown now
    feed(w, ("idlecheck", (w.store.check_key, idle_check(2))))
    check_controls(w)
    reason = "Tools ready: tsp is not installed on rx"
    assert w.b_start.toolTip() == reason and w.b_start.property("waiting") == "true"
    w.b_start.click()
    assert f.told[-1] == reason
    assert w.status.full() == TEXT["status_not_ready"].format(why=reason)
    text = w.checklist.text().splitlines()
    assert text[0] == "✔ Devices connected — fine"
    assert text[1] == "✖ Tools ready — rx: tsp is not installed on rx"
    assert text[2:4] == ["    Why: The test needs this program on that PC.", "    Fix: Install it."]
    assert len(text) == 6 and w.checklist.isVisibleTo(w)  # steps 5-7 never run here
    w.edit(duration_s=3600)  # a new choice: the old result no longer counts
    new = setup_answer(duration_s=3600)
    new["plan"] = dict(PLAN) | {"duration_s": 3600}
    feed(w, ("setup", new))  # the service saved it and answered with its plan
    assert w.b_start.property("waiting") == "false" and not w.checklist.isVisibleTo(w)
    feed(w, ("idlecheck", (w.store.check_key, idle_check(None))))
    assert w.b_start.property("waiting") == "false" and w.status.full() == TEXT["status_ready"]


def test_start_checklist_while_starting(win: tuple[MainWindow, Fakes]) -> None:
    w, _ = win
    c = idle_check(None, kind="start")
    c["steps"][4]["state"], c["steps"][5]["state"] = "ok", "running"
    feed(w, ("setup", setup_answer()),
         ("status", Status(True, "b", {"state": "PREPARING", "checklist": c}, ())))  # fmt: skip
    lines = w.checklist.text().splitlines()
    assert [x[0] for x in lines] == ["✔"] * 5 + ["⏳", "⏸"] and len(lines) == 7
    feed(w, ("status", running()))
    assert not w.checklist.isVisibleTo(w)  # during the test: the one-line status only


def fault_status(reset: dict[str, Any] | None = None) -> Status:
    d = {"suspect": False, "age_s": 12.0, "module_data": {}, "links": [], "wifi_changes": []}
    run = {
        "state": "FAULT", "run_id": "R1", "devices": {"rx": d, "tx": {**d, "age_s": 0.5}},
        "fault": {"device": "rx", "reason": "rx: no answer for 10 s",
                  "at_utc": "2026-09-25T10:00:10.000Z", "last_ok_utc": "2026-09-25T10:00:00.000Z",
                  "run_id": "R1"},
        "result": {"run_id": "R1", "verdict": "INCOMPLETE"}, "reset": reset,
    }  # fmt: skip
    return Status(True, "b", run, ({"name": "rx", "host": "10.0.0.3"},))


def test_fault_screen_says_what_stopped_and_offers_reset(win: tuple[MainWindow, Fakes]) -> None:
    """S6 (03 F11): what stopped and when, each device, the data; Reset All asks first,
    then all fault actions wait for the answer; Restart is per device."""
    w, f = win
    feed(w, ("status", fault_status()))
    check_controls(w)
    assert w.g_fault.isVisibleTo(w)
    assert (
        w.fault_what.text() == "rx stopped answering (no answer for 10 s). Last answer: 10:00:00."
    )
    assert "Report written for R1 (INCOMPLETE)" in w.fault_data.text()
    rows = {n: lab.text() for n, (_, lab, _) in w._fault_rows.items()}
    assert rows["rx"].endswith("rx: Not answering") and rows["tx"].endswith("tx: Stopped")
    f.answer = False
    w.b_fault_reset.click()  # asked, said no: nothing sent
    assert f.told[-1] == TEXT["reset_title"] and "reset" not in w.store.pending
    f.answer = True
    w.b_fault_reset.click()
    feed(w)
    assert "reset" in w.store.pending and w.b_fault_reset.text() == "Resetting…"
    _, _, restart = w._fault_rows["rx"]
    assert not w.b_fault_shutdown.isEnabled() and not restart.isEnabled()
    feed(w, ("done", Done("reset", True, {"ok": False})))
    reset = {"ok": False, "devices": [
        {"device": "rx", "answered": False, "clean": False, "why": "no answer",
         "fix": "On rx: restart the ZeroLoss service or the PC, then press Reset All again."},
        {"device": "tx", "answered": True, "clean": True, "why": None, "fix": None}]}  # fmt: skip
    feed(w, ("status", fault_status(reset)))
    rows = {n: lab.text() for n, (_, lab, _) in w._fault_rows.items()}
    assert "No answer — On rx: restart" in rows["rx"] and rows["tx"].endswith("Clean")
    _, _, restart = w._fault_rows["rx"]
    restart.click()
    feed(w)
    assert "restart:rx" in w.store.pending
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, ())))
    assert not w.g_fault.isVisibleTo(w)


def test_source_video_is_on_the_test_area_without_advanced(win: tuple[MainWindow, Fakes]) -> None:
    """11-ui 1.10 GAP-115: the movie is picked on the Test area under Video quality — the
    user did not find it inside Advanced. Picking one writes S-48 for that channel."""
    w, _ = win
    movie = "1. [AV]The greatest showman_clip 01_UHD_Dolby Atmos.mp4"
    files = {"files": [{"name": movie, "width": 3840, "height": 2160, "duration_s": 101.1,
                        "fits": {"HD": {"ok": True}, "UHD": {"ok": True}}}]}  # fmt: skip
    sources = {"value": "Generated", "label": "Source video", "where": "main", "kind": "sources",
               "help": "Video to send.", "lock": True, "available": True}  # fmt: skip
    idle = Status(True, "b", {"state": "IDLE", "result": None}, ())
    feed(w, ("schema", SCHEMA | {"S-48": sources}), ("setup", setup_answer()), ("status", idle),
         ("done", Done("sources", True, files)))  # fmt: skip
    assert not w.x_advanced.isChecked()
    assert w.sources.isVisible() and w.l_sources.isVisible()
    assert w.form.indexOf(w.sources) > w.form.indexOf(w.c_quality)  # right under it
    box = w.sources.boxes[1]
    assert box.isEnabled() and box.findData(movie) > 0
    box.setCurrentIndex(box.findData(movie))
    box.activated.emit(box.currentIndex())  # as a click on the list item
    assert w.store.setting("S-48") == {"1": movie}
    assert w.x_advanced.text() == TEXT["advanced"]  # not counted as an Advanced change
    feed(w, ("status", running()))
    assert not w.sources.isEnabled()  # locked like Video quality during a test
    assert "S-48" not in w._adv


def test_each_test_pc_row_lets_the_person_pick_its_card(win: tuple[MainWindow, Fakes]) -> None:
    """DSC-006 / GAP-115 (user: "IP설정 문제가 발생시 사용자가 변경해볼수있게"): every sender and
    wired receiver row has Card: Auto + that PC's own cards; a pick is saved as ``nics``."""
    w, _ = win
    links = {"tx": {"links": [{"nic": "enp37s0", "ip": "10.0.0.1"},
                              {"nic": "wlo1", "ip": "192.168.0.27", "wifi": None}]},
             "rx": {"links": [{"nic": "enp0s31f6", "ip": "10.0.0.100"},
                              {"nic": "wlp4s0", "ip": "192.168.0.13", "wifi": {}}]}}  # fmt: skip
    idle = Status(True, "b", {"state": "IDLE", "result": None, "devices": links}, ())
    feed(w, ("setup", setup_answer()), ("status", idle))
    row = w._setup_rows["rx"]
    assert row.card.isVisible() and row.card.currentText() == TEXT["card_auto"]
    labels = [row.card.itemText(i) for i in range(row.card.count())]
    assert labels == [TEXT["card_auto"], "enp0s31f6 · 10.0.0.100", "wlp4s0 · 192.168.0.13 (Wi-Fi)"]
    row.card.setCurrentIndex(1)
    row.card.activated.emit(1)  # as a click on the list item
    assert w.store.choice["nics"] == {"rx": "10.0.0.100"}
    row.card.setCurrentIndex(0)
    row.card.activated.emit(0)  # back to Auto
    assert w.store.choice["nics"] == {}
    assert w._setup_rows["tx"].card.isVisible()


def test_a_picked_movie_is_made_at_once_and_its_making_is_shown(
    win: tuple[MainWindow, Fakes],
) -> None:
    """GAP-116 (user: "그린랜드 파일 고르고 시작하면 11분을 기다려야해"): picking a movie asks
    the sender PC to make it right away (the newest pick wins); the line under Source
    video says how far it is, so Start waits only for what is left."""
    w, _ = win
    movie = "4K. 그 린 랜 드 2. 2026 (꿀깅).mp4"
    files = {"files": [{"name": movie, "width": 1724, "height": 720, "duration_s": 5878.6,
                        "fits": {"HD": {"ok": True}, "UHD": {"ok": True}}}]}  # fmt: skip
    sources = {"value": "Generated", "label": "Source video", "where": "main", "kind": "sources",
               "help": "Video to send.", "lock": True, "available": True}  # fmt: skip
    bitrate = SCHEMA["S-11"]  # HD 10 Mbps
    idle = Status(True, "b", {"state": "IDLE", "result": None}, ())
    feed(w, ("schema", SCHEMA | {"S-48": sources, "S-11": bitrate}), ("setup", setup_answer()),
         ("status", idle), ("done", Done("sources", True, files)))  # fmt: skip
    while not w.commands._jobs.empty():  # what the screen asked for so far
        w.commands._jobs.get_nowait()
    box = w.sources.boxes[1]
    box.setCurrentIndex(box.findData(movie))
    box.activated.emit(box.currentIndex())
    sent = []
    while not w.commands._jobs.empty():
        sent.append(w.commands._jobs.get_nowait())
    (job,) = [j for j in sent if j is not None and j[0] == "prepare"]
    assert job[2] == {"module": "iptv", "sender": "tx", "quality": "HD", "channels": [1],
                      "sources": {"1": movie}, "only": True, "bitrate_mbps": 10}  # fmt: skip
    made: dict[str, Any] = {"ch": 1, "quality": "HD", "mbps": 10, "state": "making",
                            "percent": 34.2, "left_s": 420, "error": None,
                            "source": movie}  # fmt: skip
    making = {"busy": True, "channels": [made]}
    feed(w, ("done", Done("making", True, making)), ("status", idle))
    assert w.sources.note.isVisible()
    assert w.sources.note.text() == "Making the CH1 video: 34% · about 7 min left"
    made |= {"state": "ready", "percent": None, "left_s": None}
    feed(w, ("done", Done("making", True, making)), ("status", idle))
    assert w.sources.note.text() == "CH1 video ready"


def test_start_refused_by_the_broadcast_offers_to_stop_it(win: tuple[MainWindow, Fakes]) -> None:
    """GAP-116: after a Stop the Always-on broadcast still sent the old movie; Start with
    a new movie was refused (RUN-014) and only turned the line red. Now one question,
    and a yes stops the broadcast and starts the test."""
    w, f = win
    bc = {"on": True, "channels": [1], "since_utc": "2026-10-04T00:59:13Z"}
    feed(w, ("setup", setup_answer()), ("status", Status(True, "b", {"state": "IDLE"}, (),
                                                         broadcast=bc)))  # fmt: skip
    while not w.commands._jobs.empty():
        w.commands._jobs.get_nowait()
    refused = {"code": "ZL-RUN-014", "what": "Stop the broadcast first"}
    feed(w, ("done", Done("run", False, {}, refused)))
    assert TEXT["start_over_broadcast_title"] in f.told
    assert [(j[0], j[2]) for j in sent(w)] == [("broadcast", {"on": False})]
    started: list[str] = []
    real = w.commands.send

    def record(job: str, path: str, /, **kw: Any) -> bool:
        started.append(job)
        return True

    w.commands.send = record  # type: ignore[method-assign,assignment]
    feed(w, ("done", Done("broadcast", True, {"on": False})))
    w.commands.send = real  # type: ignore[method-assign]
    assert started == ["run"]  # a Start goes on its own thread (GAP-123), not the line


def sent(w: MainWindow) -> list[Any]:
    """The commands the screen queued since the last look."""
    out = []
    while not w.commands._jobs.empty():
        job = w.commands._jobs.get_nowait()
        if job is not None:
            out.append(job)
    return out


def test_cards_from_the_setup_answer_and_an_honest_unknown(win: tuple[MainWindow, Fakes]) -> None:
    """GAP-121 (real window): between tests the status has no cards, so a T460 card it
    really has said "(not on this PC now)". The setup answer brings the cards (node
    info), and with none known the label says so instead of claiming it is gone."""
    w, _ = win
    answer = setup_answer(nics={"rx": "192.168.45.177"})
    answer["sides"]["rx"]["links"] = [{"nic": "enp0s31f6", "ip": "192.168.45.177"}]
    feed(w, ("setup", answer), ("status", Status(True, "b", {"state": "IDLE"}, ())))
    box = w._setup_rows["rx"].card
    assert box.currentText() == "enp0s31f6 · 192.168.45.177"
    unknown = setup_answer(nics={"rx": "192.168.45.177"})  # no links: never seen
    feed(w, ("setup", unknown))
    assert w._setup_rows["rx"].card.currentText() == f"192.168.45.177 {TEXT['card_unseen']}"


def test_a_window_opened_during_a_test_shows_the_movie_that_is_sent(
    win: tuple[MainWindow, Fakes],
) -> None:
    """GAP-126 (real window): reopened during a run, Source video said Generated while
    Greenland was sent — between tests only is the saved choice read. During a test the
    running plan's settings are shown."""
    w, _ = win
    movie = "4K. 그 린 랜 드 2. 2026 (꿀깅).mp4"
    sources = {"value": "Generated", "label": "Source video", "where": "main", "kind": "sources",
               "help": "Video to send.", "lock": True, "available": True}  # fmt: skip
    plan = {"test_type": "pc", "quality": "HD", "duration_s": 300,
            "settings": {"S-48": {"1": movie}}}  # fmt: skip
    files = {"files": [{"name": movie, "width": 1724, "height": 720, "duration_s": 5878.6,
                        "fits": {"HD": {"ok": True}}}]}  # fmt: skip
    feed(w, ("schema", SCHEMA | {"S-48": sources}), ("done", Done("sources", True, files)),
         ("status", running()))  # fmt: skip
    st = running()
    st = Status(True, "b", {**st.run, "plan": plan}, st.devices)
    feed(w, ("status", st))
    assert w.store.setting("S-48") == {"1": movie}


def test_stop_says_it_is_waiting_and_a_failed_stop_says_so(win: tuple[MainWindow, Fakes]) -> None:
    """2026-10-09 user: "Stop does not stop — show a wait message or a popup". The status
    line counts the wait while Stop is on its way; a Stop that failed is a popup."""
    w, f = win
    running = Status(True, "b", {"state": "RUNNING", "run_id": "R1", "elapsed_s": 10.0}, ())
    feed(w, ("status", running))
    w._stop_sent_at = time.monotonic() - 7
    feed(w, ("sent", "stop"))
    w.redraw()
    assert w.status.full().startswith("Stopping… 7 s")
    feed(w, ("done", Done("stop", False, {}, {"what": "Request timed out", "why": "no answer"})))
    assert f.told[-1].startswith("Stop did not finish: Request timed out.")


def test_the_duration_can_be_typed(win: tuple[MainWindow, Fakes]) -> None:
    """2026-10-09 user: "you set any time, but I cannot type one in the app"."""
    w, f = win
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, ())), ("setup", setup_answer()))
    assert w.c_duration.isEditable()
    line = w.c_duration.lineEdit()
    assert line is not None
    line.setText("1h 30m")
    line.editingFinished.emit()
    assert w.store.choice.get("duration_s") == 5400
    assert w.c_duration.currentText().startswith("1h 30m")
    line.setText("forever")
    line.editingFinished.emit()
    assert f.told[-1].startswith('"forever" is not a test length')
    assert w.store.choice.get("duration_s") == 5400  # unchanged


def test_every_pc_has_its_ip_box_to_pick_or_type(win: tuple[MainWindow, Fakes]) -> None:
    """GAP-155 (2026-10-15 user: "let the user pick among the IPs; when there is nothing to
    pick, let them type it", "the app doing it by itself is serious"): each row has an IP
    box with the address in use, the ones found and its cards; a pick or a typed address
    is sent as devices/address (the service uses it only when that PC answers there); a
    row whose address another PC answers says so; a refusal is told, never silent."""
    w, f = win
    answer = setup_answer()
    answer["sides"]["tx"] = {"reachable": False, "side": "unknown", "host": "10.0.0.1",
                             "addresses": ["10.0.0.1", "192.168.45.177", "192.168.45.190"],
                             "why": "probook answers at 10.0.0.1, not tx"}  # fmt: skip
    answer["sides"]["rx"] |= {"host": "192.168.45.58", "addresses": ["192.168.45.58"]}
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, ())), ("setup", answer))
    row = w._setup_rows["tx"]
    assert row.address.isVisible() and w._setup_rows["rx"].address.isVisible()
    assert [row.address.itemText(i) for i in range(row.address.count())] == [
        "10.0.0.1", "192.168.45.177", "192.168.45.190"]  # fmt: skip
    assert row.address.currentText() == "10.0.0.1"
    assert "probook answers at 10.0.0.1, not tx" in row.label.full()
    sent(w)
    row.address.setCurrentIndex(1)
    row.address.activated.emit(1)  # picked from the list
    assert [(j[0], j[1], j[2]) for j in sent(w)] == [
        ("address:tx", "/v1/controller/devices/address",
         {"name": "tx", "host": "192.168.45.177", "port": 10992})]  # fmt: skip
    w.commands._busy.discard("address:tx")  # the service answered (one at a time per PC)
    edit = row.address.lineEdit()
    assert edit is not None
    row.address.setEditText("10.0.0.7:10993")
    edit.returnPressed.emit()  # typed
    assert [j[2] for j in sent(w)] == [{"name": "tx", "host": "10.0.0.7", "port": 10993}]
    w.commands._busy.discard("address:tx")
    row.address.setEditText("10.0.0")
    edit.returnPressed.emit()
    assert not sent(w) and "is not an address" in f.told[-1]
    row.address.setEditText("10.0.0.1")  # the one in use: nothing to do
    edit.returnPressed.emit()
    assert not sent(w)
    w._after(Done("address:tx", False, {}, {"what": "probook answers at 10.0.0.7, not tx",
                                           "why": "The address belongs to probook."}))  # fmt: skip
    assert f.told[-1] == "probook answers at 10.0.0.7, not tx\n\nThe address belongs to probook."


def test_parse_address() -> None:
    assert parse_address("10.0.0.1") == ("10.0.0.1", 10992)
    assert parse_address(" 192.168.0.27:10993 ") == ("192.168.0.27", 10993)
    assert parse_address("10.0.0.256") is None
    assert parse_address("probook") is None
    assert parse_address("10.0.0.1:0") is None


def test_each_question_button_says_what_it_does() -> None:
    """2026-10-10: the Reset All question had a "Stop" button."""
    from controller.ui.main_window import yes_label

    assert yes_label(TEXT["reset_title"]) == "Reset All"
    assert yes_label(TEXT["shutdown_title"]) == "Shut Down All"
    assert yes_label(TEXT["stop_title"]) == "Stop"
    assert yes_label(TEXT["stop_broadcast_title"]) == "Stop"


def test_another_test_shows_its_own_settings_on_the_test_area(
    win: tuple[MainWindow, Fakes],
) -> None:
    """2026-10-17 user: "설정을 바꾸고 싶을수도 있는데 바꿀 방법도 없고", "test type이 iptv가
    아니면, video quality, source video, duration대신 iperf관련한 걸 보여주는게 맞지": the
    rows were made once for IPTV (Throughput's never showed) and the video fields stayed."""
    w, _ = win
    tp = {"S-60": {"value": "TCP", "label": "Protocol", "where": "adv", "kind": "choice",
                   "choices": ["TCP", "UDP"], "help": "TCP or UDP.", "lock": True,
                   "available": True, "module": "throughput", "group": "Throughput"},
          "S-58": {"value": 30, "label": "Measure time per direction", "where": "adv",
                   "kind": "int", "min": 5, "max": 600, "help": "Seconds.", "lock": True,
                   "available": True, "module": "throughput", "group": "Throughput"}}  # fmt: skip
    idle = ("status", Status(True, "b", {"state": "IDLE"}, ()))
    feed(w, idle, ("schema", SCHEMA | tp), ("setup", setup_answer()))
    assert w.c_quality.isVisibleTo(w) and w.c_duration.isVisibleTo(w)
    assert not w.g_mod.isVisibleTo(w) and "S-15" in w._adv and not w._mod
    answer = setup_answer(module="throughput", test_type="Throughput · All")
    answer["preview"] = ["Command: iperf3 -c 10.0.0.1 -p 5211 -t 30 -J -P 1   — receiving adds -R."]
    feed(w, ("setup", answer))
    assert not w.c_quality.isVisibleTo(w) and not w.c_duration.isVisibleTo(w)
    assert not w.sources.isVisibleTo(w)
    assert w.g_mod.isVisibleTo(w) and sorted(w._mod) == ["S-58", "S-60"]
    assert "S-60" not in w._adv  # on the Test area, not twice
    proto = w._mod["S-60"][1]
    assert isinstance(proto, QComboBox) and proto.isEnabled()
    assert [proto.itemText(i) for i in range(proto.count())] == ["TCP", "UDP"]
    assert w.l_preview.text().startswith("Command: iperf3 -c 10.0.0.1")
    feed(w, ("setup", setup_answer()))
    assert w.c_quality.isVisibleTo(w) and not w.g_mod.isVisibleTo(w) and not w._mod


def test_a_test_without_5_minute_boxes_shows_its_cases(win: tuple[MainWindow, Fakes]) -> None:
    """2026-10-17 user: "5-min result화면도 지금 이상황에선 안보여도 되는데 ...
    다른걸 보여주던가"."""
    w, _ = win
    feed(w, ("status", Status(True, "b", {"state": "IDLE"}, ())), ("schema", SCHEMA),
         ("setup", setup_answer(module="throughput", test_type="Throughput · All")))  # fmt: skip
    assert w.g_cells.title() == TEXT["group_cases"] and not w.grid.isVisibleTo(w)
    assert w.no_cases.isVisibleTo(w) and not w.cases.isVisibleTo(w)
    cell = {"color": "green", "reasons": [], "values": [
        {"key": "case", "label": "Case", "unit": "", "value": "Auto"},
        {"key": "send", "label": "LAN→WAN (send)", "unit": "Mbps", "value": 934.2}]}  # fmt: skip
    run = {"state": "IDLE", "cells": {"t460": [cell]}}
    feed(w, ("status", Status(True, "b", run, ())))
    assert w.cases.isVisibleTo(w) and w.cases.rowCount() == 1
    case, rate = w.cases.item(0, 2), w.cases.item(0, 3)
    assert case is not None and rate is not None
    assert case.text() == "Auto" and rate.text() == "934"
    feed(w, ("setup", setup_answer()))
    assert w.g_cells.title() == TEXT["group_cells"] and w.grid.isVisibleTo(w)
    assert not w.cases.isVisibleTo(w)


def test_the_wheel_does_not_change_a_box_not_clicked(win: tuple[MainWindow, Fakes]) -> None:
    """2026-10-22: scrolling the Test panel changed "Without the Controller" by itself."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication, QComboBox

    box = QComboBox()
    box.addItems(["Keep running to the planned end", "Stop after 10 s"])
    box.show()
    box.clearFocus()

    def wheel() -> None:
        ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120),
                         Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)  # fmt: skip
        QApplication.sendEvent(box, ev)

    wheel()
    assert box.currentIndex() == 0  # not clicked: the wheel leaves it alone
    box.setFocus()
    QApplication.processEvents()
    if box.hasFocus():  # offscreen may not give focus; when it does, the wheel works
        wheel()
        assert box.currentIndex() == 1
    box.close()


def test_a_linux_pc_is_added_from_the_screen(win: tuple[MainWindow, Fakes]) -> None:
    """2026-10-31: the black window of 2_리눅스PC_등록.bat as a dialog; the password goes to
    the service once and is not kept, and the answer is always said."""
    from controller.ui import main_window as mw

    w, f = win
    typed = {"host": "10.0.0.9", "user": "zl", "password": "pw", "name": "rx2",
             "auto_login": True}  # fmt: skip
    asked: list[tuple[str, dict[str, Any]]] = []

    def record(job: str, path: str, /, **kw: Any) -> bool:
        asked.append((path, kw))
        return True

    real_ask, real_send = mw.LinuxDialog.ask, w.commands.send
    mw.LinuxDialog.ask = lambda self: dict(typed)  # type: ignore[method-assign]
    w.commands.send = record  # type: ignore[method-assign,assignment]
    try:
        w._add_linux()
    finally:
        mw.LinuxDialog.ask, w.commands.send = real_ask, real_send  # type: ignore[method-assign]
    assert asked[0][0].endswith("/install/linux") and asked[0][1]["password"] == "pw"
    assert not w.b_add_linux.isEnabled()
    feed(w, ("done", Done("install_linux", True, {"name": "rx2", "host": "10.0.0.9",
                                                  "tools_missing": ["tsp"]})))  # fmt: skip
    assert "rx2 (10.0.0.9) is installed" in f.told[-1] and "tsp" in f.told[-1]
    assert w.b_add_linux.isEnabled()
    feed(w, ("done", Done("install_linux", False, {}, {"what": "SSH refused"})))
    assert f.told[-1].startswith("SSH refused")


def test_the_linux_dialog_needs_the_three_fields_and_forgets_the_password(
    win: tuple[MainWindow, Fakes],
) -> None:
    from controller.ui.main_window import LinuxDialog

    w, _ = win
    d = LinuxDialog(w)
    d.host.setText("10.0.0.9")
    d._ok()
    assert d.why.text() == TEXT["linux_need"] and d.result() == 0
    d.user.setText("zl")
    d.password.setText("pw")
    d._ok()
    assert d.result() == 1


def test_remove_asks_then_takes_the_app_off_a_tablet_or_zeroloss_off_a_pc(
    win: tuple[MainWindow, Fakes],
) -> None:
    """06 7 (2026-10-31): a tablet row's Remove asks, then the app comes off it; a PC row's
    asks its account in a dialog; the answer is always said."""
    from controller.ui import main_window as mw

    w, f = win
    w.store.setup = {"sides": {"rx": {"tablets": [{"serial": "S1", "state": "device"}]}}}
    asked: list[tuple[str, dict[str, Any]]] = []

    def record(job: str, path: str, /, **kw: Any) -> bool:
        asked.append((path, kw))
        return True

    real_send, real_ask = w.commands.send, mw.LinuxDialog.ask
    w.commands.send = record  # type: ignore[method-assign,assignment]
    mw.LinuxDialog.ask = lambda self: {"user": "zl", "password": "pw", "purge": True}  # type: ignore[method-assign]
    try:
        w._remove("tab:S1")
        w._remove("rx")
    finally:
        w.commands.send, mw.LinuxDialog.ask = real_send, real_ask  # type: ignore[method-assign]
    assert asked[0] == ("/v1/controller/remove/tablet_app", {"timeout_s": 150, "device": "rx",
                                                      "serial": "S1"})  # fmt: skip
    assert asked[1][0].endswith("/remove/linux") and asked[1][1]["purge"] is True
    feed(w, ("done", Done("remove_linux:rx", True, {})))
    assert f.told[-1] == TEXT["removed_pc"].format(name="rx")
    feed(w, ("done", Done("remove_tablet:S1", False, {}, {"what": "adb refused"})))
    assert f.told[-1].startswith("adb refused")


def test_help_opens_the_guide_at_this_tests_part(win: tuple[MainWindow, Fakes]) -> None:
    """08 2 (2026-10-31): the guide, served by the results web, at the chosen test's part."""
    w, _ = win
    w.store.setup = {"setup": {"test_type": "Throughput · Wi-Fi"}}
    assert w.help_url().endswith("/guide#tput")
    w.store.setup = {"setup": {"test_type": "IPTV test"}}
    assert w.help_url().endswith("/guide#run")
