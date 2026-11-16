"""Small building blocks: text, icons, buttons, the toggle switch, progress ring, badges, chips, avatar."""

from __future__ import annotations

import hashlib

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QFontMetrics, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractButton, QLabel, QPushButton, QSizePolicy, QToolButton, QWidget

from .. import icons, paint
from ..theming import current_theme, duration, easing, font, manager, qcolor, repolish

TONES = ("secondary", "muted", "accent", "success", "warning", "danger", "info", "on-accent")


def to_icon(source: QIcon | str | None) -> QIcon:
    """An icon from an icon, an icon name, or nothing."""
    if isinstance(source, QIcon):
        return source
    if isinstance(source, str) and source:
        return icons.icon(source)
    return QIcon()


class Label(QLabel):
    """Text in a step of the type ramp. Always plain text, so text from outside cannot inject markup.

    ``style``: caption, body, body-strong, body-large, subtitle, title, title-large.
    ``tone``: None (the text colour) or secondary, muted, accent, success, warning, danger, info, on-accent.
    """

    def __init__(self, text: str = "", style: str = "body", parent: QWidget | None = None, *,
                 tone: str | None = None, wrap: bool = False, selectable: bool = False) -> None:
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText(text)
        self._style = style
        self.setFont(font(style))
        self.setWordWrap(wrap)
        if tone:
            self.setProperty("tone", tone)
        if selectable:
            self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def set_style(self, style: str) -> None:
        self._style = style
        self.setFont(font(style))

    def set_tone(self, tone: str | None) -> None:
        self.setProperty("tone", tone or "")
        repolish(self)


