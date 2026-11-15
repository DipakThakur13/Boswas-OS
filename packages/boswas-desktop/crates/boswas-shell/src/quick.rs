//! Quick settings: the toggles, the brightness and volume sliders, media
//! controls, and the pages for Wi-Fi networks, Bluetooth devices and audio
//! outputs.

use boswas_services::{AccessPoint, BluetoothDevice, PowerProfile};
use boswas_ui::tokens::tokens;

use crate::{
    action::Action,
    glyph::Glyph,
    state::State,
    svc::{profile_name, wifi_icon},
    ui::{ramp, ButtonKind, Frame, Rect, TextField},
};

#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    WifiToggle,
    WifiPage,
    BluetoothToggle,
    BluetoothPage,
    Airplane,
    NightLight,
    Profile,
    Dnd,
    Brightness,
    Volume,
    Mute,
    OutputsPage,
    Back,
    /// Opens a page of Boswas Control Center.
    Settings(&'static str),
    MediaPrevious(usize),
    MediaPlayPause(usize),
    MediaNext(usize),
    /// Expands the nth visible network.
    Network(usize),
    PassphraseField,
    Reveal,
    Connect,
    Cancel,
    Disconnect,
    Forget,
    Scan,
    /// Expands the nth Bluetooth device.
    Device(usize),
    DeviceConnect,
    DeviceDisconnect,
    DeviceForget,
    /// Makes an audio output the default.
    Output(u32),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Page {
    #[default]
    Main,
    Wifi,
    Bluetooth,
    Outputs,
}

#[derive(Default)]
pub struct Quick {
    pub page: Page,
    /// The network whose row is expanded, by SSID.
    pub network: Option<String>,
    pub passphrase: TextField,
    pub reveal: bool,
    /// Keys go to the passphrase field.
    pub editing: bool,
    /// The Bluetooth device whose row is expanded, by address.
    pub device: Option<String>,
    /// First row shown of the page's list.
    pub scroll: usize,
}

impl Quick {
    /// Returns to the main page and forgets what was typed.
    pub fn reset(&mut self) {
        self.page = Page::Main;
        self.collapse();
        self.scroll = 0;
    }

    /// Closes the expanded row and wipes the passphrase.
    pub fn collapse(&mut self) {
        self.network = None;
        self.device = None;
        self.passphrase.clear();
        self.reveal = false;
        self.editing = false;
    }

    pub fn open_page(&mut self, page: Page) {
        self.collapse();
        self.page = page;
        self.scroll = 0;
    }

    /// Expands a network's row; a secured network without a saved profile
    /// gets the passphrase field and the keyboard.
    pub fn select_network(&mut self, ap: &AccessPoint) {
        let same = self.network.as_deref() == Some(ap.ssid.as_str());
        self.collapse();
        if !same {
            self.network = Some(ap.ssid.clone());
            self.editing = needs_passphrase(ap);
        }
    }
}

/// Whether connecting to this network needs a passphrase typed now.
pub fn needs_passphrase(ap: &AccessPoint) -> bool {
    ap.secured && !ap.known && !ap.connected
}

const PAD: f32 = 20.0;
const TILE_H: f32 = 48.0;
const TILE_ROW: f32 = 80.0;
const SLIDER_ROW: f32 = 44.0;
const FOOTER_H: f32 = 48.0;
const MEDIA_H: f32 = 72.0;
const LIST_ROW: f32 = 48.0;
const PAGE_H: f32 = 484.0;
/// Media players shown.
const MAX_PLAYERS: usize = 2;

/// Height of the main page for the current state.
pub fn main_height(st: &State) -> f32 {
    let media = st.sys.media.len().min(MAX_PLAYERS) as f32 * (MEDIA_H + 8.0);
    let brightness = if st.sys.brightness.is_some() { SLIDER_ROW } else { 0.0 };
    PAD + media + 2.0 * TILE_ROW + 4.0 + brightness + SLIDER_ROW + 12.0 + FOOTER_H
}

/// The size of the panel for the current page.
pub fn panel_size(st: &State, output: (f32, f32)) -> (f32, f32) {
    let t = tokens();
    let room = output.1 - t.shell("taskbar-height") - 2.0 * t.shell("flyout-margin");
    let height = match st.quick.page {
        Page::Main => main_height(st),
        _ => PAGE_H,
    };
    (t.shell("quick-settings-width").min(output.0 - 24.0), height.min(room).max(200.0))
}

pub fn draw(f: &mut Frame, st: &State, panel: Rect) {
    f.panel(panel, "acrylic");
    f.area(panel, Action::None);
    match st.quick.page {
        Page::Main => draw_main(f, st, panel),
        Page::Wifi => draw_wifi(f, st, panel),
        Page::Bluetooth => draw_bluetooth(f, st, panel),
        Page::Outputs => draw_outputs(f, st, panel),
    }
}

/// What a toggle tile shows.
struct Tile<'a> {
    on: bool,
    enabled: bool,
    icon: Option<&'a str>,
    glyph: Option<Glyph>,
    label: &'a str,
    toggle: Act,
    /// The right part opens a page.
    more: Option<Act>,
}

