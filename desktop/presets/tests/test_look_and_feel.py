"""Global Theme (look-and-feel) packages: metadata, defaults, layout scripts."""

import json
import re
import unittest

import support

FAVORITES = ["com.boswas.ControlCenter.desktop", "org.kde.dolphin.desktop", "org.kde.konsole.desktop",
             "firefox-esr.desktop", "com.boswas.CompatibilityManager.desktop", "org.kde.kcalc.desktop"]
SYSTEM_APPLICATIONS = ["com.boswas.ControlCenter.desktop", "com.boswas.SecurityCenter.desktop",
                       "org.kde.kinfocenter.desktop"]
PINNED = ["applications:com.boswas.ControlCenter.desktop", "applications:org.kde.dolphin.desktop",
          "applications:org.kde.konsole.desktop", "applications:firefox-esr.desktop",
          "applications:com.boswas.CompatibilityManager.desktop"]
ICON_THEMES = {"Boswas", "Boswas-Light"}   # produced by the Boswas icon theme package

# Plasma 6.3 desktop scripting API used by the layouts (globals, objects' methods
# and properties: see plasma-workspace shell/scripting), plus core JavaScript.
PLASMA_API = {"loadTemplate", "desktopsForActivity", "currentActivity", "panels",
              "addWidget", "remove", "widgets", "writeConfig", "indexOf", "push", "join", "floor"}
PLASMA_PROPERTIES = {"knownWidgetTypes", "widgetIds", "id", "wallpaperPlugin", "currentConfigGroup",
                     "floating", "lengthMode", "height", "length", "gridUnit"}


def lnf_dir(p):
    return support.PRESETS_DIR / "look-and-feel" / f"com.boswas.{p['id']}"


def layout(p) -> str:
    return (lnf_dir(p) / "contents" / "layouts" / "org.kde.plasma.desktop-layout.js").read_text(encoding="utf-8")


def strip_js(source: str) -> str:
    """Remove comments and string literals (enough for these scripts)."""
    source = re.sub(r"//[^\n]*", "", source)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', source)


def js_array(source: str, name: str) -> list:
    match = re.search(r"var %s = (\[.*?\]);" % name, source)
    if not match:
        raise AssertionError(f"{name} not found")
    return json.loads(match.group(1))


class MetadataTest(unittest.TestCase):
    def test_metadata(self):
        for p in support.presets():
            with self.subTest(preset=p["id"]):
                data = json.loads((lnf_dir(p) / "metadata.json").read_text(encoding="utf-8"))
                self.assertEqual(data["KPackageStructure"], "Plasma/LookAndFeel")
                plugin = data["KPlugin"]
                self.assertEqual(plugin["Id"], f"com.boswas.{p['id']}")
                self.assertEqual(plugin["Name"], p["name"])
                self.assertEqual(plugin["Description"], p["description"])
                self.assertEqual(plugin["Authors"], [{"Name": "Boswas Group"}])
                self.assertEqual(plugin["License"], "LicenseRef-Boswas-Internal")


class DefaultsTest(unittest.TestCase):
    def test_defaults_reference_existing_artifacts(self):
        for p in support.presets():
            with self.subTest(preset=p["id"]):
                d = support.parse_ini(lnf_dir(p) / "contents" / "defaults")
                scheme = d["kdeglobals][General"]["ColorScheme"]
                self.assertEqual(scheme, f"Boswas{p['label']}")
                self.assertTrue((support.PRESETS_DIR / "color-schemes" / f"{scheme}.colors").is_file())
                self.assertEqual(d["kdeglobals][KDE"], {"widgetStyle": "Breeze"})
                icons = d["kdeglobals][Icons"]["Theme"]
                self.assertIn(icons, ICON_THEMES)
                self.assertEqual(icons, p["icon_theme"])
                self.assertEqual(d["plasmarc][Theme"]["name"], p["plasma_style"])
                wallpaper = d["Wallpaper"]["Image"]
                self.assertEqual(wallpaper, f"Boswas-{p['label']}")
                meta = json.loads((support.PRESETS_DIR / "wallpapers" / wallpaper / "metadata.json")
                                  .read_text(encoding="utf-8"))
                self.assertEqual(meta["KPlugin"]["Id"], wallpaper)
                self.assertEqual(d["kcminputrc][Mouse"], {"cursorTheme": "breeze_cursors"})
                self.assertEqual(d["kwinrc][org.kde.kdecoration2"], {"library": "org.kde.breeze", "theme": "Breeze"})
                self.assertEqual(d["KSplash"], {"Theme": "com.boswas.splash"})
                self.assertEqual(d["ksplashrc][KSplash"], {"Theme": "com.boswas.splash"})

    def test_old_global_theme_is_gone(self):
        lnf = support.PRESETS_DIR / "look-and-feel"
        self.assertEqual(sorted(d.name for d in lnf.iterdir()),
                         sorted(f"com.boswas.{i}" for i in support.PRESET_IDS))


