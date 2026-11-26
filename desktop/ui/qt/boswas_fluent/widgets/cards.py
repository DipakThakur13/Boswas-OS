"""Surfaces and rows: Card, SettingsCard, Expander, SettingsExpander, InfoBar, ListItem, EmptyState."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter, QPainterPath
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLayout, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from .. import glass, icons, paint
from ..animation import Animation
from ..theming import current_theme, duration, easing, qcolor
from ..tokens import DERIVED
from .basic import Button, IconButton, IconWidget, Label, StatusIcon, to_icon

_WIDGET_MAX = 16777215


class Card(QFrame):
    """A rounded surface for a group of content. ``card.body`` is its vertical layout."""

    def __init__(self, parent: QWidget | None = None, *, title: str = "", padding: int = 16,
                 spacing: int = 12) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding, padding, padding)
        self.body.setSpacing(spacing)
        self.title_label: Label | None = None
        if title:
            self.title_label = Label(title, "body-strong", self)
            self.body.addWidget(self.title_label)
            self.setAccessibleName(title)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        """Append a widget to the card; returns it."""
        self.body.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout: QLayout, stretch: int = 0) -> QLayout:
        self.body.addLayout(layout, stretch)
        return layout

    def paintEvent(self, _event) -> None:
        paint.card(QPainter(self), self.rect())


class RowButton(QAbstractButton):
    """The click target of a row: fills its parent, takes the focus, paints the hover and pressed layers.

    It draws nothing else; the row's labels and icons lie above it and let
    the pointer through. Keyboard: Space or Enter.
    """

    def __init__(self, parent: QWidget, radius: float | None = None) -> None:
        super().__init__(parent)
        self.radius = radius
        self.corners = "all"              # "all", "top" or "none": which corners of the fill are round
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lower()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.click()
            return
        super().keyPressEvent(event)

    def fluent_focus_rect(self) -> tuple[QWidget | None, QRect]:
        row = self.parentWidget()
        return row.parentWidget(), row.geometry()

    def paintEvent(self, _event) -> None:
        if not self.isEnabled() or not (self.isDown() or self.underMouse()):
            return
        row = self.parentWidget()
        if getattr(row, "paints_row_state", lambda: False)():
            return                        # the row draws its own state (a SettingsCard on its own)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = current_theme().radius("card") if self.radius is None else self.radius
        box = QRectF(self.rect())
        path = QPainterPath()
        if self.corners == "all":
            path.addRoundedRect(box, radius, radius)
        elif self.corners == "top":
            path.addRoundedRect(box, radius, radius)
            path.addRect(QRectF(box.left(), box.center().y(), box.width(), box.height() / 2))
            path = path.simplified()
        else:
            path.addRect(box)
        painter.fillPath(path, qcolor("fill-subtle-pressed" if self.isDown() else "fill-subtle-hover"))


class SettingsCard(QFrame):
    """One row of a settings page: icon, title, description and a control at the trailing edge.

    With ``clickable=True`` the whole row is a button (``clicked``, ``click()``,
    Space or Enter) and shows a chevron; use it for rows that open something.
    ``card.button`` is that button (None for a row that is not clickable).

    The card is Boswas glass while glass is on (``paint.glass``). A clickable
    card shows the grey hover and pressed layers; ``set_hover_highlight(True)``
    gives them to a card that is not clickable too (the rows of a list of
    applications). ``set_icon_magnification(m)`` reserves room for the icon to
    grow to ``m`` times its size without moving the text (``IconMagnifier``).
    """

    clicked = Signal()
    MIN_HEIGHT = 68
    ROW_MIN_HEIGHT = 52

    def __init__(self, title: str = "", description: str = "", icon: QIcon | str | None = None,
                 control: QWidget | None = None, parent: QWidget | None = None, *, clickable: bool = False) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._mode = "card"
        self._hover_highlight = False
        self._pressed = False
        self._icon_reserve = 1.0
        self._control: QWidget | None = None
        self.button: RowButton | None = None
        self._action_glyph = "chevron-right"
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(16, 12, 16, 12)
        self._layout.setSpacing(16)
        self.icon_widget = IconWidget(None, 20, self)
        self.icon_widget.hide()
        self._layout.addWidget(self.icon_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        self.title_label = Label(title, "body", self)
        self.title_label.setWordWrap(True)
        self.description_label = Label(description, "caption", self, tone="secondary", wrap=True)
        for label in (self.title_label, self.description_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        text.addStretch(1)
        text.addWidget(self.title_label)
        text.addWidget(self.description_label)
        text.addStretch(1)
        self._layout.addLayout(text, 1)
        self._control_row = QHBoxLayout()
        self._control_row.setContentsMargins(0, 0, 0, 0)
        self._control_row.setSpacing(8)
        self._layout.addLayout(self._control_row)
        self.action_widget = IconWidget(None, 12, self)
        self.action_widget.hide()
        self._layout.addWidget(self.action_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setMinimumHeight(self.MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.description_label.setVisible(bool(description))
        self.set_title(title)
        self.set_icon(icon)
        if control is not None:
            self.set_control(control)
        if clickable:
            self.set_clickable(True)

    # --- content ---
    def title(self) -> str:
        return self.title_label.text()

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)
        self.setAccessibleName(title)
        if self.button is not None:
            self.button.setAccessibleName(title)
            self.button.setText(title)
        if self._control is not None and not self._control.accessibleName():
            self._control.setAccessibleName(title)

    def description(self) -> str:
        return self.description_label.text()

    def set_description(self, description: str) -> None:
        self.description_label.setText(description)
        self.description_label.setVisible(bool(description))
        self.setAccessibleDescription(description)
        if self.button is not None:
            self.button.setAccessibleDescription(description)

    def set_icon(self, icon: QIcon | str | None, size: int | None = None) -> None:
        qicon = to_icon(icon)
        self.icon_widget.set_icon(qicon)
        if size is not None:                  # else the size it has (20 unless it was changed)
            self.icon_widget.set_icon_size(size)
            self._sync_margins()
        self.icon_widget.setVisible(not qicon.isNull())

    def set_icon_magnification(self, reserve: float) -> None:
        """Reserve room for the icon to grow to ``reserve`` times its size; the text stays where it was."""
        self._icon_reserve = max(1.0, float(reserve))
        self.icon_widget.set_magnification(self._icon_reserve)
        self._sync_margins()

    def _sync_margins(self) -> None:
        """Margins and spacing so that the icon's centre and the text stay where they are without room."""
        extra = (self.icon_widget.cell() - self.icon_widget.icon_size()) / 2
        left, top, right, bottom = (52, 8, 16, 8) if self._mode == "row" else (16, 12, 16, 12)
        self._layout.setContentsMargins(max(0, round(left - extra)), top, right, bottom)
        self._layout.setSpacing(max(0, round(16 - extra)))

    # --- the pointer ---
    def hover_highlight(self) -> bool:
        return self._hover_highlight

    def set_hover_highlight(self, on: bool) -> None:
        """Show the hover and pressed layers although the card is not clickable."""
        self._hover_highlight = bool(on)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, self._hover_highlight)
        self.update()

    def paints_row_state(self) -> bool:
        """Whether the card draws its hover and pressed layers itself (on its own, not inside an expander)."""
        return self._mode == "card"

    def row_state(self) -> str:
        """``rest``, ``hover`` or ``pressed``: the layer the card shows now."""
        if not self.isEnabled():
            return "rest"
        if self.button is not None:
            if self.button.isDown():
                return "pressed"
            return "hover" if self.button.underMouse() else "rest"
        if self._hover_highlight:
            if self._pressed:
                return "pressed"
            return "hover" if self.underMouse() else "rest"
        return "rest"

    def mousePressEvent(self, event) -> None:
        if self._hover_highlight and event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._pressed:
            self._pressed = False
            self.update()
        super().mouseReleaseEvent(event)

    def event(self, event) -> bool:
        if event.type() in (QEvent.Type.HoverEnter, QEvent.Type.HoverLeave) and self._hover_highlight:
            if event.type() == QEvent.Type.HoverLeave:
                self._pressed = False
            self.update()
        return super().event(event)

    def control(self) -> QWidget | None:
        return self._control

    def set_control(self, control: QWidget | None) -> None:
        """Put ``control`` at the trailing edge (replacing the previous one, which is deleted)."""
        if self._control is not None:
            self._control_row.removeWidget(self._control)
            self._control.deleteLater()
        self._control = control
        if control is not None:
            control.setParent(self)
            self._control_row.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
            control.show()
            control.raise_()
            if not control.accessibleName():
                control.setAccessibleName(self.title_label.text())
            if not control.accessibleDescription() and self.description_label.text():
                control.setAccessibleDescription(self.description_label.text())

    def add_control(self, widget: QWidget) -> QWidget:
        """Add another widget at the trailing edge, before the control; returns it."""
        widget.setParent(self)
        self._control_row.insertWidget(0, widget, 0, Qt.AlignmentFlag.AlignVCenter)
        widget.show()
        widget.raise_()
        return widget

    # --- clicking ---
    def is_clickable(self) -> bool:
        return self.button is not None

    def set_clickable(self, clickable: bool, action_glyph: str | None = "chevron-right") -> None:
        """Make the row a button. ``action_glyph`` is the mark at the trailing edge (None for no mark)."""
        self._action_glyph = action_glyph or ""
        if clickable and self.button is None:
            self.button = RowButton(self)
            self.button.setGeometry(self.rect())
            self.button.setText(self.title_label.text())
            self.button.setAccessibleName(self.title_label.text())
            self.button.setAccessibleDescription(self.description_label.text())
            self.button.clicked.connect(self.clicked)
            self.button.show()
            self.button.lower()
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setFocusProxy(self.button)
            self._sync_button()
        elif not clickable and self.button is not None:
            self.setFocusProxy(None)
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.button.deleteLater()
            self.button = None
        self._sync_action()

    def click(self) -> None:
        """Click the row (does nothing when it is not clickable or disabled)."""
        if self.button is not None:
            self.button.click()

    def _sync_action(self) -> None:
        shown = self.button is not None and bool(self._action_glyph)
        if shown:
            self.action_widget.set_icon(icons.glyph_icon(self._action_glyph))
        self.action_widget.setVisible(shown)

    def _sync_button(self) -> None:
        if self.button is None:
            return
        self.button.setGeometry(self.rect())
        if self._mode == "card":
            self.button.radius, self.button.corners = None, "all"
        elif self._mode == "header":
            self.button.radius, self.button.corners = None, getattr(self, "_header_corners", "all")
        else:
            self.button.corners = "none"

    def keyPressEvent(self, event) -> None:
        if self.button is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.button.click()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.button is not None:
            self.button.setGeometry(self.rect())

    # --- inside an expander ---
    def _set_mode(self, mode: str) -> None:
        """card (on its own), header (the head of an expander) or row (inside an expander)."""
        self._mode = mode
        self.setMinimumHeight(self.ROW_MIN_HEIGHT if mode == "row" else self.MIN_HEIGHT)
        self._sync_margins()
        self._sync_button()
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if self._mode == "card":
            paint.card(painter, self.rect(), state=self.row_state())
        elif self._mode == "row":
            painter.fillRect(QRect(0, 0, self.width(), 1), qcolor("stroke-divider"))


