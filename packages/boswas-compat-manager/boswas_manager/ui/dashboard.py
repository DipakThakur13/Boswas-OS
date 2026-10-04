"""Dashboard page: application counts, runtime and device health, recent activity."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import viewmodel as vm
from .common import dot_label, heading, plain_label, set_dot, status_dot


class _Count(QWidget):
    """A large number with a caption; clicking it opens the matching filter."""

    def __init__(self, caption: str, filter_key: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.filter_key = filter_key
        layout = QVBoxLayout(self)
        self.value = QPushButton("–", self)
        self.value.setFlat(True)
        font = QFont(self.value.font())
        font.setPointSizeF(max(font.pointSizeF(), 9.0) * 2.4)
        font.setBold(True)
        self.value.setFont(font)
        self.value.setCursor(Qt.CursorShape.PointingHandCursor)
        self.caption = plain_label(caption)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)


class DashboardPage(QWidget):
    """Emits filter_requested(key) when a count is clicked."""

    filter_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Dashboard"))
        self.supported = plain_label(f"Supported: {vm.SUPPORTED_ARCHITECTURE}", wrap=True)
        layout.addWidget(self.supported)

        grid = QGridLayout()
        apps = QGroupBox("Applications", self)
        counts = QHBoxLayout(apps)
        self.counts = {"installed": _Count("Installed", "installed"), "running": _Count("Running", "running"),
                       "attention": _Count("Need attention", "repair"), "updates": _Count("Updates", "updates")}
        for key, widget in self.counts.items():
            target = "all" if key == "installed" else widget.filter_key
            widget.value.clicked.connect(lambda _checked=False, k=target: self.filter_requested.emit(k))
            counts.addWidget(widget)
        grid.addWidget(apps, 0, 0, 1, 2)

        self.status_values: dict[str, tuple] = {}
        runtime = QGroupBox("Windows compatibility runtime", self)
        runtime_form = QFormLayout(runtime)
        for key, label in (("wine", "Wine"), ("apparmor", "AppArmor"), ("bubblewrap", "Bubblewrap")):
            runtime_form.addRow(plain_label(label), self._status_field(key))
        grid.addWidget(runtime, 1, 0)

        device = QGroupBox("Device management", self)
        device_form = QFormLayout(device)
        for key, label in (("agent", "Device Agent"), ("control_plane", "Control Plane"), ("policy", "Policy")):
            device_form.addRow(plain_label(label), self._status_field(key))
        grid.addWidget(device, 1, 1)
        layout.addLayout(grid)

        activity = QGroupBox("Recent activity", self)
        activity_layout = QVBoxLayout(activity)
        self.activity = QTreeWidget(activity)
        self.activity.setColumnCount(2)
        self.activity.setHeaderLabels(["When", "Activity"])
        self.activity.setRootIsDecorated(False)
        self.activity.setUniformRowHeights(True)
        self.activity.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.activity.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.activity.setColumnWidth(0, 140)
        self.no_activity = plain_label("No recent activity.")
        activity_layout.addWidget(self.activity)
        activity_layout.addWidget(self.no_activity)
        layout.addWidget(activity, 1)
        self.set_activity([])

    def _status_field(self, key: str) -> QWidget:
        field = QWidget(self)
        row = QHBoxLayout(field)
        row.setContentsMargins(0, 0, 0, 0)
        dot = dot_label("neutral", field)
        value = plain_label("Checking…", wrap=True, selectable=True, parent=field)
        row.addWidget(dot)
        row.addWidget(value, 1)
        self.status_values[key] = (dot, value)
        return field

    def set_summary(self, summary: vm.DashboardSummary) -> None:
        for key in self.counts:
            self.counts[key].value.setText(str(getattr(summary, key)))

    def set_status_rows(self, rows: list[vm.StatusRow]) -> None:
        for row in rows:
            if row.key in self.status_values:
                dot, value = self.status_values[row.key]
                set_dot(dot, row.level)
                value.setText(row.value)

    def status_text(self, key: str) -> str:
        return self.status_values[key][1].text()

    def set_activity(self, items: list[vm.ActivityItem]) -> None:
        self.activity.clear()
        for item in items:
            entry = QTreeWidgetItem([item.when, item.text])
            if item.remote:
                entry.setIcon(1, status_dot("active"))
            self.activity.addTopLevelItem(entry)
        self.activity.setVisible(bool(items))
        self.no_activity.setVisible(not items)

    def activity_texts(self) -> list[str]:
        return [self.activity.topLevelItem(i).text(1) for i in range(self.activity.topLevelItemCount())]
