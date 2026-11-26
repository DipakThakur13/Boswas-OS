"""Boswas Software Center: installed applications, and installing Linux software.

Installed: three groups in one searchable list, Boswas applications (desktop
entries com.boswas.*), Windows applications (the session agent's apps.list)
and the other applications (XDG desktop entries shown on the Boswas desktop).
Native and Boswas applications open with `gio launch <desktop entry>`; Windows
applications are managed in the Compatibility Manager and never started from
here. The other tabs (software_tabs.py) install Debian packages, Flatpak
applications and AppImages, and route a chosen file to the right installer.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QFontMetrics, QIcon, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QStyle, QStyledItemDelegate, QTabWidget, QVBoxLayout, QWidget)

from .. import viewmodel as vm
from ..catalog import COMPAT_MANAGER
from .base import Page
from .common import SP, Badge, Card, MessageBanner, icon, muted_label, plain_label, title_label
from .jobs import JobPanel
from .software_tabs import AppImageTab, FileTab, FlatpakTab, LinuxTab

KIND, PAYLOAD = Qt.ItemDataRole.UserRole, Qt.ItemDataRole.UserRole + 1
HEADER, APP, NOTE = "header", "app", "note"
GROUP_BADGES = {vm.BOSWAS: "Boswas application", vm.WINDOWS: "Windows application", vm.NATIVE: "Application"}
_icon_cache: dict[str, QIcon] = {}


def app_icon(item: vm.AppItem) -> QIcon:
    cached = _icon_cache.get(item.key)
    if cached is not None:
        return cached
    result = QIcon()
    if item.icon.startswith("/"):
        result = QIcon(item.icon)
    if result.isNull():
        # The toolkit looks in Boswas Prism, then in hicolor (where a program
        # installs its own icon), then in Qt's theme: QIcon.fromTheme would
        # search hicolor last and show Breeze's drawing of other programs.
        themed = (item.icon,) if item.icon and not item.icon.startswith("/") else ()
        names = ("application-x-ms-dos-executable",) if item.group == vm.WINDOWS else ()
        result = icon(*themed, *names, "application-x-executable", fallback=SP.SP_FileIcon)
    _icon_cache[item.key] = result
    return result


class AppDelegate(QStyledItemDelegate):
    """Paints the installed applications as rows of glass cards, their icons magnified as in a dock.

    Every colour comes from the theme (``fluent.qcolor``), never from the
    view's palette: the application style sheet makes the palette of a list
    view transparent (``background``, ``selection-background-color``), and
    colours mixed from those roles came out black, the black hover this
    replaces. Hover and press are the grey state layers of the scheme on the
    card (``fluent.paint.glass``); a selected row has the accent pill. The
    keyboard focus is the toolkit's focus ring.
    """

    APP_H, HEADER_H, NOTE_H, ICON = 60, 40, 34, 28
    GAP = 2                       # above and below each card: rows stand 4 px apart

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self._view = view
        self.magnifier = fluent.ViewMagnifier(view, accept=lambda index: index.data(KIND) == APP)
        self._pressed = -1
        view.viewport().installEventFilter(self)

    def cell(self) -> int:
        """The room the icon of a row has, so that it can grow without moving the text."""
        return round(self.ICON * self.magnifier.grow)

    def sizeHint(self, option, index) -> QSize:
        kind = index.data(KIND)
        height = {HEADER: self.HEADER_H, NOTE: self.NOTE_H}.get(kind, self.APP_H)
        return QSize(option.rect.width(), height)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            index = self._view.indexAt(event.position().toPoint())
            self._set_pressed(index.row() if index.isValid() and index.data(KIND) == APP else -1)
        elif kind in (QEvent.Type.MouseButtonRelease, QEvent.Type.Leave):
            self._set_pressed(-1)
        return False

    def _set_pressed(self, row: int) -> None:
        if row != self._pressed:
            for old in (self._pressed, row):
                if old >= 0:
                    self._view.viewport().update(self._view.visualRect(self._view.model().index(old, 0)))
            self._pressed = row

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(4, self.GAP, -4, -self.GAP)
        kind = index.data(KIND)
        if kind == HEADER:
            title, count, note = index.data(PAYLOAD)
            title_font = fluent.font("body-strong")
            title_width = QFontMetrics(title_font).horizontalAdvance(title)
            text_rect = rect.adjusted(4, 8, 0, 0)
            fluent.paint.elided(painter, text_rect, title, title_font, "text")
            extra = f"{count}" + (f"  ·  {note}" if note else "")
            fluent.paint.elided(painter, text_rect.adjusted(title_width + 10, 0, 0, 0), extra, fluent.font("body"),
                                "text-secondary")
            painter.restore()
            return
        if kind == NOTE:
            fluent.paint.elided(painter, rect.adjusted(self.cell() + 24, 0, 0, 0), str(index.data(PAYLOAD)),
                                fluent.font("body"), "text-secondary")
            painter.restore()
            return
        item: vm.AppItem = index.data(PAYLOAD)
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        state = "rest"
        if enabled and index.row() == self._pressed:
            state = "pressed"
        elif enabled and (hovered or selected):
            state = "pressed" if hovered and selected else "hover"
        fluent.paint.glass(painter, rect, "card", state=state)
        if selected:
            pill = QRectF(rect.left() + 1, rect.center().y() - 8, 3, 16)
            fluent.paint.rounded_rect(painter, pill, 1.5, "accent" if enabled else "text-disabled")
        cell = self.cell()
        scale = self.magnifier.scale(index.row())
        side = self.ICON * scale
        cell_left = rect.left() + 12
        target = QRectF(cell_left + (cell - side) / 2, rect.center().y() - side / 2, side, side)
        ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        moving = self.magnifier.animating(index.row())
        size = cell if moving else max(1, round(side))
        pixmap = fluent.icons.cached_pixmap(app_icon(item), size, ratio, enabled)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, moving)
        if not moving:
            target = QRectF(round(target.x() * ratio) / ratio, round(target.y() * ratio) / ratio, size, size)
        painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
        text_x = cell_left + cell + 12
        width = rect.right() - text_x - 12
        title_font, body_font = fluent.font("body-strong"), fluent.font("caption")
        title_height = QFontMetrics(title_font).height()
        body_height = QFontMetrics(body_font).height()
        top = rect.top() + (rect.height() - title_height - body_height - 2) / 2
        fluent.paint.elided(painter, QRectF(text_x, top, width, title_height), item.name, title_font,
                            "text" if enabled else "text-disabled")
        fluent.paint.elided(painter, QRectF(text_x, top + title_height + 2, width, body_height),
                            item.subtitle or GROUP_BADGES[item.group], body_font,
                            "text-secondary" if enabled else "text-disabled")
        painter.restore()


class AppList(QListWidget):
    """The application list; arrow keys skip the group headers and notes."""

    def moveCursor(self, action, modifiers):
        index = super().moveCursor(action, modifiers)
        step = -1 if action in (QAbstractItemView.CursorAction.MoveUp, QAbstractItemView.CursorAction.MovePageUp,
                                QAbstractItemView.CursorAction.MoveHome) else 1
        row = index.row()
        while 0 <= row < self.count() and self.item(row).data(KIND) != APP:
            row += step
        if not 0 <= row < self.count():
            return self.currentIndex()
        return self.model().index(row, 0)


class DetailPanel(Card):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent=parent, margins=18, spacing=10)
        self.setFixedWidth(300)
        self.icon = QLabel(self)
        self.icon.setFixedSize(64, 64)
        self.name = title_label("", 1.3, self)
        self.badge = Badge("Application", parent=self)
        self.subtitle = muted_label("", parent=self)
        self.description = plain_label("", wrap=True, selectable=True, parent=self)
        self.open_button = QPushButton("Open", self)
        self.open_button.setIcon(icon("system-run", "media-playback-start", fallback=SP.SP_MediaPlay))
        self.manage_button = QPushButton("Manage in Compatibility Manager", self)
        self.manage_button.setIcon(icon("boswas-compat-manager", "configure", fallback=SP.SP_FileDialogDetailedView))
        self.hint = muted_label("", parent=self)
        self.empty = muted_label("Select an application to see its details.", parent=self)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for widget in (self.icon, self.name, self.badge, self.subtitle, self.description, self.open_button,
                       self.manage_button, self.hint):
            self.addWidget(widget)
        self.layout_.addWidget(self.empty, 1)
        self.layout_.addStretch(1)
        self.item: vm.AppItem | None = None
        self.show_item(None)

    def show_item(self, item: vm.AppItem | None) -> None:
        self.item = item
        widgets = (self.icon, self.name, self.badge, self.subtitle, self.description, self.open_button,
                   self.manage_button, self.hint)
        for widget in widgets:
            widget.setVisible(item is not None)
        self.empty.setVisible(item is None)
        if item is None:
            return
        self.icon.setPixmap(app_icon(item).pixmap(QSize(64, 64)))
        self.name.setText(item.name)
        self.badge.set_text(GROUP_BADGES[item.group])
        self.subtitle.setText(item.subtitle if item.subtitle != item.description else "")
        self.subtitle.setVisible(bool(self.subtitle.text()))
        self.description.setText(item.description)
        self.description.setVisible(bool(item.description))
        windows = item.group == vm.WINDOWS
        self.open_button.setVisible(not windows)
        self.open_button.setAccessibleName(f"Open {item.name}")
        self.manage_button.setVisible(windows)
        self.hint.setText("Windows applications start in their sandbox from the application menu or the "
                          "Compatibility Manager." if windows else "")
        self.hint.setVisible(windows)


class ApplicationsPage(Page):
    data_keys = ("desktop_apps", "windows_apps", "flatpak", "appimages")
    TABS = ("installed", "linux", "flatpak", "appimage", "file")

    def build(self) -> None:
        self.message = MessageBanner(parent=self.content)
        self.body.addWidget(self.message)
        self.panel = JobPanel(self.info.key, self.window_.jobs, self.content)
        self.body.addWidget(self.panel)
        self.tabs = QTabWidget(self.content)
        self.tabs.setAccessibleName("Software Center sections")
        installed = QWidget(self.tabs)
        installed_layout = QVBoxLayout(installed)
        installed_layout.setContentsMargins(4, 10, 4, 4)
        installed_layout.setSpacing(12)
        search_row = QHBoxLayout()
        self.search = QLineEdit(installed)
        self.search.setPlaceholderText("Search applications (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search applications")
        self.search.addAction(icon("system-search", "search", "edit-find", fallback=SP.SP_FileDialogContentsView),
                              QLineEdit.ActionPosition.LeadingPosition)
        self.count = muted_label("", wrap=False, parent=installed)
        self.updates_button = QPushButton("Check for Updates", installed)
        self.updates_button.setIcon(icon("system-software-update", fallback=SP.SP_BrowserReload))
        self.updates_button.setAccessibleDescription("Opens the Boswas Update Center")
        self.updates_button.clicked.connect(lambda: self.window_.show_page("updates"))
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.count)
        search_row.addWidget(self.updates_button)
        installed_layout.addLayout(search_row)
        row = QHBoxLayout()
        row.setSpacing(14)
        # The rows are cards of their own (AppDelegate): the list lies on the page, not on another card.
        list_card = QWidget(installed)
        list_layout = QVBoxLayout(list_card)
        list_layout.setContentsMargins(0, 0, 0, 0)
        self.list = AppList(list_card)
        self.delegate = AppDelegate(self.list)
        self.list.setItemDelegate(self.delegate)
        self.list.setFrameShape(QListWidget.Shape.NoFrame)
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(False)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setWordWrap(False)
        self.list.setAccessibleName("Installed applications")
        self.list.setMinimumHeight(380)
        list_layout.addWidget(self.list)
        self.detail = DetailPanel(installed)
        row.addWidget(list_card, 1)
        row.addWidget(self.detail, 0, Qt.AlignmentFlag.AlignTop)
        installed_layout.addLayout(row)
        note = MessageBanner(closable=False, parent=installed)
        note.show_message("info", vm.STORE_NOTE)
        self.store_note = note
        installed_layout.addWidget(note)
        self.linux_tab = LinuxTab(self, self.tabs)
        self.flatpak_tab = FlatpakTab(self, self.tabs)
        self.appimage_tab = AppImageTab(self, self.tabs)
        self.file_tab = FileTab(self, self.tabs)
        for widget, title in ((installed, "Installed"), (self.linux_tab, "Linux Packages"),
                              (self.flatpak_tab, "Flatpak"), (self.appimage_tab, "AppImages"),
                              (self.file_tab, "Install from File")):
            self.tabs.addTab(widget, title)
        self.install_tabs = (self.linux_tab, self.flatpak_tab, self.appimage_tab, self.file_tab)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.body.addWidget(self.tabs)
        self.add_settings()
        self.window_.jobs.changed.connect(self._update_tab_buttons)
        self.items: dict[str, vm.AppItem] = {}
        self.groups: dict[str, list[vm.AppItem]] = {}
        self.search.textChanged.connect(self.populate)
        self.list.currentItemChanged.connect(self._selected)
        self.list.itemActivated.connect(self._activated)
        self.detail.open_button.clicked.connect(self._open_current)
        self.detail.manage_button.clicked.connect(lambda: self.window_.open_program(COMPAT_MANAGER))
        self.setTabOrder(self.search, self.list)

    def render(self) -> None:
        apps = self.store.get("desktop_apps")
        windows = self.store.get("windows_apps")
        self.groups = vm.application_groups(apps.value if apps is not None and apps.ok else [],
                                            windows.value if windows is not None and windows.ok else [])
        self.populate()
        self.flatpak_tab.refresh()
        self.appimage_tab.refresh()
        self._update_tab_buttons()

    def show_tab(self, key: str) -> None:
        self.tabs.setCurrentIndex(self.TABS.index(key))

    def _tab_changed(self, index: int) -> None:
        if 0 < index <= len(self.install_tabs):
            self.install_tabs[index - 1].refresh()

    def _update_tab_buttons(self) -> None:
        for tab in self.install_tabs:
            tab.update_buttons()

    def _windows_note(self, items: list) -> str:
        windows = self.store.get("windows_apps")
        if windows is None:
            return "Loading…"
        if not windows.ok:
            return "Not available (the Boswas session service is not running)" if windows.unavailable \
                else "Could not be listed"
        return "" if items else "None installed"

    def populate(self) -> None:
        text = self.search.text()
        current = self.list.currentItem()
        current_key = current.data(PAYLOAD).key if current is not None and current.data(KIND) == APP else None
        self.list.blockSignals(True)
        self.list.clear()
        self.items = {}
        total = 0
        apps_result = self.store.get("desktop_apps")
        for group in vm.GROUP_ORDER:
            items = vm.filter_apps(self.groups.get(group, []), text)
            note = self._windows_note(self.groups.get(group, [])) if group == vm.WINDOWS else ""
            if group == vm.NATIVE and apps_result is None:
                note = "Loading…"
            if not items and (text or (group == vm.BOSWAS)):
                continue
            header = QListWidgetItem()
            header.setData(KIND, HEADER)
            header.setData(PAYLOAD, (vm.GROUP_TITLES[group], len(items), note))
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(header)
            for item in items:
                entry = QListWidgetItem()
                entry.setData(KIND, APP)
                entry.setData(PAYLOAD, item)
                entry.setData(Qt.ItemDataRole.AccessibleTextRole, item.name)
                entry.setData(Qt.ItemDataRole.AccessibleDescriptionRole, item.subtitle or GROUP_BADGES[item.group])
                entry.setToolTip(item.description or item.name)
                self.list.addItem(entry)
                self.items[item.key] = item
                total += 1
                if item.key == current_key:
                    self.list.setCurrentItem(entry)
            if not items and group == vm.WINDOWS and not note:
                empty = QListWidgetItem()
                empty.setData(KIND, NOTE)
                empty.setData(PAYLOAD, "Install Windows applications with the Compatibility Manager.")
                empty.setFlags(Qt.ItemFlag.NoItemFlags)
                self.list.addItem(empty)
        if text and not total:
            empty = QListWidgetItem()
            empty.setData(KIND, NOTE)
            empty.setData(PAYLOAD, f"No installed application matches “{vm.one_line(text, 60)}”.")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(empty)
        self.list.blockSignals(False)
        self.count.setText(f"{total} application{'s' if total != 1 else ''}")
        selected = self.list.currentItem()
        self.detail.show_item(selected.data(PAYLOAD) if selected is not None and selected.data(KIND) == APP else None)

    def visible_keys(self) -> list[str]:
        return [self.list.item(i).data(PAYLOAD).key for i in range(self.list.count())
                if self.list.item(i).data(KIND) == APP]

    def select(self, key: str) -> bool:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(KIND) == APP and item.data(PAYLOAD).key == key:
                self.list.setCurrentItem(item)
                return True
        return False

    def focus_search(self) -> None:
        self.tabs.setCurrentIndex(0)
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def _selected(self, current, _previous) -> None:
        self.detail.show_item(current.data(PAYLOAD) if current is not None and current.data(KIND) == APP else None)

    def _activated(self, entry: QListWidgetItem) -> None:
        if entry.data(KIND) != APP:
            return
        item: vm.AppItem = entry.data(PAYLOAD)
        if item.group == vm.WINDOWS:
            self.window_.open_program(COMPAT_MANAGER)
        else:
            self.launch(item)

    def _open_current(self) -> None:
        if self.detail.item is not None and self.detail.item.launchable:
            self.launch(self.detail.item)

    def launch(self, item: vm.AppItem) -> None:
        self.message.show_message("info", f"Opening {item.name}…")
        self.window_.runner.submit(self.window_.backend.launch, item,
                                   on_done=lambda _r: self.message.hide(),
                                   on_error=lambda exc: self.message.show_message(
                                       "error", f"{item.name} could not be opened. {self.window_.error_text(exc)}"))
