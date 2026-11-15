//! Input: keyboard shortcuts, pointer focus, clicks on window frames.
//!
//! Every device event ends in one of the `on_*` methods, which take plain
//! values; the control socket's test-input commands call the same methods,
//! so tests exercise exactly the code real devices do.

use std::time::Instant;

use smithay::{
    backend::input::{
        AbsolutePositionEvent, Axis, AxisSource, ButtonState, Event, GestureBeginEvent, GestureEndEvent,
        GesturePinchUpdateEvent as _, GestureSwipeUpdateEvent as _, InputBackend, InputEvent, KeyState,
        KeyboardKeyEvent, PointerAxisEvent, PointerButtonEvent, PointerMotionEvent,
    },
    input::{
        keyboard::{keysyms, FilterResult, Keysym, ModifiersState},
        pointer::{
            AxisFrame, ButtonEvent, CursorIcon, CursorImageStatus, GestureHoldBeginEvent, GestureHoldEndEvent,
            GesturePinchBeginEvent, GesturePinchEndEvent, GesturePinchUpdateEvent, GestureSwipeBeginEvent,
            GestureSwipeEndEvent, GestureSwipeUpdateEvent, GrabStartData, MotionEvent, RelativeMotionEvent,
        },
    },
    utils::{Logical, Point, SERIAL_COUNTER},
    wayland::{
        keyboard_shortcuts_inhibit::KeyboardShortcutsInhibitorSeat,
        pointer_constraints::{with_pointer_constraint, PointerConstraint},
        shell::wlr_layer::KeyboardInteractivity,
    },
};

use crate::{
    shell::{decoration::Button, grabs::ResizeEdge, overview::OverviewKey, wm::snap_step},
    state::{root_surface, Boswas, Under},
};

const BTN_LEFT: u32 = 0x110;
const BTN_RIGHT: u32 = 0x111;

/// A direction of the arrow keys.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    Left,
    Right,
    Up,
    Down,
}

/// A key the shell acts on (volume, brightness, media transport).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MediaKey {
    VolumeUp,
    VolumeDown,
    VolumeMute,
    MicMute,
    BrightnessUp,
    BrightnessDown,
    Play,
    Next,
    Previous,
    Stop,
}

impl MediaKey {
    /// The name on the control socket.
    pub fn name(self) -> &'static str {
        match self {
            MediaKey::VolumeUp => "volume-up",
            MediaKey::VolumeDown => "volume-down",
            MediaKey::VolumeMute => "volume-mute",
            MediaKey::MicMute => "mic-mute",
            MediaKey::BrightnessUp => "brightness-up",
            MediaKey::BrightnessDown => "brightness-down",
            MediaKey::Play => "media-play",
            MediaKey::Next => "media-next",
            MediaKey::Previous => "media-previous",
            MediaKey::Stop => "media-stop",
        }
    }

    fn from_keysym(sym: u32) -> Option<Self> {
        Some(match sym {
            keysyms::KEY_XF86AudioRaiseVolume => MediaKey::VolumeUp,
            keysyms::KEY_XF86AudioLowerVolume => MediaKey::VolumeDown,
            keysyms::KEY_XF86AudioMute => MediaKey::VolumeMute,
            keysyms::KEY_XF86AudioMicMute => MediaKey::MicMute,
            keysyms::KEY_XF86MonBrightnessUp => MediaKey::BrightnessUp,
            keysyms::KEY_XF86MonBrightnessDown => MediaKey::BrightnessDown,
            keysyms::KEY_XF86AudioPlay | keysyms::KEY_XF86AudioPause => MediaKey::Play,
            keysyms::KEY_XF86AudioNext => MediaKey::Next,
            keysyms::KEY_XF86AudioPrev => MediaKey::Previous,
            keysyms::KEY_XF86AudioStop => MediaKey::Stop,
            _ => return None,
        })
    }
}

/// What a keyboard shortcut does. The shortcuts follow the conventions of
/// Windows, so nobody has to relearn them.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    /// Swallow the key without doing anything (the release of a shortcut).
    Swallow,
    SwitchVt(i32),
    Lock,
    /// Super on its own: Start.
    Launcher,
    Close,
    CycleWindows { backwards: bool },
    /// Super+arrow: snap, maximize, restore or minimize (see
    /// [`snap_step`](crate::shell::wm::snap_step)).
    Snap(Direction),
    Fullscreen,
    /// Super+Tab: Task View.
    Overview,
    /// Super+D.
    ShowDesktop,
    /// Super+M, and Super+Shift+M to bring the windows back.
    MinimizeAll { restore: bool },
    /// Super+Shift+Left/Right: move the window to the next monitor.
    MoveToOutput(Direction),
    /// Super+Ctrl+Left/Right: the previous or next desktop.
    SwitchDesktop { forward: bool },
    /// Super+Ctrl+D.
    NewDesktop,
    /// Super+Ctrl+F4.
    CloseDesktop,
    /// Super+Space.
    NextLayout,
    /// Super+1..9 (with Shift: a new instance).
    TaskbarApp { index: u8, shift: bool },
    QuickSettings,
    Notifications,
    Run,
    /// Print (the whole screen) and Super+Shift+S (a region).
    Capture { region: bool },
    Media(MediaKey),
    Terminal,
    Files,
    Settings,
    TaskManager,
    /// A key for the window switcher while it is open.
    Switcher(SwitcherKey),
    /// A key for Task View while it is open.
    TaskView(OverviewKey),
}

