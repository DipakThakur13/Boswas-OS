//! Input: keyboard shortcuts, pointer focus, clicks on window frames.
//!
//! Every device event ends in one of the `on_*` methods, which take plain
//! values; the control socket's test-input commands call the same methods,
//! so tests exercise exactly the code real devices do.

use std::time::Instant;

use smithay::{
    backend::input::{
        AbsolutePositionEvent, Axis, AxisSource, ButtonState, Device, DeviceCapability, Event, GestureBeginEvent,
        GestureEndEvent, GesturePinchUpdateEvent as _, GestureSwipeUpdateEvent as _, InputBackend, InputEvent,
        KeyState, KeyboardKeyEvent, PointerAxisEvent, PointerButtonEvent, PointerMotionEvent, ProximityState,
        TabletToolButtonEvent, TabletToolEvent, TabletToolProximityEvent, TabletToolTipEvent,
        TabletToolTipState, TouchEvent, TouchSlot,
    },
    input::{
        touch::{DownEvent, UpEvent},
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
        tablet_manager::{TabletDescriptor, TabletSeatTrait},
        pointer_constraints::{with_pointer_constraint, PointerConstraint},
        shell::wlr_layer::KeyboardInteractivity,
    },
};

use crate::{
    shell::{
        decoration::Button,
        element::Tile,
        grabs::ResizeEdge,
        overview::OverviewKey,
        windowmenu::{AdjustKey, MenuKey},
        wm::snap_step,
    },
    state::{root_surface, Boswas, Under},
};

/// The magnifier's largest zoom (1600 %).
pub const MAX_ZOOM: f64 = 16.0;

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
    /// Super+W: the widgets board.
    Widgets,
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
    /// Alt+Space: the focused window's menu.
    WindowMenu,
    /// A key for the window menu while it is open.
    MenuKey(MenuKey),
    /// A key while a window is moved or resized from the keyboard.
    Adjust(AdjustKey),
    /// Super+Plus, Super+Minus, Super+Esc: the magnifier.
    Magnify(Zoom),
    /// Super+Ctrl+C, where Settings allows it: the colour filter on or off.
    ToggleColorFilter,
    /// Super+V: the clipboard history (the shell's).
    Clipboard,
    /// Super+. and Super+;: the emoji and symbols panel (the shell's).
    Emoji,
    /// Super+S: Start with its search box (the shell's).
    Search,
    /// Super+X: the quick-link menu at the Start button (the shell's).
    QuickLinks,
    /// Super+Alt+D: the calendar (the shell's notification centre).
    Calendar,
    /// Super+T (the applications) and Super+B (`tray`: the notification
    /// area): the keyboard on the taskbar (the shell's).
    FocusTaskbar { tray: bool },
    /// Super+Home: minimise every window but the active one; again,
    /// restore them.
    MinimizeOthers,
    /// Super+Shift+Up (`up`): the full height of the screen; Super+Shift+
    /// Down: back, or minimise.
    Stretch { up: bool },
    /// Super+U: Settings > Accessibility.
    AccessibilitySettings,
    /// Super+P: how the monitors are used (the shell's projection flyout).
    Project,
    /// Ctrl+Alt+Del: the security options (the shell's).
    SecurityOptions,
}

/// What a magnifier key does.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Zoom {
    In,
    Out,
    Off,
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

/// The key bindings of the window menu.
fn menu_key(sym: Keysym) -> Option<MenuKey> {
    Some(match sym.raw() {
        keysyms::KEY_Up => MenuKey::Step(-1),
        keysyms::KEY_Down => MenuKey::Step(1),
        keysyms::KEY_Return | keysyms::KEY_KP_Enter | keysyms::KEY_space => MenuKey::Accept,
        keysyms::KEY_Escape => MenuKey::Cancel,
        _ => return None,
    })
}

