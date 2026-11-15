//! Motion: how windows appear, leave and move between desktops.
//!
//! An animation never changes where a window *is*: window management works
//! on final geometry at once, and an animation only changes how the window
//! (or, after it is gone, a picture of it) is drawn for a few frames. With
//! `appearance.reduced_motion` nothing here runs.
//!
//! Durations and curves are the motion tokens of Boswas UI: things that
//! enter decelerate, things that leave accelerate.

use std::time::{Duration, Instant};

use boswas_ui::tokens::{cubic_bezier, tokens};
use smithay::{
    backend::renderer::element::memory::MemoryRenderBuffer,
    utils::{Logical, Point, Rectangle, Size},
};

/// The scale a window opens from and closes to.
const POP_SCALE: f64 = 0.92;
/// The size, relative to the window, of what flies to the taskbar.
const MINIMIZED_SCALE: f64 = 0.25;
/// How far the windows of a desktop slide when desktops are switched.
pub const DESKTOP_SLIDE: f64 = 96.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Curve {
    /// Fast first, then settling: things that enter.
    Decelerate,
    /// Slow first, then fast: things that leave.
    Accelerate,
}

/// A running animation: when it started, how long it takes and how it
/// eases.
#[derive(Clone, Copy, Debug)]
pub struct Timeline {
    start: Instant,
    duration: Duration,
    curve: Curve,
}

impl Timeline {
    pub fn new(milliseconds: u32, curve: Curve) -> Self {
        Self::starting_at(Instant::now(), milliseconds, curve)
    }

    pub fn starting_at(start: Instant, milliseconds: u32, curve: Curve) -> Self {
        Self {
            start,
            duration: Duration::from_millis(u64::from(milliseconds.max(1))),
            curve,
        }
    }

    /// Time that has passed, 0.0 to 1.0, without easing. Opacity follows
    /// this, so that something that leaves has faded out completely when
    /// its (accelerating) movement ends.
    pub fn linear(&self, now: Instant) -> f32 {
        (now.saturating_duration_since(self.start).as_secs_f32() / self.duration.as_secs_f32()).clamp(0.0, 1.0)
    }

    /// Eased progress, 0.0 to 1.0.
    pub fn progress(&self, now: Instant) -> f32 {
        let name = match self.curve {
            Curve::Decelerate => "decelerate",
            Curve::Accelerate => "accelerate",
        };
        // The ends are exact, so an animation starts and ends precisely on
        // the geometry it animates between.
        match self.linear(now) {
            t if t <= 0.0 => 0.0,
            t if t >= 1.0 => 1.0,
            t => cubic_bezier(tokens().curve(name), t).clamp(0.0, 1.0),
        }
    }

    pub fn finished(&self, now: Instant) -> bool {
        now.saturating_duration_since(self.start) >= self.duration
    }
}

/// Where and how opaque something is drawn at one moment.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Pose {
    pub rect: Rectangle<f64, Logical>,
    pub alpha: f32,
}

fn lerp(from: f64, to: f64, t: f32) -> f64 {
    from + (to - from) * f64::from(t)
}

/// `rect` scaled by `factor` about its centre.
fn scaled(rect: Rectangle<f64, Logical>, factor: f64) -> Rectangle<f64, Logical> {
    let size = Size::from((rect.size.w * factor, rect.size.h * factor));
    Rectangle::new(
        Point::from((
            rect.loc.x + (rect.size.w - size.w) / 2.0,
            rect.loc.y + (rect.size.h - size.h) / 2.0,
        )),
        size,
    )
}

/// The small rectangle at the taskbar a window minimizes to.
fn at_taskbar(rect: Rectangle<f64, Logical>, taskbar: Point<f64, Logical>) -> Rectangle<f64, Logical> {
    let size = Size::from((rect.size.w * MINIMIZED_SCALE, rect.size.h * MINIMIZED_SCALE));
    Rectangle::new(Point::from((taskbar.x - size.w / 2.0, taskbar.y - size.h)), size)
}

fn between(from: Rectangle<f64, Logical>, to: Rectangle<f64, Logical>, t: f32) -> Rectangle<f64, Logical> {
    Rectangle::new(
        Point::from((lerp(from.loc.x, to.loc.x, t), lerp(from.loc.y, to.loc.y, t))),
        Size::from((lerp(from.size.w, to.size.w, t), lerp(from.size.h, to.size.h, t))),
    )
}

