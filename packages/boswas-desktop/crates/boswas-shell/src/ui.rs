//! Immediate-mode drawing shared by every shell surface.
//!
//! A surface is redrawn completely when its model changes. While it is
//! drawn, controls register the rectangles that react to the pointer or the
//! keyboard together with the [`Action`] they trigger; input is resolved
//! against that list. Colours, sizes and motion come from the Boswas UI
//! tokens (Fluent geometry: 4 px controls, 8 px overlays, translucent state
//! layers and strokes).

use std::{
    cell::Cell,
    rc::Rc,
    time::{Duration, Instant},
};

use boswas_ui::{
    glass::{self as glass, Glass, Kind as GlassKind, Source as GlassSource},
    icons::IconLoader,
    material::Backdrop,
    text::{TextEngine, TextStyle},
    tokens::{cubic_bezier, tokens},
    Canvas, Rgba, Scheme, Theme,
};
use smithay_client_toolkit::seat::keyboard::Keysym;
use tiny_skia::Pixmap;

use crate::{action::Action, glyph::Glyph};

/// A rectangle in logical pixels.
#[derive(Clone, Copy, Debug, PartialEq, Default)]
pub struct Rect {
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
}

impl Rect {
    pub const fn new(x: f32, y: f32, w: f32, h: f32) -> Self {
        Self { x, y, w, h }
    }

    pub fn contains(&self, point: (f32, f32)) -> bool {
        point.0 >= self.x && point.0 < self.x + self.w && point.1 >= self.y && point.1 < self.y + self.h
    }

    pub fn inset(&self, by: f32) -> Self {
        Self::new(self.x + by, self.y + by, (self.w - 2.0 * by).max(0.0), (self.h - 2.0 * by).max(0.0))
    }

    pub fn right(&self) -> f32 {
        self.x + self.w
    }

    pub fn bottom(&self) -> f32 {
        self.y + self.h
    }

    pub fn center(&self) -> (f32, f32) {
        (self.x + self.w / 2.0, self.y + self.h / 2.0)
    }

}

/// Shared resources of the shell: fonts, icons, the theme and the blurred
/// wallpaper the materials are drawn from.
pub struct Resources {
    pub text: TextEngine,
    pub icons: IconLoader,
    pub theme: &'static Theme,
    /// The blurred wallpaper of the output being drawn, when known.
    pub backdrop: Option<Backdrop>,
    /// What `backdrop` was made for: output size in device pixels.
    pub backdrop_key: (u32, u32),
    /// Durations are zero: nothing slides or fades.
    pub reduced_motion: bool,
    /// Materials show the blurred wallpaper through their tint; without
    /// transparency they are the tint alone.
    pub transparency: bool,
    /// Counts the changes of `backdrop` (a panel drawn on the old one is
    /// not reused).
    pub backdrop_generation: u64,
    /// Overlay panels as drawn last (shadow, material, outline), reused
    /// while nothing they depend on changed: a flyout that is drawn frame
    /// by frame (Start's page scrolling) does not blur its shadow and its
    /// material again each time.
    panels: Vec<(PanelKey, Kept)>,
    /// The compositor blurs what lies behind the shell's glass (glass.rs):
    /// glass is then only its tint, grain, glow and hairline.
    pub live_blur: bool,
    /// What the glass of the surface being drawn covers (its blur region
    /// with live blur): rectangles and their corner radius.
    pub glass_regions: Vec<(Rect, f32)>,
    /// Cells whose magnified icon is still moving (magnify.rs): drawn
    /// again on the next frame.
    pub magnifying: Vec<Rect>,
}

/// A panel as kept: drawn on the whole canvas, and the part of that
/// picture it covers (with its place on the canvas, in device pixels).
#[derive(Clone)]
struct Kept {
    whole: Rc<Pixmap>,
    part: Option<(i32, i32, Rc<Pixmap>)>,
}

/// What an overlay panel's look depends on.
#[derive(Clone, Debug, PartialEq)]
struct PanelKey {
    canvas: (u32, u32),
    scale: u32,
    rect: [u32; 4],
    origin: [u32; 2],
    material: String,
    theme: usize,
    transparency: bool,
    backdrop: Option<u64>,
    live: bool,
}

/// Panels kept by [`Resources`]: Start, quick settings, the two of the
/// notification centre, and two more (menus, previews).
const MAX_PANELS: usize = 6;

impl Resources {
    pub fn new(theme: &'static Theme) -> Self {
        Self::with_text(theme, TextEngine::new())
    }

    /// The resources with a given text engine instead of the system's
    /// fonts.
    pub fn with_text(theme: &'static Theme, text: TextEngine) -> Self {
        let icon_theme = match theme.scheme {
            Scheme::Dark => "Boswas-Prism",
            Scheme::Light => "Boswas-Prism-Light",
        };
        Self {
            text,
            icons: IconLoader::new(icon_theme),
            theme,
            backdrop: None,
            backdrop_key: (0, 0),
            reduced_motion: false,
            transparency: true,
            backdrop_generation: 0,
            panels: Vec::new(),
            live_blur: false,
            glass_regions: Vec::new(),
            magnifying: Vec::new(),
        }
    }

    /// A one-pixel outline colour (`control`, `surface`, `divider`, `window`).
    pub fn stroke(&self, kind: &str) -> Rgba {
        tokens().stroke(self.theme.scheme, kind)
    }

    /// A state layer (`control`, `control-hover`, `subtle-hover`, ...).
    pub fn fill(&self, kind: &str) -> Rgba {
        tokens().fill(self.theme.scheme, kind)
    }
}

thread_local! {
    /// The text size of the desktop in percent (Accessibility > Text
    /// size): every ramp style is this much larger; nothing else of the
    /// interface is. The shell draws on one thread.
    static TEXT_SCALE: Cell<u32> = const { Cell::new(100) };
}

/// Sets the text size, 100 to 225 percent.
pub fn set_text_scale(percent: u32) {
    TEXT_SCALE.with(|scale| scale.set(percent.clamp(100, 225)));
}

/// The text size as a factor (1.0 to 2.25).
pub fn text_scale() -> f32 {
    TEXT_SCALE.with(Cell::get) as f32 / 100.0
}

/// A type-ramp style in a colour, at the desktop's text size.
pub fn ramp(name: &str, color: Rgba) -> TextStyle {
    let t = tokens().type_style(name);
    TextStyle::new((t.size * text_scale()).round(), color).weight(t.weight)
}

/// A line of text in a row of fixed height: the style as it is when its line
/// fits the row, else made smaller until it does, but never smaller than
/// at the normal text size. Larger text therefore fills fixed rows as far
/// as they allow; text that flows (wrapped text) grows fully.
pub fn fit_line(style: &TextStyle, height: f32) -> TextStyle {
    if style.line_height() <= height + 0.5 {
        return *style;
    }
    let floor = (style.size / text_scale()).round();
    let mut size = style.size;
    while size > floor && TextStyle::new(size, style.color).line_height() > height + 0.5 {
        size -= 1.0;
    }
    TextStyle { size, ..*style }
}

