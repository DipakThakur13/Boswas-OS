"""The Users page: the section on stand-ins for AccountsService (busctl), passwd and the D-Bus password call.

Run: QT_QPA_PLATFORM=offscreen python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402
from test_accounts import FakeBus, properties  # noqa: E402

from boswas_control_center import accounts as ac  # noqa: E402
from boswas_control_center import commands  # noqa: E402

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget
    PYSIDE = True
except ImportError:
    PYSIDE = False


class Bus(FakeBus):
    """FakeBus plus a stand-in for passwd on the pseudo-terminal."""

    def __init__(self):
        super().__init__()
        self.password = "oldpass12"
        self.conversations: list[tuple[str, str]] = []

    def converse(self, command, dialogue, timeout=60.0) -> int:
        assert command == commands.passwd_argv()
        typed = []
        for prompt in ("Changing password for alice.\nCurrent password: ", "New password: ",
                       "Retype new password: "):
            typed += dialogue.feed(prompt)
            if len(typed) == 1 and typed[0] != self.password:
                dialogue.feed("passwd: Authentication token manipulation error\npasswd: password unchanged\n")
                return 10
        self.conversations.append((typed[0], typed[1]))
        self.password = typed[1]
        dialogue.feed("passwd: password updated successfully\n")
        return 0


if PYSIDE:
    import boswas_fluent as fluent
    from boswas_fluent import testing
    from boswas_control_center.ui import settings
    from boswas_control_center.ui.settings import accounts as ui

    class Case(testing.FluentTestCase):
        uid = 1000

        def setUp(self):
            super().setUp()
            self.bus = Bus()
            self.home = self.tmp / "home"
            self.home.mkdir()
            self.root = self.tmp / "root"
            fx.write(self.root / "etc/passwd", "alice:x:1000:1000:Alice Example:/home/alice:/bin/bash\n"
                     "bob:x:1001:1001:Bob:/home/bob:/bin/bash\n")
            self.hashes: list[tuple[str, str]] = []
            self.password_error: str | None = None
            self.host = QWidget()
            QVBoxLayout(self.host)

        def set_password(self, path, hashed, done) -> None:
            self.hashes.append((path, hashed))
            done(self.password_error)

        def section(self):
            service = ui.AccountsService(self.host, runner=self.bus, environ={"HOME": str(self.home)},
                                         root=str(self.root), uid=self.uid, set_password=self.set_password)
            section = ui.AccountsSection(self.host, service=service)
            self.host.layout().addWidget(section)
            self.show(self.host, 900, 800)
            section.activate()
            self.settle(section)
            return section

        @staticmethod
        def settle(section) -> None:
            assert testing.wait_until(lambda: not section.loading and not section.busy, 10)
            testing.process(20)

        def submit(self, section, dialog) -> None:
            testing.click(dialog.primary_button)
            assert testing.wait_until(lambda: not section.busy and not section.loading and not dialog.busy_now, 10)
            testing.process(20)


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class OwnAccountTest(Case if PYSIDE else unittest.TestCase):
    def test_shows_the_accounts(self):
        section = self.section()
        self.assertEqual(section.me_name.text(), "Alice Example")
        self.assertEqual(section.me_detail.text(), "alice · Administrator")
        self.assertEqual(section.avatar.name(), "Alice Example")
        self.assertEqual(section.name_card.description(), "Alice Example")
        self.assertEqual(section.others.keys(), [1001])
        row = section.others.row(1001)
        self.assertEqual((row.header.title(), row.header.description()), ("Bob", "bob · Standard user"))
        self.assertTrue(section.others_title.isVisibleTo(self.host))
        self.assertFalse(section.notice.isVisibleTo(self.host))
        for button in (section.picture_button, section.name_button, section.password_button, section.add_button):
            self.assertTrue(button.isEnabled())
        self.assertEqual(self.bus.calls(), [])

    def test_change_name(self):
        section = self.section()
        testing.click(section.name_button)
        dialog = section.dialog
        self.assertEqual(dialog.name.text(), "Alice Example")
        dialog.name.setText("Bad: name")
        self.submit(section, dialog)
        self.assertTrue(dialog.is_open())                                # told in the dialog, nothing sent
        self.assertIn("cannot be empty", dialog.problem.message())
        self.assertEqual(self.bus.calls(), [])
        dialog.name.setText("Alice B. Example")
        self.bus.fail["SetRealName"] = commands.Completed(1, "", "Call failed: Not authorized\n")
        self.submit(section, dialog)
        self.assertTrue(dialog.is_open())
        self.assertEqual(dialog.problem.message(), "Changing the name was not allowed: administrator authorization "
                                                   "was not given.")
        del self.bus.fail["SetRealName"]
        self.submit(section, dialog)
        self.assertFalse(dialog.is_open())
        self.assertEqual(self.bus.calls()[-1], ["SetRealName", "Alice B. Example"])
        self.assertEqual(section.me_name.text(), "Alice B. Example")
        self.assertEqual(section.notice.message(), "Your name was changed.")

    def test_change_password_checks_then_uses_passwd(self):
        section = self.section()
        testing.click(section.password_button)
        dialog = section.dialog
        self.assertEqual(dialog.current.echoMode(), QLineEdit.EchoMode.Password)
        dialog.current.reveal_action.trigger()                           # show and hide
        self.assertEqual(dialog.current.echoMode(), QLineEdit.EchoMode.Normal)
        dialog.current.reveal_action.trigger()
        for current, new, again, words in (("", "newpass345", "newpass345", "Enter your current password."),
                                           ("oldpass12", "short", "short", "at least 8 characters"),
                                           ("oldpass12", "newpass345", "newpass346", "not the same"),
                                           ("oldpass12", "oldpass12", "oldpass12", "must differ"),
                                           ("oldpass12", "alice", "alice", "at least 8")):
            dialog.current.setText(current)
            dialog.new.setText(new)
            dialog.again.setText(again)
            self.submit(section, dialog)
            self.assertIn(words, dialog.problem.message())
        self.assertEqual(self.bus.conversations, [])
        dialog.current.setText("wrong-one")                              # passwd refuses the current password
        dialog.new.setText("newpass345")
        dialog.again.setText("newpass345")
        self.submit(section, dialog)
        self.assertTrue(dialog.is_open())
        self.assertEqual(dialog.problem.message(), "The current password is not correct.")
        self.assertFalse(dialog.new.isReadOnly())
        dialog.current.setText("oldpass12")
        left = []                                                        # what the boxes hold when the dialog closes
        dialog.finished.connect(lambda _r: left.append((dialog.current.text(), dialog.new.text(),
                                                        dialog.again.text())))
        testing.key(dialog.again, Qt.Key.Key_Return)                     # Enter submits
        assert testing.wait_until(lambda: not section.busy and not section.loading, 10)
        self.assertIsNone(section.dialog)
        self.assertEqual(self.bus.conversations, [("oldpass12", "newpass345")])
        self.assertEqual(left, [("", "", "")])
        self.assertEqual(section.notice.message(), "Your password was changed.")
        self.assertEqual(self.hashes, [])                                # the own password never goes as a hash
        self.assertNotIn("newpass345", repr(self.bus.ran))               # and never on a command line

    def test_change_picture(self):
        section = self.section()
        source = self.tmp / "photo.png"
        image = QImage(300, 200, QImage.Format.Format_RGB32)
        image.fill(QColor(200, 50, 50))
        image.save(str(source))
        section.choose_file = lambda: str(source)
        testing.click(section.picture_button)
        self.settle(section)
        face = self.home / ".face"
        saved = QImage(str(face))
        self.assertEqual((saved.width(), saved.height()), (256, 256))    # a centred square, scaled
        self.assertEqual(face.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(self.bus.calls()[-1], ["SetIconFile", str(face)])
        self.assertEqual(section.notice.message(), "Your picture was changed.")
        self.assertTrue(section.avatar.has_picture())
        text = self.tmp / "notes.txt"
        text.write_text("not a picture")
        section.choose_file = lambda: str(text)
        testing.click(section.picture_button)
        self.settle(section)
        self.assertIn("not a picture", section.error.message())
        section.choose_file = lambda: ""                                 # cancelled
        calls = len(self.bus.calls())
        testing.click(section.picture_button)
        self.settle(section)
        self.assertEqual(len(self.bus.calls()), calls)


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class OtherUsersTest(Case if PYSIDE else unittest.TestCase):
    def test_account_type(self):
        section = self.section()
        row = section.others.row(1001)
        row.kind.setCurrentIndex(1)
        row.kind.activated.emit(1)
        self.settle(section)
        self.assertEqual(self.bus.calls(), [["SetAccountType", "1"]])
        self.assertEqual(row.header.description(), "bob · Administrator")
        self.assertEqual(section.notice.message(), "Bob is now an administrator.")
        self.bus.fail["SetAccountType"] = commands.Completed(1, "", "Call failed: Not authorized\n")
        row.kind.setCurrentIndex(0)
        row.kind.activated.emit(0)
        self.settle(section)
        self.assertIn("was not allowed", section.error.message())
        self.assertEqual(row.kind.currentIndex(), 1)                     # back to what is in force

    def test_the_last_administrator_is_protected(self):
        self.uid = 1001                                                  # signed in as Bob, a standard user
        section = self.section()
        alice = section.others.row(1000)
        self.assertFalse(alice.remove_button.isEnabled())
        self.assertIn("only administrator", alice.remove_row.description())
        self.assertFalse(alice.kind.isEnabled())
        section.remove_user(1000)                                        # even when asked for directly
        section.set_type(1000, ac.STANDARD)
        self.settle(section)
        self.assertEqual(self.bus.calls(), [])
        self.assertIn("only administrator", section.error.message())
        section.remove_user(1001)                                        # and never the own account
        self.assertIn("signed in with", section.error.message())
        self.assertIsNone(section.dialog)

    def test_reset_password_sends_a_hash_over_dbus(self):
        section = self.section()
        testing.click(section.others.row(1001).reset_button)
        dialog = section.dialog
        self.assertIsNone(dialog.current)                                # an administrator is not asked for it
        dialog.new.setText("bobs new pass")
        dialog.again.setText("bobs new pass")
        self.password_error = "Setting the password was not allowed: administrator authorization was not given."
        self.submit(section, dialog)
        self.assertTrue(dialog.is_open())
        self.assertEqual(dialog.problem.message(), self.password_error)
        self.password_error = None
        self.submit(section, dialog)
        self.assertFalse(dialog.is_open())
        path, hashed = self.hashes[-1]
        self.assertEqual(path, "/org/freedesktop/Accounts/User1001")
        self.assertEqual(ac.sha512_crypt("bobs new pass", hashed.split("$")[2]), hashed)   # the hash of that password
        self.assertNotIn("bobs new pass", repr(self.bus.ran))            # nothing of it on a command line
        self.assertNotIn(hashed, repr(self.bus.ran))
        self.assertEqual(self.bus.conversations, [])                     # passwd is for the own password only
        self.assertEqual(section.notice.message(), "The password of Bob was changed.")

    def test_remove_user(self):
        section = self.section()
        testing.click(section.others.row(1001).remove_button)
        dialog = section.dialog
        self.assertEqual(dialog.title_label.text(), "Remove Bob?")
        self.assertIn("bob", dialog.text_label.text())
        self.assertIn("Also delete this user's files", dialog.files.text())
        testing.click(dialog.close_button)
        self.settle(section)
        self.assertEqual(self.bus.calls(), [])
        testing.click(section.others.row(1001).remove_button)
        testing.click(section.dialog.files)
        testing.click(section.dialog.primary_button)
        self.settle(section)
        self.assertEqual(self.bus.calls(), [["DeleteUser", "1001", "true"]])
        self.assertEqual(section.others.keys(), [])
        self.assertFalse(section.others_title.isVisibleTo(self.host))
        self.assertEqual(section.notice.message(), "Bob was removed.")

    def test_add_user(self):
        section = self.section()
        testing.click(section.add_button)
        dialog = section.dialog
        testing.type_text(dialog.real, "Carol Example")
        self.assertEqual(dialog.name.text(), "carol")                    # suggested from the name
        dialog.name.clear()
        testing.type_text(dialog.name, "bob")                            # edited by hand: no longer overwritten
        testing.type_text(dialog.real, " Jr")
        self.assertEqual(dialog.name.text(), "bob")
        dialog.password.setText("carols pass")
        dialog.again.setText("carols pass")
        self.submit(section, dialog)
        self.assertIn("a user named bob already", dialog.problem.message())
        dialog.name.setText("Carol")
        self.submit(section, dialog)
        self.assertIn("starts with a small letter", dialog.problem.message())
        dialog.name.setText("carol")
        dialog.again.setText("different")
        self.submit(section, dialog)
        self.assertIn("not the same", dialog.problem.message())
        self.assertEqual(self.bus.calls(), [])
        dialog.again.setText("carols pass")
        dialog.kind.setCurrentIndex(1)
        self.submit(section, dialog)
        self.assertFalse(dialog.is_open())
        self.assertEqual(self.bus.calls(), [["CreateUser", "carol", "Carol Example Jr", "1"]])
        path, hashed = self.hashes[-1]
        self.assertEqual(path, "/org/freedesktop/Accounts/User1002")     # the password of the new account
        self.assertEqual(ac.sha512_crypt("carols pass", hashed.split("$")[2]), hashed)
        self.assertEqual(section.others.keys(), [1001, 1002])
        self.assertEqual(section.others.row(1002).header.description(), "carol · Administrator")
        self.assertEqual(section.notice.message(), "Carol Example Jr was added.")
        self.assertNotIn("carols pass", repr(self.bus.ran))

    def test_add_user_whose_password_cannot_be_set_is_undone(self):
        section = self.section()
        testing.click(section.add_button)
        dialog = section.dialog
        dialog.real.setText("Dave")
        dialog.name.setText("dave")
        dialog.password.setText("daves pass")
        dialog.again.setText("daves pass")
        self.password_error = "Setting the password failed: no reply"
        self.submit(section, dialog)
        self.assertTrue(dialog.is_open())
        self.assertEqual(self.bus.calls(), [["CreateUser", "dave", "Dave", "0"], ["DeleteUser", "1002", "true"]])
        self.assertEqual(dialog.problem.message(), "Dave was not added: the account's password could not be set. "
                                                   "Setting the password failed: no reply")
        self.assertEqual(section.others.keys(), [1001])
        # The account cannot be removed again either: the user is told exactly what exists now.
        self.bus.fail["DeleteUser"] = commands.Completed(1, "", "Call failed: Not authorized\n")
        self.submit(section, dialog)
        message = dialog.problem.message()
        self.assertIn("The account dave was created, but its password could not be set", message)
        self.assertIn("could not be removed again", message)
        self.assertIn("Reset password", message)
        self.assertEqual(section.others.keys(), [1001, 1003])            # and it is shown

    def test_without_the_accounts_service(self):
        self.bus.down = True
        section = self.section()
        self.assertTrue(section.notice.isVisibleTo(self.host))
        self.assertIn("needs the accounts service", section.notice.message())
        self.assertEqual(section.me_name.text(), "Alice Example")        # from /etc/passwd
        self.assertEqual(section.others.keys(), [1001])
        row = section.others.row(1001)
        for control in (section.name_button, section.add_button, row.kind, row.reset_button, row.remove_button):
            self.assertFalse(control.isEnabled())
        self.assertTrue(section.password_button.isEnabled())             # passwd needs no accounts service
        testing.click(section.password_button)
        dialog = section.dialog
        dialog.current.setText("oldpass12")
        dialog.new.setText("newpass345")
        dialog.again.setText("newpass345")
        self.submit(section, dialog)
        self.assertEqual(self.bus.conversations, [("oldpass12", "newpass345")])
        self.assertFalse(dialog.is_open())

    def test_registered_and_back_in_the_sidebar(self):
        self.assertEqual([cls.__name__ for cls in settings.sections_for("users")], ["AccountsSection"])
        self.assertEqual(settings.find("change password")[0], ui.AccountsSection)
        self.assertNotIn("accounts", settings.problems())
        from boswas_control_center.ui.main_window import MainWindow
        self.assertTrue(MainWindow.page_available("users"))

    def test_picture_helper_and_dbus_guard(self):
        with self.assertRaises(ui.Failed):
            ui.picture_png(str(self.tmp / "missing.png"))
        tall = self.tmp / "tall.png"
        image = QImage(40, 400, QImage.Format.Format_RGB32)
        image.fill(QColor(10, 10, 10))
        image.save(str(tall))
        loaded = QImage.fromData(ui.picture_png(str(tall)))
        self.assertEqual((loaded.width(), loaded.height()), (256, 256))
        answers = []
        ui.dbus_set_password("/org/freedesktop/login1", "$6$a$b", answers.append)       # not a user: never sent
        ui.dbus_set_password("/org/freedesktop/Accounts/User1001", "plain password", answers.append)
        self.assertEqual(len(answers), 2)
        self.assertTrue(all("could not be set" in answer for answer in answers))


if __name__ == "__main__":
    unittest.main()
