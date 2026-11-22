"""The look of the desktop (colour mode, background, motion) and two rows about time without input.

Category "Personalization":
  Colour mode   (page colours) Dark or Light: [appearance] scheme. When the
                background is a Boswas wallpaper, its artwork for the other
                mode is chosen with it, as a preset does.
  Background    (page background) the Boswas wallpapers and the pictures of
                the user's Pictures folder as thumbnails
                (boswas_control_center.wallpapers), any PNG or JPEG file
                through "Browse", or the default background: [appearance]
                wallpaper.
  Motion        (Accessibility > Visual effects) animation effects:
                [appearance] reduced_motion.
Page "lock-screen" (Privacy & security):
  Lock screen   "Lock automatically after": [lock] idle_seconds, limited by
                the administrator's [policy] lock_idle_max_seconds.
Page "power":
  Screen        "Turn off the screen after": [power] screen_off_seconds.

Scheme and wallpaper go to appearance.toml, the file boswas-preset writes;
compositorconfig explains what goes where. Control Center itself follows a
new colour mode through the toolkit, which watches that file.

Thumbnails are decoded on a worker thread (load_thumbnail) and kept in
memory; no file is written.
"""

from __future__ import annotations

import os

import boswas_fluent as fluent
from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QImage, QImageReader, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QFileDialog, QHBoxLayout, QSizePolicy, QVBoxLayout,
                               QWidget)
from shiboken6 import isValid

from ... import compositorconfig as cc
from ... import wallpapers
from .display import ConfigSection, combo, select

THUMBNAIL = QSize(320, 180)                    # decoded size; tiles show it smaller
TILE_WIDTH, TILE_HEIGHT, TILE_GAP = 168, 96, 8
TIMES = ((60, "1 minute"), (120, "2 minutes"), (300, "5 minutes"), (600, "10 minutes"), (900, "15 minutes"),
         (1800, "30 minutes"), (0, "Never"))
_thumbnails = wallpapers.ThumbnailCache()


def duration_text(seconds: int) -> str:
    """A time for people: "45 seconds", "1 minute", "7 minutes", "2 hours"."""
    if seconds == 0:
        return "Never"
    for size, word in ((3600, "hour"), (60, "minute")):
        if seconds % size == 0:
            count = seconds // size
            return f"{count} {word}{'s' if count != 1 else ''}"
    return f"{seconds} seconds"


def load_thumbnail(path: str) -> QImage:
    """Decode a picture at thumbnail size. Runs on a worker thread (QImage, never QPixmap)."""
    key = wallpapers.stamp(path)
    if key is None or not wallpapers.picture_kind(path):
        raise ValueError(f"{path} is not a readable picture")

    def decode() -> QImage:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if not size.isValid() or size.width() > 32768 or size.height() > 32768:
            raise ValueError(f"{path} is not a readable picture")
        reader.setScaledSize(size.scaled(THUMBNAIL, Qt.AspectRatioMode.KeepAspectRatioByExpanding))
        image = reader.read()
        if image.isNull():
            raise ValueError(f"{path} is not a readable picture ({reader.errorString()})")
        return image
    return _thumbnails.get(key, decode)


def _rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


