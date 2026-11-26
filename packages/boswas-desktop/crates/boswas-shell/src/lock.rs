//! The lock screen and the login greeter: first the clock over the
//! wallpaper (on the lock screen with the weather card, `weather.rs`), then
//! (after any key or a click) the sign-in view.

use boswas_ui::{
    text::TextStyle,
    tokens::{tokens, Material},
    Rgba,
};

use crate::{
    action::Action,
    glyph::Glyph,
    state::State,
    ui::{ramp, Frame, Rect, TextField},
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Act {
    /// Leave the clock view for the sign-in view.
    Wake,
    Submit,
    /// Greeter: put the caret in the user name field.
    FocusUser,
    FocusPassword,
    Reveal,
    /// Greeter: choose one of the listed users.
    User(usize),
    /// Greeter: type a user name instead.
    OtherUser,
    /// Greeter: the power menu.
    Power,
}

#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub enum AuthState {
    #[default]
    Idle,
    /// The password is being checked.
    Checking,
    /// The last attempt failed; the text is shown to the user.
    Failed(String),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Stage {
    /// The clock and the date over the wallpaper.
    #[default]
    Clock,
    SignIn,
}

/// What the lock screen or greeter shows.
#[derive(Default)]
pub struct AuthView {
    pub stage: Stage,
    pub password: TextField,
    pub reveal: bool,
    /// Greeter only: the login name being typed ("Other user").
    pub user_field: Option<TextField>,
    /// Whether keys go to the user name field.
    pub editing_user: bool,
    pub state: AuthState,
    pub caps_lock: bool,
    /// Name shown above the password field.
    pub display_name: String,
    /// Line under the password field when nothing else is to be said.
    pub hint: String,
    /// Greeter only: the people who can sign in (login name, full name).
    pub users: Vec<(String, String)>,
    /// Greeter only: the chosen entry of `users`.
    pub selected: Option<usize>,
    /// This is the greeter (it offers the power menu and the user list).
    pub greeter: bool,
}

impl AuthView {
    /// The field keys currently go to.
    pub fn field(&mut self) -> &mut TextField {
        match (&mut self.user_field, self.editing_user) {
            (Some(user), true) => user,
            _ => &mut self.password,
        }
    }

    /// Greeter: the login name the password is for.
    pub fn login_name(&self) -> String {
        match (&self.user_field, self.selected) {
            (Some(field), _) => field.text().trim().to_owned(),
            (None, Some(index)) => self.users.get(index).map(|u| u.0.clone()).unwrap_or_default(),
            (None, None) => String::new(),
        }
    }

    /// Greeter: chooses a listed user.
    pub fn select(&mut self, index: usize) {
        if let Some((login, name)) = self.users.get(index) {
            self.selected = Some(index);
            self.display_name = if name.is_empty() { login.clone() } else { name.clone() };
            self.user_field = None;
            self.editing_user = false;
            self.password.clear();
            self.reveal = false;
            self.state = AuthState::Idle;
        }
    }

    /// Greeter: switches to typing a user name.
    pub fn other_user(&mut self) {
        self.selected = None;
        self.display_name = "Other user".into();
        self.user_field = Some(TextField::default());
        self.editing_user = true;
        self.password.clear();
        self.reveal = false;
        self.state = AuthState::Idle;
    }

    /// Back to the clock: what was typed is wiped.
    pub fn sleep(&mut self) {
        self.stage = Stage::Clock;
        self.password.clear();
        self.reveal = false;
        if matches!(self.state, AuthState::Failed(_)) {
            self.state = AuthState::Idle;
        }
    }
}

/// The people who can sign in, from the text of `/etc/passwd`: regular
/// accounts (uid 1000 to 59999) with a login shell. At most eight.
pub fn login_users(passwd: &str) -> Vec<(String, String)> {
    passwd
        .lines()
        .filter_map(|line| {
            let fields: Vec<&str> = line.split(':').collect();
            if fields.len() < 7 {
                return None;
            }
            let uid: u32 = fields[2].parse().ok()?;
            let shell = fields[6].trim();
            if !(1000..60000).contains(&uid) || shell.ends_with("nologin") || shell.ends_with("/false") || fields[0].is_empty() {
                return None;
            }
            let name = fields[4].split(',').next().unwrap_or("").trim();
            Some((fields[0].to_owned(), name.to_owned()))
        })
        .take(8)
        .collect()
}

fn status_icons(f: &mut Frame, st: &State, right: f32, y: f32) -> f32 {
    let mut x = right;
    let mut icons: Vec<&str> = Vec::new();
    if st.sys.network.available {
        icons.push(st.sys.network_icon());
    } else {
        icons.push(st.status.network.icon());
    }
    if let Some(icon) = st.sys.battery_icon().or(st.status.battery.as_ref().map(|b| b.icon())) {
        icons.push(icon);
    }
    for icon in icons.into_iter().rev() {
        x -= 20.0;
        f.icon_tinted(icon, x, y, 20.0, Rgba::WHITE);
        x -= 14.0;
    }
    if let Some(layout) = st.keyboard_layout() {
        let label: String = layout.chars().take(3).collect::<String>().to_uppercase();
        x -= 34.0;
        f.text_centered(&label, Rect::new(x, y - 2.0, 34.0, 24.0), &ramp("body-strong", Rgba::WHITE));
    }
    x
}

/// The weather card of the clock view at `now` (seconds since the epoch),
/// if any: only on the lock screen (the login screen runs before anyone's
/// files are known), only once the session's settings arrived and allow
/// it, and only while the summary is recent.
pub fn weather_shown(st: &State, now: i64) -> Option<crate::weather::Shown<'_>> {
    if st.auth.greeter || !st.desktop_known || !st.desktop.lock_weather {
        return None;
    }
    st.weather.summary.as_ref()?.shown(now)
}

