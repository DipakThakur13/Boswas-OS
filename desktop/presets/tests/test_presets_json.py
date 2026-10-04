"""presets.json: schema, the ten preset ids, and brand rules."""

import unittest

import support

PALETTE_KEYS = {
    "window", "window_alt", "view", "view_alt", "button", "button_alt",
    "header", "header_alt", "header_inactive", "tooltip",
    "complementary", "complementary_alt", "complementary_text", "complementary_muted",
    "selection", "selection_alt", "selection_text",
    "accent", "accent_hover", "accent_text", "text", "muted",
    "link", "visited", "positive", "neutral", "negative",
}
VARIANT_KEYS = {"base", "glow", "glow_opacity", "ambient", "ambient_opacity", "ring", "ring_opacity",
                "accent", "accent_hi", "mark", "mark_opacity", "motif_colors"}
PRESET_KEYS = {"id", "label", "name", "description", "variant", "icon_theme", "plasma_style",
               "panel", "palette", "terminal", "wallpaper"}


class PresetsJsonTest(unittest.TestCase):
    def setUp(self):
        self.data = support.load_presets()
        self.presets = self.data["presets"]

    def test_the_ten_presets_in_order(self):
        self.assertEqual([p["id"] for p in self.presets], support.PRESET_IDS)

    def test_horizon_is_the_default(self):
        self.assertEqual(self.data["default"], "horizon")
        self.assertEqual(self.data["schema"], 1)

    def test_keys_and_names(self):
        for p in self.presets:
            with self.subTest(preset=p["id"]):
                self.assertEqual(set(p), PRESET_KEYS)
                self.assertEqual(p["label"].lower(), p["id"])
                self.assertEqual(p["label"], p["label"].capitalize())
                self.assertEqual(p["name"], "Boswas " + p["label"])
                self.assertTrue(10 < len(p["description"]) <= 200, p["description"])

    def test_variant_icons_and_plasma_style(self):
        light = {p["id"] for p in self.presets if p["variant"] == "light"}
        self.assertEqual(light, {"pearl", "classic"})
        for p in self.presets:
            with self.subTest(preset=p["id"]):
                self.assertIn(p["variant"], ("dark", "light"))
                self.assertEqual(p["icon_theme"], "Boswas" if p["variant"] == "dark" else "Boswas-Light")
                self.assertIn(p["plasma_style"], ("default", "breeze-dark", "breeze-light"))

    def test_panels(self):
        for p in self.presets:
            with self.subTest(preset=p["id"]):
                if p["id"] == "classic":
                    self.assertEqual(p["panel"], {"style": "full-width",
                                                  "task_manager": "org.kde.plasma.taskmanager"})
                else:
                    self.assertEqual(p["panel"], {"style": "floating",
                                                  "task_manager": "org.kde.plasma.icontasks"})

    def test_palette_tokens(self):
        for p in self.presets:
            with self.subTest(preset=p["id"]):
                self.assertEqual(set(p["palette"]), PALETTE_KEYS)
                for key, value in p["palette"].items():
                    self.assertRegex(value, support.HEX, f"{p['id']}.{key}")

    def test_accents_are_distinct(self):
        accents = [p["palette"]["accent"] for p in self.presets]
        self.assertEqual(len(set(accents)), len(accents))

    def test_flagship_uses_the_logo_gold(self):
        horizon = self.presets[0]["palette"]
        self.assertEqual(horizon["accent"], "#D9B26E")      # Gold 500
        self.assertEqual(horizon["selection"], "#D9B26E")
        self.assertEqual(horizon["window"], "#111A2B")      # Navy 800

    def test_terminal_colours(self):
        for p in self.presets:
            with self.subTest(preset=p["id"]):
                t = p["terminal"]
                self.assertEqual(set(t), {"background", "foreground", "colors"})
                self.assertEqual(len(t["colors"]), 16)
                for value in [t["background"], t["foreground"], *t["colors"]]:
                    self.assertRegex(value, support.HEX)

    def test_wallpaper_parameters(self):
        for p in self.presets:
            with self.subTest(preset=p["id"]):
                wp = p["wallpaper"]
                self.assertEqual(wp["motif"], p["id"])
                x, y = wp["focus"]
                self.assertTrue(0 < x < 3840 and 0 < y < 2160)
                self.assertTrue(200 <= wp["mark_height"] <= 600, "the mark stays subtle")
                for variant in ("dark", "light"):
                    v = wp[variant]
                    self.assertEqual(set(v), VARIANT_KEYS)
                    self.assertEqual(len(v["base"]), 3)
                    for key in ("glow_opacity", "ambient_opacity", "ring_opacity", "mark_opacity"):
                        self.assertTrue(0.0 <= v[key] <= 0.5, f"{variant}.{key}")
                    self.assertTrue(v["mark"] == "colour" or support.HEX.match(v["mark"]))
                    for value in [*v["base"], v["glow"], v["ambient"], v["ring"], v["accent"],
                                  v["accent_hi"], *v["motif_colors"]]:
                        self.assertRegex(value, support.HEX)

    def test_no_teal_hue_in_ui_palettes(self):
        """Teal was retired as the accent: no UI colour may come close to it."""
        for p in self.presets:
            for key, value in p["palette"].items():
                hue, saturation = support.hue_saturation(value)
                with self.subTest(preset=p["id"], key=key):
                    self.assertFalse(160 <= hue <= 190 and saturation > 0.35, value)


class NoTealTest(unittest.TestCase):
    def test_no_teal_anywhere_in_the_presets(self):
        for path in sorted(support.PRESETS_DIR.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".png":
                continue
            if path.name == "support.py":
                continue  # defines the forbidden values
            text = path.read_text(encoding="utf-8").upper().replace(" ", "")
            for value in support.TEAL:
                with self.subTest(path=str(path.relative_to(support.PRESETS_DIR)), value=value):
                    self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
