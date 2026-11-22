//! Compositor configuration.
//!
//! Three TOML layers are merged, later ones overriding earlier ones:
//!
//! 1. `/usr/share/boswas/compositor/compositor.toml`  shipped defaults
//! 2. `/etc/boswas/compositor.toml`                   the administrator
//! 3. `$XDG_CONFIG_HOME/boswas/compositor.toml`       the user
//! 4. `$XDG_CONFIG_HOME/boswas/appearance.toml`       the preset chosen in
//!    Control Center (`boswas-preset`); only its `[appearance]` table counts
//!
//! The `[policy]` table is read from the first two layers only: a user file
//! cannot lengthen the automatic lock timeout the administrator enforces,
//! nor widen which clients may use the privileged protocols.

use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

use serde::Deserialize;
use toml::{Table, Value};

pub const SYSTEM_DEFAULTS: &str = "/usr/share/boswas/compositor/compositor.toml";
pub const SYSTEM_CONFIG: &str = "/etc/boswas/compositor.toml";

/// Size limit of one configuration file. Larger files are ignored.
const MAX_CONFIG_BYTES: u64 = 256 * 1024;

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub keyboard: Keyboard,
    pub pointer: Pointer,
    pub touchpad: Touchpad,
    pub output: Output,
    /// Settings of single outputs by connector name (`eDP-1`, `HDMI-A-1`).
    pub outputs: BTreeMap<String, OutputSettings>,
    pub night_light: NightLight,
    pub power: Power,
    pub lock: Lock,
    pub shell: Shell,
    pub input_method: InputMethod,
    pub xwayland: Xwayland,
    pub apps: Apps,
    pub appearance: Appearance,
    /// Text size and scroll bars of every program (the shell, title bars,
    /// Qt applications).
    pub accessibility: Accessibility,
    pub color_filter: ColorFilter,
    pub magnifier: Magnifier,
    /// The shell's taskbar, Start and notifications: read here and passed
    /// on to the shell with the appearance.
    pub taskbar: Taskbar,
    pub start: Start,
    pub notifications: Notifications,
    pub multitasking: Multitasking,
    pub policy: Policy,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Keyboard {
    /// XKB layout, for example `us` or `us,in`.
    pub layout: String,
    pub variant: String,
    pub model: String,
    /// XKB options, for example `grp:alt_shift_toggle`.
    pub options: String,
    /// Milliseconds before a held key repeats.
    pub repeat_delay: i32,
    /// Repeats per second.
    pub repeat_rate: i32,
    /// Num Lock is on when the session starts.
    pub numlock: bool,
    /// Shift, Ctrl, Alt and Super stay pressed for the next key after they
    /// are pressed and released alone.
    pub sticky_keys: bool,
    /// With sticky keys: a modifier pressed twice in a row stays pressed
    /// until it is pressed a third time.
    pub sticky_keys_lock: bool,
    /// Ignore brief and repeated key strokes (the two times below).
    pub filter_keys: bool,
    /// With filter keys: a key counts only after it was held this long
    /// (milliseconds; 0: at once).
    pub slow_keys_ms: u32,
    /// With filter keys: a key pressed again this soon after it was
    /// released is ignored (milliseconds; 0: never).
    pub bounce_keys_ms: u32,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Pointer {
    /// Mice and trackballs: content follows the wheel's direction reversed.
    pub natural_scroll: bool,
    /// libinput acceleration speed, -1.0 to 1.0.
    pub accel_speed: f64,
    /// `adaptive` (speed-dependent) or `flat` (constant).
    pub accel_profile: String,
    /// Swaps the left and right buttons.
    pub left_handed: bool,
    /// XCursor theme name; empty uses `XCURSOR_THEME` or the default theme.
    pub cursor_theme: String,
    pub cursor_size: u32,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Touchpad {
    pub tap_to_click: bool,
    /// Content follows the fingers.
    pub natural_scroll: bool,
    /// libinput acceleration speed, -1.0 to 1.0.
    pub accel_speed: f64,
    /// The touchpad ignores touches while keys are being typed.
    pub disable_while_typing: bool,
    /// Three and four finger swipes switch desktops and open Task View.
    pub gestures: bool,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Output {
    /// Scale factor of outputs that have no setting of their own: 1.0,
    /// 1.25, 1.5, 1.75 or 2.0.
    pub scale: f64,
}

/// What can be set for one output (`[outputs.NAME]`). Everything is
/// optional: what is not set is chosen by the compositor.
#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct OutputSettings {
    /// `false` turns the output off.
    pub enabled: Option<bool>,
    /// `WIDTHxHEIGHT` or `WIDTHxHEIGHT@HZ`, one of the modes the output
    /// offers (without a rate, the highest it offers at that size); anything
    /// else keeps the default: the monitor's native size at its highest
    /// refresh rate.
    pub mode: Option<String>,
    pub scale: Option<f64>,
    /// Position of the top-left corner on the desktop, in logical pixels.
    pub position: Option<[i32; 2]>,
    /// `normal`, `90`, `180` or `270`.
    pub rotation: Option<String>,
    /// Variable refresh rate (adaptive sync) where the monitor and its
    /// driver offer it. Off unless `true`.
    pub adaptive_sync: Option<bool>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct NightLight {
    pub enabled: bool,
    /// Colour temperature in kelvin while night light is on, 2500 to 6500.
    pub temperature: u32,
    /// Turn on and off by the clock instead of staying on.
    pub scheduled: bool,
    /// `HH:MM`, local time.
    pub from: String,
    pub to: String,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Power {
    /// Seconds without input before the screens are turned off; 0 never.
    pub screen_off_seconds: u32,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Lock {
    /// Seconds without input before the session locks; 0 never locks.
    pub idle_seconds: u32,
    /// The lock screen program. It is started as a trusted client.
    pub command: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Shell {
    /// The shell program (panel, launcher). It is started as a trusted
    /// client and restarted when it exits. Empty starts no shell.
    pub command: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct InputMethod {
    /// The input method program (for example `["fcitx5"]`); empty for none.
    /// It is started as a trusted client, because an input method sees
    /// what is typed and may type itself: only the system files may name it.
    pub command: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Xwayland {
    /// Run an X server for X11 applications.
    pub enabled: bool,
}

impl Default for Xwayland {
    fn default() -> Self {
        Self { enabled: true }
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Apps {
    /// Ctrl+Alt+T.
    pub terminal: Vec<String>,
    /// Super+E.
    pub files: Vec<String>,
    /// Super+I.
    pub settings: Vec<String>,
    /// Ctrl+Shift+Esc.
    pub task_manager: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Appearance {
    /// `dark` or `light`.
    pub scheme: String,
    /// Turns every animation off.
    pub reduced_motion: bool,
    /// A picture (PNG or JPEG, absolute file name) the shell shows instead
    /// of the default background; empty for the default.
    pub wallpaper: String,
    /// The accent colour: a hue of the palette ([`ACCENTS`]).
    pub accent: String,
    /// The shell's surfaces show the blurred wallpaper through their tint;
    /// `false` fills them with the tint alone.
    pub transparency: bool,
}

/// The palette hues that can be the accent colour; the first is the default.
pub const ACCENTS: [&str; 8] = ["iris", "azure", "sky", "mint", "sun", "coral", "orchid", "slate"];

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Accessibility {
    /// Text size in percent of the type ramp, 100 to 225; nothing else of
    /// the interface grows.
    pub text_scale: u32,
    /// Qt applications show full scroll bars instead of thin lines that
    /// widen under the pointer.
    pub always_show_scrollbars: bool,
}

/// The colour filters of the whole picture.
pub const COLOR_FILTERS: [&str; 6] = ["grayscale", "inverted", "grayscale_inverted", "deuteranopia", "protanopia", "tritanopia"];

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ColorFilter {
    pub enabled: bool,
    /// One of [`COLOR_FILTERS`].
    pub filter: String,
    /// Super+Ctrl+C turns the filter on and off.
    pub shortcut: bool,
}

/// The zoom steps of the magnifier, in percent.
pub const MAGNIFIER_STEPS: [u32; 6] = [25, 50, 100, 150, 200, 400];

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Magnifier {
    /// The picture is enlarged around the pointer.
    pub enabled: bool,
    /// Zoom in percent, 100 to 1600.
    pub zoom: u32,
    /// What Super+Plus and Super+Minus add and take away, in percent.
    pub step: u32,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Taskbar {
    /// `center` or `left`: where Start and the applications are.
    pub alignment: String,
    /// The taskbar slides away and comes back when the pointer touches the
    /// bottom edge; windows use the whole screen.
    pub auto_hide: bool,
    /// The Task View button.
    pub task_view: bool,
    /// The clock shows seconds.
    pub clock_seconds: bool,
    /// Taskbar buttons show how many unread notifications their
    /// application has.
    pub badges: bool,
}

/// The folders Start can show next to the power button.
pub const START_FOLDERS: [&str; 7] = ["settings", "files", "documents", "downloads", "music", "pictures", "videos"];

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Start {
    /// `default`, `more_pins` or `more_recommendations`.
    pub layout: String,
    /// Applications installed lately are recommended.
    pub recently_added: bool,
    /// The applications used most are recommended.
    pub most_used: bool,
    /// Buttons next to the power button, from [`START_FOLDERS`].
    pub folders: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Notifications {
    /// Only critical notifications pop up; the others go to the
    /// notification centre silently.
    pub do_not_disturb: bool,
    /// Do not disturb turns on and off by the clock.
    pub do_not_disturb_scheduled: bool,
    /// `HH:MM`, local time.
    pub do_not_disturb_from: String,
    pub do_not_disturb_to: String,
    /// Applications (by the name they send) whose notifications do not pop
    /// up; they still go to the notification centre.
    pub banners_off: Vec<String>,
    /// How long a notification stays on screen at least, in seconds.
    pub toast_seconds: u32,
}

/// How long notifications can stay, in seconds.
pub const TOAST_SECONDS: [u32; 6] = [5, 7, 15, 30, 60, 300];

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Multitasking {
    /// Dragging a window to an edge and Super+Left/Right snap it.
    pub snap_windows: bool,
    /// Resting the pointer on the maximize button offers the snap layouts.
    pub snap_layouts_on_maximize: bool,
    /// Dragging a window to the top edge offers the snap layouts.
    pub snap_layouts_on_drag: bool,
    /// `desktop`: the taskbar shows the windows of the current desktop;
    /// `all`: of every desktop.
    pub taskbar_windows: String,
    /// The same for Alt+Tab.
    pub alt_tab_windows: String,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Policy {
    /// The longest automatic lock timeout a user may choose, in seconds;
    /// 0 lets the user decide (including never).
    pub lock_idle_max_seconds: u32,
    /// `shell`: panels, the lock screen, the window list and clipboard
    /// control are offered only to the programs the compositor starts
    /// itself. `all`: to every client that is not sandboxed (development).
    pub privileged_protocols: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            keyboard: Keyboard::default(),
            pointer: Pointer::default(),
            touchpad: Touchpad::default(),
            output: Output::default(),
            outputs: BTreeMap::new(),
            night_light: NightLight::default(),
            power: Power::default(),
            lock: Lock::default(),
            shell: Shell::default(),
            input_method: InputMethod::default(),
            xwayland: Xwayland::default(),
            apps: Apps::default(),
            appearance: Appearance::default(),
            accessibility: Accessibility::default(),
            color_filter: ColorFilter::default(),
            magnifier: Magnifier::default(),
            taskbar: Taskbar::default(),
            start: Start::default(),
            notifications: Notifications::default(),
            multitasking: Multitasking::default(),
            policy: Policy::default(),
        }
    }
}

impl Default for Accessibility {
    fn default() -> Self {
        Self {
            text_scale: 100,
            always_show_scrollbars: false,
        }
    }
}

impl Default for ColorFilter {
    fn default() -> Self {
        Self {
            enabled: false,
            filter: "grayscale".into(),
            shortcut: false,
        }
    }
}

impl Default for Magnifier {
    fn default() -> Self {
        Self {
            enabled: false,
            zoom: 200,
            step: 100,
        }
    }
}

impl Default for Taskbar {
    fn default() -> Self {
        Self {
            alignment: "center".into(),
            auto_hide: false,
            task_view: true,
            clock_seconds: false,
            badges: true,
        }
    }
}

impl Default for Start {
    fn default() -> Self {
        Self {
            layout: "default".into(),
            recently_added: true,
            most_used: true,
            folders: vec!["settings".into(), "files".into()],
        }
    }
}

impl Default for Notifications {
    fn default() -> Self {
        Self {
            do_not_disturb: false,
            do_not_disturb_scheduled: false,
            do_not_disturb_from: "22:00".into(),
            do_not_disturb_to: "07:00".into(),
            banners_off: Vec::new(),
            toast_seconds: 5,
        }
    }
}

impl Default for Multitasking {
    fn default() -> Self {
        Self {
            snap_windows: true,
            snap_layouts_on_maximize: true,
            snap_layouts_on_drag: true,
            taskbar_windows: "desktop".into(),
            alt_tab_windows: "desktop".into(),
        }
    }
}

impl Default for Keyboard {
    fn default() -> Self {
        Self {
            layout: "us".into(),
            variant: String::new(),
            model: String::new(),
            options: String::new(),
            repeat_delay: 400,
            repeat_rate: 30,
            numlock: false,
            sticky_keys: false,
            sticky_keys_lock: true,
            filter_keys: false,
            slow_keys_ms: 0,
            bounce_keys_ms: 500,
        }
    }
}

impl Default for Pointer {
    fn default() -> Self {
        Self {
            natural_scroll: false,
            accel_speed: 0.0,
            accel_profile: "adaptive".into(),
            left_handed: false,
            cursor_theme: String::new(),
            cursor_size: 24,
        }
    }
}

impl Default for Touchpad {
    fn default() -> Self {
        Self {
            tap_to_click: true,
            natural_scroll: true,
            accel_speed: 0.0,
            disable_while_typing: true,
            gestures: true,
        }
    }
}

impl Default for NightLight {
    fn default() -> Self {
        Self {
            enabled: false,
            temperature: 4500,
            scheduled: false,
            from: "20:00".into(),
            to: "06:00".into(),
        }
    }
}

impl Default for Power {
    fn default() -> Self {
        Self { screen_off_seconds: 300 }
    }
}

impl Default for Output {
    fn default() -> Self {
        Self { scale: 1.0 }
    }
}

impl Default for Lock {
    fn default() -> Self {
        Self {
            idle_seconds: 600,
            command: vec!["/usr/libexec/boswas/boswas-shell".into(), "--lock".into()],
        }
    }
}

impl Default for Shell {
    fn default() -> Self {
        Self {
            command: vec!["/usr/libexec/boswas/boswas-shell".into()],
        }
    }
}

impl Default for Apps {
    fn default() -> Self {
        Self {
            terminal: vec!["boswas-terminal".into()],
            files: vec!["boswas-files".into()],
            settings: vec!["boswas-control-center".into()],
            task_manager: vec!["boswas-task-manager".into()],
        }
    }
}

impl Default for Appearance {
    fn default() -> Self {
        Self {
            scheme: "dark".into(),
            reduced_motion: false,
            wallpaper: String::new(),
            accent: ACCENTS[0].into(),
            transparency: true,
        }
    }
}

impl Default for Policy {
    fn default() -> Self {
        Self {
            lock_idle_max_seconds: 600,
            privileged_protocols: "shell".into(),
        }
    }
}

/// Tables whose `command` names a program the compositor starts as a
/// trusted client. Only the system files may set them.
const TRUSTED_PROGRAM_SECTIONS: &[&str] = &["shell", "lock", "input_method"];

impl Config {
    /// Loads the three layers. Unreadable or invalid files are reported and
    /// skipped: a broken configuration must never keep the session from
    /// starting.
    pub fn load() -> (Self, Vec<String>) {
        let user = user_config_path();
        let appearance = user.as_deref().map(|path| path.with_file_name("appearance.toml"));
        Self::load_layers(
            &[Path::new(SYSTEM_DEFAULTS), Path::new(SYSTEM_CONFIG)],
            user.as_deref(),
            appearance.as_deref(),
        )
    }

    pub fn load_from(system: &[&Path], user: Option<&Path>) -> (Self, Vec<String>) {
        Self::load_layers(system, user, None)
    }

    pub fn load_layers(system: &[&Path], user: Option<&Path>, appearance: Option<&Path>) -> (Self, Vec<String>) {
        let mut warnings = Vec::new();
        let mut merged = Table::new();
        for path in system {
            if let Some(table) = read_table(path, &mut warnings) {
                apply_layer(&mut merged, table, path, &mut warnings);
            }
        }
        // X11 applications can watch each other: when the administrator
        // turns the X server off, a user's file does not turn it back on.
        let x11_off_by_system = matches!(
            merged.get("xwayland").and_then(|table| table.get("enabled")),
            Some(Value::Boolean(false))
        );
        if let Some(mut table) = user.and_then(|path| read_table(path, &mut warnings)) {
            if table.remove("policy").is_some() {
                warnings.push("the [policy] table is ignored in a user configuration".into());
            }
            // The shell and the lock screen are started as trusted clients:
            // which programs they are is the administrator's decision. A
            // file any program of the user can write must not name them.
            for section in TRUSTED_PROGRAM_SECTIONS {
                if let Some(Value::Table(entries)) = table.get_mut(*section) {
                    if entries.remove("command").is_some() {
                        warnings.push(format!("[{section}] command is ignored in a user configuration"));
                    }
                }
            }
            if let Some(path) = user {
                apply_layer(&mut merged, table, path, &mut warnings);
            }
        }
        if let Some((path, table)) = appearance.and_then(|path| Some((path, read_table(path, &mut warnings)?))) {
            // The preset file decides the look and nothing else.
            match table.get("appearance") {
                Some(Value::Table(look)) if table.len() == 1 => {
                    let mut only = Table::new();
                    only.insert("appearance".into(), Value::Table(look.clone()));
                    apply_layer(&mut merged, only, path, &mut warnings);
                }
                _ => warnings.push("appearance.toml may only contain the [appearance] table; ignored".into()),
            }
        }
        let mut config = match Value::Table(merged).try_into::<Config>() {
            Ok(config) => config,
            Err(err) => {
                warnings.push(format!("configuration ignored: {err}"));
                Config::default()
            }
        };
        if x11_off_by_system && config.xwayland.enabled {
            config.xwayland.enabled = false;
            warnings.push("[xwayland] enabled is ignored in a user configuration: X11 applications are turned off for this device".into());
        }
        config.sanitize(&mut warnings);
        (config, warnings)
    }

    /// Parses one TOML document on top of the defaults.
    #[cfg(test)]
    pub fn from_toml(text: &str) -> Result<Self, String> {
        let mut config: Config = toml::from_str(text).map_err(|e| e.to_string())?;
        config.sanitize(&mut Vec::new());
        Ok(config)
    }

    /// Clamps every value to its supported range and applies `[policy]`.
    fn sanitize(&mut self, warnings: &mut Vec<String>) {
        self.keyboard.repeat_delay = self.keyboard.repeat_delay.clamp(100, 2000);
        self.keyboard.repeat_rate = self.keyboard.repeat_rate.clamp(1, 100);
        self.pointer.accel_speed = if self.pointer.accel_speed.is_finite() {
            self.pointer.accel_speed.clamp(-1.0, 1.0)
        } else {
            0.0
        };
        self.pointer.cursor_size = self.pointer.cursor_size.clamp(16, 96);
        if self.pointer.accel_profile != "adaptive" && self.pointer.accel_profile != "flat" {
            warnings.push(format!("pointer.accel_profile \"{}\" is unknown; using adaptive", self.pointer.accel_profile));
            self.pointer.accel_profile = "adaptive".into();
        }
        self.touchpad.accel_speed = if self.touchpad.accel_speed.is_finite() {
            self.touchpad.accel_speed.clamp(-1.0, 1.0)
        } else {
            0.0
        };
        if !SCALES.contains(&self.output.scale) {
            warnings.push(format!("output.scale {} is not supported; using 1.0", self.output.scale));
            self.output.scale = 1.0;
        }
        self.outputs.retain(|name, settings| {
            let valid_name = !name.is_empty()
                && name.len() <= 64
                && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
            if !valid_name {
                warnings.push(format!("outputs.\"{name}\" is not an output name; ignored"));
                return false;
            }
            if settings.scale.is_some_and(|scale| !SCALES.contains(&scale)) {
                warnings.push(format!("outputs.{name}.scale is not supported; ignored"));
                settings.scale = None;
            }
            if settings.mode.as_deref().is_some_and(|mode| parse_mode(mode).is_none()) {
                warnings.push(format!("outputs.{name}.mode is not WIDTHxHEIGHT[@HZ]; ignored"));
                settings.mode = None;
            }
            if settings.rotation.as_deref().is_some_and(|r| !["normal", "90", "180", "270"].contains(&r)) {
                warnings.push(format!("outputs.{name}.rotation must be normal, 90, 180 or 270; ignored"));
                settings.rotation = None;
            }
            if let Some([x, y]) = settings.position {
                if !(-32768..=32767).contains(&x) || !(-32768..=32767).contains(&y) {
                    warnings.push(format!("outputs.{name}.position is out of range; ignored"));
                    settings.position = None;
                }
            }
            true
        });
        self.night_light.temperature = self.night_light.temperature.clamp(2500, 6500);
        for (key, value, default) in [
            ("from", &mut self.night_light.from, "20:00"),
            ("to", &mut self.night_light.to, "06:00"),
        ] {
            if parse_clock(value).is_none() {
                warnings.push(format!("night_light.{key} is not HH:MM; using {default}"));
                *value = default.into();
            }
        }
        if self.power.screen_off_seconds != 0 {
            self.power.screen_off_seconds = self.power.screen_off_seconds.clamp(30, 6 * 3600);
        }
        if self.appearance.scheme != "dark" && self.appearance.scheme != "light" {
            warnings.push(format!("appearance.scheme \"{}\" is unknown; using dark", self.appearance.scheme));
            self.appearance.scheme = "dark".into();
        }
        let mut choice = |table: &str, key: &str, value: &mut String, allowed: &[&str]| {
            if !allowed.contains(&value.as_str()) {
                warnings.push(format!("{table}.{key} \"{value}\" is unknown; using {}", allowed[0]));
                *value = allowed[0].into();
            }
        };
        choice("appearance", "accent", &mut self.appearance.accent, &ACCENTS);
        choice("color_filter", "filter", &mut self.color_filter.filter, &COLOR_FILTERS);
        choice("taskbar", "alignment", &mut self.taskbar.alignment, &["center", "left"]);
        choice("start", "layout", &mut self.start.layout, &["default", "more_pins", "more_recommendations"]);
        choice("multitasking", "taskbar_windows", &mut self.multitasking.taskbar_windows, &["desktop", "all"]);
        choice("multitasking", "alt_tab_windows", &mut self.multitasking.alt_tab_windows, &["desktop", "all"]);
        self.accessibility.text_scale = self.accessibility.text_scale.clamp(100, 225);
        self.magnifier.zoom = self.magnifier.zoom.clamp(100, 1600);
        if !MAGNIFIER_STEPS.contains(&self.magnifier.step) {
            warnings.push(format!("magnifier.step {} is not one of 25, 50, 100, 150, 200 or 400; using 100", self.magnifier.step));
            self.magnifier.step = 100;
        }
        for value in [&mut self.keyboard.slow_keys_ms, &mut self.keyboard.bounce_keys_ms] {
            if *value != 0 {
                *value = (*value).clamp(100, 5000);
            }
        }
        let mut folders: Vec<String> = Vec::new();
        for folder in std::mem::take(&mut self.start.folders) {
            if !START_FOLDERS.contains(&folder.as_str()) {
                warnings.push(format!("start.folders: \"{folder}\" is not a folder Start can show; ignored"));
            } else if !folders.contains(&folder) {
                folders.push(folder);
            }
        }
        self.start.folders = folders;
        for (key, value, default) in [
            ("do_not_disturb_from", &mut self.notifications.do_not_disturb_from, "22:00"),
            ("do_not_disturb_to", &mut self.notifications.do_not_disturb_to, "07:00"),
        ] {
            if parse_clock(value).is_none() {
                warnings.push(format!("notifications.{key} is not HH:MM; using {default}"));
                *value = default.into();
            }
        }
        // Names as notification senders give them: plain text of a sensible
        // length, each once.
        let mut muted: Vec<String> = Vec::new();
        for name in std::mem::take(&mut self.notifications.banners_off) {
            let plain = !name.trim().is_empty() && name.chars().count() <= 128 && !name.chars().any(char::is_control);
            if plain && !muted.contains(&name) && muted.len() < 256 {
                muted.push(name);
            }
        }
        self.notifications.banners_off = muted;
        if !TOAST_SECONDS.contains(&self.notifications.toast_seconds) {
            let wanted = self.notifications.toast_seconds;
            let nearest = TOAST_SECONDS.iter().copied().min_by_key(|s| s.abs_diff(wanted)).unwrap_or(5);
            warnings.push(format!("notifications.toast_seconds {wanted} is not offered; using {nearest}"));
            self.notifications.toast_seconds = nearest;
        }
        if self.policy.privileged_protocols != "shell" && self.policy.privileged_protocols != "all" {
            warnings.push("policy.privileged_protocols must be \"shell\" or \"all\"; using shell".into());
            self.policy.privileged_protocols = "shell".into();
        }
        let max = self.policy.lock_idle_max_seconds;
        if max > 0 && (self.lock.idle_seconds == 0 || self.lock.idle_seconds > max) {
            warnings.push(format!("lock.idle_seconds limited to {max} by policy"));
            self.lock.idle_seconds = max;
        }
    }

    /// Whether this compositor shows the login screen: its shell is the
    /// greeter and there is no session of a user behind it.
    pub fn is_login_screen(&self) -> bool {
        self.shell.command.iter().skip(1).any(|argument| argument == "--greeter")
    }

    /// Whether an X server is started for X11 applications. The login
    /// screen runs none: its only program is the greeter.
    pub fn runs_xwayland(&self) -> bool {
        self.xwayland.enabled && !self.is_login_screen()
    }

    /// Whether ordinary (not sandboxed) clients may use the privileged
    /// protocols too.
    pub fn trust_all_clients(&self) -> bool {
        self.policy.privileged_protocols == "all"
    }
}

/// The output scales the compositor supports.
pub const SCALES: [f64; 5] = [1.0, 1.25, 1.5, 1.75, 2.0];

/// Parses `WIDTHxHEIGHT` or `WIDTHxHEIGHT@HZ` into (width, height, millihertz).
pub fn parse_mode(text: &str) -> Option<(i32, i32, Option<i32>)> {
    let (size, refresh) = match text.split_once('@') {
        Some((size, hz)) => {
            let hz: f64 = hz.parse().ok()?;
            if !(1.0..=1000.0).contains(&hz) {
                return None;
            }
            (size, Some((hz * 1000.0).round() as i32))
        }
        None => (text, None),
    };
    let (w, h) = size.split_once('x')?;
    let (w, h): (i32, i32) = (w.parse().ok()?, h.parse().ok()?);
    ((1..=16384).contains(&w) && (1..=16384).contains(&h)).then_some((w, h, refresh))
}

/// Parses `HH:MM` into minutes after midnight.
pub fn parse_clock(text: &str) -> Option<u32> {
    let (h, m) = text.split_once(':')?;
    if h.len() != 2 || m.len() != 2 {
        return None;
    }
    let (h, m): (u32, u32) = (h.parse().ok()?, m.parse().ok()?);
    (h < 24 && m < 60).then_some(h * 60 + m)
}

/// Whether `minutes` after local midnight lies in the span from `from` to
/// `to` (`HH:MM`; the end is exclusive, and a span may run over midnight).
pub fn clock_span_contains(from: &str, to: &str, minutes: u32) -> bool {
    let (Some(from), Some(to)) = (parse_clock(from), parse_clock(to)) else {
        return false;
    };
    if from <= to {
        (from..to).contains(&minutes)
    } else {
        minutes >= from || minutes < to
    }
}

impl NightLight {
    /// Whether night light is on at `minutes` after local midnight.
    pub fn active_at(&self, minutes: u32) -> bool {
        if !self.enabled {
            return false;
        }
        if !self.scheduled {
            return true;
        }
        if parse_clock(&self.from).is_none() || parse_clock(&self.to).is_none() {
            return true;
        }
        clock_span_contains(&self.from, &self.to, minutes)
    }
}

/// `$XDG_CONFIG_HOME/boswas/compositor.toml`, or the `~/.config` equivalent.
pub fn user_config_path() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))?;
    Some(base.join("boswas").join("compositor.toml"))
}

fn read_table(path: &Path, warnings: &mut Vec<String>) -> Option<Table> {
    let meta = std::fs::metadata(path).ok()?;
    if !meta.is_file() || meta.len() > MAX_CONFIG_BYTES {
        warnings.push(format!("{}: not a regular file of a sensible size; ignored", path.display()));
        return None;
    }
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(err) => {
            warnings.push(format!("{}: {err}", path.display()));
            return None;
        }
    };
    match text.parse::<Table>() {
        Ok(table) => Some(table),
        Err(err) => {
            warnings.push(format!("{}: {err}", path.display()));
            None
        }
    }
}

/// Lays one file's settings over what the earlier files gave, if the result
/// is still a configuration: a file with an unknown key or a value of the
/// wrong kind is reported and ignored as a whole, and what the files before
/// it decided stays. (Without this a user's damaged file would also throw
/// away the administrator's settings, the lock policy among them.)
fn apply_layer(merged: &mut Table, layer: Table, path: &Path, warnings: &mut Vec<String>) {
    let mut candidate = merged.clone();
    merge(&mut candidate, layer);
    match Value::Table(candidate.clone()).try_into::<Config>() {
        Ok(_) => *merged = candidate,
        Err(err) => warnings.push(format!("{}: ignored: {}", path.display(), err.to_string().trim())),
    }
}

/// Deep merge: tables merge key by key, every other value is replaced.
fn merge(base: &mut Table, over: Table) {
    for (key, value) in over {
        match (base.get_mut(&key), value) {
            (Some(Value::Table(existing)), Value::Table(new)) => merge(existing, new),
            (_, value) => {
                base.insert(key, value);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn file(dir: &Path, name: &str, text: &str) -> PathBuf {
        let path = dir.join(name);
        std::fs::File::create(&path).unwrap().write_all(text.as_bytes()).unwrap();
        path
    }

    #[test]
    fn defaults_enforce_the_ten_minute_lock() {
        let c = Config::default();
        assert_eq!(c.lock.idle_seconds, 600);
        assert_eq!(c.policy.lock_idle_max_seconds, 600);
        assert_eq!(c.policy.privileged_protocols, "shell");
        assert!(!c.trust_all_clients());
    }

    #[test]
    fn the_login_screen_is_known_by_its_shell_and_runs_no_x_server() {
        let desktop = Config::from_toml("[shell]\ncommand = [\"/usr/libexec/boswas/boswas-shell\"]\n").unwrap();
        assert!(!desktop.is_login_screen());
        assert!(desktop.runs_xwayland());
        let greeter =
            Config::from_toml("[shell]\ncommand = [\"/usr/libexec/boswas/boswas-shell\", \"--greeter\"]\n").unwrap();
        assert!(greeter.is_login_screen());
        assert!(greeter.xwayland.enabled, "the setting itself is untouched");
        assert!(!greeter.runs_xwayland());
        let off = Config::from_toml("[xwayland]\nenabled = false\n").unwrap();
        assert!(!off.runs_xwayland());
        let none = Config::from_toml("[shell]\ncommand = []\n").unwrap();
        assert!(!none.is_login_screen());
    }

    #[test]
    fn layers_merge_key_by_key() {
        let dir = tempfile::tempdir().unwrap();
        let sys = file(dir.path(), "sys.toml", "[keyboard]\nlayout = \"in\"\nrepeat_rate = 40\n");
        let user = file(dir.path(), "user.toml", "[keyboard]\nrepeat_rate = 20\n[output]\nscale = 1.5\n");
        let (c, warnings) = Config::load_from(&[&sys], Some(&user));
        assert!(warnings.is_empty(), "{warnings:?}");
        assert_eq!(c.keyboard.layout, "in");
        assert_eq!(c.keyboard.repeat_rate, 20);
        assert_eq!(c.output.scale, 1.5);
    }

    #[test]
    fn a_user_cannot_relax_the_lock_policy() {
        let dir = tempfile::tempdir().unwrap();
        let user = file(
            dir.path(),
            "user.toml",
            "[lock]\nidle_seconds = 0\n[policy]\nlock_idle_max_seconds = 0\nprivileged_protocols = \"all\"\n",
        );
        let (c, warnings) = Config::load_from(&[], Some(&user));
        assert_eq!(c.lock.idle_seconds, 600, "never-lock is clamped to the enforced maximum");
        assert_eq!(c.policy.lock_idle_max_seconds, 600);
        assert_eq!(c.policy.privileged_protocols, "shell");
        assert!(warnings.iter().any(|w| w.contains("[policy]")), "{warnings:?}");

        let user = file(dir.path(), "user2.toml", "[lock]\nidle_seconds = 120\n");
        let (c, _) = Config::load_from(&[], Some(&user));
        assert_eq!(c.lock.idle_seconds, 120, "a shorter timeout is the user's choice");
    }

    #[test]
    fn output_settings_are_validated_one_by_one() {
        let dir = tempfile::tempdir().unwrap();
        let user = file(
            dir.path(),
            "user.toml",
            "[outputs.eDP-1]\nscale = 1.5\nmode = \"1920x1080@60\"\nposition = [0, 0]\nrotation = \"90\"\n\
             [outputs.HDMI-A-1]\nenabled = false\nscale = 3.0\nmode = \"big\"\nrotation = \"sideways\"\nposition = [99999, 0]\n\
             [outputs.\"bad name!\"]\nscale = 1.0\n",
        );
        let (c, warnings) = Config::load_from(&[], Some(&user));
        let internal = &c.outputs["eDP-1"];
        assert_eq!(internal.scale, Some(1.5));
        assert_eq!(internal.mode.as_deref(), Some("1920x1080@60"));
        assert_eq!(internal.position, Some([0, 0]));
        assert_eq!(internal.rotation.as_deref(), Some("90"));
        let external = &c.outputs["HDMI-A-1"];
        assert_eq!(external.enabled, Some(false));
        assert_eq!((external.scale, external.mode.clone(), external.rotation.clone(), external.position), (None, None, None, None));
        assert!(!c.outputs.contains_key("bad name!"));
        assert_eq!(warnings.len(), 5, "{warnings:?}");
    }

    #[test]
    fn adaptive_sync_is_off_unless_asked_for_and_layers_like_the_other_output_keys() {
        assert_eq!(Config::default().outputs.get("DP-1").and_then(|o| o.adaptive_sync), None);
        let parsed = Config::from_toml("[outputs.DP-1]\nadaptive_sync = true\n").unwrap();
        assert_eq!(parsed.outputs["DP-1"].adaptive_sync, Some(true));
        assert_eq!(parsed.outputs["DP-1"].mode, None, "nothing else of the output is set by it");

        let dir = tempfile::tempdir().unwrap();
        // The administrator turns it on for one monitor; the user's file
        // changes another key of that monitor and turns it off on a second.
        let admin = file(dir.path(), "admin.toml", "[outputs.DP-1]\nadaptive_sync = true\n[outputs.DP-2]\nadaptive_sync = true\n");
        let user = file(dir.path(), "user.toml", "[outputs.DP-1]\nscale = 1.25\n[outputs.DP-2]\nadaptive_sync = false\n");
        let (c, warnings) = Config::load_from(&[&admin], Some(&user));
        assert!(warnings.is_empty(), "{warnings:?}");
        assert_eq!((c.outputs["DP-1"].adaptive_sync, c.outputs["DP-1"].scale), (Some(true), Some(1.25)));
        assert_eq!(c.outputs["DP-2"].adaptive_sync, Some(false), "the user's file has the last word");

        // A value that is not true or false rejects the file, as any wrong kind does.
        let wrong = file(dir.path(), "wrong.toml", "[outputs.DP-1]\nadaptive_sync = \"yes\"\nscale = 2.0\n");
        let (c, warnings) = Config::load_from(&[&admin], Some(&wrong));
        assert_eq!((c.outputs["DP-1"].adaptive_sync, c.outputs["DP-1"].scale), (Some(true), None));
        assert!(warnings.iter().any(|w| w.contains("wrong.toml")), "{warnings:?}");
    }

    #[test]
    fn modes_and_clock_times_parse_strictly() {
        assert_eq!(parse_mode("1920x1080"), Some((1920, 1080, None)));
        assert_eq!(parse_mode("2560x1440@59.951"), Some((2560, 1440, Some(59951))));
        for bad in ["", "1920", "1920x", "x1080", "0x0", "1920x1080@", "1920x1080@0", "1920x1080@fast", "99999x1"] {
            assert_eq!(parse_mode(bad), None, "{bad}");
        }
        assert_eq!(parse_clock("06:30"), Some(390));
        assert_eq!(parse_clock("23:59"), Some(1439));
        for bad in ["", "6:30", "24:00", "12:60", "noon", "12-30"] {
            assert_eq!(parse_clock(bad), None, "{bad}");
        }
    }

    #[test]
    fn night_light_follows_its_schedule() {
        let mut n = NightLight::default();
        assert!(!n.active_at(1380), "off unless enabled");
        n.enabled = true;
        assert!(n.active_at(720), "without a schedule it is simply on");
        n.scheduled = true;
        assert!(n.active_at(22 * 60));
        assert!(n.active_at(3 * 60), "the default schedule runs over midnight");
        assert!(!n.active_at(12 * 60));
        assert!(!n.active_at(6 * 60), "the end is exclusive");
        n.from = "08:00".into();
        n.to = "17:00".into();
        assert!(n.active_at(9 * 60) && !n.active_at(18 * 60) && !n.active_at(7 * 60));
    }

    #[test]
    fn input_and_power_settings_are_clamped() {
        let dir = tempfile::tempdir().unwrap();
        let user = file(
            dir.path(),
            "user.toml",
            "[pointer]\naccel_profile = \"warp\"\nleft_handed = true\n[touchpad]\naccel_speed = 9.0\ntap_to_click = false\n\
             [night_light]\ntemperature = 100\nfrom = \"late\"\n[power]\nscreen_off_seconds = 5\n[apps]\nsettings = [\"x\"]\n",
        );
        let (c, warnings) = Config::load_from(&[], Some(&user));
        assert_eq!(c.pointer.accel_profile, "adaptive");
        assert!(c.pointer.left_handed);
        assert_eq!(c.touchpad.accel_speed, 1.0);
        assert!(!c.touchpad.tap_to_click && c.touchpad.natural_scroll && c.touchpad.gestures);
        assert_eq!(c.night_light.temperature, 2500);
        assert_eq!(c.night_light.from, "20:00");
        assert_eq!(c.power.screen_off_seconds, 30);
        assert_eq!(c.apps.settings, ["x"]);
        assert_eq!(c.apps.task_manager, ["boswas-task-manager"]);
        assert_eq!(warnings.len(), 2, "{warnings:?}");
    }

    #[test]
    fn a_user_cannot_name_the_trusted_programs() {
        let dir = tempfile::tempdir().unwrap();
        let sys = file(
            dir.path(),
            "sys.toml",
            "[shell]\ncommand = [\"/usr/libexec/boswas/boswas-shell\"]\n[lock]\ncommand = [\"/usr/libexec/boswas/boswas-shell\", \"--lock\"]\n",
        );
        let user = file(
            dir.path(),
            "user.toml",
            "[shell]\ncommand = [\"/home/u/evil\"]\n[lock]\nidle_seconds = 60\ncommand = [\"/home/u/evil\", \"--lock\"]\n[input_method]\ncommand = [\"/home/u/keylogger\"]\n[apps]\nterminal = [\"foot\"]\n",
        );
        let (c, warnings) = Config::load_from(&[&sys], Some(&user));
        assert_eq!(c.shell.command, ["/usr/libexec/boswas/boswas-shell"]);
        assert_eq!(c.lock.command, ["/usr/libexec/boswas/boswas-shell", "--lock"]);
        assert_eq!(c.lock.idle_seconds, 60, "the rest of the table still applies");
        assert_eq!(c.apps.terminal, ["foot"], "ordinary programs stay the user's choice");
        assert!(warnings.iter().any(|w| w.contains("[shell] command")), "{warnings:?}");
        assert!(warnings.iter().any(|w| w.contains("[lock] command")), "{warnings:?}");
        assert!(c.input_method.command.is_empty(), "a user file cannot install an input method");
        assert!(warnings.iter().any(|w| w.contains("[input_method] command")), "{warnings:?}");
    }

    #[test]
    fn a_damaged_file_is_ignored_alone() {
        let dir = tempfile::tempdir().unwrap();
        let defaults = file(dir.path(), "defaults.toml", "[keyboard]\nlayout = \"in\"\n[lock]\nidle_seconds = 600\n");
        let admin = file(
            dir.path(),
            "admin.toml",
            "[policy]\nlock_idle_max_seconds = 300\n[lock]\nidle_seconds = 300\ncommand = [\"/usr/libexec/lock\"]\n",
        );
        // An unknown key: the user's file goes, the administrator's policy
        // stays (it must not fall back to the built-in ten minutes).
        let user = file(dir.path(), "user.toml", "[lock]\nidle_seconds = 200\n[keyboard]\nno_such_key = 1\n");
        let (c, warnings) = Config::load_from(&[&defaults, &admin], Some(&user));
        assert_eq!(c.policy.lock_idle_max_seconds, 300);
        assert_eq!(c.lock.idle_seconds, 300, "nothing of the damaged file applies");
        assert_eq!(c.lock.command, ["/usr/libexec/lock"]);
        assert_eq!(c.keyboard.layout, "in");
        assert_eq!(warnings.len(), 1, "{warnings:?}");
        assert!(warnings[0].contains("user.toml") && warnings[0].contains("no_such_key"), "{warnings:?}");

        // A value of the wrong kind is the same.
        let user = file(dir.path(), "user2.toml", "[keyboard]\nrepeat_rate = \"fast\"\nlayout = \"us\"\n");
        let (c, warnings) = Config::load_from(&[&defaults, &admin], Some(&user));
        assert_eq!(c.keyboard.layout, "in");
        assert_eq!(c.policy.lock_idle_max_seconds, 300);
        assert!(warnings.iter().any(|w| w.contains("user2.toml")), "{warnings:?}");

        // A damaged administrator file does not take the shipped defaults
        // or the user's own settings with it.
        let broken_admin = file(dir.path(), "admin2.toml", "[policy]\nlock_idle_max_seconds = \"never\"\n");
        let user = file(dir.path(), "user3.toml", "[lock]\nidle_seconds = 120\n");
        let (c, warnings) = Config::load_from(&[&defaults, &broken_admin], Some(&user));
        assert_eq!(c.keyboard.layout, "in");
        assert_eq!(c.lock.idle_seconds, 120);
        assert_eq!(c.policy.lock_idle_max_seconds, 600, "the built-in policy");
        assert!(warnings.iter().any(|w| w.contains("admin2.toml")), "{warnings:?}");

        // The preset file too: a wrong value there leaves the rest alone.
        let user = file(dir.path(), "user4.toml", "[appearance]\nscheme = \"light\"\n[lock]\nidle_seconds = 100\n");
        let look = file(dir.path(), "appearance.toml", "[appearance]\nreduced_motion = \"yes\"\n");
        let (c, warnings) = Config::load_layers(&[&defaults, &admin], Some(&user), Some(&look));
        assert_eq!(c.appearance.scheme, "light");
        assert_eq!(c.lock.idle_seconds, 100);
        assert_eq!(c.policy.lock_idle_max_seconds, 300);
        assert!(warnings.iter().any(|w| w.contains("appearance.toml")), "{warnings:?}");
    }

    #[test]
    fn a_user_cannot_turn_x11_back_on() {
        let dir = tempfile::tempdir().unwrap();
        let admin = file(dir.path(), "admin.toml", "[xwayland]\nenabled = false\n");
        let user = file(dir.path(), "user.toml", "[xwayland]\nenabled = true\n");
        let (c, warnings) = Config::load_from(&[&admin], Some(&user));
        assert!(!c.xwayland.enabled);
        assert!(warnings.iter().any(|w| w.contains("[xwayland]")), "{warnings:?}");
        // Turning it off for oneself is the user's choice.
        let user_off = file(dir.path(), "user2.toml", "[xwayland]\nenabled = false\n");
        let (c, warnings) = Config::load_from(&[], Some(&user_off));
        assert!(!c.xwayland.enabled);
        assert!(warnings.is_empty(), "{warnings:?}");
        // Without a word from the administrator it is on, and stays the user's.
        let (c, _) = Config::load_from(&[], None);
        assert!(c.xwayland.enabled);
    }

    #[test]
    fn an_administrator_may_change_the_policy() {
        let dir = tempfile::tempdir().unwrap();
        let sys = file(dir.path(), "sys.toml", "[policy]\nlock_idle_max_seconds = 0\n[lock]\nidle_seconds = 0\n");
        let (c, warnings) = Config::load_from(&[&sys], None);
        assert!(warnings.is_empty(), "{warnings:?}");
        assert_eq!(c.lock.idle_seconds, 0);
    }

    #[test]
    fn the_preset_file_only_changes_the_look() {
        let dir = tempfile::tempdir().unwrap();
        let user = file(dir.path(), "user.toml", "[appearance]\nscheme = \"dark\"\n[keyboard]\nlayout = \"in\"\n");
        let look = file(dir.path(), "appearance.toml", "[appearance]\nscheme = \"light\"\nwallpaper = \"/usr/share/w.png\"\n");
        let (c, warnings) = Config::load_layers(&[], Some(&user), Some(&look));
        assert!(warnings.is_empty(), "{warnings:?}");
        assert_eq!(c.appearance.scheme, "light");
        assert_eq!(c.appearance.wallpaper, "/usr/share/w.png");
        assert_eq!(c.keyboard.layout, "in");

        let sneaky = file(
            dir.path(),
            "sneaky.toml",
            "[appearance]\nscheme = \"light\"\n[lock]\nidle_seconds = 0\n[policy]\nprivileged_protocols = \"all\"\n",
        );
        let (c, warnings) = Config::load_layers(&[], Some(&user), Some(&sneaky));
        assert_eq!(c.appearance.scheme, "dark", "a preset file with other tables is ignored as a whole");
        assert_eq!(c.lock.idle_seconds, 600);
        assert_eq!(c.policy.privileged_protocols, "shell");
        assert_eq!(warnings.len(), 1);
    }

    #[test]
    fn broken_files_fall_back_to_defaults() {
        let dir = tempfile::tempdir().unwrap();
        let bad = file(dir.path(), "bad.toml", "this is not = = toml");
        let unknown = file(dir.path(), "unknown.toml", "[keyboard]\nlayuot = \"us\"\n");
        let (c, warnings) = Config::load_from(&[&bad], None);
        assert_eq!(c, Config::default());
        assert_eq!(warnings.len(), 1);
        let (c, warnings) = Config::load_from(&[&unknown], None);
        assert_eq!(c, Config::default(), "an unknown key rejects the file instead of being ignored");
        assert_eq!(warnings.len(), 1);
        let (c, warnings) = Config::load_from(&[&dir.path().join("missing.toml")], None);
        assert_eq!(c, Config::default());
        assert!(warnings.is_empty());
    }

    #[test]
    fn the_windows_like_settings_have_their_defaults() {
        let c = Config::default();
        assert_eq!((c.appearance.accent.as_str(), c.appearance.transparency), ("iris", true));
        assert_eq!((c.accessibility.text_scale, c.accessibility.always_show_scrollbars), (100, false));
        assert_eq!((c.color_filter.enabled, c.color_filter.filter.as_str(), c.color_filter.shortcut), (false, "grayscale", false));
        assert_eq!((c.magnifier.enabled, c.magnifier.zoom, c.magnifier.step), (false, 200, 100));
        assert_eq!((c.taskbar.alignment.as_str(), c.taskbar.auto_hide, c.taskbar.task_view, c.taskbar.clock_seconds), ("center", false, true, false));
        assert_eq!((c.start.layout.as_str(), c.start.recently_added, c.start.most_used), ("default", true, true));
        assert_eq!(c.start.folders, ["settings", "files"]);
        assert!(c.multitasking.snap_windows && c.multitasking.snap_layouts_on_maximize && c.multitasking.snap_layouts_on_drag);
        assert_eq!((c.multitasking.taskbar_windows.as_str(), c.multitasking.alt_tab_windows.as_str()), ("desktop", "desktop"));
        assert!(!c.notifications.do_not_disturb && !c.notifications.do_not_disturb_scheduled);
        assert_eq!(c.notifications.toast_seconds, 5);
        assert!(!c.keyboard.sticky_keys && !c.keyboard.filter_keys && c.keyboard.sticky_keys_lock);
        // The shipped file says the same as the built-in values.
        let shipped = Config::from_toml(include_str!("../../../config/compositor.toml")).unwrap();
        assert_eq!(shipped.taskbar, c.taskbar);
        assert_eq!(shipped.start, c.start);
        assert_eq!(shipped.multitasking, c.multitasking);
        assert_eq!(shipped.notifications, c.notifications);
        assert_eq!(shipped.accessibility, c.accessibility);
        assert_eq!(shipped.color_filter, c.color_filter);
        assert_eq!(shipped.magnifier, c.magnifier);
    }

    #[test]
    fn the_windows_like_settings_are_checked() {
        let mut warnings = Vec::new();
        let mut c: Config = toml::from_str(
            "[appearance]\naccent = \"teal\"\n[accessibility]\ntext_scale = 400\n[color_filter]\nfilter = \"sepia\"\n\
             [magnifier]\nzoom = 5000\nstep = 30\n[taskbar]\nalignment = \"right\"\n[start]\nlayout = \"huge\"\n\
             folders = [\"music\", \"/etc\", \"music\", \"videos\"]\n[multitasking]\ntaskbar_windows = \"some\"\n\
             [notifications]\ndo_not_disturb_from = \"9pm\"\nbanners_off = [\"Mail\", \"Mail\", \"\", \"a\\u0007b\", \"Chat\"]\n\
             toast_seconds = 8\n[keyboard]\nslow_keys_ms = 5\nbounce_keys_ms = 0\n",
        )
        .unwrap();
        c.sanitize(&mut warnings);
        assert_eq!(c.appearance.accent, "iris");
        assert_eq!(c.accessibility.text_scale, 225);
        assert_eq!(c.color_filter.filter, "grayscale");
        assert_eq!((c.magnifier.zoom, c.magnifier.step), (1600, 100));
        assert_eq!(c.taskbar.alignment, "center");
        assert_eq!(c.start.layout, "default");
        assert_eq!(c.start.folders, ["music", "videos"]);
        assert_eq!(c.multitasking.taskbar_windows, "desktop");
        assert_eq!(c.notifications.do_not_disturb_from, "22:00");
        assert_eq!(c.notifications.banners_off, ["Mail", "Chat"]);
        assert_eq!(c.notifications.toast_seconds, 7, "the nearest time offered");
        assert_eq!((c.keyboard.slow_keys_ms, c.keyboard.bounce_keys_ms), (100, 0));
        assert_eq!(warnings.len(), 9, "{warnings:?}");
        // An unknown key in one of the new tables rejects the file, as anywhere.
        assert!(Config::from_toml("[taskbar]\nposition = \"top\"\n").is_err());
        assert!(Config::from_toml("[accessibility]\ntext_scale = \"big\"\n").is_err());
    }

    #[test]
    fn the_accents_are_the_hues_the_design_system_derives() {
        assert_eq!(ACCENTS, boswas_ui::tokens::ACCENT_HUES);
    }

    #[test]
    fn clock_spans_run_over_midnight() {
        assert!(clock_span_contains("22:00", "07:00", 23 * 60));
        assert!(clock_span_contains("22:00", "07:00", 60));
        assert!(!clock_span_contains("22:00", "07:00", 7 * 60));
        assert!(clock_span_contains("09:00", "17:00", 9 * 60));
        assert!(!clock_span_contains("09:00", "17:00", 17 * 60));
        assert!(!clock_span_contains("09:00", "09:00", 9 * 60), "an empty span");
        assert!(!clock_span_contains("x", "07:00", 60));
    }

    #[test]
    fn values_are_clamped() {
        let c = Config::from_toml(
            "[keyboard]\nrepeat_delay = 1\nrepeat_rate = 100000\n[pointer]\naccel_speed = 9.0\ncursor_size = 1\n\
             [output]\nscale = 3.3\n[appearance]\nscheme = \"neon\"\n",
        )
        .unwrap();
        assert_eq!(c.keyboard.repeat_delay, 100);
        assert_eq!(c.keyboard.repeat_rate, 100);
        assert_eq!(c.pointer.accel_speed, 1.0);
        assert_eq!(c.pointer.cursor_size, 16);
        assert_eq!(c.output.scale, 1.0);
        assert_eq!(c.appearance.scheme, "dark");
    }
}
