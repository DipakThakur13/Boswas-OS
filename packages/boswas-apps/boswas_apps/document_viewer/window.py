"""The window of Document Viewer: tabs, the command bar, the start page with recent documents."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QIcon, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLineEdit,
                               QListView, QMenu, QProgressBar, QStackedWidget, QTabBar, QToolButton, QVBoxLayout,
                               QWidget)

import boswas_fluent as fluent

from . import APP_ID, DISPLAY_NAME, layout as lay, printing
from .document import READY
from .presentation import Presentation
from .render import Request
from .store import Store
from .tab import DocumentTab
from .view import HAND, SELECT

OPEN_FILTER = "PDF documents (*.pdf);;All files (*)"
_ZOOM_CHOICES = (("Fit width", lay.FIT_WIDTH), ("Fit page", lay.FIT_PAGE), ("Actual size", 1.0), ("50 %", 0.5),
                 ("75 %", 0.75), ("100 %", 1.0), ("125 %", 1.25), ("150 %", 1.5), ("200 %", 2.0), ("400 %", 4.0))
_RECENT_THUMB = QSize(132, 176)
_MAX_TABS = 40
_COPY_DPI = 150


class _RecentModel(QAbstractListModel):
    """Recent documents for the start page: name, folder and a picture of the first page."""

    def __init__(self, store: Store, thumbnail_path, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self._thumbnail_path = thumbnail_path
        self._icons: dict[str, QIcon] = {}
        self._placeholder = fluent.icon("application-pdf", "x-office-document")

    def refresh(self) -> None:
        self.beginResetModel()
        self._icons.clear()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.store.recent)

    def path(self, index: QModelIndex) -> str:
        return self.store.recent[index.row()]["path"]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.store.recent):
            return None
        entry = self.store.recent[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return os.path.basename(entry["path"])
        if role == Qt.ItemDataRole.ToolTipRole:
            pages = f"\n{entry['pages']:,} pages" if entry.get("pages") else ""
            title = f"\n{entry['title']}" if entry.get("title") else ""
            return f"{entry['path']}{title}{pages}"
        if role == Qt.ItemDataRole.DecorationRole:
            icon = self._icons.get(entry["path"])
            if icon is None:
                icon = self._placeholder
                image_path = self._thumbnail_path(entry["path"])
                if os.path.isfile(image_path) and os.path.getsize(image_path) < 4 * 1024 * 1024:
                    pixmap = QPixmap(image_path)
                    if not pixmap.isNull():
                        icon = QIcon(pixmap)
                self._icons[entry["path"]] = icon
            return icon
        return None


_stores: dict[str, Store] = {}


def shared_store() -> Store:
    """The Store of this process: every window of Document Viewer keeps its recent files in the same one."""
    try:
        directory = str(fluent.state_dir(APP_ID))
    except OSError:
        return Store(None)
    store = _stores.get(directory)
    if store is None:
        store = _stores[directory] = Store(directory)
    return store


class ViewerWindow(fluent.FluentWindow):
    def __init__(self, arguments: list[str] | None = None) -> None:
        super().__init__(APP_ID, DISPLAY_NAME, icon="boswas-document-viewer", size=(1180, 800), minimum_size=(640, 460))
        self.worker = fluent.Worker(self, max_threads=2)
        self.store = shared_store()
        try:
            self._thumb_dir = os.path.join(str(fluent.cache_dir(APP_ID)), "thumbnails")
            os.makedirs(self._thumb_dir, mode=0o700, exist_ok=True)
        except OSError:
            self._thumb_dir = None
        self.tabs: list[DocumentTab] = []
        self.dialog: fluent.ContentDialog | None = None
        self.presentation: Presentation | None = None
        self._print_job = None
        self._banner_do = None
        self._copy_request: Request | None = None
        self._recent_requests: dict[Request, str] = {}
        # Replaceable in tests: file choosers, the print dialog, the browser.
        self.choose_files = self._choose_files
        self.choose_save = self._choose_save
        self.run_print_dialog = printing.ask if printing.AVAILABLE else None
        self.open_url = fluent.open_url
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1500)
        self._save_timer.timeout.connect(self._remember_views)
        self._build_actions()
        self._build()
        self.setAcceptDrops(True)
        self._sync()
        self.handle_arguments(list(arguments or []))

    # --- actions ---
    def _action(self, text: str, slot, shortcut=None, icon: tuple = (), checkable: bool = False) -> QAction:
        action = QAction(text, self)
        if icon:
            action.setIcon(fluent.icon(*icon))
        if shortcut:
            shortcuts = [QKeySequence(key) for key in ((shortcut,) if isinstance(shortcut, str) else shortcut)]
            action.setShortcuts(shortcuts)
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.setToolTip(f"{text} ({shortcuts[0].toString(QKeySequence.SequenceFormat.NativeText)})")
        else:
            action.setToolTip(text)
        action.setCheckable(checkable)
        if checkable:
            action.toggled.connect(lambda _on: slot())
        else:
            action.triggered.connect(lambda _checked=False: slot())
        self.addAction(action)
        return action

    def _build_actions(self) -> None:
        a = self._action
        self.open_action = a("Open", self.browse, "Ctrl+O", ("document-open",))
        self.save_action = a("Save a copy", self.save_copy, "Ctrl+S", ("document-save", "document-save-as"))
        self.print_action = a("Print", self.print_document, "Ctrl+P", ("document-print",))
        self.print_action.setVisible(printing.AVAILABLE)
        self.close_action = a("Close tab", self.close_current_tab, "Ctrl+W", ("window-close",))
        self.sidebar_action = a("Sidebar", self._toggle_sidebar, "F4", ("sidebar-show", "view-list"), checkable=True)
        self.previous_action = a("Previous page", lambda: self._view_call("previous_page"), None, ("go-up",))
        self.next_action = a("Next page", lambda: self._view_call("next_page"), None, ("go-down",))
        self.go_to_action = a("Go to page…", self.focus_page_box, "Ctrl+G")
        self.back_action = a("Back", lambda: self._view_call("go_back"), "Alt+Left", ("go-previous",))
        self.forward_action = a("Forward", lambda: self._view_call("go_forward"), "Alt+Right", ("go-next",))
        self.zoom_out_action = a("Zoom out", lambda: self._view_call("zoom_out"), ("Ctrl+-",), ("zoom-out",))
        self.zoom_in_action = a("Zoom in", lambda: self._view_call("zoom_in"), ("Ctrl++", "Ctrl+="), ("zoom-in",))
        self.fit_width_action = a("Fit width", lambda: self._set_zoom_mode(lay.FIT_WIDTH), "Ctrl+2",
                                  ("zoom-fit-width",))
        self.fit_page_action = a("Fit page", lambda: self._set_zoom_mode(lay.FIT_PAGE), "Ctrl+0",
                                 ("zoom-fit-best", "zoom-fit-page"))
        self.actual_size_action = a("Actual size", lambda: self._set_zoom(1.0), "Ctrl+1", ("zoom-original",))
        self.rotate_left_action = a("Rotate left", lambda: self._rotate(-1), "Ctrl+[", ("object-rotate-left",))
        self.rotate_right_action = a("Rotate right", lambda: self._rotate(1), "Ctrl+]", ("object-rotate-right",))
        self.continuous_action = a("Continuous scrolling", self._arrangement_changed, None, checkable=True)
        self.continuous_action.setChecked(True)
        self.one_page_action = a("One page", self._arrangement_changed, None, checkable=True)
        self.two_pages_action = a("Two pages", self._arrangement_changed, None, checkable=True)
        self.cover_action = a("Two pages, first page alone", self._arrangement_changed, None, checkable=True)
        self._columns_group = QActionGroup(self)
        for action in (self.one_page_action, self.two_pages_action, self.cover_action):
            self._columns_group.addAction(action)
        self.one_page_action.setChecked(True)
        self.select_tool_action = a("Select text", self._tool_changed, None, ("edit-select-text", "edit-select"),
                                    checkable=True)
        self.hand_tool_action = a("Hand tool: drag to move the page", self._tool_changed, None,
                                  ("transform-browse", "hand"), checkable=True)
        self._tool_group = QActionGroup(self)
        self._tool_group.addAction(self.select_tool_action)
        self._tool_group.addAction(self.hand_tool_action)
        self.select_tool_action.setChecked(True)
        self.find_action = a("Find", self.find, "Ctrl+F", ("edit-find", "system-search"))
        self.find_next_action = a("Find next", lambda: self._tab_call("find_next"), "F3")
        self.find_previous_action = a("Find previous", lambda: self._tab_call("find_previous"), "Shift+F3")
        self.copy_action = a("Copy", self.copy, "Ctrl+C", ("edit-copy",))
        self.select_all_action = a("Select all on this page", lambda: self._view_call("select_all_on_page"), "Ctrl+A")
        self.copy_page_action = a("Copy page as a picture", self.copy_page_image, None)
        self.search_selection_action = a("Search for the selection", self.search_selection, None, ("edit-find",))
        self.fullscreen_action = a("Full screen", self.toggle_fullscreen, "F11", ("view-fullscreen",))
        self.presentation_action = a("Presentation", self.start_presentation, "F5",
                                     ("view-presentation", "media-playback-start"))
        self.properties_action = a("Document properties", self.show_properties, "Alt+Return",
                                   ("document-properties",))
        self.next_tab_action = a("Next tab", lambda: self._step_tab(1), ("Ctrl+Tab", "Ctrl+PgDown"))
        self.previous_tab_action = a("Previous tab", lambda: self._step_tab(-1), ("Ctrl+Shift+Tab", "Ctrl+PgUp"))
        self.escape_action = a("Leave", self._escape, "Esc")
        self._document_actions = [
            self.save_action, self.print_action, self.close_action, self.sidebar_action, self.previous_action,
            self.next_action, self.go_to_action, self.zoom_out_action, self.zoom_in_action, self.fit_width_action,
            self.fit_page_action, self.actual_size_action, self.rotate_left_action, self.rotate_right_action,
            self.continuous_action, self.one_page_action, self.two_pages_action, self.cover_action,
            self.select_tool_action, self.hand_tool_action, self.find_action, self.find_next_action,
            self.find_previous_action, self.select_all_action, self.copy_page_action, self.presentation_action,
            self.properties_action]

    def _button(self, action: QAction, text: str = "") -> QToolButton:
        """A command bar button for ``action``: its icon, or a short word when this system has no such icon."""
        button = QToolButton()
        button.setDefaultAction(action)
        button.setAccessibleName(action.text())
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.setFixedHeight(32)
        if action.icon().isNull():
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setText(text or action.text())
        else:
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setIconSize(QSize(16, 16))
            button.setFixedWidth(34)
        return button

    # --- construction ---
    def _build(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.tab_bar = QTabBar()
        self.tab_bar.setTabsClosable(True)
        self.tab_bar.setMovable(True)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.tab_bar.setUsesScrollButtons(True)
        self.tab_bar.setDocumentMode(True)
        self.tab_bar.setAccessibleName("Open documents")
        self.tab_bar.currentChanged.connect(self._tab_changed)
        self.tab_bar.tabCloseRequested.connect(self.close_tab)
        self.tab_bar.tabMoved.connect(self._tab_moved)
        self.tab_row = QWidget()
        tab_layout = QHBoxLayout(self.tab_row)
        tab_layout.setContentsMargins(8, 6, 8, 0)
        tab_layout.addWidget(self.tab_bar, 1)
        outer.addWidget(self.tab_row)

        self.bar = QWidget()
        bar = QHBoxLayout(self.bar)
        bar.setContentsMargins(8, 6, 8, 6)
        bar.setSpacing(2)
        self._groups: list[tuple[int, list[QWidget]]] = []      # (priority, widgets): low priorities hide first

        def group(priority: int, *widgets: QWidget) -> None:
            separator = fluent.Separator(Qt.Orientation.Vertical)
            separator.setFixedHeight(20)
            members = [*widgets]
            for widget in widgets:
                bar.addWidget(widget)
            bar.addSpacing(4)
            bar.addWidget(separator)
            bar.addSpacing(4)
            self._groups.append((priority, [*members, separator]))

        self.page_box = QLineEdit()
        self.page_box.setFixedWidth(58)
        self.page_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_box.setAccessibleName("Page number")
        self.page_box.setToolTip("Page number (Ctrl+G)")
        self.page_box.returnPressed.connect(self._page_entered)
        self.page_box.editingFinished.connect(self._sync_page)
        self.page_total = fluent.Label("", "body", tone="secondary")
        self.page_total.setMinimumWidth(48)
        self.zoom_box = QComboBox()
        self.zoom_box.setEditable(True)
        self.zoom_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.zoom_box.setFixedWidth(112)
        self.zoom_box.setAccessibleName("Zoom")
        self.zoom_box.setToolTip("Zoom")
        for text, _value in _ZOOM_CHOICES:
            self.zoom_box.addItem(text)
        self.zoom_box.activated.connect(self._zoom_chosen)
        self.zoom_box.lineEdit().returnPressed.connect(self._zoom_entered)
        self.zoom_box.lineEdit().editingFinished.connect(self._sync_zoom)

        self.view_button = QToolButton()
        self.view_button.setText("Page view")
        self.view_button.setToolTip("How pages are arranged")
        self.view_button.setAccessibleName("Page view")
        self.view_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.view_button.setFixedHeight(32)
        self.view_menu = QMenu(self.view_button)
        self.view_menu.addAction(self.continuous_action)
        self.view_menu.addSeparator()
        for action in (self.one_page_action, self.two_pages_action, self.cover_action):
            self.view_menu.addAction(action)
        self.view_button.setMenu(self.view_menu)

        self.more_button = QToolButton()
        self.more_button.setIcon(fluent.glyph_icon("more"))
        self.more_button.setToolTip("More")
        self.more_button.setAccessibleName("More commands")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_button.setProperty("noArrow", True)
        self.more_button.setFixedSize(34, 32)
        self.more_menu = QMenu(self.more_button)
        for item in (self.open_action, self.save_action, self.print_action, None, self.find_action, self.copy_action,
                     self.select_all_action, self.copy_page_action, None, self.go_to_action, self.back_action,
                     self.forward_action, None, self.fit_width_action, self.fit_page_action, self.actual_size_action,
                     self.rotate_left_action, self.rotate_right_action, None, self.select_tool_action,
                     self.hand_tool_action, None, self.fullscreen_action, self.presentation_action,
                     self.properties_action, None, self.close_action):
            self.more_menu.addSeparator() if item is None else self.more_menu.addAction(item)
        self.more_button.setMenu(self.more_menu)

        b = self._button
        group(9, b(self.sidebar_action, "Sidebar"))
        file_buttons = [b(self.open_action, "Open"), b(self.save_action, "Save")]
        if printing.AVAILABLE:                      # without QtPrintSupport there is no Print command at all
            file_buttons.append(b(self.print_action, "Print"))
        group(8, *file_buttons)
        group(10, b(self.previous_action, "▲"), self.page_box, self.page_total, b(self.next_action, "▼"))
        group(10, b(self.zoom_out_action, "−"), self.zoom_box, b(self.zoom_in_action, "+"))
        group(4, b(self.fit_width_action, "Width"), b(self.fit_page_action, "Page"), b(self.actual_size_action, "1:1"))
        group(3, b(self.rotate_left_action, "⟲"), b(self.rotate_right_action, "⟳"))
        group(5, self.view_button)
        group(2, b(self.select_tool_action, "Text"), b(self.hand_tool_action, "Hand"))
        bar.addStretch(1)
        self.find_button = b(self.find_action, "Find")
        self.fullscreen_button = b(self.fullscreen_action, "Full screen")
        self.presentation_button = b(self.presentation_action, "Present")
        for widget in (self.find_button, self.fullscreen_button, self.presentation_button, self.more_button):
            bar.addWidget(widget)
        self._groups.append((6, [self.fullscreen_button, self.presentation_button]))
        outer.addWidget(self.bar)

        self.banner = fluent.InfoBar("info")
        self.banner.action_triggered.connect(self._banner_action)
        self.banner.hide()
        banner_holder = QVBoxLayout()
        banner_holder.setContentsMargins(12, 0, 12, 4)
        banner_holder.addWidget(self.banner)
        outer.addLayout(banner_holder)

        self.stack = QStackedWidget()
        self.start_page = self._build_start()
        self.stack.addWidget(self.start_page)
        outer.addWidget(self.stack, 1)

        self.status_label = fluent.Label("", "caption", tone="secondary")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_row = QWidget()
        status = QHBoxLayout(self.status_row)
        status.setContentsMargins(12, 3, 12, 5)
        status.addWidget(self.status_label, 1)
        outer.addWidget(self.status_row)
        self.set_content(central)

    def _build_start(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 16, 32, 24)
        layout.setSpacing(12)
        self.welcome = fluent.EmptyState("Open a PDF document", "Drop a document here, or choose one to read.",
                                         "boswas-document-viewer", "Open document")
        self.welcome.action_triggered.connect(self.browse)
        self.welcome.setSizePolicy(self.welcome.sizePolicy().horizontalPolicy(), self.welcome.sizePolicy().Policy.Fixed)
        self.recent_title = fluent.Label("Recent documents", "body-strong")
        self.recent_model = _RecentModel(self.store, self.thumbnail_path, self)
        self.recent_view = QListView()
        self.recent_view.setModel(self.recent_model)
        self.recent_view.setViewMode(QListView.ViewMode.IconMode)
        self.recent_view.setIconSize(_RECENT_THUMB)
        self.recent_view.setGridSize(QSize(_RECENT_THUMB.width() + 40, _RECENT_THUMB.height() + 56))
        self.recent_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.recent_view.setMovement(QListView.Movement.Static)
        self.recent_view.setWordWrap(True)
        self.recent_view.setUniformItemSizes(True)
        self.recent_view.setFrameShape(QFrame.Shape.NoFrame)
        self.recent_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.recent_view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.recent_view.setAccessibleName("Recent documents")
        self.recent_view.clicked.connect(lambda index: self.open_document(self.recent_model.path(index)))
        self.recent_view.activated.connect(lambda index: self.open_document(self.recent_model.path(index)))
        layout.addWidget(self.welcome)
        layout.addSpacing(8)
        layout.addWidget(self.recent_title)
        layout.addWidget(self.recent_view, 1)
        self.no_recent = QWidget()
        layout.addWidget(self.no_recent, 1)
        return page

    # --- hooks ---
    def _choose_files(self) -> list[str]:
        start = os.path.dirname(self.tabs[-1].path) if self.tabs else os.path.expanduser("~")
        files, _filter = QFileDialog.getOpenFileNames(self, "Open document", start, OPEN_FILTER)
        return list(files)

    def _choose_save(self, suggestion: str) -> str | None:
        path, _filter = QFileDialog.getSaveFileName(self, "Save a copy", suggestion, OPEN_FILTER)
        return path or None

    # --- messages ---
    def notice(self, severity: str, message: str, title: str = "", action: str | None = None, do=None) -> None:
        self._banner_do = do
        self.banner.set_action(action)
        self.banner.show_message(severity, message, title)

    def _banner_action(self) -> None:
        do, self._banner_do = self._banner_do, None
        self.banner.hide()
        if do is not None:
            do()

    def _show_dialog(self, dialog: fluent.ContentDialog, on_finished=None) -> None:
        self.dialog = dialog

        def finished(result) -> None:
            if self.dialog is dialog:
                self.dialog = None
            if on_finished is not None:
                on_finished(result)
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.open()

    # --- tabs ---
    def current_tab(self) -> DocumentTab | None:
        if not hasattr(self, "tab_bar"):            # an action fired while the window is being built
            return None
        index = self.tab_bar.currentIndex()
        return self.tabs[index] if 0 <= index < len(self.tabs) else None

    def ready_tab(self) -> DocumentTab | None:
        tab = self.current_tab()
        return tab if tab is not None and tab.is_ready() else None

    def handle_arguments(self, arguments: list[str]) -> None:
        """Open the documents named on a command line (also of a later start of the program)."""
        for argument in arguments:
            if argument and not argument.startswith("-"):
                self.open_document(argument)

    def browse(self) -> None:
        for path in self.choose_files():
            self.open_document(path)

    def open_document(self, path: str) -> DocumentTab | None:
        """Show the document at ``path`` in a tab (the one it is already in, if any)."""
        path = os.path.abspath(path)
        for index, tab in enumerate(self.tabs):
            if tab.path == path:
                self.tab_bar.setCurrentIndex(index)
                return tab
        if len(self.tabs) >= _MAX_TABS:
            self.notice("warning", f"{_MAX_TABS} documents are open. Close one to open another.")
            return None
        tab = DocumentTab(path)
        tab.changed.connect(lambda tab=tab: self._tab_updated(tab))
        tab.status.connect(self._status)
        tab.web_link.connect(self._web_link)
        tab.changed_on_disk.connect(lambda tab=tab: self._changed_on_disk(tab))
        tab.context_requested.connect(self._context_menu)
        tab.view.view_changed.connect(self._save_timer.start)
        tab.view.page_changed.connect(lambda _page: self._save_timer.start())
        tab.sidebar.tab_changed.connect(self._sidebar_tab_changed)
        self.tabs.append(tab)
        self.stack.addWidget(tab)
        index = self.tab_bar.addTab(tab.title())
        self.tab_bar.setTabToolTip(index, path)
        tab.sidebar.setVisible(self.store.sidebar)
        tab.sidebar.show_tab(self.store.sidebar_tab)
        self.tab_bar.setCurrentIndex(index)
        tab.load(view_state=self.store.view(path))
        self._loaded(tab)
        self._tab_changed(self.tab_bar.currentIndex())
        return tab

    def _loaded(self, tab: DocumentTab) -> None:
        if tab.is_ready() and not getattr(tab, "_recorded", False):
            tab._recorded = True
            self.store.add_recent(tab.path, tab.document.title(), tab.document.page_count)
            self.store.save()
            self._make_thumbnail(tab)

    def _tab_updated(self, tab: DocumentTab) -> None:
        self._loaded(tab)
        if tab is self.current_tab():
            self._sync()

    def _tab_changed(self, index: int) -> None:
        tab = self.current_tab()
        self.stack.setCurrentWidget(tab if tab is not None else self.start_page)
        self.banner.hide()
        self._status("")
        self._sync()
        if tab is not None and getattr(tab, "_reload_offer", False):
            tab._reload_offer = False
            self.notice("info", "This document has changed on disk.", "", "Reload", tab.reload)
        if tab is not None and tab.is_ready():
            tab.view.setFocus()

    def _tab_moved(self, source: int, target: int) -> None:
        self.tabs.insert(target, self.tabs.pop(source))

    def _step_tab(self, delta: int) -> None:
        if len(self.tabs) > 1:
            self.tab_bar.setCurrentIndex((self.tab_bar.currentIndex() + delta) % len(self.tabs))

    def close_current_tab(self) -> None:
        if self.tabs:
            self.close_tab(self.tab_bar.currentIndex())
        else:
            self.close()

    def close_tab(self, index: int) -> None:
        if not 0 <= index < len(self.tabs):
            return
        tab = self.tabs.pop(index)
        self._remember_view(tab)
        self.store.save()
        tab.shutdown()
        self.stack.removeWidget(tab)
        self.tab_bar.removeTab(index)
        tab.deleteLater()
        if not self.tabs:
            self.recent_model.refresh()
        self._tab_changed(self.tab_bar.currentIndex())

    def _remember_view(self, tab: DocumentTab) -> None:
        if tab.is_ready():
            self.store.set_view(tab.path, tab.view.view_state())

    def _remember_views(self) -> None:
        for tab in self.tabs:
            self._remember_view(tab)
        self.store.save()

    # --- command state ---
    def _sync(self) -> None:
        tab = self.ready_tab()
        ready = tab is not None
        for action in self._document_actions:
            action.setEnabled(ready)
        self.close_action.setEnabled(bool(self.tabs))
        self.page_box.setEnabled(ready)
        self.zoom_box.setEnabled(ready)
        self.view_button.setEnabled(ready)
        self.tab_row.setVisible(bool(self.tabs))
        has_recent = bool(self.store.recent)
        self.recent_title.setVisible(has_recent)
        self.recent_view.setVisible(has_recent)
        self.no_recent.setVisible(not has_recent)
        if not ready:
            self.page_box.setText("")
            self.page_total.setText("")
            self.zoom_box.setEditText("")
            self.copy_action.setEnabled(False)
            self.search_selection_action.setEnabled(False)
            self.back_action.setEnabled(False)
            self.forward_action.setEnabled(False)
            title = self.current_tab().title() if self.current_tab() else ""
            self.setWindowTitle(f"{title} – {DISPLAY_NAME}" if title else DISPLAY_NAME)
            return
        view = tab.view
        self.setWindowTitle(f"{tab.title()} – {DISPLAY_NAME}")
        self._sync_page()
        self._sync_zoom()
        self.previous_action.setEnabled(view.current_page() > 0)
        self.next_action.setEnabled(view.current_page() < view.page_count() - 1)
        self.zoom_in_action.setEnabled(view.zoom < lay.ZOOM_MAX)
        self.zoom_out_action.setEnabled(view.zoom > lay.ZOOM_MIN)
        self.copy_action.setEnabled(view.has_selection())
        self.search_selection_action.setEnabled(view.has_selection())
        self.back_action.setEnabled(view.history.can_back())
        self.forward_action.setEnabled(view.history.can_forward())
        for action, value in ((self.sidebar_action, not tab.sidebar.isHidden()),
                              (self.continuous_action, view.continuous),
                              (self.one_page_action, view.columns == 1),
                              (self.two_pages_action, view.columns == 2 and not view.cover),
                              (self.cover_action, view.columns == 2 and view.cover),
                              (self.select_tool_action, view.tool == SELECT),
                              (self.hand_tool_action, view.tool == HAND)):
            action.blockSignals(True)
            action.setChecked(bool(value))
            action.blockSignals(False)

    def _sync_page(self) -> None:
        tab = self.ready_tab()
        if tab is None or self.page_box.hasFocus() and self.page_box.isModified():
            return
        self.page_box.setText(tab.document.label(tab.view.current_page()))
        count = tab.view.page_count()
        plain = str(tab.view.current_page() + 1)
        self.page_total.setText(f"/ {count:,}" if tab.document.label(tab.view.current_page()) == plain
                                else f"({plain} / {count:,})")

    def _sync_zoom(self) -> None:
        tab = self.ready_tab()
        if tab is None or self.zoom_box.lineEdit().hasFocus() and self.zoom_box.lineEdit().isModified():
            return
        self.zoom_box.setEditText(lay.zoom_text(tab.view.zoom))

    def _status(self, text: str) -> None:
        self.status_label.setText(text[:300])

    # --- view commands ---
    def _view_call(self, name: str) -> None:
        tab = self.ready_tab()
        if tab is not None:
            getattr(tab.view, name)()
            self._sync()

    def _tab_call(self, name: str) -> None:
        tab = self.ready_tab()
        if tab is not None:
            getattr(tab, name)()

    def _set_zoom_mode(self, mode: str) -> None:
        tab = self.ready_tab()
        if tab is not None:
            tab.view.set_zoom(0, mode)

    def _set_zoom(self, zoom: float) -> None:
        tab = self.ready_tab()
        if tab is not None:
            tab.view.set_zoom(zoom)

    def _zoom_chosen(self, index: int) -> None:
        value = _ZOOM_CHOICES[index][1]
        if isinstance(value, str):
            self._set_zoom_mode(value)
        else:
            self._set_zoom(value)
        self._focus_view()

    def _zoom_entered(self) -> None:
        zoom = lay.parse_zoom(self.zoom_box.currentText())
        self.zoom_box.lineEdit().setModified(False)
        if zoom is not None:
            self._set_zoom(zoom)
        self._sync_zoom()
        self._focus_view()

    def _page_entered(self) -> None:
        tab = self.ready_tab()
        if tab is None:
            return
        page = tab.document.page_for_label(self.page_box.text())
        self.page_box.setModified(False)
        if page >= 0:
            tab.view.go_to_page(page, record=True)
            self._focus_view()
        else:
            self._status(f"There is no page “{self.page_box.text().strip()[:40]}” in this document.")
        self._sync()

    def focus_page_box(self) -> None:
        if self.ready_tab() is not None:
            self.page_box.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.page_box.selectAll()

    def _focus_view(self) -> None:
        tab = self.ready_tab()
        if tab is not None:
            tab.view.setFocus()

    def _rotate(self, quarter_turns: int) -> None:
        tab = self.ready_tab()
        if tab is not None:
            tab.view.rotate(quarter_turns)

    def _arrangement_changed(self) -> None:
        tab = self.ready_tab()
        if tab is None:
            return
        columns = 1 if self.one_page_action.isChecked() else 2
        tab.view.set_arrangement(self.continuous_action.isChecked(), columns, self.cover_action.isChecked())
        self._sync()

    def _tool_changed(self) -> None:
        tab = self.ready_tab()
        if tab is not None:
            tab.view.set_tool(HAND if self.hand_tool_action.isChecked() else SELECT)

    def _toggle_sidebar(self) -> None:
        visible = self.sidebar_action.isChecked()
        self.store.sidebar = visible
        for tab in self.tabs:
            tab.sidebar.setVisible(visible)
        self.store.save()

    def _sidebar_tab_changed(self, key: str) -> None:
        self.store.sidebar_tab = key

    def find(self) -> None:
        tab = self.ready_tab()
        if tab is not None:
            tab.open_find()

    def copy(self) -> None:
        tab = self.ready_tab()
        if tab is not None and tab.view.copy_selection():
            self._status("Copied.")

    def search_selection(self) -> None:
        tab = self.ready_tab()
        if tab is not None:
            text = " ".join(tab.view.selected_text().split())[:200]
            if text:
                tab.search_for(text)

    def _escape(self) -> None:
        tab = self.current_tab()
        if self.isFullScreen():
            self.toggle_fullscreen()
        elif tab is not None and tab.find_bar.isVisible():
            tab.close_find()
        elif tab is not None and tab.is_ready():
            tab.view.clear_selection()

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _context_menu(self, position: QPoint) -> None:
        self.build_context_menu().popup(position)

    def build_context_menu(self) -> QMenu:
        menu = fluent.MenuFlyout(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        for item in (self.copy_action, self.select_all_action, self.search_selection_action, None,
                     self.copy_page_action, None, self.rotate_left_action, self.rotate_right_action, None,
                     self.properties_action):
            menu.addSeparator() if item is None else menu.addAction(item)
        return menu

    # --- links ---
    def _web_link(self, url: str) -> None:
        """A web link was clicked: show where it leads and open it only when the user says so."""
        shown = url if len(url) <= 300 else url[:300] + "…"
        if url.lower().startswith(("http://", "https://", "mailto:")):
            self.notice("info", shown, "Open this link?", "Open link", lambda: self._open_link(url))
        else:
            self.notice("warning", shown, "This kind of link is not opened")

    def _open_link(self, url: str) -> None:
        if not self.open_url(url):
            self.notice("warning", "No application could be started for this link.")

    # --- file on disk ---
    def _changed_on_disk(self, tab: DocumentTab) -> None:
        if tab is self.current_tab():
            self.notice("info", "This document has changed on disk.", "", "Reload", tab.reload)
        else:
            tab._reload_offer = True

    # --- saving, copying ---
    def save_copy(self) -> None:
        tab = self.ready_tab()
        if tab is None:
            return
        target = self.choose_save(os.path.join(os.path.dirname(tab.path), "Copy of " + tab.title()))
        if not target:
            return
        target = os.path.abspath(target)
        source = tab.path
        if os.path.exists(target) and os.path.samefile(source, target):
            self.notice("warning", "That is the document itself. Choose another name for the copy.")
            return

        def work() -> str:
            descriptor, temporary = tempfile.mkstemp(prefix=".copy-", dir=os.path.dirname(target))
            try:
                with os.fdopen(descriptor, "wb") as out, open(source, "rb") as original:
                    shutil.copyfileobj(original, out, 1 << 20)
                    out.flush()
                    os.fsync(out.fileno())
                os.chmod(temporary, 0o644)
                os.replace(temporary, target)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
            return target

        self.worker.submit(work, on_done=lambda path: self.notice("success", f"Saved a copy as {path}."),
                           on_error=lambda error: self.notice(
                               "error", f"The copy could not be saved: {getattr(error, 'strerror', None) or error}."))

    def copy_page_image(self) -> None:
        tab = self.ready_tab()
        if tab is None or tab.renderer is None:
            return
        page = tab.view.current_page()
        width, height = lay.rotated(*tab.document.sizes[page], tab.view.rotation)
        self._copy_request = Request("copy", page, round(width * _COPY_DPI / 72), round(height * _COPY_DPI / 72),
                                     tab.view.rotation)
        self._connect_renderer(tab)
        tab.renderer.request(self._copy_request, -3)

    def _connect_renderer(self, tab: DocumentTab) -> None:
        if not getattr(tab, "_window_connected", None) is tab.renderer:
            tab._window_connected = tab.renderer
            tab.renderer.rendered.connect(self._rendered)

    def _rendered(self, request: Request, image: QImage) -> None:
        if request.kind == "copy" and request == self._copy_request:
            self._copy_request = None
            if image.isNull():
                self.notice("error", "The page could not be rendered.")
            else:
                QGuiApplication.clipboard().setImage(image)
                self._status(f"Page {request.page + 1} copied as a picture.")
        elif request.kind == "recent":
            path = self._recent_requests.pop(request, None)
            if path and not image.isNull() and self._thumb_dir is not None:
                image.save(self.thumbnail_path(path), "PNG")
                self.recent_model.refresh()

    # --- recent thumbnails ---
    def thumbnail_path(self, path: str) -> str:
        name = hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()[:32] + ".png"
        return os.path.join(self._thumb_dir or "/nonexistent", name)

    def _make_thumbnail(self, tab: DocumentTab) -> None:
        if self._thumb_dir is None or tab.renderer is None:
            return
        width, height = tab.document.sizes[0]
        scale = min(_RECENT_THUMB.width() * 2 / width, _RECENT_THUMB.height() * 2 / height)
        request = Request("recent", 0, max(1, round(width * scale)), max(1, round(height * scale)))
        self._recent_requests[request] = tab.path
        self._connect_renderer(tab)
        tab.renderer.request(request, 200)

    # --- properties ---
    def show_properties(self) -> None:
        tab = self.ready_tab()
        if tab is None:
            return
        dialog = fluent.ContentDialog(self, "Document properties", primary=None, close="Close", default="close")
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        dialog.values = {}
        for row, (name, value) in enumerate(tab.document.properties()):
            dialog.values[name] = value
            label = fluent.Label(name, "body", tone="secondary")
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            text = fluent.Label(value, "body", wrap=True, selectable=True)
            text.setAccessibleName(name)
            grid.addWidget(label, row, 0)
            grid.addWidget(text, row, 1)
        grid.setColumnStretch(1, 1)
        dialog.content_layout.addLayout(grid)
        self._show_dialog(dialog)

    # --- printing ---
    def print_document(self) -> None:
        tab = self.ready_tab()
        if tab is None or not printing.AVAILABLE or self._print_job is not None:
            return
        printer = printing.new_printer()
        printer.setDocName(tab.title())
        if not self.run_print_dialog(printer, self, tab.view.page_count(), tab.view.current_page()):
            return
        pages = printing.chosen_pages(printer, tab.view.page_count(), tab.view.current_page())
        self.start_print(tab, printer, pages)

    def start_print(self, tab: DocumentTab, printer, pages: list[int]) -> None:
        job = printing.PrintJob(tab.document.sizes, tab.renderer, printer, pages, self)
        self._print_job = job
        self._printer = printer
        dialog = fluent.ContentDialog(self, "Printing", f"“{tab.title()}”", primary=None, close="Cancel",
                                      default="close")
        dialog.bar = QProgressBar()
        dialog.bar.setTextVisible(False)
        dialog.bar.setRange(0, max(1, len(pages)))
        dialog.detail = fluent.Label("Preparing…", "caption", tone="secondary")
        dialog.content_layout.addWidget(dialog.bar)
        dialog.content_layout.addWidget(dialog.detail)

        def progress(done: int, total: int) -> None:
            dialog.bar.setRange(0, total)
            dialog.bar.setValue(done)
            dialog.detail.setText(f"Page {min(done + 1, total):,} of {total:,}")

        def finished(ok: bool, message: str) -> None:
            self._print_job = None
            self._printer = None
            if dialog.is_open():
                dialog.done(fluent.ContentDialog.Result.PRIMARY)
            job.deleteLater()
            if ok:
                self.notice("success", f"{len(pages):,} page{'s' if len(pages) != 1 else ''} sent to the printer.")
            elif message:
                self.notice("error", message, "Printing failed")
            else:
                self.notice("info", "Printing was cancelled.")

        job.progress.connect(progress)
        job.finished.connect(finished)
        self._show_dialog(dialog, lambda result: job.cancel() if result == fluent.ContentDialog.Result.NONE else None)
        job.start()

    # --- presentation ---
    def start_presentation(self) -> None:
        tab = self.ready_tab()
        if tab is None or self.presentation is not None:
            return
        self.presentation = Presentation(tab.document, tab.renderer, tab.view.current_page(), tab.view.rotation)
        self.presentation.closed.connect(lambda page, tab=tab: self._presentation_closed(tab, page))
        self.presentation.start()

    def _presentation_closed(self, tab: DocumentTab, page: int) -> None:
        presentation, self.presentation = self.presentation, None
        if presentation is not None:
            presentation.deleteLater()
        if tab in self.tabs and tab.is_ready():
            tab.view.go_to_page(page)
            tab.view.setFocus()

    # --- drag and drop ---
    @staticmethod
    def _dropped(event) -> list[str]:
        data = event.mimeData()
        if not data.hasUrls():
            return []
        return [url.toLocalFile() for url in data.urls()[:_MAX_TABS] if url.isLocalFile() and url.toLocalFile()]

    def dragEnterEvent(self, event) -> None:
        if self._dropped(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = self._dropped(event)
        if paths:
            event.acceptProposedAction()
            for path in paths:
                self.open_document(path)

    # --- the bar on narrow windows ---
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_bar()

    def _fit_bar(self) -> None:
        """Hide the less important groups of the bar when it is too narrow; every command stays in "More"."""
        for _priority, widgets in self._groups:
            for widget in widgets:
                widget.setVisible(True)
        available = self.width() - 16
        for _priority, widgets in sorted(self._groups, key=lambda group: group[0]):
            if self.bar.layout().minimumSize().width() <= available:
                break
            for widget in widgets:
                widget.setVisible(False)
            self.bar.layout().invalidate()

    # --- closing ---
    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow() and not self.tabs:
            self.recent_model.refresh()                 # another window may have opened a document since
            self._sync()

    def closeEvent(self, event) -> None:
        if self._print_job is not None:
            self._print_job.cancel()
        if self.presentation is not None:
            self.presentation.close()
        self._save_timer.stop()
        self._remember_views()
        for tab in self.tabs:
            tab.shutdown()
        self.worker.shutdown()
        super().closeEvent(event)
