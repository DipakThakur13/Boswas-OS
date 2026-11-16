"""The editor window, used as a person uses it. Offscreen; every file lives in a temporary directory.

The questions the window asks (save changes? overwrite? which file?) go
through methods of the window that the tests replace; the answers are
recorded in ``self.asked``.
"""

from __future__ import annotations

import os
import re
import stat
import time
import unittest
from pathlib import Path

from boswas_fluent import testing  # selects the offscreen platform: before any other Qt import

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QDropEvent, QGuiApplication, QKeySequence, QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import boswas_fluent as fluent  # noqa: E402
from boswas_apps.text_editor import APP_ID, codec, fileio, session, settings  # noqa: E402
from boswas_apps.text_editor.app import EditorApplication  # noqa: E402
from boswas_apps.text_editor.ui import printing  # noqa: E402
from boswas_apps.text_editor.ui.window import CANCEL, DISCARD, OVERWRITE, RELOAD, SAVE  # noqa: E402

CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
PACKAGE = Path(__file__).resolve().parents[2] / "boswas_apps" / "text_editor"


class EditorTestCase(testing.FluentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.docs = self.tmp / "docs"
        self.docs.mkdir()
        self.asked: list[tuple] = []
        self.answers = {"save": CANCEL, "changed": CANCEL, "large": True, "encoding": True, "line": None,
                        "open": [], "save_file": None}
        self.context = self.new_context()
        self.window = self.new_window()

    def tearDown(self) -> None:
        for window in list(self.context.windows):
            self.discard(window)
        self.context.shutdown()
        super().tearDown()

    def new_context(self) -> EditorApplication:
        return EditorApplication(self.tmp / "xdg_config_home", self.tmp / "xdg_state_home")

    def new_window(self, context: EditorApplication | None = None, *, restore: bool = False):
        window = (context or self.context).new_window(restore=restore)
        self.keep(window)
        self.hook(window)
        window.resize(900, 600)
        self.wait(window)
        return window

    def hook(self, window) -> None:
        answers, asked = self.answers, self.asked
        window.ask_save = lambda tab: (asked.append(("save", tab.name())), answers["save"])[1]
        window.ask_changed = lambda tab: (asked.append(("changed", tab.name())), answers["changed"])[1]
        window.ask_large = lambda path, size: (asked.append(("large", os.path.basename(path))), answers["large"])[1]
        window.ask_other_encoding = lambda tab: (asked.append(("encoding", tab.name())), answers["encoding"])[1]
        window.ask_line = lambda lines: (asked.append(("line", lines)), answers["line"])[1]
        window.pick_open_files = lambda: (asked.append(("open",)), answers["open"])[1]
        window.pick_save_file = lambda tab: (asked.append(("save_file", tab.name())), answers["save_file"])[1]
        window.tell = lambda title, text, close="OK": asked.append(("tell", title, text))

    def discard(self, window) -> None:
        """Close a window without questions."""
        try:
            for tab in window.all_tabs():
                tab.area.document().setModified(False)
            window._closing = True
            window.close()
        except RuntimeError:
            pass

    def wait(self, window=None) -> None:
        window = window or self.window
        ok = testing.wait_until(lambda: not any(tab.loading or tab.saving for tab in window.all_tabs())
                                and not window.worker.pending(), 30)
        self.assertTrue(ok, "loading or saving did not finish")
        testing.process(5)

    def file(self, name: str, data: bytes | str) -> str:
        path = self.docs / name
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return str(path)

    def open(self, *paths: str, window=None):
        window = window or self.window
        window.open_paths(list(paths))
        self.wait(window)
        return window.current_tab()

    def type(self, text: str, window=None) -> None:
        area = (window or self.window).current_tab().area
        area.setFocus()
        for character in text:
            if character == "\n":
                testing.key(area, Qt.Key.Key_Return)
            elif character == "\t":
                testing.key(area, Qt.Key.Key_Tab, text="\t")
            else:
                testing.type_text(area, character)

    def trigger(self, name: str, window=None) -> None:
        (window or self.window).actions_by_name[name].trigger()
        testing.process(5)

    @property
    def tab(self):
        return self.window.current_tab()

    @property
    def text(self) -> str:
        return self.tab.text()

    def names(self, window=None) -> list[str]:
        window = window or self.window
        return [window.tab_bar.tabText(index) for index in range(window.tab_bar.count())]

    def told(self) -> list[str]:
        return [entry[2] for entry in self.asked if entry[0] == "tell"]


class TabsTest(EditorTestCase):
    def test_starts_with_one_empty_tab(self):
        self.assertEqual(self.names(), ["Untitled"])
        self.assertEqual(self.window.windowTitle(), "Untitled - Text Editor")
        self.assertFalse(self.window.tab_bar.is_marked_modified(0))
        self.assertEqual(QGuiApplication.desktopFileName() or APP_ID, APP_ID)
        self.assertTrue(self.tab.area.hasFocus())

    def test_typing_marks_the_tab(self):
        self.type("hello")
        self.assertEqual(self.text, "hello")
        self.assertTrue(self.window.tab_bar.is_marked_modified(0))
        self.assertEqual(self.window.windowTitle(), "*Untitled - Text Editor")
        self.trigger("undo")
        self.assertEqual(self.text, "")
        self.assertFalse(self.window.tab_bar.is_marked_modified(0))
        self.assertEqual(self.window.windowTitle(), "Untitled - Text Editor")

    def test_new_tabs_and_switching(self):
        self.trigger("new_tab")
        testing.click(self.window.new_tab_button)
        self.assertEqual(self.names(), ["Untitled", "Untitled 2", "Untitled 3"])
        self.assertEqual(self.window.tab_bar.currentIndex(), 2)
        self.trigger("next_tab")
        self.assertEqual(self.window.tab_bar.currentIndex(), 0)
        self.trigger("previous_tab")
        self.assertEqual(self.window.tab_bar.currentIndex(), 2)
        self.assertIs(self.window.tabs.currentWidget(), self.tab)
        self.assertEqual(self.window.actions_by_name["next_tab"].shortcut(), QKeySequence("Ctrl+Tab"))
        self.assertEqual(self.window.actions_by_name["new_tab"].shortcut(), QKeySequence("Ctrl+N"))
        self.assertEqual(self.window.actions_by_name["close_tab"].shortcut(), QKeySequence("Ctrl+W"))
        self.assertEqual(self.window.actions_by_name["new_window"].shortcut(), QKeySequence("Ctrl+Shift+N"))

    def test_drag_reorders_tabs(self):
        self.type("first")
        self.trigger("new_tab")
        self.type("second")
        bar = self.window.tab_bar
        self.assertTrue(bar.isMovable())
        bar.moveTab(1, 0)                          # what dragging a tab does
        self.assertEqual(self.names(), ["Untitled 2", "Untitled"])
        self.assertEqual([tab.text() for tab in self.window.all_tabs()], ["second", "first"])
        self.assertEqual(self.text, "second")      # the tab that was current still is
        bar.setCurrentIndex(1)
        self.assertEqual(self.text, "first")

    def test_close_tab_and_last_tab_closes_the_window(self):
        self.trigger("new_tab")
        self.trigger("close_tab")
        self.assertEqual(self.names(), ["Untitled"])
        self.assertTrue(self.window.isVisible())
        self.window.tab_bar.close_requested.emit(0)
        testing.process(30)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.context.windows, [])

    def test_closing_a_changed_tab_asks(self):
        self.trigger("new_tab")
        self.type("draft")
        self.answers["save"] = CANCEL
        self.trigger("close_tab")
        self.assertEqual(self.asked, [("save", "Untitled 2")])
        self.assertEqual(len(self.names()), 2)
        self.answers["save"] = DISCARD
        self.trigger("close_tab")
        self.assertEqual(self.names(), ["Untitled"])

    def test_save_from_the_question_then_close(self):
        self.trigger("new_tab")
        self.type("keep me")
        target = str(self.docs / "kept.txt")
        self.answers.update(save=SAVE, save_file=target)
        self.trigger("close_tab")
        self.wait()
        self.assertEqual(Path(target).read_text(), "keep me")
        self.assertEqual(self.names(), ["Untitled"])
        self.assertEqual([entry[0] for entry in self.asked], ["save", "save_file"])

    def test_cancelling_the_file_dialog_keeps_the_tab(self):
        self.type("still here")
        self.answers.update(save=SAVE, save_file=None)
        self.trigger("close_tab")
        self.assertEqual(self.names(), ["Untitled"])
        self.assertEqual(self.text, "still here")
        self.assertTrue(self.window.isVisible())

    def test_tab_menu(self):
        for name in ("a", "b", "c"):
            self.open(self.file(f"{name}.txt", name))
        menu = self.window.tab_menu(0)
        labels = [action.text() for action in menu.actions() if not action.isSeparator()]
        self.assertEqual(labels[:3], ["Close tab", "Close other tabs", "Close tabs to the right"])
        menu.actions()[2].trigger()
        self.assertEqual(self.names(), ["a.txt"])
        self.assertFalse(self.window.tab_menu(0).actions()[1].isEnabled())

    def test_middle_click_closes_a_tab(self):
        self.trigger("new_tab")
        bar = self.window.tab_bar
        testing.click(bar, bar.tabRect(1).center(), button=Qt.MouseButton.MiddleButton)
        self.assertEqual(self.names(), ["Untitled"])


