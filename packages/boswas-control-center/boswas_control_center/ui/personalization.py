"""Personalization: the Boswas preset gallery and the appearance modules.

Presets come from `boswas-preset --json list`; "Apply" runs
`boswas-preset apply <id>`, with `--layout` only when the user ticked
"Also apply the panel layout" and confirmed that it replaces the panels.
Preview images are decoded on a worker thread.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QCheckBox, QGridLayout, QHBoxLayout, QPushButton, QSizePolicy, QWidget

from .. import viewmodel as vm
from .base import Page
from .common import SP, Badge, Card, MessageBanner, border_colour, icon, muted_label, title_label

PREVIEW_W, PREVIEW_H = 480, 270
PREVIEW_HEIGHT = 150
CARD_MIN_WIDTH = 250


def load_preview(path: str) -> QImage:
    """Decode and scale a preview image (runs on a worker thread; QImage is thread-safe)."""
    image = QImage(path)
    if image.isNull():
        raise ValueError(f"{path} is not a readable image")
    return image.scaled(PREVIEW_W, PREVIEW_H, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)


class Preview(QWidget):
    """A 16:9 preview with rounded corners; a neutral placeholder until the image is loaded."""

    def __init__(self, preset: vm.Preset, parent: QWidget | None = None):
        super().__init__(parent)
        self.preset = preset
        self.pixmap: QPixmap | None = None
        self.setFixedHeight(PREVIEW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(f"Preview of {preset.name}")

    def sizeHint(self) -> QSize:
        return QSize(270, PREVIEW_HEIGHT)

    def set_image(self, image: QImage) -> None:
        self.pixmap = QPixmap.fromImage(image)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 6, 6)
        palette = self.palette()
        if self.pixmap is not None and not self.pixmap.isNull():
            p.setClipPath(path)
            scaled = self.pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                        Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2, scaled)
            p.setClipping(False)
        else:
            # No image: a plain sketch in the variant's tone (the palette's accent hue, desaturated) with the
            # preset's accent colour.
            hue = max(0, palette.color(QPalette.ColorRole.Highlight).hslHue())
            tone = QColor.fromHsl(hue, 40, 38 if self.preset.variant == "dark" else 232)
            p.fillPath(path, tone)
            accent = QColor(self.preset.accent) if self.preset.accent else palette.color(QPalette.ColorRole.Highlight)
            bar = QRectF(rect.left() + 14, rect.bottom() - 24, rect.width() - 28, 10)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            p.drawRoundedRect(bar, 5, 5)
        p.setPen(QPen(border_colour(palette), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


class AccentSwatch(QWidget):
    def __init__(self, colour: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.colour = QColor(colour)
        self.setFixedSize(18, 18)
        self.setToolTip(f"Accent colour {colour}")
        self.setAccessibleName(f"Accent colour {colour}")

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(border_colour(self.palette()), 1))
        p.setBrush(self.colour)
        p.drawEllipse(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5))


class PresetCard(Card):
    apply_requested = Signal(object)

    def __init__(self, preset: vm.Preset, parent: QWidget | None = None):
        super().__init__(parent=parent, margins=12, spacing=8)
        self.preset = preset
        self.setMinimumWidth(CARD_MIN_WIDTH)
        self.setAccessibleName(preset.name + (" (current preset)" if preset.current else ""))
        self.setAccessibleDescription(preset.description)
        self.preview = Preview(preset, self)
        self.addWidget(self.preview)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self.name = title_label(preset.name, 1.1, self)
        name_row.addWidget(self.name, 1)
        if preset.accent:
            name_row.addWidget(AccentSwatch(preset.accent, self), 0, Qt.AlignmentFlag.AlignVCenter)
        self.addLayout(name_row)
        self.description = muted_label(preset.description or "No description.", parent=self)
        self.description.setMinimumHeight(self.description.fontMetrics().lineSpacing() * 2)
        self.description.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.addWidget(self.description, 1)
        footer = QHBoxLayout()
        footer.setSpacing(6)
        if preset.variant_label:
            variant_icon = icon("weather-clear-night" if preset.variant == "dark" else "weather-clear")
            footer.addWidget(Badge(preset.variant_label, None if variant_icon.isNull() else variant_icon, parent=self))
        self.current_badge = None
        if preset.current:
            check = icon("checkmark", "dialog-ok-apply", fallback=SP.SP_DialogApplyButton)
            self.current_badge = Badge("Current", check, strong=True, parent=self)
            footer.addWidget(self.current_badge)
        footer.addStretch(1)
        self.apply = QPushButton("Applied" if preset.current else "Apply", self)
        self.apply.setEnabled(not preset.current)
        self.apply.setAccessibleName(f"Apply {preset.name}")
        self.apply.setAccessibleDescription(f"Apply the {preset.name} preset to your desktop")
        self.apply.clicked.connect(lambda: self.apply_requested.emit(self.preset))
        footer.addWidget(self.apply)
        self.addLayout(footer)

    def set_busy(self, busy: bool) -> None:
        self.apply.setEnabled(not busy and not self.preset.current)


class PersonalizationPage(Page):
    data_keys = ("presets",)

    def build(self) -> None:
        self.banner = MessageBanner(parent=self.content)
        self.body.addWidget(self.banner)
        options = QHBoxLayout()
        self.layout_box = QCheckBox("Also apply the panel layout (replaces your panels)", self.content)
        self.layout_box.setChecked(False)
        self.layout_box.setAccessibleDescription("When ticked, applying a preset also replaces your panels and their "
                                                 "widgets with the preset's layout")
        options.addWidget(self.layout_box)
        options.addStretch(1)
        self.body.addLayout(options)
        self.status = muted_label("Loading presets…", parent=self.content)
        self.body.addWidget(self.status)
        self.gallery = QWidget(self.content)
        self.grid = QGridLayout(self.gallery)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(14)
        self.body.addWidget(self.gallery)
        self.cards: list[PresetCard] = []
        self.columns = 0
        self.busy = False
        self.images: dict[str, QImage] = {}
        self.signature: tuple = ()
        self.add_modules()

    # --- data ---
    def render(self) -> None:
        result = self.store.get("presets")
        if result is None:
            self.status.setText("Loading presets…")
            self.status.show()
            return
        if not result.ok:
            self.status.setText(result.error or "Presets are not available.")
            self.status.show()
            self._set_cards([])
            return
        presets: vm.PresetList = result.value
        self.status.setVisible(not presets.presets)
        self.status.setText("No presets are installed.")
        self._set_cards(list(presets.presets))

    def _set_cards(self, presets: list[vm.Preset]) -> None:
        signature = tuple(presets)
        if signature == self.signature:
            return
        self.signature = signature
        for card in self.cards:
            self.grid.removeWidget(card)
            card.deleteLater()
        self.cards = []
        for preset in presets:
            card = PresetCard(preset, self.gallery)
            card.apply_requested.connect(self.apply)
            card.set_busy(self.busy)
            self.cards.append(card)
            if preset.preview:
                if preset.preview in self.images:
                    card.preview.set_image(self.images[preset.preview])
                else:
                    self.window_.runner.submit(load_preview, preset.preview, key=f"preview:{preset.preview}",
                                               on_done=lambda image, path=preset.preview: self._preview_loaded(
                                                   path, image),
                                               on_error=lambda _exc: None)   # the placeholder stays
        self.columns = 0
        self._reflow()

    def _preview_loaded(self, path: str, image: QImage) -> None:
        self.images[path] = image
        for card in self.cards:
            if card.preset.preview == path:
                card.preview.set_image(image)

    # --- layout ---
    def _reflow(self) -> None:
        width = max(self.gallery.width(), self.content.width() - 64, CARD_MIN_WIDTH)
        columns = max(1, min(3, width // (CARD_MIN_WIDTH + 30)))
        if columns == self.columns:
            return
        self.columns = columns
        for card in self.cards:
            self.grid.removeWidget(card)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)
        for column in range(3):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    # --- apply ---
    def apply(self, preset: vm.Preset) -> None:
        layout = self.layout_box.isChecked()
        if layout and not self.window_.ask(
                "Replace your panels?",
                f"Applying “{preset.name}” with its panel layout replaces your panels and the widgets on them. "
                "Your current panel layout cannot be restored automatically.",
                "Replace Panels and Apply"):
            return
        self.set_busy(True)
        self.banner.show_message("info", f"Applying “{preset.name}”…")
        self.window_.runner.submit(self.window_.backend.apply_preset, preset.id, layout,
                                   on_done=lambda _r: self._applied(preset, layout),
                                   on_error=lambda exc: self._failed(preset, exc))

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for card in self.cards:
            card.set_busy(busy)
        self.layout_box.setEnabled(not busy)

    def _applied(self, preset: vm.Preset, layout: bool) -> None:
        self.set_busy(False)
        extra = " Your panels were replaced with the preset's layout." if layout else ""
        self.banner.show_message("ok", f"“{preset.name}” is now applied.{extra}")
        self.store.request("presets", force=True)

    def _failed(self, preset: vm.Preset, exc: Exception) -> None:
        self.set_busy(False)
        self.banner.show_message("error", f"“{preset.name}” could not be applied. {self.window_.error_text(exc)}")
        self.store.request("presets", force=True)
