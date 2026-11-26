//! Boswas glass for the desktop shell and the compositor's own surfaces.
//!
//! The material of the Boswas applications (`material.glass` in the token
//! file; the Qt toolkit's `glass.py` and `paint.glass`), drawn the same way
//! here so that the shell, the compositor and the applications are one
//! design:
//!
//! * **The backdrop.** What lies behind a surface, blurred by `blur`
//!   logical pixels with its colours saturated by `saturation`, covered
//!   with a tint and a fine grain (`noise`) so that wide soft areas do not
//!   band. Where the compositor blurs what really lies behind a surface
//!   (live blur, OpenGL ES), the backdrop is that; elsewhere it is the
//!   blurred wallpaper ([`crate::material::Backdrop`]).
//! * **Legibility.** A tint's opacity in the token file is a minimum: it is
//!   raised in steps of [`LEGIBILITY_STEP`] until every text colour keeps
//!   4.5:1 on the darkest and the brightest parts of the backdrop, also
//!   under cards and the hover and pressed layers ([`legible_opacity`]). Over
//!   live blur the backdrop can be anything; the compositor keeps its
//!   brightness within the scheme's [`luminosity_band`] (as acrylic's
//!   luminosity layer does), and the shell makes its glass legible over the
//!   two ends of that band.
//! * **Surfaces** lie on the backdrop as translucent glass: their tint at
//!   its opacity, a white inner glow below the top edge and a hairline that
//!   is the white highlight at the top (brighter under the pointer) and a
//!   soft shadow at the bottom ([`draw_edges`]).
//! * **Off.** With transparency effects off or a high-contrast preset every
//!   surface is its opaque tint ([`Glass::opaque`]).

use std::{cell::RefCell, rc::Rc};

use tiny_skia::{FilterQuality, Pattern, Pixmap, SpreadMode, Transform};

use crate::{
    canvas::{Canvas, Corners},
    material::{Backdrop, Recipe},
    tokens::{tokens, Rgba, Scheme, Theme, TEXT_CONTRAST},
};

/// Where a glass surface's backdrop comes from.
pub enum Source<'a> {
    /// Glass is off (transparency effects off, a high-contrast preset):
    /// opaque tints.
    Off,
    /// The compositor blurs what lies behind the surface: only the tint is
    /// drawn, made legible over the luminosity band.
    Live,
    /// The wallpaper, blurred here.
    Wallpaper(&'a mut Backdrop),
    /// No backdrop is known yet: the opaque tint, as with glass off.
    Unknown,
}

impl Source<'_> {
    /// Whether the surface is translucent.
    pub fn translucent(&self) -> bool {
        matches!(self, Source::Live | Source::Wallpaper(_))
    }
}

thread_local! {
    /// Opacities found legible lately: (theme, kind, extremes) and the
    /// opacity. A surface is drawn again on every hover change.
    static LEGIBLE: RefCell<Vec<(usize, Kind, Vec<Rgba>, f32)>> = const { RefCell::new(Vec::new()) };
    /// The grain tile of each noise opacity (in ten-thousandths).
    static GRAINS: RefCell<Vec<(u32, Rc<Pixmap>)>> = const { RefCell::new(Vec::new()) };
}

/// [`legible_opacity`], remembered for the last few backdrops.
pub fn legible_opacity_kept(theme: &Theme, glass: &Glass, kind: Kind, extremes: &[Rgba]) -> f32 {
    let id = std::ptr::from_ref(theme) as usize;
    if let Some(found) = LEGIBLE.with(|kept| kept.borrow().iter().find(|(t, k, e, _)| *t == id && *k == kind && e == extremes).map(|entry| entry.3)) {
        return found;
    }
    let alpha = legible_opacity(theme, glass, kind, extremes);
    LEGIBLE.with(|kept| {
        let mut kept = kept.borrow_mut();
        if kept.len() >= 16 {
            kept.remove(0);
        }
        kept.push((id, kind, extremes.to_vec(), alpha));
    });
    alpha
}

