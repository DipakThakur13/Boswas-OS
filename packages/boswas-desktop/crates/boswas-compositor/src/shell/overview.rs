//! Task View (Super+Tab): every window of the desktop side by side as a
//! live preview, and below them the desktops.
//!
//! Opening Task View moves each window from where it is into a grid;
//! choosing one (click, or arrows and Enter) goes to it. The strip at the
//! bottom shows the desktops with a small map of their windows; desktops
//! are added, switched to and closed there. While Task View is open the
//! pointer and the keyboard belong to it, not to the applications.

use std::{collections::HashMap, time::Instant};

use boswas_ui::{text::TextStyle, tokens, Canvas, Scheme};
use smithay::{
    backend::{
        allocator::Fourcc,
        renderer::{
            element::{
                memory::{MemoryRenderBuffer, MemoryRenderBufferRenderElement},
                solid::{SolidColorBuffer, SolidColorRenderElement},
                Kind,
            },
            Color32F, Texture,
        },
    },
    output::Output,
    utils::{Logical, Point, Rectangle, Scale, Size, Transform},
};

use super::element::WindowElement;
use crate::{
    animation::{Curve, Timeline},
    effects::BoswasRenderer,
    input::Direction,
    render::{placed, OutputRenderElement},
    state::Boswas,
};

/// Height of a preview at its largest.
const PREVIEW_MAX_HEIGHT: i32 = 320;
const PREVIEW_MIN_HEIGHT: i32 = 64;
const PREVIEW_MIN_WIDTH: i32 = 120;
/// Height of the icon, title and close button above a preview.
const LABEL_HEIGHT: i32 = 32;
/// Space between a cell's edge and its content.
const CELL_PADDING: i32 = 8;
/// Space kept free around the grid.
const MARGIN: i32 = 40;
/// Height of the strip with the desktops.
const STRIP_HEIGHT: i32 = 148;
const DESKTOP_TILE: (i32, i32) = (168, 95);
const DESKTOP_GAP: i32 = 12;
/// Side of the close buttons.
const CLOSE_SIZE: i32 = 24;
/// How dark the desktop behind Task View gets.
const DIM: f32 = 0.45;

/// What the pointer is on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Hit {
    Window(u64),
    CloseWindow(u64),
    Desktop(usize),
    CloseDesktop(usize),
    NewDesktop,
}

enum Phase {
    Opening(Timeline),
    Open,
    Closing(Timeline),
}

/// Task View while it is open (and while it closes).
pub struct Overview {
    /// The window the keyboard selection is on.
    pub selected: Option<u64>,
    phase: Phase,
    hover: Option<Hit>,
    /// What is where on the desktop, from the last frame, front to back.
    hits: Vec<(Rectangle<i32, Logical>, Hit)>,
    cards: HashMap<u64, (CardKey, MemoryRenderBuffer)>,
    strips: HashMap<String, (StripKey, MemoryRenderBuffer)>,
    dim: SolidColorBuffer,
}

/// The cards and strips Task View drew, kept while it is closed.
type Kept = (HashMap<u64, (CardKey, MemoryRenderBuffer)>, HashMap<String, (StripKey, MemoryRenderBuffer)>);

thread_local! {
    static KEPT: std::cell::RefCell<Kept> = std::cell::RefCell::new((HashMap::new(), HashMap::new()));
}

impl Drop for Overview {
    fn drop(&mut self) {
        let kept = (std::mem::take(&mut self.cards), std::mem::take(&mut self.strips));
        KEPT.with(|cell| *cell.borrow_mut() = kept);
    }
}

impl Overview {
    /// How far Task View is open: 0.0 (windows where they are) to 1.0 (in
    /// the grid).
    fn progress(&self, now: Instant) -> f32 {
        match &self.phase {
            Phase::Opening(timeline) => timeline.progress(now),
            Phase::Open => 1.0,
            Phase::Closing(timeline) => 1.0 - timeline.progress(now),
        }
    }

    /// Whether Task View takes the pointer and the keyboard (not while it
    /// is on its way out).
    pub fn grabs_input(&self) -> bool {
        !matches!(self.phase, Phase::Closing(_))
    }

    fn hit_at(&self, point: Point<f64, Logical>) -> Option<Hit> {
        self.hits.iter().find(|(rect, _)| rect.to_f64().contains(point)).map(|(_, hit)| *hit)
    }
}

/// What a window's cached card (label, selection frame, close button) was
/// drawn for.
#[derive(Clone, Debug, PartialEq)]
struct CardKey {
    size: Size<i32, Logical>,
    title: String,
    app_id: String,
    selected: bool,
    hovered: bool,
    close_hovered: bool,
    scale: i32,
    light: bool,
}

/// What the cached strip of desktops was drawn for.
#[derive(Clone, Debug, PartialEq)]
struct StripKey {
    layout: Layout,
    active: usize,
    hover: Option<Hit>,
    /// The windows of each desktop as fractions of the screen, in
    /// thousandths: the little map inside each tile.
    maps: Vec<Vec<[i32; 4]>>,
    scale: i32,
    light: bool,
    /// The strip shows the wallpaper through its tint.
    translucent: bool,
}