class LayoutTest(unittest.TestCase):
    def test_starts_from_the_default_panel_and_installs_the_boswas_launcher(self):
        for p in support.presets():
            source = layout(p)
            with self.subTest(preset=p["id"]):
                code = strip_js(source)
                self.assertTrue(code.lstrip().startswith("var "))
                self.assertIn('loadTemplate("org.kde.plasma.desktop.defaultPanel")', source)
                self.assertIn('var BOSWAS_LAUNCHER = "com.boswas.launcher";', source)
                self.assertIn('panel.widgets("org.kde.plasma.kickoff")', source)
                self.assertIn('var LAUNCHER_ICON = "boswas-logo";', source)
                self.assertEqual(js_array(source, "FAVORITES"), FAVORITES)
                self.assertEqual(js_array(source, "SYSTEM_APPLICATIONS"), SYSTEM_APPLICATIONS)
                self.assertEqual(js_array(source, "PINNED_LAUNCHERS"), PINNED)
                self.assertIn('writeGeneral(widget, "favorites", FAVORITES)', source)
                self.assertIn('writeGeneral(widget, "systemApplications", SYSTEM_APPLICATIONS)', source)
                self.assertIn('writeGeneral(widget, "launchers", PINNED_LAUNCHERS)', source)
                self.assertIn('panel.writeConfig("AppletOrder", order.join(";"))', source)

    def test_panel_style(self):
        for p in support.presets():
            source = layout(p)
            classic = p["id"] == "classic"
            with self.subTest(preset=p["id"]):
                self.assertIn(f'var PANEL_FLOATING = {"false" if classic else "true"};', source)
                self.assertIn(f'var PANEL_FULL_WIDTH = {"true" if classic else "false"};', source)
                self.assertIn(f'var TASK_MANAGER = "{p["panel"]["task_manager"]}";', source)
                self.assertIn("panel.floating = PANEL_FLOATING;", source)

    def test_javascript_is_well_formed(self):
        for p in support.presets():
            code = strip_js(layout(p))
            with self.subTest(preset=p["id"]):
                pairs = {")": "(", "]": "[", "}": "{"}
                stack = []
                for ch in code:
                    if ch in "([{":
                        stack.append(ch)
                    elif ch in pairs:
                        self.assertTrue(stack and stack[-1] == pairs[ch], "unbalanced brackets")
                        stack.pop()
                self.assertEqual(stack, [])
                self.assertNotIn("'", code)
                for line in code.splitlines():
                    line = line.strip()
                    if line and not line.endswith(("{", "}", ";", ",", "(", "[")) and not line.startswith(("}", "if", "for", "else")):
                        self.fail(f"statement without semicolon: {line}")

    def test_uses_only_known_api(self):
        for p in support.presets():
            code = strip_js(layout(p))
            defined = set(re.findall(r"function\s+(\w+)", code))
            keywords = {"function", "if", "for", "while", "return"}
            with self.subTest(preset=p["id"]):
                for name in re.findall(r"([A-Za-z_]\w*)\s*\(", code):
                    if name in keywords or name in defined:
                        continue
                    self.assertIn(name, PLASMA_API, f"unknown call {name}()")
                for prop in re.findall(r"\.(\w+)\s*(?:=|;|\.|\))", code):
                    if prop in ("length",):
                        continue
                    self.assertIn(prop, PLASMA_PROPERTIES | PLASMA_API | {"Math"}, f"unknown property .{prop}")


if __name__ == "__main__":
    unittest.main()
