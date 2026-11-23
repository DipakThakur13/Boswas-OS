//! Opening a file or a folder with its default application, the way the
//! freedesktop "MIME Applications Associations" specification says: the
//! MIME type from the file name (or `inode/directory`), then the
//! `[Default Applications]` of the `mimeapps.list` files in their order
//! (the user's, the administrator's, the distribution's; the desktop's own
//! `boswas-mimeapps.list` before the generic `mimeapps.list` in each
//! place), then `[Added Associations]` less `[Removed Associations]`, then
//! the applications whose desktop entry declares the type. The entry's
//! `Exec` is expanded (`%f %F %u %U` and the others) into an argument
//! list: nothing goes through a shell. Without a match the caller falls
//! back to `xdg-open`.

use std::{
    collections::HashMap,
    io::Read,
    path::{Path, PathBuf},
};

/// Largest desktop entry or `mimeapps.list` that is read.
const MAX_FILE_BYTES: u64 = 256 * 1024;
/// Most desktop entries looked at.
const MAX_ENTRIES: usize = 8000;
const DEFAULTS: &str = "Default Applications";
const ADDED: &str = "Added Associations";
const REMOVED: &str = "Removed Associations";

/// The MIME types of a file the shell opens, from its name: the type
/// first, then the ones it is a kind of. Empty for names the shell does
/// not know (`xdg-open` decides those).
pub fn mime_types(path: &Path) -> Vec<&'static str> {
    if path.is_dir() {
        return vec!["inode/directory"];
    }
    let extension = path.extension().and_then(|e| e.to_str()).map(str::to_ascii_lowercase).unwrap_or_default();
    let mime = match extension.as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" | "jpe" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "bmp" => "image/bmp",
        "svg" => "image/svg+xml",
        "tif" | "tiff" => "image/tiff",
        "ico" => "image/vnd.microsoft.icon",
        "pdf" => "application/pdf",
        "txt" | "log" => "text/plain",
        "md" => "text/markdown",
        "html" | "htm" => "text/html",
        _ => return Vec::new(),
    };
    match mime {
        "text/markdown" | "text/html" => vec![mime, "text/plain"],
        _ => vec![mime],
    }
}

/// Where desktop entries and associations are looked for.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Dirs {
    pub config_home: Option<PathBuf>,
    pub config_dirs: Vec<PathBuf>,
    pub data_home: Option<PathBuf>,
    pub data_dirs: Vec<PathBuf>,
    /// `XDG_CURRENT_DESKTOP`, in lower case.
    pub desktops: Vec<String>,
}

fn absolute_list(value: Option<String>, default: &str) -> Vec<PathBuf> {
    let value = value.filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_owned());
    let mut out: Vec<PathBuf> = Vec::new();
    for dir in value.split(':').map(PathBuf::from).filter(|p| p.is_absolute()) {
        if !out.contains(&dir) {
            out.push(dir);
        }
    }
    out
}

impl Dirs {
    /// The directories of the session's environment.
    pub fn from_env() -> Self {
        let home = std::env::var_os("HOME").map(PathBuf::from).filter(|p| p.is_absolute());
        let own = |variable: &str, default: &str| {
            std::env::var_os(variable)
                .map(PathBuf::from)
                .filter(|p| p.is_absolute())
                .or_else(|| home.as_ref().map(|h| h.join(default)))
        };
        Self {
            config_home: own("XDG_CONFIG_HOME", ".config"),
            config_dirs: absolute_list(std::env::var("XDG_CONFIG_DIRS").ok(), "/etc/xdg"),
            data_home: own("XDG_DATA_HOME", ".local/share"),
            data_dirs: absolute_list(std::env::var("XDG_DATA_DIRS").ok(), "/usr/local/share:/usr/share"),
            desktops: std::env::var("XDG_CURRENT_DESKTOP")
                .unwrap_or_default()
                .split(':')
                .filter(|d| !d.is_empty())
                .map(str::to_lowercase)
                .collect(),
        }
    }

