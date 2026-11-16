"""iCalendar: what is read from files written by other programs, what is written, and files meant to harm."""

import datetime as dt
import time
import unittest
from dataclasses import replace

from boswas_apps.calendar import ical, model
from boswas_apps.reminders import timeutil

D = dt.datetime
BERLIN = timeutil.zone("Europe/Berlin")


def calendar(*body: str, head: str = "VERSION:2.0\r\nPRODID:-//test//EN\r\n") -> bytes:
    return ("BEGIN:VCALENDAR\r\n" + head + "".join(line + "\r\n" for line in body) + "END:VCALENDAR\r\n").encode()


def vevent(*lines: str) -> list[str]:
    return ["BEGIN:VEVENT", *lines, "END:VEVENT"]


def one(*lines: str) -> model.Event:
    parsed = ical.parse(calendar(*vevent(*lines)))
    assert len(parsed.events) == 1, parsed
    return parsed.events[0]


VTIMEZONE_BERLIN = [
    "BEGIN:VTIMEZONE", "TZID:Customized Time Zone", "BEGIN:STANDARD", "DTSTART:16011028T030000",
    "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "END:STANDARD",
    "BEGIN:DAYLIGHT", "DTSTART:16010325T020000", "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3", "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0200", "END:DAYLIGHT", "END:VTIMEZONE"]


class LineTest(unittest.TestCase):
    def test_unfolding(self):
        text = "SUMMARY:one\r\n two\r\n\tthree\r\nDESCRIPTION:x\nLOCATION:y\r"
        self.assertEqual(list(ical.unfold(text)), ["SUMMARY:onetwothree", "DESCRIPTION:x", "LOCATION:y", ""])

    def test_parameters_and_quotes(self):
        line = ical.parse_line('DTSTART;TZID="Europe/Berlin";X-A=1,"two;:,",3:20261117T090000')
        self.assertEqual((line.name, line.value), ("DTSTART", "20261117T090000"))
        self.assertEqual(line.params, {"TZID": ["Europe/Berlin"], "X-A": ["1", "two;:,", "3"]})
        self.assertEqual(ical.parse_line("item1.summary;language=de:Hallo: Welt").value, "Hallo: Welt")
        self.assertEqual(ical.parse_line("ATTENDEE;CN=\"A ^'B^' ^^ ^n\":mailto:a@example.org").param("CN"),
                         'A "B" ^ \n')
        for junk in ("", "no colon here", ":value", "BAD NAME:1", ";x=1:2", "NAME;P=\"unterminated"):
            self.assertIsNone(ical.parse_line(junk), junk)

    def test_text_escaping(self):
        text = "a, b; c\\d\nnew line"
        self.assertEqual(ical.escape(text), "a\\, b\\; c\\\\d\\nnew line")
        self.assertEqual(ical.unescape(ical.escape(text)), text)
        self.assertEqual(ical.unescape("x\\Ny\\,z\\\\n"), "x\ny,z\\n")

    def test_folding_never_exceeds_75_octets_or_splits_a_character(self):
        line = "DESCRIPTION:" + "ä€𝄞x" * 60
        parts = ical.fold(line)
        self.assertTrue(all(len(part.encode("utf-8")) <= 75 for part in parts))
        self.assertTrue(all(part.startswith(" ") for part in parts[1:]))
        self.assertEqual(list(ical.unfold("\r\n".join(parts)))[0], line)
        self.assertEqual(ical.fold("SHORT:x"), ["SHORT:x"])

    def test_durations_and_offsets(self):
        for text, seconds in (("PT15M", 900), ("-PT15M", -900), ("P1D", 86400), ("P1W", 604800), ("PT0S", 0),
                              ("P1DT2H3M4S", 93784), ("+PT1H", 3600)):
            self.assertEqual(ical.parse_duration(text).total_seconds(), seconds, text)
        for junk in ("", "P", "PT", "15M", "P1H", "PT1D", "P-1D", "P" + "9" * 400 + "D"):
            self.assertIsNone(ical.parse_duration(junk), junk)
        for seconds in (0, 900, -900, 86400, 604800, 93784, 14 * 86400, -36 * 3600):
            self.assertEqual(ical.parse_duration(ical.format_duration(dt.timedelta(seconds=seconds))).total_seconds(),
                             seconds)
        self.assertEqual((ical.parse_offset("+0100"), ical.parse_offset("-0530"), ical.parse_offset("+013045")),
                         (3600, -19800, 5445))
        self.assertIsNone(ical.parse_offset("+2500"))
        self.assertEqual((ical.format_offset(3600), ical.format_offset(-19800), ical.format_offset(5445)),
                         ("+0100", "-0530", "+013045"))


