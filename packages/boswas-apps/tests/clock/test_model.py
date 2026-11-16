"""Clock's arithmetic: alarms across summer time, timers on two clocks, laps, focus plans."""

import datetime as dt
import unittest

from boswas_apps.clock import model
from boswas_apps.reminders import timeutil

BERLIN = timeutil.zone("Europe/Berlin")
NEW_YORK = timeutil.zone("America/New_York")
SYDNEY = timeutil.zone("Australia/Sydney")
UTC = timeutil.UTC


def at(zone, *args) -> float:
    return timeutil.to_epoch(dt.datetime(*args), zone)


def local(zone, instant) -> dt.datetime:
    return timeutil.from_epoch(instant, zone)


def now(wall=1_000_000.0, boot=500.0, boot_id="boot-a") -> model.Now:
    return model.Now(wall, boot, boot_id)


def later(moment: model.Now, seconds: float) -> model.Now:
    return model.Now(moment.wall + seconds, moment.boot + seconds, moment.boot_id)


class AlarmTest(unittest.TestCase):
    def alarm(self, hour, minute, days=(), armed=0.0, enabled=True):
        return model.make_alarm(hour, minute, days=days, now_wall=armed, enabled=enabled)

    def test_one_time_alarm_rings_at_its_next_time_and_never_again(self):
        armed = at(BERLIN, 2026, 11, 15, 22, 0)
        alarm = self.alarm(7, 0, armed=armed)
        ring = model.alarm_next(alarm, armed, BERLIN)
        self.assertEqual(local(BERLIN, ring), dt.datetime(2026, 11, 16, 7, 0))
        self.assertEqual(model.alarm_next(alarm, ring - 1, BERLIN), ring)
        self.assertIsNone(model.alarm_next(alarm, ring, BERLIN))
        self.assertIsNone(model.alarm_next(alarm, ring + 86400 * 3, BERLIN))

    def test_one_time_alarm_for_later_today(self):
        armed = at(BERLIN, 2026, 11, 15, 6, 59, 30)
        ring = model.alarm_next(self.alarm(7, 0, armed=armed), armed, BERLIN)
        self.assertEqual(local(BERLIN, ring), dt.datetime(2026, 11, 15, 7, 0))

    def test_an_alarm_set_for_this_very_minute_waits_a_day(self):
        armed = at(BERLIN, 2026, 11, 15, 7, 0, 20)
        ring = model.alarm_next(self.alarm(7, 0, armed=armed), armed, BERLIN)
        self.assertEqual(local(BERLIN, ring), dt.datetime(2026, 11, 16, 7, 0))

    def test_disabled_alarm_never_rings(self):
        self.assertIsNone(model.alarm_next(self.alarm(7, 0, enabled=False), 0.0, BERLIN))

    def test_weekly_alarm_picks_its_weekdays(self):
        alarm = self.alarm(6, 30, days=(0, 2))                  # Monday and Wednesday
        after = at(BERLIN, 2026, 11, 15, 12, 0)                 # a Sunday
        rings = []
        for _ in range(4):
            after = model.alarm_next(alarm, after, BERLIN)
            rings.append(local(BERLIN, after))
        self.assertEqual(rings, [dt.datetime(2026, 11, 16, 6, 30), dt.datetime(2026, 11, 18, 6, 30),
                                 dt.datetime(2026, 11, 23, 6, 30), dt.datetime(2026, 11, 25, 6, 30)])

    def test_weekly_alarm_does_not_ring_before_it_was_switched_on(self):
        armed = at(BERLIN, 2026, 11, 16, 6, 30, 1)              # Monday, one second too late
        alarm = self.alarm(6, 30, days=(0,), armed=armed)
        ring = model.alarm_next(alarm, armed - 3600, BERLIN)
        self.assertEqual(local(BERLIN, ring), dt.datetime(2026, 11, 23, 6, 30))

    def test_weekly_alarm_keeps_its_wall_time_across_summer_time(self):
        alarm = self.alarm(7, 0, days=(6,))                     # Sundays
        after = at(BERLIN, 2026, 3, 16, 0, 0)
        instants = []
        for _ in range(4):
            after = model.alarm_next(alarm, after, BERLIN)
            instants.append(after)
        self.assertEqual([local(BERLIN, value) for value in instants],
                         [dt.datetime(2026, 3, 22, 7), dt.datetime(2026, 3, 29, 7), dt.datetime(2026, 4, 5, 7),
                          dt.datetime(2026, 4, 12, 7)])
        # The week in which the clocks go forward is an hour shorter.
        self.assertEqual(instants[1] - instants[0], 7 * 86400 - 3600)
        self.assertEqual(instants[2] - instants[1], 7 * 86400)

    def test_alarm_in_the_skipped_hour_rings_an_hour_later_by_the_clock(self):
        alarm = self.alarm(2, 30, days=tuple(range(7)))
        ring = model.alarm_next(alarm, at(BERLIN, 2026, 3, 28, 12, 0), BERLIN)
        self.assertEqual(local(BERLIN, ring), dt.datetime(2026, 3, 29, 3, 30))
        following = model.alarm_next(alarm, ring, BERLIN)
        self.assertEqual(local(BERLIN, following), dt.datetime(2026, 3, 30, 2, 30))

    def test_alarm_in_the_repeated_hour_rings_once(self):
        alarm = self.alarm(2, 30, days=tuple(range(7)))
        first = model.alarm_next(alarm, at(BERLIN, 2026, 10, 24, 12, 0), BERLIN)
        second = model.alarm_next(alarm, first, BERLIN)
        self.assertEqual(first, dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC).timestamp())
        self.assertEqual(local(BERLIN, second), dt.datetime(2026, 10, 26, 2, 30))
        self.assertEqual(second - first, 86400 + 3600)

    def test_the_southern_hemisphere_and_new_york(self):
        alarm = self.alarm(2, 30, days=tuple(range(7)))
        ring = model.alarm_next(alarm, at(SYDNEY, 2026, 10, 3, 12, 0), SYDNEY)       # clocks go forward 4 October
        self.assertEqual(local(SYDNEY, ring), dt.datetime(2026, 10, 4, 3, 30))
        ring = model.alarm_next(alarm, at(NEW_YORK, 2026, 3, 7, 12, 0), NEW_YORK)    # 8 March
        self.assertEqual(local(NEW_YORK, ring), dt.datetime(2026, 3, 8, 3, 30))

    def test_a_changed_time_zone_moves_the_alarm_with_the_wall_clock(self):
        alarm = self.alarm(7, 0, days=tuple(range(7)))
        after = at(UTC, 2026, 11, 15, 12, 0)
        self.assertEqual(local(BERLIN, model.alarm_next(alarm, after, BERLIN)).hour, 7)
        self.assertEqual(local(NEW_YORK, model.alarm_next(alarm, after, NEW_YORK)).hour, 7)
        self.assertEqual(model.alarm_next(alarm, after, NEW_YORK) - model.alarm_next(alarm, after, BERLIN), 6 * 3600)

    def test_occurrences_in_a_window(self):
        alarm = self.alarm(7, 0, days=tuple(range(7)))
        start = at(BERLIN, 2026, 11, 10, 12, 0)
        rings = model.alarm_occurrences(alarm, start, start + 5 * 86400, BERLIN)
        self.assertEqual([local(BERLIN, ring).day for ring in rings], [11, 12, 13, 14, 15])
        self.assertEqual(model.alarm_occurrences(alarm, start, start + 3600, BERLIN), [])
        many = model.alarm_occurrences(alarm, start, start + 400 * 86400, BERLIN, limit=10)
        self.assertEqual(len(many), 10)
        self.assertEqual(local(BERLIN, many[-1]).date(), dt.date(2027, 12, 15))

    def test_far_future_does_not_raise(self):
        alarm = self.alarm(7, 0, days=(0,))
        self.assertIsNone(model.alarm_next(alarm, 1e18, BERLIN))
        self.assertIsNone(model.alarm_next(self.alarm(7, 0, armed=1e18), 0.0, BERLIN))


