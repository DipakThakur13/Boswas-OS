"""Main window: a navigation pane with the categories, the page title and lazily built pages.

The window is a FluentWindow of the Boswas UI toolkit with a NavigationView:
the categories on the left (with a search box that filters them by name
and by the settings they hold), the title of the current page above it,
and messages as an InfoBar between title and page. Pages are created the
first time they are shown, and each loads only the data it displays
(store.py). The Install page exists only in a live session: its entry stays
hidden until the live check has answered yes. Keyboard: Up/Down in the
pane switch pages, Tab moves into the page, F5 refreshes the current page,
Ctrl+F searches the applications, Ctrl+Q quits.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget

from .. import catalog
from ..errors import user_message
from .android_page import AndroidPage
from .applications import ApplicationsPage
from .common import SP, Badge, MessageBanner, app_icon, icon
from .device_pages import PowerPage, SettingsPage, StoragePage
from .hardware_page import HardwarePage
from .jobs import JobController
from .network_page import NetworkPage
from .personalization import PersonalizationPage
from .security import SecurityPage
from .status_pages import PrivacyPage, WindowsPage
from .store import Store
from .system_pages import AboutPage, InstallPage, SystemPage
from .updates import UpdatesPage
from .worker import Runner

APP_NAME = "Boswas Control Center"
APP_ID = "com.boswas.ControlCenter"
DEFAULT_PAGE = "personalization"
PAGE_CLASSES = {
    "personalization": PersonalizationPage, "power": PowerPage, "storage": StoragePage,
    "applications": ApplicationsPage, "windows": WindowsPage, "security": SecurityPage, "updates": UpdatesPage,
    "privacy": PrivacyPage, "system": SystemPage, "about": AboutPage, "install": InstallPage,
    "android": AndroidPage, "hardware": HardwarePage, "network": NetworkPage,
}
SIDEBAR_FALLBACKS = {"about": SP.SP_MessageBoxInformation, "storage": SP.SP_DriveHDIcon,
                     "install": SP.SP_DriveHDIcon, "applications": SP.SP_FileDialogListView,
                     "security": SP.SP_DialogApplyButton, "network": SP.SP_DriveNetIcon,
                     "system": SP.SP_ComputerIcon, "hardware": SP.SP_ComputerIcon, "android": SP.SP_DesktopIcon,
                     "updates": SP.SP_BrowserReload}


class Header(QWidget):
    """What stands beside the page title: the live-session badge and the refresh button."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.live_badge = Badge("Live session", icon("media-optical", "drive-optical"), parent=self)
        self.live_badge.setToolTip("Boswas OS is running from the boot medium; changes are lost at shutdown")
        self.live_badge.hide()
        self.refresh_button = fluent.IconButton(icon("view-refresh", fallback=SP.SP_BrowserReload),
                                                "Refresh this page (F5)", self)
        self.refresh_button.setAccessibleName("Refresh this page")
        layout.addWidget(self.live_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.refresh_button)