/// Draws the view. `interactive` is false on secondary outputs, which show
/// the clock and the date only.
pub fn draw(f: &mut Frame, st: &State, interactive: bool) {
    let view = &st.auth;
    let theme = f.theme();
    let (w, h) = (f.canvas.width(), f.canvas.height());
    let full = Rect::new(0.0, 0.0, w, h);
    let signing_in = interactive && view.stage == Stage::SignIn;
    // The caller painted the wallpaper; it is blurred and darkened behind
    // the sign-in view.
    if signing_in {
        let material = Material {
            tint: Rgba::BLACK,
            tint_opacity: 0.42,
            blur: 64.0,
        };
        match f.res.backdrop.as_mut() {
            Some(backdrop) => backdrop.fill(f.canvas, 0.0, 0.0, w, h, [0.0; 4], (0.0, 0.0), material),
            None => f.canvas.fill_rect(0.0, 0.0, w, h, Rgba::BLACK.alpha(0.55)),
        }
    } else {
        f.canvas.fill_rect(0.0, 0.0, w, h, Rgba::BLACK.alpha(0.22));
    }

    if !signing_in {
        if interactive {
            f.area(full, Action::Lock(Act::Wake));
        }
        let size = (h * 0.13).clamp(56.0, 132.0);
        let clock = TextStyle::new(size, Rgba::WHITE).weight(600);
        let y = (h * 0.16).max(24.0);
        f.text_centered(&st.status.time, Rect::new(0.0, y, w, clock.line_height()), &clock);
        let date = TextStyle::new((size * 0.24).clamp(18.0, 28.0), Rgba::WHITE).weight(500);
        let long_date = chrono::Local::now().format("%A, %-d %B").to_string();
        let date_y = y + clock.line_height() + 2.0;
        f.text_centered(&long_date, Rect::new(0.0, date_y, w, date.line_height()), &date);
        if interactive {
            let hint = ramp("body", Rgba::WHITE.alpha(0.85));
            let text = if view.greeter { "Press any key to sign in" } else { "Press any key to unlock" };
            f.text_centered(text, Rect::new(0.0, h - 72.0, w, hint.line_height()), &hint);
            status_icons(f, st, w - 28.0, h - 48.0);
            if let Some(shown) = weather_shown(st, chrono::Utc::now().timestamp()) {
                let hint_w = f.res.text.measure(text, &hint).0.min(w);
                let room = crate::weather::Room {
                    width: w,
                    height: h,
                    top: date_y + date.line_height(),
                    hint: Rect::new((w - hint_w) / 2.0, h - 72.0, hint_w, hint.line_height()),
                };
                crate::weather::draw(f, &shown, room);
            }
        }
        return draw_osd(f, st, interactive);
    }

    let radius = tokens().radius_control;
    let centre = w / 2.0;
    let field_w = 296.0_f32.min(w - 48.0);
    let mut y = (h * 0.24).max(24.0);
    // Avatar (the account picture, where there is one) and name.
    let login = if view.greeter { view.login_name() } else { st.user.clone() };
    if !crate::accounts::draw(f, st.pictures.get(&login), centre, y + 60.0, 60.0) {
        f.canvas.fill_circle(centre, y + 60.0, 60.0, Rgba::WHITE.alpha(0.16));
        f.canvas.stroke_circle(centre, y + 60.0, 59.5, 1.0, Rgba::WHITE.alpha(0.22));
        f.icon_tinted("avatar-default-symbolic", centre - 28.0, y + 32.0, 56.0, Rgba::WHITE);
    }
    y += 136.0;
    let name = ramp("title", Rgba::WHITE).weight(500);
    f.text_centered(&view.display_name, Rect::new(24.0, y, w - 48.0, name.line_height()), &name);
    y += name.line_height() + 20.0;

    let x = centre - field_w / 2.0;
    if let Some(user) = &view.user_field {
        let rect = Rect::new(x, y, field_w, 36.0);
        f.area(rect, Action::Lock(Act::FocusUser));
        f.text_box(rect, user, "User name", view.editing_user, false, 12.0, 12.0);
        y += 44.0;
    }
    // Password: reveal and submit sit inside the box.
    let rect = Rect::new(x, y, field_w, 36.0);
    f.area(rect, Action::Lock(Act::FocusPassword));
    let checking = view.state == AuthState::Checking;
    let placeholder = if checking { "Checking…" } else { "Password" };
    f.text_box(rect, &view.password, placeholder, !view.editing_user && !checking, !view.reveal, 12.0, 76.0);
    if !view.password.is_empty() {
        let eye = if view.reveal { "eye-closed-symbolic" } else { "eye-open-symbolic" };
        f.icon_button(Rect::new(rect.right() - 68.0, rect.y + 4.0, 28.0, 28.0), eye, if view.reveal { "Hide password" } else { "Show password" }, Action::Lock(Act::Reveal));
    }
    let submit = Rect::new(rect.right() - 34.0, rect.y + 4.0, 28.0, 28.0);
    let touch = f.hit(submit, Action::Lock(Act::Submit));
    f.tip("Submit");
    let fill = if touch.pressed {
        theme.accent_pressed
    } else if touch.hovered {
        theme.accent_hover
    } else {
        theme.accent
    };
    f.canvas.fill_rounded_rect(submit.x, submit.y, submit.w, submit.h, radius, fill);
    f.glyph(Glyph::ArrowRight, submit.inset(6.0), theme.on_accent);
    if touch.focused {
        f.focus_ring(submit, radius);
    }
    y += 48.0;

    let (message, color) = match &view.state {
        AuthState::Failed(message) => (message.as_str(), tokens().color("coral.300").unwrap_or(theme.danger)),
        AuthState::Checking => ("", Rgba::WHITE),
        AuthState::Idle => (view.hint.as_str(), Rgba::WHITE.alpha(0.85)),
    };
    let message_style = ramp("body", color);
    f.text_centered(message, Rect::new(24.0, y, w - 48.0, message_style.line_height()), &message_style);
    y += message_style.line_height() + 8.0;
    if view.caps_lock {
        let caps = ramp("body", tokens().color("sun.300").unwrap_or(theme.warning));
        let (cw, ch) = f.res.text.measure("Caps Lock is on", &caps);
        f.icon_tinted("caps-lock-symbolic", centre - cw / 2.0 - 24.0, y + (ch - 16.0) / 2.0, 16.0, caps.color);
        f.text("Caps Lock is on", centre - cw / 2.0, y, None, &caps);
    }

    // Greeter: the people who can sign in, bottom left.
    if view.greeter && (view.users.len() > 1 || view.user_field.is_some() || view.users.is_empty()) {
        let rows = view.users.len() + 1;
        let mut uy = h - 32.0 - rows as f32 * 48.0;
        let name_style = ramp("body", Rgba::WHITE);
        for index in 0..rows {
            let row = Rect::new(24.0, uy, 240.0_f32.min(w * 0.4), 44.0);
            let (label, action, selected) = match view.users.get(index) {
                Some((login, name)) => (if name.is_empty() { login.as_str() } else { name.as_str() }, Act::User(index), view.selected == Some(index)),
                None => ("Other user", Act::OtherUser, view.user_field.is_some()),
            };
            let touch = f.hit(row, Action::Lock(action));
            if selected || touch.hovered {
                f.canvas.fill_rounded_rect(row.x, row.y, row.w, row.h, radius, Rgba::WHITE.alpha(if touch.pressed { 0.10 } else { 0.16 }));
            }
            if touch.focused {
                f.focus_ring(row, radius);
            }
            let picture = view.users.get(index).and_then(|(login, _)| st.pictures.get(login));
            if !crate::accounts::draw(f, picture, row.x + 22.0, row.y + 22.0, 14.0) {
                f.canvas.fill_circle(row.x + 22.0, row.y + 22.0, 14.0, Rgba::WHITE.alpha(0.18));
                f.icon_tinted("avatar-default-symbolic", row.x + 14.0, row.y + 14.0, 16.0, Rgba::WHITE);
            }
            f.text_left(label, Rect::new(row.x + 46.0, row.y, row.w - 54.0, row.h), &name_style);
            uy += 48.0;
        }
    }

    // Bottom right: status, and the greeter's power button.
    let mut right = w - 28.0;
    if view.greeter {
        let power = Rect::new(right - 36.0, h - 56.0, 36.0, 36.0);
        let touch = f.hit(power, Action::Lock(Act::Power));
        f.tip("Power");
        if touch.hovered {
            f.canvas.fill_rounded_rect(power.x, power.y, power.w, power.h, radius, Rgba::WHITE.alpha(0.16));
        }
        if touch.focused {
            f.focus_ring(power, radius);
        }
        f.icon_tinted("system-shutdown-symbolic", power.x + 8.0, power.y + 8.0, 20.0, Rgba::WHITE);
        right = power.x - 16.0;
    }
    status_icons(f, st, right, h - 48.0);
    draw_osd(f, st, interactive);
}

