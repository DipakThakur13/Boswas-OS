"""The volumes of the computer, kept current: mounts, free space, UDisks2 devices and actions on them."""

from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Signal

import boswas_fluent as fluent

from . import volumes
from .volumes import DRIVE, ENCRYPTED, FILESYSTEM, UDisksReader, Volume, udisks_error


def _build(mountinfo: str, udisks: dict | None, user_home: str) -> list[Volume]:
    return volumes.assemble(volumes.read_mounts(mountinfo), udisks, user_home=user_home, label_of=volumes.labels())


class VolumeMonitor(QObject):
    """``volumes`` is the current list; ``changed`` says it was replaced.

    ``bus`` is a ``udisks.UDisksBus`` (or a test double with ``call``,
    ``action``, ``watch`` and a ``changed`` signal) or None. Without a bus,
    or when UDisks2 does not answer, mounted volumes are listed and no
    volume offers an action (``has_udisks`` is False).
    """

    changed = Signal()
    notice = Signal(str, str)           # severity (success, error), text

    def __init__(self, worker: fluent.Worker, bus=None, parent: QObject | None = None, *,
                 mountinfo: str = volumes.MOUNTINFO, build: Callable[..., list[Volume]] = _build) -> None:
        super().__init__(parent)
        self.volumes: list[Volume] = []
        self.has_udisks = False
        self.loaded = False
        self._worker = worker
        self._bus = bus
        self._mountinfo = mountinfo
        self._build = build
        self._running = False
        self._again = False
        self._descriptor = -1
        self._notifier: QSocketNotifier | None = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self.refresh)
        self._poll = QTimer(self)
        self._poll.setInterval(15_000)                  # free space changes without any notification
        self._poll.timeout.connect(self.refresh)

    # --- watching ---
    def start(self) -> None:
        """Read the volumes now and keep them current from here on."""
        if self._bus is not None:
            self._bus.changed.connect(self._debounce.start)
            self._bus.watch()
        try:
            self._descriptor = os.open(self._mountinfo, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            self._descriptor = -1
        if self._descriptor >= 0:
            # The kernel marks the mount table as "exceptional" when it changes (poll: POLLPRI).
            self._notifier = QSocketNotifier(self._descriptor, QSocketNotifier.Type.Exception, self)
            self._notifier.activated.connect(self._mounts_changed)
        self._poll.start()
        self.refresh()

    def stop(self) -> None:
        self._poll.stop()
        self._debounce.stop()
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1

    def _mounts_changed(self, *_args) -> None:
        try:                                            # reading again re-arms the notification
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            while os.read(self._descriptor, 65536):
                pass
        except OSError:
            pass
        self._debounce.start()

    # --- reading ---
    def refresh(self) -> None:
        if self._running:
            self._again = True
            return
        self._running = True
        if self._bus is not None:
            UDisksReader(self._bus.call).read(self._assemble)
        else:
            self._assemble(None)

    def _assemble(self, udisks: dict | None) -> None:
        self.has_udisks = udisks is not None
        task = self._worker.submit(self._build, self._mountinfo, udisks, os.path.expanduser("~"),
                                   on_done=self._built, on_error=lambda _error: self._built(None))
        if task is None:                                # the worker was shut down
            self._running = False

    def _built(self, result: list[Volume] | None) -> None:
        self._running = False
        first = not self.loaded
        self.loaded = True
        if result is not None and (first or result != self.volumes):
            self.volumes = result
            self.changed.emit()
        if self._again:
            self._again = False
            self.refresh()

    def by_key(self, key: str) -> Volume | None:
        for volume in self.volumes:
            if volume.key == key:
                return volume
        return None

    def for_path(self, path: str) -> Volume | None:
        """The volume that holds ``path`` (the one with the longest mount point above it)."""
        best = None
        for volume in self.volumes:
            point = (volume.mount_point or "").rstrip("/")
            if volume.mount_point is None:
                continue
            if (path == point or path.startswith(point + "/") or point == "") and (
                    best is None or len(point) > len((best.mount_point or "").rstrip("/"))):
                best = volume
        return best

    def shown_mounts(self) -> list[tuple[str, str]]:
        return [(volume.mount_point, volume.name) for volume in self.volumes if volume.mount_point]

    # --- actions (UDisks2) ---
    def _fail(self, what: str, values: list, done: Callable[[str | None], None] | None) -> None:
        name = str(values[0]) if values else ""
        message = str(values[1]) if len(values) > 1 else ""
        self.notice.emit("error", f"{what}: {udisks_error(name, message)}")
        if done is not None:
            done(None)
        self.refresh()

    def mount(self, volume: Volume, done: Callable[[str | None], None] | None = None,
              object_path: str | None = None) -> None:
        """Mount a file system; ``done(mount point)`` or ``done(None)``."""
        if self._bus is None:
            return

        def mounted(ok: bool, values: list) -> None:
            if not ok:
                self._fail(f"“{volume.name}” could not be mounted", values, done)
                return
            self.refresh()
            if done is not None:
                done(str(values[0]) if values and isinstance(values[0], str) else None)

        self._bus.action(object_path or volume.object_path, FILESYSTEM, "Mount", [{}], mounted)

    def unlock(self, volume: Volume, passphrase: str, done: Callable[[str | None], None] | None = None) -> None:
        """Unlock an encrypted volume and mount what is inside; ``done(mount point)`` or ``done(None)``."""
        if self._bus is None:
            return

        def unlocked(ok: bool, values: list) -> None:
            if not ok or not values or not isinstance(values[0], str):
                self._fail(f"“{volume.name}” could not be unlocked", values, done)
                return
            self.mount(volume, done, object_path=values[0])

        self._bus.action(volume.object_path, ENCRYPTED, "Unlock", [passphrase, {}], unlocked)

    def unmount(self, volume: Volume, done: Callable[[bool], None] | None = None, eject: bool = False) -> None:
        """Unmount a volume (and lock it again when it was unlocked); with ``eject`` also eject or power off."""
        if self._bus is None:
            return
        bus = self._bus
        name = volume.name

        def finish(ok: bool) -> None:
            self.refresh()
            if ok:
                self.notice.emit("success", f"“{name}” can be removed now." if eject else
                                 f"“{name}” was unmounted.")
            if done is not None:
                done(ok)

        def failed(values: list) -> None:
            self._fail(f"“{name}” could not be {'ejected' if eject else 'unmounted'}", values, None)
            if done is not None:
                done(False)

        def power_off(ok: bool, values: list) -> None:
            if not ok:
                failed(values)
            elif volume.can_power_off and volume.drive_path:
                bus.action(volume.drive_path, DRIVE, "PowerOff", [{}],
                           lambda ok2, values2: finish(True) if ok2 else failed(values2))
            else:
                finish(True)

        def eject_drive(ok: bool, values: list) -> None:
            if not ok:
                failed(values)
            elif not eject or not volume.drive_path:
                finish(True)
            elif volume.kind == "optical" or not volume.can_power_off:
                bus.action(volume.drive_path, DRIVE, "Eject", [{}], power_off)
            else:
                power_off(True, [])

        def lock(ok: bool, values: list) -> None:
            if not ok:
                failed(values)
            elif volume.backing_path:
                bus.action(volume.backing_path, ENCRYPTED, "Lock", [{}], eject_drive)
            else:
                eject_drive(True, [])

        if volume.mounted and volume.object_path:
            bus.action(volume.object_path, FILESYSTEM, "Unmount", [{}], lock)
        else:
            lock(True, [])

    def eject(self, volume: Volume, done: Callable[[bool], None] | None = None) -> None:
        self.unmount(volume, done, eject=True)
