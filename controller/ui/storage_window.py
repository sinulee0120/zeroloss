"""Manage storage (docs/11-ui.md S14, 03 F15, 20 GAP-82).

Runs biggest first with where they are; the person picks one and deletes its raw data
or the whole run after a confirmation. The window only draws what the store holds and
calls back; the list and the deletion run on the command worker (11-ui 1.1, 1.2).
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

from controller.ui.control_state import Ctl
from controller.ui.texts import TEXT
from controller.ui.view import StorageRow, StorageView
from core.ui_schema.design import COLORS, GRID


class StorageModel(QAbstractTableModel):
    COLS = TEXT["storage_cols"].split("|")

    def __init__(self) -> None:
        super().__init__()
        self.rows: tuple[StorageRow, ...] = ()

    def set_rows(self, rows: tuple[StorageRow, ...]) -> None:
        if rows != self.rows:  # redraw only when something changed (1.13)
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
            return (r.run, r.size, r.raw, r.where)[index.column()]
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor(COLORS["text_disabled"] if r.in_use else COLORS["text"])
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (1, 2):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class StorageWindow(QDialog):
    """S14: not modal, so the test screen stays usable while it is open."""

    def __init__(
        self,
        parent: QWidget,
        on_refresh: Callable[[], None],
        on_delete: Callable[[StorageRow, bool], None],
        on_select: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(TEXT["storage_title"])
        self.setModal(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2 * GRID, 2 * GRID, 2 * GRID, 2 * GRID)
        lay.setSpacing(GRID)
        self.notes = QLabel()
        self.notes.setWordWrap(True)
        self.notes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.model = StorageModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        head = self.table.horizontalHeader()
        for col in range(len(StorageModel.COLS)):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch if col == 3
                                      else QHeaderView.ResizeMode.ResizeToContents)  # fmt: skip
        self.b_refresh = QPushButton(TEXT["storage_refresh"])
        self.b_raw = QPushButton("Delete raw data")
        self.b_run = QPushButton("Delete run")
        buttons = QHBoxLayout()  # window actions bottom right, main one last (1.11)
        buttons.addWidget(self.b_refresh)
        buttons.addStretch(1)
        buttons.addWidget(self.b_raw)
        buttons.addWidget(self.b_run)
        lay.addWidget(self.notes)
        lay.addWidget(self.table, 1)
        lay.addLayout(buttons)
        self.b_refresh.clicked.connect(on_refresh)
        self.b_raw.clicked.connect(lambda: self._delete(on_delete, True))
        self.b_run.clicked.connect(lambda: self._delete(on_delete, False))
        self.table.selectionModel().selectionChanged.connect(lambda *_: on_select())
        self.resize(110 * GRID, 60 * GRID)

    def selected(self) -> StorageRow | None:
        rows = self.table.selectionModel().selectedRows()
        if len(rows) != 1 or rows[0].row() >= len(self.model.rows):
            return None
        return self.model.rows[rows[0].row()]

    def _delete(self, on_delete: Callable[[StorageRow, bool], None], raw_only: bool) -> None:
        row = self.selected()
        if row is not None:
            on_delete(row, raw_only)

    def show_view(self, v: StorageView, actions: dict[str, Ctl], refresh: Ctl) -> None:
        chosen = self.selected()
        self.model.set_rows(v.rows)
        if chosen is not None and self.selected() is None:  # keep the choice over a refresh
            for i, r in enumerate(v.rows):
                if r.run == chosen.run:
                    self.table.selectRow(i)
        text = "\n".join(v.notes)
        if self.notes.text() != text:
            self.notes.setText(text)
        pairs = ((self.b_raw, actions["delete_raw"]), (self.b_run, actions["delete_run"]),
                 (self.b_refresh, refresh))  # fmt: skip
        for button, ctl in pairs:
            if button.isEnabled() != ctl.enabled:
                button.setEnabled(ctl.enabled)
            if ctl.label and button.text() != ctl.label:
                button.setText(ctl.label)
            if button.toolTip() != ctl.tooltip:
                button.setToolTip(ctl.tooltip)
