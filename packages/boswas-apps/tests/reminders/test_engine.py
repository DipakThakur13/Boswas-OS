"""The scheduling of the reminder service, with clocks and time zones the tests control."""

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import BERLIN, NEW_YORK, FakeClock, at, calendar_with, event  # noqa: E402

from boswas_apps.clock import model as clock_model  # noqa: E402
from boswas_apps.reminders import engine as engine_module  # noqa: E402
from boswas_apps.reminders import messages, timeutil  # noqa: E402
from boswas_apps.reminders.engine import Engine, Item, State  # noqa: E402


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.zone = BERLIN
        self.clock = FakeClock(at(2026, 11, 15, 20, 0))
        self.doc = clock_model.empty_doc()
        self.state = State()
        self.engine = Engine(self.state, lambda: self.zone)
        self.engine.set_clock(self.doc)
        self.engine.poll(self.clock())               # the first run: nothing was missed

    def restart(self) -> None:
        """The service stops and starts: only what the state file holds survives."""
        self.state = State.from_json(json.loads(json.dumps(self.state.to_json())))
        self.engine = Engine(self.state, lambda: self.zone)
        self.engine.set_clock(self.doc)
        self.engine.restore()

    def alarm(self, hour, minute, days=(), name="", snooze=10) -> dict:
        alarm = clock_model.make_alarm(hour, minute, days=days, name=name, snooze=snooze, now_wall=self.clock.wall)
        self.doc["alarms"].append(alarm)
        return alarm

    def run_until(self, target: float, step: float = 60.0) -> list[tuple[str, Item]]:
        """Let the service tick as it does (at the due time, at least once a minute) and collect what it does."""
        seen: list[tuple[str, Item]] = []
        while self.clock.wall < target:
            due = self.engine.next_due(self.clock())
            delay = step if due is None else max(0.01, min(step, due - self.clock.wall))
            self.clock.advance(min(delay, target - self.clock.wall))
            batch = self.engine.poll(self.clock())
            seen += [("ring", item) for item in batch.ring] + [("missed", item) for item in batch.missed]
        return seen


