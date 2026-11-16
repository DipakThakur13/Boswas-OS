"""The window of Clock: the navigation, the pages, the settings."""

from __future__ import annotations

import datetime as dt
from typing import Callable

from PySide6.QtCore import QEvent, QSettings, QTimer
from PySide6.QtWidgets import QComboBox, QVBoxLayout, QWidget

import boswas_fluent as fluent

from ...reminders import client, textfmt, timeutil
from .. import APP_ID, model
from ..store import ClockStore, SettingsStore
from .alarms import AlarmsPage
from .common import Context
from .focus import FocusPage
from .stopwatch import StopwatchPage
from .timers import TimersPage
from .widgets import scroll_page
from .world import WorldPage

PAGES = ("focus", "timers", "alarms", "stopwatch", "world")
PAGE_ALIASES = {"focus-sessions": "focus", "timer": "timers", "alarm": "alarms", "world-clock": "world",
                "worldclock": "world"}
VERSION = "1.0"


class ReminderService:
    """The reminder service as Clock sees it (tests give the window another one)."""

    @staticmethod
    def ensure() -> str:
        return client.ensure_running()

    @staticmethod
    def is_running() -> bool:
        return client.is_running()


def parse_arguments(arguments: list[str]) -> str | None:
    """The page asked for on the command line (``--page=alarms`` or ``--page alarms``), or None."""
    page = None
    for index, argument in enumerate(arguments):
        if argument.startswith("--page="):
            page = argument.split("=", 1)[1]
        elif argument == "--page" and index + 1 < len(arguments):
            page = arguments[index + 1]
    if page is None:
        return None
    page = PAGE_ALIASES.get(page.lower(), page.lower())
    return page if page in (*PAGES, "settings") else None


