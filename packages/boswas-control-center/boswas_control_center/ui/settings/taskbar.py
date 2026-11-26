"""Personalization > Taskbar, Start and Themes: how the shell's taskbar, Start and desktop icons behave.

Page "taskbar":
  Taskbar items       Widgets (the weather at the left end of the taskbar,
                      which opens the widgets board) and Task View:
                      [taskbar] widgets, task_view.
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
Page "themes" (after the presets):
  Desktop icons       show desktop icons, the system icons (Home, Trash,
                      This device), the icon size, auto arrange and align
                      to grid: [desktop] icons, system_icons, icon_size,
                      auto_arrange, align_to_grid. The desktop's own View
                      menu changes four of them in the same file.

The keys are part of the compositor's configuration, which passes them on to
the shell (boswas-compositor(1), boswas-shell(1)); they apply at once.
"""

from __future__ import annotations

import boswas_fluent as fluent

from ... import compositorconfig as cc
from .display import ConfigSection, combo, select

ALIGNMENTS = (("Center", "center"), ("Left", "left"))
LAYOUTS = (("More pins", "more_pins"), ("Default", "default"), ("More recommendations", "more_recommendations"))
# The system icons the desktop can show, in the order Settings lists them, with their words and icons.
SYSTEM_ICONS = (("home", "Home", "user-home"),
                ("trash", "Trash", "user-trash"),
                ("computer", "This device", "computer"))
ICON_SIZES = (("Large icons", "large"), ("Medium icons", "medium"), ("Small icons", "small"))
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
    keywords = ("task view", "taskbar buttons", "taskbar items", "desktops button", "widgets", "weather")
    reports_files = True

    def build(self) -> None:
        self.widgets = self.toggle("Widgets", "The weather at the left end of the taskbar; it opens the widgets board "
                                   "(Super+W opens it too)", "weather-few-clouds-symbolic", "taskbar", "widgets")
        self.task_view = self.toggle("Task view", "The button next to Start that shows every window and the desktops",
                                     "view-grid-symbolic", "taskbar", "task_view")

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.widgets.setChecked(snapshot.get("taskbar", "widgets"))
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


class DesktopIconsSection(ConfigSection):
    page = "themes"
    title = "Desktop icons"
    order = 50
    keywords = ("desktop icons", "desktop icon settings", "show desktop icons", "trash", "recycle bin", "home",
                "this device", "computer", "icon size", "auto arrange", "align to grid", "desktop folder")
    reports_files = True

    def build(self) -> None:
        self.icons = self.toggle("Show desktop icons", "The files and folders of your desktop folder, on the background",
                                 "user-desktop", "desktop", "icons")
        self.system = self.add(fluent.SettingsExpander("Desktop icon settings", "The icons the desktop shows besides "
                                                       "your files", fluent.icon("user-trash"), None, self))
        self.system_switches: dict[str, fluent.ToggleSwitch] = {}
        for key, label, icon in SYSTEM_ICONS:
            switch = fluent.ToggleSwitch()
            self.system.add_row(label, "", switch, fluent.icon(icon))
            switch.clicked.connect(self._system_changed)
            self.system_switches[key] = switch
        self.size_box = combo(ICON_SIZES, width=180)
        self.size_box.setAccessibleName("Icon size")
        self.card("Icon size", "How large the desktop's icons are", "zoom-in-symbolic", self.size_box)
        self.size_box.activated.connect(lambda _i: self.change({("desktop", "icon_size"): self.size_box.currentData()}))
        self.auto_arrange = self.toggle("Auto arrange icons", "The icons fill the desktop from the top left, column by "
                                        "column, in the order chosen with Sort by", "view-sort-ascending-symbolic",
                                        "desktop", "auto_arrange")
        self.align = self.toggle("Align icons to grid", "Icons you move snap to the grid", "view-grid-symbolic",
                                 "desktop", "align_to_grid")

    def _system_changed(self, _on: bool = False) -> None:
        # In the order Settings lists them: the desktop shows them in this order.
        wanted = [key for key, _label, _icon in SYSTEM_ICONS if self.system_switches[key].isChecked()]
        self.change({("desktop", "system_icons"): wanted})

    def show_state(self, snapshot: cc.Snapshot) -> None:
        shown = snapshot.get("desktop", "icons")
        self.icons.setChecked(shown)
        chosen = snapshot.get("desktop", "system_icons")
        for key, switch in self.system_switches.items():
            switch.setChecked(key in chosen)
        count = len(chosen)
        self.system.header.set_description("The icons the desktop shows besides your files" if not count else
                                           f"{count} shown on the desktop")
        select(self.size_box, snapshot.get("desktop", "icon_size"))
        self.auto_arrange.setChecked(snapshot.get("desktop", "auto_arrange"))
        self.align.setChecked(snapshot.get("desktop", "align_to_grid"))
        # Aligning is part of auto arrange.
        self.align.setEnabled(not snapshot.get("desktop", "auto_arrange"))
        for control in (self.system, self.size_box, self.auto_arrange):
            control.setEnabled(shown)
        if not shown:
            self.align.setEnabled(False)


SECTIONS = (TaskbarItemsSection, TaskbarBehavioursSection, StartSection, DesktopIconsSection)
