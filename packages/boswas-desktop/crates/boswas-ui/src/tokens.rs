//! Boswas UI design tokens.
//!
//! `desktop/ui/tokens.json` is the single source of truth for colours, radii,
//! type and motion. It is compiled into the binary, so no file is read (or
//! trusted) at run time.

use std::sync::OnceLock;

use serde_json::Value;

const TOKENS_JSON: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../../desktop/ui/tokens.json"
));

/// A straight (not premultiplied) 8-bit RGBA colour.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Rgba {
    pub r: u8,
    pub g: u8,
    pub b: u8,
    pub a: u8,
}

impl Rgba {
    pub const TRANSPARENT: Rgba = Rgba::rgba(0, 0, 0, 0);
    pub const WHITE: Rgba = Rgba::rgb(255, 255, 255);
    pub const BLACK: Rgba = Rgba::rgb(0, 0, 0);

    pub const fn rgb(r: u8, g: u8, b: u8) -> Self {
        Self { r, g, b, a: 255 }
    }

    pub const fn rgba(r: u8, g: u8, b: u8, a: u8) -> Self {
        Self { r, g, b, a }
    }

    /// Parses `#RRGGBB` or `#RRGGBBAA`.
    pub fn from_hex(text: &str) -> Option<Self> {
        let hex = text.strip_prefix('#')?;
        if !hex.is_ascii() || (hex.len() != 6 && hex.len() != 8) {
            return None;
        }
        let byte = |i: usize| u8::from_str_radix(&hex[i..i + 2], 16).ok();
        Some(Self {
            r: byte(0)?,
            g: byte(2)?,
            b: byte(4)?,
            a: if hex.len() == 8 { byte(6)? } else { 255 },
        })
    }

    /// The same colour with its alpha multiplied by `opacity` (0.0 to 1.0).
    #[must_use]
    pub fn alpha(self, opacity: f32) -> Self {
        let a = (f32::from(self.a) * opacity.clamp(0.0, 1.0)).round() as u8;
        Self { a, ..self }
    }

    /// Linear interpolation towards `other` (`t` from 0.0 to 1.0).
    #[must_use]
    pub fn mix(self, other: Rgba, t: f32) -> Self {
        let t = t.clamp(0.0, 1.0);
        let lerp = |a: u8, b: u8| (f32::from(a) + (f32::from(b) - f32::from(a)) * t).round() as u8;
        Self {
            r: lerp(self.r, other.r),
            g: lerp(self.g, other.g),
            b: lerp(self.b, other.b),
            a: lerp(self.a, other.a),
        }
    }

    /// Straight RGBA as floats, as renderers take clear and solid colours.
    pub fn to_f32(self) -> [f32; 4] {
        [
            f32::from(self.r) / 255.0,
            f32::from(self.g) / 255.0,
            f32::from(self.b) / 255.0,
            f32::from(self.a) / 255.0,
        ]
    }

    /// Premultiplied RGBA as floats.
    pub fn to_premultiplied_f32(self) -> [f32; 4] {
        let [r, g, b, a] = self.to_f32();
        [r * a, g * a, b * a, a]
    }

    /// WCAG relative luminance (alpha ignored).
    pub fn luminance(self) -> f32 {
        let channel = |c: u8| {
            let c = f32::from(c) / 255.0;
            if c <= 0.03928 {
                c / 12.92
            } else {
                ((c + 0.055) / 1.055).powf(2.4)
            }
        };
        0.2126 * channel(self.r) + 0.7152 * channel(self.g) + 0.0722 * channel(self.b)
    }

    /// WCAG contrast ratio between two opaque colours (1.0 to 21.0).
    pub fn contrast(self, other: Rgba) -> f32 {
        let (a, b) = (self.luminance(), other.luminance());
        let (hi, lo) = if a > b { (a, b) } else { (b, a) };
        (hi + 0.05) / (lo + 0.05)
    }
}

impl From<Rgba> for tiny_skia::Color {
    fn from(c: Rgba) -> Self {
        tiny_skia::Color::from_rgba8(c.r, c.g, c.b, c.a)
    }
}

/// Light or dark appearance.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Scheme {
    #[default]
    Dark,
    Light,
}

impl Scheme {
    fn key(self) -> &'static str {
        match self {
            Scheme::Dark => "dark",
            Scheme::Light => "light",
        }
    }
}

