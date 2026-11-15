"""qss.py and assets.py: the style sheet and its images are generated from the tokens (standard library only)."""

from __future__ import annotations

import re
import unittest
from xml.dom import minidom

import support

from boswas_fluent import assets, qss
from boswas_fluent.tokens import SCHEMES, Theme, TokenError

IMAGES = "/cache/boswas-fluent/assets/x"
# Every standard widget the toolkit promises to style.
STYLED = ("QPushButton", "QToolButton", "QLineEdit", "QTextEdit", "QPlainTextEdit", "QComboBox",
          "QComboBox QAbstractItemView", "QAbstractSpinBox", "QCheckBox", "QRadioButton", "QSlider", "QProgressBar",
          "QScrollBar", "QMenu", "QMenuBar", "QToolTip", "QTabBar", "QTabWidget", "QListView", "QTreeView",
          "QTableView", "QHeaderView", "QGroupBox", "QSplitter", "QStatusBar", "QMessageBox", "QFileDialog",
          "QCalendarWidget", "QDateTimeEdit", "QDockWidget", "QToolBar", "QScrollArea", "QDialogButtonBox")
SUBCONTROLS = ("QCheckBox::indicator:checked", "QCheckBox::indicator:indeterminate", "QRadioButton::indicator:checked",
               "QSlider::handle", "QSlider::sub-page:horizontal", "QProgressBar::chunk", "QScrollBar::handle:vertical",
               "QScrollBar::handle:vertical:hover", "QMenu::item:selected", "QMenu::separator",
               "QMenu::indicator:non-exclusive:checked", "QMenu::right-arrow", "QTabBar::tab:selected",
               "QListView::item:selected", "QTreeView::branch:has-children:closed", "QHeaderView::section",
               "QComboBox::drop-down", "QComboBox::down-arrow", "QAbstractSpinBox::up-button",
               "QPushButton[accent=\"true\"]", "QPushButton[subtle=\"true\"]", "QPushButton[hyperlink=\"true\"]",
               "QPushButton:hover", "QPushButton:pressed", "QPushButton:disabled", "QLineEdit:focus",
               "QDockWidget::title", "QToolBar::separator:horizontal", "QSplitter::handle", "QGroupBox::title")