/// How a live window is being shown.
#[derive(Clone, Copy, Debug)]
pub enum WindowAnimation {
    /// A new window grows a little and fades in.
    Open(Timeline),
    /// A window comes back from the taskbar.
    Restore { timeline: Timeline, taskbar: Point<f64, Logical> },
    /// A window of the desktop that was switched to slides in from
    /// `offset` logical pixels to the side.
    SlideIn { timeline: Timeline, offset: f64 },
}

impl WindowAnimation {
    pub fn open() -> Self {
        Self::Open(Timeline::new(tokens().motion_fast_ms, Curve::Decelerate))
    }

    pub fn restore(taskbar: Point<f64, Logical>) -> Self {
        Self::Restore {
            timeline: Timeline::new(tokens().motion_normal_ms, Curve::Decelerate),
            taskbar,
        }
    }

    pub fn slide_in(offset: f64) -> Self {
        Self::SlideIn {
            timeline: Timeline::new(tokens().motion_slow_ms, Curve::Decelerate),
            offset,
        }
    }

    /// How to draw a window whose frame is at `rect`; `None` once the
    /// animation is over.
    pub fn pose(&self, rect: Rectangle<f64, Logical>, now: Instant) -> Option<Pose> {
        match *self {
            Self::Open(timeline) => (!timeline.finished(now)).then(|| {
                let t = timeline.progress(now);
                Pose {
                    rect: scaled(rect, lerp(POP_SCALE, 1.0, t)),
                    alpha: t,
                }
            }),
            Self::Restore { timeline, taskbar } => (!timeline.finished(now)).then(|| {
                let t = timeline.progress(now);
                Pose {
                    rect: between(at_taskbar(rect, taskbar), rect, t),
                    alpha: t,
                }
            }),
            Self::SlideIn { timeline, offset } => (!timeline.finished(now)).then(|| {
                let t = timeline.progress(now);
                let mut moved = rect;
                moved.loc.x += lerp(offset, 0.0, t);
                Pose { rect: moved, alpha: t }
            }),
        }
    }
}

/// What the picture of something that is gone does.
#[derive(Clone, Copy, Debug)]
pub enum GhostKind {
    /// A closed window shrinks a little and fades out.
    Close,
    /// A minimized window flies to the taskbar.
    Minimize { taskbar: Point<f64, Logical> },
    /// The windows of the desktop that was left slide `offset` logical
    /// pixels to the side.
    SlideOut { offset: f64 },
}

/// A picture of a window (or of a whole desktop's windows) that is no longer
/// shown, drawn while its exit animation runs.
pub struct Ghost {
    pub buffer: MemoryRenderBuffer,
    /// The part of the desktop the picture covers, in global coordinates.
    pub rect: Rectangle<f64, Logical>,
    pub kind: GhostKind,
    pub timeline: Timeline,
}

impl Ghost {
    pub fn new(buffer: MemoryRenderBuffer, rect: Rectangle<f64, Logical>, kind: GhostKind) -> Self {
        let t = tokens();
        let timeline = match kind {
            GhostKind::Close => Timeline::new(t.motion_fast_ms, Curve::Accelerate),
            GhostKind::Minimize { .. } => Timeline::new(t.motion_normal_ms, Curve::Accelerate),
            GhostKind::SlideOut { .. } => Timeline::new(t.motion_normal_ms, Curve::Accelerate),
        };
        Self {
            buffer,
            rect,
            kind,
            timeline,
        }
    }

