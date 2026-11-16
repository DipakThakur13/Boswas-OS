"""Photos: the window, driven offscreen with real clicks, keys, wheel and drags.

Everything that would ask the user or the system (questions, file
choosers, starting programs, the compositor) is a method of the window
that the tests replace. Home, configuration, cache and data directories
are temporary; no network, no session bus.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import photos_fixtures as fx  # noqa: E402

from boswas_fluent import testing  # noqa: E402

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QSize, Qt, QUrl  # noqa: E402
from PySide6.QtGui import (QColor, QCursor, QDragEnterEvent, QDropEvent, QGuiApplication, QImage, QMouseEvent, QPainter,  # noqa: E402
                           QWheelEvent)
from PySide6.QtWidgets import QApplication  # noqa: E402

import boswas_fluent as fluent  # noqa: E402

from boswas_apps.photos import (APP_ID, decode, editops, exif, library, printing, settings,  # noqa: E402
                                thumbcache)
from boswas_apps.photos.__main__ import main  # noqa: E402
from boswas_apps.photos.ui import window as window_module  # noqa: E402
from boswas_apps.photos.ui.window import PhotosWindow  # noqa: E402

RED, GREEN, BLUE, WHITE = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)
NONE = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier


def rgb(image: QImage, x: int, y: int) -> tuple[int, int, int]:
    colour = image.pixelColor(x, y)
    return colour.red(), colour.green(), colour.blue()


def near(a, b, tolerance: int = 40) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def picture(width: int, height: int, colour=(120, 160, 200), quadrants: bool = False) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(*colour))
    if quadrants:
        painter = QPainter(image)
        for x, y, c in ((0, 0, RED), (width // 2, 0, GREEN), (0, height // 2, BLUE), (width // 2, height // 2, WHITE)):
            painter.fillRect(x, y, width // 2, height // 2, QColor(*c))
        painter.end()
    return image


def mouse(widget, kind, position, button=Qt.MouseButton.LeftButton, buttons=None, modifiers=NONE) -> None:
    point = QPointF(position)
    if buttons is None:
        buttons = button if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    QApplication.sendEvent(widget, QMouseEvent(kind, point, QPointF(widget.mapToGlobal(point.toPoint())), button,
                                               buttons, modifiers))


def drag(widget, start, end, steps: int = 6, modifiers=NONE) -> None:
    """Press at ``start``, move to ``end`` in steps, release."""
    start, end = QPointF(start), QPointF(end)
    mouse(widget, QEvent.Type.MouseButtonPress, start, modifiers=modifiers)
    for step in range(1, steps + 1):
        point = start + (end - start) * (step / steps)
        mouse(widget, QEvent.Type.MouseMove, point, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, modifiers)
    mouse(widget, QEvent.Type.MouseButtonRelease, end, modifiers=modifiers)
    testing.process()


def wheel(widget, position, delta: int, modifiers=NONE) -> None:
    point = QPointF(position)
    event = QWheelEvent(point, QPointF(widget.mapToGlobal(point.toPoint())), QPoint(0, 0), QPoint(0, delta),
                        Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)
    testing.process()


def drop(widget, mime: QMimeData) -> bool:
    """Drag ``mime`` onto ``widget`` and let go; True when the drop was taken."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        enter = QDragEnterEvent(QPoint(100, 100), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, NONE)
    QApplication.sendEvent(widget, enter)
    if not enter.isAccepted():
        return False
    event = QDropEvent(QPointF(100, 100), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, NONE)
    QApplication.sendEvent(widget, event)
    testing.process()
    return event.isAccepted()


class PhotosCase(testing.FluentTestCase):
    """A temporary home with a Pictures folder, and a window whose questions are answered here."""

    def setUp(self) -> None:
        super().setUp()
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.tmp)
        self.pictures = self.tmp / "Pictures"
        self.pictures.mkdir()
        fluent.manager().set_reduced_motion(True)          # animations are tested where they matter
        self.questions: list[tuple[str, str]] = []
        self.answer = True
        self.save_to: str | None = None
        self.save_requests: list[tuple[str, str, str]] = []
        self.open_answer: list[str] = []
        self.folder_answer: str | None = None
        self.programs: list[list[str]] = []
        self.shown_in_files: list[str] = []
        self.menus: list = []
        self.drags: list = []
        self.compositor_calls: list[list[str]] = []
        self.dialog_script = None

    def tearDown(self) -> None:
        for window in list(window_module._windows):
            window.close()
        fluent.manager().set_reduced_motion(None)
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home
        super().tearDown()

    # --- files ---
    def add(self, name: str, image: QImage | None = None, *, age_days: float | None = None, quality: int = 90) -> str:
        path = self.pictures / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue((image or picture(320, 240)).save(str(path), None, quality), name)
        if age_days is not None:
            stamp = time.time() - age_days * 86400
            os.utime(path, (stamp, stamp))
        return str(path)

    def trashed(self) -> list[str]:
        folder = self.tmp / "xdg_data_home" / "Trash" / "files"
        return sorted(os.listdir(folder)) if folder.is_dir() else []

    # --- the window ---
    def window(self, arguments: list[str] | None = None, size=(1100, 720)) -> PhotosWindow:
        window = PhotosWindow(list(arguments or []))
        window.ask = self._ask
        window.choose_save_path = self._choose_save
        window.choose_open_paths = lambda: list(self.open_answer)
        window.choose_folder = lambda: self.folder_answer
        window.run_program = lambda arguments: self.programs.append(arguments) or True
        window.show_in_files = lambda path: self.shown_in_files.append(path) or True
        window.popup_menu = lambda menu, position: self.menus.append(menu)
        window.run_drag = self.drags.append
        window.compositor_runner = self._compositor
        window.run_dialog = self._run_dialog
        self.show(window, *size)
        return window

    def _ask(self, title, text, confirm="OK", cancel="Cancel", *, default="cancel") -> bool:
        self.questions.append((title, text))
        return self.answer

    def _choose_save(self, title, suggested, fmt):
        self.save_requests.append((title, suggested, fmt))
        return self.save_to or suggested

    def _compositor(self, arguments, **options):
        self.compositor_calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0, b"{}", b"")

    def _run_dialog(self, dialog):
        dialog.open()
        testing.process(20)
        try:
            if self.dialog_script is not None:
                self.dialog_script(dialog)
        finally:
            if dialog.is_open():
                dialog.reject()
        return dialog.result()

    def wait(self, condition, timeout: float = 15.0, message: str = "") -> None:
        self.assertTrue(testing.wait_until(condition, timeout), message or "timed out waiting")

    def viewer(self, path: str, size=(1100, 720)) -> PhotosWindow:
        """A window showing ``path``, decoded, with its folder listed."""
        window = self.window([path], size)
        self.loaded(window, path)
        folder_count = len(library.folder_items(os.path.dirname(path), decode.supported_extensions(), path))
        self.wait(lambda: len(window.viewer.items()) == folder_count)
        return window

    def loaded(self, window: PhotosWindow, path: str | None = None) -> None:
        viewer = window.viewer
        self.wait(lambda: (viewer.loaded() is not None or viewer.error() is not None)
                  and (path is None or viewer.current_path() == path), message=f"not loaded: {path}")
        self.wait(lambda: viewer.file_worker.pending() == 0 and viewer.canvas.is_settled())

    def press(self, window: PhotosWindow, key, modifiers=NONE) -> None:
        testing.key(QApplication.focusWidget() or window, key, modifiers)

    def banner(self, window: PhotosWindow) -> str:
        return window.viewer.banner.message() if window.viewer.banner.isVisible() else ""


class ViewerNavigationTest(PhotosCase):
    def test_a_file_opens_with_its_folder_in_natural_order(self):
        for name in ("IMG_10.png", "IMG_2.png", "img_1.png", "notes.txt", ".hidden.png"):
            if name.endswith(".png"):
                self.add(name)
            else:
                (self.pictures / name).write_text("x")
        window = self.viewer(str(self.pictures / "IMG_2.png"))
        viewer = window.viewer
        self.assertEqual(window.current_page(), "viewer")
        self.assertEqual([item.name for item in viewer.items()], ["img_1.png", "IMG_2.png", "IMG_10.png"])
        self.assertEqual(viewer.index(), 1)
        self.assertEqual(viewer.name_label.full_text(), "IMG_2.png")
        self.assertEqual(viewer.position_label.text(), "2 of 3")
        self.assertEqual(window.windowTitle(), "IMG_2.png - Photos")
        self.assertEqual(viewer.filmstrip.count(), 3)
        self.assertEqual(viewer.filmstrip.current(), 1)
        self.assertEqual(viewer.zoom_label.text(), "100 %")              # smaller than the window: not blown up

    def test_arrow_keys_chevrons_mouse_buttons_and_filmstrip(self):
        paths = [self.add(f"p{index}.png", picture(300, 200, (index * 60, 0, 0))) for index in range(4)]
        window = self.viewer(paths[0])
        viewer = window.viewer
        self.press(window, Qt.Key.Key_Right)
        self.loaded(window, paths[1])
        self.assertEqual(rgb(viewer.loaded().image, 5, 5), (60, 0, 0))
        testing.click(viewer.next_button)
        self.loaded(window, paths[2])
        testing.click(viewer.previous_button)
        self.loaded(window, paths[1])
        self.press(window, Qt.Key.Key_Left)
        self.loaded(window, paths[0])
        self.press(window, Qt.Key.Key_Left)                              # round the corner
        self.loaded(window, paths[3])
        self.press(window, Qt.Key.Key_Home)
        self.loaded(window, paths[0])
        self.press(window, Qt.Key.Key_End)
        self.loaded(window, paths[3])
        mouse(viewer.canvas, QEvent.Type.MouseButtonPress, QPoint(50, 50), Qt.MouseButton.BackButton)
        self.loaded(window, paths[2])
        mouse(viewer.canvas, QEvent.Type.MouseButtonPress, QPoint(50, 50), Qt.MouseButton.ForwardButton)
        self.loaded(window, paths[3])
        strip = viewer.filmstrip
        testing.click(strip, strip.cell_rect(1).center())
        self.loaded(window, paths[1])
        self.assertEqual(strip.current(), 1)
        self.assertEqual(viewer.position_label.text(), "2 of 4")
        window.thumbs.wait(10)
        self.assertIsNotNone(window.thumbs.pixmap(paths[0], os.stat(paths[0]).st_mtime))

    def test_a_single_picture_has_no_chevrons(self):
        window = self.viewer(self.add("only.png"))
        self.assertFalse(window.viewer.previous_button.isVisible())
        self.assertFalse(window.viewer.next_button.isVisible())
        self.assertEqual(window.viewer.position_label.text(), "")
        self.press(window, Qt.Key.Key_Right)
        self.assertEqual(window.viewer.index(), 0)

    def test_several_files_and_a_folder_from_the_command_line(self):
        a, b = self.add("z-first.png"), self.add("a-second.png")
        self.add("sub/inside2.png")
        self.add("sub/inside10.png")
        window = self.window([a, b, str(self.pictures / "sub")])
        self.wait(lambda: len(window.viewer.items()) == 4)
        self.assertEqual([item.name for item in window.viewer.items()],
                         ["z-first.png", "a-second.png", "inside2.png", "inside10.png"])     # the order given
        window.open_paths([str(self.pictures / "sub")])
        self.wait(lambda: [item.name for item in window.viewer.items()] == ["inside2.png", "inside10.png"])
        (self.pictures / "empty").mkdir()
        window.open_paths([str(self.pictures / "empty")])
        self.wait(lambda: window.viewer.message.isVisible())
        self.assertEqual(window.viewer.message.title_label.text(), "No pictures here")
        self.assertFalse(window.viewer.edit_button.isEnabled())

    def test_the_mouse_wheel_can_go_to_the_next_picture(self):
        paths = [self.add(f"w{index}.png") for index in range(3)]
        window = self.viewer(paths[0])
        canvas = window.viewer.canvas
        self.assertEqual(canvas.wheel_mode, "zoom")
        wheel(canvas, QPoint(200, 200), -120)
        self.assertEqual(window.viewer.index(), 0)
        self.assertGreater(1.0, 0)                                       # (zooming is tested below)
        canvas.zoom_fit(animate=False)
        window.show_settings()
        page = window.settings_page
        page.wheel_box.setCurrentIndex(page.wheel_box.findData("navigate"))
        self.assertEqual(settings.load(window.settings_path).wheel, "navigate")
        window.show_viewer()
        wheel(canvas, QPoint(200, 200), -120)
        self.loaded(window, paths[1])
        wheel(canvas, QPoint(200, 200), 120)
        self.loaded(window, paths[0])
        before = canvas.zoom()
        wheel(canvas, QPoint(200, 200), 240, CTRL)                       # Ctrl with the wheel still zooms
        self.assertGreater(canvas.zoom(), before)
        self.assertEqual(window.viewer.index(), 0)