class ClockWindow(fluent.FluentWindow):
    def __init__(self, arguments: list[str] | None = None, *, store: ClockStore | None = None,
                 settings: SettingsStore | None = None, now: Callable[[], model.Now] = model.system_now,
                 zone: Callable[[], dt.tzinfo] = timeutil.system_zone, service=None) -> None:
        super().__init__(APP_ID, "Clock", navigation=True, icon="preferences-system-time", size=(1000, 680),
                         minimum_size=(420, 420))
        self.store = store if store is not None else ClockStore(now=now)
        self.settings = settings if settings is not None else SettingsStore()
        self.context = Context(self.store, self.settings, now=now, zone=zone)
        self.service = service if service is not None else ReminderService()
        self._undo: Callable[[], None] | None = None
        nav = self.navigation
        self.status = nav.add_banner(fluent.InfoBar("info"))
        self.status.hide()
        self.status.action_triggered.connect(self._banner_action)
        nav.add_page("focus", "Focus sessions", icon="emblem-default-symbolic", factory=lambda: FocusPage(
            self.context, self), keywords=("pomodoro", "break", "goal"))
        nav.add_page("timers", "Timers", icon="power-profile-balanced-symbolic", factory=lambda: TimersPage(
            self.context, self), keywords=("countdown",))
        nav.add_page("alarms", "Alarms", icon="notification-symbolic", factory=lambda: AlarmsPage(self.context, self),
                     keywords=("wake", "ring"))
        nav.add_page("stopwatch", "Stopwatch", icon="preferences-system-time-symbolic",
                     factory=lambda: StopwatchPage(self.context, self), keywords=("lap",))
        nav.add_page("world", "World clock", icon="mark-location-symbolic", factory=lambda: WorldPage(
            self.context, self), keywords=("time zone", "city"))
        nav.add_page("settings", "Settings", icon="preferences-system-symbolic", factory=self._settings_page,
                     footer=True)
        self.add_shortcut("quit", self.close)
        self.add_shortcut("close", self.close)
        self.add_shortcut("undo", self.undo)
        self.add_shortcut("preferences", lambda: nav.set_current("settings"))
        self.add_shortcut("fullscreen", self._toggle_fullscreen)
        self.add_shortcut("next_tab", lambda: self._step_page(1))
        self.add_shortcut("previous_tab", lambda: self._step_page(-1))
        self.store.failed.connect(lambda message: self.report("error", message))
        self.store.changed.connect(self._update_title)
        requested = parse_arguments(arguments or [])
        nav.set_current(requested or self._remembered_page())
        nav.current_changed.connect(self._page_changed)
        if self.store.load_error:
            self.report("warning", self.store.load_error)
        QTimer.singleShot(0, self, self._check_service)

    # --- hooks the pages (and the tests) use ---
    def run_dialog(self, dialog: fluent.ContentDialog) -> fluent.ContentDialog.Result:
        return dialog.exec()

    def report(self, severity: str, message: str) -> None:
        self._undo = None
        self.status.set_action(None)
        self.status.show_message(severity, message)

    def offer_undo(self, message: str, undo: Callable[[], None]) -> None:
        self._undo = undo
        self.status.set_action("Undo")
        self.status.show_message("info", message)

    def undo(self) -> None:
        action, self._undo = self._undo, None
        if action is not None:
            action()
            self.status.dismiss()

    def _banner_action(self) -> None:
        if self._undo is not None:
            self.undo()
        elif self.status.property("startService"):
            self.status.setProperty("startService", False)
            self.status.dismiss()
            self._check_service()

    # --- the reminder service ---
    def _check_service(self) -> None:
        state = self.service.ensure()
        if state == "unavailable":
            self.status.setProperty("startService", True)
            self.status.set_action("Try again")
            self.status.show_message("warning", "Alarms, timers and focus sessions cannot notify you now, because "
                                     "the reminder service (boswas-reminders) is not running and could not be started.",
                                     "Reminders are off")

    # --- pages ---
    def handle_arguments(self, arguments: list[str]) -> None:
        page = parse_arguments(arguments)
        if page is not None:
            self.navigation.set_current(page)

    def _state(self) -> QSettings:
        return QSettings(str(fluent.state_dir(APP_ID) / "window.ini"), QSettings.Format.IniFormat)

    def _remembered_page(self) -> str:
        page = self._state().value("window/page")
        return page if page in PAGES else "alarms"

    def _page_changed(self, key: str) -> None:
        if key in PAGES:
            self._state().setValue("window/page", key)
        self._update_title()

    def _step_page(self, step: int) -> None:
        keys = [*PAGES, "settings"]
        current = self.navigation.current()
        index = keys.index(current) if current in keys else 0
        self.navigation.set_current(keys[(index + step) % len(keys)])

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _update_title(self) -> None:
        running = sum(1 for timer in self.store.doc["timers"] if timer["state"] == "running")
        self.navigation.set_item_badge("timers", running or None)

    def _settings_page(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        values = self.settings.values

        def combo(choices: list[tuple[str, object]], name: str) -> QComboBox:
            box = QComboBox()
            for label, value in choices:
                box.addItem(label, value)
            box.setCurrentIndex(max(0, box.findData(getattr(values, name))))
            box.activated.connect(lambda index, b=box: self.settings.set(name, b.itemData(index)))
            return box

        self.time_format_box = combo([("As the system is set", "locale"), ("12-hour", "12"), ("24-hour", "24")],
                                     "time_format")
        layout.addWidget(fluent.SettingsCard("Time format", "How times are shown in Clock and in its notifications",
                                             "preferences-system-time-symbolic", self.time_format_box))
        self.sound_switch = fluent.ToggleSwitch(checked=values.alarm_sound)
        self.sound_switch.toggled.connect(lambda on: self.settings.set("alarm_sound", bool(on)))
        layout.addWidget(fluent.SettingsCard("Sound", "Play a chime with alarms, timers and reminders",
                                             "audio-volume-high-symbolic", self.sound_switch))
        self.snooze_box = combo([(textfmt.duration_words(minutes * 60), minutes)
                                 for minutes in sorted({*model.SNOOZE_CHOICES, values.default_snooze})],
                                "default_snooze")
        layout.addWidget(fluent.SettingsCard("Snooze", "The snooze time a new alarm starts with",
                                             "notification-symbolic", self.snooze_box))
        self.focus_box = combo([(textfmt.duration_words(minutes * 60), minutes)
                                for minutes in sorted({15, 20, 25, 30, 45, 50, 60, 90, values.focus_minutes})],
                               "focus_minutes")
        layout.addWidget(fluent.SettingsCard("Focus period", "How long you focus before a break",
                                             "emblem-default-symbolic", self.focus_box))
        self.break_box = combo([(textfmt.duration_words(minutes * 60), minutes)
                                for minutes in sorted({3, 5, 10, 15, 20, 30, values.break_minutes})],
                               "break_minutes")
        layout.addWidget(fluent.SettingsCard("Break", "How long a break in a focus session is",
                                             "night-light-symbolic", self.break_box))
        self.service_card = fluent.SettingsCard(
            "Reminder service", "", "notification-symbolic")
        layout.addWidget(self.service_card)
        layout.addWidget(fluent.SettingsCard("Clock", f"Version {VERSION}. Alarms, timers, a stopwatch, a world "
                                             "clock and focus sessions.", "help-about-symbolic"))
        page = scroll_page(content)
        self._show_service_state()
        return page

    def _show_service_state(self) -> None:
        running = self.service.is_running()
        self.service_card.set_description(
            "Running: alarms, timers and reminders are shown while Clock is closed." if running else
            "Not running: nothing can notify you. It starts with the session and when Clock opens.")

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and not self.isMinimized():
            page = self.navigation.stack.currentWidget()
            ticker = getattr(page, "ticker", None)
            if ticker is not None:
                ticker.kick()                       # back from being minimised: show the time as it is now

    def closeEvent(self, event) -> None:
        super().closeEvent(event)
        if event.isAccepted():
            self.store.close()
            self.settings.close()
