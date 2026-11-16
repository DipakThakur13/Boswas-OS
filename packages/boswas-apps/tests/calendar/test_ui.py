"""Calendar's window, driven as a user drives it; the clock and the time zone are the test's."""

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

from boswas_fluent import testing

from PySide6.QtCore import QDate, QEvent, QPoint, QPointF, QTime, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import boswas_fluent as fluent  # noqa: E402
from boswas_apps.calendar import ical, model  # noqa: E402
from boswas_apps.calendar.store import CalendarStore  # noqa: E402
from boswas_apps.calendar.ui import views  # noqa: E402
from boswas_apps.calendar.ui.window import CalendarWindow, parse_arguments  # noqa: E402
from boswas_apps.reminders import jsonfile, timeutil  # noqa: E402

BERLIN = timeutil.zone("Europe/Berlin")
NOW = timeutil.to_epoch(dt.datetime(2026, 11, 17, 10, 40), BERLIN)
PRIMARY = fluent.ContentDialog.Result.PRIMARY
SECONDARY = fluent.ContentDialog.Result.SECONDARY
NONE = fluent.ContentDialog.Result.NONE
D = dt.datetime


def mouse(widget, kind, position, button=Qt.MouseButton.LeftButton, buttons=None):
    point = QPointF(position)
    held = button if buttons is None else buttons
    event = QMouseEvent(kind, point, widget.mapToGlobal(point), button, held, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


def drag(widget, start, end):
    start, end = QPointF(start), QPointF(end)
    mouse(widget, QEvent.Type.MouseButtonPress, start)
    for step in range(1, 5):
        mouse(widget, QEvent.Type.MouseMove, start + (end - start) * step / 4, Qt.MouseButton.NoButton,
              Qt.MouseButton.LeftButton)
    mouse(widget, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    testing.process()


def double_click(widget, position):
    mouse(widget, QEvent.Type.MouseButtonPress, position)
    mouse(widget, QEvent.Type.MouseButtonRelease, position, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    mouse(widget, QEvent.Type.MouseButtonDblClick, position)
    mouse(widget, QEvent.Type.MouseButtonRelease, position, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    testing.process()


class FakeService:
    state = "running"

    def ensure(self) -> str:
        return self.state


def ev(uid, start, end, calendar="personal", **fields) -> dict:
    return {"uid": uid, "calendar": calendar, "start": start, "end": end, "summary": fields.pop("summary", uid),
            **fields}


class CalendarCase(testing.FluentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.tmp / "calendar.json"
        self.answers: list = []
        self.asked: list = []
        self.confirm = True
        self.service = FakeService()

    def open(self, *events: dict, arguments=(), settings: dict | None = None) -> CalendarWindow:
        if events or not self.path.exists():
            jsonfile.write_json_atomic(self.path, {
                "calendars": [{"id": "personal", "name": "Personal", "color": "iris"},
                              {"id": "work", "name": "Work", "color": "azure"}], "events": list(events)})
        if settings is not None:
            jsonfile.write_json_atomic(self.tmp / "settings.json", settings)
        store = CalendarStore(self.path, self.tmp / "settings.json")
        window = CalendarWindow(list(arguments), store=store, now=lambda: NOW, zone=lambda: BERLIN,
                                service=self.service)
        window.run_dialog = self._answer
        window.ask = lambda title, text, confirm="OK", *args, **kwargs: (self.asked.append(title), self.confirm)[1]
        self.show(window, 1280, 800)
        testing.process(30)
        return window

    def _answer(self, dialog):
        self.assertTrue(self.answers, f"an unexpected dialog: {dialog.title_label.text()}")
        return self.answers.pop(0)(dialog)

    def saved(self, window: CalendarWindow) -> model.CalendarData:
        self.assertTrue(window.store.flush())
        return model.CalendarData.from_json(jsonfile.read_json(self.path))

    def shown(self, view, day: int | None = None) -> list[tuple]:
        return [(occ.start.day, occ.start.hour, occ.start.minute, occ.event.summary) for occ in view.occurrences()
                if day is None or occ.start.day == day]

    @staticmethod
    def cell(window: CalendarWindow, day: dt.date) -> QPointF:
        for week, days in enumerate(window.month.weeks()):
            if day in days:
                return window.month.cell_rect(week, days.index(day)).center() + QPointF(0, 30)
        raise AssertionError(f"{day} is not on show")

    @staticmethod
    def slot(window: CalendarWindow, day: dt.date, hour: float) -> QPointF:
        grid = window.week.grid
        column = window.week.shown_days().index(day)
        return QPointF(views.GUTTER + (column + 0.5) * grid.column_width(), hour * views.HOUR + 2)


class NavigationTest(CalendarCase):
    def test_views_titles_and_moving_about(self):
        window = self.open()
        self.assertEqual((window.switch.current(), window.title_label.text()), ("month", "November 2026"))
        self.assertIs(window.stack.currentWidget(), window.month)
        testing.click(window.next_button)
        self.assertEqual(window.title_label.text(), "December 2026")
        testing.key(window, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
        testing.key(window, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
        self.assertEqual(window.title_label.text(), "October 2026")
        testing.click(window.today_button)
        self.assertEqual((window.title_label.text(), window.month.anchor), ("November 2026", dt.date(2026, 11, 17)))
        testing.click(window.switch.item("week"))
        self.assertEqual((window.week.mode, window.title_label.text()), ("week", "16 – 22 November 2026"))
        self.assertEqual(len(window.week.shown_days()), 7)
        testing.key(window, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        self.assertEqual([day.weekday() for day in window.week.shown_days()], [0, 1, 2, 3, 4])
        window.set_view("day")
        self.assertEqual(window.title_label.text(), "Tuesday, 17 November 2026")
        testing.click(window.next_button)
        self.assertEqual(window.title_label.text(), "Wednesday, 18 November 2026")
        window.set_view("agenda")
        self.assertEqual(window.title_label.text(), "18 Nov – 17 Dec 2026")
        window.set_view("week")
        window.go_to(dt.date(2026, 12, 30))
        self.assertEqual(window.title_label.text(), "28 Dec 2026 – 3 Jan 2027")

    def test_the_small_month_picks_days_and_shows_week_numbers(self):
        window = self.open()
        mini = window.mini
        self.assertEqual(mini.selected, dt.date(2026, 11, 17))
        self.assertEqual(mini.shown, (dt.date(2026, 10, 26), dt.date(2026, 12, 6)))
        target = next(mini._rect(week, column).center() for week, days in enumerate(mini.weeks())
                      for column, day in enumerate(days) if day == dt.date(2026, 11, 25))
        testing.click(mini, target.toPoint())
        self.assertEqual(window.month.anchor, dt.date(2026, 11, 25))
        testing.click(mini.next_button)
        self.assertEqual(mini.month, dt.date(2026, 12, 1))
        self.assertEqual(window.title_label.text(), "November 2026")          # browsing does not move the view
        mini.setFocus()
        testing.key(mini, Qt.Key.Key_Down)
        self.assertEqual(window.month.anchor, dt.date(2026, 12, 2))
        testing.grab(window, self.tmp / "window.png")

    def test_keys_in_the_month(self):
        window = self.open(ev("a", "2026-11-18T09:00:00", "2026-11-18T10:00:00"),
                           ev("b", "2026-11-19T09:00:00", "2026-11-19T10:00:00"))
        month = window.month
        month.setFocus()
        testing.key(month, Qt.Key.Key_Right)
        testing.key(month, Qt.Key.Key_Down)
        self.assertEqual(month.anchor, dt.date(2026, 11, 25))
        testing.key(month, Qt.Key.Key_PageDown)
        self.assertEqual(window.title_label.text(), "December 2026")
        testing.key(month, Qt.Key.Key_Home)
        self.assertEqual(month.anchor, dt.date(2026, 11, 17))
        testing.key(month, Qt.Key.Key_Tab)                                    # through the events on show
        self.assertEqual(month.selected().event.uid, "a")
        testing.key(month, Qt.Key.Key_Tab)
        self.assertEqual(month.selected().event.uid, "b")
        testing.key(month, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(month.selected().event.uid, "a")
        testing.key(month, Qt.Key.Key_Escape)
        self.assertIsNone(month.selected())

    def test_settings_change_the_views(self):
        window = self.open(ev("a", "2026-11-17T15:00:00", "2026-11-17T16:00:00"),
                           settings={"first_view": "week", "week_start": 6, "time_format": "12"})
        self.assertEqual((window.switch.current(), window.week.shown_days()[0]), ("week", dt.date(2026, 11, 15)))
        self.assertEqual(window.context.hour_text(15), "3 PM")
        window.show_settings()
        self.assertEqual(window.title_label.text(), "Settings")
        box = window.setting_boxes["week_start"]
        box.setCurrentIndex(box.findData(0))
        box.activated.emit(box.currentIndex())
        box = window.setting_boxes["first_view"]
        box.setCurrentIndex(box.findData("day"))
        box.activated.emit(box.currentIndex())
        testing.click(window.work_days.buttons[5])
        testing.click(window.settings_back)
        self.assertEqual(window.switch.current(), "day")
        window.set_view("workweek")
        self.assertEqual([day.weekday() for day in window.week.shown_days()], [0, 1, 2, 3, 4, 5])
        window.close()
        saved = jsonfile.read_json(self.tmp / "settings.json")
        self.assertEqual((saved["week_start"], saved["first_view"], saved["work_days"]), (0, "day", [0, 1, 2, 3, 4, 5]))

    def test_a_missing_reminder_service_is_said(self):
        self.service.state = "unavailable"
        window = self.open()
        self.assertTrue(window.status.isVisible())
        self.assertIn("boswas-reminders", window.status.message())


class NarrowWindowTest(CalendarCase):
    """The window snapped to half a screen, and at its smallest: nothing overlaps, everything stays reachable."""

    def bar_widgets(self, window):
        return [widget for widget in (window.pane_button, window.today_button, window.previous_button,
                                      window.next_button, window.title_label, window.search, window.switch,
                                      window.new_compact_button, window.more_button) if widget.isVisible()]

    def assert_nothing_squeezed_or_overlapping(self, window):
        widgets = self.bar_widgets(window)
        for widget in widgets:
            # (A widget of fixed size, as the icon buttons are, needs that size and no more.)
            needed = min(widget.minimumWidth() or widget.minimumSizeHint().width(), widget.maximumWidth())
            self.assertGreaterEqual(widget.width(), needed, widget.toolTip() or type(widget))
            self.assertGreaterEqual(widget.x(), 0)
            self.assertLessEqual(widget.geometry().right(), window.main.width(), type(widget))
        for index, first in enumerate(widgets):
            for second in widgets[index + 1:]:
                self.assertFalse(first.geometry().intersects(second.geometry()),
                                 f"{type(first).__name__} overlaps {type(second).__name__}")

    def resized(self, window, width, height=800):
        window.resize(width, height)
        testing.process(30)
        return window

    def test_a_wide_window_has_the_pane_and_one_row(self):
        window = self.open()
        self.assertTrue(window.pane.isVisible() and window.main.isVisible())
        self.assertFalse(window.pane_button.isVisible() or window.new_compact_button.isVisible())
        self.assertEqual(window.search.y(), window.today_button.y() + (window.today_button.height()
                                                                         - window.search.height()) // 2)
        self.assert_nothing_squeezed_or_overlapping(window)

    def test_half_a_screen_folds_the_pane_away_and_keeps_every_control_whole(self):
        for width in (800, 683, 640):
            with self.subTest(width=width):
                window = self.resized(self.open(), width)
                self.assertFalse(window.pane.isVisible())
                self.assertTrue(window.main.isVisible())
                self.assertTrue(window.pane_button.isVisible() and window.new_compact_button.isVisible())
                self.assert_nothing_squeezed_or_overlapping(window)
                self.assertEqual(window.title_label.text(), "November 2026")
                window.close()

    def test_the_view_switch_and_the_search_box_take_a_second_row_when_one_cannot_hold_them(self):
        window = self.resized(self.open(), 640)
        self.assertGreater(window.switch.y(), window.today_button.geometry().bottom())
        self.assertGreater(window.search.y(), window.today_button.geometry().bottom())
        self.resized(window, 1280)
        self.assertLess(window.switch.y(), window.today_button.geometry().bottom())
        self.assert_nothing_squeezed_or_overlapping(window)

    def test_the_folded_pane_opens_in_the_calendar_s_place_and_a_date_goes_back(self):
        window = self.resized(self.open(), 800)
        testing.click(window.pane_button)
        self.assertTrue(window.pane.isVisible())
        self.assertFalse(window.main.isVisible())
        self.assertTrue(window.pane_close_button.isVisible() and window.new_button.isVisible())
        window.mini.date_selected.emit(dt.date(2026, 12, 3))
        testing.process()
        self.assertFalse(window.pane.isVisible())
        self.assertTrue(window.main.isVisible())
        self.assertEqual(window.title_label.text(), "December 2026")
        testing.click(window.pane_button)
        testing.click(window.pane_close_button)
        self.assertTrue(window.main.isVisible())
        self.assertFalse(window.pane.isVisible())

    def test_a_new_event_is_one_click_away_while_the_pane_is_folded(self):
        window = self.resized(self.open(), 800)
        seen = []
        self.answers.append(lambda dialog: (seen.append(dialog.title_label.text()), NONE)[1])
        testing.click(window.new_compact_button)
        self.assertEqual(len(seen), 1)

    def test_widening_again_restores_the_pane_beside_the_calendar(self):
        window = self.resized(self.open(), 800)
        testing.click(window.pane_button)
        self.resized(window, 1280)
        self.assertTrue(window.pane.isVisible() and window.main.isVisible())
        self.assertEqual(window.pane.width(), 288)
        self.assertFalse(window.pane_button.isVisible() or window.pane_close_button.isVisible())


class EventsTest(CalendarCase):
    def test_ctrl_n_makes_an_event_with_the_defaults(self):
        window = self.open()

        def fill(dialog):
            self.assertEqual(dialog.title_label.text(), "New event")
            self.assertEqual((dialog.start_date.date(), dialog.start_time.time(), dialog.end_time.time()),
                             (QDate(2026, 11, 17), QTime(11, 0), QTime(12, 0)))      # the next half hour, one hour
            self.assertEqual(dialog.zone.currentData(), "Europe/Berlin")
            self.assertEqual(dialog.reminders.minutes(), (15,))
            testing.type_text(dialog.title_edit, "Dentist")
            dialog.location.setText("Main St 1")
            dialog.start_time.setTime(QTime(15, 30))
            self.assertEqual(dialog.end_time.time(), QTime(16, 30))                   # the end follows the start
            dialog.end_time.setTime(QTime(15, 0))
            self.assertFalse(dialog.primary_button.isEnabled())                       # ends before it starts
            self.assertFalse(dialog.problem.isHidden())
            dialog.end_time.setTime(QTime(16, 15))
            dialog.reminders.set_minutes([60, 1440])
            return PRIMARY
        self.answers.append(fill)
        testing.key(window, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        event, = self.saved(window).events()
        self.assertEqual((event.summary, event.location, event.start, event.end, event.tz, event.reminders,
                          event.calendar, event.all_day),
                         ("Dentist", "Main St 1", "2026-11-17T15:30:00", "2026-11-17T16:15:00", "Europe/Berlin",
                          (1440, 60), "personal", False))
        self.assertTrue(event.uid.endswith("@boswas-calendar"))
        self.assertGreater(event.created, 0)
        self.assertEqual(self.shown(window.month, 17), [(17, 15, 30, "Dentist")])
        self.assertEqual(window.month.selected().event.uid, event.uid)

    def test_all_day_and_several_days(self):
        window = self.open()

        def fill(dialog):
            dialog.title_edit.setText("Trip")
            testing.click(dialog.all_day)
            self.assertTrue(dialog.start_time.isHidden())
            self.assertTrue(dialog.zone.isHidden())
            dialog.end_date.setDate(QDate(2026, 11, 20))
            dialog.calendar.setCurrentIndex(dialog.calendar.findData("work"))
            self.assertEqual(dialog.reminders.add.itemText(1), "On the day at 09:00")
            return PRIMARY
        self.answers.append(fill)
        testing.click(window.new_button)
        event, = self.saved(window).events()
        self.assertEqual((event.all_day, event.start, event.end, event.tz, event.calendar),
                         (True, "2026-11-17", "2026-11-21", "", "work"))
        occurrence, = window.month.occurrences()
        self.assertEqual((occurrence.start, occurrence.last_day), (D(2026, 11, 17), dt.date(2026, 11, 20)))
        self.assertGreater(window.month.rect_of(occurrence).width(), 3 * window.month.cell_rect(0, 0).width())

    def test_double_click_in_the_month_asks_only_for_a_title(self):
        window = self.open()
        double_click(window.month, self.cell(window, dt.date(2026, 11, 19)))
        quick = window.quick
        self.assertTrue(quick.isVisible())
        self.assertEqual(quick.when_label.text(), "Thursday, 19 November 2026, 09:00 – 10:00")
        testing.type_text(quick.title, "Call Sam")
        testing.key(quick.title, Qt.Key.Key_Return)
        self.assertFalse(quick.isVisible())
        self.assertEqual(self.shown(window.month), [(19, 9, 0, "Call Sam")])
        # "More details" hands the same draft to the dialog.
        double_click(window.month, self.cell(window, dt.date(2026, 11, 20)))
        quick.title.setText("Lunch")
        self.answers.append(lambda dialog: (self.assertEqual(dialog.title_edit.text(), "Lunch"),
                                            dialog.location.setText("Canteen"), PRIMARY)[2])
        testing.click(quick.more_button)
        self.assertEqual(sorted(event.location for event in self.saved(window).events()), ["", "Canteen"])

    def test_drag_across_days_makes_an_all_day_event(self):
        window = self.open()
        drag(window.month, self.cell(window, dt.date(2026, 11, 24)), self.cell(window, dt.date(2026, 11, 26)))
        self.assertEqual(window.quick.when_label.text(), "Tuesday, 24 November 2026 – Thursday, 26 November 2026")
        window.quick.title.setText("Conference")
        testing.click(window.quick.save_button)
        event, = self.saved(window).events()
        self.assertEqual((event.all_day, event.start, event.end), (True, "2026-11-24", "2026-11-27"))

    def test_edit_by_double_click_and_delete_with_undo(self):
        window = self.open(ev("a", "2026-11-18T09:00:00", "2026-11-18T10:00:00", summary="Old name"))
        occurrence, = window.month.occurrences()

        def rename(dialog):
            self.assertEqual((dialog.title_label.text(), dialog.title_edit.text()), ("Edit event", "Old name"))
            dialog.title_edit.setText("New name")
            return PRIMARY
        self.answers.append(rename)
        double_click(window.month, window.month.rect_of(occurrence).center())
        self.assertEqual(self.shown(window.month), [(18, 9, 0, "New name")])
        testing.click(window.month, window.month.rect_of(window.month.occurrences()[0]).center().toPoint())
        self.assertEqual(window.month.selected().event.summary, "New name")
        testing.key(window.month, Qt.Key.Key_Delete)
        self.assertEqual(self.shown(window.month), [])
        self.assertEqual(window.status.message(), "“New name” deleted.")
        testing.key(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.shown(window.month), [(18, 9, 0, "New name")])
        testing.key(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.shown(window.month), [(18, 9, 0, "Old name")])
        testing.key(window, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.shown(window.month), [(18, 9, 0, "New name")])
        self.assertEqual([event.summary for event in self.saved(window).events()], ["New name"])
        # The Delete button of the dialog, and Cancel.
        self.answers.append(lambda dialog: NONE)
        window.edit_occurrence(window.month.occurrences()[0])
        self.assertEqual(len(window.store.data), 1)
        self.answers.append(lambda dialog: SECONDARY)
        window.edit_occurrence(window.month.occurrences()[0])
        self.assertEqual(len(window.store.data), 0)

    def test_drag_moves_an_event_to_another_day(self):
        window = self.open(ev("a", "2026-11-18T09:00:00", "2026-11-18T10:00:00"))
        occurrence, = window.month.occurrences()
        drag(window.month, window.month.rect_of(occurrence).center(), self.cell(window, dt.date(2026, 11, 27)))
        self.assertEqual(self.shown(window.month), [(27, 9, 0, "a")])
        self.assertEqual(window.status.message(), "“a” moved.")
        testing.click(window.status.findChild(fluent.Button))                 # Undo in the banner
        self.assertEqual(self.shown(window.month), [(18, 9, 0, "a")])

    def test_more_opens_the_day(self):
        events = [ev(f"e{index}", f"2026-11-19T{8 + index:02d}:00:00", f"2026-11-19T{8 + index:02d}:30:00")
                  for index in range(9)]
        window = self.open(*events)
        testing.process(20)
        more = [rect for rect, target in window.month._hits if isinstance(target, tuple)]
        self.assertEqual(len(more), 1)
        testing.click(window.month, more[0].center().toPoint())
        self.assertEqual((window.switch.current(), window.title_label.text()), ("day", "Thursday, 19 November 2026"))
        self.assertEqual(len(window.week.grid._hits), 9)


class SeriesTest(CalendarCase):
    def series(self):
        return ev("s", "2026-11-16T09:00:00", "2026-11-16T10:00:00", rrule="FREQ=DAILY;COUNT=7", summary="Daily")

    def scope(self, name, result=PRIMARY):
        def answer(dialog):
            self.assertIn("repeating event", dialog.title_label.text())
            dialog.buttons[name].setChecked(True)
            return result
        return answer

    def days(self, window) -> list[tuple]:
        return [(occ.start.day, occ.start.hour, occ.event.summary) for occ in window.month.occurrences()]

    def test_make_a_series_in_the_dialog(self):
        window = self.open()

        def fill(dialog):
            dialog.title_edit.setText("Yoga")
            repeat = dialog.repeat
            self.assertTrue(repeat.details.isHidden())
            repeat.kind.setCurrentIndex(repeat.kind.findData("weekly"))
            self.assertFalse(repeat.details.isHidden() or repeat.days.isHidden())
            self.assertTrue(repeat.monthly.isHidden())
            self.assertEqual(repeat.days.days(), [1])                              # Tuesday, the day of the event
            testing.click(repeat.days.buttons[3])
            repeat.interval.setValue(2)
            repeat.end.setCurrentIndex(repeat.end.findData("count"))
            repeat.count.setValue(6)
            self.assertEqual(repeat.summary.text(), "Every 2 weeks on Tue, Thu, 6 times")
            repeat.kind.setCurrentIndex(repeat.kind.findData("monthly"))
            self.assertEqual([repeat.monthly.itemText(index) for index in range(repeat.monthly.count())],
                             ["On day 17", "On the third Tuesday"])
            dialog.start_date.setDate(QDate(2026, 11, 24))
            self.assertEqual([repeat.monthly.itemText(index) for index in range(repeat.monthly.count())],
                             ["On day 24", "On the fourth Tuesday", "On the last Tuesday"])
            repeat.kind.setCurrentIndex(repeat.kind.findData("weekly"))
            dialog.start_date.setDate(QDate(2026, 11, 17))
            return PRIMARY
        self.answers.append(fill)
        window.new_event()
        event, = self.saved(window).events()
        self.assertEqual(event.rrule, "FREQ=WEEKLY;INTERVAL=2;COUNT=6;BYDAY=TU,TH")
        self.assertEqual([day for day, _hour, _name in self.days(window)], [17, 19, 1, 3])

    def test_the_dialog_shows_the_rule_it_was_given(self):
        window = self.open(ev("m", "2026-11-17T09:00:00", "2026-11-17T10:00:00",
                              rrule="FREQ=MONTHLY;INTERVAL=3;UNTIL=20271231T225959Z;BYDAY=3TU", tz="Europe/Berlin"),
                           ev("odd", "2026-11-18T09:00:00", "2026-11-18T10:00:00",
                              rrule="FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"))
        seen = {}

        def look(dialog):
            repeat = dialog.repeat
            seen[dialog.title_edit.text()] = (repeat.kind.currentData(), repeat.interval.int_value(),
                                              repeat.monthly.currentData(), repeat.end.currentData(),
                                              repeat.until.date(), repeat.rule_text())
            return NONE
        for occurrence in window.month.occurrences()[:2]:
            self.answers.append(look)
            window.edit_occurrence(occurrence)
        self.assertEqual(seen["m"][:5], ("monthly", 3, "weekday", "until", QDate(2027, 12, 31)))
        self.assertEqual(seen["odd"][0], "custom")
        self.assertEqual(seen["odd"][5], "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1")     # kept as it is

    def test_change_this_event_only(self):
        window = self.open(self.series())
        third = window.store.data.occurrence("s", "2026-11-18T09:00:00", BERLIN)

        def move(dialog):
            self.assertEqual(dialog.start_date.date(), QDate(2026, 11, 18))        # this occurrence, not the first
            dialog.title_edit.setText("Moved")
            dialog.start_time.setTime(QTime(14, 0))
            return PRIMARY
        self.answers += [move, self.scope("this")]
        window.edit_occurrence(third)
        self.assertEqual(self.days(window), [(16, 9, "Daily"), (17, 9, "Daily"), (18, 14, "Moved"), (19, 9, "Daily"),
                                             (20, 9, "Daily"), (21, 9, "Daily"), (22, 9, "Daily")])
        saved = self.saved(window)
        self.assertEqual((saved.get("s").rrule, saved.get("s", "2026-11-18T09:00:00").start),
                         ("FREQ=DAILY;COUNT=7", "2026-11-18T14:00:00"))
        # Editing the changed occurrence again does not ask: it is its own event now.
        self.answers.append(lambda dialog: (self.assertTrue(dialog.repeat.isHidden()),
                                            dialog.title_edit.setText("Again"), PRIMARY)[2])
        window.edit_occurrence(window.store.data.occurrence("s", "2026-11-18T09:00:00", BERLIN))
        self.assertEqual(self.days(window)[2], (18, 14, "Again"))

    def test_change_this_and_following_and_all(self):
        window = self.open(self.series())
        fourth = window.store.data.occurrence("s", "2026-11-19T09:00:00", BERLIN)
        self.answers += [lambda dialog: (dialog.title_edit.setText("Later"), dialog.start_time.setTime(QTime(11, 0)),
                                         PRIMARY)[2], self.scope("following")]
        window.edit_occurrence(fourth)
        self.assertEqual(self.days(window), [(16, 9, "Daily"), (17, 9, "Daily"), (18, 9, "Daily"), (19, 11, "Later"),
                                             (20, 11, "Later"), (21, 11, "Later"), (22, 11, "Later")])
        self.assertEqual(len([event for event in window.store.data.events() if not event.recurrence_id]), 2)
        first = window.store.data.occurrence("s", "2026-11-17T09:00:00", BERLIN)
        self.answers += [lambda dialog: (dialog.title_edit.setText("Early"), PRIMARY)[1], self.scope("all")]
        window.edit_occurrence(first)
        self.assertEqual(self.days(window)[:4], [(16, 9, "Early"), (17, 9, "Early"), (18, 9, "Early"),
                                                 (19, 11, "Later")])
        self.answers += [lambda dialog: PRIMARY, self.scope("all", NONE)]           # cancel at the question
        window.edit_occurrence(window.store.data.occurrence("s", "2026-11-16T09:00:00", BERLIN))
        window.undo()
        window.undo()
        self.assertEqual(self.days(window), [(day, 9, "Daily") for day in range(16, 23)])

    def test_delete_asks_which(self):
        window = self.open(self.series())
        occurrence = lambda day: window.store.data.occurrence("s", f"2026-11-{day}T09:00:00", BERLIN)  # noqa: E731
        self.answers.append(self.scope("this"))
        window.delete_occurrence(occurrence(17))
        self.assertEqual([day for day, _h, _s in self.days(window)], [16, 18, 19, 20, 21, 22])
        self.answers.append(self.scope("following"))
        window.delete_occurrence(occurrence(20))
        self.assertEqual([day for day, _h, _s in self.days(window)], [16, 18, 19])
        self.answers.append(self.scope("all", NONE))
        window.delete_occurrence(occurrence(18))
        self.assertEqual(len(self.days(window)), 3)
        self.answers.append(self.scope("all"))
        window.delete_occurrence(occurrence(18))
        self.assertEqual(self.days(window), [])
        self.assertEqual(len(self.saved(window)), 0)
        for _ in range(3):
            window.undo()
        self.assertEqual(len(self.days(window)), 7)

    def test_dragging_one_of_a_series_moves_only_that_one(self):
        window = self.open(self.series())
        occurrence = window.store.data.occurrence("s", "2026-11-20T09:00:00", BERLIN)
        drag(window.month, window.month.rect_of(occurrence).center(), self.cell(window, dt.date(2026, 11, 28)))
        self.assertEqual([day for day, _h, _s in self.days(window)], [16, 17, 18, 19, 21, 22, 28])
        self.assertEqual(window.store.data.get("s").rrule, "FREQ=DAILY;COUNT=7")


class WeekTest(CalendarCase):
    def test_overlaps_time_zones_and_the_all_day_strip(self):
        window = self.open(ev("a", "2026-11-17T09:00:00", "2026-11-17T10:30:00"),
                           ev("b", "2026-11-17T09:30:00", "2026-11-17T11:00:00"),
                           ev("ny", "2026-11-17T09:00:00", "2026-11-17T10:00:00", tz="America/New_York"),
                           ev("trip", "2026-11-18", "2026-11-21", all_day=True),
                           ev("long", "2026-11-19T12:00:00", "2026-11-21T12:00:00"))
        window.set_view("week")
        testing.process(30)
        grid = window.week.grid
        rects = {occ.event.uid: rect for rect, occ in grid._hits}
        self.assertEqual(sorted(rects), ["a", "b", "ny"])
        self.assertFalse(rects["a"].intersects(rects["b"]))                        # side by side
        self.assertAlmostEqual(rects["a"].top(), 9 * views.HOUR + 1, delta=1)
        self.assertAlmostEqual(rects["ny"].top(), 15 * views.HOUR + 1, delta=1)   # 09:00 in New York is 15:00 here
        strip = {occ.event.uid: rect for rect, occ in window.week.header._hits}
        self.assertEqual(sorted(strip), ["long", "trip"])
        self.assertGreater(strip["trip"].width(), 2.5 * grid.column_width())
        self.assertGreater(window.week.header.height(), window.week.header.NAMES + 2 * views.ROW)
        testing.grab(window, self.tmp / "week.png")

    def test_drag_to_create_move_and_resize(self):
        window = self.open()
        window.set_view("week")
        testing.process(30)
        grid = window.week.grid
        thursday = dt.date(2026, 11, 19)
        drag(grid, self.slot(window, thursday, 14), self.slot(window, thursday, 15.6))
        self.assertEqual(window.quick.when_label.text(), "Thursday, 19 November 2026, 14:00 – 16:00")
        window.quick.title.setText("Workshop")
        testing.key(window.quick.title, Qt.Key.Key_Return)
        self.assertEqual(self.shown(window.week), [(19, 14, 0, "Workshop")])
        testing.process(20)
        rect = grid.rect_of(window.week.occurrences()[0])
        # Drag the body: one day on, an hour and a half earlier.
        drag(grid, rect.center(), rect.center() + QPointF(grid.column_width(), -1.5 * views.HOUR))
        occurrence, = window.week.occurrences()
        self.assertEqual((occurrence.start, occurrence.end), (D(2026, 11, 20, 12, 30), D(2026, 11, 20, 14, 30)))
        testing.process(20)
        rect = grid.rect_of(occurrence)
        # Drag the lower edge: it ends at 16:15 now.
        drag(grid, QPointF(rect.center().x(), rect.bottom() - 2), QPointF(rect.center().x(), 16.1 * views.HOUR))
        occurrence, = window.week.occurrences()
        self.assertEqual((occurrence.start, occurrence.end), (D(2026, 11, 20, 12, 30), D(2026, 11, 20, 16, 15)))
        window.undo()
        window.undo()
        self.assertEqual(self.shown(window.week), [(19, 14, 0, "Workshop")])
        # A click alone creates nothing.
        testing.click(grid, self.slot(window, thursday, 8).toPoint())
        self.assertFalse(window.quick.isVisible())
        self.assertEqual(window.week.slot, D(2026, 11, 19, 8, 0))

    def test_keys_choose_a_time_and_enter_makes_an_event(self):
        window = self.open()
        window.set_view("week")
        week = window.week
        week.setFocus()
        testing.key(week, Qt.Key.Key_Down)
        testing.key(week, Qt.Key.Key_Down)
        testing.key(week, Qt.Key.Key_Right)
        self.assertEqual(week.slot, D(2026, 11, 18, 9, 0))
        testing.key(week, Qt.Key.Key_Return)
        self.assertEqual(window.quick.when_label.text(), "Wednesday, 18 November 2026, 09:00 – 10:00")
        window.quick.close()
        for _ in range(6):
            testing.key(week, Qt.Key.Key_Right)
        self.assertEqual((week.slot.date(), window.title_label.text()), (dt.date(2026, 11, 24), "23 – 29 November 2026"))

    def test_the_time_line_ticks_only_while_the_view_is_shown(self):
        window = self.open()
        self.assertFalse(window.week._minute.isActive())
        window.set_view("day")
        testing.process(20)
        self.assertTrue(window.week._minute.isActive())
        window.set_view("month")
        testing.process(20)
        self.assertFalse(window.week._minute.isActive())


class CalendarsTest(CalendarCase):
    def test_hide_add_edit_delete(self):
        window = self.open(ev("p", "2026-11-18T09:00:00", "2026-11-18T10:00:00"),
                           ev("w", "2026-11-19T09:00:00", "2026-11-19T10:00:00", calendar="work"))
        items = window.calendar_list
        self.assertEqual([items.item(row).text() for row in range(items.count())], ["Personal", "Work"])
        items.item(1).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(self.shown(window.month), [(18, 9, 0, "p")])
        self.assertFalse(self.saved(window).calendar("work").visible)
        items.item(1).setCheckState(Qt.CheckState.Checked)

        def fill(dialog):
            self.assertFalse(dialog.primary_button.isEnabled())                    # a calendar needs a name
            dialog.name.setText("Holidays")
            self.assertTrue(dialog.swatches["mint"].isChecked())                   # the first colour not in use
            testing.click(dialog.swatches["coral"])
            return PRIMARY
        self.answers.append(fill)
        testing.click(window.add_calendar_button)
        self.assertEqual([(c.name, c.color) for c in window.store.data.calendars][-1], ("Holidays", "coral"))
        self.answers.append(lambda dialog: (dialog.name.setText("Job"), PRIMARY)[1])
        items.setFocus()
        items.setCurrentRow(1)
        testing.key(items, Qt.Key.Key_F2)
        self.assertEqual(items.item(1).text(), "Job")
        self.confirm = False
        testing.key(items, Qt.Key.Key_Delete)
        self.assertEqual((self.asked, len(window.store.data.calendars)), (["Delete “Job”?"], 3))
        self.confirm = True
        testing.key(items, Qt.Key.Key_Delete)
        self.assertEqual(self.shown(window.month), [(18, 9, 0, "p")])
        self.assertEqual([c.name for c in self.saved(window).calendars], ["Personal", "Holidays"])
        testing.key(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual([c.name for c in window.store.data.calendars], ["Personal", "Job", "Holidays"])
        self.assertEqual(len(self.shown(window.month)), 2)
        window.show_only(window.store.data.calendars[1].id)
        self.assertEqual(self.shown(window.month), [(19, 9, 0, "w")])


class SearchAndAgendaTest(CalendarCase):
    def test_search(self):
        window = self.open(ev("a", "2026-11-18T09:00:00", "2026-11-18T10:00:00", summary="Zahnärztin"),
                           ev("b", "2020-01-06T09:00:00", "2020-01-06T10:00:00", rrule="FREQ=WEEKLY",
                              summary="Team sync", location="Köln"),
                           ev("c", "2026-11-19T09:00:00", "2026-11-19T10:00:00", summary="Lunch", calendar="work"))
        testing.key(window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(window.search.hasFocus())
        testing.type_text(window.search, "koln")
        self.assertTrue(testing.wait_until(lambda: window.agenda.query == "koln"))
        self.assertIs(window.stack.currentWidget(), window.agenda)
        self.assertEqual(window.title_label.text(), "Results for “koln”")
        self.assertEqual([(occ.event.uid, occ.start) for occ in window.agenda.items()], [("b", D(2026, 11, 23, 9))])
        self.assertFalse(window.next_button.isEnabled())
        window.search.setText("zzz")
        window._search()
        self.assertTrue(window.agenda.empty.isVisible())
        self.assertEqual(window.agenda.empty.title_label.text(), "No events found")
        window.search.clear()
        window._search()
        self.assertIs(window.stack.currentWidget(), window.month)
        self.assertTrue(window.next_button.isEnabled())

    def test_agenda_lists_days_and_opens_events(self):
        window = self.open(ev("a", "2026-11-18T09:00:00", "2026-11-18T10:00:00", summary="Review", location="Room 4"),
                           ev("d", "2026-11-20", "2026-11-21", all_day=True, summary="Birthday"),
                           ev("old", "2026-11-10T09:00:00", "2026-11-10T10:00:00"))
        window.set_view("agenda")
        texts = [window.agenda.list.item(row).text() for row in range(window.agenda.list.count())]
        self.assertEqual(texts, ["Wednesday, 18 November 2026", "09:00 – 10:00    Review  ·  Room 4",
                                 "Friday, 20 November 2026", "All day    Birthday"])
        window.agenda.list.setCurrentRow(1)
        self.answers.append(lambda dialog: (dialog.title_edit.setText("Design review"), PRIMARY)[1])
        testing.key(window.agenda.list, Qt.Key.Key_Return)
        self.assertEqual(window.agenda.list.item(1).text(), "09:00 – 10:00    Design review  ·  Room 4")
        self.assertEqual(window.agenda.selected().event.uid, "a")
        testing.key(window.agenda.list, Qt.Key.Key_Delete)
        self.assertEqual(window.agenda.list.count(), 2)
        testing.click(window.previous_button)
        self.assertEqual(window.title_label.text(), "18 Oct – 16 Nov 2026")
        self.assertEqual(len(window.agenda.items()), 1)


class FilesTest(CalendarCase):
    def ics(self, name: str = "team.ics", calendar_name: str = "Team") -> Path:
        events = [model.make_event(ev("x1@team", "2026-11-18T09:00:00", "2026-11-18T10:00:00", calendar="c",
                                      summary="Kick-off", tz="Europe/Berlin", reminders=[15])),
                  model.make_event(ev("x2@team", "2026-11-23", "2026-11-25", calendar="c", all_day=True,
                                      summary="Offsite", rrule="FREQ=YEARLY"))]
        path = self.tmp / name
        path.write_bytes(ical.export(events, name=calendar_name))
        return path

    def test_import_into_a_new_calendar_and_undo(self):
        window = self.open(ev("mine", "2026-11-17T09:00:00", "2026-11-17T10:00:00"))
        path = self.ics()

        def look(dialog):
            self.assertEqual(dialog.title_label.text(), "Import 2 events?")
            lines = [dialog.list.item(row).text() for row in range(dialog.list.count())]
            self.assertEqual(lines, ["Wed 18 Nov 2026, 09:00    Kick-off", "Mon 23 Nov 2026    Offsite  ·  repeats"])
            self.assertEqual(dialog.target.currentText(), "New calendar “Team”")
            return PRIMARY
        self.answers.append(look)
        window.choose_open = lambda: str(path)
        testing.key(window, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(testing.wait_until(lambda: len(window.store.data) == 3))
        self.assertEqual((window.status.severity(), window.status.message()), ("success", "2 events imported into “Team”."))
        team = window.store.data.calendars[-1]
        self.assertEqual((team.name, window.store.data.get("x1@team").calendar), ("Team", team.id))
        self.assertEqual(window.store.data.get("x1@team").reminders, (15,))
        # The same file again updates and does not double.
        self.answers.append(lambda dialog: (dialog.target.setCurrentIndex(dialog.target.findData("work")),
                                            PRIMARY)[1])
        window.import_file(str(path))
        self.assertTrue(testing.wait_until(lambda: window.store.data.get("x1@team").calendar == "work"))
        self.assertEqual(len(window.store.data), 3)
        window.undo()
        window.undo()
        self.assertEqual([event.uid for event in window.store.data.events()], ["mine"])

    def test_files_that_are_not_calendars_and_cancel(self):
        window = self.open()
        bad = self.tmp / "notes.ics"
        bad.write_text("just some text")
        window.import_file(str(bad))
        self.assertTrue(testing.wait_until(lambda: window.status.severity() == "error"))
        self.assertIn("notes.ics was not imported", window.status.message())
        window.import_file(str(self.tmp / "missing.ics"))
        self.assertTrue(testing.wait_until(lambda: "cannot be read" in window.status.message()))
        self.answers.append(lambda dialog: NONE)
        window.import_file(str(self.ics()))
        self.assertTrue(testing.wait_until(lambda: not self.answers))
        self.assertEqual(len(window.store.data), 0)

    def test_export_one_calendar_and_all(self):
        window = self.open(ev("p", "2026-11-18T09:00:00", "2026-11-18T10:00:00", tz="Europe/Berlin"),
                           ev("w", "2026-11-19T09:00:00", "2026-11-19T10:00:00", calendar="work",
                              rrule="FREQ=WEEKLY", reminders=[10]))
        target = self.tmp / "out"
        window.choose_save = lambda suggestion: str(target)
        window.export_calendars("work")
        self.assertTrue(testing.wait_until(lambda: window.status.severity() == "success"))
        self.assertEqual(window.status.message(), "1 event exported to out.ics.")
        parsed = ical.read_file(str(target) + ".ics")
        self.assertEqual((parsed.name, [event.uid for event in parsed.events]), ("Work", ["w"]))
        self.assertEqual((parsed.events[0].rrule, parsed.events[0].reminders), ("FREQ=WEEKLY", (10,)))
        window.status.dismiss()
        testing.key(window, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(testing.wait_until(lambda: window.status.message() == "2 events exported to out.ics."))
        self.assertEqual(sorted(event.uid for event in ical.read_file(str(target) + ".ics").events), ["p", "w"])
        window.choose_save = lambda suggestion: str(self.tmp / "no" / "such" / "dir" / "x.ics")
        window.export_calendars(None)
        self.assertTrue(testing.wait_until(lambda: window.status.severity() == "error"))

    def test_arguments(self):
        self.assertEqual(parse_arguments(["--show=u@x", "--occurrence", "2026-11-18", "a.ics", "--new", "--", "--b"]),
                         {"files": ["a.ics", "--b"], "show": "u@x", "occurrence": "2026-11-18", "new": True})
        opened = []
        window = self.open(ev("s", "2026-12-07T09:00:00", "2026-12-07T10:00:00", rrule="FREQ=DAILY"))
        self.answers.append(lambda dialog: (opened.append((dialog.title_edit.text(), dialog.start_date.date())),
                                            NONE)[1])
        window.handle_arguments(["--show=s", "--occurrence=2026-12-09T09:00:00"])
        self.assertTrue(testing.wait_until(lambda: bool(opened)))
        self.assertEqual(opened, [("s", QDate(2026, 12, 9))])
        self.assertEqual(window.month.anchor, dt.date(2026, 12, 9))
        window.handle_arguments(["--show=gone"])
        self.assertIn("no longer", window.status.message())
        self.answers.append(lambda dialog: PRIMARY)
        window.handle_arguments([str(self.ics("arg.ics", ""))])
        self.assertTrue(testing.wait_until(lambda: len(window.store.data) == 3))
        self.assertEqual(window.store.data.get("x1@team").calendar, "personal")    # no name: the default calendar

    def test_the_store_survives_a_restart_and_a_damaged_file(self):
        window = self.open(ev("a", "2026-11-18T09:00:00", "2026-11-18T10:00:00"))
        self.answers.append(lambda dialog: (dialog.title_edit.setText("Kept"), PRIMARY)[1])
        window.new_event()
        window.close()
        again = self.open()
        self.assertEqual(sorted(event.summary for event in again.store.data.events()), ["Kept", "a"])
        again.close()
        self.path.write_text("{ broken")
        third = self.open()
        self.assertEqual(third.status.severity(), "warning")
        self.assertIn("calendar.json.damaged", third.status.message())
        self.assertEqual([calendar.name for calendar in third.store.data.calendars], ["Personal"])
        self.assertTrue(third.store.flush())
        self.assertEqual(len(json.loads(self.path.read_text())["calendars"]), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
