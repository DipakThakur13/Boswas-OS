//! A toplevel window with its Boswas frame.
//!
//! [`WindowElement`] wraps a Smithay [`Window`]. For windows that asked for
//! server-side decoration the frame adds a title bar above the client's
//! geometry; everything in the [`Space`](smithay::desktop::Space) is
//! addressed by frame geometry, so placement, snapping and hit-testing never
//! need to know whether a window draws its own title bar.

use std::cell::{Cell, RefCell};
use std::time::Duration;

use smithay::{
    backend::renderer::{
        element::{
            memory::MemoryRenderBufferRenderElement,
            solid::SolidColorRenderElement,
            surface::{render_elements_from_surface_tree, WaylandSurfaceRenderElement},
            AsRenderElements, Kind,
        },
        Renderer, Texture,
    },
    desktop::{space::SpaceElement, utils::OutputPresentationFeedback, PopupManager, Window, WindowSurfaceType},
    output::Output,
    reexports::{
        wayland_protocols::wp::presentation_time::server::wp_presentation_feedback,
        wayland_server::protocol::wl_surface::WlSurface,
    },
    render_elements,
    utils::{IsAlive, Logical, Physical, Point, Rectangle, Scale, Size},
    wayland::{
        compositor::{with_states, SurfaceData},
        shell::xdg::{ToplevelSurface, XdgToplevelSurfaceData, XdgToplevelSurfaceRoleAttributes},
    },
};

use crate::{
    animation::WindowAnimation,
    effects::{BoswasRenderer, ClippedSurface},
};

use super::decoration::{is_rounded, Decoration, TITLEBAR_HEIGHT};

/// How a window is arranged on its output.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Tile {
    /// Free-floating; the user chose position and size.
    #[default]
    Floating,
    Maximized,
    Fullscreen,
    Left,
    Right,
    TopLeft,
    TopRight,
    BottomLeft,
    BottomRight,
}

impl Tile {
    /// The part of `area` this arrangement occupies; `None` for floating
    /// windows (and fullscreen, which uses the whole output instead).
    pub fn rect(self, area: Rectangle<i32, Logical>) -> Option<Rectangle<i32, Logical>> {
        let (x, y, w, h) = (area.loc.x, area.loc.y, area.size.w, area.size.h);
        let (hw, hh) = (w / 2, h / 2);
        let rect = |x, y, w, h| Some(Rectangle::new(Point::from((x, y)), Size::from((w, h))));
        match self {
            Tile::Floating | Tile::Fullscreen => None,
            Tile::Maximized => Some(area),
            Tile::Left => rect(x, y, hw, h),
            Tile::Right => rect(x + hw, y, w - hw, h),
            Tile::TopLeft => rect(x, y, hw, hh),
            Tile::TopRight => rect(x + hw, y, w - hw, hh),
            Tile::BottomLeft => rect(x, y + hh, hw, h - hh),
            Tile::BottomRight => rect(x + hw, y + hh, w - hw, h - hh),
        }
    }

    /// The protocol name used on the control socket.
    pub fn name(self) -> &'static str {
        match self {
            Tile::Floating => "floating",
            Tile::Maximized => "maximized",
            Tile::Fullscreen => "fullscreen",
            Tile::Left => "left",
            Tile::Right => "right",
            Tile::TopLeft => "top-left",
            Tile::TopRight => "top-right",
            Tile::BottomLeft => "bottom-left",
            Tile::BottomRight => "bottom-right",
        }
    }
}

/// Per-window state kept by the compositor.
pub struct WindowData {
    /// Stable identifier on the control socket.
    pub id: u64,
    /// Whether the compositor draws the title bar.
    pub ssd: Cell<bool>,
    /// Whether the window has been placed and shown at least once.
    pub mapped: Cell<bool>,
    pub minimized: Cell<bool>,
    /// The window asked for attention while it was not focused.
    pub urgent: Cell<bool>,
    pub workspace: Cell<usize>,
    pub tile: Cell<Tile>,
    /// Frame location on its workspace (kept while the window is hidden).
    pub location: Cell<Point<i32, Logical>>,
    /// Floating frame geometry to return to after maximize, snap or
    /// fullscreen.
    pub restore: Cell<Option<Rectangle<i32, Logical>>>,
    pub decoration: RefCell<Decoration>,
    /// The animation the window is shown with right now (opening, coming
    /// back from the taskbar).
    pub animation: Cell<Option<WindowAnimation>>,
}

/// A toplevel window and its frame.
#[derive(Debug, Clone, PartialEq)]
pub struct WindowElement(pub Window);

impl WindowElement {
    pub fn new(toplevel: ToplevelSurface, id: u64, workspace: usize) -> Self {
        let window = Window::new_wayland_window(toplevel);
        window.user_data().insert_if_missing(|| WindowData {
            id,
            ssd: Cell::new(false),
            mapped: Cell::new(false),
            minimized: Cell::new(false),
            urgent: Cell::new(false),
            workspace: Cell::new(workspace),
            tile: Cell::new(Tile::Floating),
            location: Cell::new(Point::default()),
            restore: Cell::new(None),
            decoration: RefCell::new(Decoration::default()),
            animation: Cell::new(None),
        });
        Self(window)
    }

