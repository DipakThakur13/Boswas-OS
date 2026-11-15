//! Window management policy: placement, focus, snapping, minimize and
//! workspaces.

use smithay::{
    desktop::{find_popup_root_surface, get_popup_toplevel_coords, layer_map_for_output, PopupKind},
    output::Output,
    reexports::{
        wayland_protocols::xdg::{
            decoration::zv1::server::zxdg_toplevel_decoration_v1::Mode as DecorationMode,
            shell::server::xdg_toplevel::State as ToplevelState,
        },
        wayland_server::protocol::wl_surface::WlSurface,
    },
    utils::{IsAlive, Logical, Point, Rectangle, Size, SERIAL_COUNTER},
    wayland::shell::{
        wlr_layer::{KeyboardInteractivity, Layer},
        xdg::PopupSurface,
    },
};

use super::{
    decoration::TITLEBAR_HEIGHT,
    element::{Tile, WindowElement},
};
use crate::state::{root_surface, Boswas};

/// Distance from a screen edge at which a dragged window snaps.
const SNAP_DISTANCE: f64 = 12.0;
/// Offset between consecutively opened windows.
const CASCADE_STEP: i32 = 32;

impl Boswas {
    // ---------------------------------------------------------------------
    // Mapping
    // ---------------------------------------------------------------------

    /// Called on every commit of a toplevel's own surface.
    pub fn window_committed(&mut self, window: &WindowElement) {
        let toplevel = window.toplevel();
        if !toplevel.is_initial_configure_sent() {
            let bounds = self
                .active_output()
                .map(|output| self.usable_area(&output).size)
                .map(|size| Size::from((size.w, (size.h - TITLEBAR_HEIGHT).max(1))));
            toplevel.with_pending_state(|state| state.bounds = bounds);
            toplevel.send_configure();
            return;
        }
        if !window.data().mapped.get() && !window.0.bbox().is_empty() {
            self.place_new_window(window);
        }
    }

    /// Shows a window for the first time: centred on the output under the
    /// pointer, each new window a step down and right of the last.
    fn place_new_window(&mut self, window: &WindowElement) {
        let Some(output) = self.active_output() else {
            return;
        };
        let area = self.usable_area(&output);
        let data = window.data();
        data.workspace.set(self.workspace);
        let size = window.frame().size;
        let location = match data.tile.get() {
            Tile::Fullscreen => self.space.output_geometry(&output).unwrap_or(area).loc,
            tile => match tile.rect(area) {
                Some(rect) => rect.loc,
                None => {
                    let offset = self.cascade * CASCADE_STEP;
                    self.cascade = (self.cascade + 1) % 6;
                    let x = area.loc.x + ((area.size.w - size.w) / 2 + offset - 2 * CASCADE_STEP).max(0);
                    let y = area.loc.y + ((area.size.h - size.h) / 2 + offset - 2 * CASCADE_STEP).max(0);
                    Point::from((
                        x.min((area.loc.x + area.size.w - size.w).max(area.loc.x)),
                        y.min((area.loc.y + area.size.h - size.h).max(area.loc.y)),
                    ))
                }
            },
        };
        data.mapped.set(true);
        data.location.set(location);
        self.space.map_element(window.clone(), location, true);
        self.raise_in_stack(window);
        self.set_keyboard_focus(Some(window.wl_surface().clone()));
        self.windows_changed();
    }

    /// Moves a mapped window's frame to a new location.
    pub fn move_window(&mut self, window: &WindowElement, location: Point<i32, Logical>) {
        window.data().location.set(location);
        if self.space.element_location(window).is_some() {
            self.space.map_element(window.clone(), location, false);
            self.schedule_redraw();
        }
    }

    /// Forgets a window whose toplevel is gone.
    pub fn remove_window(&mut self, surface: &WlSurface) {
        let Some(window) = self.window_for_surface(surface) else {
            return;
        };
        let id = window.id();
        self.space.unmap_elem(&window);
        self.windows.retain(|w| w.id() != id);
        self.focus_history.retain(|w| *w != id);
        self.switcher = None;
        self.update_keyboard_focus();
        self.windows_changed();
    }

    /// Keeps `windows` in stacking order: the raised window goes last.
    fn raise_in_stack(&mut self, window: &WindowElement) {
        if let Some(index) = self.windows.iter().position(|w| w == window) {
            let w = self.windows.remove(index);
            self.windows.push(w);
        }
    }

    // ---------------------------------------------------------------------
    // Focus
    // ---------------------------------------------------------------------

