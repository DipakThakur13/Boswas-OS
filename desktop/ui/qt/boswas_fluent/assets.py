"""Images the style sheet needs: chevrons, check marks, radio dots, selection pills.

Nothing is shipped as a bitmap. Each image is a small SVG generated here
from token colours (``sources``, standard library only). Qt style sheets
take images from files, so ``ensure`` writes the SVGs to the per-user cache
(``$XDG_CACHE_HOME/boswas-fluent/assets/<digest>/``) and renders each one
to ``name.png`` and ``name@2x.png`` with QtSvg, which the style sheet then
names; Qt picks the @2x file on a high-density screen. The directory name
is a digest of the sources, so a token change or the other scheme gets a
directory of its own and a complete directory is never written twice.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .tokens import Theme

# Bumped when the drawing code changes, so stale renderings are not reused.
ASSET_VERSION = "3"

_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        '{body}</svg>\n')

# Line glyphs on a 12x12 grid: (path data, stroke width).
_LINES = {
    "chevron-down": ("M2.5 4.25L6 7.75L9.5 4.25", 1.2),
    "chevron-up": ("M2.5 7.75L6 4.25L9.5 7.75", 1.2),
    "chevron-right": ("M4.25 2.5L7.75 6L4.25 9.5", 1.2),
    "chevron-left": ("M7.75 2.5L4.25 6L7.75 9.5", 1.2),
    "check": ("M2.25 6.25L4.75 8.75L9.75 3.5", 1.4),
    "dash": ("M2.75 6H9.25", 1.4),
    "close": ("M2.75 2.75L9.25 9.25M9.25 2.75L2.75 9.25", 1.2),
    "restore": ("M3 4.5H7.5V9H3ZM4.5 4.5V3H9V7.5H7.5", 1.0),
}
# Filled glyphs on a 12x12 grid.
_FILLS = {
    "more": '<circle cx="2.5" cy="6" r="1"/><circle cx="6" cy="6" r="1"/><circle cx="9.5" cy="6" r="1"/>',
    "grip": '<circle cx="4.5" cy="3" r="0.8"/><circle cx="7.5" cy="3" r="0.8"/><circle cx="4.5" cy="6" r="0.8"/>'
            '<circle cx="7.5" cy="6" r="0.8"/><circle cx="4.5" cy="9" r="0.8"/><circle cx="7.5" cy="9" r="0.8"/>',
}
GLYPHS = tuple(_LINES) + tuple(_FILLS)


def _paint(theme: Theme, colour: str) -> tuple[str, float]:
    return theme.rgba(colour)


def glyph_svg(theme: Theme, glyph: str, colour: str = "text-secondary", size: int = 12) -> str:
    """One glyph (see ``GLYPHS``) as SVG text in a token colour (any name ``Theme.rgba`` accepts)."""
    rgb, alpha = _paint(theme, colour)
    scale = size / 12
    if glyph in _LINES:
        path, width = _LINES[glyph]
        body = (f'<path d="{path}" fill="none" stroke="{rgb}" stroke-opacity="{alpha:g}" stroke-width="{width:g}" '
                f'stroke-linecap="round" stroke-linejoin="round"/>')
    elif glyph in _FILLS:
        body = f'<g fill="{rgb}" fill-opacity="{alpha:g}">{_FILLS[glyph]}</g>'
    else:
        raise KeyError(glyph)
    if scale != 1:
        body = f'<g transform="scale({scale:g})">{body}</g>'
    return _SVG.format(w=size, h=size, body=body)


def _dot(theme: Theme, colour: str, canvas: int, diameter: float) -> str:
    rgb, alpha = _paint(theme, colour)
    c = canvas / 2
    return _SVG.format(w=canvas, h=canvas, body=f'<circle cx="{c:g}" cy="{c:g}" r="{diameter / 2:g}" fill="{rgb}" '
                                                 f'fill-opacity="{alpha:g}"/>')


def _pill(theme: Theme, colour: str, width: int, height: int) -> str:
    rgb, alpha = _paint(theme, colour)
    radius = min(width, height) / 2
    return _SVG.format(w=width, h=height, body=f'<rect width="{width}" height="{height}" rx="{radius:g}" '
                                               f'fill="{rgb}" fill-opacity="{alpha:g}"/>')


def sources(theme: Theme) -> dict[str, str]:
    """Every image of the style sheet for ``theme``: name -> SVG text."""
    images: dict[str, str] = {}
    for glyph in ("chevron-down", "chevron-up", "chevron-right", "chevron-left", "close", "restore", "more", "grip"):
        images[glyph] = glyph_svg(theme, glyph, "text-secondary")
        images[f"{glyph}-hover"] = glyph_svg(theme, glyph, "text")
        images[f"{glyph}-disabled"] = glyph_svg(theme, glyph, "text-disabled")
    for glyph in ("chevron-down", "chevron-up"):
        images[f"{glyph}-small"] = glyph_svg(theme, glyph, "text-secondary", 8)
        images[f"{glyph}-small-disabled"] = glyph_svg(theme, glyph, "text-disabled", 8)
    for glyph in ("check", "dash"):
        images[glyph] = glyph_svg(theme, glyph, "text")
        images[f"{glyph}-disabled"] = glyph_svg(theme, glyph, "text-disabled")
        images[f"{glyph}-on-accent"] = glyph_svg(theme, glyph, "on-accent")
    # The dot of a checked radio button (on the accent fill) and of a radio menu item.
    images["radio-dot"] = _dot(theme, "on-accent", 18, 8)
    images["radio-dot-hover"] = _dot(theme, "on-accent", 18, 10)
    images["radio-dot-pressed"] = _dot(theme, "on-accent", 18, 6)
    images["menu-dot"] = _dot(theme, "text", 12, 6)
    images["menu-dot-disabled"] = _dot(theme, "text-disabled", 12, 6)
    # The accent dot inside a slider thumb.
    images["thumb-dot"] = _dot(theme, "accent", 18, 10)
    images["thumb-dot-hover"] = _dot(theme, "accent", 18, 12)
    images["thumb-dot-pressed"] = _dot(theme, "accent-pressed", 18, 8)
    images["thumb-dot-disabled"] = _dot(theme, "text-disabled", 18, 10)
    # Selection indicators: beside a selected row, under a selected tab.
    images["pill-vertical"] = _pill(theme, "accent", 3, 16)
    images["pill-horizontal"] = _pill(theme, "accent", 16, 3)
    images["pill-vertical-disabled"] = _pill(theme, "text-disabled", 3, 16)
    return images


def digest(theme: Theme) -> str:
    """A short digest of the images of ``theme`` (the name of their cache directory)."""
    hasher = hashlib.sha256(ASSET_VERSION.encode())
    for name, text in sorted(sources(theme).items()):
        hasher.update(name.encode() + b"\0" + text.encode() + b"\0")
    return hasher.hexdigest()[:20]


# --- files (needs Qt) -----------------------------------------------------------------------------------

_fallback_dir: tempfile.TemporaryDirectory | None = None
_ensured: dict[str, Path] = {}
_root: Path | None = None


def set_cache_root(path: str | os.PathLike | None) -> None:
    """Keep the images under ``path`` instead of the user's cache (None: the user's cache again).

    For test runs: the style sheet names the image files, so they must stay
    where they are for as long as the application is styled.
    """
    global _root
    _root = Path(path) if path is not None else None
    _ensured.clear()


def cache_root() -> Path:
    """``$XDG_CACHE_HOME/boswas-fluent`` (``~/.cache/boswas-fluent``), unless ``set_cache_root`` named another."""
    if _root is not None:
        return _root
    base = os.environ.get("XDG_CACHE_HOME") or ""
    root = Path(base) if base.startswith("/") else Path.home() / ".cache"
    return root / "boswas-fluent"


def _writable_dir(theme: Theme) -> Path:
    global _fallback_dir
    target = cache_root() / "assets" / digest(theme)
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.access(target, os.W_OK | os.X_OK):
            return target
    except OSError:
        pass
    # No usable cache (read-only home, build chroot): a private temporary directory for this process.
    if _fallback_dir is None:
        _fallback_dir = tempfile.TemporaryDirectory(prefix="boswas-fluent-")
    target = Path(_fallback_dir.name) / digest(theme)
    target.mkdir(mode=0o700, exist_ok=True)
    return target


def _write(path: Path, data: bytes) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def render_png(svg: str, scale: int = 1) -> bytes:
    """Render SVG text to PNG bytes with QtSvg at ``scale`` times its size."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise ValueError("generated SVG does not parse")
    size = renderer.defaultSize() * scale
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def ensure(theme: Theme) -> Path:
    """Write the images of ``theme`` if they are not there yet; returns their directory."""
    key = digest(theme)
    known = _ensured.get(key)
    if known is not None and (known / ".complete").is_file():
        return known
    directory = _writable_dir(theme)
    if not (directory / ".complete").is_file():
        for name, svg in sources(theme).items():
            _write(directory / f"{name}.svg", svg.encode("utf-8"))
            _write(directory / f"{name}.png", render_png(svg, 1))
            _write(directory / f"{name}@2x.png", render_png(svg, 2))
        _write(directory / ".complete", b"")
    _ensured[key] = directory
    return directory
