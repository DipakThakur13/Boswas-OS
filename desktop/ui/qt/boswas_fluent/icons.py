"""Icons by freedesktop name, from the Boswas Prism pack, in the colours of the theme.

``icon("go-next")`` looks the name up in the Boswas Prism icon themes
(``desktop/icons`` of the source tree when the toolkit runs from it, then
``$XDG_DATA_HOME/icons``, the ``icons`` directories of ``$XDG_DATA_DIRS``),
then among the icons programs install for themselves in ``hicolor``, then
asks Qt's icon theme (Breeze), and returns an empty QIcon when nobody has
it. That is the order Boswas Prism's ``Inherits=hicolor,breeze...`` asks
for: another program shows its own icon, not Breeze's drawing of it. Qt's
own loader searches ``hicolor`` last whatever ``Inherits`` says, so the
toolkit looks there itself.

Symbolic icons (``*-symbolic``: the one-colour line icons of the
``actions`` and ``status`` directories) are recoloured: whatever colour the
file was drawn in, they are painted in the theme's text colour, in the
disabled text colour for a disabled widget, or in the token colour given
with ``color=``. The recolouring happens when the icon is painted, so an
icon made once follows a scheme change. A name without the suffix finds
the symbolic icon when no coloured icon has that name (``"go-next"`` finds
``go-next-symbolic``).

Rendering uses QtSvg directly and does not need Qt's SVG image plugin.
``glyph_icon`` gives the toolkit's own drawn glyphs (chevrons, close, check,
more) the same treatment.
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QByteArray, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QIcon, QIconEngine, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import assets
from . import theming as _theme
from .tokens import Theme

THEME_DIRS = ("Boswas-Prism", "Boswas-Prism-Light")
#: Where programs install their own icons (the icon theme specification's fallback theme).
HICOLOR = "hicolor"
#: Fixed sizes looked for in hicolor, largest first.
HICOLOR_SIZES = (512, 256, 192, 128, 96, 64, 48, 32, 24, 22, 16)
CONTEXTS = ("actions", "status", "apps", "places", "devices", "mimetypes")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MAX_SVG_BYTES = 2 * 1024 * 1024
_DISABLED_OPACITY = 0.4

_paths: dict[str, Path | None] = {}
_hicolor: dict[str, list[tuple[int, Path]]] = {}
_icons: dict[tuple, QIcon] = {}
_missing: set[str] = set()
_pixmaps: OrderedDict[tuple, QPixmap] = OrderedDict()
PIXMAP_CACHE = 256            # rendered icon pixmaps kept by cached_pixmap


def icon_roots() -> list[Path]:
    """The directories searched for the Boswas Prism themes, in order."""
    roots = []
    checkout = Path(__file__).resolve().parents[3] / "icons"
    if os.path.isfile(checkout / THEME_DIRS[0] / "index.theme"):
        roots.append(checkout)
    data_home = os.environ.get("XDG_DATA_HOME") or ""
    roots.append((Path(data_home) if data_home.startswith("/") else Path.home() / ".local/share") / "icons")
    for directory in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        if directory.startswith("/"):
            roots.append(Path(directory) / "icons")
    return roots


def icon_path(name: str) -> Path | None:
    """The SVG file of ``name`` in the Boswas Prism pack, or None."""
    if not isinstance(name, str) or not _NAME.match(name):
        return None
    if name in _paths:
        return _paths[name]
    found = None
    for root in icon_roots():
        for theme_dir in THEME_DIRS:
            for context in CONTEXTS:
                candidate = root / theme_dir / "scalable" / context / f"{name}.svg"
                try:
                    exists = candidate.is_file()
                except OSError:
                    # A directory of XDG_DATA_DIRS that may not be read: it
                    # has no icon for us.
                    exists = False
                if exists:
                    found = candidate
                    break
            if found:
                break
        if found:
            break
    _paths[name] = found
    return found


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def hicolor_files(name: str) -> list[tuple[int, Path]]:
    """The files a program installed for the icon ``name`` in hicolor: (size, path), size 0 for scalable.

    Only the first directory of :func:`icon_roots` that has the icon counts,
    as in an icon theme lookup; applications' icons only (``apps``).
    """
    if not isinstance(name, str) or not _NAME.match(name) or name.endswith("-symbolic"):
        return []
    if name in _hicolor:
        return _hicolor[name]
    found: list[tuple[int, Path]] = []
    for root in icon_roots():
        base = root / HICOLOR
        scalable = base / "scalable" / "apps" / f"{name}.svg"
        if _is_file(scalable):
            found.append((0, scalable))
        for size in HICOLOR_SIZES:
            candidate = base / f"{size}x{size}" / "apps" / f"{name}.png"
            if _is_file(candidate):
                found.append((size, candidate))
        if found:
            break
    _hicolor[name] = found
    return found


def _hicolor_icon(name: str) -> QIcon:
    """The icon a program installed for itself in hicolor, or an empty icon."""
    files = hicolor_files(name)
    for size, path in files:
        if size == 0:
            data = _read(path)
            if data is not None:
                return QIcon(SvgIconEngine(data, None))
    result = QIcon()
    for size, path in files:
        if size:
            result.addFile(str(path), QSize(size, size))
    return result


def clear_cache() -> None:
    """Forget what was found (called when the theme is applied again; icons already made stay valid)."""
    _paths.clear()
    _hicolor.clear()
    _icons.clear()
    _missing.clear()
    _pixmaps.clear()


def missing_icons() -> list[str]:
    """Names that were asked for and found neither in Boswas Prism nor in Qt's icon theme."""
    return sorted(_missing)