fn tile(f: &mut Frame, rect: Rect, tile: Tile) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    let button = Rect::new(rect.x, rect.y, rect.w, TILE_H);
    let split = tile.more.is_some();
    let left = if split { Rect::new(button.x, button.y, button.w / 2.0, TILE_H) } else { button };
    let touch = if tile.enabled { f.hit(left, Action::Quick(tile.toggle)) } else { Default::default() };
    let more_touch = match (&tile.more, tile.enabled) {
        (Some(more), true) => {
            let right = Rect::new(button.x + button.w / 2.0, button.y, button.w / 2.0, TILE_H);
            let touch = f.hit(right, Action::Quick(more.clone()));
            f.tip(&format!("Manage {}", tile.label));
            touch
        }
        _ => Default::default(),
    };
    let ink = if tile.on && tile.enabled { theme.on_accent } else if tile.enabled { theme.text } else { theme.text_muted };
    if tile.on && tile.enabled {
        f.canvas.fill_rounded_rect(button.x, button.y, button.w, button.h, radius, theme.accent);
        for (rect, t) in [(left, touch), (Rect::new(button.x + button.w / 2.0, button.y, button.w / 2.0, TILE_H), more_touch)] {
            if t.hovered {
                f.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, theme.accent_hover.alpha(0.55));
            }
        }
    } else {
        let fill = f.res.fill(if tile.enabled { "control" } else { "disabled" });
        let stroke = f.res.stroke("control");
        f.canvas.fill_rounded_rect(button.x, button.y, button.w, button.h, radius, fill);
        f.canvas.stroke_rounded_rect(button.x, button.y, button.w, button.h, radius, 1.0, stroke);
        for (rect, t) in [(left, touch), (Rect::new(button.x + button.w / 2.0, button.y, button.w / 2.0, TILE_H), more_touch)] {
            if t.hovered {
                let hover = f.res.fill("subtle-hover");
                f.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, hover);
            }
        }
    }
    if touch.focused {
        f.focus_ring(left, radius);
    }
    if more_touch.focused {
        f.focus_ring(Rect::new(button.x + button.w / 2.0, button.y, button.w / 2.0, TILE_H), radius);
    }
    let (cx, cy) = left.center();
    let mark = Rect::new(cx - 8.0, cy - 8.0, 16.0, 16.0);
    match (tile.icon, tile.glyph) {
        (Some(icon), _) => {
            f.icon_tinted(icon, mark.x, mark.y, 16.0, ink);
        }
        (None, Some(glyph)) => f.glyph(glyph, mark, ink),
        _ => {}
    }
    if split {
        f.canvas.fill_rect(button.x + button.w / 2.0, button.y + 10.0, 1.0, TILE_H - 20.0, ink.alpha(0.25));
        f.glyph(Glyph::ChevronRight, Rect::new(button.x + button.w * 0.75 - 6.0, cy - 6.0, 12.0, 12.0), ink);
    }
    let label = ramp("caption", if tile.enabled { theme.text } else { theme.text_muted });
    f.text_centered(tile.label, Rect::new(rect.x - 2.0, rect.y + TILE_H + 6.0, rect.w + 4.0, 18.0), &label);
}

