//! The Calendar application's events, for the widgets board.
//!
//! Calendar keeps everything in one JSON document,
//! `$XDG_DATA_HOME/com.boswas.Calendar/calendar.json` (format in
//! `boswas_apps/calendar/model.py`): calendars, and events with local wall
//! times in a zone of the time-zone database (or floating), recurrence
//! rules of RFC 5545 with RDATE and EXDATE, and changed occurrences as
//! overrides. This module reads that document the way Calendar's
//! `CalendarData.from_json` does (anything unusable is left out, never an
//! error) and expands the events of the next days by the same rules as
//! Calendar's `recurrence.py`, which it follows line by line: DTSTART is
//! always the first occurrence and counts towards COUNT, the BYxxx parts
//! expand or limit each period, BYSETPOS picks from it, invalid dates are
//! skipped, and every walk is bounded. Nothing here writes to Calendar's
//! files.

use std::{
    collections::{HashMap, HashSet},
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
};

use chrono::{Datelike, Duration, NaiveDate, NaiveDateTime, NaiveTime, Timelike};
use serde_json::Value;

use crate::tzif::{self, Zone};

/// Largest calendar document the shell reads.
pub const MAX_BYTES: u64 = 8 * 1024 * 1024;
const MAX_CALENDARS: usize = 64;
const MAX_EVENTS: usize = 200_000;
const MAX_SUMMARY: usize = 500;
const MAX_DATES: usize = 2000;
const MAX_RULE_TEXT: usize = 4096;
const MAX_LIST: usize = 800;
const MAX_INTERVAL: u32 = 1_000_000;
const MAX_COUNT: u64 = 1_000_000_000;
const DEFAULT_BUDGET: usize = 200_000;
const COUNT_WALK: usize = 50_000;
const MAX_YEAR: i32 = 9999;
/// The palette families a calendar can have.
pub const COLORS: [&str; 8] = ["iris", "azure", "mint", "sun", "coral", "sky", "orchid", "slate"];

/// Where Calendar keeps its document.
pub fn calendar_path() -> Option<PathBuf> {
    data_home().map(|dir| dir.join("com.boswas.Calendar").join("calendar.json"))
}

/// `$XDG_DATA_HOME`, or `~/.local/share`.
pub fn data_home() -> Option<PathBuf> {
    std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/share")))
}

/// Reads one of the user's own files of the applications: a regular file of
/// this user, not through a symbolic link, without waiting on a FIFO, at
/// most `limit` bytes. `None` when it does not exist or is not acceptable.
pub fn read_own_file(path: &Path, limit: u64) -> Result<Option<String>, &'static str> {
    let file = match std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_NOCTTY).open(path) {
        Ok(file) => file,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("cannot be opened (or is a symbolic link)"),
    };
    let meta = file.metadata().map_err(|_| "cannot be examined")?;
    if !meta.file_type().is_file() {
        return Err("not a regular file");
    }
    if meta.uid() != rustix::process::geteuid().as_raw() {
        return Err("not the user's own file");
    }
    if meta.len() > limit {
        return Err("too large");
    }
    let mut bytes = Vec::new();
    file.take(limit + 1).read_to_end(&mut bytes).map_err(|_| "cannot be read")?;
    if bytes.len() as u64 > limit {
        return Err("too large");
    }
    String::from_utf8(bytes).map(Some).map_err(|_| "not UTF-8 text")
}

// ---------------------------------------------------------------------------
// Local times
// ---------------------------------------------------------------------------

/// A local time of the document: a date (all-day events) or a wall time.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Local {
    Date(NaiveDate),
    Time(NaiveDateTime),
}

impl Local {
    /// `2026-11-17` or `2026-11-17T09:00:00` (also without seconds, or with
    /// a space). Fractions of a second are dropped; a zone is refused.
    pub fn parse(text: &str) -> Option<Self> {
        if text.len() > 19 {
            return None;
        }
        if text.len() == 10 {
            return NaiveDate::parse_from_str(text, "%Y-%m-%d").ok().map(Local::Date);
        }
        for format in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"] {
            if let Ok(time) = NaiveDateTime::parse_from_str(text, format) {
                return Some(Local::Time(time));
            }
        }
        None
    }

    /// Midnight for a date.
    pub fn as_time(self) -> NaiveDateTime {
        match self {
            Local::Date(date) => date.and_time(NaiveTime::MIN),
            Local::Time(time) => time,
        }
    }

    pub fn date(self) -> NaiveDate {
        match self {
            Local::Date(date) => date,
            Local::Time(time) => time.date(),
        }
    }

    /// As Calendar writes it.
    pub fn format(self) -> String {
        match self {
            Local::Date(date) => date.format("%Y-%m-%d").to_string(),
            Local::Time(time) => time.format("%Y-%m-%dT%H:%M:%S").to_string(),
        }
    }
}

/// Seconds since the epoch of a wall time read as UTC.
fn wall_seconds(time: NaiveDateTime) -> i64 {
    time.and_utc().timestamp()
}

/// The wall time of `seconds` read as UTC.
fn from_wall_seconds(seconds: i64) -> Option<NaiveDateTime> {
    chrono::DateTime::from_timestamp(seconds, 0).map(|t| t.naive_utc())
}

/// The instant of a wall time in a zone.
pub fn to_epoch(time: NaiveDateTime, zone: &Zone) -> i64 {
    zone.instant_of(wall_seconds(time))
}

/// The wall time in a zone at an instant.
pub fn from_epoch(instant: i64, zone: &Zone) -> NaiveDateTime {
    from_wall_seconds(instant + i64::from(zone.offset_at(instant))).unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Recurrence rules (recurrence.py)
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Freq {
    Secondly,
    Minutely,
    Hourly,
    Daily,
    Weekly,
    Monthly,
    Yearly,
}

impl Freq {
    fn parse(text: &str) -> Option<Self> {
        Some(match text {
            "SECONDLY" => Freq::Secondly,
            "MINUTELY" => Freq::Minutely,
            "HOURLY" => Freq::Hourly,
            "DAILY" => Freq::Daily,
            "WEEKLY" => Freq::Weekly,
            "MONTHLY" => Freq::Monthly,
            "YEARLY" => Freq::Yearly,
            _ => return None,
        })
    }

    /// Periods in a row without an occurrence after which a rule is taken
    /// to have no more.
    fn most_empty(self) -> usize {
        match self {
            Freq::Yearly => 120,
            Freq::Monthly => 400,
            Freq::Weekly => 800,
            _ => 4000,
        }
    }
}

const WEEKDAYS: [&str; 7] = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];

/// When a rule ends by UNTIL: a wall time (or a date's last second), or an
/// instant written in UTC.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Until {
    Wall(NaiveDateTime),
    Utc(i64),
}

/// One RRULE.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rule {
    pub freq: Freq,
    pub interval: u32,
    pub count: Option<u64>,
    pub until: Option<Until>,
    pub bysecond: Vec<i32>,
    pub byminute: Vec<i32>,
    pub byhour: Vec<i32>,
    /// (ordinal or 0, weekday with Monday 0).
    pub byday: Vec<(i32, u32)>,
    pub bymonthday: Vec<i32>,
    pub byyearday: Vec<i32>,
    pub byweekno: Vec<i32>,
    pub bymonth: Vec<i32>,
    pub bysetpos: Vec<i32>,
    pub wkst: u32,
}

