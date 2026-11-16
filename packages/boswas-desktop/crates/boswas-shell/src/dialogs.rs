//! Dialogs that dim the desktop and take the keyboard: authentication for
//! polkit, the confirmation before signing out, restarting or shutting
//! down, and Run.

use boswas_services::{AuthProgress, AuthRequest};
use boswas_ui::tokens::tokens;

use crate::{
    action::Action,
    state::State,
    ui::{ramp, ButtonKind, Frame, Rect, TextField},
};

#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    /// The dialog's default button.
    Primary,
    Cancel,
    /// The text field (puts the keyboard focus there).
    Field,
    Reveal,
    /// Authenticate as this one of the request's users (an index).
    User(usize),
}

/// Ways of leaving the session that are confirmed first.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Leave {
    SignOut,
    Restart,
    ShutDown,
}

impl Leave {
    fn title(self) -> &'static str {
        match self {
            Leave::SignOut => "Sign out?",
            Leave::Restart => "Restart this device?",
            Leave::ShutDown => "Shut down this device?",
        }
    }

    fn verb(self) -> &'static str {
        match self {
            Leave::SignOut => "Sign out",
            Leave::Restart => "Restart",
            Leave::ShutDown => "Shut down",
        }
    }
}

/// Users offered side by side in one row of the authentication dialog.
const USERS_PER_ROW: usize = 3;
/// Most users offered; a request rarely names more than one or two
/// administrators.
const MAX_USERS: usize = 9;

pub struct AuthDialog {
    pub request: AuthRequest,
    /// Which of the request's users is authenticating.
    pub user: usize,
    /// The helper's current prompt ("Password:"); empty until it asks.
    pub prompt: String,
    /// The prompt's answer may be shown as typed.
    pub echo: bool,
    pub field: TextField,
    pub reveal: bool,
    /// The last message of the attempt, and whether it is an error.
    pub message: Option<(String, bool)>,
    /// An answer was sent and the result is awaited.
    pub busy: bool,
}

impl AuthDialog {
    pub fn new(request: AuthRequest) -> Self {
        Self {
            request,
            user: 0,
            prompt: String::new(),
            echo: false,
            field: TextField::default(),
            reveal: false,
            message: None,
            busy: false,
        }
    }

    /// Applies what the authentication helper reported. Returns whether
    /// the dialog is finished and must close.
    pub fn progress(&mut self, progress: AuthProgress) -> bool {
        match progress {
            AuthProgress::Prompt { text, echo } => {
                self.prompt = text.chars().filter(|c| !c.is_control()).take(120).collect();
                self.echo = echo;
                self.busy = false;
                self.field.clear();
            }
            AuthProgress::Info(text) => self.message = Some((clip(&text), false)),
            AuthProgress::Failed(text) => {
                let text = if text.trim().is_empty() { "That did not work. Try again.".to_owned() } else { clip(&text) };
                self.message = Some((text, true));
                self.busy = false;
                self.field.clear();
            }
            AuthProgress::Done { .. } => return true,
        }
        false
    }

    /// Whether an answer can be sent now.
    pub fn ready(&self) -> bool {
        !self.busy && !self.prompt.is_empty() && !self.field.is_empty()
    }

    /// The users that can be chosen (none when there is no choice).
    pub fn choices(&self) -> &[String] {
        if self.request.users.len() > 1 {
            &self.request.users[..self.request.users.len().min(MAX_USERS)]
        } else {
            &[]
        }
    }

    /// Rows the user chooser takes.
    fn choice_rows(&self) -> usize {
        self.choices().len().div_ceil(USERS_PER_ROW)
    }

    /// Switches to another of the request's users. Returns the name to tell
    /// the authentication service, or `None` when nothing changes. The
    /// attempt starts over: what was typed is wiped and the dialog waits
    /// for that user's prompt.
    pub fn choose(&mut self, index: usize) -> Option<String> {
        if self.busy || index == self.user {
            return None;
        }
        let name = self.choices().get(index)?.clone();
        self.user = index;
        self.prompt.clear();
        self.field.clear();
        self.reveal = false;
        self.message = None;
        Some(name)
    }
}

