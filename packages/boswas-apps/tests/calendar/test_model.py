"""Calendar's data: occurrences in time zones, and the three ways to edit or delete in a series."""

import datetime as dt
import json
import time
import unittest
from dataclasses import replace

from boswas_apps.calendar import model, recurrence
from boswas_apps.reminders import timeutil

D = dt.datetime
BERLIN = timeutil.zone("Europe/Berlin")
NEW_YORK = timeutil.zone("America/New_York")
TOKYO = timeutil.zone("Asia/Tokyo")


def at(*args, zone=BERLIN) -> float:
    return timeutil.to_epoch(D(*args), zone)


def data_with(*events: dict) -> model.CalendarData:
    return model.CalendarData.from_json({
        "calendars": [{"id": "c1", "name": "Personal", "color": "iris"},
                      {"id": "c2", "name": "Work", "color": "mint"}],
        "events": [{"calendar": "c1", "summary": event.get("uid", ""), **event} for event in events]})


def ev(uid, start, end, **fields) -> dict:
    return {"uid": uid, "start": start, "end": end, **fields}


def starts(occurrences) -> list[D]:
    return [occ.start for occ in occurrences]


WEEK = (at(2026, 11, 16), at(2026, 11, 23))


class OccurrenceTest(unittest.TestCase):
    def test_single_events_in_a_window(self):
        data = data_with(ev("in", "2026-11-17T09:00:00", "2026-11-17T10:00:00"),
                         ev("before", "2026-11-15T09:00:00", "2026-11-15T10:00:00"),
                         ev("ends-at-start", "2026-11-15T23:00:00", "2026-11-16T00:00:00"),
                         ev("over-start", "2026-11-15T23:00:00", "2026-11-16T00:30:00"),
                         ev("starts-at-end", "2026-11-23T00:00:00", "2026-11-23T01:00:00"),
                         ev("instant", "2026-11-18T12:00:00", "2026-11-18T12:00:00"),
                         ev("instant-at-start", "2026-11-16T00:00:00", "2026-11-16T00:00:00"))
        found = data.occurrences(*WEEK, BERLIN)
        self.assertEqual([occ.event.uid for occ in found], ["over-start", "instant-at-start", "in", "instant"])
        self.assertFalse(any(occ.recurring or occ.recurrence_id for occ in found))

    def test_floating_and_zoned_times(self):
        data = data_with(ev("floating", "2026-11-17T09:00:00", "2026-11-17T10:00:00"),
                         ev("ny", "2026-11-17T09:00:00", "2026-11-17T10:00:00", tz="America/New_York"),
                         ev("utc", "2026-11-17T09:00:00", "2026-11-17T10:00:00", tz="UTC"))
        in_berlin = {occ.event.uid: occ.start for occ in data.occurrences(*WEEK, BERLIN)}
        self.assertEqual(in_berlin, {"floating": D(2026, 11, 17, 9), "ny": D(2026, 11, 17, 15),
                                     "utc": D(2026, 11, 17, 10)})
        in_tokyo = {occ.event.uid: occ.start for occ in data.occurrences(at(2026, 11, 16, zone=TOKYO),
                                                                         at(2026, 11, 23, zone=TOKYO), TOKYO)}
        self.assertEqual(in_tokyo, {"floating": D(2026, 11, 17, 9), "ny": D(2026, 11, 17, 23),
                                    "utc": D(2026, 11, 17, 18)})

    def test_all_day_events_are_days_wherever_you_are(self):
        data = data_with(ev("day", "2026-11-17", "2026-11-18", all_day=True),
                         ev("trip", "2026-11-20", "2026-11-25", all_day=True))
        for zone in (BERLIN, NEW_YORK, TOKYO):
            found = {occ.event.uid: occ for occ in data.occurrences(at(2026, 11, 16, zone=zone),
                                                                    at(2026, 11, 23, zone=zone), zone)}
            self.assertEqual((found["day"].start, found["day"].end, found["day"].last_day),
                             (D(2026, 11, 17), D(2026, 11, 18), dt.date(2026, 11, 17)))
            self.assertFalse(found["day"].last_day > found["day"].start.date())
            self.assertEqual(found["trip"].last_day, dt.date(2026, 11, 24))
            self.assertTrue(found["trip"].multi_day)

    def test_weekly_series_keeps_its_wall_time_across_summer_time(self):
        data = data_with(ev("w", "2026-03-16T09:00:00", "2026-03-16T10:00:00", tz="Europe/Berlin",
                            rrule="FREQ=WEEKLY"))
        found = data.occurrences(at(2026, 3, 16), at(2026, 4, 13), BERLIN)
        self.assertEqual(starts(found), [D(2026, 3, 16, 9), D(2026, 3, 23, 9), D(2026, 3, 30, 9), D(2026, 4, 6, 9)])
        self.assertEqual(found[2].start_epoch - found[1].start_epoch, 7 * 86400 - 3600)
        self.assertEqual([occ.end_epoch - occ.start_epoch for occ in found], [3600.0] * 4)
        # Seen from New York (which changed its clocks three weeks earlier) the meeting moves by an hour.
        seen = data.occurrences(at(2026, 3, 16), at(2026, 4, 13), NEW_YORK)
        self.assertEqual([occ.start.hour for occ in seen], [4, 4, 3, 3])
        self.assertEqual([occ.recurrence_id for occ in found][:2], ["2026-03-16T09:00:00", "2026-03-23T09:00:00"])

    def test_an_occurrence_in_the_skipped_hour(self):
        data = data_with(ev("night", "2026-03-27T02:30:00", "2026-03-27T03:30:00", tz="Europe/Berlin",
                            rrule="FREQ=DAILY;COUNT=4"))
        found = data.occurrences(at(2026, 3, 27), at(2026, 3, 31), BERLIN)
        self.assertEqual(starts(found), [D(2026, 3, 27, 2, 30), D(2026, 3, 28, 2, 30), D(2026, 3, 29, 3, 30),
                                         D(2026, 3, 30, 2, 30)])

    def test_exceptions_rdates_and_overrides(self):
        data = data_with(
            ev("s", "2026-11-16T09:00:00", "2026-11-16T10:00:00", rrule="FREQ=DAILY;COUNT=5",
               exdates=["2026-11-17T09:00:00"], rdates=["2026-11-21T15:00:00", "2026-11-16T09:00:00"]),
            ev("s", "2026-11-19T18:00:00", "2026-11-19T20:00:00", recurrence_id="2026-11-18T09:00:00",
               summary="moved"))
        found = data.occurrences(*WEEK, BERLIN)
        self.assertEqual([(occ.start, occ.event.summary, occ.is_override) for occ in found],
                         [(D(2026, 11, 16, 9), "s", False), (D(2026, 11, 19, 9), "s", False),
                          (D(2026, 11, 19, 18), "moved", True), (D(2026, 11, 20, 9), "s", False),
                          (D(2026, 11, 21, 15), "s", False)])
        self.assertEqual(found[2].recurrence_id, "2026-11-18T09:00:00")
        self.assertEqual(found[2].master.summary, "s")
        # The override is found in a window that only holds its new time.
        self.assertEqual(len(data.occurrences(at(2026, 11, 19, 17), at(2026, 11, 19, 21), BERLIN)), 1)
        self.assertEqual(data.occurrence("s", "2026-11-18T09:00:00", BERLIN).event.summary, "moved")
        self.assertEqual(data.occurrence("s", "2026-11-20T09:00:00", BERLIN).start, D(2026, 11, 20, 9))
        self.assertIsNone(data.occurrence("s", "2026-11-17T09:00:00", BERLIN))       # deleted
        self.assertEqual(data.occurrence("s", "", BERLIN).start, D(2026, 11, 16, 9))

    def test_an_override_without_its_series_stands_alone(self):
        data = data_with(ev("orphan", "2026-11-19T18:00:00", "2026-11-19T20:00:00",
                            recurrence_id="2026-11-18T09:00:00"))
        found = data.occurrences(*WEEK, BERLIN)
        self.assertEqual(starts(found), [D(2026, 11, 19, 18)])
        data.delete_occurrence(found[0], "this")
        self.assertEqual(len(data), 0)

    def test_by_start_and_calendar_filter(self):
        data = data_with(ev("long", "2026-11-10T09:00:00", "2026-11-20T10:00:00"),
                         ev("work", "2026-11-17T09:00:00", "2026-11-17T10:00:00", calendar="c2"))
        self.assertEqual(len(data.occurrences(*WEEK, BERLIN)), 2)
        self.assertEqual([occ.event.uid for occ in data.occurrences(*WEEK, BERLIN, by_start=True)], ["work"])
        self.assertEqual([occ.event.uid for occ in data.occurrences(*WEEK, BERLIN, calendars={"c1"})], ["long"])

    def test_many_events_are_quick(self):
        events = [ev(f"e{i}", f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}T{i % 24:02d}:00:00",
                     f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}T{i % 24:02d}:30:00") for i in range(20000)]
        events += [ev(f"r{i}", "2015-01-05T08:00:00", "2015-01-05T09:00:00", rrule="FREQ=WEEKLY;BYDAY=MO,TH")
                   for i in range(300)]
        data = data_with(*events)
        began = time.monotonic()
        found = data.occurrences(at(2026, 11, 1), at(2026, 12, 1), BERLIN)
        self.assertLess(time.monotonic() - began, 3.0)
        self.assertGreater(len(found), 2000)

    def test_search(self):
        data = data_with(ev("a", "2026-11-17T09:00:00", "2026-11-17T10:00:00", summary="Zahnärztin", location="Köln"),
                         ev("b", "2020-01-06T09:00:00", "2020-01-06T10:00:00", rrule="FREQ=WEEKLY",
                            summary="Team sync", description="weekly Köln call"),
                         ev("c", "2026-11-18T09:00:00", "2026-11-18T10:00:00", summary="Lunch"))
        now = at(2026, 11, 15, 12)
        self.assertEqual([occ.event.uid for occ in data.search("koln", BERLIN, now)], ["b", "a"])
        self.assertEqual(data.search("KÖLN call", BERLIN, now)[0].start, D(2026, 11, 16, 9))   # the next one
        self.assertEqual([occ.event.uid for occ in data.search("zahnarztin", BERLIN, now)], ["a"])
        self.assertEqual(data.search("   ", BERLIN, now), [])
        self.assertEqual(data.search("nothing", BERLIN, now), [])

    def test_reminders(self):
        data = data_with(ev("a", "2026-11-17T09:00:00", "2026-11-17T10:00:00", reminders=[15, 1440]),
                         ev("b", "2026-11-16T09:00:00", "2026-11-16T09:30:00", rrule="FREQ=DAILY", reminders=[0]),
                         ev("c", "2026-11-17T09:00:00", "2026-11-17T10:00:00"))
        due = data.reminders_between(at(2026, 11, 16, 8), at(2026, 11, 17, 9), BERLIN)
        self.assertEqual([(item.occurrence.event.uid, item.minutes, timeutil.from_epoch(item.when, BERLIN))
                          for item in due],
                         [("b", 0, D(2026, 11, 16, 9)), ("a", 1440, D(2026, 11, 16, 9)),
                          ("a", 15, D(2026, 11, 17, 8, 45)), ("b", 0, D(2026, 11, 17, 9))])


