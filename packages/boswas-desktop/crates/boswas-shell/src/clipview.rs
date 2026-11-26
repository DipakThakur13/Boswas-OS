//! The clipboard history's flyout (Super+V): the items as cards, newest
//! first, each with Pin and Delete; Clear all; a search; and, with the
//! history turned off, a card that says so with a button to turn it on.
//!
//! Enter or a click pastes an item into the window that had the keyboard
//! (`datacontrol.rs`). Keys: the arrows move between the cards, Delete
//! removes one, Ctrl+P pins or unpins it, typing searches, Escape closes.

use std::path::PathBuf;

use boswas_ui::tokens::tokens;
use smithay_client_toolkit::seat::keyboard::{KeyEvent, Keysym};

use crate::{
    action::Action,
    clipboard::{file_name, path_of, Clip, Content},
    state::State,
    ui::{ramp, text_scale, ButtonKind, Frame, Nav, Rect, Resources, TextField},
    Role, Shell,
};

/// What the flyout shows besides the history itself.
#[derive(Default)]
pub struct View {
    pub search: TextField,
    /// The first card shown.
    pub scroll: usize,
}

/// What a control of the flyout does.
#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    Paste(u64),
    Pin(u64),
    Delete(u64),
    ClearAll,
    /// Turns the history on (it is off).
    TurnOn,
    Search,
}

const WIDTH: f32 = 384.0;
const HEIGHT: f32 = 500.0;
const PAD: f32 = 12.0;
/// The buttons on a card.
const BUTTON: f32 = 28.0;
/// Lines of text a card shows at most.
const LINES: usize = 3;
/// The tallest picture on a card (logical pixels).
const PICTURE_H: f32 = 110.0;

/// The flyout's size on an output (logical pixels, without its shadow).
pub fn panel_size(output: (f32, f32)) -> (f32, f32) {
    let t = tokens();
    let room = output.1 - t.shell("taskbar-height") - 2.0 * t.shell("flyout-margin");
    let grow = (text_scale() - 1.0) * 120.0;
    (WIDTH.min(output.0 - 24.0), (HEIGHT + grow).min(room).max(260.0))
}

/// Where everything is.
#[derive(Clone, Debug, Default)]
pub struct Layout {
    pub title: Rect,
    pub clear: Option<Rect>,
    pub search: Option<Rect>,
    /// The card that says the history is off.
    pub off: Option<Rect>,
    pub view: Rect,
    /// The cards shown: item id and place.
    pub cards: Vec<(u64, Rect)>,
    /// How many items the search shows in all.
    pub total: usize,
}

const WHITE: boswas_ui::tokens::Rgba = boswas_ui::tokens::Rgba::WHITE;

/// The text a card shows of an item (its first lines, short).
fn preview(text: &str) -> String {
    let head: String = text.chars().take(1200).collect();
    head.lines().map(str::trim_end).filter(|l| !l.trim().is_empty()).take(LINES).collect::<Vec<_>>().join("\n")
}

/// A card's height for its item at `width` (the whole card's width).
fn card_height(res: &mut Resources, clip: &Clip, width: f32) -> f32 {
    let inner = width - 2.0 * PAD - 2.0 * BUTTON - 8.0;
    let body = ramp("body", WHITE);
    let content = match &clip.content {
        Content::Text(text) => res.text.wrap(&preview(text), &body, inner, LINES).len().max(1) as f32 * body.line_height(),
        Content::Image { width: w, height: h, .. } => picture_size((*w, *h), inner).1,
        Content::Files(uris) => body.line_height() + if uris.len() > 1 { ramp("caption", WHITE).line_height() } else { 0.0 },
    };
    (content + 2.0 * PAD).max(BUTTON + 2.0 * 8.0)
}

/// A picture's size on a card.
fn picture_size((w, h): (u32, u32), room: f32) -> (f32, f32) {
    let (w, h) = (w.max(1) as f32, h.max(1) as f32);
    let scale = (room / w).min(PICTURE_H / h).min(1.0);
    ((w * scale).max(1.0), (h * scale).max(1.0))
}

