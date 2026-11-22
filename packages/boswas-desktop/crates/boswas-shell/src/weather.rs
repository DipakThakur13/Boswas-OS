//! The weather on the lock screen.
//!
//! The lock screen never talks to the network. The Weather application
//! (and its background update, `boswas-weather --update-lock-screen`)
//! keeps a small summary of the home place's forecast in
//! `$XDG_STATE_HOME/boswas/weather.json`, already worded and with the icons
//! chosen; this module reads it and draws it as a card in the lower left
//! corner of the clock view:
//!
//! ```text
//! {"version": 1, "place": "Berlin", "fetched": 1763731200, "unit": "c",
//!  "temperature": "8°C", "condition": "Partly cloudy",
//!  "icon": "weather-few-clouds-symbolic", "updated": "Updated at 14:20",
//!  "feels_like": "Feels like 5°", "high_low": "High 9° · Low 3°",
//!  "today_until": 1763766000,
//!  "hours": [{"at": 1763733600, "time": "15:00",
//!             "icon": "weather-overcast-symbolic", "temperature": "8°"}]}
//! ```
//!
//! Whatever the file holds, the lock screen stays correct and can be
//! unlocked: the file is read only when it changed (its `stat` is looked at
//! once a minute), never through a symbolic link, never when it is not a
//! regular file of the user or larger than [`MAX_BYTES`], and a value that
//! is missing, of the wrong kind, out of range or too long means no card at
//! all. A summary older than three hours is not shown; one older than an
//! hour says when it was fetched.

use std::{
    fs::File,
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
};

use boswas_ui::{
    tokens::{tokens, Material},
    Rgba, Scheme,
};
use serde_json::Value;
use tracing::warn;

use crate::ui::{ramp, Frame, Rect};

/// Largest summary that is read.
pub const MAX_BYTES: u64 = 16 * 1024;
/// The only version of the summary this shell understands.
pub const VERSION: u64 = 1;
/// A summary older than this (seconds) is not shown.
pub const MAX_AGE: i64 = 3 * 3600;
/// A summary older than this says when it was fetched.
pub const SAY_WHEN_AFTER: i64 = 3600;
/// A summary from the future (a clock that was set back) is believed this
/// far (seconds).
const FUTURE_SLACK: i64 = 300;
/// Times in the file are seconds since the epoch between 2000 and 2100.
const EPOCH_RANGE: std::ops::RangeInclusive<i64> = 946_684_800..=4_102_444_800;
/// Most hours the file may list.
const MAX_HOURS: usize = 48;
/// Hours shown on the card.
const SHOWN_HOURS: usize = 4;

/// One hour of the forecast.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Hour {
    /// Start of the hour, in seconds since the epoch.
    pub at: i64,
    /// Its label ("15:00", "3 PM").
    pub time: String,
    pub icon: String,
    pub temperature: String,
}

/// The summary as it was read and checked.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Summary {
    pub place: String,
    /// When the forecast was fetched, in seconds since the epoch.
    pub fetched: i64,
    pub temperature: String,
    pub condition: String,
    pub icon: String,
    /// "Updated at 14:20", shown once the summary is an hour old.
    pub updated: Option<String>,
    pub feels_like: Option<String>,
    /// Today's high and low, until `today_until`.
    pub high_low: Option<String>,
    pub today_until: Option<i64>,
    pub hours: Vec<Hour>,
}

/// What the card shows at a moment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Shown<'a> {
    pub summary: &'a Summary,
    /// The line under the condition: high and low, feels like.
    pub details: Option<String>,
    /// The next hours (at most four).
    pub hours: Vec<&'a Hour>,
    pub updated: Option<&'a str>,
}

/// The file's path: `$XDG_STATE_HOME/boswas/weather.json` (or the
/// `~/.local/state` equivalent), where the shell keeps its state files.
pub fn summary_path() -> Option<PathBuf> {
    crate::config::state_dir().map(|dir| dir.join("weather.json"))
}

/// Short plain text: 1 to `limit` characters, no control characters, not
/// only spaces.
fn text(value: &Value, limit: usize) -> Result<String, &'static str> {
    let text = value.as_str().ok_or("a text is missing or not a text")?;
    let count = text.chars().count();
    if count == 0 || count > limit || text.trim().is_empty() {
        return Err("a text is empty or too long");
    }
    // Control and direction-changing characters could disguise what is
    // shown; the writer never puts them there.
    if text.chars().any(|c| c.is_control() || matches!(c, '\u{200e}' | '\u{200f}' | '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')) {
        return Err("a text holds control characters");
    }
    Ok(text.to_owned())
}

