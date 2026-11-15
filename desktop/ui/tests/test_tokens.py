"""The Boswas UI token file and the KDE colour schemes generated from it
(standard library only)."""

from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parent.parent
HEX = re.compile(r"^#[0-9A-F]{6}$")


def load_generator():
    spec = importlib.util.spec_from_file_location("build_kde_colors", UI / "tools" / "build_kde_colors.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def luminance(colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class Tokens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = json.loads((UI / "tokens.json").read_text(encoding="utf-8"))
        cls.generator = load_generator()

    def test_palette_is_upper_case_hex_without_the_retired_teal(self):
        colours = []
        for family, value in self.tokens["color"].items():
            colours += list(value.values()) if isinstance(value, dict) else [value]
        self.assertGreater(len(colours), 40)
        for colour in colours:
            self.assertRegex(colour, HEX)
            self.assertNotIn(colour, ("#17C6C0", "#2ED3CD"))

    def test_every_reference_resolves(self):
        for scheme in ("dark", "light"):
            for name, reference in self.tokens["semantic"][scheme].items():
                self.assertRegex(self.generator.resolve(self.tokens, scheme, "@" + name), HEX, f"{scheme}.{name}")
        for hue, parts in self.tokens["icon"]["hues"].items():
            for key in ("from", "to", "glyph", "accent"):
                self.assertRegex(self.generator.resolve(self.tokens, "dark", parts[key]), HEX, f"{hue}.{key}")

    def test_the_specification_values(self):
        """The fixed values of the Boswas UI specification."""
        self.assertEqual(self.tokens["radius"]["window"], 8)
        self.assertEqual(self.tokens["radius"]["control"], 4)
        self.assertEqual(self.tokens["spacing"]["unit"], 4)
        self.assertEqual(self.tokens["type"]["scale"], [12, 14, 16, 20, 28, 40])
        for key in ("fast-ms", "normal-ms", "slow-ms"):
            self.assertTrue(150 <= self.tokens["motion"][key] <= 250, key)
        self.assertLessEqual({"desktop", "window", "flyout", "dialog"}, set(self.tokens["elevation"]))

    def test_the_fluent_geometry(self):
        """Overlays share the window radius; the shell's fixed sizes are whole logical pixels."""
        radius = self.tokens["radius"]
        self.assertEqual(radius["flyout"], radius["window"])
        self.assertEqual(radius["card"], radius["control"])
        shell = self.tokens["shell"]
        self.assertEqual(shell["taskbar-height"], 48)
        self.assertEqual(shell["titlebar-height"], 32)
        self.assertEqual(shell["caption-button"], [46, 32])
        for scheme in ("dark", "light"):
            for group in ("stroke", "fill"):
                values = self.tokens[group][scheme]
                self.assertIn(values["color"], ("white", "black"))
                for key, value in values.items():
                    if key != "color":
                        self.assertTrue(0 < value <= 0.25, f"{group}.{scheme}.{key}")
        for name, material in self.tokens["material"].items():
            if name == "comment":
                continue
            self.assertTrue(0 < material["tint-opacity"] <= 1, name)
            self.assertRegex(self.generator.resolve(self.tokens, "dark", material["tint"]), HEX, name)

    def test_icon_tiles_keep_their_glyphs_readable(self):
        for hue, parts in self.tokens["icon"]["hues"].items():
            glyph = self.generator.resolve(self.tokens, "dark", parts["glyph"])
            for stop in ("from", "to"):
                tile = self.generator.resolve(self.tokens, "dark", parts[stop])
                self.assertGreaterEqual(contrast(glyph, tile), 3.0, f"{hue}: glyph on {stop}")


class KdeColourSchemes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = load_generator()
        cls.tokens = cls.generator.load_tokens()

    def test_committed_schemes_are_current(self):
        self.assertEqual(self.generator.main(["--check"]), 0)

    def test_text_meets_wcag_aa_on_every_background(self):
        for scheme in ("dark", "light"):
            for group, colours in self.generator.groups(self.tokens, scheme).items():
                if not group.startswith("Colors:"):
                    continue
                for background in ("BackgroundNormal", "BackgroundAlternate"):
                    for role in ("ForegroundNormal", "ForegroundInactive", "ForegroundLink", "ForegroundVisited",
                                 "ForegroundNegative", "ForegroundNeutral", "ForegroundPositive"):
                        if group == "Colors:Selection" and role != "ForegroundNormal":
                            continue  # status colours on a selection are decorative
                        ratio = contrast(colours[role], colours[background])
                        self.assertGreaterEqual(ratio, 4.5, f"{scheme} {group} {role} on {background}: {ratio:.2f}")
                    self.assertGreaterEqual(contrast(colours["DecorationFocus"], colours[background]), 3.0,
                                            f"{scheme} {group} focus on {background}")

    def test_terminal_colours_are_readable(self):
        for scheme in ("dark", "light"):
            colours = self.generator.terminal_colours(self.tokens, scheme)
            background = colours["Background"]
            self.assertGreaterEqual(contrast(colours["Foreground"], background), 7.0, scheme)
            # Colour 0 is "black": a background shade, not text.
            for index in range(1, 8):
                for suffix in ("", "Intense"):
                    name = f"Color{index}{suffix}"
                    self.assertGreaterEqual(contrast(colours[name], background), 4.5, f"{scheme} {name}")

    def test_terminal_profiles_use_their_scheme(self):
        for scheme, (file_id, name) in self.generator.SCHEMES.items():
            profile = (UI / "konsole" / f"{name}.profile").read_text(encoding="utf-8")
            self.assertIn(f"ColorScheme={file_id}\n", profile)
            self.assertIn(f"Name={name}\n", profile)
            self.assertTrue((UI / "konsole" / f"{file_id}.colorscheme").is_file())

    def test_window_titles_are_readable(self):
        for scheme in ("dark", "light"):
            wm = self.generator.groups(self.tokens, scheme)["WM"]
            self.assertGreaterEqual(contrast(wm["activeForeground"], wm["activeBackground"]), 7.0)
            self.assertGreaterEqual(contrast(wm["inactiveForeground"], wm["inactiveBackground"]), 4.5)

    def test_schemes_have_every_group_kde_reads(self):
        for file_id, name in self.generator.SCHEMES.values():
            text = (UI / "color-schemes" / f"{file_id}.colors").read_text(encoding="utf-8")
            for group in ("Colors:Button", "Colors:Complementary", "Colors:Header", "Colors:Header][Inactive",
                          "Colors:Selection", "Colors:Tooltip", "Colors:View", "Colors:Window", "General", "KDE",
                          "WM", "ColorEffects:Disabled", "ColorEffects:Inactive"):
                self.assertIn(f"[{group}]", text, f"{file_id}: {group}")
            self.assertIn(f"ColorScheme={file_id}\n", text)
            self.assertIn(f"Name={name}\n", text)
            self.assertNotIn("\r", text)
            self.assertNotRegex(text, r"(?i)23,198,192|46,211,205", "no retired teal")


if __name__ == "__main__":
    unittest.main()