pub fn layout(res: &mut Resources, st: &State, panel: Rect) -> Layout {
    let inner = panel.inset(PAD);
    let title_h = ramp("body-strong", WHITE).line_height().max(32.0);
    let title = Rect::new(inner.x, inner.y, inner.w, title_h);
    let history = &st.clips;
    let has_unpinned = history.items.iter().any(|c| !c.pinned);
    let clear_w = (res.text.measure("Clear all", &ramp("body", WHITE)).0 + 24.0).max(80.0);
    let clear = (history.enabled && has_unpinned).then(|| Rect::new(inner.right() - clear_w, inner.y, clear_w, title_h));
    let mut y = title.bottom() + 8.0;
    let off = (!history.enabled).then(|| {
        let body = ramp("body", WHITE);
        let lines = res.text.wrap(OFF_TEXT, &body, inner.w - 2.0 * PAD, 6).len() as f32;
        let h = PAD + ramp("body-strong", WHITE).line_height() + 4.0 + lines * body.line_height() + 12.0 + 32.0 + PAD;
        let rect = Rect::new(inner.x, y, inner.w, h);
        y = rect.bottom() + 8.0;
        rect
    });
    let search = (!history.items.is_empty() || !st.clipview.search.is_empty()).then(|| {
        let h = (ramp("body", WHITE).line_height() + 12.0).max(32.0);
        let rect = Rect::new(inner.x, y, inner.w, h);
        y = rect.bottom() + 8.0;
        rect
    });
    let view = Rect::new(inner.x, y, inner.w, (inner.bottom() - y).max(0.0));
    let shown = history.matching(st.clipview.search.text());
    let total = shown.len();
    let mut cards = Vec::new();
    let mut top = view.y;
    for clip in shown.iter().skip(st.clipview.scroll.min(total.saturating_sub(1))) {
        let h = card_height(res, clip, view.w);
        if top + h > view.bottom() && !cards.is_empty() {
            break;
        }
        cards.push((clip.id, Rect::new(view.x, top, view.w, h.min(view.bottom() - top).max(0.0))));
        top += h + 8.0;
    }
    Layout { title, clear, search, off, view, cards, total }
}

const OFF_TEXT: &str = "Turn it on to keep the last 25 things you copy, and pin the ones you use often. \
                        What you copy stays on this device and is forgotten when you sign out.";

pub fn draw(f: &mut Frame, st: &State, panel: Rect) {
    f.panel(panel, "acrylic");
    f.area(panel, Action::None);
    let theme = f.theme();
    let lay = layout(f.res, st, panel);
    f.text_left("Clipboard", lay.title, &ramp("body-strong", theme.text));
    if let Some(rect) = lay.clear {
        f.button(rect, "Clear all", ButtonKind::Subtle, true, Action::Clip(Act::ClearAll));
        f.tip("Clear everything that is not pinned");
    }
    if let Some(card) = lay.off {
        f.card(card, tokens().radius_card);
        let x = card.x + PAD;
        let mut y = card.y + PAD;
        let strong = ramp("body-strong", theme.text);
        f.text("Clipboard history is off", x, y, Some(card.w - 2.0 * PAD), &strong);
        y += strong.line_height() + 4.0;
        y += f.text_wrapped(OFF_TEXT, x, y, card.w - 2.0 * PAD, 6, &ramp("body", theme.text_secondary)) + 12.0;
        let w = (f.res.text.measure("Turn on", &ramp("body", theme.text)).0 + 32.0).max(96.0);
        f.button(Rect::new(x, y, w, 32.0), "Turn on", ButtonKind::Accent, true, Action::Clip(Act::TurnOn));
    }
    if let Some(rect) = lay.search {
        f.area(rect, Action::Clip(Act::Search));
        f.text_box(rect, &st.clipview.search, "Search", true, false, 34.0, 8.0);
        symbol(f, "edit-find-symbolic", Rect::new(rect.x + 10.0, rect.y + (rect.h - 16.0) / 2.0, 16.0, 16.0), theme.text_secondary);
    }
    if lay.total == 0 && st.clips.enabled {
        let (head, body) = if st.clipview.search.is_empty() {
            ("Nothing here yet", "Copy text, pictures or files, and they show up here. Super+V opens this list.")
        } else {
            ("Nothing found", "Nothing you copied has that in it.")
        };
        let strong = ramp("body-strong", theme.text);
        f.text_centered(head, Rect::new(lay.view.x, lay.view.y + 24.0, lay.view.w, strong.line_height()), &strong);
        let style = ramp("body", theme.text_secondary);
        let lines = f.res.text.wrap(body, &style, lay.view.w - 32.0, 4);
        for (i, line) in lines.iter().enumerate() {
            let y = lay.view.y + 24.0 + strong.line_height() + 6.0 + i as f32 * style.line_height();
            f.text_centered(line, Rect::new(lay.view.x, y, lay.view.w, style.line_height()), &style);
        }
    }
    for (id, rect) in &lay.cards {
        if let Some(clip) = st.clips.get(*id) {
            draw_card(f, clip, *rect);
        }
    }
    if lay.total > lay.cards.len() && !lay.cards.is_empty() {
        let shown = lay.cards.len() as f32;
        let content = lay.view.h * lay.total as f32 / shown;
        let offset = lay.view.h * st.clipview.scroll as f32 / shown;
        f.scrollbar(Rect::new(lay.view.x, lay.view.y, lay.view.w + PAD * 0.75, lay.view.h), content, offset);
    }
}

