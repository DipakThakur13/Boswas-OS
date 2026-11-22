"""System > Multitasking: snapping windows, and which windows the taskbar and Alt+Tab show.

Page "multitasking":
  Snap windows   on or off (dragging a window to a screen edge, and
                 Super+Left/Right), with the snap layouts on the maximize
                 button and when a window is dragged to the top of the
                 screen: [multitasking] snap_windows,
                 snap_layouts_on_maximize, snap_layouts_on_drag.
  Desktops       the windows of the current desktop or of all desktops on
                 the taskbar and in Alt+Tab: [multitasking] taskbar_windows,
                 alt_tab_windows.

The compositor applies them at once (boswas-compositor(1)).
"""

from __future__ import annotations

import boswas_fluent as fluent

from ... import compositorconfig as cc
from .display import ConfigSection, combo, select

SCOPES = (("Only on the desktop I'm using", "desktop"), ("On all desktops", "all"))


class SnapSection(ConfigSection):
    page = "multitasking"
    title = "Snap windows"
    order = 10
    keywords = ("snap", "snap layouts", "split screen", "side by side", "arrange windows", "maximize button",
                "drag to edge", "tile")
    reports_files = True

    def build(self) -> None:
        self.snap = fluent.ToggleSwitch()
        self.expander = self.add(fluent.SettingsExpander(
            "Snap windows", "Arrange windows side by side by dragging them to a screen edge or with Super and the "
            "arrow keys", fluent.icon("view-dual-symbolic", "view-grid-symbolic"), self.snap, self))
        self.snap.clicked.connect(lambda on: self.change({("multitasking", "snap_windows"): bool(on)}))
        self.on_maximize = fluent.ToggleSwitch()
        self.expander.add_row("Show snap layouts when I hover over a window's maximize button", "", self.on_maximize)
        self.on_maximize.clicked.connect(
            lambda on: self.change({("multitasking", "snap_layouts_on_maximize"): bool(on)}))
        self.on_drag = fluent.ToggleSwitch()
        self.expander.add_row("Show snap layouts when I drag a window to the top of my screen", "", self.on_drag)
        self.on_drag.clicked.connect(lambda on: self.change({("multitasking", "snap_layouts_on_drag"): bool(on)}))
        self._opened = False

    def show_state(self, snapshot: cc.Snapshot) -> None:
        snap = snapshot.get("multitasking", "snap_windows")
        self.snap.setChecked(snap)
        self.on_maximize.setChecked(snapshot.get("multitasking", "snap_layouts_on_maximize"))
        self.on_drag.setChecked(snapshot.get("multitasking", "snap_layouts_on_drag"))
        # The snap layouts are part of snapping: without it they do nothing.
        for switch in (self.on_maximize, self.on_drag):
            switch.setEnabled(snap)
        if not self._opened:
            self._opened = True
            self.expander.set_expanded(snap, animate=False)


class DesktopsSection(ConfigSection):
    page = "multitasking"
    title = "Desktops"
    order = 20
    keywords = ("virtual desktops", "desktops", "workspaces", "alt+tab", "alt tab", "taskbar windows",
                "all desktops")

    def build(self) -> None:
        self.taskbar = combo(SCOPES, width=260)
        self.taskbar.setAccessibleName("On the taskbar, show all the open windows")
        self.card("On the taskbar, show all the open windows", "", "view-grid-symbolic", self.taskbar)
        self.taskbar.activated.connect(
            lambda _i: self.change({("multitasking", "taskbar_windows"): self.taskbar.currentData()}))
        self.alt_tab = combo(SCOPES, width=260)
        self.alt_tab.setAccessibleName("Show all open windows when I press Alt+Tab")
        self.card("Show all open windows when I press Alt+Tab", "", "view-grid-symbolic",
                  self.alt_tab)
        self.alt_tab.activated.connect(
            lambda _i: self.change({("multitasking", "alt_tab_windows"): self.alt_tab.currentData()}))

    def show_state(self, snapshot: cc.Snapshot) -> None:
        select(self.taskbar, snapshot.get("multitasking", "taskbar_windows"))
        select(self.alt_tab, snapshot.get("multitasking", "alt_tab_windows"))


SECTIONS = (SnapSection, DesktopsSection)
