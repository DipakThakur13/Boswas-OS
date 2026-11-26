//! `ext-background-effect-v1`: clients ask for the background behind a
//! region of a surface to be blurred.
//!
//! Every client may use it. The blur region is double-buffered surface
//! state (it takes effect with the surface's next commit, also through
//! synchronised subsurfaces), given in surface coordinates and clipped to
//! the surface; a NULL region or destroying the object removes the effect
//! with the next commit, and a destroyed surface takes its region with it.
//! The manager tells each client which effects work now (`capabilities`):
//! `blur` only while the renderer blurs (OpenGL ES) and transparency
//! effects are on; the capabilities are sent again whenever that changes.

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Mutex,
};

use smithay::{
    reexports::{
        wayland_protocols::ext::background_effect::v1::server::{
            ext_background_effect_manager_v1::{self, Capability, ExtBackgroundEffectManagerV1},
            ext_background_effect_surface_v1::{self, ExtBackgroundEffectSurfaceV1},
        },
        wayland_server::{
            backend::{ClientId, GlobalId},
            protocol::wl_surface::WlSurface,
            Client, DataInit, Dispatch, DisplayHandle, GlobalDispatch, New, Resource, Weak,
        },
    },
    utils::{Logical, Point, Rectangle, Size},
    wayland::compositor::{self, get_region_attributes, Cacheable, RectangleKind, RegionAttributes},
};

use crate::state::Boswas;

/// The blur region of a surface (double-buffered with its commits).
#[derive(Debug, Clone, Default)]
pub struct BlurRegionState {
    /// `None`: no effect.
    pub region: Option<RegionAttributes>,
}

impl Cacheable for BlurRegionState {
    fn commit(&mut self, _dh: &DisplayHandle) -> Self {
        self.clone()
    }

    fn merge_into(self, into: &mut Self, _dh: &DisplayHandle) {
        *into = self;
    }
}

/// Whether a surface has an effect object already (one per surface).
#[derive(Debug, Default)]
struct Taken(AtomicBool);

/// The surface an effect object belongs to.
#[derive(Debug)]
pub struct EffectSurfaceData(Mutex<Weak<WlSurface>>);

impl EffectSurfaceData {
    fn surface(&self) -> Option<WlSurface> {
        self.0.lock().ok().and_then(|weak| weak.upgrade().ok())
    }
}

/// The global and the managers bound to it.
#[derive(Debug)]
pub struct BackgroundEffectState {
    _global: GlobalId,
    managers: Vec<ExtBackgroundEffectManagerV1>,
    /// Whether blur works now.
    blur: bool,
}

impl BackgroundEffectState {
    pub fn new(display: &DisplayHandle) -> Self {
        Self {
            _global: display.create_global::<Boswas, ExtBackgroundEffectManagerV1, _>(1, ()),
            managers: Vec::new(),
            blur: false,
        }
    }

    /// The capabilities as the protocol sends them.
    pub fn capabilities(&self) -> Capability {
        if self.blur {
            Capability::Blur
        } else {
            Capability::empty()
        }
    }

    /// Whether blur works now; the bound managers are told when it changes.
    pub fn set_blur(&mut self, blur: bool) {
        if self.blur == blur {
            return;
        }
        self.blur = blur;
        let capabilities = self.capabilities();
        self.managers.retain(|manager| manager.is_alive());
        for manager in &self.managers {
            manager.capabilities(capabilities);
        }
    }

    pub fn blur(&self) -> bool {
        self.blur
    }
}

/// The blur region of a surface as it is now (its last commit), in surface
/// coordinates and clipped to `size`, as rectangles that do not overlap;
/// empty for none.
pub fn blur_region(surface: &WlSurface, size: Size<i32, Logical>) -> Vec<Rectangle<i32, Logical>> {
    let region = compositor::with_states(surface, |states| states.cached_state.get::<BlurRegionState>().current().region.clone());
    match region {
        Some(region) => region_rects(&region, size),
        None => Vec::new(),
    }
}

/// The rectangles a region covers within `size` (additions minus the
/// subtractions that follow them, in order), not overlapping.
pub fn region_rects(region: &RegionAttributes, size: Size<i32, Logical>) -> Vec<Rectangle<i32, Logical>> {
    let bounds = Rectangle::new(Point::from((0, 0)), size);
    let mut rects: Vec<Rectangle<i32, Logical>> = Vec::new();
    for (kind, rect) in &region.rects {
        match kind {
            RectangleKind::Add => {
                let Some(rect) = rect.intersection(bounds) else {
                    continue;
                };
                // Only the part not covered yet, so the rectangles stay apart.
                let new = Rectangle::subtract_rects_many([rect], rects.iter().copied());
                rects.extend(new);
            }
            RectangleKind::Subtract => {
                rects = Rectangle::subtract_rects_many(rects, [*rect]);
            }
        }
    }
    rects.retain(|r| r.size.w > 0 && r.size.h > 0);
    rects
}

impl GlobalDispatch<ExtBackgroundEffectManagerV1, ()> for Boswas {
    fn bind(
        state: &mut Self,
        _handle: &DisplayHandle,
        _client: &Client,
        resource: New<ExtBackgroundEffectManagerV1>,
        _data: &(),
        data_init: &mut DataInit<'_, Self>,
    ) {
        let manager = data_init.init(resource, ());
        manager.capabilities(state.glass.effects.capabilities());
        state.glass.effects.managers.push(manager);
    }
}

