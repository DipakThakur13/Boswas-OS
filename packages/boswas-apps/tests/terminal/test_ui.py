"""The window: tabs, shortcuts, what happens when programs end, search, settings, a real child process."""

import json
import os
import time
import unittest
from unittest import mock

from term_fixtures import CTRL, SHIFT, WindowCase, cell_point, drag, mouse, testing

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

import boswas_fluent as fluent

from boswas_apps.terminal import APP_ID, cli, session
from boswas_apps.terminal import app as app_module
from boswas_apps.terminal import settings as settings_module
from boswas_apps.terminal.pane import HIGH_WATER, start_session
from boswas_apps.terminal.session import PtySession
from boswas_apps.terminal.settings_page import SettingsPage


class Tabs(WindowCase):
    def test_first_tab_runs_the_shell_at_the_size_of_the_view(self):
        window = self.window()
        self.assertEqual(len(window.tabs), 1)
        pane = window.current_pane()
        self.assertEqual(pane.state, "running")
        self.assertEqual(len(self.sessions.made), 1)
        cols, rows = pane.view.grid_size()
        cell_w, cell_h = pane.view.cell_size()
        self.assertEqual(self.sessions.sizes[0], (cols, rows, cols * cell_w, rows * cell_h))
        self.assertGreater(cols, 60)
        launch = self.sessions.launches[0]
        self.assertTrue(os.path.isabs(launch.program))
        self.assertTrue(pane.view.hasFocus())
        self.assertEqual(QGuiApplication.desktopFileName() or APP_ID, APP_ID)

    def test_keys_reach_the_program_and_output_the_screen(self):
        window = self.window()
        pane = window.current_pane()
        testing.type_text(pane.view, "echo hi")
        testing.key(pane.view, Qt.Key.Key_Return, text="\r")
        self.assertEqual(bytes(pane.session.written), b"echo hi\r")
        self.play(window, "hi\r\n$ ")
        self.assertEqual(pane.view.term.screen_text()[:2], ["hi", "$"])

    def test_resizing_the_window_resizes_the_terminal(self):
        window = self.window()
        pane = window.current_pane()
        window.resize(600, 400)
        testing.process(20)
        self.assertEqual(pane.session.sizes[-1][:2], pane.view.grid_size())
        self.assertLess(pane.view.grid_size()[0], self.sessions.sizes[0][0])

    def test_new_tab_starts_in_the_directory_of_the_current_tab(self):
        window = self.window()
        first = window.current_pane()
        first.session.directory = str(self.tmp)
        self.assertTrue(window.trigger("Ctrl+Shift+T"))
        testing.process(20)
        self.assertEqual(len(window.tabs), 2)
        self.assertEqual(window.strip.current(), 1)
        self.assertEqual(self.sessions.launches[1].directory, str(self.tmp))
        self.play(window, f"\x1b]7;file://{first.view.term.hostname}/usr\x07", 0)
        window.select_tab(0)
        window.new_tab_here()
        testing.process(20)
        self.assertEqual(self.sessions.launches[2].directory, "/usr")
        self.assertEqual(window.tabs.index(window.current_pane()), 1)       # beside the tab it came from

    def test_switching_tabs(self):
        window = self.window()
        for _ in range(3):
            window.new_tab()
        testing.process(20)
        self.assertEqual(window.strip.current(), 3)
        window.trigger("Ctrl+Tab")
        self.assertEqual(window.strip.current(), 0)
        window.trigger("Ctrl+Shift+Tab")
        self.assertEqual(window.strip.current(), 3)
        window.trigger("Alt+2")
        self.assertEqual(window.strip.current(), 1)
        self.assertIs(window.stack.currentWidget(), window.tabs[1])
        self.assertTrue(window.tabs[1].view.hasFocus())
        window.trigger("Alt+9")
        self.assertEqual(window.strip.current(), 3)
        testing.click(window.strip, window.strip.tab_rect(2).center())
        self.assertEqual(window.strip.current(), 2)
        testing.key(window.strip, Qt.Key.Key_Left)
        self.assertEqual(window.strip.current(), 1)

    def test_titles(self):
        window = self.window()
        pane = window.current_pane()
        self.assertEqual(window.strip.title(0), self.store.settings.default().name)
        self.play(window, "\x1b]0;vim notes\x07")
        self.assertEqual(window.strip.title(0), "vim notes")
        self.assertEqual(window.windowTitle(), "vim notes – Terminal")
        window.new_tab()
        testing.process(20)
        self.assertNotIn("vim", window.windowTitle())
        self.assertEqual(pane.title(), "vim notes")

    def test_rename_in_place(self):
        window = self.window()
        strip = window.strip
        mouse(strip, "double", strip.tab_rect(0).center())
        editor = strip.rename_editor()
        self.assertIsNotNone(editor)
        editor.selectAll()
        testing.type_text(editor, "build")
        testing.key(editor, Qt.Key.Key_Return)
        self.assertFalse(strip.is_renaming())
        self.assertEqual(strip.title(0), "build")
        self.play(window, "\x1b]0;ignored\x07")
        self.assertEqual(strip.title(0), "build")              # a fixed title stays
        strip.start_rename(0)
        testing.key(strip.rename_editor(), Qt.Key.Key_Escape)
        self.assertEqual(strip.title(0), "build")
        strip.start_rename(0)
        strip.rename_editor().clear()
        strip.commit_rename()
        self.assertEqual(strip.title(0), "ignored")            # an empty title gives the tab its own back
        self.assertTrue(window.current_pane().view.hasFocus())

    def test_duplicate(self):
        window = self.window(cli.Request(command=["/bin/cat", "-n"], title="numbers"))
        pane = window.current_pane()
        pane.session.directory = "/usr"
        pane.view.set_zoom(2)
        window.trigger("Ctrl+Shift+D")
        testing.process(20)
        copy = window.current_pane()
        self.assertIsNot(copy, pane)
        self.assertEqual(window.tabs.index(copy), 1)
        self.assertEqual(self.sessions.launches[1].argv, ("/bin/cat", "-n"))
        self.assertEqual(self.sessions.launches[1].directory, "/usr")
        self.assertEqual((copy.title(), copy.view.zoom()), ("numbers", 2))

    def test_reordering_by_dragging(self):
        window = self.window()
        window.new_tab()
        window.new_tab()
        testing.process(20)
        panes = list(window.tabs)
        strip = window.strip
        start = strip.tab_rect(0).center()
        end = strip.tab_rect(2).center()
        mouse(strip, "press", start)
        mouse(strip, "move", QPoint(start.x() + 30, start.y()), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        mouse(strip, "move", end, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        mouse(strip, "release", end)
        self.assertEqual(window.tabs, [panes[1], panes[2], panes[0]])
        self.assertEqual(strip.current(), 2)
        self.assertIs(window.stack.currentWidget(), panes[0])
        self.play(window, "\x1b]0;moved\x07", 2)
        self.assertEqual(strip.title(2), "moved")

    def test_marks_for_background_tabs(self):
        window = self.window()
        window.new_tab()
        testing.process(20)
        self.play(window, "output\r\n", 0)
        self.assertEqual(window.strip.indicator(0), "activity")
        self.play(window, "\x07", 0)
        self.assertEqual(window.strip.indicator(0), "bell")
        self.assertEqual(window.strip.indicator(1), "")
        window.select_tab(0)
        self.assertEqual(window.strip.indicator(0), "")

    def test_tab_colours_follow_the_terminal(self):
        window = self.window()
        pane = window.current_pane()
        testing.set_scheme("light")
        testing.process(20)
        self.assertEqual(window.strip._tabs[0].background, pane.view.background())
        self.assertGreater(pane.view.background().lightness(), 128)

    def test_zoom_and_full_screen(self):
        window = self.window()
        view = window.current_pane().view
        window.trigger("Ctrl+=")
        window.trigger("Ctrl++")
        self.assertEqual(view.zoom(), 2)
        window.trigger("Ctrl+-")
        self.assertEqual(view.zoom(), 1)
        window.trigger("Ctrl+0")
        self.assertEqual(view.zoom(), 0)
        window.trigger("F11")
        self.assertTrue(window.isFullScreen())
        window.trigger("F11")
        self.assertFalse(window.isFullScreen())

    def test_copy_paste_and_select_all_shortcuts(self):
        window = self.window()
        pane = self.play(window, "copy me")
        drag(pane.view, cell_point(pane.view, 0, 0), cell_point(pane.view, 3, 0))
        QGuiApplication.clipboard().setText("")
        window.trigger("Ctrl+Shift+C")
        self.assertEqual(QGuiApplication.clipboard().text(), "copy")
        window.trigger("Ctrl+Shift+V")
        self.assertEqual(bytes(pane.session.written), b"copy")
        window.trigger("Ctrl+Shift+A")
        self.assertEqual(pane.view.selected_text(), "copy me")

    def test_menus(self):
        window = self.window()
        self.store.settings.profiles.append(settings_module.Profile("two", "Second", command="/bin/cat"))
        menu = window.build_menu()
        texts = [action.text() for action in menu.actions() if not action.isSeparator()]
        self.assertEqual(texts[:2], [self.store.settings.default().name + "  (default)", "Second"])
        self.assertEqual(texts[2:], ["New window", "Find", "Full screen", "Settings", "About Terminal"])
        menu.actions()[1].trigger()
        testing.process(20)
        self.assertEqual(self.sessions.launches[-1].argv, ("/bin/cat",))
        self.assertEqual(window.current_pane().title(), "Second")
        window.trigger("Ctrl+Shift+2")
        testing.process(20)
        self.assertEqual(len(window.tabs), 3)
        tab_menu = window.build_tab_menu(0)
        self.assertEqual([action.text() for action in tab_menu.actions() if not action.isSeparator()],
                         ["Rename tab", "Duplicate tab", "Close tab", "Close other tabs"])
        tab_menu.actions()[-1].trigger()
        testing.process(20)
        self.assertEqual(len(window.tabs), 1)
        testing.click(window.menu_button)
        self.assertIsNotNone(window.menu)
        window.menu.close()

    def test_context_menu_of_the_terminal(self):
        window = self.window()
        pane = self.play(window, "https://example.org text")
        pane._menu(QPoint(10, 10), "https://example.org")
        texts = [action.text() for action in pane.context_menu.actions() if not action.isSeparator()]
        self.assertEqual(texts, ["Open link", "Copy link address", "Copy", "Paste", "Select all", "Find"])
        actions = {action.text(): action for action in pane.context_menu.actions()}
        self.assertFalse(actions["Copy"].isEnabled())
        actions["Copy link address"].trigger()
        self.assertEqual(QGuiApplication.clipboard().text(), "https://example.org")
        actions["Select all"].trigger()
        self.assertEqual(pane.view.selected_text(), "https://example.org text")
        with mock.patch.object(fluent.desktop, "open_url", return_value=True) as opened:
            with mock.patch("boswas_fluent.open_url", opened):
                actions["Open link"].trigger()
        opened.assert_called_once_with("https://example.org")
        pane.context_menu.close()

    def test_links_that_cannot_be_opened_are_reported(self):
        window = self.window()
        pane = window.current_pane()
        with mock.patch("boswas_fluent.open_url", return_value=False):
            pane.open_link("https://example.org/x")
        self.assertTrue(pane.info.isVisible())
        self.assertIn("could not be opened", pane.info.message())
        pane.info.dismiss()
        pane.open_link("javascript:alert(1)")
        self.assertTrue(pane.info.isVisible())
        with mock.patch("boswas_fluent.open_path", return_value=True) as opened:
            pane.open_link("file:///etc/host%20name")
        opened.assert_called_once_with("/etc/host name")


class Closing(WindowCase):
    def test_close_tab_and_last_tab_closes_the_window(self):
        window = self.window()
        window.new_tab()
        testing.process(20)
        first = self.sessions.made[0]
        window.select_tab(0)
        window.trigger("Ctrl+Shift+W")
        self.assertEqual(len(window.tabs), 1)
        self.assertTrue(first.closed)
        testing.click(window.strip, window.strip.close_rect(0).center())
        testing.process(20)
        self.assertClosed(window)
        self.assertEqual(self.application.windows, [])
        self.assertTrue(all(made.closed for made in self.sessions.made))

    def test_middle_click_closes_a_tab(self):
        window = self.window()
        window.new_tab()
        testing.process(20)
        testing.click(window.strip, window.strip.tab_rect(0).center(), button=Qt.MouseButton.MiddleButton)
        self.assertEqual(len(window.tabs), 1)

    def test_running_program_asks_first(self):
        window = self.window()
        window.new_tab()
        testing.process(20)
        pane = window.current_pane()
        pane.session.foreground = "vim"
        asked = []
        window.ask = lambda title, text, *rest, **more: (asked.append(text), False)[1]
        self.assertFalse(window.close_tab(1))
        self.assertEqual(len(window.tabs), 2)
        self.assertIn("vim", asked[0])
        window.close()
        testing.process()
        self.assertTrue(window.isVisible())
        self.assertIn("1 tab", asked[1])
        window.ask = lambda *args, **more: True
        self.assertTrue(window.close_tab(1))
        self.assertEqual(len(window.tabs), 1)
        self.assertEqual(len(asked), 2)

    def test_real_dialog_answers(self):
        window = self.window()
        window.current_pane().session.foreground = "make"
        dialogs = []

        def answer(confirm):
            dialog = window.findChild(fluent.ContentDialog)
            dialogs.append(dialog.title_label.text() + " / " + dialog.text_label.text())
            (dialog.primary_button if confirm else dialog.close_button).click()

        QTimer.singleShot(50, lambda: answer(False))
        self.assertFalse(window.close_tab(0))
        self.assertIn("make", dialogs[0])
        QTimer.singleShot(50, lambda: answer(True))
        self.assertTrue(window.close_tab(0))
        testing.process(20)
        self.assertClosed(window)

    def test_no_question_when_only_the_shell_runs_or_when_turned_off(self):
        window = self.window()
        window.new_tab()
        testing.process(20)
        window.ask = lambda *args, **more: self.fail("asked")
        self.assertTrue(window.close_tab(1))
        window.current_pane().session.foreground = "top"
        self.store.settings.confirm_close = False
        window.close()
        testing.process()
        self.assertClosed(window)


class ProgramEnds(WindowCase):
    def profile(self, **changes):
        profile = self.store.settings.default()
        for key, value in changes.items():
            setattr(profile, key, value)
        return profile

    def test_success_closes_the_tab(self):
        window = self.window()
        window.new_tab()
        testing.process(20)
        self.sessions.made[1].end(0)
        testing.process(20)
        self.assertEqual(len(window.tabs), 1)
        self.sessions.made[0].end(0)
        testing.process(20)
        self.assertClosed(window)

    def test_failure_keeps_the_tab_with_a_message(self):
        window = self.window()
        pane = window.current_pane()
        pane.session.play("last words\r\n")
        pane.session.end(3)
        testing.process(20)
        pane.view.flush()
        self.assertEqual(len(window.tabs), 1)
        self.assertEqual(pane.state, "ended")
        text = pane.view.term.screen_text()
        self.assertEqual(text[:2], ["last words", "[The program ended with exit code 3.]"])
        self.assertIn("Press Enter", text[2])
        testing.type_text(pane.view, "ignored")
        self.assertEqual(len(self.sessions.made), 1)
        testing.key(pane.view, Qt.Key.Key_Return, text="\r")
        self.assertEqual(len(self.sessions.made), 2)
        self.assertEqual(pane.state, "running")
        self.assertIs(pane.session, self.sessions.made[1])
        pane.session.end(1)
        testing.process(20)
        testing.key(pane.view, Qt.Key.Key_D, CTRL, "\x04")
        testing.process(20)
        self.assertClosed(window)

    def test_signal_is_named(self):
        window = self.window()
        pane = window.current_pane()
        pane.session.end(137, 9)
        testing.process(20)
        self.assertIn("[The program was ended by SIGKILL.]", pane.view.term.screen_text())

    def test_always_and_never(self):
        self.profile(close_on_exit="never")
        window = self.window()
        window.new_tab()
        testing.process(20)
        self.sessions.made[1].end(0)
        testing.process(20)
        self.assertEqual(len(window.tabs), 2)
        self.assertIn("exit code 0", window.tabs[1].view.term.screen_text()[0])
        self.profile(close_on_exit="always")
        self.sessions.made[0].end(9)
        testing.process(20)
        self.assertEqual(len(window.tabs), 1)

    def test_program_that_cannot_be_started(self):
        window = self.window(cli.Request(command=["boswas-no-such-program"]))
        pane = window.current_pane()
        self.assertEqual(pane.state, "failed")
        self.assertEqual(self.sessions.made, [])
        self.assertIn("“boswas-no-such-program” was not found.", pane.view.term.screen_text()[0])
        self.assertEqual(len(window.tabs), 1)
        testing.key(pane.view, Qt.Key.Key_D, CTRL, "\x04")
        testing.process(20)
        self.assertClosed(window)


class Requests(WindowCase):
    def test_command_title_directory_profile(self):
        self.store.settings.profiles.append(settings_module.Profile("dev", "Dev", font_size=14))
        window = self.window(cli.Request(directory=str(self.tmp), title="Logs", profile="dev",
                                         command=["/bin/sh", "-c", "tail -f x"]))
        pane = window.current_pane()
        launch = self.sessions.launches[0]
        self.assertEqual((launch.program, launch.argv, launch.directory),
                         ("/bin/sh", ("/bin/sh", "-c", "tail -f x"), str(self.tmp)))
        self.assertEqual((pane.title(), pane.profile_id, pane.view.font_points()), ("Logs", "dev", 14))
        self.assertFalse(pane.info.isVisible())

    def test_unknown_profile_is_reported(self):
        window = self.window(cli.Request(profile="Nope"))
        pane = window.current_pane()
        self.assertTrue(pane.info.isVisible())
        self.assertIn("“Nope”", pane.info.message())
        self.assertEqual(pane.profile_id, self.store.settings.default_profile)

    def test_second_start_opens_a_tab_and_new_window_a_window(self):
        window = self.window()
        self.application.handle_arguments(cli.encode(cli.Request(command=["/bin/cat"], launch_directory="/usr")))
        testing.process(20)
        self.assertEqual(len(self.application.windows), 1)
        self.assertEqual(len(window.tabs), 2)
        self.assertEqual(self.sessions.launches[1].argv, ("/bin/cat",))
        self.assertEqual(self.sessions.launches[1].directory, "/usr")
        self.application.handle_arguments(cli.encode(cli.Request(new_window=True)))
        testing.process(20)
        self.assertEqual(len(self.application.windows), 2)
        self.keep(self.application.windows[1])
        self.assertTrue(self.application.windows[1].isVisible())
        self.application.handle_arguments(["garbage", "--boswas-request={broken"])
        testing.process(20)
        self.assertEqual(sum(len(each.tabs) for each in self.application.windows), 4)

    def test_new_window_shortcut_and_closing_one_of_two(self):
        window = self.window()
        window.current_pane().session.directory = "/usr"
        window.trigger("Ctrl+Shift+N")
        testing.process(20)
        self.assertEqual(len(self.application.windows), 2)
        other = self.application.windows[1]
        self.keep(other)
        self.assertEqual(self.sessions.launches[1].directory, "/usr")
        window.close()
        testing.process(20)
        self.assertEqual(self.application.windows, [other])
        self.application.handle_arguments(cli.encode(cli.Request()))
        testing.process(20)
        self.assertEqual(len(other.tabs), 2)

    def test_single_instance_passes_the_request_on(self):
        self.first = fluent.SingleInstance(APP_ID)
        self.second = fluent.SingleInstance(APP_ID)
        received = []
        self.first.message_received.connect(received.append)
        self.assertTrue(self.first.start([]))
        try:
            request = cli.Request(title="t", command=["vim", "-platform", "x"], launch_directory="/tmp")
            done = []
            QTimer.singleShot(0, lambda: done.append(self.second.start(cli.encode(request))))
            self.assertTrue(testing.wait_until(lambda: bool(done), 10))
            self.assertEqual(done, [False])
            self.assertTrue(testing.wait_until(lambda: bool(received), 5))
            self.assertEqual(cli.decode(received[0]), request)
        finally:
            self.first.close()
            testing.process(20)


class Output(WindowCase):
    def test_a_flood_is_taken_in_slices_and_nothing_is_lost(self):
        window = self.window()
        pane = window.current_pane()
        pane.view.term.set_scrollback(100000)
        lines = 40000
        data = b"".join(b"line %d\r\n" % number for number in range(lines))
        chunk = 1 << 16
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        for start in range(0, len(data), chunk):
            pane.session.play(data[start:start + chunk])
        self.assertGreater(pane.pending_bytes(), 0)                 # not all at once
        testing.type_text(pane.view, "q")                           # typing is not held up
        self.assertEqual(bytes(pane.session.written), b"q")
        self.assertTrue(testing.wait_until(lambda: pane.pending_bytes() == 0, 60))
        timer.stop()
        pane.view.flush()
        term = pane.view.term
        text = [term.line_text(index) for index in range(term.total_lines())]
        self.assertEqual([line for line in text if line], [f"line {number}" for number in range(lines)])
        gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:])]
        self.assertTrue(gaps)
        self.assertLess(max(gaps), 0.25, "the event loop was held up")

    def test_reading_pauses_when_too_much_is_waiting(self):
        window = self.window()
        pane = window.current_pane()
        block = b"x" * 65536
        while pane.pending_bytes() <= HIGH_WATER:
            pane._pending.append(block)
            pane._pending_bytes += len(block)
        pane._output(block)
        self.assertFalse(pane.session.reading)
        self.assertTrue(testing.wait_until(lambda: pane.session.reading, 60))
        self.assertTrue(testing.wait_until(lambda: pane.pending_bytes() == 0, 60))

    def test_an_error_in_the_emulator_does_not_end_the_session(self):
        window = self.window()
        pane = window.current_pane()
        with mock.patch.object(pane.view.term, "feed", side_effect=RuntimeError("bug")), \
                mock.patch("traceback.print_exc"):
            pane.session.play("boom")
            testing.process()
        self.play(window, "still alive")
        self.assertIn("still alive", pane.view.term.screen_text()[0])