fn int_list(value: &str, low: i32, high: i32, zero: bool) -> Option<Vec<i32>> {
    let parts: Vec<&str> = value.split(',').collect();
    if parts.len() > MAX_LIST {
        return None;
    }
    let mut out: Vec<i32> = Vec::new();
    for part in parts {
        let part = part.trim();
        let digits = part.strip_prefix(['+', '-']).unwrap_or(part);
        if digits.is_empty() || digits.len() > 4 || !digits.chars().all(|c| c.is_ascii_digit()) {
            return None;
        }
        let number: i32 = part.parse().ok()?;
        if !(low..=high).contains(&number) || (number == 0 && !zero) {
            return None;
        }
        if !out.contains(&number) {
            out.push(number);
        }
    }
    Some(out)
}

fn parse_until(value: &str) -> Option<Until> {
    let value = value.trim();
    if value.len() == 8 && value.chars().all(|c| c.is_ascii_digit()) {
        let date = NaiveDate::parse_from_str(value, "%Y%m%d").ok()?;
        return Some(Until::Wall(date.and_hms_opt(23, 59, 59)?));
    }
    let (body, utc) = match value.strip_suffix('Z') {
        Some(body) => (body, true),
        None => (value, false),
    };
    if body.len() != 15 || body.as_bytes()[8] != b'T' {
        return None;
    }
    let date = NaiveDate::parse_from_str(&body[..8], "%Y%m%d").ok()?;
    let digits = &body[9..];
    if !digits.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let (h, m, s): (u32, u32, u32) = (digits[0..2].parse().ok()?, digits[2..4].parse().ok()?, digits[4..6].parse().ok()?);
    let time = date.and_hms_opt(h, m, s.min(59))?;
    Some(if utc { Until::Utc(time.and_utc().timestamp()) } else { Until::Wall(time) })
}

impl Rule {
    /// Parses the value of an RRULE property (`FREQ=WEEKLY;BYDAY=MO,WE`).
    pub fn parse(text: &str) -> Option<Self> {
        if text.len() > MAX_RULE_TEXT {
            return None;
        }
        let mut text = text.trim();
        if text.len() >= 6 && text[..6].eq_ignore_ascii_case("RRULE:") {
            text = &text[6..];
        }
        let mut fields: HashMap<String, String> = HashMap::new();
        for part in text.split(';') {
            if part.trim().is_empty() {
                continue;
            }
            let (name, value) = part.split_once('=')?;
            let name = name.trim().to_ascii_uppercase();
            if value.trim().is_empty() || fields.contains_key(&name) {
                return None;
            }
            fields.insert(name, value.trim().to_ascii_uppercase());
        }
        let freq = Freq::parse(fields.get("FREQ")?)?;
        let mut rule = Rule {
            freq,
            interval: 1,
            count: None,
            until: None,
            bysecond: Vec::new(),
            byminute: Vec::new(),
            byhour: Vec::new(),
            byday: Vec::new(),
            bymonthday: Vec::new(),
            byyearday: Vec::new(),
            byweekno: Vec::new(),
            bymonth: Vec::new(),
            bysetpos: Vec::new(),
            wkst: 0,
        };
        if let Some(value) = fields.get("INTERVAL") {
            let n: u32 = value.parse().ok().filter(|_| value.chars().all(|c| c.is_ascii_digit()))?;
            if !(1..=MAX_INTERVAL).contains(&n) {
                return None;
            }
            rule.interval = n;
        }
        if let Some(value) = fields.get("COUNT") {
            let n: u64 = value.parse().ok().filter(|_| value.chars().all(|c| c.is_ascii_digit()))?;
            if !(1..=MAX_COUNT).contains(&n) {
                return None;
            }
            rule.count = Some(n);
        }
        if let Some(value) = fields.get("UNTIL") {
            rule.until = Some(parse_until(value)?);
        }
        if let Some(value) = fields.get("WKST") {
            rule.wkst = WEEKDAYS.iter().position(|d| d == value)? as u32;
        }
        if let Some(value) = fields.get("BYDAY") {
            let parts: Vec<&str> = value.split(',').collect();
            if parts.len() > MAX_LIST {
                return None;
            }
            for part in parts {
                let part = part.trim();
                if part.len() < 2 {
                    return None;
                }
                let (number, day) = part.split_at(part.len() - 2);
                let weekday = WEEKDAYS.iter().position(|d| *d == day)? as u32;
                let ordinal = if number.is_empty() {
                    0
                } else {
                    let digits = number.strip_prefix(['+', '-']).unwrap_or(number);
                    if digits.is_empty() || digits.len() > 2 || !digits.chars().all(|c| c.is_ascii_digit()) {
                        return None;
                    }
                    let n: i32 = number.parse().ok()?;
                    if !(1..=53).contains(&n.abs()) {
                        return None;
                    }
                    n
                };
                if !rule.byday.contains(&(ordinal, weekday)) {
                    rule.byday.push((ordinal, weekday));
                }
            }
        }
        let lists: [(&str, i32, i32, bool); 8] = [
            ("BYSECOND", 0, 60, true),
            ("BYMINUTE", 0, 59, true),
            ("BYHOUR", 0, 23, true),
            ("BYMONTHDAY", -31, 31, false),
            ("BYYEARDAY", -366, 366, false),
            ("BYWEEKNO", -53, 53, false),
            ("BYMONTH", 1, 12, false),
            ("BYSETPOS", -366, 366, false),
        ];
        for (name, low, high, zero) in lists {
            let Some(value) = fields.get(name) else {
                continue;
            };
            let mut numbers = int_list(value, low, high, zero)?;
            if name == "BYSECOND" {
                let mut capped: Vec<i32> = Vec::new();
                for n in numbers {
                    let n = n.min(59);
                    if !capped.contains(&n) {
                        capped.push(n);
                    }
                }
                numbers = capped;
            }
            match name {
                "BYSECOND" => rule.bysecond = numbers,
                "BYMINUTE" => rule.byminute = numbers,
                "BYHOUR" => rule.byhour = numbers,
                "BYMONTHDAY" => rule.bymonthday = numbers,
                "BYYEARDAY" => rule.byyearday = numbers,
                "BYWEEKNO" => rule.byweekno = numbers,
                "BYMONTH" => rule.bymonth = numbers,
                _ => rule.bysetpos = numbers,
            }
        }
        Some(rule)
    }

    fn plain(&self) -> bool {
        self.bysecond.is_empty()
            && self.byminute.is_empty()
            && self.byhour.is_empty()
            && self.byday.is_empty()
            && self.bymonthday.is_empty()
            && self.byyearday.is_empty()
            && self.byweekno.is_empty()
            && self.bymonth.is_empty()
            && self.bysetpos.is_empty()
            && self.until.is_none()
    }
}

fn is_leap(year: i32) -> bool {
    NaiveDate::from_ymd_opt(year, 2, 29).is_some()
}

fn year_length(year: i32) -> i32 {
    if is_leap(year) {
        366
    } else {
        365
    }
}

