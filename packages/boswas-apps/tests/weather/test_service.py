"""The weather service without Qt and without a network: addresses, the HTTP call, the cache."""

import email.message
import io
import json
import socket
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from weather_fixtures import NOW, FakeNetwork, answer, place, raw

from boswas_apps.weather import model, service
from boswas_apps.weather.service import ForecastCache, NetworkError, ResponseError, WeatherService


def query_of(url: str) -> dict[str, str]:
    parts = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    result = dict(pairs)
    assert len(result) == len(pairs), "a parameter is sent twice"
    return result


class AddressTest(unittest.TestCase):
    def test_search_sends_the_text_and_nothing_else(self):
        url = service.search_url("  Berlin\n")
        parts = urllib.parse.urlsplit(url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "geocoding-api.open-meteo.com",
                                                                     "/v1/search"))
        self.assertEqual(query_of(url), {"name": "Berlin", "count": "10", "language": "en", "format": "json"})
        self.assertEqual(query_of(service.search_url("São Paulo & co"))["name"], "São Paulo & co")
        self.assertEqual(len(query_of(service.search_url("x" * 500))["name"]), service.MAX_QUERY)
        for short in ("", " ", "a", "\n\n"):
            with self.assertRaises(ValueError):
                service.search_url(short)

    def test_forecast_sends_the_coordinates_and_nothing_else(self):
        berlin = place("berlin")
        url = service.forecast_url(berlin.latitude, berlin.longitude)
        parts = urllib.parse.urlsplit(url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "api.open-meteo.com", "/v1/forecast"))
        query = query_of(url)
        self.assertEqual(set(query), {"latitude", "longitude", "timezone", "forecast_days", "current", "hourly",
                                      "daily"})
        self.assertEqual((query["latitude"], query["longitude"]), ("52.5244", "13.4105"))
        self.assertEqual((query["timezone"], query["forecast_days"]), ("auto", "10"))
        self.assertEqual(query["current"], "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,"
                         "precipitation,weather_code,cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,"
                         "wind_gusts_10m,visibility,uv_index")
        self.assertEqual(query["hourly"], "temperature_2m,apparent_temperature,precipitation_probability,"
                         "precipitation,weather_code,is_day,wind_speed_10m")
        self.assertEqual(query["daily"], "weather_code,temperature_2m_max,temperature_2m_min,"
                         "apparent_temperature_max,apparent_temperature_min,sunrise,sunset,uv_index_max,"
                         "precipitation_sum,precipitation_probability_max,wind_speed_10m_max,"
                         "wind_direction_10m_dominant")
        self.assertIn("current=temperature_2m,relative", url)                # commas are sent as they are
        self.assertEqual(query_of(service.forecast_url(-33.86785, 151.20732))["latitude"], "-33.8678")
        with self.assertRaises(ValueError):
            service.forecast_url(95, 0)


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, length: str | None = None) -> None:
        super().__init__(body)
        self.headers = email.message.Message()
        if length is not None:
            self.headers["Content-Length"] = length


