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

pub struct AuthDialog {
    pub request: AuthRequest,
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
}

impl Dialog {
    /// The dialog's panel size.
    pub fn size(&self) -> (f32, f32) {
        match self {
            Dialog::Auth(auth) => (440.0, 300.0 + if auth.request.users.len() > 1 { 22.0 } else { 0.0 }),
            Dialog::Confirm { .. } => (400.0, 176.0),
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
            let user = auth.request.users.first().map_or(st.user.as_str(), String::as_str);
            f.text_left(&format!("Authenticating as {user}"), Rect::new(x, y, w, 18.0), &ramp("caption", theme.text_secondary));
            y += 22.0;
            if auth.request.users.len() > 1 {
                let others = auth.request.users[1..].join(", ");
                f.text_left(&format!("Also allowed: {others}"), Rect::new(x, y, w, 18.0), &ramp("caption", theme.text_muted));
                y += 22.0;
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
