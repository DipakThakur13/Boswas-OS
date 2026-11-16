"""The committed sources match the generator, and the generator's rules hold."""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import support

SVG = "{http://www.w3.org/2000/svg}"
ELEMENTS = {"svg", "title", "defs", "filter", "feGaussianBlur", "g", "path"}
HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")


def run_generator(*args):
    return subprocess.run([sys.executable, "-B", str(support.GENERATOR), *args], capture_output=True, text=True)


def channels(colour: str):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


class GeneratedFilesTest(unittest.TestCase):
    def test_generator_output_is_up_to_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_generator("--output", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            root = Path(tmp) / support.THEME
            fresh = {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}
            committed = {p.relative_to(support.SOURCE_DIR).as_posix(): p
                         for p in support.SOURCE_DIR.rglob("*") if p.is_file()}
            self.assertEqual(sorted(fresh), sorted(committed),
                             "generated file set differs: run tools/build_cursors.py")
            for rel, path in fresh.items():
                with self.subTest(file=rel):
                    self.assertEqual(path.read_bytes(), committed[rel].read_bytes(),
                                     f"{rel} is out of date: run tools/build_cursors.py")

    def test_check_mode_accepts_the_committed_files(self):
        result = run_generator("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_check_mode_reports_differences(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_generator("--output", tmp).returncode, 0)
            self.assertEqual(run_generator("--check", "--output", tmp).returncode, 0)
            src = Path(tmp) / support.THEME / "src"

            edited = src / "default.svg"
            original = edited.read_text(encoding="utf-8")
            edited.write_text(original.replace("</svg>", "<!-- edited --></svg>"), encoding="utf-8")
            result = run_generator("--check", "--output", tmp)
            self.assertEqual(result.returncode, 1)
            self.assertIn("out of date: src/default.svg", result.stderr)
            edited.write_text(original, encoding="utf-8")

            (src / "text.svg").unlink()
            result = run_generator("--check", "--output", tmp)
            self.assertEqual(result.returncode, 1)
            self.assertIn("missing: src/text.svg", result.stderr)
            self.assertEqual(run_generator("--output", tmp).returncode, 0)

            shutil.copy(src / "default.svg", src / "stale.svg")
            result = run_generator("--check", "--output", tmp)
            self.assertEqual(result.returncode, 1)
            self.assertIn("not generated: src/stale.svg", result.stderr)

            manifest = Path(tmp) / support.THEME / "cursors.json"
            (src / "stale.svg").unlink()
            manifest.write_text(manifest.read_text(encoding="utf-8").replace('"left_ptr"', '"left_pointer"'),
                                encoding="utf-8")
            result = run_generator("--check", "--output", tmp)
            self.assertEqual(result.returncode, 1)
            self.assertIn("out of date: cursors.json", result.stderr)

    def test_regenerating_removes_stale_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_generator("--output", tmp).returncode, 0)
            stale = Path(tmp) / support.THEME / "src" / "wait-99.svg"
            stale.write_text("<svg/>\n", encoding="utf-8")
            self.assertEqual(run_generator("--output", tmp).returncode, 0)
            self.assertFalse(stale.exists())
            self.assertEqual(run_generator("--check", "--output", tmp).returncode, 0)

    def test_text_files_are_lf_utf8_without_control_characters(self):
        allowed = {"\n", "\t"}
        for path in sorted(support.CURSORS_DIR.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            data = path.read_bytes()
            with self.subTest(path=str(path.relative_to(support.CURSORS_DIR))):
                self.assertNotIn(b"\r", data)
                text = data.decode("utf-8")
                self.assertTrue(text.endswith("\n"))
                self.assertFalse(text.endswith("\n\n"))
                bad = [ch for ch in text if (ord(ch) < 32 and ch not in allowed) or 0x7F <= ord(ch) < 0xA0
                       or ord(ch) in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)]
                self.assertEqual(bad, [])
                self.assertEqual([line for line in text.split("\n") if line != line.rstrip()], [])

    def test_only_sources_are_committed(self):
        """No rendered image or cursor file lives in the source tree."""
        kinds = {p.suffix for p in support.CURSORS_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        self.assertLessEqual(kinds, {".py", ".sh", ".md", ".svg", ".json", ".theme"})
        self.assertEqual([p for p in support.CURSORS_DIR.rglob("*") if p.is_symlink()], [])


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = support.manifest()
        cls.shapes = support.shapes()
        cls.targets = support.targets()

    def test_header(self):
        self.assertEqual(self.manifest["theme"], support.THEME)
        self.assertEqual(self.manifest["sizes"], list(support.SIZES))
        self.assertEqual((self.manifest["canvas"], self.manifest["nominal"]), (32, 24))

    def test_every_css_name_is_a_shape_or_a_link(self):
        self.assertEqual([name for name in support.CSS_NAMES if name not in self.targets], [])

    def test_every_legacy_name_is_a_shape_or_a_link(self):
        self.assertEqual([name for name in support.LEGACY_NAMES if name not in self.targets], [])

    def test_names_are_unique_plain_file_names(self):
        names = []
        for cursor in self.manifest["cursors"]:
            names.append(cursor["name"])
            names.extend(cursor["aliases"])
            self.assertEqual(cursor["aliases"], sorted(cursor["aliases"]))
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            with self.subTest(name=name):
                self.assertRegex(name, r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

    def test_hash_names(self):
        """Old X11 clients ask for a cursor by the hash of its bitmap: 32 hexadecimal digits."""
        hashes = {name: shape for name, shape in self.targets.items() if re.fullmatch(r"[0-9a-f]{32}", name)}
        self.assertGreaterEqual(len(hashes), 20)
        self.assertEqual(hashes["00008160000006810000408080010102"], "ns-resize")
        self.assertEqual(hashes["028006030e0e7ebffc7f7070c0600140"], "ew-resize")
        self.assertEqual(hashes["e29285e634086352946a0e7090d73106"], "pointer")
        self.assertEqual(hashes["03b6e0fcb3499374a867c041f52298f0"], "not-allowed")
        # Nothing that only looks like a hash name: all other names are words.
        for name in self.targets:
            if name not in hashes:
                self.assertIsNone(re.fullmatch(r"[0-9a-f]{20,}", name), name)

    def test_qt_finds_the_right_shape(self):
        for names, shape in support.QT_NAMES:
            with self.subTest(first=names[0]):
                self.assertEqual({name: self.targets.get(name) for name in names},
                                 {name: shape for name in names})

    def test_directions_share_the_double_arrows(self):
        pairs = {"e-resize": "ew-resize", "w-resize": "ew-resize", "n-resize": "ns-resize",
                 "s-resize": "ns-resize", "ne-resize": "nesw-resize", "sw-resize": "nesw-resize",
                 "nw-resize": "nwse-resize", "se-resize": "nwse-resize",
                 "top_right_corner": "nesw-resize", "bottom_left_corner": "nesw-resize",
                 "top_left_corner": "nwse-resize", "bottom_right_corner": "nwse-resize",
                 "size_bdiag": "nesw-resize", "size_fdiag": "nwse-resize"}
        self.assertEqual({name: self.targets[name] for name in pairs}, pairs)

    def test_frames(self):
        listed = set()
        for name, cursor in self.shapes.items():
            frames = cursor["frames"]
            with self.subTest(shape=name):
                for frame in frames:
                    self.assertTrue((support.SOURCE_DIR / "src" / frame["svg"]).is_file(), frame["svg"])
                    listed.add(frame["svg"])
                if name in ("wait", "progress"):
                    self.assertTrue(16 <= len(frames) <= 24, len(frames))
                    self.assertTrue(all(30 <= frame["delay"] <= 50 for frame in frames))
                    self.assertEqual([frame["svg"] for frame in frames],
                                     [f"{name}-{i:02d}.svg" for i in range(1, len(frames) + 1)])
                    texts = {(support.SOURCE_DIR / "src" / frame["svg"]).read_text(encoding="utf-8")
                             .replace(frame["svg"][:-4], name) for frame in frames}
                    self.assertEqual(len(texts), len(frames), "two frames show the same picture")
                else:
                    self.assertEqual(frames, [{"svg": f"{name}.svg", "delay": 0}])
        on_disk = {p.name for p in (support.SOURCE_DIR / "src").iterdir()}
        self.assertEqual(on_disk, listed)

    def test_hotspots(self):
        for name, cursor in self.shapes.items():
            with self.subTest(shape=name):
                x, y = cursor["hotspot"]
                self.assertTrue(0 <= x < 32 and 0 <= y < 32)
        arrow = self.shapes["default"]["hotspot"]
        self.assertEqual(arrow, [2.5, 2.5])
        for name in ("context-menu", "help", "progress", "alias", "copy", "no-drop"):
            self.assertEqual(self.shapes[name]["hotspot"], arrow, name)
        for name in ("text", "vertical-text", "crosshair", "cell"):
            self.assertEqual(self.shapes[name]["hotspot"], [15.5, 15.5], name)
        for name in ("wait", "not-allowed", "move", "all-scroll", "ew-resize", "ns-resize",
                     "nesw-resize", "nwse-resize", "col-resize", "row-resize"):
            self.assertEqual(self.shapes[name]["hotspot"], [16.0, 16.0], name)

    def test_index_theme(self):
        text = (support.SOURCE_DIR / "index.theme").read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertEqual(lines[0], "[Icon Theme]")
        self.assertIn("Name=Boswas Cursors", lines)
        comment = [line for line in lines if line.startswith("Comment=")]
        self.assertEqual(len(comment), 1)
        self.assertGreater(len(comment[0]), len("Comment=") + 10)
        # Nothing on screen comes from another theme.
        self.assertNotIn("Inherits", text)


class DrawingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.white = support.token_colour("white")
        cls.outline = support.token_colour("neutral.950")
        cls.accent = support.token_colour("semantic.light.accent")
        cls.files = sorted((support.SOURCE_DIR / "src").glob("*.svg"))

    def test_colours_come_from_the_tokens(self):
        self.assertEqual(self.white, "#FFFFFF")
        self.assertLess(max(channels(self.outline)), 0x30, "the outline is a dark colour")
        generator = support.generator()
        self.assertEqual(generator.colour(generator.FILL_TOKEN), self.white)
        self.assertEqual(generator.colour(generator.OUTLINE_TOKEN), self.outline)
        self.assertEqual(generator.colour(generator.ACCENT_TOKEN), self.accent)

    def test_static_shapes_are_white_with_a_dark_outline(self):
        for path in self.files:
            if re.search(r"-\d\d\.svg$", path.name):
                continue
            with self.subTest(file=path.name):
                colours = {c.upper() for c in HEX.findall(path.read_text(encoding="utf-8"))}
                self.assertEqual(colours, {self.white, self.outline, "#000000"})

    def test_the_busy_ring_uses_the_accent(self):
        wr, wg, wb = channels(self.white)
        ar, ag, ab = channels(self.accent)
        for path in self.files:
            if not re.search(r"-\d\d\.svg$", path.name):
                continue
            with self.subTest(file=path.name):
                colours = {c.upper() for c in HEX.findall(path.read_text(encoding="utf-8"))}
                self.assertIn(self.accent, colours)
                for colour in colours - {self.white, self.outline, "#000000"}:
                    # A step of the fade: between white and the accent, channel by channel.
                    r, g, b = channels(colour)
                    self.assertTrue(ar <= r <= wr and ag <= g <= wg and ab <= b <= wb, colour)

    def test_svg_structure(self):
        for path in self.files:
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!-- Generated by '))
                root = ET.fromstring(text)
                self.assertEqual(root.tag, SVG + "svg")
                self.assertEqual((root.get("width"), root.get("height"), root.get("viewBox")),
                                 ("32", "32", "0 0 32 32"))
                self.assertEqual({element.tag.replace(SVG, "") for element in root.iter()} - ELEMENTS, set())
                self.assertEqual(root.find(SVG + "title").text, path.stem)
                ids = [element.get("id") for element in root.iter() if element.get("id")]
                self.assertEqual(ids, [f"{path.stem}-shadow"])
                self.assertIn(f'filter="url(#{path.stem}-shadow)"', text)
                for element in root.iter(SVG + "path"):
                    self.assertRegex(element.get("d"), r"^M[-0-9. MLAQZ]+$")
                self.assertNotIn("<text", text)
                self.assertNotIn("href", text)
                self.assertNotIn("<image", text)

    def test_every_drawing_stays_inside_the_grid(self):
        """Coordinates leave room for the outline and the shadow: nothing is cut off at the edge."""
        number = re.compile(r"-?\d+(?:\.\d+)?")
        for path in self.files:
            with self.subTest(file=path.name):
                root = ET.fromstring(path.read_text(encoding="utf-8"))
                for element in root.iter(SVG + "path"):
                    # Drop the flags and radii of arcs, keep the end points.
                    d = re.sub(r"A[0-9.]+ [0-9.]+ 0 [01] [01] ", "L", element.get("d"))
                    values = [float(v) for v in number.findall(d)]
                    self.assertTrue(all(1.5 <= v <= 29.0 for v in values), (min(values), max(values)))

    def test_composite_shapes_reuse_the_arrow(self):
        def first_white_path(name):
            root = ET.fromstring((support.SOURCE_DIR / "src" / f"{name}.svg").read_text(encoding="utf-8"))
            group = next(g for g in root.iter(SVG + "g") if g.get("fill") == self.white)
            return group.find(SVG + "path").get("d")

        arrow = first_white_path("default")
        for name in ("context-menu", "help", "alias", "copy", "no-drop", "progress-01", "progress-13"):
            self.assertEqual(first_white_path(name), arrow, name)

    def test_axis_aligned_edges_sit_on_the_pixel_grid(self):
        """The thin shapes are built from boxes on whole units: sharp at 24, 48 and 96."""
        for name in ("text", "vertical-text", "crosshair", "cell"):
            with self.subTest(shape=name):
                root = ET.fromstring((support.SOURCE_DIR / "src" / f"{name}.svg").read_text(encoding="utf-8"))
                for element in root.iter(SVG + "path"):
                    values = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", element.get("d"))]
                    self.assertEqual([v for v in values if v != int(v)], [])


class HelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = support.generator()

    def test_numbers(self):
        n = self.generator.n
        self.assertEqual([n(3.0), n(2.5), n(1 / 3), n(-0.0001), n(10.1299)], ["3", "2.5", "0.333", "0", "10.13"])

    def test_mix(self):
        mix = self.generator.mix
        self.assertEqual(mix("#FFFFFF", "#000000", 0.0), "#FFFFFF")
        self.assertEqual(mix("#FFFFFF", "#000000", 1.0), "#000000")
        self.assertEqual(mix("#FFFFFF", "#6443E6", 0.5), "#B2A1F2")

    def test_polygon_rounds_corners_inwards(self):
        square = self.generator.polygon([(0, 0), (10, 0), (10, 10), (0, 10)], 2.0)
        self.assertEqual(square, "M0 2A2 2 0 0 1 2 0L8 0A2 2 0 0 1 10 2L10 8A2 2 0 0 1 8 10L2 10A2 2 0 0 1 0 8Z")
        self.assertEqual(self.generator.polygon([(0, 0), (4, 0), (4, 4)]), "M0 0L4 0L4 4Z")

    def test_table_lists_every_shape(self):
        table = self.generator.table()
        for name in support.shapes():
            self.assertIn(f"| `{name}` |", table)

    def test_readme_matches_the_theme(self):
        readme = (support.CURSORS_DIR / "README.md").read_text(encoding="utf-8")
        shapes, targets = support.shapes(), support.targets()
        drawn = sum(1 for name in support.CSS_NAMES if name in shapes)
        hashes = [name for name in targets if re.fullmatch(r"[0-9a-f]{32}", name)]
        frames = shapes["wait"]["frames"]
        expected = {
            "the table of build_cursors.py --table": self.generator.table() + "\n",
            "the number of shapes": f"{len(shapes)} shapes are drawn",
            "the number of names": (f"{len(targets)} names in all: {len(shapes)} files and "
                                    f"{len(targets) - len(shapes)} links"),
            "the CSS names": (f"All {len(support.CSS_NAMES)} names of the CSS `cursor` property are there, "
                              f"{drawn} as drawings and {len(support.CSS_NAMES) - drawn}\nas links"),
            "the number of hash names": f"{len(hashes)} such names are links here",
            "the animation": f"{len(frames)} frames of\n  {frames[0]['delay']} ms",
        }
        for what, text in expected.items():
            with self.subTest(what=what):
                self.assertTrue(text in readme, f"README.md does not state {what}: {text!r}")
        # Every hash name the README calls unchecked is in the theme.
        unchecked = readme[readme.index("12 are written from memory"):readme.index("## Tests")]
        listed = re.findall(r"`([0-9a-f]{32})`", unchecked)
        self.assertEqual(len(listed), 12)
        self.assertEqual([name for name in listed if name not in targets], [])
        self.assertEqual(len(hashes) - len(listed), 20)

    def test_unknown_token_is_an_error(self):
        with self.assertRaises((SystemExit, KeyError)):
            self.generator.colour("nonsense.1")


if __name__ == "__main__":
    unittest.main()