/// What a key does while the window switcher is open.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SwitcherKey {
    Step(i32),
    Accept,
    Cancel,
}

/// The key bindings of the window switcher: arrows move, Enter goes to the
/// window, Escape leaves everything as it was.
fn switcher_key(sym: Keysym) -> Option<SwitcherKey> {
    Some(match sym.raw() {
        keysyms::KEY_Left | keysyms::KEY_Up => SwitcherKey::Step(-1),
        keysyms::KEY_Right | keysyms::KEY_Down => SwitcherKey::Step(1),
        keysyms::KEY_Return | keysyms::KEY_KP_Enter => SwitcherKey::Accept,
        keysyms::KEY_Escape => SwitcherKey::Cancel,
        _ => return None,
    })
}

/// The key bindings of Task View.
fn overview_key(sym: Keysym, shift: bool) -> Option<OverviewKey> {
    Some(match sym.raw() {
        keysyms::KEY_Escape => OverviewKey::Cancel,
        keysyms::KEY_Return | keysyms::KEY_KP_Enter | keysyms::KEY_space => OverviewKey::Accept,
        keysyms::KEY_Delete => OverviewKey::Close,
        keysyms::KEY_Left => OverviewKey::Move(Direction::Left),
        keysyms::KEY_Right => OverviewKey::Move(Direction::Right),
        keysyms::KEY_Up => OverviewKey::Move(Direction::Up),
        keysyms::KEY_Down => OverviewKey::Move(Direction::Down),
        keysyms::KEY_Tab | keysyms::KEY_ISO_Left_Tab => OverviewKey::Next { backwards: shift },
        _ => return None,
    })
}

/// Maps a key press to a shortcut. `sym` is the key's symbol on the Latin
/// layout when there is one, so shortcuts work the same while typing in
/// Hindi, Bengali, Tamil or any other layout; `modified` is the symbol with
/// the current modifiers applied (VT switching, media keys).
pub fn shortcut(mods: &ModifiersState, sym: Keysym, modified: Keysym, locked: bool, inhibited: bool) -> Option<Action> {
    let raw = modified.raw();
    if (keysyms::KEY_XF86Switch_VT_1..=keysyms::KEY_XF86Switch_VT_12).contains(&raw) {
        return Some(Action::SwitchVt((raw - keysyms::KEY_XF86Switch_VT_1 + 1) as i32));
    }
    // Volume and brightness keys work on the lock screen too; the shell (or
    // the lock screen) decides what each does.
    if let Some(key) = MediaKey::from_keysym(raw) {
        return Some(Action::Media(key));
    }
    if locked {
        return None;
    }
    let sym = sym.raw();
    let lower = if (keysyms::KEY_A..=keysyms::KEY_Z).contains(&sym) {
        sym + (keysyms::KEY_a - keysyms::KEY_A)
    } else {
        sym
    };
    let only_logo = mods.logo && !mods.ctrl && !mods.alt;
    if only_logo && !mods.shift && lower == keysyms::KEY_l {
        return Some(Action::Lock);
    }
    if inhibited {
        // A client (remote desktop, virtual machine) has taken the window
        // management shortcuts; locking and VT switching stay available.
        return None;
    }
    if raw == keysyms::KEY_Print && !mods.alt && !mods.ctrl && !mods.logo {
        return Some(Action::Capture { region: mods.shift });
    }
    if mods.alt && !mods.ctrl && !mods.logo {
        match lower {
            keysyms::KEY_Tab | keysyms::KEY_ISO_Left_Tab => {
                return Some(Action::CycleWindows { backwards: mods.shift });
            }
            keysyms::KEY_F4 if !mods.shift => return Some(Action::Close),
            _ => {}
        }
    }
    if mods.ctrl && mods.alt && !mods.logo && !mods.shift && lower == keysyms::KEY_t {
        return Some(Action::Terminal);
    }
    if mods.ctrl && mods.shift && !mods.alt && !mods.logo && lower == keysyms::KEY_Escape {
        return Some(Action::TaskManager);
    }
    if !mods.logo && !mods.ctrl && !mods.alt && !mods.shift && lower == keysyms::KEY_F11 {
        return Some(Action::Fullscreen);
    }
    if mods.logo && mods.ctrl && !mods.alt && !mods.shift {
        return match lower {
            keysyms::KEY_Left => Some(Action::SwitchDesktop { forward: false }),
            keysyms::KEY_Right => Some(Action::SwitchDesktop { forward: true }),
            keysyms::KEY_d => Some(Action::NewDesktop),
            keysyms::KEY_F4 => Some(Action::CloseDesktop),
            _ => None,
        };
    }
    if only_logo {
        let digit = match lower {
            keysyms::KEY_1..=keysyms::KEY_9 => Some((lower - keysyms::KEY_1) as u8 + 1),
            // Shift+digit yields punctuation on the US layout.
            keysyms::KEY_exclam => Some(1),
            keysyms::KEY_at => Some(2),
            keysyms::KEY_numbersign => Some(3),
            keysyms::KEY_dollar => Some(4),
            keysyms::KEY_percent => Some(5),
            keysyms::KEY_asciicircum => Some(6),
            keysyms::KEY_ampersand => Some(7),
            keysyms::KEY_asterisk => Some(8),
            keysyms::KEY_parenleft => Some(9),
            _ => None,
        };
        if let Some(index) = digit {
            return Some(Action::TaskbarApp { index, shift: mods.shift });
        }
        if mods.shift {
            return match lower {
                keysyms::KEY_s => Some(Action::Capture { region: true }),
                keysyms::KEY_m => Some(Action::MinimizeAll { restore: true }),
                keysyms::KEY_Left => Some(Action::MoveToOutput(Direction::Left)),
                keysyms::KEY_Right => Some(Action::MoveToOutput(Direction::Right)),
                keysyms::KEY_Tab | keysyms::KEY_ISO_Left_Tab => Some(Action::Overview),
                _ => None,
            };
        }
        return match lower {
            keysyms::KEY_Up => Some(Action::Snap(Direction::Up)),
            keysyms::KEY_Down => Some(Action::Snap(Direction::Down)),
            keysyms::KEY_Left => Some(Action::Snap(Direction::Left)),
            keysyms::KEY_Right => Some(Action::Snap(Direction::Right)),
            keysyms::KEY_Tab => Some(Action::Overview),
            keysyms::KEY_a => Some(Action::QuickSettings),
            keysyms::KEY_n => Some(Action::Notifications),
            keysyms::KEY_d => Some(Action::ShowDesktop),
            keysyms::KEY_m => Some(Action::MinimizeAll { restore: false }),
            keysyms::KEY_e => Some(Action::Files),
            keysyms::KEY_i => Some(Action::Settings),
            keysyms::KEY_r => Some(Action::Run),
            keysyms::KEY_space => Some(Action::NextLayout),
            keysyms::KEY_F11 => Some(Action::Fullscreen),
            _ => None,
        };
    }
    None
}