class Tile(QAbstractButton):
    """A picture with a name under it that can be chosen; the chosen one has the accent frame and a check mark."""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(name)
        self.setCheckable(True)
        self.setAccessibleName(name)
        self.setToolTip(name)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.toggled.connect(lambda on: self.setAccessibleDescription("Selected" if on else ""))

    def sizeHint(self) -> QSize:
        return QSize(TILE_WIDTH, TILE_HEIGHT + 26)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            return
        super().keyPressEvent(event)

    def picture_rect(self) -> QRectF:
        return QRectF(2, 2, self.width() - 4, TILE_HEIGHT - 4)

    def paint_picture(self, painter: QPainter, box: QRectF) -> None:
        """Draw the picture into ``box`` (clipped to rounded corners by the caller)."""

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = self.picture_rect()
        painter.save()
        painter.setClipPath(_rounded(box, 6))
        self.paint_picture(painter, box)
        if self.isDown() or (self.underMouse() and not self.isChecked()):
            painter.fillRect(box, fluent.qcolor("fill-subtle-pressed" if self.isDown() else "fill-subtle-hover"))
        painter.restore()
        if self.isChecked():
            fluent.paint.rounded_rect(painter, box.adjusted(-2, -2, 2, 2), 8, None, "accent", 2)
            mark = QRectF(box.right() - 24, box.top() + 6, 18, 18)
            fluent.paint.rounded_rect(painter, mark, 9, "accent", "surface", 1.5)
            fluent.paint.glyph(painter, mark, "check", "on-accent", 10)
        else:
            fluent.paint.rounded_rect(painter, box, 6, None, "stroke-surface")
        label = QRectF(2, TILE_HEIGHT + 2, self.width() - 4, 22)
        fluent.paint.elided(painter, label, self.text(), fluent.font("body-strong" if self.isChecked() else "body"),
                            "text", Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)


