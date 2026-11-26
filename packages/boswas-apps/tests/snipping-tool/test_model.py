"""The Snipping Tool without Qt: the command line, the control socket, the Screenshots folder and the wait,
the marks with their hit tests, the crop arithmetic, the undo history and the settings."""

from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from boswas_apps.snipping_tool import annotations, capture, cli, settings
from boswas_apps.snipping_tool.annotations import History, Rect, State

PNG_HEAD = capture.PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"\x00" * 17
COMPLETE_PNG = PNG_HEAD + capture.PNG_END
RED = "#EE4B66"


class Temporary(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="snip-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()


# --- the command line -----------------------------------------------------------------------------------------

class CommandLineTest(unittest.TestCase):
    def test_options_and_file(self):
        parsed = cli.parse(["--new", "--mode=screen", "--delay=5", "shot.png"], base="/home/asha")
        self.assertTrue(parsed.new)
        self.assertEqual((parsed.mode, parsed.delay, parsed.path), ("screen", 5, "/home/asha/shot.png"))
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.forwarded(), ["--new", "--mode=screen", "--delay=5", "--", "/home/asha/shot.png"])

    def test_new_window_is_accepted_and_changes_nothing(self):
        parsed = cli.parse(["--new-window"])
        self.assertEqual((parsed.errors, parsed.new, parsed.forwarded()), ([], False, []))
        self.assertIn("--new-window", cli.USAGE)

    def test_values_may_follow_as_their_own_argument_and_modes_have_other_names(self):
        parsed = cli.parse(["--mode", "Rectangle", "--delay", "10"])
        self.assertEqual((parsed.mode, parsed.delay, parsed.path), ("region", 10, None))
        self.assertEqual(cli.parse(["--mode=full-screen"]).mode, "screen")

    def test_mistakes_are_collected(self):
        for arguments, part in ((["--delay=61"], "0 to 60"), (["--delay=-1"], "0 to 60"), (["--delay=3.5"], "0 to 60"),
                                (["--delay"], "needs a value"), (["--mode=window"], "unknown mode"),
                                (["--frobnicate"], "unknown option"), (["a.png", "b.png"], "only one"),
                                (["https://example.com/a.png"], "not a local file")):
            parsed = cli.parse(arguments, base="/tmp")
            self.assertTrue(parsed.errors, arguments)
            self.assertIn(part, parsed.errors[0])

    def test_file_addresses_and_double_dash(self):
        self.assertEqual(cli.parse(["file:///home/asha/My%20shot.png"]).path, "/home/asha/My shot.png")
        self.assertEqual(cli.parse(["file://localhost/tmp/a.png"]).path, "/tmp/a.png")
        self.assertTrue(cli.parse(["file://elsewhere/tmp/a.png"]).errors)
        self.assertEqual(cli.parse(["--", "--new"], base="/tmp").path, "/tmp/--new")
        self.assertFalse(cli.parse(["--", "--new"], base="/tmp").new)
        self.assertEqual(cli.parse(["../x/./y.png"], base="/home/asha/Pictures").path, "/home/asha/x/y.png")

    def test_help_and_version_flags(self):
        self.assertTrue(cli.parse(["-h"]).help)
        self.assertTrue(cli.parse(["--version"]).version)

    def test_main_prints_help_and_version_and_refuses_mistakes(self):
        root = Path(__file__).resolve().parents[2]
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root), str(root.parents[1] / "desktop/ui/qt")]))
        environment["QT_QPA_PLATFORM"] = "offscreen"

        def run(*arguments):
            return subprocess.run([sys.executable, "-B", "-m", "boswas_apps.snipping_tool", *arguments],
                                  capture_output=True, text=True, env=environment, timeout=60)

        result = run("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--delay=N", result.stdout)
        result = run("--version")
        self.assertEqual((result.returncode, result.stdout), (0, "boswas-snipping-tool 1.0\n"))
        result = run("--mode=window")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown mode", result.stderr)

    def test_the_modules_without_qt_import_no_qt(self):
        code = ("import sys; import boswas_apps.snipping_tool.cli, boswas_apps.snipping_tool.capture, "
                "boswas_apps.snipping_tool.annotations, boswas_apps.snipping_tool.settings; "
                "print(any(name.startswith('PySide6') for name in sys.modules))")
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60,
                                env=dict(os.environ, PYTHONPATH=str(root)))
        self.assertEqual(result.stdout.strip(), "False", result.stderr)


