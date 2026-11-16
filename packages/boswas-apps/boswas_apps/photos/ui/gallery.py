"""The pages of the navigation view: Gallery, Folders, Settings."""

from __future__ import annotations

import os

from PySide6.QtCore import QMimeData, QPoint, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QScrollArea, QSlider,
                               QStackedWidget, QVBoxLayout, QWidget)

import boswas_fluent as fluent

from .. import library, trash
from ..settings import INTERVAL_RANGE, QUALITY_RANGE
from .common import BannerHolder, action_icon
from .thumbview import ThumbGrid, draw_cover

PATH_ROLE = Qt.ItemDataRole.UserRole + 1


def _plural(count: int, word: str = "picture") -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _trash_many(paths: list[str]) -> tuple[list[tuple[str, trash.TrashRecord]], list[str]]:
    """Move files to the Trash; returns what was moved (with its record) and messages about the rest."""
    done: list[tuple[str, trash.TrashRecord]] = []
    problems: list[str] = []
    for path in paths:
        try:
            done.append((path, trash.trash(path)))
        except OSError as exc:
            problems.append(str(exc))
    return done, problems


def _restore_many(records: list[trash.TrashRecord]) -> tuple[int, list[str]]:
    restored, problems = 0, []
    for record in records:
        try:
            trash.restore(record)
            restored += 1
        except OSError as exc:
            problems.append(str(exc))
    return restored, problems