/// A type-ramp style that grows with the text size only as far as `room`
/// logical pixels of line height allow (fixed-height places such as the
/// taskbar's clock), never below the ramp's own size.
pub fn ramp_within(name: &str, color: Rgba, room: f32) -> TextStyle {
    let style = ramp(name, color);
    let base = tokens().type_style(name).size;
    if style.line_height() <= room || style.size <= base {
        return style;
    }
    let mut size = style.size;
    while size > base && TextStyle::new(size, color).line_height() > room {
        size -= 1.0;
    }
    TextStyle::new(size.max(base), color).weight(style.weight)
}

/// One control registered during a drawing pass.
#[derive(Clone, Debug, PartialEq)]
pub struct Hit {
    pub rect: Rect,
    pub action: Action,
    /// Reachable with Tab and the arrow keys.
    pub focusable: bool,
    /// Text of the tooltip shown after the pointer rests on it.
    pub tooltip: Option<String>,
    /// The pointer keeps acting on it while the button is held (sliders).
    pub drag: bool,
}

/// The rectangles of a surface that react to the pointer, in whole surface
/// pixels (grown outwards): the input region of a surface that is partly
/// empty.
pub fn input_rects(hits: &[Hit]) -> Vec<(i32, i32, i32, i32)> {
    hits.iter()
        .filter(|hit| hit.rect.w > 0.0 && hit.rect.h > 0.0)
        .map(|hit| {
            let (left, top) = (hit.rect.x.floor() as i32, hit.rect.y.floor() as i32);
            let (right, bottom) = ((hit.rect.x + hit.rect.w).ceil() as i32, (hit.rect.y + hit.rect.h).ceil() as i32);
            (left, top, right - left, bottom - top)
        })
        .collect()
}

/// How the pointer and the keyboard relate to a control right now.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Touch {
    pub hovered: bool,
    pub pressed: bool,
    pub focused: bool,
}

/// The visual kind of a button.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ButtonKind {
    /// Filled with the accent colour: the default action.
    Accent,
    /// Control fill with an outline.
    Standard,
    /// No fill at rest.
    Subtle,
}

/// Where the shell's glass gets its backdrop now.
pub fn glass_source(res: &mut Resources) -> GlassSource<'_> {
    if !res.transparency {
        GlassSource::Off
    } else if res.live_blur {
        GlassSource::Live
    } else {
        match res.backdrop.as_mut() {
            Some(backdrop) => GlassSource::Wallpaper(backdrop),
            None => GlassSource::Unknown,
        }
    }
}

/// The glass a material name of the shell stands for.
pub fn glass_kind(name: &str) -> GlassKind {
    match name {
        "chrome" => GlassKind::Taskbar,
        "flyout" => GlassKind::Flyout,
        "card" => GlassKind::Card,
        _ => GlassKind::Backdrop,
    }
}

/// One drawing pass over a surface.
pub struct Frame<'a> {
    pub canvas: &'a mut Canvas,
    pub res: &'a mut Resources,
    /// Pointer position on this surface, if the pointer is over it.
    pub pointer: Option<(f32, f32)>,
    /// The primary button is held on this surface.
    pub pressed: bool,
    pub hits: &'a mut Vec<Hit>,
    /// The control the keyboard focus is on, when the surface has one.
    pub focus: Option<&'a Action>,
    /// Where the canvas's top-left corner is on the output (for materials).
    pub origin: (f32, f32),
}

