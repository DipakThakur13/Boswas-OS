"""Widgets shared by the pages.

Colours come from the platform palette (the KDE colour scheme, so a preset
recolours the window); cards, tiles and badges blend palette roles. The only
fixed colours are the status icon tints, and every status icon has its own
shape and always comes with a text label, so no state is told by colour
alone. Text from outside is always shown as plain text.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox,
                               QSizePolicy, QStyle, QToolButton, QVBoxLayout, QWidget)

from .. import viewmodel as vm

SP = QStyle.StandardPixmap
MARK_PATH = "/usr/share/boswas/branding/boswas-os-mark.svg"

# Status tints (the only fixed colours); each state also has its own glyph and a text label.
STATUS_TINTS = {vm.SECURE: "#2e9d5b", vm.WARNING: "#e0861a", vm.ATTENTION: "#d64545", vm.ERROR: "#b03a48",
                vm.INFO: "#2f80d1"}
GLYPH = "#ffffff"
_status_cache: dict[tuple, QPixmap] = {}


def icon(*names: str, fallback: SP | None = None) -> QIcon:
    """The first icon of the theme that exists, else a Qt standard icon."""
    for name in names:
        if name and QIcon.hasThemeIcon(name):
            return QIcon.fromTheme(name)
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


def mix(a: QColor, b: QColor, amount: float) -> QColor:
    """a blended towards b (amount 0..1)."""
    return QColor(round(a.red() + (b.red() - a.red()) * amount), round(a.green() + (b.green() - a.green()) * amount),
                  round(a.blue() + (b.blue() - a.blue()) * amount))


def neutral_colour(palette: QPalette) -> QColor:
    return mix(palette.color(QPalette.ColorRole.WindowText), palette.color(QPalette.ColorRole.Window), 0.45)


def border_colour(palette: QPalette) -> QColor:
    return mix(palette.color(QPalette.ColorRole.Window), palette.color(QPalette.ColorRole.WindowText), 0.16)


def status_pixmap(state: str, size: int = 20, palette: QPalette | None = None) -> QPixmap:
    """A status glyph: check (secure), triangle (warning), ! (attention), x (error), i (info), - (not available)."""
    palette = palette or QApplication.palette()
    tint = QColor(STATUS_TINTS[state]) if state in STATUS_TINTS else neutral_colour(palette)
    ratio = max(1.0, QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0)
    cache_key = (state, size, tint.name(), ratio)
    cached = _status_cache.get(cache_key)
    if cached is not None:
        return cached
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)

    def pt(x: float, y: float) -> QPointF:
        return QPointF(x * s, y * s)

    pen = QPen(QColor(GLYPH), max(1.6, s * 0.12), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
               Qt.PenJoinStyle.RoundJoin)
    if state == vm.CHECKING:
        p.setPen(QPen(tint, max(1.5, s * 0.11), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(QRectF(s * 0.14, s * 0.14, s * 0.72, s * 0.72), 90 * 16, -270 * 16)
    elif state == vm.WARNING:
        path = QPainterPath(pt(0.5, 0.07))
        path.lineTo(pt(0.97, 0.9))
        path.lineTo(pt(0.03, 0.9))
        path.closeSubpath()
        p.setPen(QPen(tint, max(1.0, s * 0.06), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.setBrush(tint)
        p.drawPath(path)
        p.setPen(pen)
        p.drawLine(pt(0.5, 0.37), pt(0.5, 0.6))
        p.drawPoint(pt(0.5, 0.76))
    else:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tint)
        p.drawEllipse(QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92))
        p.setPen(pen)
        if state == vm.SECURE:
            path = QPainterPath(pt(0.29, 0.52))
            path.lineTo(pt(0.44, 0.66))
            path.lineTo(pt(0.72, 0.36))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        elif state == vm.ATTENTION:
            p.drawLine(pt(0.5, 0.27), pt(0.5, 0.56))
            p.drawPoint(pt(0.5, 0.73))
        elif state == vm.ERROR:
            p.drawLine(pt(0.35, 0.35), pt(0.65, 0.65))
            p.drawLine(pt(0.65, 0.35), pt(0.35, 0.65))
        elif state == vm.INFO:
            p.drawPoint(pt(0.5, 0.28))
            p.drawLine(pt(0.5, 0.45), pt(0.5, 0.73))
        else:
            p.drawLine(pt(0.32, 0.5), pt(0.68, 0.5))
    p.end()
    _status_cache[cache_key] = pixmap
    return pixmap


# --- labels ---------------------------------------------------------------------------------------------

def plain_label(text: str = "", *, wrap: bool = False, selectable: bool = False,
                parent: QWidget | None = None) -> QLabel:
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    label.setWordWrap(wrap)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def scaled_font(base: QFont, scale: float = 1.0, bold: bool = False, weight: QFont.Weight | None = None) -> QFont:
    font = QFont(base)
    if scale != 1.0:
        font.setPointSizeF(max(base.pointSizeF(), 8.0) * scale)
    if weight is not None:
        font.setWeight(weight)
    elif bold:
        font.setBold(True)
    return font


def heading(text: str = "", scale: float = 1.6, parent: QWidget | None = None) -> QLabel:
    label = plain_label(text, wrap=True, parent=parent)
    label.setFont(scaled_font(label.font(), scale, weight=QFont.Weight.DemiBold))
    label.setAccessibleName(text)
    return label


def title_label(text: str = "", scale: float = 1.05, parent: QWidget | None = None) -> QLabel:
    label = plain_label(text, wrap=True, parent=parent)
    label.setFont(scaled_font(label.font(), scale, weight=QFont.Weight.DemiBold))
    return label


def muted_label(text: str = "", *, wrap: bool = True, selectable: bool = False,
                parent: QWidget | None = None) -> QLabel:
    """Secondary text in the palette's inactive text colour (KDE: ForegroundInactive)."""
    label = plain_label(text, wrap=wrap, selectable=selectable, parent=parent)
    label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return label


