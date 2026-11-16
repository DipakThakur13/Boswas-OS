"""The dialogs of Files: conflicts, choosing an application, settings, a passphrase, a list of errors."""

from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
                               QScrollArea, QVBoxLayout, QWidget)

import boswas_fluent as fluent
from boswas_fluent import ContentDialog

from . import uiicons
from .desktopapps import DesktopApp
from .fileops import KEEP_BOTH, REPLACE, SKIP, Conflict
from .model import format_time
from .settings import VIEW_LABELS, VIEW_MODES
from .text import display_name, format_size, split_extension


class ConflictDialog(ContentDialog):
    """Something with the same name is at the destination: Replace (or merge), Skip, Keep both, or Cancel."""

    answered = Signal(str, bool)            # the choice (fileops constant, or "cancel"), for all conflicts

    def __init__(self, parent: QWidget, conflict: Conflict) -> None:
        name = display_name(os.path.basename(conflict.destination))
        folder = display_name(os.path.basename(os.path.dirname(conflict.destination)) or "/")
        both_folders = conflict.source_is_dir and conflict.destination_is_dir
        title = "Merge the folders?" if both_folders else "Replace or skip?"
        super().__init__(parent, title, f"“{folder}” already has {'a folder' if conflict.destination_is_dir else 'a file'} "
                                        f"named “{name}”.", primary=None, secondary=None, close="Cancel",
                         default=None)
        self.conflict = conflict

        def describe(is_dir: bool, size: int, mtime: float) -> str:
            kind = "Folder" if is_dir else format_size(size)
            return f"{kind}, modified {format_time(mtime)}"

        incoming = fluent.Label(f"Incoming: {describe(conflict.source_is_dir, conflict.source_size, conflict.source_mtime)}",
                                "caption", tone="secondary", wrap=True)
        existing = fluent.Label(f"Already there: {describe(conflict.destination_is_dir, conflict.destination_size, conflict.destination_mtime)}",
                                "caption", tone="secondary", wrap=True)
        self.content_layout.addWidget(incoming)
        self.content_layout.addWidget(existing)
        self.replace_button = fluent.Button("Merge" if both_folders else "Replace", accent=True)
        self.skip_button = fluent.Button("Skip")
        self.keep_button = fluent.Button("Keep both")
        for button in (self.replace_button, self.skip_button, self.keep_button):
            button.setAutoDefault(False)
            self.content_layout.addWidget(button)
        self.replace_button.setToolTip("Merge the two folders; files with the same name are asked about"
                                       if both_folders else "Replace what is there with the incoming item")
        self.skip_button.setToolTip("Leave what is there and do not copy this item")
        self.keep_button.setToolTip("Give the incoming item another name")
        self.for_all = QCheckBox("Do this for all conflicts")
        self.content_layout.addWidget(self.for_all)
        self._choice = "cancel"
        self.replace_button.clicked.connect(lambda: self._choose(REPLACE))
        self.skip_button.clicked.connect(lambda: self._choose(SKIP))
        self.keep_button.clicked.connect(lambda: self._choose(KEEP_BOTH))
        self.finished.connect(lambda _result: self.answered.emit(self._choice, self.for_all.isChecked()))

    def _choose(self, choice: str) -> None:
        self._choice = choice
        self.done(ContentDialog.Result.PRIMARY)


class OpenWithDialog(ContentDialog):
    """Choose the application that opens a file; optionally make it the default for the type."""

    def __init__(self, parent: QWidget, file_name: str, type_text: str, recommended: list[DesktopApp],
                 others: list[DesktopApp], *, always: bool = False, force_always: bool = False) -> None:
        extension = split_extension(file_name)[1]
        what = f"{extension} files" if extension else f"“{type_text}” files"
        super().__init__(parent, "Choose an app", f"Open “{display_name(file_name)}” with:" if not force_always
                         else f"Choose the app that opens {what}.", primary="Open" if not force_always else "Set default",
                         close="Cancel")
        self.list = QListWidget()
        self.list.setAccessibleName("Applications")
        self.list.setIconSize(QSize(24, 24))
        self.list.setMinimumHeight(260)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        seen: set[str] = set()
        for app in [*recommended, *others]:
            if app.app_id in seen:
                continue
            seen.add(app.app_id)
            item = QListWidgetItem(uiicons.app_icon(app.icon), app.name)
            item.setData(Qt.ItemDataRole.UserRole, app)
            if app.comment:
                item.setToolTip(app.comment)
            self.list.addItem(item)
        self.content_layout.addWidget(self.list, 1)
        self.empty = fluent.Label("No applications are installed that could open this.", "body", tone="secondary",
                                  wrap=True)
        self.content_layout.addWidget(self.empty)
        self.empty.setVisible(self.list.count() == 0)
        self.list.setVisible(self.list.count() > 0)
        self.always = QCheckBox(f"Always use this app to open {what}")
        self.always.setChecked(always or force_always)
        self.always.setVisible(not force_always)
        self.content_layout.addWidget(self.always)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.primary_button.setEnabled(self.list.count() > 0)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        self.list.itemActivated.connect(lambda _item: self.accept())

    def open(self) -> None:
        super().open()
        if self.list.count():
            self.list.setFocus()

    def chosen(self) -> DesktopApp | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def make_default(self) -> bool:
        return self.always.isChecked()


