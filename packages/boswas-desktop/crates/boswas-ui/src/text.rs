//! Text shaping and rasterisation with the system fonts.
//!
//! Shaping (HarfBuzz-compatible, through cosmic-text) covers Latin and the
//! Indic scripts alike, with per-script font fallback. Fonts are read from
//! the system font directories; nothing is downloaded.

use cosmic_text::{Attrs, Buffer, Color, Family, FontSystem, Metrics, Shaping, SwashCache, Weight};

use crate::canvas::Canvas;
use crate::tokens::{tokens, Rgba};

/// Which typeface a run of text uses.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Default)]
pub enum Face {
    /// The UI face (the first installed family of `type.family`).
    #[default]
    Ui,
    /// The brand display face (Orbitron); short labels only.
    Display,
}

/// How a run of text is drawn.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TextStyle {
    /// Font size in logical pixels.
    pub size: f32,
    /// CSS-style weight (400 regular, 500 medium, 600 semibold, 700 bold).
    pub weight: u16,
    pub color: Rgba,
    pub face: Face,
}

impl TextStyle {
    pub fn new(size: f32, color: Rgba) -> Self {
        Self {
            size,
            weight: 400,
            color,
            face: Face::Ui,
        }
    }

    #[must_use]
    pub fn weight(mut self, weight: u16) -> Self {
        self.weight = weight;
        self
    }

    #[must_use]
    pub fn face(mut self, face: Face) -> Self {
        self.face = face;
        self
    }

    /// Height of one line box.
    pub fn line_height(&self) -> f32 {
        (self.size * 1.35).round()
    }
}

/// Shapes, measures and draws text. Creating one scans the system fonts, so
/// keep a single engine per process.
pub struct TextEngine {
    fonts: FontSystem,
    cache: SwashCache,
    ui_family: Option<String>,
    display_family: Option<String>,
    /// Widths already measured, by text and style.
    widths: std::collections::HashMap<(String, u32, u16, Face), f32>,
}

/// Measured widths kept before the cache is emptied.
const MAX_WIDTHS: usize = 4096;

impl Default for TextEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl TextEngine {
    pub fn new() -> Self {
        let fonts = FontSystem::new();
        let installed = |name: &str| {
            fonts
                .db()
                .faces()
                .any(|face| face.families.iter().any(|(family, _)| family.eq_ignore_ascii_case(name)))
        };
        let t = tokens();
        let ui_family = t.font_families.iter().find(|name| installed(name)).cloned();
        let display_family = Some(t.display_family.clone()).filter(|name| installed(name));
        Self {
            fonts,
            cache: SwashCache::new(),
            ui_family,
            display_family,
            widths: std::collections::HashMap::new(),
        }
    }

    /// The UI family in use, when one of the preferred families is installed.
    pub fn ui_family(&self) -> Option<&str> {
        self.ui_family.as_deref()
    }

    fn shape(&mut self, text: &str, style: &TextStyle, scale: f32) -> Buffer {
        let size = (style.size * scale).max(1.0);
        let metrics = Metrics::new(size, (style.line_height() * scale).max(1.0));
        let mut buffer = Buffer::new(&mut self.fonts, metrics);
        let family = match style.face {
            Face::Ui => self.ui_family.as_deref(),
            Face::Display => self.display_family.as_deref().or(self.ui_family.as_deref()),
        };
        let attrs = Attrs::new()
            .family(family.map_or(Family::SansSerif, Family::Name))
            .weight(Weight(style.weight));
        buffer.set_size(&mut self.fonts, None, None);
        buffer.set_text(&mut self.fonts, text, attrs, Shaping::Advanced);
        buffer.shape_until_scroll(&mut self.fonts, false);
        buffer
    }

    fn buffer_width(buffer: &Buffer) -> f32 {
        buffer.layout_runs().map(|run| run.line_w).fold(0.0, f32::max)
    }