/// A text that may be missing (absent or `null`).
fn optional_text(value: &Value, limit: usize) -> Result<Option<String>, &'static str> {
    if value.is_null() {
        Ok(None)
    } else {
        text(value, limit).map(Some)
    }
}

/// The name of a weather icon of the icon theme: `weather-[a-z-]+`.
fn icon(value: &Value) -> Result<String, &'static str> {
    let name = value.as_str().ok_or("an icon name is missing")?;
    let rest = name.strip_prefix("weather-").ok_or("an icon is not a weather icon")?;
    if rest.is_empty() || name.len() > 64 || !rest.chars().all(|c| c.is_ascii_lowercase() || c == '-') {
        return Err("an icon is not a weather icon");
    }
    Ok(name.to_owned())
}

/// A moment in seconds since the epoch: a whole number in a sane range.
fn moment(value: &Value) -> Result<i64, &'static str> {
    value.as_i64().filter(|t| EPOCH_RANGE.contains(t)).ok_or("a time is missing or out of range")
}

/// Reads and checks the summary's text. Anything unexpected rejects the
/// whole summary; keys this version does not know are ignored.
pub fn parse(text_of_file: &str) -> Result<Summary, &'static str> {
    let value: Value = serde_json::from_str(text_of_file).map_err(|_| "not JSON")?;
    if !value.is_object() {
        return Err("not a JSON object");
    }
    if value["version"].as_u64() != Some(VERSION) {
        return Err("not a summary of version 1");
    }
    if !matches!(value["unit"].as_str(), Some("c" | "f")) {
        return Err("the temperature unit is not c or f");
    }
    let hours = match &value["hours"] {
        Value::Null => Vec::new(),
        Value::Array(list) if list.len() <= MAX_HOURS => list
            .iter()
            .map(|hour| {
                Ok(Hour {
                    at: moment(&hour["at"])?,
                    time: text(&hour["time"], 12)?,
                    icon: icon(&hour["icon"])?,
                    temperature: text(&hour["temperature"], 12)?,
                })
            })
            .collect::<Result<Vec<Hour>, &'static str>>()?,
        _ => return Err("the hours are not a short list"),
    };
    let today_until = if value["today_until"].is_null() { None } else { Some(moment(&value["today_until"])?) };
    Ok(Summary {
        place: text(&value["place"], 100)?,
        fetched: moment(&value["fetched"])?,
        temperature: text(&value["temperature"], 12)?,
        condition: text(&value["condition"], 64)?,
        icon: icon(&value["icon"])?,
        updated: optional_text(&value["updated"], 64)?,
        feels_like: optional_text(&value["feels_like"], 64)?,
        high_low: optional_text(&value["high_low"], 64)?,
        today_until,
        hours,
    })
}

/// What identifies one version of the file: a change of any of these means
/// it has to be read again.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Stamp {
    dev: u64,
    ino: u64,
    size: u64,
    mtime: i64,
    mtime_nsec: i64,
}

impl Stamp {
    fn of(meta: &std::fs::Metadata) -> Self {
        Self {
            dev: meta.dev(),
            ino: meta.ino(),
            size: meta.size(),
            mtime: meta.mtime(),
            mtime_nsec: meta.mtime_nsec(),
        }
    }
}

/// Whether a file's metadata allows reading it: a regular file of this
/// user, not larger than [`MAX_BYTES`].
fn acceptable(meta: &std::fs::Metadata, uid: u32) -> Result<(), &'static str> {
    if !meta.file_type().is_file() {
        return Err("not a regular file");
    }
    if meta.uid() != uid {
        return Err("not the user's own file");
    }
    if meta.len() > MAX_BYTES {
        return Err("larger than 16 KB");
    }
    Ok(())
}

