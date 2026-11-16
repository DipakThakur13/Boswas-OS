"""The window of Archive Manager, driven the way a person drives it (Qt offscreen platform).

The engine is the real one, working on archives built in the test's
temporary directory. File choosers and "open with the default application"
are replaced through the window's hooks; nothing leaves the temporary
directory and no other program is started.
"""

from __future__ import annotations

import os
import re
import stat
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import support
from support import raw_tar, raw_zip, tar_dir, tar_link, tar_member, tree, zip_member

if support.PYSIDE:
    from boswas_fluent import testing
    from PySide6.QtCore import QItemSelectionModel, QMimeData, QPoint, Qt, QUrl
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication

    import boswas_fluent as fluent
    from boswas_apps.archive_manager import create, extract, sevenzip
    from boswas_apps.archive_manager.ui import dialogs, listmodel
    from boswas_apps.archive_manager.ui.progress import EXIT_CANCELLED, EXIT_OK, EXIT_PROBLEMS, ProgressWindow, Task
    from boswas_apps.archive_manager.ui.window import ArchiveWindow
    BASE = testing.FluentTestCase
else:                                                        # the engine tests still run without PySide6
    BASE = unittest.TestCase

SHOTS = os.environ.get("BOSWAS_TEST_SHOTS")                  # a folder: the tests leave screenshots there
SCHEME = "light" if os.environ.get("BOSWAS_TEST_SCHEME") == "light" else "dark"


