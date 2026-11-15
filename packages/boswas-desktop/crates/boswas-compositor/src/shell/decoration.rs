//! The Boswas window frame: title bar, window buttons and shadow.
//!
//! Frames are drawn in software with Boswas UI and cached: the title bar is
//! redrawn only when its width, title, focus or hover state changes, and the
//! shadow is one pre-rendered tile cut into eight pieces that are stretched
//! around the window, so a frame costs nothing per composited frame.

use std::cell::OnceCell;

use boswas_ui::{
    text::{TextEngine, TextStyle},
    Canvas, Rgba, Theme,
};
use smithay::{
    backend::{
        allocator::Fourcc,
        renderer::{
            element::{
                memory::{MemoryRenderBuffer, MemoryRenderBufferRenderElement},
                Kind,
            },
            ImportAll, ImportMem, Renderer,
        },
    },
    utils::{Logical, Physical, Point, Rectangle, Scale, Size, Transform},
};

use super::element::{Tile, WindowElement, WindowRenderElement};

/// Height of the title bar in logical pixels.
pub const TITLEBAR_HEIGHT: i32 = 32;
/// Width of one window button.
pub const BUTTON_WIDTH: i32 = 44;
/// Width of the invisible resize border around a floating window.
pub const RESIZE_MARGIN: i32 = 8;
/// How far the shadow reaches beyond the frame.
pub const SHADOW_MARGIN: i32 = 28;
/// Part of each shadow corner piece that lies under the frame. It is larger
/// than the corner radius plus the blur, so the corner pieces end where the
/// shadow has become a straight edge and meet the edge strips seamlessly.
const SHADOW_INSET: i32 = 40;

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
    active: bool,
    hover: Option<Button>,
    tile: Tile,
    light: bool,
}

/// Cached frame artwork of one window.
#[derive(Default)]
pub struct Decoration {
    titlebar: Option<MemoryRenderBuffer>,
    key: Option<TitlebarKey>,
    shadow: Option<[MemoryRenderBuffer; 8]>,
    /// The button under the pointer.
    pub hover: Option<Button>,
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

    /// Redraws the title bar when what it shows has changed. Returns whether
    /// it was redrawn.
    pub fn update(
        &mut self,
        window: &WindowElement,
        active: bool,
        scale: f64,
        text: &mut TextEngine,
        theme: &Theme,
    ) -> bool {
        if window.titlebar_height() == 0 {
            let had = self.titlebar.take().is_some();
            self.key = None;
            return had;
        }
        let key = TitlebarKey {
            width: window.frame().size.w,
            scale: scale.ceil().max(1.0) as i32,
            title: window.title(),
            active,
            hover: self.hover,
            tile: window.data().tile.get(),
            light: theme.scheme == boswas_ui::Scheme::Light,
        };
        if key.width <= 0 {
            return false;
        }
        if self.key.as_ref() == Some(&key) && self.titlebar.is_some() {
            return false;
        }
        self.titlebar = draw_titlebar(&key, text, theme);
        self.key = Some(key);
        true
    }

