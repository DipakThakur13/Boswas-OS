"""The services bridge as a child process of the window, and a fake of it.

BridgeClient starts /usr/libexec/boswas/boswas-services-bridge (no arguments,
no shell) the first time a settings section asks for it, reads its events
without blocking the window (QProcess signals), keeps the last state of
every service and tells the sections when one changed. Commands are written
to the bridge's standard input; that pipe is the only place a Wi-Fi
passphrase goes, and nothing here logs a command.

If the bridge ends by itself it is started again after a pause that grows
with every failure in a row (servicebridge.RESTART_DELAYS); meanwhile every
service reads as not available and status() is "unavailable". A bridge that
is not installed is not retried. stop() closes the bridge's standard input,
which is how it is told to end, and waits briefly for it.

One client per window: for_widget(widget) returns the client of the
widget's window, creating it with the module's ``factory`` (tests set it to
FakeBridge); the main window calls stop_for(self) when it closes.

FakeBridge has the same interface without a process: tests and screenshots
feed states into it with set_state() and read what the sections sent from
``sent``.

This is the only module that uses QProcess, and the bridge the only program
it starts.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .. import servicebridge as sb

STOP_WAIT_MS = 1500
KILL_WAIT_MS = 500
NOT_INSTALLED = "The Boswas services bridge is not installed on this system."
STOPPED_WORKING = "The settings service stopped working; it is being started again."


class _Signals(QObject):
    """The signals both the client and the fake have (state(), send() and status() are their own)."""

    changed = Signal(str)                 # the name of the service whose state changed
    network_changed = Signal(object)      # servicebridge.NetworkState
    bluetooth_changed = Signal(object)    # servicebridge.BluetoothState
    audio_changed = Signal(object)        # servicebridge.AudioState
    power_changed = Signal(object)        # servicebridge.PowerState
    brightness_changed = Signal(object)   # servicebridge.BrightnessState
    status_changed = Signal(str)          # stopped, starting, running or unavailable
    error_received = Signal(str)          # the bridge did not understand a command (a bug, shown as text)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.states = sb.States()
        self._status = sb.STOPPED
        self._problem = ""

    def signal(self, service: str):
        """The change signal of one service (it carries the service's new state)."""
        if service not in sb.SERVICES:
            raise KeyError(service)
        return getattr(self, f"{service}_changed")

    def state(self, service: str):
        """The last state of ``service`` (its dataclass of servicebridge; not available until reported)."""
        return self.states.get(service)

    def status(self) -> str:
        return self._status

    def problem(self) -> str:
        """Why the bridge is unavailable, in words for the user ("" while it is not)."""
        return self._problem

    def is_running(self) -> bool:
        return self._status == sb.RUNNING

    def _set_status(self, status: str, problem: str = "") -> None:
        if (status, problem) != (self._status, self._problem):
            self._status, self._problem = status, problem
            self.status_changed.emit(status)

    def _apply(self, service: str, state: object) -> None:
        if self.states.apply(service, state):
            self._emit(service)

    def _emit(self, service: str) -> None:
        self.signal(service).emit(self.states.get(service))
        self.changed.emit(service)

    def _reset_states(self) -> None:
        for service in self.states.reset():
            self._emit(service)


class BridgeClient(_Signals):
    def __init__(self, parent: QObject | None = None, *, program: str = sb.PROGRAM):
        super().__init__(parent)
        self.program = program
        self.decoder = sb.Decoder()
        self.version = ""
        self.restarts = 0                 # how often the bridge was started again after it ended by itself
        self._process: QProcess | None = None
        self._wanted = False
        self._failures = 0
        self._started_at = 0.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._launch)

    # --- life cycle ---
    def start(self) -> None:
        """Start the bridge unless it runs already (returns at once; the states arrive as events)."""
        self._wanted = True
        if self._process is None and not self._timer.isActive() and self._problem != NOT_INSTALLED:
            self._launch()

    def _launch(self) -> None:
        if not self._wanted or self._process is not None:
            return
        process = QProcess(self)
        process.setProgram(self.program)
        process.setArguments([])
        # The bridge's own log (warnings only) goes where Control Center's goes.
        process.setProcessChannelMode(QProcess.ProcessChannelMode.ForwardedErrorChannel)
        process.readyReadStandardOutput.connect(self._read)
        process.errorOccurred.connect(self._error)
        process.finished.connect(self._finished)
        self._process = process
        self._started_at = time.monotonic()
        self.decoder.reset()
        self._set_status(sb.STARTING)
        process.start()

    def stop(self) -> None:
        """End the bridge: close its standard input and wait briefly; it is not started again."""
        self._wanted = False
        self._timer.stop()
        process, self._process = self._process, None
        if process is not None:
            process.readyReadStandardOutput.disconnect(self._read)
            process.errorOccurred.disconnect(self._error)
            process.finished.disconnect(self._finished)
            if process.state() != QProcess.ProcessState.NotRunning:
                process.closeWriteChannel()
                if not process.waitForFinished(STOP_WAIT_MS):
                    process.kill()
                    process.waitForFinished(KILL_WAIT_MS)
            process.deleteLater()
        self._failures = 0
        self._reset_states()
        self._set_status(sb.STOPPED)

    # --- events ---
    def _read(self) -> None:
        process = self._process
        if process is None:
            return
        for name, state in self.decoder.feed(bytes(process.readAllStandardOutput())):
            if name == "ready":
                self.version = state.version
                self._set_status(sb.RUNNING)
            elif name == "error":
                self.error_received.emit(state.message)
            else:
                if self._status == sb.STARTING:           # a bridge that reports is running, with or without "ready"
                    self._set_status(sb.RUNNING)
                self._apply(name, state)

    def _error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            # Not installed (or not executable): starting it again would not help.
            process, self._process = self._process, None
            if process is not None:
                process.deleteLater()
            self._reset_states()
            self._set_status(sb.UNAVAILABLE, NOT_INSTALLED)

    def _finished(self, _code: int = 0, _status=None) -> None:
        process, self._process = self._process, None
        if process is not None:
            process.deleteLater()
        self._reset_states()
        if not self._wanted:
            self._set_status(sb.STOPPED)
            return
        if time.monotonic() - self._started_at >= sb.STABLE_AFTER:
            self._failures = 0
        self._failures += 1
        self.restarts += 1
        self._set_status(sb.UNAVAILABLE, STOPPED_WORKING)
        self._timer.start(int(sb.restart_delay(self._failures) * 1000))

    # --- commands ---
    def send(self, command: dict) -> bool:
        """Write one command to the bridge. False when it is not running; ValueError for an invalid command."""
        line = sb.encode(command)
        process = self._process
        if process is None or self._status != sb.RUNNING or process.state() != QProcess.ProcessState.Running:
            return False
        return process.write(line) == len(line)

    def refresh(self, service: str) -> bool:
        return self.send(sb.command("refresh", service=service))


