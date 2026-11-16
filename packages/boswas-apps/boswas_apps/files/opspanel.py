"""The progress panel: the running file operations, in a card at the bottom right of the window."""

from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QVBoxLayout, QWidget

import boswas_fluent as fluent
from boswas_fluent import paint

from .jobs import Job, JobManager
from .text import display_name, format_size

_SHADOW = 16
_WIDTH = 400
_SHOW_AFTER_MS = 500


def describe(job: Job) -> str:
    """The second line of a job: the current file, items and bytes done, the speed."""
    if job.is_cancelling():
        return "Cancelling…"
    if job.waiting:
        return "Waiting for your answer…"
    progress = job.progress()
    if progress.preparing:
        return "Preparing…"
    parts = []
    if progress.items_total:
        parts.append(f"{min(progress.items_done, progress.items_total):,} of {progress.items_total:,} items")
    if progress.bytes_total:
        parts.append(f"{format_size(min(progress.bytes_done, progress.bytes_total))} of "
                     f"{format_size(progress.bytes_total)}")
    if job.speed > 0 and progress.bytes_total:
        parts.append(f"{format_size(int(job.speed))}/s")
    return ", ".join(parts)


class _JobRow(QWidget):
    def __init__(self, job: Job, parent: QWidget) -> None:
        super().__init__(parent)
        self.job = job
        self.title = fluent.Label(job.title, "body-strong", self)
        self.title.setMinimumWidth(10)
        self.current = fluent.Label("", "caption", self, tone="secondary")
        self.current.setMinimumWidth(10)
        self.detail = fluent.Label("", "caption", self, tone="secondary")
        self.detail.setMinimumWidth(10)
        self.bar = QProgressBar(self)
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 0)
        self.bar.setAccessibleName(job.title)
        self.cancel_button = fluent.IconButton(fluent.glyph_icon("close"), "Cancel", self, size=28, icon_size=12)
        self.cancel_button.clicked.connect(job.cancel)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.title, 1)
        top.addWidget(self.cancel_button)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addLayout(top)
        column.addWidget(self.bar)
        column.addWidget(self.current)
        column.addWidget(self.detail)
        job.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        job = self.job
        fraction = job.fraction()
        if fraction is None or job.is_cancelling():
            if self.bar.maximum() != 0:
                self.bar.setRange(0, 0)
        else:
            if self.bar.maximum() != 1000:
                self.bar.setRange(0, 1000)
            self.bar.setValue(round(fraction * 1000))
        progress = job.progress()
        name = display_name(os.path.basename(progress.current.rstrip("/"))) if progress.current else ""
        metrics = self.current.fontMetrics()
        self.current.setText(metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, _WIDTH - 40))
        self.detail.setText(describe(job))
        self.cancel_button.setEnabled(not job.is_cancelling())


class OperationsPanel(QWidget):
    """Shows the jobs of a ``JobManager`` over the content of its window while there are any."""

    def __init__(self, manager: JobManager, host: QWidget) -> None:
        super().__init__(host)
        self.manager = manager
        self._host = host
        self._rows: dict[Job, _JobRow] = {}
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(_SHADOW + 16, _SHADOW + 16, _SHADOW + 16, _SHADOW + 16)
        self._column.setSpacing(16)
        self.setAccessibleName("File operations")
        self.hide()
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(_SHOW_AFTER_MS)
        self._delay.timeout.connect(self._sync)
        manager.job_added.connect(self._added)
        manager.job_finished.connect(self._removed)
        host.installEventFilter(self)
        for job in manager.jobs:
            self._added(job)

    def rows(self) -> list[_JobRow]:
        return list(self._rows.values())

    def _added(self, job: Job) -> None:
        row = _JobRow(job, self)
        self._rows[job] = row
        self._column.addWidget(row)
        if self.isVisible():
            self._sync()
        elif not self._delay.isActive():
            self._delay.start()

    def _removed(self, job: Job) -> None:
        row = self._rows.pop(job, None)
        if row is not None:
            self._column.removeWidget(row)
            row.deleteLater()
        self._sync()

    def show_now(self) -> None:
        self._delay.stop()
        self._sync()

    def _sync(self) -> None:
        if not self._rows:
            self.hide()
            return
        self.place()
        self.show()
        self.raise_()

    def place(self) -> None:
        self.setFixedWidth(_WIDTH + 2 * _SHADOW)
        self.adjustSize()
        host = self._host
        self.move(host.width() - self.width() - 8 + _SHADOW, host.height() - self.height() - 36 + _SHADOW)

    def eventFilter(self, obj, event) -> bool:
        if obj is self._host and event.type() == QEvent.Type.Resize and self.isVisible():
            self.place()
        return False

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        box = QRectF(self.rect()).adjusted(_SHADOW, _SHADOW, -_SHADOW, -_SHADOW)
        radius = fluent.theme().radius("flyout")
        paint.shadow(painter, box, radius, "flyout")
        paint.rounded_rect(painter, box, radius, "flyout", "stroke-surface")
