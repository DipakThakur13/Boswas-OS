"""The committed generated files match presets.json and the generator."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support

GENERATOR = support.PRESETS_DIR / "tools" / "build_presets.py"
GENERATED_DIRS = ("color-schemes", "konsole", "look-and-feel", "wallpapers", "previews")


class GeneratedFilesTest(unittest.TestCase):
    def test_generator_output_is_up_to_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, "-B", str(GENERATOR), "--output", tmp],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            fresh = {p.relative_to(tmp).as_posix(): p for p in Path(tmp).rglob("*") if p.is_file()}
            committed = {}
            for top in GENERATED_DIRS:
                for p in (support.PRESETS_DIR / top).rglob("*"):
                    if p.is_file():
                        committed[p.relative_to(support.PRESETS_DIR).as_posix()] = p
            self.assertEqual(sorted(fresh), sorted(committed),
                             "generated file set differs: run tools/build_presets.py")
            for rel, path in fresh.items():
                with self.subTest(file=rel):
                    self.assertEqual(path.read_bytes(), committed[rel].read_bytes(),
                                     f"{rel} is out of date: run tools/build_presets.py")

    def test_check_mode(self):
        result = subprocess.run([sys.executable, "-B", str(GENERATOR), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_one_set_of_files_per_preset(self):
        for p in support.presets():
            label = p["label"]
            for rel in (f"color-schemes/Boswas{label}.colors",
                        f"konsole/Boswas{label}.colorscheme",
                        f"konsole/Boswas {label}.profile",
                        f"look-and-feel/com.boswas.{p['id']}/metadata.json",
                        f"look-and-feel/com.boswas.{p['id']}/contents/defaults",
                        f"look-and-feel/com.boswas.{p['id']}/contents/layouts/org.kde.plasma.desktop-layout.js",
                        f"wallpapers/Boswas-{label}/metadata.json",
                        f"wallpapers/Boswas-{label}/light.svg",
                        f"wallpapers/Boswas-{label}/dark.svg",
                        f"wallpapers/Boswas-{label}/lock.svg",
                        f"previews/{p['id']}.svg"):
                with self.subTest(file=rel):
                    self.assertTrue((support.PRESETS_DIR / rel).is_file())

    def test_text_files_are_lf_utf8_without_control_characters(self):
        allowed = {"\n", "\t"}
        for path in sorted(support.PRESETS_DIR.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".png", ".jpg"):
                continue
            data = path.read_bytes()
            with self.subTest(path=str(path.relative_to(support.PRESETS_DIR))):
                self.assertNotIn(b"\r", data)
                text = data.decode("utf-8")
                self.assertTrue(text.endswith("\n"))
                bad = [ch for ch in text if (ord(ch) < 32 and ch not in allowed) or 0x7F <= ord(ch) < 0xA0
                       or ord(ch) in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)]
                self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
