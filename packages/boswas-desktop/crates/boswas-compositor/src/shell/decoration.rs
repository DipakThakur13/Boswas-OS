//! The Boswas window frame: title bar, caption buttons, border and shadow.
//!
//! The frame follows the Fluent window: a 32 px title bar in the window's
//! base colour with the application's icon and title on the left and three
//! 46 px caption buttons on the right, a one-pixel translucent border, 8 px
//! rounded corners and a soft shadow.
//!
//! Frames are drawn in software with Boswas UI and cached: the title bar is
//! redrawn only when its width, title, focus or button state changes, the
//! border only when the window's size changes, and the shadow is one
//! pre-rendered tile cut into eight pieces that are stretched around the
//! window, so a frame costs nothing per composited frame.

use std::{cell::OnceCell, rc::Rc};

use boswas_ui::{
    text::{TextEngine, TextStyle},
    tokens, Canvas, Rgba, Scheme, Theme,
};
use smithay::{
    backend::{
        allocator::Fourcc,
        renderer::{
            element::{
                memory::{MemoryRenderBuffer, MemoryRenderBufferRenderElement},
                solid::{SolidColorBuffer, SolidColorRenderElement},
                Kind,
            },
            Color32F, Texture,
        },
    },
    utils::{Logical, Physical, Point, Rectangle, Scale, Size, Transform},
};
use tiny_skia::Pixmap;

use super::element::{Tile, WindowElement, WindowRenderElement};
use crate::effects::BoswasRenderer;

/// Height of the title bar in logical pixels.
pub const TITLEBAR_HEIGHT: i32 = 32;
/// Width of one caption button.
pub const BUTTON_WIDTH: i32 = 46;
/// Width of the invisible resize border around a floating window.
pub const RESIZE_MARGIN: i32 = 8;
/// How far the shadow reaches beyond the frame.
pub const SHADOW_MARGIN: i32 = 44;
/// Part of each shadow corner piece that lies under the frame. It is larger
/// than the corner radius plus the blur, so the corner pieces end where the
/// shadow has become a straight edge and meet the edge strips seamlessly.
const SHADOW_INSET: i32 = 56;
/// Height of the strip that carries the bottom border and its corners.
const BOTTOM_STRIP: i32 = 10;
/// Size of the application icon in the title bar.
pub const ICON_SIZE: u32 = 16;

/// A button in the title bar.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Button {
    Minimize,
    Maximize,
    Close,
}

/// What the cached title bar was drawn for.
#[derive(Clone, Debug, PartialEq)]
struct TitlebarKey {
    width: i32,
    scale: i32,
    title: String,
    app_id: String,
    active: bool,
    hover: Option<Button>,
    pressed: Option<Button>,
    tile: Tile,
    light: bool,
}

/// What the cached border strip was drawn for.
#[derive(Clone, Debug, PartialEq)]
struct BorderKey {
    size: Size<i32, Logical>,
    scale: i32,
    rounded: bool,
    light: bool,
}

/// The border below the title bar: two side lines and the bottom strip
/// with the lower corners.
struct Border {
    key: BorderKey,
    bottom: MemoryRenderBuffer,
    side: SolidColorBuffer,
}

/// Cached frame artwork of one window.
#[derive(Default)]
pub struct Decoration {
    titlebar: Option<MemoryRenderBuffer>,
    key: Option<TitlebarKey>,
    border: Option<Border>,
    shadow: Option<[MemoryRenderBuffer; 8]>,
    /// The button under the pointer.
    pub hover: Option<Button>,
    /// The button the pointer was pressed on (and is still over).
    pub pressed: Option<Button>,
}

/// Whether a window in this arrangement has rounded corners: everything
/// except maximized and fullscreen windows, which fill the screen.
pub fn is_rounded(tile: Tile) -> bool {
    !matches!(tile, Tile::Maximized | Tile::Fullscreen)
}

impl Decoration {
    /// The shadow margin of `window`: floating, compositor-decorated windows
    /// have a shadow; tiled, maximized, fullscreen and client-decorated
    /// windows (which bring their own) do not.
    pub fn shadow_margin(&self, window: &WindowElement) -> i32 {
        let data = window.data();
        if data.ssd.get() && data.tile.get() == Tile::Floating {
            SHADOW_MARGIN
        } else {
            0
        }
    }