class ValidationTest(unittest.TestCase):
    def test_hostile_documents(self):
        for raw in (None, [], "x", {"calendars": 5, "events": "many"}, {"calendars": [None, 3, {"id": "../x"}]}):
            data = model.CalendarData.from_json(raw)
            self.assertEqual((data.calendars, len(data)), ([], 0))
        data = model.CalendarData.from_json({
            "calendars": [{"id": "c1", "name": " A  B ", "color": "blood"}, {"id": "c1"}, {"id": "c2", "visible": 0}],
            "events": [
                {"uid": "ok", "calendar": "c1", "start": "2026-11-17T09:00:00", "end": "2026-11-17T08:00:00",
                 "summary": "x" * 9999, "tz": "Mars/Base", "rrule": "FREQ=NEVER", "reminders": [15, "x", 15, 10 ** 9],
                 "extra": ["STATUS:CONFIRMED", "BAD\nLINE", 5], "sequence": -4, "created": "then"},
                {"uid": "allday", "calendar": "c1", "all_day": True, "start": "2026-11-17T09:00:00",
                 "end": "2026-11-17", "tz": "Europe/Berlin", "exdates": ["2026-11-18T10:00:00", "nope"]},
                {"uid": "no-calendar", "calendar": "zz", "start": "2026-11-17", "end": "2026-11-18"},
                {"uid": "", "calendar": "c1", "start": "2026-11-17", "end": "2026-11-18"},
                {"uid": "bad-start", "calendar": "c1", "start": "17.11.2026", "end": "2026-11-18"},
                {"uid": "aware", "calendar": "c1", "start": "2026-11-17T09:00:00+01:00", "end": ""},
                {"uid": "x\ny", "calendar": "c1", "start": "2026-11-17", "end": "2026-11-18"}, "junk"]})
        self.assertEqual([(c.id, c.name, c.color, c.visible) for c in data.calendars],
                         [("c1", "A B", "iris", True), ("c2", "Calendar", "iris", True)])
        ok = data.get("ok")
        self.assertEqual((ok.end, len(ok.summary), ok.tz, ok.rrule, ok.reminders, ok.extra, ok.sequence, ok.created),
                         ("2026-11-17T09:00:00", model.MAX_SUMMARY, "", "", (15,), ("STATUS:CONFIRMED",), 0, 0))
        allday = data.get("allday")
        self.assertEqual((allday.start, allday.end, allday.tz, allday.exdates),
                         ("2026-11-17", "2026-11-18", "", ("2026-11-18",)))
        self.assertEqual(len(data), 2)

    def test_round_trip_through_json(self):
        data = data_with(ev("s", "2026-11-16T09:00:00", "2026-11-16T10:00:00", rrule="FREQ=DAILY;COUNT=5",
                            exdates=["2026-11-17T09:00:00"], reminders=[15, 0], tz="Europe/Berlin",
                            location="here", description="line\nline", extra=["STATUS:TENTATIVE"]),
                         ev("s", "2026-11-19T18:00:00", "2026-11-19T20:00:00", recurrence_id="2026-11-18T09:00:00"),
                         ev("d", "2026-11-17", "2026-11-19", all_day=True))
        again = model.CalendarData.from_json(json.loads(json.dumps(data.to_json())))
        self.assertEqual(again.to_json(), data.to_json())
        self.assertEqual(list(again.events()), list(data.events()))

    def test_calendars(self):
        data = model.CalendarData()
        self.assertTrue(data.ensure_default())
        self.assertFalse(data.ensure_default())
        work = data.add_calendar("  Work ")
        self.assertEqual((work.name, work.color), ("Work", "azure"))               # the first colour not in use
        self.assertEqual(data.update_calendar(work.id, name="Job", color="coral", visible=False),
                         model.Calendar(work.id, "Job", "coral", False))
        self.assertEqual(data.update_calendar(work.id, color="blood").color, "coral")
        self.assertEqual(data.visible_ids(), {"personal"})
        data.add_event(model.event_from_times(work.id, D(2026, 11, 17, 9), D(2026, 11, 17, 10)))
        calendar, events = data.remove_calendar(work.id)
        self.assertEqual((len(data), len(events), data.count_events(work.id)), (0, 1, 0))
        data.restore_calendar(calendar, events)
        self.assertEqual((len(data), len(data.calendars)), (1, 2))
        data.remove_calendar(work.id)
        with self.assertRaises(model.ModelError):
            data.remove_calendar("personal")                                       # the last one stays
        with self.assertRaises(model.ModelError):
            data.add_event(model.event_from_times("gone", D(2026, 11, 17, 9), D(2026, 11, 17, 10)))


