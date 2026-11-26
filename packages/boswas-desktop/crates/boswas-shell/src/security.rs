//! The security options screen (Ctrl+Alt+Del), as Windows has it: over the
//! whole screen, on the blurred wallpaper, a column of choices (Lock, Sign
//! out, Change a password, Task Manager) and Cancel, and the power button
//! at the bottom right. Escape cancels. Switching to another user is left
//! out: the login screen cannot keep a session running while another one
//! signs in.

use boswas_ui::tokens::tokens;
use smithay_client_toolkit::shell::wlr_layer::{KeyboardInteractivity, Layer};

use crate::{
    action::Action,
    dialogs::Leave,
    ui::{ramp, ButtonKind, Frame, Rect},
    Role, Shell,
};

/// The page of Settings where the password is changed: Accounts > Your
/// info, at "Your account" (which has Change password).
pub const PASSWORD_PAGE: [&str; 5] = ["boswas-control-center", "--page", "users", "--section", "your-account"];
const TASK_MANAGER: &str = "boswas-task-manager";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Act {
    Lock,
    SignOut,
    ChangePassword,
    TaskManager,
    Cancel,
    Power,
}

/// The open security screen.
#[derive(Clone, Debug, PartialEq)]
pub struct Security {
    /// Task Manager is installed.
    pub task_manager: bool,
}

/// The choices of the column, in order, with their labels.
pub fn choices(security: &Security) -> Vec<(Act, &'static str)> {
    let mut out = vec![(Act::Lock, "Lock"), (Act::SignOut, "Sign out"), (Act::ChangePassword, "Change a password")];
    if security.task_manager {
        out.push((Act::TaskManager, "Task Manager"));
    }
    out
}

const ROW_W: f32 = 320.0;
const ROW_H: f32 = 44.0;

/// Where the column starts in a screen `h` high, and where Cancel goes.
fn column(w: f32, h: f32, rows: usize) -> (Rect, Rect) {
    let width = ROW_W.min(w - 48.0).max(120.0);
    let height = rows as f32 * (ROW_H + 4.0);
    let x = ((w - width) / 2.0).round();
    let y = ((h - height) / 2.0 - 40.0).max(24.0).round();
    let cancel = Rect::new(((w - 120.0) / 2.0).round(), (y + height + 36.0).min(h - 120.0), 120.0, 36.0);
    (Rect::new(x, y, width, height), cancel)
}

/// The power button at the bottom right.
pub fn power_rect(w: f32, h: f32) -> Rect {
    Rect::new(w - 28.0 - 40.0, h - 28.0 - 40.0, 40.0, 40.0)
}

pub fn draw(f: &mut Frame, security: &Security) {
    let theme = f.theme();
    let (w, h) = (f.canvas.width(), f.canvas.height());
    let radius = tokens().radius_control;
    // The wallpaper, blurred and covered with the scheme's background.
    f.material(Rect::new(0.0, 0.0, w, h), [0.0; 4], "mica");
    f.area(Rect::new(0.0, 0.0, w, h), Action::None);
    let items = choices(security);
    let (col, cancel) = column(w, h, items.len());
    let style = ramp("subtitle", theme.text);
    for (row, (act, label)) in items.iter().enumerate() {
        let rect = Rect::new(col.x, col.y + row as f32 * (ROW_H + 4.0), col.w, ROW_H);
        let touch = f.hit(rect, Action::Security(*act));
        f.state_layer(rect, radius, touch, false);
        f.text_left(label, Rect::new(rect.x + 16.0, rect.y, rect.w - 32.0, rect.h), &style);
    }
    f.button(cancel, "Cancel", ButtonKind::Standard, true, Action::Security(Act::Cancel));
    f.icon_button(power_rect(w, h), "system-shutdown-symbolic", "Power", Action::Security(Act::Power));
}

impl Shell {
    /// Ctrl+Alt+Del: the security options over the whole screen.
    pub fn open_security(&mut self) {
        use crate::state::Mode;
        if self.st.mode != Mode::Desktop || self.st.locked || self.st.capture.is_some() || self.st.security.is_some() {
            return;
        }
        self.close_transients();
        self.leave_bar_focus();
        let Some(output) = self.primary_output() else {
            return;
        };
        self.st.security = Some(Security { task_manager: crate::apps::program_exists(TASK_MANAGER) });
        // From the keyboard, the first choice is chosen.
        self.st.focus = Some(Action::Security(Act::Lock));
        self.create_layer(Role::Security, &output, Self::full_screen(Layer::Overlay, "boswas-security", KeyboardInteractivity::Exclusive, -1));
    }

