"""The window of Weather: a page per place in the navigation pane, Manage places, Settings.

The pane lists the user's places in their order. Every place item has a page of its own, but there is
only one ForecastView: it moves into the page of the place that is shown. The NavigationView cannot
remove items, so the items are slots: slot ``place-<n>`` shows the n-th place, and slots without a
place are hidden.

Forecasts are fetched on the window's Worker with the key ``forecast:<place key>``, so requests for
one place never overlap. The last forecast of every place is cached; at start the cache fills the pane
and the page at once, and the current place is fetched from the service.
"""

from __future__ import annotations

import time
from functools import partial
from typing import Callable

from PySide6.QtCore import QLocale, Qt, QTimer
from PySide6.QtWidgets import QComboBox, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import APP_ID, model, places, settings
from ..model import Place
from ..service import ForecastCache, Result, ServiceError, WeatherService
from ..settings import Settings
from .forecast import ATTRIBUTION, ATTRIBUTION_URL, ForecastView, Shown
from .places_page import PlacesPage
from .widgets import scroll_page

VERSION = "1.0"
SLOT = "place-"
STALE_SECONDS = 10 * 60          # a forecast older than this is fetched again when its page is shown
TICK_MS = 5 * 60 * 1000          # the page is drawn again this often, so "now" moves on
BANNER_NARROW = 640              # content narrower than this: the banner's title goes into its message


def parse_arguments(arguments: list[str]) -> str | None:
    """The place asked for with ``--place NAME`` or ``--place=NAME``, or None."""
    name = None
    for index, argument in enumerate(arguments):
        if argument.startswith("--place="):
            name = argument.split("=", 1)[1]
        elif argument == "--place" and index + 1 < len(arguments):
            name = arguments[index + 1]
    name = model.clean_text(name, model.MAX_NAME) if name is not None else ""
    return name or None


class SlotPage(QWidget):
    """The page of a place item; the ForecastView is put into it while it is shown."""

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)


