//! Handing drawings to the compositor.
//!
//! Each surface keeps its drawing (premultiplied RGBA, as drawn) and a few
//! shared-memory buffers of its size. A buffer remembers which part of the
//! drawing it does not hold yet; when it is used again, only that part is
//! converted and copied into it, so a hover change in Start copies a tile,
//! not Start. Buffers are kept, not made anew for every frame: a compositor
//! that copies a buffer when it is committed (OpenGL ES) hands it back at
//! once and then only uploads what changed in it.
//!
//! Opening and closing transitions (a slide and a fade) are done by the
//! compositor where it can: the surface's drawing is sent once, and every
//! frame of the transition changes only its opacity (`wp_alpha_modifier`)
//! and its place (the layer surface's margin), so no frame of a transition
//! is drawn or copied in the shell.

use boswas_ui::{canvas::PixelRect, Canvas};
use smithay_client_toolkit::{
    shell::wlr_layer::Anchor,
    shm::{
        slot::{Buffer, SlotPool},
        Shm,
    },
};
use wayland_client::protocol::wl_shm;

/// Buffers kept per surface at most; more are made only while the
/// compositor holds all of them.
const MAX_BUFFERS: usize = 4;

/// How far (logical pixels) a surface slides while it opens or closes.
pub const SLIDE: f32 = 10.0;

/// The smallest rectangle holding both.
pub fn union(a: PixelRect, b: PixelRect) -> PixelRect {
    let (x0, y0) = (a.0.min(b.0), a.1.min(b.1));
    let (x1, y1) = ((a.0 + a.2).max(b.0 + b.2), (a.1 + a.3).max(b.1 + b.3));
    (x0, y0, x1 - x0, y1 - y0)
}

/// The shared-memory buffers of one surface.
#[derive(Default)]
pub struct Buffers {
    pool: Option<SlotPool>,
    kept: Vec<Kept>,
    /// The size of the buffers in device pixels.
    size: (u32, u32),
    /// The buffer attached last.
    last: Option<usize>,
}

struct Kept {
    buffer: Buffer,
    /// The part of the drawing this buffer does not hold yet (`None`: it
    /// holds all of it).
    stale: Option<PixelRect>,
}

impl Buffers {
    /// The drawing changed in `rect`: no buffer holds that part any more.
    pub fn changed(&mut self, rect: PixelRect) {
        for kept in &mut self.kept {
            kept.stale = Some(kept.stale.map_or(rect, |stale| union(stale, rect)));
        }
    }

    /// A buffer holding `drawing` (what it lacked is copied into it), to
    /// attach. `None` when no buffer can be had.
    pub fn fill(&mut self, shm: &Shm, drawing: &Canvas) -> Option<&Buffer> {
        let (w, h) = drawing.pixel_size();
        if self.size != (w, h) {
            self.kept.clear();
            self.last = None;
            self.size = (w, h);
        }
        let bytes = w as usize * h as usize * 4;
        if self.pool.is_none() {
            self.pool = SlotPool::new(bytes, shm).ok();
        }
        let pool = self.pool.as_mut()?;
        // A free buffer, the one attached last first.
        let order = self.last.into_iter().chain((0..self.kept.len()).filter(|i| Some(*i) != self.last));
        let free: Vec<usize> = order.collect();
        let index = match free.into_iter().find(|i| self.kept[*i].buffer.canvas(pool).is_some()) {
            Some(index) => index,
            None => {
                if self.kept.len() >= MAX_BUFFERS {
                    // The oldest is given up (it is destroyed once the
                    // compositor releases it).
                    self.kept.remove(0);
                    self.last = self.last.and_then(|last| last.checked_sub(1));
                }
                let (buffer, _) = pool.create_buffer(w as i32, h as i32, w as i32 * 4, wl_shm::Format::Argb8888).ok()?;
                self.kept.push(Kept {
                    buffer,
                    stale: Some((0, 0, w, h)),
                });
                self.kept.len() - 1
            }
        };
        let kept = &mut self.kept[index];
        if let Some(rect) = kept.stale.take() {
            let data = kept.buffer.canvas(pool)?;
            drawing.write_argb8888_rect(data, rect);
        }
        self.last = Some(index);
        Some(&self.kept[index].buffer)
    }