fn clip(text: &str) -> String {
    text.chars().filter(|c| !c.is_control() || *c == '\n').take(240).collect()
}

pub struct RunDialog {
    pub field: TextField,
    pub error: Option<String>,
    /// Position in the history while browsing it with the arrow keys.
    pub history: Option<usize>,
}

impl RunDialog {
    pub fn new(last: Option<&str>) -> Self {
        let mut field = TextField::default();
        if let Some(last) = last {
            field.set(last);
        }
        Self {
            field,
            error: None,
            history: None,
        }
    }

    /// Moves through the history: `older` goes back in time.
    pub fn browse(&mut self, entries: &[String], older: bool) {
        if entries.is_empty() {
            return;
        }
        let position = match (self.history, older) {
            (None, true) => entries.len() - 1,
            (None, false) => return,
            (Some(p), true) => p.saturating_sub(1),
            (Some(p), false) if p + 1 < entries.len() => p + 1,
            (Some(_), false) => {
                self.history = None;
                self.field.clear();
                return;
            }
        };
        self.history = Some(position);
        self.field.set(&entries[position]);
    }
}

pub enum Dialog {
    Auth(AuthDialog),
    Confirm { leave: Leave, open_windows: usize },
    Run(RunDialog),
    /// A program asks to see the screen (`id` is the compositor's question).
    Screen { id: u64, program: String, opened: std::time::Instant },
}

/// How long a screen capture question must have been on screen before
/// "Allow" works: a dialog that appears under a click or a key press that
/// was meant for something else must not be answered by it.
pub const SCREEN_GUARD: std::time::Duration = std::time::Duration::from_millis(700);

impl Dialog {
    /// The dialog's panel size.
    pub fn size(&self) -> (f32, f32) {
        match self {
            Dialog::Auth(auth) => (440.0, 300.0 + 38.0 * auth.choice_rows() as f32),
            Dialog::Confirm { .. } => (400.0, 176.0),
            Dialog::Screen { .. } => (440.0, 232.0),
            Dialog::Run(_) => (420.0, 204.0),
        }
    }

    /// Whether the dialog sits at the bottom left (Run) instead of the
    /// centre of the output.
    pub fn at_corner(&self) -> bool {
        matches!(self, Dialog::Run(_))
    }

    /// The field typed text goes to, if the dialog has one.
    pub fn field(&mut self) -> Option<&mut TextField> {
        match self {
            Dialog::Auth(auth) if !auth.busy && !auth.prompt.is_empty() => Some(&mut auth.field),
            Dialog::Run(run) => Some(&mut run.field),
            _ => None,
        }
    }
}

/// Splits a Run command into program and arguments: words separated by
/// spaces, with double quotes around words that contain spaces. Nothing is
/// expanded or interpreted; there is no shell.
pub fn split_command(text: &str) -> Option<Vec<String>> {
    let mut args = Vec::new();
    let mut current = String::new();
    let mut has = false;
    let mut quoted = false;
    for c in text.trim().chars() {
        match (c, quoted) {
            ('"', _) => {
                quoted = !quoted;
                has = true;
            }
            (c, false) if c.is_whitespace() => {
                if has {
                    args.push(std::mem::take(&mut current));
                    has = false;
                }
            }
            (c, _) if c.is_control() => return None,
            (c, _) => {
                current.push(c);
                has = true;
            }
        }
    }
    if quoted {
        return None;
    }
    if has {
        args.push(current);
    }
    (!args.is_empty() && !args[0].is_empty()).then_some(args)
}

fn buttons(f: &mut Frame, panel: Rect, primary: &str, enabled: bool) {
    let t = tokens();
    let footer = Rect::new(panel.x, panel.bottom() - 72.0, panel.w, 72.0);
    let tint = boswas_ui::Rgba::BLACK.alpha(if f.theme().scheme == boswas_ui::Scheme::Dark { 0.16 } else { 0.03 });
    f.canvas.fill_rounded_rect4(footer.x + 1.0, footer.y, footer.w - 2.0, footer.h - 1.0, [0.0, 0.0, t.radius_flyout, t.radius_flyout], tint);
    f.divider(footer.x + 1.0, footer.y, footer.w - 2.0);
    let width = (panel.w - 48.0 - 8.0) / 2.0;
    f.button(Rect::new(panel.x + 24.0, footer.y + 20.0, width, 32.0), primary, ButtonKind::Accent, enabled, Action::Dialog(Act::Primary));
    f.button(Rect::new(panel.x + 24.0 + width + 8.0, footer.y + 20.0, width, 32.0), "Cancel", ButtonKind::Standard, true, Action::Dialog(Act::Cancel));
}

