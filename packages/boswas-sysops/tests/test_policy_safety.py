"""The polkit policy matches the operation table; static guarantees of the code and data.

Least privilege: only runner.py starts processes (subprocess) or signals
them (os.killpg), always from an allowlisted absolute path, never through a
shell; nothing evaluates code; the polkit defaults follow the contract
(read-only: active session; everything else: administrator; never remote or
inactive sessions). Sources are LF-only and contain no invisible characters.
"""

from __future__ import annotations

import ast
import sys
import unicodedata
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sysops_fixtures  # noqa: E402,F401  (import path)

from boswas_sysops import HELPER_PATH, POLKIT_PREFIX, conffile, policy, runner  # noqa: E402
from boswas_sysops.cli import OPERATIONS  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
CODE = sorted((PACKAGE / "boswas_sysops").glob("*.py"))
SCRIPT = PACKAGE / "bin" / "sysops"
ALL = CODE + [SCRIPT]
TESTS = sorted((PACKAGE / "tests").glob("*.py"))
DATA = [PACKAGE / "data" / "com.boswas.sysops.policy", PACKAGE / "data" / "apps.conf",
        PACKAGE / "man" / "boswas-sysops.8"]
EXCLUDE_PREF = PACKAGE.parents[1] / "config" / "live-build" / "config" / "archives" / "boswas-exclude.pref.chroot"
CONTRACT = PACKAGE.parents[1] / "docs" / "architecture" / "platform-contracts.md"

CONTRACT_OPERATIONS = {
    "apt-refresh": "auth_admin_keep", "apt-upgrade": "auth_admin_keep", "apt-install": "auth_admin_keep",
    "apt-remove": "auth_admin_keep", "apt-install-local": "auth_admin_keep", "apt-clean": "auth_admin_keep",
    "journal-vacuum": "auth_admin_keep", "firmware-report": "yes", "flatpak-remote-enable": "auth_admin_keep",
    "flatpak-remote-disable": "auth_admin_keep", "android-runtime-install": "auth_admin_keep",
    "android-runtime-remove": "auth_admin_keep", "android-init": "auth_admin_keep",
    "android-update": "auth_admin_keep", "android-repair": "auth_admin_keep", "android-service": "yes",
}
FORBIDDEN_MODULES = {"multiprocessing", "pty", "pexpect", "ctypes", "pickle", "shelve", "marshal", "importlib",
                     "runpy", "code", "codeop", "asyncio", "socket", "urllib", "http", "ftplib", "webbrowser"}
FORBIDDEN_OS = {"system", "popen", "fork", "forkpty", "execv", "execve", "execl", "execle", "execlp", "execlpe",
                "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp",
                "spawnvpe", "posix_spawn", "posix_spawnp", "kill", "killpg", "setuid", "seteuid", "setgid", "putenv",
                "chdir", "chroot"}
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__", "breakpoint"}
FORBIDDEN_TEXT = ("shell=True", "os.system", "os.popen", "--allow-unauthenticated", "--allow-insecure",
                  "--allow-downgrades", "--allow-remove-essential", "--allow-change-held-packages", "--force-yes",
                  "AllowUnauthenticated=true", "trusted=yes", "-s GAPPS", '"GAPPS"', "dist-upgrade", "full-upgrade",
                  "--force-all", "--force-depends", "--force-overwrite")


def tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def rel(path: Path) -> str:
    return str(path.relative_to(PACKAGE)).replace("\\", "/")


class PolkitPolicyTest(unittest.TestCase):
    def setUp(self):
        self.root = ET.parse(PACKAGE / "data" / "com.boswas.sysops.policy").getroot()
        self.actions = self.root.findall("action")

    def test_operations_match_the_contract(self):
        self.assertEqual(set(OPERATIONS), set(CONTRACT_OPERATIONS))
        if CONTRACT.exists():
            text = CONTRACT.read_text(encoding="utf-8")
            for name in OPERATIONS:
                self.assertIn(f"`{name}`", text)

    def test_one_action_per_operation(self):
        ids = [a.get("id") for a in self.actions]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(ids), sorted(POLKIT_PREFIX + name for name in OPERATIONS))
        self.assertEqual(self.root.findtext("vendor"), "Boswas Group")

    def test_annotations_and_defaults(self):
        argv1 = []
        for action in self.actions:
            name = action.get("id")[len(POLKIT_PREFIX):]
            annotations = {a.get("key"): a.text for a in action.findall("annotate")}
            self.assertEqual(annotations, {"org.freedesktop.policykit.exec.path": HELPER_PATH,
                                           "org.freedesktop.policykit.exec.argv1": name})
            argv1.append(name)
            defaults = action.find("defaults")
            self.assertEqual(defaults.findtext("allow_any"), "no", name)
            self.assertEqual(defaults.findtext("allow_inactive"), "no", name)
            self.assertEqual(defaults.findtext("allow_active"), CONTRACT_OPERATIONS[name], name)
            self.assertEqual(defaults.findtext("allow_active") == "yes", OPERATIONS[name].read_only, name)
            self.assertEqual(action.findtext("description"), OPERATIONS[name].description)
            self.assertEqual(action.findtext("message"), OPERATIONS[name].message)
        self.assertEqual(len(argv1), len(set(argv1)))