class TimerTest(unittest.TestCase):
    def test_start_pause_resume_reset(self):
        timer = model.make_timer(300, "Tea")
        start = now()
        self.assertEqual(model.timer_phase(timer, start), "idle")
        self.assertEqual(model.timer_remaining(timer, start), 300)
        model.timer_start(timer, start)
        self.assertEqual(model.timer_phase(timer, later(start, 10)), "running")
        self.assertAlmostEqual(model.timer_remaining(timer, later(start, 10)), 290)
        model.timer_pause(timer, later(start, 100))
        self.assertEqual(timer["state"], "paused")
        self.assertAlmostEqual(model.timer_remaining(timer, later(start, 5000)), 200)
        self.assertIsNone(model.timer_due_wall(timer, later(start, 5000)))
        model.timer_start(timer, later(start, 5000))
        self.assertAlmostEqual(model.timer_remaining(timer, later(start, 5050)), 150)
        self.assertEqual(timer["run"], 2)
        self.assertEqual(model.timer_phase(timer, later(start, 5200)), "done")
        self.assertEqual(model.timer_remaining(timer, later(start, 9999)), 0)
        model.timer_reset(timer)
        self.assertEqual((timer["state"], timer["left"], timer["anchor"]), ("idle", 300.0, None))

    def test_the_wall_clock_may_be_changed_while_a_timer_runs(self):
        timer = model.make_timer(600)
        start = now()
        model.timer_start(timer, start)
        # Ten seconds later somebody sets the wall clock two hours ahead: the boot clock is what counts.
        moved = model.Now(start.wall + 7210, start.boot + 10, start.boot_id)
        self.assertAlmostEqual(model.timer_remaining(timer, moved), 590)
        self.assertAlmostEqual(model.timer_due_wall(timer, moved), moved.wall + 590)
        back = model.Now(start.wall - 7200, start.boot + 20, start.boot_id)
        self.assertAlmostEqual(model.timer_remaining(timer, back), 580)

    def test_after_a_reboot_the_wall_clock_is_used(self):
        timer = model.make_timer(600)
        start = now()
        model.timer_start(timer, start)
        rebooted = model.Now(start.wall + 100, 12.0, "boot-b")
        self.assertAlmostEqual(model.timer_remaining(timer, rebooted), 500)
        self.assertEqual(model.timer_phase(timer, model.Now(start.wall + 700, 30.0, "boot-b")), "done")

    def test_time_asleep_counts(self):
        # The boot clock goes on while the machine is suspended; a monotonic clock would not.
        timer = model.make_timer(600)
        start = now()
        model.timer_start(timer, start)
        self.assertEqual(model.timer_phase(timer, later(start, 1800)), "done")

    def test_restart_and_edit(self):
        timer = model.make_timer(60, "a")
        start = now()
        model.timer_start(timer, start)
        model.timer_restart(timer, later(start, 70))
        self.assertAlmostEqual(model.timer_remaining(timer, later(start, 80)), 50)
        self.assertEqual(timer["run"], 2)
        model.timer_set(timer, 120, "  new   name ")
        self.assertEqual((timer["name"], timer["duration"], timer["state"]), ("new name", 120, "idle"))
        model.timer_start(timer, start)
        model.timer_set(timer, 120, "same length")
        self.assertEqual(timer["state"], "running")             # only a new length resets


