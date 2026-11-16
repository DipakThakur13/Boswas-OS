"""User accounts without Qt: what AccountsService says, the passwd conversation, and password hashes.

Reading accounts and every change except passwords goes through busctl
(systemd), started by commands.py with the argument lists of
commands.accounts_argv(): ListCachedUsers, the properties of each user,
CreateUser, DeleteUser, SetRealName, SetAccountType, SetIconFile. Names and
numbers on a command line are not secrets. AccountsService asks polkit for
the changes; the shell's agent shows the question.

Nothing secret is ever part of a command line:

  * the user's own password is changed by /usr/bin/passwd on a
    pseudo-terminal (commands.CommandRunner.converse), so PAM's rules apply.
    PasswdDialogue answers its prompts and turns its messages into plain
    words;
  * another user's password is set with AccountsService's SetPassword over
    D-Bus (ui/settings/accounts.py), which takes a crypt(3) hash:
    sha512_crypt() makes it ("$6$", Drepper's "Unix crypt using SHA-256 and
    SHA-512", proven against the specification's test vectors).

Without AccountsService the accounts are read from /etc/passwd (uid 1000 to
59999 with a login shell) and nothing but the own password can be changed.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass

from . import commands
from .errors import CommandFailed, Failed, Unavailable

STANDARD, ADMINISTRATOR = 0, 1
TYPE_LABELS = {STANDARD: "Standard user", ADMINISTRATOR: "Administrator"}
USER_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
MIN_UID, MAX_UID = 1000, 59999
MAX_USERS = 200
MAX_REAL_NAME = 120
MIN_PASSWORD, MAX_PASSWORD = 8, 256
READ_TIMEOUT = 15.0
CHANGE_TIMEOUT = 180.0             # a change may wait for the person to answer polkit's question
PASSWD_TIMEOUT = 60.0
NO_LOGIN_SHELLS = ("nologin", "false", "sync", "shutdown", "halt")
DENIED_WORDS = ("not authorized", "notauthorized", "permission denied", "permissiondenied", "access denied",
                "accessdenied", "interactive authentication required", "authentication is required")
NOT_RUNNING_WORDS = ("serviceunknown", "service unknown", "name has no owner", "namehasnoowner",
                     "failed to connect", "not provided by any", "no such file or directory", "host is down",
                     "was not provided", "could not activate", "failed to activate")
SERVICE_MISSING = ("Changing accounts needs the accounts service (package accountsservice), which is not running "
                   "on this system. The accounts below are read from the system's user list.")


# --- SHA-512 crypt ($6$) ------------------------------------------------------------------------------------------

_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SALT_CHARACTERS = _ITOA64
ROUNDS_DEFAULT, ROUNDS_MIN, ROUNDS_MAX = 5000, 1000, 999_999_999
_ORDER = ((0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26), (6, 27, 48), (28, 49, 7),
          (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13), (56, 14, 35),
          (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19), (62, 20, 41))


def make_salt(length: int = 16) -> str:
    """A random salt of the crypt alphabet (from the system's random source)."""
    return "".join(secrets.choice(SALT_CHARACTERS) for _ in range(length))


def sha512_crypt(password: str | bytes, salt: str | None = None, rounds: int | None = None) -> str:
    """The "$6$" hash of crypt(3) for a password (Ulrich Drepper's SHA-512 crypt, version 0.6).

    ``salt`` is up to 16 characters (a random one when None); it may also be
    a whole prefix "$6$[rounds=N$]salt", as crypt(3) takes it. ``rounds``
    (1000 to 999999999) appears in the result when it is given.
    """
    key = password.encode("utf-8") if isinstance(password, str) else bytes(password)
    if salt is None:
        salt = make_salt()
    if salt.startswith("$6$"):
        salt = salt[3:]
    if salt.startswith("rounds="):
        number, _sep, salt = salt[len("rounds="):].partition("$")
        if not number.isdigit():
            raise ValueError("rounds must be a number")
        rounds = int(number)
    salt = salt.split("$")[0][:16]
    if any(ch not in SALT_CHARACTERS for ch in salt) and not salt.isascii():
        raise ValueError("the salt must be ASCII")
    explicit = rounds is not None
    count = max(ROUNDS_MIN, min(ROUNDS_MAX, rounds)) if explicit else ROUNDS_DEFAULT
    salt_bytes = salt.encode("ascii")
    sha = hashlib.sha512

    digest_b = sha(key + salt_bytes + key).digest()
    a = sha(key + salt_bytes)
    length = len(key)
    a.update(digest_b * (length // 64) + digest_b[:length % 64])
    bits = length
    while bits > 0:
        a.update(digest_b if bits & 1 else key)
        bits >>= 1
    digest_a = a.digest()
    digest_dp = sha(key * length).digest()
    p = (digest_dp * (length // 64 + 1))[:length]
    digest_ds = sha(salt_bytes * (16 + digest_a[0])).digest()
    s = (digest_ds * (len(salt_bytes) // 64 + 1))[:len(salt_bytes)]

    c = digest_a
    for index in range(count):
        step = sha(p if index & 1 else c)
        if index % 3:
            step.update(s)
        if index % 7:
            step.update(p)
        step.update(c if index & 1 else p)
        c = step.digest()

    out = []
    for b2, b1, b0 in _ORDER:
        word = (c[b2] << 16) | (c[b1] << 8) | c[b0]
        for _ in range(4):
            out.append(_ITOA64[word & 0x3F])
            word >>= 6
    word = c[63]
    for _ in range(2):
        out.append(_ITOA64[word & 0x3F])
        word >>= 6
    return f"$6${f'rounds={count}$' if explicit else ''}{salt}${''.join(out)}"


# --- names --------------------------------------------------------------------------------------------------------

def valid_user_name(name: object) -> bool:
    return isinstance(name, str) and bool(USER_NAME_RE.fullmatch(name))


def valid_real_name(name: object) -> bool:
    """A full name for the GECOS field: printable, no ':' or ',' (they separate fields), not starting with '-'."""
    return (isinstance(name, str) and 0 < len(name.strip()) <= MAX_REAL_NAME and name == name.strip()
            and not name.startswith("-") and ":" not in name and "," not in name
            and all(ch.isprintable() for ch in name))


def valid_uid(uid: object) -> bool:
    return isinstance(uid, int) and not isinstance(uid, bool) and 0 <= uid <= 0xFFFFFFFE


def suggest_user_name(real_name: str, taken=()) -> str:
    """A user name for a full name: "Ada Lovelace" -> "ada", then "adal", "ada2", ... when taken."""
    plain = unicodedata.normalize("NFKD", real_name).encode("ascii", "ignore").decode("ascii").lower()
    words = [re.sub(r"[^a-z0-9]", "", word) for word in plain.split()]
    words = [word for word in words if word]
    if not words:
        return ""
    first = re.sub(r"^[^a-z_]+", "", words[0])[:32]
    candidates = [first] + ([(first + words[-1][:1])[:32]] if len(words) > 1 else [])
    candidates += [f"{first[:30]}{number}" for number in range(2, 100)]
    for name in candidates:
        if valid_user_name(name) and name not in taken:
            return name
    return ""


def password_problem(password: str, user_name: str = "") -> str:
    """Why a new password is not accepted here, in words for the user; "" when it is fine."""
    if len(password) < MIN_PASSWORD:
        return f"The password needs at least {MIN_PASSWORD} characters."
    if len(password.encode("utf-8")) > MAX_PASSWORD:
        return "The password is too long."
    if "\0" in password or "\n" in password or "\r" in password:
        return "The password contains a character that cannot be used."
    if user_name and password.casefold() == user_name.casefold():
        return "The password must not be the user name."
    return ""


@dataclass(frozen=True)
class User:
    uid: int
    name: str
    real_name: str = ""
    account_type: int = STANDARD
    icon_file: str = ""
    locked: bool = False
    path: str = ""                    # the AccountsService object path; "" for an account read from /etc/passwd

    @property
    def display_name(self) -> str:
        return self.real_name or self.name

    @property
    def administrator(self) -> bool:
        return self.account_type == ADMINISTRATOR

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.account_type, TYPE_LABELS[STANDARD])


@dataclass(frozen=True)
class State:
    users: tuple[User, ...] = ()
    uid: int = -1                     # who is signed in
    service: bool = False             # AccountsService answers: accounts can be changed
    problem: str = ""                 # why not, in words for the user

    @property
    def me(self) -> User | None:
        return next((user for user in self.users if user.uid == self.uid), None)

    @property
    def others(self) -> tuple[User, ...]:
        return tuple(user for user in self.users if user.uid != self.uid)

    def administrators(self) -> int:
        return sum(1 for user in self.users if user.administrator and not user.locked)

    def removal_problem(self, user: User) -> str:
        """Why this account must not be removed; "" when it may be."""
        if user.uid == self.uid:
            return "You cannot remove the account you are signed in with."
        if user.administrator and self.administrators() <= 1:
            return "This is the only administrator of this device. Make another user an administrator first."
        return ""

    def demotion_problem(self, user: User) -> str:
        if user.administrator and self.administrators() <= 1:
            return "This is the only administrator of this device. Make another user an administrator first."
        return ""


# --- what busctl prints ------------------------------------------------------------------------------------------------

def _reply(text: object) -> list:
    """The arguments of a method reply printed by `busctl --json=short call` ([] when there are none)."""
    if not isinstance(text, str) or len(text) > 4 * 1024 * 1024:
        raise ValueError("no reply")
    text = text.strip()
    if not text:
        return []
    doc = json.loads(text.splitlines()[0])
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise ValueError("not a reply")
    return doc["data"]


def parse_user_paths(text: object) -> list[str]:
    """The object paths of ListCachedUsers (valid ones, at most MAX_USERS)."""
    try:
        data = _reply(text)
    except (ValueError, RecursionError):
        return []
    paths = data[0] if data and isinstance(data[0], list) else []
    found = [path for path in paths if isinstance(path, str) and commands.ACCOUNTS_USER_RE.fullmatch(path)]
    return list(dict.fromkeys(found))[:MAX_USERS]


def parse_created_path(text: object) -> str:
    """The object path CreateUser returned; "" when the reply is not one."""
    try:
        data = _reply(text)
    except (ValueError, RecursionError):
        return ""
    path = data[0] if data else None
    return path if isinstance(path, str) and commands.ACCOUNTS_USER_RE.fullmatch(path) else ""


def parse_user(text: object, path: str) -> User | None:
    """A user from the reply of Properties.GetAll; None for a system account or a reply that is not one."""
    try:
        data = _reply(text)
    except (ValueError, RecursionError):
        return None
    raw = data[0] if data and isinstance(data[0], dict) else {}

    def value(key: str):
        item = raw.get(key)
        return item.get("data") if isinstance(item, dict) else None

    uid, name = value("Uid"), value("UserName")
    if not valid_uid(uid) or not valid_user_name(name) or value("SystemAccount") is True:
        return None
    real = value("RealName")
    real = "".join(ch for ch in real if ch.isprintable()).strip()[:MAX_REAL_NAME] if isinstance(real, str) else ""
    icon = value("IconFile")
    icon = icon if isinstance(icon, str) and icon.startswith("/") and "\0" not in icon and len(icon) < 4096 else ""
    return User(uid, name, real, ADMINISTRATOR if value("AccountType") == ADMINISTRATOR else STANDARD, icon,
                value("Locked") is True, path)


def parse_passwd(text: object) -> list[User]:
    """The people's accounts of /etc/passwd: uid 1000 to 59999 with a login shell."""
    users = []
    if not isinstance(text, str):
        return users
    for line in text.splitlines()[:5000]:
        fields = line.split(":")
        if len(fields) != 7 or not valid_user_name(fields[0]) or not fields[2].isdigit():
            continue
        uid = int(fields[2])
        if not MIN_UID <= uid <= MAX_UID or fields[6].rsplit("/", 1)[-1] in NO_LOGIN_SHELLS or not fields[6]:
            continue
        real = "".join(ch for ch in fields[4].split(",")[0] if ch.isprintable()).strip()[:MAX_REAL_NAME]
        users.append(User(uid, fields[0], real))
        if len(users) >= MAX_USERS:
            break
    return sorted(users, key=lambda user: (user.display_name.casefold(), user.uid))


def failure_text(what: str, result: commands.Completed) -> str:
    """Why a busctl call failed, in words for the user."""
    detail = " ".join((result.stderr or result.stdout or "").split())[:300]
    lowered = detail.lower()
    if any(word in lowered for word in DENIED_WORDS):
        return f"{what} was not allowed: administrator authorization was not given."
    if "already exists" in lowered or "userexists" in lowered:
        return f"{what} failed: a user with this name exists already."
    if detail.startswith("Call failed: "):
        detail = detail[len("Call failed: "):]
    return f"{what} failed" + (f": {detail}" if detail else ".")


def service_missing(result: commands.Completed) -> bool:
    lowered = (result.stderr or "").lower()
    return any(word in lowered for word in NOT_RUNNING_WORDS)


class Directory:
    """The accounts of this system, read and changed through busctl (blocking: call from a worker thread)."""

    def __init__(self, runner, uid: int, root: str = "/"):
        self.runner, self.uid, self.root = runner, uid, root

    def _call(self, what: str, command: list[str], timeout: float) -> commands.Completed:
        result = self.runner.run(command, timeout)
        if result.code != 0:
            raise Failed(failure_text(what, result))
        return result

    def _fallback(self, problem: str) -> State:
        try:
            with open(self.root.rstrip("/") + "/etc/passwd", "rb") as handle:
                text = handle.read(1024 * 1024).decode("utf-8", "replace")
        except OSError:
            text = ""
        return State(tuple(parse_passwd(text)), self.uid, False, problem)

    def load(self) -> State:
        """Every person's account. Never raises: without the service, the accounts of /etc/passwd."""
        try:
            listed = self.runner.run(commands.accounts_argv("ListCachedUsers"), READ_TIMEOUT)
        except (Unavailable, CommandFailed):
            return self._fallback(SERVICE_MISSING)
        if listed.code != 0:
            return self._fallback(SERVICE_MISSING)
        users = []
        for path in parse_user_paths(listed.stdout):
            try:
                result = self.runner.run(commands.accounts_argv("GetAll", path=path), READ_TIMEOUT)
            except (Unavailable, CommandFailed):
                continue
            user = parse_user(result.stdout, path) if result.code == 0 else None
            if user is not None:
                users.append(user)
        if not any(user.uid == self.uid for user in users):
            # AccountsService lists who has signed in before; the own account is asked for by name of its path.
            path = f"{commands.ACCOUNTS_PATH}/User{self.uid}"
            try:
                result = self.runner.run(commands.accounts_argv("GetAll", path=path), READ_TIMEOUT)
                user = parse_user(result.stdout, path) if result.code == 0 else None
            except (Unavailable, CommandFailed):
                user = None
            if user is not None and user.uid == self.uid:
                users.append(user)
        users.sort(key=lambda user: (user.display_name.casefold(), user.uid))
        return State(tuple(users), self.uid, True, "")

    def set_real_name(self, user: User, name: str) -> None:
        if not valid_real_name(name):
            raise Failed("A name cannot be empty, start with '-' or contain ':' or ','.")
        self._call("Changing the name", commands.accounts_argv("SetRealName", name, path=user.path), CHANGE_TIMEOUT)

    def set_account_type(self, user: User, account_type: int) -> None:
        self._call("Changing the account type",
                   commands.accounts_argv("SetAccountType", ADMINISTRATOR if account_type == ADMINISTRATOR
                                          else STANDARD, path=user.path), CHANGE_TIMEOUT)

    def set_icon_file(self, user: User, file_path: str) -> None:
        self._call("Changing the picture", commands.accounts_argv("SetIconFile", file_path, path=user.path),
                   CHANGE_TIMEOUT)

    def create_user(self, name: str, real_name: str, account_type: int) -> str:
        """Create the account; returns its object path."""
        if not valid_user_name(name):
            raise Failed("A user name starts with a small letter and has only small letters, digits, '-' and '_'.")
        if not valid_real_name(real_name):
            raise Failed("A name cannot be empty, start with '-' or contain ':' or ','.")
        result = self._call("Adding the user", commands.accounts_argv(
            "CreateUser", name, real_name, ADMINISTRATOR if account_type == ADMINISTRATOR else STANDARD),
            CHANGE_TIMEOUT)
        path = parse_created_path(result.stdout)
        if not path:
            raise Failed("The user was added, but the accounts service gave an answer Control Center does not "
                         "understand.")
        return path

    def delete_user(self, uid: int, remove_files: bool) -> None:
        if uid == self.uid:
            raise Failed("You cannot remove the account you are signed in with.")
        self._call("Removing the user", commands.accounts_argv("DeleteUser", uid, bool(remove_files)), CHANGE_TIMEOUT)


# --- the conversation with passwd -------------------------------------------------------------------------------------

_CURRENT_RE = re.compile(r"(current|old|\(current\))[^\n]*password[^\n:]*:\s*$", re.IGNORECASE)
_RETYPE_RE = re.compile(r"(retype|re-enter|reenter|repeat|again|confirm)[^\n]*password[^\n:]*:\s*$", re.IGNORECASE)
_NEW_RE = re.compile(r"new[^\n]*password[^\n:]*:\s*$", re.IGNORECASE)
_PROMPT_RE = re.compile(r"[^\n]{1,200}:\s*$")
_MESSAGES = (
    ("authentication token manipulation error", "The current password is not correct."),
    ("authentication failure", "The current password is not correct."),
    ("you must choose a longer password", "The new password is too short."),
    ("password unchanged", "The new password must differ from the current one."),
    ("has been already used", "This password was used before. Choose another one."),
    ("you must wait longer", "The password was changed too recently and cannot be changed again yet."),
    ("passwords do not match", "The two new passwords were not the same."),
    ("no password has been supplied", "No password was given."),
)


class PasswdDialogue:
    """Answers the prompts of passwd and tells how it went.

    ``feed(text)`` takes what passwd printed and returns the lines to type
    (each exactly once: a prompt that comes a second time means the answer
    was refused). ``stop`` becomes True when the program should be ended
    because nothing more can be answered. After the program ended,
    ``outcome(exit_code)`` is "" for success or the reason in words for
    the user. Nothing typed is kept in the text shown: PAM's own sentence
    is only passed on with both passwords removed from it.
    """

    def __init__(self, current: str, new: str):
        self._current, self._new = current, new
        self._sent = {"current": False, "new": False, "retype": False}
        self._tail = ""                   # what was printed since the last answer
        self.transcript = ""              # everything printed (bounded), for outcome()
        self.stop = False
        self.problem = ""

    def feed(self, text: str) -> list[str]:
        if self.stop:
            return []
        self.transcript = (self.transcript + text)[-8192:]
        self._tail = (self._tail + text)[-2048:]
        tail = self._tail.replace("\r", "")
        answers: list[str] = []
        if _RETYPE_RE.search(tail):
            kind, answer = "retype", self._new
        elif _CURRENT_RE.search(tail):
            kind, answer = "current", self._current
        elif _NEW_RE.search(tail):
            kind, answer = "new", self._new
        elif _PROMPT_RE.search(tail) and not tail.rstrip().endswith((".", "!")) and "password" not in \
                tail.rstrip().splitlines()[-1].lower().replace("password:", ""):
            line = tail.rstrip().splitlines()[-1]
            if line.lower().startswith(("changing password", "passwd:")):
                return []
            self.stop = True
            self.problem = "The password program asked a question Control Center cannot answer (“" + \
                self._clean(line) + "”). Nothing was changed."
            return []
        else:
            return []
        if self._sent[kind]:
            # Asked again: the answer before was refused (a wrong current password, a rejected new one).
            self.stop = True
            return []
        self._sent[kind] = True
        self._tail = ""
        answers.append(answer)
        return answers

    def _clean(self, text: str) -> str:
        for secret in (self._current, self._new):
            if secret:
                text = text.replace(secret, "")
        return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())[:200]

    def outcome(self, code: int | None) -> str:
        """"" when the password was changed; else why not."""
        if self.problem:
            return self.problem
        text = self.transcript.replace("\r", "")
        lowered = text.lower()
        if code == 0 and not self.stop and "updated successfully" in lowered:
            return ""
        if code == 0 and not self.stop and self._sent["new"] and self._sent["retype"]:
            return ""
        for line in text.splitlines():
            if line.strip().upper().startswith("BAD PASSWORD:"):
                return "The new password was not accepted: " + self._clean(line.strip()[len("BAD PASSWORD:"):])
        for marker, words in _MESSAGES:
            if marker in lowered:
                return words
        if self.stop and self._sent["current"] and not self._sent["new"]:
            return "The current password is not correct."
        lines = [self._clean(line) for line in text.splitlines()]
        lines = [line for line in lines if line and not _PROMPT_RE.fullmatch(line + " ")
                 and not line.lower().startswith("changing password")]
        detail = lines[-1] if lines else ""
        if detail.lower().startswith("passwd: "):
            detail = detail[len("passwd: "):]
        return "The password was not changed" + (f": {detail}" if detail else ".")


def change_own_password(runner, current: str, new: str, timeout: float = PASSWD_TIMEOUT) -> None:
    """Change the signed-in user's password with passwd on a pseudo-terminal; Failed with the reason if not."""
    dialogue = PasswdDialogue(current, new)
    try:
        code = runner.converse(commands.passwd_argv(), dialogue, timeout)
    except CommandFailed as exc:
        raise Failed("The password was not changed: the password program did not finish in time."
                     if "did not finish" in exc.message else f"The password was not changed. {exc.message}") from None
    problem = dialogue.outcome(code)
    if problem:
        raise Failed(problem)
