"""Boswas glass: the tokens, when it is on, the backdrop and its caches, legibility, glass surfaces, the grey
hover of rows, acrylic popups, and icons that grow under the pointer, as in a dock."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import support

from boswas_fluent import appearance
from boswas_fluent.tokens import ACCENTS, GLASS_LAYERS, SCHEMES, TEXT_CONTRAST, Theme, blend, contrast, luminance

if support.PYSIDE:
    from PySide6.QtCore import QPoint, QRectF, QSize, Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QComboBox, QMenu, QVBoxLayout, QWidget

    import boswas_fluent as fluent
    from boswas_fluent import glass, icons, paint, qss, testing

    Base = testing.FluentTestCase
else:
    Base = unittest.TestCase


def rgb(colour) -> tuple[int, int, int]:
    return colour.red(), colour.green(), colour.blue()


def hex_rgb(value: str) -> tuple[int, int, int]:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def close(a, b, tolerance: int = 3) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


class GlassTokensTest(unittest.TestCase):
    """The glass of the token file (standard library only)."""

    def test_every_layer_and_level_resolves_in_both_schemes(self):
        for scheme in SCHEMES:
            theme = Theme(scheme)
            self.assertTrue(theme.has_glass)
            for name in GLASS_LAYERS:
                colour, alpha = theme.rgba(name)
                self.assertRegex(colour, r"^#[0-9A-F]{6}$", name)
                self.assertTrue(0 < alpha <= 1, f"{scheme} {name}: {alpha}")
            self.assertGreater(theme.glass("blur"), 0)
            self.assertTrue(1.0 <= theme.glass("saturation") <= 2.0)
            self.assertTrue(0 < theme.glass("noise") <= 0.05, "the grain stays faint")
        # The card is more opaque than the pane, the flyout (which stands over content) most.
        for scheme in SCHEMES:
            theme = Theme(scheme)
            self.assertLess(theme.glass("pane"), theme.glass("card"))
            self.assertLess(theme.glass("card"), theme.glass("flyout"))

    def test_a_token_file_without_glass_has_none(self):
        theme = Theme("dark")
        tokens = dict(theme.tokens)
        tokens["material"] = {key: value for key, value in tokens["material"].items() if key != "glass"}
        plain = Theme("dark", tokens)
        self.assertFalse(plain.has_glass)
        with self.assertRaises(Exception):
            plain.glass("card")

    def test_coloured_text_is_readable_on_the_glass_floor(self):
        """The '*-text' colours keep 4.5:1 down to the darkest (light) or brightest (dark) glass allowed."""
        for scheme in SCHEMES:
            for accent in ACCENTS:
                theme = Theme(scheme, accent=accent)
                floor = theme.glass_floor()
                for name in ("accent-text", "info-text", "success-text", "warning-text", "danger-text",
                             "text", "text-secondary", "text-muted"):
                    self.assertGreaterEqual(contrast(theme.color(name), floor), TEXT_CONTRAST,
                                            f"{scheme} {accent} {name} on {floor}")


class AppearanceTest(unittest.TestCase):
    """What the desktop says about glass (standard library only)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = Path(self.tmp.name) / "boswas"
        self.user.mkdir()

    def read(self) -> appearance.Appearance:
        return appearance.read_appearance(system_files=(), user=self.user)

    def test_defaults(self):
        look = self.read()
        self.assertEqual((look.wallpaper, look.transparency, look.high_contrast), ("", True, False))

    def test_wallpaper_transparency_and_the_high_contrast_preset(self):
        (self.user / "compositor.toml").write_text('[appearance]\ntransparency = false\n'
                                                   'wallpaper = "/usr/share/wallpapers/x.png"\n')
        look = self.read()
        self.assertFalse(look.transparency)
        self.assertEqual(look.wallpaper, "/usr/share/wallpapers/x.png")
        (self.user / "appearance.toml").write_text('[appearance]\nscheme = "dark"\nwallpaper = "/srv/y.jpg"\n')
        self.assertEqual(self.read().wallpaper, "/srv/y.jpg", "the preset's file comes last")
        (self.user.parent / "boswasrc").write_text("[Preset]\nCurrent=carbon\n")
        self.assertTrue(self.read().high_contrast)
        (self.user.parent / "boswasrc").write_text("[Preset]\nCurrent=ocean\n")
        self.assertFalse(self.read().high_contrast)

    def test_bad_values_are_ignored(self):
        (self.user / "compositor.toml").write_text('[appearance]\ntransparency = "no"\nwallpaper = "relative.png"\n')
        look = self.read()
        self.assertTrue(look.transparency)
        self.assertEqual(look.wallpaper, "")
        (self.user.parent / "boswasrc").write_text("not an ini file [[[")
        self.assertFalse(self.read().high_contrast)


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class GlassSwitchTest(Base):
    def test_tests_draw_opaque_unless_they_ask(self):
        self.assertEqual(fluent.manager().glass, testing.GLASS)
        testing.set_glass(True)
        self.assertTrue(fluent.manager().glass)
        self.assertTrue(glass.enabled())
        self.assertIn(fluent.theme().css("glass-card"), self.app.styleSheet())
        testing.set_glass(False)
        self.assertFalse(glass.enabled())
        self.assertNotIn(fluent.theme().css("glass-card"), self.app.styleSheet())

    def test_glass_follows_transparency_and_the_preset(self):
        config = Path(os.environ["XDG_CONFIG_HOME"])
        manager = fluent.manager()
        manager.set_glass(None)
        self.addCleanup(manager.reload)
        (config / "boswas").mkdir(exist_ok=True)
        target = config / "boswas" / "compositor.toml"
        self.addCleanup(lambda: target.unlink(missing_ok=True))
        manager.reload()
        self.assertTrue(manager.glass, "transparency is on by default")
        target.write_text("[appearance]\ntransparency = false\n")
        manager.reload()
        self.assertFalse(manager.glass)
        target.write_text("[appearance]\ntransparency = true\nwallpaper = \"/srv/pictures/w.png\"\n")
        manager.reload()
        self.assertTrue(manager.glass)
        self.assertEqual(manager.wallpaper, "/srv/pictures/w.png", "a new wallpaper is followed")
        record = config / "boswasrc"
        self.addCleanup(lambda: record.unlink(missing_ok=True))
        record.write_text("[Preset]\nCurrent=carbon\n")
        manager.reload()
        self.assertTrue(manager.high_contrast)
        self.assertFalse(manager.glass, "no glass with the high-contrast preset")

    def test_the_sheet_without_glass_is_the_opaque_one(self):
        theme = fluent.theme()
        sheet = qss.build(theme, "/tmp/images")
        self.assertEqual(sheet, qss.build(theme, "/tmp/images", glass=False))
        self.assertNotIn("background: transparent;\n    color: @", sheet)
        for selector in ("QMenu {\n    background: ", "QToolTip {\n    background: "):
            start = sheet.index(selector) + len(selector)
            self.assertTrue(sheet[start:].startswith("#"), f"{selector.strip()} is opaque without glass")
        glassy = qss.build(theme, "/tmp/images", glass=True)
        self.assertIn("QMenu {\n    background: transparent;", glassy)
        self.assertIn(theme.css("glass-highlight"), glassy)


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class BackdropTest(Base):
    def setUp(self):
        super().setUp()
        testing.set_glass(True)
        glass.backdrop().clear()

    def picture(self, colour: str, name: str = "wall.png") -> str:
        image = QImage(320, 180, QImage.Format.Format_RGB32)
        image.fill(QColor(colour))
        painter = QPainter(image)
        painter.fillRect(0, 0, 160, 180, QColor(Theme("dark").color("azure.500")))
        painter.end()
        path = self.tmp / name
        image.save(str(path))
        return str(path)

    def test_made_once_and_kept_while_the_window_is_resized(self):
        store = glass.backdrop()
        theme = fluent.theme()
        screen = QSize(1920, 1080)
        renders, softs = store.renders, store.softs
        first, origin = store.window(theme, "", QSize(800, 600), 1.0, screen)
        self.assertEqual((store.renders, store.softs), (renders + 1, softs + 1))
        self.assertEqual((first.width(), first.height()), (800, 640), "sides rounded up to the bucket")
        self.assertEqual((origin.x(), origin.y()), (0, 20), "the window in the middle of it")
        for width, height in ((801, 600), (790, 620), (640, 480), (700, 500)):
            pixmap, origin = store.window(theme, "", QSize(width, height), 1.0, screen)
            self.assertIs(pixmap, first if width <= 800 else pixmap)
        self.assertEqual(store.renders, renders + 2, "resizing within the backdrop makes no new one")
        store.window(theme, "", QSize(1200, 700), 1.0, screen)
        self.assertEqual((store.renders, store.softs), (renders + 3, softs + 1),
                         "a larger window: a new backdrop from the same blurred picture")
        hi, _origin = store.window(theme, "", QSize(800, 600), 2.0, screen)
        self.assertEqual((hi.width(), hi.height()), (1600, 1280), "device pixels at a scale of 2")
        for width in range(1300, 1920, 170):
            store.window(theme, "", QSize(width, 900), 1.0, screen)
        self.assertLessEqual(len(store._windows), glass.KEPT_BACKDROPS, "only a few are kept")
        whole, origin = store.window(theme, "", QSize(2400, 1400), 1.0, screen)
        self.assertEqual((whole.width(), whole.height(), origin.x()), (2400, 1400, 0),
                         "a window larger than the screen shows all of the picture")

    def test_a_window_shows_a_steady_part_of_the_picture(self):
        """Resizing reveals more of the picture around the same centre; it neither zooms nor moves."""
        store = glass.backdrop()
        theme = fluent.theme()
        screen = QSize(1920, 1080)
        small, at = store.window(theme, "", QSize(600, 400), 1.0, screen)
        large, at_large = store.window(theme, "", QSize(1000, 800), 1.0, screen)
        # The centre of the small window is the centre of the large one.
        a = small.toImage().pixelColor(int(at.x()) + 300, int(at.y()) + 200)
        b = large.toImage().pixelColor(int(at_large.x()) + 500, int(at_large.y()) + 400)
        self.assertTrue(close(rgb(a), rgb(b), 6), f"{a.name()} vs {b.name()}")

    def test_the_wallpaper_is_read_off_the_gui_thread(self):
        path = self.picture(Theme("dark").color("coral.500"))
        store = glass.backdrop()
        theme = fluent.theme()
        screen = QSize(1600, 900)
        self.assertIsNone(store.window(theme, path, QSize(400, 300), 1.0, screen), "nothing until it is read")
        self.assertTrue(store.wait(5))
        found = store.window(theme, path, QSize(1600, 900), 1.0, screen)
        self.assertIsNotNone(found)
        image = found[0].toImage()
        left, right = image.pixelColor(100, 450), image.pixelColor(1500, 450)
        self.assertGreater(left.blue(), right.blue(), "the blue half of the picture shows through on the left")
        self.assertGreater(right.red(), left.red(), "the red half on the right")

    def test_an_unreadable_wallpaper_is_the_default_background(self):
        bad = self.tmp / "bad.png"
        bad.write_bytes(b"not a picture")
        store = glass.backdrop()
        theme = fluent.theme()
        store.window(theme, str(bad), QSize(300, 200), 1.0, QSize(1920, 1080))
        self.assertTrue(store.wait(5))
        self.assertIsNotNone(store.window(theme, str(bad), QSize(300, 200), 1.0, QSize(1920, 1080)))
        missing = store.window(theme, str(self.tmp / "gone.png"), QSize(300, 200), 1.0, QSize(1920, 1080))
        self.assertIsNotNone(missing, "a missing file is the default background at once")

    def test_the_default_background_is_the_shells(self):
        theme = Theme("dark")
        image = glass.default_picture(theme, QSize(96, 54))
        corner = image.pixelColor(95, 0)
        lower_left = image.pixelColor(10, 53)
        self.assertGreater(lower_left.blue() + lower_left.red(), corner.blue() + corner.red(),
                           "the iris glow rises from the lower left")
        self.assertTrue(close(rgb(image.pixelColor(95, 5)), hex_rgb(theme.color("background")), 40))


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class LegibilityTest(Base):
    """Text on the backdrop and the glass on it keeps 4.5:1 whatever the picture."""

    def test_every_picture_gets_enough_tint(self):
        pictures = ["#FFFFFF", "#000000", "#FF0000", "#00FF00", "#0000FF", "#FFFF00", "#808080"]
        for scheme in SCHEMES:
            for accent in ("iris", "sun", "mint"):
                theme = Theme(scheme, accent=accent)
                alpha = glass.legible_opacity(theme, pictures)
                tint, minimum = theme.rgba("glass-backdrop")
                self.assertGreaterEqual(alpha, minimum)
                for picture in pictures:
                    backdrop = blend(picture, tint, alpha)
                    self.assertTrue(glass.legible(theme, backdrop), f"{scheme} {accent} on {picture}")
                    for name, surface in glass.glass_surfaces(theme, backdrop).items():
                        for text in ("text", "text-secondary", "text-muted", "accent-text"):
                            self.assertGreaterEqual(contrast(theme.color(text), surface), TEXT_CONTRAST,
                                                    f"{scheme} {accent} {text} on {name} over {picture}")

    def test_a_calm_picture_keeps_the_token_tint(self):
        for scheme in SCHEMES:
            theme = Theme(scheme)
            tint, minimum = theme.rgba("glass-backdrop")
            self.assertEqual(glass.legible_opacity(theme, [theme.color("background")]), minimum)

    def test_the_default_background_stays_glass(self):
        """The desktop's own background needs at most a little more tint than the token's: glass shows."""
        testing.set_glass(True)
        for scheme in SCHEMES:
            testing.set_scheme(scheme)
            glass.backdrop().clear()
            minimum = fluent.theme().rgba("glass-backdrop")[1]
            self.assertTrue(minimum <= glass.tint_opacity() <= minimum + 0.12, f"{scheme}: {glass.tint_opacity()}")


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class SurfacesTest(Base):
    def card_row(self, count: int = 3):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(4)
        cards = []
        for index in range(count):
            card = fluent.SettingsCard(f"Row {index}", "A description", "folder")
            card.set_hover_highlight(True)
            layout.addWidget(card)
            cards.append(card)
        layout.addStretch(1)
        return holder, cards

    def test_without_glass_a_card_is_its_opaque_colour(self):
        testing.set_glass(False)
        card = self.show(fluent.SettingsCard("Title", "", None), 300, 70)
        colour = card.grab().toImage().pixelColor(150, 60)
        self.assertEqual(colour.name().upper(), fluent.theme().color("card"))

    def test_a_hovered_row_is_the_grey_state_layer_never_black(self):
        for scheme in SCHEMES:
            testing.set_scheme(scheme)
            for on in (False, True):
                testing.set_glass(on)
                holder, cards = self.card_row()
                window = fluent.FluentWindow(size=(420, 300), remember_geometry=False)
                window.set_content(holder)
                self.show(window, 420, 300)
                point = cards[1].mapTo(window, QPoint(200, 58))
                rest = window.grab().toImage().pixelColor(point)
                testing.hover(cards[1])
                self.assertEqual(cards[1].row_state(), "hover")
                hovered = window.grab().toImage().pixelColor(point)
                what = f"{scheme}, glass {'on' if on else 'off'}"
                self.assertGreater(luminance(hovered.name()), 0.01, f"{what}: the hover is not black")
                hover, alpha = fluent.theme().rgba("fill-subtle-hover")
                expected = blend(rest.name(), hover, alpha)
                self.assertTrue(close(rgb(hovered), hex_rgb(expected), 3),
                                f"{what}: {hovered.name()} is the hover layer over {rest.name()} ({expected})")
                if not on:
                    self.assertEqual(rest.name().upper(), fluent.theme().color("card"))
                testing.hover(holder, holder.rect().bottomRight() - QPoint(4, 4))
                self.assertEqual(cards[1].row_state(), "rest")
                window.close()

    def test_glass_shows_the_backdrop_through_a_card(self):
        testing.set_glass(True)
        holder, cards = self.card_row(1)
        window = fluent.FluentWindow(size=(420, 200), remember_geometry=False)
        window.set_content(holder)
        self.show(window, 420, 200)
        image = window.grab().toImage()
        backdrop, origin = glass.window_backdrop(window)
        point = cards[0].mapTo(window, cards[0].rect().center() + QPoint(120, 20))
        card, alpha = fluent.theme().rgba("glass-card")
        below = backdrop.toImage().pixelColor(point + origin.toPoint())
        expected = blend(below.name(), card, alpha)
        self.assertTrue(close(rgb(image.pixelColor(point)), hex_rgb(expected), 4),
                        f"{image.pixelColor(point).name()} is the card glass over the backdrop {below.name()}")

    def test_glass_surfaces_are_drawn_once_per_size(self):
        testing.set_glass(True)
        paint.clear_glass_cache()
        image = QImage(400, 200, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        before = paint.glass_renders
        for _ in range(5):
            paint.glass(painter, QRectF(10, 10, 300, 68), "card")
        paint.glass(painter, QRectF(10, 100, 300, 68), "card", state="hover")
        painter.end()
        self.assertEqual(paint.glass_renders, before + 2, "one per size and state")

    def test_popups_are_acrylic_glass(self):
        testing.set_glass(True)
        window = fluent.FluentWindow(size=(400, 300), remember_geometry=False)
        window.set_content(QWidget())
        self.show(window, 400, 300)
        menu = QMenu(window)
        for text in ("One", "Two", "Three"):
            menu.addAction(text)
        menu.popup(window.mapToGlobal(window.rect().center()))
        testing.process()
        self.assertEqual(menu.property("_fluentGlass"), "popup")
        image = menu.grab().toImage()
        inside = image.pixelColor(image.width() // 2, image.height() - 6)
        self.assertEqual(inside.alpha(), 255, "the glass is opaque: the window behind does not show through")
        flyout, alpha = fluent.theme().rgba("glass-flyout")
        self.assertLess(abs(luminance(inside.name()) - luminance(flyout)), 0.05)
        menu.close()
        combo = QComboBox(window)
        combo.addItems(["A", "B"])
        combo.showPopup()
        testing.process()
        container = combo.view().window()
        self.assertEqual(container.property("_fluentGlass"), "popup")
        combo.hidePopup()

    def test_dialog_windows_show_the_backdrop(self):
        from PySide6.QtWidgets import QDialog
        testing.set_glass(True)
        dialog = self.show(QDialog(), 300, 200)
        self.assertEqual(dialog.property("_fluentGlass"), "window")
        image = dialog.grab().toImage()
        backdrop, origin = glass.window_backdrop(dialog)
        below = backdrop.toImage().pixelColor(150 + int(origin.x()), 100 + int(origin.y()))
        self.assertTrue(close(rgb(image.pixelColor(150, 100)), rgb(below), 2))


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class MagnifierTest(Base):
    def rows(self, count: int = 5):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setSpacing(4)
        cards = [fluent.SettingsCard(f"App {index}", "", "folder") for index in range(count)]
        for card in cards:
            card.icon_widget.set_icon_size(24)
            layout.addWidget(card)
        magnifier = fluent.IconMagnifier(holder)
        magnifier.set_rows(cards)
        self.show(holder, 400, 400)
        return holder, cards, magnifier

    def scales(self, cards) -> list[float]:
        return [round(card.icon_widget.scale(), 3) for card in cards]

    def test_the_hovered_icon_grows_and_its_neighbours_a_little(self):
        fluent.manager().set_reduced_motion(False)
        self.addCleanup(fluent.manager().set_reduced_motion, None)
        holder, cards, magnifier = self.rows()
        titles = [card.title_label.geometry() for card in cards]
        cells = [card.icon_widget.geometry() for card in cards]
        self.assertEqual(cards[0].icon_widget.size(), QSize(34, 34), "room for 1.4 times 24 pixels")
        testing.hover(cards[2])
        self.assertTrue(magnifier.animating(cards[2]))
        testing.process(40)
        middle = cards[2].icon_widget.scale()
        self.assertTrue(1.0 < middle < 1.4, f"on its way: {middle}")
        self.assertTrue(testing.wait_until(lambda: not magnifier.animating(), 2))
        self.assertEqual(self.scales(cards), [1.0, 1.15, 1.4, 1.15, 1.0])
        self.assertEqual([card.title_label.geometry() for card in cards], titles, "no text moved")
        self.assertEqual([card.icon_widget.geometry() for card in cards], cells, "no icon cell moved")
        testing.hover(cards[4])
        self.assertTrue(testing.wait_until(lambda: not magnifier.animating(), 2))
        self.assertEqual(self.scales(cards), [1.0, 1.0, 1.0, 1.15, 1.4])
        testing.hover(holder, holder.rect().bottomRight() - QPoint(2, 2))
        self.assertTrue(testing.wait_until(lambda: magnifier.hovered() is None and not magnifier.animating(), 2))
        self.assertEqual(self.scales(cards), [1.0] * 5)

    def test_reduced_motion_shows_the_size_at_once(self):
        fluent.manager().set_reduced_motion(True)
        self.addCleanup(fluent.manager().set_reduced_motion, None)
        holder, cards, magnifier = self.rows()
        testing.hover(cards[1])
        self.assertFalse(magnifier.animating())
        self.assertEqual(self.scales(cards), [1.15, 1.4, 1.15, 1.0, 1.0])

    def test_keyboard_focus_grows_the_icon_too(self):
        fluent.manager().set_reduced_motion(True)
        self.addCleanup(fluent.manager().set_reduced_motion, None)
        holder, cards, magnifier = self.rows(3)
        button = fluent.Button("Remove")
        cards[2].set_control(button)
        button.setFocus(Qt.FocusReason.TabFocusReason)
        testing.process()
        self.assertIs(magnifier.focused(), cards[2])
        self.assertEqual(self.scales(cards), [1.0, 1.15, 1.4])

    def test_icons_are_drawn_from_kept_pixmaps(self):
        qicon = fluent.icon("folder")
        first = icons.cached_pixmap(qicon, 30, 1.0)
        self.assertIs(icons.cached_pixmap(qicon, 30, 1.0), first)
        self.assertEqual(first.width(), 30)
        self.assertIsNot(icons.cached_pixmap(qicon, 31, 1.0), first)

    def test_an_item_view_magnifies_its_rows(self):
        from PySide6.QtWidgets import QListWidget
        fluent.manager().set_reduced_motion(True)
        self.addCleanup(fluent.manager().set_reduced_motion, None)
        view = QListWidget()
        view.addItems(["header", "a", "b", "c"])
        magnifier = fluent.ViewMagnifier(view, accept=lambda index: index.row() > 0)
        self.show(view, 200, 200)
        self.assertEqual(magnifier.keys(), [1, 2, 3])
        testing.hover(view.viewport(), view.visualItemRect(view.item(2)).center())
        self.assertEqual([magnifier.scale(row) for row in range(4)], [1.0, 1.15, 1.4, 1.15])
        view.clear()
        view.addItems(["header", "x"])
        self.assertEqual(magnifier.keys(), [1])
        self.assertEqual(magnifier.scale(1), 1.0, "new rows start at their size")


if __name__ == "__main__":
    unittest.main()