class Expander(QFrame):
    """A header that opens and closes the content below it.

    The header is a clickable row (``expander.header``, a SettingsCard: title,
    description, icon, an optional control). Content goes into
    ``content_layout`` or through ``set_content``/``add_widget``.
    Keyboard: Space or Enter on the header.
    """

    expanded_changed = Signal(bool)
    _CONTENT_MARGINS = (16, 12, 16, 16)
    _CONTENT_SPACING = 8

    def __init__(self, title: str = "", parent: QWidget | None = None, *, description: str = "",
                 icon: QIcon | str | None = None, control: QWidget | None = None, expanded: bool = False) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._expanded = False
        self._animation: Animation | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        self.header = SettingsCard(title, description, icon, control, self)
        self.header._set_mode("header")
        self.header.set_clickable(True, "chevron-down")
        self.header.clicked.connect(self.toggle)
        outer.addWidget(self.header)
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(*self._CONTENT_MARGINS)
        self.content_layout.setSpacing(self._CONTENT_SPACING)
        self.content.hide()
        outer.addWidget(self.content)
        self.setAccessibleName(title)
        self._describe()
        if expanded:
            self.set_expanded(True, animate=False)

    def title(self) -> str:
        return self.header.title()

    def set_title(self, title: str) -> None:
        self.header.set_title(title)
        self.setAccessibleName(title)

    def set_icon_magnification(self, reserve: float) -> None:
        """Reserve room for the icon of the head to grow (``IconMagnifier``); the text stays where it was."""
        self.header.set_icon_magnification(reserve)

    def add_widget(self, widget: QWidget) -> QWidget:
        """Append a widget to the content; returns it."""
        self.content_layout.addWidget(widget)
        return widget

    def set_content(self, widget: QWidget) -> None:
        """Replace the content with one widget."""
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.content_layout.addWidget(widget)

    def is_expanded(self) -> bool:
        return self._expanded

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self.header._action_glyph = "chevron-up" if expanded else "chevron-down"
        self.header._sync_action()
        self.header._header_corners = "top" if expanded else "all"
        self.header._sync_button()
        self._describe()
        if self._animation is not None:
            self._animation.stop()
            self._animation = None
        time = duration("normal") if animate and self.isVisible() else 0
        if expanded:
            self.content.show()
        if time <= 0:
            self.content.setMaximumHeight(_WIDGET_MAX)
            self.content.setVisible(expanded)
        else:
            self.content.setMaximumHeight(_WIDGET_MAX)
            full = self._content_height()
            start = min(self.content.height(), full) if not expanded else 0
            end = full if expanded else 0
            self.content.setMaximumHeight(start)
            animation = Animation(self)
            animation.setStartValue(float(start))
            animation.setEndValue(float(end))
            animation.setDuration(time)
            animation.setEasingCurve(easing("decelerate"))
            animation.valueChanged.connect(lambda value: self.content.setMaximumHeight(round(float(value))))
            animation.finished.connect(self._animation_finished)
            animation.start()
            self._animation = animation
        self.update()
        self.expanded_changed.emit(expanded)

    def _content_height(self) -> int:
        layout = self.content_layout
        width = max(self.width() - 2, 1)
        if layout.hasHeightForWidth():
            return layout.totalHeightForWidth(width)
        return layout.totalSizeHint().height()

    def _animation_finished(self) -> None:
        self._animation = None
        self.content.setMaximumHeight(_WIDGET_MAX)
        self.content.setVisible(self._expanded)

    def _describe(self) -> None:
        state = "Expanded" if self._expanded else "Collapsed"
        description = self.header.description_label.text()
        if self.header.button is not None:
            self.header.button.setAccessibleDescription(f"{description} {state}".strip())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        paint.card(painter, self.rect())
        if self.content.isVisible():
            y = self.header.geometry().bottom() + 1
            painter.fillRect(QRect(1, y, self.width() - 2, 1), qcolor("stroke-divider"))