/// The semantic colours of one scheme, resolved from the palette.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Theme {
    pub scheme: Scheme,
    pub background: Rgba,
    pub surface: Rgba,
    pub surface_raised: Rgba,
    pub surface_hover: Rgba,
    /// The tint of the taskbar and of window chrome.
    pub chrome: Rgba,
    pub border: Rgba,
    pub text: Rgba,
    pub text_secondary: Rgba,
    pub text_muted: Rgba,
    pub accent: Rgba,
    pub accent_hover: Rgba,
    pub accent_pressed: Rgba,
    pub on_accent: Rgba,
    pub focus: Rgba,
    pub success: Rgba,
    pub warning: Rgba,
    pub danger: Rgba,
    pub info: Rgba,
}

impl Theme {
    pub fn dark() -> &'static Theme {
        &tokens().dark
    }

    pub fn light() -> &'static Theme {
        &tokens().light
    }

    pub fn of(scheme: Scheme) -> &'static Theme {
        match scheme {
            Scheme::Dark => Theme::dark(),
            Scheme::Light => Theme::light(),
        }
    }

    fn of_tokens(tokens: &Tokens, scheme: Scheme) -> &Theme {
        match scheme {
            Scheme::Dark => &tokens.dark,
            Scheme::Light => &tokens.light,
        }
    }

    /// The theme of `scheme` with another hue of the palette as its accent
    /// (one of [`ACCENT_HUES`]; any other name gives the token file's own
    /// accent). The accent takes the same shades of the hue as the token
    /// file's accent takes of its own, and is moved towards the text colour
    /// just far enough for text on it to keep 4.5:1, as the Qt toolkit
    /// derives its accent fills.
    pub fn accented(scheme: Scheme, hue: &str) -> &'static Theme {
        tokens()
            .accented
            .iter()
            .find(|(name, _, _)| *name == hue)
            .map(|(_, dark, light)| match scheme {
                Scheme::Dark => dark,
                Scheme::Light => light,
            })
            .unwrap_or_else(|| Theme::of(scheme))
    }
}

/// The hues of the palette that can be the accent colour. The first is the
/// token file's own accent.
pub const ACCENT_HUES: [&str; 8] = ["iris", "azure", "sky", "mint", "sun", "coral", "orchid", "slate"];

/// The contrast text on the accent keeps (WCAG 2.1, 1.4.3).
pub const TEXT_CONTRAST: f32 = 4.5;

/// `colour`, moved towards `towards` in steps of 5 % until it has
/// [`TEXT_CONTRAST`] against `other`; unchanged when it already has.
pub fn readable(colour: Rgba, other: Rgba, towards: Rgba) -> Rgba {
    if colour.contrast(other) >= TEXT_CONTRAST {
        return colour;
    }
    let mut candidate = colour;
    for step in 1..=20 {
        candidate = colour.mix(towards, step as f32 * 0.05);
        if candidate.contrast(other) >= TEXT_CONTRAST {
            break;
        }
    }
    candidate
}

/// A translucent surface drawn from the blurred wallpaper under a tint.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Material {
    /// The tint colour (opaque).
    pub tint: Rgba,
    /// How much of the tint covers the blurred wallpaper (0.0 to 1.0).
    pub tint_opacity: f32,
    /// Blur radius of the wallpaper in logical pixels.
    pub blur: f32,
}

impl Material {
    /// The colour to use where no wallpaper is available.
    pub fn fallback(&self) -> Rgba {
        self.tint
    }
}

/// One step of the type ramp.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TypeStyle {
    pub size: f32,
    pub line_height: f32,
    pub weight: u16,
}

/// Shadow of one elevation level.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Elevation {
    pub blur: f32,
    pub y: f32,
    pub alpha: f32,
}

