"""Mouse, touchpad and keyboard settings (pages mouse, touchpad, keyboard), and the pointer size alone.

The sections change the [pointer], [touchpad] and [keyboard] tables
of the compositor's configuration (compositorconfig; ConfigSection of
display.py does the loading and saving off the GUI thread). They offer
exactly the keys boswas-compositor(1) documents; drawing tablets have no
settings there, so there is no section for them. PointerSection shows the
pointer size of the Mouse page once more, on Accessibility > Mouse pointer.

The compositor draws its own pointer at the new size at once. X11 programs
and programs that draw their own pointer read XCURSOR_SIZE, which the
session sets from the same key when it starts (boswas-session); after a
change the user manager and D-Bus activation get the new value too
(``systemctl --user set-environment`` and
``dbus-update-activation-environment``, publish_cursor_size), so programs
they start from then on use it. Programs already running keep their size.

Keyboard layouts are shown by their names from the XKB data
(boswas_control_center.xkb). XKB keeps four layouts at a time (the
compositor's keymap drops a fifth), so no more than MAX_LAYOUTS can be
added. Super+Space always switches to the next layout; the page can add one
of XKB's own switching shortcuts (the ``grp:`` options) to
``keyboard.options``, keeping the other options that are set there.

Whether the device has a touchpad is read from /proc/bus/input/devices
(touchpads(): a device with the "finger" tool that is not a touch screen or
a pen). The settings are kept either way; without a touchpad the section
says that they apply when one is connected.
"""

from __future__ import annotations

import struct

import boswas_fluent as fluent
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QSlider, QWidget

from ... import commands
from ... import compositorconfig as cc
from ... import xkb
from ...errors import CommandFailed, CommandUnavailable
from . import context
from .display import ConfigSection, combo, select

INPUT_DEVICES = "/proc/bus/input/devices"
MAX_LAYOUTS = xkb.MAX_LAYOUTS
POINTER_SIZES = ((24, "Small"), (32, "Medium"), (48, "Large"), (64, "Extra large"))
POINTER_SIZE_TEXT = ("The desktop's pointer changes at once. X11 programs and programs that draw their own pointer "
                     "use the new size the next time you start them; open ones keep theirs, and so do programs "
                     "started from a terminal that was already open")
ENVIRONMENT_TIMEOUT = 10.0
_BTN_TOOL_PEN, _BTN_TOOL_FINGER = 0x140, 0x145
_INPUT_PROP_DIRECT = 1


def _bits(words: str) -> int:
    """A bit field as the kernel prints it: hexadecimal words of one ``long`` each, the highest first."""
    value, width = 0, struct.calcsize("l") * 8
    for word in words.split()[:64]:
        value = (value << width) | int(word, 16)
    return value


def touchpads(text: str) -> list[str]:
    """The names of the touchpads in the text of /proc/bus/input/devices; lines it cannot read are skipped."""
    found = []
    for block in text.split("\n\n")[:512]:
        name, keys, properties = "", 0, 0
        for line in block.splitlines():
            try:
                if line.startswith("N: Name="):
                    name = line[len("N: Name="):].strip().strip('"')
                elif line.startswith("B: KEY="):
                    keys = _bits(line[len("B: KEY="):])
                elif line.startswith("B: PROP="):
                    properties = _bits(line[len("B: PROP="):])
            except ValueError:
                continue
        if keys >> _BTN_TOOL_FINGER & 1 and not keys >> _BTN_TOOL_PEN & 1 and not properties >> _INPUT_PROP_DIRECT & 1:
            found.append("".join(ch for ch in name if ch.isprintable())[:80] or "Touchpad")
    return found


def read_touchpads(path: str = INPUT_DEVICES) -> list[str] | None:
    """The touchpads of this device; None when the kernel's list cannot be read."""
    try:
        with open(path, "rb") as handle:
            return touchpads(handle.read(1024 * 1024).decode("utf-8", "replace"))
    except OSError:
        return None


