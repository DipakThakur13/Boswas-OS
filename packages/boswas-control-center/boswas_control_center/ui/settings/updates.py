"""Boswas Update: Update history and Advanced options.

Page "update-history":
  Update history      the runs of APT from /var/log/apt/history.log and its
                      rotated .gz files, newest first: when, by whom
                      (automatic security updates, Control Center, a person),
                      and what was installed, updated or removed. When the log
                      cannot be read on this device, the page says so.
Page "update-options":
  Automatic updates   the automatic security update settings the window has
                      read (APT's periodic settings, the last runs, the
                      update source and channel, the device policy); they are
                      the administrator's and shown, not changed

Both read only; the history is read on a worker thread.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtWidgets import QWidget

from ... import probes, updatelog
from ... import viewmodel as vm
from . import context
from .system import ToolSection
from .widgets import RowList, Section, window_of

SHOWN = 60


class UpdateHistorySection(ToolSection):
    page = "update-history"
    title = "Update history"
    order = 10
    keywords = ("update history", "installed updates", "history", "upgrades", "apt log")

    def __init__(self, parent: QWidget | None = None, *, root: str | None = None):
        super().__init__(parent)
        self.root = context.root if root is None else root
        self.summary = self.add(fluent.SettingsCard("", "", fluent.icon("document-open-recent-symbolic"), None,
                                                    self))
        self.list = self.add(RowList(self))
        self.history: updatelog.History | None = None

    def activate(self) -> None:
        super().activate()
        self._poll.stop()

    def read(self) -> updatelog.History:
        return updatelog.read(probes.Root(self.root))

    def show_state(self, history: updatelog.History) -> None:
        self.history = history
        entries = history.entries[:SHOWN]
        if not history.found:
            self.summary.set_title("No update history yet")
            self.summary.set_description("The package manager has not changed anything on this device yet.")
        elif not entries and not history.readable:
            self.summary.set_title("The update history is not readable")
            self.summary.set_description("APT's log (/var/log/apt/history.log) is readable by administrators only "
                                         "on this device.")
        else:
            automatic = sum(1 for entry in history.entries if entry.automatic)
            self.summary.set_title(f"{len(history.entries)} change{'s' if len(history.entries) != 1 else ''} "
                                   "of the installed software")
            self.summary.set_description(f"{automatic} by automatic security updates"
                                         + ("" if history.readable else " · older logs are not readable")
                                         + (f" · the newest {SHOWN} are shown" if len(history.entries) > SHOWN
                                            else ""))
        keyed = {f"{index}:{entry.start}": entry for index, entry in enumerate(entries)}
        self.list.sync(list(keyed), lambda key: self._row(keyed[key]), lambda key, row: None)

    def _row(self, entry: updatelog.Entry) -> fluent.SettingsExpander:
        card = fluent.SettingsExpander(entry.summary, f"{entry.start} · {entry.by}",
                                       fluent.icon("software-update-available-symbolic" if entry.automatic
                                                   else "system-software-install-symbolic"), None, self.list)
        for action in updatelog.ACTIONS:
            changes = entry.changes.get(action, ())
            if changes:
                text = ", ".join(change.text for change in changes[:40])
                if len(changes) > 40:
                    text += f" and {len(changes) - 40} more"
                card.add_row(updatelog.ACTION_WORDS[action], text)
        if entry.error:
            card.add_row("Error", entry.error)
        return card


class UpdateOptionsSection(Section):
    page = "update-options"
    title = "Automatic updates"
    order = 10
    keywords = ("automatic updates", "unattended upgrades", "update settings", "update source", "update channel")

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.cards: list[fluent.SettingsCard] = []
        self.add(fluent.Label("These settings belong to the administrator of this device: APT's periodic settings "
                              "(unattended-upgrades) and the Boswas update configuration. They are shown here and "
                              "not changed.", "caption", self, tone="secondary", wrap=True))
        self.store = None

    def activate(self) -> None:
        super().activate()
        window = window_of(self)
        if window is None:
            return
        self.store = window.store
        self.store.changed.connect(self._changed)
        for key in ("updates", "agent_config", "live"):
            self.store.request(key)
        self.render()

    def deactivate(self) -> None:
        super().deactivate()
        if self.store is not None:
            try:
                self.store.changed.disconnect(self._changed)
            except (RuntimeError, TypeError):
                pass

    def _changed(self, key: str) -> None:
        if key in ("updates", "agent_config", "live") and self.active:
            self.render()

    def render(self) -> None:
        result = self.store.get("updates") if self.store is not None else None
        if result is None:
            rows = [("Reading the update settings…", "")]
        else:
            settings = vm.device_settings(self.store.value("agent_config"))
            view = vm.updates_view(result.value if result.ok else None, settings.get("UPDATE_POLICY"),
                                   self.store.value("live") is True)
            rows = [(view.summary.headline, view.summary.detail), *view.rows,
                    *vm.update_option_rows(result.value if result.ok else None)]
            if view.log_lines:
                rows.append(("Recent automatic updates", "\n".join(view.log_lines[-3:])))
        while len(self.cards) < len(rows):
            self.cards.append(self.add(fluent.SettingsCard("", "", None, None, self)))
        for index, card in enumerate(self.cards):
            card.setVisible(index < len(rows))
            if index < len(rows):
                card.set_title(rows[index][0])
                card.set_description(rows[index][1])
        if self.cards:
            self.cards[0].set_icon(fluent.icon("security-high-symbolic"))

    def values(self) -> dict[str, str]:
        return {card.title(): card.description() for card in self.cards if not card.isHidden()}


SECTIONS = (UpdateHistorySection, UpdateOptionsSection)
