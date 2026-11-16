"""Recurrence rules: the examples of RFC 5545, the rules the editor makes, and rules written to do harm."""

import datetime as dt
import time
import unittest

from boswas_apps.calendar import recurrence as rec
from boswas_apps.reminders import timeutil

D = dt.datetime
NEW_YORK = timeutil.zone("America/New_York")


def take(rule: str, start: D, count: int = 400, **options) -> list[D]:
    found = []
    for moment in rec.iterate(rec.parse_rrule(rule), start, **options):
        found.append(moment)
        if len(found) >= count:
            break
    return found


def days(moments) -> list[tuple[int, int, int]]:
    return [(m.year, m.month, m.day) for m in moments]


class RfcExamplesTest(unittest.TestCase):
    """Section 3.8.5.3 of RFC 5545; the starts are in America/New_York as there."""
    start = D(1997, 9, 2, 9, 0)

    def test_daily(self):
        self.assertEqual(take("FREQ=DAILY;COUNT=10", self.start)[-1], D(1997, 9, 11, 9))
        until = take("FREQ=DAILY;UNTIL=19971224T000000Z", self.start,
                     instant=lambda m: timeutil.to_epoch(m, NEW_YORK))
        self.assertEqual((len(until), until[-1]), (113, D(1997, 12, 23, 9)))
        self.assertEqual(take("FREQ=DAILY;INTERVAL=2", self.start, 4)[-1], D(1997, 9, 8, 9))
        self.assertEqual(days(take("FREQ=DAILY;INTERVAL=10;COUNT=5", self.start)),
                         [(1997, 9, 2), (1997, 9, 12), (1997, 9, 22), (1997, 10, 2), (1997, 10, 12)])
        january = take("FREQ=YEARLY;UNTIL=20000131T140000Z;BYMONTH=1;BYDAY=SU,MO,TU,WE,TH,FR,SA", D(1998, 1, 1, 9))
        self.assertEqual(len(january), 93)
        self.assertEqual(january, take("FREQ=DAILY;UNTIL=20000131T140000Z;BYMONTH=1", D(1998, 1, 1, 9)))

    def test_weekly(self):
        self.assertEqual(len(take("FREQ=WEEKLY;COUNT=10", self.start)), 10)
        self.assertEqual(days(take("FREQ=WEEKLY;INTERVAL=2;WKST=SU", self.start, 4)),
                         [(1997, 9, 2), (1997, 9, 16), (1997, 9, 30), (1997, 10, 14)])
        self.assertEqual(days(take("FREQ=WEEKLY;UNTIL=19971007T000000Z;WKST=SU;BYDAY=TU,TH", self.start)),
                         [(1997, 9, d) for d in (2, 4, 9, 11, 16, 18, 23, 25, 30)] + [(1997, 10, 2)])
        every_other = take("FREQ=WEEKLY;INTERVAL=2;UNTIL=19971224T000000Z;WKST=SU;BYDAY=MO,WE,FR", D(1997, 9, 1, 9))
        self.assertEqual(len(every_other), 25)
        self.assertEqual(days(every_other)[-4:], [(1997, 12, 8), (1997, 12, 10), (1997, 12, 12), (1997, 12, 22)])
        self.assertEqual(days(take("FREQ=WEEKLY;INTERVAL=2;COUNT=8;WKST=SU;BYDAY=TU,TH", self.start)),
                         [(1997, 9, 2), (1997, 9, 4), (1997, 9, 16), (1997, 9, 18), (1997, 9, 30), (1997, 10, 2),
                          (1997, 10, 14), (1997, 10, 16)])

    def test_the_week_start_matters(self):
        start = D(1997, 8, 5, 9)
        self.assertEqual(days(take("FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=MO", start)),
                         [(1997, 8, 5), (1997, 8, 10), (1997, 8, 19), (1997, 8, 24)])
        self.assertEqual(days(take("FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=SU", start)),
                         [(1997, 8, 5), (1997, 8, 17), (1997, 8, 19), (1997, 8, 31)])

    def test_monthly(self):
        self.assertEqual(days(take("FREQ=MONTHLY;COUNT=10;BYDAY=1FR", D(1997, 9, 5, 9)))[-3:],
                         [(1998, 4, 3), (1998, 5, 1), (1998, 6, 5)])
        self.assertEqual(days(take("FREQ=MONTHLY;INTERVAL=2;COUNT=10;BYDAY=1SU,-1SU", D(1997, 9, 7, 9))),
                         [(1997, 9, 7), (1997, 9, 28), (1997, 11, 2), (1997, 11, 30), (1998, 1, 4), (1998, 1, 25),
                          (1998, 3, 1), (1998, 3, 29), (1998, 5, 3), (1998, 5, 31)])
        self.assertEqual(days(take("FREQ=MONTHLY;COUNT=6;BYDAY=-2MO", D(1997, 9, 22, 9))),
                         [(1997, 9, 22), (1997, 10, 20), (1997, 11, 17), (1997, 12, 22), (1998, 1, 19), (1998, 2, 16)])
        self.assertEqual(days(take("FREQ=MONTHLY;BYMONTHDAY=-3", D(1997, 9, 28, 9), 6)),
                         [(1997, 9, 28), (1997, 10, 29), (1997, 11, 28), (1997, 12, 29), (1998, 1, 29), (1998, 2, 26)])
        self.assertEqual(days(take("FREQ=MONTHLY;COUNT=10;BYMONTHDAY=2,15", self.start))[-2:],
                         [(1998, 1, 2), (1998, 1, 15)])
        self.assertEqual(days(take("FREQ=MONTHLY;COUNT=10;BYMONTHDAY=1,-1", D(1997, 9, 30, 9)))[:5],
                         [(1997, 9, 30), (1997, 10, 1), (1997, 10, 31), (1997, 11, 1), (1997, 11, 30)])
        self.assertEqual(days(take("FREQ=MONTHLY;INTERVAL=18;COUNT=10;BYMONTHDAY=10,11,12,13,14,15",
                                   D(1997, 9, 10, 9)))[5:8], [(1997, 9, 15), (1999, 3, 10), (1999, 3, 11)])
        self.assertEqual(days(take("FREQ=MONTHLY;INTERVAL=2;BYDAY=TU", self.start, 8))[5:],
                         [(1997, 11, 4), (1997, 11, 11), (1997, 11, 18)])

    def test_yearly(self):
        self.assertEqual(days(take("FREQ=YEARLY;COUNT=10;BYMONTH=6,7", D(1997, 6, 10, 9)))[:4],
                         [(1997, 6, 10), (1997, 7, 10), (1998, 6, 10), (1998, 7, 10)])
        self.assertEqual(days(take("FREQ=YEARLY;INTERVAL=2;COUNT=10;BYMONTH=1,2,3", D(1997, 3, 10, 9)))[:5],
                         [(1997, 3, 10), (1999, 1, 10), (1999, 2, 10), (1999, 3, 10), (2001, 1, 10)])
        self.assertEqual(days(take("FREQ=YEARLY;INTERVAL=3;COUNT=10;BYYEARDAY=1,100,200", D(1997, 1, 1, 9))),
                         [(1997, 1, 1), (1997, 4, 10), (1997, 7, 19), (2000, 1, 1), (2000, 4, 9), (2000, 7, 18),
                          (2003, 1, 1), (2003, 4, 10), (2003, 7, 19), (2006, 1, 1)])
        self.assertEqual(days(take("FREQ=YEARLY;BYDAY=20MO", D(1997, 5, 19, 9), 3)),
                         [(1997, 5, 19), (1998, 5, 18), (1999, 5, 17)])
        self.assertEqual(days(take("FREQ=YEARLY;BYWEEKNO=20;BYDAY=MO", D(1997, 5, 12, 9), 3)),
                         [(1997, 5, 12), (1998, 5, 11), (1999, 5, 17)])
        self.assertEqual(days(take("FREQ=YEARLY;BYMONTH=3;BYDAY=TH", D(1997, 3, 13, 9), 5)),
                         [(1997, 3, 13), (1997, 3, 20), (1997, 3, 27), (1998, 3, 5), (1998, 3, 12)])
        self.assertEqual(days(take("FREQ=YEARLY;BYDAY=TH;BYMONTH=6,7,8", D(1997, 6, 5, 9), 14))[12:],
                         [(1997, 8, 28), (1998, 6, 4)])
        self.assertEqual(days(take("FREQ=MONTHLY;BYDAY=FR;BYMONTHDAY=13", self.start, 5))[1:],
                         [(1998, 2, 13), (1998, 3, 13), (1998, 11, 13), (1999, 8, 13)])
        self.assertEqual(days(take("FREQ=MONTHLY;BYDAY=SA;BYMONTHDAY=7,8,9,10,11,12,13", D(1997, 9, 13, 9), 4)),
                         [(1997, 9, 13), (1997, 10, 11), (1997, 11, 8), (1997, 12, 13)])
        self.assertEqual(days(take("FREQ=YEARLY;INTERVAL=4;BYMONTH=11;BYDAY=TU;BYMONTHDAY=2,3,4,5,6,7,8",
                                   D(1996, 11, 5, 9), 3)), [(1996, 11, 5), (2000, 11, 7), (2004, 11, 2)])

    def test_set_positions(self):
        self.assertEqual(days(take("FREQ=MONTHLY;COUNT=3;BYDAY=TU,WE,TH;BYSETPOS=3", D(1997, 9, 4, 9))),
                         [(1997, 9, 4), (1997, 10, 7), (1997, 11, 6)])
        self.assertEqual(days(take("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-2", D(1997, 9, 29, 9), 4)),
                         [(1997, 9, 29), (1997, 10, 30), (1997, 11, 27), (1997, 12, 30)])

    def test_small_steps(self):
        self.assertEqual(take("FREQ=HOURLY;INTERVAL=3;UNTIL=19970902T170000Z", self.start),
                         [D(1997, 9, 2, 9), D(1997, 9, 2, 12), D(1997, 9, 2, 15)])
        self.assertEqual(take("FREQ=MINUTELY;INTERVAL=15;COUNT=6", self.start)[-1], D(1997, 9, 2, 10, 15))
        self.assertEqual(take("FREQ=MINUTELY;INTERVAL=90;COUNT=4", self.start)[-1], D(1997, 9, 2, 13, 30))
        daily = take("FREQ=DAILY;BYHOUR=9,10,11,12,13,14,15,16;BYMINUTE=0,20,40", self.start, 26)
        minutely = take("FREQ=MINUTELY;INTERVAL=20;BYHOUR=9,10,11,12,13,14,15,16", self.start, 26)
        self.assertEqual(daily, minutely)
        self.assertEqual(daily[23:], [D(1997, 9, 2, 16, 40), D(1997, 9, 3, 9, 0), D(1997, 9, 3, 9, 20)])
        self.assertEqual(take("FREQ=SECONDLY;INTERVAL=30;BYMINUTE=0;COUNT=5", self.start)[-1], D(1997, 9, 2, 11, 0))

    def test_an_invalid_date_is_skipped(self):
        self.assertEqual(days(take("FREQ=MONTHLY;BYMONTHDAY=31", D(2026, 1, 31, 8), 5)),
                         [(2026, 1, 31), (2026, 3, 31), (2026, 5, 31), (2026, 7, 31), (2026, 8, 31)])
        self.assertEqual(days(take("FREQ=MONTHLY", D(2026, 1, 30, 8), 3)), [(2026, 1, 30), (2026, 3, 30), (2026, 4, 30)])
        self.assertEqual(days(take("FREQ=YEARLY", D(2024, 2, 29, 8), 3)), [(2024, 2, 29), (2028, 2, 29), (2032, 2, 29)])
        self.assertEqual(days(take("FREQ=MONTHLY;BYDAY=5MO", D(2026, 3, 30, 8), 3)),
                         [(2026, 3, 30), (2026, 6, 29), (2026, 8, 31)])