/// The grain tile of a noise opacity, made once.
pub fn grain(noise: f32) -> Option<Rc<Pixmap>> {
    let key = (noise.clamp(0.0, 1.0) * 10000.0).round() as u32;
    if key == 0 {
        return None;
    }
    GRAINS.with(|kept| {
        let mut kept = kept.borrow_mut();
        if let Some((_, tile)) = kept.iter().find(|(k, _)| *k == key) {
            return Some(tile.clone());
        }
        let tile = Rc::new(grain_tile(noise)?);
        kept.push((key, tile.clone()));
        Some(tile)
    })
}

/// The opacities a kind is drawn with over a source: the backdrop layer
/// (`tint`, raised for legibility) and, for flyouts, the flyout glass on it.
pub fn layers(theme: &Theme, glass: &Glass, kind: Kind, extremes: &[Rgba]) -> [(Rgba, f32); 2] {
    let none = (Rgba::TRANSPARENT, 0.0);
    match kind {
        Kind::Backdrop | Kind::Taskbar => {
            let (tint, _) = glass.layer(kind);
            [(tint, legible_opacity_kept(theme, glass, kind, extremes)), none]
        }
        Kind::Flyout => {
            let backdrop = legible_opacity_kept(theme, glass, Kind::Backdrop, extremes);
            [(glass.tint, backdrop), (glass.flyout_tint, glass.flyout)]
        }
        Kind::Card | Kind::Pane => [glass.layer(kind), none],
    }
}

/// A whole glass surface: its backdrop from `source`, its tint (raised for
/// legibility), the grain, the inner glow and the hairline; with glass off
/// the opaque tint and the plain surface outline. `origin` is where the
/// canvas's top-left corner lies on the output (logical pixels), for the
/// wallpaper. The shell and the compositor draw every glass surface with
/// this.
#[allow(clippy::too_many_arguments)]
pub fn paint(canvas: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radii: Corners, origin: (f32, f32), theme: &Theme, kind: Kind, source: Source<'_>) {
    let radius = radii.iter().copied().fold(0.0f32, f32::max);
    let translucent = source.translucent();
    fill(canvas, x, y, w, h, radii, origin, theme, kind, source);
    match Glass::of(theme).filter(|_| translucent) {
        Some(glass) => draw_edges(canvas, x, y, w, h, radius, &glass, kind, false),
        None => canvas.stroke_rounded_rect(x, y, w, h, radius, 1.0, tokens().stroke(theme.scheme, "surface")),
    }
}

/// The fill of a glass surface (see [`paint`]) without its edges: what lies
/// under content that is drawn again (a scrolling page).
#[allow(clippy::too_many_arguments)]
pub fn fill(canvas: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radii: Corners, origin: (f32, f32), theme: &Theme, kind: Kind, source: Source<'_>) {
    let Some(glass) = Glass::of(theme).filter(|_| source.translucent()) else {
        let tint = Glass::of(theme).map_or(theme.surface, |g| g.opaque(kind));
        canvas.fill_rounded_rect4(x, y, w, h, radii, tint);
        return;
    };
    match source {
        Source::Wallpaper(backdrop) => {
            let extremes = backdrop.extremes(glass.blur, glass.saturation);
            let recipe = Recipe {
                blur: glass.blur,
                saturation: glass.saturation,
                layers: layers(theme, &glass, kind, &extremes),
                noise: glass.noise,
            };
            backdrop.fill_recipe(canvas, x, y, w, h, radii, origin, &recipe);
        }
        _ => {
            for (tint, opacity) in layers(theme, &glass, kind, &band_extremes(theme.scheme)) {
                if opacity > 0.0 {
                    canvas.fill_rounded_rect4(x, y, w, h, radii, tint.alpha(opacity));
                }
            }
            if let Some(tile) = grain(glass.noise) {
                draw_grain(canvas, x, y, w, h, radii, &tile);
            }
        }
    }
}

