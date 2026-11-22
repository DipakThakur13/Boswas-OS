//! Motion: the timing curves of Boswas UI and the choreography of the boot
//! screen.
//!
//! Everything that moves is a pure function of the clock, so a busy system
//! makes the picture update less often but never faster, slower or uneven,
//! and the review frames and the tests can ask for any moment.
//!
//! * **Intro** (boot, once, [`INTRO`] seconds): the mark fades in and grows
//!   slightly; its stem rises from its foot, its bowl sweeps round from one
//!   cut to the other, its dot pops in with a small overshoot and a warm
//!   glow that settles; then the ring fades in under it.
//! * **Fade-in** (shutdown and restart, [`FADE_IN`]): everything appears.
//! * **Breathing**: while the ring turns, the glow of the dot rises and
//!   falls once every [`BREATH`] seconds, gently.
//! * **Glide** ([`GLIDE`]): when a passphrase is asked for, the mark glides
//!   up and shrinks and the prompt fades and slides in; when it is answered,
//!   the reverse.
//! * **Outro** ([`OUTRO`]): on `quit`, everything fades to the background;
//!   on `quit --retain`, only the ring and the text, leaving the mark.
//!
//! With `--static` nothing moves: the assembled mark and a still ring.

use crate::assets;

/// A cubic Bézier timing curve, `cubic-bezier(x1, y1, x2, y2)` in CSS terms:
/// it starts at (0, 0) and ends at (1, 1).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Curve(pub [f32; 4]);

impl Curve {
    /// The eased value at `t` (clamped to 0..=1).
    pub fn at(self, t: f32) -> f32 {
        let [x1, y1, x2, y2] = self.0;
        let t = if t.is_nan() { 0.0 } else { t.clamp(0.0, 1.0) };
        if t == 0.0 || t == 1.0 {
            return t;
        }
        let bezier = |a: f32, b: f32, s: f32| {
            let u = 1.0 - s;
            3.0 * u * u * s * a + 3.0 * u * s * s * b + s * s * s
        };
        let slope = |a: f32, b: f32, s: f32| {
            let u = 1.0 - s;
            3.0 * u * u * a + 6.0 * u * s * (b - a) + 3.0 * s * s * (1.0 - b)
        };
        // Find s with x(s) = t: Newton's method, and bisection where the
        // curve is too flat for it. x(s) rises monotonically (x1, x2 in 0..1).
        let mut s = t;
        for _ in 0..8 {
            let error = bezier(x1, x2, s) - t;
            if error.abs() < 1e-6 {
                return bezier(y1, y2, s);
            }
            let d = slope(x1, x2, s);
            if d.abs() < 1e-6 {
                break;
            }
            s = (s - error / d).clamp(0.0, 1.0);
        }
        let (mut low, mut high) = (0.0f32, 1.0f32);
        for _ in 0..40 {
            s = (low + high) / 2.0;
            if bezier(x1, x2, s) < t {
                low = s;
            } else {
                high = s;
            }
        }
        bezier(y1, y2, s)
    }
}

/// Seconds the intro takes, from the first frame to the settled glow.
pub const INTRO: f32 = 1.6;
/// The stages of the intro: (start, end) in seconds.
pub const MARK_FADE: (f32, f32) = (0.0, 0.5);
pub const MARK_GROW: (f32, f32) = (0.0, 1.1);
pub const STEM_RISE: (f32, f32) = (0.05, 0.6);
pub const BOWL_SWEEP: (f32, f32) = (0.3, 1.0);
pub const DOT_POP: (f32, f32) = (0.85, 1.25);
pub const GLOW_SETTLE: (f32, f32) = (1.25, INTRO);
pub const RING_FADE: (f32, f32) = (1.1, 1.5);
/// The size the mark grows from during the intro (and the fade-in).
pub const MARK_FROM: f32 = 0.94;
/// How far the dot overshoots when it pops in (the "back" ease).
const OVERSHOOT: f32 = 1.70158;

