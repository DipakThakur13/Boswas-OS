//! Time zones of the time-zone database, for the widgets board: the
//! Calendar's events that have a zone of their own and the Clock's world
//! clocks. A zone is read from `/usr/share/zoneinfo/NAME` (TZif, RFC 8536):
//! its transitions, and for the time after the last of them the POSIX rule
//! at its end. Names are checked before any file is opened (no `..`, no
//! absolute paths), files are size-capped, and anything that is not a TZif
//! file of a sensible shape is no zone at all.
//!
//! Local times are turned into instants the way Python's `zoneinfo` does
//! with `fold=0` (what Calendar and Clock use): a time that is skipped when
//! summer time begins counts with the offset before the change, a time
//! that comes twice when it ends counts the first time.

use std::{collections::HashMap, io::Read, path::Path};

/// Largest zone file that is read.
const MAX_BYTES: u64 = 256 * 1024;
/// Most transitions accepted from one file.
const MAX_TRANSITIONS: usize = 4000;
/// Where the database is.
pub const ZONEINFO: &str = "/usr/share/zoneinfo";

/// One zone: when its offset changes and what it is before, between and
/// after the changes.
#[derive(Clone, Debug, PartialEq)]
pub struct Zone {
    /// Instants (seconds since the epoch) at which the offset changes, in
    /// order, and the offset (seconds east of UTC) from each on.
    transitions: Vec<(i64, i32)>,
    /// The offset before the first transition.
    initial: i32,
    /// The rule for the time after the last transition.
    rule: Option<Rule>,
}

/// A name of the database: `Europe/Berlin`, `UTC`, `America/Argentina/Salta`.
pub fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && !name.starts_with('/')
        && name.split('/').all(|part| !part.is_empty() && part != "." && part != ".." && !part.starts_with('-'))
        && name.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '/' | '_' | '-' | '+'))
}

fn be_i32(bytes: &[u8]) -> i32 {
    i32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]])
}

fn be_i64(bytes: &[u8]) -> i64 {
    let mut array = [0u8; 8];
    array.copy_from_slice(&bytes[..8]);
    i64::from_be_bytes(array)
}

/// The six counts of a TZif header.
struct Header {
    isutcnt: usize,
    isstdcnt: usize,
    leapcnt: usize,
    timecnt: usize,
    typecnt: usize,
    charcnt: usize,
}

fn header(data: &[u8]) -> Option<(u8, Header)> {
    if data.len() < 44 || &data[..4] != b"TZif" {
        return None;
    }
    let count = |at: usize| usize::try_from(be_i32(&data[at..])).ok();
    Some((
        data[4],
        Header {
            isutcnt: count(20)?,
            isstdcnt: count(24)?,
            leapcnt: count(28)?,
            timecnt: count(32)?,
            typecnt: count(36)?,
            charcnt: count(40)?,
        },
    ))
}

impl Header {
    fn body_len(&self, time_size: usize) -> usize {
        self.timecnt * time_size + self.timecnt + self.typecnt * 6 + self.charcnt + self.leapcnt * (time_size + 4) + self.isstdcnt + self.isutcnt
    }
}

impl Zone {
    /// A zone with one fixed offset.
    pub fn fixed(offset: i32) -> Self {
        Self { transitions: Vec::new(), initial: offset, rule: None }
    }