/// Where everything goes on one output, relative to its usable area.
#[derive(Clone, Debug, PartialEq)]
pub struct Layout {
    /// One cell per window: the label above the preview.
    pub cells: Vec<Rectangle<i32, Logical>>,
    pub preview_height: i32,
    /// The strip with the desktops.
    pub strip: Rectangle<i32, Logical>,
    /// One tile per desktop.
    pub desktops: Vec<Rectangle<i32, Logical>>,
    pub new_desktop: Rectangle<i32, Logical>,
}

impl Layout {
    /// The preview's rectangle inside a cell.
    pub fn preview(&self, cell: Rectangle<i32, Logical>) -> Rectangle<i32, Logical> {
        Rectangle::new(
            cell.loc + Point::from((CELL_PADDING, LABEL_HEIGHT)),
            Size::from((cell.size.w - 2 * CELL_PADDING, self.preview_height)),
        )
    }

    /// The close button of a cell.
    fn close_button(cell: Rectangle<i32, Logical>) -> Rectangle<i32, Logical> {
        Rectangle::new(
            cell.loc + Point::from((cell.size.w - CELL_PADDING - CLOSE_SIZE, (LABEL_HEIGHT - CLOSE_SIZE) / 2)),
            Size::from((CLOSE_SIZE, CLOSE_SIZE)),
        )
    }

    /// The close button of a desktop tile.
    fn desktop_close(tile: Rectangle<i32, Logical>) -> Rectangle<i32, Logical> {
        Rectangle::new(
            tile.loc + Point::from((tile.size.w - CLOSE_SIZE - 2, 2)),
            Size::from((CLOSE_SIZE, CLOSE_SIZE)),
        )
    }
}

/// Lays out Task View in an area of `area` size: the previews of windows
/// with the given frame sizes in rows that are as tall as fits, and the
/// strip of `desktops` desktops below.
pub fn layout(area: Size<i32, Logical>, frames: &[Size<i32, Logical>], desktops: usize) -> Layout {
    let strip_height = STRIP_HEIGHT.min(area.h / 3);
    let strip = Rectangle::new(Point::from((0, area.h - strip_height)), Size::from((area.w, strip_height)));

    // The desktops and the "new desktop" tile, centred in the strip; the
    // tiles shrink when there are many.
    let tiles = desktops as i32 + 1;
    let tile_w = DESKTOP_TILE.0.min(((area.w - 2 * MARGIN - (tiles - 1) * DESKTOP_GAP) / tiles).max(24));
    let tile_h = (tile_w * DESKTOP_TILE.1 / DESKTOP_TILE.0).min(strip_height - 44).max(12);
    let row_w = tiles * tile_w + (tiles - 1) * DESKTOP_GAP;
    let tile_y = strip.loc.y + 12;
    let tile = |index: i32| {
        Rectangle::new(
            Point::from(((area.w - row_w) / 2 + index * (tile_w + DESKTOP_GAP), tile_y)),
            Size::from((tile_w, tile_h)),
        )
    };
    let desktop_tiles: Vec<_> = (0..desktops as i32).map(tile).collect();
    let new_desktop = tile(desktops as i32);

    // The grid of previews above the strip.
    let avail_w = (area.w - 2 * MARGIN).max(PREVIEW_MIN_WIDTH + 2 * CELL_PADDING);
    let avail_h = (area.h - strip_height - 2 * MARGIN).max(LABEL_HEIGHT + PREVIEW_MIN_HEIGHT);
    let mut height = PREVIEW_MAX_HEIGHT.min(avail_h - LABEL_HEIGHT - CELL_PADDING).max(PREVIEW_MIN_HEIGHT);
    let (cells, height) = loop {
        let cell_h = LABEL_HEIGHT + height + CELL_PADDING;
        let widths: Vec<i32> = frames
            .iter()
            .map(|frame| {
                let scaled = i64::from(frame.w.max(1)) * i64::from(height) / i64::from(frame.h.max(1));
                (scaled.clamp(i64::from(PREVIEW_MIN_WIDTH), i64::from(avail_w - 2 * CELL_PADDING)) as i32)
                    + 2 * CELL_PADDING
            })
            .collect();
        let mut rows: Vec<Vec<usize>> = vec![Vec::new()];
        let mut row_width = 0;
        for (index, width) in widths.iter().enumerate() {
            if row_width + width > avail_w && !rows.last().is_some_and(Vec::is_empty) {
                rows.push(Vec::new());
                row_width = 0;
            }
            rows.last_mut().expect("there is always a row").push(index);
            row_width += width;
        }
        let total_h = rows.len() as i32 * cell_h;
        if total_h > avail_h && height > PREVIEW_MIN_HEIGHT {
            height = (height * 9 / 10).max(PREVIEW_MIN_HEIGHT);
            continue;
        }
        let mut cells = vec![Rectangle::default(); frames.len()];
        let top = MARGIN + ((avail_h - total_h) / 2).max(0);
        for (row_index, row) in rows.iter().enumerate() {
            let row_w: i32 = row.iter().map(|i| widths[*i]).sum();
            let mut x = (area.w - row_w) / 2;
            for index in row {
                cells[*index] = Rectangle::new(
                    Point::from((x, top + row_index as i32 * cell_h)),
                    Size::from((widths[*index], cell_h)),
                );
                x += widths[*index];
            }
        }
        break (cells, height);
    };
    Layout {
        cells,
        preview_height: height,
        strip,
        desktops: desktop_tiles,
        new_desktop,
    }
}