class Pasting(WindowCase):
    def test_one_line_is_pasted_without_a_question(self):
        window = self.window()
        pane = window.current_pane()
        pane.confirm_paste = lambda text: self.fail("asked")
        pane.view.paste_text("echo one\n")
        self.assertEqual(bytes(pane.session.written), b"echo one\r")

    def test_several_lines_ask(self):
        window = self.window()
        pane = window.current_pane()
        asked = []
        pane.confirm_paste = lambda text: (asked.append(text), False)[1]
        pane.view.paste_text("rm -rf a\nrm -rf b\n")
        self.assertEqual((len(asked), bytes(pane.session.written)), (1, b""))
        self.store.settings.paste_warning = False
        pane.view.paste_text("a\nb")
        self.assertEqual((len(asked), bytes(pane.session.written)), (1, b"a\rb"))

    def test_the_dialog(self):
        window = self.window()
        pane = window.current_pane()
        seen = []

        def answer(confirm, remember):
            dialog = window.findChild(fluent.ContentDialog)
            seen.append(dialog.text_label.text())
            from PySide6.QtWidgets import QCheckBox
            dialog.findChild(QCheckBox).setChecked(remember)
            (dialog.primary_button if confirm else dialog.close_button).click()

        QTimer.singleShot(50, lambda: answer(False, True))
        pane.view.paste_text("a\nb\nc")
        self.assertEqual(bytes(pane.session.written), b"")
        self.assertIn("3 lines", seen[0])
        self.assertTrue(self.store.settings.paste_warning)          # cancelling does not change the setting
        QTimer.singleShot(50, lambda: answer(True, True))
        pane.view.paste_text("a\nb\nc")
        self.assertEqual(bytes(pane.session.written), b"a\rb\rc")
        self.assertFalse(self.store.settings.paste_warning)
        pane.view.paste_text("d\ne")
        self.assertEqual(bytes(pane.session.written), b"a\rb\rcd\re")

    def test_clipboard_request_without_permission_is_explained_once(self):
        window = self.window()
        pane = self.play(window, "\x1b]52;c;aGk=\x07")
        self.assertTrue(pane.info.isVisible())
        self.assertIn("clipboard", pane.info.message())
        pane.info.action_button.click()
        testing.process(20)
        self.assertIsInstance(window.current_widget(), SettingsPage)


