"""Boswas glass: a window backdrop made from the desktop background, and translucent surfaces on it.

Boswas glass is the material of the Boswas applications (``material.glass``
in tokens.json, ADR-0039's "mica and acrylic" done in the toolkit):

* **The backdrop.** A window shows the desktop background behind its
  content, as Windows 11 shows Mica: the wallpaper of ``[appearance]
  wallpaper`` (or, without one, the default background the shell draws,
  computed the same way), reduced, its colours saturated by
  ``saturation``, blurred by ``blur`` logical pixels, covered with the
  scheme's tint and a fine grain (``noise``) so that the wide soft areas do
  not band. A Wayland client does not know where its window is, so a
  window shows the part of the picture that a window of its size centred
  on the screen would cover; it does not move with the window and does not
  zoom when the window is resized.
* **Legibility.** The tint opacity of the token file is a minimum: it is
  raised, in steps of 0.02, until every text colour of the theme keeps
  4.5:1 on the darkest and the brightest parts of this backdrop, also under
  the glass of cards and panes and their hover and pressed layers. A busy or
  very bright picture therefore gets more tint, and text and icons stay as
  crisp as on the opaque surfaces.
* **Glass surfaces** lie on the backdrop: ``paint.glass`` draws a pane, a
  card or a flyout as its tint at the token opacity, an inner glow below the
  top edge and a hairline whose top is the white highlight and whose bottom
  is a soft shadow. Menus, combo box lists, tooltips, flyouts and dialogs
  show the backdrop of the window they belong to under their tint
  (acrylic), so they stay readable over any content.
* **Off.** With ``[appearance] transparency = false``, with a high-contrast
  preset (``appearance.HIGH_CONTRAST_PRESETS``), with a token file that has
  no ``material.glass`` or when an application asks (``manager().set_glass``),
  every surface is its opaque tint again, pixel for pixel the look without
  glass.

Costs: nothing is blurred per frame. The picture is decoded once, off the
GUI thread (until it is ready a window is painted opaque); the reduced,
blurred and tinted picture is made once per picture, scheme and accent
(a few milliseconds); the backdrop of a window a little larger than the
window (``BUCKET``), so that resizing does not make one per frame, and the
last ``KEPT_BACKDROPS`` are kept. Painting it is one copy of the part that
is repainted. While a window is resized by hand and outgrows its backdrop,
the new one is made only once the size has stood still for ``RESIZE_PAUSE``
seconds (making it costs 4 to 8 ms at 200 %, at every bucket crossed): until
then the kept backdrop shows, and the edge beyond it is the backdrop's mean
colour. At rest a window looks exactly as before.
"""

from __future__ import annotations

import math
import os
import random
import time
from collections import OrderedDict

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene, QWidget

from .tokens import TEXT_CONTRAST, Theme, blend, contrast, luminance, to_hex

TINY_WIDTH = 96            # the picture is first reduced to this many pixels across (the screen's aspect ratio)
SOFT_WIDTH = 384           # and blurred at this width
PROBE = (64, 36)           # the size at which the backdrop is searched for its darkest and brightest parts
KEPT_BACKDROPS = 3         # window backdrops kept
BUCKET = 160               # a backdrop's sides are the window's rounded up to this (logical pixels)
GRAIN_TILE = 64            # device pixels of the grain pattern, tiled
LEGIBILITY_STEP = 0.02
RESIZE_PAUSE = 0.15        # seconds a size stands still before a window's new backdrop is made
MAX_PICTURE_BYTES = 64 << 20
DEFAULT_SCREEN = QSize(1920, 1080)
# The text colours that stand on glass, held to what the opaque surfaces guarantee (tests/test_contrast.py):
# body text on every surface and state layer, muted and coloured text on every surface, the link colour of a
# hovered or pressed hyperlink on the state layers.
BODY_TEXT = ("text", "text-secondary")
SURFACE_TEXT = ("text-muted", "accent-text", "info-text", "success-text", "warning-text", "danger-text")
LAYER_TEXT = ("accent-text-strong",)
_EXTREMES = 6              # darkest and brightest probe pixels that are checked


# --- whether glass is on ---------------------------------------------------------------------------------------

def enabled() -> bool:
    """Whether windows are drawn with Boswas glass now (see the module text for when they are not)."""
    from .theming import manager
    return manager().glass


# --- the picture -------------------------------------------------------------------------------------------------