class StopwatchTest(unittest.TestCase):
    def test_runs_pauses_and_counts_laps(self):
        watch = model.empty_doc()["stopwatch"]
        start = now()
        self.assertFalse(model.stopwatch_lap(watch, start))     # not running
        model.stopwatch_start(watch, start)
        self.assertAlmostEqual(model.stopwatch_elapsed(watch, later(start, 12.34)), 12.34)
        self.assertTrue(model.stopwatch_lap(watch, later(start, 10)))
        self.assertTrue(model.stopwatch_lap(watch, later(start, 25)))
        self.assertTrue(model.stopwatch_lap(watch, later(start, 31)))
        model.stopwatch_pause(watch, later(start, 40))
        self.assertAlmostEqual(model.stopwatch_elapsed(watch, later(start, 4000)), 40)
        model.stopwatch_start(watch, later(start, 4000))
        self.assertAlmostEqual(model.stopwatch_elapsed(watch, later(start, 4005)), 45)
        laps = model.stopwatch_laps(watch)
        self.assertEqual([(lap.number, round(lap.split, 6), round(lap.total, 6), lap.mark) for lap in laps],
                         [(1, 10, 10, ""), (2, 15, 25, "slowest"), (3, 6, 31, "fastest")])
        model.stopwatch_reset(watch)
        self.assertEqual(watch, {"state": "idle", "elapsed": 0.0, "anchor": None, "laps": []})

    def test_one_lap_or_equal_laps_have_no_marks(self):
        watch = model.empty_doc()["stopwatch"]
        start = now()
        model.stopwatch_start(watch, start)
        model.stopwatch_lap(watch, later(start, 5))
        self.assertEqual([lap.mark for lap in model.stopwatch_laps(watch)], [""])
        model.stopwatch_lap(watch, later(start, 10))
        self.assertEqual([lap.mark for lap in model.stopwatch_laps(watch)], ["", ""])

    def test_the_lap_list_is_bounded(self):
        watch = model.empty_doc()["stopwatch"]
        start = now()
        model.stopwatch_start(watch, start)
        for index in range(model.MAX_LAPS + 5):
            model.stopwatch_lap(watch, later(start, index))
        self.assertEqual(len(watch["laps"]), model.MAX_LAPS)

    def test_survives_a_reboot_through_the_wall_clock(self):
        watch = model.empty_doc()["stopwatch"]
        start = now()
        model.stopwatch_start(watch, start)
        restored = model.normalize({"stopwatch": watch})["stopwatch"]
        self.assertAlmostEqual(model.stopwatch_elapsed(restored, model.Now(start.wall + 90, 3.0, "boot-b")), 90)