class SettingsExpander(Expander):
    """A settings row that opens to show more rows (SettingsCard) underneath, as in Windows Settings."""

    _CONTENT_MARGINS = (0, 0, 0, 0)
    _CONTENT_SPACING = 0

    def __init__(self, title: str = "", description: str = "", icon: QIcon | str | None = None,
                 control: QWidget | None = None, parent: QWidget | None = None, *, expanded: bool = False) -> None:
        super().__init__(title, parent, description=description, icon=icon, control=control, expanded=expanded)
        self._cards: list[SettingsCard] = []

    def add_card(self, card: SettingsCard) -> SettingsCard:
        """Append a SettingsCard as a row of the expander; returns it."""
        card._set_mode("row")
        self.content_layout.addWidget(card)
        self._cards.append(card)
        return card

    def add_row(self, title: str, description: str = "", control: QWidget | None = None,
                icon: QIcon | str | None = None, *, clickable: bool = False) -> SettingsCard:
        """Create a row and append it; returns the SettingsCard."""
        return self.add_card(SettingsCard(title, description, icon, control, self.content, clickable=clickable))

    def cards(self) -> list[SettingsCard]:
        return list(self._cards)

    def paintEvent(self, _event) -> None:
        paint.card(QPainter(self), self.rect())


# --- InfoBar ---------------------------------------------------------------------------------------------