fn draw_main(f: &mut Frame, st: &State, panel: Rect) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    let sys = &st.sys;
    let x = panel.x + PAD;
    let w = panel.w - 2.0 * PAD;
    let mut y = panel.y + PAD;

    // Media players.
    for (index, player) in sys.media.iter().take(MAX_PLAYERS).enumerate() {
        let card = Rect::new(x, y, w, MEDIA_H);
        f.card(card, radius);
        let controls = 3.0 * 36.0;
        let title = if player.title.is_empty() { &player.identity } else { &player.title };
        f.text_left(title, Rect::new(card.x + 14.0, card.y + 14.0, w - controls - 30.0, 22.0), &ramp("body-strong", theme.text));
        let artist = if player.artist.is_empty() { &player.identity } else { &player.artist };
        f.text_left(artist, Rect::new(card.x + 14.0, card.y + 36.0, w - controls - 30.0, 20.0), &ramp("caption", theme.text_secondary));
        let bx = card.right() - controls - 8.0;
        let by = card.y + (MEDIA_H - 32.0) / 2.0;
        if player.can_previous {
            f.glyph_button(Rect::new(bx, by, 32.0, 32.0), Glyph::Previous, "Previous", Action::Quick(Act::MediaPrevious(index)));
        }
        if player.can_play_pause {
            let glyph = if player.playing { Glyph::Pause } else { Glyph::Play };
            f.glyph_button(Rect::new(bx + 36.0, by, 32.0, 32.0), glyph, if player.playing { "Pause" } else { "Play" }, Action::Quick(Act::MediaPlayPause(index)));
        }
        if player.can_next {
            f.glyph_button(Rect::new(bx + 72.0, by, 32.0, 32.0), Glyph::Next, "Next", Action::Quick(Act::MediaNext(index)));
        }
        y += MEDIA_H + 8.0;
    }

    // Toggle tiles, three to a row.
    let gap = 10.0;
    let tile_w = (w - 2.0 * gap) / 3.0;
    let cell = |column: usize, row: usize| Rect::new(x + column as f32 * (tile_w + gap), y + row as f32 * TILE_ROW, tile_w, TILE_ROW);
    let net = &sys.network;
    let wifi_ok = net.available && net.wifi_present;
    let wifi_label = match (&net.wifi_connected, wifi_ok) {
        (_, false) => "Wi-Fi",
        (Some(ssid), true) => ssid.as_str(),
        (None, true) => "Wi-Fi",
    };
    tile(f, cell(0, 0), Tile {
        on: net.wifi_enabled && !net.airplane,
        enabled: wifi_ok,
        icon: Some(if net.wifi_enabled { "network-wireless-signal-excellent-symbolic" } else { "network-wireless-offline-symbolic" }),
        glyph: None,
        label: wifi_label,
        toggle: Act::WifiToggle,
        more: Some(Act::WifiPage),
    });
    let bt = &sys.bluetooth;
    let connected = bt.devices.iter().find(|d| d.connected).map(|d| d.name.as_str());
    tile(f, cell(1, 0), Tile {
        on: bt.enabled,
        enabled: bt.available,
        icon: Some(if bt.enabled { "bluetooth-active-symbolic" } else { "bluetooth-disabled-symbolic" }),
        glyph: None,
        label: connected.unwrap_or("Bluetooth"),
        toggle: Act::BluetoothToggle,
        more: Some(Act::BluetoothPage),
    });
    tile(f, cell(2, 0), Tile {
        on: net.airplane,
        enabled: net.available,
        icon: Some("airplane-mode-symbolic"),
        glyph: None,
        label: "Airplane mode",
        toggle: Act::Airplane,
        more: None,
    });
    tile(f, cell(0, 1), Tile {
        on: st.night_light,
        enabled: true,
        icon: Some("night-light-symbolic"),
        glyph: None,
        label: "Night light",
        toggle: Act::NightLight,
        more: None,
    });
    let profile = sys.power.profile;
    tile(f, cell(1, 1), Tile {
        on: matches!(profile, Some(PowerProfile::PowerSaver | PowerProfile::Performance)),
        enabled: profile.is_some() && sys.power.profiles.len() > 1,
        icon: None,
        glyph: Some(match profile {
            Some(PowerProfile::PowerSaver) => Glyph::Leaf,
            Some(PowerProfile::Performance) => Glyph::Bolt,
            _ => Glyph::Gauge,
        }),
        label: profile.map_or("Power mode", profile_name),
        toggle: Act::Profile,
        more: None,
    });
    tile(f, cell(2, 1), Tile {
        on: st.notes.dnd,
        enabled: true,
        icon: Some("notification-disabled-symbolic"),
        glyph: None,
        label: "Do not disturb",
        toggle: Act::Dnd,
        more: None,
    });
    y += 2.0 * TILE_ROW + 4.0;

    // Sliders.
    if let Some(brightness) = sys.brightness {
        f.icon("display-brightness-symbolic", x + 8.0, y + (SLIDER_ROW - 16.0) / 2.0, 16.0);
        f.slider(Rect::new(x + 36.0, y + 6.0, w - 36.0 - 36.0, 32.0), brightness.level, true, Action::Quick(Act::Brightness));
        f.tip("Brightness");
        y += SLIDER_ROW;
    }
    let output = sys.audio.output.as_ref();
    let mute = Rect::new(x, y + 6.0, 32.0, 32.0);
    if output.is_some() {
        f.icon_button(mute, sys.volume_icon(), if output.is_some_and(|o| o.muted) { "Unmute" } else { "Mute" }, Action::Quick(Act::Mute));
    } else {
        f.icon("audio-volume-muted-symbolic", mute.x + 8.0, mute.y + 8.0, 16.0);
    }
    let value = output.map_or(0.0, |o| if o.muted { 0.0 } else { o.volume.min(1.0) });
    f.slider(Rect::new(x + 36.0, y + 6.0, w - 36.0 - 36.0, 32.0), value, output.is_some(), Action::Quick(Act::Volume));
    if output.is_some() {
        f.tip("Volume");
        f.glyph_button(Rect::new(x + w - 32.0, y + 6.0, 32.0, 32.0), Glyph::ChevronRight, "Select a sound output", Action::Quick(Act::OutputsPage));
    }

    // Footer: battery at the left, settings at the right.
    let footer = Rect::new(panel.x, panel.bottom() - FOOTER_H, panel.w, FOOTER_H);
    f.divider(footer.x + 1.0, footer.y, footer.w - 2.0);
    if let (Some(icon), Some(battery)) = (sys.battery_icon(), sys.power.battery.as_ref()) {
        f.icon(icon, x + 4.0, footer.y + 16.0, 16.0);
        f.text_left(&format!("{}%", battery.percent), Rect::new(x + 28.0, footer.y, 80.0, FOOTER_H), &ramp("caption", theme.text));
        f.area(Rect::new(x, footer.y + 8.0, 90.0, 32.0), Action::None);
        if let Some(text) = sys.battery_text() {
            f.tip(&text);
        }
    }
    f.icon_button(Rect::new(panel.right() - PAD - 32.0, footer.y + 8.0, 32.0, 32.0), "preferences-system-symbolic", "All settings", Action::Quick(Act::Settings("")));
}