impl Frame<'_> {
    pub fn theme(&self) -> &'static Theme {
        self.res.theme
    }

    fn register(&mut self, rect: Rect, action: Action, focusable: bool) -> Touch {
        let hovered = self.pointer.is_some_and(|p| rect.contains(p));
        let focused = focusable && self.focus == Some(&action);
        self.hits.push(Hit {
            rect,
            action,
            focusable,
            tooltip: None,
            drag: false,
        });
        Touch {
            hovered,
            pressed: hovered && self.pressed,
            focused,
        }
    }

    /// Makes a rectangle a control: clickable and reachable by keyboard.
    pub fn hit(&mut self, rect: Rect, action: Action) -> Touch {
        self.register(rect, action, true)
    }

    /// Makes a rectangle react to the pointer only (backgrounds, swallowed
    /// clicks, things that have a keyboard equivalent elsewhere).
    pub fn area(&mut self, rect: Rect, action: Action) -> Touch {
        self.register(rect, action, false)
    }

    /// Gives the control registered last a tooltip.
    pub fn tip(&mut self, text: &str) {
        if let Some(hit) = self.hits.last_mut() {
            hit.tooltip = Some(text.to_owned());
        }
    }

    /// Marks the control registered last as draggable.
    pub fn drag(&mut self) {
        if let Some(hit) = self.hits.last_mut() {
            hit.drag = true;
        }
    }

    pub fn text(&mut self, text: &str, x: f32, y: f32, max_width: Option<f32>, style: &TextStyle) -> f32 {
        self.res.text.draw(self.canvas, text, x, y, max_width, style)
    }

    pub fn text_centered(&mut self, text: &str, rect: Rect, style: &TextStyle) {
        let style = &fit_line(style, rect.h);
        self.res.text.draw_centered(self.canvas, text, rect.x, rect.y, rect.w, rect.h, style);
    }

    /// One line, vertically centred in `rect`, starting at its left edge.
    pub fn text_left(&mut self, text: &str, rect: Rect, style: &TextStyle) -> f32 {
        let style = &fit_line(style, rect.h);
        let y = rect.y + (rect.h - style.line_height()) / 2.0;
        self.res.text.draw(self.canvas, text, rect.x, y, Some(rect.w), style)
    }

    /// One line, vertically centred in `rect`, ending at its right edge.
    pub fn text_right(&mut self, text: &str, rect: Rect, style: &TextStyle) {
        let style = &fit_line(style, rect.h);
        let fitted = self.res.text.elide(text, style, rect.w);
        let (w, h) = self.res.text.measure(&fitted, style);
        self.res.text.draw(self.canvas, &fitted, rect.right() - w, rect.y + (rect.h - h) / 2.0, None, style);
    }

    /// Wrapped text; returns the height used.
    pub fn text_wrapped(&mut self, text: &str, x: f32, y: f32, width: f32, max_lines: usize, style: &TextStyle) -> f32 {
        let lines = self.res.text.wrap(text, style, width, max_lines);
        let line = style.line_height();
        for (i, text) in lines.iter().enumerate() {
            self.res.text.draw(self.canvas, text, x, y + i as f32 * line, None, style);
        }
        lines.len() as f32 * line
    }

    /// Draws a themed icon in a square of `size` logical pixels. Returns
    /// whether the icon was found.
    pub fn icon(&mut self, name: &str, x: f32, y: f32, size: f32) -> bool {
        let pixels = (size * self.canvas.scale()).round().max(1.0) as u32;
        match self.res.icons.load(name, pixels) {
            Some(pixmap) => {
                self.canvas.draw_pixmap(&pixmap, x, y, size, size, 1.0);
                true
            }
            None => false,
        }
    }

    /// Draws a symbolic icon in one colour (its shape is taken from the
    /// icon's coverage), for icons on accent or coloured backgrounds.
    pub fn icon_tinted(&mut self, name: &str, x: f32, y: f32, size: f32, color: Rgba) -> bool {
        let scale = self.canvas.scale();
        let pixels = (size * scale).round().max(1.0) as u32;
        let Some(pixmap) = self.res.icons.load(name, pixels) else {
            return false;
        };
        let (ox, oy) = ((x * scale).round() as i32, (y * scale).round() as i32);
        let width = pixmap.width() as usize;
        for (i, pixel) in pixmap.data().chunks_exact(4).enumerate() {
            if pixel[3] != 0 {
                self.canvas.blend_pixel(ox + (i % width) as i32, oy + (i / width) as i32, color, pixel[3]);
            }
        }
        true
    }

    /// The first of `names` that exists, or a neutral placeholder tile.
    pub fn icon_or_placeholder(&mut self, names: &[&str], x: f32, y: f32, size: f32) {
        if names.iter().any(|name| !name.is_empty() && self.icon(name, x, y, size)) {
            return;
        }
        let theme = self.theme();
        self.canvas.fill_rounded_rect(x, y, size, size, size * 0.24, theme.surface_hover);
        let stroke = self.res.stroke("control");
        self.canvas.stroke_rounded_rect(x, y, size, size, size * 0.24, 1.0, stroke);
    }

    pub fn pixmap(&mut self, pixmap: &Rc<Pixmap>, rect: Rect) {
        self.canvas.draw_pixmap(pixmap, rect.x, rect.y, rect.w, rect.h, 1.0);
    }

    pub fn glyph(&mut self, glyph: Glyph, rect: Rect, color: Rgba) {
        crate::glyph::draw(self.canvas, glyph, rect, color);
    }

    /// Fills a rounded rectangle with Boswas glass (`boswas_ui::glass`):
    /// `acrylic` and `mica` are the window backdrop (the shell's panels),
    /// `chrome` the taskbar, `flyout` the glass of menus, tooltips, toasts
    /// and dialogs. Over the blurred wallpaper behind this surface, or,
    /// where the compositor blurs what really lies behind it, its tint
    /// alone; opaque with glass off.
    pub fn material(&mut self, rect: Rect, radii: [f32; 4], name: &str) {
        let kind = glass_kind(name);
        let theme = self.res.theme;
        let radius = radii.iter().copied().fold(0.0f32, f32::max);
        if self.res.live_blur && self.res.transparency {
            self.res.glass_regions.push((rect, radius));
        }
        let origin = self.origin;
        let source = glass_source(self.res);
        glass::fill(self.canvas, rect.x, rect.y, rect.w, rect.h, radii, origin, theme, kind, source);
    }

    /// The glass of this scheme while glass is on.
    pub fn glass(&self) -> Option<Glass> {
        Glass::of(self.res.theme).filter(|_| self.res.transparency)
    }

    /// The edges of a glass surface (inner glow and hairline), or the plain
    /// surface outline with glass off.
    pub fn glass_edges(&mut self, rect: Rect, radius: f32, name: &str) {
        match self.glass() {
            Some(g) => glass::draw_edges(self.canvas, rect.x, rect.y, rect.w, rect.h, radius, &g, glass_kind(name), false),
            None => {
                let stroke = self.res.stroke("surface");
                self.canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, 1.0, stroke);
            }
        }
    }

    /// An overlay panel: shadow, material and a one-pixel outline. Drawn
    /// once and then reused while its size, place, material, theme and the
    /// wallpaper behind it stay the same. Returns the panel as drawn on
    /// the whole canvas (see [`Frame::restore_rows`]).
    pub fn panel(&mut self, rect: Rect, material: &str) -> Option<Rc<Pixmap>> {
        let key = PanelKey {
            canvas: self.canvas.pixel_size(),
            scale: self.canvas.scale().to_bits(),
            rect: [rect.x.to_bits(), rect.y.to_bits(), rect.w.to_bits(), rect.h.to_bits()],
            origin: [self.origin.0.to_bits(), self.origin.1.to_bits()],
            material: material.to_owned(),
            theme: std::ptr::from_ref(self.res.theme) as usize,
            transparency: self.res.transparency,
            backdrop: self.res.backdrop.as_ref().map(|_| self.res.backdrop_generation),
            live: self.res.live_blur,
        };
        // The region is collected on every drawing, kept panel or not.
        if self.res.live_blur && self.res.transparency {
            self.res.glass_regions.push((rect, tokens().radius_flyout));
        }
        // Most recently used last.
        let kept = match self.res.panels.iter().position(|(k, _)| *k == key) {
            Some(index) => {
                let entry = self.res.panels.remove(index);
                let kept = entry.1.clone();
                self.res.panels.push(entry);
                kept
            }
            None => {
                let mut chrome = Canvas::new(self.canvas.width(), self.canvas.height(), self.canvas.scale())?;
                {
                    let regions = self.res.glass_regions.len();
                    let mut frame = Frame {
                        canvas: &mut chrome,
                        res: &mut *self.res,
                        pointer: None,
                        pressed: false,
                        hits: &mut Vec::new(),
                        focus: None,
                        origin: self.origin,
                    };
                    frame.paint_panel(rect, material);
                    frame.res.glass_regions.truncate(regions);
                }
                // The part of the picture the panel and its shadow cover.
                let e = tokens().elevation("flyout");
                let reach = e.blur + e.y.abs() + 2.0;
                let area = device_rect(&Rect::new(rect.x - reach, rect.y - reach, rect.w + 2.0 * reach, rect.h + 2.0 * reach), chrome.scale());
                let whole = chrome.into_pixmap();
                let part = tiny_skia::IntRect::from_xywh(area.0 as i32, area.1 as i32, area.2, area.3)
                    .and_then(|r| whole.clone_rect(r).map(|p| (r.x(), r.y(), Rc::new(p))));
                let kept = Kept { whole: Rc::new(whole), part };
                if self.res.panels.len() >= MAX_PANELS {
                    self.res.panels.remove(0);
                }
                self.res.panels.push((key, kept.clone()));
                kept
            }
        };
        // Usually the first thing drawn: then a copy does (of the part
        // being drawn again, when only a part is). Over something drawn
        // already, only the part of the picture the panel covers is laid
        // on (the rest of it is transparent).
        if self.canvas.is_blank() {
            self.canvas.copy_from_full(&kept.whole);
        } else if let Some((x, y, part)) = &kept.part {
            let paint = tiny_skia::PixmapPaint { quality: tiny_skia::FilterQuality::Nearest, ..tiny_skia::PixmapPaint::default() };
            self.canvas.draw_device_pixmap(*x, *y, part.as_ref().as_ref(), &paint);
        } else {
            let (w, h) = (self.canvas.width(), self.canvas.height());
            self.canvas.draw_pixmap(&kept.whole, 0.0, 0.0, w, h, 1.0);
        }
        let pixmap = kept.whole;
        Some(pixmap)
    }

    /// Puts the rows from `top` to `bottom` (logical pixels, widened to
    /// whole device pixels inwards) back as `panel` has them: what was
    /// drawn there since is gone. A scrolling part is drawn as it falls
    /// and cut at its window's edges this way.
    pub fn restore_rows(&mut self, panel: &Pixmap, top: f32, bottom: f32) {
        let scale = self.canvas.scale();
        let (w, h) = self.canvas.pixel_size();
        if (panel.width(), panel.height()) != (w, h) {
            return;
        }
        let first = ((top * scale).floor().max(0.0) as u32).min(h);
        let last = ((bottom * scale).ceil().max(0.0) as u32).min(h);
        if first >= last {
            return;
        }
        self.canvas.copy_rows_from_full(panel, first, last);
    }

    fn paint_panel(&mut self, rect: Rect, material: &str) {
        let t = tokens();
        let e = t.elevation("flyout");
        self.canvas.shadow(rect.x, rect.y, rect.w, rect.h, t.radius_flyout, e.blur, e.y, e.alpha);
        self.material(rect, [t.radius_flyout; 4], material);
        self.glass_edges(rect, t.radius_flyout, material);
    }

    /// The hover and pressed layer of a control without a fill of its own.
    pub fn state_layer(&mut self, rect: Rect, radius: f32, touch: Touch, selected: bool) {
        let kind = if touch.pressed {
            Some("subtle-pressed")
        } else if touch.hovered || selected {
            Some("subtle-hover")
        } else {
            None
        };
        if let Some(kind) = kind {
            let color = self.res.fill(kind);
            self.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, color);
        }
        // On glass a control under the pointer is lighter glass with a
        // brighter hairline.
        if touch.hovered || touch.pressed {
            if let Some(g) = self.glass() {
                glass::draw_hairline(self.canvas, rect.x, rect.y, rect.w, rect.h, radius, &g, true);
            }
        }
        if touch.focused {
            self.focus_ring(rect, radius);
        }
    }

    /// The keyboard focus mark: a two-pixel ring just outside the control.
    pub fn focus_ring(&mut self, rect: Rect, radius: f32) {
        let color = self.theme().focus;
        self.canvas
            .stroke_rounded_rect(rect.x - 2.0, rect.y - 2.0, rect.w + 4.0, rect.h + 4.0, radius + 2.0, 2.0, color);
    }

    /// A card: the fill and outline of a control that holds content; on
    /// glass a card of glass (its tint, glow and hairline).
    pub fn card(&mut self, rect: Rect, radius: f32) {
        if let Some(g) = self.glass() {
            glass::draw_surface(self.canvas, rect.x, rect.y, rect.w, rect.h, radius, &g, GlassKind::Card, g.card, None);
            return;
        }
        let fill = self.res.fill("control");
        let stroke = self.res.stroke("control");
        self.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, fill);
        self.canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, 1.0, stroke);
    }

    pub fn divider(&mut self, x: f32, y: f32, w: f32) {
        let color = self.res.stroke("divider");
        self.canvas.fill_rect(x, y, w, 1.0, color);
    }

    /// A text button. Returns its state so callers can react to hover.
    pub fn button(&mut self, rect: Rect, label: &str, kind: ButtonKind, enabled: bool, action: Action) -> Touch {
        let theme = self.theme();
        let radius = tokens().radius_control;
        let touch = if enabled { self.hit(rect, action) } else { Touch::default() };
        let mut color = theme.text;
        match kind {
            ButtonKind::Accent => {
                let fill = if !enabled {
                    self.res.fill("disabled")
                } else if touch.pressed {
                    theme.accent_pressed
                } else if touch.hovered {
                    theme.accent_hover
                } else {
                    theme.accent
                };
                self.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, fill);
                color = if enabled { theme.on_accent } else { theme.text_muted };
            }
            ButtonKind::Standard => {
                let fill = self.res.fill(if !enabled {
                    "disabled"
                } else if touch.pressed {
                    "control-pressed"
                } else if touch.hovered {
                    "control-hover"
                } else {
                    "control"
                });
                let stroke = self.res.stroke("control");
                self.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, fill);
                self.canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, 1.0, stroke);
                if !enabled {
                    color = theme.text_muted;
                }
            }
            ButtonKind::Subtle => {
                self.state_layer(rect, radius, Touch { focused: false, ..touch }, false);
                if !enabled {
                    color = theme.text_muted;
                }
            }
        }
        if touch.focused {
            self.focus_ring(rect, radius);
        }
        self.text_centered(label, rect.inset(6.0), &ramp("body", color));
        touch
    }

    /// A square button showing a symbolic icon.
    pub fn icon_button(&mut self, rect: Rect, icon: &str, tip: &str, action: Action) -> Touch {
        let touch = self.hit(rect, action);
        self.tip(tip);
        self.state_layer(rect, tokens().radius_control, touch, false);
        let size = 16.0_f32.min(rect.w - 4.0);
        self.icon(icon, rect.x + (rect.w - size) / 2.0, rect.y + (rect.h - size) / 2.0, size);
        touch
    }

    /// A square button showing a drawn glyph.
    pub fn glyph_button(&mut self, rect: Rect, glyph: Glyph, tip: &str, action: Action) -> Touch {
        let touch = self.hit(rect, action);
        if !tip.is_empty() {
            self.tip(tip);
        }
        self.state_layer(rect, tokens().radius_control, touch, false);
        let size = 16.0_f32.min(rect.w - 4.0);
        let color = self.theme().text;
        let (cx, cy) = rect.center();
        self.glyph(glyph, Rect::new(cx - size / 2.0, cy - size / 2.0, size, size), color);
        touch
    }

    /// A toggle switch, 40 x 20.
    pub fn toggle(&mut self, x: f32, y: f32, on: bool, action: Action) -> Touch {
        let theme = self.theme();
        let rect = Rect::new(x, y, 40.0, 20.0);
        let touch = self.hit(rect, action);
        if on {
            let fill = if touch.hovered { theme.accent_hover } else { theme.accent };
            self.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, 10.0, fill);
            self.canvas.fill_circle(rect.right() - 10.0, rect.y + 10.0, if touch.hovered { 7.0 } else { 6.0 }, theme.on_accent);
        } else {
            let fill = self.res.fill(if touch.hovered { "control-hover" } else { "control" });
            self.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, 10.0, fill);
            self.canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, 10.0, 1.0, theme.text_muted);
            self.canvas.fill_circle(rect.x + 10.0, rect.y + 10.0, if touch.hovered { 7.0 } else { 6.0 }, theme.text_secondary);
        }
        if touch.focused {
            self.focus_ring(rect, 10.0);
        }
        touch
    }

    /// A horizontal slider; `value` is 0.0 to 1.0. The whole rectangle
    /// reacts, the track is drawn through its middle.
    pub fn slider(&mut self, rect: Rect, value: f32, enabled: bool, action: Action) -> Touch {
        let theme = self.theme();
        let touch = if enabled { self.hit(rect, action) } else { Touch::default() };
        if enabled {
            self.drag();
        }
        let value = value.clamp(0.0, 1.0);
        let (x0, x1) = slider_track(rect);
        let cy = rect.y + rect.h / 2.0;
        let knob = x0 + (x1 - x0) * value;
        let rail = self.res.stroke("window").alpha(2.2);
        self.canvas.fill_rounded_rect(x0, cy - 2.0, x1 - x0, 4.0, 2.0, rail);
        let fill = if enabled { theme.accent } else { theme.text_muted };
        self.canvas.fill_rounded_rect(x0, cy - 2.0, (knob - x0).max(0.0), 4.0, 2.0, fill);
        self.canvas.fill_circle(knob, cy, 10.0, theme.surface_hover);
        self.canvas.stroke_circle(knob, cy, 9.5, 1.0, self.res.stroke("control"));
        let inner = if touch.pressed {
            5.0
        } else if touch.hovered {
            7.0
        } else {
            6.0
        };
        self.canvas.fill_circle(knob, cy, inner, fill);
        if touch.focused {
            self.focus_ring(Rect::new(knob - 10.0, cy - 10.0, 20.0, 20.0), 10.0);
        }
        touch
    }

    /// A text box in the Fluent style: control fill, outline, and an accent
    /// line along the bottom when it has the keyboard.
    #[allow(clippy::too_many_arguments)]
    pub fn text_box(&mut self, rect: Rect, field: &TextField, placeholder: &str, focused: bool, masked: bool, pad_left: f32, pad_right: f32) {
        let theme = self.theme();
        let radius = tokens().radius_control;
        let fill = if focused { theme.background.alpha(0.55) } else { self.res.fill("control") };
        let stroke = self.res.stroke("control");
        self.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, fill);
        self.canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, 1.0, stroke);
        let (line, color) = if focused { (2.0, theme.accent) } else { (1.0, theme.text_muted) };
        self.canvas.fill_rect(rect.x + radius, rect.bottom() - line, rect.w - 2.0 * radius, line, color);
        let style = ramp("body", theme.text);
        let x = rect.x + pad_left;
        let width = (rect.w - pad_left - pad_right).max(0.0);
        let text_y = rect.y + (rect.h - style.line_height()) / 2.0;
        if field.is_empty() {
            self.text(placeholder, x, text_y, Some(width), &ramp("body", theme.text_muted));
            if focused {
                self.canvas.fill_rect(x, rect.y + (rect.h - 18.0) / 2.0, 1.0, 18.0, theme.text);
            }
            return;
        }
        if masked {
            // One dot per character, capped so the length is not on display.
            let dots = field.len().min(MAX_DOTS);
            let cursor = field.cursor().min(dots);
            for i in 0..dots {
                self.canvas.fill_circle(x + 5.0 + i as f32 * 11.0, rect.y + rect.h / 2.0, 3.5, theme.text);
            }
            if focused {
                self.canvas.fill_rect(x + cursor as f32 * 11.0, rect.y + (rect.h - 18.0) / 2.0, 1.0, 18.0, theme.text);
            }
            return;
        }
        // Keep the caret in view: show the tail when the text is too wide.
        let before: String = field.text().chars().take(field.cursor()).collect();
        let caret = self.res.text.measure(&before, &style).0;
        let full = self.res.text.measure(field.text(), &style).0;
        if full <= width {
            self.text(field.text(), x, text_y, None, &style);
            if focused {
                self.canvas.fill_rect(x + caret, rect.y + (rect.h - 18.0) / 2.0, 1.0, 18.0, theme.text);
            }
        } else {
            let mut shown: Vec<char> = before.chars().collect();
            let mut start = 0;
            while start < shown.len() && self.res.text.measure(&shown[start..].iter().collect::<String>(), &style).0 > width - 2.0 {
                start += 1;
            }
            shown.drain(..start);
            let visible: String = shown.into_iter().collect();
            let drawn = self.text(&visible, x, text_y, None, &style);
            if focused {
                self.canvas.fill_rect(x + drawn, rect.y + (rect.h - 18.0) / 2.0, 1.0, 18.0, theme.text);
            }
        }
    }

    /// A vertical scroll indicator along the right edge of `view`.
    pub fn scrollbar(&mut self, view: Rect, content_height: f32, offset: f32) {
        if content_height <= view.h + 0.5 {
            return;
        }
        let length = (view.h * view.h / content_height).max(24.0);
        let travel = view.h - length;
        let y = view.y + travel * (offset / (content_height - view.h)).clamp(0.0, 1.0);
        let color = self.theme().text_muted.alpha(0.6);
        self.canvas.fill_rounded_rect(view.right() - 5.0, y, 3.0, length, 1.5, color);
    }
}

