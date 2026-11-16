"""Users: the signed-in account, the other people's accounts, adding and removing users.

Page "users":
  Your account   picture, name, user name and account type; Change picture
                 (a file, cropped to a centred square of 256 px, saved as
                 ~/.face and given to the accounts service), Change name,
                 Change password (passwd on a pseudo-terminal, so PAM's rules
                 apply). Then the other users, each with its account type,
                 Reset password and Remove, and Add user.

Accounts are read and changed through AccountsService (boswas_control_center
/accounts.py: busctl for everything that is not a secret). Another user's
password is set with the service's SetPassword over D-Bus, the one call made
here with QtDBus: its arguments are two strings, the SHA-512 crypt hash and
an empty hint, and they never appear on a command line. Changes that need
administrator rights are authorised by the service through polkit; the
shell shows the question.

Without the accounts service the accounts of /etc/passwd are shown
read-only; changing the own password still works.

Passwords live in the password boxes and in the call that uses them; the
boxes are emptied when a dialog closes, and nothing here logs.
"""

from __future__ import annotations

import os

import boswas_fluent as fluent
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, Signal
from PySide6.QtDBus import QDBusConnection, QDBusMessage, QDBusPendingCallWatcher
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

from ... import accounts as ac
from ... import commands, userfiles
from ...errors import Failed, user_message
from . import context
from .widgets import Busy, RowList, Section

PICTURE_SIZE = 256
MAX_PICTURE_FILE = 32 * 1024 * 1024
PICTURE_FILTER = "Pictures (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
DBUS_TIMEOUT_MS = 180000
STATUS_HEIGHT = 52