    /// Width and line height of a single line of text, in logical pixels.
    pub fn measure(&mut self, text: &str, style: &TextStyle) -> (f32, f32) {
        if text.is_empty() {
            return (0.0, style.line_height());
        }
        let text = first_line(text);
        let key = (text.to_owned(), style.size.to_bits(), style.weight, style.face);
        if let Some(width) = self.widths.get(&key) {
            return (*width, style.line_height());
        }
        let buffer = self.shape(text, style, 1.0);
        let width = Self::buffer_width(&buffer).ceil();
        if self.widths.len() >= MAX_WIDTHS {
            self.widths.clear();
        }
        self.widths.insert(key, width);
        (width, style.line_height())
    }

    /// Breaks `text` into at most `max_lines` lines no wider than
    /// `max_width`, at spaces where possible. Line breaks in the text are
    /// kept; what does not fit ends in an ellipsis.
    pub fn wrap(&mut self, text: &str, style: &TextStyle, max_width: f32, max_lines: usize) -> Vec<String> {
        let mut lines: Vec<String> = Vec::new();
        if max_lines == 0 || max_width <= 0.0 {
            return lines;
        }
        let mut truncated = false;
        'paragraphs: for paragraph in text.split('\n') {
            let paragraph: String = paragraph.chars().filter(|c| !c.is_control()).collect();
            let mut current = String::new();
            for word in paragraph.split_whitespace() {
                let candidate = if current.is_empty() { word.to_owned() } else { format!("{current} {word}") };
                if self.measure(&candidate, style).0 <= max_width {
                    current = candidate;
                    continue;
                }
                if !current.is_empty() {
                    if lines.len() + 1 >= max_lines {
                        current = candidate;
                        truncated = true;
                        lines.push(current);
                        break 'paragraphs;
                    }
                    lines.push(std::mem::take(&mut current));
                }
                // A single word wider than the line is cut by eliding.
                current = word.to_owned();
            }
            if lines.len() >= max_lines {
                truncated = true;
                break;
            }
            if !current.is_empty() || lines.is_empty() {
                lines.push(current);
            }
        }
        if let Some(last) = lines.last_mut() {
            if truncated && self.measure(last, style).0 <= max_width {
                last.push('…');
            }
            *last = self.elide(last, style, max_width);
        }
        for line in &mut lines {
            if self.measure(line, style).0 > max_width {
                *line = self.elide(line, style, max_width);
            }
        }
        lines
    }

    /// The longest prefix of `text` (plus an ellipsis) that fits `max_width`.
    pub fn elide(&mut self, text: &str, style: &TextStyle, max_width: f32) -> String {
        let text = first_line(text);
        if self.measure(text, style).0 <= max_width {
            return text.to_owned();
        }
        let chars: Vec<char> = text.chars().collect();
        let (mut lo, mut hi) = (0usize, chars.len());
        while lo < hi {
            let mid = (lo + hi).div_ceil(2);
            let candidate: String = chars[..mid].iter().collect::<String>() + "…";
            if self.measure(&candidate, style).0 <= max_width {
                lo = mid;
            } else {
                hi = mid - 1;
            }
        }
        if lo == 0 {
            return String::new();
        }
        let mut out: String = chars[..lo].iter().collect();
        out.truncate(out.trim_end().len());
        out.push('…');
        out
    }

    /// Draws one line of text with its line box's top-left corner at
    /// (`x`, `y`). Text wider than `max_width` is elided. Returns the width
    /// drawn, in logical pixels.
    pub fn draw(
        &mut self,
        canvas: &mut Canvas,
        text: &str,
        x: f32,
        y: f32,
        max_width: Option<f32>,
        style: &TextStyle,
    ) -> f32 {
        let elided;
        let text = match max_width {
            Some(width) => {
                elided = self.elide(text, style, width);
                elided.as_str()
            }
            None => first_line(text),
        };
        if text.is_empty() {
            return 0.0;
        }
        let scale = canvas.scale();
        let buffer = self.shape(text, style, scale);
        let width = Self::buffer_width(&buffer) / scale;
        let (ox, oy) = ((x * scale).round() as i32, (y * scale).round() as i32);
        let c = style.color;
        buffer.draw(
            &mut self.fonts,
            &mut self.cache,
            Color::rgba(c.r, c.g, c.b, c.a),
            |gx, gy, w, h, color| {
                // Colour glyphs (emoji) arrive with their own colour; outline
                // glyphs arrive in the text colour with coverage as alpha.
                let pixel = Rgba::rgba(color.r(), color.g(), color.b(), 255);
                for dy in 0..h as i32 {
                    for dx in 0..w as i32 {
                        canvas.blend_pixel(ox + gx + dx, oy + gy + dy, pixel, color.a());
                    }
                }
            },
        );
        width.ceil()
    }

    /// Draws one line centred in the logical rectangle.
    pub fn draw_centered(&mut self, canvas: &mut Canvas, text: &str, x: f32, y: f32, w: f32, h: f32, style: &TextStyle) {
        let fitted = self.elide(text, style, w);
        let (tw, th) = self.measure(&fitted, style);
        self.draw(canvas, &fitted, x + (w - tw) / 2.0, y + (h - th) / 2.0, None, style);
    }
}