class SemanticsTest(unittest.TestCase):
    def test_the_start_is_always_the_first_occurrence_and_counts(self):
        # 2 September 1997 is a Tuesday; the rule asks for Mondays.
        self.assertEqual(days(take("FREQ=WEEKLY;BYDAY=MO;COUNT=3", D(1997, 9, 2, 9))),
                         [(1997, 9, 2), (1997, 9, 8), (1997, 9, 15)])
        self.assertEqual(take("FREQ=DAILY;UNTIL=19900101T000000", D(1997, 9, 2, 9)), [D(1997, 9, 2, 9)])
        self.assertEqual(take("FREQ=DAILY;COUNT=1", D(1997, 9, 2, 9)), [D(1997, 9, 2, 9)])

    def test_until_forms(self):
        self.assertEqual(len(take("FREQ=DAILY;UNTIL=19970905", D(1997, 9, 2, 9))), 4)            # the whole day
        self.assertEqual(len(take("FREQ=DAILY;UNTIL=19970905T085959", D(1997, 9, 2, 9))), 3)     # a wall time
        utc = lambda m: timeutil.to_epoch(m, NEW_YORK)                                           # noqa: E731
        self.assertEqual(len(take("FREQ=DAILY;UNTIL=19970905T130000Z", D(1997, 9, 2, 9), instant=utc)), 4)
        self.assertEqual(len(take("FREQ=DAILY;UNTIL=19970905T125959Z", D(1997, 9, 2, 9), instant=utc)), 3)
        self.assertEqual(len(take("FREQ=DAILY;COUNT=10;UNTIL=19970905T130000Z", D(1997, 9, 2, 9), instant=utc)), 4)
        self.assertEqual(len(take("FREQ=DAILY;COUNT=2;UNTIL=19970905T130000Z", D(1997, 9, 2, 9), instant=utc)), 2)

    def test_windows_and_skipping(self):
        rule = rec.parse_rrule("FREQ=WEEKLY;BYDAY=MO,WE")
        start = D(2001, 1, 1, 8)
        window = rec.between(rule, start, D(2026, 11, 16), D(2026, 11, 23))
        self.assertEqual(window, [D(2026, 11, 16, 8), D(2026, 11, 18, 8)])
        self.assertEqual(rec.first_from(rule, start, D(2026, 11, 17)), D(2026, 11, 18, 8))
        self.assertEqual(rec.first_from(rule, start, D(1990, 1, 1)), start)
        counted = rec.parse_rrule("FREQ=DAILY;COUNT=1000")
        self.assertEqual(rec.between(counted, start, D(2003, 9, 26), D(2003, 9, 29)),
                         [D(2003, 9, 26, 8), D(2003, 9, 27, 8)])                   # the 999th and 1000th
        self.assertIsNone(rec.first_from(counted, start, D(2010, 1, 1)))
        self.assertEqual(rec.count_before(counted, start, D(2001, 1, 11, 8)), 10)
        self.assertEqual(len(rec.between(rec.parse_rrule("FREQ=DAILY"), start, D(2001, 1, 1), D(2030, 1, 1),
                                         limit=25)), 25)

    def test_skipping_gives_the_same_as_walking(self):
        for text in ("FREQ=DAILY;INTERVAL=3", "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,SA;WKST=SU",
                     "FREQ=MONTHLY;BYDAY=-1FR", "FREQ=MONTHLY;INTERVAL=5;BYMONTHDAY=31",
                     "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", "FREQ=HOURLY;INTERVAL=7",
                     "FREQ=MINUTELY;INTERVAL=45;BYDAY=MO", "FREQ=YEARLY;INTERVAL=3;BYWEEKNO=1;BYDAY=MO",
                     "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1,-1", "FREQ=DAILY;COUNT=700;INTERVAL=2",
                     "FREQ=MONTHLY;COUNT=40;BYDAY=2TU"):
            rule = rec.parse_rrule(text)
            start = D(2019, 1, 29, 10, 30)
            low, high = D(2022, 3, 1), D(2022, 9, 1)
            walked = []
            for moment in rec.iterate(rule, start):
                if moment >= high:
                    break
                if moment >= low:
                    walked.append(moment)
            self.assertEqual(rec.between(rule, start, low, high, limit=10 ** 6), walked, text)

    def test_week_numbers(self):
        self.assertEqual(rec.week_number(dt.date(2026, 1, 1)), (1, 53))
        self.assertEqual(rec.week_number(dt.date(2027, 1, 1)), (53, 53))
        self.assertEqual(rec.week_number(dt.date(2024, 12, 30)), (1, 52))
        self.assertEqual(rec.week_number(dt.date(2026, 11, 16)), (47, 53))
        for day in (dt.date(2021, 1, 3), dt.date(2026, 6, 15), dt.date(2020, 12, 31)):
            self.assertEqual(rec.week_number(day)[0], day.isocalendar()[1])
        self.assertEqual(rec.week_number(dt.date(2026, 1, 3), wkst=6), (53, 53))      # weeks that start on Sunday