class ViewerZoomTest(PhotosCase):
    def open_large(self) -> PhotosWindow:
        return self.viewer(self.add("large.png", picture(2400, 1600, quadrants=True)))

    def test_fitted_by_default_and_follows_the_window(self):
        window = self.open_large()
        canvas = window.viewer.canvas
        self.assertTrue(canvas.is_fitted())
        self.assertAlmostEqual(canvas.zoom(), canvas.fit_zoom())
        rect = canvas.picture_rect()
        self.assertTrue(rect.width() <= canvas.width() + 0.5 and rect.height() <= canvas.height() + 0.5)
        self.assertAlmostEqual(rect.center().x(), canvas.width() / 2, delta=1)
        before = canvas.zoom()
        window.resize(800, 560)
        testing.process(30)
        self.assertTrue(canvas.is_fitted())
        self.assertLess(canvas.zoom(), before)
        self.assertEqual(window.viewer.zoom_label.text(), f"{round(canvas.zoom() * 100)} %")

    def test_ctrl_wheel_zooms_around_the_pointer(self):
        window = self.open_large()
        canvas = window.viewer.canvas
        fitted = canvas.zoom()
        wheel(canvas, QPointF(canvas.rect().center()), 120, CTRL)
        self.assertAlmostEqual(canvas.zoom(), fitted * 1.0015 ** 120, places=5)
        self.assertFalse(canvas.is_fitted())
        canvas.zoom_actual(animate=False)                               # larger than the window on both sides
        anchor = QPointF(canvas.width() * 0.3, canvas.height() * 0.6)
        under = canvas.to_picture(anchor)
        for _ in range(4):
            wheel(canvas, anchor, 120, CTRL)
        self.assertAlmostEqual(canvas.zoom(), 1.0015 ** 480, places=4)
        after = canvas.to_picture(anchor)
        self.assertAlmostEqual(after.x(), under.x(), delta=0.5)         # the point under the pointer stayed there
        self.assertAlmostEqual(after.y(), under.y(), delta=0.5)
        wheel(canvas, anchor, -240, CTRL)
        self.assertAlmostEqual(canvas.to_picture(anchor).x(), under.x(), delta=0.5)
        for _ in range(40):
            wheel(canvas, anchor, -120, CTRL)
        self.assertAlmostEqual(canvas.zoom(), canvas.fit_zoom())         # never smaller than fitted
        self.assertTrue(canvas.is_fitted())
        for _ in range(80):
            wheel(canvas, anchor, 120, CTRL)
        self.assertAlmostEqual(canvas.zoom(), canvas.max_zoom())

    def test_zoom_is_animated_unless_motion_is_reduced(self):
        window = self.open_large()
        canvas = window.viewer.canvas
        fluent.manager().set_reduced_motion(False)
        start = canvas.zoom()
        wheel(canvas, QPoint(300, 300), 120, CTRL)
        self.assertLess(canvas.zoom(), start * 1.0015 ** 120)           # on its way
        self.wait(lambda: abs(canvas.zoom() - start * 1.0015 ** 120) < 1e-6)
        fluent.manager().set_reduced_motion(True)
        wheel(canvas, QPoint(300, 300), 120, CTRL)
        self.assertAlmostEqual(canvas.zoom(), start * 1.0015 ** 240, places=5)

    def test_double_click_buttons_and_keys(self):
        window = self.open_large()
        viewer, canvas = window.viewer, window.viewer.canvas
        fitted = canvas.zoom()
        point = QPointF(canvas.width() * 0.75, canvas.height() * 0.25)
        under = canvas.to_picture(point)
        mouse(canvas, QEvent.Type.MouseButtonDblClick, point)
        self.assertAlmostEqual(canvas.zoom(), 1.0)
        self.assertAlmostEqual(canvas.to_picture(point).x(), under.x(), delta=1.0)
        self.assertEqual(viewer.zoom_label.text(), "100 %")
        mouse(canvas, QEvent.Type.MouseButtonDblClick, point)
        self.assertAlmostEqual(canvas.zoom(), fitted)
        self.assertTrue(canvas.is_fitted())
        testing.click(viewer.commands.button(viewer.zoom_in_action))
        self.assertAlmostEqual(canvas.zoom(), fitted * 1.25)
        testing.click(viewer.commands.button(viewer.zoom_out_action))
        self.assertTrue(canvas.is_fitted())
        testing.click(viewer.commands.button(viewer.actual_action))
        self.assertAlmostEqual(canvas.zoom(), 1.0)
        testing.click(viewer.commands.button(viewer.fit_action))
        self.assertTrue(canvas.is_fitted())
        self.press(window, Qt.Key.Key_1, CTRL)
        self.assertAlmostEqual(canvas.zoom(), 1.0)
        self.press(window, Qt.Key.Key_Plus, CTRL)
        self.assertAlmostEqual(canvas.zoom(), 1.25)
        self.press(window, Qt.Key.Key_Minus, CTRL)
        self.assertAlmostEqual(canvas.zoom(), 1.0)
        self.press(window, Qt.Key.Key_0, CTRL)
        self.assertTrue(canvas.is_fitted())
        self.press(window, Qt.Key.Key_1, CTRL)
        self.press(window, Qt.Key.Key_Escape)                            # Escape goes back to fitted first
        self.assertTrue(canvas.is_fitted())
        self.assertEqual(window.current_page(), "viewer")

    def test_a_small_picture_is_shown_at_its_size_and_can_be_enlarged(self):
        window = self.viewer(self.add("small.png", picture(120, 80)))
        canvas = window.viewer.canvas
        self.assertAlmostEqual(canvas.zoom(), 1.0)
        self.assertEqual(canvas.picture_rect().size().toSize(), QSize(120, 80))
        mouse(canvas, QEvent.Type.MouseButtonDblClick, QPointF(canvas.rect().center()))
        self.assertAlmostEqual(canvas.zoom(), canvas.fit_zoom())
        self.assertGreater(canvas.zoom(), 4)

    def test_dragging_pans_a_zoomed_picture_and_stops_at_its_edges(self):
        window = self.open_large()
        canvas = window.viewer.canvas
        canvas.zoom_actual(animate=False)
        self.assertTrue(canvas.can_pan())
        centre = QPointF(canvas.rect().center())
        before = canvas.to_picture(centre)
        drag(canvas, centre, centre + QPointF(-150, -90))
        after = canvas.to_picture(centre)
        self.assertAlmostEqual(after.x() - before.x(), 150, delta=1.5)
        self.assertAlmostEqual(after.y() - before.y(), 90, delta=1.5)
        self.assertEqual(self.drags, [])
        for _ in range(12):
            drag(canvas, QPoint(100, 100), QPoint(canvas.width() - 20, canvas.height() - 20))
        rect = canvas.picture_rect()
        self.assertAlmostEqual(rect.left(), 0, delta=0.5)                # no gap opens beside the picture
        self.assertAlmostEqual(rect.top(), 0, delta=0.5)

    def test_pinch(self):
        window = self.open_large()
        canvas = window.viewer.canvas
        canvas.zoom_actual(animate=False)
        centre = QPointF(canvas.width() * 0.6, canvas.height() * 0.4)
        under = canvas.to_picture(centre)
        start = canvas.zoom()
        for _ in range(5):
            canvas.pinch(1.1, centre)
        self.assertAlmostEqual(canvas.zoom(), start * 1.1 ** 5, places=5)
        self.assertAlmostEqual(canvas.to_picture(centre).x(), under.x(), delta=1.0)
        self.assertAlmostEqual(canvas.to_picture(centre).y(), under.y(), delta=1.0)
        self.assertTrue(canvas.testAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents))

    def test_fast_while_moving_fine_when_at_rest(self):
        window = self.open_large()
        canvas = window.viewer.canvas
        self.assertTrue(canvas.has_fine_rendering())                     # fitted: shown smaller than it is
        wheel(canvas, QPoint(300, 300), -120, CTRL)
        wheel(canvas, QPoint(300, 300), 60, CTRL)
        self.assertFalse(canvas.is_settled())
        self.assertFalse(canvas.has_fine_rendering())                    # while zooming: the fast picture
        grab_fast = canvas.grab().toImage()
        self.wait(canvas.is_settled)
        self.assertTrue(canvas.has_fine_rendering())
        grab_fine = canvas.grab().toImage()
        rect = canvas.picture_rect()
        x, y = int(rect.left() + rect.width() * 0.25), int(rect.top() + rect.height() * 0.25)
        self.assertEqual(rgb(grab_fast, x, y), RED)
        self.assertEqual(rgb(grab_fine, x, y), RED)
        self.assertEqual(rgb(grab_fine, int(rect.left() + rect.width() * 0.75), y), GREEN)
        canvas.zoom_actual(animate=False)
        self.wait(canvas.is_settled)
        self.assertFalse(canvas.has_fine_rendering())                    # at 100 % the painter needs no help