def slider_row(low: int, high: int, left: str, right: str, name: str) -> tuple[QWidget, QSlider, fluent.Label]:
    """A slider between two words, with a value label after it; returns (the row, the slider, the value label)."""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(low, high)
    slider.setFixedWidth(200)
    slider.setAccessibleName(name)
    value = fluent.Label("", "body", tone="secondary")
    value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(fluent.Label(left, "caption", tone="secondary"))
    row.addWidget(slider)
    row.addWidget(fluent.Label(right, "caption", tone="secondary"))
    row.addWidget(value)
    return holder, slider, value


def publish_cursor_size(runner, size: object) -> list[list[str]]:
    """Give the user manager and D-Bus activation the pointer size (XCURSOR_SIZE) for what they start from now on.

    Runs on a worker thread after the size was saved. Best effort: a program
    that is missing or fails changes nothing about the setting itself.
    Returns the commands that ran.
    """
    ran = []
    if isinstance(size, bool) or not isinstance(size, int):
        return ran
    for program in ("systemctl", "dbus-update-activation-environment"):
        try:
            command = commands.cursor_environment_argv(program, size)
        except ValueError:
            return ran
        if not runner.available(command[0]):
            continue
        try:
            runner.run(command, ENVIRONMENT_TIMEOUT)
        except (CommandFailed, CommandUnavailable):
            continue
        ran.append(command)
    return ran


class _SizeSection(ConfigSection):
    """A section with the pointer size: after a change of it the session's environment follows."""

    def __init__(self, parent: QWidget | None = None, *, config: cc.Settings | None = None, runner=None) -> None:
        self.runner = runner if runner is not None else context.runner()
        super().__init__(parent, config=config)

    def perform(self, changes: dict) -> object:
        applied = super().perform(changes)
        if ("pointer", "cursor_size") in changes:
            publish_cursor_size(self.runner, changes[("pointer", "cursor_size")])
        return applied


class _Pointing(ConfigSection):
    """What the mouse and the touchpad sections share: the speed slider and the bound switches."""

    table = ""

    def speed_card(self, description: str) -> None:
        holder, self.speed, value = slider_row(-10, 10, "Slow", "Fast", "Pointer speed")
        value.hide()
        self.speed.setPageStep(2)
        self.card("Pointer speed", description, "input-mouse-symbolic", holder)
        self.speed.valueChanged.connect(lambda step: self.change_later({(self.table, "accel_speed"): step / 10}))
        self.speed.sliderReleased.connect(self._send_delayed)

    def show_speed(self, snapshot: cc.Snapshot) -> None:
        if not self.speed.isSliderDown():
            self.speed.setValue(round(snapshot.get(self.table, "accel_speed") * 10))


class MouseSection(_SizeSection, _Pointing):
    page = "mouse"
    title = "Mouse"
    order = 30
    keywords = ("mouse", "pointer", "cursor", "speed", "acceleration", "left-handed", "primary button", "scroll",
                "wheel", "cursor size", "trackball")
    table = "pointer"
    reports_files = True

    def build(self) -> None:
        self.speed_card("How far the pointer moves when you move the mouse")
        self.precision = fluent.ToggleSwitch()
        self.card("Enhance pointer precision", "The pointer travels further the faster you move the mouse",
                  "zoom-in-symbolic", self.precision)
        self.precision.clicked.connect(
            lambda on: self.change({("pointer", "accel_profile"): "adaptive" if on else "flat"}))
        self.primary = combo((("Left", False), ("Right", True)))
        self.card("Primary button", "The button that selects and drags", "input-mouse-symbolic", self.primary)
        self.primary.activated.connect(lambda _i: self.change({("pointer", "left_handed"): self.primary.currentData()}))
        self.scroll = combo((("Wheel down scrolls down", False), ("Wheel down scrolls up", True)))
        self.card("Scroll direction", "Which way the content moves when you turn the wheel", "view-list-symbolic",
                  self.scroll)
        self.scroll.activated.connect(
            lambda _i: self.change({("pointer", "natural_scroll"): self.scroll.currentData()}))
        self.size = combo((f"{label} ({size})", size) for size, label in POINTER_SIZES)
        self.card("Pointer size", POINTER_SIZE_TEXT, "view-fullscreen-symbolic", self.size)
        self.size.activated.connect(lambda _i: self.change({("pointer", "cursor_size"): self.size.currentData()}))

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.show_speed(snapshot)
        self.precision.setChecked(snapshot.get("pointer", "accel_profile") == "adaptive")
        select(self.primary, snapshot.get("pointer", "left_handed"))
        select(self.scroll, snapshot.get("pointer", "natural_scroll"))
        size = snapshot.get("pointer", "cursor_size")
        for index in reversed(range(self.size.count())):
            if self.size.itemData(index) not in dict(POINTER_SIZES):
                self.size.removeItem(index)
        select(self.size, size, f"Custom ({size})")


