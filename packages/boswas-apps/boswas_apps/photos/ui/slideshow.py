"""The slideshow page: pictures one after the other, full screen, with a cross-fade."""

from __future__ import annotations

import random

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QWidget

import boswas_fluent as fluent

from .. import decode, library
from .common import OverlayButton, action_icon

FADE_FACTOR = 3            # the cross-fade lasts this many "slow" durations of the theme
CONTROLS_MS = 2500
MAX_FAILURES = 50


class SlideshowPage(QWidget):
    """Shows ``items`` one at a time. Space pauses, the arrows step, Escape ends it (``finished``)."""

    finished = Signal()
    shown = Signal(str)               # the path of the picture that is now on screen

    def __init__(self, host, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.host = host
        self.worker = fluent.Worker(self, max_threads=1)
        self._items: list[library.Item] = []
        self._order: list[int] = []
        self._position = -1
        self._current: QImage | None = None
        self._previous: QImage | None = None
        self._fade = 1.0
        self._animation: fluent.Animation | None = None
        self._paused = False
        self._token = 0
        self._failures = 0
        self._ready: dict[int, QImage] = {}       # position in the order -> decoded picture
        self._loading: set[int] = set()
        self._wanted: int | None = None
        self._step = 1
        self._ended = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._advance)
        self._controls_timer = QTimer(self)
        self._controls_timer.setSingleShot(True)
        self._controls_timer.setInterval(CONTROLS_MS)
        self._controls_timer.timeout.connect(self._hide_controls)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setProperty("fluentFocusRing", False)
        self.setAccessibleName("Slideshow")

        self.controls = QWidget(self)
        row = QHBoxLayout(self.controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.previous_button = OverlayButton(fluent.icon("go-previous-symbolic"), "Previous picture (Left)",
                                             self.controls)
        self.pause_button = OverlayButton(action_icon("media-playback-pause"), "Pause (Space)", self.controls)
        self.next_button = OverlayButton(fluent.icon("go-next-symbolic"), "Next picture (Right)", self.controls)
        self.close_button = OverlayButton(fluent.icon("window-close-symbolic"), "End the slideshow (Esc)",
                                          self.controls)
        self.previous_button.clicked.connect(lambda: self.step(-1))
        self.pause_button.clicked.connect(self.toggle_pause)
        self.next_button.clicked.connect(lambda: self.step(1))
        self.close_button.clicked.connect(self.stop)
        for button in (self.previous_button, self.pause_button, self.next_button, self.close_button):
            row.addWidget(button)
        self.controls.adjustSize()
        self.message = fluent.Label("", "body", self, tone="secondary")
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.hide()

        for keys, slot in (("Space", self.toggle_pause), ("Left", lambda: self.step(-1)),
                           ("Right", lambda: self.step(1)), ("Escape", self.stop), ("F5", self.stop),
                           ("F11", self.stop)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)

    # --- running ---
    def start(self, items: list[library.Item], first: int = 0) -> None:
        settings = self.host.settings
        self._items = list(items)
        self._order = list(range(len(self._items)))
        if settings.slideshow_shuffle and len(self._order) > 1:
            random.shuffle(self._order)
            if 0 <= first < len(self._items):                 # the picture on show starts the round
                self._order.remove(first)
                self._order.insert(0, first)
            first_position = 0
        else:
            first_position = max(0, min(len(self._order) - 1, first))
        self._token += 1
        self._ready.clear()
        self._loading.clear()
        self._current = self._previous = None
        self._paused = False
        self._ended = False
        self._failures = 0
        self._position = -1
        self._step = 1
        self.message.hide()
        self._sync_pause()
        self._show_controls()
        if not self._items:
            self._say("There are no pictures to show.")
            return
        self._go(first_position)

    def stop(self) -> None:
        self._timer.stop()
        self._controls_timer.stop()
        self._stop_fade()
        self._token += 1
        self._wanted = None
        self._ready.clear()
        self._loading.clear()
        self._current = self._previous = None
        self.unsetCursor()
        self.finished.emit()

    def is_paused(self) -> bool:
        return self._paused

    def current_item(self) -> library.Item | None:
        if 0 <= self._position < len(self._order):
            return self._items[self._order[self._position]]
        return None

    def position(self) -> int:
        return self._position

    def order(self) -> list[int]:
        return list(self._order)

    def interval_ms(self) -> int:
        return max(1, self.host.settings.slideshow_interval) * 1000

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        if self._paused:
            self._timer.stop()
        elif self._current is not None:
            if self._ended:
                self._ended = False
                self._go(0)
            else:
                self._timer.start(self.interval_ms())
        self._sync_pause()
        self._show_controls()

    def _sync_pause(self) -> None:
        self.pause_button.setIcon(action_icon("media-playback-start" if self._paused else "media-playback-pause"))
        tip = "Continue (Space)" if self._paused else "Pause (Space)"
        self.pause_button.setToolTip(tip)
        self.pause_button.setAccessibleName(tip)
        self.pause_button.update()

    def step(self, delta: int) -> None:
        """Go to the next or previous picture by hand; the timer starts again from there."""
        if not self._order:
            return
        self._step = 1 if delta >= 0 else -1
        self._ended = False
        self._go((self._position + delta) % len(self._order))

    def _advance(self) -> None:
        if self._paused or not self._order:
            return
        following = self._position + 1
        if following >= len(self._order):
            if not self.host.settings.slideshow_loop:
                self._paused = True
                self._ended = True
                self._sync_pause()
                self._show_controls()
                return
            following = 0
        self._step = 1
        self._go(following)

    def _go(self, position: int) -> None:
        self._timer.stop()
        self._wanted = position
        ready = self._ready.pop(position, None)
        if ready is not None:
            self._display(position, ready)
        else:
            self._load(position)

    def _side(self) -> int:
        size = self.size() * self.devicePixelRatioF()
        return max(size.width(), size.height(), 640)

    def _load(self, position: int) -> None:
        if position in self._loading or not 0 <= position < len(self._order):
            return
        self._loading.add(position)
        item = self._items[self._order[position]]
        token = self._token
        self.worker.submit(decode.load_scaled, item.path, self._side(),
                           on_done=lambda result, t=token, p=position: self._loaded(t, p, result[0]),
                           on_error=lambda _exc, t=token, p=position: self._load_failed(t, p))

    def _loaded(self, token: int, position: int, image: QImage) -> None:
        if token != self._token:
            return
        self._loading.discard(position)
        self._failures = 0
        if position == self._wanted:
            self._display(position, image)
        else:
            self._ready = {position: image}            # one picture ahead is kept

    def _load_failed(self, token: int, position: int) -> None:
        if token != self._token:
            return
        self._loading.discard(position)
        self._failures += 1
        if self._failures >= min(MAX_FAILURES, len(self._order)):
            self._say("None of these pictures could be shown.")
            return
        if position == self._wanted:                    # skip what cannot be shown, in the direction of travel
            self._go((position + self._step) % len(self._order))

    def _say(self, text: str) -> None:
        self.message.setText(text)
        self.message.setGeometry(self.rect())
        self.message.show()
        self._paused = True
        self._sync_pause()

    def _display(self, position: int, image: QImage) -> None:
        self._wanted = None
        self._position = position
        self._previous = self._current
        self._current = image
        self._stop_fade()
        time = fluent.duration("slow") * FADE_FACTOR
        if time > 0 and self._previous is not None and self.isVisible():
            self._fade = 0.0
            animation = fluent.Animation(self)
            animation.setStartValue(0.0)
            animation.setEndValue(1.0)
            animation.setDuration(time)
            animation.setEasingCurve(fluent.easing("standard"))
            animation.valueChanged.connect(self._set_fade)
            animation.finished.connect(self._fade_done)
            self._animation = animation
            animation.start()
        else:
            self._fade = 1.0
            self._previous = None
        self.update()
        item = self.current_item()
        if item is not None:
            self.shown.emit(item.path)
        if not self._paused:
            self._timer.start(self.interval_ms())
        following = position + 1
        if following >= len(self._order):
            following = 0 if self.host.settings.slideshow_loop else -1
        if following >= 0 and following != position and following not in self._ready:
            self._load(following)

    def is_fading(self) -> bool:
        return self._animation is not None

    def _set_fade(self, value) -> None:
        self._fade = float(value)
        self.update()

    def _fade_done(self) -> None:
        self._animation = None
        self._previous = None
        self._fade = 1.0
        self.update()

    def _stop_fade(self) -> None:
        if self._animation is not None:
            self._animation.stop()
            self._animation = None
        self._fade = 1.0

    # --- controls ---
    def _show_controls(self) -> None:
        self.controls.show()
        self.controls.raise_()
        self.unsetCursor()
        self._controls_timer.start()

    def _hide_controls(self) -> None:
        if self._paused or self.controls.underMouse():
            return
        self.controls.hide()
        self.setCursor(Qt.CursorShape.BlankCursor)

    def mouseMoveEvent(self, event) -> None:
        self._show_controls()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        self._show_controls()
        super().mousePressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        size = self.controls.sizeHint()
        self.controls.setGeometry((self.width() - size.width()) // 2, self.height() - size.height() - 32,
                                  size.width(), size.height())
        self.message.setGeometry(self.rect())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    @staticmethod
    def _target(image: QImage, area: QSize) -> QRectF:
        scale = min(area.width() / image.width(), area.height() / image.height())
        width, height = image.width() * scale, image.height() * scale
        return QRectF((area.width() - width) / 2, (area.height() - height) / 2, width, height)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), fluent.qcolor("black"))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self._previous is not None and self._fade < 1.0:
            painter.setOpacity(1.0 - self._fade)
            painter.drawImage(self._target(self._previous, self.size()), self._previous)
        if self._current is not None:
            painter.setOpacity(self._fade if self._previous is not None else 1.0)
            painter.drawImage(self._target(self._current, self.size()), self._current)

    def shutdown(self) -> None:
        self._timer.stop()
        self._controls_timer.stop()
        self._stop_fade()
        self.worker.shutdown()