/// Most dots a masked text box shows.
pub const MAX_DOTS: usize = 20;

/// The x range of a slider's track inside its rectangle.
pub fn slider_track(rect: Rect) -> (f32, f32) {
    (rect.x + 10.0, rect.right() - 10.0)
}

/// The value (0.0 to 1.0) a pointer at `x` selects on a slider.
pub fn slider_value(rect: Rect, x: f32) -> f32 {
    let (x0, x1) = slider_track(rect);
    if x1 <= x0 {
        return 0.0;
    }
    ((x - x0) / (x1 - x0)).clamp(0.0, 1.0)
}

/// The hit under a point, the topmost (last drawn) first.
pub fn hit_at(hits: &[Hit], point: (f32, f32)) -> Option<usize> {
    hits.iter().rposition(|hit| hit.rect.contains(point))
}

/// How far (logical pixels) the hover or pressed look of a control may
/// reach beyond the rectangle that takes the pointer.
pub const HOVER_REACH: f32 = 3.0;

/// What is drawn differently when the pointer moves from `from` to `to`
/// (`None`: off the surface) over a surface of `size` (logical pixels)
/// whose controls are `hits`: the controls it entered or left, whose hover
/// look changes. Backgrounds have no hover look (see [`has_look`]).
pub fn hover_changes(hits: &[Hit], size: (f32, f32), from: Option<(f32, f32)>, to: Option<(f32, f32)>) -> Vec<Rect> {
    let inside = |p: Option<(f32, f32)>, r: &Rect| p.is_some_and(|p| r.contains(p));
    hits.iter()
        .filter(|hit| inside(from, &hit.rect) != inside(to, &hit.rect) && has_look(hit, size))
        .map(|hit| hit.rect.inset(-HOVER_REACH))
        .collect()
}

