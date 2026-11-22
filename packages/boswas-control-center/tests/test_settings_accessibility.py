"""The Accessibility sections (ui/settings/accessibility.py), offscreen, against the real settings files."""

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

    from boswas_control_center.ui.settings import accessibility

    class Answering(shared.FakeCompositor):
        """The model compositor, answering ``version`` like the real one."""

        def __init__(self, filters: bool) -> None:
            super().__init__(shared.HEADLESS)
            self.filters = filters

        def request(self, command: str, **_arguments) -> object:
            self._check()
            if command != "version":
                raise cc.ControlError("failed", "unknown command")
            return {"name": "boswas-compositor", "trusted": False, "locked": False, "color_filters": self.filters}

    class TextSizeTest(base.DesktopCase):
        def test_the_slider_previews_and_apply_saves(self):
            section = self.open(accessibility.TextSizeSection(config=self.settings))
            self.assertEqual(section.slider.value(), 100)
            self.assertFalse(section.apply_button.isEnabled(), "nothing to apply yet")
            body = section.preview.font().pixelSize()
            section.slider.setValue(150)
            self.assertEqual(section.percent.text(), "150 %")
            self.assertEqual(section.preview.font().pixelSize(), round(body * 1.5))
            self.assertEqual(self.get("accessibility", "text_scale"), 100, "the slider alone changes nothing")
            self.assertTrue(section.apply_button.isEnabled())
            testing.click(section.apply_button)
            shared.settle(section)
            self.assertEqual(self.written(), {"accessibility": {"text_scale": 150}})
            self.assertFalse(section.apply_button.isEnabled())
            section.slider.setValue(400)
            self.assertEqual(section.slider.value(), 225, "225 % at most")
            self.pictures(section, "text-size")

    class VisualEffectsTest(base.DesktopCase):
        def test_scroll_bars_and_notification_time(self):
            section = self.open(accessibility.VisualEffectsSection(config=self.settings))
            self.assertFalse(section.scrollbars.isChecked())
            self.assertEqual(section.toasts.currentText(), "5 seconds")
            testing.click(section.scrollbars)
            shared.settle(section)
            shared.choose(section.toasts, 60)
            shared.settle(section)
            self.assertEqual(self.written(), {"accessibility": {"always_show_scrollbars": True},
                                              "notifications": {"toast_seconds": 60}})
            self.assertEqual(section.toasts.currentText(), "1 minute")
            self.pictures(section, "visual-effects")

    class ColourFiltersTest(base.DesktopCase):
        def open_filters(self, available: bool):
            self.compositor = Answering(available)
            self.settings = shared.make_settings(self.compositor)
            return self.open(accessibility.ColourFiltersSection(config=self.settings))

        def test_on_a_renderer_that_draws_them(self):
            section = self.open_filters(True)
            self.assertTrue(section.unavailable.isHidden())
            self.assertEqual(section.filter.currentText(), "Greyscale")
            testing.click(section.enabled)
            shared.settle(section)
            shared.choose(section.filter, "deuteranopia")
            shared.settle(section)
            testing.click(section.shortcut)
            shared.settle(section)
            self.assertEqual(self.written(), {"color_filter": {"enabled": True, "filter": "deuteranopia",
                                                               "shortcut": True}})
            self.assertEqual(section.filter.currentText(), "Red-green (green weak, deuteranopia)")
            self.pictures(section, "colour-filters")

        def test_where_the_screen_is_drawn_in_software_it_says_so(self):
            section = self.open_filters(False)
            self.assertFalse(section.unavailable.isHidden())
            self.assertIn("not available", section.unavailable.message())
            self.assertFalse(section.enabled.isEnabled() or section.filter.isEnabled() or section.shortcut.isEnabled())
            self.pictures(section, "colour-filters-unavailable")

        def test_without_a_desktop_the_settings_wait_for_the_next_sign_in(self):
            section = self.open_filters(True)
            self.compositor.running = False
            section.refresh()
            shared.settle(section)
            self.assertTrue(section.unavailable.isHidden() and section.enabled.isEnabled())
            testing.click(section.enabled)
            shared.settle(section)
            self.assertTrue(self.get("color_filter", "enabled"))
            self.assertIn("next time you sign in", section.notice.message())

    class MagnifierTest(base.DesktopCase):
        def test_on_zoom_and_increment(self):
            section = self.open(accessibility.MagnifierSection(config=self.settings))
            self.assertFalse(section.enabled.isChecked())
            self.assertEqual((section.zoom.int_value(), section.step.currentText()), (200, "100 %"))
            testing.click(section.enabled)
            shared.settle(section)
            shared.choose(section.step, 50)
            shared.settle(section)
            self.assertEqual(section.zoom.singleStep(), 50, "the zoom box steps by the increment")
            section.zoom.setValue(350)
            shared.settle(section)
            self.assertEqual(self.written(), {"magnifier": {"enabled": True, "step": 50, "zoom": 350}})
            self.pictures(section, "magnifier")

    class KeyboardTest(base.DesktopCase):
        def test_sticky_and_filter_keys(self):
            section = self.open(accessibility.KeyboardAccessSection(config=self.settings), 640)
            self.assertFalse(section.sticky.isChecked() or section.filter.isChecked())
            self.assertFalse(section.lock.isEnabled() or section.slow.isEnabled(), "off: their details are too")
            testing.click(section.sticky)
            shared.settle(section)
            testing.click(section.filter)
            shared.settle(section)
            section.filter_row.set_expanded(True, animate=False)
            testing.process(20)
            shared.choose(section.slow, 500)
            shared.settle(section)
            shared.choose(section.bounce, 0)
            shared.settle(section)
            self.assertEqual(self.written(), {"keyboard": {"sticky_keys": True, "filter_keys": True,
                                                           "slow_keys_ms": 500, "bounce_keys_ms": 0}})
            self.assertTrue(section.lock.isEnabled() and section.lock.isChecked())
            self.pictures(section, "keyboard")

        def test_a_time_from_a_hand_written_file_is_shown(self):
            self.settings.apply({("keyboard", "slow_keys_ms"): 750})
            section = self.open(accessibility.KeyboardAccessSection(config=self.settings))
            self.assertEqual(section.slow.currentText(), "0.75 seconds")
            self.assertEqual(accessibility.seconds_text(1000), "1 second")
            self.assertEqual(accessibility.seconds_text(0), "Off")

    class FontTest(base.DesktopCase):
        def test_the_preview_starts_from_the_normal_size_at_any_text_size(self):
            manager = fluent.manager()
            base_size = fluent.theme().type("body").size
            self.assertEqual(manager.theme.text_scale, 100)
            section = self.open(accessibility.TextSizeSection(config=self.settings))
            section.slider.setValue(200)
            self.assertEqual(section.preview.font().pixelSize(), base_size * 2)


if __name__ == "__main__":
    unittest.main()
