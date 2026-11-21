"""The bars around the canvas: the drawing tools with colours and sizes, and the zoom bar."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QSizePolicy, QWidget

import boswas_fluent as fluent
from boswas_fluent import paint

from .. import annotations

# (key, name, icon); the order of the bar
TOOLS = (
    ("pen", "Pen", "draw-freehand-symbolic"),
    ("highlighter", "Highlighter", "draw-highlight-symbolic"),
    ("eraser", "Eraser", "draw-eraser-symbolic"),
    ("arrow", "Arrow", "draw-arrow-symbolic"),
    ("rectangle", "Rectangle", "draw-rectangle-symbolic"),
    ("text", "Text", "draw-text-symbolic"),
    ("crop", "Crop", "transform-crop-symbolic"),
)
TOOL_HINTS = {
    "pen": "Draw freehand",
    "highlighter": "Mark text with a wide see-through stroke",
    "eraser": "Remove a whole mark by touching it",
    "arrow": "Drag to draw an arrow (Shift: in steps of 45 degrees)",
    "rectangle": "Drag to draw a rectangle (Shift: a square)",
    "text": "Click where the text goes, type, press Enter",
    "crop": "Crop the snip (Enter applies, Esc cancels)",
}
# Token colours for the marks: the same in both schemes, because they become part of the picture.
COLOURS = (
    ("coral.500", "Red"),
    ("sun.400", "Yellow"),
    ("mint.500", "Green"),
    ("azure.500", "Blue"),
    ("iris.500", "Violet"),
    ("orchid.500", "Pink"),
    ("black", "Black"),
    ("white", "White"),
)
DEFAULT_COLOUR = "coral.500"
WIDE_ENOUGH = 900                 # below this width the colours and sizes move into a flyout


class Swatch(QAbstractButton):
    """A round colour to pick."""

    def __init__(self, token: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.token = token
        self.setCheckable(True)
        self.setFixedSize(28, 28)
        self.setToolTip(name)
        self.setAccessibleName(name)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        centre = QPointF(self.width() / 2, self.height() / 2)
        if self.isChecked():
            painter.setPen(QPen(fluent.qcolor("accent"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, 12, 12)
        elif self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fluent.qcolor("fill-subtle-hover"))
            painter.drawEllipse(centre, 13, 13)
        painter.setPen(QPen(fluent.qcolor("stroke-strong"), 1))
        painter.setBrush(fluent.qcolor(self.token))
        painter.drawEllipse(centre, 8, 8)


class SizeButton(QAbstractButton):
    """One of the three sizes, drawn as a dot of that size."""

    DOTS = (2.5, 4.5, 7.0)

    def __init__(self, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.setCheckable(True)
        self.setFixedSize(28, 28)
        name = annotations.SIZES[index]
        self.setToolTip(name)
        self.setAccessibleName(name)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        centre = QPointF(self.width() / 2, self.height() / 2)
        if self.isChecked() or self.underMouse():
            paint.rounded_rect(painter, self.rect().adjusted(1, 1, -1, -1), fluent.theme().radius("control"),
                                      "fill-subtle-pressed" if self.isChecked() else "fill-subtle-hover",
                                      "accent" if self.isChecked() else None)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fluent.qcolor("text" if self.isEnabled() else "text-disabled"))
        painter.drawEllipse(centre, self.DOTS[self.index], self.DOTS[self.index])


class ColourButton(QAbstractButton):
    """The colour and size in use, as one button; opens the flyout with all colours and sizes."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.token = DEFAULT_COLOUR
        self.size_index = 1
        self.setFixedSize(QSize(44, 32))
        self.setToolTip("Colour and size")
        self.setAccessibleName("Colour and size")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def set_value(self, token: str, size_index: int) -> None:
        self.token, self.size_index = token, size_index
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = fluent.theme().radius("control")
        if self.isDown() or self.underMouse():
            paint.rounded_rect(painter, self.rect(), radius,
                                      "fill-subtle-pressed" if self.isDown() else "fill-subtle-hover")
        centre = QPointF(16, self.height() / 2)
        painter.setPen(QPen(fluent.qcolor("stroke-strong"), 1))
        painter.setBrush(fluent.qcolor(self.token))
        painter.drawEllipse(centre, 8, 8)
        paint.glyph(painter, self.rect().adjusted(28, 0, -2, 0), "chevron-down", "text-secondary", 10,
                           self.isEnabled())


