//! Pictures of what the shell round added, for people to look at: the
//! taskbar's menus, Start's application menu, the security options, the
//! projection flyout, quick settings with night light and the battery, the
//! lock and login screens with the level display and an account picture,
//! and Start's user button with it; in both schemes, at 100 and 200 %.
//!
//! `BOSWAS_SHELL_PICTURES=DIR cargo test -p boswas-shell -- --ignored
//! shell_round_pictures` (icons from the themes in `XDG_DATA_DIRS`).

use std::time::Instant;

use boswas_ui::{tokens::tokens, Scheme, Theme};

use crate::{
    accounts, lock,
    menu::{Item, Menu},
    osd, projection, quick, quicklinks, security, start,
    state::State,
    taskbar,
    ui::{testing, Rect, Resources},
    PAD,
};

/// A picture standing in for an account picture: the scheme's accent and
/// another hue of the palette, in two halves.
fn sample_picture(dir: &std::path::Path, login: &str) {
    let mut pixmap = tiny_skia::Pixmap::new(320, 240).unwrap();
    let theme = Theme::of(Scheme::Dark);
    let other = tokens().color("sun.400").unwrap_or(theme.warning);
    let paint = |c: boswas_ui::Rgba| tiny_skia::Paint {
        shader: tiny_skia::Shader::SolidColor(tiny_skia::Color::from_rgba8(c.r, c.g, c.b, 255)),
        anti_alias: true,
        ..Default::default()
    };
    pixmap.fill_rect(tiny_skia::Rect::from_xywh(0.0, 0.0, 320.0, 240.0).unwrap(), &paint(theme.accent), tiny_skia::Transform::identity(), None);
    let circle = tiny_skia::PathBuilder::from_circle(160.0, 100.0, 56.0).unwrap();
    pixmap.fill_path(&circle, &paint(other), tiny_skia::FillRule::Winding, tiny_skia::Transform::identity(), None);
    std::fs::write(dir.join(login), boswas_ui::icons::encode_png(&pixmap).unwrap()).unwrap();
}

struct Shot<'a> {
    out: &'a std::path::Path,
    size: (f32, f32),
    scale: f32,
    scheme: Scheme,
}

impl Shot<'_> {
    fn resources(&self, background: &crate::wallpaper::Background) -> Resources {
        let theme = Theme::of(self.scheme);
        let mut res = Resources::new(theme);
        let (w, h) = self.size;
        res.backdrop = background.reduced(theme, w, h).and_then(|reduced| boswas_ui::material::Backdrop::new(&reduced, w, h));
        res
    }

    /// The desktop (background and taskbar) with one surface over it at
    /// `origin`, drawn by `draw` on a canvas of `surface` logical pixels.
    fn compose(&self, st: &State, name: &str, origin: (f32, f32), surface: (f32, f32), draw: &dyn Fn(&mut crate::ui::Frame)) {
        let (w, h) = self.size;
        let mut background = crate::wallpaper::Background::default();
        background.load_picture("");
        let bar = tokens().shell("taskbar-height");
        let (bar_canvas, _) = testing::render_with(self.resources(&background), w, bar, self.scale, None, None, |f| taskbar::draw(f, st));
        let (top, _) = testing::render_with(self.resources(&background), surface.0, surface.1, self.scale, None, st.focus.as_ref(), |f| {
            f.origin = origin;
            draw(f);
        });
        let theme = Theme::of(self.scheme);
        let (canvas, _) = testing::render(w, h, self.scale, self.scheme, None, None, |f| {
            background.paint(f.canvas, theme);
            f.canvas.draw_pixmap(bar_canvas.pixmap(), 0.0, h - bar, w, bar, 1.0);
            f.canvas.draw_pixmap(top.pixmap(), origin.0, origin.1, surface.0, surface.1, 1.0);
        });
        self.save(&canvas, name);
    }

    /// A surface over the whole screen (lock screen, security options).
    fn full(&self, st: &State, name: &str, paint_background: bool, draw: &dyn Fn(&mut crate::ui::Frame, &State)) {
        let (w, h) = self.size;
        let mut background = crate::wallpaper::Background::default();
        background.load_picture("");
        let theme = Theme::of(self.scheme);
        let (canvas, _) = testing::render_with(self.resources(&background), w, h, self.scale, None, st.focus.as_ref(), |f| {
            if paint_background {
                background.paint(f.canvas, theme);
            }
            draw(f, st);
        });
        self.save(&canvas, name);
    }

    fn save(&self, canvas: &boswas_ui::Canvas, name: &str) {
        let file = format!("{name}-{}-{}.png", (self.scale * 100.0) as u32, if self.scheme == Scheme::Dark { "dark" } else { "light" });
        std::fs::write(self.out.join(file), boswas_ui::icons::encode_png(canvas.pixmap()).unwrap()).unwrap();
    }

    /// A menu opened upwards at `x` over the taskbar, or at a point.
    fn menu(&self, st: &State, name: &str, items: Vec<Item>, x: f32, y: f32, upward: bool) {
        let mut text = boswas_ui::text::TextEngine::new();
        let mut menu = Menu::new(items, x, y, upward);
        let rect = menu.place(&mut text, self.size);
        let origin = (rect.x - PAD, rect.y - PAD);
        self.compose(st, name, origin, (rect.w + 2.0 * PAD, rect.h + 2.0 * PAD), &|f| menu.draw(f, (PAD, PAD), 0));
    }
}