@unittest.skipUnless(support.PYSIDE, "PySide6 is not installed")
class WindowCase(BASE):
    scheme = SCHEME

    def setUp(self) -> None:
        super().setUp()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.opened_paths: list[str] = []
        # The working directory is an empty folder that must still be empty afterwards (see support).
        self._scratch = support.enter_scratch_directory()
        self.addCleanup(self._leave_scratch)

    def _leave_scratch(self) -> None:
        written = support.leave_scratch_directory(self._scratch)
        self.assertEqual(written, [], "something was written into the working directory")

    def png(self) -> bytes:
        image = QImage(64, 48, QImage.Format.Format_RGB32)
        image.fill(QColor("darkcyan"))
        target = self.tmp / "p.png"
        image.save(str(target))
        return target.read_bytes()

    def sample(self, name="Sample.zip") -> Path:
        return raw_zip(self.work / name, [
            zip_member("docs/", b"", is_dir=True, method=0), zip_member("docs/guide.txt", b"Read me first.\n" * 40),
            zip_member("docs/img/logo.png", self.png(), method=0), zip_member("docs/file10.txt", b"ten"),
            zip_member("docs/file2.txt", b"two"), zip_member("zeta.bin", bytes(range(256)) * 20, method=0),
            zip_member("alpha.txt", b"alpha"), zip_member("run.sh", b"#!/bin/sh\necho hi\n", mode=stat.S_IFREG | 0o755),
            zip_member("README.md", b"# Sample\n")], comment=b"a test archive")

    def window(self, path=None, wait=True) -> "ArchiveWindow":
        window = ArchiveWindow(str(path) if path else None)
        window.open_path = lambda target: self.opened_paths.append(str(target)) or True
        window.choose_files = lambda *_args: []
        window.choose_folder = lambda *_args: None
        window.choose_archive = lambda: None
        self.show(window, 1240, 740)
        if path and wait:
            self.assertTrue(testing.wait_until(lambda: not window.busy(), 15))
            testing.process(20)
        return window

    def wait_idle(self, window, timeout=20) -> None:
        self.assertTrue(testing.wait_until(lambda: not window.busy(), timeout))
        testing.process(20)

    def wait_dialog(self, window, kind=None):
        self.assertTrue(testing.wait_until(lambda: window.dialog is not None
                                           and (kind is None or isinstance(window.dialog, kind)), 10), kind)
        testing.process(20)
        return window.dialog

    def names(self, window) -> list[str]:
        return [entry.base for entry in window.model.entries()]

    def select(self, window, *bases) -> None:
        selection = window.view.selectionModel()
        selection.clearSelection()
        for row, entry in enumerate(window.model.entries()):
            if entry.base in bases:
                index = window.model.index(row, 0)
                selection.select(index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
                selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
        testing.process()

    def shot(self, widget, name: str) -> None:
        if SHOTS:
            testing.grab(widget, Path(SHOTS) / f"{SCHEME}-{name}.png")


class BrowseTest(WindowCase):
    def test_start_page(self):
        window = self.window()
        self.assertIs(window.stack.currentWidget(), window.start_page)
        self.assertFalse(window.navigation_row.isVisible())
        for action in (window.extract_all_action, window.extract_action, window.add_files_action,
                       window.delete_action, window.test_action, window.properties_action):
            self.assertFalse(action.isEnabled(), action.text())
        self.assertTrue(window.new_action.isEnabled() and window.open_action.isEnabled())
        self.assertIn("7z", window.formats_label.text())
        self.shot(window, "start")

    def test_open_shows_folders_first_in_natural_order(self):
        window = self.window(self.sample())
        self.assertIs(window.stack.currentWidget(), window.browser)
        self.assertEqual(self.names(window), ["docs", "alpha.txt", "README.md", "run.sh", "zeta.bin"])
        self.assertEqual(window.breadcrumb.items(), ["Sample.zip"])
        self.assertEqual(window.status_left.text(), "5 items")
        self.assertIn("ZIP archive", window.status_right.text())
        self.assertIn("8 files", window.status_right.text())
        self.assertIn("Sample.zip", window.windowTitle())
        self.assertTrue(window.extract_all_action.isEnabled())
        self.assertFalse(window.extract_action.isEnabled())
        self.assertEqual(window.state.recent, [str(self.sample())])
        self.shot(window, "browse")

    def test_navigation_by_keyboard_mouse_and_path_bar(self):
        window = self.window(self.sample())
        window.view.setCurrentIndex(window.model.index(0, 0))
        testing.key(window.view, Qt.Key.Key_Return)
        self.assertEqual(window.folder, "docs")
        self.assertEqual(self.names(window), ["img", "file2.txt", "file10.txt", "guide.txt"])
        self.assertEqual(window.breadcrumb.items(), ["Sample.zip", "docs"])
        rect = window.view.visualRect(window.model.index(0, 0))
        testing.click(window.view.viewport(), rect.center())
        window.view.doubleClicked.emit(window.model.index(0, 0))
        testing.process()
        self.assertEqual(window.folder, "docs/img")
        testing.click(window.back_button)
        self.assertEqual(window.folder, "docs")
        testing.click(window.forward_button)
        self.assertEqual(window.folder, "docs/img")
        testing.click(window.up_button)
        self.assertEqual(window.folder, "docs")
        self.assertEqual(window.model.entry(window.view.currentIndex()).base, "img")     # where we came from
        window._crumb_clicked(0)
        self.assertEqual(window.folder, "")
        self.assertFalse(window.up_button.isEnabled())
        self.assertTrue(window.back_button.isEnabled())

    def test_sorting_keeps_folders_first(self):
        window = self.window(self.sample())
        window.view.sortByColumn(listmodel.SIZE, Qt.SortOrder.DescendingOrder)
        testing.process()
        self.assertEqual(self.names(window), ["docs", "zeta.bin", "run.sh", "README.md", "alpha.txt"])
        window.view.sortByColumn(listmodel.NAME, Qt.SortOrder.DescendingOrder)
        testing.process()
        self.assertEqual(self.names(window), ["docs", "zeta.bin", "run.sh", "README.md", "alpha.txt"])
        window.view.sortByColumn(listmodel.TYPE, Qt.SortOrder.AscendingOrder)
        testing.process()
        self.assertEqual(self.names(window)[0], "docs")

    def test_list_texts(self):
        window = self.window(self.sample())
        row = self.names(window).index("zeta.bin")
        texts = [window.model.data(window.model.index(row, column)) for column in range(7)]
        self.assertEqual(texts[0], "zeta.bin")
        self.assertEqual(texts[1], "5.0 KB")
        self.assertEqual(texts[4], "BIN file")
        self.assertRegex(texts[5], r"^[0-9A-F]{8}$")
        self.assertEqual(texts[6], "-rw-r--r--")
        self.assertFalse(window.view.isColumnHidden(listmodel.CRC))
        self.assertTrue(window.view.isColumnHidden(listmodel.LOCATION))
        self.assertFalse(window.model.data(window.model.index(0, 0), Qt.ItemDataRole.DecorationRole).isNull())

    def test_search_in_the_archive(self):
        window = self.window(self.sample())
        window.focus_search()
        self.assertTrue(window.search.hasFocus())
        testing.type_text(window.search, "file")
        self.assertTrue(testing.wait_until(lambda: window.model.searching, 3))
        self.assertEqual(self.names(window), ["file2.txt", "file10.txt"])
        self.assertFalse(window.view.isColumnHidden(listmodel.LOCATION))
        self.assertEqual(window.model.data(window.model.index(0, listmodel.LOCATION)), "docs")
        self.assertIn("2 results", window.status_left.text())
        self.assertFalse(window.add_files_action.isEnabled())
        self.shot(window, "search")
        window.search.setText("nothing-like-this")
        self.assertTrue(testing.wait_until(lambda: window.list_stack.currentWidget() is window.empty_folder, 3))
        window.search.clear_query()
        self.assertTrue(testing.wait_until(lambda: not window.model.searching, 3))
        self.assertEqual(len(self.names(window)), 5)

    def test_selection_drives_the_commands_and_the_status(self):
        window = self.window(self.sample())
        self.select(window, "alpha.txt", "zeta.bin")
        self.assertTrue(window.extract_action.isEnabled())
        self.assertTrue(window.delete_action.isEnabled())
        self.assertIn("2 selected (5.0 KB)", window.status_left.text())
        window.select_all()
        self.assertEqual(len(window.selected_entries()), 5)
        menu = window.build_context_menu()
        texts = [action.text() for action in menu.actions() if action.text()]
        self.assertEqual(texts, ["Open", "Extract…", "Delete", "Select all", "Properties"])
        self.assertFalse(menu.actions()[0].isEnabled())                 # Open needs exactly one item
        menu.close()

    def test_read_only_archives_say_why(self):
        import gzip
        path = self.work / "notes.txt.gz"
        with gzip.open(path, "wb") as handle:
            handle.write(b"notes\n" * 10)
        window = self.window(path)
        self.assertEqual(self.names(window), ["notes.txt"])
        self.select(window, "notes.txt")
        for action in (window.add_files_action, window.add_folder_action, window.delete_action):
            self.assertFalse(action.isEnabled())
            self.assertIn("can only be read", action.toolTip())
        self.assertTrue(window.view.isColumnHidden(listmodel.CRC))

    def test_not_an_archive(self):
        path = self.work / "letter.zip"
        path.write_text("Dear reader, this is no archive. " * 30)
        window = self.window(path)
        self.assertIs(window.stack.currentWidget(), window.error_page)
        self.assertIsNone(window.archive)
        self.assertNotIn("Traceback", window.error_page.findChildren(fluent.Label)[1].text())
        self.assertEqual(window.state.recent, [])
        self.shot(window, "error")

    def test_seven_zip_without_the_package(self):
        path = self.work / "a.7z"
        path.write_bytes(support.SEVEN_MAGIC + bytes(64))
        with mock.patch.object(sevenzip, "PROGRAMS", ()):
            window = self.window(path)
        texts = " ".join(label.text() for label in window.error_page.findChildren(fluent.Label))
        self.assertIn("needs the 7zip package", texts)
        self.assertIn("“7zip”", texts)

    def test_damaged_tar_shows_a_warning(self):
        path = raw_tar(self.work / "d.tar", [tar_member("one", b"1" * 600), tar_member("two", b"2" * 600)])
        raw = bytearray(path.read_bytes())
        raw[1024 + 512 + 100:1024 + 512 + 140] = b"\xff" * 40
        path.write_bytes(raw)
        window = self.window(path)
        self.assertEqual(self.names(window), ["one"])
        self.assertTrue(window.banner.isVisible())
        self.assertEqual(window.banner.severity(), "warning")
        self.assertFalse(window.add_files_action.isEnabled())

    def test_two_hundred_thousand_entries_stay_usable(self):
        import time
        members = [zip_member(f"file{index:06d}.dat", b"", method=0) for index in range(200_000)]
        path = raw_zip(self.work / "huge.zip", members)
        window = self.window(path, wait=False)
        # The window stays alive while the list is read on a worker thread.
        self.assertIs(window.stack.currentWidget(), window.loading_page)
        started = time.perf_counter()
        self.wait_idle(window, 60)
        self.assertEqual(window.model.rowCount(), 200_000)
        started = time.perf_counter()
        window.view.scrollToBottom()
        testing.process()
        window.search.setText("file19999")
        self.assertTrue(testing.wait_until(lambda: window.model.searching, 10))
        self.assertEqual(window.model.rowCount(), 10)
        self.assertLess(time.perf_counter() - started, 8.0)

    def test_drop_opens_an_archive(self):
        window = self.window()
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile(str(self.sample()))])
        from PySide6.QtGui import QDropEvent
        event = QDropEvent(QPoint(50, 50).toPointF(), Qt.DropAction.CopyAction, data, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)
        window.dropEvent(event)
        self.wait_idle(window)
        self.assertEqual(len(self.names(window)), 5)

    def test_close_archive_returns_to_the_start_page(self):
        window = self.window(self.sample())
        window.close_archive()
        self.assertIs(window.stack.currentWidget(), window.start_page)
        self.assertEqual(len(window.recent_items), 1)
        testing.click(window.recent_items[0])
        self.wait_idle(window)
        self.assertIsNotNone(window.archive)


