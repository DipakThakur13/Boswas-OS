//! Installed applications: desktop entries, search and launching.

use std::{
    collections::HashMap,
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

/// Largest desktop entry that is read.
const MAX_ENTRY_BYTES: u64 = 256 * 1024;
/// Most applications listed.
const MAX_APPS: usize = 2000;

/// One launchable application.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct App {
    /// Desktop file id without `.desktop`, for example `org.kde.dolphin`.
    pub id: String,
    pub name: String,
    pub comment: String,
    pub icon: String,
    /// Program and arguments, field codes removed.
    pub exec: Vec<String>,
    pub terminal: bool,
    pub keywords: Vec<String>,
    /// `StartupWMClass`, when the window's app id differs from the file id.
    pub wm_class: String,
    /// The entry's additional actions ("New Window", "New Private Window").
    pub actions: Vec<DesktopAction>,
}

/// A `[Desktop Action ...]` group of a desktop entry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DesktopAction {
    pub name: String,
    pub exec: Vec<String>,
}

/// Most actions kept per entry.
const MAX_ACTIONS: usize = 8;

impl App {
    /// Whether a window with this app id belongs to this application.
    pub fn owns_window(&self, app_id: &str) -> bool {
        if app_id.is_empty() {
            return false;
        }
        let lower = app_id.to_ascii_lowercase();
        let id = self.id.to_ascii_lowercase();
        lower == id
            || (!self.wm_class.is_empty() && lower == self.wm_class.to_ascii_lowercase())
            || id.rsplit('.').next() == Some(lower.as_str())
            || lower.rsplit('.').next() == Some(id.as_str())
    }
}

/// The `applications` directories, the user's first.
pub fn application_dirs() -> Vec<PathBuf> {
    boswas_ui::icons::data_dirs().into_iter().map(|d| d.join("applications")).collect()
}

/// Every application a launcher should show, sorted by name. Entries in
/// earlier directories override later ones with the same id.
pub fn load_apps(dirs: &[PathBuf], locale: &str) -> Vec<App> {
    let mut seen: HashMap<String, Option<App>> = HashMap::new();
    for dir in dirs {
        let mut stack = vec![(dir.clone(), String::new())];
        while let Some((path, prefix)) = stack.pop() {
            let Ok(entries) = std::fs::read_dir(&path) else {
                continue;
            };
            for entry in entries.flatten() {
                let file_name = entry.file_name().to_string_lossy().into_owned();
                let file_path = entry.path();
                if file_path.is_dir() {
                    // Sub-directories become part of the id: a/b.desktop is "a-b".
                    if prefix.matches('-').count() < 3 {
                        stack.push((file_path, format!("{prefix}{file_name}-")));
                    }
                    continue;
                }
                let Some(stem) = file_name.strip_suffix(".desktop") else {
                    continue;
                };
                let id = format!("{prefix}{stem}");
                if seen.contains_key(&id) || seen.len() >= MAX_APPS {
                    continue;
                }
                let app = read_capped(&file_path).and_then(|text| parse_entry(&id, &text, locale));
                seen.insert(id, app);
            }
        }
    }
    let mut apps: Vec<App> = seen.into_values().flatten().collect();
    apps.sort_by(|a, b| a.name.to_lowercase().cmp(&b.name.to_lowercase()).then(a.id.cmp(&b.id)));
    apps
}

fn read_capped(path: &Path) -> Option<String> {
    let file = std::fs::File::open(path).ok()?;
    let mut text = String::new();
    file.take(MAX_ENTRY_BYTES).read_to_string(&mut text).ok()?;
    Some(text)
}

