"""User accounts without Qt: SHA-512 crypt, names, what busctl prints, and the conversation with passwd.

passwd itself is never run: the pseudo-terminal runner is tested against
stand-in programs that print passwd's prompts and messages (success, a wrong
current password, a rejected new password, an unexpected question, a hang).

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402

from boswas_control_center import accounts as ac  # noqa: E402
from boswas_control_center import commands  # noqa: E402
from boswas_control_center.errors import CommandFailed, CommandUnavailable, Failed  # noqa: E402

BUSCTL = commands.EXECUTABLES["busctl"]
PREFIX = [BUSCTL, "--system", "--json=short"]

# Ulrich Drepper, "Unix crypt using SHA-256 and SHA-512", the SHA-512 test vectors: (salt argument, key, result).
VECTORS = (
    ("$6$saltstring", "Hello world!",
     "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJuesI68u4OTLiBFdcbYEdFCoEOfaS35inz1"),
    ("$6$rounds=10000$saltstringsaltstring", "Hello world!",
     "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHbbMCVNSnCM/UrjmM0Dp8vOuZeHBy/"
     "YTBmSK6H9qs/y3RnOaw5v."),
    ("$6$rounds=5000$toolongsaltstring", "This is just a test",
     "$6$rounds=5000$toolongsaltstrin$lQ8jolhgVRVhY4b5pZKaysCLi0QBxGoNeKQzQ3glMhwllF7oGDZxUhx1yxdYcz/"
     "e1JSbq3y6JMxxl8audkUEm0"),
    ("$6$rounds=1400$anotherlongsaltstring",
     "a very much longer text to encrypt.  This one even stretches over morethan one line.",
     "$6$rounds=1400$anotherlongsalts$POfYwTEok97VWcjxIiSOjiykti.o/pQs.wPvMxQ6Fm7I6IoYN3CmLs66x9t0oSwbtEW7o7"
     "UmJEiDwGqd8p4ur1"),
    ("$6$rounds=77777$short", "we have a short salt string but not a short password",
     "$6$rounds=77777$short$WuQyW2YR.hBNpjjRhpYD/ifIw05xdfeEyQoMxIXbkvr0gge1a1x3yRULJ5CCaUeOxFmtlcGZelFl5Cxtgf"
     "iAc0"),
    ("$6$rounds=123456$asaltof16chars..", "a short string",
     "$6$rounds=123456$asaltof16chars..$BtCwjqMJGx5hrJhZywWvt0RLE8uZ4oPwcelCjmw2kSYu.Ec6ycULevoBK25fs2xXgMNrCz"
     "IMVcgEJAstJeonj1"),
)


class CryptTest(unittest.TestCase):
    def test_specification_vectors(self):
        for salt, key, expected in VECTORS:
            self.assertEqual(ac.sha512_crypt(key, salt), expected, salt)

    def test_salt_rounds_and_forms(self):
        first = ac.sha512_crypt("Hello world!", "saltstring")
        self.assertEqual(first, VECTORS[0][2])                           # the bare salt and the "$6$" prefix agree
        self.assertEqual(ac.sha512_crypt("Hello world!", "saltstringsaltstring", 10000), VECTORS[1][2])
        self.assertEqual(ac.sha512_crypt(b"Hello world!", "$6$saltstring$ignored-old-hash"), VECTORS[0][2])
        low = ac.sha512_crypt("the minimum number is still observed", "$6$rounds=10$roundstoolow")
        self.assertTrue(low.startswith("$6$rounds=1000$roundstoolow$"))  # fewer than 1000 rounds become 1000
        self.assertEqual(len(low.rsplit("$", 1)[1]), 86)
        generated = ac.sha512_crypt("correct horse battery staple")
        kind, salt, digest = generated.split("$")[1:]
        self.assertEqual(kind, "6")
        self.assertEqual(len(salt), 16)
        self.assertTrue(set(salt) <= set(ac.SALT_CHARACTERS))
        self.assertEqual(len(digest), 86)
        self.assertNotEqual(generated, ac.sha512_crypt("correct horse battery staple"))   # a new salt each time
        self.assertEqual(ac.sha512_crypt("correct horse battery staple", salt), generated)
        self.assertNotIn("correct", generated)
        self.assertNotEqual(ac.sha512_crypt("a" * 200, "x"), ac.sha512_crypt("a" * 201, "x"))
        self.assertEqual(len(ac.sha512_crypt("", "x").rsplit("$", 1)[1]), 86)
        self.assertEqual(ac.sha512_crypt("pässwörd", "abc"), ac.sha512_crypt("pässwörd".encode("utf-8"), "abc"))
        for bad in ("sält", "a:b", "a\nb", "$6$rounds=many$x"):
            with self.assertRaises(ValueError, msg=bad):
                ac.sha512_crypt("x", bad)
        self.assertEqual(len({ac.make_salt() for _ in range(50)}), 50)


class NameTest(unittest.TestCase):
    def test_user_names(self):
        for good in ("alice", "a", "_svc", "bob-2", "x" * 32, "ada_l"):
            self.assertTrue(ac.valid_user_name(good), good)
        for bad in ("", "Alice", "2fast", "-x", "a b", "a.b", "x" * 33, "root;id", "ä", None, 5, "a\n"):
            self.assertFalse(ac.valid_user_name(bad), repr(bad))

    def test_real_names(self):
        for good in ("Ada Lovelace", "Jürgen Müller-Lüdenscheidt", "李小龍", "O'Neil", "A"):
            self.assertTrue(ac.valid_real_name(good), good)
        for bad in ("", "  ", " Ada", "Ada ", "a:b", "Doe, John", "-n", "a\nb", "a\tb", "a\x1bb", "x" * 121, None):
            self.assertFalse(ac.valid_real_name(bad), repr(bad))

    def test_suggestions(self):
        self.assertEqual(ac.suggest_user_name("Ada Lovelace"), "ada")
        self.assertEqual(ac.suggest_user_name("Ada Lovelace", {"ada"}), "adal")
        self.assertEqual(ac.suggest_user_name("Ada Lovelace", {"ada", "adal"}), "ada2")
        self.assertEqual(ac.suggest_user_name("Jürgen Müller"), "jurgen")
        self.assertEqual(ac.suggest_user_name("  3D Printer  "), "d")
        self.assertEqual(ac.suggest_user_name("李小龍"), "")
        self.assertEqual(ac.suggest_user_name(""), "")
        self.assertTrue(ac.valid_user_name(ac.suggest_user_name("X" * 80)))

    def test_password_rules(self):
        self.assertEqual(ac.password_problem("long enough"), "")
        self.assertIn("at least 8", ac.password_problem("short"))
        self.assertIn("too long", ac.password_problem("x" * 300))
        self.assertIn("cannot be used", ac.password_problem("line\nbreak in it"))
        self.assertIn("user name", ac.password_problem("AliceAlice", "alicealice"))


LIST = '{"type":"ao","data":[["/org/freedesktop/Accounts/User1000","/org/freedesktop/Accounts/User1001"]]}\n'


def properties(uid, name, real="", kind=0, icon="", locked=False, system=False) -> str:
    return json.dumps({"type": "a{sv}", "data": [{
        "Uid": {"type": "t", "data": uid}, "UserName": {"type": "s", "data": name},
        "RealName": {"type": "s", "data": real}, "AccountType": {"type": "i", "data": kind},
        "HomeDirectory": {"type": "s", "data": f"/home/{name}"}, "Shell": {"type": "s", "data": "/bin/bash"},
        "IconFile": {"type": "s", "data": icon}, "Locked": {"type": "b", "data": locked},
        "SystemAccount": {"type": "b", "data": system}, "LocalAccount": {"type": "b", "data": True},
        "LoginHistory": {"type": "a(xxa{sv})", "data": []}}]}) + "\n"


class ParseTest(unittest.TestCase):
    def test_realistic_replies(self):
        self.assertEqual(ac.parse_user_paths(LIST), ["/org/freedesktop/Accounts/User1000",
                                                     "/org/freedesktop/Accounts/User1001"])
        user = ac.parse_user(properties(1000, "alice", "Alice Example", 1, "/var/lib/AccountsService/icons/alice"),
                             "/org/freedesktop/Accounts/User1000")
        self.assertEqual(user, ac.User(1000, "alice", "Alice Example", ac.ADMINISTRATOR,
                                       "/var/lib/AccountsService/icons/alice", False,
                                       "/org/freedesktop/Accounts/User1000"))
        self.assertEqual((user.display_name, user.type_label, user.administrator), ("Alice Example",
                                                                                  "Administrator", True))
        plain = ac.parse_user(properties(1001, "bob"), "/org/freedesktop/Accounts/User1001")
        self.assertEqual((plain.display_name, plain.type_label), ("bob", "Standard user"))
        self.assertEqual(ac.parse_created_path('{"type":"o","data":["/org/freedesktop/Accounts/User1002"]}\n'),
                         "/org/freedesktop/Accounts/User1002")

    def test_hostile_replies(self):
        for text in (None, 5, "", "not json", "[]", '{"type":"ao"}', '{"data":"x"}', '{"data":[5]}', "{" * 5000,
                     '{"data":[["../x","/org/freedesktop/Accounts/User","/org/freedesktop/Accounts/User1; rm",'
                     ' 7, "/org/freedesktop/Accounts/User99999999999"]]}'):
            self.assertEqual(ac.parse_user_paths(text), [], repr(text)[:40])
            self.assertIsNone(ac.parse_user(text, "/org/freedesktop/Accounts/User1"))
            self.assertEqual(ac.parse_created_path(text), "")
        many = json.dumps({"data": [[f"/org/freedesktop/Accounts/User{i}" for i in range(5000)]]})
        self.assertEqual(len(ac.parse_user_paths(many)), ac.MAX_USERS)
        path = "/org/freedesktop/Accounts/User1000"
        self.assertIsNone(ac.parse_user(properties(1000, "daemon", system=True), path))     # not a person
        self.assertIsNone(ac.parse_user(properties(1000, "Bad Name"), path))
        self.assertIsNone(ac.parse_user(properties(-5, "alice"), path))
        self.assertIsNone(ac.parse_user(properties("1000", "alice"), path))
        odd = ac.parse_user(properties(1000, "alice", "Evil\nName\x1b[31m" + "x" * 500, "admin", "relative/icon"),
                            path)
        self.assertNotIn("\n", odd.real_name)
        self.assertNotIn("\x1b", odd.real_name)
        self.assertLessEqual(len(odd.real_name), ac.MAX_REAL_NAME)
        self.assertEqual((odd.account_type, odd.icon_file), (ac.STANDARD, ""))

    def test_etc_passwd(self):
        text = ("root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
                "alice:x:1000:1000:Alice Example,,,:/home/alice:/bin/bash\nbob:x:1001:1001::/home/bob:/bin/zsh\n"
                "svc:x:1002:1002:Service:/srv:/usr/sbin/nologin\nnoshell:x:1003:1003:x:/home/x:/bin/false\n"
                "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\nBad Name:x:1004:1004::/h:/bin/sh\n"
                "short:x:1005\ncarol:x:notanumber:1:c:/h:/bin/sh\ndave:x:1006:1006:Dave\x1b[1m:/home/dave:/bin/sh\n")
        users = ac.parse_passwd(text)
        self.assertEqual([(u.uid, u.name, u.real_name) for u in users],
                         [(1000, "alice", "Alice Example"), (1001, "bob", ""), (1006, "dave", "Dave[1m")])
        self.assertTrue(all(u.path == "" and not u.administrator for u in users))
        self.assertEqual(ac.parse_passwd(None), [])

    def test_failure_words(self):
        failed = lambda text: commands.Completed(1, "", text)                               # noqa: E731
        self.assertEqual(ac.failure_text("Adding the user", failed("Call failed: Not authorized\n")),
                         "Adding the user was not allowed: administrator authorization was not given.")
        self.assertIn("not allowed", ac.failure_text("X", failed(
            "Call failed: GDBus.Error:org.freedesktop.Accounts.Error.PermissionDenied: Not authorized")))
        self.assertIn("exists already", ac.failure_text("Adding the user", failed(
            "Call failed: GDBus.Error:org.freedesktop.Accounts.Error.UserExists: A user with name 'bob' already "
            "exists")))
        self.assertEqual(ac.failure_text("Removing the user", failed("Call failed: user is logged in\n")),
                         "Removing the user failed: user is logged in")
        self.assertEqual(ac.failure_text("X", failed("")), "X failed.")

    def test_rules_for_removing_and_demoting(self):
        alice, bob, carol = (ac.User(1000, "alice", "", 1), ac.User(1001, "bob", "", 0), ac.User(1002, "carol", "", 1))
        state = ac.State((alice, bob), 1000, True)
        self.assertEqual((state.me, state.others), (alice, (bob,)))
        self.assertIn("signed in with", state.removal_problem(alice))
        self.assertEqual(state.removal_problem(bob), "")
        self.assertIn("only administrator", state.demotion_problem(alice))
        as_bob = ac.State((alice, bob), 1001, True)
        self.assertIn("only administrator", as_bob.removal_problem(alice))                  # never the last one
        two = ac.State((alice, bob, carol), 1001, True)
        self.assertEqual(two.removal_problem(alice), "")
        self.assertEqual(two.demotion_problem(carol), "")
        locked = ac.State((alice, ac.User(1002, "carol", "", 1, "", True)), 1001, True)
        self.assertIn("only administrator", locked.removal_problem(alice))                  # a locked one does not count


class ArgvTest(unittest.TestCase):
    def test_account_calls(self):
        call = PREFIX + ["--timeout=25", "--", "call", "org.freedesktop.Accounts"]
        self.assertEqual(commands.accounts_argv("ListCachedUsers"),
                         call + ["/org/freedesktop/Accounts", "org.freedesktop.Accounts", "ListCachedUsers"])
        path = "/org/freedesktop/Accounts/User1001"
        self.assertEqual(commands.accounts_argv("GetAll", path=path),
                         call + [path, "org.freedesktop.DBus.Properties", "GetAll", "s",
                                 "org.freedesktop.Accounts.User"])
        change = PREFIX + ["--timeout=180", "--", "call", "org.freedesktop.Accounts"]
        self.assertEqual(commands.accounts_argv("CreateUser", "bob", "Bob Example", 0),
                         change + ["/org/freedesktop/Accounts", "org.freedesktop.Accounts", "CreateUser", "ssi",
                                   "bob", "Bob Example", "0"])
        self.assertEqual(commands.accounts_argv("DeleteUser", 1001, True)[-4:], ["DeleteUser", "xb", "1001", "true"])
        self.assertEqual(commands.accounts_argv("SetRealName", "Bob B.", path=path)[-5:],
                         [path, "org.freedesktop.Accounts.User", "SetRealName", "s", "Bob B."])
        self.assertEqual(commands.accounts_argv("SetAccountType", 1, path=path)[-3:], ["SetAccountType", "i", "1"])
        self.assertEqual(commands.accounts_argv("SetIconFile", "/home/bob/.face", path=path)[-3:],
                         ["SetIconFile", "s", "/home/bob/.face"])
        self.assertEqual(commands.accounts_argv("SetLocked", True, path=path)[-3:], ["SetLocked", "b", "true"])
        for command in (commands.accounts_argv("ListCachedUsers"), commands.accounts_argv("DeleteUser", 1001, False),
                        commands.accounts_argv("SetRealName", "--help is not an option here"[2:], path=path)):
            commands._check(command)

    def test_nothing_else_reaches_busctl(self):
        path = "/org/freedesktop/Accounts/User1001"
        bad = [("SetPassword", ("$6$x$y", ""), path), ("SetPasswordHint", ("x",), path), ("SetShell", ("/bin/sh",), path),
               ("CreateUser", ("Bob", "Bob", 0), None), ("CreateUser", ("bob; id", "Bob", 0), None),
               ("CreateUser", ("bob", "--help", 0), None), ("CreateUser", ("bob", "a:b", 0), None),
               ("CreateUser", ("bob", "Doe, J", 0), None), ("CreateUser", ("bob", "a\nb", 0), None),
               ("CreateUser", ("bob", "Bob", 2), None), ("CreateUser", ("bob", "Bob", True), None),
               ("CreateUser", ("bob", "Bob"), None), ("DeleteUser", (0, True), None), ("DeleteUser", (999, False), None),
               ("DeleteUser", ("1001", True), None), ("DeleteUser", (1001, 1), None), ("DeleteUser", (True, True), None),
               ("ListCachedUsers", ("x",), None), ("UncacheUser", ("bob",), None), ("GetAll", (), "/org/x"),
               ("GetAll", (), "/org/freedesktop/Accounts/User1001/../.."), ("SetRealName", ("",), path),
               ("SetRealName", ("-x",), path), ("SetAccountType", (5,), path), ("SetAccountType", ("1",), path),
               ("SetIconFile", ("relative.png",), path), ("SetIconFile", ("/home/a/../../etc/shadow",), path),
               ("SetLocked", ("true",), path), ("SetRealName", ("Bob",), None)]
        for method, args, where in bad:
            with self.assertRaises(ValueError, msg=(method, args)):
                commands.accounts_argv(method, *args, path=where)
        with self.assertRaises(ValueError):
            commands.argv("busctl", "--system", "call", "org.freedesktop.login1", "/", "x", "PowerOff")
        with self.assertRaises(ValueError):
            commands.argv("passwd", "bob")
        good = commands.accounts_argv("SetRealName", "Bob", path=path)
        for broken in (good[:1] + ["--user"] + good[2:], good[:6] + ["org.freedesktop.login1"] + good[7:],
                       good[:9] + ["SetPassword"] + good[10:], [BUSCTL, "tree"], good[:4] + good[5:],
                       [BUSCTL, "--system", "--json=short", "--timeout=25", "--", "call", "org.freedesktop.Accounts",
                        "/org/freedesktop/login1", "org.freedesktop.Accounts", "ListCachedUsers"],
                       [commands.EXECUTABLES["passwd"], "bob"], [commands.EXECUTABLES["passwd"], "--stdin"]):
            with self.assertRaises(ValueError, msg=broken):
                commands._check(broken)
        self.assertEqual(commands.passwd_argv(), ["/usr/bin/passwd"])


class FakeBus:
    """Stands in for the runner: answers busctl calls as AccountsService would, and records them."""

    def __init__(self):
        self.users = {1000: properties(1000, "alice", "Alice Example", 1), 1001: properties(1001, "bob", "Bob", 0)}
        self.listed = [1000, 1001]
        self.ran: list[list[str]] = []
        self.fail: dict[str, commands.Completed] = {}
        self.down = False
        self.next_uid = 1002

    def available(self, _path) -> bool:
        return True

    def run(self, command, timeout=30.0) -> commands.Completed:
        commands._check(command)
        self.ran.append(list(command))
        if self.down:
            return commands.Completed(1, "", "Call failed: The name org.freedesktop.Accounts was not provided by "
                                      "any .service files\n")
        path, method, args = command[7], command[9], command[11:]
        if method in self.fail:
            return self.fail[method]
        if method == "ListCachedUsers":
            return commands.Completed(0, json.dumps({"type": "ao", "data": [[
                f"/org/freedesktop/Accounts/User{uid}" for uid in self.listed]]}) + "\n", "")
        if method == "CreateUser":
            uid, self.next_uid = self.next_uid, self.next_uid + 1
            self.users[uid] = properties(uid, args[0], args[1], int(args[2]))
            self.listed.append(uid)
            return commands.Completed(0, json.dumps({"type": "o", "data": [f"/org/freedesktop/Accounts/User{uid}"]}), "")
        if method == "DeleteUser":
            uid = int(args[0])
            self.users.pop(uid, None)
            self.listed = [u for u in self.listed if u != uid]
            return commands.Completed(0, "", "")
        uid = int(path.rsplit("User", 1)[1])
        if uid not in self.users:
            return commands.Completed(1, "", "Call failed: Unknown object\n")
        if method == "GetAll":
            return commands.Completed(0, self.users[uid], "")
        doc = json.loads(self.users[uid])
        key, kind = {"SetRealName": ("RealName", str), "SetAccountType": ("AccountType", int),
                     "SetIconFile": ("IconFile", str), "SetLocked": ("Locked", lambda v: v == "true")}[method]
        doc["data"][0][key]["data"] = kind(args[0])
        self.users[uid] = json.dumps(doc)
        return commands.Completed(0, "", "")

    def calls(self) -> list[list[str]]:
        return [c[9:10] + c[11:] for c in self.ran if c[9] not in ("ListCachedUsers", "GetAll")]


class DirectoryTest(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.directory = ac.Directory(self.bus, 1000)

    def test_load_and_change(self):
        state = self.directory.load()
        self.assertTrue(state.service)
        self.assertEqual([(u.uid, u.name, u.type_label) for u in state.users],
                         [(1000, "alice", "Administrator"), (1001, "bob", "Standard user")])
        self.assertEqual(state.me.name, "alice")
        bob = state.others[0]
        self.directory.set_real_name(bob, "Robert Example")
        self.directory.set_account_type(bob, ac.ADMINISTRATOR)
        self.directory.set_icon_file(bob, "/home/bob/.face")
        changed = self.directory.load().others[0]
        self.assertEqual((changed.real_name, changed.administrator, changed.icon_file),
                         ("Robert Example", True, "/home/bob/.face"))
        path = self.directory.create_user("carol", "Carol Example", ac.STANDARD)
        self.assertEqual(path, "/org/freedesktop/Accounts/User1002")
        self.assertEqual(len(self.directory.load().users), 3)
        self.directory.delete_user(1002, True)
        self.assertEqual(self.bus.calls(), [["SetRealName", "Robert Example"], ["SetAccountType", "1"],
                                            ["SetIconFile", "/home/bob/.face"],
                                            ["CreateUser", "carol", "Carol Example", "0"], ["DeleteUser", "1002", "true"]])
        self.assertTrue(all(command[3] == "--timeout=180" for command in self.bus.ran if command[9].startswith(
            ("Set", "Create", "Delete"))))                               # time to answer polkit's question

    def test_refusals(self):
        bob = self.directory.load().others[0]
        with self.assertRaises(Failed) as caught:
            self.directory.delete_user(1000, False)
        self.assertIn("signed in with", str(caught.exception.message))
        for name in ("", "a:b", "-x", "a,b"):
            with self.assertRaises(Failed):
                self.directory.set_real_name(bob, name)
        with self.assertRaises(Failed):
            self.directory.create_user("Carol", "Carol", 0)
        self.assertEqual(self.bus.calls(), [])
        self.bus.fail["SetAccountType"] = commands.Completed(1, "", "Call failed: Not authorized\n")
        with self.assertRaises(Failed) as caught:
            self.directory.set_account_type(bob, 1)
        self.assertIn("administrator authorization was not given", caught.exception.message)
        self.bus.fail["CreateUser"] = commands.Completed(0, "{}", "")
        with self.assertRaises(Failed):
            self.directory.create_user("carol", "Carol", 0)

    def test_own_account_is_found_even_when_not_listed(self):
        self.bus.listed = [1001]
        state = self.directory.load()
        self.assertEqual(state.me.name, "alice")
        self.assertEqual(len(state.users), 2)

    def test_without_the_service(self):
        with tempfile.TemporaryDirectory() as root:
            fx.write(Path(root) / "etc/passwd", "root:x:0:0::/root:/bin/bash\nalice:x:1000:1000:Alice:/home/alice:"
                     "/bin/bash\nbob:x:1001:1001::/home/bob:/bin/bash\n")
            directory = ac.Directory(self.bus, 1000, root)
            self.bus.down = True
            state = directory.load()
            self.assertFalse(state.service)
            self.assertIn("accounts service", state.problem)
            self.assertEqual([u.name for u in state.users], ["alice", "bob"])
            self.assertEqual(state.me.name, "alice")

            class Missing(FakeBus):
                def run(self, command, timeout=30.0):
                    raise CommandUnavailable("busctl")

            state = ac.Directory(Missing(), 1001, root).load()
            self.assertFalse(state.service)
            self.assertEqual(state.me.name, "bob")
            self.assertFalse(ac.Directory(Missing(), 1000, "/nonexistent-root").load().users)


class DialogueTest(unittest.TestCase):
    def play(self, chunks, current="oldpass12", new="newpass345"):
        dialogue = ac.PasswdDialogue(current, new)
        typed = []
        for chunk in chunks:
            typed += dialogue.feed(chunk)
            if dialogue.stop:
                break
        return dialogue, typed

    def test_success(self):
        dialogue, typed = self.play(["Changing password for alice.\n", "Current password: ", "New password: ",
                                     "Retype new password: ", "passwd: password updated successfully\n"])
        self.assertEqual(typed, ["oldpass12", "newpass345", "newpass345"])
        self.assertFalse(dialogue.stop)
        self.assertEqual(dialogue.outcome(0), "")

    def test_older_prompts_and_split_output(self):
        dialogue, typed = self.play(["Changing password for alice.\r\n(current) UNIX pass", "word: ",
                                     "Enter new UNIX password: ", "Retype new UNIX ", "password: ",
                                     "passwd: password updated successfully\r\n"])
        self.assertEqual(typed, ["oldpass12", "newpass345", "newpass345"])
        self.assertEqual(dialogue.outcome(0), "")
        dialogue, typed = self.play(["New password: ", "Retype new password: "])         # no current one is asked
        self.assertEqual(typed, ["newpass345", "newpass345"])
        self.assertEqual(dialogue.outcome(0), "")

    def test_wrong_current_password(self):
        dialogue, typed = self.play(["Current password: ", "passwd: Authentication token manipulation error\n"
                                     "passwd: password unchanged\n"])
        self.assertEqual(typed, ["oldpass12"])
        self.assertEqual(dialogue.outcome(10), "The current password is not correct.")
        dialogue, typed = self.play(["Current password: ", "Sorry, try again.\nCurrent password: "])
        self.assertEqual(typed, ["oldpass12"])                           # asked again: not answered a second time
        self.assertTrue(dialogue.stop)
        self.assertEqual(dialogue.outcome(None), "The current password is not correct.")

    def test_rejected_new_password(self):
        dialogue, typed = self.play(["Current password: ", "New password: ",
                                     "BAD PASSWORD: The password is shorter than 8 characters\nNew password: "])
        self.assertEqual(typed, ["oldpass12", "newpass345"])
        self.assertTrue(dialogue.stop)
        self.assertEqual(dialogue.outcome(None),
                         "The new password was not accepted: The password is shorter than 8 characters")
        cases = (("You must choose a longer password.\n", "The new password is too short."),
                 ("Password unchanged\n", "The new password must differ from the current one."),
                 ("Password has been already used. Choose another.\n", "This password was used before. Choose "
                  "another one."),
                 ("You must wait longer to change your password.\n", "The password was changed too recently and "
                  "cannot be changed again yet."),
                 ("Sorry, passwords do not match.\n", "The two new passwords were not the same."))
        for message, words in cases:
            dialogue, _typed = self.play(["Current password: ", "New password: ", "Retype new password: ",
                                          message + "passwd: Authentication token manipulation error\n"])
            self.assertEqual(dialogue.outcome(10), words, message)

    def test_unknown_message_is_passed_on_without_the_passwords(self):
        dialogue, _typed = self.play(["Current password: ", "New password: ", "Retype new password: ",
                                      "passwd: Module is unknown: newpass345 oldpass12 \x1b[1mfoo\n"])
        self.assertEqual(dialogue.outcome(1), "The password was not changed: Module is unknown: [1mfoo")
        self.assertNotIn("newpass345", dialogue.outcome(1))
        self.assertNotIn("oldpass12", dialogue.outcome(1))
        dialogue, _typed = self.play(["Current password: "])
        self.assertEqual(dialogue.outcome(1), "The password was not changed.")

    def test_unexpected_question(self):
        dialogue, typed = self.play(["Changing password for alice.\n", "Enter your smart card PIN: "])
        self.assertEqual(typed, [])                                      # nothing is typed into an unknown prompt
        self.assertTrue(dialogue.stop)
        self.assertIn("asked a question Control Center cannot answer", dialogue.outcome(None))
        self.assertIn("Enter your smart card PIN:", dialogue.outcome(None))
        dialogue, typed = self.play(["passwd:", " password updated successfully\n"])     # not a prompt
        self.assertFalse(dialogue.stop)


STAND_IN = r'''#!/usr/bin/python3 -IB
import os, sys, termios, time
mode, log = os.environ.get("STAND_IN_MODE", "ok"), os.environ["STAND_IN_LOG"]
def note(text):
    with open(log, "a") as handle:
        handle.write(text + "\n")
flags = termios.tcgetattr(0)[3]
note("args=%r tty=%s echo=%s lc=%s" % (sys.argv[1:], os.isatty(0), bool(flags & termios.ECHO), os.environ.get("LC_ALL")))
def ask(prompt):
    sys.stdout.write(prompt); sys.stdout.flush()
    answer = sys.stdin.readline().rstrip("\n")
    note("got " + answer)
    return answer
print("Changing password for tester.")
if mode == "hang":
    time.sleep(60)
if mode == "odd":
    ask("Enter your smart card PIN: ")
current = ask("(current) UNIX password: " if mode == "old" else "Current password: ")
if mode == "wrong" or current != "oldpass12":
    print("passwd: Authentication token manipulation error"); print("passwd: password unchanged"); sys.exit(10)
new = ask("Enter new UNIX password: " if mode == "old" else "New password: ")
if mode == "bad":
    print("BAD PASSWORD: The password fails the dictionary check - it is too simplistic/systematic")
    new = ask("New password: ")
again = ask("Retype new UNIX password: " if mode == "old" else "Retype new password: ")
if new != again:
    print("Sorry, passwords do not match."); sys.exit(10)
print("passwd: password updated successfully")
note("changed to " + new)
'''


class PseudoTerminalTest(unittest.TestCase):
    """commands.CommandRunner.converse against stand-ins for passwd."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.program = Path(self.tmp.name) / "passwd"
        self.program.write_text(STAND_IN, encoding="utf-8")
        self.program.chmod(self.program.stat().st_mode | stat.S_IXUSR)
        self.log = Path(self.tmp.name) / "log"
        patches = (mock.patch.dict(commands.EXECUTABLES, {"passwd": str(self.program)}),
                   mock.patch.object(commands, "ALLOWED_PATHS", frozenset({str(self.program)})))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def runner(self, mode: str) -> commands.CommandRunner:
        return commands.CommandRunner({"STAND_IN_MODE": mode, "STAND_IN_LOG": str(self.log), "PATH": "/usr/bin:/bin",
                                       "LC_ALL": "de_DE.UTF-8"})

    def logged(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_success(self):
        ac.change_own_password(self.runner("ok"), "oldpass12", "newpass345")
        log = self.logged()
        self.assertEqual(log[0], "args=[] tty=True echo=False lc=C")      # a terminal, no echo, fixed messages
        self.assertEqual(log[1:], ["got oldpass12", "got newpass345", "got newpass345", "changed to newpass345"])

    def test_older_prompts(self):
        ac.change_own_password(self.runner("old"), "oldpass12", "newpass345")
        self.assertEqual(self.logged()[-1], "changed to newpass345")

    def test_wrong_current_password(self):
        with self.assertRaises(Failed) as caught:
            ac.change_own_password(self.runner("ok"), "not-my-password", "newpass345")
        self.assertEqual(caught.exception.message, "The current password is not correct.")
        self.assertNotIn("changed to", "\n".join(self.logged()))

    def test_rejected_new_password(self):
        with self.assertRaises(Failed) as caught:
            ac.change_own_password(self.runner("bad"), "oldpass12", "newpass345")
        self.assertEqual(caught.exception.message, "The new password was not accepted: The password fails the "
                         "dictionary check - it is too simplistic/systematic")
        self.assertEqual(self.logged().count("got newpass345"), 1)       # not typed again into the second prompt
        self.assertNotIn("changed to", "\n".join(self.logged()))

    def test_unexpected_prompt(self):
        with self.assertRaises(Failed) as caught:
            ac.change_own_password(self.runner("odd"), "oldpass12", "newpass345")
        self.assertIn("asked a question Control Center cannot answer", caught.exception.message)
        self.assertEqual([line for line in self.logged() if line.startswith("got")], [])   # nothing was typed

    def test_hang_is_ended(self):
        start = time.monotonic()
        with self.assertRaises(Failed) as caught:
            ac.change_own_password(self.runner("hang"), "oldpass12", "newpass345", timeout=1.0)
        self.assertLess(time.monotonic() - start, 8)
        self.assertIn("did not finish in time", caught.exception.message)

    def test_nothing_typed_is_echoed_or_kept(self):
        seen = []

        class Spy(ac.PasswdDialogue):
            def feed(self, text):
                seen.append(text)
                return super().feed(text)

        dialogue = Spy("oldpass12", "newpass345")
        code = self.runner("ok").converse(commands.passwd_argv(), dialogue, 20)
        self.assertEqual(code, 0)
        output = "".join(seen)
        self.assertIn("password updated successfully", output)
        self.assertNotIn("oldpass12", output)                            # the terminal does not echo what is typed
        self.assertNotIn("newpass345", output)

    def test_only_passwd_and_only_without_arguments(self):
        runner = self.runner("ok")
        for command in ([str(self.program), "root"], [str(self.program), "--stdin"], ["/bin/sh"], []):
            with self.assertRaises(ValueError, msg=command):
                runner.converse(command, ac.PasswdDialogue("a", "b"), 5)
        with self.assertRaises(ValueError):
            runner.converse(commands.passwd_argv(), ac.PasswdDialogue("a", "b"), 0)
        os.unlink(self.program)
        with self.assertRaises(CommandUnavailable):
            runner.converse(commands.passwd_argv(), ac.PasswdDialogue("a", "b"), 5)
        with self.assertRaises(CommandFailed):
            pass_through = mock.Mock(side_effect=CommandFailed("passwd", "passwd could not be started (x)."))
            with mock.patch.object(commands.CommandRunner, "converse", pass_through):
                runner.converse(commands.passwd_argv(), ac.PasswdDialogue("a", "b"), 5)


if __name__ == "__main__":
    unittest.main()


class PasswordRulesTest(unittest.TestCase):
    def rules(self, files: dict[str, str]):
        import tempfile
        directory = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        os.makedirs(os.path.join(directory, "pwquality.conf.d"))
        for name, text in files.items():
            with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        return ac.password_rules(directory)

    def test_the_devices_rules_apply_to_passwords_set_for_others(self):
        rules = self.rules({"pwquality.conf": "# defaults\nminlen = 10\n",
                            "pwquality.conf.d/50-boswas.conf": "minlen = 12\nminclass = 2   # two kinds\n"})
        self.assertEqual((rules.min_length, rules.min_classes), (12, 2))
        self.assertIn("at least 12 characters", ac.password_problem("short-1", "u", rules))
        self.assertIn("2 kinds of characters", ac.password_problem("onlylowercaseletters", "u", rules))
        self.assertEqual(ac.password_problem("lower-and-12345", "u", rules), "")
        self.assertEqual(ac.password_problem("Correct Horse Battery", "u", rules), "")

    def test_odd_files_never_weaken_the_built_in_minimum(self):
        self.assertEqual(self.rules({}), ac.PasswordRules())
        weak = self.rules({"pwquality.conf": "minlen = 1\nminclass = 0\n"})
        self.assertEqual((weak.min_length, weak.min_classes), (ac.MIN_PASSWORD, 1))
        odd = self.rules({"pwquality.conf": "minlen = -5\nminlen = many\nminlen = 99999999\nminclass = 9\n\x00\xff",
                          "pwquality.conf.d/notes.txt": "minlen = 40\n"})
        self.assertEqual((odd.min_length, odd.min_classes), (ac.MIN_PASSWORD, 4))
        self.assertEqual(ac.password_rules("/nonexistent/place"), ac.PasswordRules())
        self.assertEqual(ac.password_problem("eightchr"), "", "without rules the built-in minimum applies")
