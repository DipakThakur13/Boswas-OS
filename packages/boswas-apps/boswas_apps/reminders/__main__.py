"""boswas-reminders: the reminder service of the Boswas desktop. See boswas-reminders(1)."""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import signal
import socket
import sys
import time

from . import PROGRAM, paths

EXIT_ROOT = 4
VERSION = "1.0"


def acquire_lock(attempts: int = 4) -> int | None:
    """The descriptor of the held service lock, or None when another service of this user holds it."""
    path = paths.service_lock()
    try:
        if path.parent != paths.runtime_dir():
            paths.ensure_private_dir(path.parent)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        sys.stderr.write(f"{PROGRAM}: cannot open {path}: {exc.strerror or exc}\n")
        return None
    for attempt in range(attempts):
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EINTR):
                break
            if attempt + 1 < attempts:
                time.sleep(0.15)            # an application may be looking at the lock just now
    os.close(descriptor)
    return None


def status() -> int:
    """Print what the service would do next; 0 when a service is running, 1 when none is."""
    from ..clock import model as clock_model
    from . import client, jsonfile, textfmt, timeutil
    from .engine import Engine, State

    running = client.is_running()
    print(f"service: {'running' if running else 'not running'}")
    zone = timeutil.system_zone()
    print(f"time zone: {timeutil.zone_key(zone)}")
    state = State.load(paths.service_state()) if paths.service_state().exists() else State()
    engine = Engine(state, lambda: zone)
    try:
        engine.set_clock(clock_model.normalize(jsonfile.read_json(paths.clock_store(), max_bytes=8 * 1024 * 1024)))
        raw = jsonfile.read_json(paths.calendar_store())
        if raw is not None:
            from ..calendar import model as calendar_model
            engine.set_calendar(calendar_model.CalendarData.from_json(raw))
    except jsonfile.StoreError as exc:
        print(f"error: {exc}")
    now = clock_model.system_now()

    def moment(instant: float) -> str:
        return timeutil.from_epoch(instant, zone).strftime("%Y-%m-%d %H:%M:%S")

    if state.checked_until is not None:
        print(f"checked until: {moment(state.checked_until)}")
    due = engine.next_due(now)
    alarms = [ring for ring in (clock_model.alarm_next(alarm, now.wall, zone) for alarm in engine.clock["alarms"])
              if ring is not None]
    if alarms:
        print(f"next alarm: {moment(min(alarms))} ({textfmt.until_words(min(alarms) - now.wall)})")
    if due is not None:
        print(f"next wake-up: {moment(due)}")
    print(f"snoozed: {len(state.snoozed)}")
    return 0 if running else 1


class _Signals:
    """SIGTERM and SIGINT end the event loop (Python sees a signal only when Qt lets it run, hence the pipe)."""

    def __init__(self, app) -> None:
        from PySide6.QtCore import QSocketNotifier
        self._reader, self._writer = socket.socketpair()
        self._reader.setblocking(False)
        self._writer.setblocking(False)
        signal.set_wakeup_fd(self._writer.fileno())
        self._notifier = QSocketNotifier(self._reader.fileno(), QSocketNotifier.Type.Read, app)
        self._notifier.activated.connect(self._drain)
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, lambda *_args: app.quit())

    def _drain(self, *_args) -> None:
        try:
            self._reader.recv(64)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog=PROGRAM, description="The reminder service of the Boswas desktop: "
                                     "shows alarms, timers and calendar reminders while Clock and Calendar are "
                                     "closed.")
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {VERSION}")
    parser.add_argument("--status", action="store_true", help="say whether the service runs and what is due next")
    parser.add_argument("--verbose", action="store_true", help="write more to standard error")
    parser.add_argument("--chime", metavar="KIND", help=argparse.SUPPRESS)
    parser.add_argument("--repeat", metavar="N", type=int, default=1, help=argparse.SUPPRESS)
    options = parser.parse_args(argv[1:])
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.stderr.write(f"{PROGRAM}: runs as the desktop user, never as root\n")
        return EXIT_ROOT
    if options.chime:
        from . import chime
        return chime.main(options.chime, options.repeat)
    if options.status:
        return status()
    return run_service(argv[:1], verbose=options.verbose)


def run_service(argv: list[str], *, verbose: bool = False) -> int:
    """Be the service until SIGTERM. Returns at once with 0 when another one runs in this session."""
    lock = acquire_lock()
    if lock is None:
        sys.stderr.write(f"{PROGRAM}: already running in this session\n")
        return 0

    from PySide6.QtCore import QCoreApplication

    from .notifier import Notifier
    from .service import ChimePlayer, Service, log

    app = QCoreApplication(argv)
    app.setApplicationName(PROGRAM)
    signals = _Signals(app)
    notifier = Notifier(app)
    if not notifier.is_connected():
        log("there is no session bus; reminders cannot be shown")
    service = Service(notifier, sound=ChimePlayer(app), verbose=verbose, parent=app)
    service.start()
    try:
        code = int(app.exec())
    finally:
        service.stop()
        del signals
        os.close(lock)
    return code


if __name__ == "__main__":
    sys.exit(main())
