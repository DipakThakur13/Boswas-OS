"""What the sections of connectivity, sound_power, accounts and system share.

  Section        the frame of a section: rows 4 px apart, a notice bar for
                 "the service is missing" and an error bar for a change that
                 could not be made; ``rendering`` is set while the section
                 moves its own controls, so that a handler can tell that
                 from a change made by the user
  BridgeSection  a section that shows one service of the services bridge:
                 it starts the window's bridge when activated, follows the
                 service's change signal, and sends commands. A control the
                 user changed shows the wanted value until the service
                 reports it; if it does not within a few seconds the control
                 snaps back to the real state and the error bar says so
  RowList        rows keyed by a name that are updated in place: a row that
                 stays keeps its widget (and with it the keyboard focus)
  Busy           a small progress ring with a text beside it
  ElidedLabel    one line of text from outside that may be longer than the
                 row: cut in the middle with an ellipsis, whole in the tool tip
"""

from __future__ import annotations

import time

import boswas_fluent as fluent
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget

from ... import servicebridge as sb
from .. import bridgeclient

PENDING_SECONDS = 6.0          # how long a control shows a value the service has not confirmed yet


class Section(QWidget):
    """Rows of a section, with its notice and error bars on top."""

    page = ""
    title = ""
    order = 0
    keywords: tuple[str, ...] = ()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.active = False
        self.rendering = False             # True while the section itself moves its controls
        self.rows = QVBoxLayout(self)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(4)
        self.notice = fluent.InfoBar("info", parent=self, closable=False)
        self.notice.hide()
        self.error = fluent.InfoBar("error", parent=self)
        self.error.hide()
        self.rows.addWidget(self.notice)
        self.rows.addWidget(self.error)

    def add(self, widget: QWidget) -> QWidget:
        self.rows.addWidget(widget)
        return widget

    def show_notice(self, text: str, severity: str = "info") -> None:
        if text:
            self.notice.show_message(severity, text)
        else:
            self.notice.hide()

    def show_error(self, text: str) -> None:
        self.error.show_message("error", text)

    def clear_error(self) -> None:
        self.error.hide()

    def activate(self) -> None:
        self.active = True

    def deactivate(self) -> None:
        self.active = False