fn is_logo(sym: Keysym) -> bool {
    matches!(sym.raw(), keysyms::KEY_Super_L | keysyms::KEY_Super_R)
}

fn is_alt(sym: Keysym) -> bool {
    matches!(sym.raw(), keysyms::KEY_Alt_L | keysyms::KEY_Alt_R | keysyms::KEY_Meta_L | keysyms::KEY_Meta_R)
}

impl Boswas {
    /// Dispatches a backend input event.
    pub fn process_input_event<B: InputBackend>(&mut self, event: InputEvent<B>) {
        match event {
            InputEvent::Keyboard { event } => {
                self.on_key(event.key_code().raw(), event.state(), event.time_msec());
            }
            InputEvent::PointerMotion { event } => {
                self.on_pointer_motion(event.delta(), event.delta_unaccel(), event.time(), event.time_msec());
            }
            InputEvent::PointerMotionAbsolute { event } => {
                let geo = self.space.outputs().next().and_then(|o| self.space.output_geometry(o));
                if let Some(geo) = geo {
                    let pos = event.position_transformed(geo.size) + geo.loc.to_f64();
                    self.on_pointer_position(pos, event.time_msec());
                }
            }
            InputEvent::PointerButton { event } => {
                self.on_pointer_button(event.button_code(), event.state(), event.time_msec());
            }
            InputEvent::PointerAxis { event } => self.on_pointer_axis::<B>(&event),
            InputEvent::GestureSwipeBegin { event } => {
                let pointer = self.pointer.clone();
                pointer.gesture_swipe_begin(
                    self,
                    &GestureSwipeBeginEvent {
                        serial: SERIAL_COUNTER.next_serial(),
                        time: event.time_msec(),
                        fingers: event.fingers(),
                    },
                );
            }
            InputEvent::GestureSwipeUpdate { event } => {
                let pointer = self.pointer.clone();
                pointer.gesture_swipe_update(
                    self,
                    &GestureSwipeUpdateEvent {
                        time: event.time_msec(),
                        delta: event.delta(),
                    },
                );
            }
            InputEvent::GestureSwipeEnd { event } => {
                let pointer = self.pointer.clone();
                pointer.gesture_swipe_end(
                    self,
                    &GestureSwipeEndEvent {
                        serial: SERIAL_COUNTER.next_serial(),
                        time: event.time_msec(),
                        cancelled: event.cancelled(),
                    },
                );
            }
            InputEvent::GesturePinchBegin { event } => {
                let pointer = self.pointer.clone();
                pointer.gesture_pinch_begin(
                    self,
                    &GesturePinchBeginEvent {
                        serial: SERIAL_COUNTER.next_serial(),
                        time: event.time_msec(),
                        fingers: event.fingers(),
                    },
                );
            }
            InputEvent::GesturePinchUpdate { event } => {
                let pointer = self.pointer.clone();
                pointer.gesture_pinch_update(
                    self,
                    &GesturePinchUpdateEvent {
                        time: event.time_msec(),
                        delta: event.delta(),
                        scale: event.scale(),
                        rotation: event.rotation(),
                    },
                );
            }
            InputEvent::GesturePinchEnd { event } => {
                let pointer = self.pointer.clone();
                pointer.gesture_pinch_end(
                    self,
                    &GesturePinchEndEvent {
                        serial: SERIAL_COUNTER.next_serial(),
                        time: event.time_msec(),
                        cancelled: event.cancelled(),
                    },
                );
            }
            InputEvent::GestureHoldBegin { event } => {
                let pointer = self.pointer.clone();
                pointer.gesture_hold_begin(
                    self,
                    &GestureHoldBeginEvent {
                        serial: SERIAL_COUNTER.next_serial(),
                        time: event.time_msec(),
                        fingers: event.fingers(),
                    },
                );
            }
            InputEvent::GestureHoldEnd { event } => {
                let pointer = self.pointer.clone();
                pointer.gesture_hold_end(
                    self,
                    &GestureHoldEndEvent {
                        serial: SERIAL_COUNTER.next_serial(),
                        time: event.time_msec(),
                        cancelled: event.cancelled(),
                    },
                );
            }
            _ => {}
        }
    }