class PassphraseDialog(ContentDialog):
    def __init__(self, parent: QWidget, volume_name: str) -> None:
        super().__init__(parent, f"Unlock “{display_name(volume_name)}”",
                         "This volume is encrypted. Enter its passphrase to open it.", primary="Unlock", close="Cancel")
        self.box = fluent.PasswordBox(placeholder="Passphrase")
        self.content_layout.addWidget(self.box)
        self.box.returnPressed.connect(self.accept)
        self.primary_button.setEnabled(False)
        self.box.textChanged.connect(lambda text: self.primary_button.setEnabled(bool(text)))

    def open(self) -> None:
        super().open()
        self.box.setFocus()

    def passphrase(self) -> str:
        return self.box.password()


class ErrorsDialog(ContentDialog):
    """What an operation could not do, item by item."""

    def __init__(self, parent: QWidget, title: str, errors: list[tuple[str, str]]) -> None:
        count = len(errors)
        summary = "One item could not be handled." if count == 1 else f"{count:,} items could not be handled."
        super().__init__(parent, title, summary, primary=None, close="Close", default="close")
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Details")
        self.details.setMinimumHeight(min(260, 60 + 28 * min(count, 8)))
        lines = []
        for path, message in errors[:500]:
            lines.append(f"{display_name(path)}\n    {message}" if path else message)
        if count > 500:
            lines.append(f"… and {count - 500:,} more")
        self.details.setPlainText("\n".join(lines))
        self.content_layout.addWidget(self.details)


class SettingsDialog(ContentDialog):
    """The settings of Files. Every change applies at once."""

    def __init__(self, parent: QWidget, services) -> None:
        super().__init__(parent, "Settings", "", primary="Done", close=None)
        self.services = services
        settings = services.settings
        area = QScrollArea()
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 4, 0)
        column.setSpacing(4)
        self.switches: dict[str, fluent.ToggleSwitch] = {}
        for key, title, description in (
                ("show_hidden", "Show hidden files", "Files and folders whose names start with a dot"),
                ("show_extensions", "Show file extensions", "report.pdf instead of report"),
                ("single_click", "Open with a single click", "Otherwise a double click opens"),
                ("confirm_trash", "Ask before moving to the Trash", "Deleting for good always asks")):
            switch = fluent.ToggleSwitch(title, checked=bool(settings.get(key)))
            switch.toggled.connect(lambda checked, k=key: services.set_setting(k, bool(checked)))
            self.switches[key] = switch
            column.addWidget(fluent.SettingsCard(title, description, None, switch))
        self.view_box = QComboBox()
        self.view_box.setAccessibleName("Default view")
        for mode in VIEW_MODES:
            self.view_box.addItem(VIEW_LABELS[mode], mode)
        self.view_box.setCurrentIndex(max(0, self.view_box.findData(settings.get("view_mode"))))
        self.view_box.currentIndexChanged.connect(
            lambda _index: services.set_setting("view_mode", self.view_box.currentData()))
        column.addWidget(fluent.SettingsCard("View", "Used for every folder", None, self.view_box))
        self.start_box = QComboBox()
        self.start_box.setAccessibleName("Start folder")
        for label, value in (("Home", "home"), ("This PC", "computer"), ("Another folder", "custom")):
            self.start_box.addItem(label, value)
        start = str(settings.get("start_folder"))
        self.start_path = QLineEdit(start if start.startswith("/") else "")
        self.start_path.setPlaceholderText("/path/of/a/folder")
        self.start_path.setAccessibleName("Start folder path")
        self.start_box.setCurrentIndex(self.start_box.findData(start if start in ("home", "computer") else "custom"))
        column.addWidget(fluent.SettingsCard("Start folder", "Where new windows and tabs open", None, self.start_box))
        column.addWidget(self.start_path)
        self.start_error = fluent.Label("", "caption", tone="danger", wrap=True)
        column.addWidget(self.start_error)
        self.start_error.hide()
        column.addStretch(1)
        area.setWidget(body)
        area.setMinimumHeight(420)
        self.content_layout.addWidget(area, 1)
        self.text_label.hide()
        self.start_box.currentIndexChanged.connect(self._start_changed)
        self.start_path.editingFinished.connect(self._start_changed)
        self.start_path.setVisible(self.start_box.currentData() == "custom")

    def _start_changed(self, *_args) -> None:
        choice = self.start_box.currentData()
        self.start_path.setVisible(choice == "custom")
        self.start_error.hide()
        if choice != "custom":
            self.services.set_setting("start_folder", choice)
            return
        path = os.path.expanduser(self.start_path.text().strip())
        if not path:
            return
        if not path.startswith("/") or not os.path.isdir(path):
            self.start_error.setText("That is not a folder.")
            self.start_error.show()
            return
        self.services.set_setting("start_folder", os.path.normpath(path))