/// Parses a desktop entry. `None` for entries that are hidden, are not
/// applications, are not meant for this desktop, or cannot be started.
pub fn parse_entry(id: &str, text: &str, locale: &str) -> Option<App> {
    let mut fields: HashMap<&str, &str> = HashMap::new();
    // (group id, fields) of each "Desktop Action" group, in file order.
    let mut action_groups: Vec<(&str, HashMap<&str, &str>)> = Vec::new();
    let mut in_entry = false;
    let mut in_action = false;
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('[') {
            in_entry = line == "[Desktop Entry]";
            in_action = false;
            if let Some(name) = line.strip_prefix("[Desktop Action ").and_then(|l| l.strip_suffix(']')) {
                action_groups.push((name.trim(), HashMap::new()));
                in_action = true;
            }
            continue;
        }
        if line.starts_with('#') {
            continue;
        }
        if let Some((key, value)) = line.split_once('=') {
            if in_entry {
                fields.entry(key.trim()).or_insert(value.trim());
            } else if in_action {
                if let Some((_, group)) = action_groups.last_mut() {
                    group.entry(key.trim()).or_insert(value.trim());
                }
            }
        }
    }
    let flag = |key: &str| fields.get(key).is_some_and(|v| v.eq_ignore_ascii_case("true"));
    if fields.get("Type").copied() != Some("Application") || flag("NoDisplay") || flag("Hidden") {
        return None;
    }
    let lists = |key: &str| -> Vec<&str> {
        fields.get(key).map(|v| v.split(';').filter(|s| !s.is_empty()).collect()).unwrap_or_default()
    };
    let only = lists("OnlyShowIn");
    if !only.is_empty() && !only.contains(&"Boswas") {
        return None;
    }
    if lists("NotShowIn").contains(&"Boswas") {
        return None;
    }
    let exec = parse_exec(fields.get("Exec")?)?;
    if let Some(try_exec) = fields.get("TryExec").filter(|v| !v.is_empty()) {
        if !program_exists(try_exec) {
            return None;
        }
    }
    let localized = |key: &str| -> String {
        // "hi_IN.UTF-8" tries Name[hi_IN], then Name[hi], then Name.
        let lang = locale.split(['.', '@']).next().unwrap_or("");
        let short = lang.split('_').next().unwrap_or("");
        [lang, short]
            .iter()
            .filter(|l| !l.is_empty())
            .find_map(|l| fields.get(format!("{key}[{l}]").as_str()).copied())
            .or_else(|| fields.get(key).copied())
            .map(unescape)
            .unwrap_or_default()
    };
    let name = localized("Name");
    if name.is_empty() {
        return None;
    }
    // Only the actions the entry lists in `Actions` count.
    let listed = lists("Actions");
    let lang = locale.split(['.', '@']).next().unwrap_or("");
    let short = lang.split('_').next().unwrap_or("");
    let actions = action_groups
        .iter()
        .filter(|(group, _)| listed.contains(group))
        .filter_map(|(_, group)| {
            let name = [lang, short]
                .iter()
                .filter(|l| !l.is_empty())
                .find_map(|l| group.get(format!("Name[{l}]").as_str()).copied())
                .or_else(|| group.get("Name").copied())
                .map(unescape)
                .filter(|n| !n.is_empty())?;
            Some(DesktopAction {
                name,
                exec: parse_exec(group.get("Exec")?)?,
            })
        })
        .take(MAX_ACTIONS)
        .collect();
    Some(App {
        id: id.to_owned(),
        name,
        comment: localized("Comment"),
        icon: fields.get("Icon").map(|v| unescape(v)).unwrap_or_default(),
        exec,
        terminal: flag("Terminal"),
        keywords: lists("Keywords").into_iter().map(str::to_lowercase).collect(),
        wm_class: fields.get("StartupWMClass").map(|v| unescape(v)).unwrap_or_default(),
        actions,
    })
}

/// Desktop entry string escapes: `\s`, `\n`, `\t`, `\r`, `\\`.
fn unescape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    let mut chars = value.chars();
    while let Some(c) = chars.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        match chars.next() {
            Some('s') => out.push(' '),
            Some('n') => out.push('\n'),
            Some('t') => out.push('\t'),
            Some('r') => out.push('\r'),
            Some('\\') => out.push('\\'),
            // Not a string escape: keep it for the Exec quoting rules.
            Some(other) => {
                out.push('\\');
                out.push(other);
            }
            None => {}
        }
    }
    out
}