# --- the control socket ---------------------------------------------------------------------------------------

class FakeCompositor:
    """A control socket that answers like the compositor; ``behaviour`` says how."""

    def __init__(self, path: Path, behaviour: str = "ok") -> None:
        self.path = path
        self.behaviour = behaviour
        self.lines: list[dict] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(4)
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
            self.lines.append(request)
            number = request.get("id")
            if self.behaviour == "ok":
                connection.sendall(json.dumps({"id": number, "ok": True, "data": None}).encode() + b"\n")
            elif self.behaviour == "error":
                connection.sendall(json.dumps({"id": number, "ok": False,
                                               "error": "\"capture\" is not\navailable\x07"}).encode() + b"\n")
            elif self.behaviour == "event-first":
                connection.sendall(b'{"event":"windows","data":[]}\n{"id": 999, "ok": false}\n')
                connection.sendall(json.dumps({"id": number, "ok": True, "data": None}).encode() + b"\n")
            elif self.behaviour == "garbage":
                connection.sendall(b"not json\n")
            elif self.behaviour == "close":
                pass
            elif self.behaviour == "silent":
                time.sleep(1.0)
            elif self.behaviour == "flood":
                try:
                    connection.sendall(b"x" * (capture.MAX_REPLY + 10))
                except OSError:
                    pass

    def close(self) -> None:
        self.server.close()
        self.thread.join(2)


class SocketTest(Temporary):
    def environ(self) -> dict:
        return {"XDG_RUNTIME_DIR": str(self.tmp), "WAYLAND_DISPLAY": "wayland-7"}

    def serve(self, behaviour: str) -> FakeCompositor:
        server = FakeCompositor(self.tmp / "boswas-wayland-7.sock", behaviour)
        self.addCleanup(server.close)
        return server

    def test_socket_path_rules(self):
        self.assertEqual(capture.control_socket(self.environ()), str(self.tmp / "boswas-wayland-7.sock"))
        self.assertEqual(capture.control_socket({"BOSWAS_COMPOSITOR_SOCKET": "/run/x.sock", **self.environ()}),
                         "/run/x.sock")
        self.assertEqual(capture.control_socket({"BOSWAS_COMPOSITOR_SOCKET": "relative.sock", **self.environ()}),
                         str(self.tmp / "boswas-wayland-7.sock"))
        for bad in ({}, {"XDG_RUNTIME_DIR": "run", "WAYLAND_DISPLAY": "w"}, {"XDG_RUNTIME_DIR": "/run"},
                    {"XDG_RUNTIME_DIR": "/run", "WAYLAND_DISPLAY": "../x"}):
            self.assertIsNone(capture.control_socket(bad), bad)

    def test_a_request_is_one_json_line_with_an_id_and_the_reply_is_matched(self):
        server = self.serve("ok")
        client = capture.CaptureClient(self.environ())
        self.assertTrue(client.available())
        client.request("region")
        server.thread.join(2)
        self.assertEqual(len(server.lines), 1)
        line = server.lines[0]
        self.assertEqual((line["cmd"], line["mode"]), ("capture", "region"))
        self.assertIsInstance(line["id"], int)
        self.assertEqual(set(line), {"id", "cmd", "mode"})

    def test_whole_screen(self):
        server = self.serve("ok")
        capture.CaptureClient(self.environ()).request("screen")
        server.thread.join(2)
        self.assertEqual(server.lines[0]["mode"], "screen")
        with self.assertRaises(ValueError):
            capture.CaptureClient(self.environ()).request("window")

    def test_lines_that_are_not_our_answer_are_skipped(self):
        self.serve("event-first")
        capture.CaptureClient(self.environ()).request("region")

    def test_an_error_reply_is_a_refusal_with_its_reason_cleaned(self):
        self.serve("error")
        with self.assertRaises(capture.CaptureError) as caught:
            capture.CaptureClient(self.environ()).request("region")
        self.assertEqual(caught.exception.reason, "refused")
        self.assertIn('("capture" is not available)', str(caught.exception))
        self.assertNotIn("\x07", str(caught.exception))

    def test_broken_answers(self):
        for behaviour, reason in (("garbage", "failed"), ("close", "failed"), ("flood", "failed")):
            with self.subTest(behaviour=behaviour):
                path = self.tmp / "boswas-wayland-7.sock"
                if path.exists():
                    path.unlink()
                self.serve(behaviour)
                with self.assertRaises(capture.CaptureError) as caught:
                    capture.CaptureClient(self.environ()).request("region")
                self.assertEqual(caught.exception.reason, reason)

    def test_a_compositor_that_does_not_answer_times_out(self):
        self.serve("silent")
        with self.assertRaises(capture.CaptureError) as caught:
            capture.CaptureClient(self.environ(), timeout=0.2).request("region")
        self.assertEqual(caught.exception.reason, "timeout")

    def test_missing_socket(self):
        client = capture.CaptureClient(self.environ())
        self.assertFalse(client.available())
        with self.assertRaises(capture.CaptureError) as caught:
            client.request("region")
        self.assertEqual(caught.exception.reason, "not-running")
        self.assertFalse(capture.CaptureClient({}).available())
        with self.assertRaises(capture.CaptureError):
            capture.CaptureClient({}).request("region")

    def test_a_file_that_is_not_a_socket_is_not_used(self):
        (self.tmp / "boswas-wayland-7.sock").write_text("")
        client = capture.CaptureClient(self.environ())
        self.assertFalse(client.available())
        with self.assertRaises(capture.CaptureError) as caught:
            client.request("region")
        self.assertEqual(caught.exception.reason, "not-running")

    def test_a_dead_socket_is_not_running(self):
        path = self.tmp / "boswas-wayland-7.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()                                  # the file stays, nobody listens
        with self.assertRaises(capture.CaptureError) as caught:
            capture.CaptureClient(self.environ()).request("region")
        self.assertEqual(caught.exception.reason, "not-running")


