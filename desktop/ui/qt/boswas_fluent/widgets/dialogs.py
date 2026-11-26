"""Things that appear above the page: ContentDialog, Flyout, MenuFlyout, TeachingTip."""

from __future__ import annotations

import enum
from typing import Callable

from PySide6.QtCore import QEvent, QEventLoop, QObject, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QPainterPath
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QMenu, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from .. import glass, icons, paint
from ..animation import Animation
from ..theming import current_theme, duration, easing, qcolor, repolish
from .basic import Button, IconButton, Label, to_icon


class _DialogCard(QFrame):
    """The body of a ContentDialog: the content on the flyout surface, the buttons on a strip below."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.strip_top = 0

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = current_theme().radius("flyout")
        box = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(box, radius, radius)
        painter.setClipPath(clip)
        on_glass = glass.enabled() and glass.paint_backdrop(painter, self, box)
        if on_glass:
            # Acrylic: the window's backdrop under the flyout glass, the button strip a deeper pane.
            painter.setClipping(False)
            paint.glass(painter, box, "flyout", radius=radius, outline=False)
            painter.setClipPath(clip)
        else:
            painter.fillRect(box, qcolor("flyout"))
        if self.strip_top:
            strip = QRectF(0, self.strip_top, box.width(), box.height() - self.strip_top)
            painter.fillRect(strip, qcolor("glass-pane" if on_glass else "background"))
            painter.fillRect(QRectF(0, self.strip_top, box.width(), 1), qcolor("stroke-divider"))
        painter.setClipping(False)
        if on_glass:
            paint.glass(painter, box, "flyout", radius=radius, outline=True, fill=False)
        else:
            paint.rounded_rect(painter, box, radius, None, "stroke-surface")


class ContentDialog(QWidget):
    """A modal dialog inside its window: the window is dimmed and the dialog sits in the middle.

    Up to three buttons: primary, secondary and close. ``exec()`` blocks
    and returns which one was chosen; ``open()`` returns at once and
    ``finished(Result)`` tells later. Escape and the close button give
    ``Result.NONE``. Enter presses the default button, which is drawn in
    the accent colour. While the dialog is open, the keyboard focus stays
    inside it and the shortcuts of the window behind it do nothing.

    Put more than text into ``content_layout`` (or ``set_content``).
    """

    class Result(enum.Enum):
        NONE = 0            # closed: the close button, Escape
        PRIMARY = 1
        SECONDARY = 2

    finished = Signal(object)
    MIN_WIDTH, MAX_WIDTH = 320, 548

    def __init__(self, parent: QWidget, title: str = "", text: str = "", *, primary: str | None = "OK",
                 secondary: str | None = None, close: str | None = "Cancel", default: str | None = "primary") -> None:
        host = parent.window()
        super().__init__(host)
        self._host = host
        self._result = ContentDialog.Result.NONE
        self._loop: QEventLoop | None = None
        self._previous_focus: QWidget | None = None
        self._smoke = 1.0
        self._animation: Animation | None = None
        self._open = False
        self.setObjectName("fluentContentDialog")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

        self.card = _DialogCard(self)
        self.card.setObjectName("fluentContentDialogCard")
        outer = QVBoxLayout(self.card)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        top = QWidget(self.card)
        self.content_layout = QVBoxLayout(top)
        self.content_layout.setContentsMargins(24, 24, 24, 24)
        self.content_layout.setSpacing(12)
        self.title_label = Label(title, "subtitle", top, wrap=True)
        self.text_label = Label(text, "body", top, wrap=True, selectable=True)
        self.content_layout.addWidget(self.title_label)
        self.content_layout.addWidget(self.text_label)
        self.title_label.setVisible(bool(title))
        self.text_label.setVisible(bool(text))
        outer.addWidget(top, 1)
        self._strip = QWidget(self.card)
        buttons = QHBoxLayout(self._strip)
        buttons.setContentsMargins(24, 24, 24, 24)
        buttons.setSpacing(8)
        self.primary_button: QPushButton | None = None
        self.secondary_button: QPushButton | None = None
        self.close_button: QPushButton | None = None
        made = []
        for name, label, result in (("primary", primary, ContentDialog.Result.PRIMARY),
                                    ("secondary", secondary, ContentDialog.Result.SECONDARY),
                                    ("close", close, ContentDialog.Result.NONE)):
            if not label:
                continue
            button = Button(label, self._strip)
            button.setAutoDefault(False)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked=False, r=result: self.done(r))
            setattr(self, f"{name}_button", button)
            made.append((name, button))
        if len(made) == 1:
            buttons.addStretch(1)          # a single button takes the trailing half
        for _name, button in made:
            buttons.addWidget(button, 1)
        self._default: QPushButton | None = None
        self.set_default(default)
        outer.addWidget(self._strip)
        self._strip.setVisible(bool(made))
        self.setAccessibleName(title or "Dialog")
        self.setAccessibleDescription(text)

    # --- content ---
    def set_title(self, title: str) -> None:
        self.title_label.setText(title)
        self.title_label.setVisible(bool(title))
        self.setAccessibleName(title or "Dialog")

    def set_text(self, text: str) -> None:
        self.text_label.setText(text)
        self.text_label.setVisible(bool(text))
        self.setAccessibleDescription(text)

    def set_content(self, widget: QWidget) -> QWidget:
        """Add a widget under the text (a form, a list, a progress bar); returns it."""
        self.content_layout.addWidget(widget)
        return widget

    def set_default(self, name: str | None) -> None:
        """Which button Enter presses and the accent colour marks: primary, secondary, close or None."""
        self._default = getattr(self, f"{name}_button", None) if name else None
        for button in (self.primary_button, self.secondary_button, self.close_button):
            if button is not None:
                button.setProperty("accent", button is self._default)
                if button.isVisible():
                    repolish(button)

    def default_button(self) -> QPushButton | None:
        return self._default

    def result(self) -> "ContentDialog.Result":
        return self._result

    def is_open(self) -> bool:
        return self._open

    # --- showing ---
    def open(self) -> None:
        """Show the dialog and return at once; ``finished`` is emitted when it closes."""
        if self._open:
            return
        self._open = True
        self._result = ContentDialog.Result.NONE
        self._previous_focus = QApplication.focusWidget()
        self._host.installEventFilter(self)
        QApplication.instance().installEventFilter(self)
        self._place()
        self.show()
        self.raise_()
        target = self._default or self.primary_button or self.close_button or self.secondary_button
        (target or self.card).setFocus(Qt.FocusReason.PopupFocusReason)
        time = duration("fast")
        if time > 0 and self._host.isVisible():
            self._smoke = 0.0
            animation = Animation(self)
            animation.setStartValue(0.0)
            animation.setEndValue(1.0)
            animation.setDuration(time)
            animation.setEasingCurve(easing("decelerate"))
            animation.valueChanged.connect(self._set_smoke)
            animation.start()
            self._animation = animation

    def exec(self) -> "ContentDialog.Result":
        """Show the dialog and wait until it closes; returns the result."""
        self.open()
        self._loop = QEventLoop(self)
        self._loop.exec()
        self._loop = None
        return self._result

    def done(self, result: "ContentDialog.Result") -> None:
        """Close the dialog with ``result``."""
        if not self._open:
            return
        self._open = False
        self._result = result
        if self._animation is not None:
            self._animation.stop()
            self._animation = None
        try:
            self._host.removeEventFilter(self)
            QApplication.instance().removeEventFilter(self)
        except RuntimeError:
            pass
        self.hide()
        previous = self._previous_focus
        self._previous_focus = None
        try:
            if previous is not None and previous.isVisible():
                previous.setFocus(Qt.FocusReason.PopupFocusReason)
        except RuntimeError:              # the widget is gone
            pass
        if self._loop is not None:
            self._loop.quit()
        self.finished.emit(result)

    def accept(self) -> None:
        self.done(ContentDialog.Result.PRIMARY)

    def reject(self) -> None:
        self.done(ContentDialog.Result.NONE)

    def _set_smoke(self, value) -> None:
        self._smoke = float(value)
        self.update()

    def _place(self) -> None:
        self.setGeometry(self._host.rect())
        available = self.rect().adjusted(24, 24, -24, -24)
        width = max(min(self.MAX_WIDTH, available.width()), min(self.MIN_WIDTH, available.width()))
        self.card.setFixedWidth(width)
        layout = self.card.layout()
        height = layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
        height = max(min(height, available.height()), min(160, available.height()))
        self.card.setGeometry((self.width() - width) // 2, (self.height() - height) // 2, width, height)
        self.card.strip_top = self._strip.geometry().top() if self._strip.isVisibleTo(self.card) else 0
        self.card.layout().activate()
        self.card.strip_top = self._strip.geometry().top() if not self._strip.isHidden() else 0

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if obj is self._host and kind == QEvent.Type.Resize:
            self._place()
        elif kind == QEvent.Type.Shortcut and self._open:
            # Shortcuts of the window behind the dialog stay quiet while it is open.
            owner = obj
            while owner is not None and not isinstance(owner, QWidget):
                owner = owner.parent()
            if owner is not None and owner.window() is self._host and not self.isAncestorOf(owner):
                return True
        return False

    def focusNextPrevChild(self, forward: bool) -> bool:
        # Tab and Shift+Tab stay inside the dialog.
        widgets = [w for w in self.card.findChildren(QWidget)
                   if w.focusPolicy() & Qt.FocusPolicy.TabFocus and w.isVisibleTo(self) and w.isEnabled()
                   and w.focusProxy() is None]
        if not widgets:
            return True
        current = QApplication.focusWidget()
        try:
            index = widgets.index(current)
        except ValueError:
            index = -1 if forward else 0
        target = widgets[(index + (1 if forward else -1)) % len(widgets)]
        target.setFocus(Qt.FocusReason.TabFocusReason if forward else Qt.FocusReason.BacktabFocusReason)
        return True

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.reject()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            focused = QApplication.focusWidget()
            if isinstance(focused, QPushButton) and self.isAncestorOf(focused):
                focused.click()
            elif self._default is not None and self._default.isEnabled():
                self._default.click()
        else:
            event.accept()                 # nothing reaches the window behind

    def mousePressEvent(self, event) -> None:
        event.accept()                     # a click on the dimmed window does nothing

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._open:
            self.card.strip_top = self._strip.geometry().top() if not self._strip.isHidden() else 0

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        colour, alpha = current_theme().rgba("smoke")
        smoke = qcolor(colour)
        smoke.setAlphaF(alpha * self._smoke)
        painter.fillRect(self.rect(), smoke)
        painter.setOpacity(self._smoke)
        paint.shadow(painter, self.card.geometry(), current_theme().radius("flyout"), "dialog")


def ask(parent: QWidget, title: str, text: str, confirm: str = "OK", cancel: str = "Cancel", *,
        default: str = "cancel") -> bool:
    """Ask a yes-or-no question in a ContentDialog; True when the user chose ``confirm``.

    ``default`` says which button Enter presses: ``"cancel"`` (safe for
    anything that deletes or restarts) or ``"confirm"``.
    """
    dialog = ContentDialog(parent, title, text, primary=confirm, close=cancel,
                           default="primary" if default == "confirm" else "close")
    try:
        return dialog.exec() == ContentDialog.Result.PRIMARY
    finally:
        dialog.deleteLater()


def message(parent: QWidget, title: str, text: str, close: str = "OK") -> None:
    """Tell the user something in a ContentDialog with one button."""
    dialog = ContentDialog(parent, title, text, primary=None, close=close, default="close")
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()


# --- flyouts -----------------------------------------------------------------------------------------------

class Flyout(QWidget):
    """A small popup anchored to a widget; it closes when the user clicks elsewhere or presses Escape.

    Add content to ``flyout.body`` (a vertical layout) and call
    ``show_at(anchor)``. ``closed`` is emitted when it goes away.
    """

    closed = Signal()
    _SHADOW = 16             # room around the surface for the shadow

    def __init__(self, parent: QWidget | None = None, *, padding: int = 16) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("fluentFlyout")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(self._SHADOW, self._SHADOW, self._SHADOW, self._SHADOW)
        self.surface = QWidget(self)
        self.body = QVBoxLayout(self.surface)
        self.body.setContentsMargins(padding, padding, padding, padding)
        self.body.setSpacing(8)
        outer.addWidget(self.surface)

    def add_widget(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget

    def show_at(self, anchor: QWidget, placement: str = "bottom", gap: int = 4) -> None:
        """Show beside ``anchor``: ``bottom`` (default), ``top``, ``left`` or ``right``, kept on the screen."""
        self.ensurePolished()
        self.adjustSize()
        size = self.sizeHint().expandedTo(self.minimumSize())
        self.resize(size)
        self.move(self.position_for(anchor, size, placement, gap))
        self.show()
        self.setFocus(Qt.FocusReason.PopupFocusReason)

    def position_for(self, anchor: QWidget, size: QSize, placement: str = "bottom", gap: int = 4) -> QPoint:
        """The top left corner for a flyout of ``size`` (shadow margin included) beside ``anchor``."""
        rect = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
        margin = self._SHADOW
        if placement == "top":
            x, y = rect.center().x() - size.width() // 2, rect.top() - size.height() + margin - gap
        elif placement == "left":
            x, y = rect.left() - size.width() + margin - gap, rect.center().y() - size.height() // 2
        elif placement == "right":
            x, y = rect.right() + 1 - margin + gap, rect.center().y() - size.height() // 2
        else:
            x, y = rect.center().x() - size.width() // 2, rect.bottom() + 1 - margin + gap
        screen = anchor.screen().availableGeometry() if anchor.screen() is not None else None
        if screen is not None:
            x = max(screen.left() - margin, min(x, screen.right() + 1 + margin - size.width()))
            y = max(screen.top() - margin, min(y, screen.bottom() + 1 + margin - size.height()))
        return QPoint(x, y)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.closed.emit()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        box = self.surface.geometry()
        radius = current_theme().radius("flyout")
        paint.shadow(painter, box, radius, "flyout")
        if glass.enabled():
            # Acrylic: the backdrop of the window the flyout belongs to, under the flyout glass.
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(box), radius, radius)
            painter.setClipPath(clip)
            glass.paint_backdrop(painter, self, box)
            painter.restore()
            paint.glass(painter, box, "flyout", radius=radius)
            return
        paint.rounded_rect(painter, box, radius, "flyout", "stroke-surface")


class TeachingTip(Flyout):
    """A flyout that explains something: title, a sentence or two, optionally one action and a close button."""

    action_triggered = Signal()

    def __init__(self, title: str = "", text: str = "", parent: QWidget | None = None, *,
                 action_text: str | None = None, closable: bool = True) -> None:
        super().__init__(parent)
        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = Label(title, "body-strong", self.surface, wrap=True)
        header.addWidget(self.title_label, 1)
        self.close_button: IconButton | None = None
        if closable:
            self.close_button = IconButton(icons.glyph_icon("close"), "Close", self.surface, size=28, icon_size=12)
            self.close_button.clicked.connect(self.close)
            header.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        self.body.addLayout(header)
        self.text_label = Label(text, "body", self.surface, wrap=True)
        self.body.addWidget(self.text_label)
        self.action_button: Button | None = None
        if action_text:
            self.action_button = Button(action_text, self.surface, accent=True)
            self.action_button.clicked.connect(self.action_triggered)
            self.body.addSpacing(4)
            self.body.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignRight)
        self.surface.setMinimumWidth(280)
        self.surface.setMaximumWidth(360)
        self.setAccessibleName(title)
        self.setAccessibleDescription(text)


class MenuFlyout(QMenu):
    """A menu with the helpers an application wants: ``add_item``, ``add_submenu``, ``show_at``."""

    def add_item(self, text: str, triggered: Callable[[], None] | None = None, *, icon: QIcon | str | None = None,
                 shortcut: QKeySequence | QKeySequence.StandardKey | str | None = None, checkable: bool = False,
                 checked: bool = False, enabled: bool = True) -> QAction:
        """Add an entry and return its QAction. ``shortcut`` is shown; it is not made live by the menu."""
        action = QAction(to_icon(icon), text, self)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutVisibleInContextMenu(True)
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        action.setCheckable(checkable)
        if checkable:
            action.setChecked(checked)
        action.setEnabled(enabled)
        if triggered is not None:
            action.triggered.connect(lambda _checked=False: triggered())
        self.addAction(action)
        return action

    def add_submenu(self, text: str, icon: QIcon | str | None = None) -> "MenuFlyout":
        menu = MenuFlyout(text, self)
        menu.setIcon(to_icon(icon))
        self.addMenu(menu)
        return menu

    def position_for(self, anchor: QWidget, placement: str = "bottom") -> QPoint:
        if placement == "top":
            return anchor.mapToGlobal(QPoint(0, -self.sizeHint().height() - 4))
        if placement == "right":
            return anchor.mapToGlobal(QPoint(anchor.width() + 4, 0))
        return anchor.mapToGlobal(QPoint(0, anchor.height() + 4))

    def show_at(self, anchor: QWidget, placement: str = "bottom") -> None:
        """Open the menu beside ``anchor`` (``bottom``, ``top`` or ``right``) without blocking."""
        self.popup(self.position_for(anchor, placement))

    def exec_at(self, anchor: QWidget, placement: str = "bottom") -> QAction | None:
        """Open the menu beside ``anchor`` and wait; returns the chosen action or None."""
        return self.exec(self.position_for(anchor, placement))
