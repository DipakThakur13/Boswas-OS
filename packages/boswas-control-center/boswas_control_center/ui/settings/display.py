"""Display settings: the monitors (arrangement, resolution, refresh rate, scale, orientation) and night light.

Both sections read the compositor's configuration through
boswas_control_center.compositorconfig and change it by writing the user's
file and asking the running compositor to read it again; the monitors
themselves come from the compositor's ``outputs`` command. Nothing here
touches a file or a socket on the GUI thread.

A change of resolution, refresh rate, scale or orientation, and turning a
monitor off, is applied and then asked about: "Keep these display
settings?". Without an answer within CONFIRM_SECONDS, or with Esc, the user
file is put back exactly as it was and the compositor reads it again.

ConfigSection, the base of every section that edits the compositor's
configuration, lives here; input.py and personalize.py build on it.
"""

from __future__ import annotations

import os

import boswas_fluent as fluent
from PySide6.QtCore import QFileSystemWatcher, QPointF, QRectF, QSize, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QSizePolicy, QSlider, QTimeEdit, QVBoxLayout, QWidget
from shiboken6 import isValid

from ... import compositorconfig as cc

CONFIRM_SECONDS = 15
SCALE_LABELS = {1.0: "100 %", 1.25: "125 %", 1.5: "150 %", 1.75: "175 %", 2.0: "200 %"}
# [outputs.NAME] rotation: the compositor turns "90", "180" and "270" into the output transforms of those names.
ORIENTATIONS = (("Landscape", "normal"), ("Portrait", "90"), ("Landscape (flipped)", "180"),
                ("Portrait (flipped)", "270"))
CONTROL_WIDTH = 220


def combo(items=(), width: int = CONTROL_WIDTH) -> QComboBox:
    """A combo box of (label, value) pairs; its ``activated`` signal tells a choice by the user."""
    box = QComboBox()
    box.setMinimumWidth(width)
    box.setMaxVisibleItems(12)
    for label, value in items:
        box.addItem(label, value)
    return box


def select(box: QComboBox, value: object, label: str | None = None) -> None:
    """Show ``value`` in the combo box; a value it does not offer is added (with ``label``) so that it can be shown."""
    index = next((i for i in range(box.count()) if box.itemData(i) == value), -1)
    if index < 0:
        box.addItem(label if label is not None else str(value), value)
        index = box.count() - 1
    if box.currentIndex() != index:
        box.setCurrentIndex(index)