# --- the Screenshots folder: the same cases as the shell's tests --------------------------------------------

class ScreenshotsFolderTest(Temporary):
    def test_screenshots_go_to_the_pictures_folder(self):
        home = Path("/home/asha")
        self.assertEqual(capture.screenshots_dir(home, None), Path("/home/asha/Pictures/Screenshots"))
        dirs = '# comment\nXDG_DESKTOP_DIR="$HOME/Desktop"\nXDG_PICTURES_DIR="$HOME/Bilder"\n'
        self.assertEqual(capture.screenshots_dir(home, dirs), Path("/home/asha/Bilder/Screenshots"))
        self.assertEqual(capture.screenshots_dir(home, 'XDG_PICTURES_DIR="/data/pics"\n'),
                         Path("/data/pics/Screenshots"))
        for bad in ('XDG_PICTURES_DIR="relative"\n', 'XDG_PICTURES_DIR="$HOME/"\n',
                    'XDG_PICTURES_DIR="$HOME/../other"\n'):
            self.assertEqual(capture.screenshots_dir(home, bad), Path("/home/asha/Pictures/Screenshots"), bad)

    def test_more_of_the_shells_rules(self):
        home = Path("/home/asha")
        # spaces around the line and the value, quotes or none, the first usable line wins
        self.assertEqual(capture.screenshots_dir(home, '  XDG_PICTURES_DIR= "$HOME/Foto"  \n'),
                         Path("/home/asha/Foto/Screenshots"))
        self.assertEqual(capture.screenshots_dir(home, "XDG_PICTURES_DIR=$HOME/Foto\n"),
                         Path("/home/asha/Foto/Screenshots"))
        self.assertEqual(capture.screenshots_dir(home, 'XDG_PICTURES_DIR="bad"\nXDG_PICTURES_DIR="/srv/p"\n'),
                         Path("/srv/p/Screenshots"))
        self.assertEqual(capture.screenshots_dir(home, 'XDG_PICTURES_DIR="/a"\nXDG_PICTURES_DIR="/b"\n'),
                         Path("/a/Screenshots"))
        self.assertEqual(capture.screenshots_dir(home, 'XDG_PICTURES_DIR="/home/asha"\n'),
                         Path("/home/asha/Pictures/Screenshots"))
        self.assertEqual(capture.screenshots_dir(home, 'XDG_PICTURES_DIR="$HOME"\n'),
                         Path("/home/asha/Pictures/Screenshots"))
        self.assertEqual(capture.screenshots_dir(home, '# XDG_PICTURES_DIR="/x"\n'),
                         Path("/home/asha/Pictures/Screenshots"))
        self.assertEqual(capture.screenshots_dir(home, 'XDG_PICTURES_DIR="/data/a..b"\n'),
                         Path("/home/asha/Pictures/Screenshots"), "anything with .. is refused, as in the shell")

    def test_file_names_do_not_collide(self):
        self.assertEqual(capture.file_name("2026-11-15 163812", lambda _name: False),
                         "Screenshot 2026-11-15 163812.png")
        taken = {"Screenshot t.png", "Screenshot t (2).png"}
        self.assertEqual(capture.file_name("t", lambda name: name in taken), "Screenshot t (3).png")
        self.assertEqual(capture.file_name("t", lambda _name: True), "Screenshot t (999).png")

    def test_the_names_the_shell_gives_are_recognised(self):
        for name in ("Screenshot 2026-11-15 163812.png", "Screenshot 2026-11-15 163812 (2).png",
                     "Screenshot 2026-11-15 163812 (17).png", "Screenshot 2026-11-15 163812 (999).png"):
            self.assertTrue(capture.is_screenshot_name(name), name)
        for name in ("Screenshot 2026-11-15 163812.jpg", "Screenshot 2026-11-15 16381.png", "shot.png",
                     "Screenshot 2026-11-15 163812 (1).png", "Screenshot 2026-11-15 163812 (1000).png",
                     "Screenshot 2026-11-15 163812 (edited).png", "Screenshot 2026-11-15 163812.png.part"):
            self.assertFalse(capture.is_screenshot_name(name), name)

    def test_the_folder_is_found_from_the_environment(self):
        home = self.tmp / "home"
        config = self.tmp / "config"
        config.mkdir()
        (config / "user-dirs.dirs").write_text('XDG_PICTURES_DIR="$HOME/Kuvat"\n')
        environ = {"HOME": str(home), "XDG_CONFIG_HOME": str(config)}
        self.assertEqual(capture.find_screenshots_dir(environ), home / "Kuvat" / "Screenshots")
        # a relative XDG_CONFIG_HOME is ignored: ~/.config is read
        (home / ".config").mkdir(parents=True)
        (home / ".config" / "user-dirs.dirs").write_text('XDG_PICTURES_DIR="/srv/pics"\n')
        environ["XDG_CONFIG_HOME"] = "config"
        self.assertEqual(capture.find_screenshots_dir(environ), Path("/srv/pics/Screenshots"))
        # a file that is too long or not text is not read
        (home / ".config" / "user-dirs.dirs").write_bytes(b'XDG_PICTURES_DIR="/srv/x"\n'
                                                          + b"#" * capture.MAX_USER_DIRS)
        self.assertEqual(capture.find_screenshots_dir(environ), home / "Pictures" / "Screenshots")
        (home / ".config" / "user-dirs.dirs").write_bytes(b'XDG_PICTURES_DIR="/srv/\xff"\n')
        self.assertEqual(capture.find_screenshots_dir(environ), home / "Pictures" / "Screenshots")

    def test_nearest_folder(self):
        self.assertEqual(capture.nearest_folder(self.tmp / "a" / "b"), self.tmp)
        (self.tmp / "a" / "b").mkdir(parents=True)
        self.assertEqual(capture.nearest_folder(self.tmp / "a" / "b"), self.tmp / "a" / "b")


