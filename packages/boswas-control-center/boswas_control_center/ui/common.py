"""Widgets shared by the pages, built on boswas_fluent.

The look comes from the Boswas UI toolkit (boswas_fluent): every colour,
radius and font size is a design token, the window follows the desktop's
light or dark scheme, and icons come from the Boswas Prism pack. Nothing
here holds a colour of its own. Every status icon has its own shape and
always comes with a text label, so no state is told by colour alone. Text
from outside is always shown as plain text.
"""

from __future__ import annotations

import os

import boswas_fluent as fluent
from boswas_fluent import paint
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QSizePolicy, QStyle,
                               QToolButton, QVBoxLayout, QWidget)

from .. import viewmodel as vm

SP = QStyle.StandardPixmap
MARK_PATH = "/usr/share/boswas/branding/boswas-os-mark.svg"

# How the states of the view model are drawn: one shape and one token colour each (fluent.paint.status_badge).
STATUS_KINDS = {vm.SECURE: "success", vm.WARNING: "warning", vm.ATTENTION: "attention", vm.ERROR: "danger",
                vm.INFO: "info", vm.CHECKING: "busy"}
_status_cache: dict[tuple, QPixmap] = {}


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


def mark_icon() -> QIcon:
    """The Boswas OS mark (boswas-branding), else the theme's logo icons."""
    if os.path.isfile(MARK_PATH):
        mark = QIcon(MARK_PATH)
        if not mark.pixmap(QSize(32, 32)).isNull():
            return mark
    return icon("boswas-logo", "boswas-control-center", "preferences-system", fallback=SP.SP_ComputerIcon)


def app_icon() -> QIcon:
    themed = icon("boswas-control-center")
    return themed if not themed.isNull() else mark_icon()


def neutral_colour(palette: QPalette | None = None) -> QColor:
    """The colour of muted text (the palette argument is kept for callers; the colour is a token)."""
    return fluent.qcolor("text-muted")


def border_colour(palette: QPalette | None = None) -> QColor:
    """The one-pixel outline of a surface (the palette argument is kept for callers; the colour is a token)."""
    return fluent.qcolor("stroke-surface")


def status_pixmap(state: str, size: int = 20, palette: QPalette | None = None) -> QPixmap:
    """A status glyph: check (secure), triangle (warning), ! (attention), x (error), i (info), - (not available)."""
    app = QApplication.instance()
    ratio = max(1.0, app.devicePixelRatio() if app else 1.0)
    kind = STATUS_KINDS.get(state, "neutral")
    cache_key = (kind, size, fluent.manager().scheme, ratio)
    cached = _status_cache.get(cache_key)
    if cached is not None:
        return cached
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    paint.status_badge(painter, QRect(0, 0, size, size), kind)      # logical pixels: the pixmap has the ratio
    painter.end()
    _status_cache[cache_key] = pixmap
    return pixmap


# --- labels ---------------------------------------------------------------------------------------------

def plain_label(text: str = "", *, wrap: bool = False, selectable: bool = False,
                parent: QWidget | None = None) -> QLabel:
    return fluent.Label(text, "body", parent, wrap=wrap, selectable=selectable)


def scaled_font(base: QFont, scale: float = 1.0, bold: bool = False, weight: QFont.Weight | None = None) -> QFont:
    font = QFont(base)
    if scale != 1.0:
        if base.pixelSize() > 0:
            font.setPixelSize(max(round(base.pixelSize() * scale), 9))
        else:
            font.setPointSizeF(max(base.pointSizeF(), 8.0) * scale)
    if weight is not None:
        font.setWeight(weight)
    elif bold:
        font.setBold(True)
    return font


def heading(text: str = "", scale: float = 1.6, parent: QWidget | None = None) -> QLabel:
    """A heading: the title step of the type ramp for a page (scale 1.6 and more), else the subtitle step."""
    label = fluent.Label(text, "title" if scale >= 1.6 else "subtitle", parent, wrap=True)
    label.setAccessibleName(text)
    return label


