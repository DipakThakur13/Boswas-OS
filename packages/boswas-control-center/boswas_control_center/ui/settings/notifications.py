"""System > Notifications: do not disturb, its schedule, and the banners of each application.

Page "notifications":
  Do not disturb  the switch and "Turn on do not disturb automatically"
                  during set hours: [notifications] do_not_disturb,
                  do_not_disturb_scheduled, do_not_disturb_from,
                  do_not_disturb_to. Quick settings and the notification
                  centre switch do not disturb for the session; this page
                  holds the setting the session starts with.
  Notifications from apps
                  every application that has sent notifications, with a
                  switch for its banners: off, its notifications no longer
                  pop up but still reach the notification centre
                  ([notifications] banners_off, by the name the application
                  gives itself). The shell keeps the list of senders in
                  ``$XDG_STATE_HOME/boswas/notification-senders.json``
                  (boswas-shell(1)); it is read here, never written.

"Dismiss notifications after" is on Accessibility > Visual effects
(accessibility.VisualEffectsSection). Notifications on the lock screen are
not offered: the lock screen is a program of its own that shows no
notifications (ADR-0035).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping

import boswas_fluent as fluent
from PySide6.QtCore import QTime
from PySide6.QtWidgets import QTimeEdit, QVBoxLayout, QWidget

from ... import compositorconfig as cc
from .display import ConfigSection

SENDERS_FILE = "boswas/notification-senders.json"
MAX_SENDERS_BYTES = 256 * 1024
MAX_SENDERS = 100


@dataclass(frozen=True)
class Sender:
    """An application that sent notifications, as the shell recorded it."""

    name: str
    icon: str = ""
    desktop_entry: str = ""
    last: int = 0


def state_home(environ: Mapping[str, str]) -> str | None:
    """$XDG_STATE_HOME when it is an absolute path, else ~/.local/state (None without a home)."""
    base = environ.get("XDG_STATE_HOME") or ""
    if base.startswith("/") and "\0" not in base:
        return os.path.normpath(base)
    home = environ.get("HOME") or ""
    if home.startswith("/") and "\0" not in home:
        return os.path.join(os.path.normpath(home), ".local", "state")
    return None


def _plain(text: object, limit: int) -> bool:
    return isinstance(text, str) and len(text) <= limit and not any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def read_senders(environ: Mapping[str, str]) -> list[Sender]:
    """The applications that sent notifications, most recent first; what cannot be read is left out."""
    base = state_home(environ)
    if base is None:
        return []
    try:
        with open(os.path.join(base, SENDERS_FILE), "rb") as handle:
            raw = handle.read(MAX_SENDERS_BYTES + 1)
        entries = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_SENDERS_BYTES else []
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return []
    found: list[Sender] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        name, icon, entry_id, last = (entry.get("name"), entry.get("icon", ""), entry.get("desktop_entry", ""),
                                      entry.get("last", 0))
        if (not _plain(name, 128) or not name.strip() or not _plain(icon, 256) or not _plain(entry_id, 256)
                or any(sender.name == name for sender in found)):
            continue
        found.append(Sender(name, icon, entry_id, last if isinstance(last, int) and not isinstance(last, bool) else 0))
        if len(found) >= MAX_SENDERS:
            break
    return found


class DoNotDisturbSection(ConfigSection):
    page = "notifications"
    title = "Do not disturb"
    order = 10
    keywords = ("do not disturb", "dnd", "quiet", "silence", "focus", "notifications", "quiet hours", "schedule")
    reports_files = True

    def build(self) -> None:
        self._was_scheduled = False
        self.dnd = self.toggle("Do not disturb", "Notifications go to the notification centre without popping up; "
                               "critical ones still do", "notification-disabled-symbolic", "notifications",
                               "do_not_disturb")
        self.scheduled = fluent.ToggleSwitch()
        self.schedule = self.add(fluent.SettingsExpander(
            "Turn on do not disturb automatically", "During these times, every day",
            "preferences-system-time-symbolic", self.scheduled, self))
        self.scheduled.clicked.connect(self._scheduled_changed)
        self.start, self.end = QTimeEdit(), QTimeEdit()
        for edit, title, key in ((self.start, "From", "do_not_disturb_from"), (self.end, "To", "do_not_disturb_to")):
            edit.setDisplayFormat("HH:mm")
            edit.setMinimumWidth(110)
            edit.setAccessibleName(f"Do not disturb {title.lower()}")
            self.schedule.add_row(title, "", edit)
            edit.timeChanged.connect(lambda time, key=key: self.change_later(
                {("notifications", key): cc.clock_text(time.hour() * 60 + time.minute())}))
        self.note = self.add(fluent.Label("Quick settings and the notification centre turn do not disturb on and off "
                                          "until you sign out; this switch is what each session starts with.",
                                          "caption", self, tone="secondary", wrap=True))
        self.note.setContentsMargins(4, 2, 0, 0)

    def _scheduled_changed(self, on: bool) -> None:
        self.change({("notifications", "do_not_disturb_scheduled"): bool(on)})
        self.schedule.set_expanded(bool(on))

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.dnd.setChecked(snapshot.get("notifications", "do_not_disturb"))
        scheduled = snapshot.get("notifications", "do_not_disturb_scheduled")
        self.scheduled.setChecked(scheduled)
        if scheduled != self._was_scheduled:
            self._was_scheduled = scheduled
            self.schedule.set_expanded(scheduled, animate=False)
        for edit, key in ((self.start, "do_not_disturb_from"), (self.end, "do_not_disturb_to")):
            minutes = cc.parse_clock(snapshot.get("notifications", key)) or 0
            if not edit.hasFocus():
                edit.setTime(QTime(minutes // 60, minutes % 60))


class AppNotificationsSection(ConfigSection):
    page = "notifications"
    title = "Notifications from apps"
    order = 20
    keywords = ("notifications", "banners", "apps", "senders", "mute", "pop up", "toasts")

    def __init__(self, parent=None, *, config: cc.Settings | None = None,
                 environ: Mapping[str, str] | None = None) -> None:
        self.environ = environ
        super().__init__(parent, config=config)

    def build(self) -> None:
        self.empty = self.add(fluent.Label("Applications appear here once they have sent a notification.", "body",
                                           self, tone="secondary", wrap=True))
        self.empty.setContentsMargins(4, 4, 0, 4)
        self.list = self.add(QWidget(self))
        self.list_box = QVBoxLayout(self.list)
        self.list_box.setContentsMargins(0, 0, 0, 0)
        self.list_box.setSpacing(4)
        self.list.hide()
        self.rows: dict[str, tuple[fluent.SettingsCard, fluent.ToggleSwitch]] = {}

    def load(self) -> object:
        environ = self.environ if self.environ is not None else self.config.environ
        return self.config.snapshot(), read_senders(environ)

    def _toggled(self, name: str, on: bool) -> None:
        muted = [sender for sender, (_card, switch) in self.rows.items() if not switch.isChecked()]
        if isinstance(self.state, tuple):
            # Applications muted before that are no longer listed stay muted.
            muted = [name for name in self.state[0].get("notifications", "banners_off") if name not in self.rows] + muted
        self.change({("notifications", "banners_off"): list(dict.fromkeys(muted))[:cc.MAX_MUTED]})

    def show_state(self, state: object) -> None:
        snapshot, senders = state
        muted = set(snapshot.get("notifications", "banners_off"))
        names = [sender.name for sender in senders]
        for name in [name for name in self.rows if name not in names]:
            card, _switch = self.rows.pop(name)
            card.deleteLater()
        for sender in senders:
            if sender.name not in self.rows:
                switch = fluent.ToggleSwitch()
                switch.setAccessibleName(f"Notification banners of {sender.name}")
                icon = fluent.icon(sender.icon or sender.desktop_entry or "dialog-information-symbolic",
                                   "dialog-information-symbolic")
                card = fluent.SettingsCard(sender.name, "Banners", icon, switch, self.list)
                switch.clicked.connect(lambda on, name=sender.name: self._toggled(name, bool(on)))
                self.list_box.addWidget(card)
                self.rows[sender.name] = (card, switch)
            card, switch = self.rows[sender.name]
            switch.setChecked(sender.name not in muted)
            card.set_description("Banners" if sender.name not in muted else "Banners off: the notification centre only")
        self.empty.setVisible(not senders)
        self.list.setVisible(bool(senders))


SECTIONS = (DoNotDisturbSection, AppNotificationsSection)
