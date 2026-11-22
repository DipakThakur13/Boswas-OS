"""Weather's window, driven as a person drives it: typing, clicks, keys; the network is a stand-in.

The service gets ``FakeNetwork`` as its HTTP call, so every request is answered from ``data/`` (or
fails on purpose) and is recorded. The clock stands still at 14:20 in Berlin. With the environment
variable BOSWAS_TEST_SHOTS set to a directory, the tests save screenshots there.
"""

from __future__ import annotations

import configparser
import contextlib
import importlib.util
import io
import json
import os
import re
import stat
import unittest
from pathlib import Path

from weather_fixtures import NOW, FakeNetwork, place

from boswas_apps.weather import APP_ID, model, places
from boswas_apps.weather.places import MAX_PLACES, PlaceList
from boswas_apps.weather.service import ForecastCache, NetworkError, ResponseError, WeatherService

PYSIDE = importlib.util.find_spec("PySide6") is not None
if PYSIDE:
    from boswas_fluent import testing

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QToolButton

    import boswas_fluent as fluent
    from boswas_apps.weather.ui import window as window_module
    from boswas_apps.weather.ui.forecast import ATTRIBUTION, ATTRIBUTION_URL
    from boswas_apps.weather.ui.window import WeatherWindow, parse_arguments
    BASE = testing.FluentTestCase
else:
    BASE = unittest.TestCase

SHOTS = os.environ.get("BOSWAS_TEST_SHOTS")
BERLIN, SYDNEY = "2950159", "2147714"
OFFLINE = NetworkError("Check your connection, then try again.")


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class WeatherCase(BASE):
    def setUp(self) -> None:
        super().setUp()
        self.network = FakeNetwork()
        self.addCleanup(self.network.release)
        self.cache_dir = self.tmp / "cache"
        self.service = WeatherService(fetch=self.network, cache=ForecastCache(self.cache_dir), clock=lambda: NOW)
        self.opened: list[str] = []

    @property
    def config(self) -> Path:
        return fluent.config_dir(APP_ID)

    def save_places(self, *names: str, home: str | None = None) -> None:
        places.save(self.config / "places.json", PlaceList([place(name) for name in names],
                                                           place(home).key if home else None))

    def open(self, *arguments: str, width: int = 1100, height: int = 720, settle: bool = True,
             compositor_files: list[str] | None = None) -> WeatherWindow:
        window = WeatherWindow(list(arguments), service=self.service, clock=lambda: NOW, imperial=False,
                               compositor_files=compositor_files)
        window.open_url = lambda url: self.opened.append(url) or True
        self.show(window, width, height)
        window.resize(width, height)                  # not the size a window of an earlier test remembered
        if settle:
            self.settle(window)
        return window

    def settle(self, window: WeatherWindow) -> None:
        self.assertTrue(window.worker.wait(10))
        self.assertTrue(window.files.wait(10))
        testing.process(20)

    def shot(self, window, name: str) -> None:
        if SHOTS:
            testing.grab(window, Path(SHOTS) / f"{name}.png")

    def manage(self, window: WeatherWindow):
        window.navigation.set_current("places")
        testing.process(10)
        return window.places_page

    def search(self, window: WeatherWindow, text: str):
        page = self.manage(window)
        page.search_box.clear()
        page.search_box.setFocus()
        testing.type_text(page.search_box, text)
        testing.key(page.search_box, Qt.Key.Key_Return)
        self.settle(window)
        return page

    def choose(self, window: WeatherWindow, name: str, value) -> None:
        window.navigation.set_current("settings")
        testing.process(10)
        box = window.settings_controls[name]
        index = box.findData(value)
        self.assertGreaterEqual(index, 0, f"{name}={value!r}")
        box.setCurrentIndex(index)
        box.activated.emit(index)
        testing.process(10)

    def saved_places(self) -> dict:
        return places.storage.read_json(self.config / "places.json", 1 << 20)


class EmptyStateTest(WeatherCase):
    def test_first_start(self):
        window = self.open()
        self.assertEqual(window.windowTitle(), "Weather")
        self.assertEqual(window.navigation.current(), "places")
        self.assertEqual(window.navigation.keys(), ["places", "settings"])
        page = window.places_page
        self.assertTrue(page.empty.isVisible())
        self.assertEqual(page.empty.title_label.text(), "No places yet")
        self.assertIn("never uses your location", page.empty.text_label.text())
        self.assertFalse(page.places_card.isVisible())
        self.assertFalse(window.refresh_button.isVisible())
        self.assertEqual(self.network.urls, [])               # nothing is looked up before the user asks
        self.shot(window, "dark-empty")


