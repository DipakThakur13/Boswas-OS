"""The reminder service: files in, notifications out (QtCore and, through the notifier, QtDBus).

One object, ``Service``, ties the parts together:

* It reads ``clock.json`` and ``calendar.json`` and watches their
  directories; a change is read again a moment later (the applications
  replace the files atomically, so a read never sees half a document).
* It keeps one single-shot timer. ``wake`` polls the engine, shows what
  rings, sums up what was missed, saves the state when it changed, and
  arms the timer for the next due item, at most ``TICK_SECONDS`` ahead.
  The regular wake-up is what notices a wall clock or a time zone that
  was changed: every poll works from the clocks as they are then.
* logind's ``PrepareForSleep`` signal (system bus) saves the state before
  the machine sleeps and polls at once when it wakes up.
* Buttons of the notifications come back through the notifier:
  snooze, dismiss, restart a timer, open the application.

The notifier, the sound player, the program launcher and the clocks are
given to the constructor, so the tests run the same code with stand-ins.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QFileSystemWatcher, QLocale, QObject, QProcess, Qt, QTimer, Slot

from ..clock import model as clock_model
from . import jsonfile, messages, paths, timeutil
from .engine import EVENT_SNOOZE_MINUTES, Batch, Engine, Item, State

TICK_SECONDS = 60.0
RELOAD_DELAY_MS = 200
RETRY_SECONDS = 15.0
MAX_RETRIES = 40                     # ten minutes of trying to reach the notification service
SOUND_REPEATS = {"alarm": 15, "timer": 3, "reminder": 1}
PROGRAMS = {messages.CLOCK: "/usr/bin/boswas-clock", messages.CALENDAR: "/usr/bin/boswas-calendar"}
CLOCK_PAGES = {"alarm": "alarms", "timer": "timers", "focus": "focus"}


def log(text: str) -> None:
    sys.stderr.write(f"boswas-reminders: {text}\n")
    sys.stderr.flush()


def launch(app: str, arguments: list[str]) -> bool:
    """Start Clock or Calendar (they pass the arguments on to a running instance). False when not installed."""
    program = PROGRAMS.get(app)
    if program is None or not (os.path.isfile(program) and os.access(program, os.X_OK)):
        return False
    started = QProcess.startDetached(program, arguments)
    return bool(started[0] if isinstance(started, tuple) else started)


def locale_twelve_hour() -> bool:
    return "ap" in QLocale.system().timeFormat(QLocale.FormatType.ShortFormat).lower()


def twelve_hour(settings_path: Path) -> bool:
    """Whether the application whose settings are in ``settings_path`` shows 12-hour times."""
    try:
        raw = jsonfile.read_json(settings_path, max_bytes=1024 * 1024)
    except jsonfile.StoreError:
        raw = None
    choice = raw.get("time_format") if isinstance(raw, dict) else None
    if choice == "12":
        return True
    if choice == "24":
        return False
    return locale_twelve_hour()


class ChimePlayer(QObject):
    """Plays the chime in a helper process (see ``chime``); at most one at a time."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None

    @staticmethod
    def command(kind: str, repeat: int) -> tuple[str, list[str]]:
        launcher = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
        tail = ["--chime", kind, "--repeat", str(repeat)]
        if launcher is not None and launcher.name == "boswas-reminders" and launcher.is_file():
            return sys.executable, ["-IB", str(launcher.resolve()), *tail]
        return sys.executable, ["-B", "-m", "boswas_apps.reminders", *tail]

    def play(self, kind: str, repeat: int = 1) -> None:
        self.stop()
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.ForwardedErrorChannel)
        process.setStandardInputFile(QProcess.nullDevice())
        process.setStandardOutputFile(QProcess.nullDevice())
        process.finished.connect(lambda *_args, p=process: self._finished(p))
        process.errorOccurred.connect(lambda *_args, p=process: self._finished(p))
        self._process = process
        program, arguments = self.command(kind, repeat)
        process.start(program, arguments)

    def _finished(self, process: QProcess) -> None:
        if self._process is process:
            self._process = None
        process.deleteLater()

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        if process.state() != QProcess.ProcessState.NotRunning:
            process.terminate()
            QTimer.singleShot(1500, process, process.kill)
        else:
            process.deleteLater()


