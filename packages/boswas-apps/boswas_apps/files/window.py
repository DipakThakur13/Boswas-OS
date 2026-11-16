"""The Files window: tabs, navigation row, command bar, navigation pane, views, details pane, status bar."""

from __future__ import annotations

import os

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QKeySequence, QPainter
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QStackedWidget, QToolButton, QVBoxLayout, QWidget

import boswas_fluent as fluent
from boswas_fluent import ContentDialog, paint

from . import APP_ID, DISPLAY_NAME, archives, desktopapps, fileops, mime, places, uiicons
from .addressbar import AddressBar
from .archives import Compress, Extract
from .detailspane import DetailsPane
from .dialogs import ConflictDialog, ErrorsDialog, OpenWithDialog, PassphraseDialog, SettingsDialog
from .fileops import Delete, DeleteTrashed, Restore, Transfer, TrashOp, describe_error
from .launch import LaunchError
from .listing import Entry
from .model import TRASH
from .opspanel import OperationsPanel
from .properties import PropertiesWindow
from .settings import VIEW_LABELS
from .sidebar import PINNED, VOLUME, Sidebar
from .sidebar import KIND_ROLE as SIDE_KIND
from .sidebar import LOCATION_ROLE as SIDE_LOCATION
from .sidebar import VOLUME_ROLE as SIDE_VOLUME
from .tab import FolderTab, TabStrip
from .text import display_name, plural

VIEW_ORDER = ("icons-xlarge", "icons-large", "icons-medium", "icons-small", "list", "details", "tiles")
SORT_LABELS = (("name", "Name"), ("date", "Date modified"), ("type", "Type"), ("size", "Size"))


class _ContentFrame(QWidget):
    """The surface behind the file views: the layer colour with a round top left corner."""

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setClipRect(self.rect())
        area = QRectF(self.rect()).adjusted(0, 0, 8, 8)
        paint.rounded_rect(painter, area, fluent.theme().radius("flyout"), "layer", "stroke-surface")