/// What is drawn differently when the primary button goes down or up with
/// the pointer at `pointer`: the controls under it, whose pressed look
/// changes.
pub fn press_changes(hits: &[Hit], size: (f32, f32), pointer: Option<(f32, f32)>) -> Vec<Rect> {
    hits.iter()
        .filter(|hit| pointer.is_some_and(|p| hit.rect.contains(p)) && has_look(hit, size))
        .map(|hit| hit.rect.inset(-HOVER_REACH))
        .collect()
}

/// Whether a control can look different under the pointer: not a
/// background that only takes clicks (a panel's area that does nothing,
/// the desktop, the invisible surface behind a menu, anything covering
/// the whole surface).
fn has_look(hit: &Hit, size: (f32, f32)) -> bool {
    let r = &hit.rect;
    let covers = r.x <= 0.0 && r.y <= 0.0 && r.right() >= size.0 && r.bottom() >= size.1;
    let background = !hit.focusable && matches!(hit.action, Action::None | Action::Dismiss | Action::Desktop);
    !covers && !background
}

/// Whether two rectangles overlap or touch.
pub fn overlap(a: &Rect, b: &Rect) -> bool {
    a.x <= b.right() && b.x <= a.right() && a.y <= b.bottom() && b.y <= a.bottom()
}

