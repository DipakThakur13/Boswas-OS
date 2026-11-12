"""Widget tests of the functional pages (Qt offscreen platform, real Backend on the fakes).

Covers: nothing privileged runs when the window opens or a page is shown;
the Update Center (check, install, Retry, Cancel, authorization denied, one
operation at a time, closing during an operation); the Android page (states,
disabled-with-reason controls, APK inspection and installation, F-Droid,
Google Play shown as reported); the Software Center tabs (Debian packages,
Flatpak, AppImages, file routing); Hardware & Drivers (devices, firmware
report, installing a firmware package); Storage (disks, cleanup); Network;
the system overview. The widget tests skip themselves when PySide6 is not
installed; no Qt name is used outside the guard.

Run: QT_QPA_PLATFORM=offscreen python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import traceback
import unittest
import zipfile
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
    from boswas_control_center import android, catalog, commands, jobs
    from boswas_control_center.commands import EXECUTABLES as E
    from boswas_control_center.ui.main_window import MainWindow
    from boswas_control_center.ui.software_tabs import APPIMAGE_WARNING, LOCAL_DEB_WARNING

    APP = QApplication.instance() or QApplication(["test"])

    def _quiet_offscreen(_mode, _context, message):
        if "This plugin does not support" not in message and "boswas-os-mark.svg" not in message:
            sys.stderr.write(message + "\n")

    qInstallMessageHandler(_quiet_offscreen)


def pump(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class FunctionalCase(unittest.TestCase):
    live = False

    def setUp(self):
        self.errors: list[str] = []
        self._excepthook = sys.excepthook
        sys.excepthook = lambda *exc: self.errors.append("".join(traceback.format_exception(*exc)))
        self.tmp = tempfile.TemporaryDirectory()
        self.rig = fx.Rig(Path(self.tmp.name), live=self.live)
        self.cmd = self.rig.commands
        self.windows: list = []
        self.questions: list[tuple[str, str, str]] = []
        self.answer = True
        self.chosen: str | None = None
        self.gates: list[threading.Event] = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        for window in self.windows:
            window.close_when_done = False
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
            if not window.runner.wait(15):
                raise AssertionError("backend calls did not finish")
            QCoreApplication.processEvents()

    def show(self, window, key: str):
        window.show_page(key)
        self.settle(window)
        return window.page(key)

    def script(self, words, lines, code=0, kind="sysops"):
        command = commands.sysops_argv(*words) if kind == "sysops" else commands.android_argv(*words)
        self.cmd.streams[tuple(command)] = (lines, code)
        return command

    def blocking(self, words, cancellable_phase: bool = True):
        """A sysops operation that waits until it is cancelled or the gate opens."""
        gate = threading.Event()
        self.gates.append(gate)

        def script(handle, emit):
            emit(json.dumps({"type": "start", "operation": words[0], "cancellable": True}))
            emit(json.dumps({"type": "progress", "phase": "download" if cancellable_phase else "install",
                             "percent": 30, "message": "Working", "cancellable": cancellable_phase}))
            while not gate.is_set():
                if handle.cancel_requested and cancellable_phase:
                    emit(json.dumps({"type": "result", "ok": False, "code": "CANCELLED", "message": "Cancelled.",
                                     "data": {}}))
                    return 4
                time.sleep(0.01)
            emit(json.dumps({"type": "result", "ok": True, "code": "OK", "message": "Finished.", "data": {}}))
            return 0

        self.cmd.streams[tuple(commands.sysops_argv(*words))] = script
        return gate

    def apk_file(self, name: str = "notes.apk") -> str:
        path = Path(self.tmp.name) / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00")
        return str(path)


class NothingPrivilegedAtOpenTest(FunctionalCase):
    def test_every_page_reads_only(self):
        w = self.window()
        for key in catalog.PAGE_KEYS:
            if key != "install":
                self.show(w, key)
        for tab in range(w.page("applications").tabs.count()):
            w.page("applications").tabs.setCurrentIndex(tab)
            self.settle(w)
        self.assertEqual(self.cmd.privileged(), [])
        self.assertEqual(self.cmd.changes(), [])
        self.assertEqual(self.cmd.streamed, [])
        self.assertEqual(self.questions, [])


class LiveNothingPrivilegedTest(NothingPrivilegedAtOpenTest):
    live = True


class UpdateCenterTest(FunctionalCase):
    def test_pending_updates_are_listed(self):
        page = self.show(self.window(), "updates")
        self.assertEqual(page.tree.topLevelItemCount(), 4)
        self.assertEqual(page.tree.topLevelItem(0).text(3), "Security")
        self.assertIn("4 updates pending, 2 of them security updates", page.pending_text.text())
        self.assertTrue(page.install_button.isEnabled())
        self.assertTrue(page.check_button.isEnabled())
        self.assertTrue(page.panel.isHidden())

    def test_check_asks_first_and_reports_the_result(self):
        w = self.window()
        page = self.show(w, "updates")
        self.answer = False
        page.check_button.click()
        self.settle(w)
        self.assertEqual(self.cmd.streamed, [])
        self.answer = True
        self.script(("apt-refresh",), fx.sysops_lines("apt-refresh", message="Package lists refreshed."))
        before = len(self.cmd.ran_program("apt-get"))
        page.check_button.click()
        self.settle(w)
        self.assertEqual(self.cmd.streamed, [commands.sysops_argv("apt-refresh")])
        self.assertIn("administrator password", self.questions[-1][1])
        self.assertEqual(page.panel.result.level, "ok")
        self.assertEqual(page.panel.result.text.text(), "Package lists refreshed.")
        self.assertTrue(page.panel.bar.isHidden())               # no stale progress
        self.assertGreater(len(self.cmd.ran_program("apt-get")), before)

    def test_install_locked_then_retry(self):
        w = self.window()
        page = self.show(w, "updates")
        self.script(("apt-upgrade",), fx.sysops_lines("apt-upgrade", ok=False, code="LOCKED", message="dpkg is busy."),
                    3)
        page.install_button.click()
        self.settle(w)
        self.assertIn("2 security updates", self.questions[-1][1])
        self.assertIn("never removed", self.questions[-1][1])
        self.assertEqual(page.panel.result.level, "error")
        self.assertFalse(page.panel.retry_button.isHidden())
        self.script(("apt-upgrade",), fx.sysops_lines("apt-upgrade", message="Updates installed."))
        page.panel.retry_button.click()
        self.settle(w)
        self.assertEqual(len(self.cmd.streamed), 2)
        self.assertEqual(page.panel.result.level, "ok")
        self.assertTrue(page.panel.retry_button.isHidden())

    def test_authorization_denied(self):
        w = self.window()
        page = self.show(w, "updates")
        self.script(("apt-refresh",), ["Error executing command as another user: Not authorized"], 127)
        page.check_button.click()
        self.settle(w)
        self.assertEqual(page.panel.result.level, "warning")
        self.assertEqual(page.panel.result.text.text(), jobs.AUTH_MESSAGE)
        self.assertTrue(page.panel.retry_button.isHidden())

    def test_cancel_while_downloading(self):
        w = self.window()
        page = self.show(w, "updates")
        self.blocking(("apt-refresh",))
        page.check_button.click()
        self.assertTrue(pump(lambda: page.panel.cancel_button.isEnabled()))
        self.assertFalse(page.check_button.isEnabled())
        self.assertFalse(page.install_button.isEnabled())
        self.assertIn("Another operation is running", page.install_button.toolTip())
        page.panel.cancel_button.click()
        self.assertTrue(pump(lambda: not w.jobs.busy()))
        self.settle(w)
        self.assertEqual(page.panel.result.text.text(), "Cancelled.")
        self.assertTrue(page.panel.cancel_button.isHidden())
        self.assertTrue(page.check_button.isEnabled())

    def test_no_cancel_while_installing_and_one_operation_at_a_time(self):
        w = self.window()
        page = self.show(w, "updates")
        gate = self.blocking(("apt-upgrade",), cancellable_phase=False)
        page.install_button.click()
        self.assertTrue(pump(lambda: w.jobs.current is not None and w.jobs.current.state.phase == "install"))
        self.assertFalse(page.panel.cancel_button.isEnabled())
        self.assertIn("cannot be cancelled", page.panel.cancel_button.toolTip())
        hardware = w.page("hardware")
        w.show_page("hardware")
        QCoreApplication.processEvents()
        self.assertFalse(hardware.check_button.isEnabled())
        self.assertFalse(w.jobs.start(jobs.JobSpec(jobs.SYSOPS, ("firmware-report",), "x", "hardware")))
        self.assertEqual(len(self.cmd.streamed), 1)
        # Closing now must not interrupt dpkg: the window stays open, and closes when the operation ends only
        # if the user asked for that.
        self.answer = False
        self.assertFalse(w.close())
        self.assertFalse(w.close_when_done)
        self.answer = True
        self.assertFalse(w.close())
        self.assertTrue(w.close_when_done)
        gate.set()
        self.assertTrue(pump(lambda: not w.jobs.busy()))
        self.settle(w)
        self.assertEqual(page.panel.result.level, "ok")


class AndroidPageTest(FunctionalCase):
    def test_ready_runtime(self):
        w = self.window()
        page = self.show(w, "android")
        self.settle(w)
        self.assertEqual(page.summary.headline.text(), "Android is ready")
        self.assertTrue(page.install_button.isHidden())
        self.assertFalse(page.start_button.isEnabled())          # the service is already running
        self.assertTrue(page.stop_button.isEnabled())
        self.assertEqual(page.apps.count(), 1)
        self.assertEqual(page.play_text.text(), fx.ANDROID_STATUS["google_play"]["reason"])
        self.assertFalse(page.fdroid_button.isHidden())
        self.assertIn("not Google Play", page.fdroid_button.text())
        self.assertEqual(len(page.compat_lines), 5)               # three checks and two separators
        self.assertFalse(page.launch_button.isEnabled())
        self.assertIn("Select an app", page.launch_button.toolTip())

    def test_not_installed(self):
        self.cmd.outputs[(E["boswas-android"], "--json", "status")] = fx.completed(
            json.dumps(fx.ANDROID_STATUS_NOT_INSTALLED))
        w = self.window()
        page = self.show(w, "android")
        self.assertTrue(page.install_button.isEnabled())
        self.assertFalse(page.start_button.isEnabled())
        self.assertIn("Install and initialize", page.start_button.toolTip())
        self.assertFalse(page.apk_button.isEnabled())
        self.assertNotIn([E["boswas-android"], "--json", "app", "list"], self.cmd.ran)
        self.script(("runtime", "install"), fx.sysops_lines("android-runtime-install", message="Installed."), kind="a")
        page.install_button.click()
        self.settle(w)
        self.assertIn("Google Play is not installed", self.questions[-1][1])
        self.assertEqual(self.cmd.streamed[-1], commands.android_argv("runtime", "install"))
        self.assertEqual(page.panel.result.level, "ok")

    def test_unsupported_hardware_disables_installation(self):
        self.cmd.outputs[(E["boswas-android"], "--json", "status")] = fx.completed(
            json.dumps(fx.ANDROID_STATUS_UNSUPPORTED))
        page = self.show(self.window(), "android")
        self.assertFalse(page.install_button.isEnabled())
        self.assertIn("no binder support", page.install_button.toolTip())
        self.assertEqual(page.summary.headline.text(), "This device cannot run Android applications")

    def test_manager_missing(self):
        self.cmd.installed.discard(E["boswas-android"])
        page = self.show(self.window(), "android")
        self.assertEqual(page.summary.detail.text(), android.NOT_INSTALLED_MANAGER)
        self.assertTrue(page.runtime.isHidden())
        self.assertFalse(page.install_button.isEnabled())

    def test_apk_is_inspected_then_installed(self):
        w = self.window()
        page = self.show(w, "android")
        self.chosen = self.apk_file()
        self.cmd.outputs[(E["boswas-android"], "--json", "apk", "inspect", self.chosen)] = \
            fx.completed(json.dumps(fx.APK_DOC))
        self.script(("apk", "install", self.chosen), [json.dumps({"schema": "boswas-android/1", "ok": True,
                                                                  "message": "Example Notes installed."})], kind="a")
        page.apk_button.click()
        self.settle(w)
        title, text, confirm = self.questions[-1]
        self.assertEqual(title, "Install Example Notes?")
        self.assertIn(android.INSPECTION_NOTE, text)
        self.assertIn("cd" * 32, text)
        self.assertEqual(self.cmd.streamed[-1], commands.android_argv("apk", "install", self.chosen))
        self.assertEqual(page.panel.result.level, "ok")

    def test_arm_only_apk_is_refused_without_asking(self):
        w = self.window()
        page = self.show(w, "android")
        self.chosen = self.apk_file("game.apk")
        self.cmd.outputs[(E["boswas-android"], "--json", "apk", "inspect", self.chosen)] = \
            fx.completed(json.dumps(fx.APK_ARM_ONLY))
        page.apk_button.click()
        self.settle(w)
        self.assertEqual(self.questions, [])
        self.assertEqual(self.cmd.streamed, [])
        self.assertIn("ARM translation layer", page.message.text.text())

    def test_fdroid_and_service(self):
        w = self.window()
        page = self.show(w, "android")
        self.script(("fdroid", "install"), fx.sysops_lines("fdroid"), kind="a")
        page.fdroid_button.click()
        self.settle(w)
        self.assertIn("not Google Play", self.questions[-1][1])
        self.assertEqual(self.cmd.streamed[-1], commands.android_argv("fdroid", "install"))
        self.script(("service", "stop"), fx.sysops_lines("android-service"), kind="a")
        page.stop_button.click()
        self.settle(w)
        self.assertEqual(self.cmd.streamed[-1], commands.android_argv("service", "stop"))


class SoftwareCenterTest(FunctionalCase):
    def test_tabs(self):
        page = self.show(self.window(), "applications")
        self.assertEqual([page.tabs.tabText(i) for i in range(page.tabs.count())],
                         ["Installed", "Linux Packages", "Flatpak", "AppImages", "Install from File"])

    def test_debian_package_search_and_install(self):
        w = self.window()
        page = self.show(w, "applications")
        tab = page.linux_tab
        page.show_tab("linux")
        tab.search.setText("vlc")
        tab.search_button.click()
        self.settle(w)
        self.assertEqual(tab.results.count(), 2)
        tab.results.setCurrentRow(0)
        self.settle(w)
        self.assertEqual(tab.grid.value("Available"), "3.0.21-10")
        self.assertEqual(tab.grid.value("Source"), "Debian (signed repository)")
        self.assertTrue(tab.install_button.isEnabled())
        self.assertFalse(tab.remove_button.isEnabled())
        self.script(("apt-install", "vlc"), fx.sysops_lines("apt-install", message="vlc was installed."))
        tab.install_button.click()
        self.settle(w)
        self.assertEqual(self.cmd.streamed, [commands.sysops_argv("apt-install", "vlc")])
        self.assertEqual(page.panel.result.text.text(), "vlc was installed.")

    def test_flatpak_tab(self):
        w = self.window()
        page = self.show(w, "applications")
        tab = page.flatpak_tab
        page.show_tab("flatpak")
        self.assertIn("flathub (for everyone)", tab.remotes.text())
        self.assertTrue(tab.enable_button.isHidden())            # already enabled by the administrator
        self.assertEqual(tab.installed.count(), 1)

    def test_appimage(self):
        w = self.window()
        page = self.show(w, "applications")
        image = Path(self.tmp.name) / "Tool-1.0-x86_64.AppImage"
        image.write_bytes(b"\x7fELF\x02\x01\x01\x00AI\x02" + b"\0" * 32)
        self.chosen = str(image)
        page.show_tab("appimage")
        page.appimage_tab.add_button.click()
        self.settle(w)
        self.assertIn(APPIMAGE_WARNING, self.questions[-1][1])
        self.assertEqual(page.appimage_tab.list.count(), 1)
        home = Path(self.rig.environ["HOME"])
        self.assertTrue((home / "Applications/tool-1-0-x86-64.AppImage").exists())
        page.appimage_tab.list.setCurrentRow(0)
        page.appimage_tab.remove_button.click()
        self.settle(w)
        self.assertFalse((home / "Applications/tool-1-0-x86-64.AppImage").exists())

    def test_install_from_file_routes_by_content(self):
        w = self.window()
        page = self.show(w, "applications")
        tab = page.file_tab
        deb = Path(self.tmp.name) / "tool.deb"
        deb.write_bytes(b"!<arch>\ndebian-binary   1342943816  0     0     100644  4         `\n2.0\n")
        self.script(("apt-install-local", str(deb)), fx.sysops_lines("apt-install-local"))
        tab.open_file(str(deb))
        self.settle(w)
        self.assertIn(LOCAL_DEB_WARNING, self.questions[-1][1])
        self.assertEqual(self.cmd.streamed[-1], commands.sysops_argv("apt-install-local", str(deb)))
        exe = Path(self.tmp.name) / "setup.bin"
        exe.write_bytes(b"MZ" + b"\0" * 64)
        tab.open_file(str(exe))
        self.settle(w)
        self.assertEqual(self.cmd.started[-1], [E["boswas-compat-manager"], "--install", str(exe)])
        apk = self.apk_file()
        self.cmd.outputs[(E["boswas-android"], "--json", "apk", "inspect", apk)] = fx.completed(json.dumps(fx.APK_DOC))
        self.answer = False
        tab.open_file(apk)
        self.settle(w)
        self.assertEqual(w.current_page(), "android")
        self.assertEqual(self.questions[-1][0], "Install Example Notes?")
        text = Path(self.tmp.name) / "notes.txt"
        text.write_text("hello")
        tab.open_file(str(text))
        self.assertIn("does not recognise", tab.message.text.text())
        renamed = Path(self.tmp.name) / "package"
        renamed.write_bytes(deb.read_bytes())
        tab.open_file(str(renamed))
        self.assertIn("does not end in .deb", tab.message.text.text())


class HardwarePageTest(FunctionalCase):
    def test_devices_and_firmware(self):
        w = self.window()
        page = self.show(w, "hardware")
        self.assertEqual(page.tree.topLevelItemCount(), 2)
        self.assertEqual(page.summary.icon.state, "warning")      # the Ethernet controller has no driver
        self.assertIn("RTL8125", page.summary.detail.text())
        data = {"source": "journal", "missing": [{"file": "rtl_nic/rtl8125b-2.fw", "driver": "r8169",
                                                  "device": "0000:03:00.0", "package": "firmware-realtek",
                                                  "installed": False, "candidate": "20250410-2"}]}
        self.script(("firmware-report",), fx.sysops_lines("firmware-report", data=data, progress=()))
        page.check_button.click()
        self.settle(w)
        self.assertEqual(self.questions, [])                      # read-only: no confirmation needed
        self.assertEqual(page.firmware_headline.text(), "1 firmware file is missing")
        button = page.install_buttons["firmware-realtek"]
        self.script(("apt-install", "firmware-realtek"), fx.sysops_lines("apt-install"))
        button.click()
        self.settle(w)
        self.assertEqual(self.cmd.streamed[-1], commands.sysops_argv("apt-install", "firmware-realtek"))
        self.assertEqual(page.install_buttons, {})


class StorageAndNetworkTest(FunctionalCase):
    def test_storage(self):
        w = self.window()
        page = self.show(w, "storage")
        self.assertIn("Example NVMe SSD", page.devices_text.text())
        self.assertIn("mounted at /", page.devices_text.text())
        self.script(("apt-clean",), fx.sysops_lines("apt-clean", data={"freed_bytes": 1048576}))
        page.apt_button.click()
        self.settle(w)
        self.assertEqual(page.cleanup_message.text.text(), "1.0 MiB freed.")
        thumbs = Path(self.rig.environ["HOME"]) / ".cache/thumbnails/normal"
        thumbs.mkdir(parents=True)
        (thumbs / "a.png").write_bytes(b"x" * 2048)
        page.refresh()
        self.settle(w)
        self.assertTrue(page.thumbs_button.isEnabled())
        page.thumbs_button.click()
        self.settle(w)
        self.assertFalse((thumbs / "a.png").exists())
        self.assertIn("2.0 KiB freed", page.cleanup_message.text.text())

    def test_network(self):
        w = self.window()
        page = self.show(w, "network")
        self.assertEqual(page.summary.headline.text(), "Connected (Wi-Fi: Home:Net)")
        self.assertEqual(sorted(page.device_buttons), ["wlp2s0"])  # the unavailable device offers no button
        self.assertFalse(page.hardware_button.isHidden())
        page.wifi_button.click()
        self.settle(w)
        self.assertIn([E["nmcli"], "radio", "wifi", "off"], self.cmd.ran)
        page.hardware_button.click()
        self.settle(w)
        self.assertEqual(w.current_page(), "hardware")

    def test_overview(self):
        page = self.show(self.window(), "about")
        rows = dict(page.overview_rows)
        self.assertIn("Memory in use", rows)
        self.assertTrue(rows["Network"].startswith("Connected"))
        self.assertIn("Not checked", rows["Pending updates"])
        self.assertEqual(rows["Android apps"], "Android is ready")


if __name__ == "__main__":
    unittest.main()
