"""Small widgets and helpers shared by the pages, built on boswas_fluent.

Everything that can show backend text is plain text: labels are created with
Qt.PlainText (a QLabel would otherwise guess rich text and render HTML an
installer put in an application name), and message boxes set the format
explicitly. The look comes from the Boswas UI toolkit: every colour is a
design token (the status dots included), the window follows the desktop's
light or dark scheme, and icons come from the Boswas Prism pack.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QStyle, QWidget

SP = QStyle.StandardPixmap

# Status dots: the level of the view model -> the token colour it is drawn in.
DOT_COLOURS = {"ok": "success", "active": "info", "warning": "warning", "error": "danger"}
_dot_cache: dict[tuple[str, int, str], QIcon] = {}


def icon(*names: str, fallback: SP | None = None) -> QIcon:
    """The first of the names that Boswas Prism or the icon theme has, else a Qt standard icon."""
    wanted = [name for name in names if name]
    if wanted:
        found = fluent.icon(wanted[0], *wanted[1:])
        if not found.isNull():
            return found
    if fallback is not None:
        return QApplication.style().standardIcon(fallback)
    return QIcon()


def app_icon() -> QIcon:
    return icon("boswas-compat-manager", "applications-system", fallback=SP.SP_ComputerIcon)


def windows_app_icon() -> QIcon:
    return icon("boswas-windows-application", "application-x-ms-dos-executable", "application-x-executable",
                fallback=SP.SP_FileIcon)


def status_dot(level: str, size: int = 12) -> QIcon:
    """A dot in the token colour of a level (ok, active, warning, error; anything else is neutral)."""
    scheme = fluent.manager().scheme
    cached = _dot_cache.get((level, size, scheme))
    if cached is not None:
        return cached
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(fluent.qcolor(DOT_COLOURS.get(level, "text-muted")))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, size - 4, size - 4)
    painter.end()
    result = QIcon(pixmap)
    _dot_cache[(level, size, scheme)] = result
    return result


def plain_label(text: str = "", *, wrap: bool = False, selectable: bool = False,
                parent: QWidget | None = None) -> QLabel:
    return fluent.Label(text, "body", parent, wrap=wrap, selectable=selectable)


def muted_label(text: str = "", *, wrap: bool = True, parent: QWidget | None = None) -> QLabel:
    """Secondary text."""
    return fluent.Label(text, "body", parent, tone="secondary", wrap=wrap)


def heading(text: str = "", scale: float = 1.35, parent: QWidget | None = None) -> QLabel:
    """A heading inside a page or dialog: the subtitle step of the type ramp (body-large below scale 1.3)."""
    label = fluent.Label(text, "subtitle" if scale >= 1.3 else "body-large", parent, wrap=True)
    if scale < 1.3:
        font = label.font()
        font.setWeight(fluent.font("body-strong").weight())
        label.setFont(font)
    return label


def icon_label(qicon: QIcon, size: int = 16, parent: QWidget | None = None) -> QLabel:
    label = QLabel(parent)
    label.setPixmap(qicon.pixmap(QSize(size, size)))
    label.setFixedSize(size, size)
    return label


def dot_label(level: str, parent: QWidget | None = None) -> QLabel:
    label = icon_label(status_dot(level), 12, parent)
    label.setProperty("level", level)
    return label


def set_dot(label: QLabel, level: str) -> None:
    label.setPixmap(status_dot(level).pixmap(QSize(12, 12)))
    label.setProperty("level", level)


def button(text: str, qicon: QIcon | None = None, parent: QWidget | None = None) -> QPushButton:
    widget = QPushButton(text, parent)
    if qicon is not None:
        widget.setIcon(qicon)
    return widget


class MessageBanner(fluent.InfoBar):
    """An inline message: the toolkit's InfoBar under the names the pages use (level, text, icon)."""

    _SEVERITY = {"info": "info", "ok": "success", "warning": "warning", "error": "error"}

    def __init__(self, closable: bool = True, parent: QWidget | None = None):
        super().__init__("info", parent=parent, closable=closable)
        self.level = "info"
        self.icon = self.status_icon
        self.text = self.message_label
        self.hide()

    def show_message(self, level: str, text: str) -> None:
        self.level = level
        super().show_message(self._SEVERITY.get(level, "info"), text)


def message_box(parent: QWidget | None, level: str, title: str, text: str) -> QMessageBox:
    """A message box whose text is always plain (backend messages are untrusted)."""
    icons = {"info": QMessageBox.Icon.Information, "warning": QMessageBox.Icon.Warning,
             "error": QMessageBox.Icon.Critical, "question": QMessageBox.Icon.Question}
    box = QMessageBox(parent)
    box.setIcon(icons.get(level, QMessageBox.Icon.Information))
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    return box


def export_text(path: str, text: str) -> None:
    """Write text the user chose to export (a log) to the file the user picked in a save dialog."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text if text.endswith("\n") or not text else text + "\n")