class AlarmEngineTest(EngineCase):
    def test_alarm_rings_at_its_time_exactly_once(self):
        self.alarm(7, 0, days=range(7), name="Wake up")
        self.assertEqual(self.engine.next_due(self.clock()), at(2026, 11, 16, 7, 0))
        seen = self.run_until(at(2026, 11, 16, 7, 30))
        self.assertEqual([(what, item.kind, item.title, item.when) for what, item in seen],
                         [("ring", "alarm", "Wake up", at(2026, 11, 16, 7, 0))])
        self.assertLess(seen[0][1].late, 1.0)
        self.assertEqual(self.engine.next_due(self.clock()), at(2026, 11, 17, 7, 0))

    def test_a_week_of_ticks_rings_once_a_day(self):
        self.alarm(7, 0, days=(0, 1, 2, 3, 4))
        seen = self.run_until(at(2026, 11, 23, 12, 0), step=600)
        days = [timeutil.from_epoch(item.when, BERLIN).day for _what, item in seen]
        self.assertEqual(days, [16, 17, 18, 19, 20, 23])
        self.assertTrue(all(what == "ring" for what, _item in seen))

    def test_across_the_start_of_summer_time(self):
        self.clock.set_wall(at(2026, 3, 27, 12, 0))
        self.engine.poll(self.clock())
        self.alarm(2, 30, days=range(7))
        seen = self.run_until(at(2026, 3, 30, 12, 0), step=900)
        self.assertEqual([timeutil.from_epoch(item.when, BERLIN) for _what, item in seen],
                         [dt.datetime(2026, 3, 28, 2, 30), dt.datetime(2026, 3, 29, 3, 30),
                          dt.datetime(2026, 3, 30, 2, 30)])

    def test_across_the_end_of_summer_time_it_rings_once_in_the_repeated_hour(self):
        self.clock.set_wall(at(2026, 10, 24, 12, 0))
        self.engine.poll(self.clock())
        self.alarm(2, 30, days=range(7))
        seen = self.run_until(at(2026, 10, 26, 12, 0), step=300)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1][1].when - seen[0][1].when, 25 * 3600)

    def test_nothing_fires_twice_across_restarts(self):
        self.alarm(7, 0, days=range(7))
        seen = self.run_until(at(2026, 11, 16, 7, 0) + 5)
        self.assertEqual(len(seen), 1)
        self.restart()
        batch = self.engine.poll(self.clock())
        # The alarm that was ringing unanswered when the service stopped comes back, marked as such.
        self.assertEqual([(item.kind, item.snoozed) for item in batch.ring], [("alarm", True)])
        self.assertFalse(batch.missed)
        self.assertFalse(self.engine.poll(self.clock()))
        self.clock.advance(120)
        self.assertFalse(self.engine.poll(self.clock()))
        self.engine.acknowledge(batch.ring[0])
        self.restart()
        self.assertFalse(self.engine.poll(self.clock()))

    def test_an_answered_alarm_does_not_come_back_after_a_restart(self):
        self.alarm(7, 0, days=range(7))
        (what, item), = self.run_until(at(2026, 11, 16, 7, 1))
        self.engine.acknowledge(item)
        self.restart()
        self.clock.advance(30)
        self.assertFalse(self.engine.poll(self.clock()))

    def test_machine_off_over_the_alarm_reports_it_as_missed_once(self):
        self.alarm(7, 0, days=range(7), name="Wake up")
        self.clock.advance(3600)
        self.engine.poll(self.clock())
        self.restart()
        self.clock.reboot(3 * 86400)                               # off for three days
        batch = self.engine.poll(self.clock())
        self.assertEqual(batch.ring, [])
        self.assertEqual(len(batch.missed), 1)                     # one line, not three
        self.assertEqual(batch.missed[0].detail["times"], 3)
        self.assertEqual(batch.missed[0].when, at(2026, 11, 18, 7, 0))
        self.assertFalse(self.engine.poll(self.clock()))
        self.restart()
        self.assertFalse(self.engine.poll(self.clock()))

    def test_a_few_minutes_late_still_rings(self):
        self.alarm(7, 0, days=range(7))
        self.clock.set_wall(at(2026, 11, 16, 6, 0))
        self.engine.poll(self.clock())
        self.clock.advance(3600 + 8 * 60)                          # asleep until 07:08
        batch = self.engine.poll(self.clock())
        self.assertEqual(len(batch.ring), 1)
        self.assertAlmostEqual(batch.ring[0].late, 480)
        self.clock.advance(86400 + 12 * 60)                        # the next day: 20 minutes late
        batch = self.engine.poll(self.clock())
        self.assertEqual((len(batch.ring), len(batch.missed)), (0, 1))

    def test_clock_set_back_does_not_ring_again(self):
        self.alarm(7, 0, days=range(7))
        self.run_until(at(2026, 11, 16, 7, 5))
        self.clock.set_wall(at(2026, 11, 16, 6, 55))               # back before the alarm
        self.assertFalse(self.engine.poll(self.clock()))
        seen = self.run_until(at(2026, 11, 16, 7, 30))
        self.assertEqual(seen, [])
        seen = self.run_until(at(2026, 11, 17, 7, 30), step=600)
        self.assertEqual(len(seen), 1)

    def test_clock_set_forward_past_the_alarm_does_not_lose_it(self):
        self.alarm(7, 0, days=range(7))
        self.clock.set_wall(at(2026, 11, 16, 7, 3))                # jumps over 07:00
        batch = self.engine.poll(self.clock())
        self.assertEqual(len(batch.ring), 1)
        self.clock.set_wall(at(2026, 11, 20, 12, 0))               # jumps over four alarms
        batch = self.engine.poll(self.clock())
        self.assertEqual((len(batch.ring), len(batch.missed)), (0, 1))
        self.assertEqual(batch.missed[0].detail["times"], 4)

    def test_time_zone_change_moves_the_alarm_without_double_ringing(self):
        self.alarm(7, 0, days=range(7))
        self.run_until(at(2026, 11, 16, 7, 5))
        self.zone = NEW_YORK                                        # 01:05 in New York now
        self.assertEqual(self.engine.next_due(self.clock()), at(2026, 11, 16, 7, 0, zone=NEW_YORK))
        seen = self.run_until(at(2026, 11, 16, 8, 0, zone=NEW_YORK), step=600)
        self.assertEqual([item.when for _what, item in seen], [at(2026, 11, 16, 7, 0, zone=NEW_YORK)])

    def test_one_time_alarm(self):
        alarm = self.alarm(21, 0, name="Tablet")
        seen = self.run_until(at(2026, 11, 16, 23, 0), step=600)
        self.assertEqual([(what, item.detail["once"]) for what, item in seen], [("ring", True)])
        self.assertIsNone(clock_model.alarm_next(alarm, self.clock.wall, BERLIN))

    def test_alarms_created_later_do_not_count_as_missed(self):
        self.clock.advance(86400 * 2)
        self.restart()                                             # checked_until is two days old
        self.alarm(7, 0, days=range(7))                            # armed now
        self.assertFalse(self.engine.poll(self.clock()))

    def test_first_run_reports_nothing_old(self):
        doc = clock_model.empty_doc()
        doc["alarms"].append(clock_model.make_alarm(7, 0, days=range(7), now_wall=0.0))
        engine = Engine(State(), lambda: BERLIN)
        engine.set_clock(doc)
        self.assertFalse(engine.poll(self.clock()))

    def test_catch_up_is_bounded(self):
        self.alarm(7, 0, days=range(7))
        self.clock.advance(400 * 86400)
        batch = self.engine.poll(self.clock())
        self.assertEqual(len(batch.missed), 1)
        self.assertLessEqual(batch.missed[0].detail["times"], 15)