class FakeOpener:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class HttpTest(unittest.TestCase):
    def get(self, outcome, url=None, **options):
        opener = FakeOpener(outcome)
        with mock.patch("urllib.request.build_opener", return_value=opener) as build:
            try:
                return service.http_get(url or service.search_url("Berlin"), **options), opener
            finally:
                if build.called:
                    handlers = build.call_args.args
                    self.assertTrue(any(isinstance(handler, service._NoRedirects) for handler in handlers))

    def test_a_plain_request(self):
        body, opener = self.get(FakeResponse(b'{"results": []}', "15"))
        self.assertEqual(body, b'{"results": []}')
        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 15.0)
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(headers, {"user-agent": "boswas-weather/1.0", "accept": "application/json"})
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)

    def test_only_the_weather_service_is_asked(self):
        for url in ("http://api.open-meteo.com/v1/forecast", "https://example.org/v1/search",
                    "https://api.open-meteo.com.example.org/", "file:///etc/passwd"):
            with mock.patch("urllib.request.build_opener") as build:
                with self.assertRaises(ValueError):
                    service.http_get(url)
                build.assert_not_called()

    def test_size_caps(self):
        with self.assertRaisesRegex(ResponseError, "more data"):
            self.get(FakeResponse(b"x" * 101), max_bytes=100)
        with self.assertRaisesRegex(ResponseError, "more data"):
            self.get(FakeResponse(b"{}", str(10 ** 9)))
        body, _opener = self.get(FakeResponse(b"x" * 100), max_bytes=100)
        self.assertEqual(len(body), 100)

    def test_failures(self):
        for error in (urllib.error.URLError("Name or service not known"), socket.timeout("timed out"),
                      ConnectionResetError(104, "reset"), OSError(101, "Network is unreachable")):
            with self.assertRaises(NetworkError) as caught:
                self.get(error)
            self.assertEqual(caught.exception.title, "Could not reach the weather service")
        body = io.BytesIO(json.dumps({"error": True, "reason": "Cannot initialize WeatherVariable from invalid "
                                      "String value tempeture_2m"}).encode())
        refused = urllib.error.HTTPError("https://api.open-meteo.com/v1/forecast", 400, "Bad Request",
                                         email.message.Message(), body)
        with self.assertRaisesRegex(ResponseError, r"HTTP 400\)\. Cannot initialize"):
            self.get(refused)
        moved = urllib.error.HTTPError("https://api.open-meteo.com/", 302, "Found", email.message.Message(),
                                       io.BytesIO(b"<html>"))
        with self.assertRaisesRegex(ResponseError, r"HTTP 302\)\.$"):
            self.get(moved)

    def test_redirects_are_refused(self):
        handler = service._NoRedirects()
        self.assertIsNone(handler.redirect_request(None, None, 301, "Moved", {}, "https://example.org/"))

    def test_decode(self):
        self.assertEqual(service.decode(b'{"a": [1]}'), {"a": [1]})
        for bad in (b"<html>", b"\xff\xfe", b"[" * 100000, "text"):
            with self.assertRaises(ResponseError):
                service.decode(bad)


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = Path(self._tmp.name) / "cache"
        self.network = FakeNetwork()
        self.service = WeatherService(fetch=self.network, cache=ForecastCache(self.cache_dir), clock=lambda: NOW)

    def test_search(self):
        found = self.service.search("Berlin")
        self.assertEqual(len(found), 5)
        self.assertEqual(found[0].key, "2950159")
        self.assertEqual(self.service.search("Atlantis"), [])
        self.assertEqual(self.network.urls[0], service.search_url("Berlin"))
        self.network.overrides["search"] = b"<html>busy</html>"
        with self.assertRaises(ResponseError):
            self.service.search("Berlin")
        self.network.overrides["search"] = b'{"results": 7}'
        with self.assertRaises(ResponseError):
            self.service.search("Berlin")

    def test_forecast_is_cached_as_it_came(self):
        berlin = place("berlin")
        result = self.service.forecast(berlin)
        self.assertFalse(result.cached)
        self.assertEqual(result.fetched, NOW)
        self.assertEqual(result.forecast.current.temperature, 7.9)
        path = self.cache_dir / "forecast-2950159.json"
        stored = json.loads(path.read_text("utf-8"))
        self.assertEqual((stored["version"], stored["key"], stored["fetched"]), (1, "2950159", NOW))
        self.assertEqual(stored["data"], answer("forecast-berlin.json"))      # metric, whatever the settings
        again = self.service.cached(berlin)
        self.assertTrue(again.cached)
        self.assertEqual((again.forecast, again.fetched), (result.forecast, NOW))
        self.assertIsNone(self.service.cached(place("sydney")))
        self.assertEqual(self.service.cached_many([berlin, place("sydney")]), {"2950159": again})
        self.service.forget(berlin.key)
        self.assertFalse(path.exists())

    def test_bad_forecasts(self):
        self.network.overrides["forecast"] = b'{"error": true, "reason": "No data is available for this location"}'
        with self.assertRaisesRegex(ResponseError, "No data is available"):
            self.service.forecast(place("berlin"))
        self.network.overrides["forecast"] = b"\x00" * 10
        with self.assertRaises(ResponseError):
            self.service.forecast(place("berlin"))
        self.assertFalse(self.cache_dir.exists() and any(self.cache_dir.iterdir()))
        self.network.error = NetworkError("Check your connection, then try again.")
        with self.assertRaises(NetworkError):
            self.service.forecast(place("berlin"))

    def test_a_forecast_that_cannot_be_cached_is_still_given(self):
        self.cache_dir.parent.joinpath("cache").write_text("a file where the directory should be")
        result = self.service.forecast(place("berlin"))
        self.assertEqual(result.forecast.current.code, 2)

    def test_damaged_cache_files(self):
        berlin = place("berlin")
        cache = self.service.cache
        self.service.forecast(berlin)
        path = cache.path(berlin.key)
        good = path.read_bytes()
        stored = json.loads(good)
        cases = [b"{ broken", b"null", json.dumps({**stored, "version": 2}).encode(),
                 json.dumps({**stored, "key": "2147714"}).encode(),
                 json.dumps({**stored, "fetched": NOW + 10 * 86400}).encode(),
                 json.dumps({**stored, "fetched": "yesterday"}).encode(),
                 json.dumps({**stored, "data": {"hourly": {}}}).encode(),
                 b" " * (service.MAX_BYTES + 5000)]
        for content in cases:
            path.write_bytes(content)
            self.assertIsNone(self.service.cached(berlin), content[:40])
        path.write_bytes(good)
        self.assertIsNotNone(self.service.cached(berlin))

    def test_cache_names_and_pruning(self):
        cache = self.service.cache
        for bad in ("../places", "", ".hidden", "a/b", "x" * 80):
            with self.assertRaises(ValueError):
                cache.path(bad)
        self.assertEqual(cache.path("geo_10.1235_-20.5000").name, "forecast-geo_10.1235_-20.5000.json")
        self.service.forecast(place("berlin"))
        self.service.forecast(place("sydney"))
        cache.prune({"2147714"})
        self.assertEqual(sorted(path.name for path in self.cache_dir.iterdir()), ["forecast-2147714.json"])
        self.assertIsNone(WeatherService(fetch=self.network).cached(place("berlin")))

    def test_the_fixture_answers_are_valid_json(self):
        for name in ("forecast-berlin.json", "forecast-sydney.json", "search-berlin.json", "search-sydney.json",
                     "search-nothing.json"):
            self.assertIsInstance(json.loads(raw(name)), dict, name)
        self.assertIsInstance(model.parse_forecast(answer("forecast-sydney.json")), model.Forecast)


if __name__ == "__main__":
    unittest.main()