# --- waiting for the snip -------------------------------------------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class WaiterTest(Temporary):
    def setUp(self) -> None:
        super().setUp()
        self.folder = self.tmp / "Screenshots"
        self.folder.mkdir()

    def write(self, name: str, data: bytes = COMPLETE_PNG, age: float = 0.0) -> Path:
        path = self.folder / name
        path.write_bytes(data)
        if age:
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        return path

    def test_the_new_screenshot_is_picked_and_older_ones_are_ignored(self):
        self.write("Screenshot 2026-11-20 090000.png", age=3600)
        self.write("Screenshot 2026-11-21 101500.png", age=60)
        waiter = capture.ScreenshotWaiter(self.folder)
        waiter.start()
        self.assertEqual(set(waiter.before), {"Screenshot 2026-11-20 090000.png", "Screenshot 2026-11-21 101500.png"})
        self.assertIsNone(waiter.poll())
        self.write("notes.png")                                        # not the shell's naming
        self.write("Screenshot 2026-11-21 101501.jpg")
        self.assertIsNone(waiter.poll())
        new = self.write("Screenshot 2026-11-21 101530.png")
        self.assertEqual(waiter.poll(), new)

    def test_a_file_being_written_is_not_taken_until_it_is_complete(self):
        waiter = capture.ScreenshotWaiter(self.folder)
        waiter.start()
        path = self.write("Screenshot 2026-11-21 101530.png", PNG_HEAD)
        self.assertEqual(waiter.candidates(), [path])
        self.assertIsNone(waiter.poll())
        path.write_bytes(COMPLETE_PNG)
        self.assertEqual(waiter.poll(), path)

    def test_a_missing_folder_is_waited_for(self):
        folder = self.tmp / "Pictures" / "Screenshots"
        waiter = capture.ScreenshotWaiter(folder)
        waiter.start()
        self.assertEqual(waiter.before, {})
        self.assertIsNone(waiter.poll())
        folder.mkdir(parents=True)
        (folder / "Screenshot 2026-11-21 101530.png").write_bytes(COMPLETE_PNG)
        self.assertEqual(waiter.poll(), folder / "Screenshot 2026-11-21 101530.png")

    def test_an_old_file_is_not_taken_even_when_it_was_not_seen_before(self):
        waiter = capture.ScreenshotWaiter(self.folder)
        waiter.start()
        self.write("Screenshot 2026-11-01 120000.png", age=600)       # e.g. copied in from elsewhere
        self.assertIsNone(waiter.poll())

    def test_the_newest_of_two_new_files_wins(self):
        waiter = capture.ScreenshotWaiter(self.folder)
        waiter.start()
        self.write("Screenshot 2026-11-21 101530.png")
        older = self.folder / "Screenshot 2026-11-21 101530.png"
        stamp = time.time() - 1
        os.utime(older, (stamp, stamp))
        newer = self.write("Screenshot 2026-11-21 101530 (2).png")
        self.assertEqual(waiter.poll(), newer)

    def test_symbolic_links_and_folders_are_not_screenshots(self):
        waiter = capture.ScreenshotWaiter(self.folder)
        waiter.start()
        target = self.tmp / "elsewhere.png"
        target.write_bytes(COMPLETE_PNG)
        (self.folder / "Screenshot 2026-11-21 101530.png").symlink_to(target)
        (self.folder / "Screenshot 2026-11-21 101531.png").mkdir()
        self.assertIsNone(waiter.poll())
        self.assertFalse(capture.is_complete_png(self.folder / "Screenshot 2026-11-21 101530.png"))

    def test_the_wait_times_out(self):
        clock = FakeClock()
        waiter = capture.ScreenshotWaiter(self.folder, timeout=120, clock=clock)
        waiter.start()
        clock.now += 119
        self.assertFalse(waiter.expired())
        clock.now += 1
        self.assertTrue(waiter.expired())

    def test_complete_png(self):
        self.assertTrue(capture.is_complete_png(self.write("a.png")))
        self.assertFalse(capture.is_complete_png(self.write("b.png", b"")))
        self.assertFalse(capture.is_complete_png(self.write("c.png", b"GIF89a" + capture.PNG_END)))
        self.assertFalse(capture.is_complete_png(self.folder / "missing.png"))