class SearchAndAddTest(WeatherCase):
    def test_search_add_and_open(self):
        window = self.open()
        page = window.places_page
        page.search_box.setFocus()
        testing.type_text(page.search_box, "Berlin")
        self.assertTrue(testing.wait_until(lambda: len(page.result_rows) == 5, 5))
        self.settle(window)
        self.assertEqual(self.network.count("search"), 1)          # typing waits for a pause
        self.assertEqual([row.title() for row in page.result_rows],
                         ["Berlin", "Berlin", "Berlin", "Berlin-Charlottenburg", "Berlín"])
        self.assertEqual([row.subtitle() for row in page.result_rows][:2],
                         ["Land Berlin, Germany", "New Hampshire, United States"])
        self.shot(window, "dark-search")
        testing.click(page.result_rows[0].button)
        self.settle(window)
        self.assertEqual(window.places.keys(), [BERLIN])
        self.assertEqual(window.places.home, BERLIN)                # the first place is home
        self.assertEqual(window.status.severity(), "success")
        self.assertIn("Berlin was added", window.status.message())
        self.assertTrue(window.navigation.is_item_visible("place-0"))
        self.assertEqual(window.navigation.item("place-0").text(), "Berlin")
        self.assertEqual(self.saved_places()["places"][0]["name"], "Berlin")
        self.assertEqual(self.saved_places()["home"], BERLIN)
        self.assertFalse(page.result_rows[0].isEnabled())
        self.assertEqual(page.result_rows[0]._trailing.text(), "Added")
        self.assertTrue(page.result_rows[1].button.hasFocus())      # on to the next place
        self.assertEqual(self.network.count("forecast"), 1)         # fetched at once, for the pane
        self.assertIn("8°C", window.navigation.item("place-0").toolTip())
        testing.click(page.place_rows[0].button)
        self.assertEqual(window.navigation.current(), "place-0")
        self.assertEqual(window.forecast_view.hero.temperature_label.text(), "8°C")

    def test_nothing_found_and_a_failed_search(self):
        window = self.open()
        page = self.search(window, "Atlantis")
        self.assertEqual(page.result_rows, [])
        self.assertIn("No places match “Atlantis”", page.results_label.text())
        self.network.error = OFFLINE
        self.search(window, "Berlin")
        self.assertTrue(window.status.isVisible())
        self.assertEqual(window.status.severity(), "error")
        self.assertEqual(window.status.title(), "Could not reach the weather service")
        self.assertEqual(window.status.action_button.text(), "Retry")
        self.network.error = None
        testing.click(window.status.action_button)
        self.settle(window)
        self.assertFalse(window.status.isVisible())
        self.assertEqual(len(page.result_rows), 5)

    def test_the_newest_search_wins_and_searches_do_not_overlap(self):
        window = self.open()
        page = self.manage(window)
        self.network.hold()
        page.search_box.setText("Berl")
        page.search_box.submit()
        page.search_box.setText("Sydney")
        page.search_box.submit()
        self.assertTrue(page.searching())
        self.assertTrue(page.search_ring.isVisibleTo(page))
        self.network.release()
        self.assertTrue(testing.wait_until(lambda: not page.searching(), 5))
        self.settle(window)
        self.assertEqual([row.subtitle() for row in page.result_rows], ["New South Wales, Australia",
                                                                       "Nova Scotia, Canada"])
        self.assertEqual(self.network.most_active, 1)
        self.assertFalse(page.search_ring.isVisibleTo(page))

    def test_at_most_fifty_places(self):
        many = [model.Place(f"Town {n}", 10.0, float(n), id=100 + n) for n in range(MAX_PLACES)]
        places.save(self.config / "places.json", PlaceList(many))
        window = self.open()
        self.assertEqual(len([key for key in window.navigation.keys() if window.navigation.is_item_visible(key)]),
                         MAX_PLACES + 2)
        page = self.search(window, "Sydney")
        testing.click(page.result_rows[0].button)
        self.assertEqual(len(window.places.places), MAX_PLACES)
        self.assertEqual(window.status.severity(), "warning")
        self.assertIn("at most 50 places", window.status.message())


