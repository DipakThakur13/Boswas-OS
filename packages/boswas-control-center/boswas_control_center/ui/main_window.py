"""Main window: header strip, category sidebar and lazily built pages.

Pages are created the first time they are shown, and each loads only the
data it displays (store.py). The Install page exists only in a live
session: its sidebar entry stays hidden until the live check has answered
yes. Keyboard: Up/Down in the sidebar switch pages, Tab moves into the page,
F5 refreshes the current page, Ctrl+F searches the applications, Ctrl+Q
quits.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QKeySequence, QPainter, QPalette, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QStackedWidget, QStyle, QStyledItemDelegate, QToolButton,
                               QVBoxLayout, QWidget)

from .. import catalog
from ..errors import user_message
from .applications import ApplicationsPage
from .common import (SP, Badge, MessageBanner, Separator, app_icon, border_colour, icon, mark_icon, message_box, mix,
                     plain_label, scaled_font)
from .device_pages import PowerPage, SettingsPage, StoragePage
from .personalization import PersonalizationPage
from .security import SecurityPage
from .status_pages import PrivacyPage, UpdatesPage, WindowsPage
from .store import Store
from .system_pages import AboutPage, InstallPage, SystemPage
from .worker import Runner

APP_NAME = "Boswas Control Center"
DEFAULT_PAGE = "personalization"
PAGE_CLASSES = {
    "personalization": PersonalizationPage, "power": PowerPage, "storage": StoragePage,
    "applications": ApplicationsPage, "windows": WindowsPage, "security": SecurityPage, "updates": UpdatesPage,
    "privacy": PrivacyPage, "system": SystemPage, "about": AboutPage, "install": InstallPage,
}
SIDEBAR_FALLBACKS = {"about": SP.SP_MessageBoxInformation, "storage": SP.SP_DriveHDIcon,
                     "install": SP.SP_DriveHDIcon, "applications": SP.SP_FileDialogListView,
                     "security": SP.SP_DialogApplyButton, "network": SP.SP_DriveNetIcon,
                     "system": SP.SP_ComputerIcon}


class SidebarDelegate(QStyledItemDelegate):
    """Rounded selection in the accent colour, icon and label."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), max(36, QFontMetrics(option.font).height() + 18))

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = option.palette
        rect = QRectF(option.rect).adjusted(6, 2, -6, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus) and option.widget is not None \
            and option.widget.hasFocus()
        base, accent = palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Highlight)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(rect, 6, 6)
        elif hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(mix(base, accent, 0.14))
            painter.drawRoundedRect(rect, 6, 6)
        if focused:
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Text) if selected else accent, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1.5, 1.5, -1.5, -1.5), 5, 5)
        qicon = index.data(Qt.ItemDataRole.DecorationRole)
        size = 22
        if qicon is not None:
            qicon.paint(painter, int(rect.left()) + 10, int(rect.center().y() - size / 2), size, size)
        painter.setPen(palette.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text))
        font = scaled_font(option.font, weight=QFont.Weight.DemiBold) if selected else option.font
        painter.setFont(font)
        text_rect = rect.adjusted(10 + size + 10, 0, -6, 0)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         QFontMetrics(font).elidedText(index.data(Qt.ItemDataRole.DisplayRole),
                                                       Qt.TextElideMode.ElideRight, int(text_rect.width())))
        painter.restore()


class Sidebar(QListWidget):
    """The category list; keyboard navigation skips hidden categories (Install outside a live session)."""

    def moveCursor(self, action, modifiers):
        index = super().moveCursor(action, modifiers)
        actions = QAbstractItemView.CursorAction
        backwards = action in (actions.MoveUp, actions.MovePageUp, actions.MovePrevious, actions.MoveEnd)
        step = -1 if backwards else 1
        row = index.row()
        while 0 <= row < self.count() and self.item(row).isHidden():
            row += step
        if not 0 <= row < self.count():
            return self.currentIndex()
        return self.model().index(row, 0)


