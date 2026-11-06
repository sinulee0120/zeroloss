"""Controller Desktop main screen S1 (docs/11-ui.md 3).

The window only draws the Store (1.1): workers put messages on one queue, a 100 ms timer
drains it on the screen thread, and the window redraws when the Store's version changed
(1.3, 1.13). Enabled / visible states come only from ``Store.controls()`` (1.10), and
widgets are set only when their value differs, with signals blocked (1.13).
"""

from __future__ import annotations

import functools
import json
import queue
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDesktopServices,
    QFont,
    QHelpEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QShowEvent,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from controller.cli import (
    DIAG_TIMEOUT_S,
    INSTALL_LINUX_TIMEOUT_S,
    RESET_TIMEOUT_S,
    START_TIMEOUT_S,
    STORAGE_TIMEOUT_S,
)
from controller.setup import TEST_TYPE, module_of
from controller.ui.control_state import Ctl, storage_actions
from controller.ui.reports_window import ReportsWindow
from controller.ui.storage_window import StorageWindow
from controller.ui.store import (
    Commands,
    Done,
    Feed,
    IdleCheck,
    Message,
    P,
    SetupFeed,
    Status,
    Store,
)
from controller.ui.texts import DURATIONS_S, QUALITY_LABELS, TEST_TYPES, TEXT
from controller.ui.view import (
    GENERATED,
    TABLET,
    TABLET_CARD,
    TABLETS_MAX,
    ActionRow,
    ActionsView,
    CellView,
    FaultView,
    ProblemRow,
    ProblemsView,
    ResultView,
    RowView,
    SetupRowView,
    StorageRow,
    actions_view,
    adv_help,
    adv_items,
    adv_tip,
    adv_why,
    card_change,
    card_links,
    case_table,
    cell_rows,
    checklist_lines,
    chosen_sources,
    deleted_note,
    device_rows,
    export_text,
    fault_view,
    finished_line,
    fit_cells,
    hms,
    making_lines,
    parse_address,
    parse_duration,
    problems_view,
    progress,
    ready_source,
    receiver_change,
    reports_view,
    result_view,
    role_change,
    setup_rows,
    size_text,
    source_change,
    source_channels,
    source_options,
    source_value,
    start_summary,
    status_line,
    storage_view,
    tablet_role_change,
    tablets_behind,
    tablets_present,
    ticks_change,
)
from controller.web import WEB_PORT
from core.settings import CORE_DEFAULTS
from core.ui_qt.screen import MIN_USABLE
from core.ui_schema.design import (
    COLORS,
    FONT_PT,
    GRID,
    ICONS,
    live_cell,
)

UPDATE_TIMEOUT_S = 1800.0  # every PC: upload, install, node restart
TABLET_SCAN_S = 20.0  # a tablet's Scan shows "Scanning…" until its list is read again
# a card line's tick box, plain to see on the light page (unchecked / ticked / not tickable)
CARD_TICKS = (
    "QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #5f6368;"
    " border-radius: 3px; background: #ffffff; }"
    "QCheckBox::indicator:checked { background: #1f5fbf; border-color: #1f5fbf; }"
    "QCheckBox::indicator:disabled { border-color: #c8c8c8; background: #eeeeee; }"
)
WIFI = "S-65"  # Wi-Fi to test (throughput)
TEST_TYPE_LABEL = {v: k for k, v in TEST_TYPE.items()}  # plan value -> screen value


def shown_choice(choice: dict[str, Any], run: dict[str, Any], run_state: str) -> dict[str, Any]:
    """The Test panel's values: the saved choice, or during a test the plan that runs
    (02 2-1 ``run.plan``, GAP-105 4 — a test started with --plan showed the saved one)."""
    running = run.get("plan") if run_state != "IDLE" else None
    if not isinstance(running, dict):
        return choice
    shown = {
        "test_type": TEST_TYPE_LABEL.get(str(running.get("test_type"))),
        "quality": running.get("quality"),
        "duration_s": running.get("duration_s"),
    }
    return {**choice, **{k: v for k, v in shown.items() if v is not None}}


QWIDGETSIZE_MAX = 16777215  # Qt's "no limit" for a widget size
NARROW = QSize(1280, 720)  # below this usable size: stacked layout (1.8)
CELL_PX = 2 * GRID  # one 5-min cell
LENGTH = "S-55"  # Source length: 10 min / Whole movie (GAP-118)
SOURCES = "S-48"  # Source video, on the Test area (11-ui 1.10, GAP-115)
SOURCES_EVERY_S = 30.0  # S-48 list: read again between tests
MAKING_EVERY_S = 3.0  # the picked video's making state (GAP-116)
PREPARE_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 600.0  # the report of a long test is written before Stop answers
SOURCES_TIMEOUT_S = 60.0  # the sender runs ffprobe on every file (iptv 1-1)


def _tabular(font: QFont) -> QFont:
    """Same-width digits so values do not shake (1.11, U20)."""
    f = QFont(font)  # the one UI font (1.11), only its tabular digits switched on
    f.setFeature(QFont.Tag("tnum"), 1)
    return f


def _set_text(label: QLabel, text: str) -> None:
    if label.text() != text:
        label.setText(text)


def _set_tip(w: QWidget, tip: str) -> None:
    if w.toolTip() != tip:
        w.setToolTip(tip)


class SourcesBox(QWidget):
    """S-48 Source video: one list per channel (11-ui 1.10, GAP-110)."""

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(GRID // 2)
        self.row = QGridLayout()  # one row per channel: "CH1 [list]" (GAP-111)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setHorizontalSpacing(GRID)
        self.row.setColumnStretch(1, 1)
        outer.addLayout(self.row)
        self.note = QLabel()  # the making of the picked video (iptv 1-1-2, GAP-116)
        self.note.setWordWrap(True)
        self.note.setVisible(False)
        outer.addWidget(self.note)
        self.boxes: dict[int, QComboBox] = {}
        self.key: tuple[Any, ...] = ()


class Elided(QLabel):
    """A text field that never overflows: '…' plus the full text as tooltip (1.11)."""

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(4 * GRID)
        self.set_full(text)

    def set_full(self, text: str, tip: str = "") -> None:
        if text != self._full:
            self._full = text
            self._elide()
        _set_tip(
            self, tip or (text if self.fontMetrics().horizontalAdvance(text) > self.width() else "")
        )

    def full(self) -> str:
        return self._full

    def resizeEvent(self, e: QResizeEvent) -> None:
        super().resizeEvent(e)
        self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, self.width())
        _set_text(self, shown)
        if shown != self._full and not self.toolTip():
            self.setToolTip(self._full)

    def sizeHint(self) -> QSize:
        return QSize(
            self.fontMetrics().horizontalAdvance(self._full) + GRID, super().sizeHint().height()
        )


