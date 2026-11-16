"""install.sh and the installed theme: names, XCursor files, hotspots, pictures, reproducibility."""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import support

xc = support.xcursor()

SOLID = 128          # alpha from which a pixel belongs to the shape; the shadow stays far below it
ARROW_FAMILY = ("default", "context-menu", "help", "progress", "alias", "copy", "no-drop")
CENTRED = ("crosshair", "cell", "text", "vertical-text", "move", "all-scroll", "not-allowed", "wait",
           "ew-resize", "ns-resize", "nesw-resize", "nwse-resize", "col-resize", "row-resize")


def solid_pixels(image) -> list:
    return [(x, y) for y in range(image.height) for x in range(image.width) if image.alpha(x, y) >= SOLID]


def bounds(points):
    xs, ys = [x for x, _ in points], [y for _, y in points]
    return min(xs), min(ys), max(xs), max(ys)


def is_white(rgb) -> bool:
    return min(rgb) >= 235


def is_light(rgb) -> bool:
    return min(rgb) >= 150


def is_dark(rgb) -> bool:
    return max(rgb) <= 90


def alpha_at(image, x: int, y: int) -> int:
    """Alpha of a pixel; outside the image there is nothing."""
    return image.alpha(x, y) if 0 <= x < image.width and 0 <= y < image.height else 0


