//! Pointer cursors: the XCursor theme, with a built-in arrow as fallback.

use std::{collections::HashMap, io::Read, time::Duration};

use boswas_ui::{Canvas, Rgba};
use smithay::{
    backend::{allocator::Fourcc, renderer::element::memory::MemoryRenderBuffer},
    input::pointer::CursorIcon,
    utils::{Logical, Point, Transform},
};
use tracing::debug;

/// Largest cursor file that is read (animated cursors are a few megabytes).
const MAX_CURSOR_BYTES: u64 = 16 * 1024 * 1024;

/// One frame of a cursor.
pub struct Frame {
    pub buffer: MemoryRenderBuffer,
    pub hotspot: Point<i32, Logical>,
    delay: u32,
}

/// Cursor images, loaded from the XCursor theme on first use.
pub struct CursorTheme {
    theme: xcursor::CursorTheme,
    size: u32,
    loaded: HashMap<&'static str, Option<Vec<Frame>>>,
    fallback: Vec<Frame>,
}

impl CursorTheme {
    /// `name` empty: `XCURSOR_THEME`, else the `default` theme.
    pub fn load(name: &str, size: u32) -> Self {
        let name = if name.is_empty() {
            std::env::var("XCURSOR_THEME").unwrap_or_else(|_| "default".into())
        } else {
            name.to_owned()
        };
        Self {
            theme: xcursor::CursorTheme::load(&name),
            size,
            loaded: HashMap::new(),
            fallback: vec![builtin_arrow()],
        }
    }

    /// The frame of `icon` to show `elapsed` after the cursor appeared.
    pub fn frame(&mut self, icon: CursorIcon, elapsed: Duration) -> &Frame {
        let name = icon.name();
        if !self.loaded.contains_key(name) {
            let frames = std::iter::once(name)
                .chain(icon.alt_names().iter().copied())
                .find_map(|candidate| self.load_icon(candidate));
            if frames.is_none() {
                debug!("no cursor image for \"{name}\"; using the built-in arrow");
            }
            self.loaded.insert(name, frames);
        }
        let frames = self.loaded.get(name).and_then(Option::as_ref).unwrap_or(&self.fallback);
        let total: u32 = frames.iter().map(|f| f.delay).sum();
        if total == 0 {
            return &frames[0];
        }
        let mut at = (elapsed.as_millis() % u128::from(total)) as u32;
        for frame in frames {
            if at < frame.delay {
                return frame;
            }
            at -= frame.delay;
        }
        &frames[0]
    }

    /// Whether `icon` is animated (the output then needs continuous frames).
    pub fn is_animated(&self, icon: CursorIcon) -> bool {
        self.loaded
            .get(icon.name())
            .and_then(Option::as_ref)
            .is_some_and(|frames| frames.len() > 1)
    }

    fn load_icon(&self, name: &str) -> Option<Vec<Frame>> {
        let path = self.theme.load_icon(name)?;
        let file = std::fs::File::open(path).ok()?;
        let mut bytes = Vec::new();
        file.take(MAX_CURSOR_BYTES).read_to_end(&mut bytes).ok()?;
        let images = xcursor::parser::parse_xcursor(&bytes)?;
        // The nominal size closest to the configured one.
        let nearest = images.iter().map(|i| i.size).min_by_key(|s| s.abs_diff(self.size))?;
        let frames: Vec<Frame> = images
            .into_iter()
            .filter(|image| image.size == nearest)
            .filter(|image| {
                image.width > 0
                    && image.height > 0
                    && image.width <= 512
                    && image.height <= 512
                    && image.pixels_rgba.len() == (image.width * image.height * 4) as usize
            })
            .map(|image| Frame {
                buffer: MemoryRenderBuffer::from_slice(
                    &image.pixels_rgba,
                    Fourcc::Abgr8888,
                    (image.width as i32, image.height as i32),
                    1,
                    Transform::Normal,
                    None,
                ),
                hotspot: Point::from((image.xhot as i32, image.yhot as i32)),
                delay: image.delay,
            })
            .collect();
        (!frames.is_empty()).then_some(frames)
    }
}

/// A plain arrow, drawn in code, for systems without a cursor theme.
fn builtin_arrow() -> Frame {
    let mut canvas = Canvas::new(24.0, 24.0, 1.0).expect("a 24 px canvas");
    let outline = [
        (2.0, 2.0),
        (2.0, 18.5),
        (6.5, 14.5),
        (9.5, 21.0),
        (12.5, 19.6),
        (9.6, 13.4),
        (15.5, 13.4),
        (2.0, 2.0),
    ];
    let mut pb = tiny_skia::PathBuilder::new();
    pb.move_to(outline[0].0, outline[0].1);
    for p in &outline[1..] {
        pb.line_to(p.0, p.1);
    }
    pb.close();
    if let Some(path) = pb.finish() {
        canvas.fill_path(&path, Rgba::rgb(0x12, 0x10, 0x1C));
        canvas.stroke_path(&path, 1.5, Rgba::WHITE);
    }
    let mut bytes = vec![0u8; 24 * 24 * 4];
    canvas.write_argb8888(&mut bytes);
    Frame {
        buffer: MemoryRenderBuffer::from_slice(&bytes, Fourcc::Argb8888, (24, 24), 1, Transform::Normal, None),
        hotspot: Point::from((2, 2)),
        delay: 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_missing_theme_falls_back_to_the_builtin_arrow() {
        let mut theme = CursorTheme::load("boswas-test-theme-that-does-not-exist", 24);
        let frame = theme.frame(CursorIcon::Default, Duration::ZERO);
        assert_eq!(frame.hotspot, Point::from((2, 2)));
        assert!(!theme.is_animated(CursorIcon::Default));
        // Asking again uses the cached miss.
        let _ = theme.frame(CursorIcon::Default, Duration::from_secs(5));
        let _ = theme.frame(CursorIcon::Wait, Duration::from_millis(123));
    }
}