    /// The button at a point relative to the frame's top-left corner.
    pub fn button_at(frame_width: i32, point: Point<f64, Logical>) -> Option<Button> {
        if point.y < 0.0 || point.y >= f64::from(TITLEBAR_HEIGHT) {
            return None;
        }
        let from_right = f64::from(frame_width) - point.x;
        if from_right <= 0.0 {
            return None;
        }
        match (from_right / f64::from(BUTTON_WIDTH)) as i32 {
            0 => Some(Button::Close),
            1 => Some(Button::Maximize),
            2 => Some(Button::Minimize),
            _ => None,
        }
    }

    /// The rectangle of a caption button relative to the frame's top-left
    /// corner.
    pub fn button_rect(frame_width: i32, button: Button) -> Rectangle<i32, Logical> {
        let index = match button {
            Button::Close => 1,
            Button::Maximize => 2,
            Button::Minimize => 3,
        };
        Rectangle::new(
            Point::from((frame_width - index * BUTTON_WIDTH, 0)),
            Size::from((BUTTON_WIDTH, TITLEBAR_HEIGHT)),
        )
    }

    /// Redraws the frame's artwork when what it shows has changed. `rounds`
    /// says whether the renderer clips the client's lower corners, so the
    /// border follows the same curve. Returns whether anything was redrawn.
    #[allow(clippy::too_many_arguments)]
    pub fn update(
        &mut self,
        window: &WindowElement,
        active: bool,
        scale: f64,
        rounds: bool,
        icon: Option<Rc<Pixmap>>,
        text: &mut TextEngine,
        theme: &Theme,
    ) -> bool {
        if window.titlebar_height() == 0 {
            let had = self.titlebar.take().is_some() || self.border.take().is_some();
            self.key = None;
            return had;
        }
        let frame = window.frame().size;
        if frame.w <= 0 || frame.h <= 0 {
            return false;
        }
        let pixel_scale = scale.ceil().max(1.0) as i32;
        let tile = window.data().tile.get();
        let light = theme.scheme == Scheme::Light;
        let mut redrawn = false;

        let key = TitlebarKey {
            width: frame.w,
            scale: pixel_scale,
            title: window.title(),
            app_id: window.app_id(),
            active,
            hover: self.hover,
            pressed: self.pressed,
            tile,
            light,
        };
        if self.key.as_ref() != Some(&key) || self.titlebar.is_none() {
            self.titlebar = draw_titlebar(&key, icon.as_deref(), text, theme);
            self.key = Some(key);
            redrawn = true;
        }

        let border_key = BorderKey {
            size: frame,
            scale: pixel_scale,
            rounded: rounds && is_rounded(tile),
            light,
        };
        if self.border.as_ref().map(|b| &b.key) != Some(&border_key) {
            self.border = draw_border(border_key, theme);
            redrawn = true;
        }
        redrawn
    }