class PictureGrid(QWidget):
    """A command bar, a thumbnail grid and the states around it (searching, nothing found, empty).

    Used by the Gallery page (months) and by a folder of the Folders page.
    """

    open_requested = Signal(list, int)            # the items on show, the index to open
    slideshow_requested = Signal(list)
    open_file_requested = Signal()
    refresh_requested = Signal()
    add_folder_requested = Signal()
    files_changed = Signal(list)                  # paths that are gone

    def __init__(self, host, parent: QWidget | None = None, *, by_month: bool = True) -> None:
        super().__init__(parent)
        self.host = host
        self._by_month = by_month
        self._all: list[library.Item] = []
        self._scanning = False
        self._undo: list[trash.TrashRecord] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.setContentsMargins(32, 0, 32, 0)
        self.commands = fluent.CommandBar(self)
        add = self.commands.add_command
        self.open_action = add("Open", fluent.icon("document-open-symbolic"),
                               lambda: self.open_file_requested.emit(), tooltip="Open a picture (Ctrl+O)")
        self.slideshow_action = add("Slideshow", action_icon("media-playback-start"), self.start_slideshow,
                                    tooltip="Show the pictures one after the other")
        self.commands.add_separator()
        self.copy_action = add("Copy", action_icon("edit-copy"), self.copy_selected,
                               tooltip="Copy the selected pictures (Ctrl+C)")
        self.delete_action = add("Delete", action_icon("edit-delete", "user-trash"), self.delete_selected,
                                 tooltip="Move the selected pictures to the Trash (Delete)")
        self.clear_action = add("Clear selection", None, lambda: self.grid.clear_selection(),
                                tooltip="Clear the selection (Esc)")
        self.select_all_action = add("Select all", action_icon("edit-select-all"), lambda: self.grid.select_all(),
                                     primary=False)
        self.refresh_action = add("Refresh", fluent.icon("view-refresh-symbolic"),
                                  lambda: self.refresh_requested.emit(), primary=False)
        self.add_folder_action = add("Add a folder", fluent.icon("list-add-symbolic"),
                                     lambda: self.add_folder_requested.emit(), primary=False)
        for action, keys in ((self.select_all_action, "Ctrl+A"), (self.refresh_action, "F5")):
            action.setShortcut(QKeySequence(keys))
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.setShortcutVisibleInContextMenu(True)
        self.search = fluent.SearchBox(self, placeholder="Search by file name")
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(lambda _text: self._refilter())
        self.commands.add_widget(self.search)
        row.addWidget(self.commands, 1)
        layout.addLayout(row)

        self.status = fluent.Label("", "caption", self, tone="secondary")
        status_row = QHBoxLayout()
        status_row.setContentsMargins(32, 0, 32, 0)
        status_row.addWidget(self.status)
        layout.addLayout(status_row)

        self.banner = fluent.InfoBar("info", parent=self)
        self.banner.hide()
        self.banner.action_triggered.connect(self._undo_delete)
        layout.addWidget(BannerHolder(self.banner, self, margins=(32, 0, 32, 0)))

        self.stack = QStackedWidget(self)
        self.grid = ThumbGrid(host.thumbs, self.stack)
        self.grid.activated.connect(lambda index: self.open_requested.emit(self.grid.items(), index))
        self.grid.selection_changed.connect(self._selection_changed)
        self.grid.context_requested.connect(self._context_menu)
        self.empty = fluent.EmptyState("", "", fluent.icon("folder-pictures", "folder"), "Add a folder", self.stack)
        self.empty.action_triggered.connect(self.add_folder_requested)
        self.busy = QWidget(self.stack)
        busy_layout = QVBoxLayout(self.busy)
        busy_layout.addStretch(1)
        self.ring = fluent.ProgressRing(self.busy, size=32)
        busy_layout.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignHCenter)
        busy_layout.addWidget(fluent.Label("Looking for pictures…", "body", self.busy, tone="secondary"), 0,
                              Qt.AlignmentFlag.AlignHCenter)
        busy_layout.addStretch(1)
        for widget in (self.grid, self.empty, self.busy):
            self.stack.addWidget(widget)
        layout.addWidget(self.stack, 1)

        # Shortcuts of the window while this view is on show (a hidden view's shortcuts are off; a text
        # box that has the focus keeps Delete and Ctrl+C for itself).
        for keys, slot in (("Ctrl+F", self.focus_search), ("Ctrl+O", lambda: self.open_file_requested.emit()),
                           ("F5", lambda: self.refresh_requested.emit()), ("Delete", self.delete_selected),
                           ("Ctrl+C", self.copy_selected)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)
        self._selection_changed()
        self._show_state()

    # --- content ---
    def set_items(self, items: list[library.Item], *, scanning: bool = False) -> None:
        """The pictures of this view (in the order to show them). ``scanning``: more may come."""
        self._all = items
        self._scanning = scanning
        self._refilter()

    def all_items(self) -> list[library.Item]:
        return self._all

    def shown_items(self) -> list[library.Item]:
        return self.grid.items()

    def remove_paths(self, paths) -> None:
        gone = set(paths)
        if gone:
            self.set_items([item for item in self._all if item.path not in gone], scanning=self._scanning)

    def focus_search(self) -> None:
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def query(self) -> str:
        return self.search.query()

    def _refilter(self) -> None:
        query = self.search.query()
        shown = library.search(self._all, query) if query else self._all
        sections = library.months(shown) if self._by_month else None
        self.grid.set_items(shown, sections)
        self._show_state()

    def _show_state(self) -> None:
        query = self.search.query()
        if self.grid.count():
            self.stack.setCurrentWidget(self.grid)
        elif self._scanning:
            self.stack.setCurrentWidget(self.busy)
        else:
            if query:
                self.empty.set_title("No picture found")
                self.empty.set_text(f"No file name contains “{query}”.")
                self.empty.action_button.hide()
            else:
                self.empty.set_title("No pictures here yet")
                self.empty.set_text("Pictures in your Pictures folder and in the folders you add show up here.")
                self.empty.action_button.show()
            self.stack.setCurrentWidget(self.empty)
        self._update_status()

    def _update_status(self) -> None:
        chosen = len(self.grid.selected())
        total, shown = len(self._all), self.grid.count()
        if chosen:
            text = f"{chosen} of {_plural(shown)} selected"
        elif self.search.query():
            text = f"{shown} of {_plural(total)}"
        else:
            text = _plural(total) if total else ""
        if self._scanning and total:
            text += " · still looking"
        self.status.setText(text)

    def _selection_changed(self) -> None:
        chosen = bool(self.grid.selected())
        self.copy_action.setVisible(chosen)
        self.delete_action.setVisible(chosen)
        self.clear_action.setVisible(chosen)
        self.slideshow_action.setEnabled(self.grid.count() > 0)
        self._update_status()

    # --- commands ---
    def start_slideshow(self) -> None:
        chosen = self.grid.selected_items()
        items = chosen if len(chosen) > 1 else self.grid.items()
        if items:
            self.slideshow_requested.emit(list(items))

    def copy_selected(self) -> None:
        """The selected files go to the clipboard, as file managers expect them."""
        items = self.grid.selected_items()
        if not items:
            return
        urls = [QUrl.fromLocalFile(item.path) for item in items]
        data = QMimeData()
        data.setUrls(urls)
        data.setData("x-special/gnome-copied-files", b"copy\n" + b"\n".join(bytes(url.toEncoded()) for url in urls))
        QGuiApplication.clipboard().setMimeData(data)
        self.banner.set_action(None)
        self.banner.show_message("success", f"Copied {_plural(len(items))}.")

    def delete_selected(self) -> None:
        items = self.grid.selected_items()
        if not items:
            return
        if len(items) == 1:
            question = f"“{items[0].name}” will be moved to the Trash. You can restore it from there."
        else:
            question = f"{_plural(len(items))} will be moved to the Trash. You can restore them from there."
        if self.host.settings.confirm_delete and not self.host.ask("Move to the Trash?", question, "Move to Trash",
                                                                   "Cancel", default="confirm"):
            return
        self.host.worker.submit(_trash_many, [item.path for item in items], on_done=self._deleted)

    def _deleted(self, result) -> None:
        done, problems = result
        paths = [path for path, _record in done]
        self._undo = [record for _path, record in done]
        for path in paths:
            self.host.thumbs.invalidate(path)
        if paths:
            self.files_changed.emit(paths)
        if problems:
            self.banner.set_action(None)
            extra = f" ({len(problems) - 1} more)" if len(problems) > 1 else ""
            self.banner.show_message("error", problems[0] + extra)
        elif paths:
            self.banner.set_action("Undo")
            self.banner.show_message("success", f"Moved {_plural(len(paths))} to the Trash.")

    def _undo_delete(self) -> None:
        records, self._undo = self._undo, []
        self.banner.hide()
        if records:
            self.host.worker.submit(_restore_many, records, on_done=self._restored)

    def _restored(self, result) -> None:
        _restored, problems = result
        if problems:
            self.banner.set_action(None)
            self.banner.show_message("error", problems[0])
        self.refresh_requested.emit()

    def _context_menu(self, position: QPoint, index: int) -> None:
        if index < 0:
            return
        menu = fluent.MenuFlyout(self)
        chosen = self.grid.selected()
        if len(chosen) <= 1:
            menu.add_item("Open", lambda: self.open_requested.emit(self.grid.items(), index),
                          icon=fluent.icon("document-open-symbolic"))
        menu.add_item("Copy", self.copy_selected, icon=action_icon("edit-copy"), shortcut="Ctrl+C")
        menu.add_item("Delete", self.delete_selected, icon=action_icon("edit-delete"), shortcut="Delete")
        menu.addSeparator()
        menu.add_item("Slideshow", self.start_slideshow, icon=action_icon("media-playback-start"))
        if len(chosen) <= 1:
            menu.add_item("Show in Files", lambda: self.host.show_in_files(self.grid.item(index).path))
        self.host.popup_menu(menu, position)


