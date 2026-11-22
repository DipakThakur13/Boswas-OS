"""Tests of the Boswas OS brand assets and their generators (desktop/branding).

  * tools/build_mark.py: the committed files are current, the output is
    deterministic, boswas-os-mark.svg keeps its format contract (the icon
    generator reads it), the mark keeps its margins, the variants share its
    shapes, and no colour comes from outside the token palette;
  * tools/build_wordmarks.py: Inter SemiBold outlines, white, cropped to the
    ink (the freshness check runs when fontTools and fonts-inter are there);
  * the composed artwork: every reference resolves, inlining makes it
    self-contained, every colour is a token colour, and the boot menu keeps
    the panel that the GRUB theme draws its menu in.

Run: python3 -B -m unittest discover -s tests   (from desktop/branding)
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

BRANDING = Path(__file__).resolve().parents[1]
REPO = BRANDING.parents[1]
SVG = "{http://www.w3.org/2000/svg}"
HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")
MARKS = ("boswas-os-mark.svg", "boswas-os-mark-mono.svg", "boswas-os-mark-flat.svg")
FAVICON = REPO / "control-plane" / "dashboard" / "favicon.svg"
COMPOSED = (BRANDING / "boswas-mark.svg", BRANDING / "boswas-about-logo.svg", BRANDING / "login-logo.svg",
            BRANDING / "boot-splash.svg", REPO / "installer" / "branding" / "installer-banner.svg")
THEME = REPO / "config" / "live-build" / "config" / "bootloaders" / "grub-pc" / "live-theme" / "theme.txt"
INLINE = REPO / "build" / "scripts" / "inline-svg.py"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, BRANDING / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_mark = load("build_mark")


def path_points(d: str) -> list[tuple[float, float]]:
    """Points along path data of absolute M, L, C, Z (cubic segments sampled)."""
    points: list[tuple[float, float]] = []
    tokens = re.findall(r"[MLCZ]|-?[0-9]*\.?[0-9]+", d)
    i, current = 0, (0.0, 0.0)
    while i < len(tokens):
        command = tokens[i]
        i += 1
        if command in "ML":
            current = (float(tokens[i]), float(tokens[i + 1]))
            points.append(current)
            i += 2
        elif command == "C":
            c1, c2, end = ((float(tokens[i + k]), float(tokens[i + k + 1])) for k in (0, 2, 4))
            for step in range(1, 21):
                t = step / 20
                points.append(tuple((1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t * t * p2
                                    + t ** 3 * p3 for p0, p1, p2, p3 in zip(current, c1, c2, end)))
            current = end
            i += 6
    return points


class MarkGenerator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = build_mark.load_tokens()
        cls.palette = build_mark.palette(cls.tokens)

    def test_committed_files_are_current(self):
        self.assertEqual(build_mark.main(["--check"]), 0, "re-run desktop/branding/tools/build_mark.py")

    def test_output_is_deterministic(self):
        self.assertEqual(build_mark.outputs(self.tokens), build_mark.outputs(build_mark.load_tokens()))
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            for target in (one, two):
                subprocess.run([sys.executable, "-B", str(BRANDING / "tools" / "build_mark.py"), "--output", target],
                               check=True, capture_output=True)
            for relative in build_mark.outputs(self.tokens):
                first, second = (Path(t) / relative for t in (one, two))
                self.assertEqual(first.read_bytes(), second.read_bytes(), relative)
                self.assertEqual(first.read_bytes(), (REPO / relative).read_bytes(), relative)
                self.assertNotIn(b"\r", first.read_bytes(), relative)

    def test_check_reports_a_stale_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                build_mark.main(["--output", tmp])
            self.assertEqual(build_mark.main(["--output", tmp, "--check"]), 0)
            (Path(tmp) / "desktop/branding/boswas-os-mark-flat.svg").write_text("<svg/>", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(build_mark.main(["--output", tmp, "--check"]), 1)
            self.assertIn("boswas-os-mark-flat.svg", err.getvalue())

    def test_the_full_colour_mark_keeps_its_format_contract(self):
        """What desktop/icons/tools/build_prism.py relies on (desktop/branding/README.md)."""
        text = (BRANDING / "boswas-os-mark.svg").read_text(encoding="utf-8")
        root = ET.fromstring(text)
        self.assertEqual((root.tag, root.get("viewBox"), root.get("width"), root.get("height")),
                         (SVG + "svg", "0 0 256 256", "256", "256"))
        children = list(root)
        self.assertEqual(children[0].tag, SVG + "defs")
        self.assertTrue(children[1:] and all(c.tag == SVG + "path" for c in children[1:]), "defs, then paths only")
        gradients = {}
        for gradient in children[0]:
            kind = gradient.tag.removeprefix(SVG)
            self.assertIn(kind, ("linearGradient", "radialGradient"))
            self.assertEqual(gradient.get("gradientUnits"), "userSpaceOnUse")
            coordinates = ("x1", "y1", "x2", "y2") if kind == "linearGradient" else ("cx", "cy", "r")
            self.assertEqual(set(gradient.attrib), {"id", "gradientUnits", *coordinates}, gradient.get("id"))
            for name in coordinates:
                float(gradient.get(name))
            self.assertTrue(gradient.get("id").startswith("boswas-mark-"))
            stops = list(gradient)
            self.assertGreaterEqual(len(stops), 2)
            for stop in stops:
                self.assertEqual(stop.tag, SVG + "stop")
                self.assertLessEqual(set(stop.attrib), {"offset", "stop-color", "stop-opacity"})
                self.assertRegex(stop.get("stop-color"), r"^#[0-9A-F]{6}$")
                self.assertTrue(0 <= float(stop.get("offset")) <= 1)
                self.assertTrue(0 <= float(stop.get("stop-opacity", "1")) <= 1)
            gradients[gradient.get("id")] = gradient
        used = set()
        for path in children[1:]:
            self.assertLessEqual(set(path.attrib), {"d", "fill", "fill-opacity", "fill-rule"})
            self.assertRegex(path.get("d"), r"^M[-0-9. MLCZ]+Z$", "absolute M, L, C, Z only")
            fill = path.get("fill")
            reference = re.fullmatch(r"url\(#([^)]+)\)", fill)
            if reference:
                self.assertIn(reference.group(1), gradients)
                used.add(reference.group(1))
            else:
                self.assertRegex(fill, r"^#[0-9A-F]{6}$")
        self.assertEqual(used, set(gradients), "every gradient is used")
        for forbidden in ("transform", "style=", "stroke", "<image", "<text", "href", "class="):
            self.assertNotIn(forbidden, text)

    def test_the_mark_fills_the_view_box_with_a_balanced_margin(self):
        root = ET.parse(BRANDING / "boswas-os-mark.svg").getroot()
        points = [p for path in root.iter(SVG + "path") for p in path_points(path.get("d"))]
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        top, bottom, left, right = min(ys), max(ys), min(xs), max(xs)
        for margin in (top, 256 - bottom):
            self.assertTrue(8 <= margin <= 12, f"vertical margin {margin:.2f}")
        self.assertAlmostEqual(left, 256 - right, delta=0.5, msg="centred across")
        self.assertGreaterEqual(left, 8)
        self.assertEqual((round(left), round(top), round(right), round(bottom)),
                         tuple(round(v) for v in build_mark.GEOMETRY["ink"]))

    def test_the_parts_keep_clear_of_each_other(self):
        """The stem, the bowl and the dot do not touch: the mark reads in one colour."""
        stem_x, _, stem_w, _ = build_mark.GEOMETRY["stem"]
        cx, cy, outer, inner = build_mark.GEOMETRY["bowl"]
        dot_x, dot_y, dot_r = build_mark.GEOMETRY["dot"]
        cut = build_mark.GEOMETRY["cut"]
        self.assertGreaterEqual(cut - (stem_x + stem_w), 8, "gap between the stem and the bowl")
        self.assertGreaterEqual(inner - (abs(dot_x - cx) + dot_r), 16, "the dot clear of the band")
        self.assertGreaterEqual(dot_x - dot_r - cut, 16, "the dot clear of the cut")
        self.assertEqual(dot_y, cy)
        # At 16 pixels the gap is still half a pixel and the dot two pixels across.
        self.assertGreaterEqual((cut - stem_x - stem_w) * 16 / 256, 0.5)
        self.assertGreaterEqual(2 * dot_r * 16 / 256, 2)

    def test_variants_share_the_shapes_of_the_mark(self):
        colour = ET.parse(BRANDING / "boswas-os-mark.svg").getroot()
        solid = [build_mark.FLAT.get(paint) for _, paint, flat in build_mark.layers() if flat]
        shapes = [p.get("d") for p, (_, _, flat) in zip(colour.iter(SVG + "path"), build_mark.layers()) if flat]
        self.assertEqual(len(shapes), 3)
        for name in ("boswas-os-mark-mono.svg", "boswas-os-mark-flat.svg"):
            root = ET.parse(BRANDING / name).getroot()
            self.assertEqual(root.get("viewBox"), "0 0 256 256")
            self.assertEqual([c.tag for c in root], [SVG + "path"] * 3, name)
            self.assertEqual([p.get("d") for p in root], shapes, name)
            fills = [p.get("fill") for p in root]
            if "mono" in name:
                self.assertEqual(fills, ["#FFFFFF"] * 3)
            else:
                self.assertEqual(fills, [build_mark.colour(self.tokens, ref) for ref in solid])
            self.assertNotIn("url(", (BRANDING / name).read_text(encoding="utf-8"))

    def test_no_colour_outside_the_token_palette(self):
        files = [BRANDING / name for name in MARKS] + [FAVICON, *COMPOSED, THEME]
        for path in files:
            with self.subTest(file=str(path.relative_to(REPO))):
                colours = {c.upper() for c in HEX.findall(path.read_text(encoding="utf-8"))}
                self.assertLessEqual(colours, self.palette, f"not in desktop/ui/tokens.json: {colours - self.palette}")

    def test_the_mark_uses_the_blue_family_and_the_warm_dot(self):
        refs = {stop[1] for _, stops in build_mark.PAINT.values() for stop in stops} | set(build_mark.FLAT.values())
        families = {ref.split(".")[0] for ref in refs}
        self.assertLessEqual(families, {"azure", "sky", "iris", "sun", "white"})
        self.assertEqual(build_mark.PAINT["dot"][0], "radial")

    def test_the_favicon_has_no_references(self):
        text = FAVICON.read_text(encoding="utf-8")
        root = ET.fromstring(text)
        self.assertEqual(root.get("viewBox"), "0 0 64 64")
        for forbidden in ("href", "url(", "<image", "<script", "foreignObject", "<text", "<style"):
            self.assertNotIn(forbidden, text)
        self.assertNotRegex(text, r"\son[a-z]+\s*=")
        nested = root.find(SVG + "svg")
        self.assertEqual(nested.get("viewBox"), "0 0 256 256")
        mark = ET.parse(BRANDING / "boswas-os-mark-flat.svg").getroot()
        self.assertEqual([(p.get("d"), p.get("fill")) for p in nested],
                         [(p.get("d"), p.get("fill")) for p in mark])


class Wordmarks(unittest.TestCase):
    FILES = {"wordmark-boswas-os.svg": "Boswas OS", "wordmark-boswas.svg": "Boswas", "wordmark-os.svg": "OS"}

    def test_outlined_inter_white_on_transparent(self):
        for name, text in self.FILES.items():
            with self.subTest(file=name):
                root = ET.parse(BRANDING / name).getroot()
                self.assertEqual(root.find(SVG + "title").text.split(" (")[0], text)
                self.assertIn("Inter SemiBold", root.find(SVG + "title").text)
                paths = list(root.iter(SVG + "path"))
                self.assertEqual(len(paths), 1)
                self.assertEqual(paths[0].get("fill"), "#FFFFFF")
                self.assertIsNone(root.find(SVG + "text"))
                x, y, w, h = (float(v) for v in root.get("viewBox").split())
                # Cropped to the ink: the cap height of Inter (0.727 em) plus overshoot, at 1000 to the em.
                self.assertTrue(727 <= h <= 760, h)
                self.assertLess(y, -700)
                self.assertAlmostEqual(float(root.get("width")), w, delta=1)
                self.assertAlmostEqual(float(root.get("height")), h, delta=1)

    def test_one_space_and_the_same_letters_across_files(self):
        widths = {name: float(ET.parse(BRANDING / name).getroot().get("viewBox").split()[2]) for name in self.FILES}
        self.assertLess(widths["wordmark-boswas.svg"] + widths["wordmark-os.svg"], widths["wordmark-boswas-os.svg"])

    def test_committed_wordmarks_are_current(self):
        try:
            import fontTools  # noqa: F401
        except ImportError:
            self.skipTest("python3-fonttools is not installed")
        module = load("build_wordmarks")
        try:
            module.find_font([])
        except SystemExit:
            self.skipTest("fonts-inter is not installed")
        self.assertEqual(module.main(["--check"]), 0, "re-run desktop/branding/tools/build_wordmarks.py")

    def test_kerning_reads_the_fonts_pairs(self):
        try:
            from fontTools.ttLib import TTFont
        except ImportError:
            self.skipTest("python3-fonttools is not installed")
        module = load("build_wordmarks")
        try:
            font = TTFont(module.find_font([]))
        except SystemExit:
            self.skipTest("fonts-inter is not installed")
        cmap = font.getBestCmap()
        self.assertLess(module.kerning(font, cmap[ord("T")], cmap[ord("o")]), -50)
        self.assertEqual(module.kerning(font, cmap[ord("o")], cmap[ord("o")]), 0)


class ComposedArtwork(unittest.TestCase):
    def test_references_resolve_and_inlining_makes_each_file_self_contained(self):
        for path in COMPOSED:
            with self.subTest(file=str(path.relative_to(REPO))):
                root = ET.parse(path).getroot()
                images = list(root.iter(SVG + "image"))
                self.assertTrue(images, "uses the mark")
                for image in images:
                    target = (path.parent / image.get("href")).resolve()
                    self.assertTrue(target.is_file(), image.get("href"))
                    self.assertTrue(target.name in MARKS or target.name.startswith("wordmark-"), target.name)
                    if target.name.startswith("wordmark-") or target.name == "boswas-os-mark-mono.svg":
                        self.assertRegex(image.get("data-fill", ""), r"^#[0-9A-F]{6}$")
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "out.svg"
                    subprocess.run([sys.executable, "-B", str(INLINE), str(path), str(out)], check=True)
                    text = out.read_text(encoding="utf-8")
                    inlined = ET.fromstring(text)
                    self.assertIsNone(inlined.find(f".//{SVG}image"))
                    ids = [e.get("id") for e in inlined.iter() if e.get("id")]
                    self.assertEqual(len(ids), len(set(ids)), "duplicate ids after inlining")
                    for ref in re.findall(r"url\(#([^)]+)\)", text):
                        self.assertIn(ref, ids)

    def test_artwork_that_shows_the_mark_whole_keeps_it_square(self):
        for path in COMPOSED:
            for image in ET.parse(path).getroot().iter(SVG + "image"):
                if Path(image.get("href")).name in MARKS:
                    self.assertAlmostEqual(float(image.get("width")), float(image.get("height")), places=2)

    def test_boot_menu_keeps_the_panel_the_grub_theme_draws_its_menu_in(self):
        root = ET.parse(BRANDING / "boot-splash.svg").getroot()
        self.assertEqual((root.get("width"), root.get("height")), ("800", "600"))
        panels = [r for r in root.iter(SVG + "rect") if r.get("y") == "282"]
        self.assertTrue(panels)
        x, y, w, h = (float(panels[0].get(k)) for k in ("x", "y", "width", "height"))
        theme = THEME.read_text(encoding="utf-8")
        self.assertIn("y 282-532", theme)
        menu = {k: float(v) for k, v in re.findall(r"^\s*(left|top|width|height) = ([0-9.]+)%", theme, re.M)[:4]}
        self.assertGreaterEqual(menu["left"] / 100 * 800, x)
        self.assertGreaterEqual(menu["top"] / 100 * 600, y)
        self.assertLessEqual((menu["left"] + menu["width"]) / 100 * 800, x + w)
        self.assertLessEqual((menu["top"] + menu["height"]) / 100 * 600, y + h)
        self.assertIn("@BOSWAS_VERSION@", (BRANDING / "boot-splash.svg").read_text(encoding="utf-8"))

    def test_the_traced_mark_is_retired_but_its_original_is_kept(self):
        self.assertFalse((BRANDING / "tools" / "trace_mark.py").exists())
        self.assertTrue((BRANDING / "source" / "boswas-os-logo.png").is_file())
        for path in [BRANDING / name for name in MARKS] + [FAVICON]:
            self.assertNotIn("trace_mark", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
