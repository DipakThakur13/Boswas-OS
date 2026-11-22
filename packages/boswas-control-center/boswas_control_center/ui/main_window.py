"""Main window: the categories in the pane, a category's pages as cards, the path above the page, and search.

The window is a FluentWindow of the Boswas UI toolkit with a NavigationView.
The pane holds the categories (catalog.CATEGORIES: Home, System, Bluetooth &
devices, ...). Each category has a stack of its own in the content area: its
category page, which lists its pages as cards, and the pages themselves,
built the first time they are shown. Above the content stands the path to
the current place ("System > Display", a BreadcrumbBar in the title style)
with the back button before it: a crumb or Back returns. Messages are an
InfoBar between the path and the page. Each page loads only the data it
displays (store.py); its native settings sections (ui/settings) are
activated while it is shown and deactivated when it is left or the window
closes. A page that has nothing to show on this system (no section, no
content of its own) has no card, and a category without such a page is left
out of the pane. The Install page exists only in a live session: its card
stays hidden until the live check has answered yes.

Search: typing in the box of the pane shows the results in the content area,
the categories, pages, sections and single settings whose names or keywords
contain every word (catalog.search); a result opens its place and scrolls to
its section, Enter opens the first. Keyboard: Up/Down in the pane switch
categories, Tab moves into the page, Alt+Left goes back, F5 refreshes the
current page, Ctrl+F searches the applications, Ctrl+Q quits.

``--page KEY`` names a category or a page; the keys of earlier versions
still lead to the place their settings moved to, and ``--section`` is
looked for there too (catalog.section_homes). A second start of the program
does not open a second window: the toolkit hands its arguments to
handle_arguments(), which shows the place they name.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from .. import catalog
from ..app import requested_page
from ..errors import user_message
from . import bridgeclient, settings
from .android_page import AndroidPage
from .applications import ApplicationsPage
from .category import CategoryPage, SearchResults
from .common import SP, Badge, MessageBanner, app_icon, icon
from .device_pages import SettingsPage, StoragePage
from .hardware_page import HardwarePage
from .home import HomePage
from .jobs import JobController
from .personalization import PersonalizationPage
from .security import SecurityPage
from .status_pages import DeviceManagementPage, PrivacyPage, WindowsPage
from .store import Store
from .system_pages import AboutPage, InstallPage
from .updates import UpdatesPage
from .worker import Runner

APP_NAME = "Boswas Control Center"
APP_ID = "com.boswas.ControlCenter"
DEFAULT_PAGE = "home"
COMPACT_BELOW = 720
# Places with content of their own; every other category is a CategoryPage (its pages as cards) and every other
# page a SettingsPage (its native settings sections).
PAGE_CLASSES = {
    "home": HomePage, "updates": UpdatesPage, "themes": PersonalizationPage, "storage": StoragePage,
    "applications": ApplicationsPage, "windows": WindowsPage, "security": SecurityPage, "privacy": PrivacyPage,
    "about": AboutPage, "install": InstallPage, "android": AndroidPage, "hardware": HardwarePage,
    "work": DeviceManagementPage,
}
SIDEBAR_FALLBACKS = {"about": SP.SP_MessageBoxInformation, "storage": SP.SP_DriveHDIcon,
                     "install": SP.SP_DriveHDIcon, "apps": SP.SP_FileDialogListView,
                     "privacy-security": SP.SP_DialogApplyButton, "network": SP.SP_DriveNetIcon,
                     "system": SP.SP_ComputerIcon, "hardware": SP.SP_ComputerIcon, "devices": SP.SP_ComputerIcon,
                     "updates": SP.SP_BrowserReload, "home": SP.SP_DirHomeIcon}
SEARCH_TITLE = "Search results"


class Header(QWidget):
    """What stands beside the path: the live-session badge and the refresh button."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.live_badge = Badge("Live session", icon("media-optical", "drive-optical"), parent=self)
        self.live_badge.setToolTip("Boswas OS is running from the boot medium; changes are lost at shutdown")
        self.live_badge.hide()
        self.refresh_button = fluent.IconButton(icon("view-refresh-symbolic", "view-refresh",
                                                     fallback=SP.SP_BrowserReload),
                                                "Refresh this page (F5)", self)
        self.refresh_button.setAccessibleName("Refresh this page")
        layout.addWidget(self.live_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.refresh_button)