/// The smallest rectangle holding both.
pub fn bounds(a: &Rect, b: &Rect) -> Rect {
    let (x, y) = (a.x.min(b.x), a.y.min(b.y));
    Rect::new(x, y, a.right().max(b.right()) - x, a.bottom().max(b.bottom()) - y)
}

/// A rectangle in logical pixels as whole device pixels at `scale`
/// (grown outwards).
pub fn device_rect(rect: &Rect, scale: f32) -> (u32, u32, u32, u32) {
    let (x0, y0) = ((rect.x * scale).floor().max(0.0), (rect.y * scale).floor().max(0.0));
    let (x1, y1) = ((rect.right() * scale).ceil().max(0.0), (rect.bottom() * scale).ceil().max(0.0));
    (x0 as u32, y0 as u32, (x1 - x0).max(0.0) as u32, (y1 - y0).max(0.0) as u32)
}

/// A direction of keyboard navigation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Nav {
    Next,
    Previous,
    Left,
    Right,
    Up,
    Down,
}

/// The control the focus moves to from `current`. Tab order is drawing
/// order; arrow keys go to the nearest control in that direction.
pub fn navigate(hits: &[Hit], current: Option<&Action>, nav: Nav) -> Option<Action> {
    let focusable: Vec<&Hit> = hits.iter().filter(|h| h.focusable).collect();
    if focusable.is_empty() {
        return None;
    }
    let position = current.and_then(|action| focusable.iter().position(|h| &h.action == action));
    let Some(position) = position else {
        let first = if nav == Nav::Previous { focusable.len() - 1 } else { 0 };
        return Some(focusable[first].action.clone());
    };
    match nav {
        Nav::Next => Some(focusable[(position + 1) % focusable.len()].action.clone()),
        Nav::Previous => Some(focusable[(position + focusable.len() - 1) % focusable.len()].action.clone()),
        _ => {
            let from = focusable[position].rect;
            let (fx, fy) = from.center();
            let mut best: Option<(f32, usize)> = None;
            for (index, hit) in focusable.iter().enumerate() {
                if index == position {
                    continue;
                }
                let (x, y) = hit.rect.center();
                let (dx, dy) = (x - fx, y - fy);
                // Along the direction of travel, and across it.
                let (along, across) = match nav {
                    Nav::Left => (-dx, dy.abs()),
                    Nav::Right => (dx, dy.abs()),
                    Nav::Up => (-dy, dx.abs()),
                    _ => (dy, dx.abs()),
                };
                if along <= 0.5 {
                    continue;
                }
                let score = along + across * 3.0;
                if best.is_none_or(|(s, _)| score < s) {
                    best = Some((score, index));
                }
            }
            best.map(|(_, index)| focusable[index].action.clone())
        }
    }
}

/// A timed transition from 0 to 1.
#[derive(Clone, Copy, Debug)]
pub struct Anim {
    start: Instant,
    duration: Duration,
    /// The surface is leaving: the value runs from 1 to 0.
    pub closing: bool,
}

impl Anim {
    pub fn opening(now: Instant, reduced: bool) -> Self {
        Self::new(now, tokens().motion_normal_ms, false, reduced)
    }

    pub fn closing(now: Instant, reduced: bool) -> Self {
        Self::new(now, tokens().motion_fast_ms, true, reduced)
    }

    fn new(now: Instant, ms: u32, closing: bool, reduced: bool) -> Self {
        Self {
            start: now,
            duration: Duration::from_millis(if reduced { 0 } else { u64::from(ms) }),
            closing,
        }
    }

    pub fn done(&self, now: Instant) -> bool {
        now.saturating_duration_since(self.start) >= self.duration
    }

    /// The presence to draw the surface with at `now` while the transition
    /// is drawn frame by frame (each frame at the display's pace): from its
    /// first instant to its end, even when the surface is still fully there
    /// (a closing transition starts at 1.0), and for a closing one until
    /// the surface is gone. `None`: the surface is drawn as it is.
    pub fn frame(&self, now: Instant) -> Option<f32> {
        (self.closing || !self.done(now)).then(|| self.presence(now))
    }

    /// How present the surface is: 0.0 gone, 1.0 fully shown. Things that
    /// enter decelerate, things that leave accelerate.
    pub fn presence(&self, now: Instant) -> f32 {
        if self.duration.is_zero() {
            return if self.closing { 0.0 } else { 1.0 };
        }
        let t = (now.saturating_duration_since(self.start).as_secs_f32() / self.duration.as_secs_f32()).clamp(0.0, 1.0);
        if self.closing {
            1.0 - cubic_bezier(tokens().curve("accelerate"), t)
        } else {
            cubic_bezier(tokens().curve("decelerate"), t)
        }
    }
}

/// A single-line text field with a caret.
#[derive(Default)]
pub struct TextField {
    text: String,
    /// Caret position in characters.
    cursor: usize,
}

impl TextField {
    /// Longest text a field accepts.
    pub const MAX_CHARS: usize = 256;

    pub fn text(&self) -> &str {
        &self.text
    }

    pub fn is_empty(&self) -> bool {
        self.text.is_empty()
    }

    pub fn len(&self) -> usize {
        self.text.chars().count()
    }

    pub fn cursor(&self) -> usize {
        self.cursor
    }

    fn byte_at(&self, chars: usize) -> usize {
        self.text.char_indices().nth(chars).map_or(self.text.len(), |(i, _)| i)
    }

    /// Inserts typed text at the caret, ignoring control characters.
    /// Returns whether the field changed.
    pub fn insert(&mut self, typed: &str) -> bool {
        let mut changed = false;
        for c in typed.chars().filter(|c| !c.is_control()) {
            if self.len() >= Self::MAX_CHARS {
                break;
            }
            let at = self.byte_at(self.cursor);
            self.text.insert(at, c);
            self.cursor += 1;
            changed = true;
        }
        changed
    }