class FocusTest(unittest.TestCase):
    def kinds(self, segments):
        return [(segment["kind"], segment["seconds"]) for segment in segments]

    def test_plans(self):
        self.assertEqual(self.kinds(model.focus_plan(30)), [("focus", 1800)])
        self.assertEqual(self.kinds(model.focus_plan(45)), [("focus", 1200), ("break", 300), ("focus", 1200)])
        self.assertEqual(self.kinds(model.focus_plan(60)), [("focus", 1650), ("break", 300), ("focus", 1650)])
        self.assertEqual(self.kinds(model.focus_plan(60, skip_breaks=True)), [("focus", 3600)])
        self.assertEqual(self.kinds(model.focus_plan(5)), [("focus", 300)])
        self.assertEqual(self.kinds(model.focus_plan(100, 45, 10)), [("focus", 2700), ("break", 600), ("focus", 2700)])

    def test_every_plan_adds_up_and_ends_with_focus(self):
        for total in range(model.FOCUS_MIN_TOTAL, model.FOCUS_MAX_TOTAL + 1, 5):
            for focus_minutes, break_minutes in ((25, 5), (50, 10), (15, 3), (90, 30), (10, 60)):
                plan = model.focus_plan(total, focus_minutes, break_minutes)
                self.assertEqual(sum(segment["seconds"] for segment in plan), total * 60)
                self.assertEqual(plan[0]["kind"], "focus")
                self.assertEqual(plan[-1]["kind"], "focus")
                for first, second in zip(plan, plan[1:]):
                    self.assertNotEqual(first["kind"], second["kind"])
                if len(plan) > 1:
                    self.assertGreaterEqual(min(s["seconds"] for s in plan if s["kind"] == "focus"), 600)

    def test_a_session_from_start_to_end(self):
        focus = model.empty_doc()["focus"]
        start = now(wall=at(BERLIN, 2026, 11, 15, 9, 0))
        model.focus_begin(focus, model.focus_plan(45), start)
        status = model.focus_status(focus, later(start, 600))
        self.assertEqual((status.state, status.kind, status.index, status.period, status.periods),
                         ("running", "focus", 0, 1, 2))
        self.assertAlmostEqual(status.segment_left, 600)
        self.assertAlmostEqual(status.total_left, 2100)
        status = model.focus_status(focus, later(start, 1300))
        self.assertEqual((status.kind, status.index), ("break", 1))
        self.assertAlmostEqual(status.focused, 1200)
        status = model.focus_status(focus, later(start, 99999))
        self.assertEqual((status.state, status.kind), ("done", "focus"))
        self.assertAlmostEqual(status.focused, 2400)
        self.assertEqual(model.focus_boundaries(focus), [(0, 1200.0), (1, 1500.0), (2, 2700.0)])

    def test_pause_keeps_the_place(self):
        focus = model.empty_doc()["focus"]
        start = now()
        model.focus_begin(focus, model.focus_plan(30), start)
        model.focus_pause(focus, later(start, 100))
        self.assertAlmostEqual(model.focus_position(focus, later(start, 5000)), 100)
        model.focus_resume(focus, later(start, 5000))
        self.assertAlmostEqual(model.focus_position(focus, later(start, 5100)), 200)

    def test_settling_counts_focus_time_once(self):
        focus = model.empty_doc()["focus"]
        start = now(wall=at(BERLIN, 2026, 11, 15, 9, 0))
        model.focus_begin(focus, model.focus_plan(45), start)
        self.assertAlmostEqual(model.focus_settle(focus, later(start, 1300), BERLIN), 1200)
        self.assertAlmostEqual(model.focus_settle(focus, later(start, 1400), BERLIN), 0)
        self.assertEqual(focus["state"], "running")
        self.assertAlmostEqual(model.focus_settle(focus, later(start, 5000), BERLIN), 1200)
        self.assertEqual(focus["state"], "idle")
        self.assertEqual(focus["history"], {"2026-11-15": 2400.0})

    def test_a_session_that_ended_yesterday_counts_for_yesterday(self):
        focus = model.empty_doc()["focus"]
        start = now(wall=at(BERLIN, 2026, 11, 15, 23, 0))
        model.focus_begin(focus, model.focus_plan(30), start)
        model.focus_settle(focus, later(start, 12 * 3600), BERLIN)          # the app is opened the next morning
        self.assertEqual(focus["history"], {"2026-11-15": 1800.0})

    def test_stopping_early_counts_what_was_done(self):
        focus = model.empty_doc()["focus"]
        start = now(wall=at(BERLIN, 2026, 11, 15, 9, 0))
        model.focus_begin(focus, model.focus_plan(30), start)
        self.assertAlmostEqual(model.focus_settle(focus, later(start, 420), BERLIN, stop=True), 420)
        self.assertEqual(focus["state"], "idle")

    def test_stats_goal_and_streak(self):
        focus = model.empty_doc()["focus"]
        focus["goal"] = 30
        focus["history"] = {"2026-11-12": 2000.0, "2026-11-13": 1800.0, "2026-11-14": 3600.0, "2026-11-10": 9000.0}
        moment = now(wall=at(BERLIN, 2026, 11, 15, 9, 0))
        stats = model.focus_stats(focus, moment, BERLIN)
        self.assertEqual((stats.today, stats.yesterday, stats.goal, stats.streak, stats.reached),
                         (0.0, 3600.0, 30, 3, False))
        model.focus_begin(focus, model.focus_plan(30), moment)
        stats = model.focus_stats(focus, later(moment, 1800), BERLIN)       # the running session counts
        self.assertEqual((stats.today, stats.streak, stats.reached), (1800.0, 4, True))


