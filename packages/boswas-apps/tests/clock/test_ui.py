"""Clock's window, driven as a user drives it: clicks, keys, dialogs; clocks the test moves by hand."""

import datetime as dt
import re
import unittest
from pathlib import Path

from boswas_fluent import testing

from PySide6.QtCore import QTime, Qt  # noqa: E402

import boswas_fluent as fluent  # noqa: E402
from boswas_apps.clock import cities, model  # noqa: E402
from boswas_apps.clock.store import ClockStore, SettingsStore  # noqa: E402
from boswas_apps.clock.ui import window as window_module  # noqa: E402
from boswas_apps.clock.ui.window import ClockWindow, parse_arguments  # noqa: E402
from boswas_apps.reminders import jsonfile, timeutil  # noqa: E402

BERLIN = timeutil.zone("Europe/Berlin")
PRIMARY = fluent.ContentDialog.Result.PRIMARY
SECONDARY = fluent.ContentDialog.Result.SECONDARY
NONE = fluent.ContentDialog.Result.NONE


class FakeClock:
    def __init__(self) -> None:
        self.wall = timeutil.to_epoch(dt.datetime(2026, 11, 16, 20, 48, 0), BERLIN)
        self.boot = 9000.0

    def __call__(self) -> model.Now:
        return model.Now(self.wall, self.boot, "boot")

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.boot += seconds


class FakeService:
    def __init__(self, state: str = "running") -> None:
        self.state = state
        self.asked = 0

    def ensure(self) -> str:
        self.asked += 1
        return self.state

    def is_running(self) -> bool:
        return self.state == "running"