# --- marks ----------------------------------------------------------------------------------------------------

class MarksTest(unittest.TestCase):
    def test_making_marks_checks_them(self):
        item = annotations.stroke("pen", "#ee4b66", 4, [(1, 2), (3, 4)])
        self.assertEqual((item.kind, item.colour, item.width, item.points), ("pen", RED, 4.0, ((1.0, 2.0), (3.0, 4.0))))
        for bad in (lambda: annotations.stroke("pen", "red", 4, [(0, 0)]),
                    lambda: annotations.stroke("pen", RED, 0, [(0, 0)]),
                    lambda: annotations.stroke("pen", RED, 4, []),
                    lambda: annotations.stroke("pen", RED, 4, [(math.nan, 0)]),
                    lambda: annotations.stroke("pen", RED, 4, [(1e12, 0)]),
                    lambda: annotations.stroke("arrow", RED, 4, [(0, 0)]),
                    lambda: annotations.shape("pen", RED, 4, (0, 0), (1, 1)),
                    lambda: annotations.text_item(RED, 24, (0, 0), " \n\t ", (10, 10))):
            with self.assertRaises(ValueError):
                bad()
        long = annotations.stroke("pen", RED, 4, [(i, i) for i in range(annotations.MAX_POINTS + 50)])
        self.assertEqual(len(long.points), annotations.MAX_POINTS)

    def test_text_is_one_clean_line(self):
        item = annotations.text_item(RED, 24, (5, 6), "  Look\nhere\x00 ", (80, 30))
        self.assertEqual(item.text, "Look here")
        self.assertEqual(len(annotations.clean_text("x" * 900)), annotations.MAX_TEXT)
        self.assertEqual(item.bounds(), Rect(5, 6, 80, 30))

    def test_the_eraser_touches_a_stroke_near_its_line_only(self):
        item = annotations.stroke("pen", RED, 4, [(0, 0), (100, 0), (100, 100)])
        self.assertTrue(item.hit(50, 0, 1))
        self.assertTrue(item.hit(50, 5, 3.5))                  # 5 away: radius 3.5 plus half the width 2
        self.assertFalse(item.hit(50, 6, 3.5))
        self.assertTrue(item.hit(104, 50, 2.5))
        self.assertFalse(item.hit(50, 50, 10))                 # inside the bend, far from both lines
        dot = annotations.stroke("highlighter", RED, 20, [(10, 10)])
        self.assertTrue(dot.hit(18, 10, 0))
        self.assertFalse(dot.hit(25, 10, 2))

    def test_the_eraser_touches_a_rectangles_outline_not_its_inside(self):
        item = annotations.shape("rectangle", RED, 4, (100, 100), (0, 0))
        self.assertTrue(item.hit(0, 50, 1))
        self.assertTrue(item.hit(50, 101, 1))
        self.assertFalse(item.hit(50, 50, 10))
        self.assertFalse(item.hit(120, 50, 10))

    def test_the_eraser_touches_an_arrow_and_its_head(self):
        item = annotations.shape("arrow", RED, 4, (0, 0), (100, 0))
        left, tip, right = annotations.arrow_head((0, 0), (100, 0), 4)
        self.assertEqual(tip, (100, 0))
        self.assertAlmostEqual(left[0], 84.0)
        self.assertAlmostEqual(abs(left[1]), 9.6)
        self.assertTrue(item.hit(50, 0, 1))
        self.assertTrue(item.hit(84, 9, 1))
        self.assertFalse(item.hit(50, 20, 5))
        bounds = item.bounds()
        self.assertLessEqual(bounds.y, -9.6)
        short = annotations.arrow_head((0, 0), (5, 0), 4)
        self.assertGreaterEqual(short[0][0], 0, "the head of a short arrow is never longer than the arrow")
        self.assertEqual(annotations.arrow_head((3, 3), (3, 3), 4), ((3, 3), (3, 3), (3, 3)))

    def test_text_is_touched_anywhere_in_its_box(self):
        item = annotations.text_item(RED, 24, (10, 10), "Hi", (40, 30))
        self.assertTrue(item.hit(30, 25, 0))
        self.assertTrue(item.hit(52, 25, 3))
        self.assertFalse(item.hit(60, 25, 3))

    def test_shift_constrains_shapes(self):
        self.assertEqual(annotations.constrained("rectangle", (0, 0), (30, -10)), (30, -30))
        x, y = annotations.constrained("arrow", (0, 0), (100, 10))
        self.assertAlmostEqual(y, 0)
        self.assertAlmostEqual(x, math.hypot(100, 10))
        x, y = annotations.constrained("arrow", (0, 0), (50, 45))
        self.assertAlmostEqual(x, y)


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.a = annotations.stroke("pen", RED, 4, [(10, 10), (20, 10)])
        self.b = annotations.shape("rectangle", RED, 2, (300, 300), (400, 380))
        self.c = annotations.shape("arrow", RED, 4, (50, 50), (90, 90))

    def test_adding_erasing_clearing(self):
        state = State().adding(self.a).adding(self.b).adding(self.c)
        self.assertEqual(state.items, (self.a, self.b, self.c))
        self.assertEqual(state.hits(15, 10, 1), [0])
        self.assertEqual(state.erasing([0, 2]).items, (self.b,))
        self.assertEqual(state.cleared().items, ())
        self.assertEqual(state.visible(640, 480), Rect(0, 0, 640, 480))

    def test_a_crop_keeps_the_marks_it_shows_and_drops_the_others(self):
        state = State((self.a, self.b, self.c))
        cropped = state.cropped(Rect(0, 0, 200, 200))
        self.assertEqual(cropped.crop, Rect(0, 0, 200, 200))
        self.assertEqual(cropped.items, (self.a, self.c))
        self.assertEqual(cropped.visible(640, 480), Rect(0, 0, 200, 200))
        self.assertEqual(cropped.cropped(Rect(40, 40, 100, 100)).items, (self.c,))

    def test_a_picture_takes_only_so_many_marks(self):
        state = State(tuple([self.a] * annotations.MAX_ITEMS))
        self.assertTrue(state.is_full())
        self.assertIs(state.adding(self.b), state)