class ConfigSection(QWidget):
    """A settings section that shows and changes the compositor's configuration.

    A subclass builds its cards in ``build()`` (add them with ``add``),
    shows a loaded state in ``show_state(state)`` and calls
    ``change({(table, key): value})`` when the user changes something.
    ``load()`` runs on a worker thread and returns the state (by default a
    compositorconfig.Snapshot). A change that fails is told in the
    section's InfoBar and the controls go back to what is in use.

    ``config`` is a compositorconfig.Settings (tests pass their own).
    """

    page = ""
    title = ""
    order = 0
    keywords: tuple[str, ...] = ()
    reports_files = False                      # tell what is wrong with the settings files (one section per page)

    def __init__(self, parent: QWidget | None = None, *, config: cc.Settings | None = None) -> None:
        super().__init__(parent)
        self.config = config if config is not None else cc.default()
        self.state = None                      # what load() returned last
        self.active = False
        self.loading = False                   # show_state() is setting the controls: not a change by the user
        self.busy = False                      # a change is on its way to the files
        self._queued: dict = {}
        self._delayed: dict = {}
        self._generation = 0
        self.rows = QVBoxLayout(self)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(4)
        self.notice = fluent.InfoBar("info", parent=self)
        self.notice.hide()
        self.rows.addWidget(self.notice)
        self._watcher: QFileSystemWatcher | None = None
        self._reload = QTimer(self)
        self._reload.setSingleShot(True)
        self._reload.setInterval(200)
        self._reload.timeout.connect(self.refresh)
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(350)
        self._delay.timeout.connect(self._send_delayed)
        self.build()

    # --- for subclasses ---
    def build(self) -> None:
        """Create the cards (once, no I/O)."""

    def load(self) -> object:
        """Read the state to show. Runs on a worker thread."""
        return self.config.snapshot()

    def show_state(self, state: object) -> None:
        """Set the controls from ``state`` (what load() returned)."""

    def add(self, widget: QWidget) -> QWidget:
        self.rows.addWidget(widget)
        return widget

    def card(self, title: str, description: str = "", icon: str | None = None,
             control: QWidget | None = None) -> fluent.SettingsCard:
        return self.add(fluent.SettingsCard(title, description, icon, control, self))

    def toggle(self, title: str, description: str, icon: str | None, table: str, key: str, *,
               inverted: bool = False, into: fluent.SettingsExpander | None = None) -> fluent.ToggleSwitch:
        """A switch row bound to a boolean key (``inverted``: on means false)."""
        switch = fluent.ToggleSwitch()
        if into is not None:
            into.add_row(title, description, switch, icon)
        else:
            self.card(title, description, icon, switch)
        switch.clicked.connect(lambda on: self.change({(table, key): bool(on) != inverted}))
        return switch

    # --- the section contract ---
    def activate(self) -> None:
        self.active = True
        self._watch()
        self.refresh()

    def deactivate(self) -> None:
        self.active = False
        self._generation += 1                  # results that are still on their way are not shown
        self._reload.stop()
        if self._delay.isActive():
            self._delay.stop()
            self._send_delayed()               # a slider that was just moved still counts
        if self._watcher is not None:
            self._watcher.deleteLater()
            self._watcher = None

    # --- loading ---
    def _watch(self) -> None:
        """Follow changes made elsewhere (a preset, another window, an editor) to the user's two files."""
        directory = self.config.directory()
        if not self.active or directory is None:
            return
        if self._watcher is None:
            self._watcher = QFileSystemWatcher(self)
            self._watcher.directoryChanged.connect(self._files_changed)
            self._watcher.fileChanged.connect(self._files_changed)
        # The directory tells of files that appear or are replaced, a file of a change in place. Paths that
        # do not exist yet cannot be watched; this is called again after every change.
        watched = set(self._watcher.directories()) | set(self._watcher.files())
        wanted = [directory, *(self.config.user_path(name) for name in (cc.USER_NAME, cc.APPEARANCE_NAME))]
        missing = [path for path in wanted if path not in watched and os.path.exists(path)]
        if missing:
            self._watcher.addPaths(missing)

    def _files_changed(self, _path: str = "") -> None:
        if self.active:
            self._reload.start()

    def refresh(self) -> None:
        self._generation += 1
        generation = self._generation

        def done(state: object) -> None:
            if isValid(self) and generation == self._generation and self.active:
                self._loaded(state)

        def failed(exc: Exception) -> None:
            if isValid(self) and generation == self._generation:
                self.notice.show_message("error", f"The settings could not be read ({exc}).")
        fluent.run_in_thread(self.load, on_done=done, on_error=failed)

    def _loaded(self, state: object) -> None:
        self._watch()
        if self.busy or self._queued or self._delayed:
            return                             # a change is on its way; its own refresh follows
        self.state = state
        self.loading = True
        try:
            self.show_state(state)
        finally:
            self.loading = False
        snapshot = state if isinstance(state, cc.Snapshot) else state[0] if isinstance(state, tuple) else None
        if self.reports_files and isinstance(snapshot, cc.Snapshot):
            problems = snapshot.problems()
            if problems and self.notice.isHidden():
                self.notice.show_message("warning", " ".join(problems))

    # --- changing ---
    def change(self, changes: dict) -> None:
        """Save ``changes`` and have the desktop take them; the calls of one section are kept in order."""
        if self.loading:
            return
        self._queued.update(changes)
        self._pump()

    def change_later(self, changes: dict) -> None:
        """The same, once the user has stopped moving the control for a moment (sliders, time fields)."""
        if self.loading:
            return
        self._delayed.update(changes)
        self._delay.start()

    def _send_delayed(self) -> None:
        delayed, self._delayed = self._delayed, {}
        if delayed:
            self._queued.update(delayed)
            self._pump()

    def perform(self, changes: dict) -> object:
        """What is done with one batch of changes, on a worker thread; returns a compositorconfig.Applied."""
        return self.config.apply(changes)

    def _pump(self) -> None:
        if self.busy or not self._queued:
            return
        changes, self._queued = self._queued, {}
        self.busy = True

        def done(result: object) -> None:
            if isValid(self):
                self._applied(result)

        def failed(exc: Exception) -> None:
            if isValid(self):
                self._failed(exc)
        fluent.run_in_thread(lambda: self.perform(changes), on_done=done, on_error=failed)

    def tell(self, applied: cc.Applied) -> None:
        """Show what a change had to say, or clear the bar when it simply worked."""
        if not applied.message:
            self.notice.hide()
        elif applied.saved.notice or applied.warnings:
            self.notice.show_message("warning", applied.message)
        else:
            self.notice.show_message("info", applied.message)

    def _applied(self, result: object) -> None:
        self.busy = False
        self.applied(result)
        self._watch()
        if self._queued:
            self._pump()
        elif self.active:
            self.refresh()

    def applied(self, result: object) -> None:
        self.tell(result)

    def _failed(self, exc: Exception) -> None:
        self.busy = False
        self._queued.clear()
        text = str(exc) if isinstance(exc, cc.ConfigError) else f"The setting could not be changed ({exc})."
        self.notice.show_message("error", text)
        if self.active:
            self.refresh()                     # the controls go back to what is in use