    /// Replaces the whole text and puts the caret at its end.
    pub fn set(&mut self, text: &str) {
        self.clear();
        self.insert(text);
    }

    pub fn backspace(&mut self) -> bool {
        if self.cursor == 0 {
            return false;
        }
        let at = self.byte_at(self.cursor - 1);
        self.text.remove(at);
        self.cursor -= 1;
        true
    }

    pub fn delete(&mut self) -> bool {
        if self.cursor >= self.len() {
            return false;
        }
        let at = self.byte_at(self.cursor);
        self.text.remove(at);
        true
    }

    /// Handles an editing key. Returns whether the key was one.
    pub fn key(&mut self, keysym: Keysym, ctrl: bool) -> bool {
        match keysym {
            Keysym::BackSpace => {
                self.backspace();
            }
            Keysym::Delete | Keysym::KP_Delete => {
                self.delete();
            }
            Keysym::Left | Keysym::KP_Left => self.cursor = self.cursor.saturating_sub(1),
            Keysym::Right | Keysym::KP_Right => self.cursor = (self.cursor + 1).min(self.len()),
            Keysym::Home | Keysym::KP_Home => self.cursor = 0,
            Keysym::End | Keysym::KP_End => self.cursor = self.len(),
            Keysym::u if ctrl => self.clear(),
            _ => return false,
        }
        true
    }

    /// Empties the field, overwriting what it held.
    pub fn clear(&mut self) {
        use zeroize::Zeroize;
        self.text.zeroize();
        self.cursor = 0;
    }

    /// Takes the text out, leaving the field empty.
    pub fn take(&mut self) -> zeroize::Zeroizing<String> {
        self.cursor = 0;
        zeroize::Zeroizing::new(std::mem::take(&mut self.text))
    }
}

impl Drop for TextField {
    fn drop(&mut self) {
        self.clear();
    }
}

#[cfg(test)]
pub mod testing {
    //! Drawing helpers for the unit tests of the surfaces.
    use super::*;

    /// Runs `draw` on a fresh canvas and returns the canvas and the hits.
    pub fn render(width: f32, height: f32, scale: f32, scheme: Scheme, pointer: Option<(f32, f32)>, focus: Option<&Action>, draw: impl FnOnce(&mut Frame)) -> (Canvas, Vec<Hit>) {
        render_with(Resources::new(Theme::of(scheme)), width, height, scale, pointer, focus, draw)
    }

    /// [`render`] with given resources (for one, a machine without fonts).
    pub fn render_with(mut res: Resources, width: f32, height: f32, scale: f32, pointer: Option<(f32, f32)>, focus: Option<&Action>, draw: impl FnOnce(&mut Frame)) -> (Canvas, Vec<Hit>) {
        let mut canvas = Canvas::new(width, height, scale).unwrap();
        let mut hits = Vec::new();
        let mut frame = Frame {
            canvas: &mut canvas,
            res: &mut res,
            pointer,
            pressed: false,
            hits: &mut hits,
            focus,
            origin: (0.0, 0.0),
        };
        draw(&mut frame);
        (canvas, hits)
    }

