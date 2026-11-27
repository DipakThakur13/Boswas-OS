//! What some Windows shortcuts do that the compositor carries out itself,
//! and the session's state they keep:
//!
//! * Super+Home minimises every window of the desktop but the active one;
//!   again, it brings them back;
//! * Super+Shift+Up stretches the active window to the full height of the
//!   screen, keeping its width; Super+Shift+Down puts it back (and
//!   minimises a window that was not stretched, as Super+Down does);
//! * Super+U opens Settings > Accessibility;
//! * `[keyboard] numlock` sets Num Lock at start and when the setting
//!   changes (otherwise a reload keeps it as it was);
//! * night light turned on or off now (nightlight.rs) and how the monitors
//!   are used (projection.rs) are kept here for the session.

use std::collections::{BTreeMap, HashMap};

use smithay::{
    backend::input::KeyState,
    input::keyboard::FilterResult,
    utils::{Logical, Rectangle, SERIAL_COUNTER},
};

use crate::{config::OutputSettings, shell::element::Tile, state::Boswas};

/// The Num Lock key (evdev `KEY_NUMLOCK`, plus 8 for XKB).
pub const NUM_LOCK_KEYCODE: u32 = 69 + 8;

/// The session's state of these shortcuts.
#[derive(Default)]
pub struct WinKeys {
    /// Night light turned on or off against its schedule.
    pub night_override: Option<crate::nightlight::Override>,
    /// The windows Super+Home minimised, bottom to top.
    pub minimized_others: Option<Vec<u64>>,
    /// Windows Super+Shift+Up stretched, and where they were before.
    pub stretched: HashMap<u64, Rectangle<i32, Logical>>,
    /// How the monitors are used (Super+P), when chosen this session.
    pub projection: Option<crate::projection::Mode>,
    /// The output settings of the files, under the projection's choice.
    pub configured_outputs: Option<BTreeMap<String, OutputSettings>>,
}

/// The settings command that opens a page: Control Center takes `--page`;
/// another program configured in `[apps] settings` is started as it is.
pub fn settings_page(command: &[String], page: &str) -> Vec<String> {
    let mut out = command.to_vec();
    let is_control_center = command.first().and_then(|p| std::path::Path::new(p).file_name()).is_some_and(|name| name == "boswas-control-center");
    if is_control_center {
        out.extend(["--page".to_owned(), page.to_owned()]);
    }
    out
}

/// Num Lock after the configuration was read again: the setting where it
/// changed (Settings switched it), else as it is now (the user may have
/// pressed the key since the session started).
pub fn num_lock_after_reload(setting_before: bool, setting_after: bool, now: bool) -> bool {
    if setting_before != setting_after {
        setting_after
    } else {
        now
    }
}

/// Where Super+Shift+Up puts a window: the full height of the usable area,
/// at its own horizontal place and width.
pub fn stretched(rect: Rectangle<i32, Logical>, area: Rectangle<i32, Logical>) -> Rectangle<i32, Logical> {
    Rectangle::new((rect.loc.x, area.loc.y).into(), (rect.size.w, area.size.h).into())
}

impl Boswas {
    /// Super+Home.
    pub fn minimize_others(&mut self) {
        if self.lock.locked {
            return;
        }
        if let Some(ids) = self.winkeys.minimized_others.take() {
            // Again: what it minimised comes back, the active window on top.
            let active = self.focused_window();
            for id in ids {
                let Some(window) = self.window_by_id(id) else {
                    continue;
                };
                let data = window.data();
                if data.minimized.get() && data.workspace.get() == self.workspace {
                    self.activate_window(&window);
                }
            }
            if let Some(active) = active {
                self.activate_window(&active);
            }
            return;
        }
        let Some(active) = self.focused_window() else {
            return;
        };
        let others: Vec<u64> = self
            .space
            .elements()
            .filter(|w| *w != &active && w.data().workspace.get() == self.workspace && !w.data().minimized.get() && !w.is_override_redirect())
            .map(|w| w.id())
            .collect();
        for id in &others {
            if let Some(window) = self.window_by_id(*id) {
                self.minimize_window(&window);
            }
        }
        if !others.is_empty() {
            self.winkeys.minimized_others = Some(others);
        }
    }