class MainWindow(fluent.FluentWindow):
    """One window of Files. ``app`` is the FilesApp that owns the windows (None in tests of a single window)."""

    def __init__(self, services, app=None, locations: list[str] | None = None,
                 select: list[str] | None = None) -> None:
        super().__init__(APP_ID, DISPLAY_NAME, size=(1180, 760), minimum_size=(640, 420), icon="boswas-files")
        self.services = services
        self.app = app
        self._properties: list[PropertiesWindow] = []
        self._dialogs: list[QWidget] = []
        self._closing = False
        self._build_actions()
        self._build_ui()
        self._connect()
        for index, location in enumerate(locations or [services_default(services)]):
            self.add_tab(location, select=select if index == 0 else None)
        self._sync_all()

    # ------------------------------------------------------------------------------------------- actions
    def _action(self, key: str, text: str, slot, *, icon: str | None = None, shortcut=None, scope: str = "window",
                checkable: bool = False, tip: str = "") -> QAction:
        action = QAction(text, self)
        if icon:
            action.setIcon(uiicons.symbolic(icon))
        action.setCheckable(checkable)
        shortcuts = [QKeySequence(item) for item in ([shortcut] if isinstance(shortcut, str) else (shortcut or []))]
        if shortcuts:
            action.setShortcuts(shortcuts)
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut if scope == "window"
                                      else Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.setShortcutVisibleInContextMenu(True)
        hint = tip or text.replace("&", "").rstrip("…")
        if shortcuts:
            hint = f"{hint} ({shortcuts[0].toString(QKeySequence.SequenceFormat.NativeText)})"
        action.setToolTip(hint)
        if checkable:
            action.toggled.connect(slot)
        else:
            action.triggered.connect(lambda _checked=False: slot())
        action.setProperty("scope", scope)
        self.actions_[key] = action
        return action

    def _build_actions(self) -> None:
        self.actions_: dict[str, QAction] = {}
        a = self._action
        a("back", "Back", self.go_back, icon="go-previous", shortcut=["Alt+Left"])
        a("forward", "Forward", self.go_forward, icon="go-next", shortcut=["Alt+Right"])
        a("up", "Up", self.go_up, icon="go-up", shortcut=["Alt+Up"])
        a("refresh", "Refresh", self.refresh, icon="view-refresh", shortcut=["F5", "Ctrl+R"])
        a("new_tab", "New tab", lambda: self.add_tab(), icon="tab-new", shortcut="Ctrl+T")
        a("close_tab", "Close tab", lambda: self.close_tab(self.tabs.currentIndex()), shortcut="Ctrl+W")
        a("next_tab", "Next tab", lambda: self.cycle_tab(1), shortcut=["Ctrl+Tab", "Ctrl+PgDown"])
        a("previous_tab", "Previous tab", lambda: self.cycle_tab(-1), shortcut=["Ctrl+Shift+Tab", "Ctrl+PgUp"])
        a("new_window", "New window", self.new_window, shortcut="Ctrl+N")
        a("edit_address", "Edit address", lambda: self.address.start_editing(), shortcut=["Ctrl+L", "Alt+D"])
        a("search", "Search", self.focus_search, icon="system-search", shortcut=["Ctrl+F", "Ctrl+E", "F3"])
        a("new_folder", "Folder", self.new_folder, icon="folder-new", shortcut="Ctrl+Shift+N", tip="New folder")
        a("new_file", "Text document", self.new_file, icon="document-new", tip="New text document")
        a("cut", "Cut", self.cut, icon="edit-cut", shortcut="Ctrl+X", scope="files")
        a("copy", "Copy", self.copy, icon="edit-copy", shortcut="Ctrl+C", scope="files")
        a("paste", "Paste", self.paste, icon="edit-paste", shortcut="Ctrl+V", scope="files")
        a("rename", "Rename", self.rename, icon="document-edit", shortcut="F2", scope="files")
        a("delete", "Delete", self.delete, icon="edit-delete", shortcut="Delete", scope="files",
          tip="Move to the Trash")
        a("delete_forever", "Delete permanently", lambda: self.delete(permanent=True), shortcut="Shift+Delete",
          scope="files")
        a("copy_path", "Copy as path", self.copy_path, shortcut="Ctrl+Shift+C", scope="files")
        a("select_all", "Select all", lambda: self.tab().view.select_all(), icon="object-select", shortcut="Ctrl+A",
          scope="files")
        a("select_none", "Select none", lambda: self.tab().view.select_none())
        a("invert", "Invert selection", lambda: self.tab().view.invert_selection())
        a("undo", "Undo", self.undo, icon="edit-undo", shortcut="Ctrl+Z")
        a("redo", "Redo", self.redo, icon="edit-redo", shortcut=["Ctrl+Y", "Ctrl+Shift+Z"])
        a("properties", "Properties", self.show_properties, shortcut=["Alt+Return", "Alt+Enter"])
        a("terminal", "Open in Terminal", self.open_terminal)
        a("hidden", "Show hidden files", lambda checked: self.services.set_setting("show_hidden", bool(checked)),
          shortcut="Ctrl+H", checkable=True)
        a("details_pane", "Details pane", lambda checked: self.services.set_setting("details_pane", bool(checked)),
          icon="sidebar-show", shortcut="Alt+Shift+P", checkable=True)
        a("settings", "Settings", self.show_settings, icon="preferences-system", shortcut="Ctrl+,")
        a("empty_trash", "Empty Trash", self.empty_trash, icon="user-trash")
        a("restore", "Restore", self.restore, icon="edit-undo", tip="Put the selected items back where they were")
        a("fullscreen", "Full screen", self.toggle_fullscreen, shortcut="F11")
        self.view_group = QActionGroup(self)
        self.view_actions: dict[str, QAction] = {}
        for number, mode in enumerate(VIEW_ORDER, start=1):
            action = QAction(VIEW_LABELS[mode], self)
            action.setCheckable(True)
            action.setShortcut(QKeySequence(f"Ctrl+Shift+{number}"))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(lambda _checked=False, m=mode: self.set_view_mode(m))
            self.view_group.addAction(action)
            self.view_actions[mode] = action
        self.sort_group = QActionGroup(self)
        self.sort_actions: dict[str, QAction] = {}
        for column, label in SORT_LABELS:
            action = QAction(label, self)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, c=column: self.sort_by(c, None))
            self.sort_group.addAction(action)
            self.sort_actions[column] = action
        self.direction_group = QActionGroup(self)
        self.ascending = QAction("Ascending", self, checkable=True)
        self.descending = QAction("Descending", self, checkable=True)
        self.ascending.triggered.connect(lambda _checked=False: self.sort_by(None, False))
        self.descending.triggered.connect(lambda _checked=False: self.sort_by(None, True))
        self.direction_group.addAction(self.ascending)
        self.direction_group.addAction(self.descending)

    # ------------------------------------------------------------------------------------------- widgets
    def _menu_button(self, text: str, icon: str, menu: fluent.MenuFlyout) -> QAction:
        action = QAction(uiicons.symbolic(icon), text, self)
        action.setToolTip(text)
        action.setMenu(menu)
        self.commands.add_action(action)
        button = self.commands.button(action)
        if button is not None:
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        return action

    def _build_ui(self) -> None:
        services = self.services
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # tabs
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(8, 6, 8, 0)
        tab_row.setSpacing(4)
        self.tabs = TabStrip(central)
        self.tabs.destination_of = self._tab_destination
        self.new_tab_button = fluent.IconButton(self.actions_["new_tab"].icon(), "New tab (Ctrl+T)", central)
        self.new_tab_button.clicked.connect(lambda: self.add_tab())
        tab_row.addWidget(self.tabs)
        tab_row.addWidget(self.new_tab_button, 0, Qt.AlignmentFlag.AlignBottom)
        tab_row.addStretch(1)
        outer.addLayout(tab_row)

        # navigation row
        nav_row = QHBoxLayout()
        nav_row.setContentsMargins(8, 8, 12, 4)
        nav_row.setSpacing(4)
        self.nav_buttons: dict[str, QToolButton] = {}
        for key in ("back", "forward", "up", "refresh"):
            button = fluent.IconButton(None, "", central)
            button.setDefaultAction(self.actions_[key])
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setAccessibleName(self.actions_[key].text())
            self.nav_buttons[key] = button
            nav_row.addWidget(button)
        self.address = AddressBar(services, central)
        self.search_box = fluent.SearchBox(central, placeholder="Search")
        self.search_box.setFixedWidth(260)
        nav_row.addSpacing(4)
        nav_row.addWidget(self.address, 1)
        nav_row.addSpacing(4)
        nav_row.addWidget(self.search_box)
        outer.addLayout(nav_row)

        # command bar
        self.commands = fluent.CommandBar(central)
        self.new_menu = fluent.MenuFlyout(self)
        self.new_menu.addAction(self.actions_["new_folder"])
        self.new_menu.addAction(self.actions_["new_file"])
        self.new_action = self._menu_button("New", "list-add", self.new_menu)
        self.commands.add_separator()
        for key in ("cut", "copy", "paste", "rename", "delete"):
            self.commands.add_action(self.actions_[key])
            button = self.commands.button(self.actions_[key])
            if button is not None and not self.actions_[key].icon().isNull():
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.commands.add_action(self.actions_["restore"])
        self.commands.add_action(self.actions_["empty_trash"])
        self.commands.add_separator()
        self.sort_menu = fluent.MenuFlyout(self)
        for action in self.sort_actions.values():
            self.sort_menu.addAction(action)
        self.sort_menu.addSeparator()
        self.sort_menu.addAction(self.ascending)
        self.sort_menu.addAction(self.descending)
        self.sort_action = self._menu_button("Sort", "view-sort-ascending", self.sort_menu)
        self.view_menu = fluent.MenuFlyout(self)
        for mode in VIEW_ORDER:
            self.view_menu.addAction(self.view_actions[mode])
        self.view_menu.addSeparator()
        self.view_menu.addAction(self.actions_["details_pane"])
        self.view_menu.addAction(self.actions_["hidden"])
        self.view_action = self._menu_button("View", "view-grid", self.view_menu)
        for key in ("undo", "redo", "select_all", "select_none", "invert", "properties", "terminal", "hidden",
                    "settings"):
            self.commands.add_action(self.actions_[key], primary=False)
        self.details_button = QToolButton(central)
        self.details_button.setDefaultAction(self.actions_["details_pane"])
        self.details_button.setText("Details")
        self.details_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_button.setFixedHeight(32)
        self.commands.add_widget(self.details_button)
        command_row = QHBoxLayout()
        command_row.setContentsMargins(8, 4, 12, 8)
        command_row.addWidget(self.commands)
        outer.addLayout(command_row)

        self.banner = fluent.InfoBar("info", parent=central)
        self.banner.hide()
        banner_row = QHBoxLayout()
        banner_row.setContentsMargins(12, 0, 12, 8)
        banner_row.addWidget(self.banner)
        outer.addLayout(banner_row)
        self._banner_timer = QTimer(self)
        self._banner_timer.setSingleShot(True)
        self._banner_timer.timeout.connect(self.banner.hide)

        # panes
        self.splitter = QSplitter(Qt.Orientation.Horizontal, central)
        self.splitter.setChildrenCollapsible(False)
        self.sidebar = Sidebar(services, self.splitter)
        frame = _ContentFrame(self.splitter)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(1, 1, 0, 0)
        frame_layout.setSpacing(0)
        self.pages = QStackedWidget(frame)
        frame_layout.addWidget(self.pages, 1)
        self.details = DetailsPane(services, self.splitter)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(frame)
        self.splitter.addWidget(self.details)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([int(services.settings.get("navigation_width")), 700,
                                int(services.settings.get("details_width"))])
        outer.addWidget(self.splitter, 1)

        # status bar
        status = QWidget(central)
        status.setFixedHeight(30)
        status_row = QHBoxLayout(status)
        status_row.setContentsMargins(16, 0, 8, 0)
        status_row.setSpacing(16)
        self.count_label = fluent.Label("", "caption", status, tone="secondary")
        self.selection_label = fluent.Label("", "caption", status, tone="secondary")
        status_row.addWidget(self.count_label)
        status_row.addWidget(self.selection_label)
        status_row.addStretch(1)
        self.status_details = fluent.IconButton(uiicons.symbolic("view-list"), "Details view (Ctrl+Shift+6)", status,
                                                size=26, icon_size=14, checkable=True)
        self.status_icons = fluent.IconButton(uiicons.symbolic("view-grid"), "Large icons (Ctrl+Shift+2)", status,
                                              size=26, icon_size=14, checkable=True)
        self.status_details.clicked.connect(lambda: self.set_view_mode("details"))
        self.status_icons.clicked.connect(lambda: self.set_view_mode("icons-large"))
        status_row.addWidget(self.status_details)
        status_row.addWidget(self.status_icons)
        outer.addWidget(status)

        self.set_content(central)
        self.operations = OperationsPanel(services.jobs, central)
        for action in self.actions_.values():
            if action.property("scope") == "files":
                self.pages.addAction(action)
            else:
                self.addAction(action)
        for action in self.view_actions.values():
            self.addAction(action)
        self.details.setVisible(bool(services.settings.get("details_pane")))

    def _connect(self) -> None:
        services = self.services
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.middle_clicked.connect(self.close_tab)
        self.tabs.dropped.connect(self._dropped_on_tab)
        self.tabs.context_requested.connect(self._tab_context)
        self.address.location_entered.connect(self._address_entered)
        self.address.crumb_clicked.connect(lambda location: self.tab().navigate(location))
        self.search_box.query_submitted.connect(self._search)
        self.search_box.cleared.connect(lambda: self.tab().stop_search())
        self.search_box.textChanged.connect(self._search_typed)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(450)
        self._search_timer.timeout.connect(lambda: self._search(self.search_box.query()))
        self.sidebar.location_activated.connect(lambda location: self.tab().navigate(location))
        self.sidebar.open_in_new_tab.connect(lambda location: self.add_tab(location))
        self.sidebar.volume_activated.connect(self.activate_volume)
        self.sidebar.eject_requested.connect(self.eject_volume)
        self.sidebar.context_requested.connect(self._sidebar_context)
        self.sidebar.dropped.connect(self.transfer)
        self.splitter.splitterMoved.connect(self._splitter_moved)
        services.settings_changed.connect(self._setting_changed)
        services.undo_changed.connect(self._sync_commands)
        services.volumes.changed.connect(self._volumes_changed)
        services.volumes.notice.connect(lambda severity, text: self.notify(severity, text))
        services.jobs.conflict_handler = self._conflict
        QGuiApplication.clipboard().dataChanged.connect(self._sync_commands)

    # ------------------------------------------------------------------------------------------- tabs
    def tab(self) -> FolderTab:
        return self.pages.currentWidget()        # type: ignore[return-value]

    def tab_at(self, index: int) -> FolderTab | None:
        return self.tabs.tabData(index) if 0 <= index < self.tabs.count() else None

    def all_tabs(self) -> list[FolderTab]:
        return [self.tabs.tabData(index) for index in range(self.tabs.count())]

    def add_tab(self, location: str | None = None, select: list[str] | None = None,
                activate: bool = True) -> FolderTab:
        tab = FolderTab(self.services, self.pages)
        self.pages.addWidget(tab)
        index = self.tabs.addTab("")
        self.tabs.setTabData(index, tab)
        tab.location_changed.connect(lambda t=tab: self._tab_location_changed(t))
        tab.status_changed.connect(lambda t=tab: self._tab_status_changed(t))
        view = tab.view
        view.activated.connect(self.open_entry)
        view.context_requested.connect(self._view_context)
        view.middle_clicked.connect(lambda entry: self.add_tab(entry.path, activate=False)
                                    if entry is not None and entry.is_dir else None)
        view.dropped.connect(self.transfer)
        view.up_requested.connect(self.go_up)
        view.escape_pressed.connect(self._escape)
        view.mode_changed.connect(lambda _mode: self._sync_view_actions())
        tab.model.rename_requested.connect(self.rename_entry)
        tab.volumes_page.activated.connect(self.activate_volume)
        tab.volumes_page.context_requested.connect(lambda volume, position: self._volume_menu(volume).popup(position))
        tab.navigate(location or services_default(self.services), record=False, select=select)
        if activate:
            self.tabs.setCurrentIndex(index)
            if self.pages.currentWidget() is not tab:
                self.pages.setCurrentWidget(tab)
            tab.focus_view()
        self._sync_all()
        return tab

    def close_tab(self, index: int) -> None:
        tab = self.tab_at(index)
        if tab is None:
            return
        if self.tabs.count() == 1:
            self.close()
            return
        self.tabs.removeTab(index)
        self.pages.removeWidget(tab)
        tab.shutdown()
        tab.deleteLater()
        self._sync_all()

    def cycle_tab(self, step: int) -> None:
        count = self.tabs.count()
        if count > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % count)

    def _tab_changed(self, index: int) -> None:
        tab = self.tab_at(index)
        if tab is None:
            return
        self.pages.setCurrentWidget(tab)
        tab.focus_view()
        self._sync_all()

    def _tab_destination(self, index: int) -> str | None:
        tab = self.tab_at(index)
        if tab is None:
            return None
        if tab.location == places.TRASH:
            return places.TRASH
        return tab.directory() if tab.is_folder() else None

    def _dropped_on_tab(self, index: int, paths: list[str], action: str) -> None:
        destination = self._tab_destination(index)
        if destination:
            self.transfer(paths, destination, action)

    def _tab_context(self, index: int, position: QPoint) -> None:
        menu = fluent.MenuFlyout(self)
        tab = self.tab_at(index)
        menu.add_item("Close tab", lambda: self.close_tab(self.tabs.currentIndex() if tab is None
                                                          else self.all_tabs().index(tab)), shortcut="Ctrl+W")
        menu.add_item("Close other tabs", lambda: self.close_other_tabs(tab), enabled=self.tabs.count() > 1)
        menu.add_item("Duplicate tab", lambda: self.add_tab(tab.location) if tab else None)
        menu.popup(position)
        self._menu = menu

    def close_other_tabs(self, keep: FolderTab | None) -> None:
        for other in self.all_tabs():
            if other is not keep and self.tabs.count() > 1:
                self.close_tab(self.all_tabs().index(other))

    def _tab_location_changed(self, tab: FolderTab) -> None:
        tabs = self.all_tabs()
        if tab in tabs:
            index = tabs.index(tab)
            self.tabs.setTabText(index, tab.title())
            self.tabs.setTabToolTip(index, display_name(tab.location) if tab.directory() else tab.title())
            self.tabs.setTabIcon(index, self._location_icon(tab.location))
        if tab is self.tab():
            self._sync_all()

    def _tab_status_changed(self, tab: FolderTab) -> None:
        if tab is self.tab():
            self._sync_status()
            self._sync_commands()
            self._sync_details()

    # ------------------------------------------------------------------------------------------- state
    def _location_icon(self, location: str):
        if location == places.TRASH:
            return uiicons.named("user-trash")
        if location == places.COMPUTER:
            return uiicons.named("computer")
        if location == places.home():
            return uiicons.named("user-home", "folder")
        for _label, path, icon in self.services.user_dirs:
            if path == location:
                return uiicons.named(icon, "folder")
        volume = next((v for v in self.services.volumes.volumes if v.mount_point == location), None)
        if volume is not None:
            return uiicons.named(volume.icon, "drive-harddisk")
        return uiicons.named("folder")

    def _sync_all(self) -> None:
        if self.pages.count() == 0 or self.tab() is None:
            return
        tab = self.tab()
        crumbs = places.crumbs(tab.location, self.services.volumes.shown_mounts())
        if tab.search_query:
            crumbs.append((f"Search: {tab.search_query}", tab.location))
        self.address.set_location(tab.location, crumbs, self._location_icon(tab.location))
        self.sidebar.set_current_location(tab.location if not tab.search_query else "")
        self.setWindowTitle(f"{tab.title()} – {DISPLAY_NAME}")
        if self.search_box.query() != tab.search_query and not self.search_box.hasFocus():
            blocked = self.search_box.blockSignals(True)
            self.search_box.setText(tab.search_query)
            self.search_box.blockSignals(blocked)
            self.search_box.clear_action.setVisible(bool(tab.search_query))
        self.search_box.setPlaceholderText(f"Search {tab.title()}" if tab.directory() and not tab.search_query
                                           else "Search")
        self.search_box.setEnabled(tab.directory() is not None)
        self._sync_view_actions()
        self._sync_commands()
        self._sync_status()
        self._sync_details()

    def _sync_view_actions(self) -> None:
        tab = self.tab()
        mode = tab.view.view_mode
        if mode in self.view_actions:
            self.view_actions[mode].setChecked(True)
        self.status_details.setChecked(mode == "details")
        self.status_icons.setChecked(mode == "icons-large")
        column = tab.model.sort_column
        if column in self.sort_actions:
            self.sort_actions[column].setChecked(True)
        (self.descending if tab.model.sort_descending else self.ascending).setChecked(True)
        self.sort_action.setIcon(uiicons.symbolic("view-sort-descending" if tab.model.sort_descending
                                                  else "view-sort-ascending"))
        self.actions_["hidden"].setChecked(bool(self.services.settings.get("show_hidden")))
        self.actions_["details_pane"].setChecked(bool(self.services.settings.get("details_pane")))

    def _sync_commands(self) -> None:
        if self.pages.count() == 0 or self.tab() is None:
            return
        tab = self.tab()
        actions = self.actions_
        in_trash = tab.location == places.TRASH
        on_computer = tab.location == places.COMPUTER
        folder = tab.directory() is not None
        writable_place = tab.is_folder()
        selected = tab.view.selected_count() if tab.shows_view() else 0
        actions["back"].setEnabled(bool(tab.back))
        actions["forward"].setEnabled(bool(tab.forward))
        actions["up"].setEnabled(tab.parent_location() is not None)
        for key in ("new_folder", "new_file"):
            actions[key].setEnabled(writable_place)
        self.new_action.setEnabled(writable_place)
        actions["cut"].setEnabled(selected > 0 and not in_trash)
        actions["copy"].setEnabled(selected > 0 and not in_trash)
        actions["copy_path"].setEnabled(selected > 0)
        paths, _cut = self.services.clipboard_files() if writable_place else ([], False)
        actions["paste"].setEnabled(writable_place and bool(paths))
        actions["rename"].setEnabled(selected == 1 and not in_trash)
        actions["delete"].setEnabled(selected > 0)
        actions["delete"].setToolTip("Delete permanently (Del)" if in_trash else "Move to the Trash (Del)")
        actions["delete_forever"].setEnabled(selected > 0)
        actions["restore"].setVisible(in_trash)
        actions["restore"].setEnabled(in_trash and selected > 0)
        actions["empty_trash"].setVisible(in_trash)
        actions["empty_trash"].setEnabled(in_trash and bool(tab.model.rows))
        for key in ("select_all", "select_none", "invert"):
            actions[key].setEnabled(tab.shows_view())
        actions["properties"].setEnabled(not on_computer or tab.volumes_page.current_volume() is not None)
        actions["terminal"].setEnabled(folder)
        self.sort_action.setEnabled(not on_computer)
        self.view_action.setEnabled(True)
        undo = self.services.undo
        actions["undo"].setEnabled(undo.can_undo())
        actions["undo"].setText(f"Undo {undo.undo_label()}" if undo.can_undo() else "Undo")
        actions["redo"].setEnabled(undo.can_redo())
        actions["redo"].setText(f"Redo {undo.redo_label()}" if undo.can_redo() else "Redo")

    def _sync_status(self) -> None:
        left, right = self.tab().status_texts()
        self.count_label.setText(left)
        self.selection_label.setText(right)

    def _sync_details(self) -> None:
        if not self.details.isVisible():
            return
        tab = self.tab()
        entries = tab.selected_entries()
        if len(entries) == 1:
            if self.details.shown_path() != entries[0].path:
                self.details.show_entry(entries[0])
        elif len(entries) > 1:
            self.details.show_many(entries)
        else:
            count = len(tab.model.rows) if tab.location != places.COMPUTER else len(self.services.volumes.volumes)
            self.details.show_folder(tab.title(), tab.location, count, self._location_icon(tab.location))

    def _setting_changed(self, key: str) -> None:
        settings = self.services.settings
        if key == "show_hidden":
            for tab in self.all_tabs():
                tab.model.set_show_hidden(bool(settings.get(key)))
        elif key == "show_extensions":
            for tab in self.all_tabs():
                tab.model.set_show_extensions(bool(settings.get(key)))
        elif key == "single_click":
            for tab in self.all_tabs():
                tab.view.single_click = bool(settings.get(key))
        elif key == "view_mode":
            for tab in self.all_tabs():
                tab.view.set_view_mode(str(settings.get(key)))
        elif key == "details_pane":
            self.details.setVisible(bool(settings.get(key)))
            self._sync_details()
        self._sync_view_actions()

    def _volumes_changed(self) -> None:
        mounted = {volume.mount_point for volume in self.services.volumes.volumes if volume.mount_point}
        for tab in self.all_tabs():
            # A tab inside a volume that went away has nothing to show any more.
            location = tab.location
            if tab.directory() and not os.path.isdir(location) and not any(
                    location == point or location.startswith(point.rstrip("/") + "/") for point in mounted
                    if point != "/"):
                tab.navigate(places.COMPUTER, record=False)
        self._sync_all()

    def _splitter_moved(self, *_args) -> None:
        sizes = self.splitter.sizes()
        self.services.settings.set("navigation_width", max(120, min(1200, sizes[0])))
        if self.details.isVisible() and sizes[2] > 0:
            self.services.settings.set("details_width", max(120, min(1200, sizes[2])))

    def notify(self, severity: str, text: str, title: str = "") -> None:
        """Tell the user something in the bar under the commands; successes and notes go away by themselves."""
        self.banner.show_message(severity, text, title)
        self._banner_timer.stop()
        if severity in ("success", "info"):
            self._banner_timer.start(6000)

    # ------------------------------------------------------------------------------------------- navigation
    def go_back(self) -> None:
        self.tab().go_back()

    def go_forward(self) -> None:
        self.tab().go_forward()

    def go_up(self) -> None:
        self.tab().go_up()

    def refresh(self) -> None:
        self.services.reload_user_dirs()
        self.tab().refresh()

    def navigate(self, location: str, select: list[str] | None = None) -> None:
        self.tab().navigate(location, select=select)

    def _address_entered(self, text: str) -> None:
        tab = self.tab()
        try:
            location = places.normalize(text, tab.location)
        except ValueError as error:
            self.notify("error", str(error))
            return
        if not places.is_virtual(location) and not os.path.lexists(location):
            self.notify("error", f"“{display_name(location)}” does not exist.")
            return
        tab.navigate(location)
        tab.focus_view()

    def focus_search(self) -> None:
        if self.search_box.isEnabled():
            self.search_box.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.search_box.selectAll()

    def _search_typed(self, text: str) -> None:
        if text.strip():
            self._search_timer.start()
        else:
            self._search_timer.stop()
            if self.tab().search_query:
                self.tab().stop_search()

    def _search(self, query: str) -> None:
        self._search_timer.stop()
        tab = self.tab()
        if not query.strip():
            tab.stop_search()
        elif query.strip() != tab.search_query:
            tab.start_search(query)

    def _escape(self) -> None:
        tab = self.tab()
        if tab.model.is_loading and tab.search_query:
            tab.model.cancel()                  # stop the search, keep what was found
            tab.status_changed.emit()
        elif tab.search_query:
            self.search_box.clear()
            tab.stop_search()
        elif self.services.cut_paths:
            self.services.clear_cut()

    def set_view_mode(self, mode: str) -> None:
        self.services.set_setting("view_mode", mode)
        self._sync_view_actions()

    def sort_by(self, column: str | None, descending: bool | None) -> None:
        tab = self.tab()
        tab.view.sort(column or tab.model.sort_column,
                      tab.model.sort_descending if descending is None else descending)
        self._sync_view_actions()

    def toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def new_window(self, locations: list[str] | None = None) -> None:
        if self.app is not None:
            self.app.new_window(locations or [self.tab().location])

    # ------------------------------------------------------------------------------------------- operations
    def _run(self, operation, select_in: str | None = None, rename: bool = False) -> None:
        self.services.jobs.run(operation, lambda result: self._finished(result, select_in, rename), owner=self)

    def _finished(self, result: fileops.Result, select_in: str | None = None, rename: bool = False,
                  undo_of: str | None = None) -> None:
        if self._closing:
            if undo_of is None:
                self.services.push_undo(result.undo)
            return
        if undo_of is None:
            self.services.push_undo(result.undo)
        else:
            self.services.undo.pushed_back(result.undo, redo=undo_of == "undo")
            self.services.undo_changed.emit()
        for tab in self.all_tabs():
            if tab.location == places.COMPUTER:
                continue
            here = select_in is not None and tab.location == select_in and tab is self.tab()
            tab.refresh(select=result.affected if here and result.affected else None, rename=rename and here)
        if result.errors:
            self.show_dialog(ErrorsDialog(self, result.title, result.errors))
        elif result.cancelled:
            self.notify("info", f"{result.title}: cancelled.")
        if result.untrashable:
            names = plural(len(result.untrashable), "item")
            if self.ask("No Trash on this volume", f"{names} cannot be moved to the Trash because the volume has "
                        "none. Delete permanently instead?", "Delete permanently", "Keep"):
                self._run(Delete(result.untrashable))

    def _conflict(self, job, conflict) -> None:
        candidates = [job.owner, self, *(self.app.windows if self.app is not None else [])]
        owner = next((window for window in candidates if isinstance(window, MainWindow) and not window._closing),
                     None)
        if owner is None:
            job.cancel()
            return
        dialog = ConflictDialog(owner, conflict)
        dialog.answered.connect(lambda choice, for_all, j=job: j.cancel() if choice == "cancel"
                                else j.answer(choice, for_all))
        owner.show_dialog(dialog)

    def show_dialog(self, dialog: ContentDialog) -> None:
        """Open a dialog without waiting for it; it deletes itself when it closes."""
        self._dialogs.append(dialog)

        def closed(_result) -> None:
            if dialog in self._dialogs:
                self._dialogs.remove(dialog)
            dialog.deleteLater()

        dialog.finished.connect(closed)
        dialog.open()

    def open_dialogs(self) -> list[QWidget]:
        return list(self._dialogs)

    def transfer(self, paths: list[str], destination: str, action: str) -> None:
        """Copy or move ``paths`` into ``destination`` (a folder, or the Trash)."""
        if not paths:
            return
        if destination == places.TRASH:
            self.trash_paths(paths)
            return
        self._run(Transfer(paths, destination, move=action == "move"), select_in=destination)

    def selected_paths(self) -> list[str]:
        return [entry.path for entry in self.tab().selected_entries()]

    def cut(self) -> None:
        paths = self.selected_paths()
        if paths and self.tab().location != places.TRASH:
            self.services.copy_files(paths, cut=True)

    def copy(self) -> None:
        paths = self.selected_paths()
        if paths and self.tab().location != places.TRASH:
            self.services.copy_files(paths, cut=False)

    def paste(self, destination: str | None = None) -> None:
        tab = self.tab()
        destination = destination or (tab.directory() if tab.is_folder() else None)
        if destination is None:
            return
        paths, cut = self.services.clipboard_files()
        if not paths:
            self.notify("info", "There are no files on the clipboard.")
            return
        if cut:
            self.services.copy_text("")
            self.services.clear_cut()
        self.transfer(paths, destination, "move" if cut else "copy")

    def copy_path(self) -> None:
        paths = self.selected_paths()
        if self.tab().location == places.TRASH:
            paths = [entry.extra.original_path for entry in self.tab().selected_entries() if entry.extra]
        if paths:
            from .clipformats import copy_as_path
            self.services.copy_text(copy_as_path(paths))
            self.notify("success", "The path was copied." if len(paths) == 1 else "The paths were copied.")

    def rename(self) -> None:
        tab = self.tab()
        if tab.shows_view() and tab.location != places.TRASH:
            tab.view.rename_current()

    def rename_entry(self, entry: Entry, new_name: str) -> None:
        tab = self.tab()

        def done(outcome) -> None:
            path, undo = outcome
            self.services.push_undo(undo)
            for other in self.all_tabs():
                if other.location != places.COMPUTER:
                    other.refresh(select=[path] if other is tab else None)

        def failed(error: Exception) -> None:
            if isinstance(error, FileExistsError):
                self.notify("error", f"Something named “{display_name(new_name)}” is already in this folder.")
            else:
                self.notify("error", str(error) if isinstance(error, ValueError) else describe_error(error),
                            "Not renamed")

        self.services.worker.submit(fileops.rename, entry.path, new_name, on_done=done, on_error=failed)

    def _create(self, function) -> None:
        tab = self.tab()
        directory = tab.directory() if tab.is_folder() else None
        if directory is None:
            return

        def done(outcome) -> None:
            path, undo = outcome
            self.services.push_undo(undo)
            if self.tab() is tab and tab.location == directory:
                tab.refresh(select=[path], rename=True)

        self.services.worker.submit(function, directory, on_done=done,
                                    on_error=lambda error: self.notify("error", describe_error(error), "Not created"))

    def new_folder(self) -> None:
        self._create(fileops.create_folder)

    def new_file(self) -> None:
        self._create(fileops.create_file)

    def delete(self, permanent: bool = False) -> None:
        tab = self.tab()
        entries = tab.selected_entries()
        if not entries:
            return
        what = f"“{display_name(entries[0].name)}”" if len(entries) == 1 else plural(len(entries), "item")
        if tab.location == places.TRASH:
            if self.ask("Delete permanently?", f"{what} will be deleted for good. This cannot be undone.",
                        "Delete", "Cancel"):
                self._run(DeleteTrashed(self.services.trash, [entry.extra for entry in entries if entry.extra]))
            return
        paths = [entry.path for entry in entries]
        if permanent:
            if self.ask("Delete permanently?", f"{what} will be deleted for good. This cannot be undone.",
                        "Delete", "Cancel"):
                self._run(Delete(paths))
            return
        if self.services.settings.get("confirm_trash") and not self.ask(
                "Move to the Trash?", f"{what} will be moved to the Trash.", "Move to Trash", "Cancel",
                default="confirm"):
            return
        self.trash_paths(paths)

    def trash_paths(self, paths: list[str]) -> None:
        self._run(TrashOp(paths, self.services.trash))

    def restore(self) -> None:
        items = [entry.extra for entry in self.tab().selected_entries() if entry.extra is not None]
        if items:
            self._run(Restore(items, self.services.trash))

    def empty_trash(self) -> None:
        if self.ask("Empty the Trash?", "Everything in the Trash will be deleted for good. This cannot be undone.",
                    "Empty Trash", "Cancel"):
            self._run(DeleteTrashed(self.services.trash))

    def undo(self) -> None:
        self._step("undo")

    def redo(self) -> None:
        self._step("redo")

    def _step(self, which: str) -> None:
        """Undo or redo one action. Steps run one after the other: each starts from what the last one left."""
        services = self.services
        if services.undo_running:
            services.undo_queue.append(which)
            return
        stack = services.undo
        records = stack.undos if which == "undo" else stack.redos
        if not records:
            return
        record = records.pop()
        operation = fileops.inverse(record, services.trash)
        services.undo_changed.emit()
        if operation is None:
            return
        services.undo_running = True

        def done(result: fileops.Result) -> None:
            services.undo_running = False
            self._finished(result, undo_of=which)
            if services.undo_queue:
                self._step(services.undo_queue.pop(0))

        services.jobs.run(operation, done, owner=self)

    def compress(self, paths: list[str]) -> None:
        if paths:
            self._run(Compress(paths), select_in=os.path.dirname(paths[0]))

    def extract(self, path: str) -> None:
        self._run(Extract(path), select_in=os.path.dirname(path))

    # ------------------------------------------------------------------------------------------- opening
    def open_entry(self, entry: Entry | None) -> None:
        if entry is None:
            return
        tab = self.tab()
        if tab.location == places.TRASH:
            self.notify("info", "Restore the item to open it.")
            return
        if entry.is_dir:
            tab.navigate(entry.path)
            tab.focus_view()
        elif entry.broken:
            self.notify("error", f"“{display_name(entry.name)}” is a link to something that does not exist.")
        else:
            self.open_file(entry.path, entry.mode)

    def _with_apps(self, callback) -> None:
        """Run ``callback`` once the applications are known (at once when they are)."""
        if self.services.apps.loaded:
            callback()
            return

        def loaded() -> None:
            self.services.apps_loaded.disconnect(loaded)
            callback()

        self.services.apps_loaded.connect(loaded)
        self.services.reload_apps()

    def open_file(self, path: str, mode: int = 0) -> None:
        """Open a file with the default application of its type. A program is never run without asking."""
        def typed(name: str) -> None:
            if mime.is_executable(path, name, mode):
                self.ask_run(path, name)
            else:
                self._with_apps(lambda: self._open_default(path, name))

        self.services.worker.submit(mime.for_file, path, on_done=typed,
                                    on_error=lambda error: self.notify("error", describe_error(error), "Not opened"))

    def _open_default(self, path: str, mime_name: str) -> None:
        app = self.services.apps.default_for(mime.chain(mime_name))
        if app is None:
            self.choose_app([path], mime_name)
        else:
            self.launch(app, [path])

    def launch(self, app, paths: list[str]) -> bool:
        try:
            self.services.launcher.open_with(app, paths)
        except LaunchError as error:
            self.notify("error", str(error))
            return False
        return True

    def ask_run(self, path: str, mime_name: str) -> None:
        name = display_name(os.path.basename(path))
        is_text = mime.is_text(mime_name) or mime_name == "application/x-desktop"
        dialog = ContentDialog(self, f"Run “{name}”?",
                               "This file is a program. Run it only if you trust where it came from.",
                               primary="Run", secondary="Open as text" if is_text else None, close="Cancel",
                               default="close")

        def answered(result) -> None:
            if result == ContentDialog.Result.PRIMARY:
                self.run_program(path, mime_name)
            elif result == ContentDialog.Result.SECONDARY:
                self._with_apps(lambda: self._open_default(path, "text/plain"))

        dialog.finished.connect(answered)
        self.show_dialog(dialog)

    def run_program(self, path: str, mime_name: str) -> None:
        try:
            if mime_name == "application/x-desktop" or path.endswith(".desktop"):
                app = desktopapps.load_app(path, os.path.basename(path))
                if app is None or not app.exec_line:
                    raise LaunchError("This desktop entry does not start a program.")
                self.services.launcher.open_with(app, [])
            else:
                self.services.launcher.run_program(path)
        except LaunchError as error:
            self.notify("error", str(error))

    def choose_app(self, paths: list[str], mime_name: str, *, force_default: bool = False, parent=None,
                   done=None) -> None:
        """Let the user choose an application for ``paths``; with "always", make it the default of the type."""
        def show() -> None:
            index = self.services.apps
            recommended = index.apps_for(mime.chain(mime_name))
            dialog = OpenWithDialog(parent or self, os.path.basename(paths[0]), mime.description(mime_name),
                                    recommended, index.visible_apps(), force_always=force_default)

            def answered(result) -> None:
                app = dialog.chosen()
                if result != ContentDialog.Result.PRIMARY or app is None:
                    return
                if dialog.make_default():
                    self.services.worker.submit(
                        index.set_default, mime_name, app.app_id,
                        on_done=lambda _none: done() if done is not None else None,
                        on_error=lambda error: self.notify("error", describe_error(error),
                                                           "The default application was not changed"))
                if not force_default:
                    self.launch(app, paths)

            dialog.finished.connect(answered)
            if parent is None or parent is self:
                self.show_dialog(dialog)
            else:
                dialog.finished.connect(lambda _result: dialog.deleteLater())
                dialog.open()
            self._last_open_with = dialog

        self._with_apps(show)

    def open_terminal(self, directory: str | None = None) -> None:
        directory = directory or self.tab().directory()
        if directory is None:
            return
        try:
            self.services.launcher.open_terminal(directory)
        except LaunchError as error:
            self.notify("error", str(error))

    def show_properties(self, paths: list[str] | None = None, volume=None) -> PropertiesWindow | None:
        tab = self.tab()
        if paths is None and volume is None:
            if tab.location == places.COMPUTER:
                volume = tab.volumes_page.current_volume()
                if volume is None:
                    return None
            else:
                paths = self.selected_paths() or ([tab.directory()] if tab.directory() else [])
                if tab.location != places.TRASH and len(paths) == 1:
                    volume = next((v for v in self.services.volumes.volumes if v.mount_point == paths[0]), None)
        if not paths and volume is None:
            return None
        window = PropertiesWindow(self.services, None if volume is not None else paths, volume,
                                  choose_app=lambda parent, path, mime_name, done: self.choose_app(
                                      [path], mime_name, force_default=True, parent=parent, done=done))
        self._properties.append(window)
        window.destroyed.connect(lambda _obj=None, w=window: self._properties.remove(w)
                                 if w in self._properties else None)
        window.show()
        return window

    def show_settings(self) -> SettingsDialog:
        dialog = SettingsDialog(self, self.services)
        self.show_dialog(dialog)
        return dialog

    # ------------------------------------------------------------------------------------------- volumes
    def activate_volume(self, volume) -> None:
        """Open a volume; mount or unlock it first when it is not mounted."""
        monitor = self.services.volumes
        tab = self.tab()

        def opened(mount_point: str | None) -> None:
            if mount_point and not self._closing:
                tab.navigate(mount_point)

        if volume.mount_point:
            tab.navigate(volume.mount_point)
        elif volume.can_unlock:
            dialog = PassphraseDialog(self, volume.name)
            dialog.finished.connect(lambda result: monitor.unlock(volume, dialog.passphrase(), opened)
                                    if result == ContentDialog.Result.PRIMARY else None)
            self.show_dialog(dialog)
        elif volume.can_mount:
            monitor.mount(volume, opened)

    def eject_volume(self, volume, eject: bool = True) -> None:
        point = (volume.mount_point or "").rstrip("/")
        if point:
            for tab in self.all_tabs():
                if tab.location == point or tab.location.startswith(point + "/"):
                    tab.navigate(places.COMPUTER, record=False)
        if eject and volume.can_eject:
            self.services.volumes.eject(volume)
        else:
            self.services.volumes.unmount(volume)

    # ------------------------------------------------------------------------------------------- menus
    def _volume_menu(self, volume) -> fluent.MenuFlyout:
        menu = fluent.MenuFlyout(self)
        if volume.mount_point:
            menu.add_item("Open", lambda: self.activate_volume(volume))
            menu.add_item("Open in new tab", lambda: self.add_tab(volume.mount_point))
            menu.add_item("Open in new window", lambda: self.new_window([volume.mount_point]))
        elif volume.can_unlock:
            menu.add_item("Unlock…", lambda: self.activate_volume(volume))
        elif volume.can_mount:
            menu.add_item("Mount", lambda: self.activate_volume(volume))
        if volume.can_unmount or volume.can_eject:
            menu.addSeparator()
        if volume.can_unmount:
            menu.add_item("Unmount", lambda: self.eject_volume(volume, eject=False))
        if volume.can_eject:
            menu.add_item("Eject", lambda: self.eject_volume(volume), icon=uiicons.symbolic("media-eject"))
        menu.addSeparator()
        menu.add_item("Properties", lambda: self.show_properties(volume=volume))
        self._menu = menu
        return menu

    def _sidebar_context(self, item, position: QPoint) -> None:
        self.sidebar_menu(item).popup(position)

    def sidebar_menu(self, item) -> fluent.MenuFlyout:
        kind = item.data(SIDE_KIND)
        location = item.data(SIDE_LOCATION)
        volume = item.data(SIDE_VOLUME)
        if kind == VOLUME and volume is not None:
            return self._volume_menu(volume)
        menu = fluent.MenuFlyout(self)
        menu.add_item("Open", lambda: self.tab().navigate(location))
        menu.add_item("Open in new tab", lambda: self.add_tab(location))
        menu.add_item("Open in new window", lambda: self.new_window([location]))
        if kind == PINNED:
            settings = self.services.settings
            pinned = settings.pinned()
            position = pinned.index(location) if location in pinned else -1
            menu.addSeparator()
            menu.add_item("Move up", lambda: self._move_pin(location, position - 1), enabled=position > 0)
            menu.add_item("Move down", lambda: self._move_pin(location, position + 1),
                          enabled=0 <= position < len(pinned) - 1)
            menu.add_item("Unpin from quick access", lambda: self.unpin(location), icon=uiicons.symbolic("view-pin"))
        if location == places.TRASH:
            menu.addSeparator()
            menu.add_item("Empty Trash", self.empty_trash)
        elif location.startswith("/"):
            menu.addSeparator()
            menu.add_item("Open in Terminal", lambda: self.open_terminal(location))
            menu.add_item("Properties", lambda: self.show_properties([location]))
        self._menu = menu
        return menu

    def _move_pin(self, path: str, position: int) -> None:
        if self.services.settings.move_pin(path, position):
            self.services.settings_changed.emit("pinned")

    def pin(self, path: str) -> None:
        if self.services.settings.pin(path):
            self.services.settings_changed.emit("pinned")

    def unpin(self, path: str) -> None:
        if self.services.settings.unpin(path):
            self.services.settings_changed.emit("pinned")

    def _view_context(self, entry: Entry | None, position: QPoint) -> None:
        self.context_menu(entry).popup(position)

    def context_menu(self, entry: Entry | None) -> fluent.MenuFlyout:
        """The context menu of the selection (``entry`` is the item under the pointer) or of the empty space."""
        tab = self.tab()
        actions = self.actions_
        menu = fluent.MenuFlyout(self)
        self._menu = menu
        entries = tab.selected_entries() if entry is not None else []
        in_trash = tab.location == places.TRASH
        if entry is None:
            if in_trash:
                menu.addAction(actions["empty_trash"])
                menu.addSeparator()
            view = menu.add_submenu("View", uiicons.symbolic("view-grid"))
            for mode in VIEW_ORDER:
                view.addAction(self.view_actions[mode])
            sort = menu.add_submenu("Sort by", uiicons.symbolic("view-sort-ascending"))
            for action in self.sort_actions.values():
                sort.addAction(action)
            sort.addSeparator()
            sort.addAction(self.ascending)
            sort.addAction(self.descending)
            menu.addAction(actions["refresh"])
            if not in_trash:
                menu.addSeparator()
                new = menu.add_submenu("New", uiicons.symbolic("list-add"))
                new.addAction(actions["new_folder"])
                new.addAction(actions["new_file"])
                new.setEnabled(tab.is_folder())
                menu.addAction(actions["paste"])
                menu.addSeparator()
                menu.addAction(actions["terminal"])
            menu.addAction(actions["select_all"])
            if tab.directory():
                menu.addSeparator()
                directory = tab.directory()
                if directory in self.services.settings.pinned():
                    menu.add_item("Unpin from quick access", lambda: self.unpin(directory))
                elif directory not in self.sidebar.locations():
                    menu.add_item("Pin to quick access", lambda: self.pin(directory), icon=uiicons.symbolic("view-pin"))
                menu.add_item("Properties", lambda: self.show_properties([directory]))
            return menu
        if in_trash:
            menu.addAction(actions["restore"])
            menu.addAction(actions["delete"])
            menu.addSeparator()
            menu.addAction(actions["copy_path"])
            menu.addAction(actions["properties"])
            return menu
        paths = [item.path for item in entries]
        single = entries[0] if len(entries) == 1 else None
        files = [item for item in entries if not item.is_dir]
        menu.add_item("Open", lambda: tab.view.activate_selection())
        if files and len(files) == len(entries):
            self._open_with_menu(menu, files)
        if single is not None and single.is_dir:
            menu.add_item("Open in new tab", lambda: self.add_tab(single.path, activate=False))
            menu.add_item("Open in new window", lambda: self.new_window([single.path]))
        if tab.search_query and single is not None:
            menu.add_item("Open file location", lambda: tab.navigate(single.path) if not single.is_dir
                          else tab.navigate(os.path.dirname(single.path), select=[single.path]))
        menu.addSeparator()
        menu.addAction(actions["cut"])
        menu.addAction(actions["copy"])
        if single is not None and single.is_dir:
            has_files = bool(self.services.clipboard_files()[0])
            menu.add_item("Paste into folder", lambda: self.paste(single.path), enabled=has_files)
        menu.addAction(actions["copy_path"])
        menu.addSeparator()
        menu.addAction(actions["rename"])
        menu.addAction(actions["delete"])
        menu.addSeparator()
        menu.add_item("Compress to ZIP", lambda: self.compress(paths))
        if single is not None and not single.is_dir and archives.is_archive(single.name):
            menu.add_item("Extract all", lambda: self.extract(single.path))
        if single is not None and single.is_dir:
            menu.addSeparator()
            if single.path in self.services.settings.pinned():
                menu.add_item("Unpin from quick access", lambda: self.unpin(single.path))
            elif single.path not in self.sidebar.locations():
                menu.add_item("Pin to quick access", lambda: self.pin(single.path), icon=uiicons.symbolic("view-pin"))
            menu.add_item("Open in Terminal", lambda: self.open_terminal(single.path))
        menu.addSeparator()
        menu.addAction(actions["properties"])
        return menu

    def _open_with_menu(self, menu: fluent.MenuFlyout, files: list[Entry]) -> None:
        paths = [item.path for item in files]
        mime_name = files[0].mime or mime.for_name(files[0].name)
        same_type = all((item.mime or mime.for_name(item.name)) == mime_name for item in files)
        submenu = menu.add_submenu("Open with")
        if self.services.apps.loaded and same_type:
            for app in self.services.apps.apps_for(mime.chain(mime_name))[:12]:
                submenu.add_item(app.name, lambda a=app: self.launch(a, paths), icon=uiicons.app_icon(app.icon))
            if submenu.actions():
                submenu.addSeparator()
        submenu.add_item("Choose another app…", lambda: self.choose_app(paths, mime_name))

    # ------------------------------------------------------------------------------------------- closing
    def closeEvent(self, event) -> None:
        last = self.app is None or len(self.app.windows) <= 1
        if last and self.services.jobs.busy():
            if not self.ask("File operations are still running", "Closing the last window cancels them.",
                            "Cancel them and close", "Keep working"):
                event.ignore()
                return
            self.services.jobs.cancel_all()
        self._closing = True
        for window in list(self._properties):
            window.close()
        for tab in self.all_tabs():
            tab.shutdown()
        super().closeEvent(event)
        if self.app is not None:
            self.app.window_closed(self)


def services_default(services) -> str:
    from .services import default_location
    return default_location(services.settings)