class EditTest(unittest.TestCase):
    def series(self, **fields) -> model.CalendarData:
        return data_with(ev("s", "2026-11-16T09:00:00", "2026-11-16T10:00:00", rrule="FREQ=DAILY;COUNT=7",
                            tz="Europe/Berlin", **fields))

    def occ(self, data, day: int):
        return data.occurrence("s", f"2026-11-{day:02d}T09:00:00", BERLIN)

    def week(self, data, uid=None):
        found = data.occurrences(at(2026, 11, 14), at(2026, 11, 30), BERLIN)
        return [(occ.start.day, occ.start.hour, occ.event.summary) for occ in found
                if uid is None or occ.master.uid == uid]

    def test_single_event_edit_and_undo(self):
        data = data_with(ev("a", "2026-11-17T09:00:00", "2026-11-17T10:00:00"))
        occurrence = data.occurrences(*WEEK, BERLIN)[0]
        change = data.edit_occurrence(occurrence, replace(occurrence.event, summary="New", start="2026-11-18T12:00:00",
                                                          end="2026-11-18T13:00:00"))
        stored = data.get("a")
        self.assertEqual((stored.summary, stored.start, stored.sequence), ("New", "2026-11-18T12:00:00", 1))
        self.assertGreater(stored.modified, 0)
        data.apply(change.reversed())
        self.assertEqual((data.get("a").summary, data.get("a").start), ("a", "2026-11-17T09:00:00"))
        data.apply(change)
        self.assertEqual(data.get("a").summary, "New")

    def test_edit_this_makes_an_exception(self):
        data = self.series()
        occurrence = self.occ(data, 18)
        data.edit_occurrence(occurrence, replace(occurrence.event, summary="special", start="2026-11-18T14:00:00",
                                                 end="2026-11-18T15:30:00", rrule="", calendar="c2"), "this")
        self.assertEqual(self.week(data), [(16, 9, "s"), (17, 9, "s"), (18, 14, "special"), (19, 9, "s"),
                                           (20, 9, "s"), (21, 9, "s"), (22, 9, "s")])
        override = data.get("s", "2026-11-18T09:00:00")
        self.assertEqual((override.rrule, override.calendar, data.get("s").rrule), ("", "c1", "FREQ=DAILY;COUNT=7"))
        # Editing the exception again replaces it.
        occurrence = self.occ(data, 18)
        data.edit_occurrence(occurrence, replace(model.editable(occurrence), summary="again"), "this")
        self.assertEqual(len(data), 2)
        self.assertEqual(self.occ(data, 18).event.summary, "again")

    def test_edit_all_moves_every_occurrence_and_keeps_exceptions(self):
        data = self.series(exdates=["2026-11-17T09:00:00"])
        third = self.occ(data, 18)
        data.edit_occurrence(third, replace(model.editable(third), summary="odd", rrule=""), "this")
        fifth = self.occ(data, 20)
        data.edit_occurrence(fifth, replace(model.editable(fifth), summary="later", start="2026-11-20T11:00:00",
                                            end="2026-11-20T12:30:00"), "all")
        self.assertEqual(self.week(data), [(16, 11, "later"), (18, 9, "odd"), (19, 11, "later"), (20, 11, "later"),
                                           (21, 11, "later"), (22, 11, "later")])
        master = data.get("s")
        self.assertEqual((master.start, master.end, master.exdates), ("2026-11-16T11:00:00", "2026-11-16T12:30:00",
                                                                     ("2026-11-17T11:00:00",)))
        self.assertIsNotNone(data.get("s", "2026-11-18T11:00:00"))                 # the exception moved along

    def test_edit_all_with_a_new_rule_drops_the_exceptions(self):
        data = self.series(exdates=["2026-11-17T09:00:00"])
        occurrence = self.occ(data, 18)
        data.edit_occurrence(occurrence, replace(model.editable(occurrence), rrule="FREQ=WEEKLY;COUNT=2"), "all")
        self.assertEqual(self.week(data), [(16, 9, "s"), (23, 9, "s")])
        self.assertEqual(data.get("s").exdates, ())

    def test_edit_following_splits_the_series(self):
        data = self.series()
        late = self.occ(data, 21)
        data.edit_occurrence(late, replace(model.editable(late), summary="odd", rrule=""), "this")
        fourth = self.occ(data, 19)
        change = data.edit_occurrence(fourth, replace(model.editable(fourth), summary="new", start="2026-11-19T10:00:00",
                                                      end="2026-11-19T11:00:00"), "following")
        self.assertEqual(self.week(data), [(16, 9, "s"), (17, 9, "s"), (18, 9, "s"), (19, 10, "new"),
                                           (20, 10, "new"), (21, 9, "odd"), (22, 10, "new")])
        old = data.get("s")
        self.assertEqual(old.rrule, "FREQ=DAILY;UNTIL=20261119T075959Z")           # one second before, in UTC
        new_uid = next(event.uid for event in data.events() if event.uid != "s" and not event.recurrence_id)
        self.assertEqual(data.get(new_uid).rrule, "FREQ=DAILY;COUNT=4")            # the four that were left
        self.assertIsNotNone(data.get(new_uid, "2026-11-21T10:00:00"))
        self.assertIsNone(data.get("s", "2026-11-21T09:00:00"))
        data.apply(change.reversed())
        self.assertEqual(self.week(data), [(16, 9, "s"), (17, 9, "s"), (18, 9, "s"), (19, 9, "s"), (20, 9, "s"),
                                           (21, 9, "odd"), (22, 9, "s")])
        self.assertEqual(len(data), 2)

    def test_edit_following_from_the_first_occurrence_is_edit_all(self):
        data = self.series()
        first = self.occ(data, 16)
        data.edit_occurrence(first, replace(model.editable(first), summary="renamed"), "following")
        self.assertEqual(len(data), 1)
        self.assertEqual(data.get("s").summary, "renamed")

    def test_delete_this_following_all(self):
        data = self.series()
        data.delete_occurrence(self.occ(data, 17), "this")
        self.assertEqual([d for d, _h, _s in self.week(data)], [16, 18, 19, 20, 21, 22])
        occurrence = self.occ(data, 18)
        data.edit_occurrence(occurrence, replace(model.editable(occurrence), summary="odd", rrule=""), "this")
        data.delete_occurrence(self.occ(data, 18), "this")                         # deleting an exception
        self.assertEqual([d for d, _h, _s in self.week(data)], [16, 19, 20, 21, 22])
        self.assertEqual(len(data), 1)
        change = data.delete_occurrence(self.occ(data, 20), "following")
        self.assertEqual([d for d, _h, _s in self.week(data)], [16, 19])
        data.apply(change.reversed())
        self.assertEqual([d for d, _h, _s in self.week(data)], [16, 19, 20, 21, 22])
        data.delete_occurrence(self.occ(data, 16), "following")                    # from the first: everything
        self.assertEqual(len(data), 0)
        data = self.series()
        change = data.delete_occurrence(self.occ(data, 19), "all")
        self.assertEqual(len(data), 0)
        data.apply(change.reversed())
        self.assertEqual(len(self.week(data)), 7)

    def test_all_day_and_floating_series_are_cut_in_their_own_terms(self):
        data = data_with(ev("s", "2026-11-16", "2026-11-17", all_day=True, rrule="FREQ=DAILY"))
        data.delete_occurrence(data.occurrence("s", "2026-11-19", BERLIN), "following")
        self.assertEqual(data.get("s").rrule, "FREQ=DAILY;UNTIL=20261118")
        data = data_with(ev("s", "2026-11-16T09:00:00", "2026-11-16T10:00:00", rrule="FREQ=DAILY"))
        data.delete_occurrence(data.occurrence("s", "2026-11-19T09:00:00", BERLIN), "following")
        self.assertEqual(data.get("s").rrule, "FREQ=DAILY;UNTIL=20261119T085959")

    def test_move_occurrence(self):
        data = self.series()
        data.move_occurrence(self.occ(data, 18), D(2026, 11, 18, 13), D(2026, 11, 18, 13, 30))
        self.assertEqual(self.week(data)[2], (18, 13, "s"))
        self.assertEqual(data.get("s", "2026-11-18T09:00:00").rrule, "")
        single = data_with(ev("a", "2026-11-17", "2026-11-18", all_day=True))
        occurrence = single.occurrences(*WEEK, BERLIN)[0]
        single.move_occurrence(occurrence, D(2026, 11, 20), D(2026, 11, 22))
        self.assertEqual((single.get("a").start, single.get("a").end), ("2026-11-20", "2026-11-22"))

    def test_events_that_are_gone(self):
        data = self.series()
        occurrence = self.occ(data, 18)
        data.delete_occurrence(occurrence, "all")
        with self.assertRaises(model.ModelError):
            data.edit_occurrence(occurrence, occurrence.event, "all")
        with self.assertRaises(model.ModelError):
            data.replace_event(occurrence.event)
        with self.assertRaises(model.ModelError):
            data.add_event(replace(occurrence.event, calendar="nowhere"))

    def test_import_replaces_a_series_whole(self):
        data = self.series()
        occurrence = self.occ(data, 18)
        data.edit_occurrence(occurrence, replace(model.editable(occurrence), summary="odd", rrule=""), "this")
        fresh = model.make_event(ev("s", "2026-12-01T09:00:00", "2026-12-01T10:00:00", calendar="c2",
                                    rrule="FREQ=DAILY;COUNT=2"))
        change = data.add_events([fresh])
        self.assertEqual([event.key for event in data.events()], [("s", "")])
        self.assertEqual(data.get("s").calendar, "c2")
        data.apply(change.reversed())
        self.assertEqual(len(data), 2)

    def test_rules_from_recurrence(self):
        self.assertEqual(self.series().get("s").rule(), recurrence.parse_rrule("FREQ=DAILY;COUNT=7"))


if __name__ == "__main__":
    unittest.main()