class PointerSection(_SizeSection):
    """The size of the pointer alone, for Accessibility > Mouse pointer (the Mouse page has it too)."""

    page = "mouse-pointer"
    title = "Mouse pointer"
    order = 10
    keywords = ("pointer size", "cursor size", "bigger pointer", "larger cursor", "visibility")

    def build(self) -> None:
        self.size = combo((f"{label} ({size})", size) for size, label in POINTER_SIZES)
        self.card("Pointer size", f"A bigger pointer is easier to find. {POINTER_SIZE_TEXT}",
                  "view-fullscreen-symbolic", self.size)
        self.size.activated.connect(lambda _i: self.change({("pointer", "cursor_size"): self.size.currentData()}))

    def show_state(self, snapshot: cc.Snapshot) -> None:
        size = snapshot.get("pointer", "cursor_size")
        for index in reversed(range(self.size.count())):
            if self.size.itemData(index) not in dict(POINTER_SIZES):
                self.size.removeItem(index)
        select(self.size, size, f"Custom ({size})")


class TouchpadSection(_Pointing):
    page = "touchpad"
    title = "Touchpad"
    order = 40
    keywords = ("touchpad", "trackpad", "tap to click", "natural scrolling", "gestures", "three fingers", "swipe",
                "disable while typing", "palm")
    table = "touchpad"

    def __init__(self, parent: QWidget | None = None, *, config: cc.Settings | None = None,
                 devices_file: str = INPUT_DEVICES) -> None:
        self.devices_file = devices_file
        super().__init__(parent, config=config)

    def build(self) -> None:
        self.absent = self.add(fluent.InfoBar("info", parent=self, closable=False))
        self.absent.hide()
        self.tap = self.toggle("Tap to click", "Tap the touchpad with one finger instead of pressing it",
                               "input-touchpad-symbolic", "touchpad", "tap_to_click")
        self.scroll = combo((("Content follows your fingers", True), ("Content moves the other way", False)))
        self.card("Scroll direction", "Which way the content moves when you slide two fingers", "view-list-symbolic",
                  self.scroll)
        self.scroll.activated.connect(
            lambda _i: self.change({("touchpad", "natural_scroll"): self.scroll.currentData()}))
        self.speed_card("How far the pointer moves when you slide a finger")
        self.typing = self.toggle("Ignore the touchpad while typing", "Keeps the pointer from jumping when a palm "
                                  "brushes the touchpad", "input-keyboard-symbolic", "touchpad", "disable_while_typing")
        self.gestures = self.toggle("Three- and four-finger gestures", "Swipe sideways to change desktop, up for "
                                    "Task View, down to show the desktop", "view-task-symbolic", "touchpad",
                                    "gestures")

    def load(self) -> object:
        return self.config.snapshot(), read_touchpads(self.devices_file)

    def show_state(self, state: object) -> None:
        snapshot, found = state
        self.absent.setVisible(found == [])
        if found == []:
            self.absent.show_message("info", "No touchpad was found on this device. These settings apply when one "
                                     "is connected.")
        self.tap.setChecked(snapshot.get("touchpad", "tap_to_click"))
        select(self.scroll, snapshot.get("touchpad", "natural_scroll"))
        self.show_speed(snapshot)
        self.typing.setChecked(snapshot.get("touchpad", "disable_while_typing"))
        self.gestures.setChecked(snapshot.get("touchpad", "gestures"))


