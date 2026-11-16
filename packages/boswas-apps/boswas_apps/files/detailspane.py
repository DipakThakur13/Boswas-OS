"""The details pane: a preview of the selected file and what is known about it."""

from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import QFormLayout, QFrame, QLabel, QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget

import boswas_fluent as fluent

from . import mime, thumbs, uiicons
from .listing import Entry
from .model import format_time
from .text import display_name, format_size, plural
from .thumbnailer import make_thumbnail

_TEXT_BYTES = 16 * 1024
_PREVIEW = 256


def read_preview(path: str, size: int, mtime: float) -> dict:
    """What the pane shows for one file beyond its row (blocking: runs on a worker thread)."""
    result: dict = {"path": path}
    name = mime.for_file(path)
    result["mime"] = name
    result["type"] = mime.description(name)
    if name.startswith("image/"):
        reader = QImageReader(path)
        reader.setDecideFormatFromContent(True)
        dimensions = reader.size()
        if dimensions.isValid():
            result["dimensions"] = f"{dimensions.width()} × {dimensions.height()}"
        if thumbs.wants_thumbnail(path, size):
            image = make_thumbnail(path, mtime, _PREVIEW)
            if image is not None:
                result["image"] = image
    elif mime.is_text(name) or name in ("application/json", "application/xml", "application/x-desktop",
                                        "application/x-shellscript", "application/javascript"):
        try:
            with open(path, "rb") as handle:
                data = handle.read(_TEXT_BYTES + 1)
        except OSError:
            data = b""
        if b"\0" not in data:
            text = data[:_TEXT_BYTES].decode("utf-8", "replace")
            result["text"] = text + ("\n…" if len(data) > _TEXT_BYTES else "")
    return result


def count_folder(path: str) -> int | None:
    try:
        with os.scandir(path) as iterator:
            return sum(1 for _entry in iterator)
    except OSError:
        return None


class DetailsPane(QScrollArea):
    """Shows the selection of the current tab: nothing selected (the folder), one item, several items."""

    def __init__(self, services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self._token = 0
        self._path = ""
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(220)
        self.setAccessibleName("Details pane")
        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        self.picture = QLabel(body)
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setMinimumHeight(120)
        self.picture.setAccessibleName("Preview")
        self.text_preview = QPlainTextEdit(body)
        self.text_preview.setReadOnly(True)
        self.text_preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text_preview.setFixedHeight(220)
        self.text_preview.setAccessibleName("Text preview")
        self.text_preview.setFont(fluent.font("caption"))
        self.title = fluent.Label("", "body-strong", body, wrap=True, selectable=True)
        self.subtitle = fluent.Label("", "caption", body, tone="secondary", wrap=True)
        self.form_host = QWidget(body)
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 4, 0, 0)
        self.form.setHorizontalSpacing(16)
        self.form.setVerticalSpacing(8)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        for widget in (self.picture, self.text_preview, self.title, self.subtitle, fluent.Separator(parent=body),
                       self.form_host):
            layout.addWidget(widget)
        layout.addStretch(1)
        self.setWidget(body)
        self.text_preview.hide()
        self._rows: dict[str, fluent.Label] = {}

    def _set_rows(self, rows: list[tuple[str, str]]) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self._rows = {}
        for label, value in rows:
            if not value:
                continue
            name = fluent.Label(label, "caption", self.form_host, tone="secondary")
            content = fluent.Label(value, "caption", self.form_host, wrap=True, selectable=True)
            self.form.addRow(name, content)
            self._rows[label] = content

    def row(self, label: str) -> str:
        """The value shown for a label (tests)."""
        return self._rows[label].text() if label in self._rows else ""

    def _icon(self, icon, size: int = 96) -> None:
        self.picture.setPixmap(icon.pixmap(QSize(size, size)) if icon is not None else QPixmap())
        self.picture.show()
        self.text_preview.hide()

    # --- what to show ---
    def show_folder(self, title: str, location: str, count: int, icon) -> None:
        self._token += 1
        self._path = ""
        self._icon(icon)
        self.title.setText(title)
        self.subtitle.setText(plural(count, "item"))
        self._set_rows([("Location", display_name(location) if location.startswith("/") else "")])

    def show_many(self, entries: list[Entry]) -> None:
        self._token += 1
        self._path = ""
        self._icon(uiicons.named("folder-open", "folder") if all(entry.is_dir for entry in entries)
                   else mime.icon("application/octet-stream"))
        self.title.setText(plural(len(entries), "item") + " selected")
        files = [entry for entry in entries if not entry.is_dir]
        folders = len(entries) - len(files)
        self.subtitle.setText("")
        self._set_rows([("Files", f"{len(files):,}" if files else ""), ("Folders", f"{folders:,}" if folders else ""),
                        ("Size of files", format_size(sum(entry.size for entry in files)) if files else "")])

    def show_entry(self, entry: Entry) -> None:
        self._token += 1
        token = self._token
        self._path = entry.path
        self._icon(mime.entry_icon(entry))
        self.title.setText(display_name(entry.name))
        self.subtitle.setText(entry.type_text)
        rows = [("Size", "" if entry.is_dir else format_size(entry.size)), ("Modified", format_time(entry.mtime)),
                ("Location", display_name(entry.location or os.path.dirname(entry.path)))]
        if entry.deleted:
            rows.append(("Deleted", format_time(entry.deleted)))
        if entry.is_symlink:
            try:
                rows.append(("Link to", display_name(os.readlink(entry.path))))
            except OSError:
                pass
        self._set_rows(rows)
        if entry.broken:
            return
        if entry.is_dir:
            self.services.worker.submit(count_folder, entry.path,
                                        on_done=lambda count: self._counted(token, rows, count))
        else:
            self.services.worker.submit(read_preview, entry.path, entry.size, entry.mtime,
                                        on_done=lambda result: self._previewed(token, rows, result))

    def _counted(self, token: int, rows: list[tuple[str, str]], count: int | None) -> None:
        if token == self._token and count is not None:
            self._set_rows([("Contains", plural(count, "item")), *rows[1:]])

    def _previewed(self, token: int, rows: list[tuple[str, str]], result: dict) -> None:
        if token != self._token:
            return
        self.subtitle.setText(result.get("type", self.subtitle.text()))
        if "dimensions" in result:
            self._set_rows([*rows[:1], ("Dimensions", result["dimensions"]), *rows[1:]])
        if "image" in result:
            pixmap = QPixmap.fromImage(result["image"])
            width = max(120, self.viewport().width() - 40)
            if pixmap.width() > width:
                pixmap = pixmap.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
            self.picture.setPixmap(pixmap)
        elif "text" in result:
            self.text_preview.setPlainText(result["text"])
            self.text_preview.show()
            self.picture.hide()

    def shown_path(self) -> str:
        return self._path
