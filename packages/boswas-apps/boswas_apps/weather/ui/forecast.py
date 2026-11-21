"""The forecast of one place: the conditions now, the next 24 hours, the details, the next 10 days."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QBoxLayout, QHBoxLayout, QScrollArea, QStackedWidget, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import model
from ..model import Place
from ..service import Result, ServiceError
from ..settings import Settings
from .widgets import CardGrid, DailyList, DetailCard, HourlyStrip, UvScale, WindArrow, scroll_page

ATTRIBUTION = "Weather data by Open-Meteo.com"
ATTRIBUTION_URL = "https://open-meteo.com/"
NARROW = 400                     # below this width of the hero its texts go under the temperature


@dataclass(frozen=True)
class Shown:
    """What the view is asked to show (the window builds it)."""
    place: Place
    result: Result | None
    error: ServiceError | None
    settings: Settings
    twelve_hour: bool
    now: float
    home: bool
    loading: bool


class Hero(fluent.Card):
    """The conditions now: large icon and temperature, the condition, feels like, today's high and low."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, padding=24, spacing=12)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.region_label = fluent.Label("", "body", self, tone="secondary")
        self.home_badge = fluent.Badge("Home", self, kind="accent", icon=fluent.icon("starred-symbolic",
                                                                                     color="on-accent"))
        head.addWidget(self.region_label, 1)
        head.addWidget(self.home_badge)
        self.body.addLayout(head)
        self.main = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.main.setSpacing(20)
        self.icon_widget = fluent.IconWidget(None, 72, self)
        self.temperature_label = fluent.Label("", "title-large", self)
        lead = QHBoxLayout()
        lead.setSpacing(20)
        lead.addWidget(self.icon_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        lead.addWidget(self.temperature_label, 0, Qt.AlignmentFlag.AlignVCenter)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.condition_label = fluent.Label("", "subtitle", self, wrap=True)
        self.feels_label = fluent.Label("", "body", self, tone="secondary")
        self.high_low_label = fluent.Label("", "body", self)
        texts.addWidget(self.condition_label)
        texts.addWidget(self.feels_label)
        texts.addWidget(self.high_low_label)
        self.main.addLayout(lead)
        self.main.addLayout(texts, 1)
        self.body.addLayout(self.main)
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.updated_label = fluent.Label("", "caption", self, tone="secondary", wrap=True)
        self.offline_badge = fluent.Badge("Offline", self, kind="warning")
        foot.addWidget(self.updated_label, 1)
        foot.addWidget(self.offline_badge)
        self.body.addLayout(foot)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        direction = QBoxLayout.Direction.TopToBottom if self.width() < NARROW else QBoxLayout.Direction.LeftToRight
        if self.main.direction() != direction:
            self.main.setDirection(direction)


class ForecastView(QWidget):
    """One view for every place: the window moves it into the page of the place that is shown."""

    retry_requested = Signal()
    link_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.shown: Shown | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget(self)
        outer.addWidget(self.stack)

        # --- waiting for the first forecast, or none could be had ---
        self.message_page = QWidget()
        message = QVBoxLayout(self.message_page)
        message.setContentsMargins(32, 0, 32, 32)
        message.addStretch(1)
        self.loading_ring = fluent.ProgressRing(self.message_page, size=32)
        self.loading_ring.setAccessibleName("Getting the forecast")
        message.addWidget(self.loading_ring, 0, Qt.AlignmentFlag.AlignHCenter)
        self.empty = fluent.EmptyState("", "", "network-offline-symbolic", "Try again", self.message_page)
        self.empty.action_triggered.connect(self.retry_requested)
        message.addWidget(self.empty)
        message.addStretch(1)
        self.stack.addWidget(self.message_page)

        # --- the forecast ---
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.hero = Hero(content)
        layout.addWidget(self.hero)

        layout.addSpacing(12)
        layout.addWidget(fluent.Label("Hourly", "body-strong", content))
        self.hourly_card = fluent.Card(content, padding=8, spacing=0)
        self.hourly_area = QScrollArea(self.hourly_card)
        self.hourly_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.hourly_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.hourly_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.hourly_area.setWidgetResizable(False)
        self.hourly_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hourly = HourlyStrip()
        self.hourly_area.setWidget(self.hourly)
        scroll_bar = self.hourly_area.horizontalScrollBar().sizeHint().height()
        self.hourly_area.setFixedHeight(self.hourly.height() + scroll_bar + 4)
        self.hourly_card.add(self.hourly_area)
        layout.addWidget(self.hourly_card)

        layout.addSpacing(12)
        layout.addWidget(fluent.Label("Details", "body-strong", content))
        self.wind_card = DetailCard("Wind", "weather-windy-symbolic")
        self.wind_arrow = WindArrow(self.wind_card)
        self.wind_card.value_row.addWidget(self.wind_arrow, 0, Qt.AlignmentFlag.AlignVCenter)
        self.humidity_card = DetailCard("Humidity", "weather-humidity-symbolic")
        self.pressure_card = DetailCard("Pressure", "weather-pressure-symbolic")
        self.uv_card = DetailCard("UV index", "weather-clear-symbolic")
        self.uv_scale = UvScale(self.uv_card)
        self.uv_card.body.insertWidget(2, self.uv_scale)
        self.visibility_card = DetailCard("Visibility", "eye-open-symbolic")
        self.precipitation_card = DetailCard("Precipitation", "weather-showers-symbolic")
        self.sunrise_card = DetailCard("Sunrise", "daytime-sunrise-symbolic")
        self.sunset_card = DetailCard("Sunset", "daytime-sunset-symbolic")
        self.cloud_card = DetailCard("Cloud cover", "weather-clouds-symbolic")
        self.cards = [self.wind_card, self.humidity_card, self.pressure_card, self.uv_card, self.visibility_card,
                      self.precipitation_card, self.sunrise_card, self.sunset_card, self.cloud_card]
        self.details = CardGrid(content)
        self.details.set_widgets(self.cards)
        layout.addWidget(self.details)

        layout.addSpacing(12)
        layout.addWidget(fluent.Label("10-day forecast", "body-strong", content))
        self.daily_card = fluent.Card(content, padding=4, spacing=0)
        self.daily = DailyList(self.daily_card)
        self.daily_card.add(self.daily)
        layout.addWidget(self.daily_card)

        layout.addSpacing(8)
        self.attribution = fluent.HyperlinkButton(ATTRIBUTION, None, content)
        self.attribution.setFont(fluent.font("caption"))
        self.attribution.setToolTip(ATTRIBUTION_URL)
        self.attribution.clicked.connect(lambda: self.link_requested.emit(ATTRIBUTION_URL))
        layout.addWidget(self.attribution, 0, Qt.AlignmentFlag.AlignLeft)
        self.scroll = scroll_page(content)
        self.stack.addWidget(self.scroll)

    # --- what is shown ---
    def show_state(self, shown: Shown) -> None:
        changed_place = self.shown is None or self.shown.place.key != shown.place.key
        self.shown = shown
        if shown.result is None:
            self._show_message(shown)
            return
        self.stack.setCurrentWidget(self.scroll)
        self._show_forecast(shown)
        if changed_place:
            self.scroll.verticalScrollBar().setValue(0)
            self.hourly_area.horizontalScrollBar().setValue(0)

    def _show_message(self, shown: Shown) -> None:
        self.stack.setCurrentWidget(self.message_page)
        waiting = shown.loading or shown.error is None
        self.loading_ring.setVisible(waiting)
        if waiting:
            self.empty.set_icon(None)
            self.empty.set_title(f"Getting the forecast for {shown.place.name}")
            self.empty.set_text("")
            self.empty.action_button.hide()
        else:
            self.empty.set_icon("network-offline-symbolic")
            self.empty.set_title(f"No forecast for {shown.place.name}")
            self.empty.set_text(f"{shown.error.title}. {shown.error}")
            self.empty.action_button.show()

    def _show_forecast(self, shown: Shown) -> None:
        result, settings = shown.result, shown.settings
        forecast = result.forecast
        unit = settings.temperature
        local_now = forecast.local_now(shown.now)
        today = local_now.date()
        current = forecast.conditions(local_now) or model.Current()
        day = forecast.day(today)
        hero = self.hero

        hero.region_label.setText(shown.place.region() or forecast.timezone)
        hero.home_badge.setVisible(shown.home)
        hero.icon_widget.set_icon(model.condition_icon(current.code, current.is_day))
        hero.temperature_label.setText(model.format_temperature(current.temperature, unit, with_unit=True))
        condition = model.condition_text(current.code, current.is_day)
        hero.condition_label.setText(condition)
        hero.icon_widget.setToolTip(condition)
        hero.feels_label.setText(f"Feels like {model.format_temperature(current.apparent, unit)}")
        hero.feels_label.setVisible(current.apparent is not None)
        if day is not None:
            hero.high_low_label.setText(f"High {model.format_temperature(day.temperature_max, unit)} · "
                                        f"Low {model.format_temperature(day.temperature_min, unit)}")
        hero.high_low_label.setVisible(day is not None)
        updated = model.updated_text(result.fetched, shown.now, shown.twelve_hour)
        hero.offline_badge.setVisible(shown.error is not None)
        hero.updated_label.setText(updated)
        hero.offline_badge.setToolTip("The weather service cannot be reached: this is the last forecast Weather got")
        hero.setAccessibleName(f"{shown.place.name}: {hero.temperature_label.text()}, {condition}")
        hero.setAccessibleDescription(" ".join(label.text() for label in (
            hero.feels_label, hero.high_low_label, hero.updated_label) if label.isVisible() or label.text()))

        hours = forecast.hours_from(local_now, 24)
        now_first = bool(hours) and hours[0].time <= local_now < hours[0].time + dt.timedelta(hours=1)
        self.hourly.set_hours(hours, unit, shown.twelve_hour, now_first=now_first)
        self.hourly_card.setVisible(bool(hours))

        self._show_details(current, day, forecast.day(today + dt.timedelta(days=1)), settings, shown.twelve_hour)
        self.daily.set_days(forecast.days_from(today, 10), today, unit, settings.precipitation,
                            current=current.temperature, twelve_hour=shown.twelve_hour)
        self.daily_card.setVisible(bool(self.daily.days()))

    def _show_details(self, current: model.Current, day: model.Day | None, tomorrow: model.Day | None,
                      settings: Settings, twelve_hour: bool) -> None:
        unit = settings.temperature

        def sentence(*parts: str) -> str:
            return " ".join(f"{part}." for part in parts if part)

        force = model.beaufort(current.wind_speed)
        direction = model.compass_name(current.wind_direction)
        wind_words = f"{force} from the {direction}" if force and direction else force
        gusts = f"Gusts up to {model.format_wind(current.wind_gusts, settings.wind)}" \
            if current.wind_gusts is not None else ""
        self.wind_card.set_values(model.format_wind(current.wind_speed, settings.wind), sentence(wind_words, gusts))
        self.wind_arrow.set_direction(current.wind_direction)

        dew = model.dew_point(current.temperature, current.humidity)
        self.humidity_card.set_values(model.format_percent(current.humidity),
                                      sentence(f"Dew point {model.format_temperature(dew, unit)}" if dew is not None
                                               else ""))
        self.pressure_card.set_values(model.format_pressure(current.pressure, settings.pressure),
                                      "At sea level." if current.pressure is not None else "")

        uv = current.uv_index
        maximum = day.uv_index_max if day is not None else None
        self.uv_card.set_values(model.format_uv(uv), sentence(
            model.uv_category(uv),
            f"Highest today {model.format_uv(maximum)}" if maximum is not None else ""))
        self.uv_scale.set_value(uv)
        self.uv_scale.setVisible(uv is not None)

        distance = model.distance_unit(settings.wind)
        self.visibility_card.set_values(model.format_visibility(current.visibility, distance),
                                        sentence(model.visibility_words(current.visibility)))

        total = day.precipitation_sum if day is not None else None
        chance = day.precipitation_probability_max if day is not None else None
        self.precipitation_card.set_values(
            model.format_precipitation(total, settings.precipitation),
            sentence("Expected today" if total is not None else "",
                     f"Chance of rain {model.format_percent(chance)}" if chance is not None else ""))

        sunrise = day.sunrise if day is not None else None
        sunset = day.sunset if day is not None else None
        daylight = sentence(f"Daylight {model.format_duration((sunset - sunrise).total_seconds())}"
                            if sunrise is not None and sunset is not None and sunset > sunrise else "")
        self.sunrise_card.set_values(model.format_clock(sunrise, twelve_hour), daylight)
        next_rise = tomorrow.sunrise if tomorrow is not None else None
        self.sunset_card.set_values(model.format_clock(sunset, twelve_hour), sentence(
            f"Sunrise tomorrow {model.format_clock(next_rise, twelve_hour)}" if next_rise is not None else ""))

        self.cloud_card.set_values(model.format_percent(current.cloud_cover),
                                   sentence(model.cloud_words(current.cloud_cover)))
