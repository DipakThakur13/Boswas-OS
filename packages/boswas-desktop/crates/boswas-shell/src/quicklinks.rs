//! The right-click menus of the taskbar's own buttons, as Windows 11 has
//! them: Start's quick-link menu (also Super+X), the clock's, the status
//! button's (network, volume, battery), Task View's and the empty
//! taskbar's, and the keyboard layout indicator's; and what an
//! application's menu in Start adds (open file location, uninstall).
//!
//! An entry is offered only when what it opens is installed. Settings
//! pages open with `boswas-control-center --page KEY`; every key used here
//! is one of Control Center's (a test reads its catalogue).

use std::path::{Path, PathBuf};

use crate::{
    menu::{Cmd, Item},
    start::settings_command,
    Shell,
};

/// The Settings program.
pub const SETTINGS: &str = "boswas-control-center";
const FILES: &str = "boswas-files";
const TERMINAL: &str = "boswas-terminal";
const TASK_MANAGER: &str = "boswas-task-manager";
/// GNOME Disks, when an administrator installed it.
const DISKS: &str = "gnome-disks";

/// The pages of Control Center the shell's menus and keys open.
pub mod pages {
    pub const INSTALLED_APPS: &str = "installed-apps";
    pub const POWER: &str = "power";
    pub const ABOUT: &str = "about";
    pub const NETWORK: &str = "network";
    pub const DATE_TIME: &str = "date-time";
    pub const NOTIFICATIONS: &str = "notifications";
    pub const TASKBAR: &str = "taskbar";
    pub const SOUND: &str = "sound";
    pub const REGION: &str = "region";
    pub const KEYBOARD: &str = "keyboard";

    /// Every key above (for the test that they exist).
    #[cfg(test)]
    pub const ALL: [&str; 10] = [INSTALLED_APPS, POWER, ABOUT, NETWORK, DATE_TIME, NOTIFICATIONS, TASKBAR, SOUND, REGION, KEYBOARD];
}

/// What a menu entry does inside the shell.
#[derive(Clone, Debug, PartialEq)]
pub enum ShellCmd {
    /// Start, with the keys going to its search box.
    Search,
    /// The Run dialog.
    Run,
    /// Shows the desktop (and brings the windows back).
    ShowDesktop,
    /// Opens a folder in the file manager (where Files is not installed:
    /// with the folder's default application).
    OpenFolder(PathBuf),
}

/// An entry that opens a page of Settings.
fn page(label: &str, icon: &str, key: &str) -> Item {
    Item::new(label, icon, Cmd::Spawn(settings_command(key)))
}

/// Start's quick-link menu (Super+X), in Windows' order. `exists` says
/// whether a program is installed; `power` is the power menu (its entries
/// already enabled or not by what the system allows).
pub fn quick_links(exists: &dyn Fn(&str) -> bool, power: Vec<Item>) -> Vec<Item> {
    let settings = exists(SETTINGS);
    let mut items = Vec::new();
    if settings {
        items.push(page("Installed apps", "", pages::INSTALLED_APPS));
        items.push(page("Power options", "", pages::POWER));
        items.push(page("System", "", pages::ABOUT));
        items.push(page("Network connections", "", pages::NETWORK));
    }
    if exists(DISKS) {
        items.push(Item::new("Disks", "", Cmd::Spawn(vec![DISKS.into()])));
    }
    let mut tools = Vec::new();
    if exists(TERMINAL) {
        tools.push(Item::new("Terminal", "", Cmd::Spawn(vec![TERMINAL.into()])));
    }
    if exists(TASK_MANAGER) {
        tools.push(Item::new("Task Manager", "", Cmd::Spawn(vec![TASK_MANAGER.into()])));
    }
    if settings {
        tools.push(Item::new("Settings", "", Cmd::Spawn(vec![SETTINGS.into()])));
    }
    if exists(FILES) {
        tools.push(Item::new("Files", "", Cmd::Spawn(vec![FILES.into(), "--new-window".into()])));
    }
    tools.push(Item::new("Search", "", Cmd::Shell(ShellCmd::Search)));
    tools.push(Item::new("Run", "", Cmd::Shell(ShellCmd::Run)));
    if !items.is_empty() {
        items.push(Item::separator());
    }
    items.extend(tools);
    let mut leave = vec![Item::new("Sign out", "", Cmd::SignOut)];
    leave.extend(power.into_iter().map(|mut item| {
        item.icon.clear();
        item
    }));
    let mut shut_down = Item::new("Shut down or sign out", "", Cmd::Submenu);
    shut_down.children = leave;
    items.push(Item::separator());
    items.push(shut_down);
    items.push(Item::new("Desktop", "", Cmd::Shell(ShellCmd::ShowDesktop)));
    items
}

