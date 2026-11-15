"""CommandBar: the commands of a page, with an overflow menu for what does not fit."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QFontMetrics, QIcon, QKeySequence
from PySide6.QtWidgets import (QHBoxLayout, QLayout, QMenu, QSizePolicy, QStyleOptionToolButton, QToolButton,
                               QWidget)

from .. import icons
from .basic import Separator, to_icon


class CommandButton(QToolButton):
    """A command of a CommandBar: icon and label with the Fluent gap of 8 px between them."""

    _GAP = " "       # Qt puts 4 px between icon and text; a space of the interface font adds about 4 more

    def __init__(self, action: QAction, labels: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDefaultAction(action)
        beside = labels and bool(action.text())
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if beside
                                else Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setIconSize(QSize(16, 16))
        self.setFixedHeight(32)
        self.setAccessibleName(action.text().replace("&", ""))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _spaced(self) -> bool:
        return (self.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon and not self.icon().isNull()
                and bool(self.text()))

    def initStyleOption(self, option: QStyleOptionToolButton) -> None:
        super().initStyleOption(option)
        if self._spaced():
            option.text = self._GAP + option.text

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        if self._spaced():
            hint.setWidth(hint.width() + QFontMetrics(self.font()).horizontalAdvance(self._GAP) + 4)
        return hint

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


class CommandBar(QWidget):
    """A row of commands (icon and label) for the current page or selection.

    Primary commands are buttons; when the bar is too narrow the last ones
    move into the "More" menu, which also holds the secondary commands.
    Every command is a QAction: disable it, hide it or change its text and
    the button and the menu entry follow. Keyboard: Tab to each button, the
    shortcuts given to the commands work anywhere in the window.
    """

    def __init__(self, parent: QWidget | None = None, *, labels: bool = True) -> None:
        super().__init__(parent)
        self._labels = labels
        self._primary: list[tuple[QAction | None, QWidget]] = []      # (None, separator widget) for separators
        self._secondary: list[QAction] = []
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        # The bar may be narrower than its commands: that is what the overflow is for.
        self._layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._trailing = QHBoxLayout()
        self._trailing.setSpacing(4)
        self.more_button = QToolButton(self)
        self.more_button.setIcon(icons.glyph_icon("more"))
        self.more_button.setToolTip("More")
        self.more_button.setAccessibleName("More commands")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_button.setProperty("noArrow", True)
        self.more_button.setFixedSize(32, 32)
        self.more_menu = QMenu(self.more_button)
        self.more_button.setMenu(self.more_menu)
        self.more_menu.aboutToShow.connect(self._fill_menu)
        self._layout.addWidget(self.more_button)
        self._layout.addStretch(1)
        self._layout.addLayout(self._trailing)
        self.more_button.hide()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Commands")

    def add_command(self, text: str, icon: QIcon | str | None = None, triggered: Callable[[], None] | None = None, *,
                    shortcut: QKeySequence | QKeySequence.StandardKey | str | None = None, tooltip: str = "",
                    primary: bool = True, checkable: bool = False) -> QAction:
        """Add a command and return its QAction.

        ``primary=False`` puts it in the "More" menu only. ``shortcut`` works
        while the window of the bar is active and is shown in the tool tip
        and the menu.
        """
        action = QAction(to_icon(icon), text, self)
        action.setCheckable(checkable)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            QWidget.addAction(self, action)     # so the shortcut is live while the button is in the overflow
        hint = tooltip or text.replace("&", "")
        if shortcut is not None:
            hint = f"{hint} ({action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)})"
        action.setToolTip(hint)
        if triggered is not None:
            action.triggered.connect(lambda _checked=False: triggered())
        return self.add_action(action, primary=primary)

    def add_action(self, action: QAction, *, primary: bool = True) -> QAction:  # type: ignore[override]
        """Add an existing QAction as a command; returns it."""
        if primary:
            button = CommandButton(action, self._labels, self)
            self._layout.insertWidget(self._layout.indexOf(self.more_button), button)
            self._primary.append((action, button))
            action.changed.connect(self._refit)
        else:
            self._secondary.append(action)
        self._refit()
        return action

    def add_separator(self) -> None:
        """A divider between groups of primary commands."""
        holder = QWidget(self)
        holder.setFixedSize(9, 32)
        row = QHBoxLayout(holder)
        row.setContentsMargins(4, 6, 4, 6)
        row.addWidget(Separator(Qt.Orientation.Vertical, holder))
        self._layout.insertWidget(self._layout.indexOf(self.more_button), holder)
        self._primary.append((None, holder))
        self._refit()

    def add_widget(self, widget: QWidget) -> QWidget:
        """Add a widget at the trailing edge (a search box, a view switch); it never overflows. Returns it."""
        widget.setParent(self)
        self._trailing.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        self._refit()
        return widget

    def button(self, action: QAction) -> QToolButton | None:
        """The button of a primary command."""
        for candidate, widget in self._primary:
            if candidate is action and isinstance(widget, QToolButton):
                return widget
        return None

    def commands(self) -> list[QAction]:
        """Every command, primary then secondary."""
        return [action for action, _widget in self._primary if action is not None] + list(self._secondary)

    actions_ = commands                       # earlier names, kept for callers
    add_action_command = add_action

    def overflowed(self) -> list[QAction]:
        """The primary commands that are in the "More" menu now because the bar is too narrow."""
        return [action for action, widget in self._primary
                if action is not None and action.isVisible() and widget.isHidden()]

    def _trailing_width(self) -> int:
        width = 0
        for index in range(self._trailing.count()):
            widget = self._trailing.itemAt(index).widget()
            if widget is not None and not widget.isHidden():
                width += widget.sizeHint().width() + self._trailing.spacing()
        return width

    def _refit(self) -> None:
        spacing = self._layout.spacing()
        more = self.more_button.width() + spacing
        available = self.width() - self._trailing_width()
        wanted = [(action, widget, widget.sizeHint().width() + spacing) for action, widget in self._primary
                  if action is None or action.isVisible()]
        for action, widget in self._primary:
            if action is not None and not action.isVisible():
                widget.hide()
        total = sum(width for _a, _w, width in wanted)
        need_more = bool(self._secondary)
        shown = len(wanted)
        if self.width() > 0 and total + (more if need_more else 0) > available:
            need_more = True
            used = more
            shown = 0
            for _action, _widget, width in wanted:
                if used + width > available:
                    break
                used += width
                shown += 1
        # A separator is not left dangling as the last visible thing before "More".
        while shown > 0 and wanted[shown - 1][0] is None and shown < len(wanted):
            shown -= 1
        for index, (_action, widget, _width) in enumerate(wanted):
            widget.setVisible(index < shown)
        self.more_button.setVisible(need_more)

    def _fill_menu(self) -> None:
        self.more_menu.clear()
        overflowed = self.overflowed()
        for action in overflowed:
            self.more_menu.addAction(action)
        if overflowed and self._secondary:
            self.more_menu.addSeparator()
        for action in self._secondary:
            self.more_menu.addAction(action)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refit()

    def sizeHint(self) -> QSize:
        spacing = self._layout.spacing()
        width = sum(widget.sizeHint().width() + spacing for action, widget in self._primary
                    if action is None or action.isVisible())
        if self._secondary:
            width += self.more_button.width() + spacing
        return QSize(width + self._trailing_width(), 32)

    def minimumSizeHint(self) -> QSize:
        return QSize(self.more_button.width() + self._trailing_width(), 32)
