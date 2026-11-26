//! The keyboard on the taskbar: Super+T puts it on the applications'
//! buttons (again: the next one), Super+B on the notification area (the
//! chevron of the hidden icons, or the first tray icon). The arrow keys and
//! Tab move along the taskbar, Enter or Space press the button, Escape
//! leaves. The taskbar takes the keyboard only for that while: a click
//! anywhere else, or any other key, gives it back.

use smithay_client_toolkit::{
    seat::keyboard::Keysym,
    shell::{wlr_layer::KeyboardInteractivity, WaylandSurface},
};

use crate::{
    action::Action,
    taskbar::Act,
    ui::{Hit, Nav},
    Handle, Role, Shell,
};

/// Where the keyboard lands: the first application button (Super+T), or
/// the notification area's chevron or first icon (Super+B); after them the
/// Start button, the network/volume button.
pub fn first(hits: &[Hit], tray: bool) -> Option<Action> {
    let find = |wanted: &dyn Fn(&Act) -> bool| {
        hits.iter()
            .filter(|h| h.focusable)
            .find(|h| matches!(&h.action, Action::Taskbar(act) if wanted(act)))
            .map(|h| h.action.clone())
    };
    if tray {
        find(&|a| matches!(a, Act::TrayOverflow)).or_else(|| find(&|a| matches!(a, Act::Tray(_)))).or_else(|| find(&|a| matches!(a, Act::Status)))
    } else {
        find(&|a| matches!(a, Act::Task(_))).or_else(|| find(&|a| matches!(a, Act::Start)))
    }
}

/// Super+T while the keyboard is on the taskbar: the next application
/// button, round to the first.
pub fn next_task(hits: &[Hit], current: Option<&Action>) -> Option<Action> {
    let tasks: Vec<&Action> = hits.iter().filter(|h| h.focusable && matches!(h.action, Action::Taskbar(Act::Task(_)))).map(|h| &h.action).collect();
    let position = current.and_then(|c| tasks.iter().position(|a| *a == c));
    match position {
        Some(index) => tasks.get((index + 1) % tasks.len()).map(|a| (*a).clone()),
        None => tasks.first().map(|a| (*a).clone()),
    }
}

impl Shell {
    fn primary_taskbar(&self) -> Option<usize> {
        let primary = self.primary_output();
        self.surfaces.iter().position(|s| s.role == Role::Taskbar && Some(&s.output) == primary.as_ref())
    }

    fn set_taskbar_keyboard(&mut self, index: usize, keyboard: KeyboardInteractivity) {
        if let Handle::Layer(layer) = &self.surfaces[index].handle {
            layer.set_keyboard_interactivity(keyboard);
            layer.commit();
        }
    }

    /// Super+T (`tray` false) or Super+B (`tray` true).
    pub fn focus_taskbar(&mut self, tray: bool) {
        use crate::state::Mode;
        if self.st.mode != Mode::Desktop || self.st.locked || self.st.dialog.is_some() || self.st.capture.is_some() || self.st.security.is_some() {
            return;
        }
        let Some(index) = self.primary_taskbar() else {
            return;
        };
        let target = if self.st.bar_focus && !tray {
            next_task(&self.surfaces[index].hits, self.st.focus.as_ref())
        } else {
            first(&self.surfaces[index].hits, tray)
        };
        let Some(target) = target else {
            return;
        };
        if !self.st.bar_focus {
            self.close_transients();
            let output = self.surfaces[index].output.clone();
            // A click anywhere else gives the keyboard back.
            self.ensure_dismiss(&output);
            self.set_taskbar_keyboard(index, KeyboardInteractivity::Exclusive);
            self.st.bar_focus = true;
        }
        self.st.focus = Some(target);
        self.mark(Role::Taskbar);
    }

    /// Gives the keyboard back to whatever had it before.
    pub fn leave_bar_focus(&mut self) {
        if !self.st.bar_focus {
            return;
        }
        self.st.bar_focus = false;
        if let Some(index) = self.primary_taskbar() {
            self.set_taskbar_keyboard(index, KeyboardInteractivity::None);
        }
        if matches!(self.st.focus, Some(Action::Taskbar(_))) {
            self.st.focus = None;
        }
        if self.open_kind().is_none() && self.st.menus.is_empty() {
            self.remove(Role::Dismiss);
        }
        self.mark(Role::Taskbar);
    }

