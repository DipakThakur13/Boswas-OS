"""PhotosWindow: the window of Photos and the hub of its pages.

The window holds a stack of four pages: the navigation view (Gallery,
Folders, Settings), the viewer, the editor and the slideshow. It owns what
the pages share (settings, the thumbnail provider, the list of pictures of
the gallery) and everything that asks the user or the system something
(questions, file choosers, starting programs), as methods the tests
replace.
"""

from __future__ import annotations

import os
import subprocess
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QFileDialog, QStackedWidget, QWidget

import boswas_fluent as fluent

from .. import APP_ID, DISPLAY_NAME, cli, decode, imageio, library, openwith, thumbcache
from .. import settings as settings_module
from ..thumbs import ThumbnailProvider
from .editor import EditorPage
from .gallery import FoldersPage, PictureGrid, SettingsPage
from .slideshow import SlideshowPage
from .viewer import ViewerPage, show_in_file_manager

_windows: list["PhotosWindow"] = []          # every open window of this process


def _scan_job(roots: list[str], extensions, cancelled: threading.Event) -> list[library.Item]:
    found = library.scan(roots, extensions, cancelled=cancelled.is_set, skip=[str(thumbcache.cache_root())])
    return library.newest_first(found)


def _resolve_job(paths: list[str], extensions) -> tuple[str, object]:
    """What a list of paths from the command line or a drop means: one file (show it in its folder),
    or a list of pictures."""
    if len(paths) == 1 and not os.path.isdir(paths[0]):
        return "file", paths[0]
    items: list[library.Item] = []
    seen: set[str] = set()
    for path in paths:
        if os.path.isdir(path):
            try:
                found = library.folder_items(path, extensions)
            except OSError:
                found = []
        else:
            try:
                found = [library.item_for(path)]
            except OSError:
                found = [library.Item(os.path.abspath(path), 0.0, 0)]
        for item in found:
            if item.path not in seen:
                seen.add(item.path)
                items.append(item)
    return "list", items


