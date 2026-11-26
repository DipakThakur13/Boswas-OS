"""What a style sheet cannot do for standard widgets: popups with round corners, row heights, the focus ring.

``Polisher`` is installed once by ``fluent.apply``. It is an application
event filter that looks at three kinds of event only:

* **Polish** (a widget is about to be shown for the first time): menus,
  combo box popups and tool tips get a translucent window so the 8 px
  corners of the style sheet are really round; item views lay their rows
  out again, because Qt measures rows before the style sheet is known;
  tables get the Fluent row height and a header that does not turn bold;
  trees get ``TreeItemDelegate`` (one selection pill per row); spin boxes
  get the text inset of a text box.
* **KeyPress** of a navigation key and **MouseButtonPress**: they decide
  whether the focus ring is shown. As in WinUI, the ring follows keyboard
  focus only; a click never draws it.

While Boswas glass is on (``glass.py``), ``GlassPainter`` paints under the
style sheet: menus, combo box lists and tool tips are acrylic glass (the
backdrop of their window under the flyout tint, an inner glow; the style
sheet draws their outline with its white top edge on it), and a dialog that
is a window of its own (``QDialog``: message boxes, file dialogs) shows the
backdrop like a FluentWindow.

``FocusRing`` is the ring itself: one small transparent widget, moved
around the widget (or the row, or the tab) that has the keyboard focus.
It is 2 px in the ``focus`` token colour with a 1 px inner line in the
page colour, so it is visible on every surface of both schemes. Text
boxes show focus with their accent underline instead and get no ring.

A widget can say where its ring goes by defining
``fluent_focus_rect() -> (host_widget, QRect)``; setting the dynamic
property ``fluentFocusRing`` to False turns the ring off for it.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QAbstractSpinBox, QApplication, QComboBox,
                               QDateTimeEdit, QDialog, QLineEdit, QMenu, QPlainTextEdit, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QTabBar, QTableView, QTextEdit, QTreeView, QWidget)

from . import theming as _theme

RING_WIDTH = 2           # logical pixels of the ring; one more for the inner line
_OUTSET = RING_WIDTH + 1
_NAVIGATION_KEYS = frozenset({
    Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right,
    Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Space, Qt.Key.Key_F6,
})
_TEXT_INPUTS = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)
_TABLE_ROW_HEIGHT_UNITS = 9          # 36 px rows, as in a WinUI list
_SPIN_TEXT_INSET = 8                 # with the editor's own 2 px: the 10 px inset of a text box


class FocusRing(QWidget):
    """The keyboard focus visual. One instance per application, owned by the Polisher."""

    def __init__(self) -> None:
        super().__init__(None)
        self.setObjectName("fluentFocusRing")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.target: QWidget | None = None
        self._watched: list[QObject] = []
        self._sync = QTimer(self)
        self._sync.setSingleShot(True)
        self._sync.setInterval(0)
        self._sync.timeout.connect(self.place)
        self.hide()

    def attach(self, target: QWidget | None) -> None:
        """Show the ring around ``target`` (or hide it when the widget takes none)."""
        self.detach()
        if target is None or not wants_ring(target):
            return
        self.target = target
        self._watch(target)
        if isinstance(target, QAbstractScrollArea):
            self._watch(target.viewport())
        target.destroyed.connect(self._target_destroyed)
        self.place()

    def _watch(self, obj: QObject) -> None:
        obj.installEventFilter(self)
        self._watched.append(obj)

    def detach(self) -> None:
        for obj in self._watched:
            try:
                obj.removeEventFilter(self)
            except RuntimeError:              # already deleted
                pass
        self._watched.clear()
        if self.target is not None:
            try:
                self.target.destroyed.disconnect(self._target_destroyed)
            except (RuntimeError, TypeError):
                pass
        self.target = None
        self._sync.stop()
        self.hide()
        self.setParent(None)

    def _target_destroyed(self, _obj: QObject | None = None) -> None:
        self._watched.clear()
        self.target = None
        self.hide()
        self.setParent(None)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.Paint, QEvent.Type.Move, QEvent.Type.Resize):
            self._sync.start()
        elif kind == QEvent.Type.Hide and obj is self.target:
            self.hide()
        elif kind == QEvent.Type.Show and obj is self.target:
            self._sync.start()
        return False

    def place(self) -> None:
        """Put the ring where the target is now."""
        target = self.target
        if target is None:
            return
        try:
            host, rect = focus_rect(target)
        except RuntimeError:                  # the widget was deleted under us
            self._target_destroyed()
            return
        if host is None or not target.isVisible() or rect.isEmpty():
            self.hide()
            return
        geometry = rect.adjusted(-_OUTSET, -_OUTSET, _OUTSET, _OUTSET).intersected(host.rect())
        if self.parentWidget() is not host:
            self.setParent(host)
        if self.geometry() != geometry:
            self.setGeometry(geometry)
        if not self.isVisible():
            self.show()
            self.raise_()

    def paintEvent(self, _event) -> None:
        theme = _theme.current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = theme.radius("control") + 2
        half = RING_WIDTH / 2
        outer = QRectF(self.rect()).adjusted(half, half, -half, -half)
        painter.setPen(QPen(_theme.qcolor("focus", theme), RING_WIDTH))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(outer, radius, radius)
        inner = QRectF(self.rect()).adjusted(RING_WIDTH + 0.5, RING_WIDTH + 0.5, -RING_WIDTH - 0.5, -RING_WIDTH - 0.5)
        painter.setPen(QPen(_theme.qcolor("background", theme), 1))
        painter.drawRoundedRect(inner, max(radius - RING_WIDTH, 1), max(radius - RING_WIDTH, 1))


def wants_ring(widget: QWidget) -> bool:
    """Whether the keyboard focus of ``widget`` is shown with the ring."""
    if widget.isWindow() or widget.property("fluentFocusRing") is False:
        return False
    if isinstance(widget, _TEXT_INPUTS) or (isinstance(widget, QComboBox) and widget.isEditable()):
        return False
    if isinstance(widget, QAbstractScrollArea) and not isinstance(widget, QAbstractItemView):
        return False
    window = widget.window()
    if window is not None and window.windowType() in (Qt.WindowType.Popup, Qt.WindowType.ToolTip):
        return False
    return True


def focus_rect(widget: QWidget) -> tuple[QWidget | None, QRect]:
    """Where the ring of ``widget`` goes: the widget that hosts the ring and a rectangle in its coordinates."""
    custom = getattr(widget, "fluent_focus_rect", None)
    if callable(custom):
        return custom()
    if isinstance(widget, QAbstractItemView):
        index = widget.currentIndex()
        if index.isValid():
            if widget.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows:
                rect = QRect()
                model = widget.model()
                for column in range(model.columnCount(index.parent())):
                    rect = rect.united(widget.visualRect(index.siblingAtColumn(column)))
            else:
                rect = widget.visualRect(index)
            if not rect.isEmpty():
                return widget.viewport(), rect.adjusted(_OUTSET, _OUTSET, -_OUTSET, -_OUTSET)
        return widget.viewport(), widget.viewport().rect().adjusted(_OUTSET, _OUTSET, -_OUTSET, -_OUTSET)
    if isinstance(widget, QTabBar) and widget.currentIndex() >= 0:
        return widget, widget.tabRect(widget.currentIndex()).adjusted(_OUTSET, _OUTSET, -_OUTSET, -_OUTSET)
    return widget.parentWidget(), widget.geometry()


class GlassPainter(QObject):
    """Paints acrylic glass under popups and the backdrop under dialog windows, while glass is on.

    It filters the Paint event of the widgets ``polish_widget`` gave it and paints before the widget
    paints itself (the style sheet then draws the outline and the items on top).
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Paint and isinstance(obj, QWidget):
            from . import glass
            if glass.enabled():
                kind = obj.property("_fluentGlass")
                if kind == "popup":
                    paint_popup_glass(obj)
                elif kind == "window":
                    painter = QPainter(obj)
                    glass.paint_backdrop(painter, obj, event.rect())
        return False