/// The opacity of a tint is raised in steps of this much until text on it is
/// legible.
pub const LEGIBILITY_STEP: f32 = 0.02;
/// Logical pixels below the top edge that the inner glow reaches at most.
pub const GLOW_HEIGHT: f32 = 36.0;
/// How far down the sides the top highlight runs before it is the plain edge.
pub const HIGHLIGHT_DEPTH: f32 = 10.0;
/// Device pixels of the grain pattern, tiled.
pub const GRAIN_TILE: u32 = 64;

/// The kinds of glass surface.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Kind {
    /// The window backdrop: what an application window shows behind its
    /// content, and the panels of the shell that stand on the desktop
    /// (Start, quick settings, the notification centre, the calendar, the
    /// widgets board, the clipboard and emoji panels, Task View's strip).
    Backdrop,
    /// The taskbar.
    Taskbar,
    /// Menus, tooltips, toasts, the volume and brightness indicator,
    /// dialogs and the compositor's overlays: the flyout glass over the
    /// backdrop.
    Flyout,
    /// Cards and rows on a panel.
    Card,
    /// Navigation panes and side bars.
    Pane,
}

/// The glass of one scheme, resolved from the token file.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Glass {
    pub scheme: Scheme,
    /// Blur of the backdrop in logical pixels (a Gaussian of sigma blur/2).
    pub blur: f32,
    pub saturation: f32,
    /// Opacity of the grain.
    pub noise: f32,
    pub tint: Rgba,
    pub tint_opacity: f32,
    pub taskbar_tint: Rgba,
    pub taskbar: f32,
    pub pane_tint: Rgba,
    pub pane: f32,
    pub card_tint: Rgba,
    pub card: f32,
    pub flyout_tint: Rgba,
    pub flyout: f32,
    pub highlight: f32,
    pub highlight_hover: f32,
    pub glow: f32,
    pub shadow: f32,
    /// The surface outline of the scheme: the hairline between the
    /// highlight and the shadow.
    pub stroke: Rgba,
}

impl Glass {
    /// The glass of `theme`'s scheme; `None` when the token file has no
    /// `material.glass` (every surface is then opaque).
    pub fn of(theme: &Theme) -> Option<Glass> {
        let t = tokens();
        let s = theme.scheme;
        let number = |key: &str| t.glass_number(s, key);
        let tint = |key: &str| t.glass_tint(theme, key);
        Some(Glass {
            scheme: s,
            blur: number("blur")?,
            saturation: number("saturation").unwrap_or(1.0),
            noise: number("noise").unwrap_or(0.0),
            tint: tint("tint")?,
            tint_opacity: number("tint-opacity")?,
            taskbar_tint: tint("taskbar-tint").unwrap_or(theme.chrome),
            taskbar: number("taskbar").or_else(|| number("tint-opacity"))?,
            pane_tint: tint("pane-tint").unwrap_or(theme.background),
            pane: number("pane")?,
            card_tint: tint("card-tint").unwrap_or(theme.surface),
            card: number("card")?,
            flyout_tint: tint("flyout-tint").unwrap_or(theme.surface),
            flyout: number("flyout")?,
            highlight: number("highlight").unwrap_or(0.0),
            highlight_hover: number("highlight-hover").unwrap_or(0.0),
            glow: number("glow").unwrap_or(0.0),
            shadow: number("shadow").unwrap_or(0.0),
            stroke: t.stroke(s, "surface"),
        })
    }

    /// A kind's tint and the minimum opacity of it.
    pub fn layer(&self, kind: Kind) -> (Rgba, f32) {
        match kind {
            Kind::Backdrop => (self.tint, self.tint_opacity),
            Kind::Taskbar => (self.taskbar_tint, self.taskbar),
            Kind::Flyout => (self.flyout_tint, self.flyout),
            Kind::Card => (self.card_tint, self.card),
            Kind::Pane => (self.pane_tint, self.pane),
        }
    }

    /// What a kind is with glass off: its tint, opaque.
    pub fn opaque(&self, kind: Kind) -> Rgba {
        let (tint, _) = self.layer(kind);
        Rgba { a: 255, ..tint }
    }
}