class PhotosWindow(fluent.FluentWindow):
    """The Photos window. ``arguments`` are the command line arguments without the program name."""

    def __init__(self, arguments: list[str] | None = None) -> None:
        super().__init__(APP_ID, DISPLAY_NAME, icon="boswas-photos", size=(1180, 760), minimum_size=(640, 440))
        decode.configure()
        self.settings_path = fluent.config_dir(APP_ID) / "settings.json"
        self.settings = settings_module.load(self.settings_path)
        self.pictures_dir = library.pictures_dir()
        self.worker = fluent.Worker(self, max_threads=2)
        self.thumbs = ThumbnailProvider(self)
        self.compositor_runner = subprocess.run
        self.library: list[library.Item] = []
        self._scan_cancel = threading.Event()
        self._scanning = False
        self._scan_wanted = False
        self._scanned = False
        self._was_maximized = False
        self._slideshow_from: QWidget | None = None
        self._slideshow_was_fullscreen = False
        self._closing = False

        self.stack = QStackedWidget(self)
        self.set_content(self.stack)
        self.navigation = fluent.NavigationView(self.stack, pane_toggle=True, pane_width=240)
        self.gallery_page = PictureGrid(self, by_month=True)
        self.folders_page = FoldersPage(self)
        self.navigation.add_page("gallery", "Gallery", self.gallery_page, icon="image-x-generic",
                                 tooltip="All your pictures, newest first")
        self.navigation.add_page("folders", "Folders", self.folders_page, icon="folder",
                                 tooltip="Your pictures by folder")
        self.navigation.add_page("settings", "Settings", icon="preferences-system", footer=True,
                                 factory=self._make_settings)
        self.settings_page: SettingsPage | None = None
        self.viewer = ViewerPage(self, self.stack)
        self.editor = EditorPage(self, self.stack)
        self.slideshow = SlideshowPage(self, self.stack)
        for page in (self.navigation, self.viewer, self.editor, self.slideshow):
            self.stack.addWidget(page)
        self.stack.setCurrentWidget(self.navigation)

        for grid in (self.gallery_page, self.folders_page.grid):
            grid.open_requested.connect(self._open_from_grid)
            grid.slideshow_requested.connect(lambda items: self.start_slideshow(items, 0))
            grid.open_file_requested.connect(self.open_dialog)
            grid.refresh_requested.connect(self.rescan)
            grid.add_folder_requested.connect(self.add_folder)
            grid.files_changed.connect(self._files_gone)
        self.folders_page.add_folder_requested.connect(self.add_folder)
        self.viewer.back_requested.connect(self.show_gallery)
        self.viewer.edit_requested.connect(self.edit_current)
        self.viewer.slideshow_requested.connect(self._slideshow_from_viewer)
        self.viewer.fullscreen_requested.connect(self.toggle_fullscreen)
        self.viewer.settings_requested.connect(self.show_settings)
        self.viewer.open_requested.connect(self.open_dialog)
        self.viewer.files_changed.connect(self._viewer_changed_files)
        self.editor.closed.connect(self._editor_closed)
        self.editor.saved.connect(self._editor_saved)
        self.slideshow.finished.connect(self._slideshow_finished)

        close = QShortcut(QKeySequence("Ctrl+W"), self)
        close.activated.connect(self.close)
        fullscreen = QShortcut(QKeySequence("F11"), self.navigation)
        fullscreen.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        fullscreen.activated.connect(self.toggle_fullscreen)
        self.setAcceptDrops(True)
        self._apply_settings()
        _windows.append(self)

        parsed = cli.parse(list(arguments or []))
        if parsed.paths and not parsed.gallery:
            self.open_paths(parsed.paths, slideshow=parsed.slideshow)
        else:
            QTimer.singleShot(0, self.rescan)

    # --- settings ---
    def _make_settings(self) -> SettingsPage:
        page = SettingsPage(self)
        page.folders_changed.connect(self.rescan)
        page.changed.connect(self._apply_settings)
        page.add_folder_requested.connect(self.add_folder)
        self.settings_page = page
        return page

    def save_settings(self) -> None:
        try:
            settings_module.save(self.settings, self.settings_path)
        except OSError as exc:
            self.gallery_page.banner.set_action(None)
            self.gallery_page.banner.show_message(
                "warning", f"Your settings could not be saved: {exc.strerror or exc}.")

    def _apply_settings(self) -> None:
        self.viewer.canvas.wheel_mode = self.settings.wheel
        self.viewer.set_filmstrip_visible(self.settings.filmstrip, remember=False)

    # --- hooks: everything that asks the user or the system (tests replace these) ---
    def run_dialog(self, dialog: fluent.ContentDialog):
        return dialog.exec()

    def popup_menu(self, menu, position) -> None:
        menu.exec(position)
        menu.deleteLater()

    def run_drag(self, drag) -> None:
        drag.exec(Qt.DropAction.CopyAction)

    def run_program(self, arguments: list[str]) -> bool:
        return openwith.launch(arguments)

    def show_in_files(self, path: str) -> bool:
        return show_in_file_manager(path)

    def choose_save_path(self, title: str, suggested: str, fmt: str) -> str | None:
        """Ask where to save. ``fmt`` is the format offered first ("pdf" for a PDF file)."""
        if fmt == "pdf":
            filters = ["PDF (*.pdf)"]
            selected = filters[0]
            extensions = {filters[0]: "pdf"}
        else:
            entries = imageio.file_filters()
            filters = [text for _key, text in entries]
            selected = next((text for key, text in entries if key == fmt), filters[0] if filters else "")
            extensions = {text: imageio.WRITERS[key][1] for key, text in entries}
        path, chosen = QFileDialog.getSaveFileName(self, title, suggested, ";;".join(filters), selected)
        if not path:
            return None
        if not os.path.splitext(path)[1] and chosen in extensions:
            path += "." + extensions[chosen]
        return path

    def choose_open_paths(self) -> list[str]:
        patterns = " ".join(f"*.{extension}" for extension in sorted(decode.supported_extensions()))
        start = os.path.dirname(self.viewer.current_path() or "") or self.pictures_dir
        paths, _filter = QFileDialog.getOpenFileNames(self, "Open pictures", start,
                                                      f"Pictures ({patterns});;All files (*)")
        return list(paths)

    def choose_folder(self) -> str | None:
        return QFileDialog.getExistingDirectory(self, "Add a folder", self.pictures_dir) or None

    # --- the gallery ---
    def gallery_roots(self) -> list[str]:
        return self.settings.gallery_roots(self.pictures_dir)

    def rescan(self) -> None:
        """Look for the pictures of the gallery again (in the background)."""
        if self._closing:
            return
        if self._scanning:
            self._scan_wanted = True
            self._scan_cancel.set()
            return
        self._scanning = True
        self._scan_wanted = False
        self._scan_cancel = threading.Event()
        self._show_library(scanning=True)
        self.worker.submit(_scan_job, self.gallery_roots(), decode.supported_extensions(), self._scan_cancel,
                           on_done=self._scan_done, on_error=lambda _exc: self._scan_done([]))

    def _scan_done(self, items: list[library.Item]) -> None:
        self._scanning = False
        if self._scan_wanted:
            self.rescan()
            return
        self._scanned = True
        self.library = items
        self._show_library(scanning=False)

    def is_scanning(self) -> bool:
        return self._scanning

    def _show_library(self, scanning: bool) -> None:
        self.gallery_page.set_items(self.library, scanning=scanning)
        self.folders_page.set_items(self.library, scanning=scanning)

    def _files_gone(self, paths: list[str]) -> None:
        gone = set(paths)
        self.library = [item for item in self.library if item.path not in gone]
        self._show_library(scanning=self._scanning)

    def _viewer_changed_files(self) -> None:
        if self._scanned:
            self.rescan()

    def add_folder(self) -> None:
        folder = self.choose_folder()
        if not folder:
            return
        folder = os.path.normpath(os.path.abspath(folder))
        if not os.path.isdir(folder):
            return
        if folder not in self.settings.folders and folder != os.path.normpath(self.pictures_dir):
            self.settings.folders.append(folder)
            self.save_settings()
        if self.settings_page is not None:
            self.settings_page.refresh_folders()
        self.rescan()

    # --- pages ---
    def current_page(self) -> str:
        widget = self.stack.currentWidget()
        return {self.navigation: "gallery", self.viewer: "viewer", self.editor: "editor",
                self.slideshow: "slideshow"}.get(widget, "gallery")

    def show_gallery(self) -> None:
        if self.isFullScreen() and self.current_page() == "viewer":
            self.toggle_fullscreen()
        self.stack.setCurrentWidget(self.navigation)
        self.setWindowTitle(DISPLAY_NAME)
        if not self._scanned and not self._scanning:
            self.rescan()
        page = self.navigation.page(self.navigation.current() or "gallery")
        target = self.gallery_page.grid if page is self.gallery_page else page
        if target is not None:
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    def show_page(self, key: str) -> None:
        """Show a page of the navigation view (``gallery``, ``folders``, ``settings``).

        The keyboard focus is put on the pane first. Without that, a page
        that holds the focus loses it while it is being hidden, the pane's
        list takes it, selects its first item and shows that page inside the
        switch that is under way.
        """
        footer = key == "settings"
        (self.navigation.footer_list if footer else self.navigation.list).setFocus(Qt.FocusReason.OtherFocusReason)
        self.navigation.set_current(key)

    def show_settings(self) -> None:
        self.show_gallery()
        self.show_page("settings")

    def show_viewer(self) -> None:
        self.stack.setCurrentWidget(self.viewer)
        self.viewer.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def _open_from_grid(self, items: list[library.Item], index: int) -> None:
        self.viewer.set_from_gallery(True)
        self.viewer.show_items(items, index)
        self.show_viewer()

    def open_paths(self, paths: list[str], *, slideshow: bool = False) -> None:
        """Show files and folders given on the command line, dropped, or chosen in the file dialog."""
        paths = [path for path in paths if path]
        if not paths:
            return
        self.worker.submit(_resolve_job, paths, decode.supported_extensions(),
                           on_done=lambda result, s=slideshow: self._resolved(result, s))

    def _resolved(self, result, slideshow: bool) -> None:
        kind, value = result
        self.viewer.set_from_gallery(False)
        if self.current_page() == "editor":
            if self.editor.is_dirty() and not self.ask(
                    "Discard your changes?", "Another picture is about to be opened; the picture you are editing "
                    "has changes that were not saved.", "Discard", "Keep editing"):
                return
            self.editor.finish()
        if self.current_page() == "slideshow":
            self.slideshow.stop()
        if kind == "file":
            self.viewer.open_file(value)
        else:
            self.viewer.show_items(value, 0)
        self.show_viewer()
        if slideshow and self.viewer.items():
            QTimer.singleShot(0, lambda: self.start_slideshow(self.viewer.items(), max(0, self.viewer.index())))

    def open_dialog(self) -> None:
        paths = self.choose_open_paths()
        if paths:
            self.open_paths(paths)

    def handle_arguments(self, arguments: list[str]) -> None:
        """Another start of the program passes its arguments on (see ``fluent.run``)."""
        parsed = cli.parse(list(arguments))
        target = max(_windows, key=lambda window: window.isActiveWindow()) if _windows else self
        if parsed.new_window or (self.settings.new_window and parsed.paths):
            window = PhotosWindow(arguments=[*(["--slideshow"] if parsed.slideshow else []), *parsed.paths])
            window.show()
            fluent.activate_window(window)
            return
        if parsed.paths and not parsed.gallery:
            target.open_paths(parsed.paths, slideshow=parsed.slideshow)
        elif parsed.gallery:
            target.show_gallery()
        if target is not self:
            fluent.activate_window(target)

    # --- editing ---
    def edit_current(self) -> None:
        loaded = self.viewer.loaded()
        if loaded is None:
            return
        if self.isFullScreen():
            self.toggle_fullscreen()
        self.stack.setCurrentWidget(self.editor)
        self.editor.start(loaded, had_exif=self.viewer.current_exif() is not None)
        self.setWindowTitle(f"Edit - {os.path.basename(loaded.info.path)} - {DISPLAY_NAME}")

    def _editor_closed(self) -> None:
        if self.current_page() == "editor":
            self.show_viewer()
            self.viewer._update_title()

    def _editor_saved(self, report: imageio.SaveReport) -> None:
        self.editor.finish()
        self.thumbs.invalidate(report.path)
        self.viewer._forget(report.path)
        current = self.viewer.current_path()
        text = f"Saved “{os.path.basename(report.path)}”."
        if report.kept:
            text += " Kept: " + ", ".join(report.kept) + "."
        if report.lost:
            text += " Not kept: " + ", ".join(report.lost) + "."
        if current is not None and os.path.abspath(report.path) == os.path.abspath(current):
            self.viewer.reload()
        elif current is not None and os.path.dirname(report.path) == os.path.dirname(current):
            self.viewer._after_write(report.path)
            paths = [item.path for item in self.viewer.items()]
            if report.path in paths:
                self.viewer.show_index(paths.index(report.path), animate=False)
        else:
            self.viewer.open_file(report.path)
        self.viewer.show_banner("success", text)
        if self._scanned:
            self.rescan()

    # --- slideshow and full screen ---
    def toggle_fullscreen(self) -> None:
        if self.current_page() == "slideshow":
            self.slideshow.stop()
            return
        if self.isFullScreen():
            self._leave_fullscreen()
            self.viewer.set_fullscreen(False)
        else:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
            self.viewer.set_fullscreen(self.current_page() == "viewer")

    def _leave_fullscreen(self) -> None:
        if self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()

    def _slideshow_from_viewer(self) -> None:
        self.start_slideshow(self.viewer.items(), max(0, self.viewer.index()))

    def start_slideshow(self, items: list[library.Item], first: int = 0) -> None:
        if not items:
            return
        if self.current_page() != "slideshow":
            self._slideshow_from = self.stack.currentWidget()
            self._slideshow_was_fullscreen = self.isFullScreen()
            if not self._slideshow_was_fullscreen:
                self._was_maximized = self.isMaximized()
        self.stack.setCurrentWidget(self.slideshow)
        if not self.isFullScreen():
            self.showFullScreen()
        self.setWindowTitle(f"Slideshow - {DISPLAY_NAME}")
        self.slideshow.start(items, first)

    def _slideshow_finished(self) -> None:
        if self.current_page() != "slideshow":
            return
        back = self._slideshow_from or self.navigation
        last = self.slideshow.current_item()
        if not self._slideshow_was_fullscreen and self.isFullScreen():
            self._leave_fullscreen()
        self.stack.setCurrentWidget(back)
        if back is self.viewer:
            self.viewer.set_fullscreen(self.isFullScreen())
            if last is not None:
                paths = [item.path for item in self.viewer.items()]
                if last.path in paths:
                    self.viewer.show_index(paths.index(last.path), animate=False)
            self.viewer._update_title()
            self.viewer.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.setWindowTitle(DISPLAY_NAME)

    # --- drag and drop ---
    @staticmethod
    def _dropped_paths(mime) -> list[str]:
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls()[:cli.MAX_PATHS] if url.isLocalFile()]

    def dragEnterEvent(self, event) -> None:
        if event.source() is None and self._dropped_paths(event.mimeData()) and self.current_page() != "editor":
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        paths = self._dropped_paths(event.mimeData())
        if paths and self.current_page() != "editor":
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.open_paths(paths)
        else:
            event.ignore()

    # --- closing ---
    def closeEvent(self, event) -> None:
        if self.current_page() == "editor" and self.editor.is_dirty():
            if not self.ask("Discard your changes?", "The picture has changes that were not saved.", "Discard",
                            "Keep editing"):
                event.ignore()
                return
        if self.isFullScreen():
            # so the remembered geometry is that of the window, not of the screen
            self._leave_fullscreen()
        self._closing = True
        self._scan_cancel.set()
        self.viewer.shutdown()
        self.editor.shutdown()
        self.slideshow.shutdown()
        self.thumbs.shutdown()
        self.worker.shutdown()
        if self in _windows:
            _windows.remove(self)
        super().closeEvent(event)
