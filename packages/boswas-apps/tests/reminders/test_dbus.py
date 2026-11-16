"""The service and a notification service talking over a real (private) session bus.

Skipped where there is no ``dbus-daemon``. Nothing of the user's session is
touched: the bus is started for the test and ended with it.
"""

import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from boswas_apps.clock import model as clock_model
from boswas_apps.reminders import jsonfile, messages

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = str(HERE.parents[1])
DAEMON = shutil.which("dbus-daemon")


@unittest.skipUnless(DAEMON, "dbus-daemon is not installed")
class DBusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")           # short paths for the bus socket
        self.tmp = Path(self._tmp.name)
        self.processes: list[subprocess.Popen] = []
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("XDG_")}
        for name in ("data", "state", "config"):
            (self.tmp / name).mkdir()
            self.env[f"XDG_{name.upper()}_HOME"] = str(self.tmp / name)
        (self.tmp / "run").mkdir(mode=0o700)
        self.env.update(XDG_RUNTIME_DIR=str(self.tmp / "run"), PYTHONPATH=PACKAGE_ROOT)
        daemon = subprocess.Popen([DAEMON, "--session", "--nofork", "--print-address=1",
                                   f"--address=unix:path={self.tmp}/bus"], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, env=self.env)
        self.processes.append(daemon)
        address = daemon.stdout.readline().strip()
        self.assertTrue(address.startswith("unix:"), address)
        self.env["DBUS_SESSION_BUS_ADDRESS"] = address
        self.log = self.tmp / "peer.log"
        self.peer = subprocess.Popen([sys.executable, "-B", str(HERE / "dbus_peer.py"), str(self.log)],
                                     stdin=subprocess.PIPE, text=True, env=self.env, stderr=subprocess.DEVNULL)
        self.processes.append(self.peer)
        self.assertTrue(self.wait_for(lambda: self.calls("ready")), "the stand-in service did not start")
        self.clock_path = self.tmp / "data" / "com.boswas.Clock" / "clock.json"
        self.calendar_path = self.tmp / "data" / "com.boswas.Calendar" / "calendar.json"

    def tearDown(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        self._tmp.cleanup()

    def calls(self, kind: str = "Notify") -> list[dict]:
        try:
            lines = self.log.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [entry for entry in map(json.loads, lines) if entry["call"] == kind]

    @staticmethod
    def wait_for(condition, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return bool(condition())

    def tell(self, command: str) -> None:
        self.peer.stdin.write(command + "\n")
        self.peer.stdin.flush()

    def start_service(self) -> subprocess.Popen:
        code = ("import sys; from boswas_apps.reminders.__main__ import run_service; "
                "sys.exit(run_service(['boswas-reminders']))")
        process = subprocess.Popen([sys.executable, "-B", "-c", code], env=self.env, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return process

    def test_timer_reminder_restart_and_clean_exit(self):
        doc = clock_model.empty_doc()
        timer = clock_model.make_timer(2, "Tea & <milk>")
        clock_model.timer_start(timer, clock_model.system_now())
        doc["timers"].append(timer)
        jsonfile.write_json_atomic(self.clock_path, doc)
        soon = dt.datetime.now() + dt.timedelta(seconds=3)
        jsonfile.write_json_atomic(self.calendar_path, {
            "calendars": [{"id": "c1", "name": "Personal"}],
            "events": [{"uid": "e1", "calendar": "c1", "summary": "Review", "reminders": [0],
                        "start": soon.isoformat(timespec="seconds"),
                        "end": (soon + dt.timedelta(hours=1)).isoformat(timespec="seconds")}]})
        service = self.start_service()
        self.assertTrue(self.wait_for(lambda: len(self.calls()) >= 2), self.calls())
        by_summary = {call["summary"]: call for call in self.calls()}
        tea = by_summary["Tea & <milk>"]
        self.assertEqual((tea["app"], tea["icon"], tea["replaces"], tea["timeout"]),
                         ("Clock", "preferences-system-time", 0, 0))
        self.assertEqual(tea["actions"], ["dismiss", "Dismiss", "restart", "Restart"])
        self.assertEqual(tea["hints"]["urgency"], messages.NORMAL)
        self.assertEqual(tea["hints"]["desktop-entry"], "com.boswas.Clock")
        review = by_summary["Review"]
        self.assertEqual((review["app"], review["hints"]["desktop-entry"]), ("Calendar", "com.boswas.Calendar"))
        self.assertEqual(review["actions"][::2], ["open", "snooze", "dismiss"])
        self.assertIn(" – ", review["body"])
        # The button of the notification: the timer runs again and fires again.
        self.tell(f"action {tea['id']} restart")
        self.assertTrue(self.wait_for(
            lambda: clock_model.normalize(jsonfile.read_json(self.clock_path))["timers"][0]["run"] == 2))
        self.assertTrue(self.wait_for(lambda: len(self.calls()) >= 3))
        # Snooze is on disk before anything else happens.
        self.tell(f"action {review['id']} snooze")
        state = self.tmp / "state" / "com.boswas.Reminders" / "state.json"
        self.assertTrue(self.wait_for(lambda: state.exists() and json.loads(state.read_text())["snoozed"]))
        service.send_signal(signal.SIGTERM)
        self.assertEqual(service.wait(20), 0)
        self.assertEqual(len(json.loads(state.read_text())["snoozed"]), 1)

    def test_the_urgency_is_sent_as_a_byte_and_buttons_come_back(self):
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtDBus import QDBusConnection

        from boswas_apps.reminders.notifier import Notifier, urgency_byte

        app = QCoreApplication.instance() or QCoreApplication(["test"])
        self.assertEqual(urgency_byte(2).currentSignature(), "y")
        monitor = None
        if shutil.which("dbus-monitor"):
            monitor = subprocess.Popen(["dbus-monitor", "--address", self.env["DBUS_SESSION_BUS_ADDRESS"],
                                        "interface='org.freedesktop.Notifications'"], stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)
            self.processes.append(monitor)
            time.sleep(0.5)
        bus = QDBusConnection.connectToBus(self.env["DBUS_SESSION_BUS_ADDRESS"], f"test-{os.getpid()}-{id(self)}")
        try:
            self.assertTrue(bus.isConnected())
            notifier = Notifier(bus=bus)
            seen: list[tuple] = []
            notifier.action.connect(lambda identifier, key: seen.append(("action", identifier, key)))
            notifier.closed.connect(lambda identifier, reason: seen.append(("closed", identifier, reason)))
            message = messages.Message(messages.CLOCK, "Wake up", "07:00 <b>&", (("snooze", "Snooze 10 min"),
                                                                                 ("dismiss", "Dismiss")),
                                       messages.CRITICAL, messages.STAYS, "alarm")
            identifier = notifier.notify(message)
            self.assertGreater(identifier, 0, notifier.last_error)
            call = self.calls()[-1]
            self.assertEqual((call["summary"], call["body"], call["hints"]["urgency"], call["timeout"]),
                             ("Wake up", "07:00 &lt;b&gt;&amp;", 2, 0))
            self.assertIs(call["hints"]["suppress-sound"], True)
            again = notifier.notify(message, replaces_id=identifier)
            self.assertEqual((again, self.calls()[-1]["replaces"]), (identifier, identifier))
            self.tell(f"action {identifier} snooze")
            deadline = time.monotonic() + 10
            while len(seen) < 2 and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            self.assertEqual(seen, [("action", identifier, "snooze"), ("closed", identifier, 2)])
            notifier.close(identifier)
            self.assertTrue(self.wait_for(lambda: self.calls("CloseNotification")))
            if monitor is not None:
                monitor.terminate()
                output = monitor.stdout.read()
                self.assertIn("byte 2", output)
                self.assertIn("uint32 0", output)
            # The notification service goes away: the call fails without blocking for long, and says why.
            self.tell("quit")
            self.peer.wait(10)
            began = time.monotonic()
            self.assertEqual(notifier.notify(message), 0)
            self.assertLess(time.monotonic() - began, 5.0)
            self.assertTrue(notifier.last_error)
            notifier.deleteLater()
        finally:
            QDBusConnection.disconnectFromBus(bus.name())


if __name__ == "__main__":
    unittest.main()
