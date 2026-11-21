"""The files of Weather without Qt: places.json, settings.json, atomic writing, damaged files."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from weather_fixtures import place

from boswas_apps.weather import model, places, settings, storage
from boswas_apps.weather.places import PlaceList


def numbered(count: int) -> list[model.Place]:
    return [model.Place(f"Place {n}", 10.0, float(n % 180), id=n + 1) for n in range(count)]


class FileCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def leftovers(self) -> list[str]:
        return sorted(path.name for path in self.tmp.iterdir() if path.name.endswith(".tmp"))


class PlaceListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.berlin, self.sydney = place("berlin"), place("sydney")
        self.list = PlaceList()

    def test_add_once(self):
        self.assertTrue(self.list.add(self.berlin))
        self.assertFalse(self.list.add(self.berlin))
        self.assertTrue(self.list.add(self.sydney))
        self.assertEqual(self.list.keys(), [self.berlin.key, self.sydney.key])
        self.assertEqual(self.list.get(self.sydney.key), self.sydney)
        self.assertIsNone(self.list.get("nothing"))

    def test_full(self):
        for entry in numbered(places.MAX_PLACES):
            self.assertTrue(self.list.add(entry))
        self.assertTrue(self.list.is_full())
        self.assertFalse(self.list.add(self.berlin))
        self.assertFalse(self.list.insert(0, self.berlin))

    def test_move(self):
        a, b, c = numbered(3)
        for entry in (a, b, c):
            self.list.add(entry)
        self.assertTrue(self.list.move(c.key, -1))
        self.assertEqual(self.list.keys(), [a.key, c.key, b.key])
        self.assertFalse(self.list.move(a.key, -1))
        self.assertTrue(self.list.move(a.key, 5))
        self.assertEqual(self.list.keys(), [c.key, b.key, a.key])
        self.assertFalse(self.list.move("nothing", 1))

    def test_home_and_remove(self):
        self.list.add(self.berlin)
        self.list.add(self.sydney)
        self.assertFalse(self.list.set_home("nothing"))
        self.assertTrue(self.list.set_home(self.sydney.key))
        self.assertEqual(self.list.home_place(), self.sydney)
        self.assertEqual(self.list.remove(self.sydney.key), (1, self.sydney))
        self.assertIsNone(self.list.home)
        self.assertIsNone(self.list.remove(self.sydney.key))
        self.assertTrue(self.list.insert(0, self.sydney))
        self.assertEqual(self.list.keys(), [self.sydney.key, self.berlin.key])
        self.assertTrue(self.list.set_home(None))

    def test_document(self):
        self.list.add(self.berlin)
        self.list.add(self.sydney)
        self.list.set_home(self.sydney.key)
        again = PlaceList.from_json(json.loads(json.dumps(self.list.to_json())))
        self.assertEqual(again, self.list)

    def test_a_document_from_outside(self):
        document = {"version": 1, "home": "missing", "places": [
            self.berlin.to_json(), {"name": "No coordinates"}, self.berlin.to_json(), 42,
            {"id": 9, "name": "Far", "latitude": 0, "longitude": 900}, self.sydney.to_json()]}
        result = PlaceList.from_json(document)
        self.assertEqual(result.keys(), [self.berlin.key, self.sydney.key])
        self.assertIsNone(result.home)
        too_many = {"places": [entry.to_json() for entry in numbered(300)]}
        self.assertEqual(len(PlaceList.from_json(too_many).places), places.MAX_PLACES)
        self.assertEqual(PlaceList.from_json("nonsense"), PlaceList())


class PlacesFileTest(FileCase):
    def test_missing_file(self):
        self.assertEqual(places.load(self.tmp / "places.json"), (PlaceList(), ""))

    def test_save_and_load(self):
        path = self.tmp / "places.json"
        stored = PlaceList([place("berlin"), place("sydney")], place("sydney").key)
        places.save(path, stored)
        self.assertEqual(places.load(path), (stored, ""))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.leftovers(), [])
        document = json.loads(path.read_text())
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["home"], "2147714")

    def test_a_failed_write_keeps_the_old_file(self):
        path = self.tmp / "places.json"
        places.save(path, PlaceList([place("berlin")]))
        before = path.read_bytes()
        with mock.patch("os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                places.save(path, PlaceList([place("sydney")]))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.leftovers(), [])

    def test_damaged_files_are_set_aside(self):
        for content in (b"{ not json", b"\xff\xfe", b'{"places": "Berlin"}', b"[1, 2]",
                        b" " * (places.MAX_BYTES + 1)):
            path = self.tmp / "places.json"
            path.write_bytes(content)
            loaded, problem = places.load(path)
            self.assertEqual(loaded, PlaceList(), content[:20])
            self.assertIn("places.json.damaged", problem)
            self.assertFalse(path.exists())
            self.assertEqual((self.tmp / "places.json.damaged").read_bytes(), content)


class SettingsTest(FileCase):
    def test_defaults_and_choices(self):
        defaults = settings.Settings()
        self.assertEqual((defaults.temperature, defaults.wind, defaults.precipitation, defaults.pressure,
                          defaults.time_format, defaults.refresh_minutes), ("c", "kmh", "mm", "hpa", "locale", 30))
        imperial = settings.Settings.imperial()
        self.assertEqual((imperial.temperature, imperial.wind, imperial.precipitation, imperial.pressure),
                         ("f", "mph", "in", "inhg"))
        raw = {"temperature": "kelvin", "wind": "ms", "precipitation": 3, "pressure": "inhg", "time_format": "12",
               "refresh_minutes": 7}
        parsed = settings.Settings.from_json(raw)
        self.assertEqual((parsed.temperature, parsed.wind, parsed.precipitation, parsed.pressure,
                          parsed.time_format, parsed.refresh_minutes), ("c", "ms", "mm", "inhg", "12", 30))
        self.assertEqual(settings.Settings.from_json({"refresh_minutes": True}).refresh_minutes, 30)
        self.assertEqual(settings.Settings.from_json({"refresh_minutes": 0}).refresh_minutes, 0)
        self.assertEqual(settings.Settings.from_json({"wind": "kn"}, imperial).temperature, "f")

    def test_file(self):
        path = self.tmp / "settings.json"
        self.assertEqual(settings.load(path), (settings.Settings(), False))
        self.assertEqual(settings.load(path, settings.Settings.imperial())[0].temperature, "f")
        chosen = settings.Settings(temperature="f", refresh_minutes=60)
        settings.save(path, chosen)
        self.assertEqual(settings.load(path), (chosen, True))
        path.write_text("{ broken")
        self.assertEqual(settings.load(path)[0], settings.Settings())


class StorageTest(FileCase):
    def test_read_json(self):
        path = self.tmp / "file.json"
        self.assertIsNone(storage.read_json(path, 100))
        path.write_text('{"a": 1}')
        self.assertEqual(storage.read_json(path, 100), {"a": 1})
        with self.assertRaises(storage.StoreError):
            storage.read_json(path, 4)
        path.write_text("[" * 100000)
        with self.assertRaises((storage.StoreError, RecursionError)):
            storage.read_json(path, 10 ** 6)

    def test_atomic_write_and_remove(self):
        path = self.tmp / "deep" / "er" / "file.json"
        storage.write_json_atomic(path, {"text": "Zürich"})
        self.assertEqual(json.loads(path.read_text("utf-8")), {"text": "Zürich"})
        with self.assertRaises(ValueError):
            storage.write_json_atomic(path, {"nan": float("nan")})
        self.assertEqual(json.loads(path.read_text("utf-8")), {"text": "Zürich"})
        self.assertEqual([name for name in os.listdir(path.parent) if name.endswith(".tmp")], [])
        storage.remove(path)
        storage.remove(path)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
