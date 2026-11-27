//! What the widgets board shows besides the calendar and the weather: the
//! Clock application's world clocks and next alarm, how busy the processor
//! and the memory are, the space on the home file system, and the newest
//! picture in Pictures. Everything here reads files and is called from a
//! worker thread (the board sends its results to the event loop); nothing
//! writes anywhere.

use std::path::{Path, PathBuf};

use chrono::{Datelike, Duration};
use serde_json::Value;

use crate::{
    agenda::{self, from_epoch, read_own_file, to_epoch},
    tzif::{self, Zone},
};

/// Largest Clock document the shell reads (Clock keeps alarms, timers and
/// the focus history in it).
pub const MAX_CLOCK_BYTES: u64 = 4 * 1024 * 1024;
const MAX_WORLD: usize = 100;
const MAX_ALARMS: usize = 200;

/// Where Clock keeps its document.
pub fn clock_path() -> Option<PathBuf> {
    agenda::data_home().map(|dir| dir.join("com.boswas.Clock").join("clock.json"))
}

/// A place of the world clock.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Place {
    pub zone: String,
    pub name: String,
}

/// An alarm as Clock stores it.
#[derive(Clone, Debug, PartialEq)]
pub struct Alarm {
    pub name: String,
    pub hour: u32,
    pub minute: u32,
    /// Weekdays it rings on (Monday 0); empty: once.
    pub days: Vec<u32>,
    pub enabled: bool,
    /// When it was last switched on or edited (seconds since the epoch).
    pub armed: f64,
}

/// What the board needs of Clock's document.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ClockData {
    pub world: Vec<Place>,
    pub alarms: Vec<Alarm>,
}

fn clean(value: &Value, limit: usize) -> String {
    let text = value.as_str().unwrap_or_default().replace('\0', " ");
    text.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(limit).collect()
}

fn integer(value: &Value, low: i64, high: i64, default: i64) -> i64 {
    match value {
        Value::Number(n) => n.as_f64().filter(|f| f.is_finite()).map_or(default, |f| (f as i64).clamp(low, high)),
        _ => default,
    }
}

impl ClockData {
    /// Reads Clock's document as Clock's `normalize` does: wrong entries are
    /// left out, values clamped. `zones` tells which zones exist.
    pub fn parse(text: &str, zones: &mut tzif::Cache) -> Self {
        let mut data = ClockData::default();
        let Ok(raw) = serde_json::from_str::<Value>(text) else {
            return data;
        };
        for entry in raw["world"].as_array().into_iter().flatten() {
            let Some(zone) = entry["zone"].as_str() else {
                continue;
            };
            if zones.get(zone).is_none() || data.world.iter().any(|p| p.zone == zone) || data.world.len() >= MAX_WORLD {
                continue;
            }
            let mut name = clean(&entry["name"], 80);
            if name.is_empty() {
                name = zone.rsplit('/').next().unwrap_or(zone).replace('_', " ");
            }
            data.world.push(Place { zone: zone.to_owned(), name });
        }
        let mut ids: Vec<String> = Vec::new();
        for entry in raw["alarms"].as_array().into_iter().flatten() {
            let Some(id) = entry["id"].as_str().filter(|id| !id.is_empty() && id.len() <= 64 && id.chars().all(|c| c.is_alphanumeric() || c == '-' || c == '_')) else {
                continue;
            };
            if ids.iter().any(|i| i == id) || data.alarms.len() >= MAX_ALARMS {
                continue;
            }
            ids.push(id.to_owned());
            let mut days: Vec<u32> = entry["days"].as_array().into_iter().flatten().filter_map(Value::as_u64).filter(|d| *d <= 6).map(|d| d as u32).collect();
            days.sort_unstable();
            days.dedup();
            data.alarms.push(Alarm {
                name: clean(&entry["name"], 80),
                hour: integer(&entry["hour"], 0, 23, 7) as u32,
                minute: integer(&entry["minute"], 0, 59, 0) as u32,
                days,
                enabled: entry["enabled"] == Value::Bool(true),
                armed: entry["armed"].as_f64().filter(|f| f.is_finite()).map_or(0.0, |f| f.clamp(0.0, 1e12)),
            });
        }
        data
    }
}

impl Alarm {
    /// When the alarm rings next after the instant `after` (Clock's
    /// `alarm_next`): a repeating alarm on its weekdays at its local time,
    /// a one-time alarm once, the first time its time comes after it was
    /// switched on.
    pub fn next(&self, after: i64, local: &Zone) -> Option<i64> {
        if !self.enabled {
            return None;
        }
        let armed = self.armed as i64;
        let lower = if self.days.is_empty() { armed } else { after.max(armed) };
        let start = from_epoch(lower, local).date();
        for offset in 0..9 {
            let day = start.checked_add_signed(Duration::days(offset))?;
            if !self.days.is_empty() && !self.days.contains(&day.weekday().num_days_from_monday()) {
                continue;
            }
            let ring = to_epoch(day.and_hms_opt(self.hour, self.minute, 0)?, local);
            if ring > lower {
                if self.days.is_empty() {
                    return (ring > after).then_some(ring);
                }
                return Some(ring);
            }
        }
        None
    }
}