/// The fade-in of the shutdown and restart screens, in seconds.
pub const FADE_IN: f32 = assets::MOTION_SLOW;
/// The glide between the boot screen and the unlock screen, in seconds.
pub const GLIDE: f32 = 0.32;
/// The fade at the end, in seconds.
pub const OUTRO: f32 = assets::MOTION_NORMAL;
/// Seconds for one breath of the glow.
pub const BREATH: f32 = 4.0;
/// Opacity of the glow round the dot: at rest, at the height of the pop,
/// and how much a breath adds.
pub const GLOW_REST: f32 = 0.2;
pub const GLOW_PEAK: f32 = 0.6;
pub const GLOW_BREATH: f32 = 0.1;
/// How long a reported progress takes to reach its new value, in seconds.
pub const PROGRESS_GLIDE: f32 = assets::MOTION_SLOW;

/// Where `t` is between `from` and `to`, clamped to 0..=1.
pub fn span(t: f32, (from, to): (f32, f32)) -> f32 {
    ((t - from) / (to - from)).clamp(0.0, 1.0)
}

/// An ease out that overshoots a little and settles (0 at 0, 1 at 1, about
/// 1.1 at its highest).
pub fn ease_out_back(s: f32) -> f32 {
    let s = s.clamp(0.0, 1.0) - 1.0;
    1.0 + (OVERSHOOT + 1.0) * s * s * s + OVERSHOOT * s * s
}

/// From `a` to `b`; exactly `b` at the end.
fn lerp(a: f32, b: f32, t: f32) -> f32 {
    if t >= 1.0 {
        b
    } else {
        a + (b - a) * t
    }
}

/// How far the mark is assembled: 1, 1, 1 when complete.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Assembly {
    /// The stem's rise from its foot, 0..1.
    pub stem: f32,
    /// The sweep of the bowl round its centre, 0..1.
    pub bowl: f32,
    /// The size of the dot (0 before it pops in; above 1 in the overshoot).
    pub dot: f32,
}

impl Assembly {
    pub const DONE: Assembly = Assembly { stem: 1.0, bowl: 1.0, dot: 1.0 };
}

/// Everything that moves, at one moment.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Motion {
    pub assembly: Assembly,
    /// Opacity of the mark and its glow.
    pub mark_alpha: f32,
    /// Size of the mark, 1 at rest.
    pub mark_scale: f32,
    /// Opacity of the glow round the dot.
    pub glow: f32,
    /// Opacity of the ring.
    pub ring_alpha: f32,
    /// Opacity of the text: status line, messages, the prompt.
    pub text_alpha: f32,
    /// From the boot screen (0) to the unlock screen (1), linear in time;
    /// the scene applies the curves.
    pub reveal: f32,
    /// The ring's share when it shows a progress (0..1), as it glides to the
    /// reported value. `None`: the scene takes the reported value as it is.
    pub ring_progress: Option<f32>,
}

impl Motion {
    /// Nothing moving: the assembled mark, everything shown, the boot screen.
    pub const REST: Motion = Motion {
        assembly: Assembly::DONE,
        mark_alpha: 1.0,
        mark_scale: 1.0,
        glow: GLOW_REST,
        ring_alpha: 1.0,
        text_alpha: 1.0,
        reveal: 0.0,
        ring_progress: None,
    };
}

/// The intro at `t` seconds.
pub fn intro(t: f32) -> Motion {
    let decelerate = assets::DECELERATE;
    let glow = if t < DOT_POP.1 {
        GLOW_PEAK * decelerate.at(span(t, DOT_POP))
    } else {
        lerp(GLOW_PEAK, GLOW_REST, assets::STANDARD.at(span(t, GLOW_SETTLE)))
    };
    Motion {
        assembly: Assembly {
            stem: decelerate.at(span(t, STEM_RISE)),
            bowl: assets::STANDARD.at(span(t, BOWL_SWEEP)),
            dot: if t <= DOT_POP.0 { 0.0 } else { ease_out_back(span(t, DOT_POP)) },
        },
        mark_alpha: decelerate.at(span(t, MARK_FADE)),
        mark_scale: lerp(MARK_FROM, 1.0, decelerate.at(span(t, MARK_GROW))),
        glow,
        ring_alpha: decelerate.at(span(t, RING_FADE)),
        ..Motion::REST
    }
}