/// `under` covered by `over` at `alpha` (opaque colours, gamma-encoded
/// channels, as the toolkit's `blend`).
pub fn blend(under: Rgba, over: Rgba, alpha: f32) -> Rgba {
    let mixed = under.mix(over, alpha);
    Rgba { a: 255, ..mixed }
}

/// The brightness (relative luminance) the compositor's live blur keeps a
/// backdrop within: the darkest and the brightest it lets through. Dark
/// glass over a white window then shows a mid grey, light glass over a
/// black one a light grey, and translucent glass stays legible over any
/// window.
pub fn luminosity_band(scheme: Scheme) -> (f32, f32) {
    match scheme {
        Scheme::Dark => (0.0, 0.07),
        Scheme::Light => (0.55, 1.0),
    }
}

/// The two ends of [`luminosity_band`] as greys.
pub fn band_extremes(scheme: Scheme) -> [Rgba; 2] {
    let (lo, hi) = luminosity_band(scheme);
    [grey_of_luminance(lo, true), grey_of_luminance(hi, false)]
}

/// The grey of a relative luminance: the brightest grey at or below it
/// (`at_most`) or the darkest at or above it.
pub fn grey_of_luminance(luminance: f32, at_most: bool) -> Rgba {
    let grey = |v: u8| Rgba::rgb(v, v, v);
    if at_most {
        (0..=255u8).rev().map(grey).find(|g| g.luminance() <= luminance + 1e-6).unwrap_or(Rgba::BLACK)
    } else {
        (0..=255u8).map(grey).find(|g| g.luminance() >= luminance - 1e-6).unwrap_or(Rgba::WHITE)
    }
}

/// Whether text keeps 4.5:1 on `surface` and on the glass and state layers
/// that lie on it: body text (`text`, `text_secondary`) on all of them,
/// muted and accent text on the surface and on cards.
pub fn legible(theme: &Theme, glass: &Glass, surface: Rgba) -> bool {
    let t = tokens();
    let card = blend(surface, glass.card_tint, glass.card);
    let layers = |base: Rgba| {
        ["subtle-hover", "subtle-pressed"].map(|name| {
            let layer = t.fill(theme.scheme, name);
            blend(base, Rgba { a: 255, ..layer }, f32::from(layer.a) / 255.0)
        })
    };
    let surfaces = [surface, card];
    let mut all = surfaces.to_vec();
    for base in surfaces {
        all.extend(layers(base));
    }
    let body = [theme.text, theme.text_secondary];
    let coloured = [theme.text_muted, theme.accent];
    body.iter().all(|text| all.iter().all(|s| text.contrast(*s) >= TEXT_CONTRAST))
        && coloured.iter().all(|text| surfaces.iter().all(|s| text.contrast(*s) >= TEXT_CONTRAST))
}

/// The opacity of a kind's tint over backdrops whose extreme colours are
/// `extremes`: the token's minimum, raised in steps of [`LEGIBILITY_STEP`]
/// until [`legible`] holds over each of them (1.0, opaque, at most).
pub fn legible_opacity(theme: &Theme, glass: &Glass, kind: Kind, extremes: &[Rgba]) -> f32 {
    let (tint, minimum) = glass.layer(kind);
    let mut alpha = minimum.clamp(0.0, 1.0);
    while alpha < 1.0 {
        if extremes.iter().all(|e| legible(theme, glass, blend(*e, tint, alpha))) {
            return (alpha * 10000.0).round() / 10000.0;
        }
        alpha = (alpha + LEGIBILITY_STEP).min(1.0);
    }
    1.0
}

/// A tile of fine noise: white and black specks of up to twice `opacity`
/// (premultiplied RGBA), the same pattern in every process.
pub fn grain_tile(opacity: f32) -> Option<Pixmap> {
    let mut tile = Pixmap::new(GRAIN_TILE, GRAIN_TILE)?;
    let mut state: u32 = 0x6B05_2F1D;
    let mut next = || {
        // xorshift32
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        state as f32 / u32::MAX as f32
    };
    for pixel in tile.data_mut().chunks_exact_mut(4) {
        let alpha = (next() * 2.0 * opacity.clamp(0.0, 0.5) * 255.0).round() as u8;
        let level = if next() < 0.5 { alpha } else { 0 };
        pixel.copy_from_slice(&[level, level, level, alpha]);
    }
    Some(tile)
}