class ExtractTest(WindowCase):
    def test_extract_all_through_the_dialog(self):
        window = self.window(self.sample())
        testing.click(window.commands.button(window.extract_all_action))
        dialog = self.wait_dialog(window, dialogs.ExtractDialog)
        self.assertEqual(dialog.destination.text(), str(self.work))
        self.assertTrue(dialog.make_folder.isChecked())                 # several items at the top: a folder for them
        self.assertEqual(dialog.target(), str(self.work / "Sample"))
        self.shot(window, "extract-dialog")
        testing.click(dialog.primary_button)
        self.wait_idle(window)
        found = tree(self.work / "Sample")
        self.assertEqual(found["docs/img/logo.png"].split(":")[0], "file")
        self.assertEqual((self.work / "Sample" / "alpha.txt").read_bytes(), b"alpha")
        self.assertEqual(stat.S_IMODE(os.stat(self.work / "Sample" / "run.sh").st_mode), 0o755)
        self.assertEqual(window.banner.severity(), "success")
        self.assertIn("8 files", window.banner.message())
        self.assertEqual(self.opened_paths, [str(self.work / "Sample")])
        self.assertFalse(window.progress_holder.isVisible())
        self.shot(window, "extracted")

    def test_extract_dialog_checks_its_input(self):
        window = self.window(self.sample())
        window.extract_all()
        dialog = self.wait_dialog(window, dialogs.ExtractDialog)
        dialog.destination.setText("")
        self.assertTrue(dialog.make_folder.isChecked())
        self.assertEqual(dialog.target(), "")           # never "Sample" below the working directory
        testing.click(dialog.primary_button)
        self.assertTrue(dialog.is_open())
        self.assertIn("Choose a folder", dialog.error.text())
        for relative in ("out", "./out", "../out", "."):
            dialog.destination.setText(relative)
            self.assertEqual(dialog.target(), "", relative)
            testing.click(dialog.primary_button)
            self.assertTrue(dialog.is_open(), relative)
            self.assertIn("whole path", dialog.error.text())
        self.assertFalse(window.busy())
        dialog.destination.setText("~/somewhere")
        self.assertEqual(dialog.target(), os.path.join(os.path.expanduser("~"), "somewhere", "Sample"))
        dialog.destination.setText(str(self.sample()))
        testing.click(dialog.primary_button)
        self.assertIn("not a folder", dialog.error.text())
        window.choose_folder = lambda *_args: str(self.tmp / "picked")
        testing.click(dialog.browse)
        self.assertEqual(dialog.destination.text(), str(self.tmp / "picked"))
        testing.click(dialog.close_button)
        testing.process()
        self.assertIsNone(window.dialog)
        self.assertFalse(window.busy())

    def test_no_extraction_without_an_absolute_destination(self):
        """Whatever reaches the window, a missing or relative destination starts nothing."""
        window = self.window(self.sample())
        for target in ("", "Sample", "./x", "relative/dir"):
            window.start_extract(target, None, "", extract.REPLACE, False)
            self.assertFalse(window.busy(), target)
            self.assertEqual(window.banner.severity(), "error")
            self.assertEqual(window.banner.title(), "Nothing was extracted")
        self.assertEqual(os.listdir(os.getcwd()), [])
        window.start_create("relative.zip", [str(self.sample())], create.Options())
        self.assertFalse(window.busy())
        self.assertEqual(window.banner.title(), "No archive was created")
        self.assertEqual(os.listdir(os.getcwd()), [])

    def test_extract_selected_from_inside_a_folder(self):
        window = self.window(self.sample())
        window.go_to("docs")
        self.select(window, "img", "file2.txt")
        testing.click(window.commands.button(window.extract_action))
        dialog = self.wait_dialog(window, dialogs.ExtractDialog)
        self.assertFalse(dialog.make_folder.isChecked())
        dialog.destination.setText(str(self.tmp / "out"))
        dialog.open_after.setChecked(False)
        testing.click(dialog.primary_button)
        self.wait_idle(window)
        self.assertEqual(sorted(tree(self.tmp / "out")), ["file2.txt", "img", "img/logo.png"])
        self.assertEqual(self.opened_paths, [])
        self.assertFalse(window.state.open_after)                       # remembered for next time

    def test_existing_files_are_asked_about(self):
        window = self.window(self.sample())
        target = self.work / "Sample"
        target.mkdir()
        (target / "alpha.txt").write_bytes(b"mine")
        (target / "README.md").write_bytes(b"mine too")
        (target / "zeta.bin").write_bytes(b"and mine")
        window.start_extract(str(target), None, "", extract.ASK, False)
        dialog = self.wait_dialog(window, dialogs.OverwriteDialog)
        self.assertEqual(dialog.text_label.text(), "zeta.bin")
        self.shot(window, "overwrite-dialog")
        testing.click(dialog.primary_button)                            # replace this one
        dialog = self.wait_dialog(window, dialogs.OverwriteDialog)
        self.assertEqual(dialog.text_label.text(), "alpha.txt")
        testing.click(dialog.keep_both)                                 # keep both
        dialog = self.wait_dialog(window, dialogs.OverwriteDialog)
        dialog.for_all.setChecked(True)
        testing.click(dialog.secondary_button)                          # skip this and all others
        self.wait_idle(window)
        self.assertEqual(len((target / "zeta.bin").read_bytes()), 5120)
        self.assertEqual((target / "alpha.txt").read_bytes(), b"mine")
        self.assertEqual((target / "alpha (2).txt").read_bytes(), b"alpha")
        self.assertEqual((target / "README.md").read_bytes(), b"mine too")
        self.assertEqual(window.banner.severity(), "warning")
        testing.click(window.banner.findChild(type(dialog.primary_button)))
        details = self.wait_dialog(window, dialogs.IssuesDialog)
        self.assertIn("README.md: a file with this name already exists", details.text.toPlainText())
        self.shot(window, "issues-dialog")
        testing.click(details.close_button)

    def test_cancel_in_the_overwrite_dialog_stops_the_extraction(self):
        window = self.window(self.sample())
        target = self.work / "Sample"
        target.mkdir()
        (target / "zeta.bin").write_bytes(b"mine")
        window.start_extract(str(target), None, "", extract.ASK, False)
        dialog = self.wait_dialog(window, dialogs.OverwriteDialog)
        testing.click(dialog.close_button)
        self.wait_idle(window)
        self.assertEqual((target / "zeta.bin").read_bytes(), b"mine")
        self.assertFalse((target / "alpha.txt").exists())

    def test_hostile_archive_summary(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        path = raw_tar(self.work / "evil.tar", [tar_member("../../outside/planted", b"x"), tar_link("door", "../../outside"),
                                               tar_member("door/planted", b"x"), tar_member("ok.txt", b"fine"),
                                               tar_member("pipe", None, kind=b"6")])
        window = self.window(path)
        self.assertIn("..", self.names(window))                         # the list shows what the archive holds
        window.start_extract(str(self.work / "dest"), None, "", extract.REPLACE, False)
        self.wait_idle(window)
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(sorted(tree(self.work / "dest")), ["door", "door/planted", "ok.txt"])
        self.assertEqual(window.banner.severity(), "warning")
        self.assertIn("3 skipped", window.banner.message())

    def test_size_guard_dialog_states_the_numbers(self):
        path = raw_zip(self.work / "bomb.zip", [zip_member("zeros.bin", bytes(2 * 1024 * 1024))])
        window = self.window(path)
        real = extract.extract
        small = extract.Limits(max_total=1 << 40, max_ratio=20.0, ratio_floor=256 * 1024)
        with mock.patch.object(extract, "extract", lambda *args, **kwargs: real(*args, limits=small, **kwargs)):
            window.start_extract(str(self.work / "out"), None, "", extract.REPLACE, False)
            dialog = self.wait_dialog(window)
            text = dialog.text_label.text()
            self.assertIn("2.0 MB", text)
            self.assertIn("times its size", text)
            self.assertEqual(dialog.primary_button.text(), "Extract anyway")
            self.assertIs(dialog.default_button(), dialog.close_button)
            self.shot(window, "size-dialog")
            testing.click(dialog.close_button)
            self.wait_idle(window)
            self.assertFalse((self.work / "out" / "zeros.bin").exists())
            window.start_extract(str(self.work / "out"), None, "", extract.REPLACE, False)
            dialog = self.wait_dialog(window)
            testing.click(dialog.primary_button)
            self.wait_idle(window)
        self.assertEqual(os.path.getsize(self.work / "out" / "zeros.bin"), 2 * 1024 * 1024)

    def test_password_is_asked_and_remembered(self):
        path = raw_zip(self.work / "locked.zip", [zip_member("secret.txt", b"classified\n" * 20, password="hunter2"),
                                                  zip_member("open.txt", b"public")])
        window = self.window(path)
        self.assertIn("password-protected", window.status_right.text())
        self.assertFalse(window.add_files_action.isEnabled())
        self.assertIn("password-protected", window.add_files_action.toolTip())
        self.select(window, "secret.txt")
        self.assertTrue(testing.wait_until(lambda: window.preview.unlock.isVisible(), 5))
        window.start_extract(str(self.work / "out"), None, "", extract.REPLACE, False)
        dialog = self.wait_dialog(window, dialogs.PasswordDialog)
        self.assertEqual(dialog.error.text(), "")
        testing.type_text(dialog.box, "nope")
        testing.key(dialog.box, Qt.Key.Key_Return)
        dialog = self.wait_dialog(window, dialogs.PasswordDialog)
        self.assertIn("not correct", dialog.error.text())
        self.shot(window, "password-dialog")
        dialog.box.setText("hunter2")
        testing.click(dialog.primary_button)
        self.wait_idle(window)
        self.assertEqual((self.work / "out" / "secret.txt").read_bytes(), b"classified\n" * 20)
        self.assertEqual(window.password, "hunter2")
        window._load_preview()
        self.assertTrue(testing.wait_until(lambda: window.preview.kind == "text", 5))
        self.assertIn("classified", window.preview.text.toPlainText())

    def test_cancel_running_extraction(self):
        path = raw_zip(self.work / "big.zip", [zip_member(f"f{index}.bin", os.urandom(1024 * 1024), method=0)
                                               for index in range(24)])
        window = self.window(path)
        from boswas_apps.archive_manager import reader
        import time
        real_read = reader.EntryReader.read

        def slow_read(entry_reader, size=1 << 20):       # a slow disk, so that Cancel arrives in the middle
            time.sleep(0.03)
            return real_read(entry_reader, size)

        with mock.patch.object(reader.EntryReader, "read", slow_read):
            window.start_extract(str(self.work / "out"), None, "", extract.REPLACE, False)
            self.assertTrue(window.progress_holder.isVisible())
            self.assertFalse(window.extract_all_action.isEnabled())
            self.assertTrue(testing.wait_until(lambda: window.progress.bar.value() > 0 or not window.busy(), 10))
            self.shot(window, "progress")
            testing.click(window.progress.cancel)
            self.wait_idle(window)
        self.assertLess(len(os.listdir(self.work / "out")), 24)
        self.assertFalse([name for name in os.listdir(self.work / "out") if name.startswith(".bw-")])
        self.assertEqual(window.banner.title(), "Cancelled")


class ChangeTest(WindowCase):
    def test_add_files_and_replace(self):
        window = self.window(self.sample())
        new = self.tmp / "new.txt"
        new.write_bytes(b"brand new")
        window.choose_files = lambda *_args: [str(new)]
        window.go_to("docs")
        testing.click(window.commands.button(window.add_files_action))
        self.wait_idle(window)
        self.assertTrue(testing.wait_until(lambda: window.archive is not None and "new.txt" in self.names(window), 10))
        self.assertEqual(window.folder, "docs")                         # back where the user was
        self.assertEqual(window.banner.severity(), "success")
        again = self.tmp / "other" / "guide.txt"
        again.parent.mkdir()
        again.write_bytes(b"a different guide")
        window.add_paths([str(again)])
        dialog = self.wait_dialog(window)
        self.assertIn("“docs/guide.txt”", dialog.text_label.text())
        self.assertIs(dialog.default_button(), dialog.close_button)
        testing.click(dialog.primary_button)
        self.assertTrue(testing.wait_until(lambda: not window.busy() and window.archive is not None, 10))
        with zipfile.ZipFile(self.work / "Sample.zip") as archive:
            self.assertEqual(archive.read("docs/guide.txt"), b"a different guide")
            self.assertEqual(archive.read("docs/new.txt"), b"brand new")
            self.assertEqual(archive.comment, b"a test archive")

    def test_declined_replace_changes_nothing(self):
        path = self.sample()
        before = path.read_bytes()
        window = self.window(path)
        again = self.tmp / "alpha.txt"
        again.write_bytes(b"other alpha")
        window.add_paths([str(again)])
        dialog = self.wait_dialog(window)
        testing.click(dialog.close_button)
        testing.process(50)
        self.assertFalse(window.busy())
        self.assertEqual(path.read_bytes(), before)

    def test_delete_asks_first(self):
        path = self.sample()
        window = self.window(path)
        self.select(window, "docs", "zeta.bin")
        testing.key(window.view, Qt.Key.Key_Delete)
        dialog = self.wait_dialog(window)
        self.assertIn("2 items and the 4 items inside", dialog.text_label.text())
        self.assertIs(dialog.default_button(), dialog.close_button)
        self.shot(window, "delete-dialog")
        testing.click(dialog.close_button)
        testing.process(30)
        self.assertEqual(len(self.names(window)), 5)
        testing.click(window.commands.button(window.delete_action))
        dialog = self.wait_dialog(window)
        testing.click(dialog.primary_button)
        self.assertTrue(testing.wait_until(lambda: not window.busy() and window.archive is not None
                                           and len(self.names(window)) == 3, 10))
        self.assertEqual(self.names(window), ["alpha.txt", "README.md", "run.sh"])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(sorted(archive.namelist()), ["README.md", "alpha.txt", "run.sh"])

    def test_new_archive_dialog(self):
        source = self.tmp / "project"
        (source / "sub").mkdir(parents=True)
        (source / "a.txt").write_bytes(b"a" * 100)
        (source / "sub" / ".hidden").write_bytes(b"h")
        window = self.window()
        window.choose_folder = lambda *_args: str(source)
        testing.click(window.new_button)
        dialog = self.wait_dialog(window, dialogs.NewArchiveDialog)
        testing.click(dialog.primary_button)
        self.assertIn("Add at least one", dialog.error.text())
        testing.click(dialog.add_folder)
        self.assertEqual(dialog.source_paths(), [str(source)])
        dialog.name.setText("My project")
        for relative in ("", ".", "work"):
            dialog.directory.setText(relative)
            self.assertEqual(dialog.output(), "", relative)
            testing.click(dialog.primary_button)
            self.assertTrue(dialog.is_open(), relative)
            self.assertIn("whole path", dialog.error.text())
        dialog.directory.setText(str(self.work))
        dialog.format.setCurrentIndex(dialog.format.findData("tar.xz"))
        dialog.hidden.setChecked(True)
        self.assertEqual(dialog.output(), str(self.work / "My project.tar.xz"))
        self.shot(window, "new-dialog")
        dialog.format.setCurrentIndex(dialog.format.findData("tar"))
        self.assertFalse(dialog.level.isEnabled())
        dialog.format.setCurrentIndex(dialog.format.findData("tar.xz"))
        testing.click(dialog.primary_button)
        self.assertTrue(testing.wait_until(lambda: not window.busy() and window.archive is not None, 15))
        self.assertEqual(window.path, str(self.work / "My project.tar.xz"))
        self.assertEqual(window.archive.format.key, "tar.xz")
        self.assertEqual(self.names(window), ["project"])
        self.assertEqual(sorted(entry.path for entry in window.archive.entries),
                         ["project", "project/a.txt", "project/sub"])
        self.assertEqual(window.banner.severity(), "success")

    def test_new_archive_asks_before_replacing_a_file(self):
        source = self.tmp / "a.txt"
        source.write_bytes(b"a")
        existing = self.work / "a.zip"
        existing.write_bytes(b"something else")
        window = self.window()
        window.new_archive([str(source)])
        dialog = self.wait_dialog(window, dialogs.NewArchiveDialog)
        self.assertEqual(dialog.name.text(), "a.txt")
        dialog.name.setText("a")
        dialog.directory.setText(str(self.work))
        testing.click(dialog.primary_button)
        confirm = self.wait_dialog(window)
        self.assertIn("already exists", confirm.text_label.text())
        testing.click(confirm.close_button)
        testing.process(30)
        self.assertEqual(existing.read_bytes(), b"something else")

    def test_drop_on_an_open_archive_adds(self):
        window = self.window(self.sample())
        new = self.tmp / "dropped.txt"
        new.write_bytes(b"dropped")
        window.handle_drop([str(new)])
        self.assertTrue(testing.wait_until(lambda: not window.busy() and window.archive is not None
                                           and "dropped.txt" in self.names(window), 10))


class InspectTest(WindowCase):
    def test_preview_of_text_picture_and_folder(self):
        window = self.window(self.sample())
        self.select(window, "README.md")
        self.assertTrue(testing.wait_until(lambda: window.preview.kind == "text", 5))
        self.assertEqual(window.preview.text.toPlainText(), "# Sample\n")
        self.select(window, "docs")
        self.assertTrue(testing.wait_until(lambda: "Folder with 4 items" in window.preview.message.text(), 5))
        self.select(window, "zeta.bin")
        self.assertTrue(testing.wait_until(lambda: "No preview" in window.preview.message.text(), 5))
        window.go_to("docs/img")
        self.select(window, "logo.png")
        self.assertTrue(testing.wait_until(lambda: window.preview.kind == "image", 5))
        self.assertEqual(window.preview.picture.image().width(), 64)
        self.shot(window, "preview-image")
        self.select(window)
        self.assertTrue(testing.wait_until(lambda: "Select a file" in window.preview.message.text(), 5))

    def test_preview_pane_can_be_hidden(self):
        window = self.window(self.sample())
        self.assertTrue(window.preview.isVisible())
        window.preview_action.trigger()
        self.assertFalse(window.preview.isVisible())
        self.assertFalse(window.state.preview)
        window.preview_action.trigger()
        self.assertTrue(window.preview.isVisible())

    def test_open_entry_uses_a_private_copy_that_is_cleaned_up(self):
        window = self.window(self.sample())
        self.select(window, "alpha.txt")
        testing.key(window.view, Qt.Key.Key_Return)
        self.wait_idle(window)
        (opened,) = self.opened_paths
        self.assertTrue(opened.startswith(str(self.tmp / "xdg_cache_home")), opened)
        self.assertEqual(Path(opened).read_bytes(), b"alpha")
        self.assertEqual(stat.S_IMODE(os.stat(opened).st_mode), 0o400)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(os.path.dirname(opened))).st_mode), 0o700)
        window.close()
        self.assertFalse(os.path.exists(opened))
        self.assertFalse(os.path.exists(os.path.dirname(os.path.dirname(opened))))

    def test_programs_are_not_opened(self):
        elf = raw_zip(self.work / "p.zip", [zip_member("tool", b"\x7fELF" + bytes(100)),
                                            zip_member("app.desktop", b"[Desktop Entry]\nExec=rm -rf ~\n"),
                                            zip_member("holiday.jpg", b"#!/bin/sh\nrm -rf ~\n")])
        window = self.window(elf)
        for name in ("app.desktop", "tool", "holiday.jpg"):
            self.select(window, name)
            window.open_selected()
            self.wait_idle(window)
            self.assertEqual(self.opened_paths, [], name)
            self.assertEqual(window.banner.title(), "Not opened", name)
            window.banner.hide()

    def test_test_command(self):
        window = self.window(self.sample())
        testing.click(window.commands.button(window.test_action))
        self.wait_idle(window)
        self.assertEqual(window.banner.severity(), "success")
        self.assertIn("8 files", window.banner.message())
        bad = raw_zip(self.work / "bad.zip", [zip_member("good", b"g" * 100), zip_member("bad", b"b" * 100, crc=7)])
        window.open_archive(str(bad))
        self.wait_idle(window)
        window.test_archive()
        self.wait_idle(window)
        self.assertEqual(window.banner.severity(), "error")
        self.assertIn("1 problem", window.banner.message())

    def test_properties(self):
        window = self.window(self.sample())
        window.search.setFocus()
        testing.click(window.commands.button(window.properties_action))
        dialog = self.wait_dialog(window, dialogs.PropertiesDialog)
        self.assertEqual(dialog.values["Kind"], "ZIP archive")
        self.assertEqual(dialog.values["Files"], "8")
        self.assertEqual(dialog.values["Folders"], "2")
        self.assertEqual(dialog.values["Comment"], "a test archive")
        self.assertEqual(dialog.values["Password"], "None")
        self.assertIn("%", dialog.values["Packed to"])
        self.shot(window, "properties")
        testing.click(dialog.close_button)
        testing.process()
        self.select(window, "run.sh")
        window.view.setFocus()
        window.show_properties()
        dialog = self.wait_dialog(window, dialogs.PropertiesDialog)
        self.assertEqual(dialog.values["Permissions"], "-rwxr-xr-x")
        self.assertEqual(dialog.values["Kind"], "Shell script")
        testing.click(dialog.close_button)

    def test_light_scheme_renders(self):
        testing.set_scheme("light")
        window = self.window(self.sample())
        self.select(window, "README.md")
        self.assertTrue(testing.wait_until(lambda: window.preview.kind == "text", 5))
        image = window.grab().toImage()
        self.assertGreater(image.pixelColor(600, 400).lightness(), 180)

    def test_keyboard_reaches_every_command(self):
        window = self.window(self.sample())
        window.go_to("docs")
        window.commands.button(window.new_action).setFocus()
        seen = set()
        for _step in range(40):
            widget = testing.press_tab()
            if widget is not None:
                seen.add(widget)
        for required in (window.commands.button(window.extract_all_action), window.back_button, window.search, window.view):
            self.assertIn(required, seen)
        for button in (window.back_button, window.forward_button, window.up_button):
            self.assertTrue(button.toolTip())


