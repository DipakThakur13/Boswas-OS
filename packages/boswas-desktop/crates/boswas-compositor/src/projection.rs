//! How the monitors are used (Super+P, the shell's projection flyout): PC
//! screen only, Duplicate, Extend, Second screen only. The choice holds
//! for the session, on top of the configuration's `[outputs.NAME]`
//! settings (Settings writes those; this does not): the monitors the
//! choice turns off are turned off, and Duplicate puts every monitor at the
//! same place of the desktop, so each shows the same part of it. The
//! backend turns monitors on and off and places them as it does for a
//! changed configuration, and never turns the last one off.

use std::collections::BTreeMap;

use crate::config::OutputSettings;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    PcOnly,
    Duplicate,
    Extend,
    SecondOnly,
}

impl Mode {
    pub const ALL: [Mode; 4] = [Mode::PcOnly, Mode::Duplicate, Mode::Extend, Mode::SecondOnly];

    pub fn key(self) -> &'static str {
        match self {
            Mode::PcOnly => "pc-only",
            Mode::Duplicate => "duplicate",
            Mode::Extend => "extend",
            Mode::SecondOnly => "second-only",
        }
    }

    pub fn from_key(key: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|m| m.key() == key)
    }

    /// Whether the choice can be made with this many monitors.
    pub fn possible(self, outputs: usize) -> bool {
        matches!(self, Mode::PcOnly | Mode::Extend) || outputs > 1
    }
}

/// The PC's own screen: the built-in panel (eDP, LVDS, DSI), or else the
/// first monitor by name.
pub fn primary(names: &[String]) -> Option<&String> {
    let internal = |name: &&String| ["eDP", "LVDS", "DSI"].iter().any(|prefix| name.starts_with(prefix));
    names.iter().find(internal).or_else(|| names.iter().min())
}

/// The output settings a choice leads to, from the configured ones, for
/// the connected monitors `names`.
pub fn apply(mode: Mode, names: &[String], configured: &BTreeMap<String, OutputSettings>) -> BTreeMap<String, OutputSettings> {
    let mut outputs = configured.clone();
    let Some(primary) = primary(names).cloned() else {
        return outputs;
    };
    for name in names {
        let settings = outputs.entry(name.clone()).or_default();
        let is_primary = *name == primary;
        match mode {
            Mode::PcOnly => settings.enabled = Some(is_primary),
            Mode::SecondOnly => settings.enabled = Some(!is_primary),
            Mode::Extend => settings.enabled = Some(true),
            Mode::Duplicate => {
                settings.enabled = Some(true);
                settings.position = Some([0, 0]);
            }
        }
    }
    outputs
}

/// What the monitors do now, as far as it can be told from the settings in
/// use: the choice made, or else one monitor on (PC screen only or second
/// screen only), or else Extend.
pub fn current(choice: Option<Mode>, names: &[String], enabled: &[String]) -> Mode {
    if let Some(choice) = choice {
        return choice;
    }
    match (primary(names), enabled) {
        (Some(primary), [only]) if names.len() > 1 => {
            if only == primary {
                Mode::PcOnly
            } else {
                Mode::SecondOnly
            }
        }
        _ => Mode::Extend,
    }
}

impl crate::state::Boswas {
    /// The connected monitors' names, turned on or off.
    pub fn output_names(&self) -> Vec<String> {
        let mut names: Vec<String> = self.space.outputs().map(|o| o.name()).collect();
        names.extend(self.disabled_outputs.iter().cloned());
        names.sort();
        names.dedup();
        names
    }

    /// What the projection flyout shows: how many monitors, and how they
    /// are used.
    pub fn projection_json(&self) -> serde_json::Value {
        let names = self.output_names();
        let enabled: Vec<String> = self.space.outputs().map(|o| o.name()).collect();
        let mode = current(self.winkeys.projection, &names, &enabled);
        serde_json::json!({ "outputs": names.len().max(1), "mode": mode.key() })
    }

    /// The shell's `project` command.
    pub fn project(&mut self, key: &str) -> Result<serde_json::Value, String> {
        let mode = Mode::from_key(key).ok_or_else(|| format!("unknown way to use the monitors \"{key}\""))?;
        let names = self.output_names();
        if !mode.possible(names.len()) {
            return Err("only one monitor is connected".into());
        }
        self.winkeys.projection = Some(mode);
        self.apply_projection();
        Ok(self.projection_json())
    }