class HistoryTest(unittest.TestCase):
    def test_undo_and_redo(self):
        item = annotations.stroke("pen", RED, 4, [(1, 1)])
        history = History()
        self.assertFalse(history.can_undo() or history.can_redo() or history.is_modified())
        one = history.state.adding(item)
        history.push(one)
        two = one.cropped(Rect(0, 0, 10, 10))
        history.push(two)
        self.assertTrue(history.is_modified())
        self.assertTrue(history.undo())
        self.assertIs(history.state, one)
        self.assertTrue(history.undo())
        self.assertEqual(history.state, State())
        self.assertFalse(history.is_modified())
        self.assertFalse(history.undo())
        self.assertTrue(history.redo())
        self.assertTrue(history.redo())
        self.assertIs(history.state, two)
        self.assertFalse(history.redo())

    def test_a_new_edit_drops_what_could_be_redone(self):
        history = History()
        item = annotations.stroke("pen", RED, 4, [(1, 1)])
        history.push(history.state.adding(item))
        history.undo()
        history.push(State().adding(annotations.stroke("pen", RED, 8, [(2, 2)])))
        self.assertFalse(history.can_redo())
        self.assertEqual(len(history), 2)

    def test_the_same_state_is_not_an_edit(self):
        history = History()
        history.push(State())
        self.assertEqual(len(history), 1)

    def test_saved_and_modified(self):
        history = History()
        item = annotations.stroke("pen", RED, 4, [(1, 1)])
        history.push(history.state.adding(item))
        history.mark_saved()
        self.assertFalse(history.is_modified())
        history.undo()
        self.assertTrue(history.is_modified())
        history.redo()
        self.assertFalse(history.is_modified())
        history.undo()
        history.push(State().cleared().adding(annotations.stroke("pen", RED, 2, [(5, 5)])))
        self.assertTrue(history.is_modified(), "the saved state cannot come back")
        history.undo()
        self.assertTrue(history.is_modified())

    def test_the_history_is_bounded(self):
        history = History(limit=5)
        for index in range(10):
            history.push(State().adding(annotations.stroke("pen", RED, 4, [(index, 0)])))
        self.assertEqual(len(history), 5)
        undone = 0
        while history.undo():
            undone += 1
        self.assertEqual(undone, 4)
        self.assertTrue(history.is_modified(), "the opened state was dropped from the history")
        history.reset()
        self.assertEqual((len(history), history.is_modified()), (1, False))


