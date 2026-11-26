"""tokens.py and appearance.py: the theme, colour arithmetic, the desktop's appearance (standard library only)."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from boswas_fluent import appearance, tokens
from boswas_fluent.tokens import Theme, TokenError

HEX = re.compile(r"^#[0-9A-F]{6}$")


class ThemeTest(unittest.TestCase):
    def test_the_tokens_of_the_source_tree_are_used(self):
        self.assertEqual(tokens.tokens_path(), support.TOKENS)
        self.assertEqual(Theme().scheme, "dark")                  # dark is the default

    def test_every_colour_resolves_in_both_schemes(self):
        for scheme in tokens.SCHEMES:
            theme = Theme(scheme)
            for name in tokens.COLOUR_NAMES:
                self.assertRegex(theme.color(name), HEX, f"{scheme}.{name}")
            for fill in tokens.FILLS:
                colour, alpha = theme.rgba(f"fill-{fill}")
                self.assertRegex(colour, HEX)
                self.assertTrue(0 < alpha < 1, f"{scheme} fill-{fill}")
            for stroke in tokens.STROKES:
                self.assertTrue(0 < theme.rgba(f"stroke-{stroke}")[1] < 1)
            for name in ("stroke-strong", "text-disabled", "accent-disabled", "accent-subtle", "smoke"):
                self.assertTrue(0 < theme.rgba(name)[1] < 1, name)

    def test_values_come_from_the_file(self):
        data = json.loads(support.TOKENS.read_text(encoding="utf-8"))
        dark, light = Theme("dark"), Theme("light")
        self.assertEqual(dark.color("accent"), data["color"]["iris"]["400"])
        self.assertEqual(light.color("accent"), data["color"]["iris"]["600"])
        self.assertEqual(dark.color("@background"), data["color"]["neutral"]["900"])
        self.assertEqual(dark.color("iris.500"), "#7C5CFF")
        self.assertEqual(dark.color("white"), "#FFFFFF")
        self.assertEqual(dark.rgba("fill-control"), ("#FFFFFF", 0.06))
        self.assertEqual(light.rgba("stroke-surface"), ("#000000", 0.09))
        self.assertEqual(dark.radius("control"), data["radius"]["control"])
        self.assertEqual(dark.radius("flyout"), data["radius"]["flyout"])
        self.assertEqual(dark.spacing(), 4)
        self.assertEqual(dark.spacing(8), 32)
        self.assertEqual(dark.type("subtitle"), tokens.TypeStyle(20, 28, 600))
        self.assertEqual(dark.families[0], "Inter")
        self.assertEqual((dark.duration("fast"), dark.duration("normal"), dark.duration("slow")), (150, 200, 250))
        self.assertEqual(dark.curve("decelerate"), (0.1, 0.9, 0.2, 1.0))
        self.assertEqual(dark.elevation("flyout"), (16, 8, 0.3))
        self.assertEqual(dark.status("error"), dark.color("danger"))

    def test_layers_composite(self):
        theme = Theme("dark")
        self.assertEqual(theme.over("fill-control", "surface"),
                         tokens.blend(theme.color("surface"), "#FFFFFF", 0.06))
        self.assertEqual(theme.css("fill-control"), "rgba(255, 255, 255, 15)")
        self.assertEqual(theme.css("accent"), theme.color("accent"))
        self.assertEqual(theme.color("card"), theme.color("surface"))
        self.assertNotEqual(theme.color("warning-tint"), theme.color("surface"))

    def test_unknown_names_are_errors(self):
        theme = Theme("dark")
        for call in (lambda: theme.color("no-such-role"), lambda: theme.color("iris.123"),
                     lambda: theme.radius("nope"), lambda: theme.type("huge"), lambda: theme.duration("glacial"),
                     lambda: theme.curve("bouncy"), lambda: Theme("sepia"), lambda: theme.color("#12345")):
            with self.assertRaises(TokenError):
                call()

    def test_colour_arithmetic(self):
        self.assertEqual(tokens.blend("#000000", "#FFFFFF", 0.5), "#808080")
        self.assertEqual(tokens.blend("#102030", "#FFFFFF", 0), "#102030")
        self.assertAlmostEqual(tokens.contrast("#000000", "#FFFFFF"), 21.0, places=1)
        self.assertAlmostEqual(tokens.contrast("#777777", "#777777"), 1.0)
        self.assertEqual(tokens.css_rgba("#0a0b0c", 1), "#0A0B0C")
        self.assertEqual(tokens.css_rgba("#000000", 0.5), "rgba(0, 0, 0, 128)")

    def test_readable_colours_move_only_as_far_as_needed(self):
        for scheme in tokens.SCHEMES:
            theme = Theme(scheme)
            for status in ("accent", "info", "success", "warning", "danger"):
                derived, role = theme.color(f"{status}-text"), theme.color(status)
                surfaces = [theme.color(s) for s in ("background", "layer", "surface")]
                if theme.has_glass:                # coloured text stays readable on glass as well
                    surfaces.append(theme.glass_floor())
                if all(tokens.contrast(role, s) >= tokens.TEXT_CONTRAST for s in surfaces):
                    self.assertEqual(derived, role, f"{scheme} {status}: readable roles are not touched")
                for surface in surfaces:
                    self.assertGreaterEqual(tokens.contrast(derived, surface), tokens.TEXT_CONTRAST)


class TokenFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = json.loads(support.TOKENS.read_text(encoding="utf-8"))

    def write(self, data) -> Path:
        path = Path(self.tmp.name) / "tokens.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def test_an_accent_takes_the_shades_of_its_hue(self):
        colours = self.data["color"]
        dark, light = Theme("dark", accent="azure"), Theme("light", accent="azure")
        self.assertEqual(dark.color("accent"), colours["azure"]["400"])
        self.assertEqual(dark.color("accent-hover"), colours["azure"]["300"])
        self.assertEqual(light.color("accent-pressed"), colours["azure"]["700"])
        # sun.600 is too light for white text: it is darkened just enough.
        sun = Theme("light", accent="sun")
        self.assertNotEqual(sun.color("accent"), colours["sun"]["600"])
        self.assertGreaterEqual(tokens.contrast(sun.color("accent"), sun.color("on-accent")), tokens.TEXT_CONTRAST)
        self.assertLess(tokens.contrast(sun.color("accent"), sun.color("on-accent")), tokens.TEXT_CONTRAST + 0.6)
        # Nothing else changes, and iris is the default.
        self.assertEqual(dark.color("background"), Theme("dark").color("background"))
        self.assertEqual(Theme("dark", accent="iris"), Theme("dark"))
        self.assertNotEqual(dark, Theme("dark"))
        self.assertIn("azure", repr(dark))
        with self.assertRaisesRegex(TokenError, "accent"):
            Theme("dark", accent="teal")

    def test_the_text_size_scales_the_type_ramp_only(self):
        normal, large = Theme("dark"), Theme("dark", text_scale=150)
        self.assertEqual(large.type("body").size, round(normal.type("body").size * 1.5))
        self.assertEqual(large.type("title").line_height, round(normal.type("title").line_height * 1.5))
        self.assertEqual(large.type("body").weight, normal.type("body").weight)
        self.assertEqual(large.radius("control"), normal.radius("control"))
        self.assertEqual(large.spacing(4), normal.spacing(4))
        self.assertEqual(Theme("dark", text_scale=225).type("caption").size, 27)
        for bad in (99, 226, 1.5, True, "150"):
            with self.assertRaises(TokenError, msg=bad):
                Theme("dark", text_scale=bad)

    def test_the_environment_variable_overrides(self):
        self.data["semantic"]["dark"]["accent"] = "mint.500"
        path = self.write(self.data)
        os.environ[tokens.TOKENS_ENV] = str(path)
        self.addCleanup(os.environ.pop, tokens.TOKENS_ENV, None)
        self.assertEqual(tokens.tokens_path(), path)
        self.assertEqual(Theme("dark").color("accent"), self.data["color"]["mint"]["500"])

    def test_broken_files_are_refused_with_a_reason(self):
        with self.assertRaisesRegex(TokenError, "cannot read"):
            tokens.load_tokens(Path(self.tmp.name) / "missing.json")
        with self.assertRaisesRegex(TokenError, "not valid JSON"):
            tokens.load_tokens(self.write("{ nope"))
        with self.assertRaisesRegex(TokenError, "top level"):
            tokens.load_tokens(self.write("[]"))
        for key in ("color", "semantic", "fill", "stroke", "radius", "type", "motion"):
            broken = json.loads(json.dumps(self.data))
            del broken[key]
            with self.assertRaisesRegex(TokenError, key):
                tokens.load_tokens(self.write(broken))
        broken = json.loads(json.dumps(self.data))
        broken["semantic"]["light"]["accent"] = "iris.9999"
        with self.assertRaisesRegex(TokenError, "iris.9999"):
            tokens.load_tokens(self.write(broken))
        broken = json.loads(json.dumps(self.data))
        broken["fill"]["dark"]["control"] = 7
        with self.assertRaises(TokenError):
            Theme("dark", broken).rgba("fill-control")


class AppearanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = Path(self.tmp.name) / "boswas"
        self.user.mkdir()

    def read(self, *system: Path) -> appearance.Appearance:
        return appearance.read_appearance(system_files=tuple(system), user=self.user)

    def test_defaults(self):
        self.assertEqual(self.read(), appearance.Appearance("dark", False))

    def test_the_preset_file_decides(self):
        (self.user / "appearance.toml").write_text('[appearance]\nscheme = "light"\nwallpaper = "/x.png"\n')
        self.assertEqual(self.read().scheme, "light")

    def test_layers_override_key_by_key(self):
        system = Path(self.tmp.name) / "system.toml"
        system.write_text('[appearance]\nscheme = "light"\nreduced_motion = true\n[keyboard]\nlayout = "us"\n')
        self.assertEqual(self.read(system), appearance.Appearance("light", True))
        (self.user / "compositor.toml").write_text('[appearance]\nreduced_motion = false\n')
        self.assertEqual(self.read(system), appearance.Appearance("light", False))
        (self.user / "appearance.toml").write_text('[appearance]\nscheme = "dark"\n')
        self.assertEqual(self.read(system), appearance.Appearance("dark", False))

    def test_accent_text_size_and_scroll_bars(self):
        system = Path(self.tmp.name) / "system.toml"
        system.write_text('[appearance]\naccent = "azure"\n[accessibility]\ntext_scale = 150\n')
        self.assertEqual(self.read(system), appearance.Appearance("dark", False, "azure", 150, False))
        (self.user / "compositor.toml").write_text('[appearance]\naccent = "coral"\n'
                                                   '[accessibility]\ntext_scale = 900\nalways_show_scrollbars = true\n')
        self.assertEqual(self.read(system), appearance.Appearance("dark", False, "coral", 225, True),
                         "the user's file wins; the text size is clamped as the compositor clamps it")
        # appearance.toml holds [appearance] only: an [accessibility] table there makes it unusable.
        (self.user / "appearance.toml").write_text('[appearance]\naccent = "mint"\n')
        self.assertEqual(self.read(system).accent, "mint")
        (self.user / "appearance.toml").write_text('[appearance]\naccent = "mint"\n[accessibility]\ntext_scale = 100\n')
        self.assertEqual(self.read(system), appearance.Appearance("dark", False, "coral", 225, True))
        for text in ('[appearance]\naccent = "teal"\n', '[accessibility]\ntext_scale = "big"\n',
                     '[accessibility]\nalways_show_scrollbars = 1\n'):
            (self.user / "compositor.toml").write_text(text)
            self.assertEqual(self.read(system), appearance.Appearance("dark", False, "azure", 150, False), text)

    def test_bad_files_and_values_are_ignored(self):
        target = self.user / "appearance.toml"
        for text in ('[appearance]\nscheme = "sepia"\n', '[appearance]\nscheme = 3\n', "not toml [", "",
                     '[appearance]\nscheme = "light"\n[shell]\ncommand = "x"\n',      # more than the one table
                     '[appearance]\nreduced_motion = "yes"\n'):
            target.write_text(text)
            self.assertEqual(self.read(), appearance.Appearance("dark", False), text)
        target.write_bytes(b"\xff\xfe\x00")
        self.assertEqual(self.read().scheme, "dark")
        target.write_text('[appearance]\nscheme = "light"\n' + "# pad\n" * 60000)
        self.assertEqual(self.read().scheme, "dark")              # larger than the limit

    def test_config_home(self):
        old = os.environ.get("XDG_CONFIG_HOME")
        self.addCleanup(lambda: os.environ.pop("XDG_CONFIG_HOME", None) if old is None
                        else os.environ.__setitem__("XDG_CONFIG_HOME", old))
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        self.assertEqual(appearance.appearance_path(), self.user / "appearance.toml")
        os.environ["XDG_CONFIG_HOME"] = "relative/path"           # not absolute: ignored, as the XDG rules say
        self.assertEqual(appearance.config_home(), Path.home() / ".config")


if __name__ == "__main__":
    unittest.main()