/// The parsed token file.
#[derive(Debug)]
pub struct Tokens {
    raw: Value,
    pub dark: Theme,
    pub light: Theme,
    /// The themes with another accent: (hue, dark, light).
    accented: Vec<(&'static str, Theme, Theme)>,
    /// Corner radius of windows and cards (logical pixels).
    pub radius_window: f32,
    /// Corner radius of controls.
    pub radius_control: f32,
    /// Corner radius of cards and highlighted items.
    pub radius_card: f32,
    /// Corner radius of flyouts (Start, menus, quick settings).
    pub radius_flyout: f32,
    /// Corner radius of tooltips.
    pub radius_tooltip: f32,
    /// The spacing grid unit.
    pub spacing: f32,
    pub motion_fast_ms: u32,
    pub motion_normal_ms: u32,
    pub motion_slow_ms: u32,
    /// UI font families, most preferred first.
    pub font_families: Vec<String>,
    pub display_family: String,
    /// The fixed type scale, smallest first.
    pub type_scale: Vec<f32>,
}

/// The compiled-in tokens. Panics only if `tokens.json` itself is malformed,
/// which the unit tests of this crate catch at build time.
pub fn tokens() -> &'static Tokens {
    static TOKENS: OnceLock<Tokens> = OnceLock::new();
    TOKENS.get_or_init(|| Tokens::parse(TOKENS_JSON).expect("desktop/ui/tokens.json is valid"))
}

impl Tokens {
    fn parse(text: &str) -> Result<Self, String> {
        let raw: Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
        let number = |path: &[&str]| -> Result<f64, String> {
            let mut v = &raw;
            for key in path {
                v = v.get(key).ok_or_else(|| format!("missing {}", path.join(".")))?;
            }
            v.as_f64().ok_or_else(|| format!("{} is not a number", path.join(".")))
        };
        let theme = |scheme: Scheme| -> Result<Theme, String> {
            let get = |name: &str| -> Result<Rgba, String> {
                let reference = raw["semantic"][scheme.key()][name]
                    .as_str()
                    .ok_or_else(|| format!("missing semantic.{}.{name}", scheme.key()))?;
                resolve(&raw, reference).ok_or_else(|| format!("unknown colour {reference}"))
            };
            Ok(Theme {
                scheme,
                background: get("background")?,
                surface: get("surface")?,
                surface_raised: get("surface-raised")?,
                surface_hover: get("surface-hover")?,
                chrome: get("chrome")?,
                border: get("border")?,
                text: get("text")?,
                text_secondary: get("text-secondary")?,
                text_muted: get("text-muted")?,
                accent: get("accent")?,
                accent_hover: get("accent-hover")?,
                accent_pressed: get("accent-pressed")?,
                on_accent: get("on-accent")?,
                focus: get("focus")?,
                success: get("success")?,
                warning: get("warning")?,
                danger: get("danger")?,
                info: get("info")?,
            })
        };
        let strings = |v: &Value| -> Vec<String> {
            v.as_array()
                .map(|a| a.iter().filter_map(|s| s.as_str().map(str::to_owned)).collect())
                .unwrap_or_default()
        };
        let (dark, light) = (theme(Scheme::Dark)?, theme(Scheme::Light)?);
        let accented = |base: &Theme, hue: &str| -> Option<Theme> {
            let shade = |role: &str| -> Option<Rgba> {
                let reference = raw["semantic"][base.scheme.key()][role].as_str()?;
                let (_, shade) = reference.split_once('.')?;
                resolve(&raw, &format!("{hue}.{shade}"))
            };
            Some(Theme {
                accent: readable(shade("accent")?, base.on_accent, base.text),
                accent_hover: shade("accent-hover")?,
                accent_pressed: shade("accent-pressed")?,
                ..*base
            })
        };
        let accented = ACCENT_HUES
            .iter()
            .map(|hue| Ok((*hue, accented(&dark, hue).ok_or(format!("no accent shades of {hue}"))?, accented(&light, hue).ok_or(format!("no accent shades of {hue}"))?)))
            .collect::<Result<Vec<_>, String>>()?;
        Ok(Self {
            dark,
            light,
            accented,
            radius_window: number(&["radius", "window"])? as f32,
            radius_control: number(&["radius", "control"])? as f32,
            radius_card: number(&["radius", "card"])? as f32,
            radius_flyout: number(&["radius", "flyout"])? as f32,
            radius_tooltip: number(&["radius", "tooltip"])? as f32,
            spacing: number(&["spacing", "unit"])? as f32,
            motion_fast_ms: number(&["motion", "fast-ms"])? as u32,
            motion_normal_ms: number(&["motion", "normal-ms"])? as u32,
            motion_slow_ms: number(&["motion", "slow-ms"])? as u32,
            font_families: strings(&raw["type"]["family"]),
            display_family: raw["type"]["display-family"].as_str().unwrap_or("Orbitron").to_owned(),
            type_scale: raw["type"]["scale"]
                .as_array()
                .map(|a| a.iter().filter_map(Value::as_f64).map(|v| v as f32).collect())
                .unwrap_or_default(),
            raw,
        })
    }

