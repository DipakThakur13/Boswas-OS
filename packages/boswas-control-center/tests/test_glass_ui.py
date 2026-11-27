"""The lists of applications on Boswas glass: the grey hover (never black), icons that grow as in a dock,
and the same look on every page that lists applications (Qt offscreen platform, real Backend on the fakes).

The rows are hovered the way a real pointer hovers them: the mouse moves go to the window, and Qt sends
the Enter and hover events and sets the hover state of a list's row (``boswas_fluent.testing.hover``).

Run: QT_QPA_PLATFORM=offscreen python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_pages_ui import PYSIDE, PageCase  # noqa: E402

if PYSIDE:
    from PySide6.QtCore import QPoint, Qt

    import boswas_fluent as fluent
    from boswas_fluent import testing
    from boswas_fluent.tokens import blend, luminance

    from boswas_control_center.ui.applications import APP, KIND


def close(colour, expected: str, tolerance: int = 3) -> bool:
    want = (int(expected[1:3], 16), int(expected[3:5], 16), int(expected[5:7], 16))
    return all(abs(a - b) <= tolerance for a, b in zip((colour.red(), colour.green(), colour.blue()), want))


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class GlassCase(PageCase):
    def setUp(self):
        super().setUp()
        manager = fluent.manager()
        saved = (manager._forced_glass, manager._forced_scheme)
        self.addCleanup(lambda: (manager.set_glass(saved[0]), manager.set_scheme(saved[1])))
        manager.set_reduced_motion(True)            # sizes at once; one test turns the animation back on
        self.addCleanup(manager.set_reduced_motion, None)

    def look(self, scheme: str, glass: bool) -> None:
        fluent.manager().set_scheme(scheme)
        fluent.manager().set_glass(glass)
        testing.process()

    def shown(self, page: str):
        window = self.window(page)
        window.resize(1100, 720)
        window.show()
        self.settle(window)
        return window

    @staticmethod
    def pixel(window, widget, point: QPoint):
        """The colour at ``point`` of ``widget`` as the window shows it (glass included)."""
        return window.grab().toImage().pixelColor(widget.mapTo(window, point))

    @staticmethod
    def reveal(widget) -> None:
        """Scroll the page so that ``widget`` is in view."""
        from PySide6.QtWidgets import QScrollArea
        parent = widget.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        if parent is not None:
            parent.ensureWidgetVisible(widget, 0, 80)
        testing.process()


class SoftwareCenterTest(GlassCase):
    """Apps > Software Center > Installed: the list where the hover was black."""

    def row_point(self, view, row: int) -> QPoint:
        rect = view.visualRect(view.model().index(row, 0))
        return QPoint(rect.left() + 12, rect.bottom() - 8)          # inside the card, below the icon, left of the text

    def test_a_hovered_row_is_grey_not_black(self):
        for scheme in ("dark", "light"):
            for glass in (False, True):
                self.look(scheme, glass)
                w = self.shown("applications")
                page = w.page("applications")
                view = page.list
                rows = [r for r in range(view.count()) if view.item(r).data(KIND) == APP]
                row = rows[1]
                point = self.row_point(view, row)
                testing.hover(w.sidebar)
                rest = self.pixel(w, view.viewport(), point)
                testing.hover(view.viewport(), point)
                hovered = self.pixel(w, view.viewport(), point)
                what = f"{scheme}, glass {'on' if glass else 'off'}"
                self.assertGreater(luminance(hovered.name()), 0.015, f"{what}: {hovered.name()} is not black")
                layer, alpha = fluent.theme().rgba("fill-subtle-hover")
                self.assertTrue(close(hovered, blend(rest.name(), layer, alpha)),
                                f"{what}: {hovered.name()} is the grey hover layer over the row {rest.name()}")
                if not glass:
                    self.assertTrue(close(rest, fluent.theme().color("card")), f"{what}: the row is a card")
                w.close()

    def test_the_icon_of_the_hovered_row_grows(self):
        self.look("dark", True)
        w = self.shown("applications")
        view = w.page("applications").list
        magnifier = w.page("applications").delegate.magnifier
        rows = magnifier.keys()
        self.assertTrue(all(view.item(r).data(KIND) == APP for r in rows), "headers and notes do not grow")
        testing.hover(view.viewport(), self.row_point(view, rows[1]))
        self.assertEqual(magnifier.scale(rows[1]), 1.4)
        self.assertEqual((magnifier.scale(rows[0]), magnifier.scale(rows[2])), (1.15, 1.15))
        self.assertEqual(magnifier.scale(rows[3]), 1.0)
        image = view.viewport().grab().toImage()
        self.assertFalse(image.isNull())


class InstalledAppsTest(GlassCase):
    """Apps > Installed apps: rows of glass cards with the grey hover and icons that grow."""

    def section_rows(self, w):
        section = w.page("installed-apps").section("installed-apps")
        return section, [section.list.row(key) for key in section.list.keys()]

    def test_a_hovered_row_is_grey_not_black(self):
        for scheme in ("dark", "light"):
            for glass in (False, True):
                self.look(scheme, glass)
                w = self.shown("installed-apps")
                section, rows = self.section_rows(w)
                row = rows[1]
                self.reveal(row)
                point = QPoint(row.width() // 2, row.height() - 8)
                testing.hover(w.sidebar)
                rest = self.pixel(w, row, point)
                testing.hover(row, point)
                self.assertEqual(row.row_state(), "hover")
                hovered = self.pixel(w, row, point)
                what = f"{scheme}, glass {'on' if glass else 'off'}"
                self.assertGreater(luminance(hovered.name()), 0.015, f"{what}: {hovered.name()} is not black")
                layer, alpha = fluent.theme().rgba("fill-subtle-hover")
                self.assertTrue(close(hovered, blend(rest.name(), layer, alpha)),
                                f"{what}: {hovered.name()} is the grey hover layer over {rest.name()}")
                w.close()

    def test_icons_grow_like_a_dock_and_nothing_moves(self):
        self.look("dark", True)
        w = self.shown("installed-apps")
        section, rows = self.section_rows(w)
        magnifier = section.list.magnifier
        self.assertEqual(magnifier.rows(), rows)
        self.assertEqual(rows[0].icon_widget.icon_size(), 24)
        titles = [row.title_label.mapTo(row, QPoint(0, 0)) for row in rows]
        self.reveal(rows[2])
        testing.hover(rows[2])
        self.assertEqual([round(row.icon_widget.scale(), 2) for row in rows[:5]], [1.0, 1.15, 1.4, 1.15, 1.0])
        self.assertEqual([row.title_label.mapTo(row, QPoint(0, 0)) for row in rows], titles, "no text moved")

    def test_the_icon_grows_smoothly(self):
        fluent.manager().set_reduced_motion(False)
        self.look("dark", True)
        w = self.shown("installed-apps")
        section, rows = self.section_rows(w)
        magnifier = section.list.magnifier
        self.reveal(rows[1])
        testing.hover(rows[1])
        sizes = []
        for _ in range(12):
            testing.process(16)
            sizes.append(rows[1].icon_widget.scale())
        self.assertTrue(testing.wait_until(lambda: not magnifier.animating(), 2))
        self.assertEqual(rows[1].icon_widget.scale(), 1.4)
        self.assertEqual(sizes, sorted(sizes), "the icon only grows on its way")
        self.assertTrue(any(1.0 < size < 1.4 for size in sizes), f"it passes through sizes between: {sizes}")

    def test_keyboard_focus_grows_the_icon(self):
        self.look("light", True)
        w = self.shown("installed-apps")
        section, rows = self.section_rows(w)
        kate = next(row for row in rows if row.title() == "Kate")
        testing.hover(w.sidebar)
        kate.action.setFocus(Qt.FocusReason.TabFocusReason)
        testing.process()
        self.assertIs(section.list.magnifier.focused(), kate)
        self.assertEqual(round(kate.icon_widget.scale(), 2), 1.4)


class EveryListOfApplicationsTest(GlassCase):
    """Startup, Default apps, App permissions and Optional features look like Installed apps."""

    def test_the_same_look_everywhere(self):
        self.look("dark", True)
        for page, name in (("installed-apps", "installed-apps"), ("optional-features", "optional-features"),
                           ("startup", "start-up-applications"), ("app-permissions", "app-permissions")):
            w = self.window(page)
            section = self.section(w, page, name)
            self.assertTrue(section.list.applications, page)
            self.assertIsNotNone(section.list.magnifier, page)
            for key in section.list.keys():
                row = section.list.row(key)
                head = getattr(row, "header", None) or row
                self.assertEqual(head.icon_widget.icon_size(), 24, f"{page}: {key}")
                self.assertGreater(head.icon_widget.magnification(), 1.0, f"{page}: {key}")
        w = self.window("default-apps")
        section = self.section(w, "default-apps", "default-applications")
        self.assertEqual(len(section.magnifier.rows()), len(section.cards))
        self.assertTrue(all(card.hover_highlight() for card in section.cards.values()))


if __name__ == "__main__":
    unittest.main()