class TextTest(unittest.TestCase):
    def test_round_trip(self):
        for text in ("FREQ=DAILY", "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE,FR", "FREQ=MONTHLY;COUNT=10;BYDAY=1FR,-1SU",
                     "FREQ=YEARLY;UNTIL=20301231;BYMONTH=11;BYMONTHDAY=17", "FREQ=DAILY;UNTIL=20301231T225959Z",
                     "FREQ=DAILY;UNTIL=20301231T225959", "FREQ=WEEKLY;BYDAY=SU;WKST=SU",
                     "FREQ=MONTHLY;BYDAY=MO,TU;BYHOUR=9,17;BYMINUTE=0,30;BYSECOND=0;BYSETPOS=-1",
                     "FREQ=YEARLY;BYWEEKNO=20,-1;BYYEARDAY=1,-1"):
            self.assertEqual(rec.format_rrule(rec.parse_rrule(text)), text)
        self.assertEqual(rec.format_rrule(rec.parse_rrule("rrule:freq=weekly; byday = mo ,tu;interval=1")),
                         "FREQ=WEEKLY;BYDAY=MO,TU")

    def test_what_cannot_be_a_rule(self):
        for text in ("", "FREQ=FORTNIGHTLY", "INTERVAL=2", "FREQ=DAILY;INTERVAL=0", "FREQ=DAILY;INTERVAL=-1",
                     "FREQ=DAILY;COUNT=0", "FREQ=DAILY;COUNT=ten", "FREQ=DAILY;FREQ=WEEKLY", "FREQ=WEEKLY;BYDAY=XX",
                     "FREQ=MONTHLY;BYDAY=54MO", "FREQ=MONTHLY;BYDAY=0MO", "FREQ=MONTHLY;BYMONTHDAY=0",
                     "FREQ=MONTHLY;BYMONTHDAY=32", "FREQ=YEARLY;BYMONTH=13", "FREQ=DAILY;BYHOUR=24",
                     "FREQ=DAILY;UNTIL=tomorrow", "FREQ=DAILY;UNTIL=20261340", "FREQ=DAILY;WKST=XX",
                     "FREQ=DAILY;INTERVAL=", "FREQ=DAILY;BYSETPOS=1e3", "FREQ=DAILY;" + "X" * 5000,
                     "FREQ=DAILY;BYMONTHDAY=" + ",".join(["1"] * 2000), "FREQ=DAILY;INTERVAL=99999999999"):
            with self.assertRaises(rec.RecurrenceError, msg=text):
                rec.parse_rrule(text)
        with self.assertRaises(rec.RecurrenceError):
            rec.parse_rrule(None)

    def test_the_editors_rules(self):
        tuesday = dt.date(2026, 11, 17)                                  # the third Tuesday of November
        cases = [
            (dict(kind="daily"), "FREQ=DAILY", "Every day"),
            (dict(kind="daily", interval=3, count=5), "FREQ=DAILY;INTERVAL=3;COUNT=5", "Every 3 days, 5 times"),
            (dict(kind="weekly"), "FREQ=WEEKLY;BYDAY=TU", "Every week on Tue"),
            (dict(kind="weekly", interval=2, weekdays=(0, 2, 4), until=dt.date(2026, 12, 24)),
             "FREQ=WEEKLY;INTERVAL=2;UNTIL=20261224;BYDAY=MO,WE,FR", "Every 2 weeks on Mon, Wed, Fri, until 24 Dec 2026"),
            (dict(kind="monthly"), "FREQ=MONTHLY;BYMONTHDAY=17", "Every month on day 17"),
            (dict(kind="monthly", monthly="weekday"), "FREQ=MONTHLY;BYDAY=3TU", "Every month on the third Tuesday"),
            (dict(kind="yearly"), "FREQ=YEARLY", "Every year on 17 November"),
            (dict(kind="yearly", count=1), "FREQ=YEARLY;COUNT=1", "Every year on 17 November, 1 time"),
        ]
        for options, text, words in cases:
            rule = rec.simple_rule(start=tuesday, **options)
            self.assertEqual(rec.format_rrule(rule), text)
            self.assertEqual(rec.describe(rule, tuesday), words)
            simple = rec.classify(rule, tuesday)
            self.assertEqual(simple.kind, options["kind"])
            again = rec.simple_rule(simple.kind, tuesday, interval=simple.interval, weekdays=simple.weekdays,
                                    monthly=simple.monthly, count=simple.count, until=simple.until)
            self.assertEqual(again, rule)
        last = rec.simple_rule("monthly", dt.date(2026, 11, 24), monthly="last")
        self.assertEqual(rec.format_rrule(last), "FREQ=MONTHLY;BYDAY=-1TU")
        self.assertEqual(rec.describe(last, dt.date(2026, 11, 24)), "Every month on the last Tuesday")
        self.assertEqual(rec.classify(rec.parse_rrule("FREQ=MONTHLY;BYDAY=MO;BYSETPOS=2"), tuesday).kind, "custom")
        self.assertEqual(rec.classify(rec.parse_rrule("FREQ=HOURLY"), tuesday).kind, "custom")
        self.assertTrue(rec.describe(rec.parse_rrule("FREQ=HOURLY"), tuesday).startswith("Custom: FREQ=HOURLY"))
        self.assertEqual(rec.classify(rec.parse_rrule("FREQ=DAILY;UNTIL=20261224T225959Z"), tuesday).until,
                         dt.date(2026, 12, 24))