# --- the monitors -----------------------------------------------------------------------------------------------

class MonitorCanvas(QWidget):
    """The monitors as rectangles in their real proportions and places; drag one to rearrange.

    Keyboard: the arrow keys select the next or previous monitor, Ctrl with
    an arrow key puts the selected one on that side of the others.
    """

    selected = Signal(str)
    arranged = Signal(dict)                    # {name: (x, y)} after a monitor was moved

    PADDING = 18
    MAGNET = 48                                # logical pixels within which edges line up

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rects: dict[str, cc.Rect] = {}
        self.numbers: dict[str, int] = {}
        self.current = ""
        self._drag: tuple[str, QPointF, dict] | None = None      # name, grab offset in logical pixels, start rects
        self._view: tuple[float, float, float] | None = None     # scale, x offset, y offset while dragging
        self._preview: dict[str, cc.Rect] | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Monitor arrangement")
        self.setMouseTracking(False)

    def sizeHint(self) -> QSize:
        return QSize(520, 220)

    def set_monitors(self, rects: dict[str, cc.Rect], numbers: dict[str, int], current: str) -> None:
        if self._drag is not None:
            return
        self.rects, self.numbers, self.current = dict(rects), dict(numbers), current
        self._describe()
        self.update()

    def dragging(self) -> bool:
        return self._drag is not None

    def _describe(self) -> None:
        count = len(self.rects)
        text = f"{count} monitor{'s' if count != 1 else ''}."
        if self.current in self.numbers:
            text += f" Monitor {self.numbers[self.current]} is selected."
        if count > 1:
            text += " Drag a monitor to rearrange, or press Ctrl with an arrow key."
        self.setAccessibleDescription(text)

    # --- geometry ---
    def _shown(self) -> dict[str, cc.Rect]:
        return self._preview if self._preview is not None else self.rects

    def _transform(self) -> tuple[float, float, float]:
        if self._view is not None:
            return self._view
        rects = list(self.rects.values())
        if not rects:
            return 1.0, 0.0, 0.0
        left, top = min(r.x for r in rects), min(r.y for r in rects)
        width = max(r.right for r in rects) - left
        height = max(r.bottom for r in rects) - top
        room = 0.82 if len(rects) > 1 else 1.0   # space to drag a monitor around the others
        scale = min((self.width() - 2 * self.PADDING) * room / max(width, 1),
                    (self.height() - 2 * self.PADDING) * room / max(height, 1), 0.12)
        return (scale, (self.width() - width * scale) / 2 - left * scale,
                (self.height() - height * scale) / 2 - top * scale)

    def view_rect(self, name: str) -> QRectF:
        """Where a monitor is drawn, in widget coordinates."""
        scale, dx, dy = self._transform()
        rect = self._shown()[name]
        return QRectF(rect.x * scale + dx, rect.y * scale + dy, rect.w * scale, rect.h * scale)

    def _at(self, position: QPointF) -> str:
        for name in sorted(self._shown(), key=lambda n: n != self.current):
            if self.view_rect(name).contains(position):
                return name
        return ""

    # --- painting ---
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = fluent.font("subtitle")
        small = fluent.font("caption")
        for name in sorted(self._shown(), key=lambda n: n == self.current):
            box = self.view_rect(name).adjusted(0.75, 0.75, -0.75, -0.75)
            chosen = name == self.current
            fluent.paint.rounded_rect(painter, box, 6, "accent" if chosen else "fill-control",
                                      "accent" if chosen else "stroke-strong")
            painter.setPen(fluent.qcolor("on-accent" if chosen else "text"))
            painter.setFont(font)
            painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), str(self.numbers.get(name, "")))
            if box.height() > 70 and box.width() > 80:
                painter.setFont(small)
                painter.setPen(fluent.qcolor("on-accent" if chosen else "text-secondary"))
                painter.drawText(box.adjusted(6, 0, -6, -6),
                                 int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom), name)

    # --- pointer ---
    def mousePressEvent(self, event) -> None:
        name = self._at(event.position()) if event.button() == Qt.MouseButton.LeftButton else ""
        if not name:
            return super().mousePressEvent(event)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._choose(name)
        if len(self.rects) > 1:
            scale, dx, dy = self._view = self._transform()
            rect = self.rects[name]
            grab = QPointF((event.position().x() - dx) / scale - rect.x, (event.position().y() - dy) / scale - rect.y)
            self._drag = (name, grab, dict(self.rects))

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None or self._view is None:
            return super().mouseMoveEvent(event)
        name, grab, start = self._drag
        scale, dx, dy = self._view
        x = round((event.position().x() - dx) / scale - grab.x())
        y = round((event.position().y() - dy) / scale - grab.y())
        self._preview = cc.drop(start, name, x, y, self.MAGNET)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is None:
            return super().mouseReleaseEvent(event)
        _, _, start = self._drag
        result, self._drag, self._view, self._preview = self._preview, None, None, None
        if result is not None and result != start:
            self._arrange(result)
        self.update()

    def _arrange(self, rects: dict[str, cc.Rect]) -> None:
        self.rects = dict(rects)
        self.update()
        self.arranged.emit({name: (rect.x, rect.y) for name, rect in rects.items()})

    def _choose(self, name: str) -> None:
        if name != self.current:
            self.current = name
            self._describe()
            self.update()
            self.selected.emit(name)

    # --- keyboard ---
    def keyPressEvent(self, event) -> None:
        steps = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        step = steps.get(event.key())
        if step is None or not self.rects or self.current not in self.rects:
            return super().keyPressEvent(event)
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.move_selected(*step)
            return
        order = sorted(self.rects, key=lambda name: self.numbers.get(name, 0))
        index = order.index(self.current) + (1 if step[0] + step[1] > 0 else -1)
        self._choose(order[index % len(order)])

    def move_selected(self, dx: int, dy: int) -> None:
        """Put the selected monitor on that side of the others (it keeps its place along the edge)."""
        if len(self.rects) < 2 or self.current not in self.rects:
            return
        rect = self.rects[self.current]
        others = [other for name, other in self.rects.items() if name != self.current]
        left, right = min(o.x for o in others), max(o.right for o in others)
        top, bottom = min(o.y for o in others), max(o.bottom for o in others)
        # Beyond the others on the wanted side, and within their span along it.
        x = left - rect.w if dx < 0 else right if dx > 0 else max(left, min(rect.x, right - rect.w))
        y = top - rect.h if dy < 0 else bottom if dy > 0 else max(top, min(rect.y, bottom - rect.h))
        result = cc.drop(self.rects, self.current, x, y, self.MAGNET)
        if result != self.rects:
            self._arrange(result)


