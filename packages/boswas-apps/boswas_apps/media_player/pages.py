"""The pages of Media Player: Home, Music library, Video library, Play queue, Playlists, Settings.

A page talks to the rest of the application through its ``host`` (the main
window): ``host.controller`` plays, ``host.library`` knows the files,
``host.artwork`` has the pictures, ``host.playlists`` stores playlists, and
``host.notify``, ``host.track_menu``, ``host.choose_*`` and ``host.run``
are the shared ways to tell, to offer and to work in the background.
"""

from __future__ import annotations

import os
import random

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLineEdit, QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

import boswas_fluent as fluent

from . import glyphs, library, model
from .library import Album, Artist, Song
from .model import Track, format_time
from .widgets import CoverView, Tile, TileView, TrackList, TrackTable

MARGIN = 32


def _column(page: QWidget, top: int = 0) -> QVBoxLayout:
    layout = QVBoxLayout(page)
    layout.setContentsMargins(MARGIN, top, MARGIN, 0)
    layout.setSpacing(12)
    return layout


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _total(tracks) -> str:
    """``12 songs · 48:10`` for songs or tracks."""
    length = sum(item.duration_ms for item in tracks)
    text = _plural(len(tracks), "item")
    return f"{text} · {format_time(length)}" if length else text


class ScanStatus(QWidget):
    """One line under the commands of a library page: what the scan is doing, with a way to stop it."""

    def __init__(self, host, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.ring = fluent.ProgressRing(self, size=16)
        self.label = fluent.Label("Looking for new files…", "caption", self, tone="secondary")
        self.cancel = fluent.Button("Stop", self, subtle=True)
        self.cancel.setAccessibleName("Stop looking for files")
        self.cancel.clicked.connect(host.library.cancel)
        row.addWidget(self.ring)
        row.addWidget(self.label)
        row.addWidget(self.cancel)
        row.addStretch(1)
        host.library.scanning_changed.connect(self._scanning)
        host.library.progress.connect(self._progress)
        self.setVisible(host.library.is_scanning())

    def _scanning(self, scanning: bool) -> None:
        self.label.setText("Looking for new files…")
        self.setVisible(scanning)

    def _progress(self, count: int) -> None:
        self.label.setText(f"Looking for new files… {count} found so far")


# --- Home ---------------------------------------------------------------------------------------------------

class HomePage(QWidget):
    def __init__(self, host) -> None:
        super().__init__()
        self.host = host
        layout = _column(self)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.open_files = fluent.Button("Open files", self,
                                        icon=fluent.icon("document-open-symbolic", color="on-accent"), accent=True)
        self.open_files.setToolTip("Open files (Ctrl+O)")
        self.open_folder = fluent.Button("Open folder", self, icon="folder-open")
        self.open_folder.setToolTip("Open folder (Ctrl+Shift+O)")
        self.open_url = fluent.Button("Open URL", self, icon=glyphs.icon("stream"))
        self.open_url.setToolTip("Play a network stream (Ctrl+U)")
        self.open_files.clicked.connect(host.open_files_dialog)
        self.open_folder.clicked.connect(host.open_folder_dialog)
        self.open_url.clicked.connect(host.open_url_dialog)
        for button in (self.open_files, self.open_folder, self.open_url):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        heading = QHBoxLayout()
        self.recent_label = fluent.Label("Recent media", "subtitle", self)
        self.clear_button = fluent.Button("Clear", self, subtle=True)
        self.clear_button.setAccessibleName("Clear recent media")
        self.clear_button.clicked.connect(self.clear_recents)
        heading.addWidget(self.recent_label)
        heading.addStretch(1)
        heading.addWidget(self.clear_button)
        layout.addSpacing(8)
        layout.addLayout(heading)

        self.stack = QStackedWidget(self)
        self.tiles = TileView(host.artwork, self)
        self.tiles.setAccessibleName("Recent media")
        self.tiles.tile_activated.connect(self._play)
        self.tiles.menu_requested.connect(self._menu)
        self.empty = fluent.EmptyState("Nothing played yet",
                                       "Open a file, a folder or a network stream. What you play shows up here.",
                                       "multimedia-player", "Open files", self)
        self.empty.action_triggered.connect(host.open_files_dialog)
        self.stack.addWidget(self.tiles)
        self.stack.addWidget(self.empty)
        layout.addWidget(self.stack, 1)
        host.controller.recents_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        recents = self.host.controller.recents.items
        tiles = [Tile("track", track.display_title, track.subtitle, "" if track.is_remote else track.location, track)
                 for track in recents]
        self.tiles.set_tiles(tiles)
        self.stack.setCurrentWidget(self.tiles if tiles else self.empty)
        self.clear_button.setVisible(bool(tiles))
        self.recent_label.setVisible(bool(tiles))

    def _play(self, tile: Tile) -> None:
        self.host.controller.play_tracks([tile.payload])

    def _menu(self, tile: Tile, position) -> None:
        menu = self.host.track_menu([tile.payload])
        menu.addSeparator()
        menu.add_item("Remove from recent media", lambda: self.remove(tile.payload.location))
        menu.popup(position)

    def remove(self, location: str) -> None:
        self.host.controller.recents.remove(location)
        self.host.controller.recents_changed.emit()

    def clear_recents(self) -> None:
        self.host.controller.recents.clear()
        self.host.controller.recents_changed.emit()


# --- Music library ------------------------------------------------------------------------------------------

class DetailHeader(QWidget):
    """The head of an album, artist or playlist: picture, name, a line of facts and the play buttons."""

    def __init__(self, host, parent: QWidget | None = None, *, glyph: str = "music") -> None:
        super().__init__(parent)
        self.host = host
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 8)
        row.setSpacing(16)
        self.back = fluent.IconButton("go-previous-symbolic", "Back (Alt+Left)", self, size=36)
        self.cover = CoverView(96, self, glyph=glyph)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = fluent.Label("", "subtitle", self)
        self.title.setMinimumWidth(80)
        self.facts = fluent.Label("", "body", self, tone="secondary")
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.play = fluent.Button("Play all", self, icon=glyphs.icon("play", "on-accent"), accent=True)
        self.shuffle = fluent.Button("Shuffle and play", self, icon=glyphs.icon("shuffle"))
        self.add = fluent.Button("Add to", self, icon="list-add-symbolic")
        for button in (self.play, self.shuffle, self.add):
            buttons.addWidget(button)
        buttons.addStretch(1)
        text.addStretch(1)
        text.addWidget(self.title)
        text.addWidget(self.facts)
        text.addSpacing(8)
        text.addLayout(buttons)
        text.addStretch(1)
        row.addWidget(self.back, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.cover)
        row.addLayout(text, 1)

    def set(self, title: str, facts: str) -> None:
        self.title.setText(title)
        self.title.setToolTip(title)
        self.facts.setText(facts)