/// Covers a rounded rectangle with the grain (tiled on the device pixel
/// grid, so that it stays fine at every scale).
#[allow(clippy::too_many_arguments)]
pub fn draw_grain(canvas: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radii: Corners, tile: &Pixmap) {
    let scale = canvas.scale();
    let paint = Pattern::new(tile.as_ref(), SpreadMode::Repeat, FilterQuality::Nearest, 1.0, Transform::from_scale(1.0 / scale, 1.0 / scale));
    canvas.fill_rounded_rect_shader(x, y, w, h, radii, paint);
}

/// The inner glow below the top edge and the hairline of a glass surface
/// (`hover`: the brighter highlight of a surface under the pointer). Panes
/// have no glow.
#[allow(clippy::too_many_arguments)]
pub fn draw_edges(canvas: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radius: f32, glass: &Glass, kind: Kind, hover: bool) {
    if !(w > 1.0 && h > 1.0) {
        return;
    }
    let radius = radius.clamp(0.0, w.min(h) / 2.0);
    let radii = [radius; 4];
    if kind != Kind::Pane && glass.glow > 0.0 {
        let reach = (h * 0.6).min(GLOW_HEIGHT);
        let glow = Rgba::WHITE.alpha(glass.glow);
        canvas.vertical_gradient_rounded_rect(x, y, w, h, radii, None, &[(0.0, glow), ((reach / h).min(1.0), glow.alpha(0.0))]);
    }
    draw_hairline(canvas, x, y, w, h, radius, glass, hover);
}

/// The hairline of glass alone: the white highlight at the top (brighter
/// under the pointer) running into the plain outline and a soft shadow at
/// the bottom. A control under the pointer on a glass surface gets it.
#[allow(clippy::too_many_arguments)]
pub fn draw_hairline(canvas: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radius: f32, glass: &Glass, hover: bool) {
    if !(w > 1.0 && h > 1.0) {
        return;
    }
    let radii = [radius.clamp(0.0, w.min(h) / 2.0); 4];
    let top = Rgba::WHITE.alpha(if hover { glass.highlight_hover } else { glass.highlight });
    let depth = (HIGHLIGHT_DEPTH / h.max(1.0)).min(0.45);
    let bottom = Rgba::BLACK.alpha(glass.shadow);
    canvas.vertical_gradient_rounded_rect(x, y, w, h, radii, Some(1.0), &[(0.0, top), (depth, glass.stroke), (1.0, bottom)]);
}