/// The fade-in of the shutdown and restart screens at `t` seconds.
pub fn fade_in(t: f32) -> Motion {
    let a = assets::DECELERATE.at(t / FADE_IN);
    Motion { mark_alpha: a, mark_scale: lerp(MARK_FROM + 0.03, 1.0, a), ring_alpha: a, text_alpha: a, ..Motion::REST }
}

/// The glow `t` seconds into breathing: it starts at rest, without a jolt.
pub fn breath(t: f32) -> f32 {
    GLOW_REST + GLOW_BREATH * (1.0 - (t / BREATH * std::f32::consts::TAU).cos()) / 2.0
}

/// How the screen opens.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Opening {
    /// The boot screen: the mark assembles itself.
    Intro,
    /// Shutdown and restart: a short fade-in.
    FadeIn,
}

/// A value that glides between 0 and 1 at a fixed speed.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Glide {
    from: f32,
    since: f64,
    up: bool,
}

impl Glide {
    fn at(&self, now: f64, secs: f32) -> f32 {
        let moved = ((now - self.since) as f32 / secs).max(0.0);
        if self.up {
            (self.from + moved).min(1.0)
        } else {
            (self.from - moved).max(0.0)
        }
    }
}

/// The ring: seconds per cycle.
pub const RING_SECS: f32 = 2.0;

/// The state of the choreography over time. Times are seconds on any clock
/// that does not go backwards (the daemon uses the time since it started).
#[derive(Clone, Debug, PartialEq)]
pub struct Choreography {
    animate: bool,
    opening: Opening,
    opened: f64,
    reveal: Glide,
    progress: Option<(f32, f32, f64)>,
    outro: Option<(f64, bool)>,
}

impl Choreography {
    pub fn new(opening: Opening, animate: bool, now: f64) -> Self {
        Self {
            animate,
            opening,
            opened: now,
            reveal: Glide { from: 0.0, since: now, up: false },
            progress: None,
            outro: None,
        }
    }

    /// Seconds since the screen opened.
    fn age(&self, now: f64) -> f32 {
        (now - self.opened).max(0.0) as f32
    }

    /// How long the opening takes.
    fn opening_secs(&self) -> f32 {
        match self.opening {
            Opening::Intro => INTRO,
            Opening::FadeIn => FADE_IN,
        }
    }

    /// A passphrase prompt opened (`true`) or closed.
    pub fn prompt(&mut self, open: bool, now: f64) {
        if self.reveal.up != open {
            self.reveal = Glide { from: self.reveal.at(now, GLIDE), since: now, up: open };
        }
    }

    /// The reported progress, in percent.
    pub fn progress(&mut self, percent: u8, now: f64) {
        let to = f32::from(percent.min(100)) / 100.0;
        let from = self.progress_at(now).unwrap_or(0.0);
        self.progress = Some((from, to, now));
    }

    fn progress_at(&self, now: f64) -> Option<f32> {
        let (from, to, since) = self.progress?;
        if !self.animate {
            return Some(to);
        }
        let t = (now - since) as f32 / PROGRESS_GLIDE;
        Some(lerp(from, to, assets::DECELERATE.at(t)))
    }

    /// The screen is about to end: everything fades (`retain`: everything
    /// but the mark).
    pub fn leave(&mut self, retain: bool, now: f64) {
        self.outro.get_or_insert((now, retain));
    }

    /// Whether the farewell of [`Choreography::leave`] is complete.
    pub fn left(&self, now: f64) -> bool {
        self.outro.is_none_or(|(since, _)| !self.animate || (now - since) as f32 >= OUTRO)
    }