    /// Reads a TZif file's bytes.
    pub fn parse(data: &[u8]) -> Option<Self> {
        let (version, first) = header(data)?;
        let first_len = 44 + first.body_len(4);
        if data.len() < first_len {
            return None;
        }
        // Version 2 and later repeat the data with 64-bit times, then the rule.
        let (h, body, time_size, rest) = if version >= b'2' {
            let (_, second) = header(data.get(first_len..)?)?;
            let start = first_len + 44;
            let end = start + second.body_len(8);
            if data.len() < end {
                return None;
            }
            (second, &data[start..end], 8, &data[end..])
        } else {
            (first, &data[44..first_len], 4, &data[first_len..first_len])
        };
        if h.typecnt == 0 || h.typecnt > 256 || h.timecnt > MAX_TRANSITIONS {
            return None;
        }
        let times = &body[..h.timecnt * time_size];
        let indices = &body[h.timecnt * time_size..h.timecnt * (time_size + 1)];
        let types_at = h.timecnt * (time_size + 1);
        let offsets: Vec<i32> = (0..h.typecnt).map(|i| be_i32(&body[types_at + i * 6..])).collect();
        if offsets.iter().any(|o| o.abs() > 26 * 3600) {
            return None;
        }
        let mut transitions = Vec::with_capacity(h.timecnt);
        for i in 0..h.timecnt {
            let at = if time_size == 8 { be_i64(&times[i * 8..]) } else { i64::from(be_i32(&times[i * 4..])) };
            let kind = usize::from(indices[i]);
            let offset = *offsets.get(kind)?;
            if transitions.last().is_some_and(|(last, _): &(i64, i32)| *last >= at) {
                return None;
            }
            transitions.push((at, offset));
        }
        // RFC 8536: the time before the first transition has the first type.
        let initial = offsets[0];
        let rule = std::str::from_utf8(rest)
            .ok()
            .and_then(|text| text.strip_prefix('\n'))
            .and_then(|text| text.split('\n').next())
            .filter(|text| !text.is_empty())
            .and_then(Rule::parse);
        Some(Self { transitions, initial, rule })
    }

    /// Reads the zone `name` from a database directory.
    pub fn load_from(dir: &Path, name: &str) -> Option<Self> {
        if !valid_name(name) {
            return None;
        }
        let path = dir.join(name);
        let file = std::fs::File::open(&path).ok()?;
        let meta = file.metadata().ok()?;
        if !meta.is_file() || meta.len() > MAX_BYTES {
            return None;
        }
        let mut data = Vec::new();
        file.take(MAX_BYTES).read_to_end(&mut data).ok()?;
        Self::parse(&data)
    }

    /// The offset (seconds east of UTC) at an instant.
    pub fn offset_at(&self, instant: i64) -> i32 {
        match self.transitions.partition_point(|(at, _)| *at <= instant) {
            0 => self.initial,
            index if index == self.transitions.len() => match &self.rule {
                Some(rule) => rule.offset_at(instant),
                None => self.transitions[index - 1].1,
            },
            index => self.transitions[index - 1].1,
        }
    }

    /// The instant of a local wall time (seconds since the epoch as if the
    /// wall time were UTC), with `fold=0`.
    pub fn instant_of(&self, local: i64) -> i64 {
        instant_with(local, |instant| self.offset_at(instant))
    }
}

/// The instant of the wall time `local` in a zone whose offset at an
/// instant `offset_at` tells. Around a change the offset before it counts
/// unless only the reading with the new offset is a real time: a skipped
/// wall time keeps the old offset, a repeated one is taken the first time.
fn instant_with(local: i64, offset_at: impl Fn(i64) -> i32) -> i64 {
    let before = offset_at(local - 86400);
    let after = offset_at(local + 86400);
    if before == after {
        return local - i64::from(offset_at(local - i64::from(before)));
    }
    let old_reading = local - i64::from(before);
    let new_reading = local - i64::from(after);
    let old_real = offset_at(old_reading) == before;
    let new_real = offset_at(new_reading) == after;
    if new_real && !old_real {
        new_reading
    } else {
        old_reading
    }
}

/// When a summer time change happens in a year (POSIX TZ rule).
#[derive(Clone, Copy, Debug, PartialEq)]
enum Day {
    /// `Mm.w.d`: day `d` (0 is Sunday) of week `w` (5 is the last) of month `m`.
    MonthWeek(u32, u32, u32),
    /// `Jn`: day 1 to 365, February 29 never counted.
    Julian(u32),
    /// `n`: day 0 to 365, February 29 counted.
    Zero(u32),
}