    /// Appends the frame's parts that lie in front of the client's content:
    /// the title bar and the border.
    pub fn foreground_elements<R>(
        &self,
        renderer: &mut R,
        frame_origin: Point<i32, Physical>,
        frame_size: Size<i32, Logical>,
        scale: Scale<f64>,
        alpha: f32,
        out: &mut Vec<WindowRenderElement<R>>,
    ) where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let at = |x: i32, y: i32| frame_origin + Point::<i32, Logical>::from((x, y)).to_physical_precise_round(scale);
        if let Some(border) = &self.border {
            let strip_y = frame_size.h - BOTTOM_STRIP;
            if strip_y >= TITLEBAR_HEIGHT {
                if let Ok(element) = MemoryRenderBufferRenderElement::from_buffer(
                    renderer,
                    at(0, strip_y).to_f64(),
                    &border.bottom,
                    Some(alpha),
                    None,
                    Some(Size::from((frame_size.w, BOTTOM_STRIP))),
                    Kind::Unspecified,
                ) {
                    out.push(WindowRenderElement::Memory(element));
                }
                for x in [0, frame_size.w - 1] {
                    out.push(WindowRenderElement::Solid(SolidColorRenderElement::from_buffer(
                        &border.side,
                        at(x, TITLEBAR_HEIGHT),
                        scale,
                        alpha,
                        Kind::Unspecified,
                    )));
                }
            }
        }
        if let Some(titlebar) = &self.titlebar {
            if let Ok(element) = MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                frame_origin.to_f64(),
                titlebar,
                Some(alpha),
                None,
                Some(Size::from((frame_size.w, TITLEBAR_HEIGHT))),
                Kind::Unspecified,
            ) {
                out.push(WindowRenderElement::Memory(element));
            }
        }
    }

    /// Appends the shadow, which lies behind everything else of the window.
    pub fn shadow_elements<R>(
        &self,
        renderer: &mut R,
        window: &WindowElement,
        frame_origin: Point<i32, Physical>,
        frame_size: Size<i32, Logical>,
        scale: Scale<f64>,
        alpha: f32,
        out: &mut Vec<WindowRenderElement<R>>,
    ) where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let margin = self.shadow_margin(window);
        let (Some(pieces), true) = (
            &self.shadow,
            margin > 0 && frame_size.w >= 2 * SHADOW_INSET && frame_size.h >= 2 * SHADOW_INSET,
        ) else {
            return;
        };
        let (w, h) = (frame_size.w, frame_size.h);
        let (m, i) = (SHADOW_MARGIN, SHADOW_INSET);
        let k = m + i;
        // (piece, x, y, width, height) relative to the frame, in the order
        // of `shadow_pieces`.
        let layout = [
            (0, -m, -m, k, k),
            (1, w - i, -m, k, k),
            (2, -m, h - i, k, k),
            (3, w - i, h - i, k, k),
            (4, i, -m, w - 2 * i, m),
            (5, i, h, w - 2 * i, m),
            (6, -m, i, m, h - 2 * i),
            (7, w, i, m, h - 2 * i),
        ];
        for (piece, x, y, pw, ph) in layout {
            if pw <= 0 || ph <= 0 {
                continue;
            }
            let location = frame_origin + Point::<i32, Logical>::from((x, y)).to_physical_precise_round(scale);
            let buffer = &pieces[piece];
            if let Ok(element) = MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                location.to_f64(),
                buffer,
                Some(alpha),
                Some(piece_source(piece).to_f64()),
                Some(Size::from((pw, ph))),
                Kind::Unspecified,
            ) {
                out.push(WindowRenderElement::Memory(element));
            }
        }
    }

    /// Creates this window's shadow pieces on first use.
    pub fn ensure_shadow(&mut self, window: &WindowElement) {
        if self.shadow.is_none() && self.shadow_margin(window) > 0 {
            self.shadow = Some(shadow_pieces());
        }
    }
}

/// Extra pixels each shadow piece's buffer has around the part that is
/// drawn. A piece is sampled with filtering when it is stretched or drawn
/// at a fractional scale; without neighbours to sample, its edges would
/// fade and show as seams between the pieces.
const APRON: i32 = 2;

/// The part of a shadow piece's buffer that is drawn (buffer pixels): four
/// corners, then the top, bottom, left and right edge strips. See
/// `shadow_pieces` for how the buffers are cut.
fn piece_source(piece: usize) -> Rectangle<i32, Logical> {
    let k = SHADOW_MARGIN + SHADOW_INSET;
    let m = SHADOW_MARGIN;
    let a = APRON;
    let rect = |x, y, w, h| Rectangle::new(Point::from((x, y)), Size::from((w, h)));
    match piece {
        0 => rect(0, 0, k, k),
        1 => rect(a, 0, k, k),
        2 => rect(0, a, k, k),
        3 => rect(a, a, k, k),
        4 => rect(a, 0, 2, m),
        5 => rect(a, a, 2, m),
        6 => rect(0, a, m, 2),
        _ => rect(a, a, m, 2),
    }
}

thread_local! {
    /// The shadow tile, rendered once: premultiplied ARGB8888 bytes and its
    /// side length in pixels.
    static SHADOW_TILE: OnceCell<(Vec<u8>, i32)> = const { OnceCell::new() };
}