class SnoozeTest(EngineCase):
    def test_snooze_brings_the_alarm_back(self):
        self.alarm(7, 0, days=range(7), snooze=10)
        (_what, item), = self.run_until(at(2026, 11, 16, 7, 0) + 2)
        due = self.engine.snooze(item, self.clock.wall, item.detail["snooze"])
        self.assertAlmostEqual(due, at(2026, 11, 16, 7, 10) + 2, delta=1)
        self.assertEqual(self.engine.next_due(self.clock()), due)
        seen = self.run_until(at(2026, 11, 16, 7, 30))
        self.assertEqual([(what, entry.snoozed, entry.key) for what, entry in seen], [("ring", True, item.key)])
        self.assertEqual(self.state.snoozed, {})

    def test_snooze_survives_a_restart(self):
        self.alarm(7, 0, days=range(7))
        (_what, item), = self.run_until(at(2026, 11, 16, 7, 0) + 1)
        due = self.engine.snooze(item, self.clock.wall, 10)
        self.assertTrue(self.state.dirty)
        self.restart()
        self.assertEqual(self.engine.next_due(self.clock()), due)
        seen = self.run_until(at(2026, 11, 16, 7, 30))
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][1].snoozed)

    def test_snooze_can_be_repeated(self):
        self.alarm(7, 0, days=range(7))
        (_what, item), = self.run_until(at(2026, 11, 16, 7, 0) + 1)
        for _ in range(3):
            self.engine.snooze(item, self.clock.wall, 5)
            (_what, item), = self.run_until(self.clock.wall + 6 * 60)
            self.assertTrue(item.snoozed)

    def test_snooze_that_came_due_while_off_is_reported_as_missed(self):
        self.alarm(7, 0, days=(0,))
        (_what, item), = self.run_until(at(2026, 11, 16, 7, 0) + 1)
        self.engine.snooze(item, self.clock.wall, 10)
        self.restart()
        self.clock.reboot(3 * 3600)
        batch = self.engine.poll(self.clock())
        self.assertEqual((len(batch.ring), [entry.key for entry in batch.missed]), (0, [item.key]))

    def test_snooze_of_a_changed_or_deleted_alarm_is_dropped(self):
        alarm = self.alarm(7, 0, days=range(7))
        (_what, item), = self.run_until(at(2026, 11, 16, 7, 0) + 1)
        self.engine.snooze(item, self.clock.wall, 10)
        clock_model.arm_alarm(alarm, False, self.clock.wall)       # the user switches it off
        self.assertEqual(self.run_until(at(2026, 11, 16, 7, 30)), [])
        self.assertEqual(self.state.snoozed, {})
        other = self.alarm(8, 0, days=range(7))
        (_what, item), = self.run_until(at(2026, 11, 16, 8, 0) + 1)
        self.engine.snooze(item, self.clock.wall, 10)
        self.doc["alarms"].remove(other)
        self.assertEqual(self.run_until(at(2026, 11, 16, 8, 30)), [])

    def test_one_time_alarm_switched_off_by_the_service_keeps_its_snooze(self):
        alarm = self.alarm(21, 0)
        (_what, item), = self.run_until(at(2026, 11, 15, 21, 0) + 1)
        alarm["enabled"] = False                                   # what the service does; ``armed`` stays
        self.engine.snooze(item, self.clock.wall, 10)
        seen = self.run_until(at(2026, 11, 15, 21, 30))
        self.assertEqual(len(seen), 1)

    def test_cancel_and_hurry(self):
        self.alarm(7, 0, days=range(7))
        (_what, item), = self.run_until(at(2026, 11, 16, 7, 0) + 1)
        self.engine.snooze(Item(item.key, item.kind, item.source, item.when, item.title,
                                {**item.detail, "retry": 1}), self.clock.wall, 5)
        self.assertTrue(self.engine.hurry(self.clock.wall))
        self.assertEqual(len(self.engine.poll(self.clock()).ring), 1)
        self.engine.snooze(item, self.clock.wall, 5)
        self.assertFalse(self.engine.hurry(self.clock.wall))       # a real snooze is not hurried
        self.engine.cancel_snoozes("alarm", item.source)
        self.assertEqual(self.state.snoozed, {})


