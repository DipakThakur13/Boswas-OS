"""NavigationView and FluentWindow (Qt, offscreen)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import support

if support.PYSIDE:
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QLabel, QWidget

    import boswas_fluent as fluent
    from boswas_fluent import testing
    from boswas_fluent.widgets.navigation import KEY_ROLE, ROW_HEIGHT

    Base = testing.FluentTestCase
else:
    Base = unittest.TestCase

PAGES = (("home", "Home"), ("network", "Network"), ("display", "Display"), ("sound", "Sound"))


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class NavigationTest(Base):
    def view(self, **options):
        nav = fluent.NavigationView(**options)
        self.built = []
        for key, title in PAGES:
            nav.add_page(key, title, icon="folder", factory=lambda k=key: self.build(k), tooltip=f"{title} settings",
                         keywords=("wifi", "ethernet") if key == "network" else ())
        nav.add_page("about", "About", QLabel("about page"), footer=True, heading="About this device")
        self.changes = []
        nav.current_changed.connect(self.changes.append)
        self.show(nav, 1000, 600)
        return nav

    def build(self, key: str) -> QWidget:
        self.built.append(key)
        return QLabel(f"{key} page")

    def test_pages_are_built_when_first_shown(self):
        nav = self.view()
        self.assertEqual(nav.keys(), ["home", "network", "display", "sound", "about"])
        self.assertEqual((nav.current(), self.built), ("home", ["home"]))
        nav.set_current("display")
        self.assertEqual((nav.current(), self.built, self.changes), ("display", ["home", "display"], ["display"]))
        self.assertIs(nav.stack.currentWidget(), nav.page("display"))
        self.assertEqual(nav.page("display").text(), "display page")
        nav.set_current("display")
        self.assertEqual(self.changes, ["display"], "selecting the current page again changes nothing")
        self.assertEqual(self.built, ["home", "display"])
        with self.assertRaises(KeyError):
            nav.set_current("nope")
        with self.assertRaises(ValueError):
            nav.add_page("home", "Again")

    def test_the_header_shows_the_heading(self):
        nav = self.view()
        self.assertEqual((nav.title(), nav.title_label.accessibleName()), ("Home", "Home"))
        self.assertEqual(nav.title_label.font().pixelSize(), fluent.theme().type("title").size)
        nav.set_current("about")
        self.assertEqual(nav.title(), "About this device")
        nav.set_title("Custom")
        self.assertEqual(nav.title(), "Custom")
        nav.set_current("home")
        self.assertEqual(nav.title(), "Custom")
        nav.set_title(None)
        self.assertEqual(nav.title(), "Home")
        nav.set_item_text("home", "Start", "Start page")
        self.assertEqual((nav.item("home").text(), nav.title()), ("Start", "Start page"))
        button = nav.add_header_widget(fluent.Button("Refresh"))
        testing.process()
        self.assertTrue(button.isVisibleTo(nav))
        nav.set_header_visible(False)
        self.assertFalse(nav.title_label.isVisibleTo(nav))

    def test_keyboard_moves_through_visible_items(self):
        nav = self.view()
        nav.set_item_visible("network", False)
        self.assertTrue(nav.item("network").isHidden())
        self.assertFalse(nav.is_item_visible("network"))
        nav.list.setFocus()
        testing.key(nav.list, Qt.Key.Key_Down)
        self.assertEqual(nav.current(), "display", "the hidden item is skipped")
        testing.key(nav.list, Qt.Key.Key_End)
        self.assertEqual(nav.current(), "sound")
        testing.key(nav.list, Qt.Key.Key_Down)
        self.assertEqual(nav.current(), "sound", "no wrap")
        testing.key(nav.list, Qt.Key.Key_Home)
        self.assertEqual(nav.current(), "home")
        self.assertEqual(nav.list.accessibleName(), "Navigation")
        self.assertTrue(nav.list.accessibleDescription())
        self.assertEqual(nav.item("home").data(KEY_ROLE), "home")
        self.assertEqual(nav.item("home").toolTip(), "Home settings")

    def test_sections_separators_and_actions_are_never_current(self):
        nav = fluent.NavigationView()
        nav.add_section("Group")
        nav.add_page("one", "One", QLabel("1"))
        nav.add_separator()
        nav.add_page("two", "Two", QLabel("2"))
        nav.add_action("feedback", "Feedback", footer=True)
        invoked = []
        nav.item_invoked.connect(invoked.append)
        self.show(nav, 900, 500)
        self.assertEqual(nav.keys(), ["one", "two", "feedback"])
        self.assertEqual(nav.list.count(), 4)
        nav.list.setFocus()
        testing.key(nav.list, Qt.Key.Key_Down)
        self.assertEqual(nav.current(), "two", "the separator is skipped")
        testing.key(nav.list, Qt.Key.Key_Up)
        testing.key(nav.list, Qt.Key.Key_Up)
        self.assertEqual(nav.current(), "one", "the header is skipped")
        nav.list.setCurrentRow(0)                                  # a header cannot be selected
        self.assertEqual(nav.current(), "one")
        self.assertEqual(nav.list.currentRow(), 1)
        row = nav.footer_list.visualItemRect(nav.footer_list.item(0))
        testing.click(nav.footer_list.viewport(), row.center())
        self.assertEqual((invoked, nav.current()), (["feedback"], "one"))
        self.assertEqual(nav.footer_list.currentRow(), -1, "an action is never selected")
        with self.assertRaises(ValueError):
            nav.set_current("feedback")

    def test_main_and_footer_share_one_selection(self):
        nav = self.view()
        nav.set_current("about")
        self.assertEqual((nav.list.currentRow(), nav.footer_list.currentRow()), (-1, 0))
        self.assertIsNone(nav.list.pill_rect())
        self.assertIsNotNone(nav.footer_list.pill_rect())
        row = nav.list.visualItemRect(nav.item("network"))
        testing.click(nav.list.viewport(), row.center())
        self.assertEqual((nav.current(), nav.list.currentRow(), nav.footer_list.currentRow()), ("network", 1, -1))
        self.assertEqual(nav.footer_list.height(), ROW_HEIGHT)

    def test_a_page_that_holds_the_keyboard_can_be_left_for_a_footer_page(self):
        from PySide6.QtWidgets import QLineEdit
        nav = fluent.NavigationView()
        field = QLineEdit()
        nav.add_page("home", "Home", field)
        nav.add_page("network", "Network", QLabel("network page"))
        nav.add_page("about", "About", QLabel("about page"), footer=True)
        self.show(nav, 1000, 600)
        nav.activateWindow()
        field.setFocus()
        testing.process(20)
        changes = []
        nav.current_changed.connect(changes.append)
        # Hiding the page moves the keyboard focus on; whatever takes it
        # must not select a page of its own.
        nav.set_current("about")
        testing.process(20)
        self.assertEqual((nav.current(), changes), ("about", ["about"]))
        self.assertIs(nav.stack.currentWidget(), nav.page("about"))
        self.assertEqual((nav.list.currentRow(), nav.footer_list.currentRow()), (-1, 0))
        self.assertFalse(nav.page("home").isVisible())
        # The pane lists still work with the keyboard afterwards.
        nav.list.setFocus()
        testing.process(20)
        self.assertEqual(nav.current(), "about", "focusing the list alone changes nothing")
        self.assertIs(nav.stack.currentWidget(), nav.page("about"))
        testing.key(nav.list, Qt.Key.Key_Return)
        self.assertEqual(nav.current(), "home", "Enter chooses the row the keyboard is on")
        self.assertEqual((nav.list.currentRow(), nav.footer_list.currentRow()), (0, -1))
        testing.key(nav.list, Qt.Key.Key_Down)
        self.assertEqual(nav.current(), "network")

    def test_the_selection_pill_slides(self):
        nav = self.view()
        testing.process(20)
        start = nav.list.pill_rect()
        self.assertEqual(start.height(), 16)
        self.assertEqual(start.width(), 3)
        nav.set_current("sound")
        end_row = nav.list.visualItemRect(nav.item("sound"))
        self.assertLess(nav.list.pill_rect().center().y(), end_row.top(), "it does not jump")
        self.assertTrue(testing.wait_until(
            lambda: abs(nav.list.pill_rect().center().y() - end_row.center().y()) <= 1, 2), "it arrives")
        image = nav.list.viewport().grab().toImage()
        self.assertEqual(image.pixelColor(5, end_row.center().y()).name().upper(), fluent.theme().color("accent"))
        fluent.manager().set_reduced_motion(True)
        self.addCleanup(fluent.manager().set_reduced_motion, None)
        nav.set_current("home")
        self.assertAlmostEqual(nav.list.pill_rect().center().y(),
                               nav.list.visualItemRect(nav.item("home")).center().y(), delta=1,
                               msg="reduced motion: it jumps")

    def test_compact_under_the_threshold(self):
        nav = self.view(search=True, back_button=True, pane_toggle=True, compact_below=720)
        fluent.manager().set_reduced_motion(True)
        self.addCleanup(fluent.manager().set_reduced_motion, None)
        self.assertFalse(nav.is_compact())
        self.assertEqual(nav.pane.width(), 280)
        nav.resize(600, 600)
        testing.process()
        self.assertTrue(nav.is_compact())
        self.assertEqual(nav.pane.width(), fluent.NavigationView.COMPACT_WIDTH)
        self.assertTrue(nav.list.compact)
        self.assertFalse(nav.search_box.isVisibleTo(nav))
        self.assertTrue(nav.search_button.isVisibleTo(nav))
        self.assertEqual(nav.search_button.accessibleName(), "Search")
        nav.focus_search()                                         # opens the pane for the search box
        self.assertFalse(nav.is_compact())
        self.assertTrue(nav.search_box.isVisibleTo(nav))
        nav.set_compact(None)
        self.assertTrue(nav.is_compact())
        nav.resize(1000, 600)
        testing.process()
        self.assertFalse(nav.is_compact())
        nav.toggle_button.click()
        self.assertTrue(nav.is_compact(), "the toggle button collapses the pane at any width")
        self.assertTrue(nav.toggle_button.accessibleName())

    def test_search_filters_and_selects(self):
        nav = self.view(search=True)
        queries, submitted = [], []
        nav.search_changed.connect(queries.append)
        nav.search_submitted.connect(submitted.append)
        nav.search_box.setText("wifi")
        visible = [key for key in nav.keys() if not nav.item(key).isHidden()]
        self.assertEqual(visible, ["network", "about"], "keywords are searched; footer items stay")
        self.assertTrue(nav.matches("network", "WIFI net"))
        self.assertFalse(nav.matches("display", "wifi"))
        testing.key(nav.search_box, Qt.Key.Key_Return)
        self.assertEqual((nav.current(), queries, submitted), ("network", ["wifi"], ["wifi"]))
        nav.search_box.setText("")
        self.assertEqual([key for key in nav.keys() if nav.item(key).isHidden()], [])
        nav.set_item_visible("sound", False)
        nav.search_box.setText("s")
        self.assertTrue(nav.item("sound").isHidden(), "a hidden item stays hidden whatever is searched")
        nav.set_search_filters_items(False)
        self.assertFalse(nav.item("home").isHidden())

    def test_back(self):
        nav = self.view(back_button=True)
        requests = []
        nav.back_requested.connect(lambda: requests.append(1))
        self.assertFalse(nav.back_button.isEnabled())
        self.assertEqual(nav.back_button.accessibleName(), "Back")
        nav.set_current("network")
        nav.set_current("sound")
        self.assertTrue(nav.can_go_back())
        self.assertTrue(nav.back_button.isEnabled())
        nav.back_button.click()
        self.assertEqual(nav.current(), "network")
        nav.go_back()
        self.assertEqual(nav.current(), "home")
        self.assertFalse(nav.back_button.isEnabled())
        self.assertEqual(len(requests), 2)

    def test_badges_banners_and_state(self):
        nav = self.view()
        nav.set_item_badge("network", 3)
        nav.set_item_badge("display", 120)
        self.assertEqual((nav.item("network").data(KEY_ROLE + 2), nav.item("display").data(KEY_ROLE + 2)),
                         ("3", "99+"))
        nav.set_item_badge("network", 0)
        self.assertEqual(nav.item("network").data(KEY_ROLE + 2), "")
        nav.set_item_enabled("sound", False)
        nav.list.setFocus()
        nav.set_current("display")
        testing.key(nav.list, Qt.Key.Key_Down)
        self.assertEqual(nav.current(), "display", "a disabled item is skipped")
        bar = nav.add_banner(fluent.InfoBar("info", message="Hello"))
        testing.process()
        self.assertTrue(bar.isVisibleTo(nav))
        self.assertTrue(nav.banners.isVisibleTo(nav))
        bar.dismiss()
        testing.process()
        self.assertFalse(nav.banners.isVisibleTo(nav), "an empty banner area takes no room")
        bar.show_message("error", "Again")
        testing.process()
        self.assertTrue(nav.banners.isVisibleTo(nav))

    def test_painting_in_both_schemes(self):
        nav = self.view(search=True, content_layer=True)
        for scheme in ("dark", "light"):
            testing.set_scheme(scheme)
            image = nav.grab().toImage()
            theme = fluent.theme()
            self.assertEqual(image.pixelColor(140, 590).name().upper(), theme.color("background"), "the pane")
            self.assertEqual(image.pixelColor(600, 400).name().upper(), theme.color("layer"), "the content layer")
            row = nav.list.visualItemRect(nav.item("home"))
            origin = nav.list.viewport().mapTo(nav, QPoint(0, 0))
            self.assertEqual(image.pixelColor(origin.x() + 5, origin.y() + row.center().y()).name().upper(),
                             theme.color("accent"), "the pill")


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class WindowTest(Base):
    def test_window_with_navigation(self):
        window = self.keep(fluent.FluentWindow("com.boswas.Example", "Example", navigation={"search": True}))
        self.assertIsInstance(window.navigation, fluent.NavigationView)
        self.assertIs(window.centralWidget(), window.navigation)
        self.assertIsNotNone(window.navigation.search_box)
        self.assertEqual(window.windowTitle(), "Example")
        self.assertEqual(QGuiApplication.desktopFileName(), "com.boswas.Example")
        other = self.keep(fluent.FluentWindow("com.boswas.Other"))
        self.assertEqual(QGuiApplication.desktopFileName(), "com.boswas.Example", "the first window names the app")
        QGuiApplication.setDesktopFileName("")
        self.assertIsNone(other.navigation)
        label = other.set_content(QLabel("content"))
        self.assertIs(other.centralWidget(), label)
        with self.assertRaises(ValueError):
            fluent.FluentWindow("../evil")

    def test_the_background_is_the_mica_colour(self):
        window = self.show(fluent.FluentWindow(title="Plain"), 300, 200)
        self.assertEqual(window.grab().toImage().pixelColor(5, 5).name().upper(), fluent.theme().color("background"))

    def test_geometry_is_remembered(self):
        state = Path(os.environ["XDG_STATE_HOME"]) / "com.boswas.Example" / "window.ini"
        first = fluent.FluentWindow("com.boswas.Example", "Example", size=(900, 600))
        self.assertFalse(state.exists())
        first.close()                                              # never shown: nothing to remember
        self.assertFalse(state.exists())
        first.show()
        testing.process()
        first.resize(777, 555)
        testing.process()
        first.close()
        first.deleteLater()
        self.assertTrue(state.is_file())
        second = self.keep(fluent.FluentWindow("com.boswas.Example", "Example", size=(900, 600)))
        self.assertEqual((second.width(), second.height()), (900, 600))
        second.show()
        testing.process()
        self.assertEqual((second.width(), second.height()), (777, 555))
        forgetful = self.keep(fluent.FluentWindow("com.boswas.Example", size=(640, 480), remember_geometry=False))
        forgetful.show()
        testing.process()
        self.assertEqual((forgetful.width(), forgetful.height()), (640, 480))
        state.write_text("[window]\ngeometry=garbage\n")
        broken = self.keep(fluent.FluentWindow("com.boswas.Example", size=(800, 500)))
        broken.show()
        testing.process()
        self.assertEqual((broken.width(), broken.height()), (800, 500), "an unreadable state file is ignored")
        QGuiApplication.setDesktopFileName("")

    def test_shortcuts_and_questions(self):
        window = self.show(fluent.FluentWindow(title="Shortcuts"), 400, 300)
        fired = []
        made = window.add_shortcut("refresh", lambda: fired.append("refresh"))
        self.assertTrue(made)
        self.assertEqual(made[0].key().toString(), "F5")
        window.add_shortcut("Ctrl+Shift+K", lambda: fired.append("custom"))[0].activated.emit()
        made[0].activated.emit()
        self.assertEqual(fired, ["custom", "refresh"])

        def answer(primary: bool) -> None:
            dialog = next(d for d in window.findChildren(fluent.ContentDialog) if d.is_open())
            (dialog.primary_button if primary else dialog.close_button).click()

        QTimer.singleShot(20, lambda: answer(True))
        self.assertTrue(window.ask("Quit?", "Unsaved changes are lost.", "Quit"))
        QTimer.singleShot(20, lambda: answer(False))
        self.assertFalse(window.ask("Quit?", "Unsaved changes are lost.", "Quit"))
        QTimer.singleShot(20, lambda: answer(False))
        window.tell("Saved", "All changes were saved.")


if __name__ == "__main__":
    unittest.main()