fn draw_card(f: &mut Frame, clip: &Clip, rect: Rect) {
    let theme = f.theme();
    let radius = tokens().radius_card;
    let touch = f.hit(rect, Action::Clip(Act::Paste(clip.id)));
    f.tip("Paste");
    let fill = f.res.fill(if touch.pressed {
        "control-pressed"
    } else if touch.hovered {
        "control-hover"
    } else {
        "control"
    });
    let stroke = f.res.stroke("control");
    f.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, fill);
    f.canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, 1.0, stroke);
    if touch.focused {
        f.focus_ring(rect, radius);
    }
    let inner = rect.w - 2.0 * PAD - 2.0 * BUTTON - 8.0;
    let (x, y) = (rect.x + PAD, rect.y + PAD);
    let body = ramp("body", theme.text);
    match &clip.content {
        Content::Text(text) => {
            f.text_wrapped(&preview(text), x, y, inner, LINES, &body);
        }
        Content::Image { width, height, thumbnail, .. } => {
            let (w, h) = picture_size((*width, *height), inner);
            match thumbnail {
                Some(picture) => f.pixmap(picture, Rect::new(x, y, w, h)),
                None => f.canvas.fill_rect(x, y, w, h, theme.surface_hover),
            }
            f.canvas.stroke_rounded_rect(x, y, w, h, 2.0, 1.0, f.res.stroke("divider"));
        }
        Content::Files(uris) => {
            let size = 16.0;
            let folder = uris[0].ends_with('/') || std::path::Path::new(&path_of(&uris[0])).is_dir();
            f.icon_or_placeholder(if folder { &["folder", "inode-directory"] } else { &["text-x-generic", "application-octet-stream"] }, x, y + (body.line_height() - size) / 2.0, size);
            f.text(&file_name(&uris[0]), x + size + 8.0, y, Some(inner - size - 8.0), &ramp("body-strong", theme.text));
            if uris.len() > 1 {
                let more = format!("and {} more", uris.len() - 1);
                f.text(&more, x + size + 8.0, y + body.line_height(), Some(inner - size - 8.0), &ramp("caption", theme.text_secondary));
            }
        }
    }
    // Pin and Delete.
    let delete = Rect::new(rect.right() - 8.0 - BUTTON, rect.y + 8.0, BUTTON, BUTTON);
    let pin = Rect::new(delete.x - BUTTON, delete.y, BUTTON, BUTTON);
    let touch = f.hit(pin, Action::Clip(Act::Pin(clip.id)));
    f.tip(if clip.pinned { "Unpin" } else { "Pin" });
    f.state_layer(pin, tokens().radius_control, touch, false);
    let color = if clip.pinned { theme.accent } else { theme.text_secondary };
    symbol(f, "view-pin-symbolic", Rect::new(pin.x + 6.0, pin.y + 6.0, 16.0, 16.0), color);
    let touch = f.hit(delete, Action::Clip(Act::Delete(clip.id)));
    f.tip("Delete");
    f.state_layer(delete, tokens().radius_control, touch, false);
    symbol(f, "edit-delete-symbolic", Rect::new(delete.x + 6.0, delete.y + 6.0, 16.0, 16.0), theme.text_secondary);
}