class TimerEngineTest(EngineCase):
    def timer(self, seconds, name="") -> dict:
        timer = clock_model.make_timer(seconds, name)
        self.doc["timers"].append(timer)
        return timer

    def test_timer_fires_when_it_ends(self):
        timer = self.timer(300, "Tea")
        clock_model.timer_start(timer, self.clock())
        self.assertAlmostEqual(self.engine.next_due(self.clock()), self.clock.wall + 300)
        seen = self.run_until(self.clock.wall + 400)
        self.assertEqual([(what, item.kind, item.title) for what, item in seen], [("ring", "timer", "Tea")])
        self.assertEqual(self.run_until(self.clock.wall + 4000), [])       # done, shown once
        self.assertIsNone(self.engine.next_due(self.clock()))

    def test_several_timers_and_a_pause(self):
        first, second = self.timer(60, "one"), self.timer(120, "two")
        clock_model.timer_start(first, self.clock())
        clock_model.timer_start(second, self.clock())
        self.clock.advance(30)
        clock_model.timer_pause(second, self.clock())
        seen = self.run_until(self.clock.wall + 600)
        self.assertEqual([item.title for _what, item in seen], ["one"])
        clock_model.timer_start(second, self.clock())
        seen = self.run_until(self.clock.wall + 100)
        self.assertEqual([item.title for _what, item in seen], ["two"])

    def test_restart_fires_again(self):
        timer = self.timer(60)
        clock_model.timer_start(timer, self.clock())
        self.assertEqual(len(self.run_until(self.clock.wall + 90)), 1)
        clock_model.timer_restart(timer, self.clock())
        self.assertEqual(len(self.run_until(self.clock.wall + 90)), 1)

    def test_wall_clock_changes_do_not_move_a_timer(self):
        timer = self.timer(600)
        clock_model.timer_start(timer, self.clock())
        self.clock.advance(100)
        self.clock.set_wall(self.clock.wall + 86400)               # a day ahead; 500 s are still left
        self.assertFalse(self.engine.poll(self.clock()))
        self.assertAlmostEqual(self.engine.next_due(self.clock()) - self.clock.wall, 500)
        self.clock.set_wall(self.clock.wall - 2 * 86400)
        self.assertFalse(self.engine.poll(self.clock()))
        self.clock.advance(501)
        self.assertEqual(len(self.engine.poll(self.clock()).ring), 1)

    def test_timer_that_ended_while_asleep_or_off(self):
        timer = self.timer(600, "Laundry")
        clock_model.timer_start(timer, self.clock())
        self.clock.advance(605)                                    # asleep, woke up 5 s late
        batch = self.engine.poll(self.clock())
        self.assertEqual(len(batch.ring), 1)
        other = self.timer(600, "Oven")
        clock_model.timer_start(other, self.clock())
        self.restart()
        self.clock.reboot(7200)
        batch = self.engine.poll(self.clock())
        self.assertEqual([item.title for item in batch.missed], ["Oven"])
        self.assertAlmostEqual(batch.missed[0].late, 6600, delta=1)
        self.assertFalse(self.engine.poll(self.clock()))