class ProgressWindowTest(WindowCase):
    def progress_window(self, tasks, **options) -> "ProgressWindow":
        window = ProgressWindow(tasks, **options)
        self.notes = []
        window.notify = lambda *args, **kwargs: self.notes.append(args) or 0
        window.open_path = lambda target: self.opened_paths.append(str(target)) or True
        self.show(window, 600, 380)
        return window

    def test_extract_closes_itself_when_all_went_well(self):
        path = self.sample()
        window = self.progress_window([Task("extract", str(path), str(self.work / "Sample"))])
        self.assertTrue(testing.wait_until(lambda: window.finished_all, 15))
        self.assertEqual(window.exit_code, EXIT_OK)
        self.assertFalse(window.isVisible())
        self.assertEqual((self.work / "Sample" / "alpha.txt").read_bytes(), b"alpha")
        self.assertIn("Extracted 8 files", self.notes[0][0])

    def test_problems_keep_the_window_open_with_a_summary(self):
        path = raw_tar(self.work / "evil.tar", [tar_member("../evil", b"x"), tar_member("ok.txt", b"fine")])
        window = self.progress_window([Task("extract", str(path), str(self.work / "evil")),
                                       Task("extract", str(self.work / "missing.zip"), str(self.work / "m"))])
        self.assertTrue(testing.wait_until(lambda: window.finished_all, 15))
        self.assertEqual(window.exit_code, EXIT_PROBLEMS)
        self.assertTrue(window.isVisible())
        text = window.done_text.toPlainText()
        self.assertIn("Skipped: ../evil", text)
        self.assertIn("missing.zip: The file cannot be read", text)
        self.assertIn("not everything", window.done_title.text())
        self.shot(window, "progress-window-summary")
        testing.click(window.open_button)
        self.assertEqual(self.opened_paths, [str(self.work / "evil")])
        testing.click(window.close_button)
        self.assertFalse(window.isVisible())

    def test_password_and_existing_files(self):
        path = raw_zip(self.work / "locked.zip", [zip_member("secret.txt", b"classified", password="pw")])
        target = self.work / "locked"
        target.mkdir()
        (target / "secret.txt").write_bytes(b"old")
        window = self.progress_window([Task("extract", str(path), str(target))])
        dialog = self.wait_dialog(window, dialogs.PasswordDialog)
        self.shot(window, "progress-window-password")
        dialog.box.setText("pw")
        testing.click(dialog.primary_button)
        dialog = self.wait_dialog(window, dialogs.OverwriteDialog)
        testing.click(dialog.primary_button)
        self.assertTrue(testing.wait_until(lambda: window.finished_all, 15))
        self.assertEqual((target / "secret.txt").read_bytes(), b"classified")
        self.assertEqual(window.exit_code, EXIT_OK)

    def test_giving_no_password_cancels(self):
        path = raw_zip(self.work / "locked.zip", [zip_member("secret.txt", b"classified", password="pw")])
        window = self.progress_window([Task("extract", str(path), str(self.work / "locked"))])
        dialog = self.wait_dialog(window, dialogs.PasswordDialog)
        testing.click(dialog.close_button)
        self.assertTrue(testing.wait_until(lambda: window.finished_all, 15))
        self.assertEqual(window.exit_code, EXIT_CANCELLED)
        self.assertEqual(window.done_title.text(), "Cancelled")

    def test_task_without_a_full_destination_is_not_run(self):
        path = self.sample()
        window = self.progress_window([Task("extract", str(path), None), Task("extract", str(path), "relative"),
                                       Task("create", "relative.zip", sources=[str(path)])])
        self.assertTrue(testing.wait_until(lambda: window.finished_all, 15))
        self.assertEqual(window.exit_code, EXIT_PROBLEMS)
        self.assertEqual(window.done_text.toPlainText().count("no destination folder was given"), 2)
        self.assertEqual(os.listdir(os.getcwd()), [])

    def test_create(self):
        source = self.tmp / "project"
        source.mkdir()
        (source / "a.txt").write_bytes(b"a" * 100)
        output = self.work / "project.tar.gz"
        window = self.progress_window([Task("create", str(output), sources=[str(source)])])
        self.assertTrue(testing.wait_until(lambda: window.finished_all, 15))
        self.assertEqual(window.exit_code, EXIT_OK)
        import tarfile
        with tarfile.open(output) as archive:
            self.assertEqual(sorted(archive.getnames()), ["project", "project/a.txt"])

    def test_create_asks_before_replacing(self):
        source = self.tmp / "a.txt"
        source.write_bytes(b"a")
        output = self.work / "a.zip"
        output.write_bytes(b"precious")
        window = self.progress_window([Task("create", str(output), sources=[str(source)])])
        dialog = self.wait_dialog(window)
        self.assertIn("already exists", dialog.text_label.text())
        testing.click(dialog.close_button)
        self.assertTrue(testing.wait_until(lambda: window.finished_all, 15))
        self.assertEqual(output.read_bytes(), b"precious")
        self.assertEqual(window.exit_code, EXIT_PROBLEMS)

    def test_cancel_button(self):
        path = raw_zip(self.work / "big.zip", [zip_member(f"f{index}.bin", os.urandom(1024 * 1024), method=0)
                                               for index in range(24)])
        from boswas_apps.archive_manager import reader
        import time
        real_read = reader.EntryReader.read

        def slow_read(entry_reader, size=1 << 20):
            time.sleep(0.03)
            return real_read(entry_reader, size)

        with mock.patch.object(reader.EntryReader, "read", slow_read):
            window = self.progress_window([Task("extract", str(path), str(self.work / "big"))])
            self.assertTrue(testing.wait_until(lambda: window.panel.bar.maximum() > 0 or window.finished_all, 10))
            self.shot(window, "progress-window")
            testing.click(window.cancel_button)
            self.assertTrue(testing.wait_until(lambda: window.finished_all, 15))
        self.assertEqual(window.exit_code, EXIT_CANCELLED)
        self.assertTrue(window.isVisible())


@unittest.skipUnless(support.PYSIDE, "PySide6 is not installed")
class SourceRulesTest(unittest.TestCase):
    """The rules of the toolkit that a test can check: no colours and no style sheets of our own."""

    def test_no_colour_values_or_style_sheets(self):
        root = Path(support.HERE.parents[1]) / "boswas_apps" / "archive_manager"
        colour = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|QColor\(\s*\d|rgba?\(")
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(colour.search(text), f"{path.name} holds a colour value")
            self.assertNotIn("setStyleSheet", text, path.name)
            self.assertNotIn("shell=True", text, path.name)
            self.assertNotIn("os.system", text, path.name)

    def test_engine_modules_do_not_import_qt(self):
        root = Path(support.HERE.parents[1]) / "boswas_apps" / "archive_manager"
        for name in ("names", "model", "formats", "sevenzip", "reader", "extract", "create", "errors", "previewing",
                     "state"):
            text = (root / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotIn("PySide6", text, name)
            self.assertNotIn("boswas_fluent", text, name)


if __name__ == "__main__":
    unittest.main()
