"""The Properties window of files, folders and volumes."""

from __future__ import annotations

import grp
import os
import pwd
import stat
import time

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QProgressBar, QScrollArea,
                               QVBoxLayout, QWidget)

import boswas_fluent as fluent

from . import mime, uiicons
from .fileops import describe_error
from .model import format_time
from .text import display_name, format_size
from .volumes import Volume

ACCESS = (("Read and write", 6), ("Read-only", 4), ("No access", 0))
FOLDER_ACCESS = (("Create and delete files", 7), ("Access files", 5), ("No access", 0))


class SizeCounter(QThread):
    """Adds up what is below some paths; reports while it counts."""

    counted = Signal(int, int, int, bool)       # files, folders, bytes, finished

    def __init__(self, paths: list[str], parent=None) -> None:
        super().__init__(parent)
        self.paths = list(paths)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        files = folders = size = 0
        last = time.monotonic()
        stack = list(self.paths)
        top = set(stack)
        while stack and not self._stop:
            current = stack.pop()
            try:
                info = os.lstat(current)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                if current not in top:
                    folders += 1
                try:
                    with os.scandir(current) as iterator:
                        stack.extend(entry.path for entry in iterator)
                except OSError:
                    pass
            else:
                files += 1
                size += info.st_size
            now = time.monotonic()
            if now - last >= 0.1:
                last = now
                self.counted.emit(files, folders, size, False)
        if not self._stop:
            self.counted.emit(files, folders, size, True)