class FocusEngineTest(EngineCase):
    def test_every_boundary_is_told(self):
        clock_model.focus_begin(self.doc["focus"], clock_model.focus_plan(45), self.clock())
        seen = self.run_until(self.clock.wall + 3000)
        self.assertEqual([(item.detail["ended"], item.detail["last"]) for _what, item in seen],
                         [("focus", False), ("break", False), ("focus", True)])
        self.assertAlmostEqual(seen[2][1].detail["focused"], 2400)

    def test_pause_stops_the_boundaries(self):
        focus = self.doc["focus"]
        clock_model.focus_begin(focus, clock_model.focus_plan(30), self.clock())
        self.clock.advance(600)
        clock_model.focus_pause(focus, self.clock())
        self.assertEqual(self.run_until(self.clock.wall + 7200), [])
        self.assertIsNone(self.engine.next_due(self.clock()))
        clock_model.focus_resume(focus, self.clock())
        self.assertAlmostEqual(self.engine.next_due(self.clock()) - self.clock.wall, 1200)

    def test_after_a_long_sleep_only_the_end_is_told(self):
        clock_model.focus_begin(self.doc["focus"], clock_model.focus_plan(45), self.clock())
        self.clock.advance(4 * 3600)
        batch = self.engine.poll(self.clock())
        self.assertEqual(len(batch.ring) + len(batch.missed), 1)
        self.assertTrue(batch.missed[0].detail["last"])
        self.assertFalse(self.engine.poll(self.clock()))


