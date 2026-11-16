"""The Stopwatch page: a large time, start and pause, laps with the fastest and slowest marked."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

import boswas_fluent as fluent

from ...reminders import textfmt
from .. import model
from .common import Context
from .widgets import Ticker, TimeDisplay

TICK_MS = 43            # the hundredths change faster than anyone reads; 23 updates a second look fluid


class StopwatchPage(QWidget):
    def __init__(self, context: Context, window) -> None:
        super().__init__()
        self.context = context
        self._window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 4, 32, 24)
        layout.setSpacing(16)
        self.display = TimeDisplay(self)
        layout.addWidget(self.display, 3)
        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.start_button = fluent.Button("Start", self, icon="media-playback-start-symbolic", accent=True)
        self.lap_button = fluent.Button("Lap", self, icon="view-pin-symbolic")
        self.reset_button = fluent.Button("Reset", self, icon="view-refresh-symbolic")
        self.start_button.setToolTip("Start or pause (Space)")
        self.lap_button.setToolTip("Record a lap (L)")
        self.reset_button.setToolTip("Reset (R)")
        for button in (self.start_button, self.lap_button, self.reset_button):
            button.setMinimumWidth(112)
        self.start_button.clicked.connect(self.toggle)
        self.lap_button.clicked.connect(self.lap)
        self.reset_button.clicked.connect(self.reset)
        buttons.addStretch(1)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.lap_button)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.laps = QTableWidget(0, 4, self)
        self.laps.setHorizontalHeaderLabels(["Lap", "Time", "Total", ""])
        self.laps.setAccessibleName("Laps")
        self.laps.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.laps.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.laps.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.laps.verticalHeader().hide()
        self.laps.setShowGrid(False)
        header = self.laps.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.laps.setMaximumWidth(720)
        layout.addWidget(self.laps, 4, Qt.AlignmentFlag.AlignHCenter)
        self.laps.setMinimumWidth(420)
        self.hint = fluent.Label("Laps you record appear here.", "body", self, tone="secondary")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.hint, 4)
        fluent.add_shortcut(self, "Space", self.toggle, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        fluent.add_shortcut(self, "L", self.lap, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        fluent.add_shortcut(self, "R", self.reset, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.ticker = Ticker(self, self._tick)
        context.store.changed.connect(self.refresh)
        self.refresh()

    def watch(self) -> dict:
        return self.context.store.doc["stopwatch"]

    def refresh(self) -> None:
        watch = self.watch()
        running = watch["state"] == "running"
        text = "Pause" if running else "Resume" if watch["state"] == "paused" else "Start"
        if self.start_button.text() != text:
            self.start_button.setText(text)
            self.start_button.setIcon(fluent.icon("media-playback-pause-symbolic" if running
                                                  else "media-playback-start-symbolic"))
        self.lap_button.setEnabled(running and len(watch["laps"]) < model.MAX_LAPS)
        self.reset_button.setEnabled(watch["state"] != "idle")
        laps = model.stopwatch_laps(watch)
        self._lap_count = len(laps)
        self.laps.setRowCount(len(laps))
        for row, lap in enumerate(reversed(laps)):             # the newest lap on top
            cells = (str(lap.number), textfmt.stopwatch(lap.split), textfmt.stopwatch(lap.total),
                     lap.mark.capitalize())
            for column, value in enumerate(cells):
                item = self.laps.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.laps.setItem(row, column, item)
                if item.text() != value:
                    item.setText(value)
                if column == 3:
                    item.setForeground(fluent.qcolor("success-text" if lap.mark == "fastest" else "danger-text"))
        self.laps.setVisible(bool(laps))
        self.hint.setVisible(not laps)
        self.ticker.kick()

    def _tick(self) -> int | None:
        watch = self.watch()
        text = textfmt.stopwatch(model.stopwatch_elapsed(watch, self.context.now()))
        self.display.set_text(text, dim_from=len(text) - 3)
        return TICK_MS if watch["state"] == "running" else None

    # --- changes ---
    def toggle(self) -> None:
        now = self.context.now()
        if self.watch()["state"] == "running":
            self.context.store.mutate(lambda doc: model.stopwatch_pause(doc["stopwatch"], now))
        else:
            self.context.store.mutate(lambda doc: model.stopwatch_start(doc["stopwatch"], now))

    def lap(self) -> None:
        if self.watch()["state"] != "running":
            return
        now = self.context.now()
        self.context.store.mutate(lambda doc: model.stopwatch_lap(doc["stopwatch"], now) and None)

    def reset(self) -> None:
        if self.watch()["state"] == "idle":
            return
        self.context.store.mutate(lambda doc: model.stopwatch_reset(doc["stopwatch"]))