class WorldTest(unittest.TestCase):
    def test_add_and_remove(self):
        doc = model.empty_doc()
        self.assertTrue(model.world_add(doc, "Asia/Tokyo", "Tokyo"))
        self.assertFalse(model.world_add(doc, "Asia/Tokyo", "Again"))
        self.assertFalse(model.world_add(doc, "Mars/Olympus", "Nowhere"))
        self.assertTrue(model.world_remove(doc, "Asia/Tokyo"))
        self.assertFalse(model.world_remove(doc, "Asia/Tokyo"))

    def test_times_elsewhere(self):
        instant = at(BERLIN, 2026, 11, 15, 20, 0)
        tokyo = model.world_time("Asia/Tokyo", instant, BERLIN)
        self.assertEqual((tokyo.local, tokyo.offset, tokyo.day_shift, tokyo.daytime),
                         (dt.datetime(2026, 11, 16, 4, 0), 8 * 3600, 1, False))
        honolulu = model.world_time("Pacific/Honolulu", at(BERLIN, 2026, 11, 15, 8, 0), BERLIN)
        self.assertEqual((honolulu.local, honolulu.offset, honolulu.day_shift),
                         (dt.datetime(2026, 11, 14, 21, 0), -11 * 3600, -1))
        kolkata = model.world_time("Asia/Kolkata", instant, BERLIN)
        self.assertEqual(kolkata.offset, 4 * 3600 + 1800)
        self.assertIsNone(model.world_time("Not/AZone", instant, BERLIN))


