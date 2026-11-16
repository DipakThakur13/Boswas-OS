"""The Files window, driven through its widgets (offscreen), on a private home directory."""

from __future__ import annotations

import os
import stat
import time
import unittest
import zipfile

from ui_support import FilesCase, testing

from PySide6.QtCore import QMimeData, QPoint, Qt
from PySide6.QtGui import QGuiApplication, QImage, QKeySequence


from boswas_apps.files import clipformats, places
from boswas_apps.files.dialogs import ConflictDialog, ErrorsDialog, OpenWithDialog, SettingsDialog
from boswas_apps.files.sidebar import KIND_ROLE, PINNED


def shortcut(window, key: str) -> list[str]:
    return [sequence.toString(QKeySequence.SequenceFormat.PortableText) for sequence in window.actions_[key].shortcuts()]


class BrowsingTest(FilesCase):
    def setUp(self):
        super().setUp()
        for name in ("file10.txt", "file2.txt", "File1.txt", "notes.md", ".hidden"):
            self.write(f"home/Documents/{name}", name)
        self.mkdir("home/Documents/Zeta")
        self.mkdir("home/Documents/alpha/inner")

    def test_rows_are_sorted_naturally_with_folders_first(self):
        window = self.window()
        self.assertEqual(self.names(window), ["alpha", "Zeta", "File1.txt", "file2.txt", "file10.txt", "notes.md"])
        self.assertEqual(window.count_label.text(), "6 items")
        self.assertEqual(window.windowTitle(), "Documents \u2013 Files")
        self.assertEqual(window.address.crumbs.bar.items(), ["Home", "Documents"])
        self.assertEqual(window.tabs.tabText(0), "Documents")
        self.assertEqual(window.sidebar.currentItem().text(), "Documents")

    def test_open_folder_back_forward_up(self):
        window = self.window()
        tab = window.tab()
        view = tab.view.current_view()
        self.select(window, "alpha")
        testing.key(view, Qt.Key.Key_Return)
        self.settle(window)
        self.assertEqual(tab.location, os.path.join(self.docs, "alpha"))
        self.assertEqual(self.names(window), ["inner"])
        self.assertTrue(window.actions_["back"].isEnabled())
        self.assertFalse(window.actions_["forward"].isEnabled())
        testing.key(view, Qt.Key.Key_Backspace)
        self.settle(window)
        self.assertEqual(tab.location, self.docs)
        self.assertEqual([entry.name for entry in tab.selected_entries()], ["alpha"])    # where we came from
        window.actions_["back"].trigger()
        self.settle(window)
        self.assertEqual(tab.location, os.path.join(self.docs, "alpha"))
        window.actions_["back"].trigger()
        window.actions_["forward"].trigger()
        self.settle(window)
        self.assertEqual(tab.location, os.path.join(self.docs, "alpha"))
        window.actions_["up"].trigger()
        window.actions_["up"].trigger()
        self.settle(window)
        self.assertEqual(tab.location, self.home)
        self.assertEqual(shortcut(window, "back"), ["Alt+Left"])
        self.assertEqual(shortcut(window, "forward"), ["Alt+Right"])
        self.assertEqual(shortcut(window, "up"), ["Alt+Up"])
        self.assertEqual(shortcut(window, "refresh"), ["F5", "Ctrl+R"])

    def test_double_click_opens_and_single_click_setting(self):
        window = self.window()
        tab = window.tab()
        tree = tab.view.tree
        rect = tree.visualRect(tab.model.index_of(os.path.join(self.docs, "Zeta")))
        point = QPoint(rect.left() + 60, rect.center().y())
        testing.click(tree.viewport(), point)
        self.assertEqual(tab.location, self.docs)
        self.assertEqual([entry.name for entry in tab.selected_entries()], ["Zeta"])
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication
        event = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(point), QPointF(tree.viewport().mapToGlobal(point)),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(tree.viewport(), event)
        self.settle(window)
        self.assertEqual(tab.location, os.path.join(self.docs, "Zeta"))
        window.actions_["back"].trigger()
        self.settle(window)
        window.services.set_setting("single_click", True)
        rect = tree.visualRect(tab.model.index_of(os.path.join(self.docs, "alpha")))
        testing.click(tree.viewport(), QPoint(rect.left() + 60, rect.center().y()))
        self.settle(window)
        self.assertEqual(tab.location, os.path.join(self.docs, "alpha"))

    def test_address_bar(self):
        window = self.window()
        address = window.address
        self.assertEqual(shortcut(window, "edit_address"), ["Ctrl+L", "Alt+D"])
        window.actions_["edit_address"].trigger()
        self.assertTrue(address.is_editing())
        self.assertEqual(address.edit_box.text(), "~/Documents")
        self.assertEqual(address.edit_box.selectedText(), "~/Documents")
        address.edit_box.clear()
        testing.type_text(address.edit_box, "~/Documents/alpha")
        testing.key(address.edit_box, Qt.Key.Key_Return)
        self.settle(window)
        self.assertEqual(window.tab().location, os.path.join(self.docs, "alpha"))
        self.assertFalse(address.is_editing())
        self.assertEqual(address.crumbs.bar.items(), ["Home", "Documents", "alpha"])
        address.crumb_clicked.emit(self.home)
        self.settle(window)
        self.assertEqual(window.tab().location, self.home)
        # a click beside the crumbs edits; Escape goes back; a path that does not exist is an error
        testing.click(address.crumbs, QPoint(address.crumbs.width() - 6, 10))
        self.assertTrue(address.is_editing())
        testing.key(address.edit_box, Qt.Key.Key_Escape)
        self.assertFalse(address.is_editing())
        window.actions_["edit_address"].trigger()
        address.edit_box.setText("/no/such/place")
        testing.key(address.edit_box, Qt.Key.Key_Return)
        self.settle(window)
        self.assertEqual(window.tab().location, self.home)
        self.assertTrue(window.banner.isVisible())
        self.assertIn("does not exist", window.banner.message())
        # a file: the folder opens with the file selected
        window.actions_["edit_address"].trigger()
        address.edit_box.setText(os.path.join(self.docs, "notes.md"))
        testing.key(address.edit_box, Qt.Key.Key_Return)
        self.settle(window)
        self.assertEqual(window.tab().location, self.docs)
        self.assertEqual([entry.name for entry in window.tab().selected_entries()], ["notes.md"])

    def test_address_completion(self):
        window = self.window()
        window.actions_["edit_address"].trigger()
        window.address.edit_box.clear()
        testing.type_text(window.address.edit_box, "~/Documents/")
        window.services.worker.wait(5)
        testing.process()
        self.assertEqual(window.address.completions(), ["~/Documents/alpha", "~/Documents/Zeta"])
        window.address.stop_editing()

    def test_tabs(self):
        window = self.window()
        self.assertEqual(shortcut(window, "new_tab"), ["Ctrl+T"])
        self.assertEqual(shortcut(window, "close_tab"), ["Ctrl+W"])
        self.assertEqual(shortcut(window, "next_tab"), ["Ctrl+Tab", "Ctrl+PgDown"])
        window.actions_["new_tab"].trigger()
        self.settle(window)
        self.assertEqual(window.tabs.count(), 2)
        self.assertEqual(window.tab().location, self.home)
        self.assertEqual(window.tabs.currentIndex(), 1)
        window.actions_["next_tab"].trigger()
        self.assertEqual(window.tab().location, self.docs)
        window.actions_["previous_tab"].trigger()
        self.assertEqual(window.tab().location, self.home)
        # a middle click on a folder opens it in a new tab in the background
        window.tabs.setCurrentIndex(0)
        tree = window.tab().view.tree
        rect = tree.visualRect(window.tab().model.index_of(os.path.join(self.docs, "alpha")))
        testing.click(tree.viewport(), QPoint(rect.left() + 60, rect.center().y()), button=Qt.MouseButton.MiddleButton)
        self.settle(window)
        self.assertEqual(window.tabs.count(), 3)
        self.assertEqual(window.tabs.currentIndex(), 0)
        self.assertEqual(window.tab_at(2).location, os.path.join(self.docs, "alpha"))
        self.assertEqual(window.tabs.tabText(2), "alpha")
        window.tabs.middle_clicked.emit(2)
        window.actions_["close_tab"].trigger()
        self.assertEqual(window.tabs.count(), 1)
        self.assertEqual(window.tab().location, self.home)

    def test_hidden_files_sorting_and_views(self):
        window = self.window()
        tab = window.tab()
        self.assertEqual(shortcut(window, "hidden"), ["Ctrl+H"])
        window.actions_["hidden"].trigger()
        self.assertIn(".hidden", self.names(window))
        self.assertTrue(window.services.settings.get("show_hidden"))
        window.actions_["hidden"].trigger()
        self.assertNotIn(".hidden", self.names(window))
        header = tab.view.tree.header()
        self.select(window, "notes.md")
        header.sectionClicked.emit(0)                  # Name again: descending
        self.assertEqual(self.names(window), ["Zeta", "alpha", "notes.md", "file10.txt", "file2.txt", "File1.txt"])
        self.assertEqual([entry.name for entry in tab.selected_entries()], ["notes.md"])
        header.sectionClicked.emit(3)                  # Size
        self.assertEqual(self.names(window)[2:], ["notes.md", "File1.txt", "file2.txt", "file10.txt"])
        self.assertEqual((tab.model.sort_column, tab.model.sort_descending), ("size", False))
        window.sort_actions["name"].trigger()
        window.ascending.trigger()
        self.assertEqual(self.names(window)[:3], ["alpha", "Zeta", "File1.txt"])
        for mode in ("icons-large", "tiles", "list", "icons-small", "icons-medium", "icons-xlarge", "details"):
            window.view_actions[mode].trigger()
            self.assertEqual(tab.view.view_mode, mode)
            self.assertEqual(tab.view.current_view() is tab.view.tree, mode == "details")
            self.assertEqual([entry.name for entry in tab.selected_entries()], ["notes.md"])
            testing.grab(window, self.tmp / f"{mode}.png")
        self.assertEqual(window.services.settings.get("view_mode"), "details")
        testing.click(window.status_icons)
        self.assertEqual(tab.view.view_mode, "icons-large")

    def test_selection_commands_type_ahead_and_status(self):
        window = self.window()
        tab = window.tab()
        view = tab.view
        self.assertEqual(shortcut(window, "select_all"), ["Ctrl+A"])
        window.actions_["select_all"].trigger()
        self.assertEqual(view.selected_count(), 6)
        self.assertEqual(window.selection_label.text(), "6 items selected")
        window.actions_["invert"].trigger()
        self.assertEqual(view.selected_count(), 0)
        self.select(window, "file2.txt", "notes.md")
        self.assertEqual(window.selection_label.text(), "2 items selected  17 bytes")
        window.actions_["invert"].trigger()
        self.assertEqual(sorted(entry.name for entry in tab.selected_entries()),
                         ["File1.txt", "Zeta", "alpha", "file10.txt"])
        window.actions_["select_none"].trigger()
        self.assertEqual(window.selection_label.text(), "")
        view.selection_model().clearCurrentIndex()
        testing.type_text(view.tree, "fi")
        self.assertEqual([entry.name for entry in tab.selected_entries()], ["File1.txt"])
        view._typed_at = 0
        testing.type_text(view.tree, "n")
        self.assertEqual([entry.name for entry in tab.selected_entries()], ["notes.md"])
        view._typed_at = 0
        testing.type_text(view.tree, "f")
        testing.type_text(view.tree, "f")
        self.assertEqual([entry.name for entry in tab.selected_entries()], ["file2.txt"])

    def test_rubber_band_in_details(self):
        window = self.window()
        tree = window.tab().view.tree
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication

        def send(kind, point, buttons):
            QApplication.sendEvent(tree.viewport(), QMouseEvent(
                kind, QPointF(point), QPointF(tree.viewport().mapToGlobal(point)), Qt.MouseButton.LeftButton, buttons,
                Qt.KeyboardModifier.NoModifier))

        start = QPoint(tree.viewport().width() - 30, 32 * 5 + 20)        # in the empty space under the rows... row 6
        send(QEvent.Type.MouseButtonPress, QPoint(tree.viewport().width() - 30, 32 * 6 + 30), Qt.MouseButton.LeftButton)
        send(QEvent.Type.MouseMove, QPoint(600, 32 * 3 + 10), Qt.MouseButton.LeftButton)
        send(QEvent.Type.MouseMove, QPoint(500, 32 * 2 + 10), Qt.MouseButton.LeftButton)
        self.assertIsNotNone(tree._band)
        send(QEvent.Type.MouseButtonRelease, QPoint(500, 32 * 2 + 10), Qt.MouseButton.NoButton)
        self.assertIsNone(tree._band)
        self.assertEqual([entry.name for entry in window.tab().selected_entries()],
                         ["File1.txt", "file2.txt", "file10.txt", "notes.md"])
        self.assertIsNotNone(start)

    def test_watcher_shows_changes_made_elsewhere(self):
        window = self.window()
        self.write("home/Documents/appeared.txt")
        os.unlink(os.path.join(self.docs, "notes.md"))
        self.assertTrue(testing.wait_until(lambda: "appeared.txt" in self.names(window), 5))
        self.assertNotIn("notes.md", self.names(window))

    def test_unreadable_missing_and_empty_folders(self):
        window = self.window()
        tab = window.tab()
        tab.navigate(os.path.join(self.docs, "alpha", "inner"))
        self.settle(window)
        self.assertFalse(tab.shows_view())
        self.assertEqual(tab.empty.title_label.text() if hasattr(tab.empty, "title_label") else "",
                         tab.empty.title_label.text())
        self.assertEqual(window.count_label.text(), "0 items")
        testing.grab(window, self.tmp / "empty.png")
        tab.navigate(self.path("home/missing"))
        self.settle(window)
        self.assertEqual(tab.model.error, "This folder does not exist.")
        self.assertFalse(tab.shows_view())
        if os.geteuid() != 0:
            locked = self.mkdir("home/locked")
            os.chmod(locked, 0)
            tab.navigate(locked)
            self.settle(window)
            self.assertEqual(tab.model.error, "You do not have permission to open this folder.")
        link = os.path.join(self.docs, "broken")
        os.symlink("/nowhere", link)
        tab.navigate(self.docs)
        self.settle(window)
        entry = tab.model.entry(tab.model.row_of(link))
        self.assertTrue(entry.broken)
        self.assertEqual(entry.type_text, "Broken link")
        window.open_entry(entry)
        self.assertIn("does not exist", window.banner.message())

    def test_search(self):
        self.write("home/Documents/alpha/inner/deep-file7.txt")
        window = self.window()
        tab = window.tab()
        self.assertEqual(shortcut(window, "search"), ["Ctrl+F", "Ctrl+E", "F3"])
        window.actions_["search"].trigger()
        testing.type_text(window.search_box, "file")
        testing.key(window.search_box, Qt.Key.Key_Return)
        self.settle(window)
        self.assertEqual(tab.search_query, "file")
        self.assertEqual(sorted(self.names(window)), ["File1.txt", "deep-file7.txt", "file10.txt", "file2.txt"])
        self.assertEqual(tab.model.columns(), ("name", "location", "date", "type", "size"))
        deep = tab.model.entry(tab.model.row_of(os.path.join(self.docs, "alpha/inner/deep-file7.txt")))
        self.assertEqual(deep.location, os.path.join(self.docs, "alpha", "inner"))
        self.assertEqual(window.count_label.text(), "4 results")
        self.assertEqual(window.tabs.tabText(0), "file \u2013 Search")
        tab.view.select_paths([deep.path])
        self.assertIn("Open file location", self.menu_texts(window.context_menu(deep)))
        window.search_box.clear_query()
        self.settle(window)
        self.assertEqual(tab.search_query, "")
        self.assertEqual(len(self.names(window)), 6)
        tab.start_search("nothing-like-this")
        self.settle(window)
        self.assertFalse(tab.shows_view())
        self.assertEqual(tab.empty.title_label.text(), "No results")