def picture_png(path: str) -> bytes:
    """The picture file as a PNG of a centred square, PICTURE_SIZE pixels wide (blocking)."""
    try:
        if os.path.getsize(path) > MAX_PICTURE_FILE:
            raise Failed("This picture file is too large.")
        with open(path, "rb") as handle:
            data = handle.read(MAX_PICTURE_FILE + 1)
    except OSError as exc:
        raise Failed(f"The picture could not be read ({exc.strerror or exc}).") from None
    image = QImage.fromData(QByteArray(data))
    if image.isNull() or image.width() < 1 or image.height() < 1:
        raise Failed("This file is not a picture Control Center can read.")
    side = min(image.width(), image.height())
    square = image.copy((image.width() - side) // 2, (image.height() - side) // 2, side, side)
    scaled = square.scaled(PICTURE_SIZE, PICTURE_SIZE, Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
    target = QByteArray()
    buffer = QBuffer(target)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    saved = scaled.save(buffer, "PNG")
    buffer.close()
    if not saved or target.isEmpty():
        raise Failed("The picture could not be converted.")
    return bytes(target)


def dbus_set_password(path: str, hashed: str, done, parent: QObject | None = None) -> None:
    """AccountsService SetPassword(hash, "") for the user at ``path``; ``done(error text or None)`` later.

    The only D-Bus call made from here: two strings, sent on the system
    bus, with interactive authorisation allowed so that polkit can ask.
    """
    if not commands.ACCOUNTS_USER_RE.fullmatch(path) or not hashed.startswith("$6$"):
        done("The password could not be set (not a user of the accounts service).")
        return
    bus = QDBusConnection.systemBus()
    if not bus.isConnected():
        done("The password could not be set: the system bus is not available.")
        return
    message = QDBusMessage.createMethodCall(commands.ACCOUNTS_SERVICE, path, commands.ACCOUNTS_SERVICE + ".User",
                                            "SetPassword")
    message.setArguments([hashed, ""])
    message.setInteractiveAuthorizationAllowed(True)
    watcher = QDBusPendingCallWatcher(bus.asyncCall(message, DBUS_TIMEOUT_MS), parent)

    def finished(_watcher=None) -> None:
        error = None
        if watcher.isError():
            name, text = watcher.error().name(), watcher.error().message()
            lowered = f"{name} {text}".lower()
            error = ("Setting the password was not allowed: administrator authorization was not given."
                     if any(word in lowered for word in ac.DENIED_WORDS) else
                     "Setting the password failed: " + " ".join(text.split())[:200])
        watcher.deleteLater()
        done(error)

    watcher.finished.connect(finished)


class AccountsService(QObject):
    """Everything the page does with accounts, without blocking the window.

    Every method returns at once and calls ``done(error)`` later on the GUI
    thread: None when it worked, else the reason in words for the user.
    """

    def __init__(self, parent: QObject | None = None, *, runner=None, environ=None, root: str | None = None,
                 uid: int | None = None, set_password=None):
        super().__init__(parent)
        self.runner = runner or context.runner()
        self.environ = context.environ() if environ is None else environ
        self.uid = os.getuid() if uid is None else uid
        self.directory = ac.Directory(self.runner, self.uid, context.root if root is None else root)
        self._set_password = set_password or (lambda path, hashed, done: dbus_set_password(path, hashed, done, self))

    @staticmethod
    def _text(exc: Exception) -> str:
        return user_message(exc)

    def _run(self, action, done) -> None:
        fluent.run_in_thread(action, lambda _result: done(None), lambda exc: done(self._text(exc)))

    def load(self, done) -> None:
        """done(accounts.State)."""
        fluent.run_in_thread(self.directory.load, done,
                             lambda exc: done(ac.State((), self.uid, False, self._text(exc))))

    def set_real_name(self, user: ac.User, name: str, done) -> None:
        self._run(lambda: self.directory.set_real_name(user, name), done)

    def set_account_type(self, user: ac.User, account_type: int, done) -> None:
        self._run(lambda: self.directory.set_account_type(user, account_type), done)

    def set_picture(self, user: ac.User, file_path: str, done) -> None:
        def action() -> None:
            data = picture_png(file_path)
            try:
                face = userfiles.write_face(self.environ, data)
            except userfiles.UserFileError as exc:
                raise Failed(str(exc)) from None
            if user.path:
                self.directory.set_icon_file(user, face)

        self._run(action, done)

    def change_own_password(self, current: str, new: str, done) -> None:
        self._run(lambda: ac.change_own_password(self.runner, current, new), done)

    def set_password(self, user: ac.User, password: str, done) -> None:
        """Another user's password (administrator): a hash for the service's SetPassword."""
        if not user.path:
            done("The password could not be set: the accounts service is not running.")
            return
        self._set_password(user.path, ac.sha512_crypt(password), done)

    def delete_user(self, user: ac.User, remove_files: bool, done) -> None:
        self._run(lambda: self.directory.delete_user(user.uid, remove_files), done)

    def create_user(self, name: str, real_name: str, account_type: int, password: str, done) -> None:
        """Create the account, then set its password; if that fails the account is removed again."""
        created: list[str] = []

        def password_set(error: str | None) -> None:
            if error is None:
                done(None)
                return
            path = created[0]
            uid = int(path.rsplit("User", 1)[1])

            def removed(removal: str | None) -> None:
                if removal is None:
                    done(f"{real_name} was not added: the account's password could not be set. {error}")
                else:
                    done(f"The account {name} was created, but its password could not be set ({error}) and the "
                         f"account could not be removed again ({removal}). It exists now without a password that "
                         "works: use Reset password on it, or remove it.")

            self._run(lambda: self.directory.delete_user(uid, True), removed)

        def made(path: str) -> None:
            created.append(path)
            self._set_password(path, ac.sha512_crypt(password), password_set)

        fluent.run_in_thread(lambda: self.directory.create_user(name, real_name, account_type), made,
                             lambda exc: done(self._text(exc)))


# --- dialogs -----------------------------------------------------------------------------------------------------

class FormDialog(fluent.ContentDialog):
    """A dialog with fields that stays open while its request runs.

    The primary button calls ``submit()``; a subclass checks its fields
    there and emits its own signal. The owner answers with ``fail(text)``
    (the reason is shown and the fields can be corrected) or closes the
    dialog. Password boxes are emptied whenever the dialog closes.
    """

    def __init__(self, parent: QWidget, title: str, text: str, confirm: str):
        super().__init__(parent, title, text, primary=confirm, close="Cancel")
        self.fields: list[QWidget] = []
        self.busy_now = False
        self.status = QWidget(self)
        self.status.setMinimumHeight(STATUS_HEIGHT)       # the dialog is placed once: the room is there from the start
        layout = QVBoxLayout(self.status)
        layout.setContentsMargins(0, 0, 0, 0)
        self.problem = fluent.InfoBar("error", parent=self.status, closable=False)
        self.problem.hide()
        self.busy = Busy("Working…", self.status)
        layout.addWidget(self.problem)
        layout.addWidget(self.busy)
        layout.addStretch(1)
        self.primary_button.clicked.disconnect()
        self.primary_button.clicked.connect(self.submit)

    def add_field(self, label: str, widget: QWidget) -> QWidget:
        caption = fluent.Label(label, "body", self)
        caption.setBuddy(widget)
        if not widget.accessibleName():
            widget.setAccessibleName(label)
        self.content_layout.addWidget(caption)
        self.content_layout.addWidget(widget)
        self.fields.append(widget)
        return widget

    def finish_layout(self) -> None:
        self.content_layout.addWidget(self.status)

    def open(self) -> None:
        super().open()
        if self.fields:
            self.fields[0].setFocus(Qt.FocusReason.PopupFocusReason)

    def submit(self) -> None:
        raise NotImplementedError

    def set_busy(self, busy: bool, text: str = "Working…") -> None:
        self.busy_now = busy
        self.busy.set_busy(busy, text)
        if busy:
            self.problem.hide()
        for field in self.fields:
            if isinstance(field, QLineEdit):
                field.setReadOnly(busy)       # read-only, not disabled: the focus stays where it is
            else:
                field.setEnabled(not busy)
        self.primary_button.setEnabled(not busy)

    def fail(self, text: str, field: QWidget | None = None) -> None:
        self.set_busy(False)
        self.problem.show_message("error", text)
        target = field or (self.fields[0] if self.fields else None)
        if target is not None:
            target.setFocus(Qt.FocusReason.OtherFocusReason)
            if isinstance(target, QLineEdit):
                target.selectAll()

    def done(self, result) -> None:
        for field in self.fields:
            if isinstance(field, fluent.PasswordBox):
                field.clear()
        self.set_busy(False)
        super().done(result)


class NameDialog(FormDialog):
    submitted = Signal(str)

    def __init__(self, parent: QWidget, current: str):
        super().__init__(parent, "Change your name", "The name shown on the sign-in screen and in the menu.", "Save")
        self.name = self.add_field("Full name", QLineEdit(current, self))
        self.name.setMaxLength(ac.MAX_REAL_NAME)
        self.finish_layout()

    def submit(self) -> None:
        name = self.name.text().strip()
        if not ac.valid_real_name(name):
            self.fail("A name cannot be empty, start with '-' or contain ':' or ','.")
            return
        self.set_busy(True, "Saving…")
        self.submitted.emit(name)


class PasswordDialog(FormDialog):
    """New password twice; for the own password also the current one."""

    submitted = Signal(str, str)          # current ("" when not asked), new

    def __init__(self, parent: QWidget, title: str, text: str, *, ask_current: bool, user_name: str = ""):
        super().__init__(parent, title, text, "Change password" if ask_current else "Set password")
        self.user_name = user_name
        self.current = self.add_field("Current password", fluent.PasswordBox(self)) if ask_current else None
        self.new = self.add_field("New password", fluent.PasswordBox(self))
        self.again = self.add_field("Repeat the new password", fluent.PasswordBox(self))
        for box in (self.current, self.new, self.again):
            if box is not None:
                box.setMaxLength(ac.MAX_PASSWORD)
        self.finish_layout()

    def submit(self) -> None:
        if self.busy_now:
            return
        new = self.new.text()
        if self.current is not None and not self.current.text():
            self.fail("Enter your current password.", self.current)
            return
        problem = ac.password_problem(new, self.user_name, ac.password_rules())
        if problem:
            self.fail(problem, self.new)
            return
        if new != self.again.text():
            self.fail("The two new passwords are not the same.", self.again)
            return
        if self.current is not None and new == self.current.text():
            self.fail("The new password must differ from the current one.", self.new)
            return
        self.set_busy(True, "Changing the password…")
        self.submitted.emit(self.current.text() if self.current is not None else "", new)


class AddUserDialog(FormDialog):
    submitted = Signal(str, str, int, str)        # user name, full name, account type, password

    def __init__(self, parent: QWidget, taken: set[str]):
        super().__init__(parent, "Add a user", "", "Add user")
        self.taken = taken
        self.real = self.add_field("Full name", QLineEdit(self))
        self.real.setMaxLength(ac.MAX_REAL_NAME)
        self.name = self.add_field("User name", QLineEdit(self))
        self.name.setMaxLength(32)
        self.kind = QComboBox(self)
        self.kind.addItem(ac.TYPE_LABELS[ac.STANDARD], ac.STANDARD)
        self.kind.addItem(ac.TYPE_LABELS[ac.ADMINISTRATOR], ac.ADMINISTRATOR)
        self.add_field("Account type", self.kind)
        self.password = self.add_field("Password", fluent.PasswordBox(self))
        self.again = self.add_field("Repeat the password", fluent.PasswordBox(self))
        for box in (self.password, self.again):
            box.setMaxLength(ac.MAX_PASSWORD)
        self.finish_layout()
        self._suggested = True                # the user name follows the full name until it is edited
        self.real.textEdited.connect(self._suggest)
        self.name.textEdited.connect(self._name_edited)

    def _suggest(self, text: str) -> None:
        if self._suggested:
            self.name.setText(ac.suggest_user_name(text, self.taken))

    def _name_edited(self, text: str) -> None:
        self._suggested = not text

    def submit(self) -> None:
        if self.busy_now:
            return
        real, name, password = self.real.text().strip(), self.name.text().strip(), self.password.text()
        if not ac.valid_real_name(real):
            self.fail("Enter the person's name (without ':' or ',').", self.real)
            return
        if not ac.valid_user_name(name):
            self.fail("A user name starts with a small letter and has only small letters, digits, '-' and '_'.",
                      self.name)
            return
        if name in self.taken:
            self.fail(f"There is a user named {name} already.", self.name)
            return
        problem = ac.password_problem(password, name, ac.password_rules())
        if problem:
            self.fail(problem, self.password)
            return
        if password != self.again.text():
            self.fail("The two passwords are not the same.", self.again)
            return
        self.set_busy(True, "Adding the user…")
        self.submitted.emit(name, real, self.kind.currentData(), password)


class RemoveDialog(fluent.ContentDialog):
    def __init__(self, parent: QWidget, user: ac.User):
        super().__init__(parent, f"Remove {user.display_name}?",
                         f"The account {user.name} is removed from this device. {user.display_name} can no longer "
                         "sign in.", primary="Remove", close="Cancel", default="close")
        self.files = QCheckBox(f"Also delete this user's files (the home folder of {user.name})", self)
        self.content_layout.addWidget(self.files)


# --- the page -----------------------------------------------------------------------------------------------------

class UserRow(fluent.SettingsExpander):
    type_chosen = Signal(int, int)        # uid, account type
    reset_requested = Signal(int)
    remove_requested = Signal(int)

    def __init__(self, uid: int, parent: QWidget | None = None):
        super().__init__("", "", fluent.icon("avatar-default-symbolic"), None, parent)
        self.uid = uid
        self.user: ac.User | None = None
        self.kind = QComboBox(self)
        self.kind.addItem(ac.TYPE_LABELS[ac.STANDARD], ac.STANDARD)
        self.kind.addItem(ac.TYPE_LABELS[ac.ADMINISTRATOR], ac.ADMINISTRATOR)
        self.kind.setMinimumWidth(180)
        self.kind.activated.connect(lambda _i: self.type_chosen.emit(self.uid, self.kind.currentData()))
        self.type_row = self.add_row("Account type", "Administrators can change settings for everyone and install "
                                     "software", self.kind)
        self.reset_button = fluent.Button("Reset password…", self)
        self.reset_button.clicked.connect(lambda: self.reset_requested.emit(self.uid))
        self.add_row("Password", "Set a new password for this user", self.reset_button)
        self.remove_button = fluent.Button("Remove…", self)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.uid))
        self.remove_row = self.add_row("Remove this user", "", self.remove_button)

    def update_user(self, user: ac.User, state: ac.State, enabled: bool) -> None:
        self.user = user
        self.header.set_title(user.display_name)
        self.header.set_description(f"{user.name} · {user.type_label}" + (" · locked" if user.locked else ""))
        index = 1 if user.administrator else 0
        if self.kind.currentIndex() != index:
            self.kind.setCurrentIndex(index)
        changeable = enabled and state.service
        demotion = state.demotion_problem(user)
        self.kind.setEnabled(changeable and not demotion)
        self.type_row.set_description(demotion or "Administrators can change settings for everyone and install "
                                      "software")
        self.kind.setAccessibleName(f"Account type of {user.display_name}")
        self.reset_button.setEnabled(changeable)
        self.reset_button.setAccessibleName(f"Reset the password of {user.display_name}")
        removal = state.removal_problem(user)
        self.remove_button.setEnabled(changeable and not removal)
        self.remove_button.setAccessibleName(f"Remove {user.display_name}")
        self.remove_row.set_description(removal)