fn shadow_tile() -> (Vec<u8>, i32) {
    SHADOW_TILE.with(|cell| {
        cell.get_or_init(|| {
            let k = SHADOW_MARGIN + SHADOW_INSET;
            let side = 2 * k + 2;
            let elevation = tokens().elevation("window");
            let mut canvas = Canvas::new(side as f32, side as f32, 1.0).expect("the shadow tile is small");
            let m = SHADOW_MARGIN as f32;
            canvas.shadow(
                m,
                m,
                side as f32 - 2.0 * m,
                side as f32 - 2.0 * m,
                tokens().radius_window,
                elevation.blur.min(m - elevation.y.min(m / 3.0)),
                elevation.y.min(m / 3.0),
                elevation.alpha,
            );
            let mut bytes = vec![0u8; (side * side * 4) as usize];
            canvas.write_argb8888(&mut bytes);
            (bytes, side)
        })
        .clone()
    })
}

/// Cuts the shadow tile into the eight pieces of one window.
fn shadow_pieces() -> [MemoryRenderBuffer; 8] {
    let (tile, side) = shadow_tile();
    let k = SHADOW_MARGIN + SHADOW_INSET;
    let m = SHADOW_MARGIN;
    let cut = |x: i32, y: i32, w: i32, h: i32| {
        let mut bytes = Vec::with_capacity((w * h * 4) as usize);
        for row in y..y + h {
            let start = ((row * side + x) * 4) as usize;
            bytes.extend_from_slice(&tile[start..start + (w * 4) as usize]);
        }
        MemoryRenderBuffer::from_slice(&bytes, Fourcc::Argb8888, (w, h), 1, Transform::Normal, None)
    };
    // Each piece is cut with an apron towards the middle of the tile (the
    // outer edges of the tile are transparent anyway).
    let a = APRON;
    [
        cut(0, 0, k + a, k + a),
        cut(side - k - a, 0, k + a, k + a),
        cut(0, side - k - a, k + a, k + a),
        cut(side - k - a, side - k - a, k + a, k + a),
        cut(k - a, 0, 2 + 2 * a, m + a),
        cut(k - a, side - m - a, 2 + 2 * a, m + a),
        cut(0, k - a, m + a, 2 + 2 * a),
        cut(side - m - a, k - a, m + a, 2 + 2 * a),
    ]
}

fn buffer_from(canvas: &Canvas, scale: i32) -> MemoryRenderBuffer {
    let (pw, ph) = canvas.pixel_size();
    let mut bytes = vec![0u8; (pw * ph * 4) as usize];
    canvas.write_argb8888(&mut bytes);
    MemoryRenderBuffer::from_slice(&bytes, Fourcc::Argb8888, (pw as i32, ph as i32), scale, Transform::Normal, None)
}

