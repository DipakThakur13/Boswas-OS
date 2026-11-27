"""System > Clipboard: the clipboard history, and clearing what it keeps.

Page "clipboard":
  Clipboard history      on or off: [clipboard] history. On, the desktop
                         keeps the last 25 things copied (in memory; pinned
                         items in the user's state directory) and Super+V
                         shows them. Off, Super+V says so and offers to turn
                         it on.
  Clear clipboard data   [clipboard] cleared_at = now: the desktop forgets
                         everything copied before then except pinned items,
                         and empties the clipboard if it held such an item.
  Emoji and symbols      where the panel is (Super+. or Super+;); nothing to
                         set.

The compositor passes both keys on to the shell at once
(boswas-compositor(1), boswas-shell(1)).
"""

from __future__ import annotations

import time

import boswas_fluent as fluent

from ... import compositorconfig as cc
from .display import ConfigSection


class ClipboardSection(ConfigSection):
    page = "clipboard"
    title = "Clipboard"
    order = 10
    keywords = ("clipboard", "clipboard history", "copy", "paste", "super+v", "win+v", "pinned", "clear clipboard",
                "clear clipboard data")
    reports_files = True

    def build(self) -> None:
        self.history = self.toggle(
            "Clipboard history",
            "Save several items to the clipboard to use later. Press Super+V to see the clipboard history and "
            "paste from it.",
            fluent.icon("edit-paste-symbolic", "edit-copy-symbolic"), "clipboard", "history")
        self.history.setAccessibleName("Clipboard history")
        self.clear = fluent.Button("Clear")
        self.clear.setAccessibleName("Clear clipboard data")
        self.clear.clicked.connect(self._clear)
        self.clear_card = self.card("Clear clipboard data",
                                    "Clear everything on the clipboard and in its history, except pinned items",
                                    fluent.icon("edit-clear-history-symbolic", "edit-clear-symbolic"), self.clear)
        self.card("Emoji and symbols",
                  "Press Super+. or Super+; to insert emoji, kaomoji and symbols into what you are typing.",
                  fluent.icon("preferences-desktop-emoticons", "face-smile-symbolic"))
        self._clicked = False

    def _clear(self) -> None:
        self._clicked = True
        self.change({("clipboard", "cleared_at"): int(time.time())})

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.history.setChecked(snapshot.get("clipboard", "history"))
        # Done, as Windows shows it: the button says so until the page is left.
        if self._clicked and snapshot.get("clipboard", "cleared_at") > 0:
            self.clear.setText("Cleared")
        self.clear.setEnabled(True)

    def deactivate(self) -> None:
        super().deactivate()
        self._clicked = False
        self.clear.setText("Clear")


SECTIONS = (ClipboardSection,)