    fn note_activity(&mut self) {
        self.last_input = Instant::now();
    }

    // ---------------------------------------------------------------------
    // Keyboard
    // ---------------------------------------------------------------------

    /// A key was pressed or released. `keycode` is the XKB keycode (the
    /// evdev code plus 8).
    pub fn on_key(&mut self, keycode: u32, state: KeyState, time: u32) {
        self.note_activity();
        let serial = SERIAL_COUNTER.next_serial();
        let keyboard = self.keyboard.clone();
        let locked = self.lock.locked;
        let inhibited = keyboard
            .current_focus()
            .is_some_and(|focus| self.seat.keyboard_shortcuts_inhibitor_for_surface(&focus).is_some_and(|i| i.is_active()));
        let pressed = state == KeyState::Pressed;

        let action = keyboard.input::<Action, _>(self, keycode.into(), state, serial, time, |data, mods, handle| {
            let modified = handle.modified_sym();
            let sym = handle.raw_latin_sym_or_raw_current_sym().unwrap_or(modified);

            if !pressed {
                // The release of an intercepted press is intercepted too, so
                // a client never sees half a key stroke.
                if data.suppressed_keys.remove(&keycode) {
                    return FilterResult::Intercept(Action::Swallow);
                }
                if is_alt(modified) && data.switcher.is_some() {
                    data.finish_switcher(false);
                }
                if is_logo(modified) && std::mem::take(&mut data.super_tap) && !locked && !inhibited {
                    return FilterResult::Intercept(Action::Launcher);
                }
                return FilterResult::Forward;
            }

            // Super on its own opens the launcher when it is released
            // without any other key or button in between.
            data.super_tap = is_logo(modified) && !mods.ctrl && !mods.alt && !mods.shift;
            // The switcher and Task View take the keys they understand;
            // Task View keeps every other key from the applications too.
            if data.switcher.is_some() {
                if let Some(key) = switcher_key(modified) {
                    data.suppressed_keys.insert(keycode);
                    return FilterResult::Intercept(Action::Switcher(key));
                }
            }
            if data.overview_active() && !locked {
                if let Some(key) = overview_key(modified, mods.shift).filter(|_| !mods.logo && !mods.alt && !mods.ctrl) {
                    data.suppressed_keys.insert(keycode);
                    return FilterResult::Intercept(Action::TaskView(key));
                }
            }
            match shortcut(mods, sym, modified, locked, inhibited) {
                Some(action) => {
                    data.suppressed_keys.insert(keycode);
                    FilterResult::Intercept(action)
                }
                None => FilterResult::Forward,
            }
        });

        // A layout switch by an XKB option (Alt+Shift and the like).
        if keyboard.modifier_state().serialized.layout_effective != self.layouts.1 {
            self.refresh_layouts();
        }
        if let Some(action) = action {
            self.run_action(action);
        }
    }

    fn run_action(&mut self, action: Action) {
        let focused = self.focused_window();
        let event = |state: &mut Self, name: &str| state.ipc_event(name, serde_json::json!({}));
        match action {
            Action::Swallow => {}
            Action::Switcher(SwitcherKey::Step(step)) => self.switcher_step(step),
            Action::Switcher(SwitcherKey::Accept) => self.finish_switcher(false),
            Action::Switcher(SwitcherKey::Cancel) => self.finish_switcher(true),
            Action::TaskView(key) => self.overview_key(key),
            Action::SwitchVt(vt) => self.backend.switch_vt(vt),
            Action::Lock => self.lock_session(),
            Action::Launcher => event(self, "launcher"),
            Action::QuickSettings => event(self, "quick-settings"),
            Action::Notifications => event(self, "notifications"),
            Action::Run => event(self, "run"),
            Action::Capture { region } => self.request_capture(region),
            Action::Media(key) => {
                let locked = self.lock.locked;
                self.ipc_event("media-key", serde_json::json!({ "key": key.name(), "locked": locked }));
            }
            Action::TaskbarApp { index, shift } => {
                self.ipc_event("taskbar-app", serde_json::json!({ "index": index, "shift": shift }));
            }
            Action::Close => {
                if let Some(window) = focused {
                    self.close_window(&window);
                }
            }
            Action::CycleWindows { backwards } => self.cycle_windows(backwards),
            Action::Snap(direction) => {
                if let Some(window) = focused {
                    match snap_step(window.data().tile.get(), direction) {
                        Some(tile) => self.set_tile(&window, tile),
                        None => self.minimize_window(&window),
                    }
                }
            }
            Action::Fullscreen => {
                if let Some(window) = focused {
                    self.toggle_fullscreen(&window);
                }
            }
            Action::Overview => self.set_overview(None),
            Action::ShowDesktop => self.set_show_desktop(None),
            Action::MinimizeAll { restore } => self.set_show_desktop(Some(!restore)),
            Action::MoveToOutput(direction) => {
                if let Some(window) = focused {
                    self.move_to_next_output(&window, direction);
                }
            }
            Action::SwitchDesktop { forward } => {
                let target = if forward { self.workspace + 1 } else { self.workspace.wrapping_sub(1) };
                self.switch_workspace(target);
            }
            Action::NewDesktop => {
                let index = self.add_workspace();
                self.switch_workspace(index);
            }
            Action::CloseDesktop => self.remove_workspace(self.workspace),
            Action::NextLayout => self.next_keyboard_layout(),
            Action::Terminal => {
                let command = self.config.apps.terminal.clone();
                self.spawn(&command);
            }
            Action::Files => {
                let command = self.config.apps.files.clone();
                self.spawn(&command);
            }
            Action::Settings => {
                let command = self.config.apps.settings.clone();
                self.spawn(&command);
            }
            Action::TaskManager => {
                let command = self.config.apps.task_manager.clone();
                self.spawn(&command);
            }
        }
    }