    /// A palette colour by reference: `"iris.500"`, `"white"` or `"#RRGGBB"`.
    pub fn color(&self, reference: &str) -> Option<Rgba> {
        resolve(&self.raw, reference)
    }

    /// The shadow of an elevation level (`desktop`, `window`, `flyout`, `dialog`).
    pub fn elevation(&self, level: &str) -> Elevation {
        let e = &self.raw["elevation"][level];
        Elevation {
            blur: e["blur"].as_f64().unwrap_or(0.0) as f32,
            y: e["y"].as_f64().unwrap_or(0.0) as f32,
            alpha: e["alpha"].as_f64().unwrap_or(0.0) as f32,
        }
    }

    /// A one-pixel outline colour: `control`, `surface`, `divider` or
    /// `window`. Translucent, to be drawn over the surface it outlines.
    pub fn stroke(&self, scheme: Scheme, kind: &str) -> Rgba {
        self.layer("stroke", scheme, kind)
    }

    /// A state layer of a control: `control`, `control-hover`,
    /// `control-pressed`, `subtle-hover`, `subtle-pressed` or `disabled`.
    /// Translucent, to be drawn over the surface the control sits on.
    pub fn fill(&self, scheme: Scheme, kind: &str) -> Rgba {
        self.layer("fill", scheme, kind)
    }

    fn layer(&self, group: &str, scheme: Scheme, kind: &str) -> Rgba {
        let values = &self.raw[group][scheme.key()];
        let color = values["color"].as_str().and_then(|r| self.color(r)).unwrap_or(Rgba::WHITE);
        color.alpha(values[kind].as_f64().unwrap_or(0.0) as f32)
    }

    /// A material: `mica`, `acrylic`, `chrome` or `smoke`.
    pub fn material(&self, scheme: Scheme, name: &str) -> Material {
        let m = &self.raw["material"][name];
        let theme = Theme::of_tokens(self, scheme);
        let tint = match m["tint"].as_str() {
            Some("@background") => theme.background,
            Some("@surface") => theme.surface,
            Some("@surface-raised") => theme.surface_raised,
            Some("@chrome") => theme.chrome,
            Some(reference) => self.color(reference).unwrap_or(theme.background),
            None => theme.background,
        };
        Material {
            tint,
            tint_opacity: m["tint-opacity"].as_f64().unwrap_or(1.0) as f32,
            blur: m["blur"].as_f64().unwrap_or(0.0) as f32,
        }
    }

    /// A fixed size of the shell or the window frames (`shell` section),
    /// in logical pixels: `taskbar-height`, `titlebar-height`, ...
    pub fn shell(&self, key: &str) -> f32 {
        self.raw["shell"][key].as_f64().unwrap_or(0.0) as f32
    }

    /// A `[width, height]` size of the `shell` section: `caption-button`,
    /// `taskbar-indicator`, ...
    pub fn shell_size(&self, key: &str) -> (f32, f32) {
        let v = &self.raw["shell"][key];
        (v[0].as_f64().unwrap_or(0.0) as f32, v[1].as_f64().unwrap_or(0.0) as f32)
    }

    /// A colour of the `shell` section (`caption-close-hover`, ...).
    pub fn shell_color(&self, key: &str) -> Rgba {
        self.raw["shell"][key].as_str().and_then(|r| self.color(r)).unwrap_or(Rgba::BLACK)
    }

    /// A step of the type ramp: `caption`, `body`, `body-strong`,
    /// `body-large`, `subtitle`, `title` or `title-large`.
    pub fn type_style(&self, name: &str) -> TypeStyle {
        let v = &self.raw["type"]["ramp"][name];
        TypeStyle {
            size: v[0].as_f64().unwrap_or(14.0) as f32,
            line_height: v[1].as_f64().unwrap_or(20.0) as f32,
            weight: v[2].as_u64().unwrap_or(400) as u16,
        }
    }

    /// The control points of a motion curve: `decelerate` (things that
    /// enter), `accelerate` (things that leave) or `standard`.
    pub fn curve(&self, name: &str) -> [f32; 4] {
        let v = &self.raw["motion"]["curves"][name];
        let at = |i: usize| v[i].as_f64().unwrap_or(if i == 3 { 1.0 } else { 0.0 }) as f32;
        [at(0), at(1), at(2), at(3)]
    }