class BridgeSection(Section):
    """A section that shows the state of one service of the services bridge."""

    service = sb.NETWORK
    missing = ""                   # the notice while the bridge runs but the service does not answer

    def __init__(self, parent: QWidget | None = None, *, bridge=None):
        super().__init__(parent)
        self._bridge = bridge
        self._pending: dict[str, tuple[object, float, str]] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._check_pending)

    @property
    def bridge(self):
        if self._bridge is None:
            self._bridge = bridgeclient.for_widget(self)
        return self._bridge

    def state(self):
        return self.bridge.state(self.service)

    def available(self, state) -> bool:
        return bool(getattr(state, "available", True))

    # --- life cycle ---
    def activate(self) -> None:
        if self.active:
            return
        self.active = True
        bridge = self.bridge
        bridge.signal(self.service).connect(self._state_changed)
        bridge.status_changed.connect(self._status_changed)
        bridge.start()
        self.refresh()

    def deactivate(self) -> None:
        if not self.active:
            return
        self.active = False
        self._timer.stop()
        self._pending.clear()
        bridge = self.bridge
        try:
            bridge.signal(self.service).disconnect(self._state_changed)
            bridge.status_changed.disconnect(self._status_changed)
        except (RuntimeError, TypeError):
            pass                       # the bridge went away with its window

    def _state_changed(self, _state=None) -> None:
        self.refresh()

    def _status_changed(self, _status: str = "") -> None:
        self.refresh()

    def refresh(self) -> None:
        state = self.state()
        status = self.bridge.status()
        if status == sb.UNAVAILABLE:
            self.show_notice(self.bridge.problem(), "warning")
        elif status == sb.RUNNING and not self.available(state) and self.missing:
            self.show_notice(self.missing)
        else:
            self.show_notice("")
        self.rendering = True
        try:
            self.render(state)
        finally:
            self.rendering = False

    def render(self, state) -> None:
        """Show ``state``; called for every change and when the bridge's status changes."""

    def usable(self) -> bool:
        """Whether changes can be made now: the bridge runs and the service answers."""
        return self.bridge.status() == sb.RUNNING and self.available(self.state())

    # --- changes ---
    def send(self, command: dict, failed: str) -> bool:
        """Send a command; when the bridge is not running say ``failed`` in the error bar and return False."""
        try:
            sent = self.bridge.send(command)
        except ValueError as exc:
            self.show_error(f"{failed} ({exc})")
            return False
        if not sent:
            self.show_error(f"{failed} The settings service is not running.")
            self.refresh()
            return False
        self.clear_error()
        return True

    def expect(self, key: str, wanted: object, failed: str, seconds: float | None = None) -> None:
        """Until the service reports ``wanted`` for ``key``, controls show it; after ``seconds`` they snap back."""
        seconds = PENDING_SECONDS if seconds is None else seconds
        self._pending[key] = (wanted, time.monotonic() + seconds, failed)
        self._arm()

    def shown(self, key: str, real: object, same=None) -> object:
        """The value a control shows for ``key``: the wanted one while a change is pending, else the real one."""
        pending = self._pending.get(key)
        if pending is None:
            return real
        wanted = pending[0]
        if (same(wanted, real) if same is not None else wanted == real):
            del self._pending[key]
            return real
        return wanted

    def is_pending(self, key: str) -> bool:
        return key in self._pending

    def _arm(self) -> None:
        if self._pending:
            soonest = min(deadline for _wanted, deadline, _failed in self._pending.values())
            self._timer.start(max(0, int((soonest - time.monotonic()) * 1000)) + 20)

    def _check_pending(self) -> None:
        now = time.monotonic()
        expired = [key for key, (_wanted, deadline, _failed) in self._pending.items() if deadline <= now]
        messages = []
        for key in expired:
            messages.append(self._pending.pop(key)[2])
        if expired and self.active:
            self.refresh()             # the controls go back to what the service reports
            if any(messages):
                self.show_error(" ".join(dict.fromkeys(m for m in messages if m)))
        self._arm()


class RowList(QWidget):
    """Rows that are updated in place.

    ``sync(keys, create, update)`` makes the list show exactly ``keys`` in
    that order: ``create(key)`` builds the widget of a new key, ``update(key,
    widget)`` refreshes every row, and rows whose key is gone are removed. A
    row that stays keeps its widget, so the keyboard focus stays where it
    was; when the focused row itself goes away, the focus moves to its
    neighbour instead of being lost.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(4)
        self.widgets: dict[object, QWidget] = {}
        self.order: list[object] = []

    def sync(self, keys: list, create, update) -> None:
        keys = list(dict.fromkeys(keys))
        focus = QApplication.focusWidget()
        for key in [k for k in self.order if k not in keys]:
            widget = self.widgets.pop(key)
            if focus is not None and (focus is widget or widget.isAncestorOf(focus)):
                position = self.order.index(key)
                near = [k for k in self.order[position + 1:] + self.order[:position][::-1] if k in keys]
                if near and near[0] in self.widgets:
                    self.widgets[near[0]].setFocus(Qt.FocusReason.OtherFocusReason)
            self.box.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        for key in keys:
            if key not in self.widgets:
                self.widgets[key] = create(key)
        if keys != [k for k in self.order if k in keys] or len(keys) != self.box.count():
            for index, key in enumerate(keys):
                if self.box.indexOf(self.widgets[key]) != index:
                    self.box.insertWidget(index, self.widgets[key])
        self.order = keys
        for key in keys:
            update(key, self.widgets[key])
        self.setVisible(bool(keys))

    def row(self, key) -> QWidget | None:
        return self.widgets.get(key)

    def keys(self) -> list:
        return list(self.order)


class Busy(QWidget):
    """A small progress ring with an optional text; hidden while idle."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.ring = fluent.ProgressRing(self, size=16)
        self.label = fluent.Label(text, "caption", self, tone="secondary")
        layout.addWidget(self.ring)
        layout.addWidget(self.label)
        self.label.setVisible(bool(text))
        self.hide()

    def set_busy(self, busy: bool, text: str | None = None) -> None:
        if text is not None:
            self.label.setText(text)
            self.label.setVisible(bool(text))
        self.setVisible(busy)
        self.setAccessibleName(self.label.text() if busy else "")


