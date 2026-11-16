"""tools/xcursor.py: the PNG reader, the XCursor writer and reader, the manifest checks."""

import json
import os
import random
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

import support

xc = support.xcursor()


# ---------------------------------------------------------------------------
# A PNG writer for the tests: any colour type of 8 bits, any row filter
# ---------------------------------------------------------------------------
def png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def filter_row(kind: int, row: bytes, prior: bytes, bpp: int) -> bytes:
    out = bytearray()
    for i, value in enumerate(row):
        a = row[i - bpp] if i >= bpp else 0
        b = prior[i]
        c = prior[i - bpp] if i >= bpp else 0
        predictor = (0, a, b, (a + b) // 2, paeth(a, b, c))[kind]
        out.append((value - predictor) & 0xFF)
    return bytes(out)


def make_png(width: int, height: int, samples: bytes, colour_type: int = 6, filters=(0,), depth: int = 8,
             interlace: int = 0, extra: bytes = b"", level: int = 6) -> bytes:
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour_type]
    stride = width * bpp
    raw = bytearray()
    prior = bytes(stride)
    for y in range(height):
        row = samples[y * stride:(y + 1) * stride]
        kind = filters[y % len(filters)]
        raw.append(kind)
        raw += filter_row(kind, row, prior, bpp) if kind <= 4 else row
        prior = row
    packed = zlib.compress(bytes(raw), level)
    half = len(packed) // 2                       # two IDAT chunks: the reader has to join them
    return (xc.PNG_SIGNATURE + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, colour_type,
                                                              0, 0, interlace))
            + extra + png_chunk(b"IDAT", packed[:half]) + png_chunk(b"IDAT", packed[half:])
            + png_chunk(b"IEND", b""))


def noise(count: int, seed: int = 1) -> bytes:
    generator = random.Random(seed)
    return bytes(generator.randrange(256) for _ in range(count))


def solid(width: int, height: int, rgba) -> bytes:
    return bytes(rgba) * (width * height)