    /// The `applications` directories, the user's first.
    pub fn application_dirs(&self) -> Vec<PathBuf> {
        self.data_home.iter().chain(&self.data_dirs).map(|d| d.join("applications")).collect()
    }

    /// Every `mimeapps.list` that may exist, most important first.
    pub fn mimeapps_files(&self) -> Vec<PathBuf> {
        let mut names: Vec<String> = self.desktops.iter().filter(|d| is_plain_name(d)).map(|d| format!("{d}-mimeapps.list")).collect();
        names.push("mimeapps.list".into());
        let mut dirs: Vec<PathBuf> = self.config_home.iter().cloned().collect();
        dirs.extend(self.config_dirs.iter().cloned());
        dirs.extend(self.application_dirs());
        let mut files = Vec::new();
        for dir in dirs {
            for name in &names {
                let file = dir.join(name);
                if !files.contains(&file) {
                    files.push(file);
                }
            }
        }
        files
    }
}

fn is_plain_name(name: &str) -> bool {
    !name.is_empty() && name.len() <= 64 && name.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.')) && !name.starts_with('.')
}

fn read_capped(path: &Path) -> Option<String> {
    let file = std::fs::File::open(path).ok()?;
    if !file.metadata().ok()?.is_file() {
        return None;
    }
    let mut text = String::new();
    file.take(MAX_FILE_BYTES).read_to_string(&mut text).ok()?;
    Some(text)
}

/// The groups of a key file and their keys; the first of a repeated key or
/// group wins.
fn parse_groups(text: &str) -> HashMap<String, Vec<(String, String)>> {
    let mut groups: HashMap<String, Vec<(String, String)>> = HashMap::new();
    let mut current: Option<String> = None;
    for line in text.lines().map(str::trim) {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some(name) = line.strip_prefix('[').and_then(|l| l.strip_suffix(']')) {
            current = (!groups.contains_key(name)).then(|| name.to_owned());
            if let Some(name) = &current {
                groups.insert(name.clone(), Vec::new());
            }
            continue;
        }
        let (Some(group), Some((key, value))) = (current.as_ref(), line.split_once('=')) else {
            continue;
        };
        let keys = groups.entry(group.clone()).or_default();
        let key = key.trim();
        if !keys.iter().any(|(k, _)| k == key) {
            keys.push((key.to_owned(), value.trim().to_owned()));
        }
    }
    groups
}

fn list(value: &str) -> Vec<String> {
    value.split(';').map(str::trim).filter(|s| !s.is_empty()).map(str::to_owned).collect()
}

/// A desktop entry that can open files.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    /// The desktop file id, `com.boswas.Photos.desktop`.
    pub id: String,
    pub path: PathBuf,
    pub name: String,
    pub icon: String,
    /// The `Exec` value with the string escapes resolved and the field
    /// codes still in it.
    pub exec: String,
    pub terminal: bool,
    pub mime_types: Vec<String>,
}

/// Reads a desktop entry. `Ok(None)`: a hidden entry, which masks entries
/// of the same id further down the search path; `Err`: not an application
/// that can be started.
fn read_entry(id: &str, path: &Path) -> Result<Option<Entry>, ()> {
    let text = read_capped(path).ok_or(())?;
    let groups = parse_groups(&text);
    let fields = groups.get("Desktop Entry").ok_or(())?;
    let get = |key: &str| fields.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str());
    if get("Type").unwrap_or("Application") != "Application" {
        return Err(());
    }
    if get("Hidden").is_some_and(|v| v.eq_ignore_ascii_case("true")) {
        return Ok(None);
    }
    let exec = crate::apps::unescape(get("Exec").ok_or(())?);
    if exec.trim().is_empty() {
        return Err(());
    }
    if let Some(program) = get("TryExec").map(crate::apps::unescape).filter(|p| !p.is_empty()) {
        if !crate::apps::program_exists(&program) {
            return Err(());
        }
    }
    Ok(Some(Entry {
        id: id.to_owned(),
        path: path.to_owned(),
        name: get("Name").map(crate::apps::unescape).unwrap_or_else(|| id.to_owned()),
        icon: get("Icon").map(crate::apps::unescape).unwrap_or_default(),
        exec,
        terminal: get("Terminal").is_some_and(|v| v.eq_ignore_ascii_case("true")),
        mime_types: get("MimeType").map(list).unwrap_or_default(),
    }))
}