/// The cell to go to from `from` in a direction: the nearest one whose
/// centre lies that way.
fn neighbour(cells: &[(u64, Rectangle<i32, Logical>)], from: u64, direction: Direction) -> Option<u64> {
    let centre = |r: &Rectangle<i32, Logical>| (r.loc.x + r.size.w / 2, r.loc.y + r.size.h / 2);
    let (fx, fy) = cells.iter().find(|(id, _)| *id == from).map(|(_, r)| centre(r))?;
    cells
        .iter()
        .filter(|(id, _)| *id != from)
        .filter_map(|(id, rect)| {
            let (x, y) = centre(rect);
            let (along, across) = match direction {
                Direction::Left => (fx - x, (y - fy).abs()),
                Direction::Right => (x - fx, (y - fy).abs()),
                Direction::Up => (fy - y, (x - fx).abs()),
                Direction::Down => (y - fy, (x - fx).abs()),
            };
            // Cells in the same row or column count as closest.
            (along > 0).then_some((along + 3 * across, *id))
        })
        .min()
        .map(|(_, id)| id)
}

impl Boswas {
    /// Opens or closes Task View; `None` toggles.
    pub fn set_overview(&mut self, active: Option<bool>) {
        if self.lock.locked {
            return;
        }
        let open = self.overview.as_ref().is_some_and(Overview::grabs_input);
        let active = active.unwrap_or(!open);
        if active == open {
            return;
        }
        let t = tokens();
        let now = Instant::now();
        if active {
            self.finish_switcher(true);
            self.show_desktop = None;
            let phase = if self.reduced_motion() {
                Phase::Open
            } else {
                Phase::Opening(Timeline::new(t.motion_slow_ms, Curve::Decelerate))
            };
            // The cards and strips of the last time, for the windows that
            // are still there: opening Task View again draws them again
            // only where they changed.
            let (mut cards, strips) = KEPT.with(|kept| std::mem::take(&mut *kept.borrow_mut()));
            cards.retain(|id, _| self.window_by_id(*id).is_some());
            self.overview = Some(Overview {
                selected: self.focused_window().map(|w| w.id()),
                phase,
                hover: None,
                hits: Vec::new(),
                cards,
                strips,
                dim: SolidColorBuffer::default(),
            });
            // Applications lose the pointer and the keyboard.
            self.set_keyboard_focus(None);
        } else if self.reduced_motion() {
            self.overview = None;
            self.update_keyboard_focus();
        } else if let Some(overview) = &mut self.overview {
            // Start the way back from wherever the way in has got to.
            let reached = overview.progress(now);
            let duration = (t.motion_normal_ms as f32 * reached).max(1.0) as u32;
            overview.phase = Phase::Closing(Timeline::new(duration, Curve::Accelerate));
            overview.hover = None;
            self.update_keyboard_focus();
        }
        self.schedule_redraw();
        self.ipc_event("overview", serde_json::json!({ "active": active }));
    }

    /// Whether Task View is open and takes the input.
    pub fn overview_active(&self) -> bool {
        self.overview.as_ref().is_some_and(Overview::grabs_input)
    }

    /// Whether Task View is still moving into or out of place.
    pub fn overview_animating(&self) -> bool {
        self.overview.as_ref().is_some_and(|o| !matches!(o.phase, Phase::Open))
    }

    /// Drops Task View once its way out is over. Called before each frame.
    pub fn overview_tick(&mut self) {
        let now = Instant::now();
        if let Some(overview) = &mut self.overview {
            match overview.phase {
                Phase::Opening(timeline) if timeline.finished(now) => overview.phase = Phase::Open,
                Phase::Closing(timeline) if timeline.finished(now) => self.overview = None,
                _ => {}
            }
            // The hit rectangles are rebuilt by the frame that follows.
            if let Some(overview) = &mut self.overview {
                overview.hits.clear();
            }
        }
    }

    /// Goes to a window from Task View.
    fn overview_choose(&mut self, id: u64) {
        self.set_overview(Some(false));
        if let Some(window) = self.window_by_id(id) {
            self.activate_window(&window);
        }
    }

