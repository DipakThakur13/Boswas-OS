"""app.py, worker.py, desktop.py, shortcuts.py, testing.py and the gallery (Qt, offscreen)."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import support

if support.PYSIDE:
    from PySide6.QtCore import QThread, QTimer
    from PySide6.QtGui import QGuiApplication, QKeySequence
    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QWidget

    import boswas_fluent as fluent
    from boswas_fluent import app as fluent_app
    from boswas_fluent import desktop, shortcuts, testing

    Base = testing.FluentTestCase
else:
    Base = unittest.TestCase

APP = "com.boswas.Example"


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class SingleInstanceTest(Base):
    def setUp(self):
        super().setUp()
        # Unix socket paths are short: a directory of our own directly under /tmp.
        self.sockets = tempfile.mkdtemp(prefix="bf-", dir="/tmp")
        os.chmod(self.sockets, 0o700)
        self.addCleanup(shutil.rmtree, self.sockets, True)

    def instance(self) -> "fluent.SingleInstance":
        instance = fluent.SingleInstance(APP, directory=self.sockets)
        self.addCleanup(instance.close)
        return instance

    def test_the_second_start_hands_its_arguments_to_the_first(self):
        first = self.instance()
        received = []
        first.message_received.connect(received.append)
        self.assertTrue(first.start(["--page", "one"]))
        self.assertTrue(first.is_listening())
        path = first.socket_path()
        self.assertEqual(path, Path(self.sockets) / f"{APP}.sock")
        self.assertEqual(os.stat(path).st_mode & 0o077, 0, "only the user can connect")
        result = {}

        def second_start() -> None:                    # a second process, played by a thread
            result["first"] = fluent.SingleInstance(APP, directory=self.sockets).start(["file one.txt", "--flag"])

        thread = threading.Thread(target=second_start)
        thread.start()
        self.assertTrue(testing.wait_until(lambda: bool(received), 5))
        thread.join(5)
        self.assertEqual(result, {"first": False})
        self.assertEqual(received, [["file one.txt", "--flag"]])

    def test_the_activation_token_is_passed_on(self):
        first = self.instance()
        received = []
        first.message_received.connect(received.append)
        first.start([])
        os.environ["XDG_ACTIVATION_TOKEN"] = "token-123"
        self.addCleanup(os.environ.pop, "XDG_ACTIVATION_TOKEN", None)
        thread = threading.Thread(target=lambda: fluent.SingleInstance(APP, directory=self.sockets).start([]))
        thread.start()
        self.assertTrue(testing.wait_until(lambda: bool(received), 5))
        thread.join(5)
        self.assertEqual(first.activation_token, "token-123")

    def test_a_stale_socket_is_replaced(self):
        path = Path(self.sockets) / f"{APP}.sock"
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(path))
        stale.close()                                  # the file stays, nobody listens
        self.assertTrue(path.exists())
        instance = self.instance()
        self.assertTrue(instance.start([]))
        self.assertTrue(instance.is_listening())
        instance.close()
        self.assertFalse(instance.is_listening())

    def test_malformed_messages_are_dropped(self):
        instance = self.instance()
        received = []
        instance.message_received.connect(received.append)
        instance.start([])
        path = str(instance.socket_path())
        answers = []

        def send(data: bytes) -> None:
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(2)
            client.connect(path)
            client.sendall(data)
            try:
                answers.append(client.recv(16))
            except OSError:
                answers.append(b"")
            client.close()

        for data in (b"not json\n", b'{"v": 2, "args": []}\n', b'{"v": 1, "args": "x"}\n', b'{"v": 1, "args": [1, 2]}\n',
                     b'{"v": 1, "args": ["a\\u0000b"]}\n', b'["v"]\n', b"\xff\xfe\n", b"x" * 70000 + b"\n",
                     b'{"v": 1, "args": ["ok"]}\n'):
            thread = threading.Thread(target=send, args=(data,))
            thread.start()
            deadline = time.monotonic() + 5
            while thread.is_alive() and time.monotonic() < deadline:
                testing.process(5)
            thread.join(1)
        self.assertEqual(received, [["ok"]])
        self.assertEqual(answers[-1], b"ok\n")
        self.assertTrue(all(answer == b"" for answer in answers[:-1]), answers)

    def test_without_a_private_runtime_directory_every_start_is_its_own(self):
        os.environ.pop("XDG_RUNTIME_DIR")
        instance = fluent.SingleInstance(APP)
        self.assertIsNone(instance.socket_path())
        self.assertTrue(instance.start(["x"]))
        self.assertFalse(instance.is_listening())
        shared = self.tmp / "shared"
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        loose = fluent.SingleInstance(APP, directory=shared)
        self.assertTrue(loose.start([]))
        self.assertFalse(loose.is_listening(), "a directory others can enter is not trusted")
        with self.assertRaises(ValueError):
            fluent.SingleInstance("../x")

    def test_runtime_dir(self):
        self.assertEqual(desktop.runtime_dir(), Path(os.environ["XDG_RUNTIME_DIR"]))
        os.chmod(os.environ["XDG_RUNTIME_DIR"], 0o755)
        self.assertIsNone(desktop.runtime_dir(), "group or world access: not private")
        os.environ["XDG_RUNTIME_DIR"] = "relative"
        self.assertIsNone(desktop.runtime_dir())


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class RunTest(Base):
    def setUp(self):
        super().setUp()
        # run() refuses to work as root; these tests are about everything else, whoever runs them.
        original = os.geteuid
        os.geteuid = lambda: 1000
        self.addCleanup(setattr, os, "geteuid", original)

    def tearDown(self):
        QGuiApplication.setDesktopFileName("")
        super().tearDown()

    def test_run_builds_the_window_and_returns_the_exit_code(self):
        seen = {}

        def factory(arguments):
            seen["arguments"] = arguments
            seen["applied"] = fluent.manager().applied
            seen["window"] = QLabel("window")
            QTimer.singleShot(30, lambda: QApplication.instance().exit(7))
            return seen["window"]

        code = fluent.run(APP, factory, argv=["example", "--open", "a.txt"], display_name="Example", version="1.2")
        self.assertEqual(code, 7)
        self.assertEqual(seen["arguments"], ["--open", "a.txt"])
        self.assertEqual(QGuiApplication.desktopFileName(), APP)
        self.assertEqual(self.app.applicationVersion(), "1.2")
        self.assertTrue(seen["applied"], "the theme is applied before the window is built")
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.default_int_handler, "handlers are put back")
        seen["window"].deleteLater()

    def test_a_second_run_raises_the_first_window_and_passes_its_arguments(self):
        handled, shown = [], []

        class Window(QLabel):
            def handle_arguments(self, arguments):
                handled.append(arguments)

        def factory(_arguments):
            window = Window("first")

            def start_second() -> None:
                def second() -> None:
                    other = fluent.SingleInstance(APP)
                    shown.append(other.start(["--page", "updates"]))
                thread = threading.Thread(target=second)
                thread.start()
                QTimer.singleShot(400, lambda: (thread.join(2), QApplication.instance().quit()))

            QTimer.singleShot(30, start_second)
            return window

        self.assertEqual(fluent.run(APP, factory, argv=["example"]), 0)
        self.assertEqual(shown, [False], "the second start found the first")
        self.assertEqual(handled, [["--page", "updates"]])

    def test_ctrl_c_quits(self):
        guard = QTimer()                               # ends the test if the signal is lost
        guard.setSingleShot(True)
        guard.timeout.connect(lambda: QApplication.instance().exit(99))

        def factory(_arguments):
            QTimer.singleShot(30, lambda: os.kill(os.getpid(), signal.SIGINT))
            guard.start(5000)
            return QLabel("window")

        started = time.monotonic()
        code = fluent.run(APP, factory, argv=["example"], single_instance=False)
        guard.stop()
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - started, 4)

    def test_never_as_root(self):
        os.geteuid = lambda: 0
        called = []
        stderr = sys.stderr
        sys.stderr = open(os.devnull, "w")
        try:
            code = fluent.run(APP, lambda _a: called.append(1) or QLabel(), argv=["example"])
        finally:
            sys.stderr.close()
            sys.stderr = stderr
        self.assertEqual((code, called), (fluent_app.EXIT_ROOT, []))
        with self.assertRaises(ValueError):
            fluent.run("not an id", lambda _a: QLabel(), argv=["x"])

    def test_activate_window(self):
        window = self.keep(QLabel("w"))
        window.showMinimized()
        fluent.activate_window(window, "tok")
        self.assertTrue(window.isVisible())
        self.assertFalse(window.isMinimized())
        self.assertEqual(os.environ.pop("XDG_ACTIVATION_TOKEN"), "tok")


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class WorkerTest(Base):
    def setUp(self):
        super().setUp()
        self.worker = fluent.Worker()
        self.addCleanup(self.worker.shutdown)

    def test_results_arrive_on_the_gui_thread(self):
        threads, results = [], []

        def work(a, b=0):
            threads.append(QThread.currentThread())
            return a + b

        def done(value):
            results.append((value, QThread.currentThread() is self.app.thread()))

        task = self.worker.submit(work, 2, b=3, on_done=done)
        self.assertIsInstance(task, int)
        self.assertEqual(self.worker.pending(), 1)
        self.assertTrue(self.worker.wait(5))
        self.assertEqual(results, [(5, True)])
        self.assertIsNot(threads[0], self.app.thread(), "the work ran elsewhere")
        self.assertEqual(self.worker.pending(), 0)

    def test_exceptions_go_to_on_error(self):
        errors, results = [], []

        def fail():
            raise OSError("disk on fire")

        self.worker.submit(fail, on_done=results.append, on_error=errors.append)
        self.worker.submit(fail)                              # nobody listens: nothing happens, nothing is raised
        self.worker.submit(lambda: 1 / 0, on_error=errors.append)
        self.assertTrue(self.worker.wait(5))
        self.assertEqual(results, [])
        self.assertEqual(sorted(type(e).__name__ for e in errors), ["OSError", "ZeroDivisionError"])
        self.assertEqual(str(next(e for e in errors if isinstance(e, OSError))), "disk on fire")
        self.assertEqual(self.callback_errors, [])

    def test_a_key_runs_once_at_a_time(self):
        release = threading.Event()
        results = []
        first = self.worker.submit(lambda: release.wait(5) and "first", key="refresh", on_done=results.append)
        second = self.worker.submit(lambda: "second", key="refresh", on_done=results.append)
        other = self.worker.submit(lambda: "other", key="else", on_done=results.append)
        self.assertIsNotNone(first)
        self.assertIsNone(second, "skipped while the first is running")
        self.assertIsNotNone(other)
        self.assertTrue(self.worker.busy("refresh"))
        release.set()
        self.assertTrue(self.worker.wait(5))
        self.assertEqual(sorted(results), ["first", "other"])
        self.assertFalse(self.worker.busy("refresh"))
        self.assertIsNotNone(self.worker.submit(lambda: "again", key="refresh", on_done=results.append))
        self.worker.wait(5)
        self.assertIn("again", results)

    def test_shutdown_and_discard_deliver_nothing(self):
        release = threading.Event()
        results = []
        task = self.worker.submit(lambda: release.wait(5) and "late", on_done=results.append)
        self.worker.discard(task)
        release.set()
        self.assertTrue(self.worker.wait(5))
        self.assertEqual(results, [])
        hold = threading.Event()
        self.worker.submit(lambda: hold.wait(5) and "never", on_done=results.append)
        hold.set()
        self.worker.shutdown()
        testing.process(30)
        self.assertEqual(results, [])
        self.assertIsNone(self.worker.submit(lambda: 1, on_done=results.append), "nothing starts after shutdown")

    def test_run_in_thread(self):
        results, errors = [], []
        fluent.run_in_thread(lambda: "value", results.append, errors.append)
        fluent.run_in_thread(lambda: [][1], results.append, errors.append)
        from boswas_fluent.worker import shared_worker
        self.assertTrue(shared_worker().wait(5))
        self.assertEqual(results, ["value"])
        self.assertIsInstance(errors[0], IndexError)


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class DesktopTest(Base):
    def test_directories_follow_xdg_and_are_private(self):
        for function, variable in ((fluent.config_dir, "XDG_CONFIG_HOME"), (fluent.state_dir, "XDG_STATE_HOME"),
                                   (fluent.cache_dir, "XDG_CACHE_HOME"), (fluent.data_dir, "XDG_DATA_HOME")):
            path = function(APP)
            self.assertEqual(path, Path(os.environ[variable]) / APP)
            self.assertTrue(path.is_dir())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertFalse(fluent.config_dir("com.boswas.Unused", create=False).exists())
        os.environ["XDG_CONFIG_HOME"] = "relative"
        self.assertEqual(fluent.config_dir(APP, create=False), Path.home() / ".config" / APP)
        for bad in ("", "..", "../x", "a/b", ".hidden", "x" * 200, "with space", None):
            with self.assertRaises(ValueError):
                fluent.config_dir(bad)

    def test_only_safe_addresses_are_opened(self):
        self.assertEqual(desktop.check_url("https://example.org/a?b=c#d"), "https://example.org/a?b=c#d")
        self.assertEqual(desktop.check_url("mailto:someone@example.org"), "mailto:someone@example.org")
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "ftp://example.org/x",
                    "https://", "http:///path", "example.org", "https://example.org/\n", "smb://host/share",
                    "https://exa mple.org/", "x" * 9000, "", None, 5):
            with self.assertRaises(ValueError, msg=repr(bad)[:40]):
                desktop.check_url(bad)
        self.assertEqual(desktop.check_url("ftp://example.org/x", ("ftp",)), "ftp://example.org/x")

    def test_opening_uses_an_argument_list(self):
        opened = []
        original, programs = desktop._xdg_open, desktop.XDG_OPEN
        desktop._xdg_open = lambda argument: opened.append(argument) or True
        self.addCleanup(setattr, desktop, "_xdg_open", original)
        self.assertTrue(fluent.open_url("https://example.org/"))
        self.assertFalse(fluent.open_url("file:///etc/passwd"))
        target = self.tmp / "a file; rm -rf $HOME.txt"
        target.write_text("x")
        self.assertTrue(fluent.open_path(target))
        self.assertTrue(fluent.open_path(self.tmp))
        self.assertFalse(fluent.open_path(self.tmp / "missing"))
        self.assertFalse(fluent.open_path("relative-missing"))
        self.assertEqual(opened, ["https://example.org/", str(target), str(self.tmp)])
        desktop._xdg_open = original
        desktop.XDG_OPEN = (str(self.tmp / "no-xdg-open"),)
        self.addCleanup(setattr, desktop, "XDG_OPEN", programs)
        self.assertFalse(fluent.open_url("https://example.org/"), "without xdg-open nothing is started")
        record = self.tmp / "argv"
        script = self.tmp / "xdg-open"
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$#" "$1" > "{record}"\n')
        script.chmod(0o755)
        desktop.XDG_OPEN = (str(script),)
        self.assertTrue(fluent.open_path(target))
        self.assertTrue(testing.wait_until(lambda: record.exists() and record.stat().st_size > 0, 5))
        self.assertEqual(record.read_text().splitlines(), ["1", str(target)], "one argument, never a shell")

    def test_notify_without_a_service_does_nothing(self):
        self.assertEqual(fluent.notify("Done", "The copy finished."), 0)
        self.assertEqual(fluent.notify("", "empty summary"), 0)
        self.assertEqual(self.callback_errors, [])

    @unittest.skipUnless(shutil.which("dbus-run-session") and shutil.which("dbus-daemon"), "no dbus-daemon")
    def test_notify_reaches_a_notification_service(self):
        """On a private bus, with a stand-in service in a process of its own: the call arrives with the
        argument types of the notification specification, and the service's ID comes back."""
        script = self.tmp / "notify_check.py"
        script.write_text(NOTIFY_CHECK.replace("@QT_DIR@", str(support.QT_DIR)))
        environment = {k: v for k, v in os.environ.items() if k != "DBUS_SESSION_BUS_ADDRESS"}
        environment["QT_QPA_PLATFORM"] = "offscreen"
        done = subprocess.run(["dbus-run-session", "--", sys.executable, "-B", str(script)], capture_output=True,
                              text=True, timeout=60, env=environment)
        lines = [line for line in done.stdout.splitlines() if line.startswith(("NOTIFY", "ID"))]
        self.assertEqual(lines, ["NOTIFY ('Example', 0, 'folder', 'Copy finished', '3 files &lt;b&gt;', [], "
                                 "{'desktop-entry': 'com.boswas.Example'}, 3000)", "ID 42"], done.stdout + done.stderr)


