"""Static guarantees of boswas-android's code and data.

Least privilege: only runner.py starts processes, from a fixed allowlist,
never through a shell; pkexec only ever starts boswas-sysops with an android-*
operation; Waydroid is only asked for user-level things (status, session,
app) and is never initialised outside boswas-sysops; Google applications
(GAPPS) are never requested; nothing evaluates code; only fdroid.py talks to
the network; nothing autostarts. The shipped keyring is the one whose
fingerprint runtime.conf names. Files are LF-only without invisible characters.

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import ast
import hashlib
import re
import sys
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import android_fixtures  # noqa: E402,F401  (import path)

from boswas_android import config, googleplay, runner  # noqa: E402
from boswas_android.system import Root  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parents[1]
CODE = sorted((PACKAGE / "boswas_android").glob("*.py"))
SCRIPTS = [PACKAGE / "bin" / "boswas-android"]
TESTS = sorted((PACKAGE / "tests").glob("*.py"))
SHIPPED = CODE + SCRIPTS + [PACKAGE / "man" / "boswas-android.1", PACKAGE / "data" / "runtime.conf"] + \
    sorted(p for p in (PACKAGE / "debian").rglob("*") if p.is_file())
DOC = REPO / "docs" / "android" / "README.md"

EXPECTED_EXECUTABLES = {"waydroid": "/usr/bin/waydroid", "pkexec": "/usr/bin/pkexec",
                        "systemctl": "/usr/bin/systemctl", "ip": "/usr/bin/ip"}
CONTRACT_OPERATIONS = {"android-runtime-install", "android-runtime-remove", "android-init", "android-update",
                       "android-repair", "android-service"}
FORBIDDEN_NAMES = {"system", "popen", "Popen", "check_output", "getoutput", "getstatusoutput", "spawnl", "spawnv",
                   "execv", "execve", "execl", "execvp", "posix_spawn", "fork", "startfile"}
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__", "breakpoint"}
WAYDROID_USER_ACTIONS = {"status", "session", "app"}


def tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), str(path))


def imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def docstring_nodes(module: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(module):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


class ProcessTest(unittest.TestCase):
    def test_code_was_found(self):
        self.assertGreater(len(CODE), 10)

    def test_only_the_runner_imports_subprocess(self):
        users = [p.name for p in CODE + SCRIPTS if "subprocess" in imports(p)]
        self.assertEqual(users, ["runner.py"])

    def test_no_shell_no_evaluation(self):
        for path in CODE + SCRIPTS:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", text, path.name)
            self.assertNotIn("os.system", text, path.name)
            for node in ast.walk(tree(path)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTINS:
                    self.fail(f"{path.name}: call to {func.id}")
                if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_NAMES \
                        and not (path.name == "runner.py" and func.attr == "Popen"):
                    self.fail(f"{path.name}: call to {func.attr}")
                if isinstance(func, ast.Name) and func.id in FORBIDDEN_NAMES:
                    self.fail(f"{path.name}: call to {func.id}")

    def test_allowlist(self):
        self.assertEqual(runner.EXECUTABLES, EXPECTED_EXECUTABLES)
        self.assertEqual(runner.SYSOPS, "/usr/libexec/boswas/sysops")
        self.assertEqual(set(runner.SYSOPS_OPERATIONS), CONTRACT_OPERATIONS)

    def test_check_refuses_everything_else(self):
        bad = [["/bin/sh", "-c", "true"], ["waydroid", "status"], ["/usr/bin/pkexec", "/bin/sh"],
               ["/usr/bin/pkexec", runner.SYSOPS, "apt-install", "x"], ["/usr/bin/pkexec", runner.SYSOPS],
               ["/usr/bin/waydroid", "app", "install", "a\nb"], ["/usr/bin/waydroid", "x\0y"], [], "waydroid status"]
        for command in bad:
            with self.assertRaises(ValueError, msg=repr(command)):
                runner.check(command)
        runner.check(["/usr/bin/pkexec", runner.SYSOPS, "android-service", "start"])

    def test_waydroid_is_only_asked_for_user_level_actions(self):
        """Every Waydroid command line starts with a literal user-level action; the one generic
        forwarder (runtime._waydroid) is itself only called with literal user-level actions."""
        forwarders = 0

        def calls(node, function=None):
            for child in ast.iter_child_nodes(node):
                inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
                if isinstance(child, ast.Call):
                    yield child, function
                yield from calls(child, inner)

        for path in CODE:
            for node, function in calls(tree(path)):
                name = getattr(node.func, "attr", getattr(node.func, "id", None))
                args = node.args
                if name == "argv" and args and isinstance(args[0], ast.Constant) and args[0].value == "waydroid":
                    if len(args) > 1 and isinstance(args[1], ast.Starred):
                        self.assertEqual(function, "_waydroid", path.name)
                        forwarders += 1
                        continue
                    self.assertTrue(len(args) > 1 and isinstance(args[1], ast.Constant), path.name)
                    self.assertIn(args[1].value, WAYDROID_USER_ACTIONS, f"{path.name}: waydroid {args[1].value}")
                if name == "_waydroid":
                    self.assertTrue(len(args) > 1 and isinstance(args[1], ast.Constant), path.name)
                    self.assertIn(args[1].value, WAYDROID_USER_ACTIONS, f"{path.name}: waydroid {args[1].value}")
        self.assertEqual(forwarders, 1)

    def test_sysops_operations_used_are_in_the_contract(self):
        used = set()
        for path in CODE:
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("android-"):
                    used.add(node.value)
        self.assertTrue(used)
        self.assertLessEqual(used, CONTRACT_OPERATIONS)

    def test_google_apps_are_never_requested(self):
        for path in CODE + SCRIPTS:
            if path.name == "googleplay.py":
                continue
            module = tree(path)
            docs = docstring_nodes(module)
            for node in ast.walk(module):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                    self.assertNotIn("GAPPS", node.value.upper(), path.name)

    def test_only_fdroid_uses_the_network(self):
        for path in CODE:
            net = imports(path) & {"urllib", "http", "socket", "ssl", "requests"}
            if path.name == "fdroid.py":
                self.assertEqual(net, {"urllib", "ssl"})
            else:
                self.assertEqual(net, set(), path.name)

    def test_nothing_autostarts(self):
        rules = (PACKAGE / "debian" / "rules").read_text(encoding="utf-8")
        for word in ("autostart", "systemd", ".service", ".timer", "xdg/"):
            self.assertNotIn(word, rules)
        self.assertFalse(list(PACKAGE.rglob("*.service")))
        self.assertFalse(list(PACKAGE.rglob("*.desktop")))
        control = (PACKAGE / "debian" / "control").read_text(encoding="utf-8")
        self.assertNotIn("waydroid", re.search(r"^Depends:(.*?)^\S", control, re.S | re.M).group(1).lower())
        self.assertNotRegex(control, r"^(Recommends|Suggests):", "no relation may pull the runtime in")


class DataTest(unittest.TestCase):
    def test_runtime_conf_is_valid(self):
        conf = config.runtime_conf(_DataRoot())
        self.assertEqual(conf.problems(), [])
        self.assertEqual(conf.image_type, "VANILLA")
        self.assertEqual(conf.repo_url, "https://repo.waydro.id/")
        self.assertEqual(conf.repo_suite, "trixie")
        self.assertEqual(set(conf.allowed_packages), {"waydroid", "python3-gbinder", "libgbinder", "libglibutil"})

    def test_keyring_fingerprint_matches(self):
        data = (PACKAGE / "data" / "waydroid.gpg").read_bytes()
        self.assertEqual(openpgp_fingerprint(data), config.runtime_conf(_DataRoot()).key_fingerprint)

    def test_google_play_assessment(self):
        doc = googleplay.assessment()
        self.assertEqual(set(doc), {"available", "reason", "alternatives"})
        self.assertIs(doc["available"], False)
        self.assertEqual(doc["alternatives"], ["fdroid", "local-apk"])
        for label in googleplay.ALTERNATIVE_LABELS.values():
            self.assertTrue("Google Play" not in label or "not Google Play" in label)


class _DataRoot(Root):
    """Maps the shipped data files to their installed paths."""

    def __init__(self):
        super().__init__(str(PACKAGE))

    def __call__(self, path: str) -> str:
        if path == config.RUNTIME_CONF:
            return str(PACKAGE / "data" / "runtime.conf")
        return super().__call__(path)


def openpgp_fingerprint(data: bytes) -> str:
    """V4 fingerprint of the first public-key packet (RFC 4880 12.2)."""
    tag_byte = data[0]
    if not tag_byte & 0x80:
        raise ValueError("not an OpenPGP packet")
    if tag_byte & 0x40:                       # new format
        tag, first = tag_byte & 0x3F, data[1]
        if first < 192:
            length, start = first, 2
        elif first < 224:
            length, start = ((first - 192) << 8) + data[2] + 192, 3
        else:
            length, start = int.from_bytes(data[2:6], "big"), 6
    else:                                     # old format
        tag, kind = (tag_byte >> 2) & 0x0F, tag_byte & 0x03
        size = {0: 1, 1: 2, 2: 4}[kind]
        length, start = int.from_bytes(data[1:1 + size], "big"), 1 + size
    if tag != 6:
        raise ValueError("first packet is not a public key")
    body = data[start:start + length]
    if body[0] != 4:
        raise ValueError("not a version 4 key")
    return hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()


class SourceHygieneTest(unittest.TestCase):
    def test_lf_only_and_no_invisible_characters(self):
        for path in SHIPPED + TESTS + ([DOC] if DOC.exists() else []):
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw, path.name)
            text = raw.decode("utf-8")
            for ch in text:
                if ch in "\n\t":
                    continue
                self.assertNotIn(unicodedata.category(ch), ("Cc", "Cf"), f"{path.name}: U+{ord(ch):04X}")

    def test_man_page_lists_every_command(self):
        man = (PACKAGE / "man" / "boswas-android.1").read_text(encoding="utf-8").replace("\\-", "-")
        man = re.sub(r"\\f[BIRP]|\"", "", man)
        for command in ("status", "check", "apk inspect", "apk install", "app list", "app launch", "app remove",
                        "session start", "runtime install", "service start", "fdroid install", "google-play",
                        "logs", "cleanup"):
            self.assertIn(command, man, command)

    def test_scripts_use_the_private_module_directory(self):
        for script in SCRIPTS:
            text = script.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/usr/bin/python3 -IB\n"))
            self.assertIn('sys.path.insert(0, "/usr/lib/boswas/python")', text)

    def test_docs_exist(self):
        if not (REPO / "docs").is_dir():
            self.skipTest("not a full repository checkout (package build stages packages/ only)")
        self.assertTrue(DOC.exists(), "docs/android/README.md")


if __name__ == "__main__":
    unittest.main()