/// The header of a page: back, title, and optionally a switch.
fn page_header(f: &mut Frame, panel: Rect, title: &str, switch: Option<(bool, Act)>) -> f32 {
    let theme = f.theme();
    f.glyph_button(Rect::new(panel.x + 12.0, panel.y + 12.0, 32.0, 32.0), Glyph::ArrowLeft, "Back", Action::Quick(Act::Back));
    f.text_left(title, Rect::new(panel.x + 52.0, panel.y + 12.0, panel.w - 130.0, 32.0), &ramp("body-strong", theme.text));
    if let Some((on, act)) = switch {
        f.toggle(panel.right() - PAD - 40.0, panel.y + 18.0, on, Action::Quick(act));
    }
    f.divider(panel.x + 1.0, panel.y + 56.0, panel.w - 2.0);
    panel.y + 64.0
}

/// The footer of a page: a link to the settings page and an extra button.
fn page_footer(f: &mut Frame, panel: Rect, link: &str, page: &'static str) -> Rect {
    let footer = Rect::new(panel.x, panel.bottom() - FOOTER_H, panel.w, FOOTER_H);
    f.divider(footer.x + 1.0, footer.y, footer.w - 2.0);
    let width = f.res.text.measure(link, &ramp("body", f.theme().text)).0 + 24.0;
    f.button(Rect::new(panel.x + 12.0, footer.y + 8.0, width, 32.0), link, ButtonKind::Subtle, true, Action::Quick(Act::Settings(page)));
    footer
}