class CropTest(unittest.TestCase):
    bounds = Rect(0, 0, 400, 300)

    def test_handles(self):
        frame = Rect(100, 100, 200, 100)
        cases = {(100, 100): "tl", (300, 100): "tr", (300, 200): "br", (100, 200): "bl", (200, 100): "t",
                 (300, 150): "r", (200, 205): "b", (95, 150): "l", (200, 150): "move", (50, 50): None,
                 (200, 230): None}
        for (x, y), handle in cases.items():
            self.assertEqual(annotations.handle_at(frame, x, y, 10), handle, (x, y))
        tiny = Rect(100, 100, 6, 6)
        self.assertEqual(annotations.handle_at(tiny, 101, 101, 10), "tl")
        self.assertEqual(annotations.handle_at(tiny, 105, 105, 10), "br")

    def test_dragging_sides_and_corners(self):
        frame = Rect(0, 0, 400, 300)
        self.assertEqual(annotations.drag_crop(frame, "tl", 50, 30, self.bounds), Rect(50, 30, 350, 270))
        self.assertEqual(annotations.drag_crop(frame, "r", -100, 99, self.bounds), Rect(0, 0, 300, 300))
        self.assertEqual(annotations.drag_crop(frame, "b", 0, 50, self.bounds), frame, "never out of the picture")
        self.assertEqual(annotations.drag_crop(frame, "l", 1000, 0, self.bounds), Rect(392, 0, 8, 300),
                         "never smaller than the least size")
        self.assertEqual(annotations.drag_crop(frame, "t", -20, 0, self.bounds), frame)

    def test_moving_keeps_the_size_and_stays_inside(self):
        frame = Rect(100, 100, 100, 100)
        self.assertEqual(annotations.drag_crop(frame, "move", 50, -20, self.bounds), Rect(150, 80, 100, 100))
        self.assertEqual(annotations.drag_crop(frame, "move", 900, 900, self.bounds), Rect(300, 200, 100, 100))
        self.assertEqual(annotations.drag_crop(frame, "move", -900, -900, self.bounds), Rect(0, 0, 100, 100))

    def test_a_tiny_picture_lowers_the_least_size(self):
        small = Rect(0, 0, 5, 5)
        self.assertEqual(annotations.drag_crop(small, "r", -10, 0, small), Rect(0, 0, 5, 5))

    def test_a_new_frame_and_whole_pixels(self):
        bounds = Rect(10, 20, 400, 300)                # an earlier crop
        self.assertEqual(annotations.crop_between((300, 200), (-50, 400), bounds), Rect(10, 200, 290, 120))
        self.assertEqual(annotations.snapped(Rect(10.4, 20.6, 99.4, 50.2), bounds), Rect(10, 21, 100, 50))
        self.assertIsNone(annotations.snapped(Rect(10, 20, 400, 300), bounds), "all of it is no crop")
        self.assertIsNone(annotations.snapped(Rect(100, 100, 0.2, 50), bounds))
        self.assertEqual(annotations.snapped(Rect(-5, -5, 100, 100), bounds), Rect(10, 20, 85, 75))


