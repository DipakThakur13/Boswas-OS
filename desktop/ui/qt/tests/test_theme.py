"""theming.py, polish.py, icons.py: applying the theme, following the desktop, focus ring, icons (Qt, offscreen)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import support

if support.PYSIDE:
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QColor, QIcon, QPalette
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QLabel, QLineEdit, QListWidget, QMenu,
                                   QPushButton, QSpinBox, QTableWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                                   QWidget)

    import boswas_fluent as fluent
    from boswas_fluent import assets, icons, polish, testing, theming

    Base = testing.FluentTestCase
else:
    Base = unittest.TestCase


def colours_of(image) -> set[str]:
    """The distinct colours of the fully opaque pixels of an image (as #rrggbb)."""
    found = set()
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() == 255:
                found.add(pixel.name())
    return found


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class ApplyTest(Base):
    def test_apply_styles_the_application(self):
        theme = fluent.theme()
        manager = fluent.manager()
        self.assertTrue(manager.applied)
        self.assertEqual(manager.scheme, "dark")
        palette = self.app.palette()
        self.assertEqual(palette.color(QPalette.ColorRole.Window).name().upper(), theme.color("background"))
        self.assertEqual(palette.color(QPalette.ColorRole.WindowText).name().upper(), theme.color("text"))
        self.assertEqual(palette.color(QPalette.ColorRole.Highlight).name().upper(), theme.color("accent"))
        self.assertEqual(palette.color(QPalette.ColorRole.HighlightedText).name().upper(), theme.color("on-accent"))
        self.assertEqual(palette.color(QPalette.ColorRole.PlaceholderText).name().upper(), theme.color("text-muted"))
        sheet = self.app.styleSheet()
        self.assertIn("QPushButton", sheet)
        self.assertNotIn("@", sheet)
        self.assertEqual(self.app.font().pixelSize(), theme.type("body").size)
        self.assertEqual(self.app.font().families(), list(theme.families))
        self.assertEqual(QIcon.themeName(), "Boswas-Prism")
        self.assertEqual(QIcon.fallbackThemeName(), "breeze-dark")
        self.assertIn(self.app.style().metaObject().className(), ("QStyleSheetStyle",))

    def test_fonts_follow_the_type_ramp(self):
        for name in ("caption", "body", "body-strong", "subtitle", "title", "title-large"):
            step = fluent.theme().type(name)
            font = fluent.font(name)
            self.assertEqual(font.pixelSize(), step.size)
            self.assertEqual(int(font.weight()), step.weight)
        self.assertIn(fluent.manager().font_family(), fluent.theme().families)

    def test_images_are_written_once_to_the_cache(self):
        directory = assets.ensure(fluent.theme())
        self.assertTrue((directory / ".complete").is_file())
        for name in assets.sources(fluent.theme()):
            for suffix in (".svg", ".png", "@2x.png"):
                self.assertGreater((directory / f"{name}{suffix}").stat().st_size, 0, name + suffix)
        self.assertIn(str(directory), self.app.styleSheet())
        stamp = (directory / "check.png").stat().st_mtime_ns
        self.assertEqual(assets.ensure(fluent.theme()), directory)
        self.assertEqual((directory / "check.png").stat().st_mtime_ns, stamp)

    def test_images_fall_back_to_a_private_directory(self):
        blocked = self.tmp / "file-not-directory"
        blocked.write_text("x")
        usual = assets.cache_root()
        self.addCleanup(assets.set_cache_root, usual)
        assets.set_cache_root(None)
        os.environ["XDG_CACHE_HOME"] = str(blocked)
        self.assertEqual(assets.cache_root(), blocked / "boswas-fluent")
        directory = assets.ensure(fluent.theme())
        self.assertTrue((directory / "check.png").is_file())
        self.assertNotIn(str(blocked), str(directory))

    def test_a_scheme_switch_restyles_open_windows(self):
        window = QWidget()
        layout = QVBoxLayout(window)
        button = QPushButton("Button", window)
        switch = fluent.ToggleSwitch("Switch", window, checked=True)
        layout.addWidget(button)
        layout.addWidget(switch)
        self.show(window, 240, 160)
        changes = []
        fluent.manager().changed.connect(changes.append)
        self.addCleanup(fluent.manager().changed.disconnect, changes.append)
        dark = fluent.theme()
        dark_sheet = self.app.styleSheet()
        before = window.grab().toImage()
        self.assertEqual(before.pixelColor(2, 2).name().upper(), dark.color("background"))
        self.assertIn(dark.color("accent").lower(), colours_of(switch.grab().toImage()))

        testing.set_scheme("light")
        light = fluent.theme()
        self.assertEqual(light.scheme, "light")
        self.assertEqual([theme.scheme for theme in changes], ["light"])
        self.assertNotEqual(self.app.styleSheet(), dark_sheet)
        self.assertEqual(window.palette().color(QPalette.ColorRole.Window).name().upper(), light.color("background"))
        after = window.grab().toImage()
        self.assertEqual(after.pixelColor(2, 2).name().upper(), light.color("background"))
        self.assertIn(light.color("accent").lower(), colours_of(switch.grab().toImage()))
        self.assertEqual(button.palette().color(QPalette.ColorRole.ButtonText).name().upper(), light.color("text"))
        self.assertEqual(QIcon.themeName(), "Boswas-Prism-Light")

    def test_the_desktop_file_is_followed_live(self):
        config = Path(os.environ["XDG_CONFIG_HOME"])
        manager = fluent.manager()
        manager.set_scheme(None)
        manager.apply(self.app, watch=True)
        manager.reload()
        self.assertEqual(manager.scheme, "dark")
        self.assertTrue(manager.follows_desktop)
        window = self.show(QLabel("text"), 120, 40)
        (config / "boswas").mkdir()
        target = config / "boswas" / "appearance.toml"
        target.write_text('[appearance]\nscheme = "light"\n')
        self.assertTrue(testing.wait_until(lambda: manager.scheme == "light"), "the new file was not noticed")
        self.assertEqual(window.grab().toImage().pixelColor(1, 1).name().upper(), fluent.theme().color("background"))
        # boswas-preset replaces the file as a whole (rename): the watch must survive that.
        replacement = config / "boswas" / "appearance.toml.new"
        replacement.write_text('[appearance]\nscheme = "dark"\nreduced_motion = true\n')
        os.replace(replacement, target)
        self.assertTrue(testing.wait_until(lambda: manager.scheme == "dark"), "the replaced file was not noticed")
        self.assertTrue(manager.reduced_motion)
        self.assertEqual(fluent.duration("normal"), 0)
        target.unlink()
        self.assertTrue(testing.wait_until(lambda: not manager.reduced_motion), "the removal was not noticed")
        self.assertEqual(fluent.duration("normal"), fluent.theme().duration("normal"))

    def test_reduced_motion_and_fixed_scheme(self):
        manager = fluent.manager()
        manager.set_reduced_motion(True)
        self.addCleanup(manager.set_reduced_motion, None)
        self.assertEqual((fluent.duration("fast"), fluent.duration("slow")), (0, 0))
        manager.set_reduced_motion(False)
        self.assertEqual(fluent.duration("fast"), 150)
        with self.assertRaises(fluent.TokenError):
            manager.set_scheme("sepia")
        self.assertFalse(manager.follows_desktop)                 # the tests fix the scheme
        curve = fluent.easing("decelerate")
        self.assertAlmostEqual(curve.valueForProgress(1.0), 1.0, places=3)
        self.assertGreater(curve.valueForProgress(0.3), 0.5)      # fast first, then slow


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class PolishTest(Base):
    def test_item_views_get_fluent_rows(self):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        plain = QListWidget()
        plain.addItems(["one", "two", "three"])
        plain.setCurrentRow(1)                                    # lays the list out before it is styled
        tree = QTreeWidget()
        tree.setColumnCount(2)
        for name in ("a", "b"):
            tree.addTopLevelItem(QTreeWidgetItem([name, "x"]))
        table = QTableWidget(3, 2)
        for view in (plain, tree, table):
            layout.addWidget(view)
        self.show(holder, 400, 600)
        testing.process(20)
        self.assertEqual(plain.visualItemRect(plain.item(1)).top() - plain.visualItemRect(plain.item(0)).top(), 36)
        self.assertEqual(tree.visualItemRect(tree.topLevelItem(1)).top(), 32)
        self.assertEqual(table.rowHeight(0), 36)
        self.assertFalse(table.horizontalHeader().highlightSections())
        self.assertIsInstance(tree.itemDelegate(), polish.TreeItemDelegate)

    def test_a_tree_row_shows_one_pill(self):
        tree = QTreeWidget()
        tree.setColumnCount(2)
        top = QTreeWidgetItem(["parent", "x"])
        top.addChild(QTreeWidgetItem(["child", "y"]))
        tree.addTopLevelItem(top)
        top.setExpanded(True)
        tree.setCurrentItem(top.child(0))
        self.show(tree, 320, 160)
        testing.process(20)
        image = tree.viewport().grab().toImage()
        accent = QColor(fluent.theme().color("accent"))
        row = tree.visualItemRect(top.child(0))
        columns = [x for x in range(image.width())
                   if any(image.pixelColor(x, y) == accent for y in range(row.top(), row.bottom()))]
        self.assertTrue(columns, "no selection pill")
        self.assertLessEqual(max(columns) - min(columns), 3, "more than one pill in the row")
        self.assertLess(max(columns), 6, "the pill is not at the start of the row")

    def test_popups_get_round_corners(self):
        window = self.show(QWidget(), 200, 100)
        menu = QMenu(window)
        menu.addAction("One")
        menu.popup(window.mapToGlobal(window.rect().center()))
        testing.process(20)
        self.assertTrue(menu.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(menu.windowFlags() & Qt.WindowType.NoDropShadowWindowHint)
        corner = menu.grab().toImage().pixelColor(0, 0)
        self.assertEqual(corner.alpha(), 0, "the corner of a menu is transparent")
        menu.hide()
        combo = QComboBox(window)
        combo.addItems(["a", "b"])
        combo.show()
        combo.showPopup()
        testing.process(20)
        self.assertTrue(combo.view().window().testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        combo.hidePopup()

    def test_spin_boxes_are_as_high_as_text_boxes_and_inset(self):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        spin, edit, combo, button = QSpinBox(), QLineEdit(), QComboBox(), QPushButton("x")
        for widget in (spin, edit, combo, button):
            layout.addWidget(widget)
        self.show(holder, 240, 240)
        self.assertEqual({spin.height(), edit.height(), combo.height(), button.height()}, {32})
        self.assertEqual(spin.findChild(QLineEdit).textMargins().left(), 8)

    def test_the_focus_ring_follows_the_keyboard_only(self):
        window = QWidget()
        layout = QVBoxLayout(window)
        layout.setContentsMargins(20, 20, 20, 20)
        first, second, text = QPushButton("First"), QCheckBox("Second"), QLineEdit()
        for widget in (first, second, text):
            layout.addWidget(widget)
        self.show(window, 240, 200)
        window.activateWindow()
        polisher = fluent.manager()._polisher
        ring = polisher.ring
        first.setFocus(Qt.FocusReason.OtherFocusReason)
        testing.process(10)
        self.assertFalse(ring.isVisible(), "focus set by the program draws no ring")

        testing.key(first, Qt.Key.Key_Tab)
        testing.process(20)
        focused = QApplication.focusWidget()
        if focused is None:
            self.skipTest("this platform gives no keyboard focus to hidden windows")
        self.assertIs(focused, second)
        self.assertTrue(ring.isVisible())
        self.assertIs(ring.target, second)
        self.assertEqual(ring.geometry(), second.geometry().adjusted(-3, -3, 3, 3))
        image = ring.grab().toImage()
        self.assertEqual(image.pixelColor(image.width() // 2, 0).name().upper(), fluent.theme().color("focus"))
        self.assertEqual(image.pixelColor(image.width() // 2, 1).name().upper(), fluent.theme().color("focus"))
        self.assertEqual(image.pixelColor(image.width() // 2, 2).name().upper(), fluent.theme().color("background"))
        self.assertEqual(image.pixelColor(image.width() // 2, image.height() // 2).alpha(), 0)

        testing.key(second, Qt.Key.Key_Tab)                       # a text box shows focus with its underline
        testing.process(20)
        self.assertIs(QApplication.focusWidget(), text)
        self.assertFalse(ring.isVisible())
        testing.key(text, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        testing.process(20)
        self.assertTrue(ring.isVisible())
        testing.click(first)                                      # the pointer takes over: no ring
        testing.process(20)
        self.assertFalse(ring.isVisible())

    def test_ring_rules(self):
        window = self.show(QWidget(), 100, 100)
        self.assertFalse(polish.wants_ring(window))
        self.assertFalse(polish.wants_ring(QLineEdit(window)))
        self.assertFalse(polish.wants_ring(QSpinBox(window)))
        self.assertTrue(polish.wants_ring(QPushButton(window)))
        quiet = QPushButton(window)
        quiet.setProperty("fluentFocusRing", False)
        self.assertFalse(polish.wants_ring(quiet))
        views = QListWidget(window)
        views.addItems(["a", "b"])
        views.setCurrentRow(1)
        views.show()
        testing.process(10)
        host, rect = polish.focus_rect(views)
        self.assertIs(host, views.viewport())
        self.assertEqual(rect.adjusted(-3, -3, 3, 3), views.visualItemRect(views.item(1)))


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class IconTest(Base):
    def render(self, icon, size=32, mode=None):
        mode = QIcon.Mode.Normal if mode is None else mode
        return icon.pixmap(QSize(size, size), 1.0, mode, QIcon.State.Off).toImage()

    def test_names_resolve_in_boswas_prism(self):
        prism = support.REPO / "desktop" / "icons" / "Boswas-Prism" / "scalable"
        self.assertEqual(icons.icon_path("go-next-symbolic"), prism / "actions" / "go-next-symbolic.svg")
        self.assertEqual(icons.icon_path("folder"), prism / "places" / "folder.svg")
        self.assertEqual(icons.icon_path("boswas-control-center"), prism / "apps" / "boswas-control-center.svg")
        self.assertFalse(fluent.icon("go-next").isNull(), "a name without -symbolic finds the symbolic icon")
        self.assertFalse(fluent.icon("no-such-icon", "also-missing", "folder").isNull(), "fallback names")
        self.assertTrue(fluent.has_icon("dialog-warning"))
        self.assertIs(fluent.icon("folder"), fluent.icon("folder"), "icons are cached")

    def test_unknown_and_unsafe_names(self):
        self.assertTrue(fluent.icon("no-such-icon-anywhere").isNull())
        self.assertIn("no-such-icon-anywhere", fluent.missing_icons())
        self.assertFalse(fluent.has_icon("no-such-icon-anywhere"))
        for name in ("../../../etc/passwd", "a/b", "", "x" * 200, "..", ".hidden", "name\n", None):
            self.assertIsNone(icons.icon_path(name), repr(name))
        self.assertTrue(fluent.icon("../../../etc/passwd").isNull())

    def test_symbolic_icons_take_the_text_colour(self):
        icon = fluent.icon("go-next-symbolic")
        dark = fluent.theme().color("text").lower()
        self.assertEqual(colours_of(self.render(icon)), {dark})
        disabled = self.render(icon, mode=QIcon.Mode.Disabled)
        centre = max((disabled.pixelColor(x, 16) for x in range(32)), key=lambda c: c.alpha())
        self.assertLess(centre.alpha(), 128)                      # the text colour at the disabled opacity
        self.assertGreater(centre.alpha(), 40)
        self.assertGreater(centre.lightness(), 240)
        testing.set_scheme("light")
        light = fluent.theme().color("text").lower()
        self.assertNotEqual(light, dark)
        self.assertEqual(colours_of(self.render(icon)), {light}, "the same QIcon follows the scheme")

    def test_a_colour_can_be_named(self):
        accent = fluent.icon("go-next-symbolic", color="accent")
        self.assertEqual(colours_of(self.render(accent)), {fluent.theme().color("accent").lower()})

    def test_coloured_icons_are_left_alone(self):
        folder = self.render(fluent.icon("folder"), 48)
        self.assertGreater(len(colours_of(folder)), 3)
        self.assertNotIn(fluent.theme().color("text").lower(), {folder.pixelColor(24, 30).name()})

    def test_symbolic_preference(self):
        tile = colours_of(self.render(fluent.icon("preferences-system"), 48))
        line = colours_of(self.render(fluent.icon("preferences-system", symbolic=True), 48))
        self.assertGreater(len(tile), 3)
        self.assertEqual(line, {fluent.theme().color("text").lower()})
        # With symbolic=False a name is never answered with its symbolic variant. Whatever other icon
        # themes this machine has installed is left out of the question.
        original = icons._from_theme
        icons._from_theme = lambda _name: QIcon()
        self.addCleanup(setattr, icons, "_from_theme", original)
        icons.clear_cache()
        self.assertTrue(fluent.icon("go-next", symbolic=False).isNull())
        self.assertFalse(fluent.icon("go-next").isNull())

    def test_glyphs_and_pixmaps(self):
        glyph = fluent.glyph_icon("chevron-down", "accent")
        self.assertEqual(colours_of(self.render(glyph, 24)), {fluent.theme().color("accent").lower()})
        pixmap = fluent.pixmap("folder", 24, ratio=2.0)
        self.assertEqual((pixmap.width(), pixmap.devicePixelRatio()), (48, 2.0))


if __name__ == "__main__":
    unittest.main()
