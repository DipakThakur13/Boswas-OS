"""The dialogs of Archive Manager and the progress panel.

Every dialog is a ``fluent.ContentDialog`` that is opened without blocking:
the window keeps the open dialog in ``window.dialog`` and connects its
``finished`` signal, so tests can fill in and press it like a person would.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGridLayout, QHBoxLayout, QLineEdit, QListWidget,
                               QPlainTextEdit, QProgressBar, QSizePolicy, QVBoxLayout, QWidget)

import boswas_fluent as fluent

from .. import extract, formats, model, names

Result = fluent.ContentDialog.Result
POLICY_CHOICES = ((extract.ASK, "Ask what to do"), (extract.REPLACE, "Replace the file"),
                  (extract.SKIP, "Keep the file that is there"), (extract.RENAME, "Keep both (add a number)"))
LEVEL_CHOICES = ((0, "None (store only)"), (1, "Fast"), (6, "Normal"), (9, "Smallest"))
FORMAT_CHOICES = (("zip", "ZIP"), ("tar", "TAR (not compressed)"), ("tar.gz", "TAR.GZ"), ("tar.xz", "TAR.XZ"),
                  ("tar.bz2", "TAR.BZ2"))
_MAX_ISSUES_SHOWN = 2000


def _error_label(text: str = "") -> fluent.Label:
    """A line for what is wrong with the input. It always takes its line, so the dialog keeps its size."""
    label = fluent.Label(text, "caption", tone="danger")
    label.setMinimumHeight(label.fontMetrics().height() + 2)
    label.setAccessibleName("Problem")
    return label


def _path_row(edit: QLineEdit, button: fluent.Button) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addWidget(edit, 1)
    row.addWidget(button)
    return row


class ExtractDialog(fluent.ContentDialog):
    """Where to extract to, and what to do with files that are already there."""

    def __init__(self, parent: QWidget, *, title: str, destination: str, folder_name: str, make_folder: bool,
                 policy: str, open_after: bool, choose_folder) -> None:
        super().__init__(parent, title, primary="Extract", close="Cancel")
        self._choose_folder = choose_folder
        self.destination = QLineEdit(destination)
        self.destination.setAccessibleName("Destination folder")
        self.destination.setClearButtonEnabled(True)
        self.browse = fluent.Button("Browse…")
        self.browse.clicked.connect(self._browse)
        self.make_folder = QCheckBox(f"Extract into a new folder named “{names.display(folder_name)}”")
        self.make_folder.setChecked(make_folder)
        self.folder_name = folder_name
        self.policy = QComboBox()
        self.policy.setAccessibleName("If a file already exists")
        for key, text in POLICY_CHOICES:
            self.policy.addItem(text, key)
        self.policy.setCurrentIndex(max(0, self.policy.findData(policy)))
        self.open_after = QCheckBox("Open the folder when done")
        self.open_after.setChecked(open_after)
        self.error = _error_label()
        layout = self.content_layout
        layout.addWidget(fluent.Label("Extract to", "body-strong"))
        layout.addLayout(_path_row(self.destination, self.browse))
        layout.addWidget(self.make_folder)
        layout.addWidget(fluent.Label("If a file already exists", "body-strong"))
        layout.addWidget(self.policy)
        layout.addWidget(self.open_after)
        layout.addWidget(self.error)
        self.destination.returnPressed.connect(self.accept)
        self.destination.textChanged.connect(lambda _text: self.error.setText(""))

    def _browse(self) -> None:
        chosen = self._choose_folder("Extract to", self.destination.text().strip() or os.path.expanduser("~"))
        if chosen:
            self.destination.setText(chosen)

    def target(self) -> str:
        """The folder the entries go to."""
        base = os.path.expanduser(self.destination.text().strip())
        if self.make_folder.isChecked():
            base = os.path.join(base, self.folder_name)
        return os.path.abspath(base) if base else ""

    def chosen_policy(self) -> str:
        return self.policy.currentData()

    def done(self, result) -> None:
        if result == Result.PRIMARY:                # the dialog stays open until its input makes sense
            text = self.destination.text().strip()
            if not text:
                self.error.setText("Choose a folder to extract to.")
                return
            base = os.path.abspath(os.path.expanduser(text))
            if os.path.exists(base) and not os.path.isdir(base):
                self.error.setText("That is a file, not a folder.")
                return
        super().done(result)


class OverwriteDialog(fluent.ContentDialog):
    """A file is already there: replace it, keep it, or keep both; once or for every such file."""

    def __init__(self, parent: QWidget, conflict: extract.Conflict) -> None:
        super().__init__(parent, "This file already exists", conflict.path, primary="Replace", secondary="Skip",
                         close="Cancel", default="secondary")
        self.decision = extract.CANCEL
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)
        rows = (("In the folder", conflict.existing_size, conflict.existing_mtime),
                ("In the archive", conflict.new_size, conflict.new_mtime))
        for row, (title, size, mtime) in enumerate(rows):
            grid.addWidget(fluent.Label(title, "body", tone="secondary"), row, 0)
            grid.addWidget(fluent.Label(model.human_size(size)), row, 1)
            grid.addWidget(fluent.Label(model.time_text(mtime) or "—"), row, 2)
        grid.setColumnStretch(3, 1)
        self.content_layout.addLayout(grid)
        self.keep_both = fluent.Button("Keep both (add a number to the new file)")
        self.keep_both.clicked.connect(self._keep_both)
        self.content_layout.addWidget(self.keep_both, 0, Qt.AlignmentFlag.AlignLeft)
        self.for_all = QCheckBox("Do the same for the other files that already exist")
        self.content_layout.addWidget(self.for_all)
        self.finished.connect(self._finished)
        self._custom: str | None = None

    def _keep_both(self) -> None:
        self._custom = extract.RENAME
        self.done(Result.NONE)

    def _finished(self, result) -> None:
        if self._custom is not None:
            choice = self._custom
        elif result == Result.PRIMARY:
            choice = extract.REPLACE
        elif result == Result.SECONDARY:
            choice = extract.SKIP
        else:
            self.decision = extract.CANCEL
            return
        self.decision = f"{choice}-all" if self.for_all.isChecked() else choice


class PasswordDialog(fluent.ContentDialog):
    def __init__(self, parent: QWidget, archive_name: str, wrong: bool = False) -> None:
        super().__init__(parent, "Password needed",
                         f"“{names.display(archive_name)}” is protected with a password.",
                         primary="Unlock", close="Cancel")
        self.box = fluent.PasswordBox(placeholder="Password")
        self.content_layout.addWidget(self.box)
        self.error = _error_label("That password is not correct. Try again." if wrong else "")
        self.content_layout.addWidget(self.error)
        self.box.returnPressed.connect(self.accept)

    def open(self) -> None:
        super().open()
        self.box.setFocus(Qt.FocusReason.PopupFocusReason)

    def password(self) -> str:
        return self.box.password()

    def done(self, result) -> None:
        if result == Result.PRIMARY and not self.box.password():
            return
        super().done(result)


class NewArchiveDialog(fluent.ContentDialog):
    """Name, place, kind and options of a new archive, and what goes into it."""

    def __init__(self, parent: QWidget, *, sources: list[str], directory: str, name: str, choose_files,
                 choose_folder) -> None:
        super().__init__(parent, "New archive", primary="Create", close="Cancel")
        self._choose_files, self._choose_folder = choose_files, choose_folder
        self.name = QLineEdit(name)
        self.name.setAccessibleName("Archive name")
        self.format = QComboBox()
        self.format.setAccessibleName("Kind of archive")
        for key, text in FORMAT_CHOICES:
            self.format.addItem(text, key)
        self.directory = QLineEdit(directory)
        self.directory.setAccessibleName("Folder to save in")
        self.browse = fluent.Button("Browse…")
        self.browse.clicked.connect(self._browse)
        self.level = QComboBox()
        self.level.setAccessibleName("Compression")
        for value, text in LEVEL_CHOICES:
            self.level.addItem(text, value)
        self.level.setCurrentIndex(2)
        self.follow = QCheckBox("Follow links (pack what they point to)")
        self.hidden = QCheckBox("Leave out hidden files")
        self.sources = QListWidget()
        self.sources.setAccessibleName("Files and folders to pack")
        self.sources.setFixedHeight(112)
        self.sources.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.add_files = fluent.Button("Add files…")
        self.add_folder = fluent.Button("Add folder…")
        self.remove = fluent.Button("Remove")
        self.add_files.clicked.connect(self._add_files)
        self.add_folder.clicked.connect(self._add_folder)
        self.remove.clicked.connect(self._remove)
        self.error = _error_label()

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(self.name, 1)
        name_row.addWidget(self.format)
        form.addRow(fluent.Label("Name"), name_row)
        form.addRow(fluent.Label("Save in"), _path_row(self.directory, self.browse))
        form.addRow(fluent.Label("Compression"), self.level)
        layout = self.content_layout
        layout.addLayout(form)
        layout.addWidget(self.follow)
        layout.addWidget(self.hidden)
        layout.addWidget(fluent.Label("Contents", "body-strong"))
        layout.addWidget(self.sources)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.add_files)
        buttons.addWidget(self.add_folder)
        buttons.addWidget(self.remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addWidget(self.error)
        for source in sources:
            self.add_source(source)
        self.format.currentIndexChanged.connect(self._format_changed)
        self.sources.itemSelectionChanged.connect(self._sync)
        self.name.textEdited.connect(lambda _text: self.error.setText(""))
        self.directory.textChanged.connect(lambda _text: self.error.setText(""))
        self._sync()

    def _format_changed(self) -> None:
        self.level.setEnabled(self.format.currentData() != "tar")

    def _sync(self) -> None:
        self.remove.setEnabled(bool(self.sources.selectedItems()))

    def _browse(self) -> None:
        chosen = self._choose_folder("Save the archive in", self.directory.text().strip() or os.path.expanduser("~"))
        if chosen:
            self.directory.setText(chosen)

    def add_source(self, path: str) -> None:
        path = os.path.abspath(path)
        if path not in self.source_paths():
            self.sources.addItem(path)
        self.error.setText("")

    def _add_files(self) -> None:
        for path in self._choose_files("Add files to the archive", self.directory.text().strip()):
            self.add_source(path)

    def _add_folder(self) -> None:
        chosen = self._choose_folder("Add a folder to the archive", self.directory.text().strip())
        if chosen:
            self.add_source(chosen)

    def _remove(self) -> None:
        for item in self.sources.selectedItems():
            self.sources.takeItem(self.sources.row(item))

    def source_paths(self) -> list[str]:
        return [self.sources.item(row).text() for row in range(self.sources.count())]

    def output(self) -> str:
        fmt = formats.FORMATS[self.format.currentData()]
        name = self.name.text().strip()
        if name.lower().endswith(fmt.extension):
            name = name[:-len(fmt.extension)]
        return os.path.join(os.path.abspath(os.path.expanduser(self.directory.text().strip())), name + fmt.extension)

    def _fail(self, text: str) -> None:
        self.error.setText(text)

    def done(self, result) -> None:
        if result == Result.PRIMARY:
            name = self.name.text().strip()
            if not name or "/" in name or "\0" in name or name in (".", ".."):
                return self._fail("Give the archive a name (without “/”).")
            directory = self.directory.text().strip()
            if not directory or not os.path.isdir(os.path.expanduser(directory)):
                return self._fail("Choose an existing folder to save the archive in.")
            if not self.source_paths():
                return self._fail("Add at least one file or folder.")
        super().done(result)


class PropertiesDialog(fluent.ContentDialog):
    """Facts about the archive (or one entry) in two columns; every value can be selected and copied."""

    def __init__(self, parent: QWidget, title: str, rows: list[tuple[str, str]]) -> None:
        super().__init__(parent, title, primary=None, close="Close", default="close")
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        self.values: dict[str, str] = {}
        for row, (name, value) in enumerate(rows):
            self.values[name] = value
            label = fluent.Label(name, "body", tone="secondary")
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            text = fluent.Label(value, "body", wrap=True, selectable=True)
            text.setAccessibleName(name)
            grid.addWidget(label, row, 0)
            grid.addWidget(text, row, 1)
        grid.setColumnStretch(1, 1)
        self.content_layout.addLayout(grid)


class IssuesDialog(fluent.ContentDialog):
    """What an operation left out and why, as text that can be copied."""

    def __init__(self, parent: QWidget, title: str, summary: str, sections: list[tuple[str, list]],
                 notes: list[str] = ()) -> None:
        super().__init__(parent, title, summary, primary=None, close="Close", default="close")
        lines: list[str] = []
        shown = 0
        for heading, issues in sections:
            if not issues:
                continue
            lines.append(f"{heading} ({len(issues):,})")
            for issue in issues:
                if shown >= _MAX_ISSUES_SHOWN:
                    break
                lines.append(f"  {issue.path or 'The archive'}: {issue.reason}")
                shown += 1
            if shown >= _MAX_ISSUES_SHOWN:
                lines.append("  … and more")
            lines.append("")
        lines.extend(notes)
        self.text = QPlainTextEdit("\n".join(lines).strip())
        self.text.setReadOnly(True)
        self.text.setAccessibleName("Details")
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.text.setMinimumHeight(200)
        self.content_layout.addWidget(self.text)


class ProgressPanel(QWidget):
    """Title, current item, a bar, numbers and Cancel: what a running operation shows."""

    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None, *, cancel_button: bool = True) -> None:
        super().__init__(parent)
        self.title = fluent.Label("", "body-strong")
        self.item = fluent.Label("", "body", tone="secondary")
        self.item.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.item.setTextFormat(Qt.TextFormat.PlainText)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 0)
        self.bar.setAccessibleName("Progress")
        self.detail = fluent.Label("", "caption", tone="secondary")
        self.cancel = fluent.Button("Cancel")
        self.cancel.clicked.connect(self._cancel)
        self.cancel.setVisible(cancel_button)
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addWidget(self.title)
        text.addWidget(self.item)
        text.addWidget(self.bar)
        text.addWidget(self.detail)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        row.addLayout(text, 1)
        row.addWidget(self.cancel, 0, Qt.AlignmentFlag.AlignVCenter)
        self._item_text = ""

    def start(self, title: str) -> None:
        self.title.setText(title)
        self._item_text = ""
        self.item.setText("Preparing…")
        self.detail.setText("")
        self.bar.setRange(0, 0)
        self.cancel.setEnabled(True)
        self.cancel.setText("Cancel")

    def _cancel(self) -> None:
        self.cancel.setEnabled(False)
        self.cancel.setText("Cancelling…")
        self.cancel_requested.emit()

    def show_progress(self, info) -> None:
        self._item_text = info.path
        self._elide()
        if info.bytes_total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(min(1000, int(1000 * info.bytes_done / info.bytes_total)))
            numbers = f"{model.human_size(info.bytes_done)} of {model.human_size(info.bytes_total)}"
        elif info.entries_total > 0 and info.bytes_total == 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(min(1000, int(1000 * info.entries_done / info.entries_total)))
            numbers = ""
        else:
            self.bar.setRange(0, 0)
            numbers = model.human_size(info.bytes_done)
        items = f"{info.entries_done:,} of {info.entries_total:,} items" if info.entries_total else ""
        self.detail.setText(" · ".join(part for part in (numbers, items) if part))

    def _elide(self) -> None:
        metrics = self.item.fontMetrics()
        self.item.setText(metrics.elidedText(self._item_text, Qt.TextElideMode.ElideMiddle, max(40, self.item.width())))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._item_text:
            self._elide()


def archive_rows(archive, path: str) -> list[tuple[str, str]]:
    """The rows of the Properties dialog of an archive."""
    totals = archive.listing.totals
    try:
        modified = model.time_text(os.path.getmtime(path))
    except OSError:
        modified = ""
    unpacked = model.human_size(totals.size) if not totals.unknown_sizes else "Not known before extracting"
    rows = [
        ("Name", names.display(os.path.basename(path))),
        ("Folder", names.display(os.path.dirname(path))),
        ("Kind", archive.format.title),
        ("Files", f"{totals.files:,}"),
        ("Folders", f"{sum(1 for folder in archive.listing.children if folder):,}"),
        ("Size when extracted", unpacked),
        ("Size of the archive", model.human_size(archive.file_size)),
        ("Packed to", model.ratio_text(totals.size, archive.file_size) + " of the original size"
         if totals.size and not totals.unknown_sizes else "—"),
        ("Modified", modified or "—"),
        ("Password", "Protected" if archive.encrypted else "None"),
        ("Can be changed", "Yes" if archive.writable else "No (read only)"),
    ]
    if archive.comment.strip():
        rows.append(("Comment", names.display(archive.comment.strip())[:2000]))
    return rows


def entry_rows(entry) -> list[tuple[str, str]]:
    """The rows of the Properties dialog of one entry."""
    rows = [("Name", names.display(entry.base)), ("Folder in the archive", names.display(entry.parent) or "/"),
            ("Kind", model.type_text(entry))]
    if entry.kind != model.DIR:
        rows.append(("Size", model.human_size(entry.size)))
        if entry.packed >= 0:
            rows.append(("Packed size", model.human_size(entry.packed)))
    if entry.mtime is not None:
        rows.append(("Modified", model.time_text(entry.mtime)))
    if entry.mode is not None:
        rows.append(("Permissions", model.mode_text(entry)))
    if entry.crc is not None and entry.kind == model.FILE:
        rows.append(("CRC-32", model.crc_text(entry)))
    if entry.method:
        rows.append(("Method", entry.method))
    if entry.link:
        rows.append(("Link to", names.display(entry.link)))
    rows.append(("Encrypted", "Yes" if entry.encrypted else "No"))
    return rows
