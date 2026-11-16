"""The address bar: a breadcrumb that turns into a text box for typing a path."""

from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QRectF, QStringListModel, Qt, Signal
from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import QCompleter, QHBoxLayout, QLineEdit, QSizePolicy, QStackedLayout, QWidget

import boswas_fluent as fluent
from boswas_fluent import paint

from . import places

_MAX_COMPLETIONS = 500


def list_folders(directory: str, hidden: bool) -> list[str]:
    """The names of the folders in ``directory`` (blocking: runs on a worker thread)."""
    names = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                if not hidden and entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:
                    continue
                if len(names) >= _MAX_COMPLETIONS:
                    break
    except OSError:
        return []
    return sorted(names, key=str.casefold)


class _Crumbs(QWidget):
    """The breadcrumb page: looks like a text box, a click beside the crumbs starts editing."""

    edit_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.icon = fluent.IconWidget(None, 16, self)
        self.bar = fluent.BreadcrumbBar(self)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 8, 0)
        row.setSpacing(8)
        row.addWidget(self.icon)
        row.addWidget(self.bar, 1)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.bar.setCursor(Qt.CursorShape.ArrowCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.edit_requested.emit()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        fill = "fill-control-hover" if self.underMouse() else "fill-control"
        paint.rounded_rect(painter, QRectF(self.rect()), fluent.theme().radius("control"), fill, "stroke-control")


class AddressBar(QWidget):
    """Shows where the tab is; ``location_entered(text)`` when the user typed a path and pressed Enter."""

    location_entered = Signal(str)
    crumb_clicked = Signal(str)

    def __init__(self, services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self._location = ""
        self._crumb_locations: list[str] = []
        self._listed: str | None = None
        self._text: str | None = None
        self.crumbs = _Crumbs(self)
        self.edit_box = QLineEdit(self)
        self.edit_box.setAccessibleName("Address")
        self.edit_box.setPlaceholderText("Type a path")
        self.edit_box.setClearButtonEnabled(False)
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self.crumbs)
        self._stack.addWidget(self.edit_box)
        self.setFixedHeight(32)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Address bar")
        self._completions = QStringListModel(self)
        self.completer = QCompleter(self._completions, self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.setMaxVisibleItems(10)
        self.edit_box.setCompleter(self.completer)
        self.crumbs.edit_requested.connect(self.start_editing)
        self.crumbs.bar.item_clicked.connect(self._crumb)
        self.edit_box.returnPressed.connect(self._entered)
        self.edit_box.textEdited.connect(self._typed)
        self.edit_box.installEventFilter(self)

    # --- showing ---
    def set_location(self, location: str, crumbs: list[tuple[str, str]], icon: QIcon | None = None,
                     text: str | None = None) -> None:
        """Show ``crumbs`` ((label, location) pairs); ``text`` replaces the path in the edit box (search results)."""
        self._location = location
        self._crumb_locations = [target for _label, target in crumbs]
        labels = [label for label, _target in crumbs]
        if labels != self.crumbs.bar.items():
            self.crumbs.bar.set_items(labels)
        self.crumbs.icon.set_icon(icon)
        self._text = text
        if self.is_editing():
            self.stop_editing()

    def location(self) -> str:
        return self._location

    def is_editing(self) -> bool:
        return self._stack.currentWidget() is self.edit_box

    def start_editing(self) -> None:
        """Turn into a text box with the path selected (Ctrl+L, Alt+D, a click)."""
        text = self._text if self._text is not None else (
            "" if places.is_virtual(self._location) else self._location)
        home = places.home()
        if text == home or text.startswith(home + "/"):
            text = "~" + text[len(home):]
        self.edit_box.setText(text)
        self._listed = None
        self._stack.setCurrentWidget(self.edit_box)
        self.edit_box.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.edit_box.selectAll()

    def stop_editing(self) -> None:
        if self.is_editing():
            self.completer.popup().hide()
            self._stack.setCurrentWidget(self.crumbs)

    def _crumb(self, index: int) -> None:
        if 0 <= index < len(self._crumb_locations):
            self.crumb_clicked.emit(self._crumb_locations[index])

    def _entered(self) -> None:
        text = self.edit_box.text()
        self.stop_editing()
        if text.strip():
            self.location_entered.emit(text)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.edit_box:
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self.stop_editing()
                self.parentWidget().setFocus() if self.parentWidget() is not None else None
                return True
            if event.type() == QEvent.Type.FocusOut and event.reason() not in (
                    Qt.FocusReason.PopupFocusReason, Qt.FocusReason.ActiveWindowFocusReason):
                self.stop_editing()
        return False

    # --- completion ---
    def _typed(self, text: str) -> None:
        """Offer the folders of the folder being typed."""
        head, separator, _tail = text.rpartition("/")
        if not separator:
            return
        typed_directory = head + "/"
        if typed_directory == self._listed:
            return
        self._listed = typed_directory
        try:
            directory = places.normalize(typed_directory, self._location)
        except ValueError:
            return
        if places.is_virtual(directory):
            return
        hidden = bool(self.services.settings.get("show_hidden"))

        def listed(names: list[str]) -> None:
            if self._listed != typed_directory or not self.is_editing():
                return
            self._completions.setStringList([typed_directory + name for name in names])
            if self.edit_box.hasFocus() and names:
                self.completer.setCompletionPrefix(self.edit_box.text())
                self.completer.complete()

        self.services.worker.submit(list_folders, directory, hidden, on_done=listed)

    def completions(self) -> list[str]:
        return self._completions.stringList()
