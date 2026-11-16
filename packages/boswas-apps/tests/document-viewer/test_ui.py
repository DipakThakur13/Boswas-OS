"""The window of Document Viewer, driven like a person drives it (Qt offscreen platform).

File choosers, the print dialog and the browser are replaced through the
window's hooks; printing goes to a PDF file in the test's temporary
directory. Nothing is written outside it.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import pdfgen
from pdfgen import make_pdf

if pdfgen.PYSIDE:
    from boswas_fluent import testing
    from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent, QGuiApplication, QMouseEvent
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication, QPushButton

    from boswas_apps.document_viewer import __main__ as cli
    from boswas_apps.document_viewer import layout as lay
    from boswas_apps.document_viewer import printing
    from boswas_apps.document_viewer.document import NEEDS_PASSWORD, READY
    from boswas_apps.document_viewer.window import ViewerWindow
    BASE = testing.FluentTestCase
else:
    BASE = unittest.TestCase

SHOTS = os.environ.get("BOSWAS_TEST_SHOTS")
SCHEME = "light" if os.environ.get("BOSWAS_TEST_SCHEME") == "light" else "dark"
PACKAGE = pdfgen.HERE.parents[1]
OUTLINE = [("Introduction", 0, []), ("Results", 3, [("Revenue", 4, []), ("Costs", 6, [])]), ("Outlook", 9, [])]
LINKS = [(0, (72, 60, 300, 16), 9), (0, (72, 76, 300, 16), "https://example.org/report?year=2026")]


@unittest.skipUnless(pdfgen.PYSIDE, "PySide6 is not installed")
class WindowCase(BASE):
    scheme = SCHEME

    def setUp(self) -> None:
        super().setUp()
        self._scratch = pdfgen.enter_scratch_directory()
        self.addCleanup(self._leave)
        self.urls: list[str] = []
        self.report = make_pdf(self.tmp / "Annual report.pdf", 12, outline=OUTLINE, links=LINKS,
                               info={"Title": "Annual report 2026", "Author": "Finance"})

    def _leave(self) -> None:
        self.assertEqual(pdfgen.leave_scratch_directory(self._scratch), [],
                         "something was written into the working directory")

    def window(self, *paths) -> "ViewerWindow":
        window = ViewerWindow([str(path) for path in paths])
        window.choose_files = lambda: []
        window.choose_save = lambda _suggestion: None
        window.open_url = lambda url: self.urls.append(url) or True
        window.run_print_dialog = lambda *_args: False
        self.show(window, 1180, 800)
        testing.process(20)
        return window

    def sharp(self, tab, page=None) -> None:
        view = tab.view
        self.assertTrue(testing.wait_until(lambda: view.has_sharp_image(view.current_page() if page is None else page), 10))

    def searched(self, tab) -> None:
        self.assertTrue(testing.wait_until(lambda: not tab.searching and not tab._search_timer.isActive(), 15))
        testing.process(20)

    def shot(self, widget, name: str) -> None:
        if SHOTS:
            testing.grab(widget, Path(SHOTS) / f"{SCHEME}-{name}.png")

    def view_point(self, tab, page: int, x: float, y: float) -> "QPoint":
        rect = tab.view.page_rect(page)
        scale = tab.view.zoom * 96 / 72
        return QPoint(round(rect.left() + x * scale), round(rect.top() + y * scale))


class StartAndTabsTest(WindowCase):
    def test_start_page(self):
        window = self.window()
        self.assertIs(window.stack.currentWidget(), window.start_page)
        self.assertFalse(window.tab_row.isVisible())
        self.assertFalse(window.recent_view.isVisible())
        for action in (window.save_action, window.print_action, window.find_action, window.zoom_in_action,
                       window.presentation_action, window.properties_action, window.close_action):
            self.assertFalse(action.isEnabled(), action.text())
        self.assertTrue(window.open_action.isEnabled())
        self.assertFalse(window.page_box.isEnabled())
        self.shot(window, "start")
        window.choose_files = lambda: [str(self.report)]
        testing.click(window.welcome.action_button)
        self.assertEqual(len(window.tabs), 1)

    def test_open_shows_the_document(self):
        window = self.window(self.report)
        tab = window.current_tab()
        self.assertEqual(tab.state, READY)
        self.assertTrue(window.tab_row.isVisible())
        self.assertEqual(window.tab_bar.tabText(0), "Annual report.pdf")
        self.assertIn("Annual report.pdf", window.windowTitle())
        self.assertEqual(window.page_box.text(), "1")
        self.assertEqual(window.page_total.text(), "/ 12")
        self.assertRegex(window.zoom_box.currentText(), r"^\d+ %$")
        self.assertFalse(window.previous_action.isEnabled())
        self.assertTrue(window.next_action.isEnabled())
        self.assertFalse(window.copy_action.isEnabled())
        self.assertTrue(tab.view.hasFocus())
        self.sharp(tab, 0)
        self.assertTrue(testing.wait_until(lambda: len(tab.sidebar.thumbnail_model.images) >= 3, 10))
        self.shot(window, "document")

    def test_tabs(self):
        other = make_pdf(self.tmp / "Other.pdf", 3)
        window = self.window(self.report, other)
        self.assertEqual([tab.title() for tab in window.tabs], ["Annual report.pdf", "Other.pdf"])
        self.assertEqual(window.tab_bar.currentIndex(), 1)
        self.assertEqual(window.page_total.text(), "/ 3")
        window.open_document(str(self.report))                         # already open: its tab comes forward
        self.assertEqual((len(window.tabs), window.tab_bar.currentIndex()), (2, 0))
        self.assertEqual(window.page_total.text(), "/ 12")
        window.next_tab_action.trigger()
        self.assertEqual(window.tab_bar.currentIndex(), 1)
        window.previous_tab_action.trigger()
        self.assertEqual(window.tab_bar.currentIndex(), 0)
        window.handle_arguments([str(other), "--ignored-option"])      # a second start of the program
        self.assertEqual((len(window.tabs), window.tab_bar.currentIndex()), (2, 1))
        window.tab_bar.moveTab(1, 0)
        self.assertEqual([tab.title() for tab in window.tabs], ["Other.pdf", "Annual report.pdf"])
        window.close_action.trigger()
        self.assertEqual([tab.title() for tab in window.tabs], ["Annual report.pdf"])
        window.tab_bar.tabCloseRequested.emit(0)
        self.assertEqual(window.tabs, [])
        self.assertIs(window.stack.currentWidget(), window.start_page)

    def test_recent_documents_with_pictures_and_remembered_view(self):
        window = self.window(self.report)
        tab = window.current_tab()
        picture = window.thumbnail_path(str(self.report))
        self.assertTrue(testing.wait_until(lambda: os.path.isfile(picture), 10))
        tab.view.set_zoom(1.5)
        tab.view.rotate(1)
        tab.view.go_to_page(7)
        window.close_current_tab()
        self.assertTrue(window.recent_view.isVisible())
        self.assertEqual(window.recent_model.rowCount(), 1)
        index = window.recent_model.index(0)
        self.assertEqual(index.data(), "Annual report.pdf")
        self.assertIn("Annual report 2026", index.data(Qt.ItemDataRole.ToolTipRole))
        self.assertFalse(index.data(Qt.ItemDataRole.DecorationRole).isNull())
        self.shot(window, "start-recent")
        window.recent_view.clicked.emit(index)
        tab = window.current_tab()
        self.assertEqual((tab.view.current_page(), tab.view.zoom, tab.view.rotation, tab.view.zoom_mode),
                         (7, 1.5, 1, lay.CUSTOM))
        window.close()
        again = self.window()
        self.assertEqual(again.store.recent[0]["path"], str(self.report))
        self.assertEqual(again.store.view(str(self.report))["page"], 7)

    def test_drop_opens_documents(self):
        window = self.window()
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile(str(self.report)), QUrl("https://example.org/not-a-file.pdf")])
        window.dropEvent(QDropEvent(QPointF(50, 50), Qt.DropAction.CopyAction, data, Qt.MouseButton.LeftButton,
                                    Qt.KeyboardModifier.NoModifier))
        self.assertEqual([tab.title() for tab in window.tabs], ["Annual report.pdf"])

    def test_damaged_file_shows_an_error_state(self):
        bad = self.tmp / "broken.pdf"
        bad.write_bytes(b"%PDF-1.7\nnothing useful here\n")
        window = self.window(bad)
        tab = window.current_tab()
        self.assertIs(tab.stack.currentWidget(), tab.error_page)
        self.assertFalse(window.save_action.isEnabled())
        self.assertFalse(window.page_box.isEnabled())
        self.assertTrue(window.close_action.isEnabled())
        self.assertEqual(window.store.recent, [])
        self.shot(window, "error")
        bad.write_bytes(self.report.read_bytes())
        testing.click(tab.error_page.action_button)                     # "Try again"
        self.assertEqual(tab.state, READY)
        self.assertTrue(window.save_action.isEnabled())

    def test_password_page(self):
        locked = make_pdf(self.tmp / "locked.pdf", 3, password="s3cret")
        window = self.window(locked)
        tab = window.current_tab()
        self.assertEqual(tab.state, NEEDS_PASSWORD)
        self.assertIs(tab.stack.currentWidget(), tab._password_page)
        self.assertFalse(window.find_action.isEnabled())
        self.assertEqual(tab.password_error.text(), "")
        testing.type_text(tab.password_box, "nope")
        testing.click(tab.unlock_button)
        self.assertIn("not correct", tab.password_error.text())
        self.assertEqual(tab.password_box.text(), "")
        self.shot(window, "password")
        tab.password_box.setText("s3cret")
        testing.key(tab.password_box, Qt.Key.Key_Return)
        self.assertEqual(tab.state, READY)
        self.assertEqual(window.page_total.text(), "/ 3")
        self.assertEqual(window.store.recent[0]["path"], str(locked))


class ReadingTest(WindowCase):
    def test_page_box_and_buttons(self):
        window = self.window(self.report)
        tab = window.current_tab()
        window.next_action.trigger()
        self.assertEqual((tab.view.current_page(), window.page_box.text()), (1, "2"))
        window.go_to_action.trigger()
        self.assertTrue(window.page_box.hasFocus())
        window.page_box.setText("9")
        testing.key(window.page_box, Qt.Key.Key_Return)
        self.assertEqual(tab.view.current_page(), 8)
        self.assertTrue(tab.view.hasFocus())
        self.assertTrue(window.back_action.isEnabled())
        window.back_action.trigger()
        self.assertEqual(tab.view.current_page(), 1)
        window.forward_action.trigger()
        self.assertEqual(tab.view.current_page(), 8)
        window.page_box.setFocus()
        window.page_box.setText("99")
        testing.key(window.page_box, Qt.Key.Key_Return)
        self.assertEqual(tab.view.current_page(), 8)
        self.assertIn("no page “99”", window.status_label.text())
        self.assertEqual(window.page_box.text(), "9")
        window.previous_action.trigger()
        self.assertEqual(window.page_box.text(), "8")

    def test_zoom_box_and_shortcuts(self):
        window = self.window(self.report)
        tab = window.current_tab()
        self.assertEqual(window.fit_page_action.shortcut().toString(), "Ctrl+0")
        self.assertEqual(window.actual_size_action.shortcut().toString(), "Ctrl+1")
        self.assertEqual(window.fit_width_action.shortcut().toString(), "Ctrl+2")
        window.actual_size_action.trigger()
        self.assertEqual((tab.view.zoom, window.zoom_box.currentText()), (1.0, "100 %"))
        window.zoom_in_action.trigger()
        self.assertEqual(window.zoom_box.currentText(), "110 %")
        window.zoom_out_action.trigger()
        window.zoom_out_action.trigger()
        self.assertEqual(window.zoom_box.currentText(), "90 %")
        window.fit_page_action.trigger()
        self.assertEqual(tab.view.zoom_mode, lay.FIT_PAGE)
        window.zoom_box.lineEdit().setFocus()
        window.zoom_box.setEditText("150")
        testing.key(window.zoom_box.lineEdit(), Qt.Key.Key_Return)
        self.assertEqual((tab.view.zoom, tab.view.zoom_mode), (1.5, lay.CUSTOM))
        self.assertEqual(window.zoom_box.currentText(), "150 %")
        window.zoom_box.setEditText("nonsense")
        testing.key(window.zoom_box.lineEdit(), Qt.Key.Key_Return)
        self.assertEqual(window.zoom_box.currentText(), "150 %")
        window.zoom_box.activated.emit(0)                               # "Fit width" from the list
        self.assertEqual(tab.view.zoom_mode, lay.FIT_WIDTH)
        window.set_zoom = None
        tab.view.set_zoom(lay.ZOOM_MAX)
        self.assertFalse(window.zoom_in_action.isEnabled())

    def test_rotation_arrangement_and_tools(self):
        window = self.window(self.report)
        tab = window.current_tab()
        window.rotate_right_action.trigger()
        self.assertEqual(tab.view.rotation, 1)
        window.rotate_left_action.trigger()
        window.rotate_left_action.trigger()
        self.assertEqual(tab.view.rotation, 3)
        window.two_pages_action.trigger()
        self.assertEqual((tab.view.columns, tab.view.cover), (2, False))
        window.cover_action.trigger()
        self.assertEqual((tab.view.columns, tab.view.cover), (2, True))
        self.assertFalse(window.two_pages_action.isChecked())
        window.continuous_action.trigger()
        self.assertFalse(tab.view.continuous)
        window.one_page_action.trigger()
        self.assertEqual((tab.view.columns, tab.view.continuous), (1, False))
        self.assertEqual([action.text() for action in window.view_menu.actions() if action.text()],
                         ["Continuous scrolling", "One page", "Two pages", "Two pages, first page alone"])
        window.hand_tool_action.trigger()
        self.assertEqual(tab.view.tool, "hand")
        self.assertFalse(window.select_tool_action.isChecked())
        window.select_tool_action.trigger()
        self.assertEqual(tab.view.tool, "select")

    def test_sidebar_thumbnails_and_outline(self):
        window = self.window(self.report)
        tab = window.current_tab()
        sidebar = tab.sidebar
        self.assertTrue(sidebar.isVisible())
        self.assertEqual(sidebar.thumbnail_model.rowCount(), 12)
        self.assertTrue(testing.wait_until(lambda: len(sidebar.thumbnail_model.images) >= 3, 10))
        self.assertLess(len(sidebar.thumbnail_model.images), 12)        # only the rows in view were rendered
        sidebar.thumbnails.clicked.emit(sidebar.thumbnail_model.index(5))
        self.assertEqual(tab.view.current_page(), 5)
        self.assertEqual(sidebar.thumbnail_model.current, 5)
        self.assertTrue(window.back_action.isEnabled())
        sidebar.show_tab("outline")
        self.assertIs(sidebar.outline_stack.currentWidget(), sidebar.outline)
        model = sidebar.outline.model()
        results = model.index(1, 0)
        self.assertEqual(results.data(), "Results")
        sidebar.outline.clicked.emit(model.index(1, 0, results))        # "Costs"
        self.assertEqual(tab.view.current_page(), 6)
        self.sharp(tab)
        testing.process(250)
        self.shot(window, "outline")
        window.sidebar_action.trigger()
        self.assertFalse(sidebar.isVisible())
        self.assertFalse(window.store.sidebar)
        window.sidebar_action.trigger()
        self.assertTrue(sidebar.isVisible())
        plain = make_pdf(self.tmp / "plain.pdf", 2)
        other = window.open_document(str(plain))
        self.assertIs(other.sidebar.outline_stack.currentWidget(), other.sidebar.no_outline)
        self.assertEqual(other.sidebar.pivot.current(), "outline")      # the last tab of the sidebar is remembered

    def test_a_thousand_page_document_has_a_virtual_thumbnail_list(self):
        big = make_pdf(self.tmp / "big.pdf", 1000, lines=4)
        window = self.window(big)
        tab = window.current_tab()
        self.assertEqual(window.page_total.text(), "/ 1,000")
        model = tab.sidebar.thumbnail_model
        self.assertEqual(model.rowCount(), 1000)
        tab.view.go_to_page(640)
        self.assertTrue(testing.wait_until(lambda: 640 in model.images, 10))
        self.assertLess(len(model.images), 40)
        self.assertEqual(model.current, 640)


class SearchTest(WindowCase):
    def test_incremental_search_with_options(self):
        window = self.window(self.report)
        tab = window.current_tab()
        window.find_action.trigger()
        self.assertTrue(tab.find_bar.isVisible())
        self.assertTrue(tab.find_bar.box.hasFocus())
        testing.type_text(tab.find_bar.box, "page 3")
        self.searched(tab)
        self.assertEqual(len(tab.hits), 30)                             # "Page 3 line …", thirty lines
        self.assertEqual(tab.find_bar.count.text(), "1 of 30")
        self.assertEqual(tab.view.current_page(), 2)
        self.assertEqual(tab.view._current_hit[0], 2)
        tab.find_bar.box.setText("PAGE")
        self.searched(tab)
        self.assertEqual(len(tab.hits), 360)
        tab.find_bar.case.setChecked(True)
        self.searched(tab)
        self.assertEqual(tab.hits, [])
        self.assertEqual(tab.find_bar.count.text(), "No matches")
        tab.find_bar.case.setChecked(False)
        tab.find_bar.box.setText("line 1")
        self.searched(tab)
        self.assertEqual(len(tab.hits), 12 * 11)                        # line 1 and lines 10 to 19
        tab.find_bar.words.setChecked(True)
        self.searched(tab)
        self.assertEqual(len(tab.hits), 12)
        self.sharp(tab)
        testing.process(250)
        self.shot(window, "search")

    def test_next_previous_wrap_and_results_list(self):
        window = self.window(self.report)
        tab = window.current_tab()
        tab.view.go_to_page(10)
        tab.open_find()
        tab.find_bar.box.setText("line 30:")
        self.searched(tab)
        self.assertEqual((len(tab.hits), tab.current_hit), (12, 10))    # starts at the page being read
        window.find_next_action.trigger()
        self.assertEqual((tab.current_hit, tab.view.current_page()), (11, 11))
        self.assertEqual(tab.find_bar.notice.text(), "")
        testing.key(tab.find_bar.box, Qt.Key.Key_Return)
        self.assertEqual(tab.current_hit, 0)
        self.assertEqual(tab.find_bar.notice.text(), "Continued from the top")
        self.assertEqual(tab.find_bar.count.text(), "1 of 12")
        window.find_previous_action.trigger()
        self.assertEqual(tab.current_hit, 11)
        self.assertEqual(tab.find_bar.notice.text(), "Continued from the bottom")
        sidebar = tab.sidebar
        self.assertEqual(sidebar.results_model.rowCount(), 12)
        self.assertIn("line 30:", sidebar.results_model.index(4).data())
        self.assertTrue(sidebar.results_model.index(4).data().startswith("5:"))
        sidebar.show_tab("results")
        sidebar.results.clicked.emit(sidebar.results_model.index(4))
        self.assertEqual((tab.current_hit, tab.view.current_page()), (4, 4))
        self.assertEqual(tab.find_bar.count.text(), "5 of 12")
        self.sharp(tab)
        testing.process(250)
        self.shot(window, "search-results")
        window.escape_action.trigger()
        self.assertFalse(tab.find_bar.isVisible())
        self.assertEqual(tab.hits, [])
        self.assertIsNone(tab.view._current_hit)
        self.assertEqual(sidebar.results_model.rowCount(), 0)

    def test_nothing_found_and_search_for_selection(self):
        window = self.window(self.report)
        tab = window.current_tab()
        tab.open_find()
        tab.find_bar.box.setText("unicorn")
        self.searched(tab)
        self.assertEqual(tab.find_bar.count.text(), "No matches")
        self.assertIn("Nothing found for “unicorn”", tab.sidebar.no_results.text())
        tab.close_find()
        tab.view._select_range(0, 19, 30)                               # "quick brown" on the first line
        self.assertEqual(tab.view.selected_text(), "quick brown")
        self.assertTrue(window.search_selection_action.isEnabled())
        window.search_selection_action.trigger()
        self.searched(tab)
        self.assertEqual(tab.find_bar.box.text(), "quick brown")
        self.assertEqual(len(tab.hits), 360)
        self.assertEqual(tab.sidebar.pivot.current(), "results")

    def test_search_in_a_long_document_shows_the_first_hit_early(self):
        big = make_pdf(self.tmp / "big.pdf", 600, lines=8)
        window = self.window(big)
        tab = window.current_tab()
        tab.open_find()
        tab.find_bar.box.setText("fox")
        self.assertTrue(testing.wait_until(lambda: tab.current_hit == 0, 10))
        self.assertTrue(testing.wait_until(lambda: not tab.searching, 60))
        self.assertEqual(len(tab.hits), 600 * 8)
        self.assertEqual(tab.find_bar.count.text(), "1 of 4,800")
        tab.find_bar.box.setText("page 599 line 8")                     # a new query replaces the old one
        self.searched(tab)
        self.assertEqual(len(tab.hits), 1)
        self.assertEqual(tab.view.current_page(), 598)


class ActionsTest(WindowCase):
    def test_copy_and_context_menu(self):
        window = self.window(self.report)
        tab = window.current_tab()
        menu = window.build_context_menu()
        self.assertEqual([action.text() for action in menu.actions() if action.text()],
                         ["Copy", "Select all on this page", "Search for the selection", "Copy page as a picture",
                          "Rotate left", "Rotate right", "Document properties"])
        self.assertFalse(menu.actions()[0].isEnabled())
        menu.close()
        window.select_all_action.trigger()
        self.assertTrue(window.copy_action.isEnabled())
        window.copy_action.trigger()
        self.assertIn("Page 1 line 30", QGuiApplication.clipboard().text())
        window.copy_page_action.trigger()
        self.assertTrue(testing.wait_until(lambda: "copied as a picture" in window.status_label.text(), 10))
        image = QGuiApplication.clipboard().image()
        self.assertEqual((image.width(), image.height()), (round(595 * 150 / 72), round(842 * 150 / 72)))
        window.escape_action.trigger()
        self.assertFalse(tab.view.has_selection())

    def test_web_links_open_only_after_the_user_says_so(self):
        window = self.window(self.report)
        tab = window.current_tab()
        self.sharp(tab, 0)
        position = self.view_point(tab, 0, 150, 84)
        port = tab.view.viewport()
        QApplication.sendEvent(port, QMouseEvent(QEvent.Type.MouseMove, QPointF(position), QPointF(port.mapToGlobal(position)),
                                                 Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                                 Qt.KeyboardModifier.NoModifier))
        self.assertEqual(window.status_label.text(), "https://example.org/report?year=2026")
        testing.click(port, position)
        self.assertEqual(self.urls, [])                                 # nothing was opened by the click
        self.assertTrue(window.banner.isVisible())
        self.assertEqual(window.banner.title(), "Open this link?")
        self.assertEqual(window.banner.message(), "https://example.org/report?year=2026")
        self.shot(window, "link")
        testing.click(window.banner.findChild(QPushButton))
        self.assertEqual(self.urls, ["https://example.org/report?year=2026"])
        self.assertFalse(window.banner.isVisible())
        window._web_link("file:///etc/passwd")
        self.assertEqual(window.banner.title(), "This kind of link is not opened")
        window._web_link("javascript:alert(1)")
        self.assertEqual(len(self.urls), 1)
        # The link above it leads into the document.
        testing.click(port, self.view_point(tab, 0, 150, 68))
        self.assertEqual(tab.view.current_page(), 9)
        self.assertTrue(window.back_action.isEnabled())

    def test_properties(self):
        window = self.window(self.report)
        window.properties_action.trigger()
        dialog = window.dialog
        self.assertEqual(dialog.values["Title"], "Annual report 2026")
        self.assertEqual(dialog.values["Author"], "Finance")
        self.assertEqual(dialog.values["Pages"], "12")
        self.assertEqual(dialog.values["Page size"], "210 × 297 mm (A4, portrait)")
        self.assertEqual(dialog.values["File"], "Annual report.pdf")
        testing.process(250)
        self.shot(window, "properties")
        testing.click(dialog.close_button)
        self.assertIsNone(window.dialog)

    def test_save_a_copy(self):
        window = self.window(self.report)
        target = self.tmp / "out" / "copy.pdf"
        target.parent.mkdir()
        window.choose_save = lambda suggestion: self.assertTrue(suggestion.endswith("Copy of Annual report.pdf")) or str(target)
        window.save_action.trigger()
        self.assertTrue(window.worker.wait(10))
        self.assertEqual(target.read_bytes(), self.report.read_bytes())
        self.assertEqual(window.banner.severity(), "success")
        self.assertEqual(os.listdir(target.parent), ["copy.pdf"])
        window.choose_save = lambda _suggestion: str(self.report)
        window.save_action.trigger()
        self.assertEqual(window.banner.severity(), "warning")
        window.choose_save = lambda _suggestion: str(self.tmp / "missing-folder" / "x.pdf")
        window.save_action.trigger()
        self.assertTrue(window.worker.wait(10))
        self.assertEqual(window.banner.severity(), "error")
        self.assertNotIn("Traceback", window.banner.message())

    def test_reload_is_offered_when_the_file_changes(self):
        window = self.window(self.report)
        tab = window.current_tab()
        tab.view.go_to_page(5)
        tab.view.set_zoom(1.25)
        replacement = make_pdf(self.tmp / "new.pdf", 20)
        os.replace(replacement, self.report)                            # as an editor saves: a new file under the name
        self.assertTrue(testing.wait_until(lambda: window.banner.isVisible(), 10))
        self.assertIn("changed on disk", window.banner.message())
        self.assertEqual(window.page_total.text(), "/ 12")              # nothing was reloaded by itself
        self.shot(window, "reload")
        testing.click(window.banner.findChild(QPushButton))
        self.assertEqual(window.page_total.text(), "/ 20")
        self.assertEqual((tab.view.current_page(), tab.view.zoom), (5, 1.25))
        self.sharp(tab)

    @unittest.skipUnless(pdfgen.PYSIDE and printing.AVAILABLE, "QtPrintSupport is not installed")
    def test_print_to_a_file(self):
        window = self.window(self.report)
        output = self.tmp / "printed.pdf"

        def choose(printer, _parent, page_count, _current):
            self.assertEqual(page_count, 12)
            printer.setOutputFormat(printer.OutputFormat.PdfFormat)
            printer.setOutputFileName(str(output))
            printer.setResolution(150)
            printer.setPrintRange(printer.PrintRange.PageRange)
            printer.setFromTo(3, 6)
            return True

        window.run_print_dialog = choose
        window.print_action.trigger()
        self.assertIsNotNone(window.dialog)
        self.assertTrue(testing.wait_until(lambda: window._print_job is None, 30))
        self.assertIsNone(window.dialog)
        self.assertEqual(window.banner.severity(), "success")
        self.assertIn("4 pages", window.banner.message())
        printed = QPdfDocument()
        self.assertEqual(printed.load(str(output)), QPdfDocument.Error.None_)
        self.assertEqual(printed.pageCount(), 4)
        printed.close()

    @unittest.skipUnless(pdfgen.PYSIDE and printing.AVAILABLE, "QtPrintSupport is not installed")
    def test_print_can_be_cancelled_and_declined(self):
        window = self.window(self.report)
        window.print_action.trigger()                                   # the dialog hook says "no"
        self.assertIsNone(window._print_job)
        output = self.tmp / "printed.pdf"
        tab = window.current_tab()
        printer = printing.new_printer()
        printer.setOutputFormat(printer.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(output))
        window.start_print(tab, printer, list(range(12)))
        dialog = window.dialog
        self.shot(window, "printing")
        testing.click(dialog.close_button)
        self.assertTrue(testing.wait_until(lambda: window._print_job is None, 30))
        self.assertIn("cancelled", window.banner.message())

    def test_without_print_support_there_is_no_print_command(self):
        with mock.patch.object(printing, "AVAILABLE", False):
            window = self.window(self.report)
            self.assertFalse(window.print_action.isVisible())
            buttons = [button for button in window.bar.findChildren(type(window.find_button))
                       if button.defaultAction() is window.print_action]
            self.assertEqual(buttons, [])
            window.print_document()
            self.assertIsNone(window._print_job)

    def test_presentation(self):
        window = self.window(self.report)
        tab = window.current_tab()
        tab.view.go_to_page(2)
        window.presentation_action.trigger()
        show = window.presentation
        self.keep(show)
        self.assertIsNotNone(show)
        show.resize(1024, 768)
        testing.process()
        self.assertTrue(testing.wait_until(show.has_image, 10))
        self.assertEqual(show.page, 2)
        image = show.grab().toImage()
        self.assertEqual(image.pixelColor(3, 3).name(), "#000000")      # black around the page
        self.assertEqual(image.pixelColor(512, 700).name(), "#ffffff")
        self.shot(show, "presentation")
        testing.key(show, Qt.Key.Key_Right)
        testing.key(show, Qt.Key.Key_Space)
        testing.click(show, QPoint(500, 400))
        self.assertEqual(show.page, 5)
        testing.key(show, Qt.Key.Key_Left)
        testing.key(show, Qt.Key.Key_Space, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(show.page, 3)
        testing.key(show, Qt.Key.Key_End)
        self.assertEqual(show.page, 11)
        testing.key(show, Qt.Key.Key_Right)
        self.assertEqual(show.page, 11)
        self.assertTrue(testing.wait_until(show.has_image, 10))
        testing.key(show, Qt.Key.Key_Escape)
        self.assertIsNone(window.presentation)
        self.assertEqual(tab.view.current_page(), 11)                   # the reader is where the presentation ended

    def test_full_screen_and_escape(self):
        window = self.window(self.report)
        window.fullscreen_action.trigger()
        self.assertTrue(window.isFullScreen())
        window.escape_action.trigger()
        self.assertFalse(window.isFullScreen())
        self.assertEqual(window.fullscreen_action.shortcut().toString(), "F11")
        self.assertEqual(window.presentation_action.shortcut().toString(), "F5")

    def test_narrow_window_keeps_every_command_reachable(self):
        window = self.window(self.report)
        window.resize(660, 500)
        testing.process()
        self.assertLessEqual(window.bar.layout().minimumSize().width(), 660)
        self.assertTrue(window.page_box.isVisible())
        self.assertTrue(window.zoom_box.isVisible())
        hidden = [widgets for _priority, widgets in window._groups if not widgets[0].isVisible()]
        self.assertTrue(hidden)
        texts = [action.text() for action in window.more_menu.actions()]
        for wanted in ("Open", "Save a copy", "Find", "Fit width", "Rotate left", "Full screen", "Presentation",
                       "Document properties", "Hand tool: drag to move the page"):
            self.assertIn(wanted, texts)
        self.shot(window, "narrow")
        window.resize(1300, 800)
        testing.process()
        self.assertTrue(all(widgets[0].isVisible() for _priority, widgets in window._groups))

    def test_every_control_has_a_name(self):
        window = self.window(self.report)
        for button in window.bar.findChildren(type(window.find_button)):
            self.assertTrue(button.toolTip() or button.text(), button)
            self.assertTrue(button.accessibleName() or button.text())
        self.assertEqual(window.page_box.accessibleName(), "Page number")
        self.assertEqual(window.zoom_box.accessibleName(), "Zoom")


@unittest.skipUnless(pdfgen.PYSIDE, "PySide6 is not installed")
class PackagingTest(unittest.TestCase):
    def test_no_colour_values_style_sheets_or_annotation_controls(self):
        root = PACKAGE / "boswas_apps" / "document_viewer"
        colour = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|QColor\(\s*\d|rgba?\(")
        for path in sorted(root.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(colour.search(text), f"{path.name} holds a colour value")
            self.assertNotIn("setStyleSheet", text, path.name)
            for word in ("Highlight text", "Add note", "Annotate"):
                self.assertNotIn(word, text, path.name)
        for name in ("layout", "textsearch", "store"):
            text = (root / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotIn("PySide6", text, name)

    def test_command_line(self):
        for option, expected in (("--version", "boswas-document-viewer 3."), ("--help", "FILE")):
            output = io.StringIO()
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(output):
                cli.main(["boswas-document-viewer", option])
            self.assertEqual(caught.exception.code, 0)
            self.assertIn(expected, output.getvalue())

    @unittest.skipUnless(os.geteuid() == 0, "only root is refused")
    def test_root_is_refused(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(cli.main(["boswas-document-viewer"]), 4)
        self.assertIn("never as root", errors.getvalue())

    def test_launcher_desktop_entry_and_manual(self):
        launcher = (PACKAGE / "bin" / "boswas-document-viewer").read_text()
        self.assertEqual(launcher.splitlines()[0], "#!/usr/bin/python3 -IB")
        self.assertIn("from boswas_apps.document_viewer.__main__ import main", launcher)
        lines = (PACKAGE / "data" / "document-viewer" / "com.boswas.DocumentViewer.desktop").read_text().splitlines()
        entry = dict(line.split("=", 1) for line in lines[1:lines.index("")] if "=" in line)
        self.assertEqual((entry["Name"], entry["Icon"], entry["MimeType"], entry["Exec"]),
                         ("Document Viewer", "boswas-document-viewer", "application/pdf;", "boswas-document-viewer %F"))
        self.assertEqual(entry["StartupWMClass"], "com.boswas.DocumentViewer")
        icons = PACKAGE.parents[1] / "desktop" / "icons" / "Boswas-Prism" / "scalable" / "apps"
        self.assertTrue((icons / "boswas-document-viewer.svg").is_file())
        manual = (PACKAGE / "man" / "boswas-document-viewer.1").read_text()
        for wanted in (".SH NAME", ".SH SYNOPSIS", ".SH DESCRIPTION", ".SH OPTIONS", ".SH FILES", ".SH SEE ALSO"):
            self.assertIn(wanted, manual)


if __name__ == "__main__":
    unittest.main()