/// A whole glass surface without a backdrop: the tint at `opacity`, the
/// grain, the glow and the hairline. With `opacity` 1.0 (glass off) only the
/// opaque tint and the plain surface outline are drawn.
#[allow(clippy::too_many_arguments)]
pub fn draw_surface(canvas: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radius: f32, glass: &Glass, kind: Kind, opacity: f32, grain: Option<&Pixmap>) {
    let (tint, _) = glass.layer(kind);
    canvas.fill_rounded_rect(x, y, w, h, radius, tint.alpha(opacity));
    if opacity >= 1.0 {
        canvas.stroke_rounded_rect(x, y, w, h, radius, 1.0, glass.stroke);
        return;
    }
    if let Some(tile) = grain {
        draw_grain(canvas, x, y, w, h, [radius; 4], tile);
    }
    draw_edges(canvas, x, y, w, h, radius, glass, kind, false);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn both_schemes_have_glass_and_it_grows_more_opaque_over_content() {
        for theme in [Theme::dark(), Theme::light()] {
            let g = Glass::of(theme).expect("the token file has material.glass");
            assert!(g.pane < g.card && g.card < g.flyout, "{g:?}");
            assert!(g.highlight <= g.highlight_hover);
            assert_eq!(g.tint, theme.background);
            assert_eq!(g.taskbar_tint, theme.chrome);
            assert!(g.blur > 0.0 && (1.0..=2.0).contains(&g.saturation) && g.noise > 0.0);
            assert_eq!(g.opaque(Kind::Card).a, 255);
        }
    }

    #[test]
    fn opacity_is_raised_only_as_far_as_text_needs() {
        let theme = Theme::dark();
        let g = Glass::of(theme).unwrap();
        // Over black nothing needs raising; over white dark glass becomes
        // nearly opaque; the result is always legible.
        assert_eq!(legible_opacity(theme, &g, Kind::Backdrop, &[Rgba::BLACK]), g.tint_opacity);
        let white = legible_opacity(theme, &g, Kind::Backdrop, &[Rgba::WHITE]);
        assert!(white > 0.9, "{white}");
        for extremes in [[Rgba::BLACK, Rgba::WHITE], [Rgba::rgb(90, 140, 200), Rgba::rgb(20, 10, 40)]] {
            for theme in [Theme::dark(), Theme::light()] {
                let g = Glass::of(theme).unwrap();
                for kind in [Kind::Backdrop, Kind::Taskbar] {
                    let alpha = legible_opacity(theme, &g, kind, &extremes);
                    let (tint, minimum) = g.layer(kind);
                    assert!(alpha >= minimum);
                    for e in extremes {
                        assert!(alpha >= 1.0 || legible(theme, &g, blend(e, tint, alpha)), "{kind:?} {e:?} {alpha}");
                    }
                }
            }
        }
    }

    #[test]
    fn the_luminosity_band_keeps_glass_translucent_and_legible() {
        for theme in [Theme::dark(), Theme::light()] {
            let g = Glass::of(theme).unwrap();
            let extremes = band_extremes(theme.scheme);
            let (lo, hi) = luminosity_band(theme.scheme);
            assert!(extremes[0].luminance() <= lo + 1e-6 && extremes[1].luminance() >= hi - 1e-6);
            for kind in [Kind::Backdrop, Kind::Taskbar] {
                let alpha = legible_opacity(theme, &g, kind, &extremes);
                let most = if kind == Kind::Taskbar { 0.86 } else { 0.8 };
                assert!(alpha <= most, "{:?} {kind:?}: glass over live blur stays translucent ({alpha})", theme.scheme);
            }
        }
    }

    #[test]
    fn the_grain_is_fine_and_the_same_every_time() {
        let a = grain_tile(0.012).unwrap();
        assert_eq!(a.data(), grain_tile(0.012).unwrap().data());
        let max = a.data().chunks_exact(4).map(|p| p[3]).max().unwrap();
        assert!(max > 0 && max <= 7, "specks of at most twice the opacity: {max}");
        assert!(a.data().chunks_exact(4).all(|p| p[0] <= p[3]), "premultiplied");
    }

    #[test]
    fn edges_light_the_top_and_shade_the_bottom() {
        let theme = Theme::dark();
        let g = Glass::of(theme).unwrap();
        for scale in [1.0, 2.0] {
            let mut canvas = Canvas::new(100.0, 60.0, scale).unwrap();
            canvas.clear(Rgba::rgb(40, 40, 40));
            draw_edges(&mut canvas, 0.0, 0.0, 100.0, 60.0, 8.0, &g, Kind::Card, false);
            let px = |x: f32, y: f32| canvas.pixmap().pixel((x * scale) as u32, (y * scale) as u32).unwrap();
            let top = px(50.0, 0.0);
            let bottom = px(50.0, 59.5);
            let middle = px(50.0, 45.0);
            assert!(top.red() > 50, "a light hairline on top: {top:?}");
            assert!(bottom.red() < 40, "a shade at the bottom: {bottom:?}");
            assert_eq!(middle.red(), 40, "nothing in the middle below the glow");
            let glow = px(50.0, 4.0);
            assert!(glow.red() > 40, "the glow under the top edge: {glow:?}");
        }
    }
}