class OpenSaveTest(EditorTestCase):
    def test_open_shows_the_file_and_what_it_is(self):
        tab = self.open(self.file("win.txt", b"\xef\xbb\xbfone\r\ntwo\r\n"))
        self.assertEqual(self.names(), ["win.txt"])               # the empty Untitled tab was used
        self.assertEqual(self.text, "one\ntwo\n")
        self.assertFalse(tab.is_modified())
        status = self.window.status
        self.assertEqual(status.encoding_button.text(), "UTF-8 with BOM")
        self.assertEqual(status.line_ending_button.text(), "Windows (CRLF)")
        self.assertEqual(status.position_label.text(), "Ln 1, Col 1")
        self.assertEqual(status.count_label.text(), "8 characters")
        self.assertEqual(self.context.recent, [str(self.docs / "win.txt")])
        self.assertIn(str(self.docs / "win.txt"), self.window.watcher.files())

    def test_open_dialog_takes_several_files(self):
        self.answers["open"] = [self.file("a.txt", "A"), self.file("b.txt", "B")]
        self.trigger("open")
        self.wait()
        self.assertEqual(self.names(), ["a.txt", "b.txt"])
        self.assertEqual(self.window.actions_by_name["open"].shortcut(), QKeySequence("Ctrl+O"))

    def test_opening_an_open_file_shows_its_tab(self):
        path = self.file("a.txt", "A")
        self.open(path, self.file("b.txt", "B"))
        self.open(path)
        self.assertEqual(self.names(), ["a.txt", "b.txt"])
        self.assertEqual(self.window.tab_bar.currentIndex(), 0)
        link = self.docs / "link-to-a.txt"
        os.symlink(path, link)
        self.open(str(link))
        self.assertEqual(len(self.names()), 2)

    def test_save_keeps_encoding_line_endings_and_permissions(self):
        path = self.file("win.txt", b"\xef\xbb\xbfone\r\ntwo\r\n")
        os.chmod(path, 0o640)
        self.open(path)
        self.tab.area.moveCursor(QTextCursor.MoveOperation.End)
        self.type("three\n")
        self.assertTrue(self.window.tab_bar.is_marked_modified(0))
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(path).read_bytes(), b"\xef\xbb\xbfone\r\ntwo\r\nthree\r\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        self.assertFalse(self.tab.is_modified())
        self.assertFalse(self.window.tab_bar.is_marked_modified(0))
        self.assertEqual(self.window.windowTitle(), "win.txt - Text Editor")
        self.assertEqual(self.asked, [])
        self.assertEqual(sorted(os.listdir(self.docs)), ["win.txt"])

    def test_every_encoding_is_written_back_as_it_was(self):
        cases = {"u16.txt": "﻿grüße\r\n".encode("utf-16-le"), "be.txt": "﻿a\nb".encode("utf-16-be"),
                 "ansi.txt": b"caf\xe9\r\n", "mac.txt": b"a\rb\r", "nbsp.txt": "a b\n".encode("utf-8")}
        for name, data in cases.items():
            with self.subTest(name=name):
                path = self.file(name, data)
                self.open(path)
                self.type("x")
                self.trigger("save")
                self.wait()
                written = Path(path).read_bytes()
                tab = self.tab
                self.assertEqual(written, fileio.to_bytes("x" + codec.normalize_line_endings(
                    codec.decode(data, tab.encoding)), tab.encoding, tab.line_ending))
                self.assertEqual(codec.detect(written), codec.detect(data))

    def test_save_as_and_untitled(self):
        self.type("note")
        target = str(self.docs / "note.txt")
        self.answers["save_file"] = target
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(target).read_bytes(), b"note")
        self.assertEqual(self.names(), ["note.txt"])
        other = str(self.docs / "copy.txt")
        self.answers["save_file"] = other
        self.trigger("save_as")
        self.wait()
        self.assertEqual(Path(other).read_bytes(), b"note")
        self.assertEqual(self.tab.path, other)
        self.assertEqual(self.context.recent[:2], [other, target])
        self.assertEqual(self.window.actions_by_name["save_as"].shortcut(), QKeySequence("Ctrl+Shift+S"))

    def test_save_all(self):
        first = self.file("a.txt", "a")
        second = self.file("b.txt", "b")
        self.open(first, second)
        for index in (0, 1):
            self.window.tab_bar.setCurrentIndex(index)
            self.type("!")
        self.trigger("save_all")
        self.wait()
        self.assertEqual((Path(first).read_text(), Path(second).read_text()), ("!a", "!b"))
        self.assertFalse(any(tab.is_modified() for tab in self.window.all_tabs()))

    def test_change_encoding_and_line_ending_from_the_status_bar(self):
        path = self.file("plain.txt", "café\nbar\n")
        self.open(path)
        status = self.window.status
        save_menu = status.encoding_menu().actions()[0].menu()
        self.assertEqual([action.text() for action in save_menu.actions()],
                         [encoding.label for encoding in codec.CHOICES])
        self.assertTrue(save_menu.actions()[0].isChecked())
        save_menu.actions()[2].trigger()                          # UTF-16 LE
        endings = status.line_ending_menu()
        self.assertEqual([action.text() for action in endings.actions()],
                         ["Unix (LF)", "Windows (CRLF)", "Macintosh (CR)"])
        endings.actions()[1].trigger()
        self.assertTrue(self.tab.is_modified())
        self.assertEqual((status.encoding_button.text(), status.line_ending_button.text()),
                         ("UTF-16 LE", "Windows (CRLF)"))
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(path).read_bytes(), "﻿café\r\nbar\r\n".encode("utf-16-le"))

    def test_reopen_with_another_encoding(self):
        path = self.file("k.txt", "été".encode("windows-1252"))
        self.open(path)
        self.assertEqual(self.text, "été")
        reopen = self.window.status.encoding_menu().actions()[1].menu()
        self.assertTrue(reopen.isEnabled())
        reopen.actions()[5].trigger()                             # ISO 8859-1
        self.wait()
        self.assertEqual(self.tab.encoding, codec.LATIN_1)
        self.assertEqual(self.text, "été")
        self.type("x")
        self.assertFalse(self.window.status.encoding_menu().actions()[1].menu().isEnabled())

    def test_characters_the_encoding_cannot_hold(self):
        path = self.file("ansi.txt", b"caf\xe9\n")
        self.open(path)
        self.assertEqual(self.tab.encoding, codec.WINDOWS_1252)
        self.tab.area.insertPlainText("日本")
        self.answers["encoding"] = False
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(path).read_bytes(), b"caf\xe9\n")
        self.assertTrue(self.tab.is_modified())
        self.answers["encoding"] = True
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(path).read_bytes(), "日本café\n".encode("utf-8"))
        self.assertEqual(self.tab.encoding, codec.UTF8)
        self.assertEqual([entry[0] for entry in self.asked], ["encoding", "encoding"])

    def test_mixed_line_endings_are_reported(self):
        tab = self.open(self.file("mixed.txt", b"a\r\nb\r\nc\n"))
        self.assertTrue(tab.info.isVisible())
        self.assertIn("mixes line endings (2 Windows (CRLF), 1 Unix (LF))", tab.info.message())
        self.assertEqual(tab.line_ending, "crlf")
        self.type("x")
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(tab.path).read_bytes(), b"xa\r\nb\r\nc\r\n")

    def test_what_cannot_be_opened(self):
        self.open(self.file("prog.bin", b"\x7fELF\x00\x00\x00\x01"))
        self.assertIn("is not a text file", self.told()[-1])
        self.open(str(self.docs))
        self.assertIn("is a folder", self.told()[-1])
        self.assertEqual(self.names(), ["Untitled"])
        self.assertEqual(self.context.recent, [])
        self.assertFalse(any("Traceback" in text for text in self.told()))

    def test_a_missing_file_becomes_a_new_one(self):
        path = str(self.docs / "fresh.txt")
        tab = self.open(path)
        self.assertEqual(self.names(), ["fresh.txt"])
        self.assertIn("does not exist yet", tab.info.message())
        self.type("born")
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(path).read_text(), "born")
        self.assertFalse(tab.info.isVisible())

    def test_size_limits(self):
        path = self.file("big.txt", "x" * 5000)
        limits = (fileio.LARGE_FILE, fileio.MAX_FILE)
        try:
            fileio.LARGE_FILE = 1000
            self.answers["large"] = False
            self.open(path)
            self.assertEqual(self.asked, [("large", "big.txt")])
            self.assertEqual(self.names(), ["Untitled"])
            self.answers["large"] = True
            self.open(path)
            self.assertEqual(self.tab.characters(), 5000)
            self.trigger("new_tab")
            fileio.MAX_FILE = 2000
            self.assertIsNone(self.window.open_file(self.file("huge.txt", "y" * 5000)))
            self.assertIn("opens files of up to", self.told()[-1])
        finally:
            fileio.LARGE_FILE, fileio.MAX_FILE = limits
        self.assertEqual((fileio.LARGE_FILE, fileio.MAX_FILE), (32 * 1024 * 1024, 512 * 1024 * 1024))

    def test_a_failed_save_says_why_and_keeps_the_text(self):
        self.type("important")
        self.answers["save_file"] = str(self.docs / "no-such-folder" / "x.txt")
        self.trigger("save")
        self.wait()
        self.assertIn("could not be written", self.told()[-1])
        self.assertEqual(self.text, "important")
        self.assertTrue(self.tab.is_modified())
        self.assertIsNone(self.tab.path)

    def test_trailing_spaces_are_removed_when_asked(self):
        self.context.settings["trim_trailing"] = True
        path = self.file("t.txt", "a  \nb\t\n")
        self.open(path)
        self.type("x")
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(path).read_text(), "xa\nb\n")
        self.assertEqual(self.text, "xa\nb\n")
        self.assertFalse(self.tab.is_modified())

    def test_recent_menu(self):
        first, second = self.file("a.txt", "a"), self.file("b.txt", "b")
        self.open(first, second)
        self.window._fill_recent()
        labels = [action.text() for action in self.window.recent_menu.actions() if not action.isSeparator()]
        self.assertEqual(labels, [second, first, "Clear recent files"])
        self.discard(self.window)
        window = self.new_window()
        window._fill_recent()
        window.recent_menu.actions()[1].trigger()
        self.wait(window)
        self.assertEqual(self.names(window), ["a.txt"])
        window._fill_recent()
        window.recent_menu.actions()[-1].trigger()
        self.assertEqual(self.context.recent, [])

    def test_drop_files_on_the_window(self):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(self.file("dropped.txt", "from the file manager")),
                      QUrl("https://example.invalid/x.txt")])
        event = QDropEvent(QPointF(50, 200), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)
        self.window.dropEvent(event)
        self.wait()
        self.assertEqual(self.names(), ["dropped.txt"])
        self.assertEqual(self.text, "from the file manager")
        self.tab.area.files_dropped.emit([self.file("second.txt", "2")])
        self.wait()
        self.assertEqual(self.names(), ["dropped.txt", "second.txt"])