class LayoutRow(QWidget):
    """The buttons of one configured layout: move up, move down, remove."""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.up = fluent.IconButton("go-up-symbolic", f"Move {name} up", self)
        self.down = fluent.IconButton("go-down-symbolic", f"Move {name} down", self)
        self.remove = fluent.IconButton("edit-delete-symbolic", f"Remove {name}", self)
        for button in (self.up, self.down, self.remove):
            row.addWidget(button)


class KeyboardSection(ConfigSection):
    page = "keyboard"
    title = "Keyboard"
    order = 50
    keywords = ("keyboard", "layout", "language", "input", "repeat", "delay", "num lock", "typing", "switch layout",
                "qwerty", "dvorak")

    def __init__(self, parent: QWidget | None = None, *, config: cc.Settings | None = None,
                 catalog: xkb.Catalog | None = None) -> None:
        self.catalog = catalog                 # loaded with the first state when not given
        self.pairs: list[tuple[str, str]] = []
        self.options = ""
        self.layout_rows: list[LayoutRow] = []
        self.add_dialog: fluent.ContentDialog | None = None
        super().__init__(parent, config=config)

    def build(self) -> None:
        self.add_button = fluent.Button("Add a layout", icon="list-add-symbolic")
        self.add_button.clicked.connect(self.open_add_dialog)
        self.layouts = self.add(fluent.SettingsExpander(
            "Keyboard layouts", "", "input-keyboard-symbolic", self.add_button, self, expanded=True))
        self.problem = self.add(fluent.InfoBar("warning", parent=self, closable=False))
        self.problem.hide()
        self.switch = combo(width=260)
        self.card("Switch layouts with", "Super+Space always goes to the next layout; this adds a second shortcut",
                  "media-playlist-repeat-symbolic", self.switch)
        self.switch.activated.connect(self._switch_changed)

        holder, self.delay, self.delay_value = slider_row(cc.REPEAT_DELAY[0] // 50, cc.REPEAT_DELAY[1] // 50,
                                                          "Short", "Long", "Repeat delay")
        self.delay_value.setMinimumWidth(64)
        self.card("Repeat delay", "How long a key is held before it starts to repeat",
                  "preferences-system-time-symbolic", holder)
        self.delay.valueChanged.connect(self._delay_changed)
        self.delay.sliderReleased.connect(self._send_delayed)
        holder, self.rate, self.rate_value = slider_row(*cc.REPEAT_RATE, "Slow", "Fast", "Repeat rate")
        self.rate_value.setMinimumWidth(64)
        self.rate.setPageStep(5)
        self.card("Repeat rate", "How fast a held key repeats", "media-seek-forward-symbolic", holder)
        self.rate.valueChanged.connect(self._rate_changed)
        self.rate.sliderReleased.connect(self._send_delayed)
        self.try_box = QLineEdit()
        self.try_box.setPlaceholderText("Hold down a key")
        self.try_box.setMinimumWidth(260)
        self.try_box.setClearButtonEnabled(True)
        self.card("Try it here", "Hold down a key to feel the delay and the rate", "document-edit-symbolic",
                  self.try_box)
        self.numlock = self.toggle("Num Lock", "Turn Num Lock on when you sign in", "input-keyboard-symbolic",
                                   "keyboard", "numlock")

    # --- state ---
    def load(self) -> object:
        if self.catalog is None:
            self.catalog = xkb.load()
        return self.config.snapshot()

    def show_state(self, snapshot: cc.Snapshot) -> None:
        if not self.delay.isSliderDown():
            self.delay.setValue(round(snapshot.get("keyboard", "repeat_delay") / 50))
        if not self.rate.isSliderDown():
            self.rate.setValue(snapshot.get("keyboard", "repeat_rate"))
        self._delay_changed(self.delay.value())
        self._rate_changed(self.rate.value())
        self.numlock.setChecked(snapshot.get("keyboard", "numlock"))
        self.options = snapshot.get("keyboard", "options")
        pairs = xkb.split(snapshot.get("keyboard", "layout"), snapshot.get("keyboard", "variant"))
        self.problem.setVisible(bool(self.catalog.problem))
        if self.catalog.problem:
            self.problem.show_message("warning", self.catalog.problem + " Layouts are shown by their short names "
                                      "and none can be added.")
        self._show_layouts(pairs)
        self._show_switch()

    def _show_layouts(self, pairs: list[tuple[str, str]]) -> None:
        self.add_button.setEnabled(len(pairs) < MAX_LAYOUTS and bool(self.catalog.layouts))
        self.layouts.header.set_description(
            "The first layout is used when you sign in" if len(pairs) < MAX_LAYOUTS else
            f"{MAX_LAYOUTS} layouts are the most the desktop can switch between")
        if pairs == self.pairs and self.layout_rows:
            return
        self.pairs = list(pairs)
        for card in self.layouts.cards():
            self.layouts.content_layout.removeWidget(card)
            card.deleteLater()
        self.layouts._cards.clear()
        self.layout_rows = []
        for index, (layout, variant) in enumerate(pairs):
            name = self.catalog.name(layout, variant)
            codes = f"{layout} ({variant})" if variant else layout
            if index >= MAX_LAYOUTS:
                codes += " – not in use: the desktop keeps four layouts"
            buttons = LayoutRow(name)
            buttons.up.setEnabled(index > 0)
            buttons.down.setEnabled(index < len(pairs) - 1)
            buttons.remove.setEnabled(len(pairs) > 1)
            buttons.up.clicked.connect(lambda _c=False, i=index: self.move_layout(i, -1))
            buttons.down.clicked.connect(lambda _c=False, i=index: self.move_layout(i, 1))
            buttons.remove.clicked.connect(lambda _c=False, i=index: self.remove_layout(i))
            self.layouts.add_row(name, codes, buttons)
            self.layout_rows.append(buttons)

    def _show_switch(self) -> None:
        current = xkb.switch_option(self.options)
        self.switch.clear()
        self.switch.addItem("No second shortcut", "")
        for option in self.catalog.switch_options():
            self.switch.addItem(option.name, option.code)
        select(self.switch, current, current)

    # --- changes ---
    def _delay_changed(self, step: int) -> None:
        self.delay_value.setText(f"{step * 50} ms")
        self.delay.setAccessibleDescription(f"{step * 50} milliseconds")
        self.change_later({("keyboard", "repeat_delay"): step * 50})

    def _rate_changed(self, rate: int) -> None:
        self.rate_value.setText(f"{rate} / s")
        self.rate.setAccessibleDescription(f"{rate} repeats per second")
        self.change_later({("keyboard", "repeat_rate"): rate})

    def _switch_changed(self, _index: int) -> None:
        self.change({("keyboard", "options"): xkb.with_switch_option(self.options, self.switch.currentData())})

    def set_layouts(self, pairs: list[tuple[str, str]]) -> None:
        """Save the list of layouts (at least one; the rows follow at once)."""
        if not pairs or self.loading:
            return
        try:
            layout, variant = xkb.join(pairs)
        except ValueError as exc:
            self.notice.show_message("error", f"These layouts cannot be saved ({exc}).")
            return
        self.loading = True                    # redraw the rows without treating it as another change
        try:
            self._show_layouts(pairs)
        finally:
            self.loading = False
        self.change({("keyboard", "layout"): layout, ("keyboard", "variant"): variant})

    def move_layout(self, index: int, step: int) -> None:
        pairs, target = list(self.pairs), index + step
        if 0 <= index < len(pairs) and 0 <= target < len(pairs):
            pairs[index], pairs[target] = pairs[target], pairs[index]
            self.set_layouts(pairs)
            if target < len(self.layout_rows):         # the focus follows the layout that moved
                button = self.layout_rows[target].up if step < 0 else self.layout_rows[target].down
                (button if button.isEnabled() else self.layout_rows[target].remove).setFocus()

    def remove_layout(self, index: int) -> None:
        if len(self.pairs) > 1 and 0 <= index < len(self.pairs):
            self.set_layouts(self.pairs[:index] + self.pairs[index + 1:])

    def add_layout(self, pair: tuple[str, str]) -> None:
        if pair not in self.pairs and len(self.pairs) < MAX_LAYOUTS:
            self.set_layouts(self.pairs + [pair])

    # --- the "add a layout" dialog ---
    def open_add_dialog(self) -> None:
        if self.add_dialog is not None or self.catalog is None or len(self.pairs) >= MAX_LAYOUTS:
            return
        dialog = fluent.ContentDialog(self, "Add a keyboard layout", primary="Add", close="Cancel")
        self.add_dialog = dialog
        self.add_search = fluent.SearchBox(placeholder="Search layouts, for example German or Dvorak")
        self.add_list = QListWidget()
        self.add_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.add_list.setMinimumHeight(300)
        self.add_list.setAccessibleName("Keyboard layouts")
        self.add_empty = fluent.Label("No layout matches.", "body", tone="secondary")
        self.add_empty.hide()
        for widget in (self.add_search, self.add_list, self.add_empty):
            dialog.set_content(widget)
        self.add_search.textChanged.connect(self._fill_add_list)
        self.add_search.returnPressed.connect(self._accept_first)
        self.add_list.itemSelectionChanged.connect(
            lambda: dialog.primary_button.setEnabled(bool(self.add_list.selectedItems())))
        self.add_list.itemActivated.connect(lambda _item: dialog.accept())
        dialog.finished.connect(self._add_answered)
        self._fill_add_list("")
        dialog.open()
        self.add_search.setFocus()

    def _fill_add_list(self, query: str) -> None:
        self.add_list.clear()
        for choice in self.catalog.search(query):
            if choice.pair in self.pairs:
                continue
            item = QListWidgetItem(choice.name)
            item.setData(Qt.ItemDataRole.UserRole, choice.pair)
            item.setToolTip(f"{choice.layout} ({choice.variant})" if choice.variant else choice.layout)
            self.add_list.addItem(item)
        self.add_empty.setVisible(self.add_list.count() == 0)
        self.add_list.setVisible(self.add_list.count() > 0)
        if query.strip() and self.add_list.count():
            self.add_list.setCurrentRow(0)
        self.add_dialog.primary_button.setEnabled(bool(self.add_list.selectedItems()))

    def _accept_first(self) -> None:
        if self.add_list.selectedItems():
            self.add_dialog.accept()

    def _add_answered(self, result: object) -> None:
        dialog, self.add_dialog = self.add_dialog, None
        chosen = self.add_list.selectedItems()
        pair = chosen[0].data(Qt.ItemDataRole.UserRole) if chosen else None
        dialog.deleteLater()
        if result == fluent.ContentDialog.Result.PRIMARY and pair is not None:
            self.add_layout(tuple(pair))

    def deactivate(self) -> None:
        if self.add_dialog is not None:
            self.add_dialog.reject()
        super().deactivate()


SECTIONS = (MouseSection, PointerSection, TouchpadSection, KeyboardSection)
