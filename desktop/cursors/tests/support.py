"""Shared helpers for the cursor theme tests (standard library only)."""

from __future__ import annotations

import atexit
import ctypes
import ctypes.util
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CURSORS_DIR = Path(__file__).resolve().parent.parent
REPO = CURSORS_DIR.parent.parent
THEME = "Boswas-Cursors"
SOURCE_DIR = CURSORS_DIR / THEME
MANIFEST_FILE = SOURCE_DIR / "cursors.json"
GENERATOR = CURSORS_DIR / "tools" / "build_cursors.py"
XCURSOR_TOOL = CURSORS_DIR / "tools" / "xcursor.py"
INSTALL = CURSORS_DIR / "install.sh"
TOKENS_FILE = REPO / "desktop" / "ui" / "tokens.json"

SIZES = (24, 32, 48, 64, 96)
# Pixels of the image of each nominal size: the grid has 32 units for the nominal size 24.
PIXELS = {24: 32, 32: 43, 48: 64, 64: 85, 96: 128}

# The names the compositor asks for (crate cursor-icon, CSS "cursor" property).
CSS_NAMES = (
    "default", "context-menu", "help", "pointer", "progress", "wait", "cell", "crosshair", "text",
    "vertical-text", "alias", "copy", "move", "no-drop", "not-allowed", "grab", "grabbing",
    "e-resize", "n-resize", "ne-resize", "nw-resize", "s-resize", "se-resize", "sw-resize",
    "w-resize", "ew-resize", "ns-resize", "nesw-resize", "nwse-resize", "col-resize", "row-resize",
    "all-scroll", "zoom-in", "zoom-out",
)

# Names of the X11 cursor font and of toolkits (Xwayland, GTK and Qt clients).
LEGACY_NAMES = (
    "left_ptr", "arrow", "top_left_arrow", "hand1", "hand2", "pointing_hand", "xterm", "ibeam",
    "watch", "left_ptr_watch", "half-busy", "question_arrow", "whats_this", "cross", "tcross",
    "fleur", "size_all", "crossed_circle", "forbidden", "circle", "dnd-move", "dnd-copy",
    "dnd-link", "dnd-none", "dnd-no-drop", "openhand", "closedhand", "fist", "sb_h_double_arrow",
    "sb_v_double_arrow", "h_double_arrow", "v_double_arrow", "size_hor", "size_ver", "size_bdiag",
    "size_fdiag", "split_h", "split_v", "top_left_corner", "top_right_corner",
    "bottom_left_corner", "bottom_right_corner", "top_side", "bottom_side", "left_side",
    "right_side", "plus", "pencil",
)

# What Qt 6 asks for, in this order, per cursor shape (the name lists of its
# xcb and Wayland platform plugins), and the shape that must answer. The rows
# leave out the few names to which this theme gives another shape; Qt never
# gets that far, because an earlier name of the row exists (README, "Names
# with two meanings").
QT_NAMES = (
    (("left_ptr", "default", "top_left_arrow", "left_arrow"), "default"),
    (("up_arrow",), "up-arrow"),
    (("cross",), "crosshair"),
    (("wait", "watch", "0426c94ea35c87780ff01dc239897213"), "wait"),
    (("ibeam", "text", "xterm"), "text"),
    (("size_ver", "ns-resize", "v_double_arrow", "00008160000006810000408080010102"), "ns-resize"),
    (("size_hor", "ew-resize", "h_double_arrow", "028006030e0e7ebffc7f7070c0600140"), "ew-resize"),
    (("size_bdiag", "nesw-resize", "50585d75b494802d0151028115016902",
      "fcf1c3c7cd4491d801f1e1c78f100000"), "nesw-resize"),
    (("size_fdiag", "nwse-resize", "38c5dff7c7b8962045400281044508d2",
      "c7088f0f3e6c8088236ef8e1e3e70000"), "nwse-resize"),
    (("size_all",), "move"),
    (("split_v", "row-resize", "2870a09082c103050810ffdffffe0204",
      "c07385c7190e701020ff7ffffd08103c"), "row-resize"),
    (("split_h", "col-resize", "043a9f68147c53184671403ffa811cc5",
      "14fef782d02440884392942c11205230"), "col-resize"),
    (("pointing_hand", "pointer", "hand1", "e29285e634086352946a0e7090d73106"), "pointer"),
    (("forbidden", "not-allowed", "crossed_circle", "circle",
      "03b6e0fcb3499374a867c041f52298f0"), "not-allowed"),
    (("whats_this", "help", "question_arrow", "5c6cd98b3f3ebcb1f9c7f1c204630408",
      "d9ce0ab605698f320427677b458ad60b"), "help"),
    (("left_ptr_watch", "half-busy", "progress", "00000000000000020006000e7e9ffc3f",
      "08e8e1c95fe2fc01f976f1e063a24ccd"), "progress"),
    (("openhand", "grab", "5aca4d189052212118709018842178c0"), "grab"),
    (("closedhand", "grabbing", "208530c400c041818281048008011002"), "grabbing"),
    (("dnd-copy", "copy"), "copy"),
    (("dnd-link", "link"), "alias"),
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def xcursor():
    """tools/xcursor.py as a module."""
    return sys.modules.get("boswas_xcursor") or load_module("boswas_xcursor", XCURSOR_TOOL)


def generator():
    """tools/build_cursors.py as a module."""
    return sys.modules.get("boswas_build_cursors") or load_module("boswas_build_cursors", GENERATOR)


def manifest() -> dict:
    with MANIFEST_FILE.open(encoding="utf-8") as handle:
        return json.load(handle)


def shapes() -> dict:
    """Shape name -> manifest entry."""
    return {cursor["name"]: cursor for cursor in manifest()["cursors"]}


def targets() -> dict:
    """Every cursor name -> the shape it shows."""
    out = {}
    for cursor in manifest()["cursors"]:
        out[cursor["name"]] = cursor["name"]
        for alias in cursor["aliases"]:
            out[alias] = cursor["name"]
    return out


def token_colour(ref: str) -> str:
    """Resolve "white", "neutral.950" or "semantic.light.accent" against desktop/ui/tokens.json."""
    tokens = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    if ref.startswith("semantic."):
        _, theme, role = ref.split(".", 2)
        ref = tokens["semantic"][theme][role]
    scale, _, shade = ref.partition(".")
    value = tokens["color"][scale]
    return (value[shade] if shade else value).upper()


HAVE_RSVG = shutil.which("rsvg-convert") is not None
NEEDS_RSVG = "needs rsvg-convert (librsvg2-bin)"


def run_install(dest) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(INSTALL), str(dest)], capture_output=True, text=True)