    /// A key while the keyboard is on the taskbar.
    pub fn bar_key(&mut self, keysym: Keysym, shift: bool) {
        let Some(index) = self.primary_taskbar() else {
            return self.leave_bar_focus();
        };
        let nav = match keysym {
            Keysym::Left | Keysym::Up => Nav::Left,
            Keysym::Right | Keysym::Down => Nav::Right,
            Keysym::Tab if shift => Nav::Previous,
            Keysym::ISO_Left_Tab => Nav::Previous,
            Keysym::Tab => Nav::Next,
            Keysym::Return | Keysym::KP_Enter | Keysym::space => {
                let action = self.st.focus.clone();
                self.leave_bar_focus();
                if let Some(action) = action.filter(|a| self.surfaces[index].hits.iter().any(|h| &h.action == a)) {
                    self.activate(action, index);
                }
                return;
            }
            // Escape, and anything else, gives the keyboard back.
            _ => return self.leave_bar_focus(),
        };
        if let Some(action) = crate::ui::navigate(&self.surfaces[index].hits, self.st.focus.as_ref(), nav) {
            self.st.focus = Some(action);
            self.mark(Role::Taskbar);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{state::State, ui::testing};
    use boswas_ui::Scheme;

    #[test]
    fn super_t_and_super_b_land_where_windows_puts_them() {
        let st = State::for_tests();
        let (_, hits) = testing::render(1366.0, 48.0, 1.0, Scheme::Dark, None, None, |f| crate::taskbar::draw(f, &st));
        assert_eq!(first(&hits, false), Some(Action::Taskbar(Act::Task(0))));
        let tray = first(&hits, true);
        assert!(matches!(tray, Some(Action::Taskbar(Act::TrayOverflow | Act::Tray(_)))), "{tray:?}");
        // Super+T again: the next application, round to the first.
        let count = hits.iter().filter(|h| matches!(h.action, Action::Taskbar(Act::Task(_)))).count();
        assert!(count >= 2);
        assert_eq!(next_task(&hits, Some(&Action::Taskbar(Act::Task(0)))), Some(Action::Taskbar(Act::Task(1))));
        assert_eq!(next_task(&hits, Some(&Action::Taskbar(Act::Task(count - 1)))), Some(Action::Taskbar(Act::Task(0))));
        assert_eq!(next_task(&hits, None), Some(Action::Taskbar(Act::Task(0))));
        // An empty taskbar: Start.
        let mut empty = State::for_tests();
        empty.windows.clear();
        empty.config.taskbar.clear();
        let (_, hits) = testing::render(1366.0, 48.0, 1.0, Scheme::Dark, None, None, |f| crate::taskbar::draw(f, &empty));
        assert_eq!(first(&hits, false), Some(Action::Taskbar(Act::Start)));
    }

    #[test]
    fn the_focused_button_is_marked_within_its_own_place() {
        // The ring is drawn when the taskbar is drawn with the focus on a
        // button (the hover rule: within the button and 3 px).
        let st = State::for_tests();
        let focus = Action::Taskbar(Act::Task(0));
        let (plain, hits) = testing::render(1366.0, 48.0, 1.0, Scheme::Dark, None, None, |f| crate::taskbar::draw(f, &st));
        let (ringed, _) = testing::render(1366.0, 48.0, 1.0, Scheme::Dark, None, Some(&focus), |f| crate::taskbar::draw(f, &st));
        let rect = hits.iter().find(|h| h.action == focus).unwrap().rect;
        let width = 1366usize;
        let mut differs = false;
        for (index, (a, b)) in plain.data().chunks_exact(4).zip(ringed.data().chunks_exact(4)).enumerate() {
            if a != b {
                differs = true;
                let (x, y) = ((index % width) as f32, (index / width) as f32);
                assert!(x >= rect.x - 3.0 && x <= rect.right() + 3.0 && y >= rect.y - 3.0 && y <= rect.bottom() + 3.0, "({x}, {y}) is outside the button");
            }
        }
        assert!(differs, "the focus is drawn");
    }
}