    /// Starts the interactive capture: the shell freezes the picture and
    /// lets the user choose and confirm what to keep.
    pub fn request_capture(&mut self, region: bool) {
        if self.lock.locked {
            return;
        }
        let mode = if region { "region" } else { "screen" };
        self.ipc_event("capture", serde_json::json!({ "mode": mode }));
    }

    /// Super+Space: the next keyboard layout of the configured list.
    pub fn next_keyboard_layout(&mut self) {
        let keyboard = self.keyboard.clone();
        keyboard.with_xkb_state(self, |mut context| context.cycle_next_layout());
        self.refresh_layouts();
    }

    // ---------------------------------------------------------------------
    // Pointer
    // ---------------------------------------------------------------------

    /// Keeps a position on the desktop: inside the output it is on, or the
    /// nearest one.
    fn clamp_to_outputs(&self, pos: Point<f64, Logical>) -> Point<f64, Logical> {
        let mut best: Option<(f64, Point<f64, Logical>)> = None;
        for output in self.space.outputs() {
            let Some(geo) = self.space.output_geometry(output) else {
                continue;
            };
            let geo = geo.to_f64();
            let clamped = Point::from((
                pos.x.clamp(geo.loc.x, geo.loc.x + geo.size.w - 1.0),
                pos.y.clamp(geo.loc.y, geo.loc.y + geo.size.h - 1.0),
            ));
            let distance = (clamped.x - pos.x).abs() + (clamped.y - pos.y).abs();
            if best.as_ref().is_none_or(|(d, _)| distance < *d) {
                best = Some((distance, clamped));
            }
        }
        best.map_or(pos, |(_, p)| p)
    }

    /// Relative motion from a mouse or touchpad.
    pub fn on_pointer_motion(
        &mut self,
        delta: Point<f64, Logical>,
        delta_unaccel: Point<f64, Logical>,
        utime: u64,
        time: u32,
    ) {
        self.note_activity();
        let pointer = self.pointer.clone();
        let location = pointer.current_location();
        let under = self.under(location).and_then(|u| u.focus());

        // Pointer constraints (games, 3D viewports).
        let mut locked = false;
        let mut confined_to = None;
        if let Some((surface, origin)) = &under {
            with_pointer_constraint(surface, &pointer, |constraint| {
                let Some(constraint) = constraint.filter(|c| c.is_active()) else {
                    return;
                };
                let local = (location - *origin).to_i32_round();
                if !constraint.region().is_none_or(|region| region.contains(local)) {
                    return;
                }
                match &*constraint {
                    PointerConstraint::Locked(_) => locked = true,
                    PointerConstraint::Confined(confine) => confined_to = Some(confine.region().cloned()),
                }
            });
        }

        pointer.relative_motion(
            self,
            under.clone(),
            &RelativeMotionEvent {
                delta,
                delta_unaccel,
                utime,
            },
        );
        if locked {
            pointer.frame(self);
            return;
        }

        let target = self.clamp_to_outputs(location + delta);
        if let (Some(region), Some((surface, origin))) = (confined_to, &under) {
            let new_under = self.under(target).and_then(|u| u.focus());
            let same_surface = new_under.as_ref().is_some_and(|(s, _)| s == surface);
            let in_region = region.is_none_or(|r| r.contains((target - *origin).to_i32_round()));
            if !same_surface || !in_region {
                pointer.frame(self);
                return;
            }
        }
        self.on_pointer_position(target, time);

        // Entering a surface with a pending constraint activates it.
        if let Some((surface, origin)) = self.under(target).and_then(|u| u.focus()) {
            with_pointer_constraint(&surface, &pointer, |constraint| {
                if let Some(constraint) = constraint.filter(|c| !c.is_active()) {
                    let local = (target - origin).to_i32_round();
                    if constraint.region().is_none_or(|region| region.contains(local)) {
                        constraint.activate();
                    }
                }
            });
        }
    }

    /// The pointer is now at an absolute position on the desktop.
    pub fn on_pointer_position(&mut self, pos: Point<f64, Logical>, time: u32) {
        self.note_activity();
        let pointer = self.pointer.clone();
        if !self.lock.locked && (self.overview_active() || self.switcher.is_some()) {
            // The pointer belongs to Task View or the switcher: no
            // application sees it.
            if self.overview_active() {
                self.overview_motion(pos);
            } else {
                self.switcher_point(pos);
            }
            self.cursor_status = CursorImageStatus::Named(CursorIcon::Default);
            pointer.motion(
                self,
                None,
                &MotionEvent {
                    location: pos,
                    serial: SERIAL_COUNTER.next_serial(),
                    time,
                },
            );
            pointer.frame(self);
            self.schedule_redraw();
            return;
        }
        let under = self.under(pos);
        if !pointer.is_grabbed() {
            self.update_frame_hover(under.as_ref());
        }
        let focus = under.and_then(|u| u.focus());
        pointer.motion(
            self,
            focus,
            &MotionEvent {
                location: pos,
                serial: SERIAL_COUNTER.next_serial(),
                time,
            },
        );
        pointer.frame(self);
        self.schedule_redraw();
    }

