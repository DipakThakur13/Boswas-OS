//! The window switcher (Alt+Tab).
//!
//! Holding Alt and pressing Tab steps through the windows of the desktop,
//! most recently used first; releasing Alt goes to the selected one. While
//! Alt is held a panel in the middle of the screen shows a live preview of
//! every window with its icon and title. A quick Alt+Tab switches to the
//! previous window before the panel would have appeared.

use std::time::{Duration, Instant};

use boswas_ui::{text::TextStyle, tokens, Canvas};
use smithay::{
    backend::{
        allocator::Fourcc,
        renderer::{
            element::{
                memory::{MemoryRenderBuffer, MemoryRenderBufferRenderElement},
                Kind,
            },
            Texture,
        },
    },
    output::Output,
    utils::{Logical, Point, Rectangle, Scale, Size, Transform},
};

use super::element::WindowElement;
use crate::{
    effects::BoswasRenderer,
    render::{placed, OutputRenderElement},
    state::Boswas,
};

/// How long Alt must be held before the panel appears.
const APPEAR_AFTER: Duration = Duration::from_millis(120);
/// Height of a preview at its largest.
const PREVIEW_HEIGHT: i32 = 148;
const PREVIEW_MIN_WIDTH: i32 = 112;
const PREVIEW_MAX_WIDTH: i32 = 288;
/// Height of the icon and title above a preview.
const LABEL_HEIGHT: i32 = 32;
/// Space between a cell's edge and its content.
const CELL_PADDING: i32 = 8;
const PANEL_PADDING: i32 = 16;
/// Room for the panel's shadow in its buffer.
const SHADOW: i32 = 32;
/// Space kept free between the panel and the screen's edges.
const SCREEN_MARGIN: i32 = 48;

/// The switcher while Alt is held.
pub struct Switcher {
    /// Window ids in the order they are shown.
    pub order: Vec<u64>,
    pub index: usize,
    opened: Instant,
    /// The output the panel is shown on.
    output: String,
    /// Where each cell is on the desktop, for the pointer.
    cells: Vec<Rectangle<i32, Logical>>,
    panel: Option<(PanelKey, MemoryRenderBuffer)>,
}

impl Switcher {
    pub fn selected(&self) -> Option<u64> {
        self.order.get(self.index).copied()
    }

    /// The cell under a point on the desktop.
    pub fn cell_at(&self, point: Point<f64, Logical>) -> Option<usize> {
        self.cells.iter().position(|cell| cell.to_f64().contains(point))
    }
}

/// What the cached panel picture was drawn for.
#[derive(Clone, Debug, PartialEq)]
struct PanelKey {
    layout: Layout,
    titles: Vec<(String, String)>,
    selected: usize,
    scale: i32,
    light: bool,
}

/// Where the panel and its cells go.
#[derive(Clone, Debug, PartialEq)]
pub struct Layout {
    /// The panel, relative to the area it was laid out in.
    pub panel: Rectangle<i32, Logical>,
    /// The cells (label and preview), relative to the same area.
    pub cells: Vec<Rectangle<i32, Logical>>,
    pub preview_height: i32,
}

impl Layout {
    /// The preview's rectangle inside a cell.
    pub fn preview(&self, cell: Rectangle<i32, Logical>) -> Rectangle<i32, Logical> {
        Rectangle::new(
            cell.loc + Point::from((CELL_PADDING, LABEL_HEIGHT)),
            Size::from((cell.size.w - 2 * CELL_PADDING, self.preview_height)),
        )
    }
}