class Finding(WindowCase):
    TEXT = "".join(f"row {number} {'needle' if number % 25 == 0 else 'hay'}\r\n" for number in range(100)) + "Needle"

    def open(self):
        window = self.window()
        pane = self.play(window, self.TEXT)
        window.trigger("Ctrl+Shift+F")
        bar = pane.find_bar
        self.assertTrue(bar.isVisible())
        self.assertTrue(bar.box.hasFocus())
        self.assertTrue(pane.rect().contains(bar.geometry()))
        return window, pane, bar

    def test_find_next_and_previous(self):
        _window, pane, bar = self.open()
        testing.type_text(bar.box, "needle")
        bar.finish()
        self.assertEqual(len(bar.matches()), 5)
        self.assertEqual(bar.count_label.text(), "5 of 5")           # the newest match first
        self.assertEqual(pane.view.scroll_offset(), 0)
        testing.key(bar.box, Qt.Key.Key_Return)
        self.assertEqual(bar.count_label.text(), "4 of 5")
        line = bar.matches()[3][0][0]
        self.assertEqual(pane.view.term.line_text(line), "row 75 needle")
        first, last = pane.view._first, pane.view._first + pane.view.grid_size()[1]
        self.assertTrue(first <= line < last, "the match was not scrolled into view")
        testing.click(bar.next_button)
        self.assertEqual(bar.count_label.text(), "5 of 5")
        testing.click(bar.next_button)
        self.assertEqual(bar.count_label.text(), "1 of 5")           # around
        testing.key(bar, Qt.Key.Key_Return, SHIFT)
        self.assertEqual(bar.count_label.text(), "2 of 5")

    def test_match_case_and_no_results(self):
        _window, _pane, bar = self.open()
        testing.type_text(bar.box, "Needle")
        bar.finish()
        self.assertEqual(len(bar.matches()), 5)
        testing.click(bar.case_button)
        bar.finish()
        self.assertEqual(bar.count_label.text(), "1 of 1")
        bar.box.setText("absent")
        bar.finish()
        self.assertEqual(bar.count_label.text(), "No results")
        self.assertFalse(bar.next_button.isEnabled())

    def test_highlighting(self):
        _window, pane, bar = self.open()
        testing.type_text(bar.box, "hay")
        bar.finish()
        self.assertGreater(len(pane.view._matches), 10)
        testing.click(bar.all_button)
        self.assertEqual(len(pane.view._matches), 1)                 # only the current match
        testing.key(bar.box, Qt.Key.Key_Escape)                     # clears the text
        testing.key(bar.box, Qt.Key.Key_Escape)                     # closes the bar
        self.assertFalse(bar.isVisible())
        self.assertEqual(pane.view._matches, {})
        self.assertTrue(pane.view.hasFocus())

    def test_new_output_is_searched(self):
        window, pane, bar = self.open()
        testing.type_text(bar.box, "needle")
        bar.finish()
        self.play(window, "\r\nanother needle")
        self.assertTrue(testing.wait_until(lambda: len(bar.matches()) == 6, 5))

    def test_selection_is_offered_as_the_text(self):
        window = self.window()
        pane = self.play(window, "pick this word")
        drag(pane.view, cell_point(pane.view, 5, 0), cell_point(pane.view, 8, 0))
        window.find()
        self.assertEqual(pane.find_bar.box.text(), "this")