fn draw_titlebar(
    key: &TitlebarKey,
    icon: Option<&Pixmap>,
    text: &mut TextEngine,
    theme: &Theme,
) -> Option<MemoryRenderBuffer> {
    let t = tokens();
    let (w, h) = (key.width as f32, TITLEBAR_HEIGHT as f32);
    let mut canvas = Canvas::new(w, h, key.scale as f32)?;
    let radius = if is_rounded(key.tile) { t.radius_window } else { 0.0 };
    canvas.fill_rounded_rect4(0.0, 0.0, w, h, [radius, radius, 0.0, 0.0], theme.background);

    // Icon and title.
    let buttons = 3.0 * BUTTON_WIDTH as f32;
    let caption = t.type_style("caption");
    let ink = if key.active { theme.text } else { theme.text_muted };
    let mut x = 16.0;
    if let Some(icon) = icon {
        let size = ICON_SIZE as f32;
        canvas.draw_pixmap(icon, x, (h - size) / 2.0, size, size, if key.active { 1.0 } else { 0.6 });
        x += size + 12.0;
    }
    let room = w - buttons - x - 8.0;
    if room > 24.0 {
        let style = TextStyle::new(caption.size, ink).weight(caption.weight);
        let line = style.line_height();
        text.draw(&mut canvas, &key.title, x, ((h - line) / 2.0).floor(), Some(room), &style);
    }

    // Caption buttons: minimize, maximize or restore, close.
    for (index, button) in [Button::Close, Button::Maximize, Button::Minimize].into_iter().enumerate() {
        let x = w - (index as f32 + 1.0) * BUTTON_WIDTH as f32;
        if x < 0.0 {
            break;
        }
        let pressed = key.pressed == Some(button);
        let hovered = key.hover == Some(button);
        let mut color = ink;
        let fill = match (button, pressed, hovered) {
            (Button::Close, true, _) => Some(t.shell_color("caption-close-pressed")),
            (Button::Close, _, true) => Some(t.shell_color("caption-close-hover")),
            (_, true, _) => Some(t.fill(theme.scheme, "subtle-pressed")),
            (_, _, true) => Some(t.fill(theme.scheme, "subtle-hover")),
            _ => None,
        };
        if let Some(fill) = fill {
            let corner = if button == Button::Close { radius } else { 0.0 };
            canvas.fill_rounded_rect4(x, 0.0, BUTTON_WIDTH as f32, h, [0.0, corner, 0.0, 0.0], fill);
            if button == Button::Close {
                color = Rgba::WHITE;
            } else {
                color = theme.text;
            }
        }
        // Glyphs are 10 px wide, drawn on pixel centres so that their one
        // pixel lines stay sharp.
        let (cx, cy) = ((x + BUTTON_WIDTH as f32 / 2.0).floor() + 0.5, (h / 2.0).floor() + 0.5);
        let r = 5.0;
        match button {
            Button::Close => {
                canvas.line(cx - r, cy - r, cx + r, cy + r, 1.0, color);
                canvas.line(cx + r, cy - r, cx - r, cy + r, 1.0, color);
            }
            Button::Maximize if key.tile == Tile::Maximized => {
                // Restore: a window in front of another.
                canvas.stroke_rounded_rect(cx - r, cy - r + 2.0, 2.0 * r - 2.0, 2.0 * r - 2.0, 1.5, 1.0, color);
                canvas.polyline(
                    &[(cx - r + 2.0, cy - r + 2.0), (cx - r + 2.0, cy - r), (cx + r, cy - r), (cx + r, cy + r - 2.0), (cx + r - 2.0, cy + r - 2.0)],
                    1.0,
                    color,
                );
            }
            Button::Maximize => canvas.stroke_rounded_rect(cx - r, cy - r, 2.0 * r, 2.0 * r, 1.5, 1.0, color),
            Button::Minimize => canvas.line(cx - r, cy, cx + r, cy, 1.0, color),
        }
    }

    // The border: the top edge and the upper part of both sides. Drawn as a
    // rounded rectangle that continues below the bar, where it is cut off.
    let stroke = t.stroke(theme.scheme, "window");
    canvas.stroke_rounded_rect(0.5, 0.5, w - 1.0, h + radius + 8.0, (radius - 0.5).max(0.0), 1.0, stroke);
    Some(buffer_from(&canvas, key.scale))
}

