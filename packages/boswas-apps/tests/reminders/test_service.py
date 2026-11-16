"""The service object with stand-ins for the notification service, the sound and the clocks."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import BERLIN, FakeClock, at, event  # noqa: E402

from PySide6.QtCore import QCoreApplication, QObject, Signal  # noqa: E402

from boswas_apps.clock import model as clock_model  # noqa: E402
from boswas_apps.reminders import __main__ as program  # noqa: E402
from boswas_apps.reminders import chime, client, jsonfile, messages, paths  # noqa: E402
from boswas_apps.reminders.service import ChimePlayer, Service  # noqa: E402

APP = QCoreApplication.instance() or QCoreApplication(["test"])
PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])


class FakeNotifier(QObject):
    action = Signal(int, str)
    closed = Signal(int, int)
    available = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.shown: list[tuple[int, messages.Message]] = []
        self.down = False
        self.last_error = ""

    def notify(self, message, replaces_id: int = 0) -> int:
        if self.down:
            self.last_error = "no notification service"
            return 0
        self.shown.append((len(self.shown) + 1, message))
        return len(self.shown)

    def close(self, identifier: int) -> None:
        pass

    def is_connected(self) -> bool:
        return True


class FakeSound:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def play(self, kind: str, repeat: int = 1) -> None:
        self.calls.append(("play", kind, repeat))

    def stop(self) -> None:
        self.calls.append(("stop",))


def wait_until(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return bool(condition())


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock_path = self.tmp / "data" / "clock" / "clock.json"
        self.calendar_path = self.tmp / "data" / "calendar" / "calendar.json"
        self.state_path = self.tmp / "state" / "state.json"
        self.clock = FakeClock(at(2026, 11, 15, 20, 0))
        self.notifier = FakeNotifier()
        self.sound = FakeSound()
        self.launched: list[tuple[str, list[str]]] = []
        self.services: list[Service] = []
        self.doc = clock_model.empty_doc()

    def tearDown(self):
        for service in self.services:
            service._timer.stop()
            service._reload_timer.stop()
            service.deleteLater()
        APP.processEvents()
        self._tmp.cleanup()

    def write_clock(self) -> None:
        jsonfile.write_json_atomic(self.clock_path, self.doc)

    def write_calendar(self, *events: dict, visible: bool = True) -> None:
        jsonfile.write_json_atomic(self.calendar_path, {
            "calendars": [{"id": "c1", "name": "Personal", "visible": visible}],
            "events": [{"calendar": "c1", **entry} for entry in events]})

    def service(self, watch: bool = False) -> Service:
        service = Service(self.notifier, sound=self.sound, launcher=lambda app, args: self.launched.append((app, args))
                          or True, now=self.clock, zone=lambda: BERLIN, clock_path=self.clock_path,
                          calendar_path=self.calendar_path, state_path=self.state_path,
                          clock_settings=self.tmp / "clock-settings.json",
                          calendar_settings=self.tmp / "calendar-settings.json", watch=watch, logind=False)
        self.services.append(service)
        service.start()
        return service

    def alarm(self, hour, minute, **fields) -> dict:
        alarm = clock_model.make_alarm(hour, minute, now_wall=self.clock.wall, **fields)
        self.doc["alarms"].append(alarm)
        return alarm


class ServiceTest(ServiceCase):
    def test_sleeps_until_the_next_due_item_but_never_longer_than_a_minute(self):
        self.alarm(7, 0, days=range(7))
        self.write_clock()
        service = self.service()
        self.assertEqual(self.notifier.shown, [])
        self.assertAlmostEqual(service.next_wake, self.clock.wall + 60)
        self.assertTrue(service._timer.isActive())
        self.assertLessEqual(service._timer.interval(), 60_000)
        self.clock.set_wall(at(2026, 11, 16, 6, 59, 40))
        service.wake()
        self.assertAlmostEqual(service.next_wake, at(2026, 11, 16, 7, 0))
        self.assertLessEqual(abs(service._timer.interval() - 20_000), 20)

    def test_without_any_files_it_just_waits(self):
        service = self.service()
        self.assertEqual(self.notifier.shown, [])
        self.assertTrue(service._timer.isActive())
        service.stop()
        self.assertEqual(jsonfile.read_json(self.state_path)["checked_until"], self.clock.wall)

    def test_alarm_rings_with_buttons_sound_and_urgency(self):
        self.alarm(7, 0, days=range(7), name="Wake up", snooze=5)
        self.write_clock()
        service = self.service()
        self.clock.set_wall(at(2026, 11, 16, 7, 0))
        service.wake()
        (identifier, message), = self.notifier.shown
        self.assertEqual((message.app, message.summary, message.body, message.urgency, message.timeout),
                         ("clock", "Wake up", "07:00", messages.CRITICAL, messages.STAYS))
        self.assertEqual(message.actions, (("snooze", "Snooze 5 min"), ("dismiss", "Dismiss")))
        self.assertEqual(self.sound.calls, [("play", "alarm", 15)])
        service.wake()
        self.assertEqual(len(self.notifier.shown), 1)
        self.notifier.action.emit(identifier, "dismiss")
        self.assertEqual(self.sound.calls[-1], ("stop",))
        self.assertEqual(service.engine.state.ringing, {})
        self.assertEqual(jsonfile.read_json(self.state_path)["ringing"], {})

    def test_twelve_hour_times_and_silence_follow_the_settings(self):
        self.alarm(19, 30, days=range(7))
        self.write_clock()
        jsonfile.write_json_atomic(self.tmp / "clock-settings.json", {"time_format": "12", "alarm_sound": False})
        service = self.service()
        self.clock.set_wall(at(2026, 11, 16, 19, 30))
        service.wake()
        self.assertEqual(self.notifier.shown[0][1].body, "7:30 PM")
        self.assertEqual(self.sound.calls, [])

    def test_snooze_is_saved_at_once_and_survives_a_restart(self):
        self.alarm(7, 0, days=range(7), snooze=10)
        self.write_clock()
        service = self.service()
        self.clock.set_wall(at(2026, 11, 16, 7, 0))
        service.wake()
        self.notifier.action.emit(1, "snooze")
        saved = jsonfile.read_json(self.state_path)
        self.assertEqual([entry["due"] for entry in saved["snoozed"].values()], [at(2026, 11, 16, 7, 10)])
        self.assertEqual(self.sound.calls[-1], ("stop",))
        self.assertAlmostEqual(service.next_wake, self.clock.wall + 60)
        # The service is killed without a chance to save anything more, and starts again.
        service._timer.stop()
        again = self.service()
        self.assertEqual(len(self.notifier.shown), 1)
        self.clock.set_wall(at(2026, 11, 16, 7, 10))
        again.wake()
        self.assertEqual(len(self.notifier.shown), 2)
        self.assertEqual(self.notifier.shown[1][1].body, "07:00 · snoozed")
        self.notifier.closed.emit(2, 2)
        self.clock.advance(3600)
        again.wake()
        self.assertEqual(len(self.notifier.shown), 2)

    def test_one_time_alarm_is_switched_off_in_the_file(self):
        alarm = self.alarm(21, 0, name="Once")
        self.write_clock()
        service = self.service()
        self.clock.set_wall(at(2026, 11, 15, 21, 0))
        service.wake()
        stored = clock_model.normalize(jsonfile.read_json(self.clock_path))["alarms"][0]
        self.assertEqual((stored["enabled"], stored["armed"]), (False, alarm["armed"]))
        self.assertEqual(len(self.notifier.shown), 1)
        self.notifier.action.emit(1, "snooze")                       # the snooze still belongs to it
        self.clock.advance(601)
        service.wake()
        self.assertEqual(len(self.notifier.shown), 2)

    def test_timer_restart_button_restarts_the_timer_in_the_file(self):
        timer = clock_model.make_timer(300, "Tea")
        clock_model.timer_start(timer, self.clock())
        self.doc["timers"].append(timer)
        self.write_clock()
        service = self.service()
        self.clock.advance(300)
        service.wake()
        (identifier, message), = self.notifier.shown
        self.assertEqual((message.summary, message.actions, message.urgency),
                         ("Tea", (("dismiss", "Dismiss"), ("restart", "Restart")), messages.NORMAL))
        self.assertEqual(self.sound.calls, [("play", "timer", 3)])
        self.clock.advance(20)
        self.notifier.action.emit(identifier, "restart")
        stored = clock_model.normalize(jsonfile.read_json(self.clock_path))["timers"][0]
        self.assertEqual((stored["state"], stored["run"]), ("running", 2))
        self.assertAlmostEqual(clock_model.timer_remaining(stored, self.clock()), 300)
        self.assertAlmostEqual(service.next_wake, self.clock.wall + 60)
        self.clock.advance(300)
        service.wake()
        self.assertEqual(len(self.notifier.shown), 2)

    def test_restart_does_nothing_when_the_user_was_faster(self):
        timer = clock_model.make_timer(300)
        clock_model.timer_start(timer, self.clock())
        self.doc["timers"].append(timer)
        self.write_clock()
        service = self.service()
        self.clock.advance(300)
        service.wake()
        clock_model.timer_reset(timer)                                # the user resets it in Clock
        self.write_clock()
        self.notifier.action.emit(1, "restart")
        self.assertEqual(clock_model.normalize(jsonfile.read_json(self.clock_path))["timers"][0]["state"], "idle")

    def test_calendar_reminder_and_its_buttons(self):
        self.write_calendar(event("e1", "2026-11-16T10:00:00", "2026-11-16T11:00:00", reminders=[15],
                                  summary="Dentist", location="Main St"),
                            event("s", "2026-11-16T12:00:00", "2026-11-16T12:30:00", rrule="FREQ=DAILY",
                                  reminders=[0], summary="Standup"))
        service = self.service()
        self.clock.set_wall(at(2026, 11, 16, 9, 45))
        service.wake()
        (identifier, message), = self.notifier.shown
        self.assertEqual((message.app, message.summary, message.body),
                         ("calendar", "Dentist", "Today 10:00 – 11:00 · in 15 minutes\nMain St"))
        self.assertEqual(self.sound.calls, [("play", "reminder", 1)])
        self.notifier.action.emit(identifier, "open")
        self.assertEqual(self.launched, [("calendar", ["--show=e1"])])
        self.clock.set_wall(at(2026, 11, 16, 12, 0))
        service.wake()
        self.notifier.action.emit(2, "snooze")
        self.clock.advance(599)
        service.wake()
        self.assertEqual(len(self.notifier.shown), 2)
        self.clock.advance(2)
        service.wake()
        self.assertEqual(self.notifier.shown[2][1].summary, "Standup")
        self.notifier.action.emit(3, "default")
        self.assertEqual(self.launched[-1], ("calendar", ["--show=s", "--occurrence=2026-11-16T12:00:00"]))

    def test_what_was_missed_is_summed_up_once_per_application(self):
        self.alarm(7, 0, days=range(7), name="Wake up")
        self.write_clock()
        self.write_calendar(event("a", "2026-11-16T09:00:00", "2026-11-16T10:00:00", reminders=[15]),
                            event("b", "2026-11-17T09:00:00", "2026-11-17T10:00:00", reminders=[15, 5]))
        service = self.service()
        service.stop()
        self.clock.reboot(4 * 86400)
        again = self.service()
        self.assertEqual([(message.app, message.summary) for _id, message in self.notifier.shown],
                         [("clock", "Missed alarm"), ("calendar", "3 missed reminders")])
        self.assertIn("(4 times)", self.notifier.shown[0][1].body)
        self.assertEqual(self.sound.calls, [("stop",)])                # only the stop() of the first run
        self.notifier.action.emit(2, "open")
        self.assertEqual(self.launched, [("calendar", [])])
        again.wake()
        self.assertEqual(len(self.notifier.shown), 2)

    def test_a_notification_service_that_is_away_gets_the_reminder_later(self):
        self.alarm(7, 0, days=range(7))
        self.write_clock()
        service = self.service()
        self.notifier.down = True
        self.clock.set_wall(at(2026, 11, 16, 7, 0))
        service.wake()
        self.assertEqual(self.notifier.shown, [])
        self.assertEqual(self.sound.calls, [("play", "alarm", 15)])    # it still makes itself heard
        self.assertLessEqual(service.next_wake - self.clock.wall, 15.01)
        self.clock.advance(15)
        service.wake()
        self.assertEqual(self.notifier.shown, [])
        self.assertEqual(len(self.sound.calls), 1)                     # the sound is not started over and over
        self.notifier.down = False
        self.notifier.available.emit(True)                             # it is back: no waiting for the retry
        self.assertEqual([message.body for _id, message in self.notifier.shown], ["07:00"])
        self.clock.advance(60)
        service.wake()
        self.assertEqual(len(self.notifier.shown), 1)

    def test_other_peoples_notifications_are_ignored(self):
        service = self.service()
        self.notifier.action.emit(42, "dismiss")
        self.notifier.closed.emit(42, 1)
        self.assertEqual(self.launched, [])
        self.assertTrue(service._timer.isActive())

    def test_resume_from_sleep_polls_at_once(self):
        self.alarm(7, 0, days=range(7))
        self.write_clock()
        service = self.service()
        service.prepare_for_sleep(True)
        self.assertEqual(jsonfile.read_json(self.state_path)["checked_until"], self.clock.wall)
        self.clock.set_wall(at(2026, 11, 16, 7, 2))                   # asleep over the alarm
        service.prepare_for_sleep(False)
        self.assertEqual(len(self.notifier.shown), 1)

    def test_a_replaced_file_is_read_again(self):
        self.write_clock()
        service = self.service(watch=True)
        self.assertAlmostEqual(service.next_wake, self.clock.wall + 60)
        self.clock.set_wall(at(2026, 11, 16, 6, 59, 50))
        self.alarm(7, 0, days=range(7))
        self.write_clock()                                             # as Clock does: a new file, renamed over
        self.assertTrue(wait_until(lambda: abs(service.next_wake - at(2026, 11, 16, 7, 0)) < 0.01))
        self.write_calendar(event("e1", "2026-11-16T07:00:05", "2026-11-16T08:00:00", reminders=[0]))
        self.assertTrue(wait_until(lambda: service.engine.calendar is not None))
        self.clock.set_wall(at(2026, 11, 16, 7, 0, 6))
        service.wake()
        self.assertEqual(sorted(message.app for _id, message in self.notifier.shown), ["calendar", "clock"])

    def test_a_damaged_file_keeps_the_last_good_content(self):
        self.alarm(7, 0, days=range(7))
        self.write_clock()
        service = self.service()
        self.clock_path.write_text("{ half a docu")
        service.reload()
        self.clock.set_wall(at(2026, 11, 16, 7, 0))
        service.wake()
        self.assertEqual(len(self.notifier.shown), 1)


class LockTest(unittest.TestCase):
    def test_one_service_per_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            old = os.environ.get("XDG_RUNTIME_DIR")
            os.environ["XDG_RUNTIME_DIR"] = tmp
            try:
                self.assertFalse(client.is_running())
                first = program.acquire_lock()
                self.assertIsNotNone(first)
                self.assertTrue(client.is_running())
                self.assertIsNone(program.acquire_lock(attempts=2))
                os.close(first)
                self.assertFalse(client.is_running())
                second = program.acquire_lock()
                self.assertIsNotNone(second)
                os.close(second)
            finally:
                if old is None:
                    os.environ.pop("XDG_RUNTIME_DIR", None)
                else:
                    os.environ["XDG_RUNTIME_DIR"] = old


class ProgramTest(unittest.TestCase):
    """The real program in a process of its own, without a session bus."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")}
        for name in ("data", "state", "config"):
            (self.tmp / name).mkdir()
            self.env[f"XDG_{name.upper()}_HOME"] = str(self.tmp / name)
        (self.tmp / "run").mkdir(mode=0o700)
        self.env["XDG_RUNTIME_DIR"] = str(self.tmp / "run")
        self.env["PYTHONPATH"] = PACKAGE_ROOT
        self.env["TZ"] = "Europe/Berlin"

    def tearDown(self):
        self._tmp.cleanup()

    def start(self) -> subprocess.Popen:
        code = ("import sys; from boswas_apps.reminders.__main__ import run_service; "
                "sys.exit(run_service(['boswas-reminders']))")
        return subprocess.Popen([sys.executable, "-B", "-c", code], env=self.env, stderr=subprocess.PIPE, text=True)

    def test_runs_until_sigterm_saves_its_state_and_is_alone(self):
        lock = self.tmp / "run" / "boswas-reminders.lock"
        state = self.tmp / "state" / "com.boswas.Reminders" / "state.json"
        first = self.start()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not state.exists():
                time.sleep(0.05)
            self.assertTrue(state.exists() or lock.exists())
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not (self.tmp / "data" / "com.boswas.Clock").is_dir():
                time.sleep(0.05)
            second = self.start()
            self.assertEqual(second.wait(20), 0)
            self.assertIn("already running", second.stderr.read())
            second.stderr.close()
            self.assertIsNone(first.poll())
            first.send_signal(signal.SIGTERM)
            self.assertEqual(first.wait(20), 0)
        finally:
            if first.poll() is None:
                first.kill()
                first.wait()
            first.stderr.close()
        saved = json.loads(state.read_text())
        self.assertAlmostEqual(saved["checked_until"], time.time(), delta=30)
        third = self.start()                                           # the lock went with the process
        try:
            time.sleep(0.5)
            self.assertIsNone(third.poll())
        finally:
            third.send_signal(signal.SIGTERM)
            third.wait(20)
            third.stderr.close()

    def test_command_line(self):
        def run(*arguments):
            return subprocess.run([sys.executable, "-B", "-m", "boswas_apps.reminders", *arguments], env=self.env,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(run("--version").stdout.strip(), "boswas-reminders 1.0")
        self.assertIn("--status", run("--help").stdout)
        self.assertEqual(run("--no-such-option").returncode, 2)
        status = run("--status")
        if os.geteuid() == 0:
            self.assertEqual(status.returncode, 4)                    # never as root
            self.assertIn("never as root", status.stderr)
        else:
            self.assertEqual(status.returncode, 1)
            self.assertIn("service: not running", status.stdout)
            self.assertIn("time zone: Europe/Berlin", status.stdout)

    def test_status_text(self):
        import contextlib
        import io
        old = {name: os.environ.get(name) for name in self.env if name.startswith("XDG_") or name == "TZ"}
        os.environ.update({name: self.env[name] for name in old})
        try:
            doc = clock_model.empty_doc()
            doc["alarms"].append(clock_model.make_alarm(7, 0, days=range(7), now_wall=0.0))
            jsonfile.write_json_atomic(paths.clock_store(), doc)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = program.status()
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertEqual(code, 1)
        text = output.getvalue()
        self.assertIn("service: not running", text)
        self.assertRegex(text, r"next alarm: \d{4}-\d\d-\d\d 07:00:00 \(in ")
        self.assertIn("snoozed: 0", text)


class ChimeTest(unittest.TestCase):
    def test_every_chime_is_a_valid_short_wav(self):
        import io
        for kind in chime.KINDS:
            with wave.open(io.BytesIO(chime.wave_bytes(kind))) as sound:
                self.assertEqual((sound.getnchannels(), sound.getsampwidth(), sound.getframerate()), (1, 2, 44100))
                seconds = sound.getnframes() / sound.getframerate()
                self.assertTrue(0.5 < seconds < 3.0)
                frames = sound.readframes(sound.getnframes())
            self.assertGreater(max(frames), 0)
        with self.assertRaises(ValueError):
            chime.wave_bytes("gong")
        self.assertEqual(chime.main("gong"), 2)

    def test_the_file_is_written_into_a_private_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            old = os.environ.get("XDG_RUNTIME_DIR")
            os.environ["XDG_RUNTIME_DIR"] = tmp
            try:
                path = chime.chime_file("timer")
                self.assertEqual(path, Path(tmp) / "boswas-reminders" / "timer.wav")
                self.assertEqual(path.read_bytes(), chime.wave_bytes("timer"))
                self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)
                first = os.stat(path).st_mtime_ns
                self.assertEqual(os.stat(chime.chime_file("timer")).st_mtime_ns, first)     # not written again
            finally:
                if old is None:
                    os.environ.pop("XDG_RUNTIME_DIR", None)
                else:
                    os.environ["XDG_RUNTIME_DIR"] = old

    def test_the_helper_command(self):
        program_path, arguments = ChimePlayer.command("alarm", 3)
        self.assertEqual(program_path, sys.executable)
        self.assertEqual(arguments[-4:], ["--chime", "alarm", "--repeat", "3"])


if __name__ == "__main__":
    unittest.main()