class AccountsSection(Section):
    page = "users"
    title = "Your account"
    order = 10
    keywords = ("account", "user", "users", "password", "change password", "picture", "avatar", "name",
                "administrator", "add user", "remove user", "other users")

    def __init__(self, parent: QWidget | None = None, *, service: AccountsService | None = None):
        super().__init__(parent)
        self._service = service
        self.state = ac.State()
        self.busy = False
        self.loading = False
        self.dialog: fluent.ContentDialog | None = None
        self.choose_file = self._choose_file      # tests replace it

        self.me_card = fluent.Card(self)
        row = QHBoxLayout()
        row.setSpacing(16)
        self.avatar = fluent.Avatar("", self.me_card, size=56)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.me_name = fluent.Label("", "subtitle", self.me_card, wrap=True)
        self.me_detail = fluent.Label("", "body", self.me_card, tone="secondary", wrap=True)
        text.addStretch(1)
        text.addWidget(self.me_name)
        text.addWidget(self.me_detail)
        text.addStretch(1)
        row.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(text, 1)
        self.me_card.add_layout(row)
        self.add(self.me_card)

        self.picture_button = fluent.Button("Change picture…", self)
        self.picture_button.clicked.connect(self.change_picture)
        self.picture_card = self.add(fluent.SettingsCard(
            "Picture", "Shown on the sign-in screen and in the menu", fluent.icon("image-x-generic-symbolic"),
            self.picture_button, self))
        self.name_button = fluent.Button("Change name…", self)
        self.name_button.clicked.connect(self.change_name)
        self.name_card = self.add(fluent.SettingsCard("Name", "", fluent.icon("document-edit-symbolic"),
                                                      self.name_button, self))
        self.password_button = fluent.Button("Change password…", self)
        self.password_button.clicked.connect(self.change_password)
        self.password_card = self.add(fluent.SettingsCard(
            "Password", "You are asked for your current password first", fluent.icon("dialog-password-symbolic"),
            self.password_button, self))

        self.others_title = fluent.Label("Other users", "body-strong", self)
        self.others_title.setContentsMargins(0, 8, 0, 4)
        self.add(self.others_title)
        self.others = RowList(self)
        self.add(self.others)
        self.add_button = fluent.Button("Add user", self, accent=True)
        self.add_button.clicked.connect(self.add_user)
        self.add_card = self.add(fluent.SettingsCard("Add a user", "Create an account for another person",
                                                     fluent.icon("list-add-symbolic"), self.add_button, self))
        self._enable()

    @property
    def service(self) -> AccountsService:
        if self._service is None:
            self._service = AccountsService(self)
        return self._service

    # --- life cycle ---
    def activate(self) -> None:
        if self.active:
            return
        self.active = True
        self.reload()

    def deactivate(self) -> None:
        self.active = False
        if self.dialog is not None and self.dialog.is_open():
            self.dialog.reject()

    def reload(self) -> None:
        if self.loading:
            return
        self.loading = True
        self.service.load(self._loaded)

    def _loaded(self, state: ac.State) -> None:
        self.loading = False
        if not self.active:
            return
        self.state = state
        self.show_notice(state.problem if not state.service else "")
        me = state.me
        self.me_card.setVisible(me is not None)
        if me is not None:
            self.avatar.set_name(me.display_name)
            self.avatar.set_picture(self._picture(me))
            self.me_name.setText(me.display_name)
            self.me_detail.setText(f"{me.name} · {me.type_label}")
            self.name_card.set_description(me.real_name or "No name is set")
        by_uid = {user.uid: user for user in state.others}
        self.others.sync(list(by_uid), self._row,
                         lambda uid, row: row.update_user(by_uid[uid], state, not self.busy))
        self.others_title.setVisible(bool(by_uid))
        self.add_card.set_description("Create an account for another person" if state.service else
                                      "Needs the accounts service")
        self._enable()

    def _picture(self, user: ac.User) -> QImage | None:
        for path in (user.icon_file, os.path.join(self.service.environ.get("HOME") or "/nonexistent", ".face")):
            try:
                if path and os.path.isfile(path) and os.path.getsize(path) <= 4 * 1024 * 1024:
                    image = QImage(path)
                    if not image.isNull():
                        return image
            except OSError:
                continue
        return None

    def _row(self, uid: int) -> UserRow:
        row = UserRow(uid, self.others)
        row.type_chosen.connect(self.set_type)
        row.reset_requested.connect(self.reset_password)
        row.remove_requested.connect(self.remove_user)
        return row

    def _enable(self) -> None:
        me, service, free = self.state.me, self.state.service, not self.busy
        self.picture_button.setEnabled(free and me is not None)
        self.name_button.setEnabled(free and me is not None and service)
        self.password_button.setEnabled(free and me is not None)
        self.add_button.setEnabled(free and service)
        for uid in self.others.keys():
            row = self.others.row(uid)
            if row.user is not None:
                row.update_user(row.user, self.state, free)

    def _user(self, uid: int) -> ac.User | None:
        return next((user for user in self.state.users if user.uid == uid), None)

    # --- changes ---
    def _finish(self, error: str | None, done_text: str = "", dialog: FormDialog | None = None) -> None:
        """A request ended: tell the dialog or the page, then show the real state again."""
        self.busy = False
        if error is not None and dialog is not None and dialog.is_open():
            dialog.fail(error)
        else:
            if dialog is not None and dialog.is_open():
                dialog.done(fluent.ContentDialog.Result.PRIMARY)
            if error is not None:
                self.show_error(error)
            elif done_text and self.active:
                self.clear_error()
                self.show_notice(done_text, "success")
        if self.active:
            self._enable()
            self.reload()

    def _start(self) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.clear_error()
        self._enable()
        return True

    def _open(self, dialog: fluent.ContentDialog) -> None:
        self.dialog = dialog

        def closed(_result=None) -> None:
            if self.dialog is dialog:
                self.dialog = None
            dialog.deleteLater()

        dialog.finished.connect(closed)
        dialog.open()

    def _choose_file(self) -> str:
        start = os.path.join(self.service.environ.get("HOME") or "", "Pictures")
        path, _filter = QFileDialog.getOpenFileName(self.window(), "Choose a picture", start, PICTURE_FILTER)
        return path

    def change_picture(self) -> None:
        me = self.state.me
        path = self.choose_file() if me is not None else ""
        if not path or not self._start():
            return
        self.service.set_picture(me, path, lambda error: self._finish(error, "Your picture was changed."))

    def change_name(self) -> None:
        me = self.state.me
        if me is None:
            return
        dialog = NameDialog(self, me.real_name)

        def submitted(name: str) -> None:
            if not self._start():
                return
            self.service.set_real_name(me, name, lambda error: self._finish(error, "Your name was changed.", dialog))

        dialog.submitted.connect(submitted)
        self._open(dialog)

    def change_password(self) -> None:
        me = self.state.me
        if me is None:
            return
        dialog = PasswordDialog(self, "Change your password", "", ask_current=True, user_name=me.name)

        def submitted(current: str, new: str) -> None:
            if not self._start():
                return
            self.service.change_own_password(current, new, lambda error: self._finish(
                error, "Your password was changed.", dialog))

        dialog.submitted.connect(submitted)
        self._open(dialog)

    def reset_password(self, uid: int) -> None:
        user = self._user(uid)
        if user is None:
            return
        dialog = PasswordDialog(self, f"Set a new password for {user.display_name}",
                                "The user signs in with this password from now on.", ask_current=False,
                                user_name=user.name)

        def submitted(_current: str, new: str) -> None:
            if not self._start():
                return
            self.service.set_password(user, new, lambda error: self._finish(
                error, f"The password of {user.display_name} was changed.", dialog))

        dialog.submitted.connect(submitted)
        self._open(dialog)

    def set_type(self, uid: int, account_type: int) -> None:
        user = self._user(uid)
        if user is None or user.account_type == account_type:
            return
        problem = self.state.demotion_problem(user) if account_type != ac.ADMINISTRATOR else ""
        if problem or not self._start():
            if problem:
                self.show_error(problem)
            self._enable()
            return
        self.service.set_account_type(user, account_type, lambda error: self._finish(
            error, f"{user.display_name} is now {'an administrator' if account_type == ac.ADMINISTRATOR else 'a standard user'}."))

    def remove_user(self, uid: int) -> None:
        user = self._user(uid)
        if user is None:
            return
        problem = self.state.removal_problem(user)
        if problem:
            self.show_error(problem)
            return
        dialog = RemoveDialog(self, user)

        def closed(result) -> None:
            remove_files = dialog.files.isChecked()
            if result != fluent.ContentDialog.Result.PRIMARY or not self._start():
                return
            self.service.delete_user(user, remove_files, lambda error: self._finish(
                error, f"{user.display_name} was removed."))

        dialog.finished.connect(closed)
        self._open(dialog)

    def add_user(self) -> None:
        dialog = AddUserDialog(self, {user.name for user in self.state.users})

        def submitted(name: str, real: str, account_type: int, password: str) -> None:
            if not self._start():
                return
            self.service.create_user(name, real, account_type, password, lambda error: self._finish(
                error, f"{real} was added.", dialog))

        dialog.submitted.connect(submitted)
        self._open(dialog)


SECTIONS = (AccountsSection,)
