"""The window of Task Manager, driven through its widgets on a fake computer (offscreen, no D-Bus, no real /proc)."""

from __future__ import annotations

import json
import re
import signal
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boswas_fluent import testing  # noqa: E402  (selects the offscreen platform)

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLineEdit  # noqa: E402

import boswas_fluent as fluent  # noqa: E402

import tm_fixtures as fx  # noqa: E402
import tm_ui  # noqa: E402

from boswas_apps.task_manager import APP_ID, PAGES, settings as settings_module  # noqa: E402
from boswas_apps.task_manager.__main__ import main  # noqa: E402
from boswas_apps.task_manager.bus import BusError  # noqa: E402
from boswas_apps.task_manager.ui import table  # noqa: E402
from boswas_apps.task_manager.ui.dialogs import AddStartupDialog, PropertiesDialog, RunDialog  # noqa: E402
from boswas_apps.task_manager.ui.pages import SessionRow  # noqa: E402
from boswas_apps.task_manager.ui.sampling import Sampling  # noqa: E402
from boswas_apps.task_manager.ui.window import TaskManagerWindow, page_argument  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[2]
MB = 1048576


class UiCase(testing.FluentTestCase):
    def setUp(self):
        super().setUp()
        root = self.tmp / "root"
        self.system = fx.FakeSystem(root)
        tm_ui.populate(self.system)
        self.env = tm_ui.write_desktop_files(root)
        self.backend = tm_ui.FakeBackend(self.system, self.env, self.tmp / "conf" / "settings.json")
        self.questions: list[tuple[str, str, str]] = []
        self.answer = True

    def window(self, page: str | None = None) -> TaskManagerWindow:
        window = TaskManagerWindow(["--page", page] if page else [], self.backend)
        window.ask = self._ask
        self.show(window)
        window.resize(1180, 760)
        testing.process()
        return window

    def _ask(self, title, text, confirm="OK", cancel="Cancel", **_options) -> bool:
        self.questions.append((title, text, confirm))
        return self.answer

    def tick(self, window, busy: int = 0, idle: int = 400, seconds: float = 1.0) -> None:
        self.system.burn(busy, idle)
        self.backend.clock.advance(seconds)
        window.sampling.request()
        testing.process()

    @staticmethod
    def press(page, action) -> None:
        button = page.bar.button(action)
        assert button is not None and button.isEnabled(), action.text()
        testing.click(button)

    @staticmethod
    def choose(menu, *path: str):
        """Trigger the entry of a menu (or of a submenu, given as further names)."""
        for depth, name in enumerate(path):
            action = next((a for a in menu.actions() if a.text().split("  ")[0] == name), None)
            assert action is not None, f"no menu entry {name!r} in {[a.text() for a in menu.actions()]}"
            if depth < len(path) - 1:
                menu = action.menu()
            else:
                assert action.isEnabled(), name
                action.trigger()
        testing.process()

    @staticmethod
    def names(model) -> list[str]:
        return [model.data(model.index(row, 0), Qt.ItemDataRole.DisplayRole) for row in range(model.rowCount())]

    @staticmethod
    def select(page, key: str) -> None:
        assert page.view.select_key(key), key
        testing.process()


