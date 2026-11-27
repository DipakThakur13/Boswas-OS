//! The shell's side of glass: asking the compositor to blur behind its
//! translucent surfaces (`ext-background-effect-v1`).
//!
//! Where the compositor offers the blur capability (OpenGL ES, glass on),
//! a surface's glass is only its tint, grain, glow and hairline; the
//! compositor blurs what really lies behind it within the surface's blur
//! region. The region is what the glass of the last drawing covered
//! (`Resources::glass_regions`, collected as the panels and materials are
//! drawn), each rounded rectangle described row by row near its corners
//! ([`staircase`]): a region is made of rectangles, and the surface's own
//! anti-aliased translucent edge lies over the steps. Without the
//! capability the shell draws the blurred wallpaper under its glass itself
//! and sets no region.

use smithay_client_toolkit::compositor::{CompositorState, Region};
use wayland_client::{Connection, Dispatch, QueueHandle};
use wayland_protocols::ext::background_effect::v1::client::{
    ext_background_effect_manager_v1::{self, Capability, ExtBackgroundEffectManagerV1},
    ext_background_effect_surface_v1::ExtBackgroundEffectSurfaceV1,
};

use crate::{ui::Rect, Role, Shell, Surface};

/// Whether surfaces of a role may be glass with a blur region: everything
/// but the wallpaper, the invisible and dimming surfaces, the snipping
/// overlay and the lock screen (which are opaque or show no glass over
/// other surfaces).
pub(crate) fn may_blur(role: Role) -> bool {
    !matches!(role, Role::Wallpaper | Role::Dismiss | Role::Smoke | Role::Capture | Role::Auth | Role::Rename)
}

/// A rounded rectangle as rectangles in whole logical pixels: one row per
/// pixel within `radius` of the top and bottom edges (each inset as far as
/// the corner's circle is from the edge at the middle of the row), and one
/// rectangle for the rows between.
pub(crate) fn staircase(rect: Rect, radius: f32) -> Vec<(i32, i32, i32, i32)> {
    let (x0, y0) = (rect.x.round() as i32, rect.y.round() as i32);
    let (x1, y1) = ((rect.x + rect.w).round() as i32, (rect.y + rect.h).round() as i32);
    let (w, h) = (x1 - x0, y1 - y0);
    if w <= 0 || h <= 0 {
        return Vec::new();
    }
    let r = radius.clamp(0.0, (w.min(h) as f32) / 2.0);
    let rows = r.ceil() as i32;
    let inset = |row: i32| {
        // Distance of the row's middle from the corner circle's centre line.
        let dy = r - (row as f32 + 0.5);
        if dy <= 0.0 {
            return 0;
        }
        (r - (r * r - dy * dy).max(0.0).sqrt()).round() as i32
    };
    let mut out = Vec::with_capacity(2 * rows as usize + 1);
    for row in 0..rows {
        let i = inset(row);
        out.push((x0 + i, y0 + row, w - 2 * i, 1));
    }
    if h - 2 * rows > 0 {
        out.push((x0, y0 + rows, w, h - 2 * rows));
    }
    for row in (0..rows).rev() {
        let i = inset(row);
        out.push((x0 + i, y1 - row - 1, w - 2 * i, 1));
    }
    out.retain(|r| r.2 > 0 && r.3 > 0);
    out
}

/// The rectangles of a surface's blur region for what its glass covered.
pub(crate) fn region_of(glass: &[(Rect, f32)]) -> Vec<(i32, i32, i32, i32)> {
    glass.iter().flat_map(|(rect, radius)| staircase(*rect, *radius)).collect()
}

