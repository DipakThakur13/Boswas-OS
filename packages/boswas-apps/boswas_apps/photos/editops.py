"""The edit pipeline of Photos: pure functions over QImage (QtGui only, no widgets, no numpy).

An edit is a value, ``EditState``; ``render(source, state)`` makes the
edited picture from the untouched source. Nothing is changed in place, so
undo and redo are a list of states, the preview is ``render`` on a small
copy of the source, and saving is ``render`` on the full one. The same
state gives the same pixels every time.

Order of the pipeline:

1. geometry: orientation (quarter turns and mirroring, exact), straighten
   (a rotation by up to 45 degrees, cropped to the largest upright
   rectangle of the same shape that has no empty corner), crop;
2. the filter (a named set of adjustments at a strength);
3. the adjustments;
4. markup: the pen and highlighter strokes, which are vectors until here.

How the adjustments are computed without a per-pixel loop in Python:

* tone and colour balance (exposure, brightness, contrast, highlights,
  shadows, warmth, tint) are functions of one channel value, so they are
  256-entry tables applied with ``bytes.translate`` to the colour planes
  of the picture, which runs at the speed of C;
* saturation and sharpness need a second picture (the grey version, a
  blurred version). The differences "picture minus other" and "other
  minus picture" are made with QPainter (inverting and saturating
  addition), scaled with a table, and added to and subtracted from the
  picture: ``out = clamp(c + k * (c - other))``, exact in 8 bits;
* the vignette is a radial gradient multiplied onto (or screened over)
  the picture.

Large pictures are processed in horizontal bands so that the temporary
copies stay small.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field, fields, replace

from PySide6.QtCore import QPointF, QRect, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QRadialGradient, QTransform

from . import exif
from .decode import apply_orientation, orientation_transform

BAND_PIXELS = 6_000_000                 # pictures above this are adjusted band by band
STRAIGHTEN_LIMIT = 45.0
MIN_CROP = 0.01                         # the smallest crop, as a fraction of the picture's side
_LITTLE = sys.byteorder == "little"
_B, _G, _R, _A = (0, 1, 2, 3) if _LITTLE else (3, 2, 1, 0)
_BUMP = 9.4815                          # 1 / max of x^3 (1 - x): makes the highlight and shadow weights peak at 1
_RGB32 = QImage.Format.Format_RGB32
_ARGB32 = QImage.Format.Format_ARGB32
_PREMULTIPLIED = QImage.Format.Format_ARGB32_Premultiplied


# --- the state of an edit ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Adjustments:
    """Light and colour, each from -1 to 1 with 0 meaning "as it is" (sharpness from 0 to 1)."""

    brightness: float = 0.0
    exposure: float = 0.0
    contrast: float = 0.0
    highlights: float = 0.0
    shadows: float = 0.0
    saturation: float = 0.0
    warmth: float = 0.0
    tint: float = 0.0
    sharpness: float = 0.0
    vignette: float = 0.0

    def is_identity(self) -> bool:
        return all(getattr(self, item.name) == 0 for item in fields(self))

    def clamped(self) -> "Adjustments":
        values = {}
        for item in fields(self):
            value = float(getattr(self, item.name))
            if not math.isfinite(value):
                value = 0.0
            low = 0.0 if item.name == "sharpness" else -1.0
            values[item.name] = max(low, min(1.0, value))
        return Adjustments(**values)

    def scaled(self, strength: float) -> "Adjustments":
        return Adjustments(**{item.name: getattr(self, item.name) * strength for item in fields(self)}).clamped()


ADJUSTMENT_NAMES = tuple(item.name for item in fields(Adjustments))

# The looks of the Filter tab: adjustments under a name, in the order they are offered.
FILTERS: dict[str, Adjustments] = {
    "Vivid": Adjustments(saturation=0.4, contrast=0.2),
    "Bright": Adjustments(exposure=0.15, brightness=0.2, saturation=0.1),
    "Warm": Adjustments(warmth=0.45, saturation=0.1, highlights=0.2),
    "Cool": Adjustments(warmth=-0.45, contrast=0.1),
    "Fade": Adjustments(contrast=-0.45, brightness=0.15, saturation=-0.25, shadows=0.5),
    "Dramatic": Adjustments(contrast=0.5, highlights=-0.5, shadows=-0.3, saturation=0.1, vignette=0.4),
    "Sepia": Adjustments(saturation=-1.0, warmth=0.6, tint=-0.1, contrast=0.05),
    "Mono": Adjustments(saturation=-1.0),
    "Noir": Adjustments(saturation=-1.0, contrast=0.55, vignette=0.5),
}

PEN, HIGHLIGHTER = "pen", "highlighter"
HIGHLIGHTER_OPACITY = 0.45


@dataclass(frozen=True)
class Stroke:
    """One line of markup. Points are in pixels of the full-size, untouched source picture."""

    tool: str                                   # PEN or HIGHLIGHTER
    color: str                                  # "#RRGGBB"
    width: float                                # in pixels of the full-size picture
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class EditState:
    orientation: int = 1                        # EXIF-style 1..8: quarter turns and mirroring
    straighten: float = 0.0                     # degrees, clockwise positive
    crop: tuple[float, float, float, float] | None = None    # x, y, w, h as fractions of the straightened picture
    adjust: Adjustments = field(default_factory=Adjustments)
    filter: str = ""                            # a key of FILTERS, or "" for none
    filter_strength: float = 1.0
    strokes: tuple[Stroke, ...] = ()

    def is_identity(self) -> bool:
        return (self.orientation == 1 and self.straighten == 0 and self.crop is None
                and self.adjust.is_identity() and not self.filter and not self.strokes)

    def has_geometry(self) -> bool:
        return self.orientation != 1 or self.straighten != 0 or self.crop is not None

    # --- changes; each returns a new state ---
    def rotated(self, clockwise: bool = True) -> "EditState":
        """A quarter turn of the result. The crop turns with it."""
        crop = self.crop
        if crop is not None:
            x, y, w, h = crop
            crop = (1 - (y + h), x, h, w) if clockwise else (y, 1 - (x + w), h, w)
        turn = exif.ROTATE_CW if clockwise else exif.ROTATE_CCW
        return replace(self, orientation=exif.compose_orientation(self.orientation, turn), crop=_clean_crop(crop))

    def flipped(self, horizontal: bool = True) -> "EditState":
        """The result mirrored left to right (or top to bottom). Crop and straightening follow."""
        crop = self.crop
        if crop is not None:
            x, y, w, h = crop
            crop = (1 - (x + w), y, w, h) if horizontal else (x, 1 - (y + h), w, h)
        mirror = exif.FLIP_HORIZONTAL if horizontal else exif.FLIP_VERTICAL
        return replace(self, orientation=exif.compose_orientation(self.orientation, mirror),
                       straighten=-self.straighten, crop=_clean_crop(crop))

    def with_straighten(self, degrees: float) -> "EditState":
        if not math.isfinite(degrees):
            degrees = 0.0
        return replace(self, straighten=max(-STRAIGHTEN_LIMIT, min(STRAIGHTEN_LIMIT, float(degrees))))

    def with_crop(self, crop: tuple[float, float, float, float] | None) -> "EditState":
        return replace(self, crop=_clean_crop(crop))

    def with_adjust(self, **values: float) -> "EditState":
        return replace(self, adjust=replace(self.adjust, **values).clamped())

    def with_filter(self, name: str, strength: float = 1.0) -> "EditState":
        if name and name not in FILTERS:
            raise KeyError(name)
        return replace(self, filter=name, filter_strength=max(0.0, min(1.0, float(strength))))

    def with_stroke(self, stroke: Stroke) -> "EditState":
        return replace(self, strokes=self.strokes + (stroke,))

    def without_strokes(self, indexes) -> "EditState":
        gone = set(indexes)
        return replace(self, strokes=tuple(s for i, s in enumerate(self.strokes) if i not in gone))

    def filter_adjustments(self) -> Adjustments:
        if not self.filter:
            return Adjustments()
        return FILTERS[self.filter].scaled(self.filter_strength)


def _clean_crop(crop) -> tuple[float, float, float, float] | None:
    """A crop inside the picture and not degenerate; None when it is the whole picture."""
    if crop is None:
        return None
    x, y, w, h = (float(v) if math.isfinite(float(v)) else 0.0 for v in crop)
    x, y = max(0.0, min(1.0 - MIN_CROP, x)), max(0.0, min(1.0 - MIN_CROP, y))
    w, h = max(MIN_CROP, min(1.0 - x, w)), max(MIN_CROP, min(1.0 - y, h))
    if x <= 1e-6 and y <= 1e-6 and w >= 1 - 1e-6 and h >= 1 - 1e-6:
        return None
    return (x, y, w, h)


# --- geometry ----------------------------------------------------------------------------------------------

def inscribed_scale(width: float, height: float, degrees: float) -> float:
    """How much smaller the largest upright rectangle of the same shape is that fits into a
    ``width`` x ``height`` rectangle turned by ``degrees`` (1 for no turn)."""
    angle = math.radians(abs(degrees))
    if angle == 0 or width <= 0 or height <= 0:
        return 1.0
    sine, cosine = math.sin(angle), math.cos(angle)
    return min(width / (width * cosine + height * sine), height / (width * sine + height * cosine))


@dataclass(frozen=True)
class Geometry:
    """Where the pixels of a source of a given size go."""

    canvas: QSize               # the picture after orientation and straightening: what the crop tool shows
    crop: QRect                 # the crop inside the canvas, in pixels
    to_canvas: QTransform       # source pixel -> canvas pixel

    @property
    def size(self) -> QSize:
        return self.crop.size()

    @property
    def to_output(self) -> QTransform:
        return self.to_canvas * QTransform.fromTranslate(-self.crop.x(), -self.crop.y())


def crop_pixels(crop: tuple[float, float, float, float] | None, width: int, height: int) -> QRect:
    """A crop given in fractions as a pixel rectangle inside ``width`` x ``height`` (never empty)."""
    if crop is None:
        return QRect(0, 0, width, height)
    x, y, w, h = crop
    left, top = round(x * width), round(y * height)
    right, bottom = round((x + w) * width), round((y + h) * height)
    left, top = max(0, min(width - 1, left)), max(0, min(height - 1, top))
    right, bottom = max(left + 1, min(width, right)), max(top + 1, min(height, bottom))
    return QRect(left, top, right - left, bottom - top)


def geometry(source: QSize, state: EditState) -> Geometry:
    width, height = source.width(), source.height()
    oriented = QImage.trueMatrix(orientation_transform(state.orientation), width, height)
    if exif.swaps_sides(state.orientation):
        width, height = height, width
    if state.straighten == 0:
        canvas = QSize(width, height)
        to_canvas = oriented
    else:
        scale = inscribed_scale(width, height, state.straighten)
        canvas = QSize(max(1, math.floor(width * scale)), max(1, math.floor(height * scale)))
        centre_x, centre_y = width / 2, height / 2
        turn = QTransform()
        turn.rotate(state.straighten)
        to_canvas = (oriented * QTransform.fromTranslate(-centre_x, -centre_y) * turn
                     * QTransform.fromTranslate(canvas.width() / 2, canvas.height() / 2))
    return Geometry(canvas, crop_pixels(state.crop, canvas.width(), canvas.height()), to_canvas)


def aspect_crop(canvas_width: int, canvas_height: int, ratio: float,
                within: tuple[float, float, float, float] | None = None) -> tuple[float, float, float, float]:
    """The largest crop with ``ratio`` (width / height in pixels) centred in ``within`` (default: everything)."""
    x, y, w, h = within or (0.0, 0.0, 1.0, 1.0)
    pixel_w, pixel_h = w * canvas_width, h * canvas_height
    if ratio <= 0 or pixel_w <= 0 or pixel_h <= 0:
        return (x, y, w, h)
    if pixel_w / pixel_h > ratio:
        new_w, new_h = pixel_h * ratio, pixel_h
    else:
        new_w, new_h = pixel_w, pixel_w / ratio
    fraction_w, fraction_h = new_w / canvas_width, new_h / canvas_height
    return (x + (w - fraction_w) / 2, y + (h - fraction_h) / 2, fraction_w, fraction_h)


def render_geometry(source: QImage, state: EditState, *, crop: bool = True) -> QImage:
    """Step 1 of the pipeline. With ``crop=False`` the whole straightened picture (for the crop tool)."""
    placed = geometry(source.size(), state)
    area = placed.crop if crop else QRect(0, 0, placed.canvas.width(), placed.canvas.height())
    if state.straighten == 0:
        upright = apply_orientation(source, state.orientation)
        return upright if area == upright.rect() else upright.copy(area)
    alpha = source.hasAlphaChannel()
    result = QImage(area.size(), _PREMULTIPLIED if alpha else _RGB32)
    result.fill(Qt.GlobalColor.transparent if alpha else Qt.GlobalColor.black)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setTransform(placed.to_canvas * QTransform.fromTranslate(-area.x(), -area.y()))
    painter.drawImage(0, 0, source)
    painter.end()
    return result


def resize(image: QImage, width: int, height: int) -> QImage:
    """``image`` scaled to exactly ``width`` x ``height`` with smooth filtering."""
    width, height = max(1, int(width)), max(1, int(height))
    if image.width() == width and image.height() == height:
        return image
    return image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)


# --- tables ------------------------------------------------------------------------------------------------

def _clamp01(value: float) -> float:
    return 0.0 if value < 0 else 1.0 if value > 1 else value


def tone_value(x: float, adjust: Adjustments) -> float:
    """Exposure, brightness, contrast, highlights and shadows on one channel value in 0..1."""
    if adjust.exposure:
        x = _clamp01(x * 2.0 ** (adjust.exposure * 2.0 / 2.2))      # up to two stops, in gamma space
    if adjust.brightness:
        x = x ** (2.0 ** -adjust.brightness)                         # the mid tones move, black and white stay
    if adjust.contrast > 0:
        power = 1.0 + 2.0 * adjust.contrast                          # an S curve through 0, 0.5 and 1
        x = 0.5 * (2 * x) ** power if x < 0.5 else 1 - 0.5 * (2 * (1 - x)) ** power
    elif adjust.contrast < 0:
        x = 0.5 + (x - 0.5) * (1 + 0.7 * adjust.contrast)
    if adjust.highlights:
        x += 0.1 * adjust.highlights * _BUMP * x ** 3 * (1 - x)      # the bright tones, white stays white
    if adjust.shadows:
        x += 0.1 * adjust.shadows * _BUMP * x * (1 - x) ** 3         # the dark tones, black stays black
    return _clamp01(x)


def _table(function) -> bytes:
    return bytes(max(0, min(255, round(255 * function(value / 255)))) for value in range(256))


def tone_table(adjust: Adjustments) -> bytes | None:
    """The table of the tone adjustments, or None when they change nothing."""
    if not (adjust.exposure or adjust.brightness or adjust.contrast or adjust.highlights or adjust.shadows):
        return None
    return _table(lambda x: tone_value(x, adjust))


def balance_gains(adjust: Adjustments) -> tuple[float, float, float]:
    """The gains of red, green and blue for warmth and tint."""
    red = 1 + 0.25 * adjust.warmth + 0.1 * adjust.tint
    green = 1 - 0.2 * adjust.tint
    blue = 1 - 0.25 * adjust.warmth + 0.1 * adjust.tint
    return red, green, blue


def balance_tables(adjust: Adjustments) -> tuple[bytes, bytes, bytes] | None:
    """The tables of red, green and blue for warmth and tint, or None when they change nothing."""
    if not (adjust.warmth or adjust.tint):
        return None
    red, green, blue = balance_gains(adjust)
    return (_table(lambda x: _clamp01(x * red)), _table(lambda x: _clamp01(x * green)),
            _table(lambda x: _clamp01(x * blue)))


def _gain_table(factor: float) -> bytes:
    return bytes(min(255, round(value * factor)) for value in range(256))


# --- pictures as bytes -------------------------------------------------------------------------------------

def _wrap(data: bytes | bytearray, width: int, height: int, fmt: QImage.Format = _RGB32) -> QImage:
    """An independent QImage with the pixels of ``data`` (four bytes a pixel)."""
    return QImage(data, width, height, 4 * width, fmt).copy()


def _opaque_bytes(image: QImage) -> bytearray:
    """The pixels of a 32-bit image with the alpha byte set to 255."""
    data = bytearray(image.constBits())
    data[_A::4] = b"\xff" * (len(data) // 4)
    return data


def _apply_tables(data: bytearray, red: bytes | None, green: bytes | None, blue: bytes | None) -> None:
    """Translate the colour planes of BGRA pixel data in place."""
    for offset, table in ((_R, red), (_G, green), (_B, blue)):
        if table is not None:
            data[offset::4] = data[offset::4].translate(table)


def _translated(image: QImage, table: bytes) -> QImage:
    """An RGB32 image with every colour value sent through ``table``."""
    data = bytearray(bytes(image.constBits()).translate(table))
    data[_A::4] = b"\xff" * (len(data) // 4)
    return _wrap(data, image.width(), image.height())


def _plus(a: QImage, b: QImage) -> QImage:
    """a + b per channel, stopping at 255 (both RGB32)."""
    result = a.copy()
    painter = QPainter(result)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
    painter.drawImage(0, 0, b)
    painter.end()
    return result


def _minus(a: QImage, b: QImage) -> QImage:
    """a - b per channel, stopping at 0 (both RGB32): 255 - ((255 - a) + b)."""
    result = a.copy()
    result.invertPixels()
    painter = QPainter(result)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
    painter.drawImage(0, 0, b)
    painter.end()
    result.invertPixels()
    return result


def _push_away(image: QImage, other: QImage, amount: float) -> QImage:
    """``clamp(c + amount * (c - other))`` per channel: away from ``other`` for a positive amount,
    towards it for a negative one (-1 gives ``other``)."""
    if amount == 0:
        return image
    table = _gain_table(abs(amount))
    above = _translated(_minus(image, other), table)        # where the picture is brighter than the other
    below = _translated(_minus(other, image), table)        # where it is darker
    if amount > 0:
        return _minus(_plus(image, above), below)
    return _minus(_plus(image, below), above)


def _grey(image: QImage) -> QImage:
    return image.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(_RGB32)


def _blurred(image: QImage, factor: int) -> QImage:
    small = image.scaled(max(1, image.width() // factor), max(1, image.height() // factor),
                         Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return small.scaled(image.width(), image.height(), Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)


def blur_factor(width: int, height: int) -> int:
    """The radius of the blur behind the sharpening, growing with the picture so that the effect looks
    the same on the preview and on the full picture."""
    return max(2, round(max(width, height) / 800))


def _vignette(image: QImage, amount: float, origin_y: int, full: QSize) -> None:
    """Darken (amount > 0) or lighten the corners of an RGB32 image in place. ``image`` is the band of
    the ``full`` picture that starts at row ``origin_y``."""
    strength = 0.75 * abs(amount)
    gradient = QRadialGradient(QPointF(0, 0), 1.0)
    steps = 16
    for step in range(steps + 1):
        t = step / steps
        edge = _clamp01((t - 0.3) / 0.7)
        weight = strength * edge * edge * (3 - 2 * edge)
        level = 1 - weight if amount > 0 else weight
        gradient.setColorAt(t, QColor.fromRgbF(level, level, level))
    brush = QBrush(gradient)
    # radius 1 of the gradient is the ellipse through the corners of the full picture
    half = math.sqrt(0.5)
    transform = QTransform.fromTranslate(full.width() / 2, full.height() / 2 - origin_y)
    transform = QTransform.fromScale(full.width() * half, full.height() * half) * transform
    brush.setTransform(transform)
    painter = QPainter(image)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply if amount > 0
                               else QPainter.CompositionMode.CompositionMode_Screen)
    painter.fillRect(image.rect(), brush)
    painter.end()


def _adjust_band(band: QImage, adjust: Adjustments, has_alpha: bool, origin_y: int, full: QSize,
                 factor: int) -> bytearray:
    """The adjustments on one band (RGB32 or ARGB32, not premultiplied). Returns its pixels, four
    bytes each, alpha as it was."""
    width, height = band.width(), band.height()
    data = bytearray(band.constBits())
    alpha = None
    if has_alpha:
        alpha = bytes(data[_A::4])
        data[_A::4] = b"\xff" * (width * height)
    tone = tone_table(adjust)
    balance = balance_tables(adjust)
    if adjust.saturation == 0 and (tone is not None or balance is not None):
        # one pass: balance after tone, per channel
        tables = []
        for index in range(3):
            first = tone if tone is not None else bytes(range(256))
            tables.append(first.translate(balance[index]) if balance is not None else first)
        _apply_tables(data, *tables)
        tone = balance = None
    elif tone is not None:
        _apply_tables(data, tone, tone, tone)
    image = _wrap(data, width, height)
    if adjust.saturation:
        image = _push_away(image, _grey(image), adjust.saturation)
        if balance is not None:
            data = _opaque_bytes(image)
            _apply_tables(data, *balance)
            image = _wrap(data, width, height)
    if adjust.sharpness > 0:
        image = _push_away(image, _blurred(image, factor), 1.5 * adjust.sharpness)
    if adjust.vignette:
        _vignette(image, adjust.vignette, origin_y, full)
    data = bytearray(image.constBits())
    if alpha is not None:
        data[_A::4] = alpha
    return data


def apply_adjustments(image: QImage, adjust: Adjustments) -> QImage:
    """Steps 2 and 3 of the pipeline: ``image`` with ``adjust`` applied. The alpha channel is kept."""
    adjust = adjust.clamped()
    if adjust.is_identity() or image.isNull():
        return image
    has_alpha = image.hasAlphaChannel()
    work = image.convertToFormat(_ARGB32 if has_alpha else _RGB32)
    width, height = work.width(), work.height()
    full = QSize(width, height)
    factor = blur_factor(width, height)
    if width * height <= BAND_PIXELS:
        data = _adjust_band(work, adjust, has_alpha, 0, full, factor)
    else:
        # Band by band, so the temporary pictures stay small. Sharpening looks at neighbours: each band
        # is computed with a margin that is thrown away.
        rows = max(factor, (BAND_PIXELS // width) // factor * factor)
        margin = 4 * factor if adjust.sharpness > 0 else 0
        stride = 4 * width
        data = bytearray(stride * height)
        for top in range(0, height, rows):
            start = max(0, top - margin)
            stop = min(height, top + rows + margin)
            count = min(rows, height - top)
            band = _adjust_band(work.copy(0, start, width, stop - start), adjust, has_alpha, start, full, factor)
            data[top * stride:(top + count) * stride] = band[(top - start) * stride:(top - start + count) * stride]
    if has_alpha:
        return _wrap(data, width, height, _ARGB32).convertToFormat(_PREMULTIPLIED)
    return _wrap(data, width, height)


# --- markup ------------------------------------------------------------------------------------------------

def stroke_path(points, transform: QTransform | None = None) -> QPainterPath:
    """The line through ``points`` (smoothed through the middles of its segments)."""
    path = QPainterPath()
    mapped = [QPointF(x, y) for x, y in points]
    if transform is not None:
        mapped = [transform.map(point) for point in mapped]
    if not mapped:
        return path
    path.moveTo(mapped[0])
    if len(mapped) == 1:
        path.lineTo(mapped[0] + QPointF(0.01, 0.01))
        return path
    if len(mapped) == 2:
        path.lineTo(mapped[1])
        return path
    for index in range(1, len(mapped) - 1):
        middle = (mapped[index] + mapped[index + 1]) / 2
        path.quadTo(mapped[index], middle)
    path.lineTo(mapped[-1])
    return path


def stroke_pen(stroke: Stroke, scale: float = 1.0) -> QPen:
    colour = QColor(stroke.color)
    if not colour.isValid():
        colour = QColor(Qt.GlobalColor.black)
    if stroke.tool == HIGHLIGHTER:
        colour.setAlphaF(HIGHLIGHTER_OPACITY)
        return QPen(colour, max(0.5, stroke.width * scale), Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap,
                    Qt.PenJoinStyle.RoundJoin)
    return QPen(colour, max(0.5, stroke.width * scale), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin)


def draw_strokes(painter: QPainter, strokes, transform: QTransform, scale: float = 1.0) -> None:
    """Draw markup with ``painter``. ``transform`` maps full-size source pixels to the painter's
    coordinates; ``scale`` is how much larger a length is there than in the full-size source."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for stroke in strokes:
        if not stroke.points:
            continue
        painter.setPen(stroke_pen(stroke, scale))
        painter.drawPath(stroke_path(stroke.points, transform))
    painter.restore()