/// Splits an `Exec` value into program and arguments following the desktop
/// entry rules (double quotes, backslash escapes inside them) and removes
/// field codes: the launcher starts applications without files or URLs.
pub fn parse_exec(exec: &str) -> Option<Vec<String>> {
    let exec = unescape(exec);
    let mut args: Vec<String> = Vec::new();
    let mut current = String::new();
    let mut has_current = false;
    let mut quoted = false;
    let mut chars = exec.chars().peekable();
    while let Some(c) = chars.next() {
        match (c, quoted) {
            ('"', _) => {
                quoted = !quoted;
                has_current = true;
            }
            ('\\', true) => {
                if let Some(next) = chars.next() {
                    current.push(next);
                }
            }
            (' ' | '\t', false) => {
                if has_current {
                    args.push(std::mem::take(&mut current));
                    has_current = false;
                }
            }
            ('%', false) => match chars.next() {
                Some('%') => {
                    current.push('%');
                    has_current = true;
                }
                // %f %F %u %U %i %c %k and the deprecated ones expand to nothing.
                Some(_) | None => {}
            },
            (other, _) => {
                current.push(other);
                has_current = true;
            }
        }
    }
    if quoted {
        return None;
    }
    if has_current {
        args.push(current);
    }
    args.retain(|a| !a.is_empty());
    (!args.is_empty()).then_some(args)
}

pub fn program_exists(program: &str) -> bool {
    if program.contains('/') {
        return Path::new(program).is_file();
    }
    std::env::var_os("PATH")
        .map(|path| std::env::split_paths(&path).any(|dir| dir.join(program).is_file()))
        .unwrap_or(false)
}

/// How well an application matches a search; lower is better.
pub fn match_rank(app: &App, query: &str) -> Option<u8> {
    let name = app.name.to_lowercase();
    if name.starts_with(query) {
        Some(0)
    } else if name.split_whitespace().any(|word| word.starts_with(query)) {
        Some(1)
    } else if name.contains(query) {
        Some(2)
    } else if app.keywords.iter().any(|k| k.starts_with(query)) {
        Some(3)
    } else if app.id.to_lowercase().contains(query) || app.comment.to_lowercase().contains(query) {
        Some(4)
    } else {
        None
    }
}

/// Indices of the applications matching `query`, best first: by match
/// quality, then by how often the user started them, then by name. An
/// empty query lists everything, most used first. Results are local only.
pub fn search(apps: &[App], query: &str, usage: &HashMap<String, u32>) -> Vec<usize> {
    let query = query.trim().to_lowercase();
    let mut matches: Vec<(u8, u32, usize)> = apps
        .iter()
        .enumerate()
        .filter_map(|(index, app)| {
            let rank = if query.is_empty() { 0 } else { match_rank(app, &query)? };
            Some((rank, usage.get(&app.id).copied().unwrap_or(0), index))
        })
        .collect();
    matches.sort_by(|a, b| a.0.cmp(&b.0).then(b.1.cmp(&a.1)).then(a.2.cmp(&b.2)));
    matches.into_iter().map(|(_, _, index)| index).collect()
}

/// Starts a program as an ordinary client. The shell's own privileged
/// descriptors are close-on-exec and their environment variables are
/// removed, so nothing the shell starts inherits its trust.
pub fn spawn(argv: &[String]) -> std::io::Result<()> {
    let (program, args) = argv
        .split_first()
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidInput, "empty command"))?;
    let mut child = Command::new(program)
        .args(args)
        .env_remove("WAYLAND_SOCKET")
        .env_remove("BOSWAS_COMPOSITOR_FD")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    // Reap the child whenever it exits, without blocking the shell.
    std::thread::Builder::new()
        .name("reaper".into())
        .spawn(move || {
            let _ = child.wait();
        })
        .map(|_| ())
}

/// Starts an application, in a terminal when its entry asks for one.
pub fn launch(app: &App) -> std::io::Result<()> {
    if !app.terminal {
        return spawn(&app.exec);
    }
    for (terminal, flag) in [("konsole", Some("-e")), ("foot", None), ("x-terminal-emulator", Some("-e"))] {
        if program_exists(terminal) {
            let mut argv = vec![terminal.to_owned()];
            argv.extend(flag.map(str::to_owned));
            argv.extend(app.exec.iter().cloned());
            return spawn(&argv);
        }
    }
    Err(std::io::Error::new(std::io::ErrorKind::NotFound, "no terminal emulator is installed"))
}