    /// Raises a window and gives it the keyboard; un-minimizes it and
    /// switches to its workspace when needed.
    pub fn activate_window(&mut self, window: &WindowElement) {
        if self.lock.locked || !window.alive() || !window.data().mapped.get() {
            return;
        }
        let data = window.data();
        if data.workspace.get() != self.workspace {
            self.switch_workspace(data.workspace.get());
        }
        if data.minimized.get() {
            data.minimized.set(false);
            self.space.map_element(window.clone(), data.location.get(), true);
        } else {
            self.space.raise_element(window, true);
        }
        self.raise_in_stack(window);
        self.set_keyboard_focus(Some(window.wl_surface().clone()));
        self.windows_changed();
    }

    /// Gives the keyboard to a surface and updates which window is drawn as
    /// active.
    pub fn set_keyboard_focus(&mut self, surface: Option<WlSurface>) {
        let serial = SERIAL_COUNTER.next_serial();
        let keyboard = self.keyboard.clone();
        if keyboard.current_focus() != surface {
            keyboard.set_focus(self, surface.clone(), serial);
        }
        let focused = surface.as_ref().map(root_surface);
        for window in &self.windows {
            let active = focused.as_ref() == Some(window.wl_surface());
            if window.0.set_activated(active) || active {
                window.toplevel().send_pending_configure();
            }
            if active {
                let id = window.id();
                self.focus_history.retain(|w| *w != id);
                self.focus_history.push(id);
            }
        }
        self.schedule_redraw();
    }

    /// Chooses the surface that should have the keyboard right now: the
    /// lock screen, then a layer surface that demands it (the launcher),
    /// then the current focus if it is still valid, then the top window.
    pub fn update_keyboard_focus(&mut self) {
        if self.lock.locked {
            let surface = self
                .active_output()
                .and_then(|o| self.lock.surfaces.get(&o))
                .or_else(|| self.lock.surfaces.values().next())
                .filter(|s| s.alive())
                .map(|s| s.wl_surface().clone());
            return self.set_keyboard_focus(surface);
        }

        let exclusive = self.space.outputs().find_map(|output| {
            let map = layer_map_for_output(output);
            [Layer::Overlay, Layer::Top].into_iter().find_map(|layer| {
                map.layers_on(layer)
                    .rev()
                    .find(|l| {
                        l.cached_state().keyboard_interactivity == KeyboardInteractivity::Exclusive && l.alive()
                    })
                    .map(|l| l.wl_surface().clone())
            })
        });
        if exclusive.is_some() {
            return self.set_keyboard_focus(exclusive);
        }

        let current = self.keyboard.current_focus().filter(|s| s.alive());
        if let Some(surface) = &current {
            let root = root_surface(surface);
            let on_window = self
                .window_for_surface(&root)
                .is_some_and(|w| self.space.element_location(&w).is_some());
            let on_layer = self.space.outputs().any(|output| {
                layer_map_for_output(output).layers().any(|l| {
                    l.wl_surface() == &root
                        && l.cached_state().keyboard_interactivity != KeyboardInteractivity::None
                })
            });
            if on_window || on_layer {
                return self.set_keyboard_focus(current);
            }
        }
        let top = self.space.elements().next_back().map(|w| w.wl_surface().clone());
        self.set_keyboard_focus(top);
    }

    /// Alt+Tab: steps through the windows of this workspace, most recently
    /// used first. The order is fixed while the switcher is held.
    pub fn cycle_windows(&mut self, backwards: bool) {
        if self.lock.locked {
            return;
        }
        if self.switcher.is_none() {
            let mut order: Vec<u64> = self
                .focus_history
                .iter()
                .rev()
                .copied()
                .filter(|id| {
                    self.window_by_id(*id)
                        .is_some_and(|w| w.data().mapped.get() && w.data().workspace.get() == self.workspace)
                })
                .collect();
            for window in &self.windows {
                let data = window.data();
                if data.mapped.get() && data.workspace.get() == self.workspace && !order.contains(&data.id) {
                    order.push(data.id);
                }
            }
            if order.is_empty() {
                return;
            }
            self.switcher = Some((order, 0));
        }
        let Some((order, index)) = self.switcher.clone() else {
            return;
        };
        let len = order.len();
        let next = if backwards { (index + len - 1) % len } else { (index + 1) % len };
        let history = self.focus_history.clone();
        if let Some(window) = self.window_by_id(order[next]) {
            self.activate_window(&window);
        }
        // Looking at a window while cycling does not reorder the history;
        // only the window the switcher ends on moves to the front.
        self.focus_history = history;
        self.switcher = Some((order, next));
    }