class Header(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAutoFillBackground(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 10, 10)
        layout.setSpacing(10)
        self.mark = QLabel(self)
        self.mark.setPixmap(mark_icon().pixmap(QSize(28, 28)))
        self.mark.setFixedSize(28, 28)
        self.mark.setAccessibleName("Boswas OS")
        self.title = plain_label(APP_NAME, parent=self)
        self.title.setFont(scaled_font(self.title.font(), 1.15, weight=QFont.Weight.DemiBold))
        self.live_badge = Badge("Live session", icon("media-optical", "drive-optical"), parent=self)
        self.live_badge.setToolTip("Boswas OS is running from the boot medium; changes are lost at shutdown")
        self.live_badge.hide()
        self.refresh_button = QToolButton(self)
        self.refresh_button.setIcon(icon("view-refresh", fallback=SP.SP_BrowserReload))
        self.refresh_button.setToolTip("Refresh this page (F5)")
        self.refresh_button.setAccessibleName("Refresh this page")
        self.refresh_button.setAutoRaise(True)
        layout.addWidget(self.mark)
        layout.addWidget(self.title)
        layout.addWidget(self.live_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        layout.addWidget(self.refresh_button)


class MainWindow(QMainWindow):
    def __init__(self, backend, *, runner: Runner | None = None, initial_page: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.backend = backend
        self.runner = runner or Runner(self)
        self.store = Store(backend, self.runner, self)
        self.pages: dict[str, QWidget] = {}
        self.live: bool | None = None
        self.pending_page = initial_page if initial_page in catalog.PAGE_KEYS else None
        # Modal interactions go through these attributes so tests can answer them.
        self.ask = self._ask
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1180, 780)
        self.setMinimumSize(820, 560)
        self._build()
        self.store.changed.connect(self._store_changed)
        for key, slot in ((QKeySequence.StandardKey.Refresh, self.refresh_current),
                          (QKeySequence.StandardKey.Find, self.focus_search),
                          (QKeySequence.StandardKey.Quit, self.close)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)
        self.store.request("live")
        start = self.pending_page if self.pending_page and self.pending_page != "install" else DEFAULT_PAGE
        self.show_page(start)
        self.sidebar.setFocus(Qt.FocusReason.OtherFocusReason)

    # --- layout ---
    def _build(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = Header(central)
        self.header.refresh_button.clicked.connect(self.refresh_current)
        outer.addWidget(self.header)
        outer.addWidget(Separator(central))

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = Sidebar(central)
        self.sidebar.setItemDelegate(SidebarDelegate(self.sidebar))
        self.sidebar.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar.setFixedWidth(236)
        self.sidebar.setIconSize(QSize(22, 22))
        self.sidebar.setMouseTracking(True)
        self.sidebar.setAccessibleName("Settings categories")
        self.sidebar.setAccessibleDescription("Use the Up and Down arrow keys to switch between categories")
        self.sidebar.setContentsMargins(0, 8, 0, 8)
        self.sidebar.setSpacing(1)
        for page in catalog.PAGES:
            fallback = SIDEBAR_FALLBACKS.get(page.key, SP.SP_FileDialogInfoView)
            item = QListWidgetItem(icon(*page.icons, fallback=fallback), page.label)
            item.setData(Qt.ItemDataRole.UserRole, page.key)
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, page.description)
            item.setToolTip(page.description)
            self.sidebar.addItem(item)
            item.setHidden(page.live_only)
        body.addWidget(self.sidebar)
        body.addWidget(_VLine(central))

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.message_holder = QWidget(central)            # hidden with the message, margins and all
        message_row = QHBoxLayout(self.message_holder)
        message_row.setContentsMargins(24, 12, 24, 0)
        self.message = MessageBanner(parent=self.message_holder)
        self.message.closed.connect(self.message_holder.hide)
        message_row.addWidget(self.message)
        self.message_holder.hide()
        content.addWidget(self.message_holder)
        self.stack = QStackedWidget(central)
        self.holders: dict[str, QWidget] = {}
        for page in catalog.PAGES:
            holder = QWidget(self.stack)
            holder_layout = QVBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            self.holders[page.key] = holder
            self.stack.addWidget(holder)
        content.addWidget(self.stack, 1)
        body.addLayout(content, 1)
        outer.addLayout(body, 1)
        self.setCentralWidget(central)
        self.sidebar.currentRowChanged.connect(self._row_changed)
        self.setTabOrder(self.header.refresh_button, self.sidebar)

    # --- pages ---
    def page(self, key: str) -> QWidget:
        """The page widget, built on first use."""
        widget = self.pages.get(key)
        if widget is None:
            info = catalog.BY_KEY[key]
            widget = PAGE_CLASSES.get(key, SettingsPage)(info, self, self.holders[key])
            self.holders[key].layout().addWidget(widget)
            self.pages[key] = widget
        return widget

    def show_page(self, key: str) -> None:
        if key not in catalog.BY_KEY:
            raise ValueError(f"unknown page {key!r}")
        if catalog.BY_KEY[key].live_only and self.live is not True:
            if self.live is None:
                self.pending_page = key        # shown once the live check has answered
                return
            self.show_status("Install Boswas OS is available only in a live session.")
            return
        row = catalog.PAGE_KEYS.index(key)
        if self.sidebar.currentRow() == row:
            self._row_changed(row)
        else:
            self.sidebar.setCurrentRow(row)

    def current_page(self) -> str:
        return catalog.PAGES[self.stack.currentIndex()].key

    def _row_changed(self, row: int) -> None:
        if not 0 <= row < len(catalog.PAGES):
            return
        info = catalog.PAGES[row]
        if info.live_only and self.live is not True:
            # Never reachable outside a live session, whatever selected it.
            self.sidebar.setCurrentRow(self.stack.currentIndex())
            return
        self.stack.setCurrentIndex(row)
        self.page(info.key).activate()
        self.setWindowTitle(f"{info.heading} — {APP_NAME}" if info.heading != APP_NAME else APP_NAME)

    def refresh_current(self) -> None:
        self.page(self.current_page()).refresh()

    def focus_search(self) -> None:
        self.show_page("applications")
        self.page("applications").focus_search()

    # --- live session ---
    def _store_changed(self, key: str) -> None:
        if key != "live":
            return
        self.live = self.store.value("live") is True
        self.header.live_badge.setVisible(self.live)
        install_row = catalog.PAGE_KEYS.index("install")
        self.sidebar.item(install_row).setHidden(not self.live)
        if not self.live and self.current_page() == "install":
            self.show_page(DEFAULT_PAGE)
        if self.pending_page == "install":
            self.pending_page = None
            self.show_page("install" if self.live else DEFAULT_PAGE)
            if not self.live:
                self.show_status("Install Boswas OS is available only in a live session.")

    # --- actions ---
    def open_module(self, module: catalog.Module) -> None:
        self.runner.submit(self.backend.open_module, module, key=f"open:{module.kind}:{module.id}",
                           on_done=lambda _r: None,
                           on_error=lambda exc: self.show_error(f"{module.title} could not be opened. "
                                                                f"{self.error_text(exc)}"))

    def restart_to_install(self) -> None:
        if not self.live:
            return
        if not self.ask("Restart to install Boswas OS?",
                        "The computer restarts now. Files and settings of this live session are lost. When the boot "
                        "menu appears, choose “Install Boswas OS”.",
                        "Restart Now"):
            return
        self.show_status("Restarting…")
        self.runner.submit(self.backend.restart, on_error=lambda exc: self.show_error(
            f"The computer could not be restarted. {self.error_text(exc)}"))

    # --- messages ---
    @staticmethod
    def error_text(exc: Exception) -> str:
        return user_message(exc)

    def show_status(self, text: str) -> None:
        self.message.show_message("info", text)
        self.message_holder.show()

    def show_error(self, text: str) -> None:
        self.message.show_message("error", text)
        self.message_holder.show()

    def _ask(self, title: str, text: str, confirm: str) -> bool:
        box = message_box(self, "question", title, text)
        yes = box.addButton(confirm, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is yes

    # --- shutdown ---
    def shutdown(self) -> None:
        self.runner.shutdown()

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)


class _VLine(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedWidth(1)

    def paintEvent(self, event) -> None:
        QPainter(self).fillRect(self.rect(), border_colour(self.palette()))