class PngReaderTest(unittest.TestCase):
    def test_every_row_filter(self):
        width, height = 13, 10
        samples = noise(width * height * 4)
        for filters in ((0,), (1,), (2,), (3,), (4,), (4, 3, 2, 1, 0), (1, 4)):
            with self.subTest(filters=filters):
                got = xc.read_png(make_png(width, height, samples, 6, filters))
                self.assertEqual(got, (width, height, samples))

    def test_smooth_picture_with_every_filter(self):
        """A gradient with transparent rows, the kind of picture a cursor is."""
        width, height = 16, 12
        samples = bytearray()
        for y in range(height):
            for x in range(width):
                alpha = 0 if y < 3 or y > 9 else min(255, 40 * x)
                samples += bytes((x * 16, y * 20, (x + y) * 8, alpha)) if alpha else bytes(4)
        samples = bytes(samples)
        for kind in range(5):
            with self.subTest(filter=kind):
                self.assertEqual(xc.read_png(make_png(width, height, samples, 6, (kind,)))[2], samples)

    def test_other_colour_types_become_rgba(self):
        rgb = noise(5 * 4 * 3, 2)
        got = xc.read_png(make_png(5, 4, rgb, 2, (4, 1)))
        self.assertEqual(got[2], b"".join(rgb[i:i + 3] + b"\xff" for i in range(0, len(rgb), 3)))
        grey = noise(5 * 4, 3)
        got = xc.read_png(make_png(5, 4, grey, 0, (2, 3)))
        self.assertEqual(got[2], b"".join(bytes((v, v, v, 255)) for v in grey))
        grey_alpha = noise(5 * 4 * 2, 4)
        got = xc.read_png(make_png(5, 4, grey_alpha, 4, (3, 4)))
        self.assertEqual(got[2], b"".join(bytes((grey_alpha[i],) * 3 + (grey_alpha[i + 1],))
                                          for i in range(0, len(grey_alpha), 2)))

    def test_ancillary_chunks_are_ignored(self):
        samples = noise(3 * 3 * 4, 5)
        extra = png_chunk(b"bKGD", b"\x00\xff\x00\xff\x00\xff") + png_chunk(b"tEXt", b"Software\x00test")
        self.assertEqual(xc.read_png(make_png(3, 3, samples, extra=extra))[2], samples)

    def test_rejects_what_it_cannot_read(self):
        good = make_png(4, 4, noise(64, 6))
        cases = {
            "not a PNG": b"GIF89a" + good,
            "truncated": good[:-20],
            "cut inside a chunk": good[:40],
            "bad checksum": good[:30] + bytes([good[30] ^ 1]) + good[31:],
            "16 bit": make_png(4, 4, noise(64, 6), depth=16),
            "interlaced": make_png(4, 4, noise(64, 6), interlace=1),
            "palette": make_png(4, 4, noise(16, 6), colour_type=3),
            "unknown filter": make_png(4, 4, noise(64, 6), filters=(5,)),
            "too little data": make_png(4, 5, noise(64, 6)),
            "no header": xc.PNG_SIGNATURE + png_chunk(b"IEND", b""),
            "zero size": make_png(0, 4, b""),
            "garbage data": (xc.PNG_SIGNATURE + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
                             + png_chunk(b"IDAT", b"not zlib") + png_chunk(b"IEND", b"")),
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(xc.FormatError):
                    xc.read_png(data)


class PremultiplyTest(unittest.TestCase):
    def test_known_pixels(self):
        rgba = bytes((255, 255, 255, 255,  10, 20, 30, 255,  255, 255, 255, 128,
                      200, 100, 50, 51,  9, 9, 9, 0,  24, 24, 27, 195))
        self.assertEqual(xc.premultiply(rgba),
                         bytes((255, 255, 255, 255,  30, 20, 10, 255,  128, 128, 128, 128,
                                10, 20, 40, 51,  0, 0, 0, 0,  21, 18, 18, 195)))

    def test_rounds_to_the_nearest_value(self):
        for alpha in range(256):
            rgba = b"".join(bytes((value, 255 - value, value, alpha)) for value in range(256))
            out = xc.premultiply(rgba)
            for value in range(256):
                expected = (2 * value * alpha + 255) // 510
                self.assertEqual(out[4 * value + 2], expected, (value, alpha))
                self.assertLessEqual(max(out[4 * value:4 * value + 3]), alpha)

    def test_uniform_alpha_shortcuts(self):
        self.assertEqual(xc.premultiply(bytes((1, 2, 3, 0, 4, 5, 6, 0))), bytes(8))
        self.assertEqual(xc.premultiply(bytes((1, 2, 3, 255, 4, 5, 6, 255))), bytes((3, 2, 1, 255, 6, 5, 4, 255)))


def image(size=24, width=2, height=1, xhot=0, yhot=0, delay=0, pixels=None):
    return xc.Image(size, width, height, xhot, yhot, delay,
                    bytes(range(1, 4 * width * height + 1)) if pixels is None else pixels)


class XCursorFormatTest(unittest.TestCase):
    def test_bytes_of_a_small_file(self):
        data = xc.encode([image(size=24, width=2, height=1, xhot=1, yhot=0, delay=0)])
        expected = (b"Xcur" + struct.pack("<III", 16, 0x10000, 1)
                    + struct.pack("<III", 0xFFFD0002, 24, 28)
                    + struct.pack("<IIIIIIIII", 36, 0xFFFD0002, 24, 1, 2, 1, 1, 0, 0)
                    + bytes(range(1, 9)))
        self.assertEqual(data, expected)

    def test_round_trip(self):
        images = [image(24, 3, 2, 1, 1, 40), image(24, 3, 2, 1, 1, 50), image(48, 6, 4, 2, 3, 40),
                  image(48, 6, 4, 2, 3, 50)]
        data = xc.encode(images)
        back = xc.decode(data)
        self.assertEqual([(i.size, i.width, i.height, i.xhot, i.yhot, i.delay, i.pixels) for i in back],
                         [(i.size, i.width, i.height, i.xhot, i.yhot, i.delay, i.pixels) for i in images])
        self.assertEqual(xc.encode(back), data)
        # The table points at every chunk, and the file ends with the last pixel.
        self.assertEqual(len(data), 16 + 12 * 4 + sum(36 + len(i.pixels) for i in images))

    def test_pixel_accessors(self):
        picture = xc.Image(24, 2, 1, 0, 0, 0, bytes((128, 64, 32, 128, 0, 0, 0, 0)))
        self.assertEqual(picture.alpha(0, 0), 128)
        self.assertEqual(picture.colour(0, 0), (64, 128, 255))
        self.assertEqual((picture.alpha(1, 0), picture.colour(1, 0)), (0, (0, 0, 0)))

    def test_encode_refuses_bad_images(self):
        for bad in (image(xhot=2), image(yhot=1), image(width=0), image(pixels=b"\x00" * 4),
                    image(size=0), image(delay=70000)):
            with self.assertRaises(xc.FormatError):
                xc.encode([bad])
        with self.assertRaises(xc.FormatError):
            xc.encode([])

    def test_decode_refuses_bad_files(self):
        good = xc.encode([image(24, 2, 2, 1, 1, 0)])

        def patched(offset, value):
            return good[:offset] + struct.pack("<I", value) + good[offset + 4:]

        cases = {
            "empty": b"",
            "other magic": b"Xcux" + good[4:],
            "truncated pixels": good[:-1],
            "table outside the file": patched(12, 1000),
            "chunk outside the file": patched(24, len(good)),
            "wrong chunk header size": patched(28, 40),
            "wrong chunk type": patched(32, 0xFFFE0001),
            "size differs from the table": patched(36, 32),
            "unknown image version": patched(40, 2),
            "zero width": patched(44, 0),
            "huge height": patched(48, 0x8000),
            "hotspot outside": patched(52, 2),
            "no images": xc.encode([image()])[:12] + struct.pack("<I", 0),
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(xc.FormatError):
                    xc.decode(data)

    def test_decode_skips_comment_chunks(self):
        body = xc.encode([image(24, 1, 1, 0, 0, 0)])
        chunk = body[28:]
        comment = struct.pack("<IIIII", 20, 0xFFFE0001, 1, 1, 0)
        data = (b"Xcur" + struct.pack("<III", 16, 0x10000, 2)
                + struct.pack("<III", 0xFFFE0001, 1, 40) + struct.pack("<III", 0xFFFD0002, 24, 60)
                + comment + chunk)
        self.assertEqual([(i.size, i.width) for i in xc.decode(data)], [(24, 1)])


def small_manifest(**changes) -> dict:
    manifest = {
        "theme": "Test", "canvas": 32, "nominal": 24, "sizes": [24, 48],
        "cursors": [
            {"name": "default", "hotspot": [2.5, 2.5], "aliases": ["arrow", "left_ptr"],
             "frames": [{"svg": "default.svg", "delay": 0}]},
            {"name": "wait", "hotspot": [16.0, 16.0], "aliases": ["watch"],
             "frames": [{"svg": "wait-01.svg", "delay": 40}, {"svg": "wait-02.svg", "delay": 40}]},
        ],
    }
    manifest.update(changes)
    return manifest


class ManifestCheckTest(unittest.TestCase):
    def load(self, manifest):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cursors.json"
            path.write_text(manifest if isinstance(manifest, str) else json.dumps(manifest), encoding="utf-8")
            return xc.load_manifest(path)

    def test_accepts_the_committed_manifest(self):
        manifest = xc.load_manifest(support.MANIFEST_FILE)
        self.assertEqual(manifest["sizes"], list(support.SIZES))
        self.assertEqual(self.load(small_manifest())["canvas"], 32)

    def test_refuses_bad_manifests(self):
        def cursor(**changes):
            entry = {"name": "x", "hotspot": [1, 1], "aliases": [], "frames": [{"svg": "x.svg", "delay": 0}]}
            entry.update(changes)
            return small_manifest(cursors=[entry])

        cases = {
            "not JSON": "{",
            "no cursors": small_manifest(cursors=[]),
            "missing key": {"canvas": 32},
            "sizes not sorted": small_manifest(sizes=[48, 24]),
            "size too large": small_manifest(sizes=[24, 4096]),
            "nominal larger than the canvas": small_manifest(nominal=64),
            "path in a name": cursor(name="../x"),
            "slash in an alias": cursor(aliases=["a/b"]),
            "dot file": cursor(name=".hidden"),
            "empty name": cursor(name=""),
            "duplicate alias": cursor(aliases=["a", "a"]),
            "alias equals the name": cursor(aliases=["x"]),
            "hotspot outside": cursor(hotspot=[32, 0]),
            "hotspot not a pair": cursor(hotspot=[1]),
            "no frames": cursor(frames=[]),
            "frame outside src": cursor(frames=[{"svg": "../x.svg", "delay": 0}]),
            "frame not an svg": cursor(frames=[{"svg": "x.png", "delay": 0}]),
            "animation without delay": cursor(frames=[{"svg": "a.svg", "delay": 40}, {"svg": "b.svg", "delay": 0}]),
            "negative delay": cursor(frames=[{"svg": "a.svg", "delay": -1}]),
            "incomplete entry": small_manifest(cursors=[{"name": "x"}]),
        }
        for label, manifest in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(xc.FormatError):
                    self.load(manifest)
        with self.assertRaises(xc.FormatError):
            xc.load_manifest(Path("/nonexistent/cursors.json"))

    def test_pixel_sizes_and_hotspots(self):
        manifest = xc.load_manifest(support.MANIFEST_FILE)
        self.assertEqual({size: xc.pixels_for(manifest, size) for size in support.SIZES}, support.PIXELS)
        self.assertEqual([xc.hotspot_for(manifest, (2.5, 2.5), p) for p in (32, 43, 64, 85, 128)],
                         [(2, 2), (3, 3), (5, 5), (6, 6), (10, 10)])
        self.assertEqual([xc.hotspot_for(manifest, (15.5, 15.5), p) for p in (32, 64, 128)],
                         [(15, 15), (31, 31), (62, 62)])
        self.assertEqual(xc.hotspot_for(manifest, (31.99, 0), 43), (42, 0))

    def test_plan(self):
        lines = list(xc.plan(small_manifest()))
        self.assertEqual(lines, [(32, "default.svg", "default.24.png"), (64, "default.svg", "default.48.png"),
                                 (32, "wait-01.svg", "wait-01.24.png"), (64, "wait-01.svg", "wait-01.48.png"),
                                 (32, "wait-02.svg", "wait-02.24.png"), (64, "wait-02.svg", "wait-02.48.png")])
        real = list(xc.plan(xc.load_manifest(support.MANIFEST_FILE)))
        self.assertEqual(len(real), len({png for _, _, png in real}))
        self.assertEqual({pixels for pixels, _, _ in real}, set(support.PIXELS.values()))


class AssembleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pngs = self.root / "png"
        self.pngs.mkdir()
        self.manifest = small_manifest()
        for pixels, svg, png in xc.plan(self.manifest):
            colour = (255, 255, 255, 255) if svg.startswith("default") else (100, 67, 230, 128 + int(svg[5:7]))
            (self.pngs / png).write_bytes(make_png(pixels, pixels, solid(pixels, pixels, colour), filters=(1, 2)))

    def test_writes_files_and_links(self):
        out = self.root / "cursors"
        self.assertEqual(xc.assemble(self.manifest, self.pngs, out), 5)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["arrow", "default", "left_ptr", "wait", "watch"])
        self.assertEqual(os.readlink(out / "left_ptr"), "default")
        self.assertEqual(os.readlink(out / "watch"), "wait")
        self.assertEqual(stat.S_IMODE((out / "default").stat().st_mode), 0o644)

        arrow = xc.decode((out / "default").read_bytes())
        self.assertEqual([(i.size, i.width, i.height, i.xhot, i.yhot, i.delay) for i in arrow],
                         [(24, 32, 32, 2, 2, 0), (48, 64, 64, 5, 5, 0)])
        self.assertEqual(arrow[0].pixels, b"\xff" * (4 * 32 * 32))

        wait = xc.decode((out / "wait").read_bytes())
        self.assertEqual([(i.size, i.delay, i.xhot) for i in wait], [(24, 40, 16), (24, 40, 16), (48, 40, 32), (48, 40, 32)])
        # Frame order within a size, and premultiplied colours: (100, 67, 230) at alpha 129 and 130.
        self.assertEqual(wait[0].pixels[:4], bytes((116, 34, 51, 129)))
        self.assertEqual(wait[1].pixels[:4], bytes((117, 34, 51, 130)))

    def test_replaces_what_is_there(self):
        out = self.root / "cursors"
        out.mkdir()
        (out / "default").symlink_to("nowhere")            # a dangling link where a file belongs
        (out / "left_ptr").write_bytes(b"old file")        # a file where a link belongs
        (out / "watch").symlink_to("default")              # a link to the wrong shape
        xc.assemble(self.manifest, self.pngs, out)
        first = {p.name: (os.readlink(p) if p.is_symlink() else p.read_bytes()) for p in out.iterdir()}
        self.assertFalse((out / "default").is_symlink())
        self.assertEqual(first["left_ptr"], "default")
        self.assertEqual(first["watch"], "wait")
        xc.assemble(self.manifest, self.pngs, out)
        second = {p.name: (os.readlink(p) if p.is_symlink() else p.read_bytes()) for p in out.iterdir()}
        self.assertEqual(first, second)

    def test_refuses_missing_or_wrong_pngs(self):
        (self.pngs / "wait-02.48.png").unlink()
        with self.assertRaises(xc.FormatError):
            xc.assemble(self.manifest, self.pngs, self.root / "a")
        (self.pngs / "wait-02.48.png").write_bytes(make_png(60, 64, solid(60, 64, (0, 0, 0, 255))))
        with self.assertRaises(xc.FormatError) as caught:
            xc.assemble(self.manifest, self.pngs, self.root / "b")
        self.assertIn("expected 64x64", str(caught.exception))

    def test_command_line(self):
        manifest = self.root / "cursors.json"
        manifest.write_text(json.dumps(self.manifest), encoding="utf-8")

        def run(*args):
            return subprocess.run([sys.executable, "-B", str(support.XCURSOR_TOOL), *args],
                                  capture_output=True, text=True)

        plan = run("plan", str(manifest))
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(plan.stdout.splitlines()[0], "32 default.svg default.24.png")
        self.assertEqual(len(plan.stdout.splitlines()), 6)

        out = self.root / "out"
        built = run("assemble", str(manifest), str(self.pngs), str(out))
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual(built.stdout, "")

        info = run("info", str(out / "wait"), str(out / "arrow"))
        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertIn("wait: size 48, 64x64, hotspot 32,32, delay 40 ms", info.stdout)
        self.assertIn("arrow: size 24, 32x32, hotspot 2,2, delay 0 ms", info.stdout)

        garbage = self.root / "garbage"
        garbage.write_bytes(b"not a cursor")
        self.assertEqual(run("info", str(garbage)).returncode, 1)
        (self.pngs / "default.24.png").write_bytes(b"not a png")
        broken = run("assemble", str(manifest), str(self.pngs), str(self.root / "out2"))
        self.assertEqual(broken.returncode, 1)
        self.assertIn("default.24.png", broken.stderr)
        for usage in ((), ("plan",), ("assemble", str(manifest)), ("frobnicate", "x")):
            self.assertEqual(run(*usage).returncode, 2, usage)


def sample_pictures():
    """Three small pictures with every kind of alpha, as (pixels, RGBA samples)."""
    out = []
    for pixels, seed in ((8, 11), (11, 12), (16, 13)):
        samples = bytearray(noise(pixels * pixels * 4, seed))
        for i in range(0, 4 * pixels, 4):
            samples[i + 3] = 0                  # a transparent row
            samples[4 * pixels + i + 3] = 255   # an opaque row
        out.append((pixels, bytes(samples)))
    return out


@unittest.skipUnless(shutil.which("xcursorgen"), "xcursorgen is not installed")
class XcursorgenCrossCheckTest(unittest.TestCase):
    """The reference tool writes the same bytes for the same pictures."""

    def test_same_bytes_as_xcursorgen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images, config = [], []
            for size, (pixels, samples) in zip((24, 32, 48), sample_pictures()):
                for frame, delay in enumerate((40, 50)):
                    name = f"p{size}-{frame}.png"
                    rotated = samples[4 * frame:] + samples[:4 * frame]
                    (root / name).write_bytes(make_png(pixels, pixels, rotated, filters=(4, 1, 2, 3, 0)))
                    config.append(f"{size} {pixels // 2} {pixels // 3} {name} {delay}")
                    images.append(xc.Image(size, pixels, pixels, pixels // 2, pixels // 3, delay,
                                           xc.premultiply(rotated)))
            (root / "cursor.cfg").write_text("\n".join(config) + "\n", encoding="utf-8")
            result = subprocess.run(["xcursorgen", "cursor.cfg", "reference"], cwd=root,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(xc.encode(images), (root / "reference").read_bytes())


@unittest.skipUnless(support.libxcursor(), "libXcursor is not installed")
class LibXcursorCrossCheckTest(unittest.TestCase):
    """The C library reads back what the writer wrote."""

    def test_libxcursor_reads_the_writer_s_files(self):
        lib = support.libxcursor()
        images = []
        for size, (pixels, samples) in zip((24, 32, 48), sample_pictures()):
            for delay in (40, 50):
                images.append(xc.Image(size, pixels, pixels, 1, 2, delay, xc.premultiply(samples)))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cursor"
            path.write_bytes(xc.encode(images))
            for wanted, size in ((24, 24), (30, 32), (64, 48)):
                got = support.copy_images(lib, lib.XcursorFilenameLoadImages(os.fsencode(path), wanted))
                expected = [(i.size, i.width, i.height, i.xhot, i.yhot, i.delay, i.pixels)
                            for i in images if i.size == size]
                with self.subTest(wanted=wanted):
                    self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