fn message(f: &mut Frame, panel: Rect, y: f32, text: &str) {
    let style = ramp("body", f.theme().text_muted);
    f.text_wrapped(text, panel.x + PAD, y + 8.0, panel.w - 2.0 * PAD, 3, &style);
}

fn draw_wifi(f: &mut Frame, st: &State, panel: Rect) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    let net = &st.sys.network;
    let usable = net.available && net.wifi_present;
    let mut y = page_header(f, panel, "Wi-Fi", usable.then_some((net.wifi_enabled, Act::WifiToggle)));
    let footer = page_footer(f, panel, "More Wi-Fi settings", "network");
    if usable && net.wifi_enabled {
        f.icon_button(Rect::new(panel.right() - PAD - 32.0, footer.y + 8.0, 32.0, 32.0), "view-refresh-symbolic", "Search for networks", Action::Quick(Act::Scan));
    }
    if !net.available {
        return message(f, panel, y, "Network status is not available on this device.");
    }
    if !net.wifi_present {
        return message(f, panel, y, "This device has no Wi-Fi adapter.");
    }
    if !net.wifi_enabled {
        return message(f, panel, y, "Wi-Fi is turned off.");
    }
    if net.access_points.is_empty() {
        return message(f, panel, y, if net.wifi_scanning { "Searching for networks…" } else { "No networks found." });
    }
    let bottom = footer.y - 4.0;
    let view = Rect::new(panel.x, y, panel.w, bottom - y);
    let scroll = st.quick.scroll.min(net.access_points.len().saturating_sub(1));
    for (index, ap) in net.access_points.iter().enumerate().skip(scroll) {
        let expanded = st.quick.network.as_deref() == Some(ap.ssid.as_str());
        let pass = expanded && needs_passphrase(ap);
        let error = expanded && net.error.is_some();
        let height = LIST_ROW + if expanded { 44.0 } else { 0.0 } + if pass { 40.0 } else { 0.0 } + if error { 36.0 } else { 0.0 };
        if y + height > bottom {
            break;
        }
        let row = Rect::new(panel.x + 8.0, y, panel.w - 16.0, height);
        if expanded {
            let fill = f.res.fill("control");
            f.canvas.fill_rounded_rect(row.x, row.y, row.w, row.h, radius, fill);
        }
        let head = Rect::new(row.x, y, row.w, LIST_ROW);
        let touch = f.hit(head, Action::Quick(Act::Network(index)));
        if !expanded {
            f.state_layer(head, radius, touch, false);
        } else if touch.focused {
            f.focus_ring(head, radius);
        }
        f.icon(wifi_icon(ap.strength), head.x + 12.0, head.y + 16.0, 16.0);
        f.text_left(&ap.ssid, Rect::new(head.x + 40.0, head.y + 5.0, head.w - 52.0, 20.0), &ramp("body", theme.text));
        let status = match (ap.connected, ap.connecting, ap.known, ap.secured) {
            (true, _, _, _) => "Connected",
            (_, true, _, _) => "Connecting…",
            (_, _, true, _) => "Saved",
            (_, _, _, true) => "Secured",
            _ => "Open",
        };
        let color = if ap.connected { theme.accent } else { theme.text_muted };
        f.text_left(status, Rect::new(head.x + 40.0, head.y + 25.0, head.w - 52.0, 18.0), &ramp("caption", color));
        y += LIST_ROW;
        if !expanded {
            continue;
        }
        if pass {
            let field = Rect::new(row.x + 12.0, y, row.w - 24.0, 32.0);
            f.area(field, Action::Quick(Act::PassphraseField));
            f.text_box(field, &st.quick.passphrase, "Enter the network passphrase", st.quick.editing, !st.quick.reveal, 10.0, 40.0);
            let eye = if st.quick.reveal { "eye-closed-symbolic" } else { "eye-open-symbolic" };
            f.icon_button(Rect::new(field.right() - 34.0, field.y + 2.0, 28.0, 28.0), eye, if st.quick.reveal { "Hide" } else { "Show" }, Action::Quick(Act::Reveal));
            y += 40.0;
        }
        if error {
            let style = ramp("caption", theme.danger);
            f.text_wrapped(net.error.as_deref().unwrap_or_default(), row.x + 12.0, y, row.w - 24.0, 2, &style);
            y += 36.0;
        }
        let button_w = (row.w - 24.0 - 8.0) / 2.0;
        let first = Rect::new(row.x + 12.0, y + 2.0, button_w, 32.0);
        let second = Rect::new(first.right() + 8.0, y + 2.0, button_w, 32.0);
        if ap.connected {
            f.button(first, "Disconnect", ButtonKind::Standard, true, Action::Quick(Act::Disconnect));
            f.button(second, "Forget", ButtonKind::Standard, true, Action::Quick(Act::Forget));
        } else {
            let ready = !pass || st.quick.passphrase.len() >= 8;
            f.button(first, "Connect", ButtonKind::Accent, ready && !ap.connecting, Action::Quick(Act::Connect));
            if ap.known {
                f.button(second, "Forget", ButtonKind::Standard, true, Action::Quick(Act::Forget));
            } else {
                f.button(second, "Cancel", ButtonKind::Standard, true, Action::Quick(Act::Cancel));
            }
        }
        y += 44.0;
    }
    f.scrollbar(view, net.access_points.len() as f32 * LIST_ROW, scroll as f32 * LIST_ROW);
}