class EventEngineTest(EngineCase):
    def test_reminder_before_a_single_event(self):
        self.engine.set_calendar(calendar_with(event("e1", "2026-11-16T10:00:00", "2026-11-16T11:00:00",
                                                     tz="Europe/Berlin", reminders=[15], summary="Dentist",
                                                     location="Main St 1")))
        seen = self.run_until(at(2026, 11, 16, 12, 0), step=3600)
        self.assertEqual([(what, item.kind, item.title, item.when) for what, item in seen],
                         [("ring", "event", "Dentist", at(2026, 11, 16, 9, 45))])
        detail = seen[0][1].detail
        self.assertEqual((detail["start"], detail["end"], detail["location"], detail["minutes"]),
                         (at(2026, 11, 16, 10, 0), at(2026, 11, 16, 11, 0), "Main St 1", 15))

    def test_several_reminders_of_one_event(self):
        self.engine.set_calendar(calendar_with(event("e1", "2026-11-17T10:00:00", "2026-11-17T11:00:00",
                                                     reminders=[1440, 60, 0])))
        seen = self.run_until(at(2026, 11, 17, 12, 0), step=3600)
        self.assertEqual([item.when for _what, item in seen],
                         [at(2026, 11, 16, 10, 0), at(2026, 11, 17, 9, 0), at(2026, 11, 17, 10, 0)])
        self.assertEqual(len({item.key for _what, item in seen}), 3)

    def test_weekly_event_across_summer_time_reminds_by_the_wall_clock(self):
        self.clock.set_wall(at(2026, 3, 20, 12, 0))
        self.engine.poll(self.clock())
        self.engine.set_calendar(calendar_with(event(
            "weekly", "2026-03-16T09:00:00", "2026-03-16T09:30:00", tz="Europe/Berlin",
            rrule="FREQ=WEEKLY;BYDAY=MO", reminders=[30])))
        seen = self.run_until(at(2026, 4, 7, 12, 0), step=3600)
        self.assertEqual([timeutil.from_epoch(item.when, BERLIN) for _what, item in seen],
                         [dt.datetime(2026, 3, 23, 8, 30), dt.datetime(2026, 3, 30, 8, 30),
                          dt.datetime(2026, 4, 6, 8, 30)])

    def test_event_in_another_zone(self):
        self.engine.set_calendar(calendar_with(event("ny", "2026-11-16T09:00:00", "2026-11-16T10:00:00",
                                                     tz="America/New_York", reminders=[10])))
        seen = self.run_until(at(2026, 11, 16, 23, 0), step=3600)
        self.assertEqual([item.when for _what, item in seen], [at(2026, 11, 16, 8, 50, zone=NEW_YORK)])

    def test_all_day_event_and_a_reminder_on_the_day(self):
        self.engine.set_calendar(calendar_with(event("bday", "2026-11-18", "2026-11-19", all_day=True,
                                                     reminders=[-540, 900])))
        seen = self.run_until(at(2026, 11, 19, 0, 0), step=3600)
        self.assertEqual([timeutil.from_epoch(item.when, BERLIN) for _what, item in seen],
                         [dt.datetime(2026, 11, 17, 9, 0), dt.datetime(2026, 11, 18, 9, 0)])

    def test_exceptions_of_a_series(self):
        data = calendar_with(
            event("s", "2026-11-16T09:00:00", "2026-11-16T10:00:00", rrule="FREQ=DAILY;COUNT=5", reminders=[0],
                  exdates=["2026-11-17T09:00:00"]),
            event("s", "2026-11-18T14:00:00", "2026-11-18T15:00:00", recurrence_id="2026-11-18T09:00:00",
                  reminders=[60], summary="moved"))
        self.engine.set_calendar(data)
        seen = self.run_until(at(2026, 11, 22, 0, 0), step=3600)
        self.assertEqual([(timeutil.from_epoch(item.when, BERLIN), item.title) for _what, item in seen],
                         [(dt.datetime(2026, 11, 16, 9), "s"), (dt.datetime(2026, 11, 18, 13), "moved"),
                          (dt.datetime(2026, 11, 19, 9), "s"), (dt.datetime(2026, 11, 20, 9), "s")])

    def test_hidden_calendars_do_not_remind(self):
        self.engine.set_calendar(calendar_with(event("e1", "2026-11-16T10:00:00", "2026-11-16T11:00:00",
                                                     reminders=[15]), hidden=True))
        self.assertEqual(self.run_until(at(2026, 11, 16, 12, 0), step=3600), [])

    def test_missed_reminder_of_an_event_that_still_runs_is_shown(self):
        self.engine.set_calendar(calendar_with(event("conf", "2026-11-16T09:00:00", "2026-11-16T17:00:00",
                                                     reminders=[15])))
        self.restart()
        self.engine.set_calendar(calendar_with(event("conf", "2026-11-16T09:00:00", "2026-11-16T17:00:00",
                                                     reminders=[15])))
        self.clock.reboot(at(2026, 11, 16, 11, 0) - self.clock.wall)       # switched on at 11:00
        batch = self.engine.poll(self.clock())
        self.assertEqual([item.title for item in batch.ring], ["conf"])
        self.clock.reboot(at(2026, 11, 16, 18, 0) - self.clock.wall)
        self.assertFalse(self.engine.poll(self.clock()))

    def test_reminder_of_an_event_that_is_over_is_only_summed_up(self):
        data = calendar_with(event("a", "2026-11-16T09:00:00", "2026-11-16T10:00:00", reminders=[15]),
                             event("b", "2026-11-16T11:00:00", "2026-11-16T12:00:00", reminders=[15, 5]))
        self.engine.set_calendar(data)
        self.clock.reboot(at(2026, 11, 16, 20, 0) - self.clock.wall)
        batch = self.engine.poll(self.clock())
        self.assertEqual(batch.ring, [])
        self.assertEqual(sorted(item.title for item in batch.missed), ["a", "b", "b"])

    def test_an_event_written_down_after_it_began_does_not_remind(self):
        past = event("old", "2026-11-15T08:00:00", "2026-11-15T23:00:00", reminders=[15],
                     modified=int(self.clock.wall))
        soon = event("soon", "2026-11-15T20:10:00", "2026-11-15T21:00:00", reminders=[30],
                     modified=int(self.clock.wall))
        self.state.checked_until = self.clock.wall - 86400          # as after a crash: an old mark
        self.engine.set_calendar(calendar_with(past, soon))
        batch = self.engine.poll(self.clock())
        # "soon" starts in ten minutes: its reminder (due twenty minutes ago) is still wanted.
        self.assertEqual([item.title for item in batch.ring], ["soon"])
        self.assertEqual(batch.missed, [])

    def test_pathological_rules_cost_little(self):
        import time
        events = [event(f"h{index}", "1990-01-01T00:00:00", "1990-01-01T00:00:01",
                        rrule="FREQ=SECONDLY;COUNT=999999999", reminders=[0]) for index in range(50)]
        events += [event(f"n{index}", "2020-02-01T00:00:00", "2020-02-01T01:00:00",
                         rrule="FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30", reminders=[5]) for index in range(50)]
        self.engine.set_calendar(calendar_with(*events))
        started = time.monotonic()
        self.engine.poll(self.clock())
        self.engine.next_due(self.clock())
        self.assertLess(time.monotonic() - started, 5.0)

    def test_the_plan_is_reused_between_polls(self):
        data = calendar_with(event("e1", "2026-11-16T10:00:00", "2026-11-16T11:00:00", reminders=[15]))
        calls = []
        original = data.reminders_between
        data.reminders_between = lambda *args, **kwargs: (calls.append(args), original(*args, **kwargs))[1]
        self.engine.set_calendar(data)
        for _ in range(120):                                       # two hours of one-minute ticks
            self.clock.advance(60)
            self.engine.poll(self.clock())
            self.engine.next_due(self.clock())
        self.assertLessEqual(len(calls), 2)


