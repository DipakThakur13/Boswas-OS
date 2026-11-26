//! How the outputs are drawn, for finding out why the desktop is slow.
//!
//! Every frame an output renders is counted: how long it took and how much
//! of the screen was drawn again. Once a second the numbers of each output
//! that rendered go to the log at debug level, target
//! `boswas_compositor::stats`:
//!
//! ```text
//! BOSWAS_LOG=info,boswas_compositor::stats=debug
//! ```
//!
//! Nothing is logged for a second in which nothing was drawn, and the cost
//! is a few arithmetic operations per frame.
//!
//! At trace level every frame is also logged on a line of its own (its
//! cost in microseconds, the time since the frame of that output before
//! it, the share of the output drawn), so that occasional long frames can
//! be found and counted, not only averages:
//!
//! ```text
//! BOSWAS_LOG=info,boswas_compositor::stats=trace
//! ```
use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

use smithay::utils::{Physical, Rectangle, Size};
use tracing::{debug, enabled, trace, Level};

/// The figures of one output for the current second.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct Second {
    pub frames: u32,
    pub busy: Duration,
    pub slowest: Duration,
    /// Frames that drew the whole output.
    pub full: u32,
    /// The share of the output drawn again, summed over the frames.
    pub drawn: f64,
}

impl Second {
    /// One line for the log.
    pub fn line(&self, output: &str) -> String {
        let frames = self.frames.max(1);
        format!(
            "{output}: {} frames, {:.1} ms each on average (slowest {:.1} ms), {:.0}% of the screen drawn per frame, {} full redraws",
            self.frames,
            self.busy.as_secs_f64() * 1000.0 / f64::from(frames),
            self.slowest.as_secs_f64() * 1000.0,
            self.drawn * 100.0 / f64::from(frames),
            self.full,
        )
    }
}

#[derive(Debug)]
pub struct FrameStats {
    enabled: bool,
    /// Every frame is logged (trace level).
    each: bool,
    since: Instant,
    outputs: HashMap<String, Second>,
    /// When each output last finished a frame.
    last: HashMap<String, Instant>,
}

impl Default for FrameStats {
    fn default() -> Self {
        Self::new()
    }
}

impl FrameStats {
    pub fn new() -> Self {
        Self {
            enabled: enabled!(target: "boswas_compositor::stats", Level::DEBUG),
            each: enabled!(target: "boswas_compositor::stats", Level::TRACE),
            since: Instant::now(),
            outputs: HashMap::new(),
            last: HashMap::new(),
        }
    }

    /// Whether frames are counted at all (the log level asks for it).
    pub fn enabled(&self) -> bool {
        self.enabled
    }

    /// A frame of `output` (of `size` pixels) took `busy` and drew
    /// `damage` (`None`: all of it).
    pub fn frame(&mut self, output: &str, size: Size<i32, Physical>, busy: Duration, damage: Option<&[Rectangle<i32, Physical>]>) {
        if !self.enabled {
            return;
        }
        let second = self.outputs.entry(output.to_owned()).or_default();
        let share = record(second, size, busy, damage);
        let now = Instant::now();
        if self.each {
            let previous = self.last.insert(output.to_owned(), now);
            let interval = previous.map_or(0, |at| now.saturating_duration_since(at).as_micros());
            trace!(
                target: "boswas_compositor::stats",
                "frame {output} busy_us={} interval_us={interval} drawn={share:.4}",
                busy.as_micros()
            );
        }
        self.flush(now);
    }

    /// Logs and starts a new second when one has passed.
    fn flush(&mut self, now: Instant) {
        if now.saturating_duration_since(self.since) < Duration::from_secs(1) {
            return;
        }
        let mut names: Vec<&String> = self.outputs.keys().collect();
        names.sort();
        for name in names {
            debug!(target: "boswas_compositor::stats", "{}", self.outputs[name].line(name));
        }
        self.outputs.clear();
        self.since = now;
    }
}

/// Adds a frame to its second; returns the share of the output it drew.
fn record(second: &mut Second, size: Size<i32, Physical>, busy: Duration, damage: Option<&[Rectangle<i32, Physical>]>) -> f64 {
    let area = f64::from(size.w.max(1)) * f64::from(size.h.max(1));
    let share = match damage {
        None => 1.0,
        Some(rects) => {
            let drawn: f64 = rects.iter().map(|r| f64::from(r.size.w.max(0)) * f64::from(r.size.h.max(0))).sum();
            (drawn / area).min(1.0)
        }
    };
    second.frames += 1;
    second.busy += busy;
    second.slowest = second.slowest.max(busy);
    second.drawn += share;
    if share >= 0.999 {
        second.full += 1;
    }
    share
}

#[cfg(test)]
mod tests {
    use super::*;
    use smithay::utils::Point;

    #[test]
    fn a_second_adds_up_its_frames() {
        let size = Size::from((100, 100));
        let mut second = Second::default();
        record(&mut second, size, Duration::from_millis(2), None);
        let corner = [Rectangle::new(Point::from((0, 0)), Size::from((50, 50)))];
        record(&mut second, size, Duration::from_millis(4), Some(&corner));
        record(&mut second, size, Duration::from_millis(3), Some(&[]));
        assert_eq!(second.frames, 3);
        assert_eq!(second.full, 1);
        assert_eq!(second.slowest, Duration::from_millis(4));
        assert!((second.drawn - 1.25).abs() < 1e-9, "{}", second.drawn);
        let line = second.line("eDP-1");
        assert!(line.starts_with("eDP-1: 3 frames, 3.0 ms each"), "{line}");
        assert!(line.contains("42% of the screen") && line.ends_with("1 full redraws"), "{line}");
    }

    #[test]
    fn overlapping_damage_never_counts_more_than_the_screen() {
        let mut second = Second::default();
        let all = Rectangle::new(Point::from((0, 0)), Size::from((10, 10)));
        record(&mut second, Size::from((10, 10)), Duration::ZERO, Some(&[all, all]));
        assert!((second.drawn - 1.0).abs() < 1e-9);
        assert_eq!(second.full, 1);
    }
}