class Settings(WindowCase):
    def page(self):
        window = self.window()
        window.trigger("Ctrl+,")
        testing.process(20)
        page = window.current_widget()
        self.assertIsInstance(page, SettingsPage)
        self.assertEqual(window.strip.title(window.strip.current()), "Settings")
        return window, page

    def saved(self):
        self.store.save_now()
        return json.loads((fluent.config_dir(APP_ID) / "settings.json").read_text(encoding="utf-8"))

    def test_one_settings_tab(self):
        window, page = self.page()
        window.select_tab(0)
        window.open_settings()
        self.assertEqual(len(window.tabs), 2)
        self.assertIs(window.current_widget(), page)
        window.close_current_tab()
        self.assertEqual(len(window.tabs), 1)
        self.assertIsNone(window.settings_page)

    def test_switches_change_and_save(self):
        window, page = self.page()
        for key in ("copy_on_select", "clipboard_write"):
            self.assertFalse(getattr(self.store.settings, key))
            testing.click(page.controls[key])
            self.assertTrue(getattr(self.store.settings, key))
        testing.click(page.controls["paste_warning"])
        saved = self.saved()
        self.assertEqual((saved["copy_on_select"], saved["clipboard_write"], saved["paste_warning"]), (True, True, False))
        view = window.tabs[0].view
        self.assertTrue(view.copy_on_select)
        self.assertTrue(view.term.allow_clipboard_write)

    def test_settings_are_saved_off_the_gui_thread_after_a_moment(self):
        _window, page = self.page()
        testing.click(page.controls["ctrl_c_copies"])
        path = fluent.config_dir(APP_ID) / "settings.json"
        self.assertTrue(testing.wait_until(path.is_file, 10))
        self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["ctrl_c_copies"])

    def test_profile_changes_reach_open_tabs(self):
        window, page = self.page()
        key = self.store.settings.default_profile
        view = window.tabs[0].view
        cell = view.cell_size()
        page.controls[f"{key}.font_size"].setValue(16)
        self.assertEqual(view.font_points(), 16)
        self.assertGreater(view.cell_size()[0], cell[0])
        scheme = page.controls[f"{key}.scheme"]
        scheme.setCurrentIndex(scheme.findData("solarized-light"))
        self.assertEqual(view.scheme().key, "solarized-light")
        self.assertEqual(window.strip._tabs[0].background, view.background())
        shape = page.controls[f"{key}.cursor_shape"]
        shape.setCurrentIndex(shape.findData("block"))
        self.assertEqual(view._cursor_shape, "block")
        testing.click(page.controls[f"{key}.cursor_blink"])
        self.assertFalse(view._cursor_blinks)
        page.controls[f"{key}.scrollback"].setValue(123)
        self.assertEqual(view.term.history.max_lines, 123)
        name = page.controls[f"{key}.name"]
        name.setText("Main shell")
        name.editingFinished.emit()
        self.assertEqual(window.strip.title(0), "Main shell")
        self.assertEqual(page.default_box.currentText(), "Main shell")
        command = page.controls[f"{key}.command"]
        command.setText("/bin/cat -n")
        command.editingFinished.emit()
        saved = self.saved()["profiles"][0]
        self.assertEqual((saved["font_size"], saved["scheme"], saved["cursor_shape"], saved["cursor_blink"]),
                         (16.0, "solarized-light", "block", False))
        self.assertEqual((saved["scrollback"], saved["name"], saved["command"]), (123, "Main shell", "/bin/cat -n"))
        window.select_tab(0)
        window.new_tab()
        testing.process(20)
        self.assertEqual(self.sessions.launches[-1].argv, ("/bin/cat", "-n"))

    def test_add_duplicate_delete_and_default(self):
        window, page = self.page()
        first = self.store.settings.default_profile
        testing.click(page.add_button)
        self.assertEqual([profile.name for profile in self.store.settings.profiles][1], "New profile")
        added = self.store.settings.profiles[1].id
        self.assertTrue(page.profile_cards[added].is_expanded())
        testing.click(page.controls[f"{added}.duplicate"])
        self.assertEqual([profile.name for profile in self.store.settings.profiles],
                         [self.store.settings.profiles[0].name, "New profile", "New profile 2"])
        page.default_box.setCurrentIndex(1)
        self.assertEqual(self.store.settings.default_profile, added)
        window.select_tab(0)
        pane = window.new_tab()
        testing.process(20)
        self.assertEqual(pane.profile_id, added)
        answers = [False, True]
        with mock.patch.object(fluent, "ask", side_effect=lambda *args, **more: answers.pop(0)) as asked, \
                mock.patch("boswas_apps.terminal.settings_page.fluent.ask", asked):
            page.delete_profile(added)
            self.assertEqual(len(self.store.settings.profiles), 3)
            page.delete_profile(added)
        self.assertEqual(len(self.store.settings.profiles), 2)
        self.assertEqual(self.store.settings.default_profile, first)
        self.assertEqual(pane.profile().id, first)                 # its profile is gone: the default one
        self.assertNotIn(added, page.profile_cards)
        self.assertEqual(len(self.saved()["profiles"]), 2)

    def test_the_last_profile_cannot_be_deleted(self):
        _window, page = self.page()
        key = self.store.settings.default_profile
        self.assertFalse(page.controls[f"{key}.delete"].isEnabled())
        page.delete_profile(key)
        self.assertEqual(len(self.store.settings.profiles), 1)

    def test_damaged_settings_are_reported_and_not_overwritten_unasked(self):
        path = fluent.config_dir(APP_ID) / "settings.json"
        path.write_text("{broken", encoding="utf-8")
        self.store = app_module.SettingsStore()
        self.application = app_module.TerminalApp(session_factory=self.sessions, store=self.store)
        _window, page = self.page()
        self.assertTrue(page.message.isVisible())
        self.assertIn("damaged", page.message.message())
        self.store.save_now()
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_save_failure_is_shown(self):
        _window, page = self.page()
        with mock.patch.object(settings_module, "save", side_effect=OSError(28, "No space left on device")):
            testing.click(page.controls["copy_on_select"])
            self.store.save_now()
        self.assertTrue(page.message.isVisible())
        self.assertIn("No space left on device", page.message.message())


