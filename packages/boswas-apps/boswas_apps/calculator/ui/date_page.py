"""Date calculation: the difference between two dates, or a date plus or minus a span."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, QLocale, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDateEdit, QGridLayout, QHBoxLayout, QRadioButton, QScrollArea,
                               QVBoxLayout, QWidget)

import boswas_fluent as fluent

from .. import dates

DIFFERENCE, SHIFT = "difference", "shift"


def to_date(value: QDate) -> date:
    return date(value.year(), value.month(), value.day())


class DatePage(QScrollArea):
    """The Date calculation page. ``mode_changed`` when the user switches between its two calculations."""

    mode_changed = Signal(str)

    def __init__(self, mode: str = DIFFERENCE, parent: QWidget | None = None, *, today: date | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        today = today or date.today()
        start = QDate(today.year, today.month, today.day)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 8, 16, 16)
        layout.setSpacing(8)

        self.mode_box = QComboBox(content)
        self.mode_box.addItem("Difference between dates", DIFFERENCE)
        self.mode_box.addItem("Add or subtract days", SHIFT)
        self.mode_box.setAccessibleName("Calculation")
        layout.addWidget(self.mode_box, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(8)

        self.from_label = fluent.Label("From", "body", content)
        self.from_edit = self._date_edit(start, "From", content)
        layout.addWidget(self.from_label)
        layout.addWidget(self.from_edit)

        # --- difference ---
        self.difference_box = QWidget(content)
        box = QVBoxLayout(self.difference_box)
        box.setContentsMargins(0, 8, 0, 0)
        box.setSpacing(8)
        box.addWidget(fluent.Label("To", "body", self.difference_box))
        self.to_edit = self._date_edit(start, "To", self.difference_box)
        box.addWidget(self.to_edit)
        box.addSpacing(16)
        box.addWidget(fluent.Label("Difference", "body", self.difference_box, tone="secondary"))
        self.difference_label = fluent.Label("Same dates", "subtitle", self.difference_box, wrap=True,
                                             selectable=True)
        self.total_label = fluent.Label("", "body", self.difference_box, tone="secondary", selectable=True)
        box.addWidget(self.difference_label)
        box.addWidget(self.total_label)
        layout.addWidget(self.difference_box)

        # --- add or subtract ---
        self.shift_box = QWidget(content)
        box = QVBoxLayout(self.shift_box)
        box.setContentsMargins(0, 8, 0, 0)
        box.setSpacing(8)
        direction = QHBoxLayout()
        direction.setSpacing(24)
        self.add_radio = QRadioButton("Add", self.shift_box)
        self.subtract_radio = QRadioButton("Subtract", self.shift_box)
        self.add_radio.setChecked(True)
        direction.addWidget(self.add_radio)
        direction.addWidget(self.subtract_radio)
        direction.addStretch(1)
        box.addLayout(direction)
        spans = QGridLayout()
        spans.setHorizontalSpacing(12)
        spans.setVerticalSpacing(8)
        spans.setColumnStretch(2, 1)
        self.span_boxes: dict[str, fluent.NumberBox] = {}
        for row, name in enumerate(("Years", "Months", "Days")):
            number = fluent.NumberBox(self.shift_box, value=0, minimum=0, maximum=dates.MAX_SPAN, step=1, decimals=0)
            number.setAccessibleName(name)
            number.valueChanged.connect(lambda _value: self.recalculate())
            self.span_boxes[name.lower()] = number
            spans.addWidget(fluent.Label(name, "body", self.shift_box), row, 0)
            spans.addWidget(number, row, 1)
        box.addLayout(spans)
        box.addSpacing(16)
        box.addWidget(fluent.Label("Date", "body", self.shift_box, tone="secondary"))
        self.result_label = fluent.Label("", "subtitle", self.shift_box, wrap=True, selectable=True)
        box.addWidget(self.result_label)
        layout.addWidget(self.shift_box)
        layout.addStretch(1)
        self.setWidget(content)

        self.from_edit.dateChanged.connect(lambda _date: self.recalculate())
        self.to_edit.dateChanged.connect(lambda _date: self.recalculate())
        self.add_radio.toggled.connect(lambda _checked: self.recalculate())
        self.mode_box.currentIndexChanged.connect(self._mode_selected)
        self.set_mode(mode)

    def _date_edit(self, start: QDate, name: str, parent: QWidget) -> QDateEdit:
        edit = QDateEdit(start, parent)
        edit.setCalendarPopup(True)
        edit.setDateRange(QDate(dates.MIN_DATE.year, 1, 1), QDate(dates.MAX_DATE.year, 12, 31))
        edit.setDisplayFormat(QLocale().dateFormat(QLocale.FormatType.LongFormat))
        edit.setMinimumWidth(280)
        edit.setMaximumWidth(360)
        edit.setAccessibleName(name)
        return edit

    def mode(self) -> str:
        return self.mode_box.currentData()

    def set_mode(self, mode: str) -> None:
        index = self.mode_box.findData(mode if mode in (DIFFERENCE, SHIFT) else DIFFERENCE)
        if self.mode_box.currentIndex() != index:
            self.mode_box.setCurrentIndex(index)
        else:
            self._mode_selected(index)

    def _mode_selected(self, _index: int) -> None:
        difference = self.mode() == DIFFERENCE
        self.difference_box.setVisible(difference)
        self.shift_box.setVisible(not difference)
        self.recalculate()
        self.mode_changed.emit(self.mode())

    def recalculate(self) -> None:
        start = to_date(self.from_edit.date())
        if self.mode() == DIFFERENCE:
            span = dates.difference(start, to_date(self.to_edit.date()))
            self.difference_label.setText(dates.describe(span))
            total = dates.describe_total(span)
            self.total_label.setText(total)
            self.total_label.setVisible(bool(total))
            return
        try:
            result = dates.shift(start, years=self.span_boxes["years"].int_value(),
                                 months=self.span_boxes["months"].int_value(),
                                 days=self.span_boxes["days"].int_value(), subtract=self.subtract_radio.isChecked())
        except dates.DateRangeError:
            self.result_label.setText("Date out of bounds")
            return
        self.result_label.setText(QLocale().toString(QDate(result.year, result.month, result.day),
                                                     QLocale.FormatType.LongFormat))
        self.result_date = result
