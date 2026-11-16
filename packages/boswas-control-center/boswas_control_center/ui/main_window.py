"""Main window: a navigation pane with the categories, the page title and lazily built pages.

The window is a FluentWindow of the Boswas UI toolkit with a NavigationView:
the categories on the left (with a search box that filters them by name
and by the settings they hold), the title of the current page above it,
and messages as an InfoBar between title and page. Pages are created the
first time they are shown, and each loads only the data it displays
(store.py). A page shows its native settings sections (ui/settings); they
are activated while the page is the current one and deactivated when it is
left or the window closes. Enter in the search box opens the page of the
best matching settings section and scrolls to it. A page that has nothing
to show on this system (no section, no content of its own) is left out of
the pane. The Install page exists only in a live session: its entry stays
hidden until the live check has answered yes. Keyboard: Up/Down in the
pane switch pages, Tab moves into the page, F5 refreshes the current page,
Ctrl+F searches the applications, Ctrl+Q quits.

A second start of the program does not open a second window: the toolkit
hands its arguments to handle_arguments(), which shows the page (and
section) they name.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget

from .. import catalog
from ..app import requested_page
from ..errors import user_message
from . import bridgeclient, settings
from .android_page import AndroidPage
from .applications import ApplicationsPage
from .common import SP, Badge, MessageBanner, app_icon, icon
from .device_pages import SettingsPage, StoragePage
from .hardware_page import HardwarePage
from .jobs import JobController
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
# Pages with content of their own; every other page is a SettingsPage: its native settings sections.
PAGE_CLASSES = {
    "personalization": PersonalizationPage, "storage": StoragePage,
    "applications": ApplicationsPage, "windows": WindowsPage, "security": SecurityPage, "updates": UpdatesPage,
    "privacy": PrivacyPage, "system": SystemPage, "about": AboutPage, "install": InstallPage,
    "android": AndroidPage, "hardware": HardwarePage,
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
                 initial_section: str | None = None, parent: QWidget | None = None):
        super().__init__(APP_ID, APP_NAME, parent, navigation={"search": True, "compact_below": 760},
                         icon=app_icon(), size=(1180, 780), minimum_size=(820, 560))
        self.backend = backend
        self.runner = runner or Runner(self)
        self.store = Store(backend, self.runner, self)
        self.pages: dict[str, QWidget] = {}
        self.live: bool | None = None
        self.pending_page = initial_page if initial_page in catalog.PAGE_KEYS else None
        self.pending_section = initial_section if self.pending_page else None
        self.closed = False
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
        self.show_page(start, self.pending_section if start == self.pending_page else None)
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
        section_words = settings.all_keywords()
        for page in catalog.PAGES:
            holder = QWidget()
            holder_layout = QVBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            self.holders[page.key] = holder
            fallback = SIDEBAR_FALLBACKS.get(page.key, SP.SP_FileDialogInfoView)
            # The search box of the pane also finds a category by the settings it holds.
            keywords = [*page.keywords, *section_words.get(page.key, ()),
                        *(word for program in page.programs for word in (program.title, program.description))]
            nav.add_page(page.key, page.label, holder, icon=icon(*page.icons, fallback=fallback),
                         tooltip=page.description, heading=page.heading, keywords=keywords)
            if page.live_only or not self.page_available(page.key):
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
        nav.search_submitted.connect(self._search_submitted)
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

    @staticmethod
    def page_available(key: str) -> bool:
        """Whether the page has anything to show: content of its own, a settings section or a program row."""
        return key in PAGE_CLASSES or bool(catalog.BY_KEY[key].programs) or bool(settings.sections_for(key))

    def show_page(self, key: str, section: str | type | None = None) -> None:
        """Make ``key`` the current page; with ``section`` (a title, command-line name or class), scroll to it."""
        if key not in catalog.BY_KEY:
            raise ValueError(f"unknown page {key!r}")
        if catalog.BY_KEY[key].live_only and self.live is not True:
            if self.live is None:
                self.pending_page = key        # shown once the live check has answered
                return
            self.show_status("Install Boswas OS is available only in a live session.")
            return
        if not self.page_available(key):
            self.show_status(f"The {catalog.BY_KEY[key].label} settings are not available on this system.")
            return
        if self.navigation.current() == key:
            self._page_selected(key)
        else:
            self.navigation.set_current(key)
        if section is not None and self._shown == key:
            self.page(key).scroll_to_section(section)

    def current_page(self) -> str:
        return self._shown or self.navigation.current()

    def _page_selected(self, key: str) -> None:
        info = catalog.BY_KEY[key]
        if (info.live_only and self.live is not True) or not self.page_available(key):
            # Never reachable outside a live session (or without content), whatever selected it.
            self.navigation.set_current(self._shown or DEFAULT_PAGE)
            return
        previous = self._shown
        if previous is not None and previous != key and previous in self.pages:
            self.pages[previous].deactivate()
        self._shown = key
        self.page(key).activate()
        self.setWindowTitle(f"{info.heading} — {APP_NAME}" if info.heading != APP_NAME else APP_NAME)
        query = self.navigation.search_box.text().strip() if self.navigation.search_box is not None else ""
        if query and previous != key:
            # Chosen from the filtered list: show what was searched for.
            found = settings.find(query, key)
            if found:
                self.page(key).scroll_to_section(found[0])

    # --- search and other instances ---
    def _search_submitted(self, query: str) -> None:
        """Enter in the search box: the page of the best matching settings section, scrolled to it."""
        found = [cls for cls in settings.find(query)
                 if self.page_available(cls.page) and not catalog.BY_KEY[cls.page].live_only]
        if found:
            self.show_page(found[0].page, found[0])

    def handle_arguments(self, arguments: list[str]) -> None:
        """A second start of the program: show the page (and section) its command line names."""
        page, section = requested_page(arguments)
        if page is not None:
            self.show_page(page, section)

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
    def open_program(self, program: catalog.Program) -> None:
        self.runner.submit(self.backend.open_program, program, key=f"open:{program.id}",
                           on_done=lambda _r: None,
                           on_error=lambda exc: self.show_error(f"{program.title} could not be opened. "
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
        """Stop everything that runs for this window: the sections' watching, the services bridge, the jobs."""
        if self.closed:
            return
        self.closed = True
        for page in self.pages.values():
            page.deactivate()
        bridgeclient.stop_for(self)
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
