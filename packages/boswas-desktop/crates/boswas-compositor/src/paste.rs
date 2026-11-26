//! Pasting on the shell's behalf.
//!
//! The clipboard history (Super+V) and the emoji panel (Super+.) are the
//! shell's. When the user picks an item, the shell puts it on the
//! clipboard through clipboard control (a privileged protocol) and sends
//! `paste` on its trusted control connection. The compositor then gives
//! the focused window the key stroke that pastes there: Ctrl+V, or
//! Ctrl+Shift+V in a terminal, where Ctrl+V means something else.
//!
//! This is not a way to type for anybody: the command takes no keys, only
//! the shell's trusted connection may send it (the public socket refuses
//! it), it goes to a window that has the keyboard and never to a layer
//! surface or the lock screen, and nothing happens while the session is
//! locked. The key stroke waits until the keys the user still holds (the
//! Enter that chose the item) are up, so the window sees Ctrl+V and not
//! Ctrl+Enter+V.

use std::time::{Duration, Instant};

use serde_json::{json, Value};
use smithay::{
    backend::input::KeyState,
    input::keyboard::{keysyms, FilterResult, Keycode, Layout},
    reexports::calloop::timer::{TimeoutAction, Timer},
    utils::SERIAL_COUNTER,
};
use tracing::{debug, warn};

use crate::state::Boswas;

/// Programs (by Wayland application id or X11 class) that paste with
/// Ctrl+Shift+V. Compared without regard to case.
pub const TERMINALS: &[&str] = &[
    "com.boswas.Terminal",
    "foot",
    "footclient",
    "org.kde.konsole",
    "konsole",
    "org.gnome.Terminal",
    "gnome-terminal-server",
    "org.gnome.Console",
    "org.gnome.Ptyxis",
    "kitty",
    "Alacritty",
    "org.wezfurlong.wezterm",
    "com.mitchellh.ghostty",
    "com.gexperts.Tilix",
    "xfce4-terminal",
    "terminator",
    "lxterminal",
    "qterminal",
    "mate-terminal",
    "st-256color",
];

/// Whether a window with this application id pastes with Ctrl+Shift+V.
pub fn is_terminal(app_id: &str) -> bool {
    let id = app_id.trim().trim_end_matches(".desktop");
    TERMINALS.iter().any(|t| t.eq_ignore_ascii_case(id))
}

/// How long a paste waits at most for the keys that are still held.
const SETTLE: Duration = Duration::from_millis(800);
/// How often it looks again.
const POLL: Duration = Duration::from_millis(10);

/// The paste that is on its way (at most one).
#[derive(Debug, Default)]
pub struct Paste {
    pending: Option<Pending>,
}

#[derive(Debug, Clone, Copy)]
struct Pending {
    window: u64,
    terminal: bool,
    deadline: Instant,
}

impl Boswas {
    /// The trusted `paste` command: checks that there is a window to paste
    /// into and sends it the key stroke once the held keys are up.
    pub fn request_paste(&mut self) -> Result<Value, String> {
        if self.lock.locked {
            return Err("the session is locked".into());
        }
        let window = self.focused_window().ok_or_else(|| "no window has the keyboard".to_string())?;
        if self.paste.pending.is_some() {
            return Err("a paste is already on its way".into());
        }
        let app_id = window.app_id();
        let terminal = is_terminal(&app_id);
        let pending = Pending {
            window: window.id(),
            terminal,
            deadline: Instant::now() + SETTLE,
        };
        self.paste.pending = Some(pending);
        let timer = Timer::immediate();
        let inserted = self.loop_handle.insert_source(timer, |_, _, state| {
            if state.paste_when_settled() {
                TimeoutAction::Drop
            } else {
                TimeoutAction::ToDuration(POLL)
            }
        });
        if let Err(err) = inserted {
            self.paste.pending = None;
            warn!("paste: no timer ({err})");
            return Err("the paste could not be scheduled".into());
        }
        Ok(json!({ "keys": if terminal { "ctrl+shift+v" } else { "ctrl+v" }, "app_id": app_id }))
    }

    /// Sends the paste once no key is held any more (or the wait is over).
    /// Returns whether the paste is finished, sent or dropped.
    fn paste_when_settled(&mut self) -> bool {
        let Some(pending) = self.paste.pending else {
            return true;
        };
        if !self.keyboard.pressed_keys().is_empty() && Instant::now() < pending.deadline {
            return false;
        }
        self.paste.pending = None;
        // Still the window it was meant for, and still allowed.
        if self.lock.locked || self.focused_window().map(|w| w.id()) != Some(pending.window) {
            debug!("paste dropped: the focus moved or the session locked");
            return true;
        }
        let control = self.keycode_for(&[keysyms::KEY_Control_L, keysyms::KEY_Control_R]);
        let shift = self.keycode_for(&[keysyms::KEY_Shift_L, keysyms::KEY_Shift_R]);
        let v = self.keycode_for(&[keysyms::KEY_v, keysyms::KEY_V]).or(Some(Keycode::new(KEY_V_EVDEV + 8)));
        let (Some(control), Some(v)) = (control, v) else {
            warn!("paste: the keyboard layout has no Control key");
            return true;
        };
        let mut keys = vec![control];
        if pending.terminal {
            match shift {
                Some(shift) => keys.push(shift),
                None => warn!("paste: the keyboard layout has no Shift key"),
            }
        }
        keys.push(v);
        for key in &keys {
            self.inject_key(*key, KeyState::Pressed);
        }
        for key in keys.iter().rev() {
            self.inject_key(*key, KeyState::Released);
        }
        true
    }

    /// A key event for the focused client only: no shortcut, no sticky or
    /// filter keys, nothing else of the compositor sees it.
    fn inject_key(&mut self, keycode: Keycode, state: KeyState) {
        let serial = SERIAL_COUNTER.next_serial();
        let time = self.start_time.elapsed().as_millis() as u32;
        let keyboard = self.keyboard.clone();
        keyboard.input::<(), _>(self, keycode, state, serial, time, |_, _, _| FilterResult::Forward);
    }

    /// The key that gives one of `wanted` without modifiers in the active
    /// layout, else in any layout of the keymap.
    fn keycode_for(&mut self, wanted: &[u32]) -> Option<Keycode> {
        let keyboard = self.keyboard.clone();
        keyboard.with_xkb_state(self, |context| {
            let xkb = context.xkb().lock().unwrap();
            let active = xkb.active_layout();
            let layouts: Vec<Layout> = std::iter::once(active).chain(xkb.layouts().filter(|l| *l != active)).collect();
            layouts.into_iter().find_map(|layout| {
                (8..=255u32).map(Keycode::new).find(|code| {
                    xkb.raw_syms_for_key_in_layout(*code, layout).iter().any(|sym| wanted.contains(&sym.raw()))
                })
            })
        })
    }
}

/// The V key's evdev code: the key used when no layout has a "v".
const KEY_V_EVDEV: u32 = 47;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminals_paste_with_ctrl_shift_v() {
        for id in ["com.boswas.Terminal", "foot", "org.kde.konsole", "kitty", "Alacritty", "org.gnome.Console.desktop", "FOOT"] {
            assert!(is_terminal(id), "{id}");
        }
        for id in ["com.boswas.TextEditor", "firefox-esr", "org.kde.dolphin", "", "terminal-but-not", "XTerm"] {
            assert!(!is_terminal(id), "{id}");
        }
    }
}
