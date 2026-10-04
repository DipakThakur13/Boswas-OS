"""Static guarantees of Control Center's code and data.

Least privilege: only commands.py may start a process, always from an
argument list whose program is in the fixed allowlist, never through a
shell; nothing evaluates code; nothing talks to a package manager or starts
Wine; the device agent and session agent are used read-only. The desktop
entries are valid and there is no Install Boswas OS entry (the image
generates it in live sessions only). No teal anywhere in the data or code.

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

from boswas_control_center import backend, catalog, commands  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
CODE = sorted((PACKAGE / "boswas_control_center").rglob("*.py"))
SCRIPTS = [PACKAGE / "bin" / "boswas-control-center"]
ALL = CODE + SCRIPTS
DATA = sorted((PACKAGE / "data").iterdir())
SHIPPED = ALL + DATA + [PACKAGE / "man" / "boswas-control-center.1"]
PURE = ("__init__.py", "app.py", "backend.py", "catalog.py", "commands.py", "errors.py", "probes.py", "viewmodel.py")

EXPECTED_EXECUTABLES = {
    "boswas": "/usr/bin/boswas", "boswas-preset": "/usr/bin/boswas-preset",
    "boswas-compat-manager": "/usr/bin/boswas-compat-manager", "kcmshell6": "/usr/bin/kcmshell6",
    "kstart": "/usr/bin/kstart", "kioclient": "/usr/bin/kioclient", "dolphin": "/usr/bin/dolphin",
    "partitionmanager": "/usr/bin/partitionmanager", "kinfocenter": "/usr/bin/kinfocenter",
    "plasmashell": "/usr/bin/plasmashell", "apt-config": "/usr/bin/apt-config", "systemctl": "/usr/bin/systemctl",
}
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
FORBIDDEN_TEXT = ("os.system", "os.popen", "shell=True", "QProcess", "startDetached", "apt-get", "apt install",
                  "aptdaemon", "packagekit", "pkcon", "dpkg -i", "pkexec", "sudo ", "boswas-winapp launch",
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
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, FORBIDDEN_BUILTINS | FORBIDDEN_NAMES, f"{rel(path)}:{node.lineno}")
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"QProcess", "QDesktopServices", "startDetached", "check_output",
                                                 "getoutput", "getstatusoutput"},
                                     f"{rel(path)}:{node.lineno} uses .{node.attr}")
                    if isinstance(node.value, ast.Name) and node.value.id == "os":
                        self.assertNotIn(node.attr, FORBIDDEN_OS, f"{rel(path)}:{node.lineno} uses os.{node.attr}")
                if isinstance(node, ast.Import) or (isinstance(node, ast.ImportFrom) and node.level == 0):
                    modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for module in modules:
                        self.assertNotIn(module.split(".")[0], FORBIDDEN_MODULES, f"{rel(path)} imports {module}")

    def test_subprocess_is_used_only_through_the_checked_runner(self):
        source = (PACKAGE / "boswas_control_center" / "commands.py").read_text(encoding="utf-8")
        parsed = ast.parse(source)
        calls = [n for n in ast.walk(parsed) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
        self.assertEqual(sorted(c.func.attr for c in calls), ["Popen", "run"])
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
        for name in ("run", "start"):
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

    def test_kcm_modules_are_plain_plugin_names(self):
        for module in catalog.KCM_MODULES:
            self.assertRegex(module, r"^kcm[a-zA-Z0-9_]+$")

    def test_forbidden_text(self):
        for path in ALL:
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_TEXT:
                self.assertNotIn(token, text, f"{rel(path)} contains {token!r}")

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
        for name in PURE:
            for node in ast.walk(tree(PACKAGE / "boswas_control_center" / name)):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for module in modules:
                        if name == "app.py" and module.startswith("PySide6"):
                            continue                         # imported inside main(), after the root check
                        self.assertFalse(module.startswith("PySide"), f"{name} imports {module}")

    def test_app_checks_root_before_qt(self):
        source = (PACKAGE / "boswas_control_center" / "app.py").read_text(encoding="utf-8")
        self.assertLess(source.index("running_as_root()"), source.index("from PySide6"))
        self.assertIn("return EXIT_ROOT", source)
        self.assertIn("EXIT_ROOT = 4", source)


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

    def test_hard_coded_colours_are_only_status_tints(self):
        allowed = {"boswas_control_center/ui/common.py"}
        for path in CODE:
            colours = HEX_COLOUR.findall(path.read_text(encoding="utf-8"))
            if rel(path) not in allowed:
                self.assertEqual(colours, [], rel(path))
        common = (PACKAGE / "boswas_control_center" / "ui" / "common.py").read_text(encoding="utf-8")
        self.assertEqual(len(HEX_COLOUR.findall(common)), 6)          # five status tints and the glyph colour


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