def _owner(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def size_text(size: int) -> str:
    return f"{format_size(size)} ({size:,} bytes)" if size >= 1024 else format_size(size)


class PropertiesWindow(fluent.FluentWindow):
    """Properties of one item, of several items, or of a volume. A window of its own, as in Windows."""

    def __init__(self, services, paths: list[str] | None = None, volume: Volume | None = None,
                 choose_app=None) -> None:
        self.paths = [os.path.abspath(path) for path in (paths or [])]
        self.volume = volume
        if volume is not None:
            name = volume.name
        elif len(self.paths) == 1:
            name = os.path.basename(self.paths[0].rstrip("/")) or "/"
        else:
            name = f"{len(self.paths):,} items"
        super().__init__("", f"{display_name(name)} – Properties", size=(440, 600), minimum_size=(360, 320),
                         remember_geometry=False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.services = services
        self._choose_app = choose_app
        self._counter: SizeCounter | None = None
        self._applying = False
        self._apply_again = False
        self._mime = ""
        self._info: os.stat_result | None = None
        self.values: dict[str, fluent.Label] = {}
        area = QScrollArea(self)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidgetResizable(True)
        body = QWidget(area)
        self._layout = QVBoxLayout(body)
        self._layout.setContentsMargins(24, 24, 24, 24)
        self._layout.setSpacing(16)
        self.banner = fluent.InfoBar("error", parent=body)
        self.banner.hide()
        header = QHBoxLayout()
        header.setSpacing(16)
        self.icon = fluent.IconWidget(None, 48, body)
        self.name_label = fluent.Label(display_name(name), "subtitle", body, wrap=True, selectable=True)
        header.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(self.name_label, 1)
        self._layout.addLayout(header)
        self._layout.addWidget(self.banner)
        self.general = fluent.Card(body)
        self.form = QFormLayout()
        self.form.setHorizontalSpacing(24)
        self.form.setVerticalSpacing(10)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.general.add_layout(self.form)
        self._layout.addWidget(self.general)
        self.change_button: fluent.Button | None = None
        self.access_boxes: dict[str, QComboBox] = {}
        self.execute_box: QCheckBox | None = None
        self.usage_bar: QProgressBar | None = None
        if volume is not None:
            self._build_volume(volume)
        elif len(self.paths) == 1:
            self._build_single(self.paths[0])
        else:
            self._build_many()
        self._layout.addStretch(1)
        area.setWidget(body)
        self.set_content(area)
        self.add_shortcut("Esc", self.close)
        self.add_shortcut("close", self.close)

    # --- rows ---
    def _row(self, label: str, value: str, target: QFormLayout | None = None) -> fluent.Label:
        name = fluent.Label(label, "body", tone="secondary")
        content = fluent.Label(value, "body", wrap=True, selectable=True)
        (target or self.form).addRow(name, content)
        self.values[label] = content
        return content

    def value(self, label: str) -> str:
        return self.values[label].text() if label in self.values else ""

    def _count(self, paths: list[str]) -> None:
        self._counter = SizeCounter(paths, self)
        self._counter.counted.connect(self._counted)
        self._counter.start()

    def _counted(self, files: int, folders: int, size: int, finished: bool) -> None:
        suffix = "" if finished else " …"
        if "Size" in self.values:
            self.values["Size"].setText(size_text(size) + suffix)
        if "Contains" in self.values:
            self.values["Contains"].setText(f"{files:,} files, {folders:,} folders{suffix}")

    # --- one item ---
    def _build_single(self, path: str) -> None:
        try:
            info = os.lstat(path)
        except OSError as error:
            self.banner.show_message("error", describe_error(error), "This item cannot be read")
            self.general.hide()
            self.icon.set_icon(mime.icon("application/octet-stream"))
            return
        self._info = info
        is_link = stat.S_ISLNK(info.st_mode)
        try:
            target_info = os.stat(path) if is_link else info
        except OSError:
            target_info = None
        is_dir = target_info is not None and stat.S_ISDIR(target_info.st_mode)
        if is_dir:
            self._mime = mime.FOLDER
            type_text = "File folder"
            self.icon.set_icon(fluent.icon("folder"))
        elif target_info is None:
            self._mime = "inode/symlink"
            type_text = "Broken link"
            self.icon.set_icon(mime.icon("application/octet-stream"))
        else:
            self._mime = mime.for_name(os.path.basename(path))
            type_text = mime.description(self._mime)
            self.icon.set_icon(mime.icon(self._mime))
            self.services.worker.submit(mime.for_file, path, on_done=self._typed)
        self._row("Type", type_text)
        if not is_dir and target_info is not None:
            line = QHBoxLayout()
            line.setSpacing(12)
            self.opens_label = fluent.Label("", "body", wrap=True)
            self.change_button = fluent.Button("Change…")
            self.change_button.clicked.connect(self._change_app)
            line.addWidget(self.opens_label, 1)
            line.addWidget(self.change_button)
            self.form.addRow(fluent.Label("Opens with", "body", tone="secondary"), line)
            self._show_app()
        self._row("Location", display_name(os.path.dirname(path) or "/"))
        if is_link:
            try:
                self._row("Link to", display_name(os.readlink(path)))
            except OSError:
                pass
        if is_dir and not is_link:
            self._row("Size", "…")
            self._row("Contains", "…")
            self._count([path])
        else:
            self._row("Size", size_text((target_info or info).st_size))
        shown = target_info or info
        self._row("Modified", format_time(shown.st_mtime))
        self._row("Accessed", format_time(shown.st_atime))
        self._build_permissions(path, shown, is_dir)

    def _typed(self, name: str) -> None:
        if name and name != self._mime:
            self._mime = name
            self.values["Type"].setText(mime.description(name))
            self.icon.set_icon(mime.icon(name))
            self._show_app()

    def _show_app(self) -> None:
        if self.change_button is None:
            return
        app = self.services.apps.default_for(mime.chain(self._mime)) if self.services.apps.loaded else None
        self.opens_label.setText(app.name if app is not None else "No application")

    def _change_app(self) -> None:
        if self._choose_app is None:
            return
        self._choose_app(self, self.paths[0], self._mime, self._show_app)

    def _build_permissions(self, path: str, info: os.stat_result, is_dir: bool) -> None:
        card = fluent.Card(self, title="Permissions")
        form = QFormLayout()
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(10)
        self._row("Owner", _owner(info.st_uid), form)
        self._row("Group", _group(info.st_gid), form)
        mine = info.st_uid == os.geteuid() or os.geteuid() == 0
        choices = FOLDER_ACCESS if is_dir else ACCESS
        mode = stat.S_IMODE(info.st_mode)
        for label, shift in (("Owner can", 6), ("Group can", 3), ("Others can", 0)):
            box = QComboBox()
            box.setAccessibleName(label)
            bits = (mode >> shift) & 7
            for text, value in choices:
                box.addItem(text, value)
            wanted = bits if is_dir else bits & 6
            index = box.findData(wanted)
            if index < 0:                       # an unusual combination: show it as it is, selectable again
                box.addItem({1: "Execute only", 2: "Write only", 3: "Write and execute", 4: "Read-only",
                             6: "Read and write"}.get(wanted, f"Mode {wanted}"), wanted)
                index = box.count() - 1
            box.setCurrentIndex(index)
            box.setEnabled(mine)
            box.currentIndexChanged.connect(lambda _index: self._apply_mode())
            form.addRow(fluent.Label(label, "body", tone="secondary"), box)
            self.access_boxes[label] = box
        if not is_dir:
            self.execute_box = QCheckBox("Allow running this file as a program")
            self.execute_box.setChecked(bool(mode & 0o111))
            self.execute_box.setEnabled(mine)
            self.execute_box.toggled.connect(lambda _checked: self._apply_mode())
            form.addRow(self.execute_box)
        if not mine:
            form.addRow(fluent.Label("Only the owner can change permissions.", "caption", tone="secondary", wrap=True))
        card.add_layout(form)
        self._is_dir = is_dir
        self._layout.addWidget(card)

    def wanted_mode(self) -> int:
        mode = 0
        special = stat.S_IMODE(self._info.st_mode) & 0o7000 if self._info is not None else 0
        for label, shift in (("Owner can", 6), ("Group can", 3), ("Others can", 0)):
            bits = int(self.access_boxes[label].currentData())
            if not self._is_dir and self.execute_box is not None and self.execute_box.isChecked() and bits & 4:
                bits |= 1
            mode |= bits << shift
        return mode | special

    def _apply_mode(self) -> None:
        """Set the permissions the controls show. Changes are applied one after the other, the last one wins."""
        if self._applying:
            self._apply_again = True
            return
        self._applying = True
        path, mode = self.paths[0], self.wanted_mode()

        def finished(error: Exception | None = None) -> None:
            self._applying = False
            if error is not None:
                self.banner.show_message("error", describe_error(error), "The permissions were not changed")
            else:
                self.banner.hide()
            if self._apply_again:
                self._apply_again = False
                self._apply_mode()

        self.services.worker.submit(os.chmod, path, mode, on_done=lambda _none: finished(), on_error=finished)

    # --- several items ---
    def _build_many(self) -> None:
        self.icon.set_icon(mime.icon("application/octet-stream"))
        folders = {os.path.dirname(path) for path in self.paths}
        self._row("Items", f"{len(self.paths):,}")
        self._row("Location", display_name(folders.pop()) if len(folders) == 1 else "Several folders")
        self._row("Size", "…")
        self._row("Contains", "…")
        self._count(self.paths)

    # --- a volume ---
    def _build_volume(self, volume: Volume) -> None:
        self.icon.set_icon(uiicons.named(volume.icon, "drive-harddisk"))
        kinds = {"root": "System volume", "disk": "Local disk", "removable": "Removable drive",
                 "optical": "Optical disc", "network": "Network share", "encrypted": "Encrypted volume"}
        self._row("Type", kinds.get(volume.kind, "Volume"))
        if volume.description:
            self._row("Drive", volume.description)
        self._row("File system", volume.fstype or "Unknown")
        self._row("Device", display_name(volume.device))
        self._row("Mounted at", display_name(volume.mount_point) if volume.mount_point else "Not mounted")
        if volume.mounted and volume.total:
            self.usage_bar = QProgressBar()
            self.usage_bar.setTextVisible(False)
            self.usage_bar.setRange(0, 1000)
            self.usage_bar.setValue(round(1000 * volume.used / volume.total))
            self.usage_bar.setAccessibleName("Used space")
            if volume.free / volume.total < 0.1:
                self.usage_bar.setProperty("status", "error")
            self.form.addRow(self.usage_bar)
            self._row("Used", size_text(volume.used))
            self._row("Free", size_text(volume.free))
        if volume.total:
            self._row("Capacity", size_text(volume.total))
        if volume.read_only:
            self._row("Access", "Read-only")

    def closeEvent(self, event) -> None:
        if self._counter is not None:
            self._counter.stop()
            self._counter.wait(2000)
        super().closeEvent(event)