/// How often each application was started, kept in the user's state
/// directory so search can rank by use.
pub struct Usage {
    pub(crate) path: Option<PathBuf>,
    pub counts: HashMap<String, u32>,
}

impl Usage {
    pub fn load() -> Self {
        let path = std::env::var_os("XDG_STATE_HOME")
            .map(PathBuf::from)
            .filter(|p| p.is_absolute())
            .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/state")))
            .map(|dir| dir.join("boswas").join("launcher-usage.json"));
        let counts = path
            .as_deref()
            .and_then(read_capped)
            .and_then(|text| serde_json::from_str::<HashMap<String, u32>>(&text).ok())
            .unwrap_or_default();
        Self { path, counts }
    }

    pub fn record(&mut self, id: &str) {
        let count = self.counts.entry(id.to_owned()).or_insert(0);
        *count = count.saturating_add(1);
        let Some(path) = &self.path else {
            return;
        };
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        if let Ok(text) = serde_json::to_string(&self.counts) {
            let _ = std::fs::write(path, text);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ENTRY: &str = "[Desktop Entry]\nType=Application\nName=Files\nName[hi]=फ़ाइलें\nComment=Browse folders\n\
        Exec=dolphin %u\nIcon=system-file-manager\nKeywords=folder;browse;\nStartupWMClass=dolphin\nActions=new;\n\
        [Desktop Action new]\nName=New Window\nName[hi]=नई विंडो\nExec=dolphin --new-window %u\n\
        [Desktop Action unlisted]\nName=Hidden\nExec=dolphin --hidden\n";

    #[test]
    fn entries_parse_with_localised_names() {
        let app = parse_entry("org.kde.dolphin", ENTRY, "en_IN.UTF-8").unwrap();
        assert_eq!(app.name, "Files");
        assert_eq!(app.exec, ["dolphin"]);
        assert_eq!(app.icon, "system-file-manager");
        assert_eq!(app.keywords, ["folder", "browse"]);
        assert!(!app.terminal);
        assert_eq!(app.actions, [DesktopAction { name: "New Window".into(), exec: vec!["dolphin".into(), "--new-window".into()] }]);
        let hindi = parse_entry("org.kde.dolphin", ENTRY, "hi_IN.UTF-8").unwrap();
        assert_eq!(hindi.name, "फ़ाइलें");
        assert_eq!(hindi.comment, "Browse folders");
        assert_eq!(hindi.actions[0].name, "नई विंडो");
    }

    #[test]
    fn hidden_and_foreign_entries_are_skipped() {
        let base = "[Desktop Entry]\nType=Application\nName=X\nExec=x\n";
        assert!(parse_entry("x", base, "").is_some());
        for extra in ["NoDisplay=true", "Hidden=true", "OnlyShowIn=KDE;GNOME;", "NotShowIn=Boswas;", "TryExec=/nonexistent/x"] {
            assert!(parse_entry("x", &format!("{base}{extra}\n"), "").is_none(), "{extra}");
        }
        assert!(parse_entry("x", &format!("{base}OnlyShowIn=KDE;Boswas;\n"), "").is_some());
        assert!(parse_entry("x", "[Desktop Entry]\nType=Link\nName=X\nURL=https://example.org\n", "").is_none());
        assert!(parse_entry("x", "[Desktop Entry]\nType=Application\nName=X\n", "").is_none(), "no Exec");
        assert!(parse_entry("x", "[Other]\nType=Application\nName=X\nExec=x\n", "").is_none());
    }

    #[test]
    fn exec_lines_are_split_and_field_codes_dropped() {
        assert_eq!(parse_exec("firefox-esr %u").unwrap(), ["firefox-esr"]);
        assert_eq!(parse_exec("konsole --workdir %f -e top").unwrap(), ["konsole", "--workdir", "-e", "top"]);
        assert_eq!(
            parse_exec(r#"sh -c "echo \"two words\" %f""#).unwrap(),
            ["sh", "-c", "echo \"two words\" %f"],
            "quoted arguments are literal"
        );
        assert_eq!(parse_exec("env A=1 prog --flag=%% %F %i %c %k").unwrap(), ["env", "A=1", "prog", "--flag=%"]);
        assert_eq!(parse_exec(r#""/opt/My App/run" --x"#).unwrap(), ["/opt/My App/run", "--x"]);
        assert!(parse_exec("").is_none());
        assert!(parse_exec("%U").is_none());
        assert!(parse_exec(r#"prog "unterminated"#).is_none());
    }

    fn app(id: &str, name: &str, keywords: &[&str]) -> App {
        App {
            id: id.into(),
            name: name.into(),
            comment: String::new(),
            icon: String::new(),
            exec: vec![id.into()],
            terminal: false,
            keywords: keywords.iter().map(|k| k.to_string()).collect(),
            wm_class: String::new(),
            actions: Vec::new(),
        }
    }

    #[test]
    fn search_ranks_by_match_then_use() {
        let apps = vec![
            app("calc", "Calculator", &["math"]),
            app("cc", "Boswas Control Center", &["settings"]),
            app("compat", "Compatibility Manager", &["windows"]),
            app("term", "Terminal", &["console", "command"]),
        ];
        let mut usage = HashMap::new();
        let names = |q: &str, usage: &HashMap<String, u32>| -> Vec<&str> {
            search(&apps, q, usage).into_iter().map(|i| apps[i].id.as_str()).collect()
        };
        assert_eq!(names("c", &usage), ["calc", "compat", "cc", "term"]);
        assert_eq!(names("co", &usage), ["compat", "cc", "term"]);
        assert_eq!(names("  SETT ", &usage), ["cc"]);
        assert_eq!(names("zzz", &usage), Vec::<&str>::new());
        usage.insert("compat".to_owned(), 9);
        assert_eq!(names("c", &usage), ["compat", "calc", "cc", "term"], "use breaks ties within a rank");
        assert_eq!(names("", &usage), ["compat", "calc", "cc", "term"]);
    }

    #[test]
    fn windows_are_matched_to_their_application() {
        let dolphin = App {
            wm_class: "Dolphin".into(),
            actions: Vec::new(),
            ..app("org.kde.dolphin", "Files", &[])
        };
        assert!(dolphin.owns_window("org.kde.dolphin"));
        assert!(dolphin.owns_window("dolphin"));
        assert!(!dolphin.owns_window("konsole"));
        assert!(!dolphin.owns_window(""));
        assert!(app("foot", "Foot", &[]).owns_window("foot"));
        assert!(app("firefox-esr", "Firefox", &[]).owns_window("org.mozilla.firefox-esr"));
    }

    #[test]
    fn user_entries_override_system_entries() {
        let root = std::env::temp_dir().join(format!("boswas-shell-apps-{}", std::process::id()));
        let (user, system) = (root.join("user"), root.join("system"));
        std::fs::create_dir_all(user.join("kde")).unwrap();
        std::fs::create_dir_all(&system).unwrap();
        let entry = |name: &str| format!("[Desktop Entry]\nType=Application\nName={name}\nExec=x\n");
        std::fs::write(system.join("a.desktop"), entry("System A")).unwrap();
        std::fs::write(user.join("a.desktop"), entry("User A")).unwrap();
        std::fs::write(system.join("b.desktop"), entry("B")).unwrap();
        std::fs::write(user.join("b.desktop"), "[Desktop Entry]\nType=Application\nName=B\nExec=x\nHidden=true\n").unwrap();
        std::fs::write(user.join("kde/c.desktop"), entry("C")).unwrap();
        std::fs::write(system.join("notes.txt"), "not an entry").unwrap();
        let apps = load_apps(&[user, system], "");
        let _ = std::fs::remove_dir_all(&root);
        let names: Vec<(&str, &str)> = apps.iter().map(|a| (a.id.as_str(), a.name.as_str())).collect();
        assert_eq!(names, [("kde-c", "C"), ("a", "User A")], "a user's Hidden=true removes the system entry");
    }
}
