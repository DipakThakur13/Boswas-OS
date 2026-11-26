//! Window management policy: placement, focus, snapping, minimize and
//! workspaces.

use smithay::{
    desktop::{find_popup_root_surface, get_popup_toplevel_coords, layer_map_for_output, PopupKind},
    output::Output,
    reexports::{
        wayland_protocols::xdg::decoration::zv1::server::zxdg_toplevel_decoration_v1::Mode as DecorationMode,
        wayland_server::protocol::wl_surface::WlSurface,
    },
    utils::{IsAlive, Logical, Point, Rectangle, Size, SERIAL_COUNTER},
    wayland::shell::{
        wlr_layer::{KeyboardInteractivity, Layer},
        xdg::PopupSurface,
    },
};

use super::{
    decoration::{ICON_SIZE, TITLEBAR_HEIGHT},
    element::{Tile, WindowElement},
};
use crate::{
    animation::{GhostKind, WindowAnimation, DESKTOP_SLIDE},
    focus::KeyboardFocus,
    input::Direction,
    state::{root_surface, Boswas},
};

/// The most desktops a session can have.
pub const MAX_WORKSPACES: usize = 16;

/// Super+arrow: the arrangement a window takes next, or `None` to minimize
/// it. Sideways keys move a window to that half (and a quarter to the
/// quarter on that side, the opposite half back to floating); up grows it
/// (half to upper quarter, upper quarter and floating to maximized); down
/// shrinks it (maximized to floating, half to lower quarter, upper quarter
/// to half, and from floating or a lower quarter away into the taskbar).
pub fn snap_step(tile: Tile, direction: Direction) -> Option<Tile> {
    use Direction as D;
    use Tile as T;
    Some(match (direction, tile) {
        (_, T::Fullscreen) => T::Fullscreen,
        (D::Left, T::Right) | (D::Right, T::Left) => T::Floating,
        (D::Left, T::TopRight) => T::TopLeft,
        (D::Left, T::BottomRight) => T::BottomLeft,
        (D::Left, T::TopLeft | T::BottomLeft) => tile,
        (D::Left, _) => T::Left,
        (D::Right, T::TopLeft) => T::TopRight,
        (D::Right, T::BottomLeft) => T::BottomRight,
        (D::Right, T::TopRight | T::BottomRight) => tile,
        (D::Right, _) => T::Right,
        (D::Up, T::Left) => T::TopLeft,
        (D::Up, T::Right) => T::TopRight,
        (D::Up, T::BottomLeft) => T::Left,
        (D::Up, T::BottomRight) => T::Right,
        (D::Up, _) => T::Maximized,
        (D::Down, T::Maximized) => T::Floating,
        (D::Down, T::Left) => T::BottomLeft,
        (D::Down, T::Right) => T::BottomRight,
        (D::Down, T::TopLeft) => T::Left,
        (D::Down, T::TopRight) => T::Right,
        (D::Down, T::Floating | T::BottomLeft | T::BottomRight) => return None,
    })
}

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
        if let Some(toplevel) = window.toplevel() {
            if !toplevel.is_initial_configure_sent() {
                let bounds = self
                    .active_output()
                    .map(|output| self.usable_area(&output).size)
                    .map(|size| Size::from((size.w, (size.h - TITLEBAR_HEIGHT).max(1))));
                toplevel.with_pending_state(|state| state.bounds = bounds);
                toplevel.send_configure();
                return;
            }
        }
        if window.is_x11() {
            // X11 windows are placed when they ask to be mapped.
            return;
        }
        if !window.data().mapped.get() && !window.0.bbox().is_empty() {
            self.place_new_window(window);
        }
    }

    /// Shows a window for the first time: centred on the output under the
    /// pointer, each new window a step down and right of the last.
    pub fn place_new_window(&mut self, window: &WindowElement) {
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
        data.animation.set(Some(WindowAnimation::open()));
        self.space.map_element(window.clone(), location, true);
        window.sync_x11_position(location);
        self.raise_in_stack(window);
        self.focus_window(window);
        self.windows_changed();
    }

    /// Moves a mapped window's frame to a new location.
    pub fn move_window(&mut self, window: &WindowElement, location: Point<i32, Logical>) {
        window.data().location.set(location);
        if self.space.element_location(window).is_some() {
            self.space.map_element(window.clone(), location, false);
            self.schedule_redraw();
        }
        window.sync_x11_position(location);
    }

    /// Forgets a window whose toplevel is gone.
    pub fn remove_window(&mut self, surface: &WlSurface) {
        if let Some(window) = self.window_for_surface(surface) {
            self.forget_window(&window);
        }
    }

    /// Forgets a window that is gone.
    pub fn forget_window(&mut self, window: &WindowElement) {
        let window = window.clone();
        let id = window.id();
        if self.space.element_location(&window).is_some() && !window.is_override_redirect() {
            self.leave_ghost(&window, GhostKind::Close);
        }
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
            let rect = Rectangle::new(data.location.get(), window.frame().size);
            data.animation.set(Some(WindowAnimation::restore(self.taskbar_point(rect))));
            self.space.map_element(window.clone(), data.location.get(), true);
        } else {
            self.space.raise_element(window, true);
        }
        self.raise_in_stack(window);
        if let Some(x11) = window.x11() {
            // The X server keeps its own stacking order.
            if let Some(xwm) = self.xwm.as_mut() {
                let _ = xwm.raise_window(x11);
            }
        }
        self.focus_window(window);
        self.windows_changed();
    }

    /// Gives the keyboard to a window.
    pub fn focus_window(&mut self, window: &WindowElement) {
        let focus = window.keyboard_focus();
        // An X11 window without its surface yet gets the keyboard as soon
        // as it has one.
        window.data().wants_focus.set(focus.is_none());
        self.set_focus(focus);
    }

    /// Gives the keyboard to a surface and updates which window is drawn as
    /// active.
    pub fn set_keyboard_focus(&mut self, surface: Option<WlSurface>) {
        // The surface of an X11 window stands for that window.
        let focus = surface.map(|surface| {
            match self.window_for_surface(&surface).and_then(|window| window.x11().cloned()) {
                Some(window) => KeyboardFocus::X11 { window, surface },
                None => KeyboardFocus::Surface(surface),
            }
        });
        self.set_focus(focus);
    }

    fn set_focus(&mut self, focus: Option<KeyboardFocus>) {
        let serial = SERIAL_COUNTER.next_serial();
        let keyboard = self.keyboard.clone();
        if keyboard.current_focus() != focus {
            keyboard.set_focus(self, focus.clone(), serial);
        }
        let focused = focus
            .as_ref()
            .and_then(|focus| self.window_for_surface(&root_surface(focus.surface())))
            .map(|window| window.id());
        for window in &self.windows {
            let active = focused == Some(window.id());
            window.set_activated(active);
            if active {
                let id = window.id();
                self.focus_history.retain(|w| *w != id);
                self.focus_history.push(id);
                if window.data().urgent.replace(false) {
                    self.ipc.mark_windows_dirty();
                }
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
        let takes_keyboard = |state: &Self, root: &WlSurface| {
            state.space.outputs().any(|output| {
                layer_map_for_output(output).layers().any(|l| {
                    l.wl_surface() == root && l.cached_state().keyboard_interactivity != KeyboardInteractivity::None
                })
            })
        };
        let current = self.keyboard.current_focus().filter(|focus| focus.alive());
        if exclusive.is_some() {
            // What had the keyboard before: a layer surface that takes it
            // on demand gets it back afterwards (a window is found again
            // as the top window).
            if let Some(root) = current.as_ref().map(|focus| root_surface(focus.surface())) {
                if Some(&root) != exclusive.as_ref() {
                    let layer = self.window_for_surface(&root).is_none() && takes_keyboard(self, &root);
                    self.keyboard_before_exclusive = layer.then_some(root);
                }
            }
            return self.set_keyboard_focus(exclusive);
        }

        if let Some(surface) = current.as_ref().map(KeyboardFocus::surface) {
            let root = root_surface(surface);
            let on_window = self
                .focused_window()
                .is_some_and(|w| self.space.element_location(&w).is_some());
            let on_layer = takes_keyboard(self, &root);
            if on_window || on_layer {
                self.keyboard_before_exclusive = None;
                return self.set_focus(current);
            }
        }
        // An exclusive layer surface went away: the desktop it took the
        // keyboard from has it again.
        if let Some(surface) = self.keyboard_before_exclusive.take().filter(|s| s.alive() && takes_keyboard(self, s)) {
            return self.set_keyboard_focus(Some(surface));
        }
        // The top window that is one (not a menu of an X11 application).
        let top = self.space.elements().rev().find(|w| !w.is_override_redirect()).cloned();
        match top {
            Some(window) => self.focus_window(&window),
            None => self.set_focus(None),
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
        window.configure(target, tile);
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
        // Multitasking > Snap windows: off, a dragged window lands where it
        // is dropped.
        if !self.config.multitasking.snap_windows {
            return None;
        }
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
        if let Some(rect) = self.window_rect(window) {
            let taskbar = self.taskbar_point(rect);
            self.leave_ghost(window, GhostKind::Minimize { taskbar });
        }
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
        window.send_close();
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
        // The desktops lie side by side: going to a later one moves
        // everything to the left.
        let slide = if index > self.workspace { DESKTOP_SLIDE } else { -DESKTOP_SLIDE };
        let leaving: Vec<WindowElement> = self.space.elements().cloned().collect();
        for window in leaving.iter().rev().take(4) {
            self.leave_ghost(window, GhostKind::SlideOut { offset: -slide });
        }
        self.workspace = index;
        self.switcher = None;
        self.show_desktop = None;
        // `windows` is in stacking order, so remapping in order keeps it.
        for window in self.windows.clone() {
            let data = window.data();
            if !data.mapped.get() {
                continue;
            }
            if data.workspace.get() == index && !data.minimized.get() {
                data.animation.set(Some(WindowAnimation::slide_in(slide)));
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
        let data = window.data();
        data.workspace.set(index);
        if index != self.workspace {
            self.space.unmap_elem(window);
            self.update_keyboard_focus();
        } else if data.mapped.get() && !data.minimized.get() {
            self.space.map_element(window.clone(), data.location.get(), false);
        }
        self.windows_changed();
    }

    /// Adds a desktop after the last one and returns its index.
    pub fn add_workspace(&mut self) -> usize {
        if self.workspace_count < MAX_WORKSPACES {
            self.workspace_count += 1;
            self.ipc_event("workspace", self.workspaces_json());
        }
        self.workspace_count - 1
    }

    /// Closes a desktop: its windows move to the one before it (the one
    /// after, for the first), and the desktops behind it move up. The last
    /// remaining desktop cannot be closed.
    pub fn remove_workspace(&mut self, index: usize) {
        if self.workspace_count <= 1 || index >= self.workspace_count {
            return;
        }
        let target = index.saturating_sub(1);
        for window in &self.windows {
            let workspace = &window.data().workspace;
            match workspace.get() {
                w if w == index => workspace.set(target),
                w if w > index => workspace.set(w - 1),
                _ => {}
            }
        }
        self.workspace_count -= 1;
        // The active desktop keeps its windows; only its number may change.
        let active = match self.workspace {
            w if w == index => target,
            w if w > index => w - 1,
            w => w,
        };
        self.workspace = active;
        self.show_desktop = None;
        for window in self.windows.clone() {
            let data = window.data();
            if !data.mapped.get() {
                continue;
            }
            if data.workspace.get() == active && !data.minimized.get() {
                if self.space.element_location(&window).is_none() {
                    self.space.map_element(window.clone(), data.location.get(), false);
                }
            } else {
                self.space.unmap_elem(&window);
            }
        }
        self.update_keyboard_focus();
        self.windows_changed();
        self.ipc_event("workspace", self.workspaces_json());
    }

    // ---------------------------------------------------------------------
    // Show desktop
    // ---------------------------------------------------------------------

    /// Shows the desktop (minimizes every window of this desktop) or brings
    /// the windows back; `None` toggles.
    pub fn set_show_desktop(&mut self, show: Option<bool>) {
        if self.lock.locked {
            return;
        }
        let show = show.unwrap_or(self.show_desktop.is_none());
        if show == self.show_desktop.is_some() {
            return;
        }
        if show {
            // Bottom to top, so restoring in this order rebuilds the stack.
            let hidden: Vec<u64> = self
                .space
                .elements()
                .filter(|w| w.data().workspace.get() == self.workspace && !w.is_override_redirect())
                .map(|w| w.id())
                .collect();
            for id in &hidden {
                if let Some(window) = self.window_by_id(*id) {
                    window.data().minimized.set(true);
                    self.space.unmap_elem(&window);
                }
            }
            self.show_desktop = Some(hidden);
            self.update_keyboard_focus();
        } else {
            let focus = self.focus_history.last().copied();
            let hidden = self.show_desktop.take().unwrap_or_default();
            let mut top = None;
            for id in hidden {
                let Some(window) = self.window_by_id(id) else {
                    continue;
                };
                let data = window.data();
                if !data.minimized.get() || data.workspace.get() != self.workspace {
                    continue;
                }
                data.minimized.set(false);
                self.space.map_element(window.clone(), data.location.get(), false);
                if top.is_none() || focus == Some(id) {
                    top = Some(window);
                }
            }
            match top {
                Some(window) => self.activate_window(&window),
                None => self.update_keyboard_focus(),
            }
        }
        self.windows_changed();
        let active = self.show_desktop.is_some();
        self.ipc_event("show-desktop", serde_json::json!({ "active": active }));
    }

    // ---------------------------------------------------------------------
    // Monitors
    // ---------------------------------------------------------------------

    /// Super+Shift+Left/Right: moves a window to the monitor on that side,
    /// keeping its arrangement and, for floating windows, its relative
    /// position.
    pub fn move_to_next_output(&mut self, window: &WindowElement, direction: Direction) {
        let Some(rect) = self.window_rect(window) else {
            return;
        };
        let Some(current) = self.output_for_rect(rect) else {
            return;
        };
        let Some(from) = self.space.output_geometry(&current) else {
            return;
        };
        let centre = |geo: Rectangle<i32, Logical>| geo.loc.x + geo.size.w / 2;
        let candidates = self.space.outputs().filter(|o| **o != current).filter_map(|o| {
            let geo = self.space.output_geometry(o)?;
            let delta = centre(geo) - centre(from);
            let wanted = match direction {
                Direction::Left => delta < 0,
                Direction::Right => delta > 0,
                _ => false,
            };
            wanted.then_some((delta.abs(), o.clone()))
        });
        let Some((_, target)) = candidates.min_by_key(|(distance, _)| *distance) else {
            return;
        };
        let (from_area, to_area) = (self.usable_area(&current), self.usable_area(&target));
        let data = window.data();
        let tile = data.tile.get();
        // Where the floating frame goes: the same fraction of the new area.
        let relocate = |r: Rectangle<i32, Logical>| {
            let fx = f64::from(r.loc.x - from_area.loc.x) / f64::from(from_area.size.w.max(1));
            let fy = f64::from(r.loc.y - from_area.loc.y) / f64::from(from_area.size.h.max(1));
            let size = Size::from((r.size.w.min(to_area.size.w), r.size.h.min(to_area.size.h)));
            let x = to_area.loc.x + (fx * f64::from(to_area.size.w)).round() as i32;
            let y = to_area.loc.y + (fy * f64::from(to_area.size.h)).round() as i32;
            Rectangle::new(
                Point::from((
                    x.clamp(to_area.loc.x, (to_area.loc.x + to_area.size.w - size.w).max(to_area.loc.x)),
                    y.clamp(to_area.loc.y, (to_area.loc.y + to_area.size.h - size.h).max(to_area.loc.y)),
                )),
                size,
            )
        };
        if tile == Tile::Floating {
            let moved = relocate(rect);
            if moved.size != rect.size {
                window.resize_client(window.client_size_for_frame(moved.size), false);
            }
            self.move_window(window, moved.loc);
        } else {
            if let Some(restore) = data.restore.get() {
                data.restore.set(Some(relocate(restore)));
            }
            // Put the frame on the target first; `set_tile` then fits it to
            // the output it finds the frame on.
            self.move_window(window, to_area.loc);
            self.set_tile(window, tile);
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
        let rounds = self.rounds_corners;
        let icon_size = (f64::from(ICON_SIZE) * scale.ceil()) as u32;
        // In Task View every window of the desktop is on screen, minimized
        // ones too.
        let windows: Vec<WindowElement> = if self.overview.is_some() || self.switcher.is_some() {
            self.windows.iter().filter(|w| w.data().mapped.get()).cloned().collect()
        } else {
            self.space.elements().cloned().collect()
        };
        let mut redrawn = false;
        for window in windows {
            let active = focused.as_ref() == Some(&window);
            let icon = if window.titlebar_height() > 0 {
                self.app_icons.get(&window.app_id(), icon_size)
            } else {
                None
            };
            let mut decoration = window.data().decoration.borrow_mut();
            decoration.ensure_shadow(&window);
            let text_scale = self.config.accessibility.text_scale;
            redrawn |= decoration.update(&window, active, scale, rounds, icon, self.text(), theme, text_scale);
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
    fn super_arrows_step_through_the_layouts() {
        use Direction::*;
        let walk = |start: Tile, keys: &[Direction]| keys.iter().try_fold(start, |tile, key| snap_step(tile, *key));
        assert_eq!(walk(Tile::Floating, &[Left]), Some(Tile::Left));
        assert_eq!(walk(Tile::Floating, &[Left, Up]), Some(Tile::TopLeft));
        assert_eq!(walk(Tile::Floating, &[Left, Up, Up]), Some(Tile::Maximized));
        assert_eq!(walk(Tile::Floating, &[Left, Up, Right]), Some(Tile::TopRight));
        assert_eq!(walk(Tile::Floating, &[Left, Down]), Some(Tile::BottomLeft));
        assert_eq!(walk(Tile::Floating, &[Left, Down, Up]), Some(Tile::Left));
        assert_eq!(walk(Tile::Floating, &[Left, Right]), Some(Tile::Floating), "the opposite key restores");
        assert_eq!(walk(Tile::Floating, &[Up]), Some(Tile::Maximized));
        assert_eq!(walk(Tile::Floating, &[Up, Down]), Some(Tile::Floating));
        assert_eq!(walk(Tile::Floating, &[Down]), None, "down on a floating window minimizes it");
        assert_eq!(walk(Tile::Floating, &[Right, Down, Down]), None);
        assert_eq!(walk(Tile::Maximized, &[Left]), Some(Tile::Left));
        for key in [Left, Right, Up, Down] {
            assert_eq!(snap_step(Tile::Fullscreen, key), Some(Tile::Fullscreen), "fullscreen ignores the arrows");
        }
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
