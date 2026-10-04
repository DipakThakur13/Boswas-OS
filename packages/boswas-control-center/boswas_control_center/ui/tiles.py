"""Buttons that open a KDE settings module or a program of the catalog.

A tile shows the module's icon, title and a one-line description, and a
chevron telling that it opens a separate window. It takes keyboard focus
(Tab), opens with Space or Enter, and draws a visible focus ring. A module
that is not installed is disabled and says so; an optional one is hidden.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QAbstractButton, QGridLayout, QSizePolicy, QWidget

from ..catalog import Module
from .common import SP, Card, icon, mix, scaled_font

NOT_INSTALLED = "Not installed on this system"


class ModuleTile(QAbstractButton):
    activated_module = Signal(object)

    PAD, ICON = 10, 32

    def __init__(self, module: Module, parent: QWidget | None = None):
        super().__init__(parent)
        self.module = module
        self.available: bool | None = None
        self.qicon: QIcon = icon(module.icon, fallback=SP.SP_FileDialogDetailedView)
        self.title_font = scaled_font(self.font(), weight=QFont.Weight.DemiBold)
        self.setText(module.title)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(module.title)
        self.setAccessibleDescription(f"{module.description}. Opens in a separate window.")
        self.setToolTip(module.description)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.clicked.connect(lambda: self.activated_module.emit(self.module))
        if module.optional:
            self.hide()                       # until the module is known to be installed

    def set_available(self, available: bool) -> None:
        self.available = available
        if self.module.optional:
            self.setVisible(available)
        self.setEnabled(available)
        description = self.module.description if available else NOT_INSTALLED
        self.setToolTip(description)
        self.setAccessibleDescription(description + (". Opens in a separate window." if available else ""))
        self.update()

    def description(self) -> str:
        return self.module.description if self.available is not False else NOT_INSTALLED

    def sizeHint(self) -> QSize:
        fm_title, fm = QFontMetrics(self.title_font), QFontMetrics(self.font())
        text_h = fm_title.height() + 2 + fm.height()
        return QSize(260, max(self.ICON, text_h) + 2 * self.PAD + 4)

    def minimumSizeHint(self) -> QSize:
        return QSize(180, self.sizeHint().height())

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.animateClick()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        base, accent = palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Highlight)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        enabled = self.isEnabled()
        if enabled and (self.isDown() or self.underMouse()):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(mix(base, accent, 0.22 if self.isDown() else 0.12))
            p.drawRoundedRect(rect, 6, 6)
        if self.hasFocus():
            p.setPen(QPen(accent, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)
        group = QPalette.ColorGroup.Normal if enabled else QPalette.ColorGroup.Disabled
        text_colour = palette.color(group, QPalette.ColorRole.Text)
        muted = palette.color(group, QPalette.ColorRole.PlaceholderText) if enabled else text_colour
        x, y = self.PAD + 2, (self.height() - self.ICON) // 2
        mode = QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled
        self.qicon.paint(p, x, y, self.ICON, self.ICON, Qt.AlignmentFlag.AlignCenter, mode)
        chevron = 14
        text_x = x + self.ICON + 12
        text_w = self.width() - text_x - chevron - 2 * self.PAD
        fm_title, fm = QFontMetrics(self.title_font), QFontMetrics(self.font())
        block = fm_title.height() + 2 + fm.height()
        top = (self.height() - block) / 2
        p.setFont(self.title_font)
        p.setPen(text_colour)
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        p.drawText(QRectF(text_x, top, text_w, fm_title.height()), left,
                   fm_title.elidedText(self.module.title, Qt.TextElideMode.ElideRight, int(text_w)))
        p.setFont(self.font())
        p.setPen(muted)
        p.drawText(QRectF(text_x, top + fm_title.height() + 2, text_w, fm.height()), left,
                   fm.elidedText(self.description(), Qt.TextElideMode.ElideRight, int(text_w)))
        if enabled:
            cx, cy = self.width() - self.PAD - chevron / 2 - 2, self.height() / 2
            path = QPainterPath(QPointF(cx - 2.5, cy - 5))
            path.lineTo(QPointF(cx + 2.5, cy))
            path.lineTo(QPointF(cx - 2.5, cy + 5))
            p.setPen(QPen(muted, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)


class ModuleGroup(Card):
    """A card of tiles in two columns."""

    def __init__(self, title: str, modules: tuple[Module, ...], parent: QWidget | None = None, columns: int = 2):
        super().__init__(title, parent, margins=12, spacing=6)
        if self.title is not None:
            self.title.setContentsMargins(4, 2, 0, 0)
        self.columns = columns
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(2)
        self.tiles = [ModuleTile(module, self) for module in modules]
        for column in range(columns):
            self.grid.setColumnStretch(column, 1)
        self.addLayout(self.grid)
        self.relayout()

    def relayout(self) -> None:
        """Place the tiles that are not hidden, without gaps."""
        for tile in self.tiles:
            self.grid.removeWidget(tile)
        shown = [t for t in self.tiles if not t.isHidden()]
        if len(shown) == 1:
            self.grid.addWidget(shown[0], 0, 0, 1, self.columns)      # a single tile uses the whole row
        else:
            for index, tile in enumerate(shown):
                self.grid.addWidget(tile, index // self.columns, index % self.columns)
        self.setVisible(bool(shown) or not self.tiles)

    def set_availability(self, kcms: dict | None, programs: dict | None) -> None:
        for tile in self.tiles:
            known = kcms if tile.module.kind == "kcm" else programs
            if isinstance(known, dict) and tile.module.id in known:
                tile.set_available(bool(known[tile.module.id]))
        self.relayout()

    def tile(self, module_id: str) -> ModuleTile:
        return next(t for t in self.tiles if t.module.id == module_id)