class Palette(QWidget):
    """The colours and the sizes in a row; ``colour_chosen(token)``, ``size_chosen(index)``."""

    colour_chosen = Signal(str)
    size_chosen = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.swatches: dict[str, Swatch] = {}
        for token, name in COLOURS:
            swatch = Swatch(token, name, self)
            swatch.clicked.connect(lambda _checked=False, t=token: self.colour_chosen.emit(t))
            layout.addWidget(swatch)
            self.swatches[token] = swatch
        layout.addSpacing(6)
        layout.addWidget(fluent.Separator(Qt.Orientation.Vertical, self))
        layout.addSpacing(6)
        self.sizes = []
        for index in range(len(annotations.SIZES)):
            button = SizeButton(index, self)
            button.clicked.connect(lambda _checked=False, i=index: self.size_chosen.emit(i))
            layout.addWidget(button)
            self.sizes.append(button)

    def show_value(self, token: str, size_index: int) -> None:
        for key, swatch in self.swatches.items():
            swatch.setChecked(key == token)
        for button in self.sizes:
            button.setChecked(button.index == size_index)


class EditBar(QWidget):
    """Tools, colour and size, undo, redo and clearing; while cropping, Apply and Cancel.

    ``tool_chosen(key)`` with "" when the tool in use was clicked again
    (back to moving the picture); ``colour_chosen(token)``,
    ``size_chosen(index)``.
    """

    tool_chosen = Signal(str)
    colour_chosen = Signal(str)
    size_chosen = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Drawing tools")
        self.token = DEFAULT_COLOUR
        self.size_index = 1
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.tool_buttons: dict[str, fluent.IconButton] = {}
        for key, name, icon in TOOLS:
            button = fluent.IconButton(icon, name, self, size=36, icon_size=18, checkable=True)
            button.setToolTip(f"{name}: {TOOL_HINTS[key]}")
            button.setAccessibleName(name)
            button.setAccessibleDescription(TOOL_HINTS[key])
            button.clicked.connect(lambda checked, k=key: self.tool_chosen.emit(k if checked else ""))
            layout.addWidget(button)
            self.tool_buttons[key] = button
        layout.addSpacing(4)
        self.tools_separator = fluent.Separator(Qt.Orientation.Vertical, self)
        layout.addWidget(self.tools_separator)
        layout.addSpacing(4)
        self.palette = Palette(self)
        self.palette.colour_chosen.connect(self.colour_chosen)
        self.palette.size_chosen.connect(self.size_chosen)
        layout.addWidget(self.palette)
        self.colour_button = ColourButton(self)
        self.colour_button.clicked.connect(self._open_flyout)
        layout.addWidget(self.colour_button)
        self.flyout: fluent.Flyout | None = None
        self.flyout_palette: Palette | None = None
        layout.addSpacing(4)
        self.history_separator = fluent.Separator(Qt.Orientation.Vertical, self)
        layout.addWidget(self.history_separator)
        layout.addSpacing(4)
        self.undo_button = fluent.IconButton("edit-undo-symbolic", "Undo (Ctrl+Z)", self, size=36, icon_size=18)
        self.redo_button = fluent.IconButton("edit-redo-symbolic", "Redo (Ctrl+Y)", self, size=36, icon_size=18)
        self.clear_button = fluent.IconButton("edit-delete-symbolic", "Clear all marks (Delete)", self, size=36,
                                              icon_size=18)
        for button in (self.undo_button, self.redo_button, self.clear_button):
            layout.addWidget(button)
        self.undo_button.setAccessibleName("Undo")
        self.redo_button.setAccessibleName("Redo")
        self.clear_button.setAccessibleName("Clear all marks")
        layout.addStretch(1)
        self.crop_hint = fluent.Label("Drag the frame or its handles", "caption", self, tone="secondary")
        self.apply_crop_button = fluent.Button("Apply", self, accent=True)
        self.apply_crop_button.setToolTip("Crop to the frame (Enter)")
        self.cancel_crop_button = fluent.Button("Cancel", self)
        self.cancel_crop_button.setToolTip("Leave the picture as it is (Esc)")
        layout.addWidget(self.crop_hint)
        layout.addWidget(self.apply_crop_button)
        layout.addWidget(self.cancel_crop_button)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._cropping = False
        self.show_value(self.token, self.size_index)
        self._arrange()

    # --- state shown ---
    def show_tool(self, key: str) -> None:
        for name, button in self.tool_buttons.items():
            button.setChecked(name == key)
        self._cropping = key == "crop"
        self._arrange()

    def show_value(self, token: str, size_index: int) -> None:
        self.token, self.size_index = token, size_index
        self.palette.show_value(token, size_index)
        if self.flyout_palette is not None:
            self.flyout_palette.show_value(token, size_index)
        self.colour_button.set_value(token, size_index)

    def is_compact(self) -> bool:
        return self.width() < WIDE_ENOUGH

    def _arrange(self) -> None:
        compact = self.is_compact()
        drawing = not self._cropping
        self.palette.setVisible(drawing and not compact)
        self.colour_button.setVisible(drawing and compact)
        self.history_separator.setVisible(True)
        for widget in (self.apply_crop_button, self.cancel_crop_button):
            widget.setVisible(self._cropping)
        self.crop_hint.setVisible(self._cropping and not compact)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._arrange()

    def _open_flyout(self) -> None:
        if self.flyout is None:
            self.flyout = fluent.Flyout(self.window(), padding=12)
            self.flyout_palette = Palette(self.flyout)
            self.flyout_palette.colour_chosen.connect(self.colour_chosen)
            self.flyout_palette.size_chosen.connect(self.size_chosen)
            self.flyout.add_widget(self.flyout_palette)
        self.flyout_palette.show_value(self.token, self.size_index)
        self.flyout.show_at(self.colour_button)
        self.flyout_palette.swatches[self.token].setFocus(Qt.FocusReason.PopupFocusReason)