/// Lays the previews of windows with the given frame sizes out in rows,
/// centred in an area of `area` size. Previews shrink until the rows fit.
pub fn layout(area: Size<i32, Logical>, frames: &[Size<i32, Logical>]) -> Layout {
    let max_width = (area.w - 2 * SCREEN_MARGIN - 2 * PANEL_PADDING).max(PREVIEW_MIN_WIDTH + 2 * CELL_PADDING);
    let max_height = (area.h - 2 * SCREEN_MARGIN - 2 * PANEL_PADDING).max(LABEL_HEIGHT + 48);
    let mut height = PREVIEW_HEIGHT;
    loop {
        let cell_height = LABEL_HEIGHT + height + CELL_PADDING;
        let widths: Vec<i32> = frames
            .iter()
            .map(|frame| {
                let scaled = i64::from(frame.w.max(1)) * i64::from(height) / i64::from(frame.h.max(1));
                let min = PREVIEW_MIN_WIDTH * height / PREVIEW_HEIGHT;
                let max = PREVIEW_MAX_WIDTH * height / PREVIEW_HEIGHT;
                (scaled.clamp(i64::from(min), i64::from(max.max(min))) as i32 + 2 * CELL_PADDING).min(max_width)
            })
            .collect();
        // Fill rows left to right.
        let mut rows: Vec<Vec<usize>> = vec![Vec::new()];
        let mut row_width = 0;
        for (index, width) in widths.iter().enumerate() {
            if row_width + width > max_width && !rows.last().is_some_and(Vec::is_empty) {
                rows.push(Vec::new());
                row_width = 0;
            }
            rows.last_mut().expect("there is always a row").push(index);
            row_width += width;
        }
        let total_height = rows.len() as i32 * cell_height;
        if total_height > max_height && height > 48 {
            height = (height * 4 / 5).max(48);
            continue;
        }
        let row_widths: Vec<i32> = rows.iter().map(|row| row.iter().map(|i| widths[*i]).sum()).collect();
        let content_width = row_widths.iter().copied().max().unwrap_or(0);
        let panel_size = Size::from((content_width + 2 * PANEL_PADDING, total_height + 2 * PANEL_PADDING));
        let panel = Rectangle::new(
            Point::from(((area.w - panel_size.w) / 2, (area.h - panel_size.h) / 2)),
            panel_size,
        );
        let mut cells = vec![Rectangle::default(); frames.len()];
        for (row_index, row) in rows.iter().enumerate() {
            // Each row is centred in the panel.
            let mut x = panel.loc.x + PANEL_PADDING + (content_width - row_widths[row_index]) / 2;
            let y = panel.loc.y + PANEL_PADDING + row_index as i32 * cell_height;
            for index in row {
                cells[*index] = Rectangle::new(Point::from((x, y)), Size::from((widths[*index], cell_height)));
                x += widths[*index];
            }
        }
        return Layout {
            panel,
            cells,
            preview_height: height,
        };
    }
}

impl Boswas {
    /// The windows the switcher steps through: those of this desktop,
    /// minimized ones too, most recently used first.
    fn switcher_order(&self) -> Vec<u64> {
        let here = |window: &WindowElement| {
            let data = window.data();
            data.mapped.get() && data.workspace.get() == self.workspace
        };
        let mut order: Vec<u64> = self
            .focus_history
            .iter()
            .rev()
            .copied()
            .filter(|id| self.window_by_id(*id).is_some_and(|w| here(&w)))
            .collect();
        for window in self.windows.iter().rev() {
            if here(window) && !order.contains(&window.id()) {
                order.push(window.id());
            }
        }
        order
    }

    /// Alt+Tab: opens the switcher on the window used before this one, or
    /// steps to the next (previous with Shift).
    pub fn cycle_windows(&mut self, backwards: bool) {
        if self.lock.locked || self.overview.is_some() {
            return;
        }
        match &mut self.switcher {
            Some(switcher) => {
                let len = switcher.order.len();
                switcher.index = if backwards {
                    (switcher.index + len - 1) % len
                } else {
                    (switcher.index + 1) % len
                };
            }
            None => {
                let order = self.switcher_order();
                if order.is_empty() {
                    return;
                }
                // The first entry is the focused window when there is one;
                // the switcher starts on the one after (or before) it.
                let focused = self.focused_window().map(|w| w.id());
                let len = order.len();
                let start = match (focused == order.first().copied(), backwards) {
                    (true, false) => 1 % len,
                    (true, true) => len - 1,
                    (false, _) => 0,
                };
                let output = self.active_output().map(|o| o.name()).unwrap_or_default();
                self.switcher = Some(Switcher {
                    order,
                    index: start,
                    opened: Instant::now(),
                    output,
                    cells: Vec::new(),
                    panel: None,
                });
            }
        }
        self.schedule_redraw();
    }

    /// Moves the selection by `step` cells (arrow keys, the pointer).
    pub fn switcher_step(&mut self, step: i32) {
        if let Some(switcher) = &mut self.switcher {
            let len = switcher.order.len() as i32;
            switcher.index = (switcher.index as i32 + step).rem_euclid(len) as usize;
            self.schedule_redraw();
        }
    }

    /// Selects the cell under the pointer, if there is one. Returns whether
    /// the pointer is on a cell.
    pub fn switcher_point(&mut self, point: Point<f64, Logical>) -> bool {
        let Some(switcher) = &mut self.switcher else {
            return false;
        };
        match switcher.cell_at(point) {
            Some(index) => {
                if switcher.index != index {
                    switcher.index = index;
                    self.schedule_redraw();
                }
                true
            }
            None => false,
        }
    }