/// A symbolic icon in one colour; where the icon theme lacks it, a drawn
/// stand-in, so no control of the flyouts is ever blank.
pub(crate) fn symbol(f: &mut Frame, name: &str, rect: Rect, color: boswas_ui::tokens::Rgba) {
    if f.icon_tinted(name, rect.x, rect.y, rect.w, color) {
        return;
    }
    let s = rect.w / 16.0;
    let p = |x: f32, y: f32| (rect.x + x * s, rect.y + y * s);
    let width = (1.5 * s).max(1.2);
    match name {
        "edit-find-symbolic" => {
            let (cx, cy) = p(6.5, 6.5);
            f.canvas.stroke_circle(cx, cy, 4.5 * s, width, color);
            f.canvas.polyline(&[p(10.0, 10.0), p(14.0, 14.0)], width, color);
        }
        "edit-delete-symbolic" => {
            f.canvas.polyline(&[p(2.5, 4.0), p(13.5, 4.0)], width, color);
            f.canvas.polyline(&[p(6.0, 4.0), p(6.0, 2.0), p(10.0, 2.0), p(10.0, 4.0)], width, color);
            f.canvas.polyline(&[p(4.0, 4.0), p(5.0, 14.0), p(11.0, 14.0), p(12.0, 4.0)], width, color);
        }
        "view-pin-symbolic" => {
            f.canvas.polyline(&[p(5.0, 2.0), p(11.0, 2.0)], width, color);
            f.canvas.polyline(&[p(6.0, 2.0), p(6.0, 7.0), p(4.0, 9.0), p(12.0, 9.0), p(10.0, 7.0), p(10.0, 2.0)], width, color);
            f.canvas.polyline(&[p(8.0, 9.0), p(8.0, 14.5)], width, color);
        }
        _ => {
            // A clock: recently used.
            let (cx, cy) = p(8.0, 8.0);
            f.canvas.stroke_circle(cx, cy, 6.0 * s, width, color);
            f.canvas.polyline(&[p(8.0, 4.5), p(8.0, 8.0), p(10.5, 9.5)], width, color);
        }
    }
}

/// The user's own `compositor.toml`.
fn user_config() -> Option<PathBuf> {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))
        .map(|dir| dir.join("boswas").join("compositor.toml"))
}

/// The user's configuration with `[clipboard] history = true`: the line
/// changed where it is, else the table added. `None` when the result would
/// not say so for certain (a file that does not parse, the key written in
/// another form); then the file is left as it is.
pub fn with_history_on(text: &str) -> Option<String> {
    text.parse::<toml::Table>().ok()?;
    let mut out = String::new();
    let mut in_table = false;
    let mut done = false;
    for line in text.lines() {
        let code = line.split('#').next().unwrap_or("").trim();
        if code.starts_with('[') {
            if in_table && !done {
                out.push_str("history = true\n");
                done = true;
            }
            in_table = code.replace(' ', "") == "[clipboard]";
        } else if in_table && code.split('=').next().map(str::trim) == Some("history") {
            out.push_str("history = true\n");
            done = true;
            continue;
        }
        out.push_str(line);
        out.push('\n');
    }
    if in_table && !done {
        out.push_str("history = true\n");
        done = true;
    }
    if !done {
        if !out.is_empty() && !out.ends_with("\n\n") {
            out.push('\n');
        }
        out.push_str("[clipboard]\nhistory = true\n");
    }
    let parsed: toml::Table = out.parse().ok()?;
    (parsed.get("clipboard")?.get("history")?.as_bool() == Some(true)).then_some(out)
}

impl Shell {
    /// The flyout's layout as it is open.
    fn clip_layout(&mut self) -> Option<Layout> {
        let surface = self.surfaces.iter().find(|s| s.role == Role::Flyout)?;
        let (w, h) = (surface.width as f32, surface.height as f32);
        let panel = Rect::new(crate::PAD, crate::PAD, w - 2.0 * crate::PAD, h - 2.0 * crate::PAD);
        Some(layout(&mut self.res, &self.st, panel))
    }

