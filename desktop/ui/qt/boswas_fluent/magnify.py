"""Icons that grow under the pointer, as in a dock: ``IconMagnifier`` for rows of widgets, ``ViewMagnifier``
for the rows of an item view.

The row under the pointer shows its icon at ``GROW`` times its size, its
two neighbours at ``NEAR``, every other row at its own size. When the
pointer is not over a row, the row that has the keyboard focus grows the
same way, so the effect also follows Tab and the arrow keys. Each icon
moves to its new size with ``fluent.Animation`` (``duration("fast")``, the
decelerate curve), stepped at the refresh rate of the screen; only the rows
whose size changes animate, and only their icons are painted again. With
reduced motion an icon takes its new size at once, without animation (the
enlargement stays, as the cue of where the pointer or the focus is).

No layout moves: a row reserves the room its icon grows into
(``IconWidget.set_magnification``; ``SettingsCard`` and ``ListItem`` keep
their text where it was), and a delegate of an item view reserves it in its
own drawing. Icons are drawn from pixmaps kept per size
(``icons.cached_pixmap``).

    magnifier = fluent.IconMagnifier(list_widget)
    magnifier.set_rows([row1, row2, row3])        # in their visual order; again when the rows change

    magnifier = fluent.ViewMagnifier(view, accept=lambda index: index.data(KIND) == APP)
    ...  # in the delegate's paint: scale = magnifier.scale(index.row())
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QModelIndex, QObject, QTimer, Signal
from PySide6.QtWidgets import QAbstractItemView, QApplication, QWidget

from .animation import Animation
from .theming import duration, easing

GROW = 1.4                 # the icon of the row under the pointer, or with the focus
NEAR = 1.15                # the icons of its neighbours
LEAVE_DELAY_MS = 90        # the pointer crossing the gap between two rows does not shrink everything


class Magnifier(QObject):
    """The sizes of the icons of a list of rows, each identified by a key (a widget, a row number).

    ``scale_changed(key, scale, settled)`` is emitted for every step; ``settled`` is True when the icon
    arrived at its size (draw it crisp at a whole pixel size then).
    """

    scale_changed = Signal(object, float, bool)

    def __init__(self, parent: QObject, *, grow: float = GROW, near: float = NEAR,
                 pace: QWidget | None = None) -> None:
        super().__init__(parent)
        self.grow, self.near = grow, near
        self._pace = pace
        self._keys: list = []
        self._scales: dict = {}
        self._targets: dict = {}
        self._animations: dict = {}
        self._hovered = None
        self._focused = None

    # --- state ---
    def keys(self) -> list:
        return list(self._keys)

    def hovered(self):
        return self._hovered

    def focused(self):
        return self._focused

    def active(self):
        """The row that grows most: the one under the pointer, else the one with the focus (None: none)."""
        return self._hovered if self._hovered is not None else self._focused

    def scale(self, key) -> float:
        """The size of the icon of ``key`` now, as a multiple of its own size."""
        return self._scales.get(key, 1.0)

    def target(self, key) -> float:
        """The size the icon of ``key`` is going to."""
        return self._targets.get(key, 1.0)

    def animating(self, key=None) -> bool:
        return bool(self._animations) if key is None else key in self._animations

    # --- changes ---
    def set_keys(self, keys: list) -> None:
        keys = list(dict.fromkeys(keys))
        for key in [k for k in self._keys if k not in keys]:
            animation = self._animations.pop(key, None)
            if animation is not None:
                animation.stop()
            self._scales.pop(key, None)
            self._targets.pop(key, None)
        self._keys = keys
        if self._hovered not in keys:
            self._hovered = None
        if self._focused not in keys:
            self._focused = None
        self._retarget()

    def reset(self) -> None:
        """Every icon at its own size at once (the rows were replaced)."""
        for animation in self._animations.values():
            animation.stop()
        self._animations.clear()
        self._scales.clear()
        self._targets.clear()
        self._hovered = self._focused = None
        self._retarget()

    def set_hovered(self, key) -> None:
        key = key if key in self._keys else None
        if key != self._hovered:
            self._hovered = key
            self._retarget()

    def set_focused(self, key) -> None:
        key = key if key in self._keys else None
        if key != self._focused:
            self._focused = key
            self._retarget()

    def _retarget(self) -> None:
        active = self.active()
        position = self._keys.index(active) if active is not None else -1
        for index, key in enumerate(self._keys):
            if position < 0:
                target = 1.0
            elif index == position:
                target = self.grow
            elif abs(index - position) == 1:
                target = self.near
            else:
                target = 1.0
            if target != self._targets.get(key, 1.0):
                self._targets[key] = target
                self._animate(key, target)

    def _animate(self, key, target: float) -> None:
        running = self._animations.pop(key, None)
        if running is not None:
            running.stop()
        start = self.scale(key)
        if abs(start - target) < 1e-6:
            self._set(key, target, True)
            return
        time = duration("fast")
        if time <= 0:
            self._set(key, target, True)
            return
        animation = Animation(self._pace if self._pace is not None else self)
        animation.setStartValue(float(start))
        animation.setEndValue(float(target))
        animation.setDuration(time)
        animation.setEasingCurve(easing("decelerate"))
        animation.valueChanged.connect(lambda value, k=key: self._set(k, float(value), False))
        animation.finished.connect(lambda k=key, a=animation: self._finished(k, a))
        self._animations[key] = animation
        animation.start()

    def _finished(self, key, animation: Animation) -> None:
        if self._animations.get(key) is animation:
            del self._animations[key]
            self._set(key, self._targets.get(key, 1.0), True)

    def _set(self, key, value: float, settled: bool) -> None:
        if value == 1.0 and settled:
            self._scales.pop(key, None)
        else:
            self._scales[key] = value
        self.scale_changed.emit(key, value, settled)


def _icon_of(row: QWidget):
    """The IconWidget of a row: SettingsCard and ListItem have ``icon_widget``, an expander its header's."""
    icon = getattr(row, "icon_widget", None)
    if icon is None and getattr(row, "header", None) is not None:
        icon = getattr(row.header, "icon_widget", None)
    return icon