impl Dispatch<ExtBackgroundEffectManagerV1, ()> for Boswas {
    fn request(
        state: &mut Self,
        _client: &Client,
        manager: &ExtBackgroundEffectManagerV1,
        request: ext_background_effect_manager_v1::Request,
        _data: &(),
        _handle: &DisplayHandle,
        data_init: &mut DataInit<'_, Self>,
    ) {
        match request {
            ext_background_effect_manager_v1::Request::GetBackgroundEffect { id, surface } => {
                let taken = compositor::with_states(&surface, |states| {
                    states.data_map.insert_if_missing_threadsafe(Taken::default);
                    states.data_map.get::<Taken>().is_some_and(|taken| taken.0.swap(true, Ordering::AcqRel))
                });
                if taken {
                    manager.post_error(
                        ext_background_effect_manager_v1::Error::BackgroundEffectExists,
                        "the surface already has a background effect object",
                    );
                    return;
                }
                data_init.init(id, EffectSurfaceData(Mutex::new(surface.downgrade())));
            }
            ext_background_effect_manager_v1::Request::Destroy => {
                state.glass.effects.managers.retain(|m| m != manager);
            }
            _ => {}
        }
    }

    fn destroyed(state: &mut Self, _client: ClientId, manager: &ExtBackgroundEffectManagerV1, _data: &()) {
        state.glass.effects.managers.retain(|m| m != manager);
    }
}

impl Dispatch<ExtBackgroundEffectSurfaceV1, EffectSurfaceData> for Boswas {
    fn request(
        _state: &mut Self,
        _client: &Client,
        effect: &ExtBackgroundEffectSurfaceV1,
        request: ext_background_effect_surface_v1::Request,
        data: &EffectSurfaceData,
        _handle: &DisplayHandle,
        _data_init: &mut DataInit<'_, Self>,
    ) {
        match request {
            ext_background_effect_surface_v1::Request::SetBlurRegion { region } => {
                let Some(surface) = data.surface() else {
                    effect.post_error(ext_background_effect_surface_v1::Error::SurfaceDestroyed, "the surface was destroyed");
                    return;
                };
                // Copy semantics: the region object may go right away.
                let region = region.as_ref().map(get_region_attributes);
                compositor::with_states(&surface, |states| {
                    states.cached_state.get::<BlurRegionState>().pending().region = region;
                });
            }
            ext_background_effect_surface_v1::Request::Destroy => {
                // The effect goes with the next commit; the surface may get
                // a new object.
                if let Some(surface) = data.surface() {
                    compositor::with_states(&surface, |states| {
                        states.cached_state.get::<BlurRegionState>().pending().region = None;
                        if let Some(taken) = states.data_map.get::<Taken>() {
                            taken.0.store(false, Ordering::Release);
                        }
                    });
                }
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rect(x: i32, y: i32, w: i32, h: i32) -> Rectangle<i32, Logical> {
        Rectangle::new(Point::from((x, y)), Size::from((w, h)))
    }

    fn area(rects: &[Rectangle<i32, Logical>]) -> i32 {
        rects.iter().map(|r| r.size.w * r.size.h).sum()
    }

    #[test]
    fn a_region_is_clipped_to_the_surface_and_kept_without_overlaps() {
        let size = Size::from((100, 80));
        let region = RegionAttributes {
            rects: vec![(RectangleKind::Add, rect(-10, -10, 60, 60)), (RectangleKind::Add, rect(20, 20, 200, 20))],
        };
        let rects = region_rects(&region, size);
        // 50x50 inside, plus the part of the strip outside it: 80x20 - 30x20.
        assert_eq!(area(&rects), 50 * 50 + 80 * 20 - 30 * 20);
        for (i, a) in rects.iter().enumerate() {
            assert!(a.loc.x >= 0 && a.loc.y >= 0 && a.loc.x + a.size.w <= 100 && a.loc.y + a.size.h <= 80);
            for b in &rects[i + 1..] {
                assert!(a.intersection(*b).is_none(), "{a:?} overlaps {b:?}");
            }
        }
    }

    #[test]
    fn subtractions_cut_holes_in_order() {
        let size = Size::from((100, 100));
        let region = RegionAttributes {
            rects: vec![
                (RectangleKind::Add, rect(0, 0, 100, 100)),
                (RectangleKind::Subtract, rect(0, 0, 10, 10)),
                (RectangleKind::Add, rect(0, 0, 5, 5)),
            ],
        };
        let rects = region_rects(&region, size);
        assert_eq!(area(&rects), 100 * 100 - 100 + 25);
        assert!(region_rects(&RegionAttributes::default(), size).is_empty());
    }

    #[test]
    fn region_state_is_double_buffered_by_copy() {
        let dh = smithay::reexports::wayland_server::Display::<()>::new().unwrap().handle();
        let mut pending = BlurRegionState {
            region: Some(RegionAttributes { rects: vec![(RectangleKind::Add, rect(1, 2, 3, 4))] }),
        };
        let mut current = BlurRegionState::default();
        let size = Size::from((100, 100));
        let rects = |state: &BlurRegionState| state.region.as_ref().map(|r| region_rects(r, size));
        assert_eq!(rects(&current), None, "no effect until a commit");
        pending.commit(&dh).merge_into(&mut current, &dh);
        assert_eq!(rects(&current), Some(vec![rect(1, 2, 3, 4)]));
        // Removing it is again a pending change until the next commit.
        pending.region = None;
        assert!(current.region.is_some());
        pending.commit(&dh).merge_into(&mut current, &dh);
        assert_eq!(rects(&current), None);
    }
}