    /// Appends the frame's render elements, front to back.
    #[allow(clippy::too_many_arguments)]
    pub fn render_elements<R>(
        &self,
        renderer: &mut R,
        window: &WindowElement,
        frame_origin: Point<i32, Physical>,
        frame_size: Size<i32, Logical>,
        scale: Scale<f64>,
        alpha: f32,
        out: &mut Vec<WindowRenderElement<R>>,
    ) where
        R: Renderer + ImportAll + ImportMem,
        R::TextureId: Clone + Send + 'static,
    {
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
            let src = piece_source_size(piece);
            if let Ok(element) = MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                location.to_f64(),
                buffer,
                Some(alpha),
                Some(Rectangle::from_size(src.to_f64())),
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

/// Source size (logical = buffer pixels) of a shadow piece: four corners,
/// then the top, bottom, left and right edge strips.
fn piece_source_size(piece: usize) -> Size<i32, Logical> {
    let k = SHADOW_MARGIN + SHADOW_INSET;
    match piece {
        0..=3 => Size::from((k, k)),
        4 | 5 => Size::from((2, SHADOW_MARGIN)),
        _ => Size::from((SHADOW_MARGIN, 2)),
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
            let elevation = boswas_ui::tokens().elevation("window");
            let mut canvas = Canvas::new(side as f32, side as f32, 1.0).expect("the shadow tile is small");
            let m = SHADOW_MARGIN as f32;
            canvas.shadow(
                m,
                m,
                side as f32 - 2.0 * m,
                side as f32 - 2.0 * m,
                boswas_ui::tokens().radius_window,
                elevation.blur.min(m),
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
    [
        cut(0, 0, k, k),
        cut(side - k, 0, k, k),
        cut(0, side - k, k, k),
        cut(side - k, side - k, k, k),
        cut(k, 0, 2, m),
        cut(k, side - m, 2, m),
        cut(0, k, m, 2),
        cut(side - m, k, m, 2),
    ]
}

fn draw_titlebar(key: &TitlebarKey, text: &mut TextEngine, theme: &Theme) -> Option<MemoryRenderBuffer> {
    let (w, h) = (key.width as f32, TITLEBAR_HEIGHT as f32);
    let mut canvas = Canvas::new(w, h, key.scale as f32)?;
    let radius = if key.tile == Tile::Floating {
        boswas_ui::tokens().radius_window
    } else {
        0.0
    };
    let background = if key.active { theme.surface_raised } else { theme.surface };
    canvas.fill_rounded_rect4(0.0, 0.0, w, h, [radius, radius, 0.0, 0.0], background);
    canvas.fill_rect(0.0, h - 1.0, w, 1.0, theme.border.alpha(0.7));

    let buttons = 3.0 * BUTTON_WIDTH as f32;
    let style = TextStyle::new(13.0, if key.active { theme.text } else { theme.text_muted }).weight(500);
    let room = w - buttons - 24.0;
    if room > 24.0 {
        let line = style.line_height();
        text.draw(&mut canvas, &key.title, 12.0, ((h - line) / 2.0).floor(), Some(room), &style);
    }

    let glyph = if key.active { theme.text_secondary } else { theme.text_muted };
    for (index, button) in [Button::Close, Button::Maximize, Button::Minimize].into_iter().enumerate() {
        let x = w - (index as f32 + 1.0) * BUTTON_WIDTH as f32;
        if x < 0.0 {
            break;
        }
        let hovered = key.hover == Some(button);
        let mut color = glyph;
        if hovered {
            let (fill, ink) = match button {
                Button::Close => (theme.danger, Rgba::WHITE),
                _ => (theme.surface_hover, theme.text),
            };
            let corner = if button == Button::Close { radius } else { 0.0 };
            canvas.fill_rounded_rect4(x, 0.0, BUTTON_WIDTH as f32, h - 1.0, [0.0, corner, 0.0, 0.0], fill);
            color = ink;
        }
        let (cx, cy) = (x + BUTTON_WIDTH as f32 / 2.0, h / 2.0);
        let r = 5.0;
        match button {
            Button::Close => {
                canvas.line(cx - r, cy - r, cx + r, cy + r, 1.4, color);
                canvas.line(cx + r, cy - r, cx - r, cy + r, 1.4, color);
            }
            Button::Maximize if key.tile == Tile::Maximized => {
                // Restore: two overlapping squares.
                canvas.stroke_rounded_rect(cx - r, cy - r + 2.5, 2.0 * r - 2.5, 2.0 * r - 2.5, 1.5, 1.3, color);
                canvas.polyline(
                    &[(cx - r + 2.5, cy - r), (cx + r, cy - r), (cx + r, cy + r - 2.5)],
                    1.3,
                    color,
                );
            }
            Button::Maximize => canvas.stroke_rounded_rect(cx - r, cy - r, 2.0 * r, 2.0 * r, 1.5, 1.3, color),
            Button::Minimize => canvas.line(cx - r, cy, cx + r, cy, 1.4, color),
        }
    }

    let (pw, ph) = canvas.pixel_size();
    let mut bytes = vec![0u8; (pw * ph * 4) as usize];
    canvas.write_argb8888(&mut bytes);
    Some(MemoryRenderBuffer::from_slice(
        &bytes,
        Fourcc::Argb8888,
        (pw as i32, ph as i32),
        key.scale,
        Transform::Normal,
        None,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn buttons_are_hit_from_the_right_edge() {
        let at = |x: f64, y: f64| Decoration::button_at(400, Point::from((x, y)));
        assert_eq!(at(399.0, 10.0), Some(Button::Close));
        assert_eq!(at(357.0, 10.0), Some(Button::Close));
        assert_eq!(at(355.0, 10.0), Some(Button::Maximize));
        assert_eq!(at(311.0, 31.0), Some(Button::Minimize));
        assert_eq!(at(200.0, 10.0), None);
        assert_eq!(at(399.0, 32.0), None, "below the title bar");
        assert_eq!(at(399.0, -1.0), None);
        assert_eq!(at(400.0, 10.0), None, "outside the frame");
    }

    #[test]
    fn narrow_windows_keep_their_close_button() {
        assert_eq!(Decoration::button_at(50, Point::from((30.0, 5.0))), Some(Button::Close));
        assert_eq!(Decoration::button_at(50, Point::from((3.0, 5.0))), Some(Button::Maximize));
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
        for piece in 0..8 {
            let size = piece_source_size(piece);
            assert!(size.w > 0 && size.h > 0);
        }
    }

    #[test]
    fn titlebar_is_rendered_at_the_requested_scale() {
        let mut text = TextEngine::new();
        let key = TitlebarKey {
            width: 300,
            scale: 2,
            title: "Boswas".into(),
            active: true,
            hover: Some(Button::Close),
            tile: Tile::Floating,
            light: false,
        };
        assert!(draw_titlebar(&key, &mut text, Theme::dark()).is_some());
        let zero = TitlebarKey { width: 0, ..key };
        assert!(draw_titlebar(&zero, &mut text, Theme::dark()).is_none());
    }
}
