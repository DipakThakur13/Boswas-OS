//! Snap layouts: resting the pointer on a window's maximize button opens a
//! small flyout with the ways a window can be arranged; clicking a zone
//! puts the window there.

use std::time::{Duration, Instant};

use boswas_ui::{tokens, Canvas};
use smithay::{
    backend::{
        allocator::Fourcc,
        renderer::{
            element::{
                memory::{MemoryRenderBuffer, MemoryRenderBufferRenderElement},
                Kind,
            },
            Texture,
        },
    },
    output::Output,
    utils::{Logical, Point, Rectangle, Scale, Size, Transform},
};

use super::{
    decoration::{Button, Decoration},
    element::Tile,
};
use crate::{effects::BoswasRenderer, render::OutputRenderElement, state::Boswas};

/// How long the pointer rests on the maximize button before the flyout
/// opens.
pub const HOVER_DELAY: Duration = Duration::from_millis(450);
/// How long the flyout stays after the pointer has left it and the button.
const LEAVE_GRACE: Duration = Duration::from_millis(300);
/// Size of one layout picture: odd, so that two halves and the gap between
/// them fill it exactly.
const LAYOUT: (i32, i32) = (97, 61);
const GAP: i32 = 12;
const PADDING: i32 = 12;
/// Space between the zones of a layout picture.
const ZONE_GAP: i32 = 3;

/// The layouts offered, each a list of zones as fractions of the layout
/// picture (x, y, width, height in halves) and the arrangement it stands
/// for.
const LAYOUTS: [&[(i32, i32, i32, i32, Tile)]; 3] = [
    &[(0, 0, 1, 2, Tile::Left), (1, 0, 1, 2, Tile::Right)],
    &[(0, 0, 1, 2, Tile::Left), (1, 0, 1, 1, Tile::TopRight), (1, 1, 1, 1, Tile::BottomRight)],
    &[
        (0, 0, 1, 1, Tile::TopLeft),
        (1, 0, 1, 1, Tile::TopRight),
        (0, 1, 1, 1, Tile::BottomLeft),
        (1, 1, 1, 1, Tile::BottomRight),
    ],
];

/// The open flyout.
pub struct SnapFlyout {
    /// The window whose maximize button it belongs to.
    pub window: u64,
    /// Where the flyout is on the desktop.
    pub rect: Rectangle<i32, Logical>,
    /// The zones, on the desktop, and what each arranges.
    zones: Vec<(Rectangle<i32, Logical>, Tile)>,
    hover: Option<usize>,
    /// When the pointer left the flyout and the button.
    left: Option<Instant>,
    picture: Option<((Option<usize>, i32, bool), MemoryRenderBuffer)>,
}

/// The flyout's size.
pub fn size() -> Size<i32, Logical> {
    let count = LAYOUTS.len() as i32;
    Size::from((2 * PADDING + count * LAYOUT.0 + (count - 1) * GAP, 2 * PADDING + LAYOUT.1))
}

/// The zones of every layout, relative to the flyout's corner.
pub fn zones() -> Vec<(Rectangle<i32, Logical>, Tile)> {
    let (half_w, half_h) = ((LAYOUT.0 - ZONE_GAP) / 2, (LAYOUT.1 - ZONE_GAP) / 2);
    let mut out = Vec::new();
    for (index, layout) in LAYOUTS.iter().enumerate() {
        let origin = Point::from((PADDING + index as i32 * (LAYOUT.0 + GAP), PADDING));
        for (x, y, w, h, tile) in layout.iter().copied() {
            let rect = Rectangle::new(
                origin + Point::from((x * (half_w + ZONE_GAP), y * (half_h + ZONE_GAP))),
                Size::from((w * half_w + (w - 1) * ZONE_GAP, h * half_h + (h - 1) * ZONE_GAP)),
            );
            out.push((rect, tile));
        }
    }
    out
}

/// Where the flyout goes for a maximize button at `button` (desktop
/// coordinates): centred under it, kept inside `area`.
pub fn place(button: Rectangle<i32, Logical>, area: Rectangle<i32, Logical>) -> Rectangle<i32, Logical> {
    let size = size();
    let x = button.loc.x + button.size.w / 2 - size.w / 2;
    let y = button.loc.y + button.size.h + 4;
    let max_x = (area.loc.x + area.size.w - size.w - 8).max(area.loc.x + 8);
    let max_y = (area.loc.y + area.size.h - size.h - 8).max(area.loc.y + 8);
    Rectangle::new(
        Point::from((x.clamp(area.loc.x + 8, max_x), y.clamp(area.loc.y + 8, max_y))),
        size,
    )
}