class InstallScriptTest(unittest.TestCase):
    def test_posix_syntax(self):
        for shell in ("sh", "dash"):
            if shutil.which(shell):
                result = subprocess.run([shell, "-n", str(support.INSTALL)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_strict_mode_and_pipeline(self):
        text = support.INSTALL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("\nset -eu\n", text)
        self.assertIn("\numask 022\n", text)
        self.assertIn('build_cursors.py" --check', text)
        self.assertIn("rsvg-convert --width", text)
        self.assertIn('install -m 0644 "$source/index.theme" "$theme/index.theme"', text)
        self.assertIn('theme="$dest/usr/share/icons/Boswas-Cursors"', text)
        # The XCursor files come from the theme's own writer.
        code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        self.assertFalse([line for line in code if "xcursorgen" in line])
        # No bash: no [[ ]], arrays, "function" or "local".
        for line in code:
            self.assertIsNone(re.search(r"\[\[|\]\]|\bfunction\b|\blocal\b|<<<|\bdeclare\b", line), line)

    def test_requires_destdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            for args in ((), ("",), (tmp, "extra")):
                with self.subTest(args=args):
                    result = subprocess.run(["sh", str(support.INSTALL), *args], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("usage: install.sh DESTDIR", result.stderr)
                    self.assertEqual(result.stdout, "")
            self.assertEqual(os.listdir(tmp), [])

    def test_reports_a_missing_renderer(self):
        """Without rsvg-convert the script stops before it writes anything."""
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "bin"
            tools.mkdir()
            for program in ("python3", "dirname"):
                found = shutil.which(program)
                self.assertIsNotNone(found, program)
                (tools / program).symlink_to(found)
            dest = Path(tmp) / "dest"
            result = subprocess.run([shutil.which("sh"), str(support.INSTALL), str(dest)], capture_output=True,
                                    text=True, env={"PATH": str(tools)})
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("rsvg-convert is required", result.stderr)
            self.assertFalse(dest.exists())


@unittest.skipUnless(support.HAVE_RSVG, support.NEEDS_RSVG)
class InstalledThemeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.theme = support.installed()
        cls.cursors = cls.theme / "cursors"
        cls.manifest = xc.load_manifest(support.MANIFEST_FILE)
        cls.shapes = support.shapes()
        cls.targets = support.targets()
        cls.images = {name: xc.decode((cls.cursors / name).read_bytes()) for name in cls.shapes}

    def of_size(self, name: str, size: int) -> list:
        return [image for image in self.images[name] if image.size == size]

    # -- names ---------------------------------------------------------------
    def test_directory_holds_exactly_the_manifest_s_names(self):
        self.assertEqual(sorted(p.name for p in self.theme.iterdir()), ["cursor.theme", "cursors", "index.theme"])
        self.assertEqual(sorted(p.name for p in self.cursors.iterdir()), sorted(self.targets))
        for name, shape in self.targets.items():
            path = self.cursors / name
            with self.subTest(name=name):
                if name == shape:
                    self.assertTrue(path.is_file() and not path.is_symlink())
                else:
                    self.assertTrue(path.is_symlink())
                    self.assertEqual(os.readlink(path), shape)

    def test_every_required_name_resolves(self):
        required = (*support.CSS_NAMES, *support.LEGACY_NAMES, "00008160000006810000408080010102")
        for name in required:
            with self.subTest(name=name):
                path = self.cursors / name
                self.assertTrue(path.is_file(), f"{name} is missing or dangling")
                self.assertEqual(path.resolve().parent, self.cursors.resolve())
                self.assertEqual(path.read_bytes()[:4], b"Xcur")

    def test_no_dangling_or_chained_links(self):
        links = [p for p in self.cursors.iterdir() if p.is_symlink()]
        self.assertGreater(len(links), 80)
        for link in links:
            with self.subTest(link=link.name):
                target = os.readlink(link)
                self.assertNotIn("/", target)
                self.assertTrue((self.cursors / target).is_file())
                self.assertFalse((self.cursors / target).is_symlink())

    def test_modes_and_index(self):
        self.assertEqual(stat.S_IMODE(self.theme.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.cursors.stat().st_mode), 0o755)
        for name in self.shapes:
            self.assertEqual(stat.S_IMODE((self.cursors / name).stat().st_mode), 0o644, name)
        index = self.theme / "index.theme"
        self.assertEqual(stat.S_IMODE(index.stat().st_mode), 0o644)
        self.assertEqual(index.read_bytes(), (support.SOURCE_DIR / "index.theme").read_bytes())

    def test_the_file_for_the_default_theme_alternative_inherits_this_theme(self):
        # Debian points /usr/share/icons/default/index.theme at a theme's cursor.theme.
        default = self.theme / "cursor.theme"
        self.assertEqual(stat.S_IMODE(default.stat().st_mode), 0o644)
        self.assertEqual(default.read_text(encoding="utf-8"), "[Icon Theme]\nInherits=Boswas-Cursors\n")

    # -- XCursor files -------------------------------------------------------
    def test_every_file_has_all_five_sizes(self):
        for name, cursor in self.shapes.items():
            images = self.images[name]
            frames = len(cursor["frames"])
            with self.subTest(shape=name):
                self.assertEqual([image.size for image in images],
                                 [size for size in support.SIZES for _ in range(frames)])
                for image in images:
                    pixels = support.PIXELS[image.size]
                    self.assertEqual((image.width, image.height), (pixels, pixels))
                    self.assertEqual((image.xhot, image.yhot), xc.hotspot_for(self.manifest, cursor["hotspot"], pixels))
                    self.assertTrue(0 <= image.xhot < image.width and 0 <= image.yhot < image.height)
                for size in support.SIZES:
                    self.assertEqual([image.delay for image in self.of_size(name, size)],
                                     [frame["delay"] for frame in cursor["frames"]])

    def test_files_fit_the_compositor_s_limits(self):
        """boswas-compositor reads at most 16 MiB of a cursor file and images up to 512 pixels."""
        total = 0
        for name in self.shapes:
            size = (self.cursors / name).stat().st_size
            total += size
            self.assertLess(size, 16 * 1024 * 1024, name)
            self.assertLessEqual(max(image.width for image in self.images[name]), 512)
        self.assertLess(total, 12 * 1024 * 1024)

    def test_pixels_are_premultiplied(self):
        for name in self.shapes:
            for image in self.images[name][::max(1, len(self.shapes[name]["frames"]) // 2)]:
                pixels = image.pixels
                alpha = pixels[3::4]
                with self.subTest(shape=name, size=image.size):
                    for channel in (pixels[0::4], pixels[1::4], pixels[2::4]):
                        self.assertTrue(all(c <= a for c, a in zip(channel, alpha)))

    # -- pictures ------------------------------------------------------------
    def test_images_are_not_empty_and_not_cut_off(self):
        for name in self.shapes:
            for size in support.SIZES:
                image = self.of_size(name, size)[0]
                scale = image.width / 32
                with self.subTest(shape=name, size=size):
                    solid = solid_pixels(image)
                    self.assertGreater(len(solid), 40 * scale * scale)
                    self.assertLess(len(solid), 0.5 * image.width * image.height)
                    # A clear margin all around: neither the shape nor its shadow touches the edge.
                    last = image.width - 1
                    edge = [image.alpha(i, j) for i in range(image.width) for j in (0, last)]
                    edge += [image.alpha(j, i) for i in range(image.height) for j in (0, last)]
                    self.assertEqual(max(edge), 0)
                    x0, y0, x1, y1 = bounds(solid)
                    self.assertGreaterEqual(min(x0, y0), int(scale))
                    self.assertLessEqual(max(x1, y1), last - int(2 * scale))

    def test_the_arrow_is_mostly_white_with_a_dark_edge(self):
        for size in support.SIZES:
            image = self.of_size("default", size)[0]
            with self.subTest(size=size):
                opaque = [(x, y) for x, y in solid_pixels(image) if image.alpha(x, y) == 255]
                colours = [image.colour(x, y) for x, y in opaque]
                white = sum(1 for rgb in colours if is_white(rgb))
                dark = sum(1 for rgb in colours if is_dark(rgb))
                self.assertGreater(white / len(colours), 0.5)
                self.assertGreater(dark / len(colours), 0.15)
                self.assertLess(dark / len(colours), 0.5)
                # About 12 by 19 pixels at the nominal size 24, like the pointer it is modelled on.
                x0, y0, x1, y1 = bounds(solid_pixels(image))
                scale = image.width / 32
                self.assertAlmostEqual((x1 - x0 + 1) / scale, 12.5, delta=1.5)
                self.assertAlmostEqual((y1 - y0 + 1) / scale, 19.5, delta=1.5)

    def test_white_never_touches_the_background(self):
        """Every shape has its outline: the pixels beside a white pixel belong to the shape.

        The four pixels that share an edge with a white pixel are solid; the
        four at its corners are at least partly covered (the one pixel wide
        outline of a small circle passes between them at the nominal size).
        """
        beside = ((1, 0), (-1, 0), (0, 1), (0, -1))
        corners = ((1, 1), (1, -1), (-1, 1), (-1, -1))
        for name in self.shapes:
            for size in support.SIZES:
                image = self.of_size(name, size)[0]
                bare = []
                rim = []
                for x, y in solid_pixels(image):
                    edge = min(alpha_at(image, x + dx, y + dy) for dx, dy in beside)
                    corner = min(alpha_at(image, x + dx, y + dy) for dx, dy in corners)
                    if image.alpha(x, y) == 255 and is_white(image.colour(x, y)) and (edge < SOLID or corner < 48):
                        bare.append((x, y))
                    if edge < 64:
                        rim.append(image.colour(x, y))
                with self.subTest(shape=name, size=size):
                    self.assertEqual(bare, [])
                    # The rim of the silhouette is the outline colour, never white.
                    self.assertEqual([rgb for rgb in rim if is_white(rgb)], [])
                    self.assertGreater(sum(1 for rgb in rim if is_dark(rgb)) / len(rim), 0.85)

    def test_shapes_use_only_white_outline_and_accent(self):
        accent = tuple(int(support.token_colour("semantic.light.accent")[i:i + 2], 16) for i in (1, 3, 5))
        order = sorted(range(3), key=lambda i: accent[i])     # channels from the weakest to the strongest
        self.assertGreater(max(accent) - min(accent), 60, "the accent is a colour, not a grey")
        for name in self.shapes:
            image = self.of_size(name, 96)[0]
            coloured = 0
            for x, y in solid_pixels(image):
                if image.alpha(x, y) < 255:
                    continue
                red, green, blue = image.colour(x, y)
                if max(red, green, blue) - min(red, green, blue) > 24:
                    coloured += 1
                    # A tint or shade of the accent: the same order of the channels.
                    self.assertEqual(sorted((red, green, blue)), [(red, green, blue)[i] for i in order],
                                     (name, red, green, blue))
            with self.subTest(shape=name):
                if name in ("wait", "progress"):
                    self.assertGreater(coloured, 100)
                else:
                    self.assertEqual(coloured, 0)

    # -- hotspots ------------------------------------------------------------
    def test_arrow_hotspot_is_the_tip_at_the_top_left(self):
        for name in ARROW_FAMILY:
            for image in self.images[name]:
                scale = image.width / 32
                with self.subTest(shape=name, size=image.size):
                    self.assertGreaterEqual(image.alpha(image.xhot, image.yhot), SOLID)
                    self.assertTrue(is_dark(image.colour(image.xhot, image.yhot)))
                    x0, y0, _, _ = bounds(solid_pixels(image))
                    self.assertTrue(0 <= image.xhot - x0 <= scale, (image.xhot, x0))
                    self.assertTrue(0 <= image.yhot - y0 <= scale, (image.yhot, y0))
                    self.assertEqual((image.xhot, image.yhot),
                                     (self.of_size("default", image.size)[0].xhot,
                                      self.of_size("default", image.size)[0].yhot))

    def test_centred_shapes_have_the_hotspot_in_the_middle(self):
        for name in CENTRED:
            for size in support.SIZES:
                image = self.of_size(name, size)[0]
                scale = image.width / 32
                x0, y0, x1, y1 = bounds(solid_pixels(image))
                with self.subTest(shape=name, size=size):
                    self.assertAlmostEqual(image.xhot, (x0 + x1) / 2, delta=scale)
                    self.assertAlmostEqual(image.yhot, (y0 + y1) / 2, delta=scale)

    def test_text_hotspot_is_on_the_beam(self):
        for name in ("text", "vertical-text", "crosshair"):
            for image in self.images[name]:
                with self.subTest(shape=name, size=image.size):
                    self.assertEqual(image.alpha(image.xhot, image.yhot), 255)
                    self.assertTrue(is_light(image.colour(image.xhot, image.yhot)))
                    if image.size in (24, 48, 96):             # whole pixels per unit: the core is pure white
                        self.assertTrue(is_white(image.colour(image.xhot, image.yhot)))
        # The I-beam is tall and narrow, its twin wide and flat.
        x0, y0, x1, y1 = bounds(solid_pixels(self.of_size("text", 24)[0]))
        self.assertEqual((x1 - x0 + 1, y1 - y0 + 1), (9, 19))
        x0, y0, x1, y1 = bounds(solid_pixels(self.of_size("vertical-text", 24)[0]))
        self.assertEqual((x1 - x0 + 1, y1 - y0 + 1), (19, 9))

    def test_other_hotspots(self):
        for size in support.SIZES:
            scale = support.PIXELS[size] / 32
            with self.subTest(size=size):
                # The pointing hand: the tip of the index finger, at the top of the picture.
                image = self.of_size("pointer", size)[0]
                solid = solid_pixels(image)
                x0, y0, x1, y1 = bounds(solid)
                self.assertGreaterEqual(image.alpha(image.xhot, image.yhot), SOLID)
                self.assertTrue(0 <= image.yhot - y0 <= 2 * scale)
                top = [x for x, y in solid if y == y0]
                self.assertTrue(min(top) - scale <= image.xhot <= max(top) + scale)

                # The hands: inside the hand, near its middle.
                for name in ("grab", "grabbing"):
                    image = self.of_size(name, size)[0]
                    x0, y0, x1, y1 = bounds(solid_pixels(image))
                    self.assertEqual(image.alpha(image.xhot, image.yhot), 255)
                    self.assertAlmostEqual(image.xhot, (x0 + x1) / 2, delta=3 * scale)
                    self.assertAlmostEqual(image.yhot, (y0 + y1) / 2, delta=4 * scale)

                # The magnifiers: the middle of the lens, which is the upper left of the picture.
                for name in ("zoom-in", "zoom-out"):
                    image = self.of_size(name, size)[0]
                    x0, y0, x1, y1 = bounds(solid_pixels(image))
                    self.assertEqual(image.alpha(image.xhot, image.yhot), 255)
                    self.assertAlmostEqual(image.xhot - x0, 8 * scale, delta=scale)
                    self.assertAlmostEqual(image.yhot - y0, 8 * scale, delta=scale)

                # The arrow that points up: the top of its middle.
                image = self.of_size("up-arrow", size)[0]
                x0, y0, x1, y1 = bounds(solid_pixels(image))
                self.assertTrue(0 <= image.yhot - y0 <= scale)
                self.assertAlmostEqual(image.xhot, (x0 + x1) / 2, delta=scale)

                # The pencil: its point at the lower left.
                image = self.of_size("pencil", size)[0]
                x0, y0, x1, y1 = bounds(solid_pixels(image))
                self.assertTrue(0 <= image.xhot - x0 <= 1.5 * scale)
                self.assertTrue(0 <= y1 - image.yhot <= 1.5 * scale)

    # -- animation -----------------------------------------------------------
    def test_busy_cursors_are_animated(self):
        for name in ("wait", "progress"):
            for size in support.SIZES:
                frames = self.of_size(name, size)
                with self.subTest(shape=name, size=size):
                    self.assertTrue(16 <= len(frames) <= 24)
                    self.assertTrue(all(30 <= frame.delay <= 50 for frame in frames))
                    self.assertTrue(600 <= sum(frame.delay for frame in frames) <= 1200, "one turn takes about a second")
                    self.assertEqual(len({frame.pixels for frame in frames}), len(frames))
                    self.assertEqual(len({(f.width, f.height, f.xhot, f.yhot) for f in frames}), 1)
                    # Only the arc turns: the silhouette is the same in every frame (the renderer's
                    # rounding may change the alpha of a pixel under the arc's edge by a step or two).
                    first = frames[0].pixels[3::4]
                    for frame in frames[1:]:
                        self.assertLessEqual(max(abs(a - b) for a, b in zip(first, frame.pixels[3::4])), 2)

    def test_only_the_busy_cursors_are_animated(self):
        for name in self.shapes:
            if name not in ("wait", "progress"):
                frames = self.of_size(name, 24)
                self.assertEqual([frame.delay for frame in frames], [0], name)

    def test_progress_keeps_the_arrow_still(self):
        """Between frames only the ring changes; above the ring the picture is the plain arrow."""
        arrow = self.of_size("default", 48)[0]
        frames = self.of_size("progress", 48)
        width = frames[0].width
        ring = (15 * 2, 13 * 2, 30 * 2, 28 * 2)        # left, top, right, bottom of the ring's box, in pixels
        for frame in frames:
            for y in range(frame.height):
                row = slice(4 * y * width, 4 * (y + 1) * width)
                if y < 11 * 2:
                    self.assertEqual(frame.pixels[row], arrow.pixels[row])
                if ring[1] <= y < ring[3]:
                    row = slice(4 * y * width, 4 * (y * width + ring[0]))
                self.assertEqual(frame.pixels[row], frames[0].pixels[row])

    def test_the_arc_turns_clockwise(self):
        """The strongest colour of the ring moves from the top over the right to the bottom."""
        def head(image):
            best, where = -1, None
            for x, y in solid_pixels(image):
                red, green, blue = image.colour(x, y)
                if blue - green > best:
                    best, where = blue - green, (x, y)
            return where

        frames = self.of_size("wait", 96)
        quarter = len(frames) // 4
        centre = 64
        x, y = head(frames[0])
        self.assertLess(y, centre - 16)                # twelve o'clock
        x, y = head(frames[quarter])
        self.assertGreater(x, centre + 16)             # three o'clock
        x, y = head(frames[2 * quarter])
        self.assertGreater(y, centre + 16)             # six o'clock
        x, y = head(frames[3 * quarter])
        self.assertLess(x, centre - 16)                # nine o'clock

    # -- the install as a whole ----------------------------------------------
    def test_install_twice_gives_identical_files(self):
        first = support.snapshot(self.theme)
        with tempfile.TemporaryDirectory() as tmp:
            result = support.run_install(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            other = Path(tmp) / "usr/share/icons" / support.THEME
            self.assertEqual(support.snapshot(other), first)
            # Nothing else is written below DESTDIR.
            everything = sorted(p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*") if not p.is_dir())
            self.assertEqual([p for p in everything if not p.startswith(f"usr/share/icons/{support.THEME}/")], [])
            # Installing over an existing theme changes nothing.
            result = support.run_install(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(support.snapshot(other), first)

    def test_install_leaves_the_source_tree_alone(self):
        self.assertEqual(sorted(p.name for p in support.CURSORS_DIR.iterdir() if p.name != "__pycache__"),
                         [support.THEME, "README.md", "install.sh", "tests", "tools"])
        self.assertEqual([p for p in support.SOURCE_DIR.rglob("*.png")], [])

    def test_refuses_sources_that_differ_from_the_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "repo/desktop/cursors"
            shutil.copytree(support.CURSORS_DIR, copy, ignore=shutil.ignore_patterns("__pycache__", "tests"))
            (copy.parent / "ui").mkdir()
            shutil.copy(support.TOKENS_FILE, copy.parent / "ui/tokens.json")
            edited = copy / support.THEME / "src/default.svg"
            edited.write_text(edited.read_text(encoding="utf-8").replace("#FFFFFF", "#FF0000"), encoding="utf-8")
            dest = Path(tmp) / "dest"
            result = subprocess.run(["sh", str(copy / "install.sh"), str(dest)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("out of date: src/default.svg", result.stderr)
            self.assertFalse(dest.exists())


@unittest.skipUnless(support.HAVE_RSVG, support.NEEDS_RSVG)
@unittest.skipUnless(support.libxcursor(), "libXcursor is not installed")
class LibXcursorThemeTest(unittest.TestCase):
    """libXcursor, which X11 clients use, finds the theme and its names."""

    @classmethod
    def setUpClass(cls):
        cls.lib = support.libxcursor()
        cls.theme = support.installed()
        cls.saved = os.environ.get("XCURSOR_PATH")
        os.environ["XCURSOR_PATH"] = str(cls.theme.parent)

    @classmethod
    def tearDownClass(cls):
        if cls.saved is None:
            os.environ.pop("XCURSOR_PATH", None)
        else:
            os.environ["XCURSOR_PATH"] = cls.saved

    def load(self, name: str, size: int) -> list:
        pointer = self.lib.XcursorLibraryLoadImages(name.encode(), support.THEME.encode(), size)
        return support.copy_images(self.lib, pointer)

    def test_every_name_loads_at_every_size(self):
        shapes = support.shapes()
        for name, shape in support.targets().items():
            ours = xc.decode((self.theme / "cursors" / shape).read_bytes())
            for size in support.SIZES:
                with self.subTest(name=name, size=size):
                    got = self.load(name, size)
                    expected = [(i.size, i.width, i.height, i.xhot, i.yhot, i.delay, i.pixels)
                                for i in ours if i.size == size]
                    self.assertEqual(len(got), len(shapes[shape]["frames"]))
                    self.assertEqual(got, expected)

    def test_nearest_size_and_unknown_names(self):
        self.assertEqual([(i[0], i[1]) for i in self.load("left_ptr", 27)], [(24, 32)])
        self.assertEqual([(i[0], i[1]) for i in self.load("left_ptr", 30)], [(32, 43)])
        self.assertEqual([(i[0], i[1]) for i in self.load("left_ptr", 200)], [(96, 128)])
        self.assertEqual(self.load("no-such-cursor", 24), [])


if __name__ == "__main__":
    unittest.main()