class SchemeTile(Tile):
    """A small picture of the desktop in one colour mode, drawn in that mode's own colours."""

    def __init__(self, scheme: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(name, parent)
        self.scheme = scheme

    def sizeHint(self) -> QSize:
        return QSize(TILE_WIDTH + 24, TILE_HEIGHT + 26)

    def paint_picture(self, painter: QPainter, box: QRectF) -> None:
        theme = fluent.Theme(self.scheme)

        def colour(name: str):
            return fluent.qcolor(name, theme)
        painter.fillRect(box, colour("background"))
        painter.setPen(Qt.PenStyle.NoPen)
        window = QRectF(box.left() + 18, box.top() + 12, box.width() - 62, box.height() - 34)
        painter.setBrush(colour("card"))
        painter.drawRoundedRect(window, 4, 4)
        painter.setBrush(colour("text"))
        painter.drawRoundedRect(QRectF(window.left() + 8, window.top() + 8, 44, 4), 2, 2)
        painter.setBrush(colour("text-muted"))
        for line, width in enumerate((window.width() - 30, window.width() - 46)):
            painter.drawRoundedRect(QRectF(window.left() + 8, window.top() + 20 + line * 9, width, 3), 1.5, 1.5)
        painter.setBrush(colour("accent"))
        painter.drawRoundedRect(QRectF(window.right() - 38, window.bottom() - 14, 30, 8), 4, 4)
        bar = QRectF(box.left(), box.bottom() - 12, box.width(), 12)
        painter.fillRect(bar, colour("layer"))
        painter.setBrush(colour("accent"))
        painter.drawRoundedRect(QRectF(bar.center().x() - 16, bar.top() + 4, 8, 4), 2, 2)
        painter.setBrush(colour("text-muted"))
        for index in (1, 2):
            painter.drawRoundedRect(QRectF(bar.center().x() - 16 + index * 12, bar.top() + 4, 8, 4), 2, 2)


class WallpaperTile(Tile):
    def __init__(self, wallpaper: wallpapers.Wallpaper, parent: QWidget | None = None) -> None:
        super().__init__(wallpaper.name, parent)
        self.wallpaper = wallpaper
        self.pixmap: QPixmap | None = None
        self.failed = False

    def set_image(self, image: QImage) -> None:
        self.pixmap = QPixmap.fromImage(image)
        self.update()

    def set_failed(self) -> None:
        self.failed = True
        self.pixmap = QPixmap()
        self.setToolTip(f"{self.wallpaper.name} (the picture cannot be shown)")
        self.update()

    def paint_picture(self, painter: QPainter, box: QRectF) -> None:
        painter.fillRect(box, fluent.qcolor("fill-control"))
        if self.pixmap is None or self.pixmap.isNull():
            if self.failed:
                centre = box.center()
                fluent.paint.status_badge(painter, QRectF(centre.x() - 9, centre.y() - 9, 18, 18), "warning")
            return
        scaled = self.pixmap.size().scaled(box.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        target = QRectF(box.center().x() - scaled.width() / 2, box.center().y() - scaled.height() / 2,
                        scaled.width(), scaled.height())
        painter.drawPixmap(target, self.pixmap, QRectF(self.pixmap.rect()))


class TileGrid(QWidget):
    """Tiles in rows, as many per row as fit the width."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tiles: list[Tile] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_tiles(self, tiles: list[Tile]) -> None:
        for tile in self.tiles:
            if tile not in tiles:
                tile.deleteLater()
        self.tiles = list(tiles)
        for tile in tiles:
            tile.setParent(self)
            tile.show()
        self._place()
        self.updateGeometry()

    def _columns(self, width: int) -> int:
        return max(1, (width + TILE_GAP) // (TILE_WIDTH + TILE_GAP))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        rows = -(-len(self.tiles) // self._columns(width))
        return rows * (TILE_HEIGHT + 26) + max(0, rows - 1) * TILE_GAP

    def sizeHint(self) -> QSize:
        width = max(self.width(), TILE_WIDTH)
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        return QSize(TILE_WIDTH, self.heightForWidth(max(self.width(), TILE_WIDTH)))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place()
        self.updateGeometry()

    def _place(self) -> None:
        columns = self._columns(self.width())
        for index, tile in enumerate(self.tiles):
            tile.setGeometry((index % columns) * (TILE_WIDTH + TILE_GAP),
                             (index // columns) * (TILE_HEIGHT + 26 + TILE_GAP), TILE_WIDTH, TILE_HEIGHT + 26)


class ColourModeSection(ConfigSection):
    page = "colours"
    title = "Colour mode"
    order = 10
    keywords = ("dark mode", "light mode", "colour mode", "color mode", "theme", "appearance", "night")
    reports_files = True

    def __init__(self, parent: QWidget | None = None, *, config: cc.Settings | None = None,
                 wallpaper_dir: str | None = None) -> None:
        self.wallpaper_dir = wallpaper_dir or wallpapers.SYSTEM_DIR
        super().__init__(parent, config=config)

    def build(self) -> None:
        card = self.add(fluent.Card(self, padding=16, spacing=8))
        card.add(fluent.Label("Choose how windows, the taskbar and applications look", "body", card))
        row = QHBoxLayout()
        row.setSpacing(16)
        self.group = QButtonGroup(self)
        self.tiles = {}
        for scheme, name in (("dark", "Dark"), ("light", "Light")):
            tile = SchemeTile(scheme, name, card)
            tile.clicked.connect(lambda _on=False, scheme=scheme: self.choose(scheme))
            self.group.addButton(tile)
            self.tiles[scheme] = tile
            row.addWidget(tile)
        row.addStretch(1)
        card.add_layout(row)

    def choose(self, scheme: str) -> None:
        self.change({("appearance", "scheme"): scheme})

    def perform(self, changes: dict) -> object:
        scheme = changes.get(("appearance", "scheme"))
        if scheme is not None:
            # A Boswas wallpaper comes as light and dark artwork: it follows the mode.
            current = self.config.snapshot().get("appearance", "wallpaper")
            other = wallpapers.counterpart(current, scheme, self.wallpaper_dir)
            if other and other != current:
                changes = {**changes, ("appearance", "wallpaper"): other}
        return self.config.apply(changes)

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.tiles[snapshot.get("appearance", "scheme")].setChecked(True)


class BackgroundSection(ConfigSection):
    page = "background"
    title = "Background"
    order = 20
    keywords = ("background", "wallpaper", "desktop picture", "photo", "image")

    thumbnails_done = Signal()

    def __init__(self, parent: QWidget | None = None, *, config: cc.Settings | None = None,
                 wallpaper_dir: str | None = None, choose_file=None) -> None:
        self.wallpaper_dir = wallpaper_dir or wallpapers.SYSTEM_DIR
        self.choose_file = choose_file or self._ask_for_file
        self.tiles: list[WallpaperTile] = []
        self.current = ""
        self._listed: tuple = ()
        super().__init__(parent, config=config)
        self.worker = fluent.Worker(self, max_threads=2)

    def build(self) -> None:
        self.browse = fluent.Button("Browse…")
        self.browse.setAccessibleName("Browse for a picture")
        self.browse.clicked.connect(self._browse)
        self.default = fluent.Button("Use the default background")
        self.default.clicked.connect(lambda: self.change({("appearance", "wallpaper"): None}))
        self.header = self.card("Background picture", "", "image-x-generic-symbolic", self.browse)
        self.header.add_control(self.default)
        self.grid_card = self.add(fluent.Card(self, padding=16, spacing=8))
        self.boswas_label = fluent.Label("Boswas wallpapers", "body-strong", self.grid_card)
        self.boswas_grid = TileGrid(self.grid_card)
        self.pictures_label = fluent.Label("Your pictures", "body-strong", self.grid_card)
        self.pictures_grid = TileGrid(self.grid_card)
        self.empty = fluent.Label("No wallpapers are installed and your Pictures folder has no PNG or JPEG files. "
                                  "Use Browse to choose a picture.", "body", self.grid_card, tone="secondary",
                                  wrap=True)
        for widget in (self.boswas_label, self.boswas_grid, self.pictures_label, self.pictures_grid, self.empty):
            self.grid_card.add(widget)
        self.grid_card.hide()

    # --- state ---
    def load(self) -> object:
        snapshot = self.config.snapshot()
        scheme = snapshot.get("appearance", "scheme")
        found = wallpapers.available(self.config.environ, scheme, self.wallpaper_dir)
        current = snapshot.get("appearance", "wallpaper")
        if current and not any(item.matches(current) for item in found) and wallpapers.picture_kind(current):
            name = os.path.splitext(os.path.basename(current))[0][:80]
            found.append(wallpapers.Wallpaper(current, name, "pictures"))
        return snapshot, found

    def show_state(self, state: object) -> None:
        snapshot, found = state
        self.current = snapshot.get("appearance", "wallpaper")
        listed = tuple(found)
        if listed != self._listed:
            self._listed = listed
            self.tiles = [WallpaperTile(wallpaper) for wallpaper in found]
            for tile in self.tiles:
                tile.clicked.connect(lambda _on=False, tile=tile: self._chosen(tile))
            self.boswas_grid.set_tiles([tile for tile in self.tiles if tile.wallpaper.source == "boswas"])
            self.pictures_grid.set_tiles([tile for tile in self.tiles if tile.wallpaper.source != "boswas"])
            for tile in self.tiles:
                self._load(tile)
        chosen = None
        for tile in self.tiles:
            tile.setChecked(tile.wallpaper.matches(self.current))
            chosen = tile if tile.isChecked() else chosen
        for label, grid in ((self.boswas_label, self.boswas_grid), (self.pictures_label, self.pictures_grid)):
            label.setVisible(bool(grid.tiles))
            grid.setVisible(bool(grid.tiles))
        self.empty.setVisible(not self.tiles)
        self.grid_card.show()
        self.default.setEnabled(bool(self.current))
        if not self.current:
            self.header.set_description("The default Boswas background")
        elif chosen is not None:
            self.header.set_description(chosen.wallpaper.name)
        else:
            self.header.set_description(f"{self.current} (this picture cannot be read; the desktop shows the "
                                        "default background)")

    def _load(self, tile: WallpaperTile) -> None:
        def done(image: QImage) -> None:
            if isValid(self) and isValid(tile):
                tile.set_image(image)
                self._check_done()

        def failed(_exc: Exception) -> None:
            if isValid(self) and isValid(tile):
                tile.set_failed()
                self._check_done()
        self.worker.submit(load_thumbnail, tile.wallpaper.path, on_done=done, on_error=failed)

    def _check_done(self) -> None:
        if all(tile.pixmap is not None for tile in self.tiles):
            self.thumbnails_done.emit()

    # --- changes ---
    def _chosen(self, tile: WallpaperTile) -> None:
        for other in self.tiles:
            other.setChecked(other is tile)    # at once; a failure puts the real one back
        self.change({("appearance", "wallpaper"): tile.wallpaper.path})

    def _ask_for_file(self) -> str:
        start = wallpapers.pictures_dir(self.config.environ) or self.config.environ.get("HOME") or ""
        path, _filter = QFileDialog.getOpenFileName(self.window(), "Choose a picture", start,
                                                    "Pictures (*.png *.jpg *.jpeg)")
        return path

    def _browse(self) -> None:
        path = self.choose_file()
        if not path:
            return

        def done(checked: str) -> None:
            if isValid(self):
                self.change({("appearance", "wallpaper"): checked})

        def failed(exc: Exception) -> None:
            if isValid(self):
                self.notice.show_message("error", str(exc))
        fluent.run_in_thread(lambda: wallpapers.check_picture(path), on_done=done, on_error=failed)

class MotionSection(ConfigSection):
    page = "visual-effects"
    title = "Motion"
    order = 30
    keywords = ("animation", "animations", "motion", "effects", "reduce motion", "transitions")

    def build(self) -> None:
        self.effects = self.toggle("Animation effects", "Windows, menus and the taskbar fade and slide. Turn this "
                                   "off for a calmer screen", "media-playback-start-symbolic", "appearance",
                                   "reduced_motion", inverted=True)

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.effects.setChecked(not snapshot.get("appearance", "reduced_motion"))


class _TimeRow(ConfigSection):
    """One row with a list of times; ``table`` and ``key`` name the setting in seconds (0: never)."""

    table = ""
    key = ""
    row_title = ""
    row_description = ""
    row_icon = ""

    def build(self) -> None:
        self.box = combo(((label, seconds) for seconds, label in TIMES), width=180)
        self.row = self.card(self.row_title, self.row_description, self.row_icon, self.box)
        self.box.activated.connect(lambda _i: self.change({(self.table, self.key): self.box.currentData()}))

    def allowed(self, snapshot: cc.Snapshot, seconds: int) -> bool:
        return True

    def show_state(self, snapshot: cc.Snapshot) -> None:
        known = dict(TIMES)
        for index in reversed(range(self.box.count())):
            if self.box.itemData(index) not in known:
                self.box.removeItem(index)
        value = snapshot.get(self.table, self.key)
        select(self.box, value, duration_text(value))
        model = self.box.model()
        for index in range(self.box.count()):
            seconds = self.box.itemData(index)
            model.item(index).setEnabled(seconds == value or self.allowed(snapshot, seconds))


class LockScreenSection(_TimeRow):
    page = "lock-screen"
    title = "Lock screen"
    order = 80
    keywords = ("lock", "lock screen", "automatic lock", "screen lock", "idle", "timeout", "away", "password")
    reports_files = True
    table, key = "lock", "idle_seconds"
    row_title = "Lock automatically after"
    row_description = "The screen locks when the device has not been used for this long"
    row_icon = "system-lock-screen-symbolic"

    def allowed(self, snapshot: cc.Snapshot, seconds: int) -> bool:
        return snapshot.lock_allowed(seconds)

    def show_state(self, snapshot: cc.Snapshot) -> None:
        super().show_state(snapshot)
        limit = snapshot.lock_limit
        self.row.set_description(self.row_description if limit == 0 else
                                 f"Your organisation requires the screen to lock after at most {duration_text(limit)}")
        self.managed = limit > 0


class ScreenSection(_TimeRow):
    page = "power"
    title = "Screen"
    order = 60
    keywords = ("screen off", "turn off screen", "display off", "blank", "idle", "timeout", "sleep")
    reports_files = True
    table, key = "power", "screen_off_seconds"
    row_title = "Turn off the screen after"
    row_description = "The screens go dark when the device has not been used for this long; any key or the " \
                      "mouse wakes them"
    row_icon = "video-display-symbolic"


SECTIONS = (ColourModeSection, BackgroundSection, MotionSection, LockScreenSection, ScreenSection)
