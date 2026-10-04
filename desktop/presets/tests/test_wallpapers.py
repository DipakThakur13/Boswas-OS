"""Wallpaper packages, lock screens and previews (SVG sources)."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import support

SVG = "{http://www.w3.org/2000/svg}"


def wallpaper_dir(p) -> Path:
    return support.PRESETS_DIR / "wallpapers" / f"Boswas-{p['label']}"


def sources():
    for p in support.presets():
        for name in ("light.svg", "dark.svg", "lock.svg"):
            yield p, wallpaper_dir(p) / name


class WallpaperPackageTest(unittest.TestCase):
    def test_metadata(self):
        for p in support.presets():
            with self.subTest(preset=p["id"]):
                data = json.loads((wallpaper_dir(p) / "metadata.json").read_text(encoding="utf-8"))
                self.assertEqual(data, {"KPlugin": {
                    "Authors": [{"Name": "Boswas Group"}],
                    "Id": f"Boswas-{p['label']}",
                    "License": "LicenseRef-Boswas-Internal",
                    "Name": p["name"],
                }})

    def test_default_wallpaper_name(self):
        data = json.loads((support.PRESETS_DIR / "wallpapers" / "Boswas" / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(data["KPlugin"]["Id"], "Boswas")
        self.assertEqual(sorted(p.name for p in (support.PRESETS_DIR / "wallpapers" / "Boswas").iterdir()),
                         ["metadata.json"])

    def test_svg_sources(self):
        for p, path in sources():
            with self.subTest(file=str(path.relative_to(support.PRESETS_DIR))):
                root = ET.parse(path).getroot()
                self.assertEqual(root.tag, SVG + "svg")
                self.assertEqual((root.get("width"), root.get("height"), root.get("viewBox")),
                                 ("3840", "2160", "0 0 3840 2160"))
                ids = [e.get("id") for e in root.iter() if e.get("id")]
                self.assertEqual(len(ids), len(set(ids)), "duplicate ids")
                for ref in re.findall(r"url\(#([^)]+)\)", path.read_text(encoding="utf-8")):
                    self.assertIn(ref, ids)
                images = list(root.iter(SVG + "image"))
                self.assertEqual(len(images), 1, "the mark, once")
                href = images[0].get("href")
                self.assertRegex(href, r"^\.\./\.\./\.\./branding/boswas-os-mark(-mono)?\.svg$")
                self.assertTrue((path.parent / href).resolve().is_file())
                if images[0].get("data-fill"):
                    self.assertRegex(images[0].get("data-fill"), support.HEX)
                self.assertLessEqual(float(images[0].get("opacity")), 0.35, "the mark stays subtle")
                self.assertLessEqual(float(images[0].get("height")), 600)

    def test_brand_geometry(self):
        """Every wallpaper carries the orbit (rings), the dot and the blade."""
        for p, path in sources():
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=str(path.relative_to(support.PRESETS_DIR))):
                self.assertGreaterEqual(len(re.findall(r'<circle cx="[^"]+" cy="[^"]+" r="[^"]+" stroke-opacity', text)), 3)
                self.assertRegex(text, r'<circle [^>]*r="1[0-9](\.\d+)?" fill="#')  # the dot
                if path.name != "lock.svg":
                    self.assertRegex(text, r'd="M[\d.]+ [\d.]+A[\d.]+ [\d.]+ 0 0 1 ')  # the blade

    def test_previews(self):
        for p in support.presets():
            path = support.PRESETS_DIR / "previews" / f"{p['id']}.svg"
            with self.subTest(preset=p["id"]):
                root = ET.parse(path).getroot()
                self.assertEqual((root.get("width"), root.get("height")), ("1920", "1080"))
                for image in root.iter(SVG + "image"):
                    self.assertTrue((path.parent / image.get("href")).resolve().is_file(), image.get("href"))
                variant = "dark" if p["variant"] == "dark" else "light"
                self.assertIn(f"../wallpapers/Boswas-{p['label']}/{variant}.svg", path.read_text(encoding="utf-8"))


class InlineAndRenderTest(unittest.TestCase):
    """The sources work with the build pipeline: inline-svg.py, then rsvg-convert."""

    @classmethod
    def setUpClass(cls):
        cls.inline = support.load_module("inline_svg", support.INLINE_SVG)

    def test_inlined_sources_are_self_contained(self):
        files = [path for _, path in sources()] + sorted((support.PRESETS_DIR / "previews").glob("*.svg"))
        for path in files:
            with self.subTest(file=str(path.relative_to(support.PRESETS_DIR))):
                root = self.inline.inline(path)
                hrefs = [e.get("href") for e in root.iter(SVG + "image")]
                self.assertEqual(hrefs, [])

    @unittest.skipUnless(shutil.which("rsvg-convert"), "rsvg-convert not installed")
    def test_render_small(self):
        with tempfile.TemporaryDirectory() as tmp:
            for p in support.presets():
                for name in ("dark.svg", "light.svg", "lock.svg"):
                    with self.subTest(preset=p["id"], file=name):
                        flat = Path(tmp) / "flat.svg"
                        out = Path(tmp) / "out.png"
                        root = self.inline.inline(wallpaper_dir(p) / name)
                        ET.ElementTree(root).write(flat, encoding="unicode")
                        result = subprocess.run(["rsvg-convert", "--width", "96", "--height", "54",
                                                 str(flat), "-o", str(out)], capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertTrue(out.read_bytes().startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