class FakeBridge(_Signals):
    """A bridge without a process, for tests and screenshots.

    ``set_state(service, state)`` (or keyword changes to the current state)
    plays an event; ``sent`` holds the validated commands the sections sent;
    ``on_command`` may be set to a function that reacts to one, as the real
    services would. ``fail()`` plays a bridge that ended; ``running=False``
    one that is not installed.
    """

    def __init__(self, parent: QObject | None = None, *, running: bool = True, states: dict | None = None):
        super().__init__(parent)
        self.sent: list[dict] = []
        self.started = 0
        self.stopped = 0
        self.on_command = None
        self._can_run = running
        for service, state in (states or {}).items():
            self.states.apply(service, state)

    def start(self) -> None:
        self.started += 1
        if self._can_run:
            self._set_status(sb.RUNNING)
        else:
            self._set_status(sb.UNAVAILABLE, NOT_INSTALLED)

    def stop(self) -> None:
        self.stopped += 1
        self._set_status(sb.STOPPED)

    def fail(self, problem: str = STOPPED_WORKING) -> None:
        self._reset_states()
        self._set_status(sb.UNAVAILABLE, problem)

    def set_state(self, service: str, state: object = None, **changes) -> None:
        if state is None:
            state = sb.with_fields(self.states.get(service), **changes)
        self._apply(service, state)

    def send(self, command: dict) -> bool:
        checked = sb.validated(command)
        if self._status != sb.RUNNING:
            return False
        self.sent.append(checked)
        if self.on_command is not None:
            self.on_command(checked)
        return True

    def refresh(self, service: str) -> bool:
        return self.send(sb.command("refresh", service=service))

    def commands(self, cmd: str | None = None) -> list[dict]:
        """The commands sent so far (only those named ``cmd``), without refreshes."""
        return [c for c in self.sent if c["cmd"] != "refresh" and (cmd is None or c["cmd"] == cmd)]


# --- one client per window -------------------------------------------------------------------------------------

# What for_widget() creates; tests replace it with FakeBridge (or a function returning a prepared one).
factory = BridgeClient
_ATTRIBUTE = "_boswas_services_bridge"


def for_widget(widget) -> _Signals:
    """The bridge of the window ``widget`` is in, created (not yet started) on first use."""
    window = widget.window()
    bridge = getattr(window, _ATTRIBUTE, None)
    if bridge is None:
        bridge = factory(window)
        setattr(window, _ATTRIBUTE, bridge)
    return bridge


def stop_for(window) -> None:
    """Stop the window's bridge, if it has one (the window is closing)."""
    bridge = getattr(window, _ATTRIBUTE, None)
    if bridge is not None:
        setattr(window, _ATTRIBUTE, None)
        bridge.stop()