NOTIFY_CHECK = r'''
import subprocess
import sys

sys.path.insert(0, "@QT_DIR@")
SERVICE = """
import sys
from PySide6.QtCore import ClassInfo, QCoreApplication, QObject, QTimer, Slot
from PySide6.QtDBus import QDBusConnection


@ClassInfo({"D-Bus Interface": "org.freedesktop.Notifications"})
class Service(QObject):
    @Slot(str, "uint", str, str, str, "QStringList", "QVariantMap", int, result="uint")
    def Notify(self, app, replaces, icon, summary, body, actions, hints, timeout):
        print("NOTIFY", repr((app, replaces, icon, summary, body, list(actions), dict(hints), timeout)), flush=True)
        return 42


app = QCoreApplication(["service"])
bus = QDBusConnection.sessionBus()
service = Service()
assert bus.registerObject("/org/freedesktop/Notifications", service, QDBusConnection.RegisterOption.ExportAllSlots)
assert bus.registerService("org.freedesktop.Notifications")
print("READY", flush=True)
QTimer.singleShot(15000, app.quit)
app.exec()
"""
service = subprocess.Popen([sys.executable, "-B", "-c", SERVICE], stdout=subprocess.PIPE, text=True)
assert service.stdout.readline().strip() == "READY"

from PySide6.QtWidgets import QApplication

import boswas_fluent as fluent

app = QApplication(["check"])
app.setApplicationDisplayName("Example")
app.setDesktopFileName("com.boswas.Example")
identifier = fluent.notify("Copy finished", "3 files <b>", icon="folder", timeout_ms=3000)
print(service.stdout.readline().strip(), flush=True)
print("ID", identifier, flush=True)
service.terminate()
'''


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class HelpersTest(Base):
    def test_standard_shortcuts(self):
        window = self.show(QWidget(), 200, 100)
        fired = []
        made = fluent.add_shortcuts(window, find=lambda: fired.append("find"), quit=lambda: fired.append("quit"))
        self.assertEqual(set(made), {"find", "quit"})
        self.assertEqual(made["find"][0].key(), QKeySequence(QKeySequence.StandardKey.Find))
        made["find"][0].activated.emit()
        self.assertEqual(fired, ["find"])
        self.assertEqual(shortcuts.key_sequences("preferences")[0].toString(), "Ctrl+,")
        self.assertEqual(fluent.add_shortcut(window, "Ctrl+Shift+N", lambda: None)[0].key().toString(), "Ctrl+Shift+N")
        with self.assertRaises(KeyError):
            shortcuts.key_sequences("no-such-shortcut")
        for name in shortcuts.STANDARD:
            self.assertTrue(shortcuts.key_sequences(name), name)

    def test_testing_helpers(self):
        window = QWidget()
        button = QPushButton("Press", window)
        button.setGeometry(10, 10, 100, 32)
        edit = QLineEdit(window)
        edit.setGeometry(10, 50, 150, 32)
        self.show(window, 200, 100)
        clicks = []
        button.clicked.connect(lambda: clicks.append(1))
        testing.click(button)
        testing.click(window, button.geometry().center())           # a point in a parent finds the child
        self.assertEqual(len(clicks), 2)
        testing.type_text(edit, "Hello 42")
        self.assertEqual(edit.text(), "Hello 42")
        path = testing.grab(window, self.tmp / "shots" / "window.png")
        self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertTrue(testing.wait_until(lambda: True))
        self.assertFalse(testing.wait_until(lambda: False, 0.05))
        self.assertEqual(os.environ["XDG_CONFIG_HOME"], str(self.tmp / "xdg_config_home"))

    def test_the_gallery_renders_every_page_in_both_schemes(self):
        from PySide6.QtCore import QSize

        from boswas_fluent import gallery
        written = gallery.grab_all(self.tmp / "gallery", QSize(1100, 760))
        names = {path.name for path in written}
        for scheme in ("dark", "light"):
            for key, _title, _icon in gallery.PAGES:
                self.assertIn(f"{scheme}-{key}.png", names)
            for extra in ("dialog", "menu", "flyout", "teachingtip", "combo-popup", "focus", "compact"):
                self.assertIn(f"{scheme}-{extra}.png", names)
        for path in written:
            self.assertGreater(path.stat().st_size, 500, path.name)
        self.assertEqual(fluent.missing_icons(), [], "every icon the gallery asks for exists in Boswas Prism")


if __name__ == "__main__":
    unittest.main()