fn month_length(year: i32, month: u32) -> u32 {
    match month {
        2 if is_leap(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// The first day of week 1 of `year`: the first week (starting on `wkst`)
/// with four days in the year.
fn week_one(year: i32, wkst: u32) -> Option<NaiveDate> {
    let first = NaiveDate::from_ymd_opt(year, 1, 1)?;
    let offset = (i64::from(first.weekday().num_days_from_monday()) - i64::from(wkst)).rem_euclid(7);
    Some(if offset <= 3 { first - Duration::days(offset) } else { first + Duration::days(7 - offset) })
}

/// (the week number of `day`, the number of weeks of its year).
fn week_number(day: NaiveDate, wkst: u32) -> Option<(i32, i32)> {
    let mut year = day.year();
    if year < MAX_YEAR && day >= week_one(year + 1, wkst)? {
        year += 1;
    } else if day < week_one(year, wkst)? {
        year -= 1;
    }
    let start = week_one(year, wkst)?;
    let next = week_one(year + 1, wkst).unwrap_or(start + Duration::days(364));
    let weeks = ((next - start).num_days() / 7) as i32;
    Some((((day - start).num_days() / 7) as i32 + 1, weeks))
}

/// The occurrences of one rule from one start (`_Expander`).
struct Expander<'a> {
    rule: &'a Rule,
    start: NaiveDateTime,
    bymonth: HashSet<i32>,
    bymonthday: HashSet<i32>,
    byyearday: HashSet<i32>,
    byweekno: HashSet<i32>,
    weekdays: HashSet<u32>,
    nth_month: HashSet<(i32, u32)>,
    nth_year: HashSet<(i32, u32)>,
    has_byday: bool,
    minutes: Vec<u32>,
    seconds: Vec<u32>,
    byhour: HashSet<i32>,
    byminute: HashSet<i32>,
    bysecond: HashSet<i32>,
    times: Vec<NaiveTime>,
    day_filtered: bool,
}

impl<'a> Expander<'a> {
    fn new(rule: &'a Rule, start: NaiveDateTime) -> Self {
        let mut bymonth = rule.bymonth.clone();
        let mut bymonthday = rule.bymonthday.clone();
        let mut byday = rule.byday.clone();
        if rule.byweekno.is_empty() && rule.byyearday.is_empty() && bymonthday.is_empty() && byday.is_empty() {
            match rule.freq {
                Freq::Yearly => {
                    if bymonth.is_empty() {
                        bymonth = vec![start.month() as i32];
                    }
                    bymonthday = vec![start.day() as i32];
                }
                Freq::Monthly => bymonthday = vec![start.day() as i32],
                Freq::Weekly => byday = vec![(0, start.weekday().num_days_from_monday())],
                _ => {}
            }
        }
        let monthly_nth = rule.freq == Freq::Monthly || (rule.freq == Freq::Yearly && !rule.bymonth.is_empty());
        let yearly_nth = rule.freq == Freq::Yearly && rule.bymonth.is_empty();
        let sorted = |list: &[i32], default: u32| -> Vec<u32> {
            if list.is_empty() {
                vec![default]
            } else {
                let mut out: Vec<u32> = list.iter().map(|n| *n as u32).collect();
                out.sort_unstable();
                out
            }
        };
        let hours = sorted(&rule.byhour, start.hour());
        let minutes = sorted(&rule.byminute, start.minute());
        let seconds = sorted(&rule.bysecond, start.second());
        let mut times = Vec::new();
        if rule.freq >= Freq::Daily {
            for h in &hours {
                for m in &minutes {
                    for s in &seconds {
                        if let Some(time) = NaiveTime::from_hms_opt(*h, *m, *s) {
                            times.push(time);
                        }
                    }
                }
            }
        }
        let day_filtered = !bymonth.is_empty() || !bymonthday.is_empty() || !rule.byyearday.is_empty() || (!rule.byweekno.is_empty() && rule.freq == Freq::Yearly) || !byday.is_empty();
        Self {
            rule,
            start,
            bymonth: bymonth.into_iter().collect(),
            bymonthday: bymonthday.into_iter().collect(),
            byyearday: rule.byyearday.iter().copied().collect(),
            byweekno: if rule.freq == Freq::Yearly { rule.byweekno.iter().copied().collect() } else { HashSet::new() },
            weekdays: byday.iter().filter(|(o, _)| *o == 0 || !(monthly_nth || yearly_nth)).map(|(_, d)| *d).collect(),
            nth_month: byday.iter().filter(|(o, _)| *o != 0 && monthly_nth).copied().collect(),
            nth_year: byday.iter().filter(|(o, _)| *o != 0 && yearly_nth).copied().collect(),
            has_byday: !byday.is_empty(),
            minutes,
            seconds,
            byhour: rule.byhour.iter().copied().collect(),
            byminute: rule.byminute.iter().copied().collect(),
            bysecond: rule.bysecond.iter().copied().collect(),
            times,
            day_filtered,
        }
    }

    fn day_ok(&self, day: NaiveDate) -> bool {
        if !self.bymonth.is_empty() && !self.bymonth.contains(&(day.month() as i32)) {
            return false;
        }
        if !self.byweekno.is_empty() {
            let Some((number, weeks)) = week_number(day, self.rule.wkst) else {
                return false;
            };
            if !self.byweekno.contains(&number) && !self.byweekno.contains(&(number - weeks - 1)) {
                return false;
            }
        }
        if !self.byyearday.is_empty() {
            let number = day.ordinal() as i32;
            if !self.byyearday.contains(&number) && !self.byyearday.contains(&(number - year_length(day.year()) - 1)) {
                return false;
            }
        }
        if !self.bymonthday.is_empty() {
            let length = month_length(day.year(), day.month()) as i32;
            let d = day.day() as i32;
            if !self.bymonthday.contains(&d) && !self.bymonthday.contains(&(d - length - 1)) {
                return false;
            }
        }
        if self.has_byday {
            let weekday = day.weekday().num_days_from_monday();
            if self.weekdays.contains(&weekday) {
                return true;
            }
            if !self.nth_month.is_empty() {
                let length = month_length(day.year(), day.month()) as i32;
                let d = day.day() as i32;
                if self.nth_month.contains(&((d - 1) / 7 + 1, weekday)) || self.nth_month.contains(&(-((length - d) / 7 + 1), weekday)) {
                    return true;
                }
            }
            if !self.nth_year.is_empty() {
                let number = day.ordinal() as i32;
                if self.nth_year.contains(&((number - 1) / 7 + 1, weekday)) || self.nth_year.contains(&(-((year_length(day.year()) - number) / 7 + 1), weekday)) {
                    return true;
                }
            }
            return false;
        }
        true
    }

    fn pick(&self, moments: Vec<NaiveDateTime>) -> Vec<NaiveDateTime> {
        if self.rule.bysetpos.is_empty() || moments.is_empty() {
            return moments;
        }
        let mut chosen: Vec<usize> = Vec::new();
        let len = moments.len() as i64;
        for position in &self.rule.bysetpos {
            let index = if *position > 0 { i64::from(*position) - 1 } else { len + i64::from(*position) };
            if (0..len).contains(&index) && !chosen.contains(&(index as usize)) {
                chosen.push(index as usize);
            }
        }
        chosen.sort_unstable();
        chosen.into_iter().map(|i| moments[i]).collect()
    }

    fn days_times(&self, days: impl IntoIterator<Item = NaiveDate>) -> Vec<NaiveDateTime> {
        let mut moments = Vec::new();
        for day in days {
            if self.day_ok(day) {
                for time in &self.times {
                    moments.push(day.and_time(*time));
                }
            }
        }
        self.pick(moments)
    }

    /// The candidate set of period `index` (periods before `skip_to` left
    /// out); `None` past the last year.
    fn period(&self, index: i64) -> Option<Vec<NaiveDateTime>> {
        let interval = i64::from(self.rule.interval);
        let start = self.start;
        match self.rule.freq {
            Freq::Yearly => {
                let year = i64::from(start.year()) + index * interval;
                if year > i64::from(MAX_YEAR) {
                    return None;
                }
                let year = year as i32;
                if !self.bymonth.is_empty() && self.byweekno.is_empty() && self.byyearday.is_empty() {
                    let mut months: Vec<i32> = self.bymonth.iter().copied().collect();
                    months.sort_unstable();
                    let days = months.into_iter().flat_map(|m| (1..=month_length(year, m as u32)).filter_map(move |d| NaiveDate::from_ymd_opt(year, m as u32, d)));
                    Some(self.days_times(days))
                } else {
                    let first = NaiveDate::from_ymd_opt(year, 1, 1)?;
                    Some(self.days_times((0..year_length(year)).map(|o| first + Duration::days(i64::from(o)))))
                }
            }
            Freq::Monthly => {
                let base = i64::from(start.year()) * 12 + i64::from(start.month()) - 1;
                let n = base + index * interval;
                let (year, month) = (n.div_euclid(12), n.rem_euclid(12) as u32 + 1);
                if year > i64::from(MAX_YEAR) {
                    return None;
                }
                let year = year as i32;
                Some(self.days_times((1..=month_length(year, month)).filter_map(|d| NaiveDate::from_ymd_opt(year, month, d))))
            }
            Freq::Weekly => {
                let back = (i64::from(start.weekday().num_days_from_monday()) - i64::from(self.rule.wkst)).rem_euclid(7);
                let first = start.date() - Duration::days(back);
                let week = first.checked_add_signed(Duration::try_days((7 * interval).checked_mul(index)?)?)?;
                if week.year() > MAX_YEAR {
                    return None;
                }
                Some(self.days_times((0..7).map(|o| week + Duration::days(o))))
            }
            Freq::Daily => {
                let day = start.date().checked_add_signed(Duration::try_days(interval.checked_mul(index)?)?)?;
                if day.year() > MAX_YEAR {
                    return None;
                }
                Some(self.days_times([day]))
            }
            _ => None,
        }
    }

    /// The first period index for `skip_to`.
    fn first_index(&self, skip_to: Option<NaiveDateTime>) -> i64 {
        let Some(skip) = skip_to else {
            return 0;
        };
        let interval = i64::from(self.rule.interval);
        let start = self.start;
        match self.rule.freq {
            Freq::Yearly if skip.year() > start.year() => (i64::from(skip.year() - start.year()) / interval - 1).max(0),
            Freq::Monthly => {
                let ahead = i64::from(skip.year()) * 12 + i64::from(skip.month()) - 1 - (i64::from(start.year()) * 12 + i64::from(start.month()) - 1);
                (ahead.div_euclid(interval) - 1).max(0)
            }
            Freq::Weekly => {
                let back = (i64::from(start.weekday().num_days_from_monday()) - i64::from(self.rule.wkst)).rem_euclid(7);
                let first = start.date() - Duration::days(back);
                ((skip.date() - first).num_days().div_euclid(7 * interval) - 1).max(0)
            }
            Freq::Daily => ((skip.date() - start.date()).num_days().div_euclid(interval) - 1).max(0),
            _ => 0,
        }
    }

    /// The periods of HOURLY, MINUTELY and SECONDLY rules, one step at a
    /// time, whole days and hours that cannot match jumped over.
    fn small_walk(&self, skip_to: Option<NaiveDateTime>, mut visit: impl FnMut(Vec<NaiveDateTime>) -> bool) {
        let unit: i64 = match self.rule.freq {
            Freq::Hourly => 3600,
            Freq::Minutely => 60,
            _ => 1,
        };
        let step = unit * i64::from(self.rule.interval);
        let base = match self.rule.freq {
            Freq::Hourly => self.start.with_minute(0).and_then(|t| t.with_second(0)),
            Freq::Minutely => self.start.with_second(0),
            _ => Some(self.start),
        };
        let Some(base) = base else {
            return;
        };
        let mut index: i64 = 0;
        if let Some(skip) = skip_to.filter(|s| *s > base) {
            index = ((skip - base).num_seconds() / step - 1).max(0);
        }
        let jump = |index: i64, target: NaiveDateTime| -> i64 {
            let seconds = (target - base).num_seconds();
            (index + 1).max(-((-seconds).div_euclid(step)))
        };
        loop {
            let Some(moment) = Duration::try_seconds(step.saturating_mul(index)).and_then(|d| base.checked_add_signed(d)) else {
                return;
            };
            if moment.year() > MAX_YEAR {
                return;
            }
            if self.day_filtered && !self.day_ok(moment.date()) {
                let Some(next_day) = moment.date().succ_opt() else {
                    return;
                };
                index = jump(index, next_day.and_time(NaiveTime::MIN));
                if !visit(Vec::new()) {
                    return;
                }
                continue;
            }
            if !self.byhour.is_empty() && !self.byhour.contains(&(moment.hour() as i32)) {
                let Some(hour) = moment.with_minute(0).and_then(|t| t.with_second(0)) else {
                    return;
                };
                index = jump(index, hour + Duration::hours(1));
                if !visit(Vec::new()) {
                    return;
                }
                continue;
            }
            let found: Vec<NaiveDateTime> = match self.rule.freq {
                Freq::Hourly => self.minutes.iter().flat_map(|m| self.seconds.iter().filter_map(move |s| moment.with_minute(*m).and_then(|t| t.with_second(*s)))).collect(),
                Freq::Minutely => {
                    if !self.byminute.is_empty() && !self.byminute.contains(&(moment.minute() as i32)) {
                        Vec::new()
                    } else {
                        self.seconds.iter().filter_map(|s| moment.with_second(*s)).collect()
                    }
                }
                _ => {
                    if (!self.byminute.is_empty() && !self.byminute.contains(&(moment.minute() as i32))) || (!self.bysecond.is_empty() && !self.bysecond.contains(&(moment.second() as i32))) {
                        Vec::new()
                    } else {
                        vec![moment]
                    }
                }
            };
            if !visit(self.pick(found)) {
                return;
            }
            index += 1;
        }
    }

    /// Gives every period's candidates to `visit` until it says stop.
    fn each_period(&self, skip_to: Option<NaiveDateTime>, mut visit: impl FnMut(Vec<NaiveDateTime>) -> bool) {
        if self.rule.freq < Freq::Daily {
            return self.small_walk(skip_to, visit);
        }
        let mut index = self.first_index(skip_to);
        while let Some(candidates) = self.period(index) {
            if !visit(candidates) {
                return;
            }
            index += 1;
        }
    }
}

/// The start, then what the rule gives after it while `within` holds, at
/// most `budget` candidates (`_walk`). Returns whether the budget ran out.
fn walk(rule: &Rule, start: NaiveDateTime, skip_to: Option<NaiveDateTime>, within: &dyn Fn(NaiveDateTime) -> bool, budget: usize, out: &mut dyn FnMut(NaiveDateTime) -> bool) -> bool {
    if !out(start) {
        return false;
    }
    if !within(start) {
        return false;
    }
    let expander = Expander::new(rule, start);
    let most_empty = rule.freq.most_empty();
    let (mut empty, mut spent, mut cut) = (0usize, 0usize, false);
    expander.each_period(skip_to, |candidates| {
        let mut produced = false;
        for moment in candidates {
            if moment <= start {
                continue;
            }
            if !within(moment) {
                return false;
            }
            spent += 1;
            if spent > budget {
                cut = true;
                return false;
            }
            produced = true;
            if !out(moment) {
                return false;
            }
        }
        if produced {
            empty = 0;
        } else {
            empty += 1;
            spent += 1;
            if empty > most_empty {
                return false;
            }
            if spent > budget {
                cut = true;
                return false;
            }
        }
        true
    });
    cut
}

/// The last occurrence of a rule with COUNT (`_count_end`); `None` when the
/// series is taken as endless.
fn count_end(rule: &Rule, start: NaiveDateTime) -> Option<NaiveDateTime> {
    let count = rule.count?;
    let steps = i64::try_from(count - 1).ok()?.checked_mul(i64::from(rule.interval))?;
    if rule.plain() {
        let seconds = match rule.freq {
            Freq::Secondly => Some(1),
            Freq::Minutely => Some(60),
            Freq::Hourly => Some(3600),
            Freq::Daily => Some(86400),
            Freq::Weekly => Some(7 * 86400),
            _ => None,
        };
        if let Some(unit) = seconds {
            return start.checked_add_signed(Duration::try_seconds(steps.checked_mul(unit)?)?);
        }
        if rule.freq == Freq::Monthly && start.day() <= 28 {
            let months = i64::from(start.year()) * 12 + i64::from(start.month()) - 1 + steps;
            return start.with_day(1)?.with_year(months.div_euclid(12) as i32)?.with_month(months.rem_euclid(12) as u32 + 1)?.with_day(start.day());
        }
        if rule.freq == Freq::Yearly && (start.month(), start.day()) != (2, 29) {
            return start.with_year(i32::try_from(i64::from(start.year()) + steps).ok()?);
        }
    }
    let until = match rule.until {
        Some(Until::Wall(time)) => Some(time),
        _ => None,
    };
    let within = |moment: NaiveDateTime| until.is_none_or(|u| moment <= u);
    let mut plain = rule.clone();
    plain.count = None;
    let (mut seen, mut last) = (0u64, start);
    let mut finished = false;
    let cut = walk(&plain, start, None, &within, COUNT_WALK, &mut |moment| {
        seen += 1;
        last = moment;
        if seen >= count {
            finished = true;
            return false;
        }
        true
    });
    if finished {
        Some(last)
    } else if cut {
        None
    } else {
        Some(last)
    }
}

/// The occurrences with `window_start <= occurrence < window_end` of a rule
/// for an event that starts at `start` (`between`). `instant` converts a
/// wall time to an instant, for an UNTIL written in UTC.
pub fn between(rule: &Rule, start: NaiveDateTime, window_start: NaiveDateTime, window_end: NaiveDateTime, instant: &dyn Fn(NaiveDateTime) -> i64, limit: usize) -> Vec<NaiveDateTime> {
    let until_instant = match rule.until {
        Some(Until::Utc(at)) => Some(at),
        _ => None,
    };
    let mut last = match rule.until {
        Some(Until::Wall(time)) => Some(time),
        _ => None,
    };
    let mut remaining = rule.count;
    if remaining.is_some() && until_instant.is_none() {
        remaining = None;
        if let Some(end) = count_end(rule, start) {
            last = Some(last.map_or(end, |l| l.min(end)));
        }
    }
    let within = |moment: NaiveDateTime| match until_instant {
        Some(at) => instant(moment) <= at,
        None => last.is_none_or(|l| moment <= l),
    };
    let mut found = Vec::new();
    let skip = if remaining.is_none() { Some(window_start) } else { None };
    walk(rule, start, skip, &within, DEFAULT_BUDGET, &mut |moment| {
        if let Some(left) = remaining.as_mut() {
            if *left == 0 {
                return false;
            }
            *left -= 1;
        }
        if moment >= window_end {
            return false;
        }
        if moment >= window_start {
            found.push(moment);
            if found.len() >= limit {
                return false;
            }
        }
        true
    });
    found
}

// ---------------------------------------------------------------------------
// The document (model.py)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Calendar {
    pub id: String,
    pub name: String,
    pub color: String,
    pub visible: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Event {
    pub uid: String,
    pub calendar: String,
    pub summary: String,
    pub all_day: bool,
    pub start: Local,
    pub end: Local,
    /// A zone of the database, or empty: floating (all-day events always).
    pub tz: String,
    pub rule: Option<Rule>,
    pub rdates: Vec<Local>,
    pub exdates: Vec<Local>,
    /// The start this occurrence had in its series; `None` for a master.
    pub recurrence_id: Option<Local>,
}

impl Event {
    fn recurring(&self) -> bool {
        self.rule.is_some() || !self.rdates.is_empty()
    }

    fn duration(&self) -> Duration {
        self.end.as_time() - self.start.as_time()
    }
}

/// Calendar's document as far as the board needs it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Document {
    pub calendars: Vec<Calendar>,
    pub events: Vec<Event>,
}

fn clean(value: &Value, limit: usize) -> String {
    let text = value.as_str().unwrap_or_default().replace('\0', "");
    let joined = text.split_whitespace().collect::<Vec<_>>().join(" ");
    joined.chars().take(limit).collect()
}

fn local_list(value: &Value, all_day: bool) -> Vec<Local> {
    let mut out: Vec<Local> = value
        .as_array()
        .map(|list| list.iter().take(MAX_DATES).filter_map(Value::as_str).filter_map(Local::parse).map(|l| if all_day { Local::Date(l.date()) } else { Local::Time(l.as_time()) }).collect())
        .unwrap_or_default();
    out.sort();
    out.dedup();
    out
}

fn make_calendar(raw: &Value) -> Option<Calendar> {
    let id = raw.get("id")?.as_str()?;
    if id.is_empty() || id.len() > 64 || !id.chars().all(|c| c.is_alphanumeric() || c == '-' || c == '_') {
        return None;
    }
    let name = clean(&raw["name"], 80);
    let color = raw["color"].as_str().filter(|c| COLORS.contains(c)).unwrap_or(COLORS[0]);
    Some(Calendar {
        id: id.to_owned(),
        name: if name.is_empty() { "Calendar".into() } else { name },
        color: color.to_owned(),
        visible: raw.get("visible") != Some(&Value::Bool(false)),
    })
}

fn make_event(raw: &Value, calendars: &HashSet<String>, zones: &mut tzif::Cache) -> Option<Event> {
    let uid = raw.get("uid")?.as_str()?;
    if uid.trim().is_empty() || uid.len() > 512 || uid.chars().any(|c| (c as u32) < 0x20) {
        return None;
    }
    let calendar = raw.get("calendar")?.as_str()?;
    if !calendars.contains(calendar) {
        return None;
    }
    let start = Local::parse(raw.get("start")?.as_str()?)?;
    let end = match raw.get("end").and_then(Value::as_str).filter(|e| !e.is_empty()) {
        Some(text) => Local::parse(text)?,
        None => start,
    };
    let all_day = raw.get("all_day") == Some(&Value::Bool(true));
    let (start, end) = if all_day {
        let (s, e) = (start.date(), end.date());
        let e = if e <= s { s.succ_opt()? } else { e };
        (Local::Date(s), Local::Date(e))
    } else {
        let (s, e) = (start.as_time(), end.as_time());
        (Local::Time(s), Local::Time(if e < s { s } else { e }))
    };
    let mut tz = raw.get("tz").and_then(Value::as_str).unwrap_or_default().to_owned();
    if all_day || (!tz.is_empty() && zones.get(&tz).is_none()) {
        tz.clear();
    }
    let mut rule = raw.get("rrule").and_then(Value::as_str).filter(|r| !r.is_empty()).and_then(|r| Rule::parse(r.trim()));
    let recurrence_id = match raw.get("recurrence_id").and_then(Value::as_str).filter(|r| !r.is_empty()) {
        Some(text) => {
            rule = None;
            Some(Local::parse(text)?)
        }
        None => None,
    };
    let master = recurrence_id.is_none();
    Some(Event {
        uid: uid.trim().to_owned(),
        calendar: calendar.to_owned(),
        summary: clean(&raw["summary"], MAX_SUMMARY),
        all_day,
        start,
        end,
        tz,
        rule,
        rdates: if master { local_list(&raw["rdates"], all_day) } else { Vec::new() },
        exdates: if master { local_list(&raw["exdates"], all_day) } else { Vec::new() },
        recurrence_id,
    })
}

impl Document {
    /// Reads the document's text as Calendar does: what cannot be used is
    /// left out. `None` for text that is not a JSON object.
    pub fn parse(text: &str, zones: &mut tzif::Cache) -> Option<Self> {
        let raw: Value = serde_json::from_str(text).ok()?;
        let raw = raw.as_object()?;
        let mut document = Document::default();
        let mut seen: HashSet<String> = HashSet::new();
        for entry in raw.get("calendars").and_then(Value::as_array).into_iter().flatten() {
            if let Some(calendar) = make_calendar(entry) {
                if !seen.contains(&calendar.id) && document.calendars.len() < MAX_CALENDARS {
                    seen.insert(calendar.id.clone());
                    document.calendars.push(calendar);
                }
            }
        }
        let mut keys: HashMap<(String, Option<Local>), usize> = HashMap::new();
        for entry in raw.get("events").and_then(Value::as_array).into_iter().flatten().take(MAX_EVENTS) {
            if let Some(event) = make_event(entry, &seen, zones) {
                // A later event with the same identity replaces an earlier one.
                let key = (event.uid.clone(), event.recurrence_id.map(|r| Local::Time(r.as_time())));
                match keys.get(&key) {
                    Some(&index) => document.events[index] = event,
                    None => {
                        keys.insert(key, document.events.len());
                        document.events.push(event);
                    }
                }
            }
        }
        Some(document)
    }

    #[cfg(test)]
    pub fn calendar(&self, id: &str) -> Option<&Calendar> {
        self.calendars.iter().find(|c| c.id == id)
    }
}

/// One event on the board: an event, or one time of a series.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Occurrence {
    pub summary: String,
    pub calendar: String,
    pub all_day: bool,
    /// Wall times in the local zone (all-day events: their dates' midnights).
    pub start: NaiveDateTime,
    pub end: NaiveDateTime,
    pub start_epoch: i64,
    pub end_epoch: i64,
    pub uid: String,
}