fn device_icon(device: &BluetoothDevice) -> &str {
    if device.icon.is_empty() {
        "bluetooth-active-symbolic"
    } else {
        &device.icon
    }
}

fn draw_bluetooth(f: &mut Frame, st: &State, panel: Rect) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    let bt = &st.sys.bluetooth;
    let mut y = page_header(f, panel, "Bluetooth", bt.available.then_some((bt.enabled, Act::BluetoothToggle)));
    let footer = page_footer(f, panel, "More Bluetooth settings", "bluetooth");
    if !bt.available {
        return message(f, panel, y, "Bluetooth is not available on this device.");
    }
    if !bt.enabled {
        return message(f, panel, y, "Bluetooth is turned off.");
    }
    if bt.devices.is_empty() {
        return message(f, panel, y, if bt.discovering { "Searching for devices…" } else { "No devices found." });
    }
    let bottom = footer.y - 4.0;
    let view = Rect::new(panel.x, y, panel.w, bottom - y);
    let scroll = st.quick.scroll.min(bt.devices.len().saturating_sub(1));
    for (index, device) in bt.devices.iter().enumerate().skip(scroll) {
        let expanded = st.quick.device.as_deref() == Some(device.address.as_str());
        let height = LIST_ROW + if expanded { 44.0 } else { 0.0 };
        if y + height > bottom {
            break;
        }
        let row = Rect::new(panel.x + 8.0, y, panel.w - 16.0, height);
        if expanded {
            let fill = f.res.fill("control");
            f.canvas.fill_rounded_rect(row.x, row.y, row.w, row.h, radius, fill);
        }
        let head = Rect::new(row.x, y, row.w, LIST_ROW);
        let touch = f.hit(head, Action::Quick(Act::Device(index)));
        if !expanded {
            f.state_layer(head, radius, touch, false);
        } else if touch.focused {
            f.focus_ring(head, radius);
        }
        if !f.icon(device_icon(device), head.x + 10.0, head.y + 14.0, 20.0) {
            f.icon("bluetooth-active-symbolic", head.x + 12.0, head.y + 16.0, 16.0);
        }
        f.text_left(&device.name, Rect::new(head.x + 40.0, head.y + 5.0, head.w - 52.0, 20.0), &ramp("body", theme.text));
        let mut status = match (device.connected, device.connecting, device.paired) {
            (true, _, _) => "Connected".to_owned(),
            (_, true, _) => "Connecting…".to_owned(),
            (_, _, true) => "Paired".to_owned(),
            _ => "Not paired".to_owned(),
        };
        if let (true, Some(battery)) = (device.connected, device.battery) {
            status.push_str(&format!(" · Battery {battery}%"));
        }
        let color = if device.connected { theme.accent } else { theme.text_muted };
        f.text_left(&status, Rect::new(head.x + 40.0, head.y + 25.0, head.w - 52.0, 18.0), &ramp("caption", color));
        y += LIST_ROW;
        if !expanded {
            continue;
        }
        let button_w = (row.w - 24.0 - 8.0) / 2.0;
        let first = Rect::new(row.x + 12.0, y + 2.0, button_w, 32.0);
        let second = Rect::new(first.right() + 8.0, y + 2.0, button_w, 32.0);
        if device.connected {
            f.button(first, "Disconnect", ButtonKind::Standard, true, Action::Quick(Act::DeviceDisconnect));
        } else {
            let label = if device.paired { "Connect" } else { "Pair" };
            f.button(first, label, ButtonKind::Accent, !device.connecting, Action::Quick(Act::DeviceConnect));
        }
        if device.paired {
            f.button(second, "Remove", ButtonKind::Standard, true, Action::Quick(Act::DeviceForget));
        }
        y += 44.0;
    }
    f.scrollbar(view, bt.devices.len() as f32 * LIST_ROW, scroll as f32 * LIST_ROW);
}