class DiskChangeTest(EditorTestCase):
    def rewrite(self, path: str, data: str) -> None:
        Path(path).write_text(data)
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))

    def test_save_notices_that_the_file_changed(self):
        path = self.file("shared.txt", "original")
        self.open(path)
        self.type("mine ")
        self.rewrite(path, "theirs")
        self.answers["changed"] = CANCEL
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(path).read_text(), "theirs")
        self.assertTrue(self.tab.is_modified())
        self.answers["changed"] = OVERWRITE
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(path).read_text(), "mine original")
        self.assertEqual([entry[0] for entry in self.asked], ["changed", "changed"])
        self.asked.clear()
        self.type("again ")
        self.trigger("save")
        self.wait()
        self.assertEqual(self.asked, [])                           # our own save is not a foreign change

    def test_reload_instead_of_saving(self):
        path = self.file("shared.txt", "original")
        self.open(path)
        self.type("mine ")
        self.rewrite(path, "theirs")
        self.answers["changed"] = RELOAD
        self.trigger("save")
        self.wait()
        self.assertEqual(self.text, "theirs")
        self.assertFalse(self.tab.is_modified())
        self.assertEqual(Path(path).read_text(), "theirs")

    def test_an_unmodified_file_offers_a_reload(self):
        path = self.file("log.txt", "line 1\n")
        tab = self.open(path)
        self.rewrite(path, "line 1\nline 2\n")
        self.window.check_files_on_disk()
        self.assertTrue(tab.info.isVisible())
        self.assertIn("was changed by another program", tab.info.message())
        self.assertEqual(tab.info.action_button.text(), "Reload")
        self.assertEqual(self.text, "line 1\n")
        testing.click(tab.info.action_button)
        self.wait()
        self.assertEqual(self.text, "line 1\nline 2\n")
        self.assertFalse(tab.info.isVisible())
        self.window.check_files_on_disk()
        self.assertFalse(tab.info.isVisible())                     # nothing new: no message again

    def test_the_watcher_sees_the_change_by_itself(self):
        path = self.file("watched.txt", "before")
        tab = self.open(path)
        self.rewrite(path, "after")
        self.assertTrue(testing.wait_until(tab.info.isVisible, 5))

    def test_a_changed_file_with_unsaved_text_warns(self):
        path = self.file("both.txt", "base")
        tab = self.open(path)
        self.type("mine ")
        self.rewrite(path, "theirs")
        self.window.check_files_on_disk()
        self.assertIn("Reloading discards your changes", tab.info.message())
        self.assertEqual(self.text, "mine base")

    def test_a_deleted_file(self):
        path = self.file("gone.txt", "text")
        tab = self.open(path)
        os.unlink(path)
        self.window.check_files_on_disk()
        self.assertIn("was deleted or moved", tab.info.message())
        self.assertTrue(tab.is_modified())
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(path).read_text(), "text")
        self.assertEqual(self.asked, [])


