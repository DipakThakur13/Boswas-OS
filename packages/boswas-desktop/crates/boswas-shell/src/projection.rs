//! The display projection flyout (Super+P), as Windows has it: PC screen
//! only, Duplicate, Extend, Second screen only. A choice is carried out by
//! the compositor (`project`), which turns monitors on and off and places
//! them for the session; with one monitor the other choices are shown but
//! cannot be taken. Super+P again moves to the next choice, Enter takes
//! the one marked, a click takes one at once.

use boswas_ui::tokens::tokens;

use crate::{
    action::Action,
    state::Flyout,
    ui::{ramp, Frame, Rect},
    Role, Shell,
};

/// How the monitors are used.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    PcOnly,
    Duplicate,
    Extend,
    SecondOnly,
}

impl Mode {
    pub const ALL: [Mode; 4] = [Mode::PcOnly, Mode::Duplicate, Mode::Extend, Mode::SecondOnly];

    /// The name the compositor's `project` command takes.
    pub fn key(self) -> &'static str {
        match self {
            Mode::PcOnly => "pc-only",
            Mode::Duplicate => "duplicate",
            Mode::Extend => "extend",
            Mode::SecondOnly => "second-only",
        }
    }

    pub fn from_key(key: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|m| m.key() == key)
    }

    pub fn label(self) -> &'static str {
        match self {
            Mode::PcOnly => "PC screen only",
            Mode::Duplicate => "Duplicate",
            Mode::Extend => "Extend",
            Mode::SecondOnly => "Second screen only",
        }
    }

    /// Whether the choice can be taken with this many monitors.
    pub fn possible(self, outputs: usize) -> bool {
        match self {
            Mode::PcOnly | Mode::Extend => true,
            Mode::Duplicate | Mode::SecondOnly => outputs > 1,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Act {
    Choose(Mode),
}

/// What the open flyout shows.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Projection {
    /// Monitors connected (turned on or off).
    pub outputs: usize,
    /// How they are used now.
    pub current: Mode,
}

impl Default for Projection {
    fn default() -> Self {
        Self { outputs: 1, current: Mode::Extend }
    }
}

impl Projection {
    /// The choice after `from` that can be taken (Super+P again).
    pub fn next(&self, from: Mode) -> Mode {
        let start = Mode::ALL.iter().position(|m| *m == from).unwrap_or(0);
        (1..=4).map(|step| Mode::ALL[(start + step) % 4]).find(|m| m.possible(self.outputs)).unwrap_or(from)
    }
}

const ROW_H: f32 = 56.0;
const HEADER_H: f32 = 52.0;

pub fn panel_size(st: &crate::state::State) -> (f32, f32) {
    let note = if st.projection.outputs > 1 { 0.0 } else { 40.0 };
    (320.0, HEADER_H + 4.0 * (ROW_H + 4.0) + 8.0 + note)
}

pub fn draw(f: &mut Frame, st: &crate::state::State, panel: Rect) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    f.panel(panel, "acrylic");
    f.area(panel, Action::None);
    let p = st.projection;
    f.text_left("Project", Rect::new(panel.x + 16.0, panel.y + 12.0, panel.w - 32.0, 28.0), &ramp("body-strong", theme.text));
    let mut y = panel.y + HEADER_H;
    for mode in Mode::ALL {
        let rect = Rect::new(panel.x + 8.0, y, panel.w - 16.0, ROW_H);
        let enabled = mode.possible(p.outputs);
        let touch = if enabled { f.hit(rect, Action::Projection(Act::Choose(mode))) } else { Default::default() };
        f.state_layer(rect, radius, touch, mode == p.current);
        if mode == p.current {
            f.canvas.fill_rounded_rect(rect.x, rect.y + (rect.h - 16.0) / 2.0, 3.0, 16.0, 1.5, theme.accent);
        }
        let ink = if enabled { theme.text } else { theme.text_muted };
        draw_screens(f, mode, Rect::new(rect.x + 14.0, rect.y + 12.0, 44.0, 32.0), ink, theme.accent, enabled);
        f.text_left(mode.label(), Rect::new(rect.x + 72.0, rect.y, rect.w - 84.0, rect.h), &ramp("body", ink));
        y += ROW_H + 4.0;
    }
    if p.outputs <= 1 {
        let note = ramp("caption", theme.text_muted);
        f.text_wrapped("Connect a second screen for the other choices.", panel.x + 16.0, y + 4.0, panel.w - 32.0, 2, &note);
    }
}

/// The small picture of a choice: one or two screens, the one in use
/// filled.
fn draw_screens(f: &mut Frame, mode: Mode, rect: Rect, ink: boswas_ui::Rgba, accent: boswas_ui::Rgba, enabled: bool) {
    let screen = |f: &mut Frame, x: f32, y: f32, w: f32, h: f32, on: bool| {
        if on && enabled {
            f.canvas.fill_rounded_rect(x, y, w, h, 2.0, accent.alpha(0.85));
        }
        f.canvas.stroke_rounded_rect(x, y, w, h, 2.0, 1.5, ink);
    };
    let (x, y) = (rect.x, rect.y);
    match mode {
        Mode::PcOnly => {
            screen(f, x, y + 4.0, 26.0, 18.0, true);
            screen(f, x + 22.0, y + 12.0, 20.0, 14.0, false);
        }
        Mode::Duplicate => {
            screen(f, x, y + 4.0, 26.0, 18.0, true);
            screen(f, x + 18.0, y + 10.0, 26.0, 18.0, true);
        }
        Mode::Extend => {
            screen(f, x, y + 6.0, 21.0, 16.0, true);
            screen(f, x + 23.0, y + 6.0, 21.0, 16.0, true);
        }
        Mode::SecondOnly => {
            screen(f, x, y + 4.0, 26.0, 18.0, false);
            screen(f, x + 22.0, y + 12.0, 20.0, 14.0, true);
        }
    }
}

