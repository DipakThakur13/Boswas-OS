"""Applications page: filter tabs, search, the application table and its actions."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLineEdit, QStackedWidget, QTabBar,
                               QTableView, QVBoxLayout, QWidget)

from .. import viewmodel as vm
from .common import SP, button, heading, icon, plain_label, status_dot, windows_app_icon

COLUMNS = ("Name", "Version", "Publisher", "Architecture", "State", "Running", "Compatibility", "Last launch")
ID_ROLE = Qt.ItemDataRole.UserRole
SORT_ROLE = Qt.ItemDataRole.UserRole + 1


class AppTableModel(QAbstractTableModel):
    """Rows are viewmodel.AppRow values; every string is already sanitised."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[vm.AppRow] = []
        self._icon = windows_app_icon()

    def set_rows(self, rows: list[vm.AppRow]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row, column = self.rows[index.row()], index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (row.name, row.version, row.publisher, row.architecture, row.display_state, row.running_label,
                    row.compatibility_label, row.last_launch)[column]
        if role == Qt.ItemDataRole.DecorationRole:
            if column == 0:
                return self._icon
            if column == 4:
                return status_dot(row.level)
            if column == 6:
                return status_dot(vm.CATALOG_LEVELS.get(row.compatibility, "neutral"))
        if role == ID_ROLE:
            return row.id
        if role == SORT_ROLE:
            if column == 7:
                return row.last_launch_at
            return self.data(index, Qt.ItemDataRole.DisplayRole).casefold()
        return None


class LibraryPage(QWidget):
    """Emits action_requested(action, app_id) for Launch, Stop, Repair, Update, Remove, Logs and Details."""

    action_requested = Signal(str, str)

    BUTTONS = (
        ("launch", "Launch", ("media-playback-start",), SP.SP_MediaPlay),
        ("stop", "Stop", ("media-playback-stop", "process-stop"), SP.SP_MediaStop),
        ("repair", "Repair", ("tools-wizard", "run-build-configure", "configure"), SP.SP_BrowserReload),
        ("update", "Update…", ("system-software-update", "update-none"), SP.SP_ArrowUp),
        ("remove", "Remove", ("edit-delete", "list-remove"), SP.SP_TrashIcon),
        ("logs", "Logs", ("text-x-log", "utilities-log-viewer", "view-list-text"), SP.SP_FileDialogContentsView),
        ("details", "Details", ("documentinfo", "dialog-information"), SP.SP_FileDialogInfoView),
    )

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.rows: list[vm.AppRow] = []
        self.filter_key = "all"

        layout = QVBoxLayout(self)
        layout.addWidget(heading("Windows Applications"))

        self.tabs = QTabBar(self)
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        for key, label in vm.FILTERS:
            index = self.tabs.addTab(label)
            self.tabs.setTabData(index, key)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search applications")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("edit-find", "search", fallback=SP.SP_FileDialogContentsView),
                              QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self.apply_filter)
        top = QHBoxLayout()
        top.addWidget(self.tabs, 1)
        top.addWidget(self.search)
        layout.addLayout(top)

        self.buttons = {}
        actions = QHBoxLayout()
        for key, text, names, fallback in self.BUTTONS:
            widget = button(text, icon(*names, fallback=fallback), self)
            widget.clicked.connect(lambda _checked=False, k=key: self._request(k))
            self.buttons[key] = widget
            actions.addWidget(widget)
            if key == "remove":
                actions.addStretch(1)
        layout.addLayout(actions)

        self.model = AppTableModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.table = QTableView(self)
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setHighlightSections(False)
        self.table.selectionModel().selectionChanged.connect(self.update_actions)
        self.table.doubleClicked.connect(lambda _index: self._request("details"))

        self.empty = plain_label("", wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty)
        layout.addWidget(self.stack, 1)

        # Why the selected application is in its state (plain text, below the table).
        self.reason = plain_label("", wrap=True, selectable=True)
        layout.addWidget(self.reason)
        self.update_actions()

    # --- data ---
    def set_rows(self, rows: list[vm.AppRow]) -> None:
        selected = self.selected_id()
        self.rows = list(rows)
        counts = vm.filter_counts(self.rows)
        for index in range(self.tabs.count()):
            key = self.tabs.tabData(index)
            label = vm.FILTER_LABELS[key]
            self.tabs.setTabText(index, label if key == "all" or not counts[key] else f"{label} ({counts[key]})")
        self.apply_filter()
        if selected:
            self.select_app(selected)

    def set_filter(self, key: str) -> None:
        for index in range(self.tabs.count()):
            if self.tabs.tabData(index) == key:
                self.tabs.setCurrentIndex(index)
                return
        raise ValueError(f"unknown filter {key!r}")

    def _tab_changed(self, index: int) -> None:
        self.filter_key = self.tabs.tabData(index) or "all"
        self.apply_filter()

    def apply_filter(self, *_args) -> None:
        selected = self.selected_id()
        visible = vm.filter_rows(self.rows, self.filter_key, self.search.text())
        self.model.set_rows(visible)
        if visible:
            self.stack.setCurrentWidget(self.table)
        else:
            if not self.rows:
                self.empty.setText("No Windows applications are installed yet. Use Install to add one.")
            elif self.search.text().strip():
                self.empty.setText("No applications match your search.")
            else:
                self.empty.setText(f"No applications in “{vm.FILTER_LABELS[self.filter_key]}”.")
            self.stack.setCurrentWidget(self.empty)
        if selected:
            self.select_app(selected)
        self.update_actions()

    def visible_ids(self) -> list[str]:
        return [self.proxy.index(r, 0).data(ID_ROLE) for r in range(self.proxy.rowCount())]

    # --- selection ---
    def selected_id(self) -> str | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].data(ID_ROLE) if rows else None

    def selected_row(self) -> vm.AppRow | None:
        app_id = self.selected_id()
        return next((row for row in self.rows if row.id == app_id), None)

    def select_app(self, app_id: str) -> bool:
        for r in range(self.proxy.rowCount()):
            index = self.proxy.index(r, 0)
            if index.data(ID_ROLE) == app_id:
                self.table.selectRow(r)
                return True
        return False

    def update_actions(self, *_args) -> None:
        row = self.selected_row()
        for key, enabled in vm.available_actions(row).items():
            self.buttons[key].setEnabled(enabled)
        if row is None:
            self.reason.clear()
        elif row.update_version:
            self.reason.setText(f"{row.name}: version {row.update_version} is available in the compatibility "
                                "catalog.")
        elif row.state_reason:
            self.reason.setText(f"{row.name}: {row.state_reason}")
        else:
            self.reason.clear()

    def _request(self, action: str) -> None:
        row = self.selected_row()
        if row is not None and vm.available_actions(row).get(action):
            self.action_requested.emit(action, row.id)