    pub fn data(&self) -> &WindowData {
        self.0.user_data().get::<WindowData>().expect("every WindowElement carries WindowData")
    }

    pub fn id(&self) -> u64 {
        self.data().id
    }

    pub fn toplevel(&self) -> &ToplevelSurface {
        self.0.toplevel().expect("Boswas windows are xdg toplevels")
    }

    pub fn wl_surface(&self) -> &WlSurface {
        self.toplevel().wl_surface()
    }

    /// Whether this is a window of an X11 application (through XWayland).
    pub fn is_x11(&self) -> bool {
        self.0.toplevel().is_none()
    }

    /// Height of the compositor-drawn title bar (0 for client-decorated and
    /// fullscreen windows).
    pub fn titlebar_height(&self) -> i32 {
        let data = self.data();
        if data.ssd.get() && data.tile.get() != Tile::Fullscreen {
            TITLEBAR_HEIGHT
        } else {
            0
        }
    }

    /// The client size that makes the frame exactly `frame` large.
    pub fn client_size_for_frame(&self, frame: Size<i32, Logical>) -> Size<i32, Logical> {
        Size::from((frame.w.max(1), (frame.h - self.titlebar_height()).max(1)))
    }

    pub fn title(&self) -> String {
        self.toplevel_attribute(|data| data.title.clone())
    }

    pub fn app_id(&self) -> String {
        self.toplevel_attribute(|data| data.app_id.clone())
    }

    fn toplevel_attribute(&self, f: impl FnOnce(&XdgToplevelSurfaceRoleAttributes) -> Option<String>) -> String {
        with_states(self.wl_surface(), |states| {
            states
                .data_map
                .get::<XdgToplevelSurfaceData>()
                .and_then(|data| data.lock().ok().and_then(|data| f(&data)))
        })
        .unwrap_or_default()
    }

    /// The frame rectangle in element coordinates (relative to the point
    /// the [`Space`](smithay::desktop::Space) renders this element at).
    pub fn frame(&self) -> Rectangle<i32, Logical> {
        SpaceElement::geometry(self)
    }

    /// The client surface (or one of its popups and subsurfaces) under a
    /// point in element coordinates, with that surface's origin in the same
    /// coordinates. `None` over the title bar.
    pub fn surface_under(&self, point: Point<f64, Logical>) -> Option<(WlSurface, Point<f64, Logical>)> {
        let offset = Point::from((0.0, f64::from(self.titlebar_height())));
        self.0
            .surface_under(point - offset, WindowSurfaceType::ALL)
            .map(|(surface, loc)| (surface, loc.to_f64() + offset))
    }

    pub fn send_frame<T, F>(&self, output: &Output, time: T, throttle: Option<Duration>, primary: F)
    where
        T: Into<Duration>,
        F: FnMut(&WlSurface, &SurfaceData) -> Option<Output> + Copy,
    {
        self.0.send_frame(output, time, throttle, primary);
    }

    pub fn take_presentation_feedback<F1, F2>(&self, feedback: &mut OutputPresentationFeedback, primary: F1, flags: F2)
    where
        F1: FnMut(&WlSurface, &SurfaceData) -> Option<Output> + Copy,
        F2: FnMut(&WlSurface, &SurfaceData) -> wp_presentation_feedback::Kind + Copy,
    {
        self.0.take_presentation_feedback(feedback, primary, flags);
    }
}

impl IsAlive for WindowElement {
    fn alive(&self) -> bool {
        self.0.alive()
    }
}

impl SpaceElement for WindowElement {
    fn geometry(&self) -> Rectangle<i32, Logical> {
        let mut geo = SpaceElement::geometry(&self.0);
        geo.size.h += self.titlebar_height();
        geo
    }

    fn bbox(&self) -> Rectangle<i32, Logical> {
        let mut bbox = SpaceElement::bbox(&self.0);
        bbox.size.h += self.titlebar_height();
        // Room for the shadow, so damage tracking and output overlap cover it.
        let margin = self.data().decoration.borrow().shadow_margin(self);
        if margin > 0 {
            let mut frame = self.frame();
            frame.loc -= Point::from((margin, margin));
            frame.size += Size::from((2 * margin, 2 * margin));
            bbox = bbox.merge(frame);
        }
        bbox
    }

    fn is_in_input_region(&self, point: &Point<f64, Logical>) -> bool {
        let bar = f64::from(self.titlebar_height());
        if bar > 0.0 {
            let frame = self.frame().to_f64();
            let in_bar = point.x >= frame.loc.x
                && point.x < frame.loc.x + frame.size.w
                && point.y >= frame.loc.y
                && point.y < frame.loc.y + bar;
            if in_bar {
                return true;
            }
        }
        SpaceElement::is_in_input_region(&self.0, &(*point - Point::from((0.0, bar))))
    }