    /// Highlights the title bar button under the pointer and picks the
    /// cursor for window frames, where no client sets one.
    fn update_frame_hover(&mut self, under: Option<&Under>) {
        let (hovered, button) = match under {
            Some(Under::TitleBar(window, button)) => (Some(window.id()), *button),
            _ => (None, None),
        };
        for window in &self.windows {
            let mut decoration = window.data().decoration.borrow_mut();
            decoration.hover = if hovered == Some(window.id()) { button } else { None };
        }
        match under {
            Some(Under::Surface(..)) => {}
            Some(Under::Resize(_, edge)) => self.cursor_status = CursorImageStatus::Named(edge.cursor()),
            Some(Under::TitleBar(..)) | None => self.cursor_status = CursorImageStatus::Named(CursorIcon::Default),
        }
    }

    pub fn on_pointer_button(&mut self, button: u32, state: ButtonState, time: u32) {
        self.note_activity();
        self.super_tap = false;
        let serial = SERIAL_COUNTER.next_serial();
        let pointer = self.pointer.clone();
        let pos = pointer.current_location();

        if !self.lock.locked && !pointer.is_grabbed() {
            if self.overview_active() {
                // Task View acts on the release, like a button.
                if state == ButtonState::Released && button == BTN_LEFT {
                    self.overview_click(pos);
                }
                return;
            }
            if self.switcher.is_some() {
                if state == ButtonState::Released && button == BTN_LEFT {
                    let on_cell = self.switcher_point(pos);
                    self.finish_switcher(!on_cell);
                }
                return;
            }
        }

        if state == ButtonState::Pressed && !pointer.is_grabbed() && !self.lock.locked {
            let under = self.under(pos);
            let logo = self.keyboard.modifier_state().logo;
            self.focus_for_click(under.as_ref());

            // Clicks on the frame, and Super+drag anywhere on a window,
            // belong to the compositor.
            let start_data = GrabStartData {
                focus: None,
                button,
                location: pos,
            };
            match under {
                Some(Under::TitleBar(window, Some(pressed))) if button == BTN_LEFT => {
                    self.pressed_button = Some((window.id(), pressed));
                    window.data().decoration.borrow_mut().pressed = Some(pressed);
                    self.schedule_redraw();
                    return;
                }
                Some(Under::TitleBar(window, None)) if button == BTN_LEFT => {
                    let double = self
                        .last_titlebar_click
                        .is_some_and(|(id, at)| id == window.id() && at.elapsed().as_millis() < 400);
                    if double {
                        self.last_titlebar_click = None;
                        self.toggle_maximize(&window);
                    } else {
                        self.last_titlebar_click = Some((window.id(), Instant::now()));
                        self.start_move(&window, start_data, serial);
                    }
                    return;
                }
                Some(Under::Resize(window, edge)) if button == BTN_LEFT => {
                    self.start_resize(&window, start_data, edge, serial);
                    return;
                }
                Some(Under::Surface(surface, _)) if logo && (button == BTN_LEFT || button == BTN_RIGHT) => {
                    if let Some(window) = self.window_for_surface(&root_surface(&surface)) {
                        if button == BTN_LEFT {
                            self.start_move(&window, start_data, serial);
                        } else if let Some(rect) = self.window_rect(&window) {
                            // Resize from the corner nearest the pointer.
                            let centre = rect.loc.to_f64() + Point::from((rect.size.w as f64 / 2.0, rect.size.h as f64 / 2.0));
                            let mut edge = if pos.x < centre.x { ResizeEdge::LEFT } else { ResizeEdge::RIGHT };
                            edge |= if pos.y < centre.y { ResizeEdge::TOP } else { ResizeEdge::BOTTOM };
                            self.start_resize(&window, start_data, edge, serial);
                        }
                        return;
                    }
                }
                _ => {}
            }
        }

        if state == ButtonState::Released && button == BTN_LEFT {
            if let Some((id, pressed)) = self.pressed_button.take() {
                if let Some(window) = self.window_by_id(id) {
                    window.data().decoration.borrow_mut().pressed = None;
                }
                self.schedule_redraw();
                // A title bar button acts on release, and only when the
                // pointer is still on it.
                let still_on_it = matches!(
                    self.under(pos),
                    Some(Under::TitleBar(window, Some(b))) if window.id() == id && b == pressed
                );
                if let (true, Some(window)) = (still_on_it, self.window_by_id(id)) {
                    match pressed {
                        Button::Close => self.close_window(&window),
                        Button::Maximize => self.toggle_maximize(&window),
                        Button::Minimize => self.minimize_window(&window),
                    }
                }
                return;
            }
        }

        pointer.button(
            self,
            &ButtonEvent {
                button,
                state,
                serial,
                time,
            },
        );
        pointer.frame(self);
    }

