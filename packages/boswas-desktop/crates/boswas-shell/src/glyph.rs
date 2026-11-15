//! Small line glyphs drawn in code: the marks the shell needs that are not
//! themed icons (chevrons, media transport, check marks). They are drawn
//! with a 1.5 px round stroke inside a square, like the symbolic icons.

use boswas_ui::{Canvas, Rgba};
use tiny_skia::PathBuilder;

use crate::ui::Rect;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Glyph {
    ChevronRight,
    ChevronLeft,
    ChevronUp,
    ChevronDown,
    ArrowLeft,
    ArrowRight,
    Close,
    Check,
    Dot,
    Play,
    Pause,
    Next,
    Previous,
    /// Two overlapping windows: Task View.
    TaskView,
    /// Power saver.
    Leaf,
    /// Balanced power.
    Gauge,
    /// Performance.
    Bolt,
}

/// Draws `glyph` centred in `rect` (a square is expected).
pub fn draw(canvas: &mut Canvas, glyph: Glyph, rect: Rect, color: Rgba) {
    let s = rect.w.min(rect.h);
    let (ox, oy) = (rect.x + (rect.w - s) / 2.0, rect.y + (rect.h - s) / 2.0);
    // Points are given on a 16 x 16 grid.
    let p = |x: f32, y: f32| (ox + x * s / 16.0, oy + y * s / 16.0);
    let width = (1.5 * s / 16.0).clamp(1.2, 2.5);
    let line = |canvas: &mut Canvas, points: &[(f32, f32)]| {
        let mapped: Vec<(f32, f32)> = points.iter().map(|&(x, y)| p(x, y)).collect();
        canvas.polyline(&mapped, width, color);
    };
    let fill = |canvas: &mut Canvas, points: &[(f32, f32)]| {
        let mut pb = PathBuilder::new();
        for (i, &(x, y)) in points.iter().enumerate() {
            let (px, py) = p(x, y);
            if i == 0 {
                pb.move_to(px, py);
            } else {
                pb.line_to(px, py);
            }
        }
        pb.close();
        if let Some(path) = pb.finish() {
            canvas.fill_path(&path, color);
        }
    };
    match glyph {
        Glyph::ChevronRight => line(canvas, &[(6.0, 3.5), (10.5, 8.0), (6.0, 12.5)]),
        Glyph::ChevronLeft => line(canvas, &[(10.0, 3.5), (5.5, 8.0), (10.0, 12.5)]),
        Glyph::ChevronUp => line(canvas, &[(3.5, 10.0), (8.0, 5.5), (12.5, 10.0)]),
        Glyph::ChevronDown => line(canvas, &[(3.5, 6.0), (8.0, 10.5), (12.5, 6.0)]),
        Glyph::ArrowLeft => {
            line(canvas, &[(13.0, 8.0), (3.0, 8.0)]);
            line(canvas, &[(7.0, 4.0), (3.0, 8.0), (7.0, 12.0)]);
        }
        Glyph::ArrowRight => {
            line(canvas, &[(3.0, 8.0), (13.0, 8.0)]);
            line(canvas, &[(9.0, 4.0), (13.0, 8.0), (9.0, 12.0)]);
        }
        Glyph::Close => {
            line(canvas, &[(4.0, 4.0), (12.0, 12.0)]);
            line(canvas, &[(12.0, 4.0), (4.0, 12.0)]);
        }
        Glyph::Check => line(canvas, &[(3.5, 8.5), (6.5, 11.5), (12.5, 4.5)]),
        Glyph::Dot => {
            let (cx, cy) = p(8.0, 8.0);
            canvas.fill_circle(cx, cy, 3.0 * s / 16.0, color);
        }
        Glyph::Play => fill(canvas, &[(5.0, 3.0), (13.0, 8.0), (5.0, 13.0)]),
        Glyph::Pause => {
            fill(canvas, &[(4.5, 3.5), (7.0, 3.5), (7.0, 12.5), (4.5, 12.5)]);
            fill(canvas, &[(9.0, 3.5), (11.5, 3.5), (11.5, 12.5), (9.0, 12.5)]);
        }
        Glyph::Next => {
            fill(canvas, &[(3.5, 3.5), (10.5, 8.0), (3.5, 12.5)]);
            line(canvas, &[(12.0, 4.0), (12.0, 12.0)]);
        }
        Glyph::Previous => {
            fill(canvas, &[(12.5, 3.5), (5.5, 8.0), (12.5, 12.5)]);
            line(canvas, &[(4.0, 4.0), (4.0, 12.0)]);
        }
        Glyph::TaskView => {
            let (x, y) = p(2.0, 5.0);
            canvas.stroke_rounded_rect(x, y, 9.0 * s / 16.0, 8.0 * s / 16.0, 1.5 * s / 16.0, width, color);
            line(canvas, &[(6.0, 3.0), (13.0, 3.0), (14.0, 4.0), (14.0, 10.0)]);
        }
        Glyph::Leaf => {
            line(canvas, &[(3.0, 13.0), (5.0, 7.0), (9.0, 4.0), (13.0, 3.0), (12.0, 7.5), (9.0, 11.0), (5.0, 12.0), (3.0, 13.0)]);
            line(canvas, &[(3.0, 13.0), (9.0, 7.0)]);
        }
        Glyph::Gauge => {
            line(canvas, &[(2.5, 11.5), (3.0, 8.0), (5.0, 5.0), (8.0, 4.0), (11.0, 5.0), (13.0, 8.0), (13.5, 11.5)]);
            line(canvas, &[(8.0, 11.0), (10.5, 7.0)]);
        }
        Glyph::Bolt => fill(canvas, &[(9.5, 1.5), (3.5, 9.0), (7.5, 9.0), (6.5, 14.5), (12.5, 7.0), (8.5, 7.0)]),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_glyph_leaves_ink_inside_its_square() {
        let all = [
            Glyph::ChevronRight, Glyph::ChevronLeft, Glyph::ChevronUp, Glyph::ChevronDown, Glyph::ArrowLeft,
            Glyph::ArrowRight, Glyph::Close, Glyph::Check, Glyph::Dot, Glyph::Play, Glyph::Pause, Glyph::Next,
            Glyph::Previous, Glyph::TaskView, Glyph::Leaf, Glyph::Gauge, Glyph::Bolt,
        ];
        for glyph in all {
            for scale in [1.0, 2.0] {
                let mut canvas = Canvas::new(32.0, 32.0, scale).unwrap();
                draw(&mut canvas, glyph, Rect::new(8.0, 8.0, 16.0, 16.0), Rgba::WHITE);
                let (w, _) = canvas.pixel_size();
                let mut inked = 0;
                for (i, p) in canvas.data().chunks_exact(4).enumerate() {
                    if p[3] > 0 {
                        inked += 1;
                        let (x, y) = ((i as u32 % w) as f32 / scale, (i as u32 / w) as f32 / scale);
                        assert!((6.0..=26.0).contains(&x) && (6.0..=26.0).contains(&y), "{glyph:?} drew at {x},{y}");
                    }
                }
                assert!(inked > 8, "{glyph:?} drew {inked} pixels");
            }
        }
    }
}