class StateTest(unittest.TestCase):
    def test_round_trip_and_hostile_content(self):
        state = State()
        item = Item("alarm:a:1", "alarm", "a", 1.0, "x", {"snooze": 10, "nested": {"a": 1}})
        state.checked_until = 123.0
        state.fired = {"k": 1.0}
        state.snoozed = {item.key: {"due": 5.0, "item": item.to_json()}}
        state.ringing = {item.key: item.to_json()}
        again = State.from_json(json.loads(json.dumps(state.to_json())))
        self.assertEqual(again.to_json(), state.to_json())
        for raw in (None, [], "x", {"checked_until": "soon", "fired": [1], "snoozed": {"k": 1}, "ringing": 7},
                    {"fired": {"k": "x", 5: 1, "ok": 2.0}, "snoozed": {"other": {"due": 1, "item": item.to_json()}},
                     "checked_until": float("nan"), "ringing": {"k": {"key": "different"}}}):
            loaded = State.from_json(raw)
            self.assertIsNone(loaded.checked_until)
            self.assertEqual(loaded.snoozed, {})
            self.assertEqual(loaded.ringing, {})
        self.assertEqual(State.from_json({"fired": {"ok": 2.0, "bad": True}}).fired, {"ok": 2.0})

    def test_load_and_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "state.json"
            self.assertIsNone(State.load(path).checked_until)
            state = State()
            state.checked_until = 5.0
            state.dirty = True
            state.save(path)
            self.assertFalse(state.dirty)
            self.assertEqual(State.load(path).checked_until, 5.0)
            path.write_text("{broken")
            self.assertIsNone(State.load(path).checked_until)
            self.assertTrue((path.parent / "state.json.damaged").exists())

    def test_fired_keys_are_pruned(self):
        state = State()
        engine = Engine(state, lambda: BERLIN)
        base = at(2026, 11, 15, 12, 0)
        state.checked_until = base
        state.fired = {f"old{index}": base - 10 * 86400 for index in range(200)}
        state.fired.update({f"new{index}": base - 60 for index in range(10)})
        engine.poll(clock_model.Now(base + 1, 1.0, "b"))
        self.assertEqual(sorted(state.fired), sorted(f"new{index}" for index in range(10)))
        state.fired = {f"k{index}": base for index in range(engine_module.MAX_FIRED + 50)}
        engine.poll(clock_model.Now(base + 2, 2.0, "b"))
        self.assertEqual(len(state.fired), engine_module.MAX_FIRED)

    def test_item_from_hostile_json(self):
        self.assertIsNone(Item.from_json({"key": "", "kind": "alarm", "source": "a", "when": 1, "title": ""}))
        self.assertIsNone(Item.from_json({"key": "k", "kind": "bomb", "source": "a", "when": 1, "title": ""}))
        self.assertIsNone(Item.from_json({"key": "k", "kind": "alarm", "source": "a", "when": True, "title": ""}))
        item = Item.from_json({"key": "k", "kind": "alarm", "source": "a", "when": 1, "title": "t" * 9000,
                               "detail": {"deep": {"deeper": {"deepest": 1}}, "list": [1, 2], "text": "x" * 9000,
                                          "n": float("inf")}})
        self.assertEqual(len(item.title), 500)
        self.assertEqual(item.detail["deep"], {"deeper": None})
        self.assertIsNone(item.detail["list"])
        self.assertEqual(len(item.detail["text"]), 500)