class MusicPage(QWidget):
    """Songs, albums and artists of the music library, and the pages of one album or one artist."""

    def __init__(self, host) -> None:
        super().__init__()
        self.host = host
        self._detail: tuple[str, str] | None = None       # ("album", key) or ("artist", name)
        self._albums: list[Album] = []
        self._artists: list[Artist] = []
        layout = _column(self)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.pivot = fluent.Pivot(self)
        for key, text in (("songs", "Songs"), ("albums", "Albums"), ("artists", "Artists")):
            self.pivot.add_item(key, text)
        self.pivot.current_changed.connect(self._view_changed)
        self.shuffle_all = fluent.Button("Shuffle and play", self, icon=glyphs.icon("shuffle", "on-accent"),
                                         accent=True)
        self.shuffle_all.clicked.connect(self.shuffle_everything)
        self.add_folder = fluent.Button("Add folder", self, icon="list-add-symbolic")
        self.add_folder.clicked.connect(lambda: host.add_library_folder("music"))
        self.sort_box = QComboBox(self)
        self.sort_box.setAccessibleName("Sort albums by")
        for text, key in (("A to Z", "title"), ("Artist", "artist"), ("Newest first", "year")):
            self.sort_box.addItem(text, key)
        self.sort_box.currentIndexChanged.connect(lambda _index: self.refresh())
        self.search = fluent.SearchBox(self, placeholder="Search the music library")
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(lambda _text: self.refresh())
        top.addWidget(self.pivot)
        top.addStretch(1)
        top.addWidget(self.sort_box)
        top.addWidget(self.search)
        self.top = QWidget(self)
        self.top.setLayout(top)
        top.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.top)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.shuffle_all)
        actions.addWidget(self.add_folder)
        self.count_label = fluent.Label("", "caption", self, tone="secondary")
        actions.addSpacing(8)
        actions.addWidget(self.count_label)
        actions.addStretch(1)
        self.actions = QWidget(self)
        self.actions.setLayout(actions)
        actions.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.actions)
        self.status = ScanStatus(host, self)
        layout.addWidget(self.status)

        self.stack = QStackedWidget(self)
        self.table = TrackTable(self)
        self.album_tiles = TileView(host.artwork, self)
        self.album_tiles.setAccessibleName("Albums")
        self.artist_tiles = TileView(host.artwork, self)
        self.artist_tiles.setAccessibleName("Artists")
        self.empty = fluent.EmptyState("", "", "folder-music", "Add folder", self)
        self.empty.action_triggered.connect(lambda: host.add_library_folder("music"))
        self.detail = QWidget(self)
        detail = QVBoxLayout(self.detail)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setSpacing(8)
        self.header = DetailHeader(host, self.detail)
        self.detail_stack = QStackedWidget(self.detail)
        self.album_table = TrackTable(self.detail, ("#", "Title", "Artist", "Length"), sortable=False)
        self.artist_table = TrackTable(self.detail, ("Title", "Album", "Year", "Length"), sortable=False)
        self.detail_stack.addWidget(self.album_table)
        self.detail_stack.addWidget(self.artist_table)
        detail.addWidget(self.header)
        detail.addWidget(self.detail_stack, 1)
        for widget in (self.table, self.album_tiles, self.artist_tiles, self.empty, self.detail):
            self.stack.addWidget(widget)
        layout.addWidget(self.stack, 1)

        for table in (self.table, self.album_table, self.artist_table):
            table.play_requested.connect(lambda row, table=table: self._play_from(table, row))
            table.menu_requested.connect(self._song_menu)
        self.album_tiles.tile_activated.connect(lambda tile: self.open_album(tile.payload))
        self.album_tiles.menu_requested.connect(self._group_menu)
        self.artist_tiles.tile_activated.connect(lambda tile: self.open_artist(tile.payload))
        self.artist_tiles.menu_requested.connect(self._group_menu)
        self.header.back.clicked.connect(self.go_back)
        self.header.play.clicked.connect(lambda: self._play_detail(False))
        self.header.shuffle.clicked.connect(lambda: self._play_detail(True))
        self.header.add.clicked.connect(self._add_detail)
        host.library.changed.connect(self.refresh)
        host.library.scanning_changed.connect(lambda _scanning: self._sync_empty())
        host.controller.track_changed.connect(self._mark_current)
        host.artwork.loaded.connect(self._cover_loaded)
        self.refresh()

    # --- content ---
    def _filtered(self) -> list[Song]:
        return library.search(self.host.library.songs(), self.search.text())

    def refresh(self) -> None:
        songs = self._filtered()
        self.table.songs.set_songs(songs)
        self._albums = library.albums_of(songs)
        order = self.sort_box.currentData()
        if order == "artist":
            self._albums.sort(key=lambda album: (album.artist.casefold(), album.title.casefold()))
        elif order == "year":
            self._albums.sort(key=lambda album: (-album.year, album.title.casefold()))
        self._artists = library.artists_of(songs)
        self.album_tiles.set_tiles([
            Tile("album", album.title, album.artist, self._cover_source(album.songs), album)
            for album in self._albums])
        self.artist_tiles.set_tiles([
            Tile("artist", artist.name, _plural(len(artist.songs), "song"), "", artist) for artist in self._artists])
        self._mark_current(self.host.controller.current())
        if self._detail is not None:
            kind, key = self._detail
            found = (next((a for a in self._albums if a.key == key), None) if kind == "album"
                     else next((a for a in self._artists if a.name == key), None))
            if found is None:
                self._detail = None
            elif kind == "album":
                self._show_album(found)
            else:
                self._show_artist(found)
        self._sync_empty()

    @staticmethod
    def _cover_source(songs: list[Song]) -> str:
        for song in songs:
            if song.has_picture:
                return song.path
        return songs[0].path if songs else ""

    def _sync_empty(self) -> None:
        everything = self.host.library.songs()
        view = self.pivot.current() or "songs"
        shown = {"songs": len(self.table.songs.songs), "albums": len(self._albums), "artists": len(self._artists)}[view]
        self.shuffle_all.setEnabled(bool(self.table.songs.songs))
        self.sort_box.setVisible(view == "albums" and self._detail is None)
        noun = {"songs": "song", "albums": "album", "artists": "artist"}[view]
        self.count_label.setText(_plural(shown, noun) if shown else "")
        self.top.setVisible(self._detail is None)
        self.actions.setVisible(self._detail is None)
        if self._detail is not None:
            self.stack.setCurrentWidget(self.detail)
            return
        if shown:
            self.stack.setCurrentWidget({"songs": self.table, "albums": self.album_tiles,
                                         "artists": self.artist_tiles}[view])
            return
        if everything:
            self.empty.set_title("No results")
            self.empty.set_text(f"Nothing in your music library matches “{self.search.text().strip()}”.")
        elif self.host.library.is_scanning() or not self.host.library.loaded:
            self.empty.set_title("Looking for music…")
            self.empty.set_text("Your music folders are being read.")
        else:
            folders = ", ".join(self.host.settings.music_folders) or "no folder"
            self.empty.set_title("Your music library is empty")
            self.empty.set_text(f"No music was found in {folders}. Add the folder where your music is.")
        if self.empty.action_button is not None:
            self.empty.action_button.setVisible(not everything)
        self.stack.setCurrentWidget(self.empty)

    def _view_changed(self, _key: str) -> None:
        self._detail = None
        self._sync_empty()

    def _mark_current(self, track) -> None:
        path = track.location if track is not None else ""
        for table in (self.table, self.album_table, self.artist_table):
            table.songs.set_current(path)

    def focus_search(self) -> None:
        if self._detail is not None:
            self.go_back()
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    # --- album and artist pages ---
    def open_album(self, album: Album) -> None:
        self._detail = ("album", album.key)
        self._show_album(album)
        self._sync_empty()
        self.album_table.setFocus()

    def _show_album(self, album: Album) -> None:
        facts = [album.artist]
        if album.year:
            facts.append(str(album.year))
        facts.append(_total(album.songs).replace("item", "song"))
        self.header.set(album.title, " · ".join(facts))
        self.header.cover.set_glyph("music")
        self._header_source = self._cover_source(album.songs)
        self.header.cover.set_pixmap(self.host.artwork.cover(self._header_source, 320) if self._header_source else None)
        self.album_table.songs.set_songs(album.songs)
        self.detail_stack.setCurrentWidget(self.album_table)

    def open_artist(self, artist: Artist) -> None:
        self._detail = ("artist", artist.name)
        self._show_artist(artist)
        self._sync_empty()
        self.artist_table.setFocus()

    def _show_artist(self, artist: Artist) -> None:
        self.header.set(artist.name, f"{_plural(len(artist.albums), 'album')} · "
                                     f"{_total(artist.songs).replace('item', 'song')}")
        self.header.cover.set_glyph("artist")
        self._header_source = ""
        self.header.cover.set_pixmap(None)
        self.artist_table.songs.set_songs(artist.songs)
        self.detail_stack.setCurrentWidget(self.artist_table)

    _header_source = ""

    def _cover_loaded(self, key: str) -> None:
        if self._detail is not None and self._header_source and key.endswith("\x1f" + self._header_source):
            self.header.cover.set_pixmap(self.host.artwork.peek(key))

    def go_back(self) -> bool:
        """Leave an album or artist page; False when the page was not showing one."""
        if self._detail is None:
            return False
        self._detail = None
        self._sync_empty()
        return True

    def detail_songs(self) -> list[Song]:
        table = self.detail_stack.currentWidget()
        return list(table.songs.songs) if self._detail is not None else []

    # --- actions ---
    def _play_from(self, table: TrackTable, row: int) -> None:
        songs = table.songs.songs
        if 0 <= row < len(songs):
            self.host.controller.play_tracks([song.to_track() for song in songs], row)

    def _play_detail(self, shuffle: bool) -> None:
        tracks = [song.to_track() for song in self.detail_songs()]
        if shuffle:
            random.shuffle(tracks)
        self.host.controller.play_tracks(tracks)

    def _add_detail(self) -> None:
        tracks = [song.to_track() for song in self.detail_songs()]
        menu = fluent.MenuFlyout(self)
        self.host.fill_add_menu(menu, tracks)
        menu.show_at(self.header.add)

    def shuffle_everything(self) -> None:
        tracks = [song.to_track() for song in self.table.songs.songs]
        random.shuffle(tracks)
        self.host.controller.play_tracks(tracks)

    def _song_menu(self, songs: list[Song], position) -> None:
        menu = self.host.track_menu([song.to_track() for song in songs])
        if len(songs) == 1 and self._detail is None:
            song = songs[0]
            menu.addSeparator()
            menu.add_item("Show album", lambda: self._show_album_of(song))
            menu.add_item("Show artist", lambda: self._show_artist_of(song))
        menu.popup(position)

    def _show_album_of(self, song: Song) -> None:
        album = next((album for album in self._albums if song in album.songs), None)
        if album is not None:
            self.open_album(album)

    def _show_artist_of(self, song: Song) -> None:
        artist = next((artist for artist in self._artists if song in artist.songs), None)
        if artist is not None:
            self.open_artist(artist)

    def _group_menu(self, tile: Tile, position) -> None:
        tracks = [song.to_track() for song in tile.payload.songs]
        menu = fluent.MenuFlyout(self)
        menu.add_item("Play", lambda: self.host.controller.play_tracks(tracks), icon=glyphs.icon("play"))
        self.host.fill_add_menu(menu, tracks)
        menu.popup(position)