/// The alarm that rings next and when.
pub fn next_alarm<'a>(alarms: &'a [Alarm], now: i64, local: &Zone) -> Option<(&'a Alarm, i64)> {
    alarms.iter().filter_map(|alarm| alarm.next(now, local).map(|at| (alarm, at))).min_by_key(|(_, at)| *at)
}

/// Reads Clock's document; an empty one when there is none or it cannot be
/// used.
pub fn read_clock(path: &Path, zones: &mut tzif::Cache) -> ClockData {
    match read_own_file(path, MAX_CLOCK_BYTES) {
        Ok(Some(text)) => ClockData::parse(&text, zones),
        Ok(None) => ClockData::default(),
        Err(problem) => {
            tracing::warn!("{}: not shown on the widgets board: {problem}", path.display());
            ClockData::default()
        }
    }
}

// ---------------------------------------------------------------------------
// The system
// ---------------------------------------------------------------------------

/// The processor's counters from `/proc/stat`: busy and total time.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct CpuTimes {
    pub busy: u64,
    pub total: u64,
}

/// Reads the first line of `/proc/stat` (`cpu  user nice system idle iowait
/// irq softirq steal ...`).
pub fn parse_cpu(stat: &str) -> Option<CpuTimes> {
    let line = stat.lines().find(|l| l.starts_with("cpu "))?;
    let values: Vec<u64> = line.split_whitespace().skip(1).take(8).map(|v| v.parse().ok()).collect::<Option<Vec<u64>>>()?;
    if values.len() < 4 {
        return None;
    }
    let total: u64 = values.iter().sum();
    let idle = values[3] + values.get(4).copied().unwrap_or(0);
    Some(CpuTimes { busy: total.saturating_sub(idle), total })
}

/// The share of time the processor was busy between two readings, 0 to 1.
pub fn cpu_load(before: CpuTimes, after: CpuTimes) -> Option<f32> {
    let total = after.total.checked_sub(before.total)?;
    let busy = after.busy.checked_sub(before.busy)?;
    (total > 0).then(|| (busy as f32 / total as f32).clamp(0.0, 1.0))
}

/// Memory in bytes: (in use, total), from `/proc/meminfo`.
pub fn parse_memory(meminfo: &str) -> Option<(u64, u64)> {
    let field = |name: &str| -> Option<u64> {
        let line = meminfo.lines().find(|l| l.starts_with(name))?;
        let kib: u64 = line[name.len()..].split_whitespace().next()?.parse().ok()?;
        kib.checked_mul(1024)
    };
    let total = field("MemTotal:")?;
    let available = field("MemAvailable:")?;
    (total > 0).then(|| (total.saturating_sub(available), total))
}

/// Space on the file system of `path` in bytes: (used, total).
pub fn disk_space(path: &Path) -> Option<(u64, u64)> {
    let stat = rustix::fs::statvfs(path).ok()?;
    let total = stat.f_blocks.checked_mul(stat.f_frsize)?;
    let free = stat.f_bavail.checked_mul(stat.f_frsize)?;
    (total > 0).then(|| (total.saturating_sub(free), total))
}

/// One reading of the system.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Sample {
    pub cpu: Option<CpuTimes>,
    pub memory: Option<(u64, u64)>,
    pub disk: Option<(u64, u64)>,
}

/// Reads `/proc` and the home file system.
pub fn sample(home: &Path) -> Sample {
    Sample {
        cpu: std::fs::read_to_string("/proc/stat").ok().as_deref().and_then(parse_cpu),
        memory: std::fs::read_to_string("/proc/meminfo").ok().as_deref().and_then(parse_memory),
        disk: disk_space(home),
    }
}

/// `3.2 GB`, `512 MB`: sizes the way Files and Settings write them (powers
/// of 1000).
pub fn size_text(bytes: u64) -> String {
    const UNITS: [&str; 5] = ["bytes", "KB", "MB", "GB", "TB"];
    let mut value = bytes as f64;
    let mut unit = 0;
    while value >= 1000.0 && unit < UNITS.len() - 1 {
        value /= 1000.0;
        unit += 1;
    }
    if unit == 0 {
        format!("{bytes} bytes")
    } else if value < 10.0 {
        format!("{value:.1} {}", UNITS[unit])
    } else {
        format!("{value:.0} {}", UNITS[unit])
    }
}

// ---------------------------------------------------------------------------
// Pictures
// ---------------------------------------------------------------------------