impl Boswas {
    /// Opens the flyout when the pointer has rested on a maximize button,
    /// and closes it when the pointer has been away. Called before each
    /// frame.
    pub fn snap_flyout_tick(&mut self) {
        let now = Instant::now();
        if let Some(flyout) = &self.snap_flyout {
            let gone = self.window_by_id(flyout.window).is_none_or(|w| self.space.element_location(&w).is_none());
            if gone || flyout.left.is_some_and(|left| now.duration_since(left) >= LEAVE_GRACE) {
                self.snap_flyout = None;
                self.schedule_redraw();
            }
            return;
        }
        let Some((id, since)) = self.maximize_hover else {
            return;
        };
        if now.duration_since(since) < HOVER_DELAY || self.pointer.is_grabbed() || self.pressed_button.is_some() {
            return;
        }
        let Some(window) = self.window_by_id(id) else {
            return;
        };
        let (Some(frame), Some(output)) = (self.window_rect(&window), self.active_output()) else {
            return;
        };
        let mut button = Decoration::button_rect(frame.size.w, Button::Maximize);
        button.loc += frame.loc;
        let Some(area) = self.space.output_geometry(&output) else {
            return;
        };
        let rect = place(button, area);
        self.snap_flyout = Some(SnapFlyout {
            window: id,
            rect,
            zones: zones().into_iter().map(|(zone, tile)| (Rectangle::new(zone.loc + rect.loc, zone.size), tile)).collect(),
            hover: None,
            left: None,
            picture: None,
        });
        self.schedule_redraw();
    }

    /// Whether the flyout is waiting to open or to close, so that frames
    /// keep coming to do it on time.
    pub fn snap_flyout_pending(&self) -> bool {
        match &self.snap_flyout {
            Some(flyout) => flyout.left.is_some(),
            None => self.maximize_hover.is_some(),
        }
    }

    /// Tells the flyout where the pointer is. Returns whether the pointer
    /// is on the flyout (which then takes it).
    pub fn snap_flyout_motion(&mut self, point: Point<f64, Logical>, on_button: bool) -> bool {
        let Some(flyout) = &mut self.snap_flyout else {
            return false;
        };
        let inside = flyout.rect.to_f64().contains(point);
        let hover = flyout.zones.iter().position(|(zone, _)| zone.to_f64().contains(point));
        if hover != flyout.hover {
            flyout.hover = hover;
            self.redraw = true;
        }
        if inside || on_button {
            flyout.left = None;
        } else if flyout.left.is_none() {
            flyout.left = Some(Instant::now());
        }
        inside
    }

    /// A click on the flyout: arranges the window as the zone under the
    /// pointer says. Returns whether the click was on the flyout.
    pub fn snap_flyout_click(&mut self, point: Point<f64, Logical>) -> bool {
        let Some(flyout) = &self.snap_flyout else {
            return false;
        };
        if !flyout.rect.to_f64().contains(point) {
            return false;
        }
        let chosen = flyout.zones.iter().find(|(zone, _)| zone.to_f64().contains(point)).map(|(_, tile)| *tile);
        let window = self.window_by_id(flyout.window);
        if let (Some(tile), Some(window)) = (chosen, window) {
            self.snap_flyout = None;
            self.maximize_hover = None;
            self.set_tile(&window, tile);
            self.activate_window(&window);
        }
        true
    }

