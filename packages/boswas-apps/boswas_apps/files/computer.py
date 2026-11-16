"""This PC: the volumes of the computer as tiles with their free space."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QListView, QListWidget, QListWidgetItem, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

import boswas_fluent as fluent
from boswas_fluent import paint

from . import uiicons
from .text import display_name

VOLUME_ROLE = Qt.ItemDataRole.UserRole
_TILE = QSize(300, 92)


class _Delegate(QStyledItemDelegate):
    def sizeHint(self, option, index: QModelIndex) -> QSize:
        return _TILE

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        volume = index.data(VOLUME_ROLE)
        if volume is None:
            return
        rect = option.rect
        box = QRectF(rect).adjusted(4, 4, -4, -4)
        radius = fluent.theme().radius("control")
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        if selected:
            paint.rounded_rect(painter, box, radius, "fill-control-hover", "accent")
        elif hovered:
            paint.rounded_rect(painter, box, radius, "fill-subtle-hover")
        icon = uiicons.named(volume.icon, "drive-harddisk")
        if not volume.mounted:
            painter.setOpacity(0.6)
        icon.paint(painter, QRect(rect.left() + 16, rect.center().y() - 24, 48, 48))
        painter.setOpacity(1.0)
        x = rect.left() + 80
        width = rect.width() - 96
        name = display_name(volume.name)
        if volume.mount_point and volume.mount_point != "/" and volume.kind == "root":
            name = f"{name} ({volume.mount_point})"
        paint.elided(painter, QRectF(x, rect.top() + 14, width, 20), name, fluent.font("body"), "text")
        text_y = rect.top() + 38
        if volume.mounted and volume.total:
            bar = QRectF(x, rect.top() + 40, width, 8)
            paint.rounded_rect(painter, bar, 2, "fill-control-hover", "stroke-control")
            used = max(0.0, min(1.0, volume.used / volume.total))
            if used > 0:
                fill = "danger" if volume.free / volume.total < 0.1 else "accent"
                paint.rounded_rect(painter, QRectF(bar.left(), bar.top(), max(4.0, bar.width() * used), bar.height()),
                                   2, fill)
            text_y = rect.top() + 54
        paint.elided(painter, QRectF(x, text_y, width, 18), volume.space_text, fluent.font("caption"),
                     "text-secondary")
        painter.restore()


class VolumesPage(QWidget):
    """The page of the location ``computer:///``."""

    activated = Signal(object)                  # a Volume: open it, or mount / unlock it first
    context_requested = Signal(object, QPoint)

    def __init__(self, services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 12, 12, 0)
        column.setSpacing(8)
        self.heading = fluent.Label("Devices and drives", "body-strong", self)
        self.note = fluent.Label("The storage service (UDisks2) is not running: drives are shown as they are "
                                 "mounted and cannot be mounted or ejected from here.", "caption", self,
                                 tone="secondary", wrap=True)
        self.list = QListWidget(self)
        self.list.setItemDelegate(_Delegate(self.list))
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setFlow(QListView.Flow.LeftToRight)
        self.list.setWrapping(True)
        self.list.setUniformItemSizes(True)
        self.list.setGridSize(_TILE)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setMouseTracking(True)
        self.list.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.setAccessibleName("Devices and drives")
        column.addWidget(self.heading)
        column.addWidget(self.note)
        column.addWidget(self.list, 1)
        self.list.itemActivated.connect(self._activated)
        self.list.customContextMenuRequested.connect(self._context)
        services.volumes.changed.connect(self.rebuild)
        self.rebuild()

    def rebuild(self) -> None:
        current = self.current_volume()
        key = current.key if current is not None else None
        self.list.clear()
        monitor = self.services.volumes
        self.note.setVisible(monitor.loaded and not monitor.has_udisks)
        for volume in monitor.volumes:
            item = QListWidgetItem(display_name(volume.name))
            item.setData(VOLUME_ROLE, volume)
            item.setToolTip(f"{display_name(volume.name)}\n{volume.space_text}".strip())
            self.list.addItem(item)
            if volume.key == key:
                self.list.setCurrentItem(item)

    def volumes(self) -> list:
        return [self.list.item(row).data(VOLUME_ROLE) for row in range(self.list.count())]

    def current_volume(self):
        item = self.list.currentItem()
        return item.data(VOLUME_ROLE) if item is not None else None

    def _activated(self, item: QListWidgetItem) -> None:
        self.activated.emit(item.data(VOLUME_ROLE))

    def _context(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        if item is not None:
            self.list.setCurrentItem(item)
            self.context_requested.emit(item.data(VOLUME_ROLE), self.list.viewport().mapToGlobal(position))
