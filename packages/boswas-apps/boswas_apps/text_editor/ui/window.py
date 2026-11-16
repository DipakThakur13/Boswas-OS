"""A window of the text editor: tabs, the menu bar, the text, the find bar and the status bar."""

from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import QDate, QEvent, QFileSystemWatcher, QLocale, QTime, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QIntValidator, QKeySequence
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLineEdit, QMenuBar, QStackedWidget, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import APP_ID, DISPLAY_NAME, codec, fileio, session
from .. import settings as settings_store
from ..search import MAX_COUNT, Query
from . import printing
from .editor import EditorTab
from .findbar import FindBar
from .settings_page import SettingsPage
from .statusbar import StatusBar
from .tabbar import TabBar

SAVE, DISCARD, CANCEL = "save", "discard", "cancel"
OVERWRITE, RELOAD = "overwrite", "reload"
FILE_FILTERS = "Text documents (*.txt);;All files (*)"
SESSION_DELAY_MS = 3000
COUNT_DELAY_MS = 150
MAX_COUNTED_CHARACTERS = 64 * 1024 * 1024


class EditorWindow(fluent.FluentWindow):
    """One window of the editor. ``context`` is the application: settings, recent files, directories, windows.

    What asks the user something is a method that a test can replace:
    ``ask_save``, ``ask_changed``, ``ask_large``, ``ask_other_encoding``,
    ``ask_line``, ``pick_open_files``, ``pick_save_file``, ``tell``.
    """

    load_progress = Signal(object, float)          # from the worker thread to the tab that is loading

    def __init__(self, context) -> None:
        super().__init__(APP_ID, DISPLAY_NAME, icon="accessories-text-editor", size=(920, 640),
                         minimum_size=(420, 320))
        self.context = context
        self.settings = context.settings
        self.worker = fluent.Worker(self, max_threads=2)
        self.zoom = 100
        self._closing = False
        self._text_cache: tuple[object, int, str] | None = None
        self._print_job = None
        self._printer = None
        self.setAcceptDrops(True)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(8, 4, 8, 0)
        tab_row.setSpacing(4)
        self.tab_bar = TabBar(central)
        self.new_tab_button = fluent.IconButton("tab-new-symbolic", "New tab (Ctrl+N)", central)
        tab_row.addWidget(self.tab_bar)
        tab_row.addWidget(self.new_tab_button)
        tab_row.addStretch(1)
        layout.addLayout(tab_row)
        menu_row = QHBoxLayout()
        menu_row.setContentsMargins(4, 0, 8, 0)
        self.menu_bar = QMenuBar(central)
        self.settings_button = fluent.IconButton("preferences-system-symbolic", "Settings", central)
        menu_row.addWidget(self.menu_bar)
        menu_row.addStretch(1)
        menu_row.addWidget(self.settings_button)
        layout.addLayout(menu_row)
        self.pages = QStackedWidget(central)
        self.tabs = QStackedWidget(self.pages)
        self.settings_page = SettingsPage(self.settings, self.pages)
        self.pages.addWidget(self.tabs)
        self.pages.addWidget(self.settings_page)
        layout.addWidget(self.pages, 1)
        self.status = StatusBar(central)
        layout.addWidget(self.status)
        self.set_content(central)
        self.find_bar = FindBar(self.settings, self.tabs)

        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(lambda _path: self._changes_timer.start())
        self._changes_timer = self._timer(250, self.check_files_on_disk)
        self._session_timer = self._timer(SESSION_DELAY_MS, self.save_session_soon)
        self._count_timer = self._timer(COUNT_DELAY_MS, self._update_count)
        self._build_actions()

        self.tab_bar.currentChanged.connect(self._current_changed)
        self.tab_bar.tabMoved.connect(self._tab_moved)
        self.tab_bar.close_requested.connect(self.close_tab)
        self.tab_bar.new_requested.connect(self.new_tab)
        self.tab_bar.menu_requested.connect(lambda index, position: self.tab_menu(index).exec(position))
        self.new_tab_button.clicked.connect(lambda: self.new_tab())
        self.settings_button.clicked.connect(self.show_settings)
        self.settings_page.back_requested.connect(self.hide_settings)
        self.settings_page.changed.connect(self._setting_changed)
        self.find_bar.query_changed.connect(self._query_changed)
        self.find_bar.find_requested.connect(self.find)
        self.find_bar.replace_requested.connect(self.replace)
        self.find_bar.replace_all_requested.connect(self.replace_all)
        self.find_bar.closed.connect(lambda: self.current_tab() and self.current_tab().area.setFocus())
        self.status.zoom_step.connect(self.step_zoom)
        self.status.line_ending_chosen.connect(self.set_line_ending)
        self.status.encoding_chosen.connect(self.set_encoding)
        self.status.reopen_chosen.connect(self.reopen_with_encoding)
        self.load_progress.connect(self._load_progress)
        self.apply_settings()

    def _timer(self, interval: int, slot: Callable[[], None]) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(interval)
        timer.timeout.connect(slot)
        return timer

    # --- menus ---
    def _action(self, text: str, slot: Callable[[], None], shortcut: str | None = None, *, live: bool = True,
                checkable: bool = False) -> QAction:
        action = QAction(text, self)
        action.setCheckable(checkable)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutVisibleInContextMenu(True)
            if live:
                action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
                self.addAction(action)
            else:
                # Shown only: the text area (or the field that has the focus) answers this key itself.
                action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def _build_actions(self) -> None:
        a = self.actions_by_name = {}
        a["new_tab"] = self._action("New tab", self.new_tab, "Ctrl+N")
        a["new_window"] = self._action("New window", self.context.new_window, "Ctrl+Shift+N")
        a["open"] = self._action("Open…", self.open_dialog, "Ctrl+O")
        a["save"] = self._action("Save", self.save, "Ctrl+S")
        a["save_as"] = self._action("Save as…", self.save_as, "Ctrl+Shift+S")
        a["save_all"] = self._action("Save all", self.save_all, "Ctrl+Alt+S")
        a["page_setup"] = self._action("Page setup…", self.page_setup)
        a["print"] = self._action("Print…", self.print_dialog, "Ctrl+P")
        a["close_tab"] = self._action("Close tab", lambda: self.close_tab(self.tab_bar.currentIndex()), "Ctrl+W")
        a["close_window"] = self._action("Close window", self.close, "Ctrl+Shift+W")
        a["exit"] = self._action("Exit", self.context.quit)
        a["undo"] = self._action("Undo", lambda: self._edit("undo"), "Ctrl+Z", live=False)
        a["redo"] = self._action("Redo", lambda: self._edit("redo"), "Ctrl+Y", live=False)
        a["cut"] = self._action("Cut", lambda: self._edit("cut"), "Ctrl+X", live=False)
        a["copy"] = self._action("Copy", lambda: self._edit("copy"), "Ctrl+C", live=False)
        a["paste"] = self._action("Paste", lambda: self._edit("paste"), "Ctrl+V", live=False)
        a["delete"] = self._action("Delete", self.delete_selection, "Del", live=False)
        a["find"] = self._action("Find", self.show_find, "Ctrl+F")
        a["find_next"] = self._action("Find next", lambda: self.find(False), "F3")
        a["find_previous"] = self._action("Find previous", lambda: self.find(True), "Shift+F3")
        a["replace"] = self._action("Replace", lambda: self.show_find(replace=True), "Ctrl+H")
        a["go_to"] = self._action("Go to…", self.go_to_line, "Ctrl+G")
        a["select_all"] = self._action("Select all", lambda: self._edit("selectAll"), "Ctrl+A", live=False)
        a["time_date"] = self._action("Time/Date", self.insert_time_date, "F5")
        a["font"] = self._action("Font", self.show_settings)
        a["zoom_in"] = self._action("Zoom in", lambda: self.step_zoom(1), "Ctrl++")
        a["zoom_out"] = self._action("Zoom out", lambda: self.step_zoom(-1), "Ctrl+-")
        a["zoom_reset"] = self._action("Restore default zoom", lambda: self.step_zoom(0), "Ctrl+0")
        a["status_bar"] = self._action("Status bar", lambda: self._toggle_setting("status_bar"), checkable=True)
        a["word_wrap"] = self._action("Word wrap", lambda: self._toggle_setting("word_wrap"), checkable=True)
        a["next_tab"] = self._action("Next tab", lambda: self.step_tab(1), "Ctrl+Tab")
        a["previous_tab"] = self._action("Previous tab", lambda: self.step_tab(-1), "Ctrl+Shift+Tab")
        for extra, slot in (("Ctrl+=", lambda: self.step_zoom(1)), ("Ctrl+PgDown", lambda: self.step_tab(1)),
                            ("Ctrl+PgUp", lambda: self.step_tab(-1)), ("Ctrl+Y", lambda: self._edit("redo")),
                            ("Ctrl+Shift+Z", lambda: self._edit("redo"))):
            self.add_shortcut(extra, slot)

        file_menu = self.menu_bar.addMenu("&File")
        file_menu.addActions([a["new_tab"], a["new_window"], a["open"]])
        self.recent_menu = file_menu.addMenu("Recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        file_menu.addActions([a["save"], a["save_as"], a["save_all"]])
        if printing.AVAILABLE:
            file_menu.addSeparator()
            file_menu.addActions([a["page_setup"], a["print"]])
        else:
            self.removeAction(a["print"])
        file_menu.addSeparator()
        file_menu.addActions([a["close_tab"], a["close_window"], a["exit"]])
        edit_menu = self.menu_bar.addMenu("&Edit")
        edit_menu.addActions([a["undo"], a["redo"]])
        edit_menu.addSeparator()
        edit_menu.addActions([a["cut"], a["copy"], a["paste"], a["delete"]])
        edit_menu.addSeparator()
        edit_menu.addActions([a["find"], a["find_next"], a["find_previous"], a["replace"], a["go_to"]])
        edit_menu.addSeparator()
        edit_menu.addActions([a["select_all"], a["time_date"]])
        edit_menu.addSeparator()
        edit_menu.addAction(a["font"])
        edit_menu.aboutToShow.connect(self._sync_edit_actions)
        view_menu = self.menu_bar.addMenu("&View")
        zoom_menu = view_menu.addMenu("Zoom")
        zoom_menu.addActions([a["zoom_in"], a["zoom_out"], a["zoom_reset"]])
        view_menu.addActions([a["status_bar"], a["word_wrap"]])
        self.file_menu, self.edit_menu, self.view_menu = file_menu, edit_menu, view_menu

    def _sync_edit_actions(self) -> None:
        tab = self.current_tab()
        a = self.actions_by_name
        area = tab.area if tab is not None else None
        editable = area is not None and not area.isReadOnly()
        selection = area is not None and area.textCursor().hasSelection()
        a["undo"].setEnabled(editable and area.document().isUndoAvailable())
        a["redo"].setEnabled(editable and area.document().isRedoAvailable())
        a["cut"].setEnabled(editable and selection)
        a["copy"].setEnabled(bool(selection))
        a["paste"].setEnabled(editable and area.canPaste())
        a["delete"].setEnabled(editable and selection)
        a["time_date"].setEnabled(editable)

    def _fill_recent(self) -> None:
        menu = self.recent_menu
        menu.clear()
        for path in self.context.recent:
            action = menu.addAction(path.replace("&", "&&"))
            action.triggered.connect(lambda _checked=False, p=path: self.open_paths([p]))
        if self.context.recent:
            menu.addSeparator()
            menu.addAction("Clear recent files").triggered.connect(lambda _checked=False: self.context.clear_recent())
        else:
            menu.addAction("No recent files").setEnabled(False)

    def tab_menu(self, index: int) -> fluent.MenuFlyout:
        menu = fluent.MenuFlyout(self)
        tab = self.tab_at(index)
        menu.add_item("Close tab", lambda: self.close_tab(self.index_of(tab)), shortcut="Ctrl+W")
        menu.add_item("Close other tabs", lambda: self.close_tabs([t for t in self.all_tabs() if t is not tab]),
                      enabled=self.tab_bar.count() > 1)
        menu.add_item("Close tabs to the right", lambda: self.close_tabs(self.all_tabs()[self.index_of(tab) + 1:]),
                      enabled=index < self.tab_bar.count() - 1)
        if tab.path:
            menu.addSeparator()
            menu.add_item("Copy path", lambda: self._copy_text(tab.path))
            menu.add_item("Open containing folder", lambda: fluent.open_path(os.path.dirname(tab.path)))
        return menu

    @staticmethod
    def _copy_text(text: str) -> None:
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(text)

    # --- tabs ---
    def all_tabs(self) -> list[EditorTab]:
        return [self.tab_bar.tabData(index) for index in range(self.tab_bar.count())]

    def tab_at(self, index: int) -> EditorTab | None:
        return self.tab_bar.tabData(index) if 0 <= index < self.tab_bar.count() else None

    def index_of(self, tab: EditorTab) -> int:
        for index in range(self.tab_bar.count()):
            if self.tab_bar.tabData(index) is tab:
                return index
        return -1

    def current_tab(self) -> EditorTab | None:
        return self.tab_at(self.tab_bar.currentIndex())

    def new_tab(self, *, select: bool = True) -> EditorTab:
        """Open an empty tab at the end."""
        taken = {tab.untitled for tab in self.all_tabs() if not tab.path}
        number = 1
        while ("Untitled" if number == 1 else f"Untitled {number}") in taken:
            number += 1
        tab = EditorTab("Untitled" if number == 1 else f"Untitled {number}", self.tabs)
        tab.line_ending = str(self.settings["default_line_ending"])
        tab.area.apply_settings(self.settings)
        tab.area.set_zoom(self.zoom)
        tab.title_changed.connect(lambda t=tab: self._tab_changed(t))
        tab.area.cursorPositionChanged.connect(lambda t=tab: t is self.current_tab() and self._sync_status())
        tab.area.selectionChanged.connect(lambda t=tab: t is self.current_tab() and self._sync_status())
        tab.area.textChanged.connect(lambda t=tab: self._text_changed(t))
        tab.area.zoom_requested.connect(self.step_zoom)
        tab.area.files_dropped.connect(self.open_paths)
        self.tabs.addWidget(tab)
        blocked = self.tab_bar.blockSignals(not select)
        index = self.tab_bar.addTab(tab.name())
        self.tab_bar.setTabData(index, tab)
        self.tab_bar.blockSignals(blocked)
        if select or self.tab_bar.count() == 1:
            self.tab_bar.setCurrentIndex(index)
            self._current_changed(index)
        self._tab_changed(tab)
        return tab

    def _tab_changed(self, tab: EditorTab) -> None:
        index = self.index_of(tab)
        if index < 0:
            return
        self.tab_bar.setTabText(index, tab.name().replace("&", "&&"))
        self.tab_bar.setTabToolTip(index, tab.path or tab.name())
        self.tab_bar.set_modified(index, tab.is_modified())
        if tab is self.current_tab():
            self._sync_title()
            self._sync_status()

    def _tab_moved(self, _from: int, _to: int) -> None:
        self._session_timer.start()

    def _current_changed(self, index: int) -> None:
        tab = self.tab_at(index)
        if tab is None:
            return
        self.tabs.setCurrentWidget(tab)
        self._text_cache = None
        self._sync_title()
        self._sync_status()
        if self.pages.currentWidget() is self.tabs and not self.find_bar.isVisible():
            tab.area.setFocus()
        if self.find_bar.isVisible():
            self._count_timer.start()
            self._place_find_bar()
        self._session_timer.start()

    def step_tab(self, step: int) -> None:
        count = self.tab_bar.count()
        if count > 1:
            self.tab_bar.setCurrentIndex((self.tab_bar.currentIndex() + step) % count)

    def _sync_title(self) -> None:
        tab = self.current_tab()
        if tab is None:
            self.setWindowTitle(DISPLAY_NAME)
            return
        self.setWindowTitle(f"{'*' if tab.is_modified() else ''}{tab.name()} - {DISPLAY_NAME}")

    def _sync_status(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        area = tab.area
        cursor = area.textCursor()
        self.status.set_position(*area.position())
        self.status.set_counts(tab.characters(), abs(cursor.selectionEnd() - cursor.selectionStart()))
        self.status.set_zoom(self.zoom)
        self.status.set_file_state(tab.line_ending, tab.encoding, enabled=not tab.loading,
                                   can_reopen=bool(tab.path) and not tab.is_modified() and not tab.loading)

    def _text_changed(self, tab: EditorTab) -> None:
        if tab is self.current_tab():
            self._text_cache = None
            if self.find_bar.isVisible():
                self._count_timer.start()
        if not tab.loading:
            self._session_timer.start()

    # --- settings ---
    def apply_settings(self) -> None:
        """Bring every tab, the menus and the bars in line with the settings."""
        settings = self.settings
        for tab in self.all_tabs():
            tab.area.apply_settings(settings)
            tab.area.set_zoom(self.zoom)
        self.status.setVisible(bool(settings["status_bar"]))
        self.actions_by_name["status_bar"].setChecked(bool(settings["status_bar"]))
        self.actions_by_name["word_wrap"].setChecked(bool(settings["word_wrap"]))
        theme = settings["theme"]
        manager = fluent.manager()
        wanted = None if theme == "system" else theme
        if (None if manager.follows_desktop else manager.scheme) != wanted:
            manager.set_scheme(wanted)
        self.settings_page.refresh()
        self._sync_status()

    def _setting_changed(self, _key: str) -> None:
        self.context.settings_changed()

    def _toggle_setting(self, key: str) -> None:
        self.settings[key] = not self.settings[key]
        self.context.settings_changed()

    def show_settings(self) -> None:
        self.find_bar.close_bar()
        self.settings_page.refresh()
        self.pages.setCurrentWidget(self.settings_page)
        self.settings_page.back_button.setFocus()

    def hide_settings(self) -> None:
        self.pages.setCurrentWidget(self.tabs)
        tab = self.current_tab()
        if tab is not None:
            tab.area.setFocus()

    def settings_shown(self) -> bool:
        return self.pages.currentWidget() is self.settings_page

    def step_zoom(self, step: int) -> None:
        """Zoom in (+1), out (-1) or back to 100 % (0)."""
        self.zoom = 100 if step == 0 else settings_store.next_zoom(self.zoom, step)
        for tab in self.all_tabs():
            tab.area.set_zoom(self.zoom)
        self._sync_status()

    # --- questions (tests replace these) ---
    def ask_save(self, tab: EditorTab) -> str:
        dialog = fluent.ContentDialog(self, DISPLAY_NAME, f"Do you want to save changes to {tab.name()}?",
                                      primary="Save", secondary="Don’t save", close="Cancel")
        try:
            result = dialog.exec()
        finally:
            dialog.deleteLater()
        return {fluent.ContentDialog.Result.PRIMARY: SAVE, fluent.ContentDialog.Result.SECONDARY: DISCARD}.get(
            result, CANCEL)

    def ask_changed(self, tab: EditorTab) -> str:
        dialog = fluent.ContentDialog(
            self, "The file changed on disk",
            f"{tab.name()} was changed by another program since it was opened. Overwrite it with your text, or "
            "reload it and lose your changes?", primary="Overwrite", secondary="Reload", close="Cancel",
            default="close")
        try:
            result = dialog.exec()
        finally:
            dialog.deleteLater()
        return {fluent.ContentDialog.Result.PRIMARY: OVERWRITE, fluent.ContentDialog.Result.SECONDARY: RELOAD}.get(
            result, CANCEL)

    def ask_large(self, path: str, size: int) -> bool:
        return self.ask("Open a large file?",
                        f"{os.path.basename(path)} is {fileio.describe_size(size)}. Opening it takes a while and "
                        "a lot of memory.", "Open", "Cancel", default="confirm")

    def ask_other_encoding(self, tab: EditorTab) -> bool:
        return self.ask("Some characters cannot be saved",
                        f"{tab.name()} has characters that {tab.encoding.label} cannot hold. Save it as UTF-8 "
                        "instead?", "Save as UTF-8", "Cancel", default="confirm")

    def ask_line(self, lines: int) -> int | None:
        dialog = fluent.ContentDialog(self, "Go to line", f"Line number (1 to {lines:,})", primary="Go to",
                                      close="Cancel")
        field = QLineEdit(dialog)
        field.setValidator(QIntValidator(1, 2147483647, field))
        tab = self.current_tab()
        field.setText(str(tab.area.position()[0]) if tab is not None else "1")
        field.selectAll()
        field.setAccessibleName("Line number")
        dialog.set_content(field)
        QTimer.singleShot(0, field.setFocus)
        try:
            if dialog.exec() != fluent.ContentDialog.Result.PRIMARY or not field.text():
                return None
            return int(field.text())
        finally:
            dialog.deleteLater()

    def pick_open_files(self) -> list[str]:
        files, _filter = QFileDialog.getOpenFileNames(self, "Open", self._dialog_directory(), FILE_FILTERS,
                                                      "All files (*)")
        return files

    def pick_save_file(self, tab: EditorTab) -> str | None:
        suggestion = tab.path or os.path.join(self._dialog_directory(), tab.name() + ".txt")
        name, _filter = QFileDialog.getSaveFileName(self, "Save as", suggestion, FILE_FILTERS)
        return name or None

    def _dialog_directory(self) -> str:
        tab = self.current_tab()
        if tab is not None and tab.path:
            return os.path.dirname(tab.path)
        for path in self.context.recent:
            if os.path.isdir(os.path.dirname(path)):
                return os.path.dirname(path)
        return os.path.expanduser("~")

    # --- opening ---
    def open_dialog(self) -> None:
        self.open_paths(self.pick_open_files())

    def find_open(self, path: str) -> EditorTab | None:
        real = os.path.realpath(path)
        for tab in self.all_tabs():
            if tab.path and os.path.realpath(tab.path) == real:
                return tab
        return None

    def open_paths(self, paths: list[str]) -> None:
        """Open files in tabs (a file that is open already is shown instead)."""
        self.hide_settings()
        for path in paths:
            if not isinstance(path, str) or not path or "\x00" in path:
                continue
            path = os.path.abspath(path)
            existing = self.find_open(path)
            if existing is not None:
                self.tab_bar.setCurrentIndex(self.index_of(existing))
                continue
            self.open_file(path)

    def open_file(self, path: str, *, tab: EditorTab | None = None, encoding: codec.Encoding | None = None,
                  cursor: int = 0, quiet: bool = False) -> EditorTab | None:
        """Load ``path`` into ``tab`` (default: a new tab, or the empty untitled one). Returns the tab used."""
        try:
            size = fileio.check(path)
        except fileio.LoadError as error:
            if error.kind == "missing" and tab is None and not quiet:
                return self._new_file_tab(path)
            if not quiet:
                self.tell("The file cannot be opened", str(error))
            return None
        if size > fileio.LARGE_FILE and not self.ask_large(path, size):
            return None
        created = tab is None
        if tab is None:
            current = self.current_tab()
            tab = current if current is not None and current.is_pristine() else self.new_tab()
        tab.path = path
        tab.missing_on_disk = False
        tab.info.hide()
        tab.show_progress(f"Opening {tab.name()}…", 0.0)
        self._tab_changed(tab)

        def done(loaded: fileio.Loaded) -> None:
            if self.index_of(tab) < 0:
                return
            tab.encoding, tab.line_ending, tab.signature = loaded.encoding, loaded.line_ending, loaded.signature
            tab.change_noticed = None

            def finished(ok: bool) -> None:
                tab.load_finished.disconnect(finished)
                if not ok:
                    self._load_failed(tab, created, None)
                    return
                self._watch(path)
                self.context.add_recent(path)
                if loaded.mixed:
                    tab.show_message("warning", "This file mixes line endings ("
                                     f"{codec.describe_line_endings(loaded.line_endings)}). It will be saved with "
                                     f"{codec.LINE_ENDING_LABELS[loaded.line_ending]} line endings unless you "
                                     "choose another kind in the status bar.")
                elif not loaded.writable:
                    tab.show_message("info", "This file is read-only. Use Save as to keep your changes.")
                self._tab_changed(tab)
                if tab is self.current_tab():
                    tab.area.setFocus()
            tab.load_finished.connect(finished)
            tab.set_text(loaded.text, cursor=cursor)

        def failed(error: Exception) -> None:
            if self.index_of(tab) >= 0:
                self._load_failed(tab, created, error)

        self.worker.submit(fileio.load, path, progress=lambda fraction: self.load_progress.emit(tab, fraction),
                           cancelled=tab.load_cancelled, encoding=encoding,
                           default_line_ending=str(self.settings["default_line_ending"]), on_done=done,
                           on_error=failed)
        return tab

    def _load_progress(self, tab: EditorTab, fraction: float) -> None:
        try:
            if tab.loading:
                tab.set_progress(fraction * 0.5)
        except RuntimeError:                   # the tab was closed meanwhile
            pass

    def _load_failed(self, tab: EditorTab, created: bool, error: Exception | None) -> None:
        tab.finish_progress()
        tab.path = None
        tab.signature = None
        self._tab_changed(tab)
        if created and tab.is_pristine() and self.tab_bar.count() > 1:
            self._remove_tab(tab)
        if isinstance(error, fileio.LoadError):
            self.tell("The file cannot be opened", str(error))
        elif error is not None and not isinstance(error, fileio.Cancelled):
            self.tell("The file cannot be opened", f"Something went wrong while reading it: {error}.")

    def _new_file_tab(self, path: str) -> EditorTab:
        current = self.current_tab()
        tab = current if current is not None and current.is_pristine() else self.new_tab()
        tab.path = path
        tab.missing_on_disk = True
        tab.show_message("info", f"{os.path.basename(path)} does not exist yet. It is created when you save.")
        self._tab_changed(tab)
        return tab

    def reload(self, tab: EditorTab) -> None:
        if tab.path:
            line = tab.area.textCursor().position()
            self.open_file(tab.path, tab=tab, cursor=line)

    def reopen_with_encoding(self, key: str) -> None:
        tab = self.current_tab()
        if tab is not None and tab.path and not tab.is_modified():
            self.open_file(tab.path, tab=tab, encoding=codec.by_key(key))

    # --- saving ---
    def save(self, tab: EditorTab | None = None, then: Callable[[bool], None] | None = None) -> None:
        tab = tab or self.current_tab()
        if tab is not None:
            self.save_tab(tab, then=then)

    def save_as(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            self.save_tab(tab, choose=True)

    def save_all(self) -> None:
        for tab in self.all_tabs():
            if tab.is_modified() and tab.path:
                self.save_tab(tab)
        untitled = [tab for tab in self.all_tabs() if tab.is_modified() and not tab.path]
        for tab in untitled:
            self.tab_bar.setCurrentIndex(self.index_of(tab))
            self.save_tab(tab)

    def save_tab(self, tab: EditorTab, *, choose: bool = False, then: Callable[[bool], None] | None = None) -> None:
        """Save a tab; ``then(saved)`` is called when the file is written (or was not)."""
        def result(saved: bool) -> None:
            if then is not None:
                then(saved)
        if tab.loading or tab.saving:
            result(False)
            return
        path = tab.path
        if choose or not path:
            path = self.pick_save_file(tab)
            if not path:
                result(False)
                return
            path = os.path.abspath(path)
        elif tab.signature is not None:
            on_disk = fileio.signature(path)
            if on_disk is not None and on_disk != tab.signature:
                choice = self.ask_changed(tab)
                if choice == RELOAD:
                    self.reload(tab)
                if choice != OVERWRITE:
                    result(False)
                    return
        text = tab.text()
        if self.settings["trim_trailing"]:
            trimmed = codec.trim_trailing_whitespace(text)
            if trimmed != text:
                tab.area.replace_all_text(trimmed)
                text = trimmed
        try:
            data = fileio.to_bytes(text, tab.encoding, tab.line_ending)
        except UnicodeEncodeError:
            if not self.ask_other_encoding(tab):
                result(False)
                return
            tab.encoding = codec.UTF8
            data = fileio.to_bytes(text, tab.encoding, tab.line_ending)
        revision = tab.area.document().revision()
        tab.saving = True
        old_path = tab.path

        def done(mark: fileio.Signature) -> None:
            tab.saving = False
            if self.index_of(tab) < 0:
                return
            if old_path and old_path != path:
                self._unwatch(old_path)
            tab.path, tab.signature, tab.change_noticed, tab.missing_on_disk = path, mark, None, False
            if tab.area.document().revision() == revision:
                tab.area.document().setModified(False)
            tab.info.hide()
            self._watch(path)
            self.context.add_recent(path)
            self._tab_changed(tab)
            self._session_timer.start()
            result(True)

        def failed(error: Exception) -> None:
            tab.saving = False
            reason = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
            if self.index_of(tab) >= 0:
                self.tell("The file was not saved", f"{os.path.basename(path)} could not be written: {reason}. "
                          "Your text is still in the tab; try Save as with another name or folder.")
            result(False)

        self.worker.submit(fileio.save, path, data, on_done=done, on_error=failed)

    def set_line_ending(self, kind: str) -> None:
        tab = self.current_tab()
        if tab is not None and kind in codec.LINE_ENDINGS and kind != tab.line_ending:
            tab.line_ending = kind
            tab.area.document().setModified(True)
            self._tab_changed(tab)

    def set_encoding(self, key: str) -> None:
        tab = self.current_tab()
        encoding = codec.by_key(key)
        if tab is not None and encoding != tab.encoding:
            tab.encoding = encoding
            tab.area.document().setModified(True)
            self._tab_changed(tab)

    # --- closing ---
    def _remove_tab(self, tab: EditorTab) -> None:
        index = self.index_of(tab)
        if index < 0:
            return
        if tab.path:
            self._unwatch(tab.path)
        tab.cancel_load()
        self.tab_bar.removeTab(index)
        self.tabs.removeWidget(tab)
        tab.deleteLater()
        self._text_cache = None
        if self.tab_bar.count() == 0 and not self._closing:
            self._closing = True
            # As in Notepad, closing the last tab closes the window. Not from here: this may run inside
            # the window's own close event, where a second close() does nothing.
            QTimer.singleShot(0, self.close)
        else:
            self._session_timer.start()

    def close_tab(self, index: int, then: Callable[[bool], None] | None = None) -> None:
        """Close a tab, asking first when it has unsaved changes; ``then(closed)`` afterwards."""
        tab = self.tab_at(index)

        def finish(closed: bool) -> None:
            if closed:
                self._remove_tab(tab)
            if then is not None:
                then(closed)
        if tab is None:
            if then is not None:
                then(False)
            return
        if not tab.is_modified():
            finish(True)
            return
        self.tab_bar.setCurrentIndex(index)
        choice = self.ask_save(tab)
        if choice == SAVE:
            self.save_tab(tab, then=finish)
        else:
            finish(choice == DISCARD)

    def close_tabs(self, tabs: list[EditorTab], then: Callable[[bool], None] | None = None) -> None:
        """Close several tabs one after the other; stops at the first the user keeps."""
        remaining = [tab for tab in tabs if self.index_of(tab) >= 0]
        if not remaining:
            if then is not None:
                then(True)
            return

        def next_one(closed: bool) -> None:
            if closed:
                self.close_tabs(remaining[1:], then)
            elif then is not None:
                then(False)
        self.close_tab(self.index_of(remaining[0]), next_one)

    def _session_pairs(self) -> list[tuple[EditorTab, session.TabState, bool]]:
        """(tab, what the session keeps of it, whether that is everything) for the tabs worth remembering.

        Unsaved text is kept up to the caps of ``session``; a tab beyond them is remembered by its
        path alone, and closing the window asks about it.
        """
        pairs = []
        total = 0
        for tab in self.all_tabs():
            if tab.loading:
                if tab.path:
                    pairs.append((tab, session.TabState(path=tab.path, encoding=tab.encoding.key,
                                                        line_ending=tab.line_ending), True))
                continue
            if not tab.path and not tab.is_modified():
                continue                       # an empty untitled tab is not worth remembering
            unsaved = tab.is_modified() or not tab.path
            size = tab.characters() if unsaved else 0
            keep = (len(pairs) < session.MAX_TABS and size <= session.MAX_TAB_TEXT
                    and total + size <= session.MAX_TOTAL_TEXT)
            if keep:
                total += size
            state = session.TabState(path=tab.path, text=tab.text() if unsaved and keep else None,
                                     encoding=tab.encoding.key, line_ending=tab.line_ending,
                                     cursor=tab.area.textCursor().position(), title=tab.untitled)
            pairs.append((tab, state, keep or not unsaved))
        return pairs

    def session_states(self) -> list[session.TabState]:
        """What is written to the session file."""
        return [state for _tab, state, _complete in self._session_pairs() if state.path or state.text is not None]

    def unsaved_outside_session(self) -> list[EditorTab]:
        """The tabs whose unsaved text the session cannot keep (too large): closing asks about them."""
        return [tab for tab, _state, complete in self._session_pairs() if not complete]

    def _session_current(self) -> int:
        """The index of the current tab among the tabs the session keeps."""
        kept = [tab for tab, state, _complete in self._session_pairs() if state.path or state.text is not None]
        current = self.current_tab()
        return kept.index(current) if current in kept else 0

    def save_session_soon(self) -> None:
        """Write the session in the background while the editor runs, so a crash loses little."""
        if not self.settings["restore_session"] or self.context.session_window() is not self:
            return
        self.worker.submit(session.save, self.context.state_dir, self.session_states(), self._session_current(),
                           key="session")

    def closeEvent(self, event) -> None:
        if self._closing or self.tab_bar.count() == 0:
            self._shutdown()
            super().closeEvent(event)
            return
        last = self.context.is_last_window(self)
        if last and self.settings["restore_session"]:
            ask = self.unsaved_outside_session()
        else:
            ask = [tab for tab in self.all_tabs() if tab.is_modified()]
        if ask:
            event.ignore()

            def decided(all_closed: bool) -> None:
                if all_closed and not self._closing:
                    QTimer.singleShot(0, self.close)
            self.close_tabs(ask, decided)
            return
        if last:
            if self.settings["restore_session"]:
                session.save(self.context.state_dir, self.session_states(), self._session_current())
            else:
                session.clear(self.context.state_dir)
        self._closing = True
        self._shutdown()
        super().closeEvent(event)

    def _shutdown(self) -> None:
        for timer in (self._session_timer, self._count_timer, self._changes_timer):
            timer.stop()
        for tab in self.all_tabs():
            tab.cancel_load()
        self.worker.shutdown()
        self.context.window_closed(self)

    def restore_session(self, states: list[session.TabState], current: int) -> None:
        """Open the tabs of the last session."""
        missing = []
        opened: list[EditorTab | None] = []
        for state in states:
            if state.text is None:
                tab = self.open_file(state.path, cursor=state.cursor, quiet=True)
                if tab is None:
                    missing.append(os.path.basename(state.path))
                opened.append(tab)
                continue
            current_tab = self.current_tab()
            tab = current_tab if current_tab is not None and current_tab.is_pristine() else self.new_tab()
            tab.path = state.path
            if state.title and not state.path:
                tab.untitled = state.title
            tab.encoding = codec.by_key(state.encoding)
            tab.line_ending = state.line_ending
            tab.signature = fileio.signature(state.path) if state.path else None
            tab.set_text(state.text, modified=True, cursor=state.cursor)
            if state.path:
                self._watch(state.path)
            self._tab_changed(tab)
            opened.append(tab)
        if 0 <= current < len(opened) and opened[current] is not None:
            self.tab_bar.setCurrentIndex(self.index_of(opened[current]))
        if missing:
            tab = self.current_tab() or self.new_tab()
            tab.show_message("warning", "Not opened again because the file is gone or cannot be read: "
                             + ", ".join(missing) + ".")

    # --- files changing on disk ---
    def _watch(self, path: str) -> None:
        if path not in self.watcher.files() and os.path.exists(path):
            self.watcher.addPath(path)

    def _unwatch(self, path: str) -> None:
        if path in self.watcher.files() and not any(tab.path == path for tab in self.all_tabs()):
            self.watcher.removePath(path)

    def check_files_on_disk(self) -> None:
        """Compare every open file with the disk and offer a reload where another program changed it."""
        for tab in self.all_tabs():
            if not tab.path or tab.loading or tab.saving or tab.signature is None:
                continue
            on_disk = fileio.signature(tab.path)
            if on_disk is not None and (on_disk == tab.signature or on_disk == tab.change_noticed):
                self._watch(tab.path)
                continue
            if on_disk is None:
                tab.change_noticed = None
                tab.signature = None
                tab.area.document().setModified(True)
                tab.show_message("warning", f"{tab.name()} was deleted or moved. Save to write it again.")
                continue
            tab.change_noticed = on_disk
            self._watch(tab.path)              # a program that replaces the file drops it from the watch
            if tab.is_modified():
                tab.show_message("warning", f"{tab.name()} was changed by another program. Reloading discards "
                                 "your changes.", "Reload", lambda t=tab: self.reload(t))
            else:
                tab.show_message("info", f"{tab.name()} was changed by another program.", "Reload",
                                 lambda t=tab: self.reload(t))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow() and hasattr(self, "watcher"):
            self._changes_timer.start()
            self.context.window_activated(self)

    # --- editing ---
    def _edit(self, method: str) -> None:
        tab = self.current_tab()
        if tab is not None:
            getattr(tab.area, method)()

    def delete_selection(self) -> None:
        tab = self.current_tab()
        if tab is not None and not tab.area.isReadOnly():
            tab.area.textCursor().removeSelectedText()

    def insert_time_date(self) -> None:
        tab = self.current_tab()
        if tab is None or tab.area.isReadOnly():
            return
        locale = QLocale()
        tab.area.insertPlainText(f"{locale.toString(QTime.currentTime(), QLocale.FormatType.ShortFormat)} "
                                 f"{locale.toString(QDate.currentDate(), QLocale.FormatType.ShortFormat)}")

    def go_to_line(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        lines = tab.area.blockCount()
        line = self.ask_line(lines)
        if line is None:
            return
        if not tab.area.go_to_line(line):
            self.tell("Go to line", f"The line number is beyond the total number of lines ({lines:,}).")
        tab.area.setFocus()

    # --- find and replace ---
    def _text(self, tab: EditorTab) -> str:
        """The text of a tab, read once per change (a search needs it whole)."""
        revision = tab.area.document().revision()
        cache = self._text_cache
        if cache is not None and cache[0] is tab and cache[1] == revision:
            return cache[2]
        text = tab.text()
        self._text_cache = (tab, revision, text)
        return text

    def _place_find_bar(self) -> None:
        bar = self.find_bar
        bar.adjustSize()
        width = min(max(bar.sizeHint().width(), 460), max(self.tabs.width() - 32, 200))
        bar.setGeometry(max(self.tabs.width() - width - 24, 8), 8, width, bar.sizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "find_bar") and self.find_bar.isVisible():
            self._place_find_bar()

    def show_find(self, replace: bool = False) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        self.hide_settings()
        selected = tab.area.textCursor().selectedText()
        self.find_bar.set_read_only(tab.area.isReadOnly())
        self.find_bar.open_bar(selected if selected and chr(0x2029) not in selected else None, replace=replace)
        self._place_find_bar()
        self._count_timer.start()

    def _query(self) -> Query:
        return Query(self.find_bar.query(), self.find_bar.options())

    def _query_changed(self) -> None:
        """Typing in the find field looks for the text from where the selection starts."""
        tab = self.current_tab()
        self.context.settings_changed(apply=False)
        if tab is None:
            return
        query = self._query()
        if query.valid:
            text = self._text(tab)
            # From where the selection starts, and from the top when there is nothing below.
            found = query.find_next(text, tab.area.textCursor().selectionStart()) or query.find_next(text, 0)
            if found is not None:
                tab.area.select_range(found[0], found[1])
        self._count_timer.start()

    def find(self, backwards: bool = False) -> bool:
        """Find next or previous; opens the bar when there is nothing to look for yet."""
        tab = self.current_tab()
        if tab is None:
            return False
        if not self.find_bar.query():
            self.show_find()
            return False
        query = self._query()
        if not query.valid:
            self._update_count()
            return False
        cursor = tab.area.textCursor()
        text = self._text(tab)
        found = (query.find_previous(text, cursor.selectionStart()) if backwards
                 else query.find_next(text, cursor.selectionEnd()))
        if found is None:
            self._update_count()
            return False
        tab.area.select_range(found[0], found[1])
        self._update_count()
        return True

    def replace(self) -> None:
        """Replace the match that is selected and go to the next one."""
        tab = self.current_tab()
        if tab is None or tab.area.isReadOnly():
            return
        query = self._query()
        if not query.valid:
            self._update_count()
            return
        cursor = tab.area.textCursor()
        if cursor.hasSelection():
            new = query.replace_one(self._text(tab), cursor.selectionStart(), cursor.selectionEnd(),
                                    self.find_bar.replacement())
            if new is not None:
                cursor.insertText(new)
                tab.area.setTextCursor(cursor)
        self.find(False)

    def replace_all(self) -> int:
        tab = self.current_tab()
        if tab is None or tab.area.isReadOnly():
            return 0
        query = self._query()
        if not query.valid:
            self._update_count()
            return 0
        text, count = query.replace_all(self._text(tab), self.find_bar.replacement())
        if count:
            tab.area.replace_all_text(text)
        self.find_bar.set_count(f"{count:,} replaced" if count else "No results")
        return count

    def _update_count(self) -> None:
        tab = self.current_tab()
        bar = self.find_bar
        if tab is None or not bar.isVisible():
            return
        query = self._query()
        if query.error == "empty":
            bar.set_count("")
            return
        if not query.valid:
            bar.set_count(query.error, problem=True)
            return
        if tab.characters() > MAX_COUNTED_CHARACTERS:
            bar.set_count("")
            return
        cursor = tab.area.textCursor()
        total, before, capped = query.count(self._text(tab), cursor.selectionStart())
        if total == 0:
            bar.set_count("No results")
        elif capped:
            bar.set_count(f"{MAX_COUNT:,}+")
        else:
            selected = cursor.hasSelection() and query.replace_one(
                self._text(tab), cursor.selectionStart(), cursor.selectionEnd(), "") is not None
            bar.set_count(f"{before + 1:,} of {total:,}" if selected else f"{total:,} found")

    # --- printing ---
    def printer(self):
        if self._printer is None and printing.AVAILABLE:
            self._printer = printing.QPrinter(printing.QPrinter.PrinterMode.HighResolution)
        return self._printer

    def page_setup(self) -> None:
        if printing.AVAILABLE:
            printing.QPageSetupDialog(self.printer(), self).exec()

    def print_dialog(self) -> None:
        tab = self.current_tab()
        if not printing.AVAILABLE or tab is None or self._print_job is not None:
            return
        dialog = printing.QPrintDialog(self.printer(), self)
        if dialog.exec():
            self.print_tab(tab, self.printer())

    def print_tab(self, tab: EditorTab, printer):
        """Print a tab on ``printer`` (a QPrinter: a real one, or one that writes a PDF file)."""
        printer.setDocName(tab.name())
        font = tab.area.font()
        font.setPointSizeF(float(self.settings["font_size"]))
        job = printing.PrintJob(tab.area.document(), printer, tab.name(), font, int(self.settings["tab_width"]), self)
        self._print_job = job
        tab.show_message("info", f"Printing {tab.name()}…", "Cancel", job.cancel)

        def finished(pages: int, message: str) -> None:
            self._print_job = None
            job.deleteLater()
            if self.index_of(tab) < 0:
                return
            if pages > 0:
                tab.show_message("success", f"{tab.name()} was sent to the printer ({pages:,} "
                                 f"{'page' if pages == 1 else 'pages'}).")
            else:
                tab.show_message("warning" if pages < 0 else "error", message)
        job.finished.connect(finished)
        job.start()
        return job

    # --- files dropped on the window ---
    def dragEnterEvent(self, event) -> None:
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        files = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if files:
            event.acceptProposedAction()
            self.open_paths(files)

    def handle_arguments(self, arguments: list[str]) -> None:
        self.open_paths(arguments)
