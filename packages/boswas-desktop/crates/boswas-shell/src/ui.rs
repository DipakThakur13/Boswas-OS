//! Immediate-mode drawing helpers shared by the shell's surfaces.
//!
//! A surface is redrawn completely whenever its model changes. While it is
//! drawn, widgets register the rectangles that react to the pointer
//! together with the action a click triggers; pointer events are resolved
//! against that list.

use std::rc::Rc;

use boswas_ui::{
    icons::IconLoader,
    text::{TextEngine, TextStyle},
    Canvas, Rgba, Theme,
};
use tiny_skia::Pixmap;

/// A rectangle in logical pixels.
#[derive(Clone, Copy, Debug, PartialEq)]
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
}

/// Shared resources of the shell: fonts, icons and the theme.
pub struct Resources {
    pub text: TextEngine,
    pub icons: IconLoader,
    pub theme: &'static Theme,
}

impl Resources {
    pub fn new(theme: &'static Theme) -> Self {
        let icon_theme = match theme.scheme {
            boswas_ui::Scheme::Dark => "Boswas-Prism",
            boswas_ui::Scheme::Light => "Boswas-Prism-Light",
        };
        Self {
            text: TextEngine::new(),
            icons: IconLoader::new(icon_theme),
            theme,
        }
    }
}

/// One drawing pass over a surface.
pub struct Frame<'a, A> {
    pub canvas: &'a mut Canvas,
    pub res: &'a mut Resources,
    /// Pointer position on this surface, if the pointer is over it.
    pub pointer: Option<(f32, f32)>,
    pub hits: &'a mut Vec<(Rect, A)>,
}

impl<A> Frame<'_, A> {
    pub fn theme(&self) -> &'static Theme {
        self.res.theme
    }

    /// Makes a rectangle clickable and reports whether the pointer is on it.
    pub fn hit(&mut self, rect: Rect, action: A) -> bool {
        self.hits.push((rect, action));
        self.pointer.is_some_and(|p| rect.contains(p))
    }

    pub fn text(&mut self, text: &str, x: f32, y: f32, max_width: Option<f32>, style: &TextStyle) -> f32 {
        self.res.text.draw(self.canvas, text, x, y, max_width, style)
    }

    pub fn text_centered(&mut self, text: &str, rect: Rect, style: &TextStyle) {
        self.res.text.draw_centered(self.canvas, text, rect.x, rect.y, rect.w, rect.h, style);
    }

    /// Draws a themed icon centred in a square of `size` logical pixels at
    /// (`x`, `y`). Returns whether the icon was found.
    pub fn icon(&mut self, name: &str, x: f32, y: f32, size: f32) -> bool {
        let pixels = (size * self.canvas.scale()).round().max(1.0) as u32;
        match self.res.icons.load(name, pixels) {
            Some(pixmap) => {
                self.draw_pixmap(&pixmap, x, y, size);
                true
            }
            None => false,
        }
    }

    /// The first of `names` that exists, or a neutral placeholder tile.
    pub fn icon_or_placeholder(&mut self, names: &[&str], x: f32, y: f32, size: f32) {
        if names.iter().any(|name| !name.is_empty() && self.icon(name, x, y, size)) {
            return;
        }
        let theme = self.theme();
        self.canvas.fill_rounded_rect(x, y, size, size, size * 0.24, theme.surface_hover);
        self.canvas.stroke_rounded_rect(x, y, size, size, size * 0.24, 1.0, theme.border);
    }

    pub fn draw_pixmap(&mut self, pixmap: &Rc<Pixmap>, x: f32, y: f32, size: f32) {
        self.canvas.draw_pixmap(pixmap, x, y, size, size, 1.0);
    }

    /// A rounded highlight behind a hovered or selected item.
    pub fn highlight(&mut self, rect: Rect, radius: f32, color: Rgba) {
        self.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, color);
    }
}

/// The action registered for a point, the topmost (last drawn) first.
pub fn action_at<A: Clone>(hits: &[(Rect, A)], point: (f32, f32)) -> Option<A> {
    hits.iter().rev().find(|(rect, _)| rect.contains(point)).map(|(_, action)| action.clone())
}

/// A single-line text field: typed text, optionally shown as dots.
#[derive(Default)]
pub struct TextField {
    text: String,
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

    /// Appends typed text, ignoring control characters. Returns whether the
    /// field changed.
    pub fn insert(&mut self, typed: &str) -> bool {
        let mut changed = false;
        for c in typed.chars().filter(|c| !c.is_control()) {
            if self.len() >= Self::MAX_CHARS {
                break;
            }
            self.text.push(c);
            changed = true;
        }
        changed
    }

    pub fn backspace(&mut self) -> bool {
        self.text.pop().is_some()
    }

    /// Empties the field, overwriting what it held.
    pub fn clear(&mut self) {
        use zeroize::Zeroize;
        self.text.zeroize();
    }

    /// Takes the text out, leaving the field empty.
    pub fn take(&mut self) -> zeroize::Zeroizing<String> {
        zeroize::Zeroizing::new(std::mem::take(&mut self.text))
    }
}

impl Drop for TextField {
    fn drop(&mut self) {
        self.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_topmost_hit_wins() {
        let hits = vec![(Rect::new(0.0, 0.0, 100.0, 100.0), "back"), (Rect::new(10.0, 10.0, 20.0, 20.0), "front")];
        assert_eq!(action_at(&hits, (15.0, 15.0)), Some("front"));
        assert_eq!(action_at(&hits, (50.0, 50.0)), Some("back"));
        assert_eq!(action_at(&hits, (100.0, 50.0)), None, "the right edge is outside");
        assert_eq!(action_at(&hits, (-1.0, 5.0)), None);
    }

    #[test]
    fn text_fields_reject_control_characters_and_overlong_input() {
        let mut field = TextField::default();
        assert!(field.insert("ab\u{8}c\n\u{1b}"));
        assert_eq!(field.text(), "abc");
        assert!(!field.insert("\r\n"));
        assert!(field.insert("नमस्ते"));
        assert!(field.backspace());
        field.clear();
        assert!(field.is_empty() && !field.backspace());
        field.insert(&"x".repeat(1000));
        assert_eq!(field.len(), TextField::MAX_CHARS);
        let taken = field.take();
        assert_eq!(taken.len(), TextField::MAX_CHARS);
        assert!(field.is_empty());
    }

    #[test]
    fn rect_inset_never_goes_negative() {
        let r = Rect::new(0.0, 0.0, 10.0, 4.0).inset(3.0);
        assert_eq!((r.w, r.h), (4.0, 0.0));
    }
}