class MainWindow(fluent.FluentWindow):
    def __init__(self, backend, *, runner: Runner | None = None, initial_page: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(APP_ID, APP_NAME, parent, navigation={"search": True, "compact_below": 760},
                         icon=app_icon(), size=(1180, 780), minimum_size=(820, 560))
        self.backend = backend
        self.runner = runner or Runner(self)
        self.store = Store(backend, self.runner, self)
        self.pages: dict[str, QWidget] = {}
        self.live: bool | None = None
        self.pending_page = initial_page if initial_page in catalog.PAGE_KEYS else None
        # Modal interactions go through these attributes so tests can answer them.
        self.ask = self._ask
        self.choose_file = self._choose_file
        self.jobs = JobController(self)
        self.close_when_done = False
        self.jobs.finished.connect(self._job_finished)
        self._shown: str | None = None
        self._build()
        self.store.changed.connect(self._store_changed)
        self.add_shortcut("refresh", self.refresh_current)
        self.add_shortcut("find", self.focus_search)
        self.add_shortcut("quit", self.close)
        self.store.request("live")
        start = self.pending_page if self.pending_page and self.pending_page != "install" else DEFAULT_PAGE
        self.show_page(start)
        self.sidebar.setFocus(Qt.FocusReason.OtherFocusReason)

    # --- layout ---
    def _build(self) -> None:
        nav = self.navigation
        self.sidebar = nav.list
        self.sidebar.setIconSize(QSize(20, 20))
        self.sidebar.setAccessibleName("Settings categories")
        self.sidebar.setAccessibleDescription("Use the Up and Down arrow keys to switch between categories")
        self.stack = nav.stack
        self.holders: dict[str, QWidget] = {}
        for page in catalog.PAGES:
            holder = QWidget()
            holder_layout = QVBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            self.holders[page.key] = holder
            fallback = SIDEBAR_FALLBACKS.get(page.key, SP.SP_FileDialogInfoView)
            # The search box of the pane also finds a category by the settings it holds.
            keywords = [word for module in page.modules for word in (module.title, module.description)]
            nav.add_page(page.key, page.label, holder, icon=icon(*page.icons, fallback=fallback),
                         tooltip=page.description, heading=page.heading, keywords=keywords)
            if page.live_only:
                nav.set_item_visible(page.key, False)
        self.header = Header()
        self.header.refresh_button.clicked.connect(self.refresh_current)
        nav.add_header_widget(self.header)
        self.message_holder = QWidget()                   # hidden with the message
        message_row = QHBoxLayout(self.message_holder)
        message_row.setContentsMargins(0, 0, 0, 0)
        self.message = MessageBanner(parent=self.message_holder)
        self.message.closed.connect(self.message_holder.hide)
        message_row.addWidget(self.message)
        self.message_holder.hide()
        nav.add_banner(self.message_holder)
        nav.current_changed.connect(self._page_selected)
        if nav.search_box is not None:
            nav.search_box.setPlaceholderText("Find a setting")
            nav.search_box.setAccessibleName("Find a setting")

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
        if self.navigation.current() == key:
            self._page_selected(key)
        else:
            self.navigation.set_current(key)

    def current_page(self) -> str:
        return self._shown or self.navigation.current()

    def _page_selected(self, key: str) -> None:
        info = catalog.BY_KEY[key]
        if info.live_only and self.live is not True:
            # Never reachable outside a live session, whatever selected it.
            self.navigation.set_current(self._shown or DEFAULT_PAGE)
            return
        self._shown = key
        self.page(key).activate()
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
        self.navigation.set_item_visible("install", self.live)
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

    def _choose_file(self, title: str, filters: str) -> str | None:
        path, _selected = QFileDialog.getOpenFileName(self, title, "", filters)
        return path or None

    def _ask(self, title: str, text: str, confirm: str) -> bool:
        # A ContentDialog inside the window; Cancel is the default, so Enter never confirms by accident.
        return fluent.ask(self, title, text, confirm, "Cancel", default="cancel")

    # --- shutdown ---
    def shutdown(self) -> None:
        self.jobs.stop_for_close()
        self.runner.shutdown()

    def _job_finished(self, _spec, _outcome) -> None:
        if self.close_when_done:
            self.close()

    def closeEvent(self, event) -> None:
        if self.jobs.busy():
            current = self.jobs.current.spec.title
            if self.jobs.current.state.cancellable:
                if not self.ask("Cancel and close?", f"{current} is still running. Closing Control Center cancels "
                                                     "it; nothing has been changed yet.", "Cancel and Close"):
                    event.ignore()
                    return
            else:
                # Packages are being installed: never interrupt dpkg. Close once it has finished, if wanted.
                self.close_when_done = self.ask("Wait for the operation to finish",
                                                f"{current} cannot be interrupted safely. Control Center can close "
                                                "by itself when it has finished.", "Close When Finished")
                if self.close_when_done:
                    self.show_status(f"{current} is finishing; Control Center closes when it is done.")
                event.ignore()
                return
        self.shutdown()
        super().closeEvent(event)
