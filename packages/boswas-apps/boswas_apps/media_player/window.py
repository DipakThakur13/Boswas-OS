"""The main window of Media Player."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFileDialog, QFormLayout, QLineEdit, QStackedWidget, QWidget

import boswas_fluent as fluent

from . import APP_ID, __version__, glyphs, model, opening, playlist_io
from .backend import FrameGrabber, PlayerBackend
from .controller import SEEK_STEP_MS, VOLUME_STEP, PlayerController
from .covers import ART_SIZE, Artwork
from .model import PlaylistStore, Settings, Track, format_time
from .pages import HomePage, MusicPage, PlaylistsPage, QueuePage, SettingsPage, VideoPage, url_dialog_content
from .services import LibraryManager
from .transport import MiniPlayer, NowPlayingBar, NowPlayingPage

HIDE_CONTROLS_MS = 3000
_AUDIO = " ".join(f"*{extension}" for extension in model.AUDIO_EXTENSIONS)
_VIDEO = " ".join(f"*{extension}" for extension in model.VIDEO_EXTENSIONS)
_LISTS = " ".join(f"*{extension}" for extension in model.PLAYLIST_EXTENSIONS)


def describe_error(error: Exception) -> str:
    """An exception in plain words."""
    if isinstance(error, OSError) and error.strerror:
        name = f" ({error.filename})" if error.filename else ""
        return f"{error.strerror}{name}"
    return str(error) or error.__class__.__name__


class _Shell(QWidget):
    """The content of the window: the pages, the now-playing bar under (or, in full screen, over) them, messages."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.pages = QStackedWidget(self)
        self.bar: NowPlayingBar | None = None
        self.toast: fluent.InfoBar | None = None
        self.bar_overlays = False
        self.bar_wanted = True

    def arrange(self) -> None:
        width, height = self.width(), self.height()
        bar_height = self.bar.height() if self.bar is not None and self.bar_wanted else 0
        if self.bar is not None:
            self.bar.setVisible(self.bar_wanted and (not self.bar_overlays or self.bar.isVisible()))
            self.bar.setGeometry(0, height - self.bar.height(), width, self.bar.height())
            self.bar.raise_()
        self.pages.setGeometry(0, 0, width, height - (0 if self.bar_overlays else bar_height))
        if self.toast is not None:
            toast_width = min(720, width - 32)
            toast_height = max(self.toast.minimumHeight(), self.toast.heightForWidth(toast_width)
                               if self.toast.hasHeightForWidth() else self.toast.sizeHint().height())
            self.toast.setGeometry((width - toast_width) // 2, height - bar_height - toast_height - 12, toast_width,
                                   toast_height)
            self.toast.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.arrange()