    /// The pointer moved while Task View is open.
    pub fn overview_motion(&mut self, point: Point<f64, Logical>) {
        if let Some(overview) = &mut self.overview {
            let hover = overview.hit_at(point);
            if hover != overview.hover {
                overview.hover = hover;
                self.schedule_redraw();
            }
        }
    }

    /// A click (the button's release) while Task View is open.
    pub fn overview_click(&mut self, point: Point<f64, Logical>) {
        let Some(overview) = &self.overview else {
            return;
        };
        match overview.hit_at(point) {
            Some(Hit::Window(id)) => self.overview_choose(id),
            Some(Hit::CloseWindow(id)) => {
                if let Some(window) = self.window_by_id(id) {
                    self.close_window(&window);
                }
            }
            Some(Hit::Desktop(index)) => {
                self.switch_workspace(index);
                self.set_overview(Some(false));
            }
            Some(Hit::CloseDesktop(index)) => self.remove_workspace(index),
            Some(Hit::NewDesktop) => {
                self.add_workspace();
                self.schedule_redraw();
            }
            // A click beside everything leaves Task View.
            None => self.set_overview(Some(false)),
        }
    }

    /// A key press while Task View is open.
    pub fn overview_key(&mut self, key: OverviewKey) {
        let Some(overview) = &self.overview else {
            return;
        };
        let cells: Vec<(u64, Rectangle<i32, Logical>)> = overview
            .hits
            .iter()
            .filter_map(|(rect, hit)| match hit {
                Hit::Window(id) => Some((*id, *rect)),
                _ => None,
            })
            .collect();
        let selected = overview.selected.filter(|id| cells.iter().any(|(cell, _)| cell == id));
        match key {
            OverviewKey::Cancel => self.set_overview(Some(false)),
            OverviewKey::Accept => match selected {
                Some(id) => self.overview_choose(id),
                None => self.set_overview(Some(false)),
            },
            OverviewKey::Close => {
                if let Some(window) = selected.and_then(|id| self.window_by_id(id)) {
                    self.close_window(&window);
                }
            }
            OverviewKey::Move(direction) => {
                let next = match selected {
                    Some(id) => neighbour(&cells, id, direction).or(Some(id)),
                    None => cells.first().map(|(id, _)| *id),
                };
                self.overview_select(next);
            }
            OverviewKey::Next { backwards } => {
                let position = selected.and_then(|id| cells.iter().position(|(cell, _)| *cell == id));
                let next = match (position, cells.len()) {
                    (_, 0) => None,
                    (None, _) => Some(cells[0].0),
                    (Some(i), len) => Some(cells[if backwards { (i + len - 1) % len } else { (i + 1) % len }].0),
                };
                self.overview_select(next);
            }
        }
    }

    fn overview_select(&mut self, id: Option<u64>) {
        if let Some(overview) = &mut self.overview {
            overview.selected = id;
            self.schedule_redraw();
        }
    }

    /// The windows Task View shows on an output: those of this desktop
    /// that are (or, minimized, were) mostly on it, most recently used
    /// first.
    fn overview_windows(&self, output: &Output) -> Vec<WindowElement> {
        let mut windows: Vec<WindowElement> = self
            .windows
            .iter()
            .filter(|window| {
                let data = window.data();
                data.mapped.get()
                    && !window.is_override_redirect()
                    && data.workspace.get() == self.workspace
                    && self
                        .output_for_rect(Rectangle::new(data.location.get(), window.frame().size))
                        .is_some_and(|o| &o == output)
            })
            .cloned()
            .collect();
        let rank = |window: &WindowElement| self.focus_history.iter().rev().position(|id| *id == window.id()).unwrap_or(usize::MAX);
        windows.sort_by_key(rank);
        windows
    }

    /// Task View's picture on an output, front to back: the strip of
    /// desktops, then for each window its preview on its card, then the
    /// dimmed desktop.
    pub fn overview_elements<R>(
        &mut self,
        renderer: &mut R,
        output: &Output,
        scale: Scale<f64>,
        elements: &mut Vec<OutputRenderElement<R>>,
    ) where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let Some(output_geo) = self.space.output_geometry(output) else {
            return;
        };
        let area = self.usable_area(output);
        let area_offset = area.loc - output_geo.loc;
        let now = Instant::now();
        let windows = self.overview_windows(output);
        let frames: Vec<Size<i32, Logical>> = windows.iter().map(|w| w.frame().size).collect();
        let layout = layout(area.size, &frames, self.workspace_count);
        let pixel_scale = scale.x.ceil().max(1.0) as i32;
        let light = self.theme.scheme == Scheme::Light;
        let Some(overview) = &self.overview else {
            return;
        };
        let progress = overview.progress(now);
        let (selected, hover) = (overview.selected, overview.hover);
        let interactive = overview.grabs_input();