    /// How to draw the picture; `None` once the animation is over.
    pub fn pose(&self, now: Instant) -> Option<Pose> {
        if self.timeline.finished(now) {
            return None;
        }
        let t = self.timeline.progress(now);
        let alpha = 1.0 - self.timeline.linear(now);
        Some(match self.kind {
            GhostKind::Close => Pose {
                rect: scaled(self.rect, lerp(1.0, POP_SCALE, t)),
                alpha,
            },
            GhostKind::Minimize { taskbar } => Pose {
                rect: between(self.rect, at_taskbar(self.rect, taskbar), t),
                alpha,
            },
            GhostKind::SlideOut { offset } => {
                let mut moved = self.rect;
                moved.loc.x += lerp(0.0, offset, t);
                Pose { rect: moved, alpha }
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rect(x: f64, y: f64, w: f64, h: f64) -> Rectangle<f64, Logical> {
        Rectangle::new(Point::from((x, y)), Size::from((w, h)))
    }

    #[test]
    fn a_timeline_runs_from_zero_to_one_and_ends() {
        let start = Instant::now();
        let timeline = Timeline::starting_at(start, 200, Curve::Decelerate);
        assert_eq!(timeline.progress(start), 0.0);
        assert!(!timeline.finished(start + Duration::from_millis(199)));
        assert!(timeline.finished(start + Duration::from_millis(200)));
        assert!((timeline.progress(start + Duration::from_millis(500)) - 1.0).abs() < 0.01);
        let early = timeline.progress(start + Duration::from_millis(60));
        assert!(early > 0.5, "decelerate covers most of the way early: {early}");
        let leaving = Timeline::starting_at(start, 200, Curve::Accelerate);
        assert!(leaving.progress(start + Duration::from_millis(60)) < 0.3);
        // A time before the start is the start.
        assert_eq!(Timeline::starting_at(start + Duration::from_secs(1), 200, Curve::Decelerate).progress(start), 0.0);
    }

    #[test]
    fn opening_grows_into_place_about_the_centre() {
        let start = Instant::now();
        let animation = WindowAnimation::Open(Timeline::starting_at(start, 150, Curve::Decelerate));
        let frame = rect(100.0, 100.0, 800.0, 600.0);
        let first = animation.pose(frame, start).unwrap();
        assert_eq!(first.alpha, 0.0);
        assert!((first.rect.size.w - 800.0 * POP_SCALE).abs() < 1e-6);
        let centre = |r: Rectangle<f64, Logical>| (r.loc.x + r.size.w / 2.0, r.loc.y + r.size.h / 2.0);
        assert_eq!(centre(first.rect), centre(frame));
        let middle = animation.pose(frame, start + Duration::from_millis(75)).unwrap();
        assert!(middle.alpha > first.alpha && middle.rect.size.w > first.rect.size.w);
        assert!(animation.pose(frame, start + Duration::from_millis(150)).is_none(), "then the window is simply there");
    }

    #[test]
    fn minimizing_ends_small_and_invisible_at_the_taskbar() {
        let start = Instant::now();
        let frame = rect(200.0, 100.0, 800.0, 600.0);
        let taskbar = Point::from((960.0, 1032.0));
        let buffer = MemoryRenderBuffer::default();
        let mut ghost = Ghost::new(buffer, frame, GhostKind::Minimize { taskbar });
        ghost.timeline = Timeline::starting_at(start, 200, Curve::Accelerate);
        assert_eq!(ghost.pose(start).unwrap(), Pose { rect: frame, alpha: 1.0 });
        let late = ghost.pose(start + Duration::from_millis(199)).unwrap();
        assert!(late.alpha < 0.05, "it has faded out when the movement ends");
        assert!(late.rect.size.w < 800.0 * 0.5 && late.rect.size.w >= 200.0);
        let bottom = late.rect.loc.y + late.rect.size.h;
        assert!(bottom > 900.0 && bottom <= 1032.5, "its bottom edge heads for the taskbar: {bottom}");
        assert!(ghost.pose(start + Duration::from_millis(200)).is_none());
        // Coming back is the same way in reverse.
        let back = WindowAnimation::Restore {
            timeline: Timeline::starting_at(start, 200, Curve::Decelerate),
            taskbar,
        };
        let first = back.pose(frame, start).unwrap();
        assert_eq!(first.alpha, 0.0);
        assert_eq!(first.rect, at_taskbar(frame, taskbar));
    }

    #[test]
    fn desktops_slide_in_opposite_directions() {
        let start = Instant::now();
        let frame = rect(0.0, 0.0, 400.0, 300.0);
        let incoming = WindowAnimation::SlideIn {
            timeline: Timeline::starting_at(start, 250, Curve::Decelerate),
            offset: DESKTOP_SLIDE,
        };
        assert_eq!(incoming.pose(frame, start).unwrap().rect.loc.x, DESKTOP_SLIDE);
        let mut outgoing = Ghost::new(MemoryRenderBuffer::default(), frame, GhostKind::SlideOut { offset: -DESKTOP_SLIDE });
        outgoing.timeline = Timeline::starting_at(start, 200, Curve::Accelerate);
        let late = outgoing.pose(start + Duration::from_millis(190)).unwrap();
        assert!(late.rect.loc.x < -DESKTOP_SLIDE * 0.5 && late.alpha < 0.1);
        assert_eq!(late.rect.size, frame.size, "a slide does not scale");
    }
}
