"""Static guarantees of the Compatibility Manager's code.

The GUI is a front end only: it must never start a process (Wine,
bubblewrap, the WinCompat command or anything else) and never read or write
application, policy, catalog or Wine files. Everything goes through the two
local sockets in backend.py. These tests read the source and fail when that
boundary is crossed.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import ast
import re
import unicodedata
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
CODE = sorted((PACKAGE / "boswas_manager").rglob("*.py"))
SCRIPTS = [PACKAGE / "bin" / "boswas-compat-manager"]
ALL = CODE + SCRIPTS

FORBIDDEN_MODULES = {"subprocess", "multiprocessing", "pty", "pexpect", "webbrowser", "shutil", "glob", "pathlib",
                     "ctypes", "socket", "asyncio", "signal", "tempfile", "sqlite3", "io", "codecs", "fcntl",
                     "pickle", "shelve", "dbm", "importlib", "runpy", "code", "commands", "popen2"}
FORBIDDEN_OS = {"system", "popen", "fork", "forkpty", "execv", "execve", "execl", "execle", "execlp", "execlpe",
                "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp",
                "spawnvpe", "posix_spawn", "posix_spawnp", "startfile", "kill", "killpg", "listdir", "scandir",
                "walk", "remove", "unlink", "rename", "renames", "replace", "rmdir", "removedirs", "makedirs",
                "mkdir", "chmod", "chown", "lchown", "symlink", "link", "open", "fdopen", "truncate", "getenv",
                "putenv", "chdir", "access", "stat", "lstat", "readlink", "expanduser", "expandvars", "exists",
                "isfile", "isdir", "getsize"}
FORBIDDEN_NAMES = {"QProcess", "QDesktopServices", "QFileSystemWatcher", "QDir", "QFile", "QSaveFile", "QSettings",
                   "QLibrary", "QPluginLoader", "QTemporaryFile", "QTemporaryDir", "QLockFile", "QStandardPaths",
                   "Popen"}
# Builtins that run code; only bare names count (dialog.exec() and re.compile are fine).
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__", "breakpoint"}
FORBIDDEN_TEXT = ("subprocess", "os.system", "os.popen", "QProcess", "Popen", "startDetached", "bwrap",
                  "boswas-winapp", "/etc/boswas", "/var/lib/boswas", "XDG_DATA_HOME", "expanduser", "Path.home",
                  "/usr/bin/wine", "/usr/lib/wine", "wineserver", "wine64", "wineboot", "winecfg", "msiexec")
WINE_COMMAND = re.compile(r"^(?:/\S*/)?wine(?:64|server|boot|cfg|-preloader)?(?:\s|$)")
USER_DATA = ".local/share/boswas"


def tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def rel(path: Path) -> str:
    return str(path.relative_to(PACKAGE)).replace("\\", "/")


class NoProcessesTest(unittest.TestCase):
    def test_code_was_found(self):
        self.assertGreaterEqual(len(CODE), 10)
        self.assertIn("boswas_manager/backend.py", [rel(p) for p in CODE])

    def test_no_forbidden_imports(self):
        for path in ALL:
            for node in ast.walk(tree(path)):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                    names += [f"{node.module}.{alias.name}" for alias in node.names]
                for name in names:
                    self.assertNotIn(name.split(".")[0], FORBIDDEN_MODULES, f"{rel(path)} imports {name}")
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        self.assertNotIn(alias.name, FORBIDDEN_NAMES, f"{rel(path)} imports {alias.name}")

    def test_no_process_or_file_apis(self):
        for path in ALL:
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, FORBIDDEN_NAMES | FORBIDDEN_BUILTINS,
                                     f"{rel(path)}:{node.lineno} uses {node.id}")
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, FORBIDDEN_NAMES, f"{rel(path)}:{node.lineno} uses .{node.attr}")
                    base = node.value
                    if isinstance(base, ast.Name) and base.id == "os":
                        self.assertNotIn(node.attr, FORBIDDEN_OS, f"{rel(path)}:{node.lineno} uses os.{node.attr}")
                    if isinstance(base, ast.Attribute) and base.attr == "path" and isinstance(base.value, ast.Name) \
                            and base.value.id == "os":
                        self.assertNotIn(node.attr, FORBIDDEN_OS, f"{rel(path)}:{node.lineno} uses os.path.{node.attr}")

    def test_forbidden_text(self):
        for path in ALL:
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_TEXT:
                self.assertFalse(token in text, f"{rel(path)} contains {token!r}")

    def test_no_wine_command_strings(self):
        for path in ALL:
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    value = node.value.strip()
                    if value != "wine":                   # the runtime status key, not a command
                        self.assertIsNone(WINE_COMMAND.match(value), f"{rel(path)}:{node.lineno} {node.value!r}")

    def test_no_home_lookups(self):
        for path in ALL:
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.Constant) and node.value in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
                    self.fail(f"{rel(path)}:{node.lineno} reads {node.value}")


class NoFilesTest(unittest.TestCase):
    def test_user_data_path_only_as_a_display_label(self):
        """~/.local/share/boswas appears once: the abbreviated location shown on the Overview tab."""
        hits = []
        for path in ALL:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if USER_DATA in line:
                    hits.append((rel(path), line.strip()))
        self.assertEqual(hits, [("boswas_manager/viewmodel.py", 'PREFIX_DISPLAY_ROOT = "~/.local/share/boswas/wine"')])

    def test_open_only_for_a_user_chosen_export(self):
        found = []
        for path in ALL:
            parsed = tree(path)
            owners: dict[int, str] = {}
            for function in ast.walk(parsed):        # breadth first: inner functions overwrite outer ones
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(function):
                        owners[id(node)] = function.name
            for node in ast.walk(parsed):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                    found.append((rel(path), owners.get(id(node)), node))
        self.assertEqual([(f[0], f[1]) for f in found], [("boswas_manager/ui/common.py", "export_text")])
        mode = found[0][2].args[1]
        self.assertIsInstance(mode, ast.Constant)
        self.assertEqual(mode.value, "w")

    def test_export_is_reached_only_from_a_save_dialog(self):
        users = [rel(p) for p in CODE if "export_text(" in p.read_text(encoding="utf-8")
                 and not p.name == "common.py"]
        self.assertEqual(users, ["boswas_manager/ui/details.py"])
        details = (PACKAGE / "boswas_manager" / "ui" / "details.py").read_text(encoding="utf-8")
        self.assertIn("QFileDialog.getSaveFileName", details)


class BackendBoundaryTest(unittest.TestCase):
    SESSION = {"apps.list", "apps.status", "apps.inspect", "apps.install", "apps.upgrade", "apps.repair",
               "apps.remove", "apps.launch", "apps.stop", "apps.logs", "apps.clear_logs", "catalog.list",
               "system.status", "jobs.get", "jobs.list", "events.list"}
    AGENT = {"agent.status", "policy.status", "commands.list"}

    def calls(self, method: str) -> set[str]:
        found = set()
        for node in ast.walk(tree(PACKAGE / "boswas_manager" / "backend.py")):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == method:
                first = node.args[0]
                self.assertIsInstance(first, ast.Constant, f"{method} must name its operation literally")
                found.add(first.value)
        return found

    def test_session_operations(self):
        self.assertEqual(self.calls("_session_call"), self.SESSION)

    def test_device_agent_is_read_only(self):
        self.assertEqual(self.calls("_agent_call"), self.AGENT)
        source = (PACKAGE / "boswas_manager" / "backend.py").read_text(encoding="utf-8")
        self.assertIn('AGENT_OPERATIONS = frozenset({"agent.status", "policy.status", "commands.list"})', source)

    def test_only_the_backend_talks_to_sockets(self):
        for path in CODE:
            text = path.read_text(encoding="utf-8")
            if path.name != "backend.py":
                self.assertNotIn("ApiClient", text, rel(path))
                self.assertNotIn("localapi", text, rel(path))

    def test_no_permission_editing_api(self):
        source = (PACKAGE / "boswas_manager" / "backend.py").read_text(encoding="utf-8")
        methods = {n.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)
                   and not n.name.startswith("_")}
        self.assertEqual(methods, {"session_socket_path", "list_apps", "app_status", "inspect", "install", "upgrade",
                                   "repair", "remove", "job", "jobs", "launch", "stop", "logs", "clear_logs",
                                   "catalog", "system_status", "events", "agent_status", "policy_status",
                                   "remote_commands"})
        for path in CODE:
            text = path.read_text(encoding="utf-8").casefold()
            for word in ("set_permission", "grant_permission", "set_policy", "policy.set", "unlisted_apps=allow"):
                self.assertNotIn(word, text, rel(path))

    def test_viewmodel_and_backend_have_no_qt(self):
        for name in ("viewmodel.py", "backend.py", "__init__.py"):
            for node in ast.walk(tree(PACKAGE / "boswas_manager" / name)):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for module in modules:
                        self.assertFalse(module.startswith("PySide"), f"{name} imports {module}")


class SourceHygieneTest(unittest.TestCase):
    def test_no_invisible_characters(self):
        """No control, format (bidirectional) or odd space characters hide in the shipped sources."""
        shipped = ALL + sorted((PACKAGE / "data").iterdir()) + [PACKAGE / "man" / "boswas-compat-manager.1"]
        for path in shipped:
            for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                for ch in line:
                    category = unicodedata.category(ch)
                    hidden = category in ("Cc", "Cf") or (category == "Zs" and ch != " ")
                    self.assertFalse(hidden, f"{rel(path)}:{number} contains U+{ord(ch):04X}")


class DataFilesTest(unittest.TestCase):
    @staticmethod
    def entries(path: Path) -> dict[str, dict[str, str]]:
        groups: dict[str, dict[str, str]] = {}
        current = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("[") and line.endswith("]"):
                current = groups.setdefault(line[1:-1], {})
            elif "=" in line and current is not None:
                key, _, value = line.partition("=")
                current[key] = value
        return groups

    def test_application_entry(self):
        entry = self.entries(PACKAGE / "data" / "com.boswas.CompatibilityManager.desktop")["Desktop Entry"]
        self.assertEqual(entry["Name"], "Compatibility Manager")
        self.assertEqual(entry["GenericName"], "Windows Application Manager")
        self.assertEqual(entry["Exec"], "boswas-compat-manager")
        self.assertEqual(entry["Icon"], "boswas-compat-manager")
        self.assertEqual(entry["Terminal"], "false")
        self.assertEqual(entry["Categories"], "System;Settings;")
        self.assertNotIn("MimeType", entry)           # "Run with Boswas" stays the default handler

    def test_service_menu(self):
        groups = self.entries(PACKAGE / "data" / "boswas-compat-manager-install.desktop")
        self.assertEqual(groups["Desktop Entry"]["Type"], "Service")
        self.assertEqual(groups["Desktop Entry"]["Actions"], "install;")
        self.assertIn("application/x-msi", groups["Desktop Entry"]["MimeType"])
        action = groups["Desktop Action install"]
        self.assertEqual(action["Name"], "Install with Boswas Compatibility Manager")
        self.assertEqual(action["Exec"], "boswas-compat-manager --install %f")

    def test_icon_is_self_contained(self):
        svg = (PACKAGE / "data" / "boswas-compat-manager.svg").read_text(encoding="utf-8")
        self.assertIn("#EBC786", svg)                     # brand gold (desktop/branding/README.md)
        self.assertIn("#080C16", svg)                     # brand navy
        self.assertNotIn("href", svg)
        self.assertNotIn("<script", svg)


if __name__ == "__main__":
    unittest.main()