    /// Complete the opening and any glide by `now`, as if the screen had
    /// been up for a while, with the glow at the start of a breath (tests).
    pub fn settle(&mut self, now: f64) {
        let opening = self.opening_secs() as f64 + 100.0 * BREATH as f64;
        self.opened = now - opening;
        self.reveal.since = now - 2.0 * GLIDE as f64;
        if let Some(progress) = self.progress.as_mut() {
            progress.2 = now - 2.0 * PROGRESS_GLIDE as f64;
        }
    }

    /// Where the ring is in its cycle (0..1).
    pub fn phase(&self, now: f64) -> f32 {
        if !self.animate {
            return crate::scene::REST_PHASE;
        }
        // The ring's cycle starts as it begins to fade in: with its
        // shortest arc, which then grows.
        let start = if self.opening == Opening::Intro { RING_FADE.0 } else { 0.0 };
        ((self.age(now) - start) / RING_SECS).rem_euclid(1.0)
    }

    /// Whether anything changes with time: the loop then draws a frame per
    /// refresh. While the unlock screen is shown and settled, nothing does
    /// (the caret and the shake are the daemon's).
    pub fn moving(&self, now: f64) -> bool {
        if !self.animate {
            return false;
        }
        let reveal = self.reveal.at(now, GLIDE);
        let gliding = reveal != if self.reveal.up { 1.0 } else { 0.0 };
        let progressing = self.progress.is_some_and(|(_, _, since)| ((now - since) as f32) < PROGRESS_GLIDE);
        self.outro.is_some_and(|_| !self.left(now))
            || self.age(now) < self.opening_secs()
            || gliding
            || progressing
            || reveal < 1.0
    }