/// What happens between the instants `start` and `end` (overlapping it),
/// in visible calendars, in order (`occurrences`).
pub fn occurrences(document: &Document, start: i64, end: i64, local: &Zone, zones: &mut tzif::Cache, limit: usize) -> Vec<Occurrence> {
    let visible: HashSet<&str> = document.calendars.iter().filter(|c| c.visible).map(|c| c.id.as_str()).collect();
    let masters: HashSet<&str> = document.events.iter().filter(|e| e.recurrence_id.is_none()).map(|e| e.uid.as_str()).collect();
    let mut overrides: HashMap<&str, Vec<&Event>> = HashMap::new();
    for event in document.events.iter().filter(|e| e.recurrence_id.is_some()) {
        overrides.entry(event.uid.as_str()).or_default().push(event);
    }
    let mut found: Vec<Occurrence> = Vec::new();
    for event in &document.events {
        if !visible.contains(event.calendar.as_str()) {
            continue;
        }
        if event.recurrence_id.is_some() {
            if masters.contains(event.uid.as_str()) {
                continue;
            }
            // An override whose series is not here stands alone.
            emit(event, event.start.as_time(), event.duration(), zone_of(event, local, zones), local, start, end, &mut found);
            continue;
        }
        let own = overrides.get(event.uid.as_str()).map(Vec::as_slice).unwrap_or_default();
        expand(event, own, start, end, local, zones, limit, &mut found);
        if found.len() >= 20_000 {
            break;
        }
    }
    found.sort_by(|a, b| {
        (a.start_epoch, !a.all_day, -a.end_epoch, a.summary.to_lowercase(), &a.uid).cmp(&(b.start_epoch, !b.all_day, -b.end_epoch, b.summary.to_lowercase(), &b.uid))
    });
    found.truncate(limit);
    found
}