/// The POSIX TZ string at the end of a TZif file: a standard offset and,
/// maybe, a summer time offset with the days it begins and ends.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Rule {
    std: i32,
    dst: Option<(i32, Day, i32, Day, i32)>,
}

/// Parses `[+-]hh[:mm[:ss]]` (POSIX: west of UTC is positive) from the
/// start of `text`; returns seconds and the rest.
fn posix_time(text: &str, signed: bool) -> Option<(i32, &str)> {
    let (sign, text) = match text.as_bytes().first() {
        Some(b'+') if signed => (1, &text[1..]),
        Some(b'-') if signed => (-1, &text[1..]),
        _ => (1, text),
    };
    let end = text.find(|c: char| !(c.is_ascii_digit() || c == ':')).unwrap_or(text.len());
    let mut parts = text[..end].split(':');
    let hours: i32 = parts.next()?.parse().ok()?;
    let minutes: i32 = parts.next().map_or(Some(0), |p| p.parse().ok())?;
    let seconds: i32 = parts.next().map_or(Some(0), |p| p.parse().ok())?;
    if parts.next().is_some() || !(0..=167).contains(&hours) || !(0..60).contains(&minutes) || !(0..60).contains(&seconds) {
        return None;
    }
    Some((sign * (hours * 3600 + minutes * 60 + seconds), &text[end..]))
}

/// Skips a zone abbreviation (`CET` or `<+0530>`).
fn abbreviation(text: &str) -> Option<&str> {
    if let Some(rest) = text.strip_prefix('<') {
        let end = rest.find('>')?;
        return Some(&rest[end + 1..]);
    }
    let end = text.find(|c: char| !c.is_ascii_alphabetic()).unwrap_or(text.len());
    (end >= 3).then(|| &text[end..])
}

fn posix_day(text: &str) -> Option<(Day, &str)> {
    let end = text.find(['/', ',']).unwrap_or(text.len());
    let (spec, rest) = text.split_at(end);
    let day = if let Some(m) = spec.strip_prefix('M') {
        let mut parts = m.split('.');
        let month: u32 = parts.next()?.parse().ok()?;
        let week: u32 = parts.next()?.parse().ok()?;
        let weekday: u32 = parts.next()?.parse().ok()?;
        if parts.next().is_some() || !(1..=12).contains(&month) || !(1..=5).contains(&week) || weekday > 6 {
            return None;
        }
        Day::MonthWeek(month, week, weekday)
    } else if let Some(n) = spec.strip_prefix('J') {
        let n: u32 = n.parse().ok()?;
        (1..=365).contains(&n).then_some(Day::Julian(n))?
    } else {
        let n: u32 = spec.parse().ok()?;
        (n <= 365).then_some(Day::Zero(n))?
    };
    Some((day, rest))
}