class ZoomBar(QWidget):
    """The picture's size and the zoom: out, the percentage (fit or actual size), in, fit, actual size."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.size_label = fluent.Label("", "caption", self, tone="secondary")
        self.size_label.setAccessibleName("Picture size")
        layout.addWidget(self.size_label)
        layout.addStretch(1)
        self.zoom_out_button = fluent.IconButton("zoom-out-symbolic", "Zoom out (Ctrl+minus)", self)
        self.percent_button = fluent.Button("100 %", self, subtle=True)
        self.percent_button.setToolTip("Switch between fitting the window and the actual size")
        self.percent_button.setAccessibleName("Zoom")
        self.zoom_in_button = fluent.IconButton("zoom-in-symbolic", "Zoom in (Ctrl+plus)", self)
        self.fit_button = fluent.IconButton("zoom-fit-best-symbolic", "Fit to window (Ctrl+0)", self)
        self.actual_button = fluent.IconButton("zoom-original-symbolic", "Actual size (Ctrl+1)", self)
        for widget in (self.zoom_out_button, self.percent_button, self.zoom_in_button, self.fit_button,
                       self.actual_button):
            layout.addWidget(widget)
        self.zoom_out_button.setAccessibleName("Zoom out")
        self.zoom_in_button.setAccessibleName("Zoom in")
        self.fit_button.setAccessibleName("Fit to window")
        self.actual_button.setAccessibleName("Actual size")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def show_zoom(self, percent: int, fitted: bool) -> None:
        self.percent_button.setText(f"{percent} %")
        self.percent_button.setAccessibleName(f"Zoom {percent} %" + (", fitted to the window" if fitted else ""))
        self.fit_button.setEnabled(not fitted)

    def show_size(self, width: int, height: int) -> None:
        self.size_label.setText(f"{width} × {height} px")


def colour_value(token: str) -> str:
    """The "#RRGGBB" of a token colour, for a mark."""
    return QColor(fluent.theme().color(token)).name().upper()