class MainWindow(fluent.FluentWindow):
    def __init__(self, backend, *, runner: Runner | None = None, initial_page: str | None = None,
                 initial_section: str | None = None, parent: QWidget | None = None):
        super().__init__(APP_ID, APP_NAME, parent, navigation={"search": True, "compact_below": COMPACT_BELOW},
                         icon=app_icon(), size=(1100, 720), minimum_size=(560, 440))
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
        self.history: list[str] = []
        self._selecting = False
        self._query = ""
        self._build()
        self.store.changed.connect(self._store_changed)
        self.add_shortcut("refresh", self.refresh_current)
        self.add_shortcut("find", self.focus_search)
        self.add_shortcut("quit", self.close)
        self.add_shortcut("back", self.go_back)
        self.store.request("live")
        start = self.pending_page if self.pending_page and self.pending_page != "install" else DEFAULT_PAGE
        self.show_page(start, self.pending_section if start == self.pending_page else None)
        self.history.clear()
        self._update_header()
        self.sidebar.setFocus(Qt.FocusReason.OtherFocusReason)

    # --- layout ---
    def _build(self) -> None:
        nav = self.navigation
        self.sidebar = nav.list
        self.sidebar.setIconSize(QSize(20, 20))
        self.sidebar.setAccessibleName("Settings categories")
        self.sidebar.setAccessibleDescription("Use the Up and Down arrow keys to switch between categories")
        self.stack = nav.stack
        self.holders: dict[str, QStackedWidget] = {}
        section_words = settings.all_keywords()
        for category in catalog.CATEGORIES:
            holder = QStackedWidget()
            holder.setAccessibleName(category.heading)
            self.holders[category.key] = holder
            fallback = SIDEBAR_FALLBACKS.get(category.key, SP.SP_FileDialogInfoView)
            words = [*category.keywords]
            for page in (category, *catalog.children(category.key)):
                words += [page.label, *page.keywords, *section_words.get(page.key, ())]
            nav.add_page(category.key, category.label, holder, icon=icon(*category.icons, fallback=fallback),
                         tooltip=category.description, heading=category.heading, keywords=words)
            if not self.page_available(category.key):
                nav.set_item_visible(category.key, False)
        # The pane is not filtered by the search box: the results are listed in the content area.
        nav.set_search_filters_items(False)
        self.results = SearchResults(self)
        self.results.chosen.connect(self.open_result)
        self.stack.addWidget(self.results)
        nav.title_label.hide()
        self.back_button = fluent.IconButton(fluent.icon("go-previous-symbolic"), "Back (Alt+Left)", size=36)
        self.back_button.setAccessibleName("Back")
        self.back_button.clicked.connect(self.go_back)
        nav.add_header_widget(self.back_button)
        self.crumbs = fluent.BreadcrumbBar(style="title")
        self.crumbs.setAccessibleName("Where you are")
        self.crumbs.item_clicked.connect(self._crumb_clicked)
        nav.add_header_widget(self.crumbs)
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
        nav.current_changed.connect(self._category_selected)
        nav.list.itemClicked.connect(self._pane_clicked)
        nav.search_changed.connect(self._search_changed)
        nav.search_submitted.connect(self._search_submitted)
        if nav.search_box is not None:
            nav.search_box.setPlaceholderText("Find a setting")
            nav.search_box.setAccessibleName("Find a setting")

    # --- places ---
    def page(self, key: str) -> QWidget:
        """The widget of a category or page, built on first use."""
        widget = self.pages.get(key)
        if widget is None:
            info = catalog.BY_KEY[key]
            cls = PAGE_CLASSES.get(key, CategoryPage if info.is_category else SettingsPage)
            holder = self.holders[catalog.category_of(key)]
            widget = cls(info, self, holder)
            holder.addWidget(widget)
            self.pages[key] = widget
        return widget

    @staticmethod
    def page_available(key: str) -> bool:
        """Whether a place has anything to show: content of its own, a settings section, a program row, or (for a
        category) a page that has."""
        info = catalog.BY_KEY[key]
        if key in PAGE_CLASSES or info.programs or settings.sections_for(key):
            return True
        return info.is_category and any(MainWindow.page_available(child.key) for child in catalog.children(key))

    def page_visible(self, key: str) -> bool:
        """Whether a place can be shown now: it has something to show and, if it is live-only, this is live."""
        info = catalog.BY_KEY.get(key)
        return info is not None and self.page_available(key) and (not info.live_only or self.live is True)

    def show_page(self, key: str, section: str | type | None = None) -> None:
        """Show the category or page ``key``; with ``section`` (a title, command-line name or class), scroll to it.

        A section that is not on ``key`` itself is looked for where the settings of ``key`` were in earlier
        versions and, for a category, on its pages (catalog.section_homes); the first page that has it is shown.
        """
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
        target, found = self.resolve(key, section)
        self._go(target, found)

    def resolve(self, key: str, section: str | type | None) -> tuple[str, str | type | None]:
        """(the place to show, the section to scroll to) for ``--page key --section section``."""
        if section is None:
            return key, None
        if isinstance(section, type):
            pages = settings.pages_of(section) if hasattr(section, "page") else ()
            if key in pages:
                return key, section
            return (section.page, section) if self.page_visible(section.page) else (key, None)
        name = catalog.SECTION_ALIASES.get(section.casefold(), section.casefold())
        if name in catalog.BY_KEY and name != key and catalog.BY_KEY[name].parent in (key, ""):
            if name in catalog.section_homes(key) and self.page_visible(name):
                return name, None              # the section of earlier versions is a page of its own now
        for home in catalog.section_homes(key):
            if not self.page_visible(home):
                continue
            for cls in settings.sections_for(home):
                if name in (cls.__name__.casefold(), cls.title.casefold(), settings.section_id(cls)):
                    return home, cls
        return key, None

    def _go(self, key: str, section: str | type | None = None, *, remember: bool = True) -> None:
        previous = self._shown
        if previous is not None and previous != key:
            if remember:
                self.history.append(previous)
                del self.history[:-50]
            if previous in self.pages:
                self.pages[previous].deactivate()
        self._shown = key
        category = catalog.category_of(key)
        self._selecting = True
        try:
            if self.navigation.current() != category:
                self.navigation.set_current(category)
        finally:
            self._selecting = False
        holder = self.holders[category]
        page = self.page(key)
        holder.setCurrentWidget(page)
        if self._query:
            self._query = ""
            box = self.navigation.search_box
            if box is not None and box.text():
                blocked = box.blockSignals(True)
                box.clear()
                box.blockSignals(blocked)
        self.stack.setCurrentWidget(holder)
        page.activate()
        self._update_header()
        if section is not None:
            page.scroll_to_section(section)

    def current_page(self) -> str:
        return self._shown or self.navigation.current()

    def current_category(self) -> str:
        return catalog.category_of(self.current_page())

    def _category_selected(self, key: str) -> None:
        """A category was chosen in the pane (click, arrow keys): show its category page."""
        if self._selecting or key not in catalog.BY_KEY:
            return
        if not self.page_visible(key):
            # Never reachable without content, whatever selected it.
            self._selecting = True
            try:
                self.navigation.set_current(self.current_category() if self._shown else DEFAULT_PAGE)
            finally:
                self._selecting = False
            return
        self._go(key)

    def _pane_clicked(self, item) -> None:
        """A click on the category that is already selected returns to its category page."""
        key = next((k for k in self.navigation.keys() if self.navigation.item(k) is item), None)
        if key is not None and key in catalog.BY_KEY and (key != self._shown or self._query):
            self._go(key)

    def _crumb_clicked(self, index: int) -> None:
        if index == 0 and not self._query:
            self._go(self.current_category())

    def can_go_back(self) -> bool:
        return bool(self.history) or not catalog.BY_KEY[self.current_page()].is_category or bool(self._query)

    def go_back(self) -> None:
        """Back: to the place shown before, or from a page to its category."""
        if self._query:
            self._end_search()
            return
        while self.history:
            key = self.history.pop()
            if self.page_visible(key):
                self._go(key, remember=False)
                return
        current = self.current_page()
        if not catalog.BY_KEY[current].is_category:
            self._go(catalog.category_of(current), remember=False)
        self._update_header()

    def _update_header(self) -> None:
        if self._query:
            items, heading = [SEARCH_TITLE], SEARCH_TITLE
        else:
            key = self.current_page()
            items, heading = list(catalog.path(key)), catalog.BY_KEY[key].heading
        if self.crumbs.items() != items:
            self.crumbs.set_items(items)
        self.setWindowTitle(f"{heading} — {APP_NAME}")
        self.back_button.setEnabled(self.can_go_back())
        self.header.refresh_button.setVisible(not self._query)

    # --- search and other instances ---
    def search(self, query: str) -> list[catalog.Entry]:
        return catalog.search(query, catalog.entries(settings.search_entries()), visible=self.page_visible)

    def _search_changed(self, text: str) -> None:
        query = text.strip()
        if not query:
            self._end_search()
            return
        self._query = query
        self.results.show_results(query, self.search(query))
        self.stack.setCurrentWidget(self.results)
        self._update_header()

    def _end_search(self) -> None:
        if not self._query:
            return
        self._query = ""
        box = self.navigation.search_box
        if box is not None and box.text():
            blocked = box.blockSignals(True)
            box.clear()
            box.blockSignals(blocked)
        self.stack.setCurrentWidget(self.holders[self.current_category()])
        self._update_header()

    def _search_submitted(self, query: str) -> None:
        """Enter in the search box: the best result."""
        found = self.search(query)
        if found:
            self.open_result(found[0])

    def open_result(self, entry: catalog.Entry) -> None:
        self._end_search()
        self.show_page(entry.page, entry.section or None)

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
        if not self.live and self.current_page() == "install":
            self.show_page(DEFAULT_PAGE)
        if self.pending_page == "install":
            self.pending_page = None
            self.show_page("install" if self.live else DEFAULT_PAGE)
            if not self.live:
                self.show_status("Install Boswas OS is available only in a live session.")
        for page in self.pages.values():
            page.places_changed()

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