class MainWindow(fluent.FluentWindow):
    """Navigation on the left, the pages, and the now-playing bar across the bottom."""

    playlists_changed = Signal()
    MEDIA_FILTER = f"Media files ({_AUDIO} {_VIDEO} {_LISTS});;Audio ({_AUDIO});;Video ({_VIDEO});;All files (*)"

    def __init__(self, arguments: list[str] | None = None, *, backend: PlayerBackend | None = None,
                 grabber: FrameGrabber | None = None, mpris_connection=None, use_mpris: bool = True,
                 backend_info: Callable[[], dict] | None = None, scan: bool = True) -> None:
        super().__init__(APP_ID, "Media Player", icon="multimedia-player", size=(1180, 760), minimum_size=(480, 420))
        self.version = __version__
        self.setAcceptDrops(True)
        self._backend_info = backend_info
        self._fullscreen = False
        self._was_maximized = False
        self._mini = False
        self._normal_geometry = None
        self._cover_key = ""

        config, state, cache = fluent.config_dir(APP_ID), fluent.state_dir(APP_ID), fluent.cache_dir(APP_ID)
        self.settings_path = config / "settings.json"
        self.settings = Settings.load(self.settings_path)
        if backend is None:
            from .backend import QtPlayerBackend
            backend = QtPlayerBackend(self)
        if grabber is None:
            from .backend import QtFrameGrabber
            grabber = QtFrameGrabber(self)
        self.grabber = grabber
        self.controller = PlayerController(backend, self.settings, state, self.settings_path, self)
        self.worker = fluent.Worker(self)
        self.library = LibraryManager(self.settings, state, self.worker, self)
        self.artwork = Artwork(cache, self.worker, grabber, self)
        self.playlists = PlaylistStore(config / "playlists")

        # --- layout ---
        self.shell = _Shell(self)
        self.set_content(self.shell)
        self.navigation = fluent.NavigationView(self.shell, pane_toggle=True, pane_width=260, compact_below=900)
        nav = self.navigation
        self.home_page = HomePage(self)
        self.music_page = MusicPage(self)
        self.video_page = VideoPage(self)
        self.queue_page = QueuePage(self)
        self.playlists_page = PlaylistsPage(self)
        nav.add_page("home", "Home", self.home_page, icon=glyphs.icon("home"))
        nav.add_page("music", "Music library", self.music_page, icon="folder-music", keywords=("songs", "albums"))
        nav.add_page("video", "Video library", self.video_page, icon="folder-videos")
        nav.add_separator()
        nav.add_page("queue", "Play queue", self.queue_page, icon=glyphs.icon("queue"))
        nav.add_page("playlists", "Playlists", self.playlists_page, icon=glyphs.icon("playlist"))
        nav.add_page("settings", "Settings", icon="preferences-system", factory=lambda: SettingsPage(self),
                     footer=True)
        self.now_playing = NowPlayingPage(self.controller, self.shell)
        self.mini_player = MiniPlayer(self.controller, self.shell)
        for page in (nav, self.now_playing, self.mini_player):
            self.shell.pages.addWidget(page)
        self.bar = NowPlayingBar(self.controller, self.shell)
        self.shell.bar = self.bar
        self.toast = fluent.InfoBar("info", parent=self.shell)
        self.toast.hide()
        self.shell.toast = self.toast
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast.hide)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(HIDE_CONTROLS_MS)
        self._hide_timer.timeout.connect(self._hide_controls)

        # --- wiring ---
        controller = self.controller
        controller.message.connect(self.notify)
        controller.track_changed.connect(self._track_changed)
        controller.track_updated.connect(lambda _track: self._update_cover())
        controller.video_changed.connect(self._video_changed)
        controller.state_changed.connect(lambda _state: self._wake_controls())
        self.artwork.loaded.connect(self._artwork_loaded)
        self.library.failed.connect(lambda text: self.notify("error", f"Couldn't read the libraries: {text}"))
        self.bar.show_now_playing.connect(self.show_now_playing)
        self.bar.fullscreen_requested.connect(self.toggle_fullscreen)
        self.bar.mini_requested.connect(self.toggle_mini)
        self.bar.activity.connect(self._wake_controls)
        self.now_playing.back_requested.connect(self.leave_now_playing)
        self.now_playing.video.double_clicked.connect(self.toggle_fullscreen)
        self.now_playing.video.activity.connect(self._wake_controls)
        self.mini_player.leave_requested.connect(self.toggle_mini)
        for view in (nav.list, nav.footer_list):
            view.installEventFilter(self)

        for keys, slot in (("Ctrl+P", controller.toggle), ("Ctrl+Right", controller.next),
                           ("Ctrl+Left", controller.previous),
                           ("Ctrl+Up", lambda: controller.change_volume(VOLUME_STEP)),
                           ("Ctrl+Down", lambda: controller.change_volume(-VOLUME_STEP)),
                           ("Ctrl+O", self.open_files_dialog), ("Ctrl+Shift+O", self.open_folder_dialog),
                           ("Ctrl+U", self.open_url_dialog), ("Ctrl+H", controller.toggle_shuffle),
                           ("Ctrl+T", controller.cycle_repeat), ("Ctrl+M", self.toggle_mini),
                           ("F11", self.toggle_fullscreen), ("Ctrl+F", self.focus_search),
                           ("Ctrl+N", self.new_playlist), ("Alt+Left", self.go_back), ("F5", self.refresh_libraries),
                           ("Ctrl+W", self.close), ("Ctrl+Q", self.close)):
            self.add_shortcut(keys, slot)

        self.mpris = None
        if use_mpris:
            try:
                from .mpris import MprisService
                self.mpris = MprisService(controller, self, connection=mpris_connection,
                                          raise_window=lambda: fluent.activate_window(self),
                                          quit_application=self.close)
                self.mpris.start()
            except ImportError:           # QtDBus is not installed: the desktop cannot control the player
                self.mpris = None

        if not backend.available():
            self.notify("error", backend.unavailable_reason() or "Playback is not available on this system.")
        locations = self._locations(arguments or [])
        if locations:
            controller.open_locations(locations, "play")
        else:
            controller.restore_state()
        if scan:
            QTimer.singleShot(0, self.library.start)
        self._update_cover()

    # --- arguments, drops ---
    @staticmethod
    def _locations(arguments: list[str]) -> list[str]:
        found = []
        for argument in arguments:
            if isinstance(argument, str) and argument.startswith("--"):
                continue
            location = opening.to_location(argument)
            if location is not None:
                found.append(location)
        return found

    def handle_arguments(self, arguments: list[str]) -> None:
        """Another start of the program: what it was given goes to the end of the play queue."""
        locations = self._locations(arguments)
        if locations:
            self.controller.open_locations(locations, "enqueue")

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        locations = self._locations([bytes(url.toEncoded()).decode("ascii", "replace")
                                     for url in event.mimeData().urls()[:500]])
        if locations:
            event.acceptProposedAction()
            self.controller.open_locations(locations, "play")

    # --- shared services of the pages ---
    def notify(self, severity: str, text: str) -> None:
        """Show a message above the now-playing bar; errors stay until closed."""
        self.toast.show_message(severity, text)
        self.shell.arrange()
        self._toast_timer.stop()
        if severity != "error":
            self._toast_timer.start(12_000 if severity == "warning" else 6000)

    def run(self, function: Callable, *arguments, done: Callable | None = None, what: str = "do that") -> None:
        """Run ``function`` on the worker; a failure is told as "Couldn't <what>: <reason>"."""
        self.worker.submit(function, *arguments, on_done=done,
                           on_error=lambda error: self.notify("error", f"Couldn't {what}: {describe_error(error)}"))

    @staticmethod
    def expand(paths: list[str]) -> opening.Opened:
        return opening.expand([location for location in map(opening.to_location, paths) if location])

    def choose_files(self, title: str, file_filter: str) -> list[str]:
        paths, _selected = QFileDialog.getOpenFileNames(self, title, self._dialog_dir(), file_filter)
        return list(paths)

    def choose_folder(self, title: str) -> str:
        return QFileDialog.getExistingDirectory(self, title, self._dialog_dir())

    def choose_save(self, title: str, name: str, file_filter: str) -> str:
        path, _selected = QFileDialog.getSaveFileName(self, title, os.path.join(self._dialog_dir(), name), file_filter)
        return path

    def _dialog_dir(self) -> str:
        folders = self.settings.music_folders + self.settings.video_folders
        return next((folder for folder in folders if os.path.isdir(folder)), str(Path.home()))

    def ask_text(self, title: str, label: str, initial: str, confirm: str,
                 validate: Callable[[str], str | None]) -> str | None:
        """Ask for one line of text; ``validate`` returns what is wrong with it or None. None when cancelled."""
        dialog = fluent.ContentDialog(self, title, primary=confirm)
        edit = QLineEdit(initial, dialog)
        edit.setAccessibleName(label)
        edit.setMaxLength(100)
        edit.setMinimumWidth(360)
        error = fluent.Label("", "caption", dialog, tone="danger", wrap=True)
        error.hide()
        dialog.set_content(edit)
        dialog.set_content(error)
        self.text_dialog = (dialog, edit, error)
        edit.returnPressed.connect(dialog.accept)
        try:
            while True:
                QTimer.singleShot(0, lambda: (edit.setFocus(), edit.selectAll()))
                if dialog.exec() != fluent.ContentDialog.Result.PRIMARY:
                    return None
                problem = validate(edit.text())
                if problem is None:
                    return " ".join(edit.text().split())
                error.setText(problem)
                error.show()
        finally:
            self.text_dialog = None
            dialog.deleteLater()

    text_dialog = None
    url_dialog = None

    def track_menu(self, tracks: list[Track]) -> fluent.MenuFlyout:
        """The context menu of one or more tracks."""
        controller = self.controller
        menu = fluent.MenuFlyout(self)
        menu.add_item("Play", lambda: controller.play_tracks(tracks), icon=glyphs.icon("play"))
        self.fill_add_menu(menu, tracks)
        if len(tracks) == 1 and not tracks[0].is_remote:
            menu.addSeparator()
            menu.add_item("Open file location", lambda: self.open_location(tracks[0]), icon="folder-open")
            menu.add_item("Properties", lambda: self.show_properties(tracks[0]))
        return menu

    def fill_add_menu(self, menu: fluent.MenuFlyout, tracks: list[Track]) -> None:
        menu.add_item("Play next", lambda: self.controller.play_next(tracks))
        menu.add_item("Add to queue", lambda: self.controller.enqueue(tracks), icon=glyphs.icon("queue"))
        self.fill_playlist_menu(menu.add_submenu("Add to playlist", glyphs.icon("playlist")), tracks)

    def fill_playlist_menu(self, menu: fluent.MenuFlyout, tracks: list[Track]) -> None:
        menu.add_item("New playlist…", lambda: self.playlists_page.new_playlist(tracks), icon="list-add-symbolic")
        names = self.playlists_page.names()
        if names:
            menu.addSeparator()
        for name in names[:40]:
            menu.add_item(name, lambda name=name: self.playlists_page.add_tracks(name, tracks))

    def open_location(self, track: Track) -> None:
        folder = os.path.dirname(track.location)
        if not fluent.open_path(folder):
            self.notify("warning", f"Couldn't open {folder}.")

    def show_properties(self, track: Track) -> None:
        dialog = fluent.ContentDialog(self, "Properties", primary=None, close="Close", default="close")
        form = QFormLayout()
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(8)
        rows = [("Title", track.display_title), ("Artist", track.artist), ("Album", track.album),
                ("Length", format_time(track.duration_ms) if track.duration_ms else ""),
                ("Kind", "Video" if track.video else "Audio"), ("Location", track.location)]
        for name, value in rows:
            if value:
                form.addRow(fluent.Label(name, "body", dialog, tone="secondary"),
                            fluent.Label(value, "body", dialog, wrap=True, selectable=True))
        holder = QWidget(dialog)
        holder.setLayout(form)
        holder.setMinimumWidth(420)
        dialog.set_content(holder)
        dialog.finished.connect(lambda _result: dialog.deleteLater())
        self.properties_dialog = dialog
        dialog.open()

    properties_dialog = None

    @staticmethod
    def export_playlist(path: str, tracks: list[Track]) -> None:
        """Worker thread: write ``tracks`` as a playlist file; paths below its folder are written relative."""
        entries = []
        for track in tracks:
            title = f"{track.artist} - {track.title}" if track.artist and track.title else track.title
            entries.append(playlist_io.Entry(track.location, title, track.duration_ms // 1000))
        data = playlist_io.dump(entries, os.path.basename(path), os.path.dirname(os.path.abspath(path)))
        with open(path, "wb") as stream:
            stream.write(data)

    def save_settings(self) -> None:
        self.run(self.settings.save, self.settings_path, what="save the settings")

    def backend_summary(self) -> str:
        info = None
        try:
            if self._backend_info is not None:
                info = self._backend_info()
            else:
                from .backend import describe_backend
                info = describe_backend()
        except Exception:                 # the summary is information only; without it the page still works
            info = None
        if not info:
            return "Plays the audio and video formats that the multimedia components of the system decode."
        return ("The system's multimedia components report these formats. Containers: "
                f"{', '.join(info.get('containers', [])) or 'none'}. Audio: {', '.join(info.get('audio', [])) or 'none'}"
                f". Video: {', '.join(info.get('video', [])) or 'none'}.")

    # --- opening ---
    def open_files_dialog(self) -> None:
        paths = self.choose_files("Open files", self.MEDIA_FILTER)
        if paths:
            self.controller.open_locations(self._locations(paths), "play")

    def open_folder_dialog(self) -> None:
        folder = self.choose_folder("Open a folder")
        if folder:
            self.controller.open_locations(self._locations([folder]), "play")

    def add_files_to_queue(self) -> None:
        paths = self.choose_files("Add files to the play queue", self.MEDIA_FILTER)
        if paths:
            self.controller.open_locations(self._locations(paths), "enqueue")

    @staticmethod
    def check_stream_address(text: str) -> tuple[str | None, str]:
        """(address, "") for an http(s) address that can be played, else (None, what is wrong)."""
        text = text.strip()
        if not text:
            return None, "Enter the address of a stream or a media file."
        if len(text) > 4096 or any(ord(character) < 0x21 or ord(character) == 0x7F for character in text):
            return None, "An address cannot contain spaces or control characters."
        url = QUrl(text, QUrl.ParsingMode.StrictMode)
        if not url.isValid() or url.scheme().lower() not in ("http", "https"):
            return None, "Only addresses that start with http:// or https:// can be opened."
        if not url.host():
            return None, "The address has no host name."
        return bytes(url.toEncoded()).decode("ascii"), ""

    def open_url_dialog(self) -> None:
        dialog = fluent.ContentDialog(
            self, "Open URL", "Play a network stream or a media file from the web. The address is contacted when "
            "you choose Open; streams are marked “Network stream” while they play.", primary="Open")
        edit, error = url_dialog_content(dialog)
        edit.returnPressed.connect(dialog.accept)
        self.url_dialog = (dialog, edit, error)
        try:
            while dialog.exec() == fluent.ContentDialog.Result.PRIMARY:
                address, problem = self.check_stream_address(edit.text())
                if address is not None:
                    self.controller.open_locations([address], "play")
                    return
                error.setText(problem)
                error.show()
        finally:
            self.url_dialog = None
            dialog.deleteLater()

    # --- libraries ---
    def add_library_folder(self, kind: str) -> None:
        folder = self.choose_folder("Add a folder to the music library" if kind == "music"
                                    else "Add a folder to the video library")
        if not folder:
            return
        folder = os.path.normpath(folder)
        folders = getattr(self.settings, f"{kind}_folders")
        if folder in folders:
            self.notify("info", f"{folder} is already in the library.")
            return
        if len(folders) >= model.MAX_FOLDERS:
            self.notify("warning", "The library cannot have more folders.")
            return
        folders.append(folder)
        self._folders_changed()

    def remove_library_folder(self, kind: str, folder: str) -> None:
        folders = getattr(self.settings, f"{kind}_folders")
        if folder in folders:
            folders.remove(folder)
            self._folders_changed()

    def _folders_changed(self) -> None:
        self.save_settings()
        self.library.rescan()
        settings = self.navigation.page("settings")
        if isinstance(settings, SettingsPage):
            settings.fill_folders()

    def refresh_libraries(self) -> None:
        self.artwork.forget_missing()
        self.library.rescan()

    def new_playlist(self) -> None:
        self.leave_now_playing()
        self.navigation.set_current("playlists")
        self.playlists_page.new_playlist()

    def focus_search(self) -> None:
        self.leave_now_playing()
        if self.navigation.current() not in ("music", "video"):
            self.navigation.set_current("music")
        page = self.navigation.page(self.navigation.current())
        page.focus_search()

    # --- what is playing ---
    def _track_changed(self, track) -> None:
        self._update_cover()
        title = track.display_title if track is not None else ""
        self.setWindowTitle(f"{title} – Media Player" if title else "Media Player")

    def _video_changed(self, has_video: bool) -> None:
        if has_video and not self._mini and self.controller.state() == "playing":
            self.show_now_playing()
        elif not has_video and self._fullscreen:
            self.toggle_fullscreen()
        self._update_cover()

    def _update_cover(self) -> None:
        track = self.controller.current()
        pixmap: QPixmap | None = None
        self._cover_key = ""
        if track is not None:
            if track.is_remote:
                pass
            elif track.video:
                self._cover_key = Artwork.key("thumb", track.location, 320)
                pixmap = self.artwork.thumbnail(track.location, 320)
            else:
                self._cover_key = Artwork.key("cover", track.location, ART_SIZE)
                pixmap = self.artwork.cover(track.location, ART_SIZE)
                self.controller.set_art_path(self.artwork.art_file(track.location) if pixmap is not None else "")
            if pixmap is None and self.controller.backend_cover is not None:
                pixmap = QPixmap.fromImage(self.controller.backend_cover)
        for view in (self.bar.cover, self.now_playing.cover, self.mini_player.cover):
            view.set_pixmap(pixmap)

    def _artwork_loaded(self, key: str) -> None:
        if key == self._cover_key:
            self._update_cover()

    # --- views ---
    def showing_now_playing(self) -> bool:
        return self.shell.pages.currentWidget() is self.now_playing

    def show_now_playing(self) -> None:
        if self._mini or self.controller.current() is None:
            return
        self.shell.pages.setCurrentWidget(self.now_playing)
        self.now_playing.video.setFocus()

    def leave_now_playing(self) -> None:
        if self._fullscreen:
            self.toggle_fullscreen()
        if self._mini:
            self.toggle_mini()
        self.shell.pages.setCurrentWidget(self.navigation)

    def go_back(self) -> None:
        """Alt+Left: out of full screen, out of the now-playing page, out of an album, artist or playlist."""
        if self._fullscreen or self.showing_now_playing():
            self.leave_now_playing()
            return
        page = self.navigation.page(self.navigation.current()) if self.navigation.current() else None
        back = getattr(page, "go_back", None)
        if callable(back):
            back()

    def is_fullscreen(self) -> bool:
        return self._fullscreen

    def toggle_fullscreen(self) -> None:
        if self._mini:
            self.toggle_mini()
        if not self._fullscreen:
            if self.controller.current() is None:
                return
            self.show_now_playing()
            self._fullscreen = True
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        else:
            self._fullscreen = False
            if self._was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
        self.now_playing.set_fullscreen(self._fullscreen)
        self.bar.set_fullscreen(self._fullscreen)
        self.shell.bar_overlays = self._fullscreen
        self.bar.show()
        self.shell.arrange()
        self._wake_controls()

    def controls_hidden(self) -> bool:
        return self._fullscreen and not self.bar.isVisible()

    def _wake_controls(self) -> None:
        """Show the bar and the pointer; in full screen they go away again after a few seconds of playing."""
        self._hide_timer.stop()
        if not self._mini:
            if not self.bar.isVisible():
                self.bar.show()
                self.shell.arrange()
        self.now_playing.video.unsetCursor()
        self.now_playing.video.set_bottom_inset(self.bar.height() if self._fullscreen and self.bar.isVisible() else 0)
        if self._fullscreen and self.controller.state() == "playing":
            self._hide_timer.start()

    def _hide_controls(self) -> None:
        if not self._fullscreen or self.controller.state() != "playing" or self.bar.underMouse():
            return
        self.bar.hide()
        self.now_playing.video.set_bottom_inset(0)
        self.now_playing.video.setCursor(Qt.CursorShape.BlankCursor)

    def is_mini(self) -> bool:
        return self._mini

    def toggle_mini(self) -> None:
        if self._fullscreen:
            self.toggle_fullscreen()
        if not self._mini:
            self._mini = True
            self._normal_geometry = (self.saveGeometry(), self.minimumSize(), self.shell.pages.currentWidget())
            self.shell.bar_wanted = False
            self.shell.pages.setCurrentWidget(self.mini_player)
            if self.isMaximized():
                self.showNormal()
            self.setMinimumSize(*MiniPlayer.MINIMUM)
            self.resize(*MiniPlayer.SIZE)
        else:
            self._mini = False
            geometry, minimum, page = self._normal_geometry
            self.shell.bar_wanted = True
            self.shell.pages.setCurrentWidget(page if page is not self.mini_player else self.navigation)
            self.setMinimumSize(minimum)
            self.restoreGeometry(geometry)
        self.shell.arrange()

    # --- keys ---
    def media_key(self, key: int) -> bool:
        """Space, M, F, Left and Right when no control wants them; True when the key was used."""
        controller = self.controller
        if key == Qt.Key.Key_Space:
            controller.toggle()
        elif key == Qt.Key.Key_M:
            controller.toggle_mute()
        elif key == Qt.Key.Key_F:
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_Left:
            controller.seek_relative(-SEEK_STEP_MS)
        elif key == Qt.Key.Key_Right:
            controller.seek_relative(SEEK_STEP_MS)
        elif key == Qt.Key.Key_Escape and (self._fullscreen or self._mini):
            if self._fullscreen:
                self.toggle_fullscreen()
            else:
                self.toggle_mini()
        else:
            return False
        self._wake_controls()
        return True

    def keyPressEvent(self, event) -> None:
        plain = not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if plain and self.media_key(event.key()):
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event) -> bool:
        # The navigation lists would use these keys for type-ahead search; here they belong to the player.
        if event.type() == QEvent.Type.KeyPress and not event.modifiers():
            if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_M, Qt.Key.Key_F, Qt.Key.Key_Left, Qt.Key.Key_Right):
                return self.media_key(event.key())
        return super().eventFilter(watched, event)

    # --- closing ---
    def closeEvent(self, event) -> None:
        if self._mini:
            self.toggle_mini()                # so that the size that is remembered is the normal one
        if self._fullscreen:
            self.toggle_fullscreen()
        self.controller.save_state()
        self.library.shutdown()
        self.library.save_if_changed()
        if self.mpris is not None:
            self.mpris.stop()
        self.grabber.shutdown()
        self.worker.shutdown()
        self.controller.shutdown()
        super().closeEvent(event)
