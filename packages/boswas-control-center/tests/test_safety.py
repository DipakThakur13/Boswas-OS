"""Static guarantees of Control Center's code and data.

Least privilege: only commands.py may start a process, always from an
argument list whose program is in the fixed allowlist, never through a
shell; the one exception is the services bridge, a long-lived child that
only ui/bridgeclient.py starts, by its fixed path and without arguments.
Nothing evaluates code; nothing starts Wine or a KDE program; the device
agent and session agent are used read-only. Privileged work goes only
through `pkexec /usr/libexec/boswas/sysops <operation>` with an operation of
a fixed set and validated arguments; apt-get only ever simulates;
timedatectl, localectl, ip, locale, gio, nmcli and busctl only with their
fixed argument forms (busctl: AccountsService calls without any secret and
logind's two read-only questions); hostnamectl only for the static host
name, systemctl only to restart, to ask whether Bluetooth runs and to pass
the pointer size on (with dbus-update-activation-environment: XCURSOR_SIZE
only), nmcli changes only in the forms of nmcli_change_argv (a wired
connection's addresses, DNS and metered state by validated properties),
udisksctl only to unmount, lock and power off a block device, Files only at
an absolute folder, fc-cache only for the user's font folder, lp only for
CUPS's own test page, dpkg-query only to read, the flatpak permission store
only to list and to remove one decision; passwd only without arguments on a
pseudo-terminal, the one new way a program is started
(commands.CommandRunner.converse); only userfiles.py writes or deletes
files. The desktop entries are valid and there is no
Install Boswas OS entry (the image generates it in live sessions only). No
teal anywhere in the data or code.

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import ast
import colorsys
import re
import sys
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)

from boswas_control_center import backend, catalog, commands, jobs, servicebridge  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
CODE = sorted(p for p in (PACKAGE / "boswas_control_center").rglob("*.py") if "__pycache__" not in p.parts)
SCRIPTS = [PACKAGE / "bin" / "boswas-control-center"]
ALL = CODE + SCRIPTS
DATA = sorted((PACKAGE / "data").iterdir())
SHIPPED = ALL + DATA + [PACKAGE / "man" / "boswas-control-center.1"]
PURE = ("__init__.py", "app.py", "backend.py", "catalog.py", "commands.py", "errors.py", "probes.py", "viewmodel.py",
        "jobs.py", "software.py", "android.py", "hardware.py", "sysinfo.py", "filetypes.py", "userfiles.py",
        "overview.py", "servicebridge.py", "device.py", "cleanup.py", "troubleshoot.py", "peripherals.py",
        "netconfig.py", "fonts.py", "installed.py", "features.py", "permissions.py", "updatelog.py")
# Every module directly in the package is Qt-free unless it is listed here (none is: Qt lives in ui/).
QT_MODULES: tuple[str, ...] = ()

EXPECTED_EXECUTABLES = {
    "boswas": "/usr/bin/boswas", "boswas-preset": "/usr/bin/boswas-preset",
    "boswas-compat-manager": "/usr/bin/boswas-compat-manager", "boswas-files": "/usr/bin/boswas-files",
    "gio": "/usr/bin/gio", "ip": "/usr/bin/ip", "timedatectl": "/usr/bin/timedatectl",
    "localectl": "/usr/bin/localectl", "locale": "/usr/bin/locale", "busctl": "/usr/bin/busctl",
    "passwd": "/usr/bin/passwd",
    "apt-config": "/usr/bin/apt-config", "systemctl": "/usr/bin/systemctl",
    "boswas-android": "/usr/bin/boswas-android", "apt-get": "/usr/bin/apt-get", "apt-cache": "/usr/bin/apt-cache",
    "flatpak": "/usr/bin/flatpak", "nmcli": "/usr/bin/nmcli", "lsblk": "/usr/bin/lsblk", "pkexec": "/usr/bin/pkexec",
    "hostnamectl": "/usr/bin/hostnamectl", "fc-list": "/usr/bin/fc-list", "fc-cache": "/usr/bin/fc-cache",
    "lpstat": "/usr/bin/lpstat", "lp": "/usr/bin/lp", "dpkg-query": "/usr/bin/dpkg-query",
    "boswas-camera": "/usr/bin/boswas-camera", "udisksctl": "/usr/bin/udisksctl",
    "dbus-update-activation-environment": "/usr/bin/dbus-update-activation-environment",
}
EXPECTED_SYSOPS = {"apt-refresh", "apt-upgrade", "apt-install", "apt-remove", "apt-install-local", "apt-clean",
                   "journal-vacuum", "firmware-report", "flatpak-remote-enable", "flatpak-remote-disable"}
# Changing files is allowed only in userfiles.py: AppImages, their menu entries, the thumbnail cache, the
# settings files of write_config()/remove_config() (boswas/compositor.toml and its .bak, boswas/appearance.toml,
# mimeapps.list, autostart/<id>.desktop in the user's config directory), the account picture ~/.face, the
# list of recently used files (recently-used.xbel, emptied), the items of the cache folder and of Downloads the
# user picked (deleted; moved to the Trash), the Trash (emptied) and the fonts the user installs.
FILE_CHANGING_OS = {"remove", "unlink", "rename", "replace", "makedirs", "mkdir", "chmod"}
FILE_WRITER = "boswas_control_center/userfiles.py"
FORBIDDEN_MODULES = {"multiprocessing", "pty", "pexpect", "webbrowser", "ctypes", "pickle", "shelve", "marshal",
                     "importlib", "runpy", "code", "codeop", "commands", "popen2", "asyncio"}
FORBIDDEN_OS = {"system", "popen", "fork", "forkpty", "execv", "execve", "execl", "execle", "execlp", "execlpe",
                "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp",
                "spawnvpe", "posix_spawn", "posix_spawnp", "startfile", "kill", "killpg", "remove", "unlink", "rename",
                "renames", "replace", "rmdir", "removedirs", "makedirs", "mkdir", "chmod", "chown", "symlink", "link",
                "truncate", "putenv", "chdir", "setuid", "seteuid", "setgid"}
# subprocess.call and friends are covered by test_subprocess_is_used_only_through_the_checked_runner
# (client.call is the agent socket client).
FORBIDDEN_NAMES = {"QProcess", "QDesktopServices", "QLibrary", "QPluginLoader", "Popen", "check_output",
                   "getoutput", "getstatusoutput"}
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__", "breakpoint"}
# The services bridge is a child process that speaks on pipes: QProcess is used for it, in this module only.
BRIDGE_CLIENT = "boswas_control_center/ui/bridgeclient.py"
# No KDE program is started or probed any more (the native desktop has none of them).
KDE_PROGRAMS = ("kcmshell", "kstart", "kioclient", "kinfocenter", "partitionmanager", "kwalletmanager",
                "plasmashell", "systemsettings", "kde-open", "kreadconfig", "kwriteconfig", "qdbus")
FORBIDDEN_TEXT = ("os.system", "os.popen", "shell=True", "QProcess", "startDetached", "apt install",
                  "aptdaemon", "packagekit", "pkcon", "dpkg -i", "sudo ", "boswas-winapp launch",
                  "boswas-winapp install", "/usr/bin/boswas-winapp", "wine64",
                  "wineserver", "winecfg", "msiexec", "calamares", "debian-installer")
# Ways of recording text (never used where passwords pass through): print, logging, a logger, the standard streams.
RECORDING = (re.compile(r"\bprint\("), re.compile(r"\blogging\b"), re.compile(r"(?<![A-Za-z_])log\."),
             re.compile(r"sys\.std(err|out)"))
WINE_COMMAND = re.compile(r"^(?:/\S*/)?wine(?:64|server|boot|cfg|-preloader)?(?:\s|$)")
HEX_COLOUR = re.compile(r"#([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})(?![0-9A-Fa-f])")


def tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def rel(path: Path) -> str:
    return str(path.relative_to(PACKAGE)).replace("\\", "/")


class ProcessTest(unittest.TestCase):
    def test_code_was_found(self):
        self.assertGreaterEqual(len(CODE), 18)
        self.assertIn("boswas_control_center/commands.py", [rel(p) for p in CODE])

    def test_only_commands_py_imports_subprocess(self):
        users = []
        for path in ALL:
            for node in ast.walk(tree(path)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                if any(name.split(".")[0] == "subprocess" for name in names):
                    users.append(rel(path))
        self.assertEqual(users, ["boswas_control_center/commands.py"])

    def test_no_shell_and_no_code_evaluation(self):
        for path in ALL:
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.keyword) and node.arg == "shell":
                    self.fail(f"{rel(path)}:{node.value.lineno} passes shell=")
                bridge = {"QProcess"} if rel(path) == BRIDGE_CLIENT else set()
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, (FORBIDDEN_BUILTINS | FORBIDDEN_NAMES) - bridge,
                                     f"{rel(path)}:{node.lineno}")
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"QProcess", "QDesktopServices", "startDetached", "check_output",
                                                 "getoutput", "getstatusoutput", "startCommand", "execute"} - bridge,
                                     f"{rel(path)}:{node.lineno} uses .{node.attr}")
                    if isinstance(node.value, ast.Name) and node.value.id == "os":
                        allowed = FILE_CHANGING_OS if rel(path) == FILE_WRITER else set()
                        self.assertNotIn(node.attr, FORBIDDEN_OS - allowed,
                                         f"{rel(path)}:{node.lineno} uses os.{node.attr}")
                if isinstance(node, ast.Import) or (isinstance(node, ast.ImportFrom) and node.level == 0):
                    modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for module in modules:
                        self.assertNotIn(module.split(".")[0], FORBIDDEN_MODULES, f"{rel(path)} imports {module}")

    def test_subprocess_is_used_only_through_the_checked_runner(self):
        source = (PACKAGE / "boswas_control_center" / "commands.py").read_text(encoding="utf-8")
        parsed = ast.parse(source)
        calls = [n for n in ast.walk(parsed) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
        # run(), start(), stream() and converse(): the fourth, on a pseudo-terminal, is the one way a program is
        # started that is not a pipe. It runs exactly /usr/bin/passwd without arguments (the user's own
        # password), see test_the_pseudo_terminal_is_for_passwd_only.
        self.assertEqual(sorted(c.func.attr for c in calls), ["Popen", "Popen", "Popen", "run"])
        for call in calls:
            self.assertIsInstance(call.args[0], ast.Name)
            self.assertEqual(call.args[0].id, "command")
            keywords = {k.arg for k in call.keywords}
            self.assertNotIn("shell", keywords)
            self.assertIn("stdin", keywords)
        run = next(c for c in calls if c.func.attr == "run")
        self.assertIn("timeout", {k.arg for k in run.keywords})
        functions = {n.name: ast.get_source_segment(source, n) for n in ast.walk(parsed)
                     if isinstance(n, ast.FunctionDef)}
        for name in ("run", "start", "stream", "converse"):
            self.assertIn("_check(command)", functions[name])
        self.assertIn("command[0] not in ALLOWED_PATHS", functions["_check"])

    def test_the_pseudo_terminal_is_for_passwd_only(self):
        """One place opens a pseudo-terminal: converse() in commands.py, for `/usr/bin/passwd` and nothing else."""
        for path in ALL:
            text = path.read_text(encoding="utf-8")
            if rel(path) != "boswas_control_center/commands.py":
                for token in ("openpty", "termios", "forkpty", "TIOCSCTTY", "os.read(", "os.write("):
                    if token in ("os.read(", "os.write(") and rel(path) == FILE_WRITER:
                        continue                              # userfiles writes its files through descriptors
                    self.assertNotIn(token, text, f"{rel(path)} uses {token}")
        source = (PACKAGE / "boswas_control_center" / "commands.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("os.openpty()"), 1)
        functions = {n.name: ast.get_source_segment(source, n) for n in ast.walk(ast.parse(source))
                     if isinstance(n, ast.FunctionDef)}
        converse = functions["converse"]
        self.assertIn("os.openpty()", converse)
        self.assertIn("if command != passwd_argv():", converse)
        self.assertLess(converse.index("if command != passwd_argv():"), converse.index("os.openpty()"))
        self.assertIn("~(termios.ECHO | termios.ECHONL)", converse)      # nothing typed is printed back
        for recording in RECORDING:
            self.assertIsNone(recording.search(converse), recording.pattern)   # nothing typed or printed is recorded
        self.assertEqual(functions["passwd_argv"].count("EXECUTABLES[\"passwd\"]"), 1)
        self.assertEqual(commands.passwd_argv(), ["/usr/bin/passwd"])
        for bad in (["/usr/bin/passwd", "root"], ["/usr/bin/passwd", "-d", "bob"], ["/usr/bin/passwd", "--stdin"]):
            with self.assertRaises(ValueError, msg=bad):
                commands._check(bad)
        runner = commands.CommandRunner({})
        for command in (["/usr/bin/busctl", "tree"], commands.argv("locale", "-a"), ["/bin/sh"]):
            with self.assertRaises(ValueError, msg=command):
                runner.converse(command, object(), 5)
        with self.assertRaises(ValueError):
            runner.run(commands.passwd_argv() + ["bob"], 5)

    def test_no_secret_reaches_a_command_line(self):
        """busctl carries names and numbers only: the password methods of AccountsService are not callable."""
        for method in ("SetPassword", "SetPasswordHint", "SetPasswordMode"):
            self.assertNotIn(method, commands.ACCOUNTS_METHODS)
            with self.assertRaises(ValueError):
                commands.accounts_argv(method, "$6$salt$hash", "", path="/org/freedesktop/Accounts/User1001")
        source = (PACKAGE / "boswas_control_center" / "accounts.py").read_text(encoding="utf-8")
        self.assertNotIn("SetPassword\"", source)                        # the Qt-free module never builds that call
        ui = (PACKAGE / "boswas_control_center" / "ui" / "settings" / "accounts.py").read_text(encoding="utf-8")
        self.assertEqual(ui.count('"SetPassword"'), 1)                   # one D-Bus call, in dbus_set_password
        self.assertIn("message.setArguments([hashed, \"\"])", ui)
        for recording in RECORDING:
            self.assertIsNone(recording.search(ui), recording.pattern)
            self.assertIsNone(recording.search(source), recording.pattern)

    def test_allowlist(self):
        self.assertEqual(commands.EXECUTABLES, EXPECTED_EXECUTABLES)
        self.assertEqual(commands.ALLOWED_PATHS, frozenset(EXPECTED_EXECUTABLES.values()))
        for name, (program, args) in catalog.PROGRAMS.items():
            self.assertIn(program, commands.EXECUTABLES, name)
            self.assertTrue(all(isinstance(a, str) for a in args))

    def test_backend_names_its_programs_literally(self):
        source = PACKAGE / "boswas_control_center" / "backend.py"
        for node in ast.walk(tree(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "argv":
                first = node.args[0]
                if isinstance(first, ast.Constant):
                    self.assertIn(first.value, commands.EXECUTABLES)
                else:                                     # argv(name, *args) from catalog.PROGRAMS only
                    self.assertEqual(ast.unparse(first), "name")

    def test_no_kde_program_is_named(self):
        """Nothing starts, probes or mentions a KDE program: the catalog has programs of Boswas only."""
        for path in SHIPPED:
            text = path.read_text(encoding="utf-8")
            for token in KDE_PROGRAMS:
                self.assertNotIn(token, text, f"{rel(path)} names {token}")
        self.assertEqual(set(catalog.PROGRAMS), {"files", "compat-manager", "camera"})
        self.assertEqual(catalog.PROGRAMS["files"], ("boswas-files", ("/",)))
        self.assertEqual(catalog.PROGRAMS["compat-manager"], ("boswas-compat-manager", ()))
        self.assertEqual(catalog.PROGRAMS["camera"], ("boswas-camera", ()))
        for page in catalog.PAGES:
            for program in page.programs:
                self.assertIn(program.id, catalog.PROGRAMS, page.key)
        self.assertFalse(hasattr(catalog, "KCM_MODULES"))
        self.assertFalse(hasattr(backend.Backend, "kcm_availability"))

    def test_forbidden_text(self):
        for path in ALL:
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_TEXT:
                if token == "QProcess" and rel(path) == BRIDGE_CLIENT:
                    continue                              # see test_only_the_bridge_client_uses_qprocess
                self.assertNotIn(token, text, f"{rel(path)} contains {token!r}")

    def test_only_the_bridge_client_uses_qprocess(self):
        """One QProcess, in ui/bridgeclient.py: the bridge by its fixed path, without arguments, never detached."""
        source = (PACKAGE / BRIDGE_CLIENT).read_text(encoding="utf-8")
        parsed = ast.parse(source)
        created = [n for n in ast.walk(parsed) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "QProcess"]
        self.assertEqual(len(created), 1)
        programs = [n for n in ast.walk(parsed) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "setProgram"]
        self.assertEqual([ast.unparse(c.args[0]) for c in programs], ["self.program"])
        arguments = [n for n in ast.walk(parsed) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "setArguments"]
        self.assertEqual([ast.unparse(c.args[0]) for c in arguments], ["[]"])
        starts = [n for n in ast.walk(parsed) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "start" and ast.unparse(n.func.value) == "process"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].args, [])              # start() of the program set above: no command string
        self.assertEqual(starts[0].keywords, [])
        for forbidden in ("startDetached", "startCommand", "setNativeArguments", "splitCommand", "execute("):
            self.assertNotIn(forbidden, source)
        # The program is the constructor's default and nothing in the package passes another one.
        self.assertEqual(servicebridge.PROGRAM, "/usr/libexec/boswas/boswas-services-bridge")
        self.assertIn("program: str = sb.PROGRAM", source)
        for path in CODE:
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                        "BridgeClient", "factory"):
                    self.assertFalse([k for k in node.keywords if k.arg == "program"], rel(path))
                    self.assertLessEqual(len(node.args), 1, rel(path))
        # A passphrase goes to the pipe only: the client never prints, logs or formats a command.
        for forbidden in ("print(", "log.", "logging", "sys.stderr", "describe("):
            self.assertNotIn(forbidden, source, forbidden)

    def test_only_userfiles_writes_files(self):
        for path in CODE:
            if rel(path) == FILE_WRITER:
                continue
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "shutil":
                    self.assertEqual(node.attr, "which", f"{rel(path)}:{node.lineno} uses shutil.{node.attr}")
                if isinstance(node, ast.Attribute) and node.attr in ("write_text", "write_bytes", "unlink", "rmdir",
                                                                     "mkdir", "chmod", "rename", "touch"):
                    self.fail(f"{rel(path)}:{node.lineno} changes files (.{node.attr})")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                    modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
                    modes += [k.value.value for k in node.keywords if k.arg == "mode" and
                              isinstance(k.value, ast.Constant)]
                    self.assertTrue(all(m in ("r", "rb") for m in modes), f"{rel(path)}:{node.lineno} opens for writing")

    def test_pkexec_only_through_the_sysops_builder(self):
        for path in ALL:
            if rel(path) != "boswas_control_center/commands.py":
                self.assertNotIn("pkexec", path.read_text(encoding="utf-8"), rel(path))
        self.assertEqual(commands.SYSOPS, "/usr/libexec/boswas/sysops")
        self.assertEqual(commands.SYSOPS_OPERATIONS, frozenset(EXPECTED_SYSOPS))
        with self.assertRaises(ValueError):
            commands.argv("pkexec", commands.SYSOPS, "apt-refresh")
        self.assertEqual(commands.sysops_argv("apt-install", "vlc"),
                         ["/usr/bin/pkexec", "/usr/libexec/boswas/sysops", "apt-install", "vlc"])
        for bad in (("shell",), ("apt-install",), ("apt-install", "vlc; rm -rf /"), ("apt-install", "-y"),
                    ("apt-install", "vlc", "vlc"), ("apt-refresh", "--now"), ("apt-install-local", "relative.deb"),
                    ("apt-install-local", "/tmp/../etc/x.deb"), ("flatpak-remote-enable", "../x"),
                    ("apt-install", *[f"p{i}" for i in range(51)]), ("android-init",)):
            with self.assertRaises(ValueError, msg=bad):
                commands.sysops_argv(*bad)
        runner = commands.CommandRunner({})
        for command in (["/usr/bin/pkexec", "/bin/sh"], ["/usr/bin/pkexec", commands.SYSOPS, "anything"],
                        ["/usr/bin/pkexec", commands.SYSOPS, "apt-install", "--allow-unauthenticated"]):
            with self.assertRaises(ValueError):
                commands._check(command)
        with self.assertRaises(ValueError):
            runner.run(commands.sysops_argv("apt-refresh"), 5)
        with self.assertRaises(ValueError):
            runner.start(commands.sysops_argv("apt-refresh"))

    def test_apt_get_only_simulates_and_apt_cache_only_reads(self):
        for path in ALL:
            if rel(path) not in ("boswas_control_center/commands.py", "boswas_control_center/backend.py",
                                 "boswas_control_center/software.py"):
                self.assertNotIn("apt-get", path.read_text(encoding="utf-8"), rel(path))
        for node in ast.walk(tree(PACKAGE / "boswas_control_center" / "backend.py")):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "argv" and \
                    isinstance(node.args[0], ast.Constant) and node.args[0].value == "apt-get":
                self.assertEqual(node.args[1].value, "-s")
        for bad in (("apt-get", "install", "vlc"), ("apt-get", "upgrade"), ("apt-cache", "gencaches"),
                    ("flatpak", "install", "flathub", "org.x.Y"), ("flatpak", "remote-add", "x", "y"),
                    ("flatpak", "run", "org.x.Y"), ("nmcli", "connection", "show", "--show-secrets"),
                    ("nmcli", "radio", "all", "off"), ("nmcli", "radio", "wifi", "off"),
                    ("nmcli", "device", "disconnect", "wlp2s0"), ("boswas-android", "status"),
                    ("gio", "open", "/etc/passwd"), ("gio", "launch", "relative.desktop"),
                    ("gio", "launch", "/usr/share/applications/x.desktop", "extra"),
                    ("gio", "launch", "/usr/share/applications/../x.desktop"), ("gio", "launch", "/tmp/x.sh"),
                    ("gio", "trash", "/home/a/file"), ("ip", "addr", "add", "10.0.0.1/8", "dev", "eth0"),
                    ("ip", "link", "set", "eth0", "down"), ("ip", "-j", "addr"), ("ip",), ("ip", "netns", "exec"),
                    ("locale",), ("locale", "-a", "-v"), ("timedatectl", "show"), ("localectl", "status")):
            with self.assertRaises(ValueError, msg=bad):
                commands.argv(*bad)
        with self.assertRaises(ValueError):
            commands._check(["/usr/bin/apt-get", "install", "vlc"])

    def test_timedatectl_and_localectl_only_with_their_fixed_forms(self):
        t, l = commands.EXECUTABLES["timedatectl"], commands.EXECUTABLES["localectl"]
        self.assertEqual(commands.timedatectl_argv("show"), [t, "show"])
        self.assertEqual(commands.timedatectl_argv("set-ntp", "true"), [t, "set-ntp", "true"])
        self.assertEqual(commands.timedatectl_argv("set-timezone", "Europe/Berlin"), [t, "set-timezone", "Europe/Berlin"])
        self.assertEqual(commands.timedatectl_argv("set-timezone", "America/Argentina/Buenos_Aires")[-1],
                         "America/Argentina/Buenos_Aires")
        self.assertEqual(commands.timedatectl_argv("set-timezone", "Etc/GMT+5")[-1], "Etc/GMT+5")
        self.assertEqual(commands.timedatectl_argv("set-time", "2026-11-15 09:30:00")[-1], "2026-11-15 09:30:00")
        for bad in ((), ("status",), ("set-ntp", "yes"), ("set-ntp",), ("set-timezone", "../../etc/passwd"),
                    ("set-timezone", "Europe/../UTC"), ("set-timezone", "/etc/localtime"), ("set-timezone", ""),
                    ("set-timezone", "Europe/Berlin", "extra"), ("set-timezone", "-h"), ("set-timezone", "a b"),
                    ("set-time", "now"), ("set-time", "2026-11-15"), ("set-time", "2026-11-15 09:30:00; reboot"),
                    ("set-local-rtc", "1"), ("--host", "example", "show"), ("show", "--all"),
                    ("set-timezone", "Europe/Berlin\n"), ("set-timezone", None)):
            with self.assertRaises(ValueError, msg=bad):
                commands.timedatectl_argv(*bad)
        self.assertEqual(commands.localectl_argv("status"), [l, "status"])
        self.assertEqual(commands.localectl_argv("set-locale", "LANG=de_DE.UTF-8"), [l, "set-locale", "LANG=de_DE.UTF-8"])
        self.assertEqual(commands.localectl_argv("set-locale", "LANG=en_GB.UTF-8", "LC_TIME=de_DE.UTF-8",
                                                 "LC_MEASUREMENT=C.UTF-8")[1:],
                         ["set-locale", "LANG=en_GB.UTF-8", "LC_TIME=de_DE.UTF-8", "LC_MEASUREMENT=C.UTF-8"])
        self.assertEqual(commands.localectl_argv("set-locale", "LANG=sr_RS.UTF-8@latin")[-1], "LANG=sr_RS.UTF-8@latin")
        for bad in ((), ("set-locale",), ("set-locale", "de_DE.UTF-8"), ("set-locale", "LANG="),
                    ("set-locale", "LANG=../x"), ("set-locale", "LANG=de_DE.UTF-8", "LANG=en_GB.UTF-8"),
                    ("set-locale", "LD_PRELOAD=x"), ("set-locale", "LC_ALL=de_DE.UTF-8"),
                    ("set-locale", "LANG=de DE"), ("set-keymap", "de"), ("set-x11-keymap", "de"),
                    ("status", "--no-pager"), ("set-locale", "LANG=de_DE.UTF-8\n"), ("set-locale", None)):
            with self.assertRaises(ValueError, msg=bad):
                commands.localectl_argv(*bad)
        self.assertEqual(commands.argv("ip", "-j", "addr", "show"), [commands.EXECUTABLES["ip"], "-j", "addr", "show"])
        self.assertEqual(commands.argv("locale", "-a"), ["/usr/bin/locale", "-a"])
        self.assertEqual(commands.argv("gio", "launch", "/usr/share/applications/org.example.App.desktop"),
                         ["/usr/bin/gio", "launch", "/usr/share/applications/org.example.App.desktop"])

    def test_the_changes_of_the_new_pages_have_fixed_forms(self):
        """Renaming, restarting, VPN and adapters, fonts, printing, packages and portal permissions: exact forms only."""
        self.assertEqual(commands.hostnamectl_argv("my-laptop"),
                         ["/usr/bin/hostnamectl", "--static", "set-hostname", "my-laptop"])
        for bad in ("", "-x", "x-", "a b", "a" * 64, "name;reboot", "über", "--pretty", "a.b"):
            with self.assertRaises(ValueError, msg=bad):
                commands.hostnamectl_argv(bad)
        uuid = "5b2c7f2e-1d3a-4c55-9e0b-3f6a7d8e9a10"
        self.assertEqual(commands.nmcli_change_argv("connection", "up", "uuid", uuid),
                         ["/usr/bin/nmcli", "-t", "connection", "up", "uuid", uuid])
        self.assertEqual(commands.nmcli_change_argv("connection", "import", "type", "wireguard", "file",
                                                    "/home/a/wg0.conf")[-2:], ["file", "/home/a/wg0.conf"])
        self.assertEqual(commands.nmcli_change_argv("device", "disconnect", "wlp2s0")[-1], "wlp2s0")
        for bad in (("connection", "up", "id", "Office"), ("connection", "up", "uuid", "x"),
                    ("connection", "modify", "uuid", uuid, "vpn.secrets", "password=x"),
                    ("connection", "import", "type", "vpnc", "file", "/a.conf"),
                    ("connection", "import", "type", "openvpn", "file", "relative.ovpn"),
                    ("connection", "import", "type", "openvpn", "file", "/a/../b.ovpn"),
                    ("connection", "up", "uuid", uuid, "--ask"), ("radio", "all", "off"),
                    ("device", "disconnect", "wlp2s0;x"), ("device", "delete", "wlp2s0"),
                    ("connection", "show", "--show-secrets", uuid)):
            with self.assertRaises(ValueError, msg=bad):
                commands.nmcli_change_argv(*bad)
        for bad in (("nmcli", "-t", "connection", "up", "uuid", uuid), ("nmcli", "-t", "radio", "wifi", "off"),
                    ("nmcli", "-t", "-s", "connection", "show"), ("nmcli", "-t", "--show-secrets", "connection"),
                    ("systemctl", "poweroff"), ("systemctl", "reboot", "--force"), ("systemctl", "start", "x"),
                    ("fc-list",), ("lpstat", "-a", "-l"), ("hostnamectl", "set-hostname", "x"),
                    ("lp", "/etc/shadow"), ("dpkg-query", "-L", "bash"), ("fc-cache", "-f", "/")):
            with self.assertRaises(ValueError, msg=bad):
                commands.argv(*bad)
        self.assertEqual(commands.argv("systemctl", "reboot", "--firmware-setup")[1:], ["reboot", "--firmware-setup"])
        self.assertEqual(commands.fc_cache_argv("/home/a/.local/share/fonts"),
                         ["/usr/bin/fc-cache", "-f", "/home/a/.local/share/fonts"])
        for bad in ("/", "/home/a", "relative/fonts", "/home/a/../fonts", "/usr/share/fonts/../../etc"):
            with self.assertRaises(ValueError, msg=bad):
                commands.fc_cache_argv(bad)
        self.assertEqual(commands.lp_test_page_argv("Office_Laser"),
                         ["/usr/bin/lp", "-d", "Office_Laser", "/usr/share/cups/data/testprint"])
        for bad in ("", "-o", "a b", "x/y", "a#b"):
            with self.assertRaises(ValueError, msg=bad):
                commands.lp_test_page_argv(bad)
        self.assertEqual(commands.dpkg_query_argv("owners", "/usr/share/applications/x.desktop")[1], "-S")
        self.assertEqual(commands.dpkg_query_argv("status", "vlc")[1:3], ["-W", commands.DPKG_STATUS_FORMAT])
        for bad in (("owners", "relative"), ("status", "Bad Name"), ("purge", "vlc"), ("status",)):
            with self.assertRaises(ValueError, msg=bad):
                commands.dpkg_query_argv(*bad)
        self.assertEqual(commands.permission_remove_argv("devices", "camera", "org.example.Meet"),
                         ["/usr/bin/flatpak", "permission-remove", "devices", "camera", "org.example.Meet"])
        for bad in (("devices", "camera", "notanapp"), ("../x", "camera", "org.a.B"), ("devices", "", "org.a.B")):
            with self.assertRaises(ValueError, msg=bad):
                commands.permission_remove_argv(*bad)
        self.assertEqual(commands.logind_argv("CanRebootToFirmwareSetup")[6:],
                         ["org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                          "CanRebootToFirmwareSetup"])
        for bad in ("PowerOff", "Reboot", "SetRebootToFirmwareSetup", "KillUser"):
            with self.assertRaises(ValueError, msg=bad):
                commands.logind_argv(bad)
        # The runner checks every one of them again before anything starts.
        for command in (["/usr/bin/hostnamectl", "set-hostname", "x"], ["/usr/bin/hostnamectl", "--static",
                                                                       "set-hostname", "-x"],
                        ["/usr/bin/nmcli", "-t", "connection", "delete", "id", "x"], ["/usr/bin/lp", "-d", "p", "/x"],
                        ["/usr/bin/fc-cache", "-r"], ["/usr/bin/dpkg-query", "--purge", "x"],
                        ["/usr/bin/flatpak", "permission-remove", "devices"], ["/usr/bin/systemctl", "halt"],
                        commands.logind_argv("CanRebootToFirmwareSetup")[:-1] + ["PowerOff"]):
            with self.assertRaises(ValueError, msg=command):
                commands._check(command)
        for command in (commands.hostnamectl_argv("pc"), commands.nmcli_change_argv("device", "connect", "eth0"),
                        commands.fc_cache_argv("/h/.local/share/fonts"), commands.lp_test_page_argv("P"),
                        commands.dpkg_query_argv("status", "a", "b"), commands.permission_remove_argv("a", "b", "c.d.E"),
                        commands.logind_argv("CanRebootToBootLoaderMenu"), commands.argv("systemctl", "is-active",
                                                                                         "bluetooth.service")):
            commands._check(command)

    def test_the_forms_of_eject_ethernet_and_the_pointer_size(self):
        uuid = "5b2c7f2e-1d3a-4c55-9e0b-3f6a7d8e9a10"
        modify = ("connection", "modify", "uuid", uuid)
        good = [("ipv4.method", "manual", "ipv4.addresses", "192.168.1.20/24", "ipv4.gateway", "192.168.1.1"),
                ("ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", ""),
                ("ipv4.dns", "1.1.1.1,9.9.9.9", "ipv4.ignore-auto-dns", "yes", "ipv6.dns", "2606:4700:4700::1111",
                 "ipv6.ignore-auto-dns", "yes"),
                ("ipv4.dns", "", "ipv4.ignore-auto-dns", "no", "ipv6.dns", "", "ipv6.ignore-auto-dns", "no"),
                ("connection.metered", "yes"), ("connection.metered", "unknown"), ("ipv4.addresses", "10.0.0.5/31")]
        for words in good:
            command = commands.nmcli_change_argv(*modify, *words)
            self.assertEqual(command, ["/usr/bin/nmcli", "-t", *modify, *words])
            commands._check(command)
        bad = [("ipv4.method", "link-local"), ("ipv4.method", "manual", "ipv4.method", "auto"),
               ("ipv4.addresses", "192.168.1.0/24"), ("ipv4.addresses", "192.168.1.255/24"),
               ("ipv4.addresses", "192.168.1.20"), ("ipv4.addresses", "192.168.1.20/33"),
               ("ipv4.addresses", "192.168.1.20/024"), ("ipv4.addresses", "127.0.0.1/8"),
               ("ipv4.addresses", "224.0.0.1/4"), ("ipv4.gateway", "fe80::1"), ("ipv4.gateway", " 192.168.1.1"),
               ("ipv4.dns", "1.1.1.1 8.8.8.8"), ("ipv4.dns", "1.1.1.1,1.1.1.1"),
               ("ipv4.dns", "1.1.1.1,2.2.2.2,3.3.3.3,4.4.4.4"), ("ipv4.dns", "::1"), ("ipv6.dns", "1.1.1.1"),
               ("ipv6.dns", "2001:0db8::1"), ("connection.metered", "maybe"),
               ("ipv4.ignore-auto-dns", "true"), ("802-1x.password", "secret"), ("vpn.secrets", "password=x"),
               ("connection.id", "x"), ("ipv4.method",), ()]
        for words in bad:
            with self.assertRaises(ValueError, msg=words):
                commands.nmcli_change_argv(*modify, *words)
        with self.assertRaises(ValueError):
            commands.nmcli_change_argv("connection", "modify", "id", "Wired", "connection.metered", "yes")
        self.assertEqual(commands.udisksctl_argv("power-off", "/dev/sdb"), ["/usr/bin/udisksctl", "power-off", "-b",
                                                                            "/dev/sdb"])
        for action, device in (("unmount", "/dev/sdb1"), ("lock", "/dev/sdc2"), ("unmount", "/dev/dm-3"),
                               ("power-off", "/dev/mmcblk0"), ("unmount", "/dev/mmcblk0p1")):
            commands._check(commands.udisksctl_argv(action, device))
        for action, device in (("mount", "/dev/sdb1"), ("format", "/dev/sdb"), ("unmount", "/dev/../etc/passwd"),
                               ("unmount", "sdb1"), ("unmount", "/dev/sda1 /dev/sdb"), ("power-off", "/dev/loop0"),
                               ("unmount", "-b")):
            with self.assertRaises(ValueError, msg=(action, device)):
                commands.udisksctl_argv(action, device)
        self.assertEqual(commands.files_argv("/media/alice/USB STICK"),
                         ["/usr/bin/boswas-files", "/media/alice/USB STICK"])
        commands._check(commands.files_argv("computer:///"))
        for folder in ("relative", "/media/../etc", "", "trash:///", "-x"):
            with self.assertRaises(ValueError, msg=folder):
                commands.files_argv(folder)
        self.assertEqual(commands.cursor_environment_argv("systemctl", 48),
                         ["/usr/bin/systemctl", "--user", "set-environment", "XCURSOR_SIZE=48"])
        self.assertEqual(commands.cursor_environment_argv("dbus-update-activation-environment", 16),
                         ["/usr/bin/dbus-update-activation-environment", "XCURSOR_SIZE=16"])
        for program, size in (("systemctl", 15), ("systemctl", 97), ("systemctl", True), ("systemctl", "48"),
                              ("busctl", 48)):
            with self.assertRaises(ValueError, msg=(program, size)):
                commands.cursor_environment_argv(program, size)
        for command in (["/usr/bin/systemctl", "--user", "set-environment", "LD_PRELOAD=/tmp/x.so"],
                        ["/usr/bin/systemctl", "--user", "set-environment", "XCURSOR_SIZE=48", "PATH=/tmp"],
                        ["/usr/bin/systemctl", "--user", "unset-environment", "XCURSOR_SIZE"],
                        ["/usr/bin/dbus-update-activation-environment", "--systemd", "XCURSOR_SIZE=48"],
                        ["/usr/bin/dbus-update-activation-environment", "XCURSOR_SIZE=480"],
                        ["/usr/bin/udisksctl", "mount", "-b", "/dev/sdb1"],
                        ["/usr/bin/udisksctl", "unmount", "/dev/sdb1"],
                        ["/usr/bin/boswas-files", "/a", "/b"], ["/usr/bin/boswas-files", "relative"]):
            with self.assertRaises(ValueError, msg=command):
                commands._check(command)
        for bad in (("udisksctl", "unmount", "-b", "/dev/sdb1"), ("dbus-update-activation-environment", "X=1")):
            with self.assertRaises(ValueError, msg=bad):
                commands.argv(*bad)
        # apt-install's one option: first, once, and only for apt-install.
        self.assertEqual(commands.sysops_argv("apt-install", "--no-recommends", "cups")[2:],
                         ["apt-install", "--no-recommends", "cups"])
        for bad in (("apt-install", "cups", "--no-recommends"), ("apt-install", "--no-recommends"),
                    ("apt-install", "--no-recommends", "--no-recommends", "cups"),
                    ("apt-remove", "--no-recommends", "cups"), ("apt-install", "--install-suggests", "cups")):
            with self.assertRaises(ValueError, msg=bad):
                commands.sysops_argv(*bad)

    def test_operations_named_by_the_ui_are_allowed(self):
        """Every JobSpec in the code names an allowed operation (checked from the source, not at run time)."""
        found = 0
        for path in CODE:
            for node in ast.walk(tree(path)):
                if not (isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) ==
                        "JobSpec"):
                    continue
                kind = ast.unparse(node.args[0])
                words = node.args[1]
                if not isinstance(words, ast.Tuple):
                    continue
                first = [e.value for e in words.elts if isinstance(e, ast.Constant)]
                found += 1
                if kind == "jobs.SYSOPS":
                    self.assertIn(first[0], EXPECTED_SYSOPS, f"{rel(path)}:{node.lineno}")
                else:
                    self.assertEqual(kind, "jobs.ANDROID", f"{rel(path)}:{node.lineno}")
                    self.assertTrue(tuple(first[:2]) in commands.ANDROID_COMMANDS or
                                    tuple(first[:1]) in commands.ANDROID_COMMANDS, f"{rel(path)}:{node.lineno}")
        self.assertGreaterEqual(found, 10)
        self.assertEqual(jobs.SYSOPS, "sysops")
        # The Android page starts its operations through AndroidPage._start(words, title).
        android_found = 0
        for node in ast.walk(tree(PACKAGE / "boswas_control_center" / "ui" / "android_page.py")):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "_start" and node.args and                     isinstance(node.args[0], ast.Tuple):
                elements = node.args[0].elts
                words = tuple(e.value for e in elements if isinstance(e, ast.Constant))
                android_found += 1
                if len(words) < len(elements):           # ("service", verb): verb comes from a fixed table
                    self.assertTrue(any(c[:len(words)] == words for c in commands.ANDROID_COMMANDS),
                                    f"android_page.py:{node.lineno} {words}")
                else:
                    self.assertTrue(words[:2] in commands.ANDROID_COMMANDS or words[:1] in commands.ANDROID_COMMANDS,
                                    f"android_page.py:{node.lineno} {words}")
        self.assertGreaterEqual(android_found, 4)

    def test_android_commands(self):
        self.assertEqual(commands.android_argv("status"), ["/usr/bin/boswas-android", "--json", "status"])
        self.assertEqual(commands.android_argv("runtime", "remove", "--purge-data"),
                         ["/usr/bin/boswas-android", "--json", "runtime", "remove", "--purge-data"])
        self.assertEqual(commands.android_argv("apk", "install", "/home/a/x.apk")[-1], "/home/a/x.apk")
        for bad in (("shell",), ("apk", "install", "x.apk"), ("apk", "install", "/a/../b.apk"),
                    ("app", "launch", "not a package"), ("runtime", "init", "--gapps"), ("status", "--all"),
                    ("google-play", "install"), ("runtime", "remove", "--force")):
            with self.assertRaises(ValueError, msg=bad):
                commands.android_argv(*bad)

    def test_no_wine_commands(self):
        for path in ALL:
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip() != "wine":
                    self.assertIsNone(WINE_COMMAND.match(node.value.strip()), f"{rel(path)}:{node.lineno}")


class ServiceTest(unittest.TestCase):
    def calls(self, method: str) -> set[str]:
        found = set()
        for node in ast.walk(tree(PACKAGE / "boswas_control_center" / "backend.py")):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == method:
                self.assertIsInstance(node.args[0], ast.Constant, f"{method} must name its operation literally")
                found.add(node.args[0].value)
        return found

    def test_read_only_operations(self):
        self.assertEqual(self.calls("_session_call"), {"apps.list", "system.status"})
        self.assertEqual(self.calls("_agent_call"), {"agent.status", "device.config", "runtime.status"})
        self.assertEqual(backend.SESSION_OPERATIONS, frozenset({"apps.list", "system.status"}))
        self.assertEqual(backend.AGENT_OPERATIONS, frozenset({"agent.status", "device.config", "runtime.status"}))

    def test_only_the_backend_talks_to_sockets(self):
        for path in CODE:
            if path.name != "backend.py":
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("ApiClient", text, rel(path))
                self.assertNotIn("localapi", text, rel(path))

    def test_non_ui_modules_have_no_qt(self):
        top = sorted(p.name for p in (PACKAGE / "boswas_control_center").glob("*.py"))
        for name in sorted(set(PURE) | (set(top) - set(QT_MODULES))):
            for node in ast.walk(tree(PACKAGE / "boswas_control_center" / name)):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for module in modules:
                        if name == "app.py" and module.startswith("PySide6"):
                            continue                         # imported inside main(), after the root check
                        self.assertFalse(module.startswith("PySide"), f"{name} imports {module}")

    def test_app_checks_root_before_qt(self):
        source = (PACKAGE / "boswas_control_center" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("PySide6", source)               # Qt comes in with the toolkit and the window
        for late in ("import boswas_fluent", "from .ui.main_window import", "from .backend import"):
            self.assertLess(source.index("if running_as_root():"), source.index(late), late)
        self.assertLess(source.index("return EXIT_ROOT"), source.index("import boswas_fluent"))
        self.assertIn("EXIT_ROOT = 4", source)
        self.assertIn("fluent.run(DESKTOP_ID", source)      # one instance per session, by the toolkit


class DataTest(unittest.TestCase):
    @staticmethod
    def entry(name: str) -> dict[str, str]:
        group, values = None, {}
        for line in (PACKAGE / "data" / name).read_text(encoding="utf-8").splitlines():
            if line.startswith("["):
                group = line
            elif "=" in line and group == "[Desktop Entry]":
                key, _, value = line.partition("=")
                values[key] = value
        return values

    def test_entries(self):
        expected = {
            "com.boswas.ControlCenter.desktop": ("Boswas Control Center", "boswas-control-center",
                                                 "boswas-control-center"),
            "com.boswas.SecurityCenter.desktop": ("Boswas Security Center", "boswas-control-center --page security",
                                                  "boswas-security-center"),
            "com.boswas.SoftwareCenter.desktop": ("Boswas Software Center",
                                                  "boswas-control-center --page applications",
                                                  "boswas-software-center"),
            "com.boswas.UpdateCenter.desktop": ("Boswas Update Center", "boswas-control-center --page updates",
                                                "boswas-update-center"),
        }
        self.assertEqual(sorted(p.name for p in DATA), sorted(expected))
        for name, (title, exec_line, icon) in expected.items():
            entry = self.entry(name)
            self.assertEqual(entry["Type"], "Application", name)
            self.assertEqual(entry["Name"], title)
            self.assertEqual(entry["Exec"], exec_line)
            self.assertEqual(entry["Icon"], icon)
            self.assertEqual(entry["Terminal"], "false")
            for key in ("Categories", "Keywords"):
                self.assertTrue(entry[key].endswith(";"), f"{name} {key}")
            self.assertIn("Qt", entry["Categories"].split(";"))
            self.assertNotIn("MimeType", entry)
        self.assertEqual(self.entry("com.boswas.ControlCenter.desktop")["Categories"], "Settings;System;Qt;")

    def test_page_options_exist(self):
        for path in DATA:
            exec_line = self.entry(path.name)["Exec"].split()
            if "--page" in exec_line:
                self.assertIn(exec_line[exec_line.index("--page") + 1], catalog.PAGE_KEYS)

    def test_no_install_entry(self):
        for path in DATA:
            self.assertNotIn("Install Boswas OS", path.read_text(encoding="utf-8"), path.name)

    def test_no_teal(self):
        """No colour with a teal/cyan hue (160-200 degrees) anywhere in the data, code or man page."""
        for path in SHIPPED:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("teal", text.casefold(), rel(path))
            for match in HEX_COLOUR.finditer(text):
                value = match.group(1)
                if len(value) == 3:
                    value = "".join(c * 2 for c in value)
                r, g, b = (int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
                hue, lightness, saturation = colorsys.rgb_to_hls(r, g, b)
                teal = 160 <= hue * 360 <= 200 and saturation > 0.2 and 0.1 < lightness < 0.9
                self.assertFalse(teal, f"{rel(path)} uses the teal colour #{value}")

    def test_no_hard_coded_colours(self):
        """Every colour is a Boswas UI token, taken through boswas_fluent; the status icons included."""
        for path in CODE:
            colours = HEX_COLOUR.findall(path.read_text(encoding="utf-8"))
            self.assertEqual(colours, [], rel(path))


class SourceHygieneTest(unittest.TestCase):
    def test_no_invisible_characters_and_lf_only(self):
        for path in SHIPPED + sorted((PACKAGE / "tests").glob("*.py")) + sorted((PACKAGE / "debian").rglob("*")):
            if path.is_dir():
                continue
            data = path.read_bytes()
            self.assertNotIn(b"\r", data, f"{rel(path)} has CRLF line endings")
            for number, line in enumerate(data.decode("utf-8").split("\n"), 1):
                for ch in line:
                    category = unicodedata.category(ch)
                    hidden = (category in ("Cc", "Cf") and ch != "\t") or (category == "Zs" and ch != " ")
                    self.assertFalse(hidden, f"{rel(path)}:{number} contains U+{ord(ch):04X}")

    def test_man_page_lists_every_page(self):
        man = (PACKAGE / "man" / "boswas-control-center.1").read_text(encoding="utf-8").replace("\\-", "-")
        for key in catalog.PAGE_KEYS:
            self.assertIn(key, man)

    def test_widget_tests_guard_qt(self):
        """test_ui.py only uses Qt names inside the PySide6 guard (the suite runs without PySide6)."""
        source = (PACKAGE / "tests" / "test_ui.py").read_text(encoding="utf-8")
        parsed = ast.parse(source)
        for node in parsed.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
                self.assertFalse((module or "").startswith(("PySide6", "boswas_control_center.ui")), module)


if __name__ == "__main__":
    unittest.main()
