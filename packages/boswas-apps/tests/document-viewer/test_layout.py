"""Page layout, zoom arithmetic, text search and the remembered state: the parts without Qt."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import pdfgen  # noqa: F401  (sets up the import path)
from boswas_apps.document_viewer import layout as lay
from boswas_apps.document_viewer import textsearch
from boswas_apps.document_viewer.store import MAX_DOCUMENTS, MAX_RECENT, Store

A4 = (595.0, 842.0)


class RowsTest(unittest.TestCase):
    def test_rows(self):
        self.assertEqual(lay.rows_of(0), [])
        self.assertEqual(lay.rows_of(3), [[0], [1], [2]])
        self.assertEqual(lay.rows_of(5, 2), [[0, 1], [2, 3], [4]])
        self.assertEqual(lay.rows_of(5, 2, cover=True), [[0], [1, 2], [3, 4]])
        self.assertEqual(lay.rows_of(1, 2, cover=True), [[0]])

    def test_row_of_page_agrees_with_rows(self):
        for count in (1, 2, 7, 8):
            for columns, cover in ((1, False), (2, False), (2, True)):
                rows = lay.rows_of(count, columns, cover)
                for index, row in enumerate(rows):
                    for page in row:
                        self.assertEqual(lay.row_of_page(page, count, columns, cover), index)


class BuildTest(unittest.TestCase):
    def test_single_column_is_centred_with_gaps(self):
        layout = lay.build([A4, A4, (842.0, 595.0)], 1.0, viewport_width=1200)
        x, y, width, height = layout.rect(0)
        self.assertEqual((width, height), (793.0, 1123.0))
        self.assertEqual(x, round((1200 - 793) / 2))
        self.assertEqual(y, lay.MARGIN)
        self.assertEqual(layout.rect(1)[1], lay.MARGIN + 1123 + lay.GAP)
        wide = layout.rect(2)
        self.assertEqual(wide[2], 1123.0)
        self.assertEqual(wide[0], round((1200 - 1123) / 2))
        self.assertEqual(layout.height, wide[1] + wide[3] + lay.MARGIN)
        self.assertEqual(layout.width, 1200)

    def test_wider_than_the_viewport(self):
        layout = lay.build([A4], 2.0, viewport_width=800)
        self.assertEqual(layout.width, 1587 + 2 * lay.MARGIN)
        self.assertEqual(layout.rect(0)[0], lay.MARGIN)

    def test_two_columns_with_cover(self):
        layout = lay.build([A4] * 4, 0.5, columns=2, cover=True, viewport_width=1000)
        cover, left, right, last = (layout.rect(page) for page in range(4))
        self.assertEqual(cover[0], round((1000 - cover[2]) / 2))
        self.assertEqual(left[1], right[1])
        self.assertEqual(right[0], left[0] + left[2] + lay.GAP)
        self.assertEqual(left[0] + (left[2] * 2 + lay.GAP) / 2, 500)
        self.assertEqual(last[0], round((1000 - last[2]) / 2))             # a page alone in the last row is centred
        self.assertEqual(layout.rows, [[0], [1, 2], [3]])

    def test_rotation_swaps_sides(self):
        layout = lay.build([A4], 1.0, rotation=1, viewport_width=2000)
        self.assertEqual(layout.rect(0)[2:], (1123.0, 793.0))
        self.assertEqual(lay.rotated(3, 5, 2), (3, 5))
        self.assertEqual(lay.rotated(3, 5, 3), (5, 3))

    def test_everything_is_on_whole_pixels(self):
        layout = lay.build([(612.3, 791.7), (500.5, 333.3)], 1.37, columns=2, viewport_width=1777)
        for page in (0, 1):
            self.assertTrue(all(float(value).is_integer() for value in layout.rect(page)))

    def test_one_row_alone_is_centred_in_the_height(self):
        layout = lay.build([A4] * 6, 0.5, columns=2, viewport_width=1000, only_row=1, viewport_height=900)
        self.assertEqual(sorted(layout.rects), [2, 3])
        self.assertEqual(layout.rect(2)[1], round((900 - layout.rect(2)[3]) / 2))
        self.assertEqual(layout.height, 900)
        self.assertEqual(lay.build([A4], 1.0, only_row=5).rects, {})

    def test_pages_between_and_page_at(self):
        layout = lay.build([A4] * 100, 1.0, viewport_width=1000)
        step = 1123 + lay.GAP
        self.assertEqual(layout.pages_between(0, 500), [0])
        self.assertEqual(layout.pages_between(1000, 1400), [0, 1])
        self.assertEqual(layout.pages_between(50 * step + 300, 50 * step + 900), [50])
        self.assertEqual(layout.pages_between(layout.height + 10, layout.height + 500), [])
        self.assertEqual(layout.pages_between(100, 100), [])
        self.assertEqual(layout.page_at(500, 20 * step + 600), 20)
        self.assertEqual(layout.page_at(5, 20 * step + 600), 20)               # beside the page: the nearest
        self.assertEqual(layout.main_page(3 * step - 100, 3 * step + 800), 3)
        self.assertEqual(lay.Layout().page_at(0, 0), -1)
        self.assertEqual(lay.Layout().main_page(0, 100), -1)

    def test_a_thousand_pages_are_laid_out_quickly(self):
        import time
        started = time.perf_counter()
        layout = lay.build([A4] * 1000, 1.25, viewport_width=1400)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.2)
        self.assertEqual(len(layout.rects), 1000)
        started = time.perf_counter()
        for top in range(0, int(layout.height), 997):
            layout.pages_between(top, top + 900)
        self.assertLess(time.perf_counter() - started, 0.2)


class ZoomTest(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(lay.zoom_in(1.0), 1.1)
        self.assertEqual(lay.zoom_out(1.0), 0.9)
        self.assertEqual(lay.zoom_in(1.17), 1.25)
        self.assertEqual(lay.zoom_out(1.17), 1.1)
        self.assertEqual(lay.zoom_in(8.0), lay.ZOOM_MAX)
        self.assertEqual(lay.zoom_out(0.1), lay.ZOOM_MIN)
        self.assertEqual(lay.clamp_zoom(99), lay.ZOOM_MAX)
        self.assertEqual(lay.clamp_zoom(0), lay.ZOOM_MIN)

    def test_fit_width_and_page(self):
        width = lay.fit_zoom(lay.FIT_WIDTH, [A4] * 3, 0, 1, False, 1000, 700)
        self.assertAlmostEqual(width * 595 * lay.PX_PER_POINT, 1000 - 2 * lay.MARGIN, places=3)
        page = lay.fit_zoom(lay.FIT_PAGE, [A4] * 3, 0, 1, False, 1000, 700)
        self.assertAlmostEqual(page * 842 * lay.PX_PER_POINT, 700 - 2 * lay.MARGIN, places=3)
        two = lay.fit_zoom(lay.FIT_WIDTH, [A4] * 4, 0, 2, False, 1000, 700)
        self.assertAlmostEqual(two * 2 * 595 * lay.PX_PER_POINT, 1000 - 2 * lay.MARGIN - lay.GAP, places=3)
        rotated = lay.fit_zoom(lay.FIT_WIDTH, [A4], 1, 1, False, 1000, 700)
        self.assertAlmostEqual(rotated * 842 * lay.PX_PER_POINT, 1000 - 2 * lay.MARGIN, places=3)

    def test_fit_uses_the_widest_row_or_the_given_one(self):
        sizes = [A4, (1190.0, 842.0), A4]
        widest = lay.fit_zoom(lay.FIT_WIDTH, sizes, 0, 1, False, 1000, 700)
        self.assertAlmostEqual(widest * 1190 * lay.PX_PER_POINT, 968, places=3)
        first = lay.fit_zoom(lay.FIT_WIDTH, sizes, 0, 1, False, 1000, 700, row=0)
        self.assertAlmostEqual(first * 595 * lay.PX_PER_POINT, 968, places=3)
        self.assertEqual(lay.fit_zoom(lay.FIT_WIDTH, [], 0, 1, False, 1000, 700), 1.0)
        self.assertEqual(lay.fit_zoom(lay.FIT_WIDTH, [A4], 0, 1, False, 0, 0), 1.0)
        self.assertEqual(lay.fit_zoom(lay.FIT_WIDTH, [(1.0, 1.0)], 0, 1, False, 5000, 5000), lay.ZOOM_MAX)

    def test_points_and_rectangles_map_through_every_rotation(self):
        rect = (100.0, 50.0, 400.0, 800.0)
        size = (200.0, 400.0)
        for rotation in range(4):
            turned = rect if rotation % 2 == 0 else (100.0, 50.0, 800.0, 400.0)
            for point in ((0.0, 0.0), (200.0, 400.0), (50.0, 300.0)):
                x, y = lay.from_page(*point, turned, size, rotation)
                back = lay.to_page(x, y, turned, size, rotation)
                self.assertAlmostEqual(back[0], point[0], places=6)
                self.assertAlmostEqual(back[1], point[1], places=6)
        # A quarter turn clockwise puts the top left corner of the page at the top right of its rectangle.
        self.assertEqual(lay.from_page(0, 0, (0, 0, 400, 200), size, 1), (400.0, 0.0))
        self.assertEqual(lay.rect_from_page((0, 0, 100, 200), (0, 0, 400, 800), size, 0), (0.0, 0.0, 200.0, 400.0))
        self.assertEqual(lay.rect_from_page((0, 0, 100, 200), (0, 0, 800, 400), size, 1), (400.0, 0.0, 400.0, 200.0))

    def test_text_in_the_zoom_and_page_boxes(self):
        self.assertEqual(lay.zoom_text(1.1483), "115 %")
        self.assertEqual(lay.parse_zoom("150 %"), 1.5)
        self.assertEqual(lay.parse_zoom(" 75"), 0.75)
        self.assertEqual(lay.parse_zoom("62,5%"), 0.625)
        self.assertEqual(lay.parse_zoom("100000"), lay.ZOOM_MAX)
        for text in ("", "abc", "-5", "0", "nan", "1e999x"):
            self.assertIsNone(lay.parse_zoom(text), text)
        self.assertEqual(lay.parse_pages("1-3, 7", 10), [0, 1, 2, 6])
        self.assertEqual(lay.parse_pages("5", 10), [4])
        self.assertEqual(lay.parse_pages("2–4", 10), [1, 2, 3])
        for text in ("", "0", "3-1", "11", "1-11", "a", "1,,2", "1-", "-3"):
            self.assertIsNone(lay.parse_pages(text, 10), text)

    def test_history(self):
        history = lay.History(limit=3)
        self.assertFalse(history.can_back())
        self.assertIsNone(history.back((9, 0)))
        for position in ((0, 0), (0, 0), (5, 10), (7, 0), (9, 0)):
            history.push(position)
        self.assertEqual(history.back((20, 0)), (9, 0))
        self.assertEqual(history.back((9, 0)), (7, 0))
        self.assertTrue(history.can_forward())
        self.assertEqual(history.forward((7, 0)), (9, 0))
        self.assertEqual(history.back((9, 0)), (7, 0))
        self.assertEqual(history.back((7, 0)), (5, 10))
        self.assertIsNone(history.back((5, 10)))                           # the limit dropped the oldest
        history.push((1, 1))
        self.assertFalse(history.can_forward())


class SearchTest(unittest.TestCase):
    TEXT = "The Quick brown fox.\r\nA quick-witted FOX and a foxhound; quick\r\nbrown boxes. Naïve café 𝔘𝔫𝔦 fox"

    def find(self, query, **options):
        pattern = textsearch.compile_query(query, **options)
        return [] if pattern is None else textsearch.find_in_page(pattern, self.TEXT, 3)

    def test_case(self):
        self.assertEqual([hit.text for hit in self.find("quick")], ["Quick", "quick", "quick"])
        self.assertEqual([hit.text for hit in self.find("quick", match_case=True)], ["quick", "quick"])
        self.assertEqual(len(self.find("FOX", match_case=True)), 1)

    def test_whole_words(self):
        self.assertEqual(len(self.find("fox")), 4)
        self.assertEqual(len(self.find("fox", whole_words=True)), 3)
        self.assertEqual(len(self.find("quick", whole_words=True)), 3)       # "quick-witted" counts: "-" is no letter
        self.assertEqual(len(self.find("box", whole_words=True)), 0)
        self.assertEqual(len(self.find("fox.", whole_words=True)), 1)

    def test_a_query_is_plain_text(self):
        self.assertEqual(self.find(".*"), [])
        self.assertEqual(len(self.find("fox.")), 1)
        self.assertEqual(self.find("(quick"), [])
        self.assertIsNone(textsearch.compile_query("   "))
        self.assertIsNotNone(textsearch.compile_query("x" * 5000))

    def test_matches_run_over_line_breaks(self):
        (hit,) = self.find("quick brown boxes")
        self.assertEqual(hit.text, "quick brown boxes")
        self.assertEqual(self.TEXT[hit.start:hit.start + hit.length], "quick\r\nbrown boxes")
        self.assertEqual(len(self.find("quick   brown")), 2)

    def test_positions_count_utf16_units(self):
        (hit,) = self.find("café")
        self.assertEqual(hit.start, self.TEXT.index("café"))
        last = self.find("fox")[-1]
        self.assertEqual(last.start, self.TEXT.rindex("fox") + 3)            # three letters outside the BMP before it
        self.assertEqual(textsearch.utf16_length("a𝔘b"), 4)
        (astral,) = self.find("𝔘𝔫𝔦")
        self.assertEqual(astral.length, 6)

    def test_context(self):
        hit = self.find("foxhound")[0]
        self.assertEqual(hit.page, 3)
        self.assertIn("FOX and a ", hit.before)
        self.assertTrue(hit.after.startswith("; quick brown"))
        self.assertNotIn("\n", hit.line())
        self.assertNotIn("\r", hit.line())
        long_text = "word " * 100 + "needle" + " tail" * 100
        (hit,) = textsearch.find_in_page(textsearch.compile_query("needle"), long_text, 0)
        self.assertTrue(hit.before.startswith("…"))
        self.assertTrue(hit.after.endswith("…"))
        self.assertLess(len(hit.line()), 140)

    def test_limit_and_empty(self):
        pattern = textsearch.compile_query("a")
        self.assertEqual(len(textsearch.find_in_page(pattern, "a" * 5000, 0, limit=100)), 100)
        self.assertEqual(textsearch.find_in_page(pattern, "", 0), [])

    def test_stepping_wraps_and_says_so(self):
        self.assertEqual(textsearch.step(0, -1), (-1, False))
        self.assertEqual(textsearch.step(5, -1), (0, False))
        self.assertEqual(textsearch.step(5, -1, forward=False), (4, False))
        self.assertEqual(textsearch.step(5, 2), (3, False))
        self.assertEqual(textsearch.step(5, 4), (0, True))
        self.assertEqual(textsearch.step(5, 0, forward=False), (4, True))
        hits = [textsearch.Hit(page, 0, 1, "", "x", "") for page in (0, 0, 4, 9)]
        self.assertEqual(textsearch.first_at_or_after(hits, 3), 2)
        self.assertEqual(textsearch.first_at_or_after(hits, 0), 0)
        self.assertEqual(textsearch.first_at_or_after(hits, 50), 0)
        self.assertEqual(textsearch.first_at_or_after([], 0), -1)

    def test_words_and_lines(self):
        text = "one two-three\r\nfour  don't"
        self.assertEqual(text[slice(*textsearch.word_at(text, 5))], "two")
        self.assertEqual(text[slice(*textsearch.word_at(text, 7))], "-")
        self.assertEqual(text[slice(*textsearch.word_at(text, 23))], "don't")
        self.assertEqual(text[slice(*textsearch.line_at(text, 5))], "one two-three")
        self.assertEqual(text[slice(*textsearch.line_at(text, 17))], "four  don't")
        self.assertEqual(textsearch.word_at(text, 99), (99, 99))
        self.assertEqual(textsearch.line_at(text, -1), (-1, -1))


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_round_trip(self):
        store = Store(self.dir)
        store.add_recent("/docs/a.pdf", "Title A", 12)
        store.add_recent("/docs/b.pdf", "", 3)
        store.add_recent("/docs/a.pdf", "Title A", 12)
        store.set_view("/docs/a.pdf", {"page": 7, "zoom": 1.5, "mode": "custom", "rotation": 1, "continuous": False,
                                       "columns": 2, "cover": True, "offset": 0.25})
        store.sidebar, store.sidebar_tab = False, "outline"
        store.save()
        again = Store(self.dir)
        self.assertEqual([entry["path"] for entry in again.recent], ["/docs/a.pdf", "/docs/b.pdf"])
        self.assertEqual(again.recent[0]["title"], "Title A")
        self.assertEqual(again.view("/docs/a.pdf"), {"page": 7, "zoom": 1.5, "mode": "custom", "rotation": 1,
                                                     "continuous": False, "columns": 2, "cover": True, "offset": 0.25})
        self.assertIsNone(again.view("/docs/unknown.pdf"))
        self.assertEqual((again.sidebar, again.sidebar_tab), (False, "outline"))
        again.forget("/docs/a.pdf")
        self.assertEqual(len(again.recent), 1)
        self.assertIsNone(again.view("/docs/a.pdf"))

    def test_it_is_bounded(self):
        store = Store(self.dir)
        for index in range(MAX_DOCUMENTS + 50):
            store.add_recent(f"/docs/{index}.pdf")
            store.set_view(f"/docs/{index}.pdf", {"page": index})
        self.assertEqual(len(store.recent), MAX_RECENT)
        self.assertEqual(len(store.views), MAX_DOCUMENTS)
        self.assertIsNone(store.view("/docs/0.pdf"))                        # the oldest went first
        self.assertEqual(store.view(f"/docs/{MAX_DOCUMENTS + 49}.pdf")["page"], MAX_DOCUMENTS + 49)
        store.set_view("/docs/60.pdf", {"page": 1})                        # used again: now the newest
        for index in range(1000, 1000 + MAX_DOCUMENTS - 1):
            store.set_view(f"/docs/{index}.pdf", {"page": 1})
        self.assertIsNotNone(store.view("/docs/60.pdf"))
        store.save()
        self.assertLess(os.path.getsize(self.dir / "documents.json"), 100_000)

    def test_damaged_and_hostile_files_are_ignored(self):
        path = self.dir / "documents.json"
        for content in (b"", b"not json", b"[1, 2, 3]", b"\xff\xfe\x00", b"{" + b"x" * 3_000_000):
            path.write_bytes(content)
            store = Store(self.dir)
            self.assertEqual((store.recent, store.views), ([], {}))
        path.write_text(json.dumps({
            "recent": [{"path": "relative.pdf"}, {"path": "/ok.pdf", "title": 5, "pages": "many", "opened": "now"},
                       {"path": "/nul\u0000.pdf"}, "text", {"path": "/ok.pdf"}, {"path": "/" + "x" * 5000}],
            "views": {"/ok.pdf": {"page": -5, "zoom": 1e9, "mode": "sideways", "rotation": 7, "continuous": "yes",
                                  "columns": 9, "cover": 1, "offset": 5},
                      "relative": {"page": 1}, "/bad.pdf": "not a view", "/bool.pdf": {"page": True, "zoom": True}},
            "sidebar": "yes", "sidebar_tab": "nonsense"}))
        store = Store(self.dir)
        self.assertEqual(store.recent, [{"path": "/ok.pdf", "title": "", "pages": 0, "opened": 0.0}])
        self.assertEqual(store.view("/ok.pdf"), {"page": 0, "zoom": 1.0, "mode": "fit-width", "rotation": 3,
                                                 "continuous": True, "columns": 1, "cover": False, "offset": 0.0})
        self.assertEqual(store.view("/bool.pdf")["page"], 0)
        self.assertEqual(sorted(store.views), ["/bool.pdf", "/ok.pdf"])
        self.assertEqual((store.sidebar, store.sidebar_tab), (True, "thumbnails"))

    def test_without_a_directory_nothing_is_written(self):
        store = Store(None)
        store.add_recent("/a.pdf")
        store.save()
        self.assertEqual(len(store.recent), 1)


if __name__ == "__main__":
    unittest.main()