/// The clock's menu.
pub fn clock_menu(exists: &dyn Fn(&str) -> bool) -> Vec<Item> {
    if !exists(SETTINGS) {
        return Vec::new();
    }
    vec![page("Adjust date and time", "", pages::DATE_TIME), page("Notification settings", "", pages::NOTIFICATIONS)]
}

/// The menu of the network, volume and battery button: one button holds
/// the three icons, so the menu has the settings of each (the battery's
/// only where there is one).
pub fn status_menu(exists: &dyn Fn(&str) -> bool, battery: bool) -> Vec<Item> {
    if !exists(SETTINGS) {
        return Vec::new();
    }
    let mut items = vec![
        page("Network and internet settings", "", pages::NETWORK),
        Item::separator(),
        page("Sound settings", "", pages::SOUND),
        page("Open volume mixer", "", pages::SOUND),
    ];
    if battery {
        items.push(Item::separator());
        items.push(page("Power settings", "", pages::POWER));
    }
    items
}

/// The menu of the Task View button and of the taskbar's background.
pub fn taskbar_menu(exists: &dyn Fn(&str) -> bool) -> Vec<Item> {
    let mut items = Vec::new();
    if exists(TASK_MANAGER) {
        items.push(Item::new("Task Manager", "", Cmd::Spawn(vec![TASK_MANAGER.into()])));
    }
    if exists(SETTINGS) {
        items.push(page("Taskbar settings", "preferences-system-symbolic", pages::TASKBAR));
    }
    items
}

/// The keyboard layout indicator's menu.
pub fn layout_menu(exists: &dyn Fn(&str) -> bool) -> Vec<Item> {
    if !exists(SETTINGS) {
        return Vec::new();
    }
    vec![page("Language and region", "", pages::REGION), page("Keyboard settings", "", pages::KEYBOARD)]
}

/// What an application's menu in Start adds after pinning: the folder of
/// its desktop entry in Files, and where it is uninstalled.
pub fn app_extras(exists: &dyn Fn(&str) -> bool, entry: Option<&Path>) -> Vec<Item> {
    let mut items = Vec::new();
    if let Some(entry) = entry {
        let cmd = if exists(FILES) {
            Cmd::Spawn(vec![FILES.into(), "--select".into(), entry.to_string_lossy().into_owned()])
        } else {
            match entry.parent() {
                Some(folder) => Cmd::Shell(ShellCmd::OpenFolder(folder.to_path_buf())),
                None => Cmd::Nothing,
            }
        };
        if cmd != Cmd::Nothing {
            items.push(Item::new("Open file location", "folder-open-symbolic", cmd));
        }
    }
    if exists(SETTINGS) {
        items.push(page("Uninstall", "edit-delete-symbolic", pages::INSTALLED_APPS));
    }
    items
}

/// Whether a program is installed (on the `PATH`).
pub fn installed(program: &str) -> bool {
    crate::apps::program_exists(program)
}

impl Shell {
    /// Carries out an entry of the quick-link menu that acts in the shell.
    pub fn shell_command(&mut self, cmd: ShellCmd) {
        match cmd {
            ShellCmd::Search => self.open_search(),
            ShellCmd::Run => {
                self.close_transients();
                self.control_event(crate::control::ControlEvent::Run);
            }
            ShellCmd::ShowDesktop => {
                self.close_transients();
                self.send_control("show-desktop");
            }
            ShellCmd::OpenFolder(folder) => {
                self.close_transients();
                self.open_path(&folder);
            }
        }
    }

    /// The menu of one of Start's search results: an application's menu,
    /// or Open for a settings page, or Run for a command.
    pub fn result_menu(&self, result: &crate::start::ResultItem) -> Vec<Item> {
        use crate::start::{ResultItem, SETTINGS as PAGES};
        match result {
            ResultItem::App(index) => self.app_menu(*index),
            ResultItem::Setting(index) => {
                let (key, _, icon, _) = PAGES[*index];
                vec![Item::new("Open", icon, Cmd::Spawn(settings_command(key)))]
            }
            ResultItem::Run(command) => match crate::dialogs::split_command(command).filter(|argv| installed(&argv[0])) {
                Some(argv) => vec![Item::new("Run", "utilities-terminal", Cmd::Spawn(argv))],
                None => Vec::new(),
            },
        }
    }