    /// Ends the switcher: goes to the selected window, or with `cancel`
    /// leaves everything as it was.
    pub fn finish_switcher(&mut self, cancel: bool) {
        let Some(switcher) = self.switcher.take() else {
            return;
        };
        self.schedule_redraw();
        if cancel {
            return;
        }
        if let Some(window) = switcher.selected().and_then(|id| self.window_by_id(id)) {
            self.activate_window(&window);
        }
    }

    /// Whether the switcher still has to appear.
    pub fn switcher_animating(&self) -> bool {
        self.switcher.as_ref().is_some_and(|s| s.opened.elapsed() < APPEAR_AFTER + Duration::from_millis(50))
    }

    /// The switcher's picture on an output: live previews in front of the
    /// panel with labels and the selection.
    pub fn switcher_elements<R>(
        &mut self,
        renderer: &mut R,
        output: &Output,
        scale: Scale<f64>,
        elements: &mut Vec<OutputRenderElement<R>>,
    ) where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let Some(switcher) = &self.switcher else {
            return;
        };
        if switcher.output != output.name() || switcher.opened.elapsed() < APPEAR_AFTER {
            return;
        }
        let Some(output_geo) = self.space.output_geometry(output) else {
            return;
        };
        let windows: Vec<WindowElement> = switcher.order.iter().filter_map(|id| self.window_by_id(*id)).collect();
        if windows.len() != switcher.order.len() {
            // A window went away while the switcher was open.
            let order: Vec<u64> = windows.iter().map(WindowElement::id).collect();
            let switcher = self.switcher.as_mut().expect("checked above");
            switcher.index = switcher.index.min(order.len().saturating_sub(1));
            switcher.order = order;
            if windows.is_empty() {
                self.switcher = None;
                return;
            }
        }
        let frames: Vec<Size<i32, Logical>> = windows.iter().map(|w| w.frame().size).collect();
        let layout = layout(output_geo.size, &frames);
        let selected = self.switcher.as_ref().map_or(0, |s| s.index);
        let pixel_scale = scale.x.ceil().max(1.0) as i32;
        let key = PanelKey {
            layout: layout.clone(),
            titles: windows.iter().map(|w| (w.title(), w.app_id())).collect(),
            selected,
            scale: pixel_scale,
            light: self.theme.scheme == boswas_ui::Scheme::Light,
        };

        // Previews, in front of the panel.
        for (window, cell) in windows.iter().zip(&layout.cells) {
            let preview = layout.preview(*cell).to_f64();
            elements.extend(
                placed(renderer, window, preview, scale, 1.0, false)
                    .into_iter()
                    .map(OutputRenderElement::Moved),
            );
        }