fn zone_of(event: &Event, local: &Zone, zones: &mut tzif::Cache) -> Zone {
    if event.all_day || event.tz.is_empty() {
        return local.clone();
    }
    zones.get(&event.tz).cloned().unwrap_or_else(|| local.clone())
}

#[allow(clippy::too_many_arguments)]
fn emit(event: &Event, moment: NaiveDateTime, length: Duration, zone: Zone, local: &Zone, start: i64, end: i64, found: &mut Vec<Occurrence>) {
    let Some(finish) = moment.checked_add_signed(length) else {
        return;
    };
    let begin_epoch = to_epoch(moment, &zone);
    let end_epoch = to_epoch(finish, &zone);
    let overlaps = begin_epoch < end && (end_epoch > start || (end_epoch == begin_epoch && begin_epoch >= start));
    if !overlaps {
        return;
    }
    let (shown_start, shown_end) = if event.all_day { (moment, finish) } else { (from_epoch(begin_epoch, local), from_epoch(end_epoch, local)) };
    found.push(Occurrence {
        summary: if event.summary.is_empty() { "(No title)".into() } else { event.summary.clone() },
        calendar: event.calendar.clone(),
        all_day: event.all_day,
        start: shown_start,
        end: shown_end,
        start_epoch: begin_epoch,
        end_epoch,
        uid: event.uid.clone(),
    });
}