/// The keys that move or resize a window after Move or Size was chosen from
/// its menu: arrows (Ctrl for single pixels), Enter or Escape to finish.
fn adjust_key(sym: Keysym, ctrl: bool) -> Option<AdjustKey> {
    let direction = match sym.raw() {
        keysyms::KEY_Left => Direction::Left,
        keysyms::KEY_Right => Direction::Right,
        keysyms::KEY_Up => Direction::Up,
        keysyms::KEY_Down => Direction::Down,
        keysyms::KEY_Return | keysyms::KEY_KP_Enter | keysyms::KEY_Escape => return Some(AdjustKey::Finish),
        _ => return None,
    };
    Some(AdjustKey::Nudge { direction, fine: ctrl })
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
    // Ctrl+Alt+Del, like Super+L, reaches the desktop even while a program
    // (a remote desktop, a virtual machine) has taken the other shortcuts.
    if mods.ctrl && mods.alt && !mods.logo && !mods.shift && matches!(lower, keysyms::KEY_Delete | keysyms::KEY_KP_Delete) {
        return Some(Action::SecurityOptions);
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
            keysyms::KEY_space if !mods.shift => return Some(Action::WindowMenu),
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
    if mods.logo && mods.alt && !mods.ctrl && !mods.shift && lower == keysyms::KEY_d {
        return Some(Action::Calendar);
    }
    if mods.logo && mods.ctrl && !mods.alt && !mods.shift {
        return match lower {
            keysyms::KEY_Left => Some(Action::SwitchDesktop { forward: false }),
            keysyms::KEY_Right => Some(Action::SwitchDesktop { forward: true }),
            keysyms::KEY_d => Some(Action::NewDesktop),
            keysyms::KEY_F4 => Some(Action::CloseDesktop),
            keysyms::KEY_c => Some(Action::ToggleColorFilter),
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
        // The magnifier: the key with + on it, with or without Shift.
        match lower {
            keysyms::KEY_plus | keysyms::KEY_equal | keysyms::KEY_KP_Add => return Some(Action::Magnify(Zoom::In)),
            keysyms::KEY_minus | keysyms::KEY_KP_Subtract if !mods.shift => return Some(Action::Magnify(Zoom::Out)),
            keysyms::KEY_Escape if !mods.shift => return Some(Action::Magnify(Zoom::Off)),
            _ => {}
        }
        if mods.shift {
            return match lower {
                keysyms::KEY_s => Some(Action::Capture { region: true }),
                keysyms::KEY_m => Some(Action::MinimizeAll { restore: true }),
                keysyms::KEY_Left => Some(Action::MoveToOutput(Direction::Left)),
                keysyms::KEY_Right => Some(Action::MoveToOutput(Direction::Right)),
                keysyms::KEY_Up => Some(Action::Stretch { up: true }),
                keysyms::KEY_Down => Some(Action::Stretch { up: false }),
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
            keysyms::KEY_w => Some(Action::Widgets),
            keysyms::KEY_d => Some(Action::ShowDesktop),
            keysyms::KEY_m => Some(Action::MinimizeAll { restore: false }),
            keysyms::KEY_e => Some(Action::Files),
            keysyms::KEY_i => Some(Action::Settings),
            keysyms::KEY_r => Some(Action::Run),
            keysyms::KEY_space => Some(Action::NextLayout),
            keysyms::KEY_F11 => Some(Action::Fullscreen),
            keysyms::KEY_v => Some(Action::Clipboard),
            keysyms::KEY_period | keysyms::KEY_semicolon => Some(Action::Emoji),
            keysyms::KEY_s => Some(Action::Search),
            keysyms::KEY_x => Some(Action::QuickLinks),
            keysyms::KEY_t => Some(Action::FocusTaskbar { tray: false }),
            keysyms::KEY_b => Some(Action::FocusTaskbar { tray: true }),
            keysyms::KEY_Home | keysyms::KEY_KP_Home => Some(Action::MinimizeOthers),
            keysyms::KEY_u => Some(Action::AccessibilitySettings),
            keysyms::KEY_p => Some(Action::Project),
            _ => None,
        };
    }
    None
}

/// The arrangement at the opposite side of a monitor: a window that leaves
/// one monitor through its left edge arrives at the right of the next.
pub fn facing_tile(tile: Tile) -> Option<Tile> {
    Some(match tile {
        Tile::Left => Tile::Right,
        Tile::Right => Tile::Left,
        Tile::TopLeft => Tile::TopRight,
        Tile::TopRight => Tile::TopLeft,
        Tile::BottomLeft => Tile::BottomRight,
        Tile::BottomRight => Tile::BottomLeft,
        Tile::Floating | Tile::Maximized | Tile::Fullscreen => return None,
    })
}

/// How far three fingers must travel for a swipe to count, in the
/// touchpad's (unaccelerated, roughly millimetre-like) units.
const SWIPE_THRESHOLD: f64 = 80.0;

/// What a finished three- or four-finger swipe does: sideways goes to the
/// next or previous desktop (the content follows the fingers), up opens
/// Task View and down shows the desktop.
pub fn swipe_action(distance: Point<f64, Logical>) -> Option<Action> {
    let (dx, dy) = (distance.x, distance.y);
    if dx.abs().max(dy.abs()) < SWIPE_THRESHOLD {
        return None;
    }
    Some(if dx.abs() > dy.abs() {
        // Fingers to the left pull the next desktop in from the right.
        Action::SwitchDesktop { forward: dx < 0.0 }
    } else if dy < 0.0 {
        Action::Overview
    } else {
        Action::ShowDesktop
    })
}

fn is_logo(sym: Keysym) -> bool {
    matches!(sym.raw(), keysyms::KEY_Super_L | keysyms::KEY_Super_R)
}

/// Shift, Ctrl, Alt, Super and the lock keys: keys that only change what
/// other keys mean.
fn is_modifier(sym: Keysym) -> bool {
    is_logo(sym)
        || is_alt(sym)
        || matches!(
            sym.raw(),
            keysyms::KEY_Shift_L
                | keysyms::KEY_Shift_R
                | keysyms::KEY_Control_L
                | keysyms::KEY_Control_R
                | keysyms::KEY_Caps_Lock
                | keysyms::KEY_Num_Lock
                | keysyms::KEY_ISO_Level3_Shift
        )
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
                self.note_activity();
                // Three and four fingers belong to the desktop: they switch
                // desktops and open Task View. Applications never see them.
                let fingers = event.fingers();
                if self.config.touchpad.gestures && fingers >= 3 && !self.lock.locked {
                    self.swipe = Some((fingers, Point::from((0.0, 0.0))));
                    return;
                }
                let pointer = self.pointer.clone();
                pointer.gesture_swipe_begin(
                    self,
                    &GestureSwipeBeginEvent {
                        serial: SERIAL_COUNTER.next_serial(),
                        time: event.time_msec(),
                        fingers,
                    },
                );
            }
            InputEvent::GestureSwipeUpdate { event } => {
                if let Some((_, distance)) = &mut self.swipe {
                    *distance += event.delta();
                    return;
                }
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
                if let Some((_, distance)) = self.swipe.take() {
                    if !event.cancelled() {
                        if let Some(action) = swipe_action(distance) {
                            self.run_action(action);
                        }
                    }
                    return;
                }
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
            InputEvent::TouchDown { event } => {
                if let Some(pos) = self.touch_position(&event) {
                    self.on_touch_down(event.slot(), pos, event.time_msec());
                }
            }
            InputEvent::TouchMotion { event } => {
                if let Some(pos) = self.touch_position(&event) {
                    self.on_touch_motion(event.slot(), pos, event.time_msec());
                }
            }
            InputEvent::TouchUp { event } => self.on_touch_up(event.slot(), event.time_msec()),
            InputEvent::TouchFrame { .. } => {
                if let Some(touch) = self.seat.get_touch() {
                    touch.frame(self);
                }
            }
            InputEvent::TouchCancel { .. } => {
                self.touch_pointer = None;
                if let Some(touch) = self.seat.get_touch() {
                    touch.cancel(self);
                }
            }
            InputEvent::TabletToolProximity { event } => self.on_tablet_proximity::<B>(&event),
            InputEvent::TabletToolAxis { event } => self.on_tablet_axis::<B>(&event),
            InputEvent::TabletToolTip { event } => self.on_tablet_tip::<B>(&event),
            InputEvent::TabletToolButton { event } => {
                if let Some(tool) = self.seat.tablet_seat().get_tool(&event.tool()) {
                    tool.button(event.button(), event.button_state(), SERIAL_COUNTER.next_serial(), event.time_msec());
                }
            }
            InputEvent::DeviceAdded { device } => {
                if device.has_capability(DeviceCapability::TabletTool) {
                    let dh = self.display_handle.clone();
                    self.seat.tablet_seat().add_tablet::<Self>(&dh, &TabletDescriptor::from(&device));
                }
                if device.has_capability(DeviceCapability::Touch) && self.seat.get_touch().is_none() {
                    self.seat.add_touch();
                }
            }
            InputEvent::DeviceRemoved { device } => {
                if device.has_capability(DeviceCapability::TabletTool) {
                    let tablets = self.seat.tablet_seat();
                    tablets.remove_tablet(&TabletDescriptor::from(&device));
                    if tablets.count_tablets() == 0 {
                        tablets.clear_tools();
                    }
                }
            }
            _ => {}
        }
    }

    fn note_activity(&mut self) {
        self.last_input = Instant::now();
        let seat = self.seat.clone();
        self.idle_notifier_state.notify_activity(&seat);
        self.wake_outputs();
    }

    // ---------------------------------------------------------------------
    // Touch screens
    // ---------------------------------------------------------------------

    /// Where a touch or pen event is on the desktop: touch screens and pen
    /// displays are mapped to the first (built-in) output.
    fn touch_position<E: AbsolutePositionEvent<impl InputBackend>>(&self, event: &E) -> Option<Point<f64, Logical>> {
        let output = self.space.outputs().next()?;
        let geo = self.space.output_geometry(output)?;
        Some(event.position_transformed(geo.size) + geo.loc.to_f64())
    }

    /// A finger went down. On an application it is a touch point for that
    /// application; on anything the compositor draws itself (a title bar, a
    /// resize border, Task View) the finger acts as the pointer.
    pub fn on_touch_down(&mut self, slot: TouchSlot, pos: Point<f64, Logical>, time: u32) {
        self.note_activity();
        let Some(touch) = self.seat.get_touch() else {
            return;
        };
        let serial = SERIAL_COUNTER.next_serial();
        let ui = !self.lock.locked && (self.overview_active() || self.switcher.is_some() || self.on_window_menu(pos));
        let under = if ui { None } else { self.under(pos) };
        match under {
            Some(Under::Surface(surface, origin)) => {
                let target = Under::Surface(surface.clone(), origin);
                if !self.lock.locked {
                    self.focus_for_click(Some(&target));
                }
                touch.down(
                    self,
                    Some((surface, origin)),
                    &DownEvent {
                        slot,
                        location: pos,
                        serial,
                        time,
                    },
                );
            }
            _ if self.touch_pointer.is_none() => {
                self.touch_pointer = Some(slot);
                self.on_pointer_position(pos, time);
                self.on_pointer_button(BTN_LEFT, ButtonState::Pressed, time);
            }
            _ => {}
        }
    }

    pub fn on_touch_motion(&mut self, slot: TouchSlot, pos: Point<f64, Logical>, time: u32) {
        self.note_activity();
        if self.touch_pointer == Some(slot) {
            self.on_pointer_position(pos, time);
            return;
        }
        let Some(touch) = self.seat.get_touch() else {
            return;
        };
        let focus = self.under(pos).and_then(|under| under.focus());
        touch.motion(
            self,
            focus,
            &smithay::input::touch::MotionEvent {
                slot,
                location: pos,
                time,
            },
        );
    }

    pub fn on_touch_up(&mut self, slot: TouchSlot, time: u32) {
        self.note_activity();
        if self.touch_pointer == Some(slot) {
            self.touch_pointer = None;
            self.on_pointer_button(BTN_LEFT, ButtonState::Released, time);
            return;
        }
        if let Some(touch) = self.seat.get_touch() {
            touch.up(
                self,
                &UpEvent {
                    slot,
                    serial: SERIAL_COUNTER.next_serial(),
                    time,
                },
            );
        }
    }

    // ---------------------------------------------------------------------
    // Drawing tablets
    // ---------------------------------------------------------------------

    /// The pen moved the pointer; tells the tool where it is and what it
    /// measures (pressure, tilt and so on).
    fn on_tablet_axis<B: InputBackend>(&mut self, event: &B::TabletToolAxisEvent) {
        self.note_activity();
        let Some(pos) = self.touch_position(event) else {
            return;
        };
        self.on_pointer_position(pos, event.time_msec());
        let tablets = self.seat.tablet_seat();
        let (Some(tablet), Some(tool)) = (
            tablets.get_tablet(&TabletDescriptor::from(&event.device())),
            tablets.get_tool(&event.tool()),
        ) else {
            return;
        };
        if event.pressure_has_changed() {
            tool.pressure(event.pressure());
        }
        if event.distance_has_changed() {
            tool.distance(event.distance());
        }
        if event.tilt_has_changed() {
            tool.tilt(event.tilt());
        }
        if event.slider_has_changed() {
            tool.slider_position(event.slider_position());
        }
        if event.rotation_has_changed() {
            tool.rotation(event.rotation());
        }
        if event.wheel_has_changed() {
            tool.wheel(event.wheel_delta(), event.wheel_delta_discrete());
        }
        let focus = self.under(pos).and_then(|under| under.focus());
        tool.motion(pos, focus, &tablet, SERIAL_COUNTER.next_serial(), event.time_msec());
    }

    fn on_tablet_proximity<B: InputBackend>(&mut self, event: &B::TabletToolProximityEvent) {
        self.note_activity();
        let Some(pos) = self.touch_position(event) else {
            return;
        };
        let dh = self.display_handle.clone();
        let tablets = self.seat.tablet_seat();
        tablets.add_tool::<Self>(self, &dh, &event.tool());
        self.on_pointer_position(pos, event.time_msec());
        let (Some(tablet), Some(tool)) = (
            tablets.get_tablet(&TabletDescriptor::from(&event.device())),
            tablets.get_tool(&event.tool()),
        ) else {
            return;
        };
        match event.state() {
            ProximityState::In => {
                if let Some(focus) = self.under(pos).and_then(|under| under.focus()) {
                    tool.proximity_in(pos, focus, &tablet, SERIAL_COUNTER.next_serial(), event.time_msec());
                }
            }
            ProximityState::Out => tool.proximity_out(event.time_msec()),
        }
    }

    /// The pen's tip touched or left the surface: applications that speak
    /// the tablet protocol get the tip; the frame and Task View treat it
    /// as the left mouse button.
    fn on_tablet_tip<B: InputBackend>(&mut self, event: &B::TabletToolTipEvent) {
        self.note_activity();
        let pos = self.pointer.current_location();
        let on_surface = matches!(self.under(pos), Some(Under::Surface(..)))
            && !self.overview_active()
            && self.switcher.is_none();
        let down = event.tip_state() == TabletToolTipState::Down;
        if !on_surface || self.pressed_button.is_some() || self.pointer.is_grabbed() {
            let state = if down { ButtonState::Pressed } else { ButtonState::Released };
            self.on_pointer_button(BTN_LEFT, state, event.time_msec());
            return;
        }
        if let Some(tool) = self.seat.tablet_seat().get_tool(&event.tool()) {
            if down {
                let under = self.under(pos);
                if !self.lock.locked {
                    self.focus_for_click(under.as_ref());
                }
                tool.tip_down(SERIAL_COUNTER.next_serial(), event.time_msec());
            } else {
                tool.tip_up(event.time_msec());
            }
        }
    }

    // ---------------------------------------------------------------------
    // Keyboard
    // ---------------------------------------------------------------------

    /// A key was pressed or released on a device. `keycode` is the XKB
    /// keycode (the evdev code plus 8). Sticky and filter keys decide which
    /// key events count (`keyaccess`).
    pub fn on_key(&mut self, keycode: u32, state: KeyState, time: u32) {
        self.note_activity();
        let outcome = self.key_access.key(&self.config.keyboard, keycode, state == KeyState::Pressed, Instant::now());
        self.key_access_outcome(outcome, time);
    }

    /// Delivers what sticky and filter keys let through, and comes back
    /// when a held slow key is due.
    fn key_access_outcome(&mut self, outcome: crate::keyaccess::Outcome, time: u32) {
        for (code, pressed) in outcome.keys {
            self.deliver_key(code, if pressed { KeyState::Pressed } else { KeyState::Released }, time);
        }
        if let Some(due) = outcome.wake {
            let timer = smithay::reexports::calloop::timer::Timer::from_deadline(due);
            let inserted = self.loop_handle.insert_source(timer, move |_, _, state| {
                let outcome = state.key_access.due(&state.config.keyboard, Instant::now());
                let time = state.start_time.elapsed().as_millis() as u32;
                state.key_access_outcome(outcome, time);
                smithay::reexports::calloop::timer::TimeoutAction::Drop
            });
            if inserted.is_err() {
                tracing::warn!("slow keys: no timer; the key is let through at once");
                let outcome = self.key_access.due(&self.config.keyboard, due);
                self.key_access_outcome(outcome, time);
            }
        }
    }

    /// Lets go of the modifiers sticky keys holds down (it was turned off).
    pub fn release_sticky_keys(&mut self) {
        let time = self.start_time.elapsed().as_millis() as u32;
        for (code, _) in self.key_access.release_all() {
            self.deliver_key(code, KeyState::Released, time);
        }
    }

    /// A key event that counts: shortcuts, then the focused client.
    fn deliver_key(&mut self, keycode: u32, state: KeyState, time: u32) {
        let serial = SERIAL_COUNTER.next_serial();
        let keyboard = self.keyboard.clone();
        let locked = self.lock.locked;
        let inhibited = self
            .keyboard_surface()
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
            // The window menu and a keyboard move or resize take their
            // keys; any other key ends them and is handled as usual.
            if data.window_menu.is_some() && !locked {
                if let Some(key) = menu_key(modified).filter(|_| !mods.logo && !mods.alt && !mods.ctrl) {
                    data.suppressed_keys.insert(keycode);
                    return FilterResult::Intercept(Action::MenuKey(key));
                }
                if !is_modifier(modified) {
                    data.close_window_menu();
                }
            }
            if data.key_adjust.is_some() && !locked {
                if let Some(key) = adjust_key(modified, mods.ctrl).filter(|_| !mods.logo && !mods.alt) {
                    data.suppressed_keys.insert(keycode);
                    return FilterResult::Intercept(Action::Adjust(key));
                }
                if !is_modifier(modified) {
                    data.finish_key_adjust();
                }
            }
            if data.overview_active() && !locked {
                if let Some(key) = overview_key(modified, mods.shift).filter(|_| !mods.logo && !mods.alt && !mods.ctrl) {
                    data.suppressed_keys.insert(keycode);
                    return FilterResult::Intercept(Action::TaskView(key));
                }
            }
            // Super+Ctrl+C only where Settings turned the shortcut on.
            let allowed = |action: &Action| *action != Action::ToggleColorFilter || data.config.color_filter.shortcut;
            match shortcut(mods, sym, modified, locked, inhibited).filter(allowed) {
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
            Action::WindowMenu => self.open_focused_window_menu(),
            Action::MenuKey(key) => self.window_menu_key(key),
            Action::Adjust(key) => self.key_adjust_key(key),
            Action::SwitchVt(vt) => self.backend.switch_vt(vt),
            Action::Lock => self.lock_session(),
            Action::Launcher => event(self, "launcher"),
            Action::QuickSettings => event(self, "quick-settings"),
            Action::Notifications => event(self, "notifications"),
            Action::Widgets => event(self, "widgets"),
            Action::Run => event(self, "run"),
            Action::Clipboard => event(self, "clipboard"),
            Action::Emoji => event(self, "emoji"),
            Action::Search => event(self, "search"),
            Action::QuickLinks => event(self, "quick-links"),
            Action::Calendar => event(self, "calendar"),
            Action::SecurityOptions => event(self, "security"),
            Action::FocusTaskbar { tray } => {
                self.ipc_event("focus-taskbar", serde_json::json!({ "area": if tray { "tray" } else { "apps" } }));
            }
            Action::Project => {
                let projection = self.projection_json();
                self.ipc_event("projection", projection);
            }
            Action::MinimizeOthers => self.minimize_others(),
            Action::Stretch { up } => self.stretch_key(up),
            Action::AccessibilitySettings => self.open_accessibility_settings(),
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
                    let current = window.data().tile.get();
                    if !self.config.multitasking.snap_windows {
                        // Without snapping Super+Up and Super+Down still
                        // maximize, restore and minimize; the sides do nothing.
                        match direction {
                            Direction::Up if current != Tile::Maximized => self.set_tile(&window, Tile::Maximized),
                            Direction::Down if current == Tile::Floating => self.minimize_window(&window),
                            Direction::Down => self.set_tile(&window, Tile::Floating),
                            _ => {}
                        }
                        return;
                    }
                    match snap_step(current, direction) {
                        // Already at that side: on to the next monitor, at
                        // the side facing the one it left.
                        Some(tile) if tile == current && matches!(direction, Direction::Left | Direction::Right) => {
                            let before = self.window_rect(&window).and_then(|rect| self.output_for_rect(rect));
                            self.move_to_next_output(&window, direction);
                            let after = self.window_rect(&window).and_then(|rect| self.output_for_rect(rect));
                            if before != after {
                                if let Some(tile) = facing_tile(current) {
                                    self.set_tile(&window, tile);
                                }
                            }
                        }
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
            Action::Magnify(zoom) => self.magnify(zoom),
            Action::ToggleColorFilter => {
                self.color_filter_on = !self.color_filter_on;
                self.schedule_redraw();
            }
        }
    }

    /// The magnifier keys: Super+Plus starts it at the zoom of Settings or
    /// enlarges by its step, Super+Minus reduces by the step (it stays on at
    /// 100 %), Super+Esc ends it.
    pub fn magnify(&mut self, zoom: Zoom) {
        let step = f64::from(self.config.magnifier.step) / 100.0;
        let start = (f64::from(self.config.magnifier.zoom) / 100.0).max(1.0 + step);
        let current = self.magnifier_zoom;
        self.magnifier_zoom = match zoom {
            Zoom::In if current <= 1.0 && !self.magnifier_running => start,
            Zoom::In => (current + step).min(MAX_ZOOM),
            Zoom::Out => (current - step).max(1.0),
            Zoom::Off => 1.0,
        };
        self.magnifier_running = zoom != Zoom::Off;
        self.schedule_redraw();
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
        // The snap layouts flyout takes the pointer while it is over it.
        let on_maximize = matches!(&under, Some(Under::TitleBar(window, Some(Button::Maximize)))
            if self.snap_flyout.as_ref().is_some_and(|flyout| flyout.window == window.id()));
        let on_flyout = !pointer.is_grabbed() && (self.window_menu_motion(pos) || self.snap_flyout_motion(pos, on_maximize));
        let under = if on_flyout { None } else { under };
        if !pointer.is_grabbed() {
            self.update_frame_hover(under.as_ref());
            if on_flyout {
                self.maximize_hover = None;
            }
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
        // Resting on a maximize button opens the snap layouts (unless
        // Multitasking turned them off).
        let layouts = self.config.multitasking.snap_windows && self.config.multitasking.snap_layouts_on_maximize;
        self.maximize_hover = match (hovered, button) {
            (Some(id), Some(Button::Maximize)) if layouts => match self.maximize_hover {
                Some((current, since)) if current == id => Some((current, since)),
                _ => Some((id, Instant::now())),
            },
            _ => None,
        };
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
            if self.on_window_menu(pos) {
                if state == ButtonState::Released && button == BTN_LEFT {
                    self.window_menu_click(pos);
                }
                return;
            }
            // A click anywhere else closes the window menu.
            if state == ButtonState::Pressed {
                self.close_window_menu();
            }
            if self.snap_flyout.as_ref().is_some_and(|flyout| flyout.rect.to_f64().contains(pos)) {
                if state == ButtonState::Released && button == BTN_LEFT {
                    self.snap_flyout_click(pos);
                }
                return;
            }
            // A click anywhere else closes the snap layouts.
            if state == ButtonState::Pressed && self.snap_flyout.take().is_some() {
                self.schedule_redraw();
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
                Some(Under::TitleBar(window, _)) if button == BTN_RIGHT => {
                    self.open_window_menu(&window, pos.to_i32_round());
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
    fn the_window_menu_keys() {
        let alt = mods(false, true, false, false);
        assert_eq!(press(&alt, keysyms::KEY_space), Some(Action::WindowMenu));
        assert_eq!(press(&mods(false, true, false, true), keysyms::KEY_space), None);
        assert_eq!(press(&mods(false, false, false, false), keysyms::KEY_space), None);
        // A remote desktop window that took the shortcuts keeps Alt+Space.
        assert_eq!(shortcut(&alt, Keysym::new(keysyms::KEY_space), Keysym::new(keysyms::KEY_space), false, true), None);

        assert_eq!(menu_key(Keysym::new(keysyms::KEY_Down)), Some(MenuKey::Step(1)));
        assert_eq!(menu_key(Keysym::new(keysyms::KEY_Up)), Some(MenuKey::Step(-1)));
        assert_eq!(menu_key(Keysym::new(keysyms::KEY_Return)), Some(MenuKey::Accept));
        assert_eq!(menu_key(Keysym::new(keysyms::KEY_Escape)), Some(MenuKey::Cancel));
        assert_eq!(menu_key(Keysym::new(keysyms::KEY_a)), None);

        assert_eq!(
            adjust_key(Keysym::new(keysyms::KEY_Left), false),
            Some(AdjustKey::Nudge { direction: Direction::Left, fine: false })
        );
        assert_eq!(
            adjust_key(Keysym::new(keysyms::KEY_Down), true),
            Some(AdjustKey::Nudge { direction: Direction::Down, fine: true })
        );
        assert_eq!(adjust_key(Keysym::new(keysyms::KEY_Escape), false), Some(AdjustKey::Finish));
        assert_eq!(adjust_key(Keysym::new(keysyms::KEY_Return), false), Some(AdjustKey::Finish));
        assert_eq!(adjust_key(Keysym::new(keysyms::KEY_x), false), None);
        assert!(is_modifier(Keysym::new(keysyms::KEY_Alt_L)));
        assert!(is_modifier(Keysym::new(keysyms::KEY_Shift_R)));
        assert!(!is_modifier(Keysym::new(keysyms::KEY_space)));
    }

    #[test]
    fn a_snapped_window_arrives_at_the_facing_side_of_the_next_monitor() {
        assert_eq!(facing_tile(Tile::Left), Some(Tile::Right));
        assert_eq!(facing_tile(Tile::Right), Some(Tile::Left));
        assert_eq!(facing_tile(Tile::TopLeft), Some(Tile::TopRight));
        assert_eq!(facing_tile(Tile::BottomRight), Some(Tile::BottomLeft));
        assert_eq!(facing_tile(Tile::Maximized), None);
        assert_eq!(facing_tile(Tile::Floating), None);
        // Every snapped arrangement comes back after two crossings.
        for tile in [Tile::Left, Tile::Right, Tile::TopLeft, Tile::TopRight, Tile::BottomLeft, Tile::BottomRight] {
            assert_eq!(facing_tile(tile).and_then(facing_tile), Some(tile));
        }
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
        assert_eq!(press(&logo, keysyms::KEY_w), Some(Action::Widgets));
        assert_eq!(press(&logo, keysyms::KEY_r), Some(Action::Run));
        assert_eq!(press(&logo, keysyms::KEY_m), Some(Action::MinimizeAll { restore: false }));
        assert_eq!(press(&logo, keysyms::KEY_space), Some(Action::NextLayout));
        assert_eq!(press(&logo, keysyms::KEY_v), Some(Action::Clipboard));
        assert_eq!(press(&logo, keysyms::KEY_V), Some(Action::Clipboard), "with Caps Lock on");
        assert_eq!(press(&logo, keysyms::KEY_period), Some(Action::Emoji));
        assert_eq!(press(&logo, keysyms::KEY_semicolon), Some(Action::Emoji));
        assert_eq!(press(&mods(false, false, true, false), keysyms::KEY_v), None, "Ctrl+V is the application's");
        assert_eq!(press(&mods(true, false, false, true), keysyms::KEY_v), None);
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
    fn accessibility_shortcuts_take_free_keys() {
        let logo = mods(true, false, false, false);
        assert_eq!(press(&logo, keysyms::KEY_equal), Some(Action::Magnify(Zoom::In)));
        assert_eq!(press(&logo, keysyms::KEY_KP_Add), Some(Action::Magnify(Zoom::In)));
        assert_eq!(press(&mods(true, false, false, true), keysyms::KEY_equal), Some(Action::Magnify(Zoom::In)), "Super+Shift+= is Super+Plus");
        assert_eq!(press(&logo, keysyms::KEY_minus), Some(Action::Magnify(Zoom::Out)));
        assert_eq!(press(&logo, keysyms::KEY_KP_Subtract), Some(Action::Magnify(Zoom::Out)));
        assert_eq!(press(&logo, keysyms::KEY_Escape), Some(Action::Magnify(Zoom::Off)));
        let logo_ctrl = mods(true, false, true, false);
        assert_eq!(press(&logo_ctrl, keysyms::KEY_c), Some(Action::ToggleColorFilter));
        // Nothing else lost its key.
        assert_eq!(press(&logo_ctrl, keysyms::KEY_d), Some(Action::NewDesktop));
        assert_eq!(press(&mods(false, false, true, false), keysyms::KEY_c), None, "Ctrl+C is the application's");
        assert_eq!(press(&mods(false, false, false, false), keysyms::KEY_Escape), None);
        assert_eq!(press(&mods(true, false, false, true), keysyms::KEY_s), Some(Action::Capture { region: true }));
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
    fn the_windows_shortcuts_of_the_shell_round() {
        let logo = mods(true, false, false, false);
        let logo_shift = mods(true, false, false, true);
        let logo_alt = mods(true, true, false, false);
        let ctrl_alt = mods(false, true, true, false);
        assert_eq!(press(&logo, keysyms::KEY_s), Some(Action::Search));
        assert_eq!(press(&logo, keysyms::KEY_S), Some(Action::Search), "with Caps Lock on");
        assert_eq!(press(&logo, keysyms::KEY_x), Some(Action::QuickLinks));
        assert_eq!(press(&logo_alt, keysyms::KEY_d), Some(Action::Calendar));
        assert_eq!(press(&logo, keysyms::KEY_t), Some(Action::FocusTaskbar { tray: false }));
        assert_eq!(press(&logo, keysyms::KEY_b), Some(Action::FocusTaskbar { tray: true }));
        assert_eq!(press(&logo, keysyms::KEY_Home), Some(Action::MinimizeOthers));
        assert_eq!(press(&logo_shift, keysyms::KEY_Up), Some(Action::Stretch { up: true }));
        assert_eq!(press(&logo_shift, keysyms::KEY_Down), Some(Action::Stretch { up: false }));
        assert_eq!(press(&logo, keysyms::KEY_u), Some(Action::AccessibilitySettings));
        assert_eq!(press(&logo, keysyms::KEY_p), Some(Action::Project));
        assert_eq!(press(&ctrl_alt, keysyms::KEY_Delete), Some(Action::SecurityOptions));
        assert_eq!(press(&ctrl_alt, keysyms::KEY_KP_Delete), Some(Action::SecurityOptions));
        // The neighbours keep their keys.
        assert_eq!(press(&logo_shift, keysyms::KEY_s), Some(Action::Capture { region: true }), "Super+Shift+S still captures a region");
        assert_eq!(press(&logo, keysyms::KEY_Up), Some(Action::Snap(Direction::Up)));
        assert_eq!(press(&logo, keysyms::KEY_d), Some(Action::ShowDesktop), "Super+D is not Super+Alt+D");
        assert_eq!(press(&mods(true, false, true, false), keysyms::KEY_d), Some(Action::NewDesktop));
        assert_eq!(press(&ctrl_alt, keysyms::KEY_t), Some(Action::Terminal));
        assert_eq!(press(&mods(false, false, false, false), keysyms::KEY_Delete), None, "Delete is the application's");
        assert_eq!(press(&mods(false, false, true, false), keysyms::KEY_Delete), None);
        assert_eq!(press(&mods(false, true, true, true), keysyms::KEY_Delete), None);
        // Ctrl+Alt+Del reaches the desktop through an inhibitor, like Super+L;
        // the others do not, and none works while the session is locked.
        let inhibited = |m: &ModifiersState, sym: u32| shortcut(m, Keysym::new(sym), Keysym::new(sym), false, true);
        assert_eq!(inhibited(&ctrl_alt, keysyms::KEY_Delete), Some(Action::SecurityOptions));
        assert_eq!(inhibited(&logo, keysyms::KEY_s), None);
        let locked = |m: &ModifiersState, sym: u32| shortcut(m, Keysym::new(sym), Keysym::new(sym), true, false);
        assert_eq!(locked(&ctrl_alt, keysyms::KEY_Delete), None);
        assert_eq!(locked(&logo, keysyms::KEY_p), None);
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
    fn three_finger_swipes_move_between_desktops_and_open_task_view() {
        let swipe = |x: f64, y: f64| swipe_action(Point::from((x, y)));
        assert_eq!(swipe(10.0, -20.0), None, "a short swipe does nothing");
        assert_eq!(swipe(-200.0, 15.0), Some(Action::SwitchDesktop { forward: true }));
        assert_eq!(swipe(200.0, -15.0), Some(Action::SwitchDesktop { forward: false }));
        assert_eq!(swipe(20.0, -150.0), Some(Action::Overview));
        assert_eq!(swipe(-20.0, 150.0), Some(Action::ShowDesktop));
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