        // The strip of desktops.
        let screen = self.space.output_geometry(output).unwrap_or(area);
        let maps: Vec<Vec<[i32; 4]>> = (0..self.workspace_count)
            .map(|desktop| {
                self.windows
                    .iter()
                    .filter(|w| w.data().mapped.get() && w.data().workspace.get() == desktop && !w.data().minimized.get())
                    .filter_map(|w| {
                        let rect = Rectangle::new(w.data().location.get(), w.frame().size).intersection(screen)?;
                        let per_mille = |value: i32, of: i32| value * 1000 / of.max(1);
                        Some([
                            per_mille(rect.loc.x - screen.loc.x, screen.size.w),
                            per_mille(rect.loc.y - screen.loc.y, screen.size.h),
                            per_mille(rect.size.w, screen.size.w),
                            per_mille(rect.size.h, screen.size.h),
                        ])
                    })
                    .collect()
            })
            .collect();
        let strip_key = StripKey {
            layout: layout.clone(),
            active: self.workspace,
            hover: hover.filter(|hit| matches!(hit, Hit::Desktop(_) | Hit::CloseDesktop(_) | Hit::NewDesktop)),
            maps,
            scale: pixel_scale,
            light,
            translucent: self.glass_on(),
        };
        let name = output.name();
        let cached = self
            .overview
            .as_ref()
            .and_then(|o| o.strips.get(&name))
            .is_some_and(|(key, _)| *key == strip_key);
        if !cached {
            if let Some(buffer) = self.draw_overview_strip(&strip_key) {
                if let Some(overview) = &mut self.overview {
                    overview.strips.insert(name.clone(), (strip_key, buffer));
                }
            }
        }
        if let Some((_, buffer)) = self.overview.as_ref().and_then(|o| o.strips.get(&name)) {
            let location = (layout.strip.loc + area_offset).to_physical_precise_round::<_, i32>(scale);
            if let Ok(element) = MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                location.to_f64(),
                buffer,
                Some(progress),
                None,
                Some(layout.strip.size),
                Kind::Unspecified,
            ) {
                elements.push(OutputRenderElement::Memory(element));
            }
        }

        // The windows: each preview in front of its card.
        let mut hits: Vec<(Rectangle<i32, Logical>, Hit)> = Vec::new();
        for (window, cell) in windows.iter().zip(&layout.cells) {
            let id = window.id();
            let preview = layout.preview(*cell);
            let global = |rect: Rectangle<i32, Logical>| Rectangle::new(rect.loc + area.loc, rect.size);
            let close = Layout::close_button(*cell);
            let cell_hovered = matches!(hover, Some(Hit::Window(h) | Hit::CloseWindow(h)) if h == id);
            if cell_hovered {
                hits.push((global(close), Hit::CloseWindow(id)));
            }
            hits.push((global(*cell), Hit::Window(id)));

            // From where the window is (or, minimized, would be) into its
            // place in the grid.
            let data = window.data();
            let real = Rectangle::new(data.location.get(), window.frame().size);
            let mut from = real.to_f64();
            from.loc -= output_geo.loc.to_f64();
            if data.minimized.get() {
                let taskbar = self.taskbar_point(real) - output_geo.loc.to_f64();
                from = Rectangle::new(
                    Point::from((taskbar.x - from.size.w / 8.0, taskbar.y - from.size.h / 4.0)),
                    Size::from((from.size.w / 4.0, from.size.h / 4.0)),
                );
            }
            let mut to = preview.to_f64();
            to.loc += area_offset.to_f64();
            let t = f64::from(progress);
            let target = Rectangle::new(
                Point::from((from.loc.x + (to.loc.x - from.loc.x) * t, from.loc.y + (to.loc.y - from.loc.y) * t)),
                Size::from((
                    from.size.w + (to.size.w - from.size.w) * t,
                    from.size.h + (to.size.h - from.size.h) * t,
                )),
            );
            let alpha = if data.minimized.get() { progress } else { 1.0 };
            elements.extend(
                placed(renderer, window, target, scale, alpha, false)
                    .into_iter()
                    .map(OutputRenderElement::Moved),
            );

            let key = CardKey {
                size: cell.size,
                title: window.title(),
                app_id: window.app_id(),
                selected: interactive && selected == Some(id),
                hovered: cell_hovered,
                close_hovered: hover == Some(Hit::CloseWindow(id)),
                scale: pixel_scale,
                light,
            };
            let cached = self
                .overview
                .as_ref()
                .and_then(|o| o.cards.get(&id))
                .is_some_and(|(cached, _)| *cached == key);
            if !cached {
                if let Some(buffer) = self.draw_overview_card(&key) {
                    if let Some(overview) = &mut self.overview {
                        overview.cards.insert(id, (key, buffer));
                    }
                }
            }
            if let Some((_, buffer)) = self.overview.as_ref().and_then(|o| o.cards.get(&id)) {
                let location = (cell.loc + area_offset).to_physical_precise_round::<_, i32>(scale);
                if let Ok(element) = MemoryRenderBufferRenderElement::from_buffer(
                    renderer,
                    location.to_f64(),
                    buffer,
                    Some(progress),
                    None,
                    Some(cell.size),
                    Kind::Unspecified,
                ) {
                    elements.push(OutputRenderElement::Memory(element));
                }
            }
        }

        // The desktops' hit rectangles come after the windows': they never
        // overlap, but the order is front to back.
        for (index, tile) in layout.desktops.iter().enumerate() {
            let global = |rect: Rectangle<i32, Logical>| Rectangle::new(rect.loc + area.loc, rect.size);
            if self.workspace_count > 1 {
                hits.push((global(Layout::desktop_close(*tile)), Hit::CloseDesktop(index)));
            }
            hits.push((global(*tile), Hit::Desktop(index)));
        }
        hits.push((
            Rectangle::new(layout.new_desktop.loc + area.loc, layout.new_desktop.size),
            Hit::NewDesktop,
        ));

        if let Some(overview) = &mut self.overview {
            let alive: Vec<u64> = self.windows.iter().map(WindowElement::id).collect();
            overview.cards.retain(|id, _| alive.contains(id));
            if interactive {
                overview.hits.extend(hits);
                // What is under the pointer may have changed without the
                // pointer moving (a window closed, a desktop was added).
                let hover = overview.hit_at(self.pointer.current_location());
                if hover != overview.hover {
                    overview.hover = hover;
                    self.redraw = true;
                }
            }
            // The desktop behind everything gets darker.
            overview.dim.update(output_geo.size, Color32F::from([0.0, 0.0, 0.0, DIM * progress]));
            elements.push(OutputRenderElement::Solid(SolidColorRenderElement::from_buffer(
                &overview.dim,
                Point::from((0, 0)),
                scale,
                1.0,
                Kind::Unspecified,
            )));
        }
        // Behind it, the desktop blurred into glass (glass/).
        if let Some(backdrop) = self.blurred_desktop(renderer, output, scale, progress) {
            elements.push(OutputRenderElement::Memory(backdrop));
        }
    }

    /// Draws a window's card: the icon and title above the preview and,
    /// when the window is selected or under the pointer, a frame around it
    /// and a close button.
    fn draw_overview_card(&mut self, key: &CardKey) -> Option<MemoryRenderBuffer> {
        let t = tokens();
        let theme = self.theme;
        let scheme = theme.scheme;
        let (w, h) = (key.size.w as f32, key.size.h as f32);
        let mut canvas = Canvas::new(w, h, key.scale as f32)?;
        if key.selected || key.hovered {
            canvas.fill_rounded_rect(0.0, 0.0, w, h, t.radius_flyout, t.fill(scheme, "control-hover"));
        }
        if key.selected {
            canvas.stroke_rounded_rect(1.0, 1.0, w - 2.0, h - 2.0, t.radius_flyout - 1.0, 2.0, theme.accent);
        }
        let caption = t.type_style("caption");
        let mut x = CELL_PADDING as f32 + 2.0;
        let icon_size = 16u32;
        if let Some(icon) = self.app_icons.get(&key.app_id, icon_size * key.scale as u32) {
            let size = icon_size as f32;
            canvas.draw_pixmap(&icon, x, (LABEL_HEIGHT as f32 - size) / 2.0, size, size, 1.0);
            x += size + 8.0;
        }
        let close_room = if key.hovered { CLOSE_SIZE as f32 + 4.0 } else { 0.0 };
        let room = w - CELL_PADDING as f32 - close_room - x;
        if room > 16.0 {
            // Task View lies over the dimmed wallpaper: the text is always
            // light there unless the card is highlighted in the light scheme.
            let ink = if key.selected || key.hovered { theme.text } else { boswas_ui::Rgba::WHITE };
            let style = TextStyle::new(caption.size, ink).weight(caption.weight);
            let line = style.line_height();
            self.text()
                .draw(&mut canvas, &key.title, x, ((LABEL_HEIGHT as f32 - line) / 2.0).floor(), Some(room), &style);
        }
        if key.hovered {
            let size = CLOSE_SIZE as f32;
            let (bx, by) = (w - CELL_PADDING as f32 - size, (LABEL_HEIGHT as f32 - size) / 2.0);
            let ink = if key.close_hovered {
                canvas.fill_rounded_rect(bx, by, size, size, t.radius_control, t.shell_color("caption-close-hover"));
                boswas_ui::Rgba::WHITE
            } else {
                theme.text
            };
            let (cx, cy) = ((bx + size / 2.0).floor() + 0.5, (by + size / 2.0).floor() + 0.5);
            canvas.line(cx - 4.0, cy - 4.0, cx + 4.0, cy + 4.0, 1.0, ink);
            canvas.line(cx + 4.0, cy - 4.0, cx - 4.0, cy + 4.0, 1.0, ink);
        }
        Some(memory_buffer(&canvas, key.scale))
    }

    /// Draws the strip of desktops: a tile per desktop with a small map of
    /// its windows and its name, and the tile that adds a desktop.
    fn draw_overview_strip(&mut self, key: &StripKey) -> Option<MemoryRenderBuffer> {
        let t = tokens();
        let theme = self.theme;
        let scheme = theme.scheme;
        let strip = key.layout.strip;
        let (w, h) = (strip.size.w as f32, strip.size.h as f32);
        let mut canvas = Canvas::new(w, h, key.scale as f32)?;
        // Glass over the blurred desktop (opaque with glass off).
        let source = if key.translucent { boswas_ui::glass::Source::Live } else { boswas_ui::glass::Source::Off };
        boswas_ui::glass::fill(&mut canvas, 0.0, 0.0, w, h, [0.0; 4], (0.0, 0.0), theme, boswas_ui::glass::Kind::Taskbar, source);
        canvas.fill_rect(0.0, 0.0, w, 1.0, t.stroke(scheme, "divider"));

        let caption = t.type_style("caption");
        let local = |rect: Rectangle<i32, Logical>| {
            (
                (rect.loc.x - strip.loc.x) as f32,
                (rect.loc.y - strip.loc.y) as f32,
                rect.size.w as f32,
                rect.size.h as f32,
            )
        };
        let radius = t.radius_control + 2.0;
        for (index, tile) in key.layout.desktops.iter().enumerate() {
            let (x, y, tw, th) = local(*tile);
            let hovered = matches!(key.hover, Some(Hit::Desktop(i) | Hit::CloseDesktop(i)) if i == index);
            canvas.fill_rounded_rect(x, y, tw, th, radius, theme.surface);
            if hovered {
                canvas.fill_rounded_rect(x, y, tw, th, radius, t.fill(scheme, "control-hover"));
            }
            for [mx, my, mw, mh] in key.maps.get(index).map(Vec::as_slice).unwrap_or_default() {
                let scaled = |value: i32, of: f32| value as f32 / 1000.0 * of;
                let (rx, ry) = (x + scaled(*mx, tw), y + scaled(*my, th));
                let (rw, rh) = (scaled(*mw, tw).max(3.0), scaled(*mh, th).max(3.0));
                canvas.fill_rounded_rect(rx, ry, rw, rh, 1.5, theme.surface_hover);
                canvas.stroke_rounded_rect(rx + 0.5, ry + 0.5, rw - 1.0, rh - 1.0, 1.5, 1.0, t.stroke(scheme, "surface"));
            }
            if index == key.active {
                canvas.stroke_rounded_rect(x + 1.0, y + 1.0, tw - 2.0, th - 2.0, radius - 1.0, 2.0, theme.accent);
            } else {
                canvas.stroke_rounded_rect(x + 0.5, y + 0.5, tw - 1.0, th - 1.0, radius - 0.5, 1.0, t.stroke(scheme, "surface"));
            }
            if hovered && key.layout.desktops.len() > 1 {
                let size = CLOSE_SIZE as f32;
                let (bx, by) = (x + tw - size - 2.0, y + 2.0);
                let over = key.hover == Some(Hit::CloseDesktop(index));
                let fill = if over { t.shell_color("caption-close-hover") } else { theme.surface_raised };
                canvas.fill_rounded_rect(bx, by, size, size, t.radius_control, fill);
                let ink = if over { boswas_ui::Rgba::WHITE } else { theme.text };
                let (cx, cy) = ((bx + size / 2.0).floor() + 0.5, (by + size / 2.0).floor() + 0.5);
                canvas.line(cx - 4.0, cy - 4.0, cx + 4.0, cy + 4.0, 1.0, ink);
                canvas.line(cx + 4.0, cy - 4.0, cx - 4.0, cy + 4.0, 1.0, ink);
            }
            let style = TextStyle::new(caption.size, if index == key.active { theme.text } else { theme.text_secondary })
                .weight(if index == key.active { 600 } else { caption.weight });
            let label = format!("Desktop {}", index + 1);
            self.text().draw_centered(&mut canvas, &label, x, y + th + 4.0, tw, caption.line_height, &style);
        }

        // The tile that adds a desktop.
        let (x, y, tw, th) = local(key.layout.new_desktop);
        let hovered = key.hover == Some(Hit::NewDesktop);
        let fill = if hovered { "control-hover" } else { "control" };
        canvas.fill_rounded_rect(x, y, tw, th, radius, t.fill(scheme, fill));
        canvas.stroke_rounded_rect(x + 0.5, y + 0.5, tw - 1.0, th - 1.0, radius - 0.5, 1.0, t.stroke(scheme, "surface"));
        let (cx, cy) = ((x + tw / 2.0).floor() + 0.5, (y + th / 2.0).floor() + 0.5);
        canvas.line(cx - 7.0, cy, cx + 7.0, cy, 1.0, theme.text);
        canvas.line(cx, cy - 7.0, cx, cy + 7.0, 1.0, theme.text);
        let style = TextStyle::new(caption.size, theme.text_secondary).weight(caption.weight);
        self.text().draw_centered(&mut canvas, "New desktop", x - 20.0, y + th + 4.0, tw + 40.0, caption.line_height, &style);
        Some(memory_buffer(&canvas, key.scale))
    }
}

