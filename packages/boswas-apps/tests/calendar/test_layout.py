"""Where events go: the grid of a month, the bars of a week, the columns of a busy day."""

import datetime as dt
import unittest

from boswas_apps.calendar import layout, model
from boswas_apps.reminders import timeutil

BERLIN = timeutil.zone("Europe/Berlin")
MONDAY = dt.date(2026, 11, 16)


def occurrences(*events: dict, first=MONDAY, days=7):
    data = model.CalendarData.from_json({"calendars": [{"id": "c"}], "events": [
        {"calendar": "c", "summary": event["uid"], **event} for event in events]})
    begin = timeutil.to_epoch(dt.datetime(first.year, first.month, first.day), BERLIN)
    return data.occurrences(begin, begin + days * 86400, BERLIN)


def timed(uid, start, end, day="2026-11-17"):
    end_day = day if end >= start else "2026-11-18"
    return {"uid": uid, "start": f"{day}T{start}:00", "end": f"{end_day}T{end}:00"}


class GridTest(unittest.TestCase):
    def test_month_weeks(self):
        weeks = layout.month_weeks(2026, 11, 0)
        self.assertEqual((len(weeks), weeks[0][0], weeks[-1][-1]), (6, dt.date(2026, 10, 26), dt.date(2026, 12, 6)))
        sunday = layout.month_weeks(2026, 11, 6)
        self.assertEqual((sunday[0][0], sunday[0][0].weekday()), (dt.date(2026, 11, 1), 6))
        self.assertEqual(layout.month_weeks(2026, 2, 6)[0][0], dt.date(2026, 2, 1))
        for year, month in ((2026, 2), (2027, 2), (2024, 2), (2026, 12), (1, 1), (9999, 12)):
            try:
                grid = layout.month_weeks(year, month, 0)
            except OverflowError:
                continue
            self.assertTrue(all(len(week) == 7 for week in grid))

    def test_week_numbers(self):
        self.assertEqual(layout.week_number(MONDAY, 0), 47)
        self.assertEqual(layout.week_number(dt.date(2027, 1, 1), 0), 53)
        self.assertEqual(layout.week_number(dt.date(2026, 11, 15), 6), layout.week_number(dt.date(2026, 11, 21), 6))
        self.assertEqual(layout.week_start_of(dt.date(2026, 11, 18), 0), MONDAY)
        self.assertEqual(layout.week_start_of(dt.date(2026, 11, 18), 6), dt.date(2026, 11, 15))

    def test_add_months(self):
        self.assertEqual(layout.add_months(dt.date(2026, 11, 30), 3), dt.date(2027, 2, 1))
        self.assertEqual(layout.add_months(dt.date(2026, 1, 15), -1), dt.date(2025, 12, 1))
        self.assertEqual(layout.add_months(dt.date(9999, 12, 1), 5), dt.date(9999, 12, 1))