/// Picture files the board can show (what the shell can decode).
fn is_picture(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    [".png", ".jpg", ".jpeg"].iter().any(|ext| lower.ends_with(ext)) && !name.starts_with('.')
}

/// Largest picture file decoded for the board.
pub const MAX_PICTURE_BYTES: u64 = 40 * 1024 * 1024;

/// The newest picture in `dir` and one level of folders below it (Photos
/// shows the same), by modification time; symbolic links are not followed.
pub fn newest_picture(dir: &Path) -> Option<PathBuf> {
    let mut best: Option<(std::time::SystemTime, PathBuf)> = None;
    let mut consider = |path: PathBuf, meta: &std::fs::Metadata| {
        if !meta.file_type().is_file() || meta.len() == 0 || meta.len() > MAX_PICTURE_BYTES {
            return;
        }
        let Ok(modified) = meta.modified() else {
            return;
        };
        if best.as_ref().is_none_or(|(when, _)| modified > *when) {
            best = Some((modified, path));
        }
    };
    let mut looked = 0usize;
    let mut folders: Vec<PathBuf> = vec![dir.to_path_buf()];
    let mut depth = 0;
    while depth < 2 && !folders.is_empty() {
        let mut next = Vec::new();
        for folder in folders {
            let Ok(entries) = std::fs::read_dir(&folder) else {
                continue;
            };
            for entry in entries.flatten() {
                looked += 1;
                if looked > 5000 {
                    break;
                }
                let name = entry.file_name().to_string_lossy().into_owned();
                let Ok(meta) = entry.path().symlink_metadata() else {
                    continue;
                };
                if meta.file_type().is_dir() && !name.starts_with('.') {
                    next.push(entry.path());
                } else if is_picture(&name) {
                    consider(entry.path(), &meta);
                }
            }
        }
        folders = next;
        depth += 1;
    }
    best.map(|(_, path)| path)
}

/// A picture made at most `largest` pixels on its longer side (aspect
/// kept, never enlarged), decoded with the shell's limits; not through a
/// symbolic link.
pub fn thumbnail(path: &Path, largest: u32) -> Option<tiny_skia::Pixmap> {
    let meta = std::fs::symlink_metadata(path).ok()?;
    if !meta.file_type().is_file() || meta.len() > MAX_PICTURE_BYTES {
        return None;
    }
    let picture = boswas_ui::icons::decode_image(&std::fs::read(path).ok()?)?;
    let scale = (largest as f32 / picture.width().max(picture.height()) as f32).min(1.0);
    let (w, h) = (((picture.width() as f32 * scale).round() as u32).max(1), ((picture.height() as f32 * scale).round() as u32).max(1));
    cover(&picture, w, h)
}