/// The installed applications and the associations, read once for one
/// lookup.
pub struct Index {
    /// Entries by desktop file id, in the order they were found.
    entries: Vec<Entry>,
    /// Each `mimeapps.list` that exists, most important first.
    lists: Vec<HashMap<String, Vec<(String, String)>>>,
}

impl Index {
    pub fn load(dirs: &Dirs) -> Self {
        let mut entries: Vec<Entry> = Vec::new();
        let mut known: Vec<String> = Vec::new();
        let mut count = 0;
        for directory in dirs.application_dirs() {
            // Sub-directories are part of the id: kde/okular.desktop is
            // kde-okular.desktop.
            let mut stack = vec![(directory, String::new(), 0)];
            while let Some((dir, prefix, depth)) = stack.pop() {
                let Ok(read) = std::fs::read_dir(&dir) else {
                    continue;
                };
                let mut children: Vec<_> = read.flatten().collect();
                children.sort_by_key(|e| e.file_name());
                for child in children {
                    count += 1;
                    if count > MAX_ENTRIES {
                        break;
                    }
                    let name = child.file_name().to_string_lossy().into_owned();
                    let path = child.path();
                    if path.is_dir() {
                        if depth < 3 {
                            stack.push((path, format!("{prefix}{name}-"), depth + 1));
                        }
                        continue;
                    }
                    if !name.ends_with(".desktop") {
                        continue;
                    }
                    let id = format!("{prefix}{name}");
                    if known.contains(&id) {
                        continue;
                    }
                    match read_entry(&id, &path) {
                        Ok(Some(entry)) => {
                            known.push(id);
                            entries.push(entry);
                        }
                        // Hidden: nothing of this id below it counts.
                        Ok(None) => known.push(id),
                        Err(()) => {}
                    }
                }
            }
        }
        let lists = dirs.mimeapps_files().iter().filter_map(|path| read_capped(path)).map(|text| parse_groups(&text)).collect();
        Self { entries, lists }
    }

    pub fn entry(&self, id: &str) -> Option<&Entry> {
        self.entries.iter().find(|e| e.id == id)
    }

    fn group(file: &HashMap<String, Vec<(String, String)>>, group: &str, mime: &str) -> Vec<String> {
        file.get(group).and_then(|keys| keys.iter().find(|(k, _)| k == mime)).map(|(_, v)| list(v)).unwrap_or_default()
    }

    /// The applications associated with exactly `mime`: added
    /// associations, then the entries that declare it (by name); removed
    /// ones not.
    pub fn associated(&self, mime: &str) -> Vec<&Entry> {
        let mut out: Vec<&Entry> = Vec::new();
        let mut removed: Vec<String> = Vec::new();
        for list in &self.lists {
            for id in Self::group(list, ADDED, mime) {
                if let Some(entry) = self.entry(&id).filter(|e| !removed.contains(&e.id) && !out.iter().any(|o| o.id == e.id)) {
                    out.push(entry);
                }
            }
            removed.extend(Self::group(list, REMOVED, mime));
        }
        let mut declaring: Vec<&Entry> = self.entries.iter().filter(|e| e.mime_types.iter().any(|m| m == mime)).collect();
        declaring.sort_by(|a, b| a.name.to_lowercase().cmp(&b.name.to_lowercase()).then(a.id.cmp(&b.id)));
        for entry in declaring {
            if !removed.contains(&entry.id) && !out.iter().any(|o| o.id == entry.id) {
                out.push(entry);
            }
        }
        out
    }