class BarsTest(unittest.TestCase):
    days = [MONDAY + dt.timedelta(days=offset) for offset in range(7)]

    def test_spanning_events_keep_one_row(self):
        found = occurrences({"uid": "trip", "start": "2026-11-14", "end": "2026-11-19", "all_day": True},
                            {"uid": "conf", "start": "2026-11-18", "end": "2026-11-25", "all_day": True},
                            {"uid": "day", "start": "2026-11-18", "end": "2026-11-19", "all_day": True},
                            timed("meet", "09:00", "10:00", "2026-11-18"), first=dt.date(2026, 11, 9), days=21)
        bars, more = layout.week_bars(found, self.days, 5)
        placed = {bar.occurrence.event.uid: (bar.first, bar.last, bar.row, bar.starts_before, bar.ends_after,
                                             bar.spanning) for bar in bars}
        self.assertEqual(placed, {"trip": (0, 2, 0, True, False, True), "conf": (2, 6, 1, False, True, True),
                                  "day": (2, 2, 2, False, False, True), "meet": (2, 2, 3, False, False, False)})
        self.assertEqual(more, {})

    def test_what_does_not_fit_is_counted(self):
        events = [timed(f"e{index}", f"{8 + index:02d}:00", f"{8 + index:02d}:30") for index in range(6)]
        events.append({"uid": "trip", "start": "2026-11-16", "end": "2026-11-19", "all_day": True})
        bars, more = layout.week_bars(occurrences(*events), self.days, 3)
        self.assertEqual(sorted(bar.occurrence.event.uid for bar in bars), ["e0", "trip"])
        self.assertEqual(more, {1: 5})                    # Tuesday: two shown, the last row says "+5 more"
        self.assertEqual(max(bar.row for bar in bars), 1)
        bars, more = layout.week_bars(occurrences(*events), self.days, 0)
        self.assertEqual((bars, more), ([], {0: 1, 1: 7, 2: 1}))
        bars, more = layout.week_bars(occurrences(*events), self.days, 7)
        self.assertEqual((len(bars), more), (7, {}))

    def test_an_event_that_ends_at_midnight_does_not_touch_the_next_day(self):
        found = occurrences(timed("late", "22:00", "00:00"), timed("night", "23:00", "01:00"))
        bars, _more = layout.week_bars(found, self.days, 5)
        self.assertEqual({bar.occurrence.event.uid: (bar.first, bar.last) for bar in bars},
                         {"late": (1, 1), "night": (1, 2)})


class BlocksTest(unittest.TestCase):
    def blocks(self, *events, day=dt.date(2026, 11, 17)):
        return {block.occurrence.event.uid: (block.start, block.end, block.column, block.columns)
                for block in layout.day_blocks(occurrences(*events), day)}

    def test_overlapping_events_stand_side_by_side(self):
        found = self.blocks(timed("a", "09:00", "10:00"), timed("b", "09:30", "11:00"), timed("c", "10:00", "10:30"),
                            timed("d", "13:00", "14:00"), timed("e", "13:00", "14:00"), timed("f", "13:00", "14:00"),
                            timed("g", "15:00", "16:00"))
        self.assertEqual(found, {"a": (540, 600, 0, 2), "b": (570, 660, 1, 2), "c": (600, 630, 0, 2),
                                 "d": (780, 840, 0, 3), "e": (780, 840, 1, 3), "f": (780, 840, 2, 3),
                                 "g": (900, 960, 0, 1)})

    def test_short_and_long_events(self):
        found = self.blocks(timed("instant", "09:00", "09:00"), timed("short", "09:10", "09:15"),
                            timed("overnight", "22:00", "02:00"), timed("last", "23:50", "23:55"),
                            {"uid": "allday", "start": "2026-11-17", "end": "2026-11-18", "all_day": True})
        self.assertEqual(found["instant"][:2], (540, 570))               # at least half an hour tall
        self.assertEqual(found["short"], (550, 580, 1, 2))
        self.assertEqual(found["overnight"][:2], (1320, 1440))
        self.assertEqual(found["last"][:2], (1410, 1440))
        self.assertNotIn("allday", found)
        next_day = self.blocks(timed("overnight", "22:00", "02:00"), timed("late", "22:00", "00:00"),
                               day=dt.date(2026, 11, 18))
        self.assertEqual(next_day, {"overnight": (0, 120, 0, 1)})

    def test_many_overlaps_are_laid_out_quickly(self):
        events = [timed(f"e{index}", f"{index % 24:02d}:{index % 60:02d}", f"{index % 24:02d}:59") for index in
                  range(400)]
        blocks = layout.day_blocks(occurrences(*events), dt.date(2026, 11, 17))
        self.assertEqual(len(blocks), 400)
        for block in blocks:
            self.assertLess(block.column, block.columns)

    def test_all_day_part(self):
        found = occurrences(timed("a", "09:00", "10:00"), {"uid": "d", "start": "2026-11-17", "end": "2026-11-18",
                                                           "all_day": True},
                            {"uid": "long", "start": "2026-11-17T09:00:00", "end": "2026-11-19T09:00:00"})
        self.assertEqual(sorted(occ.event.uid for occ in layout.all_day_part(found)), ["d", "long"])


if __name__ == "__main__":
    unittest.main()