class NormalizeTest(unittest.TestCase):
    def test_garbage_becomes_an_empty_document(self):
        for raw in (None, 7, "text", [], {"alarms": "no", "timers": 3, "stopwatch": [], "focus": 1, "world": {}}):
            self.assertEqual(model.normalize(raw), model.empty_doc())

    def test_entries_are_checked_one_by_one(self):
        raw = {
            "alarms": [{"id": "a1", "hour": 99, "minute": -5, "days": [0, 0, 9, "x", True], "snooze": 10 ** 9,
                        "enabled": "yes", "name": "x" * 500, "armed": float("nan")},
                       {"id": "a1", "hour": 7},                       # the same ID again
                       {"id": "../../x"}, {"hour": 7}, "junk", None],
            "timers": [{"id": "t1", "duration": 10 ** 12, "state": "running"},     # running without an anchor
                       {"id": "t2", "duration": 60, "state": "paused", "left": 500},
                       {"id": "t3", "duration": 0}],
            "stopwatch": {"state": "running", "elapsed": "1", "laps": [5, 3, "x", 9, -1, float("inf")]},
            "focus": {"state": "running", "segments": [{"kind": "focus", "seconds": 600}, {"kind": "nap"}],
                      "position": 1e9, "goal": 0, "history": {"2026-11-15": 100, "nonsense": 5, "2026-13-40": 5,
                                                              "2026-11-14": "many"}},
            "world": [{"zone": "Europe/Berlin"}, {"zone": "Europe/Berlin", "name": "again"},
                      {"zone": "../../etc/passwd"}, {"zone": 5}],
        }
        doc = model.normalize(raw)
        self.assertEqual(len(doc["alarms"]), 1)
        alarm = doc["alarms"][0]
        self.assertEqual((alarm["hour"], alarm["minute"], alarm["days"], alarm["snooze"], alarm["enabled"],
                          len(alarm["name"]), alarm["armed"]), (23, 0, [0], 120, False, model.MAX_NAME, 0.0))
        self.assertEqual([(t["id"], t["duration"], t["state"], t["left"]) for t in doc["timers"]],
                         [("t1", model.MAX_TIMER_SECONDS, "idle", float(model.MAX_TIMER_SECONDS)),
                          ("t2", 60, "paused", 500.0), ("t3", 1, "idle", 1.0)])
        self.assertEqual(doc["stopwatch"]["state"], "paused")       # running without an anchor
        self.assertEqual(doc["stopwatch"]["laps"], [5.0, 9.0, 1e9])
        self.assertEqual(doc["focus"]["state"], "paused")
        self.assertEqual(doc["focus"]["position"], 600.0)
        self.assertEqual(doc["focus"]["goal"], 5)
        self.assertEqual(doc["focus"]["history"], {"2026-11-15": 100.0})
        self.assertEqual(doc["world"], [{"zone": "Europe/Berlin", "name": "Berlin"}])

    def test_a_normalized_document_stays_the_same(self):
        doc = model.first_run_doc()
        doc["alarms"].append(model.make_alarm(7, 30, name="Wake up", days=(0, 1, 2, 3, 4), now_wall=5.0))
        model.timer_start(doc["timers"][0], now())
        model.stopwatch_start(doc["stopwatch"], now())
        model.stopwatch_lap(doc["stopwatch"], later(now(), 3))
        model.focus_begin(doc["focus"], model.focus_plan(60), now())
        model.world_add(doc, "Asia/Tokyo", "Tokyo")
        import json
        again = model.normalize(json.loads(json.dumps(doc)))
        self.assertEqual(again, doc)
        self.assertEqual([timer["name"] for timer in doc["timers"]],
                         ["1 min timer", "3 min timer", "5 min timer", "10 min timer"])

    def test_lists_are_capped(self):
        raw = {"alarms": [{"id": f"a{i}"} for i in range(1000)], "timers": [{"id": f"t{i}"} for i in range(1000)]}
        doc = model.normalize(raw)
        self.assertEqual((len(doc["alarms"]), len(doc["timers"])), (model.MAX_ALARMS, model.MAX_TIMERS))

    def test_system_clocks(self):
        first = model.system_now()
        second = model.system_now()
        self.assertGreaterEqual(second.boot, first.boot)
        self.assertEqual(first.boot_id, second.boot_id)
        self.assertEqual(model.elapsed_since(None, first), 0.0)


if __name__ == "__main__":
    unittest.main()