    /// Ends Alt+Tab: the selected window becomes the most recently used.
    pub fn finish_switcher(&mut self) {
        if let Some((order, index)) = self.switcher.take() {
            let id = order[index];
            self.focus_history.retain(|w| *w != id);
            self.focus_history.push(id);
        }
    }

    // ---------------------------------------------------------------------
    // Tiling, maximize, fullscreen, minimize
    // ---------------------------------------------------------------------

    /// Arranges a window: floating, maximized, fullscreen or a snap layout.
    pub fn set_tile(&mut self, window: &WindowElement, tile: Tile) {
        let data = window.data();
        let previous = data.tile.get();
        let current = self
            .window_rect(window)
            .unwrap_or_else(|| Rectangle::new(data.location.get(), window.frame().size));
        if previous == Tile::Floating && tile != Tile::Floating && data.mapped.get() {
            data.restore.set(Some(current));
        }
        let output = self.output_for_rect(current);
        let area = output.as_ref().map(|o| self.usable_area(o));
        let output_geo = output.as_ref().and_then(|o| self.space.output_geometry(o));

        // The title bar disappears in fullscreen, so set the tile first:
        // `client_size_for_frame` then accounts for the right frame.
        data.tile.set(tile);
        let target = match tile {
            Tile::Fullscreen => output_geo,
            Tile::Floating => data.restore.take().or(Some(current)),
            other => area.and_then(|area| other.rect(area)),
        };
        let toplevel = window.toplevel();
        let size = target.map(|rect| window.client_size_for_frame(rect.size));
        toplevel.with_pending_state(|state| {
            for s in [
                ToplevelState::Maximized,
                ToplevelState::Fullscreen,
                ToplevelState::TiledLeft,
                ToplevelState::TiledRight,
                ToplevelState::TiledTop,
                ToplevelState::TiledBottom,
            ] {
                state.states.unset(s);
            }
            let tiled: &[ToplevelState] = match tile {
                Tile::Floating => &[],
                Tile::Maximized => &[ToplevelState::Maximized],
                Tile::Fullscreen => &[ToplevelState::Fullscreen],
                Tile::Left => &[ToplevelState::TiledLeft, ToplevelState::TiledTop, ToplevelState::TiledBottom],
                Tile::Right => &[ToplevelState::TiledRight, ToplevelState::TiledTop, ToplevelState::TiledBottom],
                Tile::TopLeft => &[ToplevelState::TiledLeft, ToplevelState::TiledTop],
                Tile::TopRight => &[ToplevelState::TiledRight, ToplevelState::TiledTop],
                Tile::BottomLeft => &[ToplevelState::TiledLeft, ToplevelState::TiledBottom],
                Tile::BottomRight => &[ToplevelState::TiledRight, ToplevelState::TiledBottom],
            };
            for s in tiled {
                state.states.set(*s);
            }
            state.size = size;
        });
        if toplevel.is_initial_configure_sent() {
            toplevel.send_pending_configure();
        }
        if let Some(rect) = target {
            self.move_window(window, rect.loc);
        }
        if tile == Tile::Fullscreen && data.mapped.get() && !data.minimized.get() {
            self.space.raise_element(window, true);
            self.raise_in_stack(window);
        }
        self.windows_changed();
    }

    pub fn toggle_maximize(&mut self, window: &WindowElement) {
        let tile = if window.data().tile.get() == Tile::Maximized {
            Tile::Floating
        } else {
            Tile::Maximized
        };
        self.set_tile(window, tile);
    }

    pub fn toggle_fullscreen(&mut self, window: &WindowElement) {
        let tile = if window.data().tile.get() == Tile::Fullscreen {
            Tile::Floating
        } else {
            Tile::Fullscreen
        };
        self.set_tile(window, tile);
    }

    /// Returns a tiled window to its floating size for a title bar drag
    /// that started at `anchor` and has reached `pointer`: the pointer keeps
    /// the same horizontal fraction of the title bar. Returns the new frame
    /// location.
    pub fn float_for_drag(
        &mut self,
        window: &WindowElement,
        anchor: Point<f64, Logical>,
        pointer: Point<f64, Logical>,
    ) -> Point<i32, Logical> {
        let data = window.data();
        let current = self
            .window_rect(window)
            .unwrap_or_else(|| Rectangle::new(data.location.get(), window.frame().size));
        let restore = data.restore.get().unwrap_or(current);
        let fraction = ((anchor.x - f64::from(current.loc.x)) / f64::from(current.size.w.max(1))).clamp(0.0, 1.0);
        let location = Point::from((
            (pointer.x - fraction * f64::from(restore.size.w)).round() as i32,
            (pointer.y - f64::from(TITLEBAR_HEIGHT) / 2.0).round() as i32,
        ));
        data.restore.set(Some(Rectangle::new(location, restore.size)));
        self.set_tile(window, Tile::Floating);
        location
    }

