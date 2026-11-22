"""Choosing one of a few: Pivot, SegmentedControl. Showing where you are: BreadcrumbBar."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QIcon, QPainter
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLayout, QMenu, QSizePolicy, QToolButton, QWidget

from .. import icons, paint
from ..animation import Animation
from ..theming import current_theme, duration, easing, font, qcolor
from .basic import to_icon


class _SelectorItem(QAbstractButton):
    """One choice of a Pivot or SegmentedControl. Left and Right move to the neighbours."""

    def __init__(self, owner: "_Selector", key: str, text: str, qicon: QIcon) -> None:
        super().__init__(owner)
        self.owner, self.key = owner, key
        self.setText(text)
        self.setIcon(qicon)
        self.setCheckable(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setAccessibleName(text)
        self.setSizePolicy(QSizePolicy.Policy.Fixed if not owner.STRETCH else QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        width = metrics.horizontalAdvance(self.text()) + 2 * self.owner.PADDING
        if not self.icon().isNull():
            width += 16 + (8 if self.text() else 0)
        return QSize(max(width, 40), self.owner.ITEM_HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def nextCheckState(self) -> None:
        if not self.isChecked():          # a click never unselects the current choice
            self.setChecked(True)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End):
            self.owner.step(self.key, key)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        self.owner.paint_item(painter, self)

    def content_rect(self) -> QRectF:
        """Where icon and text are drawn (the indicator of a Pivot is centred under it)."""
        return QRectF(self.rect())

    def paint_content(self, painter: QPainter, colour: str) -> None:
        metrics = QFontMetrics(self.font())
        text_width = metrics.horizontalAdvance(self.text())
        icon_width = 16 + (8 if self.text() else 0) if not self.icon().isNull() else 0
        x = (self.width() - text_width - icon_width) / 2
        if icon_width:
            mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
            self.icon().paint(painter, round(x), (self.height() - 16) // 2, 16, 16, Qt.AlignmentFlag.AlignCenter, mode)
            x += icon_width
        painter.setFont(self.font())
        painter.setPen(qcolor(colour))
        painter.drawText(QRectF(x, 0, text_width + 2, self.height() - self.owner.TEXT_LIFT),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())


class _Selector(QWidget):
    """What Pivot and SegmentedControl share: items by key, one current, an indicator that slides."""

    current_changed = Signal(str)
    ITEM_HEIGHT = 40
    PADDING = 12
    SPACING = 4
    STRETCH = False
    TEXT_LIFT = 0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: dict[str, _SelectorItem] = {}
        self._current: str | None = None
        self._indicator: QRectF | None = None
        self._animation: Animation | None = None
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(*self._margins())
        self._layout.setSpacing(self.SPACING)
        if not self.STRETCH:
            self._layout.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def _margins(self) -> tuple[int, int, int, int]:
        return 0, 0, 0, 0

    def add_item(self, key: str, text: str, icon: QIcon | str | None = None) -> None:
        """Add a choice. The first one added becomes the current one."""
        if key in self._items:
            raise ValueError(f"duplicate key {key!r}")
        item = _SelectorItem(self, key, text, to_icon(icon))
        item.setFont(self._item_font())
        item.toggled.connect(lambda checked, k=key: self._toggled(k, checked))
        self._items[key] = item
        position = self._layout.count() - (0 if self.STRETCH else 1)
        self._layout.insertWidget(position, item)
        if self._current is None:
            self.set_current(key)
        else:
            item.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _item_font(self):
        return font("body")

    def keys(self) -> list[str]:
        return list(self._items)

    def count(self) -> int:
        return len(self._items)

    def item(self, key: str) -> QAbstractButton:
        return self._items[key]

    def set_item_text(self, key: str, text: str) -> None:
        item = self._items[key]
        item.setText(text)
        item.setAccessibleName(text)
        item.updateGeometry()
        self._indicator = None
        self.update()

    def current(self) -> str | None:
        return self._current

    def set_current(self, key: str) -> None:
        if key not in self._items:
            raise KeyError(f"unknown key {key!r}")
        self._items[key].setChecked(True)

    def _toggled(self, key: str, checked: bool) -> None:
        if not checked:
            return
        previous = self._current
        self._current = key
        for other_key, other in self._items.items():
            if other_key != key and other.isChecked():
                blocked = other.blockSignals(True)
                other.setChecked(False)
                other.blockSignals(blocked)
                other.update()
            # One tab stop for the whole control: the current item.
            other.setFocusPolicy(Qt.FocusPolicy.StrongFocus if other_key == key else Qt.FocusPolicy.NoFocus)
        self._slide(previous)
        if previous != key:
            self.current_changed.emit(key)

    def step(self, key: str, pressed: int) -> None:
        """Move from the item ``key`` with an arrow key, Home or End."""
        keys = [k for k, item in self._items.items() if item.isEnabled() and not item.isHidden()]
        if key not in keys:
            return
        index = keys.index(key)
        if pressed == Qt.Key.Key_Home:
            index = 0
        elif pressed == Qt.Key.Key_End:
            index = len(keys) - 1
        else:
            forward = pressed == Qt.Key.Key_Right
            if self.layoutDirection() == Qt.LayoutDirection.RightToLeft:
                forward = not forward
            index = max(0, min(len(keys) - 1, index + (1 if forward else -1)))
        target = self._items[keys[index]]
        target.setChecked(True)
        target.setFocus(Qt.FocusReason.TabFocusReason)

    # --- the indicator ---
    def indicator_for(self, item: _SelectorItem) -> QRectF:
        raise NotImplementedError

    def indicator_rect(self) -> QRectF | None:
        """Where the indicator is painted now (None before anything is selected)."""
        if self._animation is not None and self._indicator is not None:
            return QRectF(self._indicator)
        if self._current is None:
            return None
        return self.indicator_for(self._items[self._current])

    def _slide(self, previous: str | None) -> None:
        if self._animation is not None:
            self._animation.stop()
            self._animation = None
        time = duration("fast")
        if previous is None or previous not in self._items or time <= 0 or not self.isVisible():
            self._indicator = None
            self.update()
            return
        start = self._indicator if self._indicator is not None else self.indicator_for(self._items[previous])
        self._indicator = QRectF(start)       # where it is painted until the animation's first step
        animation = Animation(self)
        animation.setStartValue(QRectF(start))
        animation.setEndValue(self.indicator_for(self._items[self._current]))
        animation.setDuration(time)
        animation.setEasingCurve(easing("decelerate"))
        animation.valueChanged.connect(self._animate)
        animation.finished.connect(self._animation_done)
        animation.start()
        self._animation = animation

    def _animate(self, value) -> None:
        self._indicator = QRectF(value)
        self.update()

    def _animation_done(self) -> None:
        self._animation = None
        self._indicator = None
        self.update()

    def paint_item(self, painter: QPainter, item: _SelectorItem) -> None:
        raise NotImplementedError


class Pivot(_Selector):
    """Tabs as plain words; the current one is underlined with the accent pill (``current_changed(key)``).

    Use it to switch between a few views of the same page. Keyboard: Left,
    Right, Home, End.
    """

    ITEM_HEIGHT = 40
    PADDING = 12
    SPACING = 0
    TEXT_LIFT = 2

    def indicator_for(self, item: _SelectorItem) -> QRectF:
        geometry = item.geometry()
        return QRectF(geometry.center().x() - 7.5, geometry.bottom() - 3, 16, 3)

    def paint_item(self, painter: QPainter, item: _SelectorItem) -> None:
        enabled = item.isEnabled()
        if enabled and (item.isDown() or item.underMouse()):
            box = QRectF(item.rect()).adjusted(0, 2, 0, -6)
            paint.rounded_rect(painter, box, current_theme().radius("control"),
                               "fill-subtle-pressed" if item.isDown() else "fill-subtle-hover")
        colour = "text-disabled" if not enabled else ("text" if item.isChecked() or item.underMouse()
                                                      else "text-secondary")
        item.paint_content(painter, colour)

    def paintEvent(self, _event) -> None:
        rect = self.indicator_rect()
        if rect is not None:
            paint.rounded_rect(QPainter(self), rect, 1.5, "accent" if self.isEnabled() else "text-disabled")


class SegmentedControl(_Selector):
    """A row of joined segments, one of them selected (``current_changed(key)``).

    Use it for a setting with two to five exclusive values. Keyboard: Left,
    Right, Home, End.
    """

    ITEM_HEIGHT = 30
    PADDING = 16
    SPACING = 0
    STRETCH = True

    def _margins(self) -> tuple[int, int, int, int]:
        return 1, 1, 1, 1

    def indicator_for(self, item: _SelectorItem) -> QRectF:
        return QRectF(item.geometry())

    def paint_item(self, painter: QPainter, item: _SelectorItem) -> None:
        enabled = item.isEnabled()
        if enabled and not item.isChecked() and (item.isDown() or item.underMouse()):
            paint.rounded_rect(painter, QRectF(item.rect()).adjusted(2, 2, -2, -2), current_theme().radius("control"),
                               "fill-subtle-pressed" if item.isDown() else "fill-subtle-hover")
        item.paint_content(painter, "text" if enabled else "text-disabled")

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        radius = current_theme().radius("control")
        paint.rounded_rect(painter, self.rect(), radius, "fill-control", "stroke-control")
        rect = self.indicator_rect()
        if rect is not None:
            # The selected segment is raised: lighter than the track in both schemes.
            raised = "fill-control-hover" if current_theme().is_dark else "surface"
            paint.rounded_rect(painter, rect, radius, raised, "stroke-surface")
            pill = QRectF(rect.center().x() - 7.5, rect.bottom() - 2.5, 16, 3)
            paint.rounded_rect(painter, pill, 1.5, "accent" if self.isEnabled() else "text-disabled")


# --- breadcrumb ---------------------------------------------------------------------------------------------

class _Crumb(QAbstractButton):
    def __init__(self, owner: "BreadcrumbBar", index: int, text: str) -> None:
        super().__init__(owner)
        self.owner, self.index = owner, index
        self.setText(text)
        self.setAccessibleName(text)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFont(owner.font_())
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def is_last(self) -> bool:
        return self.index == self.owner.count() - 1

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance(self.text()) + 2, metrics.height() + 4)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if not self.isEnabled():
            colour = "text-disabled"
        elif self.is_last():
            colour = "text"
        else:
            colour = "text-secondary" if self.isDown() or not self.underMouse() else "text"
        painter.setFont(self.font())
        painter.setPen(qcolor(colour))
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())


class _Chevron(QWidget):
    def __init__(self, parent: QWidget, size: int) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size + 8, size + 8)

    def paintEvent(self, _event) -> None:
        paint.glyph(QPainter(self), self.rect(), "chevron-right", "text-secondary", self._size)


class BreadcrumbBar(QWidget):
    """The path to the current place: ``Home > Documents > Reports``.

    ``item_clicked(index)`` is emitted for every crumb but the last, which is
    the current place. When the bar is too narrow the first crumbs move into
    a "..." menu. ``style`` is a step of the type ramp (``"title"`` makes it
    a page header as in Windows Settings).
    """

    item_clicked = Signal(int)

    def __init__(self, parent: QWidget | None = None, *, items: list[str] | tuple[str, ...] = (),
                 style: str = "body") -> None:
        super().__init__(parent)
        self._style = style
        self._texts: list[str] = []
        self._crumbs: list[_Crumb] = []
        self._chevrons: list[_Chevron] = []
        self._hidden = 0
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        # The bar may be narrower than its path: the first crumbs then move into the menu.
        self._layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.overflow_button = QToolButton(self)
        self.overflow_button.setIcon(icons.glyph_icon("more"))
        self.overflow_button.setAccessibleName("Earlier places")
        self.overflow_button.setToolTip("Earlier places")
        self.overflow_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.overflow_button.setProperty("noArrow", True)
        self._overflow_chevron = _Chevron(self, self._chevron_size())
        self._layout.addWidget(self.overflow_button)
        self._layout.addWidget(self._overflow_chevron)
        self._layout.addStretch(1)
        self.overflow_button.hide()
        self._overflow_chevron.hide()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Path")
        self.set_items(list(items))

    def font_(self):
        return font(self._style)

    def _chevron_size(self) -> int:
        return max(10, round(current_theme().type(self._style).size * 0.6))

    def items(self) -> list[str]:
        return list(self._texts)

    def count(self) -> int:
        return len(self._texts)

    def set_items(self, items: list[str]) -> None:
        """Replace the path; the last item is the current place."""
        for widget in self._crumbs + self._chevrons:
            self._layout.removeWidget(widget)
            widget.deleteLater()
        self._crumbs, self._chevrons = [], []
        self._texts = [str(item) for item in items]
        position = 2
        for index, text in enumerate(self._texts):
            crumb = _Crumb(self, index, text)
            crumb.clicked.connect(lambda _checked=False, i=index: self._clicked(i))
            last = index == len(self._texts) - 1
            crumb.setFocusPolicy(Qt.FocusPolicy.NoFocus if last else Qt.FocusPolicy.StrongFocus)
            crumb.setCursor(Qt.CursorShape.ArrowCursor if last else Qt.CursorShape.PointingHandCursor)
            self._layout.insertWidget(position, crumb)
            self._crumbs.append(crumb)
            position += 1
            if not last:
                chevron = _Chevron(self, self._chevron_size())
                self._layout.insertWidget(position, chevron)
                self._chevrons.append(chevron)
                position += 1
        self._fit()

    def push(self, text: str) -> None:
        """Go one level deeper."""
        self.set_items(self._texts + [text])

    def _clicked(self, index: int) -> None:
        if index < len(self._texts) - 1:
            self.item_clicked.emit(index)

    def hidden_count(self) -> int:
        """How many leading crumbs are in the "..." menu now."""
        return self._hidden

    def _fit(self) -> None:
        widths = [crumb.sizeHint().width() for crumb in self._crumbs]
        chevron = self._chevron_size() + 8
        available = self.width()
        hidden = 0
        if self._crumbs and available > 0:
            def needed(first: int) -> int:
                total = sum(widths[first:]) + chevron * max(0, len(widths) - first - 1)
                if first:
                    total += self.overflow_button.sizeHint().width() + chevron
                return total
            while hidden < len(self._crumbs) - 1 and needed(hidden) > available:
                hidden += 1
        self._hidden = hidden
        for index, crumb in enumerate(self._crumbs):
            crumb.setVisible(index >= hidden)
            if index < len(self._chevrons):
                self._chevrons[index].setVisible(index >= hidden)
        self.overflow_button.setVisible(hidden > 0)
        self._overflow_chevron.setVisible(hidden > 0)
        if hidden:
            menu = QMenu(self.overflow_button)
            for index in range(hidden):
                menu.addAction(self._texts[index]).triggered.connect(lambda _c=False, i=index: self._clicked(i))
            old = self.overflow_button.menu()
            self.overflow_button.setMenu(menu)
            if old is not None:
                old.deleteLater()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font_())
        width = sum(crumb.sizeHint().width() for crumb in self._crumbs)
        width += (self._chevron_size() + 8) * len(self._chevrons)
        return QSize(width, max(metrics.height() + 4, self._chevron_size() + 8))

    def minimumSizeHint(self) -> QSize:
        return QSize(48, self.sizeHint().height())