    /// Everything that moves, at `now`.
    pub fn motion(&self, now: f64) -> Motion {
        if !self.animate {
            return Motion {
                reveal: if self.reveal.up { 1.0 } else { 0.0 },
                ring_progress: self.progress.map(|p| p.1),
                ..Motion::REST
            };
        }
        let mut age = self.age(now);
        let reveal = self.reveal.at(now, GLIDE);
        let out = self.outro.map(|(since, retain)| (span((now - since) as f32, (0.0, OUTRO)), since, retain));
        // Leaving with the last frame kept: the mark completes its intro
        // during the farewell, so that the frame left behind is whole.
        if let Some((s, since, true)) = out {
            let then = (since - self.opened).max(0.0) as f32;
            age = age.max(lerp(then, INTRO.max(then), s));
        }
        let mut m = match self.opening {
            Opening::Intro if age < INTRO => intro(age),
            Opening::FadeIn if age < FADE_IN => fade_in(age),
            Opening::Intro => Motion { glow: breath(age - INTRO), ..Motion::REST },
            Opening::FadeIn => Motion { glow: breath(age - FADE_IN), ..Motion::REST },
        };
        // The glow breathes only on the boot screen, and is calm on the
        // unlock screen.
        m.glow = lerp(m.glow, m.glow.min(GLOW_REST), reveal);
        m.reveal = reveal;
        m.ring_progress = self.progress_at(now);
        if let Some((s, _, retain)) = out {
            let fade = 1.0 - assets::ACCELERATE.at(s);
            m.ring_alpha *= fade;
            m.text_alpha *= fade;
            if retain {
                m.glow = lerp(m.glow, GLOW_REST, s);
            } else {
                m.mark_alpha *= fade;
            }
        }
        m
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_token_curves_ease() {
        for curve in [assets::DECELERATE, assets::ACCELERATE, assets::STANDARD] {
            assert_eq!(curve.at(0.0), 0.0);
            assert_eq!(curve.at(1.0), 1.0);
            assert_eq!(curve.at(-3.0), 0.0);
            assert_eq!(curve.at(7.0), 1.0);
            assert_eq!(curve.at(f32::NAN), 0.0);
            // Monotonic.
            let mut last = 0.0;
            for i in 1..=1000 {
                let v = curve.at(i as f32 / 1000.0);
                assert!(v >= last - 1e-5, "{curve:?} at {i}");
                last = v;
            }
        }
        // Decelerate is fast at first and slow at the end; accelerate the
        // reverse; standard slow at both ends.
        assert!(assets::DECELERATE.at(0.2) > 0.6);
        assert!(assets::ACCELERATE.at(0.5) < 0.3);
        assert!(assets::STANDARD.at(0.1) < 0.05 && assets::STANDARD.at(0.9) > 0.95);
        assert!((assets::STANDARD.at(0.5) - 0.5).abs() < 0.05);
        // A linear curve is the identity.
        let linear = Curve([0.25, 0.25, 0.75, 0.75]);
        for i in 0..=10 {
            assert!((linear.at(i as f32 / 10.0) - i as f32 / 10.0).abs() < 1e-4);
        }
    }

    /// The fastest change of `f` over `range`, per second, sampled every millisecond.
    fn fastest(f: impl Fn(f32) -> f32, range: (f32, f32)) -> f32 {
        let mut top = 0f32;
        let mut t = range.0;
        while t < range.1 {
            top = top.max((f(t + 0.001) - f(t)).abs() / 0.001);
            t += 0.001;
        }
        top
    }

    #[test]
    fn the_intro_assembles_the_mark_and_ends_at_rest() {
        let start = intro(0.0);
        assert_eq!(start.mark_alpha, 0.0, "nothing of the mark at first");
        assert_eq!((start.assembly.stem, start.assembly.bowl, start.assembly.dot), (0.0, 0.0, 0.0));
        assert_eq!(start.ring_alpha, 0.0);
        assert!((start.mark_scale - MARK_FROM).abs() < 1e-6);
        // In order: the stem rises, then the bowl sweeps, then the dot pops.
        let mid = intro(0.5);
        assert!(mid.assembly.stem > 0.9 && mid.assembly.bowl > 0.0 && mid.assembly.bowl < 0.6);
        assert_eq!(mid.assembly.dot, 0.0);
        let pop = intro(1.05);
        assert!(pop.assembly.bowl > 0.99 && pop.assembly.dot > 1.0, "the dot overshoots: {pop:?}");
        assert!(pop.glow > GLOW_REST * 2.0, "with a warm glow");
        // Settled: the motion at the end of the intro is the motion at rest.
        let end = intro(INTRO);
        assert_eq!(end, Motion::REST);
        // No stage jumps: every change is spread over many frames (a jump
        // would be a thousand per second here).
        for f in [
            &(|t: f32| intro(t).mark_alpha) as &dyn Fn(f32) -> f32,
            &|t| intro(t).mark_scale,
            &|t| intro(t).assembly.stem,
            &|t| intro(t).assembly.bowl,
            &|t| intro(t).assembly.dot,
            &|t| intro(t).glow,
            &|t| intro(t).ring_alpha,
        ] {
            assert!(fastest(f, (0.0, INTRO)) < 30.0);
        }
        // The overshoot peaks a little above the full size.
        let peak = (0..400).map(|i| ease_out_back(i as f32 / 400.0)).fold(0.0, f32::max);
        assert!(peak > 1.05 && peak < 1.15, "{peak}");
        assert_eq!(ease_out_back(0.0), 0.0);
        assert!((ease_out_back(1.0) - 1.0).abs() < 1e-6);
    }

    #[test]
    fn the_fade_in_and_breathing() {
        assert_eq!(fade_in(0.0).mark_alpha, 0.0);
        assert_eq!(fade_in(FADE_IN), Motion::REST);
        assert_eq!(breath(0.0), GLOW_REST);
        assert!((breath(BREATH / 2.0) - (GLOW_REST + GLOW_BREATH)).abs() < 1e-6);
        assert!((breath(BREATH) - GLOW_REST).abs() < 1e-5);
        // Slow: a breath changes the glow by a few hundredths a second at most.
        assert!(fastest(breath, (0.0, BREATH)) < GLOW_BREATH);
    }

    #[test]
    fn the_choreography_of_a_boot() {
        let mut c = Choreography::new(Opening::Intro, true, 10.0);
        assert!(c.moving(10.0));
        assert_eq!(c.motion(10.0).mark_alpha, 0.0);
        assert_eq!(c.motion(10.0 + 0.7), intro(0.7));
        // After the intro the glow breathes and the ring turns.
        let later = c.motion(10.0 + INTRO as f64 + 2.0);
        assert!(later.glow > GLOW_REST + GLOW_BREATH * 0.9 && later.ring_alpha == 1.0);
        assert!(c.moving(30.0));
        // The ring's cycle begins as it fades in, with its shortest arc.
        assert!(c.phase(10.0 + RING_FADE.0 as f64).abs() < 1e-4);
        assert!((c.phase(10.0 + RING_FADE.0 as f64 + 0.5) - 0.25).abs() < 1e-4);

        // A prompt: the glide takes GLIDE seconds, there and back.
        c.prompt(true, 20.0);
        assert!((c.motion(20.0 + GLIDE as f64 / 2.0).reveal - 0.5).abs() < 1e-3);
        assert_eq!(c.motion(20.0 + GLIDE as f64).reveal, 1.0);
        assert!(!c.moving(21.0), "the settled unlock screen does not move");
        assert_eq!(c.motion(21.0).glow, GLOW_REST, "nor does the glow");
        c.prompt(false, 30.0);
        c.prompt(true, 30.0 + GLIDE as f64 / 4.0);
        assert!((c.motion(30.0 + GLIDE as f64 / 4.0).reveal - 0.75).abs() < 1e-3, "turning back mid-way");

        // A progress glides to its value.
        c.progress(50, 40.0);
        assert_eq!(c.motion(40.0).ring_progress, Some(0.0));
        assert!(c.moving(40.1));
        assert_eq!(c.motion(41.0).ring_progress, Some(0.5));
        c.progress(80, 41.0);
        let between = c.motion(41.0 + PROGRESS_GLIDE as f64 / 2.0).ring_progress.unwrap();
        assert!(between > 0.6 && between < 0.8);

        // Settled: nothing left of the opening or the glide, the glow at rest.
        let mut c = Choreography::new(Opening::Intro, true, 0.0);
        c.prompt(true, 0.1);
        c.settle(0.2);
        let settled = c.motion(0.2);
        assert_eq!((settled.reveal, settled.assembly, settled.glow), (1.0, Assembly::DONE, GLOW_REST));
    }

    #[test]
    fn leaving_fades_everything_or_all_but_the_mark() {
        let mut c = Choreography::new(Opening::Intro, true, 0.0);
        c.leave(false, 5.0);
        assert!(!c.left(5.0) && c.left(5.0 + OUTRO as f64));
        let gone = c.motion(5.0 + OUTRO as f64);
        assert_eq!((gone.mark_alpha, gone.ring_alpha, gone.text_alpha), (0.0, 0.0, 0.0));
        let half = c.motion(5.0 + OUTRO as f64 / 2.0);
        assert!(half.mark_alpha > 0.0 && half.mark_alpha < 1.0);

        // Retained, and early: the mark completes, the ring goes, the glow is calm.
        let mut c = Choreography::new(Opening::Intro, true, 0.0);
        c.leave(true, 0.4);
        let last = c.motion(0.4 + OUTRO as f64);
        assert_eq!(last.assembly, Assembly::DONE);
        assert_eq!((last.mark_alpha, last.mark_scale, last.ring_alpha, last.text_alpha), (1.0, 1.0, 0.0, 0.0));
        assert_eq!(last.glow, GLOW_REST);
    }

    #[test]
    fn nothing_moves_without_animation() {
        let mut c = Choreography::new(Opening::Intro, false, 0.0);
        assert!(!c.moving(0.0));
        assert_eq!(c.motion(0.0), Motion::REST);
        assert_eq!(c.phase(3.0), crate::scene::REST_PHASE);
        c.prompt(true, 1.0);
        assert_eq!(c.motion(1.0).reveal, 1.0, "the unlock screen at once");
        c.progress(30, 2.0);
        assert_eq!(c.motion(2.0).ring_progress, Some(0.3));
        c.leave(false, 3.0);
        assert!(c.left(3.0), "no farewell");
        assert!(!c.moving(3.0));
    }
}