    /// Click-to-focus: a click on a window raises and focuses it; a click
    /// on a layer surface that accepts the keyboard focuses that.
    fn focus_for_click(&mut self, under: Option<&Under>) {
        let window = match under {
            Some(Under::TitleBar(window, _)) | Some(Under::Resize(window, _)) => Some(window.clone()),
            Some(Under::Surface(surface, _)) => {
                let root = root_surface(surface);
                match self.window_for_surface(&root) {
                    Some(window) => Some(window),
                    None => {
                        let takes_keyboard = self.space.outputs().any(|output| {
                            smithay::desktop::layer_map_for_output(output).layers().any(|layer| {
                                layer.wl_surface() == &root
                                    && layer.cached_state().keyboard_interactivity != KeyboardInteractivity::None
                            })
                        });
                        if takes_keyboard {
                            self.set_keyboard_focus(Some(root));
                        }
                        None
                    }
                }
            }
            None => None,
        };
        if let Some(window) = window {
            if self.focused_window().as_ref() != Some(&window) || self.space.elements().next_back() != Some(&window) {
                self.activate_window(&window);
            }
        }
    }

    fn on_pointer_axis<B: InputBackend>(&mut self, event: &B::PointerAxisEvent) {
        self.note_activity();
        let source = event.source();
        let mut frame = AxisFrame::new(event.time_msec()).source(source);
        for axis in [Axis::Horizontal, Axis::Vertical] {
            let v120 = event.amount_v120(axis);
            let amount = event.amount(axis).unwrap_or_else(|| v120.unwrap_or(0.0) * 15.0 / 120.0);
            if amount != 0.0 {
                frame = frame.relative_direction(axis, event.relative_direction(axis));
                frame = frame.value(axis, amount);
                if let Some(v120) = v120 {
                    frame = frame.v120(axis, v120 as i32);
                }
            } else if source == AxisSource::Finger && event.amount(axis) == Some(0.0) {
                frame = frame.stop(axis);
            }
        }
        let pointer = self.pointer.clone();
        pointer.axis(self, frame);
        pointer.frame(self);
    }

