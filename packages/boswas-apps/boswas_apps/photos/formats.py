"""Image file types: by extension, and by the first bytes of the file (standard library only).

The extension decides what the gallery lists; the content decides how a
file is opened, so a JPEG named ``.png`` still opens, and a file that is
no picture at all gets a message that says so.
"""

from __future__ import annotations

import os

# extension -> (format key, MIME type). The format key is the name Qt's image plugins use.
EXTENSIONS: dict[str, tuple[str, str]] = {
    "png": ("png", "image/png"),
    "jpg": ("jpeg", "image/jpeg"), "jpeg": ("jpeg", "image/jpeg"), "jpe": ("jpeg", "image/jpeg"),
    "jfif": ("jpeg", "image/jpeg"),
    "gif": ("gif", "image/gif"),
    "bmp": ("bmp", "image/bmp"), "dib": ("bmp", "image/bmp"),
    "webp": ("webp", "image/webp"),
    "svg": ("svg", "image/svg+xml"), "svgz": ("svgz", "image/svg+xml-compressed"),
    "tif": ("tiff", "image/tiff"), "tiff": ("tiff", "image/tiff"),
    "ico": ("ico", "image/x-icon"), "cur": ("cur", "image/x-win-bitmap"),
    "avif": ("avif", "image/avif"),
    "heic": ("heic", "image/heif"), "heif": ("heif", "image/heif"),
    "jxl": ("jxl", "image/jxl"),
    "tga": ("tga", "image/x-tga"),
    "pbm": ("pbm", "image/x-portable-bitmap"), "pgm": ("pgm", "image/x-portable-graymap"),
    "ppm": ("ppm", "image/x-portable-pixmap"),
    "xbm": ("xbm", "image/x-xbitmap"), "xpm": ("xpm", "image/x-xpixmap"),
    "wbmp": ("wbmp", "image/vnd.wap.wbmp"), "icns": ("icns", "image/x-icns"),
    "jp2": ("jp2", "image/jp2"), "qoi": ("qoi", "image/qoi"),
}

# What people call a format, for messages.
NAMES = {
    "png": "PNG", "jpeg": "JPEG", "gif": "GIF", "bmp": "BMP", "webp": "WebP", "svg": "SVG", "svgz": "SVG",
    "tiff": "TIFF", "ico": "ICO", "cur": "CUR", "avif": "AVIF", "heic": "HEIF", "heif": "HEIF", "jxl": "JPEG XL",
    "tga": "TGA", "pbm": "PBM", "pgm": "PGM", "ppm": "PPM", "xbm": "XBM", "xpm": "XPM", "wbmp": "WBMP",
    "icns": "ICNS", "jp2": "JPEG 2000", "qoi": "QOI",
}

# Which Debian package brings the Qt image plugin of a format this build may lack.
PLUGIN_PACKAGES = {
    "webp": "qt6-image-formats-plugins", "tiff": "qt6-image-formats-plugins", "tga": "qt6-image-formats-plugins",
    "icns": "qt6-image-formats-plugins", "wbmp": "qt6-image-formats-plugins", "jp2": "qt6-image-formats-plugins",
    "avif": "kimageformat6-plugins", "heic": "kimageformat6-plugins", "heif": "kimageformat6-plugins",
    "jxl": "kimageformat6-plugins", "qoi": "kimageformat6-plugins",
}

SNIFF_BYTES = 4096
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}


def extension(path: str | os.PathLike) -> str:
    """The extension of a path in lower case, without the dot (``""`` when there is none)."""
    name = os.path.basename(os.fspath(path))
    dot = name.rfind(".")
    return name[dot + 1:].lower() if dot > 0 else ""


def format_for_extension(ext: str) -> str | None:
    entry = EXTENSIONS.get(ext.lower().lstrip("."))
    return entry[0] if entry else None


def mime_for_extension(ext: str) -> str | None:
    entry = EXTENSIONS.get(ext.lower().lstrip("."))
    return entry[1] if entry else None


def display_name(fmt: str | None) -> str:
    return NAMES.get(fmt or "", (fmt or "").upper())


def sniff(header: bytes) -> str | None:
    """The format the first bytes of a file say it is, or None when they say nothing we know."""
    if len(header) >= 8 and header[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if header[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if header[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if header[:4] == b"qoif":
        return "qoi"
    if header[:2] == b"\xff\x0a" or header[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return "jxl"
    if header[4:8] == b"ftyp" and len(header) >= 12:
        brands = {header[8:12]}
        # compatible brands follow the minor version, four bytes each, up to the size of the box
        size = int.from_bytes(header[:4], "big")
        for offset in range(16, min(size, len(header), 64) - 3, 4):
            brands.add(header[offset:offset + 4])
        if brands & _AVIF_BRANDS:
            return "avif"
        if brands & _HEIF_BRANDS:
            return "heif"
        return None
    if header[:2] == b"BM" and len(header) >= 14:
        return "bmp"
    if header[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if header[:4] == b"\x00\x00\x02\x00":
        return "cur"
    if header[:2] == b"\x1f\x8b":
        return "svgz"                         # a gzip stream: only an SVG is plausible among pictures
    if _looks_like_svg(header):
        return "svg"
    return None


def _looks_like_svg(header: bytes) -> bool:
    text = header.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not text.startswith(b"<"):
        return False
    lowered = text[:SNIFF_BYTES].lower()
    return b"<svg" in lowered


def sniff_file(path: str | os.PathLike) -> str | None:
    """``sniff`` of the beginning of a file; OSError when it cannot be read."""
    with open(path, "rb") as handle:
        return sniff(handle.read(SNIFF_BYTES))