/// The level of the volume and brightness keys, in the lock screen's own
/// surface: while the session is locked the compositor shows nothing else.
fn draw_osd(f: &mut Frame, st: &State, interactive: bool) {
    if let (true, Some(osd)) = (interactive, st.osd.as_ref()) {
        let (w, h) = (f.canvas.width(), f.canvas.height());
        crate::osd::paint(f, osd, crate::osd::auth_rect(w, h));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_go_to_the_focused_field() {
        let mut view = AuthView {
            user_field: Some(TextField::default()),
            editing_user: true,
            ..AuthView::default()
        };
        view.field().insert("asha");
        view.editing_user = false;
        view.field().insert("secret");
        assert_eq!(view.user_field.as_ref().unwrap().text(), "asha");
        assert_eq!(view.password.text(), "secret");
        assert_eq!(view.login_name(), "asha");
        let mut lock = AuthView::default();
        lock.editing_user = true;
        lock.field().insert("pw");
        assert_eq!(lock.password.text(), "pw", "without a user field keys are always the password");
    }

    #[test]
    fn only_people_who_can_log_in_are_listed() {
        let passwd = "root:x:0:0:root:/root:/bin/bash\n\
            daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n\
            asha:x:1000:1000:Asha Rao,,,:/home/asha:/bin/bash\n\
            build:x:1001:1001::/home/build:/usr/sbin/nologin\n\
            ravi:x:1002:1002:Ravi:/home/ravi:/bin/zsh\n\
            nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n\
            broken:line\n";
        assert_eq!(login_users(passwd), [("asha".to_owned(), "Asha Rao".to_owned()), ("ravi".to_owned(), "Ravi".to_owned())]);
        let many: String = (0..30).map(|i| format!("u{i}:x:{}:1::/home/u{i}:/bin/sh\n", 1000 + i)).collect();
        assert_eq!(login_users(&many).len(), 8);
        assert!(login_users("").is_empty());
    }

    #[test]
    fn choosing_a_user_wipes_what_was_typed() {
        let mut view = AuthView {
            users: vec![("asha".into(), "Asha Rao".into()), ("ravi".into(), String::new())],
            greeter: true,
            ..AuthView::default()
        };
        view.select(0);
        view.password.insert("secret");
        view.state = AuthState::Failed("Incorrect password".into());
        view.select(1);
        assert_eq!(view.display_name, "ravi", "the login name stands in for a missing full name");
        assert!(view.password.is_empty());
        assert_eq!(view.state, AuthState::Idle);
        assert_eq!(view.login_name(), "ravi");
        view.other_user();
        assert!(view.editing_user && view.user_field.is_some() && view.selected.is_none());
        view.field().insert(" guest ");
        assert_eq!(view.login_name(), "guest");
        view.select(7);
        assert!(view.user_field.is_some(), "an index that does not exist changes nothing");
        view.stage = Stage::SignIn;
        view.password.insert("x");
        view.sleep();
        assert!(view.stage == Stage::Clock && view.password.is_empty());
    }
}