class IconMagnifier(Magnifier):
    """Magnifies the icons of rows of widgets (``SettingsCard``, ``ListItem``, ``SettingsExpander``).

    ``set_rows(rows)`` names the rows in their visual order (again whenever they change). A row reserves
    room for its icon to grow (``set_icon_magnification`` where it has it). The pointer is followed through
    the Enter and Leave events of the rows; the focus through the application's focus changes (the row
    that holds the focused widget).
    """

    def __init__(self, parent: QWidget, *, grow: float = GROW, near: float = NEAR) -> None:
        super().__init__(parent, grow=grow, near=near, pace=parent)
        self._leave = QTimer(self)
        self._leave.setSingleShot(True)
        self._leave.setInterval(LEAVE_DELAY_MS)
        self._leave.timeout.connect(lambda: self.set_hovered(None))
        self.scale_changed.connect(self._show)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._focus_changed)

    def set_rows(self, rows: list[QWidget]) -> None:
        rows = [row for row in rows if row is not None]
        for row in self.keys():
            if row not in rows:
                try:
                    row.removeEventFilter(self)
                except RuntimeError:          # deleted already
                    pass
        for row in rows:
            if row not in self.keys():
                row.installEventFilter(self)
                reserve = getattr(row, "set_icon_magnification", None)
                if callable(reserve):
                    reserve(self.grow)
                else:
                    icon = _icon_of(row)
                    if icon is not None:
                        icon.set_magnification(self.grow)
                row.destroyed.connect(self._row_destroyed)
        self.set_keys(rows)
        focus = QApplication.focusWidget()
        self.set_focused(self._row_of(focus))

    def rows(self) -> list[QWidget]:
        return self.keys()

    def _row_destroyed(self, row: QObject) -> None:
        self.set_keys([key for key in self.keys() if key is not row])

    def _row_of(self, widget: QWidget | None) -> QWidget | None:
        rows = self.keys()
        while widget is not None:
            if widget in rows:
                return widget
            widget = widget.parentWidget()
        return None

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Enter:
            self._leave.stop()
            self.set_hovered(obj)
        elif kind == QEvent.Type.Leave and obj is self.hovered():
            self._leave.start()
        return False

    def _focus_changed(self, _old: QWidget | None, new: QWidget | None) -> None:
        try:
            self.set_focused(self._row_of(new))
        except RuntimeError:                  # a row was deleted while the focus moved
            pass

    def _show(self, row: QWidget, scale: float, settled: bool) -> None:
        icon = _icon_of(row)
        if icon is not None:
            try:
                icon.set_scale(scale, settled)
            except RuntimeError:
                pass