class MonitorsSection(ConfigSection):
    page = "display"
    title = "Monitors"
    order = 10
    reports_files = True
    keywords = ("monitor", "screen", "resolution", "refresh rate", "scale", "zoom", "orientation", "rotate",
                "arrange", "second screen", "external display", "hdmi")

    def __init__(self, parent: QWidget | None = None, *, config: cc.Settings | None = None) -> None:
        self.outputs: list[cc.Output] = []
        self.current = ""                      # the selected monitor (connector name)
        self.confirm_seconds = CONFIRM_SECONDS
        self.tick_ms = 1000
        self.keep_dialog: fluent.ContentDialog | None = None
        self._previous: bytes | None = None
        self._risky = False
        super().__init__(parent, config=config)

    def build(self) -> None:
        self.unavailable = self.add(fluent.InfoBar("info", parent=self, closable=False))
        self.unavailable.hide()
        self.canvas_card = self.add(fluent.Card(self, padding=8, spacing=4))
        self.canvas = MonitorCanvas(self.canvas_card)
        self.canvas_card.add(self.canvas)
        self.hint = fluent.Label("Select a monitor to change its settings. Drag monitors to match how they stand "
                                 "on your desk.", "caption", self.canvas_card, tone="secondary", wrap=True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.canvas_card.add(self.hint)
        self.canvas.selected.connect(self.select_monitor)
        self.canvas.arranged.connect(self._arranged)

        self.monitor_box = combo()
        self.monitor_card = self.card("Monitor", "The monitor the settings below belong to",
                                      "video-display-symbolic", self.monitor_box)
        self.monitor_box.activated.connect(lambda _i: self.select_monitor(self.monitor_box.currentData()))
        self.use_switch = fluent.ToggleSwitch()
        self.use_card = self.card("Use this monitor", "", "eye-open-symbolic", self.use_switch)
        self.use_switch.clicked.connect(self._use_changed)
        self.resolution_box = combo()
        self.resolution_card = self.card("Resolution", "How many pixels the monitor shows",
                                         "view-fullscreen-symbolic", self.resolution_box)
        self.resolution_box.activated.connect(self._resolution_changed)
        self.rate_box = combo()
        self.rate_card = self.card("Refresh rate", "A higher rate makes motion smoother and uses more power",
                                   "view-refresh-symbolic", self.rate_box)
        self.rate_box.activated.connect(self._rate_changed)
        self.scale_box = combo((label, value) for value, label in SCALE_LABELS.items())
        self.scale_card = self.card("Scale", "The size of text, applications and other items",
                                    "zoom-in-symbolic", self.scale_box)
        self.scale_box.activated.connect(self._scale_changed)
        self.orientation_box = combo(ORIENTATIONS)
        self.orientation_card = self.card("Orientation", "Turn the picture for a monitor that stands upright",
                                          "object-rotate-right-symbolic", self.orientation_box)
        self.orientation_box.activated.connect(self._orientation_changed)
        self.detail_cards = (self.resolution_card, self.rate_card, self.scale_card, self.orientation_card)
        for widget in (self.canvas_card, self.monitor_card, self.use_card, *self.detail_cards):
            widget.hide()

    # --- state ---
    def load(self) -> object:
        snapshot = self.config.snapshot()
        try:
            return snapshot, self.config.outputs(), ""
        except cc.ControlError as exc:
            return snapshot, [], str(exc)

    def output(self, name: str | None = None) -> cc.Output | None:
        name = self.current if name is None else name
        return next((output for output in self.outputs if output.name == name), None)

    def enabled_outputs(self) -> list[cc.Output]:
        return [output for output in self.outputs if output.enabled]

    def number(self, name: str) -> int:
        return [output.name for output in self.outputs].index(name) + 1

    def rects(self) -> dict[str, cc.Rect]:
        return {o.name: cc.Rect(o.x, o.y, o.width, o.height) for o in self.enabled_outputs()}

    def show_state(self, state: object) -> None:
        if self.canvas.dragging():
            return
        _snapshot, self.outputs, problem = state
        shown = bool(self.outputs)
        self.unavailable.setVisible(not shown)
        if not shown:
            self.unavailable.show_message("info", "Monitors can be set up here while the Boswas desktop is running. "
                                          + (problem or "The desktop reported no monitor."))
        if self.output() is None:
            first = next(iter(self.enabled_outputs()), None) or next(iter(self.outputs), None)
            self.current = first.name if first is not None else ""
        self.canvas_card.setVisible(shown)
        self.hint.setVisible(len(self.enabled_outputs()) > 1)
        self.monitor_card.setVisible(len(self.outputs) > 1)
        self.use_card.setVisible(len(self.outputs) > 1)
        self.monitor_box.clear()
        for output in self.outputs:
            off = "" if output.enabled else ", off"
            self.monitor_box.addItem(f"{self.number(output.name)}: {output.label} ({output.name}{off})", output.name)
        self._show_monitor()

    def _show_monitor(self) -> None:
        output = self.output()
        self.canvas.set_monitors(self.rects(), {o.name: self.number(o.name) for o in self.outputs}, self.current)
        for card in self.detail_cards:
            card.setVisible(output is not None)
        if output is None:
            return
        select(self.monitor_box, output.name)
        only = output.enabled and len(self.enabled_outputs()) == 1
        self.use_switch.setChecked(output.enabled)
        self.use_switch.setEnabled(not only)
        self.use_card.set_description("The only monitor in use cannot be turned off" if only else
                                      "Turned off, the monitor stays dark and windows move to the others")
        usable = output.enabled and output.mode is not None
        for card in self.detail_cards:
            card.setEnabled(usable)
        self.resolution_card.set_description("How many pixels the monitor shows" if output.enabled else
                                             "Turn the monitor on to change its settings")
        self.resolution_box.clear()
        self.rate_box.clear()
        if not usable:
            return
        best = cc.preferred_size(output.modes)
        for width, height in cc.sizes(output.modes):
            self.resolution_box.addItem(f"{width} × {height}" + (" (recommended)" if (width, height) == best
                                                                         else ""), (width, height))
        select(self.resolution_box, output.mode.size, f"{output.mode.width} × {output.mode.height}")
        for rate in cc.rates(output.modes, output.mode.size):
            self.rate_box.addItem(cc.rate_label(rate), rate)
        select(self.rate_box, output.mode.refresh, cc.rate_label(output.mode.refresh))
        self.rate_box.setEnabled(self.rate_box.count() > 1)
        for index in reversed(range(self.scale_box.count())):
            if self.scale_box.itemData(index) not in SCALE_LABELS:
                self.scale_box.removeItem(index)
        select(self.scale_box, output.scale, f"{round(output.scale * 100)} %")
        select(self.orientation_box, output.rotation)

    def select_monitor(self, name: str) -> None:
        if name and name != self.current and self.output(name) is not None:
            self.current = name
            self.loading = True
            try:
                self._show_monitor()
            finally:
                self.loading = False

    # --- changes ---
    def _placement(self, anchor: str, rects: dict[str, cc.Rect]) -> dict:
        """The positions to write so that ``rects`` have neither gaps nor overlaps; ``anchor`` stays where it is."""
        if len(rects) < 2:
            return {(f"outputs.{name}", "position"): None for name in rects}
        arranged = cc.arrange(rects, anchor)
        return {(f"outputs.{name}", "position"): [rect.x, rect.y] for name, rect in arranged.items()}

    def _resized(self, output: cc.Output, *, size: tuple[int, int] | None = None, scale: float | None = None,
                 rotation: str | None = None) -> dict:
        """The positions after ``output`` takes another size on the desktop."""
        width, height = size or (output.mode.size if output.mode else (output.width, output.height))
        logical = cc.logical_size(width, height, scale or output.scale, rotation or output.rotation)
        rects = self.rects()
        rects[output.name] = cc.Rect(output.x, output.y, *logical)
        return self._placement(output.name, rects)

    def _arranged(self, positions: dict) -> None:
        self.change({(f"outputs.{name}", "position"): [x, y] for name, (x, y) in positions.items()})

    def _resolution_changed(self, _index: int) -> None:
        output, size = self.output(), self.resolution_box.currentData()
        if output is None or size is None or self.loading:
            return
        offered = cc.rates(output.modes, size)
        rate = output.mode.refresh if output.mode and output.mode.refresh in offered else (offered or [None])[0]
        self.change_risky({(f"outputs.{output.name}", "mode"): cc.mode_text(*size, rate),
                           **self._resized(output, size=size)})

    def _rate_changed(self, _index: int) -> None:
        output, rate = self.output(), self.rate_box.currentData()
        if output is None or output.mode is None or rate is None or self.loading:
            return
        self.change_risky({(f"outputs.{output.name}", "mode"): cc.mode_text(*output.mode.size, rate)})

    def _scale_changed(self, _index: int) -> None:
        output, scale = self.output(), self.scale_box.currentData()
        if output is None or scale not in cc.SCALES or self.loading:
            return
        self.change_risky({(f"outputs.{output.name}", "scale"): scale, **self._resized(output, scale=scale)})

    def _orientation_changed(self, _index: int) -> None:
        output, rotation = self.output(), self.orientation_box.currentData()
        if output is None or rotation not in cc.ROTATIONS or self.loading:
            return
        self.change_risky({(f"outputs.{output.name}", "rotation"): rotation,
                           **self._resized(output, rotation=rotation)})

    def _use_changed(self, on: bool) -> None:
        output = self.output()
        if output is None or self.loading:
            return
        table = f"outputs.{output.name}"
        if on:
            # Without a position the compositor puts it to the right of the others.
            self.change_risky({(table, "enabled"): None, (table, "position"): None})
            return
        remaining = {name: rect for name, rect in self.rects().items() if name != output.name}
        if not remaining:
            self.refresh()
            return
        self.change_risky({(table, "enabled"): False, (table, "position"): None,
                           **self._placement(sorted(remaining)[0], remaining)})

    def change_risky(self, changes: dict) -> None:
        """A change that can leave the user without a picture: applied, then confirmed or taken back."""
        if self.loading or self.busy or self.keep_dialog is not None:
            self.refresh()
            return
        self._risky = True
        self.change(changes)

    def perform(self, changes: dict) -> object:
        if not self._risky:
            return self.config.apply(changes)
        previous = self.config.user_file()
        return previous, self.config.apply(changes)

    def applied(self, result: object) -> None:
        if not self._risky:
            self.tell(result)
            return
        self._risky = False
        self._previous, applied = result
        self.tell(applied)
        if applied.live:
            self._ask_to_keep()

    def _failed(self, exc: Exception) -> None:
        self._risky = False
        super()._failed(exc)

    # --- keep or revert ---
    def _ask_to_keep(self) -> None:
        self._remaining = self.confirm_seconds
        dialog = fluent.ContentDialog(self, "Keep these display settings?", self._countdown_text(),
                                      primary="Keep changes", close="Revert", default="primary")
        self.keep_dialog = dialog
        self._countdown = QTimer(dialog)
        self._countdown.setInterval(self.tick_ms)
        self._countdown.timeout.connect(self._tick)
        dialog.finished.connect(self._keep_answered)
        dialog.open()
        self._countdown.start()

    def _countdown_text(self) -> str:
        seconds = self._remaining
        return f"The previous settings come back in {seconds} second{'s' if seconds != 1 else ''}."

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self.keep_dialog.done(fluent.ContentDialog.Result.NONE)
        else:
            self.keep_dialog.set_text(self._countdown_text())

    def _keep_answered(self, result: object) -> None:
        dialog, self.keep_dialog = self.keep_dialog, None
        self._countdown.stop()
        dialog.deleteLater()
        if result == fluent.ContentDialog.Result.PRIMARY:
            self.refresh()
            return
        previous = self._previous
        self.busy = True

        def revert() -> object:
            self.config.restore(previous)
            return self.config.reloaded()

        def done(_applied: object) -> None:
            if isValid(self):
                self.busy = False
                self.notice.show_message("info", "The previous display settings are back.")
                self.refresh()

        def failed(exc: Exception) -> None:
            if isValid(self):
                self._failed(exc)
        fluent.run_in_thread(revert, on_done=done, on_error=failed)

    def deactivate(self) -> None:
        if self.keep_dialog is not None:       # leaving the page without an answer: not kept
            self.keep_dialog.done(fluent.ContentDialog.Result.NONE)
        super().deactivate()


# --- night light ------------------------------------------------------------------------------------------------

class NightLightSection(ConfigSection):
    page = "display"
    title = "Night light"
    order = 20
    keywords = ("night light", "blue light", "warm", "colour temperature", "color temperature", "evening", "schedule")

    def build(self) -> None:
        self._was_scheduled = False
        self.enabled = self.toggle("Night light", "Warmer colours in the evening are easier on the eyes",
                                   "night-light-symbolic", "night_light", "enabled")
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.strength = QSlider(Qt.Orientation.Horizontal)
        self.strength.setRange(cc.TEMPERATURE[0] // 100, cc.TEMPERATURE[1] // 100)     # hundreds of kelvin
        self.strength.setPageStep(5)
        self.strength.setFixedWidth(200)
        self.strength.setAccessibleName("Colour temperature")
        self.kelvin = fluent.Label("", "body", tone="secondary")
        self.kelvin.setMinimumWidth(58)
        self.kelvin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(fluent.Label("Warmer", "caption", tone="secondary"))
        row.addWidget(self.strength)
        row.addWidget(fluent.Label("Cooler", "caption", tone="secondary"))
        row.addWidget(self.kelvin)
        self.strength_card = self.card("Strength", "The colour temperature while night light is on",
                                       "display-brightness-symbolic", holder)
        self.strength.valueChanged.connect(self._strength_changed)
        self.strength.sliderReleased.connect(self._send_delayed)

        self.scheduled = fluent.ToggleSwitch()
        self.schedule = self.add(fluent.SettingsExpander(
            "Schedule night light", "Turn it on and off by the clock instead of leaving it on",
            "preferences-system-time-symbolic", self.scheduled, self))
        self.scheduled.clicked.connect(self._scheduled_changed)
        self.start, self.end = QTimeEdit(), QTimeEdit()
        for edit, title, key in ((self.start, "Turn on at", "from"), (self.end, "Turn off at", "to")):
            edit.setDisplayFormat("HH:mm")
            edit.setMinimumWidth(110)
            self.schedule.add_row(title, "", edit)
            edit.timeChanged.connect(lambda time, key=key: self.change_later(
                {("night_light", key): cc.clock_text(time.hour() * 60 + time.minute())}))
        self.note = self.add(fluent.Label("Night light needs a graphics driver with gamma tables.", "caption", self,
                                          tone="secondary", wrap=True))
        self.note.setContentsMargins(4, 2, 0, 0)

    def _strength_changed(self, value: int) -> None:
        self.kelvin.setText(f"{value * 100} K")
        self.strength.setAccessibleDescription(f"{value * 100} kelvin. Lower is warmer.")
        self.change_later({("night_light", "temperature"): value * 100})

    def _scheduled_changed(self, on: bool) -> None:
        self.change({("night_light", "scheduled"): bool(on)})
        self.schedule.set_expanded(bool(on))

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.enabled.setChecked(snapshot.get("night_light", "enabled"))
        if not self.strength.isSliderDown():
            self.strength.setValue(round(snapshot.get("night_light", "temperature") / 100))
        self.kelvin.setText(f"{self.strength.value() * 100} K")
        scheduled = snapshot.get("night_light", "scheduled")
        self.scheduled.setChecked(scheduled)
        if scheduled != self._was_scheduled:   # open with the schedule; afterwards the user decides
            self._was_scheduled = scheduled
            self.schedule.set_expanded(scheduled, animate=False)
        for edit, key in ((self.start, "from"), (self.end, "to")):
            minutes = cc.parse_clock(snapshot.get("night_light", key)) or 0
            if not edit.hasFocus():
                edit.setTime(QTime(minutes // 60, minutes % 60))


SECTIONS = (MonitorsSection, NightLightSection)