class MessageTest(unittest.TestCase):
    wall = at(2026, 11, 16, 7, 0)

    def test_alarm(self):
        item = Item("k", "alarm", "a", self.wall, "Wake up", {"hour": 7, "minute": 0, "snooze": 5})
        message = messages.compose(item, self.wall, BERLIN)
        self.assertEqual((message.app, message.summary, message.body, message.urgency, message.timeout,
                          message.sound), ("clock", "Wake up", "07:00", messages.CRITICAL, 0, "alarm"))
        self.assertEqual(message.actions, (("snooze", "Snooze 5 min"), ("dismiss", "Dismiss")))
        self.assertEqual(messages.compose(item, self.wall, BERLIN, True).body, "7:00 AM")
        unnamed = Item("k", "alarm", "a", self.wall, "", {"hour": 19, "minute": 5}, snoozed=True)
        message = messages.compose(unnamed, self.wall, BERLIN)
        self.assertEqual((message.summary, message.body), ("Alarm", "19:05 · snoozed"))
        late = Item("k", "alarm", "a", self.wall, "", {"hour": 7, "minute": 0}, late=300.0)
        self.assertEqual(messages.compose(late, self.wall, BERLIN).body, "07:00 · 5 minutes ago")

    def test_timer_and_focus(self):
        message = messages.compose(Item("k", "timer", "t", self.wall, "Tea", {"duration": 300}), self.wall, BERLIN)
        self.assertEqual((message.summary, message.body, message.actions, message.sound),
                         ("Tea", "5 minutes are up", (("dismiss", "Dismiss"), ("restart", "Restart")), "timer"))
        self.assertEqual(messages.compose(Item("k", "timer", "t", self.wall, "", {"duration": 60}), self.wall,
                                          BERLIN).body, "1 minute is up")
        pause = Item("k", "focus", "1", self.wall, "", {"ended": "focus", "last": False, "next_seconds": 300})
        self.assertEqual(messages.compose(pause, self.wall, BERLIN).summary, "Time for a break")
        back = Item("k", "focus", "1", self.wall, "", {"ended": "break", "last": False, "next_seconds": 1500})
        self.assertEqual(messages.compose(back, self.wall, BERLIN).body, "Back to focus for 25 minutes.")
        done = Item("k", "focus", "1", self.wall, "", {"ended": "focus", "last": True, "focused": 3000})
        self.assertEqual(messages.compose(done, self.wall, BERLIN).body, "You focused for 50 minutes.")

    def test_event(self):
        detail = {"start": at(2026, 11, 16, 10, 0), "end": at(2026, 11, 16, 11, 0), "location": "Room <4> & 5"}
        message = messages.compose(Item("k", "event", "u", self.wall, "Review", detail), at(2026, 11, 16, 9, 45),
                                   BERLIN)
        self.assertEqual((message.app, message.summary), ("calendar", "Review"))
        self.assertEqual(message.body, "Today 10:00 – 11:00 · in 15 minutes\nRoom <4> & 5")
        self.assertEqual([key for key, _label in message.actions], ["open", "snooze", "dismiss"])
        running = messages.compose(Item("k", "event", "u", self.wall, "", detail), at(2026, 11, 16, 10, 20), BERLIN)
        self.assertEqual((running.summary, running.body.split("\n")[0]),
                         ("(No title)", "Today 10:00 – 11:00 · started 20 minutes ago"))
        whole_day = {"start": at(2026, 11, 17, 0, 0), "end": at(2026, 11, 19, 0, 0), "all_day": True}
        self.assertEqual(messages.compose(Item("k", "event", "u", self.wall, "Trip", whole_day), self.wall,
                                          BERLIN).body, "Tomorrow – Wed 18 Nov, all day")
        overnight = {"start": at(2026, 11, 16, 22, 0), "end": at(2026, 11, 17, 6, 0)}
        self.assertEqual(messages.compose(Item("k", "event", "u", self.wall, "Night", overnight), self.wall,
                                          BERLIN, True).body, "Today 10:00 PM – Tomorrow 6:00 AM · in 15 hours")

    def test_missed_summaries_are_one_per_application(self):
        items = [Item(f"a{index}", "alarm", "a", self.wall - index * 86400, "Wake up",
                      {"hour": 7, "minute": 0, "times": 1 + index}) for index in range(7)]
        items.append(Item("t", "timer", "t", self.wall - 50, "Tea", {"duration": 60}))
        items.append(Item("e", "event", "u", self.wall - 7200, "Standup",
                          {"start": self.wall - 7000, "end": self.wall - 6000}))
        clock, calendar = messages.compose_missed(items, self.wall + 600, BERLIN)
        self.assertEqual((clock.app, clock.summary, clock.sound, clock.urgency),
                         ("clock", "8 missed alarms and timers", "", messages.NORMAL))
        lines = clock.body.split("\n")
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[0], "Today 07:00 · Alarm: Wake up")
        self.assertEqual(lines[1], "Today 06:59 · Timer: Tea")
        self.assertEqual(lines[2], "Yesterday 07:00 · Alarm: Wake up (2 times)")
        self.assertEqual(lines[-1], "and 3 more")
        self.assertEqual((calendar.app, calendar.summary), ("calendar", "Missed reminder"))
        self.assertEqual(calendar.body, "Today 05:03 – 05:20 · Standup")
        self.assertEqual(messages.compose_missed([], self.wall, BERLIN), [])
        single, = messages.compose_missed(items[:1], self.wall, BERLIN)
        self.assertEqual(single.summary, "Missed alarm")


if __name__ == "__main__":
    unittest.main()
