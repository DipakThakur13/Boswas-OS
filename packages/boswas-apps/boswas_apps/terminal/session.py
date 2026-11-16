"""A terminal's child process wired to the Qt event loop, and the stand-in the tests use.

``PtySession`` runs a program on a pseudo-terminal (``ptyhost.PtyProcess``).
Nothing here blocks: the master side is read when a QSocketNotifier says
there is data, at most ``READ_BURST`` bytes a turn; writes that the kernel
does not take at once wait in a bounded queue for a write notifier; the end
of the child is noticed through a pidfd (a timer where the kernel has
none). ``set_reading(False)`` stops reading, so a child that writes faster
than the terminal can draw is slowed down by the kernel instead of filling
memory.

A session that is closed while its child still runs hands the child to the
module's reaper, which waits for it without blocking, so no zombie stays.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Signal

from . import ptyhost

READ_SIZE = 65536
READ_BURST = 1 << 18            # bytes read in one turn of the event loop
MAX_OUTGOING = 32 << 20         # bytes waiting to be written to the child (pastes); more is dropped
FINAL_DRAIN = 1 << 20           # bytes still read after the child has ended
POLL_MS = 250


class Session(QObject):
    """What a terminal tab needs from its child process."""

    output = Signal(object)             # bytes the child wrote
    finished = Signal(int, int)         # exit status (128 + signal for a signal), signal number or 0

    pid = 0

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def resize(self, cols: int, rows: int, width: int = 0, height: int = 0) -> None:
        raise NotImplementedError

    def set_reading(self, reading: bool) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def is_running(self) -> bool:
        raise NotImplementedError

    def working_directory(self) -> str | None:
        return None

    def foreground_process(self) -> str | None:
        return None


class PtySession(Session):
    """A program on a pseudo-terminal. Raises ``ptyhost.SpawnError`` when it cannot be started."""

    def __init__(self, argv: list[str], *, program: str | None = None, cwd: str | None = None,
                 environ: dict | None = None, cols: int = 80, rows: int = 24, width: int = 0, height: int = 0,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.process: ptyhost.PtyProcess | None = ptyhost.PtyProcess(
            argv, program=program, cwd=cwd, environ=environ, cols=cols, rows=rows, width=width, height=height)
        self.pid = self.process.pid
        self._outgoing = bytearray()
        self._reading = True
        self._hung_up = False
        self._done = False
        self._reader = QSocketNotifier(self.process.master, QSocketNotifier.Type.Read, self)
        self._reader.activated.connect(self._readable)
        self._writer = QSocketNotifier(self.process.master, QSocketNotifier.Type.Write, self)
        self._writer.setEnabled(False)
        self._writer.activated.connect(self._writable)
        self._watch = _ExitWatch(self.process, self._child_ended, self)

    def is_running(self) -> bool:
        return not self._done

    # --- reading ---
    def set_reading(self, reading: bool) -> None:
        self._reading = reading
        if self.process is not None and not self._hung_up and not self._done:
            self._reader.setEnabled(reading)

    def _readable(self, *_args) -> None:
        process = self.process
        if process is None or self._done:
            return
        chunks = []
        total = 0
        while total < READ_BURST:
            data = process.read(READ_SIZE)
            if data is None:
                self._hung_up = True
                self._reader.setEnabled(False)
                break
            if not data:
                break
            chunks.append(data)
            total += len(data)
        if chunks:
            self.output.emit(chunks[0] if len(chunks) == 1 else b"".join(chunks))
        if self._hung_up and not self._done:
            # Every process has let go of the terminal; the child's end follows at once or has happened.
            # A child that lives on may open its terminal again: look again in a while.
            QTimer.singleShot(0, self._watch.check)
            QTimer.singleShot(POLL_MS, self._look_again)

    def _look_again(self) -> None:
        if self.process is not None and not self._done:
            self._hung_up = False
            self._reader.setEnabled(self._reading)

    # --- writing ---
    def write(self, data: bytes) -> None:
        process = self.process
        if process is None or self._done or not data:
            return
        if not self._outgoing:
            sent = process.write(data)
            if sent >= len(data):
                return
            data = data[sent:]
        if len(self._outgoing) + len(data) > MAX_OUTGOING:
            data = data[:max(0, MAX_OUTGOING - len(self._outgoing))]
        self._outgoing += data
        self._writer.setEnabled(bool(self._outgoing))

    def _writable(self, *_args) -> None:
        process = self.process
        if process is None or self._done:
            return
        if self._outgoing:
            sent = process.write(bytes(self._outgoing[:READ_SIZE]))
            del self._outgoing[:sent]
        if not self._outgoing:
            self._writer.setEnabled(False)

    def pending_output(self) -> int:
        """Bytes that wait to be written to the child."""
        return len(self._outgoing)

    def resize(self, cols: int, rows: int, width: int = 0, height: int = 0) -> None:
        if self.process is not None and not self._done:
            self.process.resize(cols, rows, width, height)

    # --- the end ---
    def _child_ended(self) -> None:
        process = self.process
        if process is None or self._done:
            return
        self._done = True
        total = 0
        while total < FINAL_DRAIN:          # what the child wrote last is still in the kernel's buffer
            data = process.read(READ_SIZE)
            if not data:
                break
            total += len(data)
            self.output.emit(data)
        status, number = process.returncode or 0, process.exit_signal
        self._stop_notifiers()
        process.hangup()
        self.finished.emit(status, number)

    def _stop_notifiers(self) -> None:
        self._reader.setEnabled(False)
        self._writer.setEnabled(False)
        self._watch.stop()

    def close(self) -> None:
        """Hang up. A child that is still running gets SIGHUP and is reaped when it ends."""
        process = self.process
        if process is None:
            return
        self.process = None
        self._stop_notifiers()
        self._outgoing.clear()
        was_done = self._done
        self._done = True
        process.hangup()
        if not was_done and process.poll() is None:
            _reaper().adopt(process)

    def working_directory(self) -> str | None:
        return self.process.working_directory() if self.process is not None and not self._done else None

    def foreground_process(self) -> str | None:
        return self.process.foreground_process() if self.process is not None and not self._done else None


class _ExitWatch(QObject):
    """Calls ``ended`` once when a child process has ended (and has been reaped)."""

    def __init__(self, process: ptyhost.PtyProcess, ended, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process = process
        self._ended = ended
        self._fd = -1
        self._notifier: QSocketNotifier | None = None
        self._timer: QTimer | None = None
        try:
            self._fd = os.pidfd_open(process.pid)
        except (AttributeError, OSError):
            self._fd = -1
        if self._fd >= 0:
            self._notifier = QSocketNotifier(self._fd, QSocketNotifier.Type.Read, self)
            self._notifier.activated.connect(self.check)
        else:
            self._timer = QTimer(self)
            self._timer.setInterval(POLL_MS)
            self._timer.timeout.connect(self.check)
            self._timer.start()

    def check(self, *_args) -> None:
        if self._ended is None:
            return
        if self._process.poll() is not None:
            ended, self._ended = self._ended, None
            self.stop()
            ended()

    def stop(self) -> None:
        self._ended = None
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        if self._timer is not None:
            self._timer.stop()
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1


class _Reaper(QObject):
    """Waits for children whose terminal was closed while they were running."""

    def __init__(self) -> None:
        super().__init__()
        self._watches: list[_ExitWatch] = []

    def adopt(self, process: ptyhost.PtyProcess) -> None:
        watch: _ExitWatch | None = None

        def gone() -> None:
            if watch in self._watches:
                self._watches.remove(watch)
                watch.deleteLater()

        watch = _ExitWatch(process, gone, self)
        self._watches.append(watch)
        QTimer.singleShot(0, watch.check)

    def waiting(self) -> int:
        return len(self._watches)


_the_reaper: _Reaper | None = None


def _reaper() -> _Reaper:
    global _the_reaper
    if _the_reaper is None:
        _the_reaper = _Reaper()
    return _the_reaper


def children_waiting() -> int:
    """Closed sessions whose child has not ended yet (tests)."""
    return _the_reaper.waiting() if _the_reaper is not None else 0


class FakeSession(Session):
    """A session without a process, for tests: remembers what was written, plays what the test gives it."""

    pid = 4242

    def __init__(self, parent: QObject | None = None, *, echo: bool = False) -> None:
        super().__init__(parent)
        self.written = bytearray()
        self.sizes: list[tuple[int, int, int, int]] = []
        self.reading = True
        self.closed = False
        self.running = True
        self.echo = echo
        self.directory: str | None = None
        self.foreground: str | None = None

    def write(self, data: bytes) -> None:
        self.written += data
        if self.echo and self.running:
            self.output.emit(bytes(data))

    def resize(self, cols: int, rows: int, width: int = 0, height: int = 0) -> None:
        self.sizes.append((cols, rows, width, height))

    def set_reading(self, reading: bool) -> None:
        self.reading = reading

    def close(self) -> None:
        self.closed = True
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def working_directory(self) -> str | None:
        return self.directory

    def foreground_process(self) -> str | None:
        return self.foreground if self.running else None

    def play(self, data: bytes | str) -> None:
        """The child writes ``data``."""
        self.output.emit(data.encode("utf-8") if isinstance(data, str) else bytes(data))

    def end(self, status: int = 0, number: int = 0) -> None:
        """The child ends."""
        if self.running:
            self.running = False
            self.finished.emit(status, number)