def _tiny_size(screen: QSize) -> QSize:
    width, height = max(1, screen.width()), max(1, screen.height())
    return QSize(TINY_WIDTH, max(1, round(TINY_WIDTH * height / width)))


def _cover(image: QImage, size: QSize) -> QImage:
    """``image`` scaled to cover ``size`` (centred, cut at the long side), as RGB32."""
    scaled = image.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
    x, y = (scaled.width() - size.width()) // 2, (scaled.height() - size.height()) // 2
    return scaled.copy(x, y, size.width(), size.height()).convertToFormat(QImage.Format.Format_RGB32)


def read_picture(path: str, screen: QSize) -> QImage:
    """The wallpaper at ``path`` reduced to the tiny size for a screen of ``screen`` (raises OSError).

    Runs off the GUI thread: it touches no widget and nothing shared.
    """
    try:
        if os.path.getsize(path) > MAX_PICTURE_BYTES:
            raise OSError(f"{path}: larger than {MAX_PICTURE_BYTES >> 20} MiB")
    except OSError as exc:
        raise OSError(str(exc)) from None
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    tiny = _tiny_size(screen)
    if size.isValid() and size.width() > tiny.width() * 4 and size.height() > tiny.height() * 4:
        # Decoders that can (JPEG, SVG) decode at a quarter of the work; the rest scale afterwards.
        reader.setScaledSize(size.scaled(tiny * 4, Qt.AspectRatioMode.KeepAspectRatioByExpanding))
    image = reader.read()
    if image.isNull():
        raise OSError(f"{path}: {reader.errorString()}")
    return _cover(image, tiny)


def default_picture(theme: Theme, size: QSize) -> QImage:
    """The default desktop background at ``size``, as boswas-shell draws it (``wallpaper.rs``, draw_default):
    the scheme's background lit by four glows of the Prism palette."""
    width, height = size.width(), size.height()
    unit = max(width, height)
    strength = 1.0 if theme.is_dark else 0.5

    def rgb(name: str) -> tuple[int, int, int]:
        value = theme.color(name)
        return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)

    glows = ((width * 0.16, height * 1.04, unit * 0.66, rgb("iris.600"), 0.62 * strength),
             (width * 0.94, height * 0.64, unit * 0.54, rgb("azure.600"), 0.46 * strength),
             (width * 0.62, height * -0.12, unit * 0.46, rgb("orchid.600"), 0.30 * strength),
             (width * 0.46, height * 0.58, unit * 0.32, rgb("iris.400"), 0.14 * strength))
    base = rgb("background")
    data = bytearray(width * height * 4)
    index = 0
    for y in range(height):
        for x in range(width):
            r, g, b = float(base[0]), float(base[1]), float(base[2])
            for gx, gy, radius, colour, power in glows:
                dx, dy = x + 0.5 - gx, y + 0.5 - gy
                distance = math.sqrt(dx * dx + dy * dy)
                if distance >= radius:
                    continue
                f = 1.0 - distance / radius
                a = power * f * f * (3.0 - 2.0 * f)
                r += (colour[0] - r) * a
                g += (colour[1] - g) * a
                b += (colour[2] - b) * a
            data[index:index + 4] = bytes((round(b), round(g), round(r), 255))      # RGB32 is BGRA in memory
            index += 4
    image = QImage(bytes(data), width, height, width * 4, QImage.Format.Format_RGB32)
    return image.copy()


def saturate(image: QImage, amount: float) -> QImage:
    """``image`` with its colours moved away from grey by ``amount`` (1: unchanged), as RGB32."""
    source = image.convertToFormat(QImage.Format.Format_RGB32)
    if abs(amount - 1.0) < 1e-3:
        return source
    width, height = source.width(), source.height()
    raw = bytearray(bytes(source.constBits())[:source.bytesPerLine() * height])
    stride = source.bytesPerLine()
    for y in range(height):
        row = y * stride
        for x in range(width):
            i = row + x * 4
            b, g, r = raw[i], raw[i + 1], raw[i + 2]
            grey = 0.2126 * r + 0.7152 * g + 0.0722 * b
            raw[i] = max(0, min(255, round(grey + (b - grey) * amount)))
            raw[i + 1] = max(0, min(255, round(grey + (g - grey) * amount)))
            raw[i + 2] = max(0, min(255, round(grey + (r - grey) * amount)))
    return QImage(bytes(raw), width, height, stride, QImage.Format.Format_RGB32).copy()