fn is_leap(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

/// Days since the epoch of a civil date.
pub fn days_from_civil(year: i32, month: u32, day: u32) -> i64 {
    let y = i64::from(year) - i64::from(month <= 2);
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let m = i64::from(month);
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + i64::from(day) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// The civil year of a day since the epoch.
fn year_of(days: i64) -> i32 {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + i64::from(month <= 2)) as i32
}

fn month_length(year: i32, month: u32) -> u32 {
    match month {
        2 if is_leap(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

impl Day {
    /// The day of the change in `year`, as days since the epoch.
    fn in_year(self, year: i32) -> i64 {
        match self {
            Day::MonthWeek(month, week, weekday) => {
                let first = days_from_civil(year, month, 1);
                // 1970-01-01 was a Thursday (4).
                let first_weekday = (first + 4).rem_euclid(7) as u32;
                let mut day = 1 + (weekday + 7 - first_weekday) % 7 + (week - 1) * 7;
                while day > month_length(year, month) {
                    day -= 7;
                }
                days_from_civil(year, month, day)
            }
            Day::Julian(n) => {
                let start = days_from_civil(year, 1, 1);
                start + i64::from(n) - 1 + i64::from(is_leap(year) && n >= 60)
            }
            Day::Zero(n) => days_from_civil(year, 1, 1) + i64::from(n),
        }
    }
}

impl Rule {
    pub fn parse(text: &str) -> Option<Self> {
        let rest = abbreviation(text)?;
        let (std_west, rest) = posix_time(rest, true)?;
        let std = -std_west;
        if rest.is_empty() {
            return Some(Self { std, dst: None });
        }
        let rest = abbreviation(rest)?;
        let (dst, rest) = match rest.as_bytes().first() {
            Some(b',') | None => (std + 3600, rest),
            _ => {
                let (west, rest) = posix_time(rest, true)?;
                (-west, rest)
            }
        };
        let rest = rest.strip_prefix(',')?;
        let (start, rest) = posix_day(rest)?;
        let (start_time, rest) = match rest.strip_prefix('/') {
            Some(time) => posix_time(time, true)?,
            None => (7200, rest),
        };
        let rest = rest.strip_prefix(',')?;
        let (end, rest) = posix_day(rest)?;
        let (end_time, rest) = match rest.strip_prefix('/') {
            Some(time) => posix_time(time, true)?,
            None => (7200, rest),
        };
        rest.is_empty().then_some(Self { std, dst: Some((dst, start, start_time, end, end_time)) })
    }

    /// The instants summer time begins and ends in `year`.
    fn changes(&self, year: i32) -> Option<(i64, i64, i32)> {
        let (dst, start, start_time, end, end_time) = self.dst?;
        // The start is given in standard local time, the end in summer time.
        let begins = start.in_year(year) * 86400 + i64::from(start_time) - i64::from(self.std);
        let ends = end.in_year(year) * 86400 + i64::from(end_time) - i64::from(dst);
        Some((begins, ends, dst))
    }

    pub fn offset_at(&self, instant: i64) -> i32 {
        let Some((begins, ends, dst)) = self.changes(year_of(instant.div_euclid(86400))) else {
            return self.std;
        };
        let summer = if begins < ends { instant >= begins && instant < ends } else { instant >= begins || instant < ends };
        if summer {
            dst
        } else {
            self.std
        }
    }

    #[cfg(test)]
    pub fn instant_of(&self, local: i64) -> i64 {
        instant_with(local, |instant| self.offset_at(instant))
    }
}

/// Zones read once and kept (a world clock is drawn every second).
#[derive(Default)]
pub struct Cache {
    dir: Option<std::path::PathBuf>,
    zones: HashMap<String, Option<Zone>>,
}

impl Cache {
    pub fn new(dir: &Path) -> Self {
        Self { dir: Some(dir.to_path_buf()), zones: HashMap::new() }
    }

    pub fn get(&mut self, name: &str) -> Option<&Zone> {
        if name == "UTC" || name == "Etc/UTC" {
            self.zones.entry(name.to_owned()).or_insert_with(|| Some(Zone::fixed(0)));
        }
        if !self.zones.contains_key(name) {
            if self.zones.len() > 256 {
                self.zones.clear();
            }
            let dir = self.dir.clone().unwrap_or_else(|| ZONEINFO.into());
            self.zones.insert(name.to_owned(), Zone::load_from(&dir, name));
        }
        self.zones.get(name).and_then(Option::as_ref)
    }
}

/// The name of the local zone: `TZ` (a name, or a path into the database),
/// else what `/etc/localtime` links to, else `/etc/timezone`.
pub fn local_zone_name() -> Option<String> {
    let from_path = |value: &str| -> Option<String> {
        let value = value.trim_start_matches(':');
        let name = match value.find("zoneinfo/") {
            Some(at) => &value[at + "zoneinfo/".len()..],
            None if !value.starts_with('/') => value,
            None => return None,
        };
        valid_name(name).then(|| name.to_owned())
    };
    if let Ok(tz) = std::env::var("TZ") {
        if !tz.is_empty() {
            return from_path(&tz);
        }
    }
    if let Ok(target) = std::fs::read_link("/etc/localtime") {
        if let Some(name) = from_path(&target.to_string_lossy()) {
            return Some(name);
        }
    }
    let text = std::fs::read_to_string("/etc/timezone").ok()?;
    let name = text.lines().next()?.trim();
    valid_name(name).then(|| name.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A TZif version 2 file: two transitions, two types, and a rule.
    pub fn sample(rule: &str) -> Vec<u8> {
        let mut data = Vec::new();
        let header = |data: &mut Vec<u8>, timecnt: u32, typecnt: u32, charcnt: u32| {
            data.extend_from_slice(b"TZif2");
            data.extend_from_slice(&[0; 15]);
            for count in [0u32, 0, 0, timecnt, typecnt, charcnt] {
                data.extend_from_slice(&count.to_be_bytes());
            }
        };
        // Version 1 block, empty but for one type.
        header(&mut data, 0, 1, 4);
        data.extend_from_slice(&3600i32.to_be_bytes());
        data.extend_from_slice(&[0, 0]);
        data.extend_from_slice(b"CET\0");
        // Version 2 block: summer time from 2026-03-29 01:00 UTC to 2026-10-25 01:00 UTC.
        header(&mut data, 2, 2, 9);
        data.extend_from_slice(&1_774_746_000i64.to_be_bytes());
        data.extend_from_slice(&1_792_890_000i64.to_be_bytes());
        data.extend_from_slice(&[1, 0]);
        data.extend_from_slice(&3600i32.to_be_bytes());
        data.extend_from_slice(&[0, 0]);
        data.extend_from_slice(&7200i32.to_be_bytes());
        data.extend_from_slice(&[1, 4]);
        data.extend_from_slice(b"CET\0CEST\0");
        data.push(b'\n');
        data.extend_from_slice(rule.as_bytes());
        data.push(b'\n');
        data
    }

    #[test]
    fn names_are_checked_before_any_file_is_opened() {
        for good in ["Europe/Berlin", "UTC", "America/Argentina/Salta", "Etc/GMT+5", "America/Port-au-Prince"] {
            assert!(valid_name(good), "{good}");
        }
        for bad in ["", "/etc/passwd", "../etc/passwd", "Europe/../../etc", "Europe//Berlin", "a b", "-x", "Europe/Berlin\n"] {
            assert!(!valid_name(bad), "{bad:?}");
        }
        assert!(Zone::load_from(Path::new("/"), "etc/../etc/passwd").is_none());
    }

    #[test]
    fn transitions_and_the_rule_after_them() {
        let zone = Zone::parse(&sample("CET-1CEST,M3.5.0,M10.5.0/3")).unwrap();
        // 2026-01-15 12:00 UTC: winter; 2026-07-01: summer; 2026-12-01: winter.
        assert_eq!(zone.offset_at(1_768_478_400), 3600);
        assert_eq!(zone.offset_at(1_782_907_200), 7200);
        assert_eq!(zone.offset_at(1_796_126_400), 3600);
        // After the last transition the rule decides: 2027-07-01 is summer.
        assert_eq!(zone.offset_at(1_814_443_200), 7200);
        assert_eq!(zone.offset_at(1_830_000_000), 3600, "2027-12-28 is winter");
        // The rule's own dates: summer time 2027 begins on 28 March 01:00 UTC.
        assert_eq!(zone.offset_at(1_806_195_600 - 1), 3600);
        assert_eq!(zone.offset_at(1_806_195_600), 7200);
    }

    #[test]
    fn local_times_become_instants_like_zoneinfo_with_fold_0() {
        let zone = Zone::parse(&sample("CET-1CEST,M3.5.0,M10.5.0/3")).unwrap();
        let local = |y, m, d, h: i64, min: i64| days_from_civil(y, m, d) * 86400 + h * 3600 + min * 60;
        // An ordinary summer hour.
        assert_eq!(zone.instant_of(local(2026, 7, 1, 14, 0)), local(2026, 7, 1, 12, 0));
        // 02:30 on 29 March 2026 does not exist: counted with the winter offset.
        assert_eq!(zone.instant_of(local(2026, 3, 29, 2, 30)), local(2026, 3, 29, 1, 30));
        // 02:30 on 25 October 2026 comes twice: the first time (summer).
        assert_eq!(zone.instant_of(local(2026, 10, 25, 2, 30)), local(2026, 10, 25, 0, 30));
        // After the transitions, by the rule: 2027 the same.
        assert_eq!(zone.instant_of(local(2027, 7, 1, 14, 0)), local(2027, 7, 1, 12, 0));
        assert_eq!(zone.instant_of(local(2027, 1, 1, 0, 0)), local(2026, 12, 31, 23, 0));
        assert_eq!(zone.instant_of(local(2027, 3, 28, 2, 30)), local(2027, 3, 28, 1, 30));
        assert_eq!(zone.instant_of(local(2027, 10, 31, 2, 30)), local(2027, 10, 31, 0, 30));
    }

    #[test]
    fn rules_of_other_shapes() {
        let india = Rule::parse("IST-5:30").unwrap();
        assert_eq!(india.offset_at(0), 19800);
        let numeric = Rule::parse("<+0545>-5:45").unwrap();
        assert_eq!(numeric.offset_at(1_000_000_000), 20700);
        let new_york = Rule::parse("EST5EDT,M3.2.0,M11.1.0").unwrap();
        // 2026-07-04 16:00 UTC: EDT; 2026-01-04: EST.
        assert_eq!(new_york.offset_at(1_783_180_800), -4 * 3600);
        assert_eq!(new_york.offset_at(1_767_542_400), -5 * 3600);
        // The southern hemisphere: summer over the new year.
        let chile = Rule::parse("<-04>4<-03>,M9.1.6/24,M4.1.6/24").unwrap();
        assert_eq!(chile.offset_at(1_767_225_600), -3 * 3600, "January is summer");
        assert_eq!(chile.offset_at(1_782_907_200), -4 * 3600, "July is winter");
        for bad in ["", "X", "CET", "CET-1CEST,M13.5.0,M10.5.0", "CET-1CEST,M3.5.0", "CET-1CEST,M3.5.0,M10.5.0/3x"] {
            assert!(Rule::parse(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn damaged_files_are_no_zone() {
        let good = sample("CET-1CEST,M3.5.0,M10.5.0/3");
        assert!(Zone::parse(&good[..good.len() - 30]).is_none(), "cut short");
        assert!(Zone::parse(b"TZif2 not really").is_none());
        assert!(Zone::parse(b"").is_none());
        let mut absurd = good.clone();
        // An offset of more than a day.
        let at = absurd.len() - 28 - 9;
        absurd[at..at + 4].copy_from_slice(&200_000i32.to_be_bytes());
        assert!(Zone::parse(&absurd).is_none() || Zone::parse(&absurd).unwrap().offset_at(0).abs() <= 26 * 3600);
        // Without a rule the last offset holds.
        let zone = Zone::parse(&sample("")).unwrap();
        assert_eq!(zone.offset_at(1_900_000_000), 3600);
    }

    #[test]
    fn the_system_database_is_read_when_it_is_there() {
        let mut cache = Cache::new(Path::new(ZONEINFO));
        assert_eq!(cache.get("UTC").map(|z| z.offset_at(0)), Some(0));
        if Path::new(ZONEINFO).join("Asia/Kolkata").is_file() {
            assert_eq!(cache.get("Asia/Kolkata").unwrap().offset_at(1_800_000_000), 19800);
        }
        if Path::new(ZONEINFO).join("Europe/Berlin").is_file() {
            let berlin = cache.get("Europe/Berlin").unwrap();
            assert_eq!(berlin.offset_at(1_782_907_200), 7200);
            assert_eq!(berlin.offset_at(1_768_478_400), 3600);
        }
        assert!(cache.get("Nowhere/Atlantis").is_none());
    }
}