class RealChild(testing.FluentTestCase):
    """PtySession with the Qt event loop and real programs."""

    def collect(self, child):
        self.output = bytearray()
        self.ended = []
        child.output.connect(self.output.extend)
        child.finished.connect(lambda status, number: self.ended.append((status, number)))

    def test_output_and_exit(self):
        child = PtySession(["/bin/sh", "-c", "printf hello; exit 3"])
        self.collect(child)
        self.assertTrue(testing.wait_until(lambda: bool(self.ended), 10))
        self.assertEqual((bytes(self.output), self.ended), (b"hello", [(3, 0)]))
        self.assertFalse(child.is_running())
        self.assertFalse(os.path.exists(f"/proc/{child.pid}"))
        child.close()

    def test_echo_and_resize(self):
        child = PtySession(["/bin/cat"], cols=80, rows=24)
        self.collect(child)
        child.write(b"ping\n")
        self.assertTrue(testing.wait_until(lambda: self.output.count(b"ping") == 2, 10))
        child.close()
        self.assertTrue(testing.wait_until(lambda: session.children_waiting() == 0, 10))
        self.assertFalse(os.path.exists(f"/proc/{child.pid}"))
        self.assertEqual(self.ended, [])                    # closed by us: no report

    def test_large_paste_is_queued_not_blocked(self):
        child = PtySession(["/bin/sh", "-c", "stty raw -echo; exec wc -c"])
        self.collect(child)
        testing.process(200)
        data = b"x" * 300000
        started = time.monotonic()
        child.write(data)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertGreater(child.pending_output(), 0)
        self.assertTrue(testing.wait_until(lambda: child.pending_output() == 0, 30))
        child.close()
        self.assertTrue(testing.wait_until(lambda: session.children_waiting() == 0, 10))

    def test_a_shell_in_a_window_stays_responsive_during_a_flood(self):
        store = app_module.SettingsStore()
        application = app_module.TerminalApp(session_factory=start_session, store=store)
        store.settings.confirm_close = False
        window = application.open(cli.Request(command=["/bin/sh", "-c", "echo ready; read line; yes 'flooding the terminal'"]))
        self.keep(window)
        window.resize(800, 500)
        window.show()
        pane = window.current_pane()
        self.assertTrue(testing.wait_until(lambda: pane.state == "running", 10))
        self.assertTrue(testing.wait_until(lambda: "ready" in pane.view.term.screen_text()[0], 10))
        self.assertEqual(pane.session.foreground_process(), None)
        pid = pane.session.pid
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        testing.key(pane.view, Qt.Key.Key_Return, text="\r")
        testing.process(1500)
        timer.stop()
        gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:])]
        self.assertGreater(len(pane.view.term.history), 1000)
        self.assertLess(max(gaps), 0.25, "the event loop was held up by the flood")
        self.assertLess(pane.pending_bytes(), 8 << 20)
        window.close()
        testing.process(50)
        self.assertTrue(testing.wait_until(lambda: session.children_waiting() == 0, 10))
        self.assertFalse(os.path.exists(f"/proc/{pid}"))