def blur(tiny: QImage, theme: Theme, screen: QSize) -> QImage:
    """The tiny picture enlarged to the soft size and blurred by the token's ``blur`` (logical pixels of
    the screen): a Gaussian of sigma blur/2, as the shell's materials use, drawn by Qt's blur effect."""
    soft = QSize(SOFT_WIDTH, max(1, round(SOFT_WIDTH * tiny.height() / max(1, tiny.width()))))
    scale = SOFT_WIDTH / max(1, screen.width())
    sigma = theme.glass("blur") / 2 * scale
    radius = max(1.0, sigma * 2.5)                 # the effect's radius reaches about 2.5 sigma
    margin = math.ceil(radius * 1.5)
    # The edges are continued outwards before blurring (the picture enlarged by the margin), then cut off,
    # so the blur does not darken the border.
    padded = tiny.scaled(soft.width() + 2 * margin, soft.height() + 2 * margin,
                         Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(padded))
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    result = QImage(soft, QImage.Format.Format_RGB32)
    result.fill(QColor(theme.color("background")))
    painter = QPainter(result)
    scene.render(painter, QRectF(0, 0, soft.width(), soft.height()),
                 QRectF(margin, margin, soft.width(), soft.height()))
    painter.end()
    scene.clear()
    return result


def _probe(image: QImage) -> list[str]:
    """The darkest and the brightest colours of ``image`` (searched at the PROBE size), as #RRGGBB."""
    small = image.scaled(QSize(*PROBE), Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation).convertToFormat(QImage.Format.Format_RGB32)
    raw = bytes(small.constBits())[:small.bytesPerLine() * small.height()]
    colours = set()
    for y in range(small.height()):
        row = y * small.bytesPerLine()
        for x in range(small.width()):
            i = row + x * 4
            colours.add(to_hex((raw[i + 2], raw[i + 1], raw[i])))
    ranked = sorted(colours, key=luminance)
    return list(dict.fromkeys(ranked[:_EXTREMES] + ranked[-_EXTREMES:]))


def glass_surfaces(theme: Theme, backdrop: str) -> dict[str, str]:
    """The opaque colours text stands on where the backdrop is ``backdrop``: the backdrop, and a pane and a
    card of glass on it."""
    surfaces = {"backdrop": backdrop}
    for kind in ("pane", "card"):
        colour, alpha = theme.rgba(f"glass-{kind}")
        surfaces[kind] = blend(backdrop, colour, alpha)
    return surfaces


def glass_layers(theme: Theme, backdrop: str) -> dict[str, str]:
    """``glass_surfaces`` under the hover and the pressed layer."""
    layers = {}
    for name, surface in glass_surfaces(theme, backdrop).items():
        for layer in ("fill-subtle-hover", "fill-subtle-pressed"):
            colour, alpha = theme.rgba(layer)
            layers[f"{layer} on {name}"] = blend(surface, colour, alpha)
    return layers


def legible(theme: Theme, backdrop: str) -> bool:
    """Whether text on ``backdrop`` and on the glass on it keeps 4.5:1 as on the opaque surfaces (see
    BODY_TEXT, SURFACE_TEXT, LAYER_TEXT)."""
    surfaces = list(glass_surfaces(theme, backdrop).values())
    layers = list(glass_layers(theme, backdrop).values())
    pairs = [(name, surfaces + layers) for name in BODY_TEXT] + [(name, surfaces) for name in SURFACE_TEXT] \
        + [(name, layers) for name in LAYER_TEXT]
    return all(contrast(theme.color(name), surface) >= TEXT_CONTRAST for name, behind in pairs for surface in behind)


def legible_opacity(theme: Theme, colours: list[str]) -> float:
    """The tint opacity for a backdrop whose extreme colours are ``colours``: the token's, raised in steps
    of LEGIBILITY_STEP until ``legible`` holds for each of them (1.0, the opaque tint, at most)."""
    tint, alpha = theme.rgba("glass-backdrop")
    while alpha < 1.0:
        if all(legible(theme, blend(colour, tint, alpha)) for colour in colours):
            return round(alpha, 4)
        alpha = min(1.0, alpha + LEGIBILITY_STEP)
    return 1.0


