//! Interactive move and resize.

use smithay::{
    input::pointer::{
        AxisFrame, ButtonEvent, GestureHoldBeginEvent, GestureHoldEndEvent, GesturePinchBeginEvent,
        GesturePinchEndEvent, GesturePinchUpdateEvent, GestureSwipeBeginEvent, GestureSwipeEndEvent,
        GestureSwipeUpdateEvent, GrabStartData, MotionEvent, PointerGrab, PointerInnerHandle, RelativeMotionEvent,
    },
    reexports::{
        wayland_protocols::xdg::shell::server::xdg_toplevel, wayland_server::protocol::wl_surface::WlSurface,
    },
    utils::{IsAlive, Logical, Point, Rectangle, Size},
};

use super::element::{Tile, WindowElement};
use crate::state::Boswas;

const BTN_LEFT: u32 = 0x110;
const BTN_RIGHT: u32 = 0x111;

bitflags::bitflags! {
    /// The edges a resize drags.
    #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
    pub struct ResizeEdge: u32 {
        const TOP = 0b0001;
        const BOTTOM = 0b0010;
        const LEFT = 0b0100;
        const RIGHT = 0b1000;
    }
}

impl From<xdg_toplevel::ResizeEdge> for ResizeEdge {
    fn from(edge: xdg_toplevel::ResizeEdge) -> Self {
        Self::from_bits_truncate(edge as u32)
    }
}

impl ResizeEdge {
    /// The XCursor name for this edge combination.
    pub fn cursor(self) -> smithay::input::pointer::CursorIcon {
        use smithay::input::pointer::CursorIcon as C;
        let (t, b, l, r) = (
            self.contains(Self::TOP),
            self.contains(Self::BOTTOM),
            self.contains(Self::LEFT),
            self.contains(Self::RIGHT),
        );
        match (t, b, l, r) {
            (true, _, true, _) => C::NwResize,
            (true, _, _, true) => C::NeResize,
            (_, true, true, _) => C::SwResize,
            (_, true, _, true) => C::SeResize,
            (true, ..) => C::NResize,
            (_, true, ..) => C::SResize,
            (_, _, true, _) => C::WResize,
            (_, _, _, true) => C::EResize,
            _ => C::Default,
        }
    }
}

/// Forwards every pointer event a grab does not care about.
macro_rules! forward_gestures {
    () => {
        fn relative_motion(
            &mut self,
            data: &mut Boswas,
            handle: &mut PointerInnerHandle<'_, Boswas>,
            focus: Option<(WlSurface, Point<f64, Logical>)>,
            event: &RelativeMotionEvent,
        ) {
            handle.relative_motion(data, focus, event);
        }

        fn axis(&mut self, data: &mut Boswas, handle: &mut PointerInnerHandle<'_, Boswas>, details: AxisFrame) {
            handle.axis(data, details);
        }

        fn frame(&mut self, data: &mut Boswas, handle: &mut PointerInnerHandle<'_, Boswas>) {
            handle.frame(data);
        }

        fn gesture_swipe_begin(
            &mut self,
            data: &mut Boswas,
            handle: &mut PointerInnerHandle<'_, Boswas>,
            event: &GestureSwipeBeginEvent,
        ) {
            handle.gesture_swipe_begin(data, event);
        }

        fn gesture_swipe_update(
            &mut self,
            data: &mut Boswas,
            handle: &mut PointerInnerHandle<'_, Boswas>,
            event: &GestureSwipeUpdateEvent,
        ) {
            handle.gesture_swipe_update(data, event);
        }

        fn gesture_swipe_end(
            &mut self,
            data: &mut Boswas,
            handle: &mut PointerInnerHandle<'_, Boswas>,
            event: &GestureSwipeEndEvent,
        ) {
            handle.gesture_swipe_end(data, event);
        }

        fn gesture_pinch_begin(
            &mut self,
            data: &mut Boswas,
            handle: &mut PointerInnerHandle<'_, Boswas>,
            event: &GesturePinchBeginEvent,
        ) {
            handle.gesture_pinch_begin(data, event);
        }

        fn gesture_pinch_update(
            &mut self,
            data: &mut Boswas,
            handle: &mut PointerInnerHandle<'_, Boswas>,
            event: &GesturePinchUpdateEvent,
        ) {
            handle.gesture_pinch_update(data, event);
        }

        fn gesture_pinch_end(
            &mut self,
            data: &mut Boswas,
            handle: &mut PointerInnerHandle<'_, Boswas>,
            event: &GesturePinchEndEvent,
        ) {
            handle.gesture_pinch_end(data, event);
        }

        fn gesture_hold_begin(
            &mut self,
            data: &mut Boswas,
            handle: &mut PointerInnerHandle<'_, Boswas>,
            event: &GestureHoldBeginEvent,
        ) {
            handle.gesture_hold_begin(data, event);
        }

        fn gesture_hold_end(
            &mut self,
            data: &mut Boswas,
            handle: &mut PointerInnerHandle<'_, Boswas>,
            event: &GestureHoldEndEvent,
        ) {
            handle.gesture_hold_end(data, event);
        }

        fn start_data(&self) -> &GrabStartData<Boswas> {
            &self.start_data
        }
    };
}

