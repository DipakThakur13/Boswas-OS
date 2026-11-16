"""The calculator window: the header with the mode menu, the pages, History and Memory beside them."""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import APP_ID, DISPLAY_NAME, state, units
from ..engine import MODES, PROGRAMMER, SCIENTIFIC, STANDARD
from ..model import Calculator
from .calc_page import CalcPage
from .converter_page import ConverterPage
from .date_page import DatePage
from .navigation import NavigationOverlay
from .panel import HISTORY, RecordPanel
from .widgets import SUBTLE, KeyButton

TITLES = {STANDARD: "Standard", SCIENTIFIC: "Scientific", PROGRAMMER: "Programmer", state.DATE: "Date calculation"}
SHORTCUT_PAGES = (STANDARD, SCIENTIFIC, PROGRAMMER, state.DATE)       # Alt+1 ... Alt+4
WIDE = 640                    # from this width on History and Memory stand beside the keypad
PANEL_WIDTH = 300
HISTORY_ICON = "document-open-recent-symbolic"


def locale_marks() -> tuple[str, str]:
    """(decimal point, group separator) of the user's locale; a separator equal to the point is dropped."""
    locale = QLocale()
    point = locale.decimalPoint() or "."
    separator = locale.groupSeparator()
    if locale.numberOptions() & QLocale.NumberOption.OmitGroupSeparator or separator == point:
        separator = ""
    return point[:1], separator[:1]