/// Reads the summary at `path`. A symbolic link is not followed, and the
/// file is opened without waiting (a FIFO in its place cannot hold the
/// lock screen up).
pub fn read(path: &Path, uid: u32) -> Result<Summary, &'static str> {
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_NOCTTY)
        .open(path)
        .map_err(|_| "cannot be opened (missing, or a symbolic link)")?;
    acceptable(&file.metadata().map_err(|_| "cannot be examined")?, uid)?;
    let mut bytes = Vec::new();
    file.take(MAX_BYTES + 1).read_to_end(&mut bytes).map_err(|_| "cannot be read")?;
    if bytes.len() as u64 > MAX_BYTES {
        return Err("larger than 16 KB");
    }
    parse(std::str::from_utf8(&bytes).map_err(|_| "not UTF-8 text")?)
}

/// The summary file as the lock screen follows it.
#[derive(Default)]
pub struct Watch {
    path: Option<PathBuf>,
    /// The user the file must belong to.
    uid: u32,
    /// The version of the file last looked at (`None`: there was none).
    stamp: Option<Stamp>,
    /// The minute the file was last looked at.
    minute: Option<i64>,
    /// The summary, when the file holds a good one.
    pub summary: Option<Summary>,
}

impl Watch {
    /// Follows the file at `path` (`None`: follows nothing; the desktop and
    /// the login screen show no weather).
    pub fn new(path: Option<PathBuf>) -> Self {
        Self {
            path,
            uid: rustix::process::geteuid().as_raw(),
            ..Self::default()
        }
    }

    /// Looks at the file when a new minute began since the last look.
    /// Returns whether the summary changed.
    pub fn poll(&mut self, now: i64) -> bool {
        let minute = now.div_euclid(60);
        if self.minute == Some(minute) {
            return false;
        }
        self.minute = Some(minute);
        self.check()
    }

    /// Looks at the file now: it is read again only when its `stat`
    /// differs from the last look, so a bad file is reported once per
    /// change, not once a minute. Returns whether the summary changed.
    pub fn check(&mut self) -> bool {
        let Some(path) = self.path.clone() else {
            return false;
        };
        let stamp = std::fs::symlink_metadata(&path).ok().map(|meta| Stamp::of(&meta));
        if stamp == self.stamp {
            return false;
        }
        self.stamp = stamp;
        let summary = match stamp {
            None => None,
            Some(_) => match read(&path, self.uid) {
                Ok(summary) => Some(summary),
                Err(problem) => {
                    warn!("{}: not shown on the lock screen: {problem}", path.display());
                    None
                }
            },
        };
        let changed = summary != self.summary;
        self.summary = summary;
        changed
    }
}

impl Summary {
    /// What the card shows at `now` (seconds since the epoch); `None` when
    /// the summary is too old or comes from the future.
    pub fn shown(&self, now: i64) -> Option<Shown<'_>> {
        let age = now - self.fetched;
        if !(-FUTURE_SLACK..=MAX_AGE).contains(&age) {
            return None;
        }
        let high_low = self.high_low.as_deref().filter(|_| self.today_until.is_none_or(|until| now < until));
        let details: Vec<&str> = [high_low, self.feels_like.as_deref()].into_iter().flatten().collect();
        Some(Shown {
            summary: self,
            details: (!details.is_empty()).then(|| details.join(" · ")),
            hours: self.hours.iter().filter(|hour| hour.at > now).take(SHOWN_HOURS).collect(),
            updated: self.updated.as_deref().filter(|_| age >= SAY_WHEN_AFTER),
        })
    }
}

// -------------------------------------------------------------------------
// The card
// -------------------------------------------------------------------------

/// Distance of the card from the left and bottom edges (the status icons
/// keep the same distance from the right edge).
pub const MARGIN: f32 = 28.0;
const PAD: f32 = 16.0;
const HOUR_ICON: f32 = 20.0;
/// Room left between the card and the unlock hint.
const GAP: f32 = 24.0;

/// Where the card may go: the lock screen's size, the lowest point of the
/// clock and the date, and the line of the unlock hint.
#[derive(Clone, Copy, Debug)]
pub struct Room {
    pub width: f32,
    pub height: f32,
    /// The lowest point of what is drawn above (the date).
    pub top: f32,
    /// The unlock hint: its rectangle (centred on the screen).
    pub hint: Rect,
}

/// The size of the large icon for a temperature line: 40 px at the normal
/// text size, growing with the text up to 64 px.
fn icon_size(temperature_line: f32) -> f32 {
    (temperature_line * 0.75).round().clamp(32.0, 64.0).max(40.0_f32.min(temperature_line))
}