class FoldersPage(QWidget):
    """The folders that hold pictures; a folder opens as a grid of its pictures."""

    add_folder_requested = Signal()

    def __init__(self, host, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.host = host
        self._items: list[library.Item] = []
        self._folders: list[library.Folder] = []
        self._current: str | None = None
        self._covers: dict[str, QListWidgetItem] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)

        # the list of folders
        self.list_page = QWidget(self.stack)
        list_layout = QVBoxLayout(self.list_page)
        list_layout.setContentsMargins(32, 0, 32, 24)
        list_layout.setSpacing(8)
        row = QHBoxLayout()
        self.count_label = fluent.Label("", "caption", self.list_page, tone="secondary")
        row.addWidget(self.count_label, 1)
        self.add_button = fluent.Button("Add a folder", self.list_page, icon=fluent.icon("list-add-symbolic"))
        self.add_button.clicked.connect(self.add_folder_requested)
        row.addWidget(self.add_button)
        list_layout.addLayout(row)
        self.list = QListWidget(self.list_page)
        self.list.setAccessibleName("Folders with pictures")
        self.list.setIconSize(QSize(40, 40))
        self.list.setUniformItemSizes(True)
        self.list.itemActivated.connect(self._activated)
        self.list.itemClicked.connect(self._activated)
        self.empty = fluent.EmptyState("No folders with pictures",
                                       "Add a folder to see its pictures in Photos.",
                                       fluent.icon("folder"), "Add a folder", self.list_page)
        self.empty.action_triggered.connect(self.add_folder_requested)
        list_layout.addWidget(self.list, 1)
        list_layout.addWidget(self.empty, 1)
        self.stack.addWidget(self.list_page)

        # one folder
        self.folder_page = QWidget(self.stack)
        folder_layout = QVBoxLayout(self.folder_page)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.setSpacing(4)
        crumbs = QHBoxLayout()
        crumbs.setContentsMargins(28, 0, 32, 0)
        self.back_button = fluent.IconButton(fluent.icon("go-previous-symbolic"), "All folders (Alt+Left)",
                                             self.folder_page)
        self.back_button.clicked.connect(self.show_list)
        crumbs.addWidget(self.back_button)
        self.crumbs = fluent.BreadcrumbBar(self.folder_page, items=["Folders"])
        self.crumbs.item_clicked.connect(lambda index: self.show_list() if index == 0 else None)
        crumbs.addWidget(self.crumbs, 1)
        folder_layout.addLayout(crumbs)
        self.grid = PictureGrid(host, self.folder_page, by_month=False)
        self.grid.add_folder_action.setVisible(False)
        folder_layout.addWidget(self.grid, 1)
        self.stack.addWidget(self.folder_page)
        back = QShortcut(QKeySequence("Alt+Left"), self.folder_page)
        back.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        back.activated.connect(self.show_list)
        host.thumbs.ready.connect(self._cover_ready)

    def set_items(self, items: list[library.Item], *, scanning: bool = False) -> None:
        self._items = items
        self._folders = library.folders(items)
        self._fill()
        if self._current is not None:
            inside = library.by_name(library.in_folder(items, self._current))
            if inside or scanning:
                self.grid.set_items(inside, scanning=scanning)
            else:
                self.show_list()

    def folders(self) -> list[library.Folder]:
        return self._folders

    def current_folder(self) -> str | None:
        return self._current

    def _fill(self) -> None:
        self.list.clear()
        self._covers = {}
        home = os.path.expanduser("~")
        wanted = []
        for folder in self._folders:
            place = os.path.dirname(folder.path)
            if place == home or place.startswith(home + os.sep):
                place = "~" + place[len(home):]
            item = QListWidgetItem(self._cover_icon(folder), f"{folder.name}\n{_plural(folder.count)} · {place}")
            item.setData(PATH_ROLE, folder.path)
            item.setToolTip(folder.path)
            item.setSizeHint(QSize(0, 60))
            self.list.addItem(item)
            if self.host.thumbs.pixmap(folder.cover.path, folder.cover.mtime) is None:
                self._covers[folder.cover.path] = item
                wanted.append((folder.cover.path, folder.cover.mtime, folder.cover.size))
        self.host.thumbs.want(self, wanted[:200])
        has = bool(self._folders)
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        self.count_label.setText(_plural(len(self._folders), "folder") if has else "")

    def _cover_icon(self, folder: library.Folder) -> QIcon:
        pixmap = self.host.thumbs.pixmap(folder.cover.path, folder.cover.mtime)
        if pixmap is None:
            return fluent.icon("folder-pictures", "folder")
        side = 80
        square = QPixmap(side, side)
        square.fill(Qt.GlobalColor.transparent)
        painter = QPainter(square)
        from PySide6.QtCore import QRectF
        draw_cover(painter, QRectF(0, 0, side, side), pixmap, fluent.theme().radius("control") * 2)
        painter.end()
        return QIcon(square)

    def _cover_ready(self, path: str) -> None:
        item = self._covers.pop(path, None)
        if item is None:
            return
        try:
            folder_path = item.data(PATH_ROLE)
        except RuntimeError:                     # the list was filled again
            return
        for folder in self._folders:
            if folder.path == folder_path:
                item.setIcon(self._cover_icon(folder))
                break

    def _activated(self, item: QListWidgetItem) -> None:
        self.open_folder(item.data(PATH_ROLE))

    def open_folder(self, path: str) -> None:
        self._current = path
        self.crumbs.set_items(["Folders", os.path.basename(path) or path])
        self.grid.search.clear()
        self.grid.set_items(library.by_name(library.in_folder(self._items, path)))
        self.stack.setCurrentWidget(self.folder_page)
        self.grid.grid.setFocus(Qt.FocusReason.OtherFocusReason)

    def show_list(self) -> None:
        self._current = None
        self.stack.setCurrentWidget(self.list_page)
        self.list.setFocus(Qt.FocusReason.OtherFocusReason)