class PolicyDataTest(unittest.TestCase):
    def test_apps_conf_defaults(self):
        values = conffile.parse((PACKAGE / "data" / "apps.conf").read_text(encoding="utf-8"))
        self.assertEqual(values, {"LOCAL_DEB": "allow", "FLATPAK_REMOTES": "flathub", "ANDROID_RUNTIME": "allow"})
        self.assertEqual(values, policy.DEFAULTS)
        for remote in values["FLATPAK_REMOTES"].split():
            self.assertTrue((PACKAGE / "data" / "flatpak-remotes" / f"{remote}.flatpakrepo").is_file(), remote)

    def test_excluded_packages_match_the_image_build(self):
        if not EXCLUDE_PREF.exists():
            self.skipTest("the image configuration is not part of this source tree")
        names = set()
        for line in EXCLUDE_PREF.read_text(encoding="utf-8").splitlines():
            if line.startswith("Package:"):
                names.update(line.split(":", 1)[1].split())
        self.assertEqual(names, set(policy.EXCLUDED))

    def test_protected(self):
        for name in ("boswas-os", "boswas-sysops", "linux-image-6.12.111+deb13-amd64", "grub-efi-amd64-signed",
                     "shim-signed", "cryptsetup", "cryptsetup-initramfs", "systemd", "dpkg", "apt", "sudo:amd64"):
            self.assertTrue(policy.protected_by_name(name), name)
        for name in ("vim", "firefox-esr", "linux-headers-amd64", "libcryptsetup12", "aptitude"):
            self.assertFalse(policy.protected_by_name(name), name)


class SafetyTest(unittest.TestCase):
    def test_code_was_found(self):
        self.assertGreaterEqual(len(CODE), 15)

    def test_only_runner_imports_subprocess(self):
        users = []
        for path in ALL:
            for node in ast.walk(tree(path)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                if any(name.split(".")[0] == "subprocess" for name in names):
                    users.append(rel(path))
        self.assertEqual(users, ["boswas_sysops/runner.py"])

    def test_no_shell_no_evaluation_no_forbidden_calls(self):
        for path in ALL:
            in_runner = path.name == "runner.py"
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.keyword) and node.arg == "shell":
                    self.fail(f"{rel(path)}:{node.value.lineno} passes shell=")
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, FORBIDDEN_BUILTINS, f"{rel(path)}:{node.lineno}")
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                    allowed = {"killpg"} if in_runner else set()
                    self.assertNotIn(node.attr, FORBIDDEN_OS - allowed, f"{rel(path)}:{node.lineno} os.{node.attr}")
                if isinstance(node, ast.Import) or (isinstance(node, ast.ImportFrom) and node.level == 0):
                    modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for module in modules:
                        self.assertNotIn(module.split(".")[0], FORBIDDEN_MODULES, f"{rel(path)} imports {module}")

    def test_runner_checks_every_command(self):
        source = (PACKAGE / "boswas_sysops" / "runner.py").read_text(encoding="utf-8")
        parsed = ast.parse(source)
        calls = [n for n in ast.walk(parsed) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
        self.assertEqual(sorted(c.func.attr for c in calls), ["Popen", "run"])
        for call in calls:
            self.assertEqual(ast.unparse(call.args[0]), "command")
            keywords = {k.arg: ast.unparse(k.value) for k in call.keywords}
            self.assertNotIn("shell", keywords)
            self.assertEqual(keywords.get("stdin"), "subprocess.DEVNULL")
            self.assertEqual(keywords.get("env"), "dict(SAFE_ENV)")
        functions = {n.name: ast.get_source_segment(source, n) for n in ast.walk(parsed) if isinstance(n, ast.FunctionDef)}
        for name in ("run", "stream"):
            self.assertIn("_check(command)", functions[name])
        self.assertIn("command[0] not in ALLOWED_PATHS", functions["_check"])

    def test_programs_are_named_literally(self):
        for path in CODE:
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "argv":
                    first = node.args[0]
                    self.assertIsInstance(first, ast.Constant, f"{rel(path)}:{node.lineno}")
                    self.assertIn(first.value, runner.EXECUTABLES, f"{rel(path)}:{node.lineno}")

    def test_forbidden_text(self):
        for path in ALL + DATA:
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_TEXT:
                self.assertNotIn(token, text, f"{rel(path)} contains {token!r}")

    def test_entry_script_ignores_the_environment(self):
        first = SCRIPT.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first, "#!/usr/bin/python3 -IB")

    def test_no_invisible_characters_and_lf_only(self):
        for path in ALL + TESTS + DATA + [PACKAGE / "debian" / "control", PACKAGE / "debian" / "rules",
                                          PACKAGE / "debian" / "changelog"]:
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw, f"{rel(path)} has CR line endings")
            self.assertTrue(raw.endswith(b"\n"), f"{rel(path)} does not end with a newline")
            for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
                for char in line:
                    if char == "\t" and path.name == "rules":      # make recipes
                        continue
                    category = unicodedata.category(char)
                    self.assertFalse(category in ("Cc", "Cf", "Co", "Cs"), f"{rel(path)}:{number} has {char!r}")


if __name__ == "__main__":
    unittest.main()
