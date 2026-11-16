"""System Status page: runtime, device agent, Control Plane, disk, architecture, version, policy."""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from .. import viewmodel as vm
from .common import dot_label, muted_label, plain_label, set_dot
from .dashboard import PAGE_MARGINS


class SystemPage(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(12)
        intro = muted_label("The state of the components that run Windows applications on this device. "
                            "This page is read-only.")
        layout.addWidget(intro)
        group = fluent.Card(self, title="Components")
        grid = QGridLayout()
        grid.setHorizontalSpacing(32)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        self.values: dict[str, tuple] = {}
        for index, (key, label) in enumerate(vm.SYSTEM_ROW_LABELS):
            field = QWidget(group)
            row = QHBoxLayout(field)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            dot = dot_label("neutral", field)
            value = plain_label("Checking…", wrap=True, selectable=True, parent=field)
            row.addWidget(dot)
            row.addWidget(value, 1)
            self.values[key] = (dot, value)
            grid.addWidget(muted_label(label, wrap=False), index, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(field, index, 1)
        group.add_layout(grid)
        layout.addWidget(group)
        layout.addStretch(1)

    def set_rows(self, rows: list[vm.StatusRow]) -> None:
        for row in rows:
            dot, value = self.values[row.key]
            set_dot(dot, row.level)
            value.setText(row.value)

    def value(self, key: str) -> str:
        return self.values[key][1].text()