/// Sends a surface's blur region when it changed (it takes effect with the
/// surface's next commit). Returns whether it did.
pub(crate) fn send_region(surface: &mut Surface, compositor: &CompositorState, rects: Vec<(i32, i32, i32, i32)>) -> bool {
    let Some(effect) = surface.blur.as_ref() else {
        return false;
    };
    if surface.blur_region == rects {
        return false;
    }
    if rects.is_empty() {
        effect.set_blur_region(None);
    } else {
        let Ok(region) = Region::new(compositor) else {
            return false;
        };
        for &(x, y, w, h) in &rects {
            region.add(x, y, w, h);
        }
        effect.set_blur_region(Some(region.wl_region()));
    }
    surface.blur_region = rects;
    true
}

impl Shell {
    /// The compositor blurs behind the shell's glass, and glass is on.
    pub(crate) fn live_blur(&self) -> bool {
        self.blur_offered && self.res.transparency
    }
}

impl Dispatch<ExtBackgroundEffectManagerV1, ()> for Shell {
    fn event(
        shell: &mut Self,
        _: &ExtBackgroundEffectManagerV1,
        event: ext_background_effect_manager_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        if let ext_background_effect_manager_v1::Event::Capabilities { flags } = event {
            let blur = flags.into_result().is_ok_and(|flags| flags.contains(Capability::Blur));
            if blur != shell.blur_offered {
                shell.blur_offered = blur;
                shell.res.live_blur = shell.live_blur();
                // Everything glass is drawn again, with or without its own
                // backdrop.
                shell.mark_all();
            }
        }
    }
}

impl Dispatch<ExtBackgroundEffectSurfaceV1, ()> for Shell {
    fn event(_: &mut Self, _: &ExtBackgroundEffectSurfaceV1, _: <ExtBackgroundEffectSurfaceV1 as wayland_client::Proxy>::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {}
}

#[cfg(test)]
mod tests {
    use super::*;

    fn area(rects: &[(i32, i32, i32, i32)]) -> i32 {
        rects.iter().map(|r| r.2 * r.3).sum()
    }

    #[test]
    fn a_rounded_rectangle_is_a_staircase_inside_it() {
        let rects = staircase(Rect::new(10.0, 20.0, 100.0, 60.0), 8.0);
        assert_eq!(rects.len(), 8 + 1 + 8);
        // The first row is inset most, the rows towards the middle less.
        assert!(rects[0].0 > rects[7].0 && rects[7].0 >= 10);
        assert_eq!(rects[8], (10, 28, 100, 44), "the straight middle");
        // Symmetric top and bottom, and inside the rectangle.
        assert_eq!(rects[0].0, rects[16].0);
        assert_eq!(rects[16].1, 79);
        for r in &rects {
            assert!(r.0 >= 10 && r.1 >= 20 && r.0 + r.2 <= 110 && r.1 + r.3 <= 80);
        }
        // Close to the area of the rounded rectangle (100 x 60 minus the
        // four corners' 8^2 (4 - pi) / 4 each).
        let exact = 100.0 * 60.0 - 4.0 * 64.0 * (4.0 - std::f32::consts::PI) / 4.0;
        assert!((area(&rects) as f32 - exact).abs() < 40.0, "{} vs {exact}", area(&rects));
    }

    #[test]
    fn square_and_empty_rectangles() {
        assert_eq!(staircase(Rect::new(0.0, 0.0, 30.0, 10.0), 0.0), vec![(0, 0, 30, 10)]);
        assert!(staircase(Rect::new(0.0, 0.0, 0.0, 10.0), 4.0).is_empty());
        assert_eq!(region_of(&[(Rect::new(0.0, 0.0, 10.0, 10.0), 0.0), (Rect::new(20.0, 0.0, 10.0, 10.0), 0.0)]).len(), 2);
    }

    #[test]
    fn only_glass_surfaces_ask_for_blur() {
        assert!(may_blur(Role::Flyout) && may_blur(Role::Taskbar) && may_blur(Role::Toasts) && may_blur(Role::Tooltip));
        assert!(!may_blur(Role::Wallpaper) && !may_blur(Role::Dismiss) && !may_blur(Role::Auth));
    }
}