class Service(QObject):
    def __init__(self, notifier, *, sound=None, launcher: Callable[[str, list[str]], bool] = launch,
                 now: Callable[[], clock_model.Now] = clock_model.system_now,
                 zone: Callable[[], object] = timeutil.system_zone, clock_path: Path | None = None,
                 calendar_path: Path | None = None, state_path: Path | None = None,
                 clock_settings: Path | None = None, calendar_settings: Path | None = None,
                 watch: bool = True, logind: bool = True, verbose: bool = False,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.notifier = notifier
        self.sound = sound
        self.launcher = launcher
        self._now = now
        self._zone_source = zone
        self._zone = zone()
        self.clock_path = Path(clock_path) if clock_path is not None else paths.clock_store()
        self.calendar_path = Path(calendar_path) if calendar_path is not None else paths.calendar_store()
        self.state_path = Path(state_path) if state_path is not None else paths.service_state()
        self.clock_settings = Path(clock_settings) if clock_settings is not None else paths.clock_settings()
        self.calendar_settings = (Path(calendar_settings) if calendar_settings is not None
                                  else paths.calendar_settings())
        self._watch = watch
        self._use_logind = logind
        self.verbose = verbose
        self.engine = Engine(State.load(self.state_path), lambda: self._zone)
        self._signatures: dict[Path, object] = {}
        self._shown: dict[int, object] = {}             # notification ID -> Item, or ("missed", app)
        self._sounding: str | None = None               # key of the item whose chime plays
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.wake)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(RELOAD_DELAY_MS)
        self._reload_timer.timeout.connect(self.reload)
        self._watcher: QFileSystemWatcher | None = None
        self._system_bus = None
        self.next_wake: float | None = None             # the instant the timer is armed for (for --status, tests)
        notifier.action.connect(self._on_action)
        notifier.closed.connect(self._on_closed)
        notifier.available.connect(self._on_available)

    # --- life cycle ---
    def start(self) -> None:
        self.engine.restore()
        if self._watch:
            self._watcher = QFileSystemWatcher(self)
            self._watcher.directoryChanged.connect(lambda _path: self._reload_timer.start())
            self._watcher.fileChanged.connect(lambda _path: self._reload_timer.start())
            for path in (self.clock_path, self.calendar_path):
                try:
                    paths.ensure_private_dir(path.parent)
                except OSError as exc:
                    log(f"cannot create {path.parent}: {exc.strerror or exc}")
        self._rewatch()
        if self._use_logind:
            self._connect_logind()
        self.reload(force=True)

    def stop(self) -> None:
        """Save what must survive and stop the sound; the object is not used afterwards."""
        self._timer.stop()
        self._reload_timer.stop()
        if self.sound is not None:
            self.sound.stop()
        self.engine.state.dirty = True                 # ``checked_until`` moved on since the last save
        self._save_state()

    def _rewatch(self) -> None:
        if self._watcher is None:
            return
        wanted = set()
        for path in (self.clock_path, self.calendar_path):
            if path.parent.is_dir():
                wanted.add(str(path.parent))
            if path.is_file():
                wanted.add(str(path))
        watched = set(self._watcher.files()) | set(self._watcher.directories())
        if wanted - watched:
            self._watcher.addPaths(sorted(wanted - watched))

    def _connect_logind(self) -> None:
        try:
            from PySide6.QtCore import SLOT
            from PySide6.QtDBus import QDBusConnection
        except ImportError:
            return
        bus = QDBusConnection.systemBus()
        if not bus.isConnected():
            return
        self._system_bus = bus
        bus.connect("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                    "PrepareForSleep", self, SLOT("prepare_for_sleep(bool)"))

    @Slot(bool)
    def prepare_for_sleep(self, going_to_sleep: bool) -> None:
        if going_to_sleep:
            self.engine.state.dirty = True
            self._save_state()
        else:
            self.wake()

    # --- the stores ---
    def reload(self, force: bool = False) -> None:
        """Read the stores that changed on disk, then poll."""
        self._rewatch()
        for path, loader in ((self.clock_path, self._load_clock), (self.calendar_path, self._load_calendar)):
            signature = jsonfile.signature(path)
            if force or signature != self._signatures.get(path):
                self._signatures[path] = signature
                loader()
        self.wake()

    def _load_clock(self) -> None:
        try:
            raw = jsonfile.read_json(self.clock_path, max_bytes=8 * 1024 * 1024)
        except jsonfile.StoreError as exc:
            log(str(exc))
            return                                     # keep what was read before
        self.engine.set_clock(clock_model.normalize(raw))

    def _load_calendar(self) -> None:
        from ..calendar import model as calendar_model     # only loaded here: Clock alone does not need it
        try:
            raw = jsonfile.read_json(self.calendar_path)
        except jsonfile.StoreError as exc:
            log(str(exc))
            return
        self.engine.set_calendar(calendar_model.CalendarData.from_json(raw) if raw is not None else None)

    def _change_clock(self, change: Callable[[dict], None]) -> None:
        """Change ``clock.json`` under its lock (the same way Clock does) and take the result as read."""
        def apply(raw):
            doc = clock_model.normalize(raw)
            change(doc)
            return doc
        try:
            doc = jsonfile.update_json(self.clock_path, apply, max_bytes=8 * 1024 * 1024)
        except jsonfile.StoreError as exc:
            log(str(exc))
            return
        self.engine.set_clock(doc)
        self._signatures[self.clock_path] = jsonfile.signature(self.clock_path)

    def _save_state(self) -> None:
        state = self.engine.state
        if not state.dirty:
            return
        try:
            state.save(self.state_path)
        except jsonfile.StoreError as exc:
            log(str(exc))

    # --- the heartbeat ---
    @Slot()
    def wake(self) -> None:
        now = self._now()
        self._zone = self._zone_source()
        batch = self.engine.poll(now)
        if batch:
            self._deliver(batch, now)
        self._save_state()
        self._arm(now)

    def _arm(self, now: clock_model.Now) -> None:
        due = self.engine.next_due(now)
        delay = TICK_SECONDS if due is None else max(0.05, min(TICK_SECONDS, due - now.wall))
        self.next_wake = now.wall + delay
        self._timer.setTimerType(Qt.TimerType.CoarseTimer if delay >= TICK_SECONDS else Qt.TimerType.PreciseTimer)
        self._timer.start(int(delay * 1000) + (0 if delay >= TICK_SECONDS else 15))

    def _twelve_hour(self, app: str) -> bool:
        return twelve_hour(self.clock_settings if app == messages.CLOCK else self.calendar_settings)

    def _sound_wanted(self) -> bool:
        try:
            raw = jsonfile.read_json(self.clock_settings, max_bytes=1024 * 1024)
        except jsonfile.StoreError:
            return True
        return not (isinstance(raw, dict) and raw.get("alarm_sound") is False)

    def _deliver(self, batch: Batch, now: clock_model.Now) -> None:
        loudest: tuple[int, Item] | None = None
        formats: dict[str, bool] = {}

        def fmt(app: str) -> bool:
            if app not in formats:
                formats[app] = self._twelve_hour(app)
            return formats[app]

        once = [item.source for item in batch.ring + batch.missed if item.kind == "alarm" and item.detail.get("once")]
        if once:
            def switch_off(doc: dict) -> None:
                for alarm in doc["alarms"]:
                    if alarm["id"] in once and not alarm["days"]:
                        alarm["enabled"] = False       # ``armed`` stays: a snooze still belongs to this alarm
            self._change_clock(switch_off)
        for item in batch.ring:
            app = messages.CALENDAR if item.kind == "event" else messages.CLOCK
            message = messages.compose(item, now.wall, self._zone, fmt(app))
            identifier = self.notifier.notify(message)
            retry = int(item.detail.get("retry", 0))
            if identifier:
                self._shown[identifier] = item
            elif retry < MAX_RETRIES:
                if retry == 0 or self.verbose:
                    log(f"notification not shown ({self.notifier.last_error}); trying again")
                again = Item(item.key, item.kind, item.source, item.when, item.title,
                             {**item.detail, "retry": retry + 1})
                self.engine.snooze(again, now.wall, RETRY_SECONDS / 60)
            else:
                log(f"giving up on a notification: {item.title or item.kind}")
                self.engine.acknowledge(item)
            if message.sound and retry == 0:
                rank = ("reminder", "timer", "alarm").index(message.sound)
                if loudest is None or rank > loudest[0]:
                    loudest = (rank, item)
        if batch.missed:
            for message in messages.compose_missed(batch.missed, now.wall, self._zone, fmt(messages.CLOCK)):
                identifier = self.notifier.notify(message)
                if identifier:
                    self._shown[identifier] = ("missed", message.app)
        if loudest is not None and self.sound is not None and self._sound_wanted():
            kind = ("reminder", "timer", "alarm")[loudest[0]]
            self._sounding = loudest[1].key
            self.sound.play(kind, SOUND_REPEATS[kind])
        if len(self._shown) > 500:
            for identifier in list(self._shown)[:len(self._shown) - 500]:
                del self._shown[identifier]

    # --- answers ---
    def _quiet(self, item: Item | None = None) -> None:
        if self.sound is not None and self._sounding is not None and (item is None or item.key == self._sounding):
            self.sound.stop()
            self._sounding = None

    def _on_available(self, available: bool) -> None:
        if available and self.engine.hurry(self._now().wall):
            self.wake()

    def _on_action(self, identifier: int, key: str) -> None:
        entry = self._shown.get(identifier)
        if entry is None:
            return                                     # somebody else's notification
        now = self._now()
        if isinstance(entry, tuple):
            del self._shown[identifier]
            if key in ("open", "default"):
                self.launcher(entry[1], [])
            return
        item: Item = entry
        del self._shown[identifier]
        self._quiet(item)
        if key == "snooze" and item.kind in ("alarm", "event"):
            minutes = int(item.detail.get("snooze", 10)) if item.kind == "alarm" else EVENT_SNOOZE_MINUTES
            detail = {name: value for name, value in item.detail.items() if name != "retry"}
            self.engine.snooze(Item(item.key, item.kind, item.source, item.when, item.title, detail), now.wall,
                               minutes)
        else:
            self.engine.acknowledge(item)
            if key == "restart" and item.kind == "timer":
                self._restart_timer(item, now)
            elif key in ("open", "default"):
                if item.kind == "event":
                    arguments = [f"--show={item.source}"]
                    if item.detail.get("recurrence_id"):
                        arguments.append(f"--occurrence={item.detail['recurrence_id']}")
                    self.launcher(messages.CALENDAR, arguments)
                else:
                    self.launcher(messages.CLOCK, [f"--page={CLOCK_PAGES.get(item.kind, 'alarms')}"])
        self._save_state()
        self._arm(now)

    def _restart_timer(self, item: Item, now: clock_model.Now) -> None:
        run = item.key.rsplit(":", 1)[-1]

        def restart(doc: dict) -> None:
            timer = clock_model.find(doc["timers"], item.source)
            # Only the run that finished is restarted: the user may have reset or started it since.
            if timer is not None and str(timer["run"]) == run and clock_model.timer_phase(timer, now) == "done":
                clock_model.timer_restart(timer, now)
        self._change_clock(restart)

    def _on_closed(self, identifier: int, _reason: int) -> None:
        entry = self._shown.pop(identifier, None)
        if isinstance(entry, Item):
            self._quiet(entry)
            self.engine.acknowledge(entry)
            self._save_state()