class ReadTest(unittest.TestCase):
    def test_the_forms_of_a_start(self):
        event = one("UID:a", "DTSTART;VALUE=DATE:20261117", "DTEND;VALUE=DATE:20261119", "SUMMARY:Trip")
        self.assertEqual((event.all_day, event.start, event.end, event.tz), (True, "2026-11-17", "2026-11-19", ""))
        event = one("UID:a", "DTSTART:20261117T090000", "DTEND:20261117T100000")
        self.assertEqual((event.all_day, event.start, event.end, event.tz),
                         (False, "2026-11-17T09:00:00", "2026-11-17T10:00:00", ""))
        event = one("UID:a", "DTSTART:20261117T090000Z", "DURATION:PT1H30M")
        self.assertEqual((event.start, event.end, event.tz), ("2026-11-17T09:00:00", "2026-11-17T10:30:00", "UTC"))
        event = one("UID:a", "DTSTART;TZID=America/New_York:20261117T090000", "DTEND;TZID=Europe/Berlin:20261117T170000")
        self.assertEqual((event.start, event.end, event.tz),
                         ("2026-11-17T09:00:00", "2026-11-17T11:00:00", "America/New_York"))
        self.assertEqual(one("UID:a", "DTSTART;VALUE=DATE:20261117").end, "2026-11-18")
        self.assertEqual(one("UID:a", "DTSTART:20261117T090000").end, "2026-11-17T09:00:00")
        self.assertEqual(one("UID:a", "DTSTART;VALUE=DATE:20261117", "DURATION:P3D").end, "2026-11-20")
        self.assertEqual(one("UID:a", "DTSTART;VALUE=DATE:20261117", "DTEND;VALUE=DATE:20261117").end, "2026-11-18")

    def test_texts(self):
        event = one("UID:a\\,b", "DTSTART:20261117T090000", "SUMMARY:Lunch\\, with A\\; B", "LOCATION:Café",
                    "DESCRIPTION:Line one\\nLine two\\\\", "STATUS:TENTATIVE", "X-CUSTOM;A=b:kept",
                    "SEQUENCE:3", "CREATED:20260101T120000Z", "LAST-MODIFIED:20260102T120000Z")
        self.assertEqual((event.uid, event.summary, event.location, event.description),
                         ("a,b", "Lunch, with A; B", "Café", "Line one\nLine two\\"))
        self.assertEqual((event.extra, event.sequence), (("STATUS:TENTATIVE", "X-CUSTOM;A=b:kept"), 3))
        self.assertEqual(event.modified - event.created, 86400)

    def test_time_zone_names_of_other_programs(self):
        cases = {"W. Europe Standard Time": "Europe/Berlin", "Eastern Standard Time": "America/New_York",
                 "/mozilla.org/20070129_1/Europe/Berlin": "Europe/Berlin", "India Standard Time": "Asia/Kolkata",
                 "/freeassociation.sourceforge.net/Tzfile/America/Argentina/Buenos_Aires":
                     "America/Argentina/Buenos_Aires", "UTC": "UTC", "GMT+0200": "Etc/GMT-2", "utc-5": "Etc/GMT+5"}
        for tzid, expected in cases.items():
            event = one("UID:a", f'DTSTART;TZID="{tzid}":20261117T090000')
            self.assertEqual((event.tz, event.start), (expected, "2026-11-17T09:00:00"), tzid)

    def test_every_windows_zone_name_leads_to_a_zone(self):
        for name, candidates in ical.WINDOWS_ZONES.items():
            self.assertTrue(any(timeutil.zone(candidate) is not None for candidate in candidates), name)

    def test_a_vtimezone_is_matched_by_its_offsets(self):
        parsed = ical.parse(calendar(*VTIMEZONE_BERLIN, *vevent(
            "UID:a", 'DTSTART;TZID="Customized Time Zone":20260715T090000', "RRULE:FREQ=WEEKLY")))
        zone = timeutil.zone(parsed.events[0].tz)
        self.assertIsNotNone(zone)
        for month in range(1, 13):
            self.assertEqual(D(2026, month, 15, 12, tzinfo=zone).utcoffset(), D(2026, month, 15, 12,
                                                                                  tzinfo=BERLIN).utcoffset())

    def test_a_zone_like_no_other_is_converted_to_utc(self):
        odd = ["BEGIN:VTIMEZONE", "TZID:Odd", "BEGIN:STANDARD", "DTSTART:19700101T000000", "TZOFFSETFROM:+0317",
               "TZOFFSETTO:+0317", "END:STANDARD", "END:VTIMEZONE"]
        parsed = ical.parse(calendar(*odd, *vevent("UID:a", "DTSTART;TZID=Odd:20261117T090000",
                                                   "DTEND;TZID=Odd:20261117T100000")))
        event = parsed.events[0]
        self.assertEqual((event.tz, event.start, event.end), ("UTC", "2026-11-17T05:43:00", "2026-11-17T06:43:00"))
        parsed = ical.parse(calendar(*vevent("UID:a", "DTSTART;TZID=Nowhere/Special:20261117T090000")))
        self.assertEqual((parsed.events[0].tz, parsed.events[0].start), ("", "2026-11-17T09:00:00"))
        self.assertIn("times in an unknown time zone (kept as local times)", parsed.skipped)

    def test_vtimezone_offsets(self):
        parsed = ical.parse(calendar(*VTIMEZONE_BERLIN, *vevent("UID:a", "DTSTART:20261117T090000")))
        self.assertEqual(len(parsed.events), 1)
        lines = [ical.parse_line(raw) for raw in VTIMEZONE_BERLIN]
        children, current = [], None
        for line in lines:
            if line.name == "BEGIN" and line.value in ("STANDARD", "DAYLIGHT"):
                current = (line.value, [])
            elif line.name == "END" and current and line.value == current[0]:
                children.append(current)
                current = None
            elif current is not None:
                current[1].append(line)
        zone = ical._read_vtimezone([line for line in lines if line.name == "TZID"], children)
        self.assertEqual(zone.offset_at(D(2026, 1, 15, 12)), 3600)
        self.assertEqual(zone.offset_at(D(2026, 3, 29, 1, 59)), 3600)
        self.assertEqual(zone.offset_at(D(2026, 3, 29, 3, 0)), 7200)
        self.assertEqual(zone.offset_at(D(2026, 10, 25, 3, 30)), 3600)
        self.assertEqual(zone.offset_at(D(1500, 1, 1)), 3600)           # before every onset: the first FROM

    def test_recurrence_properties(self):
        event = one("UID:a", "DTSTART;TZID=Europe/Berlin:20261116T090000", "DTEND;TZID=Europe/Berlin:20261116T100000",
                    "RRULE:FREQ=DAILY;UNTIL=20261130T080000Z", "EXDATE;TZID=Europe/Berlin:20261117T090000,20261118T090000",
                    "EXDATE:20261119T080000Z", "RDATE;TZID=America/New_York:20261205T030000",
                    "RDATE;VALUE=PERIOD:20261206T080000Z/PT1H")
        self.assertEqual(event.rrule, "FREQ=DAILY;UNTIL=20261130T080000Z")
        self.assertEqual(event.exdates, ("2026-11-17T09:00:00", "2026-11-18T09:00:00", "2026-11-19T09:00:00"))
        self.assertEqual(event.rdates, ("2026-12-05T09:00:00", "2026-12-06T09:00:00"))
        all_day = one("UID:a", "DTSTART;VALUE=DATE:20261116", "RRULE:FREQ=WEEKLY;UNTIL=20261231T235959Z",
                      "EXDATE;VALUE=DATE:20261123")
        self.assertEqual((all_day.rrule, all_day.exdates), ("FREQ=WEEKLY;UNTIL=20261231", ("2026-11-23",)))
        floating = one("UID:a", "DTSTART:20261116T090000", "RRULE:FREQ=DAILY;UNTIL=20261130T080000Z")
        self.assertEqual(floating.rrule, "FREQ=DAILY;UNTIL=20261130T080000")
        zoned = one("UID:a", "DTSTART;TZID=Europe/Berlin:20261116T090000", "RRULE:FREQ=DAILY;UNTIL=20261130T090000")
        self.assertEqual(zoned.rrule, "FREQ=DAILY;UNTIL=20261130T080000Z")
        broken = ical.parse(calendar(*vevent("UID:a", "DTSTART:20261116T090000", "RRULE:FREQ=SOMETIMES")))
        self.assertEqual(broken.events[0].rrule, "")
        self.assertEqual(sum(broken.skipped.values()), 1)

    def test_changed_occurrences_are_put_in_the_terms_of_their_series(self):
        parsed = ical.parse(calendar(
            *vevent("UID:s", "RECURRENCE-ID:20261118T080000Z", "DTSTART:20261118T140000Z", "DTEND:20261118T150000Z",
                    "SUMMARY:moved"),
            *vevent("UID:s", "DTSTART;TZID=Europe/Berlin:20261116T090000", "DTEND;TZID=Europe/Berlin:20261116T100000",
                    "RRULE:FREQ=DAILY;COUNT=5", "SUMMARY:daily"),
            *vevent("UID:d", "DTSTART;VALUE=DATE:20261116", "RRULE:FREQ=DAILY;COUNT=5"),
            *vevent("UID:d", "RECURRENCE-ID;VALUE=DATE:20261117", "DTSTART;VALUE=DATE:20261120"),
            *vevent("UID:lonely", "RECURRENCE-ID;RANGE=THISANDFUTURE:20261118T090000", "DTSTART:20261118T100000")))
        keys = sorted(event.key for event in parsed.events)
        self.assertEqual(keys, [("d", ""), ("d", "2026-11-17"), ("lonely", "2026-11-18T09:00:00"), ("s", ""),
                                ("s", "2026-11-18T09:00:00")])
        data = model.CalendarData()
        data.ensure_default()
        data.add_events([replace(event, calendar="personal") for event in parsed.events])
        found = data.occurrences(timeutil.to_epoch(D(2026, 11, 18), BERLIN), timeutil.to_epoch(D(2026, 11, 19), BERLIN),
                                 BERLIN, calendars={"personal"})
        self.assertEqual(sorted((occ.event.summary, occ.start.hour) for occ in found if occ.master.uid == "s"),
                         [("moved", 15)])

    def test_alarms(self):
        def alarm(*lines):
            return ["BEGIN:VALARM", *lines, "END:VALARM"]
        event = one("UID:a", "DTSTART:20261117T090000Z", "DTEND:20261117T100000Z",
                    *alarm("ACTION:DISPLAY", "TRIGGER:-PT15M"), *alarm("ACTION:AUDIO", "TRIGGER;VALUE=DURATION:-P1D"),
                    *alarm("ACTION:DISPLAY", "TRIGGER;RELATED=END:-PT30M"), *alarm("ACTION:DISPLAY", "TRIGGER:PT0S"),
                    *alarm("ACTION:EMAIL", "TRIGGER:-PT5M"), *alarm("ACTION:DISPLAY", "TRIGGER:-PT15M"),
                    *alarm("ACTION:DISPLAY", "TRIGGER;VALUE=DATE-TIME:20261117T083000Z"),
                    *alarm("ACTION:DISPLAY", "TRIGGER:PT10M"), *alarm("ACTION:DISPLAY"),
                    *alarm("ACTION:DISPLAY", "TRIGGER:-P9999W"))
        self.assertEqual(event.reminders, (1440, 30, 15, 0, -10, -30))
        repeating = one("UID:a", "DTSTART:20261117T090000Z", "RRULE:FREQ=DAILY",
                        *alarm("ACTION:DISPLAY", "TRIGGER;VALUE=DATE-TIME:20261117T083000Z"))
        self.assertEqual(repeating.reminders, ())

    def test_other_writers(self):
        parsed = ical.parse(b"\xef\xbb\xbfBEGIN:VCALENDAR\nX-WR-CALNAME:Feiertage\\, Bayern\nBEGIN:VEVENT\n"
                            b"DTSTART;VALUE=DATE:20261225\nSUMMARY:1. Weihnachtstag\nEND:VEVENT\n"
                            b"BEGIN:VTODO\nSUMMARY:x\nEND:VTODO\nBEGIN:VJOURNAL\nEND:VJOURNAL\nEND:VCALENDAR\n")
        self.assertEqual((parsed.name, len(parsed.events), parsed.skipped), ("Feiertage, Bayern", 1,
                                                                             {"tasks": 1, "journal entries": 1}))
        self.assertTrue(parsed.events[0].uid.endswith("@imported"))
        again = ical.parse(b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:20261225\nSUMMARY:1. Weihnachtstag\n"
                           b"DTSTAMP:20200101T000000Z\nEND:VEVENT\nEND:VCALENDAR\n")
        self.assertNotEqual(again.events[0].uid, "")
        latin = ical.parse("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:l\nDTSTART:20261117T090000\nSUMMARY:Café\n"
                           "END:VEVENT\nEND:VCALENDAR".encode("latin-1"))
        self.assertEqual(latin.events[0].summary, "Café")
        bare = ical.parse(b"BEGIN:VEVENT\nUID:b\nDTSTART:20261117T090000\nEND:VEVENT\n")
        self.assertEqual(len(bare.events), 1)
        twice = ical.parse(calendar(*vevent("UID:x", "DTSTART:20261117T090000", "SUMMARY:first"),
                                    *vevent("UID:x", "DTSTART:20261117T090000", "SUMMARY:second")))
        self.assertEqual([event.summary for event in twice.events], ["second"])

    def test_the_same_file_twice_gives_the_same_uids(self):
        data = b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:20261225\nSUMMARY:No UID\nEND:VEVENT\nEND:VCALENDAR\n"
        self.assertEqual(ical.parse(data).events[0].uid, ical.parse(data).events[0].uid)