class ForecastTest(WeatherCase):
    def test_the_page_shows_the_answer(self):
        self.save_places("berlin", "sydney", home="berlin")
        window = self.open()
        self.assertEqual(window.navigation.current(), "place-0")
        self.assertEqual(window.navigation.title(), "Berlin")
        self.assertEqual(self.network.count("forecast"), 1)          # only the place that is shown
        view = window.forecast_view
        hero = view.hero
        self.assertTrue(view.isVisible())
        self.assertEqual(hero.region_label.text(), "Land Berlin, Germany")
        self.assertTrue(hero.home_badge.isVisible())
        self.assertEqual(hero.temperature_label.text(), "8°C")
        self.assertEqual(hero.condition_label.text(), "Partly cloudy")
        self.assertEqual(hero.feels_label.text(), "Feels like 5°")
        self.assertEqual(hero.high_low_label.text(), "High 8° · Low 2°")
        self.assertEqual(hero.updated_label.text(), model.updated_text(NOW, NOW, window.twelve_hour()))
        self.assertFalse(hero.offline_badge.isVisible())
        cards = {card.title: (card.value(), card.detail()) for card in view.cards}
        self.assertEqual(cards["Wind"], ("15 km/h", "Gentle breeze from the southwest. Gusts up to 31 km/h."))
        self.assertEqual(view.wind_arrow.direction(), 236)
        self.assertEqual(cards["Humidity"], ("78%", "Dew point 4°."))
        self.assertEqual(cards["Pressure"], ("1012 hPa", "At sea level."))
        self.assertEqual(cards["UV index"], ("1", "Low. Highest today 1."))
        self.assertEqual(cards["Visibility"], ("24 km", "Good visibility."))
        self.assertEqual(cards["Precipitation"], ("0.0 mm", "Expected today. Chance of rain 8%."))
        self.assertEqual(cards["Sunrise"], ("07:44" if not window.twelve_hour() else "7:44 AM",
                                            "Daylight 8 h 15 min."))
        self.assertEqual(cards["Cloud cover"], ("64%", "Partly cloudy."))
        hourly = view.hourly
        self.assertEqual(len(hourly.hours()), 24)
        self.assertEqual(hourly.hours()[0].time.hour, 14)
        self.assertEqual(hourly.label(0), "Now")
        self.assertTrue(hourly.describe(0).startswith("Now, Partly cloudy, 8°C"))
        daily = view.daily
        self.assertEqual(len(daily.days()), 10)
        self.assertTrue(daily.describe(0).startswith("Today, Partly cloudy, high 8°C, low 2°C"))
        self.assertIn("Sunrise 07:44, sunset 15:59", daily.details(0))
        self.assertTrue(view.attribution.isVisibleTo(window))
        self.assertEqual(view.attribution.text(), ATTRIBUTION)
        self.assertEqual(ATTRIBUTION, "Weather data by Open-Meteo.com")
        testing.click(view.attribution)
        self.assertEqual(self.opened, [ATTRIBUTION_URL])
        self.assertTrue(window.refresh_button.isVisible())
        self.assertFalse(window.busy_ring.isVisible())
        self.shot(window, "dark-forecast")

    def test_another_place(self):
        self.save_places("berlin", "sydney")
        window = self.open()
        window.navigation.list.setFocus()
        testing.key(window.navigation.list, Qt.Key.Key_Down)
        self.settle(window)
        self.assertEqual(window.navigation.current(), "place-1")
        hero = window.forecast_view.hero
        self.assertEqual(hero.temperature_label.text(), "20°C")
        self.assertEqual(hero.condition_label.text(), "Clear")               # after midnight in Sydney
        self.assertFalse(hero.home_badge.isVisible())
        cards = {card.title: card.value() for card in window.forecast_view.cards}
        self.assertEqual((cards["Visibility"], cards["UV index"]), (model.MISSING, model.MISSING))
        self.assertFalse(window.forecast_view.uv_scale.isVisible())
        self.assertEqual(window.forecast_view.hourly.label(0), "Now")
        self.assertEqual(window.forecast_view.daily.days()[0].date.day, 22)
        self.assertIn("Sydney, New South Wales, Australia", window.navigation.item("place-1").toolTip())
        self.assertEqual(self.network.count("forecast"), 2)

    def test_units_and_time_format(self):
        self.save_places("berlin")
        window = self.open()
        for name, value in (("temperature", "f"), ("wind", "mph"), ("precipitation", "in"), ("pressure", "inhg"),
                            ("time_format", "12")):
            self.choose(window, name, value)
        window.navigation.set_current("place-0")
        self.settle(window)
        view = window.forecast_view
        self.assertEqual(view.hero.temperature_label.text(), "46°F")
        self.assertEqual(view.hero.feels_label.text(), "Feels like 40°")
        cards = {card.title: card.value() for card in view.cards}
        self.assertEqual((cards["Wind"], cards["Precipitation"], cards["Pressure"], cards["Visibility"]),
                         ("9 mph", "0.00 in", "29.89 inHg", "15 mi"))
        self.assertEqual((cards["Sunrise"], cards["Sunset"]), ("7:44 AM", "3:59 PM"))
        self.assertEqual(view.hourly.label(1), "3 PM")
        self.assertIn("47°F", view.hourly.describe(0))
        self.choose(window, "wind", "ms")
        window.navigation.set_current("place-0")
        self.assertEqual({card.title: card.value() for card in view.cards}["Wind"], "4.1 m/s")
        self.settle(window)
        saved = places.storage.read_json(self.config / "settings.json", 1 << 16)
        self.assertEqual((saved["temperature"], saved["wind"], saved["time_format"]), ("f", "ms", "12"))
        self.assertEqual(self.network.count("forecast"), 1)       # units are converted here, not fetched again
        window.close()
        again = self.open()
        self.assertEqual(again.settings.pressure, "inhg")
        again.navigation.set_current("settings")
        self.assertEqual(again.settings_controls["temperature"].currentData(), "f")

    def test_offline_shows_the_cached_forecast(self):
        self.save_places("berlin")
        self.open().close()
        self.assertTrue((self.cache_dir / f"forecast-{BERLIN}.json").exists())
        self.network.error = OFFLINE
        window = self.open()
        hero = window.forecast_view.hero
        self.assertIs(window.forecast_view.stack.currentWidget(), window.forecast_view.scroll)
        self.assertEqual(hero.temperature_label.text(), "8°C")
        self.assertTrue(hero.offline_badge.isVisible())
        self.assertEqual(hero.updated_label.text(), model.updated_text(NOW, NOW, window.twelve_hour()))
        self.assertTrue(window.status.isVisible())
        self.assertEqual(window.status.severity(), "error")
        self.assertEqual(window.status.title(), "Could not reach the weather service")
        self.assertEqual(window.status.message(), "Showing the last forecast, "
                         + model.updated_text(NOW, NOW, window.twelve_hour()).replace("Updated", "updated") + ".")
        self.assertEqual(window.status.action_button.text(), "Retry")
        self.shot(window, "dark-offline")
        window.resize(600, 720)
        testing.process(300)
        self.shot(window, "dark-offline-narrow")
        self.assertEqual(window.status.title(), "")
        self.assertTrue(window.status.message().startswith("Could not reach the weather service. Showing"))
        window.resize(1100, 720)
        testing.process(300)
        self.assertEqual(window.status.title(), "Could not reach the weather service")
        self.network.error = None
        testing.click(window.status.action_button)
        self.settle(window)
        self.assertFalse(window.status.isVisible())
        self.assertFalse(hero.offline_badge.isVisible())
        self.assertEqual(self.network.count("forecast"), 3)

    def test_an_error_without_a_cached_forecast(self):
        self.save_places("berlin")
        self.network.error = ResponseError("The weather service answered with an error (HTTP 503).")
        window = self.open()
        view = window.forecast_view
        self.assertIs(view.stack.currentWidget(), view.message_page)
        self.assertEqual(view.empty.title_label.text(), "No forecast for Berlin")
        self.assertIn("HTTP 503", view.empty.text_label.text())
        self.assertFalse(view.loading_ring.isVisible())
        self.assertEqual(window.status.title(), "The weather service did not answer as expected")
        self.network.error = None
        testing.click(view.empty.action_button)
        self.settle(window)
        self.assertIs(view.stack.currentWidget(), view.scroll)
        self.assertFalse(window.status.isVisible())

    def test_waiting_for_the_first_forecast(self):
        self.save_places("berlin")
        self.network.hold()
        window = self.open(settle=False)
        testing.process(20)
        view = window.forecast_view
        self.assertIs(view.stack.currentWidget(), view.message_page)
        self.assertTrue(view.loading_ring.isVisible())
        self.assertEqual(view.empty.title_label.text(), "Getting the forecast for Berlin")
        self.assertTrue(window.busy_ring.isVisible())
        self.network.release()
        self.settle(window)
        self.assertIs(view.stack.currentWidget(), view.scroll)
        self.assertFalse(window.busy_ring.isVisible())

    def test_requests_for_a_place_never_overlap(self):
        self.save_places("berlin")
        window = self.open()
        self.network.hold()
        for _ in range(3):
            testing.key(window, Qt.Key.Key_F5)
        testing.key(window, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)
        testing.click(window.refresh_button)
        testing.process(20)
        self.assertTrue(window.busy_ring.isVisible())
        self.assertFalse(window.refresh_button.isEnabled())
        self.assertEqual(self.network.count("forecast"), 2)
        self.network.release()
        self.settle(window)
        self.assertEqual(self.network.most_active, 1)
        self.assertFalse(window.busy_ring.isVisible())
        testing.click(window.refresh_button)
        self.settle(window)
        self.assertEqual(self.network.count("forecast"), 3)

    def test_refresh_on_the_interval(self):
        self.save_places("berlin")
        window = self.open()
        self.assertTrue(window.refresh_timer.isActive())
        self.assertEqual(window.refresh_timer.interval(), 30 * 60 * 1000)
        self.choose(window, "refresh_minutes", 15)
        self.assertEqual(window.refresh_timer.interval(), 15 * 60 * 1000)
        window.navigation.set_current("place-0")
        self.settle(window)
        before = self.network.count("forecast")
        window.refresh_timer.timeout.emit()
        self.settle(window)
        self.assertEqual(self.network.count("forecast"), before + 1)
        self.choose(window, "refresh_minutes", 0)
        self.assertFalse(window.refresh_timer.isActive())

    def test_place_argument(self):
        self.save_places("berlin", "sydney", home="berlin")
        window = self.open("--place", "sydney")
        self.assertEqual(window.navigation.current(), "place-1")
        window.handle_arguments(["--place=Berlin"])
        self.assertEqual(window.navigation.current(), "place-0")
        window.handle_arguments(["--place", "Atlantis"])
        self.assertEqual(window.navigation.current(), "place-0")
        self.assertEqual(parse_arguments(["--place=  Sao\nPaulo "]), "Sao Paulo")
        self.assertIsNone(parse_arguments(["--place"]))