class Main(unittest.TestCase):
    def test_usage_help_version_root(self):
        import contextlib
        import io
        errors, output = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(output):
            self.assertEqual(app_module.main(["boswas-terminal", "--nonsense"]), 2)
            self.assertEqual(app_module.main(["boswas-terminal", "--help"]), 0)
            self.assertEqual(app_module.main(["boswas-terminal", "--version"]), 0)
            with mock.patch("os.geteuid", return_value=0):
                self.assertEqual(app_module.main(["boswas-terminal"]), 4)
        self.assertIn("unknown option", errors.getvalue())
        self.assertIn("never as root", errors.getvalue())
        self.assertIn("Usage: boswas-terminal", output.getvalue())
        self.assertIn("boswas-terminal 1.0", output.getvalue())


class WholeProgram(testing.FluentTestCase):
    def test_main_runs_a_command_until_the_window_closes(self):
        seen = []

        def look():
            windows = [widget for widget in QApplication.topLevelWidgets() if hasattr(widget, "current_pane")
                       and widget.isVisible()]
            if not windows:
                return
            pane = windows[0].current_pane()
            if pane is not None and "from main" in "\n".join(pane.view.term.screen_text()):
                seen.append((pane.title(), QGuiApplication.desktopFileName(), pane.session.pid))
                windows[0].store.settings.confirm_close = False
                windows[0].close()
                timer.stop()

        timer = QTimer()
        timer.setInterval(20)
        timer.timeout.connect(look)
        timer.start()
        QTimer.singleShot(20000, QApplication.quit)
        with mock.patch("os.geteuid", return_value=1000), mock.patch("os.chdir"):
            status = app_module.main(["boswas-terminal", "--separate", "--title", "T", "-e", "/bin/sh", "-c",
                                      "echo from main; sleep 30"])
        timer.stop()
        self.assertEqual(status, 0)
        self.assertEqual(seen[0][:2], ("T", APP_ID))
        self.assertTrue(testing.wait_until(lambda: session.children_waiting() == 0, 10))
        self.assertFalse(os.path.exists(f"/proc/{seen[0][2]}"))


if __name__ == "__main__":
    unittest.main()