    pub fn clip_action(&mut self, act: Act) {
        match act {
            Act::Paste(id) => return self.clip_paste(id),
            Act::Pin(id) => {
                let pinned = self.st.clips.get(id).is_some_and(|c| c.pinned);
                self.st.clips.set_pinned(id, !pinned);
            }
            Act::Delete(id) => {
                self.st.clips.remove(id);
                self.clamp_clip_scroll();
            }
            Act::ClearAll => {
                self.st.clips.clear();
                self.st.clipview.scroll = 0;
                self.st.focus = None;
            }
            Act::TurnOn => self.turn_on_history(),
            Act::Search => self.st.focus = None,
        }
        self.mark(Role::Flyout);
    }

    fn clamp_clip_scroll(&mut self) {
        let total = self.st.clips.matching(self.st.clipview.search.text()).len();
        self.st.clipview.scroll = self.st.clipview.scroll.min(total.saturating_sub(1));
    }

    /// "Turn on": at once for this session, and in the user's settings
    /// (then the compositor reads them again and tells everyone). When the
    /// file cannot be changed safely, Settings opens at the page instead.
    fn turn_on_history(&mut self) {
        let cleared_at = self.st.clips.cleared_at;
        self.st.clips.configure(true, cleared_at);
        let Some(path) = user_config() else {
            return;
        };
        let current = match std::fs::read_to_string(&path) {
            Ok(text) => Some(text),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Some(String::new()),
            Err(_) => None,
        };
        let written = current
            .and_then(|text| with_history_on(&text))
            .is_some_and(|text| crate::clipboard::write_private(&path, text.as_bytes()).is_ok());
        if written {
            self.send_control("reload-config");
        } else {
            tracing::warn!("could not turn the clipboard history on in {}; opening Settings", path.display());
            self.spawn(&crate::start::settings_command("clipboard"));
        }
    }

    pub fn clip_scroll(&mut self, steps: i32) {
        let total = self.st.clips.matching(self.st.clipview.search.text()).len() as i64;
        let scroll = (self.st.clipview.scroll as i64 + i64::from(steps)).clamp(0, (total - 1).max(0)) as usize;
        if scroll != self.st.clipview.scroll {
            self.st.clipview.scroll = scroll;
            self.mark(Role::Flyout);
        }
    }

