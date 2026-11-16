"""Sound and power settings.

Page "sound":
  Output      the output device, its volume (0 to 100 %, to 150 % behind
              "Allow louder than 100 %") and mute
  Input       the input device, its volume and mute
Page "power":
  Battery     percentage, charging state and time left; the section is not
              shown on a device without a battery
  Power mode  the profiles power-profiles-daemon offers; not shown without it
  Brightness  the backlight; not shown without one

All of it is the services bridge's state (ui/bridgeclient.py), so the
sliders follow the volume keys and the quick settings panel, and a change
made here shows up there. The time after which the screen turns off is the
compositor's setting: ui/settings/personalize.py puts it on the Power page.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QProgressBar, QSlider, QWidget

from ... import servicebridge as sb
from .widgets import BridgeSection, set_checked, set_value

PROFILE_LABELS = {"power-saver": "Power saver", "balanced": "Balanced", "performance": "Performance"}
PROFILE_TEXTS = {"power-saver": "Longest battery life, lower performance",
                 "balanced": "Good performance and battery life",
                 "performance": "Highest performance, uses more energy"}
PROFILE_ICONS = {"power-saver": "power-profile-power-saver-symbolic", "balanced": "power-profile-balanced-symbolic",
                 "performance": "power-profile-performance-symbolic"}
MIN_BRIGHTNESS = 5             # per cent: the slider never turns the screen fully dark


def percent(volume: float) -> int:
    return int(round(volume * 100))


def duration_words(seconds: int) -> str:
    """ "3 h 25 min", "40 min", "less than a minute"."""
    minutes = int(round(seconds / 60))
    if minutes < 1:
        return "less than a minute"
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h" if hours else f"{minutes} min"


def battery_text(battery: sb.Battery, on_ac: bool) -> str:
    if battery.full or (battery.percent >= 100 and on_ac):
        return "Fully charged"
    if battery.charging:
        return "Charging" + (f" · {duration_words(battery.seconds_left)} until full"
                             if battery.seconds_left is not None else "")
    if on_ac:
        return "Plugged in, not charging"
    return "On battery" + (f" · {duration_words(battery.seconds_left)} left"
                           if battery.seconds_left is not None else "")


def battery_icon(battery: sb.Battery) -> str:
    level = ("full" if battery.percent >= 90 else "good" if battery.percent >= 50 else
             "low" if battery.percent >= 20 else "caution" if battery.percent >= 8 else "empty")
    return f"battery-{level}{'-charging' if battery.charging else ''}-symbolic"


class VolumeControl(QWidget):
    """A mute button, a slider and the value in per cent."""

    def __init__(self, name: str, muted_icon: str, icon: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._icons = (icon, muted_icon)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.mute = fluent.IconButton(fluent.icon(icon), f"Mute {name}", self, checkable=True)
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, 100)
        self.slider.setPageStep(10)
        self.slider.setMinimumWidth(180)
        self.slider.setMaximumWidth(260)
        self.slider.setAccessibleName(f"{name} volume")
        self.value = fluent.Label("", "body", self, tone="secondary")
        self.value.setMinimumWidth(44)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.mute)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.value)
        self._name = name

    def show_values(self, volume: int, muted: bool) -> None:
        if not self.slider.isSliderDown():
            set_value(self.slider, min(volume, self.slider.maximum()))
        self.value.setText(f"{volume} %")
        if self.mute.isChecked() != muted:
            self.mute.setChecked(muted)
        self.mute.setIcon(fluent.icon(self._icons[1] if muted else self._icons[0]))
        self.mute.setToolTip(f"Unmute {self._name}" if muted else f"Mute {self._name}")
        self.mute.setAccessibleName(self.mute.toolTip())


class _AudioSection(BridgeSection):
    """One direction of sound: the default device, its volume and mute."""

    service = sb.AUDIO
    missing = "The sound server is not running, so sound cannot be shown or changed."
    direction = "output"
    noun = "Output"
    device_title = "Output device"
    device_text = "Where sound is played"
    none_text = "No output device was found."
    volume_title = "Volume"
    icons = ("audio-volume-high-symbolic", "audio-volume-muted-symbolic")
    commands = ("set-default-output", "set-volume", "set-muted")
    boost = True

    def __init__(self, parent: QWidget | None = None, *, bridge=None):
        super().__init__(parent, bridge=bridge)
        self.devices = QComboBox(self)
        self.devices.setMinimumWidth(240)
        self.devices.setMaximumWidth(320)
        self.device_card = self.add(fluent.SettingsCard(self.device_title, self.device_text,
                                                        fluent.icon(self.icons[0]), self.devices, self))
        self.devices.activated.connect(self._device_chosen)
        self.volume = VolumeControl(self.noun, self.icons[1], self.icons[0], self)
        self.volume_card = self.add(fluent.SettingsCard(self.volume_title, "", None, self.volume, self))
        self.volume.slider.valueChanged.connect(self._volume_moved)
        self.volume.slider.sliderReleased.connect(self.refresh)
        self.volume.mute.toggled.connect(self._mute_toggled)
        self.louder = fluent.ToggleSwitch()
        self.louder_card = fluent.SettingsCard("Allow louder than 100 %", "Amplifies quiet recordings; loud "
                                               "sound may be distorted", None, self.louder, self)
        self.louder.toggled.connect(self._louder_toggled)
        if self.boost:
            self.add(self.louder_card)
        else:
            self.louder_card.hide()

    def _lists(self, state: sb.AudioState):
        if self.direction == "output":
            return state.outputs, state.output
        return state.inputs, state.input

    def render(self, state: sb.AudioState) -> None:
        usable = self.usable()
        devices, current = self._lists(state)
        ids = [d.id for d in devices]
        if [self.devices.itemData(i) for i in range(self.devices.count())] != ids or \
                [self.devices.itemText(i) for i in range(self.devices.count())] != [d.name or f"Device {d.id}"
                                                                                    for d in devices]:
            self.devices.clear()
            for device in devices:
                self.devices.addItem(device.name or f"Device {device.id}", device.id)
        wanted = self.shown("device", current.id if current is not None else None)
        index = ids.index(wanted) if wanted in ids else -1
        if self.devices.currentIndex() != index:
            self.devices.setCurrentIndex(index)
        self.devices.setEnabled(usable and len(devices) > 0)
        self.device_card.set_description(self.device_text if devices or not state.available else self.none_text)

        has = current is not None
        real = percent(current.volume) if has else 0
        if self.boost and real > 100 and not self.louder.isChecked():
            self.louder.setChecked(True)             # it is louder already: show the real range
        self.volume.slider.setMaximum(150 if self.boost and self.louder.isChecked() else 100)
        volume = self.shown("volume", real, lambda a, b: abs(a - b) <= 1)
        muted = bool(self.shown("muted", current.muted if has else False))
        self.volume.show_values(int(volume), muted)
        self.volume.setEnabled(usable and has)
        self.volume_card.set_description(("Muted" if muted else "") if has else "")
        self.louder.setEnabled(usable and has and real <= 100)

    # --- changes ---
    def _device_chosen(self, index: int) -> None:
        ident = self.devices.itemData(index)
        _devices, current = self._lists(self.state())
        if ident is None or (current is not None and current.id == ident):
            return
        failed = f"The {self.noun.lower()} device could not be changed."
        if self.send(sb.command(self.commands[0], id=ident), failed):
            self.expect("device", ident, failed)
        self.refresh()

    def _volume_moved(self, value: int) -> None:
        if self.rendering:
            return
        failed = f"The {self.noun.lower()} volume could not be changed."
        if self.send(sb.command(self.commands[1], volume=min(value, 150) / 100), failed):
            self.expect("volume", value, failed)
        self.volume.value.setText(f"{value} %")

    def _mute_toggled(self, muted: bool) -> None:
        if self.rendering:
            return
        failed = f"{self.noun} could not be {'muted' if muted else 'unmuted'}."
        if self.send(sb.command(self.commands[2], muted=muted), failed):
            self.expect("muted", muted, failed)
        self.refresh()

    def _louder_toggled(self, _checked: bool) -> None:
        if not self.rendering and self.active:
            self.refresh()


class OutputSection(_AudioSection):
    page = "sound"
    title = "Output"
    order = 10
    keywords = ("sound", "audio", "volume", "speakers", "headphones", "mute", "output device", "louder")


class InputSection(_AudioSection):
    page = "sound"
    title = "Input"
    order = 20
    keywords = ("microphone", "mic", "input device", "recording", "input volume", "mute microphone")
    direction = "input"
    noun = "Input"
    device_title = "Input device"
    device_text = "What is used for recording and calls"
    none_text = "No microphone or other input device was found."
    volume_title = "Input volume"
    icons = ("audio-input-microphone-symbolic", "microphone-sensitivity-muted-symbolic")
    commands = ("set-default-input", "set-input-volume", "set-input-muted")
    boost = False


class BatterySection(BridgeSection):
    page = "power"
    title = "Battery"
    order = 10
    keywords = ("battery", "charge", "charging", "time left", "power source")
    service = sb.POWER

    def __init__(self, parent: QWidget | None = None, *, bridge=None):
        super().__init__(parent, bridge=bridge)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(180)
        self.bar.setAccessibleName("Battery charge")
        self.card = self.add(fluent.SettingsCard("", "", None, self.bar, self))
        self.setVisible(False)                       # until the bridge reports a battery

    def render(self, state: sb.PowerState) -> None:
        battery = state.battery
        self.setVisible(battery is not None)
        if battery is None:
            return
        self.card.set_title(f"{battery.percent} %")
        self.card.set_description(battery_text(battery, state.on_ac))
        self.card.set_icon(fluent.icon(battery_icon(battery)))
        self.bar.setValue(battery.percent)
        self.bar.setAccessibleDescription(f"{battery.percent} %")
        low = battery.percent < 20 and not battery.charging
        if self.bar.property("status") != ("error" if low else None):
            self.bar.setProperty("status", "error" if low else None)
            fluent.repolish(self.bar)


class PowerModeSection(BridgeSection):
    page = "power"
    title = "Power mode"
    order = 20
    keywords = ("power mode", "power profile", "performance", "power saver", "balanced", "energy")
    service = sb.POWER

    def __init__(self, parent: QWidget | None = None, *, bridge=None):
        super().__init__(parent, bridge=bridge)
        self.modes = QComboBox(self)
        self.modes.setMinimumWidth(180)
        self.card = self.add(fluent.SettingsCard("Power mode", "", fluent.icon(PROFILE_ICONS["balanced"]),
                                                 self.modes, self))
        self.modes.activated.connect(self._chosen)
        self.setVisible(False)                       # until the bridge reports profiles

    def render(self, state: sb.PowerState) -> None:
        self.setVisible(bool(state.profiles))
        if not state.profiles:
            return
        if [self.modes.itemData(i) for i in range(self.modes.count())] != list(state.profiles):
            self.modes.clear()
            for profile in state.profiles:
                self.modes.addItem(PROFILE_LABELS[profile], profile)
        profile = self.shown("profile", state.profile)
        index = state.profiles.index(profile) if profile in state.profiles else -1
        if self.modes.currentIndex() != index:
            self.modes.setCurrentIndex(index)
        self.modes.setEnabled(self.usable())
        self.card.set_description(PROFILE_TEXTS.get(profile, "Balance performance and energy use"))
        self.card.set_icon(fluent.icon(PROFILE_ICONS.get(profile, PROFILE_ICONS["balanced"])))

    def _chosen(self, index: int) -> None:
        profile = self.modes.itemData(index)
        if profile is None or profile == self.state().profile:
            return
        failed = "The power mode could not be changed."
        if self.send(sb.command("set-power-profile", profile=profile), failed):
            self.expect("profile", profile, failed)
        self.refresh()


class BrightnessSection(BridgeSection):
    page = "power"
    title = "Brightness"
    order = 30
    keywords = ("brightness", "backlight", "screen brightness", "dim")
    service = sb.BRIGHTNESS

    def __init__(self, parent: QWidget | None = None, *, bridge=None):
        super().__init__(parent, bridge=bridge)
        holder = QWidget(self)
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.slider = QSlider(Qt.Orientation.Horizontal, holder)
        self.slider.setRange(MIN_BRIGHTNESS, 100)
        self.slider.setPageStep(10)
        self.slider.setMinimumWidth(180)
        self.slider.setMaximumWidth(260)
        self.slider.setAccessibleName("Screen brightness")
        self.value = fluent.Label("", "body", holder, tone="secondary")
        self.value.setMinimumWidth(44)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.value)
        self.card = self.add(fluent.SettingsCard("Screen brightness", "The built-in screen's backlight",
                                                 fluent.icon("display-brightness-symbolic"), holder, self))
        self.slider.valueChanged.connect(self._moved)
        self.slider.sliderReleased.connect(self.refresh)
        self.setVisible(False)                       # until the bridge reports a backlight

    def available(self, state) -> bool:
        return state.level is not None

    def render(self, state: sb.BrightnessState) -> None:
        self.setVisible(state.level is not None)
        if state.level is None:
            return
        level = int(self.shown("level", percent(state.level), lambda a, b: abs(a - b) <= 1))
        if not self.slider.isSliderDown():
            set_value(self.slider, max(MIN_BRIGHTNESS, level))
        self.value.setText(f"{level} %")
        self.slider.setEnabled(self.bridge.status() == sb.RUNNING)

    def _moved(self, value: int) -> None:
        if self.rendering:
            return
        failed = "The brightness could not be changed."
        if self.send(sb.command("set-brightness", level=value / 100), failed):
            self.expect("level", value, failed)
        self.value.setText(f"{value} %")


SECTIONS = (OutputSection, InputSection, BatterySection, PowerModeSection, BrightnessSection)
