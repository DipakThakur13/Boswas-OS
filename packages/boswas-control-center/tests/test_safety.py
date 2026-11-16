"""Static guarantees of Control Center's code and data.

Least privilege: only commands.py may start a process, always from an
argument list whose program is in the fixed allowlist, never through a
shell; the one exception is the services bridge, a long-lived child that
only ui/bridgeclient.py starts, by its fixed path and without arguments.
Nothing evaluates code; nothing starts Wine or a KDE program; the device
agent and session agent are used read-only. Privileged work goes only
through `pkexec /usr/libexec/boswas/sysops <operation>` with an operation of
a fixed set and validated arguments; apt-get only ever simulates;
timedatectl, localectl, ip, locale, gio and nmcli only with their fixed
argument forms; only userfiles.py writes or deletes files. The desktop entries are valid and there is no
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
        "overview.py", "servicebridge.py")
# Every module directly in the package is Qt-free unless it is listed here (none is: Qt lives in ui/).
QT_MODULES: tuple[str, ...] = ()

EXPECTED_EXECUTABLES = {
    "boswas": "/usr/bin/boswas", "boswas-preset": "/usr/bin/boswas-preset",
    "boswas-compat-manager": "/usr/bin/boswas-compat-manager", "boswas-files": "/usr/bin/boswas-files",
    "gio": "/usr/bin/gio", "ip": "/usr/bin/ip", "timedatectl": "/usr/bin/timedatectl",
    "localectl": "/usr/bin/localectl", "locale": "/usr/bin/locale",
    "apt-config": "/usr/bin/apt-config", "systemctl": "/usr/bin/systemctl",
    "boswas-android": "/usr/bin/boswas-android", "apt-get": "/usr/bin/apt-get", "apt-cache": "/usr/bin/apt-cache",
    "flatpak": "/usr/bin/flatpak", "nmcli": "/usr/bin/nmcli", "lsblk": "/usr/bin/lsblk", "pkexec": "/usr/bin/pkexec",
}
EXPECTED_SYSOPS = {"apt-refresh", "apt-upgrade", "apt-install", "apt-remove", "apt-install-local", "apt-clean",
                   "journal-vacuum", "firmware-report", "flatpak-remote-enable", "flatpak-remote-disable"}
# Changing files is allowed only in userfiles.py: AppImages, their menu entries, the thumbnail cache, the
# settings files of write_config()/remove_config() (boswas/compositor.toml and its .bak, boswas/appearance.toml,
# mimeapps.list, autostart/<id>.desktop in the user's config directory), the account picture ~/.face and the
# list of recently used files (recently-used.xbel, emptied).
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
        self.assertEqual(sorted(c.func.attr for c in calls), ["Popen", "Popen", "run"])
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
        for name in ("run", "start", "stream"):
            self.assertIn("_check(command)", functions[name])
        self.assertIn("command[0] not in ALLOWED_PATHS", functions["_check"])

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
        self.assertEqual(set(catalog.PROGRAMS), {"files", "compat-manager"})
        self.assertEqual(catalog.PROGRAMS["files"], ("boswas-files", ("/",)))
        self.assertEqual(catalog.PROGRAMS["compat-manager"], ("boswas-compat-manager", ()))
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
        man = (PACKAGE / "man" / "boswas-control-center.1").read_text(encoding="utf-8")
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