# --- Video library ------------------------------------------------------------------------------------------

class VideoPage(QWidget):
    def __init__(self, host) -> None:
        super().__init__()
        self.host = host
        layout = _column(self)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.add_folder = fluent.Button("Add folder", self, icon="list-add-symbolic")
        self.add_folder.clicked.connect(lambda: host.add_library_folder("video"))
        self.count_label = fluent.Label("", "caption", self, tone="secondary")
        self.search = fluent.SearchBox(self, placeholder="Search the video library")
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(lambda _text: self.refresh())
        top.addWidget(self.add_folder)
        top.addSpacing(8)
        top.addWidget(self.count_label)
        top.addStretch(1)
        top.addWidget(self.search)
        layout.addLayout(top)
        self.status = ScanStatus(host, self)
        layout.addWidget(self.status)
        self.stack = QStackedWidget(self)
        self.tiles = TileView(host.artwork, self)
        self.tiles.setAccessibleName("Videos")
        self.empty = fluent.EmptyState("", "", "folder-videos", "Add folder", self)
        self.empty.action_triggered.connect(lambda: host.add_library_folder("video"))
        self.stack.addWidget(self.tiles)
        self.stack.addWidget(self.empty)
        layout.addWidget(self.stack, 1)
        self.tiles.tile_activated.connect(self._play)
        self.tiles.menu_requested.connect(self._menu)
        host.library.changed.connect(self.refresh)
        host.library.scanning_changed.connect(lambda _scanning: self.refresh())
        host.artwork.duration_found.connect(self._duration_found)
        self.refresh()

    def videos(self) -> list[Song]:
        return library.search(self.host.library.videos(), self.search.text())

    def refresh(self) -> None:
        videos = self.videos()
        self.tiles.set_tiles([Tile("video", video.display_title,
                                   format_time(video.duration_ms) if video.duration_ms else "", video.path, video)
                              for video in videos])
        self.count_label.setText(_plural(len(videos), "video") if videos else "")
        if videos:
            self.stack.setCurrentWidget(self.tiles)
            return
        if self.host.library.videos():
            self.empty.set_title("No results")
            self.empty.set_text(f"Nothing in your video library matches “{self.search.text().strip()}”.")
        elif self.host.library.is_scanning() or not self.host.library.loaded:
            self.empty.set_title("Looking for videos…")
            self.empty.set_text("Your video folders are being read.")
        else:
            folders = ", ".join(self.host.settings.video_folders) or "no folder"
            self.empty.set_title("Your video library is empty")
            self.empty.set_text(f"No videos were found in {folders}. Add the folder where your videos are.")
        if self.empty.action_button is not None:
            self.empty.action_button.setVisible(not self.host.library.videos())
        self.stack.setCurrentWidget(self.empty)

    def _duration_found(self, path: str, duration: int) -> None:
        self.host.library.set_duration(path, duration)
        for row, tile in enumerate(self.tiles.tiles.tiles):
            if tile.source == path and not tile.subtitle:
                tile.subtitle = format_time(duration)
                index = self.tiles.tiles.index(row, 0)
                self.tiles.tiles.dataChanged.emit(index, index)

    def focus_search(self) -> None:
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def _play(self, tile: Tile) -> None:
        self.host.controller.play_tracks([tile.payload.to_track()])

    def _menu(self, tile: Tile, position) -> None:
        self.host.track_menu([tile.payload.to_track()]).popup(position)


