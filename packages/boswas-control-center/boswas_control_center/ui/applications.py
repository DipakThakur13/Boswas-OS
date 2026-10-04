"""Boswas Software Center: the installed applications in one searchable list.

Three groups: Boswas applications (desktop entries com.boswas.*), Windows
applications (the session agent's apps.list) and the other applications
(XDG desktop entries visible in KDE). Native and Boswas applications open
with `kstart --application <desktop id>`; Windows applications are managed
in the Compatibility Manager and never started from here. Installing new
software is not possible here: it arrives with the Boswas Store.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QIcon, QPainter, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QStyle, QStyledItemDelegate, QWidget)

from .. import viewmodel as vm
from ..catalog import COMPAT_MANAGER
from .base import Page
from .common import SP, Badge, Card, MessageBanner, icon, mix, muted_label, plain_label, scaled_font, title_label

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
    elif item.icon:
        result = QIcon.fromTheme(item.icon)
    if result.isNull():
        names = ("application-x-ms-dos-executable",) if item.group == vm.WINDOWS else ()
        result = icon(*names, "application-x-executable", fallback=SP.SP_FileIcon)
    _icon_cache[item.key] = result
    return result


class AppDelegate(QStyledItemDelegate):
    APP_H, HEADER_H, NOTE_H, ICON = 52, 40, 34, 32

    def sizeHint(self, option, index) -> QSize:
        kind = index.data(KIND)
        height = {HEADER: self.HEADER_H, NOTE: self.NOTE_H}.get(kind, self.APP_H)
        return QSize(option.rect.width(), height)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = option.palette
        rect = QRectF(option.rect).adjusted(4, 2, -4, -2)
        kind = index.data(KIND)
        base_font = option.font
        if kind == HEADER:
            title, count, note = index.data(PAYLOAD)
            font = scaled_font(base_font, 1.05, weight=QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(palette.color(QPalette.ColorRole.Text))
            fm = QFontMetrics(font)
            text = f"{title}"
            left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            painter.drawText(rect.adjusted(8, 8, 0, 0), left, text)
            painter.setFont(base_font)
            painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
            extra = f"{count}" + (f"  ·  {note}" if note else "")
            painter.drawText(rect.adjusted(8 + fm.horizontalAdvance(text) + 10, 8, 0, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(base_font).elidedText(extra, Qt.TextElideMode.ElideRight,
                                                                int(rect.width() - fm.horizontalAdvance(text) - 30)))
            painter.restore()
            return
        if kind == NOTE:
            painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(rect.adjusted(52, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             str(index.data(PAYLOAD)))
            painter.restore()
            return
        item: vm.AppItem = index.data(PAYLOAD)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        base, accent = palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Highlight)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(rect, 6, 6)
        elif hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(mix(base, accent, 0.12))
            painter.drawRoundedRect(rect, 6, 6)
        if focused and not selected:
            painter.setPen(accent)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        x = int(rect.left()) + 10
        y = int(rect.top() + (rect.height() - self.ICON) / 2)
        app_icon(item).paint(painter, QRect(x, y, self.ICON, self.ICON))
        text_x = x + self.ICON + 12
        width = int(rect.right() - text_x - 10)
        title_font = scaled_font(base_font, weight=QFont.Weight.DemiBold)
        fm_title, fm = QFontMetrics(title_font), QFontMetrics(base_font)
        top = rect.top() + (rect.height() - fm_title.height() - fm.height() - 2) / 2
        text_colour = palette.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        painter.setFont(title_font)
        painter.setPen(text_colour)
        painter.drawText(QRectF(text_x, top, width, fm_title.height()), Qt.AlignmentFlag.AlignLeft,
                         fm_title.elidedText(item.name, Qt.TextElideMode.ElideRight, width))
        painter.setFont(base_font)
        painter.setPen(text_colour if selected else palette.color(QPalette.ColorRole.PlaceholderText))
        painter.drawText(QRectF(text_x, top + fm_title.height() + 2, width, fm.height()), Qt.AlignmentFlag.AlignLeft,
                         fm.elidedText(item.subtitle or GROUP_BADGES[item.group], Qt.TextElideMode.ElideRight, width))
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
    data_keys = ("desktop_apps", "windows_apps")

    def build(self) -> None:
        self.message = MessageBanner(parent=self.content)
        self.body.addWidget(self.message)
        search_row = QHBoxLayout()
        self.search = QLineEdit(self.content)
        self.search.setPlaceholderText("Search applications (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search applications")
        self.search.addAction(icon("search", "edit-find", fallback=SP.SP_FileDialogContentsView),
                              QLineEdit.ActionPosition.LeadingPosition)
        self.count = muted_label("", wrap=False, parent=self.content)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.count)
        self.body.addLayout(search_row)
        row = QHBoxLayout()
        row.setSpacing(14)
        list_card = Card(parent=self.content, margins=6, spacing=0)
        self.list = AppList(list_card)
        self.list.setItemDelegate(AppDelegate(self.list))
        self.list.setFrameShape(QListWidget.Shape.NoFrame)
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(False)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setWordWrap(False)
        self.list.setAccessibleName("Installed applications")
        self.list.setMinimumHeight(380)
        list_card.addWidget(self.list)
        self.detail = DetailPanel(self.content)
        row.addWidget(list_card, 1)
        row.addWidget(self.detail, 0, Qt.AlignmentFlag.AlignTop)
        self.body.addLayout(row)
        note = MessageBanner(closable=False, parent=self.content)
        note.show_message("info", vm.STORE_NOTE)
        self.store_note = note
        self.body.addWidget(note)
        self.add_modules()
        self.items: dict[str, vm.AppItem] = {}
        self.groups: dict[str, list[vm.AppItem]] = {}
        self.search.textChanged.connect(self.populate)
        self.list.currentItemChanged.connect(self._selected)
        self.list.itemActivated.connect(self._activated)
        self.detail.open_button.clicked.connect(self._open_current)
        self.detail.manage_button.clicked.connect(lambda: self.window_.open_module(COMPAT_MANAGER))
        self.setTabOrder(self.search, self.list)

    def render(self) -> None:
        apps = self.store.get("desktop_apps")
        windows = self.store.get("windows_apps")
        self.groups = vm.application_groups(apps.value if apps is not None and apps.ok else [],
                                            windows.value if windows is not None and windows.ok else [])
        self.populate()

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
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def _selected(self, current, _previous) -> None:
        self.detail.show_item(current.data(PAYLOAD) if current is not None and current.data(KIND) == APP else None)

    def _activated(self, entry: QListWidgetItem) -> None:
        if entry.data(KIND) != APP:
            return
        item: vm.AppItem = entry.data(PAYLOAD)
        if item.group == vm.WINDOWS:
            self.window_.open_module(COMPAT_MANAGER)
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