impl Shell {
    /// Super+P: opens the flyout, or, while it is open, marks the next
    /// choice. `outputs` and `mode` are what the compositor said.
    pub fn open_projection(&mut self, outputs: usize, mode: &str) {
        if self.open_kind() == Some(Flyout::Projection) {
            let next = match self.st.focus {
                Some(Action::Projection(Act::Choose(from))) => self.st.projection.next(from),
                _ => self.st.projection.next(self.st.projection.current),
            };
            self.st.focus = Some(Action::Projection(Act::Choose(next)));
            return self.mark(Role::Flyout);
        }
        self.st.projection = Projection {
            outputs: outputs.max(1),
            current: Mode::from_key(mode).unwrap_or(Mode::Extend),
        };
        self.toggle_flyout(Flyout::Projection, None);
        if self.open_kind() == Some(Flyout::Projection) {
            self.st.focus = Some(Action::Projection(Act::Choose(self.st.projection.current)));
        }
    }

    pub fn projection_action(&mut self, act: Act) {
        let Act::Choose(mode) = act;
        if !mode.possible(self.st.projection.outputs) {
            return;
        }
        self.control_with("project", serde_json::json!({ "mode": mode.key() }));
        self.st.projection.current = mode;
        self.close_flyout();
    }

    /// Keys while the flyout is open.
    pub fn projection_key(&mut self, keysym: smithay_client_toolkit::seat::keyboard::Keysym) {
        use smithay_client_toolkit::seat::keyboard::Keysym;
        let focused = match self.st.focus {
            Some(Action::Projection(Act::Choose(mode))) => mode,
            _ => self.st.projection.current,
        };
        let p = self.st.projection;
        let previous = |from: Mode| {
            let start = Mode::ALL.iter().position(|m| *m == from).unwrap_or(0);
            (1..=4).map(|step| Mode::ALL[(start + 4 - step) % 4]).find(|m| m.possible(p.outputs)).unwrap_or(from)
        };
        let next = match keysym {
            Keysym::Escape => return self.close_flyout(),
            Keysym::Return | Keysym::KP_Enter | Keysym::space => return self.projection_action(Act::Choose(focused)),
            Keysym::Down | Keysym::Tab => p.next(focused),
            Keysym::Up | Keysym::ISO_Left_Tab => previous(focused),
            _ => return,
        };
        self.st.focus = Some(Action::Projection(Act::Choose(next)));
        self.mark(Role::Flyout);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{state::State, ui::testing};
    use boswas_ui::Scheme;

    #[test]
    fn with_one_screen_only_what_needs_one_can_be_chosen() {
        let one = Projection { outputs: 1, current: Mode::Extend };
        assert!(Mode::PcOnly.possible(1) && Mode::Extend.possible(1));
        assert!(!Mode::Duplicate.possible(1) && !Mode::SecondOnly.possible(1));
        assert_eq!(one.next(Mode::Extend), Mode::PcOnly, "Super+P again skips what cannot be chosen");
        assert_eq!(one.next(Mode::PcOnly), Mode::Extend);
        let two = Projection { outputs: 2, current: Mode::PcOnly };
        assert_eq!(two.next(Mode::PcOnly), Mode::Duplicate);
        assert_eq!(two.next(Mode::SecondOnly), Mode::PcOnly);
        for mode in Mode::ALL {
            assert_eq!(Mode::from_key(mode.key()), Some(mode));
        }
        assert_eq!(Mode::from_key("mirror"), None);
    }

    #[test]
    fn the_flyout_draws_and_offers_only_what_can_be_chosen() {
        let mut st = State::for_tests();
        for outputs in [1, 2] {
            st.projection = Projection { outputs, current: Mode::Extend };
            let (w, h) = panel_size(&st);
            let panel = Rect::new(12.0, 12.0, w, h);
            for scheme in [Scheme::Dark, Scheme::Light] {
                for scale in [1.0, 1.25, 2.0] {
                    let (canvas, hits) = testing::render(w + 24.0, h + 24.0, scale, scheme, None, None, |f| draw(f, &st, panel));
                    assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0));
                    assert!(testing::hits_are_inside(&hits, w + 24.0, h + 24.0));
                    let choices: Vec<Mode> = hits
                        .iter()
                        .filter_map(|h| match h.action {
                            Action::Projection(Act::Choose(mode)) => Some(mode),
                            _ => None,
                        })
                        .collect();
                    let expected: Vec<Mode> = Mode::ALL.into_iter().filter(|m| m.possible(outputs)).collect();
                    assert_eq!(choices, expected, "{outputs} screen(s)");
                }
            }
        }
    }
}