def popup_frame(widget: QWidget) -> tuple[QRectF, float]:
    """The rounded outline the style sheet draws for a popup: (rectangle, radius)."""
    theme = _theme.current_theme()
    if widget.inherits("QTipLabel"):
        return QRectF(widget.rect()), float(theme.radius("tooltip"))
    if widget.inherits("QComboBoxPrivateContainer"):
        view = widget.findChild(QAbstractItemView)
        if view is not None:
            return QRectF(view.geometry()), float(theme.radius("flyout"))
    return QRectF(widget.rect()), float(theme.radius("flyout"))


def paint_popup_glass(widget: QWidget) -> None:
    """Acrylic glass inside the outline of a popup: its window's backdrop, the flyout tint, an inner glow."""
    from . import glass
    box, radius = popup_frame(widget)
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    painter.setClipPath(path)
    if not glass.paint_backdrop(painter, widget, box):
        painter.fillRect(box, _theme.qcolor("flyout"))
    painter.fillRect(box, _theme.qcolor("glass-flyout"))
    glow = QLinearGradient(box.topLeft(), box.topLeft() + QPointF(0, min(box.height(), 32.0)))
    colour = _theme.qcolor("glass-glow")
    glow.setColorAt(0, colour)
    colour.setAlpha(0)
    glow.setColorAt(1, colour)
    painter.fillRect(box, glow)
    painter.end()


