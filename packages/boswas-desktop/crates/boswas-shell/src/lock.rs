//! The lock screen and the login greeter share one view: a clock, the
//! user, and a password field over the desktop background.

use boswas_ui::{
    text::{Face, TextStyle},
    Canvas,
};

use crate::{
    status::Status,
    ui::{Frame, Rect, TextField},
    wallpaper::Background,
};

/// Most dots shown in the password field, so its length is not on display.
const MAX_DOTS: usize = 16;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LockAction {
    Submit,
    /// Greeter: put the cursor in the user name field.
    FocusUser,
    /// Greeter: put the cursor in the password field.
    FocusPassword,
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

/// What the lock screen or greeter shows.
#[derive(Default)]
pub struct AuthView {
    pub password: TextField,
    /// Greeter only: the login name being typed. The lock screen shows the
    /// session's user instead.
    pub user_field: Option<TextField>,
    /// Whether keys go to the user name field (greeter).
    pub editing_user: bool,
    pub state: AuthState,
    pub caps_lock: bool,
    /// Name shown above the password field (lock screen).
    pub display_name: String,
    /// Line under the password field when nothing else is to be said.
    pub hint: String,
}

impl AuthView {
    /// The field keys currently go to.
    pub fn field(&mut self) -> &mut TextField {
        match (&mut self.user_field, self.editing_user) {
            (Some(user), true) => user,
            _ => &mut self.password,
        }
    }
}

fn pill(canvas: &mut Canvas, rect: Rect, focused: bool, theme: &boswas_ui::Theme) {
    canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, rect.h / 2.0, theme.surface_raised.alpha(0.92));
    let (width, color) = if focused { (1.5, theme.focus) } else { (1.0, theme.border) };
    canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, rect.h / 2.0, width, color);
}

