"""The Alarms page: a card per alarm, and the dialog that makes or changes one."""

from __future__ import annotations

from PySide6.QtCore import QTime, Qt
from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLineEdit, QStackedWidget, QTimeEdit, QVBoxLayout, QWidget

import boswas_fluent as fluent

from ...reminders import textfmt
from .. import model
from .common import ClickCard, Context
from .widgets import CardGrid, DayPicker, Ticker, scroll_page


class AlarmDialog(fluent.ContentDialog):
    """Time, name, weekdays and snooze of one alarm. ``values()`` after it was accepted."""

    def __init__(self, parent: QWidget, context: Context, alarm: dict | None) -> None:
        super().__init__(parent, "Edit alarm" if alarm else "Add an alarm", primary="Save",
                         secondary="Delete" if alarm else None, close="Cancel")
        form = QWidget(self)
        layout = QFormLayout(form)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setVerticalSpacing(12)
        self.time = QTimeEdit(form)
        self.time.setDisplayFormat(context.time_format())
        self.time.setAccessibleName("Time")
        self.time.setTime(QTime(alarm["hour"], alarm["minute"]) if alarm else QTime(7, 0))
        self.name = QLineEdit(form)
        self.name.setMaxLength(model.MAX_NAME)
        self.name.setPlaceholderText("Alarm name")
        self.name.setAccessibleName("Alarm name")
        self.name.setText(alarm["name"] if alarm else "")
        self.days = DayPicker(form, first_day=context.first_day())
        self.days.set_days(alarm["days"] if alarm else [])
        self.repeat_label = fluent.Label("", "caption", form, tone="secondary")
        self.days.changed.connect(self._describe)
        self.snooze = QComboBox(form)
        self.snooze.setAccessibleName("Snooze time")
        chosen = alarm["snooze"] if alarm else context.settings.values.default_snooze
        for minutes in sorted({*model.SNOOZE_CHOICES, chosen}):
            self.snooze.addItem(textfmt.duration_words(minutes * 60), minutes)
        self.snooze.setCurrentIndex(self.snooze.findData(chosen))
        days = QVBoxLayout()
        days.setSpacing(4)
        days.addWidget(self.days)
        days.addWidget(self.repeat_label)
        layout.addRow("Time", self.time)
        layout.addRow("Name", self.name)
        layout.addRow("Repeat", days)
        layout.addRow("Snooze", self.snooze)
        self.set_content(form)
        self._describe()

    def _describe(self) -> None:
        days = self.days.days()
        self.repeat_label.setText("Rings once" if not days else textfmt.weekdays_text(days))

    def values(self) -> dict:
        moment = self.time.time()
        return {"hour": moment.hour(), "minute": moment.minute(), "name": self.name.text(),
                "days": self.days.days(), "snooze": int(self.snooze.currentData())}


class AlarmCard(ClickCard):
    def __init__(self, page: "AlarmsPage", alarm: dict) -> None:
        super().__init__(padding=16, spacing=6)
        self.alarm_id = alarm["id"]
        top = QHBoxLayout()
        self.time_label = fluent.Label("", "title-large", self)
        self.switch = fluent.ToggleSwitch("", self, show_state=False)
        self._showing = False
        self.switch.toggled.connect(lambda on: None if self._showing else page.set_enabled(self.alarm_id, on))
        top.addWidget(self.time_label, 1)
        top.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignTop)
        self.next_label = fluent.Label("", "caption", self, tone="secondary")
        self.name_label = fluent.Label("", "body-strong", self)
        self.days = DayPicker(self, first_day=page.context.first_day(), size=26, editable=False)
        self.once_label = fluent.Label("Once", "caption", self, tone="secondary")
        self.body.addLayout(top)
        self.body.addWidget(self.next_label)
        self.body.addWidget(self.name_label)
        self.body.addWidget(self.days)
        self.body.addWidget(self.once_label)
        self.setMinimumHeight(170)

    def show_alarm(self, alarm: dict, context: Context) -> None:
        name = alarm["name"] or "Alarm"
        time_text = textfmt.clock_time(alarm["hour"], alarm["minute"], context.twelve_hour())
        self.time_label.setText(time_text)
        self.time_label.set_tone(None if alarm["enabled"] else "muted")
        self.name_label.setText(name)
        self.days.set_days(alarm["days"])
        self.days.setVisible(bool(alarm["days"]))
        self.once_label.setVisible(not alarm["days"])
        self._showing = True                # the switch animates through its signal: it is not blocked
        self.switch.setChecked(alarm["enabled"])
        self._showing = False
        self.switch.setAccessibleName(f"{name}, {time_text}")
        self.setAccessibleName(f"{name}, {time_text}, {textfmt.weekdays_text(alarm['days'])}")
        self.show_next(alarm, context)

    def show_next(self, alarm: dict, context: Context) -> None:
        now = context.now().wall
        ring = model.alarm_next(alarm, now, context.zone())
        self.next_label.setText(f"Rings {textfmt.until_words(ring - now)}" if ring is not None else "Off")
        self.setAccessibleDescription(self.next_label.text())


