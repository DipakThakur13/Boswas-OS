//! Everything the shell's surfaces show: one model, drawn by the surface
//! modules and changed by the event handlers in `main.rs`.

use std::{collections::HashMap, rc::Rc, time::Instant};

use tiny_skia::Pixmap;

use crate::{
    action::Action,
    apps::{App, Usage},
    capture::Capture,
    config::{RunHistory, ShellConfig},
    control::WindowInfo,
    dialogs::Dialog,
    lock::AuthView,
    menu::Menu,
    notify::Notes,
    osd::Osd,
    quick::Quick,
    start::Start,
    status::Status,
    svc::Sys,
    taskbar::Preview,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Desktop,
    Lock,
    Greeter,
}

/// The flyout that is open; at most one at a time.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Flyout {
    Start,
    Quick,
    /// Notification centre and calendar.
    Notifications,
    /// Tray icons that do not fit on the taskbar.
    TrayOverflow,
    /// The widgets board.
    Widgets,
}

/// A tooltip waiting to be shown or showing.
#[derive(Clone, Debug, PartialEq)]
pub struct Tooltip {
    pub text: String,
    /// Centre of the control it describes, on the output (logical pixels).
    pub x: f32,
    /// Top edge of that control on the output.
    pub y: f32,
    /// When it appears.
    pub due: Instant,
    pub shown: bool,
    /// The taskbar button it belongs to, when it is one: a running
    /// application shows its windows instead of this text.
    pub task: Option<usize>,
}

pub struct State {
    pub mode: Mode,
    pub apps: Vec<App>,
    pub usage: Usage,
    pub config: ShellConfig,
    pub run_history: RunHistory,
    pub windows: Vec<WindowInfo>,
    pub workspace: usize,
    pub status: Status,
    pub sys: Sys,
    /// The session is locked: nothing but the lock screen may be shown.
    pub locked: bool,
    /// Login name and display name of the session's user.
    pub user: String,
    pub user_name: String,
    pub locale: String,

    pub flyout: Option<Flyout>,
    pub start: Start,
    /// The widgets board and what its cards show.
    pub board: crate::widgets::Board,
    /// The desktop icons.
    pub desk: crate::desktop_icons::Desk,
    pub quick: Quick,
    pub notes: Notes,
    /// The open context menu and its open submenus.
    pub menus: Vec<Menu>,
    pub dialog: Option<Dialog>,
    pub osd: Option<Osd>,
    pub tooltip: Option<Tooltip>,
    pub preview: Option<Preview>,
    pub capture: Option<Capture>,
    /// The control the keyboard focus is on, on the surface that has the
    /// keyboard.
    pub focus: Option<Action>,

    pub night_light: bool,
    /// The programs that may capture the screen right now.
    pub capturing: Vec<String>,
    pub layouts: Vec<String>,
    pub layout: usize,
    /// Window previews by window id.
    pub thumbnails: HashMap<u64, Rc<Pixmap>>,
    pub auth: AuthView,
    /// The desktop's settings, as the compositor last sent them.
    pub desktop: crate::settings::Desktop,
    /// Whether the settings have arrived at least once.
    pub desktop_known: bool,
    /// Whether the do-not-disturb schedule was on at the last check
    /// (`None` before the first): it acts when it turns on or off.
    pub dnd_schedule: Option<bool>,
    /// The applications that sent notifications (for Settings).
    pub senders: crate::config::Senders,
    /// When each application was first seen, for "Recently added".
    pub seen: crate::config::SeenApps,
    /// The lock screen: the weather summary the Weather application keeps.
    pub weather: crate::weather::Watch,
}

#[cfg(test)]
impl State {
    /// A desktop state for drawing tests: a few applications and windows,
    /// and the simulated services' view of the system.
    pub fn for_tests() -> Self {
        use crate::{apps::App, config::ShellConfig};
        let app = |id: &str, name: &str| App {
            id: id.into(),
            name: name.into(),
            comment: String::new(),
            icon: id.into(),
            exec: vec![id.into()],
            terminal: false,
            keywords: Vec::new(),
            wm_class: String::new(),
            actions: Vec::new(),
            categories: Vec::new(),
        };
        let window = |id: u64, app_id: &str, active: bool| WindowInfo {
            id,
            title: format!("Window {id} with a rather long title that has to be shortened"),
            app_id: app_id.into(),
            active,
            minimized: false,
            workspace: 0,
            urgent: false,
        };
        let mut state = Self {
            mode: Mode::Desktop,
            apps: vec![
                app("com.boswas.ControlCenter", "Control Center"),
                app("firefox-esr", "Firefox"),
                app("org.kde.dolphin", "Files"),
                app("org.kde.konsole", "Terminal"),
                app("zz.long", "An application with an exceptionally long name"),
            ],
            usage: Usage { path: None, counts: [("firefox-esr".to_owned(), 4)].into() },
            config: ShellConfig::default(),
            run_history: RunHistory::default(),
            windows: vec![window(1, "firefox-esr", true), window(2, "firefox-esr", false), window(3, "foot", false)],
            workspace: 0,
            status: Status {
                time: "09:41".into(),
                date: "Mon 16 Nov".into(),
                battery: None,
                network: crate::status::Network::Offline,
            },
            sys: Sys::default(),
            locked: false,
            user: "asha".into(),
            user_name: "Asha Rao".into(),
            locale: "en_IN.UTF-8".into(),
            flyout: None,
            start: Start::default(),
            board: crate::widgets::Board::default(),
            desk: crate::desktop_icons::Desk::default(),
            quick: Quick::default(),
            notes: Notes::default(),
            menus: Vec::new(),
            dialog: None,
            osd: None,
            tooltip: None,
            preview: None,
            capture: None,
            focus: None,
            night_light: false,
            capturing: Vec::new(),
            layouts: vec!["us".into(), "in".into()],
            layout: 1,
            thumbnails: HashMap::new(),
            auth: AuthView::default(),
            desktop: crate::settings::Desktop::default(),
            desktop_known: true,
            dnd_schedule: None,
            senders: crate::config::Senders::default(),
            seen: crate::config::SeenApps::default(),
            weather: crate::weather::Watch::default(),
        };
        for event in crate::svc::fake::Fake::default().initial() {
            state.sys.apply(&event);
            if let boswas_services::Event::Notify(note) = event {
                state.notes.receive(note, Instant::now(), false, true);
            }
        }
        state
    }
}