    /// The snap layout a window dropped at `pointer` would take, and the
    /// rectangle to preview.
    pub fn snap_target(&self, pointer: Point<f64, Logical>) -> Option<(Tile, Rectangle<i32, Logical>)> {
        let output = self
            .space
            .outputs()
            .find(|o| self.space.output_geometry(o).is_some_and(|g| g.to_f64().contains(pointer)))?;
        let geo = self.space.output_geometry(output)?.to_f64();
        let tile = snap_tile(geo, pointer)?;
        let rect = tile.rect(self.usable_area(output))?;
        Some((tile, rect))
    }

    pub fn minimize_window(&mut self, window: &WindowElement) {
        let data = window.data();
        if data.minimized.get() || !data.mapped.get() {
            return;
        }
        data.minimized.set(true);
        self.space.unmap_elem(window);
        self.update_keyboard_focus();
        self.windows_changed();
    }

    /// The taskbar's click: activates a window, or minimizes it when it is
    /// already the active one.
    pub fn toggle_minimize(&mut self, window: &WindowElement) {
        let is_focused = self.focused_window().as_ref() == Some(window);
        if is_focused && !window.data().minimized.get() {
            self.minimize_window(window);
        } else {
            self.activate_window(window);
        }
    }

    pub fn close_window(&mut self, window: &WindowElement) {
        window.toplevel().send_close();
    }

    /// Re-fits maximized, tiled and fullscreen windows after an output or a
    /// panel changed size.
    pub fn usable_area_changed(&mut self, output: &Output) {
        let Some(geo) = self.space.output_geometry(output) else {
            return;
        };
        for window in self.windows.clone() {
            let data = window.data();
            if data.tile.get() == Tile::Floating || !data.mapped.get() {
                continue;
            }
            let rect = Rectangle::new(data.location.get(), window.frame().size);
            if rect.overlaps(geo) {
                self.set_tile(&window, data.tile.get());
            }
        }
        self.schedule_redraw();
    }

    // ---------------------------------------------------------------------
    // Workspaces
    // ---------------------------------------------------------------------

    pub fn switch_workspace(&mut self, index: usize) {
        if index >= self.workspace_count || index == self.workspace {
            return;
        }
        self.workspace = index;
        self.switcher = None;
        // `windows` is in stacking order, so remapping in order keeps it.
        for window in self.windows.clone() {
            let data = window.data();
            if !data.mapped.get() {
                continue;
            }
            if data.workspace.get() == index && !data.minimized.get() {
                self.space.map_element(window.clone(), data.location.get(), false);
            } else {
                self.space.unmap_elem(&window);
            }
        }
        self.update_keyboard_focus();
        self.windows_changed();
        self.ipc_event("workspace", self.workspaces_json());
    }

    pub fn move_to_workspace(&mut self, window: &WindowElement, index: usize) {
        if index >= self.workspace_count || window.data().workspace.get() == index {
            return;
        }
        window.data().workspace.set(index);
        if index != self.workspace {
            self.space.unmap_elem(window);
            self.update_keyboard_focus();
        }
        self.windows_changed();
    }

    // ---------------------------------------------------------------------
    // Frames and popups
    // ---------------------------------------------------------------------

    /// Applies a decoration mode: the compositor draws the frame for
    /// clients that ask for server-side decoration.
    pub fn set_decoration_mode(&mut self, surface: &WlSurface, mode: DecorationMode) {
        if let Some(window) = self.window_for_surface(surface) {
            window.data().ssd.set(mode == DecorationMode::ServerSide);
            self.schedule_redraw();
        }
    }

    /// Redraws the title bars whose content changed. Called before each
    /// frame; a no-op when nothing changed.
    pub fn update_decorations(&mut self) {
        let focused = self.focused_window();
        let scale = self.max_scale();
        let theme = self.theme;
        let windows: Vec<WindowElement> = self.space.elements().cloned().collect();
        let mut redrawn = false;
        for window in windows {
            let active = focused.as_ref() == Some(&window);
            let mut decoration = window.data().decoration.borrow_mut();
            decoration.ensure_shadow(&window);
            redrawn |= decoration.update(&window, active, scale, self.text(), theme);
        }
        if redrawn {
            self.schedule_redraw();
        }
    }

