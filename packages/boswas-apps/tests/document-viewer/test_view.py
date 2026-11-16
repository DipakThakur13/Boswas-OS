"""The document, the render thread and the page view, on PDF files written by the tests (Qt offscreen)."""

from __future__ import annotations

import os
import time
import unittest

import pdfgen
from pdfgen import make_pdf

if pdfgen.PYSIDE:
    from boswas_fluent import testing
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QGuiApplication, QMouseEvent, QWheelEvent
    from PySide6.QtWidgets import QApplication

    from boswas_apps.document_viewer import layout as lay
    from boswas_apps.document_viewer import view as view_module
    from boswas_apps.document_viewer.document import (FAILED, NEEDS_PASSWORD, READY, WRONG_PASSWORD, PdfDocument,
                                                      paper_name, size_text)
    from boswas_apps.document_viewer.render import Renderer, Request, render
    from boswas_apps.document_viewer.view import HAND, PdfView
    BASE = testing.FluentTestCase
else:
    BASE = unittest.TestCase

SCALE = 96 / 72


@unittest.skipUnless(pdfgen.PYSIDE, "PySide6 is not installed")
class PdfCase(BASE):
    def setUp(self) -> None:
        super().setUp()
        self._scratch = pdfgen.enter_scratch_directory()
        self.addCleanup(self._leave)
        self._renderers = []
        self._documents = []

    def _leave(self) -> None:
        for renderer in self._renderers:
            renderer.stop()
        for document in self._documents:
            document.close()
        self.assertEqual(pdfgen.leave_scratch_directory(self._scratch), [],
                         "something was written into the working directory")

    def document(self, *args, **kwargs) -> "PdfDocument":
        password = kwargs.pop("open_with", None)
        path = make_pdf(self.tmp / kwargs.pop("name", "doc.pdf"), *args, **kwargs)
        document = PdfDocument(str(path))
        self._documents.append(document)
        document.load(password)
        return document

    def renderer(self, document) -> "Renderer":
        renderer = Renderer(document.qt)
        self._renderers.append(renderer)
        return renderer

    def view(self, document, width=900, height=700) -> "PdfView":
        view = PdfView()
        self.show(view, width, height)
        view.set_document(document, self.renderer(document))
        testing.process()
        return view

    def sharp(self, view, page=None, timeout=10) -> None:
        self.assertTrue(testing.wait_until(lambda: view.has_sharp_image(view.current_page() if page is None else page),
                                           timeout))

    def at(self, view, page: int, x: float, y: float) -> "QPoint":
        """The viewport position of a point of a page given in points."""
        rect = view.page_rect(page)
        return QPoint(round(rect.left() + x * view.zoom * SCALE), round(rect.top() + y * view.zoom * SCALE))

    def mouse(self, view, kind, position, button=None, buttons=None) -> None:
        button = Qt.MouseButton.LeftButton if button is None else button
        if buttons is None:
            buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
        port = view.viewport()
        QApplication.sendEvent(port, QMouseEvent(kind, QPointF(position), QPointF(port.mapToGlobal(position)),
                                                 button, buttons, Qt.KeyboardModifier.NoModifier))

    def drag(self, view, start, end, steps=6) -> None:
        self.mouse(view, QEvent.Type.MouseButtonPress, start)
        for step in range(1, steps + 1):
            point = QPoint(round(start.x() + (end.x() - start.x()) * step / steps),
                           round(start.y() + (end.y() - start.y()) * step / steps))
            self.mouse(view, QEvent.Type.MouseMove, point, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        self.mouse(view, QEvent.Type.MouseButtonRelease, end)
        testing.process()

    def wheel(self, view, position, delta, modifiers=None) -> None:
        port = view.viewport()
        event = QWheelEvent(QPointF(position), QPointF(port.mapToGlobal(position)), QPoint(0, 0), QPoint(0, delta),
                            Qt.MouseButton.NoButton, modifiers or Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(port, event)
        testing.process()


class DocumentTest(PdfCase):
    def test_facts(self):
        document = self.document(5, info={"Title": "Annual report", "Author": "Finance", "Subject": "Numbers",
                                          "Keywords": "a, b", "Creator": "pdfgen", "Producer": "tests"},
                                 sizes=[(595, 842)] * 4 + [(842, 595)])
        self.assertEqual(document.state, READY)
        self.assertEqual(document.page_count, 5)
        self.assertEqual(document.sizes[4], (842.0, 595.0))
        self.assertEqual(document.title(), "Annual report")
        self.assertEqual((document.label(0), document.label(4), document.label(99)), ("1", "5", "100"))
        self.assertEqual(document.page_for_label("3"), 2)
        self.assertEqual(document.page_for_label(" 5 "), 4)
        for text in ("0", "6", "", "x", "-1"):
            self.assertEqual(document.page_for_label(text), -1, text)
        rows = dict(document.properties())
        self.assertEqual(rows["Title"], "Annual report")
        self.assertEqual(rows["Author"], "Finance")
        self.assertEqual(rows["Keywords"], "a, b")
        self.assertEqual(rows["Producer"], "tests")
        self.assertEqual(rows["Pages"], "5")
        self.assertEqual(rows["Page size"], "210 × 297 mm (A4, portrait) (first page; sizes vary)")
        self.assertRegex(rows["File size"], r"^\d+(\.\d)? KB$")
        self.assertEqual(rows["Password"], "None")
        self.assertEqual(rows["File"], "doc.pdf")
        self.assertEqual(rows["Created"], "—")

    def test_paper_names(self):
        self.assertEqual(paper_name(595, 842), "A4")
        self.assertEqual(paper_name(792, 612), "Letter")
        self.assertEqual(paper_name(500, 500), "")
        self.assertEqual(size_text(612, 792), "216 × 279 mm (Letter, portrait)")
        self.assertEqual(size_text(842, 595), "297 × 210 mm (A4, landscape)")
        self.assertEqual(size_text(300, 300), "106 × 106 mm")

    def test_files_that_are_no_documents(self):
        cases = {"empty.pdf": b"", "text.pdf": b"just some text, no PDF at all\n" * 20,
                 "broken.pdf": b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"}
        for name, content in cases.items():
            path = self.tmp / name
            path.write_bytes(content)
            document = PdfDocument(str(path))
            self.assertEqual(document.load(), FAILED, name)
            self.assertTrue(document.error_text, name)
            self.assertNotIn("Traceback", document.error_text)
            self.assertEqual(document.page_count, 0)
        missing = PdfDocument(str(self.tmp / "missing.pdf"))
        self.assertEqual(missing.load(), FAILED)
        self.assertIn("not there", missing.error_text)
        truncated = make_pdf(self.tmp / "cut.pdf", 3).read_bytes()
        (self.tmp / "cut.pdf").write_bytes(truncated[:200])
        self.assertEqual(PdfDocument(str(self.tmp / "cut.pdf")).load(), FAILED)

    def test_password(self):
        path = make_pdf(self.tmp / "locked.pdf", 2, password="s3cret", info={"Title": "Locked"},
                        outline=[("Hidden chapter", 1, [])])
        document = PdfDocument(str(path))
        self._documents.append(document)
        self.assertEqual(document.load(), NEEDS_PASSWORD)
        self.assertEqual(document.page_count, 0)
        self.assertEqual(document.load("wrong"), WRONG_PASSWORD)
        self.assertEqual(document.load("s3cret"), READY)
        self.assertEqual(document.page_count, 2)
        self.assertIn("Page 2 line 1", document.text(1))
        self.assertEqual(document.title(), "Locked")
        self.assertEqual(dict(document.properties())["Password"], "Protected")
        self.assertTrue(document.has_outline())

    def test_links(self):
        document = self.document(5, links=[(0, (72, 60, 200, 16), 3), (0, (72, 92, 200, 16), "https://example.org/a?b=1"),
                                           (2, (10, 10, 50, 50), 0)])
        internal, web = document.links(0)
        self.assertEqual((internal.page, internal.url, internal.rect), (3, "", (72.0, 60.0, 200.0, 16.0)))
        self.assertEqual((web.page, web.url), (-1, "https://example.org/a?b=1"))
        self.assertEqual(document.links(1), [])
        self.assertIs(document.link_at(0, 100, 70), internal)
        self.assertIs(document.link_at(0, 100, 100), web)
        self.assertIsNone(document.link_at(0, 400, 400))
        self.assertEqual(document.link_at(2, 20, 20).page, 0)

    def test_text_and_where_it_is(self):
        document = self.document(3)
        text = document.text(1)
        self.assertTrue(text.startswith("Page 2 line 1: the quick brown fox"))
        self.assertIs(document.text(1), text)                              # cached
        self.assertEqual(document.text(99), "")
        start = text.index("brown")
        rects = document.rects_at(1, start, 5)
        self.assertTrue(rects)
        left = min(rect[0] for rect in rects)
        right = max(rect[0] + rect[2] for rect in rects)
        top = min(rect[1] for rect in rects)
        self.assertTrue(72 < left < right < 300, (left, right))
        self.assertTrue(55 < top < 80, top)                                # the first line, 72 points from the top
        index = document.index_at(1, (left + right) / 2, top + 5)
        self.assertTrue(start <= index < start + 5, index)
        self.assertEqual(document.index_at(1, 400, 700), -1)

    def test_outline(self):
        document = self.document(6, outline=[("Introduction", 0, []), ("Results", 2, [("Revenue", 3, []), ("Ünïcode ✓", 4, [])])])
        from PySide6.QtCore import QModelIndex
        from PySide6.QtPdf import QPdfBookmarkModel
        model = document.bookmarks()
        self.assertTrue(document.has_outline())
        titles = [model.index(row, 0, QModelIndex()).data(QPdfBookmarkModel.Role.Title.value) for row in range(2)]
        self.assertEqual(titles, ["Introduction", "Results"])
        results = model.index(1, 0, QModelIndex())
        child = model.index(1, 0, results)
        self.assertEqual(child.data(QPdfBookmarkModel.Role.Title.value), "Ünïcode ✓")
        self.assertEqual(child.data(QPdfBookmarkModel.Role.Page.value), 4)
        self.assertFalse(self.document(2, name="plain.pdf").has_outline())


class RenderTest(PdfCase):
    def test_whole_page_part_and_rotation(self):
        document = self.document(2)
        image = render(document.qt, Request("page", 0, 595, 842))
        self.assertEqual((image.width(), image.height()), (595, 842))
        self.assertFalse(image.hasAlphaChannel())                          # opaque, on white paper
        self.assertEqual(image.pixelColor(5, 5).name(), "#ffffff")
        dark = sum(1 for x in range(72, 400, 2) for y in range(60, 76) if image.pixelColor(x, y).lightness() < 128)
        self.assertGreater(dark, 20)                                       # the first line of text is there
        part = render(document.qt, Request("detail", 0, 2380, 3368, 0, (288, 240, 800, 80)))
        self.assertEqual((part.width(), part.height()), (800, 80))
        self.assertGreater(sum(1 for x in range(0, 800, 4) for y in range(0, 80, 2)
                               if part.pixelColor(x, y).lightness() < 128), 50)
        turned = render(document.qt, Request("page", 0, 842, 595, 1))
        self.assertEqual((turned.width(), turned.height()), (842, 595))
        # A quarter turn clockwise: the text that ran along the top now runs down the right edge.
        column = sum(1 for y in range(72, 400, 2) for x in range(842 - 76, 842 - 60) if turned.pixelColor(x, y).lightness() < 128)
        self.assertGreater(column, 20)
        self.assertTrue(render(document.qt, Request("page", 99, 100, 100)).isNull())

    def test_thread_delivers_and_honours_priority(self):
        document = self.document(40)
        renderer = self.renderer(document)
        got = []
        renderer.rendered.connect(lambda request, image: got.append((request, image.width())))
        # Many requests at once: the urgent one overtakes those that were asked for before it.
        for page in range(30):
            renderer.request(Request("page", page, 1200, 1700), priority=50)
        renderer.request(Request("thumb", 39, 60, 85), priority=0)
        self.assertTrue(testing.wait_until(lambda: len(got) == 31, 30))
        order = [request.page for request, _width in got]
        self.assertLess(order.index(39), 3)
        self.assertEqual(sorted(order), sorted([*range(30), 39]))
        self.assertEqual(renderer.pending(), 0)

    def test_waiting_requests_can_be_dropped(self):
        document = self.document(60)
        renderer = self.renderer(document)
        got = []
        renderer.rendered.connect(lambda request, image: got.append(request))
        wanted = Request("page", 59, 1200, 1700)
        for page in range(60):
            renderer.request(Request("page", page, 1200, 1700), priority=page)
        renderer.request(Request("thumb", 5, 60, 85), priority=500)
        renderer.keep_only("page", {wanted})
        self.assertTrue(testing.wait_until(lambda: renderer.pending() == 0, 30))
        testing.process(50)
        pages = [request for request in got if request.kind == "page"]
        self.assertLessEqual(len(pages), 3)                                # the one in work, and the one wanted
        self.assertIn(wanted, got)
        self.assertIn(Request("thumb", 5, 60, 85), got)                    # other kinds are left alone

    def test_stop_ends_the_thread(self):
        document = self.document(5)
        renderer = Renderer(document.qt)
        for page in range(5):
            renderer.request(Request("page", page, 2000, 2800))
        renderer.stop()
        self.assertFalse(renderer._thread.is_alive())
        renderer.request(Request("page", 0, 10, 10))                       # ignored after stop
        self.assertEqual(renderer.pending(), 0)


class ViewTest(PdfCase):
    def test_opens_fitted_to_the_width_and_sharp(self):
        document = self.document(3)
        view = self.view(document)
        self.assertEqual(view.zoom_mode, lay.FIT_WIDTH)
        rect = view.page_rect(0)
        self.assertAlmostEqual(rect.left(), lay.MARGIN, delta=1)
        self.assertAlmostEqual(rect.width(), view.viewport().width() - 2 * lay.MARGIN, delta=2)
        self.assertEqual(rect.top(), lay.MARGIN)
        self.sharp(view, 0)
        image = view.viewport().grab().toImage()
        self.assertEqual(image.pixelColor(round(rect.center().x()), 400).name(), "#ffffff")
        self.assertNotEqual(image.pixelColor(3, 400).name(), "#ffffff")           # the margin is not paper
        second = view.page_rect(1)
        self.assertEqual(second.top() - rect.bottom(), lay.GAP)                   # the gap between pages

    def test_a_thousand_pages_scroll_without_waiting(self):
        document = self.document(1000, lines=12)
        view = self.view(document)
        bar = view.verticalScrollBar()
        self.assertGreater(bar.maximum(), 1_000_000)
        self.sharp(view, 0)
        worst = 0.0
        started = time.perf_counter()
        for step in range(200):
            frame = time.perf_counter()
            bar.setValue(step * bar.maximum() // 200)
            view.viewport().repaint()                                     # painting never waits for the renderer
            QApplication.processEvents()
            worst = max(worst, time.perf_counter() - frame)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed / 200, 0.05, "scrolling is too slow")
        self.assertLess(worst, 0.5)
        self.assertGreater(view.current_page(), 990)
        self.assertLessEqual(view.cached_bytes(), view_module.CACHE_BYTES)
        self.assertLess(len(view._pages), 120)                           # not a thousand images
        self.assertLessEqual(view.renderer.pending(), 8)                  # what scrolled past was dropped
        self.sharp(view)
        view.go_to_page(499)
        self.assertEqual(view.current_page(), 499)
        self.sharp(view, 499)

    def test_memory_budget_drops_far_pages(self):
        document = self.document(60, lines=5)
        view = self.view(document)
        old = view_module.CACHE_BYTES
        view_module.CACHE_BYTES = 12 * 1024 * 1024
        try:
            for page in range(0, 60, 2):
                view.go_to_page(page)
                self.sharp(view, page)
            self.assertLessEqual(view.cached_bytes(), 12 * 1024 * 1024 + 8 * 1024 * 1024)
            self.assertIn(view.current_page(), view._pages)
            self.assertNotIn(0, view._pages)
        finally:
            view_module.CACHE_BYTES = old

    def test_zoom_keeps_the_point_under_the_pointer(self):
        document = self.document(5)
        view = self.view(document)
        view.go_to_page(1)
        pointer = QPoint(300, 260)
        page, x, y = view._page_at(pointer)
        self.assertEqual(page, 1)
        for delta in (120, 120, 120, -240, 360):
            self.wheel(view, pointer, delta, Qt.KeyboardModifier.ControlModifier)
            same_page, new_x, new_y = view._page_at(pointer)
            self.assertEqual(same_page, 1)
            self.assertAlmostEqual(new_x, x, delta=2.0)
            self.assertAlmostEqual(new_y, y, delta=2.0)
        self.assertEqual(view.zoom_mode, lay.CUSTOM)
        self.assertGreater(view.zoom, 1.3)

    def test_old_image_is_stretched_until_the_sharp_one_arrives(self):
        document = self.document(3)
        view = self.view(document)
        self.sharp(view, 0)
        before = view._pages[0][1]
        view.set_zoom(view.zoom * 1.7)
        self.assertFalse(view.has_sharp_image(0))
        self.assertEqual(view._pages[0][1], before)                       # still there, drawn stretched
        image = view.viewport().grab().toImage()
        rect = view.page_rect(0)
        dark = sum(1 for x in range(max(0, int(rect.left())) + 20, min(880, int(rect.right())), 3)
                   for y in range(max(0, int(rect.top())), 400, 3) if image.pixelColor(x, y).lightness() < 140)
        self.assertGreater(dark, 30)                                      # text is on screen at once
        self.sharp(view, 0)
        self.assertNotEqual(view._pages[0][1], before)

    def test_high_zoom_renders_the_visible_part_only(self):
        document = self.document(3)
        view = self.view(document)
        view.set_zoom(8.0, at=QPointF(300, 300))
        self.sharp(view)
        page = view.current_page()
        base = view._pages[page][0]
        self.assertLessEqual(base.width() * base.height(), view_module.BASE_PIXELS * 1.2)
        detail, key, area = view._details[page]
        self.assertEqual(key[:2], view._target(page))
        self.assertLess(detail.width() * detail.height(), 4_000_000)
        self.assertLess(view.cached_bytes(), 64 * 1024 * 1024)
        # Moving on asks for the part that is now in view.
        view.verticalScrollBar().setValue(view.verticalScrollBar().value() + 1500)
        self.assertTrue(testing.wait_until(lambda: view._details.get(page, (None, None, area))[2] != area, 10))

    def test_fit_modes_follow_the_window(self):
        document = self.document(3)
        view = self.view(document)
        view.set_zoom(0, lay.FIT_PAGE)
        rect = view.page_rect(0)
        self.assertLessEqual(rect.height(), view.viewport().height() - 2 * lay.MARGIN + 1)
        self.assertAlmostEqual(rect.center().x(), view.viewport().width() / 2, delta=2)
        zoom = view.zoom
        view.resize(1200, 900)
        testing.process()
        self.assertGreater(view.zoom, zoom)
        view.set_zoom(1.0)
        view.resize(700, 500)
        testing.process()
        self.assertEqual(view.zoom, 1.0)                                  # a fixed zoom stays
        self.assertGreater(view.horizontalScrollBar().maximum(), 0)

    def test_rotation(self):
        document = self.document(3)
        view = self.view(document)
        view.go_to_page(1)
        view.set_zoom(0.5)
        upright = view.page_rect(1)
        view.rotate(1)
        turned = view.page_rect(1)
        self.assertEqual((turned.width(), turned.height()), (upright.height(), upright.width()))
        self.assertEqual(view.current_page(), 1)
        self.sharp(view, 1)
        self.assertEqual(view._pages[1][1][2], 1)
        view.rotate(-1)
        view.rotate(-1)
        self.assertEqual(view.rotation, 3)

    def test_single_page_mode_flips_pages(self):
        document = self.document(6)
        view = self.view(document)
        view.set_arrangement(False, 1, False)
        view.set_zoom(0, lay.FIT_PAGE)
        self.assertEqual(view._visible_pages(), [0])
        self.assertEqual(view.verticalScrollBar().maximum(), 0)
        testing.key(view, Qt.Key.Key_PageDown)
        self.assertEqual(view.current_page(), 1)
        testing.key(view, Qt.Key.Key_Space)
        self.assertEqual(view.current_page(), 2)
        testing.key(view, Qt.Key.Key_Space, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(view.current_page(), 1)
        self.wheel(view, QPoint(400, 300), -120)
        self.assertEqual(view.current_page(), 2)
        self.wheel(view, QPoint(400, 300), 120)
        self.assertEqual(view.current_page(), 1)
        testing.key(view, Qt.Key.Key_End)
        self.assertEqual(view.current_page(), 5)
        testing.key(view, Qt.Key.Key_PageDown)
        self.assertEqual(view.current_page(), 5)
        testing.key(view, Qt.Key.Key_Home)
        self.assertEqual(view.current_page(), 0)
        self.assertEqual(view._visible_pages(), [0])
        self.sharp(view, 0)

    def test_two_pages_with_cover(self):
        document = self.document(6)
        view = self.view(document, 1200, 700)
        view.set_arrangement(True, 2, True)
        cover, left, right = view.page_rect(0), view.page_rect(1), view.page_rect(2)
        self.assertAlmostEqual(cover.center().x(), view.viewport().width() / 2, delta=2)
        self.assertEqual(left.top(), right.top())
        self.assertEqual(right.left() - left.right(), lay.GAP)
        self.assertGreater(left.top(), cover.bottom())
        view.next_page()
        self.assertEqual(view.current_page(), 1)
        view.next_page()
        self.assertEqual(view.current_page(), 3)
        view.set_arrangement(True, 2, False)
        self.assertEqual(view.page_rect(0).top(), view.page_rect(1).top())

    def test_keys_in_continuous_mode(self):
        document = self.document(8)
        view = self.view(document)
        bar = view.verticalScrollBar()
        testing.key(view, Qt.Key.Key_Down)
        self.assertEqual(bar.value(), view_module.LINE_STEP)
        testing.key(view, Qt.Key.Key_Up)
        self.assertEqual(bar.value(), 0)
        testing.key(view, Qt.Key.Key_PageDown)
        step = view.viewport().height() - view_module.LINE_STEP
        self.assertEqual(bar.value(), step)
        testing.key(view, Qt.Key.Key_Space)
        self.assertEqual(bar.value(), 2 * step)
        testing.key(view, Qt.Key.Key_Space, Qt.KeyboardModifier.ShiftModifier)
        testing.key(view, Qt.Key.Key_PageUp)
        self.assertEqual(bar.value(), 0)
        testing.key(view, Qt.Key.Key_End)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertEqual(view.current_page(), 7)
        testing.key(view, Qt.Key.Key_Home)
        self.assertEqual((bar.value(), view.current_page()), (0, 0))
        pages = []
        view.page_changed.connect(pages.append)
        view.go_to_page(4)
        self.assertEqual(pages, [4])

    def test_selecting_text_with_the_mouse(self):
        document = self.document(3)
        view = self.view(document)
        changes = []
        view.selection_changed.connect(lambda: changes.append(view.selected_text()))
        self.drag(view, self.at(view, 0, 74, 68), self.at(view, 0, 190, 68))
        self.assertTrue(view.has_selection())
        self.assertTrue(view.selected_text().startswith("Page 1 line 1"), view.selected_text())
        self.assertNotIn("\n", view.selected_text())
        self.assertTrue(changes)
        self.drag(view, self.at(view, 0, 74, 68), self.at(view, 0, 300, 116))        # down into the fourth line
        self.assertEqual(view.selected_text().count("\n"), 3)
        self.assertTrue(view.copy_selection())
        self.assertIn("Page 1 line 2: the quick brown fox", QGuiApplication.clipboard().text())
        self.assertNotIn("\r", QGuiApplication.clipboard().text())
        testing.click(view.viewport(), self.at(view, 0, 400, 600))                  # a click elsewhere drops it
        self.assertFalse(view.has_selection())
        self.assertFalse(view.copy_selection())

    def test_double_and_triple_click(self):
        document = self.document(3)
        view = self.view(document)
        text = document.text(0)
        rects = document.rects_at(0, text.index("brown"), 5)
        middle = self.at(view, 0, rects[0][0] + rects[0][2] / 2, rects[0][1] + rects[0][3] / 2)
        testing.click(view.viewport(), middle)
        testing.click(view.viewport(), middle)
        self.assertEqual(view.selected_text(), "brown")
        testing.click(view.viewport(), middle)
        self.assertEqual(view.selected_text(), "Page 1 line 1: the quick brown fox jumps over the lazy dog.")
        view.select_all_on_page()
        self.assertEqual(view.selected_text().count("\n"), 29)
        view.clear_selection()
        self.assertEqual(view.selected_text(), "")

    def test_links_jump_and_web_links_only_report(self):
        document = self.document(6, links=[(0, (72, 60, 200, 16), 4), (0, (72, 92, 200, 16), "https://example.org/x")])
        view = self.view(document)
        hovered, web, jumps = [], [], []
        view.link_hovered.connect(hovered.append)
        view.web_link_clicked.connect(web.append)
        view.jumped.connect(lambda: jumps.append(view.current_page()))
        inside = self.at(view, 0, 150, 68)
        self.mouse(view, QEvent.Type.MouseMove, inside, Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
        self.assertEqual(hovered[-1], "Go to page 5")
        self.assertEqual(view.viewport().cursor().shape(), Qt.CursorShape.PointingHandCursor)
        self.mouse(view, QEvent.Type.MouseMove, self.at(view, 0, 400, 500), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
        self.assertEqual(hovered[-1], "")
        self.assertEqual(view.viewport().cursor().shape(), Qt.CursorShape.IBeamCursor)
        testing.click(view.viewport(), inside)
        self.assertEqual(view.current_page(), 4)
        self.assertEqual(jumps, [4])
        self.assertTrue(view.history.can_back())
        view.go_back()
        self.assertEqual(view.current_page(), 0)
        self.assertTrue(view.history.can_forward())
        view.go_forward()
        self.assertEqual(view.current_page(), 4)
        view.go_back()
        web_link = self.at(view, 0, 150, 100)
        self.mouse(view, QEvent.Type.MouseMove, web_link, Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
        self.assertEqual(hovered[-1], "https://example.org/x")
        testing.click(view.viewport(), web_link)
        self.assertEqual(web, ["https://example.org/x"])
        self.assertEqual(view.current_page(), 0)
        # Pressing on a link and letting go somewhere else is not a click on it.
        self.mouse(view, QEvent.Type.MouseButtonPress, web_link)
        self.mouse(view, QEvent.Type.MouseButtonRelease, self.at(view, 0, 400, 500))
        self.assertEqual(len(web), 1)

    def test_hand_tool_drags_the_page(self):
        document = self.document(5)
        view = self.view(document)
        view.set_tool(HAND)
        self.assertEqual(view.viewport().cursor().shape(), Qt.CursorShape.OpenHandCursor)
        self.drag(view, QPoint(400, 500), QPoint(400, 200))
        self.assertEqual(view.verticalScrollBar().value(), 300)
        self.assertFalse(view.has_selection())
        self.drag(view, QPoint(400, 200), QPoint(400, 400))
        self.assertEqual(view.verticalScrollBar().value(), 100)

    def test_search_marks_are_painted(self):
        document = self.document(2)
        view = self.view(document)
        self.sharp(view, 0)
        text = document.text(0)
        rects = document.rects_at(0, text.index("brown"), 5)
        centre = self.at(view, 0, rects[0][0] + rects[0][2] / 2, rects[0][1] + 0.9)
        plain = view.viewport().grab().toImage().pixelColor(centre)
        view.set_hit_source(lambda page: [rects] if page == 0 else [])
        marked = view.viewport().grab().toImage().pixelColor(centre)
        self.assertNotEqual(marked.name(), plain.name())
        view.set_current_hit(0, rects)
        current = view.viewport().grab().toImage().pixelColor(centre)
        self.assertNotEqual(current.name(), marked.name())               # the current hit has its own colour
        view.set_hit_source(None)
        view.set_current_hit(-1, None)
        self.assertEqual(view.viewport().grab().toImage().pixelColor(centre).name(), plain.name())
        view.show_rects(1, document.rects_at(1, 0, 4))
        self.assertEqual(view.current_page(), 1)

    def test_view_state_round_trip(self):
        document = self.document(12)
        view = self.view(document)
        view.set_arrangement(True, 2, True)
        view.set_zoom(0.8)
        view.rotate(1)
        view.go_to_page(7)
        state = view.view_state()
        self.assertEqual((state["page"], state["mode"], state["rotation"], state["columns"], state["cover"]),
                         (7, lay.CUSTOM, 1, 2, True))
        other = self.view(document)
        other.restore_view(state)
        self.assertEqual(other.view_state(), state)
        other.restore_view({"page": 999, "zoom": 500, "mode": "fit-page"})
        self.assertEqual(other.current_page(), 11)
        self.assertEqual(other.zoom_mode, lay.FIT_PAGE)

    def test_empty_view_and_closing_a_document(self):
        view = PdfView()
        self.show(view, 600, 400)
        view.viewport().grab()
        for call in (view.next_page, view.zoom_in, view.select_all_on_page, view.go_back, lambda: view.go_to_page(3)):
            call()
        testing.key(view, Qt.Key.Key_PageDown)
        self.assertEqual(view.page_count(), 0)
        document = self.document(3)
        view.set_document(document, self.renderer(document))
        self.sharp(view, 0)
        view.set_document(None, None)
        view.viewport().grab()
        self.assertEqual(view.page_count(), 0)


if __name__ == "__main__":
    unittest.main()
