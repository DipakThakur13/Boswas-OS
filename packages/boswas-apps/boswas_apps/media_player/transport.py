"""The now-playing bar at the bottom of the window, the now-playing page and the mini player."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QSizePolicy, QSlider, QStackedWidget, QVBoxLayout, QWidget

import boswas_fluent as fluent
from boswas_fluent import paint

from . import glyphs, model
from .controller import SEEK_STEP_MS, PlayerController
from .model import format_time
from .widgets import CoverView, PlayButton, SeekSlider, VideoSurface


class ElidedLabel(QWidget):
    """One line of plain text that is cut with an ellipsis instead of asking for more room."""

    def __init__(self, text: str = "", style: str = "body", color: str = "text", parent: QWidget | None = None, *,
                 centred: bool = False) -> None:
        super().__init__(parent)
        self._text, self._style, self._color, self._centred = text, style, color, centred
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.setAccessibleName(text)
            self.update()

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics() if self._style == "body" else None
        height = fluent.theme().type(self._style).line_height
        _ = metrics
        return QSize(40, height)

    def minimumSizeHint(self) -> QSize:
        return QSize(24, self.sizeHint().height())

    def paintEvent(self, _event) -> None:
        flags = (Qt.AlignmentFlag.AlignHCenter if self._centred else Qt.AlignmentFlag.AlignLeft)
        paint.elided(QPainter(self), QRectF(self.rect()), self._text, fluent.font(self._style), self._color,
                     flags | Qt.AlignmentFlag.AlignVCenter)


def volume_icon(volume: float, muted: bool) -> str:
    if muted or volume <= 0:
        return "audio-volume-muted-symbolic"
    if volume < 0.34:
        return "audio-volume-low-symbolic"
    if volume < 0.67:
        return "audio-volume-medium-symbolic"
    return "audio-volume-high-symbolic"


def rate_text(rate: float) -> str:
    return f"{rate:g}×"


class NowPlayingBar(QWidget):
    """Seek slider with times above; cover and title, the transport buttons, volume and view buttons below."""

    show_now_playing = Signal()
    fullscreen_requested = Signal()
    mini_requested = Signal()
    activity = Signal()                   # the pointer is over the bar (keeps it visible in full screen)

    HEIGHT = 108

    def __init__(self, controller: PlayerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.overlay = False
        self.setFixedHeight(self.HEIGHT)
        self.setMouseTracking(True)
        self.setAccessibleName("Now playing")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 6, 16, 10)
        outer.setSpacing(2)

        seek = QHBoxLayout()
        seek.setSpacing(12)
        self.position_label = fluent.Label("0:00", "caption", self, tone="secondary")
        self.duration_label = fluent.Label("0:00", "caption", self, tone="secondary")
        for label in (self.position_label, self.duration_label):
            label.setMinimumWidth(44)
        self.position_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.position_label.setAccessibleName("Position")
        self.duration_label.setAccessibleName("Length")
        self.seek = SeekSlider(self)
        seek.addWidget(self.position_label)
        seek.addWidget(self.seek, 1)
        seek.addWidget(self.duration_label)
        outer.addLayout(seek)

        row = QGridLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setHorizontalSpacing(8)
        row.setColumnStretch(0, 1)
        row.setColumnStretch(2, 1)

        left = QHBoxLayout()
        left.setSpacing(12)
        self.cover = CoverView(48, self, clickable=True)
        self.cover.setToolTip("Show what is playing")
        self.cover.setAccessibleName("Show what is playing")
        self.cover.clicked.connect(self.show_now_playing)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.title = ElidedLabel("", "body-strong", "text", self)
        self.subtitle = ElidedLabel("", "caption", "text-secondary", self)
        text.addStretch(1)
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        text.addStretch(1)
        left.addWidget(self.cover)
        left.addLayout(text, 1)
        self.stream_badge = fluent.Badge("Network stream", self, kind="info")
        self.stream_badge.hide()
        left.addWidget(self.stream_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(left, 0, 0)

        centre = QHBoxLayout()
        centre.setSpacing(4)
        self.shuffle = fluent.IconButton(glyphs.icon("shuffle"), "Shuffle (Ctrl+H)", self, size=36, checkable=True)
        self.previous = fluent.IconButton(glyphs.icon("previous"), "Previous (Ctrl+Left)", self, size=36)
        self.play = PlayButton(self)
        self.next = fluent.IconButton(glyphs.icon("next"), "Next (Ctrl+Right)", self, size=36)
        self.repeat = fluent.IconButton(glyphs.icon("repeat"), "Repeat (Ctrl+T)", self, size=36, checkable=True)
        for widget in (self.shuffle, self.previous, self.play, self.next, self.repeat):
            centre.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        centre.insertSpacing(2, 4)
        centre.insertSpacing(4, 4)
        row.addLayout(centre, 0, 1)

        right = QHBoxLayout()
        right.setSpacing(4)
        right.addStretch(1)
        self.rate = fluent.Button("1×", self, subtle=True)
        self.rate.setToolTip("Playback speed")
        self.rate.setAccessibleName("Playback speed")
        self.rate.setFixedWidth(56)
        self.mute = fluent.IconButton("audio-volume-high-symbolic", "Mute (M)", self, size=36)
        self.volume = QSlider(Qt.Orientation.Horizontal, self)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(104)
        self.volume.setAccessibleName("Volume")
        self.volume.setToolTip("Volume (Ctrl+Up, Ctrl+Down)")
        self.more = fluent.IconButton(fluent.glyph_icon("more", "text"), "Audio, subtitles and more", self, size=36,
                                      icon_size=14)
        self.mini = fluent.IconButton(glyphs.icon("mini-player"), "Mini player (Ctrl+M)", self, size=36)
        self.fullscreen = fluent.IconButton(glyphs.icon("fullscreen"), "Full screen (F11)", self, size=36)
        for widget in (self.rate, self.mute, self.volume, self.more, self.mini, self.fullscreen):
            right.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(right, 0, 2)
        outer.addLayout(row, 1)

        self.shuffle.clicked.connect(lambda _checked=False: controller.toggle_shuffle())
        self.repeat.clicked.connect(lambda _checked=False: controller.cycle_repeat())
        self.previous.clicked.connect(controller.previous)
        self.next.clicked.connect(controller.next)
        self.play.clicked.connect(controller.toggle)
        self.mute.clicked.connect(controller.toggle_mute)
        self.volume.valueChanged.connect(lambda value: controller.set_volume(value / 100))
        self.rate.clicked.connect(self.show_rate_menu)
        self.more.clicked.connect(self.show_more_menu)
        self.mini.clicked.connect(self.mini_requested)
        self.fullscreen.clicked.connect(self.fullscreen_requested)
        self.seek.seek_requested.connect(controller.seek)
        self.seek.preview.connect(lambda value: self.position_label.setText(format_time(value)))

        controller.state_changed.connect(self._state)
        controller.position_changed.connect(self._position)
        controller.duration_changed.connect(self._duration)
        controller.track_changed.connect(self._track)
        controller.track_updated.connect(self._track)
        controller.options_changed.connect(self.sync_options)
        controller.queue_changed.connect(self._sync_enabled)
        self._track(controller.current())
        self.sync_options()
        self._state(controller.state())

    # --- what the controller says ---
    def _state(self, state: str) -> None:
        self.play.set_playing(state == "playing")

    def _position(self, position: int) -> None:
        self.seek.set_position(position)
        if not self.seek.is_dragging():
            self.position_label.setText(format_time(position))

    def _duration(self, duration: int) -> None:
        self.seek.set_duration(duration if self.controller.current() is not None else 0)
        self.duration_label.setText(format_time(duration))

    def _track(self, track) -> None:
        if track is None:
            self.title.setText("Nothing is playing")
            self.subtitle.setText("")
            self.stream_badge.hide()
            self.seek.set_duration(0)
            self.position_label.setText("0:00")
            self.duration_label.setText("0:00")
        else:
            self.title.setText(track.display_title)
            self.subtitle.setText(track.subtitle if not track.is_remote else track.subtitle.split(" · ", 1)[-1])
            self.stream_badge.setVisible(track.is_remote)
            self.cover.set_glyph("stream" if track.is_remote else ("video" if track.video else "music"))
            self._duration(self.controller.duration())
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        controller = self.controller
        has_track = controller.current() is not None
        self.play.setEnabled(has_track or bool(controller.queue.items))
        self.previous.setEnabled(has_track)
        self.next.setEnabled(controller.queue.has_next())
        self.cover.setEnabled(has_track)

    def sync_options(self) -> None:
        controller, settings = self.controller, self.controller.settings
        self.shuffle.setChecked(controller.queue.shuffle)
        self.shuffle.setToolTip(f"Shuffle: {'on' if controller.queue.shuffle else 'off'} (Ctrl+H)")
        repeat = controller.queue.repeat
        self.repeat.setChecked(repeat != "off")
        self.repeat.setIcon(glyphs.icon("repeat-one" if repeat == "one" else "repeat"))
        text = {"off": "Repeat: off", "all": "Repeat: all", "one": "Repeat: one"}[repeat]
        self.repeat.setToolTip(f"{text} (Ctrl+T)")
        self.repeat.setAccessibleName(text)
        self.shuffle.setAccessibleName(self.shuffle.toolTip().split(" (")[0])
        blocked = self.volume.blockSignals(True)
        self.volume.setValue(round(settings.volume * 100))
        self.volume.blockSignals(blocked)
        self.mute.set_icon(volume_icon(settings.volume, settings.muted))
        self.mute.setToolTip("Unmute (M)" if settings.muted else "Mute (M)")
        self.mute.setAccessibleName("Unmute" if settings.muted else "Mute")
        self.rate.setText(rate_text(settings.rate))
        self._sync_enabled()

    def set_fullscreen(self, fullscreen: bool) -> None:
        self.fullscreen.set_icon(glyphs.icon("fullscreen-exit" if fullscreen else "fullscreen"))
        text = "Leave full screen (F11)" if fullscreen else "Full screen (F11)"
        self.fullscreen.setToolTip(text)
        self.fullscreen.setAccessibleName(text.split(" (")[0])
        self.overlay = fullscreen
        self.update()

    # --- menus ---
    def rate_menu(self) -> fluent.MenuFlyout:
        menu = fluent.MenuFlyout(self)
        current = self.controller.settings.rate
        for rate in model.RATES:
            label = f"{rate_text(rate)} (normal)" if rate == 1.0 else rate_text(rate)
            menu.add_item(label, lambda rate=rate: self.controller.set_rate(rate), checkable=True,
                          checked=abs(rate - current) < 1e-6)
        return menu

    def show_rate_menu(self) -> None:
        self.rate_menu().show_at(self.rate, "top")

    def more_menu(self) -> fluent.MenuFlyout:
        """Audio track, subtitles and video track (each only when the media offers a choice), and the speed."""
        controller, backend = self.controller, self.controller.backend
        menu = fluent.MenuFlyout(self)
        audio, video, subtitles = backend.audio_tracks(), backend.video_tracks(), backend.subtitle_tracks()
        if len(audio) > 1:
            self._track_menu(menu.add_submenu("Audio track"), "audio", audio, off=False)
        if subtitles:
            self._track_menu(menu.add_submenu("Subtitles"), "subtitle", subtitles, off=True)
        if len(video) > 1:
            self._track_menu(menu.add_submenu("Video track"), "video", video, off=False)
        speed = menu.add_submenu("Playback speed")
        for action in self.rate_menu().actions():
            action.setParent(speed)
            speed.addAction(action)
        menu.addSeparator()
        menu.add_item("Stop", controller.stop, icon=glyphs.icon("stop"), enabled=controller.current() is not None)
        return menu

    def _track_menu(self, menu: fluent.MenuFlyout, kind: str, labels: list[str], *, off: bool) -> None:
        backend = self.controller.backend
        active = backend.active_track(kind)
        if off:
            menu.add_item("Off", lambda: backend.set_active_track(kind, -1), checkable=True, checked=active < 0)
        for index, label in enumerate(labels):
            menu.add_item(label, lambda index=index: backend.set_active_track(kind, index), checkable=True,
                          checked=index == active)

    def show_more_menu(self) -> None:
        self.more_menu().show_at(self.more, "top")

    # --- looks ---
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = self.width()
        self.volume.setVisible(width >= 900)
        self.rate.setVisible(width >= 760)
        self.mini.setVisible(width >= 620)
        self.shuffle.setVisible(width >= 560)
        self.repeat.setVisible(width >= 560)
        self.stream_badge.setMaximumWidth(16777215 if width >= 1000 else 0)

    def mouseMoveEvent(self, event) -> None:
        self.activity.emit()
        super().mouseMoveEvent(event)

    def enterEvent(self, event) -> None:
        self.activity.emit()
        super().enterEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), fluent.qcolor("chrome" if self.overlay else "surface"))
        painter.fillRect(0, 0, self.width(), 1, fluent.qcolor("stroke-divider"))


class NowPlayingPage(QWidget):
    """What is playing, large: the video, or the cover with title, artist and album."""

    back_requested = Signal()

    def __init__(self, controller: PlayerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = QWidget(self)
        head = QHBoxLayout(self.header)
        head.setContentsMargins(16, 12, 16, 8)
        head.setSpacing(12)
        self.back = fluent.IconButton("go-previous-symbolic", "Back to the library (Alt+Left)", self.header, size=36)
        self.back.clicked.connect(self.back_requested)
        self.heading = ElidedLabel("Now playing", "subtitle", "text", self.header)
        head.addWidget(self.back)
        head.addWidget(self.heading, 1)
        layout.addWidget(self.header)
        self.stack = QStackedWidget(self)
        self.video = VideoSurface(self.stack)
        self.audio = QWidget(self.stack)
        audio = QVBoxLayout(self.audio)
        audio.setContentsMargins(32, 16, 32, 32)
        audio.setSpacing(6)
        self.cover = CoverView(280, self.audio)
        self.title = ElidedLabel("", "title", "text", self.audio, centred=True)
        self.artist = ElidedLabel("", "body-large", "text-secondary", self.audio, centred=True)
        self.album = ElidedLabel("", "body", "text-muted", self.audio, centred=True)
        audio.addStretch(1)
        audio.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignHCenter)
        audio.addSpacing(16)
        audio.addWidget(self.title)
        audio.addWidget(self.artist)
        audio.addWidget(self.album)
        audio.addStretch(1)
        self.stack.addWidget(self.audio)
        self.stack.addWidget(self.video)
        layout.addWidget(self.stack, 1)
        controller.track_changed.connect(self._track)
        controller.track_updated.connect(self._track)
        controller.video_changed.connect(self._video)
        controller.backend.frame_changed.connect(self.video.set_frame)
        controller.backend.subtitle_changed.connect(self.video.set_subtitle)
        self.video.clicked.connect(controller.toggle)
        self._track(controller.current())
        self._video(controller.has_video())

    def _track(self, track) -> None:
        if track is None:
            self.title.setText("Nothing is playing")
            self.artist.setText("")
            self.album.setText("")
            self.heading.setText("Now playing")
            return
        self.title.setText(track.display_title)
        self.artist.setText(track.subtitle)
        self.album.setText("" if track.is_remote else track.album)
        self.heading.setText(track.display_title)
        self.cover.set_glyph("stream" if track.is_remote else "music")

    def _video(self, has_video: bool) -> None:
        self.stack.setCurrentWidget(self.video if has_video else self.audio)
        if not has_video:
            self.video.set_frame(None)
            self.video.set_subtitle("")

    def set_fullscreen(self, fullscreen: bool) -> None:
        self.header.setVisible(not fullscreen)

    def paintEvent(self, _event) -> None:
        QPainter(self).fillRect(self.rect(), fluent.qcolor("background"))


class MiniPlayer(QWidget):
    """The small layout: picture, title and the three transport buttons."""

    leave_requested = Signal()
    SIZE = (360, 232)
    MINIMUM = (300, 200)

    def __init__(self, controller: PlayerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.stack = QStackedWidget(self)
        self.video = VideoSurface(self.stack)
        holder = QWidget(self.stack)
        centre = QVBoxLayout(holder)
        centre.setContentsMargins(12, 12, 12, 4)
        self.cover = CoverView(0, holder)
        self.cover.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.cover.setMinimumSize(64, 64)
        centre.addWidget(self.cover, 1)
        self.stack.addWidget(holder)
        self.stack.addWidget(self.video)
        layout.addWidget(self.stack, 1)
        strip = QWidget(self)
        row = QHBoxLayout(strip)
        row.setContentsMargins(12, 6, 8, 8)
        row.setSpacing(4)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.title = ElidedLabel("", "body-strong", "text", strip)
        self.subtitle = ElidedLabel("", "caption", "text-secondary", strip)
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        row.addLayout(text, 1)
        self.previous = fluent.IconButton(glyphs.icon("previous"), "Previous (Ctrl+Left)", strip, size=32)
        self.play = PlayButton(strip, size=36)
        self.next = fluent.IconButton(glyphs.icon("next"), "Next (Ctrl+Right)", strip, size=32)
        self.leave = fluent.IconButton(glyphs.icon("fullscreen"), "Leave the mini player (Ctrl+M)", strip, size=32)
        for widget in (self.previous, self.play, self.next, self.leave):
            row.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(strip)
        self.previous.clicked.connect(controller.previous)
        self.next.clicked.connect(controller.next)
        self.play.clicked.connect(controller.toggle)
        self.leave.clicked.connect(self.leave_requested)
        self.video.clicked.connect(controller.toggle)
        self.video.double_clicked.connect(self.leave_requested)
        controller.state_changed.connect(lambda state: self.play.set_playing(state == "playing"))
        controller.track_changed.connect(self._track)
        controller.track_updated.connect(self._track)
        controller.queue_changed.connect(self._sync_enabled)
        controller.video_changed.connect(self._video)
        controller.backend.frame_changed.connect(self.video.set_frame)
        controller.backend.subtitle_changed.connect(self.video.set_subtitle)
        self._track(controller.current())
        self._video(controller.has_video())
        self.play.set_playing(controller.state() == "playing")

    def _track(self, track) -> None:
        self.title.setText(track.display_title if track is not None else "Nothing is playing")
        self.subtitle.setText(track.subtitle if track is not None else "")
        if track is not None:
            self.cover.set_glyph("stream" if track.is_remote else "music")
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        controller = self.controller
        has_track = controller.current() is not None
        self.play.setEnabled(has_track or bool(controller.queue.items))
        self.previous.setEnabled(has_track)
        self.next.setEnabled(controller.queue.has_next())

    def _video(self, has_video: bool) -> None:
        self.stack.setCurrentIndex(1 if has_video else 0)
        if not has_video:
            self.video.set_frame(None)

    def paintEvent(self, _event) -> None:
        QPainter(self).fillRect(self.rect(), fluent.qcolor("surface"))


__all__ = ["ElidedLabel", "MiniPlayer", "NowPlayingBar", "NowPlayingPage", "SEEK_STEP_MS", "rate_text", "volume_icon"]
