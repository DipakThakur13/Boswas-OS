"""The dialogs of Photos: resize and print. Both are ContentDialogs inside the window."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QSlider, QVBoxLayout, QWidget

import boswas_fluent as fluent
from boswas_fluent import paint

from .. import imageio, printing
from ..fileutil import format_size

MAX_SIDE = 30000              # the largest side a resized picture may get
MAX_PIXELS = 100_000_000      # and the most pixels


class ResizeDialog(fluent.ContentDialog):
    """Asks for the new size of a picture, in pixels or percent, and how to save it.

    ``changed`` is emitted when a value that affects the file size changed;
    the window answers with ``set_estimate``.
    """

    changed = Signal()

    def __init__(self, parent: QWidget, size: QSize, file_size: int, fmt: str, quality: int) -> None:
        super().__init__(parent, "Resize picture", "", primary="Save resized copy", close="Cancel")
        self._original = QSize(size)
        self._ratio = size.width() / max(1, size.height())
        self._updating = False
        holder = QWidget(self)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.units = fluent.SegmentedControl(holder)
        self.units.add_item("pixels", "Pixels")
        self.units.add_item("percent", "Percent")
        self.units.setAccessibleName("Unit")
        layout.addWidget(self.units, 0, Qt.AlignmentFlag.AlignLeft)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        self.width_box = fluent.NumberBox(holder, value=size.width(), minimum=1, maximum=MAX_SIDE)
        self.height_box = fluent.NumberBox(holder, value=size.height(), minimum=1, maximum=MAX_SIDE)
        self.percent_box = fluent.NumberBox(holder, value=100, minimum=1, maximum=400, suffix=" %")
        self.width_box.setAccessibleName("Width in pixels")
        self.height_box.setAccessibleName("Height in pixels")
        self.percent_box.setAccessibleName("Size in percent")
        self.width_label = fluent.Label("Width", "body", holder)
        self.height_label = fluent.Label("Height", "body", holder)
        self.percent_label = fluent.Label("Size", "body", holder)
        form.addRow(self.width_label, self.width_box)
        form.addRow(self.height_label, self.height_box)
        form.addRow(self.percent_label, self.percent_box)
        self.keep = QCheckBox("Keep the shape of the picture", holder)
        self.keep.setChecked(True)
        form.addRow("", self.keep)
        self.format_box = QComboBox(holder)
        self.format_box.setAccessibleName("File type")
        for key in imageio.writable_formats():
            self.format_box.addItem(imageio.WRITERS[key][2], key)
        index = self.format_box.findData(fmt)
        self.format_box.setCurrentIndex(index if index >= 0 else max(0, self.format_box.findData("jpeg")))
        form.addRow(fluent.Label("File type", "body", holder), self.format_box)
        quality_row = QHBoxLayout()
        self.quality = QSlider(Qt.Orientation.Horizontal, holder)
        self.quality.setRange(10, 100)
        self.quality.setValue(max(10, min(100, quality)))
        self.quality.setAccessibleName("Quality")
        self.quality_value = fluent.Label("", "body", holder, tone="secondary")
        self.quality_value.setMinimumWidth(40)
        quality_row.addWidget(self.quality, 1)
        quality_row.addWidget(self.quality_value)
        self.quality_label = fluent.Label("Quality", "body", holder)
        self.quality_holder = QWidget(holder)
        self.quality_holder.setLayout(quality_row)
        quality_row.setContentsMargins(0, 0, 0, 0)
        form.addRow(self.quality_label, self.quality_holder)
        layout.addLayout(form)

        megabytes = f", {format_size(file_size)}" if file_size > 0 else ""
        self.now_label = fluent.Label(f"Now: {size.width()} × {size.height()} pixels{megabytes}", "caption", holder,
                                      tone="secondary")
        self.new_label = fluent.Label("", "body", holder, wrap=True)
        self.problem_label = fluent.Label("", "caption", holder, tone="danger", wrap=True)
        self.problem_label.hide()
        layout.addWidget(self.now_label)
        layout.addWidget(self.new_label)
        layout.addWidget(self.problem_label)
        self.set_content(holder)

        self._estimate: int | None = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.changed)
        self.units.current_changed.connect(self._units_changed)
        self.width_box.valueChanged.connect(lambda _v: self._side_changed(True))
        self.height_box.valueChanged.connect(lambda _v: self._side_changed(False))
        self.percent_box.valueChanged.connect(self._percent_changed)
        self.keep.toggled.connect(lambda checked: self._side_changed(True) if checked else None)
        self.format_box.currentIndexChanged.connect(self._other_changed)
        self.quality.valueChanged.connect(self._other_changed)
        self._units_changed("pixels")
        self._refresh()

    # --- values ---
    def new_size(self) -> QSize:
        return QSize(self.width_box.int_value(), self.height_box.int_value())

    def format(self) -> str:
        return self.format_box.currentData()

    def quality_value_(self) -> int:
        return self.quality.value()

    def has_quality(self) -> bool:
        return imageio.WRITERS[self.format()][3]

    def set_estimate(self, size: int | None, key: tuple | None = None) -> None:
        """The estimated size of the new file (None: not known); ignored when it answers older values."""
        if key is not None and key != self.estimate_key():
            return
        self._estimate = size
        self._refresh()

    def estimate_key(self) -> tuple:
        return (self.width_box.int_value(), self.height_box.int_value(), self.format(), self.quality.value())

    # --- behaviour ---
    def _units_changed(self, key: str) -> None:
        pixels = key == "pixels"
        for widget in (self.width_box, self.height_box, self.width_label, self.height_label, self.keep):
            widget.setVisible(pixels)
        for widget in (self.percent_box, self.percent_label):
            widget.setVisible(not pixels)
        if not pixels:
            self._updating = True
            self.percent_box.setValue(max(1, round(self.width_box.value() * 100 / self._original.width())))
            self._updating = False
            self._percent_changed(self.percent_box.value())
        if self.is_open():
            self._place()

    def _percent_changed(self, value: float) -> None:
        if self._updating:
            return
        self._updating = True
        self.width_box.setValue(max(1, round(self._original.width() * value / 100)))
        self.height_box.setValue(max(1, round(self._original.height() * value / 100)))
        self._updating = False
        self._other_changed()

    def _side_changed(self, width_edited: bool) -> None:
        if self._updating:
            return
        if self.keep.isChecked():
            self._updating = True
            if width_edited:
                self.height_box.setValue(max(1, round(self.width_box.value() / self._ratio)))
            else:
                self.width_box.setValue(max(1, round(self.height_box.value() * self._ratio)))
            self._updating = False
        self._other_changed()

    def _other_changed(self, *_args) -> None:
        self._estimate = None
        self._refresh()
        self._debounce.start()

    def _refresh(self) -> None:
        size = self.new_size()
        quality = self.has_quality()
        self.quality_label.setVisible(quality)
        self.quality_holder.setVisible(quality)
        self.quality_value.setText(str(self.quality.value()))
        text = f"New: {size.width()} × {size.height()} pixels"
        if self._estimate is not None:
            text += f", about {format_size(self._estimate)}"
        self.new_label.setText(text)
        too_many = size.width() * size.height() > MAX_PIXELS
        self.problem_label.setText("That is more pixels than Photos saves in one picture." if too_many else "")
        self.problem_label.setVisible(too_many)
        if self.primary_button is not None:
            self.primary_button.setEnabled(not too_many)
        if self.is_open():
            self._place()


class _PagePreview(QWidget):
    """A small page with the picture on it, as it will be printed."""

    def __init__(self, image: QImage, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = image if image.width() <= 512 else image.scaledToWidth(
            512, Qt.TransformationMode.SmoothTransformation)
        self._full = image.size()
        self.mode = printing.FIT
        self.landscape = False
        self.picture_dpi = printing.DEFAULT_PICTURE_DPI
        self.setFixedSize(200, 200)
        self.setAccessibleName("Page preview")

    def page_rect(self) -> QRectF:
        long_side, short_side = 180.0, 180.0 * 210 / 297          # the shape of an A4 sheet
        width, height = (long_side, short_side) if self.landscape else (short_side, long_side)
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        page = self.page_rect()
        paint.shadow(painter, page, 0, "tooltip")
        painter.fillRect(page, fluent.qcolor("white"))
        if self._image.isNull():
            return
        # the preview page stands for a sheet 297 mm long: scale the picture's resolution to it
        page_dpi = max(page.width(), page.height()) / (297 / 25.4)
        placed = printing.place(page.width(), page.height(), self._full.width(), self._full.height(), self.mode,
                                page_dpi=page_dpi, picture_dpi=self.picture_dpi)
        factor = self._image.width() / max(1, self._full.width())
        x, y, w, h = placed.source
        target = QRectF(*placed.target).translated(page.topLeft())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setClipRect(page)
        painter.drawImage(target, self._image, QRectF(x * factor, y * factor, w * factor, h * factor))
        painter.setClipping(False)
        paint.rounded_rect(painter, page, 0, None, "stroke-strong")


class PrintDialog(fluent.ContentDialog):
    """Printer, orientation, how the picture is put on the page, copies; a preview of the page."""

    def __init__(self, parent: QWidget, image: QImage, picture_dpi: float | None = None) -> None:
        super().__init__(parent, "Print", "", primary="Print", close="Cancel")
        holder = QWidget(self)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)
        self.preview = _PagePreview(image, holder)
        if picture_dpi:
            self.preview.picture_dpi = picture_dpi
        self.preview.landscape = image.width() > image.height()
        row.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignTop)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.printer_box = QComboBox(holder)
        self.printer_box.setAccessibleName("Printer")
        self.printer_box.addItem("Save as PDF", printing.PDF_PRINTER)
        self.printer_box.setMinimumWidth(180)
        self.orientation_box = QComboBox(holder)
        self.orientation_box.setAccessibleName("Orientation")
        for key, text in ((printing.AUTO, "Automatic"), (printing.PORTRAIT, "Portrait"),
                          (printing.LANDSCAPE, "Landscape")):
            self.orientation_box.addItem(text, key)
        self.mode_box = QComboBox(holder)
        self.mode_box.setAccessibleName("Size on the page")
        for key, text in ((printing.FIT, "Fit to page"), (printing.FILL, "Fill page"),
                          (printing.ACTUAL, "Actual size")):
            self.mode_box.addItem(text, key)
        self.copies_box = fluent.NumberBox(holder, value=1, minimum=1, maximum=99)
        self.copies_box.setAccessibleName("Copies")
        self.copies_label = fluent.Label("Copies", "body", holder)
        form.addRow(fluent.Label("Printer", "body", holder), self.printer_box)
        form.addRow(fluent.Label("Orientation", "body", holder), self.orientation_box)
        form.addRow(fluent.Label("Size", "body", holder), self.mode_box)
        form.addRow(self.copies_label, self.copies_box)
        self.note = fluent.Label("Looking for printers…", "caption", holder, tone="secondary", wrap=True)
        form.addRow("", self.note)
        row.addLayout(form, 1)
        self.set_content(holder)
        self._image_size = image.size()
        self.orientation_box.currentIndexChanged.connect(self._sync)
        self.mode_box.currentIndexChanged.connect(self._sync)
        self.printer_box.currentIndexChanged.connect(self._sync)
        self._sync()

    def set_printers(self, found: list[tuple[str, str]]) -> None:
        """The printers of this computer (the default first); "Save as PDF" stays as the last entry."""
        self.printer_box.blockSignals(True)
        self.printer_box.clear()
        for name, label in found:
            self.printer_box.addItem(label, name)
        self.printer_box.addItem("Save as PDF", printing.PDF_PRINTER)
        self.printer_box.setCurrentIndex(0)
        self.printer_box.blockSignals(False)
        self.note.setText("" if found else "No printer is set up; the picture can be saved as a PDF file.")
        self.note.setVisible(not found)
        self._sync()
        if self.is_open():
            self._place()

    def printer(self) -> str:
        return self.printer_box.currentData()

    def is_pdf(self) -> bool:
        return self.printer() == printing.PDF_PRINTER

    def mode(self) -> str:
        return self.mode_box.currentData()

    def orientation(self) -> str:
        return self.orientation_box.currentData()

    def copies(self) -> int:
        return self.copies_box.int_value()

    def _sync(self, *_args) -> None:
        self.preview.mode = self.mode()
        self.preview.landscape = printing.landscape_for(self._image_size.width(), self._image_size.height(),
                                                        self.orientation())
        self.preview.update()
        pdf = self.is_pdf()
        self.copies_box.setEnabled(not pdf)
        self.copies_label.setEnabled(not pdf)
        if self.primary_button is not None:
            self.primary_button.setText("Save as PDF" if pdf else "Print")