class ElidedLabel(fluent.Label):
    """A one-line label that never asks for more width than it gets: a long text is cut in the middle."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *, width: int = 320):
        super().__init__("", "body", parent)
        self._full = ""
        self._width = width
        self.set_full_text(text)

    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self.setAccessibleDescription(text)
        self.updateGeometry()
        self._elide()

    def _elide(self) -> None:
        metrics = QFontMetrics(self.font())
        self.setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideMiddle, max(self.width(), 40)))

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        return QSize(min(self._width, metrics.horizontalAdvance(self._full) + 4), metrics.height() + 2)

    def minimumSizeHint(self) -> QSize:
        return QSize(min(80, self.sizeHint().width()), self.sizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()


def status_card(title: str, description: str = "", icon: str | None = None, control: QWidget | None = None,
                parent: QWidget | None = None) -> fluent.SettingsCard:
    """A settings row; its icon is a symbolic icon of Boswas Prism."""
    return fluent.SettingsCard(title, description, fluent.icon(icon) if icon else None, control, parent)


def set_checked(toggle, checked: bool) -> None:
    """Put a switch in a position. Its ``toggled`` signal still fires (the switch animates on it): the
    section's handler tells a change made here from one made by the user by ``Section.rendering``."""
    if toggle.isChecked() != checked:
        toggle.setChecked(checked)


def set_value(slider, value: int) -> None:
    """Move a slider without telling its listeners."""
    if slider.value() != value:
        blocked = slider.blockSignals(True)
        slider.setValue(value)
        slider.blockSignals(blocked)


# --- what a section asks of the window it is shown in -----------------------------------------------------------

def window_of(widget: QWidget):
    """The Control Center window that shows ``widget`` (it has ``jobs``, ``ask`` and ``show_page``), else None."""
    window = widget.window()
    return window if hasattr(window, "jobs") and hasattr(window, "show_page") else None


def go(widget: QWidget, key: str, section: str | None = None) -> None:
    """Show a place of the window that holds ``widget``."""
    window = window_of(widget)
    if window is not None:
        window.show_page(key, section)


def ask(widget: QWidget, title: str, text: str, confirm: str) -> bool:
    """A confirmation through the window (whose ``ask`` the tests answer), else a dialog of the toolkit."""
    window = window_of(widget)
    if window is not None:
        return bool(window.ask(title, text, confirm))
    return fluent.ask(widget, title, text, confirm, "Cancel", default="cancel")


STEP_KINDS = {"ok": "success", "problem": "attention", "note": "info"}


def step_card(state: str, title: str, detail: str = "", parent: QWidget | None = None) -> fluent.SettingsCard:
    """A row with a status shape (check, !, i) and its state in words for assistive technology."""
    card = fluent.SettingsCard(title, detail, None, None, parent)
    mark = fluent.StatusIcon(STEP_KINDS.get(state, "neutral"), 16, card)
    mark.setAccessibleName({"ok": "Fine", "problem": "Problem", "note": "Note"}.get(state, ""))
    card.add_control(mark)
    card.setAccessibleName(f"{title}: {mark.accessibleName()}")
    return card