# --- Play queue ---------------------------------------------------------------------------------------------

class QueuePage(QWidget):
    def __init__(self, host) -> None:
        super().__init__()
        self.host = host
        controller = host.controller
        layout = _column(self)
        self.commands = fluent.CommandBar(self)
        self.add_action = self.commands.add_command("Add files", "list-add-symbolic", host.add_files_to_queue)
        self.save_action = self.commands.add_command("Add to playlist", glyphs.icon("playlist"), self._save_menu,
                                                     tooltip="Add everything in the queue to a playlist")
        self.clear_action = self.commands.add_command("Clear", "edit-delete-symbolic", self.clear)
        self.total = fluent.Label("", "caption", self, tone="secondary")
        self.commands.add_widget(self.total)
        layout.addWidget(self.commands)
        self.stack = QStackedWidget(self)
        self.list = TrackList(self)
        self.list.setAccessibleName("Play queue")
        self.empty = fluent.EmptyState("The play queue is empty", "Play something from your library, or add files.",
                                       glyphs.icon("queue", "text-secondary"), "Add files", self)
        self.empty.action_triggered.connect(host.add_files_to_queue)
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.empty)
        layout.addWidget(self.stack, 1)
        self.hint = fluent.Label("Drag an item to move it, or press Alt+Up and Alt+Down. Delete removes it.",
                                 "caption", self, tone="muted", wrap=True)
        layout.addWidget(self.hint)
        layout.addSpacing(4)
        self.list.play_requested.connect(controller.play_index)
        self.list.move_requested.connect(controller.move)
        self.list.remove_requested.connect(self._remove)
        self.list.menu_requested.connect(self._menu)
        controller.queue_changed.connect(self.refresh)
        controller.track_changed.connect(lambda _track: self._sync_current())
        controller.track_updated.connect(lambda _track: self.refresh())
        controller.state_changed.connect(lambda _state: self._sync_current())
        self.refresh()

    def refresh(self) -> None:
        controller = self.host.controller
        queue = controller.queue
        self.list.set_tracks(queue.items, queue.index, controller.state() == "playing")
        has = bool(queue.items)
        self.stack.setCurrentWidget(self.list if has else self.empty)
        self.hint.setVisible(has)
        self.total.setText(_total(queue.items) if has else "")
        self.clear_action.setEnabled(has)
        self.save_action.setEnabled(has)

    def _sync_current(self) -> None:
        controller = self.host.controller
        self.list.set_current(controller.queue.index, controller.state() == "playing")

    def _remove(self, rows: list[int]) -> None:
        for row in rows:
            self.host.controller.remove(row)

    def clear(self) -> None:
        self.host.controller.clear()

    def _save_menu(self) -> None:
        menu = fluent.MenuFlyout(self)
        self.host.fill_playlist_menu(menu, list(self.host.controller.queue.items))
        menu.show_at(self.commands.button(self.save_action) or self.commands)

    def _menu(self, rows: list[int], position) -> None:
        controller = self.host.controller
        tracks = [controller.queue.items[row] for row in rows if row < len(controller.queue)]
        menu = fluent.MenuFlyout(self)
        if len(rows) == 1:
            row = rows[0]
            menu.add_item("Play", lambda: controller.play_index(row), icon=glyphs.icon("play"))
            menu.add_item("Move up", lambda: self.list.request_move(row, row - 1), shortcut="Alt+Up",
                          enabled=row > 0)
            menu.add_item("Move down", lambda: self.list.request_move(row, row + 1), shortcut="Alt+Down",
                          enabled=row < len(controller.queue) - 1)
        menu.add_item("Remove from queue", lambda: self._remove(sorted(rows, reverse=True)),
                      icon="list-remove-symbolic", shortcut="Del")
        playlist = menu.add_submenu("Add to playlist")
        self.host.fill_playlist_menu(playlist, tracks)
        if len(tracks) == 1 and not tracks[0].is_remote:
            menu.addSeparator()
            menu.add_item("Open file location", lambda: self.host.open_location(tracks[0]))
            menu.add_item("Properties", lambda: self.host.show_properties(tracks[0]))
        menu.popup(position)