#[allow(clippy::too_many_arguments)]
fn expand(master: &Event, overrides: &[&Event], start: i64, end: i64, local: &Zone, zones: &mut tzif::Cache, limit: usize, found: &mut Vec<Occurrence>) {
    let zone = zone_of(master, local, zones);
    let first = master.start.as_time();
    let duration = master.duration();
    let override_ids: HashSet<String> = overrides.iter().filter_map(|o| o.recurrence_id).map(|r| if master.all_day { Local::Date(r.date()) } else { Local::Time(r.as_time()) }.format()).collect();
    if !master.recurring() {
        emit(master, first, duration, zone.clone(), local, start, end, found);
    } else {
        let exdates: HashSet<String> = master.exdates.iter().map(|d| d.format()).collect();
        let low = from_epoch(start, &zone) - duration - Duration::days(1);
        let high = from_epoch(end, &zone) + Duration::days(1);
        let mut moments: Vec<NaiveDateTime> = match &master.rule {
            Some(rule) => between(rule, first, low, high, &|moment| to_epoch(moment, &zone), limit.max(64)),
            None if low <= first && first < high => vec![first],
            None => Vec::new(),
        };
        if !master.rdates.is_empty() {
            moments.extend(master.rdates.iter().map(|d| d.as_time()).filter(|m| low <= *m && *m < high));
            moments.sort();
            moments.dedup();
        }
        for moment in moments {
            let rid = if master.all_day { Local::Date(moment.date()) } else { Local::Time(moment) }.format();
            if exdates.contains(&rid) || override_ids.contains(&rid) {
                continue;
            }
            emit(master, moment, duration, zone.clone(), local, start, end, found);
        }
    }
    for event in overrides {
        let zone = zone_of(event, local, zones);
        emit(event, event.start.as_time(), event.duration(), zone, local, start, end, found);
    }
}