pub fn draw(f: &mut Frame, st: &State, dialog: &Dialog, panel: Rect) {
    let theme = f.theme();
    let t = tokens();
    let e = t.elevation("dialog");
    f.canvas.shadow(panel.x, panel.y, panel.w, panel.h, t.radius_flyout, e.blur.min(22.0), e.y.min(10.0), e.alpha);
    f.canvas.fill_rounded_rect(panel.x, panel.y, panel.w, panel.h, t.radius_flyout, theme.surface);
    let stroke = f.res.stroke("surface");
    f.canvas.stroke_rounded_rect(panel.x, panel.y, panel.w, panel.h, t.radius_flyout, 1.0, stroke);
    f.area(panel, Action::None);
    let x = panel.x + 24.0;
    let w = panel.w - 48.0;
    match dialog {
        Dialog::Auth(auth) => {
            f.icon_or_placeholder(&["dialog-password-symbolic"], x, panel.y + 26.0, 20.0);
            f.text_left("Authentication required", Rect::new(x + 30.0, panel.y + 20.0, w - 30.0, 32.0), &ramp("subtitle", theme.text));
            let mut y = panel.y + 62.0;
            let message = if auth.request.message.trim().is_empty() { "An application needs administrator rights." } else { &auth.request.message };
            y += f.text_wrapped(message, x, y, w, 3, &ramp("body", theme.text));
            // The action that is being authorised, as polkit names it.
            f.text_left(&auth.request.action_id, Rect::new(x, y + 2.0, w, 18.0), &ramp("caption", theme.text_muted));
            y += 26.0;
            let user = auth.request.users.get(auth.user).map_or(st.user.as_str(), String::as_str);
            f.text_left(&format!("Authenticating as {user}"), Rect::new(x, y, w, 18.0), &ramp("caption", theme.text_secondary));
            y += 22.0;
            // Several administrators may authorise this: choose who types
            // a password.
            let choices = auth.choices();
            if !choices.is_empty() {
                let gap = 8.0;
                let width = (w - gap * (USERS_PER_ROW as f32 - 1.0)) / USERS_PER_ROW as f32;
                for (index, name) in choices.iter().enumerate() {
                    let (row, column) = (index / USERS_PER_ROW, index % USERS_PER_ROW);
                    let rect = Rect::new(x + column as f32 * (width + gap), y + 2.0 + row as f32 * 38.0, width, 32.0);
                    let kind = if index == auth.user { ButtonKind::Accent } else { ButtonKind::Standard };
                    f.button(rect, name, kind, !auth.busy, Action::Dialog(Act::User(index)));
                }
                y += 38.0 * auth.choice_rows() as f32;
            }
            let field = Rect::new(x, y + 4.0, w, 32.0);
            f.area(field, Action::Dialog(Act::Field));
            let placeholder = if auth.busy {
                "Checking…"
            } else if auth.prompt.is_empty() {
                "Waiting for the authentication service…"
            } else {
                auth.prompt.trim_end_matches([':', ' '])
            };
            let masked = !auth.echo && !auth.reveal;
            f.text_box(field, &auth.field, placeholder, !auth.busy && !auth.prompt.is_empty(), masked, 10.0, 40.0);
            if !auth.echo {
                let eye = if auth.reveal { "eye-closed-symbolic" } else { "eye-open-symbolic" };
                f.icon_button(Rect::new(field.right() - 34.0, field.y + 2.0, 28.0, 28.0), eye, if auth.reveal { "Hide" } else { "Show" }, Action::Dialog(Act::Reveal));
            }
            if let Some((text, error)) = &auth.message {
                let color = if *error { theme.danger } else { theme.text_secondary };
                f.text_wrapped(text, x, field.bottom() + 6.0, w, 2, &ramp("caption", color));
            }
            buttons(f, panel, "Authenticate", auth.ready());
        }
        Dialog::Confirm { leave, open_windows } => {
            f.text_left(leave.title(), Rect::new(x, panel.y + 20.0, w, 32.0), &ramp("subtitle", theme.text));
            let text = match open_windows {
                0 => "Make sure your work is saved.".to_owned(),
                1 => "1 window is open. Work that is not saved will be lost.".to_owned(),
                n => format!("{n} windows are open. Work that is not saved will be lost."),
            };
            f.text_wrapped(&text, x, panel.y + 60.0, w, 2, &ramp("body", theme.text_secondary));
            buttons(f, panel, leave.verb(), true);
        }
        Dialog::Screen { program, .. } => {
            f.icon_or_placeholder(&["camera-video-symbolic", "video-display-symbolic"], x, panel.y + 26.0, 20.0);
            f.text_left("Allow screen capture?", Rect::new(x + 30.0, panel.y + 20.0, w - 30.0, 32.0), &ramp("subtitle", theme.text));
            let mut y = panel.y + 62.0;
            y += f.text_wrapped(&format!("{program} wants to see your screen."), x, y, w, 2, &ramp("body", theme.text));
            f.text_wrapped(
                "It will be able to take pictures of everything on it, including other applications, until it stops. A mark on the taskbar shows while it can, and stops it.",
                x,
                y + 6.0,
                w,
                4,
                &ramp("caption", theme.text_secondary),
            );
            let footer = Rect::new(panel.x, panel.bottom() - 72.0, panel.w, 72.0);
            let tint = boswas_ui::Rgba::BLACK.alpha(if theme.scheme == boswas_ui::Scheme::Dark { 0.16 } else { 0.03 });
            f.canvas.fill_rounded_rect4(footer.x + 1.0, footer.y, footer.w - 2.0, footer.h - 1.0, [0.0, 0.0, t.radius_flyout, t.radius_flyout], tint);
            f.divider(footer.x + 1.0, footer.y, footer.w - 2.0);
            let width = (panel.w - 48.0 - 8.0) / 2.0;
            // Neither button is the default: allowing is always a
            // deliberate click (or Tab and Enter).
            f.button(Rect::new(panel.x + 24.0, footer.y + 20.0, width, 32.0), "Allow", ButtonKind::Standard, true, Action::Dialog(Act::Primary));
            f.button(Rect::new(panel.x + 24.0 + width + 8.0, footer.y + 20.0, width, 32.0), "Don't allow", ButtonKind::Standard, true, Action::Dialog(Act::Cancel));
        }
        Dialog::Run(run) => {
            f.text_left("Run", Rect::new(x, panel.y + 16.0, w, 32.0), &ramp("subtitle", theme.text));
            f.text_wrapped("Type the name of an application or a command to start it.", x, panel.y + 52.0, w, 2, &ramp("caption", theme.text_secondary));
            let field = Rect::new(x, panel.y + 80.0, w, 32.0);
            f.area(field, Action::Dialog(Act::Field));
            f.text_box(field, &run.field, "Command", true, false, 10.0, 10.0);
            if let Some(error) = &run.error {
                f.text_left(error, Rect::new(x, field.bottom() + 2.0, w, 18.0), &ramp("caption", theme.danger));
            }
            buttons(f, panel, "OK", !run.field.text().trim().is_empty());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commands_are_split_without_a_shell() {
        assert_eq!(split_command("konsole").unwrap(), ["konsole"]);
        assert_eq!(split_command("  firefox-esr   --private-window  ").unwrap(), ["firefox-esr", "--private-window"]);
        assert_eq!(split_command("kwrite \"my notes.txt\" other").unwrap(), ["kwrite", "my notes.txt", "other"]);
        assert_eq!(split_command("echo $HOME; rm -rf ~").unwrap(), ["echo", "$HOME;", "rm", "-rf", "~"], "nothing is expanded or chained");
        assert!(split_command("").is_none());
        assert!(split_command("   ").is_none());
        assert!(split_command("unterminated \"quote").is_none());
        assert!(split_command("\"\"").is_none(), "an empty program name");
        assert!(split_command("a\u{7}b").is_none());
    }

    fn request() -> AuthRequest {
        AuthRequest {
            cookie: "c1".into(),
            action_id: "org.boswas.sysops.install".into(),
            message: "Authentication is required to install software".into(),
            icon_name: String::new(),
            users: vec!["asha".into()],
        }
    }

    #[test]
    fn the_authentication_dialog_follows_the_helper() {
        let mut auth = AuthDialog::new(request());
        assert!(!auth.ready(), "nothing can be sent before the helper asks");
        assert!(!auth.progress(AuthProgress::Prompt { text: "Password: ".into(), echo: false }));
        auth.field.insert("wrong");
        assert!(auth.ready());
        auth.busy = true;
        assert!(!auth.ready());
        assert!(!auth.progress(AuthProgress::Failed(String::new())));
        assert!(auth.field.is_empty(), "a failed attempt wipes what was typed");
        assert_eq!(auth.message.as_ref().map(|m| m.1), Some(true));
        assert!(!auth.busy);
        assert!(!auth.progress(AuthProgress::Info("Touch the sensor\u{1b}[0m".into())));
        assert_eq!(auth.message.as_ref().unwrap().0, "Touch the sensor[0m");
        assert!(auth.progress(AuthProgress::Done { authorized: true }));
        let mut dialog = Dialog::Auth(AuthDialog::new(request()));
        assert!(dialog.field().is_none(), "typing goes nowhere until there is a prompt");
    }

    #[test]
    fn another_administrator_can_be_chosen() {
        let single = AuthDialog::new(request());
        assert!(single.choices().is_empty(), "one user: nothing to choose");
        assert_eq!(Dialog::Auth(single).size(), (440.0, 300.0));

        let mut several = request();
        several.users = vec!["asha".into(), "ravi".into(), "meera".into(), "root".into()];
        let mut auth = AuthDialog::new(several);
        assert_eq!(auth.choices().len(), 4);
        auth.progress(AuthProgress::Prompt { text: "Password: ".into(), echo: false });
        auth.field.insert("half typed");
        auth.reveal = true;
        assert_eq!(auth.choose(0), None, "already that user");
        assert_eq!(auth.choose(9), None, "not one of the request's users");
        assert_eq!(auth.choose(1).as_deref(), Some("ravi"));
        assert_eq!(auth.user, 1);
        assert!(auth.field.is_empty(), "what was typed for the other user is wiped");
        assert!(!auth.reveal);
        assert!(!auth.ready(), "waits for the new user's prompt");
        auth.progress(AuthProgress::Prompt { text: "Password: ".into(), echo: false });
        auth.field.insert("secret");
        auth.busy = true;
        assert_eq!(auth.choose(2), None, "not while an answer is being checked");
        // Two rows of users make the dialog taller.
        assert_eq!(Dialog::Auth(auth).size(), (440.0, 300.0 + 2.0 * 38.0));

        let mut many = request();
        many.users = (0..40).map(|i| format!("admin{i}")).collect();
        let auth = AuthDialog::new(many);
        assert_eq!(auth.choices().len(), MAX_USERS);
    }

    #[test]
    fn run_history_is_browsed_with_the_arrows() {
        let entries = vec!["first".to_owned(), "second".to_owned(), "third".to_owned()];
        let mut run = RunDialog::new(None);
        run.browse(&entries, false);
        assert!(run.field.is_empty());
        run.browse(&entries, true);
        assert_eq!(run.field.text(), "third");
        run.browse(&entries, true);
        run.browse(&entries, true);
        run.browse(&entries, true);
        assert_eq!(run.field.text(), "first", "stops at the oldest");
        run.browse(&entries, false);
        assert_eq!(run.field.text(), "second");
        run.browse(&entries, false);
        run.browse(&entries, false);
        assert!(run.field.is_empty() && run.history.is_none(), "past the newest is an empty line");
        run.browse(&[], true);
        assert_eq!(RunDialog::new(Some("last")).field.text(), "last");
    }
}
