"""Widget tests of the Compatibility Manager (Qt offscreen platform, FakeBackend).

Every backend call still runs on the Runner's thread pool; the tests wait for
delivery with Runner.wait() and drive polling slots directly instead of
sleeping. Modal dialogs are answered through the window's replaceable hooks
(dialog_exec, ask, notify_error, open_dialog, save_dialog).

Run: QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))

import manager_fixtures as fx  # noqa: E402

try:
    from PySide6.QtCore import QCoreApplication, QEvent, Qt, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QApplication, QComboBox,
                                   QDialog, QLabel, QLineEdit, QPlainTextEdit, QTextEdit)
    PYSIDE = True
except ImportError:                       # PySide6 is not a build dependency: skip the widget tests
    PYSIDE = False

if PYSIDE:
    from boswas_manager import viewmodel as vm
    from boswas_manager.backend import BackendRefused
    from boswas_manager.ui.install import InstallDialog
    from boswas_manager.ui.main_window import MainWindow

    APP = QApplication.instance() or QApplication(["test"])

    def _quiet_offscreen(_mode, _context, message):
        # The offscreen platform cannot raise or resize windows; everything else is still reported.
        if "This plugin does not support" not in message:
            sys.stderr.write(message + "\n")

    qInstallMessageHandler(_quiet_offscreen)

FORBIDDEN_WORDS = ("wine32", "i386", "wow64", "multiarch")


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class UiTestCase(unittest.TestCase):
    def setUp(self):
        self.errors: list[str] = []
        self._excepthook = sys.excepthook
        sys.excepthook = lambda *exc: self.errors.append("".join(traceback.format_exception(*exc)))
        self.backend = fx.FakeBackend()
        self.windows: list = []

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        sys.excepthook = self._excepthook
        if self.errors:
            self.fail("exceptions in Qt callbacks:\n" + "\n".join(self.errors))

    def window(self, environ=None) -> "MainWindow":
        window = MainWindow(self.backend, environ=environ or {}, refresh_ms=600_000)
        # Timers stay active (tests check that) but never fire on their own: the tests poll explicitly.
        window.job_timer.setInterval(3_600_000)
        window.install_page.poll_interval = 3_600_000
        self.windows.append(window)
        self.settle(window)
        return window

    @staticmethod
    def settle(window) -> None:
        if not window.runner.wait(10):
            raise AssertionError("backend calls did not finish")

    @staticmethod
    def label_texts(widget) -> list[str]:
        return [label.text() for label in widget.findChildren(QLabel) if label.text()]


class MainWindowTest(UiTestCase):
    def test_builds_with_all_pages(self):
        w = self.window()
        self.assertEqual([w.sidebar.item(i).text() for i in range(w.sidebar.count())],
                         ["Dashboard", "Applications", "Install", "System Status"])
        self.assertEqual(w.library.model.rowCount(), len(fx.APPS))
        self.assertEqual({k: c.value.text() for k, c in w.dashboard.counts.items()},
                         {"installed": "9", "running": "1", "attention": "5", "updates": "1"})
        self.assertEqual(w.device_label.text(), "Device: Ready")
        self.assertEqual(w.plane_label.text(), "Control Plane: Connected")
        self.assertEqual(w.dashboard.status_text("wine"), "Healthy (wine-10.0)")
        self.assertEqual(w.dashboard.status_text("policy"), "Managed: 2026.10.1")
        self.assertTrue(any(t.endswith("requested by your administrator") for t in w.dashboard.activity_texts()))
        self.assertTrue(w.session_banner.isHidden())
        self.assertEqual(w.system_page.value("windows"), "x86_64 / 64-bit only")
        self.assertEqual(w.system_page.value("apparmor"), "Enforced")
        self.assertEqual(w.system_page.value("agent"), "Running (Ready)")

    def test_filters_and_search(self):
        lib = self.window().library
        lib.set_filter("running")
        self.assertEqual(lib.visible_ids(), ["com.example.paint"])
        lib.set_filter("repair")
        self.assertEqual(set(lib.visible_ids()), {"com.example.broken", "com.example.failed", "local.damaged"})
        lib.set_filter("updates")
        self.assertEqual(lib.visible_ids(), ["com.example.notepad"])
        lib.set_filter("all")
        lib.search.setText("notepad")
        self.assertEqual(lib.visible_ids(), ["com.example.notepad"])
        lib.search.setText("nothing like this")
        self.assertEqual(lib.visible_ids(), [])
        self.assertIs(lib.stack.currentWidget(), lib.empty)
        self.assertEqual(lib.empty.text(), "No applications match your search.")
        tab_texts = [lib.tabs.tabText(i) for i in range(lib.tabs.count())]
        self.assertIn("Running (1)", tab_texts)
        self.assertEqual(tab_texts[0], "All Applications")

    def test_action_buttons_follow_the_state(self):
        lib = self.window().library
        expected = {
            "com.example.notepad": {"launch", "repair", "update", "remove", "logs", "details"},
            "com.example.paint": {"stop", "logs", "details"},
            "com.example.blocked": {"remove", "logs", "details"},
            "com.example.old": {"remove", "logs", "details"},
            "com.example.broken": {"repair", "remove", "logs", "details"},
            "com.example.busy": {"logs", "details"},
            "com.example.installing": {"logs", "details"},
            "local.damaged": {"remove", "logs", "details"},
        }
        for app_id, enabled in expected.items():
            self.assertTrue(lib.select_app(app_id), app_id)
            self.assertEqual({k for k, b in lib.buttons.items() if b.isEnabled()}, enabled, app_id)
        lib.table.clearSelection()
        self.assertFalse(any(b.isEnabled() for b in lib.buttons.values()))

    def test_launch_passes_the_session_display(self):
        env = {"DISPLAY": ":1", "XAUTHORITY": "/run/user/1000/xauth_Abc", "WAYLAND_DISPLAY": "wayland-0"}
        w = self.window(environ=env)
        w.library.select_app("com.example.notepad")
        w.library.buttons["launch"].click()
        self.settle(w)
        self.assertEqual(self.backend.called("launch"),
                         [(("com.example.notepad",), {"display": ":1", "xauthority": "/run/user/1000/xauth_Abc",
                                                      "wayland_display": "wayland-0"})])
        self.assertEqual(w.message.text.text(), "Example Notepad is starting.")

    def test_launch_refusal_is_shown(self):
        w = self.window()
        shown = []
        w.notify_error = lambda title, text: shown.append((title, text))
        self.backend.errors["launch"] = BackendRefused("POLICY", "com.example.notepad may not be started: blocked")
        w.library.select_app("com.example.notepad")
        w.library.buttons["launch"].click()
        self.settle(w)
        self.assertEqual(shown, [("Example Notepad could not be started",
                                  "com.example.notepad may not be started: blocked")])

    def test_stop_asks_first(self):
        w = self.window()
        questions = []
        w.ask = lambda title, text, confirm: questions.append((title, text, confirm)) or False
        w.library.select_app("com.example.paint")
        w.library.buttons["stop"].click()
        self.settle(w)
        self.assertEqual(self.backend.called("stop"), [])
        self.assertEqual(questions, [("Stop Example Paint?", vm.STOP_WARNING, "Stop")])
        w.ask = lambda *_args: True
        w.library.buttons["stop"].click()
        self.settle(w)
        self.assertEqual(self.backend.called("stop"), [(("com.example.paint",), {})])

    def test_repair_starts_a_job_and_reports_it(self):
        w = self.window()
        self.backend.job_sequence = [{"op": "repair", "state": "running", "progress": []},
                                     {"op": "repair", "state": "succeeded", "result": {"healthy": True}}]
        w.library.select_app("com.example.broken")
        w.library.buttons["repair"].click()
        self.settle(w)
        self.assertEqual(self.backend.called("repair"), [(("com.example.broken",), {})])
        self.assertTrue(w.job_timer.isActive())
        w.poll_jobs()
        self.settle(w)
        self.assertIn("1111111111111111", w.jobs)
        w.poll_jobs()
        self.settle(w)
        self.assertEqual(w.jobs, {})
        self.assertEqual(w.message.text.text(), "Broken Editor was repaired.")

    def test_untrusted_names_stay_plain_text(self):
        self.backend.apps = [fx.app("com.example.html", "<b>Bold</b>\x1b[31m<img src=x>\x9b1m", "INSTALLED")]
        w = self.window()
        index = w.library.proxy.index(0, 0)
        self.assertEqual(index.data(), "<b>Bold</b><img src=x>")
        dialog = w.open_details("com.example.html")
        self.settle(w)
        self.assertEqual(dialog.title.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(dialog.title.text(), "<b>Bold</b><img src=x>")

    def test_every_label_is_plain_text(self):
        w = self.window()
        dialog = w.open_details("com.example.notepad", "permissions")
        self.settle(w)
        w.install_page.check_file("/x/viewer-setup.exe")
        self.settle(w)
        labels = [label for label in w.findChildren(QLabel) + dialog.findChildren(QLabel) if label.text()]
        self.assertGreater(len(labels), 40)
        for label in labels:
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText, label.text())


class InstallTest(UiTestCase):
    def test_32bit_installer_shows_the_fixed_message_and_never_installs(self):
        self.backend.inspection = fx.INSPECT_32
        w = self.window()
        w.open_install("/home/u/Downloads/setup32.exe")
        self.settle(w)
        page = w.install_page
        self.assertEqual(w.current_page(), "install")
        self.assertEqual(page.current_page(), "refused_32bit")
        self.assertEqual(page.r32_title.text(), "This application requires 32-bit Windows compatibility, which is "
                                                "not supported by Boswas OS.")
        self.assertEqual(page.r32_detail.text(),
                         "32-bit Windows applications are not supported by this version of Boswas OS.")
        self.assertEqual(self.backend.called("inspect"), [(("/home/u/Downloads/setup32.exe", None, None), {})])
        page.start_install()
        self.settle(w)
        self.assertEqual(self.backend.called("install"), [])
        for text in self.label_texts(page.pages["refused_32bit"]):
            for word in FORBIDDEN_WORDS:
                self.assertNotIn(word, text.casefold())

    def test_32bit_update_is_refused_too(self):
        self.backend.inspection = fx.INSPECT_32
        w = self.window()
        dialog = w.update_app("com.example.notepad")
        flow = dialog.flow_widget
        flow.open_dialog = lambda *_args: ("/x/setup32.exe", "")
        flow.choose_file()
        self.settle(w)
        self.assertEqual(flow.current_page(), "refused_32bit")
        self.assertEqual(flow.r32_title.text(), vm.UNSUPPORTED_32BIT_MESSAGE)
        self.assertEqual(self.backend.called("upgrade"), [])
        dialog.close()

    def test_valid_installer_installs_with_progress(self):
        w = self.window()
        flow = w.install_page
        w.show_page("install")
        flow.open_dialog = lambda *_args: ("/home/u/viewer-setup.exe", "")
        flow.choose_button.click()
        self.settle(w)
        self.assertEqual(flow.current_page(), "confirm")
        self.assertEqual(flow.confirm_title.text(), "Install Example Viewer")
        texts = self.label_texts(flow.pages["confirm"])
        self.assertIn("x86_64 (64-bit)", texts)
        self.assertIn(vm.REMOVABLE_DEVICES, texts)
        self.assertIn(vm.PERMISSIONS_NOTICE, texts)
        flow.install_button.click()
        self.settle(w)
        self.assertEqual(self.backend.called("install"), [(("/home/u/viewer-setup.exe", None, None), {})])
        self.assertEqual(flow.current_page(), "installing")
        self.assertTrue(flow.poll_timer.isActive())
        flow.poll_job()
        self.settle(w)
        self.assertEqual(flow.current_page(), "installing")
        self.assertIn("creating the Wine prefix", flow.progress_view.toPlainText())
        self.assertTrue(flow.poll_timer.isActive())
        flow.poll_job()
        self.settle(w)
        self.assertEqual(flow.current_page(), "done")
        self.assertEqual(flow.done_title.text(), "Example Viewer is installed.")
        self.assertTrue(flow.launch_button.isEnabled())
        self.assertEqual(len(self.backend.called("job")), 2)
        flow.launch_button.click()
        self.settle(w)
        self.assertEqual(self.backend.called("launch")[-1][0], ("com.example.viewer",))

    def test_failed_install_offers_the_log(self):
        self.backend.job_sequence = [fx.JOB_FAILED]
        w = self.window()
        flow = w.install_page
        flow.check_file("/x/viewer-setup.exe")
        self.settle(w)
        flow.start_install()
        self.settle(w)
        flow.poll_job()
        self.settle(w)
        self.assertEqual(flow.current_page(), "failed")
        self.assertIn("exited with code 2", flow.failed_message.text())
        self.assertNotIn("/home/", flow.failed_message.text())
        self.assertFalse(flow.log_button.isHidden())
        flow.log_button.click()
        self.settle(w)
        dialog = w.details["com.example.viewer"]
        self.assertEqual(dialog.current_kind, "install")

    def test_refusal_pages_show_the_backend_message(self):
        self.backend.inspection = fx.INSPECT_INSTALLED
        w = self.window()
        flow = w.install_page
        flow.check_file("/x/notepad.exe")
        self.settle(w)
        self.assertEqual(flow.current_page(), "refused")
        self.assertEqual(flow.refused_title.text(), "This application is already installed")
        self.assertEqual(flow.refused_message.text(), fx.INSPECT_INSTALLED["decision"]["message"])
        self.assertFalse(flow.open_apps_button.isHidden())
        self.assertEqual(self.backend.called("install"), [])

    def test_install_refused_by_the_service(self):
        self.backend.errors["install"] = BackendRefused("BLOCKED", "this installer is blocked")
        w = self.window()
        flow = w.install_page
        flow.check_file("/x/viewer-setup.exe")
        self.settle(w)
        flow.install_button.click()
        self.settle(w)
        self.assertEqual(flow.current_page(), "refused")
        self.assertEqual(flow.refused_title.text(), "This installer is blocked")

    def test_update(self):
        self.backend.inspection = fx.INSPECT_INSTALLED
        w = self.window()
        dialog = w.update_app("com.example.notepad")
        flow = dialog.flow_widget
        flow.poll_interval = 3_600_000
        self.assertTrue(flow.advanced.isHidden())
        flow.check_file("/x/notepad-2.2.exe")
        self.settle(w)
        self.assertEqual(self.backend.called("inspect")[-1][0], ("/x/notepad-2.2.exe", "com.example.notepad", None))
        self.assertEqual(flow.current_page(), "confirm")
        self.assertEqual(flow.install_button.text(), "Update")
        flow.install_button.click()
        self.settle(w)
        self.assertEqual(self.backend.called("upgrade"), [(("com.example.notepad", "/x/notepad-2.2.exe"), {})])
        dialog.close()

    def test_standalone_install_dialog(self):
        from boswas_manager.ui.worker import Runner
        runner = Runner()
        dialog = InstallDialog(self.backend, runner)
        try:
            self.backend.inspection = fx.INSPECT_32
            dialog.flow_widget.check_file("/x/setup32.exe")
            self.assertTrue(runner.wait(10))
            self.assertEqual(dialog.flow_widget.current_page(), "refused_32bit")
        finally:
            runner.shutdown()
            dialog.deleteLater()


class RemoveTest(UiTestCase):
    def test_remove_asks_and_lists_what_is_deleted(self):
        w = self.window()
        seen = []

        def answer(result):
            def exec_(dialog):
                seen.append((dialog.text(), dialog.remove_button.text(), dialog.cancel_button.isDefault()))
                return result
            return exec_

        w.library.select_app("com.example.blocked")
        w.dialog_exec = answer(QDialog.DialogCode.Rejected)
        w.library.buttons["remove"].click()
        self.settle(w)
        self.assertEqual(self.backend.called("remove"), [])
        text, button_text, cancel_default = seen[0]
        self.assertIn("Remove Blocked Game?", text)
        self.assertIn("Its Wine prefix (C: drive) and everything stored inside it", text)
        self.assertIn(vm.REMOVE_WARNING, text)
        self.assertEqual(button_text, "Remove")
        self.assertTrue(cancel_default)

        self.backend.job_sequence = [{"op": "remove", "state": "succeeded", "result": {"removed": True}}]
        w.dialog_exec = answer(QDialog.DialogCode.Accepted)
        w.library.buttons["remove"].click()
        self.settle(w)
        self.assertEqual(self.backend.called("remove"), [(("com.example.blocked",), {})])
        w.poll_jobs()
        self.settle(w)
        self.assertEqual(w.message.text.text(), "Blocked Game was removed.")

    def test_running_application_cannot_be_removed(self):
        w = self.window()
        w.dialog_exec = lambda _dialog: self.fail("no dialog expected")
        w.library.select_app("com.example.paint")
        self.assertFalse(w.library.buttons["remove"].isEnabled())
        w.library.buttons["remove"].click()
        self.settle(w)
        self.assertEqual(self.backend.called("remove"), [])


class DetailsTest(UiTestCase):

    def test_overview(self):
        w = self.window()
        dialog = w.open_details("com.example.notepad")
        self.settle(w)
        texts = self.label_texts(dialog)
        self.assertIn("~/.local/share/boswas/wine/com.example.notepad", texts)
        self.assertIn("Approved", texts)
        for text in texts:
            self.assertNotIn("/home/", text)

    def test_permissions_tab_is_read_only(self):
        w = self.window()
        dialog = w.open_details("com.example.notepad", "permissions")
        self.settle(w)
        page = dialog.permissions_page
        for kind in (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractButton, QComboBox, QAbstractSpinBox,
                     QAbstractSlider):
            self.assertEqual(page.findChildren(kind), [], kind.__name__)
        texts = self.label_texts(page)
        self.assertIn(vm.PERMISSIONS_NOTICE, texts)
        self.assertIn("Never available to Windows applications", texts)
        self.assertIn("Its own C: drive, and your Documents and Downloads folders", texts)
        self.assertIn("Blocked", texts)
        self.assertTrue(any(t.startswith("Enforced (the boswas-winapp profile)") for t in texts))

    def test_logs_are_sanitised(self):
        w = self.window()
        dialog = w.open_details("com.example.notepad", "logs")
        self.settle(w)
        self.assertEqual(self.backend.called("logs")[0][0], ("com.example.notepad", "latest"))
        text = dialog.log_content()
        self.assertEqual(text.splitlines(), ["fixme:ntdll:NtQuerySystemInformation", "err:module cleared line",
                                             "titledone", "plain line"])
        for ch in text:
            self.assertFalse((ord(ch) < 32 and ch not in "\n\t") or 0x7F <= ord(ch) <= 0x9F, repr(ch))
        self.assertEqual(dialog.log_file.text(), "launch-20261003.log")
        self.assertTrue(dialog.log_view.isReadOnly())

        dialog.copy_button.click()
        self.assertEqual(QGuiApplication.clipboard().text(), text)
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "exported.log")
            dialog.save_dialog = lambda *_args: (target, "")
            dialog.save_button.click()
            with open(target, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), text + "\n")

        self.backend.errors["logs"] = BackendRefused("NOT_FOUND", "no repair logs for com.example.notepad")
        dialog.log_kind.setCurrentIndex(dialog.log_kind.findData("repair"))
        self.settle(w)
        self.assertEqual(self.backend.called("logs")[-1][0], ("com.example.notepad", "repair"))
        self.assertEqual(dialog.log_content(), "")
        self.assertEqual(dialog.log_view.placeholderText(), vm.LOG_MISSING["repair"])
        self.assertFalse(dialog.save_button.isEnabled())

    def test_reopening_switches_tab_and_loads_the_log_once(self):
        w = self.window()
        dialog = w.open_details("com.example.notepad")
        self.settle(w)
        self.assertEqual(self.backend.called("logs"), [])
        again = w.open_details("com.example.notepad", "logs", "install")
        self.settle(w)
        self.assertIs(again, dialog)
        self.assertEqual(dialog.tabs.currentIndex(), 2)
        self.assertEqual([args for args, _kwargs in self.backend.called("logs")], [("com.example.notepad", "install")])
        self.assertEqual(len(self.backend.called("app_status")), 2)

    def test_clear_logs_needs_confirmation_and_a_stopped_application(self):
        w = self.window()
        running = w.open_details("com.example.paint", "logs")
        self.settle(w)
        self.assertFalse(running.clear_button.isEnabled())
        dialog = w.open_details("com.example.notepad", "logs")
        self.settle(w)
        self.assertTrue(dialog.clear_button.isEnabled())
        dialog.ask = lambda *_args: False
        dialog.clear_button.click()
        self.settle(w)
        self.assertEqual(self.backend.called("clear_logs"), [])
        dialog.ask = lambda *_args: True
        dialog.clear_button.click()
        self.settle(w)
        self.assertEqual(self.backend.called("clear_logs"), [(("com.example.notepad",), {})])


class AvailabilityTest(UiTestCase):
    def test_without_the_device_agent(self):
        self.backend.errors["agent_status"] = fx.unavailable("agent")
        self.backend.errors["remote_commands"] = fx.unavailable("agent")
        w = self.window()
        self.assertEqual(w.system_page.value("agent"), "Not running")
        self.assertEqual(w.system_page.value("control_plane"), "Unknown")
        self.assertEqual(w.device_label.text(), "Device agent: not running")
        self.assertEqual(w.library.model.rowCount(), len(fx.APPS))       # applications keep working
        self.assertTrue(w.session_banner.isHidden())

    def test_without_the_session_agent(self):
        for method in ("list_apps", "events", "catalog", "system_status"):
            self.backend.errors[method] = fx.unavailable()
        w = self.window()
        self.assertFalse(w.session_banner.isHidden())
        self.assertEqual(w.session_banner.text.text(), vm.SESSION_UNAVAILABLE)
        self.assertTrue(vm.SESSION_UNAVAILABLE.startswith("The Boswas session service is not running"))
        self.assertFalse(w.library.isEnabled())
        self.assertFalse(w.install_page.isEnabled())
        self.assertEqual(w.system_page.value("agent"), "Running (Ready)")
        self.assertEqual(w.system_page.value("wine"), "Unknown")
        self.backend.errors.clear()
        w.refresh(full=True)
        self.settle(w)
        self.assertTrue(w.session_banner.isHidden())
        self.assertTrue(w.library.isEnabled())


if __name__ == "__main__":
    unittest.main()