class SessionTest(EditorTestCase):
    def test_tabs_and_unsaved_text_come_back(self):
        saved = self.file("saved.txt", "on disk\n")
        edited = self.file("edited.txt", b"old\r\n")
        self.open(saved, edited)
        self.type("new ")
        self.trigger("new_tab")
        self.type("never saved   \U0001f600")
        self.window.tab_bar.setCurrentIndex(1)
        self.window.close()                                        # no question: the session keeps it all
        testing.process(10)
        self.assertEqual(self.asked, [])
        self.assertFalse(self.window.isVisible())
        self.assertEqual(Path(edited).read_bytes(), b"old\r\n")    # nothing was written to the files

        context = self.new_context()
        window = self.new_window(context, restore=True)
        try:
            self.assertEqual(self.names(window), ["saved.txt", "edited.txt", "Untitled"])
            tabs = window.all_tabs()
            self.assertEqual([tab.text() for tab in tabs], ["on disk\n", "new old\n", "never saved   \U0001f600"])
            self.assertEqual([tab.is_modified() for tab in tabs], [False, True, True])
            self.assertEqual(window.tab_bar.currentIndex(), 1)
            self.assertEqual(tabs[1].line_ending, "crlf")
            self.assertTrue(window.tab_bar.is_marked_modified(1))
            window.tab_bar.setCurrentIndex(1)
            self.hook(window)
            window.actions_by_name["save"].trigger()
            self.wait(window)
            self.assertEqual(Path(edited).read_bytes(), b"new old\r\n")
        finally:
            self.discard(window)
            context.shutdown()

    def test_the_setting_turns_it_off(self):
        self.context.settings["restore_session"] = False
        self.type("draft")
        self.answers["save"] = CANCEL
        self.window.close()
        testing.process(10)
        self.assertEqual(self.asked, [("save", "Untitled")])
        self.assertTrue(self.window.isVisible())
        self.answers["save"] = DISCARD
        self.window.close()
        testing.process(30)
        self.assertFalse(self.window.isVisible())
        self.assertFalse((self.context.state_dir / session.FILE_NAME).exists())
        window = self.new_window(restore=True)
        self.assertEqual(self.names(window), ["Untitled"])
        self.assertEqual(window.current_tab().text(), "")

    def test_a_tab_too_large_for_the_session_is_asked_about(self):
        limit = session.MAX_TAB_TEXT
        try:
            session.MAX_TAB_TEXT = 100
            self.tab.area.insertPlainText("x" * 500)
            self.trigger("new_tab")
            self.type("small")
            self.assertEqual([tab.name() for tab in self.window.unsaved_outside_session()], ["Untitled"])
            self.answers["save"] = CANCEL
            self.window.close()
            testing.process(10)
            self.assertEqual(self.asked, [("save", "Untitled")])
            self.assertTrue(self.window.isVisible())
        finally:
            session.MAX_TAB_TEXT = limit

    def test_the_session_is_written_while_the_editor_runs(self):
        self.type("crash protection")
        self.window._session_timer.stop()
        self.window.save_session_soon()
        self.wait()
        states, _current = session.load(self.context.state_dir)
        self.assertEqual([state.text for state in states], ["crash protection"])

    def test_a_file_that_is_gone_is_named(self):
        path = self.file("temp.txt", "t")
        session.save(self.context.state_dir, [session.TabState(path=path), session.TabState(text="note")], 0)
        os.unlink(path)
        window = self.new_window(restore=True)
        self.assertEqual(self.names(window), ["Untitled"])
        self.assertEqual(window.current_tab().text(), "note")
        self.assertIn("temp.txt", window.current_tab().info.message())

    def test_a_second_window_asks_because_it_is_not_the_session(self):
        other = self.new_window()
        self.assertEqual(len(self.context.windows), 2)
        self.type("in the second window", other)
        self.answers["save"] = CANCEL
        other.close()
        testing.process(10)
        self.assertEqual(self.asked, [("save", "Untitled")])
        self.assertTrue(other.isVisible())
        self.answers["save"] = DISCARD
        other.close()
        testing.process(30)
        self.assertEqual(self.context.windows, [self.window])