class ViewerFilesTest(PhotosCase):
    def test_rotate_turns_the_jpeg_on_disk_without_reencoding(self):
        path = self.add("turn.jpg", picture(400, 200, quadrants=True), quality=95)
        scan = Path(path).read_bytes()
        scan = scan[scan.index(b"\xff\xda"):]
        window = self.viewer(path)
        viewer = window.viewer
        self.press(window, Qt.Key.Key_R, CTRL)
        self.wait(lambda: viewer.file_worker.pending() == 0)
        self.assertEqual(viewer.loaded().image.size(), QSize(200, 400))
        self.assertTrue(near(rgb(viewer.loaded().image, 150, 50), RED))
        self.assertEqual(exif.read_file(path).orientation, 6)
        self.assertTrue(Path(path).read_bytes().endswith(scan))
        testing.click(viewer.commands.button(viewer.rotate_action))
        self.wait(lambda: viewer.file_worker.pending() == 0)
        self.assertEqual(exif.read_file(path).orientation, 3)
        self.assertEqual(viewer.canvas.natural_size(), QSize(400, 200))
        self.press(window, Qt.Key.Key_R, CTRL | SHIFT)
        self.wait(lambda: viewer.file_worker.pending() == 0)
        self.assertEqual(exif.read_file(path).orientation, 6)
        self.assertEqual(decode.load(path).image.size(), QSize(200, 400))
        self.assertEqual(self.banner(window), "")

    def test_rotate_is_for_viewing_only_where_the_file_cannot_be_turned(self):
        if "webp" not in decode.supported_extensions():
            self.skipTest("no WebP plugin")
        path = self.add("turn.webp", picture(400, 200, quadrants=True), quality=100)
        before = Path(path).read_bytes()
        window = self.viewer(path)
        self.press(window, Qt.Key.Key_R, CTRL)
        self.assertEqual(window.viewer.loaded().image.size(), QSize(200, 400))
        self.assertIn("for viewing only", self.banner(window))
        self.assertEqual(Path(path).read_bytes(), before)
        gif = str(fx.write(self.pictures / "anim.gif", fx.gif([RED, GREEN, BLUE], 16, 12)))
        window.open_paths([gif])
        self.loaded(window, gif)
        self.assertFalse(window.viewer.rotate_action.isEnabled())

    def test_delete_moves_to_the_trash_shows_the_next_and_can_be_undone(self):
        paths = [self.add(f"d{index}.png", picture(200, 100, (0, index * 80, 0))) for index in range(3)]
        window = self.viewer(paths[1])
        viewer = window.viewer
        self.press(window, Qt.Key.Key_Delete)
        self.wait(lambda: not os.path.exists(paths[1]))
        self.assertEqual(self.questions[0][0], "Move to the Trash?")
        self.assertIn("d1.png", self.questions[0][1])
        self.loaded(window, paths[2])                                    # the next picture takes its place
        self.assertEqual(self.trashed(), ["d1.png"])
        self.assertEqual([item.name for item in viewer.items()], ["d0.png", "d2.png"])
        self.assertEqual(viewer.position_label.text(), "2 of 2")
        self.assertIn("moved to the Trash", self.banner(window))
        testing.click(viewer.banner.action_button)                       # Undo
        self.wait(lambda: os.path.exists(paths[1]))
        self.loaded(window, paths[1])
        self.assertEqual([item.name for item in viewer.items()], ["d0.png", "d1.png", "d2.png"])
        self.assertEqual(self.trashed(), [])

    def test_delete_can_be_declined_and_need_not_ask(self):
        paths = [self.add("keep.png"), self.add("zap.png")]
        window = self.viewer(paths[0])
        self.answer = False
        testing.click(window.viewer.commands.button(window.viewer.delete_action))
        testing.process(50)
        self.assertTrue(os.path.exists(paths[0]))
        window.settings.confirm_delete = False
        self.questions.clear()
        self.press(window, Qt.Key.Key_Delete)
        self.wait(lambda: not os.path.exists(paths[0]))
        self.assertEqual(self.questions, [])
        self.loaded(window, paths[1])
        self.press(window, Qt.Key.Key_Delete)
        self.wait(lambda: not os.path.exists(paths[1]))
        self.wait(lambda: window.viewer.message.isVisible())
        self.assertEqual(window.viewer.message.title_label.text(), "No more pictures")
        self.assertEqual(self.trashed(), ["keep.png", "zap.png"])

    def test_copy_puts_the_picture_and_its_address_on_the_clipboard(self):
        path = self.add("copy me.png", picture(64, 48, (10, 200, 30)))
        window = self.viewer(path)
        self.press(window, Qt.Key.Key_C, CTRL)
        data = QGuiApplication.clipboard().mimeData()
        self.assertEqual([url.toLocalFile() for url in data.urls()], [path])
        self.assertTrue(data.hasImage())
        image = QImage(data.imageData())
        self.assertEqual(image.size(), QSize(64, 48))
        self.assertEqual(rgb(image, 5, 5), (10, 200, 30))
        self.assertEqual(bytes(data.data("x-special/gnome-copied-files")),
                         b"copy\nfile://" + str(self.pictures).encode() + b"/copy%20me.png")
        self.press(window, Qt.Key.Key_C, CTRL | SHIFT)
        self.assertEqual(QGuiApplication.clipboard().text(), path)

    def test_the_picture_can_be_dragged_out_and_files_dropped_in(self):
        path = self.add("drag.png", picture(300, 200))
        other = self.add("other/dropped.png", picture(100, 100, (9, 9, 9)))
        window = self.viewer(path)
        canvas = window.viewer.canvas
        centre = QPointF(canvas.rect().center())
        drag(canvas, centre, centre + QPointF(60, 30))
        self.assertEqual(len(self.drags), 1)
        mime = self.drags[0].mimeData()
        self.assertEqual([url.toLocalFile() for url in mime.urls()], [path])
        self.assertTrue(mime.hasImage())
        drag(canvas, QPointF(5, 5), QPointF(60, 60))                     # beside the picture: nothing to drag
        self.assertEqual(len(self.drags), 1)
        dropped = QMimeData()
        dropped.setUrls([QUrl.fromLocalFile(other), QUrl("https://example.org/x.png")])
        self.assertTrue(drop(window, dropped))
        self.loaded(window, other)
        text_only = QMimeData()
        text_only.setText("hello")
        self.assertFalse(drop(window, text_only))
        self.assertEqual(window.viewer.current_path(), other)

    def test_file_info_shows_exif_and_renames_in_place(self):
        plain = self.add("cam.jpg", picture(400, 300), quality=90)
        Path(plain).write_bytes(fx.jpeg_with_block(Path(plain).read_bytes(), fx.camera_exif(1)))
        self.add("taken.jpg")
        window = self.viewer(plain)
        viewer, pane = window.viewer, window.viewer.info_pane
        self.assertFalse(pane.isVisible())
        self.press(window, Qt.Key.Key_Return, ALT)
        self.assertTrue(pane.isVisible())
        self.assertTrue(viewer.info_action.isChecked())
        rows = pane.rows()
        self.assertEqual(rows["Folder"], str(self.pictures))
        self.assertEqual(rows["Dimensions"], "400 × 300 pixels")
        self.assertEqual(rows["Megapixels"], "0.1 MP")
        self.assertEqual(rows["Resolution"], "300 dpi")
        self.assertEqual(rows["Camera"], "Boswas Prism One")
        self.assertEqual(rows["Lens"], "Boswas 24-70mm F2.8")
        self.assertEqual((rows["Exposure"], rows["Aperture"], rows["ISO"]), ("1/250 s", "f/2.8", "200"))
        self.assertEqual(rows["Focal length"], "50 mm (75 mm in 35 mm terms)")
        self.assertEqual(rows["Flash"], "Fired, automatic")
        self.assertEqual(rows["Date taken"], "14 August 2026, 18:23")
        self.assertEqual(rows["Coordinates"], "48.85837° N, 2.29448° E")
        self.assertTrue(pane.map_button.isVisible())
        self.assertIn("mlat=48.85837", pane.map_button.url())
        # rename: F2, type, Enter
        self.press(window, Qt.Key.Key_F2)
        self.assertTrue(pane.is_renaming())
        self.assertEqual(pane.name_edit.selectedText(), "cam")           # the extension is left alone
        testing.type_text(pane.name_edit, "holiday")
        testing.key(pane.name_edit, Qt.Key.Key_Return)
        self.wait(lambda: os.path.exists(self.pictures / "holiday.jpg"))
        self.wait(lambda: viewer.current_path() == str(self.pictures / "holiday.jpg"))
        self.assertFalse(os.path.exists(plain))
        self.assertEqual(viewer.name_label.full_text(), "holiday.jpg")
        self.assertEqual([item.name for item in viewer.items()], ["holiday.jpg", "taken.jpg"])
        self.assertFalse(pane.is_renaming())
        # a name that is taken, a name that cannot be: said in the pane, nothing renamed
        pane.start_rename()
        pane.name_edit.setText("taken.jpg")
        testing.key(pane.name_edit, Qt.Key.Key_Return)
        self.wait(lambda: pane.name_error.isVisible())
        self.assertIn("already here", pane.name_error.text())
        pane.name_edit.setText("a/b.jpg")
        testing.key(pane.name_edit, Qt.Key.Key_Return)
        self.assertIn("slash", pane.name_error.text())
        testing.key(pane.name_edit, Qt.Key.Key_Escape)
        self.assertFalse(pane.is_renaming())
        self.assertTrue(os.path.exists(self.pictures / "holiday.jpg"))
        testing.click(pane.close_button)
        self.assertFalse(pane.isVisible())
        self.assertFalse(viewer.info_action.isChecked())
        testing.click(viewer.commands.button(viewer.info_action))
        self.assertTrue(pane.isVisible())

    def test_save_as_converts_and_says_what_is_lost(self):
        plain = self.add("cam.jpg", picture(200, 100, quadrants=True), quality=95)
        Path(plain).write_bytes(fx.jpeg_with_block(Path(plain).read_bytes(), fx.camera_exif(1)))
        window = self.viewer(plain)
        self.save_to = str(self.pictures / "converted.png")
        self.press(window, Qt.Key.Key_S, CTRL)
        self.wait(lambda: os.path.exists(self.save_to) and window.viewer.file_worker.pending() == 0)
        self.assertEqual(self.save_requests[0][:2], ("Save as", str(self.pictures / "cam (2).jpg")))
        saved = decode.load(self.save_to)
        self.assertEqual((saved.info.format, saved.image.size()), ("png", QSize(200, 100)))
        self.assertIn("Saved “converted.png”", self.banner(window))
        self.assertIn("Not kept: camera details", self.banner(window))
        self.wait(lambda: "converted.png" in [item.name for item in window.viewer.items()])
        self.save_to = str(self.pictures / "same kind.jpg")              # the same format: a plain copy
        window.viewer.save_as()
        self.wait(lambda: os.path.exists(self.save_to) and window.viewer.file_worker.pending() == 0)
        self.assertEqual(Path(self.save_to).read_bytes(), Path(plain).read_bytes())
        self.save_to = str(self.pictures / "no-extension")
        window.viewer.save_as()
        testing.process(50)
        self.assertIn("ends in .jpg", self.banner(window))
        self.assertFalse(os.path.exists(self.save_to))

    def test_resize_dialog_saves_a_smaller_copy(self):
        path = self.add("big.png", picture(800, 400, quadrants=True))
        window = self.viewer(path)
        seen = {}

        def script(dialog):
            self.assertEqual(dialog.new_size(), QSize(800, 400))
            dialog.width_box.setValue(200)                               # the height follows
            self.assertEqual(dialog.new_size(), QSize(200, 100))
            dialog.keep.setChecked(False)
            dialog.height_box.setValue(150)
            self.assertEqual(dialog.new_size(), QSize(200, 150))
            dialog.keep.setChecked(True)
            self.assertEqual(dialog.new_size(), QSize(200, 100))
            dialog.units.set_current("percent")
            self.assertTrue(dialog.percent_box.isVisible() and not dialog.width_box.isVisible())
            dialog.percent_box.setValue(50)
            self.assertEqual(dialog.new_size(), QSize(400, 200))
            dialog.format_box.setCurrentIndex(dialog.format_box.findData("jpeg"))
            self.assertTrue(dialog.quality.isVisible())
            dialog.quality.setValue(60)
            self.wait(lambda: "about" in dialog.new_label.text(), 10)
            seen["label"] = dialog.new_label.text()
            dialog.units.set_current("pixels")
            dialog.keep.setChecked(False)
            dialog.width_box.setValue(30000)
            dialog.height_box.setValue(30000)
            self.assertFalse(dialog.primary_button.isEnabled())          # more pixels than Photos saves
            self.assertTrue(dialog.problem_label.isVisible())
            dialog.width_box.setValue(400)
            dialog.height_box.setValue(200)
            testing.click(dialog.primary_button)

        self.dialog_script = script
        self.save_to = str(self.pictures / "smaller.jpg")
        window.viewer.resize_picture()
        self.wait(lambda: os.path.exists(self.save_to) and window.viewer.file_worker.pending() == 0)
        self.assertTrue(seen["label"].startswith("New: 400 × 200 pixels, about "))
        self.assertEqual(self.save_requests[0][1], str(self.pictures / "big (400x200).jpg"))
        saved = decode.load(self.save_to)
        self.assertEqual(saved.image.size(), QSize(400, 200))
        self.assertTrue(near(rgb(saved.image, 50, 50), RED))
        self.assertEqual(decode.load(path).image.size(), QSize(800, 400))        # the original is untouched
        self.dialog_script = None                                        # cancelled: nothing happens
        count = len(self.save_requests)
        window.viewer.resize_picture()
        self.assertEqual(len(self.save_requests), count)

    @unittest.skipUnless(printing.AVAILABLE, "QtPrintSupport is not installed")
    def test_print_to_a_pdf_file(self):
        window = self.viewer(self.add("print.png", picture(600, 400, quadrants=True)))

        def script(dialog):
            self.wait(lambda: not dialog.note.isVisible() or "Looking" not in dialog.note.text(), 10)
            self.assertTrue(dialog.preview.landscape)                    # automatic: the picture is wide
            dialog.orientation_box.setCurrentIndex(dialog.orientation_box.findData(printing.PORTRAIT))
            self.assertFalse(dialog.preview.landscape)
            dialog.mode_box.setCurrentIndex(dialog.mode_box.findData(printing.FILL))
            self.assertEqual(dialog.preview.mode, printing.FILL)
            dialog.printer_box.setCurrentIndex(dialog.printer_box.findData(printing.PDF_PRINTER))
            self.assertEqual(dialog.primary_button.text(), "Save as PDF")
            self.assertFalse(dialog.copies_box.isEnabled())
            dialog.preview.grab()
            testing.click(dialog.primary_button)

        self.dialog_script = script
        self.save_to = str(self.pictures / "out.pdf")
        self.press(window, Qt.Key.Key_P, CTRL)
        self.wait(lambda: os.path.exists(self.save_to) and window.viewer.file_worker.pending() == 0)
        self.assertEqual(self.save_requests[0], ("Save as PDF", str(self.pictures / "print.pdf"), "pdf"))
        self.assertTrue(Path(self.save_to).read_bytes().startswith(b"%PDF-"))
        self.assertIn("Saved “out.pdf”", self.banner(window))

    def test_set_as_desktop_background(self):
        config = self.tmp / "xdg_config_home" / "boswas" / "compositor.toml"
        fx.write(config, b'# mine\n[appearance]\nscheme = "light"\n\n[input]\nnatural_scroll = true\n')
        path = self.add("wall.png", picture(640, 360, (20, 40, 60)))
        window = self.viewer(path)
        window.viewer.commands._fill_menu()
        self.assertIn(window.viewer.wallpaper_action, window.viewer.commands.more_menu.actions())
        window.viewer.wallpaper_action.trigger()
        self.wait(lambda: window.viewer.file_worker.pending() == 0 and bool(self.banner(window)))
        self.assertEqual(self.banner(window), "This picture is now the desktop background.")
        self.assertEqual(self.compositor_calls, [["boswas-compositor", "--send", "reload-config"]])
        copies = list((self.tmp / "xdg_data_home" / "boswas" / "wallpapers").iterdir())
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), Path(path).read_bytes())
        self.assertEqual(config.read_text(), '# mine\n[appearance]\nscheme = "light"\n'
                                             f'wallpaper = "{copies[0]}"\n\n[input]\nnatural_scroll = true\n')
        # a desktop that is not running: said, not hidden
        window.compositor_runner = lambda arguments, **options: (_ for _ in ()).throw(FileNotFoundError(arguments[0]))
        window.viewer.set_as_wallpaper()
        self.wait(lambda: "next time" in self.banner(window))
        # a configuration that cannot be edited safely: an error, and the file as it was
        config.write_text('appearance = { scheme = "dark" }\n')
        window.viewer.set_as_wallpaper()
        self.wait(lambda: "was not changed" in self.banner(window))
        self.assertEqual(config.read_text(), 'appearance = { scheme = "dark" }\n')

    def test_open_with_show_in_files_and_the_context_menu(self):
        program = fx.write(self.tmp / "bin" / "othergimp", b"#!/bin/sh\n")
        os.chmod(program, 0o755)
        apps = self.tmp / "xdg_data_home" / "applications"
        fx.write(apps / "other.desktop", f"[Desktop Entry]\nType=Application\nName=Other Viewer\nExec={program} --view %f\n"
                                         "MimeType=image/png;\n".encode())
        fx.write(apps / f"{APP_ID}.desktop", f"[Desktop Entry]\nType=Application\nName=Photos\nExec={program} %F\n"
                                             "MimeType=image/png;\n".encode())
        path = self.add("open.png")
        jpeg = self.add("zz.jpg")
        window = self.viewer(path)
        viewer = window.viewer
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": str(self.tmp / "none")}):
            viewer._apps.clear()
            viewer.commands._fill_menu()
            self.assertIn(viewer.open_with_action, viewer.commands.more_menu.actions())
            viewer.open_with_menu.aboutToShow.emit()
            actions = viewer.open_with_menu.actions()
            self.assertEqual([action.text() for action in actions], ["Other Viewer"])        # not Photos itself
            actions[0].trigger()
            self.assertEqual(self.programs, [[str(program), "--view", path]])
            window.open_paths([jpeg])
            self.loaded(window, jpeg)
            viewer._apps.clear()
            viewer.open_with_menu.aboutToShow.emit()
            only = viewer.open_with_menu.actions()
            self.assertEqual(len(only), 1)
            self.assertFalse(only[0].isEnabled())                        # nothing else opens JPEG here: said so
        viewer.show_in_files_action.trigger()
        self.assertEqual(self.shown_in_files, [jpeg])
        mouse(viewer.canvas, QEvent.Type.MouseButtonPress, QPoint(50, 50), Qt.MouseButton.RightButton)
        viewer.canvas.customContextMenuRequested.emit(QPoint(50, 50))
        self.assertEqual(len(self.menus), 1)
        texts = [action.text() for action in self.menus[0].actions() if action.text()]
        self.assertEqual(texts[:2], ["Copy", "Copy as path"])
        self.assertTrue({"Open with", "Show in Files", "Set as desktop background", "Rotate right", "Edit", "Delete",
                         "File info"} <= set(texts))

    def test_every_command_of_the_bar_and_menu_is_wired(self):
        window = self.viewer(self.add("all.png"))
        viewer = window.viewer
        for action in viewer.commands.commands():
            self.assertTrue(action.text(), "a command without a name")
            wired = action.menu() is not None or action.receivers("2triggered(bool)") > 0
            self.assertTrue(wired, action.text())
        for action in viewer.commands.commands():
            button = viewer.commands.button(action)
            if button is not None:
                self.assertTrue(button.toolTip(), action.text())
                self.assertTrue(button.accessibleName(), action.text())
        self.assertEqual(viewer.print_action.isVisible(), printing.AVAILABLE)


