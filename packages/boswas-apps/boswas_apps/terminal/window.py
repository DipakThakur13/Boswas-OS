"""TerminalWindow: a window of tabs, each a terminal (or the settings).

Keyboard, as people know it from other terminals:

    Ctrl+Shift+T / N / W     new tab / new window / close tab
    Ctrl+Shift+D             duplicate the tab
    Ctrl+Shift+1..9          new tab with the first..ninth profile
    Ctrl+Tab, Ctrl+Shift+Tab next and previous tab; Alt+1..9 a tab by position
    Ctrl+Shift+C / V / A     copy, paste, select all
    Ctrl+Shift+F             find in the scrollback
    Ctrl+= / Ctrl+- / Ctrl+0 larger, smaller, normal text
    F11                      full screen
    Ctrl+,                   settings
    Ctrl+Shift+Space         the menu of profiles
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

import boswas_fluent as fluent

from . import APP_ID, DISPLAY_NAME, VERSION, cli
from .pane import SessionFactory, TerminalPane
from .settings_page import SettingsPage
from .tabstrip import STRIP_HEIGHT, TabStrip

DEFAULT_GRID = (120, 30)


class _TabRow(QWidget):
    """The strip behind the tabs, in the colour of window chrome so the current tab stands out from it."""

    def paintEvent(self, _event) -> None:
        QPainter(self).fillRect(self.rect(), fluent.qcolor("chrome"))


class TerminalWindow(fluent.FluentWindow):
    def __init__(self, application, session_factory: SessionFactory | None = None) -> None:
        super().__init__(APP_ID, DISPLAY_NAME, icon="boswas-terminal", size=(1000, 640), minimum_size=(360, 220))
        self.application = application
        self.store = application.store
        self.session_factory = session_factory
        self.tabs: list[QWidget] = []
        self.shortcuts: dict[str, QShortcut] = {}
        self.settings_page: SettingsPage | None = None
        self.menu: fluent.MenuFlyout | None = None
        self._closing = False
        self._sized = False
        central = QWidget(self)
        column = QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.tab_row = _TabRow(central)
        self.tab_row.setFixedHeight(STRIP_HEIGHT)
        row = QHBoxLayout(self.tab_row)
        row.setContentsMargins(8, 0, 8, 0)
        row.setSpacing(2)
        self.strip = TabStrip(self.tab_row)
        self.new_button = fluent.IconButton("list-add-symbolic", "New tab (Ctrl+Shift+T)", self.tab_row, size=28,
                                            icon_size=14)
        self.menu_button = fluent.IconButton(fluent.glyph_icon("chevron-down"), "Profiles and settings "
                                             "(Ctrl+Shift+Space)", self.tab_row, size=28, icon_size=12)
        row.addWidget(self.strip, 0, Qt.AlignmentFlag.AlignBottom)
        row.addSpacing(4)
        row.addWidget(self.new_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.menu_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        column.addWidget(self.tab_row)
        self.stack = QStackedWidget(central)
        column.addWidget(self.stack, 1)
        self.set_content(central)
        self.strip.current_changed.connect(self._current_changed)
        self.strip.close_requested.connect(self.close_tab)
        self.strip.tab_moved.connect(self._tab_moved)
        self.strip.renamed.connect(self._renamed)
        self.strip.menu_requested.connect(self._tab_menu)
        self.strip.new_tab_requested.connect(self.new_tab_here)
        self.new_button.clicked.connect(self.new_tab_here)
        self.menu_button.clicked.connect(self.show_menu)
        self._add_shortcuts()

    # --- shortcuts ---
    def _shortcut(self, keys: str, slot: Callable[[], None]) -> None:
        for shortcut in self.add_shortcut(keys, slot):
            self.shortcuts[shortcut.key().toString()] = shortcut

    def _add_shortcuts(self) -> None:
        for keys, slot in (
                ("Ctrl+Shift+T", self.new_tab_here), ("Ctrl+Shift+N", self.new_window),
                ("Ctrl+Shift+W", self.close_current_tab), ("Ctrl+Shift+D", self.duplicate_current_tab),
                ("Ctrl+Tab", self.next_tab), ("Ctrl+Shift+Tab", self.previous_tab),
                ("Ctrl+PgDown", self.next_tab), ("Ctrl+PgUp", self.previous_tab),
                ("Ctrl+Shift+C", self.copy), ("Ctrl+Shift+V", self.paste), ("Ctrl+Shift+A", self.select_all),
                ("Ctrl+Shift+F", self.find), ("Ctrl+=", self.zoom_in), ("Ctrl++", self.zoom_in),
                ("Ctrl+-", self.zoom_out), ("Ctrl+0", self.zoom_reset), ("F11", self.toggle_fullscreen),
                ("Ctrl+,", self.open_settings), ("Ctrl+Shift+Space", self.show_menu)):
            self._shortcut(keys, slot)
        for number in range(1, 10):
            self._shortcut(f"Alt+{number}", lambda n=number: self.select_tab(n - 1))
            self._shortcut(f"Ctrl+Shift+{number}", lambda n=number: self.new_tab_with_profile_number(n - 1))

    def trigger(self, keys: str) -> bool:
        """Do what a shortcut of this window does (tests). False when there is no such shortcut."""
        from PySide6.QtGui import QKeySequence
        shortcut = self.shortcuts.get(QKeySequence(keys).toString())
        if shortcut is None:
            return False
        shortcut.activated.emit()
        return True

    # --- tabs ---
    def current_pane(self) -> TerminalPane | None:
        widget = self.current_widget()
        return widget if isinstance(widget, TerminalPane) else None

    def current_widget(self) -> QWidget | None:
        index = self.strip.current()
        return self.tabs[index] if 0 <= index < len(self.tabs) else None

    def panes(self) -> list[TerminalPane]:
        return [tab for tab in self.tabs if isinstance(tab, TerminalPane)]

    def new_tab(self, profile_id: str | None = None, *, directory: str = "", fallback_directory: str = "",
                command: list[str] | None = None, title: str = "", index: int | None = None) -> TerminalPane:
        pane = TerminalPane(self.store, profile_id or self.store.settings.default_profile, directory=directory,
                            fallback_directory=fallback_directory, command=command, title=title,
                            session_factory=self.session_factory, parent=self.stack)
        pane.changed.connect(lambda p=pane: self._refresh_tab(p))
        pane.close_requested.connect(lambda p=pane: self._pane_wants_to_close(p))
        pane.settings_requested.connect(self.open_settings)
        self._insert(pane, pane.title(), index)
        if not self._sized:
            self._sized = True
            hint = pane.view.size_for(*DEFAULT_GRID)
            self.resize(hint.width(), hint.height() + STRIP_HEIGHT)
        return pane

    def _insert(self, widget: QWidget, title: str, index: int | None = None, icon: str = "") -> None:
        index = len(self.tabs) if index is None else max(0, min(index, len(self.tabs)))
        self.tabs.insert(index, widget)
        self.stack.addWidget(widget)
        self.strip.add_tab(title, index, icon)
        self._refresh_tab(widget)
        self.strip.set_current(index)
        self._current_changed(self.strip.current())

    def open_request(self, request: cli.Request) -> TerminalPane:
        """A tab for what a start of the program asked for."""
        profile = self.store.settings.find(request.profile) if request.profile else None
        pane = self.new_tab(profile.id if profile is not None else None, directory=request.directory,
                            fallback_directory=request.launch_directory, command=request.command or None,
                            title=request.title)
        if request.profile and profile is None:
            pane.info.show_message("warning", f"There is no profile named “{request.profile[:64]}”. "
                                   "The default profile is used.")
        return pane

    def new_tab_here(self) -> TerminalPane:
        """A new tab with the default profile, in the directory of the current tab."""
        current = self.current_pane()
        directory = current.current_directory() if current is not None else ""
        return self.new_tab(directory=directory, index=self.strip.current() + 1 if current is not None else None)

    def new_tab_with_profile_number(self, number: int) -> None:
        profiles = self.store.settings.profiles
        if 0 <= number < len(profiles):
            self.new_tab_with_profile(profiles[number].id)

    def new_tab_with_profile(self, profile_id: str) -> TerminalPane:
        current = self.current_pane()
        profile = self.store.settings.profile(profile_id)
        directory = current.current_directory() if current is not None and not profile.directory else ""
        return self.new_tab(profile_id, directory=directory)

    def duplicate_current_tab(self) -> None:
        pane = self.current_pane()
        if pane is not None:
            self.duplicate_tab(self.tabs.index(pane))

    def duplicate_tab(self, index: int) -> None:
        if not 0 <= index < len(self.tabs) or not isinstance(self.tabs[index], TerminalPane):
            return
        pane = self.tabs[index]
        copy = self.new_tab(pane.profile_id, directory=pane.current_directory() or pane.directory,
                            fallback_directory=pane.fallback_directory, command=pane.command,
                            title=pane.fixed_title, index=index + 1)
        copy.view.set_zoom(pane.view.zoom())

    def new_window(self) -> None:
        pane = self.current_pane()
        request = cli.Request(directory=pane.current_directory() if pane is not None else "", new_window=True)
        self.application.open(request).show()

    def select_tab(self, index: int) -> None:
        if index == 8:
            index = len(self.tabs) - 1               # the ninth shortcut is the last tab, as in browsers
        if 0 <= index < len(self.tabs):
            self.strip.set_current(index)

    def next_tab(self) -> None:
        if self.tabs:
            self.strip.set_current((self.strip.current() + 1) % len(self.tabs))

    def previous_tab(self) -> None:
        if self.tabs:
            self.strip.set_current((self.strip.current() - 1) % len(self.tabs))

    def _current_changed(self, index: int) -> None:
        if not 0 <= index < len(self.tabs):
            return
        widget = self.tabs[index]
        for tab in self.tabs:
            if isinstance(tab, TerminalPane):
                tab.set_active(tab is widget)
        self.stack.setCurrentWidget(widget)
        self._refresh_tab(widget)
        if isinstance(widget, TerminalPane):
            widget.view.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            widget.setFocus(Qt.FocusReason.OtherFocusReason)

    def _tab_moved(self, source: int, target: int) -> None:
        self.tabs.insert(target, self.tabs.pop(source))

    def _renamed(self, index: int, title: str) -> None:
        if 0 <= index < len(self.tabs) and isinstance(self.tabs[index], TerminalPane):
            self.tabs[index].set_fixed_title(title)
        self._focus_current()

    def rename_current_tab(self) -> None:
        if self.current_pane() is not None:
            self.strip.start_rename(self.strip.current())

    def _focus_current(self) -> None:
        pane = self.current_pane()
        if pane is not None:
            pane.view.setFocus(Qt.FocusReason.OtherFocusReason)

    def _refresh_tab(self, widget: QWidget) -> None:
        if widget not in self.tabs:
            return
        index = self.tabs.index(widget)
        if isinstance(widget, TerminalPane):
            self.strip.set_title(index, widget.title())
            self.strip.set_indicator(index, widget.indicator)
            self.strip.set_colours(index, widget.view.background(), widget.view.foreground())
        if index == self.strip.current():
            title = self.strip.title(index)
            self.setWindowTitle(f"{title} – {DISPLAY_NAME}" if title and title != DISPLAY_NAME else DISPLAY_NAME)

    # --- closing ---
    def close_current_tab(self) -> None:
        if self.tabs:
            self.close_tab(self.strip.current())

    def close_tab(self, index: int) -> bool:
        """Close a tab, asking first when a program is running in it. True when it was closed."""
        if not 0 <= index < len(self.tabs):
            return False
        widget = self.tabs[index]
        if isinstance(widget, TerminalPane) and self.store.settings.confirm_close:
            program = widget.is_busy()
            if program and not self.ask("Close this tab?", f"“{program}” is still running in it and "
                                        "will be ended.", "Close tab", "Cancel"):
                self._focus_current()
                return False
        self._remove(index)
        return True

    def close_other_tabs(self, index: int) -> None:
        if not 0 <= index < len(self.tabs):
            return
        keep = self.tabs[index]
        for widget in [tab for tab in self.tabs if tab is not keep]:
            if not self.close_tab(self.tabs.index(widget)):
                break

    def _pane_wants_to_close(self, pane: TerminalPane) -> None:
        if pane in self.tabs:
            # Not from inside the signal of the session that just ended.
            QTimer.singleShot(0, lambda: pane in self.tabs and self._remove(self.tabs.index(pane)))

    def _remove(self, index: int) -> None:
        widget = self.tabs.pop(index)
        if isinstance(widget, TerminalPane):
            widget.shutdown()
        if widget is self.settings_page:
            self.settings_page = None
        self.strip.remove_tab(index)
        self.stack.removeWidget(widget)
        widget.deleteLater()
        if not self.tabs:
            if not self._closing:
                self.close()
            return
        self._current_changed(self.strip.current())

    def closeEvent(self, event) -> None:
        if not self._closing and self.store.settings.confirm_close:
            busy = [name for name in (pane.is_busy() for pane in self.panes()) if name]
            if busy:
                names = ", ".join(sorted(set(busy))[:6])
                if not self.ask("Close this window?", f"Programs are still running in {len(busy)} "
                                f"{'tab' if len(busy) == 1 else 'tabs'} and will be ended: {names}.",
                                "Close window", "Cancel"):
                    event.ignore()
                    self._focus_current()
                    return
        self._closing = True
        for pane in self.panes():
            pane.shutdown()
        super().closeEvent(event)
        if event.isAccepted():
            self.application.window_closed(self)

    # --- commands ---
    def copy(self) -> None:
        pane = self.current_pane()
        if pane is not None:
            pane.view.copy()

    def paste(self) -> None:
        pane = self.current_pane()
        if pane is not None:
            pane.view.paste()

    def select_all(self) -> None:
        pane = self.current_pane()
        if pane is not None:
            pane.view.select_all()

    def find(self) -> None:
        pane = self.current_pane()
        if pane is not None:
            pane.show_find()

    def zoom_in(self) -> None:
        pane = self.current_pane()
        if pane is not None:
            pane.view.zoom_in()

    def zoom_out(self) -> None:
        pane = self.current_pane()
        if pane is not None:
            pane.view.zoom_out()

    def zoom_reset(self) -> None:
        pane = self.current_pane()
        if pane is not None:
            pane.view.zoom_reset()

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def open_settings(self) -> None:
        if self.settings_page is None:
            self.settings_page = SettingsPage(self.store, self.stack)
            self._insert(self.settings_page, "Settings", icon="preferences-system-symbolic")
        else:
            self.strip.set_current(self.tabs.index(self.settings_page))

    def about(self) -> None:
        self.tell(f"{DISPLAY_NAME} {VERSION}", "The terminal of Boswas OS: tabs, profiles, colour schemes that "
                  "follow the desktop, search in the scrollback.\n\nSee the manual page boswas-terminal(1).")
        self._focus_current()

    # --- menus ---
    def build_menu(self) -> fluent.MenuFlyout:
        menu = fluent.MenuFlyout(self)
        settings = self.store.settings
        for number, profile in enumerate(settings.profiles[:32], 1):
            text = profile.name + ("  (default)" if profile.id == settings.default_profile and
                                   len(settings.profiles) > 1 else "")
            menu.add_item(text, lambda key=profile.id: self.new_tab_with_profile(key), icon="tab-new-symbolic",
                          shortcut=f"Ctrl+Shift+{number}" if number <= 9 else None)
        menu.addSeparator()
        menu.add_item("New window", self.new_window, shortcut="Ctrl+Shift+N")
        menu.add_item("Find", self.find, icon="edit-find-symbolic", shortcut="Ctrl+Shift+F",
                      enabled=self.current_pane() is not None)
        menu.add_item("Full screen", self.toggle_fullscreen, icon="view-fullscreen-symbolic", shortcut="F11",
                      checkable=True, checked=self.isFullScreen())
        menu.addSeparator()
        menu.add_item("Settings", self.open_settings, icon="preferences-system-symbolic", shortcut="Ctrl+,")
        menu.add_item(f"About {DISPLAY_NAME}", self.about, icon="help-about-symbolic")
        return menu

    def show_menu(self) -> None:
        self.menu = self.build_menu()
        self.menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.menu.show_at(self.menu_button)

    def build_tab_menu(self, index: int) -> fluent.MenuFlyout:
        menu = fluent.MenuFlyout(self)
        is_pane = 0 <= index < len(self.tabs) and isinstance(self.tabs[index], TerminalPane)
        if is_pane:
            menu.add_item("Rename tab", lambda: self.strip.start_rename(index), icon="document-edit-symbolic",
                          shortcut="F2")
            menu.add_item("Duplicate tab", lambda: self.duplicate_tab(index), icon="edit-copy-symbolic",
                          shortcut="Ctrl+Shift+D")
            menu.addSeparator()
        menu.add_item("Close tab", lambda: self.close_tab(index), icon="window-close-symbolic",
                      shortcut="Ctrl+Shift+W")
        menu.add_item("Close other tabs", lambda: self.close_other_tabs(index), enabled=len(self.tabs) > 1)
        return menu

    def _tab_menu(self, index: int, position) -> None:
        self.menu = self.build_tab_menu(index)
        self.menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.menu.popup(position)
