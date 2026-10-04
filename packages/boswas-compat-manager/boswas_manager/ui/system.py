"""System Status page: runtime, device agent, Control Plane, disk, architecture, version, policy."""

from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QGroupBox, QHBoxLayout, QVBoxLayout, QWidget

from .. import viewmodel as vm
from .common import dot_label, heading, plain_label, set_dot


class SystemPage(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(heading("System Status"))
        intro = plain_label("The state of the components that run Windows applications on this device. "
                            "This page is read-only.", wrap=True)
        layout.addWidget(intro)
        group = QGroupBox("Components", self)
        form = QFormLayout(group)
        self.values: dict[str, tuple] = {}
        for key, label in vm.SYSTEM_ROW_LABELS:
            field = QWidget(group)
            row = QHBoxLayout(field)
            row.setContentsMargins(0, 0, 0, 0)
            dot = dot_label("neutral", field)
            value = plain_label("Checking…", wrap=True, selectable=True, parent=field)
            row.addWidget(dot)
            row.addWidget(value, 1)
            self.values[key] = (dot, value)
            form.addRow(plain_label(label), field)
        layout.addWidget(group)
        layout.addStretch(1)

    def set_rows(self, rows: list[vm.StatusRow]) -> None:
        for row in rows:
            dot, value = self.values[row.key]
            set_dot(dot, row.level)
            value.setText(row.value)

    def value(self, key: str) -> str:
        return self.values[key][1].text()