class ClockCase(testing.FluentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = FakeClock()
        self.path = self.tmp / "clock.json"
        self.service = FakeService()
        self.answers: list = []                   # what the next dialogs do: a function(dialog) -> Result

    def open(self, *arguments: str, doc: dict | None = None) -> ClockWindow:
        if doc is not None:
            jsonfile.write_json_atomic(self.path, doc)
        store = ClockStore(self.path, now=self.clock, watch=False)
        settings = SettingsStore(self.tmp / "settings.json")
        window = ClockWindow(list(arguments), store=store, settings=settings, now=self.clock, zone=lambda: BERLIN,
                             service=self.service)
        window.run_dialog = self._answer
        self.show(window, 1100, 720)
        testing.process(20)
        return window

    def _answer(self, dialog):
        self.assertTrue(self.answers, f"an unexpected dialog: {dialog.title_label.text()}")
        return self.answers.pop(0)(dialog)

    def saved(self, window: ClockWindow) -> dict:
        self.assertTrue(window.store.flush())
        return model.normalize(jsonfile.read_json(self.path))

    def page(self, window: ClockWindow, key: str):
        window.navigation.set_current(key)
        testing.process(20)
        return window.navigation.page(key)


class WindowTest(ClockCase):
    def test_pages_and_arguments(self):
        window = self.open(doc=model.empty_doc())
        self.assertEqual(window.navigation.keys(), ["focus", "timers", "alarms", "stopwatch", "world", "settings"])
        self.assertEqual(window.navigation.current(), "alarms")
        self.assertEqual(window.windowTitle(), "Clock")
        window.handle_arguments(["--page=timers"])
        self.assertEqual(window.navigation.current(), "timers")
        window.handle_arguments(["--page", "World-Clock"])
        self.assertEqual(window.navigation.current(), "world")
        window.handle_arguments(["--page=../../etc"])
        self.assertEqual(window.navigation.current(), "world")
        self.assertEqual(parse_arguments(["--page=alarm"]), "alarms")
        self.assertIsNone(parse_arguments(["file.txt", "--page"]))
        testing.key(window, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.service.asked, 1)

    def test_the_last_page_is_remembered(self):
        window = self.open(doc=model.empty_doc())
        self.page(window, "stopwatch")
        window.close()
        again = self.open()
        self.assertEqual(again.navigation.current(), "stopwatch")
        third = self.open("--page=focus")
        self.assertEqual(third.navigation.current(), "focus")

    def test_first_run_has_the_usual_timers_and_writes_the_file(self):
        window = self.open()
        self.assertEqual([timer["duration"] for timer in self.saved(window)["timers"]], [60, 180, 300, 600])

    def test_a_damaged_file_is_set_aside_and_said(self):
        self.path.write_text("{ not json")
        window = self.open()
        self.assertEqual(window.status.severity(), "warning")
        self.assertIn("clock.json.damaged", window.status.message())
        self.assertEqual((self.tmp / "clock.json.damaged").read_text(), "{ not json")

    def test_missing_reminder_service_is_said_plainly(self):
        self.service.state = "unavailable"
        window = self.open(doc=model.empty_doc())
        self.assertTrue(window.status.isVisible())
        self.assertIn("cannot notify you", window.status.message())
        self.service.state = "running"
        window.status.action_triggered.emit()
        self.assertFalse(window.status.isVisible())
        self.assertEqual(self.service.asked, 2)

    def test_no_colour_is_written_into_the_interface(self):
        root = Path(window_module.__file__).resolve().parents[2]
        pattern = re.compile(r"#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b|QColor\(\s*[\d\"']|setStyleSheet|rgba?\(")
        for folder in ("clock/ui", "calendar/ui"):
            for path in sorted((root / folder).glob("*.py")):
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    self.assertIsNone(pattern.search(line), f"{path.name}:{number}: {line.strip()}")


class AlarmsTest(ClockCase):
    def fill(self, hour, minute, name="", days=(), result=PRIMARY):
        def answer(dialog):
            dialog.time.setTime(QTime(hour, minute))
            dialog.name.setText(name)
            dialog.days.set_days(days)
            return result
        return answer

    def test_add_edit_toggle_delete_undo(self):
        window = self.open(doc=model.empty_doc())
        page = self.page(window, "alarms")
        self.assertEqual(page.stack.currentIndex(), 1)                       # the empty state
        self.answers.append(self.fill(4, 0, "Flight", ()))
        testing.click(page.empty.action_button)
        card, = page.cards.values()
        self.assertEqual((card.time_label.text(), card.name_label.text(), card.next_label.text()),
                         ("04:00", "Flight", "Rings in 7 hours 12 minutes"))
        self.assertTrue(card.switch.isChecked())
        self.assertFalse(card.days.isVisible())
        stored = self.saved(window)["alarms"][0]
        self.assertEqual((stored["hour"], stored["days"], stored["enabled"], stored["armed"]),
                         (4, [], True, self.clock.wall))
        # Ctrl+N adds another; the cards are in order of time.
        self.answers.append(self.fill(2, 30, "", (0, 2)))
        testing.key(window, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual([c.time_label.text() for c in page.grid.widgets()], ["02:30", "04:00"])
        self.assertTrue(page.grid.widgets()[0].days.isVisible())
        # The switch turns it off, and on again from now.
        testing.click(card.switch)
        self.assertEqual(card.next_label.text(), "Off")
        self.assertFalse(model.find(self.saved(window)["alarms"], card.alarm_id)["enabled"])
        self.clock.advance(120)
        testing.click(card.switch)
        self.assertEqual(model.find(self.saved(window)["alarms"], card.alarm_id)["armed"], self.clock.wall)
        self.assertEqual(card.next_label.text(), "Rings in 7 hours 10 minutes")
        # A click on the card edits it.
        self.answers.append(self.fill(5, 15, "Flight to Lisbon", (4,)))
        testing.click(card.name_label)
        self.assertEqual((card.time_label.text(), card.name_label.text()), ("05:15", "Flight to Lisbon"))
        self.assertEqual(model.find(self.saved(window)["alarms"], card.alarm_id)["days"], [4])
        # Cancel changes nothing; Delete in the dialog deletes, Ctrl+Z brings it back.
        self.answers.append(self.fill(9, 9, "no", (), NONE))
        card.setFocus()
        testing.key(card, Qt.Key.Key_Return)
        self.assertEqual(card.time_label.text(), "05:15")
        self.answers.append(lambda dialog: SECONDARY)
        testing.key(card, Qt.Key.Key_Space)
        self.assertEqual(len(page.cards), 1)
        self.assertEqual(window.status.message(), "Alarm “Flight to Lisbon” deleted.")
        testing.key(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(page.cards), 2)
        self.assertEqual(len(self.saved(window)["alarms"]), 2)
        self.assertFalse(window.status.isVisible())

    def test_the_text_follows_the_minute_and_the_time_format(self):
        doc = model.empty_doc()
        doc["alarms"].append(model.make_alarm(19, 5, days=range(7), now_wall=self.clock.wall - 10))
        window = self.open(doc=doc)
        page = self.page(window, "alarms")
        card, = page.cards.values()
        self.assertEqual(card.next_label.text(), "Rings in 22 hours 17 minutes")
        self.assertTrue(page.ticker.is_active())
        self.clock.advance(60)
        page.ticker.kick()
        self.assertEqual(card.next_label.text(), "Rings in 22 hours 16 minutes")
        window.settings.set("time_format", "12")
        self.assertEqual(card.time_label.text(), "7:05 PM")
        self.page(window, "stopwatch")
        self.assertFalse(page.ticker.is_active())                            # hidden pages do not tick

    def test_changes_made_by_the_service_show_up(self):
        doc = model.empty_doc()
        doc["alarms"].append(model.make_alarm(21, 0, name="Once", now_wall=self.clock.wall - 10))
        window = self.open(doc=doc)
        page = self.page(window, "alarms")
        card, = page.cards.values()
        self.assertTrue(card.switch.isChecked())

        def service_switches_it_off(raw):
            raw = model.normalize(raw)
            raw["alarms"][0]["enabled"] = False
            return raw
        jsonfile.update_json(self.path, service_switches_it_off)
        window.store.reload_now()
        self.assertTrue(testing.wait_until(lambda: not card.switch.isChecked()))
        # A change of ours made at the same moment as one of the service keeps both.
        jsonfile.update_json(self.path, lambda raw: {**raw, "world": [{"zone": "Asia/Tokyo", "name": "Tokyo"}]})
        testing.click(card.switch)
        stored = self.saved(window)
        self.assertEqual((stored["alarms"][0]["enabled"], [place["zone"] for place in stored["world"]]),
                         (True, ["Asia/Tokyo"]))
        testing.process(20)
        self.assertEqual(len(window.store.doc["world"]), 1)


class TimersTest(ClockCase):
    def test_start_pause_reset_and_the_ring(self):
        window = self.open()
        page = self.page(window, "timers")
        card = page.grid.widgets()[2]                                        # the 5 minute timer
        self.assertEqual((card.name_label.text(), card.ring.text(), card.ring.state()), ("5 min timer", "05:00", "idle"))
        self.assertFalse(card.reset_button.isEnabled())
        self.assertFalse(page.ticker.is_active())
        testing.click(card.start_button)
        self.assertEqual(card.ring.state(), "running")
        self.assertTrue(page.ticker.is_active())
        self.assertEqual(card.start_button.toolTip(), "Pause")
        self.clock.advance(83.4)
        page.ticker.kick()
        self.assertEqual(card.ring.text(), "03:37")
        self.assertEqual(self.saved(window)["timers"][2]["state"], "running")
        testing.click(card.start_button)                                     # pause
        self.clock.advance(500)
        page.ticker.kick()
        self.assertEqual((card.ring.text(), card.ring.state()), ("03:37", "paused"))
        self.assertFalse(page.ticker.is_active())
        testing.click(card.start_button)
        self.clock.advance(216)
        page.ticker.kick()
        self.assertEqual((card.ring.text(), card.ring.state()), ("00:01", "running"))
        self.clock.advance(1)
        page.ticker.kick()
        self.assertEqual((card.ring.text(), card.ring.state(), card.start_button.toolTip()),
                         ("00:00", "done", "Restart"))
        testing.click(card.reset_button)
        self.assertEqual((card.ring.text(), card.ring.state()), ("05:00", "idle"))
        self.assertEqual(self.saved(window)["timers"][2]["state"], "idle")

    def test_several_run_at_once_and_survive_closing_the_window(self):
        window = self.open()
        page = self.page(window, "timers")
        cards = page.grid.widgets()
        testing.click(cards[0].start_button)
        self.clock.advance(20)
        testing.click(cards[3].start_button)
        self.assertEqual(window.navigation.item("timers").data(Qt.ItemDataRole.UserRole + 3) or 2, 2)
        window.close()
        self.clock.advance(30)                                               # Clock is closed for half a minute
        again = self.open()
        page = self.page(again, "timers")
        self.assertEqual([card.ring.text() for card in page.grid.widgets()], ["00:10", "03:00", "05:00", "09:30"])
        self.assertEqual([card.ring.state() for card in page.grid.widgets()], ["running", "idle", "idle", "running"])

    def test_add_edit_expand_delete(self):
        window = self.open()
        page = self.page(window, "timers")

        def new(dialog):
            self.assertTrue(dialog.primary_button.isEnabled())
            dialog.hours.setValue(0)
            dialog.minutes.setValue(0)
            dialog.seconds.setValue(0)
            self.assertFalse(dialog.primary_button.isEnabled())             # a timer of no length cannot be saved
            dialog.hours.setValue(1)
            dialog.minutes.setValue(30)
            dialog.name.setText("Roast")
            return PRIMARY
        self.answers.append(new)
        testing.key(window, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        card = page.grid.widgets()[-1]
        self.assertEqual((card.name_label.text(), card.ring.text()), ("Roast", "1:30:00"))
        testing.click(card.expand_button)
        self.assertEqual(page.stack.currentIndex(), 2)
        big = page.expanded_card
        testing.click(big.start_button)
        self.clock.advance(61)
        page.ticker.kick()
        self.assertEqual((big.ring.text(), card.ring.text()), ("1:28:59", "1:28:59"))
        testing.key(page, Qt.Key.Key_Escape)
        self.assertEqual(page.stack.currentIndex(), 0)
        self.answers.append(lambda dialog: (dialog.minutes.setValue(45), dialog.name.setText("Roast beef"), PRIMARY)[2])
        testing.click(card.edit_button)
        self.assertEqual((card.name_label.text(), card.ring.text(), card.ring.state()), ("Roast beef", "1:45:00", "idle"))
        testing.click(card.delete_button)
        self.assertEqual(len(page.grid.widgets()), 4)
        testing.click(window.status.findChild(fluent.Button))               # Undo
        self.assertEqual([c.name_label.text() for c in page.grid.widgets()][-1], "Roast beef")
        self.assertEqual(len(self.saved(window)["timers"]), 5)


class StopwatchTest(ClockCase):
    def test_keys_laps_and_the_display(self):
        window = self.open(doc=model.empty_doc())
        page = self.page(window, "stopwatch")
        self.assertEqual(page.display.text(), "00:00:00.00")
        self.assertFalse(page.lap_button.isEnabled())
        self.assertFalse(page.ticker.is_active())
        page.start_button.setFocus()
        testing.click(page.start_button)
        self.assertTrue(page.ticker.is_active())
        self.assertEqual(page.start_button.text(), "Pause")
        for seconds in (54.36, 95.44, 61.0):
            self.clock.advance(seconds)
            testing.click(page.lap_button)
        self.clock.advance(12.345)
        page.ticker.kick()
        self.assertEqual(page.display.text(), "00:03:43.14")
        rows = [[page.laps.item(row, column).text() for column in range(4)] for row in range(page.laps.rowCount())]
        self.assertEqual(rows, [["3", "00:01:01.00", "00:03:30.80", ""], ["2", "00:01:35.44", "00:02:29.80", "Slowest"],
                                ["1", "00:00:54.36", "00:00:54.36", "Fastest"]])
        testing.click(page.start_button)                                    # pause
        self.assertFalse(page.ticker.is_active())
        self.assertEqual(page.start_button.text(), "Resume")
        self.clock.advance(1000)
        page.ticker.kick()
        self.assertEqual(page.display.text(), "00:03:43.14")
        self.assertEqual(self.saved(window)["stopwatch"]["state"], "paused")
        testing.click(page.reset_button)
        self.assertEqual((page.display.text(), page.laps.rowCount(), page.hint.isVisible()), ("00:00:00.00", 0, True))

    def test_it_goes_on_while_the_window_is_closed_and_does_not_tick_while_hidden(self):
        window = self.open(doc=model.empty_doc())
        page = self.page(window, "stopwatch")
        testing.click(page.start_button)
        self.page(window, "alarms")
        self.assertFalse(page.ticker.is_active())
        window.close()
        self.clock.advance(3600)
        again = self.open()
        page = self.page(again, "stopwatch")
        self.assertEqual(page.display.text(), "01:00:00.00")
        self.assertTrue(page.ticker.is_active())
        again.hide()
        testing.process(10)
        self.assertFalse(page.ticker.is_active())

    def test_the_figures_scale_with_the_window(self):
        window = self.open(doc=model.empty_doc())
        page = self.page(window, "stopwatch")
        wide = page.display.font_size()
        window.resize(520, 520)
        testing.process(20)
        narrow = page.display.font_size()
        self.assertGreater(wide, narrow)
        self.assertGreaterEqual(narrow, 16)


class WorldTest(ClockCase):
    def test_add_remove_compare(self):
        window = self.open(doc=model.empty_doc())
        page = self.page(window, "world")
        self.assertEqual([row.name_label.text() for row in page.rows], ["Local time"])
        self.assertEqual(page.rows[0].time_label.text(), "20:48")

        def choose(dialog):
            dialog.search.setText("toky")
            self.assertEqual(dialog.place().zone, "Asia/Tokyo")
            return PRIMARY
        self.answers.append(choose)
        testing.key(window, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        tokyo = page.rows[1]
        self.assertEqual((tokyo.name_label.text(), tokyo.time_label.text(), tokyo.detail_label.text()),
                         ("Tokyo", "04:48", "Tomorrow, 8 hours ahead"))
        self.assertIn("night", tokyo.accessibleName())

        def taken(dialog):
            dialog.search.setText("tokyo")
            self.assertIsNone(dialog.place())                               # already added
            self.assertFalse(dialog.primary_button.isEnabled())
            dialog.search.setText("zzzz")
            self.assertEqual(dialog.results.count(), 0)
            return NONE
        self.answers.append(taken)
        page.add_city()
        page.slider.setValue(16)                                            # four hours on
        self.assertEqual((page.rows[0].time_label.text(), tokyo.time_label.text()), ("00:48", "08:48"))
        self.assertIn("4 hours from now", page.compare_label.text())
        self.assertIn("day", tokyo.accessibleName())
        testing.click(page.now_button)
        self.assertEqual(tokyo.time_label.text(), "04:48")
        testing.click(tokyo.remove_button)
        self.assertEqual(len(page.rows), 1)
        window.undo()
        self.assertEqual([row.name_label.text() for row in page.rows], ["Local time", "Tokyo"])
        self.assertEqual(self.saved(window)["world"], [{"zone": "Asia/Tokyo", "name": "Tokyo"}])


class FocusTest(ClockCase):
    def test_a_session(self):
        window = self.open(doc=model.empty_doc())
        page = self.page(window, "focus")
        self.assertEqual(page.stack.currentIndex(), 0)
        page.minutes.setValue(45)
        self.assertEqual(page.plan_label.text(), "You'll have 1 break.")
        page.skip.setChecked(True)
        self.assertEqual(page.plan_label.text(), "You'll have no breaks.")
        page.skip.setChecked(False)
        testing.click(page.start_button)
        self.assertEqual((page.stack.currentIndex(), page.phase_label.text(), page.ring.text()),
                         (1, "Focus period 1 of 2", "20:00"))
        self.assertTrue(page.ticker.is_active())
        self.clock.advance(600)
        testing.click(page.pause_button)
        self.clock.advance(999)
        page.ticker.kick()
        self.assertEqual((page.ring.text(), page.pause_button.text()), ("10:00", "Resume"))
        testing.click(page.pause_button)
        self.clock.advance(660)                                             # a minute into the break
        page.ticker.kick()
        page.ticker.kick()
        self.assertEqual((page.phase_label.text(), page.ring.text()), ("Break", "04:00"))
        self.assertEqual(self.saved(window)["focus"]["history"], {"2026-11-16": 1200.0})
        self.assertEqual(page.day_ring.text(), "20 min")
        testing.click(page.stop_button)
        self.assertEqual(page.stack.currentIndex(), 0)
        self.assertEqual(self.saved(window)["focus"]["state"], "idle")
        self.assertEqual(window.settings.values.session_minutes, 45)

    def test_goal_and_streak(self):
        doc = model.empty_doc()
        doc["focus"]["history"] = {"2026-11-15": 3600.0, "2026-11-14": 3700.0}
        window = self.open(doc=doc)
        page = self.page(window, "focus")
        self.assertIn("2 days", page.streak_label.text())
        self.assertIn("1 h", page.yesterday_label.text())
        page.goal.setCurrentIndex(page.goal.findData(120))
        page.goal.activated.emit(page.goal.currentIndex())
        self.assertEqual(self.saved(window)["focus"]["goal"], 120)
        self.assertIn("0 days", page.streak_label.text())


class SettingsTest(ClockCase):
    def test_settings_are_kept(self):
        window = self.open(doc=model.empty_doc())
        self.page(window, "settings")
        window.time_format_box.setCurrentIndex(window.time_format_box.findData("12"))
        window.time_format_box.activated.emit(window.time_format_box.currentIndex())
        testing.click(window.sound_switch)
        window.close()
        saved = jsonfile.read_json(self.tmp / "settings.json")
        self.assertEqual((saved["time_format"], saved["alarm_sound"]), ("12", False))
        self.assertIn("Running", window.service_card.description())


class CitiesTest(unittest.TestCase):
    def test_every_city_of_the_table_has_a_zone_here(self):
        table = [line.split("|") for line in cities._TABLE.strip().splitlines()]
        self.assertGreater(len(table), 300)
        self.assertEqual(len({name for name, _country, _zone in table}), len(table))
        for name, country, zone in table:
            self.assertTrue(name and country, name)
            self.assertTrue(timeutil.zone(zone) is not None or timeutil.zone(cities._OLD_NAMES.get(zone, "")) is not None,
                            f"{name}: {zone}")

    def test_search(self):
        self.assertGreater(len(cities.places()), 400)
        self.assertEqual(cities.search("zurich")[0].label, "Zürich, Switzerland")
        self.assertEqual(cities.search("SÃO pau")[0].zone, "America/Sao_Paulo")
        self.assertEqual(cities.search("new y")[0].name, "New York")
        self.assertIn("Mumbai", [place.name for place in cities.search("india")])
        self.assertIn("Ushuaia", [place.name for place in cities.search("ushu")])          # only in the database
        self.assertEqual(cities.search("qqqq"), [])
        self.assertEqual(len(cities.search("", limit=10)), 10)
        self.assertEqual(cities.name_for_zone("America/Argentina/Buenos_Aires"), "Buenos Aires")


if __name__ == "__main__":
    unittest.main()