class CalculatorWindow(fluent.FluentWindow):
    """The main window. ``state_directory`` is where the last mode is remembered (default: the state dir)."""

    def __init__(self, arguments: list[str] | None = None, *, state_directory: str | Path | None = None,
                 today: date | None = None) -> None:
        super().__init__(APP_ID, DISPLAY_NAME, icon="accessories-calculator", size=(360, 600),
                         minimum_size=(320, 500))
        self._state_directory = Path(state_directory) if state_directory else None
        self.state = state.load(self._directory()) if self._directory() is not None else state.defaults()
        for argument in arguments or []:
            if argument in state.PAGES:
                self.state["page"] = argument
        point, separator = locale_marks()
        self.calculator = Calculator(decimal_point=point, group_separator=separator)
        self.worker = fluent.Worker(self, max_threads=1)

        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(4, 4, 8, 4)
        header.setSpacing(4)
        self.menu_button = fluent.IconButton("open-menu-symbolic", "Open navigation", central, size=40)
        self.menu_button.clicked.connect(self.open_navigation)
        self.title_label = fluent.Label("", "subtitle", central)
        header.addWidget(self.menu_button)
        header.addWidget(self.title_label, 1)
        if fluent.has_icon(HISTORY_ICON):
            self.history_button: QWidget = fluent.IconButton(HISTORY_ICON, "History (Ctrl+H)", central, size=40)
        else:
            self.history_button = KeyButton("History", "history", SUBTLE, "History (Ctrl+H)", central)
            self.history_button.setSizePolicy(self.menu_button.sizePolicy())
            self.history_button.setFixedHeight(36)
        self.history_button.clicked.connect(self.toggle_history)
        header.addWidget(self.history_button)
        outer.addLayout(header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.stack = QStackedWidget(central)
        self.calc_page = CalcPage(self.calculator, self.state, self.stack)
        self.date_page = DatePage(self.state["date_mode"], self.stack, today=today)
        self.converter_page = ConverterPage(self.state["units"], self.stack, decimal_point=point,
                                            group_separator=separator)
        for page in (self.calc_page, self.date_page, self.converter_page):
            self.stack.addWidget(page)
        body.addWidget(self.stack, 1)
        self.panel = RecordPanel(self.calculator, central)
        self.panel.setFixedWidth(PANEL_WIDTH)
        body.addWidget(self.panel)
        outer.addLayout(body, 1)
        self.set_content(central)

        self.navigation = NavigationOverlay(central)
        self.navigation.add_section("Calculator")
        for key in (*MODES, state.DATE):
            self.navigation.add_item(key, TITLES[key])
        self.navigation.add_section("Converter")
        for category in units.CATEGORIES:
            self.navigation.add_item(category.key, category.name)
        self.navigation.chosen.connect(self.set_page)
        self.navigation.closed.connect(self._focus_page)

        self.calc_page.changed.connect(self.panel.refresh)
        self.calc_page.settings_changed.connect(self.save_state)
        self.panel.changed.connect(self.calc_page.refresh)
        self.panel.changed.connect(self.panel.refresh)
        self.date_page.mode_changed.connect(self._date_mode_changed)
        self.converter_page.units_changed.connect(lambda *_units: self.save_state())

        for index, key in enumerate(SHORTCUT_PAGES, start=1):
            self.add_shortcut(f"Alt+{index}", lambda k=key: self.set_page(k))
        self.add_shortcut("Ctrl+H", self.toggle_history)
        self.add_shortcut("Ctrl+Shift+D", self.clear_history)
        self.page = ""
        self.set_page(self.state["page"], save=False)

    # --- pages ---
    def _directory(self) -> Path | None:
        if self._state_directory is not None:
            return self._state_directory
        try:
            return fluent.state_dir(APP_ID)
        except OSError:
            return None

    def page_title(self) -> str:
        return self.title_label.text()

    def set_page(self, key: str, save: bool = True) -> None:
        """Show a calculator mode, the date calculation or a converter (the keys of the navigation)."""
        if key not in state.PAGES:
            raise ValueError(f"unknown page {key!r}")
        self.page = key
        self.state["page"] = key
        if key in MODES:
            self.calc_page.set_mode(key)
            self.stack.setCurrentWidget(self.calc_page)
            title = TITLES[key]
        elif key == state.DATE:
            self.stack.setCurrentWidget(self.date_page)
            title = TITLES[key]
        else:
            self.converter_page.set_category(key)
            self.stack.setCurrentWidget(self.converter_page)
            title = units.category(key).name
        self.title_label.setText(title)
        self.setWindowTitle(f"{title} - {DISPLAY_NAME}")
        self.navigation.set_current(key)
        self.panel.refresh()
        self._sync_layout()
        self._focus_page()
        if save:
            self.save_state()

    def _focus_page(self) -> None:
        if self.page in MODES:
            self.calc_page.setFocus()
        elif self.page == state.DATE:
            self.date_page.from_edit.setFocus()
        else:
            self.converter_page.focus_entry()

    def _date_mode_changed(self, mode: str) -> None:
        self.state["date_mode"] = mode
        self.save_state()

    def is_wide(self) -> bool:
        return self.width() >= WIDE

    def _sync_layout(self) -> None:
        calculator = self.page in MODES
        wide = self.is_wide()
        self.panel.setVisible(calculator and wide)
        self.calc_page.set_narrow(not wide)
        self.history_button.setVisible(calculator and not wide and self.calculator.has_history())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "panel"):
            self._sync_layout()
            if self.navigation.is_open():
                self.navigation.setGeometry(self.centralWidget().rect())

    # --- navigation, history ---
    def open_navigation(self) -> None:
        self.navigation.set_current(self.page)
        self.navigation.open_pane()

    def toggle_history(self) -> None:
        """Ctrl+H: show the history (of the modes that have one)."""
        if self.page not in MODES or not self.calculator.has_history():
            return
        if self.is_wide():
            self.panel.show_tab(HISTORY)
            self.panel.pivot.item(HISTORY).setFocus()
        else:
            self.calc_page.toggle_sheet(HISTORY)

    def clear_history(self) -> None:
        self.calculator.clear_history()
        self.panel.refresh()
        self.calc_page.refresh()

    # --- keyboard ---
    def keyPressEvent(self, event) -> None:
        if self.navigation.is_open():
            return
        page = self.calc_page if self.page in MODES else (self.converter_page if self.page != state.DATE else None)
        if page is not None and page.handle_key(event.key(), event.text(), event.modifiers()):
            return
        super().keyPressEvent(event)

    # --- remembering ---
    def save_state(self) -> None:
        """Write the remembered choices without holding up the window."""
        directory = self._directory()
        if directory is not None:
            self.worker.submit(state.save, directory, copy.deepcopy(self.state))

    def closeEvent(self, event) -> None:
        directory = self._directory()
        self.worker.shutdown()
        if directory is not None:
            state.save(directory, self.state)
        super().closeEvent(event)