    /// The flyout's picture on an output.
    pub fn snap_flyout_elements<R>(
        &mut self,
        renderer: &mut R,
        output: &Output,
        scale: Scale<f64>,
        elements: &mut Vec<OutputRenderElement<R>>,
    ) where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let Some(output_geo) = self.space.output_geometry(output) else {
            return;
        };
        let theme = self.theme;
        let Some(flyout) = &mut self.snap_flyout else {
            return;
        };
        if !flyout.rect.overlaps(output_geo) {
            return;
        }
        let pixel_scale = scale.x.ceil().max(1.0) as i32;
        let key = (flyout.hover, pixel_scale, theme.scheme == boswas_ui::Scheme::Light);
        if flyout.picture.as_ref().map(|(cached, _)| cached) != Some(&key) {
            let t = tokens();
            let scheme = theme.scheme;
            let (w, h) = (flyout.rect.size.w as f32, flyout.rect.size.h as f32);
            let Some(mut canvas) = Canvas::new(w, h, pixel_scale as f32) else {
                return;
            };
            canvas.fill_rounded_rect(0.0, 0.0, w, h, t.radius_flyout, t.material(scheme, "acrylic").tint);
            canvas.stroke_rounded_rect(0.5, 0.5, w - 1.0, h - 1.0, t.radius_flyout - 0.5, 1.0, t.stroke(scheme, "surface"));
            for (index, (zone, _)) in flyout.zones.iter().enumerate() {
                let (x, y) = ((zone.loc.x - flyout.rect.loc.x) as f32, (zone.loc.y - flyout.rect.loc.y) as f32);
                let (zw, zh) = (zone.size.w as f32, zone.size.h as f32);
                if flyout.hover == Some(index) {
                    canvas.fill_rounded_rect(x, y, zw, zh, t.radius_control, theme.accent);
                } else {
                    canvas.fill_rounded_rect(x, y, zw, zh, t.radius_control, t.fill(scheme, "control-hover"));
                    canvas.stroke_rounded_rect(x + 0.5, y + 0.5, zw - 1.0, zh - 1.0, t.radius_control - 0.5, 1.0, t.stroke(scheme, "surface"));
                }
            }
            let (pw, ph) = canvas.pixel_size();
            let mut bytes = vec![0u8; (pw * ph * 4) as usize];
            canvas.write_argb8888(&mut bytes);
            let buffer =
                MemoryRenderBuffer::from_slice(&bytes, Fourcc::Argb8888, (pw as i32, ph as i32), pixel_scale, Transform::Normal, None);
            flyout.picture = Some((key, buffer));
        }
        if let Some((_, buffer)) = &flyout.picture {
            let location = (flyout.rect.loc - output_geo.loc).to_physical_precise_round::<_, i32>(scale);
            if let Ok(element) = MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                location.to_f64(),
                buffer,
                None,
                None,
                Some(flyout.rect.size),
                Kind::Unspecified,
            ) {
                elements.push(OutputRenderElement::Memory(element));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rect(x: i32, y: i32, w: i32, h: i32) -> Rectangle<i32, Logical> {
        Rectangle::new(Point::from((x, y)), Size::from((w, h)))
    }

    #[test]
    fn every_layout_fills_its_picture_without_overlap() {
        let all = zones();
        assert_eq!(all.len(), 2 + 3 + 4);
        let bounds = Rectangle::new(Point::from((0, 0)), size());
        for (i, (zone, _)) in all.iter().enumerate() {
            assert!(zone.size.w > 0 && zone.size.h > 0);
            assert!(bounds.contains_rect(*zone), "{zone:?}");
            for (other, _) in &all[i + 1..] {
                assert!(!zone.overlaps(*other), "{zone:?} {other:?}");
            }
        }
        // The first layout is two halves side by side, full height.
        assert_eq!(all[0].1, Tile::Left);
        assert_eq!(all[1].1, Tile::Right);
        assert_eq!(all[0].0.size.h, LAYOUT.1);
        assert_eq!(all[0].0.loc.x + all[0].0.size.w + ZONE_GAP, all[1].0.loc.x);
        // Every arrangement a window can be snapped to is offered.
        for tile in [Tile::Left, Tile::Right, Tile::TopLeft, Tile::TopRight, Tile::BottomLeft, Tile::BottomRight] {
            assert!(all.iter().any(|(_, t)| *t == tile), "{tile:?}");
        }
    }

    #[test]
    fn the_flyout_sits_under_the_button_and_stays_on_screen() {
        let screen = rect(0, 0, 1280, 800);
        let button = rect(600, 100, 46, 32);
        let placed = place(button, screen);
        assert_eq!(placed.loc.y, 136);
        assert_eq!(placed.loc.x + placed.size.w / 2, 623);
        // A maximized window's button is in the screen's corner.
        let corner = place(rect(1188, 0, 46, 32), screen);
        assert!(screen.contains_rect(corner), "{corner:?}");
        assert_eq!(corner.loc.x + corner.size.w, 1272);
        let low = place(rect(100, 790, 46, 32), screen);
        assert!(screen.contains_rect(low));
    }
}