class EditingTest(EditorTestCase):
    def test_status_bar_follows_the_cursor_and_the_selection(self):
        self.type("hello\nworld")
        status = self.window.status
        self.assertEqual(status.position_label.text(), "Ln 2, Col 6")
        self.assertEqual(status.count_label.text(), "11 characters")
        self.tab.area.select_range(0, 5)
        self.assertEqual(status.count_label.text(), "5 of 11 characters")
        self.assertEqual(status.position_label.text(), "Ln 1, Col 6")
        self.trigger("select_all")
        self.assertEqual(status.count_label.text(), "11 of 11 characters")

    def test_undo_redo_cut_copy_paste_delete(self):
        self.type("one two")
        self.tab.area.select_range(0, 3)
        self.trigger("cut")
        self.assertEqual(self.text, " two")
        self.assertEqual(QGuiApplication.clipboard().text(), "one")
        self.tab.area.moveCursor(QTextCursor.MoveOperation.End)
        self.trigger("paste")
        self.assertEqual(self.text, " twoone")
        self.tab.area.select_range(0, 4)
        self.trigger("copy")
        self.trigger("delete")
        self.assertEqual(self.text, "one")
        self.trigger("undo")
        self.assertEqual(self.text, " twoone")
        self.trigger("redo")
        self.assertEqual(self.text, "one")
        testing.key(self.tab.area, Qt.Key.Key_Z, CTRL)
        self.assertEqual(self.text, " twoone")
        testing.key(self.tab.area, Qt.Key.Key_Y, CTRL)
        self.assertEqual(self.text, "one")

    def test_pasted_text_keeps_its_characters_and_gets_plain_line_ends(self):
        QGuiApplication.clipboard().setText("a\r\nb\rc d")
        self.trigger("paste")
        self.assertEqual(self.text, "a\nb\nc d")

    def test_time_and_date(self):
        self.trigger("time_date")
        self.assertRegex(self.text, r"\d{1,2}[:.]\d{2}.* \d")
        self.assertEqual(self.window.actions_by_name["time_date"].shortcut(), QKeySequence("F5"))

    def test_go_to_line(self):
        self.tab.area.setPlainText("\n".join(f"line {number}" for number in range(1, 101)))
        self.answers["line"] = 42
        self.trigger("go_to")
        self.assertEqual(self.asked[-1], ("line", 100))
        self.assertEqual(self.window.status.position_label.text(), "Ln 42, Col 1")
        self.answers["line"] = 500
        self.trigger("go_to")
        self.assertIn("beyond the total number of lines (100)", self.told()[-1])
        self.assertEqual(self.window.status.position_label.text(), "Ln 42, Col 1")
        self.answers["line"] = None
        self.trigger("go_to")
        self.assertEqual(self.window.actions_by_name["go_to"].shortcut(), QKeySequence("Ctrl+G"))

    def test_the_go_to_dialog_itself(self):
        self.tab.area.setPlainText("a\nb\nc")
        del self.window.ask_line                                   # the real dialog

        def answer() -> None:
            dialog = self.window.findChild(fluent.ContentDialog)
            field = dialog.findChild(type(self.window.find_bar.replace_edit).__mro__[1])
            field.setText("3")
            dialog.primary_button.click()
        QTimer.singleShot(30, answer)
        self.trigger("go_to")
        self.assertEqual(self.window.status.position_label.text(), "Ln 3, Col 1")

    def test_indentation(self):
        area = self.tab.area
        area.setPlainText("one\ntwo\n\nthree")
        area.select_range(1, 8)                                    # from "one" to the start of the empty line
        testing.key(area, Qt.Key.Key_Tab, text="\t")
        self.assertEqual(self.text, "\tone\n\ttwo\n\nthree")
        testing.key(area, Qt.Key.Key_Backtab, SHIFT)
        self.assertEqual(self.text, "one\ntwo\n\nthree")
        self.context.settings.update(spaces_for_tabs=True, tab_width=4)
        self.context.settings_changed()
        area.select_range(0, 5)
        testing.key(area, Qt.Key.Key_Tab, text="\t")
        self.assertEqual(self.text, "    one\n    two\n\nthree")
        area.moveCursor(QTextCursor.MoveOperation.End)
        self.type("\n")
        testing.type_text(area, "ab")
        testing.key(area, Qt.Key.Key_Tab, text="\t")
        self.assertEqual(self.text.split("\n")[-1], "ab  ")        # up to the next tab stop
        testing.key(area, Qt.Key.Key_Backtab, SHIFT)
        self.trigger("undo")

    def test_auto_indent(self):
        self.type("\tfirst\nsecond")
        self.assertEqual(self.text, "\tfirst\n\tsecond")
        self.context.settings["auto_indent"] = False
        self.context.settings_changed()
        self.type("\nthird")
        self.assertEqual(self.text, "\tfirst\n\tsecond\nthird")

    def test_zoom(self):
        area = self.tab.area
        base = area.font().pointSizeF()
        self.trigger("zoom_in")
        self.assertEqual(self.window.status.zoom_button.text(), "110%")
        self.assertAlmostEqual(area.font().pointSizeF(), base * 1.1, places=3)
        self.trigger("zoom_out")
        self.trigger("zoom_out")
        self.assertEqual(self.window.status.zoom_button.text(), "90%")
        area.zoom_requested.emit(1)                                # Ctrl and the wheel
        self.assertEqual(self.window.zoom, 100)
        self.trigger("zoom_in")
        self.trigger("new_tab")
        self.assertAlmostEqual(self.tab.area.font().pointSizeF(), base * 1.1, places=3)
        self.window.status.zoom_menu().actions()[2].trigger()
        self.assertEqual(self.window.status.zoom_button.text(), "100%")
        self.assertFalse(self.tab.is_modified())

    def test_word_wrap_status_bar_and_line_numbers(self):
        from PySide6.QtWidgets import QPlainTextEdit
        area = self.tab.area
        self.assertEqual(area.lineWrapMode(), QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.assertTrue(self.window.actions_by_name["word_wrap"].isChecked())
        self.trigger("word_wrap")
        self.assertEqual(area.lineWrapMode(), QPlainTextEdit.LineWrapMode.NoWrap)
        self.assertFalse(self.context.settings["word_wrap"])
        self.assertTrue(self.window.status.isVisible())
        self.trigger("status_bar")
        self.assertFalse(self.window.status.isVisible())
        self.assertFalse(area.line_numbers_shown())
        self.context.settings["line_numbers"] = True
        self.context.settings_changed()
        self.assertTrue(area.line_numbers_shown())
        self.assertGreater(area.viewportMargins().left(), 20)
        self.context.worker.wait(5)
        self.assertEqual(settings.load(self.context.config_dir)["status_bar"], False)

    def test_menus_are_the_ones_of_a_notepad(self):
        def labels(menu) -> list[str]:
            return [action.text() for action in menu.actions() if not action.isSeparator()]
        expected_file = ["New tab", "New window", "Open…", "Recent", "Save", "Save as…", "Save all"]
        if printing.AVAILABLE:
            expected_file += ["Page setup…", "Print…"]
        expected_file += ["Close tab", "Close window", "Exit"]
        self.assertEqual(labels(self.window.file_menu), expected_file)
        self.assertEqual(labels(self.window.edit_menu),
                         ["Undo", "Redo", "Cut", "Copy", "Paste", "Delete", "Find", "Find next", "Find previous",
                          "Replace", "Go to…", "Select all", "Time/Date", "Font"])
        self.assertEqual(labels(self.window.view_menu), ["Zoom", "Status bar", "Word wrap"])
        self.window._sync_edit_actions()
        self.assertFalse(self.window.actions_by_name["undo"].isEnabled())
        self.assertFalse(self.window.actions_by_name["cut"].isEnabled())
        self.type("x")
        self.window._sync_edit_actions()
        self.assertTrue(self.window.actions_by_name["undo"].isEnabled())


class FindTest(EditorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tab.area.setPlainText("The cat sat.\nA Cat, a catalogue, a bobcat.\nconcat\n")
        self.tab.area.document().setModified(False)
        self.bar = self.window.find_bar

    def selection(self) -> tuple[int, str]:
        cursor = self.tab.area.textCursor()
        return cursor.selectionStart(), cursor.selectedText()

    def test_find_as_you_type_and_count(self):
        self.assertFalse(self.bar.isVisible())
        self.trigger("find")
        self.assertTrue(self.bar.isVisible())
        self.assertTrue(self.bar.find_edit.hasFocus())
        self.assertFalse(self.bar.replace_visible())
        testing.type_text(self.bar.find_edit, "cat")
        self.assertEqual(self.selection(), (4, "cat"))
        self.window._update_count()
        self.assertEqual(self.bar.count_label.text(), "1 of 5")
        testing.key(self.bar.find_edit, Qt.Key.Key_Return)
        self.assertEqual(self.selection(), (15, "Cat"))
        self.assertEqual(self.bar.count_label.text(), "2 of 5")
        testing.key(self.bar.find_edit, Qt.Key.Key_Return, SHIFT)
        self.assertEqual(self.selection(), (4, "cat"))
        testing.click(self.bar.next_button)
        testing.click(self.bar.next_button)
        self.assertEqual(self.selection(), (22, "cat"))
        testing.click(self.bar.previous_button)
        self.assertEqual(self.selection(), (15, "Cat"))

    def test_f3_and_wrap_around(self):
        self.trigger("find")
        self.bar.find_edit.setText("concat")
        self.assertEqual(self.selection(), (43, "concat"))
        self.trigger("find_next")
        self.assertEqual(self.selection(), (43, "concat"))          # wrapped to the only match
        self.bar.option_actions["wrap_around"].setChecked(False)
        self.bar.find_edit.setText("The")
        self.trigger("find_next")
        self.assertEqual(self.selection(), (0, "The"))              # no further match: stays
        self.assertFalse(self.window.find(False))
        self.assertEqual(self.window.actions_by_name["find_next"].shortcut(), QKeySequence("F3"))
        self.assertEqual(self.window.actions_by_name["find_previous"].shortcut(), QKeySequence("Shift+F3"))

    def test_options(self):
        self.trigger("find")
        self.bar.find_edit.setText("cat")
        self.bar.option_actions["match_case"].setChecked(True)
        self.window._update_count()
        self.assertEqual(self.bar.count_label.text(), "1 of 4")
        self.bar.option_actions["whole_word"].setChecked(True)
        self.window._update_count()
        self.assertEqual(self.bar.count_label.text(), "1 of 1")
        self.bar.option_actions["match_case"].setChecked(False)
        self.bar.option_actions["whole_word"].setChecked(False)
        self.bar.option_actions["regex"].setChecked(True)
        self.bar.find_edit.setText(r"\bc\w+")
        self.window._update_count()
        self.assertEqual(self.bar.count_label.text(), "1 of 4")
        self.bar.find_edit.setText("(")
        self.window._update_count()
        self.assertIn("Not a regular expression", self.bar.count_label.text())
        self.bar.find_edit.setText("zebra")
        self.window._update_count()
        self.assertEqual(self.bar.count_label.text(), "No results")
        self.assertTrue(self.context.settings["regex"])             # the options are remembered

    def test_selected_text_becomes_the_query(self):
        self.tab.area.select_range(15, 18)
        self.trigger("find")
        self.assertEqual(self.bar.query(), "Cat")

    def test_replace(self):
        self.trigger("replace")
        self.assertTrue(self.bar.replace_visible())
        self.bar.option_actions["match_case"].setChecked(True)
        self.bar.find_edit.setText("cat")
        self.bar.replace_edit.setText("dog")
        testing.click(self.bar.replace_button)
        self.assertEqual(self.text, "The dog sat.\nA Cat, a catalogue, a bobcat.\nconcat\n")
        self.assertEqual(self.selection(), (22, "cat"))
        testing.key(self.bar.replace_edit, Qt.Key.Key_Return)
        self.assertEqual(self.text, "The dog sat.\nA Cat, a dogalogue, a bobcat.\nconcat\n")
        self.assertTrue(self.tab.is_modified())

    def test_replace_all_is_one_undo_step(self):
        self.trigger("replace")
        self.bar.find_edit.setText("cat")
        self.bar.replace_edit.setText("dog")
        testing.click(self.bar.replace_all_button)
        self.assertEqual(self.text, "The dog sat.\nA dog, a dogalogue, a bobdog.\ncondog\n")
        self.assertEqual(self.bar.count_label.text(), "5 replaced")
        self.trigger("undo")
        self.assertEqual(self.text, "The cat sat.\nA Cat, a catalogue, a bobcat.\nconcat\n")

    def test_regular_expression_replacement(self):
        self.trigger("replace")
        self.bar.option_actions["regex"].setChecked(True)
        self.bar.find_edit.setText(r"(\w+)cat\b")
        self.bar.replace_edit.setText(r"cat-$1")
        self.assertEqual(self.window.replace_all(), 2)
        self.assertEqual(self.text, "The cat sat.\nA Cat, a catalogue, a cat-bob.\ncat-con\n")

    def test_escape_closes_the_bar(self):
        self.trigger("find")
        testing.key(self.bar.find_edit, Qt.Key.Key_Escape)
        self.assertFalse(self.bar.isVisible())
        self.assertTrue(self.tab.area.hasFocus())
        self.trigger("find_next")                                   # nothing to look for: the bar opens
        self.assertTrue(self.bar.isVisible())
        testing.click(self.bar.close_button)
        self.assertFalse(self.bar.isVisible())
        testing.click(self.bar.expand_button) if self.bar.isVisible() else None

    def test_emoji_do_not_shift_the_selection(self):
        self.tab.area.setPlainText("\U0001f600\U0001f600 target \U0001f600 target")
        self.trigger("find")
        self.bar.find_edit.setText("target")
        self.assertEqual(self.tab.area.textCursor().selectedText(), "target")
        self.trigger("find_next")
        self.assertEqual(self.tab.area.textCursor().selectedText(), "target")
        self.assertEqual(self.tab.area.textCursor().selectionStart(), 15)


class LargeFileTest(EditorTestCase):
    def test_twenty_megabytes_open_without_holding_up_the_window(self):
        line = "The quick brown fox jumps over the lazy dog 0123456789\n"
        count = 20 * 1024 * 1024 // len(line)
        path = self.file("big.log", line * count)
        gaps: list[float] = []
        last = [time.monotonic()]

        def tick() -> None:
            now = time.monotonic()
            gaps.append(now - last[0])
            last[0] = now
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(tick)
        started = time.monotonic()
        timer.start()
        self.window.open_paths([path])
        tab = self.tab
        self.assertTrue(tab.loading)
        self.assertTrue(tab.progress.isVisible())                   # a progress ring with a Cancel button
        self.assertTrue(tab.cancel_button.isVisible())
        seen = set()
        while tab.loading:
            testing.process(1)
            seen.add(round(tab.ring.value()))
            self.assertLess(time.monotonic() - started, 60)
        timer.stop()
        self.wait()
        self.assertEqual(tab.area.blockCount(), count + 1)
        self.assertEqual(tab.characters(), len(line) * count)
        self.assertFalse(tab.is_modified())
        self.assertFalse(tab.progress.isVisible())
        self.assertGreater(len(seen), 5, "the progress did not move")
        # The event loop kept turning: no single step held the window for long.
        self.assertGreater(len(gaps), 20)
        self.assertLess(max(gaps), 1.0, f"the window was held up for {max(gaps):.2f} s")

        tab.area.moveCursor(QTextCursor.MoveOperation.End)          # typing stays instant
        started = time.monotonic()
        for _ in range(50):
            testing.type_text(tab.area, "x")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(self.window.status.count_label.text(), f"{len(line) * count + 50:,} characters")
        self.assertTrue(self.window.unsaved_outside_session())      # too large for the session: closing asks
        self.trigger("save")
        self.wait()
        self.assertEqual(os.path.getsize(path), len(line) * count + 50)

    def test_loading_can_be_cancelled(self):
        path = self.file("long.txt", "line of text\n" * 300000)
        self.window.open_paths([path])
        tab = self.tab
        self.assertTrue(tab.loading)
        testing.click(tab.cancel_button)
        self.wait()
        self.assertEqual(self.names(), ["Untitled"])
        self.assertEqual(self.tab.text(), "")
        self.assertEqual(self.told(), [])
        self.assertEqual(self.context.recent, [])


class SettingsPageTest(EditorTestCase):
    def test_page_opens_and_goes_back(self):
        testing.click(self.window.settings_button)
        self.assertTrue(self.window.settings_shown())
        testing.key(self.window.settings_page, Qt.Key.Key_Escape)
        self.assertFalse(self.window.settings_shown())
        self.trigger("font")
        self.assertTrue(self.window.settings_shown())
        testing.click(self.window.settings_page.back_button)
        self.assertFalse(self.window.settings_shown())
        self.assertTrue(self.tab.area.hasFocus())

    def test_switches_change_the_editor_and_are_saved(self):
        from PySide6.QtWidgets import QPlainTextEdit
        page = self.window.settings_page
        self.window.show_settings()
        testing.click(page.controls["line_numbers"])
        testing.click(page.controls["word_wrap"])
        testing.click(page.controls["spaces_for_tabs"])
        page.controls["tab_width"].setValue(2)
        page.controls["font_size"].setCurrentIndex(page.controls["font_size"].findData(16))
        area = self.tab.area
        self.assertTrue(area.line_numbers_shown())
        self.assertEqual(area.lineWrapMode(), QPlainTextEdit.LineWrapMode.NoWrap)
        self.assertFalse(self.window.actions_by_name["word_wrap"].isChecked())
        self.assertAlmostEqual(area.font().pointSizeF(), 16.0, places=2)
        self.assertEqual(area.tab_width, 2)
        self.context.worker.wait(5)
        stored = settings.load(self.context.config_dir)
        self.assertEqual((stored["line_numbers"], stored["word_wrap"], stored["tab_width"], stored["font_size"]),
                         (True, False, 2, 16))
        again = self.new_context()
        self.assertEqual(again.settings["font_size"], 16)
        again.shutdown()

    def test_theme(self):
        page = self.window.settings_page
        page.controls["theme"].setCurrentIndex(page.controls["theme"].findData("light"))
        self.assertEqual(fluent.manager().scheme, "light")
        page.controls["theme"].setCurrentIndex(page.controls["theme"].findData("dark"))
        self.assertEqual(fluent.manager().scheme, "dark")

    def test_new_file_line_ending(self):
        self.context.settings["default_line_ending"] = "crlf"
        self.trigger("new_tab")
        self.assertEqual(self.window.status.line_ending_button.text(), "Windows (CRLF)")
        self.type("a\nb")
        target = str(self.docs / "crlf.txt")
        self.answers["save_file"] = target
        self.trigger("save")
        self.wait()
        self.assertEqual(Path(target).read_bytes(), b"a\r\nb")


class ApplicationTest(EditorTestCase):
    def test_a_second_start_opens_its_files_in_the_running_window(self):
        path = self.file("later.txt", "from the second start")
        window = self.context.handle_arguments([path])
        self.assertIs(window, self.window)
        self.wait()
        self.assertEqual(self.names(), ["later.txt"])
        self.assertEqual(self.text, "from the second start")

    def test_new_window_argument(self):
        window = self.context.handle_arguments(["--new-window", self.file("w.txt", "w")])
        self.keep(window)
        self.hook(window)
        self.wait(window)
        self.assertIsNot(window, self.window)
        self.assertEqual(len(self.context.windows), 2)
        self.assertEqual(self.names(window), ["w.txt"])
        self.assertEqual(self.names(), ["Untitled"])

    def test_new_window_command(self):
        self.trigger("new_window")
        self.assertEqual(len(self.context.windows), 2)
        other = self.context.windows[-1]
        self.keep(other)
        self.assertEqual(self.names(other), ["Untitled"])

    def test_arguments_travel_between_two_starts(self):
        from boswas_apps.text_editor.__main__ import forwarded, parse_arguments
        options, _rest = parse_arguments(["notes.txt", "--new-window", "file:///tmp/a%20b.txt", "/abs/x.txt"])
        sent = forwarded(options)
        self.assertEqual(sent, ["--new-window", os.path.abspath("notes.txt"), "/tmp/a b.txt", "/abs/x.txt"])
        first = fluent.SingleInstance(APP_ID, directory=self.tmp / "run")
        received: list[list] = []
        first.message_received.connect(received.append)
        self.assertTrue(first.start([]))
        second = fluent.SingleInstance(APP_ID, directory=self.tmp / "run")
        done: list[bool] = []
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: done.append(second.start(sent)))
        # The second start waits for the answer of the first: both live in this one event loop here.
        import threading
        result: list[bool] = []
        thread = threading.Thread(target=lambda: result.append(_send_from_another_thread(self.tmp / "run", sent)))
        thread.start()
        testing.wait_until(lambda: bool(received), 5)
        thread.join(5)
        first.close()
        self.assertEqual(received, [sent])
        self.assertEqual(result, [False])                           # the second start hands over and ends

    def test_exit_closes_every_window(self):
        other = self.new_window()
        self.trigger("exit")
        testing.process(20)
        self.assertEqual(self.context.windows, [])
        self.assertFalse(other.isVisible() or self.window.isVisible())

    def test_session_is_kept_when_the_program_is_told_to_quit(self):
        self.type("not closed properly")
        self.context.save_session_now()
        states, _current = session.load(self.context.state_dir)
        self.assertEqual([state.text for state in states], ["not closed properly"])


def _send_from_another_thread(directory, arguments) -> bool:
    """What a second process does: connect to the first instance and hand the arguments over."""
    import json
    import socket
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(str(Path(directory) / f"{APP_ID}.sock"))
        client.sendall(json.dumps({"v": 1, "args": arguments, "token": ""}).encode("ascii") + b"\n")
        return not client.recv(16).startswith(b"ok")
    finally:
        client.close()


@unittest.skipUnless(printing.AVAILABLE, "QtPrintSupport is not installed")
class PrintTest(EditorTestCase):
    def test_print_to_pdf(self):
        self.tab.area.setPlainText("\n".join(f"Line {number} of the document, long enough to be seen."
                                             for number in range(1, 301)))
        target = self.tmp / "out.pdf"
        printer = printing.QPrinter(printing.QPrinter.PrinterMode.ScreenResolution)
        printer.setOutputFormat(printing.QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(target))
        job = self.window.print_tab(self.tab, printer)
        self.assertIn("Printing", self.tab.info.message())
        self.assertEqual(self.tab.info.action_button.text(), "Cancel")
        self.assertTrue(testing.wait_until(lambda: self.window._print_job is None, 30))
        data = target.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 2000)
        self.assertIn("was sent to the printer", self.tab.info.message())
        pages = int(re.search(r"\((\d+) pages?\)", self.tab.info.message()).group(1))
        self.assertGreaterEqual(pages, 3)
        self.assertFalse(self.tab.is_modified())
        del job

    def test_printing_can_be_cancelled(self):
        self.tab.area.setPlainText("x\n" * 20000)
        printer = printing.QPrinter(printing.QPrinter.PrinterMode.ScreenResolution)
        printer.setOutputFormat(printing.QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(self.tmp / "cancelled.pdf"))
        self.window.print_tab(self.tab, printer)
        testing.click(self.tab.info.action_button)
        self.assertTrue(testing.wait_until(lambda: self.window._print_job is None, 10))
        self.assertIn("cancelled", self.tab.info.message())