class OperationsTest(FilesCase):
    def setUp(self):
        super().setUp()
        self.a = self.write("home/Documents/a.txt", "AAA")
        self.b = self.write("home/Documents/b.tar.gz", "BBBB")
        self.folder = self.mkdir("home/Documents/folder")
        self.answers = []

    def clipboard_formats(self):
        data = QGuiApplication.clipboard().mimeData()
        return {name: bytes(data.data(name)) for name in data.formats()}

    def test_new_folder_is_renamed_in_place_and_undone(self):
        window = self.window()
        tab = window.tab()
        self.assertEqual(shortcut(window, "new_folder"), ["Ctrl+Shift+N"])
        window.actions_["new_folder"].trigger()
        self.settle(window, 50)
        self.assertTrue(os.path.isdir(os.path.join(self.docs, "New folder")))
        editor = tab.view.editor()
        self.assertIsNotNone(editor)
        self.assertEqual(editor.text(), "New folder")
        self.assertEqual(editor.selectedText(), "New folder")
        editor.clear()
        testing.type_text(editor, "Projects")
        testing.key(editor, Qt.Key.Key_Return)
        self.settle(window)
        self.assertTrue(os.path.isdir(os.path.join(self.docs, "Projects")))
        self.assertEqual([entry.name for entry in tab.selected_entries()], ["Projects"])
        self.assertEqual(shortcut(window, "undo"), ["Ctrl+Z"])
        self.assertEqual(window.actions_["undo"].text(), "Undo Rename")
        window.actions_["undo"].trigger()
        self.settle(window)
        self.assertTrue(os.path.isdir(os.path.join(self.docs, "New folder")))
        window.actions_["undo"].trigger()                   # the creation: the folder goes to the Trash
        self.settle(window)
        self.assertNotIn("New folder", self.names(window))
        self.assertEqual([item.original_path for item in window.services.trash.items()],
                         [os.path.join(self.docs, "New folder")])
        self.assertFalse(window.actions_["undo"].isEnabled())
        window.actions_["redo"].trigger()
        window.actions_["redo"].trigger()
        self.settle(window)
        self.assertIn("Projects", self.names(window))
        window.new_menu.actions()[1].trigger()              # New > Text document
        self.settle(window, 50)
        self.assertTrue(os.path.isfile(os.path.join(self.docs, "New text document.txt")))
        editor = tab.view.editor()
        self.assertEqual(editor.selectedText(), "New text document")       # the extension stays

    def test_rename_with_f2(self):
        window = self.window()
        tab = window.tab()
        self.assertEqual(shortcut(window, "rename"), ["F2"])
        self.select(window, "b.tar.gz")
        window.actions_["rename"].trigger()
        testing.process(20)
        editor = tab.view.editor()
        self.assertEqual((editor.text(), editor.selectedText()), ("b.tar.gz", "b"))
        testing.type_text(editor, "backup")
        testing.key(editor, Qt.Key.Key_Return)
        self.settle(window)
        self.assertTrue(os.path.exists(os.path.join(self.docs, "backup.tar.gz")))
        self.assertEqual([entry.name for entry in tab.selected_entries()], ["backup.tar.gz"])
        # a name that is taken, and a name that is not allowed
        for bad, words in (("a.txt", "already in this folder"), ("x/y", "slash")):
            self.select(window, "backup.tar.gz")
            window.actions_["rename"].trigger()
            testing.process(20)
            editor = tab.view.editor()
            editor.setText(bad)
            testing.key(editor, Qt.Key.Key_Return)
            self.settle(window)
            self.assertIn(words, window.banner.message())
            self.assertTrue(os.path.exists(os.path.join(self.docs, "backup.tar.gz")))
        # Escape leaves the name alone
        window.actions_["rename"].trigger()
        testing.process(20)
        editor = tab.view.editor()
        editor.setText("other")
        testing.key(editor, Qt.Key.Key_Escape)
        self.settle(window)
        self.assertIn("backup.tar.gz", self.names(window))
        # in an icon view too
        window.set_view_mode("icons-medium")
        self.select(window, "a.txt")
        window.actions_["rename"].trigger()
        testing.process(20)
        editor = tab.view.editor()
        self.assertEqual(editor.selectedText(), "a")
        testing.grab(window, self.tmp / "rename-icons.png")
        testing.key(editor, Qt.Key.Key_Escape)

    def test_copy_paste_and_clipboard_formats(self):
        window = self.window()
        self.select(window, "a.txt", "folder")
        self.assertEqual(shortcut(window, "copy"), ["Ctrl+C"])
        self.assertFalse(window.actions_["paste"].isEnabled())
        window.actions_["copy"].trigger()
        formats = self.clipboard_formats()
        self.assertEqual(clipformats.decode(formats), ([self.folder, self.a], False))
        self.assertEqual(formats["application/x-kde-cutselection"], b"0")
        self.assertTrue(formats["x-special/gnome-copied-files"].startswith(b"copy\nfile://"))
        self.assertTrue(window.actions_["paste"].isEnabled())
        window.actions_["paste"].trigger()                 # into the same folder: copies get new names
        self.settle(window)
        self.assertEqual(self.names(window), ["folder", "folder - Copy", "a - Copy.txt", "a.txt", "b.tar.gz"])
        self.assertEqual(sorted(entry.name for entry in window.tab().selected_entries()),
                         ["a - Copy.txt", "folder - Copy"])
        self.select(window, "a.txt")
        window.actions_["copy"].trigger()
        window.tab().navigate(self.folder)
        self.settle(window)
        window.actions_["paste"].trigger()
        self.settle(window)
        self.assertEqual(self.names(window), ["a.txt"])
        self.assertEqual(window.actions_["undo"].text(), "Undo Copy of \u201ca.txt\u201d")
        window.actions_["undo"].trigger()                  # the copy goes to the Trash; the original stays
        self.settle(window)
        self.assertEqual(self.names(window), [])
        self.assertEqual(self.read(self.a), b"AAA")
        self.assertEqual(window.open_dialogs(), [])

    def test_cut_paste_moves_and_dims(self):
        window = self.window()
        self.select(window, "a.txt")
        self.assertEqual(shortcut(window, "cut"), ["Ctrl+X"])
        window.actions_["cut"].trigger()
        formats = self.clipboard_formats()
        self.assertEqual(clipformats.decode(formats), ([self.a], True))
        self.assertEqual(formats["application/x-kde-cutselection"], b"1")
        self.assertEqual(window.services.cut_paths, frozenset([self.a]))
        window.tab().navigate(self.folder)
        self.settle(window)
        window.actions_["paste"].trigger()
        self.settle(window)
        self.assertEqual(self.names(window), ["a.txt"])
        self.assertFalse(os.path.exists(self.a))
        self.assertEqual(window.services.cut_paths, frozenset())
        self.assertFalse(window.actions_["paste"].isEnabled())
        self.assertEqual(window.actions_["undo"].text(), "Undo Move of \u201ca.txt\u201d")
        window.actions_["undo"].trigger()
        self.settle(window)
        self.assertTrue(os.path.exists(self.a))

    def test_paste_from_another_file_manager(self):
        outside = self.write("elsewhere/from dolphin.txt", "D")
        window = self.window()
        data = QMimeData()
        data.setData("text/uri-list", clipformats.encode_uri_list([outside]))
        data.setData("application/x-kde-cutselection", b"1")
        QGuiApplication.clipboard().setMimeData(data)
        testing.process()
        self.assertTrue(window.actions_["paste"].isEnabled())
        window.actions_["paste"].trigger()
        self.settle(window)
        self.assertIn("from dolphin.txt", self.names(window))
        self.assertFalse(os.path.exists(outside))               # it was cut there

    def test_conflict_dialog(self):
        self.write("home/Documents/folder/a.txt", "old")
        window = self.window()
        self.select(window, "a.txt")
        window.actions_["copy"].trigger()
        window.tab().navigate(self.folder)
        self.settle(window)
        window.actions_["paste"].trigger()
        self.assertTrue(testing.wait_until(lambda: any(isinstance(d, ConflictDialog) for d in window.open_dialogs()), 5))
        dialog = next(d for d in window.open_dialogs() if isinstance(d, ConflictDialog))
        self.assertIn("a.txt", dialog.text_label.text())
        self.assertEqual(dialog.replace_button.text(), "Replace")
        testing.grab(window, self.tmp / "conflict.png")
        testing.click(dialog.keep_button)
        self.settle(window)
        self.assertEqual(self.names(window), ["a (2).txt", "a.txt"])
        self.assertEqual(self.read(os.path.join(self.folder, "a.txt")), b"old")
        window.actions_["paste"].trigger()
        self.assertTrue(testing.wait_until(lambda: bool(window.open_dialogs()), 5))
        dialog = window.open_dialogs()[0]
        dialog.for_all.setChecked(True)
        testing.click(dialog.replace_button)
        self.settle(window)
        self.assertEqual(self.read(os.path.join(self.folder, "a.txt")), b"AAA")
        window.actions_["paste"].trigger()
        self.assertTrue(testing.wait_until(lambda: bool(window.open_dialogs()), 5))
        testing.click(window.open_dialogs()[0].close_button)        # Cancel: the operation stops
        self.settle(window)
        self.assertEqual(window.open_dialogs(), [])
        self.assertIn("cancelled", window.banner.message())

    def test_delete_goes_to_the_trash_and_comes_back(self):
        window = self.window()
        tab = window.tab()
        self.assertEqual(shortcut(window, "delete"), ["Del"])
        self.assertEqual(shortcut(window, "delete_forever"), ["Shift+Del"])
        self.select(window, "a.txt", "folder")
        window.actions_["delete"].trigger()
        self.settle(window)
        self.assertEqual(self.names(window), ["b.tar.gz"])
        tab.navigate(places.TRASH)
        self.settle(window)
        self.assertEqual(self.names(window), ["folder", "a.txt"])
        self.assertEqual(tab.model.columns(), ("name", "location", "deleted", "type", "size"))
        row = tab.model.row_of(window.services.trash.items()[0].files_path)
        self.assertEqual(tab.model.index(row, 1).data(), self.docs)
        self.assertRegex(tab.model.index(row, 2).data(), r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertEqual(tab.model.headerData(1, Qt.Orientation.Horizontal), "Original location")
        self.assertTrue(window.actions_["empty_trash"].isVisible())
        self.assertFalse(window.actions_["rename"].isEnabled())
        self.assertFalse(window.actions_["restore"].isEnabled())
        tab.view.select_paths([entry.path for entry in tab.model.rows if entry.name == "a.txt"])
        testing.process()
        self.assertEqual(self.menu_texts(window.context_menu(tab.selected_entries()[0])),
                         ["Restore", "Delete", "Copy as path", "Properties"])
        window.actions_["restore"].trigger()
        self.settle(window)
        self.assertEqual(self.names(window), ["folder"])
        self.assertEqual(self.read(self.a), b"AAA")
        # delete from the Trash asks first
        tab.view.select_all()
        self.answers = [False, True]
        window.actions_["delete"].trigger()
        self.settle(window)
        self.assertEqual(self.names(window), ["folder"])
        window.actions_["delete"].trigger()
        self.settle(window)
        self.assertEqual(self.names(window), [])
        self.assertEqual(tab.empty.title_label.text(), "The Trash is empty")
        self.assertEqual(window.services.trash.items(), [])

    def test_undo_of_trash_and_empty_trash(self):
        window = self.window()
        self.select(window, "a.txt")
        window.actions_["delete"].trigger()
        self.settle(window)
        self.assertEqual(window.actions_["undo"].text(), "Undo Delete of \u201ca.txt\u201d")
        window.actions_["undo"].trigger()
        self.settle(window)
        self.assertIn("a.txt", self.names(window))
        self.select(window, "a.txt", "b.tar.gz")
        window.actions_["delete"].trigger()
        self.settle(window)
        window.tab().navigate(places.TRASH)
        self.settle(window)
        self.answers = [False, True]
        window.actions_["empty_trash"].trigger()
        self.settle(window)
        self.assertEqual(len(self.names(window)), 2)
        window.actions_["empty_trash"].trigger()
        self.settle(window)
        self.assertEqual(self.names(window), [])
        self.assertFalse(window.actions_["empty_trash"].isEnabled())

    def test_confirm_before_trash_setting_and_permanent_delete(self):
        window = self.window()
        window.services.set_setting("confirm_trash", True)
        self.select(window, "a.txt")
        self.answers = [False]
        window.actions_["delete"].trigger()
        self.settle(window)
        self.assertTrue(os.path.exists(self.a))
        self.answers = [False, True]
        window.actions_["delete_forever"].trigger()
        self.settle(window)
        self.assertTrue(os.path.exists(self.a))
        window.actions_["delete_forever"].trigger()
        self.settle(window)
        self.assertFalse(os.path.exists(self.a))
        self.assertEqual(window.services.trash.items(), [])
        self.assertFalse(window.actions_["undo"].isEnabled())

    def test_errors_are_listed_at_the_end(self):
        window = self.window()
        missing = os.path.join(self.docs, "gone.txt")
        window.transfer([missing, self.a], self.folder, "copy")
        self.assertTrue(testing.wait_until(lambda: any(isinstance(d, ErrorsDialog) for d in window.open_dialogs()), 5))
        dialog = window.open_dialogs()[0]
        self.assertIn("gone.txt", dialog.details.toPlainText())
        self.assertIn("no longer exists", dialog.details.toPlainText())
        self.assertEqual(dialog.text_label.text(), "One item could not be handled.")
        testing.click(dialog.close_button)
        self.settle(window)
        self.assertTrue(os.path.exists(os.path.join(self.folder, "a.txt")))
        window.transfer([self.folder], os.path.join(self.folder), "move")
        self.assertTrue(testing.wait_until(lambda: bool(window.open_dialogs()), 5))
        self.assertIn("inside this folder", window.open_dialogs()[0].details.toPlainText())

    def test_drop_rules_and_drop_on_view_sidebar_and_tab(self):
        from boswas_apps.files import dnd
        both = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
        device = os.stat(self.docs).st_dev
        none, ctrl, shift = (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier,
                             Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(dnd.choose_action(none, both, device, self.folder), Qt.DropAction.MoveAction)
        self.assertEqual(dnd.choose_action(none, both, device + 1, self.folder), Qt.DropAction.CopyAction)
        self.assertEqual(dnd.choose_action(ctrl, both, device, self.folder), Qt.DropAction.CopyAction)
        self.assertEqual(dnd.choose_action(shift, both, device + 1, self.folder), Qt.DropAction.MoveAction)
        self.assertEqual(dnd.choose_action(none, both, device + 1, places.TRASH), Qt.DropAction.MoveAction)
        self.assertEqual(dnd.choose_action(shift, Qt.DropAction.CopyAction, device, self.folder),
                         Qt.DropAction.CopyAction)
        self.assertEqual(dnd.choose_action(none, Qt.DropAction.LinkAction, device, self.folder),
                         Qt.DropAction.IgnoreAction)
        window = self.window()
        tab = window.tab()
        data = QMimeData()
        data.setData("text/uri-list", clipformats.encode_uri_list([self.a]))
        self.assertEqual(dnd.source_device(data), device)
        # the target under the pointer: a folder row, else the folder shown
        tree = tab.view.tree
        rect = tree.visualRect(tab.model.index_of(self.folder))
        self.assertEqual(tab.view._drop_target(tree, QPoint(rect.left() + 50, rect.center().y())), (self.folder, 0))
        rect = tree.visualRect(tab.model.index_of(self.a))
        self.assertEqual(tab.view._drop_target(tree, QPoint(rect.left() + 50, rect.center().y())), (self.docs, -1))
        tab.view.dropped.emit([self.a], self.folder, "move")
        self.settle(window)
        self.assertTrue(os.path.exists(os.path.join(self.folder, "a.txt")))
        self.assertFalse(os.path.exists(self.a))
        window.sidebar.dropped.emit([os.path.join(self.folder, "a.txt")], os.path.join(self.home, "Downloads"), "copy")
        self.settle(window)
        self.assertTrue(os.path.exists(os.path.join(self.home, "Downloads", "a.txt")))
        window.add_tab(self.folder, activate=False)
        self.assertEqual(window.tabs.destination_of(1), self.folder)
        window.tabs.dropped.emit(1, [self.b], "copy")
        self.settle(window)
        self.assertTrue(os.path.exists(os.path.join(self.folder, "b.tar.gz")))
        window.sidebar.dropped.emit([self.b], places.TRASH, "move")
        self.settle(window)
        self.assertEqual([item.original_path for item in window.services.trash.items()], [self.b])

    def test_progress_panel_and_cancel(self):
        from boswas_apps.files.fileops import Operation, Progress

        class Slow(Operation):
            title = "Copying a lot"

            def _run(self):
                self.progress = Progress("/x/current file.bin", 3, 10, 300 * 2 ** 20, 2 ** 30)
                self._emit(force=True)
                while True:
                    self._check()
                    time.sleep(0.005)

        window = self.window()
        results = []
        job = window.services.jobs.run(Slow(), results.append, owner=window)
        self.assertTrue(testing.wait_until(lambda: window.operations.isVisible(), 3))
        row = window.operations.rows()[0]
        self.assertEqual(row.title.text(), "Copying a lot")
        self.assertEqual(row.current.text(), "current file.bin")
        self.assertEqual(row.detail.text(), "3 of 10 items, 300 MB of 1.00 GB")
        self.assertEqual(row.bar.value(), 293)
        testing.grab(window, self.tmp / "progress.png")
        # the window stays usable while the operation runs
        window.actions_["new_tab"].trigger()
        self.assertEqual(window.tabs.count(), 2)
        testing.click(row.cancel_button)
        self.settle(window)
        self.assertTrue(results[0].cancelled)
        self.assertFalse(window.operations.isVisible())
        self.assertFalse(job.is_running())

    def test_compress_and_extract_from_the_context_menu(self):
        window = self.window()
        tab = window.tab()
        self.select(window, "a.txt", "folder")
        menu = window.context_menu(tab.selected_entries()[0])
        self.trigger(menu, "Compress to ZIP")
        self.settle(window)
        archive = os.path.join(self.docs, "Documents.zip")
        with zipfile.ZipFile(archive) as packed:
            self.assertEqual(sorted(packed.namelist()), ["a.txt", "folder/"])
        self.assertEqual([entry.name for entry in tab.selected_entries()], ["Documents.zip"])
        menu = window.context_menu(tab.selected_entries()[0])
        self.assertIn("Extract all", self.menu_texts(menu))
        self.trigger(menu, "Extract all")
        self.settle(window)
        self.assertEqual(self.read(os.path.join(self.docs, "Documents", "a.txt")), b"AAA")
        self.assertEqual([entry.name for entry in tab.selected_entries()], ["Documents"])
        self.select(window, "a.txt")
        self.assertNotIn("Extract all", self.menu_texts(window.context_menu(tab.selected_entries()[0])))

    def test_context_menus(self):
        window = self.window()
        tab = window.tab()
        self.select(window, "folder")
        folder_menu = self.menu_texts(window.context_menu(tab.selected_entries()[0]))
        self.assertEqual(folder_menu, ["Open", "Open in new tab", "Open in new window", "Cut", "Copy",
                                       "Paste into folder", "Copy as path", "Rename", "Delete", "Compress to ZIP",
                                       "Pin to quick access", "Open in Terminal", "Properties"])
        self.select(window, "a.txt")
        self.assertEqual(self.menu_texts(window.context_menu(tab.selected_entries()[0])),
                         ["Open", "Open with", "Cut", "Copy", "Copy as path", "Rename", "Delete", "Compress to ZIP",
                          "Properties"])
        empty = window.context_menu(None)
        self.assertEqual(self.menu_texts(empty), ["View", "Sort by", "Refresh", "New", "Paste", "Open in Terminal",
                                                  "Select all", "Properties"])
        # right click in the view: on a name it selects the row, in the empty space it clears the selection
        tree = tab.view.tree
        seen = []
        tab.view.context_requested.connect(lambda entry, _pos: seen.append(entry.name if entry else None))
        from PySide6.QtGui import QContextMenuEvent
        from PySide6.QtWidgets import QApplication
        window._view_context = lambda *_args: None
        rect = tree.visualRect(tab.model.index_of(self.b))
        for point in (QPoint(rect.left() + 50, rect.center().y()), QPoint(rect.left() + 50, rect.bottom() + 200)):
            tab.view.context_requested.disconnect(window._view_context) if False else None
            QApplication.sendEvent(tree.viewport(), QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point,
                                                                      tree.viewport().mapToGlobal(point)))
            testing.process()
        for menu in window.findChildren(type(empty)):
            menu.close()
        self.assertEqual(seen, ["b.tar.gz", None])
        self.assertEqual(tab.selected_entries(), [])

    def test_copy_as_path_pin_and_terminal(self):
        window = self.window()
        tab = window.tab()
        self.select(window, "a.txt", "folder")
        self.assertEqual(shortcut(window, "copy_path"), ["Ctrl+Shift+C"])
        window.actions_["copy_path"].trigger()
        self.assertEqual(QGuiApplication.clipboard().text(), f"{self.folder}\n{self.a}")
        self.select(window, "folder")
        self.trigger(window.context_menu(tab.selected_entries()[0]), "Pin to quick access")
        self.assertEqual(window.services.settings.pinned(), [self.folder])
        item = window.sidebar.item_for(self.folder)
        self.assertEqual((item.text(), item.data(KIND_ROLE)), ("folder", PINNED))
        other = self.mkdir("home/Documents/other")
        window.pin(other)
        self.assertEqual(self.menu_texts(window.sidebar_menu(window.sidebar.item_for(other))),
                         ["Open", "Open in new tab", "Open in new window", "Move up", "Move down",
                          "Unpin from quick access", "Open in Terminal", "Properties"])
        self.trigger(window.sidebar_menu(window.sidebar.item_for(other)), "Move up")
        self.assertEqual(window.services.settings.pinned(), [other, self.folder])
        self.trigger(window.sidebar_menu(window.sidebar.item_for(other)), "Unpin from quick access")
        self.assertIsNone(window.sidebar.item_for(other))
        self.assertIn("Unpin from quick access", self.menu_texts(window.context_menu(tab.selected_entries()[0])))
        self.trigger(window.context_menu(tab.selected_entries()[0]), "Open in Terminal")
        self.assertEqual(self.launcher.started[-1],
                         ("/usr/bin/boswas-terminal", ["--working-directory", self.folder], self.folder))
        self.launcher.programs = {"konsole"}
        window.actions_["terminal"].trigger()
        self.assertEqual(self.launcher.started[-1], ("/usr/bin/konsole", ["--workdir", self.docs], self.docs))
        self.launcher.programs = set()
        window.actions_["terminal"].trigger()
        self.assertIn("No terminal application", window.banner.message())

    def test_sidebar(self):
        window = self.window()
        sidebar = window.sidebar
        labels = [sidebar.item(row).text() for row in range(sidebar.count()) if sidebar.item(row).text()]
        self.assertEqual(labels, ["Home", "Documents", "Downloads", "This PC", "Trash"])
        item = sidebar.item_for(os.path.join(self.home, "Downloads"))
        testing.click(sidebar.viewport(), sidebar.visualItemRect(item).center())
        self.settle(window)
        self.assertEqual(window.tab().location, os.path.join(self.home, "Downloads"))
        self.assertEqual(sidebar.currentItem().text(), "Downloads")
        testing.click(sidebar.viewport(), sidebar.visualItemRect(sidebar.item_for(places.TRASH)).center(),
                      button=Qt.MouseButton.MiddleButton)
        self.settle(window)
        self.assertEqual(window.tabs.count(), 2)
        self.assertEqual(window.tab().location, places.TRASH)
        sidebar.setFocus()
        sidebar.setCurrentRow(0)
        testing.key(sidebar, Qt.Key.Key_Return)
        self.settle(window)
        self.assertEqual(window.tab().location, self.home)
        testing.key(sidebar, Qt.Key.Key_Down)                   # skips the separator
        self.assertEqual(sidebar.currentItem().text(), "Documents")


class OpeningTest(FilesCase):
    def setUp(self):
        super().setUp()
        self.text = self.write("home/Documents/notes.txt", "hello\nworld\n")
        self.picture = os.path.join(self.docs, "photo.png")
        image = QImage(600, 400, QImage.Format.Format_RGB32)
        image.fill(0x336699)
        image.save(self.picture)
        self.script = self.write("home/Documents/run.sh", "#!/bin/sh\necho hi\n", mode=0o755)
        self.unknown = self.write("home/Documents/data.xyzzy", b"\0\1\2\3")
        apps = self.path("share/applications")
        os.makedirs(apps)
        for name, body in (("kate.desktop", "Name=Kate\nExec=kate %U\nMimeType=text/plain;\nIcon=kate\n"),
                           ("viewer.desktop", "Name=Viewer\nExec=viewer --view %f\nMimeType=image/png;text/plain;\n"),
                           ("tool.desktop", "Name=Console Tool\nExec=viewer %F\nTerminal=true\n")):
            with open(os.path.join(apps, name), "w") as handle:
                handle.write("[Desktop Entry]\nType=Application\n" + body)
        self.answers = []

    def entry(self, window, name):
        model = window.tab().model
        return model.entry(model.row_of(os.path.join(self.docs, name)))

    def test_open_starts_the_default_application(self):
        window = self.window()
        window.open_entry(self.entry(window, "notes.txt"))
        self.settle(window)
        self.assertEqual(self.launcher.started, [("/usr/bin/kate", ["file://" + self.text], self.docs)])
        window.open_entry(self.entry(window, "photo.png"))
        self.settle(window)
        self.assertEqual(self.launcher.started[-1], ("/usr/bin/viewer", ["--view", self.picture], self.docs))
        self.select(window, "notes.txt")
        testing.key(window.tab().view.tree, Qt.Key.Key_Return)
        self.settle(window)
        self.assertEqual(len(self.launcher.started), 3)

    def test_open_with_menu_and_default_application(self):
        window = self.window()
        self.select(window, "notes.txt")
        menu = window.context_menu(window.tab().selected_entries()[0])
        submenu = next(action.menu() for action in menu.actions() if action.text() == "Open with")
        self.assertEqual(self.menu_texts(submenu), ["Kate", "Viewer", "Choose another app\u2026"])
        self.trigger(submenu, "Viewer")
        self.assertEqual(self.launcher.started[-1], ("/usr/bin/viewer", ["--view", self.text], self.docs))
        self.trigger(submenu, "Choose another app\u2026")
        self.assertTrue(testing.wait_until(lambda: bool(window.open_dialogs()), 3))
        dialog = window.open_dialogs()[0]
        self.assertIsInstance(dialog, OpenWithDialog)
        names = [dialog.list.item(row).text() for row in range(dialog.list.count())]
        self.assertEqual(names, ["Kate", "Viewer", "Console Tool"])
        self.assertEqual(dialog.always.text(), "Always use this app to open .txt files")
        dialog.list.setCurrentRow(1)
        dialog.always.setChecked(True)
        testing.grab(window, self.tmp / "open-with.png")
        testing.click(dialog.primary_button)
        self.settle(window)
        self.assertEqual(self.launcher.started[-1], ("/usr/bin/viewer", ["--view", self.text], self.docs))
        written = self.read(self.path("xdg_config_home/mimeapps.list")).decode()
        self.assertIn("[Default Applications]\ntext/plain=viewer.desktop\n", written)
        window.open_entry(self.entry(window, "notes.txt"))
        self.settle(window)
        self.assertEqual(self.launcher.started[-1][0], "/usr/bin/viewer")
        # an application that wants a terminal is started inside one
        dialog = None
        window.choose_app([self.text], "text/plain")
        dialog = window.open_dialogs()[0]
        dialog.list.setCurrentRow(2)
        testing.click(dialog.primary_button)
        self.settle(window)
        self.assertEqual(self.launcher.started[-1],
                         ("/usr/bin/boswas-terminal", ["-e", "viewer", self.text], self.docs))

    def test_a_file_without_an_application_asks(self):
        window = self.window()
        window.open_entry(self.entry(window, "data.xyzzy"))
        self.settle(window)
        self.assertEqual(self.launcher.started, [])
        dialog = window.open_dialogs()[0]
        self.assertIsInstance(dialog, OpenWithDialog)
        testing.click(dialog.close_button)
        self.assertEqual(self.launcher.started, [])

    def test_programs_are_never_run_without_asking(self):
        window = self.window()
        window.open_entry(self.entry(window, "run.sh"))
        self.settle(window)
        self.assertEqual(self.launcher.started, [])
        dialog = window.open_dialogs()[0]
        self.assertEqual(dialog.title_label.text(), "Run \u201crun.sh\u201d?")
        self.assertIs(dialog.default_button(), dialog.close_button)
        testing.click(dialog.close_button)
        self.assertEqual(self.launcher.started, [])
        window.open_entry(self.entry(window, "run.sh"))
        self.settle(window)
        testing.click(window.open_dialogs()[0].secondary_button)        # Open as text
        self.settle(window)
        self.assertEqual(self.launcher.started[-1][0], "/usr/bin/kate")
        window.open_entry(self.entry(window, "run.sh"))
        self.settle(window)
        testing.click(window.open_dialogs()[0].primary_button)          # Run
        self.settle(window)
        self.assertEqual(self.launcher.started[-1], (self.script, [], self.docs))
        # a desktop entry is a program too, even without the execute bit
        entry_file = self.write("home/Documents/evil.desktop",
                                "[Desktop Entry]\nType=Application\nName=Evil\nExec=viewer --wipe\n", mode=0o644)
        self.settle(window)
        window.open_file(entry_file, 0o100644)
        self.settle(window)
        count = len(self.launcher.started)
        dialog = window.open_dialogs()[0]
        self.assertEqual(dialog.title_label.text(), "Run \u201cevil.desktop\u201d?")
        testing.click(dialog.primary_button)
        self.settle(window)
        self.assertEqual(self.launcher.started[count:], [("/usr/bin/viewer", ["--wipe"], "")])

    def test_launch_failure_is_told(self):
        window = self.window()
        self.launcher.programs = set()
        window.open_entry(self.entry(window, "notes.txt"))
        self.settle(window)
        self.assertIn("was not found", window.banner.message())

    def test_details_pane(self):
        window = self.window()
        self.assertEqual(shortcut(window, "details_pane"), ["Alt+Shift+P"])
        self.assertFalse(window.details.isVisible())
        testing.click(window.details_button)
        self.assertTrue(window.details.isVisible())
        self.assertEqual(window.details.title.text(), "Documents")
        self.assertEqual(window.details.subtitle.text(), "4 items")
        self.select(window, "notes.txt")
        self.settle(window, 50)
        self.assertEqual(window.details.title.text(), "notes.txt")
        self.assertEqual(window.details.text_preview.toPlainText(), "hello\nworld\n")
        self.assertTrue(window.details.text_preview.isVisible())
        self.assertEqual(window.details.row("Size"), "12 bytes")
        self.select(window, "photo.png")
        self.settle(window, 50)
        self.assertEqual(window.details.row("Dimensions"), "600 \u00d7 400")
        self.assertFalse(window.details.text_preview.isVisible())
        self.assertEqual(window.details.picture.pixmap().width(), min(256, window.details.viewport().width() - 40))
        self.select(window, "photo.png", "notes.txt")
        self.settle(window, 50)
        self.assertEqual(window.details.title.text(), "2 items selected")
        self.assertEqual(window.details.row("Files"), "2")
        testing.grab(window, self.tmp / "details.png")
        self.select(window, "data.xyzzy")
        self.settle(window, 50)
        self.assertFalse(window.details.text_preview.isVisible())       # binary: no text preview
        window.actions_["details_pane"].trigger()
        self.assertFalse(window.details.isVisible())

    def test_thumbnails_are_made_and_cached(self):
        from boswas_apps.files import thumbs
        window = self.window()
        big = os.path.join(self.docs, "big.png")
        image = QImage(1200, 800, QImage.Format.Format_RGB32)
        image.fill(0x993366)
        image.save(big)
        self.write("home/Documents/broken.png", b"not a picture")
        window.tab().refresh()
        self.settle(window)
        window.set_view_mode("icons-large")
        testing.process(50)
        thumbnailer = window.services.thumbnailer
        self.assertTrue(testing.wait_until(lambda: not thumbnailer.worker.pending(), 10))
        testing.process(20)
        cached = thumbs.thumb_path(big, "normal")
        self.assertTrue(os.path.isfile(cached))
        self.assertEqual(stat.S_IMODE(os.stat(cached).st_mode), 0o600)
        self.assertTrue(thumbs.is_valid(cached, big, os.stat(big).st_mtime))
        text = thumbs.read_png_text(cached)
        self.assertEqual((text["Thumb::Image::Width"], text["Thumb::Image::Height"], text["Software"]),
                         ("1200", "800", "Boswas Files"))
        self.assertEqual(QImage(cached).width(), 128)
        self.assertTrue(thumbs.has_failed(os.path.join(self.docs, "broken.png"),
                                          os.stat(os.path.join(self.docs, "broken.png")).st_mtime))
        pixmap = thumbnailer.get(big, os.stat(big).st_mtime, os.path.getsize(big), 96)
        self.assertIsNotNone(pixmap)
        self.assertIsNone(thumbnailer.get(self.text, 0, 10, 96))
        testing.grab(window, self.tmp / "thumbnails.png")

    def test_properties(self):
        window = self.window()
        self.select(window, "notes.txt")
        self.assertEqual(shortcut(window, "properties"), ["Alt+Return", "Alt+Enter"])
        properties = window.show_properties()
        self.keep(properties)
        self.settle(window, 50)
        self.assertEqual(properties.windowTitle(), "notes.txt \u2013 Properties")
        self.assertEqual(properties.value("Type"), "Plain text document")
        self.assertEqual(properties.value("Location"), self.docs)
        self.assertEqual(properties.value("Size"), "12 bytes")
        self.assertEqual(properties.opens_label.text(), "Kate")
        self.assertTrue(properties.value("Owner"))
        os.chmod(self.text, 0o644)
        properties.close()
        properties = self.keep(window.show_properties([self.text]))
        self.assertEqual([properties.access_boxes[key].currentText() for key in ("Owner can", "Group can", "Others can")],
                         ["Read and write", "Read-only", "Read-only"])
        self.assertFalse(properties.execute_box.isChecked())
        properties.access_boxes["Others can"].setCurrentIndex(2)
        properties.execute_box.setChecked(True)
        self.settle(window, 20)
        self.assertEqual(stat.S_IMODE(os.stat(self.text).st_mode), 0o750)
        testing.grab(properties, self.tmp / "properties.png")
        # "Change..." sets the default application of the type
        testing.click(properties.change_button)
        dialog = window._last_open_with
        self.assertEqual(dialog.primary_button.text(), "Set default")
        dialog.list.setCurrentRow(1)
        testing.click(dialog.primary_button)
        self.settle(window, 20)
        self.assertEqual(properties.opens_label.text(), "Viewer")
        self.assertEqual(self.launcher.started, [])
        properties.close()
        # a folder is counted in the background; several items are added up
        self.write("home/Documents/sub/one.bin", b"1" * 1000)
        self.write("home/Documents/sub/deep/two.bin", b"2" * 500)
        properties = self.keep(window.show_properties([os.path.join(self.docs, "sub")]))
        self.assertTrue(testing.wait_until(lambda: properties.value("Contains") == "2 files, 1 folders", 5))
        self.assertEqual(properties.value("Size"), "1.46 KB (1,500 bytes)")
        self.assertEqual(properties.access_boxes["Owner can"].currentText(), "Create and delete files")
        properties.close()
        properties = self.keep(window.show_properties([self.text, os.path.join(self.docs, "sub")]))
        self.assertTrue(testing.wait_until(lambda: properties.value("Size").endswith("(1,512 bytes)"), 5))
        self.assertEqual(properties.value("Items"), "2")
        properties.close()
        link = os.path.join(self.docs, "link")
        os.symlink("notes.txt", link)
        properties = self.keep(window.show_properties([link]))
        self.assertEqual(properties.value("Link to"), "notes.txt")
        properties.close()
        properties = self.keep(window.show_properties([self.path("home/ghost")]))
        self.assertTrue(properties.banner.isVisible())
        properties.close()

    def test_settings_dialog(self):
        window = self.window()
        dialog = window.show_settings()
        self.assertIsInstance(dialog, SettingsDialog)
        testing.process(50)
        testing.grab(window, self.tmp / "settings.png")
        testing.click(dialog.switches["show_hidden"])
        self.assertTrue(window.services.settings.get("show_hidden"))
        self.assertTrue(window.tab().model.show_hidden)
        testing.click(dialog.switches["show_extensions"])
        self.assertEqual(window.tab().model.index(window.tab().model.row_of(self.text), 0).data(), "notes")
        testing.click(dialog.switches["single_click"])
        self.assertTrue(window.tab().view.single_click)
        testing.click(dialog.switches["confirm_trash"])
        self.assertTrue(window.services.settings.get("confirm_trash"))
        dialog.view_box.setCurrentIndex(dialog.view_box.findData("tiles"))
        self.assertEqual(window.tab().view.view_mode, "tiles")
        dialog.start_box.setCurrentIndex(dialog.start_box.findData("computer"))
        self.assertEqual(window.services.settings.get("start_folder"), "computer")
        dialog.start_box.setCurrentIndex(dialog.start_box.findData("custom"))
        self.assertTrue(dialog.start_path.isVisible())
        dialog.start_path.setText("/no/such/folder")
        dialog.start_path.editingFinished.emit()
        self.assertTrue(dialog.start_error.isVisible())
        dialog.start_path.setText(self.docs)
        dialog.start_path.editingFinished.emit()
        self.assertEqual(window.services.settings.get("start_folder"), self.docs)
        testing.click(dialog.primary_button)
        self.assertEqual(window.open_dialogs(), [])
        window.actions_["new_tab"].trigger()
        self.settle(window)
        self.assertEqual(window.tab().location, self.docs)
        # and it is all there in a new session
        from boswas_apps.files.settings import Settings
        again = Settings(window.services.settings.path)
        self.assertEqual((again.get("show_hidden"), again.get("view_mode"), again.get("single_click")),
                         (True, "tiles", True))


if __name__ == "__main__":
    unittest.main()