    fn z_index(&self) -> u8 {
        SpaceElement::z_index(&self.0)
    }

    fn set_activate(&self, activated: bool) {
        SpaceElement::set_activate(&self.0, activated);
    }

    fn output_enter(&self, output: &Output, overlap: Rectangle<i32, Logical>) {
        SpaceElement::output_enter(&self.0, output, overlap);
    }

    fn output_leave(&self, output: &Output) {
        SpaceElement::output_leave(&self.0, output);
    }

    fn refresh(&self) {
        SpaceElement::refresh(&self.0);
    }
}

render_elements! {
    pub WindowRenderElement<R> where R: BoswasRenderer;
    Surface=WaylandSurfaceRenderElement<R>,
    Clipped=ClippedSurface<R>,
    Memory=MemoryRenderBufferRenderElement<R>,
    Solid=SolidColorRenderElement,
}

impl<R: Renderer> std::fmt::Debug for WindowRenderElement<R> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Surface(e) => f.debug_tuple("Surface").field(e).finish(),
            Self::Clipped(_) => f.write_str("Clipped"),
            Self::Memory(_) => f.write_str("Memory"),
            Self::Solid(e) => f.debug_tuple("Solid").field(e).finish(),
            Self::_GenericCatcher(_) => f.write_str("_GenericCatcher"),
        }
    }
}

impl WindowElement {
    /// The render elements of the window, front to back: popups, the title
    /// bar and border, the client's content (clipped to the frame's rounded
    /// corners where the renderer can), and with `shadow` the shadow.
    ///
    /// `location` is where the element's own origin is drawn. The frame
    /// starts at the client's window geometry offset; the client's surfaces
    /// are pushed down by the title bar.
    pub fn elements<R>(
        &self,
        renderer: &mut R,
        location: Point<i32, Physical>,
        scale: Scale<f64>,
        alpha: f32,
        shadow: bool,
    ) -> Vec<WindowRenderElement<R>>
    where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let data = self.data();
        let decoration = data.decoration.borrow();
        let frame = self.frame();
        let bar = self.titlebar_height();
        let frame_origin = location + frame.loc.to_physical_precise_round(scale);
        let client_origin = location + Point::<i32, Logical>::from((0, bar)).to_physical_precise_round(scale);
        let mut out: Vec<WindowRenderElement<R>> = Vec::new();

        let surface = self.wl_surface();
        for (popup, popup_offset) in PopupManager::popups_for_surface(surface) {
            let offset: Point<i32, Physical> =
                (self.0.geometry().loc + popup_offset - popup.geometry().loc).to_physical_precise_round(scale);
            out.extend(
                render_elements_from_surface_tree::<_, WaylandSurfaceRenderElement<R>>(
                    renderer,
                    popup.wl_surface(),
                    client_origin + offset,
                    scale,
                    alpha,
                    Kind::Unspecified,
                )
                .into_iter()
                .map(WindowRenderElement::Surface),
            );
        }

        if bar > 0 {
            decoration.foreground_elements(renderer, frame_origin, frame.size, scale, alpha, &mut out);
        }

        let surfaces: Vec<WaylandSurfaceRenderElement<R>> =
            render_elements_from_surface_tree(renderer, surface, client_origin, scale, alpha, Kind::Unspecified);
        if bar > 0 && R::ROUNDS_CORNERS && is_rounded(data.tile.get()) {
            // The client area: the frame below the title bar.
            let origin = frame_origin + Point::<i32, Logical>::from((0, bar)).to_physical_precise_round(scale);
            let size: Size<i32, Physical> =
                Size::<i32, Logical>::from((frame.size.w, frame.size.h - bar)).to_physical_precise_round(scale);
            let rect = Rectangle::new(origin, size).to_f64();
            let radius = (f64::from(boswas_ui::tokens().radius_window) * scale.x) as f32;
            for surface in surfaces {
                out.push(match ClippedSurface::wrap(surface, rect, [0.0, 0.0, radius, radius], scale) {
                    Ok(clipped) => WindowRenderElement::Clipped(clipped),
                    Err(plain) => WindowRenderElement::Surface(plain),
                });
            }
        } else {
            out.extend(surfaces.into_iter().map(WindowRenderElement::Surface));
        }

        if shadow {
            decoration.shadow_elements(renderer, self, frame_origin, frame.size, scale, alpha, &mut out);
        }
        out
    }
}

impl<R> AsRenderElements<R> for WindowElement
where
    R: BoswasRenderer,
    R::TextureId: Clone + Send + Texture + 'static,
{
    type RenderElement = WindowRenderElement<R>;

    fn render_elements<C: From<Self::RenderElement>>(
        &self,
        renderer: &mut R,
        location: Point<i32, Physical>,
        scale: Scale<f64>,
        alpha: f32,
    ) -> Vec<C> {
        self.elements(renderer, location, scale, alpha, true).into_iter().map(C::from).collect()
    }
}