class _Fit(QScrollArea):
    """A scroll area as wide and, when there is room, as tall as its content: it only
    scrolls on a short screen (1.8), never squeezes or cuts (1.11)."""

    def sizeHint(self) -> QSize:
        w = self.widget()
        if w is None:
            return super().sizeHint()
        bar = self.verticalScrollBar().sizeHint().width()
        return QSize(w.sizeHint().width() + bar, w.sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        w = self.widget()
        bar = self.verticalScrollBar().sizeHint().width()
        width = (w.minimumSizeHint().width() + bar) if w is not None else 0
        return QSize(width, 4 * GRID)


def _fit_field(w: QWidget) -> None:
    """An Advanced field takes the width that is left next to its label: the Test area
    never scrolls sideways, so a wide field would be cut off (11-ui 1.11, GAP-111)."""
    w.setMinimumWidth(8 * GRID)
    w.setSizePolicy(QSizePolicy.Policy.Ignored, w.sizePolicy().verticalPolicy())
    if isinstance(w, QComboBox):
        _fit_combo(w)


def _fit_combo(box: QComboBox) -> None:
    """A list as narrow as the room: its shown text is shortened, the open list shows
    every name whole, and the full choice is the tooltip (1.11)."""
    box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(4)
    box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    view = box.view()
    if view is not None:
        view.setMinimumWidth(view.sizeHintForColumn(0) + 4 * GRID)
    box.setToolTip(box.currentText())
    box.currentTextChanged.connect(box.setToolTip)


def _scroll(content: QWidget) -> QScrollArea:
    s = _Fit()
    s.setWidgetResizable(True)
    s.setFrameShape(QScrollArea.Shape.NoFrame)
    s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    s.setWidget(content)
    return s


def yes_label(title: str) -> str:
    """The confirm button of a question (1.6): its action, not a generic Yes."""
    for key, label in (("reset_title", "Reset All"), ("shutdown_title", "Shut Down All"),
                       ("start_over_broadcast_title", "Stop broadcast and start"),
                       ("start_title", "Start")):  # fmt: skip
        if title == TEXT[key]:
            return label
    return "Stop"  # Stop the test? / Stop the broadcast?


BUFFER_CHOICES_MS = (500, 1000, 2000, 3000, 5000)  # S-25 range 300~5,000


class SetupRow(QWidget):
    """F14 device line while no test runs: name · side, role, TV channel, network."""

    def __init__(self, on_role: Callable[[str], None], on_ch: Callable[[int], None],
                 on_net: Callable[[str], None],
                 on_card: Callable[[str], None] = lambda ip: None,
                 on_address: Callable[[str], None] = lambda text: None,
                 on_buffer: Callable[[int], None] = lambda ms: None,
                 on_tick: Callable[[str, bool], None] = lambda nic, on: None,
                 on_band: Callable[[str, str, str], None] = lambda nic, band, bssid: None,
                 on_scan: Callable[[str], None] = lambda nic: None,
                 on_password: Callable[[str, str], None] = lambda s, p: None,
                 on_tabwifi: Callable[[str, str], None] = lambda s, sec: None,
                 on_remove: Callable[[], None] = lambda: None) -> None:  # fmt: skip
        super().__init__()
        # the name and side on a line of their own: the boxes below never cover them
        # (2026-10-16 user: the receiver's words were hidden by its boxes)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, GRID // 2, 0, GRID // 2)
        v.setSpacing(GRID // 2)
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(GRID)
        self.label = Elided()
        self.role = QComboBox()
        for key in ("sender", "receiver", "unused"):
            self.role.addItem(TEXT[f"role_{key}"], key)
        MainWindow._help(self.role, TEXT["tip_role"])
        self.ch = QComboBox()
        for c in (1, 2, 3):
            self.ch.addItem(f"CH{c}", c)
        MainWindow._help(self.ch, TEXT["tip_channel"])
        self.net = QComboBox()
        for n in ("Wired", "Wireless"):
            self.net.addItem(n, n)
        MainWindow._help(self.net, TEXT["tip_network"])
        # 2026-10-16 user: "수신기마다 다를거아니냐" -- the player buffer per receiver PC
        self.buffer = QComboBox()
        self.buffer.addItem(TEXT["buffer_default"], 0)
        for ms in BUFFER_CHOICES_MS:
            self.buffer.addItem(TEXT["buffer_ms"].format(s=ms / 1000), ms)
        MainWindow._help(self.buffer, TEXT["tip_buffer"])
        self.card = QComboBox()  # the test card (DSC-006, GAP-115): Auto or one of its own
        self.card.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        MainWindow._help(self.card, TEXT["tip_card"])
        self._cards: tuple[tuple[str, str], ...] | None = None
        # GAP-155 (2026-10-15 user: "let the user pick among the IPs; when there is
        # nothing to pick, let them type it"): the PC's address, always here, editable
        self.address = QComboBox()
        self.address.setEditable(True)
        self.address.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.address.setMinimumContentsLength(15)
        self.address.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        MainWindow._help(self.address, TEXT["tip_address"])
        self._addresses: tuple[tuple[str, ...], str] | None = None
        # the item's own address (its text also says WAN / LAN / Wi-Fi)
        self.address.activated.connect(
            lambda _: on_address(str(self.address.currentData() or self.address.currentText()))
        )
        line = self.address.lineEdit()
        if line is not None:
            line.setPlaceholderText(TEXT["address_placeholder"])
            line.returnPressed.connect(lambda: on_address(self.address.currentText()))
        v.addWidget(self.label)
        v.addLayout(h)
        # a test without video: one line per card, ticked to measure it (2026-10-17 user)
        self.cards_box = QWidget()
        self.cards_grid = QGridLayout(self.cards_box)
        self.cards_grid.setContentsMargins(3 * GRID, 0, 0, 0)
        self.cards_grid.setHorizontalSpacing(GRID)
        self.cards_grid.setVerticalSpacing(GRID // 2)
        self.cards_grid.setColumnStretch(3, 1)
        self.cards_box.setVisible(False)
        self.cards_box.setStyleSheet(CARD_TICKS)
        v.addWidget(self.cards_box)
        self._lines: tuple[Any, ...] | None = None
        self._on_tick, self._on_band, self._on_scan = on_tick, on_band, on_scan
        self._on_password = on_password
        # a tablet's Wi-Fi (S-51): what it sees, joined by adb at Start; its password box
        self.tabwifi = QComboBox()
        self.tabwifi.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        MainWindow._help(self.tabwifi, TEXT["tip_tablet_wifi"])
        self.tabwifi.setVisible(False)
        self._tabwifi: tuple[Any, ...] | None = None
        self.tabpw: QLineEdit | None = None
        self._tabpw: tuple[str, str] | None = None
        self.tabwifi.activated.connect(lambda _: on_tabwifi(
            *(self.tabwifi.currentData() or ("", ""))))  # fmt: skip
        for w in (self.address, self.role, self.ch, self.net, self.buffer, self.card, self.tabwifi):
            h.addWidget(w)
        self._h = h
        h.addStretch(1)
        self.remove = QPushButton(TEXT["remove_mark"])  # 06 7: ZeroLoss off it (between tests)
        self.remove.setFixedWidth(3 * GRID)  # 2026-10-31: "Remove…" made the rows scroll
        # 2026-11-05 user ("허연 빈 박스 ... 글자도 없고"): the app's button padding left the
        # mark no room in 3 * GRID and it was cut off; no padding here, a mark every font has
        self.remove.setStyleSheet("QPushButton { padding: 0px; font-weight: bold; }")
        self.remove.setToolTip(TEXT["remove_tip"])
        self.remove.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.remove.clicked.connect(lambda: on_remove())
        h.addWidget(self.remove)
        self.role.activated.connect(lambda _: on_role(str(self.role.currentData())))
        self.ch.activated.connect(lambda _: on_ch(int(self.ch.currentData())))
        self.net.activated.connect(lambda _: on_net(str(self.net.currentData())))
        self.buffer.activated.connect(lambda _: on_buffer(int(self.buffer.currentData())))
        self.card.activated.connect(lambda _: on_card(str(self.card.currentData() or "")))

    def _show_tablet_wifi(self, v: SetupRowView, receiver: bool, ctl: Ctl) -> None:
        if v.wifi_options != self._tabwifi:
            self._tabwifi = v.wifi_options
            self.tabwifi.clear()
            for label, ssid, sec in v.wifi_options:
                self.tabwifi.addItem(label, (ssid, sec))
        i = next((n for n, x in enumerate(v.wifi_options) if x[1] == v.wifi_pick), 0)
        if self.tabwifi.currentIndex() != i:
            self.tabwifi.setCurrentIndex(i)
        MainWindow._apply(self.tabwifi, ctl)
        want = (v.wifi_pw, v.wifi_pick) if receiver and v.wifi_pick else None
        if want != self._tabpw:  # the password box of the network picked, as a PC band's
            self._tabpw = want
            if self.tabpw is not None:
                self._h.removeWidget(self.tabpw)
                self.tabpw.deleteLater()
                self.tabpw = None
            if want is not None:
                self.tabpw = _password_box(self, want[0], want[1])
                self._h.insertWidget(self._h.indexOf(self.tabwifi) + 1, self.tabpw)

    def _fit_role(self, v: SetupRowView) -> None:
        """A tablet is a receiver or not used; a PC sends, receives or is not used."""
        want = ("receiver", "unused") if v.tablet else ("sender", "receiver", "unused")
        have = tuple(str(self.role.itemData(i)) for i in range(self.role.count()))
        if have != want:
            self.role.clear()
            for key in want:
                self.role.addItem(TEXT[f"role_{key}"], key)

    def _show_lines(self, lines: tuple[Any, ...], ctl: Ctl) -> None:
        _show_lines_impl(self, lines, ctl)

    def show_view(self, v: SetupRowView, ctl: Ctl) -> None:
        mark = "✔" if v.reachable and not (v.tablet and v.why) else "○"
        side = v.side if not v.why or (v.reachable and not v.tablet) else f"{v.side}: {v.why}"
        if v.this_pc:
            side = f"{side} · {TEXT['this_pc_video' if v.video else 'this_pc_iperf']}"
        self.label.set_full(f"{mark} {v.title or v.name} · {side}")
        self._fit_role(v)
        removable = not v.this_pc and ctl.enabled  # Controller PC: Windows' own uninstall
        if self.remove.isHidden() == removable:
            self.remove.setVisible(removable)

        MainWindow._select(self.role, v.role)
        MainWindow._select(self.ch, v.ch)
        MainWindow._select(self.net, v.network)
        if self.buffer.findData(v.buffer_ms) < 0:  # a plan's own value (300~5,000 ms)
            self.buffer.addItem(TEXT["buffer_ms"].format(s=v.buffer_ms / 1000), v.buffer_ms)
        MainWindow._select(self.buffer, v.buffer_ms)
        receiver = v.role == "receiver"
        if v.cards != self._cards:
            self._cards = v.cards
            self.card.clear()
            self.card.addItem(TEXT["card_auto"], "")
            for label, ip in v.cards:
                self.card.addItem(label, ip)
        if v.card and self.card.findData(v.card) < 0:  # picked, but not among its cards
            gone = TEXT["card_gone"] if v.cards_known else TEXT["card_unseen"]
            self.card.addItem(f"{v.card} {gone}", v.card)
        MainWindow._select(self.card, v.card)
        for w in (self.role, self.ch, self.net, self.buffer, self.card):
            MainWindow._apply(w, ctl)
        if v.this_pc and not self.role.isHidden():  # the Controller PC: nothing to pick
            self.role.setVisible(False)
        if v.tablet:  # a tablet: channel, buffer and its Wi-Fi; no card or address
            for w in (self.address, self.net, self.card):
                if not w.isHidden():
                    w.setVisible(False)
            for w in (self.ch, self.buffer, self.tabwifi):
                if w.isHidden() == receiver:
                    w.setVisible(receiver)
            self._show_tablet_wifi(v, receiver, ctl)
            return
        editing = self.address.hasFocus()  # never pull text from under the person
        if (v.addresses, v.host) != self._addresses and not editing:
            self._addresses = (v.addresses, v.host)
            said = dict(v.labels)
            self.address.clear()
            for a in v.addresses or ((v.host,) if v.host else ()):
                self.address.addItem(f"{a}  ({said[a]})" if a in said else a, a)
            self.address.setEditText(v.host)
        MainWindow._apply(self.address, ctl)
        for key in ("sender", "receiver"):  # the role's words for this kind of test
            want = TEXT[f"role_{key}" if v.video else f"role_{key}_iperf"]
            i = self.role.findData(key)
            if i >= 0 and self.role.itemText(i) != want:
                self.role.setItemText(i, want)
        for w in (self.ch, self.net, self.buffer):
            if w.isHidden() == (receiver and v.video):
                w.setVisible(receiver and v.video)
        self._show_lines(v.cards_lines, ctl)
        # a Wi-Fi receiver gets on the test Wi-Fi itself (03 F17); unused PCs need no card;
        # a test without video measures every card of the PC (no card box)
        wants = v.video and (v.role == "sender" or (receiver and v.network != "Wireless"))
        if self.card.isHidden() == wants:
            self.card.setVisible(wants)


def _password_box(row: SetupRow, state: str, ssid: str) -> QLineEdit:
    """The box beside a band's SSID: "open" (no password needed), empty or "password
    saved" for one that has a password; the text typed is sent once and cleared."""
    pw = QLineEdit()
    pw.setEchoMode(QLineEdit.EchoMode.Password)
    pw.setFixedWidth(16 * GRID)  # the SSID list beside it takes the rest
    pw.setToolTip(TEXT["wifi_pw_tip"])
    pw.setPlaceholderText({"open": TEXT["wifi_pw_open"], "saved": TEXT["wifi_pw_saved"],
                           "empty": TEXT["wifi_pw_empty"]}.get(state, ""))  # fmt: skip
    pw.setEnabled(state in ("empty", "saved"))
    pw.setProperty("tickable", state in ("empty", "saved"))

    def sent() -> None:
        if pw.text():
            row._on_password(ssid, pw.text())
            pw.clear()  # never kept on screen
            pw.setPlaceholderText(TEXT["wifi_pw_saved"])

    pw.editingFinished.connect(sent)
    return pw


def _band_picked(row: SetupRow, nic: str, band: str, box: QComboBox, _i: int) -> None:
    row._on_band(nic, band, str(box.currentData() or ""))


def _show_lines_impl(row: SetupRow, lines: tuple[Any, ...], ctl: Ctl) -> None:
    """The row's card lines: a tick per card; a ticked Wi-Fi card's bands under it."""
    if row.cards_box.isHidden() == bool(lines):
        row.cards_box.setVisible(bool(lines))
    if lines != row._lines:
        row._lines = lines
        grid = row.cards_grid
        while grid.count():
            entry = grid.takeAt(0)
            gone = entry.widget() if entry is not None else None
            if gone is not None:
                gone.deleteLater()
        r = 0
        for line in lines:
            tick = QCheckBox(line.label)
            tick.setChecked(line.ticked)
            tick.setEnabled(line.tickable)
            tick.setProperty("tickable", line.tickable)
            if not line.tickable:
                tick.setToolTip(TEXT["card_not_tickable"])
            tick.toggled.connect(functools.partial(row._on_tick, line.nic))
            grid.addWidget(tick, r, 0, 1, 4)
            r += 1
            if line.note:
                note = QLabel(line.note)
                note.setWordWrap(True)
                note.setStyleSheet(f"color: {COLORS['yellow']};")
                grid.addWidget(note, r, 1, 1, 4)
                r += 1
            for i, (band, opts, chosen, pw_box, ssid) in enumerate(line.bands):
                box = QComboBox()
                box.addItem(TEXT["wifi_band_none"], "")
                for label, bssid, usable, tip in opts:
                    box.addItem(label, bssid)
                    k = box.count() - 1
                    if tip:
                        box.setItemData(k, tip, Qt.ItemDataRole.ToolTipRole)
                    model = box.model()
                    item = model.item(k) if isinstance(model, QStandardItemModel) else None
                    if item is not None and not usable:
                        item.setEnabled(False)
                MainWindow._select(box, chosen)
                box.activated.connect(functools.partial(_band_picked, row, line.nic, band, box))
                grid.addWidget(QLabel(f"   {band}"), r, 1)
                grid.addWidget(box, r, 2)
                grid.addWidget(_password_box(row, pw_box, ssid), r, 3)
                if i == 0:
                    scan = QPushButton(
                        TEXT["wifi_scanning"] if line.scanning else TEXT["wifi_scan"]
                    )
                    scan.setToolTip(TEXT["wifi_scan_tip"])
                    scan.setEnabled(not line.scanning)
                    scan.clicked.connect(functools.partial(row._on_scan, line.nic))
                    grid.addWidget(scan, r, 4)
                r += 1
    for i in range(row.cards_grid.count()):  # locked during a test; a card never tickable stays off
        entry = row.cards_grid.itemAt(i)
        w = entry.widget() if entry is not None else None
        if isinstance(w, QCheckBox | QComboBox | QLineEdit):
            want = ctl.enabled and w.property("tickable") is not False
            if w.isEnabled() != want:
                w.setEnabled(want)


class DeviceRow(QWidget):
    """One device line: icon, name, TV channel (F19), main values (Jitter / Loss
    boldest), thin extras."""

    def __init__(self, on_channel: Callable[[int], None] = lambda ch: None) -> None:
        super().__init__()
        self.chan = QComboBox()
        MainWindow._help(self.chan, TEXT["tip_channel_live"])
        self.chan.activated.connect(lambda _: self._chosen(on_channel))
        self.chan.setVisible(False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)  # border drawn
        self.view: RowView | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, GRID // 2, 0, GRID // 2)
        outer.setSpacing(GRID // 4)
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(GRID)
        outer.addLayout(lay)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon.setFixedWidth(3 * GRID)
        self.name = QLabel()
        bold = QFont(self.font())
        bold.setBold(True)
        self.name.setFont(bold)
        self.values = Elided()
        self.values.setTextFormat(Qt.TextFormat.PlainText)
        self.values.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.key = QLabel()
        key_font = _tabular(bold)
        key_font.setPointSize(FONT_PT["key"])
        self.key.setFont(key_font)
        self.extra = Elided()
        self.extra.setFont(_tabular(self.font()))
        lay.addWidget(self.icon)
        lay.addWidget(self.name)
        lay.addWidget(self.chan)
        lay.addWidget(self.values)
        lay.addWidget(self.key, 1)
        # the thin extras (RSSI, Wi-Fi channel, ...) on a line of their own under the
        # values: on one line they were cut at the window edge (2026-10-22 user: "t460라인
        # 글자 끝에 잘린다")
        second = QHBoxLayout()
        second.setContentsMargins(4 * GRID, 0, 0, 0)
        second.addWidget(self.extra, 1)
        outer.addLayout(second)

    def _chosen(self, on_channel: Callable[[int], None]) -> None:
        ch = self.chan.currentData()
        if self.view is not None and isinstance(ch, int) and ch != self.view.ch:
            on_channel(ch)

    def show_view(self, v: RowView) -> None:
        if [self.chan.itemData(i) for i in range(self.chan.count())] != list(v.channels):
            self.chan.clear()
            for c in v.channels:
                self.chan.addItem(f"CH{c}", c)
        MainWindow._select(self.chan, v.ch)  # also after a pick that did not happen
        if v == self.view:
            return
        self.view = v
        cell = live_cell(v.color)
        _set_text(self.icon, v.icon)
        self.icon.setStyleSheet(
            f"background:{cell.color}; color:{cell.fg}; border-radius:4px; font-weight:bold;"
        )
        _set_tip(self.icon, v.why)
        _set_text(self.name, v.name)
        plain = "  ".join(p.text for p in v.parts if not p.key)
        self.values.set_full(plain, v.tooltip)
        _set_text(self.key, "   ".join(p.text for p in v.parts if p.key))
        extra = "  ".join(
            ("⚠ " if p.warn == "yellow" else "✖ " if p.warn == "red" else "") + p.text
            for p in v.extra
        )
        tips = "\n".join(p.tooltip for p in v.extra if p.tooltip)
        self.extra.set_full(extra, tips)
        if self.extra.isHidden() == bool(extra):
            self.extra.setVisible(bool(extra))
        border = f"2px solid {COLORS['red']}" if v.alarm else "2px solid transparent"
        style = f"DeviceRow {{ border: {border}; border-radius: 4px; }}"
        if self.styleSheet() != style:
            self.setStyleSheet(style)  # an open error for this device (F21)
        grey = COLORS["text_disabled"] if v.dim else COLORS["text"]
        for w in (self.name, self.values, self.key):
            w.setStyleSheet(f"color:{grey};")
        self.extra.setStyleSheet(
            f"color:{COLORS['text_disabled'] if v.dim else COLORS['text_muted']};"
        )

    def texts(self) -> str:
        """Everything the row shows, for tests and the black-and-white check (7)."""
        return " ".join(
            x
            for x in (
                self.icon.text(),
                self.name.text(),
                self.values.full(),
                self.key.text(),
                self.chan.currentText() if self.chan.isVisible() else "",
                self.extra.full(),
            )
            if x
        )


class CellGrid(QWidget):
    """5-min cells, one line per device; icon + color in every cell (1.7). Following live
    shows the newest cells that fit; the tooltip gives the cell's values."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, list[CellView]] = {}
        self.setMinimumHeight(CELL_PX + GRID)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMouseTracking(True)
        self.label_w = 10 * GRID

    def set_rows(self, rows: dict[str, list[CellView]]) -> None:
        if rows == self.rows:
            return
        self.rows = rows
        self.setMinimumHeight(max(1, len(rows)) * (CELL_PX + GRID // 2) + GRID)
        self.update()

    def _layout(self) -> list[tuple[str, list[tuple[QRect, CellView]]]]:
        fit = max(1, (self.width() - self.label_w) // (CELL_PX + 2))
        out = []
        for i, (name, cells) in enumerate(self.rows.items()):
            y = i * (CELL_PX + GRID // 2)
            shown = fit_cells(cells, fit)
            out.append(
                (
                    name,
                    [
                        (QRect(self.label_w + j * (CELL_PX + 2), y, CELL_PX, CELL_PX), c)
                        for j, c in enumerate(shown)
                    ],
                )
            )
        return out

    def paintEvent(self, e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.rows:
            p.setPen(QColor(COLORS["text_muted"]))
            p.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                TEXT["no_cells"],
            )
            return
        for i, (name, cells) in enumerate(self._layout()):
            y = i * (CELL_PX + GRID // 2)
            p.setPen(QColor(COLORS["text"]))
            p.drawText(
                QRect(0, y, self.label_w - GRID, CELL_PX),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                p.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, self.label_w - GRID),
            )
            for rect, c in cells:
                cell = live_cell(c.color)
                p.fillRect(rect, QColor(cell.color))
                if c.merged > 1:  # an older stretch in one box: a frame tells it apart
                    p.setPen(QColor(COLORS["text"]))
                    p.drawRect(rect.adjusted(0, 0, -1, -1))
                p.setPen(QColor(cell.fg))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, c.icon)

    def event(self, e: QEvent) -> bool:
        if e.type() == QEvent.Type.ToolTip and isinstance(e, QHelpEvent):
            for _, cells in self._layout():
                for rect, c in cells:
                    if rect.contains(e.pos()):
                        QToolTip.showText(e.globalPos(), c.tooltip, self)
                        return True
            QToolTip.hideText()
            return True
        return super().event(e)


class ActionsModel(QAbstractTableModel):
    COLS = TEXT["actions_cols"].split("|")

    def __init__(self) -> None:
        super().__init__()
        self.rows: tuple[ActionRow, ...] = ()

    def set_rows(self, rows: tuple[ActionRow, ...]) -> None:
        if rows != self.rows:
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(self.COLS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLS[section]
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = 0) -> Any:
        r = self.rows[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return (r.device, r.what, r.how, r.why, r.state)[index.column()]
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor(COLORS["text"] if r.waiting else COLORS["text_disabled"])
        return None


class LinuxDialog(QDialog):
    """Add a Linux PC (06 11.3, 2026-10-31): what 2_리눅스PC_등록.bat asked in a black
    window. The password goes to the local service once and is never kept here."""

    def __init__(self, parent: MainWindow, remove: str = "") -> None:
        super().__init__(parent)
        self._main = parent
        self._remove = remove  # a removal from this PC: user, password and purge only
        self.setWindowTitle(TEXT["remove_pc_title"].format(name=remove) if remove
                            else TEXT["linux_title"])  # fmt: skip
        form = QGridLayout(self)
        self.host, self.user, self.password, self.name = (QLineEdit() for _ in range(4))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.auto = QCheckBox(TEXT["linux_auto_login"])
        for i, (key, box) in enumerate((("linux_host", self.host), ("linux_user", self.user),
                                        ("linux_password", self.password),
                                        ("linux_name", self.name))):  # fmt: skip
            form.addWidget(QLabel(TEXT[key]), i, 0)
            form.addWidget(box, i, 1)
        form.addWidget(self.auto, 4, 0, 1, 2)
        self.purge = QCheckBox(TEXT["remove_purge"])  # default off: data kept (06 1 5)
        form.addWidget(self.purge, 4, 0, 1, 2)
        for w in (self.host, self.name, self.auto) if remove else (self.purge,):
            w.setVisible(False)
        if remove:
            for i in (0, 3):  # their labels too
                item = form.itemAtPosition(i, 0)
                label = item.widget() if item is not None else None
                if label is not None:
                    label.setVisible(False)
            self.host.setText(remove)
        self.why = QLabel("")
        form.addWidget(self.why, 5, 0, 1, 2)
        ok = QPushButton(TEXT["remove"] if remove else TEXT["linux_title"])
        cancel = QPushButton("Cancel")
        ok.setDefault(True)
        ok.clicked.connect(self._ok)
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        form.addLayout(buttons, 6, 0, 1, 2)

    def _ok(self) -> None:
        if not (self.host.text().strip() and self.user.text().strip() and self.password.text()):
            self.why.setText(TEXT["linux_need"])
            return
        self.accept()

    def ask(self) -> dict[str, Any] | None:
        if self._main._modal_open:
            return None
        self._main._modal_open = True
        try:
            if self.exec() != QDialog.DialogCode.Accepted:
                return None
            got = {"host": self.host.text().strip(), "user": self.user.text().strip(),
                   "password": self.password.text(), "name": self.name.text().strip(),
                   "auto_login": self.auto.isChecked()}  # fmt: skip
            return {**got, "purge": self.purge.isChecked()} if self._remove else got
        finally:
            self.password.clear()
            self._main._modal_open = False


class ActionsWindow(QDialog):
    """03 F22: its own window above ZeroLoss, never modal; comes to the front when a
    new action appears. [Done] on every waiting row."""

    def __init__(
        self,
        parent: QWidget,
        send_done: Callable[[str], None],
        send_choice: Callable[[str, str], None] = lambda key, choice: None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(TEXT["actions_title"])
        self.setModal(False)
        # above the ZeroLoss window (its parent), not above every program: 2026-10-17 user
        # "화면 가운데 가려버리고" -- it covered the middle of the screen over everything
        self._send_done = send_done
        self._send_choice = send_choice
        self._shown_choices: tuple[str, tuple[tuple[str, str], ...]] = ("", ())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2 * GRID, 2 * GRID, 2 * GRID, 2 * GRID)
        self.empty = QLabel(TEXT["actions_none"])
        self.model = ActionsModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        head = self.table.horizontalHeader()
        for col in range(len(ActionsModel.COLS)):
            fit = col in (0, 4)
            head.setSectionResizeMode(
                col,
                QHeaderView.ResizeMode.ResizeToContents if fit else QHeaderView.ResizeMode.Stretch,
            )
        self.b_done = QPushButton(TEXT["actions_done_button"])
        self.b_done.setEnabled(False)
        self.b_done.setToolTip(TEXT["actions_done_tip"])
        # 20 W-1: an action that offers choices (e.g. which card goes to the router WAN)
        # gets one button per choice, for the selected row only
        self.choice_box = QWidget()
        self.choice_row = QHBoxLayout(self.choice_box)
        self.choice_row.setContentsMargins(0, 0, 0, 0)
        self.choice_row.addWidget(QLabel(TEXT["actions_choose"]))
        self.choice_row.addStretch(1)
        self.choice_box.setVisible(False)
        # 2026-10-09 user: "all done, so why a Done button that cannot be clicked?" [Done]
        # is only for a waiting action; with none waiting the corner says so and closes
        self.all_done = QLabel(TEXT["actions_all_done"])
        self.all_done.setVisible(False)
        self.b_close = QPushButton(TEXT["actions_close_button"])
        self.b_close.setVisible(False)
        self.b_close.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addWidget(self.all_done)
        row.addStretch(1)
        row.addWidget(self.b_done)  # area action: bottom right (11-ui 1.11)
        row.addWidget(self.b_close)
        lay.addWidget(self.empty)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.choice_box)
        lay.addLayout(row)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._enable())
        self.b_done.clicked.connect(self._done)
        self.resize(110 * GRID, 40 * GRID)

    def show_view(self, v: ActionsView) -> None:
        self.model.set_rows(v.rows)
        if self.empty.isHidden() == (not v.rows):
            self.empty.setVisible(not v.rows)
        self._enable()

    def _selected(self) -> ActionRow | None:
        """The row picked, else the first waiting one: [Done] never needs a hunt for a
        row first (GAP-116)."""
        rows = self.table.selectionModel().selectedRows()
        if rows:
            return self.model.rows[rows[0].row()]
        return next((r for r in self.model.rows if r.waiting), None)

    def _enable(self) -> None:
        r = self._selected()
        on = bool(r and r.manual)
        if self.b_done.isEnabled() != on:
            self.b_done.setEnabled(on)
        waiting = any(x.waiting for x in self.model.rows)
        if self.b_done.isHidden() == waiting:
            self.b_done.setVisible(waiting)
        if self.b_close.isHidden() != waiting:
            self.b_close.setVisible(not waiting)
        finished = bool(self.model.rows) and not waiting
        if self.all_done.isHidden() == finished:
            self.all_done.setVisible(finished)
        self._show_choices(r)

    def _show_choices(self, r: ActionRow | None) -> None:
        """Rebuilt only when the selected row's choices change (11-ui 1.13: no redraw
        loops); hidden when the row offers none."""
        want = (r.key, r.choices) if r is not None and r.choices else ("", ())
        if want == self._shown_choices:
            return
        self._shown_choices = want
        while self.choice_row.count() > 2:  # keep the label and the stretch
            item = self.choice_row.takeAt(1)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        key, choices = want
        for i, (cid, label) in enumerate(choices):
            b = QPushButton(label)
            b.setToolTip(TEXT["actions_choose_tip"])
            b.clicked.connect(lambda _=False, c=cid: self._send_choice(key, c))
            self.choice_row.insertWidget(1 + i, b)
        if self.choice_box.isHidden() == bool(choices):
            self.choice_box.setVisible(bool(choices))

    def _done(self) -> None:
        r = self._selected()
        if r is not None and r.manual:
            self._send_done(r.key)


class ProblemsModel(QAbstractTableModel):
    """Only visible rows are drawn (11-ui 1.3); rows change only when the list does."""

    COLS = TEXT["problems_cols"].split("|")

    def __init__(self) -> None:
        super().__init__()
        self.rows: tuple[ProblemRow, ...] = ()

    def set_rows(self, rows: tuple[ProblemRow, ...]) -> None:
        if rows != self.rows:
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(self.COLS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLS[section]
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = 0) -> Any:
        r = self.rows[index.row()]
        col = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            what = TEXT["problems_repeat"].format(what=r.what, n=r.count) if r.count > 1 else r.what
            if r.stale:
                what += f" — {TEXT['problems_stale']}"
            values = (f"{r.icon} {r.time}", r.device, r.code, what, r.why, r.fix)
            return values[col]
        if role == Qt.ItemDataRole.ForegroundRole:
            if not r.active:
                return QColor(COLORS["text_disabled"])
            if col == 0:  # the icon column carries the level color (with the icon, 1.7)
                return QColor(COLORS["red"] if r.color == "red" else COLORS["yellow"])
            return QColor(COLORS["text"])
        return None


class ProblemsWindow(QDialog):
    """03 F21: open problems first, resolved ones grey below; a flood shows one summary
    line and only the errors. Not modal: the test screen stays usable."""

    def __init__(
        self,
        parent: QWidget,
        on_export: Callable[[], None] = lambda: None,
        on_open: Callable[[str], None] = lambda path: None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(TEXT["problems_title"])
        self.setModal(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2 * GRID, 2 * GRID, 2 * GRID, 2 * GRID)
        self.flood = QLabel()
        self.flood.setStyleSheet(f"color:{COLORS['red']}; font-weight:bold;")
        self.flood.setVisible(False)
        self.empty = QLabel(TEXT["problems_none"])
        self.model = ProblemsModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        # 2026-10-09 user: "columns cannot be widened, so I cannot read it all": every
        # column is dragged wider or narrower, texts wrap, the picked row is shown in full
        self.table.setWordWrap(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        head.setStretchLastSection(True)
        # 2026-10-10 user: "What to do" was squeezed to a sliver at the right: What, Why
        # and What to do share the width left after Time, Device and Code, again at every
        # window resize, until the person drags a column (then their widths stay)
        self._fitting = False
        self._dragged = False
        head.sectionResized.connect(self._section_dragged)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setVisible(False)
        lay.addWidget(self.flood)
        lay.addWidget(self.empty)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.detail)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._show_detail())
        # 03 F21 / 20 GAP-82: always here, bottom right; the file path shows below
        self.export_note = QLabel()
        self.export_note.setWordWrap(True)
        self.export_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.export_note.setVisible(False)
        self.b_folder = QPushButton(TEXT["open_folder"])
        self.b_folder.setVisible(False)
        self.b_export = QPushButton("Export diagnostics")
        row = QHBoxLayout()
        row.addWidget(self.export_note, 1)
        row.addWidget(self.b_folder)
        row.addWidget(self.b_export)
        lay.addLayout(row)
        self._folder = ""
        self.b_export.clicked.connect(on_export)
        self.b_folder.clicked.connect(lambda: on_open(self._folder))
        self.resize(150 * GRID, 60 * GRID)
        self._fit_columns()

    FIXED_COLS = (12, 14, 12)  # Time, Device, Code (GRID units)

    def _section_dragged(self, col: int, *_: object) -> None:
        last = self.table.horizontalHeader().count() - 1
        if not self._fitting and col != last:  # the last one stretches by itself
            self._dragged = True

    def _fit_columns(self) -> None:
        if self._dragged:
            return
        head = self.table.horizontalHeader()
        self._fitting = True
        try:
            fixed = [w * GRID for w in self.FIXED_COLS]
            for col, w in enumerate(fixed):
                head.resizeSection(col, w)
            room = self.table.viewport().width() - sum(fixed)
            text_cols = max(0, self.model.columnCount() - len(fixed))
            share = max(20 * GRID, room // text_cols) if text_cols else 0
            for col in range(len(fixed), len(fixed) + text_cols):
                head.resizeSection(col, share)
        finally:
            self._fitting = False

    def resizeEvent(self, e: QResizeEvent) -> None:
        super().resizeEvent(e)
        self._fit_columns()

    def showEvent(self, e: QShowEvent) -> None:
        super().showEvent(e)
        self._fit_columns()

    def show_export(self, ctl: Ctl, note: str, folder: str) -> None:
        MainWindow._apply(self.b_export, ctl)
        if self.export_note.text() != note:
            self.export_note.setText(note)
        if self.export_note.isHidden() == bool(note):
            self.export_note.setVisible(bool(note))
        self._folder = folder
        if self.b_folder.isHidden() == bool(folder):
            self.b_folder.setVisible(bool(folder))

    def show_view(self, v: ProblemsView) -> None:
        if self.flood.text() != v.flood:
            self.flood.setText(v.flood)
        if self.flood.isHidden() == bool(v.flood):
            self.flood.setVisible(bool(v.flood))
        if self.empty.isHidden() == (not v.rows):
            self.empty.setVisible(not v.rows)
        self.model.set_rows(v.rows)
        self._show_detail()

    def _show_detail(self) -> None:
        """The picked row in full (selectable text), under the table."""
        picked = self.table.selectionModel().selectedRows()
        r = self.model.rows[picked[0].row()] if picked else None
        text = (
            TEXT["problems_detail"].format(
                time=r.time, device=r.device or "—", code=r.code, what=r.what, why=r.why,
                fix=r.fix,
            )
            if r is not None
            else ""
        )  # fmt: skip
        if self.detail.text() != text:
            self.detail.setText(text)
        if self.detail.isHidden() == bool(text):
            self.detail.setVisible(bool(text))


def _open_local(path: str) -> None:
    """Default browser / file manager (F15). Fails visibly in the OS, never blocks."""
    url = QUrl(path) if path.startswith(("http://", "https://")) else QUrl.fromLocalFile(path)
    QDesktopServices.openUrl(url)


class ResultDialog(QDialog):
    """03 F15 result popup: verdict and why in color, how long, where, three choices.
    Buttons follow 11-ui 1.11 (bottom right, the main choice at the right end)."""

    def __init__(self, v: ResultView, opener: Callable[[str], None], parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(TEXT["app_title"])
        self.view, self._open = v, opener
        lay = QVBoxLayout(self)
        lay.setContentsMargins(3 * GRID, 3 * GRID, 3 * GRID, 2 * GRID)
        lay.setSpacing(2 * GRID)
        head = QHBoxLayout()
        cell = live_cell(v.color)
        icon = QLabel(cell.icon)
        icon.setFixedWidth(4 * GRID)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(f"background:{cell.color}; color:{cell.fg}; border-radius:4px;")
        self.title = QLabel(v.title)
        f = QFont(self.font())
        f.setPointSize(FONT_PT["title"])
        f.setBold(True)
        self.title.setFont(f)
        self.title.setWordWrap(True)
        head.addWidget(icon)
        head.addWidget(self.title, 1)
        lay.addLayout(head)
        lay.addWidget(QLabel(v.body))
        lay.addWidget(QLabel(TEXT["report_saved"]))
        path_row = QHBoxLayout()
        self.path = Elided(v.folder)
        self.b_copy = QPushButton(TEXT["copy_path"])
        path_row.addWidget(self.path, 1)
        path_row.addWidget(self.b_copy)
        lay.addLayout(path_row)
        lay.addWidget(QLabel(TEXT["see_now"]))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.b_later = QPushButton(TEXT["later"])
        self.b_folder = QPushButton(TEXT["open_folder"])
        self.b_show = QPushButton(TEXT["show_report"])
        self.b_show.setObjectName("primary")
        self.b_show.setDefault(True)
        for b in (self.b_later, self.b_folder, self.b_show):
            buttons.addWidget(b)
        lay.addLayout(buttons)
        self.b_copy.clicked.connect(lambda: QApplication.clipboard().setText(v.folder))
        self.b_later.clicked.connect(self.reject)
        self.b_folder.clicked.connect(lambda: self._choose(v.folder))
        self.b_show.clicked.connect(lambda: self._choose(v.report))
        self.setMinimumWidth(60 * GRID)

    def _choose(self, path: str) -> None:
        self._open(path)
        self.accept()


class WheelGuard(QObject):
    """A dropdown or number box not clicked yet does not take the mouse wheel: the wheel
    scrolls the panel (2026-10-22: scrolling the Test panel changed "Without the
    Controller" to "Stop after 10 s" under the pointer, nobody saw it)."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if (event.type() == QEvent.Type.Wheel and isinstance(obj, QComboBox | QAbstractSpinBox)
                and not obj.hasFocus()):  # fmt: skip
            area = obj.parentWidget()
            while area is not None and not isinstance(area, QAbstractScrollArea):
                area = area.parentWidget()
            if area is not None:
                QApplication.sendEvent(area.viewport(), event)
            return True
        return False


class MainWindow(QMainWindow):
    def __init__(
        self,
        port: int,
        feed_interval_s: float = 1.0,
        ask: Callable[[str, str], bool] | None = None,
        tell: Callable[[str, str], None] | None = None,
        show_result: Callable[[ResultView], None] | None = None,
        opener: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._wheel_guard = WheelGuard(self)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._wheel_guard)
        self.q: queue.Queue[Message] = queue.Queue()
        self.store = Store()
        self.feed = Feed(port, self.q, feed_interval_s)
        self.setup_feed = SetupFeed(port, self.q)  # F14 choice, roles, settings data
        self.commands = Commands(port, self.q, self._after_command)
        self.idle = IdleCheck(port, self.q)  # F17 light check: Start grey with a reason
        self._check_key = ""
        self._ask = ask or self._ask_modal
        self._tell = tell or self._tell_modal
        self._show_result = show_result or self._result_modal
        self._open: Callable[[str], None] = opener or _open_local
        self._drawn = -1
        self._rows: dict[str, DeviceRow] = {}
        self._setup_rows: dict[str, SetupRow] = {}
        self._adv: dict[str, tuple[QLabel, QWidget]] = {}
        self._adv_module = ""  # the test type the Advanced rows were made for
        self._sources_for: str | None = None  # S-48 list read for this sender, at this time
        self._sources_at = float("-inf")
        self._making_at = float("-inf")  # the picked video's making state, last read
        self._start_after_broadcast = False  # Start again once the broadcast stopped
        self._stop_sent_at = time.monotonic()  # when Stop was sent (the wait it shows)
        self._asked_making: tuple[Any, ...] | None = None  # the choice last asked to be made
        self._edits = 0
        self._modal_open = False  # one modal at a time (1.6)
        self._closing_told = False
        self._started = False
        self.setWindowTitle(TEXT["app_title"])
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(100)  # a click shows within 0.1 s (1.14)
        self.timer.timeout.connect(self.drain)

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._started = True
        self.feed.start()
        self.commands.start()
        self.setup_feed.start()
        self.idle.start()
        self.timer.start()
        self.redraw()

    def shutdown(self, wait_s: float = 5.0) -> None:
        self.timer.stop()
        if self._started:  # safe twice and before start (closeEvent + app exit)
            self._started = False
            for worker in (self.feed, self.commands, self.setup_feed, self.idle):
                worker.stop(wait_s)

    def closeEvent(self, e: QCloseEvent) -> None:
        """Closing the screen never stops a test: it runs in the service (12 1-0)."""
        if self.store.run_state != "IDLE" and not self._closing_told:
            self._closing_told = True
            self._tell(TEXT["close_title"], TEXT["close_body"])
        # never wait for a worker: one may be asking a PC that does not answer, and the
        # window would hang for seconds (2026-10-04 the user had to kill it, GAP-116)
        self.shutdown(wait_s=0)
        e.accept()

    # ---------------------------------------------------------------- building

    def _build(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)
        self._outer = outer
        outer.setContentsMargins(2 * GRID, 2 * GRID, 2 * GRID, 2 * GRID)
        outer.setSpacing(2 * GRID)

        top = QHBoxLayout()  # screen-wide actions: top right (1.11)
        self.top = top
        self.l_broadcast = QLabel()  # F18 banner, with its button; only while on
        self.l_broadcast.setStyleSheet("font-weight:bold;")
        self.b_broadcast = QPushButton(TEXT["stop_broadcast"])
        self.b_broadcast.clicked.connect(self.on_stop_broadcast)
        top.addWidget(self.l_broadcast)
        top.addWidget(self.b_broadcast)
        top.addStretch(1)
        self.b_actions = QPushButton()  # F22 badge; hidden while nothing waits
        self.b_actions.setVisible(False)
        top.addWidget(self.b_actions)
        self.b_problems = QPushButton()  # F21 badge; hidden while nothing is open
        self.b_problems.setVisible(False)
        top.addWidget(self.b_problems)
        self.b_reset = QPushButton(TEXT["reset_all"])
        self.b_storage = QPushButton("Storage")  # 20 GAP-82: ⚠ Storage low when short
        self.b_reports = QPushButton(TEXT["reports"])
        self.b_live = QPushButton(TEXT["live_view"])  # live monitor (02 12 /live) (GAP-119)
        self._help(self.b_live, TEXT["tip_live_view"].format(port=WEB_PORT))
        self.b_last = QPushButton(TEXT["last_report"])
        self.b_help = QPushButton(TEXT["help"])  # 08 2: the guide, at this test's part
        self._help(self.b_help, TEXT["tip_help"])
        for b in (self.b_reset, self.b_storage, self.b_live, self.b_reports, self.b_last,
                  self.b_help):  # fmt: skip
            top.addWidget(b)
        outer.addLayout(top)
        self.l_too_small = QLabel(TEXT["screen_too_small"])  # 1.8: below 1024x600 usable
        self.l_too_small.setVisible(False)
        outer.addWidget(self.l_too_small)

        # S6 fault screen (11-ui 2, 03 F11): over the main screen while FAULT
        self.g_fault = QGroupBox(TEXT["fault_title"])
        self.g_fault.setStyleSheet(f"QGroupBox {{ border: 2px solid {COLORS['red']}; }}")
        fv = QVBoxLayout(self.g_fault)
        self.fault_what = QLabel()
        self.fault_what.setWordWrap(True)
        self.fault_data = QLabel()
        self.fault_data.setWordWrap(True)
        fv.addWidget(self.fault_what)
        fv.addWidget(self.fault_data)
        self.fault_devices = QVBoxLayout()
        fv.addLayout(self.fault_devices)
        self._fault_rows: dict[str, tuple[QWidget, QLabel, QPushButton]] = {}
        fb = QHBoxLayout()
        fb.addStretch(1)
        self.b_fault_shutdown = QPushButton(TEXT["fault_shutdown"])
        self.b_fault_shutdown.setToolTip(TEXT["fault_shutdown_tip"])
        self.b_fault_reset = QPushButton(TEXT["reset_all"])
        fb.addWidget(self.b_fault_shutdown)
        fb.addWidget(self.b_fault_reset)  # the right end: the action this screen is for
        fv.addLayout(fb)
        self.g_fault.setVisible(False)
        self.fault_scroll = _scroll(self.g_fault)  # many devices: scroll, never push (1.8)
        self.fault_scroll.setVisible(False)
        outer.addWidget(self.fault_scroll)

        self.body = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.body.setSpacing(2 * GRID)
        outer.addLayout(self.body, 1)

        # Test
        self.g_test = QGroupBox(TEXT["group_test"])
        self.form = QGridLayout()
        self.form.setHorizontalSpacing(2 * GRID)
        self.form.setVerticalSpacing(GRID)
        self.c_type = QComboBox()
        for label in TEST_TYPES.values():  # S-01 screen values
            self.c_type.addItem(label, label)
        self.c_quality = QComboBox()
        for k, v in QUALITY_LABELS.items():
            self.c_quality.addItem(v, k)
        self.sources = SourcesBox()  # S-48 under Video quality: picked every test (GAP-115)
        self.l_sources = self._ilabel("source_video")
        self._help(self.sources, TEXT["tip_source_video"])
        self.c_duration = QComboBox()
        for s in DURATIONS_S:
            self.c_duration.addItem(hms(s).replace(" 00m", "").replace(" 00s", ""), s)
        # 2026-10-09 user: "you set any time, I cannot type one in the app": pick from the
        # list or type one (40m, 1h 30m, 100h); a typed time is checked, never guessed
        self.c_duration.setEditable(True)
        self.c_duration.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        # 2026-10-17 user ("test type이 iptv가 아니면, video quality, source video, duration
        # 대신 iperf관련한 걸 보여주는게 맞지"): a test with its own settings shows them here,
        # editable, with what Start will run; the video fields hide
        self.l_quality = self._ilabel("video_quality", "tip_quality")
        self.l_duration = self._ilabel("duration")
        self.g_mod = QWidget()
        mod_box = QVBoxLayout(self.g_mod)
        mod_box.setContentsMargins(0, 0, 0, 0)
        self.mod_form = QGridLayout()
        self.mod_form.setHorizontalSpacing(2 * GRID)
        self.mod_form.setVerticalSpacing(GRID)
        mod_box.addLayout(self.mod_form)
        self._wifi_asked: set[str] = set()
        self._tablet_scans: dict[str, float] = {}  # "device|tablet:serial" -> when asked
        self.l_preview = QLabel()
        self.l_preview.setWordWrap(True)
        self.l_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mod_box.addWidget(self.l_preview)
        self.g_mod.setVisible(False)
        self._mod: dict[str, tuple[QLabel, QWidget]] = {}
        self._mod_module = ""
        self.x_reports = QCheckBox(TEXT["send_reports"])
        self.x_reports.setChecked(True)
        self.x_advanced = QCheckBox(TEXT["advanced"])
        self.g_adv = QWidget()  # F14: filled from the settings data (16 1), shown when ticked
        self.adv_form = QGridLayout(self.g_adv)
        self.adv_form.setContentsMargins(0, 0, 3 * GRID, 0)  # room for the scroll bar
        self.adv_form.setHorizontalSpacing(2 * GRID)
        self.adv_form.setVerticalSpacing(GRID)
        self.g_adv.setVisible(False)
        # (label, field) pairs; one column normally, two in the narrow layout (1.8)
        self.fields: list[tuple[QWidget | None, QWidget]] = [
            (self._ilabel("test_type"), self.c_type),
            (self.l_quality, self.c_quality),
            (self.l_sources, self.sources),
            (self.l_duration, self.c_duration),
            (None, self.g_mod),
            (None, self.x_reports),
            (None, self.x_advanced),
        ]
        for w, key in ((self.c_type, "tip_test_type"), (self.c_quality, "tip_quality"),
                       (self.c_duration, "tip_duration"), (self.x_reports, "tip_send_reports"),
                       (self.x_advanced, "tip_advanced")):  # fmt: skip
            self._help(w, TEXT[key])
        self._flow(1)
        self.b_start = QPushButton("Start")
        self.b_start.setObjectName("primary")
        # 2026-10-30 user ("왜 마우스 드래그해서 내리면 start가 눌리니"): the Devices rows are
        # drawn again every few seconds; the focus of a removed field fell on Start. Start
        # takes no focus (no key presses it) and asks before a test begins.
        self.b_start.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.progress = QLabel()
        self.progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress.setFont(_tabular(self.font()))
        self.run_box = QVBoxLayout()  # Start/Stop + progress: always on screen (1.8)
        self.run_box.addWidget(self.b_start)
        self.run_box.addWidget(self.progress)
        self.tv = QVBoxLayout(self.g_test)
        self.tv.addLayout(self.form)
        self.adv_scroll = QScrollArea()  # many Advanced items: scroll, never squeeze (1.11)
        self.adv_scroll.setWidgetResizable(True)
        self.adv_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.adv_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.adv_scroll.setWidget(self.g_adv)
        self.adv_scroll.setVisible(False)
        self.tv.addWidget(self.adv_scroll, 8)  # most of the room when shown (1.11)
        self.tv.addStretch(1)

        # Devices + 5-min results
        right = QVBoxLayout()
        right.setSpacing(2 * GRID)
        self.g_devices = QGroupBox(TEXT["group_devices"])
        self.devices_box = QVBoxLayout(self.g_devices)
        self.devices_box.setSpacing(0)
        self.no_devices = QLabel(TEXT["no_devices"])
        self.devices_box.addWidget(self.no_devices)
        self.devices_box.addStretch(1)
        # 2026-10-31 user ("refresh버튼이라도 만들어 놓든가"): the PCs and the tablets on their
        # USB are read every few seconds; Refresh reads them now (a tablet just plugged in)
        self.b_refresh = QPushButton(TEXT["refresh"])
        self.b_refresh.setToolTip(TEXT["refresh_tip"])
        self.b_refresh.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.b_refresh.clicked.connect(self._refresh_devices)
        self.b_add_linux = QPushButton(TEXT["add_linux"])
        self.b_add_linux.setToolTip(TEXT["add_linux_tip"])
        self.b_add_linux.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.b_add_linux.clicked.connect(self._add_linux)
        bar = QHBoxLayout()
        bar.addStretch(1)
        bar.addWidget(self.b_add_linux)
        bar.addWidget(self.b_refresh)
        self.devices_box.addLayout(bar)
        self.g_cells = QGroupBox(TEXT["group_cells"])
        cv = QVBoxLayout(self.g_cells)
        self.grid = CellGrid()
        cv.addWidget(self.grid)
        self.cases = QTableWidget()  # a test without 5-minute boxes: one line per case
        self.cases.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.cases.verticalHeader().setVisible(False)
        self.cases.setVisible(False)
        self.no_cases = QLabel(TEXT["no_cases"])
        self.no_cases.setVisible(False)
        cv.addWidget(self.cases, 1)
        cv.addWidget(self.no_cases)
        self._cases_key: tuple[Any, ...] = ()
        self.b_follow = QPushButton(TEXT["follow_live"])  # area action: bottom right
        fl = QHBoxLayout()
        fl.addStretch(1)
        fl.addWidget(self.b_follow)
        cv.addLayout(fl)
        right.addWidget(self.g_devices, 1)
        right.addWidget(self.g_cells, 1)
        self.right = QWidget()
        self.right.setLayout(right)
        self.right_scroll = QScrollArea()
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.right_scroll.setWidget(self.right)

        self.left_scroll = _scroll(self.g_test)  # a short screen scrolls it (1.8)
        # Start/Stop under the scrolled Test area, not in it: a test with many settings of
        # its own (Throughput, 2026-10-17) scrolled it off the screen (1.8)
        self.left = QWidget()
        self.left_box = QVBoxLayout(self.left)
        self.left_box.setContentsMargins(0, 0, 0, 0)
        self.left_box.addWidget(self.left_scroll, 1)
        self.left_box.addLayout(self.run_box)
        self.body.addWidget(self.left, 0)
        self.body.addWidget(self.right_scroll, 1)

        # Status
        self.g_status = QGroupBox(TEXT["group_status"])
        sv = QVBoxLayout(self.g_status)
        sl = QHBoxLayout()
        self.status_icon = QLabel()
        self.status_icon.setFixedWidth(3 * GRID)
        self.status_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status = Elided()
        self._status_font = QFont(self.status.font())
        self._verdict_font = QFont(self._status_font)  # the finished verdict line (GAP-106)
        self._verdict_font.setBold(True)
        self._verdict_font.setPointSize(FONT_PT["title"])
        sl.addWidget(self.status_icon)
        sl.addWidget(self.status, 1)
        sv.addLayout(sl)
        self.checklist = QLabel()  # F17 steps while starting or after a failed check
        self.checklist.setWordWrap(True)
        self.checklist.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.checklist.setVisible(False)
        sv.addWidget(self.checklist)
        # 2026-10-17: a PC on another build -- the fix was a command line ("zeroloss deploy")
        self.b_update = QPushButton()
        self.b_update.setToolTip(TEXT["update_pcs_tip"])
        self.b_update.setVisible(False)
        self.b_update.clicked.connect(self.on_update_pcs)
        up = QHBoxLayout()
        up.addWidget(self.b_update)
        up.addStretch(1)
        sv.addLayout(up)
        outer.addWidget(self.g_status)
        # below a usable 1024x600 the whole screen scrolls, the window still fits (1.8);
        # above it the page is as big as the window and no scroll bar shows
        self.page = QScrollArea()
        self.page.setObjectName("page")
        self.page.setWidgetResizable(True)
        self.page.setFrameShape(QScrollArea.Shape.NoFrame)
        self.page.setWidget(root)
        self.setCentralWidget(self.page)

        self.b_start.clicked.connect(self.on_start_stop)
        self.b_reset.clicked.connect(self.on_reset)
        self.b_fault_reset.clicked.connect(self.on_reset)
        self.b_fault_shutdown.clicked.connect(self.on_shutdown)
        self.x_advanced.toggled.connect(self.adv_scroll.setVisible)  # view only, values stay
        self.b_last.clicked.connect(self.on_last_report)
        self.b_reports.clicked.connect(self.on_reports)
        self.b_live.clicked.connect(lambda: self._open(f"http://127.0.0.1:{WEB_PORT}/live"))
        self.b_help.clicked.connect(lambda: self._open(self.help_url()))
        self.b_storage.clicked.connect(self.on_storage)
        self.storage_window: StorageWindow | None = None
        self.reports_window: ReportsWindow | None = None
        self._storage_note = ""
        self._deleted_seen: object = None  # the last deletion answer already handled
        self.b_problems.clicked.connect(self.on_problems)
        self.b_actions.clicked.connect(self.on_actions)
        self.actions_window: ActionsWindow | None = None
        self._actions_seen: frozenset[str] = frozenset()
        self.problems_window: ProblemsWindow | None = None
        self._errors_seen: set[str] = set()  # presentation only: which errors blinked
        self.blink = QTimer(self)
        self.blink.setInterval(500)
        self.blink.timeout.connect(self._blink_step)
        self._blinks_left = 0
        self.c_type.activated.connect(lambda _: self.edit(
            test_type=self.c_type.currentData(),
            module=module_of(str(self.c_type.currentData()))))  # fmt: skip
        self.c_quality.activated.connect(lambda _: self.edit(quality=self.c_quality.currentData()))
        dur_edit = self.c_duration.lineEdit()
        if dur_edit is not None:
            dur_edit.editingFinished.connect(self._duration_typed)
        self.c_duration.activated.connect(
            lambda _: self.edit(duration_s=self.c_duration.currentData())
        )
        self.resize(1280, 760)

    def _ilabel(self, key: str, tip: str = "") -> QLabel:
        """Every setting has an ⓘ help tooltip: what, example, where (docs/11-ui.md)."""
        lab = QLabel(f"{TEXT[key]} ⓘ")
        lab.setToolTip(TEXT[tip or f"tip_{key}"])
        return lab

    @staticmethod
    def _help(w: QWidget, text: str) -> None:
        """The ⓘ help of an input: kept by ``_apply`` under any reason (11-ui 1.10)."""
        w.setProperty("help", text)
        _set_tip(w, text)

    # ---------------------------------------------------------------- queue -> store

    def drain(self) -> None:
        changed = False
        busy_broadcast = False
        while True:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break
            changed = self.store.apply(msg) or changed
            if msg[0] == "done" and isinstance(msg[1], Done):
                busy_broadcast = self._after(msg[1]) or busy_broadcast
        self.setup_feed.between_tests(self.store.run_state == "IDLE")
        key = self.store.check_key
        if key != self._check_key:  # plan or devices changed: check again now (F17)
            self._check_key = key
            self.idle.want(key, self.store.plan)
        if changed or self._drawn != self.store.version:
            self.redraw()
        if busy_broadcast and not self._modal_open:
            self._offer_stop_broadcast()
        if not self._modal_open:  # one modal at a time (1.6); waits for its turn
            result = self.store.take_finished()
            if result is not None:
                self._show_result(result_view(result, self.store.status.reports_dir))

    def _after(self, d: Done) -> bool:
        """What an answer leads to: True when Start was refused because the broadcast
        that is on sends something else (RUN-014, F18)."""
        if d.name == "broadcast" and d.ok and self._start_after_broadcast:
            self._start_after_broadcast = False
            self.commands.send("run", f"{P}/run", timeout_s=START_TIMEOUT_S)
            self.q.put(("sent", "run"))
            return False
        if d.name == "broadcast":
            self._start_after_broadcast = False
        if d.name == "stop" and not d.ok:  # never silent (2026-10-09, "Stop does not stop")
            err = d.error or {}
            self._tell(TEXT["stop_failed_title"], TEXT["stop_failed_body"].format(
                what=err.get("what") or "no answer", why=err.get("why") or ""))  # fmt: skip
        if d.name == "install_linux":  # never silent: added, or why not
            self.b_add_linux.setEnabled(True)
            self.b_add_linux.setText(TEXT["add_linux"])
            if d.ok:
                r = d.data or {}
                tools = ", ".join(r.get("tools_missing") or [])
                more = TEXT["linux_missing"].format(tools=tools) if tools else ""
                self._tell(TEXT["linux_done_title"], TEXT["linux_done_body"].format(
                    name=r.get("name", ""), host=r.get("host", ""), missing=more))  # fmt: skip
                self._refresh_devices()
            else:
                err = d.error or {}
                self._tell(TEXT["linux_failed_title"], TEXT["linux_failed_body"].format(
                    what=err.get("what") or "no answer", why=err.get("why") or "",
                    fix=err.get("fix") or ""))  # fmt: skip
        if d.name.startswith(("remove_linux:", "remove_tablet:")):  # never silent
            what = d.name.split(":", 1)[1]
            if d.ok:
                body = (TEXT["removed_pc"].format(name=what) if d.name.startswith("remove_linux")
                        else TEXT["removed_tablet"].format(serial=what))  # fmt: skip
                self._tell(TEXT["removed_title"], body)
                self._refresh_devices()
            else:
                err = d.error or {}
                self._tell(TEXT["remove_failed_title"], TEXT["remove_failed_body"].format(
                    what=err.get("what") or "no answer", why=err.get("why") or "",
                    fix=err.get("fix") or ""))  # fmt: skip
        if d.name.startswith("address:") and not d.ok:  # GAP-153: never silent
            err = d.error or {}
            self._tell(TEXT["address_failed_title"], TEXT["address_failed_body"].format(
                what=err.get("what") or "no answer", why=err.get("why") or ""))  # fmt: skip
        code = str((d.error or {}).get("code", ""))
        return (
            d.name == "run"
            and not d.ok
            and code.endswith("RUN-014")
            and bool(self.store.status.broadcast.get("on"))
        )

    def _offer_stop_broadcast(self) -> None:
        """GAP-116: after a Stop the Always-on broadcast still sent the old movie, and
        Start with a new movie only turned red — the user saw "Start does nothing". Now
        one question: stop the broadcast and start this test."""
        if self._ask(TEXT["start_over_broadcast_title"], TEXT["start_over_broadcast_body"]):
            self._start_after_broadcast = True
            self.commands.send("broadcast", f"{P}/broadcast", timeout_s=60, on=False)

    @property
    def failed_text(self) -> str:
        d = self.store.last_done
        return str((d.error or {}).get("what", "")) if d is not None and not d.ok else ""

    # ---------------------------------------------------------------- drawing

    def redraw(self) -> None:
        """Layout waits until every part is set: a moment where an old button is still
        shown next to a new one would grow the window past the screen, and a window never
        shrinks back by itself (1.8)."""
        lay = self._outer
        lay.setEnabled(False)
        try:
            self._redraw()
        finally:
            lay.setEnabled(True)
            lay.activate()

    def _redraw(self) -> None:
        self._drawn = self.store.version
        st = self.store.status
        choice = self.store.choice
        choice = shown_choice(choice, self.store.status.run, self.store.run_state)
        _, why = self.store.precheck()
        ctl = self.store.screen_controls()
        self._apply(self.b_start, ctl["start_stop"])
        self._apply(self.c_type, ctl["test_type"])
        self._apply(self.c_quality, ctl["quality"])
        self._apply(self.c_duration, ctl["duration"])
        self._apply(self.x_reports, ctl["send_reports"])
        self._apply(self.x_advanced, ctl["advanced_toggle"])
        self._apply(self.b_follow, ctl["follow_live"])
        self._apply(self.b_reports, ctl["reports"])
        # between tests only: while one runs the top bar is full at 1280 px (11-ui 1.8),
        # and the guide is in the Start menu too
        idle = self.store.run_state == "IDLE"
        if self.b_help.isHidden() == idle:
            self.b_help.setVisible(idle)
        self._apply(self.b_live, ctl["live_view"])
        self._apply(self.b_storage, ctl["storage"])
        self._apply(self.b_last, ctl["last_report"])
        self._apply(self.b_reset, ctl["reset_all"])
        self._apply(self.b_broadcast, ctl["stop_broadcast"])
        bc = st.broadcast if st.connected else {}
        if bc.get("on"):
            n = len(bc.get("channels") or [])
            _set_text(self.l_broadcast, TEXT["broadcast_on"].format(n=n))
            _set_tip(self.l_broadcast, TEXT["broadcast_tip"].format(since=bc.get("since_utc")))
        if self.l_broadcast.isHidden() == bool(bc.get("on")):
            self.l_broadcast.setVisible(bool(bc.get("on")))
        self._apply(self.b_fault_reset, ctl["reset_all"])
        self._apply(self.b_fault_shutdown, ctl["fault_shutdown"])
        self._render_fault(fault_view(st), ctl["fault_restart"])
        self._select(self.c_type, choice.get("test_type", "IPTV test"))
        self._select(self.c_quality, choice.get("quality", "UHD"))
        self._select(self.c_duration, choice.get("duration_s"))
        self._disable_items(self.c_type, self.store.schema.get("S-01", {}))
        adv = {sid for sid, _ in adv_items(self.store.schema)}
        n = len([sid for sid in choice.get("settings") or {} if sid in adv])
        text = TEXT["advanced_changed"].format(n=n) if n else TEXT["advanced"]
        if self.x_advanced.text() != text:
            self.x_advanced.setText(text)
        self._render_mod(ctl["advanced_values"])
        self._render_adv(ctl["advanced_values"])
        self._render_main_sources(ctl["quality"])
        _set_text(self.progress, progress(st))
        idle = self.store.run_state == "IDLE"
        self._render_devices(device_rows(st) if not idle else [], ctl["channel"])
        busy = {k for k in self._wifi_asked if self.commands.busy(f"wifi_scan:{k}")}
        # a tablet's Scan: shown until the tablet list read after it is in (at most 20 s)
        now = time.monotonic()
        got_at = self.store.setup_at
        for k, t in list(self._tablet_scans.items()):
            if now - t > TABLET_SCAN_S or got_at > t + 1.0:
                del self._tablet_scans[k]
        busy |= set(self._tablet_scans)
        rows = (setup_rows(self.store.setup, card_links(st), self.store.setting(WIFI),
                           self._wifi_scans(), busy) if idle else [])  # fmt: skip
        self._render_setup(rows, ctl["devices"])
        own = str(self.store.choice.get("module") or "iptv") != "iptv"
        if self.grid.isHidden() != own:
            self.grid.setVisible(not own)
        title = TEXT["group_cases" if own else "group_cells"]
        if self.g_cells.title() != title:
            self.g_cells.setTitle(title)
        if own:
            self._render_cases(st)
        else:
            for w in (self.cases, self.no_cases):
                if not w.isHidden():
                    w.setVisible(False)
        self.grid.set_rows(cell_rows(st))
        color, text = status_line(st, self.store.plan_ok, self.failed_text, why)
        if "stop" in self.store.pending and self.store.run_state == "RUNNING":
            # 2026-10-09 user: "Stop does not stop — show a wait message or a popup": the
            # test finishes its current step first; say so, with the time it has taken
            waited = int(time.monotonic() - self._stop_sent_at)
            color, text = "grey", TEXT["status_stop_waiting"].format(s=waited)
        cell = live_cell(color)
        _set_text(self.status_icon, cell.icon)
        self.status_icon.setStyleSheet(
            f"background:{cell.color}; color:{cell.fg}; border-radius:4px;"
        )
        result = st.run.get("result")
        verdict = isinstance(result, dict) and text == finished_line(result)[1]
        font = self._verdict_font if verdict else self._status_font
        if self.status.font() != font:
            self.status.setFont(font)
        self.status.set_full(text)
        lines = "\n".join(checklist_lines(st, self.store.current_idle_check()))
        _set_text(self.checklist, lines)
        if self.checklist.isHidden() == bool(lines):
            self.checklist.setVisible(bool(lines))
        self._render_update(st)
        self._render_problems(problems_view(st))
        self._render_actions(actions_view(st))
        self._render_export(ctl["export_diagnostics"])
        self._render_storage()
        self._render_reports()

    # ---------------------------------------------------------------- F14 choice

    def _after_command(self) -> None:  # worker thread: only wakes the readers
        self.feed.poke()
        self.setup_feed.poke()

    def edit(self, **changes: Any) -> None:
        """A screen change: shown at once through the store, then saved in the service
        (02 2-11) in click order. Refused when a test runs (decided when handled)."""
        if not self.store.apply(("edit", changes)):
            return
        self._edits += 1
        self.commands.send(f"setup:{self._edits}", f"{P}/setup", changes=changes)
        self.redraw()

    def edit_setting(self, sid: str, value: Any) -> None:
        changed = dict(self.store.choice.get("settings") or {})
        changed[sid] = value
        self.edit(settings=changed)
        if sid in (SOURCES, LENGTH):  # a movie or its length picked: make it now (GAP-116)
            self._prepare_now()

    def _refresh_devices(self) -> None:
        self.setup_feed.poke()
        self.b_refresh.setEnabled(False)
        self.b_refresh.setText(TEXT["refreshing"])

        def done() -> None:
            self.b_refresh.setEnabled(True)
            self.b_refresh.setText(TEXT["refresh"])

        QTimer.singleShot(3000, done)  # a tablet list takes a few seconds per PC

    def _add_linux(self) -> None:
        """A Linux PC registered from the screen (06 11.3): IP, user, password once (sent
        to the local service and dropped there, never kept here), name, auto login."""
        got = LinuxDialog(self).ask()
        if got is None:
            return
        if self.commands.send("install_linux", f"{P}/install/linux",
                              timeout_s=INSTALL_LINUX_TIMEOUT_S, **got):  # fmt: skip
            self.b_add_linux.setEnabled(False)
            self.b_add_linux.setText(TEXT["add_linux_busy"].format(host=got["host"]))
            self.q.put(("sent", "install_linux"))

    def _start_when_saved(self, tries: int) -> None:
        """Start once every change made on screen is saved (the service starts from the
        saved choice); waits at most ``tries`` x 0.1 s."""
        if self.commands.busy_prefix("setup:") and tries > 0:
            QTimer.singleShot(100, lambda: self._start_when_saved(tries - 1))
            return
        if self.commands.send("run", f"{P}/run", timeout_s=START_TIMEOUT_S):  # saved choice
            self.q.put(("sent", "run"))
            self.drain()

    def help_url(self) -> str:
        """The guide's part for the test chosen now (08 2: Help opens the right part)."""
        su = (self.store.setup or {}).get("setup") or {}
        part = "tput" if module_of(str(su.get("test_type") or "")) == "throughput" else "run"
        return f"http://127.0.0.1:{WEB_PORT}/guide#{part}"

    def _start_what(self) -> str:
        """The test the saved choice runs, in one line (the Start question)."""
        su = (self.store.setup or {}).get("setup") or {}
        return start_summary(self.store.plan, str(su.get("test_type") or "the test"))

    def _render_setup(self, rows: list[SetupRowView], ctl: Ctl) -> None:
        # 2026-10-30 user ("태블릿 설정을 바꾸면 자꾸 화면이 위로 올라가"): the rows are drawn
        # again; the scroll stays where the person left it
        bar = self.right_scroll.verticalScrollBar()
        kept = bar.value()
        self._render_setup_rows(rows, ctl)
        QTimer.singleShot(0, lambda: bar.setValue(min(kept, bar.maximum())))

    def _render_setup_rows(self, rows: list[SetupRowView], ctl: Ctl) -> None:
        names = [r.name for r in rows]
        for name in list(self._setup_rows):
            if name not in names:
                w = self._setup_rows.pop(name)
                self.devices_box.removeWidget(w)
                w.hide()  # never drawn under the new rows while it waits
                w.deleteLater()
        for i, r in enumerate(rows):
            row = self._setup_rows.get(r.name)
            if row is None:
                n = r.name
                row = self._setup_rows[n] = self._new_setup_row(n)
                self.devices_box.insertWidget(i, row)
            row.show_view(r, ctl)
        self.no_devices.setVisible(not rows and not self._rows)

    def _new_setup_row(self, name: str) -> SetupRow:
        def on_role(role: str) -> None:
            self._on_role(name, role)

        def on_ch(ch: int) -> None:
            self._on_receiver(name, ch=ch)

        def on_net(net: str) -> None:
            self._on_receiver(name, network=net)

        def on_card(ip: str) -> None:
            if self.store.setup is not None:
                self.edit(**card_change(self.store.setup, name, ip))

        def on_tick(nic: str, on: bool) -> None:
            lines = self._lines_of(name)
            tablet = nic.startswith(TABLET_CARD)  # a tablet line: its own, never in the way
            wifi = any(line.nic == nic and line.wifi for line in lines) and not tablet
            # one Wi-Fi card per PC: ticking one unticks the other (2026-10-17 user)
            drop = tuple(x.nic for x in lines if x.wifi and x.nic != nic
                         and not x.nic.startswith(TABLET_CARD)) if wifi else ()  # fmt: skip
            self.edit_setting(WIFI, ticks_change(self.store.setting(WIFI), name, nic, tick=on,
                                                 drop=drop))  # fmt: skip
            if on and wifi:
                self._scan_wifi(name, nic)  # its bands come from what it sees

        def on_tabwifi(ssid: str, security: str) -> None:
            wifi = {"ssid": ssid, "security": security} if ssid else None
            self._on_receiver(name, wifi=wifi)

        def on_band(nic: str, band: str, bssid: str) -> None:
            nets = (self._wifi_scans().get(f"{name}|{nic}") or {}).get("networks") or []
            net = next((n for n in nets if isinstance(n, dict) and n.get("bssid") == bssid),
                       None)  # fmt: skip
            if nic.startswith(TABLET_CARD):  # a tablet's pick is by SSID (its own scan)
                net = self._tablet_net(name, nic[len(TABLET_CARD) :], bssid)
            self.edit_setting(WIFI, ticks_change(self.store.setting(WIFI), name, nic,
                                                 band=band, net=net))  # fmt: skip

        return SetupRow(on_role, on_ch, on_net, on_card,
                        lambda text: self._on_address(name, text),
                        lambda ms: self._on_receiver(name, buffer_ms=ms),
                        on_tick, on_band, lambda nic: self._scan_wifi(name, nic),
                        self._send_password, on_tabwifi,
                        lambda: self._remove(name))  # fmt: skip

    def _remove(self, name: str) -> None:
        """06 7: a tablet row takes the app off that tablet; a PC row takes ZeroLoss off that
        PC over SSH (account and password once, test data kept unless ticked)."""
        if name.startswith(TABLET):
            serial = name[len(TABLET) :]
            sides = (self.store.setup or {}).get("sides") or {}
            host = next((n for n, s in sides.items() if serial in _serials_of(s)), "")
            body = TEXT["remove_tablet_body"].format(serial=serial, host=host)
            if host and self._ask(TEXT["remove_tablet_title"], body):
                self.commands.send(f"remove_tablet:{serial}", f"{P}/remove/tablet_app",
                                   timeout_s=150, device=host, serial=serial)  # fmt: skip
                self.q.put(("sent", f"remove_tablet:{serial}"))
            return
        got = LinuxDialog(self, remove=name).ask()
        if got is not None:
            self.commands.send(f"remove_linux:{name}", f"{P}/remove/linux", timeout_s=300,
                               name=name, user=got["user"], password=got["password"],
                               purge=got["purge"])  # fmt: skip
            self.q.put(("sent", f"remove_linux:{name}"))

    def _send_password(self, ssid: str, password: str) -> None:
        """A band network's password, to the service's encrypted store (never kept here)."""
        self.commands.send(f"wifi_password:{ssid}", f"{P}/wifi_password", ssid=ssid,
                           password=password)  # fmt: skip
        self.q.put(("sent", f"wifi_password:{ssid}"))

    def _tablet_net(self, name: str, serial: str, ssid: str) -> dict[str, Any] | None:
        side = ((self.store.setup or {}).get("sides") or {}).get(name) or {}
        tab: dict[str, Any] = next(
            (t for t in side.get("tablets") or [] if t.get("serial") == serial), {}
        )
        found = next((n for n in tab.get("networks") or [] if n.get("ssid") == ssid), None)
        return {**found, "bssid": ""} if found and ssid else None

    def _lines_of(self, name: str) -> tuple[Any, ...]:
        row = self._setup_rows.get(name)
        return row._lines or () if row is not None else ()

    def _on_address(self, name: str, text: str) -> None:
        """GAP-155: the address picked or typed in the row's IP box. The service uses it
        only when that PC answers there (same device ID); otherwise a popup says who."""
        side = ((self.store.setup or {}).get("sides") or {}).get(name) or {}
        if not text.strip() or text.strip() == str(side.get("host") or ""):
            return
        got = parse_address(text)
        if got is None:
            self._tell(TEXT["address_title"].format(name=name),
                       TEXT["address_bad"].format(text=text))  # fmt: skip
            return
        host, port = got
        self.commands.send(f"address:{name}", f"{P}/devices/address", timeout_s=10,
                           name=name, host=host, port=port)  # fmt: skip

    def _on_role(self, name: str, role: str) -> None:
        if self.store.setup is None:
            return
        if name.startswith(TABLET):
            present = tablets_present(self.store.setup)  # an unplugged one holds no place
            mine = [r for r in (self.store.setup.get("roles") or {}).get("receivers") or []
                    if r.get("serial") and r.get("serial") in present]  # fmt: skip
            if role == "receiver" and len(mine) >= TABLETS_MAX:
                self._tell(TEXT["tablet_new"], TEXT["tablets_full"])
                self.redraw()  # the box goes back to Not used
                return
            self.edit(**tablet_role_change(self.store.setup, name, role))
            return
        self.edit(**role_change(self.store.setup, name, role))

    def _on_receiver(self, name: str, **fields: Any) -> None:
        if self.store.setup is not None:
            self.edit(**receiver_change(self.store.setup, name, **fields))

    def _render_adv(self, ctl: Ctl) -> None:
        """Advanced settings from the settings data (16 1): a control per editable kind,
        the value in use, grey + reason when this build has not got it."""
        module = str(self.store.choice.get("module") or "iptv")
        items = [x for x in adv_items(self.store.schema, module) if x[0] not in self._mod]
        if self._adv and module != self._adv_module:
            # 2026-10-17 user: "설정을 바꾸고 싶을수도 있는데 바꿀 방법도 없고": the rows were
            # made once for the test picked at start (IPTV); Throughput's never showed
            while self.adv_form.count():
                entry = self.adv_form.takeAt(0)
                gone = entry.widget() if entry is not None else None
                if gone is not None:
                    gone.deleteLater()
            self._adv = {}
        self._adv_module = module
        if items and not self._adv:
            row, group = 0, None
            for sid, item in items:
                if item.get("group") != group:  # a titled group of at most 7 (1.11)
                    group = item.get("group")
                    title = QLabel(str(group))
                    font = QFont(title.font())
                    font.setBold(True)
                    title.setFont(font)
                    self.adv_form.addWidget(title, row, 0, 1, 2)
                    row += 1
                label = QLabel(f"{item.get('label')} ⓘ")
                label.setWordWrap(True)  # a long name takes two lines, not the field's room
                widget = self._adv_widget(sid, item)
                _fit_field(widget)
                label.setToolTip(adv_tip(item))
                self._help(widget, adv_help(item))
                self.adv_form.addWidget(label, row, 0)
                self.adv_form.addWidget(widget, row, 1)
                self._adv[sid] = (label, widget)
                row += 1
            self.adv_form.setColumnStretch(0, 1)  # names and fields share the width (GAP-111)
            self.adv_form.setColumnStretch(1, 1)
        quality = str(self.store.choice.get("quality") or "UHD")
        for sid, (_, w) in self._adv.items():
            item = self.store.schema.get(sid) or {}
            value = self.store.setting(sid)
            why = adv_why(item)
            self._apply(w, Ctl(False, tooltip=why) if why else ctl)
            self._show_value(w, item, value, quality)

    def _adv_widget(self, sid: str, item: dict[str, Any]) -> QWidget:
        kind = item.get("kind")
        if kind == "choice":
            box = QComboBox()
            for c in item.get("choices") or []:
                box.addItem(str(c), c)
            self._disable_items(box, item)
            box.activated.connect(lambda _, b=box: self.edit_setting(sid, b.currentData()))
            return box
        if kind in ("int", "per_quality_int"):
            spin = QSpinBox()
            scale = int(item.get("scale") or 1)
            if kind == "int":
                spin.setRange(int(item["min"]), int(item["max"]))
            if item.get("unit"):
                spin.setSuffix(f" {item['unit']}")
            spin.editingFinished.connect(lambda s=spin: self._spin_done(sid, item, s, scale))
            return spin
        if kind == "float":
            dspin = QDoubleSpinBox()
            dspin.setRange(float(item["min"]), float(item["max"]))
            dspin.setSingleStep(float(item.get("step") or 0.1))
            dspin.editingFinished.connect(lambda d=dspin: self.edit_setting(sid, d.value()))
            return dspin
        if kind == "bool":
            box2 = QCheckBox()
            box2.toggled.connect(lambda on: self.edit_setting(sid, on))
            return box2
        return Elided()  # shown, not edited here (groups, pair, text, wifi)

    def _render_cases(self, st: Status) -> None:
        """One line per finished case (Throughput), redrawn only when it changes."""
        heads, rows = case_table(st)
        key = (heads, tuple(rows))
        if key == self._cases_key:
            return
        self._cases_key = key
        has = bool(rows)
        if self.cases.isHidden() == has:
            self.cases.setVisible(has)
        if self.no_cases.isHidden() == (not has):
            self.no_cases.setVisible(not has)
        cols = [TEXT["case_device"], "", *heads, TEXT["case_why"]]
        self.cases.setColumnCount(len(cols))
        self.cases.setHorizontalHeaderLabels(cols)
        self.cases.setRowCount(len(rows))
        for i, r in enumerate(rows):
            cells = [r.device, r.icon, *r.values, r.why]
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip("\n".join(x for x in (text, r.detail) if x))
                if j == 1:
                    item.setForeground(QColor(COLORS.get(r.color, COLORS["grey"])))
                self.cases.setItem(i, j, item)
        self.cases.resizeColumnsToContents()
        self.cases.scrollToBottom()  # the newest case in view

    def _render_mod(self, ctl: Ctl) -> None:
        """A test other than IPTV: its own settings on the Test area in place of Video
        quality, Source video and Duration, and what Start will run (setup ``preview``)."""
        module = str(self.store.choice.get("module") or "iptv")
        own = module != "iptv"
        for w in (self.l_quality, self.c_quality, self.l_duration, self.c_duration):
            if w.isHidden() != own:
                w.setVisible(not own)
        if self.g_mod.isHidden() == own:
            self.g_mod.setVisible(own)
        mine = [(sid, s) for sid, s in adv_items(self.store.schema, module)
                if own and s.get("module") == module]  # fmt: skip
        if module != self._mod_module or [sid for sid, _ in mine] != list(self._mod):
            while self.mod_form.count():
                entry = self.mod_form.takeAt(0)
                gone = entry.widget() if entry is not None else None
                if gone is not None:
                    gone.deleteLater()
            self._mod = {}
            self._mod_module = module
            for row, (sid, item) in enumerate(mine):
                label = QLabel(f"{item.get('label')} ⓘ")
                label.setWordWrap(True)
                widget = self._adv_widget(sid, item)
                _fit_field(widget)
                label.setToolTip(adv_tip(item))
                self._help(widget, adv_help(item))
                self.mod_form.addWidget(label, row, 0)
                self.mod_form.addWidget(widget, row, 1)
                self._mod[sid] = (label, widget)
            self.mod_form.setColumnStretch(0, 1)
            self.mod_form.setColumnStretch(1, 1)
        quality = str(self.store.choice.get("quality") or "UHD")
        for sid, (_, box) in self._mod.items():
            item = self.store.schema.get(sid) or {}
            why = adv_why(item)
            self._apply(box, Ctl(False, tooltip=why) if why else ctl)
            self._show_value(box, item, self.store.setting(sid), quality)
        lines = [str(x) for x in (self.store.setup or {}).get("preview") or []] if own else []
        # the command on the Test area (Start stays on screen); every line on hover, and on
        # the live page and in the report
        short = [x for x in lines if x.startswith("Command:")] or lines[:1]
        _set_text(self.l_preview, "\n".join(short))
        _set_tip(self.l_preview, "\n".join(f"• {x}" for x in lines))

    def _wifi_scans(self) -> dict[str, Any]:
        out = {}
        for name, done in self.store.done_by.items():
            if not name.startswith("wifi_scan:"):
                continue
            if done.ok and isinstance(done.data, dict):
                out[name.removeprefix("wifi_scan:")] = done.data
            elif not done.ok:  # shown under the card: a scan never fails silently
                out[name.removeprefix("wifi_scan:")] = {"error": done.error or {}}
        return out

    def _scan_wifi(self, device: str, nic: str) -> None:
        if nic.startswith(TABLET_CARD):  # a tablet's networks come with the tablet list
            # read again now, "Scanning…" until it is in (2026-10-30 user: "scan눌러도
            # scanning으로 안바뀜")
            self._tablet_scans[f"{device}|{nic}"] = time.monotonic()
            self.setup_feed.poke()
            self.q.put(("sent", f"wifi_scan:{device}|{nic}"))
            return
        key = f"{device}|{nic}"
        if self.commands.read(f"wifi_scan:{key}", f"{P}/wifi_scan", timeout_s=60,
                              node=device, nic=nic):  # fmt: skip
            self._wifi_asked.add(key)
            self.q.put(("sent", f"wifi_scan:{key}"))

    def _render_update(self, st: Status) -> None:
        """Update PCs: shown while a check says a PC has another build (INS-015)."""
        checks = [self.store.current_idle_check(), st.run.get("checklist")]
        codes = {s.get("code") for c in checks if isinstance(c, dict) for s in c.get("steps") or []}
        other = bool(codes & {"ZL-INS-015", "INS-015", "ZL-INS-024", "INS-024"})
        other = other or tablets_behind(self.store.setup, st.build)  # a tablet's app too
        busy = self.commands.busy("update_pcs")
        show = (other or busy) and self.store.run_state == "IDLE"
        text = TEXT["update_pcs_busy"] if busy else TEXT["update_pcs"].format(build=st.build)
        if self.b_update.text() != text:
            self.b_update.setText(text)
        if self.b_update.isEnabled() == busy:
            self.b_update.setEnabled(not busy)
        if self.b_update.isHidden() == show:
            self.b_update.setVisible(show)

    def on_update_pcs(self) -> None:
        if self.commands.send("update_pcs", f"{P}/update_pcs", timeout_s=UPDATE_TIMEOUT_S):
            self.q.put(("sent", "update_pcs"))

    def _render_main_sources(self, ctl: Ctl) -> None:
        """S-48 on the Test area under Video quality (11-ui 1.10, GAP-115): shown when the
        module has it, locked like Video quality."""
        item = self.store.schema.get(SOURCES)
        if item and item.get("module") not in (None, self.store.choice.get("module") or "iptv"):
            item = None  # another test: no video to pick
        if self.sources.isHidden() == bool(item):
            self.sources.setVisible(bool(item))
            self.l_sources.setVisible(bool(item))
        if not item:
            return
        why = adv_why(item)
        self._apply(self.sources, Ctl(False, tooltip=why) if why else ctl)
        quality = str(self.store.choice.get("quality") or "UHD")
        self._render_sources(SOURCES, self.sources, self.store.setting(SOURCES), quality)
        self._render_making(self.sources)

    def _render_sources(self, sid: str, w: SourcesBox, value: Any, quality: str) -> None:
        """One list per channel: Generated + the sender PC's files (02 2-10), a file that
        does not fit the quality grey with why (K-28). Read again every 30 s between
        tests, never on the screen thread."""
        self._want_sources()
        done = self.store.done_by.get("sources")
        answer = done.data if done is not None and done.ok else None
        chans = source_channels(self.store.setup)
        picked = tuple(source_value(value, ch) for ch in chans)
        mbps = int((self.store.setting("S-11") or {}).get(quality) or 0) or None
        whole = self.store.setting(LENGTH) == "Whole movie"
        self._pick_ready_sources(sid, value, answer, quality, chans, mbps, whole)
        each = {ch: source_options(answer, quality, picked, ch, mbps, whole) for ch in chans}
        key = (tuple(chans), tuple(tuple(o) for o in each.values()))
        if w.key != key:
            while (old := w.row.takeAt(0)) is not None:
                if (gone := old.widget()) is not None:
                    gone.deleteLater()
            w.boxes = {}
            for ch in chans:
                opts = each[ch]
                box = QComboBox()
                for label, data, _, note in opts:
                    box.addItem(label, data)
                    if note:
                        box.setItemData(box.count() - 1, note, Qt.ItemDataRole.ToolTipRole)
                model = box.model()
                if isinstance(model, QStandardItemModel):
                    for i, (_, _, usable, _) in enumerate(opts):
                        row = model.item(i)
                        if row is not None and not usable:
                            row.setEnabled(False)
                box.activated.connect(functools.partial(self._source_picked, sid, ch, box))
                _fit_combo(box)
                at = len(w.boxes)
                w.row.addWidget(QLabel(f"CH{ch}"), at, 0)
                w.row.addWidget(box, at, 1)
                w.boxes[ch] = box
            w.key = key
        for ch, box in w.boxes.items():
            self._select(box, source_value(value, ch))

    def _pick_ready_sources(self, sid: str, value: Any, answer: Any, quality: str,
                            chans: list[int], mbps: int | None, whole: bool) -> None:  # fmt: skip
        """A channel with no movie picked gets one made already for it (once per channel
        and quality), between tests only; a person's own pick is never replaced."""
        if answer is None or self.store.run_state != "IDLE":
            return
        picks = dict(value) if isinstance(value, dict) else {}
        done: set[tuple[int, str]] = getattr(self, "_auto_sources", set())
        self._auto_sources = done
        for ch in chans:
            if str(ch) in picks or (ch, quality) in done:
                continue
            done.add((ch, quality))
            name = ready_source(answer, quality, ch, mbps, whole)
            if name is not None:
                self.edit_setting(sid, source_change(self.store.setting(sid), ch, name))

    def _source_picked(self, sid: str, ch: int, box: QComboBox, _index: int) -> None:
        self.edit_setting(sid, source_change(self.store.setting(sid), ch, str(box.currentData())))

    def _making_for(self) -> tuple[str, str, int, dict[int, str]] | None:
        """(sender, quality, Mbps, {channel: file}) of the choice on screen, when it has a
        sender and a movie; None otherwise."""
        sender = ((self.store.setup or {}).get("roles") or {}).get("sender")
        quality = str(self.store.choice.get("quality") or "UHD")
        chosen = chosen_sources(self.store.setting(SOURCES), source_channels(self.store.setup))
        if not sender or all(v == GENERATED for v in chosen.values()):
            return None
        mbps = int((self.store.setting("S-11") or {}).get(quality) or 0)
        return str(sender), quality, mbps, chosen

    def _prepare_now(self) -> None:
        """A movie was picked: the sender PC starts making it at once, the newest pick
        wins (02 2-4 ``only``); Start then waits only for what is left (GAP-116)."""
        want = self._making_for()
        if want is None or self.store.run_state != "IDLE" or self.store.status.broadcast.get("on"):
            return
        sender, quality, mbps, chosen = want
        module = str(self.store.choice.get("module") or "iptv")
        movies = {str(ch): name for ch, name in chosen.items() if name != GENERATED}
        whole = self.store.setting(LENGTH) == "Whole movie"
        params: dict[str, Any] = {
            "module": module,
            "sender": sender,
            "quality": quality,
            "channels": sorted(chosen),
            "sources": movies,
            "only": True,
        }
        if mbps:
            params["bitrate_mbps"] = mbps
        if whole:
            params["length"] = "whole"
        self.commands.send("prepare", f"{P}/prepare", timeout_s=PREPARE_TIMEOUT_S, **params)
        self._making_at = float("-inf")  # read its state soon

    def _want_making(self) -> None:
        """The making state under Source video, read every few seconds between tests
        (off the screen thread) while a movie is picked."""
        want = self._making_for()
        if want is None or self.store.run_state != "IDLE":
            return
        now = time.monotonic()
        if now - self._making_at < MAKING_EVERY_S:
            return
        module = str(self.store.choice.get("module") or "iptv")
        if self.commands.read("making", f"{P}/prepare", timeout_s=PREPARE_TIMEOUT_S,
                              module=module, sender=want[0]):  # fmt: skip
            self._making_at = now

    def _render_making(self, w: SourcesBox) -> None:
        want = self._making_for() if self.store.run_state == "IDLE" else None
        lines: list[tuple[str, str]] = []
        if want is not None:
            whole_now = self.store.setting(LENGTH) == "Whole movie"
            asked = (want[0], want[1], want[2], tuple(sorted(want[3].items())), whole_now)
            if asked != self._asked_making:  # picked before this window opened (GAP-121)
                self._asked_making = asked
                self._prepare_now()
            self._want_making()
            done = self.store.done_by.get("making")
            answer = done.data if done is not None and done.ok else None
            whole = self.store.setting(LENGTH) == "Whole movie"
            lines = making_lines(answer, want[1], want[2], want[3], whole)
        text = "\n".join(t for _, t in lines)
        _set_text(w.note, text)
        color = "red" if any(c == "red" for c, _ in lines) else ""
        style = f"color:{COLORS['red']};" if color else ""
        if w.note.styleSheet() != style:
            w.note.setStyleSheet(style)
        if w.note.isHidden() == bool(lines):
            w.note.setVisible(bool(lines))

    def _want_sources(self) -> None:
        sender = ((self.store.setup or {}).get("roles") or {}).get("sender")
        if not sender or self.store.run_state != "IDLE":
            return
        now = time.monotonic()
        if sender == self._sources_for and now - self._sources_at < SOURCES_EVERY_S:
            return
        module = str(self.store.choice.get("module") or "iptv")
        if self.commands.read("sources", f"{P}/sources", timeout_s=SOURCES_TIMEOUT_S,
                              node=sender, module=module):  # fmt: skip
            self._sources_for, self._sources_at = sender, now
            self.q.put(("sent", "sources"))

    def _spin_done(self, sid: str, item: dict[str, Any], spin: QSpinBox, scale: int) -> None:
        if item.get("kind") == "per_quality_int":  # S-11: only the chosen quality changes
            current = dict(self.store.setting(sid) or {})
            current[str(self.store.choice.get("quality") or "UHD")] = spin.value()
            value: Any = current
        else:
            value = spin.value() * scale
        if value != self.store.setting(sid):
            self.edit_setting(sid, value)

    def _show_value(self, w: QWidget, item: dict[str, Any], value: Any, quality: str) -> None:
        if isinstance(w, QComboBox):
            self._select(w, value)
        elif isinstance(w, QSpinBox):
            if item.get("kind") == "per_quality_int":
                lo, hi = (item.get("ranges") or {}).get(quality, (0, 999))
                if (w.minimum(), w.maximum()) != (lo, hi):
                    w.setRange(int(lo), int(hi))
                v = int((value or {}).get(quality) or lo)
            else:
                v = int(value or 0) // int(item.get("scale") or 1)
            if w.value() != v and not w.hasFocus():  # never fight the user's typing
                w.blockSignals(True)
                w.setValue(v)
                w.blockSignals(False)
        elif isinstance(w, QDoubleSpinBox):
            if abs(w.value() - float(value or 0)) > 1e-9 and not w.hasFocus():
                w.blockSignals(True)
                w.setValue(float(value or 0))
                w.blockSignals(False)
        elif isinstance(w, QCheckBox):
            if w.isChecked() != bool(value):
                w.blockSignals(True)
                w.setChecked(bool(value))
                w.blockSignals(False)
        elif isinstance(w, Elided) and item.get("kind") == "groups" and isinstance(value, dict):
            text = f"{', '.join(value.get('groups') or [])} : {value.get('port')}"  # 16 S-14
            w.set_full(text, f"{text}\n{adv_tip(item)}")
        elif isinstance(w, Elided):
            text = (
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, dict | list)
                else str(value)
            )
            w.set_full(text, f"{text}\n{adv_tip(item)}")  # the value and why it is grey

    @staticmethod
    def _disable_items(box: QComboBox, item: dict[str, Any]) -> None:
        """Choices this build has not got stay visible, grey, with the reason (16 3)."""
        not_yet = item.get("unavailable_choices") or {}
        model = box.model()
        if not isinstance(model, QStandardItemModel):
            return
        for i in range(box.count()):
            row = model.item(i)
            why = not_yet.get(box.itemData(i))
            if row is not None and row.isEnabled() == bool(why):
                row.setEnabled(not why)
                row.setToolTip(str(why or ""))

    def _render_devices(self, rows: list[RowView], ctl: Ctl) -> None:
        names = [r.name for r in rows]
        for name in list(self._rows):
            if name not in names:
                w = self._rows.pop(name)
                self.devices_box.removeWidget(w)
                w.hide()  # never drawn under the new rows while it waits
                w.deleteLater()  # windows opened and closed leave nothing (1.4)
        for i, r in enumerate(rows):
            row = self._rows.get(r.name)
            if row is None:
                row = self._rows[r.name] = DeviceRow(functools.partial(self._on_channel, r.name))
                self.devices_box.insertWidget(i, row)
            row.show_view(r)
            self._apply(row.chan, replace(ctl, visible=ctl.visible and bool(r.channels)))
        self.no_devices.setVisible(not rows)

    def on_stop_broadcast(self) -> None:
        """03 F18 [Stop broadcast]: asks first, then one command (double clicks wait)."""
        if self.commands.busy("broadcast"):
            return
        if self._ask(TEXT["stop_broadcast_title"], TEXT["stop_broadcast_body"]):
            self.commands.send("broadcast", f"{P}/broadcast", timeout_s=60, on=False)

    def _on_channel(self, device: str, ch: int) -> None:
        """03 F19: the box changed; the service moves the receiver (answers in up to 7 s)."""
        self.commands.send(f"channel:{device}", f"{P}/channel", timeout_s=30, device=device, ch=ch)

    @staticmethod
    def _apply(w: QWidget, c: Ctl) -> None:
        """The only place that sets enabled / visible (1.10). Grey-but-clickable Start/Stop
        stays clickable and explains itself."""
        clickable = c.enabled or bool(c.popup)
        if w.isEnabled() != clickable:
            w.setEnabled(clickable)
        if w.isHidden() == c.visible:
            w.setVisible(c.visible)
        # the reason first, the ⓘ help always after it (11-ui 1.10, 20 GAP-84)
        _set_tip(w, "\n".join(x for x in (c.tooltip, str(w.property("help") or "")) if x))
        if isinstance(w, QPushButton):
            if c.label and w.text() != c.label:
                w.setText(c.label)
            waiting = "true" if (c.popup and not c.enabled) else "false"
            if w.property("waiting") != waiting:
                w.setProperty("waiting", waiting)
                w.style().unpolish(w)
                w.style().polish(w)
            w.setProperty("popup", c.popup)

    def _duration_typed(self) -> None:
        """A typed test length: set when it reads as 10 s .. 200 h, else say how to type it
        and show the current one again."""
        text = self.c_duration.currentText()
        now = self.store.choice.get("duration_s")
        secs = parse_duration(text)
        if secs is None:
            if text.strip() and text != self.c_duration.itemText(self.c_duration.currentIndex()):
                self._tell(TEXT["duration_title"], TEXT["duration_bad"].format(text=text))
            self._select(self.c_duration, now)
            i = self.c_duration.currentIndex()
            self.c_duration.setEditText(self.c_duration.itemText(i) if i >= 0 else "")
            return
        self._select(self.c_duration, secs)
        if secs != now:
            self.edit(duration_s=secs)

    @staticmethod
    def _select(box: QComboBox, value: Any) -> None:
        i = box.findData(value)
        if i < 0 and isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            box.blockSignals(True)
            box.addItem(hms(float(value)), value)  # a plan value not in the list
            box.blockSignals(False)
            i = box.findData(value)
        if i >= 0 and box.currentIndex() != i:
            box.blockSignals(True)
            box.setCurrentIndex(i)
            box.blockSignals(False)

    # ---------------------------------------------------------------- actions

    def on_start_stop(self) -> None:
        popup = str(self.b_start.property("popup") or "")
        if popup:
            self._tell(TEXT["app_title"], popup)
            return
        sent = ""
        if self.store.run_state == "PREPARING":  # Cancel while videos are made (USR-006)
            if self.commands.send("stop", f"{P}/stop", timeout_s=60):
                sent = "stop"
        elif self.store.run_state == "RUNNING":
            if self._ask(TEXT["stop_title"], TEXT["stop_body"]) and self.commands.send(
                "stop", f"{P}/stop", timeout_s=STOP_TIMEOUT_S
            ):
                sent = "stop"
                self._stop_sent_at = time.monotonic()
        elif self.store.plan is not None and self._ask(
            TEXT["start_title"], TEXT["start_body"].format(what=self._start_what())
        ):
            self._start_when_saved(50)  # a change just made is saved first, then Start
            return
        if sent:  # visible within 0.1 s (1.14), through the store like everything (1.1)
            self.q.put(("sent", sent))
            self.drain()

    # ---------------------------------------------------------------- S6 fault screen

    def _render_fault(self, v: FaultView | None, restart: Ctl) -> None:
        want = v is not None
        if self.fault_scroll.isHidden() == want:
            self.g_fault.setVisible(want)
            self.fault_scroll.setVisible(want)
        if v is None:
            return
        _set_text(self.fault_what, v.what)
        _set_text(self.fault_data, v.data)
        names = [d.name for d in v.devices]
        for name in list(self._fault_rows):
            if name not in names:
                w, _, _ = self._fault_rows.pop(name)
                self.fault_devices.removeWidget(w)
                w.hide()  # never drawn under the new rows while it waits
                w.deleteLater()
        for i, d in enumerate(v.devices):
            if d.name not in self._fault_rows:
                row = QWidget()
                h = QHBoxLayout(row)
                h.setContentsMargins(0, 0, 0, 0)
                label = QLabel()
                label.setWordWrap(True)
                button = QPushButton(TEXT["fault_restart"])
                button.clicked.connect(lambda _=False, n=d.name: self.on_restart(n))
                h.addWidget(label, 1)
                h.addWidget(button)
                self.fault_devices.insertWidget(i, row)
                self._fault_rows[d.name] = (row, label, button)
            _, label, button = self._fault_rows[d.name]
            _set_text(label, f"{ICONS.get(d.color, '')} {d.name}: {d.state}")
            self._apply(button, restart if not restart.enabled else Ctl(
                True, tooltip=TEXT["fault_restart_tip"]))  # fmt: skip

    def on_reset(self) -> None:
        """F11 Reset All: asks first (13: a confirmed action), then one command."""
        popup = str(self.b_reset.property("popup") or "")
        if popup:
            self._tell(TEXT["app_title"], popup)
            return
        if self._ask(TEXT["reset_title"], TEXT["reset_body"]) and self.commands.send(
            "reset", f"{P}/reset", timeout_s=RESET_TIMEOUT_S
        ):
            self.q.put(("sent", "reset"))
            self.drain()

    def on_restart(self, name: str) -> None:
        job = f"restart:{name}"
        if self.commands.send(job, f"{P}/nodes", timeout_s=RESET_TIMEOUT_S, action="restart",
                              node=name):  # fmt: skip
            self.q.put(("sent", job))
            self.drain()

    def on_shutdown(self) -> None:
        if self._ask(TEXT["shutdown_title"], TEXT["shutdown_body"]) and self.commands.send(
            "shutdown", f"{P}/nodes", timeout_s=RESET_TIMEOUT_S, action="stop"
        ):
            self.q.put(("sent", "shutdown"))
            self.drain()

    def _render_problems(self, v: ProblemsView) -> None:
        """Badge top right (F21): red with errors, yellow with warnings only."""
        if self.b_problems.text() != v.badge:
            self.b_problems.setText(v.badge)
        want = bool(v.badge)
        if self.b_problems.isHidden() == want:
            self.b_problems.setVisible(want)
        icon = ICONS.get(v.badge_color, "")
        tip = f"{icon} {v.badge}" if v.badge else ""
        if self.b_problems.toolTip() != tip:
            self.b_problems.setToolTip(tip)
        style = f"color:{COLORS['red'] if v.badge_color == 'red' else COLORS['yellow']};"
        if self.b_problems.styleSheet() != style:
            self.b_problems.setStyleSheet(style)
        if self.problems_window is not None:
            self.problems_window.show_view(v)
        errors = {f"{r.code}|{r.device}" for r in v.rows if r.active and r.color == "red"}
        if errors - self._errors_seen:
            self._blinks_left = 10  # a new error: blink for 5 s (F21)
            self.blink.start()
            if (self.store.setting("S-52") or CORE_DEFAULTS["S-52"]) == "On":
                QApplication.beep()  # S-52 Problem sound (off by default)
        elif not errors and self.blink.isActive():
            self.blink.stop()  # timers follow the state (11-ui 1.12 rule 6)
            self._blinks_left = 0
        self._errors_seen = errors

    def _blink_step(self) -> None:
        self._blinks_left -= 1
        on = self._blinks_left % 2 == 1
        self.b_problems.setProperty("blink", "true" if on else "false")
        weight = "bold" if on else "normal"
        self.b_problems.setStyleSheet(f"color:{COLORS['red']}; font-weight:{weight};")
        if self._blinks_left <= 0:
            self.blink.stop()

    def _render_actions(self, v: ActionsView) -> None:
        if self.b_actions.text() != v.badge:
            self.b_actions.setText(v.badge)
        if self.b_actions.isHidden() == bool(v.badge):
            self.b_actions.setVisible(bool(v.badge))
        if v.waiting_keys - self._actions_seen:
            self.on_actions()  # a new action: the window comes to the front (F22)
        self._actions_seen = v.waiting_keys
        if self.actions_window is not None:
            self.actions_window.show_view(v)

    def on_actions(self) -> None:
        if self.actions_window is None:
            self.actions_window = ActionsWindow(
                self, self._send_action_done, self._send_action_choice
            )
        self.actions_window.show_view(actions_view(self.store.status))
        self.actions_window.show()
        self.actions_window.raise_()

    def _send_action_done(self, key: str) -> None:
        if self.commands.send(f"action_done:{key}", f"{P}/actions/done", key=key):
            self.q.put(("sent", f"action_done:{key}"))

    def _send_action_choice(self, key: str, choice: str) -> None:
        """20 W-1: off the screen thread like every command (11-ui 1.1); one at a time."""
        job = f"action_choose:{key}"
        if self.commands.send(job, f"{P}/actions/choose", timeout_s=30, key=key, choice=choice):
            self.q.put(("sent", job))

    def on_problems(self) -> None:
        """Opens the Problems window; it never blocks the main window (F21, 11-ui 1.6)."""
        if self.problems_window is None:
            self.problems_window = ProblemsWindow(self, self.on_export, self._open)
        self.problems_window.show_view(problems_view(self.store.status))
        self.problems_window.show()
        self.problems_window.raise_()

    # ---------------------------------------------------------------- diagnostics, storage

    def on_export(self) -> None:
        """02 2-13 diagnostics off the screen thread; a second click while it runs is
        ignored (11-ui 1.12 one job at a time)."""
        if self.commands.send("diagnostics", f"{P}/diagnostics", timeout_s=DIAG_TIMEOUT_S):
            self.q.put(("sent", "diagnostics"))
            self.drain()

    def _render_export(self, ctl: Ctl) -> None:
        if self.problems_window is None:
            return
        d = self.store.done_by.get("diagnostics")
        note = export_text(d.ok, d.data, d.error) if d is not None else ""
        path = str(d.data.get("path", "")) if d is not None and d.ok else ""
        self.problems_window.show_export(ctl, note, str(Path(path).parent) if path else "")

    def on_storage(self) -> None:
        if self.storage_window is None:
            self.storage_window = StorageWindow(self, self.refresh_storage, self.on_delete,
                                                self.redraw)  # fmt: skip
        self.refresh_storage()
        self.storage_window.show()
        self.storage_window.raise_()
        self.redraw()

    def refresh_storage(self) -> None:
        if self.commands.read("storage_list", f"{P}/storage", timeout_s=STORAGE_TIMEOUT_S):
            self.q.put(("sent", "storage_list"))

    def on_delete(self, row: StorageRow, raw_only: bool) -> None:
        """03 F15: people choose and confirm; nothing is deleted by itself."""
        key = "storage_raw" if raw_only else "storage_run"
        size = size_text(row.raw_bytes if raw_only else row.bytes)
        if row.in_use or not self._ask(
            TEXT[f"{key}_title"].format(run=row.run),
            TEXT[f"{key}_body"].format(run=row.run, size=size),
        ):
            return  # fmt: skip
        if self.commands.send("storage_delete", f"{P}/storage/delete", timeout_s=STORAGE_TIMEOUT_S,
                              run=row.run, raw_only=raw_only):  # fmt: skip
            self.q.put(("sent", "storage_delete"))
            self.drain()

    def _render_storage(self) -> None:
        w = self.storage_window
        if w is None:
            return
        deleted = self.store.done_by.get("storage_delete")
        if deleted is not None and deleted is not self._deleted_seen:
            self._deleted_seen = deleted  # answered: say what happened, then list again
            why = str((deleted.error or {}).get("what", ""))
            self._storage_note = (deleted_note(deleted.data) if deleted.ok
                                  else TEXT["storage_kept"].format(what=why))  # fmt: skip
            self.refresh_storage()
        listing = self.store.done_by.get("storage_list")
        if listing is not None and not listing.ok:
            data: dict[str, Any] | None = {
                "nodes": {"Controller": (listing.error or {}).get("what", "")}
            }
        else:
            data = listing.data if listing is not None else None
        chosen = w.selected()
        pending = self.store.pending
        actions = storage_actions(
            chosen.run if chosen else None, bool(chosen and chosen.in_use), pending
        )
        loading = "storage_list" in pending
        refresh = Ctl(not loading, label=TEXT["storage_refresh"],
                      tooltip=TEXT["storage_loading"] if loading else "")  # fmt: skip
        w.show_view(storage_view(data, self._storage_note), actions, refresh)

    def on_last_report(self) -> None:
        result = self.store.status.run.get("result") or {}
        if not result.get("run_id"):
            self._tell(TEXT["last_report"], TEXT["no_report"])
            return
        self._open(result_view(result, self.store.status.reports_dir).report)

    def on_reports(self) -> None:
        """F15 S8: the list opens at once and is read by a worker (20 GAP-83)."""
        if self.reports_window is None:
            self.reports_window = ReportsWindow(self, self._open, self.refresh_reports)
        self.refresh_reports()
        self.reports_window.show()
        self.reports_window.raise_()
        self.redraw()

    def refresh_reports(self) -> None:
        if self.commands.read("reports_list", f"{P}/reports"):
            self.q.put(("sent", "reports_list"))

    def _render_reports(self) -> None:
        if self.reports_window is None:
            return
        d = self.store.done_by.get("reports_list")
        error = str((d.error or {}).get("what", "")) if d is not None and not d.ok else ""
        loading = "reports_list" in self.store.pending
        refresh = Ctl(not loading, label=TEXT["storage_refresh"],
                      tooltip=TEXT["reports_loading"] if loading else "")  # fmt: skip
        data = d.data if d is not None and d.ok else None
        self.reports_window.show_view(reports_view(data, error), refresh)

    # ---------------------------------------------------------------- modals (1.6)

    def _ask_modal(self, title: str, text: str) -> bool:
        if self._modal_open:
            return False
        self._modal_open = True
        try:
            box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=self)
            box.setStandardButtons(
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes
            )
            # the button says what it does (2026-10-10: Reset All? asked "Stop / Cancel")
            box.button(QMessageBox.StandardButton.Yes).setText(yes_label(title))
            return box.exec() == QMessageBox.StandardButton.Yes
        finally:
            self._modal_open = False

    def _result_modal(self, v: ResultView) -> None:
        """The result window does not hold the main window (2026-10-24: a result window
        behind it made the program look frozen until it was found and closed). One at a
        time: a newer result replaces an older one still open."""
        old = getattr(self, "_result_window", None)
        if old is not None:
            old.close()
        dlg = ResultDialog(v, self._open, self)
        dlg.setModal(False)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda *_: setattr(self, "_result_window", None))
        self._result_window = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _tell_modal(self, title: str, text: str) -> None:
        if self._modal_open:
            return
        self._modal_open = True
        try:
            QMessageBox.information(self, title, text)
        finally:
            self._modal_open = False

    # ---------------------------------------------------------------- layout (1.8)

    def _flow(self, cols: int) -> None:
        for i in reversed(range(self.form.count())):
            self.form.takeAt(i)
        for i, (label, field) in enumerate(self.fields):
            r, c = divmod(i, cols)
            if label is None:
                self.form.addWidget(field, r, 2 * c, 1, 2)
            else:
                self.form.addWidget(label, r, 2 * c)
                self.form.addWidget(field, r, 2 * c + 1)
        for c in range(4):
            self.form.setColumnStretch(c, 1 if c % 2 else 0)

    @property
    def narrow(self) -> bool:
        return self.body.direction() == QBoxLayout.Direction.TopToBottom

    def resizeEvent(self, e: QResizeEvent) -> None:
        """Below a usable 1280x720 the areas stack (Test above Devices); the Test fields
        flow into two columns and Start/Stop moves to the fixed top bar, so Start/Stop
        and Jitter / Loss stay on screen (1.8)."""
        super().resizeEvent(e)
        small = e.size().width() < MIN_USABLE.width() or e.size().height() < MIN_USABLE.height()
        if self.l_too_small.isHidden() == small:
            self.l_too_small.setVisible(small)
        narrow = e.size().width() < NARROW.width() or e.size().height() < NARROW.height()
        # stacked: Advanced must leave room for the device rows below it (1.11 no clipping)
        cap = e.size().height() // 4 if narrow else QWIDGETSIZE_MAX
        if self.adv_scroll.maximumHeight() != cap:
            self.adv_scroll.setMaximumHeight(cap)
        fault_cap = e.size().height() // 3  # the device rows stay in view below it
        if self.fault_scroll.maximumHeight() != fault_cap:
            self.fault_scroll.setMaximumHeight(fault_cap)
        if narrow == self.narrow:
            return
        if narrow:
            self.body.setDirection(QBoxLayout.Direction.TopToBottom)
            self._flow(2)
            self.left_box.removeItem(self.run_box)
            self.run_box.setDirection(QBoxLayout.Direction.LeftToRight)
            self.top.insertLayout(0, self.run_box)
        else:
            self.body.setDirection(QBoxLayout.Direction.LeftToRight)
            self._flow(1)
            self.top.removeItem(self.run_box)
            self.run_box.setDirection(QBoxLayout.Direction.TopToBottom)
            self.left_box.addLayout(self.run_box)


def _serials_of(side: Any) -> set[str]:
    """The tablets on one PC's USB, by serial (a row's tablet to its PC)."""
    return {str(t.get("serial")) for t in (side or {}).get("tablets") or [] if isinstance(t, dict)}