_SEVERITY = {"info": "info", "informational": "info", "success": "success", "ok": "success",
             "warning": "warning", "error": "error", "danger": "error", "critical": "error"}
_SEVERITY_FILL = {"info": "card", "success": "success-tint", "warning": "warning-tint", "error": "danger-tint"}
_SEVERITY_ICON = {"info": "info", "success": "success", "warning": "warning", "error": "danger"}


class InfoBar(QFrame):
    """An inline message across the content area: status icon, title, message, optional action, close button.

    ``severity``: info, success, warning or error. ``closed`` is emitted
    after the user closed the bar (it hides itself); ``action_triggered``
    when the action button is clicked. All text is plain text.
    """

    closed = Signal()
    action_triggered = Signal()

    def __init__(self, severity: str = "info", title: str = "", message: str = "", parent: QWidget | None = None, *,
                 closable: bool = True, action_text: str | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._severity = _SEVERITY.get(severity, "info")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 7, 8, 7)
        layout.setSpacing(12)
        self.status_icon = StatusIcon(_SEVERITY_ICON[self._severity], 16, self)
        layout.addWidget(self.status_icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.setAlignment(self.status_icon, Qt.AlignmentFlag.AlignVCenter)
        self.title_label = Label(title, "body-strong", self)
        self.message_label = Label(message, "body", self, wrap=True, selectable=True)
        self.message_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title_label, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.message_label, 1)
        self._actions = QHBoxLayout()
        self._actions.setSpacing(8)
        layout.addLayout(self._actions)
        self.action_button: QPushButton | None = None
        self.close_button: IconButton | None = None
        if closable:
            self.close_button = IconButton(icons.glyph_icon("close"), "Close", self, icon_size=12)
            self.close_button.setAccessibleName("Close message")
            self.close_button.clicked.connect(self.dismiss)
            layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            layout.setContentsMargins(16, 7, 16, 7)
        self.setMinimumHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.title_label.setVisible(bool(title))
        if action_text:
            self.set_action(action_text)
        self._describe()

    def severity(self) -> str:
        return self._severity

    def set_severity(self, severity: str) -> None:
        self._severity = _SEVERITY.get(severity, "info")
        self.status_icon.set_kind(_SEVERITY_ICON[self._severity])
        self._describe()
        self.update()

    def title(self) -> str:
        return self.title_label.text()

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)
        self.title_label.setVisible(bool(title))
        self._describe()

    def message(self) -> str:
        return self.message_label.text()

    def set_message(self, message: str) -> None:
        self.message_label.setText(message)
        self._describe()

    def show_message(self, severity: str, message: str, title: str = "") -> None:
        """Set severity, text and title in one call and show the bar."""
        self.set_severity(severity)
        self.set_title(title)
        self.set_message(message)
        self.show()

    def set_action(self, text: str | None) -> QPushButton | None:
        """Show (or with None remove) the action button; returns it."""
        if self.action_button is not None:
            self._actions.removeWidget(self.action_button)
            self.action_button.deleteLater()
            self.action_button = None
        if text:
            self.action_button = Button(text, self)
            self.action_button.clicked.connect(self.action_triggered)
            self._actions.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignVCenter)
        return self.action_button

    def add_widget(self, widget: QWidget) -> QWidget:
        """Add a widget (a button, a link) before the close button; returns it."""
        widget.setParent(self)
        self._actions.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        return widget

    def dismiss(self) -> None:
        self.hide()
        self.closed.emit()

    def _describe(self) -> None:
        text = " ".join(part for part in (self.title(), self.message()) if part)
        self.setAccessibleName(text)
        self.setAccessibleDescription(StatusIcon.LABELS[_SEVERITY_ICON[self._severity]])

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if self._severity != "info" and glass.enabled():
            # Glass, washed with the status colour as much as its opaque tint is.
            paint.glass(painter, self.rect(), "card")
            theme = current_theme()
            wash = qcolor(_SEVERITY_ICON[self._severity] if self._severity != "error" else "danger", theme)
            wash.setAlphaF(DERIVED["status-tint"][theme.scheme])
            paint.rounded_rect(painter, self.rect(), theme.radius("card"), wash)
            return
        paint.card(painter, self.rect(), fill=_SEVERITY_FILL[self._severity])