    /// A scroll step injected through the control socket (tests).
    pub fn on_scroll(&mut self, dx: f64, dy: f64, time: u32) {
        self.note_activity();
        let mut frame = AxisFrame::new(time).source(AxisSource::Wheel);
        if dx != 0.0 {
            frame = frame.value(Axis::Horizontal, dx);
        }
        if dy != 0.0 {
            frame = frame.value(Axis::Vertical, dy);
        }
        let pointer = self.pointer.clone();
        pointer.axis(self, frame);
        pointer.frame(self);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mods(logo: bool, alt: bool, ctrl: bool, shift: bool) -> ModifiersState {
        ModifiersState {
            logo,
            alt,
            ctrl,
            shift,
            ..ModifiersState::default()
        }
    }

    fn press(m: &ModifiersState, sym: u32) -> Option<Action> {
        shortcut(m, Keysym::new(sym), Keysym::new(sym), false, false)
    }

    #[test]
    fn window_management_shortcuts() {
        let logo = mods(true, false, false, false);
        assert_eq!(press(&logo, keysyms::KEY_l), Some(Action::Lock));
        assert_eq!(press(&logo, keysyms::KEY_L), Some(Action::Lock), "Caps Lock does not disable the shortcut");
        assert_eq!(press(&logo, keysyms::KEY_Up), Some(Action::Snap(Direction::Up)));
        assert_eq!(press(&logo, keysyms::KEY_Down), Some(Action::Snap(Direction::Down)));
        assert_eq!(press(&logo, keysyms::KEY_Left), Some(Action::Snap(Direction::Left)));
        assert_eq!(press(&logo, keysyms::KEY_Right), Some(Action::Snap(Direction::Right)));
        assert_eq!(press(&logo, keysyms::KEY_e), Some(Action::Files));
        assert_eq!(press(&logo, keysyms::KEY_i), Some(Action::Settings));
        assert_eq!(press(&logo, keysyms::KEY_d), Some(Action::ShowDesktop));
        assert_eq!(press(&logo, keysyms::KEY_Tab), Some(Action::Overview));
        assert_eq!(press(&logo, keysyms::KEY_a), Some(Action::QuickSettings));
        assert_eq!(press(&logo, keysyms::KEY_n), Some(Action::Notifications));
        assert_eq!(press(&logo, keysyms::KEY_r), Some(Action::Run));
        assert_eq!(press(&logo, keysyms::KEY_m), Some(Action::MinimizeAll { restore: false }));
        assert_eq!(press(&logo, keysyms::KEY_space), Some(Action::NextLayout));
        let alt = mods(false, true, false, false);
        assert_eq!(press(&alt, keysyms::KEY_Tab), Some(Action::CycleWindows { backwards: false }));
        assert_eq!(press(&alt, keysyms::KEY_F4), Some(Action::Close));
        let alt_shift = mods(false, true, false, true);
        assert_eq!(press(&alt_shift, keysyms::KEY_ISO_Left_Tab), Some(Action::CycleWindows { backwards: true }));
        let ctrl_alt = mods(false, true, true, false);
        assert_eq!(press(&ctrl_alt, keysyms::KEY_t), Some(Action::Terminal));
        let ctrl_shift = mods(false, false, true, true);
        assert_eq!(press(&ctrl_shift, keysyms::KEY_Escape), Some(Action::TaskManager));
        assert_eq!(press(&mods(false, false, false, false), keysyms::KEY_F11), Some(Action::Fullscreen));
    }

    #[test]
    fn taskbar_desktop_and_monitor_shortcuts() {
        let logo = mods(true, false, false, false);
        let logo_shift = mods(true, false, false, true);
        let logo_ctrl = mods(true, false, true, false);
        assert_eq!(press(&logo, keysyms::KEY_1), Some(Action::TaskbarApp { index: 1, shift: false }));
        assert_eq!(press(&logo, keysyms::KEY_9), Some(Action::TaskbarApp { index: 9, shift: false }));
        assert_eq!(press(&logo_shift, keysyms::KEY_2), Some(Action::TaskbarApp { index: 2, shift: true }));
        assert_eq!(press(&logo_shift, keysyms::KEY_at), Some(Action::TaskbarApp { index: 2, shift: true }));
        assert_eq!(press(&logo_shift, keysyms::KEY_s), Some(Action::Capture { region: true }));
        assert_eq!(press(&logo_shift, keysyms::KEY_S), Some(Action::Capture { region: true }));
        assert_eq!(press(&logo_shift, keysyms::KEY_m), Some(Action::MinimizeAll { restore: true }));
        assert_eq!(press(&logo_shift, keysyms::KEY_Left), Some(Action::MoveToOutput(Direction::Left)));
        assert_eq!(press(&logo_shift, keysyms::KEY_Right), Some(Action::MoveToOutput(Direction::Right)));
        assert_eq!(press(&logo_ctrl, keysyms::KEY_Left), Some(Action::SwitchDesktop { forward: false }));
        assert_eq!(press(&logo_ctrl, keysyms::KEY_Right), Some(Action::SwitchDesktop { forward: true }));
        assert_eq!(press(&logo_ctrl, keysyms::KEY_d), Some(Action::NewDesktop));
        assert_eq!(press(&logo_ctrl, keysyms::KEY_F4), Some(Action::CloseDesktop));
        assert_eq!(press(&logo_ctrl, keysyms::KEY_l), None, "Super+Ctrl+L is not the lock shortcut");
    }

    #[test]
    fn capture_and_media_keys() {
        let none = mods(false, false, false, false);
        assert_eq!(press(&none, keysyms::KEY_Print), Some(Action::Capture { region: false }));
        assert_eq!(press(&mods(false, false, false, true), keysyms::KEY_Print), Some(Action::Capture { region: true }));
        for (sym, key) in [
            (keysyms::KEY_XF86AudioRaiseVolume, MediaKey::VolumeUp),
            (keysyms::KEY_XF86AudioLowerVolume, MediaKey::VolumeDown),
            (keysyms::KEY_XF86AudioMute, MediaKey::VolumeMute),
            (keysyms::KEY_XF86AudioMicMute, MediaKey::MicMute),
            (keysyms::KEY_XF86MonBrightnessUp, MediaKey::BrightnessUp),
            (keysyms::KEY_XF86MonBrightnessDown, MediaKey::BrightnessDown),
            (keysyms::KEY_XF86AudioPlay, MediaKey::Play),
            (keysyms::KEY_XF86AudioNext, MediaKey::Next),
            (keysyms::KEY_XF86AudioPrev, MediaKey::Previous),
        ] {
            assert_eq!(press(&none, sym), Some(Action::Media(key)));
            // The lock screen keeps volume and brightness keys.
            assert_eq!(shortcut(&none, Keysym::new(sym), Keysym::new(sym), true, false), Some(Action::Media(key)));
            assert!(!key.name().is_empty());
        }
        assert_eq!(shortcut(&none, Keysym::new(keysyms::KEY_Print), Keysym::new(keysyms::KEY_Print), true, false), None);
    }

    #[test]
    fn plain_typing_is_never_a_shortcut() {
        let none = mods(false, false, false, false);
        for sym in [keysyms::KEY_l, keysyms::KEY_Tab, keysyms::KEY_F4, keysyms::KEY_1, keysyms::KEY_Up, keysyms::KEY_t, keysyms::KEY_Escape, keysyms::KEY_space] {
            assert_eq!(press(&none, sym), None);
        }
        let ctrl = mods(false, false, true, false);
        assert_eq!(press(&ctrl, keysyms::KEY_t), None);
        assert_eq!(press(&mods(false, true, false, false), keysyms::KEY_a), None);
    }

    #[test]
    fn a_locked_session_only_switches_vt() {
        let logo = mods(true, false, false, false);
        let alt = mods(false, true, false, false);
        let locked = |m: &ModifiersState, sym: u32| shortcut(m, Keysym::new(sym), Keysym::new(sym), true, false);
        assert_eq!(locked(&logo, keysyms::KEY_l), None);
        assert_eq!(locked(&alt, keysyms::KEY_Tab), None);
        assert_eq!(locked(&alt, keysyms::KEY_F4), None);
        assert_eq!(locked(&logo, keysyms::KEY_1), None);
        assert_eq!(locked(&logo, keysyms::KEY_d), None);
        assert_eq!(locked(&logo, keysyms::KEY_Tab), None);
        let ctrl_alt = mods(false, true, true, false);
        assert_eq!(
            shortcut(&ctrl_alt, Keysym::new(keysyms::KEY_F2), Keysym::new(keysyms::KEY_XF86Switch_VT_2), true, false),
            Some(Action::SwitchVt(2))
        );
    }

    #[test]
    fn an_inhibitor_keeps_lock_and_vt_switching() {
        let logo = mods(true, false, false, false);
        let alt = mods(false, true, false, false);
        let inhibited = |m: &ModifiersState, sym: u32| shortcut(m, Keysym::new(sym), Keysym::new(sym), false, true);
        assert_eq!(inhibited(&logo, keysyms::KEY_l), Some(Action::Lock));
        assert_eq!(inhibited(&alt, keysyms::KEY_Tab), None);
        assert_eq!(inhibited(&logo, keysyms::KEY_Up), None);
    }
}
