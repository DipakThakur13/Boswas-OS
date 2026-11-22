"""Widget tests of the pages added with the categories (Qt offscreen platform, real Backend on the fakes).

Each page is shown in a whole window on the fake root, fake programs and the
fake services bridge (cc_fixtures). The tests check what is shown, that
nothing changes before a click and a confirmation, and the exact command
(or file change) each action makes: renaming the device, the
troubleshooters, advanced start-up, storage cleanup, printers, cameras,
USB, VPN connections and network adapters, fonts, installed apps, optional
features, app permissions, the update history and options, Diagnostics &
feedback, and Home's recommended settings.

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
    from PySide6.QtCore import QCoreApplication, QEvent, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication
    PYSIDE = True
except ImportError:                       # PySide6 is not a build dependency: skip the widget tests
    PYSIDE = False

if PYSIDE:
    from boswas_fluent import testing
    from boswas_fluent.worker import shared_worker

    from boswas_control_center import commands
    from boswas_control_center import compositorconfig as cc
    from boswas_control_center import servicebridge as sb
    from boswas_control_center.commands import EXECUTABLES as E
    from boswas_control_center.ui import bridgeclient
    from boswas_control_center.ui.main_window import MainWindow
    from boswas_control_center.ui.settings import context

    fx.use_fake_bridge()
    APP = QApplication.instance() or QApplication(["test"])

    def _quiet_offscreen(_mode, _context, message):
        if "This plugin does not support" not in message and "boswas-os-mark.svg" not in message:
            sys.stderr.write(message + "\n")

    qInstallMessageHandler(_quiet_offscreen)


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class PageCase(unittest.TestCase):
    def setUp(self):
        self.errors: list[str] = []
        self._excepthook = sys.excepthook
        sys.excepthook = lambda *exc: self.errors.append("".join(traceback.format_exception(*exc)))
        self.tmp = tempfile.TemporaryDirectory()
        self.rig = fx.Rig(Path(self.tmp.name))
        self.addCleanup(fx.use_rig_for_settings(self.rig))
        self.cmd = self.rig.commands
        self.home = Path(self.rig.environ["HOME"])
        self.windows: list = []
        self.questions: list[tuple[str, str, str]] = []
        self.answer = True
        self.chosen: str | None = None

    def tearDown(self):
        for window in self.windows:
            window.jobs.current = None
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        sys.excepthook = self._excepthook
        self.tmp.cleanup()
        if self.errors:
            self.fail("exceptions in Qt callbacks:\n" + "\n".join(self.errors))

    def window(self, page: str | None = None):
        window = MainWindow(self.rig.backend, initial_page=page)
        window.ask = self._ask
        window.choose_file = lambda title, filters: self.chosen
        self.windows.append(window)
        self.settle(window)
        return window

    def _ask(self, title: str, text: str, confirm: str) -> bool:
        self.questions.append((title, text, confirm))
        return self.answer

    @staticmethod
    def settle(window) -> None:
        for _ in range(4):
            window.runner.wait(15)
            shared_worker().wait(15)
            QCoreApplication.processEvents()

    def section(self, window, key: str, name: str):
        window.show_page(key)
        self.settle(window)
        found = window.page(key).section(name)
        self.assertIsNotNone(found, name)
        return found

    def ran(self, program: str) -> list[list[str]]:
        return self.cmd.ran_program(program)


class DeviceNameTest(PageCase):
    def test_rename_after_checking_the_name(self):
        self.cmd.outputs[tuple(commands.hostnamectl_argv("alice-pc"))] = fx.completed()
        w = self.window()
        section = self.section(w, "about", "device-name")
        self.assertEqual(section.card.title(), "alice-laptop")
        section.rename_device()
        dialog = section.dialog
        self.assertEqual(dialog.edit.text(), "alice-laptop")
        self.assertFalse(dialog.primary_button.isEnabled())                 # unchanged
        dialog.edit.setText("Alice PC")
        self.assertFalse(dialog.primary_button.isEnabled())
        self.assertIn("Alice-PC", dialog.problem.text())                    # a suggestion
        dialog.edit.setText("-bad")
        self.assertIn("hyphen", dialog.problem.text())
        dialog.edit.setText("alice-pc")
        self.assertTrue(dialog.primary_button.isEnabled())
        self.assertEqual(self.ran("hostnamectl"), [])
        dialog.primary_button.click()
        self.settle(w)
        self.assertEqual(self.ran("hostnamectl"), [[E["hostnamectl"], "--static", "set-hostname", "alice-pc"]])
        self.assertIn("now called alice-pc", section.notice.message())

    def test_a_refusal_is_said(self):
        self.cmd.outputs[tuple(commands.hostnamectl_argv("pc2"))] = fx.completed("", 1, "Access denied")
        w = self.window()
        section = self.section(w, "about", "device-name")
        section.set_name("pc2")
        self.settle(w)
        self.assertIn("not allowed", section.error.message())
        section.set_name("bad name")
        self.assertEqual(len(self.ran("hostnamectl")), 1)                  # never run with an invalid name


class TroubleshootTest(PageCase):
    def setUp(self):
        super().setUp()
        saved = (context.resolve, context.fetch)
        self.contacted: list[str] = []
        context.resolve = lambda host: self.contacted.append(host) or ["151.101.2.132"]
        context.fetch = lambda url: self.contacted.append(url) or 200

        def restore():
            context.resolve, context.fetch = saved
        self.addCleanup(restore)

    def test_nothing_runs_until_asked(self):
        w = self.window()
        section = self.section(w, "troubleshoot", "troubleshooters")
        self.assertEqual(self.contacted, [])
        self.assertEqual(self.ran("systemctl"), [])
        self.assertIn("deb.debian.org", section.checks["internet"].header.description())
        self.assertTrue(section.checks["internet"].busy.isHidden())

    def test_internet(self):
        from boswas_control_center import netinfo
        w = self.window()
        section = self.section(w, "troubleshoot", "troubleshooters")
        item = netinfo.Interface("wlp2s0", "Wi-Fi", "", True, ("192.168.1.5/24",), (), "192.168.1.1")
        section.details_reader = lambda: netinfo.Details((item,), ("192.168.1.1",))
        section.run("internet")
        self.settle(w)
        report = section.checks["internet"].report
        self.assertTrue(report.ok, report)
        self.assertEqual(self.contacted, ["deb.debian.org", "https://deb.debian.org/"])
        self.assertTrue(section.checks["internet"].is_expanded())
        context.fetch = lambda url: (_ for _ in ()).throw(OSError("timed out"))
        section.run("internet")
        self.settle(w)
        self.assertEqual(section.checks["internet"].report.headline, "Secure websites cannot be reached")

    def test_sound_bluetooth_and_display(self):
        w = self.window()
        section = self.section(w, "troubleshoot", "troubleshooters")
        bridge = bridgeclient.for_widget(section)
        speakers = sb.AudioDevice(1, "Speakers", 0.0, False, True)
        bridge.set_state(sb.AUDIO, sb.AudioState(True, speakers, None, (speakers,)))
        section.run("sound")
        self.settle(w)
        self.assertEqual(section.checks["sound"].report.headline, "The volume is at zero")
        bridge.set_state(sb.BLUETOOTH, sb.BluetoothState(True, False))
        section.run("bluetooth")
        self.settle(w)
        self.assertEqual(section.checks["bluetooth"].report.headline, "Bluetooth is turned off")
        self.assertEqual(self.ran("systemctl"), [[E["systemctl"], "is-active", "bluetooth.service"]])

        class Compositor:
            class control:
                @staticmethod
                def outputs():
                    return [cc.Output("eDP-1", True, mode=cc.Mode(2560, 1600, 165000))]

        section.compositor = Compositor()
        section.run("display")
        self.settle(w)
        self.assertTrue(section.checks["display"].report.ok)
        self.assertEqual(self.cmd.changes(), [])


class RecoveryTest(PageCase):
    def test_firmware_settings_only_where_supported(self):
        w = self.window()
        section = self.section(w, "recovery", "advanced-start-up")
        self.assertFalse(section.firmware.isHidden())
        self.assertTrue(section.menu.isHidden())                          # logind: "na"
        self.assertTrue(section.none.isHidden())
        self.answer = False
        section.firmware_button.click()
        self.settle(w)
        self.assertEqual(self.questions[-1][2], "Restart Now")
        self.assertNotIn([E["systemctl"], "reboot", "--firmware-setup"], self.ran("systemctl"))
        self.answer = True
        self.cmd.outputs[(E["systemctl"], "reboot", "--firmware-setup")] = fx.completed()
        section.firmware_button.click()
        self.settle(w)
        self.assertIn([E["systemctl"], "reboot", "--firmware-setup"], self.ran("systemctl"))

    def test_nothing_offered_without_support(self):
        for question in fx.LOGIND:
            self.cmd.outputs[tuple(commands.logind_argv(question))] = fx.completed('{"type":"s","data":["na"]}')
        w = self.window()
        section = self.section(w, "recovery", "advanced-start-up")
        self.assertTrue(section.firmware.isHidden())
        self.assertFalse(section.none.isHidden())


class CleanupTest(PageCase):
    def test_only_what_is_ticked_is_cleared_after_a_question(self):
        w = self.window()
        section = self.section(w, "storage", "cleanup-recommendations")
        self.assertEqual([item.name for _c, _b, item in section.cache_list.rows], ["mozilla", "pip"])
        self.assertFalse(any(box.isChecked() for _c, box, _i in section.cache_list.rows))     # nothing preselected
        self.assertFalse(section.cache_button.isEnabled())
        section.cache_list.rows[1][1].setChecked(True)
        self.assertTrue(section.cache_button.isEnabled())
        self.answer = False
        section.cache_button.click()
        self.assertTrue((self.home / ".cache/pip").exists())
        self.answer = True
        section.cache_button.click()
        self.settle(w)
        self.assertIn("pip (117.2 KiB)", self.questions[-1][1])
        self.assertFalse((self.home / ".cache/pip").exists())
        self.assertTrue((self.home / ".cache/mozilla").exists())
        self.assertEqual(self.cmd.privileged(), [])

    def test_trash_and_downloads(self):
        w = self.window()
        section = self.section(w, "storage", "cleanup-recommendations")
        self.assertIn("1 item", section.trash.description())
        section.trash_button.click()
        self.settle(w)
        self.assertIn("cannot be undone", self.questions[-1][1])
        self.assertEqual(os.listdir(self.home / ".local/share/Trash/files"), [])
        self.assertEqual([item.name for _c, _b, item in section.downloads_list.rows], ["installer-2025.iso"])
        section.downloads_list.rows[0][1].setChecked(True)
        section.downloads_button.click()
        self.settle(w)
        self.assertTrue((self.home / ".local/share/Trash/files/installer-2025.iso").exists())
        self.assertTrue((self.home / "Downloads/today.pdf").exists())
        self.assertIn("1 item", section.trash.description())


class PrintersTest(PageCase):
    def test_printers_and_a_test_page(self):
        w = self.window()
        section = self.section(w, "printers", "printers")
        self.assertTrue(section.missing.isHidden())
        office, home = section.list.row("Office_Laser"), section.list.row("Home_Inkjet")
        self.assertEqual(office.description(), "Stopped · Default printer")
        self.assertFalse(office.control().isEnabled())
        self.assertEqual(home.description(), "Ready")
        home.control().click()
        self.settle(w)
        self.assertEqual(self.questions[-1][0], "Print a test page on Home_Inkjet?")
        self.assertEqual(self.ran("lp"), [[E["lp"], "-d", "Home_Inkjet", "/usr/share/cups/data/testprint"]])

    def test_install_printing_support_without_cups(self):
        os.unlink(self.rig.root / "usr/sbin/cupsd")
        w = self.window()
        section = self.section(w, "printers", "printers")
        self.assertFalse(section.missing.isHidden())
        self.assertTrue(section.web.isHidden())
        self.assertEqual(self.ran("lpstat"), [])
        section.install_button.click()
        self.settle(w)
        self.assertIn("cups", self.questions[-1][1])
        self.assertEqual(self.cmd.streamed, [commands.sysops_argv("apt-install", "cups")])


class DevicesTest(PageCase):
    def test_cameras_and_usb(self):
        w = self.window()
        cameras = self.section(w, "cameras", "connected-cameras")
        self.assertEqual(cameras.list.keys(), ["video0"])
        self.assertEqual(cameras.list.row("video0").title(), "Integrated Camera: Integrated C")
        page = w.page("cameras")
        self.assertEqual([t.program.id for t in page.programs.tiles], ["camera"])
        page.programs.tile("camera").click()
        self.settle(w)
        self.assertEqual(self.cmd.started, [[E["boswas-camera"]]])
        usb = self.section(w, "usb", "usb-devices")
        self.assertEqual([usb.list.row(k).title() for k in usb.list.keys()], ["USB Keyboard"])
        self.assertNotIn("SECRET", usb.list.row(usb.list.keys()[0]).description())


class NetworkTest(PageCase):
    def test_vpn_connections(self):
        w = self.window()
        section = self.section(w, "vpn", "vpn-connections")
        self.assertEqual(section.list.keys(), [fx.WG_UUID, fx.VPN_UUID])
        office = section.list.row(fx.VPN_UUID)
        self.assertEqual(office.description(), "VPN · Not connected")
        office.connect_button.click()
        self.settle(w)
        self.assertIn([E["nmcli"], "-t", "connection", "up", "uuid", fx.VPN_UUID], self.ran("nmcli"))
        self.assertFalse(section.error.isHidden())                          # no canned answer: it failed, said so
        self.cmd.outputs[tuple(commands.nmcli_change_argv("connection", "up", "uuid", fx.VPN_UUID))] = \
            fx.completed("", 4, "Error: Connection activation failed: Secrets were required, but not provided.")
        office.connect_button.click()
        self.settle(w)
        self.assertIn("no prompt", section.error.message())
        self.answer = False
        section.list.row(fx.WG_UUID).remove_button.click()
        self.settle(w)
        self.assertNotIn("delete", [word for c in self.ran("nmcli") for word in c])

    def test_import_by_content(self):
        w = self.window()
        section = self.section(w, "vpn", "vpn-connections")
        wg = Path(self.tmp.name) / "wg-office.conf"
        wg.write_text("[Interface]\nPrivateKey = abc=\n[Peer]\nPublicKey = def=\n")
        self.chosen = str(wg)
        self.cmd.outputs[tuple(commands.nmcli_change_argv("connection", "import", "type", "wireguard", "file",
                                                          str(wg)))] = fx.completed("Connection added.\n")
        section.add_button.click()
        self.settle(w)
        self.assertEqual(self.questions[-1][0], "Add this WireGuard connection?")
        self.assertEqual([c for c in self.ran("nmcli") if "import" in c],
                         [commands.nmcli_change_argv("connection", "import", "type", "wireguard", "file", str(wg))])
        self.assertIn("was added", section.notice.message())
        other = Path(self.tmp.name) / "notes.txt"
        other.write_text("hello\n")
        section.import_file(str(other))
        self.assertIn("not a WireGuard or OpenVPN", section.error.message())

    def test_adapters(self):
        w = self.window()
        section = self.section(w, "advanced-network", "network-adapters")
        self.assertEqual(section.list.keys(), ["enp3s0", "wlp2s0"])
        wifi = section.list.row("wlp2s0")
        self.assertIn("Received 1.4 GiB", wifi.usage_row.description())
        self.assertEqual(wifi.mac_row.description(), "Shown only when you ask")
        wifi.mac_button.click()
        self.assertEqual(wifi.mac_row.description(), "a0:b1:c2:d3:e4:f5")
        self.assertFalse(section.list.row("enp3s0").switch.isEnabled())     # no cable: nothing to enable
        self.answer = False
        wifi.switch.click()
        self.settle(w)
        self.assertEqual(self.cmd.changes(), [])
        self.answer = True
        wifi.switch.click()
        self.settle(w)
        self.assertEqual(self.cmd.changes(), [[E["nmcli"], "-t", "device", "disconnect", "wlp2s0"]])


class FontsTest(PageCase):
    def test_list_preview_install_and_remove(self):
        w = self.window()
        section = self.section(w, "fonts", "fonts")
        self.assertEqual([section.list.item(i).text() for i in range(section.list.count())],
                         ["Hack", "Handwriting Demo", "Inter", "Liberation Serif", "Noto Sans"])
        section.search.setText("inter")
        self.assertEqual(section.list.count(), 1)
        self.assertEqual(section.preview_card.title_label.text(), "Inter")
        self.assertTrue(section.remove_button.isHidden())                   # a font of the system
        section.size.setCurrentIndex(0)
        self.assertEqual(section.sample.font().pointSize(), 12)
        section.search.setText("handwriting")
        self.assertFalse(section.remove_button.isHidden())
        font = Path(self.tmp.name) / "Nice Font.otf"
        font.write_bytes(b"OTTO" + b"\x00" * 300)
        self.chosen = str(font)
        section.install_button.click()
        self.settle(w)
        installed = self.home / ".local/share/fonts/Nice Font.otf"
        self.assertTrue(installed.exists())
        cache = [E["fc-cache"], "-f", str(self.home / ".local/share/fonts")]
        self.assertIn(cache, self.ran("fc-cache"))
        section.search.setText("handwriting")
        section.remove_button.click()
        self.settle(w)
        self.assertEqual(self.questions[-1][2], "Uninstall")
        self.assertFalse((self.home / ".local/share/fonts/HandwritingDemo.ttf").exists())


class AppsTest(PageCase):
    def test_installed_apps(self):
        w = self.window()
        section = self.section(w, "installed-apps", "installed-apps")
        rows = {section.list.row(key).title(): section.list.row(key) for key in section.list.keys()}
        self.assertIn("Debian package · kate · 24.12.3-1 · 8.8 MiB", rows["Kate"].description())
        self.assertTrue(rows["Boswas Control Center"].action.isHidden())       # a part of Boswas OS
        self.assertEqual(rows["Example Notepad"].action.text(), "Manage")
        section.source.setCurrentIndex(section.source.findData("windows"))
        section.populate()
        self.assertEqual({section.list.row(k).title() for k in section.list.keys()},
                         {"Example Notepad", "Example Paint"})
        section.source.setCurrentIndex(0)
        section.populate()
        rows["Kate"].action.click()
        self.settle(w)
        self.assertEqual(self.questions[-1][0], "Uninstall Kate?")
        self.assertEqual(self.cmd.streamed, [commands.sysops_argv("apt-remove", "kate")])
        rows["Example Notepad"].action.click()
        self.settle(w)
        self.assertEqual(self.cmd.started[-1], [E["boswas-compat-manager"]])

    def test_optional_features(self):
        w = self.window()
        section = self.section(w, "optional-features", "optional-features")
        keys = section.list.keys()
        self.assertIn("firefox-de", keys)                                   # de_DE is generated on this device
        self.assertEqual(section.list.row("7zip").action.text(), "Remove")
        self.assertEqual(section.list.row("7zip").state_label.text(), "Installed")
        printing = section.list.row("printing")
        self.assertEqual(printing.action.text(), "Install")
        self.answer = False
        printing.action.click()
        self.assertEqual(self.cmd.streamed, [])
        self.answer = True
        printing.action.click()
        self.settle(w)
        self.assertEqual(self.cmd.streamed, [commands.sysops_argv("apt-install", "cups")])


class PrivacyTest(PageCase):
    def test_app_permissions(self):
        w = self.window()
        section = self.section(w, "app-permissions", "app-permissions")
        self.assertEqual(section.list.keys(), ["org.example.Draw", "org.example.Maps", "org.example.Meet"])
        meet = section.list.row("org.example.Meet")
        self.assertEqual(meet.header.description(), "2 permissions, 2 allowed")
        camera = next(row for row in meet.rows if row.title() == "Camera")
        self.assertEqual(camera.description(), "Allowed")
        camera.control().click()
        self.settle(w)
        self.assertEqual(self.questions[-1][0], "Revoke camera for org.example.Meet?")
        self.assertEqual(self.cmd.changes(), [[E["flatpak"], "permission-remove", "devices", "camera",
                                               "org.example.Meet"]])
        self.assertIn("not sandboxed", section.native.description())

    def test_diagnostics(self):
        page = self.window(page="privacy").page("privacy")
        self.assertIn("sends no diagnostic data", page.diagnostics_text.text())
        self.assertFalse(page.statement.is_open())
        self.assertIn("Never on any device", page.statement_text.text())


class UpdatesTest(PageCase):
    def test_history_and_options(self):
        w = self.window()
        history = self.section(w, "update-history", "update-history")
        self.assertEqual(len(history.list.keys()), 3)
        self.assertIn("1 by automatic security updates", history.summary.description())
        first = history.list.row(history.list.keys()[0])
        self.assertEqual(first.header.title(), "Updated 2 packages")
        self.assertIn("libssl3t64", " ".join(card.description() for card in first.cards()))
        options = self.section(w, "update-options", "automatic-updates")
        values = options.values()
        self.assertEqual(values["Automatic security updates"], "On (unattended-upgrades)")
        self.assertIn("Download updates in advance", values)
        updates = w.page("updates")
        self.assertEqual(updates.subpages.visible_keys(), ["update-history", "update-options"])


if PYSIDE:
    import test_settings_display as shared                # the compositor stand-in of the display tests

    class RecommendedTest(testing.FluentTestCase):
        def test_dark_mode_and_night_light_apply_at_once(self):
            from boswas_control_center.ui.home import RecommendedSection
            compositor = shared.FakeCompositor(shared.HEADLESS)
            settings = cc.Settings(dict(os.environ), system_files=(str(self.tmp / "a.toml"), str(self.tmp / "b.toml")),
                                   control=compositor)
            compositor.attach(settings)
            section = self.keep(RecommendedSection(config=settings))
            self.show(section, 600, 400)
            section.activate()
            shared.settle(section)
            self.assertTrue(section.dark.isChecked())                     # dark is the default
            testing.click(section.dark)
            shared.settle(section)
            self.assertEqual(settings.snapshot().get("appearance", "scheme"), "light")
            testing.click(section.night)
            shared.settle(section)
            self.assertTrue(settings.snapshot().get("night_light", "enabled"))
            section.deactivate()


if __name__ == "__main__":
    unittest.main()
