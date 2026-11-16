"""The window of Calendar: the pane with the small month and the calendars, the bar, the views."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import replace
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
                               QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)
from PySide6.QtGui import QIcon, QKeySequence

import boswas_fluent as fluent
from boswas_fluent.shortcuts import key_sequences

from ...clock.ui.widgets import DayPicker
from ...reminders import client, textfmt, timeutil
from .. import APP_ID, ical, model, settings as settings_module
from ..store import CalendarStore
from .common import Context, color_dot
from .dialogs import (CalendarDialog, EventDialog, ImportDialog, ScopeDialog, reminder_label)
from .views import AgendaView, MiniMonth, MonthView, WeekView

PANE_WIDTH = 288        # the pane with the month and the calendars
COMPACT_BELOW = 900     # window width under which the pane folds away (half of a 1600 px screen is 800)
TITLE_MIN_WIDTH = 150   # room the bar keeps for "November 2026"

VERSION = "1.0"
VIEWS = settings_module.VIEWS
PRIMARY = fluent.ContentDialog.Result.PRIMARY
SECONDARY = fluent.ContentDialog.Result.SECONDARY
CAL_ROLE = Qt.ItemDataRole.UserRole + 1


class ReminderService:
    @staticmethod
    def ensure() -> str:
        return client.ensure_running()


def parse_arguments(arguments: list[str]) -> dict:
    """What a command line asks for: ``files`` to import, an event to ``show`` (with ``occurrence``), ``new``."""
    wanted: dict = {"files": [], "show": None, "occurrence": "", "new": False}
    position = 0
    while position < len(arguments):
        argument = arguments[position]
        if argument == "--new":
            wanted["new"] = True
        elif argument.startswith("--show="):
            wanted["show"] = argument.split("=", 1)[1]
        elif argument.startswith("--occurrence="):
            wanted["occurrence"] = argument.split("=", 1)[1]
        elif argument in ("--show", "--occurrence") and position + 1 < len(arguments):
            position += 1
            wanted[argument[2:]] = arguments[position]
        elif argument == "--":
            wanted["files"] += arguments[position + 1:]
            break
        elif not argument.startswith("--"):
            wanted["files"].append(argument)
        position += 1
    return wanted


def _write_bytes(path: str, data: bytes) -> int:
    target = Path(path)
    temporary = target.with_name(f".{target.name}.part")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return len(data)


class QuickCreate(fluent.Flyout):
    """The small popup that makes an event from a title."""

    def __init__(self, window: "CalendarWindow") -> None:
        super().__init__(window)
        self._window = window
        self.draft: model.Event | None = None
        self.when_label = fluent.Label("", "caption", self.surface, tone="secondary")
        self.title = QLineEdit(self.surface)
        self.title.setPlaceholderText("Add a title")
        self.title.setAccessibleName("Title of the new event")
        self.title.setMaxLength(model.MAX_SUMMARY)
        self.title.setMinimumWidth(280)
        self.calendar = QComboBox(self.surface)
        self.calendar.setAccessibleName("Calendar")
        buttons = QHBoxLayout()
        self.more_button = fluent.Button("More details", self.surface)
        self.save_button = fluent.Button("Save", self.surface, accent=True)
        buttons.addWidget(self.more_button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        self.body.addWidget(self.when_label)
        self.body.addWidget(self.title)
        self.body.addWidget(self.calendar)
        self.body.addLayout(buttons)
        self.title.returnPressed.connect(self.save)
        self.save_button.clicked.connect(self.save)
        self.more_button.clicked.connect(self.more)

    def start(self, draft: model.Event, text: str, position: QPoint) -> None:
        self.draft = draft
        self.when_label.setText(text)
        self.title.clear()
        self.calendar.clear()
        for calendar in self._window.store.data.calendars:
            self.calendar.addItem(color_dot(calendar.color), calendar.name, calendar.id)
        self.calendar.setCurrentIndex(max(0, self.calendar.findData(draft.calendar)))
        self.ensurePolished()
        self.adjustSize()
        self.move(position - QPoint(self._SHADOW, self._SHADOW))
        self.show()
        self.title.setFocus()

    def _result(self) -> model.Event | None:
        if self.draft is None:
            return None
        return replace(self.draft, summary=" ".join(self.title.text().split())[:model.MAX_SUMMARY],
                       calendar=self.calendar.currentData() or self.draft.calendar)

    def save(self) -> None:
        event = self._result()
        self.draft = None
        self.close()
        if event is not None:
            self._window.add_event(event)

    def more(self) -> None:
        event = self._result()
        self.draft = None
        self.close()
        if event is not None:
            self._window.edit_new(event)


class CalendarWindow(fluent.FluentWindow):
    def __init__(self, arguments: list[str] | None = None, *, store: CalendarStore | None = None,
                 now: Callable[[], float] | None = None, zone: Callable[[], dt.tzinfo] = timeutil.system_zone,
                 service=None) -> None:
        super().__init__(APP_ID, "Calendar", icon="office-calendar", size=(1180, 760), minimum_size=(640, 480))
        self.store = store if store is not None else CalendarStore()
        self.context = Context(self.store, now=now, zone=zone)
        self.service = service if service is not None else ReminderService()
        self.worker = fluent.Worker(self)
        self._undo_hint = False
        self._view = ""
        self._before_search = "month"
        root = QWidget(self)
        columns = QHBoxLayout(root)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(0)
        self._compact = False       # the window is too narrow for the pane beside the calendar
        self._pane_open = False     # compact only: the pane is shown in the calendar's place
        self._two_rows = False      # the view switch and the search box are on a row of their own
        self.pane = self._build_pane()
        self.main = self._build_main()
        columns.addWidget(self.pane)
        columns.addWidget(self.main, 1)
        self.set_content(root)
        self._reflow()
        self.quick = QuickCreate(self)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._search)
        for keys, slot in (("new", self.new_event), ("find", self.search.setFocus), ("undo", self.undo),
                           ("redo", self.redo), ("open", self.import_dialog),
                           ("save", lambda: self.export_calendars(None)), ("quit", self.close),
                           ("close", self.close), ("back", lambda: self.step(-1)), ("forward", lambda: self.step(1)),
                           ("Ctrl+PgUp", lambda: self.step(-1)), ("Ctrl+PgDown", lambda: self.step(1)),
                           ("Ctrl+T", self.go_today), ("preferences", self.show_settings),
                           ("fullscreen", lambda: self.showNormal() if self.isFullScreen()
                            else self.showFullScreen()),
                           ("Ctrl+Alt+1", lambda: self.set_view("day")),
                           ("Ctrl+Alt+2", lambda: self.set_view("workweek")),
                           ("Ctrl+Alt+3", lambda: self.set_view("week")),
                           ("Ctrl+Alt+4", lambda: self.set_view("month")),
                           ("Ctrl+Alt+5", lambda: self.set_view("agenda"))):
            self.add_shortcut(keys, slot)
        if not any(sequence.matches(QKeySequence("Ctrl+Y")) == QKeySequence.SequenceMatch.ExactMatch
                   for sequence in key_sequences("redo")):
            self.add_shortcut("Ctrl+Y", self.redo)           # the key Windows users know for it
        self.store.changed.connect(self._data_changed)
        self.store.settings_changed.connect(self._settings_changed)
        self.store.failed.connect(lambda message: self.report("error", message))
        self._fill_calendars()
        self.set_view(self.store.settings.first_view)
        if self.store.load_error:
            self.report("warning", self.store.load_error)
        QTimer.singleShot(0, self, self._check_service)
        if arguments:
            QTimer.singleShot(0, self, lambda: self.handle_arguments(list(arguments)))

    # --- building ---
    def _build_pane(self) -> QWidget:
        pane = QWidget(self)
        pane.setObjectName("fluentNavigationPane")
        pane.setFixedWidth(PANE_WIDTH)
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(12)
        self.pane_close_button = fluent.IconButton("open-menu-symbolic", "Back to the calendar", pane)
        self.pane_close_button.clicked.connect(lambda: self.set_pane_open(False))
        self.pane_close_button.hide()
        layout.addWidget(self.pane_close_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.new_button = fluent.Button("New event", pane, icon=fluent.icon("list-add-symbolic", color="on-accent"),
                                        accent=True)
        self.new_button.setToolTip("New event (Ctrl+N)")
        self.new_button.clicked.connect(lambda: self.new_event())
        layout.addWidget(self.new_button)
        self.mini = MiniMonth(self.context, pane)
        self.mini.date_selected.connect(self.go_to)
        layout.addWidget(self.mini, 0, Qt.AlignmentFlag.AlignHCenter)
        head = QHBoxLayout()
        head.addWidget(fluent.Label("Calendars", "body-strong", pane), 1)
        self.add_calendar_button = fluent.IconButton("list-add-symbolic", "Add a calendar", pane, size=28,
                                                     icon_size=14)
        self.add_calendar_button.clicked.connect(self.add_calendar)
        head.addWidget(self.add_calendar_button)
        layout.addLayout(head)
        self.calendar_list = QListWidget(pane)
        self.calendar_list.setAccessibleName("Calendars")
        self.calendar_list.setAccessibleDescription("Space shows or hides a calendar, F2 edits it, Delete deletes it")
        self.calendar_list.setFrameShape(QListWidget.Shape.NoFrame)
        self.calendar_list.itemChanged.connect(self._calendar_toggled)
        self.calendar_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.calendar_list.customContextMenuRequested.connect(self._calendar_menu)
        self.calendar_list.itemDoubleClicked.connect(lambda item: self.edit_calendar(item.data(CAL_ROLE)))
        fluent.add_shortcut(self.calendar_list, "rename", self._edit_current_calendar,
                            context=Qt.ShortcutContext.WidgetShortcut)
        fluent.add_shortcut(self.calendar_list, "Del", self._delete_current_calendar,
                            context=Qt.ShortcutContext.WidgetShortcut)
        layout.addWidget(self.calendar_list, 1)
        self.settings_button = fluent.Button("Settings", pane, icon="preferences-system-symbolic", subtle=True)
        self.settings_button.clicked.connect(self.show_settings)
        layout.addWidget(self.settings_button, 0, Qt.AlignmentFlag.AlignLeft)
        return pane

    def _build_main(self) -> QWidget:
        main = QWidget(self)
        layout = QVBoxLayout(main)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.bar = bar
        self.pane_button = fluent.IconButton("open-menu-symbolic", "Calendars and the month", main)
        self.pane_button.clicked.connect(lambda: self.set_pane_open(True))
        self.pane_button.hide()
        bar.addWidget(self.pane_button)
        self.today_button = fluent.Button("Today", main)
        self.today_button.setToolTip("Go to today (Ctrl+T)")
        self.today_button.clicked.connect(self.go_today)
        self.previous_button = fluent.IconButton("go-previous-symbolic", "Previous (Alt+Left)", main)
        self.next_button = fluent.IconButton("go-next-symbolic", "Next (Alt+Right)", main)
        self.previous_button.clicked.connect(lambda: self.step(-1))
        self.next_button.clicked.connect(lambda: self.step(1))
        self.title_label = fluent.Label("", "subtitle", main)
        self.search = fluent.SearchBox(main, placeholder="Search events")
        self.search.setMinimumWidth(110)
        self.search.setMaximumWidth(210)
        self.search.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.search.setToolTip("Search events (Ctrl+F)")
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        self.switch = fluent.SegmentedControl(main)
        for key in ("day", "workweek", "week", "month", "agenda"):
            self.switch.add_item(key, settings_module.VIEW_NAMES[key])
        self.switch.current_changed.connect(self.set_view)
        self.switch.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.more_button = fluent.IconButton("view-more-horizontal-symbolic", "More", main)
        self.more_button.clicked.connect(self._more_menu)
        self.new_compact_button = fluent.IconButton("list-add-symbolic", "New event (Ctrl+N)", main)
        self.new_compact_button.clicked.connect(lambda: self.new_event())
        self.new_compact_button.hide()
        for widget in (self.today_button, self.previous_button, self.next_button):
            bar.addWidget(widget)
        bar.addSpacing(6)
        self.title_label.setMinimumWidth(TITLE_MIN_WIDTH)
        bar.addWidget(self.title_label, 1)
        bar.addWidget(self.search)
        bar.addWidget(self.switch)
        bar.addWidget(self.new_compact_button)
        bar.addWidget(self.more_button)
        layout.addLayout(bar)
        # Where the view switch and the search box go when one row cannot hold everything.
        self.tools = QHBoxLayout()
        self.tools.setSpacing(6)
        self.tools.addStretch(1)
        layout.addLayout(self.tools)
        self.status = fluent.InfoBar("info", parent=main)
        self.status.hide()
        self.status.action_triggered.connect(self._banner_action)
        layout.addWidget(self.status)
        self.stack = QStackedWidget(main)
        self.month = MonthView(self.context, self.stack)
        self.week = WeekView(self.context, self.stack)
        self.agenda = AgendaView(self.context, self.stack)
        for view in (self.month, self.week):
            view.activated.connect(self.edit_occurrence)
            view.create_requested.connect(self._create_from_view)
            view.menu_requested.connect(self._event_menu)
            view.delete_requested.connect(self.delete_occurrence)
            view.day_requested.connect(self.open_day)
            view.moved.connect(self.move_occurrence)
            view.day_changed.connect(lambda _day: self._sync_title())
            self.stack.addWidget(view)
        self.agenda.activated.connect(self.edit_occurrence)
        self.agenda.menu_requested.connect(self._event_menu)
        self.agenda.delete_requested.connect(self.delete_occurrence)
        self.agenda.create_requested.connect(lambda start, end: self.new_event(start, end))
        self.stack.addWidget(self.agenda)
        self.settings_page = self._build_settings()
        self.stack.addWidget(self.settings_page)
        layout.addWidget(self.stack, 1)
        return main

    # --- the arrangement follows the window's width ---
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def set_pane_open(self, shown: bool) -> None:
        """Compact arrangement only: show the pane (the month and the calendars) in the calendar's place,
        or go back to the calendar."""
        shown = bool(shown) and self._compact
        if shown != self._pane_open:
            self._pane_open = shown
            self._reflow()
            (self.pane_close_button if shown else self.pane_button).setFocus()

    def _reflow(self) -> None:
        if getattr(self, "main", None) is None:     # a resize before the window is built
            return
        compact = self.width() < COMPACT_BELOW
        if compact != self._compact:
            self._compact = compact
            self._pane_open = False
        pane_shown = not compact or self._pane_open
        if compact:
            self.pane.setMinimumWidth(PANE_WIDTH)
            self.pane.setMaximumWidth(16777215)
        else:
            self.pane.setFixedWidth(PANE_WIDTH)
        self.pane.setVisible(pane_shown)
        self.main.setVisible(not compact or not self._pane_open)
        self.pane_close_button.setVisible(compact)
        self.pane_button.setVisible(compact)
        self.new_compact_button.setVisible(compact)
        self._arrange_bar(self._one_row_width() > self._main_width())

    def _main_width(self) -> int:
        """The width the bar has: the window's, less the pane beside it and the margins."""
        margins = self.main.layout().contentsMargins()
        return self.width() - (0 if self._compact else PANE_WIDTH) - margins.left() - margins.right()

    def _one_row_width(self) -> int:
        """What the bar needs to hold every control on one row."""
        widgets = [self.today_button, self.previous_button, self.next_button, self.switch, self.more_button]
        if self._compact:
            widgets += [self.pane_button, self.new_compact_button]
        spacing = self.bar.spacing()
        return (sum(widget.sizeHint().width() + spacing for widget in widgets) + 6 + TITLE_MIN_WIDTH + spacing
                + self.search.minimumWidth())

    def _arrange_bar(self, two_rows: bool) -> None:
        if two_rows == self._two_rows:
            return
        self._two_rows = two_rows
        for widget in (self.search, self.switch):
            self.bar.removeWidget(widget)
            self.tools.removeWidget(widget)
        if two_rows:
            self.tools.insertWidget(0, self.switch)
            self.tools.addWidget(self.search)
        else:
            index = self.bar.indexOf(self.new_compact_button)
            self.bar.insertWidget(index, self.search)
            self.bar.insertWidget(index + 1, self.switch)

    # --- hooks the tests replace ---
    def run_dialog(self, dialog: fluent.ContentDialog) -> fluent.ContentDialog.Result:
        return dialog.exec()

    def choose_open(self) -> str:
        return QFileDialog.getOpenFileName(self, "Import a calendar file", str(Path.home()),
                                           "Calendar files (*.ics *.ical *.ifb *.vcs);;All files (*)")[0]

    def choose_save(self, suggestion: str) -> str:
        return QFileDialog.getSaveFileName(self, "Export to a calendar file", str(Path.home() / suggestion),
                                           "Calendar files (*.ics)")[0]

    # --- messages ---
    def report(self, severity: str, message: str, *, undo: bool = False) -> None:
        self._undo_hint = undo
        self.status.setProperty("startService", False)
        self.status.set_action("Undo" if undo else None)
        self.status.show_message(severity, message)

    def _banner_action(self) -> None:
        if self._undo_hint:
            self.undo()
        elif self.status.property("startService"):
            self.status.dismiss()
            self._check_service()

    def _check_service(self) -> None:
        if self.service.ensure() == "unavailable":
            self.report("warning", "Reminders cannot be shown now, because the reminder service "
                        "(boswas-reminders) is not running and could not be started.")
            self.status.setProperty("startService", True)
            self.status.set_action("Try again")

    def undo(self) -> None:
        label = self.store.undo()
        if label:
            self.report("info", f"Undone: {label[0].lower()}{label[1:]}.")

    def redo(self) -> None:
        label = self.store.redo()
        if label:
            self.report("info", f"Done again: {label[0].lower()}{label[1:]}.")

    # --- views ---
    def current_view(self):
        return {"month": self.month, "agenda": self.agenda}.get(self._view, self.week)

    def anchor(self) -> dt.date:
        return self.current_view().anchor if self._view != "settings" else self.month.anchor

    def set_view(self, key: str) -> None:
        if key not in VIEWS:
            key = "month"
        day = self.anchor() if self._view else self.context.today()
        if self.agenda.query is not None and key != "agenda":
            self.search.clear()
            self.agenda.set_query(None)
        self._view = key
        view = self.current_view()
        if key in ("day", "workweek", "week"):
            self.week.set_mode(key)
        view.set_anchor(day)
        self.stack.setCurrentWidget(view)
        if self.switch.current() != key:
            self.switch.set_current(key)
        self._sync_title()

    def _sync_title(self) -> None:
        if self._view == "settings":
            self.title_label.setText("Settings")
            for widget in (self.previous_button, self.next_button):
                widget.setEnabled(False)            # there is no period to step through here
            self.today_button.setEnabled(True)
            return
        view = self.current_view()
        self.title_label.setText(view.title())
        days = view.shown_days()
        self.mini.set_selected(view.anchor, (days[0], days[-1]))
        searching = self.agenda.query is not None
        for widget in (self.today_button, self.previous_button, self.next_button):
            widget.setEnabled(not searching)

    def step(self, count: int) -> None:
        if self._view == "settings" or self.agenda.query is not None:
            return
        self.current_view().step(count)
        self._sync_title()

    def go_to(self, day: dt.date) -> None:
        self.set_pane_open(False)
        if self._view == "settings":
            self.set_view(self.store.settings.first_view)
        if self.agenda.query is not None:
            self.search.clear()
            self._search()
        self.current_view().set_anchor(day)
        self._sync_title()

    def go_today(self) -> None:
        self.go_to(self.context.today())

    def open_day(self, day: dt.date) -> None:
        self.set_view("day")
        self.go_to(day)

    def show_settings(self) -> None:
        self.set_pane_open(False)
        self._view = "settings"
        self.stack.setCurrentWidget(self.settings_page)
        self._sync_title()

    def _search(self) -> None:
        text = self.search.query()
        if text:
            if self.agenda.query is None and self._view != "settings":
                self._before_search = self._view
            self.agenda.set_query(text)
            self._view = "agenda"
            self.stack.setCurrentWidget(self.agenda)
        elif self.agenda.query is not None:
            self.agenda.set_query(None)
            self.set_view(self._before_search)
        self._sync_title()

    def _data_changed(self) -> None:
        self._fill_calendars()
        self._sync_title()

    def _settings_changed(self) -> None:
        for name in ("work_start", "work_end"):          # an end before the start was put right by the store
            box = self.setting_boxes[name]
            box.setCurrentIndex(max(0, box.findData(getattr(self.store.settings, name))))
        self._sync_title()

    # --- calendars ---
    def _fill_calendars(self) -> None:
        current = self.calendar_list.currentRow()
        blocked = self.calendar_list.blockSignals(True)
        self.calendar_list.clear()
        for calendar in self.store.data.calendars:
            item = QListWidgetItem(QIcon(color_dot(calendar.color)), calendar.name)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if calendar.visible else Qt.CheckState.Unchecked)
            item.setData(CAL_ROLE, calendar.id)
            item.setToolTip(f"{calendar.name}: {textfmt.plural(self.store.data.count_events(calendar.id), 'event')}")
            self.calendar_list.addItem(item)
        self.calendar_list.setCurrentRow(min(max(current, 0), self.calendar_list.count() - 1))
        self.calendar_list.blockSignals(blocked)
        self.add_calendar_button.setEnabled(len(self.store.data.calendars) < model.MAX_CALENDARS)

    def _calendar_toggled(self, item: QListWidgetItem) -> None:
        visible = item.checkState() == Qt.CheckState.Checked
        calendar = self.store.data.calendar(item.data(CAL_ROLE))
        if calendar is not None and calendar.visible != visible:
            self.store.data.update_calendar(calendar.id, visible=visible)
            self.store.touch()

    def _current_calendar(self) -> str | None:
        item = self.calendar_list.currentItem()
        return item.data(CAL_ROLE) if item is not None else None

    def _edit_current_calendar(self) -> None:
        if self._current_calendar():
            self.edit_calendar(self._current_calendar())

    def _delete_current_calendar(self) -> None:
        if self._current_calendar():
            self.delete_calendar(self._current_calendar())

    def _calendar_menu(self, position) -> None:
        item = self.calendar_list.itemAt(position)
        if item is None:
            return
        identifier = item.data(CAL_ROLE)
        menu = fluent.MenuFlyout(self)
        menu.add_item("Edit…", lambda: self.edit_calendar(identifier), icon="document-edit-symbolic", shortcut="F2")
        menu.add_item("Show only this calendar", lambda: self.show_only(identifier))
        menu.add_item("Export…", lambda: self.export_calendars(identifier), icon="document-save-symbolic")
        menu.addSeparator()
        menu.add_item("Delete", lambda: self.delete_calendar(identifier), icon="edit-delete-symbolic",
                      shortcut="Del", enabled=len(self.store.data.calendars) > 1)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(self.calendar_list.viewport().mapToGlobal(position))

    def show_only(self, identifier: str) -> None:
        for calendar in list(self.store.data.calendars):
            self.store.data.update_calendar(calendar.id, visible=calendar.id == identifier)
        self.store.touch()

    def add_calendar(self) -> None:
        used = {calendar.color for calendar in self.store.data.calendars}
        suggested = next((color for color in model.COLORS if color not in used), model.COLORS[0])
        dialog = CalendarDialog(self, None, suggested)
        result = self.run_dialog(dialog)
        name, color = dialog.values()
        dialog.deleteLater()
        if result != PRIMARY or not name:
            return
        try:
            self.store.data.add_calendar(name, color)
        except model.ModelError as error:
            self.report("warning", str(error))
            return
        self.store.touch()

    def edit_calendar(self, identifier: str) -> None:
        calendar = self.store.data.calendar(identifier)
        if calendar is None:
            return
        dialog = CalendarDialog(self, calendar)
        result = self.run_dialog(dialog)
        name, color = dialog.values()
        dialog.deleteLater()
        if result == PRIMARY and name:
            self.store.data.update_calendar(identifier, name=name, color=color)
            self.store.touch()

    def delete_calendar(self, identifier: str) -> None:
        data = self.store.data
        calendar = data.calendar(identifier)
        if calendar is None:
            return
        if len(data.calendars) <= 1:
            self.report("info", "The last calendar cannot be deleted.")
            return
        count = data.count_events(identifier)
        if count and not self.ask(f"Delete “{calendar.name}”?", f"The calendar and its "
                                  f"{textfmt.plural(count, 'event')} are deleted. You can undo this until you "
                                  "close Calendar.", "Delete"):
            return
        position = data.calendars.index(calendar)
        _calendar, events = data.remove_calendar(identifier)
        self.store.commit_action(f"Delete calendar “{calendar.name}”",
                                 lambda: data.restore_calendar(calendar, events, position),
                                 lambda: data.remove_calendar(identifier))
        self.report("info", f"Calendar “{calendar.name}” deleted.", undo=True)

    # --- events ---
    def _draft(self, start=None, end=None) -> model.Event:
        """A new event for the given times (dates: all day), with the defaults of the settings."""
        settings = self.store.settings
        length = dt.timedelta(minutes=settings.default_length)
        if start is None:
            start = self.anchor() if self._view != "settings" else self.context.today()
        if not isinstance(start, dt.datetime) and end is None:
            now = self.context.now_local()
            if start == now.date():
                minutes = min(23 * 60, ((now.hour * 60 + now.minute) // 30 + 1) * 30)     # the next half hour
                base = dt.datetime.combine(start, dt.time(minutes // 60, minutes % 60))
            else:
                base = dt.datetime.combine(start, dt.time(min(23, settings.work_start + 1)))
            start, end = base, base + length
        elif isinstance(start, dt.datetime) and end is None:
            end = start + length
        all_day = not isinstance(start, dt.datetime)
        reminders: tuple[int, ...] = ()
        if settings.default_reminder is not None:
            reminders = (-540,) if all_day else (settings.default_reminder,)
        zone_name = timeutil.zone_key(self.context.zone())
        return model.event_from_times(self.store.default_calendar(), start, end, reminders=reminders,
                                      tz=zone_name if timeutil.zone(zone_name) is not None else "")

    def _draft_text(self, draft: model.Event) -> str:
        start, end = draft.start_dt, draft.end_dt
        if draft.all_day:
            last = (end - model.DAY).date()
            text = textfmt.long_date(start.date())
            return f"{text} – {textfmt.long_date(last)}" if last > start.date() else f"{text}, all day"
        return (f"{textfmt.long_date(start.date())}, {self.context.time_text(start)} – "
                f"{self.context.time_text(end)}")

    def new_event(self, start=None, end=None) -> None:
        """Make an event in the full dialog (the button, Ctrl+N)."""
        try:
            self.edit_new(self._draft(start, end))
        except (model.ModelError, OverflowError) as error:
            self.report("warning", str(error))

    def _create_from_view(self, start, end) -> None:
        """A double click or a drag in a view: the small popup where the pointer is."""
        try:
            draft = self._draft(start, end)
        except (model.ModelError, OverflowError) as error:
            self.report("warning", str(error))
            return
        from PySide6.QtGui import QCursor
        view = self.current_view()
        position = QCursor.pos()
        if not view.rect().contains(view.mapFromGlobal(position)):
            position = view.mapToGlobal(view.rect().center())
        self.quick.start(draft, self._draft_text(draft), position)

    def edit_new(self, draft: model.Event) -> None:
        dialog = EventDialog(self, self.context, draft, new=True)
        result = self.run_dialog(dialog)
        event = dialog.edited_event()
        dialog.deleteLater()
        if result == PRIMARY and event is not None:
            self.add_event(event)

    def add_event(self, event: model.Event) -> None:
        try:
            change = self.store.data.add_event(event)
        except model.ModelError as error:
            self.report("warning", str(error))
            return
        self.store.commit(change)
        self._reveal(event.uid, "")

    def _reveal(self, uid: str, recurrence_id: str) -> None:
        """Choose an event in the view that is open (when it is on show there)."""
        occurrence = self.store.data.occurrence(uid, recurrence_id, self.context.zone())
        if occurrence is not None and self._view != "settings":
            self.current_view().select(occurrence)

    def _scope(self, occurrence: model.Occurrence, deleting: bool) -> str | None:
        """Which part of a series is meant; ``all`` for a single event; None when the user cancels."""
        if not occurrence.recurring or not occurrence.recurrence_id:
            return "all"
        if occurrence.is_override and not deleting:
            return "this"
        dialog = ScopeDialog(self, deleting)
        result = self.run_dialog(dialog)
        scope = dialog.scope()
        dialog.deleteLater()
        return scope if result == PRIMARY else None

    def edit_occurrence(self, occurrence: model.Occurrence) -> None:
        dialog = EventDialog(self, self.context, model.editable(occurrence), new=False)
        result = self.run_dialog(dialog)
        edited = dialog.edited_event()
        dialog.deleteLater()
        if result == SECONDARY:
            self.delete_occurrence(occurrence)
            return
        if result != PRIMARY or edited is None:
            return
        scope = self._scope(occurrence, False)
        if scope is None:
            return
        try:
            change = self.store.data.edit_occurrence(occurrence, edited, scope)
        except (model.ModelError, OverflowError, ValueError) as error:
            self.report("warning", str(error))
            return
        self.store.commit(change)

    def delete_occurrence(self, occurrence: model.Occurrence) -> None:
        scope = self._scope(occurrence, True)
        if scope is None:
            return
        try:
            change = self.store.data.delete_occurrence(occurrence, scope)
        except model.ModelError as error:
            self.report("warning", str(error))
            return
        self.store.commit(change)
        self.report("info", f"“{occurrence.event.title()}” deleted.", undo=True)

    def move_occurrence(self, occurrence: model.Occurrence, start: dt.datetime, end: dt.datetime) -> None:
        """A drag in a view: new wall times in the display zone, for this one occurrence."""
        event = occurrence.event
        zone = self.context.zone()
        try:
            if not event.all_day:
                start, end = model.local_to_event_zone(start, event, zone), model.local_to_event_zone(end, event, zone)
            change = self.store.data.move_occurrence(occurrence, start, end)
        except (model.ModelError, OverflowError, ValueError) as error:
            self.report("warning", str(error))
            return
        self.store.commit(change)
        self.report("info", f"“{event.title()}” moved.", undo=True)

    def _event_menu(self, occurrence: model.Occurrence, position) -> None:
        menu = fluent.MenuFlyout(self)
        menu.add_item("Open", lambda: self.edit_occurrence(occurrence), icon="document-edit-symbolic",
                      shortcut="Return")
        menu.add_item("Delete", lambda: self.delete_occurrence(occurrence), icon="edit-delete-symbolic",
                      shortcut="Del")
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(position)

    def _more_menu(self) -> None:
        menu = fluent.MenuFlyout(self)
        menu.add_item("Import…", self.import_dialog, icon="document-open-symbolic", shortcut="Ctrl+O")
        menu.add_item("Export all calendars…", lambda: self.export_calendars(None), icon="document-save-symbolic",
                      shortcut="Ctrl+S")
        menu.addSeparator()
        undo, redo = self.store.undo_label(), self.store.redo_label()
        menu.add_item(f"Undo {undo[0].lower()}{undo[1:]}" if undo else "Undo", self.undo, icon="edit-undo-symbolic",
                      shortcut="Ctrl+Z", enabled=bool(undo))
        menu.add_item(f"Redo {redo[0].lower()}{redo[1:]}" if redo else "Redo", self.redo, icon="edit-redo-symbolic",
                      shortcut="Ctrl+Y", enabled=bool(redo))
        menu.addSeparator()
        menu.add_item("Settings", self.show_settings, icon="preferences-system-symbolic", shortcut="Ctrl+,")
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.show_at(self.more_button)

    # --- files ---
    def handle_arguments(self, arguments: list[str]) -> None:
        wanted = parse_arguments(arguments)
        if wanted["show"]:
            self.show_event(wanted["show"], wanted["occurrence"])
        for path in wanted["files"][:20]:
            self.import_file(path)
        if wanted["new"]:
            self.new_event()

    def show_event(self, uid: str, recurrence_id: str = "") -> None:
        """Go to an event (the reminder service asks for this) and open it."""
        occurrence = self.store.data.occurrence(uid, recurrence_id, self.context.zone())
        if occurrence is None:
            self.report("info", "That event is no longer in your calendars.")
            return
        if self._view in ("settings", "agenda"):
            self.set_view("day")
        self.go_to(occurrence.start.date())
        self.current_view().select(occurrence)
        QTimer.singleShot(0, self, lambda: self.edit_occurrence(occurrence))

    def import_dialog(self) -> None:
        path = self.choose_open()
        if path:
            self.import_file(path)

    def import_file(self, path: str) -> None:
        if path.startswith("file://"):
            from PySide6.QtCore import QUrl
            path = QUrl(path).toLocalFile()
        name = os.path.basename(path)
        self.report("info", f"Reading {name}…")
        self.worker.submit(ical.read_file, path, on_done=lambda parsed: self._import_read(parsed, name),
                           on_error=lambda error: self.report("error", f"{name} was not imported. {error}"))

    def _import_read(self, parsed: ical.Parsed, name: str) -> None:
        self.status.dismiss()
        dialog = ImportDialog(self, self.context, parsed, name)
        result = self.run_dialog(dialog)
        target, new_name = dialog.target_calendar(), dialog.new_name
        dialog.deleteLater()
        if result != PRIMARY or not parsed.events:
            return
        data = self.store.data
        created = None
        try:
            if target == ImportDialog.NEW:
                created = data.add_calendar(new_name)
                target = created.id
            change = data.add_events([replace(event, calendar=target) for event in parsed.events],
                                     f"Import {name}")
        except model.ModelError as error:
            if created is not None:
                data.remove_calendar(created.id)
            self.report("warning", str(error))
            return
        self.store.commit(change)
        count = len({event.uid for event in parsed.events})
        calendar = data.calendar(target)
        self.report("success", f"{textfmt.plural(count, 'event')} imported into “{calendar.name}”.", undo=True)
        first = min((event.start for event in parsed.events), default="")
        upcoming = sorted(event.start for event in parsed.events
                          if event.start[:10] >= self.context.today().isoformat())
        try:
            self.go_to(model.as_datetime(model.parse_local(upcoming[0] if upcoming else first)).date())
        except ValueError:
            pass

    def export_calendars(self, identifier: str | None) -> None:
        data = self.store.data
        calendar = data.calendar(identifier) if identifier else None
        events = [event for event in data.events() if calendar is None or event.calendar == calendar.id]
        if not events:
            self.report("info", "There are no events to export.")
            return
        suggestion = (calendar.name if calendar else "Calendar").replace("/", "-").strip(". ") or "Calendar"
        path = self.choose_save(f"{suggestion}.ics")
        if not path:
            return
        if not path.lower().endswith((".ics", ".ical")):
            path += ".ics"
        name = calendar.name if calendar else ""
        count = len({event.uid for event in events})

        def work() -> int:
            return _write_bytes(path, ical.export(events, name=name))

        self.worker.submit(work, on_done=lambda _size: self.report(
            "success", f"{textfmt.plural(count, 'event')} exported to {os.path.basename(path)}."),
            on_error=lambda error: self.report("error", f"The file could not be written. "
                                               f"{getattr(error, 'strerror', None) or error}"))

    # --- settings ---
    def _build_settings(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(4)
        settings = self.store.settings
        self.setting_boxes: dict[str, QComboBox] = {}

        def combo(name: str, choices: list[tuple[str, object]]) -> QComboBox:
            box = QComboBox()
            for label, value in choices:
                box.addItem(label, value)
            box.setCurrentIndex(max(0, box.findData(getattr(settings, name))))
            box.activated.connect(lambda index, b=box: self.store.set_setting(name, b.itemData(index)))
            self.setting_boxes[name] = box
            return box

        back = fluent.Button("Back to the calendar", content, icon="go-previous-symbolic")
        back.clicked.connect(lambda: self.set_view(self.store.settings.first_view))
        self.settings_back = back
        layout.addWidget(back, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(8)
        days = [("As the system is set", -1)] + [(textfmt.WEEKDAYS[day], day) for day in (0, 6, 5)]
        layout.addWidget(fluent.SettingsCard("First day of the week", "", "x-office-calendar-symbolic",
                                             combo("week_start", days)))
        layout.addWidget(fluent.SettingsCard("Time format", "", "preferences-system-time-symbolic", combo(
            "time_format", [("As the system is set", "locale"), ("12-hour", "12"), ("24-hour", "24")])))
        layout.addWidget(fluent.SettingsCard("First view", "What Calendar shows when it opens", "view-grid-symbolic",
                                             combo("first_view", [(settings_module.VIEW_NAMES[key], key)
                                                                  for key in VIEWS])))
        reminders = [("None" if minutes is None else reminder_label(minutes, False), minutes)
                     for minutes in settings_module.REMINDER_CHOICES]
        layout.addWidget(fluent.SettingsCard("Default reminder", "For new events; all-day events are reminded on "
                                             "the day at 09:00", "notification-symbolic",
                                             combo("default_reminder", reminders)))
        lengths = [(textfmt.duration_words(minutes * 60), minutes)
                   for minutes in sorted({*settings_module.LENGTH_CHOICES, settings.default_length})]
        layout.addWidget(fluent.SettingsCard("Default length", "How long a new event is",
                                             "preferences-system-time-symbolic", combo("default_length", lengths)))
        span = QWidget()
        span_row = QHBoxLayout(span)
        span_row.setContentsMargins(0, 0, 0, 0)
        span_row.setSpacing(8)
        span_row.addWidget(combo("work_start", [(self.context.hour_text(hour), hour) for hour in range(0, 24)]))
        span_row.addWidget(fluent.Label("to", "body", span, tone="secondary"))
        span_row.addWidget(combo("work_end", [(self.context.hour_text(hour % 24), hour) for hour in range(1, 25)]))
        self.setting_boxes["work_start"].setAccessibleName("Working hours start")
        self.setting_boxes["work_end"].setAccessibleName("Working hours end")
        layout.addWidget(fluent.SettingsCard("Working hours", "Shown lighter in the week and day views",
                                             "display-brightness-symbolic", span))
        self.work_days = DayPicker(first_day=self.context.first_weekday())
        self.work_days.set_days(settings.work_days)
        self.work_days.changed.connect(lambda: self.store.set_setting("work_days", self.work_days.days())
                                       if self.work_days.days() else self.work_days.set_days(
                                           self.store.settings.work_days))
        layout.addWidget(fluent.SettingsCard("Working days", "The days of the work week view",
                                             "x-office-calendar-symbolic", self.work_days))
        self.default_calendar_box = QComboBox()
        self.default_calendar_box.activated.connect(lambda index: self.store.set_setting(
            "default_calendar", self.default_calendar_box.itemData(index)))
        layout.addWidget(fluent.SettingsCard("Default calendar", "Where new events go",
                                             "user-bookmarks-symbolic", self.default_calendar_box))
        layout.addWidget(fluent.SettingsCard("Calendar", f"Version {VERSION}. Events are kept on this computer; "
                                             "nothing is sent anywhere.", "help-about-symbolic"))
        layout.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(content)
        self.store.changed.connect(self._fill_default_calendar)
        self._fill_default_calendar()
        return area

    def _fill_default_calendar(self) -> None:
        box = self.default_calendar_box
        box.clear()
        for calendar in self.store.data.calendars:
            box.addItem(QIcon(color_dot(calendar.color)), calendar.name, calendar.id)
        box.setCurrentIndex(max(0, box.findData(self.store.default_calendar())))

    def closeEvent(self, event) -> None:
        super().closeEvent(event)
        if event.isAccepted():
            self.quick.close()
            self.worker.shutdown()
            self.store.close()