class AlarmsPage(QWidget):
    def __init__(self, context: Context, window) -> None:
        super().__init__()
        self.context = context
        self._window = window
        self.cards: dict[str, AlarmCard] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        bar_holder = QHBoxLayout()
        bar_holder.setContentsMargins(32, 0, 32, 0)
        self.commands = fluent.CommandBar(self)
        self.add_action_ = self.commands.add_command("Add an alarm", "list-add-symbolic", self.add_alarm,
                                                     shortcut="Ctrl+N")
        bar_holder.addWidget(self.commands)
        layout.addLayout(bar_holder)
        self.stack = QStackedWidget(self)
        self.grid = CardGrid(column_width=280)
        self.stack.addWidget(scroll_page(self.grid))
        self.empty = fluent.EmptyState("No alarms yet", "Add an alarm and it rings at its time, also while Clock "
                                       "is closed.", "alarm-symbolic", "Add an alarm")
        self.empty.action_triggered.connect(self.add_alarm)
        self.stack.addWidget(self.empty)
        layout.addWidget(self.stack, 1)
        self.ticker = Ticker(self, self._tick)
        context.store.changed.connect(self.refresh)
        context.settings.changed.connect(self.refresh)
        self.refresh()

    # --- showing ---
    def refresh(self) -> None:
        alarms = sorted(self.context.store.doc["alarms"], key=lambda alarm: (alarm["hour"], alarm["minute"]))
        ids = [alarm["id"] for alarm in alarms]
        if ids != [card.alarm_id for card in self.grid.widgets()]:
            focused = next((card.alarm_id for card in self.cards.values() if card.hasFocus()), None)
            cards = []
            for alarm in alarms:
                card = self.cards.get(alarm["id"])
                if card is None:
                    card = AlarmCard(self, alarm)
                    card.clicked.connect(lambda i=alarm["id"]: self.edit_alarm(i))
                    card.menu_requested.connect(lambda position, i=alarm["id"]: self._menu(i, position))
                cards.append(card)
            self.cards = {card.alarm_id: card for card in cards}
            self.grid.set_widgets(cards)
            if focused in self.cards:
                self.cards[focused].setFocus()
        for alarm in alarms:
            self.cards[alarm["id"]].show_alarm(alarm, self.context)
        self.stack.setCurrentIndex(0 if alarms else 1)
        self.ticker.kick()

    def _tick(self) -> int | None:
        alarms = self.context.store.doc["alarms"]
        for alarm in alarms:
            card = self.cards.get(alarm["id"])
            if card is not None:
                card.show_next(alarm, self.context)
        if not any(alarm["enabled"] for alarm in alarms):
            return None
        return int((60 - self.context.now().wall % 60) * 1000) + 50       # the texts change with the minute

    # --- changing ---
    def set_enabled(self, alarm_id: str, enabled: bool) -> None:
        wall = self.context.now().wall

        def change(doc: dict) -> None:
            alarm = model.find(doc["alarms"], alarm_id)
            if alarm is not None:
                model.arm_alarm(alarm, enabled, wall)
        self.context.store.mutate(change)

    def add_alarm(self) -> None:
        dialog = AlarmDialog(self._window, self.context, None)
        result = self._window.run_dialog(dialog)
        values = dialog.values()
        dialog.deleteLater()
        if result != fluent.ContentDialog.Result.PRIMARY:
            return
        if len(self.context.store.doc["alarms"]) >= model.MAX_ALARMS:
            self._window.report("warning", f"There can be at most {model.MAX_ALARMS} alarms.")
            return
        alarm = model.make_alarm(now_wall=self.context.now().wall, **values)
        self.context.store.mutate(lambda doc: doc["alarms"].append(dict(alarm))
                                  if model.find(doc["alarms"], alarm["id"]) is None else None)

    def edit_alarm(self, alarm_id: str) -> None:
        alarm = model.find(self.context.store.doc["alarms"], alarm_id)
        if alarm is None:
            return
        dialog = AlarmDialog(self._window, self.context, alarm)
        result = self._window.run_dialog(dialog)
        values = dialog.values()
        dialog.deleteLater()
        if result == fluent.ContentDialog.Result.SECONDARY:
            self.delete_alarm(alarm_id)
        elif result == fluent.ContentDialog.Result.PRIMARY:
            wall = self.context.now().wall

            def change(doc: dict) -> None:
                stored = model.find(doc["alarms"], alarm_id)
                if stored is not None:
                    fresh = model.make_alarm(now_wall=wall, **values)
                    fresh["id"] = alarm_id
                    stored.update(fresh)               # saving switches the alarm on, as setting one does
            self.context.store.mutate(change)

    def delete_alarm(self, alarm_id: str) -> None:
        alarm = model.find(self.context.store.doc["alarms"], alarm_id)
        if alarm is None:
            return
        removed = dict(alarm)
        self.context.store.mutate(lambda doc: doc.__setitem__(
            "alarms", [entry for entry in doc["alarms"] if entry["id"] != alarm_id]))
        name = removed["name"] or textfmt.clock_time(removed["hour"], removed["minute"], self.context.twelve_hour())
        self._window.offer_undo(f"Alarm “{name}” deleted.", lambda: self.context.store.mutate(
            lambda doc: doc["alarms"].append(dict(removed)) if model.find(doc["alarms"], alarm_id) is None else None))

    def _menu(self, alarm_id: str, position) -> None:
        menu = fluent.MenuFlyout(self)
        menu.add_item("Edit", lambda: self.edit_alarm(alarm_id), icon="document-edit-symbolic")
        menu.add_item("Delete", lambda: self.delete_alarm(alarm_id), icon="edit-delete-symbolic", shortcut="Del")
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(position)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Delete:
            for card in self.cards.values():
                if card.hasFocus():
                    self.delete_alarm(card.alarm_id)
                    return
        super().keyPressEvent(event)