impl State {
    /// Index of the application with this desktop file id.
    pub fn app_index(&self, id: &str) -> Option<usize> {
        self.apps.iter().position(|a| a.id == id)
    }

    /// The buttons of the taskbar's application group.
    pub fn tasks(&self) -> Vec<crate::taskbar::Task> {
        let workspace = (!self.desktop.taskbar.all_desktops).then_some(self.workspace);
        crate::taskbar::tasks(&self.apps, &self.config.taskbar, &self.windows, workspace)
    }

    /// The applications pinned to Start, as ids, in order.
    pub fn start_pins(&self) -> Vec<String> {
        crate::start::pinned_ids(&self.config, &self.apps)
    }

    /// The active keyboard layout, when there is more than one to choose
    /// from.
    pub fn keyboard_layout(&self) -> Option<&str> {
        (self.layouts.len() > 1).then(|| self.layouts.get(self.layout).map(String::as_str)).flatten()
    }
}

#[cfg(test)]
mod tests {
    //! Every surface is drawn in both schemes and at every supported scale:
    //! nothing may panic, every control must lie inside its surface, and
    //! every control must be reachable from the keyboard.
    use super::*;
    use crate::{
        action::Action,
        dialogs::{self, AuthDialog, Dialog, Leave, RunDialog},
        lock, menu, notify, osd, quick, start, taskbar,
        ui::{navigate, testing, Frame, Hit, Nav, Rect},
    };
    use boswas_ui::Scheme;

    const PAD: f32 = 12.0;

    fn check(name: &str, hits: &[Hit], width: f32, height: f32) {
        assert!(testing::hits_are_inside(hits, width, height), "{name}: a control lies outside its surface: {:?}", hits.iter().find(|h| !testing::hits_are_inside(std::slice::from_ref(h), width, height)));
        // Tab from nowhere reaches every focusable control exactly once.
        let focusable = hits.iter().filter(|h| h.focusable).count();
        let mut seen: Vec<Action> = Vec::new();
        let mut focus: Option<Action> = None;
        for _ in 0..focusable {
            focus = navigate(hits, focus.as_ref(), Nav::Next);
            let action = focus.clone().expect("a focusable control");
            if !seen.contains(&action) {
                seen.push(action);
            }
        }
        let distinct = hits.iter().filter(|h| h.focusable).fold(Vec::new(), |mut acc: Vec<&Action>, h| {
            if !acc.contains(&&h.action) {
                acc.push(&h.action);
            }
            acc
        });
        assert_eq!(seen.len(), distinct.len(), "{name}: Tab does not reach every control");
    }