class ProcessesTest(UiCase):
    def test_three_groups_with_applications_collapsed_to_one_row(self):
        window = self.window()
        page = window.pages["processes"]
        self.assertEqual(window.navigation.current(), "processes")
        self.assertEqual(page.section_counts(), {"Apps": 3, "Background processes": 5, "System processes": 4})
        names = self.names(page.model)
        self.assertEqual(names[:4], ["Apps (3)", "Firefox (3)", "Task Manager", "Terminal (3)"])
        self.assertEqual(names[4:10], ["Background processes (5)", "bash", "boswas-compositor", "boswas-shell",
                                       "pipewire", "stubborn"])
        self.assertEqual(names[10:], ["System processes (4)", "cupsd", "Kernel threads (3)", "sshd", "systemd"])
        children = [row.data.name for row in page.model.child_rows("g600.6000")]
        self.assertEqual(children, ["firefox-esr", "Web Content", "Web Content"])
        self.assertEqual([row.data.name for row in page.model.child_rows("g700.7000")], ["bash", "boswas-terminal", "vim"])
        self.assertEqual(page.model.child_rows("g500.5000"), [])              # one process: nothing to open

    def test_values_are_summed_and_coloured(self):
        window = self.window()
        page = window.pages["processes"]
        self.system.use_cpu(600, 60)
        self.system.use_cpu(601, 40)
        self.system.update(601, io=(3 * MB, 1 * MB))
        self.tick(window, busy=100, idle=300)
        model = page.model
        self.assertEqual(model.text("g600.6000", "cpu"), "25%")
        self.assertEqual(model.text("g600.6000", "memory"), f"{(60000 + 30000 + 20000) * 4096 / MB:,.1f} MB")
        self.assertEqual(model.text("g600.6000", "disk"), "4.0 MB/s")
        self.assertEqual(model.text("p601.6100", "cpu"), "10%")
        self.assertEqual(model.text("g2.100", "disk"), "")                    # kernel threads: not readable
        index = model.index_of_key("g600.6000")
        heat = [model.data(index.siblingAtColumn(column), table.HEAT_ROLE) for column in range(5)]
        self.assertEqual(heat[:2], [None, None])
        self.assertAlmostEqual(heat[2], 0.25)
        self.assertAlmostEqual(heat[3], 110000 * 4096 / (16000000 * 1024))
        self.assertGreater(heat[4], 0.5)
        self.assertIsNone(model.data(model.index(0, 2), table.HEAT_ROLE))     # the group heading has none
        header = Qt.Orientation.Horizontal
        self.assertEqual(model.headerData(2, header, table.METRIC_ROLE), "25%")
        self.assertEqual(model.headerData(3, header, table.METRIC_ROLE), "38%")
        self.assertEqual(model.headerData(4, header, table.METRIC_ROLE), "0%")
        self.assertIsNone(model.headerData(0, header, table.METRIC_ROLE))
        self.assertLess(table.heat_colour(0.0).alphaF(), table.heat_colour(0.5).alphaF())
        self.assertLessEqual(table.heat_colour(5.0).alphaF(), 0.6)

    def test_the_table_is_updated_in_place(self):
        window = self.window()
        page = window.pages["processes"]
        model, view = page.model, page.view
        events = {"reset": 0, "inserted": [], "removed": [], "layout": 0, "changed": 0}
        model.modelReset.connect(lambda: events.__setitem__("reset", events["reset"] + 1))
        model.rowsInserted.connect(lambda parent, first, last: events["inserted"].append((parent.isValid(), first, last)))
        model.rowsRemoved.connect(lambda parent, first, last: events["removed"].append((parent.isValid(), first, last)))
        model.layoutChanged.connect(lambda *args: events.__setitem__("layout", events["layout"] + 1))
        model.dataChanged.connect(lambda *args: events.__setitem__("changed", events["changed"] + 1))
        view.expand(model.index_of_key("g700.7000"))
        self.select(page, "p702.7200")
        view.verticalScrollBar().setValue(40)
        scroll = view.verticalScrollBar().value()
        for _ in range(3):                                   # nothing happens on the machine: nothing is emitted
            self.tick(window)
        self.assertEqual(events, {"reset": 0, "inserted": [], "removed": [], "layout": 0, "changed": 0})
        self.system.use_cpu(702, 20)
        self.tick(window, busy=20, idle=380)
        self.assertEqual(events["reset"], 0)
        self.assertGreater(events["changed"], 0)
        self.assertEqual(view.current_row().data.pid, 702)
        self.assertTrue(view.isExpanded(model.index_of_key("g700.7000")))
        self.assertEqual(view.verticalScrollBar().value(), scroll)
        self.system.add_process(990, "newcomer", ppid=410, start=9900)
        self.system.remove_process(800)
        self.tick(window)
        self.assertEqual((events["reset"], len(events["inserted"]), len(events["removed"])), (0, 1, 1))
        self.assertIn("newcomer", self.names(model))
        self.assertNotIn("pipewire", self.names(model))
        self.assertEqual(view.current_row().data.pid, 702)
        self.assertEqual(model.resets, 0)

    def test_sort_by_a_column_keeps_the_groups(self):
        window = self.window()
        page = window.pages["processes"]
        self.system.use_cpu(702, 30)
        self.system.use_cpu(500, 10)
        self.tick(window, busy=40, idle=360)
        header = page.view.header()
        position = QPoint(header.sectionPosition(2) + header.sectionSize(2) // 2, header.height() // 2)
        testing.click(header.viewport(), position)
        self.assertEqual(page.model.sort_key(), ("cpu", True))            # numbers: the largest first
        self.assertEqual(self.names(page.model)[:4], ["Apps (3)", "Terminal (3)", "Task Manager", "Firefox (3)"])
        self.assertEqual((header.sortIndicatorSection(), header.sortIndicatorOrder()), (2, Qt.SortOrder.DescendingOrder))
        testing.click(header.viewport(), position)
        self.assertEqual(page.model.sort_key(), ("cpu", False))
        self.assertEqual(self.names(page.model)[:4], ["Apps (3)", "Firefox (3)", "Task Manager", "Terminal (3)"])
        self.assertEqual(self.names(page.model)[4], "Background processes (5)")
        page.model.set_sort("memory", True)
        self.assertEqual(self.names(page.model)[1:4], ["Firefox (3)", "Task Manager", "Terminal (3)"])
        self.assertEqual([row.data.pid for row in page.model.child_rows("g600.6000")], [600, 601, 602])

    def test_filter_box(self):
        window = self.window()
        page = window.pages["processes"]
        testing.type_text(window.search, "vim")
        self.assertEqual(self.names(page.model), ["Apps (1)", "Terminal (3)"])     # found by a process inside it
        window.search.clear()
        testing.type_text(window.search, "950")
        self.assertEqual(self.names(page.model), ["Background processes (1)", "bash"])
        window.search.clear()
        testing.type_text(window.search, "root")
        self.assertEqual(self.names(page.model)[0], "System processes (4)")
        window.search.clear()
        testing.type_text(window.search, "zzz")
        self.assertIs(page.stack.currentWidget(), page.empty)
        self.assertEqual(page.empty.title_label.text(), "Nothing matches")
        testing.key(window.search, Qt.Key.Key_Escape)
        self.assertEqual(window.search.text(), "")
        self.assertIs(page.stack.currentWidget(), page.view)
        self.assertEqual(len(self.names(page.model)), 15)

    def test_end_task_asks_politely_and_the_row_goes_away(self):
        window = self.window()
        page = window.pages["processes"]
        self.assertFalse(page.bar.button(page.end_action).isEnabled())
        self.select(page, "g600.6000")
        self.press(page, page.end_action)
        self.assertEqual(self.backend.signals, [(600, signal.SIGTERM)])
        self.assertEqual(self.questions, [])
        self.system.remove_process(601)
        self.system.remove_process(602)
        self.tick(window)
        self.assertNotIn("Firefox (3)", self.names(page.model))
        self.assertIsNone(page.view.current_row())                # not a neighbour of the row that went
        self.assertFalse(page.bar.button(page.end_action).isEnabled())
        window.check_ended()
        self.assertTrue(window.banner.isHidden())

    def test_a_task_that_does_not_end_can_be_forced(self):
        window = self.window()
        page = window.pages["processes"]
        self.select(page, "g900.8000")
        testing.key(page.view, Qt.Key.Key_Delete)
        self.assertEqual(self.backend.signals, [(900, signal.SIGTERM)])
        self.assertTrue(window._grace.isActive())
        self.assertEqual(window._grace.interval(), window.END_GRACE_MS)
        window.check_ended()
        self.assertFalse(window.banner.isHidden())
        self.assertEqual(window.banner.severity(), "warning")
        self.assertIn("stubborn did not end when asked to", window.banner.message())
        self.assertEqual(window.banner.action_button.text(), "Force")
        testing.click(window.banner.action_button)
        self.assertEqual(self.backend.signals, [(900, signal.SIGTERM), (900, signal.SIGKILL)])
        self.assertTrue(window.banner.isHidden())
        self.tick(window)
        self.assertNotIn("stubborn", self.names(page.model))

    def test_refusal_of_the_system_is_shown_in_plain_words(self):
        window = self.window()
        page = window.pages["processes"]
        self.select(page, "g300.100")
        self.press(page, page.end_action)
        self.assertEqual(self.backend.signals, [])
        self.assertEqual(window.banner.severity(), "error")
        self.assertEqual(window.banner.message(), "The system refused to signal sshd (PID 300): it belongs to another "
                                                  "user or to the system, and you are not allowed to change it.")
        self.assertFalse(window._grace.isActive())

    def test_ending_what_task_manager_depends_on_asks_first(self):
        window = self.window()
        page = window.pages["processes"]
        self.answer = False
        self.select(page, "g410.100")                             # the shell that started Task Manager
        self.press(page, page.end_action)
        self.assertEqual(len(self.questions), 1)
        self.assertIn("may end your whole desktop session", self.questions[0][1])
        self.assertEqual(self.backend.signals, [])
        self.answer = True
        self.select(page, "g500.5000")                            # Task Manager itself
        self.press(page, page.end_action)
        self.assertEqual(len(self.questions), 2)
        self.assertEqual(self.backend.signals, [(500, signal.SIGTERM)])

    def test_context_menu(self):
        window = self.window()
        page = window.pages["processes"]
        self.select(page, "g700.7000")
        menu = page.context_menu()
        self.assertEqual([action.text() for action in menu.actions() if not action.isSeparator()],
                         ["Expand", "End task", "End process tree", "Go to details", "Open file location", "Properties"])
        self.choose(menu, "Expand")
        self.assertTrue(page.view.isExpanded(page.model.index_of_key("g700.7000")))
        self.assertEqual(page.context_menu().actions()[0].text(), "Collapse")
        self.choose(page.context_menu(), "End process tree")
        self.assertEqual(self.backend.signals, [(702, signal.SIGTERM), (701, signal.SIGTERM), (700, signal.SIGTERM)])
        self.select(page, "g500.5000")
        self.assertEqual(page.context_menu().actions()[0].text(), "End task")       # nothing to expand
        page.view.setCurrentIndex(page.model.index(0, 0))                           # a group heading
        self.assertIsNone(page.context_menu())

    def test_context_menu_opens_at_the_row_from_the_keyboard(self):
        window = self.window()
        page = window.pages["processes"]
        self.select(page, "g800.4100")
        page._context(page.view.viewport().mapToGlobal(QPoint(40, 40)))
        testing.process()
        self.assertTrue(page._menu.isVisible())
        page._menu.close()

    def test_go_to_details_open_location_and_properties(self):
        window = self.window()
        page = window.pages["processes"]
        self.select(page, "g500.5000")
        self.choose(page.context_menu(), "Open file location")
        self.assertEqual(self.backend.shown, [sys.executable])
        self.select(page, "g900.8000")
        self.choose(page.context_menu(), "Open file location")       # /opt/stubborn/stubborn does not exist
        self.assertEqual(window.banner.severity(), "error")
        self.assertIn("no longer exists", window.banner.message())
        self.select(page, "g2.100")
        self.choose(page.context_menu(), "Open file location")
        self.assertIn("kernel threads have none", window.banner.message())
        self.assertEqual(len(self.backend.shown), 1)
        self.select(page, "g700.7000")
        self.choose(page.context_menu(), "Properties")
        dialog = window.dialog
        self.assertIsInstance(dialog, PropertiesDialog)
        self.assertEqual(dialog.title_label.text(), "boswas-terminal properties")
        self.assertEqual(dialog.values["Process ID"], "700")
        self.assertEqual(dialog.values["Executable"], "/usr/bin/python3.13")
        self.assertEqual(dialog.values["Working folder"], "/home/alice")
        self.assertEqual(dialog.values["Open files"], "3")
        self.assertEqual(dialog.values["Max open files"], "1024 files / 524288 files")
        self.assertEqual(dialog.values["Max cpu time"], "unlimited / unlimited")
        self.assertEqual(dialog.values["Command line"], "/usr/bin/python3 -IB /usr/bin/boswas-terminal")
        self.assertFalse(any("environ" in key.lower() for key in dialog.values))
        testing.grab(dialog, self.tmp / "properties.png")
        testing.click(dialog.close_button)
        self.assertIsNone(window.dialog)
        self.select(page, "g600.6000")
        self.choose(page.context_menu(), "Go to details")
        details = window.pages["details"]
        self.assertEqual(window.navigation.current(), "details")
        self.assertEqual(details.view.current_row().data.pid, 600)

    def test_grouping_can_be_switched_off(self):
        window = self.window()
        page = window.pages["processes"]
        page.group_action.trigger()
        testing.process()
        self.assertEqual(self.names(page.model)[:3], ["bash", "boswas-compositor", "boswas-shell"])
        self.assertEqual(page.section_counts(), {})
        self.assertFalse(settings_module.load(self.backend.settings_path()).group_by_type)
        page.group_action.trigger()
        self.assertEqual(self.names(page.model)[0], "Apps (3)")

    def test_type_to_find(self):
        window = self.window()
        page = window.pages["processes"]
        page.view.setFocus()
        testing.type_text(page.view, "ter")
        self.assertEqual(page.view.current_row().data.name, "Terminal")
        page.view._typed_at = 0.0                               # a second later
        testing.type_text(page.view, "s")
        self.assertEqual(page.view.current_row().data.name, "stubborn")
        page.view._typed_at = 0.0
        testing.type_text(page.view, "s")
        self.assertEqual(page.view.current_row().data.name, "sshd")

    def test_applications_come_from_the_desktop_entries(self):
        window = self.window()
        page = window.pages["processes"]
        row = page.model.row_at(page.model.index_of_key("g600.6000"))
        self.assertEqual((row.data.kind, row.data.app.app_id), ("app", "firefox-esr.desktop"))
        self.assertFalse(page.model.data(page.model.index_of_key("g600.6000"), Qt.ItemDataRole.DecorationRole).isNull())
        (self.tmp / "root/usr/share/applications/firefox-esr.desktop").unlink()
        window.refresh()                                        # F5 reads the applications again
        self.tick(window)
        self.assertEqual(page.section_counts()["Apps"], 2)
        self.assertIn("firefox-esr (3)", self.names(page.model))


class DetailsTest(UiCase):
    def test_every_process_with_its_columns(self):
        window = self.window("details")
        page = window.pages["details"]
        self.assertEqual(page.model.rowCount(), len(self.system.processes))
        model = page.model
        key = "p702.7200"
        expected = {"name": "vim", "pid": "702", "status": "Sleeping", "user": "alice", "cpu": "0%",
                    "memory": f"{3000 * 4096 / MB:,.1f} MB", "threads": "1", "nice": "5",
                    "command": "vim notes with spaces.txt"}
        for column, text in expected.items():
            self.assertEqual(model.text(key, column), text, column)
        self.assertRegex(model.text(key, "started"), r"^\d\d:\d\d:\d\d$|^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertEqual(model.text("p11.100", "command"), "[kworker/0:1-events]")
        self.assertEqual(model.text("p11.100", "status"), "Idle")
        self.assertEqual(model.text("p300.100", "user"), "root")
        self.assertEqual(model.text("p500.5000", "name"), "boswas-task-manager")
        self.assertEqual([column.title for column in model.columns],
                         ["Name", "PID", "Status", "User name", "CPU", "Memory (RSS)", "PSS", "Threads", "Nice",
                          "Started", "Command line"])

    def test_pss_arrives_in_turns(self):
        window = self.window("details")
        page = window.pages["details"]
        self.assertIn("pss", window.sampling.parts())
        for _ in range(4):
            self.tick(window)
        self.assertEqual(page.model.text("p702.7200", "pss"), "0.5 MB")
        self.assertEqual(page.model.text("p11.100", "pss"), "")

    def test_sorting_and_filter(self):
        window = self.window("details")
        page = window.pages["details"]
        header = page.view.header()
        self.assertEqual(page.model.sort_key(), ("name", False))
        header.sectionClicked.emit(1)
        pids = [row.data.pid for row in page.model.top_rows()]
        self.assertEqual(pids, sorted(pids))
        header.sectionClicked.emit(5)
        sizes = [row.data.rss for row in page.model.top_rows()]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(page.model.top_rows()[0].data.pid, 600)
        testing.type_text(window.search, "bob")
        self.assertEqual([row.data.pid for row in page.model.top_rows()], [950])
        window.search.clear()
        testing.type_text(window.search, "contentproc")
        self.assertEqual(sorted(row.data.pid for row in page.model.top_rows()), [601, 602])

    def test_priority_menu(self):
        window = self.window("details")
        page = window.pages["details"]
        self.select(page, "p702.7200")
        menu = page.context_menu()
        self.assertEqual([action.text() for action in menu.actions() if not action.isSeparator()],
                         ["End task", "End process tree", "Set priority", "Send signal", "Open file location",
                          "Copy command line", "Properties"])
        priority = next(action.menu() for action in menu.actions() if action.text() == "Set priority")
        self.assertEqual([action.text() for action in priority.actions()],
                         ["Very high  (nice -20)", "High  (nice -10)", "Above normal  (nice -5)", "Normal  (nice 0)",
                          "Below normal  (nice 5)", "Low  (nice 10)", "Very low  (nice 19)"])
        self.assertEqual([action.text() for action in priority.actions() if action.isChecked()], ["Below normal  (nice 5)"])
        self.choose(menu, "Set priority", "Low")
        self.assertEqual(self.backend.priorities, [(702, 10)])
        self.assertEqual(window.banner.severity(), "success")
        self.assertEqual(page.model.text("p702.7200", "nice"), "10")
        self.choose(page.context_menu(), "Set priority", "High")
        self.assertEqual(self.backend.priorities, [(702, 10)])
        self.assertEqual(window.banner.severity(), "error")
        self.assertIn("refused to give vim (PID 702) the priority “High” (nice -10): permission denied",
                      window.banner.message())
        self.assertIn("administrator rights", window.banner.message())

    def test_send_signal_menu(self):
        window = self.window("details")
        page = window.pages["details"]
        self.select(page, "p800.4100")
        signals = next(action.menu() for action in page.context_menu().actions() if action.text() == "Send signal")
        self.assertEqual([action.text() for action in signals.actions()],
                         ["Stop (SIGSTOP)", "Continue (SIGCONT)", "Hang up (SIGHUP)", "Interrupt (SIGINT)",
                          "Terminate (SIGTERM)", "Kill (SIGKILL)"])
        self.choose(page.context_menu(), "Send signal", "Stop (SIGSTOP)")
        self.assertEqual(self.backend.signals, [(800, signal.SIGSTOP)])
        self.assertEqual(window.banner.message(), "SIGSTOP was sent to pipewire (PID 800).")
        self.tick(window)
        self.assertEqual(page.model.text("p800.4100", "status"), "Stopped")
        self.choose(page.context_menu(), "Send signal", "Kill (SIGKILL)")
        self.tick(window)
        self.assertEqual(self.backend.signals[-1], (800, signal.SIGKILL))
        self.assertFalse(page.model.index_of_key("p800.4100").isValid())

    def test_commands_copy_and_properties(self):
        window = self.window("details")
        page = window.pages["details"]
        for action in (page.end_action, page.properties_action):
            self.assertFalse(page.bar.button(action).isEnabled())
        self.select(page, "p702.7200")
        page.copy_action.trigger()
        self.assertEqual(self.backend.clipboard, "vim notes with spaces.txt")
        window.copy_selection()                                  # Ctrl+C: the row
        self.assertTrue(self.backend.clipboard.startswith("vim\t702\tSleeping\talice\t"))
        self.press(page, page.properties_action)
        self.assertIsInstance(window.dialog, PropertiesDialog)
        self.assertEqual(window.dialog.values["Priority"], "Below normal (nice 5)")
        window.dialog.reject()
        testing.key(page.view, Qt.Key.Key_Return)                # Enter on a row: its properties
        self.assertIsInstance(window.dialog, PropertiesDialog)
        window.dialog.reject()
        self.press(page, page.end_action)
        self.assertEqual(self.backend.signals, [(702, signal.SIGTERM)])
        self.select(page, "p700.7000")
        page.tree_action.trigger()
        self.assertEqual(self.backend.signals[1:], [(701, signal.SIGTERM), (700, signal.SIGTERM)])

    def test_properties_of_a_process_of_someone_else(self):
        window = self.window("details")
        page = window.pages["details"]
        self.select(page, "p11.100")
        window.properties_of_selection()                         # Alt+Enter
        values = window.dialog.values
        self.assertEqual(values["Executable"], "None: a kernel thread")
        self.assertEqual(values["Status"], "Idle")
        window.dialog.reject()

    def test_go_to_a_process_that_is_listed_only_after_the_next_sample(self):
        window = self.window("processes")
        self.system.add_process(1234, "late", ppid=410, start=12340)
        window.go_to_pid(1234)
        self.tick(window)
        self.assertEqual(window.pages["details"].view.current_row().data.pid, 1234)


class UsersTest(UiCase):
    def test_sessions_with_their_processes(self):
        window = self.window("users")
        page = window.pages["users"]
        self.assertTrue(page.note.isHidden())
        rows = page.model.top_rows()
        self.assertEqual([row.data.session.user for row in rows], ["alice", "bob"])     # not the greeter
        self.assertIsInstance(rows[0].data, SessionRow)
        model = page.model
        self.assertEqual(model.text(rows[0].key, "name"), "alice (11)")
        self.assertEqual(model.text(rows[0].key, "status"), "Active")
        self.assertEqual(model.text(rows[0].key, "place"), "seat0 · tty2")
        self.assertEqual(model.text(rows[0].key, "id"), "1")
        self.assertEqual(model.text(rows[1].key, "place"), "pts/4 · from 192.0.2.9")
        self.assertEqual(model.text(rows[1].key, "status"), "In the background")
        self.assertEqual([row.data.pid for row in model.child_rows(rows[1].key)], [950])
        pids = [row.data.pid for row in model.child_rows(rows[0].key)]
        self.assertIn(800, pids)                                 # a service of the user's own manager
        self.assertNotIn(300, pids)
        self.assertEqual(model.text("p701.7100", "place"), "pts/0")
        self.system.use_cpu(600, 40)
        self.tick(window, busy=40, idle=360)
        self.assertEqual(model.text(rows[0].key, "cpu"), "10%")
        self.assertEqual(model.headerData(model.column_index("cpu"), Qt.Orientation.Horizontal, table.METRIC_ROLE), "10%")

    def test_only_processes_can_be_ended(self):
        window = self.window("users")
        page = window.pages["users"]
        session = page.model.top_rows()[0].key
        self.select(page, session)
        self.assertFalse(page.bar.button(page.end_action).isEnabled())
        self.assertEqual([action.text() for action in page.context_menu().actions()], ["Expand"])
        self.select(page, "p900.8000")
        self.assertTrue(page.view.isExpanded(page.model.index_of_key(session)))
        self.press(page, page.end_action)
        self.assertEqual(self.backend.signals, [(900, signal.SIGTERM)])
        self.assertIn("Go to details", [action.text() for action in page.context_menu().actions()])

    def test_without_logind_the_current_user_from_proc(self):
        self.backend.sessions = tm_ui.NO_BUS
        window = self.window("users")
        page = window.pages["users"]
        rows = page.model.top_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(page.model.text(rows[0].key, "name"), "alice (11)")
        self.assertEqual(page.model.text(rows[0].key, "place"), "tty2")
        self.assertEqual(page.model.text(rows[0].key, "id"), "—")
        self.assertFalse(page.note.isHidden())
        self.assertIn("could not be asked", page.note.text())
        self.assertIn("cannot be reached", page.note.text())

    def test_filter(self):
        window = self.window("users")
        page = window.pages["users"]
        testing.type_text(window.search, "vim")
        rows = page.model.top_rows()
        self.assertEqual([row.data.session.user for row in rows], ["alice"])
        self.assertEqual([row.data.pid for row in page.model.child_rows(rows[0].key)], [702])
        window.search.clear()
        testing.type_text(window.search, "bob")
        self.assertEqual([row.data.session.user for row in page.model.top_rows()], ["bob"])


class PerformanceTest(UiCase):
    def test_list_and_cpu_page(self):
        window = self.window("performance")
        page = window.pages["performance"]
        self.system.burn(100, 0, cpu=0)
        self.tick(window, busy=0, idle=300)
        self.assertEqual(page.keys(), ["cpu", "memory", "disk:nvme0n1", "net:eth0", "gpu:card0", "battery:BAT0"])
        self.assertEqual(page.current_key, "cpu")
        self.assertEqual(page.title.text(), "CPU")
        self.assertEqual(page.heading.text(), "Example(R) Core(TM) i7-1234X CPU @ 2.80GHz")
        self.assertEqual(page.stats.value("Utilisation"), "25%")
        self.assertEqual(page.stats.value("Up time"), "0:03:25:45")
        self.assertEqual(page.stats.value("Load average"), "0.52  0.48  0.40")
        self.assertEqual(page.info.value("Logical processors:"), "4")
        self.assertEqual(page.info.value("Base speed:"), "2.80 GHz")
        block = page.blocks[0]
        self.assertEqual((block.title.text(), block.top_label.text(), block.span.text()), ("% Utilisation", "100%", "60 seconds"))
        self.assertTrue(block.isVisible())
        self.assertFalse(page.blocks[1].isVisible())
        self.assertFalse(page.legend.isVisible())
        self.assertFalse(page.composition.isVisible())
        self.assertEqual(block.graph.describe(59), "Utilisation: 25%\nNow")
        self.assertEqual(block.graph.describe(0), "Utilisation: no sample\n59 s ago")
        self.assertEqual(block.graph.accessibleName(), "% Utilisation graph")
        self.assertEqual(block.graph.accessibleDescription(), "Utilisation 25%")

    def test_per_core_view(self):
        window = self.window("performance")
        page = window.pages["performance"]
        self.system.burn(100, 0, cpu=3)
        self.tick(window, busy=0, idle=300)
        self.assertTrue(page.cpu_view.isVisible())
        self.assertFalse(page.cores.isVisible())
        page.cpu_view.set_current("cores")
        testing.process()
        self.assertTrue(page.cores.isVisible())
        self.assertFalse(page.blocks[0].isVisible())
        cells = page.cores.cells()
        self.assertEqual([number for number, _rect in cells], [0, 1, 2, 3])
        self.assertTrue(all(rect.width() > 20 and rect.height() > 20 for _number, rect in cells))
        self.assertIn("4 processors; the busiest, CPU 3, is at 57%", page.cores.accessibleDescription())
        self.assertEqual(settings_module.load(self.backend.settings_path()).cpu_view, "cores")
        testing.grab(window, self.tmp / "cores.png")
        page.cpu_view.set_current("overall")
        self.assertTrue(page.blocks[0].isVisible())

    def test_tiles_for_many_processors(self):
        window = self.window("performance")
        page = window.pages["performance"]
        page.cores.resize(600, 300)
        page.cores.set_cores([], [(number, float(number % 100)) for number in range(4096)])
        cells = page.cores.cells()
        self.assertEqual(len(cells), 4096)
        self.assertTrue(all(0 <= rect.left() and rect.right() <= 601 and rect.bottom() <= 301 for _n, rect in cells))
        page.cores.grab()

    def test_memory_disk_network_gpu_battery(self):
        window = self.window("performance")
        page = window.pages["performance"]
        self.system.set_net({"eth0": (125000, 2500)})
        self.system.set_diskstats({"nvme0n1": (10, 2048, 50, 20, 4096, 100, 300)})
        self.tick(window)
        self.assertTrue(page.select("memory"))
        testing.process()
        self.assertEqual((page.title.text(), page.heading.text()), ("Memory", "15.3 GB"))
        self.assertEqual(page.stats.value("In use"), "5.7 GB")
        self.assertEqual(page.stats.value("Swap in use"), "977 MB")
        self.assertTrue(page.composition.isVisible())
        self.assertEqual([label for label, _size, _rect in page.composition.rects()], ["In use", "Modified", "Standby", "Free"])
        self.assertAlmostEqual(sum(rect.width() for _l, _s, rect in page.composition.rects()), page.composition.width())
        self.assertEqual([entry[0] for entry in page.composition_legend.entries()], ["In use", "Modified", "Standby", "Free"])
        self.assertEqual(page.blocks[0].top_label.text(), "15.3 GB")
        page.select("disk:nvme0n1")
        testing.process()
        self.assertEqual((page.title.text(), page.heading.text()), ("Disk 0 (nvme0n1)", "Fast NVMe 512"))
        self.assertTrue(page.blocks[1].isVisible())
        self.assertEqual(page.blocks[1].title.text(), "Disk transfer rate")
        self.assertEqual(page.blocks[1].top_label.text(), "2.0 MB/s")
        self.assertEqual(page.legend.entries(), [("Read", "line", 1.0), ("Write", "dashed", 1.0)])
        self.assertEqual(page.stats.value("Active time"), "30%")
        self.assertEqual(page.stats.value("Write speed"), "2.0 MB/s")
        self.assertEqual(page.info.value("Type:"), "SSD (NVMe)")
        self.assertEqual(page.info.value("System disk:"), "Yes")
        self.assertEqual(page.blocks[1].graph.describe(59), "Read: 1.0 MB/s\nWrite: 2.0 MB/s\nNow")
        page.select("net:eth0")
        testing.process()
        self.assertEqual(page.title.text(), "Ethernet (eth0)")
        self.assertEqual(page.stats.value("Receive"), "1.0 Mbps")
        self.assertEqual(page.info.value("IPv4 address:"), "192.0.2.7/24")
        self.assertEqual(page.legend.entries(), [("Receive", "line", 1.0), ("Send", "dashed", 1.0)])
        self.assertFalse(page.blocks[1].isVisible())
        page.select("gpu:card0")
        testing.process()
        self.assertEqual((page.title.text(), page.heading.text()), ("GPU 0", "Intel Device 9a49"))
        self.assertFalse(page.blocks[0].isVisible())
        self.assertTrue(page.note.isVisible())
        self.assertIn("does not report its utilisation", page.note.message())
        self.assertEqual(page.info.value("Driver:"), "i915")
        page.select("battery:BAT0")
        testing.process()
        self.assertEqual(page.title.text(), "Battery")
        self.assertEqual(page.stats.value("Charge"), "76%")
        self.assertEqual(page.stats.value("Discharge rate"), "9.5 W")
        self.assertFalse(page.note.isVisible())
        self.tick(window)
        self.assertEqual(page.current_key, "battery:BAT0")           # the choice survives the next sample

    def test_selection_by_mouse_and_keyboard(self):
        window = self.window("performance")
        page = window.pages["performance"]
        self.tick(window)
        rect = page.list.visualItemRect(page.list.item(2))
        testing.click(page.list.viewport(), rect.center())
        self.assertEqual(page.current_key, "disk:nvme0n1")
        testing.key(page.list, Qt.Key.Key_Down)
        self.assertEqual(page.current_key, "net:eth0")
        self.assertEqual(page.list.item(3).data(Qt.ItemDataRole.AccessibleTextRole)[:15], "Ethernet (eth0)")

    def test_history_keeps_sixty_samples_and_marks_what_was_missed(self):
        window = self.window("performance")
        for _ in range(65):
            self.tick(window, busy=40, idle=360)
        values = window.history.item("cpu").graphs[0].series[0].values
        self.assertEqual(len(values), 60)
        self.assertNotIn(None, values)
        window.show_page("settings")                               # nothing is sampled here
        self.assertEqual(window.sampling.parts(), frozenset())
        self.assertFalse(window.sampling.is_active())
        self.system.burn(120, 1080)
        self.backend.clock.advance(3.0)
        window.show_page("performance")
        self.assertTrue(window.sampling.is_active())
        self.tick(window, busy=40, idle=360)
        self.assertIn(None, list(values)[-3:])
        testing.grab(window, self.tmp / "gap.png")

    def test_paused_and_slow_refresh_label_the_time_axis(self):
        window = self.window("performance")
        page = window.pages["performance"]
        window.settings.refresh = "low"
        window.settings_changed()
        self.tick(window, seconds=4.0)
        self.assertEqual(page.blocks[0].span.text(), "4 minutes")
        self.assertEqual(page.blocks[0].graph.describe(58), "Utilisation: 0%\n4 s ago")
        self.assertEqual(page.blocks[0].graph.describe(0), "Utilisation: no sample\n236 s ago")
        window.settings.refresh = "paused"
        window.settings_changed()
        self.assertEqual(page.blocks[0].span.text(), "Paused")


class StartupTest(UiCase):
    def test_entries_of_this_desktop(self):
        window = self.window("startup")
        page = window.pages["startup"]
        model = page.model
        self.assertEqual(self.names(model), ["Boswas Device Agent", "KDE Connect", "My Notes"])       # not the GNOME one
        self.assertEqual(model.text("boswas-agent.desktop", "publisher"), "Boswas")
        self.assertEqual(model.text("org.kde.kdeconnect.daemon.desktop", "publisher"), "KDE")
        self.assertEqual(model.text("org.kde.kdeconnect.daemon.desktop", "status"), "Enabled")
        self.assertEqual(model.text("org.kde.kdeconnect.daemon.desktop", "source"), "Everyone")
        self.assertEqual(model.text("notes.desktop", "status"), "Disabled")
        self.assertEqual(model.text("notes.desktop", "source"), "You")
        self.assertEqual(model.text("notes.desktop", "command"), "/home/alice/bin/notes --tray")
        self.assertEqual(window.sampling.parts(), frozenset())

    def test_disable_and_enable_write_the_users_override(self):
        window = self.window("startup")
        page = window.pages["startup"]
        for action in (page.enable_action, page.disable_action):
            self.assertFalse(page.bar.button(action).isEnabled())
        self.select(page, "org.kde.kdeconnect.daemon.desktop")
        self.assertFalse(page.bar.button(page.enable_action).isEnabled())
        self.press(page, page.disable_action)
        override = self.tmp / "root/home/.config/autostart/org.kde.kdeconnect.daemon.desktop"
        self.assertIn("\nHidden=true\n", override.read_text())
        self.assertIn("Exec=kdeconnectd", override.read_text())
        self.assertNotIn("Hidden", (self.tmp / "root/etc/xdg/autostart/org.kde.kdeconnect.daemon.desktop").read_text())
        self.assertEqual(page.model.text("org.kde.kdeconnect.daemon.desktop", "status"), "Disabled")
        self.assertEqual(window.banner.message(), "KDE Connect will no longer start when you sign in.")
        self.assertEqual(page.view.current_row().key, "org.kde.kdeconnect.daemon.desktop")      # still selected
        self.press(page, page.enable_action)
        self.assertIn("\nHidden=false\n", override.read_text())
        self.assertEqual(page.model.text("org.kde.kdeconnect.daemon.desktop", "status"), "Enabled")
        self.select(page, "notes.desktop")
        self.choose(page.context_menu(), "Enable")
        self.assertIn("Hidden=false", (self.tmp / "root/home/.config/autostart/notes.desktop").read_text())

    def test_add_from_an_installed_application(self):
        window = self.window("startup")
        page = window.pages["startup"]
        self.press(page, page.add_action)
        dialog = window.dialog
        self.assertIsInstance(dialog, AddStartupDialog)
        shown = [dialog.list.item(row).text() for row in range(dialog.list.count())]
        self.assertEqual(shown, ["Files", "Firefox", "Kate", "Task Manager", "Terminal", "Vim"])
        self.assertFalse(dialog.primary_button.isEnabled())
        testing.type_text(dialog.search, "kat")
        visible = [dialog.list.item(row).text() for row in range(dialog.list.count()) if not dialog.list.item(row).isHidden()]
        self.assertEqual(visible, ["Kate"])
        dialog.list.setCurrentRow(shown.index("Kate"))
        self.assertTrue(dialog.primary_button.isEnabled())
        testing.grab(window, self.tmp / "add.png")
        testing.click(dialog.primary_button)
        self.assertIsNone(window.dialog)
        self.assertIn("Kate", self.names(page.model))
        self.assertEqual(page.model.text("org.kde.kate.desktop", "source"), "You")
        self.assertTrue((self.tmp / "root/home/.config/autostart/org.kde.kate.desktop").is_file())
        self.assertEqual(page.view.current_row().key, "org.kde.kate.desktop")
        self.press(page, page.add_action)                              # the same again: told, not duplicated
        window.dialog.list.setCurrentRow(shown.index("Kate"))
        testing.click(window.dialog.primary_button)
        self.assertEqual(window.banner.severity(), "error")
        self.assertEqual(window.banner.message(), "Kate already starts when you sign in.")
        self.press(page, page.add_action)
        testing.click(window.dialog.close_button)                      # cancelled: nothing happens
        self.assertEqual(self.names(page.model).count("Kate"), 1)

    def test_remove_asks_and_only_for_the_users_own(self):
        window = self.window("startup")
        page = window.pages["startup"]
        self.select(page, "org.kde.kdeconnect.daemon.desktop")
        self.assertFalse(page.remove_action.isEnabled())
        self.assertNotIn("Remove", [action.text() for action in page.context_menu().actions()])
        self.select(page, "notes.desktop")
        self.answer = False
        page.remove_action.trigger()
        self.assertEqual(self.questions[0][2], "Remove")
        self.assertIn("My Notes", self.names(page.model))
        self.answer = True
        testing.key(page.view, Qt.Key.Key_Delete)
        self.assertNotIn("My Notes", self.names(page.model))
        self.assertFalse((self.tmp / "root/home/.config/autostart/notes.desktop").exists())

    def test_open_file_location_filter_and_empty_state(self):
        window = self.window("startup")
        page = window.pages["startup"]
        self.select(page, "boswas-agent.desktop")
        page.location_action.trigger()
        self.assertEqual(self.backend.shown, [str(self.tmp / "root/etc/xdg/autostart/boswas-agent.desktop")])
        testing.type_text(window.search, "kde")
        self.assertEqual(self.names(page.model), ["KDE Connect"])
        testing.type_text(window.search, "zzzz")
        self.assertIs(page.stack.currentWidget(), page.nothing)
        window.search.clear()
        for path in (self.tmp / "root/etc/xdg/autostart").iterdir():
            path.unlink()
        (self.tmp / "root/home/.config/autostart/notes.desktop").unlink()
        window.refresh()
        self.assertIs(page.stack.currentWidget(), page.empty)
        testing.click(page.empty.action_button)
        self.assertIsInstance(window.dialog, AddStartupDialog)
        window.dialog.reject()


class ServicesTest(UiCase):
    def test_services_of_both_managers(self):
        window = self.window("services")
        page = window.pages["services"]
        model = page.model
        self.assertEqual(self.names(model), ["backup.service", "bluetooth.service", "cups.service", "pipewire.service",
                                             "ssh.service", "tracker.service"])
        self.assertEqual([model.text("system:cups.service", key) for key in ("pid", "description", "status", "startup", "scope")],
                         ["310", "CUPS Scheduler", "Running", "Enabled", "System"])
        self.assertEqual(model.text("system:backup.service", "status"), "Failed")
        self.assertEqual(model.text("system:bluetooth.service", "status"), "Inactive")
        self.assertEqual(model.text("user:pipewire.service", "scope"), "User")
        self.assertEqual(self.backend.list_calls, [("system", True), ("user", True)])
        self.assertEqual(window.sampling.parts(), frozenset())
        page.load(False)
        self.assertEqual(self.backend.list_calls[2:], [("system", False), ("user", False)])
        window.refresh()                                              # F5 reads everything again
        self.assertEqual(self.backend.list_calls[4:], [("system", True), ("user", True)])

    def test_start_stop_restart(self):
        window = self.window("services")
        page = window.pages["services"]
        for action in (page.start_action, page.stop_action, page.restart_action):
            self.assertFalse(page.bar.button(action).isEnabled())
        self.select(page, "system:bluetooth.service")
        self.assertTrue(page.bar.button(page.start_action).isEnabled())
        self.assertFalse(page.bar.button(page.stop_action).isEnabled())
        self.press(page, page.start_action)
        self.assertEqual(self.backend.service_calls, [("system", "start", "bluetooth.service")])
        self.assertEqual(window.banner.severity(), "success")
        self.assertEqual(window.banner.message(), "bluetooth.service was started and is running.")
        self.assertEqual(page.model.text("system:bluetooth.service", "status"), "Running")
        self.assertEqual(page.model.text("system:bluetooth.service", "pid"), "4321")
        self.assertEqual(page.view.current_row().key, "system:bluetooth.service")
        self.assertTrue(page.bar.button(page.stop_action).isEnabled())
        self.press(page, page.restart_action)
        self.choose(page.context_menu(), "Stop")
        self.assertEqual(self.backend.service_calls[1:], [("system", "restart", "bluetooth.service"),
                                                          ("system", "stop", "bluetooth.service")])
        self.assertEqual(page.model.text("system:bluetooth.service", "status"), "Inactive")
        self.select(page, "user:tracker.service")
        self.press(page, page.start_action)
        self.assertEqual(self.backend.service_calls[-1], ("user", "start", "tracker.service"))

    def test_refusal_is_shown(self):
        window = self.window("services")
        page = window.pages["services"]
        self.backend.service_error = BusError("The system refused to stop cups.service: Access denied: Interactive "
                                              "authentication required.")
        self.select(page, "system:cups.service")
        self.press(page, page.stop_action)
        self.assertEqual(window.banner.severity(), "error")
        self.assertIn("refused to stop cups.service", window.banner.message())
        self.assertEqual(page.model.text("system:cups.service", "status"), "Running")
        self.assertEqual(page.busy, "")
        self.assertTrue(page.bar.button(page.stop_action).isEnabled())

    def test_context_menu_details_and_copy(self):
        window = self.window("services")
        page = window.pages["services"]
        self.select(page, "system:backup.service")
        menu = page.context_menu()
        enabled = {action.text(): action.isEnabled() for action in menu.actions() if not action.isSeparator()}
        self.assertEqual(enabled, {"Start": True, "Stop": False, "Restart": False, "Go to details": False, "Copy name": True})
        self.choose(menu, "Copy name")
        self.assertEqual(self.backend.clipboard, "backup.service")
        self.select(page, "system:ssh.service")
        self.choose(page.context_menu(), "Go to details")
        self.assertEqual(window.navigation.current(), "details")
        self.assertEqual(window.pages["details"].view.current_row().data.pid, 300)

    def test_one_manager_out_of_reach(self):
        self.backend.units["system"] = tm_ui.NO_BUS
        window = self.window("services")
        page = window.pages["services"]
        self.assertEqual(self.names(page.model), ["pipewire.service", "tracker.service"])
        self.assertFalse(page.note.isHidden())
        self.assertIn("Only your own services are listed: the system's services could not be read.", page.note.message())
        self.assertIs(page.stack.currentWidget(), page.view)

    def test_no_systemd_at_all(self):
        self.backend.units = {"system": tm_ui.NO_BUS, "user": BusError("There is no session message bus in this session.")}
        window = self.window("services")
        page = window.pages["services"]
        self.assertIs(page.stack.currentWidget(), page.empty)
        self.assertEqual(page.empty.title_label.text(), "Services are not available")
        self.assertIn("cannot be reached", page.empty.text_label.text())
        self.assertIn("no session message bus", page.empty.text_label.text())
        self.assertTrue(page.note.isHidden())
        for action in (page.start_action, page.stop_action, page.restart_action):
            self.assertFalse(action.isEnabled())
        testing.grab(window, self.tmp / "no-services.png")

    def test_filter(self):
        window = self.window("services")
        page = window.pages["services"]
        testing.type_text(window.search, "multimedia")
        self.assertEqual(self.names(page.model), ["pipewire.service"])
        window.search.clear()
        testing.type_text(window.search, "310")
        self.assertEqual(self.names(page.model), ["cups.service"])
        testing.type_text(window.search, "9")
        self.assertIs(page.stack.currentWidget(), page.nothing)


class SettingsAndWindowTest(UiCase):
    def test_refresh_speed(self):
        window = self.window("settings")
        page = window.pages["settings"]
        self.assertEqual(window.sampling.interval(), 1.0)
        self.assertEqual([page.refresh_box.itemText(i) for i in range(page.refresh_box.count())],
                         ["High (every 0.5 s)", "Normal (every second)", "Low (every 4 s)", "Paused"])
        page.refresh_box.setCurrentIndex(0)
        self.assertEqual(window.sampling.interval(), 0.5)
        page.refresh_box.setCurrentIndex(2)
        self.assertEqual((window.sampling.interval(), window.history.interval), (4.0, 4.0))
        page.refresh_box.setCurrentIndex(3)
        self.assertIsNone(window.sampling.interval())
        stored = json.loads(self.backend.settings_path().read_text())
        self.assertEqual(stored["refresh"], "paused")
        window.show_page("processes")
        before = window.sampling.ticks
        window.refresh()                                               # F5 still takes a sample
        self.assertEqual(window.sampling.ticks, before + 1)

    def test_start_page_and_command_line_column(self):
        window = self.window("settings")
        page = window.pages["settings"]
        page.page_box.setCurrentIndex(page.page_box.findData("services"))
        testing.click(page.command_switch)
        self.assertTrue(window.settings.full_command_line)
        processes = window.pages["processes"]
        self.assertEqual([column.key for column in processes.model.columns][-1], "command")
        window.show_page("processes")
        self.assertEqual(processes.model.text("g700.7000", "command"), "/usr/bin/python3 -IB /usr/bin/boswas-terminal")
        self.assertEqual(self.names(processes.model)[0], "Apps (3)")
        window.close()
        again = self.window()
        self.assertEqual(again.navigation.current(), "services")
        self.assertEqual(again.windowTitle(), "Services — Task Manager")
        self.assertEqual(again.pages["processes"].model.column_index("command"), 5)
        testing.click(again.pages["settings"].command_switch)
        self.assertEqual(again.pages["processes"].model.column_index("command"), -1)

    def test_theme(self):
        window = self.window("settings")
        page = window.pages["settings"]
        self.assertEqual(fluent.manager().scheme, "dark")
        page.theme_box.setCurrentIndex(page.theme_box.findData("light"))
        self.assertEqual(fluent.manager().scheme, "light")
        self.assertFalse(fluent.manager().follows_desktop)
        testing.grab(window, self.tmp / "light.png")
        page.theme_box.setCurrentIndex(page.theme_box.findData("desktop"))
        self.assertTrue(fluent.manager().follows_desktop)
        self.assertEqual(settings_module.load(self.backend.settings_path()).theme, "desktop")

    def test_only_the_visible_page_is_sampled(self):
        window = self.window()
        expected = {
            "processes": {"procs", "io", "memory", "disks"}, "performance": {"cpu", "memory", "disks", "net", "gpu", "battery"},
            "startup": set(), "users": {"procs", "memory"}, "details": {"procs", "pss"}, "services": set(), "settings": set(),
        }
        for key in PAGES:
            window.show_page(key)
            self.assertEqual(window.sampling.parts(), frozenset(expected[key]), key)
            self.assertEqual(window.navigation.title(), {"startup": "Startup apps"}.get(key, key.capitalize()))
        self.assertFalse(window.sampling.is_active())                  # Settings shows nothing that is sampled
        window.show_page("processes")
        self.assertTrue(window.sampling.is_active())
        window.hide()
        self.assertFalse(window.sampling.is_active())
        window.show()
        self.assertTrue(window.sampling.is_active())
        window.showMinimized()
        testing.process()
        if window.isMinimized():                                       # the offscreen platform may not minimise
            self.assertFalse(window.sampling.is_active())

    def test_search_box_follows_the_page(self):
        window = self.window()
        self.assertTrue(window.search.isVisible())
        testing.type_text(window.search, "vim")
        window.show_page("details")
        self.assertEqual([row.data.pid for row in window.pages["details"].model.top_rows()], [702])
        window.show_page("performance")
        self.assertFalse(window.search.isVisible())
        window.show_page("services")
        self.assertTrue(window.search.isVisible())
        self.assertEqual(window.search.placeholderText(), "Filter by name, description or PID")
        window.focus_search()
        self.assertIs(QApplication.focusWidget(), window.search)

    def test_only_the_commands_of_the_page_are_shown(self):
        window = self.window()
        for key in PAGES:
            window.show_page(key)
            shown = [name for name, page in window.pages.items() if page.bar.isVisible()]
            self.assertEqual(shown, [key])

    def test_keyboard_page_switching_and_arguments(self):
        window = self.window()
        window.step_page(1)
        self.assertEqual(window.navigation.current(), "performance")
        window.step_page(-1)
        window.step_page(-1)
        self.assertEqual(window.navigation.current(), "settings")
        window.handle_arguments(["--page", "users"])
        self.assertEqual(window.navigation.current(), "users")
        window.handle_arguments(["--page=nonsense", "junk"])
        self.assertEqual(window.navigation.current(), "users")
        self.assertEqual(page_argument(["--page"]), None)
        self.assertEqual(page_argument(["--page=details"]), "details")
        testing.key(window.navigation.list, Qt.Key.Key_Down)
        self.assertEqual(window.navigation.current(), "details")

    def test_run_new_task(self):
        window = self.window()
        page = window.pages["processes"]
        self.press(page, page.run_action)
        dialog = window.dialog
        self.assertIsInstance(dialog, RunDialog)
        self.assertEqual(dialog.directory.text(), "~")
        testing.click(dialog.primary_button)                           # nothing typed
        self.assertTrue(dialog.is_open())
        self.assertEqual(dialog.error.message(), "Type the name of a program.")
        testing.type_text(dialog.command, 'say "unclosed')
        testing.click(dialog.primary_button)
        self.assertIn("quotation mark is not closed", dialog.error.message())
        dialog.command.setText("no-such-program --flag")
        testing.click(dialog.primary_button)
        self.assertIn("No program named “no-such-program” was found", dialog.error.message())
        self.assertTrue(dialog.is_open())
        testing.grab(window, self.tmp / "run-error.png")
        dialog.command.setText('editor "my file.txt" $HOME ; rm -rf x')
        dialog.directory.setText(str(self.tmp / "nowhere"))
        testing.click(dialog.primary_button)
        self.assertIn("does not exist", dialog.error.message())
        dialog.directory.setText(str(self.tmp))
        testing.key(dialog.command, Qt.Key.Key_Return)
        self.assertIsNone(window.dialog)
        self.assertEqual(self.backend.started, [(["editor", "my file.txt", "$HOME", ";", "rm", "-rf", "x"], str(self.tmp))])
        self.assertEqual(window.banner.message(), "editor was started.")
        window.run_new_task()
        testing.key(window.dialog, Qt.Key.Key_Escape)
        self.assertIsNone(window.dialog)
        self.assertEqual(len(self.backend.started), 1)

    def test_every_control_has_a_name(self):
        window = self.window()
        for key in PAGES:
            window.show_page(key)
            for widget in window.findChildren(QAbstractButton) + window.findChildren(QLineEdit) + window.findChildren(QComboBox):
                if not widget.isVisible() or widget.objectName().startswith("qt_"):
                    continue
                name = widget.accessibleName() or getattr(widget, "text", lambda: "")() or widget.toolTip()
                self.assertTrue(name, f"{key}: {widget!r} has no name")
        for page in window.pages.values():
            for view in page.findChildren(table.TableView):
                self.assertTrue(view.accessibleName())

    def test_both_schemes_paint_every_page(self):
        window = self.window()
        self.tick(window, busy=100, idle=300)
        for scheme in ("dark", "light"):
            testing.set_scheme(scheme)
            for key in PAGES:
                window.show_page(key)
                self.tick(window, busy=50, idle=350)
                image = testing.grab(window, self.tmp / f"{key}-{scheme}.png")
                self.assertGreater(image.stat().st_size, 5000)

    def test_settings_that_cannot_be_saved_are_reported(self):
        window = self.window("settings")
        blocked = self.tmp / "blocked"
        blocked.write_text("a file")
        self.backend._settings_path = blocked / "settings.json"
        testing.click(window.pages["settings"].command_switch)
        self.assertEqual(window.banner.severity(), "error")
        self.assertIn("settings could not be saved", window.banner.message())


class SamplingThreadTest(UiCase):
    def test_thread_samples_while_active_and_stops(self):
        samples = []
        sampling = Sampling(self.backend.make_sampler, threaded=True)
        sampling.sampled.connect(lambda sample: (samples.append(sample), sampling.done()))
        sampling.start()
        sampling.configure({"procs"}, 0.02, False)
        testing.process(120)
        self.assertEqual(samples, [])                                  # not active: nothing is read
        sampling.configure(active=True)
        self.assertTrue(testing.wait_until(lambda: len(samples) >= 3, 5))
        self.assertEqual(sorted(samples[-1].procs), sorted(self.system.processes))
        sampling.configure(active=False)
        testing.process(60)
        count = len(samples)
        testing.process(120)
        self.assertEqual(len(samples), count)
        sampling.request()                                             # asked for: one more, even when not active
        self.assertTrue(testing.wait_until(lambda: len(samples) == count + 1, 5))
        sampling.configure({"memory"}, None, True)                     # paused: only the sample for the new parts
        self.assertTrue(testing.wait_until(lambda: len(samples) == count + 2, 5))
        self.assertIsNone(samples[-1].procs)
        self.assertIsNotNone(samples[-1].memory)
        testing.process(100)
        self.assertEqual(len(samples), count + 2)
        sampling.stop()
        self.assertIsNone(sampling._thread)

    def test_window_with_the_real_thread(self):
        self.backend.threaded = True
        window = self.window()
        page = window.pages["processes"]
        self.assertTrue(testing.wait_until(lambda: page.model.rowCount() > 0, 5))
        self.assertTrue(window.worker.wait(5))
        self.assertTrue(testing.wait_until(lambda: page.section_counts().get("Apps") == 3, 5))
        window.show_page("services")
        self.assertTrue(testing.wait_until(lambda: window.pages["services"].model.rowCount() == 6, 5))
        window.close()
        self.assertIsNone(window.sampling._thread)


class PackagingTest(unittest.TestCase):
    def test_no_colour_is_written_in_the_sources(self):
        pattern = re.compile(r"#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b|\brgba?\(|QColor\(\s*[\"'\d]|Qt\.GlobalColor\.(?!transparent)|setStyleSheet")
        for path in sorted((PACKAGE / "boswas_apps/task_manager").rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                self.assertIsNone(pattern.search(line), f"{path.name}:{number}: {line.strip()}")

    def test_engine_modules_do_not_import_qt(self):
        for name in ("textutil", "procfs", "sysfs", "sampler", "desktopfile", "grouping", "services", "actions",
                     "settings", "history"):
            text = (PACKAGE / "boswas_apps/task_manager" / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotIn("PySide6", text, name)
            self.assertNotIn("boswas_fluent", text, name)
            self.assertNotIn("subprocess.run", text, name)
            self.assertNotIn("shell=True", text, name)

    def test_desktop_entry_launcher_and_manual(self):
        entry = (PACKAGE / "data/task-manager" / f"{APP_ID}.desktop").read_text(encoding="utf-8")
        for line in ("Name=Task Manager", "Exec=boswas-task-manager", "Icon=utilities-system-monitor",
                     "Categories=System;Monitor;", f"StartupWMClass={APP_ID}", "Type=Application"):
            self.assertIn(f"\n{line}\n", entry)
        icons = PACKAGE.parents[1] / "desktop/icons/Boswas-Prism/scalable/apps"
        if icons.is_dir():
            self.assertTrue((icons / "utilities-system-monitor.svg").is_file())
        launcher = (PACKAGE / "bin/boswas-task-manager").read_text(encoding="utf-8")
        self.assertTrue(launcher.startswith("#!/usr/bin/python3 -IB\n"))
        self.assertIn("from boswas_apps.task_manager.__main__ import main", launcher)
        manual = (PACKAGE / "man/boswas-task-manager.1").read_text(encoding="utf-8")
        for section in (".SH NAME", ".SH SYNOPSIS", ".SH DESCRIPTION", ".SH OPTIONS", ".SH FILES", ".SH SEE ALSO"):
            self.assertIn(section, manual)
        for page in PAGES:
            self.assertIn(page, manual)

    def test_command_line(self):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(["--help"]), 0)
            self.assertEqual(main(["--version"]), 0)
            self.assertEqual(main(["--page", "nonsense"]), 2)
            self.assertEqual(main(["--page"]), 2)
        self.assertIn("Usage: boswas-task-manager", out.getvalue())
        self.assertRegex(out.getvalue(), r"boswas-task-manager \d")
        self.assertIn("there is no page “nonsense”", err.getvalue())

    def test_icons_used_exist(self):
        testing.app()
        wanted = set()
        pattern = re.compile(r"[\"']([a-z0-9-]+-symbolic|application-x-executable|utilities-system-monitor|"
                             r"preferences-system|preferences-desktop-theme-global)[\"']")
        for path in sorted((PACKAGE / "boswas_apps/task_manager/ui").rglob("*.py")):
            wanted |= set(pattern.findall(path.read_text(encoding="utf-8")))
        missing = sorted(name for name in wanted if not fluent.has_icon(name))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