    pub fn close_security(&mut self) {
        if self.st.security.take().is_some() {
            self.remove(Role::Security);
            self.st.focus = None;
        }
    }

    pub fn security_action(&mut self, act: Act, index: usize) {
        match act {
            Act::Power => {
                let rect = self.surfaces.get(index).map(|s| (s.output.clone(), power_rect(s.width as f32, s.height as f32)));
                if let Some((output, rect)) = rect {
                    let items = self.power_menu();
                    self.open_menu(items, rect.right() - 180.0, rect.y - 8.0, true, &output);
                }
                return;
            }
            Act::Cancel => {}
            Act::Lock => {
                self.close_security();
                self.send_control("lock");
            }
            Act::SignOut => {
                self.close_security();
                self.leave(Leave::SignOut);
            }
            Act::ChangePassword => {
                self.close_security();
                let argv: Vec<String> = PASSWORD_PAGE.iter().map(|s| (*s).to_owned()).collect();
                self.spawn(&argv);
            }
            Act::TaskManager => {
                self.close_security();
                self.spawn(&[TASK_MANAGER.to_owned()]);
            }
        }
        self.close_security();
    }

    /// Keys while the security screen is open: Tab and the arrows go
    /// through the choices, Enter takes one, Escape cancels.
    pub fn security_key(&mut self, keysym: smithay_client_toolkit::seat::keyboard::Keysym, shift: bool) {
        use smithay_client_toolkit::seat::keyboard::Keysym;
        if !self.st.menus.is_empty() {
            return self.menu_key(keysym);
        }
        let Some(index) = self.surfaces.iter().position(|s| s.role == Role::Security) else {
            return;
        };
        let nav = match keysym {
            Keysym::Escape => return self.close_security(),
            Keysym::Return | Keysym::KP_Enter | Keysym::space => {
                if let Some(Action::Security(act)) = self.st.focus.clone() {
                    self.security_action(act, index);
                }
                return;
            }
            Keysym::Tab if shift => crate::ui::Nav::Previous,
            Keysym::ISO_Left_Tab | Keysym::Up => crate::ui::Nav::Previous,
            Keysym::Tab | Keysym::Down => crate::ui::Nav::Next,
            Keysym::Left => crate::ui::Nav::Left,
            Keysym::Right => crate::ui::Nav::Right,
            _ => return,
        };
        if let Some(action) = crate::ui::navigate(&self.surfaces[index].hits, self.st.focus.as_ref(), nav) {
            self.st.focus = Some(action);
            self.mark(Role::Security);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::testing;
    use boswas_ui::Scheme;

    #[test]
    fn the_choices_are_windows_and_task_manager_only_where_installed() {
        let with = Security { task_manager: true };
        let labels: Vec<&str> = choices(&with).iter().map(|c| c.1).collect();
        assert_eq!(labels, ["Lock", "Sign out", "Change a password", "Task Manager"]);
        assert!(!choices(&Security { task_manager: false }).iter().any(|c| c.0 == Act::TaskManager));
        assert_eq!(&PASSWORD_PAGE[..3], ["boswas-control-center", "--page", "users"]);
    }

    #[test]
    fn the_screen_draws_with_every_choice_reachable_from_the_keyboard() {
        let security = Security { task_manager: true };
        for scheme in [Scheme::Dark, Scheme::Light] {
            for (w, h) in [(1920.0, 1080.0), (1366.0, 768.0), (640.0, 480.0)] {
                for scale in [1.0, 2.0] {
                    let (canvas, hits) = testing::render(w, h, scale, scheme, None, Some(&Action::Security(Act::Lock)), |f| draw(f, &security));
                    assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0));
                    assert!(testing::hits_are_inside(&hits, w, h), "{w}x{h}");
                    for act in [Act::Lock, Act::SignOut, Act::ChangePassword, Act::TaskManager, Act::Cancel, Act::Power] {
                        assert!(hits.iter().any(|h| h.focusable && h.action == Action::Security(act)), "{act:?}");
                    }
                    let mut focus = None;
                    let mut seen = Vec::new();
                    for _ in 0..6 {
                        focus = crate::ui::navigate(&hits, focus.as_ref(), crate::ui::Nav::Next);
                        seen.push(focus.clone().unwrap());
                    }
                    seen.dedup();
                    assert_eq!(seen.len(), 6, "Tab goes through all six");
                }
            }
        }
        let (_, hits) = testing::render(1920.0, 1080.0, 1.0, Scheme::Dark, None, None, |f| draw(f, &security));
        let power = hits.iter().find(|h| h.action == Action::Security(Act::Power)).unwrap().rect;
        assert!(power.x > 1800.0 && power.y > 960.0, "the power button is at the bottom right");
    }
}