class HostileRulesTest(unittest.TestCase):
    def timed(self, rule: str, start: D, limit: float = 1.5, **options):
        began = time.monotonic()
        found = take(rule, start, 50, **options)
        self.assertLess(time.monotonic() - began, limit, rule)
        return found

    def test_rules_that_never_match_end(self):
        for rule in ("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30", "FREQ=MONTHLY;BYMONTHDAY=31;BYMONTH=2,4,6,9,11",
                     "FREQ=DAILY;INTERVAL=7;BYDAY=TU", "FREQ=DAILY;BYDAY=MO;BYSETPOS=2",
                     "FREQ=SECONDLY;BYMONTH=2;BYMONTHDAY=31", "FREQ=YEARLY;BYWEEKNO=53;BYMONTH=6",
                     "FREQ=MINUTELY;BYHOUR=3;BYDAY=MO;BYMONTHDAY=30;BYMONTH=2"):
            self.assertEqual(self.timed(rule, D(2026, 11, 16, 8)), [D(2026, 11, 16, 8)], rule)

    def test_huge_counts_and_tiny_steps_are_entered_where_asked(self):
        found = self.timed("FREQ=SECONDLY", D(1970, 1, 1), skip_to=D(2026, 11, 16))
        self.assertEqual(found[1], D(2026, 11, 15, 23, 59, 59))
        found = self.timed("FREQ=SECONDLY;COUNT=999999999", D(1990, 1, 1), skip_to=D(2021, 1, 1))
        self.assertGreaterEqual(found[1], D(2020, 12, 31, 23, 59, 58))
        found = self.timed("FREQ=MINUTELY;BYHOUR=9;COUNT=1000000", D(2020, 2, 1), skip_to=D(2026, 1, 1))
        self.assertEqual(found[1], D(2026, 1, 1, 9, 0))
        found = self.timed("FREQ=SECONDLY;BYMONTH=1", D(2026, 2, 1))
        self.assertEqual(found[1], D(2027, 1, 1))
        found = self.timed("FREQ=DAILY;INTERVAL=1000000", D(2026, 1, 1))
        self.assertLessEqual(len(found), 4)                             # the fourth would be past the year 9999
        found = self.timed("FREQ=YEARLY;INTERVAL=1000000;COUNT=5", D(2026, 1, 1))
        self.assertEqual(found, [D(2026, 1, 1)])

    def test_the_budget_stops_a_walk(self):
        began = time.monotonic()
        rule = rec.parse_rrule("FREQ=SECONDLY;BYSECOND=59;COUNT=999999;UNTIL=20991231T000000Z")
        found = list(rec.iterate(rule, D(2026, 1, 1), budget=20_000))
        self.assertLess(len(found), 20_000)
        self.assertLess(time.monotonic() - began, 2.0)

    def test_big_lists(self):
        rule = "FREQ=YEARLY;BYYEARDAY=" + ",".join(str(n) for n in range(1, 367)) + ";BYSETPOS=" + ",".join(
            str(n) for n in range(-366, 0))
        found = self.timed(rule, D(2026, 1, 1, 8))
        self.assertEqual(found[:3], [D(2026, 1, 1, 8), D(2026, 1, 2, 8), D(2026, 1, 3, 8)])


if __name__ == "__main__":
    unittest.main()