class HostileTest(unittest.TestCase):
    def test_not_a_calendar(self):
        for data in (b"", b"hello", b"\x00\x01\x02" * 100, b"<html><body>BEGIN:VCALENDAR</body></html>",
                     b"BEGIN:VCARD\nFN:x\nEND:VCARD"):
            with self.assertRaises(ical.ICalError, msg=data[:20]):
                ical.parse(data)

    def test_size_limits(self):
        with self.assertRaises(ical.ICalError):
            ical.parse(b"BEGIN:VCALENDAR\n" + b"X" * ical.MAX_FILE_BYTES)
        nested = b"BEGIN:VCALENDAR\n" + b"BEGIN:VEVENT\n" * 50
        with self.assertRaises(ical.ICalError):
            ical.parse(nested)
        long_line = one("UID:a", "DTSTART:20261117T090000", "DESCRIPTION:" + "x" * (ical.MAX_LINE * 3),
                        "SUMMARY:after the long line")
        self.assertLessEqual(len(long_line.description), model.MAX_DESCRIPTION)
        self.assertEqual(long_line.summary, "after the long line")
        folded = "DESCRIPTION:" + ("\r\n " + "y" * 70) * 3000
        self.assertLessEqual(len(one("UID:a", "DTSTART:20261117T090000", folded).description), model.MAX_DESCRIPTION)

    def test_many_events_are_read_quickly_and_capped(self):
        lines = []
        for index in range(3000):
            lines += vevent(f"UID:e{index}", f"DTSTART:2026{1 + index % 12:02d}{1 + index % 28:02d}T090000",
                            "SUMMARY:Event", "RRULE:FREQ=WEEKLY;BYDAY=MO", "BEGIN:VALARM", "ACTION:DISPLAY",
                            "TRIGGER:-PT15M", "END:VALARM")
        began = time.monotonic()
        parsed = ical.parse(calendar(*lines))
        self.assertLess(time.monotonic() - began, 10.0)
        self.assertEqual(len(parsed.events), 3000)
        original = ical.MAX_EVENTS
        ical.MAX_EVENTS = 100
        try:
            capped = ical.parse(calendar(*lines))
        finally:
            ical.MAX_EVENTS = original
        self.assertEqual(len(capped.events), 100)
        self.assertEqual(len(capped.warnings), 1)

    def test_broken_events_are_skipped_not_fatal(self):
        parsed = ical.parse(calendar(
            *vevent("UID:nostart", "SUMMARY:x"), *vevent("UID:badstart", "DTSTART:tomorrow"),
            *vevent("UID:faraway", "DTSTART;VALUE=DATE:99991231", "DURATION:P5D"),
            *vevent("UID:feb30", "DTSTART;VALUE=DATE:20260230"),
            *vevent("UID:ok", "DTSTART:20261117T090000", "DTEND:20261117T080000", "RRULE:FREQ=SECONDLY;COUNT=999999999",
                    "EXDATE:garbage,20261117T090001", "RDATE:" + ",".join(["20261118T090000"] * 5000)),
            "END:VEVENT", "END:VEVENT", "GARBAGE", "BEGIN:VEVENT"))
        self.assertEqual([event.uid for event in parsed.events], ["ok"])
        ok = parsed.events[0]
        self.assertEqual((ok.end, ok.exdates, ok.rdates), ("2026-11-17T09:00:00", ("2026-11-17T09:00:01",),
                                                           ("2026-11-18T09:00:00",)))
        self.assertGreaterEqual(sum(parsed.skipped.values()), 4)

    def test_a_hostile_file_cannot_hang_the_calendar(self):
        lines = []
        for index in range(200):
            lines += vevent(f"UID:h{index}", "DTSTART:19700101T000000Z", "RRULE:FREQ=SECONDLY;COUNT=999999999")
            lines += vevent(f"UID:n{index}", "DTSTART:20200201T000000Z", "RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30")
            lines += vevent(f"UID:m{index}", "DTSTART:20200201T000000Z",
                            f"RRULE:FREQ=MINUTELY;BYSECOND={','.join(str(n) for n in range(60))};INTERVAL={index + 1}")
        parsed = ical.parse(calendar(*lines))
        data = model.CalendarData()
        data.ensure_default()
        data.add_events([replace(event, calendar="personal") for event in parsed.events])
        began = time.monotonic()
        data.occurrences(timeutil.to_epoch(D(2026, 11, 16), BERLIN), timeutil.to_epoch(D(2026, 11, 17), BERLIN),
                         BERLIN, limit=5000)
        self.assertLess(time.monotonic() - began, 20.0)