/// Labels are single-line: anything after a line break is dropped, and
/// control characters never reach the shaper.
fn first_line(text: &str) -> &str {
    let end = text.find(|c: char| c == '\n' || c == '\r' || (c.is_control() && c != '\t'));
    match end {
        Some(end) => &text[..end],
        None => text,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn labels_stop_at_line_breaks_and_control_characters() {
        assert_eq!(first_line("one\ntwo"), "one");
        assert_eq!(first_line("a\u{1b}[31m"), "a");
        assert_eq!(first_line("plain"), "plain");
        assert_eq!(first_line(""), "");
    }

    #[test]
    fn measuring_and_eliding_are_consistent() {
        let mut engine = TextEngine::new();
        let style = TextStyle::new(14.0, Rgba::WHITE);
        let (w, h) = engine.measure("Boswas OS", &style);
        assert_eq!(h, style.line_height());
        if w == 0.0 {
            // No fonts on this machine: nothing to shape, nothing to check.
            return;
        }
        let (wide, _) = engine.measure("Boswas OS Boswas OS", &style);
        assert!(wide > w);
        let elided = engine.elide("Boswas OS Compatibility Manager", &style, w);
        assert!(elided.ends_with('…'), "{elided}");
        assert!(engine.measure(&elided, &style).0 <= w);
        assert_eq!(engine.elide("Boswas OS", &style, w + 1.0), "Boswas OS");
        assert_eq!(engine.elide("Boswas OS", &style, 0.5), "");
    }

    #[test]
    fn wrapping_respects_width_and_line_count() {
        let mut engine = TextEngine::new();
        let style = TextStyle::new(14.0, Rgba::WHITE);
        let text = "The quarterly report is ready for review and has been shared with the whole team today";
        let (full, _) = engine.measure(text, &style);
        if full == 0.0 {
            return;
        }
        let width = full / 3.0;
        let lines = engine.wrap(text, &style, width, 10);
        assert!(lines.len() >= 3, "{lines:?}");
        assert!(lines.iter().all(|l| engine.measure(l, &style).0 <= width), "{lines:?}");
        assert_eq!(lines.join(" "), text, "nothing is lost when there is room");
        let two = engine.wrap(text, &style, width, 2);
        assert_eq!(two.len(), 2);
        assert!(two[1].ends_with('…'), "{two:?}");
        assert!(engine.measure(&two[1], &style).0 <= width);
        assert_eq!(engine.wrap("one\ntwo", &style, 500.0, 4), ["one", "two"]);
        assert!(engine.wrap("x", &style, 500.0, 0).is_empty());
        assert_eq!(engine.wrap("", &style, 500.0, 3), [""]);
    }

    #[test]
    fn drawing_puts_ink_inside_the_canvas_only() {
        let mut engine = TextEngine::new();
        let style = TextStyle::new(16.0, Rgba::WHITE).weight(600);
        let mut canvas = Canvas::new(200.0, 30.0, 1.0).unwrap();
        let width = engine.draw(&mut canvas, "Boswas", 4.0, 4.0, Some(190.0), &style);
        if width == 0.0 {
            return;
        }
        let inked = canvas.data().chunks_exact(4).filter(|p| p[3] > 0).count();
        assert!(inked > 20, "text left {inked} pixels");
        // Far off-canvas drawing must not panic.
        engine.draw(&mut canvas, "Boswas", 5000.0, -5000.0, None, &style);
    }
}
