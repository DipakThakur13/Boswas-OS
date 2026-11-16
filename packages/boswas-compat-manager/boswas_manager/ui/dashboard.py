"""Dashboard page: application counts, runtime and device health, recent activity."""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import viewmodel as vm
from .common import dot_label, muted_label, plain_label, set_dot, status_dot

PAGE_MARGINS = (32, 0, 32, 24)


class _Count(fluent.Card):
    """A large number with a caption; clicking it opens the matching filter."""

    def __init__(self, caption: str, filter_key: str, parent: QWidget | None = None):
        super().__init__(parent, padding=12, spacing=0)
        self.filter_key = filter_key
        self.value = fluent.Button("–", self, subtle=True)
        self.value.setFont(fluent.font("title"))
        self.value.setMinimumHeight(48)
        self.value.setCursor(Qt.CursorShape.PointingHandCursor)
        self.value.setAccessibleName(caption)
        self.caption = muted_label(caption, wrap=False)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.add(self.value)
        self.add(self.caption)


class DashboardPage(QWidget):
    """Emits filter_requested(key) when a count is clicked."""

    filter_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(12)
        self.supported = muted_label(f"Supported: {vm.SUPPORTED_ARCHITECTURE}")
        layout.addWidget(self.supported)

        counts = QHBoxLayout()
        counts.setSpacing(8)
        self.counts = {"installed": _Count("Installed", "installed"), "running": _Count("Running", "running"),
                       "attention": _Count("Need attention", "repair"), "updates": _Count("Updates", "updates")}
        for key, widget in self.counts.items():
            target = "all" if key == "installed" else widget.filter_key
            widget.value.clicked.connect(lambda _checked=False, k=target: self.filter_requested.emit(k))
            counts.addWidget(widget)
        layout.addLayout(counts)

        self.status_values: dict[str, tuple] = {}
        health = QHBoxLayout()
        health.setSpacing(8)
        health.addWidget(self._status_card("Windows compatibility runtime", (
            ("wine", "Wine"), ("apparmor", "AppArmor"), ("bubblewrap", "Bubblewrap"))))
        health.addWidget(self._status_card("Device management", (
            ("agent", "Device Agent"), ("control_plane", "Control Plane"), ("policy", "Policy"))))
        layout.addLayout(health)

        activity = fluent.Card(self, title="Recent activity")
        self.activity = QTreeWidget(activity)
        self.activity.setColumnCount(2)
        self.activity.setHeaderLabels(["When", "Activity"])
        self.activity.setRootIsDecorated(False)
        self.activity.setUniformRowHeights(True)
        self.activity.setFrameShape(QFrame.Shape.NoFrame)
        self.activity.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.activity.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.activity.setColumnWidth(0, 160)
        self.no_activity = muted_label("No recent activity.")
        activity.add(self.activity, 1)
        activity.add(self.no_activity)
        layout.addWidget(activity, 1)
        self.set_activity([])

    def _status_card(self, title: str, rows: tuple[tuple[str, str], ...]) -> fluent.Card:
        card = fluent.Card(self, title=title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        for index, (key, label) in enumerate(rows):
            grid.addWidget(muted_label(label, wrap=False), index, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(self._status_field(key), index, 1)
        card.add_layout(grid)
        card.body.addStretch(1)
        return card

    def _status_field(self, key: str) -> QWidget:
        field = QWidget(self)
        row = QHBoxLayout(field)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
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
