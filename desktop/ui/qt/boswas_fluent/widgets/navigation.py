"""NavigationView: the pane on the left, the page title and the pages of an application."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QEvent, QModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QIcon, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QBoxLayout, QFrame, QHBoxLayout, QListWidget, QListWidgetItem,
                               QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate, QToolTip, QVBoxLayout,
                               QWidget)

from .. import glass, icons, paint
from ..animation import Animation
from ..theming import current_theme, duration, easing, font, qcolor
from .basic import IconButton, Label, to_icon
from .inputs import SearchBox

KEY_ROLE = Qt.ItemDataRole.UserRole
KIND_ROLE = Qt.ItemDataRole.UserRole + 1
BADGE_ROLE = Qt.ItemDataRole.UserRole + 2
ITEM, ACTION, SECTION, SEPARATOR = "item", "action", "section", "separator"

ROW_HEIGHT = 40          # 36 px item and 2 px above and below
_INSET = 4               # between the pane edge and an item
_ICON_X = 16             # so the icon is centred in the 48 px compact pane
_TEXT_X = 48


class NavigationDelegate(QStyledItemDelegate):
    """Paints the items of the pane: state layers, icon, label, badge; headers and separators."""

    def __init__(self, view: "NavigationList") -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        kind = index.data(KIND_ROLE)
        if kind == SEPARATOR:
            return QSize(option.rect.width(), 9)
        if kind == SECTION and self._view.compact:
            return QSize(option.rect.width(), 9)
        return QSize(option.rect.width(), ROW_HEIGHT)

    def helpEvent(self, event, view, option, index: QModelIndex) -> bool:
        if event.type() == QEvent.Type.ToolTip and index.isValid() and index.data(KIND_ROLE) in (ITEM, ACTION):
            text = index.data(Qt.ItemDataRole.ToolTipRole) or ""
            if self._view.compact:
                text = index.data(Qt.ItemDataRole.DisplayRole) or text
            if text:
                QToolTip.showText(event.globalPos(), text, view)
                return True
            QToolTip.hideText()
            return True
        return super().helpEvent(event, view, option, index)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        kind = index.data(KIND_ROLE)
        rect = QRectF(option.rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if kind == SEPARATOR or (kind == SECTION and self._view.compact):
            painter.fillRect(QRectF(rect.left(), rect.center().y(), rect.width(), 1), qcolor("stroke-divider"))
            painter.restore()
            return
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if kind == SECTION:
            paint.elided(painter, rect.adjusted(_ICON_X, 0, -_INSET * 2, 0), text, font("body-strong"), "text")
            painter.restore()
            return
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        pressed = self._view.pressed_row == index.row()
        box = rect.adjusted(_INSET, 2, -_INSET, -2)
        radius = current_theme().radius("control")
        if enabled and (pressed or (selected and hovered)):
            paint.rounded_rect(painter, box, radius, "fill-subtle-pressed")
        elif enabled and (selected or hovered):
            paint.rounded_rect(painter, box, radius, "fill-subtle-hover")
        qicon = index.data(Qt.ItemDataRole.DecorationRole)
        size = self._view.iconSize().width()
        if isinstance(qicon, QIcon) and not qicon.isNull():
            mode = QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled
            target = QRect(round(rect.left() + _ICON_X + (16 - size) / 2), round(rect.center().y() - size / 2),
                           size, size)
            qicon.paint(painter, target, Qt.AlignmentFlag.AlignCenter, mode)
        if not self._view.compact:
            right = _INSET + 8
            badge = index.data(BADGE_ROLE)
            if badge:
                label = font("caption")
                width = max(16, QFontMetrics(label).horizontalAdvance(str(badge)) + 8)
                pill = QRectF(rect.right() - right - width, rect.center().y() - 8, width, 16)
                paint.rounded_rect(painter, pill, 8, "accent" if enabled else "accent-disabled")
                paint.elided(painter, pill, str(badge), label, "on-accent" if enabled else "text-disabled",
                             Qt.AlignmentFlag.AlignCenter)
                right += width + 8
            paint.elided(painter, QRectF(rect.left() + _TEXT_X, rect.top(), rect.width() - _TEXT_X - right,
                                         rect.height()), text, option.font, "text" if enabled else "text-disabled")
        painter.restore()


class NavigationList(QListWidget):
    """The list of a navigation pane. Arrow keys skip hidden rows, headers and separators.

    The accent pill that marks the selected item is drawn here and slides
    from the old item to the new one.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.compact = False
        self.pressed_row = -1
        self._pill: QRectF | None = None
        self._animation: Animation | None = None
        self.setItemDelegate(NavigationDelegate(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setIconSize(QSize(16, 16))
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformItemSizes(False)
        self.setSpacing(0)
        self.currentRowChanged.connect(self._current_changed)

    def selectable(self, row: int) -> bool:
        item = self.item(row)
        return (item is not None and not item.isHidden() and item.data(KIND_ROLE) in (ITEM, ACTION)
                and bool(item.flags() & Qt.ItemFlag.ItemIsEnabled))

    def moveCursor(self, action, modifiers):
        index = super().moveCursor(action, modifiers)
        actions = QAbstractItemView.CursorAction
        backwards = action in (actions.MoveUp, actions.MovePageUp, actions.MovePrevious, actions.MoveLeft,
                               actions.MoveEnd)
        if action == actions.MoveHome:
            backwards = False
        step = -1 if backwards else 1
        row = index.row()
        while 0 <= row < self.count() and not self.selectable(row):
            row += step
        if not 0 <= row < self.count():
            return self.currentIndex()
        return self.model().index(row, 0)

    def visible_rows(self) -> int:
        return sum(1 for row in range(self.count()) if not self.item(row).isHidden())

    def content_height(self) -> int:
        """The height of all visible rows."""
        total = 0
        for row in range(self.count()):
            if not self.item(row).isHidden():
                total += self.sizeHintForRow(row) if self.sizeHintForRow(row) > 0 else ROW_HEIGHT
        return total

    def focusInEvent(self, event) -> None:
        # Qt gives a list without a current row its first row as soon as it
        # gets the keyboard. Here the current row is the page that is shown,
        # so that would switch pages just by tabbing into the pane, or when
        # a page that held the keyboard is hidden and the focus moves on.
        # The row becomes the keyboard position only: an arrow key, Enter or
        # Space selects.
        if self.currentRow() >= 0:
            super().focusInEvent(event)
            return
        blocked = self.blockSignals(True)
        try:
            super().focusInEvent(event)
        finally:
            self.blockSignals(blocked)
        self.viewport().update()

    def keyPressEvent(self, event) -> None:
        item = self.currentItem()
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and item is not None
                and not item.isSelected() and self.selectable(self.currentRow())):
            # The keyboard position that focusInEvent gave: now it is chosen.
            item.setSelected(True)
            self.currentRowChanged.emit(self.currentRow())
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        self.pressed_row = self.indexAt(event.position().toPoint()).row()
        self.viewport().update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self.pressed_row = -1
        self.viewport().update()
        super().mouseReleaseEvent(event)

    def set_compact(self, compact: bool) -> None:
        self.compact = compact
        self.scheduleDelayedItemsLayout()
        self.viewport().update()

    # --- the selection pill ---
    def _pill_for(self, row: int) -> QRectF | None:
        item = self.item(row)
        if item is None or item.isHidden() or item.data(KIND_ROLE) != ITEM or not item.isSelected():
            return None
        rect = self.visualItemRect(item)
        return QRectF(rect.left() + _INSET, rect.center().y() - 7.5, 3, 16)

    def _current_changed(self, row: int) -> None:
        target = self._pill_for(row) if row >= 0 else None
        if self._animation is not None:
            self._animation.stop()
            self._animation = None
        time = duration("fast")
        if target is None or self._pill is None or time <= 0 or not self.isVisible():
            self._pill = None            # painted where the current row is
            self.viewport().update()
            return
        animation = Animation(self)
        animation.setStartValue(QRectF(self._pill))
        animation.setEndValue(target)
        animation.setDuration(time)
        animation.setEasingCurve(easing("decelerate"))
        animation.valueChanged.connect(self._animate)
        animation.finished.connect(self._animation_done)
        animation.start()
        self._animation = animation

    def _animate(self, value) -> None:
        self._pill = QRectF(value)
        self.viewport().update()

    def _animation_done(self) -> None:
        self._animation = None
        self.viewport().update()

    def pill_rect(self) -> QRectF | None:
        """Where the selection pill is painted now, in viewport coordinates (None: no selection here)."""
        if self._animation is not None and self._pill is not None:
            return QRectF(self._pill)
        return self._pill_for(self.currentRow())

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        pill = self.pill_rect()
        if self._animation is None:
            self._pill = pill
        if pill is None:
            return
        painter = QPainter(self.viewport())
        paint.rounded_rect(painter, pill, 1.5, "accent" if self.isEnabled() else "text-disabled")


@dataclass
class _Entry:
    key: str
    title: str
    heading: str
    widget: QWidget | None = None
    factory: Callable[[], QWidget] | None = None
    footer: bool = False
    kind: str = ITEM
    keywords: tuple[str, ...] = field(default_factory=tuple)
    item: QListWidgetItem | None = None
    visible: bool = True


class _BannerArea(QWidget):
    """Holds InfoBars above the page; takes no room while every one of them is hidden."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 8)
        self._layout.setSpacing(4)
        self.hide()

    def add(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)
        widget.installEventFilter(self)
        self._sync()

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.ShowToParent, QEvent.Type.HideToParent):
            self._sync(obj, event.type() == QEvent.Type.ShowToParent)
        return False

    def _sync(self, changed: QWidget | None = None, shown: bool = False) -> None:
        visible = False
        for index in range(self._layout.count()):
            widget = self._layout.itemAt(index).widget()
            if widget is None:
                continue
            visible = visible or (shown if widget is changed else not widget.isHidden())
        self.setVisible(visible)


class NavigationView(QWidget):
    """The shell of an application with several pages.

    On the left a pane of items (icon and label, optional sections, footer
    items at the bottom, an optional search box and back button); on the
    right the title of the current page, a place for InfoBars and the page
    itself. Under ``compact_below`` pixels of width the pane shows icons
    only.

    Keyboard: Up and Down move through the items and show their pages, Tab
    moves from the pane into the page, Ctrl+F (when there is a search box)
    is left to the application (``search_box.setFocus()``).
    """

    current_changed = Signal(str)        # key of the page now shown
    item_invoked = Signal(str)           # key of an action item that was clicked
    back_requested = Signal()
    search_changed = Signal(str)
    search_submitted = Signal(str)

    COMPACT_WIDTH = 48

    def __init__(self, parent: QWidget | None = None, *, search: bool = False, back_button: bool = False,
                 pane_toggle: bool = False, header: bool = True, pane_width: int = 280, compact_below: int = 720,
                 content_layer: bool = False) -> None:
        super().__init__(parent)
        self._entries: dict[str, _Entry] = {}
        self._order: list[str] = []
        self._current: str | None = None
        self._history: list[str] = []
        self._pane_width = pane_width
        self._compact_below = compact_below
        self._forced_compact: bool | None = None
        self._compact = False
        self._title_override: str | None = None
        self._search_filters = True
        self._content_layer = content_layer
        self._anonymous = 0
        self._width_animation: Animation | None = None

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- the pane ---
        self.pane = QWidget(self)
        self.pane.setObjectName("fluentNavigationPane")
        self.pane.setFixedWidth(pane_width)
        pane = QVBoxLayout(self.pane)
        pane.setContentsMargins(0, 8, 0, 4)
        pane.setSpacing(0)
        self._buttons = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._buttons.setContentsMargins(_INSET, 0, _INSET, 0)
        self._buttons.setSpacing(0)
        self.back_button: IconButton | None = None
        self.toggle_button: IconButton | None = None
        if back_button:
            self.back_button = IconButton(icons.icon("go-previous-symbolic"), "Back", self.pane, size=40)
            self.back_button.setFixedHeight(36)
            self.back_button.setEnabled(False)
            self.back_button.clicked.connect(self.go_back)
            self._buttons.addWidget(self.back_button)
        if pane_toggle:
            self.toggle_button = IconButton(icons.icon("open-menu-symbolic"), "Open or close navigation", self.pane,
                                            size=40)
            self.toggle_button.setFixedHeight(36)
            self.toggle_button.clicked.connect(lambda: self.set_compact(not self._compact))
            self._buttons.addWidget(self.toggle_button)
        self._buttons.addStretch(1)
        if back_button or pane_toggle:
            pane.addLayout(self._buttons)
            pane.addSpacing(4)
        self._pane_header = QVBoxLayout()
        self._pane_header.setContentsMargins(0, 0, 0, 0)
        pane.addLayout(self._pane_header)
        self.search_box: SearchBox | None = None
        self.search_button: IconButton | None = None
        if search:
            holder = QVBoxLayout()
            holder.setContentsMargins(16, 4, 16, 8)
            self.search_box = SearchBox(self.pane, placeholder="Find a setting")
            self.search_box.textChanged.connect(self._search_text_changed)
            self.search_box.query_submitted.connect(self._search_submitted)
            holder.addWidget(self.search_box)
            pane.addLayout(holder)
            self.search_button = IconButton(icons.icon("system-search-symbolic"), "Search", self.pane, size=40)
            self.search_button.setFixedHeight(36)
            self.search_button.clicked.connect(self.focus_search)
            self.search_button.hide()
            row = QHBoxLayout()
            row.setContentsMargins(_INSET, 0, _INSET, 0)
            row.addWidget(self.search_button)
            row.addStretch(1)
            pane.addLayout(row)
        self.list = NavigationList(self.pane)
        self.list.setAccessibleName("Navigation")
        self.list.setAccessibleDescription("Use the Up and Down arrow keys to switch between pages")
        pane.addWidget(self.list, 1)
        self.footer_list = NavigationList(self.pane)
        self.footer_list.setAccessibleName("More navigation")
        self.footer_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.footer_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.footer_list.setFixedHeight(0)
        self.footer_list.hide()
        pane.addWidget(self.footer_list)
        outer.addWidget(self.pane)

        # --- the content ---
        self.content = QWidget(self)
        self.content.setObjectName("fluentNavigationContent")
        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.header = QWidget(self.content)
        self.header_layout = QHBoxLayout(self.header)
        self.header_layout.setContentsMargins(32, 20, 32, 12)
        self.header_layout.setSpacing(8)
        self.title_label = Label("", "title", self.header)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.header_layout.addWidget(self.title_label, 1)
        self.header.setVisible(header)
        content.addWidget(self.header)
        self._banner_holder = QWidget(self.content)
        holder = QVBoxLayout(self._banner_holder)
        holder.setContentsMargins(32, 0, 32, 0)
        self.banners = _BannerArea(self._banner_holder)
        holder.addWidget(self.banners)
        content.addWidget(self._banner_holder)
        self._banner_holder.hide()
        self.banners.installEventFilter(self)
        self.stack = QStackedWidget(self.content)
        content.addWidget(self.stack, 1)
        outer.addWidget(self.content, 1)

        self.list.currentRowChanged.connect(lambda row: self._row_changed(self.list, row))
        self.footer_list.currentRowChanged.connect(lambda row: self._row_changed(self.footer_list, row))
        self.list.itemClicked.connect(self._item_clicked)
        self.footer_list.itemClicked.connect(self._item_clicked)
        self.list.itemActivated.connect(self._item_activated)
        self.footer_list.itemActivated.connect(self._item_activated)
        self.setFocusProxy(self.list)

    # --- building ---
    def add_page(self, key: str, title: str, widget: QWidget | None = None, *, icon: QIcon | str | None = None,
                 factory: Callable[[], QWidget] | None = None, footer: bool = False, tooltip: str = "",
                 heading: str | None = None, keywords: tuple[str, ...] | list[str] = ()) -> None:
        """Add an item and its page.

        Give the page as ``widget``, or a ``factory`` that builds it the first
        time the item is selected. ``heading`` is the title shown above the
        page (default: ``title``). ``footer=True`` puts the item at the bottom
        of the pane. ``keywords`` are extra words the search box finds the
        item by.
        """
        if key in self._entries:
            raise ValueError(f"duplicate navigation key {key!r}")
        entry = _Entry(key, title, heading if heading is not None else title, widget, factory, footer, ITEM,
                       tuple(keywords))
        self._append(entry, to_icon(icon), tooltip)
        if widget is not None:
            self.stack.addWidget(widget)
        if self._current is None:
            self.set_current(key)

    def add_action(self, key: str, title: str, *, icon: QIcon | str | None = None, footer: bool = True,
                   tooltip: str = "") -> None:
        """Add an item that has no page: clicking it emits ``item_invoked(key)`` and selection stays."""
        if key in self._entries:
            raise ValueError(f"duplicate navigation key {key!r}")
        self._append(_Entry(key, title, title, footer=footer, kind=ACTION), to_icon(icon), tooltip)

    def add_section(self, title: str) -> None:
        """Add a header to the pane; the items added next belong to it."""
        self._anonymous += 1
        self._append(_Entry(f"\0section{self._anonymous}", title, title, kind=SECTION), QIcon(), "")

    def add_separator(self, footer: bool = False) -> None:
        self._anonymous += 1
        self._append(_Entry(f"\0separator{self._anonymous}", "", "", footer=footer, kind=SEPARATOR), QIcon(), "")

    def _append(self, entry: _Entry, qicon: QIcon, tooltip: str) -> None:
        target = self.footer_list if entry.footer else self.list
        item = QListWidgetItem(entry.title)
        if not qicon.isNull():
            item.setIcon(qicon)
        item.setData(KEY_ROLE, entry.key if entry.kind in (ITEM, ACTION) else None)
        item.setData(KIND_ROLE, entry.kind)
        if tooltip:
            item.setToolTip(tooltip)
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, tooltip)
        if entry.kind in (SECTION, SEPARATOR):
            item.setFlags(Qt.ItemFlag.NoItemFlags)
        entry.item = item
        self._entries[entry.key] = entry
        self._order.append(entry.key)
        blocked = target.blockSignals(True)
        target.addItem(item)
        target.blockSignals(blocked)
        self._sync_footer()

    def _sync_footer(self) -> None:
        rows = self.footer_list.count()
        self.footer_list.setVisible(rows > 0)
        if rows:
            height = sum(9 if self.footer_list.item(row).data(KIND_ROLE) == SEPARATOR else ROW_HEIGHT
                         for row in range(rows) if not self.footer_list.item(row).isHidden())
            self.footer_list.setFixedHeight(height)

    def keys(self) -> list[str]:
        """The keys of the pages and action items, in pane order (main items, then footer items)."""
        main = [key for key in self._order if self._entries[key].kind in (ITEM, ACTION)
                and not self._entries[key].footer]
        return main + [key for key in self._order if self._entries[key].kind in (ITEM, ACTION)
                       and self._entries[key].footer]

    def item(self, key: str) -> QListWidgetItem:
        """The list item of ``key`` (for an icon, tool tip or text change)."""
        return self._entry(key).item

    def _entry(self, key: str) -> _Entry:
        try:
            return self._entries[key]
        except KeyError:
            raise KeyError(f"unknown navigation key {key!r}") from None

    # --- pages ---
    def page(self, key: str) -> QWidget | None:
        """The page of ``key``, built now if it has a factory and was never shown."""
        entry = self._entry(key)
        if entry.widget is None and entry.factory is not None:
            entry.widget = entry.factory()
            entry.factory = None
            self.stack.addWidget(entry.widget)
        return entry.widget

    def current(self) -> str | None:
        return self._current

    def set_current(self, key: str) -> None:
        """Select the item ``key`` and show its page."""
        entry = self._entry(key)
        if entry.kind != ITEM:
            raise ValueError(f"{key!r} has no page")
        target = self.footer_list if entry.footer else self.list
        row = target.row(entry.item)
        if target.currentRow() != row:
            target.setCurrentRow(row)
        else:
            self._show(key)

    def _row_changed(self, source: NavigationList, row: int) -> None:
        if row < 0:
            return
        item = source.item(row)
        key = item.data(KEY_ROLE)
        entry = self._entries.get(key) if key else None
        if entry is None or entry.kind != ITEM:
            # Headers, separators and action items are never the current page.
            self._restore_selection()
            return
        other = self.list if source is self.footer_list else self.footer_list
        if other.currentRow() >= 0:
            blocked = other.blockSignals(True)
            other.setCurrentRow(-1)
            other.clearSelection()
            other.blockSignals(blocked)
            other._current_changed(-1)
        self._show(key)

    def _restore_selection(self) -> None:
        for view in (self.list, self.footer_list):
            blocked = view.blockSignals(True)
            view.setCurrentRow(-1)
            view.clearSelection()
            view.blockSignals(blocked)
        if self._current is not None:
            entry = self._entries[self._current]
            view = self.footer_list if entry.footer else self.list
            blocked = view.blockSignals(True)
            view.setCurrentRow(view.row(entry.item))
            view.blockSignals(blocked)
            view.viewport().update()

    def _show(self, key: str) -> None:
        previous = self._current
        widget = self.page(key)
        if widget is not None:
            self.stack.setCurrentWidget(widget)
        self._current = key
        self._update_title()
        if previous is not None and previous != key:
            if not self._going_back:
                self._history.append(previous)
                del self._history[:-50]
            self._sync_back()
        if previous != key:
            self.current_changed.emit(key)

    _going_back = False

    def go_back(self) -> None:
        """Show the page that was shown before this one (the back button); always emits ``back_requested``."""
        self.back_requested.emit()
        while self._history:
            key = self._history.pop()
            entry = self._entries.get(key)
            if entry is not None and entry.visible and entry.kind == ITEM:
                self._going_back = True
                try:
                    self.set_current(key)
                finally:
                    self._going_back = False
                break
        self._sync_back()

    def can_go_back(self) -> bool:
        return bool(self._history)

    def _sync_back(self) -> None:
        if self.back_button is not None:
            self.back_button.setEnabled(bool(self._history))

    def _item_clicked(self, item: QListWidgetItem) -> None:
        if item.data(KIND_ROLE) == ACTION and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            self.item_invoked.emit(item.data(KEY_ROLE))

    def _item_activated(self, item: QListWidgetItem) -> None:
        # Enter on an item: an action item is invoked; a page item moves the focus into its page.
        if item.data(KIND_ROLE) == ACTION:
            self.item_invoked.emit(item.data(KEY_ROLE))

    # --- item state ---
    def set_item_visible(self, key: str, visible: bool) -> None:
        entry = self._entry(key)
        entry.visible = visible
        self._apply_filter()
        self._sync_footer()

    def is_item_visible(self, key: str) -> bool:
        return self._entry(key).visible

    def set_item_enabled(self, key: str, enabled: bool) -> None:
        item = self._entry(key).item
        flags = item.flags()
        item.setFlags(flags | Qt.ItemFlag.ItemIsEnabled if enabled else flags & ~Qt.ItemFlag.ItemIsEnabled)

    def set_item_text(self, key: str, title: str, heading: str | None = None) -> None:
        entry = self._entry(key)
        entry.title = title
        entry.heading = heading if heading is not None else title
        entry.item.setText(title)
        if key == self._current:
            self._update_title()

    def set_item_icon(self, key: str, icon: QIcon | str | None) -> None:
        self._entry(key).item.setIcon(to_icon(icon))

    def set_item_badge(self, key: str, badge: int | str | None) -> None:
        """Show a count or a short word at the trailing edge of the item (None or 0 removes it)."""
        text = "" if badge in (None, 0, "") else ("99+" if isinstance(badge, int) and badge > 99 else str(badge))
        self._entry(key).item.setData(BADGE_ROLE, text)

    # --- header ---
    def title(self) -> str:
        return self.title_label.text()

    def set_title(self, title: str | None) -> None:
        """Show ``title`` above the page instead of the current item's heading (None: back to the heading)."""
        self._title_override = title
        self._update_title()

    def _update_title(self) -> None:
        if self._title_override is not None:
            text = self._title_override
        else:
            text = self._entries[self._current].heading if self._current in self._entries else ""
        self.title_label.setText(text)
        self.title_label.setAccessibleName(text)

    def set_header_visible(self, visible: bool) -> None:
        self.header.setVisible(visible)

    def add_header_widget(self, widget: QWidget) -> QWidget:
        """Add a widget (a button, a command bar) at the trailing edge of the page header; returns it."""
        widget.setParent(self.header)
        self.header_layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        return widget

    def set_pane_header(self, widget: QWidget | None) -> None:
        """Put a widget (an account row, the application name) at the top of the pane; hidden when compact."""
        while self._pane_header.count():
            old = self._pane_header.takeAt(0).widget()
            if old is not None:
                old.deleteLater()
        self._pane_header_widget = widget
        if widget is not None:
            widget.setParent(self.pane)
            self._pane_header.addWidget(widget)
            widget.setVisible(not self._compact)

    _pane_header_widget: QWidget | None = None

    def add_banner(self, widget: QWidget) -> QWidget:
        """Add an InfoBar (or any widget) between the header and the page; returns it."""
        self.banners.add(widget)
        return widget

    def eventFilter(self, obj, event) -> bool:
        if obj is self.banners and event.type() in (QEvent.Type.ShowToParent, QEvent.Type.HideToParent):
            self._banner_holder.setVisible(event.type() == QEvent.Type.ShowToParent)
        return False

    # --- search ---
    def focus_search(self) -> None:
        """Open the pane if it is compact and put the cursor in the search box."""
        if self.search_box is None:
            return
        if self._compact:
            self.set_compact(False)
        self.search_box.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_box.selectAll()

    def set_search_filters_items(self, filters: bool) -> None:
        """Whether typing in the search box hides the items that do not match (default: yes)."""
        self._search_filters = filters
        self._apply_filter()

    def _search_text_changed(self, text: str) -> None:
        self._apply_filter()
        self.search_changed.emit(text.strip())

    def _search_submitted(self, query: str) -> None:
        if self._search_filters and query:
            for key in self.keys():
                entry = self._entries[key]
                if entry.kind == ITEM and not entry.item.isHidden():
                    self.set_current(key)
                    break
        self.search_submitted.emit(query)

    def matches(self, key: str, query: str) -> bool:
        """Whether the item ``key`` matches a search for ``query`` (its title or keywords contain every word)."""
        entry = self._entry(key)
        haystack = " ".join((entry.title, entry.heading, *entry.keywords)).casefold()
        return all(word in haystack for word in query.casefold().split())

    def _apply_filter(self) -> None:
        query = self.search_box.text().strip() if self.search_box is not None and self._search_filters else ""
        for key, entry in self._entries.items():
            if entry.kind in (ITEM, ACTION):
                hidden = not entry.visible or (bool(query) and not entry.footer and not self.matches(key, query))
            else:
                hidden = bool(query)          # headers and separators make no sense in a filtered list
            entry.item.setHidden(hidden)
        self.list.viewport().update()

    # --- compact pane ---
    def is_compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool | None) -> None:
        """Show icons only (True), the full pane (False), or decide by the width of the view (None)."""
        self._forced_compact = compact
        self._sync_compact()

    def set_pane_width(self, width: int) -> None:
        self._pane_width = width
        if not self._compact:
            self.pane.setFixedWidth(width)

    def _sync_compact(self) -> None:
        if self._forced_compact is not None:
            compact = self._forced_compact
        else:
            compact = self.width() < self._compact_below
        if compact == self._compact:
            return
        self._compact = compact
        for view in (self.list, self.footer_list):
            view.set_compact(compact)
        if self.search_box is not None:
            self.search_box.setVisible(not compact)
            self.search_button.setVisible(compact)
        if self._pane_header_widget is not None:
            self._pane_header_widget.setVisible(not compact)
        # Two 40 px buttons do not fit side by side in the 48 px pane.
        self._buttons.setDirection(QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight)
        target = self.COMPACT_WIDTH if compact else self._pane_width
        if self._width_animation is not None:
            self._width_animation.stop()
            self._width_animation = None
        time = duration("fast")
        if time <= 0 or not self.isVisible():
            self.pane.setFixedWidth(target)
        else:
            animation = Animation(self)
            animation.setStartValue(self.pane.width())
            animation.setEndValue(target)
            animation.setDuration(time)
            animation.setEasingCurve(easing("decelerate"))
            animation.valueChanged.connect(lambda value: self.pane.setFixedWidth(int(value)))
            animation.start()
            self._width_animation = animation
        self._sync_footer()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_compact()

    def paintEvent(self, _event) -> None:
        on_glass = glass.enabled()
        if not self._content_layer and not on_glass:
            return
        painter = QPainter(self)
        if on_glass:
            # The pane is a sheet of glass on the backdrop, with a hairline at its inner edge.
            pane = QRectF(self.pane.geometry())
            paint.glass(painter, pane, "pane", outline=False)
            painter.fillRect(QRectF(pane.right(), pane.top(), 1, pane.height()), qcolor("stroke-surface"))
        if not self._content_layer:
            return
        area = QRectF(self.content.geometry()).adjusted(0, 0, 8, 8)       # only the top left corner is round
        painter.setClipRect(self.content.geometry())
        if on_glass:
            paint.glass(painter, area, "card", radius=current_theme().radius("flyout"))
        else:
            paint.rounded_rect(painter, area, current_theme().radius("flyout"), "layer", "stroke-surface")