/// Dragging a window by its title bar. Dragging to a screen edge previews
/// and then applies a snap layout; dragging a snapped or maximized window
/// returns it to its floating size first.
pub struct MoveGrab {
    pub start_data: GrabStartData<Boswas>,
    pub window: WindowElement,
    /// Frame location when the drag started (or when it left its tile).
    pub initial_location: Point<i32, Logical>,
    /// Pointer position that `initial_location` belongs to.
    pub anchor: Point<f64, Logical>,
}

impl PointerGrab<Boswas> for MoveGrab {
    fn motion(
        &mut self,
        data: &mut Boswas,
        handle: &mut PointerInnerHandle<'_, Boswas>,
        _focus: Option<(WlSurface, Point<f64, Logical>)>,
        event: &MotionEvent,
    ) {
        // While a window is dragged no client has pointer focus.
        handle.motion(data, None, event);
        if !self.window.alive() {
            return;
        }

        let delta = event.location - self.anchor;
        if self.window.data().tile.get() != Tile::Floating {
            if delta.x.abs().max(delta.y.abs()) < 12.0 {
                return;
            }
            self.initial_location = data.float_for_drag(&self.window, self.anchor, event.location);
            self.anchor = event.location;
        }
        let delta = event.location - self.anchor;
        let location = (self.initial_location.to_f64() + delta).to_i32_round();
        data.move_window(&self.window, location);
        // Near the top of a screen the snap layouts open; over one of
        // their zones the preview shows that zone.
        let zone = data.drag_snap_flyout(self.window.id(), event.location);
        data.snap_preview = match zone {
            Some((_, rect)) => Some(rect),
            None => data.snap_target(event.location).map(|(_, rect)| rect),
        };
        data.schedule_redraw();
    }

    fn button(&mut self, data: &mut Boswas, handle: &mut PointerInnerHandle<'_, Boswas>, event: &ButtonEvent) {
        handle.button(data, event);
        if !handle.current_pressed().contains(&BTN_LEFT) {
            let pos = handle.current_location();
            let zone = data.drag_snap_flyout(self.window.id(), pos);
            if let Some((tile, _)) = zone.or_else(|| data.snap_target(pos)) {
                data.set_tile(&self.window, tile);
            }
            data.end_drag_snap_flyout();
            data.snap_preview = None;
            data.schedule_redraw();
            handle.unset_grab(self, data, event.serial, event.time, true);
        }
    }

    forward_gestures!();

    fn unset(&mut self, data: &mut Boswas) {
        data.snap_preview = None;
        data.end_drag_snap_flyout();
    }
}

/// Dragging a window edge or corner.
pub struct ResizeGrab {
    start_data: GrabStartData<Boswas>,
    window: WindowElement,
    edges: ResizeEdge,
    /// Frame rectangle when the drag started.
    initial_rect: Rectangle<i32, Logical>,
    last_size: Size<i32, Logical>,
}

impl ResizeGrab {
    pub fn start(
        start_data: GrabStartData<Boswas>,
        window: WindowElement,
        edges: ResizeEdge,
        initial_rect: Rectangle<i32, Logical>,
    ) -> Self {
        window.data().resize.set(ResizeState::Resizing { edges, initial_rect });
        Self {
            start_data,
            window,
            edges,
            initial_rect,
            last_size: initial_rect.size,
        }
    }
}

impl PointerGrab<Boswas> for ResizeGrab {
    fn motion(
        &mut self,
        data: &mut Boswas,
        handle: &mut PointerInnerHandle<'_, Boswas>,
        _focus: Option<(WlSurface, Point<f64, Logical>)>,
        event: &MotionEvent,
    ) {
        handle.motion(data, None, event);
        if !self.window.alive() {
            return;
        }

        let delta = event.location - self.start_data.location;
        let (mut width, mut height) = (self.initial_rect.size.w, self.initial_rect.size.h);
        if self.edges.contains(ResizeEdge::LEFT) {
            width = (f64::from(width) - delta.x) as i32;
        } else if self.edges.contains(ResizeEdge::RIGHT) {
            width = (f64::from(width) + delta.x) as i32;
        }
        if self.edges.contains(ResizeEdge::TOP) {
            height = (f64::from(height) - delta.y) as i32;
        } else if self.edges.contains(ResizeEdge::BOTTOM) {
            height = (f64::from(height) + delta.y) as i32;
        }

        let bar = self.window.titlebar_height();
        let (min, max) = self.window.size_limits();
        let min_w = min.w.max(160);
        let min_h = min.h.max(1) + bar;
        let max_w = if max.w == 0 { i32::MAX } else { max.w.max(min_w) };
        let max_h = if max.h == 0 { i32::MAX } else { (max.h + bar).max(min_h) };
        self.last_size = Size::from((width.clamp(min_w, max_w), height.clamp(min_h, max_h)));

        let client_size = self.window.client_size_for_frame(self.last_size);
        self.window.resize_client(client_size, true);
    }