/// Draws the bottom strip of the border (the lower corners and the bottom
/// edge) and makes the side lines.
fn draw_border(key: BorderKey, theme: &Theme) -> Option<Border> {
    let t = tokens();
    let stroke = t.stroke(theme.scheme, "window");
    let (w, h) = (key.size.w as f32, BOTTOM_STRIP as f32);
    let mut canvas = Canvas::new(w, h, key.scale as f32)?;
    let radius = if key.rounded { t.radius_window } else { 0.0 };
    // A rounded rectangle that starts above the strip: only its lower part
    // is inside.
    let above = radius + 8.0;
    canvas.stroke_rounded_rect(0.5, -above, w - 1.0, above + h - 0.5, (radius - 0.5).max(0.0), 1.0, stroke);
    let bottom = buffer_from(&canvas, key.scale);

    let side_height = key.size.h - TITLEBAR_HEIGHT - BOTTOM_STRIP;
    let mut side = SolidColorBuffer::default();
    side.update(
        Size::from((1, side_height.max(0))),
        Color32F::from(stroke.to_premultiplied_f32()),
    );
    Some(Border { key, bottom, side })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn buttons_are_hit_from_the_right_edge() {
        let at = |x: f64, y: f64| Decoration::button_at(400, Point::from((x, y)));
        assert_eq!(at(399.0, 10.0), Some(Button::Close));
        assert_eq!(at(355.0, 10.0), Some(Button::Close));
        assert_eq!(at(353.0, 10.0), Some(Button::Maximize));
        assert_eq!(at(307.0, 31.0), Some(Button::Minimize));
        assert_eq!(at(200.0, 10.0), None);
        assert_eq!(at(399.0, 32.0), None, "below the title bar");
        assert_eq!(at(399.0, -1.0), None);
        assert_eq!(at(400.0, 10.0), None, "outside the frame");
    }

    #[test]
    fn button_rectangles_match_the_hit_test() {
        for button in [Button::Close, Button::Maximize, Button::Minimize] {
            let rect = Decoration::button_rect(400, button);
            assert_eq!(rect.size, Size::from((BUTTON_WIDTH, TITLEBAR_HEIGHT)));
            let centre = Point::from((f64::from(rect.loc.x) + 23.0, 16.0));
            assert_eq!(Decoration::button_at(400, centre), Some(button));
        }
        assert_eq!(Decoration::button_rect(400, Button::Close).loc.x, 354);
    }

    #[test]
    fn narrow_windows_keep_their_close_button() {
        assert_eq!(Decoration::button_at(50, Point::from((30.0, 5.0))), Some(Button::Close));
        assert_eq!(Decoration::button_at(50, Point::from((3.0, 5.0))), Some(Button::Maximize));
    }

    #[test]
    fn the_geometry_is_the_fluent_window() {
        let t = tokens();
        assert_eq!(TITLEBAR_HEIGHT as f32, t.shell("titlebar-height"));
        assert_eq!((BUTTON_WIDTH as f32, TITLEBAR_HEIGHT as f32), t.shell_size("caption-button"));
        assert_eq!(RESIZE_MARGIN as f32, t.shell("resize-margin"));
        let elevation = t.elevation("window");
        assert!(elevation.blur + elevation.y <= SHADOW_MARGIN as f32, "the shadow fits its margin");
        assert!(SHADOW_INSET as f32 > t.radius_window + elevation.blur);
    }

    #[test]
    fn only_windows_that_fill_the_screen_have_square_corners() {
        assert!(is_rounded(Tile::Floating) && is_rounded(Tile::Left) && is_rounded(Tile::TopRight));
        assert!(!is_rounded(Tile::Maximized) && !is_rounded(Tile::Fullscreen));
    }

    #[test]
    fn shadow_tile_is_cut_into_eight_consistent_pieces() {
        let (tile, side) = shadow_tile();
        assert_eq!(tile.len(), (side * side * 4) as usize);
        let alpha = |x: i32, y: i32| tile[((y * side + x) * 4 + 3) as usize];
        assert_eq!(alpha(0, 0), 0, "the tile's corner is beyond the blur");
        assert!(alpha(side / 2, side / 2) > 0, "the middle is shadowed");
        assert!(alpha(side / 2, side - 4) >= alpha(side / 2, 3), "the shadow falls downwards");
        let pieces = shadow_pieces();
        assert_eq!(pieces.len(), 8);
        let k = SHADOW_MARGIN + SHADOW_INSET;
        for piece in 0..8 {
            let source = piece_source(piece);
            assert!(source.size.w > 0 && source.size.h > 0);
            assert!(source.loc.x >= 0 && source.loc.y >= 0);
            let expected = match piece {
                0..=3 => Size::from((k, k)),
                4 | 5 => Size::from((2, SHADOW_MARGIN)),
                _ => Size::from((SHADOW_MARGIN, 2)),
            };
            assert_eq!(source.size, expected, "piece {piece}");
        }
    }

    #[test]
    fn titlebar_is_rendered_at_the_requested_scale() {
        let mut text = TextEngine::new();
        let key = TitlebarKey {
            width: 300,
            scale: 2,
            title: "Boswas".into(),
            app_id: "com.boswas.test".into(),
            active: true,
            hover: Some(Button::Close),
            pressed: None,
            tile: Tile::Floating,
            light: false,
        };
        assert!(draw_titlebar(&key, None, &mut text, Theme::dark()).is_some());
        let pressed = TitlebarKey { pressed: Some(Button::Maximize), tile: Tile::Maximized, ..key.clone() };
        assert!(draw_titlebar(&pressed, None, &mut text, Theme::light()).is_some());
        let zero = TitlebarKey { width: 0, ..key };
        assert!(draw_titlebar(&zero, None, &mut text, Theme::dark()).is_none());
    }

    #[test]
    fn the_border_is_made_for_rounded_and_square_frames() {
        for rounded in [true, false] {
            let key = BorderKey {
                size: Size::from((320, 240)),
                scale: 1,
                rounded,
                light: false,
            };
            let border = draw_border(key.clone(), Theme::dark()).unwrap();
            assert_eq!(border.key, key);
        }
    }
}