    /// The default application of a file of these MIME types (the type
    /// first, then the kinds it belongs to).
    pub fn default_for(&self, mimes: &[&str]) -> Option<&Entry> {
        for mime in mimes {
            for list in &self.lists {
                for id in Self::group(list, DEFAULTS, mime) {
                    if let Some(entry) = self.entry(&id) {
                        return Some(entry);
                    }
                }
            }
            if let Some(entry) = self.associated(mime).into_iter().next() {
                return Some(entry);
            }
        }
        None
    }
}

/// `file://` followed by the path, with every byte but the unreserved
/// characters and `/` percent-encoded.
pub fn file_uri(path: &Path) -> String {
    use std::os::unix::ffi::OsStrExt;
    let mut out = String::from("file://");
    for byte in path.as_os_str().as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' | b'/' => out.push(*byte as char),
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// Splits an `Exec` value (string escapes already resolved) into its
/// arguments by the desktop entry quoting rules, keeping the field codes.
/// Each argument says whether it was quoted. `None` for an unfinished
/// quote.
pub fn split_exec(exec: &str) -> Option<Vec<(String, bool)>> {
    let mut out: Vec<(String, bool)> = Vec::new();
    let mut current = String::new();
    let (mut started, mut quoted, mut inside) = (false, false, false);
    let mut chars = exec.chars();
    while let Some(c) = chars.next() {
        match (c, inside) {
            ('"', _) => {
                inside = !inside;
                quoted = true;
                started = true;
            }
            ('\\', true) => {
                if let Some(next) = chars.next() {
                    current.push(next);
                }
            }
            (' ' | '\t', false) => {
                if started {
                    out.push((std::mem::take(&mut current), quoted));
                    started = false;
                    quoted = false;
                }
            }
            (other, _) => {
                current.push(other);
                started = true;
            }
        }
    }
    if inside {
        return None;
    }
    if started {
        out.push((current, quoted));
    }
    Some(out)
}

/// The command that opens `file` with `entry`: `%f` and `%F` become the
/// file, `%u` and `%U` its `file://` address, `%c` the name, `%k` the
/// entry's file, `%i` the icon, `%%` a percent sign; the deprecated codes
/// go. An entry without a file code gets the file at the end. Quoted
/// arguments are taken as they are.
pub fn command(entry: &Entry, file: &Path) -> Option<Vec<String>> {
    let tokens = split_exec(&entry.exec)?;
    let has_code = tokens.iter().any(|(t, quoted)| !quoted && ["%f", "%F", "%u", "%U"].iter().any(|code| t.contains(code)));
    let path = file.to_string_lossy().into_owned();
    let mut argv: Vec<String> = Vec::new();
    for (token, quoted) in tokens {
        if quoted || !token.contains('%') {
            argv.push(token);
            continue;
        }
        match token.as_str() {
            "%i" => {
                if !entry.icon.is_empty() {
                    argv.extend(["--icon".to_owned(), entry.icon.clone()]);
                }
                continue;
            }
            "%f" | "%F" => {
                argv.push(path.clone());
                continue;
            }
            "%u" | "%U" => {
                argv.push(file_uri(file));
                continue;
            }
            _ => {}
        }
        let mut out = String::new();
        let mut chars = token.chars();
        while let Some(c) = chars.next() {
            if c != '%' {
                out.push(c);
                continue;
            }
            match chars.next() {
                Some('%') => out.push('%'),
                Some('f' | 'F') => out.push_str(&path),
                Some('u' | 'U') => out.push_str(&file_uri(file)),
                Some('c') => out.push_str(&entry.name),
                Some('k') => out.push_str(&entry.path.to_string_lossy()),
                _ => {}
            }
        }
        if !out.is_empty() {
            argv.push(out);
        }
    }
    if !has_code {
        argv.push(path);
    }
    (!argv.is_empty() && !argv[0].is_empty()).then_some(argv)
}

/// How a file is to be opened.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Plan {
    /// With this application: the command, and whether it runs in a
    /// terminal.
    App { id: String, argv: Vec<String>, terminal: bool },
    /// No application is known for it: `xdg-open` decides.
    XdgOpen(Vec<String>),
}