class SettingsPage(QWidget):
    """What the user can choose. Every control writes the setting at once."""

    folders_changed = Signal()
    changed = Signal()
    add_folder_requested = Signal()

    def __init__(self, host, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.host = host
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(32, 0, 32, 32)
        self._layout.setSpacing(4)
        settings = host.settings

        self._heading("Pictures")
        self.pictures_switch = fluent.ToggleSwitch(checked=settings.include_pictures)
        self.pictures_switch.toggled.connect(self._pictures_toggled)
        self.pictures_card = fluent.SettingsCard("Pictures folder", host.pictures_dir, "folder-pictures",
                                                 self.pictures_switch)
        self._layout.addWidget(self.pictures_card)
        self.add_folder_button = fluent.Button("Add a folder")
        self.add_folder_button.clicked.connect(self.add_folder_requested)
        self.folders_card = fluent.SettingsCard("More folders", "Folders whose pictures appear in the gallery",
                                                "folder", self.add_folder_button)
        self._layout.addWidget(self.folders_card)
        self.folders_holder = QWidget()
        self.folders_layout = QVBoxLayout(self.folders_holder)
        self.folders_layout.setContentsMargins(0, 0, 0, 0)
        self.folders_layout.setSpacing(4)
        self._layout.addWidget(self.folders_holder)
        self._folder_cards: list[fluent.SettingsCard] = []

        self._heading("Appearance")
        self.theme_card = fluent.SettingsCard("Theme", "", "preferences-desktop-theme-global")
        self._layout.addWidget(self.theme_card)
        fluent.manager().changed.connect(lambda _theme: self._describe_theme())
        self._describe_theme()

        self._heading("Viewing")
        self.wheel_box = QComboBox()
        self.wheel_box.addItem("Zoom in and out", "zoom")
        self.wheel_box.addItem("Next and previous picture", "navigate")
        self.wheel_box.setCurrentIndex(max(0, self.wheel_box.findData(settings.wheel)))
        self.wheel_box.currentIndexChanged.connect(lambda _i: self._set("wheel", self.wheel_box.currentData()))
        self._layout.addWidget(fluent.SettingsCard("Mouse wheel", "Ctrl with the wheel always zooms",
                                                   "input-mouse", self.wheel_box))
        self.filmstrip_switch = fluent.ToggleSwitch(checked=settings.filmstrip)
        self.filmstrip_switch.toggled.connect(lambda on: self._set("filmstrip", on))
        self._layout.addWidget(fluent.SettingsCard("Filmstrip", "Show the other pictures of the folder under the "
                                                   "picture", "image-x-generic", self.filmstrip_switch))
        self.window_switch = fluent.ToggleSwitch(checked=settings.new_window)
        self.window_switch.toggled.connect(lambda on: self._set("new_window", on))
        self._layout.addWidget(fluent.SettingsCard(
            "Open pictures in a new window", "When a picture is opened from another application while Photos "
            "is running", "preferences-system-windows", self.window_switch))

        self._heading("Slideshow")
        self.interval_box = fluent.NumberBox(value=settings.slideshow_interval, minimum=INTERVAL_RANGE[0],
                                             maximum=INTERVAL_RANGE[1], suffix=" s")
        self.interval_box.valueChanged.connect(lambda _v: self._set("slideshow_interval",
                                                                   self.interval_box.int_value()))
        self._layout.addWidget(fluent.SettingsCard("Time per picture", "", "preferences-system-time",
                                                   self.interval_box))
        self.loop_switch = fluent.ToggleSwitch(checked=settings.slideshow_loop)
        self.loop_switch.toggled.connect(lambda on: self._set("slideshow_loop", on))
        self._layout.addWidget(fluent.SettingsCard("Repeat", "Start again after the last picture", None,
                                                   self.loop_switch))
        self.shuffle_switch = fluent.ToggleSwitch(checked=settings.slideshow_shuffle)
        self.shuffle_switch.toggled.connect(lambda on: self._set("slideshow_shuffle", on))
        self._layout.addWidget(fluent.SettingsCard("Shuffle", "Show the pictures in random order", None,
                                                   self.shuffle_switch))

        self._heading("Deleting and saving")
        self.confirm_switch = fluent.ToggleSwitch(checked=settings.confirm_delete)
        self.confirm_switch.toggled.connect(lambda on: self._set("confirm_delete", on))
        self._layout.addWidget(fluent.SettingsCard("Ask before deleting", "Deleted pictures go to the Trash either "
                                                   "way", "user-trash", self.confirm_switch))
        quality = QWidget()
        quality_row = QHBoxLayout(quality)
        quality_row.setContentsMargins(0, 0, 0, 0)
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(*QUALITY_RANGE)
        self.quality_slider.setValue(settings.jpeg_quality)
        self.quality_slider.setFixedWidth(160)
        self.quality_slider.setAccessibleName("Quality of saved pictures")
        self.quality_label = fluent.Label(str(settings.jpeg_quality), "body", tone="secondary")
        self.quality_label.setMinimumWidth(28)
        self.quality_slider.valueChanged.connect(self._quality_changed)
        quality_row.addWidget(self.quality_slider)
        quality_row.addWidget(self.quality_label)
        self._layout.addWidget(fluent.SettingsCard("Quality of saved JPEG and WebP pictures",
                                                   "Higher keeps more detail and makes larger files",
                                                   "document-save", quality))
        self._layout.addStretch(1)
        area.setWidget(content)
        outer.addWidget(area)
        self.refresh_folders()

    def _heading(self, text: str) -> None:
        self._layout.addSpacing(16)
        self._layout.addWidget(fluent.Label(text, "body-strong"))
        self._layout.addSpacing(4)

    def _describe_theme(self) -> None:
        scheme = "dark" if fluent.theme().is_dark else "light"
        self.theme_card.set_description(f"Photos follows the theme of the desktop, which is {scheme} now. "
                                        "Change it in Settings, under Personalization.")

    def _set(self, name: str, value) -> None:
        setattr(self.host.settings, name, value)
        self.host.save_settings()
        self.changed.emit()

    def _quality_changed(self, value: int) -> None:
        self.quality_label.setText(str(value))
        self._set("jpeg_quality", value)

    def _pictures_toggled(self, on: bool) -> None:
        self._set("include_pictures", on)
        self.folders_changed.emit()

    def refresh_folders(self) -> None:
        """Show the folders of the settings as rows with a Remove button."""
        for card in self._folder_cards:
            self.folders_layout.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._folder_cards = []
        for folder in self.host.settings.folders:
            button = fluent.Button("Remove")
            button.setAccessibleName(f"Remove {folder}")
            button.clicked.connect(lambda _checked=False, f=folder: self.remove_folder(f))
            card = fluent.SettingsCard(os.path.basename(folder) or folder, folder, "folder", button)
            self.folders_layout.addWidget(card)
            self._folder_cards.append(card)
        self.folders_holder.setVisible(bool(self._folder_cards))

    def remove_folder(self, folder: str) -> None:
        if folder in self.host.settings.folders:
            self.host.settings.folders.remove(folder)
            self.host.save_settings()
            self.refresh_folders()
            self.folders_changed.emit()