# --- list rows, empty state --------------------------------------------------------------------------

class _SelectionPill(QWidget):
    """The accent mark of a selected row; a widget of its own so it stays above the row's state layers."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedSize(3, 16)
        self.hide()

    def paintEvent(self, _event) -> None:
        paint.rounded_rect(QPainter(self), self.rect(), 1.5, "accent" if self.isEnabled() else "text-disabled")


class ListItem(QFrame):
    """A row for a list built from widgets: leading icon, title, subtitle, trailing widget.

    Clickable by default (``clicked``, Space or Enter). ``set_selected`` marks
    the row with the accent pill. Put rows in a vertical layout with spacing
    2-4; for long or model-backed lists use a QListView, which the style
    sheet gives the same look.
    """

    clicked = Signal()

    def __init__(self, title: str = "", subtitle: str = "", icon: QIcon | str | None = None,
                 trailing: QWidget | None = None, parent: QWidget | None = None, *, clickable: bool = True) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._selected = False
        self._trailing: QWidget | None = None
        self.button: RowButton | None = None
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(12, 6, 12, 6)
        self._layout.setSpacing(12)
        self.icon_widget = IconWidget(None, 20, self)
        self._leading: QWidget = self.icon_widget
        self._layout.addWidget(self.icon_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        self.title_label = Label(title, "body", self)
        self.subtitle_label = Label(subtitle, "caption", self, tone="secondary")
        for label in (self.title_label, self.subtitle_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        text.addStretch(1)
        text.addWidget(self.title_label)
        text.addWidget(self.subtitle_label)
        text.addStretch(1)
        self._layout.addLayout(text, 1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._pill = _SelectionPill(self)
        self.set_icon(icon)
        self.set_subtitle(subtitle)
        self.setAccessibleName(title)
        if trailing is not None:
            self.set_trailing(trailing)
        if clickable:
            self.button = RowButton(self, current_theme().radius("control"))
            self.button.setText(title)
            self.button.setAccessibleName(title)
            self.button.setAccessibleDescription(subtitle)
            self.button.clicked.connect(self.clicked)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setFocusProxy(self.button)

    def title(self) -> str:
        return self.title_label.text()

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)
        self.setAccessibleName(title)
        if self.button is not None:
            self.button.setText(title)
            self.button.setAccessibleName(title)

    def subtitle(self) -> str:
        return self.subtitle_label.text()

    def set_subtitle(self, subtitle: str) -> None:
        self.subtitle_label.setText(subtitle)
        self.subtitle_label.setVisible(bool(subtitle))
        self.setFixedHeight(56 if subtitle else 40)
        if self.button is not None:
            self.button.setAccessibleDescription(subtitle)

    def set_icon(self, icon: QIcon | str | None, size: int | None = None) -> None:
        qicon = to_icon(icon)
        self.icon_widget.set_icon(qicon)
        if size is not None:                  # else the size it has (20 unless it was changed)
            self.icon_widget.set_icon_size(size)
            self.set_icon_magnification(self.icon_widget.magnification())
        self.icon_widget.setVisible(not qicon.isNull() and self._leading is self.icon_widget)

    def set_icon_magnification(self, reserve: float) -> None:
        """Reserve room for the icon to grow to ``reserve`` times its size; the text stays where it was."""
        self.icon_widget.set_magnification(reserve)
        extra = (self.icon_widget.cell() - self.icon_widget.icon_size()) / 2
        self._layout.setContentsMargins(max(0, round(12 - extra)), 6, 12, 6)
        self._layout.setSpacing(max(0, round(12 - extra)))

    def set_leading(self, widget: QWidget) -> None:
        """Use a widget (an Avatar, a check box) instead of the icon."""
        if self._leading is not self.icon_widget:
            self._layout.removeWidget(self._leading)
            self._leading.deleteLater()
        self.icon_widget.hide()
        self._leading = widget
        widget.setParent(self)
        self._layout.insertWidget(0, widget, 0, Qt.AlignmentFlag.AlignVCenter)
        widget.show()
        widget.raise_()

    def set_trailing(self, widget: QWidget | None) -> None:
        if self._trailing is not None:
            self._layout.removeWidget(self._trailing)
            self._trailing.deleteLater()
        self._trailing = widget
        if widget is not None:
            widget.setParent(self)
            self._layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
            widget.show()
            widget.raise_()

    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._pill.setVisible(selected)
        self._pill.raise_()
        self.update()

    def click(self) -> None:
        if self.button is not None:
            self.button.click()

    def keyPressEvent(self, event) -> None:
        if self.button is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.button.click()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.button is not None:
            self.button.setGeometry(self.rect())
        self._pill.move(0, (self.height() - self._pill.height()) // 2)

    def paintEvent(self, _event) -> None:
        if self._selected:
            paint.rounded_rect(QPainter(self), self.rect(), current_theme().radius("control"), "fill-subtle-hover")


class EmptyState(QWidget):
    """What a page shows when there is nothing to show: icon, title, a sentence and one action."""

    action_triggered = Signal()

    def __init__(self, title: str = "", text: str = "", icon: QIcon | str | None = None,
                 action_text: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(8)
        layout.addStretch(1)
        self.icon_widget = IconWidget(icon, 48, self)
        self.icon_widget.setVisible(not to_icon(icon).isNull())
        layout.addWidget(self.icon_widget, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(8)
        self.title_label = Label(title, "subtitle", self, wrap=True)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.text_label = Label(text, "body", self, tone="secondary", wrap=True)
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.text_label.setVisible(bool(text))
        layout.addWidget(self.title_label)
        layout.addWidget(self.text_label)
        self.action_button: Button | None = None
        if action_text:
            layout.addSpacing(8)
            self.action_button = Button(action_text, self, accent=True)
            self.action_button.clicked.connect(self.action_triggered)
            layout.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        self.setAccessibleName(title)
        self.setAccessibleDescription(text)

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)
        self.setAccessibleName(title)

    def set_text(self, text: str) -> None:
        self.text_label.setText(text)
        self.text_label.setVisible(bool(text))
        self.setAccessibleDescription(text)

    def set_icon(self, icon: QIcon | str | None) -> None:
        self.icon_widget.set_icon(icon)
        self.icon_widget.setVisible(not to_icon(icon).isNull())

    def sizeHint(self) -> QSize:
        return super().sizeHint().expandedTo(QSize(320, 200))
