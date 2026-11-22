"""Personalization > Colours: accent colour and transparency (ui/settings/colors.py), offscreen."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)
import test_settings_display as shared  # noqa: E402
import test_settings_taskbar as base  # noqa: E402

from boswas_control_center import compositorconfig as cc  # noqa: E402

if shared.HAVE_QT:
    import boswas_fluent as fluent
    from boswas_fluent import testing
    from PySide6.QtCore import Qt

    from boswas_control_center.ui.settings import colors

    class AccentTest(base.DesktopCase):
        def test_every_hue_is_offered_in_its_own_colour(self):
            section = self.open(colors.AccentSection(config=self.settings))
            self.assertEqual(list(section.swatches), list(cc.ACCENTS))
            self.assertTrue(section.swatches["iris"].isChecked())
            self.assertEqual(section.swatches["iris"].accessibleName(), "Iris (default)")
            shown = {hue: swatch.colour() for hue, swatch in section.swatches.items()}
            self.assertEqual(len(set(shown.values())), len(cc.ACCENTS))
            self.assertEqual(shown["iris"], fluent.theme().color("accent"))
            image = section.swatches["coral"].grab().toImage()
            centre = image.pixelColor(image.width() // 2, image.height() // 2 + 8).name().upper()
            self.assertEqual(centre, shown["coral"], "the swatch is painted in its hue")

        def test_choosing_an_accent_writes_it_where_presets_leave_it(self):
            section = self.open(colors.AccentSection(config=self.settings))
            testing.click(section.swatches["coral"])
            shared.settle(section)
            self.assertEqual(self.get("appearance", "accent"), "coral")
            self.assertEqual(self.written(), {"appearance": {"accent": "coral"}}, "the user's compositor.toml")
            self.assertFalse(self.user.with_name("appearance.toml").exists(), "not the file a preset replaces")
            self.assertTrue(section.swatches["coral"].isChecked())
            self.assertEqual(section.current.text(), "Coral")
            # From the keyboard: the swatches are buttons in the Tab order.
            section.swatches["mint"].setFocus(Qt.FocusReason.TabFocusReason)
            testing.key(section.swatches["mint"], Qt.Key.Key_Space)
            shared.settle(section)
            self.assertEqual(self.get("appearance", "accent"), "mint")
            self.pictures(section, "accent")

    class TransparencyTest(base.DesktopCase):
        def test_transparency_effects(self):
            section = self.open(colors.TransparencySection(config=self.settings))
            self.assertTrue(section.effects.isChecked())
            self.assertIn("visual-effects", colors.TransparencySection.also)
            testing.click(section.effects)
            shared.settle(section)
            self.assertEqual(self.written(), {"appearance": {"transparency": False}})


if __name__ == "__main__":
    unittest.main()