_INSTALLED: list = []


def installed() -> Path:
    """The theme directory of one install.sh run, shared by all tests of the process."""
    if not _INSTALLED:
        dest = tempfile.mkdtemp(prefix="boswas-cursors-test-")
        atexit.register(shutil.rmtree, dest, True)
        result = run_install(dest)
        if result.returncode != 0:
            raise AssertionError(f"install.sh failed: {result.stderr}")
        _INSTALLED.append(Path(dest) / "usr/share/icons" / THEME)
    return _INSTALLED[0]


def snapshot(theme: Path) -> dict:
    """Relative path -> ("link", target) or ("file", mode, bytes) for everything below a theme."""
    out = {}
    for path in sorted(theme.rglob("*")):
        rel = path.relative_to(theme).as_posix()
        if path.is_symlink():
            out[rel] = ("link", str(path.readlink()))
        elif path.is_dir():
            out[rel] = ("dir", path.stat().st_mode & 0o7777)
        else:
            out[rel] = ("file", path.stat().st_mode & 0o7777, path.read_bytes())
    return out


# ---------------------------------------------------------------------------
# libXcursor, the C library X11 clients read cursor files with (optional)
# ---------------------------------------------------------------------------
class XcursorImage(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("size", ctypes.c_uint32), ("width", ctypes.c_uint32),
                ("height", ctypes.c_uint32), ("xhot", ctypes.c_uint32), ("yhot", ctypes.c_uint32),
                ("delay", ctypes.c_uint32), ("pixels", ctypes.POINTER(ctypes.c_uint32))]


class XcursorImages(ctypes.Structure):
    _fields_ = [("nimage", ctypes.c_int), ("images", ctypes.POINTER(ctypes.POINTER(XcursorImage))),
                ("name", ctypes.c_char_p)]


def libxcursor():
    """libXcursor through ctypes, or None when it is not installed."""
    name = ctypes.util.find_library("Xcursor")
    for candidate in (name, "libXcursor.so.1"):
        if not candidate:
            continue
        try:
            lib = ctypes.CDLL(candidate)
        except OSError:
            continue
        for function in ("XcursorFilenameLoadImages", "XcursorLibraryLoadImages"):
            getattr(lib, function).restype = ctypes.POINTER(XcursorImages)
        lib.XcursorFilenameLoadImages.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.XcursorLibraryLoadImages.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        lib.XcursorImagesDestroy.argtypes = [ctypes.POINTER(XcursorImages)]
        lib.XcursorImagesDestroy.restype = None
        return lib
    return None


def copy_images(lib, pointer) -> list:
    """(size, width, height, xhot, yhot, delay, pixel bytes) of a libXcursor result, which is then freed."""
    if not pointer:
        return []
    out = []
    try:
        for index in range(pointer.contents.nimage):
            image = pointer.contents.images[index].contents
            data = ctypes.string_at(image.pixels, 4 * image.width * image.height)
            if sys.byteorder == "big":
                data = b"".join(data[i:i + 4][::-1] for i in range(0, len(data), 4))
            out.append((image.size, image.width, image.height, image.xhot, image.yhot, image.delay, data))
    finally:
        lib.XcursorImagesDestroy(pointer)
    return out