# --- Playlists ----------------------------------------------------------------------------------------------

class PlaylistsPage(QWidget):
    """The list of playlists and, after a click, one playlist with its tracks."""

    def __init__(self, host) -> None:
        super().__init__()
        self.host = host
        self.current: str | None = None                   # the playlist that is open
        self.tracks: list[Track] = []
        self._counts: dict[str, int] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget(self)
        outer.addWidget(self.stack)

        # --- all playlists ---
        self.master = QWidget(self)
        layout = _column(self.master)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.new_button = fluent.Button("New playlist", self.master,
                                        icon=fluent.icon("list-add-symbolic", color="on-accent"), accent=True)
        self.new_button.setToolTip("New playlist (Ctrl+N)")
        self.new_button.clicked.connect(self.new_playlist)
        self.import_button = fluent.Button("Import", self.master, icon="document-open-symbolic")
        self.import_button.setToolTip("Import an M3U, M3U8 or PLS file as a playlist")
        self.import_button.clicked.connect(self.import_playlist)
        buttons.addWidget(self.new_button)
        buttons.addWidget(self.import_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.master_stack = QStackedWidget(self.master)
        scroll = QScrollArea(self.master)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.rows_holder = QWidget(scroll)
        self.rows = QVBoxLayout(self.rows_holder)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(4)
        self.rows.addStretch(1)
        scroll.setWidget(self.rows_holder)
        self.empty = fluent.EmptyState("No playlists yet", "Collect songs and videos in a playlist, in the order "
                                       "you like.", glyphs.icon("playlist", "text-secondary"), "New playlist",
                                       self.master)
        self.empty.action_triggered.connect(self.new_playlist)
        self.master_stack.addWidget(scroll)
        self.master_stack.addWidget(self.empty)
        layout.addWidget(self.master_stack, 1)
        self.stack.addWidget(self.master)

        # --- one playlist ---
        self.detail = QWidget(self)
        detail = _column(self.detail)
        self.header = DetailHeader(host, self.detail, glyph="playlist")
        self.header.add.hide()
        self.commands = fluent.CommandBar(self.detail)
        self.add_files_action = self.commands.add_command("Add files", "list-add-symbolic", self.add_files)
        self.rename_action = self.commands.add_command("Rename", glyphs.icon("edit"), self.rename, shortcut="F2")
        self.export_action = self.commands.add_command("Export", "document-open-symbolic", self.export,
                                                       tooltip="Save a copy as M3U8, M3U or PLS")
        self.delete_action = self.commands.add_command("Delete", "edit-delete-symbolic", self.delete)
        self.detail_stack = QStackedWidget(self.detail)
        self.list = TrackList(self.detail)
        self.list.setAccessibleName("Playlist")
        self.detail_empty = fluent.EmptyState(
            "This playlist is empty", "Add files here, or use “Add to playlist” on songs, albums and the play queue.",
            glyphs.icon("playlist", "text-secondary"), "Add files", self.detail)
        self.detail_empty.action_triggered.connect(self.add_files)
        self.detail_stack.addWidget(self.list)
        self.detail_stack.addWidget(self.detail_empty)
        detail.addWidget(self.header)
        detail.addWidget(self.commands)
        detail.addWidget(self.detail_stack, 1)
        detail.addSpacing(4)
        self.stack.addWidget(self.detail)

        self.header.back.clicked.connect(self.go_back)
        self.header.play.clicked.connect(lambda: self.play(False))
        self.header.shuffle.clicked.connect(lambda: self.play(True))
        self.list.play_requested.connect(lambda row: host.controller.play_tracks(self.tracks, row))
        self.list.move_requested.connect(self._move)
        self.list.remove_requested.connect(self._remove)
        self.list.menu_requested.connect(self._menu)
        host.playlists_changed.connect(self.reload)
        self.reload()

    # --- all playlists ---
    def reload(self) -> None:
        store = self.host.playlists

        def read() -> dict[str, list[Track] | None]:
            found: dict[str, list[Track] | None] = {}
            for name in store.names():
                try:
                    found[name] = store.load(name)
                except (OSError, ValueError):
                    found[name] = None
            return found

        self.host.run(read, done=self._loaded, what="read the playlists")

    def _loaded(self, playlists: dict[str, list[Track] | None]) -> None:
        self._all = playlists
        while self.rows.count() > 1:
            widget = self.rows.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self.items: dict[str, fluent.ListItem] = {}
        for name, tracks in playlists.items():
            subtitle = "This file could not be read" if tracks is None else _total(tracks)
            more = fluent.IconButton(fluent.glyph_icon("more"), f"More for {name}", size=32, icon_size=12)
            item = fluent.ListItem(name, subtitle, glyphs.icon("playlist"), more, self.rows_holder)
            item.clicked.connect(lambda name=name: self.open(name))
            more.clicked.connect(lambda _checked=False, name=name, more=more: self._playlist_menu(name).show_at(more))
            item.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            item.customContextMenuRequested.connect(
                lambda position, name=name, item=item: self._playlist_menu(name).popup(item.mapToGlobal(position)))
            self.rows.insertWidget(self.rows.count() - 1, item)
            self.items[name] = item
        self.master_stack.setCurrentIndex(0 if playlists else 1)
        if self.current is not None:
            if self.current in playlists and playlists[self.current] is not None:
                self._show(self.current, playlists[self.current])
            else:
                self.go_back()

    _all: dict[str, list[Track] | None] = {}
    items: dict[str, fluent.ListItem] = {}

    def names(self) -> list[str]:
        return list(self._all)

    def _playlist_menu(self, name: str) -> fluent.MenuFlyout:
        menu = fluent.MenuFlyout(self)
        tracks = self._all.get(name) or []
        menu.add_item("Play", lambda: self.host.controller.play_tracks(tracks), icon=glyphs.icon("play"),
                      enabled=bool(tracks))
        menu.add_item("Add to queue", lambda: self.host.controller.enqueue(tracks), enabled=bool(tracks))
        menu.addSeparator()
        menu.add_item("Rename", lambda: self.rename(name), icon=glyphs.icon("edit"))
        menu.add_item("Export", lambda: self.export(name))
        menu.add_item("Delete", lambda: self.delete(name), icon="edit-delete-symbolic")
        return menu

    def new_playlist(self, tracks: list[Track] | None = None) -> None:
        store = self.host.playlists
        name = self.host.ask_text("New playlist", "Name", store.unique_name(), "Create", self._check_new_name)
        if name is None:
            return
        self.host.run(store.create, name, tracks or [], done=lambda created: self._created(created),
                      what="create the playlist")

    def _created(self, name: str) -> None:
        self.host.playlists_changed.emit()
        self.host.notify("success", f"Created the playlist “{name}”.")

    def _check_new_name(self, text: str, old: str | None = None) -> str | None:
        """None when ``text`` can be the name of a new playlist; else the reason it cannot."""
        try:
            name = model.check_playlist_name(text)
        except model.PlaylistNameError as error:
            return str(error)
        taken = {other.casefold() for other in self._all if other != old}
        return f"There is already a playlist called “{name}”." if name.casefold() in taken else None

    def import_playlist(self) -> None:
        paths = self.host.choose_files("Import a playlist", "Playlists (*.m3u *.m3u8 *.pls)")
        if not paths:
            return
        store = self.host.playlists

        def work() -> list[str]:
            made = []
            for path in paths[:50]:
                opened = self.host.expand([path])
                wanted = os.path.splitext(os.path.basename(path))[0][:100].lstrip(".") or "Imported playlist"
                try:
                    wanted = model.check_playlist_name(wanted)
                except model.PlaylistNameError:
                    wanted = "Imported playlist"
                made.append(store.create(store.unique_name(wanted), opened.tracks))
            return made

        def done(made: list[str]) -> None:
            self.host.playlists_changed.emit()
            if len(made) == 1:
                self.host.notify("success", f"Imported the playlist “{made[0]}”.")
            else:
                self.host.notify("success", f"Imported {len(made)} playlists.")

        self.host.run(work, done=done, what="import the playlist")

    # --- one playlist ---
    def open(self, name: str) -> None:
        tracks = self._all.get(name)
        if tracks is None:
            self.host.notify("error", f"The playlist “{name}” could not be read.")
            return
        self.current = name
        self._show(name, tracks)
        self.stack.setCurrentWidget(self.detail)
        self.list.setFocus()

    def _show(self, name: str, tracks: list[Track]) -> None:
        self.tracks = list(tracks)
        self.header.set(name, _total(tracks))
        self.list.set_tracks(self.tracks, self._current_row())
        self.detail_stack.setCurrentIndex(0 if tracks else 1)
        self.header.play.setEnabled(bool(tracks))
        self.header.shuffle.setEnabled(bool(tracks))
        source = next((t.location for t in tracks if not t.is_remote and not t.video), "")
        self.header.cover.set_pixmap(self.host.artwork.cover(source, 320) if source else None)

    def _current_row(self) -> int:
        current = self.host.controller.current()
        if current is None:
            return -1
        return next((row for row, track in enumerate(self.tracks) if track.location == current.location), -1)

    def go_back(self) -> bool:
        if self.current is None:
            return False
        self.current = None
        self.stack.setCurrentWidget(self.master)
        return True

    def play(self, shuffle: bool) -> None:
        tracks = list(self.tracks)
        if shuffle:
            random.shuffle(tracks)
        self.host.controller.play_tracks(tracks)

    def _store(self) -> None:
        name, tracks = self.current, list(self.tracks)
        if name is None:
            return
        self._all[name] = tracks
        self._show(name, tracks)
        if name in self.items:
            self.items[name].set_subtitle(_total(tracks))
        self.host.run(self.host.playlists.save, name, tracks, what="save the playlist")

    def _move(self, source: int, target: int) -> None:
        if 0 <= source < len(self.tracks) and 0 <= target < len(self.tracks):
            self.tracks.insert(target, self.tracks.pop(source))
            self._store()

    def _remove(self, rows: list[int]) -> None:
        for row in sorted(rows, reverse=True):
            if 0 <= row < len(self.tracks):
                del self.tracks[row]
        self._store()

    def add_tracks(self, name: str, tracks: list[Track]) -> None:
        """Append ``tracks`` to the playlist ``name`` (from anywhere in the application)."""
        store = self.host.playlists

        def work() -> int:
            existing = store.load(name)
            store.save(name, existing + tracks)
            return len(tracks)

        def done(count: int) -> None:
            self.host.playlists_changed.emit()
            self.host.notify("success", f"Added {_plural(count, 'item')} to “{name}”.")

        self.host.run(work, done=done, what="add to the playlist")

    def add_files(self) -> None:
        if self.current is None:
            return
        paths = self.host.choose_files("Add files to the playlist", self.host.MEDIA_FILTER)
        if not paths:
            return
        name = self.current
        self.host.run(self.host.expand, paths, done=lambda opened: self.add_tracks(name, opened.tracks),
                      what="read those files")

    def rename(self, name: str | None = None) -> None:
        old = name or self.current
        if old is None:
            return
        new = self.host.ask_text("Rename playlist", "Name", old, "Rename", lambda text: self._check_new_name(text, old))
        if new is None or new == old:
            return

        def done(renamed: str) -> None:
            if self.current == old:
                self.current = renamed
            self.host.playlists_changed.emit()

        self.host.run(self.host.playlists.rename, old, new, done=done, what="rename the playlist")

    def delete(self, name: str | None = None) -> None:
        target = name or self.current
        if target is None:
            return
        if not self.host.ask("Delete this playlist?", f"“{target}” will be deleted. The songs and videos in it stay "
                             "where they are.", "Delete", "Cancel"):
            return

        def done(_result) -> None:
            if self.current == target:
                self.go_back()
            self.host.playlists_changed.emit()
            self.host.notify("success", f"Deleted the playlist “{target}”.")

        self.host.run(self.host.playlists.delete, target, done=done, what="delete the playlist")

    def export(self, name: str | None = None) -> None:
        target = name or self.current
        if target is None:
            return
        tracks = self._all.get(target) or []
        path = self.host.choose_save("Export the playlist", f"{target}.m3u8",
                                     "M3U8 playlist (*.m3u8);;M3U playlist (*.m3u);;PLS playlist (*.pls)")
        if not path:
            return
        if os.path.splitext(path)[1].lower() not in model.PLAYLIST_EXTENSIONS:
            path += ".m3u8"
        self.host.run(self.host.export_playlist, path, tracks,
                      done=lambda _result: self.host.notify("success", f"Exported “{target}” to {path}."),
                      what="export the playlist")

    def _menu(self, rows: list[int], position) -> None:
        tracks = [self.tracks[row] for row in rows if row < len(self.tracks)]
        menu = fluent.MenuFlyout(self)
        if len(rows) == 1:
            row = rows[0]
            menu.add_item("Play", lambda: self.host.controller.play_tracks(self.tracks, row), icon=glyphs.icon("play"))
            menu.add_item("Move up", lambda: self.list.request_move(row, row - 1), shortcut="Alt+Up", enabled=row > 0)
            menu.add_item("Move down", lambda: self.list.request_move(row, row + 1), shortcut="Alt+Down",
                          enabled=row < len(self.tracks) - 1)
        menu.add_item("Play next", lambda: self.host.controller.play_next(tracks))
        menu.add_item("Add to queue", lambda: self.host.controller.enqueue(tracks))
        menu.add_item("Remove from playlist", lambda: self._remove(rows), icon="list-remove-symbolic", shortcut="Del")
        if len(tracks) == 1 and not tracks[0].is_remote:
            menu.addSeparator()
            menu.add_item("Open file location", lambda: self.host.open_location(tracks[0]))
            menu.add_item("Properties", lambda: self.host.show_properties(tracks[0]))
        menu.popup(position)


# --- Settings -----------------------------------------------------------------------------------------------

class SettingsPage(QScrollArea):
    def __init__(self, host) -> None:
        super().__init__()
        self.host = host
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(self)
        self.layout_ = QVBoxLayout(body)
        self.layout_.setContentsMargins(MARGIN, 0, MARGIN, MARGIN)
        self.layout_.setSpacing(4)
        self.setWidget(body)
        self.body = body

        self._section("Libraries")
        self.folder_holders: dict[str, QVBoxLayout] = {}
        self.expanders: dict[str, fluent.SettingsExpander] = {}
        self.add_buttons: dict[str, fluent.Button] = {}
        for kind in ("music", "video"):
            holder = QVBoxLayout()
            holder.setContentsMargins(0, 0, 0, 0)
            self.folder_holders[kind] = holder
            self.layout_.addLayout(holder)
        self.refresh_button = fluent.Button("Refresh", body, icon="view-refresh-symbolic")
        self.refresh_button.clicked.connect(self.refresh_libraries)
        self.refresh_card = fluent.SettingsCard("Refresh libraries", "Look for new, changed and removed files now",
                                                "view-refresh-symbolic", self.refresh_button, body)
        self.layout_.addWidget(self.refresh_card)

        self._section("Playback")
        self.resume = fluent.ToggleSwitch(parent=body, checked=host.settings.resume_videos)
        self.resume.toggled.connect(self._resume_toggled)
        self.layout_.addWidget(fluent.SettingsCard(
            "Resume videos", "Continue a video where you stopped watching it", glyphs.icon("video"), self.resume,
            body))
        self.device_box = QComboBox(body)
        self.device_box.setMinimumWidth(220)
        self.device_card = fluent.SettingsCard("Playback device", "", "audio-card", self.device_box, body)
        self.layout_.addWidget(self.device_card)
        self.device_box.activated.connect(self._device_chosen)
        host.controller.backend.devices_changed.connect(self.fill_devices)

        self._section("Appearance")
        self.theme_value = fluent.Label("", "body", body, tone="secondary")
        self.layout_.addWidget(fluent.SettingsCard(
            "Theme and accent colour", "Media Player follows the desktop. Change them in Settings, under "
            "Personalisation.", "preferences-desktop-theme-global", self.theme_value, body))
        fluent.manager().changed.connect(self._theme_changed)
        self._theme_changed()

        self._section("About")
        self.about = fluent.SettingsCard(f"Media Player {host.version}", host.backend_summary(),
                                         "multimedia-player", None, body)
        self.layout_.addWidget(self.about)
        self.layout_.addStretch(1)

        host.library.finished.connect(self._scan_finished)
        host.library.scanning_changed.connect(lambda scanning: self.refresh_button.setEnabled(not scanning))
        self.fill_folders()
        self.fill_devices()

    def _section(self, title: str) -> None:
        self.layout_.addSpacing(12)
        label = fluent.Label(title, "body-strong", self.body)
        self.layout_.addWidget(label)
        self.layout_.addSpacing(2)

    def _theme_changed(self, *_args) -> None:
        self.theme_value.setText("Dark" if fluent.theme().is_dark else "Light")

    # --- folders ---
    def fill_folders(self) -> None:
        for kind, title, icon in (("music", "Music library locations", "folder-music"),
                                  ("video", "Video library locations", "folder-videos")):
            holder = self.folder_holders[kind]
            was_open = kind in self.expanders and self.expanders[kind].is_expanded()
            while holder.count():
                widget = holder.takeAt(0).widget()
                if widget is not None:
                    widget.deleteLater()
            folders = getattr(self.host.settings, f"{kind}_folders")
            add = fluent.Button("Add folder", self.body, icon="list-add-symbolic")
            add.setAccessibleName(f"Add a {kind} folder")
            add.clicked.connect(lambda _checked=False, kind=kind: self.host.add_library_folder(kind))
            expander = fluent.SettingsExpander(title, _plural(len(folders), "folder"), icon, add, self.body,
                                               expanded=was_open or kind not in self.expanders)
            for folder in folders:
                remove = fluent.Button("Remove", expander)
                remove.setAccessibleName(f"Remove {folder}")
                remove.clicked.connect(lambda _checked=False, kind=kind, folder=folder:
                                       self.host.remove_library_folder(kind, folder))
                missing = "" if os.path.isdir(folder) else "This folder does not exist"
                expander.add_row(folder, missing, remove, "folder")
            if not folders:
                expander.add_row("No folders", "Files are found only in the folders listed here")
            holder.addWidget(expander)
            self.expanders[kind] = expander
            self.add_buttons[kind] = add

    def refresh_libraries(self) -> None:
        self.host.artwork.forget_missing()
        self.host.library.rescan()

    def _scan_finished(self, result) -> None:
        if result.cancelled:
            text = "The last search was stopped before it finished"
        else:
            songs, videos = len(self.host.library.songs()), len(self.host.library.videos())
            text = f"{_plural(songs, 'song')} and {_plural(videos, 'video')} in the libraries"
            if result.unreadable:
                text += f" · could not read {', '.join(result.unreadable[:3])}"
        self.refresh_card.set_description(text)

    # --- playback ---
    def _resume_toggled(self, checked: bool) -> None:
        self.host.settings.resume_videos = checked
        self.host.save_settings()

    def fill_devices(self) -> None:
        devices = self.host.controller.backend.audio_devices()
        self.device_box.blockSignals(True)
        self.device_box.clear()
        self.device_box.addItem("System default", "")
        for device_id, description in devices:
            self.device_box.addItem(description or device_id, device_id)
        chosen = self.host.settings.audio_device
        index = self.device_box.findData(chosen)
        self.device_box.setCurrentIndex(index if index >= 0 else 0)
        self.device_box.blockSignals(False)
        self.device_box.setEnabled(bool(devices))
        if not devices:
            self.device_card.set_description("No audio output device was found. Sound will play once one is "
                                             "connected.")
        elif chosen and index < 0:
            self.device_card.set_description("The device chosen before is not connected; the system default is used")
        else:
            self.device_card.set_description("Where sound is played")

    def _device_chosen(self, index: int) -> None:
        self.host.controller.set_audio_device(self.device_box.itemData(index) or "")
        self.host.save_settings()
        self.fill_devices()


def url_dialog_content(dialog: fluent.ContentDialog) -> tuple[QLineEdit, fluent.Label]:
    """The body of the "Open URL" dialog: an address box and a line for what is wrong with the address."""
    edit = QLineEdit(dialog)
    edit.setPlaceholderText("https://example.org/stream.mp3")
    edit.setAccessibleName("Address")
    edit.setMinimumWidth(420)
    edit.setMaxLength(4096)
    error = fluent.Label("", "caption", dialog, tone="danger", wrap=True)
    error.hide()
    dialog.set_content(edit)
    dialog.set_content(error)
    QTimer.singleShot(0, edit.setFocus)
    return edit, error