        let cached = self.switcher.as_ref().and_then(|s| s.panel.as_ref()).is_some_and(|(k, _)| *k == key);
        if !cached {
            let buffer = self.draw_switcher_panel(&key);
            if let (Some(switcher), Some(buffer)) = (self.switcher.as_mut(), buffer) {
                switcher.panel = Some((key, buffer));
            }
        }
        let Some(switcher) = self.switcher.as_mut() else {
            return;
        };
        switcher.cells = layout
            .cells
            .iter()
            .map(|cell| Rectangle::new(cell.loc + output_geo.loc, cell.size))
            .collect();
        if let Some((_, buffer)) = &switcher.panel {
            let location = (layout.panel.loc - Point::from((SHADOW, SHADOW))).to_physical_precise_round::<_, i32>(scale);
            if let Ok(element) = MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                location.to_f64(),
                buffer,
                None,
                None,
                Some(layout.panel.size + Size::from((2 * SHADOW, 2 * SHADOW))),
                Kind::Unspecified,
            ) {
                elements.push(OutputRenderElement::Memory(element));
            }
        }
    }

    /// Draws the panel: its surface and shadow, the labels and the frame
    /// around the selected cell. The previews are live windows drawn in
    /// front of it.
    fn draw_switcher_panel(&mut self, key: &PanelKey) -> Option<MemoryRenderBuffer> {
        let t = tokens();
        let theme = self.theme;
        let scheme = theme.scheme;
        let panel = key.layout.panel;
        let margin = SHADOW as f32;
        let (w, h) = (panel.size.w as f32, panel.size.h as f32);
        let mut canvas = Canvas::new(w + 2.0 * margin, h + 2.0 * margin, key.scale as f32)?;
        let elevation = t.elevation("dialog");
        canvas.shadow(margin, margin, w, h, t.radius_flyout, elevation.blur.min(margin - 8.0), elevation.y.min(8.0), elevation.alpha);
        let material = t.material(scheme, "acrylic");
        canvas.fill_rounded_rect(margin, margin, w, h, t.radius_flyout, material.tint);
        canvas.stroke_rounded_rect(margin + 0.5, margin + 0.5, w - 1.0, h - 1.0, t.radius_flyout - 0.5, 1.0, t.stroke(scheme, "surface"));

        let caption = t.type_style("caption");
        let icon_size = 16u32;
        for (index, cell) in key.layout.cells.iter().enumerate() {
            let x = margin + (cell.loc.x - panel.loc.x) as f32;
            let y = margin + (cell.loc.y - panel.loc.y) as f32;
            let (cw, ch) = (cell.size.w as f32, cell.size.h as f32);
            if index == key.selected {
                canvas.fill_rounded_rect(x, y, cw, ch, 6.0, t.fill(scheme, "control-hover"));
                canvas.stroke_rounded_rect(x + 1.0, y + 1.0, cw - 2.0, ch - 2.0, 5.0, 2.0, theme.focus);
            }
            let (title, app_id) = &key.titles[index];
            let mut text_x = x + CELL_PADDING as f32;
            if let Some(icon) = self.app_icons.get(app_id, icon_size * key.scale as u32) {
                let size = icon_size as f32;
                canvas.draw_pixmap(&icon, text_x, y + (LABEL_HEIGHT as f32 - size) / 2.0, size, size, 1.0);
                text_x += size + 8.0;
            }
            let style = TextStyle::new(caption.size, theme.text).weight(caption.weight);
            let room = x + cw - CELL_PADDING as f32 - text_x;
            if room > 16.0 {
                let line = style.line_height();
                self.text()
                    .draw(&mut canvas, title, text_x, y + ((LABEL_HEIGHT as f32 - line) / 2.0).floor(), Some(room), &style);
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
}

#[cfg(test)]
mod tests {
    use super::*;

    fn size(w: i32, h: i32) -> Size<i32, Logical> {
        Size::from((w, h))
    }

    fn inside(outer: Rectangle<i32, Logical>, inner: Rectangle<i32, Logical>) -> bool {
        inner.loc.x >= outer.loc.x
            && inner.loc.y >= outer.loc.y
            && inner.loc.x + inner.size.w <= outer.loc.x + outer.size.w
            && inner.loc.y + inner.size.h <= outer.loc.y + outer.size.h
    }

    #[test]
    fn a_few_windows_sit_in_one_centred_row() {
        let area = size(1920, 1080);
        let l = layout(area, &[size(800, 600), size(1280, 720), size(400, 900)]);
        assert_eq!(l.cells.len(), 3);
        assert_eq!(l.preview_height, PREVIEW_HEIGHT);
        assert!(l.cells.windows(2).all(|pair| pair[0].loc.y == pair[1].loc.y), "one row");
        assert!(l.cells.windows(2).all(|pair| pair[0].loc.x + pair[0].size.w == pair[1].loc.x), "side by side");
        assert_eq!(l.panel.loc.x, (1920 - l.panel.size.w) / 2);
        assert_eq!(l.panel.loc.y, (1080 - l.panel.size.h) / 2);
        for cell in &l.cells {
            assert!(inside(l.panel, *cell));
            assert!(inside(*cell, l.preview(*cell)));
        }
        // Wide windows get wide previews, within limits.
        assert!(l.cells[1].size.w > l.cells[0].size.w);
        assert_eq!(l.cells[2].size.w, PREVIEW_MIN_WIDTH + 2 * CELL_PADDING);
    }

    #[test]
    fn many_windows_wrap_and_shrink_to_stay_on_screen() {
        let area = size(1280, 720);
        let frames = vec![size(1280, 720); 40];
        let l = layout(area, &frames);
        assert!(l.preview_height < PREVIEW_HEIGHT, "previews shrink");
        let screen = Rectangle::new(Point::from((0, 0)), area);
        assert!(inside(screen, l.panel), "{:?}", l.panel);
        let rows: std::collections::BTreeSet<i32> = l.cells.iter().map(|c| c.loc.y).collect();
        assert!(rows.len() > 1, "more than one row");
        for (i, a) in l.cells.iter().enumerate() {
            assert!(inside(l.panel, *a));
            for b in &l.cells[i + 1..] {
                assert!(!a.overlaps(*b), "{a:?} {b:?}");
            }
        }
    }

    #[test]
    fn odd_sizes_do_not_break_the_layout() {
        let l = layout(size(640, 480), &[size(0, 0), size(100_000, 1), size(1, 100_000)]);
        assert_eq!(l.cells.len(), 3);
        assert!(l.cells.iter().all(|c| c.size.w > 0 && c.size.h > 0));
        let empty = layout(size(640, 480), &[]);
        assert!(empty.cells.is_empty());
    }
}