class ViewerStatesTest(PhotosCase):
    def test_damaged_and_foreign_files_are_explained_and_do_not_stop_navigation(self):
        self.add("a-good.png")
        fx.write(self.pictures / "b-broken.jpg", b"\xff\xd8\xff\xe0 nothing more")
        fx.write(self.pictures / "c-text.png", b"these are words")
        fx.write(self.pictures / "d-empty.png", b"")
        fx.write(self.pictures / "e-bomb.png", fx.png_bomb())
        self.add("f-good.png")
        window = self.viewer(str(self.pictures / "b-broken.jpg"))
        viewer = window.viewer
        self.assertEqual(viewer.error().kind, "corrupt")
        self.assertTrue(viewer.message.isVisible())
        self.assertEqual(viewer.message.title_label.text(), "This picture is damaged")
        self.assertFalse(viewer.canvas.has_picture())
        self.assertFalse(viewer.edit_button.isEnabled())
        self.assertFalse(viewer.rotate_action.isEnabled())
        self.assertTrue(viewer.delete_action.isEnabled())               # a damaged file can still be removed
        expected = {"c-text.png": "This file is not a picture", "d-empty.png": "This file is empty",
                    "e-bomb.png": "This picture is too large to open"}
        for name, title in expected.items():
            self.press(window, Qt.Key.Key_Right)
            self.loaded(window, str(self.pictures / name))
            self.assertEqual(viewer.message.title_label.text(), title, name)
            self.assertNotIn("Traceback", viewer.message.text_label.text())
        self.assertIn("60 000 × 60 000", viewer.message.text_label.text())
        self.press(window, Qt.Key.Key_Right)
        self.loaded(window, str(self.pictures / "f-good.png"))
        self.assertFalse(viewer.message.isVisible())
        self.assertTrue(viewer.canvas.has_picture())
        self.assertTrue(viewer.edit_button.isEnabled())

    def test_a_file_that_does_not_exist(self):
        window = self.window([str(self.pictures / "gone.png")])
        self.wait(lambda: window.viewer.error() is not None)
        self.assertEqual(window.viewer.message.title_label.text(), "This file is no longer there")
        self.assertEqual(window.current_page(), "viewer")

    def test_a_cut_short_jpeg_is_shown_with_a_warning(self):
        import random
        noise = random.Random(5)
        image = picture(200, 200)
        for y in range(40, 200):
            for x in range(200):
                image.setPixelColor(x, y, QColor(noise.randrange(256), noise.randrange(256), noise.randrange(256)))
        path = self.add("cut.jpg", image)
        data = Path(path).read_bytes()
        Path(path).write_bytes(data[:len(data) * 2 // 3])
        window = self.viewer(path)
        self.assertTrue(window.viewer.canvas.has_picture())
        self.assertIn("incomplete", self.banner(window))
        self.assertEqual(window.viewer.banner.severity(), "warning")

    def test_animations_play_and_transparency_shows_the_checkerboard(self):
        gif = str(fx.write(self.pictures / "anim.gif", fx.gif([RED, GREEN, BLUE], 40, 30, delay=2)))
        window = self.viewer(gif)
        canvas = window.viewer.canvas
        self.assertTrue(canvas.is_animating())
        seen = set()

        def frame() -> bool:
            rect = canvas.picture_rect()
            seen.add(rgb(canvas.grab().toImage(), int(rect.center().x()), int(rect.center().y())))
            return {RED, GREEN, BLUE} <= seen
        self.wait(frame, 10)
        self.assertFalse(window.viewer.rotate_action.isEnabled())
        window.viewer.set_info_visible(True)
        self.assertEqual(window.viewer.info_pane.rows()["Type"], "GIF (animation, 3 frames)")
        clear = QImage(200, 200, QImage.Format.Format_ARGB32)
        clear.fill(Qt.GlobalColor.transparent)
        path = self.add("clear.png", clear)
        window.open_paths([path])
        self.loaded(window, path)
        self.assertFalse(canvas.is_animating())
        rect = canvas.picture_rect()
        grabbed = canvas.grab().toImage()
        first = rgb(grabbed, int(rect.left()) + 2, int(rect.top()) + 2)
        second = rgb(grabbed, int(rect.left()) + 10, int(rect.top()) + 2)
        self.assertNotEqual(first, second)                               # two tones of squares
        tones = {fluent.qcolor("surface").getRgb()[:3], fluent.qcolor("surface-hover").getRgb()[:3]}
        self.assertEqual({first, second}, tones)

    def test_svg_stays_sharp_when_zoomed(self):
        path = str(fx.write(self.pictures / "flag.svg", fx.SVG.encode()))
        window = self.viewer(path)
        canvas = window.viewer.canvas
        self.assertEqual(canvas.natural_size(), QSize(40, 20))
        self.assertFalse(window.viewer.rotate_action.isEnabled())
        canvas.set_zoom(32.0, animate=False)
        self.wait(canvas.is_settled)
        self.assertTrue(canvas.has_fine_rendering())
        grabbed = canvas.grab().toImage()
        rect = canvas.picture_rect()
        border = int(rect.left() + rect.width() / 2)
        y = int(rect.center().y())
        self.assertEqual(rgb(grabbed, border - 2, y), RED)               # a clean edge, not a blur of 32 pixels
        self.assertEqual(rgb(grabbed, border + 2, y), BLUE)

    def test_a_large_jpeg_gets_a_quick_preview_first_and_is_kept_small_in_memory(self):
        import random
        noise = random.Random(1)
        image = picture(1600, 1200, quadrants=True)
        painter = QPainter(image)
        for _ in range(60000):
            painter.setPen(QColor(noise.randrange(256), noise.randrange(256), noise.randrange(256)))
            painter.drawPoint(noise.randrange(1600), noise.randrange(1200))
        painter.end()
        path = self.add("huge.jpg", image, quality=98)
        self.assertGreater(os.path.getsize(path), 1024 * 1024)
        window = self.window([])
        viewer = window.viewer
        shown: list[tuple[QSize, QSize]] = []
        original = viewer.canvas.set_picture

        def record(image, natural=None, **options):
            shown.append((image.size(), QSize(natural) if natural is not None else image.size()))
            return original(image, natural, **options)

        viewer.canvas.set_picture = record
        real_load = decode.load

        def slow_load(p, max_pixels=500_000):           # the full decode of a huge picture takes its time
            time.sleep(0.4)
            return real_load(p, max_pixels)

        with mock.patch.object(decode, "load", slow_load):
            window.open_paths([path])
            self.loaded(window, path)
        self.assertGreaterEqual(len(shown), 2)
        self.assertEqual(shown[0][1], QSize(1600, 1200))                 # the preview already knows the real size
        self.assertEqual(shown[-1][1], QSize(1600, 1200))
        self.assertEqual(shown[-1][0], QSize(800, 600))                  # the decoder halved it: 0.48 megapixels
        self.assertTrue(viewer.loaded().reduced)
        self.assertIn("fewer pixels", self.banner(window))
        self.assertEqual(viewer.canvas.natural_size(), QSize(1600, 1200))
        viewer.canvas.zoom_actual(animate=False)
        self.assertEqual(viewer.zoom_label.text(), "100 %")              # 100 % is measured against the real size
        self.assertAlmostEqual(viewer.canvas.picture_rect().width(), 1600, delta=1)

    def test_full_screen_hides_the_bars_until_the_pointer_moves(self):
        paths = [self.add("one.png", picture(900, 600)), self.add("two.png")]
        window = self.viewer(paths[0])
        viewer = window.viewer
        self.assertTrue(viewer.top_bar.isVisible() and viewer.filmstrip.isVisible())
        self.press(window, Qt.Key.Key_F11)
        self.assertTrue(window.isFullScreen() and viewer.is_fullscreen())
        self.assertFalse(viewer.top_bar.isVisible())
        self.assertFalse(viewer.filmstrip.isVisible())
        self.assertFalse(viewer.next_button.isVisible())
        self.assertEqual(viewer.stage.geometry().top(), 0)               # the picture has the whole window
        self.press(window, Qt.Key.Key_Right)                             # the keys still work
        self.loaded(window, paths[1])
        self.press(window, Qt.Key.Key_R, CTRL)
        self.wait(lambda: viewer.file_worker.pending() == 0)
        self.assertEqual(decode.load(paths[1]).image.size(), QSize(240, 320))
        viewer._hide_timer.setInterval(40)
        QCursor.setPos(window.mapToGlobal(QPoint(400, 400)))
        mouse(viewer.canvas, QEvent.Type.MouseMove, QPoint(300, 300), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
        self.assertTrue(viewer.top_bar.isVisible())                      # the pointer moved: the bar is back
        self.assertTrue(viewer.next_button.isVisible())
        self.wait(lambda: not viewer.top_bar.isVisible(), 5)             # and goes again when the pointer rests
        testing.process(150)
        self.assertFalse(viewer.top_bar.isVisible())                     # it does not come back by itself
        mouse(viewer.canvas, QEvent.Type.MouseMove, QPoint(340, 320), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
        self.assertTrue(viewer.top_bar.isVisible())
        self.press(window, Qt.Key.Key_Escape)
        self.assertFalse(window.isFullScreen())
        self.assertFalse(viewer.is_fullscreen())
        self.assertTrue(viewer.top_bar.isVisible() and viewer.filmstrip.isVisible())
        self.assertEqual(viewer.stage.geometry().top(), 48)

    def test_the_filmstrip_can_be_switched_off_and_stays_off(self):
        window = self.viewer(self.add("strip.png"))
        viewer = window.viewer
        viewer.commands._fill_menu()
        viewer.filmstrip_action.trigger()
        self.assertFalse(viewer.filmstrip.isVisible())
        self.assertFalse(settings.load(window.settings_path).filmstrip)
        window.close()
        again = self.viewer(str(self.pictures / "strip.png"))
        self.assertFalse(again.viewer.filmstrip.isVisible())
        again.viewer.toggle_filmstrip()
        self.assertTrue(again.viewer.filmstrip.isVisible())


class EditorTest(PhotosCase):
    def edit(self, name: str = "edit.png", image: QImage | None = None) -> PhotosWindow:
        window = self.viewer(self.add(name, image or picture(400, 200, quadrants=True)))
        self.press(window, Qt.Key.Key_E, CTRL)
        self.assertEqual(window.current_page(), "editor")
        self.idle(window)
        return window

    def idle(self, window: PhotosWindow) -> None:
        editor = window.editor
        self.wait(lambda: editor.canvas.preview() is not None and editor.preview_idle())

    def test_crop_with_the_handles(self):
        window = self.edit()
        editor, canvas = window.editor, window.editor.canvas
        self.assertEqual(editor.mode(), "crop")
        self.assertFalse(editor.undo_button.isEnabled())
        box = canvas.image_rect()
        # drag the right edge to the middle: the left half stays
        drag(canvas, QPointF(box.right(), box.center().y()), QPointF(box.center().x(), box.center().y()))
        x, y, w, h = editor.state().crop
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(w, 0.5, delta=0.02)
        self.assertAlmostEqual(h, 1.0)
        self.assertTrue(editor.undo_button.isEnabled())
        # drag the bottom left corner up and in
        rect = canvas.crop_rect()
        drag(canvas, rect.bottomLeft(), rect.bottomLeft() + QPointF(rect.width() / 2, -rect.height() / 2))
        x, y, w, h = editor.state().crop
        self.assertAlmostEqual(x, 0.25, delta=0.02)
        self.assertAlmostEqual(w, 0.25, delta=0.02)
        self.assertAlmostEqual(h, 0.5, delta=0.02)
        # move the frame by its inside
        rect = canvas.crop_rect()
        drag(canvas, rect.center(), rect.center() + QPointF(box.width() / 2, box.height()))
        x, y, w, h = editor.state().crop
        self.assertAlmostEqual(x + w, 1.0, delta=0.01)                   # stopped at the edges of the picture
        self.assertAlmostEqual(y + h, 1.0, delta=0.01)
        self.assertIn("100 × 100 pixels", editor.size_label.text())
        self.idle(window)
        rendered = editops.render(window.viewer.loaded().image, editor.state())
        self.assertEqual(rendered.size(), QSize(100, 100))
        self.assertEqual(rgb(rendered, 50, 50), WHITE)                   # the bottom right quarter
        self.assertEqual(len(editor._history), 4)

    def test_shapes_straighten_turn_and_mirror(self):
        window = self.edit()
        editor = window.editor
        editor.aspect_box.setCurrentIndex(editor.aspect_box.findData("1:1"))
        x, y, w, h = editor.state().crop
        self.assertAlmostEqual(w * 400, h * 200)
        self.assertAlmostEqual(h, 1.0)
        self.assertFalse(editor.flip_aspect_button.isEnabled())
        editor.aspect_box.setCurrentIndex(editor.aspect_box.findData("4:3"))
        self.assertTrue(editor.flip_aspect_button.isEnabled())
        x, y, w, h = editor.state().crop
        self.assertAlmostEqual((w * 400) / (h * 200), 4 / 3, places=3)
        testing.click(editor.flip_aspect_button)                          # flipped: 3:4
        x, y, w, h = editor.state().crop
        self.assertAlmostEqual((w * 400) / (h * 200), 3 / 4, places=3)
        box = editor.canvas.image_rect()
        rect = editor.canvas.crop_rect()
        drag(editor.canvas, rect.topRight(), rect.topRight() + QPointF(-rect.width() / 2, 10))
        x, y, w, h = editor.state().crop
        self.assertAlmostEqual((w * 400) / (h * 200), 3 / 4, places=2)     # the shape holds while dragging
        testing.click(editor.reset_crop_button)
        self.assertIsNone(editor.state().crop)
        self.assertEqual(editor.aspect_box.currentData(), "free")
        # straighten with the keyboard: the slider moves in tenths of a degree
        editor.straighten.slider.setFocus()
        for _ in range(3):
            testing.key(editor.straighten.slider, Qt.Key.Key_PageUp)
        self.assertAlmostEqual(editor.state().straighten, 3.0)
        self.assertEqual(editor.straighten.value_label.text(), "3.0°")
        self.assertLess(editops.geometry(QSize(400, 200), editor.state()).size.width(), 400)
        testing.click(editor.rotate_right_button)
        self.assertEqual(editor.state().orientation, 6)
        testing.click(editor.flip_h_button)
        self.assertEqual(editor.state().orientation, exif.compose_orientation(6, 2))
        self.assertAlmostEqual(editor.state().straighten, -3.0)          # mirrored: the tilt changes side
        testing.click(editor.rotate_left_button)
        testing.click(editor.flip_v_button)
        self.idle(window)
        self.assertTrue(editor.is_dirty())

    def test_adjust_sliders_change_the_preview_and_undo_step_by_step(self):
        window = self.edit("grey.png", picture(400, 200, (100, 100, 100)))
        editor = window.editor
        testing.click(editor.tabs.item("adjust"))
        self.assertEqual(editor.mode(), "adjust")
        self.idle(window)
        self.assertEqual(rgb(editor.canvas.preview(), 200, 100), (100, 100, 100))
        slider = editor.sliders["brightness"].slider
        slider.setFocus()
        testing.key(slider, Qt.Key.Key_PageUp)                           # +10
        testing.key(slider, Qt.Key.Key_PageUp)
        self.assertAlmostEqual(editor.state().adjust.brightness, 0.2)
        self.assertEqual(editor.sliders["brightness"].value_label.text(), "+20")
        self.idle(window)
        brighter = rgb(editor.canvas.preview(), 200, 100)
        self.assertEqual(brighter, rgb(editops.apply_adjustments(picture(4, 4, (100, 100, 100)),
                                                                 editops.Adjustments(brightness=0.2)), 1, 1))
        self.assertGreater(brighter[0], 100)
        # dragging the handle is one step of history however far it goes
        history = len(editor._history)
        warmth = editor.sliders["warmth"]
        warmth.slider.setSliderDown(True)
        for value in (10, 25, 40):
            warmth.slider.setValue(value)
        self.assertAlmostEqual(editor.state().adjust.warmth, 0.4)        # live while dragging
        self.assertEqual(len(editor._history), history)
        warmth.slider.setSliderDown(False)
        self.assertEqual(len(editor._history), history + 1)
        self.idle(window)
        r, g, b = rgb(editor.canvas.preview(), 200, 100)
        self.assertTrue(r > g > b)
        self.press(window, Qt.Key.Key_Z, CTRL)
        self.assertEqual(editor.state().adjust.warmth, 0.0)
        self.assertAlmostEqual(editor.state().adjust.brightness, 0.2)
        self.assertEqual(warmth.slider.value(), 0)
        self.press(window, Qt.Key.Key_Z, CTRL)
        self.press(window, Qt.Key.Key_Z, CTRL)
        self.assertTrue(editor.state().is_identity())
        self.assertFalse(editor.undo_button.isEnabled())
        self.press(window, Qt.Key.Key_Y, CTRL)
        self.assertAlmostEqual(editor.state().adjust.brightness, 0.1)
        testing.click(editor.redo_button)
        testing.click(editor.redo_button)
        self.assertAlmostEqual(editor.state().adjust.warmth, 0.4)
        self.assertFalse(editor.redo_button.isEnabled())
        testing.click(editor.undo_button)
        editor.sliders["contrast"].slider.setValue(30)                   # a new change drops what could be redone
        self.assertFalse(editor.redo_button.isEnabled())
        testing.click(editor.reset_adjust_button)
        self.assertTrue(editor.state().adjust.is_identity())
        self.assertEqual(sorted(editor.sliders), sorted(editops.ADJUSTMENT_NAMES))
        self.assertEqual(editor.sliders["sharpness"].slider.minimum(), 0)

    def test_filters_show_previews_and_have_a_strength(self):
        window = self.edit()
        editor = window.editor
        testing.click(editor.tabs.item("filter"))
        self.wait(lambda: not editor.filter_buttons["Mono"]._picture.isNull())
        self.assertEqual(list(editor.filter_buttons), ["", *editops.FILTERS])
        self.assertFalse(editor.filter_strength.isEnabled())
        testing.click(editor.filter_buttons["Mono"])
        self.assertEqual(editor.state().filter, "Mono")
        self.assertTrue(editor.filter_strength.isEnabled())
        self.idle(window)
        preview = editor.canvas.preview()
        self.assertEqual(len(set(rgb(preview, 100, 50))), 1)             # the red quarter is grey now
        editor.filter_strength.slider.setValue(50)
        self.assertAlmostEqual(editor.state().filter_strength, 0.5)
        self.idle(window)
        r, g, b = rgb(editor.canvas.preview(), 100, 50)
        self.assertTrue(r > g and g == b)                                # half way back to red
        thumb = editor.filter_buttons["Mono"]._picture.toImage()
        self.assertEqual(len(set(rgb(thumb, thumb.width() // 4, thumb.height() // 4))), 1)
        testing.click(editor.filter_buttons[""])
        self.assertEqual(editor.state().filter, "")

    def test_markup_pen_highlighter_and_eraser(self):
        window = self.edit("white.png", picture(400, 200, WHITE))
        editor, canvas = window.editor, window.editor.canvas
        testing.click(editor.tabs.item("markup"))
        self.idle(window)
        box = canvas.image_rect()
        blue = next(swatch for swatch in editor.swatches if swatch.accessibleName() == "Blue")
        testing.click(blue)
        editor.width_slider.slider.setValue(8)
        start = QPointF(box.left() + box.width() * 0.2, box.center().y())
        end = QPointF(box.left() + box.width() * 0.8, box.center().y())
        drag(canvas, start, end, steps=12)
        self.assertEqual(len(editor.state().strokes), 1)
        stroke = editor.state().strokes[0]
        self.assertEqual((stroke.tool, stroke.color), (editops.PEN, fluent.theme().color("azure.500")))
        self.assertAlmostEqual(stroke.points[0][0], 80, delta=3)         # in pixels of the picture, not of the screen
        self.assertAlmostEqual(stroke.points[-1][0], 320, delta=3)
        self.assertAlmostEqual(stroke.points[0][1], 100, delta=3)
        self.assertAlmostEqual(stroke.width, 8 * 400 / 800)
        shown = canvas.grab().toImage()
        self.assertTrue(near(rgb(shown, int(box.center().x()), int(box.center().y())),
                             fluent.qcolor("azure.500").getRgb()[:3], 30))
        editor.tool_box.set_current(editops.HIGHLIGHTER)
        yellow = next(swatch for swatch in editor.swatches if swatch.accessibleName() == "Yellow")
        testing.click(yellow)
        drag(canvas, QPointF(start.x(), box.top() + 20), QPointF(end.x(), box.top() + 20), steps=8)
        self.assertEqual([s.tool for s in editor.state().strokes], [editops.PEN, editops.HIGHLIGHTER])
        self.assertGreater(editor.state().strokes[1].width, stroke.width)
        rendered = editops.render(window.viewer.loaded().image, editor.state())
        self.assertTrue(near(rgb(rendered, 200, 100), fluent.qcolor("azure.500").getRgb()[:3], 5))
        self.assertEqual(rgb(rendered, 200, 150), WHITE)
        # the eraser takes whole strokes; one drag is one step back
        editor.tool_box.set_current("eraser")
        self.assertFalse(editor.colour_holder.isEnabled())
        history = len(editor._history)
        drag(canvas, QPointF(box.center().x(), box.center().y() + 30), QPointF(box.center().x(), box.center().y() - 10))
        self.assertEqual([s.tool for s in editor.state().strokes], [editops.HIGHLIGHTER])
        self.assertEqual(len(editor._history), history + 1)
        testing.click(editor.undo_button)
        self.assertEqual(len(editor.state().strokes), 2)
        testing.click(editor.clear_markup_button)
        self.assertEqual(editor.state().strokes, ())
        drag(canvas, QPointF(3, 3), QPointF(10, 10))                     # beside the picture: no stroke
        self.assertEqual(editor.state().strokes, ())

    def test_save_as_copy_writes_the_edit_at_full_size_and_keeps_the_original(self):
        plain = self.add("cam.jpg", picture(1600, 800, quadrants=True), quality=95)
        Path(plain).write_bytes(fx.jpeg_with_block(Path(plain).read_bytes(), fx.camera_exif(1)))
        original = Path(plain).read_bytes()
        window = self.viewer(plain)
        testing.click(window.viewer.edit_button)
        self.idle(window)
        editor = window.editor
        self.assertLess(editor.canvas.preview().width(), 1600)           # the preview is a small copy
        testing.click(editor.rotate_right_button)
        editor._apply(editor.state().with_crop((0.0, 0.0, 1.0, 0.5)).with_filter("Mono"))
        self.idle(window)
        testing.click(editor.save_copy_button)
        self.assertEqual(self.save_requests[0][:2], ("Save as copy", str(self.pictures / "cam (edited).jpg")))
        target = str(self.pictures / "cam (edited).jpg")
        self.wait(lambda: os.path.exists(target) and window.current_page() == "viewer")
        self.assertEqual(Path(plain).read_bytes(), original)
        saved = decode.load(target)
        self.assertEqual(saved.image.size(), QSize(800, 800))            # turned, the top half kept: full resolution
        self.assertEqual(len({c // 8 for c in rgb(saved.image, 200, 200)}), 1)         # mono
        details = exif.read_file(target)
        self.assertEqual((details.orientation, details.model), (1, "Boswas Prism One"))
        self.assertEqual((details.pixel_width, details.pixel_height), (800, 800))
        self.loaded(window, target)                                      # the copy is what is shown now
        self.assertIn("Saved “cam (edited).jpg”", self.banner(window))
        self.assertIn("Kept: camera details", self.banner(window))
        self.assertEqual(sorted(os.listdir(self.pictures)), ["cam (edited).jpg", "cam.jpg"])

    def test_save_replaces_the_original_only_after_a_yes(self):
        path = self.add("over.png", picture(400, 200, quadrants=True))
        before = Path(path).read_bytes()
        window = self.edit("over.png", picture(400, 200, quadrants=True))
        editor = window.editor
        testing.click(editor.flip_h_button)
        self.answer = False
        testing.click(editor.save_button)
        self.assertEqual(self.questions[-1][0], "Replace the original?")
        testing.process(100)
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertEqual(window.current_page(), "editor")
        self.answer = True
        testing.click(editor.save_button)
        self.wait(lambda: window.current_page() == "viewer")
        self.loaded(window, path)
        self.assertEqual(rgb(decode.load(path).image, 50, 50), GREEN)    # mirrored: green is on the left now
        self.assertEqual(rgb(window.viewer.loaded().image, 50, 50), GREEN)
        self.assertEqual(os.listdir(self.pictures), ["over.png"])

    def test_leaving_asks_when_there_are_changes(self):
        window = self.edit()
        editor = window.editor
        self.press(window, Qt.Key.Key_Escape)                            # nothing changed: no question
        self.assertEqual(window.current_page(), "viewer")
        self.assertEqual(self.questions, [])
        testing.click(window.viewer.edit_button)
        self.idle(window)
        testing.click(editor.rotate_left_button)
        self.answer = False
        testing.click(editor.cancel_button)
        self.assertEqual(self.questions[-1][0], "Discard your changes?")
        self.assertEqual(window.current_page(), "editor")
        self.assertFalse(window.close())                                 # closing the window asks too
        self.assertEqual(len(self.questions), 2)
        other = self.add("other.png")
        window.open_paths([other])                                       # so does a picture opened from outside
        self.wait(lambda: len(self.questions) == 3)
        self.assertEqual(window.current_page(), "editor")
        self.answer = True
        testing.click(editor.reset_button)
        self.assertFalse(editor.is_dirty())
        testing.click(editor.cancel_button)
        self.assertEqual(window.current_page(), "viewer")
        self.assertEqual(len(self.questions), 3)

    def test_what_cannot_be_overwritten_offers_only_a_copy(self):
        gif = str(fx.write(self.pictures / "anim.gif", fx.gif([RED, GREEN], 40, 30)))
        window = self.viewer(gif)
        testing.click(window.viewer.edit_button)
        self.idle(window)
        editor = window.editor
        self.assertFalse(editor.save_button.isVisible())
        self.assertIn("first picture of this animation", editor.banner.message())
        testing.click(editor.save_copy_button)
        self.assertEqual(self.save_requests[0][1:], (str(self.pictures / "anim (edited).png"), "png"))
        self.wait(lambda: os.path.exists(self.pictures / "anim (edited).png") and window.current_page() == "viewer")
        self.assertEqual(rgb(decode.load(str(self.pictures / "anim (edited).png")).image, 5, 5), RED)

    def test_a_failed_save_is_said_and_the_edit_stays(self):
        window = self.edit()
        editor = window.editor
        testing.click(editor.flip_v_button)
        self.save_to = str(self.pictures / "no such folder" / "x.png")
        testing.click(editor.save_copy_button)
        self.wait(lambda: editor.banner.isVisible() and editor.banner.severity() == "error")
        self.assertIn("could not be written", editor.banner.message())
        self.assertEqual(window.current_page(), "editor")
        self.assertTrue(editor.is_dirty())
        self.assertTrue(editor.save_copy_button.isEnabled())


class GalleryTest(PhotosCase):
    def fill(self) -> None:
        self.add("new.png", picture(300, 200, RED), age_days=1)
        self.add("Trips/older.jpg", picture(300, 200, GREEN), age_days=40)
        self.add("Trips/IMG_2.png", age_days=41)
        self.add("Trips/IMG_10.png", age_days=41.5)
        self.add("Scans/ancient.bmp", picture(100, 300, BLUE), age_days=400)
        (self.pictures / "notes.txt").write_text("not a picture")

    def gallery(self) -> PhotosWindow:
        window = self.window([])
        self.wait(lambda: not window.is_scanning() and window._scanned)
        return window

    def test_without_a_file_the_gallery_shows_the_pictures_newest_first_by_month(self):
        self.fill()
        window = self.gallery()
        page, grid = window.gallery_page, window.gallery_page.grid
        self.assertEqual(window.current_page(), "gallery")
        self.assertEqual(window.navigation.current(), "gallery")
        self.assertEqual([item.name for item in grid.items()], ["new.png", "older.jpg", "IMG_2.png", "IMG_10.png",
                                                                "ancient.bmp"])
        titles = [section.title for section in grid._sections]
        self.assertEqual(len(titles), len(set(titles)))
        self.assertEqual(titles[0], library.month_title(time.time() - 86400))
        self.assertEqual(sum(section.count for section in grid._sections), 5)
        self.assertEqual(page.status.text(), "5 pictures")
        window.thumbs.wait(20)
        testing.process(30)
        cache = self.tmp / "xdg_cache_home" / "thumbnails" / "large"
        self.assertEqual(len(os.listdir(cache)), 5)                      # the shared thumbnail cache was filled
        self.assertTrue((cache / thumbcache.thumbnail_name(thumbcache.file_uri(str(self.pictures / "new.png")))).is_file())
        shot = grid.grab().toImage()
        rect = grid.item_rect(0)
        self.assertEqual(rgb(shot, int(rect.center().x()), int(rect.center().y())), RED)
        grid.ensure_visible(4)
        testing.process(30)
        window.thumbs.wait(20)
        shot = grid.grab().toImage()
        tall = grid.item_rect(4)                                         # a tall picture fills its square
        self.assertEqual(rgb(shot, int(tall.left()) + 4, int(tall.center().y())), BLUE)
        self.assertEqual(rgb(shot, int(tall.right()) - 4, int(tall.center().y())), BLUE)

    def test_click_opens_the_picture_and_escape_comes_back(self):
        self.fill()
        window = self.gallery()
        grid = window.gallery_page.grid
        testing.click(grid, grid.item_rect(1).center().toPoint())
        self.assertEqual(window.current_page(), "viewer")
        self.loaded(window, str(self.pictures / "Trips" / "older.jpg"))
        self.assertEqual(window.viewer.position_label.text(), "2 of 5")  # the arrows walk the gallery
        self.press(window, Qt.Key.Key_Right)
        self.loaded(window, str(self.pictures / "Trips" / "IMG_2.png"))
        self.press(window, Qt.Key.Key_Escape)
        self.assertEqual(window.current_page(), "gallery")
        self.assertEqual(window.windowTitle(), "Photos")
        grid.setFocus()
        testing.key(grid, Qt.Key.Key_End)
        testing.key(grid, Qt.Key.Key_Return)                             # the keyboard opens too
        self.loaded(window, str(self.pictures / "Scans" / "ancient.bmp"))
        testing.click(window.viewer.back_button)
        self.assertEqual(window.current_page(), "gallery")

    def test_selecting_several_to_copy_and_delete(self):
        self.fill()
        window = self.gallery()
        page, grid = window.gallery_page, window.gallery_page.grid
        self.assertFalse(page.commands.button(page.delete_action).isVisible())
        testing.click(grid, grid.item_rect(0).center().toPoint(), modifiers=CTRL)
        self.assertEqual(grid.selected(), [0])
        self.assertEqual(window.current_page(), "gallery")               # selecting does not open
        testing.click(grid, grid.item_rect(2).center().toPoint(), modifiers=SHIFT)
        self.assertEqual(grid.selected(), [0, 1, 2])
        testing.click(grid, grid.item_rect(1).center().toPoint())        # with a selection a click selects too
        self.assertEqual(grid.selected(), [0, 2])
        testing.click(grid, grid.mark_rect(4).center().toPoint())        # the mark in the corner
        self.assertEqual(grid.selected(), [0, 2, 4])
        self.assertEqual(page.status.text(), "3 of 5 pictures selected")
        self.assertTrue(page.commands.button(page.delete_action).isVisible())
        grid.setFocus()
        testing.key(grid, Qt.Key.Key_C, CTRL)
        urls = [url.toLocalFile() for url in QGuiApplication.clipboard().mimeData().urls()]
        self.assertEqual([os.path.basename(u) for u in urls], ["new.png", "IMG_2.png", "ancient.bmp"])
        testing.key(grid, Qt.Key.Key_Delete)
        self.wait(lambda: len(self.trashed()) == 3)
        self.assertIn("3 pictures will be moved to the Trash", self.questions[0][1])
        self.wait(lambda: grid.count() == 2)
        self.assertEqual([item.name for item in grid.items()], ["older.jpg", "IMG_10.png"])
        self.assertEqual(grid.selected(), [])
        self.assertEqual(page.banner.message(), "Moved 3 pictures to the Trash.")
        testing.click(page.banner.action_button)                         # Undo
        self.wait(lambda: self.trashed() == [] and not window.is_scanning() and grid.count() == 5)
        # the keyboard alone: arrows, Space, Shift+arrows, Ctrl+A, Escape
        grid.setFocus()
        testing.key(grid, Qt.Key.Key_Home)
        testing.key(grid, Qt.Key.Key_Right)
        testing.key(grid, Qt.Key.Key_Space)
        self.assertEqual(grid.selected(), [1])
        testing.key(grid, Qt.Key.Key_Right, SHIFT)
        testing.key(grid, Qt.Key.Key_Right, SHIFT)
        self.assertEqual(grid.selected(), [1, 2, 3])
        testing.key(grid, Qt.Key.Key_A, CTRL)
        self.assertEqual(len(grid.selected()), 5)
        testing.key(grid, Qt.Key.Key_Escape)
        self.assertEqual(grid.selected(), [])
        self.assertEqual(grid.fluent_focus_rect()[1], grid.item_rect(grid.cursor_index()).toRect())

    def test_search_by_file_name(self):
        self.fill()
        window = self.gallery()
        page, grid = window.gallery_page, window.gallery_page.grid
        testing.key(grid, Qt.Key.Key_F, CTRL)
        self.assertIs(QApplication.focusWidget(), page.search)
        testing.type_text(page.search, "img")
        self.assertEqual([item.name for item in grid.items()], ["IMG_2.png", "IMG_10.png"])
        self.assertEqual(page.status.text(), "2 of 5 pictures")
        testing.type_text(page.search, " 10")
        self.assertEqual([item.name for item in grid.items()], ["IMG_10.png"])
        testing.click(grid, grid.item_rect(0).center().toPoint())
        self.loaded(window, str(self.pictures / "Trips" / "IMG_10.png"))
        self.assertEqual(window.viewer.position_label.text(), "")        # the arrows walk what was found
        window.show_gallery()
        page.search.setText("zebra")
        self.assertEqual(page.stack.currentWidget(), page.empty)
        self.assertEqual(page.empty.title_label.text(), "No picture found")
        self.assertFalse(page.empty.action_button.isVisible())
        testing.key(page.search, Qt.Key.Key_Escape)
        self.assertEqual(grid.count(), 5)

    def test_an_empty_gallery_offers_to_add_a_folder(self):
        window = self.gallery()
        page = window.gallery_page
        self.assertEqual(page.stack.currentWidget(), page.empty)
        self.assertEqual(page.empty.title_label.text(), "No pictures here yet")
        self.assertFalse(page.slideshow_action.isEnabled())
        elsewhere = self.tmp / "Elsewhere"
        elsewhere.mkdir()
        picture(50, 50).save(str(elsewhere / "far.png"))
        self.folder_answer = str(elsewhere)
        testing.click(page.empty.action_button)
        self.wait(lambda: page.grid.count() == 1 and not window.is_scanning())
        self.assertEqual(settings.load(window.settings_path).folders, [str(elsewhere)])
        self.folder_answer = None                                        # the chooser was cancelled
        window.add_folder()
        self.assertEqual(window.settings.folders, [str(elsewhere)])

    def test_folders_page(self):
        self.fill()
        window = self.gallery()
        window.show_page("folders")
        page = window.folders_page
        self.assertEqual([(folder.name, folder.count) for folder in page.folders()],
                         [("Pictures", 1), ("Scans", 1), ("Trips", 3)])
        self.assertEqual(page.list.count(), 3)
        self.assertEqual(page.list.item(2).text(), "Trips\n3 pictures · ~/Pictures")
        self.assertEqual(page.count_label.text(), "3 folders")
        window.thumbs.wait(20)
        testing.process(30)
        self.assertFalse(page.list.item(0).icon().isNull())
        rect = page.list.visualItemRect(page.list.item(2))
        testing.click(page.list.viewport(), rect.center())
        self.assertEqual(page.stack.currentWidget(), page.folder_page)
        self.assertEqual(page.crumbs.items(), ["Folders", "Trips"])
        self.assertEqual([item.name for item in page.grid.grid.items()], ["IMG_2.png", "IMG_10.png", "older.jpg"])
        testing.click(page.grid.grid, page.grid.grid.item_rect(1).center().toPoint())
        self.loaded(window, str(self.pictures / "Trips" / "IMG_10.png"))
        self.assertEqual(window.viewer.position_label.text(), "2 of 3")
        self.press(window, Qt.Key.Key_Escape)
        self.assertEqual(window.current_page(), "gallery")
        self.assertEqual(window.navigation.current(), "folders")         # back where the user was
        testing.click(page.back_button)
        self.assertEqual(page.stack.currentWidget(), page.list_page)

    def test_settings_are_applied_and_remembered(self):
        self.fill()
        extra = self.tmp / "More"
        extra.mkdir()
        picture(60, 60).save(str(extra / "more.png"))
        window = self.gallery()
        window.show_settings()
        page = window.settings_page
        self.assertEqual(window.navigation.current(), "settings")
        self.assertIs(window.navigation.stack.currentWidget(), page)
        self.assertFalse(window.gallery_page.isVisible())
        self.assertIn("follows the theme of the desktop, which is dark now", page.theme_card.description())
        testing.set_scheme("light")
        self.assertIn("which is light now", page.theme_card.description())
        testing.set_scheme("dark")
        self.folder_answer = str(extra)
        testing.click(page.add_folder_button)
        self.wait(lambda: not window.is_scanning() and len(window.library) == 6)
        self.assertEqual([card.title() for card in page._folder_cards], ["More"])
        testing.click(page.pictures_switch)                              # leave the Pictures folder out
        self.wait(lambda: not window.is_scanning() and len(window.library) == 1)
        testing.click(page._folder_cards[0].control())                   # Remove
        self.wait(lambda: not window.is_scanning() and len(window.library) == 0)
        self.assertEqual(page._folder_cards, [])
        testing.click(page.pictures_switch)
        page.interval_box.setValue(12)
        testing.click(page.loop_switch)
        testing.click(page.shuffle_switch)
        testing.click(page.confirm_switch)
        testing.click(page.window_switch)
        page.quality_slider.setValue(70)
        saved = settings.load(window.settings_path)
        self.assertEqual((saved.slideshow_interval, saved.slideshow_loop, saved.slideshow_shuffle), (12, False, True))
        self.assertEqual((saved.confirm_delete, saved.new_window, saved.jpeg_quality), (False, True, 70))
        self.assertEqual((saved.include_pictures, saved.folders), (True, []))
        window.close()
        again = self.gallery()
        self.assertEqual(again.settings, saved)
        again.show_settings()
        self.assertEqual(again.settings_page.interval_box.int_value(), 12)
        self.assertTrue(again.settings_page.shuffle_switch.isChecked())

    def test_the_window_remembers_its_size(self):
        window = self.window([], size=(760, 600))                        # within the 800 x 800 offscreen screen
        window.close()
        self.assertTrue((self.tmp / "xdg_state_home" / APP_ID / "window.ini").is_file())
        again = PhotosWindow([])
        self.keep(again)
        again.show()
        testing.process()
        self.assertEqual((again.width(), again.height()), (760, 600))

    def test_fifty_thousand_pictures_stay_smooth(self):
        base = time.time()
        items = [library.Item(f"/nowhere/{index % 300}/IMG_{index}.jpg", base - index * 1700, 1000)
                 for index in range(50000)]
        window = self.gallery()
        grid = window.gallery_page.grid
        wanted: list[int] = []
        window.thumbs.want = lambda client, entries: wanted.append(len(list(entries)))
        started = time.perf_counter()
        window.library = items
        window._show_library(scanning=False)
        testing.process()
        setup = time.perf_counter() - started
        self.assertEqual(grid.count(), 50000)
        self.assertGreater(grid.content_height(), 500000)
        self.assertLess(setup, 3.0)
        started = time.perf_counter()
        frames = 0
        bar = grid.scrollbar
        for value in range(0, bar.maximum(), max(1, bar.maximum() // 60)):
            bar.setValue(value)
            grid.grab()
            frames += 1
        per_frame = (time.perf_counter() - started) / frames
        self.assertLess(per_frame, 0.1)                                  # about 3 ms on a desktop
        testing.process(20)
        self.assertTrue(wanted)
        self.assertLess(max(wanted), 400)                                # only what is on screen is ever asked for
        bar.setValue(bar.maximum())
        testing.process(20)
        last = grid.visible_indexes()
        self.assertEqual(last.stop, 50000)
        self.assertEqual(grid.index_at(grid.item_rect(49999).center().toPoint()), 49999)
        self.assertEqual(grid.index_at(grid.item_rect(last.start).center().toPoint()), last.start)
        testing.key(grid, Qt.Key.Key_Home)
        self.assertEqual(bar.value(), 0)
        started = time.perf_counter()
        window.gallery_page.search.setText("img_4999")
        self.assertEqual(grid.count(), 11)
        self.assertLess(time.perf_counter() - started, 2.0)


class SlideshowTest(PhotosCase):
    def show(self, *args, **kwargs):
        return super().show(*args, **kwargs)

    def start(self, count: int = 4):
        paths = [self.add(f"s{index}.png", picture(200, 100, (index * 60, 0, 200))) for index in range(count)]
        window = self.viewer(paths[0])
        return window, paths

    def shown(self, window: PhotosWindow, path: str) -> None:
        self.wait(lambda: window.slideshow.current_item() is not None
                  and window.slideshow.current_item().path == path, message=f"not shown: {path}")

    def test_it_runs_full_screen_steps_pauses_and_ends_where_it_stopped(self):
        window, paths = self.start()
        show = window.slideshow
        self.press(window, Qt.Key.Key_F5)
        self.assertEqual(window.current_page(), "slideshow")
        self.assertTrue(window.isFullScreen())
        self.shown(window, paths[0])
        self.assertFalse(show.is_paused())
        self.press(window, Qt.Key.Key_Right)
        self.shown(window, paths[1])
        self.press(window, Qt.Key.Key_Right)
        self.shown(window, paths[2])
        self.press(window, Qt.Key.Key_Left)
        self.shown(window, paths[1])
        self.press(window, Qt.Key.Key_Space)
        self.assertTrue(show.is_paused())
        self.assertFalse(show._timer.isActive())
        self.assertEqual(show.pause_button.toolTip(), "Continue (Space)")
        self.press(window, Qt.Key.Key_Space)
        self.assertFalse(show.is_paused())
        self.assertTrue(show._timer.isActive())
        shot = show.grab().toImage()
        self.assertEqual(rgb(shot, 2, 2), (0, 0, 0))                     # black around the picture
        self.assertEqual(rgb(shot, shot.width() // 2, shot.height() // 2), (60, 0, 200))
        self.press(window, Qt.Key.Key_Escape)
        self.assertEqual(window.current_page(), "viewer")
        self.assertFalse(window.isFullScreen())
        self.assertFalse(window.viewer.is_fullscreen())
        self.loaded(window, paths[1])                                    # the viewer shows what the show ended on

    def test_the_interval_advances_and_the_end_is_honoured(self):
        window, paths = self.start(3)
        show = window.slideshow
        window.settings.slideshow_loop = False
        show.interval_ms = lambda: 40
        testing.click(window.viewer.commands.button(window.viewer.slideshow_action))
        self.shown(window, paths[2])                                     # it walked there by itself
        self.wait(show.is_paused)                                        # and stops at the end: no repeat
        self.assertEqual(show.position(), 2)
        self.press(window, Qt.Key.Key_Space)                             # continue: from the start
        self.shown(window, paths[0])
        window.settings.slideshow_loop = True
        seen: list[str] = []
        show.shown.connect(seen.append)
        self.wait(lambda: len(seen) >= 5)
        self.assertEqual(seen[:5], [paths[1], paths[2], paths[0], paths[1], paths[2]])
        testing.click(show.close_button)
        self.assertEqual(window.current_page(), "viewer")

    def test_shuffle_shows_every_picture_once_per_round(self):
        window, paths = self.start(6)
        window.settings.slideshow_shuffle = True
        window.viewer.show_index(3, animate=False)
        self.loaded(window, paths[3])
        window.start_slideshow(window.viewer.items(), 3)
        show = window.slideshow
        order = show.order()
        self.assertEqual(order[0], 3)                                    # the picture on show starts the round
        self.assertEqual(sorted(order), list(range(6)))
        self.shown(window, paths[3])
        self.press(window, Qt.Key.Key_Right)
        self.shown(window, paths[order[1]])
        show.stop()

    def test_cross_fade_and_reduced_motion(self):
        window, paths = self.start(3)
        show = window.slideshow
        fluent.manager().set_reduced_motion(False)
        window.start_slideshow(window.viewer.items(), 0)
        self.shown(window, paths[0])
        self.press(window, Qt.Key.Key_Right)
        self.shown(window, paths[1])
        self.assertTrue(show.is_fading())
        self.wait(lambda: 0.2 < show._fade < 0.8, 5)
        middle = rgb(show.grab().toImage(), show.width() // 2, show.height() // 2)
        self.assertTrue(0 < middle[0] < 60)                              # between the two pictures
        self.wait(lambda: not show.is_fading(), 5)
        self.assertEqual(rgb(show.grab().toImage(), show.width() // 2, show.height() // 2), (60, 0, 200))
        fluent.manager().set_reduced_motion(True)
        self.press(window, Qt.Key.Key_Right)
        self.shown(window, paths[2])
        self.assertFalse(show.is_fading())                               # no animation when the desktop asks for none
        self.assertEqual(rgb(show.grab().toImage(), show.width() // 2, show.height() // 2), (120, 0, 200))
        show.stop()

    def test_pictures_that_cannot_be_shown_are_skipped(self):
        good = self.add("a.png", picture(100, 100, RED))
        fx.write(self.pictures / "b.png", b"broken")
        fx.write(self.pictures / "c.png", b"broken too")
        last = self.add("d.png", picture(100, 100, BLUE))
        window = self.viewer(good)
        window.start_slideshow(window.viewer.items(), 0)
        self.shown(window, good)
        self.press(window, Qt.Key.Key_Right)
        self.shown(window, last)
        self.press(window, Qt.Key.Key_Left)
        self.shown(window, good)
        window.slideshow.stop()
        bad = [library.item_for(str(self.pictures / name)) for name in ("b.png", "c.png")]
        window.start_slideshow(bad, 0)
        self.wait(lambda: window.slideshow.message.isVisible())
        self.assertEqual(window.slideshow.message.text(), "None of these pictures could be shown.")
        window.slideshow.stop()

    def test_from_the_gallery_with_a_selection(self):
        for index in range(5):
            self.add(f"g{index}.png", age_days=index)
        window = self.window([])
        self.wait(lambda: not window.is_scanning() and window.gallery_page.grid.count() == 5)
        page = window.gallery_page
        page.grid.set_selected([1, 3])
        testing.click(page.commands.button(page.slideshow_action))
        self.assertEqual(window.current_page(), "slideshow")
        self.assertEqual([item.name for item in window.slideshow._items], ["g1.png", "g3.png"])
        self.press(window, Qt.Key.Key_Escape)
        self.assertEqual(window.current_page(), "gallery")
        self.assertFalse(window.isFullScreen())
        window.open_paths([str(self.pictures / "g0.png")])
        self.loaded(window, str(self.pictures / "g0.png"))
        window = self.window(["--slideshow", str(self.pictures / "g2.png")])
        self.wait(lambda: window.current_page() == "slideshow")


class InstanceTest(PhotosCase):
    def test_another_start_reuses_the_window_or_opens_a_new_one(self):
        first, second = self.add("first.png"), self.add("second.png")
        window = self.viewer(first)
        window.handle_arguments([second])
        self.loaded(window, second)
        self.assertEqual(len(window_module._windows), 1)
        window.handle_arguments(["--gallery"])
        self.assertEqual(window.current_page(), "gallery")
        window.handle_arguments([])
        self.assertEqual(len(window_module._windows), 1)
        window.handle_arguments(["--new-window", first])
        self.assertEqual(len(window_module._windows), 2)
        other = window_module._windows[-1]
        self.loaded(other, first)
        self.assertEqual(window.current_page(), "gallery")
        other.close()
        self.assertEqual(len(window_module._windows), 1)
        window.settings.new_window = True                                # the setting does the same
        window.handle_arguments([second])
        self.assertEqual(len(window_module._windows), 2)
        self.loaded(window_module._windows[-1], second)

    def test_command_line(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["boswas-photos", "--help"]), 0)
            self.assertEqual(main(["boswas-photos", "--version"]), 0)
        self.assertIn("Usage: boswas-photos", out.getvalue())
        self.assertRegex(out.getvalue().splitlines()[-1], r"^boswas-photos \d")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["boswas-photos", "--frobnicate"]), 2)
            with mock.patch("os.geteuid", return_value=0):
                self.assertEqual(main(["boswas-photos"]), 4)             # never as root
        self.assertIn("unknown option --frobnicate", err.getvalue())
        self.assertIn("never as root", err.getvalue())
        calls = {}

        def fake_run(app_id, factory, **options):
            calls.update(app_id=app_id, options=options)
            return 0

        with mock.patch.object(fluent, "run", fake_run), mock.patch("os.getcwd", return_value="/work"):
            self.assertEqual(main(["boswas-photos", "--slideshow", "relative.png", "/abs.png"]), 0)
        self.assertEqual(calls["app_id"], "com.boswas.Photos")
        self.assertEqual(calls["options"]["argv"], ["boswas-photos", "--slideshow", "/work/relative.png", "/abs.png"])


class PackagingTest(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]
    SOURCES = sorted((ROOT / "boswas_apps" / "photos").rglob("*.py"))

    def test_no_colour_of_its_own(self):
        """Colours come from the toolkit's tokens: no colour value and no named colour in the sources.
        (Black and white as picture content, and transparent, are not interface colours.)"""
        import re
        value = re.compile(r"""["']#[0-9A-Fa-f]{3,8}["']""")
        named = re.compile(r"\bQColor\(\s*[\"'](?!#)|Qt\.GlobalColor\.(?!transparent\b|black\b|white\b)|\brgba?\(")
        for path in self.SOURCES:
            text = path.read_text()
            for number, line in enumerate(text.splitlines(), 1):
                if "#RRGGBB" in line:
                    continue                                    # a comment that says what a colour looks like
                self.assertIsNone(value.search(line), f"{path.name}:{number}: a colour value: {line.strip()}")
                self.assertIsNone(named.search(line), f"{path.name}:{number}: a colour of its own: {line.strip()}")
            self.assertNotIn("setStyleSheet", text, path.name)

    def test_nothing_is_run_through_a_shell(self):
        import re
        forbidden = re.compile(r"shell\s*=\s*True|\bos\.system\(|\bos\.popen\(|\bpickle\b|(?<![\w.])eval\(|(?<![\w.])exec\(")
        for path in self.SOURCES:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                self.assertIsNone(forbidden.search(line), f"{path.name}:{number}: {line.strip()}")

    def test_logic_modules_do_not_import_qt(self):
        for name in ("natsort", "formats", "exif", "trash", "thumbcache", "desktopcfg", "library", "settings",
                     "openwith", "cli", "fileutil", "fileinfo"):
            text = (self.ROOT / "boswas_apps" / "photos" / f"{name}.py").read_text()
            self.assertNotIn("PySide6", text, name)
            self.assertNotIn("boswas_fluent", text, name)
        for name in ("decode", "editops", "imageio", "wallpaper"):
            text = (self.ROOT / "boswas_apps" / "photos" / f"{name}.py").read_text()
            self.assertNotIn("QtWidgets", text, name)

    def test_desktop_entry(self):
        text = (self.ROOT / "data" / "photos" / "com.boswas.Photos.desktop").read_text()
        keys = dict(line.split("=", 1) for line in text.split("\n[Desktop Action")[0].splitlines() if "=" in line)
        self.assertEqual(keys["Name"], "Photos")
        self.assertEqual(keys["GenericName"], "Image Viewer")
        self.assertEqual(keys["Exec"], "boswas-photos %F")
        self.assertEqual(keys["Icon"], "boswas-photos")
        self.assertEqual(keys["StartupWMClass"], APP_ID)
        self.assertEqual(keys["Type"], "Application")
        icons = self.ROOT.parents[1] / "desktop" / "icons" / "Boswas-Prism" / "scalable" / "apps"
        self.assertTrue((icons / "boswas-photos.svg").is_file())
        testing.app()
        offered = [mime for mime in keys["MimeType"].split(";") if mime]
        self.assertEqual(len(offered), len(set(offered)))
        aliases = {"image/vnd.microsoft.icon": "image/x-icon"}
        readable = set(decode.supported_mime_types())
        for mime in offered:
            self.assertIn(aliases.get(mime, mime), readable, f"{mime} is offered but cannot be read here")
        self.assertTrue({"image/png", "image/jpeg", "image/gif", "image/bmp", "image/svg+xml"} <= set(offered))
        self.assertIn("[Desktop Action NewWindow]", text)
        self.assertIn("Exec=boswas-photos --new-window", text)
        self.assertTrue(keys["MimeType"].endswith(";") and keys["Categories"].endswith(";"))

    def test_launcher_and_manual(self):
        launcher = (self.ROOT / "bin" / "boswas-photos").read_text()
        self.assertTrue(launcher.startswith("#!/usr/bin/python3 -IB\n"))
        self.assertIn('sys.path.insert(0, "/usr/lib/boswas/python")', launcher)
        self.assertIn("from boswas_apps.photos.__main__ import main", launcher)
        manual = (self.ROOT / "man" / "boswas-photos.1").read_text()
        for section in (".SH NAME", ".SH SYNOPSIS", ".SH DESCRIPTION", ".SH OPTIONS", ".SH FILES", ".SH SEE ALSO"):
            self.assertIn(section, manual)
        from boswas_apps.photos import cli
        for option in ("--new-window", "--slideshow", "--gallery", "--help", "--version"):
            self.assertIn(option, cli.USAGE)
            self.assertIn(option.replace("-", "\\-"), manual, option)
        for path in [*self.SOURCES, self.ROOT / "man" / "boswas-photos.1", self.ROOT / "bin" / "boswas-photos"]:
            self.assertNotIn(b"\r", path.read_bytes(), path.name)


if __name__ == "__main__":
    unittest.main()
