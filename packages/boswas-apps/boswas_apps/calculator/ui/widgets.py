"""The calculator's own widgets: keys, the display that shrinks its text to fit, list rows.

Everything paints itself with the colours, radii and type ramp of the
toolkit (``fluent.paint`` and token names); there is no colour here.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QPainter
from PySide6.QtWidgets import QAbstractButton, QMenu, QSizePolicy, QWidget

import boswas_fluent as fluent

NUMBER, OPERATOR, ACCENT, SUBTLE = "number", "operator", "accent", "subtle"
_SHRINK_STEPS = ("title-large", "title", "subtitle", "body-large", "body", "caption")


class KeyButton(QAbstractButton):
    """One key of a keypad.

    ``kind``: ``number`` (the brightest keys), ``operator`` (functions and
    operators), ``accent`` (the equals key) or ``subtle`` (no fill until the
    pointer is over it: the memory row, the small switches). A checked key
    is drawn in the accent colour. Space presses the focused key; every
    other key press is left to the window, so Enter stays "equals".
    """

    def __init__(self, text: str, command: str | None = None, kind: str = OPERATOR, tooltip: str = "",
                 parent: QWidget | None = None, *, checkable: bool = False, style: str | None = None,
                 chevron: bool = False) -> None:
        super().__init__(parent)
        self.setText(text)
        self.command = command
        self.kind = kind
        self.style_name = style
        self.chevron = chevron                # a key that opens a list or a flyout
        self.setCheckable(checkable)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName(tooltip or text)
        if tooltip:
            self.setToolTip(tooltip)

    def set_label(self, text: str, tooltip: str = "") -> None:
        self.setText(text)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip or text)
        self.update()

    _CHEVRON = 20                             # room for the chevron and the gap before it

    def _text_width(self) -> int:
        return QFontMetrics(self._font()).horizontalAdvance(self.text()) + (self._CHEVRON if self.chevron else 0)

    def sizeHint(self) -> QSize:
        if self.kind == SUBTLE:
            return QSize(max(40, self._text_width() + 20), 32)
        return QSize(56, 44)

    def minimumSizeHint(self) -> QSize:
        if self.kind == SUBTLE:
            return QSize(max(32, self._text_width() + 12), 28)
        return QSize(32, 28)

    def _font(self):
        if self.style_name:
            return fluent.font(self.style_name)
        if self.kind == SUBTLE:
            return fluent.font("body")
        tall = self.height() >= 40
        if self.kind == NUMBER or len(self.text()) == 1:
            # digits and the one-character operators (÷ × − + =) are the large glyphs of the keypad
            return fluent.font("subtitle" if tall else "body-strong")
        return fluent.font("body-large" if tall else "body")

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and self.isEnabled():
            self.click()
            return
        event.ignore()

    def keyReleaseEvent(self, event) -> None:
        event.ignore()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        theme = fluent.theme()
        enabled, hovered, pressed = self.isEnabled(), self.underMouse(), self.isDown()
        radius = theme.radius("control")
        box = self.rect()
        text = "text"
        if not enabled:
            if self.kind != SUBTLE:
                fluent.paint.rounded_rect(painter, box, radius, "fill-disabled")
            text = "text-disabled"
        elif self.kind == SUBTLE and self.isChecked():
            # a switch that is on: a quiet fill and the accent pill under the word
            fluent.paint.rounded_rect(painter, box, radius, "fill-subtle-pressed" if pressed else "fill-subtle-hover")
            fluent.paint.rounded_rect(painter, QRectF(box.center().x() - 8, box.bottom() - 3, 16, 3), 1.5, "accent")
        elif self.kind == ACCENT or self.isChecked():
            fill = "accent-fill-pressed" if pressed else ("accent-fill-hover" if hovered else "accent")
            fluent.paint.rounded_rect(painter, box, radius, fill)
            text = "on-accent"
        else:
            if self.kind == NUMBER:
                fluent.paint.rounded_rect(painter, box, radius, "surface-raised" if theme.is_dark else "surface",
                                          "stroke-control")
            elif self.kind == OPERATOR:
                fluent.paint.rounded_rect(painter, box, radius, "surface" if theme.is_dark else "layer",
                                          "stroke-control")
            if pressed:
                fluent.paint.rounded_rect(painter, box, radius, "fill-subtle-pressed")
            elif hovered:
                fluent.paint.rounded_rect(painter, box, radius, "fill-subtle-hover")
        area = QRectF(box).adjusted(2, 0, -2, 0)
        if self.chevron:
            width = min(self._text_width(), area.width())
            left = area.center().x() - width / 2
            fluent.paint.elided(painter, QRectF(left, area.top(), width - self._CHEVRON + 4, area.height()),
                                self.text(), self._font(), text,
                                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            fluent.paint.glyph(painter, QRectF(left + width - 12, area.top() + 1, 12, area.height()), "chevron-down",
                               "text-secondary" if enabled else "text-disabled", 10, enabled)
            return
        fluent.paint.elided(painter, area, self.text(), self._font(), text, Qt.AlignmentFlag.AlignCenter)


class FitLabel(QWidget):
    """One line of text, right aligned, in the largest step of the type ramp that fits the width.

    The calculator's result: 40 px for a short number, smaller as the number
    grows, and cut at the left with an ellipsis only when even the smallest
    step does not fit. ``context_requested(global position)`` asks for the
    copy and paste menu; ``clicked`` reports a press.
    """

    clicked = Signal()
    focus_received = Signal()
    context_requested = Signal(QPoint)

    def __init__(self, text: str = "", parent: QWidget | None = None, *, largest: str = "title-large",
                 tone: str = "text", strong_when_active: bool = False, left: bool = False) -> None:
        super().__init__(parent)
        self._left = left                     # left aligned (the converter's fields) instead of right
        self._text = text
        self._steps = _SHRINK_STEPS[_SHRINK_STEPS.index(largest):]
        self._tone = tone
        self._active = True
        self._strong_when_active = strong_when_active
        height = QFontMetrics(fluent.font(largest)).height()
        self.setMinimumHeight(height + 4)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(text)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.setAccessibleName(text)
            self.update()

    def set_tone(self, tone: str) -> None:
        self._tone = tone
        self.update()

    def set_active(self, active: bool) -> None:
        """An inactive label is drawn in the secondary colour (the converter's other field)."""
        self._active = active
        self.update()

    def is_active(self) -> bool:
        return self._active

    def sizeHint(self) -> QSize:
        return QSize(160, self.minimumHeight())

    def fitted_style(self) -> str:
        """The step of the type ramp the text is drawn in at the current width."""
        available = max(self.width() - 4, 1)
        for style in self._steps:
            if QFontMetrics(self._font(style)).horizontalAdvance(self._text) <= available:
                return style
        return self._steps[-1]

    def _font(self, style: str):
        font = fluent.font(style)
        if self._strong_when_active:
            strong = fluent.font("body-strong" if self._active else "body")
            font.setWeight(strong.weight())
        return font

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.focus_received.emit()

    def keyPressEvent(self, event) -> None:
        event.ignore()                     # typing goes to the page the label belongs to

    def contextMenuEvent(self, event) -> None:
        self.context_requested.emit(event.globalPos())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        style = self.fitted_style()
        font = self._font(style)
        colour = self._tone if self._active else "text-secondary"
        box = QRectF(self.rect()).adjusted(2, 0, -2, 0)
        metrics = QFontMetrics(font)
        text = self._text
        if metrics.horizontalAdvance(text) > box.width():
            text = metrics.elidedText(text, Qt.TextElideMode.ElideLeft, int(box.width()))
        painter.setFont(font)
        painter.setPen(fluent.qcolor(colour))
        side = Qt.AlignmentFlag.AlignLeft if self._left else Qt.AlignmentFlag.AlignRight
        painter.drawText(box, int(side | Qt.AlignmentFlag.AlignVCenter), text)


class RecordRow(QAbstractButton):
    """A row of the history or memory list: a small line above a large one, both right aligned."""

    def __init__(self, top: str, bottom: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._top, self._bottom = top, bottom
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(f"{top} {bottom}".strip())
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def texts(self) -> tuple[str, str]:
        return self._top, self._bottom

    def sizeHint(self) -> QSize:
        return QSize(200, 68 if self._top else 48)

    def minimumSizeHint(self) -> QSize:
        return QSize(80, self.sizeHint().height())

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.click()
            return
        event.ignore()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if self.isDown() or self.underMouse():
            fluent.paint.rounded_rect(painter, self.rect(), fluent.theme().radius("control"),
                                      "fill-subtle-pressed" if self.isDown() else "fill-subtle-hover")
        box = QRectF(self.rect()).adjusted(12, 6, -12, -6)
        flags = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if self._top:
            fluent.paint.elided(painter, QRectF(box.left(), box.top(), box.width(), 20), self._top,
                                fluent.font("body"), "text-secondary", flags)
            fluent.paint.elided(painter, QRectF(box.left(), box.top() + 22, box.width(), box.height() - 22),
                                self._bottom, fluent.font("subtitle"), "text", flags)
        else:
            fluent.paint.elided(painter, box, self._bottom, fluent.font("subtitle"), "text", flags)


def copy_paste_menu(parent: QWidget, copy, paste, *, can_paste: bool = True) -> QMenu:
    """The context menu of a display."""
    menu = fluent.MenuFlyout(parent)
    menu.add_item("Copy", copy, shortcut="Ctrl+C")
    menu.add_item("Paste", paste, shortcut="Ctrl+V", enabled=can_paste)
    return menu
