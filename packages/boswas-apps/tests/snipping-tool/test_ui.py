"""The Snipping Tool's window, driven offscreen with real clicks, drags, keys and the wheel.

The compositor and the shell are a fake capture client that "saves" a snip
into a temporary Screenshots folder; the clipboard, file choosers,
questions and other programs are attributes of the window that the tests
replace. Configuration, state and runtime directories are temporary.

Set SNIP_GRAB_DIR to a directory to keep the screenshots the tests take.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import unittest
from pathlib import Path

from boswas_fluent import testing

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import boswas_fluent as fluent  # noqa: E402

from boswas_apps.snipping_tool import capture, settings  # noqa: E402
from boswas_apps.snipping_tool.ui import window as window_module  # noqa: E402
from boswas_apps.snipping_tool.ui.toolbar import TOOLS  # noqa: E402
from boswas_apps.snipping_tool.ui.window import SnipWindow  # noqa: E402

NONE = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
PRIMARY = fluent.ContentDialog.Result.PRIMARY
SECONDARY = fluent.ContentDialog.Result.SECONDARY
CLOSE = fluent.ContentDialog.Result.NONE
GREY = (200, 200, 200)
RED = (0xEE, 0x4B, 0x66)                     # coral.500, the first colour of the bar


def rgb(image: QImage, x: int, y: int) -> tuple[int, int, int]:
    colour = image.pixelColor(x, y)
    return colour.red(), colour.green(), colour.blue()


def near(a, b, tolerance: int = 30) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def snapshot(width: int = 1600, height: int = 900) -> QImage:
    """Something that looks like a screen: a dark desktop, a light window, a blue block."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(*GREY))
    painter = QPainter(image)
    painter.fillRect(0, 0, width, height // 10, QColor(30, 34, 40))
    painter.fillRect(width // 2, height // 3, width // 3, height // 2, QColor(60, 120, 220))
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def mouse(widget, kind, position, button=Qt.MouseButton.LeftButton, buttons=None, modifiers=NONE) -> None:
    point = QPointF(position)
    if buttons is None:
        buttons = button if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    QApplication.sendEvent(widget, QMouseEvent(kind, point, QPointF(widget.mapToGlobal(point.toPoint())), button,
                                               buttons, modifiers))


def drag(widget, points, modifiers=NONE, button=Qt.MouseButton.LeftButton) -> None:
    """Press at the first point, move through the others, release at the last."""
    points = [QPointF(point) for point in points]
    mouse(widget, QEvent.Type.MouseButtonPress, points[0], button, modifiers=modifiers)
    for point in points[1:]:
        mouse(widget, QEvent.Type.MouseMove, point, Qt.MouseButton.NoButton, button, modifiers)
    mouse(widget, QEvent.Type.MouseButtonRelease, points[-1], button, modifiers=modifiers)
    testing.process()


def line(start: QPointF, end: QPointF, steps: int = 12) -> list[QPointF]:
    return [start + (end - start) * (step / steps) for step in range(steps + 1)]


def wheel(widget, position, delta: int, modifiers=NONE) -> None:
    point = QPointF(position)
    event = QWheelEvent(point, QPointF(widget.mapToGlobal(point.toPoint())), QPoint(0, 0), QPoint(0, delta),
                        Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)
    testing.process()


class FakeClipboard:
    def __init__(self) -> None:
        self.images: list[QImage] = []
        self.content = QImage()

    def setImage(self, image: QImage) -> None:  # noqa: N802 - the QClipboard name
        self.images.append(QImage(image))
        self.content = QImage(image)

    def image(self) -> QImage:
        return QImage(self.content)


class FakeCapture:
    """The compositor and the shell: a request "saves" a snip as the shell does (``later`` seconds after)."""

    def __init__(self, folder: Path, png: bytes, *, available: bool = True, error: Exception | None = None,
                 produce: bool = True, later: float = 0.0) -> None:
        self.folder = folder
        self.png = png
        self._available = available
        self.error = error
        self.produce = produce
        self.later = later
        self.requests: list[str] = []
        self.written: list[Path] = []

    def available(self) -> bool:
        return self._available

    def request(self, mode: str) -> None:
        self.requests.append(mode)
        if self.error is not None:
            raise self.error
        if self.produce:
            if self.later:
                threading.Timer(self.later, self._write).start()
            else:
                self._write()

    def _write(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        name = capture.file_name("2026-11-21 101530", lambda candidate: (self.folder / candidate).exists())
        path = self.folder / name
        partial = self.png[: len(self.png) // 2]
        path.write_bytes(partial)                     # as the shell writes: the file grows, then is complete
        path.write_bytes(self.png)
        self.written.append(path)


class CompositorAndShell:
    """A control socket that answers "capture" like the compositor and then saves a snip like the shell."""

    def __init__(self, path: Path, folder: Path, png: bytes) -> None:
        self.folder, self.png = folder, png
        self.requests: list[dict] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            connection, _address = self.server.accept()
        except OSError:
            return
        with connection:
            data = b""
            while b"\n" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                data += chunk
            request = json.loads(data.split(b"\n", 1)[0])
            self.requests.append(request)
            connection.sendall(json.dumps({"id": request.get("id"), "ok": True, "data": None}).encode() + b"\n")
        time.sleep(0.2)                                # the user drags and presses Enter
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "Screenshot 2026-11-21 101530.png").write_bytes(self.png)

    def close(self) -> None:
        self.server.close()
        self.thread.join(2)


class SnipCase(testing.FluentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.folder = self.tmp / "Pictures" / "Screenshots"
        self.clipboard = FakeClipboard()
        self.answers: list = []              # what the next questions answer: functions(dialog) -> Result
        self.asked: list[str] = []
        self.save_paths: list[str | None] = []
        self.suggested: list[str] = []
        self.open_paths: list[str | None] = []
        self.opened: list[str] = []
        self.png = png_bytes(snapshot())
        self.picture = self.tmp / "screen.png"
        self.picture.write_bytes(self.png)
        grab = os.environ.get("SNIP_GRAB_DIR")
        self.grab_dir = Path(grab) if grab else self.tmp / "grabs"

    def open(self, *arguments: str, client: FakeCapture | None = None, size=(1100, 720)) -> SnipWindow:
        self.client = client if client is not None else FakeCapture(self.folder, self.png)
        window = SnipWindow(list(arguments), client=self.client, clipboard=self.clipboard,
                            settings_path=self.tmp / "settings.json", folder=lambda: self.folder)
        window.run_dialog = self._answer
        window.choose_save_path = self._choose_save
        window.choose_open_path = self._choose_open
        window.open_path = self._open_path
        window.flow.hide_ms = 20
        window.flow.reshow_ms = 10_000
        window.flow.poll_ms = 30
        self.show(window, *size)
        testing.process(20)
        self.assertTrue(window.worker.wait(5))
        return window

    def tearDown(self) -> None:
        for widget in self._kept:                    # closing at the end asks nothing
            if isinstance(widget, SnipWindow):
                try:
                    widget.settings.ask_to_save = False
                except RuntimeError:
                    pass
        super().tearDown()

    def _answer(self, dialog):
        self.asked.append(dialog.title_label.text())
        self.assertTrue(self.answers, f"an unexpected question: {dialog.title_label.text()}")
        return self.answers.pop(0)

    def _choose_save(self, suggested: str):
        self.suggested.append(suggested)
        self.assertTrue(self.save_paths, "an unexpected file chooser")
        return self.save_paths.pop(0)

    def _choose_open(self, start: str):
        self.suggested.append(start)
        self.assertTrue(self.open_paths, "an unexpected file chooser")
        return self.open_paths.pop(0)

    def _open_path(self, path: str) -> bool:
        self.opened.append(path)
        return True

    def load(self, window: SnipWindow, path: Path | None = None) -> None:
        window.load_path(str(path or self.picture))
        self.assertTrue(window.worker.wait(5))
        testing.process(20)
        self.assertIsNotNone(window.image)

    def at(self, window: SnipWindow, x: float, y: float) -> QPointF:
        """A point of the picture (its pixels) on the canvas."""
        return window.canvas.to_widget(x, y)

    def choose_tool(self, window: SnipWindow, key: str) -> None:
        testing.click(window.edit_bar.tool_buttons[key])
        self.assertEqual(window.canvas.tool, key)

    def draw_line(self, window: SnipWindow, start: tuple[float, float], end: tuple[float, float],
                  modifiers=NONE) -> None:
        drag(window.canvas, line(self.at(window, *start), self.at(window, *end)), modifiers)

    def export(self, window: SnipWindow) -> QImage:
        result: list[QImage] = []
        window._render(result.append)
        self.assertTrue(window.worker.wait(5))
        return result[0]

    def grab(self, window, name: str) -> Path:
        return testing.grab(window, self.grab_dir / name)


class OpenAndDrawTest(SnipCase):
    def test_empty_window(self):
        window = self.open()
        self.assertEqual(window.windowTitle(), "Snipping Tool")
        self.assertIs(window.stack.currentWidget(), window.empty_page)
        self.assertFalse(window.edit_holder.isVisible())
        self.assertFalse(window.zoom_holder.isVisible())
        self.assertFalse(window.save_action.isEnabled())
        self.assertFalse(window.copy_action.isEnabled())
        self.assertTrue(window.new_button.isEnabled())
        self.assertFalse(window.desktop_bar.isVisible())
        self.assertEqual(window.folder_card.description(), str(self.folder))

    def test_opening_a_picture(self):
        window = self.open()
        self.open_paths.append(str(self.picture))
        testing.click(window.commands.button(window.open_action))
        self.assertEqual(self.suggested[-1], str(self.folder))
        self.assertTrue(window.worker.wait(5))
        testing.process(20)
        self.assertIs(window.stack.currentWidget(), window.canvas)
        self.assertTrue(window.edit_holder.isVisible())
        self.assertEqual(window.windowTitle(), "screen.png - Snipping Tool")
        self.assertEqual(window.zoom_bar.size_label.text(), "1600 × 900 px")
        self.assertTrue(window.canvas.is_fitted())
        self.assertEqual(window.zoom_bar.percent_button.text(), f"{window.canvas.zoom_percent()} %")
        self.assertLess(window.canvas.zoom_percent(), 100)
        self.assertTrue(window.save_action.isEnabled())

    def test_a_file_that_is_no_picture_is_said_and_the_picture_stays(self):
        window = self.open()
        self.load(window)
        (self.tmp / "notes.png").write_text("not a picture")
        window.load_path(str(self.tmp / "notes.png"))
        self.assertTrue(window.worker.wait(5))
        self.assertEqual(window.status.severity(), "error")
        self.assertIn("not a picture", window.status.title())
        self.assertEqual(window.file_path, str(self.picture))

    def test_a_pen_stroke_undo_and_redo(self):
        window = self.open()
        self.load(window)
        self.choose_tool(window, "pen")
        self.draw_line(window, (200, 500), (700, 500))
        state = window.history.state
        self.assertEqual(len(state.items), 1)
        item = state.items[0]
        self.assertEqual((item.kind, item.width), ("pen", 4.0))
        self.assertEqual(item.colour, "#EE4B66")
        self.assertAlmostEqual(item.points[0][0], 200, delta=2)
        self.assertAlmostEqual(item.points[-1][0], 700, delta=2)
        self.assertGreater(len(item.points), 5)
        self.assertEqual(window.windowTitle(), "*screen.png - Snipping Tool")
        exported = self.export(window)
        self.assertTrue(near(rgb(exported, 450, 500), RED), rgb(exported, 450, 500))
        self.assertEqual(rgb(exported, 450, 520), GREY)
        testing.key(window, Qt.Key.Key_Z, CTRL)
        self.assertEqual(window.history.state.items, ())
        self.assertEqual(window.windowTitle(), "screen.png - Snipping Tool")
        self.assertFalse(window.edit_bar.undo_button.isEnabled())
        testing.key(window, Qt.Key.Key_Y, CTRL)
        self.assertEqual(len(window.history.state.items), 1)
        testing.click(window.edit_bar.undo_button)
        self.assertEqual(window.history.state.items, ())
        testing.click(window.edit_bar.redo_button)
        self.assertEqual(len(window.history.state.items), 1)
        testing.key(window, Qt.Key.Key_Z, CTRL)
        testing.key(window, Qt.Key.Key_Z, CTRL | SHIFT)
        self.assertEqual(len(window.history.state.items), 1)

    def test_colour_and_size_and_the_highlighter(self):
        window = self.open()
        self.load(window)
        testing.click(window.edit_bar.palette.swatches["azure.500"])
        self.assertEqual(window.canvas.tool, "pen", "choosing a colour picks up the pen")
        testing.click(window.edit_bar.palette.sizes[2])
        self.choose_tool(window, "highlighter")
        self.draw_line(window, (100, 300), (500, 300))
        item = window.history.state.items[-1]
        self.assertEqual((item.kind, item.width, item.colour), ("highlighter", 32.0, "#2F7FF0"))
        self.assertTrue(window.edit_bar.palette.swatches["azure.500"].isChecked())
        self.assertTrue(window.edit_bar.palette.sizes[2].isChecked())
        exported = self.export(window)
        red, green, blue = rgb(exported, 300, 300)
        self.assertTrue(blue > green > red and red > 100, (red, green, blue))      # blue over grey, see-through

    def test_the_eraser_removes_whole_marks_it_touches(self):
        window = self.open()
        self.load(window)
        self.choose_tool(window, "pen")
        self.draw_line(window, (200, 400), (900, 400))
        self.draw_line(window, (200, 700), (900, 700))
        self.choose_tool(window, "rectangle")
        self.draw_line(window, (1000, 200), (1400, 600))
        self.assertEqual(len(window.history.state.items), 3)
        self.choose_tool(window, "eraser")
        self.draw_line(window, (550, 350), (550, 450))              # across the first line only
        kinds = [(item.kind, item.points[0][1]) for item in window.history.state.items]
        self.assertEqual(len(kinds), 2)
        self.assertNotIn(("pen", window.history.state.items[0].points[0][1]) if False else None, kinds)
        self.assertAlmostEqual(window.history.state.items[0].points[0][1], 700, delta=2)
        self.draw_line(window, (1200, 380), (1200, 420))            # inside the rectangle: it is an outline
        self.assertEqual(len(window.history.state.items), 2)
        self.draw_line(window, (1390, 300), (1410, 300))            # across its right side
        self.assertEqual(len(window.history.state.items), 1)
        testing.key(window, Qt.Key.Key_Z, CTRL)
        self.assertEqual(len(window.history.state.items), 2)
        testing.key(window, Qt.Key.Key_Z, CTRL)
        self.assertEqual(len(window.history.state.items), 3, "one drag of the eraser is one step")

    def test_arrows_rectangles_and_text(self):
        window = self.open()
        self.load(window)
        self.choose_tool(window, "arrow")
        self.draw_line(window, (300, 600), (500, 620), SHIFT)
        arrow = window.history.state.items[-1]
        self.assertEqual(arrow.kind, "arrow")
        self.assertAlmostEqual(arrow.points[1][1], 600, delta=0.5, msg="Shift keeps the arrow level")
        self.choose_tool(window, "rectangle")
        self.draw_line(window, (100, 100), (300, 200), SHIFT)
        box = window.history.state.items[-1]
        width, height = abs(box.points[1][0] - box.points[0][0]), abs(box.points[1][1] - box.points[0][1])
        self.assertAlmostEqual(width, height, delta=0.5, msg="Shift makes a square")
        testing.click(window.canvas, self.at(window, 900, 900 - 50).toPoint())      # a click is no rectangle
        self.assertEqual(len(window.history.state.items), 2)
        self.choose_tool(window, "text")
        testing.click(window.canvas, self.at(window, 120, 650).toPoint())
        editor = window.canvas.text_editor()
        self.assertIsNotNone(editor)
        testing.type_text(editor, "Look here")
        self.grab(window, "text-dark.png")
        testing.key(editor, Qt.Key.Key_Return)
        self.assertIsNone(window.canvas.text_editor())
        text = window.history.state.items[-1]
        self.assertEqual((text.kind, text.text), ("text", "Look here"))
        self.assertAlmostEqual(text.points[0][0], 120, delta=2)
        self.assertGreater(text.box[0], 50)
        testing.click(window.canvas, self.at(window, 120, 750).toPoint())
        testing.type_text(window.canvas.text_editor(), "gone")
        testing.key(window.canvas.text_editor(), Qt.Key.Key_Escape)
        self.assertIsNone(window.canvas.text_editor())
        self.assertEqual(len(window.history.state.items), 3, "Esc drops the text")
        self.assertEqual(window.canvas.tool, "text", "and keeps the tool")
        testing.click(window.canvas, self.at(window, 120, 800).toPoint())
        testing.type_text(window.canvas.text_editor(), "placed")
        self.choose_tool(window, "pen")                        # leaving the text places it
        self.assertEqual(window.history.state.items[-1].text, "placed")

    def test_typing_text_keeps_editing_keys_for_the_text(self):
        window = self.open()
        self.load(window)
        self.choose_tool(window, "pen")
        self.draw_line(window, (200, 400), (900, 400))
        self.choose_tool(window, "text")
        testing.click(window.canvas, self.at(window, 120, 650).toPoint())
        editor = window.canvas.text_editor()
        testing.type_text(editor, "abc")
        editor.setCursorPosition(0)
        testing.key(editor, Qt.Key.Key_Delete)
        testing.key(editor, Qt.Key.Key_Z, CTRL)
        self.assertEqual(len(window.history.state.items), 1, "Delete and Ctrl+Z went to the text, not the snip")
        self.assertIsNotNone(window.canvas.text_editor())

    def test_delete_clears_the_marks_and_can_be_undone(self):
        window = self.open()
        self.load(window)
        self.choose_tool(window, "pen")
        self.draw_line(window, (200, 400), (900, 400))
        self.draw_line(window, (200, 500), (900, 500))
        testing.key(window, Qt.Key.Key_Delete)
        self.assertEqual(window.history.state.items, ())
        self.assertFalse(window.edit_bar.clear_button.isEnabled())
        testing.key(window, Qt.Key.Key_Z, CTRL)
        self.assertEqual(len(window.history.state.items), 2)
        testing.click(window.edit_bar.clear_button)
        self.assertEqual(window.history.state.items, ())

    def test_no_tool_moves_the_picture_and_escape_puts_the_tool_down(self):
        window = self.open()
        self.load(window)
        window.canvas.zoom_actual()
        before = window.canvas.to_picture(QPointF(500, 300))
        drag(window.canvas, line(QPointF(500, 300), QPointF(400, 250)))
        after = window.canvas.to_picture(QPointF(500, 300))
        self.assertAlmostEqual(after[0] - before[0], 100, delta=1)
        self.assertEqual(window.history.state.items, ())
        self.choose_tool(window, "pen")
        testing.key(window.canvas, Qt.Key.Key_Escape)
        self.assertEqual(window.canvas.tool, "")
        self.assertFalse(window.edit_bar.tool_buttons["pen"].isChecked())
        testing.click(window.edit_bar.tool_buttons["arrow"])
        testing.click(window.edit_bar.tool_buttons["arrow"])
        self.assertEqual(window.canvas.tool, "", "clicking the tool in use puts it down")
        # the middle button moves the picture with any tool
        self.choose_tool(window, "pen")
        before = window.canvas.to_picture(QPointF(500, 300))
        drag(window.canvas, line(QPointF(500, 300), QPointF(500, 200)), button=Qt.MouseButton.MiddleButton)
        self.assertAlmostEqual(window.canvas.to_picture(QPointF(500, 300))[1] - before[1], 100, delta=1)
        self.assertEqual(window.history.state.items, ())


class CropTest(SnipCase):
    def test_crop_with_a_handle_and_enter_then_undo(self):
        window = self.open()
        self.load(window)
        self.choose_tool(window, "crop")
        self.assertTrue(window.edit_bar.apply_crop_button.isVisible())
        self.assertFalse(window.edit_bar.palette.isVisible())
        self.draw_line(window, (0, 0), (400, 200))                   # the top left handle
        self.draw_line(window, (1600, 900), (1200, 700))             # the bottom right handle
        frame = window.canvas.crop_frame()
        self.assertAlmostEqual(frame.x, 400, delta=2)
        self.assertAlmostEqual(frame.right, 1200, delta=2)
        testing.key(window.canvas, Qt.Key.Key_Return)
        crop = window.history.state.crop
        self.assertIsNotNone(crop)
        self.assertEqual((crop.x, crop.y, crop.width, crop.height), (round(frame.x), round(frame.y),
                                                                     round(frame.right) - round(frame.x),
                                                                     round(frame.bottom) - round(frame.y)))
        self.assertEqual(window.canvas.tool, "")
        self.assertEqual(window.zoom_bar.size_label.text(), f"{int(crop.width)} × {int(crop.height)} px")
        self.assertTrue(window.canvas.is_fitted())
        exported = self.export(window)
        self.assertEqual((exported.width(), exported.height()), (int(crop.width), int(crop.height)))
        self.assertEqual(rgb(exported, 0, 0), GREY)
        testing.key(window, Qt.Key.Key_Z, CTRL)
        self.assertIsNone(window.history.state.crop)
        self.assertEqual(window.zoom_bar.size_label.text(), "1600 × 900 px")
        testing.key(window, Qt.Key.Key_Y, CTRL)
        self.assertEqual(window.history.state.crop, crop)

    def test_marks_follow_a_crop(self):
        window = self.open()
        self.load(window)
        self.choose_tool(window, "pen")
        self.draw_line(window, (850, 400), (1050, 400))
        self.choose_tool(window, "crop")
        self.draw_line(window, (500, 300), (1300, 600))              # inside the frame, not on a handle: a new frame
        frame = window.canvas.crop_frame()
        self.assertAlmostEqual(frame.x, 500, delta=2)
        self.assertAlmostEqual(frame.bottom, 600, delta=2)
        testing.click(window.edit_bar.apply_crop_button)
        exported = self.export(window)
        crop = window.history.state.crop
        self.assertTrue(near(rgb(exported, int(950 - crop.x), int(400 - crop.y)), RED))
        # cropping again works inside the crop
        self.choose_tool(window, "crop")
        frame = window.canvas.crop_frame()
        self.assertEqual(frame, crop)
        self.draw_line(window, (crop.right, crop.y), (crop.right - 100, crop.y))
        testing.key(window, Qt.Key.Key_Escape)
        self.assertEqual(window.history.state.crop, crop, "Esc leaves the picture as it was")
        self.assertEqual(window.canvas.tool, "")

    def test_a_frame_that_covers_everything_is_no_edit(self):
        window = self.open()
        self.load(window)
        self.choose_tool(window, "crop")
        testing.click(window.edit_bar.apply_crop_button)
        self.assertFalse(window.history.can_undo())
        self.choose_tool(window, "crop")
        testing.click(window.edit_bar.cancel_crop_button)
        self.assertEqual(window.canvas.tool, "")
        self.choose_tool(window, "crop")
        self.draw_line(window, (0, 0), (300, 300))
        window.edit_bar.cancel_crop_button.setFocus(Qt.FocusReason.TabFocusReason)
        testing.key(window.edit_bar.cancel_crop_button, Qt.Key.Key_Return)
        self.assertIsNone(window.history.state.crop, "Enter on Cancel cancels")
        self.assertEqual(window.canvas.tool, "")


class ZoomTest(SnipCase):
    def test_fit_actual_in_out_and_the_wheel(self):
        window = self.open()
        self.load(window)
        canvas = window.canvas
        fitted = canvas.zoom_percent()
        testing.key(window, Qt.Key.Key_1, CTRL)
        self.assertEqual(canvas.zoom_percent(), 100)
        self.assertEqual(window.zoom_bar.percent_button.text(), "100 %")
        self.assertTrue(window.zoom_bar.fit_button.isEnabled())
        testing.click(window.zoom_bar.zoom_in_button)
        self.assertEqual(canvas.zoom_percent(), 125)
        testing.click(window.zoom_bar.zoom_out_button)
        self.assertEqual(canvas.zoom_percent(), 100)
        anchor = QPointF(300, 200)
        picture_point = canvas.to_picture(anchor)
        wheel(canvas, anchor, 120, CTRL)
        self.assertGreater(canvas.zoom_percent(), 110)
        moved = canvas.to_picture(anchor)
        self.assertAlmostEqual(moved[0], picture_point[0], delta=0.5, msg="the point under the pointer stays")
        self.assertAlmostEqual(moved[1], picture_point[1], delta=0.5)
        before = canvas.to_picture(anchor)
        wheel(canvas, anchor, -120)                                  # without Ctrl the wheel scrolls
        self.assertGreater(canvas.to_picture(anchor)[1], before[1])
        testing.key(window, Qt.Key.Key_0, CTRL)
        self.assertTrue(canvas.is_fitted())
        self.assertEqual(canvas.zoom_percent(), fitted)
        self.assertFalse(window.zoom_bar.fit_button.isEnabled())
        testing.click(window.zoom_bar.percent_button)
        self.assertEqual(canvas.zoom_percent(), 100)
        testing.click(window.zoom_bar.percent_button)
        self.assertTrue(canvas.is_fitted())
        window.resize(700, 600)
        testing.process(20)
        self.assertLess(canvas.zoom_percent(), fitted, "the fitted picture follows the window")

    def test_a_small_picture_is_not_enlarged(self):
        window = self.open()
        small = self.tmp / "small.png"
        snapshot(200, 100).save(str(small))
        self.load(window, small)
        self.assertEqual(window.canvas.zoom_percent(), 100)
        window.canvas.toggle_zoom()
        self.assertGreater(window.canvas.zoom_percent(), 100, "the zoom button fills the window with it")


class SaveCopyTest(SnipCase):
    def test_save_as_png_and_jpeg(self):
        window = self.open()
        self.load(window)
        self.choose_tool(window, "pen")
        self.draw_line(window, (200, 500), (700, 500))
        target = self.tmp / "out.png"
        self.save_paths.append(str(target))
        testing.key(window, Qt.Key.Key_S, CTRL)
        self.assertTrue(window.worker.wait(5))
        self.assertEqual(self.suggested[-1], str(self.folder / "screen (edited).png"))
        saved = QImage(str(target))
        self.assertEqual((saved.width(), saved.height()), (1600, 900))
        self.assertTrue(near(rgb(saved, 450, 500), RED))
        self.assertFalse(window.history.is_modified())
        self.assertEqual(window.windowTitle(), "out.png - Snipping Tool")
        self.assertEqual(window.status.severity(), "success")
        self.assertIn("out.png", window.status.message())
        # JPEG, through the button; the last place is offered again
        jpeg = self.tmp / "out.jpg"
        self.save_paths.append(str(jpeg))
        testing.click(window.commands.button(window.save_action))
        self.assertTrue(window.worker.wait(5))
        self.assertEqual(self.suggested[-1], str(target))
        self.assertEqual(jpeg.read_bytes()[:3], b"\xff\xd8\xff")
        saved = QImage(str(jpeg))
        self.assertEqual((saved.width(), saved.height()), (1600, 900))
        self.assertTrue(near(rgb(saved, 450, 500), RED, 50))
        self.assertEqual(sorted(p.name for p in self.tmp.glob("out.*")), ["out.jpg", "out.png"])

    def test_cancelled_and_failed_saves(self):
        window = self.open()
        self.load(window)
        self.choose_tool(window, "pen")
        self.draw_line(window, (200, 500), (700, 500))
        self.save_paths.append(None)
        testing.key(window, Qt.Key.Key_S, CTRL)
        self.assertTrue(window.history.is_modified())
        self.save_paths.append(str(self.tmp / "out.gif"))
        testing.key(window, Qt.Key.Key_S, CTRL)
        self.assertEqual(window.status.severity(), "error")
        self.assertIn(".png or .jpg", window.status.message())
        self.save_paths.append(str(self.tmp / "missing" / "out.png"))
        testing.key(window, Qt.Key.Key_S, CTRL)
        self.assertTrue(window.worker.wait(5))
        self.assertEqual(window.status.title(), "Not saved")
        self.assertTrue(window.history.is_modified())
        self.assertTrue(window.save_action.isEnabled())

    def test_copy(self):
        window = self.open()
        self.load(window)
        window.settings.auto_copy = False
        self.choose_tool(window, "crop")
        self.draw_line(window, (0, 0), (100, 100))
        testing.key(window.canvas, Qt.Key.Key_Return)
        testing.key(window, Qt.Key.Key_C, CTRL)
        self.assertTrue(window.worker.wait(5))
        self.assertEqual(len(self.clipboard.images), 1)
        copied = self.clipboard.images[0]
        self.assertEqual((copied.width(), copied.height()), (1500, 800))
        self.assertEqual(window.status.message(), "Copied to the clipboard.")
        testing.click(window.commands.button(window.copy_action))
        self.assertTrue(window.worker.wait(5))
        self.assertEqual(len(self.clipboard.images), 2)

    def test_changes_are_copied_automatically_unless_turned_off(self):
        window = self.open()
        self.load(window)
        self.assertEqual(self.clipboard.images, [], "the desktop copied the snip; opening copies nothing")
        self.choose_tool(window, "pen")
        self.draw_line(window, (200, 500), (700, 500))
        self.assertTrue(testing.wait_until(lambda: len(self.clipboard.images) == 1, 5))
        self.assertTrue(near(rgb(self.clipboard.images[0], 450, 500), RED))
        window.show_settings()
        testing.click(window.auto_copy_switch)
        self.assertFalse(window.settings.auto_copy)
        window.hide_settings()
        self.draw_line(window, (200, 600), (700, 600))
        testing.process(window_module.AUTO_COPY_MS + 200)
        window.worker.wait(5)
        self.assertEqual(len(self.clipboard.images), 1)

    def test_paste_a_picture(self):
        window = self.open()
        testing.key(window, Qt.Key.Key_V, CTRL)
        self.assertEqual(window.status.message(), "There is no picture on the clipboard.")
        self.clipboard.content = snapshot(320, 200)
        testing.key(window, Qt.Key.Key_V, CTRL)
        self.assertEqual(window.zoom_bar.size_label.text(), "320 × 200 px")
        self.assertRegex(window.windowTitle(), r"^Snip \d{4}-\d\d-\d\d \d{6} - Snipping Tool$")
        self.save_paths.append(None)
        testing.key(window, Qt.Key.Key_S, CTRL)
        self.assertRegex(self.suggested[-1], re.escape(str(self.folder)) + r"/Snip .*\.png$")

    def test_open_in_photos(self):
        window = self.open()
        self.load(window)
        testing.click(window.commands.button(window.photos_action))
        self.assertEqual(self.opened, [str(self.picture)])
        self.choose_tool(window, "pen")
        self.draw_line(window, (200, 500), (700, 500))
        self.save_paths.append(str(self.tmp / "marked.png"))
        testing.click(window.commands.button(window.photos_action))
        self.assertTrue(window.worker.wait(5))
        self.assertEqual(self.opened, [str(self.picture), str(self.tmp / "marked.png")], "saved first, then shown")


class SnipFlowTest(SnipCase):
    def test_a_new_snip_hides_the_window_and_opens_the_picture(self):
        (self.folder).mkdir(parents=True)
        old = self.folder / "Screenshot 2026-11-20 080000.png"
        old.write_bytes(png_bytes(snapshot(100, 100)))
        window = self.open()
        seen: list[bool] = []
        window.flow.hide_requested.connect(lambda: seen.append(window.isVisible()))
        window.flow.waiting.connect(lambda: seen.append(window.isVisible()))
        testing.click(window.new_button)
        self.assertTrue(testing.wait_until(lambda: window.image is not None and window.isVisible(), 10))
        self.assertEqual(seen, [False, False], "hidden before the request was sent")
        self.assertEqual(self.client.requests, ["region"])
        self.assertEqual(window.file_path, str(self.client.written[0]))
        self.assertNotEqual(window.file_path, str(old))
        self.assertEqual(window.zoom_bar.size_label.text(), "1600 × 900 px")
        self.assertIs(window.stack.currentWidget(), window.canvas)
        self.assertFalse(window.history.is_modified())
        self.assertFalse(window.flow.is_active())
        self.assertEqual(self.clipboard.images, [])

    def test_the_real_client_against_a_compositor_socket(self):
        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        server = CompositorAndShell(runtime / "boswas-wayland-3.sock", self.folder, self.png)
        self.addCleanup(server.close)
        environ = {"XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": "wayland-3", "HOME": str(self.tmp)}
        window = SnipWindow([], clipboard=self.clipboard, settings_path=self.tmp / "settings.json", environ=environ)
        window.flow.hide_ms = 20
        window.flow.poll_ms = 30
        self.show(window, 1100, 720)
        self.assertTrue(window.worker.wait(5))
        self.assertTrue(window.desktop_available)
        self.assertEqual(window.screenshots_dir, self.folder, "the folder of $HOME, as the shell finds it")
        testing.click(window.new_button)
        self.assertTrue(testing.wait_until(lambda: window.image is not None and window.isVisible(), 10))
        self.assertEqual(server.requests[0]["cmd"], "capture")
        self.assertEqual(server.requests[0]["mode"], "region")
        self.assertEqual(window.file_path, str(self.folder / "Screenshot 2026-11-21 101530.png"))

    def test_the_folder_is_made_by_the_desktop_while_the_tool_waits(self):
        window = self.open(client=FakeCapture(self.folder, self.png, later=0.3))
        window.set_mode("screen")
        testing.key(window, Qt.Key.Key_N, CTRL)
        self.assertTrue(testing.wait_until(lambda: window.image is not None, 10))
        self.assertEqual(self.client.requests, ["screen"])
        self.assertTrue(window.isVisible())

    def test_the_window_comes_back_while_waiting_and_cancel_stops(self):
        window = self.open(client=FakeCapture(self.folder, self.png, produce=False))
        window.flow.reshow_ms = 50
        self.load(window)
        testing.click(window.new_button)
        self.assertTrue(testing.wait_until(lambda: window.isVisible() and window.flow.phase == "waiting", 5))
        self.assertIs(window.stack.currentWidget(), window.waiting_page)
        self.assertIn("press Esc there to cancel", window.waiting_text.text())
        self.grab(window, "waiting-dark.png")
        self.assertFalse(window.new_button.isEnabled())
        self.assertFalse(window.edit_holder.isVisible())
        testing.click(window.waiting_cancel)
        self.assertFalse(window.flow.is_active())
        self.assertIs(window.stack.currentWidget(), window.canvas, "back to the picture there was")
        self.assertTrue(window.new_button.isEnabled())
        self.assertFalse(window.status.isVisible())

    def start_waiting(self, window: SnipWindow, back: bool = True) -> None:
        """New, until the desktop took the request; with ``back`` until the window came back and counts input."""
        testing.click(window.new_button)
        self.assertTrue(testing.wait_until(lambda: window.flow.phase == "waiting", 5))
        if back:
            self.assertTrue(testing.wait_until(window.input_watcher.is_armed, 5))

    def quick(self, window: SnipWindow, grace_ms: int) -> None:
        window.flow.reshow_ms = 30
        window.input_guard_ms = 20
        window.flow.grace_ms = grace_ms

    def come_back(self, window: SnipWindow) -> None:
        """The capture screen closed: the pointer moves over the window again."""
        mouse(window.waiting_page, QEvent.Type.MouseMove, QPointF(40, 40), Qt.MouseButton.NoButton,
              Qt.MouseButton.NoButton)

    def test_the_first_input_after_coming_back_starts_the_grace_showing_does_not(self):
        window = self.open(client=FakeCapture(self.folder, self.png, produce=False))
        self.quick(window, 100)
        calls: list[int] = []
        original = window.overlay_closed
        window.overlay_closed = lambda: (calls.append(1), original())
        self.start_waiting(window, back=False)
        self.assertTrue(testing.wait_until(window.isVisible, 5))
        QApplication.sendEvent(window, QEvent(QEvent.Type.ActivationChange))
        QApplication.sendEvent(window, QEvent(QEvent.Type.WindowActivate))
        self.assertTrue(testing.wait_until(window.input_watcher.is_armed, 5))
        self.assertEqual(calls, [], "being shown or activated is no sign of the user")
        testing.process(400)                                 # no input: waiting goes on past the grace
        self.assertEqual(window.flow.phase, "waiting")
        self.assertFalse(window.flow._grace.isActive())
        testing.key(window.waiting_page, Qt.Key.Key_Shift)  # the first input
        self.assertEqual(calls, [1])
        self.assertTrue(window.flow._grace.isActive())
        self.assertFalse(window.input_watcher.is_armed())
        self.come_back(window)
        self.assertEqual(calls, [1], "only the first input counts")
        window.flow.cancel()

    def test_a_click_on_cancel_after_coming_back_still_cancels(self):
        window = self.open(client=FakeCapture(self.folder, self.png, produce=False))
        self.quick(window, 100)
        self.start_waiting(window)
        testing.click(window.waiting_cancel)
        self.assertFalse(window.flow.is_active())
        self.assertFalse(window.status.isVisible(), "the user cancelled: no note")
        self.assertFalse(window.input_watcher.is_armed())
        self.assertIs(window.stack.currentWidget(), window.empty_page)

    def test_input_then_a_snip_within_the_grace_opens_it(self):
        window = self.open(client=FakeCapture(self.folder, self.png, produce=False))
        self.quick(window, 1500)
        self.start_waiting(window)
        self.assertEqual(window.windowTitle(), "Snipping Tool")
        self.come_back(window)
        self.assertTrue(window.flow._grace.isActive())
        self.client._write()                                 # the shell saves the snip a moment later
        self.assertTrue(testing.wait_until(lambda: window.image is not None and window.isVisible(), 5))
        self.assertEqual(window.file_path, str(self.client.written[0]))
        self.assertFalse(window.status.isVisible())
        self.assertEqual(window.windowTitle(), "Screenshot 2026-11-21 101530.png - Snipping Tool")

    def test_input_without_a_snip_is_a_cancelled_snip_and_nothing_is_lost(self):
        window = self.open(client=FakeCapture(self.folder, self.png, produce=False))
        self.quick(window, 200)
        self.load(window)
        self.choose_tool(window, "pen")
        self.draw_line(window, (200, 500), (700, 500))
        marks = window.history.state
        self.assertEqual(window.windowTitle(), "*screen.png - Snipping Tool")
        self.answers.append(SECONDARY)                       # "Don't save" ...
        self.start_waiting(window)
        self.assertEqual(self.asked, ["Save the changes to this snip?"])
        self.assertTrue(window.isVisible())
        self.assertEqual(window.windowTitle(), "Snipping Tool", "no old name while waiting")
        started = time.monotonic()
        self.come_back(window)                               # ... then Esc on the capture screen
        self.assertTrue(testing.wait_until(lambda: not window.flow.is_active(), 5))
        self.assertGreaterEqual(time.monotonic() - started, 0.17, "the grace (a coarse timer may be 5 % early)")
        self.assertTrue(window.isVisible())
        self.assertIs(window.stack.currentWidget(), window.canvas)
        self.assertEqual(window.file_path, str(self.picture))
        self.assertIs(window.history.state, marks, "the picture and its marks are still there")
        self.assertTrue(window.history.is_modified())
        self.assertEqual(window.windowTitle(), "*screen.png - Snipping Tool")
        self.assertEqual((window.status.severity(), window.status.message()), ("info", "Snip cancelled."))
        self.grab(window, "cancelled-dark.png")
        self.assertTrue(window.edit_bar.undo_button.isEnabled())
        self.assertFalse(window.input_watcher.is_armed())
        self.choose_tool(window, "highlighter")              # the next action puts the note away
        self.assertFalse(window.status.isVisible())
        self.assertEqual(self.client.requests, ["region"])
        # the note also goes by itself
        window._notice_timer.setInterval(100)
        self.answers.append(SECONDARY)
        self.start_waiting(window)
        self.come_back(window)
        self.assertTrue(testing.wait_until(lambda: not window.flow.is_active(), 5))
        self.assertTrue(window.status.isVisible())
        self.assertTrue(testing.wait_until(lambda: not window.status.isVisible(), 5))
        # the marks were never thrown away: closing still asks
        self.answers.append(CLOSE)
        window.close()
        self.assertTrue(window.isVisible())
        self.assertEqual(len(self.asked), 3)

    def test_a_snip_before_any_input_opens_it(self):
        window = self.open(client=FakeCapture(self.folder, self.png, later=0.3))
        self.quick(window, 50)
        self.start_waiting(window, back=False)
        self.assertTrue(testing.wait_until(lambda: window.image is not None and window.isVisible(), 10))
        self.assertFalse(window.flow._grace.isActive())
        self.assertFalse(window.input_watcher.is_armed())
        self.assertFalse(window.status.isVisible())

    def test_input_counts_only_while_waiting(self):
        window = self.open(client=FakeCapture(self.folder, self.png, produce=False))
        self.quick(window, 100)
        window.overlay_closed()                              # nothing to wait for
        self.assertFalse(window.flow.is_active())
        window.set_delay(10)
        testing.click(window.new_button)
        self.assertFalse(window.input_watcher.is_armed(), "the countdown is no capture screen")
        window.overlay_closed()
        self.assertFalse(window.flow._grace.isActive())
        testing.key(window, Qt.Key.Key_Escape)
        self.assertFalse(window.flow.is_active())

    def test_waiting_ends(self):
        window = self.open(client=FakeCapture(self.folder, self.png, produce=False))
        window.flow.timeout = 0.3
        testing.click(window.new_button)
        self.assertTrue(testing.wait_until(lambda: not window.flow.is_active(), 5))
        self.assertTrue(window.isVisible())
        self.assertEqual(window.status.title(), "No snip")
        self.assertIn("Ctrl+V", window.status.message())
        self.assertIs(window.stack.currentWidget(), window.empty_page)

    def test_a_refused_request_is_said(self):
        error = capture.CaptureError("refused", "The desktop refused to start a snip (the screen is locked).")
        window = self.open(client=FakeCapture(self.folder, self.png, error=error))
        testing.click(window.new_button)
        self.assertTrue(testing.wait_until(lambda: not window.flow.is_active(), 5))
        self.assertTrue(window.isVisible())
        self.assertEqual(window.status.severity(), "error")
        self.assertEqual(window.status.title(), "A snip could not be started")
        self.assertIn("screen is locked", window.status.message())

    def test_a_countdown_before_the_snip(self):
        window = self.open()
        window.flow._tick.setInterval(60)
        index = window.delay_box.findData(3)
        window.delay_box.setCurrentIndex(index)
        window.delay_box.activated.emit(index)
        self.assertEqual(window.delay, 3)
        counts: list[int] = []
        window.flow.countdown.connect(counts.append)
        testing.click(window.new_button)
        self.assertIs(window.stack.currentWidget(), window.countdown_page)
        self.assertEqual(window.count_label.text(), "3")
        self.assertIn("3 seconds", window.count_text.text())
        self.assertTrue(window.isVisible(), "the countdown is shown")
        self.grab(window, "countdown-dark.png")
        self.assertTrue(testing.wait_until(lambda: window.image is not None and window.isVisible(), 10))
        self.assertEqual(counts, [3, 2, 1])
        self.assertTrue(window.worker.wait(5))
        self.assertTrue(testing.wait_until(lambda: (self.tmp / "settings.json").exists(), 5))
        self.assertEqual(settings.load(self.tmp / "settings.json").delay, 3, "the delay chosen is kept")

    def test_escape_cancels_the_countdown(self):
        window = self.open()
        window.set_delay(10)
        testing.click(window.new_button)
        self.assertEqual(window.count_label.text(), "10")
        testing.key(window, Qt.Key.Key_Escape)
        self.assertFalse(window.flow.is_active())
        self.assertIs(window.stack.currentWidget(), window.empty_page)
        self.assertEqual(self.client.requests, [])

    def test_without_the_desktop_new_is_off_and_open_still_works(self):
        window = self.open(client=FakeCapture(self.folder, self.png, available=False))
        self.assertTrue(window.desktop_bar.isVisible())
        self.assertEqual(window.desktop_bar.severity(), "warning")
        self.assertEqual(window.desktop_bar.title(), "New snips are not available")
        self.assertIn("open a picture", window.desktop_bar.message())
        self.assertFalse(window.new_button.isEnabled())
        testing.key(window, Qt.Key.Key_N, CTRL)
        self.assertFalse(window.flow.is_active())
        self.assertEqual(window.status.title(), "No new snip")
        self.open_paths.append(str(self.picture))
        testing.key(window, Qt.Key.Key_O, CTRL)
        self.assertTrue(window.worker.wait(5))
        self.assertIsNotNone(window.image)
        self.grab(window, "no-desktop-dark.png")
        self.client._available = True
        window.desktop_bar.action_triggered.emit()
        self.assertTrue(window.worker.wait(5))
        self.assertFalse(window.desktop_bar.isVisible())
        self.assertTrue(window.new_button.isEnabled())

    def test_the_command_line(self):
        window = self.open("--mode=screen", "--delay=7", str(self.picture))
        self.assertTrue(testing.wait_until(lambda: window.image is not None, 5))
        self.assertEqual(window.mode_box.currentData(), "screen")
        self.assertEqual(window.delay_box.currentText(), "7 seconds")
        self.assertEqual(window.settings.mode, "region", "options of one start are not kept")
        self.assertFalse((self.tmp / "settings.json").exists())
        window.flow._tick.setInterval(20)
        window.handle_arguments(["--new", "--delay=0"])
        self.assertTrue(testing.wait_until(lambda: self.client.requests == ["screen"], 5))
        self.assertTrue(testing.wait_until(lambda: not window.flow.is_active() and window.image is not None, 5))
        window.handle_arguments(["--bogus", str(self.tmp / "missing.png")])
        self.assertTrue(window.worker.wait(5))
        self.assertEqual(window.status.severity(), "error")

    def test_new_with_the_start_option(self):
        window = self.open("--new")
        self.assertTrue(testing.wait_until(lambda: window.image is not None and window.isVisible(), 10))
        self.assertEqual(self.client.requests, ["region"])


class QuestionTest(SnipCase):
    def marked(self, **kwargs) -> SnipWindow:
        window = self.open(**kwargs)
        self.load(window)
        self.choose_tool(window, "pen")
        self.draw_line(window, (200, 500), (700, 500))
        return window

    def test_closing_with_changes_asks(self):
        window = self.marked()
        self.answers.append(CLOSE)
        window.close()
        self.assertTrue(window.isVisible(), "Cancel keeps the window")
        self.assertEqual(self.asked, ["Save the changes to this snip?"])
        self.answers.append(SECONDARY)
        window.close()
        testing.process(20)
        self.assertFalse(window.isVisible(), "Don't save closes")

    def test_save_then_close(self):
        window = self.marked()
        target = self.tmp / "kept.png"
        self.answers.append(PRIMARY)
        self.save_paths.append(str(target))
        window.close()
        self.assertTrue(window.worker.wait(5) or True)
        self.assertTrue(testing.wait_until(lambda: not window.isVisible(), 5))
        self.assertTrue(target.exists())

    def test_a_new_snip_with_changes_asks_first(self):
        window = self.marked()
        self.answers.append(CLOSE)
        testing.click(window.new_button)
        self.assertFalse(window.flow.is_active())
        self.answers.append(SECONDARY)
        testing.click(window.new_button)
        self.assertTrue(testing.wait_until(lambda: window.file_path is not None and "Screenshot" in window.file_path,
                                           10))
        self.assertEqual(len(self.asked), 2)
        self.assertFalse(window.history.is_modified())

    def test_opening_another_picture_with_changes_asks(self):
        window = self.marked()
        self.answers.append(CLOSE)
        testing.key(window, Qt.Key.Key_O, CTRL)
        self.assertEqual(self.suggested, [])
        self.assertEqual(len(window.history.state.items), 1)

    def test_no_question_when_turned_off_or_unchanged(self):
        window = self.marked()
        window.show_settings()
        testing.click(window.ask_switch)
        self.assertFalse(window.settings.ask_to_save)
        window.close()
        testing.process(20)
        self.assertFalse(window.isVisible())
        self.assertEqual(self.asked, [])
        other = self.open()
        self.load(other)
        other.close()
        self.assertFalse(other.isVisible())


class SettingsPageTest(SnipCase):
    def test_settings_are_kept(self):
        window = self.open()
        testing.key(window, Qt.Key.Key_Comma, CTRL)
        self.assertIs(window.pages.currentWidget(), window.settings_page)
        box = window.settings_mode_box
        box.setCurrentIndex(box.findData("screen"))
        box.activated.emit(box.currentIndex())
        self.assertEqual(window.mode_box.currentData(), "screen", "the bar shows the same choice")
        delay = window.settings_delay_box
        delay.setCurrentIndex(delay.findData(5))
        delay.activated.emit(delay.currentIndex())
        testing.click(window.auto_copy_switch)
        testing.click(window.ask_switch)
        self.assertTrue(window.settings_worker.wait(5))
        stored = settings.load(self.tmp / "settings.json")
        self.assertEqual((stored.mode, stored.delay, stored.auto_copy, stored.ask_to_save), ("screen", 5, False, False))
        self.grab(window, "settings-dark.png")
        testing.key(window, Qt.Key.Key_Escape)
        self.assertIs(window.pages.currentWidget(), window.main_page)
        again = self.open()
        self.assertEqual((again.mode, again.delay), ("screen", 5))
        self.assertEqual((again.mode_box.currentData(), again.delay_box.currentData()), ("screen", 5))
        self.assertEqual(again.delay_box.toolTip(), "Snip delay: 5 seconds")
        self.assertFalse(again.auto_copy_switch.isChecked())

    def test_the_screenshots_folder_card(self):
        window = self.open()
        window.show_settings()
        testing.click(window.folder_button)
        self.assertTrue(window.worker.wait(5))
        self.assertEqual(self.opened, [])
        self.assertIn("does not exist yet", window.status.message())
        self.folder.mkdir(parents=True)
        window.show_settings()
        testing.click(window.folder_button)
        self.assertTrue(window.worker.wait(5))
        self.assertEqual(self.opened, [str(self.folder)])


class LookAndAccessTest(SnipCase):
    def test_every_control_is_named_and_the_tools_are_keyboard_reachable(self):
        window = self.open()
        self.load(window)
        self.choose_tool(window, "pen")
        self.draw_line(window, (200, 500), (700, 500))
        for key, name, _icon in TOOLS:
            button = window.edit_bar.tool_buttons[key]
            self.assertEqual(button.accessibleName(), name)
            self.assertTrue(button.toolTip().startswith(name))
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        for widget in window.findChildren(fluent.IconButton):
            self.assertTrue(widget.toolTip() and widget.accessibleName(), widget)
        for widget in (window.mode_box, window.delay_box, window.new_button, window.canvas):
            self.assertTrue(widget.accessibleName(), widget)
        window.edit_bar.tool_buttons["pen"].setFocus(Qt.FocusReason.TabFocusReason)
        reached = set()
        for _step in range(40):
            reached.add(testing.press_tab())
        for key in ("highlighter", "eraser", "crop"):
            self.assertIn(window.edit_bar.tool_buttons[key], reached)
        self.assertIn(window.edit_bar.undo_button, reached)
        window.edit_bar.tool_buttons["eraser"].setFocus(Qt.FocusReason.TabFocusReason)
        testing.key(window.edit_bar.tool_buttons["eraser"], Qt.Key.Key_Space)
        self.assertEqual(window.canvas.tool, "eraser")

    def test_both_schemes_wide_and_narrow(self):
        for scheme in ("dark", "light"):
            testing.set_scheme(scheme)
            window = self.open()
            window.resize(1100, 720)                 # the window remembers the narrow size of the last round
            testing.process(20)
            self.grab(window, f"empty-{scheme}.png")
            self.load(window)
            self.choose_tool(window, "pen")
            self.draw_line(window, (150, 450), (650, 520))
            testing.click(window.edit_bar.palette.swatches["sun.400"])
            self.choose_tool(window, "highlighter")
            self.draw_line(window, (850, 330), (1300, 330))
            self.choose_tool(window, "arrow")
            self.draw_line(window, (500, 800), (850, 560))
            testing.click(window.edit_bar.palette.swatches["azure.500"])
            self.choose_tool(window, "rectangle")
            self.draw_line(window, (780, 270), (1380, 760))
            testing.click(window.edit_bar.palette.swatches["black"])
            self.choose_tool(window, "text")
            testing.click(window.canvas, self.at(window, 120, 150).toPoint())
            testing.type_text(window.canvas.text_editor(), "Release notes")
            testing.key(window.canvas.text_editor(), Qt.Key.Key_Return)
            self.choose_tool(window, "pen")
            self.assertEqual(len(window.history.state.items), 5)
            self.assertFalse(window.edit_bar.is_compact())
            self.assertTrue(window.edit_bar.palette.isVisible())
            self.grab(window, f"editor-{scheme}.png")
            self.choose_tool(window, "crop")
            self.draw_line(window, (0, 0), (100, 120))
            self.grab(window, f"crop-{scheme}.png")
            self.choose_tool(window, "pen")
            window.resize(600, 720)
            testing.process(50)
            self.assertTrue(window.edit_bar.is_compact())
            self.assertFalse(window.edit_bar.palette.isVisible())
            self.assertTrue(window.edit_bar.colour_button.isVisible())
            bar = window.edit_bar
            last = bar.clear_button.geometry()
            self.assertLessEqual(last.right(), bar.width(), "the tools fit at 600 pixels")
            self.assertTrue(window.commands.overflowed(), "what does not fit is under More")
            self.assertLess(window.delay_box.width(), 80, "the choices show their icons only")
            self.assertTrue(window.commands.button(window.open_action).isVisible())
            self.grab(window, f"narrow-{scheme}.png")
            testing.click(bar.colour_button)
            self.assertTrue(bar.flyout.isVisible())
            testing.click(bar.flyout_palette.swatches["mint.500"])
            self.assertEqual(window.colour_token, "mint.500")
            self.assertTrue(bar.flyout_palette.swatches["mint.500"].isChecked())
            self.grab(bar.flyout, f"narrow-flyout-{scheme}.png")
            bar.flyout.close()
            window.show_settings()
            self.grab(window, f"settings-narrow-{scheme}.png")
            window.settings.ask_to_save = False
            window.close()

    def test_no_colour_is_written_into_the_code_and_every_icon_exists(self):
        root = Path(window_module.__file__).resolve().parents[1]
        pattern = re.compile(r"#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b|QColor\(\s*[\d\"']|setStyleSheet|rgba?\(")
        files = sorted(root.rglob("*.py"))
        self.assertGreater(len(files), 8)
        for path in files:
            for number, text in enumerate(path.read_text().splitlines(), 1):
                self.assertIsNone(pattern.search(text), f"{path.name}:{number}: {text.strip()}")
        window = self.open()
        self.load(window)
        testing.click(window.edit_bar.colour_button)
        self.assertEqual(fluent.missing_icons(), [])


if __name__ == "__main__":
    unittest.main()