    /// Whether every hit lies inside the canvas and has a real size.
    pub fn hits_are_inside(hits: &[Hit], width: f32, height: f32) -> bool {
        hits.iter().all(|h| h.rect.w > 0.0 && h.rect.h > 0.0 && h.rect.x >= -0.5 && h.rect.y >= -0.5 && h.rect.right() <= width + 0.5 && h.rect.bottom() <= height + 0.5)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn larger_text_fills_fixed_rows_as_far_as_they_allow() {
        set_text_scale(100);
        assert_eq!(ramp("body", Rgba::WHITE).size, 14.0);
        set_text_scale(200);
        let body = ramp("body", Rgba::WHITE);
        assert_eq!(body.size, 28.0);
        assert_eq!(fit_line(&body, 60.0), body, "room enough");
        let fitted = fit_line(&body, 22.0);
        assert!(fitted.line_height() <= 22.5 && fitted.size >= 14.0, "{fitted:?}");
        assert_eq!(fit_line(&body, 4.0).size, 14.0, "never below the normal size");
        let clock = ramp_within("caption", Rgba::WHITE, 18.0);
        assert!(clock.line_height() <= 18.0 && clock.size >= 12.0);
        set_text_scale(1000);
        assert_eq!(text_scale(), 2.25, "at most 225 %");
        set_text_scale(100);
    }

    #[test]
    fn the_input_region_covers_what_reacts_and_nothing_else() {
        let mut card = hit(12.5, 40.25, 0);
        card.rect = Rect::new(12.5, 40.25, 300.0, 80.5);
        let mut empty = hit(0.0, 0.0, 1);
        empty.rect = Rect::new(5.0, 5.0, 0.0, 10.0);
        assert_eq!(input_rects(&[card, empty, hit(400.0, 8.0, 2)]), [(12, 40, 301, 81), (400, 8, 20, 20)]);
        assert!(input_rects(&[]).is_empty(), "nothing drawn: every click passes through");
    }

    fn hit(x: f32, y: f32, id: usize) -> Hit {
        Hit {
            rect: Rect::new(x, y, 20.0, 20.0),
            action: Action::Menu { level: 0, index: id },
            focusable: true,
            tooltip: None,
            drag: false,
        }
    }

    fn id(action: Option<Action>) -> usize {
        match action {
            Some(Action::Menu { index, .. }) => index,
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn the_topmost_hit_wins() {
        let hits = vec![
            Hit { rect: Rect::new(0.0, 0.0, 100.0, 100.0), ..hit(0.0, 0.0, 1) },
            Hit { rect: Rect::new(10.0, 10.0, 20.0, 20.0), ..hit(0.0, 0.0, 2) },
        ];
        assert_eq!(hit_at(&hits, (15.0, 15.0)), Some(1));
        assert_eq!(hit_at(&hits, (50.0, 50.0)), Some(0));
        assert_eq!(hit_at(&hits, (100.0, 50.0)), None, "the right edge is outside");
        assert_eq!(hit_at(&hits, (-1.0, 5.0)), None);
    }

    #[test]
    fn keyboard_navigation_follows_order_and_geometry() {
        // A 3x2 grid: ids 0 1 2 / 3 4 5.
        let hits: Vec<Hit> = (0..6).map(|i| hit((i % 3) as f32 * 30.0, (i / 3) as f32 * 30.0, i)).collect();
        let at = |i: usize| hits[i].action.clone();
        assert_eq!(id(navigate(&hits, None, Nav::Next)), 0);
        assert_eq!(id(navigate(&hits, None, Nav::Previous)), 5);
        assert_eq!(id(navigate(&hits, Some(&at(2)), Nav::Next)), 3);
        assert_eq!(id(navigate(&hits, Some(&at(5)), Nav::Next)), 0, "Tab wraps");
        assert_eq!(id(navigate(&hits, Some(&at(0)), Nav::Previous)), 5);
        assert_eq!(id(navigate(&hits, Some(&at(1)), Nav::Down)), 4);
        assert_eq!(id(navigate(&hits, Some(&at(4)), Nav::Up)), 1);
        assert_eq!(id(navigate(&hits, Some(&at(4)), Nav::Left)), 3);
        assert_eq!(id(navigate(&hits, Some(&at(4)), Nav::Right)), 5);
        assert_eq!(navigate(&hits, Some(&at(5)), Nav::Right), None, "nothing further right");
        assert_eq!(navigate(&[], None, Nav::Next), None);
        let unfocusable = vec![Hit { focusable: false, ..hit(0.0, 0.0, 9) }];
        assert_eq!(navigate(&unfocusable, None, Nav::Next), None);
    }

    #[test]
    fn sliders_map_positions_to_values() {
        let rect = Rect::new(100.0, 0.0, 220.0, 32.0);
        assert_eq!(slider_value(rect, 0.0), 0.0);
        assert_eq!(slider_value(rect, 110.0), 0.0);
        assert_eq!(slider_value(rect, 310.0), 1.0);
        assert_eq!(slider_value(rect, 900.0), 1.0);
        assert!((slider_value(rect, 210.0) - 0.5).abs() < 1e-6);
        assert_eq!(slider_value(Rect::new(0.0, 0.0, 10.0, 10.0), 5.0), 0.0, "a slider too small to have a track");
    }

    #[test]
    fn animations_run_once_and_respect_reduced_motion() {
        let now = Instant::now();
        let open = Anim::opening(now, false);
        assert!(open.presence(now) < 0.01);
        assert!(!open.done(now));
        let later = now + Duration::from_secs(1);
        assert!(open.done(later));
        assert!((open.presence(later) - 1.0).abs() < 0.01);
        let mid = open.presence(now + Duration::from_millis(60));
        assert!(mid > 0.4 && mid < 1.0, "entering starts fast: {mid}");
        let close = Anim::closing(now, false);
        assert!((close.presence(now) - 1.0).abs() < 0.01);
        assert!(close.presence(later).abs() < 0.01);
        let reduced = Anim::opening(now, true);
        assert!(reduced.done(now));
        assert_eq!(reduced.presence(now), 1.0);
        assert_eq!(Anim::closing(now, true).presence(now), 0.0);
    }

    #[test]
    fn a_transition_is_drawn_frame_by_frame_from_its_first_instant_to_its_end() {
        let now = Instant::now();
        let later = now + Duration::from_secs(1);
        // Opening: from nothing, until it is over; then the surface is simply drawn.
        let open = Anim::opening(now, false);
        assert!(open.frame(now).is_some_and(|p| p < 0.001));
        assert!(open.frame(now + Duration::from_millis(60)).is_some_and(|p| p > 0.0 && p < 1.0));
        assert_eq!(open.frame(later), None);
        // Closing: it starts with the surface fully there, and that first
        // frame already belongs to the transition (else no frame of it is
        // asked for and the surface vanishes at the end without fading).
        let close = Anim::closing(now, false);
        assert!(close.frame(now).is_some_and(|p| p > 0.999));
        assert!(close.frame(now + Duration::from_millis(100)).is_some_and(|p| p > 0.0 && p < 1.0));
        // Over, it stays invisible until the surface is removed.
        assert!(close.frame(later).is_some_and(|p| p < 0.001));
        // Reduced motion: nothing moves.
        assert_eq!(Anim::opening(now, true).frame(now), None);
        assert_eq!(Anim::closing(now, true).frame(now), Some(0.0));
    }

    #[test]
    fn text_fields_edit_at_the_caret() {
        let mut field = TextField::default();
        assert!(field.insert("ab\u{8}c\n\u{1b}"));
        assert_eq!(field.text(), "abc");
        assert!(!field.insert("\r\n"));
        assert!(field.key(Keysym::Left, false));
        assert!(field.insert("X"));
        assert_eq!(field.text(), "abXc");
        assert!(field.key(Keysym::Home, false));
        assert!(!field.backspace(), "nothing before the caret");
        assert!(field.delete());
        assert_eq!(field.text(), "bXc");
        assert!(field.key(Keysym::End, false));
        assert!(field.insert("नमस्ते"));
        assert!(field.backspace());
        assert!(!field.key(Keysym::F5, false));
        field.clear();
        assert!(field.is_empty() && !field.backspace() && field.cursor() == 0);
        field.insert(&"x".repeat(1000));
        assert_eq!(field.len(), TextField::MAX_CHARS);
        let taken = field.take();
        assert_eq!(taken.len(), TextField::MAX_CHARS);
        assert!(field.is_empty());
        field.set("hello");
        assert_eq!((field.text(), field.cursor()), ("hello", 5));
        assert!(field.key(Keysym::u, true));
        assert!(field.is_empty());
    }

    #[test]
    fn rect_helpers() {
        let r = Rect::new(0.0, 0.0, 10.0, 4.0).inset(3.0);
        assert_eq!((r.w, r.h), (4.0, 0.0));
        let a = Rect::new(0.0, 0.0, 10.0, 10.0);
        assert_eq!(a.center(), (5.0, 5.0));
        assert_eq!((a.right(), a.bottom()), (10.0, 10.0));
    }

    #[test]
    fn widgets_register_what_they_draw() {
        let focus = Action::Menu { level: 0, index: 1 };
        let (canvas, hits) = testing::render(300.0, 120.0, 1.0, Scheme::Dark, Some((20.0, 20.0)), Some(&focus), |f| {
            let t = f.button(Rect::new(8.0, 8.0, 80.0, 32.0), "OK", ButtonKind::Accent, true, Action::Menu { level: 0, index: 0 });
            assert!(t.hovered && !t.focused);
            let t = f.button(Rect::new(100.0, 8.0, 80.0, 32.0), "Cancel", ButtonKind::Standard, true, Action::Menu { level: 0, index: 1 });
            assert!(t.focused && !t.hovered);
            f.button(Rect::new(200.0, 8.0, 80.0, 32.0), "Off", ButtonKind::Standard, false, Action::None);
            f.toggle(8.0, 60.0, true, Action::Menu { level: 0, index: 2 });
            f.slider(Rect::new(60.0, 54.0, 200.0, 32.0), 0.25, true, Action::Menu { level: 0, index: 3 });
            let mut field = TextField::default();
            field.insert("secret");
            f.text_box(Rect::new(8.0, 90.0, 200.0, 28.0), &field, "Password", true, true, 8.0, 8.0);
        });
        assert_eq!(hits.len(), 4, "a disabled button is not a control");
        assert!(hits[3].drag);
        assert!(testing::hits_are_inside(&hits, 300.0, 120.0));
        assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0));
    }
}
