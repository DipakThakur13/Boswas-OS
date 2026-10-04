"""Small widgets and helpers shared by the pages.

Everything that can show backend text is plain text: labels are created with
Qt.PlainText (a QLabel would otherwise guess rich text and render HTML an
installer put in an application name), and message boxes set the format
explicitly. Colours come from the platform palette so light and dark themes
both work; the only fixed colours are the tiny status dots.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSizePolicy,
                               QStyle, QToolButton, QWidget)

SP = QStyle.StandardPixmap

# Status dots (the only hard-coded colours): ok, active, warning, error; neutral uses the palette.
DOT_COLOURS = {"ok": "#2e9d5b", "active": "#2f80d1", "warning": "#d9961a", "error": "#d64545"}
_dot_cache: dict[tuple[str, int], QIcon] = {}


def icon(*names: str, fallback: SP | None = None) -> QIcon:
    """The first icon of the theme that exists, else a Qt standard icon."""
    for name in names:
        if QIcon.hasThemeIcon(name):
            return QIcon.fromTheme(name)
    if fallback is not None:
        return QApplication.style().standardIcon(fallback)
    return QIcon()


def app_icon() -> QIcon:
    return icon("boswas-compat-manager", "applications-system", fallback=SP.SP_ComputerIcon)


def windows_app_icon() -> QIcon:
    return icon("application-x-ms-dos-executable", "application-x-executable", fallback=SP.SP_FileIcon)


def status_dot(level: str, size: int = 12) -> QIcon:
    cached = _dot_cache.get((level, size))
    if cached is not None:
        return cached
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    colour = DOT_COLOURS.get(level)
    painter.setBrush(QColor(colour) if colour else QApplication.palette().color(QPalette.ColorRole.Mid))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    result = QIcon(pixmap)
    _dot_cache[(level, size)] = result
    return result


def plain_label(text: str = "", *, wrap: bool = False, selectable: bool = False,
                parent: QWidget | None = None) -> QLabel:
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    label.setWordWrap(wrap)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def heading(text: str = "", scale: float = 1.35, parent: QWidget | None = None) -> QLabel:
    label = plain_label(text, wrap=True, parent=parent)
    font = QFont(label.font())
    font.setPointSizeF(max(font.pointSizeF(), 9.0) * scale)
    font.setBold(True)
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


class MessageBanner(QFrame):
    """An inline message (like KDE's KMessageWidget): icon, plain text, optional close button."""

    closed = Signal()
    _ICONS = {"info": (("dialog-information",), SP.SP_MessageBoxInformation),
              "ok": (("dialog-positive", "dialog-ok", "emblem-success"), SP.SP_DialogApplyButton),
              "warning": (("dialog-warning",), SP.SP_MessageBoxWarning),
              "error": (("dialog-error",), SP.SP_MessageBoxCritical)}

    def __init__(self, closable: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.AlternateBase)
        self.level = "info"
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 6, 8)
        self.icon = QLabel(self)
        self.icon.setFixedSize(22, 22)
        self.text = plain_label(wrap=True, selectable=True, parent=self)
        self.text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.text, 1)
        self.close_button = None
        if closable:
            self.close_button = QToolButton(self)
            self.close_button.setIcon(icon("window-close", "dialog-close", fallback=SP.SP_TitleBarCloseButton))
            self.close_button.setAutoRaise(True)
            self.close_button.setToolTip("Close")
            self.close_button.clicked.connect(self.dismiss)
            layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        self.hide()

    def show_message(self, level: str, text: str) -> None:
        names, fallback = self._ICONS.get(level, self._ICONS["info"])
        self.level = level
        self.icon.setPixmap(icon(*names, fallback=fallback).pixmap(QSize(22, 22)))
        self.text.setText(text)
        self.show()

    def dismiss(self) -> None:
        self.hide()
        self.closed.emit()


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