class StatusIcon(QLabel):
    def __init__(self, state: str = vm.CHECKING, size: int = 20, parent: QWidget | None = None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.set_state(state)

    def set_state(self, state: str) -> None:
        self.state = state
        self.setPixmap(status_pixmap(state, self._size, self.palette()))
        self.setAccessibleName(vm.STATE_LABELS.get(state, ""))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == event.Type.PaletteChange:
            self.setPixmap(status_pixmap(self.state, self._size, self.palette()))


# --- containers -----------------------------------------------------------------------------------------

class Card(QFrame):
    """A rounded panel in the palette's view colour, with an optional title."""

    def __init__(self, title: str = "", parent: QWidget | None = None, margins: int = 16, spacing: int = 10):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(margins, margins - 2, margins, margins)
        self.layout_.setSpacing(spacing)
        self.title = None
        if title:
            self.title = title_label(title, parent=self)
            self.layout_.addWidget(self.title)
            self.setAccessibleName(title)

    def addWidget(self, widget: QWidget, stretch: int = 0) -> None:
        self.layout_.addWidget(widget, stretch)

    def addLayout(self, layout, stretch: int = 0) -> None:
        self.layout_.addLayout(layout, stretch)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        p.setPen(QPen(border_colour(palette), 1))
        p.setBrush(palette.color(QPalette.ColorRole.Base))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)


class Separator(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), border_colour(self.palette()))