class LookTest(EditorTestCase):
    def test_no_colours_and_one_structural_style_sheet(self):
        pattern = re.compile(r"#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b|rgba?\(|QColor\(\s*\d")
        sheets = 0
        for path in sorted(PACKAGE.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            sheets += source.count(".setStyleSheet(")
            for number, line in enumerate(source.splitlines(), start=1):
                self.assertIsNone(pattern.search(line), f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(sheets, 1)                                 # the frameless text area, nothing else
        from boswas_apps.text_editor.ui.editor import _PLAIN_AREA
        self.assertNotRegex(_PLAIN_AREA.replace("transparent", ""), r"color|#|rgb|font|px [a-z]")

    def test_icon_buttons_have_tool_tips_and_fields_have_names(self):
        window = self.window
        for button in (window.new_tab_button, window.settings_button, window.find_bar.next_button,
                       window.find_bar.previous_button, window.find_bar.options_button, window.find_bar.close_button,
                       window.find_bar.expand_button, window.settings_page.back_button, window.status.zoom_button,
                       window.status.encoding_button, window.status.line_ending_button):
            self.assertTrue(button.toolTip(), button)
            self.assertFalse(button.icon().isNull() and not button.text(), button)
        self.assertTrue(window.find_bar.find_edit.accessibleName())
        self.assertTrue(self.tab.area.accessibleName())
        self.assertEqual(fluent.missing_icons(), [])

    def test_light_and_dark(self):
        self.type("some text")
        for scheme in ("dark", "light"):
            testing.set_scheme(scheme)
            image = self.window.grab().toImage()
            self.assertEqual(image.pixelColor(450, 300).name(), fluent.qcolor("layer").name())


if __name__ == "__main__":
    unittest.main()