/// The local zone: the database's entry of the system's zone, else the
/// offset the system uses now.
pub fn local_zone(zones: &mut tzif::Cache) -> Zone {
    if let Some(zone) = tzif::local_zone_name().and_then(|name| zones.get(&name).cloned()) {
        return zone;
    }
    Zone::fixed(chrono::Local::now().offset().local_minus_utc())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(text: &str) -> NaiveDateTime {
        Local::parse(text).unwrap().as_time()
    }

    fn rule(text: &str) -> Rule {
        Rule::parse(text).unwrap_or_else(|| panic!("{text}"))
    }

    fn expand_rule(text: &str, start: &str, from: &str, to: &str) -> Vec<String> {
        let utc = |m: NaiveDateTime| m.and_utc().timestamp();
        between(&rule(text), at(start), at(from), at(to), &utc, 5000).into_iter().map(|m| m.format("%Y-%m-%dT%H:%M").to_string()).collect()
    }

    #[test]
    fn rules_are_read_as_recurrence_py_reads_them() {
        let r = rule("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,-1FR;UNTIL=20261231T235959Z;WKST=SU");
        assert_eq!((r.freq, r.interval, r.wkst), (Freq::Weekly, 2, 6));
        assert_eq!(r.byday, [(0, 0), (-1, 4)]);
        assert!(matches!(r.until, Some(Until::Utc(_))));
        assert_eq!(rule("RRULE:freq=daily;count=3").count, Some(3));
        assert_eq!(rule("FREQ=MONTHLY;BYSECOND=60").bysecond, [59]);
        assert!(matches!(rule("FREQ=YEARLY;UNTIL=20270101").until, Some(Until::Wall(t)) if t == at("2027-01-01T23:59:59")));
        for bad in ["", "FREQ=FORTNIGHTLY", "FREQ=DAILY;COUNT=0", "FREQ=DAILY;INTERVAL=-1", "FREQ=DAILY;FREQ=DAILY", "FREQ=WEEKLY;BYDAY=XX", "FREQ=WEEKLY;BYDAY=54MO", "FREQ=MONTHLY;BYMONTHDAY=0", "FREQ=YEARLY;BYMONTH=13", "FREQ=DAILY;UNTIL=tomorrow", "FREQ=DAILY;COUNT"] {
            assert!(Rule::parse(bad).is_none(), "{bad}");
        }
        assert!(Rule::parse(&format!("FREQ=DAILY;BYHOUR={}", vec!["1"; 900].join(","))).is_none(), "too many values");
    }

    #[test]
    fn the_usual_rules_expand_like_calendar() {
        // Every weekday at 09:00.
        assert_eq!(
            expand_rule("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "2026-11-02T09:00:00", "2026-11-26T00:00:00", "2026-12-02T00:00:00"),
            ["2026-11-26T09:00", "2026-11-27T09:00", "2026-11-30T09:00", "2026-12-01T09:00"]
        );
        // Every other day, three times (the start counts).
        assert_eq!(expand_rule("FREQ=DAILY;INTERVAL=2;COUNT=3", "2026-11-20T08:00:00", "2026-11-01T00:00:00", "2026-12-31T00:00:00"), ["2026-11-20T08:00", "2026-11-22T08:00", "2026-11-24T08:00"]);
        // The 31st of each month skips the short months.
        assert_eq!(expand_rule("FREQ=MONTHLY", "2026-10-31T10:00:00", "2026-10-01T00:00:00", "2027-02-01T00:00:00"), ["2026-10-31T10:00", "2026-12-31T10:00", "2027-01-31T10:00"]);
        // The last Friday of the month.
        assert_eq!(expand_rule("FREQ=MONTHLY;BYDAY=-1FR", "2026-11-27T17:00:00", "2026-11-01T00:00:00", "2027-02-01T00:00:00"), ["2026-11-27T17:00", "2026-12-25T17:00", "2027-01-29T17:00"]);
        // The second-to-last weekday of the month (BYSETPOS).
        assert_eq!(expand_rule("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-2", "2026-11-27T12:00:00", "2026-11-01T00:00:00", "2027-01-01T00:00:00"), ["2026-11-27T12:00", "2026-12-30T12:00"]);
        // A birthday on 29 February: only in leap years.
        assert_eq!(expand_rule("FREQ=YEARLY", "2024-02-29T00:00:00", "2025-01-01T00:00:00", "2029-01-01T00:00:00"), ["2028-02-29T00:00"]);
        // Thanksgiving: the fourth Thursday of November.
        assert_eq!(expand_rule("FREQ=YEARLY;BYMONTH=11;BYDAY=4TH", "2020-11-26T00:00:00", "2026-01-01T00:00:00", "2028-01-01T00:00:00"), ["2026-11-26T00:00", "2027-11-25T00:00"]);
        // The start always counts, even when the rule would not give it.
        assert_eq!(expand_rule("FREQ=WEEKLY;BYDAY=MO;COUNT=2", "2026-11-26T09:00:00", "2026-11-01T00:00:00", "2027-01-01T00:00:00"), ["2026-11-26T09:00", "2026-11-30T09:00"]);
        // UNTIL as a date includes that day.
        assert_eq!(expand_rule("FREQ=DAILY;UNTIL=20261128", "2026-11-26T20:00:00", "2026-11-01T00:00:00", "2027-01-01T00:00:00"), ["2026-11-26T20:00", "2026-11-27T20:00", "2026-11-28T20:00"]);
        // Week numbers and year days.
        assert_eq!(expand_rule("FREQ=YEARLY;BYWEEKNO=1;BYDAY=MO", "2026-01-05T09:00:00", "2026-01-01T00:00:00", "2028-01-31T00:00:00"), ["2026-01-05T09:00", "2027-01-04T09:00", "2028-01-03T09:00"]);
        assert_eq!(expand_rule("FREQ=YEARLY;BYYEARDAY=-1", "2026-12-31T09:00:00", "2026-01-01T00:00:00", "2028-01-31T00:00:00"), ["2026-12-31T09:00", "2027-12-31T09:00"]);
        // Hourly, with whole hours that cannot match jumped over.
        assert_eq!(expand_rule("FREQ=HOURLY;INTERVAL=3;BYHOUR=9,12,15", "2026-11-26T09:30:00", "2026-11-26T00:00:00", "2026-11-27T13:00:00"), ["2026-11-26T09:30", "2026-11-26T12:30", "2026-11-26T15:30", "2026-11-27T09:30", "2026-11-27T12:30"]);
        assert_eq!(expand_rule("FREQ=MINUTELY;INTERVAL=20;COUNT=4", "2026-11-26T09:00:00", "2026-11-26T00:00:00", "2026-11-27T00:00:00"), ["2026-11-26T09:00", "2026-11-26T09:20", "2026-11-26T09:40", "2026-11-26T10:00"]);
    }

    #[test]
    fn a_window_far_from_the_start_is_reached_without_walking_everything() {
        let started = std::time::Instant::now();
        // A daily rule from 1990: the window in 2026 is entered directly.
        assert_eq!(expand_rule("FREQ=DAILY", "1990-01-01T07:00:00", "2026-11-26T00:00:00", "2026-11-28T00:00:00"), ["2026-11-26T07:00", "2026-11-27T07:00"]);
        // A rule that can never match ends instead of hanging.
        assert_eq!(expand_rule("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30", "2026-01-01T07:00:00", "2026-11-26T00:00:00", "2126-11-28T00:00:00"), Vec::<String>::new());
        // A count far beyond the window.
        assert_eq!(expand_rule("FREQ=WEEKLY;BYDAY=TU,TH;COUNT=4000", "2000-01-04T07:00:00", "2026-11-26T00:00:00", "2026-12-02T00:00:00"), ["2026-11-26T07:00", "2026-12-01T07:00"]);
        assert!(started.elapsed() < std::time::Duration::from_secs(5), "{:?}", started.elapsed());
    }

    fn sample() -> String {
        serde_json::json!({
            "version": 1,
            "calendars": [
                {"id": "personal", "name": "Personal", "color": "iris", "visible": true},
                {"id": "work", "name": "Work", "color": "coral"},
                {"id": "hidden", "name": "Hidden", "color": "mint", "visible": false},
                {"id": "../bad", "name": "Bad"}
            ],
            "events": [
                {"uid": "standup@x", "calendar": "work", "summary": "Stand-up", "all_day": false, "start": "2026-11-02T09:30:00", "end": "2026-11-02T09:45:00", "tz": "", "rrule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "exdates": ["2026-11-27T09:30:00"]},
                {"uid": "standup@x", "calendar": "work", "summary": "Stand-up (moved)", "all_day": false, "start": "2026-11-30T11:00:00", "end": "2026-11-30T11:15:00", "recurrence_id": "2026-11-30T09:30:00"},
                {"uid": "trip@x", "calendar": "personal", "summary": "Trip  to\nthe hills", "all_day": true, "start": "2026-11-28", "end": "2026-11-30"},
                {"uid": "secret@x", "calendar": "hidden", "summary": "Hidden", "all_day": false, "start": "2026-11-26T12:00:00", "end": "2026-11-26T13:00:00"},
                {"uid": "orphan@x", "calendar": "nowhere", "summary": "No calendar", "start": "2026-11-26T12:00:00"},
                {"uid": "", "calendar": "work", "summary": "No uid", "start": "2026-11-26T12:00:00"},
                {"uid": "late@x", "calendar": "work", "summary": "Call", "start": "2026-11-26T18:00:00", "end": "2026-11-26T17:00:00", "tz": "Mars/Olympus"},
                {"uid": "bad@x", "calendar": "work", "summary": "Bad time", "start": "26 November"},
                {"uid": "rdate@x", "calendar": "personal", "summary": "Lessons", "start": "2026-11-26T16:00:00", "end": "2026-11-26T17:00:00", "rrule": "nonsense", "rdates": ["2026-11-29T16:00:00"]}
            ]
        })
        .to_string()
    }

    #[test]
    fn the_document_is_read_as_calendar_reads_it() {
        let mut zones = tzif::Cache::new(Path::new("/nonexistent"));
        let doc = Document::parse(&sample(), &mut zones).unwrap();
        assert_eq!(doc.calendars.iter().map(|c| c.id.as_str()).collect::<Vec<_>>(), ["personal", "work", "hidden"]);
        assert!(doc.calendar("work").unwrap().visible && !doc.calendar("hidden").unwrap().visible);
        let uids: Vec<&str> = doc.events.iter().map(|e| e.uid.as_str()).collect();
        assert_eq!(uids, ["standup@x", "standup@x", "trip@x", "secret@x", "late@x", "rdate@x"], "unusable events are left out");
        let late = doc.events.iter().find(|e| e.uid == "late@x").unwrap();
        assert_eq!((late.end, late.tz.as_str()), (late.start, ""), "an end before the start; an unknown zone floats");
        let trip = doc.events.iter().find(|e| e.uid == "trip@x").unwrap();
        assert_eq!(trip.summary, "Trip to the hills");
        assert!(doc.events.iter().find(|e| e.uid == "rdate@x").unwrap().rule.is_none(), "a rule that cannot be read is dropped");
        for bad in ["", "[]", "nonsense", "{\"calendars\": 5}"] {
            let parsed = Document::parse(bad, &mut zones);
            assert!(parsed.is_none() || parsed.unwrap().events.is_empty(), "{bad}");
        }
    }

    #[test]
    fn the_next_days_come_out_in_order_with_overrides_and_exceptions() {
        let mut zones = tzif::Cache::new(Path::new("/nonexistent"));
        let doc = Document::parse(&sample(), &mut zones).unwrap();
        let local = Zone::fixed(3600);
        let from = to_epoch(at("2026-11-26T00:00:00"), &local);
        let to = to_epoch(at("2026-12-01T00:00:00"), &local);
        let list = occurrences(&doc, from, to, &local, &mut zones, 50);
        let shown: Vec<String> = list.iter().map(|o| format!("{} {}", o.start.format("%a %d %H:%M"), o.summary)).collect();
        assert_eq!(
            shown,
            [
                "Thu 26 09:30 Stand-up",
                "Thu 26 16:00 Lessons",
                "Thu 26 18:00 Call",
                "Sat 28 00:00 Trip to the hills",
                "Sun 29 16:00 Lessons",
                "Mon 30 11:00 Stand-up (moved)",
            ],
            "Friday's stand-up is an exception; Monday's was moved; the hidden calendar is not shown"
        );
        assert!(list[3].all_day);
        assert!(list.iter().all(|o| o.start_epoch < to));
    }

    #[test]
    fn an_event_in_another_zone_is_shown_in_local_time() {
        let mut zones = tzif::Cache::new(Path::new(tzif::ZONEINFO));
        if zones.get("Asia/Kolkata").is_none() {
            return;
        }
        let text = serde_json::json!({"calendars": [{"id": "c", "name": "C"}], "events": [
            {"uid": "a", "calendar": "c", "summary": "Call India", "start": "2026-11-26T19:30:00", "end": "2026-11-26T20:00:00", "tz": "Asia/Kolkata"}
        ]})
        .to_string();
        let doc = Document::parse(&text, &mut zones).unwrap();
        let local = Zone::fixed(0);
        let list = occurrences(&doc, 0, i64::MAX / 4, &local, &mut zones, 5);
        assert_eq!(list[0].start, at("2026-11-26T14:00:00"), "19:30 in India is 14:00 UTC");
    }

    #[test]
    fn only_the_users_own_regular_file_is_read() {
        let dir = std::env::temp_dir().join(format!("boswas-agenda-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("calendar.json");
        assert_eq!(read_own_file(&file, 100), Ok(None), "no file yet");
        std::fs::write(&file, "{}").unwrap();
        assert_eq!(read_own_file(&file, 100), Ok(Some("{}".into())));
        assert!(read_own_file(&file, 1).is_err(), "too large");
        let link = dir.join("link.json");
        std::os::unix::fs::symlink(&file, &link).unwrap();
        assert!(read_own_file(&link, 100).is_err(), "a symbolic link is not followed");
        assert!(read_own_file(&dir, 100).is_err(), "a directory");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