# --- settings ---------------------------------------------------------------------------------------------------

class SettingsTest(Temporary):
    def test_defaults_and_round_trip(self):
        path = self.tmp / "settings.json"
        values = settings.load(path)
        self.assertEqual((values.mode, values.delay, values.auto_copy, values.ask_to_save), ("region", 0, True, True))
        values.mode, values.delay, values.auto_copy = "screen", 5, False
        settings.save(values, path)
        self.assertEqual(settings.load(path), values)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["settings.json"], "no temporary file is left")

    def test_wrong_values_keep_their_defaults(self):
        path = self.tmp / "settings.json"
        path.write_text(json.dumps({"mode": "window", "delay": 7, "auto_copy": "yes", "ask_to_save": False}))
        values = settings.load(path)
        self.assertEqual((values.mode, values.delay, values.auto_copy, values.ask_to_save), ("region", 0, True, False))
        path.write_text(json.dumps({"delay": True}))
        self.assertEqual(settings.load(path).delay, 0)
        for text in ("[1, 2]", "{not json", "\"text\""):
            path.write_text(text)
            self.assertEqual(settings.load(path), settings.Settings())
        path.write_bytes(b'{"mode": "screen"' + b" " * settings.MAX_BYTES + b"}")
        self.assertEqual(settings.load(path), settings.Settings())
        path.write_bytes(b'{"mode": "\xff"}')
        self.assertEqual(settings.load(path), settings.Settings())

    def test_a_failed_write_leaves_the_old_file(self):
        path = self.tmp / "settings.json"
        settings.save(settings.Settings(mode="screen"), path)
        with unittest.mock.patch("os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                settings.save(settings.Settings(mode="region"), path)
        self.assertEqual(settings.load(path).mode, "screen")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["settings.json"])


if __name__ == "__main__":
    unittest.main()