class StyleSheetTest(unittest.TestCase):
    def test_every_placeholder_resolves_in_both_schemes(self):
        for scheme in SCHEMES:
            theme = Theme(scheme)
            table = qss.values(theme, IMAGES)
            used = qss.placeholders()
            self.assertGreater(len(used), 60)
            self.assertEqual(used - set(table), set(), "placeholders without a value")
            sheet = qss.build(theme, IMAGES)
            self.assertNotIn("@", sheet, "a placeholder was left in the sheet")
            self.assertNotIn("None", sheet)
            self.assertNotIn("{}", sheet.replace(" ", ""))

    def test_an_unknown_placeholder_is_an_error(self):
        with self.assertRaisesRegex(TokenError, "@no-such-token"):
            qss.render("QWidget { color: @no-such-token; }", qss.values(Theme("dark"), IMAGES))

    def test_the_template_holds_no_colour_of_its_own(self):
        self.assertIsNone(re.search(r"#[0-9A-Fa-f]{3,8}\b", qss.TEMPLATE), "a hex colour in the template")
        self.assertIsNone(re.search(r"\brgba?\(", qss.TEMPLATE), "an rgb() colour in the template")
        named = re.findall(r":\s*(white|black|red|green|blue|gray|grey|yellow)\s*;", qss.TEMPLATE)
        self.assertEqual(named, [])

    def test_the_schemes_differ_and_use_the_tokens(self):
        dark, light = Theme("dark"), Theme("light")
        dark_sheet, light_sheet = qss.build(dark, IMAGES), qss.build(light, IMAGES)
        self.assertNotEqual(dark_sheet, light_sheet)
        for theme, sheet in ((dark, dark_sheet), (light, light_sheet)):
            for name in ("accent", "text", "text-secondary", "flyout", "on-accent"):
                self.assertIn(theme.color(name), sheet, name)
            self.assertIn(theme.css("fill-control"), sheet)
            self.assertIn(f"border-radius: {theme.radius('control')}px", sheet)
            self.assertIn(f"border-radius: {theme.radius('flyout')}px", sheet)
            self.assertIn(f"font-size: {theme.type('caption').size}px", sheet)

    def test_it_is_well_formed(self):
        sheet = qss.build(Theme("dark"), IMAGES)
        self.assertEqual(sheet.count("{"), sheet.count("}"))
        depth = 0
        for character in sheet:
            depth += character == "{"
            depth -= character == "}"
            self.assertIn(depth, (0, 1), "nested or unbalanced braces")
        for block in re.findall(r"\{([^{}]*)\}", sheet):
            for declaration in filter(None, (part.strip() for part in block.split(";"))):
                self.assertRegex(declaration, r"^[A-Za-z-]+\s*:\s*\S", declaration)

    def test_every_standard_widget_is_covered(self):
        sheet = qss.build(Theme("dark"), IMAGES)
        selectors = " ".join(re.findall(r"([^{}]+)\{", re.sub(r"/\*.*?\*/", "", sheet, flags=re.S)))
        for widget in STYLED:
            self.assertRegex(selectors, rf"(^|[\s,>]){re.escape(widget)}\b", widget)
        for selector in SUBCONTROLS:
            self.assertIn(selector, selectors)

    def test_every_image_the_sheet_names_is_generated(self):
        for scheme in SCHEMES:
            theme = Theme(scheme)
            sources = assets.sources(theme)
            named = re.findall(r'url\("([^"]+)"\)', qss.build(theme, IMAGES))
            self.assertGreater(len(named), 30)
            for path in named:
                self.assertTrue(path.startswith(IMAGES + "/") and path.endswith(".png"), path)
                self.assertIn(path[len(IMAGES) + 1:-4], sources, path)

    def test_an_image_directory_that_would_break_the_sheet_is_refused(self):
        for directory in ('/tmp/a"b', "/tmp/a\nb", "/tmp/a\\b"):
            with self.assertRaises(TokenError):
                qss.build(Theme("dark"), directory)

    def test_table_layers_are_half_strength(self):
        theme = Theme("dark")
        once = theme.rgba("fill-subtle-hover")[1]
        twice = int(re.search(r"(\d+)\)$", qss.values(theme, IMAGES)["fill-subtle-hover-twice"]).group(1)) / 255
        self.assertAlmostEqual(1 - (1 - twice) ** 2, once, places=2)


class AssetTest(unittest.TestCase):
    def test_images_are_small_valid_svg_in_token_colours(self):
        for scheme in SCHEMES:
            theme = Theme(scheme)
            allowed = {theme.rgba(name)[0] for name in ("text", "text-secondary", "text-disabled", "on-accent",
                                                        "accent", "accent-pressed")}
            for name, svg in assets.sources(theme).items():
                document = minidom.parseString(svg)
                root = document.documentElement
                self.assertEqual(root.tagName, "svg", name)
                self.assertLessEqual(int(root.getAttribute("width")), 24, name)
                colours = set(re.findall(r'(?:fill|stroke)="(#[0-9A-Fa-f]{6})"', svg))
                self.assertTrue(colours, name)
                self.assertTrue(colours <= allowed, f"{scheme} {name}: {colours - allowed}")
                self.assertNotIn("<image", svg)
                self.assertNotIn("href", svg)

    def test_the_digest_follows_the_tokens(self):
        self.assertNotEqual(assets.digest(Theme("dark")), assets.digest(Theme("light")))
        self.assertEqual(assets.digest(Theme("dark")), assets.digest(Theme("dark")))
        self.assertRegex(assets.digest(Theme("dark")), r"^[0-9a-f]{20}$")

    def test_glyphs(self):
        theme = Theme("dark")
        for glyph in assets.GLYPHS:
            self.assertIn(theme.color("text-secondary"), assets.glyph_svg(theme, glyph))
        self.assertIn('width="8"', assets.glyph_svg(theme, "chevron-down", "text", 8))
        with self.assertRaises(KeyError):
            assets.glyph_svg(theme, "no-such-glyph")


class NoHardCodedColourTest(unittest.TestCase):
    """No module of the toolkit holds a colour value: colours come from tokens.json."""

    def test_sources(self):
        literal = re.compile(r"""["']#[0-9A-Fa-f]{3,8}["']|QColor\(\s*\d|\brgba?\(\s*\d|Qt\.GlobalColor\.(?!transparent)""")
        offenders = []
        for path in sorted((support.QT_DIR / "boswas_fluent").rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0] if '"#' not in line and "'#" not in line else line
                if literal.search(code):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