/// Draws the card for `shown`. Returns its rectangle (`None` when there is
/// no room for it).
pub fn draw(f: &mut Frame, shown: &Shown, room: Room) -> Option<Rect> {
    let white = Rgba::WHITE;
    let quiet = Rgba::WHITE.alpha(0.78);
    let temperature = ramp("title-large", white);
    let condition = ramp("body-strong", white);
    let place = ramp("body", quiet);
    let details = ramp("caption", quiet);
    let hour_label = ramp("caption", quiet);
    let hour_temperature = ramp("body-strong", white);
    let updated = ramp("caption", quiet);
    let s = shown.summary;

    let icon = icon_size(temperature.line_height());
    let measure = |f: &mut Frame, text: &str, style| f.res.text.measure(text, style).0;
    let temperature_w = measure(f, &s.temperature, &temperature);
    let column_w = measure(f, &s.condition, &condition).max(measure(f, &s.place, &place));
    let details_w = shown.details.as_deref().map_or(0.0, |d| measure(f, d, &details));
    let updated_w = shown.updated.map_or(0.0, |u| measure(f, u, &updated));
    let hour_w = shown
        .hours
        .iter()
        .map(|h| measure(f, &h.time, &hour_label).max(measure(f, &h.temperature, &hour_temperature)))
        .fold(HOUR_ICON, f32::max)
        + 12.0;
    let head_w = icon + 12.0 + temperature_w;
    let side_by_side_w = head_w + 16.0 + column_w;
    let natural = side_by_side_w.max(details_w).max(updated_w).max(hour_w * shown.hours.len() as f32) + 2.0 * PAD;

    // Beside the unlock hint when the card fits there, else above it.
    let beside = room.hint.x - GAP - MARGIN;
    let (width, bottom) = if natural.min(320.0) <= beside {
        (natural.min(beside), room.height - MARGIN)
    } else {
        (natural.min(room.width - 2.0 * MARGIN), room.hint.y - GAP / 2.0)
    };
    let inner = width - 2.0 * PAD;
    if inner < icon {
        return None;
    }
    let side_by_side = side_by_side_w <= inner;
    let column_h = condition.line_height() + place.line_height();
    let head_h = if side_by_side {
        icon.max(temperature.line_height()).max(column_h)
    } else {
        icon.max(temperature.line_height()) + 4.0 + column_h
    };
    let details_h = shown.details.as_ref().map_or(0.0, |_| 4.0 + details.line_height());
    let hours = shown.hours.len().min((inner / hour_w).floor() as usize);
    let hours_h = if hours > 0 { 12.0 + 1.0 + 10.0 + hour_label.line_height() + 4.0 + HOUR_ICON + 4.0 + hour_temperature.line_height() } else { 0.0 };
    let updated_h = shown.updated.map_or(0.0, |_| 8.0 + updated.line_height());
    let height = PAD + head_h + details_h + hours_h + updated_h + PAD;
    let card = Rect::new(MARGIN, bottom - height, width, height);
    if card.y < room.top + GAP {
        return None;
    }

    // The card: the blurred wallpaper under the dark acrylic tint (the text
    // on the lock screen is always white), a soft shadow and an outline.
    let t = tokens();
    let radius = t.radius_flyout;
    let shadow = t.elevation("tooltip");
    f.canvas.shadow(card.x, card.y, card.w, card.h, radius, shadow.blur, shadow.y, shadow.alpha);
    let material: Material = t.material(Scheme::Dark, "acrylic");
    let transparency = f.res.transparency;
    match f.res.backdrop.as_mut().filter(|_| transparency) {
        Some(backdrop) => backdrop.fill(f.canvas, card.x, card.y, card.w, card.h, [radius; 4], f.origin, material),
        // No wallpaper to blur: the tint alone, translucent unless
        // transparency is off.
        None => {
            let opacity = if transparency { material.tint_opacity } else { 1.0 };
            f.canvas.fill_rounded_rect(card.x, card.y, card.w, card.h, radius, material.tint.alpha(opacity));
        }
    }
    f.canvas.stroke_rounded_rect(card.x + 0.5, card.y + 0.5, card.w - 1.0, card.h - 1.0, radius, 1.0, t.stroke(Scheme::Dark, "surface"));

    // The weather now.
    let (x, mut y) = (card.x + PAD, card.y + PAD);
    let row_h = icon.max(temperature.line_height());
    let row_h = if side_by_side { row_h.max(column_h) } else { row_h };
    f.icon_tinted(&s.icon, x, y + (row_h - icon) / 2.0, icon, white);
    let tx = x + icon + 12.0;
    f.text(&s.temperature, tx, y + (row_h - temperature.line_height()) / 2.0, Some(card.right() - PAD - tx), &temperature);
    let (cx, cy, cw) = if side_by_side {
        let cx = tx + temperature_w + 16.0;
        (cx, y + (row_h - column_h) / 2.0, card.right() - PAD - cx)
    } else {
        (x, y + row_h + 4.0, inner)
    };
    f.text(&s.condition, cx, cy, Some(cw), &condition);
    f.text(&s.place, cx, cy + condition.line_height(), Some(cw), &place);
    y += head_h;
    if let Some(line) = &shown.details {
        y += 4.0;
        f.text(line, x, y, Some(inner), &details);
        y += details.line_height();
    }

    // The next hours, in columns of equal width.
    if hours > 0 {
        y += 12.0;
        f.canvas.fill_rect(x, y, inner, 1.0, t.stroke(Scheme::Dark, "divider"));
        y += 11.0;
        let column = inner / hours as f32;
        for (index, hour) in shown.hours.iter().take(hours).enumerate() {
            let cell = Rect::new(x + index as f32 * column, y, column, hour_label.line_height());
            f.text_centered(&hour.time, cell, &hour_label);
            let iy = cell.bottom() + 4.0;
            f.icon_tinted(&hour.icon, cell.x + (column - HOUR_ICON) / 2.0, iy, HOUR_ICON, white);
            let below = Rect::new(cell.x, iy + HOUR_ICON + 4.0, column, hour_temperature.line_height());
            f.text_centered(&hour.temperature, below, &hour_temperature);
        }
        y += hours_h - 23.0;
    }
    if let Some(text) = shown.updated {
        y += 8.0;
        f.text(text, x, y, Some(inner), &updated);
    }
    Some(card)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// A good summary fetched at `fetched`, with twelve hours from then on.
    pub fn sample(fetched: i64) -> Value {
        let hour0 = fetched - fetched.rem_euclid(3600);
        json!({
            "version": 1, "place": "Berlin", "fetched": fetched, "unit": "c",
            "temperature": "8°C", "condition": "Partly cloudy", "icon": "weather-few-clouds-symbolic",
            "updated": "Updated at 14:20", "feels_like": "Feels like 5°", "high_low": "High 9° · Low 3°",
            "today_until": fetched + 6 * 3600,
            "hours": (0..12).map(|n| json!({"at": hour0 + n * 3600, "time": format!("{:02}:00", (14 + n) % 24),
                "icon": if n % 2 == 0 { "weather-overcast-symbolic" } else { "weather-showers-symbolic" },
                "temperature": format!("{}°", 8 - n / 3)})).collect::<Vec<_>>(),
            "something_new": {"ignored": true}
        })
    }

    const T: i64 = 1_763_731_200; // 2025-11-21 13:20 UTC

    #[test]
    fn a_good_summary_is_read() {
        let summary = parse(&sample(T).to_string()).unwrap();
        assert_eq!((summary.place.as_str(), summary.temperature.as_str(), summary.icon.as_str()), ("Berlin", "8°C", "weather-few-clouds-symbolic"));
        assert_eq!(summary.hours.len(), 12);
        assert_eq!(summary.hours[1].time, "15:00");
        // The optional lines may be missing or null.
        let mut minimal = sample(T);
        for key in ["updated", "feels_like", "high_low", "today_until", "hours"] {
            minimal.as_object_mut().unwrap().remove(key);
        }
        minimal["feels_like"] = Value::Null;
        let summary = parse(&minimal.to_string()).unwrap();
        assert!(summary.hours.is_empty() && summary.high_low.is_none() && summary.updated.is_none());
    }

    #[test]
    fn every_bad_value_rejects_the_whole_summary() {
        let bad: Vec<(&str, Value)> = vec![
            ("version", json!(2)),
            ("version", json!("1")),
            ("unit", json!("k")),
            ("place", json!("")),
            ("place", json!("   ")),
            ("place", json!("x".repeat(101))),
            ("place", json!("Berlin\u{1b}[31m")),
            ("place", json!("Ber\u{202e}lin")),
            ("place", json!(7)),
            ("temperature", json!("1".repeat(13))),
            ("condition", Value::Null),
            ("icon", json!("../../../etc/passwd")),
            ("icon", json!("/usr/share/icons/x.svg")),
            ("icon", json!("weather-")),
            ("icon", json!("weather-Clear")),
            ("icon", json!("weather-clear-symbolic.svg")),
            ("icon", json!(format!("weather-{}", "a".repeat(60)))),
            ("icon", json!("battery-full-symbolic")),
            ("fetched", json!(1.5e9)),
            ("fetched", json!(-1)),
            ("fetched", json!(1e300)),
            ("fetched", json!(u64::MAX)),
            ("fetched", json!("1763731200")),
            ("today_until", json!(12)),
            ("updated", json!("x".repeat(65))),
            ("hours", json!("soon")),
            ("hours", json!((0..49).map(|n| json!({"at": T + n * 3600, "time": "1", "icon": "weather-fog", "temperature": "1°"})).collect::<Vec<_>>())),
            ("hours", json!([{"at": T, "time": "15:00", "icon": "weather-fog-symbolic"}])),
            ("hours", json!([{"at": T, "time": "15:00", "icon": "fog", "temperature": "1°"}])),
            ("hours", json!([{"at": "now", "time": "15:00", "icon": "weather-fog-symbolic", "temperature": "1°"}])),
            ("hours", json!([7])),
        ];
        for (key, value) in bad {
            let mut summary = sample(T);
            summary[key] = value.clone();
            assert!(parse(&summary.to_string()).is_err(), "{key} = {value} was accepted");
        }
        for text in ["", "null", "[]", "{}", "\"weather\"", "{\"version\": 1", "NaN", "{\"version\": 1, \"fetched\": NaN}"] {
            assert!(parse(text).is_err(), "{text:?}");
        }
        // Deeply nested JSON is refused by the parser, not a stack overflow.
        let deep = format!("{}{}", "[".repeat(100_000), "]".repeat(100_000));
        assert!(parse(&deep).is_err());
    }

    #[test]
    fn old_and_future_summaries_are_not_shown() {
        let summary = parse(&sample(T).to_string()).unwrap();
        let now = summary.shown(T + 60).unwrap();
        assert_eq!(now.updated, None, "fresh: no time is said");
        assert_eq!(now.details.as_deref(), Some("High 9° · Low 3° · Feels like 5°"));
        assert_eq!(now.hours.iter().map(|h| h.time.as_str()).collect::<Vec<_>>(), ["15:00", "16:00", "17:00", "18:00"]);
        let later = summary.shown(T + 3600 + 600).unwrap();
        assert_eq!(later.updated, Some("Updated at 14:20"), "an hour old: it says when");
        assert_eq!(later.hours[0].time, "16:00", "hours that began are not shown");
        assert!(summary.shown(T + MAX_AGE).is_some());
        assert!(summary.shown(T + MAX_AGE + 1).is_none(), "older than three hours");
        assert!(summary.shown(T - 200).is_some(), "a clock a little behind");
        assert!(summary.shown(T - 3600).is_none(), "from the future");
        // Today's high and low end with the day.
        let night = summary.shown(T + 6 * 3600 - 1).map(|s| s.details);
        assert_eq!(night, None, "too old by then anyway");
        let mut short_day = sample(T);
        short_day["today_until"] = json!(T + 600);
        let summary = parse(&short_day.to_string()).unwrap();
        assert_eq!(summary.shown(T + 601).unwrap().details.as_deref(), Some("Feels like 5°"));
        // The last hours of the list run out.
        let summary = parse(&sample(T).to_string()).unwrap();
        assert!(summary.shown(T + MAX_AGE).unwrap().hours.len() == SHOWN_HOURS);
        let mut few = sample(T);
        few["hours"] = json!([]);
        few["feels_like"] = Value::Null;
        few["high_low"] = Value::Null;
        let summary = parse(&few.to_string()).unwrap();
        let shown = summary.shown(T).unwrap();
        assert!(shown.hours.is_empty() && shown.details.is_none());
    }

    #[test]
    fn only_a_regular_file_of_the_user_is_read() {
        use std::os::unix::fs::{symlink, PermissionsExt};
        let dir = tempfile::tempdir().unwrap();
        let uid = rustix::process::geteuid().as_raw();
        let path = dir.path().join("weather.json");
        std::fs::write(&path, sample(T).to_string()).unwrap();
        assert_eq!(read(&path, uid).unwrap().place, "Berlin");
        assert_eq!(read(&path, uid.wrapping_add(1)), Err("not the user's own file"));
        // A symbolic link is not followed, even to a good summary.
        let link = dir.path().join("link.json");
        symlink(&path, &link).unwrap();
        assert!(read(&link, uid).is_err());
        // Too large, even when it is valid JSON.
        let mut large = sample(T);
        large["padding"] = json!("x".repeat(MAX_BYTES as usize));
        std::fs::write(&path, large.to_string()).unwrap();
        assert_eq!(read(&path, uid), Err("larger than 16 KB"));
        // A directory, a FIFO (opening it must not wait for a writer) and a
        // file that is not UTF-8.
        let sub = dir.path().join("dir.json");
        std::fs::create_dir(&sub).unwrap();
        assert!(read(&sub, uid).is_err());
        let fifo = dir.path().join("fifo.json");
        assert!(std::process::Command::new("mkfifo").arg(&fifo).status().unwrap().success());
        assert_eq!(read(&fifo, uid), Err("not a regular file"));
        std::fs::write(&path, b"{\"version\": 1, \"place\": \"\xff\"}").unwrap();
        assert_eq!(read(&path, uid), Err("not UTF-8 text"));
        // Unreadable: no panic, no summary.
        std::fs::write(&path, sample(T).to_string()).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o000)).unwrap();
        if rustix::process::geteuid().is_root() {
            assert!(read(&path, uid).is_ok(), "root reads it anyway");
        } else {
            assert!(read(&path, uid).is_err());
        }
        assert!(read(&dir.path().join("missing.json"), uid).is_err());
    }

    #[test]
    fn the_file_is_followed_and_read_only_when_it_changes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("weather.json");
        let mut watch = Watch::new(Some(path.clone()));
        assert!(!watch.check(), "no file, nothing to show");
        assert!(watch.summary.is_none());
        std::fs::write(&path, sample(T).to_string()).unwrap();
        assert!(watch.check());
        assert_eq!(watch.summary.as_ref().unwrap().place, "Berlin");
        assert!(!watch.check(), "unchanged: not read again");
        // Replaced (as the writer does: a new file renamed over the old one).
        let mut other = sample(T + 1800);
        other["place"] = json!("Sydney");
        let fresh = dir.path().join("fresh.tmp");
        std::fs::write(&fresh, other.to_string()).unwrap();
        std::fs::rename(&fresh, &path).unwrap();
        assert!(watch.check());
        assert_eq!(watch.summary.as_ref().unwrap().place, "Sydney");
        // Damaged, then a symbolic link in its place, then gone.
        std::fs::write(&path, "{\"version\": 1, \"place\": ").unwrap();
        assert!(watch.check() && watch.summary.is_none());
        std::fs::remove_file(&path).unwrap();
        let target = dir.path().join("target.json");
        std::fs::write(&target, sample(T).to_string()).unwrap();
        std::os::unix::fs::symlink(&target, &path).unwrap();
        assert!(!watch.check(), "still nothing to show");
        assert!(watch.summary.is_none());
        std::fs::remove_file(&path).unwrap();
        assert!(!watch.check());
        // Polled once a minute.
        std::fs::write(&path, sample(T).to_string()).unwrap();
        assert!(watch.poll(T));
        std::fs::write(&path, sample(T + 60).to_string() + " ").unwrap();
        assert!(!watch.poll(T + 30), "the same minute: not looked at");
        assert!(watch.poll(T + 60));
        // Following nothing (the desktop, the login screen).
        let mut nothing = Watch::new(None);
        assert!(!nothing.poll(T) && !nothing.check() && nothing.summary.is_none());
    }

    #[test]
    fn the_large_icon_follows_the_temperature() {
        crate::ui::set_text_scale(100);
        assert_eq!(icon_size(ramp("title-large", Rgba::WHITE).line_height()), 40.0);
        crate::ui::set_text_scale(225);
        assert_eq!(icon_size(ramp("title-large", Rgba::WHITE).line_height()), 64.0);
        crate::ui::set_text_scale(100);
    }
}
