"""The navigation pane: Home and quick access, pinned folders, This PC with its volumes, the Trash."""

from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QMimeData, QModelIndex, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QDrag, QIcon, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate,
                               QToolTip, QWidget)

import boswas_fluent as fluent
from boswas_fluent import paint

from . import clipboard, dnd, places, uiicons
from .text import display_name

LOCATION_ROLE = Qt.ItemDataRole.UserRole
KIND_ROLE = Qt.ItemDataRole.UserRole + 1
VOLUME_ROLE = Qt.ItemDataRole.UserRole + 2
PLACE, PINNED, VOLUME, SEPARATOR = "place", "pinned", "volume", "separator"
PIN_FORMAT = "application/x-boswas-files-pin"
ROW = 36
_ICON_X, _TEXT_X, _INSET = 14, 44, 4
_EJECT = 28


class _Delegate(QStyledItemDelegate):
    def __init__(self, view: "Sidebar") -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), 9 if index.data(KIND_ROLE) == SEPARATOR else ROW)

    def eject_rect(self, rect: QRect) -> QRect:
        return QRect(rect.right() - _INSET - _EJECT, rect.center().y() - _EJECT // 2 + 1, _EJECT, _EJECT)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        kind = index.data(KIND_ROLE)
        rect = QRectF(option.rect)
        painter.save()
        if kind == SEPARATOR:
            painter.fillRect(QRectF(rect.left() + 12, rect.center().y(), rect.width() - 24, 1),
                             fluent.qcolor("stroke-divider"))
            painter.restore()
            return
        view = self._view
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        box = rect.adjusted(_INSET, 2, -_INSET, -2)
        radius = fluent.theme().radius("control")
        if index.row() == view.drop_row:
            paint.rounded_rect(painter, box, radius, "fill-control-hover", "accent")
        elif selected:
            paint.rounded_rect(painter, box, radius, "fill-subtle-hover" if not hovered else "fill-control-hover")
            paint.rounded_rect(painter, QRectF(box.left(), box.center().y() - 8, 3, 16), 1.5, "accent")
        elif hovered:
            paint.rounded_rect(painter, box, radius, "fill-subtle-hover")
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            icon.paint(painter, QRect(int(rect.left()) + _ICON_X, int(rect.center().y()) - 9, 20, 20))
        right = _INSET + 8
        volume = index.data(VOLUME_ROLE)
        if volume is not None and (volume.can_eject or (volume.can_unmount and volume.kind in ("removable", "optical"))):
            eject = QRectF(self.eject_rect(option.rect))
            if view.eject_hover == index.row():
                paint.rounded_rect(painter, eject, radius, "fill-subtle-hover")
            if not view.eject_icon.isNull():
                view.eject_icon.paint(painter, eject.toRect().adjusted(6, 6, -6, -6))
            else:
                paint.elided(painter, eject, "⏏", option.font, "text-secondary", Qt.AlignmentFlag.AlignCenter)
            right += _EJECT
        if kind == PINNED and not view.pin_icon.isNull() and hovered:
            view.pin_icon.paint(painter, QRect(int(rect.right()) - _INSET - 24, int(rect.center().y()) - 7, 14, 14))
            right += 20
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        paint.elided(painter, QRectF(rect.left() + _TEXT_X, rect.top(), rect.width() - _TEXT_X - right, rect.height()),
                     text, fluent.font("body"), "text")
        painter.restore()


class Sidebar(QListWidget):
    """The list of places. Emits what the user wants; the window decides what happens."""

    location_activated = Signal(str)
    open_in_new_tab = Signal(str)
    volume_activated = Signal(object)           # a volume that is not mounted (mount or unlock it)
    eject_requested = Signal(object)
    context_requested = Signal(object, QPoint)  # the QListWidgetItem, global position
    dropped = Signal(list, str, str)            # paths, destination location, "copy" or "move"

    def __init__(self, services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.drop_row = -1
        self.eject_hover = -1
        self.eject_icon = uiicons.symbolic("media-eject")
        self.pin_icon = uiicons.symbolic("view-pin", "pin", "window-pin")
        self._current = ""
        self._drag_device: int | None = None
        self._middle = -1
        self._press = QPoint()
        self.setItemDelegate(_Delegate(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(False)
        self.setAccessibleName("Navigation pane")
        self.setMinimumWidth(160)
        services.volumes.changed.connect(self.rebuild)
        services.settings_changed.connect(lambda key: self.rebuild() if key == "pinned" else None)
        self.rebuild()

    # --- content ---
    def _add(self, kind: str, label: str, location: str, icon: QIcon | None = None, volume=None,
             tooltip: str = "") -> QListWidgetItem:
        item = QListWidgetItem(label)
        item.setData(KIND_ROLE, kind)
        item.setData(LOCATION_ROLE, location)
        if volume is not None:
            item.setData(VOLUME_ROLE, volume)
        if icon is not None:
            item.setIcon(icon)
        item.setToolTip(tooltip or (display_name(location) if location.startswith("/") else label))
        if kind == SEPARATOR:
            item.setFlags(Qt.ItemFlag.NoItemFlags)
        else:
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
                          | Qt.ItemFlag.ItemIsDropEnabled)
        self.addItem(item)
        return item

    def rebuild(self) -> None:
        """Fill the list from the user's folders, the pinned folders and the volumes."""
        blocked = self.blockSignals(True)
        self.clear()
        home = places.home()
        self._add(PLACE, "Home", home, uiicons.named("user-home", "folder"))
        self._add(SEPARATOR, "", "")
        shown = {home}
        for label, path, icon in self.services.user_dirs:
            self._add(PLACE, display_name(label), path, uiicons.named(icon, "folder"))
            shown.add(path)
        for path in self.services.settings.pinned():
            if path in shown:
                continue
            shown.add(path)
            label = display_name(os.path.basename(path.rstrip("/")) or path)
            self._add(PINNED, label, path, uiicons.named("folder"))
        self._add(SEPARATOR, "", "")
        self._add(PLACE, "This PC", places.COMPUTER, uiicons.named("computer"))
        for volume in self.services.volumes.volumes:
            location = volume.mount_point or ""
            tooltip = display_name(volume.name) + (f"\n{volume.space_text}" if volume.space_text else "")
            self._add(VOLUME, display_name(volume.name), location, uiicons.named(volume.icon, "drive-harddisk"),
                      volume, tooltip)
        self._add(SEPARATOR, "", "")
        self._add(PLACE, "Trash", places.TRASH, uiicons.named("user-trash"), tooltip="Trash")
        self.blockSignals(blocked)
        self.set_current_location(self._current)

    def set_current_location(self, location: str) -> None:
        """Mark the place that is exactly ``location`` (none when the location is not in the list)."""
        self._current = location
        blocked = self.blockSignals(True)
        self.clearSelection()
        self.setCurrentRow(-1)
        for row in range(self.count()):
            item = self.item(row)
            if item.data(KIND_ROLE) != SEPARATOR and location and item.data(LOCATION_ROLE) == location:
                item.setSelected(True)
                self.setCurrentRow(row)
                break
        self.blockSignals(blocked)
        self.viewport().update()

    def item_for(self, location: str) -> QListWidgetItem | None:
        for row in range(self.count()):
            item = self.item(row)
            if item.data(KIND_ROLE) != SEPARATOR and item.data(LOCATION_ROLE) == location:
                return item
        return None

    def locations(self) -> list[str]:
        return [self.item(row).data(LOCATION_ROLE) for row in range(self.count())
                if self.item(row).data(KIND_ROLE) != SEPARATOR]

    # --- activation ---
    def activate_item(self, item: QListWidgetItem | None) -> None:
        if item is None or item.data(KIND_ROLE) == SEPARATOR:
            return
        location = item.data(LOCATION_ROLE)
        volume = item.data(VOLUME_ROLE)
        if location:
            self.location_activated.emit(location)
        elif volume is not None:
            self.volume_activated.emit(volume)

    def _eject_at(self, position: QPoint) -> QListWidgetItem | None:
        item = self.itemAt(position)
        if item is None:
            return None
        volume = item.data(VOLUME_ROLE)
        if volume is None or not (volume.can_eject or (volume.can_unmount and volume.kind in ("removable", "optical"))):
            return None
        rect = self.itemDelegate().eject_rect(self.visualItemRect(item))
        return item if rect.contains(position) else None

    def mousePressEvent(self, event) -> None:
        position = event.position().toPoint()
        self._press = position
        if event.button() == Qt.MouseButton.MiddleButton:
            self._middle = self.row(self.itemAt(position)) if self.itemAt(position) else -1
            return
        if event.button() == Qt.MouseButton.RightButton:
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        position = event.position().toPoint()
        item = self.itemAt(position)
        if event.button() == Qt.MouseButton.MiddleButton:
            if item is not None and self.row(item) == self._middle and item.data(LOCATION_ROLE):
                self.open_in_new_tab.emit(item.data(LOCATION_ROLE))
            self._middle = -1
            return
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton or item is None:
            return
        if (position - self._press).manhattanLength() > 8:
            return
        eject = self._eject_at(position)
        if eject is not None:
            self.eject_requested.emit(eject.data(VOLUME_ROLE))
            self.set_current_location(self._current)
            return
        self.activate_item(item)

    def mouseMoveEvent(self, event) -> None:
        eject = self._eject_at(event.position().toPoint())
        row = self.row(eject) if eject is not None else -1
        if row != self.eject_hover:
            self.eject_hover = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.eject_hover = -1
        self.viewport().update()
        super().leaveEvent(event)

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            eject = self._eject_at(event.pos())
            if eject is not None:
                QToolTip.showText(event.globalPos(), "Eject", self)
                return True
        return super().viewportEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activate_item(self.currentItem())
            return
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = -1 if event.key() == Qt.Key.Key_Up else 1
            row = self.currentRow() + step
            while 0 <= row < self.count() and self.item(row).data(KIND_ROLE) == SEPARATOR:
                row += step
            if 0 <= row < self.count():
                self.setCurrentRow(row)
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        item = self.itemAt(event.pos()) if event.reason() != event.Reason.Keyboard else self.currentItem()
        if item is not None and item.data(KIND_ROLE) != SEPARATOR:
            self.context_requested.emit(item, event.globalPos())

    # --- drag and drop ---
    def startDrag(self, _actions) -> None:
        item = self.currentItem()
        if item is None or item.data(KIND_ROLE) != PINNED:
            return                              # only pinned folders are moved around
        data = QMimeData()
        data.setData(PIN_FORMAT, item.data(LOCATION_ROLE).encode("utf-8", "surrogateescape"))
        drag = QDrag(self)
        drag.setMimeData(data)
        drag.setPixmap(item.icon().pixmap(32, 32))
        drag.exec(Qt.DropAction.MoveAction)
        self.drop_row = -1
        self.viewport().update()

    def _target(self, position: QPoint) -> tuple[str | None, int]:
        item = self.itemAt(position)
        if item is None or item.data(KIND_ROLE) == SEPARATOR:
            return None, -1
        location = item.data(LOCATION_ROLE)
        if not location or location == places.COMPUTER:
            return None, -1
        return location, self.row(item)

    def _drag(self, event) -> None:
        data = event.mimeData()
        position = event.position().toPoint()
        row = -1
        if data.hasFormat(PIN_FORMAT):
            item = self.itemAt(position)
            if item is not None and item.data(KIND_ROLE) == PINNED:
                row = self.row(item)
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept()
            else:
                event.ignore()
        elif clipboard.has_files(data):
            if event.type() == QEvent.Type.DragEnter:
                self._drag_device = dnd.source_device(data)
            location, row = self._target(position)
            action = dnd.choose_action(event.modifiers(), event.possibleActions(), self._drag_device,
                                       location) if location else Qt.DropAction.IgnoreAction
            if action == Qt.DropAction.IgnoreAction:
                row = -1
                event.ignore()
            else:
                event.setDropAction(action)
                event.accept()
        else:
            event.ignore()
        if row != self.drop_row:
            self.drop_row = row
            self.viewport().update()

    def dragEnterEvent(self, event) -> None:
        self._drag(event)

    def dragMoveEvent(self, event) -> None:
        self._drag(event)

    def dragLeaveEvent(self, event) -> None:
        self.drop_row = -1
        self.viewport().update()
        event.accept()

    def dropEvent(self, event) -> None:
        data = event.mimeData()
        position = event.position().toPoint()
        self.drop_row = -1
        self.viewport().update()
        if data.hasFormat(PIN_FORMAT):
            item = self.itemAt(position)
            moved = bytes(data.data(PIN_FORMAT)).decode("utf-8", "surrogateescape")
            pinned = self.services.settings.pinned()
            if item is None or item.data(KIND_ROLE) != PINNED or moved not in pinned:
                event.ignore()
                return
            target = item.data(LOCATION_ROLE)
            if target != moved and target in pinned:
                self.services.settings.move_pin(moved, pinned.index(target))
                self.services.settings_changed.emit("pinned")
            event.accept()
            return
        location, _row = self._target(position)
        paths, _cut = clipboard.read(data)
        if location is None or not paths:
            event.ignore()
            return
        action = dnd.choose_action(event.modifiers(), event.possibleActions(), dnd.source_device(data), location)
        if action == Qt.DropAction.IgnoreAction:
            event.ignore()
            return
        event.setDropAction(action)
        event.accept()
        self.dropped.emit(paths, location, "move" if action == Qt.DropAction.MoveAction else "copy")