    /// A buffer holding `picture`, a drawing that is not the surface's own
    /// (a frame of a transition composed in the shell): every buffer lacks
    /// the surface's drawing afterwards.
    pub fn fill_other(&mut self, shm: &Shm, picture: &Canvas) -> Option<&Buffer> {
        let (w, h) = picture.pixel_size();
        self.changed((0, 0, w, h));
        let index = {
            self.fill(shm, picture)?;
            self.last?
        };
        self.kept[index].stale = Some((0, 0, w, h));
        Some(&self.kept[index].buffer)
    }
}

/// How a layer surface anchored at `anchor` slides into place: `Some(1)`
/// when its top margin grows to move it down, `Some(-1)` when its bottom
/// margin shrinks; `None` when it is anchored to neither or both edges
/// (then the shell composes the slide itself).
pub fn slide_direction(anchor: Anchor) -> Option<i32> {
    match (anchor.contains(Anchor::TOP), anchor.contains(Anchor::BOTTOM)) {
        (true, false) => Some(1),
        (false, true) => Some(-1),
        _ => None,
    }
}

/// The margins (top, right, bottom, left) of a surface whose resting
/// margins are `margin`, moved down by `offset` logical pixels.
pub fn slid_margin(margin: (i32, i32, i32, i32), direction: i32, offset: i32) -> (i32, i32, i32, i32) {
    let (top, right, bottom, left) = margin;
    if direction > 0 {
        (top + offset, right, bottom, left)
    } else {
        (top, right, bottom - offset, left)
    }
}

/// How far down a surface is drawn at `presence` (0.0 gone, 1.0 there), in
/// whole logical pixels.
pub fn slide_offset(presence: f32) -> i32 {
    ((1.0 - presence.clamp(0.0, 1.0)) * SLIDE).round() as i32
}

/// The `wp_alpha_modifier_surface_v1` multiplier for an opacity.
pub fn alpha_multiplier(presence: f32) -> u32 {
    (f64::from(presence.clamp(0.0, 1.0)) * f64::from(u32::MAX)).round() as u32
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rectangles_unite() {
        assert_eq!(union((10, 10, 5, 5), (0, 12, 2, 20)), (0, 10, 15, 22));
        assert_eq!(union((3, 4, 5, 6), (3, 4, 5, 6)), (3, 4, 5, 6));
    }

    #[test]
    fn surfaces_slide_down_from_the_edge_they_are_anchored_to() {
        assert_eq!(slide_direction(Anchor::BOTTOM), Some(-1));
        assert_eq!(slide_direction(Anchor::BOTTOM | Anchor::RIGHT), Some(-1));
        assert_eq!(slide_direction(Anchor::TOP | Anchor::LEFT), Some(1));
        assert_eq!(slide_direction(Anchor::empty()), None, "centred: composed in the shell");
        assert_eq!(slide_direction(Anchor::TOP | Anchor::BOTTOM), None);
        // Start above a taskbar of 48: ten pixels lower at first.
        assert_eq!(slid_margin((0, 0, 48, 0), -1, slide_offset(0.0)), (0, 0, 38, 0));
        assert_eq!(slid_margin((0, 0, 48, 0), -1, slide_offset(1.0)), (0, 0, 48, 0));
        assert_eq!(slid_margin((100, 0, 0, 20), 1, slide_offset(0.5)), (105, 0, 0, 20));
        assert_eq!(alpha_multiplier(1.0), u32::MAX);
        assert_eq!(alpha_multiplier(0.0), 0);
        assert_eq!(alpha_multiplier(2.0), u32::MAX);
    }
}