class ManagePlacesTest(WeatherCase):
    def test_reorder_home_remove_undo(self):
        self.save_places("berlin", "sydney", home="berlin")
        window = self.open()
        page = self.manage(window)
        self.assertEqual([row.title() for row in page.place_rows], ["Berlin", "Sydney"])
        self.assertIn("Home", page.place_rows[0].subtitle())
        self.assertFalse(page.place_rows[0].up_button.isEnabled())
        testing.click(page.row(SYDNEY).up_button)
        self.assertEqual(window.places.keys(), [SYDNEY, BERLIN])
        self.assertEqual(window.navigation.item("place-0").text(), "Sydney")
        self.assertTrue(page.row(SYDNEY).button.hasFocus())         # its up button is off at the top
        testing.click(page.row(SYDNEY).home_button)
        self.assertEqual(window.places.home, SYDNEY)
        self.assertIn("Home", page.row(SYDNEY).subtitle())
        self.assertTrue(page.row(SYDNEY).home_button.hasFocus())
        self.settle(window)
        saved = self.saved_places()
        self.assertEqual(([entry["id"] for entry in saved["places"]], saved["home"]), ([2147714, 2950159], SYDNEY))
        testing.click(page.row(BERLIN).remove_button)
        self.assertEqual(window.places.keys(), [SYDNEY])
        self.assertFalse(window.navigation.is_item_visible("place-1"))
        self.assertEqual(window.status.action_button.text(), "Undo")
        testing.click(window.status.action_button)
        self.assertEqual(window.places.keys(), [SYDNEY, BERLIN])
        self.assertTrue(window.navigation.is_item_visible("place-1"))
        testing.click(page.row(SYDNEY).remove_button)
        self.assertIsNone(window.places.home)
        testing.click(window.status.action_button)
        self.assertEqual((window.places.keys(), window.places.home), ([SYDNEY, BERLIN], SYDNEY))
        self.shot(window, "dark-places")

    def test_keys_on_the_rows(self):
        self.save_places("berlin", "sydney")
        window = self.open()
        page = self.manage(window)
        page.search_box.setFocus()
        testing.key(page.search_box, Qt.Key.Key_Down)
        self.assertTrue(page.row(BERLIN).button.hasFocus())
        testing.key(page.row(BERLIN).button, Qt.Key.Key_Down)
        self.assertTrue(page.row(SYDNEY).button.hasFocus())
        testing.key(page.row(SYDNEY).button, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
        self.assertEqual(window.places.keys(), [SYDNEY, BERLIN])
        self.assertTrue(page.row(SYDNEY).button.hasFocus())
        testing.key(page.row(SYDNEY).button, Qt.Key.Key_Up)
        self.assertTrue(page.search_box.hasFocus())
        page.row(SYDNEY).button.setFocus()
        testing.key(page.row(SYDNEY).button, Qt.Key.Key_Delete)
        self.assertEqual(window.places.keys(), [BERLIN])
        self.assertTrue(page.row(BERLIN).button.hasFocus())
        testing.key(page.row(BERLIN).button, Qt.Key.Key_Return)
        self.assertEqual(window.navigation.current(), "place-0")

    def test_search_results_from_the_keyboard(self):
        window = self.open()
        page = self.search(window, "Sydney")
        testing.key(page.search_box, Qt.Key.Key_Down)
        self.assertTrue(page.result_rows[0].button.hasFocus())
        testing.key(page.result_rows[0].button, Qt.Key.Key_Return)
        self.settle(window)
        self.assertEqual(window.places.keys(), [SYDNEY])
        focused = QApplication.focusWidget()
        self.assertIs(focused, page.result_rows[1].button)
        testing.key(focused, Qt.Key.Key_Space)
        self.settle(window)
        self.assertEqual(len(window.places.places), 2)

    def test_a_damaged_places_file(self):
        (self.config / "places.json").write_text("{ this is not JSON")
        window = self.open()
        self.assertEqual(window.status.severity(), "warning")
        self.assertIn("places.json.damaged", window.status.message())
        self.assertTrue((self.config / "places.json.damaged").exists())
        self.assertEqual(window.places.places, [])

    def test_removed_while_its_forecast_is_fetched(self):
        self.save_places("berlin", "sydney")
        self.network.hold()
        window = self.open(settle=False)
        window.remove_place(BERLIN)
        self.network.release()
        self.settle(window)
        self.assertNotIn(BERLIN, window.results)
        self.assertFalse((self.cache_dir / f"forecast-{BERLIN}.json").exists())
        self.assertEqual(window.navigation.current(), "place-0")         # the slot shows Sydney now
        self.assertEqual(window.navigation.title(), "Sydney")
        self.assertEqual(window.forecast_view.hero.temperature_label.text(), "20°C")
        window.remove_place(SYDNEY)
        self.assertEqual(window.navigation.current(), "places")


class KeyboardTest(WeatherCase):
    def test_pages_and_shortcuts(self):
        self.save_places("berlin", "sydney")
        window = self.open()
        testing.key(window, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(window.navigation.current(), "place-1")
        testing.key(window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(window.navigation.current(), "places")
        self.assertTrue(window.places_page.search_box.hasFocus())
        testing.key(window, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(window.navigation.current(), "settings")
        self.settle(window)

    def test_the_forecast_from_the_keyboard(self):
        self.save_places("berlin")
        window = self.open()
        view = window.forecast_view
        window.navigation.list.setFocus()
        reached = set()
        for _ in range(40):
            focused = testing.press_tab()
            for name, widget in (("hourly", view.hourly), ("daily", view.daily), ("link", view.attribution),
                                 ("refresh", window.refresh_button)):
                if focused is widget:
                    reached.add(name)
        self.assertEqual(reached, {"hourly", "daily", "link", "refresh"})
        view.hourly.setFocus()
        testing.key(view.hourly, Qt.Key.Key_Right)
        self.assertEqual(view.hourly.cursor(), 1)
        self.assertIn("15:00" if not window.twelve_hour() else "3 PM", view.hourly.accessibleName())
        testing.key(view.hourly, Qt.Key.Key_End)
        self.assertEqual(view.hourly.cursor(), 23)
        host, rect = view.hourly.fluent_focus_rect()
        self.assertTrue(host.rect().contains(rect))
        view.daily.setFocus()
        testing.key(view.daily, Qt.Key.Key_Down)
        self.assertEqual(view.daily.cursor(), 1)
        self.assertIn("Tomorrow, Light rain", view.daily.accessibleName())

    def test_every_control_has_a_name(self):
        self.save_places("berlin", "sydney", home="berlin")
        window = self.open()
        self.manage(window)
        window.navigation.set_current("settings")
        window.navigation.set_current("place-0")
        self.settle(window)
        for button in window.findChildren(QToolButton):
            if button.isVisibleTo(window) and button.icon() and not button.text().strip():
                self.assertTrue(button.toolTip() or button.accessibleName(), button.objectName())
        for name in ("refresh_button",):
            self.assertTrue(getattr(window, name).toolTip())


class AppearanceTest(WeatherCase):
    def check_columns(self, window: WeatherWindow) -> None:
        columns = window.forecast_view.daily.layout_columns()
        order = ["day", "icon", "text", "chance", "low", "track", "high"]
        for first, second in zip(order, order[1:]):
            if columns[first].width() == 0:
                continue
            self.assertLessEqual(columns[first].right(), columns[second].left() + 0.5, (first, second))
        self.assertLessEqual(columns["high"].right(), window.forecast_view.daily.width())
        scroll = window.forecast_view.scroll
        self.assertLessEqual(scroll.widget().width(), scroll.viewport().width())

    def test_both_schemes_wide_and_narrow(self):
        self.save_places("berlin", "sydney", home="berlin")
        window = self.open()
        colours = {}
        for scheme in ("dark", "light"):
            testing.set_scheme(scheme)
            window.resize(1100, 720)
            window.navigation.set_current("place-0")
            testing.process(30)
            self.assertFalse(window.navigation.is_compact())
            self.assertEqual(window.forecast_view.details.columns(), 3)
            self.check_columns(window)
            image = window.grab().toImage()
            colours[scheme] = image.pixelColor(700, 700).lightness()
            self.shot(window, f"{scheme}-forecast")
            window.forecast_view.scroll.verticalScrollBar().setValue(100000)
            testing.process(10)
            self.shot(window, f"{scheme}-forecast-end")
            window.forecast_view.scroll.verticalScrollBar().setValue(0)
            window.resize(600, 720)
            testing.process(300)                      # the pane folds with an animation
            self.assertTrue(window.navigation.is_compact())
            self.assertEqual(window.forecast_view.details.columns(), 2)
            self.check_columns(window)
            self.shot(window, f"{scheme}-narrow")
            window.resize(420, 640)
            testing.process(100)
            self.check_columns(window)
            self.assertEqual(window.forecast_view.details.columns(), 1)
            self.shot(window, f"{scheme}-narrowest")
            window.resize(1100, 720)
            testing.process(300)
            self.manage(window)
            self.shot(window, f"{scheme}-places")
            window.navigation.set_current("settings")
            testing.process(30)
            self.shot(window, f"{scheme}-settings")
            settings_page = window.navigation.page("settings")
            settings_page.verticalScrollBar().setValue(100000)
            testing.process(10)
            self.assertTrue(window.about_link.isVisibleTo(window))
            self.assertEqual(window.about_link.text(), ATTRIBUTION)
            self.shot(window, f"{scheme}-settings-end")
            settings_page.verticalScrollBar().setValue(0)
        self.assertGreater(colours["light"], colours["dark"])
        testing.click(window.about_link)
        self.assertEqual(self.opened, [ATTRIBUTION_URL])

    def test_icons_of_the_pack(self):
        self.save_places("berlin", "sydney")
        window = self.open()
        self.manage(window)
        window.navigation.set_current("settings")
        window.navigation.set_current("place-1")
        self.settle(window)
        window.grab()
        ours = {name for name in fluent.missing_icons()
                if name.startswith(("weather-", "daytime-", "temperature", "boswas-weather"))}
        self.assertEqual(ours, set())

    def test_no_colour_or_size_of_its_own(self):
        root = Path(window_module.__file__).resolve().parent
        pattern = re.compile(r"#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b|QColor\(\s*[\d\"']|setStyleSheet|rgba?\(|"
                             r"setPointSize\(\s*\d|setPixelSize\(\s*\d|QFont\([^)]*,\s*\d|setDuration\(\s*\d")
        for path in sorted(root.glob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                self.assertIsNone(pattern.search(line), f"{path.name}:{number}: {line.strip()}")


class PackagingTest(unittest.TestCase):
    PACKAGE = Path(__file__).resolve().parents[2]

    @unittest.skipUnless(PYSIDE, "PySide6 is not installed")
    def test_command_line(self):
        from boswas_apps.weather import __main__ as cli
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["boswas-weather", "--help"]), 0)
        self.assertIn("--place NAME", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["boswas-weather", "--version"]), 0)
        self.assertEqual(output.getvalue(), f"boswas-weather {window_module.VERSION}\n")
        self.assertEqual(window_module.VERSION, "1.0")

    def test_launcher_and_desktop_entry(self):
        launcher = self.PACKAGE / "bin" / "boswas-weather"
        # Its mode is not checked here: the package build gives every
        # source file 0644 and installs launchers 0755 (the package stage
        # checks that).
        clock = (self.PACKAGE / "bin" / "boswas-clock").read_text()
        self.assertEqual(launcher.read_text(), clock.replace("Clock", "Weather").replace("clock", "weather"))
        entry = configparser.ConfigParser(interpolation=None)
        entry.optionxform = str
        entry.read(self.PACKAGE / "data" / "weather" / f"{APP_ID}.desktop", encoding="utf-8")
        main = entry["Desktop Entry"]
        self.assertEqual((main["Type"], main["Exec"], main["Icon"], main["StartupWMClass"]),
                         ("Application", "boswas-weather", "boswas-weather", APP_ID))
        self.assertTrue(main["Categories"].endswith(";") and main["Keywords"].endswith(";"))


class LockScreenTest(WeatherCase):
    """The summary the lock screen shows follows the window: the home place's forecast, the units, the setting."""

    def setUp(self) -> None:
        super().setUp()
        self.summary = Path(os.environ["XDG_STATE_HOME"], "boswas", "weather.json")
        self.setting = self.tmp / "compositor.toml"

    def lock_screen(self) -> dict | None:
        return json.loads(self.summary.read_text(encoding="utf-8")) if self.summary.exists() else None

    def test_the_home_place_goes_to_the_lock_screen(self):
        self.save_places("sydney", "berlin", home="berlin")
        window = self.open(compositor_files=[str(self.setting)])
        summary = self.lock_screen()
        self.assertEqual((summary["place"], summary["temperature"], summary["fetched"]), ("Berlin", "8°C", int(NOW)))
        self.assertEqual(summary["hours"][0]["time"], "14:00")
        self.assertEqual(stat.S_IMODE(self.summary.stat().st_mode), 0o600)
        # Other units and the 12-hour clock: written again at once.
        self.choose(window, "temperature", "f")
        self.choose(window, "time_format", "12")
        self.settle(window)
        summary = self.lock_screen()
        self.assertEqual((summary["temperature"], summary["hours"][0]["time"]), ("46°F", "2 PM"))
        # Another home whose forecast is not known yet: the old place leaves the lock screen until it is.
        window.set_home(SYDNEY)
        self.settle(window)
        self.assertIsNone(self.lock_screen())
        window.refresh(place("sydney"))
        self.settle(window)
        self.assertEqual(self.lock_screen()["place"], "Sydney")
        # Without a home, the first place; without places, nothing.
        window.set_home(None)
        window.move_place(BERLIN, -1)
        self.settle(window)
        self.assertEqual(self.lock_screen()["place"], "Berlin")
        window.remove_place(BERLIN)
        window.remove_place(SYDNEY)
        self.settle(window)
        self.assertIsNone(self.lock_screen())

    def test_a_newer_forecast_in_the_cache_wins(self):
        """The timer's update fetched while the window was open: the window does not put back its older one."""
        self.save_places("berlin")
        window = self.open(compositor_files=[str(self.setting)])
        self.assertEqual(self.lock_screen()["fetched"], int(NOW))
        answer = (Path(__file__).parent / "data" / "forecast-berlin.json").read_bytes()
        ForecastCache(self.cache_dir).write(place("berlin"), answer, NOW + 900)
        self.choose(window, "temperature", "f")
        self.settle(window)
        self.assertEqual((self.lock_screen()["fetched"], self.lock_screen()["temperature"]), (int(NOW) + 900, "46°F"))

    def test_nothing_for_the_lock_screen_when_it_does_not_show_the_weather(self):
        self.setting.write_text("[lock]\nshow_weather = false\n", encoding="utf-8")
        self.summary.parent.mkdir(parents=True)
        self.summary.write_text("{}", encoding="utf-8")
        self.save_places("berlin")
        self.open(compositor_files=[str(self.setting)])
        self.assertEqual(self.network.count("forecast"), 1, "the window still shows the forecast")
        self.assertIsNone(self.lock_screen(), "and the lock screen's summary is gone")

    def test_a_summary_that_cannot_be_written_is_no_trouble_for_the_window(self):
        Path(os.environ["XDG_STATE_HOME"], "boswas").write_text("not a directory", encoding="utf-8")
        self.save_places("berlin")
        window = self.open(compositor_files=[str(self.setting)])
        self.assertFalse(window.status.isVisible(), "no banner about it")
        self.assertEqual(window.navigation.current(), "place-0")


class LifecycleTest(WeatherCase):
    def test_closing_while_a_forecast_is_fetched(self):
        self.save_places("berlin")
        self.network.hold()
        window = self.open(settle=False)
        window.close()
        self.network.release()
        testing.process(50)

    def test_a_second_start_with_nothing_to_say(self):
        window = self.open()
        window.handle_arguments([])
        self.assertEqual(window.navigation.current(), "places")


if __name__ == "__main__":
    unittest.main()
