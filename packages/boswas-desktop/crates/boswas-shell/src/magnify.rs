//! Icons that grow under the pointer, as in a dock: the same magnification
//! as Control Center's lists (the Qt toolkit's `magnify.py`).
//!
//! The icon under the pointer (or, with the pointer elsewhere, the one with
//! the keyboard focus) grows to [`GROW`] times its size, its neighbours in
//! the row to [`NEAR`], every other icon stays at its own size. An icon
//! grows upwards from its lower edge and never leaves its cell, so the
//! factor is smaller where the cell has less room ([`limit`]); nothing is
//! laid out again and the label never moves. Each icon moves to its new
//! size over the fast duration with the decelerate curve, drawn again at
//! the display's pace: the drawing code reports the cells whose icon is
//! still moving ([`Resources::magnifying`](crate::ui::Resources)) and only
//! those are drawn again, frame by frame, until they stand. With reduced
//! motion there is no magnification.

use std::{cell::RefCell, collections::HashMap, time::Instant};

use boswas_ui::tokens::{cubic_bezier, tokens};

use crate::ui::{Frame, Rect};

/// The icon under the pointer or with the focus.
pub const GROW: f32 = 1.4;
/// The icons beside it.
pub const NEAR: f32 = 1.15;

/// Which icon: the panel it is in and its place there.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Key(pub &'static str, pub usize);

/// An icon on its way from one size to another.
#[derive(Clone, Copy, Debug)]
struct Motion {
    from: f32,
    to: f32,
    start: Instant,
}

thread_local! {
    static MOTIONS: RefCell<HashMap<Key, Motion>> = RefCell::new(HashMap::new());
    /// Drawing tests leave the magnification off unless they ask for it
    /// (a hover would otherwise animate between the drawings they compare).
    static ENABLED: std::cell::Cell<bool> = const { std::cell::Cell::new(!cfg!(test)) };
}

/// Turns the magnification on or off (tests).
#[cfg(test)]
pub fn set_enabled(on: bool) {
    ENABLED.with(|enabled| enabled.set(on));
    MOTIONS.with(|motions| motions.borrow_mut().clear());
}

/// The largest factor an icon of `icon` logical pixels may grow to when it
/// grows upwards into `above` pixels of its cell and sideways into a cell
/// `width` wide.
pub fn limit(icon: f32, above: f32, width: f32) -> f32 {
    if icon <= 0.0 {
        return 1.0;
    }
    GROW.min(1.0 + above.max(0.0) / icon).min(width / icon).max(1.0)
}

/// How the icons of a panel are lined up: their neighbours are beside them
/// in a row, or above and below them in a list.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Axis {
    Row,
    /// A list (no panel of the shell magnifies one yet).
    #[cfg_attr(not(test), allow(dead_code))]
    Column,
}

/// What an icon in `cell` is going to: grown under the pointer (or with
/// the focus while the pointer is not on the panel's icons), less beside
/// the one under the pointer (the cells next to it along `axis`), else its
/// own size. `most` caps it ([`limit`]).
pub fn target(cell: Rect, pointer: Option<(f32, f32)>, focused: bool, most: f32, axis: Axis) -> f32 {
    let grown = |factor: f32| factor.min(most).max(1.0);
    let beside = |(x, y): (f32, f32)| match axis {
        Axis::Row => y >= cell.y && y < cell.bottom() && ((x >= cell.x - cell.w && x < cell.x) || (x >= cell.right() && x < cell.right() + cell.w)),
        Axis::Column => x >= cell.x && x < cell.right() && ((y >= cell.y - cell.h && y < cell.y) || (y >= cell.bottom() && y < cell.bottom() + cell.h)),
    };
    match pointer {
        Some(p) if cell.contains(p) => grown(GROW),
        Some(p) if beside(p) => grown(NEAR),
        None if focused => grown(GROW),
        _ => 1.0,
    }
}

/// The size of an icon now, as a factor of its own, moving towards
/// `target`; and whether it is still moving.
pub fn scale(key: Key, target: f32, now: Instant, reduced_motion: bool) -> (f32, bool) {
    if reduced_motion || !ENABLED.with(std::cell::Cell::get) {
        return (1.0, false);
    }
    let duration = tokens().motion_fast_ms as f32 / 1000.0;
    let curve = tokens().curve("decelerate");
    MOTIONS.with(|motions| {
        let mut motions = motions.borrow_mut();
        let current = |m: &Motion| {
            let t = (now.saturating_duration_since(m.start).as_secs_f32() / duration.max(1e-3)).clamp(0.0, 1.0);
            (m.from + (m.to - m.from) * cubic_bezier(curve, t), t >= 1.0)
        };
        let (value, done) = match motions.get(&key) {
            Some(m) => current(m),
            None => (1.0, true),
        };
        let heading = motions.get(&key).map_or(1.0, |m| m.to);
        if (heading - target).abs() > 1e-4 {
            motions.insert(key, Motion { from: value, to: target, start: now });
            return (value, true);
        }
        if done {
            if (target - 1.0).abs() < 1e-4 {
                motions.remove(&key);
            }
            return (target, false);
        }
        (value, true)
    })
}

