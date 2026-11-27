//! The on-screen display for the volume and brightness keys.

use std::time::{Duration, Instant};

use crate::{
    action::Action,
    svc::volume_icon,
    ui::{ramp, Frame, Rect},
};

/// How long the display stays after the last key press.
pub const LINGER: Duration = Duration::from_millis(1800);
pub const SIZE: (f32, f32) = (220.0, 48.0);

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Kind {
    Volume { muted: bool },
    Brightness,
    /// The key has nothing to act on (no sound output, no backlight).
    Unavailable,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Osd {
    pub kind: Kind,
    /// 0.0 to 1.0 (volume above 100% shows a full bar).
    pub value: f32,
    pub until: Instant,
}

impl Osd {
    pub fn new(kind: Kind, value: f32, now: Instant) -> Self {
        Self {
            kind,
            value: value.clamp(0.0, 1.5),
            until: now + LINGER,
        }
    }
}

/// The volume one key press leads to: steps of 5%, capped at 100% when
/// going up (louder is a deliberate choice in quick settings' slider).
pub fn step_volume(current: f32, up: bool) -> f32 {
    let step = 0.05;
    let snapped = (current / step).round() * step;
    if up {
        (snapped + step).min(1.0_f32.max(current))
    } else {
        (snapped - step).max(0.0)
    }
}

/// The brightness one key press leads to: steps of 10%, never fully dark.
pub fn step_brightness(current: f32, up: bool) -> f32 {
    let snapped = (current * 10.0).round() / 10.0;
    if up {
        (snapped + 0.1).min(1.0)
    } else {
        (snapped - 0.1).max(0.05)
    }
}

/// The keys the lock screen and the login screen act on: the volume and
/// the brightness (their services obey nothing else).
pub fn auth_key(key: &str) -> bool {
    matches!(key, "volume-up" | "volume-down" | "volume-mute" | "brightness-up" | "brightness-down")
}

/// Where the display goes on the lock screen and the login screen, in a
/// surface of `w` by `h`: centred, above the hint at the bottom, where the
/// desktop shows it above the taskbar.
pub fn auth_rect(w: f32, h: f32) -> Rect {
    Rect::new(((w - SIZE.0) / 2.0).round(), (h - 112.0 - SIZE.1).max(0.0).round(), SIZE.0, SIZE.1)
}

/// What a change of the display draws again on the lock screen: its place
/// and its shadow.
pub fn auth_damage(w: f32, h: f32) -> Rect {
    let rect = auth_rect(w, h);
    Rect::new(rect.x - 32.0, rect.y - 32.0, rect.w + 64.0, rect.h + 64.0)
}

pub fn draw(f: &mut Frame, osd: &Osd, panel: Rect) {
    f.area(panel, Action::None);
    paint(f, osd, panel);
}

/// Draws the display without taking clicks (over the lock screen, whose
/// clicks stay its own).
pub fn paint(f: &mut Frame, osd: &Osd, panel: Rect) {
    let theme = f.theme();
    f.panel(panel, "flyout");
    let icon = match osd.kind {
        Kind::Volume { muted } => volume_icon(osd.value, muted),
        Kind::Brightness => "display-brightness-symbolic",
        Kind::Unavailable => "audio-volume-muted-symbolic",
    };
    f.icon(icon, panel.x + 16.0, panel.y + (panel.h - 16.0) / 2.0, 16.0);
    if osd.kind == Kind::Unavailable {
        f.text_left("Not available", Rect::new(panel.x + 44.0, panel.y, panel.w - 56.0, panel.h), &ramp("caption", theme.text_secondary));
        return;
    }
    let shown = match osd.kind {
        Kind::Volume { muted: true } => 0.0,
        _ => osd.value.min(1.0),
    };
    let bar = Rect::new(panel.x + 44.0, panel.y + panel.h / 2.0 - 2.0, panel.w - 44.0 - 52.0, 4.0);
    let rail = f.res.fill("control-hover");
    f.canvas.fill_rounded_rect(bar.x, bar.y, bar.w, bar.h, 2.0, rail);
    f.canvas.fill_rounded_rect(bar.x, bar.y, bar.w * shown, bar.h, 2.0, theme.accent);
    let percent = (osd.value * 100.0).round() as u32;
    f.text_right(&percent.to_string(), Rect::new(panel.right() - 48.0, panel.y, 34.0, panel.h), &ramp("body", theme.text));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn volume_steps_snap_and_stop_at_the_ends() {
        assert!((step_volume(0.5, true) - 0.55).abs() < 1e-6);
        assert!((step_volume(0.52, true) - 0.55).abs() < 1e-6, "an odd value first snaps to the grid");
        assert!((step_volume(0.5, false) - 0.45).abs() < 1e-6);
        assert_eq!(step_volume(0.02, false), 0.0);
        assert_eq!(step_volume(0.98, true), 1.0);
        assert_eq!(step_volume(1.0, true), 1.0);
        assert_eq!(step_volume(1.3, true), 1.3, "the key never lowers an amplified volume");
        assert!((step_volume(1.3, false) - 1.25).abs() < 1e-6);
    }

    #[test]
    fn the_lock_screen_shows_the_level_of_the_volume_and_brightness_keys_only() {
        for key in ["volume-up", "volume-down", "volume-mute", "brightness-up", "brightness-down"] {
            assert!(auth_key(key), "{key}");
        }
        for key in ["media-play", "media-next", "mic-mute", ""] {
            assert!(!auth_key(key), "{key}: nothing the lock screen's services would do");
        }
        for (w, h) in [(1920.0, 1080.0), (1024.0, 768.0), (480.0, 360.0)] {
            let rect = auth_rect(w, h);
            assert!(rect.x >= 0.0 && rect.right() <= w && rect.y >= 0.0, "inside {w}x{h}");
            assert!(rect.bottom() <= h - 72.0, "above the hint at the bottom of {w}x{h}");
            assert!((rect.center().0 - w / 2.0).abs() <= 1.0, "centred");
            let damage = auth_damage(w, h);
            assert!(damage.x < rect.x && damage.bottom() > rect.bottom(), "the shadow is drawn again too");
        }
    }

    #[test]
    fn brightness_steps_never_reach_black() {
        assert!((step_brightness(0.5, true) - 0.6).abs() < 1e-6);
        assert!((step_brightness(0.1, false) - 0.05).abs() < 1e-6);
        assert!((step_brightness(0.05, false) - 0.05).abs() < 1e-6);
        assert_eq!(step_brightness(0.97, true), 1.0);
    }
}
