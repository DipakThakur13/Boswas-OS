"""The file info pane: name (rename in place), where the file is, what the picture is, EXIF."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLineEdit, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import fileinfo
from ..fileutil import check_new_name
from .common import action_icon


class _NameEdit(QLineEdit):
    """The name of the file as a text box; Escape puts the old name back."""

    cancelled = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class InfoPane(QWidget):
    """Shows ``fileinfo.Group``s. ``rename_requested(new name)`` when the user gave the file a new name."""

    rename_requested = Signal(str)
    close_requested = Signal()
    WIDTH = 320

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(self.WIDTH)
        self._name = ""
        self._map_url: str | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(20, 12, 8, 4)
        self.heading = fluent.Label("File info", "body-strong", self)
        head.addWidget(self.heading, 1)
        self.close_button = fluent.IconButton(fluent.glyph_icon("close"), "Close file info", self, icon_size=12)
        self.close_button.clicked.connect(self.close_requested)
        head.addWidget(self.close_button)
        outer.addLayout(head)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(20, 4, 16, 20)
        self.body_layout.setSpacing(4)

        # --- the name ---
        self.body_layout.addWidget(fluent.Label("Name", "caption", self.body, tone="secondary"))
        row = QHBoxLayout()
        row.setSpacing(4)
        self.name_label = fluent.Label("", "body", self.body, wrap=True, selectable=True)
        self.name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.name_edit = _NameEdit(self.body)
        self.name_edit.setAccessibleName("File name")
        self.name_edit.setMaxLength(255)
        self.name_edit.hide()
        self.name_edit.returnPressed.connect(self._commit)
        self.name_edit.cancelled.connect(self.cancel_rename)
        self.rename_button = fluent.IconButton(action_icon("document-edit", "edit-rename"), "Rename (F2)", self.body)
        self.rename_button.clicked.connect(self.start_rename)
        row.addWidget(self.name_label, 1)
        row.addWidget(self.name_edit, 1)
        row.addWidget(self.rename_button, 0, Qt.AlignmentFlag.AlignTop)
        self.body_layout.addLayout(row)
        self.name_error = fluent.Label("", "caption", self.body, tone="danger", wrap=True)
        self.name_error.hide()
        self.body_layout.addWidget(self.name_error)

        self.groups_holder = QWidget(self.body)
        self.groups_layout = QVBoxLayout(self.groups_holder)
        self.groups_layout.setContentsMargins(0, 0, 0, 0)
        self.groups_layout.setSpacing(4)
        self.body_layout.addWidget(self.groups_holder)
        self.map_button = fluent.HyperlinkButton("Show on a map", None, self.body)
        self.map_button.setToolTip("Opens openstreetmap.org in the web browser")
        self.map_button.hide()
        self.body_layout.addWidget(self.map_button)
        self.body_layout.addStretch(1)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll, 1)
        self.setAccessibleName("File info")

    # --- content ---
    def name(self) -> str:
        return self._name

    def set_file(self, path: str, groups: list[fileinfo.Group], *, renamable: bool = True,
                 coordinates: tuple[float, float] | None = None) -> None:
        self._name = os.path.basename(path)
        self.name_label.setText(self._name)
        self.cancel_rename()
        self.rename_button.setEnabled(renamable)
        while self.groups_layout.count():
            item = self.groups_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for group in groups:
            self.groups_layout.addSpacing(12)
            title = fluent.Label(group.title, "body-strong", self.groups_holder)
            self.groups_layout.addWidget(title)
            for label, value in group.rows:
                self.groups_layout.addSpacing(4)
                self.groups_layout.addWidget(fluent.Label(label, "caption", self.groups_holder, tone="secondary"))
                text = fluent.Label(value, "body", self.groups_holder, wrap=True, selectable=True)
                text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                text.setAccessibleName(f"{label}: {value}")
                self.groups_layout.addWidget(text)
        if coordinates is not None:
            latitude, longitude = coordinates
            self.map_button.set_url(f"https://www.openstreetmap.org/?mlat={latitude:.5f}&mlon={longitude:.5f}"
                                    f"#map=15/{latitude:.5f}/{longitude:.5f}")
            self.map_button.show()
        else:
            self.map_button.set_url(None)
            self.map_button.hide()

    def clear(self) -> None:
        self.set_file("", [], renamable=False)

    def rows(self) -> dict[str, str]:
        """Label -> value of everything shown (tests)."""
        found: dict[str, str] = {}
        labels = [self.groups_layout.itemAt(i).widget() for i in range(self.groups_layout.count())]
        labels = [widget for widget in labels if isinstance(widget, fluent.Label)]
        pending = None
        for widget in labels:
            if widget.property("tone") == "secondary":
                pending = widget.text()
            elif pending is not None:
                found[pending] = widget.text()
                pending = None
        return found

    # --- renaming ---
    def is_renaming(self) -> bool:
        return self.name_edit.isVisible()

    def start_rename(self) -> None:
        if not self._name or not self.rename_button.isEnabled():
            return
        self.name_error.hide()
        self.name_edit.setText(self._name)
        self.name_label.hide()
        self.rename_button.hide()
        self.name_edit.show()
        self.name_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        stem = os.path.splitext(self._name)[0]
        self.name_edit.setSelection(0, len(stem) if stem else len(self._name))

    def cancel_rename(self) -> None:
        self.name_edit.hide()
        self.name_error.hide()
        self.name_label.show()
        self.rename_button.show()

    def show_rename_error(self, text: str) -> None:
        self.name_error.setText(text)
        self.name_error.show()

    def _commit(self) -> None:
        name = self.name_edit.text()
        if name == self._name:
            self.cancel_rename()
            return
        problem = check_new_name(name)
        if problem is not None:
            self.show_rename_error(problem)
            return
        self.rename_requested.emit(name)

    def paintEvent(self, _event) -> None:
        from PySide6.QtGui import QPainter
        painter = QPainter(self)
        painter.fillRect(self.rect(), fluent.qcolor("layer"))
        painter.fillRect(0, 0, 1, self.height(), fluent.qcolor("stroke-divider"))
