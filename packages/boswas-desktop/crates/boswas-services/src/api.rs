//! The contract between the shell and the system services.
//!
//! The shell sends [`Command`]s and receives [`Event`]s. Every event that
//! describes a service carries that service's *whole* current state, so the
//! shell never has to merge deltas: it replaces what it shows.
//!
//! Nothing here blocks the shell: commands are queued, and a service that is
//! missing (no NetworkManager, no BlueZ, no battery) reports
//! `available: false` or `None` instead of failing.

use zeroize::Zeroizing;

/// A secret typed by the user (a Wi-Fi passphrase, a password). Wiped from
/// memory when dropped; never logged (its `Debug` prints nothing of it).
#[derive(Clone, PartialEq, Eq)]
pub struct Secret(Zeroizing<String>);

impl Secret {
    pub fn new(text: String) -> Self {
        Self(Zeroizing::new(text))
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Secret(..)")
    }
}

// --- network ----------------------------------------------------------------

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct NetworkState {
    /// NetworkManager is running and answering.
    pub available: bool,
    /// What the taskbar icon shows.
    pub connectivity: Connectivity,
    /// A wired device is connected.
    pub wired: bool,
    /// There is a Wi-Fi device.
    pub wifi_present: bool,
    pub wifi_enabled: bool,
    /// The connected access point, if any.
    pub wifi_connected: Option<String>,
    /// A scan is running.
    pub wifi_scanning: bool,
    /// Visible networks, strongest first, one entry per SSID.
    pub access_points: Vec<AccessPoint>,
    /// A VPN connection is active.
    pub vpn: bool,
    /// Airplane mode: every radio is off.
    pub airplane: bool,
    /// The last failed connection attempt (wrong passphrase, timeout), for
    /// the shell to show once.
    pub error: Option<String>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Connectivity {
    /// No connection.
    #[default]
    None,
    /// Connected to a network without internet access.
    Limited,
    /// A login page is in the way.
    Portal,
    /// Internet access.
    Full,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AccessPoint {
    pub ssid: String,
    /// 0 to 100.
    pub strength: u8,
    /// The network needs a passphrase (or other credentials).
    pub secured: bool,
    /// NetworkManager already has a profile for it, so connecting needs no
    /// passphrase.
    pub known: bool,
    pub connected: bool,
    pub connecting: bool,
}

// --- Bluetooth --------------------------------------------------------------

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct BluetoothState {
    /// BlueZ is running and there is an adapter.
    pub available: bool,
    pub enabled: bool,
    pub discovering: bool,
    /// Paired devices first, then discovered ones, each by name.
    pub devices: Vec<BluetoothDevice>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BluetoothDevice {
    /// `AA:BB:CC:DD:EE:FF`.
    pub address: String,
    pub name: String,
    /// The freedesktop icon name BlueZ suggests (`audio-headphones`,
    /// `input-keyboard`, ...), or empty.
    pub icon: String,
    pub paired: bool,
    pub connected: bool,
    pub connecting: bool,
    /// Battery level 0 to 100, when the device reports one.
    pub battery: Option<u8>,
}

// --- audio ------------------------------------------------------------------

#[derive(Clone, Debug, Default, PartialEq)]
pub struct AudioState {
    /// The sound server answers.
    pub available: bool,
    /// The default output, if any.
    pub output: Option<AudioDevice>,
    /// The default input, if any.
    pub input: Option<AudioDevice>,
    pub outputs: Vec<AudioDevice>,
    pub inputs: Vec<AudioDevice>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct AudioDevice {
    /// The sound server's id of the device.
    pub id: u32,
    /// A name for people ("Speakers", "USB headset").
    pub name: String,
    /// 0.0 to 1.5 (above 1.0 is amplified).
    pub volume: f32,
    pub muted: bool,
    pub is_default: bool,
}

// --- power ------------------------------------------------------------------

#[derive(Clone, Debug, Default, PartialEq)]
pub struct PowerState {
    pub battery: Option<Battery>,
    /// Running on external power.
    pub on_ac: bool,
    /// The active power profile, when power-profiles-daemon is there.
    pub profile: Option<PowerProfile>,
    /// The profiles that can be chosen.
    pub profiles: Vec<PowerProfile>,
    /// What logind lets this session do.
    pub can_suspend: bool,
    pub can_hibernate: bool,
    pub can_reboot: bool,
    pub can_power_off: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Battery {
    /// 0 to 100.
    pub percent: u8,
    pub charging: bool,
    pub full: bool,
    /// Seconds until empty (discharging) or full (charging), when known.
    pub seconds_left: Option<u64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PowerProfile {
    PowerSaver,
    Balanced,
    Performance,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PowerAction {
    Suspend,
    Hibernate,
    Reboot,
    PowerOff,
    /// End this session (back to the login screen).
    LogOut,
}

/// The backlight of the built-in display.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Brightness {
    /// 0.0 to 1.0.
    pub level: f32,
}

// --- notifications ----------------------------------------------------------

#[derive(Clone, Debug, PartialEq)]
pub struct Notification {
    pub id: u32,
    /// The application's name as it gave it.
    pub app_name: String,
    /// A freedesktop icon name or an absolute file path; may be empty.
    pub app_icon: String,
    /// The `desktop-entry` hint (an application id), when given.
    pub desktop_entry: Option<String>,
    pub summary: String,
    /// Plain text: markup is removed by the service.
    pub body: String,
    /// (action key, label) pairs; the key `default` is the body click.
    pub actions: Vec<(String, String)>,
    pub urgency: Urgency,
    /// Milliseconds the toast stays; `None` leaves it to the shell's default.
    /// Critical notifications stay until dismissed.
    pub timeout_ms: Option<u32>,
    /// The notification asked not to be kept in the notification centre.
    pub transient: bool,
    /// A progress value 0 to 100 (the `value` hint), when given.
    pub progress: Option<u8>,
    /// Seconds since the Unix epoch when it arrived.
    pub time: u64,
    /// It replaces the notification with the same id that is already shown.
    pub replaces_existing: bool,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub enum Urgency {
    Low,
    #[default]
    Normal,
    Critical,
}

// --- tray -------------------------------------------------------------------

/// A StatusNotifierItem.
#[derive(Clone, Debug, PartialEq)]
pub struct TrayItem {
    /// Stable key of the item (bus name and object path).
    pub key: String,
    pub id: String,
    pub title: String,
    /// A freedesktop icon name, when the item gives one.
    pub icon_name: String,
    /// Extra icon theme directory the item ships (`IconThemePath`).
    pub icon_theme_path: String,
    /// The item's own pixmap (straight RGBA), when it gives no icon name.
    pub icon_pixmap: Option<Pixmap>,
    pub tooltip: String,
    pub status: TrayStatus,
    /// The item has a menu the shell can ask for.
    pub has_menu: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Pixmap {
    pub width: u32,
    pub height: u32,
    /// `width * height * 4` bytes, straight (not premultiplied) RGBA.
    pub rgba: Vec<u8>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum TrayStatus {
    Passive,
    #[default]
    Active,
    NeedsAttention,
}

/// One entry of a tray item's menu (com.canonical.dbusmenu).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MenuEntry {
    pub id: i32,
    /// The label without mnemonic underscores.
    pub label: String,
    pub enabled: bool,
    pub separator: bool,
    pub toggle: MenuToggle,
    pub icon_name: String,
    pub children: Vec<MenuEntry>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum MenuToggle {
    #[default]
    None,
    Checkmark(bool),
    Radio(bool),
}

// --- media ------------------------------------------------------------------

/// An MPRIS media player.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MediaPlayer {
    /// The player's bus name.
    pub key: String,
    /// The player's name for people.
    pub identity: String,
    pub desktop_entry: Option<String>,
    pub title: String,
    pub artist: String,
    /// A local file path of the cover art, when the player gives one.
    pub art_path: Option<String>,
    pub playing: bool,
    pub can_play_pause: bool,
    pub can_next: bool,
    pub can_previous: bool,
}

// --- session and authentication -----------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SessionEvent {
    /// logind asks this session to lock (`loginctl lock-session`).
    Lock,
    /// logind asks this session to unlock.
    Unlock,
    /// The machine is about to sleep (`true`) or has woken up (`false`).
    PrepareForSleep(bool),
}

/// polkit asks the user to authenticate for an action.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AuthRequest {
    /// Identifies the request in [`Command::AuthRespond`] and
    /// [`Command::AuthCancel`].
    pub cookie: String,
    pub action_id: String,
    /// polkit's message for people ("Authentication is required to ...").
    pub message: String,
    pub icon_name: String,
    /// The users who may authenticate, the session's own user first when
    /// it is among them.
    pub users: Vec<String>,
}

/// How an authentication attempt went.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AuthProgress {
    /// The helper asks for input; `echo` is false for passwords.
    Prompt { text: String, echo: bool },
    /// A message from PAM to show ("Place your finger on the reader").
    Info(String),
    /// The attempt failed; the request stays open for another attempt.
    Failed(String),
    /// Authenticated, or the request went away: close the dialog.
    Done { authorized: bool },
}

// --- the two directions --------------------------------------------------------

/// Which services [`Command::Refresh`] re-reads.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Service {
    Network,
    Bluetooth,
    Audio,
    Power,
    Brightness,
    Tray,
    Media,
}

/// From the services to the shell.
#[derive(Clone, Debug, PartialEq)]
pub enum Event {
    Network(NetworkState),
    Bluetooth(BluetoothState),
    Audio(AudioState),
    Power(PowerState),
    /// `None`: there is no backlight to control.
    Brightness(Option<Brightness>),
    /// A notification arrived or replaced an earlier one.
    Notify(Notification),
    /// A notification was closed by its application or expired at the
    /// service; the shell removes its toast (and its entry, if the
    /// application withdrew it).
    NotificationClosed { id: u32, withdrawn: bool },
    /// All tray items, in the order they registered.
    Tray(Vec<TrayItem>),
    /// The menu asked for with [`Command::TrayMenu`].
    TrayMenu { key: String, entries: Vec<MenuEntry> },
    /// All media players, the most recently active first.
    Media(Vec<MediaPlayer>),
    Session(SessionEvent),
    AuthRequest(AuthRequest),
    AuthProgress { cookie: String, progress: AuthProgress },
}

/// From the shell to the services.
#[derive(Clone, Debug, PartialEq)]
pub enum Command {
    /// Re-read a service and send its state again.
    Refresh(Service),

    WifiSetEnabled(bool),
    WifiScan,
    /// Connect to a visible network; `passphrase` is needed for a secured
    /// network NetworkManager has no profile for.
    WifiConnect { ssid: String, passphrase: Option<Secret> },
    WifiDisconnect,
    /// Forget the saved profile of a network.
    WifiForget { ssid: String },
    AirplaneMode(bool),

    BluetoothSetEnabled(bool),
    BluetoothDiscover(bool),
    /// Pair if needed, then connect.
    BluetoothConnect { address: String },
    BluetoothDisconnect { address: String },
    BluetoothForget { address: String },

    /// Volume of the default output, 0.0 to 1.5.
    SetVolume(f32),
    SetMuted(bool),
    /// Volume of the default input.
    SetInputVolume(f32),
    SetInputMuted(bool),
    SetDefaultOutput { id: u32 },
    SetDefaultInput { id: u32 },

    /// 0.0 to 1.0.
    SetBrightness(f32),
    SetPowerProfile(PowerProfile),
    Power(PowerAction),

    /// The user chose an action of a notification (`default` for its body).
    NotificationAction { id: u32, action: String },
    /// The user dismissed a notification.
    NotificationDismiss { id: u32 },
    /// While set, the service still records notifications but tells the
    /// shell nothing except critical ones... the shell decides what to show;
    /// the service reports the flag to applications (`Inhibited`).
    SetDoNotDisturb(bool),

    /// Primary click on a tray item at global logical coordinates.
    TrayActivate { key: String, x: i32, y: i32 },
    /// Middle click.
    TraySecondaryActivate { key: String, x: i32, y: i32 },
    /// Scroll over a tray item; `delta` in wheel steps.
    TrayScroll { key: String, delta: i32, horizontal: bool },
    /// Ask for the item's menu; answered with [`Event::TrayMenu`].
    TrayMenu { key: String },
    /// The user chose a menu entry.
    TrayMenuActivate { key: String, id: i32 },

    MediaPlayPause { key: String },
    MediaNext { key: String },
    MediaPrevious { key: String },

    /// Answer the current prompt of an authentication request.
    AuthRespond { cookie: String, response: Secret },
    AuthCancel { cookie: String },
}