/// How `file` is opened in a session with these directories.
pub fn plan(file: &Path, dirs: &Dirs) -> Plan {
    let mimes = mime_types(file);
    if !mimes.is_empty() {
        let index = Index::load(dirs);
        if let Some(entry) = index.default_for(&mimes) {
            if let Some(argv) = command(entry, file) {
                return Plan::App { id: entry.id.clone(), argv, terminal: entry.terminal };
            }
        }
    }
    Plan::XdgOpen(vec!["xdg-open".into(), file.to_string_lossy().into_owned()])
}

/// Opens `file` with its default application (or through `xdg-open`).
pub fn open(file: &Path) -> std::io::Result<Plan> {
    let plan = plan(file, &Dirs::from_env());
    match &plan {
        Plan::App { argv, terminal, .. } => crate::apps::run(argv, *terminal)?,
        Plan::XdgOpen(argv) => crate::apps::spawn(argv)?,
    }
    Ok(plan)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A home and a system with their own directories, removed afterwards.
    struct Fake {
        root: PathBuf,
        dirs: Dirs,
    }

    impl Fake {
        fn new(name: &str) -> Self {
            let root = std::env::temp_dir().join(format!("boswas-shell-open-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&root);
            let dirs = Dirs {
                config_home: Some(root.join("home/.config")),
                config_dirs: vec![root.join("etc/xdg")],
                data_home: Some(root.join("home/.local/share")),
                data_dirs: vec![root.join("usr/local/share"), root.join("usr/share")],
                desktops: vec!["boswas".into()],
            };
            Self { root, dirs }
        }

        fn write(&self, relative: &str, text: &str) {
            let path = self.root.join(relative);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, text).unwrap();
        }

        fn app(&self, relative: &str, name: &str, exec: &str, mime: &str) {
            self.write(relative, &format!("[Desktop Entry]\nType=Application\nName={name}\nExec={exec}\nMimeType={mime}\n"));
        }

        fn default_id(&self, file: &str) -> Option<String> {
            Index::load(&self.dirs).default_for(&mime_types(Path::new(file))).map(|e| e.id.clone())
        }
    }

    impl Drop for Fake {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn types_come_from_the_name() {
        assert_eq!(mime_types(Path::new("/x/Screenshot 2026-11-23 101500.png")), ["image/png"]);
        assert_eq!(mime_types(Path::new("photo.JPG")), ["image/jpeg"]);
        assert_eq!(mime_types(Path::new("a.pdf")), ["application/pdf"]);
        assert_eq!(mime_types(Path::new("notes.md")), ["text/markdown", "text/plain"]);
        assert!(mime_types(Path::new("archive.unknown")).is_empty());
        assert!(mime_types(Path::new("no-extension")).is_empty());
        assert_eq!(mime_types(&std::env::temp_dir()), ["inode/directory"]);
    }

    #[test]
    fn the_lists_are_read_in_the_specifications_order() {
        let fake = Fake::new("order");
        let files = fake.dirs.mimeapps_files();
        let names: Vec<String> = files.iter().map(|f| f.strip_prefix(&fake.root).unwrap().to_string_lossy().into_owned()).collect();
        assert_eq!(
            names,
            [
                "home/.config/boswas-mimeapps.list",
                "home/.config/mimeapps.list",
                "etc/xdg/boswas-mimeapps.list",
                "etc/xdg/mimeapps.list",
                "home/.local/share/applications/boswas-mimeapps.list",
                "home/.local/share/applications/mimeapps.list",
                "usr/local/share/applications/boswas-mimeapps.list",
                "usr/local/share/applications/mimeapps.list",
                "usr/share/applications/boswas-mimeapps.list",
                "usr/share/applications/mimeapps.list",
            ]
        );
        let odd = Dirs { desktops: vec!["../x".into(), "".into()], ..fake.dirs.clone() };
        assert!(odd.mimeapps_files().iter().all(|f| f.file_name().unwrap() == "mimeapps.list"), "a desktop name is never a path");
    }

    #[test]
    fn defaults_follow_the_lists_then_the_entries() {
        let fake = Fake::new("defaults");
        let apps = "usr/share/applications";
        fake.app(&format!("{apps}/com.boswas.Photos.desktop"), "Photos", "boswas-photos %F", "image/png;image/jpeg;");
        fake.app(&format!("{apps}/org.example.Paint.desktop"), "Paint", "paint %f", "image/png;");
        fake.app(&format!("{apps}/com.boswas.Files.desktop"), "Files", "boswas-files %U", "inode/directory;");
        // Nothing but the entries: the first by name that declares the type.
        assert_eq!(fake.default_id("a.png").as_deref(), Some("org.example.Paint.desktop"));
        assert_eq!(fake.default_id("a.pdf"), None);
        // The distribution's defaults, the desktop's list first.
        fake.write(&format!("{apps}/mimeapps.list"), "[Default Applications]\nimage/png=org.example.Paint.desktop\napplication/pdf=gone.desktop\n");
        fake.write(&format!("{apps}/boswas-mimeapps.list"), "[Default Applications]\nimage/png=missing.desktop;com.boswas.Photos.desktop;\ninode/directory=com.boswas.Files.desktop\n");
        assert_eq!(fake.default_id("a.png").as_deref(), Some("com.boswas.Photos.desktop"), "an application that is not installed is skipped");
        assert_eq!(fake.default_id("a.pdf"), None, "a default that is not installed is no default");
        assert_eq!(fake.default_id(&fake.root.to_string_lossy()).as_deref(), Some("com.boswas.Files.desktop"));
        // The administrator's list comes before the distribution's.
        fake.write("etc/xdg/mimeapps.list", "[Default Applications]\nimage/png=org.example.Paint.desktop\n");
        assert_eq!(fake.default_id("a.png").as_deref(), Some("org.example.Paint.desktop"));
        // And the user's before both.
        fake.write("home/.config/mimeapps.list", "# mine\n[Default Applications]\nimage/png=com.boswas.Photos.desktop\n");
        assert_eq!(fake.default_id("a.png").as_deref(), Some("com.boswas.Photos.desktop"));
    }

    #[test]
    fn added_removed_and_hidden_entries_count() {
        let fake = Fake::new("assoc");
        let apps = "usr/share/applications";
        fake.app(&format!("{apps}/a.desktop"), "Aardvark", "a %f", "image/png;");
        fake.app(&format!("{apps}/b.desktop"), "Badger", "b %f", "");
        fake.app(&format!("{apps}/kde/c.desktop"), "Cat", "c %f", "image/png;");
        fake.write("home/.config/mimeapps.list", "[Added Associations]\nimage/png=b.desktop;\n[Removed Associations]\nimage/png=a.desktop;\n");
        let index = Index::load(&fake.dirs);
        let ids: Vec<&str> = index.associated("image/png").iter().map(|e| e.id.as_str()).collect();
        assert_eq!(ids, ["b.desktop", "kde-c.desktop"], "added first; removed left out; sub-directories in the id");
        assert_eq!(index.default_for(&["image/png"]).unwrap().id, "b.desktop");
        // A hidden entry of the user's masks the system's entry of that id.
        fake.write("home/.local/share/applications/b.desktop", "[Desktop Entry]\nType=Application\nName=B\nExec=b\nHidden=true\n");
        assert_eq!(fake.default_id("x.png").as_deref(), Some("kde-c.desktop"));
        // Entries that cannot start are not applications.
        fake.write(&format!("{apps}/link.desktop"), "[Desktop Entry]\nType=Link\nName=L\nURL=https://example.org\nMimeType=image/gif;\n");
        fake.write(&format!("{apps}/noexec.desktop"), "[Desktop Entry]\nType=Application\nName=N\nMimeType=image/gif;\n");
        fake.write(&format!("{apps}/tryexec.desktop"), "[Desktop Entry]\nType=Application\nName=T\nExec=t\nTryExec=/nonexistent/t\nMimeType=image/gif;\n");
        assert_eq!(fake.default_id("x.gif"), None);
    }

    #[test]
    fn exec_lines_become_argument_lists() {
        let entry = |exec: &str| Entry {
            id: "x.desktop".into(),
            path: PathBuf::from("/usr/share/applications/x.desktop"),
            name: "X App".into(),
            icon: "x-icon".into(),
            exec: exec.into(),
            terminal: false,
            mime_types: Vec::new(),
        };
        let file = Path::new("/home/a/Pictures/Screenshots/Screenshot 2026-11-23 101500.png");
        assert_eq!(command(&entry("boswas-photos %F"), file).unwrap(), ["boswas-photos", "/home/a/Pictures/Screenshots/Screenshot 2026-11-23 101500.png"]);
        assert_eq!(command(&entry("viewer %f"), file).unwrap()[1], file.to_str().unwrap());
        assert_eq!(command(&entry("boswas-files %U"), file).unwrap()[1], "file:///home/a/Pictures/Screenshots/Screenshot%202026-11-23%20101500.png");
        assert_eq!(command(&entry("app --open=%u"), Path::new("/t/ü.png")).unwrap()[1], "--open=file:///t/%C3%BC.png");
        assert_eq!(command(&entry("app %i -t %c %k %d %%x"), file).unwrap(), ["app", "--icon", "x-icon", "-t", "X App", "/usr/share/applications/x.desktop", "%x", file.to_str().unwrap()]);
        // No file code: the file goes at the end (as GLib does).
        assert_eq!(command(&entry("plain --new"), file).unwrap(), ["plain", "--new", file.to_str().unwrap()]);
        // Quoted arguments are literal, even with what looks like a field code.
        assert_eq!(command(&entry(r#""/opt/My App/run" "%f" %f"#), file).unwrap(), ["/opt/My App/run", "%f", file.to_str().unwrap()]);
        assert_eq!(command(&entry(r#"sh -c "echo \"$1\"" %f"#), file).unwrap()[..3], ["sh", "-c", "echo \"$1\""]);
        // A file name with shell characters is one argument and stays as it is.
        let odd = Path::new("/tmp/a;rm -rf $HOME `x`.png");
        assert_eq!(command(&entry("viewer %f"), odd).unwrap(), ["viewer", "/tmp/a;rm -rf $HOME `x`.png"]);
        assert!(command(&entry(r#"broken "quote %f"#), file).is_none());
        assert!(command(&entry("%f"), Path::new("")).is_none(), "no program");
    }

    /// The default applications boswas-apps ships, with the desktop
    /// entries of the system applications (skipped in a tree without
    /// them).
    #[test]
    fn the_shipped_defaults_open_pictures_in_photos() {
        let packages = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
        let shipped = packages.join("boswas-apps/data/mimeapps.list");
        let Some(defaults) = read_capped(&shipped) else {
            return;
        };
        let fake = Fake::new("shipped");
        let target = fake.root.join("usr/share/applications");
        std::fs::create_dir_all(&target).unwrap();
        let mut declared: HashMap<String, Vec<String>> = HashMap::new();
        for dir in ["boswas-apps/data", "boswas-control-center/data", "boswas-compat-manager/data"] {
            let mut stack = vec![packages.join(dir)];
            while let Some(dir) = stack.pop() {
                for entry in std::fs::read_dir(&dir).unwrap().flatten() {
                    let path = entry.path();
                    let name = entry.file_name().to_string_lossy().into_owned();
                    if path.is_dir() {
                        stack.push(path);
                    } else if name.ends_with(".desktop") && name != "com.boswas.Reminders.desktop" {
                        std::fs::copy(&path, target.join(&name)).unwrap();
                        let groups = parse_groups(&read_capped(&path).unwrap());
                        let types = groups["Desktop Entry"].iter().find(|(k, _)| k == "MimeType").map(|(_, v)| list(v)).unwrap_or_default();
                        declared.insert(name, types);
                    }
                }
            }
        }
        std::fs::copy(packages.join("boswas-compat-manager/data/boswas-mimeapps.list"), target.join("boswas-mimeapps.list")).unwrap();
        std::fs::write(target.join("mimeapps.list"), &defaults).unwrap();
        // Debian's entry of the browser, as far as it matters here.
        let firefox = ["text/html", "text/xml", "application/xhtml+xml", "x-scheme-handler/http", "x-scheme-handler/https"];
        fake.app("usr/share/applications/firefox-esr.desktop", "Firefox ESR", "/usr/lib/firefox-esr/firefox-esr %u", &firefox.join(";"));
        declared.insert("firefox-esr.desktop".into(), firefox.iter().map(|m| (*m).to_owned()).collect());
        // Every default is an installed application that declares the type.
        let groups = parse_groups(&defaults);
        let pairs = groups.get(DEFAULTS).expect("a [Default Applications] group");
        assert!(pairs.len() >= 40, "{}", pairs.len());
        for (mime, ids) in pairs {
            for id in list(ids) {
                assert!(declared.get(&id).is_some_and(|types| types.contains(mime)), "{mime}={id}: not declared by an installed entry");
            }
        }
        // What the shell opens.
        let screenshots = fake.root.join("home/Pictures/Screenshots");
        std::fs::create_dir_all(&screenshots).unwrap();
        let cases = [
            ("Screenshot 2026-11-23 101500.png", "com.boswas.Photos.desktop"),
            ("photo.jpg", "com.boswas.Photos.desktop"),
            ("manual.pdf", "com.boswas.DocumentViewer.desktop"),
            ("notes.txt", "com.boswas.TextEditor.desktop"),
            ("README.md", "com.boswas.TextEditor.desktop"),
            ("page.html", "firefox-esr.desktop"),
        ];
        for (file, id) in cases {
            assert_eq!(fake.default_id(file).as_deref(), Some(id), "{file}");
        }
        assert_eq!(fake.default_id(&screenshots.to_string_lossy()).as_deref(), Some("com.boswas.Files.desktop"));
        match plan(&screenshots.join("Screenshot 2026-11-23 101500.png"), &fake.dirs) {
            Plan::App { id, argv, terminal } => {
                assert_eq!((id.as_str(), argv[0].as_str(), argv.len(), terminal), ("com.boswas.Photos.desktop", "boswas-photos", 2, false));
                assert!(argv[1].ends_with("/Screenshots/Screenshot 2026-11-23 101500.png"));
            }
            other => panic!("{other:?}"),
        }
        match plan(&screenshots, &fake.dirs) {
            Plan::App { argv, .. } => assert!(argv[0] == "boswas-files" && argv[1].starts_with("file:///") && argv[1].ends_with("/Screenshots")),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn a_plan_names_the_application_or_falls_back_to_xdg_open() {
        let fake = Fake::new("plan");
        fake.app("usr/share/applications/com.boswas.Photos.desktop", "Photos", "boswas-photos %F", "image/png;");
        fake.write("usr/share/applications/mimeapps.list", "[Default Applications]\nimage/png=com.boswas.Photos.desktop\n");
        let file = Path::new("/home/a/shot.png");
        assert_eq!(plan(file, &fake.dirs), Plan::App { id: "com.boswas.Photos.desktop".into(), argv: vec!["boswas-photos".into(), "/home/a/shot.png".into()], terminal: false });
        assert_eq!(plan(Path::new("/home/a/x.odt"), &fake.dirs), Plan::XdgOpen(vec!["xdg-open".into(), "/home/a/x.odt".into()]));
        assert_eq!(plan(Path::new("/home/a/x.pdf"), &fake.dirs), Plan::XdgOpen(vec!["xdg-open".into(), "/home/a/x.pdf".into()]), "a type nobody opens");
    }
}