    /// Tile gradient, glyph and accent colours of an icon hue.
    pub fn icon_hue(&self, hue: &str) -> Option<[Rgba; 4]> {
        let h = &self.raw["icon"]["hues"][hue];
        let get = |k: &str| h[k].as_str().and_then(|r| self.color(r));
        Some([get("from")?, get("to")?, get("glyph")?, get("accent")?])
    }
}

fn resolve(raw: &Value, reference: &str) -> Option<Rgba> {
    if reference.starts_with('#') {
        return Rgba::from_hex(reference);
    }
    let colors = &raw["color"];
    let value = match reference.split_once('.') {
        Some((family, shade)) => &colors[family][shade],
        None => &colors[reference],
    };
    Rgba::from_hex(value.as_str()?)
}

/// A CSS-style cubic Bézier easing: the curve through (0,0), (x1,y1),
/// (x2,y2) and (1,1), evaluated at time `t` (0.0 to 1.0).
pub fn cubic_bezier(points: [f32; 4], t: f32) -> f32 {
    let [x1, y1, x2, y2] = points;
    let t = t.clamp(0.0, 1.0);
    let bezier = |a: f32, b: f32, s: f32| {
        let inv = 1.0 - s;
        3.0 * inv * inv * s * a + 3.0 * inv * s * s * b + s * s * s
    };
    // Solve x(s) = t by bisection: x is monotonic for x1, x2 in 0..=1.
    let (mut lo, mut hi) = (0.0f32, 1.0f32);
    for _ in 0..24 {
        let mid = (lo + hi) / 2.0;
        if bezier(x1, x2, mid) < t {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    bezier(y1, y2, (lo + hi) / 2.0)
}

/// Ease-out cubic, the Boswas UI motion curve (`t` from 0.0 to 1.0).
pub fn ease_out_cubic(t: f32) -> f32 {
    let t = 1.0 - t.clamp(0.0, 1.0);
    1.0 - t * t * t
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_file_parses_and_resolves() {
        let t = tokens();
        assert_eq!(t.color("iris.500"), Rgba::from_hex("#7C5CFF"));
        assert_eq!(t.color("white"), Some(Rgba::WHITE));
        assert_eq!(t.color("nope.500"), None);
        assert_eq!(t.radius_window, 8.0);
        assert_eq!(t.radius_control, 4.0);
        assert_eq!(t.spacing, 4.0);
        assert_eq!(t.type_scale, vec![12.0, 14.0, 16.0, 20.0, 28.0, 40.0]);
        assert!((150..=250).contains(&t.motion_fast_ms));
        assert!((150..=250).contains(&t.motion_slow_ms));
        assert!(t.icon_hue("iris").is_some());
        assert!(!t.font_families.is_empty());
    }

    #[test]
    fn fluent_layers_materials_and_geometry_resolve() {
        let t = tokens();
        for scheme in [Scheme::Dark, Scheme::Light] {
            for kind in ["control", "surface", "divider", "window"] {
                let stroke = t.stroke(scheme, kind);
                assert!(stroke.a > 0 && stroke.a < 80, "{scheme:?} stroke {kind}: {stroke:?}");
            }
            for kind in ["control", "control-hover", "control-pressed", "subtle-hover", "subtle-pressed", "disabled"] {
                assert!(t.fill(scheme, kind).a > 0, "{scheme:?} fill {kind}");
            }
            for name in ["mica", "acrylic", "chrome", "smoke"] {
                let m = t.material(scheme, name);
                assert_eq!(m.tint.a, 255, "{name}");
                assert!(m.tint_opacity > 0.0 && m.tint_opacity <= 1.0, "{name}");
            }
            assert_eq!(t.material(scheme, "mica").tint, Theme::of(scheme).background);
            assert_eq!(t.material(scheme, "chrome").tint, Theme::of(scheme).chrome);
        }
        assert_eq!(t.shell("taskbar-height"), 48.0);
        assert_eq!(t.shell("titlebar-height"), 32.0);
        assert_eq!(t.shell_size("caption-button"), (46.0, 32.0));
        assert_eq!(t.shell_color("caption-close-hover"), Rgba::rgb(0xC4, 0x2B, 0x1C));
        assert_eq!(t.radius_flyout, t.radius_window);
        assert_eq!(t.type_style("body"), TypeStyle { size: 14.0, line_height: 20.0, weight: 400 });
        assert_eq!(t.type_style("subtitle").weight, 600);
    }

    #[test]
    fn bezier_curves_run_from_zero_to_one() {
        for name in ["decelerate", "accelerate", "standard"] {
            let curve = tokens().curve(name);
            assert!(cubic_bezier(curve, 0.0).abs() < 0.01, "{name}");
            assert!((cubic_bezier(curve, 1.0) - 1.0).abs() < 0.01, "{name}");
            let mut last = -0.01;
            for i in 0..=50 {
                let v = cubic_bezier(curve, i as f32 / 50.0);
                assert!(v >= last - 0.001, "{name} is monotonic");
                last = v;
            }
        }
        // Things that enter move fast first; things that leave move fast last.
        assert!(cubic_bezier(tokens().curve("decelerate"), 0.3) > 0.6);
        assert!(cubic_bezier(tokens().curve("accelerate"), 0.5) < 0.3);
    }

    #[test]
    fn retired_teal_is_not_in_the_palette() {
        let lower = TOKENS_JSON.to_ascii_lowercase();
        assert!(!lower.contains("#17c6c0") && !lower.contains("#2ed3cd"));
    }

    #[test]
    fn text_meets_wcag_aa_in_both_schemes() {
        for theme in [Theme::dark(), Theme::light()] {
            for surface in [theme.background, theme.surface, theme.surface_raised] {
                assert!(theme.text.contrast(surface) >= 7.0, "{theme:?}");
                assert!(theme.text_secondary.contrast(surface) >= 4.5, "{theme:?}");
                assert!(theme.text_muted.contrast(surface) >= 4.5, "{theme:?}");
            }
            assert!(theme.on_accent.contrast(theme.accent) >= 4.0, "{theme:?}");
            assert!(theme.accent.contrast(theme.background) >= 3.0, "{theme:?}");
        }
    }

    #[test]
    fn every_accent_keeps_the_contrast_rules() {
        for scheme in [Scheme::Dark, Scheme::Light] {
            assert_eq!(Theme::accented(scheme, "iris"), Theme::of(scheme), "the default hue is the token file's accent");
            assert_eq!(Theme::accented(scheme, "teal"), Theme::of(scheme), "an unknown hue is the default");
            let mut accents = Vec::new();
            for hue in ACCENT_HUES {
                let theme = Theme::accented(scheme, hue);
                assert_eq!(theme.scheme, scheme);
                assert!(theme.on_accent.contrast(theme.accent) >= TEXT_CONTRAST, "{scheme:?} {hue}: text on the accent");
                for surface in [theme.background, theme.surface, theme.chrome] {
                    assert!(theme.accent.contrast(surface) >= 3.0, "{scheme:?} {hue}: the accent as a mark");
                }
                assert_eq!((theme.text, theme.background, theme.on_accent), (Theme::of(scheme).text, Theme::of(scheme).background, Theme::of(scheme).on_accent));
                assert!(!accents.contains(&theme.accent), "{hue} has a colour of its own");
                accents.push(theme.accent);
            }
        }
        // A hue that needs it is moved towards the text colour, no further.
        let sun = Theme::accented(Scheme::Light, "sun");
        assert_ne!(Some(sun.accent), tokens().color("sun.600"));
        assert!(sun.on_accent.contrast(sun.accent) < TEXT_CONTRAST + 0.6);
        assert_eq!(Some(Theme::accented(Scheme::Dark, "azure").accent), tokens().color("azure.400"));
    }

    #[test]
    fn hex_parsing_rejects_bad_input() {
        assert_eq!(Rgba::from_hex("#0B0A12"), Some(Rgba::rgb(0x0B, 0x0A, 0x12)));
        assert_eq!(Rgba::from_hex("#0B0A1280"), Some(Rgba::rgba(0x0B, 0x0A, 0x12, 0x80)));
        for bad in ["", "#", "0B0A12", "#0B0A1", "#GGGGGG", "#ééé"] {
            assert_eq!(Rgba::from_hex(bad), None, "{bad}");
        }
    }

    #[test]
    fn easing_is_monotonic_and_bounded() {
        assert_eq!(ease_out_cubic(0.0), 0.0);
        assert_eq!(ease_out_cubic(1.0), 1.0);
        let mut last = 0.0;
        for i in 0..=100 {
            let v = ease_out_cubic(i as f32 / 100.0);
            assert!(v >= last);
            last = v;
        }
    }
}
