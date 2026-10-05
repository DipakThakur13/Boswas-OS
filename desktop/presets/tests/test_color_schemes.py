"""KDE colour schemes: completeness, valid colours, contrast."""

import unittest

import support

COLOR_GROUPS = ("Colors:Button", "Colors:Complementary", "Colors:Header", "Colors:Header][Inactive",
                "Colors:Selection", "Colors:Tooltip", "Colors:View", "Colors:Window")
COLOR_KEYS = {"BackgroundAlternate", "BackgroundNormal", "DecorationFocus", "DecorationHover",
              "ForegroundActive", "ForegroundInactive", "ForegroundLink", "ForegroundNegative",
              "ForegroundNeutral", "ForegroundNormal", "ForegroundPositive", "ForegroundVisited"}
EFFECT_KEYS = {
    "ColorEffects:Disabled": {"Color", "ColorAmount", "ColorEffect", "ContrastAmount", "ContrastEffect",
                              "IntensityAmount", "IntensityEffect"},
    "ColorEffects:Inactive": {"ChangeSelectionColor", "Color", "ColorAmount", "ColorEffect",
                              "ContrastAmount", "ContrastEffect", "Enable", "IntensityAmount",
                              "IntensityEffect"},
}
WM_KEYS = {"activeBackground", "activeBlend", "activeForeground",
           "inactiveBackground", "inactiveBlend", "inactiveForeground"}
STATUS_KEYS = ("ForegroundLink", "ForegroundVisited", "ForegroundNegative", "ForegroundNeutral",
               "ForegroundPositive")


def scheme(p):
    return support.parse_ini(support.PRESETS_DIR / "color-schemes" / f"Boswas{p['label']}.colors")


def text_ratio(p) -> float:
    """WCAG AA (4.5:1) for every preset; Carbon is the high-contrast preset (7:1)."""
    return 7.0 if p["id"] == "carbon" else 4.5


class ColorSchemeTest(unittest.TestCase):
    def test_complete_and_valid(self):
        for p in support.presets():
            with self.subTest(preset=p["id"]):
                s = scheme(p)
                for group in COLOR_GROUPS:
                    self.assertEqual(set(s[group]), COLOR_KEYS, group)
                    for value in s[group].values():
                        support.parse_triplet(value)
                for group, keys in EFFECT_KEYS.items():
                    self.assertEqual(set(s[group]), keys)
                self.assertEqual(set(s["WM"]), WM_KEYS)
                for value in s["WM"].values():
                    support.parse_triplet(value)
                self.assertEqual(s["General"]["ColorScheme"], f"Boswas{p['label']}")
                self.assertEqual(s["General"]["Name"], p["name"])
                self.assertEqual(s["KDE"]["contrast"], "4")
                expected = set(COLOR_GROUPS) | set(EFFECT_KEYS) | {"General", "KDE", "WM"}
                self.assertEqual(set(s), expected)

    def test_scheme_follows_the_palette(self):
        for p in support.presets():
            c = p["palette"]
            s = scheme(p)
            with self.subTest(preset=p["id"]):
                rgb = support.to_rgb
                self.assertEqual(support.parse_triplet(s["Colors:Window"]["BackgroundNormal"]), rgb(c["window"]))
                self.assertEqual(support.parse_triplet(s["Colors:View"]["BackgroundNormal"]), rgb(c["view"]))
                self.assertEqual(support.parse_triplet(s["Colors:Selection"]["BackgroundNormal"]), rgb(c["selection"]))
                self.assertEqual(support.parse_triplet(s["Colors:Window"]["DecorationFocus"]), rgb(c["accent"]))
                self.assertEqual(support.parse_triplet(s["Colors:Header"]["BackgroundNormal"]), rgb(c["header"]))

    def test_variant_matches_the_window_colour(self):
        for p in support.presets():
            with self.subTest(preset=p["id"]):
                window = support.parse_triplet(scheme(p)["Colors:Window"]["BackgroundNormal"])
                if p["variant"] == "dark":
                    self.assertLess(support.luminance(window), 0.05)
                else:
                    self.assertGreater(support.luminance(window), 0.6)

    def test_text_contrast(self):
        for p in support.presets():
            s = scheme(p)
            ratio = text_ratio(p)
            for group in COLOR_GROUPS:
                g = s[group]
                bg = g["BackgroundNormal"]
                with self.subTest(preset=p["id"], group=group):
                    self.assertGreaterEqual(support.contrast(g["ForegroundNormal"], bg), ratio)
                    self.assertGreaterEqual(support.contrast(g["ForegroundInactive"], bg),
                                            3.0 if group == "Colors:Selection" else 4.5)
                    for key in STATUS_KEYS + ("ForegroundActive",):
                        self.assertGreaterEqual(support.contrast(g[key], bg), 4.5, key)

    def test_selection_text_contrast(self):
        for p in support.presets():
            sel = scheme(p)["Colors:Selection"]
            with self.subTest(preset=p["id"]):
                self.assertGreaterEqual(support.contrast(sel["ForegroundNormal"], sel["BackgroundNormal"]),
                                        text_ratio(p))

    def test_dark_text_on_light_selections(self):
        for p in support.presets():
            sel = scheme(p)["Colors:Selection"]
            if support.luminance(sel["BackgroundNormal"]) > 0.3:
                with self.subTest(preset=p["id"]):
                    self.assertLess(support.luminance(sel["ForegroundNormal"]), 0.05)

    def test_focus_indicators_are_visible(self):
        """WCAG 1.4.11: focus decorations reach 3:1 against the window and the view."""
        for p in support.presets():
            s = scheme(p)
            for group in ("Colors:Window", "Colors:View", "Colors:Button"):
                g = s[group]
                with self.subTest(preset=p["id"], group=group):
                    self.assertGreaterEqual(support.contrast(g["DecorationFocus"], g["BackgroundNormal"]),
                                            4.5 if p["id"] == "carbon" else 3.0)

    def test_warnings_stay_distinct_from_the_accent(self):
        for p in support.presets():
            window = scheme(p)["Colors:Window"]
            neutral, accent = window["ForegroundNeutral"], window["DecorationFocus"]
            with self.subTest(preset=p["id"]):
                self.assertNotEqual(neutral, accent)
                self.assertGreaterEqual(support.delta_e(neutral, accent), 20.0)
                self.assertGreaterEqual(support.delta_e(neutral, window["ForegroundNegative"]), 15.0)
                self.assertGreaterEqual(support.delta_e(neutral, window["ForegroundPositive"]), 20.0)
                hue, saturation = support.hue_saturation(neutral)
                self.assertTrue(15 <= hue <= 50 and saturation > 0.5, "warnings are orange or amber")


if __name__ == "__main__":
    unittest.main()