class WriteTest(unittest.TestCase):
    def events(self) -> list[model.Event]:
        raw = [
            {"uid": "single@x", "start": "2026-11-17T09:00:00", "end": "2026-11-17T10:00:00", "tz": "Europe/Berlin",
             "summary": "Lunch, with A; B", "location": "Café \U0001F600", "description": "one\ntwo\\three",
             "reminders": [15, 0, -540], "created": 1790000000, "modified": 1790000500, "sequence": 2,
             "extra": ["STATUS:CONFIRMED", "X-LONG:" + "v" * 200]},
            {"uid": "allday@x", "start": "2026-12-24", "end": "2026-12-27", "all_day": True, "summary": "Holidays",
             "rrule": "FREQ=YEARLY;UNTIL=20301224", "exdates": ["2027-12-24"]},
            {"uid": "series@x", "start": "2026-03-16T09:00:00", "end": "2026-03-16T09:30:00", "tz": "America/New_York",
             "rrule": "FREQ=WEEKLY;BYDAY=MO,WE", "exdates": ["2026-03-18T09:00:00"], "rdates": ["2026-03-20T12:00:00"]},
            {"uid": "series@x", "recurrence_id": "2026-03-23T09:00:00", "start": "2026-03-23T11:00:00",
             "end": "2026-03-23T12:00:00", "tz": "America/New_York", "summary": "moved"},
            {"uid": "utc@x", "start": "2026-11-17T09:00:00", "end": "2026-11-17T10:00:00", "tz": "UTC",
             "rrule": "FREQ=DAILY;COUNT=3"},
            {"uid": "floating@x", "start": "2026-11-17T09:00:00", "end": "2026-11-17T10:00:00",
             "rrule": "FREQ=DAILY;UNTIL=20261120T090000"},
        ]
        return [model.make_event({"calendar": "c", "modified": 1790000500, **entry}) for entry in raw]

    def test_format(self):
        data = ical.export(self.events(), name="My, calendar", now=1790001000)
        text = data.decode("utf-8")
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in text.split("\r\n")))
        for expected in ("X-WR-CALNAME:My\\, calendar", "UID:single@x", "DTSTART;TZID=Europe/Berlin:20261117T090000",
                         "SUMMARY:Lunch\\, with A\\; B", "DESCRIPTION:one\\ntwo\\\\three", "TRIGGER:-PT15M",
                         "TRIGGER:PT0S", "TRIGGER:PT9H", "DTSTART;VALUE=DATE:20261224", "DTEND;VALUE=DATE:20261227",
                         "RRULE:FREQ=YEARLY;UNTIL=20301224", "EXDATE;VALUE=DATE:20271224",
                         "RECURRENCE-ID;TZID=America/New_York:20260323T090000", "DTSTART:20261117T090000Z",
                         "EXDATE;TZID=America/New_York:20260318T090000", "RDATE;TZID=America/New_York:20260320T120000",
                         "DTSTAMP:20260921T", "SEQUENCE:2", "STATUS:CONFIRMED", "BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin",
                         "TZID:America/New_York", "RRULE:FREQ=DAILY;UNTIL=20261120T090000\r\n"):
            self.assertIn(expected, text.replace("\r\n ", ""), expected)

    def test_round_trip(self):
        events = self.events()
        parsed = ical.parse(ical.export(events, name="Round trip"))
        self.assertEqual(parsed.name, "Round trip")
        self.assertEqual(parsed.skipped, {})
        again = {event.key: replace(event, calendar="c") for event in parsed.events}
        self.assertEqual(set(again), {event.key for event in events})
        for event in events:
            self.assertEqual(again[event.key], event, event.uid)
        # And once more: what was read is written the same way.
        self.assertEqual(ical.export(again.values(), name="Round trip", now=5),
                         ical.export(events, name="Round trip", now=5))

    def test_written_time_zones_match_the_database(self):
        for name in ("Europe/Berlin", "America/New_York", "Australia/Sydney", "Asia/Kolkata", "America/Sao_Paulo",
                     "Asia/Tehran", "Europe/London", "Africa/Casablanca", "Australia/Lord_Howe", "Asia/Tokyo"):
            lines = ical.vtimezone(name, 2015, 2030)
            self.assertEqual((lines[0], lines[-1]), ("BEGIN:VTIMEZONE", "END:VTIMEZONE"), name)
            parsed = ical.parse(calendar(*lines, *vevent("UID:a", "DTSTART:20261117T090000")))
            self.assertEqual(len(parsed.events), 1)
            definition = self._definition(lines)
            zone = timeutil.zone(name)
            for year in range(2015, 2034):                      # three years past the range: the rule goes on
                for month in range(1, 13):
                    for day in (3, 17, 28):
                        local = D(year, month, day, 12)
                        expected = int(local.replace(tzinfo=zone).utcoffset().total_seconds())
                        self.assertEqual(definition.offset_at(local), expected, (name, local))
        self.assertEqual(ical.vtimezone("UTC", 2020, 2030), [])
        self.assertEqual(ical.vtimezone("No/Zone", 2020, 2030), [])
        berlin = "\n".join(ical.vtimezone("Europe/Berlin", 2020, 2030))
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", berlin)
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", berlin)
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU", "\n".join(ical.vtimezone("America/New_York", 2020, 2030)))
        self.assertNotIn("RRULE", "\n".join(ical.vtimezone("Asia/Tokyo", 2020, 2030)))

    @staticmethod
    def _definition(lines: list[str]) -> ical.VTimezone:
        children, current, top = [], None, []
        for line in (ical.parse_line(raw) for raw in lines):
            if line.name == "BEGIN" and line.value in ("STANDARD", "DAYLIGHT"):
                current = (line.value, [])
            elif line.name == "END" and current is not None and line.value == current[0]:
                children.append(current)
                current = None
            elif current is not None:
                current[1].append(line)
            else:
                top.append(line)
        return ical._read_vtimezone(top, children)

    def test_nothing_from_an_event_can_break_out_of_its_line(self):
        event = model.make_event({"uid": "x", "calendar": "c", "start": "2026-11-17T09:00:00",
                                  "end": "2026-11-17T10:00:00", "summary": "a\r\nEND:VEVENT\r\nBEGIN:VEVENT",
                                  "description": "l1\r\nBEGIN:VALARM", "location": "x;y,z",
                                  "extra": ["END:VCALENDAR", "BEGIN:VEVENT", "X-OK:1", "DTSTART:19000101T000000"]})
        text = ical.export([event]).decode()
        lines = text.split("\r\n")
        self.assertEqual(lines.count("BEGIN:VEVENT"), 1)
        self.assertEqual(lines.count("END:VEVENT"), 1)
        self.assertEqual(lines.count("END:VCALENDAR"), 1)
        self.assertEqual(sum(1 for line in lines if line.startswith("DTSTART")), 1)
        self.assertEqual(sum(1 for line in lines if line.startswith("BEGIN:VALARM")), 0)
        parsed = ical.parse(text.encode())
        self.assertEqual(len(parsed.events), 1)
        self.assertEqual(parsed.events[0].description, "l1\nBEGIN:VALARM")


if __name__ == "__main__":
    unittest.main()