fn draw_outputs(f: &mut Frame, st: &State, panel: Rect) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    let mut y = page_header(f, panel, "Sound output", None);
    let footer = page_footer(f, panel, "More sound settings", "sound");
    if st.sys.audio.outputs.is_empty() {
        return message(f, panel, y, "No sound output was found.");
    }
    for device in st.sys.audio.outputs.iter().skip(st.quick.scroll) {
        if y + LIST_ROW > footer.y - 4.0 {
            break;
        }
        let row = Rect::new(panel.x + 8.0, y, panel.w - 16.0, LIST_ROW - 4.0);
        let touch = f.hit(row, Action::Quick(Act::Output(device.id)));
        f.state_layer(row, radius, touch, device.is_default);
        if device.is_default {
            f.canvas.fill_rounded_rect(row.x, row.y + (row.h - 16.0) / 2.0, 3.0, 16.0, 1.5, theme.accent);
        }
        f.icon("audio-volume-high-symbolic", row.x + 14.0, row.y + (row.h - 16.0) / 2.0, 16.0);
        f.text_left(&device.name, Rect::new(row.x + 42.0, row.y, row.w - 54.0, row.h), &ramp("body", theme.text));
        y += LIST_ROW;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ap(ssid: &str, secured: bool, known: bool, connected: bool) -> AccessPoint {
        AccessPoint {
            ssid: ssid.into(),
            strength: 60,
            secured,
            known,
            connected,
            connecting: false,
        }
    }

    #[test]
    fn a_passphrase_is_asked_only_for_new_secured_networks() {
        assert!(needs_passphrase(&ap("Cafe", true, false, false)));
        assert!(!needs_passphrase(&ap("Home", true, true, false)));
        assert!(!needs_passphrase(&ap("Guest", false, false, false)));
        assert!(!needs_passphrase(&ap("Office", true, false, true)));
    }

    #[test]
    fn selecting_a_network_expands_it_and_wipes_the_previous_passphrase() {
        let mut quick = Quick::default();
        let cafe = ap("Cafe", true, false, false);
        quick.select_network(&cafe);
        assert_eq!(quick.network.as_deref(), Some("Cafe"));
        assert!(quick.editing, "the passphrase field takes the keyboard");
        quick.passphrase.insert("hunter22");
        quick.reveal = true;
        let home = ap("Home", true, true, false);
        quick.select_network(&home);
        assert_eq!(quick.network.as_deref(), Some("Home"));
        assert!(quick.passphrase.is_empty() && !quick.reveal && !quick.editing);
        quick.select_network(&home);
        assert_eq!(quick.network, None, "selecting it again collapses it");
        quick.open_page(Page::Bluetooth);
        quick.device = Some("AA".into());
        quick.reset();
        assert_eq!((quick.page, quick.device.as_ref()), (Page::Main, None));
    }
}
