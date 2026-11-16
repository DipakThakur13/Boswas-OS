"""Widget tests of Control Center (Qt offscreen platform, real Backend on the fakes).

Every backend call still runs on the Runner's thread pool; the tests wait
for delivery with Runner.wait(). Confirmation dialogs are answered through
the window's replaceable ``ask`` hook. The widget tests skip themselves when
PySide6 is not installed; no Qt name is used outside the guard.

Run: QT_QPA_PLATFORM=offscreen python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402

try:
    from PySide6.QtCore import QCoreApplication, QEvent, Qt, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication, QKeyEvent
    from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QApplication, QLabel, QLineEdit
    PYSIDE = True
except ImportError:                       # PySide6 is not a build dependency: skip the widget tests
    PYSIDE = False

if PYSIDE:
    from boswas_control_center import catalog
    from boswas_control_center import viewmodel as vm
    from boswas_control_center.commands import EXECUTABLES as E
    from boswas_control_center.ui.main_window import MainWindow
    from boswas_control_center.ui.tiles import ProgramTile

    fx.use_fake_bridge()                  # no test starts the real services bridge
    # The pages with content of their own and those made of this package's own sections; Display and
    # Devices appear with the compositor sections (ui/settings/display.py, input.py).
    ALWAYS_PAGES = ("personalization", "network", "bluetooth", "storage", "applications", "windows", "android",
                    "security", "updates",
                    "privacy", "hardware", "system", "about")

    APP = QApplication.instance() or QApplication(["test"])

    def _quiet_offscreen(_mode, _context, message):
        # The offscreen platform cannot raise or resize windows; a missing branding file is expected here.
        if "This plugin does not support" not in message and "boswas-os-mark.svg" not in message:
            sys.stderr.write(message + "\n")

    qInstallMessageHandler(_quiet_offscreen)

    def key_click(widget, key) -> None:
        """Press and release a key on a widget (python3-pyside6 in Debian has no QtTest)."""
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(widget, QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier))
        QCoreApplication.processEvents()


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class UiTestCase(unittest.TestCase):
    live = False

    def setUp(self):
        self.errors: list[str] = []
        self._excepthook = sys.excepthook
        sys.excepthook = lambda *exc: self.errors.append("".join(traceback.format_exception(*exc)))
        self.tmp = tempfile.TemporaryDirectory()
        self.rig = fx.Rig(Path(self.tmp.name), live=self.live)
        self.windows: list = []
        self.questions: list[tuple[str, str, str]] = []
        self.answer = True

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        sys.excepthook = self._excepthook
        self.tmp.cleanup()
        if self.errors:
            self.fail("exceptions in Qt callbacks:\n" + "\n".join(self.errors))

    def window(self, page: str | None = None) -> "MainWindow":
        window = MainWindow(self.rig.backend, initial_page=page)
        window.ask = self._ask
        self.windows.append(window)
        self.settle(window)
        return window

    def _ask(self, title: str, text: str, confirm: str) -> bool:
        self.questions.append((title, text, confirm))
        return self.answer

    @staticmethod
    def settle(window) -> None:
        for _ in range(3):
            if not window.runner.wait(15):
                raise AssertionError("backend calls did not finish")
            QCoreApplication.processEvents()

    def show(self, window, key: str):
        window.show_page(key)
        self.settle(window)
        return window.page(key)

    @staticmethod
    def sidebar_keys(window, visible_only: bool = True) -> list[str]:
        return [window.sidebar.item(i).data(Qt.ItemDataRole.UserRole) for i in range(window.sidebar.count())
                if not (visible_only and window.sidebar.item(i).isHidden())]


class NavigationTest(UiTestCase):
    def test_sidebar_order_and_default_page(self):
        w = self.window()
        self.assertEqual(self.sidebar_keys(w, visible_only=False), list(catalog.PAGE_KEYS))
        self.assertEqual([w.sidebar.item(i).text() for i in range(4)],
                         ["Personalization", "Network", "Bluetooth", "Display"])
        self.assertEqual(w.current_page(), "personalization")
        self.assertEqual(w.windowTitle(), "Personalization — Boswas Control Center")

    def test_every_page_builds(self):
        w = self.window()
        shown = [key for key in catalog.PAGE_KEYS if key != "install" and w.page_available(key)]
        for key in shown:
            page = self.show(w, key)
            self.assertEqual(w.current_page(), key)
            self.assertIs(w.stack.currentWidget(), w.holders[key])
            self.assertEqual(page.heading.text(), catalog.BY_KEY[key].heading)
        self.assertEqual(set(w.pages), set(shown))
        # The pages with content of their own, and those of this engineer's sections, are always there.
        for key in ALWAYS_PAGES:
            self.assertIn(key, shown)

    def test_pages_are_built_lazily(self):
        w = self.window()
        self.assertEqual(set(w.pages), {"personalization"})
        self.assertEqual(self.rig.commands.ran_program("boswas"), [])          # security not read yet
        self.show(w, "security")
        self.assertEqual(len(self.rig.commands.ran_program("boswas")), 1)

    def test_page_option(self):
        for key in ("security", "applications", "updates", "about"):
            w = self.window(page=key)
            self.assertEqual(w.current_page(), key, key)

    def test_keyboard_navigation(self):
        w = self.window()
        w.sidebar.setFocus()
        key_click(w.sidebar, Qt.Key.Key_Down)
        self.settle(w)
        following = [key for key in catalog.PAGE_KEYS[1:] if w.page_available(key)][0]
        self.assertEqual(w.current_page(), following)
        key_click(w.sidebar, Qt.Key.Key_End)
        self.settle(w)
        self.assertEqual(w.current_page(), "about")          # Install is hidden outside a live session

    def test_refresh_reloads_the_current_page(self):
        w = self.window(page="security")
        before = len(self.rig.commands.ran_program("boswas"))
        w.refresh_current()
        self.settle(w)
        self.assertEqual(len(self.rig.commands.ran_program("boswas")), before + 1)
        w.page("security").refresh_button.click()
        self.settle(w)
        self.assertEqual(len(self.rig.commands.ran_program("boswas")), before + 2)

    def test_find_shortcut_focuses_the_search(self):
        w = self.window()
        w.focus_search()
        self.settle(w)
        self.assertEqual(w.current_page(), "applications")
        self.assertIs(QApplication.focusWidget() or w.page("applications").search, w.page("applications").search)


class LiveSessionTest(UiTestCase):
    def test_install_hidden_when_not_live(self):
        w = self.window(page="install")
        self.assertNotIn("install", self.sidebar_keys(w))
        self.assertTrue(w.header.live_badge.isHidden())
        self.assertNotEqual(w.current_page(), "install")
        self.assertIn("only in a live session", w.message.text.text())
        w.show_page("install")
        self.assertNotEqual(w.current_page(), "install")
        w.restart_to_install()                               # no effect outside a live session
        self.settle(w)
        self.assertEqual(self.questions, [])
        self.assertEqual(self.rig.commands.ran_program("systemctl"), [])


class LiveTest(UiTestCase):
    live = True

    def test_install_shown_when_live(self):
        w = self.window(page="install")
        self.assertIn("install", self.sidebar_keys(w))
        self.assertFalse(w.header.live_badge.isHidden())
        self.assertEqual(w.current_page(), "install")
        page = w.page("install")
        self.assertEqual(page.restart_button.text(), "Restart to Install…")
        texts = [label.text() for label in page.findChildren(QLabel)]
        self.assertTrue(any("lost when you shut down" in t for t in texts))
        self.assertTrue(any("“Install Boswas OS”" in t for t in texts))

    def test_restart_asks_for_confirmation(self):
        w = self.window(page="install")
        self.answer = False
        w.page("install").restart_button.click()
        self.settle(w)
        self.assertEqual(len(self.questions), 1)
        self.assertEqual(self.questions[0][2], "Restart Now")
        self.assertIn("Install Boswas OS", self.questions[0][1])
        self.assertEqual(self.rig.commands.ran_program("systemctl"), [])
        self.answer = True
        w.page("install").restart_button.click()
        self.settle(w)
        self.assertEqual(self.rig.commands.ran_program("systemctl"), [[E["systemctl"], "reboot"]])

    def test_restart_failure_is_shown(self):
        self.rig.commands.outputs[(E["systemctl"], "reboot")] = fx.completed("", 1, "Access denied")
        w = self.window(page="install")
        w.page("install").restart_button.click()
        self.settle(w)
        self.assertEqual(w.message.level, "error")
        self.assertIn("Access denied", w.message.text.text())

    def test_about_says_live(self):
        w = self.window(page="about")
        self.assertEqual(w.page("about").grid.value("Session"), "Live session")


class PersonalizationTest(UiTestCase):
    def cards(self, w):
        return {card.preset.id: card for card in w.page("personalization").cards}

    def test_gallery(self):
        w = self.window()
        cards = self.cards(w)
        self.assertEqual(list(cards), ["boswas-dark", "boswas-light", "classic", "odd-preview"])
        self.assertTrue(cards["boswas-dark"].preset.current)
        self.assertIsNotNone(cards["boswas-dark"].current_badge)
        self.assertFalse(cards["boswas-dark"].apply.isEnabled())
        self.assertTrue(cards["boswas-light"].apply.isEnabled())
        self.assertIsNotNone(cards["boswas-dark"].preview.pixmap)          # decoded on a worker thread
        self.assertIsNone(cards["classic"].preview.pixmap)
        self.assertFalse(w.page("personalization").layout_box.isChecked())

    def test_apply_runs_the_exact_command(self):
        w = self.window()
        self.cards(w)["boswas-light"].apply.click()
        self.settle(w)
        self.assertEqual(self.rig.commands.ran_program("boswas-preset")[-2],
                         [E["boswas-preset"], "apply", "boswas-light"])
        self.assertEqual(self.questions, [])                               # no layout: no question
        self.assertEqual(w.page("personalization").banner.level, "ok")
        self.assertEqual(self.rig.commands.ran_program("boswas-preset")[-1], [E["boswas-preset"], "--json", "list"])

    def test_layout_needs_confirmation(self):
        w = self.window()
        page = w.page("personalization")
        page.layout_box.setChecked(True)
        self.answer = False
        self.cards(w)["classic"].apply.click()
        self.settle(w)
        self.assertEqual(len(self.questions), 1)
        self.assertIn("replaces your panels", self.questions[0][1])
        self.assertFalse(any(c[1] == "apply" for c in self.rig.commands.ran_program("boswas-preset")))
        self.answer = True
        self.cards(w)["classic"].apply.click()
        self.settle(w)
        self.assertIn([E["boswas-preset"], "apply", "classic", "--layout"],
                      self.rig.commands.ran_program("boswas-preset"))

    def test_reset_to_default_applies_the_default_preset_with_its_layout_after_asking(self):
        # The fixture list has no "horizon": the reset is disabled and says why.
        w = self.window()
        page = w.page("personalization")
        self.assertFalse(page.reset_button.isEnabled())
        self.assertIn("not installed", page.reset_button.toolTip())
        # With the default preset installed.
        doc = fx.preset_doc(self.rig.root)
        doc["presets"].append({"id": "horizon", "name": "Horizon", "description": "The Boswas OS default.",
                               "variant": "dark", "accent": "#D9B26E", "preview": None})
        self.rig.commands.outputs[(E["boswas-preset"], "--json", "list")] = fx.completed(json.dumps(doc))
        w2 = self.window()
        page = w2.page("personalization")
        self.assertTrue(page.reset_button.isEnabled())
        self.answer = False
        page.reset_button.click()
        self.settle(w2)
        self.assertEqual(len(self.questions), 1)
        self.assertIn("panels", self.questions[0][1])
        self.assertFalse(any(c[1] == "apply" for c in self.rig.commands.ran_program("boswas-preset")))
        self.answer = True
        page.reset_button.click()
        self.settle(w2)
        self.assertIn([E["boswas-preset"], "apply", "horizon", "--layout"], self.rig.commands.ran_program("boswas-preset"))
        self.assertEqual(page.banner.level, "ok")

    def test_reset_on_the_native_desktop_returns_to_prism_after_asking(self):
        # No "horizon" in the fixture list, and none is needed: Prism is no preset.
        self.rig.environ["XDG_CURRENT_DESKTOP"] = "Boswas"
        w = self.window()
        page = w.page("personalization")
        self.assertTrue(page.reset_button.isEnabled())
        self.assertIn(vm.NATIVE_DEFAULT_LOOK, page.reset_button.toolTip())
        self.answer = False
        page.reset_button.click()
        self.settle(w)
        self.assertEqual(len(self.questions), 1)
        self.assertIn(vm.NATIVE_DEFAULT_LOOK, self.questions[0][1])
        self.assertNotIn("panels", self.questions[0][1])
        self.assertNotIn([E["boswas-preset"], "reset"], self.rig.commands.ran_program("boswas-preset"))
        self.answer = True
        page.reset_button.click()
        self.settle(w)
        self.assertIn([E["boswas-preset"], "reset"], self.rig.commands.ran_program("boswas-preset"))
        self.assertFalse(any(c[1] == "apply" for c in self.rig.commands.ran_program("boswas-preset")))
        self.assertEqual(page.banner.level, "ok")
        self.assertTrue(page.reset_button.isEnabled())

    def test_default_preset_matches_the_shipped_presets(self):
        shipped = json.loads((Path(__file__).resolve().parents[3] / "desktop/presets/presets.json").read_text())
        self.assertEqual(shipped["default"], vm.DEFAULT_PRESET)
        names = {p["id"]: p["name"] for p in shipped["presets"]}
        self.assertEqual(names[vm.DEFAULT_PRESET], vm.DEFAULT_PRESET_NAME)

    def test_apply_failure(self):
        self.rig.commands.outputs[(E["boswas-preset"], "apply", "classic")] = fx.completed("", 2, "theme missing")
        w = self.window()
        self.cards(w)["classic"].apply.click()
        self.settle(w)
        banner = w.page("personalization").banner
        self.assertEqual(banner.level, "error")
        self.assertIn("theme missing", banner.text.text())
        self.assertTrue(self.cards(w)["classic"].apply.isEnabled())

    def test_presets_unavailable(self):
        self.rig.commands.installed.discard(E["boswas-preset"])
        w = self.window()
        page = w.page("personalization")
        self.assertEqual(page.cards, [])
        self.assertIn("not available", page.status.text())
        self.assertIsNone(page.programs)                                   # no program rows on this page

    def test_untrusted_names_are_plain_text(self):
        doc = fx.preset_doc(self.rig.root)
        doc["presets"][1]["name"] = "<b>Bold</b> <img src=x>"
        self.rig.commands.outputs[(E["boswas-preset"], "--json", "list")] = fx.completed(json.dumps(doc))
        w = self.window()
        card = self.cards(w)["boswas-light"]
        self.assertEqual(card.name.text(), "<b>Bold</b> <img src=x>")
        self.assertEqual(card.name.textFormat(), Qt.TextFormat.PlainText)


class ProgramTest(UiTestCase):
    def test_tiles_start_boswas_programs(self):
        w = self.window()
        page = self.show(w, "storage")
        self.assertIsInstance(page.programs.tile("files"), ProgramTile)
        self.assertEqual(page.programs.tile("files").title(), "Browse files")
        page.programs.tile("files").click()
        self.settle(w)
        self.assertEqual(self.rig.commands.started, [[E["boswas-files"], "/"]])

    def test_keyboard_opens_a_tile(self):
        w = self.window()
        tile = self.show(w, "storage").programs.tile("files")
        tile.setFocus()
        key_click(tile, Qt.Key.Key_Return)
        deadline = time.monotonic() + 3
        while not self.rig.commands.started and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            w.runner.wait(0.05)
        self.settle(w)
        self.assertEqual(self.rig.commands.started, [[E["boswas-files"], "/"]])

    def test_missing_program(self):
        self.rig.commands.installed.discard(E["boswas-files"])
        w = self.window()
        tile = self.show(w, "storage").programs.tile("files")
        self.assertFalse(tile.isEnabled())
        self.assertIn("Not installed", tile.toolTip())
        self.assertEqual(tile.description(), "Not installed on this system")

    def test_open_failure_is_shown(self):
        from boswas_control_center.errors import CommandFailed
        self.rig.commands.start_errors[(E["boswas-files"], "/")] = CommandFailed("boswas-files", "boom")
        w = self.window()
        self.show(w, "storage").programs.tile("files").click()
        self.settle(w)
        self.assertEqual(w.message.level, "error")
        self.assertIn("Browse files could not be opened", w.message.text.text())
        self.assertFalse(w.message_holder.isHidden())
        w.message.close_button.click()
        self.assertTrue(w.message_holder.isHidden())

    def test_every_program_of_the_catalog_is_offered(self):
        w = self.window()
        offered = 0
        for key in catalog.PAGE_KEYS:
            info = catalog.BY_KEY[key]
            if not info.programs or key in ("install", "windows"):
                continue
            page = self.show(w, key)
            self.assertEqual([t.program for t in page.programs.tiles], list(info.programs), key)
            offered += len(info.programs)
        self.assertEqual(offered, 1)
        # The Compatibility Manager is opened by the Windows page's own button.
        page = self.show(w, "windows")
        page.open_button.click()
        self.settle(w)
        self.assertEqual(self.rig.commands.started[-1], [E["boswas-compat-manager"]])

    def test_no_page_has_a_dead_tile(self):
        """Nothing on any page starts a KDE settings module or says "Not installed" for one."""
        w = self.window()
        for key in catalog.PAGE_KEYS:
            if key == "install" or not w.page_available(key):
                continue
            page = self.show(w, key)
            for tile in page.findChildren(ProgramTile):
                self.assertIn(tile.program.id, catalog.PROGRAMS, key)
        self.assertEqual([c for c in self.rig.commands.started if "kcm" in " ".join(c)], [])


class SecurityPageTest(UiTestCase):
    def test_states(self):
        page = self.show(self.window(), "security")
        self.assertEqual(page.line_state("firewall"), ("Secure", "nftables.service active (Boswas baseline ruleset)"))
        self.assertEqual(page.line_state("apparmor")[0], "Warning")
        self.assertEqual(page.line_state("winapp")[0], "Secure")
        self.assertEqual(page.line_state("disk-encryption")[0], "Info")
        self.assertEqual(page.line_state("policy")[0], "Info")
        self.assertEqual(page.summary.headline.text(), "Protected, with 3 items to review")
        self.assertEqual(page.summary.icon.state, vm.WARNING)
        titles = [line.title.text() for line in page.other_lines if not line.isHidden()]
        self.assertEqual(titles, ["TPM", "Audit logging", "SSH server", "Root account", "Repository trust",
                                  "USB storage policy"])
        self.assertEqual(page.lines["firewall"].accessibleName(), "Firewall: Secure")
        self.assertIsNone(page.programs)

    def test_without_the_boswas_tool_and_the_agent(self):
        self.rig.commands.installed.discard(E["boswas"])
        self.rig.services.agent_down = True
        page = self.show(self.window(), "security")
        self.assertEqual(page.line_state("firewall")[0], "Error")
        self.assertIn("not installed", page.line_state("firewall")[1])
        self.assertEqual(page.line_state("agent")[0], "Attention")
        self.assertEqual(page.line_state("policy")[0], "Not available")
        self.assertEqual(page.summary.icon.state, vm.ATTENTION)


class ApplicationsPageTest(UiTestCase):
    def test_groups_and_search(self):
        page = self.show(self.window(), "applications")
        self.assertEqual(page.visible_keys(), [
            "desktop:com.boswas.CompatibilityManager", "windows:com.example.notepad", "windows:com.example.paint",
            "desktop:boswas-only", "desktop:org.kde.dolphin", "desktop:firefox-esr", "desktop:org.kde.kate",
            "desktop:kde4-oldapp"])
        page.search.setText("kate")
        self.assertEqual(page.visible_keys(), ["desktop:org.kde.kate"])
        page.search.setText("example")
        self.assertEqual(page.visible_keys(), ["windows:com.example.notepad", "windows:com.example.paint"])
        page.search.setText("no such thing")
        self.assertEqual(page.visible_keys(), [])
        self.assertEqual(page.count.text(), "0 applications")

    def test_open_native_application(self):
        w = self.window()
        page = self.show(w, "applications")
        self.assertTrue(page.select("desktop:org.kde.kate"))
        self.assertTrue(page.detail.open_button.isVisibleTo(page.detail))
        self.assertFalse(page.detail.manage_button.isVisibleTo(page.detail))
        page.detail.open_button.click()
        self.settle(w)
        self.assertEqual(self.rig.commands.started,
                         [[E["gio"], "launch", str(self.rig.base / "usr/share/applications/org.kde.kate.desktop")]])

    def test_windows_applications_go_to_the_compatibility_manager(self):
        w = self.window()
        page = self.show(w, "applications")
        page.select("windows:com.example.paint")
        self.assertFalse(page.detail.open_button.isVisibleTo(page.detail))
        page.detail.manage_button.click()
        self.settle(w)
        page._activated(page.list.currentItem())
        self.settle(w)
        self.assertEqual(self.rig.commands.started, [[E["boswas-compat-manager"]], [E["boswas-compat-manager"]]])

    def test_arrow_keys_skip_headers(self):
        page = self.show(self.window(), "applications")
        page.list.setFocus()
        page.select("desktop:com.boswas.CompatibilityManager")
        key_click(page.list, Qt.Key.Key_Down)
        self.assertEqual(page.list.currentItem().data(Qt.ItemDataRole.UserRole + 1).key, "windows:com.example.notepad")
        page.select("windows:com.example.paint")
        key_click(page.list, Qt.Key.Key_Down)
        self.assertEqual(page.list.currentItem().data(Qt.ItemDataRole.UserRole + 1).key, "desktop:boswas-only")

    def test_session_service_down(self):
        self.rig.services.session_down = True
        page = self.show(self.window(), "applications")
        self.assertNotIn("windows:com.example.notepad", page.visible_keys())
        header = next(page.list.item(i) for i in range(page.list.count())
                      if page.list.item(i).data(Qt.ItemDataRole.UserRole) == "header"
                      and page.list.item(i).data(Qt.ItemDataRole.UserRole + 1)[0] == "Windows applications")
        self.assertIn("session service is not running", header.data(Qt.ItemDataRole.UserRole + 1)[2])

    def test_store_note(self):
        page = self.show(self.window(), "applications")
        self.assertIn("signed repositories", page.store_note.text.text())
        self.assertIn("Compatibility Manager", page.store_note.text.text())


class StatusPagesTest(UiTestCase):
    def test_windows_page(self):
        w = self.window()
        page = self.show(w, "windows")
        self.assertEqual(page.grid.value("Supported applications"),
                         "64-bit (x86_64) and 32-bit (x86) Windows applications")
        self.assertIn("32-bit (x86)", page.note.text())
        self.assertNotIn("applications only", page.note.text())
        self.assertEqual(page.grid.value("Installed applications"), "2")
        page.open_button.click()
        self.settle(w)
        self.assertEqual(self.rig.commands.started, [[E["boswas-compat-manager"]]])

    def test_updates_page(self):
        page = self.show(self.window(), "updates")
        self.assertEqual(page.summary.headline.text(), "Automatic security updates are on")
        self.assertEqual(page.grid.value("Device update policy"), "Security updates only")
        self.assertIn("All upgrades installed", page.log.text())

    def test_privacy_page(self):
        page = self.show(self.window(), "privacy")
        self.assertEqual(page.summary.headline.text(), "Nothing leaves this device")
        self.assertTrue(page.grid.value("Inventory").startswith("Standard"))

    def test_storage_and_power(self):
        w = self.window()
        storage = self.show(w, "storage")
        self.assertIn("system", storage.cards)
        self.assertIn("used of", storage.cards["system"][2].text())
        power = self.show(w, "power")
        self.assertFalse(hasattr(power, "battery_rows"))    # the battery is a settings section now (sound_power)

    def test_system_page(self):
        page = self.show(self.window(), "system")
        self.assertEqual(page.grid.value("Device ID"), fx.AGENT["device_id"])
        self.assertIsNone(page.programs)

    def test_about_page(self):
        w = self.window()
        page = self.show(w, "about")
        self.assertEqual(page.name.text(), "Boswas OS")
        self.assertEqual(page.grid.value("Version"), "v1 Alpha (1.0~alpha3)")
        self.assertEqual(page.grid.value("Desktop"), "Boswas Desktop 1.0~alpha3")
        self.assertEqual(page.grid.value("Session"), "Installed")
        self.assertEqual(page.grid.value("Security"), "7 protections active, 3 items to review")
        self.assertEqual(page.technical_grid.value("Package base"), "Debian 13.7")
        self.assertFalse(page.technical.is_open())
        page.technical.toggle.click()
        self.assertTrue(page.technical.is_open())
        page.copy()
        self.assertIn("Package base: Debian 13.7", QGuiApplication.clipboard().text())


class AccessibilityTest(UiTestCase):
    def test_controls_have_names_and_focus(self):
        w = self.window()
        for key in catalog.PAGE_KEYS:
            if key != "install" and w.page_available(key):
                self.show(w, key)
        self.assertTrue(w.sidebar.accessibleName())
        for button in w.findChildren(QAbstractButton):
            if isinstance(button.parent(), QLineEdit):
                continue                                   # the line edit's own clear button
            name = button.accessibleName() or button.text() or button.toolTip()
            self.assertTrue(name, f"{type(button).__name__} without a name")
        for tile in w.findChildren(ProgramTile):
            self.assertEqual(tile.accessibleName(), tile.program.title)
            self.assertTrue(tile.accessibleDescription())
            self.assertEqual(tile.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        for edit in w.findChildren(QLineEdit):
            # The text field inside a spin box or time box is named through that box.
            named = edit.parent() if isinstance(edit.parent(), QAbstractSpinBox) else edit
            self.assertTrue(named.accessibleName(), f"{type(named).__name__} without a name")

    def test_labels_are_plain_text_and_no_style_sheets(self):
        w = self.window()
        for key in catalog.PAGE_KEYS:
            if key != "install" and w.page_available(key):
                self.show(w, key)
        for label in w.findChildren(QLabel):
            if label.text():
                self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText, label.text()[:40])
        for widget in [w, *w.findChildren(QAbstractButton), *w.findChildren(QLabel)]:
            self.assertEqual(widget.styleSheet(), "")


if __name__ == "__main__":
    unittest.main()