class ViewMagnifier(Magnifier):
    """Magnifies the icons of the rows of an item view; its delegate draws them at ``scale(index.row())``.

    ``accept(index)`` tells the rows that have an icon to magnify (headers and notes do not count as
    neighbours either). The pointer is followed through the view's mouse moves, the focus through its
    current index while it has the focus. Each step repaints only the row that changed.
    """

    def __init__(self, view: QAbstractItemView, *, grow: float = GROW, near: float = NEAR,
                 accept: Callable[[QModelIndex], bool] | None = None) -> None:
        super().__init__(view, grow=grow, near=near, pace=view.viewport())
        self.view = view
        self._accept = accept or (lambda _index: True)
        view.setMouseTracking(True)
        view.viewport().setMouseTracking(True)
        view.viewport().installEventFilter(self)
        view.installEventFilter(self)
        self.scale_changed.connect(self._show)
        self._model = None
        self._watch_model()

    def _watch_model(self) -> None:
        model = self.view.model()
        if model is self._model:
            return
        self._model = model
        if model is None:
            return
        for signal in (model.modelReset, model.layoutChanged):
            signal.connect(self.refresh)
        for signal in (model.rowsInserted, model.rowsRemoved, model.rowsMoved):
            signal.connect(lambda *_args: self.refresh())
        selection = self.view.selectionModel()
        if selection is not None:
            selection.currentChanged.connect(lambda *_args: self._sync_focus())
        self.refresh()

    def refresh(self) -> None:
        """Read the rows again (the view's model changed)."""
        try:
            model = self.view.model()
        except RuntimeError:                  # the view is being destroyed
            return
        if model is None:
            self.set_keys([])
            return
        keys = [row for row in range(model.rowCount()) if self._accept(model.index(row, 0))]
        if keys != self.keys():
            self.reset()
            self.set_keys(keys)
        self._sync_focus()

    def _sync_focus(self) -> None:
        try:
            index = self.view.currentIndex()
            focused = self.view.hasFocus()
        except RuntimeError:
            return
        self.set_focused(index.row() if focused and index.isValid() else None)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        try:
            kind = event.type()
            if obj is self.view.viewport():
                if kind in (QEvent.Type.MouseMove, QEvent.Type.HoverMove, QEvent.Type.HoverEnter):
                    index = self.view.indexAt(event.position().toPoint())
                    self.set_hovered(index.row() if index.isValid() else None)
                elif kind in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
                    self.set_hovered(None)
            elif obj is self.view and kind in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
                self._sync_focus()
        except RuntimeError:                  # the view is being destroyed
            pass
        return False

    def _show(self, row: int, _scale: float, _settled: bool) -> None:
        try:
            model = self.view.model()
            if model is not None and 0 <= row < model.rowCount():
                rect = self.view.visualRect(model.index(row, 0))
                if not rect.isEmpty():
                    self.view.viewport().update(rect)
        except RuntimeError:
            pass
