"""The viewer page: one picture, the pictures of its folder around it, and what can be done with it."""

from __future__ import annotations

import os
import shutil
from collections import OrderedDict

from PySide6.QtCore import QMimeData, QPoint, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDrag, QGuiApplication, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QMenu, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import APP_ID, decode, editops, exif, fileinfo, formats, imageio, library, openwith, printing, trash
from .. import wallpaper as wallpaper_module
from ..desktopcfg import ConfigError
from ..fileutil import format_size, free_name, rename_no_replace
from .canvas import ImageCanvas
from .common import BannerHolder, ElidedLabel, OverlayButton, Surface, action_icon
from .dialogs import PrintDialog, ResizeDialog
from .infopane import InfoPane
from .thumbview import Filmstrip

BAR_HEIGHT = 48
CACHE_BYTES = 256 * 1024 * 1024         # decoded pictures kept for going back and forth
PREFETCH_FILE_BYTES = 24 * 1024 * 1024  # only files up to this size are decoded ahead
PREVIEW_FILE_BYTES = 1024 * 1024        # JPEGs above this get a quick reduced decode first
LOAD_DELAY_MS = 40                      # holding an arrow key does not decode every picture passed
FILE_MANAGER = ("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1")


def _load_job(path: str) -> tuple[decode.Loaded, exif.Exif | None, os.stat_result | None]:
    loaded = decode.load(path)
    details = exif.read_file(path) if loaded.info.format in ("jpeg", "tiff") else None
    try:
        status = os.stat(path)
    except OSError:
        status = None
    return loaded, details, status


def show_in_file_manager(path: str) -> bool:
    """Open the folder of ``path`` in the file manager, with the file selected where the file manager
    offers that (org.freedesktop.FileManager1). True when a request was handed over."""
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusMessage
    except ImportError:
        return fluent.open_path(os.path.dirname(path))
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS") or fluent.runtime_dir() is not None:
        bus = QDBusConnection.sessionBus()
        interface = bus.interface() if bus.isConnected() else None
        if interface is not None:
            reply = interface.isServiceRegistered(FILE_MANAGER[0])
            if reply.isValid() and reply.value():
                call = QDBusMessage.createMethodCall(*FILE_MANAGER, "ShowItems")
                call.setArguments([[QUrl.fromLocalFile(path).toString()], ""])
                bus.asyncCall(call, 1500)
                return True
    return fluent.open_path(os.path.dirname(path))


