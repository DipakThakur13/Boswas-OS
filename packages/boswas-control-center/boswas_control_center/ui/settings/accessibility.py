"""The Accessibility category: text size, visual effects, colour filters, the magnifier and the keyboard.

Page "text-size":
  Text size         100 to 225 percent, previewed and applied with a button
                    (as dragging the slider would re-lay every window out at
                    each step): [accessibility] text_scale. The shell, the
                    title bars and every Qt application draw their text that
                    much larger; nothing else grows.
Page "visual-effects" (with Animation effects of personalize.MotionSection
and Transparency effects of colors.TransparencySection):
  Visual effects    always show scroll bars ([accessibility]
                    always_show_scrollbars, the Qt applications) and how long
                    notifications stay ([notifications] toast_seconds).
Page "color-filters":
  Colour filters    on or off, the filter, and the Super+Ctrl+C shortcut:
                    [color_filter] enabled, filter, shortcut. Drawn by the
                    compositor with OpenGL ES; where it draws with pixman the
                    page says that they are not available (the compositor's
                    ``version`` answer tells).
Page "magnifier":
  Magnifier         on or off, the zoom and the zoom increment of
                    Super+Plus and Super+Minus: [magnifier] enabled, zoom,
                    step.
Page "accessibility-keyboard":
  Sticky keys       [keyboard] sticky_keys, sticky_keys_lock.
  Filter keys       [keyboard] filter_keys, slow_keys_ms, bounce_keys_ms.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QSlider, QWidget

from ... import compositorconfig as cc
from .display import ConfigSection, combo, select

TOAST_TIMES = ((5, "5 seconds"), (7, "7 seconds"), (15, "15 seconds"), (30, "30 seconds"), (60, "1 minute"),
               (300, "5 minutes"))
FILTERS = (("grayscale", "Greyscale"), ("inverted", "Inverted"), ("grayscale_inverted", "Greyscale inverted"),
           ("deuteranopia", "Red-green (green weak, deuteranopia)"),
           ("protanopia", "Red-green (red weak, protanopia)"),
           ("tritanopia", "Blue-yellow (tritanopia)"))
ZOOM_STEPS = tuple((f"{step} %", step) for step in cc.MAGNIFIER_STEPS)
SLOW_KEYS = ((0, "Off"), (300, "0.3 seconds"), (500, "0.5 seconds"), (1000, "1 second"), (1500, "1.5 seconds"),
             (2000, "2 seconds"))
BOUNCE_KEYS = ((0, "Off"), (300, "0.3 seconds"), (500, "0.5 seconds"), (1000, "1 second"), (2000, "2 seconds"))
SAMPLE = "The quick brown fox jumps over the lazy dog. Drag the slider until this text is easy to read."


def seconds_text(milliseconds: int) -> str:
    if milliseconds == 0:
        return "Off"
    value = f"{milliseconds / 1000:.2f}".rstrip("0").rstrip(".")
    return f"{value} second{'s' if value != '1' else ''}"


class TextSizeSection(ConfigSection):
    page = "text-size"
    title = "Text size"
    order = 10
    keywords = ("text size", "font size", "bigger text", "larger text", "make text bigger", "zoom text", "dpi")
    reports_files = True

    def build(self) -> None:
        card = self.add(fluent.Card(self, padding=16, spacing=12))
        self.preview = fluent.Label(SAMPLE, "body", card, wrap=True)
        self.preview.setMinimumHeight(72)
        self.preview.setAccessibleName("Text size preview")
        card.add(self.preview)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(*cc.TEXT_SCALE)
        self.slider.setSingleStep(5)
        self.slider.setPageStep(25)
        self.slider.setMinimumWidth(220)
        self.slider.setAccessibleName("Text size")
        self.percent = fluent.Label("100 %", "body", tone="secondary")
        self.percent.setMinimumWidth(52)
        self.apply_button = fluent.Button("Apply", accent=True)
        self.apply_button.setEnabled(False)
        row.addWidget(self.slider, 1)
        row.addWidget(self.percent)
        row.addWidget(self.apply_button)
        self.card("Text size", "Text in the taskbar, Start, title bars and applications; nothing else grows",
                  "format-text-larger-symbolic", holder)
        self.slider.valueChanged.connect(self._moved)
        self.apply_button.clicked.connect(self._apply)
        self.current = 100

    def _moved(self, value: int) -> None:
        self.percent.setText(f"{value} %")
        self.slider.setAccessibleDescription(f"{value} percent")
        font = QFont(fluent.font("body"))
        base = fluent.theme().type("body").size * 100 / max(fluent.theme().text_scale, 100)
        font.setPixelSize(max(1, round(base * value / 100)))
        self.preview.setFont(font)
        self.apply_button.setEnabled(value != self.current)

    def _apply(self) -> None:
        self.change({("accessibility", "text_scale"): self.slider.value()})

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.current = snapshot.get("accessibility", "text_scale")
        if not self.slider.isSliderDown():
            self.slider.setValue(self.current)
        self._moved(self.slider.value())


class VisualEffectsSection(ConfigSection):
    page = "visual-effects"
    title = "Visual effects"
    order = 10
    keywords = ("scroll bars", "scrollbars", "always show scrollbars", "notifications", "dismiss notifications",
                "notification time", "toast")
    reports_files = True

    def build(self) -> None:
        self.scrollbars = self.toggle("Always show scroll bars", "Full scroll bars instead of thin lines that widen "
                                      "under the pointer, in Boswas applications", "view-list-symbolic",
                                      "accessibility", "always_show_scrollbars")
        self.toasts = combo(((label, seconds) for seconds, label in TOAST_TIMES), width=180)
        self.toasts.setAccessibleName("Dismiss notifications after this amount of time")
        self.card("Dismiss notifications after this amount of time", "How long a notification stays on screen at "
                  "least", "notification-symbolic", self.toasts)
        self.toasts.activated.connect(
            lambda _i: self.change({("notifications", "toast_seconds"): self.toasts.currentData()}))

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.scrollbars.setChecked(snapshot.get("accessibility", "always_show_scrollbars"))
        select(self.toasts, snapshot.get("notifications", "toast_seconds"))


class ColourFiltersSection(ConfigSection):
    page = "color-filters"
    title = "Colour filters"
    order = 10
    keywords = ("colour filters", "color filters", "greyscale", "grayscale", "invert", "inverted colours",
                "colour blindness", "color blindness", "deuteranopia", "protanopia", "tritanopia", "red-green",
                "blue-yellow")
    reports_files = True

    def build(self) -> None:
        self.enabled = self.toggle("Colour filters", "Show the whole screen through a filter: greyscale, inverted, "
                                   "or one that helps to tell colours apart", "applications-graphics-symbolic",
                                   "color_filter", "enabled")
        self.filter = combo(((label, name) for name, label in FILTERS), width=300)
        self.filter.setAccessibleName("Colour filter")
        self.filter_card = self.card("Filter", "", "preferences-desktop-color-symbolic", self.filter)
        self.filter.activated.connect(lambda _i: self.change({("color_filter", "filter"): self.filter.currentData()}))
        self.shortcut = self.toggle("Keyboard shortcut for colour filters", "Super+Ctrl+C turns the filter on and off",
                                    "input-keyboard-symbolic", "color_filter", "shortcut")
        self.unavailable = self.add(fluent.InfoBar("info", parent=self, closable=False))
        self.unavailable.hide()

    def load(self) -> object:
        snapshot = self.config.snapshot()
        try:
            answer = self.config.control.request("version")
        except cc.ControlError:
            answer = None                      # no desktop here: the settings wait for the next sign-in
        available = answer.get("color_filters") if isinstance(answer, dict) else None
        return snapshot, available

    def show_state(self, state: object) -> None:
        snapshot, available = state
        self.enabled.setChecked(snapshot.get("color_filter", "enabled"))
        select(self.filter, snapshot.get("color_filter", "filter"))
        self.shortcut.setChecked(snapshot.get("color_filter", "shortcut"))
        usable = available is not False
        for control in (self.enabled, self.filter, self.shortcut):
            control.setEnabled(usable)
        if usable:
            self.unavailable.hide()
        else:
            self.unavailable.show_message("info", "Colour filters are drawn with OpenGL ES. This device draws the "
                                          "screen in software (pixman), so they are not available here.")


class MagnifierSection(ConfigSection):
    page = "magnifier"
    title = "Magnifier"
    order = 10
    keywords = ("magnifier", "magnify", "zoom", "enlarge", "bigger", "lens", "screen zoom")
    reports_files = True

    def build(self) -> None:
        self.enabled = self.toggle("Magnifier", "Enlarge the screen around the pointer. Super+Plus starts it and "
                                   "zooms in, Super+Minus zooms out, Super+Esc ends it", "zoom-in-symbolic",
                                   "magnifier", "enabled")
        self.zoom = fluent.NumberBox(value=200, minimum=cc.MAGNIFIER_ZOOM[0], maximum=cc.MAGNIFIER_ZOOM[1], step=100,
                                     suffix=" %")
        self.zoom.setAccessibleName("Zoom level")
        self.zoom.setMinimumWidth(120)
        self.card("Zoom level", "How much Magnifier enlarges when it starts", "zoom-fit-best-symbolic", self.zoom)
        self.zoom.valueChanged.connect(self._zoom_changed)
        self.step = combo(ZOOM_STEPS, width=120)
        self.step.setAccessibleName("Zoom increment")
        self.card("Zoom increment", "How much each Super+Plus and Super+Minus changes the zoom", "list-add-symbolic",
                  self.step)
        self.step.activated.connect(lambda _i: self.change({("magnifier", "step"): self.step.currentData()}))

    def _zoom_changed(self, value: float) -> None:
        self.change_later({("magnifier", "zoom"): int(round(value))})

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.enabled.setChecked(snapshot.get("magnifier", "enabled"))
        step = snapshot.get("magnifier", "step")
        select(self.step, step, f"{step} %")
        self.zoom.setSingleStep(step)
        if not self.zoom.hasFocus():
            blocked = self.zoom.blockSignals(True)
            self.zoom.setValue(snapshot.get("magnifier", "zoom"))
            self.zoom.blockSignals(blocked)


class KeyboardAccessSection(ConfigSection):
    page = "accessibility-keyboard"
    title = "Keyboard"
    order = 10
    keywords = ("sticky keys", "filter keys", "slow keys", "bounce keys", "modifier keys", "shift", "keyboard",
                "repeated keystrokes", "accessibility keyboard")
    reports_files = True

    def build(self) -> None:
        self.sticky = fluent.ToggleSwitch()
        self.sticky_row = self.add(fluent.SettingsExpander(
            "Sticky keys", "Press Shift, Ctrl or Alt and then the other key, one after the other",
            "input-keyboard-symbolic", self.sticky, self))
        self.sticky.clicked.connect(lambda on: self.change({("keyboard", "sticky_keys"): bool(on)}))
        self.lock = fluent.ToggleSwitch()
        self.sticky_row.add_row("Lock shortcut keys when pressed twice in a row", "Press the key a third time to "
                                "release it", self.lock)
        self.lock.clicked.connect(lambda on: self.change({("keyboard", "sticky_keys_lock"): bool(on)}))

        self.filter = fluent.ToggleSwitch()
        self.filter_row = self.add(fluent.SettingsExpander(
            "Filter keys", "Ignore brief or repeated keystrokes", "input-keyboard-symbolic", self.filter, self))
        self.filter.clicked.connect(lambda on: self.change({("keyboard", "filter_keys"): bool(on)}))
        self.slow = combo(((label, ms) for ms, label in SLOW_KEYS), width=160)
        self.slow.setAccessibleName("Ignore quick keystrokes")
        self.filter_row.add_row("Ignore quick keystrokes", "A key counts only after it was held this long", self.slow)
        self.slow.activated.connect(lambda _i: self.change({("keyboard", "slow_keys_ms"): self.slow.currentData()}))
        self.bounce = combo(((label, ms) for ms, label in BOUNCE_KEYS), width=160)
        self.bounce.setAccessibleName("Ignore unintended keystrokes")
        self.filter_row.add_row("Ignore unintended keystrokes", "A key pressed again this soon after it was "
                                "released does not count", self.bounce)
        self.bounce.activated.connect(lambda _i: self.change({("keyboard", "bounce_keys_ms"): self.bounce.currentData()}))
        self._opened = False

    def show_state(self, snapshot: cc.Snapshot) -> None:
        sticky, filtering = snapshot.get("keyboard", "sticky_keys"), snapshot.get("keyboard", "filter_keys")
        self.sticky.setChecked(sticky)
        self.lock.setChecked(snapshot.get("keyboard", "sticky_keys_lock"))
        self.lock.setEnabled(sticky)
        self.filter.setChecked(filtering)
        for box, key in ((self.slow, "slow_keys_ms"), (self.bounce, "bounce_keys_ms")):
            value = snapshot.get("keyboard", key)
            select(box, value, seconds_text(value))
            box.setEnabled(filtering)
        if not self._opened:
            self._opened = True
            self.sticky_row.set_expanded(sticky, animate=False)
            self.filter_row.set_expanded(filtering, animate=False)


SECTIONS = (TextSizeSection, VisualEffectsSection, ColourFiltersSection, MagnifierSection, KeyboardAccessSection)
