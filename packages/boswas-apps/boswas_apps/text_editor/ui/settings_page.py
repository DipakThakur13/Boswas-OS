"""The settings page of the editor (the gear at the right of the menu bar)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QFontComboBox, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import codec, fileio

FONT_SIZES = (8, 9, 10, 11, 12, 14, 16, 18, 20, 22, 24, 26, 28, 36, 48, 72)
THEME_LABELS = (("system", "Use system setting"), ("light", "Light"), ("dark", "Dark"))
TOGGLES = (
    ("word_wrap", "Word wrap", "Fit the text to the width of the window"),
    ("line_numbers", "Line numbers", "Show the number of each line beside the text"),
    ("status_bar", "Status bar", "Show position, size, zoom, line ending and encoding below the text"),
)
EDITING_TOGGLES = (
    ("auto_indent", "Automatic indentation", "A new line starts as far in as the line before it"),
    ("spaces_for_tabs", "Insert spaces for tabs", "The Tab key fills up to the next tab stop with spaces"),
    ("trim_trailing", "Remove trailing spaces when saving", "Spaces and tabs at the ends of lines are not saved"),
)


class SettingsPage(QWidget):
    """Every setting as a card. ``changed(key)`` after the user changed one; ``back_requested`` from the arrow."""

    changed = Signal(str)
    back_requested = Signal()

    def __init__(self, settings: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(8, 8, 32, 8)
        header.setSpacing(8)
        self.back_button = fluent.IconButton("go-previous-symbolic", "Back (Esc)", self, size=36)
        self.back_button.clicked.connect(self.back_requested)
        header.addWidget(self.back_button)
        header.addWidget(fluent.Label("Settings", "title", self), 1)
        outer.addLayout(header)
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(32, 4, 32, 32)
        layout.setSpacing(4)
        self.controls: dict[str, QWidget] = {}

        layout.addWidget(fluent.Label("Appearance", "body-strong", content))
        theme = QComboBox(content)
        for key, label in THEME_LABELS:
            theme.addItem(label, key)
        theme.currentIndexChanged.connect(lambda _index: self._set("theme", theme.currentData()))
        self.controls["theme"] = theme
        layout.addWidget(fluent.SettingsCard("App theme", "Light or dark, or as the desktop is set", None, theme))

        self.font_card = fluent.SettingsExpander("Font", "The typeface of the text", None, None, content,
                                                 expanded=True)
        family = QFontComboBox(content)
        family.setEditable(False)
        family.setMaximumWidth(260)
        family.currentFontChanged.connect(lambda font: self._set("font_family", font.family()))
        self.controls["font_family"] = family
        self.font_card.add_row("Family", "", family)
        size = QComboBox(content)
        for points in FONT_SIZES:
            size.addItem(str(points), points)
        size.currentIndexChanged.connect(lambda _index: self._set("font_size", size.currentData()))
        self.controls["font_size"] = size
        self.font_card.add_row("Size", "", size)
        for key, title in (("font_bold", "Bold"), ("font_italic", "Italic")):
            self.font_card.add_row(title, "", self._switch(key))
        layout.addWidget(self.font_card)
        for key, title, description in TOGGLES:
            layout.addWidget(fluent.SettingsCard(title, description, None, self._switch(key)))

        layout.addSpacing(12)
        layout.addWidget(fluent.Label("Editing", "body-strong", content))
        for key, title, description in EDITING_TOGGLES:
            layout.addWidget(fluent.SettingsCard(title, description, None, self._switch(key)))
        tab_width = fluent.NumberBox(content, value=8, minimum=1, maximum=16)
        tab_width.valueChanged.connect(lambda _value: self._set("tab_width", tab_width.int_value()))
        self.controls["tab_width"] = tab_width
        layout.addWidget(fluent.SettingsCard("Tab width", "How many spaces wide a tab stop is", None, tab_width))
        line_ending = QComboBox(content)
        for kind, label in codec.LINE_ENDING_LABELS.items():
            line_ending.addItem(label, kind)
        line_ending.currentIndexChanged.connect(
            lambda _index: self._set("default_line_ending", line_ending.currentData()))
        self.controls["default_line_ending"] = line_ending
        layout.addWidget(fluent.SettingsCard("Line ending of new files",
                                             "A file that is opened keeps the line ending it has", None,
                                             line_ending))

        layout.addSpacing(12)
        layout.addWidget(fluent.Label("When Text Editor starts", "body-strong", content))
        layout.addWidget(fluent.SettingsCard(
            "Continue the previous session", "Open the tabs of last time again, with the text that was not saved",
            None, self._switch("restore_session")))
        layout.addSpacing(12)
        note = fluent.Label(
            f"Files larger than {fileio.describe_size(fileio.LARGE_FILE)} are opened after a question; files "
            f"larger than {fileio.describe_size(fileio.MAX_FILE)} are not opened.", "caption", content,
            tone="secondary", wrap=True)
        layout.addWidget(note)
        layout.addStretch(1)
        area.setWidget(content)
        outer.addWidget(area, 1)
        self._loading = False
        self.refresh()

    def _switch(self, key: str) -> fluent.ToggleSwitch:
        switch = fluent.ToggleSwitch(parent=self)
        switch.toggled.connect(lambda checked, k=key: self._set(k, checked))
        self.controls[key] = switch
        return switch

    def _set(self, key: str, value) -> None:
        if self._loading or self.settings.get(key) == value:
            return
        self.settings[key] = value
        self.changed.emit(key)

    def refresh(self) -> None:
        """Show the settings as they are now (they can change from the menus too)."""
        self._loading = True
        settings = self.settings
        for key, control in self.controls.items():
            value = settings[key]
            if isinstance(control, fluent.ToggleSwitch):
                control.setChecked(bool(value))
            elif isinstance(control, QFontComboBox):
                from PySide6.QtGui import QFont, QFontDatabase
                control.setCurrentFont(QFont(value) if value else QFontDatabase.systemFont(
                    QFontDatabase.SystemFont.FixedFont))
            elif isinstance(control, QComboBox):
                index = control.findData(value)
                if index < 0 and key == "font_size":
                    control.addItem(str(value), value)
                    index = control.findData(value)
                control.setCurrentIndex(max(index, 0))
            elif isinstance(control, fluent.NumberBox):
                control.setValue(int(value))
        self._loading = False

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.back_requested.emit()
            return
        super().keyPressEvent(event)
