"""Personalization > Colours: the accent colour and transparency effects.

Page "colours":
  Accent colour         one of the palette's hues (iris, the default, azure,
                        sky, mint, sun, coral, orchid, slate):
                        [appearance] accent. The shell, the compositor's own
                        surfaces and every Qt application (through the
                        toolkit) take it; each takes the same shades of the
                        hue as the default takes of iris, moved where needed
                        so that text on it stays readable.
  Transparency effects  (also on Accessibility > Visual effects) the
                        taskbar and the flyouts show the blurred background
                        through their tint, or the tint alone:
                        [appearance] transparency.

Both go to the user's compositor.toml (not appearance.toml, which the next
preset replaces); compositorconfig explains what goes where.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QButtonGroup, QHBoxLayout, QWidget

from ... import compositorconfig as cc
from .display import ConfigSection

NAMES = {"iris": "Iris", "azure": "Azure", "sky": "Sky", "mint": "Mint", "sun": "Sun", "coral": "Coral",
         "orchid": "Orchid", "slate": "Slate"}
SWATCH = 40


class Swatch(QAbstractButton):
    """A round button in the accent colour of one hue, in the scheme the window shows; checked: ringed."""

    def __init__(self, hue: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hue = hue
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        label = NAMES[hue] + (" (default)" if hue == cc.ACCENTS[0] else "")
        self.setAccessibleName(label)
        self.setToolTip(label)

    def sizeHint(self) -> QSize:
        return QSize(SWATCH + 8, SWATCH + 8)

    def colour(self) -> str:
        current = fluent.theme()
        return fluent.Theme(current.scheme, current.tokens, accent=self.hue).color("accent")

    def paintEvent(self, _event) -> None:
        theme = fluent.theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(4, 4, SWATCH, SWATCH)
        if self.isChecked():
            painter.setPen(QPen(fluent.qcolor("text"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(box.adjusted(-2, -2, 2, 2))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fluent.qcolor(self.colour()))
        inset = 4 if self.isChecked() else (1 if self.underMouse() else 2)
        painter.drawEllipse(box.adjusted(inset, inset, -inset, -inset))
        if self.isChecked():
            fluent.paint.glyph(painter, box.adjusted(10, 10, -10, -10).toRect(), "check", "on-accent", 16)
        if self.hasFocus() and self.focusPolicy() != Qt.FocusPolicy.NoFocus:
            painter.setPen(QPen(fluent.qcolor("focus"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), theme.radius("control"),
                                    theme.radius("control"))
        painter.end()


class AccentSection(ConfigSection):
    page = "colours"
    title = "Accent colour"
    order = 20
    keywords = ("accent", "accent colour", "accent color", "highlight", "colour", "color", "theme colour")
    reports_files = True

    def build(self) -> None:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.swatches: dict[str, Swatch] = {}
        for hue in cc.ACCENTS:
            swatch = Swatch(hue, holder)
            swatch.clicked.connect(lambda _on=False, hue=hue: self.change({("appearance", "accent"): hue}))
            self.group.addButton(swatch)
            self.swatches[hue] = swatch
            row.addWidget(swatch)
        row.addStretch(1)
        self.row = self.add(fluent.SettingsExpander(
            "Accent colour", "The colour of selections, switches, the focused window's details and the taskbar's "
            "marks", "preferences-desktop-color-symbolic", None, self, expanded=True))
        self.row.add_row("Choose a colour", "", holder)
        self.current = fluent.Label("", "caption", self, tone="secondary")
        self.row.header.add_control(self.current)

    def show_state(self, snapshot: cc.Snapshot) -> None:
        hue = snapshot.get("appearance", "accent")
        self.swatches[hue].setChecked(True)
        self.current.setText(NAMES[hue])
        for swatch in self.swatches.values():
            swatch.update()


class TransparencySection(ConfigSection):
    page = "colours"
    also = ("visual-effects",)
    title = "Transparency effects"
    order = 25
    keywords = ("transparency", "transparent", "blur", "acrylic", "mica", "see-through", "effects")

    def build(self) -> None:
        self.effects = self.toggle("Transparency effects", "The taskbar and the flyouts let the background show "
                                   "through", "preferences-desktop-effects-symbolic", "appearance", "transparency")

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.effects.setChecked(snapshot.get("appearance", "transparency"))


SECTIONS = (AccentSection, TransparencySection)