#[test]
fn quick_settings_tell_why_night_light_is_off_and_the_battery_opens_its_settings() {
    use crate::{action::Action, quick::Act};
    let mut st = State::for_tests();
    let (pw, ph) = quick::panel_size(&st, (1366.0, 768.0));
    let panel = Rect::new(PAD, PAD, pw, ph);
    let size = (pw + 2.0 * PAD, ph + 2.0 * PAD);
    let hits = |st: &State| testing::render(size.0, size.1, 1.0, Scheme::Dark, None, None, |f| quick::draw(f, st, panel)).1;
    let available = hits(&st);
    assert!(available.iter().any(|h| h.action == Action::Quick(Act::NightLight)), "the tile turns it on or off");
    assert!(st.sys.power.battery.is_some(), "the simulated system has a battery");
    let battery = available.iter().find(|h| h.action == Action::Quick(Act::Settings("power"))).expect("the percentage is a button");
    assert!(battery.focusable && battery.tooltip.as_deref().is_some_and(|t| t.contains("Power & battery")));
    st.night_light_available = false;
    let without = hits(&st);
    assert!(!without.iter().any(|h| h.action == Action::Quick(Act::NightLight)), "nothing to turn on");
    assert!(
        without.iter().any(|h| h.tooltip.as_deref().is_some_and(|t| t.contains("cannot change its colours"))),
        "resting on the tile says why"
    );
}

