"""Sound and power settings, driven through a fake bridge.

Run: QT_QPA_PLATFORM=offscreen python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402,F401

from boswas_control_center import servicebridge as sb  # noqa: E402

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    PYSIDE = True
except ImportError:
    PYSIDE = False

if PYSIDE:
    from boswas_fluent import testing
    from boswas_control_center.ui import bridgeclient, settings
    from boswas_control_center.ui.settings import sound_power, widgets
    from boswas_control_center.ui.tiles import SectionGroup

    AD = sb.AudioDevice
    AUDIO = sb.AudioState(True, AD(40, "Speakers", 0.62, False, True), AD(51, "Microphone", 0.8, False, True),
                          (AD(40, "Speakers", 0.62, False, True), AD(41, "Headphones", 0.5, False),
                           AD(42, "", 1.0, True)),
                          (AD(51, "Microphone", 0.8, False, True), AD(52, "USB Headset", 0.4, True)))
    POWER = sb.PowerState(sb.Battery(76, False, False, 3 * 3600 + 25 * 60), False, "balanced",
                          ("power-saver", "balanced", "performance"), True, True)

    class Case(testing.FluentTestCase):
        states: dict = {}

        def setUp(self):
            super().setUp()
            self.bridge = bridgeclient.FakeBridge(states=dict(self.states))
            self.host = QWidget()
            QVBoxLayout(self.host)
            patch = mock.patch.object(widgets, "PENDING_SECONDS", 0.15)
            patch.start()
            self.addCleanup(patch.stop)

        def section(self, cls):
            section = cls(self.host, bridge=self.bridge)
            self.host.layout().addWidget(section)
            self.show(self.host, 900, 600)
            section.activate()
            testing.process()
            return section

        def last(self):
            found = self.bridge.commands()
            return found[-1] if found else None


class TextTest(unittest.TestCase):
    @unittest.skipUnless(PYSIDE, "PySide6 is not installed")
    def test_battery_words(self):
        B = sb.Battery
        self.assertEqual(sound_power.battery_text(B(76, False, False, 12300), False), "On battery · 3 h 25 min left")
        self.assertEqual(sound_power.battery_text(B(76, False, False, None), False), "On battery")
        self.assertEqual(sound_power.battery_text(B(40, True, False, 2400), True), "Charging · 40 min until full")
        self.assertEqual(sound_power.battery_text(B(40, True, False, None), True), "Charging")
        self.assertEqual(sound_power.battery_text(B(100, False, True, None), True), "Fully charged")
        self.assertEqual(sound_power.battery_text(B(80, False, False, None), True), "Plugged in, not charging")
        self.assertEqual([sound_power.duration_words(s) for s in (0, 29, 60, 3600, 7200 + 60, 59 * 60 + 59)],
                         ["less than a minute", "less than a minute", "1 min", "1 h", "2 h 1 min", "1 h"])
        self.assertEqual([sound_power.battery_icon(B(p, c)) for p, c in ((95, False), (60, True), (30, False),
                                                                         (10, False), (3, True))],
                         ["battery-full-symbolic", "battery-good-charging-symbolic", "battery-low-symbolic",
                          "battery-caution-symbolic", "battery-empty-charging-symbolic"])
        import boswas_fluent as fluent
        testing.app()
        for name in [*sound_power.PROFILE_ICONS.values(), "battery-full-symbolic", "battery-caution-charging-symbolic",
                     "display-brightness-symbolic", "audio-volume-muted-symbolic",
                     "microphone-sensitivity-muted-symbolic", "audio-input-microphone-symbolic"]:
            self.assertTrue(fluent.has_icon(name), name)


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class SoundTest(Case if PYSIDE else unittest.TestCase):
    states = {sb.AUDIO: AUDIO} if PYSIDE else {}

    def test_output_shows_the_state_and_follows_it(self):
        out = self.section(sound_power.OutputSection)
        self.assertEqual([out.devices.itemText(i) for i in range(out.devices.count())],
                         ["Speakers", "Headphones", "Device 42"])
        self.assertEqual(out.devices.currentData(), 40)
        self.assertEqual(out.volume.slider.value(), 62)
        self.assertEqual(out.volume.value.text(), "62 %")
        self.assertFalse(out.volume.mute.isChecked())
        self.assertEqual(out.volume.slider.maximum(), 100)
        self.assertEqual(out.volume.mute.toolTip(), "Mute Output")
        # The volume keys or the quick settings change it: the slider follows without sending anything.
        self.bridge.set_state(sb.AUDIO, output=AD(40, "Speakers", 0.30, True, True))
        self.assertEqual(out.volume.slider.value(), 30)
        self.assertTrue(out.volume.mute.isChecked())
        self.assertEqual(out.volume_card.description(), "Muted")
        self.assertEqual(out.volume.mute.toolTip(), "Unmute Output")
        self.bridge.set_state(sb.AUDIO, output=AD(41, "Headphones", 0.5, False, True))
        self.assertEqual(out.devices.currentData(), 41)
        self.assertEqual(self.bridge.commands(), [])

    def test_volume_slider(self):
        out = self.section(sound_power.OutputSection)
        out.volume.slider.setValue(45)                                   # as a click on the track does
        self.assertEqual(self.last(), {"cmd": "set-volume", "volume": 0.45})
        self.assertEqual(out.volume.value.text(), "45 %")
        self.bridge.set_state(sb.AUDIO, output=AD(40, "Speakers", 0.45, False, True))
        testing.process(250)
        self.assertEqual(out.volume.slider.value(), 45)
        self.assertFalse(out.error.isVisibleTo(self.host))
        testing.key(out.volume.slider, Qt.Key.Key_Right)                 # the keyboard works too
        self.assertEqual(self.last(), {"cmd": "set-volume", "volume": 0.46})
        # The sound server never applies it: the slider goes back to the real volume and says so.
        self.assertTrue(testing.wait_until(lambda: out.error.isVisibleTo(self.host), 3))
        self.assertEqual(out.volume.slider.value(), 45)
        self.assertEqual(out.error.message(), "The output volume could not be changed.")
        self.assertEqual(len(self.bridge.commands("set-volume")), 2)     # going back sends nothing

    def test_louder_than_100_percent(self):
        out = self.section(sound_power.OutputSection)
        out.volume.slider.setValue(140)
        self.assertEqual(out.volume.slider.value(), 100)                 # not without the switch
        testing.click(out.louder)
        self.assertEqual(out.volume.slider.maximum(), 150)
        out.volume.slider.setValue(140)
        self.assertEqual(self.last(), {"cmd": "set-volume", "volume": 1.4})
        self.bridge.set_state(sb.AUDIO, output=AD(40, "Speakers", 1.4, False, True))
        self.assertEqual(out.volume.value.text(), "140 %")
        self.assertFalse(out.louder.isEnabled())                         # cannot be switched off while it is louder
        self.bridge.set_state(sb.AUDIO, output=AD(40, "Speakers", 0.9, False, True))
        self.assertTrue(out.louder.isEnabled())
        testing.click(out.louder)
        self.assertEqual(out.volume.slider.maximum(), 100)
        self.assertEqual(out.volume.slider.value(), 90)
        # A volume above 100 % set elsewhere shows the real range by itself.
        self.bridge.set_state(sb.AUDIO, output=AD(40, "Speakers", 1.2, False, True))
        self.assertTrue(out.louder.isChecked())
        self.assertEqual(out.volume.slider.value(), 120)

    def test_mute_and_device(self):
        out = self.section(sound_power.OutputSection)
        testing.click(out.volume.mute)
        self.assertEqual(self.last(), {"cmd": "set-muted", "muted": True})
        self.assertTrue(testing.wait_until(lambda: out.error.isVisibleTo(self.host), 3))    # never confirmed
        self.assertFalse(out.volume.mute.isChecked())
        self.assertEqual(out.error.message(), "Output could not be muted.")
        out.devices.setCurrentIndex(1)
        out.devices.activated.emit(1)                                    # chosen from the list
        self.assertEqual(self.last(), {"cmd": "set-default-output", "id": 41})
        self.bridge.set_state(sb.AUDIO, output=AD(41, "Headphones", 0.5, False, True))
        self.assertEqual(out.devices.currentData(), 41)
        out.devices.activated.emit(1)                                    # the same one again: nothing to do
        self.assertEqual(len(self.bridge.commands("set-default-output")), 1)

    def test_input(self):
        section = self.section(sound_power.InputSection)
        self.assertEqual(section.devices.currentText(), "Microphone")
        self.assertEqual(section.volume.slider.value(), 80)
        self.assertFalse(section.louder_card.isVisibleTo(self.host))     # no amplification of the microphone
        section.volume.slider.setValue(50)
        self.assertEqual(self.last(), {"cmd": "set-input-volume", "volume": 0.5})
        testing.click(section.volume.mute)
        self.assertEqual(self.last(), {"cmd": "set-input-muted", "muted": True})
        section.devices.activated.emit(1)
        self.assertEqual(self.last(), {"cmd": "set-default-input", "id": 52})

    def test_no_sound_server_and_no_devices(self):
        self.bridge = bridgeclient.FakeBridge()
        out = self.section(sound_power.OutputSection)
        self.assertIn("sound server is not running", out.notice.message())
        self.assertFalse(out.volume.isEnabled())
        self.assertFalse(out.devices.isEnabled())
        self.bridge.set_state(sb.AUDIO, sb.AudioState(available=True))
        self.assertFalse(out.notice.isVisibleTo(self.host))
        self.assertEqual(out.device_card.description(), "No output device was found.")
        self.assertFalse(out.volume.isEnabled())
        self.bridge.set_state(sb.AUDIO, AUDIO)
        self.assertTrue(out.volume.isEnabled())
        self.assertEqual(out.volume.slider.value(), 62)


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class PowerTest(Case if PYSIDE else unittest.TestCase):
    states = {sb.POWER: POWER, sb.BRIGHTNESS: sb.BrightnessState(0.7)} if PYSIDE else {}

    def test_battery(self):
        battery = self.section(sound_power.BatterySection)
        self.assertTrue(battery.isVisibleTo(self.host))
        self.assertEqual((battery.card.title(), battery.card.description()), ("76 %", "On battery · 3 h 25 min left"))
        self.assertEqual(battery.bar.value(), 76)
        self.bridge.set_state(sb.POWER, battery=sb.Battery(12, False, False, 600))
        self.assertEqual(battery.card.title(), "12 %")
        self.assertEqual(battery.bar.property("status"), "error")       # low: drawn as a warning
        self.bridge.set_state(sb.POWER, battery=sb.Battery(13, True, False, 5400), on_ac=True)
        self.assertEqual(battery.card.description(), "Charging · 1 h 30 min until full")
        self.assertIsNone(battery.bar.property("status"))

    def test_sections_hide_themselves_and_their_titles(self):
        """No battery, no power profiles, no backlight: nothing is shown, not even a heading."""
        self.bridge = bridgeclient.FakeBridge()
        groups = []
        for cls in (sound_power.BatterySection, sound_power.PowerModeSection, sound_power.BrightnessSection):
            with mock.patch.object(bridgeclient, "factory", lambda _window: self.bridge):
                group = SectionGroup(cls, self.host)
            self.host.layout().addWidget(group)
            groups.append(group)
        with mock.patch.object(bridgeclient, "for_widget", lambda _widget: self.bridge):
            self.show(self.host, 900, 600)
            for group in groups:
                group.activate()
            testing.process()
            for group in groups:
                self.assertFalse(group.isVisibleTo(self.host), group.section_class.__name__)
                self.assertFalse(group.title.isVisibleTo(self.host))
            self.bridge.set_state(sb.POWER, POWER)
            self.bridge.set_state(sb.BRIGHTNESS, level=0.4)
            for group in groups:
                self.assertTrue(group.isVisibleTo(self.host), group.section_class.__name__)
                self.assertTrue(group.title.isVisibleTo(self.host))
            self.bridge.set_state(sb.POWER, sb.PowerState())             # the battery was taken out
            self.assertFalse(groups[0].isVisibleTo(self.host))
            self.assertFalse(groups[1].isVisibleTo(self.host))
            self.assertTrue(groups[2].isVisibleTo(self.host))

    def test_power_mode(self):
        mode = self.section(sound_power.PowerModeSection)
        self.assertEqual([mode.modes.itemText(i) for i in range(mode.modes.count())],
                         ["Power saver", "Balanced", "Performance"])
        self.assertEqual(mode.modes.currentData(), "balanced")
        self.assertEqual(mode.card.description(), "Good performance and battery life")
        mode.modes.setCurrentIndex(2)
        mode.modes.activated.emit(2)
        self.assertEqual(self.last(), {"cmd": "set-power-profile", "profile": "performance"})
        self.bridge.set_state(sb.POWER, profile="performance")
        testing.process(250)
        self.assertEqual(mode.modes.currentData(), "performance")
        self.assertFalse(mode.error.isVisibleTo(self.host))
        mode.modes.setCurrentIndex(0)
        mode.modes.activated.emit(0)                                     # refused by the daemon
        self.assertTrue(testing.wait_until(lambda: mode.error.isVisibleTo(self.host), 3))
        self.assertEqual(mode.modes.currentData(), "performance")        # back to what is in use
        self.assertEqual(mode.error.message(), "The power mode could not be changed.")
        # Only the profiles the bridge reports are offered.
        self.bridge.set_state(sb.POWER, profiles=("power-saver", "balanced"), profile="balanced")
        self.assertEqual(mode.modes.count(), 2)
        self.bridge.set_state(sb.POWER, profile="power-saver")           # changed in quick settings
        self.assertEqual(mode.modes.currentData(), "power-saver")

    def test_brightness(self):
        section = self.section(sound_power.BrightnessSection)
        self.assertTrue(section.isVisibleTo(self.host))
        self.assertEqual((section.slider.value(), section.value.text()), (70, "70 %"))
        section.slider.setValue(40)
        self.assertEqual(self.last(), {"cmd": "set-brightness", "level": 0.4})
        self.bridge.set_state(sb.BRIGHTNESS, level=0.4)
        testing.process(250)
        self.assertFalse(section.error.isVisibleTo(self.host))
        section.slider.setValue(0)                                       # never fully dark from here
        self.assertEqual(section.slider.value(), sound_power.MIN_BRIGHTNESS)
        self.assertEqual(self.last(), {"cmd": "set-brightness", "level": 0.05})
        self.bridge.set_state(sb.BRIGHTNESS, level=0.9)                  # the brightness keys
        testing.process(250)
        self.assertEqual(section.slider.value(), 90)

    def test_registration(self):
        self.assertEqual([cls.__name__ for cls in settings.sections_for("sound")], ["OutputSection", "InputSection"])
        mine = [cls.__name__ for cls in settings.sections_for("power") if cls.__module__.endswith("sound_power")]
        self.assertEqual(mine, ["BatterySection", "PowerModeSection", "BrightnessSection"])
        self.assertEqual(settings.find("volume")[0], sound_power.OutputSection)
        self.assertEqual(settings.find("microphone")[0], sound_power.InputSection)
        self.assertEqual(settings.find("brightness")[0], sound_power.BrightnessSection)


if __name__ == "__main__":
    unittest.main()