/// Scales `picture` to cover `width` by `height`.
pub fn cover(picture: &tiny_skia::Pixmap, width: u32, height: u32) -> Option<tiny_skia::Pixmap> {
    let (pw, ph) = (picture.width() as f32, picture.height() as f32);
    let scale = (width as f32 / pw).max(height as f32 / ph);
    let mut out = tiny_skia::Pixmap::new(width.max(1), height.max(1))?;
    let transform = tiny_skia::Transform::from_scale(scale, scale).post_translate((width as f32 - pw * scale) / 2.0, (height as f32 - ph * scale) / 2.0);
    let paint = tiny_skia::PixmapPaint { quality: tiny_skia::FilterQuality::Bilinear, ..Default::default() };
    out.draw_pixmap(0, 0, picture.as_ref(), &paint, transform, None);
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn local() -> Zone {
        Zone::fixed(3600)
    }

    fn instant(text: &str) -> i64 {
        to_epoch(agenda::Local::parse(text).unwrap().as_time(), &local())
    }

    #[test]
    fn clocks_document_is_read_as_clock_normalizes_it() {
        let mut zones = tzif::Cache::new(Path::new(tzif::ZONEINFO));
        let text = serde_json::json!({
            "version": 1,
            "alarms": [
                {"id": "a1", "name": "Wake  up", "hour": 7, "minute": 30, "days": [0, 1, 2, 3, 4, 9, 2], "enabled": true, "armed": 0},
                {"id": "a1", "name": "Duplicate", "hour": 5, "enabled": true},
                {"id": "bad id!", "hour": 6, "enabled": true},
                {"id": "a2", "name": "Late", "hour": 99, "minute": -5, "enabled": "yes"}
            ],
            "world": [
                {"zone": "UTC", "name": ""},
                {"zone": "Nowhere/Atlantis", "name": "Atlantis"},
                {"zone": "UTC", "name": "Again"}
            ]
        })
        .to_string();
        let data = ClockData::parse(&text, &mut zones);
        assert_eq!(data.world, [Place { zone: "UTC".into(), name: "UTC".into() }], "unknown zones and repeats are left out");
        assert_eq!(data.alarms.len(), 2);
        assert_eq!((data.alarms[0].name.as_str(), data.alarms[0].days.as_slice()), ("Wake up", [0, 1, 2, 3, 4].as_slice()));
        assert_eq!((data.alarms[1].hour, data.alarms[1].minute, data.alarms[1].enabled), (23, 0, false), "clamped; only true is true");
        assert_eq!(ClockData::parse("not json", &mut zones), ClockData::default());
    }

    #[test]
    fn the_next_alarm_follows_clocks_rules() {
        let weekdays = Alarm { name: "Work".into(), hour: 7, minute: 0, days: vec![0, 1, 2, 3, 4], enabled: true, armed: 0.0 };
        // Thursday 26 November 2026, 08:00: the next weekday alarm is Friday 07:00.
        let now = instant("2026-11-26T08:00:00");
        assert_eq!(weekdays.next(now, &local()), Some(instant("2026-11-27T07:00:00")));
        // On Friday after 07:00: Monday.
        assert_eq!(weekdays.next(instant("2026-11-27T07:00:00"), &local()), Some(instant("2026-11-30T07:00:00")));
        // A one-time alarm rings the first time its time comes after it was armed.
        let once = Alarm { name: String::new(), hour: 6, minute: 15, days: Vec::new(), enabled: true, armed: instant("2026-11-26T05:00:00") as f64 };
        assert_eq!(once.next(instant("2026-11-26T05:30:00"), &local()), Some(instant("2026-11-26T06:15:00")));
        assert_eq!(once.next(instant("2026-11-26T07:00:00"), &local()), None, "it has rung");
        let off = Alarm { enabled: false, ..weekdays.clone() };
        assert_eq!(off.next(now, &local()), None);
        let alarms = vec![off, weekdays.clone(), Alarm { hour: 9, days: vec![3], ..weekdays }];
        let (alarm, at) = next_alarm(&alarms, now, &local()).unwrap();
        assert_eq!((alarm.hour, at), (9, instant("2026-11-26T09:00:00")), "the soonest of all");
    }

    #[test]
    fn the_system_is_read_from_proc() {
        let before = parse_cpu("cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 2 3 4\n").unwrap();
        let after = parse_cpu("cpu  150 0 150 900 0 0 0 0 0 0\n").unwrap();
        assert_eq!(before, CpuTimes { busy: 200, total: 1000 });
        assert!((cpu_load(before, after).unwrap() - 0.5).abs() < 1e-6);
        assert_eq!(cpu_load(after, before), None, "counters that went back");
        assert_eq!(cpu_load(before, before), None);
        assert!(parse_cpu("intr 5\n").is_none());
        let meminfo = "MemTotal:       16000000 kB\nMemFree:         1000000 kB\nMemAvailable:    4000000 kB\n";
        assert_eq!(parse_memory(meminfo), Some((12_000_000 * 1024, 16_000_000 * 1024)));
        assert!(parse_memory("MemTotal: x kB\n").is_none());
        // This machine's own /proc and root file system.
        let now = sample(Path::new("/"));
        assert!(now.cpu.is_some() && now.memory.is_some() && now.disk.is_some(), "{now:?}");
        assert_eq!(size_text(999), "999 bytes");
        assert_eq!(size_text(1_500_000), "1.5 MB");
        assert_eq!(size_text(256_000_000_000), "256 GB");
    }

    #[test]
    fn the_newest_picture_is_found_without_following_links() {
        let dir = std::env::temp_dir().join(format!("boswas-pictures-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("Holiday")).unwrap();
        let png = boswas_ui::icons::encode_png(&tiny_skia::Pixmap::new(40, 20).unwrap()).unwrap();
        std::fs::write(dir.join("old.png"), &png).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        std::fs::write(dir.join("Holiday/beach.png"), &png).unwrap();
        std::fs::write(dir.join("notes.txt"), "x").unwrap();
        assert_eq!(newest_picture(&dir), Some(dir.join("Holiday/beach.png")));
        std::thread::sleep(std::time::Duration::from_millis(20));
        std::os::unix::fs::symlink(dir.join("old.png"), dir.join("newer-link.png")).unwrap();
        assert_eq!(newest_picture(&dir), Some(dir.join("Holiday/beach.png")), "a link is not a picture of the folder");
        let thumb = thumbnail(&dir.join("old.png"), 20).unwrap();
        assert_eq!((thumb.width(), thumb.height()), (20, 10), "the aspect is kept");
        assert_eq!(thumbnail(&dir.join("old.png"), 400).map(|t| t.width()), Some(40), "never enlarged");
        assert!(thumbnail(&dir.join("newer-link.png"), 10).is_none());
        assert_eq!(newest_picture(&dir.join("missing")), None);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