def stroke_hit(stroke: Stroke, x: float, y: float, radius: float) -> bool:
    """Whether a point (full-size source pixels) lies within ``radius`` of a stroke: what the eraser asks."""
    reach = radius + stroke.width / 2
    points = stroke.points
    if len(points) == 1:
        return math.hypot(points[0][0] - x, points[0][1] - y) <= reach
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        t = 0.0 if length == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length))
        if math.hypot(ax + t * dx - x, ay + t * dy - y) <= reach:
            return True
    return False


# --- the whole pipeline ------------------------------------------------------------------------------------

def render(source: QImage, state: EditState, scale: float = 1.0, *, crop: bool = True, markup: bool = True) -> QImage:
    """The edited picture.

    ``source`` is the untouched picture or a smaller copy of it; ``scale``
    says how much smaller (width of ``source`` / width of the full
    picture), which places and sizes the markup. ``crop=False`` leaves the
    crop out (the crop tool shows the whole picture).
    """
    if source.isNull():
        return source
    image = render_geometry(source, state, crop=crop)
    image = apply_adjustments(image, state.filter_adjustments())
    image = apply_adjustments(image, state.adjust)
    if markup and state.strokes:
        placed = geometry(source.size(), state)
        to_image = placed.to_output if crop else placed.to_canvas
        if image.format() not in (_RGB32, _PREMULTIPLIED):
            image = image.convertToFormat(_PREMULTIPLIED if image.hasAlphaChannel() else _RGB32)
        else:
            image = image.copy()
        painter = QPainter(image)
        draw_strokes(painter, state.strokes, QTransform.fromScale(scale, scale) * to_image, scale)
        painter.end()
    return image