class WeatherWindow(fluent.FluentWindow):
    def __init__(self, arguments: list[str] | None = None, *, service: WeatherService | None = None,
                 clock: Callable[[], float] = time.time, imperial: bool | None = None) -> None:
        super().__init__(APP_ID, "Weather", navigation=True, icon="boswas-weather", size=(1100, 720),
                         minimum_size=(420, 420))
        self.clock = clock
        self.open_url = fluent.open_url                  # tests replace it
        self.worker = fluent.Worker(self)                # the network
        self.files = fluent.Worker(self, max_threads=1)  # the files, in the order they were asked for
        self.service = service if service is not None else WeatherService(
            cache=ForecastCache(fluent.cache_dir(APP_ID)))
        directory = fluent.config_dir(APP_ID)
        self.places_path = directory / places.FILE_NAME
        self.settings_path = directory / settings.FILE_NAME
        self.places, problem = places.load(self.places_path)
        if imperial is None:
            imperial = QLocale().measurementSystem() == QLocale.MeasurementSystem.ImperialUSSystem
        self.settings, _existed = settings.load(self.settings_path, Settings.imperial() if imperial else Settings())
        self.results: dict[str, Result] = {}
        self.errors: dict[str, ServiceError] = {}
        self.slots: list[str] = []
        self._asked: set[str] = set()                    # places asked of the service since the window opened
        self._shown_key: str | None = None               # the place the ForecastView shows
        self._banner_action: Callable[[], None] | None = None
        self._banner_context: str | None = None          # "forecast:<key>", "search", "undo", "files"
        self._banner_text = ("", "")                     # (title, message) of the banner
        self.places_page: PlacesPage | None = None
        self.settings_controls: dict[str, QComboBox] = {}

        nav = self.navigation
        self.status = nav.add_banner(fluent.InfoBar("info"))
        self.status.hide()
        self.status.action_triggered.connect(self._banner_triggered)
        self.status.closed.connect(self._banner_closed)
        self.busy_ring = fluent.ProgressRing(size=20)
        self.busy_ring.setAccessibleName("Getting the forecast")
        self.busy_ring.hide()
        self.refresh_button = fluent.IconButton("view-refresh-symbolic", "Refresh (F5)")
        self.refresh_button.clicked.connect(self.refresh_current)
        nav.add_header_widget(self.busy_ring)
        nav.add_header_widget(self.refresh_button)
        self.forecast_view = ForecastView(self)
        self.forecast_view.hide()
        self.forecast_view.retry_requested.connect(self.refresh_current)
        self.forecast_view.link_requested.connect(self._open_link)

        nav.add_section("My places")
        nav.add_page("places", "Manage places", icon="mark-location-symbolic", factory=self._places_page,
                     footer=True, keywords=("add", "search", "home", "city", "location"),
                     tooltip="Search for places, add, order and remove them")
        nav.add_page("settings", "Settings", icon="preferences-system-symbolic", factory=self._settings_page,
                     footer=True)
        self._sync_slots()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._interval_refresh)
        self._sync_refresh_timer()
        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(TICK_MS)
        self.tick_timer.timeout.connect(self._update_view)
        self.tick_timer.start()

        self.add_shortcut("refresh", self.refresh_current)
        self.add_shortcut("Ctrl+R", self.refresh_current)
        self.add_shortcut("find", self.focus_search)
        self.add_shortcut("preferences", lambda: nav.set_current("settings"))
        self.add_shortcut("next_tab", lambda: self._step_page(1))
        self.add_shortcut("previous_tab", lambda: self._step_page(-1))
        self.add_shortcut("quit", self.close)
        self.add_shortcut("close", self.close)

        if self.places.places:
            self.files.submit(self._read_cache, list(self.places.places), on_done=self._cache_read)
        start = self._find_place(parse_arguments(arguments or []))
        if start is None:
            home = self.places.index(self.places.home) if self.places.home else None
            start = home if home is not None else (0 if self.places.places else None)
        nav.set_current(f"{SLOT}{start}" if start is not None else "places")
        nav.current_changed.connect(self._page_changed)
        self._page_changed(nav.current())
        if problem:
            self.report("warning", problem)

    # --- places in the pane ---
    def _slot_index(self, key: str | None) -> int | None:
        if not key or not key.startswith(SLOT):
            return None
        index = int(key[len(SLOT):])
        return index if index < len(self.places.places) else None

    def current_place(self) -> Place | None:
        index = self._slot_index(self.navigation.current())
        return self.places.places[index] if index is not None else None

    def place_item(self, key: str) -> str | None:
        """The navigation key of the item that shows the place ``key``."""
        index = self.places.index(key)
        return f"{SLOT}{index}" if index is not None else None

    def _sync_slots(self) -> None:
        nav = self.navigation
        for index, place in enumerate(self.places.places):
            key = f"{SLOT}{index}"
            if index >= len(self.slots):
                nav.add_page(key, place.name, SlotPage(), icon="mark-location-symbolic")
                self.slots.append(key)
            nav.set_item_text(key, place.name)
            nav.set_item_visible(key, True)
            self._sync_slot_icon(index)
        for key in self.slots[len(self.places.places):]:
            nav.set_item_visible(key, False)
        current = nav.current()
        if current and current.startswith(SLOT):
            index = self._slot_index(current)
            if index is None:
                nav.set_current("places")
            elif self.places.places[index].key != self._shown_key:
                self._page_changed(current)          # another place is in the slot that is shown now

    def _sync_slot_icon(self, index: int) -> None:
        place = self.places.places[index]
        item = self.navigation.item(f"{SLOT}{index}")
        result = self.results.get(place.key)
        current = result.forecast.conditions(result.forecast.local_now(self.clock())) if result else None
        home = " (home)" if self.places.home == place.key else ""
        if current is not None:
            self.navigation.set_item_icon(f"{SLOT}{index}", model.condition_icon(current.code, current.is_day))
            temperature = model.format_temperature(current.temperature, self.settings.temperature, with_unit=True)
            tip = f"{place.label()}{home}: {temperature}, {model.condition_text(current.code, current.is_day)}"
        else:
            self.navigation.set_item_icon(f"{SLOT}{index}", "mark-location-symbolic")
            tip = place.label() + home
        item.setToolTip(tip)
        item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, tip)

    def _find_place(self, name: str | None) -> int | None:
        if not name:
            return None
        wanted = name.casefold()
        for test in (lambda place: place.name.casefold() == wanted,
                     lambda place: place.label().casefold().startswith(wanted)):
            for index, place in enumerate(self.places.places):
                if test(place):
                    return index
        return None

    def handle_arguments(self, arguments: list[str]) -> None:
        index = self._find_place(parse_arguments(arguments))
        if index is not None:
            self.navigation.set_current(f"{SLOT}{index}")

    def open_place(self, key: str) -> None:
        item = self.place_item(key)
        if item is not None:
            self.navigation.set_current(item)

    # --- changing the places ---
    def _save_places(self) -> None:
        self.files.submit(places.save, self.places_path, self.places.to_json(),
                          on_error=lambda error: self.report("error", f"Your places could not be saved: {error}",
                                                             context="files"))

    def _places_changed(self, focus: tuple[str, str] | None = None) -> None:
        self._save_places()
        self._sync_slots()
        if self.places_page is not None:
            self.places_page.refresh(focus)

    def add_place(self, place: Place) -> bool:
        if self.places.index(place.key) is not None:
            return False
        if self.places.is_full():
            self.report("warning", f"Weather keeps at most {places.MAX_PLACES} places. Remove one to add another.")
            return False
        first = not self.places.places
        self.places.add(place)
        if first:
            self.places.set_home(place.key)          # the first place is home until the user says otherwise
        self._places_changed()
        self.report("success", f"{place.name} was added to My places.", context="added")
        self.refresh(place)
        return True

    def remove_place(self, key: str) -> None:
        index = self.places.index(key)
        if index is None:
            return
        was_home = self.places.home == key
        position, place = self.places.remove(key)
        neighbours = self.places.places
        focus = (neighbours[min(position, len(neighbours) - 1)].key, "row") if neighbours else None
        self.errors.pop(key, None)
        self._places_changed(focus)
        if focus is None and self.places_page is not None:
            self.places_page.search_box.setFocus(Qt.FocusReason.OtherFocusReason)

        def undo() -> None:
            if self.places.insert(position, place):
                if was_home:
                    self.places.set_home(place.key)
                self._places_changed((place.key, "row"))
        self.offer_undo(f"{place.name} was removed.", undo)

    def move_place(self, key: str, step: int) -> bool:
        if not self.places.move(key, step):
            return False
        self._save_places()
        self._sync_slots()
        return True

    def set_home(self, key: str | None) -> None:
        if self.places.set_home(key):
            self._save_places()
            self._sync_slots()

    # --- forecasts ---
    @staticmethod
    def _task(key: str) -> str:
        return f"forecast:{key}"

    def _read_cache(self, wanted: list[Place]) -> dict[str, Result]:
        """On the files thread: the cached forecasts; files of places that are gone are deleted."""
        found = self.service.cached_many(wanted)
        if self.service.cache is not None:
            self.service.cache.prune({place.key for place in wanted})
        return found

    def _cache_read(self, found: dict[str, Result]) -> None:
        for key, result in found.items():
            if key not in self.results and self.places.index(key) is not None:
                self.results[key] = result
        self._sync_slots()
        self._update_view()
        place = self.current_place()
        if place is not None and place.key in found and self._banner_context == self._task(place.key):
            self._show_place_error(place)            # the request failed first: now the banner can say more

    def refresh(self, place: Place | None = None) -> bool:
        """Fetch the forecast of ``place`` (default: the one shown); False when one is on its way already."""
        place = place if place is not None else self.current_place()
        if place is None:
            return False
        key = place.key
        self._asked.add(key)
        task = self.worker.submit(self.service.forecast, place, key=self._task(key),
                                  on_done=partial(self._forecast_done, key),
                                  on_error=partial(self._forecast_failed, key))
        self._sync_busy()
        self._update_view()
        return task is not None

    def refresh_current(self) -> None:
        place = self.current_place()
        if place is not None:
            if self._banner_context == self._task(place.key):
                self.dismiss_banner()
            self.refresh(place)

    def _interval_refresh(self) -> None:
        self.refresh()

    def _forecast_done(self, key: str, result: Result) -> None:
        if self.places.index(key) is None:
            self.files.submit(self.service.forget, key)      # removed while it was fetched
            self._sync_busy()
            return
        self.results[key] = result
        self.errors.pop(key, None)
        if self._banner_context == self._task(key):
            self.dismiss_banner()
        self._sync_slot_icon(self.places.index(key))
        self._sync_busy()
        self._update_view()

    def _forecast_failed(self, key: str, error: Exception) -> None:
        if not isinstance(error, ServiceError):
            error = ServiceError(f"Something went wrong: {error}")
        if self.places.index(key) is not None:
            self.errors[key] = error
            place = self.current_place()
            if place is not None and place.key == key:
                self._show_place_error(place)
        self._sync_busy()
        self._update_view()

    def _show_place_error(self, place: Place) -> None:
        error = self.errors.get(place.key)
        if error is None:
            return
        cached = self.results.get(place.key)
        message = str(error)
        if cached is not None:
            updated = model.updated_text(cached.fetched, self.clock(), self.twelve_hour())
            message = f"Showing the last forecast, {updated[0].lower()}{updated[1:]}."
        self.show_error(error, lambda p=place: self.refresh(p), context=self._task(place.key), message=message)

    def _sync_busy(self) -> None:
        place = self.current_place()
        busy = place is not None and self.worker.busy(self._task(place.key))
        self.busy_ring.setVisible(busy)
        self.refresh_button.setVisible(place is not None)
        self.refresh_button.setEnabled(not busy)

    def _update_view(self) -> None:
        place = self.current_place()
        if place is None:
            return
        key = place.key
        self.forecast_view.show_state(Shown(place, self.results.get(key), self.errors.get(key), self.settings,
                                            self.twelve_hour(), self.clock(), self.places.home == key,
                                            self.worker.busy(self._task(key))))

    def twelve_hour(self) -> bool:
        choice = self.settings.time_format
        if choice in ("12", "24"):
            return choice == "12"
        return "ap" in QLocale().timeFormat(QLocale.FormatType.ShortFormat).lower()

    # --- pages ---
    def _page_changed(self, key: str | None) -> None:
        index = self._slot_index(key)
        if self._banner_context is not None and self._banner_context.startswith("forecast:"):
            self.dismiss_banner()
        self._shown_key = None
        if index is not None:
            place = self.places.places[index]
            self._shown_key = place.key
            page = self.navigation.page(key)
            if self.forecast_view.parentWidget() is not page:
                page.layout().addWidget(self.forecast_view)
                self.forecast_view.show()
            self._update_view()
            result = self.results.get(place.key)
            if result is None or self.clock() - result.fetched > STALE_SECONDS or place.key not in self._asked:
                self.refresh(place)
            elif place.key in self.errors:
                self._show_place_error(place)
        self._sync_busy()

    def _step_page(self, step: int) -> None:
        nav = self.navigation
        keys = [key for key in nav.keys() if nav.is_item_visible(key)]
        current = nav.current()
        index = keys.index(current) if current in keys else 0
        nav.set_current(keys[(index + step) % len(keys)])

    def focus_search(self) -> None:
        self.navigation.set_current("places")
        if self.places_page is not None:
            self.places_page.focus_search()

    def _places_page(self) -> QWidget:
        self.places_page = PlacesPage(self)
        return self.places_page

    def _open_link(self, url: str) -> None:
        self.open_url(url)

    # --- the banner ---
    def report(self, severity: str, message: str, title: str = "", *, context: str | None = None) -> None:
        self._banner_action = None
        self._banner_context = context
        self._banner_text = (title, message)
        self.status.set_action(None)
        self.status.show_message(severity, message, title)
        self._fit_banner()

    def _fit_banner(self) -> None:
        """In a narrow window the title of the banner becomes the first sentence of its message."""
        title, message = self._banner_text
        if not title:
            return
        if self.navigation.content.width() < BANNER_NARROW:
            self.status.set_title("")
            self.status.set_message(f"{title}. {message}")
        else:
            self.status.set_title(title)
            self.status.set_message(message)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.status.isVisible():
            self._fit_banner()

    def show_error(self, error: Exception, retry: Callable[[], None], *, context: str,
                   message: str | None = None) -> None:
        title = getattr(error, "title", "Something went wrong")
        self.report("error", message if message is not None else str(error), title, context=context)
        self._banner_action = retry
        self.status.set_action("Retry")

    def offer_undo(self, message: str, undo: Callable[[], None]) -> None:
        self.report("info", message, context="undo")
        self._banner_action = undo
        self.status.set_action("Undo")

    def dismiss_banner(self, context: str | None = None) -> None:
        if context is None or self._banner_context == context:
            self._banner_action = None
            self._banner_context = None
            self.status.set_action(None)
            self.status.hide()

    def _banner_triggered(self) -> None:
        action = self._banner_action
        self.dismiss_banner()
        if action is not None:
            action()

    def _banner_closed(self) -> None:
        self._banner_action = None
        self._banner_context = None

    # --- settings ---
    def _settings_page(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        def combo(name: str, choices: list[tuple[str, object]]) -> QComboBox:
            box = QComboBox()
            for label, value in choices:
                box.addItem(label, value)
            box.setCurrentIndex(max(0, box.findData(getattr(self.settings, name))))
            box.activated.connect(lambda index, b=box: self.set_setting(name, b.itemData(index)))
            self.settings_controls[name] = box
            return box

        def heading(text: str) -> None:
            if layout.count():
                layout.addSpacing(16)
            label = fluent.Label(text, "body-strong", content)
            label.setContentsMargins(0, 0, 0, 4)
            layout.addWidget(label)

        heading("Units")
        layout.addWidget(fluent.SettingsCard("Temperature", "", "temperature-symbolic", combo(
            "temperature", [("Celsius (°C)", "c"), ("Fahrenheit (°F)", "f")])))
        layout.addWidget(fluent.SettingsCard("Wind speed", "", "weather-windy-symbolic", combo(
            "wind", [("Kilometers per hour (km/h)", "kmh"), ("Miles per hour (mph)", "mph"),
                     ("Meters per second (m/s)", "ms"), ("Knots (kn)", "kn")])))
        layout.addWidget(fluent.SettingsCard("Precipitation", "", "weather-showers-symbolic", combo(
            "precipitation", [("Millimeters (mm)", "mm"), ("Inches (in)", "in")])))
        layout.addWidget(fluent.SettingsCard("Pressure", "", "weather-pressure-symbolic", combo(
            "pressure", [("Hectopascals (hPa)", "hpa"), ("Inches of mercury (inHg)", "inhg")])))
        layout.addWidget(fluent.SettingsCard("Time format", "How the hours and the sunrise and sunset are shown",
                                             "preferences-system-time-symbolic", combo(
                                                 "time_format", [("As the system is set", "locale"),
                                                                 ("12-hour", "12"), ("24-hour", "24")])))
        heading("Updates")
        layout.addWidget(fluent.SettingsCard(
            "Refresh automatically", "Get a new forecast of the place you are looking at while Weather is open",
            "view-refresh-symbolic", combo("refresh_minutes", [("Off", 0), ("Every 15 minutes", 15),
                                                              ("Every 30 minutes", 30), ("Every hour", 60)])))
        heading("About")
        layout.addWidget(fluent.SettingsCard(
            "Your location", "Weather never uses your location. It sends Open-Meteo only what you type into the "
            "search and the coordinates of the places you add, and nothing that identifies you.",
            "mark-location-symbolic"))
        self.about_link = fluent.HyperlinkButton(ATTRIBUTION)
        self.about_link.setToolTip(ATTRIBUTION_URL)
        self.about_link.clicked.connect(lambda: self._open_link(ATTRIBUTION_URL))
        layout.addWidget(fluent.SettingsCard("Weather", f"Version {VERSION}. Forecasts for the places you choose.",
                                             "help-about-symbolic", self.about_link))
        return scroll_page(content)

    def set_setting(self, name: str, value) -> None:
        if getattr(self.settings, name) == value:
            return
        setattr(self.settings, name, value)
        self.settings = Settings.from_json(self.settings.to_json())
        self.files.submit(settings.save, self.settings_path, self.settings.to_json(),
                          on_error=lambda error: self.report("error", f"The settings could not be saved: {error}",
                                                             context="files"))
        if name == "refresh_minutes":
            self._sync_refresh_timer()
        for index in range(len(self.places.places)):
            self._sync_slot_icon(index)
        self._update_view()

    def _sync_refresh_timer(self) -> None:
        minutes = self.settings.refresh_minutes
        if minutes > 0:
            self.refresh_timer.start(minutes * 60 * 1000)
        else:
            self.refresh_timer.stop()

    def closeEvent(self, event) -> None:
        super().closeEvent(event)
        if event.isAccepted():
            self.refresh_timer.stop()
            self.tick_timer.stop()
            self.files.wait(5.0)
            self.files.shutdown()
            self.worker.shutdown(0)