    /// Keeps a popup on its output.
    pub fn unconstrain_popup(&self, popup: &PopupSurface) {
        let Ok(root) = find_popup_root_surface(&PopupKind::Xdg(popup.clone())) else {
            return;
        };
        let Some(window) = self.window_for_surface(&root) else {
            return;
        };
        let (Some(origin), Some(rect)) = (self.window_origin(&window), self.window_rect(&window)) else {
            return;
        };
        let Some(output) = self.output_for_rect(rect) else {
            return;
        };
        let Some(mut target) = self.space.output_geometry(&output) else {
            return;
        };
        // The positioner works relative to the parent's window geometry.
        target.loc -= origin + Point::from((0, window.titlebar_height())) + window.0.geometry().loc;
        target.loc -= get_popup_toplevel_coords(&PopupKind::Xdg(popup.clone()));
        popup.with_pending_state(|state| {
            state.geometry = state.positioner.get_unconstrained_geometry(target);
        });
    }
}

/// The snap layout for a pointer position on an output, if it is at an edge.
pub fn snap_tile(output: Rectangle<f64, Logical>, pointer: Point<f64, Logical>) -> Option<Tile> {
    let left = pointer.x - output.loc.x < SNAP_DISTANCE;
    let right = output.loc.x + output.size.w - pointer.x <= SNAP_DISTANCE;
    let top = pointer.y - output.loc.y < SNAP_DISTANCE;
    let bottom = output.loc.y + output.size.h - pointer.y <= SNAP_DISTANCE;
    // The outer quarter of a side edge snaps to that corner.
    let quarter = output.size.h / 4.0;
    let upper = pointer.y - output.loc.y < quarter;
    let lower = output.loc.y + output.size.h - pointer.y < quarter;
    match (left, right, top, bottom) {
        (true, _, _, _) if upper => Some(Tile::TopLeft),
        (true, _, _, _) if lower => Some(Tile::BottomLeft),
        (true, _, _, _) => Some(Tile::Left),
        (_, true, _, _) if upper => Some(Tile::TopRight),
        (_, true, _, _) if lower => Some(Tile::BottomRight),
        (_, true, _, _) => Some(Tile::Right),
        (_, _, true, _) => Some(Tile::Maximized),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn output() -> Rectangle<f64, Logical> {
        Rectangle::new(Point::from((0.0, 0.0)), Size::from((1920.0, 1080.0)))
    }

    #[test]
    fn edges_snap_and_the_middle_does_not() {
        let at = |x: f64, y: f64| snap_tile(output(), Point::from((x, y)));
        assert_eq!(at(960.0, 540.0), None);
        assert_eq!(at(960.0, 2.0), Some(Tile::Maximized));
        assert_eq!(at(3.0, 540.0), Some(Tile::Left));
        assert_eq!(at(1917.0, 540.0), Some(Tile::Right));
        assert_eq!(at(3.0, 100.0), Some(Tile::TopLeft));
        assert_eq!(at(3.0, 1000.0), Some(Tile::BottomLeft));
        assert_eq!(at(1919.0, 20.0), Some(Tile::TopRight));
        assert_eq!(at(1919.0, 1079.0), Some(Tile::BottomRight));
        assert_eq!(at(960.0, 1079.0), None, "the bottom edge belongs to the panel");
    }

    #[test]
    fn snapping_follows_the_output_origin() {
        let second = Rectangle::new(Point::from((1920.0, 0.0)), Size::from((1280.0, 720.0)));
        assert_eq!(snap_tile(second, Point::from((1922.0, 360.0))), Some(Tile::Left));
        assert_eq!(snap_tile(second, Point::from((2500.0, 360.0))), None);
    }

    #[test]
    fn tiles_partition_the_usable_area() {
        let area = Rectangle::new(Point::from((0, 0)), Size::from((1921, 1033)));
        let left = Tile::Left.rect(area).unwrap();
        let right = Tile::Right.rect(area).unwrap();
        assert_eq!(left.size.w + right.size.w, area.size.w, "odd widths leave no gap");
        assert_eq!(right.loc.x, left.size.w);
        let quarters: i32 = [Tile::TopLeft, Tile::TopRight, Tile::BottomLeft, Tile::BottomRight]
            .into_iter()
            .map(|t| {
                let r = t.rect(area).unwrap();
                r.size.w * r.size.h
            })
            .sum();
        assert_eq!(quarters, area.size.w * area.size.h);
        assert_eq!(Tile::Maximized.rect(area), Some(area));
        assert_eq!(Tile::Floating.rect(area), None);
        assert_eq!(Tile::Fullscreen.rect(area), None);
    }
}
