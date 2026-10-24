"""Reports list (docs/11-ui.md S8, 03 F15, 20 GAP-83).

Past reports newest first, the result in its color, [Show] and [Folder] on every row;
a double click shows the report. The window only draws what the store holds; the list
is read by the command worker (11-ui 1.1, 1.2). [Compare] comes in the second phase.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from controller.ui.control_state import Ctl, report_row_actions
from controller.ui.texts import TEXT
from controller.ui.view import ReportRow, ReportsView
from core.ui_schema.design import BUTTON_MIN_HEIGHT, COLORS, GRID, ICONS


class ReportsModel(QAbstractTableModel):
    COLS = (*TEXT["reports_cols"].split("|"), "")  # the last column holds the buttons

    def __init__(self) -> None:
        super().__init__()
        self.rows: tuple[ReportRow, ...] = ()

    def set_rows(self, rows: tuple[ReportRow, ...]) -> bool:
        if rows == self.rows:  # redraw only when something changed (1.13)
            return False
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()
        return True

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
            result = f"{ICONS.get(r.color, '')} {r.verdict}".strip()
            return (r.started, r.run, r.kind, r.time, result, r.why, "")[col]
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 4 and r.has_report:  # the result in its color (05 5.4), with an icon
                return QColor(COLORS[r.color])
            return QColor(COLORS["text"] if r.has_report else COLORS["text_disabled"])
        return None


class ReportsWindow(QDialog):
    """S8: not modal, so the test screen stays usable while it is open."""

    def __init__(
        self, parent: QWidget, on_open: Callable[[str], None], on_refresh: Callable[[], None]
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(TEXT["reports_title"])
        self.setModal(False)
        self._open = on_open
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2 * GRID, 2 * GRID, 2 * GRID, 2 * GRID)
        lay.setSpacing(GRID)
        self.notes = QLabel()
        self.notes.setWordWrap(True)
        self.notes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.model = ReportsModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(BUTTON_MIN_HEIGHT + GRID)  # buttons fit
        self.table.setWordWrap(False)
        head = self.table.horizontalHeader()
        for col in range(len(ReportsModel.COLS)):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch if col == 5
                                      else QHeaderView.ResizeMode.ResizeToContents)  # fmt: skip
        self.b_refresh = QPushButton(TEXT["storage_refresh"])
        bottom = QHBoxLayout()
        bottom.addWidget(self.b_refresh)
        bottom.addStretch(1)
        lay.addWidget(self.notes)
        lay.addWidget(self.table, 1)
        lay.addLayout(bottom)
        self.row_buttons: list[tuple[QPushButton, QPushButton]] = []
        self.b_refresh.clicked.connect(on_refresh)
        self.table.doubleClicked.connect(self._double)
        self.resize(130 * GRID, 60 * GRID)

    def _double(self, index: QModelIndex) -> None:
        r = self.model.rows[index.row()]
        if r.has_report:
            self._open(r.report)

    def _buttons(self, r: ReportRow) -> tuple[QWidget, QPushButton, QPushButton]:
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(GRID // 2, 0, GRID // 2, 0)
        acts = report_row_actions(r.has_report)
        show, folder = QPushButton(acts["show"].label), QPushButton(acts["folder"].label)
        for b, c in ((folder, acts["folder"]), (show, acts["show"])):  # main one last (1.11)
            b.setEnabled(c.enabled)
            b.setToolTip(c.tooltip)
            h.addWidget(b)
        show.clicked.connect(lambda: self._open(r.report))
        folder.clicked.connect(lambda: self._open(r.folder))
        return box, show, folder

    def show_view(self, v: ReportsView, refresh: Ctl) -> None:
        if self.model.set_rows(v.rows):
            self.row_buttons = []
            for i, r in enumerate(v.rows):
                box, show, folder = self._buttons(r)
                self.table.setIndexWidget(self.model.index(i, len(ReportsModel.COLS) - 1), box)
                self.row_buttons.append((show, folder))
            self.table.resizeColumnToContents(len(ReportsModel.COLS) - 1)
        text = "\n".join(v.notes)
        if self.notes.text() != text:
            self.notes.setText(text)
        if self.b_refresh.isEnabled() != refresh.enabled:
            self.b_refresh.setEnabled(refresh.enabled)
        if self.b_refresh.toolTip() != refresh.tooltip:
            self.b_refresh.setToolTip(refresh.tooltip)