/// A key Task View acts on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OverviewKey {
    Cancel,
    Accept,
    /// Delete: close the selected window.
    Close,
    Move(Direction),
    /// Tab and Shift+Tab.
    Next { backwards: bool },
}

fn memory_buffer(canvas: &Canvas, scale: i32) -> MemoryRenderBuffer {
    let (pw, ph) = canvas.pixel_size();
    let mut bytes = vec![0u8; (pw * ph * 4) as usize];
    canvas.write_argb8888(&mut bytes);
    MemoryRenderBuffer::from_slice(&bytes, Fourcc::Argb8888, (pw as i32, ph as i32), scale, Transform::Normal, None)
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
    fn previews_fill_the_space_above_the_strip() {
        let area = size(1920, 1032);
        let l = layout(area, &[size(1200, 800), size(800, 600), size(700, 532)], 2);
        assert_eq!(l.strip, Rectangle::new(Point::from((0, 1032 - STRIP_HEIGHT)), size(1920, STRIP_HEIGHT)));
        assert_eq!(l.preview_height, PREVIEW_MAX_HEIGHT);
        let above = Rectangle::new(Point::from((0, 0)), size(1920, 1032 - STRIP_HEIGHT));
        for (i, cell) in l.cells.iter().enumerate() {
            assert!(inside(above, *cell), "{cell:?}");
            assert!(inside(*cell, l.preview(*cell)));
            assert!(inside(*cell, Layout::close_button(*cell)));
            for other in &l.cells[i + 1..] {
                assert!(!cell.overlaps(*other));
            }
        }
        // Two desktops and the tile that adds one, in a centred row.
        assert_eq!(l.desktops.len(), 2);
        let tiles = [l.desktops[0], l.desktops[1], l.new_desktop];
        for tile in tiles {
            assert!(inside(l.strip, tile));
            assert!(inside(tile, Layout::desktop_close(tile)));
        }
        assert_eq!(tiles[0].loc.x + (tiles[2].loc.x + tiles[2].size.w - tiles[0].loc.x) / 2, 960);
    }

    #[test]
    fn many_windows_shrink_and_never_leave_the_screen() {
        let area = size(1366, 720);
        let frames = vec![size(1366, 720); 30];
        let l = layout(area, &frames, 16);
        assert!(l.preview_height < PREVIEW_MAX_HEIGHT);
        let screen = Rectangle::new(Point::from((0, 0)), area);
        for (i, cell) in l.cells.iter().enumerate() {
            assert!(cell.loc.x >= 0 && cell.loc.x + cell.size.w <= area.w, "{cell:?}");
            for other in &l.cells[i + 1..] {
                assert!(!cell.overlaps(*other));
            }
        }
        for tile in l.desktops.iter().chain([&l.new_desktop]) {
            assert!(inside(screen, *tile), "{tile:?}");
        }
        assert!(l.desktops.windows(2).all(|pair| pair[0].loc.x + pair[0].size.w <= pair[1].loc.x));
    }

    #[test]
    fn an_empty_desktop_still_has_its_strip() {
        let l = layout(size(800, 600), &[], 1);
        assert!(l.cells.is_empty());
        assert_eq!(l.desktops.len(), 1);
        assert!(l.new_desktop.loc.x > l.desktops[0].loc.x);
        // Degenerate sizes do not panic or produce empty cells.
        let odd = layout(size(200, 120), &[size(0, 0), size(50_000, 1)], 3);
        assert!(odd.cells.iter().all(|c| c.size.w > 0 && c.size.h > 0));
    }

    #[test]
    fn arrows_go_to_the_nearest_cell_that_way() {
        let cell = |x: i32, y: i32| Rectangle::new(Point::from((x, y)), size(100, 100));
        // Two rows: 1 2 3 / 4 5
        let cells = vec![(1, cell(0, 0)), (2, cell(100, 0)), (3, cell(200, 0)), (4, cell(50, 100)), (5, cell(150, 100))];
        assert_eq!(neighbour(&cells, 1, Direction::Right), Some(2));
        assert_eq!(neighbour(&cells, 3, Direction::Left), Some(2));
        assert_eq!(neighbour(&cells, 1, Direction::Left), None);
        assert_eq!(neighbour(&cells, 1, Direction::Down), Some(4));
        assert_eq!(neighbour(&cells, 3, Direction::Down), Some(5));
        assert_eq!(neighbour(&cells, 5, Direction::Up), Some(2), "a tie goes to the earlier cell");
        assert_eq!(neighbour(&cells, 4, Direction::Down), None);
        assert_eq!(neighbour(&cells, 9, Direction::Down), None, "an unknown cell has no neighbours");
    }
}