class SvgIconEngine(QIconEngine):
    """Paints one SVG, optionally recoloured to a token colour that is looked up at paint time."""

    def __init__(self, source: bytes, tint: str | None) -> None:
        super().__init__()
        self._source = source
        self._tint = tint
        self._renderer: QSvgRenderer | None = None
        self._cache: dict[tuple, QPixmap] = {}

    def clone(self) -> "SvgIconEngine":
        return SvgIconEngine(self._source, self._tint)

    def _render(self, size: QSize, mode: QIcon.Mode) -> QPixmap:
        theme = _theme.current_theme()
        disabled = mode == QIcon.Mode.Disabled
        if self._tint is None:
            colour = None
        else:
            colour = _theme.qcolor("text-disabled" if disabled else self._tint, theme)
        key = (size.width(), size.height(), colour.rgba() if colour is not None else disabled)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if self._renderer is None:
            self._renderer = QSvgRenderer(QByteArray(self._source))
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        if self._renderer.isValid() and not size.isEmpty():
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            if colour is None and disabled:
                painter.setOpacity(_DISABLED_OPACITY)
            self._renderer.render(painter)
            if colour is not None:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                painter.fillRect(image.rect(), colour)
            painter.end()
        pixmap = QPixmap.fromImage(image)
        if len(self._cache) > 24:
            self._cache.clear()
        self._cache[key] = pixmap
        return pixmap

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self._render(size, mode)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:
        pixmap = self._render(size * scale, mode)
        pixmap = QPixmap(pixmap)
        pixmap.setDevicePixelRatio(scale)
        return pixmap

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        side = min(rect.width(), rect.height())
        pixmap = self._render(QSize(round(side * ratio), round(side * ratio)), mode)
        target = QRect(QPoint(rect.x() + (rect.width() - side) // 2, rect.y() + (rect.height() - side) // 2),
                       QSize(side, side))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(target, pixmap)
        painter.restore()

    def actualSize(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QSize:
        side = min(size.width(), size.height())
        return QSize(side, side)


def _read(path: Path) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read(_MAX_SVG_BYTES + 1)
    except OSError:
        return None
    return data if len(data) <= _MAX_SVG_BYTES else None


def icon(name: str, *fallbacks: str, color: str | None = None, symbolic: bool | None = None) -> QIcon:
    """The icon ``name`` (a freedesktop icon name), else the first of ``fallbacks`` that exists.

    ``color`` is a token colour for symbolic icons (default ``"text"``).
    ``symbolic=True`` prefers ``name-symbolic`` over a coloured icon of the
    same name; ``symbolic=False`` never substitutes the symbolic variant.
    """
    key = (name, fallbacks, color, symbolic)
    cached = _icons.get(key)
    if cached is not None:
        return cached
    result = QIcon()
    for candidate in (name, *fallbacks):
        result = _lookup(candidate, color, symbolic)
        if not result.isNull():
            break
    else:
        if name:
            _missing.add(name)
    _icons[key] = result
    return result


def _lookup(name: str, color: str | None, symbolic: bool | None) -> QIcon:
    if not name:
        return QIcon()
    names = [name]
    if not name.endswith("-symbolic") and symbolic is not False:
        names = [f"{name}-symbolic", name] if symbolic else [name, f"{name}-symbolic"]
    for candidate in names:
        path = icon_path(candidate)
        if path is None:
            continue
        data = _read(path)
        if data is None:
            continue
        tint = (color or "text") if candidate.endswith("-symbolic") else None
        return QIcon(SvgIconEngine(data, tint))
    for candidate in names:
        own = _hicolor_icon(candidate)
        if not own.isNull():
            return own
    for candidate in names:
        themed = _from_theme(candidate)
        if not themed.isNull():
            return themed
    return QIcon()


def _from_theme(name: str) -> QIcon:
    """The icon ``name`` of Qt's icon theme (Breeze, then hicolor), or an empty icon."""
    return QIcon.fromTheme(name) if QIcon.hasThemeIcon(name) else QIcon()


def has_icon(name: str) -> bool:
    """Whether ``icon(name)`` would find something."""
    return not _lookup(name, None, None).isNull()


def glyph_icon(glyph: str, color: str = "text-secondary") -> QIcon:
    """One of the toolkit's drawn glyphs (``assets.GLYPHS``) as an icon in a token colour."""
    key = ("glyph", glyph, color)
    cached = _icons.get(key)
    if cached is None:
        # Drawn opaque and recoloured when painted, so it follows the scheme like a symbolic icon.
        svg = assets.glyph_svg(_OPAQUE, glyph, "text")
        cached = QIcon(SvgIconEngine(svg.encode("utf-8"), color))
        _icons[key] = cached
    return cached


def pixmap(source: QIcon | str, size: int, *, color: str | None = None, enabled: bool = True,
           ratio: float = 1.0) -> QPixmap:
    """``source`` (an icon or an icon name) rendered at ``size`` logical pixels for the current theme."""
    qicon = icon(source, color=color) if isinstance(source, str) else source
    mode = QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled
    result = qicon.pixmap(QSize(size, size), ratio, mode, QIcon.State.Off)
    return result


def cached_pixmap(qicon: QIcon, size: int, ratio: float = 1.0, enabled: bool = True) -> QPixmap:
    """``qicon`` rendered at ``size`` logical pixels for a screen of device pixel ratio ``ratio``; the last
    PIXMAP_CACHE renderings are kept (an icon that changes size while it animates is rendered once per
    size). The cache is emptied when the theme is applied again."""
    key = (qicon.cacheKey(), size, round(ratio, 3), enabled)
    cached = _pixmaps.get(key)
    if cached is not None:
        _pixmaps.move_to_end(key)
        return cached
    mode = QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled
    rendered = qicon.pixmap(QSize(size, size), ratio, mode, QIcon.State.Off)
    _pixmaps[key] = rendered
    while len(_pixmaps) > PIXMAP_CACHE:
        _pixmaps.popitem(last=False)
    return rendered


class _OpaqueTheme(Theme):
    """A stand-in that draws every glyph opaque (the engine applies the real colour)."""

    def __init__(self) -> None:           # no tokens needed
        self.scheme = "dark"
        self.tokens = {}
        self._cache = {}

    def rgba(self, name: str) -> tuple[str, float]:
        return "black", 1.0             # an SVG colour name; never shown, the engine recolours every pixel


_OPAQUE = _OpaqueTheme()
