"""The Timers page: several timers at once, each a card with a ring; one can fill the page."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLineEdit, QStackedWidget, QVBoxLayout, QWidget

import boswas_fluent as fluent

from ...reminders import textfmt, timeutil
from .. import model
from .common import Context
from .widgets import CardGrid, RingProgress, Ticker, scroll_page, until_next


class TimerDialog(fluent.ContentDialog):
    def __init__(self, parent: QWidget, timer: dict | None) -> None:
        super().__init__(parent, "Edit timer" if timer else "Add a timer", primary="Save",
                         secondary="Delete" if timer else None, close="Cancel")
        seconds = timer["duration"] if timer else 300
        form = QWidget(self)
        layout = QFormLayout(form)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setVerticalSpacing(12)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.hours = fluent.NumberBox(form, value=seconds // 3600, minimum=0, maximum=99, suffix=" h")
        self.minutes = fluent.NumberBox(form, value=seconds % 3600 // 60, minimum=0, maximum=59, suffix=" min")
        self.seconds = fluent.NumberBox(form, value=seconds % 60, minimum=0, maximum=59, suffix=" s")
        for box, name in ((self.hours, "Hours"), (self.minutes, "Minutes"), (self.seconds, "Seconds")):
            box.setAccessibleName(name)
            box.valueChanged.connect(self._check)
            row.addWidget(box, 1)
        self.name = QLineEdit(form)
        self.name.setMaxLength(model.MAX_NAME)
        self.name.setPlaceholderText("Timer name")
        self.name.setAccessibleName("Timer name")
        self.name.setText(timer["name"] if timer else "")
        layout.addRow("Length", row)
        layout.addRow("Name", self.name)
        self.set_content(form)
        self._check()

    def duration(self) -> int:
        return self.hours.int_value() * 3600 + self.minutes.int_value() * 60 + self.seconds.int_value()

    def _check(self, *_args) -> None:
        self.primary_button.setEnabled(self.duration() > 0)


class TimerCard(fluent.Card):
    def __init__(self, page: "TimersPage", timer: dict, *, expanded: bool = False) -> None:
        super().__init__(padding=16, spacing=8)
        self.timer_id = timer["id"]
        self.page = page
        self.expanded = expanded
        top = QHBoxLayout()
        top.setSpacing(4)
        self.name_label = fluent.Label("", "subtitle" if expanded else "body-strong", self)
        self.edit_button = fluent.IconButton("document-edit-symbolic", "Edit timer", self)
        self.expand_button = fluent.IconButton("view-restore-symbolic" if expanded else "view-fullscreen-symbolic",
                                               "Back to all timers" if expanded else "Expand", self)
        self.delete_button = fluent.IconButton("edit-delete-symbolic", "Delete timer", self)
        self.edit_button.clicked.connect(lambda: page.edit_timer(self.timer_id))
        self.expand_button.clicked.connect(lambda: page.set_expanded(None if expanded else self.timer_id))
        self.delete_button.clicked.connect(lambda: page.delete_timer(self.timer_id))
        top.addWidget(self.name_label, 1)
        for button in (self.edit_button, self.expand_button, self.delete_button):
            top.addWidget(button)
        self.ring = RingProgress(self, minimum=170 if not expanded else 220)
        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.start_button = fluent.IconButton("media-playback-start-symbolic", "Start", self, size=44, icon_size=20)
        self.start_button.setProperty("accent", True)
        self.reset_button = fluent.IconButton("view-refresh-symbolic", "Reset", self, size=44, icon_size=20)
        self.start_button.clicked.connect(lambda: page.toggle(self.timer_id))
        self.reset_button.clicked.connect(lambda: page.reset(self.timer_id))
        buttons.addStretch(1)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        self.body.addLayout(top)
        self.body.addWidget(self.ring, 1)
        self.body.addLayout(buttons)

    def show_timer(self, timer: dict, context: Context) -> float:
        """Update the card; returns the seconds left when the timer runs, else -1."""
        now = context.now()
        phase = model.timer_phase(timer, now)
        left = model.timer_remaining(timer, now)
        name = timer["name"] or textfmt.duration_words(timer["duration"])
        self.name_label.setText(name)
        caption = ""
        if phase == "running":
            due = model.timer_due_wall(timer, now)
            caption = "Ends " + textfmt.time_of(timeutil.from_epoch(due, context.zone()), context.twelve_hour())
        elif phase == "paused":
            caption = "Paused"
        elif phase == "done":
            caption = "Done"
        fraction = left / timer["duration"] if phase != "idle" else 1.0
        self.ring.set_progress(fraction, textfmt.countdown(left, hours=timer["duration"] >= 3600), caption, phase)
        running = phase == "running"
        tip = "Pause" if running else "Restart" if phase == "done" else "Start"
        if self.start_button.toolTip() != tip:
            self.start_button.set_icon("media-playback-pause-symbolic" if running else "media-playback-start-symbolic")
            self.start_button.setToolTip(tip)
            self.start_button.setAccessibleName(f"{tip} {name}")
        self.reset_button.setEnabled(phase != "idle")
        self.setAccessibleName(f"Timer {name}")
        return left if running else -1.0


class TimersPage(QWidget):
    def __init__(self, context: Context, window) -> None:
        super().__init__()
        self.context = context
        self._window = window
        self.cards: dict[str, TimerCard] = {}
        self.expanded_id: str | None = None
        self.expanded_card: TimerCard | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        bar_holder = QHBoxLayout()
        bar_holder.setContentsMargins(32, 0, 32, 0)
        self.commands = fluent.CommandBar(self)
        self.commands.add_command("Add a timer", "list-add-symbolic", self.add_timer, shortcut="Ctrl+N")
        bar_holder.addWidget(self.commands)
        layout.addLayout(bar_holder)
        self.stack = QStackedWidget(self)
        self.grid = CardGrid(column_width=260)
        self.stack.addWidget(scroll_page(self.grid))
        self.empty = fluent.EmptyState("No timers", "Add a timer for the tea, the laundry or the next break.",
                                       "preferences-system-time-symbolic", "Add a timer")
        self.empty.action_triggered.connect(self.add_timer)
        self.stack.addWidget(self.empty)
        self.expanded_holder = QWidget()
        self.expanded_layout = QVBoxLayout(self.expanded_holder)
        self.expanded_layout.setContentsMargins(32, 4, 32, 24)
        self.stack.addWidget(self.expanded_holder)
        layout.addWidget(self.stack, 1)
        self.ticker = Ticker(self, self._tick)
        context.store.changed.connect(self.refresh)
        context.settings.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        timers = self.context.store.doc["timers"]
        ids = [timer["id"] for timer in timers]
        if ids != [card.timer_id for card in self.grid.widgets()]:
            cards = [self.cards.get(timer["id"]) or TimerCard(self, timer) for timer in timers]
            self.cards = {card.timer_id: card for card in cards}
            self.grid.set_widgets(cards)
        if self.expanded_id is not None and model.find(timers, self.expanded_id) is None:
            self.expanded_id = None
        if self.expanded_card is not None and self.expanded_card.timer_id != self.expanded_id:
            self.expanded_layout.removeWidget(self.expanded_card)
            self.expanded_card.deleteLater()
            self.expanded_card = None
        if self.expanded_id is not None and self.expanded_card is None:
            self.expanded_card = TimerCard(self, model.find(timers, self.expanded_id), expanded=True)
            self.expanded_layout.addWidget(self.expanded_card)
        self.stack.setCurrentIndex(2 if self.expanded_id is not None else 0 if timers else 1)
        self.ticker.kick()

    def _tick(self) -> int | None:
        soonest: float | None = None
        for timer in self.context.store.doc["timers"]:
            for card in (self.cards.get(timer["id"]),
                         self.expanded_card if self.expanded_id == timer["id"] else None):
                if card is None:
                    continue
                left = card.show_timer(timer, self.context)
                if left >= 0 and (soonest is None or left < soonest):
                    soonest = left
        if soonest is None:
            return None
        return until_next(soonest) if soonest > 0 else 250

    def set_expanded(self, timer_id: str | None) -> None:
        self.expanded_id = timer_id
        self.refresh()
        if timer_id is None:
            return
        self.expanded_card.start_button.setFocus()

    # --- changes ---
    def _change(self, timer_id: str, action) -> None:
        now = self.context.now()

        def change(doc: dict) -> None:
            timer = model.find(doc["timers"], timer_id)
            if timer is not None:
                action(timer, now)
        self.context.store.mutate(change)

    def toggle(self, timer_id: str) -> None:
        timer = model.find(self.context.store.doc["timers"], timer_id)
        if timer is None:
            return
        phase = model.timer_phase(timer, self.context.now())
        if phase == "running":
            self._change(timer_id, model.timer_pause)
        elif phase == "done":
            self._change(timer_id, model.timer_restart)
        else:
            self._change(timer_id, model.timer_start)

    def reset(self, timer_id: str) -> None:
        self._change(timer_id, lambda timer, _now: model.timer_reset(timer))

    def add_timer(self) -> None:
        dialog = TimerDialog(self._window, None)
        result = self._window.run_dialog(dialog)
        duration, name = dialog.duration(), dialog.name.text()
        dialog.deleteLater()
        if result != fluent.ContentDialog.Result.PRIMARY or duration <= 0:
            return
        if len(self.context.store.doc["timers"]) >= model.MAX_TIMERS:
            self._window.report("warning", f"There can be at most {model.MAX_TIMERS} timers.")
            return
        timer = model.make_timer(duration, name)
        self.context.store.mutate(lambda doc: doc["timers"].append(dict(timer))
                                  if model.find(doc["timers"], timer["id"]) is None else None)

    def edit_timer(self, timer_id: str) -> None:
        timer = model.find(self.context.store.doc["timers"], timer_id)
        if timer is None:
            return
        dialog = TimerDialog(self._window, timer)
        result = self._window.run_dialog(dialog)
        duration, name = dialog.duration(), dialog.name.text()
        dialog.deleteLater()
        if result == fluent.ContentDialog.Result.SECONDARY:
            self.delete_timer(timer_id)
        elif result == fluent.ContentDialog.Result.PRIMARY and duration > 0:
            self._change(timer_id, lambda stored, _now: model.timer_set(stored, duration, name))

    def delete_timer(self, timer_id: str) -> None:
        timer = model.find(self.context.store.doc["timers"], timer_id)
        if timer is None:
            return
        removed = dict(timer)
        position = self.context.store.doc["timers"].index(timer)
        self.context.store.mutate(lambda doc: doc.__setitem__(
            "timers", [entry for entry in doc["timers"] if entry["id"] != timer_id]))
        name = removed["name"] or textfmt.duration_words(removed["duration"])
        self._window.offer_undo(f"Timer “{name}” deleted.", lambda: self.context.store.mutate(
            lambda doc: doc["timers"].insert(min(position, len(doc["timers"])), dict(removed))
            if model.find(doc["timers"], timer_id) is None else None))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self.expanded_id is not None:
            self.set_expanded(None)
            return
        super().keyPressEvent(event)