    fn button(&mut self, data: &mut Boswas, handle: &mut PointerInnerHandle<'_, Boswas>, event: &ButtonEvent) {
        handle.button(data, event);
        let pressed = handle.current_pressed();
        if pressed.contains(&BTN_LEFT) || pressed.contains(&BTN_RIGHT) {
            return;
        }
        handle.unset_grab(self, data, event.serial, event.time, true);
        if !self.window.alive() {
            return;
        }
        let client_size = self.window.client_size_for_frame(self.last_size);
        self.window.resize_client(client_size, false);
        self.window.data().resize.set(ResizeState::WaitingForLastCommit {
            edges: self.edges,
            initial_rect: self.initial_rect,
        });
    }

    forward_gestures!();

    fn unset(&mut self, _data: &mut Boswas) {}
}

/// Resize bookkeeping kept with the window: resizing from the top or left
/// edge moves the window as the client commits each new size.
#[derive(Default, Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResizeState {
    #[default]
    Idle,
    Resizing {
        edges: ResizeEdge,
        initial_rect: Rectangle<i32, Logical>,
    },
    WaitingForLastCommit {
        edges: ResizeEdge,
        initial_rect: Rectangle<i32, Logical>,
    },
}

impl ResizeState {
    /// Edges and starting rectangle while a resize is in progress; ends the
    /// resize after the commit that follows the button release.
    fn commit(&mut self) -> Option<(ResizeEdge, Rectangle<i32, Logical>)> {
        match *self {
            Self::Resizing { edges, initial_rect } => Some((edges, initial_rect)),
            Self::WaitingForLastCommit { edges, initial_rect } => {
                *self = Self::Idle;
                Some((edges, initial_rect))
            }
            Self::Idle => None,
        }
    }
}

/// Keeps the opposite corner fixed while a window is resized from its top
/// or left edge. Call on every commit of a toplevel surface.
pub fn handle_commit(state: &mut Boswas, surface: &WlSurface) {
    let Some(window) = state.window_for_surface(surface) else {
        return;
    };
    let mut resize = window.data().resize.get();
    let committed = resize.commit();
    window.data().resize.set(resize);
    let Some((edges, initial)) = committed else {
        return;
    };
    if !edges.intersects(ResizeEdge::TOP | ResizeEdge::LEFT) {
        return;
    }
    let Some(mut location) = state.space.element_location(&window) else {
        return;
    };
    let size = window.frame().size;
    if edges.contains(ResizeEdge::LEFT) {
        location.x = initial.loc.x + (initial.size.w - size.w);
    }
    if edges.contains(ResizeEdge::TOP) {
        location.y = initial.loc.y + (initial.size.h - size.h);
    }
    state.move_window(&window, location);
}

#[cfg(test)]
mod tests {
    use super::*;
    use smithay::input::pointer::CursorIcon;

    #[test]
    fn xdg_edges_map_onto_the_bitflags() {
        assert_eq!(ResizeEdge::from(xdg_toplevel::ResizeEdge::TopLeft), ResizeEdge::TOP | ResizeEdge::LEFT);
        assert_eq!(ResizeEdge::from(xdg_toplevel::ResizeEdge::Right), ResizeEdge::RIGHT);
        assert_eq!(ResizeEdge::from(xdg_toplevel::ResizeEdge::None), ResizeEdge::empty());
    }

    #[test]
    fn every_edge_has_a_resize_cursor() {
        assert_eq!((ResizeEdge::TOP | ResizeEdge::LEFT).cursor(), CursorIcon::NwResize);
        assert_eq!((ResizeEdge::BOTTOM | ResizeEdge::RIGHT).cursor(), CursorIcon::SeResize);
        assert_eq!(ResizeEdge::LEFT.cursor(), CursorIcon::WResize);
        assert_eq!(ResizeEdge::BOTTOM.cursor(), CursorIcon::SResize);
        assert_eq!(ResizeEdge::empty().cursor(), CursorIcon::Default);
    }

    #[test]
    fn a_resize_ends_after_the_commit_that_follows_release() {
        let rect = Rectangle::new(Point::from((10, 10)), Size::from((300, 200)));
        let mut state = ResizeState::Resizing {
            edges: ResizeEdge::LEFT,
            initial_rect: rect,
        };
        assert_eq!(state.commit(), Some((ResizeEdge::LEFT, rect)));
        assert_ne!(state, ResizeState::Idle, "still resizing while the button is held");
        state = ResizeState::WaitingForLastCommit {
            edges: ResizeEdge::LEFT,
            initial_rect: rect,
        };
        assert_eq!(state.commit(), Some((ResizeEdge::LEFT, rect)));
        assert_eq!(state, ResizeState::Idle);
        assert_eq!(state.commit(), None);
    }
}