class IconWidget(QWidget):
    """Shows an icon at a fixed size and repaints it in the colours of the current scheme."""

    def __init__(self, icon: QIcon | str | None = None, size: int = 20, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon = to_icon(icon)
        self._size = size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def icon(self) -> QIcon:
        return self._icon

    def set_icon(self, icon: QIcon | str | None) -> None:
        self._icon = to_icon(icon)
        self.update()

    def set_icon_size(self, size: int) -> None:
        self._size = size
        self.setFixedSize(size, size)

    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    def paintEvent(self, _event) -> None:
        if self._icon.isNull():
            return
        painter = QPainter(self)
        mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
        self._icon.paint(painter, self.rect(), Qt.AlignmentFlag.AlignCenter, mode)


class StatusIcon(QWidget):
    """A status symbol: info, success, warning, danger, attention, neutral or busy (``paint.status_badge``)."""

    LABELS = {"info": "Information", "success": "Success", "warning": "Warning", "danger": "Error",
              "attention": "Attention", "neutral": "Not available", "busy": "Working"}

    def __init__(self, kind: str = "info", size: int = 16, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = paint.status_kind(kind)
        self.setFixedSize(size, size)
        self.setAccessibleName(self.LABELS[self._kind])

    def kind(self) -> str:
        return self._kind

    def set_kind(self, kind: str) -> None:
        self._kind = paint.status_kind(kind)
        self.setAccessibleName(self.LABELS[self._kind])
        self.update()

    def paintEvent(self, _event) -> None:
        paint.status_badge(QPainter(self), self.rect(), self._kind)


class Separator(QWidget):
    """A one-pixel divider line."""

    def __init__(self, orientation: Qt.Orientation = Qt.Orientation.Horizontal, parent: QWidget | None = None):
        super().__init__(parent)
        self._orientation = orientation
        if orientation == Qt.Orientation.Horizontal:
            self.setFixedHeight(1)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            self.setFixedWidth(1)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def paintEvent(self, _event) -> None:
        QPainter(self).fillRect(self.rect(), qcolor("stroke-divider"))


# --- buttons ---------------------------------------------------------------------------------------------

class Button(QPushButton):
    """A push button. ``accent=True`` is the one primary action of a page or dialog; ``subtle=True`` has no
    fill until the pointer is over it."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *, icon: QIcon | str | None = None,
                 accent: bool = False, subtle: bool = False) -> None:
        super().__init__(text, parent)
        if icon is not None:
            self.setIcon(to_icon(icon))
        if accent:
            self.setProperty("accent", True)
        if subtle:
            self.setProperty("subtle", True)

    def set_accent(self, accent: bool) -> None:
        self.setProperty("accent", accent)
        repolish(self)

    def set_subtle(self, subtle: bool) -> None:
        self.setProperty("subtle", subtle)
        repolish(self)


class IconButton(QToolButton):
    """A square button that shows only an icon. The tool tip is also its accessible name, so give one."""

    def __init__(self, icon: QIcon | str | None = None, tooltip: str = "", parent: QWidget | None = None, *,
                 size: int = 32, icon_size: int = 16, checkable: bool = False) -> None:
        super().__init__(parent)
        self.setIcon(to_icon(icon))
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(size, size)
        self.setAutoRaise(True)
        self.setCheckable(checkable)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if tooltip:
            self.setToolTip(tooltip)
            self.setAccessibleName(tooltip)

    def set_icon(self, icon: QIcon | str | None) -> None:
        self.setIcon(to_icon(icon))


class HyperlinkButton(QPushButton):
    """A button that looks like a link. With ``url`` it opens the address (``fluent.open_url``) when clicked."""

    def __init__(self, text: str = "", url: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._url = url
        self.setProperty("hyperlink", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.clicked.connect(self._open)

    def url(self) -> str | None:
        return self._url

    def set_url(self, url: str | None) -> None:
        self._url = url

    def _open(self) -> None:
        if self._url:
            from ..desktop import open_url
            open_url(self._url)


class ToggleSwitch(QAbstractButton):
    """An on/off switch (the ``toggled(bool)`` signal and ``isChecked()`` of QAbstractButton).

    ``name`` is the accessible name (it is not drawn; a SettingsCard names an
    unnamed switch after its title). With ``show_state`` the words ``on_text``
    and ``off_text`` are shown beside the switch, as in Windows Settings.
    Keyboard: Space or Enter toggles.
    """

    TRACK = QSize(40, 20)
    _GAP = 12

    def __init__(self, name: str = "", parent: QWidget | None = None, *, checked: bool = False,
                 on_text: str = "On", off_text: str = "Off", show_state: bool = True) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setText(name)
        if name:
            self.setAccessibleName(name)
        self._on_text, self._off_text, self._show_state = on_text, off_text, show_state
        self._position = 1.0 if checked else 0.0
        self._animation: QVariantAnimation | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.toggled.connect(self._animate)

    def state_text(self) -> str:
        """The word shown for the current state."""
        return self._on_text if self.isChecked() else self._off_text

    def set_state_texts(self, on_text: str, off_text: str) -> None:
        self._on_text, self._off_text = on_text, off_text
        self.updateGeometry()
        self.update()

    def set_show_state(self, show: bool) -> None:
        self._show_state = show
        self.updateGeometry()
        self.update()

    def _label_width(self) -> int:
        if not self._show_state:
            return 0
        metrics = QFontMetrics(self.font())
        return max(metrics.horizontalAdvance(self._on_text), metrics.horizontalAdvance(self._off_text)) + self._GAP

    def sizeHint(self) -> QSize:
        return QSize(self._label_width() + self.TRACK.width(), max(self.TRACK.height() + 4,
                                                                    QFontMetrics(self.font()).height()))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.click()
            return
        super().keyPressEvent(event)

    def _animate(self, checked: bool) -> None:
        end = 1.0 if checked else 0.0
        if self._animation is not None:
            self._animation.stop()
            self._animation = None
        time = duration("fast")
        if time <= 0 or not self.isVisible():
            self._position = end
            self.update()
            return
        animation = QVariantAnimation(self)
        animation.setStartValue(float(self._position))
        animation.setEndValue(end)
        animation.setDuration(time)
        animation.setEasingCurve(easing("decelerate"))
        animation.valueChanged.connect(self._set_position)
        animation.start()
        self._animation = animation

    def _set_position(self, value) -> None:
        self._position = float(value)
        self.update()

    def knob_position(self) -> float:
        """0.0 (off) to 1.0 (on); in between while the switch animates."""
        self._settle()
        return self._position

    def _settle(self) -> None:
        # setChecked() with signals blocked (a page that shows the real
        # state without announcing a change) starts no animation: the knob
        # then simply stands where the state says.
        running = self._animation is not None and self._animation.state() == QVariantAnimation.State.Running
        if not running:
            self._position = 1.0 if self.isChecked() else 0.0

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled, hovered, pressed = self.isEnabled(), self.underMouse(), self.isDown()
        track = QRectF(self.width() - self.TRACK.width(), (self.height() - self.TRACK.height()) / 2,
                       self.TRACK.width(), self.TRACK.height())
        if self._show_state:
            painter.setFont(self.font())
            painter.setPen(qcolor("text" if enabled else "text-disabled"))
            painter.drawText(QRectF(0, 0, track.left() - self._GAP, self.height()),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), self.state_text())
        self._settle()
        position = self._position
        radius = track.height() / 2
        # Off: an outlined track. On: the accent fill fades in over it.
        if position < 1:
            painter.setOpacity(1 - position)
            if enabled:
                fill = "fill-control-pressed" if pressed else ("fill-control-hover" if hovered else "fill-control")
                paint.rounded_rect(painter, track, radius, fill, "stroke-strong")
            else:
                paint.rounded_rect(painter, track, radius, None, "text-disabled")
        if position > 0:
            painter.setOpacity(position)
            if enabled:
                fill = "accent-fill-pressed" if pressed else ("accent-fill-hover" if hovered else "accent")
                paint.rounded_rect(painter, track, radius, fill)
            else:
                paint.rounded_rect(painter, track, radius, "accent-disabled")
        painter.setOpacity(1.0)
        size = 12.0 if not enabled else (14.0 if hovered or pressed else 12.0)
        stretch = 3.0 if pressed and enabled else 0.0
        travel = track.width() - track.height()
        centre = track.left() + radius + travel * position
        if enabled:
            off, on = qcolor("text-secondary"), qcolor("on-accent")
        else:
            off = on = qcolor("text-disabled")
        knob = QRectF(centre - size / 2 - stretch * position, track.center().y() - size / 2, size + stretch, size)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(on if position >= 0.5 else off)
        painter.drawRoundedRect(knob, size / 2, size / 2)


# --- progress ring -----------------------------------------------------------------------------------------

class ProgressRing(QWidget):
    """A circular progress indicator, spinning (indeterminate) or showing ``value`` out of 100.

    The spin runs only while the ring is visible, and not at all when the
    desktop asks for reduced motion (a still three-quarter ring is shown).
    """

    _CYCLE_MS = 1600
    _TICK_MS = 16

    def __init__(self, parent: QWidget | None = None, *, size: int = 32, stroke: float | None = None,
                 indeterminate: bool = True) -> None:
        super().__init__(parent)
        self._size = size
        self._stroke = stroke if stroke is not None else max(2.0, size / 8)
        self._indeterminate = indeterminate
        self._value = 0.0
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(size, size)
        self.setAccessibleName("Working")

    def is_indeterminate(self) -> bool:
        return self._indeterminate

    def set_indeterminate(self, indeterminate: bool) -> None:
        self._indeterminate = indeterminate
        self._sync_timer()
        self.update()

    def value(self) -> float:
        return self._value

    def set_value(self, value: float) -> None:
        """Show ``value`` (0 to 100) and stop spinning."""
        self._value = max(0.0, min(100.0, float(value)))
        self._indeterminate = False
        self._sync_timer()
        self.update()

    def is_animating(self) -> bool:
        return self._timer.isActive()

    def _sync_timer(self) -> None:
        wanted = self._indeterminate and self.isVisible() and not manager().reduced_motion
        if wanted and not self._timer.isActive():
            self._timer.start()
        elif not wanted and self._timer.isActive():
            self._timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def _tick(self) -> None:
        self._phase = (self._phase + self._TICK_MS / self._CYCLE_MS) % 1.0
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        half = self._stroke / 2
        box = QRectF(self.rect()).adjusted(half, half, -half, -half)
        colour = qcolor("accent" if self.isEnabled() else "text-disabled")
        pen = QPen(colour, self._stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        if self._indeterminate:
            painter.setPen(pen)
            if manager().reduced_motion:
                painter.drawArc(box, 90 * 16, -270 * 16)
                return
            # The arc turns twice per cycle while its length swells and shrinks.
            phase = self._phase
            swell = 0.5 - abs(phase - 0.5)                    # 0 -> 0.5 -> 0
            span = 30 + 480 * swell
            start = 90 - 720 * phase - (240 * phase if phase < 0.5 else 240 * (1 - phase))
            painter.drawArc(box, round(start * 16), round(-span * 16))
            return
        painter.setPen(QPen(qcolor("stroke-strong"), self._stroke))
        painter.drawEllipse(box)
        if self._value > 0:
            painter.setPen(pen)
            painter.drawArc(box, 90 * 16, round(-360 * 16 * self._value / 100))


# --- badges and chips ----------------------------------------------------------------------------------

_BADGE_KINDS = ("neutral", "accent", "info", "success", "warning", "danger")


class Badge(QWidget):
    """A small pill with a word or two (Current, Beta, Live session), optionally with an icon.

    ``kind``: neutral, accent, info, success, warning or danger.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None, *, kind: str = "neutral",
                 icon: QIcon | str | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._kind = kind if kind in _BADGE_KINDS else "neutral"
        self._icon = to_icon(icon)
        self.setFont(font("caption"))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(text)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def kind(self) -> str:
        return self._kind

    def set_kind(self, kind: str) -> None:
        self._kind = kind if kind in _BADGE_KINDS else "neutral"
        self.update()

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        icon_width = metrics.height() + 4 if not self._icon.isNull() else 0
        return QSize(metrics.horizontalAdvance(self._text) + icon_width + 18, max(20, metrics.height() + 4))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def colours(self) -> tuple[str, str]:
        """(fill, text) token colour names of the current kind."""
        if self._kind == "accent":
            return "accent", "on-accent"
        if self._kind == "neutral":
            return "fill-control-hover", "text"
        return f"{self._kind}-tint", "text"

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        fill, text = self.colours()
        stroke = None if self._kind in ("accent", "neutral") else paint.status_colour(self._kind)
        paint.rounded_rect(painter, self.rect(), self.height() / 2, fill, stroke)
        x = 8
        metrics = QFontMetrics(self.font())
        if not self._icon.isNull():
            side = metrics.height() - 2
            self._icon.paint(painter, x, (self.height() - side) // 2, side, side)
            x += side + 4
        paint.elided(painter, QRectF(x, 0, self.width() - x - 7, self.height()), self._text, self.font(), text)


class InfoBadge(QWidget):
    """A dot or a count that marks something as new or needing attention.

    ``value``: None for a dot, else a number (more than 99 shows "99+") or a short text.
    ``kind``: attention (the accent colour), info, success, warning or danger.
    """

    def __init__(self, value: int | str | None = None, parent: QWidget | None = None, *,
                 kind: str = "attention") -> None:
        super().__init__(parent)
        self._value = value
        self._kind = kind
        self.setFont(font("caption"))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._describe()

    def value(self) -> int | str | None:
        return self._value

    def set_value(self, value: int | str | None) -> None:
        self._value = value
        self._describe()
        self.updateGeometry()
        self.update()

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self.update()

    def text(self) -> str:
        if self._value is None:
            return ""
        if isinstance(self._value, int) and not isinstance(self._value, bool):
            return "99+" if self._value > 99 else str(self._value)
        return str(self._value)

    def _describe(self) -> None:
        self.setAccessibleName(self.text() or "New")

    def sizeHint(self) -> QSize:
        text = self.text()
        if not text:
            return QSize(8, 8)
        return QSize(max(16, QFontMetrics(self.font()).horizontalAdvance(text) + 8), 16)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        fill = "accent" if self._kind == "attention" else f"{paint.status_colour(self._kind)}-fill"
        paint.rounded_rect(painter, self.rect(), self.height() / 2, fill)
        if self.text():
            paint.elided(painter, self.rect(), self.text(), self.font(), "on-accent",
                         Qt.AlignmentFlag.AlignCenter)


class TagChip(QAbstractButton):
    """A chip: a tag, a filter or a token of a list.

    ``checkable=True`` makes it a filter that stays on (``toggled``).
    ``closable=True`` adds a remove mark; clicking it, or Delete or Backspace
    with the chip focused, emits ``removed`` (the owner takes the chip away).
    """

    removed = Signal()
    _HEIGHT = 28
    _CLOSE = 20

    def __init__(self, text: str = "", parent: QWidget | None = None, *, icon: QIcon | str | None = None,
                 closable: bool = False, checkable: bool = False) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setIcon(to_icon(icon))
        self.setCheckable(checkable)
        self._closable = closable
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(text)

    def is_closable(self) -> bool:
        return self._closable

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        width = metrics.horizontalAdvance(self.text()) + 22
        if not self.icon().isNull():
            width += 20
        if self._closable:
            width += self._CLOSE
        return QSize(width, self._HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _close_rect(self) -> QRectF:
        return QRectF(self.width() - self._CLOSE - 4, (self.height() - self._CLOSE) / 2, self._CLOSE, self._CLOSE)

    def mouseReleaseEvent(self, event) -> None:
        if (self._closable and event.button() == Qt.MouseButton.LeftButton
                and self._close_rect().contains(event.position())):
            self.setDown(False)
            self.removed.emit()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if self._closable and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.removed.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        enabled, hovered, pressed = self.isEnabled(), self.underMouse(), self.isDown()
        radius = current_theme().radius("control")
        if self.isChecked():
            fill = "accent-fill-pressed" if pressed else ("accent-fill-hover" if hovered else "accent")
            paint.rounded_rect(painter, self.rect(), radius, fill if enabled else "accent-disabled")
            text = "on-accent" if enabled else "text-disabled"
        else:
            fill = "fill-control-pressed" if pressed else ("fill-control-hover" if hovered else "fill-control")
            paint.rounded_rect(painter, self.rect(), radius, fill if enabled else "fill-disabled", "stroke-control")
            text = "text" if enabled else "text-disabled"
        x = 10
        if not self.icon().isNull():
            mode = QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled
            self.icon().paint(painter, x, (self.height() - 16) // 2, 16, 16, Qt.AlignmentFlag.AlignCenter, mode)
            x += 20
        right = self._CLOSE + 4 if self._closable else 10
        paint.elided(painter, QRectF(x, 0, self.width() - x - right, self.height()), self.text(), self.font(), text)
        if self._closable:
            paint.glyph(painter, self._close_rect(), "close", text, 10, enabled)


# --- avatar, skeleton ---------------------------------------------------------------------------------

def initials(name: str) -> str:
    """Up to two initials of a name: "Ada Lovelace" -> "AL", "ada" -> "A"."""
    words = [word for word in name.replace("_", " ").replace(".", " ").split() if word[:1].isalnum()]
    if not words:
        return ""
    letters = words[0][0] + (words[-1][0] if len(words) > 1 else "")
    return letters.upper()


class Avatar(QWidget):
    """A person as a circle: their picture, else their initials on one of the icon hues, else a figure."""

    def __init__(self, name: str = "", parent: QWidget | None = None, *, size: int = 32,
                 picture: QPixmap | QImage | str | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._picture: QPixmap | None = None
        self.setFixedSize(size, size)
        self.setAccessibleName(name)
        self.set_picture(picture)

    def name(self) -> str:
        return self._name

    def set_name(self, name: str) -> None:
        self._name = name
        self.setAccessibleName(name)
        self.update()

    def has_picture(self) -> bool:
        return self._picture is not None

    def set_picture(self, picture: QPixmap | QImage | str | None) -> None:
        """A pixmap, an image or the name of an image file; None or an unreadable file shows the initials."""
        if isinstance(picture, str):
            picture = QPixmap(picture)
        elif isinstance(picture, QImage):
            picture = QPixmap.fromImage(picture)
        self._picture = picture if isinstance(picture, QPixmap) and not picture.isNull() else None
        self.update()

    def hue(self) -> str:
        """The icon hue behind the initials (stable for a name)."""
        hues = sorted(current_theme().tokens.get("icon", {}).get("hues", {}))
        if not hues:
            return ""
        return hues[hashlib.sha256(self._name.encode("utf-8")).digest()[0] % len(hues)]

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(self.rect())
        clip = QPainterPath()
        clip.addEllipse(box)
        if self._picture is not None:
            painter.setClipPath(clip)
            side = min(self._picture.width(), self._picture.height())
            source = QRectF((self._picture.width() - side) / 2, (self._picture.height() - side) / 2, side, side)
            painter.drawPixmap(box, self._picture, source)
            return
        theme = current_theme()
        hue = theme.tokens.get("icon", {}).get("hues", {}).get(self.hue())
        fill, text = ("accent", "on-accent") if not hue else (hue["from"], hue["glyph"])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(qcolor(fill, theme))
        painter.drawEllipse(box)
        letters = initials(self._name)
        if letters:
            label = font("body-strong")
            label.setPixelSize(max(9, round(self.height() * 0.4)))
            painter.setFont(label)
            painter.setPen(qcolor(text, theme))
            painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), letters)
            return
        # Nobody named: a plain figure (head and shoulders).
        painter.setBrush(qcolor(text, theme))
        side = box.width()
        painter.drawEllipse(QPointF(side / 2, side * 0.4), side * 0.17, side * 0.17)
        painter.setClipPath(clip)
        painter.drawEllipse(QPointF(side / 2, side * 1.02), side * 0.36, side * 0.36)


class Skeleton(QWidget):
    """A placeholder block shown while content loads. It pulses gently; not when hidden or with reduced motion."""

    def __init__(self, parent: QWidget | None = None, *, width: int | None = None, height: int = 16,
                 radius: int | None = None) -> None:
        super().__init__(parent)
        self._radius = radius
        self._level = 1.0
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(1.0)
        self._animation.setKeyValueAt(0.5, 0.45)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(1400)
        self._animation.setLoopCount(-1)
        self._animation.valueChanged.connect(self._set_level)
        self.setFixedHeight(height)
        if width is not None:
            self.setFixedWidth(width)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Loading")

    def is_animating(self) -> bool:
        return self._animation.state() == QVariantAnimation.State.Running

    def _set_level(self, value) -> None:
        self._level = float(value)
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not manager().reduced_motion:
            self._animation.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._animation.stop()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setOpacity(self._level)
        radius = current_theme().radius("control") if self._radius is None else self._radius
        paint.rounded_rect(painter, self.rect(), radius, "fill-control-hover")
