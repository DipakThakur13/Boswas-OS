"""The Focus sessions page: plan a session, run it, see the day's progress."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

import boswas_fluent as fluent

from ...reminders import textfmt
from .. import model
from .common import Context
from .widgets import RingProgress, Ticker, scroll_page, until_next

GOAL_CHOICES = (15, 30, 45, 60, 90, 120, 180, 240, 300, 360, 480)


class FocusPage(QWidget):
    def __init__(self, context: Context, window) -> None:
        super().__init__()
        self.context = context
        self._window = window
        content = QWidget()
        columns = QHBoxLayout(content)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(12)

        # --- the session ---
        self.session_card = fluent.Card(padding=24, spacing=12)
        self.stack = QStackedWidget(self.session_card)
        setup = QWidget()
        setup_layout = QVBoxLayout(setup)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        setup_layout.setSpacing(12)
        setup_layout.addWidget(fluent.Label("Get ready to focus", "subtitle"))
        setup_layout.addWidget(fluent.Label("Choose how long you want to work. Longer sessions are split into "
                                            "focus periods with short breaks; a notification tells you when each "
                                            "one ends, also while Clock is closed.", "body", tone="secondary",
                                            wrap=True))
        self.minutes = fluent.NumberBox(setup, value=context.settings.values.session_minutes,
                                        minimum=model.FOCUS_MIN_TOTAL, maximum=model.FOCUS_MAX_TOTAL, step=5,
                                        suffix=" min")
        self.minutes.setAccessibleName("Session length in minutes")
        self.minutes.setFixedWidth(160)
        self.plan_label = fluent.Label("", "body", setup, tone="secondary")
        self.skip = QCheckBox("Skip breaks", setup)
        self.skip.setChecked(context.settings.values.skip_breaks)
        self.start_button = fluent.Button("Start focus session", setup, icon=fluent.icon(
            "media-playback-start-symbolic", color="on-accent"), accent=True)
        self.start_button.clicked.connect(self.start)
        self.minutes.valueChanged.connect(self._plan_changed)
        self.skip.toggled.connect(self._plan_changed)
        setup_layout.addWidget(self.minutes, 0, Qt.AlignmentFlag.AlignHCenter)
        setup_layout.addWidget(self.plan_label, 0, Qt.AlignmentFlag.AlignHCenter)
        setup_layout.addWidget(self.skip, 0, Qt.AlignmentFlag.AlignHCenter)
        setup_layout.addWidget(self.start_button, 0, Qt.AlignmentFlag.AlignHCenter)
        setup_layout.addStretch(1)
        running = QWidget()
        running_layout = QVBoxLayout(running)
        running_layout.setContentsMargins(0, 0, 0, 0)
        running_layout.setSpacing(12)
        self.phase_label = fluent.Label("", "subtitle", running)
        self.phase_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.ring = RingProgress(running, minimum=220)
        self.next_label = fluent.Label("", "body", running, tone="secondary")
        self.next_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.pause_button = fluent.Button("Pause", running, icon="media-playback-pause-symbolic")
        self.stop_button = fluent.Button("End session", running, icon="media-playback-stop-symbolic")
        self.pause_button.clicked.connect(self.toggle_pause)
        self.stop_button.clicked.connect(self.stop)
        buttons.addStretch(1)
        buttons.addWidget(self.pause_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        running_layout.addWidget(self.phase_label)
        running_layout.addWidget(self.ring, 1)
        running_layout.addWidget(self.next_label)
        running_layout.addLayout(buttons)
        self.stack.addWidget(setup)
        self.stack.addWidget(running)
        self.session_card.add(self.stack, 1)
        columns.addWidget(self.session_card, 3)

        # --- the day ---
        self.progress_card = fluent.Card(title="Daily progress", padding=24, spacing=12)
        self.day_ring = RingProgress(self.progress_card, minimum=150)
        figures = QHBoxLayout()
        self.yesterday_label = fluent.Label("", "body", self.progress_card)
        self.streak_label = fluent.Label("", "body", self.progress_card)
        self.yesterday_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.streak_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        figures.addWidget(self.yesterday_label, 1)
        figures.addWidget(self.streak_label, 1)
        goal_row = QHBoxLayout()
        goal_row.addWidget(fluent.Label("Daily goal", "body", self.progress_card), 1)
        self.goal = QComboBox(self.progress_card)
        self.goal.setAccessibleName("Daily goal")
        self.goal.activated.connect(self._goal_chosen)
        goal_row.addWidget(self.goal)
        self.progress_card.add(self.day_ring, 1)
        self.progress_card.add_layout(figures)
        self.progress_card.add_layout(goal_row)
        columns.addWidget(self.progress_card, 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll_page(content))
        self.ticker = Ticker(self, self._tick)
        context.store.changed.connect(self.refresh)
        context.settings.changed.connect(self._plan_changed)
        self._plan_changed()
        self.refresh()

    def focus(self) -> dict:
        return self.context.store.doc["focus"]

    def plan(self) -> list[dict]:
        values = self.context.settings.values
        return model.focus_plan(self.minutes.int_value(), values.focus_minutes, values.break_minutes,
                                self.skip.isChecked())

    def _plan_changed(self, *_args) -> None:
        breaks = model.plan_breaks(self.plan())
        self.plan_label.setText("You'll have no breaks." if not breaks
                                else f"You'll have {textfmt.plural(breaks, 'break')}.")

    def refresh(self) -> None:
        focus = self.focus()
        goals = sorted({*GOAL_CHOICES, focus["goal"]})
        if [self.goal.itemData(index) for index in range(self.goal.count())] != goals:
            self.goal.clear()
            for minutes in goals:
                self.goal.addItem(textfmt.duration_words(minutes * 60), minutes)
        self.goal.setCurrentIndex(self.goal.findData(focus["goal"]))
        self.stack.setCurrentIndex(0 if focus["state"] == "idle" else 1)
        self.ticker.kick()

    def _tick(self) -> int | None:
        now = self.context.now()
        focus = self.focus()
        status = model.focus_status(focus, now)
        delay = None
        if status.state == "done" or (status.state != "idle" and status.focused - focus["credited"] >= 60
                                      and status.kind == "break"):
            # A focus period is over: count it for the day (and end the session when the plan is through).
            zone = self.context.zone()
            self.context.store.mutate(lambda doc: model.focus_settle(doc["focus"], now, zone) and None)
            return 1000
        if status.state != "idle":
            if status.kind == "focus":
                self.phase_label.setText(f"Focus period {status.period} of {status.periods}")
            else:
                self.phase_label.setText("Break")
            caption = "Paused" if status.state == "paused" else ""
            fraction = status.segment_left / status.segment_length if status.segment_length else 0.0
            self.ring.set_progress(fraction, textfmt.countdown(status.segment_left), caption,
                                   "paused" if status.state == "paused" else "running")
            self.next_label.setText(f"{textfmt.duration_words(-(-status.total_left // 60) * 60)} left in this "
                                    "session")
            paused = status.state == "paused"
            if self.pause_button.text() != ("Resume" if paused else "Pause"):
                self.pause_button.setText("Resume" if paused else "Pause")
                self.pause_button.setIcon(fluent.icon("media-playback-start-symbolic" if paused
                                                      else "media-playback-pause-symbolic"))
            if status.state == "running":
                delay = until_next(status.segment_left)
        stats = model.focus_stats(focus, now, self.context.zone())
        done = int(stats.today // 60)
        self.day_ring.set_progress(min(1.0, stats.today / (stats.goal * 60)), textfmt.duration_words(done * 60, short=True),
                                   f"of {textfmt.duration_words(stats.goal * 60, short=True)}",
                                   "done" if stats.reached else "running")
        self.yesterday_label.setText(f"Yesterday\n{textfmt.duration_words(int(stats.yesterday // 60) * 60, short=True)}")
        self.streak_label.setText(f"Streak\n{textfmt.plural(stats.streak, 'day')}")
        return delay

    # --- changes ---
    def start(self) -> None:
        plan = self.plan()
        now = self.context.now()
        self.context.settings.set("session_minutes", self.minutes.int_value())
        self.context.settings.set("skip_breaks", self.skip.isChecked())
        self.context.store.mutate(lambda doc: model.focus_begin(doc["focus"], plan, now)
                                  if doc["focus"]["state"] == "idle" else None)
        self.pause_button.setFocus()

    def toggle_pause(self) -> None:
        now = self.context.now()
        if self.focus()["state"] == "running":
            self.context.store.mutate(lambda doc: model.focus_pause(doc["focus"], now))
        else:
            self.context.store.mutate(lambda doc: model.focus_resume(doc["focus"], now))

    def stop(self) -> None:
        now = self.context.now()
        zone = self.context.zone()
        self.context.store.mutate(lambda doc: model.focus_settle(doc["focus"], now, zone, stop=True) and None)
        self.start_button.setFocus()

    def _goal_chosen(self, index: int) -> None:
        minutes = int(self.goal.itemData(index))
        self.context.store.mutate(lambda doc: doc["focus"].__setitem__("goal", minutes))