    pub fn clip_key(&mut self, event: &KeyEvent, typed: Option<&str>) {
        let shown: Vec<u64> = self.st.clips.matching(self.st.clipview.search.text()).iter().map(|c| c.id).collect();
        // As on Windows, the newest item is the one the keys act on until
        // another is chosen.
        let focused = match &self.st.focus {
            Some(Action::Clip(Act::Paste(id) | Act::Pin(id) | Act::Delete(id))) => shown.iter().position(|i| i == id),
            _ => None,
        };
        let current = focused.unwrap_or(0);
        let ctrl = self.modifiers.ctrl;
        let step = |delta: i64| -> Option<usize> {
            if shown.is_empty() {
                return None;
            }
            Some((current as i64 + delta).clamp(0, shown.len() as i64 - 1) as usize)
        };
        let target = match event.keysym {
            Keysym::Escape => {
                if !self.st.clipview.search.is_empty() {
                    self.st.clipview.search.clear();
                    self.st.clipview.scroll = 0;
                    self.st.focus = None;
                    return self.mark(Role::Flyout);
                }
                return self.close_flyout();
            }
            Keysym::Return | Keysym::KP_Enter => {
                match self.st.focus.clone() {
                    Some(Action::Clip(act)) if !matches!(act, Act::Search) => self.clip_action(act),
                    _ => {
                        if let Some(id) = shown.first() {
                            self.clip_paste(*id);
                        } else if !self.st.clips.enabled {
                            self.clip_action(Act::TurnOn);
                        }
                    }
                }
                return;
            }
            Keysym::Delete | Keysym::KP_Delete if !shown.is_empty() => {
                self.clip_action(Act::Delete(shown[current]));
                let after: Vec<u64> = self.st.clips.matching(self.st.clipview.search.text()).iter().map(|c| c.id).collect();
                self.st.focus = after.get(current.min(after.len().saturating_sub(1))).map(|id| Action::Clip(Act::Paste(*id)));
                return self.mark(Role::Flyout);
            }
            Keysym::p | Keysym::P if ctrl => {
                if let Some(id) = shown.get(current) {
                    self.clip_action(Act::Pin(*id));
                }
                return;
            }
            Keysym::Up | Keysym::KP_Up => step(-1),
            Keysym::Down | Keysym::KP_Down => step(1),
            Keysym::Page_Up | Keysym::KP_Page_Up => step(-4),
            Keysym::Page_Down | Keysym::KP_Page_Down => step(4),
            Keysym::Home | Keysym::KP_Home if focused.is_some() => step(-(shown.len() as i64)),
            Keysym::End | Keysym::KP_End if focused.is_some() => step(shown.len() as i64),
            Keysym::Tab | Keysym::ISO_Left_Tab => {
                // Every control in drawing order: the cards and their buttons.
                let nav = if self.modifiers.shift || event.keysym == Keysym::ISO_Left_Tab { Nav::Previous } else { Nav::Next };
                if let Some(index) = self.surfaces.iter().position(|s| s.role == Role::Flyout) {
                    if let Some(action) = crate::ui::navigate(&self.surfaces[index].hits, self.st.focus.as_ref(), nav) {
                        self.st.focus = Some(action);
                        self.mark(Role::Flyout);
                    }
                }
                return;
            }
            Keysym::BackSpace => {
                if self.st.clipview.search.backspace() {
                    self.st.clipview.scroll = 0;
                    self.st.focus = None;
                    self.mark(Role::Flyout);
                }
                return;
            }
            _ => {
                if let Some(text) = typed.filter(|t| !t.chars().all(char::is_control)) {
                    if self.st.clipview.search.insert(text) {
                        self.st.clipview.scroll = 0;
                        self.st.focus = None;
                        self.mark(Role::Flyout);
                    }
                } else if self.st.clipview.search.key(event.keysym, ctrl) {
                    self.mark(Role::Flyout);
                }
                return;
            }
        };
        let Some(at) = target else {
            return;
        };
        // Scrolled so that the focused card is shown whole.
        if at < self.st.clipview.scroll {
            self.st.clipview.scroll = at;
        } else {
            while self.st.clipview.scroll < at {
                let Some(lay) = self.clip_layout() else {
                    break;
                };
                let id = shown[at];
                if lay.cards.iter().any(|(card, rect)| *card == id && rect.bottom() <= lay.view.bottom() + 0.5) {
                    break;
                }
                self.st.clipview.scroll += 1;
            }
        }
        self.st.focus = Some(Action::Clip(Act::Paste(shown[at])));
        self.mark(Role::Flyout);
    }

