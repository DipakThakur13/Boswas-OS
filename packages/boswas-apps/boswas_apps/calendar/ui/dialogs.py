"""The dialogs of Calendar: an event with its repeat rule and reminders, the scope question for a series,
a calendar's name and colour, and the preview of a file before it is imported."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

from PySide6.QtCore import QDate, QTime, Qt, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QComboBox, QDateEdit, QFormLayout, QHBoxLayout,
                               QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QRadioButton, QScrollArea,
                               QTimeEdit, QVBoxLayout, QWidget)

import boswas_fluent as fluent

from ...clock.ui.widgets import DayPicker
from ...reminders import textfmt, timeutil
from .. import ical, model, recurrence, settings as settings_module
from .common import COLOR_NAMES, Context, calendar_colors, color_dot

UNITS = (("minutes", 1), ("hours", 60), ("days", 1440), ("weeks", 10080))


def to_qdate(day: dt.date) -> QDate:
    return QDate(day.year, day.month, day.day)


def from_qdate(day: QDate) -> dt.date:
    return dt.date(day.year(), day.month(), day.day())


def reminder_label(minutes: int, all_day: bool) -> str:
    """A lead time in words. For an all-day event it counts from midnight, so 9 in the morning of the day
    before is 15 hours: such times are named by their day."""
    if all_day and (minutes + 540) % 1440 == 0:
        days = (minutes + 540) // 1440
        if days == 0:
            return "On the day at 09:00"
        if days > 0:
            return f"{textfmt.plural(days // 7, 'week') if days % 7 == 0 else textfmt.plural(days, 'day')} " \
                   "before at 09:00"
    return textfmt.before_words(minutes)


class WideDialog(fluent.ContentDialog):
    MAX_WIDTH = 640


class RepeatEditor(QWidget):
    """The repeat rule of an event in the editor's terms; rules it cannot express are kept as they are."""

    changed = Signal()

    def __init__(self, context: Context, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._start = context.today()
        self._custom: recurrence.Rule | None = None
        self._original: tuple = ("", ())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.kind = QComboBox(self)
        self.kind.setAccessibleName("Repeat")
        for label, value in (("Never", ""), ("Daily", "daily"), ("Weekly", "weekly"), ("Monthly", "monthly"),
                             ("Yearly", "yearly")):
            self.kind.addItem(label, value)
        layout.addWidget(self.kind)
        self.details = QWidget(self)
        details = QVBoxLayout(self.details)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(8)
        every = QHBoxLayout()
        every.setSpacing(8)
        every.addWidget(fluent.Label("Every", "body", self.details))
        self.interval = fluent.NumberBox(self.details, value=1, minimum=1, maximum=999)
        self.interval.setAccessibleName("Repeat interval")
        self.interval.setFixedWidth(110)
        self.unit_label = fluent.Label("", "body", self.details)
        every.addWidget(self.interval)
        every.addWidget(self.unit_label)
        every.addStretch(1)
        details.addLayout(every)
        self.days = DayPicker(self.details, first_day=context.first_weekday())
        details.addWidget(self.days)
        self.monthly = QComboBox(self.details)
        self.monthly.setAccessibleName("Day of the month")
        details.addWidget(self.monthly)
        ends = QHBoxLayout()
        ends.setSpacing(8)
        ends.addWidget(fluent.Label("Ends", "body", self.details))
        self.end = QComboBox(self.details)
        self.end.setAccessibleName("End of the repetition")
        for label, value in (("Never", "never"), ("On a date", "until"), ("After a number of times", "count")):
            self.end.addItem(label, value)
        self.until = QDateEdit(self.details)
        self.until.setCalendarPopup(True)
        self.until.setDisplayFormat("d MMM yyyy")
        self.until.setAccessibleName("Last day")
        self.count = fluent.NumberBox(self.details, value=10, minimum=1, maximum=9999, suffix=" times")
        self.count.setAccessibleName("Number of times")
        self.count.setFixedWidth(130)
        ends.addWidget(self.end)
        ends.addWidget(self.until)
        ends.addWidget(self.count)
        ends.addStretch(1)
        details.addLayout(ends)
        layout.addWidget(self.details)
        self.summary = fluent.Label("", "caption", self, tone="secondary", wrap=True)
        layout.addWidget(self.summary)
        for signal in (self.kind.currentIndexChanged, self.monthly.currentIndexChanged, self.end.currentIndexChanged,
                       self.interval.valueChanged, self.count.valueChanged, self.until.dateChanged):
            signal.connect(self._sync)
        self.days.changed.connect(self._sync)
        self._sync()

    def set_start(self, start: dt.date) -> None:
        """The event's first day changed: the weekday and day-of-month choices follow it."""
        if start == self._start and self.monthly.count():
            return
        previous = self.monthly.currentData() or "date"
        self._start = start
        ordinal, weekday = recurrence.nth_weekday(start)
        blocked = self.monthly.blockSignals(True)
        self.monthly.clear()
        self.monthly.addItem(f"On day {start.day}", "date")
        if ordinal <= 4:
            self.monthly.addItem(f"On the {('first', 'second', 'third', 'fourth')[ordinal - 1]} "
                                 f"{textfmt.WEEKDAYS[weekday]}", "weekday")
        if recurrence.is_last_weekday(start):
            self.monthly.addItem(f"On the last {textfmt.WEEKDAYS[weekday]}", "last")
        self.monthly.setCurrentIndex(max(0, self.monthly.findData(previous)))
        self.monthly.blockSignals(blocked)
        if self.until.date() < to_qdate(start):
            self.until.setDate(to_qdate(start + dt.timedelta(days=30)))
        self._sync()

    def _state(self) -> tuple:
        return (self._start, self.kind.currentData(), self.interval.int_value(), tuple(self.days.days()),
                self.monthly.currentData(), self.end.currentData(), self.until.date(), self.count.int_value())

    def set_rule(self, text: str, start: dt.date) -> None:
        self._set_rule(text, start)
        # A rule that is not touched is given back letter for letter (an UNTIL may be more exact than a day).
        self._original = (text, self._state())

    def _set_rule(self, text: str, start: dt.date) -> None:
        self._custom = None
        self.set_start(start)
        rule = None
        if text:
            try:
                rule = recurrence.parse_rrule(text)
            except recurrence.RecurrenceError:
                rule = None
        index = self.kind.findData("custom")
        if index >= 0:
            self.kind.removeItem(index)
        if rule is None:
            self.kind.setCurrentIndex(0)
            return
        simple = recurrence.classify(rule, start)
        if simple.kind == "custom":
            self._custom = rule
            self.kind.addItem("Custom", "custom")
            self.kind.setCurrentIndex(self.kind.findData("custom"))
            self._sync()
            return
        self.kind.setCurrentIndex(self.kind.findData(simple.kind))
        self.interval.setValue(min(999, simple.interval))
        self.days.set_days(simple.weekdays or (start.weekday(),))
        self.monthly.setCurrentIndex(max(0, self.monthly.findData(simple.monthly)))
        if simple.count is not None:
            self.end.setCurrentIndex(self.end.findData("count"))
            self.count.setValue(min(9999, simple.count))
        elif simple.until is not None:
            self.end.setCurrentIndex(self.end.findData("until"))
            self.until.setDate(to_qdate(simple.until))
        else:
            self.end.setCurrentIndex(0)
        self._sync()

    def rule(self) -> recurrence.Rule | None:
        kind = self.kind.currentData()
        if not kind:
            return None
        if kind == "custom":
            return self._custom
        end = self.end.currentData()
        weekdays = tuple(self.days.days()) or (self._start.weekday(),)
        return recurrence.simple_rule(kind, self._start, interval=self.interval.int_value(), weekdays=weekdays,
                                      monthly=self.monthly.currentData() or "date",
                                      count=self.count.int_value() if end == "count" else None,
                                      until=max(from_qdate(self.until.date()), self._start) if end == "until"
                                      else None)

    def rule_text(self) -> str:
        if self._original[0] and self._state() == self._original[1]:
            return self._original[0]
        rule = self.rule()
        return recurrence.format_rrule(rule) if rule is not None else ""

    def _sync(self, *_args) -> None:
        kind = self.kind.currentData()
        simple = bool(kind) and kind != "custom"
        self.details.setVisible(simple)
        self.days.setVisible(kind == "weekly")
        self.monthly.setVisible(kind == "monthly")
        self.until.setVisible(self.end.currentData() == "until")
        self.count.setVisible(self.end.currentData() == "count")
        many = self.interval.int_value() != 1
        self.unit_label.setText({"daily": "days" if many else "day", "weekly": "weeks" if many else "week",
                                 "monthly": "months" if many else "month",
                                 "yearly": "years" if many else "year"}.get(kind, ""))
        if kind == "weekly" and not self.days.days():
            self.days.set_days((self._start.weekday(),))
        rule = self.rule()
        self.summary.setText(recurrence.describe(rule, self._start) if rule is not None else "")
        self.summary.setVisible(rule is not None)
        self.changed.emit()


class ReminderEditor(QWidget):
    """The reminders of an event: chips that can be removed, and a box to add one."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._all_day = False
        self._minutes: list[int] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        layout.addLayout(self.chips)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.add = QComboBox(self)
        self.add.setAccessibleName("Add a reminder")
        self.add.activated.connect(self._chosen)
        row.addWidget(self.add, 1)
        self.amount = fluent.NumberBox(self, value=45, minimum=1, maximum=999)
        self.amount.setAccessibleName("Custom reminder: how long before")
        self.amount.setFixedWidth(100)
        self.unit = QComboBox(self)
        self.unit.setAccessibleName("Custom reminder: unit")
        for label, minutes in UNITS:
            self.unit.addItem(f"{label} before", minutes)
        self.add_custom = fluent.Button("Add", self)
        self.add_custom.clicked.connect(self._add_custom)
        for widget in (self.amount, self.unit, self.add_custom):
            row.addWidget(widget)
            widget.hide()
        layout.addLayout(row)
        self._fill()

    def _fill(self) -> None:
        self.add.clear()
        self.add.addItem("Add a reminder…", None)
        choices = settings_module.ALL_DAY_REMINDER_CHOICES if self._all_day else settings_module.REMINDER_CHOICES
        for minutes in choices:
            if minutes is not None and minutes not in self._minutes:
                self.add.addItem(reminder_label(minutes, self._all_day), minutes)
        self.add.addItem("Custom…", "custom")
        self.add.setEnabled(len(self._minutes) < model.MAX_REMINDERS)

    def _rebuild(self) -> None:
        while self.chips.count():
            item = self.chips.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for minutes in self._minutes:
            chip = fluent.TagChip(reminder_label(minutes, self._all_day), self, closable=True)
            chip.setAccessibleName(f"Reminder {reminder_label(minutes, self._all_day)}; Delete removes it")
            chip.removed.connect(lambda m=minutes: self._remove(m))
            self.chips.addWidget(chip)
        if not self._minutes:
            self.chips.addWidget(fluent.Label("No reminder", "body", self, tone="secondary"))
        self.chips.addStretch(1)
        self._fill()

    def set_all_day(self, all_day: bool) -> None:
        if all_day != self._all_day:
            self._all_day = all_day
            self._rebuild()

    def set_minutes(self, minutes) -> None:
        self._minutes = list(model.clean_reminders(list(minutes)))
        self._rebuild()

    def minutes(self) -> tuple[int, ...]:
        return model.clean_reminders(self._minutes)

    def _remove(self, minutes: int) -> None:
        self._minutes = [value for value in self._minutes if value != minutes]
        self._rebuild()

    def _chosen(self, index: int) -> None:
        value = self.add.itemData(index)
        custom = value == "custom"
        for widget in (self.amount, self.unit, self.add_custom):
            widget.setVisible(custom)
        if custom:
            self.amount.setFocus()
        elif value is not None:
            self.set_minutes([*self._minutes, int(value)])

    def _add_custom(self) -> None:
        minutes = self.amount.int_value() * int(self.unit.currentData())
        if abs(minutes) <= model.MAX_REMINDER_MINUTES:
            self.set_minutes([*self._minutes, minutes])
        for widget in (self.amount, self.unit, self.add_custom):
            widget.hide()


class EventDialog(WideDialog):
    """An event as the user edits it. ``edited_event()`` gives the result; it is None while something is wrong."""

    def __init__(self, parent: QWidget, context: Context, event: model.Event, *, new: bool,
                 series: bool = False) -> None:
        super().__init__(parent, "New event" if new else "Edit event", primary="Save",
                         secondary=None if new else "Delete", close="Cancel")
        self.context = context
        self._event = event
        self._series = series
        store = context.store
        form_holder = QWidget(self)
        form = QFormLayout(form_holder)
        form.setContentsMargins(0, 0, 8, 0)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(16)
        self.title_edit = QLineEdit(form_holder)
        self.title_edit.setMaxLength(model.MAX_SUMMARY)
        self.title_edit.setPlaceholderText("Add a title")
        self.title_edit.setAccessibleName("Title")
        self.title_edit.setText(event.summary)
        self.calendar = QComboBox(form_holder)
        self.calendar.setAccessibleName("Calendar")
        for calendar in store.data.calendars:
            self.calendar.addItem(color_dot(calendar.color), calendar.name, calendar.id)
        self.calendar.setCurrentIndex(max(0, self.calendar.findData(event.calendar)))
        self.all_day = fluent.ToggleSwitch("All day", form_holder, checked=event.all_day, show_state=False)
        self.start_date = QDateEdit(form_holder)
        self.end_date = QDateEdit(form_holder)
        self.start_time = QTimeEdit(form_holder)
        self.end_time = QTimeEdit(form_holder)
        for edit, name in ((self.start_date, "Start date"), (self.end_date, "End date")):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("ddd d MMM yyyy")
            edit.setAccessibleName(name)
        for edit, name in ((self.start_time, "Start time"), (self.end_time, "End time")):
            edit.setDisplayFormat(context.edit_format())
            edit.setAccessibleName(name)
        start, end = event.start_dt, event.end_dt
        if event.all_day:
            end = end - model.DAY                       # the editor shows the last day, not the day after
            default = dt.time(max(0, min(22, store.settings.work_start + 1)))
            start_time, end_time = default, (dt.datetime.combine(start.date(), default)
                                             + dt.timedelta(minutes=store.settings.default_length)).time()
        else:
            start_time, end_time = start.time(), end.time()
        self._length = event.end_dt - event.start_dt
        self.start_date.setDate(to_qdate(start.date()))
        self.end_date.setDate(to_qdate(max(end.date(), start.date())))
        self.start_time.setTime(QTime(start_time.hour, start_time.minute))
        self.end_time.setTime(QTime(end_time.hour, end_time.minute))
        self.zone = QComboBox(form_holder)
        self.zone.setAccessibleName("Time zone")
        self.zone.setEditable(True)
        self.zone.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        local_name = timeutil.zone_key(context.zone())
        self.zone.addItem("Same local time everywhere", "")
        self.zone.addItem("UTC", "UTC")
        for name in timeutil.zone_names():
            if name != "UTC":
                self.zone.addItem(name.replace("_", " "), name)
        chosen = event.tz if (event.tz or not new) else (local_name if timeutil.zone(local_name) else "")
        self.zone.setCurrentIndex(max(0, self.zone.findData(chosen)))
        self.repeat = RepeatEditor(context, form_holder)
        self.reminders = ReminderEditor(form_holder)
        self.location = QLineEdit(form_holder)
        self.location.setMaxLength(model.MAX_LOCATION)
        self.location.setPlaceholderText("Add a place")
        self.location.setAccessibleName("Location")
        self.location.setText(event.location)
        self.notes = QPlainTextEdit(form_holder)
        self.notes.setAccessibleName("Notes")
        self.notes.setPlaceholderText("Add notes")
        self.notes.setPlainText(event.description)
        self.notes.setFixedHeight(84)
        self.notes.setTabChangesFocus(True)
        start_row, end_row = QHBoxLayout(), QHBoxLayout()
        for row, date_edit, time_edit in ((start_row, self.start_date, self.start_time),
                                          (end_row, self.end_date, self.end_time)):
            row.setSpacing(8)
            row.addWidget(date_edit, 1)
            row.addWidget(time_edit, 1)
        self.problem = fluent.Label("", "caption", form_holder, tone="danger", wrap=True)
        form.addRow("Title", self.title_edit)
        form.addRow("Calendar", self.calendar)
        form.addRow("All day", self.all_day)
        form.addRow("Starts", start_row)
        form.addRow("Ends", end_row)
        form.addRow("", self.problem)
        self.zone_label = fluent.Label("Time zone", "body", form_holder)
        form.addRow(self.zone_label, self.zone)
        self.repeat_label = fluent.Label("Repeat", "body", form_holder)
        form.addRow(self.repeat_label, self.repeat)
        form.addRow("Reminder", self.reminders)
        form.addRow("Location", self.location)
        form.addRow("Notes", self.notes)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(form_holder)
        self.scroll.setMinimumHeight(max(260, min(500, parent.window().height() - 250)))
        self.set_content(self.scroll)
        self.reminders.set_all_day(event.all_day)
        self.reminders.set_minutes(event.reminders)
        self.repeat.set_rule(event.rrule, start.date())
        if event.recurrence_id and not series:
            # One changed occurrence of a series has no rule of its own.
            self.repeat.hide()
            self.repeat_label.hide()
        self.all_day.toggled.connect(self._all_day_toggled)
        self.start_date.dateChanged.connect(self._start_changed)
        self.start_time.timeChanged.connect(self._start_changed)
        self.end_date.dateChanged.connect(self._check)
        self.end_time.timeChanged.connect(self._check)
        self._all_day_toggled(event.all_day)

    def open(self) -> None:
        super().open()
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def _all_day_toggled(self, on: bool) -> None:
        for widget in (self.start_time, self.end_time, self.zone, self.zone_label):
            widget.setVisible(not on)
        self.reminders.set_all_day(on)
        if on:
            self.reminders.set_minutes([m for m in self.reminders.minutes()
                                        if m in settings_module.ALL_DAY_REMINDER_CHOICES or m % 1440 == 0 or m < 0])
        self._check()

    def _times(self) -> tuple[dt.datetime, dt.datetime]:
        start_day, end_day = from_qdate(self.start_date.date()), from_qdate(self.end_date.date())
        if self.all_day.isChecked():
            return (dt.datetime.combine(start_day, dt.time()), dt.datetime.combine(end_day, dt.time()) + model.DAY)
        start_time, end_time = self.start_time.time(), self.end_time.time()
        return (dt.datetime.combine(start_day, dt.time(start_time.hour(), start_time.minute())),
                dt.datetime.combine(end_day, dt.time(end_time.hour(), end_time.minute())))

    def _start_changed(self, *_args) -> None:
        """A new start keeps the length: the end moves with it."""
        start, _end = self._times()
        length = self._length
        if self.all_day.isChecked():
            last = start + max(length, model.DAY) - model.DAY
            self.end_date.setDate(to_qdate(last.date()))
        else:
            if length >= model.DAY and self._event.all_day:
                length = dt.timedelta(minutes=self.context.store.settings.default_length)
            end = start + length
            self.end_date.setDate(to_qdate(end.date()))
            self.end_time.setTime(QTime(end.hour, end.minute))
        self.repeat.set_start(start.date())
        self._check()

    def _check(self, *_args) -> None:
        start, end = self._times()
        if end < start or (self.all_day.isChecked() and end <= start):
            self.problem.setText("The event ends before it starts.")
            self.problem.show()
            self.primary_button.setEnabled(False)
        else:
            self._length = end - start
            self.problem.hide()
            self.primary_button.setEnabled(True)

    def edited_event(self) -> model.Event | None:
        start, end = self._times()
        all_day = self.all_day.isChecked()
        if end < start or (all_day and end <= start):
            return None
        index = self.zone.findText(self.zone.currentText(), Qt.MatchFlag.MatchFixedString)
        zone = self.zone.itemData(index if index >= 0 else self.zone.currentIndex())
        raw = self._event.to_json()
        raw.update(summary=self.title_edit.text(), calendar=self.calendar.currentData(), all_day=all_day,
                   start=model.format_local(start.date() if all_day else start),
                   end=model.format_local(end.date() if all_day else end), tz="" if all_day else (zone or ""),
                   rrule="" if self.repeat.isHidden() else self.repeat.rule_text(),
                   reminders=list(self.reminders.minutes()), location=self.location.text(),
                   description=self.notes.toPlainText())
        edited = model.make_event(raw)
        if edited is not None and self._event.recurrence_id:
            edited = replace(edited, recurrence_id=self._event.recurrence_id)
        return edited


class ScopeDialog(fluent.ContentDialog):
    """Which occurrences of a series an edit or a deletion is for."""

    def __init__(self, parent: QWidget, deleting: bool) -> None:
        super().__init__(parent, "Delete repeating event" if deleting else "Change repeating event",
                         "This event is part of a series.", primary="Delete" if deleting else "Save", close="Cancel")
        holder = QWidget(self)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QRadioButton] = {}
        for scope, label in (("this", "This event"), ("following", "This and following events"),
                             ("all", "All events")):
            button = QRadioButton(label, holder)
            self.group.addButton(button)
            self.buttons[scope] = button
            layout.addWidget(button)
        self.buttons["this"].setChecked(True)
        self.set_content(holder)

    def open(self) -> None:
        super().open()
        self.buttons["this"].setFocus()

    def scope(self) -> str:
        return next(scope for scope, button in self.buttons.items() if button.isChecked())


class _Swatch(QAbstractButton):
    def __init__(self, color: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.color = color
        self.setCheckable(True)
        self.setFixedSize(32, 32)
        self.setToolTip(COLOR_NAMES[color])
        self.setAccessibleName(COLOR_NAMES[color])
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(calendar_colors(self.color)[1])
        painter.drawEllipse(self.rect().adjusted(5, 5, -5, -5))
        if self.isChecked():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(fluent.qcolor("text"), 2))
            painter.drawEllipse(self.rect().adjusted(2, 2, -2, -2))


class CalendarDialog(fluent.ContentDialog):
    """The name and colour of a calendar."""

    def __init__(self, parent: QWidget, calendar: model.Calendar | None, suggested: str = "iris") -> None:
        super().__init__(parent, "Edit calendar" if calendar else "Add a calendar", primary="Save", close="Cancel")
        holder = QWidget(self)
        layout = QFormLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setVerticalSpacing(12)
        self.name = QLineEdit(holder)
        self.name.setMaxLength(80)
        self.name.setPlaceholderText("Calendar name")
        self.name.setAccessibleName("Calendar name")
        self.name.setText(calendar.name if calendar else "")
        self.name.textChanged.connect(lambda text: self.primary_button.setEnabled(bool(text.strip())))
        swatches = QHBoxLayout()
        swatches.setSpacing(4)
        self.group = QButtonGroup(self)
        self.swatches: dict[str, _Swatch] = {}
        for color in model.COLORS:
            swatch = _Swatch(color, holder)
            self.group.addButton(swatch)
            self.swatches[color] = swatch
            swatches.addWidget(swatch)
        swatches.addStretch(1)
        self.swatches[calendar.color if calendar else suggested].setChecked(True)
        layout.addRow("Name", self.name)
        layout.addRow("Colour", swatches)
        self.set_content(holder)
        self.primary_button.setEnabled(bool(self.name.text().strip()))

    def open(self) -> None:
        super().open()
        self.name.setFocus()

    def values(self) -> tuple[str, str]:
        color = next(color for color, swatch in self.swatches.items() if swatch.isChecked())
        return self.name.text().strip(), color


class ImportDialog(WideDialog):
    """What a file holds, and the calendar its events go into."""

    NEW = "\0new"
    SHOWN = 200

    def __init__(self, parent: QWidget, context: Context, parsed: ical.Parsed, file_name: str) -> None:
        count = len({event.uid for event in parsed.events})
        super().__init__(parent, f"Import {textfmt.plural(count, 'event')}?", primary="Import", close="Cancel")
        self.parsed = parsed
        data = context.store.data
        existing = sum(1 for event in parsed.events if data.get(event.uid, event.recurrence_id) is not None)
        holder = QWidget(self)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        text = f"From {file_name}."
        if existing:
            text += f" {textfmt.plural(existing, 'event')} of these you already have; they are updated."
        layout.addWidget(fluent.Label(text, "body", holder, wrap=True))
        self.list = QListWidget(holder)
        self.list.setAccessibleName("Events in the file")
        self.list.setMinimumHeight(240)
        zone = context.zone()
        preview = model.CalendarData()
        preview.calendars.append(model.Calendar("preview", "preview"))
        masters = [replace(event, calendar="preview") for event in parsed.events if not event.recurrence_id]
        masters.sort(key=lambda event: event.start)
        for event in masters[:self.SHOWN]:
            start = event.start_value
            when = textfmt.short_date(start if not isinstance(start, dt.datetime) else start.date())
            if isinstance(start, dt.datetime):
                when += f", {context.time_text(start)}"
                if event.tz and event.tz != timeutil.zone_key(zone):
                    when += f" ({event.tz.replace('_', ' ')})"
            line = f"{when}    {event.title()}"
            if event.rrule:
                line += "  ·  repeats"
            self.list.addItem(QListWidgetItem(line))
        if len(masters) > self.SHOWN:
            more = QListWidgetItem(f"and {len(masters) - self.SHOWN} more")
            more.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(more)
        layout.addWidget(self.list, 1)
        notes = [f"{number} {reason}" for reason, number in parsed.skipped.items()] + list(parsed.warnings)
        if notes:
            layout.addWidget(fluent.Label("Left out or changed: " + "; ".join(notes) + ".", "caption", holder,
                                          tone="secondary", wrap=True))
        row = QHBoxLayout()
        row.addWidget(fluent.Label("Add to", "body", holder))
        self.target = QComboBox(holder)
        self.target.setAccessibleName("Calendar to import into")
        self.new_name = (parsed.name or file_name.rsplit(".", 1)[0] or "Imported")[:80]
        for calendar in data.calendars:
            self.target.addItem(color_dot(calendar.color), calendar.name, calendar.id)
        if len(data.calendars) < model.MAX_CALENDARS:
            self.target.addItem(f"New calendar “{self.new_name}”", self.NEW)
            if parsed.name:
                self.target.setCurrentIndex(self.target.count() - 1)     # a named calendar stays its own
        if self.target.currentData() != self.NEW:
            self.target.setCurrentIndex(max(0, self.target.findData(context.store.default_calendar())))
        row.addWidget(self.target, 1)
        layout.addLayout(row)
        self.set_content(holder)
        self.primary_button.setEnabled(bool(parsed.events))
        if not parsed.events:
            self.set_title("No events to import")

    def target_calendar(self) -> str:
        return self.target.currentData()