    /// Super+Shift+Up (`up`) and Super+Shift+Down.
    pub fn stretch_key(&mut self, up: bool) {
        let Some(window) = self.focused_window() else {
            return;
        };
        let id = window.id();
        let tile = window.data().tile.get();
        // Forget windows that are gone.
        let live: Vec<u64> = self.windows.iter().map(|w| w.id()).collect();
        self.winkeys.stretched.retain(|id, _| live.contains(id));
        if up {
            if tile != Tile::Floating {
                return;
            }
            let Some(rect) = self.window_rect(&window) else {
                return;
            };
            let Some(output) = self.output_for_rect(rect) else {
                return;
            };
            let target = stretched(rect, self.usable_area(&output));
            if target == rect {
                return;
            }
            self.winkeys.stretched.entry(id).or_insert(rect);
            window.configure(Some(target), Tile::Floating);
            self.move_window(&window, target.loc);
            self.windows_changed();
            return;
        }
        match self.winkeys.stretched.remove(&id) {
            Some(before) if tile == Tile::Floating => {
                window.configure(Some(before), Tile::Floating);
                self.move_window(&window, before.loc);
                self.windows_changed();
            }
            // Maximised or snapped since: restored as Super+Down would.
            _ if tile != Tile::Floating => self.set_tile(&window, Tile::Floating),
            _ => self.minimize_window(&window),
        }
    }

    /// Super+U.
    pub fn open_accessibility_settings(&mut self) {
        let command = settings_page(&self.config.apps.settings, "accessibility");
        self.spawn(&command);
    }

    /// Sets Num Lock on or off: a press and release of the Num Lock key,
    /// so the keymap locks the modifier, the keyboard's light follows and
    /// the focused window learns of it as of any key.
    pub fn set_num_lock(&mut self, on: bool) {
        let keyboard = self.keyboard.clone();
        if keyboard.modifier_state().num_lock == on {
            return;
        }
        let time = self.start_time.elapsed().as_millis() as u32;
        for state in [KeyState::Pressed, KeyState::Released] {
            let serial = SERIAL_COUNTER.next_serial();
            keyboard.input::<(), _>(self, NUM_LOCK_KEYCODE.into(), state, serial, time, |_, _, _| FilterResult::Forward);
        }
    }

    /// Whether Num Lock is on.
    pub fn num_lock(&self) -> bool {
        self.keyboard.modifier_state().num_lock
    }

    /// Night light on or off now (the quick settings tile): with a
    /// schedule, until its next change.
    pub fn set_night_light_now(&mut self, on: bool) {
        use chrono::Timelike;
        let now = chrono::Local::now();
        let settings = crate::config::NightLight {
            enabled: self.night_light.0,
            temperature: self.night_light.1,
            ..self.config.night_light.clone()
        };
        let (enabled, choice) = crate::nightlight::set_now(&settings, on, now.hour() * 60 + now.minute());
        self.night_light.0 = enabled;
        self.winkeys.night_override = choice;
        self.apply_night_light();
        let state = self.night_light_json();
        self.ipc_event("night-light", state);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn super_u_opens_the_accessibility_page_of_control_center() {
        let cc = vec!["boswas-control-center".to_owned()];
        assert_eq!(settings_page(&cc, "accessibility"), ["boswas-control-center", "--page", "accessibility"]);
        let full = vec!["/usr/bin/boswas-control-center".to_owned()];
        assert_eq!(settings_page(&full, "accessibility").len(), 3);
        let other = vec!["other-settings".to_owned(), "--x".to_owned()];
        assert_eq!(settings_page(&other, "accessibility"), other, "another program does not know the page names");
        assert!(settings_page(&[], "accessibility").is_empty());
    }

    #[test]
    fn a_reload_keeps_num_lock_unless_the_setting_changed() {
        assert!(num_lock_after_reload(false, true, false), "switched on in Settings");
        assert!(!num_lock_after_reload(true, false, true), "switched off in Settings");
        assert!(num_lock_after_reload(false, false, true), "pressed by hand: kept");
        assert!(!num_lock_after_reload(true, true, false), "released by hand: kept");
    }

    #[test]
    fn a_stretched_window_keeps_its_width_and_place_and_takes_the_full_height() {
        let rect = Rectangle::new((300, 200).into(), (640, 480).into());
        let area = Rectangle::new((0, 0).into(), (1920, 1032).into());
        assert_eq!(stretched(rect, area), Rectangle::new((300, 0).into(), (640, 1032).into()));
        let second = Rectangle::new((1920, 30).into(), (1280, 994).into());
        assert_eq!(stretched(Rectangle::new((2000, 400).into(), (500, 300).into()), second), Rectangle::new((2000, 30).into(), (500, 994).into()));
    }
}