    /// Super+S: Start, with what is typed going to the search box.
    pub fn open_search(&mut self) {
        use crate::state::Flyout;
        if self.open_kind() != Some(Flyout::Start) {
            self.toggle_flyout(Flyout::Start, None);
        }
        if self.open_kind() == Some(Flyout::Start) {
            self.st.start.index = false;
            self.st.focus = None;
            self.mark(crate::Role::Flyout);
        }
    }

    /// The quick-link menu, at Start's button on the primary taskbar (a
    /// right-click on the button, or Super+X).
    pub fn open_quick_links(&mut self, at: Option<(f32, f32)>) {
        if self.st.mode != crate::state::Mode::Desktop || self.st.locked || self.st.dialog.is_some() || self.st.capture.is_some() {
            return;
        }
        let Some(index) = self.surfaces.iter().position(|s| s.role == crate::Role::Taskbar && Some(&s.output) == self.primary_output().as_ref()) else {
            return;
        };
        let start = self.surfaces[index]
            .hits
            .iter()
            .find(|h| h.action == crate::action::Action::Taskbar(crate::taskbar::Act::Start))
            .map(|h| h.rect);
        let origin = self.surfaces[index].origin;
        let output = self.surfaces[index].output.clone();
        let x = at.map(|p| p.0).or(start.map(|r| origin.0 + r.x)).unwrap_or(origin.0 + 12.0);
        let top = origin.1 + start.map_or(0.0, |r| r.y) - 8.0;
        self.close_transients();
        let items = quick_links(&installed, self.power_menu());
        self.open_menu(items, x, top, true, &output);
        if let Some(menu) = self.st.menus.first_mut() {
            // From the keyboard the first entry is chosen, as on Windows.
            if at.is_none() {
                menu.selected = menu.items.iter().position(|i| i.enabled && !i.separator);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use boswas_services::PowerAction;

    fn labels(items: &[Item]) -> Vec<&str> {
        items.iter().map(|i| if i.separator { "-" } else { i.label.as_str() }).collect()
    }

    fn power() -> Vec<Item> {
        vec![
            Item::new("Sleep", "system-suspend-symbolic", Cmd::Power(PowerAction::Suspend)),
            Item::new("Restart", "system-reboot-symbolic", Cmd::Power(PowerAction::Reboot)),
            Item::new("Shut down", "system-shutdown-symbolic", Cmd::Power(PowerAction::PowerOff)),
        ]
    }

    #[test]
    fn the_quick_link_menu_has_what_is_installed_in_windows_order() {
        let everything = |_: &str| true;
        let items = quick_links(&everything, power());
        assert_eq!(
            labels(&items),
            [
                "Installed apps", "Power options", "System", "Network connections", "Disks", "-", "Terminal", "Task Manager", "Settings",
                "Files", "Search", "Run", "-", "Shut down or sign out", "Desktop"
            ]
        );
        let leave = &items.iter().find(|i| i.label == "Shut down or sign out").unwrap().children;
        assert_eq!(labels(leave), ["Sign out", "Sleep", "Restart", "Shut down"]);
        assert_eq!(leave[0].cmd, Cmd::SignOut);
        assert_eq!(leave[3].cmd, Cmd::Power(PowerAction::PowerOff), "the existing power paths (they ask when windows are open)");
        assert_eq!(items[0].cmd, Cmd::Spawn(settings_command("installed-apps")));
        assert_eq!(items.iter().find(|i| i.label == "Files").unwrap().cmd, Cmd::Spawn(vec!["boswas-files".into(), "--new-window".into()]));
        assert_eq!(items.iter().find(|i| i.label == "Search").unwrap().cmd, Cmd::Shell(ShellCmd::Search));
        assert_eq!(items.last().unwrap().cmd, Cmd::Shell(ShellCmd::ShowDesktop));
        // Without GNOME Disks, Settings and the Boswas tools: only what the
        // shell does itself, and the power paths.
        let none = |_: &str| false;
        assert_eq!(labels(&quick_links(&none, power())), ["Search", "Run", "-", "Shut down or sign out", "Desktop"]);
        let no_disks = |program: &str| program != "gnome-disks";
        assert!(!labels(&quick_links(&no_disks, power())).contains(&"Disks"));
    }

    #[test]
    fn the_taskbar_buttons_have_their_windows_menus() {
        let everything = |_: &str| true;
        let none = |_: &str| false;
        assert_eq!(labels(&clock_menu(&everything)), ["Adjust date and time", "Notification settings"]);
        assert_eq!(clock_menu(&everything)[0].cmd, Cmd::Spawn(settings_command("date-time")));
        assert_eq!(labels(&status_menu(&everything, false)), ["Network and internet settings", "-", "Sound settings", "Open volume mixer"]);
        assert_eq!(labels(&status_menu(&everything, true)).last(), Some(&"Power settings"));
        assert_eq!(labels(&taskbar_menu(&everything)), ["Task Manager", "Taskbar settings"]);
        assert_eq!(labels(&taskbar_menu(&|p: &str| p == SETTINGS)), ["Taskbar settings"]);
        assert_eq!(labels(&layout_menu(&everything)), ["Language and region", "Keyboard settings"]);
        for menu in [clock_menu(&none), status_menu(&none, true), taskbar_menu(&none), layout_menu(&none)] {
            assert!(menu.is_empty(), "nothing is offered that is not installed");
        }
    }

    #[test]
    fn an_application_in_start_can_be_found_in_files_and_uninstalled() {
        let entry = Path::new("/usr/share/applications/firefox-esr.desktop");
        let items = app_extras(&|_: &str| true, Some(entry));
        assert_eq!(labels(&items), ["Open file location", "Uninstall"]);
        assert_eq!(items[0].cmd, Cmd::Spawn(vec!["boswas-files".into(), "--select".into(), entry.to_string_lossy().into_owned()]));
        assert_eq!(items[1].cmd, Cmd::Spawn(settings_command("installed-apps")));
        let without_files = app_extras(&|p: &str| p != FILES, Some(entry));
        assert_eq!(without_files[0].cmd, Cmd::Shell(ShellCmd::OpenFolder("/usr/share/applications".into())));
        assert!(app_extras(&|_: &str| false, None).is_empty());
    }

    /// The page keys of Control Center, read from its catalogue
    /// (`_page(parent, "key", ...)` and `_category("key", ...)`).
    fn control_center_pages() -> Vec<String> {
        let catalog = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../boswas-control-center/boswas_control_center/catalog.py");
        let text = std::fs::read_to_string(&catalog).expect("Control Center's catalogue is in the repository");
        let quoted = |rest: &str| rest.split('"').nth(1).map(str::to_owned);
        let mut keys = Vec::new();
        for line in text.lines().map(str::trim) {
            if let Some(rest) = line.strip_prefix("_category(") {
                keys.extend(quoted(rest));
            } else if let Some(rest) = line.strip_prefix("_page(") {
                // _page("parent", "key", ...): the second string.
                keys.extend(rest.split('"').nth(3).map(str::to_owned));
            }
        }
        keys
    }

    #[test]
    fn start_search_finds_the_clipboard_settings() {
        for query in ["clipboard", "clip", "paste"] {
            let mut start = crate::start::Start::default();
            start.typed(query, &[], &Default::default());
            let found = start.results.iter().any(|r| matches!(r, crate::start::ResultItem::Setting(i) if crate::start::SETTINGS[*i].0 == "clipboard"));
            assert!(found, "{query}: {:?}", start.results);
        }
    }

    #[test]
    fn every_settings_page_the_shell_opens_exists() {
        let known = control_center_pages();
        assert!(known.len() > 40 && known.iter().any(|k| k == "display"), "the catalogue was read: {known:?}");
        let mut used: Vec<&str> = pages::ALL.to_vec();
        used.extend(crate::start::SETTINGS.iter().map(|s| s.0));
        // The pages of the other menus, quick settings, the security screen
        // and Super+U (the compositor's).
        used.extend(["users", "notifications", "taskbar", "start", "power", "wifi", "bluetooth", "sound", "display", "accessibility"]);
        assert_eq!(crate::security::PASSWORD_PAGE[2], "users");
        for key in used {
            assert!(known.iter().any(|k| k == key), "Control Center has no page \"{key}\"");
        }
    }
}