def _grain(dpr: float, opacity: float) -> QPixmap:
    """A tile of fine noise: white and black specks at up to twice ``opacity`` (premultiplied ARGB)."""
    generator = random.Random(0x6B05)              # the same pattern in every process
    size = GRAIN_TILE
    data = bytearray(size * size * 4)
    for i in range(0, len(data), 4):
        alpha = round(generator.random() * 2 * opacity * 255)
        level = alpha if generator.random() < 0.5 else 0        # premultiplied: white = alpha, black = 0
        data[i:i + 4] = bytes((level, level, level, alpha))
    image = QImage(bytes(data), size, size, size * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


# --- the backdrop ------------------------------------------------------------------------------------------------

class _Notifier(QObject):
    changed = Signal()            # a picture finished loading: windows repaint


class Backdrop:
    """The backdrops of this process (one instance, ``backdrop()``)."""

    def __init__(self) -> None:
        self.notifier = _Notifier()
        self._pictures: dict[tuple, QImage | None] = {}         # (path, mtime, tiny size) -> tiny picture
        self._loading: set[tuple] = set()
        self._soft: OrderedDict[tuple, tuple[QImage, float, QColor]] = OrderedDict()
        self._windows: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._grains: dict[float, QPixmap] = {}
        self.renders = 0          # window backdrops made (tests: once per size)
        self.softs = 0            # blurred pictures made (tests: once per picture, scheme and accent)
        self.deferred = 0         # backdrops put off while a window was being resized (tests)
        self.clock = time.monotonic
        self._last_size: tuple[float, tuple, QSize] | None = None     # (when, base, size) of the last request
        self._pause: QTimer | None = None

    def clear(self) -> None:
        self._pictures.clear()
        self._soft.clear()
        self._windows.clear()
        self._grains.clear()

    # --- the picture ---
    def _picture_key(self, wallpaper: str, screen: QSize) -> tuple:
        try:
            mtime = os.stat(wallpaper).st_mtime_ns if wallpaper else 0
        except OSError:
            mtime = -1
        return wallpaper, mtime, _tiny_size(screen).width(), _tiny_size(screen).height()

    def _picture(self, theme: Theme, wallpaper: str, screen: QSize) -> QImage | None:
        """The tiny picture, or None while it loads. A picture that cannot be read is the default background."""
        if not wallpaper:
            return default_picture(theme, _tiny_size(screen))
        key = self._picture_key(wallpaper, screen)
        if key in self._pictures:
            picture = self._pictures[key]
            return picture if picture is not None else default_picture(theme, _tiny_size(screen))
        if key[1] == -1:                     # gone or unreadable: the default background
            self._pictures[key] = None
            return default_picture(theme, _tiny_size(screen))
        if key not in self._loading:
            self._loading.add(key)
            from .worker import shared_worker

            def done(picture: QImage, key=key) -> None:
                self._loading.discard(key)
                self._pictures[key] = picture
                self.notifier.changed.emit()

            def failed(_exc: Exception, key=key) -> None:
                self._loading.discard(key)
                self._pictures[key] = None
                self.notifier.changed.emit()

            shared_worker().submit(read_picture, wallpaper, QSize(screen), on_done=done, on_error=failed)
        return None

    def wait(self, timeout: float = 5.0) -> bool:
        """Wait until no picture is loading (tests); True when none is."""
        from .worker import shared_worker
        if self._loading:
            shared_worker().wait(timeout)
            QApplication.processEvents()
        return not self._loading

    # --- the blurred, tinted picture ---
    def soft(self, theme: Theme, wallpaper: str, screen: QSize) -> tuple[QImage, float, QColor] | None:
        """(the blurred and tinted picture at the soft size, the tint opacity used, its mean colour), or None
        while the picture loads."""
        key = (theme.scheme, theme.accent, id(theme.tokens), self._picture_key(wallpaper, screen))
        cached = self._soft.get(key)
        if cached is not None:
            self._soft.move_to_end(key)
            return cached
        picture = self._picture(theme, wallpaper, screen)
        if picture is None:
            return None
        softened = blur(saturate(picture, theme.glass("saturation")), theme, screen)
        tint, _minimum = theme.rgba("glass-backdrop")
        alpha = legible_opacity(theme, _probe(softened))
        painter = QPainter(softened)
        colour = QColor(tint)
        colour.setAlphaF(alpha)
        painter.fillRect(softened.rect(), colour)
        painter.end()
        mean = softened.scaled(1, 1, Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation).pixelColor(0, 0)
        self.softs += 1
        self._soft[key] = (softened, alpha, mean)
        while len(self._soft) > 4:
            self._soft.popitem(last=False)
        return self._soft[key]

    # --- the backdrop of a window ---
    def window(self, theme: Theme, wallpaper: str, size: QSize, dpr: float,
               screen: QSize, owner: int = 0) -> tuple[QPixmap, QPointF] | None:
        """The backdrop of a window of ``size`` (logical pixels) at ``dpr``: a pixmap and where the window's top
        left corner lies in it; None while the picture loads.

        A window shows the part of the screen-sized picture centred on the screen, so the backdrop of a
        larger window holds that of a smaller one in its middle. A backdrop is therefore made a little larger
        than the window (sides rounded up to BUCKET, at most the screen) and every smaller window, and every
        window that grows within it while it is resized, is a part of it: no new backdrop per frame.
        """
        soft = self.soft(theme, wallpaper, screen)
        if soft is None or size.isEmpty():
            return None
        image, alpha, _mean = soft
        width, height = size.width(), size.height()
        sw, sh = max(1, screen.width()), max(1, screen.height())
        base = (theme.scheme, theme.accent, id(theme.tokens), wallpaper, alpha, round(dpr, 3), sw, sh,
                image.cacheKey())
        fits = width <= sw and height <= sh
        if fits:
            # The smallest kept backdrop that holds this window.
            best = None
            for key, pixmap in self._windows.items():
                if key[:-2] == base and key[-2] >= width and key[-1] >= height and key[-2] <= sw:
                    if best is None or key[-2] * key[-1] < best[0][-2] * best[0][-1]:
                        best = (key, pixmap)
            if best is not None:
                key, pixmap = best
                self._windows.move_to_end(key)
                self._last_size = (self.clock(), (owner, base), QSize(size))
                return pixmap, QPointF((key[-2] - width) // 2, (key[-1] - height) // 2)
            stand_in = self._while_resizing(owner, base, size)
            if stand_in is not None:
                return stand_in
            made_w = min(sw, -(-width // BUCKET) * BUCKET)
            made_h = min(sh, -(-height // BUCKET) * BUCKET)
            scale = image.width() / sw
            source = QRectF((sw - made_w) / 2 * scale, (sh - made_h) / 2 * scale, made_w * scale, made_h * scale)
        else:
            # A window larger than the screen shows all of the picture, enlarged.
            made_w, made_h = width, height
            key = base + (width + sw + 1, height)            # never the backdrop of a smaller window
            cached = self._windows.get(key)
            if cached is not None:
                self._windows.move_to_end(key)
                return cached, QPointF(0, 0)
            fit = min(sw / width, sh / height)
            scale = image.width() / sw
            crop_w, crop_h = width * fit, height * fit
            source = QRectF((sw - crop_w) / 2 * scale, (sh - crop_h) / 2 * scale, crop_w * scale, crop_h * scale)
        pixmap = QPixmap(max(1, round(made_w * dpr)), max(1, round(made_h * dpr)))
        pixmap.setDevicePixelRatio(dpr)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(QRectF(0, 0, made_w, made_h), image, source)
        noise = theme.glass("noise")
        if noise > 0:
            grain = self._grains.get(dpr)
            if grain is None:
                grain = self._grains[dpr] = _grain(dpr, noise)
            painter.drawTiledPixmap(QRectF(0, 0, made_w, made_h), grain)
        painter.end()
        self.renders += 1
        self._last_size = (self.clock(), (owner, base), QSize(size))
        key = base + ((made_w, made_h) if fits else (width + sw + 1, height))
        self._windows[key] = pixmap
        while len(self._windows) > KEPT_BACKDROPS:
            self._windows.popitem(last=False)
        return pixmap, QPointF((made_w - width) // 2, (made_h - height) // 2) if fits else QPointF(0, 0)


    def _while_resizing(self, owner: int, base: tuple, size: QSize) -> tuple[QPixmap, QPointF] | None:
        """While a window (``owner``) is being resized (its size changed less than RESIZE_PAUSE ago) and has
        outgrown its backdrop: the largest kept one, centred (paint_backdrop fills what lies beyond it with the
        mean colour), and a repaint once the size stands still, when the right one is made. None otherwise."""
        now = self.clock()
        last, self._last_size = self._last_size, (now, (owner, base), QSize(size))
        if last is None or last[1] != (owner, base) or last[2] == size or now - last[0] >= RESIZE_PAUSE:
            return None
        kept = [(key, pixmap) for key, pixmap in self._windows.items() if key[:-2] == base and key[-2] <= base[6]]
        if not kept:
            return None
        key, pixmap = max(kept, key=lambda item: item[0][-2] * item[0][-1])
        self.deferred += 1
        if QApplication.instance() is not None:
            if self._pause is None:
                self._pause = QTimer()
                self._pause.setSingleShot(True)
                self._pause.timeout.connect(self._paused)
            self._pause.start(round(RESIZE_PAUSE * 1000) + 10)
        return pixmap, QPointF((key[-2] - size.width()) // 2, (key[-1] - size.height()) // 2)

    def _paused(self) -> None:
        """The size stood still: windows paint again, and the one being resized gets its backdrop."""
        if self._last_size is not None and self.clock() - self._last_size[0] < RESIZE_PAUSE and self._pause is not None:
            self._pause.start(round(RESIZE_PAUSE * 1000) + 10)
            return
        self.notifier.changed.emit()


_backdrop: Backdrop | None = None


def backdrop() -> Backdrop:
    global _backdrop
    if _backdrop is None:
        _backdrop = Backdrop()
    return _backdrop


def _screen_size(widget: QWidget | None) -> QSize:
    screen = widget.screen() if widget is not None else None
    screen = screen or QGuiApplication.primaryScreen()
    size = screen.size() if screen is not None else QSize()
    return size if size.width() > 0 and size.height() > 0 else QSize(DEFAULT_SCREEN)


def host_of(widget: QWidget) -> QWidget:
    """The window whose backdrop ``widget`` shows: its own window, or for a popup, a tool tip or a dialog
    without a backdrop of its own the window it belongs to."""
    window = widget.window()
    kind = window.windowType()
    if kind in (Qt.WindowType.Popup, Qt.WindowType.ToolTip):
        parent = window.parentWidget()
        if parent is not None:
            return parent.window()
        active = QApplication.activeWindow()
        if active is not None and active is not window:
            return active
    return window


def window_backdrop(window: QWidget) -> tuple[QPixmap, QPointF] | None:
    """The backdrop of ``window`` for its size and screen now: a pixmap and the window's top left corner in it
    (None: glass is off or the picture is not ready)."""
    if not enabled():
        return None
    from .theming import manager
    themes = manager()
    return backdrop().window(themes.theme, themes.wallpaper, window.size(), window.devicePixelRatioF(),
                             _screen_size(window), id(window))


def paint_backdrop(painter: QPainter, widget: QWidget, rect: QRect | QRectF | None = None) -> bool:
    """Paint the backdrop under ``rect`` (default: all) of ``widget``: the part of its window's backdrop that
    lies there. False (and nothing painted) while glass is off or the picture loads."""
    host = host_of(widget)
    found = window_backdrop(host)
    if found is None:
        return False
    pixmap, origin = found
    target = QRectF(widget.rect() if rect is None else rect)
    if host is widget or host.isAncestorOf(widget):
        offset = QPointF(widget.mapTo(host, QPoint(0, 0)))
    else:
        offset = QPointF(widget.mapToGlobal(QPoint(0, 0)) - host.mapToGlobal(QPoint(0, 0)))
    offset += origin
    dpr = pixmap.devicePixelRatio()
    source = target.translated(offset)
    bounds = QRectF(0, 0, pixmap.width() / dpr, pixmap.height() / dpr)
    if not bounds.contains(source):
        # A popup that reaches past its window: the rest is the backdrop's mean colour.
        soft = backdrop().soft(manager_theme(), manager_wallpaper(), _screen_size(host))
        if soft is not None:
            painter.fillRect(target, soft[2])
        visible = source.intersected(bounds)
        if visible.isEmpty():
            return True
        target = visible.translated(-offset)
        source = visible
    painter.drawPixmap(target, pixmap, QRectF(source.x() * dpr, source.y() * dpr, source.width() * dpr,
                                              source.height() * dpr))
    return True


def manager_theme() -> Theme:
    from .theming import manager
    return manager().theme


def manager_wallpaper() -> str:
    from .theming import manager
    return manager().wallpaper


def tint_opacity(widget: QWidget | None = None) -> float | None:
    """The tint opacity the backdrop uses now (the token's, or more for legibility; None while it loads)."""
    soft = backdrop().soft(manager_theme(), manager_wallpaper(), _screen_size(widget))
    return None if soft is None else soft[1]