#[test]
#[ignore = "writes pictures for people to look at"]
fn shell_round_pictures() {
    let Some(out) = std::env::var_os("BOSWAS_SHELL_PICTURES").map(std::path::PathBuf::from) else {
        return;
    };
    let dirs: Vec<std::path::PathBuf> = std::env::var("BOSWAS_SHELL_APPS").unwrap_or_default().split(':').filter(|d| !d.is_empty()).map(Into::into).collect();
    let icons = out.join("account-icons");
    std::fs::create_dir_all(&icons).unwrap();
    sample_picture(&icons, "asha");
    let mut st = State::for_tests();
    if !dirs.is_empty() {
        st.apps = crate::apps::load_apps(&dirs, "en_IN.UTF-8");
        st.windows.retain(|w| w.app_id == "firefox-esr");
    }
    st.pictures = accounts::Pictures::with_service_dir(icons.clone());
    st.pictures.refresh("asha", None);
    st.status.time = "14:30".into();
    let everything = |_: &str| true;
    for scheme in [Scheme::Dark, Scheme::Light] {
        for scale in [1.0, 2.0] {
            let shot = Shot { out: &out, size: (1366.0, 768.0), scale, scheme };
            let (w, h) = shot.size;
            let bar = tokens().shell("taskbar-height");
            let lay = taskbar::layout(w, st.tasks().len(), 2, true, true, 3, 80.0, taskbar::Arrangement::of(&st));
            // The quick-link menu at the Start button (Super+X), with its
            // submenu's parent marked.
            let power = vec![
                Item::new("Sleep", "", crate::menu::Cmd::Nothing),
                Item::new("Restart", "", crate::menu::Cmd::Nothing),
                Item::new("Shut down", "", crate::menu::Cmd::Nothing),
            ];
            shot.menu(&st, "quick-links", quicklinks::quick_links(&everything, power), lay.start.x, h - bar - 4.0, true);
            shot.menu(&st, "clock-menu", quicklinks::clock_menu(&everything), lay.clock.x, h - bar - 4.0, true);
            shot.menu(&st, "status-menu", quicklinks::status_menu(&everything, true), lay.status.x, h - bar - 4.0, true);
            shot.menu(&st, "taskbar-menu", quicklinks::taskbar_menu(&everything), lay.task_view.map_or(400.0, |r| r.x), h - bar - 4.0, true);
            shot.menu(&st, "layout-menu", quicklinks::layout_menu(&everything), lay.layout.map_or(1000.0, |r| r.x), h - bar - 4.0, true);
            // An application's menu in Start, with Open file location and
            // Uninstall.
            let app = st.apps.iter().position(|a| a.id == "firefox-esr").unwrap_or(0);
            let mut items = vec![Item::new("Open", &st.apps[app].icon, crate::menu::Cmd::Nothing), Item::new("Pin to Start", "", crate::menu::Cmd::Nothing), Item::new("Unpin from taskbar", "", crate::menu::Cmd::Nothing), Item::separator()];
            items.extend(quicklinks::app_extras(&everything, Some(std::path::Path::new("/usr/share/applications/firefox-esr.desktop"))));
            shot.menu(&st, "start-app-menu", items, 560.0, 300.0, false);
            // The taskbar's menu of a running application: "New window of"
            // once.
            let tasks = st.tasks();
            if let Some(task) = tasks.iter().find(|t| !t.windows.is_empty()) {
                let rect = lay.tasks.first().copied().unwrap_or(lay.start);
                let mut menu_items = Vec::new();
                if let Some(app) = task.app.and_then(|i| st.apps.get(i)) {
                    for action in app.other_actions() {
                        menu_items.push(Item::new(&action.name, "", crate::menu::Cmd::Nothing));
                    }
                    if !menu_items.is_empty() {
                        menu_items.push(Item::separator());
                    }
                    menu_items.push(Item::new(&format!("New window of {}", app.name), &app.icon, crate::menu::Cmd::Nothing));
                    menu_items.push(Item::new("Unpin from taskbar", "", crate::menu::Cmd::Nothing));
                }
                menu_items.push(Item::new("Close all windows", "window-close-symbolic", crate::menu::Cmd::Nothing));
                shot.menu(&st, "task-menu", menu_items, rect.x, h - bar - 8.0, true);
            }
            // The security options.
            let mut sec = st.clone_for_pictures();
            sec.security = Some(security::Security { task_manager: true });
            sec.focus = Some(crate::action::Action::Security(security::Act::Lock));
            shot.full(&sec, "security", true, &|f, st| security::draw(f, st.security.as_ref().unwrap()));
            // The projection flyout, with one monitor and with two.
            for outputs in [1, 2] {
                let mut p = st.clone_for_pictures();
                p.projection = projection::Projection { outputs, current: projection::Mode::Extend };
                p.focus = Some(crate::action::Action::Projection(projection::Act::Choose(projection::Mode::Extend)));
                let (pw, ph) = projection::panel_size(&p);
                let (sw, sh) = (pw + 2.0 * PAD, ph + 2.0 * PAD);
                shot.compose(&p, &format!("projection-{outputs}"), (w - sw, h - bar - sh), (sw, sh), &|f| projection::draw(f, &p, Rect::new(PAD, PAD, pw, ph)));
            }
            // Quick settings: night light lit (tinting now), and not
            // available; the battery's percentage is a button.
            for (name, available) in [("quick-night-light", true), ("quick-no-gamma", false)] {
                let mut q = st.clone_for_pictures();
                q.night_light = available;
                q.night_light_available = available;
                let (pw, ph) = quick::panel_size(&q, shot.size);
                let (sw, sh) = (pw + 2.0 * PAD, ph + 2.0 * PAD);
                shot.compose(&q, name, (w - sw, h - bar - sh), (sw, sh), &|f| quick::draw(f, &q, Rect::new(PAD, PAD, pw, ph)));
            }
            // Start with the account picture on the user button.
            let (pw, ph) = start::panel_size(shot.size);
            let (sw, sh) = (pw + 2.0 * PAD, ph + 2.0 * PAD);
            let mut s = st.clone_for_pictures();
            s.start.reset();
            shot.compose(&s, "start-picture", (((w - sw) / 2.0).floor(), h - bar - sh), (sw, sh), &|f| start::draw(f, &s, Rect::new(PAD, PAD, pw, ph)));
            // The lock screen: the clock with the volume level, the sign-in
            // view with the account picture and the brightness level.
            let mut l = st.clone_for_pictures();
            l.auth.display_name = "Asha Rao".into();
            l.auth.hint = "Enter your password to unlock".into();
            l.osd = Some(osd::Osd::new(osd::Kind::Volume { muted: false }, 0.6, Instant::now()));
            shot.full(&l, "lock-clock-volume", true, &|f, st| lock::draw(f, st, true));
            l.auth.stage = lock::Stage::SignIn;
            l.osd = Some(osd::Osd::new(osd::Kind::Brightness, 0.4, Instant::now()));
            shot.full(&l, "lock-signin-picture", true, &|f, st| lock::draw(f, st, true));
            // The login screen: two people, one with a picture.
            let mut g = st.clone_for_pictures();
            g.auth = lock::AuthView {
                greeter: true,
                hint: "Sign in to Boswas OS".into(),
                users: vec![("asha".into(), "Asha Rao".into()), ("ravi".into(), "Ravi".into())],
                stage: lock::Stage::SignIn,
                ..lock::AuthView::default()
            };
            g.auth.select(0);
            g.osd = Some(osd::Osd::new(osd::Kind::Volume { muted: false }, 0.35, Instant::now()));
            shot.full(&g, "greeter-picture", true, &|f, st| lock::draw(f, st, true));
        }
    }
}

impl State {
    /// A copy of the parts the pictures need (State itself is not Clone).
    fn clone_for_pictures(&self) -> State {
        let mut st = State::for_tests();
        st.apps = self.apps.clone();
        st.windows = self.windows.clone();
        st.status = self.status.clone();
        st.pictures = accounts::Pictures::default();
        if let Some(dir) = std::env::var_os("BOSWAS_SHELL_PICTURES").map(std::path::PathBuf::from) {
            st.pictures = accounts::Pictures::with_service_dir(dir.join("account-icons"));
            st.pictures.refresh("asha", None);
        }
        st
    }
}