class ViewerPage(QWidget):
    """Shows the pictures of a list (a folder, or the files the user chose) one at a time."""

    back_requested = Signal()
    edit_requested = Signal()
    slideshow_requested = Signal()
    fullscreen_requested = Signal()
    settings_requested = Signal()
    open_requested = Signal()
    files_changed = Signal()              # a file was deleted, renamed, turned or written
    current_changed = Signal(str)

    def __init__(self, host, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.host = host                  # the window: settings, questions, file choosers, thumbnails
        self.worker = fluent.Worker(self, max_threads=2)
        self.file_worker = fluent.Worker(self, max_threads=1)      # changes to files, one after the other
        self._items: list[library.Item] = []
        self._index = -1
        self._token = 0
        self._loaded: decode.Loaded | None = None
        self._exif: exif.Exif | None = None
        self._error: decode.DecodeError | None = None
        self._cache: OrderedDict[str, tuple[int, decode.Loaded, exif.Exif | None]] = OrderedDict()
        self._cache_bytes = 0
        self._apps: dict[str, list[openwith.Application]] = {}
        self._fullscreen = False
        self._bars_shown = True
        self._from_gallery = False
        self._last_trash: trash.TrashRecord | None = None
        self._direction = 1
        self._view_turned = False            # the picture on show was turned without the file being changed
        self._last_pointer = None

        # --- the top bar ---
        self.top_bar = Surface(self, fill=None)
        bar = QHBoxLayout(self.top_bar)
        bar.setContentsMargins(8, 8, 12, 8)
        bar.setSpacing(4)
        self.back_button = fluent.IconButton(fluent.icon("go-previous-symbolic"), "Gallery (Alt+Left)", self.top_bar)
        self.back_button.clicked.connect(self.back_requested)
        bar.addWidget(self.back_button)
        self.name_label = ElidedLabel("", "body-strong", self.top_bar)
        self.name_label.setMaximumWidth(360)
        bar.addWidget(self.name_label, 1)
        bar.addSpacing(8)
        self.edit_button = fluent.Button("Edit", self.top_bar, icon=action_icon("document-edit", "edit"))
        self.edit_button.setToolTip("Edit picture (Ctrl+E)")
        self.edit_button.clicked.connect(self.edit_requested)
        bar.addWidget(self.edit_button)
        self.commands = fluent.CommandBar(self.top_bar, labels=False)
        self.commands.setAccessibleName("Picture commands")
        add = self.commands.add_command
        self.rotate_action = add("Rotate", action_icon("object-rotate-right"), self.rotate,
                                 tooltip="Rotate right (Ctrl+R)")
        self.delete_action = add("Delete", action_icon("edit-delete", "user-trash"), self.delete,
                                 tooltip="Move to the Trash (Delete)")
        self.print_action = add("Print", action_icon("document-print", "printer"), self.print_picture,
                                tooltip="Print (Ctrl+P)")
        self.print_action.setVisible(printing.AVAILABLE)
        self.slideshow_action = add("Slideshow", action_icon("media-playback-start"),
                                    lambda: self.slideshow_requested.emit(), tooltip="Slideshow (F5)")
        self.commands.add_separator()
        self.zoom_out_action = add("Zoom out", action_icon("zoom-out"), lambda: self.canvas.zoom_out(),
                                   tooltip="Zoom out (Ctrl+Minus)")
        self.zoom_in_action = add("Zoom in", action_icon("zoom-in"), lambda: self.canvas.zoom_in(),
                                  tooltip="Zoom in (Ctrl+Plus)")
        self.fit_action = add("Fit to window", action_icon("zoom-fit-best"), lambda: self.canvas.zoom_fit(),
                              tooltip="Fit to window (Ctrl+0)")
        self.actual_action = add("Actual size", action_icon("zoom-original"), lambda: self.canvas.zoom_actual(),
                                 tooltip="Actual size, 100 % (Ctrl+1)")
        self.commands.add_separator()
        self.info_action = add("File info", fluent.icon("dialog-information-symbolic"), self.toggle_info,
                               tooltip="File info (Alt+Enter)", checkable=True)
        # the "more" menu
        self.open_with_menu = QMenu("Open with", self)
        self.open_with_menu.aboutToShow.connect(self._fill_open_with)
        self.open_with_action = QAction("Open with", self)
        self.open_with_action.setMenu(self.open_with_menu)
        self.commands.add_action(self.open_with_action, primary=False)
        self.copy_action = add("Copy", action_icon("edit-copy"), self.copy, primary=False)
        self.copy_path_action = add("Copy as path", None, self.copy_path, primary=False)
        self.wallpaper_action = add("Set as desktop background", None, self.set_as_wallpaper, primary=False)
        self.save_as_action = add("Save as", action_icon("document-save-as"), self.save_as, primary=False)
        self.resize_action = add("Resize", None, self.resize_picture, primary=False)
        self.show_in_files_action = add("Show in Files", None, self.show_in_files, primary=False)
        self.info_menu_action = add("File info", None, self.toggle_info, primary=False)
        self.filmstrip_action = add("Filmstrip", None, self.toggle_filmstrip, primary=False, checkable=True)
        self.open_action = add("Open file", fluent.icon("document-open-symbolic"),
                               lambda: self.open_requested.emit(), primary=False)
        self.fullscreen_action = add("Full screen", action_icon("view-fullscreen"),
                                     lambda: self.fullscreen_requested.emit(), primary=False)
        self.settings_action = add("Settings", None, lambda: self.settings_requested.emit(), primary=False)
        for action, keys in ((self.copy_action, "Ctrl+C"), (self.copy_path_action, "Ctrl+Shift+C"),
                             (self.save_as_action, "Ctrl+S"), (self.info_menu_action, "Alt+Return"),
                             (self.open_action, "Ctrl+O"), (self.fullscreen_action, "F11")):
            # shown in the menu; the live shortcuts are made below so they also work while the bar is hidden
            action.setShortcut(QKeySequence(keys))
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.setShortcutVisibleInContextMenu(True)
        for action in self.commands.commands():
            # where the icon theme has no picture for a command, its name stands in
            button = self.commands.button(action)
            if button is not None and action.icon().isNull():
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        bar.addWidget(self.commands, 2)
        self.position_label = fluent.Label("", "caption", self.top_bar, tone="secondary")
        self.position_label.setAccessibleName("Position in the folder")
        bar.addWidget(self.position_label)
        self.zoom_label = fluent.Label("", "caption", self.top_bar, tone="secondary")
        self.zoom_label.setMinimumWidth(44)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.zoom_label.setAccessibleName("Zoom")
        bar.addWidget(self.zoom_label)

        # --- the middle: picture and info pane ---
        self.banner = fluent.InfoBar("info", parent=self)
        self.banner.hide()
        self.banner.action_triggered.connect(self._banner_action)
        self._banner_callback = None
        self._banner_about_picture = False       # the banner says something about the picture on show
        self.stage = QWidget(self)
        self.canvas = ImageCanvas(self.stage)
        self.canvas.zoom_changed.connect(self._zoom_changed)
        self.canvas.step_requested.connect(self.step)
        self.canvas.drag_started.connect(self._drag_out)
        self.canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.canvas.customContextMenuRequested.connect(self._context_menu)
        self.previous_button = OverlayButton(fluent.icon("go-previous-symbolic"), "Previous picture (Left)",
                                             self.stage, size=QSize(36, 56))
        self.next_button = OverlayButton(fluent.icon("go-next-symbolic"), "Next picture (Right)", self.stage,
                                         size=QSize(36, 56))
        self.previous_button.clicked.connect(lambda: self.step(-1))
        self.next_button.clicked.connect(lambda: self.step(1))
        self.spinner = fluent.ProgressRing(self.stage, size=32)
        self.spinner.hide()
        self.message = fluent.EmptyState("", "", fluent.icon("image-x-generic"), None, self.stage)
        self.message.hide()
        self.info_pane = InfoPane(self)
        self.info_pane.hide()
        self.info_pane.close_requested.connect(lambda: self.set_info_visible(False))
        self.info_pane.rename_requested.connect(self.rename)
        self.filmstrip = Filmstrip(host.thumbs, self)
        self.filmstrip.activated.connect(self.show_index)

        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)
        middle.addWidget(self.stage, 1)
        middle.addWidget(self.info_pane)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, BAR_HEIGHT, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addWidget(BannerHolder(self.banner, self))
        self._layout.addLayout(middle, 1)
        self._layout.addWidget(self.filmstrip)
        self.top_bar.raise_()

        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(LOAD_DELAY_MS)
        self._load_timer.timeout.connect(self._start_load)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(2500)
        self._hide_timer.timeout.connect(self._auto_hide)
        self.setMouseTracking(True)
        self.stage.setMouseTracking(True)
        self.stage.installEventFilter(self)
        self.canvas.installEventFilter(self)
        self.top_bar.installEventFilter(self)

        self._shortcuts: list[QShortcut] = []
        for keys, slot in (
                ("Left", lambda: self.step(-1)), ("Right", lambda: self.step(1)),
                ("Home", lambda: self.show_index(0)), ("End", lambda: self.show_index(len(self._items) - 1)),
                ("PgUp", lambda: self.step(-1)), ("PgDown", lambda: self.step(1)),
                ("Ctrl+R", self.rotate), ("Ctrl+Shift+R", lambda: self.rotate(False)), ("Delete", self.delete),
                ("Ctrl+C", self.copy), ("Ctrl+Shift+C", self.copy_path), ("Ctrl+P", self.print_picture),
                ("F5", lambda: self.slideshow_requested.emit()), ("Ctrl+E", lambda: self.edit_requested.emit()),
                ("Ctrl+S", self.save_as), ("Ctrl+O", lambda: self.open_requested.emit()),
                ("Alt+Return", self.toggle_info), ("Alt+Enter", self.toggle_info), ("F2", self.start_rename),
                ("F11", lambda: self.fullscreen_requested.emit()), ("Escape", self._escape),
                ("Alt+Left", lambda: self.back_requested.emit()),
                ("Ctrl++", lambda: self.canvas.zoom_in()), ("Ctrl+=", lambda: self.canvas.zoom_in()),
                ("+", lambda: self.canvas.zoom_in()), ("Ctrl+-", lambda: self.canvas.zoom_out()),
                ("-", lambda: self.canvas.zoom_out()), ("Ctrl+0", lambda: self.canvas.zoom_fit()),
                ("Ctrl+1", lambda: self.canvas.zoom_actual())):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)
        self._sync_actions()
        self.set_filmstrip_visible(host.settings.filmstrip, remember=False)

    # --- what is shown ---
    def items(self) -> list[library.Item]:
        return self._items

    def index(self) -> int:
        return self._index

    def current_path(self) -> str | None:
        return self._items[self._index].path if 0 <= self._index < len(self._items) else None

    def loaded(self) -> decode.Loaded | None:
        return self._loaded

    def current_exif(self) -> exif.Exif | None:
        return self._exif

    def error(self) -> decode.DecodeError | None:
        return self._error

    def set_from_gallery(self, from_gallery: bool) -> None:
        self._from_gallery = from_gallery

    def show_items(self, items: list[library.Item], index: int = 0) -> None:
        """Show ``items`` (the list the arrows walk through), starting at ``index``."""
        self._items = list(items)
        self.filmstrip.set_items(self._items, index)
        self._index = -1
        if self._items:
            self.show_index(max(0, min(len(self._items) - 1, index)), animate=False)
        else:
            self._show_nothing("No pictures here", "This folder has no pictures Photos can show.")

    def open_file(self, path: str) -> None:
        """Show ``path`` at once and list its folder in the background."""
        path = os.path.abspath(path)
        try:
            first = library.item_for(path)
        except OSError:
            first = library.Item(path, 0.0, 0)
        self.show_items([first], 0)
        folder = os.path.dirname(path)
        extensions = decode.supported_extensions()
        self.worker.submit(library.folder_items, folder, extensions, path,
                           on_done=lambda items, p=path: self._folder_listed(p, items))

    def _folder_listed(self, path: str, items: list[library.Item]) -> None:
        current = self.current_path()
        if current is None or os.path.dirname(current) != os.path.dirname(path) or len(self._items) > 1:
            return                               # the user has moved on
        paths = [item.path for item in items]
        if current not in paths:
            return
        self._items = list(items)
        self._index = paths.index(current)
        self.filmstrip.set_items(self._items, self._index)
        self._sync_actions()
        self._update_title()

    def show_index(self, index: int, animate: bool = True) -> None:
        if not 0 <= index < len(self._items):
            return
        if index == self._index and (self._loaded is not None or self._error is not None):
            return
        self._direction = 1 if index >= self._index else -1
        self._index = index
        self._token += 1
        self._loaded = None
        self._exif = None
        self._error = None
        self._view_turned = False
        item = self._items[index]
        self.message.hide()
        self.filmstrip.set_current(index, animate)
        self._update_title()
        self._sync_actions()
        cached = self._cache.get(item.path)
        if cached is not None and cached[0] == int(item.mtime):
            self._cache.move_to_end(item.path)
            self._load_timer.stop()
            self._show_loaded(cached[1], cached[2])
        else:
            thumbnail = self.host.thumbs.pixmap(item.path, item.mtime)
            if thumbnail is not None and max(thumbnail.width(), thumbnail.height()) >= 200:
                # A stand-in until the picture is decoded: the thumbnail, stretched over the place the
                # picture will take (pretend it is large, so it is fitted like the real one will be).
                self.canvas.set_picture(thumbnail.toImage(), thumbnail.size() * 64)
            else:
                self.canvas.clear()
                self.spinner.show()
            self._load_timer.start()
        self.current_changed.emit(item.path)

    def step(self, delta: int) -> None:
        if len(self._items) > 1 and self._index >= 0:
            self.show_index((self._index + delta) % len(self._items))

    def reload(self) -> None:
        """Decode the current file again (it changed on disk)."""
        path = self.current_path()
        if path is None:
            return
        self._forget(path)
        try:
            self._items[self._index] = library.item_for(path)
        except OSError:
            pass
        index, self._index = self._index, -1
        self.show_index(index, animate=False)

    # --- loading ---
    def _start_load(self) -> None:
        path = self.current_path()
        if path is None:
            return
        token = self._token
        item = self._items[self._index]
        if item.size > PREVIEW_FILE_BYTES and formats.format_for_extension(formats.extension(path)) == "jpeg":
            screen = self.screen().size() * self.devicePixelRatioF() if self.screen() else QSize(1920, 1080)
            side = max(screen.width(), screen.height(), 1024)
            self.worker.submit(decode.load_scaled, path, side,
                               on_done=lambda result, t=token: self._preview_loaded(t, result))
        self.worker.submit(_load_job, path,
                           on_done=lambda result, t=token, p=path: self._load_done(t, p, result),
                           on_error=lambda exc, t=token: self._load_failed(t, exc))

    def _preview_loaded(self, token: int, result) -> None:
        if token != self._token or self._loaded is not None or self._error is not None:
            return
        image, info = result
        self.spinner.hide()
        self.canvas.set_picture(image, QSize(info.width, info.height))

    def _load_done(self, token: int, path: str, result) -> None:
        loaded, details, status = result
        if status is not None:
            self._remember(path, status.st_mtime, loaded, details)
        if token != self._token:
            return
        self._show_loaded(loaded, details)
        self._prefetch()

    def _load_failed(self, token: int, error: Exception) -> None:
        if token != self._token:
            return
        self.spinner.hide()
        if not isinstance(error, decode.DecodeError):
            error = decode.DecodeError("corrupt", "The file could not be read.")
        self._error = error
        self._loaded = None
        self.canvas.clear()
        self.message.set_icon(fluent.icon("image-x-generic"))
        self.message.set_title(error.title)
        self.message.set_text(error.detail)
        self.message.show()
        self._place_overlays()
        self._sync_actions()
        self._update_info()

    def _show_loaded(self, loaded: decode.Loaded, details: exif.Exif | None) -> None:
        self.spinner.hide()
        self.message.hide()
        self._loaded = loaded
        self._exif = details
        self._error = None
        info = loaded.info
        natural = QSize(info.width, info.height)
        self.canvas.set_picture(loaded.image, natural, svg=loaded.svg, alpha=bool(info.has_alpha),
                                keep_view=True)
        note = ""
        if info.animated:
            if not self.canvas.set_movie(info.path):
                note = "This animation is too large to play; its first picture is shown."
        if loaded.truncated:
            note, severity = "This file is incomplete: part of the picture is missing.", "warning"
        elif loaded.reduced:
            note, severity = (f"This picture has {fileinfo.format_megapixels(info.width, info.height)}. To save "
                              "memory it is shown with fewer pixels; edits are saved at that size."), "info"
        else:
            severity = "info"
        if note:
            if not self.banner.isVisible() or self._banner_about_picture:
                self.show_banner(severity, note)         # never over the result of something the user just did
                self._banner_about_picture = True
        elif self._banner_about_picture:
            self.banner.hide()
            self._banner_about_picture = False
        self._sync_actions()
        self._update_info()
        self._request_apps(info.format)

    def _remember(self, path: str, mtime: float, loaded: decode.Loaded, details: exif.Exif | None) -> None:
        self._forget(path)
        cost = loaded.image.sizeInBytes()
        if cost > CACHE_BYTES // 2:
            return
        self._cache[path] = (int(mtime), loaded, details)
        self._cache_bytes += cost
        while self._cache_bytes > CACHE_BYTES and len(self._cache) > 1:
            _old, (_m, dropped, _e) = self._cache.popitem(last=False)
            self._cache_bytes -= dropped.image.sizeInBytes()

    def _forget(self, path: str) -> None:
        entry = self._cache.pop(path, None)
        if entry is not None:
            self._cache_bytes -= entry[1].image.sizeInBytes()

    def _prefetch(self) -> None:
        if len(self._items) < 2 or self.worker.pending() > 0:
            return
        neighbour = self._items[(self._index + self._direction) % len(self._items)]
        cached = self._cache.get(neighbour.path)
        if neighbour.size > PREFETCH_FILE_BYTES or (cached is not None and cached[0] == int(neighbour.mtime)):
            return
        self.worker.submit(_load_job, neighbour.path, key=f"prefetch:{neighbour.path}",
                           on_done=lambda result, p=neighbour.path: self._prefetched(p, result),
                           on_error=lambda _exc: None)

    def _prefetched(self, path: str, result) -> None:
        loaded, details, status = result
        if status is not None and loaded.image.sizeInBytes() <= CACHE_BYTES // 3:
            self._remember(path, status.st_mtime, loaded, details)

    def _show_nothing(self, title: str, text: str) -> None:
        self._index = -1
        self._loaded = None
        self._error = None
        self._token += 1
        self.canvas.clear()
        self.spinner.hide()
        self.message.set_icon(fluent.icon("folder-pictures", "folder"))
        self.message.set_title(title)
        self.message.set_text(text)
        self.message.show()
        self.filmstrip.set_items([], -1)
        self._place_overlays()
        self._update_title()
        self._sync_actions()
        self._update_info()

    # --- chrome ---
    def _update_title(self) -> None:
        path = self.current_path()
        name = os.path.basename(path) if path else ""
        self.name_label.set_text(name)
        many = len(self._items) > 1
        self.position_label.setText(f"{self._index + 1} of {len(self._items)}" if many and self._index >= 0 else "")
        window = self.window()
        if window is not None and self.isVisible():
            window.setWindowTitle(f"{name} - Photos" if name else "Photos")

    def _zoom_changed(self) -> None:
        self.zoom_label.setText(f"{self.canvas.zoom_percent()} %" if self.canvas.has_picture() else "")

    def _sync_actions(self) -> None:
        have = self._loaded is not None
        path = self.current_path()
        info = self._loaded.info if have else None
        still = have and not info.animated
        self.edit_button.setEnabled(have)
        self.rotate_action.setEnabled(bool(still and not info.vector))
        self.delete_action.setEnabled(path is not None)
        self.print_action.setEnabled(have)
        self.slideshow_action.setEnabled(bool(self._items))
        for action in (self.zoom_in_action, self.zoom_out_action, self.fit_action, self.actual_action,
                       self.copy_action, self.wallpaper_action, self.save_as_action, self.resize_action):
            action.setEnabled(have)
        for action in (self.copy_path_action, self.show_in_files_action, self.open_with_action):
            action.setEnabled(path is not None)
        many = len(self._items) > 1
        self.previous_button.setVisible(many and self._bars_shown)
        self.next_button.setVisible(many and self._bars_shown)
        self.back_button.setVisible(True)
        self._zoom_changed()

    def show_banner(self, severity: str, text: str, action: str | None = None, callback=None) -> None:
        self._banner_callback = callback
        self._banner_about_picture = False
        self.banner.set_action(action)
        self.banner.show_message(severity, text)

    def _banner_action(self) -> None:
        callback, self._banner_callback = self._banner_callback, None
        self.banner.hide()
        if callback is not None:
            callback()

    def _place_overlays(self) -> None:
        stage = self.stage.rect()
        self.canvas.setGeometry(stage)
        y = (stage.height() - self.previous_button.height()) // 2
        self.previous_button.move(12, y)
        self.next_button.move(stage.width() - self.next_button.width() - 12, y)
        self.spinner.move((stage.width() - self.spinner.width()) // 2, (stage.height() - self.spinner.height()) // 2)
        self.message.setGeometry(stage)
        for widget in (self.previous_button, self.next_button, self.spinner):
            widget.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.top_bar.setGeometry(0, 0, self.width(), BAR_HEIGHT)
        self._place_overlays()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update_title()
        self._place_overlays()
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if obj is self.stage and kind == event.Type.Resize:
            self._place_overlays()
        elif self._fullscreen and kind == event.Type.MouseMove:
            self._pointer_moved(event.globalPosition())
        return False

    def mouseMoveEvent(self, event) -> None:
        if self._fullscreen:
            self._pointer_moved(event.globalPosition())
        super().mouseMoveEvent(event)

    def _pointer_moved(self, position) -> None:
        """In full screen: the bar comes back when the pointer really moves (a bar that appears or goes
        away under a resting pointer makes mouse events too, and must not keep itself alive)."""
        last = self._last_pointer
        self._last_pointer = position
        if last is None or (position - last).manhattanLength() >= 3:
            self._reveal_bars()

    # --- full screen ---
    def is_fullscreen(self) -> bool:
        return self._fullscreen

    def set_fullscreen(self, fullscreen: bool) -> None:
        """In full screen the picture has the whole window; the bar comes back while the pointer moves."""
        self._fullscreen = fullscreen
        self._layout.setContentsMargins(0, 0 if fullscreen else BAR_HEIGHT, 0, 0)
        self.top_bar.set_fill("background" if fullscreen else None)
        self.filmstrip.setVisible(not fullscreen and self.host.settings.filmstrip)
        self._last_pointer = None
        if fullscreen:
            self.info_pane.hide()
            self.info_action.setChecked(False)
            self._set_bars(False)
        else:
            self._hide_timer.stop()
            self._set_bars(True)
        self.fullscreen_action.setText("Leave full screen" if fullscreen else "Full screen")

    def bars_visible(self) -> bool:
        return self._bars_shown

    def _set_bars(self, shown: bool) -> None:
        self._bars_shown = shown
        self.top_bar.setVisible(shown)
        many = len(self._items) > 1
        self.previous_button.setVisible(shown and many)
        self.next_button.setVisible(shown and many)
        if shown:
            self.top_bar.raise_()

    def _reveal_bars(self) -> None:
        if not self._bars_shown:
            self._set_bars(True)
        self._hide_timer.start()

    def _auto_hide(self) -> None:
        if not self._fullscreen:
            return
        if self.top_bar.underMouse() or self.commands.more_menu.isVisible():
            self._hide_timer.start()
            return
        self._set_bars(False)

    def _escape(self) -> None:
        if self.info_pane.is_renaming():
            self.info_pane.cancel_rename()
        elif self._fullscreen:
            self.fullscreen_requested.emit()
        elif not self.canvas.is_fitted():
            self.canvas.zoom_fit()
        elif self._from_gallery:
            self.back_requested.emit()

    # --- info pane, filmstrip ---
    def toggle_info(self) -> None:
        self.set_info_visible(not self.info_pane.isVisible())

    def set_info_visible(self, visible: bool) -> None:
        if self._fullscreen and visible:
            return
        self.info_pane.setVisible(visible)
        self.info_action.setChecked(visible)
        if visible:
            self._update_info()

    def toggle_filmstrip(self) -> None:
        self.set_filmstrip_visible(not self.host.settings.filmstrip)

    def set_filmstrip_visible(self, visible: bool, remember: bool = True) -> None:
        if remember:
            self.host.settings.filmstrip = visible
            self.host.save_settings()
        self.filmstrip_action.setChecked(visible)
        self.filmstrip.setVisible(visible and not self._fullscreen)

    def _update_info(self) -> None:
        if not self.info_pane.isVisible():
            return
        path = self.current_path()
        if path is None:
            self.info_pane.clear()
            return
        if self._loaded is None:
            self.info_pane.set_file(path, fileinfo.describe(path), renamable=True)
            return
        info = self._loaded.info
        groups = fileinfo.describe(path, width=info.width, height=info.height, fmt=info.format,
                                   bits_per_pixel=info.bits_per_pixel, has_alpha=info.has_alpha,
                                   dpi=self._loaded.dpi, frames=info.frames, animated=info.animated,
                                   vector=info.vector, exif=self._exif)
        place = None
        if self._exif is not None and self._exif.latitude is not None and self._exif.longitude is not None:
            place = (self._exif.latitude, self._exif.longitude)
        self.info_pane.set_file(path, groups, renamable=True, coordinates=place)

    def start_rename(self) -> None:
        if self.current_path() is None or self._fullscreen:
            return
        self.set_info_visible(True)
        self.info_pane.start_rename()

    def rename(self, new_name: str) -> None:
        path = self.current_path()
        if path is None:
            return
        target = os.path.join(os.path.dirname(path), new_name)
        self.file_worker.submit(rename_no_replace, path, target,
                                on_done=lambda _r, old=path, new=target: self._renamed(old, new),
                                on_error=self._rename_failed)

    def _rename_failed(self, error: Exception) -> None:
        if isinstance(error, FileExistsError):
            self.info_pane.show_rename_error("A file with this name is already here.")
        else:
            reason = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
            self.info_pane.show_rename_error(f"The file could not be renamed: {reason}.")

    def _renamed(self, old: str, new: str) -> None:
        self._forget(old)
        self.host.thumbs.invalidate(old)
        for index, item in enumerate(self._items):
            if item.path == old:
                self._items[index] = library.Item(new, item.mtime, item.size)
        current = self.current_path()
        self._items = library.by_name(self._items) if self._is_one_folder() else self._items
        if current is not None:
            self._index = [item.path for item in self._items].index(current)
        if self._loaded is not None and self._loaded.info.path == old:
            from dataclasses import replace
            self._loaded.info = replace(self._loaded.info, path=new)
            if self._loaded.info.animated:
                self.canvas.set_movie(new)
        self.filmstrip.set_items(self._items, self._index)
        self.info_pane.cancel_rename()
        self._update_title()
        self._update_info()
        self.files_changed.emit()

    def _is_one_folder(self) -> bool:
        return len({item.folder for item in self._items}) <= 1

    # --- commands ---
    def rotate(self, clockwise: bool = True) -> None:
        loaded = self._loaded
        if loaded is None or loaded.info.animated or loaded.info.vector:
            return
        from dataclasses import replace
        path = loaded.info.path
        turn = exif.ROTATE_CW if clockwise else exif.ROTATE_CCW
        loaded.image = decode.apply_orientation(loaded.image, turn)
        loaded.info = replace(loaded.info, width=loaded.info.height, height=loaded.info.width,
                              orientation=exif.compose_orientation(loaded.info.orientation, turn))
        if loaded.dpi is not None:
            loaded.dpi = (loaded.dpi[1], loaded.dpi[0])
        self.canvas.set_picture(loaded.image, QSize(loaded.info.width, loaded.info.height),
                                alpha=bool(loaded.info.has_alpha))
        self._update_info()
        if imageio.can_rotate_file(loaded.info):
            self.file_worker.submit(self._rotate_job, path, clockwise,
                                    on_done=lambda item, p=path: self._rotated(p, item),
                                    on_error=lambda exc: self.show_banner(
                                        "error", f"The turned picture could not be saved: {exc}"))
        else:
            self._view_turned = True
            self.show_banner("info", f"{formats.display_name(loaded.info.format)} pictures are turned for viewing "
                                     "only; use Save as to keep the turned picture.")

    @staticmethod
    def _rotate_job(path: str, clockwise: bool) -> library.Item:
        imageio.rotate_file(path, clockwise)
        return library.item_for(path)

    def _rotated(self, path: str, item: library.Item) -> None:
        self.host.thumbs.invalidate(path)
        for index, old in enumerate(self._items):
            if old.path == path:
                self._items[index] = item
        cached = self._cache.get(path)
        if cached is not None:
            self._forget(path)
        if self._loaded is not None and self._loaded.info.path == path and self.file_worker.pending() <= 1:
            self._remember(path, item.mtime, self._loaded, self._exif)
        self.filmstrip.set_items(self._items, self._index)
        self.files_changed.emit()

    def delete(self) -> None:
        path = self.current_path()
        if path is None:
            return
        name = os.path.basename(path)
        if self.host.settings.confirm_delete and not self.host.ask(
                "Move to the Trash?", f"“{name}” will be moved to the Trash. You can restore it from there.",
                "Move to Trash", "Cancel", default="confirm"):
            return
        self.file_worker.submit(trash.trash, path, on_done=lambda record, p=path: self._deleted(p, record),
                                on_error=lambda exc: self.show_banner("error", str(exc)))

    def _deleted(self, path: str, record: trash.TrashRecord) -> None:
        self._last_trash = record
        self._forget(path)
        self.host.thumbs.invalidate(path)
        paths = [item.path for item in self._items]
        if path in paths:
            index = paths.index(path)
            removed = self._items.pop(index)
            current = self._index
            self.filmstrip.set_items(self._items, -1)
            if not self._items:
                self._show_nothing("No more pictures", "The last picture of this list was moved to the Trash.")
            elif index == current:
                self._index = -1
                self.show_index(min(index, len(self._items) - 1), animate=False)
            else:
                self._index = current - 1 if index < current else current
                self.filmstrip.set_current(self._index, False)
                self._update_title()
            self._sync_actions()
            self.show_banner("success", f"“{os.path.basename(path)}” was moved to the Trash.", "Undo",
                             lambda item=removed, at=index, rec=record: self._undo_delete(item, at, rec))
        self.files_changed.emit()

    def _undo_delete(self, item: library.Item, at: int, record: trash.TrashRecord) -> None:
        self.file_worker.submit(trash.restore, record,
                                on_done=lambda _path, i=item, a=at: self._restored(i, a),
                                on_error=lambda exc: self.show_banner("error", str(exc)))

    def _restored(self, item: library.Item, at: int) -> None:
        self._items.insert(min(at, len(self._items)), item)
        self.filmstrip.set_items(self._items, -1)
        self._index = -1
        self.show_index(min(at, len(self._items) - 1), animate=False)
        self.files_changed.emit()

    def _mime_data(self) -> QMimeData | None:
        path = self.current_path()
        if path is None:
            return None
        data = QMimeData()
        url = QUrl.fromLocalFile(path)
        data.setUrls([url])
        data.setData("x-special/gnome-copied-files", b"copy\n" + bytes(url.toEncoded()))
        if self._loaded is not None:
            data.setImageData(self._loaded.image)
        return data

    def copy(self) -> None:
        """The picture and the address of its file go to the clipboard."""
        data = self._mime_data()
        if data is None:
            return
        QGuiApplication.clipboard().setMimeData(data)
        self.show_banner("success", "Copied the picture.")

    def copy_path(self) -> None:
        path = self.current_path()
        if path is not None:
            QGuiApplication.clipboard().setText(path)
            self.show_banner("success", "Copied the file name with its folder.")

    def _drag_out(self) -> None:
        data = self._mime_data()
        if data is None:
            return
        drag = QDrag(self)
        drag.setMimeData(data)
        item = self._items[self._index]
        thumbnail = self.host.thumbs.pixmap(item.path, item.mtime)
        if thumbnail is None and self._loaded is not None:
            thumbnail = QPixmap.fromImage(self._loaded.image.scaled(
                128, 128, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
        if thumbnail is not None:
            small = thumbnail.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            drag.setPixmap(small)
            drag.setHotSpot(QPoint(small.width() // 2, small.height() // 2))
        self.host.run_drag(drag)

    def show_in_files(self) -> None:
        path = self.current_path()
        if path is not None and not self.host.show_in_files(path):
            self.show_banner("warning", "No file manager could be opened.")

    # open with
    def _mime_type(self) -> str | None:
        path = self.current_path()
        if path is None:
            return None
        fmt = self._loaded.info.format if self._loaded is not None else formats.format_for_extension(
            formats.extension(path))
        for _ext, (key, mime) in formats.EXTENSIONS.items():
            if key == fmt:
                return mime
        return None

    def _request_apps(self, fmt: str) -> None:
        mime = self._mime_type()
        if mime is None or mime in self._apps:
            return
        self.worker.submit(openwith.applications_for, mime, exclude=(APP_ID,), key=f"apps:{mime}",
                           on_done=lambda apps, m=mime: self._apps.__setitem__(m, apps))

    def _fill_open_with(self) -> None:
        menu = self.open_with_menu
        menu.clear()
        mime = self._mime_type()
        path = self.current_path()
        apps = self._apps.get(mime or "", []) if path else []
        if mime is not None and mime not in self._apps:
            apps = openwith.applications_for(mime, exclude=(APP_ID,))
            self._apps[mime] = apps
        shown = 0
        for app in apps[:12]:
            arguments = openwith.command(app, [path])
            if arguments is None:
                continue
            action = menu.addAction(fluent.icon(app.icon) if app.icon and "/" not in app.icon else fluent.icon(""),
                                    app.name)
            action.triggered.connect(lambda _checked=False, a=arguments, n=app.name: self._launch(a, n))
            shown += 1
        if not shown:
            empty = menu.addAction("No other application for this file type")
            empty.setEnabled(False)

    def _launch(self, arguments: list[str], name: str) -> None:
        if not self.host.run_program(arguments):
            self.show_banner("error", f"{name} could not be started.")

    def _context_menu(self, point: QPoint) -> None:
        if self.current_path() is None:
            return
        menu = fluent.MenuFlyout(self)
        menu.add_item("Copy", self.copy, icon=action_icon("edit-copy"), shortcut="Ctrl+C",
                      enabled=self._loaded is not None)
        menu.add_item("Copy as path", self.copy_path, shortcut="Ctrl+Shift+C")
        menu.addSeparator()
        menu.addMenu(self.open_with_menu)
        menu.add_item("Show in Files", self.show_in_files)
        menu.add_item("Set as desktop background", self.set_as_wallpaper, enabled=self._loaded is not None)
        menu.addSeparator()
        menu.add_item("Rotate right", self.rotate, icon=action_icon("object-rotate-right"), shortcut="Ctrl+R",
                      enabled=self.rotate_action.isEnabled())
        menu.add_item("Edit", lambda: self.edit_requested.emit(), icon=action_icon("document-edit"),
                      shortcut="Ctrl+E", enabled=self._loaded is not None)
        menu.add_item("Delete", self.delete, icon=action_icon("edit-delete"), shortcut="Delete")
        menu.addSeparator()
        menu.add_item("File info", self.toggle_info, icon=fluent.icon("dialog-information-symbolic"),
                      shortcut="Alt+Return")
        self.host.popup_menu(menu, self.canvas.mapToGlobal(point))

    # saving
    def _source_exif(self) -> bytes | None:
        loaded = self._loaded
        if loaded is None or loaded.info.format != "jpeg":
            return None
        return imageio.read_exif_block(loaded.info.path)

    def save_as(self) -> None:
        loaded = self._loaded
        if loaded is None:
            return
        path = loaded.info.path
        fmt = loaded.info.format if loaded.info.format in imageio.writable_formats() else "png"
        stem = os.path.splitext(os.path.basename(path))[0]
        suggested = free_name(os.path.join(os.path.dirname(path), f"{stem}.{imageio.WRITERS[fmt][1]}"))
        target = self.host.choose_save_path("Save as", suggested, fmt)
        if not target:
            return
        chosen = imageio.format_for_path(target)
        if chosen is None:
            self.show_banner("error", "Give the file a name that ends in .jpg, .png, .webp, .bmp or .tif.")
            return
        quality = self.host.settings.jpeg_quality
        image = loaded.image
        same_kind = chosen == loaded.info.format and not self._view_turned

        def job() -> imageio.SaveReport:
            if same_kind and os.path.abspath(target) != os.path.abspath(path):
                # the same format: copy the file as it is, nothing is lost
                shutil.copyfile(path, target)
                return imageio.SaveReport(target, os.path.getsize(target), ("everything",), ())
            block = imageio.read_exif_block(path) if loaded.info.format == "jpeg" else None
            return imageio.save(image, target, chosen, quality, exif_block=block,
                                had_exif=self._exif is not None)

        self.file_worker.submit(job, on_done=self._saved, on_error=self._save_failed)

    def _saved(self, report: imageio.SaveReport) -> None:
        text = f"Saved “{os.path.basename(report.path)}” ({format_size(report.size)})."
        if report.lost:
            text += " Not kept: " + ", ".join(report.lost) + "."
        self.show_banner("success" if not report.lost else "info", text)
        self.host.thumbs.invalidate(report.path)
        self._forget(report.path)
        self.files_changed.emit()
        self._after_write(report.path)

    def _after_write(self, path: str) -> None:
        """A file was written: when it belongs to the folder on show, list it; when it is the one on show, reload."""
        current = self.current_path()
        if current is None:
            return
        if os.path.abspath(path) == os.path.abspath(current):
            self.reload()
        elif os.path.dirname(path) == os.path.dirname(current) and self._is_one_folder():
            try:
                item = library.item_for(path)
            except OSError:
                return
            self._items = library.by_name([i for i in self._items if i.path != path] + [item])
            self._index = [i.path for i in self._items].index(current)
            self.filmstrip.set_items(self._items, self._index)
            self._update_title()
            self._sync_actions()

    def _save_failed(self, error: Exception) -> None:
        if isinstance(error, OSError) and not isinstance(error, imageio.SaveError):
            text = f"The file could not be written: {error.strerror or error}."
        else:
            text = str(error)
        self.show_banner("error", text)

    def resize_picture(self) -> None:
        loaded = self._loaded
        if loaded is None:
            return
        info = loaded.info
        image = loaded.image
        fmt = info.format if info.format in imageio.writable_formats() else "png"
        dialog = ResizeDialog(self, image.size(), info.file_size, fmt, self.host.settings.jpeg_quality)

        def estimate() -> None:
            key = dialog.estimate_key()
            width, height, kind, quality = key
            if width * height > 100_000_000:
                return
            self.worker.submit(imageio.estimate_size, image, width, height, kind, quality,
                               on_done=lambda size, k=key: dialog.set_estimate(size, k) if dialog.is_open() else None,
                               on_error=lambda _exc: None)

        dialog.changed.connect(estimate)
        estimate()
        result = self.host.run_dialog(dialog)
        size, kind, quality = dialog.new_size(), dialog.format(), dialog.quality_value_()
        dialog.deleteLater()
        if result != fluent.ContentDialog.Result.PRIMARY:
            return
        stem = os.path.splitext(os.path.basename(info.path))[0]
        suggested = free_name(os.path.join(os.path.dirname(info.path),
                                           f"{stem} ({size.width()}x{size.height()}).{imageio.WRITERS[kind][1]}"))
        target = self.host.choose_save_path("Save resized copy", suggested, kind)
        if not target:
            return
        chosen = imageio.format_for_path(target) or kind

        def job() -> imageio.SaveReport:
            block = imageio.read_exif_block(info.path) if info.format == "jpeg" else None
            return imageio.save(editops.resize(image, size.width(), size.height()), target, chosen, quality,
                                exif_block=block, had_exif=self._exif is not None)

        self.file_worker.submit(job, on_done=self._saved, on_error=self._save_failed)

    def print_picture(self) -> None:
        loaded = self._loaded
        if loaded is None or not printing.AVAILABLE:
            return
        image = loaded.image
        dpi = self._exif.dpi()[0] if self._exif is not None and self._exif.dpi() else (
            loaded.dpi[0] if loaded.dpi else None)
        dialog = PrintDialog(self, image, dpi)
        self.worker.submit(printing.printers,
                           on_done=lambda found: dialog.set_printers(found) if dialog.is_open() else None,
                           on_error=lambda _exc: dialog.set_printers([]) if dialog.is_open() else None)
        result = self.host.run_dialog(dialog)
        printer, mode, orientation, copies = dialog.printer(), dialog.mode(), dialog.orientation(), dialog.copies()
        dialog.deleteLater()
        if result != fluent.ContentDialog.Result.PRIMARY:
            return
        pdf_path = None
        if printer == printing.PDF_PRINTER:
            stem = os.path.splitext(os.path.basename(loaded.info.path))[0]
            pdf_path = self.host.choose_save_path(
                "Save as PDF", free_name(os.path.join(os.path.dirname(loaded.info.path), f"{stem}.pdf")), "pdf")
            if not pdf_path:
                return
        self.show_banner("info", "Printing…" if pdf_path is None else "Writing the PDF file…")
        self.file_worker.submit(
            printing.print_image, image, printer_name=None if pdf_path else printer, pdf_path=pdf_path, mode=mode,
            orientation=orientation, copies=copies, picture_dpi=dpi or printing.DEFAULT_PICTURE_DPI,
            on_done=lambda _r: self.show_banner(
                "success", "The picture was sent to the printer." if pdf_path is None
                else f"Saved “{os.path.basename(pdf_path)}”."),
            on_error=lambda exc: self.show_banner("error", str(exc)))

    def set_as_wallpaper(self) -> None:
        loaded = self._loaded
        if loaded is None:
            return
        self.file_worker.submit(wallpaper_module.set_wallpaper, loaded.info.path, loaded.image, loaded.info,
                                self.host.compositor_runner, on_done=self._wallpaper_set,
                                on_error=self._wallpaper_failed)

    def _wallpaper_set(self, result: wallpaper_module.WallpaperResult) -> None:
        if result.reloaded:
            self.show_banner("success", "This picture is now the desktop background.")
        else:
            self.show_banner("info", "This picture is set as the desktop background; it will show the next time "
                                     f"the desktop starts ({result.note}).")

    def _wallpaper_failed(self, error: Exception) -> None:
        if isinstance(error, (ConfigError, imageio.SaveError)):
            text = str(error)
        elif isinstance(error, OSError):
            text = f"The picture could not be copied: {error.strerror or error}."
        else:
            text = "The desktop background could not be changed."
        self.show_banner("error", f"The desktop background was not changed. {text}")

    def shutdown(self) -> None:
        self._load_timer.stop()
        self._hide_timer.stop()
        self.canvas.shutdown()
        self.worker.shutdown()
        self.file_worker.shutdown()
