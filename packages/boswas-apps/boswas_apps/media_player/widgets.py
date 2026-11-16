"""The custom widgets of Media Player: seek slider, play button, cover, video surface, song table, tiles, track list.

Everything is painted with the colours, radii and fonts of the toolkit's
theme. The one exception is the video surface: the bars around a picture
are black and subtitles are white on a dark plate in both schemes, as on
every screen that shows video.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import (QAbstractListModel, QAbstractTableModel, QModelIndex, QPoint, QRect, QRectF, QSize,
                            Qt, Signal)
from PySide6.QtGui import QFontMetrics, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QFrame, QHeaderView, QListView, QListWidget,
                               QListWidgetItem, QSizePolicy, QSlider, QStyle, QStyledItemDelegate,
                               QStyleOptionSlider, QTableView, QToolTip, QWidget)

import boswas_fluent as fluent
from boswas_fluent import paint

from . import glyphs
from .covers import Artwork
from .library import Song
from .model import Track, format_time

# Keys the lists hand on to the window instead of using them themselves (play/pause, mute, full screen, seek).
PASS_KEYS = (Qt.Key.Key_Space, Qt.Key.Key_M, Qt.Key.Key_F, Qt.Key.Key_Left, Qt.Key.Key_Right)


def _accent_text() -> str:
    try:
        fluent.theme().color("accent-text")
        return "accent-text"
    except Exception:           # the theme has no such derived colour: the accent itself
        return "accent"


# --- transport ---------------------------------------------------------------------------------------------

class SeekSlider(QSlider):
    """The position in the media. A click jumps there; hovering shows the time under the pointer.

    ``seek_requested(ms)`` is emitted when the user lets go after dragging,
    clicks on the rail or uses the keyboard; ``preview(ms)`` while dragging.
    """

    seek_requested = Signal(int)
    preview = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setRange(0, 0)
        self.setSingleStep(10_000)
        self.setPageStep(60_000)
        self.setMouseTracking(True)
        self.setTracking(False)
        self.setAccessibleName("Seek")
        self._dragging = False
        self.hover_text = ""
        self.actionTriggered.connect(self._action)
        self.sliderMoved.connect(self.preview)

    def is_dragging(self) -> bool:
        return self._dragging

    def set_position(self, position_ms: int) -> None:
        """Show the playback position (ignored while the user holds the handle)."""
        if not self._dragging:
            blocked = self.blockSignals(True)
            self.setValue(max(0, int(position_ms)))
            self.blockSignals(blocked)

    def set_duration(self, duration_ms: int) -> None:
        blocked = self.blockSignals(True)
        self.setRange(0, max(0, int(duration_ms)))
        self.blockSignals(blocked)
        self.setEnabled(duration_ms > 0)

    def time_at(self, x: int) -> int:
        """The time the point ``x`` of the rail stands for."""
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self)
        span = max(1, groove.width() - handle.width())
        offset = x - groove.left() - handle.width() // 2
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), max(0, min(span, offset)), span)

    def _action(self, action: int) -> None:
        if action != QSlider.SliderAction.SliderMove.value and not self._dragging:
            self.seek_requested.emit(self.sliderPosition())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            self._dragging = True
            value = self.time_at(int(event.position().x()))
            self.setSliderPosition(value)
            self.preview.emit(value)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        x = int(event.position().x())
        if self._dragging:
            value = self.time_at(x)
            self.setSliderPosition(value)
            self.preview.emit(value)
        if self.maximum() > 0:
            self.hover_text = format_time(self.time_at(x))
            QToolTip.showText(self.mapToGlobal(QPoint(x, -28)), self.hover_text, self)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            value = self.sliderPosition()
            blocked = self.blockSignals(True)
            self.setValue(value)
            self.blockSignals(blocked)
            self.seek_requested.emit(value)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        self.hover_text = ""
        QToolTip.hideText()
        super().leaveEvent(event)


class PlayButton(QAbstractButton):
    """The round play/pause button: the accent colour with the glyph in the colour of text on accent."""

    def __init__(self, parent: QWidget | None = None, *, size: int = 40) -> None:
        super().__init__(parent)
        self._playing = False
        self.setFixedSize(size, size)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.set_playing(False)

    def set_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        text = "Pause" if playing else "Play"
        self.setToolTip(f"{text} (Ctrl+P)")
        self.setAccessibleName(text)
        self.update()

    def is_playing(self) -> bool:
        return self._playing

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if not self.isEnabled():
            fill = "accent-disabled"
        elif self.isDown():
            fill = "accent-pressed"
        elif self.underMouse():
            fill = "accent-hover"
        else:
            fill = "accent"
        box = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        paint.rounded_rect(painter, box, box.width() / 2, fill)
        side = self.width() * 0.42
        glyph = QRectF(self.width() / 2 - side / 2, self.height() / 2 - side / 2, side, side)
        glyphs.paint(painter, glyph, "pause" if self._playing else "play",
                     "on-accent" if self.isEnabled() else "text-disabled")


class CoverView(QWidget):
    """A square picture with round corners; a glyph on a quiet surface when there is no picture."""

    clicked = Signal()

    def __init__(self, size: int = 48, parent: QWidget | None = None, *, glyph: str = "music",
                 clickable: bool = False) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._glyph = glyph
        self._clickable = clickable
        self._fixed = size
        if size > 0:
            self.setFixedSize(size, size)
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap if pixmap is not None and not pixmap.isNull() else None
        self.update()

    def has_picture(self) -> bool:
        return self._pixmap is not None

    def set_glyph(self, glyph: str) -> None:
        self._glyph = glyph
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._clickable and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit()

    def keyPressEvent(self, event) -> None:
        if self._clickable and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        side = min(self.width(), self.height())
        box = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        paint_cover(painter, box, self._pixmap, self._glyph)


def paint_cover(painter: QPainter, box: QRectF, pixmap: QPixmap | None, glyph: str, *, round_: bool = False) -> None:
    """A picture clipped to a rounded rectangle (or a circle), or a placeholder with ``glyph``."""
    radius = min(box.width(), box.height()) / 2 if round_ else fluent.theme().radius("card")
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if pixmap is not None and not pixmap.isNull():
        path = QPainterPath()
        path.addRoundedRect(box, radius, radius)
        painter.setClipPath(path)
        source = QRectF(pixmap.rect())
        ratio = box.width() / box.height()
        if source.width() / source.height() > ratio:
            width = source.height() * ratio
            source = QRectF((source.width() - width) / 2, 0, width, source.height())
        else:
            height = source.width() / ratio
            source = QRectF(0, (source.height() - height) / 2, source.width(), height)
        painter.drawPixmap(box, pixmap, source)
        painter.setClipping(False)
        paint.rounded_rect(painter, box, radius, None, "stroke-surface")
    else:
        paint.rounded_rect(painter, box, radius, "fill-control", "stroke-surface")
        side = min(box.width(), box.height()) * 0.38
        glyphs.paint(painter, QRectF(box.center().x() - side / 2, box.center().y() - side / 2, side, side), glyph,
                     "text-muted")
    painter.restore()


class VideoSurface(QWidget):
    """Shows the frames of the player, letterboxed, with the subtitle line on top."""

    clicked = Signal()
    double_clicked = Signal()
    activity = Signal()                   # the pointer moved over the picture

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame: object = None
        self._image: QImage | None = None
        self._subtitle = ""
        self._inset = 0
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setAccessibleName("Video")

    def set_frame(self, frame: object) -> None:
        """A QVideoFrame or QImage from the backend; None clears the picture."""
        self._frame = frame
        self._image = frame if isinstance(frame, QImage) else None
        self.update()

    def has_frame(self) -> bool:
        return self._frame is not None

    def set_subtitle(self, text: str) -> None:
        text = text.strip()[:600]
        if text != self._subtitle:
            self._subtitle = text
            self.update()

    def subtitle(self) -> str:
        return self._subtitle

    def set_bottom_inset(self, pixels: int) -> None:
        """Keep the subtitle line above something that covers the bottom of the picture (the controls)."""
        if pixels != self._inset:
            self._inset = max(0, pixels)
            self.update()

    def image(self) -> QImage | None:
        """The current frame as an image (converted when first needed)."""
        if self._image is None and self._frame is not None:
            image = self._frame.toImage()
            self._image = image if not image.isNull() else None
        return self._image

    def mouseMoveEvent(self, event) -> None:
        self.activity.emit()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        image = self.image()
        if image is not None and image.width() > 0 and image.height() > 0:
            size = image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2, size.width(),
                           size.height())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(target, image)
        if self._subtitle:
            font = fluent.font("subtitle" if self.height() >= 480 else "body-large")
            painter.setFont(font)
            metrics = QFontMetrics(font)
            width = min(self.width() - 48, 960)
            flags = int(Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap)
            bounds = metrics.boundingRect(QRect(0, 0, width, self.height()), flags, self._subtitle)
            plate = QRect((self.width() - bounds.width()) // 2 - 12,
                          self.height() - self._inset - bounds.height() - 48,
                          bounds.width() + 24, bounds.height() + 12)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setOpacity(0.72)
            painter.setBrush(Qt.GlobalColor.black)
            painter.drawRoundedRect(plate, 4, 4)
            painter.setOpacity(1.0)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(plate.adjusted(12, 6, -12, -6), flags, self._subtitle)


# --- the song table ----------------------------------------------------------------------------------------

COLUMNS = ("Title", "Artist", "Album", "Year", "Genre", "Length")


class SongModel(QAbstractTableModel):
    """The songs of the library as a table. ``current`` (a path) marks the row that is playing."""

    def __init__(self, parent=None, columns: tuple[str, ...] = COLUMNS) -> None:
        super().__init__(parent)
        self.columns = columns
        self.songs: list[Song] = []
        self.current = ""
        self._sort = (0, Qt.SortOrder.AscendingOrder)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.songs)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.columns):
            if role == Qt.ItemDataRole.DisplayRole:
                return self.columns[section]
            if role == Qt.ItemDataRole.TextAlignmentRole:
                right = self.columns[section] in ("Length", "Year")
                return int((Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft)
                           | Qt.AlignmentFlag.AlignVCenter)
        return None

    @staticmethod
    def text(song: Song, column: str) -> str:
        if column == "Title":
            return song.display_title
        if column == "Artist":
            return song.display_artist
        if column == "Album":
            return song.display_album
        if column == "Year":
            return str(song.year) if song.year else ""
        if column == "Genre":
            return song.genre
        if column == "Length":
            return format_time(song.duration_ms) if song.duration_ms else ""
        if column == "#":
            return str(song.track) if song.track else ""
        return ""

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.songs):
            return None
        song = self.songs[index.row()]
        column = self.columns[index.column()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self.text(song, column)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{column}: {self.text(song, column)}"
        if role == Qt.ItemDataRole.TextAlignmentRole:
            right = column in ("Length", "Year", "#")
            return int((Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft)
                       | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole:
            if song.path == self.current:
                return fluent.qcolor(_accent_text())
            if column not in ("Title",):
                return fluent.qcolor("text-secondary")
        if role == Qt.ItemDataRole.FontRole and song.path == self.current and column == "Title":
            return fluent.font("body-strong")
        if role == Qt.ItemDataRole.DecorationRole and song.path == self.current and column == "Title":
            return glyphs.icon("play", _accent_text())
        return None

    def set_songs(self, songs: list[Song]) -> None:
        self.beginResetModel()
        self.songs = list(songs)
        self._apply_sort()
        self.endResetModel()

    def set_current(self, path: str) -> None:
        if path != self.current:
            self.current = path
            if self.songs:
                self.dataChanged.emit(self.index(0, 0), self.index(len(self.songs) - 1, len(self.columns) - 1))

    def _apply_sort(self) -> None:
        column, order = self._sort
        if column < 0 or column >= len(self.columns):
            return
        name = self.columns[column]
        if name == "Length":
            key = lambda song: song.duration_ms                                  # noqa: E731
        elif name == "Year":
            key = lambda song: song.year                                         # noqa: E731
        elif name == "#":
            key = lambda song: (song.disc, song.track)                           # noqa: E731
        elif name == "Album":
            key = lambda song: (song.display_album.casefold(), song.disc, song.track)   # noqa: E731
        else:
            key = lambda song: self.text(song, name).casefold()                  # noqa: E731
        self.songs.sort(key=key, reverse=order == Qt.SortOrder.DescendingOrder)

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        self.layoutAboutToBeChanged.emit()
        self._sort = (column, order)
        self._apply_sort()
        self.layoutChanged.emit()

    def unsorted(self) -> None:
        self._sort = (-1, Qt.SortOrder.AscendingOrder)


class TrackTable(QTableView):
    """A table of songs: double click or Enter plays from that row; a context menu for the selection."""

    play_requested = Signal(int)                    # row
    menu_requested = Signal(list, object)           # list[Song], global position

    def __init__(self, parent: QWidget | None = None, columns: tuple[str, ...] = COLUMNS, *,
                 sortable: bool = True) -> None:
        super().__init__(parent)
        self.songs = SongModel(self, columns)
        self.setModel(self.songs)
        self.setShowGrid(False)
        self.verticalHeader().hide()
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrap(False)
        self.setTabKeyNavigation(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.doubleClicked.connect(lambda index: self.play_requested.emit(index.row()))
        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setMinimumSectionSize(48)
        widths = {"Title": 0, "Artist": 180, "Album": 200, "Year": 64, "Genre": 120, "Length": 72, "#": 48}
        for section, name in enumerate(columns):
            if name == "Title":
                header.setSectionResizeMode(section, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(section, QHeaderView.ResizeMode.Interactive)
                self.setColumnWidth(section, widths.get(name, 120))
        if sortable:
            self.setSortingEnabled(True)
            self.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        else:
            self.songs.unsorted()
        self.setAccessibleName("Songs")
        # Qt's corner button (select all) has no name of its own. It never shows (the vertical header
        # is hidden), but accessibility tools still list it.
        corner = self.findChild(QAbstractButton, "", Qt.FindChildOption.FindDirectChildrenOnly)
        if corner is not None:
            corner.setAccessibleName("Select all songs")

    def selected_songs(self) -> list[Song]:
        rows = sorted({index.row() for index in self.selectionModel().selectedRows()})
        if not rows and self.currentIndex().isValid():
            rows = [self.currentIndex().row()]
        return [self.songs.songs[row] for row in rows if 0 <= row < len(self.songs.songs)]

    def _menu(self, position: QPoint) -> None:
        index = self.indexAt(position)
        if index.isValid() and not self.selectionModel().isRowSelected(index.row(), QModelIndex()):
            self.selectRow(index.row())
        songs = self.selected_songs()
        if songs:
            self.menu_requested.emit(songs, self.viewport().mapToGlobal(position))

    def keyPressEvent(self, event) -> None:
        if event.key() in PASS_KEYS and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            event.ignore()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentIndex().isValid():
            self.play_requested.emit(self.currentIndex().row())
            return
        if event.key() == Qt.Key.Key_Menu and self.currentIndex().isValid():
            rect = self.visualRect(self.currentIndex())
            self.menu_requested.emit(self.selected_songs(), self.viewport().mapToGlobal(rect.bottomLeft()))
            return
        super().keyPressEvent(event)


# --- tiles ---------------------------------------------------------------------------------------------------

@dataclass
class Tile:
    """One tile of a grid: an album, an artist, a video or something played recently."""

    kind: str                     # album, artist, video, track
    title: str
    subtitle: str = ""
    source: str = ""              # the file whose cover or thumbnail is shown ("" for none)
    payload: object = None


_TILE = {
    # kind: (cell width, cell height, picture width, picture height)
    "album": (184, 236, 160, 160), "track": (184, 236, 160, 160), "artist": (184, 220, 144, 144),
    "video": (272, 216, 248, 140),
}
TILE_ROLE = Qt.ItemDataRole.UserRole


class TileModel(QAbstractListModel):
    def __init__(self, artwork: Artwork, parent=None) -> None:
        super().__init__(parent)
        self.tiles: list[Tile] = []
        self.artwork = artwork
        artwork.loaded.connect(self._picture_loaded)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.tiles)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.tiles):
            return None
        tile = self.tiles[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return tile.title
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{tile.title}\n{tile.subtitle}" if tile.subtitle else tile.title
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{tile.title}, {tile.subtitle}" if tile.subtitle else tile.title
        if role == TILE_ROLE:
            return tile
        return None

    def set_tiles(self, tiles: list[Tile]) -> None:
        self.beginResetModel()
        self.tiles = list(tiles)
        self.endResetModel()

    def picture(self, tile: Tile) -> QPixmap | None:
        if not tile.source or tile.kind == "artist":
            return None
        _cell_w, _cell_h, width, _height = _TILE[tile.kind]
        if tile.kind == "video" or (tile.kind == "track" and getattr(tile.payload, "video", False)):
            return self.artwork.thumbnail(tile.source, 320)
        return self.artwork.cover(tile.source, 320 if width > 150 else 160)

    def _picture_loaded(self, key: str) -> None:
        path = key.rsplit("\x1f", 1)[-1]
        for row, tile in enumerate(self.tiles):
            if tile.source == path:
                index = self.index(row, 0)
                self.dataChanged.emit(index, index)


class TileDelegate(QStyledItemDelegate):
    def __init__(self, view: "TileView") -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        tile = index.data(TILE_ROLE)
        width, height, _pw, _ph = _TILE[tile.kind if tile is not None else "album"]
        return QSize(width, height)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        tile: Tile | None = index.data(TILE_ROLE)
        if tile is None:
            return
        cell_w, _cell_h, width, height = _TILE[tile.kind]
        rect = QRectF(option.rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if hovered or selected:
            paint.rounded_rect(painter, rect.adjusted(2, 2, -2, -2), fluent.theme().radius("card"),
                               "fill-subtle-pressed" if hovered and selected else "fill-subtle-hover")
        left = rect.left() + (cell_w - width) / 2
        box = QRectF(left, rect.top() + 12, width, height)
        glyph = {"video": "video", "artist": "artist"}.get(tile.kind, "music")
        if tile.kind == "track" and getattr(tile.payload, "video", False):
            glyph = "video"
        if tile.kind == "track" and getattr(tile.payload, "is_remote", False):
            glyph = "stream"
        model = index.model()
        pixmap = model.picture(tile) if isinstance(model, TileModel) else None
        paint_cover(painter, box, pixmap, glyph, round_=tile.kind == "artist")
        centre = tile.kind == "artist"
        flags = (Qt.AlignmentFlag.AlignHCenter if centre else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter
        text_left, text_width = (rect.left() + 8, cell_w - 16) if centre else (left, width)
        paint.elided(painter, QRectF(text_left, box.bottom() + 8, text_width, 20), tile.title, fluent.font("body"),
                     "text", flags)
        if tile.subtitle:
            paint.elided(painter, QRectF(text_left, box.bottom() + 28, text_width, 16), tile.subtitle,
                         fluent.font("caption"), "text-secondary", flags)
        painter.restore()


class TileView(QListView):
    """A grid of tiles. A click (or Enter) opens a tile; the context menu belongs to the page."""

    tile_activated = Signal(object)                 # Tile
    menu_requested = Signal(object, object)         # Tile, global position

    def __init__(self, artwork: Artwork, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tiles = TileModel(artwork, self)
        self.setModel(self.tiles)
        self.setItemDelegate(TileDelegate(self))
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setSpacing(0)
        self.setUniformItemSizes(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.clicked.connect(self._activate)

    def set_tiles(self, tiles: list[Tile]) -> None:
        self.tiles.set_tiles(tiles)

    def _activate(self, index: QModelIndex) -> None:
        tile = index.data(TILE_ROLE)
        if tile is not None:
            self.tile_activated.emit(tile)

    def _menu(self, position: QPoint) -> None:
        index = self.indexAt(position)
        tile = index.data(TILE_ROLE) if index.isValid() else None
        if tile is not None:
            self.setCurrentIndex(index)
            self.menu_requested.emit(tile, self.viewport().mapToGlobal(position))

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_M, Qt.Key.Key_F) and not event.modifiers():
            event.ignore()
            return
        index = self.currentIndex()
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and index.isValid():
            self._activate(index)
            return
        if event.key() == Qt.Key.Key_Menu and index.isValid():
            self.menu_requested.emit(index.data(TILE_ROLE), self.viewport().mapToGlobal(
                self.visualRect(index).center()))
            return
        super().keyPressEvent(event)


# --- the list of a queue or a playlist ---------------------------------------------------------------------

TRACK_ROLE = Qt.ItemDataRole.UserRole


class _TrackDelegate(QStyledItemDelegate):
    ROW = 48

    def __init__(self, view: "TrackList") -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), self.ROW)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        track: Track | None = index.data(TRACK_ROLE)
        if track is None:
            return
        rect = QRectF(option.rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            paint.rounded_rect(painter, rect.adjusted(2, 1, -2, -1), fluent.theme().radius("control"),
                               "fill-subtle-pressed" if selected and hovered else "fill-subtle-hover")
        current = index.row() == self._view.current_row
        colour = _accent_text() if current else "text"
        lead = QRectF(rect.left() + 8, rect.top(), 32, rect.height())
        if current:
            glyphs.paint(painter, QRectF(lead.center().x() - 7, lead.center().y() - 7, 14, 14),
                         "pause" if self._view.playing else "play", colour)
        else:
            paint.elided(painter, lead, str(index.row() + 1), fluent.font("caption"), "text-muted",
                         Qt.AlignmentFlag.AlignCenter)
        duration = format_time(track.duration_ms) if track.duration_ms else ""
        right = 72 if duration else 12
        text = QRectF(rect.left() + 48, rect.top() + 6, rect.width() - 48 - right, 18)
        paint.elided(painter, text, track.display_title, fluent.font("body-strong" if current else "body"), colour)
        parts = [part for part in (track.subtitle, "" if track.is_remote else track.album) if part]
        paint.elided(painter, text.translated(0, 20), " · ".join(parts), fluent.font("caption"), "text-secondary")
        if duration:
            paint.elided(painter, QRectF(rect.right() - 72, rect.top(), 60, rect.height()), duration,
                         fluent.font("caption"), "text-secondary",
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        painter.restore()


class TrackList(QListWidget):
    """Tracks in an order the user can change: drag a row, or Alt+Up and Alt+Down.

    The list never reorders itself: it asks with ``move_requested`` and is
    filled again by whoever owns the order.
    """

    play_requested = Signal(int)
    move_requested = Signal(int, int)               # from row, to row
    remove_requested = Signal(list)                 # rows, descending
    menu_requested = Signal(list, object)           # rows, global position

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_row = -1
        self.playing = False
        self.setItemDelegate(_TrackDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemDoubleClicked.connect(lambda item: self.play_requested.emit(self.row(item)))

    def set_tracks(self, tracks: list[Track], current_row: int = -1, playing: bool = False) -> None:
        """Show ``tracks``; the selection and the scroll position stay where they were."""
        selected = self.currentRow()
        scroll = self.verticalScrollBar().value()
        self.current_row, self.playing = current_row, playing
        self.setUpdatesEnabled(False)
        try:
            while self.count() > len(tracks):
                self.takeItem(self.count() - 1)
            for row, track in enumerate(tracks):
                item = self.item(row)
                if item is None:
                    item = QListWidgetItem()
                    self.addItem(item)
                item.setData(TRACK_ROLE, track)
                item.setText(track.display_title)
                item.setToolTip(track.location)
        finally:
            self.setUpdatesEnabled(True)
        if 0 <= selected < self.count():
            self.setCurrentRow(selected)
        self.verticalScrollBar().setValue(scroll)
        self.viewport().update()

    def set_current(self, current_row: int, playing: bool) -> None:
        if (current_row, playing) != (self.current_row, self.playing):
            self.current_row, self.playing = current_row, playing
            self.viewport().update()

    def selected_rows(self) -> list[int]:
        rows = sorted({self.row(item) for item in self.selectedItems()})
        if not rows and self.currentRow() >= 0:
            rows = [self.currentRow()]
        return rows

    def request_move(self, source: int, target: int) -> None:
        if 0 <= source < self.count() and 0 <= target < self.count() and source != target:
            self.move_requested.emit(source, target)
            self.setCurrentRow(target)

    def _menu(self, position: QPoint) -> None:
        item = self.itemAt(position)
        if item is not None and not item.isSelected():
            self.setCurrentItem(item)
        rows = self.selected_rows()
        if rows:
            self.menu_requested.emit(rows, self.viewport().mapToGlobal(position))

    def dropEvent(self, event) -> None:
        if event.source() is not self:
            event.ignore()
            return
        source = self.currentRow()
        position = event.position().toPoint()
        item = self.itemAt(position)
        if item is None:
            target = self.count() - 1
        else:
            target = self.row(item)
            below = position.y() > self.visualItemRect(item).center().y()
            if below and target < source:
                target += 1
            elif not below and target > source:
                target -= 1
        event.setDropAction(Qt.DropAction.IgnoreAction)       # the owner of the order moves the row, not Qt
        event.accept()
        self.request_move(source, target)

    def keyPressEvent(self, event) -> None:
        key, modifiers = event.key(), event.modifiers()
        row = self.currentRow()
        if key in (Qt.Key.Key_Space, Qt.Key.Key_M, Qt.Key.Key_F, Qt.Key.Key_Left, Qt.Key.Key_Right) and not modifiers:
            event.ignore()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and row >= 0:
            self.play_requested.emit(row)
            return
        if key == Qt.Key.Key_Delete and self.selected_rows():
            self.remove_requested.emit(sorted(self.selected_rows(), reverse=True))
            return
        if modifiers == Qt.KeyboardModifier.AltModifier and key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and row >= 0:
            self.request_move(row, row + (-1 if key == Qt.Key.Key_Up else 1))
            return
        if key == Qt.Key.Key_Menu and row >= 0:
            self.menu_requested.emit(self.selected_rows(),
                                     self.viewport().mapToGlobal(self.visualItemRect(self.item(row)).center()))
            return
        super().keyPressEvent(event)