/// Draws the view. `interactive` is false on secondary outputs, which show
/// the clock only.
pub fn draw(
    frame: &mut Frame<LockAction>,
    view: &AuthView,
    status: &Status,
    background: &mut Background,
    interactive: bool,
) {
    let theme = frame.theme();
    let (w, h) = (frame.canvas.width(), frame.canvas.height());
    background.paint(frame.canvas, theme);
    frame.canvas.fill_rect(0.0, 0.0, w, h, theme.background.alpha(0.55));

    let centre = w / 2.0;
    let mut y = (h * 0.16).max(24.0);
    let clock = TextStyle::new((h * 0.09).clamp(40.0, 96.0), theme.text).weight(500).face(Face::Display);
    frame.text_centered(&status.time, Rect::new(0.0, y, w, clock.line_height()), &clock);
    y += clock.line_height() + 4.0;
    let date = TextStyle::new(18.0, theme.text_secondary);
    frame.text_centered(&status.date, Rect::new(0.0, y, w, date.line_height()), &date);
    if !interactive {
        return;
    }

    let field_w = 300.0_f32.min(w - 48.0);
    let mut y = (h * 0.50).max(y + 60.0);
    frame.canvas.fill_circle(centre, y, 40.0, theme.surface_raised.alpha(0.92));
    frame.canvas.stroke_circle(centre, y, 40.0, 1.0, theme.border);
    frame.icon("avatar-default-symbolic", centre - 20.0, y - 20.0, 40.0);
    y += 56.0;

    let field_text = TextStyle::new(14.0, theme.text);
    let placeholder = TextStyle::new(14.0, theme.text_muted);
    let line = field_text.line_height();
    match &view.user_field {
        Some(user) => {
            let rect = Rect::new(centre - field_w / 2.0, y, field_w, 40.0);
            frame.hit(rect, LockAction::FocusUser);
            pill(frame.canvas, rect, view.editing_user, theme);
            if user.is_empty() {
                frame.text("User name", rect.x + 18.0, rect.y + (40.0 - line) / 2.0, None, &placeholder);
            } else {
                let drawn = frame.text(user.text(), rect.x + 18.0, rect.y + (40.0 - line) / 2.0, Some(field_w - 40.0), &field_text);
                if view.editing_user {
                    frame.canvas.fill_rect(rect.x + 20.0 + drawn, rect.y + 11.0, 1.5, 18.0, theme.focus);
                }
            }
            y += 52.0;
        }
        None => {
            let name = TextStyle::new(18.0, theme.text).weight(600);
            frame.text_centered(&view.display_name, Rect::new(24.0, y, w - 48.0, name.line_height()), &name);
            y += name.line_height() + 14.0;
        }
    }

    // Password field: one dot per character, capped.
    let rect = Rect::new(centre - field_w / 2.0, y, field_w, 40.0);
    frame.hit(rect, LockAction::FocusPassword);
    pill(frame.canvas, rect, !view.editing_user, theme);
    let dots = view.password.len().min(MAX_DOTS);
    if dots == 0 {
        let text = if view.state == AuthState::Checking { "Checking…" } else { "Password" };
        frame.text(text, rect.x + 18.0, rect.y + (40.0 - line) / 2.0, None, &placeholder);
    } else {
        for i in 0..dots {
            frame.canvas.fill_circle(rect.x + 24.0 + i as f32 * 14.0, rect.y + 20.0, 4.0, theme.text);
        }
    }
    let submit = Rect::new(rect.x + rect.w - 36.0, rect.y + 4.0, 32.0, 32.0);
    let hovered = frame.hit(submit, LockAction::Submit);
    let fill = if hovered { theme.accent_hover } else { theme.accent };
    frame.canvas.fill_circle(submit.x + 16.0, submit.y + 16.0, 16.0, fill);
    frame.icon("go-next-symbolic", submit.x + 7.0, submit.y + 7.0, 18.0);
    y += 54.0;

    let (message, color) = match &view.state {
        AuthState::Failed(message) => (message.as_str(), theme.danger),
        AuthState::Checking => ("", theme.text_muted),
        AuthState::Idle => (view.hint.as_str(), theme.text_secondary),
    };
    let message_style = TextStyle::new(13.0, color);
    frame.text_centered(message, Rect::new(24.0, y, w - 48.0, message_style.line_height()), &message_style);
    y += message_style.line_height() + 6.0;
    if view.caps_lock {
        let caps = TextStyle::new(13.0, theme.warning);
        let (cw, ch) = frame.res.text.measure("Caps Lock is on", &caps);
        frame.icon("caps-lock-symbolic", centre - cw / 2.0 - 22.0, y + (ch - 16.0) / 2.0, 16.0);
        frame.text("Caps Lock is on", centre - cw / 2.0, y, None, &caps);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{status::Network, ui::Resources};
    use boswas_ui::Theme;

    fn status() -> Status {
        Status {
            time: "09:41".into(),
            date: "Mon 16 Nov".into(),
            battery: None,
            network: Network::Offline,
        }
    }

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
        let mut lock = AuthView::default();
        lock.editing_user = true;
        lock.field().insert("pw");
        assert_eq!(lock.password.text(), "pw", "without a user field keys are always the password");
    }

    #[test]
    fn the_view_draws_and_registers_its_controls() {
        let mut res = Resources::new(Theme::dark());
        let mut canvas = Canvas::new(800.0, 600.0, 1.0).unwrap();
        let mut hits = Vec::new();
        let mut background = Background::default();
        let mut view = AuthView {
            display_name: "Asha Rao".into(),
            state: AuthState::Failed("Incorrect password".into()),
            caps_lock: true,
            ..AuthView::default()
        };
        view.password.insert(&"x".repeat(40));
        let mut frame = Frame {
            canvas: &mut canvas,
            res: &mut res,
            pointer: None,
            hits: &mut hits,
        };
        draw(&mut frame, &view, &status(), &mut background, true);
        assert!(hits.iter().any(|(_, a)| *a == LockAction::Submit));
        assert!(canvas.data().chunks_exact(4).all(|p| p[3] == 255), "the lock screen is opaque everywhere");

        let mut hits = Vec::new();
        let mut frame = Frame {
            canvas: &mut canvas,
            res: &mut res,
            pointer: None,
            hits: &mut hits,
        };
        draw(&mut frame, &view, &status(), &mut background, false);
        assert!(hits.is_empty(), "secondary outputs take no input");
    }
}
