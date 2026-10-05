"""install.sh and the PNG repacker.

The full install renders about 40 images at 3840x2160 (about a minute); set
BOSWAS_PRESETS_FULL_INSTALL=1 to run it.
"""

import os
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

import support

INSTALL = support.PRESETS_DIR / "install.sh"


def tiny_png(width=4, height=3) -> bytes:
    """A small RGB PNG with an ancillary chunk, written at zlib level 1."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row += bytes([x * 50 % 256, y * 70 % 256, 120])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"tEXt", b"Software\x00test")
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


class InstallScriptTest(unittest.TestCase):
    def test_posix_syntax(self):
        result = subprocess.run(["sh", "-n", str(INSTALL)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_strict_mode_and_pipeline(self):
        text = INSTALL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("\nset -eu\n", text)
        self.assertIn('inline="$repo/build/scripts/inline-svg.py"', text)
        self.assertIn("rsvg-convert", text)
        self.assertIn("install -D -m 0644", text)
        self.assertIn('install -D -m 0755 "$here/bin/boswas-preset" "$dest/usr/bin/boswas-preset"', text)
        self.assertIn("build_presets.py\" --check", text)
        self.assertNotIn("com.boswas.desktop", text)
        self.assertNotIn("BoswasDark", text)

    def test_requires_destdir(self):
        result = subprocess.run(["sh", str(INSTALL)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)

    @unittest.skipUnless(os.environ.get("BOSWAS_PRESETS_FULL_INSTALL") == "1" and shutil.which("rsvg-convert"),
                         "set BOSWAS_PRESETS_FULL_INSTALL=1 (needs rsvg-convert)")
    def test_full_install(self):
        with tempfile.TemporaryDirectory() as dest:
            result = subprocess.run(["sh", str(INSTALL), dest], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            root = Path(dest)
            share = root / "usr/share"
            for p in support.presets():
                label, pid = p["label"], p["id"]
                for rel in (f"color-schemes/Boswas{label}.colors",
                            f"konsole/Boswas{label}.colorscheme", f"konsole/Boswas {label}.profile",
                            f"plasma/look-and-feel/com.boswas.{pid}/metadata.json",
                            f"plasma/look-and-feel/com.boswas.{pid}/contents/defaults",
                            f"plasma/look-and-feel/com.boswas.{pid}/contents/layouts/org.kde.plasma.desktop-layout.js",
                            f"plasma/look-and-feel/com.boswas.{pid}/contents/previews/preview.png",
                            f"wallpapers/Boswas-{label}/metadata.json",
                            f"wallpapers/Boswas-{label}/contents/images/3840x2160.png",
                            f"wallpapers/Boswas-{label}/contents/images_dark/3840x2160.png",
                            f"wallpapers/Boswas-{label}/contents/screenshot.png",
                            f"boswas/presets/lock/{pid}.png", f"boswas/presets/previews/{pid}.png"):
                    with self.subTest(file=rel):
                        self.assertTrue((share / rel).is_file())
            for rel in ("usr/share/wallpapers/Boswas/contents/images_dark/3840x2160.png",
                        "usr/share/boswas/branding/login-background.png",
                        "usr/share/boswas/presets/presets.json",
                        "usr/lib/boswas/python/boswas_preset/cli.py"):
                self.assertTrue((root / rel).is_file(), rel)
            tool = root / "usr/bin/boswas-preset"
            self.assertEqual(stat.S_IMODE(tool.stat().st_mode), 0o755)
            for path in root.rglob("*"):
                if path.is_file() and path != tool:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644, path)
            total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            self.assertLess(total, 40 * 1024 * 1024)


class RepackPngTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repack = support.load_module("repack_png", support.PRESETS_DIR / "tools" / "repack_png.py")

    def test_same_pixels_fewer_chunks(self):
        data = tiny_png()
        out = self.repack.repack(data)
        kinds = [kind for kind, _ in self.repack.chunks(out)]
        self.assertEqual(kinds, [b"IHDR", b"IDAT", b"IEND"])

        def pixels(png):
            return zlib.decompress(b"".join(body for kind, body in self.repack.chunks(png) if kind == b"IDAT"))

        self.assertEqual(pixels(out), pixels(data))
        pos = 8
        while pos < len(out):
            length, kind = struct.unpack(">I4s", out[pos:pos + 8])
            body = out[pos + 8:pos + 8 + length]
            (crc,) = struct.unpack(">I", out[pos + 8 + length:pos + 12 + length])
            self.assertEqual(crc, zlib.crc32(kind + body) & 0xFFFFFFFF)
            pos += 12 + length

    def test_rejects_non_png(self):
        with self.assertRaises(ValueError):
            self.repack.repack(b"GIF89a")


if __name__ == "__main__":
    unittest.main()
