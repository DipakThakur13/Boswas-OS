"""Personalization > Taskbar and Personalization > Start: how the shell's taskbar and Start look and behave.

Page "taskbar":
  Taskbar items       Task View: [taskbar] task_view.
  Taskbar behaviours  alignment (centre or left), automatically hide the
                      taskbar, badges on taskbar buttons, seconds in the
                      clock: [taskbar] alignment, auto_hide, badges,
                      clock_seconds.
Page "start":
  Start               layout (more pins, default, more recommendations),
                      recently added and most used applications under
                      "Recommended", and the folders next to the power
                      button: [start] layout, recently_added, most_used,
                      folders.

The keys are part of the compositor's configuration, which passes them on to
the shell (boswas-compositor(1), boswas-shell(1)); they apply at once.
"""

from __future__ import annotations

import boswas_fluent as fluent

from ... import compositorconfig as cc
from .display import ConfigSection, combo, select

ALIGNMENTS = (("Center", "center"), ("Left", "left"))
LAYOUTS = (("More pins", "more_pins"), ("Default", "default"), ("More recommendations", "more_recommendations"))
# The folders Start can show, in the order Settings lists them, with their words and icons.
FOLDERS = (("settings", "Settings", "preferences-system-symbolic"),
           ("files", "Files", "system-file-manager-symbolic"),
           ("documents", "Documents", "folder-documents-symbolic"),
           ("downloads", "Downloads", "folder-download-symbolic"),
           ("music", "Music", "folder-music-symbolic"),
           ("pictures", "Pictures", "folder-pictures-symbolic"),
           ("videos", "Videos", "folder-videos-symbolic"))


class TaskbarItemsSection(ConfigSection):
    page = "taskbar"
    title = "Taskbar items"
    order = 10
    keywords = ("task view", "taskbar buttons", "taskbar items", "desktops button")
    reports_files = True

    def build(self) -> None:
        self.task_view = self.toggle("Task view", "The button next to Start that shows every window and the desktops",
                                     "view-grid-symbolic", "taskbar", "task_view")

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.task_view.setChecked(snapshot.get("taskbar", "task_view"))


class TaskbarBehavioursSection(ConfigSection):
    page = "taskbar"
    title = "Taskbar behaviours"
    order = 20
    keywords = ("alignment", "align left", "centre", "center", "auto hide", "automatically hide", "hide taskbar",
                "badges", "seconds", "clock")

    def build(self) -> None:
        self.alignment = combo(ALIGNMENTS, width=160)
        self.alignment.setAccessibleName("Taskbar alignment")
        self.card("Taskbar alignment", "Where Start and the applications sit on the taskbar",
                  "format-justify-center-symbolic", self.alignment)
        self.alignment.activated.connect(lambda _i: self.change({("taskbar", "alignment"): self.alignment.currentData()}))
        self.auto_hide = self.toggle("Automatically hide the taskbar", "Windows use the whole screen; the taskbar "
                                     "comes back when the pointer touches the bottom edge", "go-bottom-symbolic",
                                     "taskbar", "auto_hide")
        self.badges = self.toggle("Show badges on taskbar apps", "The number of notifications an application has, on "
                                  "its button", "mail-unread-symbolic", "taskbar", "badges")
        self.seconds = self.toggle("Show seconds in the clock", "The taskbar's clock shows the seconds too",
                                   "preferences-system-time-symbolic", "taskbar", "clock_seconds")

    def show_state(self, snapshot: cc.Snapshot) -> None:
        select(self.alignment, snapshot.get("taskbar", "alignment"))
        self.auto_hide.setChecked(snapshot.get("taskbar", "auto_hide"))
        self.badges.setChecked(snapshot.get("taskbar", "badges"))
        self.seconds.setChecked(snapshot.get("taskbar", "clock_seconds"))


class StartSection(ConfigSection):
    page = "start"
    title = "Start"
    order = 10
    keywords = ("start menu", "layout", "more pins", "recommendations", "recommended", "recently added",
                "most used", "folders", "power button")
    reports_files = True

    def build(self) -> None:
        self.layout_box = combo(LAYOUTS, width=220)
        self.layout_box.setAccessibleName("Layout")
        self.card("Layout", "More rows of pinned applications, or more recommendations", "view-grid-symbolic",
                  self.layout_box)
        self.layout_box.activated.connect(lambda _i: self.change({("start", "layout"): self.layout_box.currentData()}))
        self.recent = self.toggle("Show recently added apps", "Applications installed in the last two weeks appear "
                                  "under Recommended", "list-add-symbolic", "start", "recently_added")
        self.most_used = self.toggle("Show most used apps", "The applications you start most often appear under "
                                     "Recommended", "starred-symbolic", "start", "most_used")
        self.folders = self.add(fluent.SettingsExpander("Folders", "These appear on Start next to the power button",
                                                        fluent.icon("folder-symbolic"), None, self))
        self.folder_switches: dict[str, fluent.ToggleSwitch] = {}
        for key, label, icon in FOLDERS:
            switch = fluent.ToggleSwitch()
            self.folders.add_row(label, "", switch, fluent.icon(icon))
            switch.clicked.connect(self._folders_changed)
            self.folder_switches[key] = switch

    def _folders_changed(self, _on: bool = False) -> None:
        # In the order Settings lists them: Start puts the first next to the power button.
        wanted = [key for key, _label, _icon in FOLDERS if self.folder_switches[key].isChecked()]
        self.change({("start", "folders"): wanted})

    def show_state(self, snapshot: cc.Snapshot) -> None:
        select(self.layout_box, snapshot.get("start", "layout"))
        self.recent.setChecked(snapshot.get("start", "recently_added"))
        self.most_used.setChecked(snapshot.get("start", "most_used"))
        chosen = snapshot.get("start", "folders")
        for key, switch in self.folder_switches.items():
            switch.setChecked(key in chosen)
        count = len(chosen)
        self.folders.header.set_description("These appear on Start next to the power button" if not count else
                                            f"{count} shown next to the power button")


SECTIONS = (TaskbarItemsSection, TaskbarBehavioursSection, StartSection)
