"""The settings framework: the section registry, how pages show sections, search, and a second start.

The registry is tested with section classes made here, put in place of the
real ones, so these tests do not depend on which settings modules exist.

Run: QT_QPA_PLATFORM=offscreen python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402

try:
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    PYSIDE = True
except ImportError:
    PYSIDE = False

if PYSIDE:
    import boswas_fluent as fluent
    from boswas_control_center import catalog
    from boswas_control_center.ui import settings
    from boswas_control_center.ui.main_window import MainWindow
    from boswas_control_center.ui.tiles import SectionGroup

    APP = QApplication.instance() or QApplication(["test"])
    bridgeclient = fx.use_fake_bridge()

    class Recorder(QWidget):
        """A section that records what the page did with it."""

        page = "display"
        title = "Night light"
        order = 20
        keywords = ("night", "blue light", "warm")
        log: list[str] = []

        def __init__(self, parent=None):
            super().__init__(parent)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            self.toggle = fluent.ToggleSwitch()
            self.card = fluent.SettingsCard(self.title, "A row of the section", None, self.toggle, self)
            layout.addWidget(self.card)
            self.active = False
            type(self).log.append(f"{type(self).__name__}:init")

        def activate(self):
            self.active = True
            type(self).log.append(f"{type(self).__name__}:activate")

        def deactivate(self):
            self.active = False
            type(self).log.append(f"{type(self).__name__}:deactivate")

    class Screens(Recorder):
        title = "Screens"
        order = 10
        keywords = ("monitor", "resolution")

    class Tall(Recorder):
        """Many rows, so that the page has to scroll to reach the sections after it."""

        title = "Arrangement"
        order = 15
        keywords = ()

        def __init__(self, parent=None):
            super().__init__(parent)
            for index in range(14):
                self.layout().addWidget(fluent.SettingsCard(f"Row {index}", "", None, None, self))

    class Sound(Recorder):
        page = "sound"
        title = "Output"
        order = 1
        keywords = ("speakers", "volume")

    class Broken(Recorder):
        page = "sound"
        title = "Broken"
        order = 5
        keywords = ()

        def __init__(self, parent=None):
            raise RuntimeError("no such device")

    class FailsToLoad(Recorder):
        page = "sound"
        title = "Input"
        order = 9
        keywords = ("microphone",)

        def activate(self):
            raise OSError("service gone")


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.saved = settings._sections
        self.addCleanup(self.restore)

    def restore(self):
        settings._sections = self.saved

    def test_real_modules_load(self):
        found = settings.load(force=True)
        for cls in found:
            self.assertIsNone(settings.valid_section(cls), cls)
        self.assertEqual(found, settings.load())
        # A module that does not exist yet is a note, never an exception.
        for name, reason in settings.problems().items():
            self.assertIsInstance(reason, str, name)
        self.assertEqual(settings.MODULES, ("connectivity", "sound_power", "accounts", "system", "display", "input",
                                            "personalize", "maintenance", "cleanup", "peripherals", "netadmin",
                                            "fonts", "apps", "permissions", "updates", "colors", "taskbar",
                                            "multitasking", "notifications", "accessibility", "clipboard"))
        self.assertEqual(settings.problems(), {})
        # Every page of the catalog that is not a category and has no content of its own has a section.
        from boswas_control_center.ui.main_window import PAGE_CLASSES
        for page in catalog.PAGES:
            if not page.is_category and page.key not in PAGE_CLASSES and not page.programs:
                self.assertTrue(settings.sections_for(page.key), page.key)
        # A section shown on a second page (also) is found on both.
        transparency = next(cls for cls in found if cls.__name__ == "TransparencySection")
        self.assertIn(transparency, settings.sections_for("colours"))
        self.assertIn(transparency, settings.sections_for("visual-effects"))
        self.assertIn(("Transparency effects", "visual-effects", "transparency-effects", transparency.keywords),
                      settings.search_entries())
        # One animation switch, where Windows has it: Accessibility > Visual effects.
        motion = next(cls for cls in found if cls.__name__ == "MotionSection")
        self.assertNotIn(motion, settings.sections_for("colours"))
        self.assertIn(("Motion", "visual-effects", "motion", motion.keywords), settings.search_entries())
        with self.assertRaises(ImportError):
            settings._import("os")

    def test_sections_for_sorts_by_order(self):
        settings._sections = [Recorder, Sound, Tall, Screens]
        self.assertEqual(settings.sections_for("display"), [Screens, Tall, Recorder])
        self.assertEqual(settings.sections_for("sound"), [Sound])
        self.assertEqual(settings.sections_for("about"), [])
        self.assertEqual(settings.sections_for("no such page"), [])

    def test_keywords_and_ids(self):
        settings._sections = [Screens, Recorder, Sound]
        self.assertEqual(settings.all_keywords(), {
            "display": ("Screens", "monitor", "resolution", "Night light", "night", "blue light", "warm"),
            "sound": ("Output", "speakers", "volume")})
        self.assertEqual(settings.section_id(Recorder), "night-light")
        self.assertEqual(settings.section_id(Screens), "screens")

    def test_find(self):
        settings._sections = [Sound, Screens, Recorder]
        self.assertEqual(settings.find("night"), [Recorder])
        self.assertEqual(settings.find("NIGHT LIGHT"), [Recorder])
        self.assertEqual(settings.find("night-light"), [Recorder])
        self.assertEqual(settings.find("blue"), [Recorder])
        self.assertEqual(settings.find("volume"), [Sound])
        self.assertEqual(settings.find("o"), [Sound, Screens])                 # in a title before in a keyword
        self.assertEqual(settings.find("n"), [Screens, Recorder])              # then the order on the page
        self.assertEqual(settings.find("output"), [Sound])
        self.assertEqual(settings.find("o", "sound"), [Sound])
        self.assertEqual(settings.find(""), [])
        self.assertEqual(settings.find("   "), [])
        self.assertEqual(settings.find("zebra"), [])
        self.assertEqual(settings.find("night volume"), [])                    # every word must match one section

    def test_contract_check(self):
        class NoPage(QWidget):
            page, title, order, keywords = "nowhere", "X", 1, ()
            def activate(self): pass
            def deactivate(self): pass

        class NoTitle(NoPage):
            page, title = "display", " "

        class BadOrder(NoPage):
            page, order = "display", "first"

        class BadKeywords(NoPage):
            page, keywords = "display", "night"

        class NoDeactivate(QWidget):
            page, title, order, keywords = "display", "X", 1, ()
            def activate(self): pass

        self.assertIsNone(settings.valid_section(Recorder))
        self.assertIn("not a QWidget", settings.valid_section(object))
        self.assertIn("not a QWidget", settings.valid_section("Recorder"))
        self.assertIn("not a page", settings.valid_section(NoPage))
        self.assertIn("title", settings.valid_section(NoTitle))
        self.assertIn("order", settings.valid_section(BadOrder))
        self.assertIn("keywords", settings.valid_section(BadKeywords))
        self.assertIn("deactivate", settings.valid_section(NoDeactivate))


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class PageTest(unittest.TestCase):
    sections: list = []

    def setUp(self):
        self.errors: list[str] = []
        self._excepthook = sys.excepthook
        sys.excepthook = lambda *exc: self.errors.append("".join(traceback.format_exception(*exc)))
        self.tmp = tempfile.TemporaryDirectory()
        self.rig = fx.Rig(Path(self.tmp.name))
        self.addCleanup(fx.use_rig_for_settings(self.rig))
        self.saved = settings._sections
        settings._sections = [Recorder, Screens, Tall, Sound, Broken, FailsToLoad] if not self.sections \
            else list(self.sections)
        Recorder.log.clear()
        self.windows: list = []

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        settings._sections = self.saved
        sys.excepthook = self._excepthook
        self.tmp.cleanup()
        if self.errors:
            self.fail("exceptions in Qt callbacks:\n" + "\n".join(self.errors))

    def window(self, **options) -> "MainWindow":
        window = MainWindow(self.rig.backend, **options)
        self.windows.append(window)
        window.resize(1100, 700)
        window.show()
        self.settle(window)
        return window

    @staticmethod
    def settle(window) -> None:
        for _ in range(3):
            window.runner.wait(15)
            QCoreApplication.processEvents()

    def show(self, window, key, section=None):
        window.show_page(key, section)
        self.settle(window)
        return window.page(key)

    # --- sections on a page ---
    def test_page_shows_its_sections_in_order_with_titles(self):
        w = self.window()
        self.assertEqual(Recorder.log, [])                             # nothing is built before its page is shown
        page = self.show(w, "display")
        self.assertEqual([type(s) for s in page.sections], [Screens, Tall, Recorder])
        self.assertEqual([g.title.text() for g in page.section_groups], ["Screens", "Arrangement", "Night light"])
        for group in page.section_groups:
            self.assertTrue(group.title.isVisibleTo(page))
            self.assertTrue(group.section.isVisibleTo(page))
            self.assertEqual(group.accessibleName(), group.section_class.title)
        self.assertIs(page.section("night-light"), page.section(Recorder))
        self.assertIs(page.section("Night light"), page.section("recorder"))
        self.assertIsNone(page.section("volume"))
        self.assertIsNone(page.programs)

    def test_activate_on_show_deactivate_on_leave_and_close(self):
        w = self.window()
        page = self.show(w, "display")
        self.assertEqual(Recorder.log, ["Screens:init", "Tall:init", "Recorder:init",
                                        "Screens:activate", "Tall:activate", "Recorder:activate"])
        self.assertTrue(all(s.active for s in page.sections))
        Recorder.log.clear()
        self.show(w, "about")
        self.assertEqual(Recorder.log, ["Screens:deactivate", "Tall:deactivate", "Recorder:deactivate"])
        self.assertFalse(any(s.active for s in page.sections))
        Recorder.log.clear()
        self.show(w, "display")                                          # built once, activated again
        self.assertEqual(Recorder.log, ["Screens:activate", "Tall:activate", "Recorder:activate"])
        Recorder.log.clear()
        self.show(w, "display")                                          # already current: not activated twice
        self.assertEqual(Recorder.log, [])
        w.refresh_current()                                              # F5: stop and load again
        self.settle(w)
        self.assertEqual(Recorder.log, ["Screens:deactivate", "Tall:deactivate", "Recorder:deactivate",
                                        "Screens:activate", "Tall:activate", "Recorder:activate"])
        Recorder.log.clear()
        w.close()
        self.assertEqual(Recorder.log, ["Screens:deactivate", "Tall:deactivate", "Recorder:deactivate"])
        Recorder.log.clear()
        w.shutdown()                                                     # a second shutdown does nothing
        self.assertEqual(Recorder.log, [])

    def test_a_broken_section_does_not_take_the_page_down(self):
        w = self.window()
        page = self.show(w, "sound")
        self.assertEqual([g.section_class for g in page.section_groups], [Sound, Broken, FailsToLoad])
        ok, broken, failing = page.section_groups
        self.assertTrue(ok.section.active)
        self.assertIsNone(broken.section)
        self.assertTrue(broken.problem.isVisibleTo(page))
        self.assertIn("could not be shown", broken.problem.message())
        self.assertIn("no such device", broken.problem.message())
        self.assertTrue(failing.problem.isVisibleTo(page))
        self.assertIn("could not be loaded", failing.problem.message())
        self.assertFalse(failing.section.isVisibleTo(page))
        self.show(w, "about")
        self.show(w, "sound")                                            # the failed one is not tried again
        self.assertEqual(Recorder.log.count("FailsToLoad:activate"), 0)
        self.assertEqual(Recorder.log.count("Sound:activate"), 2)

    def test_the_bridge_is_stopped_with_the_window(self):
        w = self.window()
        bridge = bridgeclient.for_widget(self.show(w, "sound").sections[0])
        self.assertIsInstance(bridge, bridgeclient.FakeBridge)
        self.assertIs(bridgeclient.for_widget(w), bridge)                # one per window
        bridge.start()
        w.close()
        self.assertEqual(bridge.stopped, 1)
        other = self.window()
        self.assertIsNot(bridgeclient.for_widget(other), bridge)

    # --- pages without content ---
    def test_a_page_without_sections_or_content_is_left_out(self):
        w = self.window()
        for key in ("bluetooth", "users", "mouse", "power", "wifi", "date-time"):
            self.assertFalse(w.page_available(key), key)
        for key in ("network", "time-language", "accessibility"):        # no page of theirs has anything
            self.assertFalse(w.page_available(key), key)
            self.assertFalse(w.navigation.is_item_visible(key), key)
        for key in ("display", "sound", "storage", "windows", "about", "themes", "system", "devices", "home"):
            self.assertTrue(w.page_available(key), key)
        for key in ("system", "devices", "personalization", "home"):
            self.assertTrue(w.navigation.is_item_visible(key), key)
        devices = self.show(w, "devices")
        self.assertEqual(devices.subpages.visible_keys(), ["cameras", "hardware"])  # a program row; content
        w.show_page("bluetooth")
        self.settle(w)
        self.assertEqual(w.current_page(), "devices")
        self.assertIn("Bluetooth settings are not available", w.message.text.text())
        self.assertNotIn("bluetooth", w.pages)                           # never built: no empty heading anywhere

    def test_a_page_with_content_of_its_own_shows_no_empty_group(self):
        w = self.window()
        for key in ("security", "system", "privacy", "hardware", "about"):
            page = self.show(w, key)
            self.assertEqual(page.section_groups, [], key)
            self.assertIsNone(page.programs, key)
        storage = self.show(w, "storage")
        self.assertTrue(storage.programs.isVisibleTo(storage))
        self.assertEqual(storage.programs.title.text(), "Tools")
        self.rig.commands.installed.discard(fx.E["boswas-files"])
        w.refresh_current()
        self.settle(w)
        self.assertTrue(storage.programs.isVisibleTo(storage))           # shown as not installed, with its title
        self.assertFalse(storage.programs.tile("files").isEnabled())

    # --- search ---
    def test_search_finds_pages_by_section_titles_and_keywords(self):
        w = self.window()
        nav = w.navigation
        for query, title, page in (("night", "Night light", "display"), ("blue light", "Night light", "display"),
                                   ("speakers", "Sound", "sound"), ("Screens", "Screens", "display"),
                                   ("microphone", "Input", "sound")):
            nav.search_box.setText(query)
            found = [(row.entry.title, row.entry.page) for row in w.results.rows]
            self.assertIn((title, page), found, query)
            self.assertIs(w.stack.currentWidget(), w.results)
        nav.search_box.setText("")
        self.assertIs(w.stack.currentWidget(), w.holders["home"])
        self.assertTrue(all(not nav.item(key).isHidden() for key in nav.keys() if w.page_available(key)))

    def test_enter_in_the_search_box_jumps_to_the_section(self):
        w = self.window()
        nav = w.navigation
        nav.search_box.setText("night")
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(nav.search_box, QKeyEvent(kind, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))
        self.settle(w)
        self.assertEqual(w.current_page(), "display")
        page = w.page("display")
        fluent.testing.wait_until(lambda: page.scroll.verticalScrollBar().value() > 0, 2)
        group = page.section_group(Recorder)
        bar = page.scroll.verticalScrollBar()
        top = group.mapTo(page.scroll.viewport(), group.rect().topLeft()).y()
        self.assertGreater(bar.value(), 300)                             # below the tall section
        # Its title is at the top of the page, or the page is scrolled as far as it goes.
        self.assertTrue(abs(top) < 40 or bar.value() == bar.maximum(), (top, bar.value(), bar.maximum()))
        self.assertGreaterEqual(top, -8)
        self.assertLessEqual(top + group.height(), page.scroll.viewport().height() + 40)
        self.assertTrue(group.section.active)
        # A section in the middle of a long page is brought to the top.
        w.show_page("display", "arrangement")
        fluent.testing.process(300)
        middle = page.section_group(Tall)
        self.assertLess(abs(middle.mapTo(page.scroll.viewport(), middle.rect().topLeft()).y()), 40)

    def test_search_for_something_unknown_changes_nothing(self):
        w = self.window()
        w._search_submitted("zebra crossing")
        self.settle(w)
        self.assertEqual(w.current_page(), "home")

    # --- another start of the program ---
    def test_second_start_opens_the_page_and_section(self):
        w = self.window()
        w.handle_arguments(["--page", "sound"])
        self.settle(w)
        self.assertEqual(w.current_page(), "sound")
        w.handle_arguments(["--page", "display", "--section", "night-light"])
        self.settle(w)
        self.assertEqual(w.current_page(), "display")
        page = w.page("display")
        fluent.testing.wait_until(lambda: page.scroll.verticalScrollBar().value() > 300, 2)
        self.assertGreater(page.scroll.verticalScrollBar().value(), 300)
        for arguments in ([], ["--page", "registry"], ["--frobnicate"], ["--page"], ["--page", "display",
                                                                                      "--section", "no-such"]):
            w.handle_arguments(arguments)
            self.settle(w)
            self.assertEqual(w.current_page(), "display", arguments)
        self.assertEqual(self.errors, [])

    def test_initial_page_and_section(self):
        w = self.window(initial_page="display", initial_section="night-light")
        self.assertEqual(w.current_page(), "display")
        page = w.page("display")
        fluent.testing.wait_until(lambda: page.scroll.verticalScrollBar().value() > 300, 2)
        self.assertGreater(page.scroll.verticalScrollBar().value(), 300)


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class SectionGroupTest(unittest.TestCase):
    def test_group_alone(self):
        Recorder.log.clear()
        group = SectionGroup(Screens)
        self.assertEqual(group.section_id, "screens")
        group.deactivate()                                               # not active: nothing happens
        group.activate()
        group.activate()
        group.deactivate()
        self.assertEqual(Recorder.log, ["Screens:init", "Screens:activate", "Screens:deactivate"])
        group.deleteLater()


class SingleInstanceTest(unittest.TestCase):
    """The entry point runs through the toolkit's run(): one instance, the arguments handed over."""

    def test_main_runs_through_fluent_run(self):
        from unittest import mock

        from boswas_control_center import app
        calls = []

        def fake_run(app_id, factory, **options):
            calls.append((app_id, options))
            return 0

        fake = mock.MagicMock()
        fake.run = fake_run
        with mock.patch.dict(sys.modules, {"boswas_fluent": fake}), \
                mock.patch.object(app, "running_as_root", return_value=False):
            self.assertEqual(app.main(["boswas-control-center", "--page", "display"]), 0)
        self.assertEqual(calls[0][0], "com.boswas.ControlCenter")
        self.assertEqual(calls[0][1]["argv"], ["boswas-control-center", "--page", "display"])
        self.assertEqual(calls[0][1]["display_name"], "Boswas Control Center")
        self.assertNotIn("single_instance", calls[0][1])                 # the toolkit's default: one instance

    @unittest.skipUnless(PYSIDE, "PySide6 is not installed")
    def test_a_second_start_reaches_handle_arguments(self):
        """The toolkit's SingleInstance, as run() uses it: a second process hands its arguments to the first."""
        import subprocess
        second = ("import sys\n"
                  "sys.path[:0] = sys.argv[1].split(':')\n"
                  "from PySide6.QtCore import QCoreApplication\n"
                  "import boswas_fluent as fluent\n"
                  "app = QCoreApplication([])\n"
                  "instance = fluent.SingleInstance('com.boswas.ControlCenter', directory=sys.argv[2])\n"
                  "sys.exit(0 if instance.start(sys.argv[3:]) is False else 3)\n")
        paths = ":".join(str(path) for path in (fx.PACKAGE.parents[1] / "desktop" / "ui" / "qt", fx.PACKAGE))
        with tempfile.TemporaryDirectory(prefix="cc") as tmp:
            os.chmod(tmp, 0o700)
            first = fluent.SingleInstance("com.boswas.ControlCenter", directory=tmp)
            received = []
            first.message_received.connect(received.append)
            self.assertTrue(first.start([]))
            try:
                child = subprocess.Popen([sys.executable, "-B", "-c", second, paths, tmp, "--page", "display",
                                          "--section", "night-light"], stdin=subprocess.DEVNULL)
                # The second start waits for the answer of the first, which has to serve it meanwhile.
                self.assertTrue(fluent.testing.wait_until(lambda: child.poll() is not None, 30))
                self.assertEqual(child.returncode, 0)                    # told to exit: another instance took over
                self.assertEqual(received, [["--page", "display", "--section", "night-light"]])
                from boswas_control_center import app
                self.assertEqual(app.requested_page(received[0]), ("display", "night-light"))
            finally:
                first.close()


if __name__ == "__main__":
    unittest.main()