    /// Prepares the flyout as it opens.
    pub fn open_clipboard(&mut self) {
        self.st.clipview.search.clear();
        self.st.clipview.scroll = 0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::testing;
    use boswas_ui::Scheme;

    #[test]
    fn turning_the_history_on_changes_one_line_or_adds_the_table() {
        assert_eq!(with_history_on("").unwrap(), "[clipboard]\nhistory = true\n");
        assert_eq!(
            with_history_on("[keyboard]\nlayout = \"us\"\n").unwrap(),
            "[keyboard]\nlayout = \"us\"\n\n[clipboard]\nhistory = true\n"
        );
        assert_eq!(
            with_history_on("[clipboard]\nhistory = false # off\ncleared_at = 5\n[start]\nlayout = \"default\"\n").unwrap(),
            "[clipboard]\nhistory = true\ncleared_at = 5\n[start]\nlayout = \"default\"\n"
        );
        assert_eq!(with_history_on("[clipboard]\ncleared_at = 5\n").unwrap(), "[clipboard]\ncleared_at = 5\nhistory = true\n");
        assert_eq!(with_history_on("[ clipboard ]\ncleared_at = 5\n[x]\n").unwrap(), "[ clipboard ]\ncleared_at = 5\nhistory = true\n[x]\n");
        // Not for certain: left alone.
        assert_eq!(with_history_on("clipboard.history = false\n"), None);
        assert_eq!(with_history_on("[clipboard\nbroken"), None);
    }

    fn with_items(st: &mut State) {
        let png = {
            let mut picture = tiny_skia::Pixmap::new(320, 180).unwrap();
            picture.fill(tiny_skia::Color::from_rgba8(80, 120, 200, 255));
            boswas_ui::icons::encode_png(&picture).unwrap()
        };
        st.clips.add(crate::clipboard::image_content(png, 0).unwrap(), 1);
        st.clips.add(Content::Files(vec!["file:///home/asha/Documents/Report%202026.pdf".into(), "file:///home/asha/Pictures/".into()]), 2);
        st.clips.add(Content::Text("A longer text that was copied from a document, with more words than fit on one line of the card, so that it wraps onto a second and a third line and then ends with an ellipsis because it is still not over.".into()), 3);
        st.clips.add(Content::Text("https://example.org/a/very/long/address/that/does/not/break/at/spaces".into()), 4);
        st.clips.add(Content::Text("Short".into()), 5);
        let id = st.clips.items[3].id;
        st.clips.set_pinned(id, true);
    }

    #[test]
    fn the_flyout_draws_its_cards_inside_at_every_text_size() {
        for text in [100, 175, 225] {
            crate::ui::set_text_scale(text);
            for state in ["items", "empty", "off"] {
                let mut st = State::for_tests();
                match state {
                    "items" => with_items(&mut st),
                    "off" => {
                        with_items(&mut st);
                        st.clips.configure(false, 0);
                    }
                    _ => {}
                }
                let (w, h) = panel_size((1366.0, 768.0));
                let (canvas, hits) = testing::render(w + 24.0, h + 24.0, 1.0, Scheme::Light, None, None, |f| draw(f, &st, Rect::new(12.0, 12.0, w, h)));
                assert!(testing::hits_are_inside(&hits, w + 24.0, h + 24.0), "{state} at {text} %");
                assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0));
                let cards = hits.iter().filter(|h| matches!(h.action, Action::Clip(Act::Paste(_)))).count();
                let turn_on = hits.iter().any(|h| h.action == Action::Clip(Act::TurnOn));
                match state {
                    "items" => assert!(cards >= 2 && !turn_on, "{text} %: {cards} cards"),
                    "off" => assert!(turn_on && cards == 1, "only the pinned item, and the card to turn it on"),
                    _ => assert!(cards == 0 && !turn_on),
                }
            }
        }
        crate::ui::set_text_scale(100);
    }

    /// Writes pictures of both flyouts over the taskbar and the wallpaper,
    /// for people to look at: `BOSWAS_SHELL_PICTURES=DIR`, then
    /// `cargo test -p boswas-shell -- --ignored picker_pictures`.
    #[test]
    #[ignore = "writes pictures for people to look at"]
    fn picker_pictures() {
        use crate::{emoji, taskbar, PAD};
        let Some(out) = std::env::var_os("BOSWAS_SHELL_PICTURES").map(std::path::PathBuf::from) else {
            return;
        };
        let picture = |st: &State, (w, h): (f32, f32), scheme: Scheme, kind: &str, name: &str| {
            let theme = boswas_ui::Theme::of(scheme);
            let mut background = crate::wallpaper::Background::default();
            background.load_picture("");
            let mut res = Resources::new(theme);
            res.backdrop = background.reduced(theme, w, h).and_then(|reduced| boswas_ui::material::Backdrop::new(&reduced, w, h));
            let bar = tokens().shell("taskbar-height");
            let (pw, ph) = if kind == "emoji" { emoji::panel_size((w, h)) } else { panel_size((w, h)) };
            let (sw, sh) = (pw + 2.0 * PAD, ph + 2.0 * PAD);
            let origin = (((w - sw) / 2.0).floor(), h - bar - sh);
            let (taskbar, _) = testing::render_with(Resources::new(theme), w, bar, 1.0, None, None, |f| taskbar::draw(f, st));
            let (flyout, _) = testing::render_with(res, sw, sh, 1.0, None, st.focus.as_ref(), |f| {
                f.origin = origin;
                if kind == "emoji" {
                    emoji::draw(f, st, Rect::new(PAD, PAD, pw, ph));
                } else {
                    draw(f, st, Rect::new(PAD, PAD, pw, ph));
                }
            });
            let (canvas, _) = testing::render(w, h, 1.0, scheme, None, None, |f| {
                background.paint(f.canvas, theme);
                f.canvas.draw_pixmap(taskbar.pixmap(), 0.0, h - bar, w, bar, 1.0);
                f.canvas.draw_pixmap(flyout.pixmap(), origin.0, origin.1, sw, sh, 1.0);
            });
            let file = format!("{kind}-{name}-{w}x{h}-{}-{}.png", text_scale() * 100.0, if scheme == Scheme::Dark { "dark" } else { "light" });
            std::fs::write(out.join(file), boswas_ui::icons::encode_png(canvas.pixmap()).unwrap()).unwrap();
        };
        for output in [(1920.0, 1080.0), (1366.0, 768.0)] {
            for text in [100, 175] {
                crate::ui::set_text_scale(text);
                for scheme in [Scheme::Dark, Scheme::Light] {
                    let mut st = State::for_tests();
                    with_items(&mut st);
                    picture(&st, output, scheme, "clipboard", "items");
                    st.focus = Some(Action::Clip(Act::Paste(st.clips.items[1].id)));
                    picture(&st, output, scheme, "clipboard", "focused");
                    st.focus = None;
                    st.clipview.search.insert("report");
                    picture(&st, output, scheme, "clipboard", "search");
                    let mut empty = State::for_tests();
                    picture(&empty, output, scheme, "clipboard", "empty");
                    empty.clips.configure(false, 0);
                    picture(&empty, output, scheme, "clipboard", "off");
                    for tab in emoji::Tab::ALL {
                        let mut st = State::for_tests();
                        st.emoji.tab = tab;
                        st.emoji.reset();
                        picture(&st, output, scheme, "emoji", &format!("{tab:?}").to_lowercase());
                    }
                    let mut st = State::for_tests();
                    st.emoji.recent.items = ["\u{1F44B}", "\u{1F600}", "\u{2764}\u{FE0F}", "\u{1F44D}", "\u{1F389}"].iter().map(|s| (*s).to_owned()).collect();
                    st.emoji.recent.tone = 3;
                    st.emoji.reset();
                    st.emoji.tones_open = true;
                    picture(&st, output, scheme, "emoji", "recent-tones");
                    st.emoji.tones_open = false;
                    st.emoji.search.insert("heart");
                    st.focus = Some(Action::Emoji(emoji::Act::Item(2)));
                    picture(&st, output, scheme, "emoji", "search");
                    st.emoji.search.clear();
                    st.emoji.group = emoji::tab_groups(emoji::Tab::Emoji).find(|(_, g)| g.key == "people").map(|(i, _)| i).unwrap();
                    st.emoji.scroll = 3;
                    picture(&st, output, scheme, "emoji", "people-scrolled");
                }
            }
        }
        crate::ui::set_text_scale(225);
        let mut st = State::for_tests();
        with_items(&mut st);
        picture(&st, (1366.0, 768.0), Scheme::Dark, "clipboard", "items");
        st.emoji.reset();
        picture(&st, (1366.0, 768.0), Scheme::Dark, "emoji", "emoji");
        st.emoji.tab = emoji::Tab::Kaomoji;
        st.emoji.reset();
        picture(&st, (1366.0, 768.0), Scheme::Light, "emoji", "kaomoji");
        crate::ui::set_text_scale(100);
    }

    #[test]
    fn cards_fit_their_text_and_pictures() {
        let mut res = Resources::new(boswas_ui::Theme::of(Scheme::Dark));
        let short = Clip { id: 1, content: Content::Text("Short".into()), pinned: false, copied_at: 0 };
        let long = Clip { id: 2, content: Content::Text("word ".repeat(200)), pinned: false, copied_at: 0 };
        let picture = Clip { id: 3, content: Content::Image { png: Default::default(), width: 4000, height: 1000, thumbnail: None }, pinned: false, copied_at: 0 };
        let one = card_height(&mut res, &short, 360.0);
        let three = card_height(&mut res, &long, 360.0);
        assert!(three > one && three <= one + 2.0 * ramp("body", WHITE).line_height() + 0.5, "{one} {three}: three lines at most");
        let (w, h) = picture_size((4000, 1000), 260.0);
        assert!((w - 260.0).abs() < 0.5 && (h - 65.0).abs() < 0.5, "{w}x{h}");
        assert!(card_height(&mut res, &picture, 360.0) > one);
        assert_eq!(preview("\n\n  first\n\nsecond\nthird\nfourth"), "  first\nsecond\nthird");
    }
}