    fn each(mut draw: impl FnMut(&mut Frame, Scheme), width: f32, height: f32, name: &str) {
        for scheme in [Scheme::Dark, Scheme::Light] {
            for scale in [1.0, 1.25, 1.5, 2.0] {
                let (canvas, hits) = testing::render(width, height, scale, scheme, Some((width / 2.0, height / 2.0)), None, |f| draw(f, scheme));
                assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0), "{name} drew nothing at {scale}");
                check(name, &hits, width, height);
            }
        }
    }

    #[test]
    fn the_taskbar_draws_everywhere() {
        let st = State::for_tests();
        for width in [1920.0, 1366.0, 800.0, 480.0] {
            each(|f, _| taskbar::draw(f, &st), width, 48.0, "taskbar");
        }
        // The buttons a user needs are there.
        let (_, hits) = testing::render(1366.0, 48.0, 1.0, Scheme::Dark, None, None, |f| taskbar::draw(f, &st));
        for wanted in [taskbar::Act::Start, taskbar::Act::TaskView, taskbar::Act::Status, taskbar::Act::Clock, taskbar::Act::ShowDesktop, taskbar::Act::Layout, taskbar::Act::Task(0), taskbar::Act::Widgets] {
            assert!(hits.iter().any(|h| h.action == Action::Taskbar(wanted.clone())), "{wanted:?}");
        }
        assert!(hits.iter().filter(|h| matches!(h.action, Action::Taskbar(taskbar::Act::Tray(_)))).count() >= 2);
        assert!(hits.iter().filter(|h| h.tooltip.is_some()).count() >= 8, "taskbar controls explain themselves");
    }

    #[test]
    fn the_taskbar_follows_its_settings() {
        let mut st = State::for_tests();
        st.desktop.taskbar.left = true;
        st.desktop.taskbar.task_view = false;
        st.desktop.taskbar.clock_seconds = true;
        st.status.time = "09:41:07".into();
        each(|f, _| taskbar::draw(f, &st), 1366.0, 48.0, "taskbar at the left");
        let (_, hits) = testing::render(1366.0, 48.0, 1.0, Scheme::Dark, None, None, |f| taskbar::draw(f, &st));
        assert!(!hits.iter().any(|h| h.action == Action::Taskbar(taskbar::Act::TaskView)), "no Task View button");
        let start = hits.iter().find(|h| h.action == Action::Taskbar(taskbar::Act::Start)).unwrap();
        assert!(start.rect.x < 20.0, "Start at the left edge");
        // A badge: the notifications of the application in the centre.
        let firefox = st.tasks().iter().position(|t| t.id == "firefox-esr").unwrap();
        let before = taskbar::badge_count(&st, &st.tasks()[firefox]);
        let mut note = st.notes.history[0].clone();
        note.id = 900;
        note.app_name = "Someone else".into();
        note.desktop_entry = Some("firefox-esr".into());
        st.notes.history.push(note);
        assert_eq!(taskbar::badge_count(&st, &st.tasks()[firefox]), before + 1);
        st.desktop.taskbar.badges = true;
        let (with, _) = testing::render(1366.0, 48.0, 1.0, Scheme::Dark, None, None, |f| taskbar::draw(f, &st));
        st.desktop.taskbar.badges = false;
        let (without, _) = testing::render(1366.0, 48.0, 1.0, Scheme::Dark, None, None, |f| taskbar::draw(f, &st));
        assert_ne!(with.data(), without.data(), "the badge is drawn");
        // The largest text size still fits the taskbar.
        crate::ui::set_text_scale(225);
        each(|f, _| taskbar::draw(f, &st), 1366.0, 48.0, "taskbar with large text");
        crate::ui::set_text_scale(100);
    }

    #[test]
    fn start_draws_in_every_view() {
        use crate::config::AllView;
        let mut st = State::for_tests();
        let (apps, usage) = (st.apps.clone(), st.usage.counts.clone());
        for (output, full) in [((1920.0, 1080.0), true), ((1366.0, 768.0), true), ((2560.0, 1440.0), false), ((800.0, 600.0), false), ((480.0, 400.0), false)] {
            let (w, h) = start::panel_size(output);
            let panel = Rect::new(PAD, PAD, w, h);
            let (sw, sh) = (w + 2.0 * PAD, h + 2.0 * PAD);
            let draw = |st: &State, name: &str| {
                if full {
                    each(|f, _| start::draw(f, st, panel), sw, sh, name);
                } else {
                    let (_, hits) = testing::render(sw, sh, 1.0, Scheme::Dark, Some((sw / 2.0, sh / 2.0)), None, |f| start::draw(f, st, panel));
                    check(name, &hits, sw, sh);
                }
            };
            for view in AllView::ALL {
                st.config.view = view;
                st.start.reset();
                draw(&st, "start");
                st.start.expanded = true;
                st.start.scroll = 400.0;
                draw(&st, "start scrolled");
                st.start.scroll = 1.0e6;
                draw(&st, "start scrolled past the end");
                if view == AllView::List {
                    st.start.index = true;
                    draw(&st, "start with the index of letters");
                }
                st.start.reset();
                st.start.typed("fi", &apps, &usage);
                draw(&st, "start search");
            }
        }
        // Every layout, with every folder next to the power button, and
        // without recommendations.
        st.desktop.start.folders = crate::settings::Folder::ALL.to_vec();
        let panel = Rect::new(PAD, PAD, 832.0, 918.0);
        for layout in [crate::settings::StartLayout::MorePins, crate::settings::StartLayout::MoreRecommendations] {
            st.desktop.start.layout = layout;
            st.start.reset();
            each(|f, _| start::draw(f, &st, panel), 832.0 + 2.0 * PAD, 918.0 + 2.0 * PAD, "start layouts");
        }
        let (_, hits) = testing::render(832.0 + 2.0 * PAD, 918.0 + 2.0 * PAD, 1.0, Scheme::Dark, None, None, |f| start::draw(f, &st, panel));
        assert!(hits.iter().filter(|h| matches!(h.action, Action::Start(start::Act::Folder(_)))).count() >= 4, "the folders are there");
        assert!(hits.iter().any(|h| h.action == Action::Start(start::Act::Recommended(1))), "Firefox is used most");
        st.desktop.start.recently_added = false;
        st.desktop.start.most_used = false;
        let (_, hits) = testing::render(832.0 + 2.0 * PAD, 918.0 + 2.0 * PAD, 1.0, Scheme::Dark, None, None, |f| start::draw(f, &st, panel));
        assert!(!hits.iter().any(|h| matches!(h.action, Action::Start(start::Act::Recommended(_)))), "Recommended is hidden");
        // The largest text sizes still fit.
        for text in [175, 225] {
            crate::ui::set_text_scale(text);
            for view in AllView::ALL {
                st.config.view = view;
                st.start.reset();
                each(|f, _| start::draw(f, &st, panel), 832.0 + 2.0 * PAD, 918.0 + 2.0 * PAD, "start with large text");
            }
        }
        crate::ui::set_text_scale(100);
    }

    /// Many applications, so that the page is longer than its window.
    fn with_many_apps(mut st: State) -> State {
        let make = |id: String, name: String, categories: &str| crate::apps::App {
            id: id.clone(),
            name,
            comment: String::new(),
            icon: id.clone(),
            exec: vec![id],
            terminal: false,
            keywords: Vec::new(),
            wm_class: String::new(),
            actions: Vec::new(),
            categories: categories.split(';').map(str::to_owned).collect(),
        };
        for (i, letter) in "BDGHKMNPRSTW".chars().enumerate() {
            for n in 0..3 {
                let categories = ["Office", "Graphics", "Utility", "Game", "Network", "System"][(i + n) % 6];
                st.apps.push(make(format!("app.{letter}{n}"), format!("{letter}app {n}"), categories));
            }
        }
        st.apps.sort_by_key(|a| a.name.to_lowercase());
        st
    }

    #[test]
    fn the_keyboard_reaches_every_part_of_start() {
        use crate::config::AllView;
        let mut st = with_many_apps(State::for_tests());
        let panel = Rect::new(PAD, PAD, 640.0, 652.0);
        let (sw, sh) = (640.0 + 2.0 * PAD, 652.0 + 2.0 * PAD);
        for view in AllView::ALL {
            st.config.view = view;
            st.start.reset();
            let (_, hits) = testing::render(sw, sh, 1.0, Scheme::Dark, None, None, |f| start::draw(f, &st, panel));
            let footer: Vec<Hit> = hits.iter().filter(|h| matches!(&h.action, Action::Start(a) if a.in_footer())).cloned().collect();
            assert!(footer.len() >= 2, "the user and power buttons");
            let geo = start::geometry(panel, st.desktop.start.layout);
            let mut text = boswas_ui::text::TextEngine::new();
            let page = start::page(&st, &geo, &mut text);
            assert!(page.height > geo.view.h * 1.5, "{view:?}: the page is longer than its window");
            // Down from the search box: the first pin.
            let first = start::navigate(&page, &footer, None, Nav::Down).unwrap();
            assert!(matches!(first, Action::Start(start::Act::App(_))), "{first:?}");
            assert_eq!(start::navigate(&page, &footer, None, Nav::Up), None);
            // Down again and again: through every section to the bottom,
            // scrolling as the focus goes, then the buttons at the bottom.
            let mut focus = Some(first);
            let mut seen_all = false;
            for _ in 0..200 {
                let next = start::navigate(&page, &footer, focus.as_ref(), Nav::Down);
                if let Some(Action::Start(act)) = &next {
                    seen_all |= matches!(act, start::Act::All(_) | start::Act::Letter(_));
                    if let Some(element) = page.find(act) {
                        let max = page.max_scroll(geo.view.h);
                        st.start.reveal(element.rect, geo.view.h, max, Instant::now(), true);
                        assert!(element.rect.y >= st.start.scroll - 0.5 && element.rect.bottom() <= st.start.scroll + geo.view.h + 0.5, "{view:?}: {act:?} is scrolled into sight");
                    }
                }
                if next == focus {
                    break;
                }
                focus = next;
            }
            assert!(seen_all, "{view:?}: Down reaches the applications under All");
            assert!(matches!(&focus, Some(Action::Start(a)) if a.in_footer()), "{view:?}: and ends at the bottom: {focus:?}");
            assert!(st.start.scroll > 0.0, "the page scrolled along");
            // Up from the bottom goes back to the page; Up from the top to the search box.
            let up = start::navigate(&page, &footer, focus.as_ref(), Nav::Up);
            assert!(matches!(&up, Some(Action::Start(a)) if a.on_page()), "{up:?}");
            // Tab reaches every control of the page and the bottom once.
            let controls = page.hits().len() + footer.len();
            let mut reached: Vec<Action> = Vec::new();
            let mut focus: Option<Action> = None;
            for _ in 0..controls {
                focus = start::navigate(&page, &footer, focus.as_ref(), Nav::Next);
                let action = focus.clone().unwrap();
                assert!(!reached.contains(&action), "{view:?}: {action:?} twice");
                reached.push(action);
            }
            assert_eq!(start::navigate(&page, &footer, focus.as_ref(), Nav::Next), page.hits().first().map(|h| h.action.clone()), "Tab wraps");
            // The same application under Pinned and under All are two controls.
            let pins: Vec<usize> = page.hits().iter().filter_map(|h| match h.action {
                Action::Start(start::Act::App(i)) => Some(i),
                _ => None,
            }).collect();
            assert!(pins.iter().all(|i| page.find(&start::Act::All(*i)).is_some()));
        }
        // "Show all" shows every pin; the letters of the list lead to their headings.
        st.config.view = AllView::List;
        st.start.reset();
        st.config.start = Some(st.apps.iter().map(|a| a.id.clone()).collect());
        let geo = start::geometry(panel, st.desktop.start.layout);
        let mut text = boswas_ui::text::TextEngine::new();
        let collapsed = start::page(&st, &geo, &mut text);
        assert!(collapsed.find(&start::Act::ShowAll).is_some());
        let tiles = |page: &start::Page| page.hits().iter().filter(|h| matches!(h.action, Action::Start(start::Act::App(_)))).count();
        assert_eq!(tiles(&collapsed), geo.pin_rows * geo.columns);
        st.start.expanded = true;
        let expanded = start::page(&st, &geo, &mut text);
        assert_eq!(tiles(&expanded), st.apps.len());
        assert!(expanded.height > collapsed.height);
        let letters: Vec<char> = st.apps.iter().map(start::letter_of).collect();
        for letter in &letters {
            assert!(expanded.find(&start::Act::Letter(*letter)).is_some(), "{letter}");
        }
        st.start.index = true;
        let (_, hits) = testing::render(sw, sh, 1.0, Scheme::Light, None, None, |f| start::draw(f, &st, panel));
        let jumps = hits.iter().filter(|h| matches!(h.action, Action::Start(start::Act::Jump(_)))).count();
        let mut distinct = letters.clone();
        distinct.dedup();
        assert_eq!(jumps, distinct.len(), "a letter for each heading");
        assert!(hits.iter().any(|h| h.action == Action::Start(start::Act::CloseIndex)));
        assert!(!hits.iter().any(|h| matches!(h.action, Action::Start(start::Act::All(_)))), "the index covers the list");
    }

    /// Drawing Start is cheap enough to scroll it at the refresh rate of a
    /// fast display.
    #[test]
    fn start_draws_quickly() {
        let st = with_many_apps(State::for_tests());
        let panel = Rect::new(PAD, PAD, 832.0, 918.0);
        let mut res = crate::ui::Resources::new(boswas_ui::Theme::of(Scheme::Dark));
        let mut canvas = boswas_ui::Canvas::new(832.0 + 2.0 * PAD, 918.0 + 2.0 * PAD, 1.0).unwrap();
        let mut hits = Vec::new();
        let frame = |res: &mut crate::ui::Resources, canvas: &mut boswas_ui::Canvas, hits: &mut Vec<Hit>| {
            hits.clear();
            let mut f = Frame { canvas, res, pointer: None, pressed: false, hits, focus: None, origin: (0.0, 0.0) };
            start::draw(&mut f, &st, panel);
        };
        frame(&mut res, &mut canvas, &mut hits);
        let started = Instant::now();
        for _ in 0..10 {
            frame(&mut res, &mut canvas, &mut hits);
        }
        let each = started.elapsed() / 10;
        // A debug build without optimisation of the dependencies is far
        // slower than the package; this only catches a drawing that went
        // wrong (a page drawn whole, icons decoded every time).
        assert!(each < std::time::Duration::from_millis(250), "{each:?} per frame");
    }

    #[test]
    fn quick_settings_draw_on_every_page() {
        let mut st = State::for_tests();
        let output = (1366.0, 768.0);
        for page in [quick::Page::Main, quick::Page::Wifi, quick::Page::Bluetooth, quick::Page::Outputs] {
            st.quick.open_page(page);
            if page == quick::Page::Wifi {
                let ap = st.sys.network.access_points[2].clone();
                st.quick.select_network(&ap);
                st.sys.network.error = Some("Could not connect: the passphrase was not accepted".into());
            }
            if page == quick::Page::Bluetooth {
                st.quick.device = Some(st.sys.bluetooth.devices[0].address.clone());
            }
            let (w, h) = quick::panel_size(&st, output);
            let panel = Rect::new(PAD, PAD, w, h);
            each(|f, _| quick::draw(f, &st, panel), w + 2.0 * PAD, h + 2.0 * PAD, "quick settings");
        }
        // With nothing reporting, the page still draws and says so.
        st.sys = Sys::default();
        for page in [quick::Page::Main, quick::Page::Wifi, quick::Page::Bluetooth, quick::Page::Outputs] {
            st.quick.open_page(page);
            let (w, h) = quick::panel_size(&st, output);
            each(|f, _| quick::draw(f, &st, Rect::new(PAD, PAD, w, h)), w + 2.0 * PAD, h + 2.0 * PAD, "quick settings without services");
        }
    }

    #[test]
    fn notifications_draw_as_toasts_and_in_the_centre() {
        let mut st = State::for_tests();
        let (w, h) = (364.0, 720.0);
        each(|f, _| notify::draw_centre(f, &st, Rect::new(PAD, PAD, w, h)), w + 2.0 * PAD, h + 2.0 * PAD, "notification centre");
        each(|f, _| notify::draw_centre(f, &st, Rect::new(PAD, PAD, w, 300.0)), w + 2.0 * PAD, 300.0 + 2.0 * PAD, "notification centre, short");
        let mut text = boswas_ui::text::TextEngine::new();
        let (tw, th) = notify::toasts_size(&mut text, &st.notes);
        let now = Instant::now() + std::time::Duration::from_secs(1);
        each(|f, _| notify::draw_toasts(f, &st, now), tw, th, "toasts");
        st.notes = Notes::default();
        each(|f, _| notify::draw_centre(f, &st, Rect::new(PAD, PAD, w, h)), w + 2.0 * PAD, h + 2.0 * PAD, "empty notification centre");
    }

    #[test]
    fn menus_dialogs_and_the_rest_draw() {
        let mut st = State::for_tests();
        let items = vec![menu::Item::new("Open", "", menu::Cmd::Lock), menu::Item::separator(), menu::Item::new("Close window", "", menu::Cmd::SignOut)];
        let mut m = menu::Menu::new(items, 20.0, 20.0, false);
        each(
            |f, _| {
                m.place(&mut f.res.text, (800.0, 600.0));
                m.draw(f, (PAD, PAD), 0);
            },
            400.0,
            200.0,
            "menu",
        );
        let request = boswas_services::AuthRequest {
            cookie: "c".into(),
            action_id: "org.example.action".into(),
            message: "Authentication is required to change the system time and this message is long enough to wrap".into(),
            icon_name: String::new(),
            users: vec!["asha".into(), "ravi".into()],
        };
        let mut auth = AuthDialog::new(request);
        auth.progress(boswas_services::AuthProgress::Prompt { text: "Password: ".into(), echo: false });
        auth.field.insert("secret");
        auth.message = Some(("That did not work. Try again.".into(), true));
        let mut run = RunDialog::new(Some("konsole"));
        run.error = Some("“nothing” was not found.".into());
        let screen = Dialog::Screen {
            id: 1,
            program: "obs".into(),
            opened: std::time::Instant::now(),
        };
        for dialog in [Dialog::Auth(auth), Dialog::Confirm { leave: Leave::ShutDown, open_windows: 3 }, Dialog::Run(run), screen] {
            let (w, h) = dialog.size();
            each(|f, _| dialogs::draw(f, &st, &dialog, Rect::new(PAD, PAD, w, h)), w + 2.0 * PAD, h + 2.0 * PAD, "dialog");
        }
        let display = osd::Osd::new(osd::Kind::Volume { muted: false }, 0.4, Instant::now());
        each(|f, _| osd::draw(f, &display, Rect::new(PAD, PAD, osd::SIZE.0, osd::SIZE.1)), osd::SIZE.0 + 2.0 * PAD, osd::SIZE.1 + 2.0 * PAD, "osd");
        let (ow, oh) = taskbar::overflow_size(&st);
        each(|f, _| taskbar::draw_overflow(f, &st, Rect::new(PAD, PAD, ow, oh)), ow + 2.0 * PAD, oh + 2.0 * PAD, "tray overflow");
        let preview = taskbar::Preview { task: 0, windows: vec![1, 2], x: 300.0, left_at: None };
        let (pw, ph) = preview.size();
        each(|f, _| taskbar::draw_preview(f, &st, &preview, Rect::new(PAD, PAD, pw, ph)), pw + 2.0 * PAD, ph + 2.0 * PAD, "preview");

        // Lock screen and greeter, both stages.
        st.auth.display_name = "Asha Rao".into();
        st.auth.hint = "Enter your password to unlock".into();
        each(|f, _| lock::draw(f, &st, true), 1024.0, 768.0, "lock clock");
        st.auth.stage = lock::Stage::SignIn;
        st.auth.password.insert("secret");
        st.auth.caps_lock = true;
        st.auth.state = lock::AuthState::Failed("The password is incorrect. Try again.".into());
        each(|f, _| lock::draw(f, &st, true), 1024.0, 768.0, "lock sign-in");
        each(|f, _| lock::draw(f, &st, false), 640.0, 480.0, "lock, second output");
        st.auth.greeter = true;
        st.auth.users = vec![("asha".into(), "Asha Rao".into()), ("ravi".into(), "Ravi".into())];
        st.auth.select(1);
        each(|f, _| lock::draw(f, &st, true), 1024.0, 768.0, "greeter");
        st.auth.other_user();
        each(|f, _| lock::draw(f, &st, true), 800.0, 600.0, "greeter, other user");
    }

    /// The lock screen's state with a weather summary fetched ten minutes
    /// ago.
    fn with_weather(mut st: State) -> State {
        st.mode = Mode::Lock;
        st.auth.display_name = "Asha Rao".into();
        st.auth.hint = "Enter your password to unlock".into();
        let fetched = chrono::Utc::now().timestamp() - 600;
        st.weather.summary = Some(crate::weather::parse(&crate::weather::sample(fetched).to_string()).unwrap());
        st
    }

    fn picture(st: &State, width: f32, height: f32, interactive: bool) -> Vec<u8> {
        testing::render(width, height, 1.0, Scheme::Dark, None, None, |f| lock::draw(f, st, interactive)).0.data().to_vec()
    }

    #[test]
    fn the_weather_card_draws_on_the_lock_screen() {
        let mut st = with_weather(State::for_tests());
        for text in [100, 175, 225] {
            crate::ui::set_text_scale(text);
            for (w, h) in [(1920.0, 1080.0), (1366.0, 768.0), (1024.0, 768.0), (800.0, 600.0), (480.0, 360.0)] {
                each(|f, _| lock::draw(f, &st, true), w, h, "lock clock with the weather");
            }
        }
        crate::ui::set_text_scale(100);
        // A long place name and condition, every optional line, hours that
        // ran out, and an hour-old summary that says when it was fetched.
        let mut long = crate::weather::sample(chrono::Utc::now().timestamp() - 2 * 3600);
        long["place"] = serde_json::json!("Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch, Isle of Anglesey, Wales");
        long["condition"] = serde_json::json!("Thunderstorm with heavy hail");
        long["temperature"] = serde_json::json!("-12°F");
        st.weather.summary = Some(crate::weather::parse(&long.to_string()).unwrap());
        for text in [100, 225] {
            crate::ui::set_text_scale(text);
            each(|f, _| lock::draw(f, &st, true), 1366.0, 768.0, "lock clock with a long weather card");
        }
        crate::ui::set_text_scale(100);
    }

    #[test]
    fn the_weather_card_keeps_clear_of_the_clock_the_hint_and_the_status() {
        use crate::weather::{Room, MARGIN};
        let st = with_weather(State::for_tests());
        let summary = st.weather.summary.as_ref().unwrap();
        let shown = summary.shown(summary.fetched + 60).unwrap();
        for text in [100, 125, 150, 175, 200, 225] {
            crate::ui::set_text_scale(text);
            for (w, h) in [(3840.0_f32, 2160.0_f32), (1920.0, 1080.0), (1366.0, 768.0), (1280.0, 720.0), (1024.0, 768.0), (800.0, 600.0), (480.0, 360.0)] {
                // As lock::draw has it: the clock and the date above, the
                // hint at the bottom.
                let hint_w = 170.0 * text as f32 / 100.0;
                let size = (h * 0.13).clamp(56.0, 132.0);
                let top = (h * 0.16).max(24.0) + (size * 1.35).round() + 2.0 + ((size * 0.24).clamp(18.0, 28.0) * 1.35).round();
                let room = Room { width: w, height: h, top, hint: Rect::new((w - hint_w) / 2.0, h - 72.0, hint_w, 20.0) };
                let mut card = None;
                testing::render(w, h, 1.0, Scheme::Dark, None, None, |f| card = crate::weather::draw(f, &shown, room));
                let name = format!("{w}x{h} at {text} %");
                if let Some(card) = card {
                    assert_eq!(card.x, MARGIN, "{name}: at the left edge");
                    assert!(card.y >= room.top && card.bottom() <= h - MARGIN + 0.5 && card.right() <= w - MARGIN, "{name}: {card:?}");
                    let beside = card.right() <= room.hint.x;
                    let above = card.bottom() <= room.hint.y;
                    assert!(beside || above, "{name}: the card covers the unlock hint: {card:?}");
                    assert!(card.right() <= w / 2.0 || above, "{name}: it reaches into the status icons");
                } else {
                    // Screens of a usual size always have room for it, at
                    // every text size (the details and then the hours
                    // give way first).
                    assert!(h < 720.0, "{name}: no card where there is room for one");
                }
            }
        }
        crate::ui::set_text_scale(100);
    }

    #[test]
    fn the_weather_card_appears_only_where_it_belongs() {
        let plain = {
            let mut st = with_weather(State::for_tests());
            st.weather.summary = None;
            st
        };
        let mut st = with_weather(State::for_tests());
        let (w, h) = (1366.0, 768.0);
        let without = picture(&plain, w, h, true);
        assert_ne!(picture(&st, w, h, true), without, "the card is drawn");
        // Not on the other outputs, which show the clock and the date only.
        assert_eq!(picture(&st, w, h, false), picture(&plain, w, h, false));
        // Not in the sign-in view.
        st.auth.stage = lock::Stage::SignIn;
        let mut plain_sign_in = with_weather(State::for_tests());
        plain_sign_in.weather.summary = None;
        plain_sign_in.auth.stage = lock::Stage::SignIn;
        assert_eq!(picture(&st, w, h, true), picture(&plain_sign_in, w, h, true));
        st.auth.stage = lock::Stage::Clock;
        // Not when the setting is off, nor before the settings arrived.
        st.desktop.lock_weather = false;
        assert_eq!(picture(&st, w, h, true), without);
        st.desktop.lock_weather = true;
        st.desktop_known = false;
        assert_eq!(picture(&st, w, h, true), without);
        st.desktop_known = true;
        // Not on the login screen.
        st.auth.greeter = true;
        let mut plain_greeter = with_weather(State::for_tests());
        plain_greeter.weather.summary = None;
        plain_greeter.auth.greeter = true;
        assert_eq!(picture(&st, w, h, true), picture(&plain_greeter, w, h, true));
        st.auth.greeter = false;
        // Not when the summary is older than three hours.
        let now = chrono::Utc::now().timestamp();
        st.weather.summary = Some(crate::weather::parse(&crate::weather::sample(now - 3 * 3600 - 120).to_string()).unwrap());
        assert_eq!(picture(&st, w, h, true), without);
        assert!(lock::weather_shown(&st, now).is_none());
        st.weather.summary = Some(crate::weather::parse(&crate::weather::sample(now - 2 * 3600).to_string()).unwrap());
        assert_eq!(lock::weather_shown(&st, now).unwrap().updated, Some("Updated at 14:20"));
    }

    /// Writes pictures of Start over the taskbar and the wallpaper, for
    /// people to look at: `BOSWAS_SHELL_PICTURES=DIR` (where they go) and
    /// `BOSWAS_SHELL_APPS=DIR:DIR` (the desktop entries listed), then
    /// `cargo test -p boswas-shell -- --ignored start_pictures`. Icons come
    /// from the themes in `XDG_DATA_DIRS`.
    #[test]
    #[ignore = "writes pictures for people to look at"]
    fn start_pictures() {
        use crate::config::AllView;
        let Some(out) = std::env::var_os("BOSWAS_SHELL_PICTURES").map(std::path::PathBuf::from) else {
            return;
        };
        let dirs: Vec<std::path::PathBuf> = std::env::var("BOSWAS_SHELL_APPS").unwrap_or_default().split(':').filter(|d| !d.is_empty()).map(Into::into).collect();
        let mut st = State::for_tests();
        st.apps = crate::apps::load_apps(&dirs, "en_IN.UTF-8");
        st.windows.clear();
        st.usage.counts = [("firefox-esr", 9), ("com.boswas.Files", 5), ("com.boswas.TextEditor", 3), ("com.boswas.Terminal", 2)].iter().map(|(k, v)| ((*k).to_owned(), *v)).collect();
        let picture = |st: &State, (w, h): (f32, f32), scheme: Scheme, name: &str| {
            let theme = boswas_ui::Theme::of(scheme);
            let mut background = crate::wallpaper::Background::default();
            background.load_picture("");
            let mut res = crate::ui::Resources::new(theme);
            res.backdrop = background.reduced(theme, w, h).and_then(|reduced| boswas_ui::material::Backdrop::new(&reduced, w, h));
            let bar = boswas_ui::tokens::tokens().shell("taskbar-height");
            let (pw, ph) = start::panel_size((w, h));
            // Start on a surface of its own, as the compositor puts it over
            // the desktop: above the taskbar, centred or at the left.
            let (sw, sh) = (pw + 2.0 * PAD, ph + 2.0 * PAD);
            let origin = (if st.desktop.taskbar.left { 0.0 } else { ((w - sw) / 2.0).floor() }, h - bar - sh);
            let (taskbar, _) = testing::render_with(crate::ui::Resources::new(theme), w, bar, 1.0, None, None, |f| taskbar::draw(f, st));
            let (flyout, _) = testing::render_with(res, sw, sh, 1.0, None, None, |f| {
                f.origin = origin;
                start::draw(f, st, Rect::new(PAD, PAD, pw, ph));
            });
            let (canvas, _) = testing::render(w, h, 1.0, scheme, None, None, |f| {
                background.paint(f.canvas, theme);
                f.canvas.draw_pixmap(taskbar.pixmap(), 0.0, h - bar, w, bar, 1.0);
                f.canvas.draw_pixmap(flyout.pixmap(), origin.0, origin.1, sw, sh, 1.0);
            });
            let file = format!("start-{name}-{w}x{h}-{}-{}.png", crate::ui::text_scale() * 100.0, if scheme == Scheme::Dark { "dark" } else { "light" });
            std::fs::write(out.join(file), boswas_ui::icons::encode_png(canvas.pixmap()).unwrap()).unwrap();
        };
        let mut text = boswas_ui::text::TextEngine::new();
        for output in [(1920.0, 1080.0), (1366.0, 768.0), (2560.0, 1440.0)] {
            for text_size in [100, 175] {
                crate::ui::set_text_scale(text_size);
                for scheme in [Scheme::Dark, Scheme::Light] {
                    for view in AllView::ALL {
                        st.config.view = view;
                        st.start.reset();
                        picture(&st, output, scheme, &format!("{}-top", view.key()));
                        // Scrolled to the applications under All.
                        let (pw, ph) = start::panel_size(output);
                        let geo = start::geometry(Rect::new(PAD, PAD, pw, ph), st.desktop.start.layout);
                        let page = start::page(&st, &geo, &mut text);
                        let all = page.elements.iter().find(|e| e.piece == start::Piece::Title("All")).map_or(0.0, |e| e.rect.y);
                        st.start.scroll = (all - 8.0).min(page.max_scroll(geo.view.h));
                        picture(&st, output, scheme, &format!("{}-scrolled", view.key()));
                    }
                }
            }
        }
        crate::ui::set_text_scale(100);
        let output = (1920.0, 1080.0);
        st.config.view = AllView::List;
        st.start.reset();
        st.start.expanded = true;
        st.config.start = Some(st.apps.iter().map(|a| a.id.clone()).collect());
        picture(&st, output, Scheme::Dark, "expanded");
        st.config.start = None;
        st.start.reset();
        st.start.index = true;
        picture(&st, output, Scheme::Dark, "index");
        st.start.reset();
        let (apps, usage) = (st.apps.clone(), st.usage.counts.clone());
        st.start.typed("te", &apps, &usage);
        picture(&st, output, Scheme::Light, "search");
        st.start.reset();
        st.desktop.taskbar.left = true;
        picture(&st, (1366.0, 768.0), Scheme::Dark, "left");
    }

    /// Writes pictures of the lock screen with the weather card, for people
    /// to look at: `BOSWAS_SHELL_PICTURES=DIR` (where they go) and
    /// `BOSWAS_SHELL_WALLPAPERS=A.png:B.png` (the backgrounds), then
    /// `cargo test -p boswas-shell -- --ignored lock_screen_pictures`.
    #[test]
    #[ignore = "writes pictures for people to look at"]
    fn lock_screen_pictures() {
        let Some(out) = std::env::var_os("BOSWAS_SHELL_PICTURES").map(std::path::PathBuf::from) else {
            return;
        };
        let wallpapers = std::env::var("BOSWAS_SHELL_WALLPAPERS").unwrap_or_default();
        let mut st = with_weather(State::for_tests());
        st.status.time = "14:30".into();
        let now = chrono::Utc::now().timestamp();
        for (index, wallpaper) in std::iter::once("").chain(wallpapers.split(':').filter(|p| !p.is_empty())).enumerate() {
            let mut background = crate::wallpaper::Background::default();
            background.load_picture(wallpaper);
            for (w, h, age) in [(1920.0, 1080.0, 600), (1366.0, 768.0, 600), (1366.0, 768.0, 5400)] {
                st.weather.summary = Some(crate::weather::parse(&crate::weather::sample(now - age).to_string()).unwrap());
                for text in [100, 175] {
                    crate::ui::set_text_scale(text);
                    for scheme in [Scheme::Dark, Scheme::Light] {
                        let theme = boswas_ui::Theme::of(scheme);
                        let mut res = crate::ui::Resources::new(theme);
                        res.backdrop = background.reduced(theme, w, h).and_then(|reduced| boswas_ui::material::Backdrop::new(&reduced, w, h));
                        let (canvas, _) = testing::render_with(res, w, h, 1.0, None, None, |f| {
                            background.paint(f.canvas, theme);
                            lock::draw(f, &st, true);
                        });
                        let name = format!("lock-{index}-{w}x{h}-{text}-{}-{age}s.png", if scheme == Scheme::Dark { "dark" } else { "light" });
                        std::fs::write(out.join(name), boswas_ui::icons::encode_png(canvas.pixmap()).unwrap()).unwrap();
                    }
                }
            }
        }
        crate::ui::set_text_scale(100);
    }

    /// On a machine without a single usable font nothing panics: the lock
    /// screen (which has to stay, or the session could not be unlocked) and
    /// the other surfaces draw their shapes and controls without text.
    #[test]
    fn every_surface_draws_without_any_font() {
        let fontless = |scheme| {
            let text = boswas_ui::text::TextEngine::from_font_dirs(&[]);
            assert!(!text.has_fonts());
            crate::ui::Resources::with_text(boswas_ui::Theme::of(scheme), text)
        };
        let mut st = State::for_tests();
        st.auth.display_name = "Asha Rao".into();
        st.auth.hint = "Enter your password to unlock".into();
        for scheme in [Scheme::Dark, Scheme::Light] {
            for (name, stage, greeter) in [
                ("lock clock", lock::Stage::Clock, false),
                ("lock sign-in", lock::Stage::SignIn, false),
                ("greeter", lock::Stage::SignIn, true),
            ] {
                st.auth.stage = stage;
                st.auth.greeter = greeter;
                let (canvas, hits) = testing::render_with(fontless(scheme), 1024.0, 768.0, 1.0, None, None, |f| lock::draw(f, &st, true));
                assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0), "{name} drew nothing");
                assert!(testing::hits_are_inside(&hits, 1024.0, 768.0), "{name}");
            }
            // The weather card, too, at every text size.
            let mut weather = with_weather(State::for_tests());
            for text in [100, 225] {
                crate::ui::set_text_scale(text);
                let (canvas, hits) = testing::render_with(fontless(scheme), 1024.0, 768.0, 1.0, None, None, |f| lock::draw(f, &weather, true));
                assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0), "the weather card drew nothing");
                assert!(testing::hits_are_inside(&hits, 1024.0, 768.0));
            }
            crate::ui::set_text_scale(100);
            weather.auth.stage = lock::Stage::SignIn;
            testing::render_with(fontless(scheme), 1024.0, 768.0, 1.0, None, None, |f| lock::draw(f, &weather, true));
            let (canvas, _) = testing::render_with(fontless(scheme), 1366.0, 48.0, 1.0, None, None, |f| taskbar::draw(f, &st));
            assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0), "taskbar drew nothing");
            let panel = Rect::new(PAD, PAD, 832.0, 918.0);
            testing::render_with(fontless(scheme), 832.0 + 2.0 * PAD, 918.0 + 2.0 * PAD, 1.0, None, None, |f| start::draw(f, &st, panel));
            testing::render_with(fontless(scheme), 400.0, 768.0, 1.0, None, None, |f| notify::draw_centre(f, &st, Rect::new(PAD, PAD, 364.0, 720.0)));
        }
    }
}