class Badge(QWidget):
    """A small pill with text and an optional icon (tags such as Light, Dark, Current, Live session)."""

    def __init__(self, text: str, qicon: QIcon | None = None, strong: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.text, self.qicon, self.strong = text, qicon, strong
        self.setFont(scaled_font(self.font(), 0.9, weight=QFont.Weight.DemiBold))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(text)
        self.setToolTip(text)

    def set_text(self, text: str) -> None:
        self.text = text
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        icon_w = fm.height() + 4 if self.qicon is not None else 0
        return QSize(fm.horizontalAdvance(self.text) + icon_w + 18, fm.height() + 6)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        window, accent = palette.color(QPalette.ColorRole.Window), palette.color(QPalette.ColorRole.Highlight)
        if self.strong:
            fill, text = accent, palette.color(QPalette.ColorRole.HighlightedText)
        else:
            fill, text = mix(window, accent, 0.22), palette.color(QPalette.ColorRole.WindowText)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        x = 9
        fm = QFontMetrics(self.font())
        if self.qicon is not None:
            side = fm.height() - 2
            self.qicon.paint(p, x, (self.height() - side) // 2, side, side)
            x += side + 4
        p.setPen(text)
        p.drawText(QRectF(x, 0, self.width() - x - 8, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text)


class StatusLine(QWidget):
    """One status row: icon, title, detail and the state in words."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.title_text = title
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 9, 6, 9)
        layout.setSpacing(12)
        self.icon = StatusIcon(vm.CHECKING, 22, self)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = title_label(title, 1.0, self)
        self.detail = muted_label("Checking…", selectable=True, parent=self)
        text.addWidget(self.title)
        text.addWidget(self.detail)
        self.state_label = plain_label(vm.STATE_LABELS[vm.CHECKING], parent=self)
        self.state_label.setFont(scaled_font(self.state_label.font(), weight=QFont.Weight.DemiBold))
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.state_label.setMinimumWidth(QFontMetrics(self.state_label.font()).horizontalAdvance("Not available") + 8)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(text, 1)
        layout.addWidget(self.state_label, 0, Qt.AlignmentFlag.AlignTop)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

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

    def paintEvent(self, event) -> None:
        if self.hasFocus():
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(self.palette().color(QPalette.ColorRole.Highlight), 2))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)


class SummaryCard(Card):
    """The headline of a page: large status icon, headline and explanation."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent=parent, margins=18)
        row = QHBoxLayout()
        row.setSpacing(16)
        self.icon = StatusIcon(vm.CHECKING, 44, self)
        text = QVBoxLayout()
        text.setSpacing(3)
        self.headline = title_label("Checking…", 1.3, self)
        self.detail = muted_label("", parent=self)
        text.addWidget(self.headline)
        text.addWidget(self.detail)
        text.addStretch(1)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
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
        self.grid.setHorizontalSpacing(24)
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
        layout.setSpacing(6)
        self.toggle = QToolButton(self)
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setAutoRaise(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setFont(scaled_font(self.toggle.font(), weight=QFont.Weight.DemiBold))
        self.toggle.setAccessibleName(title)
        self.toggle.setAccessibleDescription("Shows or hides this section")
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(22, 0, 0, 4)
        self.content.hide()
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.content)
        self.toggle.toggled.connect(self.set_open)

    def set_open(self, opened: bool) -> None:
        if self.toggle.isChecked() != opened:
            self.toggle.setChecked(opened)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
        self.content.setVisible(opened)

    def is_open(self) -> bool:
        return self.content.isVisibleTo(self)


class MessageBanner(QFrame):
    """An inline message (like KDE's KMessageWidget): status icon, plain text, optional close button."""

    closed = Signal()
    LEVELS = {"info": vm.INFO, "ok": vm.SECURE, "warning": vm.WARNING, "error": vm.ERROR}

    def __init__(self, closable: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        self.level = "info"
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 8, 9)
        layout.setSpacing(10)
        self.icon = StatusIcon(vm.INFO, 20, self)
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
            self.close_button.setAccessibleName("Close message")
            self.close_button.clicked.connect(self.dismiss)
            layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        self.hide()

    def show_message(self, level: str, text: str) -> None:
        self.level = level
        self.icon.set_state(self.LEVELS.get(level, vm.INFO))
        self.text.setText(text)
        self.setAccessibleName(text)
        self.show()
        self.update()

    def dismiss(self) -> None:
        self.hide()
        self.closed.emit()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        tint = QColor(STATUS_TINTS.get(self.LEVELS.get(self.level, vm.INFO), STATUS_TINTS[vm.INFO]))
        window = palette.color(QPalette.ColorRole.Window)
        p.setPen(QPen(mix(window, tint, 0.55), 1))
        p.setBrush(mix(window, tint, 0.12))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)


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
