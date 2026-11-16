"""Rules every module of Files keeps: toolkit colours only, no shell, the engine without Qt."""

import ast
import os
import re
import unittest

import support  # noqa: F401

PACKAGE = os.path.join(str(support.ROOT), "boswas_apps", "files")
ENGINE = ("text", "listing", "places", "trash", "fileops", "archives", "clipformats", "desktopapps", "volumes",
          "thumbs", "search", "settings")


def sources():
    for name in sorted(os.listdir(PACKAGE)):
        if name.endswith(".py"):
            with open(os.path.join(PACKAGE, name), encoding="utf-8") as handle:
                yield name, handle.read()


class StaticTest(unittest.TestCase):
    def test_no_colours_of_our_own(self):
        colour = re.compile(r"""["']#[0-9A-Fa-f]{3,8}["']|QColor\(\s*\d|rgba?\(|setStyleSheet|Qt\.GlobalColor\.(?!transparent)""")
        for name, text in sources():
            self.assertIsNone(colour.search(text), f"{name}: a colour or style sheet of its own")

    def test_nothing_goes_through_a_shell(self):
        banned = re.compile(r"os\.system|subprocess|shell\s*=\s*True|os\.popen|\beval\(|\bexec\(|pickle|marshal")
        for name, text in sources():
            self.assertIsNone(banned.search(text), f"{name}: {banned.pattern}")

    def test_engine_modules_do_not_import_qt(self):
        for module in ENGINE:
            with open(os.path.join(PACKAGE, module + ".py"), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    self.assertFalse(name.startswith(("PySide6", "boswas_fluent")), f"{module} imports {name}")

    def test_packaging_files(self):
        root = str(support.ROOT)
        with open(os.path.join(root, "data", "files", "com.boswas.Files.desktop"), encoding="utf-8") as handle:
            entry = handle.read()
        for line in ("Name=Files", "Icon=boswas-files", "MimeType=inode/directory;", "Exec=boswas-files %U",
                     "StartupWMClass=com.boswas.Files", "[Desktop Action new-window]", "Name=New Window",
                     "Exec=boswas-files --new-window"):
            self.assertIn(line + "\n", entry)
        from boswas_apps.files import desktopapps
        app = desktopapps.load_app(os.path.join(root, "data", "files", "com.boswas.Files.desktop"), "com.boswas.Files.desktop", [])
        self.assertEqual(desktopapps.expand_exec(app, ["/a b"]), [["boswas-files", "file:///a%20b"]])
        self.assertEqual(app.actions, {"new-window": ("New Window", "boswas-files --new-window")})
        with open(os.path.join(root, "bin", "boswas-files"), encoding="utf-8") as handle:
            launcher = handle.read()
        self.assertTrue(launcher.startswith("#!/usr/bin/python3 -IB\n"))
        self.assertIn("from boswas_apps.files.__main__ import main", launcher)
        with open(os.path.join(root, "man", "boswas-files.1"), encoding="utf-8") as handle:
            manual = handle.read()
        for word in (".SH NAME", ".SH SYNOPSIS", ".SH OPTIONS", ".SH FILES", ".SH SEE ALSO", "\\-\\-select",
                     "\\-\\-new\\-window"):
            self.assertIn(word, manual)
        icon = os.path.join(root, "..", "..", "desktop", "icons", "Boswas-Prism", "scalable", "apps", "boswas-files.svg")
        self.assertTrue(os.path.isfile(icon))


if __name__ == "__main__":
    unittest.main()
