"""What every page of Clock is given: the stores, the clocks, the user's formats."""

from __future__ import annotations

import datetime as dt
from typing import Callable

from PySide6.QtCore import QLocale, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QWidget

import boswas_fluent as fluent

from ...reminders import timeutil
from .. import model
from ..store import ClockStore, SettingsStore


class Context:
    def __init__(self, store: ClockStore, settings: SettingsStore, *,
                 now: Callable[[], model.Now] = model.system_now,
                 zone: Callable[[], dt.tzinfo] = timeutil.system_zone) -> None:
        self.store = store
        self.settings = settings
        self.now = now
        self.zone = zone

    def twelve_hour(self) -> bool:
        choice = self.settings.values.time_format
        if choice in ("12", "24"):
            return choice == "12"
        return "ap" in QLocale().timeFormat(QLocale.FormatType.ShortFormat).lower()

    @staticmethod
    def first_day() -> int:
        """The first day of the week of the locale, with Monday as 0."""
        return (QLocale().firstDayOfWeek().value - 1) % 7

    def time_format(self) -> str:
        """The display format for a QTimeEdit."""
        return "h:mm AP" if self.twelve_hour() else "HH:mm"


class ClickCard(fluent.Card):
    """A card that is one big button: a click, Enter or Space opens it; the context menu key asks for its menu."""

    clicked = Signal()
    menu_requested = Signal(object)        # the global position

    def __init__(self, parent: QWidget | None = None, **options) -> None:
        super().__init__(parent, **options)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._pressed = False

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            self.update()
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        elif event.key() == Qt.Key.Key_Menu:
            self.menu_requested.emit(self.mapToGlobal(self.rect().center()))
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.menu_requested.emit(event.globalPos())

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._pressed or self.underMouse():
            painter = QPainter(self)
            fluent.paint.rounded_rect(painter, self.rect(), fluent.theme().radius("card"),
                                      "fill-subtle-pressed" if self._pressed else "fill-subtle-hover")
