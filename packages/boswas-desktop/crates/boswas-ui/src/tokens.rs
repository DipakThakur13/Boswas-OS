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
    /// Corner radius of windows and cards (logical pixels).
    pub radius_window: f32,
    /// Corner radius of controls.
    pub radius_control: f32,
    /// Corner radius of cards and highlighted items.
    pub radius_card: f32,
    /// Corner radius of flyouts (launcher, menus).
    pub radius_flyout: f32,
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
        Ok(Self {
            dark: theme(Scheme::Dark)?,
            light: theme(Scheme::Light)?,
            radius_window: number(&["radius", "window"])? as f32,
            radius_control: number(&["radius", "control"])? as f32,
            radius_card: number(&["radius", "card"])? as f32,
            radius_flyout: number(&["radius", "flyout"])? as f32,
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
