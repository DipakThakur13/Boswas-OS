"""System > Notifications (ui/settings/notifications.py): the list of senders (no Qt) and the sections, offscreen."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)
import test_settings_display as shared  # noqa: E402
import test_settings_taskbar as base  # noqa: E402

try:
    from boswas_control_center.ui.settings import notifications
except ImportError:                                          # no PySide6: the pure part is skipped too
    notifications = None


@unittest.skipIf(notifications is None, "PySide6 is not installed")
class SendersFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cc-senders-")
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        (self.state / "boswas").mkdir(parents=True)
        self.environ = {"XDG_STATE_HOME": str(self.state), "HOME": "/nonexistent"}

    def write(self, data) -> None:
        text = data if isinstance(data, str) else json.dumps(data)
        (self.state / "boswas" / "notification-senders.json").write_text(text)

    def test_the_shells_list_is_read_as_it_wrote_it(self):
        self.write([{"name": "Mail", "icon": "mail", "desktop_entry": "org.example.Mail", "last": 1700000000},
                    {"name": "Chat"}])
        senders = notifications.read_senders(self.environ)
        self.assertEqual(senders, [notifications.Sender("Mail", "mail", "org.example.Mail", 1700000000),
                                   notifications.Sender("Chat")])

    def test_what_another_program_may_have_put_there_is_checked(self):
        self.write([{"name": "Ok"}, {"name": "Ok"}, {"name": "a\u0007b"}, {"name": ""}, {"name": 3}, "x",
                    {"name": "Icon", "icon": "x" * 300}, {"name": "Last", "last": True}, {"name": "x" * 129}])
        self.assertEqual(notifications.read_senders(self.environ),
                         [notifications.Sender("Ok"), notifications.Sender("Last", last=0)])
        for text in ("not json", "{}", "[", "\xff"):
            self.write(text)
            self.assertEqual(notifications.read_senders(self.environ), [], text)
        self.write([{"name": f"App {i}"} for i in range(500)])
        self.assertEqual(len(notifications.read_senders(self.environ)), notifications.MAX_SENDERS)
        self.write("[" + " " * (notifications.MAX_SENDERS_BYTES + 10) + "]")
        self.assertEqual(notifications.read_senders(self.environ), [], "too large")

    def test_where_the_list_is(self):
        self.assertEqual(notifications.state_home({"XDG_STATE_HOME": "/s/"}), "/s")
        self.assertEqual(notifications.state_home({"XDG_STATE_HOME": "rel", "HOME": "/home/a"}),
                         "/home/a/.local/state")
        self.assertIsNone(notifications.state_home({}))
        self.assertEqual(notifications.read_senders({}), [])


if shared.HAVE_QT:
    from boswas_fluent import testing
    from PySide6.QtCore import QTime

    class DoNotDisturbTest(base.DesktopCase):
        def test_the_switch_and_the_schedule(self):
            section = self.open(notifications.DoNotDisturbSection(config=self.settings), 600)
            self.assertFalse(section.dnd.isChecked() or section.scheduled.isChecked())
            self.assertEqual((section.start.time(), section.end.time()), (QTime(22, 0), QTime(7, 0)))
            testing.click(section.dnd)
            shared.settle(section)
            testing.click(section.scheduled)
            shared.settle(section)
            self.assertTrue(section.schedule.is_expanded())
            section.start.setTime(QTime(21, 30))
            shared.settle(section)
            self.assertEqual(self.written(), {"notifications": {"do_not_disturb": True, "do_not_disturb_scheduled": True,
                                                                "do_not_disturb_from": "21:30"}})
            self.pictures(section, "do-not-disturb")

    class AppNotificationsTest(base.DesktopCase):
        def setUp(self) -> None:
            super().setUp()
            self.state = self.tmp / "state"
            (self.state / "boswas").mkdir(parents=True)
            self.environ = {**os.environ, "XDG_STATE_HOME": str(self.state)}

        def senders(self, *names: str) -> None:
            (self.state / "boswas" / "notification-senders.json").write_text(
                json.dumps([{"name": name, "icon": "dialog-information"} for name in names]))

        def test_without_senders_the_page_says_where_they_come_from(self):
            section = self.open(notifications.AppNotificationsSection(config=self.settings, environ=self.environ))
            self.assertTrue(section.empty.isVisibleTo(section) and not section.list.isVisibleTo(section))

        def test_banners_are_turned_off_by_application(self):
            self.senders("Mail", "Chat", "Updates")
            section = self.open(notifications.AppNotificationsSection(config=self.settings, environ=self.environ))
            self.assertEqual(list(section.rows), ["Mail", "Chat", "Updates"])
            self.assertTrue(all(switch.isChecked() for _card, switch in section.rows.values()))
            testing.click(section.rows["Chat"][1])
            shared.settle(section)
            testing.click(section.rows["Mail"][1])
            shared.settle(section)
            self.assertEqual(self.get("notifications", "banners_off"), ["Mail", "Chat"])
            self.assertEqual(section.rows["Chat"][0].description(), "Banners off: the notification centre only")
            testing.click(section.rows["Mail"][1])
            shared.settle(section)
            self.assertEqual(self.get("notifications", "banners_off"), ["Chat"])
            self.pictures(section, "app-notifications")

        def test_a_muted_application_that_left_the_list_stays_muted(self):
            self.settings.apply({("notifications", "banners_off"): ["Old app", "Chat"]})
            self.senders("Chat", "Mail")
            section = self.open(notifications.AppNotificationsSection(config=self.settings, environ=self.environ))
            self.assertFalse(section.rows["Chat"][1].isChecked())
            testing.click(section.rows["Mail"][1])
            shared.settle(section)
            self.assertEqual(self.get("notifications", "banners_off"), ["Old app", "Chat", "Mail"])


if __name__ == "__main__":
    unittest.main()