class Polisher(QObject):
    """The application event filter described in the module text."""

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self._app = app
        self.keyboard = False             # the last input that moved focus came from the keyboard
        self.glass = GlassPainter(self)
        self.ring = FocusRing()
        self._deferred = QTimer(self)
        self._deferred.setSingleShot(True)
        self._deferred.setInterval(0)
        self._deferred.timeout.connect(self._show_ring)
        app.installEventFilter(self)
        app.focusChanged.connect(self._focus_changed)

    def remove(self) -> None:
        try:
            self._app.removeEventFilter(self)
            self._app.focusChanged.disconnect(self._focus_changed)
        except (RuntimeError, TypeError):
            pass
        self.ring.detach()
        self.ring.deleteLater()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Polish:
            if isinstance(obj, QWidget):
                polish_widget(obj)
                glass_kind = obj.property("_fluentGlass")
                if glass_kind and not obj.property("_fluentGlassFilter"):
                    obj.setProperty("_fluentGlassFilter", True)
                    obj.installEventFilter(self.glass)
        elif kind == QEvent.Type.KeyPress:
            if event.key() in _NAVIGATION_KEYS and not self.keyboard:
                self.keyboard = True
                self._deferred.start()
        elif kind == QEvent.Type.MouseButtonPress:
            if self.keyboard:
                self.keyboard = False
                self.ring.detach()
        return False

    def _focus_changed(self, _old: QWidget | None, new: QWidget | None) -> None:
        if self.keyboard and new is not None:
            self.ring.attach(new)
        else:
            self.ring.detach()

    def _show_ring(self) -> None:
        widget = QApplication.focusWidget()
        if self.keyboard and widget is not None and self.ring.target is not widget:
            self.ring.attach(widget)


def polish_widget(widget: QWidget) -> None:
    """The one-time adjustments of a standard widget (see the module text)."""
    if isinstance(widget, QMenu) or widget.inherits("QComboBoxPrivateContainer") or widget.inherits("QTipLabel"):
        _round_popup(widget)
        if widget.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) and widget.property("_fluentGlass") is None:
            widget.setProperty("_fluentGlass", "popup")
    elif isinstance(widget, QDialog) and widget.isWindow() and widget.property("_fluentGlass") is None:
        widget.setProperty("_fluentGlass", "window")
    elif isinstance(widget, QAbstractSpinBox):
        # The style sheet cannot inset the text of a spin box (see qss.py); the editor's text margin can.
        editor = widget.findChild(QLineEdit, "qt_spinbox_lineedit")
        popup = isinstance(widget, QDateTimeEdit) and widget.calendarPopup()
        if editor is not None and not popup and editor.textMargins().left() == 0:
            editor.setTextMargins(_SPIN_TEXT_INSET, 0, 0, 0)
    elif isinstance(widget, QAbstractItemView):
        widget.scheduleDelayedItemsLayout()
        if isinstance(widget, QTableView):
            rows = widget.verticalHeader()
            height = _theme.current_theme().spacing(_TABLE_ROW_HEIGHT_UNITS)
            if rows.defaultSectionSize() < height:
                rows.setDefaultSectionSize(height)
            # Qt draws the header of a selected column bold; in a Fluent table the header stays quiet.
            widget.horizontalHeader().setHighlightSections(False)
        elif isinstance(widget, QTreeView) and _is_plain(widget, ("QTreeView", "QTreeWidget")):
            delegate = widget.itemDelegate()
            if delegate is not None and delegate.metaObject().className() == "QStyledItemDelegate":
                widget.setItemDelegate(TreeItemDelegate(widget))


def _is_plain(widget: QWidget, classes: tuple[str, ...]) -> bool:
    """Whether the nearest Qt class of ``widget`` is one of ``classes`` (not a private subclass of Qt's own)."""
    meta = widget.metaObject()
    while meta is not None and not meta.className().startswith("Q"):
        meta = meta.superClass()
    return meta is not None and meta.className() in classes


class TreeItemDelegate(QStyledItemDelegate):
    """Paints the selection of a tree row so that the accent pill appears once, at the start of the row.

    Qt draws a selected tree row in two steps: the indentation area (which
    the style sheet gives the fill and the pill), then every cell. A style
    sheet cannot tell the two apart, so the cells would repeat the pill.
    This delegate fills the selected cells itself and lets the style draw
    text and icon as for an unselected row. Polisher installs it on every
    QTreeView that still has Qt's default delegate.
    """

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        cell = QStyleOptionViewItem(option)
        if cell.state & QStyle.StateFlag.State_Selected:
            theme = _theme.current_theme()
            radius = theme.radius("control")
            last = cell.viewItemPosition in (QStyleOptionViewItem.ViewItemPosition.End,
                                             QStyleOptionViewItem.ViewItemPosition.OnlyOne)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_theme.qcolor("fill-subtle-hover", theme))
            box = QRectF(cell.rect)
            if last:
                painter.setClipRect(box)
                painter.drawRoundedRect(box.adjusted(-radius, 0, 0, 0), radius, radius)
            else:
                painter.drawRect(box)
            painter.restore()
        cell.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver)
        super().paint(painter, cell, index)


def _round_popup(widget: QWidget) -> None:
    if widget.property("_fluentPopup"):
        return
    widget.setProperty("_fluentPopup", True)
    if widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created) and widget.windowHandle() is not None:
        return        # the native window exists already and has no alpha channel: keep it square
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    if not widget.inherits("QTipLabel"):
        widget.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        widget.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