impl Frame<'_> {
    /// The factor of a magnifying icon in `cell` now (capped at `most`);
    /// a cell whose icon is still moving is drawn again on the next frame.
    pub fn magnify(&mut self, key: Key, cell: Rect, focused: bool, most: f32, axis: Axis) -> f32 {
        let goal = target(cell, self.pointer, focused, most, axis);
        let (factor, moving) = scale(key, goal, Instant::now(), self.res.reduced_motion);
        if moving {
            self.res.magnifying.push(cell);
        }
        factor
    }

    /// An icon that magnifies: drawn at `size` scaled by its factor now,
    /// growing upwards from its lower edge around its centre line, inside
    /// `cell` (whose hover look it is part of). `above` is the room above
    /// the icon in the cell. Returns the factor.
    #[allow(clippy::too_many_arguments)]
    pub fn magnified_icon(&mut self, key: Key, names: &[&str], x: f32, y: f32, size: f32, cell: Rect, focused: bool, above: f32) -> f32 {
        let most = limit(size, above, cell.w);
        let factor = self.magnify(key, cell, focused, most, Axis::Row);
        let moving = self.res.magnifying.last() == Some(&cell);
        if factor <= 1.0 + 1e-4 {
            self.icon_or_placeholder(names, x, y, size);
            return factor;
        }
        // Rasterised once at its largest and drawn smaller on the way (two
        // sizes of each icon are kept, not one per frame); at its largest it
        // stands on whole device pixels.
        let grown = size * factor;
        let largest = size * most;
        let scale = self.canvas.scale();
        let pixels = (largest * scale).round().max(1.0) as u32;
        let (gx, gy) = (x + (size - grown) / 2.0, y + size - grown);
        let picture = names.iter().filter(|name| !name.is_empty()).find_map(|name| self.icon_pixmap(name, pixels));
        match picture {
            Some(picture) if (factor - most).abs() < 1e-4 && !moving => {
                let side = pixels as f32 / scale;
                let (sx, sy) = (((x + (size - side) / 2.0) * scale).round() / scale, ((y + size - side) * scale).round() / scale);
                self.canvas.draw_pixmap(&picture, sx, sy, side, side, 1.0);
            }
            Some(picture) => self.canvas.draw_pixmap(&picture, gx, gy, grown, grown, 1.0),
            // Not installed at any size: looking it up again at every size
            // on the way (a file search each frame) would find nothing.
            None => self.placeholder(gx, gy, grown),
        }
        factor
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    #[test]
    fn the_hovered_icon_grows_most_and_its_neighbours_less() {
        let cell = Rect::new(100.0, 50.0, 96.0, 90.0);
        assert_eq!(target(cell, Some((140.0, 70.0)), false, GROW, Axis::Row), GROW);
        assert_eq!(target(cell, Some((60.0, 70.0)), false, GROW, Axis::Row), NEAR, "the cell to the left is hovered");
        assert_eq!(target(cell, Some((250.0, 70.0)), false, GROW, Axis::Row), NEAR, "the cell to the right");
        assert_eq!(target(cell, Some((140.0, 160.0)), false, GROW, Axis::Row), 1.0, "the row below");
        assert_eq!(target(cell, Some((400.0, 70.0)), false, GROW, Axis::Row), 1.0, "further away");
        assert_eq!(target(cell, None, true, GROW, Axis::Row), GROW, "the focus, with the pointer away");
        assert_eq!(target(cell, Some((400.0, 70.0)), true, GROW, Axis::Row), 1.0, "the pointer wins over the focus");
        // In a list the neighbours are above and below.
        assert_eq!(target(cell, Some((140.0, 160.0)), false, GROW, Axis::Column), NEAR);
        assert_eq!(target(cell, Some((60.0, 70.0)), false, GROW, Axis::Column), 1.0);
    }

    #[test]
    fn an_icon_never_leaves_its_cell() {
        // A 36 px icon with 10 px above it: at most 1 + 10/36.
        let most = limit(36.0, 10.0, 96.0);
        assert!((most - (1.0 + 10.0 / 36.0)).abs() < 1e-5);
        assert_eq!(target(Rect::new(0.0, 0.0, 96.0, 90.0), Some((10.0, 10.0)), false, most, Axis::Row), most);
        assert_eq!(limit(36.0, 40.0, 96.0), GROW);
        assert_eq!(limit(36.0, 40.0, 40.0), 40.0 / 36.0, "a narrow cell");
        assert_eq!(limit(36.0, 0.0, 96.0), 1.0, "no room: no growth");
    }

    #[test]
    fn icons_ease_to_their_size_and_stand() {
        set_enabled(true);
        let start = Instant::now();
        let key = Key("test", 1);
        assert_eq!(scale(key, 1.4, start, false), (1.0, true), "starts where it is");
        let (mid, moving) = scale(key, 1.4, start + Duration::from_millis(60), false);
        assert!(moving && mid > 1.0 && mid < 1.4, "{mid}");
        // Decelerating: more than half the way after 40 % of the time.
        assert!(mid > 1.2, "{mid}");
        assert_eq!(scale(key, 1.4, start + Duration::from_millis(400), false), (1.4, false));
        // Back to its size, from where it is.
        let (back, moving) = scale(key, 1.0, start + Duration::from_millis(500), false);
        assert!(moving && (back - 1.4).abs() < 1e-4);
        assert_eq!(scale(key, 1.0, start + Duration::from_millis(900), false), (1.0, false));
        assert!(MOTIONS.with(|m| m.borrow().is_empty()), "nothing is kept for icons at rest");
        // Reduced motion: no magnification.
        assert_eq!(scale(key, 1.4, start, true), (1.0, false));
        set_enabled(false);
    }
}