    /// Puts the session's choice on top of the configured output settings
    /// and lets the backend carry it out. Called after the choice and after
    /// every reload of the configuration.
    pub fn apply_projection(&mut self) {
        if !self.projection_outputs() {
            return;
        }
        let config = self.config.clone();
        self.backend.config_changed(&config);
        self.schedule_redraw();
    }

    /// The output settings in use with the session's choice put on top of
    /// the files' (always from those, not from an earlier choice). Returns
    /// whether there is a choice.
    pub fn projection_outputs(&mut self) -> bool {
        let Some(mode) = self.winkeys.projection else {
            return false;
        };
        let names = self.output_names();
        let configured = self.winkeys.configured_outputs.get_or_insert_with(|| self.config.outputs.clone()).clone();
        self.config.outputs = apply(mode, &names, &configured);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn the_built_in_panel_is_the_pc_screen() {
        assert_eq!(primary(&names(&["HDMI-A-1", "eDP-1"])).map(String::as_str), Some("eDP-1"));
        assert_eq!(primary(&names(&["HDMI-A-1", "DP-2"])).map(String::as_str), Some("DP-2"), "else the first by name");
        assert_eq!(primary(&[]), None);
    }

    #[test]
    fn each_choice_turns_the_right_monitors_on_and_places_them() {
        let connected = names(&["HDMI-A-1", "eDP-1"]);
        let mut configured = BTreeMap::new();
        configured.insert("HDMI-A-1".to_owned(), OutputSettings { position: Some([1920, 0]), scale: Some(1.5), ..Default::default() });
        let on = |outputs: &BTreeMap<String, OutputSettings>, name: &str| outputs.get(name).and_then(|s| s.enabled);
        let pc = apply(Mode::PcOnly, &connected, &configured);
        assert_eq!((on(&pc, "eDP-1"), on(&pc, "HDMI-A-1")), (Some(true), Some(false)));
        let second = apply(Mode::SecondOnly, &connected, &configured);
        assert_eq!((on(&second, "eDP-1"), on(&second, "HDMI-A-1")), (Some(false), Some(true)));
        let extend = apply(Mode::Extend, &configured.keys().cloned().chain(["eDP-1".to_owned()]).collect::<Vec<_>>(), &configured);
        assert_eq!(extend["HDMI-A-1"].position, Some([1920, 0]), "Extend keeps the arrangement of Settings");
        assert_eq!(extend["HDMI-A-1"].scale, Some(1.5));
        let duplicate = apply(Mode::Duplicate, &connected, &configured);
        assert!(connected.iter().all(|n| duplicate[n].position == Some([0, 0]) && duplicate[n].enabled == Some(true)));
        assert_eq!(duplicate["HDMI-A-1"].scale, Some(1.5), "other settings stay");
    }

    #[test]
    fn one_monitor_allows_only_pc_screen_and_extend() {
        assert!(Mode::PcOnly.possible(1) && Mode::Extend.possible(1));
        assert!(!Mode::Duplicate.possible(1) && !Mode::SecondOnly.possible(1));
        assert!(Mode::ALL.iter().all(|m| m.possible(2)));
        for mode in Mode::ALL {
            assert_eq!(Mode::from_key(mode.key()), Some(mode));
        }
    }

    #[test]
    fn the_way_the_monitors_are_used_is_told_from_what_is_on() {
        let connected = names(&["HDMI-A-1", "eDP-1"]);
        assert_eq!(current(None, &connected, &names(&["eDP-1"])), Mode::PcOnly);
        assert_eq!(current(None, &connected, &names(&["HDMI-A-1"])), Mode::SecondOnly);
        assert_eq!(current(None, &connected, &connected), Mode::Extend);
        assert_eq!(current(None, &names(&["eDP-1"]), &names(&["eDP-1"])), Mode::Extend, "one monitor: extended over itself");
        assert_eq!(current(Some(Mode::Duplicate), &connected, &connected), Mode::Duplicate);
    }
}