def title_label(text: str = "", scale: float = 1.05, parent: QWidget | None = None) -> QLabel:
    """The title of a card or row (body-strong; from scale 1.2 the larger body-large step, semibold)."""
    label = fluent.Label(text, "body-strong", parent, wrap=True)
    if scale >= 1.2:
        font = fluent.font("body-large")
        font.setWeight(fluent.font("body-strong").weight())
        label.setFont(font)
    return label


def muted_label(text: str = "", *, wrap: bool = True, selectable: bool = False,
                parent: QWidget | None = None) -> QLabel:
    """Secondary text."""
    return fluent.Label(text, "body", parent, tone="secondary", wrap=wrap, selectable=selectable)


class StatusIcon(QLabel):
    def __init__(self, state: str = vm.CHECKING, size: int = 20, parent: QWidget | None = None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.set_state(state)

    def set_state(self, state: str) -> None:
        self.state = state
        self.setPixmap(status_pixmap(state, self._size))
        self.setAccessibleName(vm.STATE_LABELS.get(state, ""))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (event.Type.PaletteChange, event.Type.StyleChange):
            self.setPixmap(status_pixmap(self.state, self._size))


# --- containers -----------------------------------------------------------------------------------------

class Card(fluent.Card):
    """A card of the toolkit with the names the pages use: ``layout_``, ``title``, ``addWidget``, ``addLayout``."""

    def __init__(self, title: str = "", parent: QWidget | None = None, margins: int = 16, spacing: int = 10):
        super().__init__(parent, title=title, padding=margins, spacing=spacing)
        self.layout_ = self.body
        self.title = self.title_label

    def addWidget(self, widget: QWidget, stretch: int = 0) -> None:
        self.layout_.addWidget(widget, stretch)

    def addLayout(self, layout, stretch: int = 0) -> None:
        self.layout_.addLayout(layout, stretch)


class Separator(fluent.Separator):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(Qt.Orientation.Horizontal, parent)


class Badge(fluent.Badge):
    """A small pill with text and an optional icon (tags such as Light, Dark, Current, Live session)."""

    def __init__(self, text: str, qicon: QIcon | None = None, strong: bool = False, parent: QWidget | None = None):
        super().__init__(text, parent, kind="accent" if strong else "neutral", icon=qicon)
        self.qicon, self.strong = qicon, strong
        self.setToolTip(text)

    def set_text(self, text: str) -> None:
        super().set_text(text)
        self.setToolTip(text)


class StatusLine(QWidget):
    """One status row: icon, title, detail and the state in words."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.title_text = title
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 10, 4, 10)
        layout.setSpacing(16)
        self.icon = StatusIcon(vm.CHECKING, 20, self)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.title = fluent.Label(title, "body", self, wrap=True)
        self.detail = fluent.Label("Checking…", "caption", self, tone="secondary", wrap=True, selectable=True)
        text.addWidget(self.title)
        text.addWidget(self.detail)
        self.state_label = fluent.Label(vm.STATE_LABELS[vm.CHECKING], "body", self, tone="secondary")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.state_label.setMinimumWidth(QFontMetrics(self.state_label.font()).horizontalAdvance("Not available") + 8)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(text, 1)
        layout.addWidget(self.state_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)          # the toolkit draws the focus ring

    def set_status(self, state: str, detail: str, title: str | None = None) -> None:
        if title is not None:
            self.title_text = title
            self.title.setText(title)
        self.state = state
        self.icon.set_state(state)
        self.detail.setText(detail)
        self.state_label.setText(vm.STATE_LABELS.get(state, ""))
        self.setAccessibleName(f"{self.title_text}: {vm.STATE_LABELS.get(state, '')}")
        self.setAccessibleDescription(detail)


class SummaryCard(Card):
    """The headline of a page: large status icon, headline and explanation."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent=parent, margins=16)
        row = QHBoxLayout()
        row.setSpacing(16)
        self.icon = StatusIcon(vm.CHECKING, 40, self)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.headline = fluent.Label("Checking…", "subtitle", self, wrap=True)
        self.detail = fluent.Label("", "body", self, tone="secondary", wrap=True)
        text.addStretch(1)
        text.addWidget(self.headline)
        text.addWidget(self.detail)
        text.addStretch(1)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(text, 1)
        self.actions = QHBoxLayout()
        row.addLayout(self.actions)
        self.addLayout(row)

    def set_summary(self, summary: vm.Summary) -> None:
        self.icon.set_state(summary.state)
        self.headline.setText(summary.headline)
        self.detail.setText(summary.detail)
        self.detail.setVisible(bool(summary.detail))
        self.setAccessibleName(f"{summary.headline}. {vm.STATE_LABELS.get(summary.state, '')}")
        self.setAccessibleDescription(summary.detail)


class InfoGrid(QWidget):
    """Label: value rows (values are plain, selectable text)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(32)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(1, 1)
        self.labels: list[tuple[QLabel, QLabel]] = []

    def set_rows(self, rows: list[tuple[str, str]]) -> None:
        while len(self.labels) < len(rows):
            row = len(self.labels)
            key = muted_label("", wrap=False, parent=self)
            value = plain_label("", wrap=True, selectable=True, parent=self)
            self.grid.addWidget(key, row, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(value, row, 1)
            self.labels.append((key, value))
        for index, (key, value) in enumerate(self.labels):
            visible = index < len(rows)
            key.setVisible(visible)
            value.setVisible(visible)
            if visible:
                key.setText(rows[index][0])
                value.setText(rows[index][1])
                value.setAccessibleName(rows[index][0])

    def value(self, label: str) -> str | None:
        for key, value in self.labels:
            if not key.isHidden() and key.text() == label:
                return value.text()
        return None

    def rows(self) -> list[tuple[str, str]]:
        return [(k.text(), v.text()) for k, v in self.labels if not k.isHidden()]


class Collapsible(QWidget):
    """A titled section that opens and closes (keyboard: Space or Enter on the title)."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.toggle = QToolButton(self)
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setAutoRaise(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setIconSize(QSize(12, 12))
        self.toggle.setIcon(fluent.glyph_icon("chevron-right"))
        self.toggle.setFont(fluent.font("body-strong"))
        self.toggle.setAccessibleName(title)
        self.toggle.setAccessibleDescription("Shows or hides this section")
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(24, 0, 0, 4)
        self.content.hide()
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.content)
        self.toggle.toggled.connect(self.set_open)

    def set_open(self, opened: bool) -> None:
        if self.toggle.isChecked() != opened:
            self.toggle.setChecked(opened)
        self.toggle.setIcon(fluent.glyph_icon("chevron-down" if opened else "chevron-right"))
        self.content.setVisible(opened)

    def is_open(self) -> bool:
        return self.content.isVisibleTo(self)


class MessageBanner(fluent.InfoBar):
    """An inline message: the toolkit's InfoBar under the names the pages use (level, text, icon)."""

    LEVELS = {"info": vm.INFO, "ok": vm.SECURE, "warning": vm.WARNING, "error": vm.ERROR}
    _SEVERITY = {"info": "info", "ok": "success", "warning": "warning", "error": "error"}

    def __init__(self, closable: bool = True, parent: QWidget | None = None):
        super().__init__("info", parent=parent, closable=closable)
        self.level = "info"
        self.icon = self.status_icon
        self.text = self.message_label
        self.hide()

    def show_message(self, level: str, text: str) -> None:      # noqa: D401 (the pages' two-argument form)
        self.level = level
        super().show_message(self._SEVERITY.get(level, "info"), text)


def message_box(parent: QWidget | None, level: str, title: str, text: str, informative: str = "") -> QMessageBox:
    """A message box whose text is always plain."""
    icons = {"info": QMessageBox.Icon.Information, "warning": QMessageBox.Icon.Warning,
             "error": QMessageBox.Icon.Critical, "question": QMessageBox.Icon.Question}
    box = QMessageBox(parent)
    box.setIcon(icons.get(level, QMessageBox.Icon.Information))
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    return box
