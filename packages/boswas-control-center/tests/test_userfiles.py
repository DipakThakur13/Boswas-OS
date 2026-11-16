"""The settings files Control Center writes: userfiles.write_config, remove_config and write_face.

Everything runs in a temporary home: the allow-list of names, the symlink
rules, atomic replacement, modes, and the errors in words for the user.

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)

from boswas_control_center import userfiles  # noqa: E402
from boswas_control_center.userfiles import UserFileError, remove_config, write_config, write_face  # noqa: E402


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class ConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home)}
        self.config = self.home / ".config"

    def tearDown(self):
        self.tmp.cleanup()

    def leftovers(self, directory: Path) -> list[str]:
        return [name for name in os.listdir(directory) if name.endswith(".tmp")]

    # --- names ---
    def test_allow_list(self):
        for name in userfiles.CONFIG_FILES:
            path = write_config(self.env, name, b"x")
            self.assertEqual(path, str(self.config / name))
            self.assertEqual(Path(path).read_bytes(), b"x")
        self.assertEqual(set(userfiles.CONFIG_FILES), {"boswas/compositor.toml", "boswas/compositor.toml.bak",
                                                       "boswas/appearance.toml", "mimeapps.list"})
        for name in ("autostart/org.example.App.desktop", "autostart/a.desktop", "autostart/my_app-2.desktop",
                     "autostart/" + "a" * 128 + ".desktop"):
            self.assertTrue(os.path.isfile(write_config(self.env, name, b"[Desktop Entry]\n")), name)

    def test_other_names_are_refused(self):
        bad = ["", ".", "..", "/etc/passwd", "/tmp/x", "../x", "boswas/../mimeapps.list", "boswas/../../.bashrc",
               "boswas", "boswas/", "boswas/compositor.toml/", "boswas/other.toml", "boswas//compositor.toml",
               "./mimeapps.list", "mimeapps.list\0", "mimeapps.list\0.png", "mimeapps.list ", "MIMEAPPS.LIST",
               "boswas/compositor.toml.bak.bak", "boswas/policy.toml", "kwinrc", "user-dirs.dirs",
               "autostart/.desktop", "autostart/.hidden.desktop", "autostart/../mimeapps.list",
               "autostart/a/b.desktop", "autostart/a.desktop/x", "autostart/a b.desktop", "autostart/a.txt",
               "autostart/a.desktop\n", "autostart/a\0.desktop", "autostart/..desktop",
               "autostart/" + "a" * 129 + ".desktop", "autostart/é.desktop", "autostart2/a.desktop",
               "autostart", b"mimeapps.list", None, 7, ("mimeapps.list",)]
        for name in bad:
            with self.assertRaises(UserFileError, msg=repr(name)):
                write_config(self.env, name, b"x")
            with self.assertRaises(UserFileError, msg=repr(name)):
                remove_config(self.env, name)
        self.assertFalse(self.config.exists())                      # nothing was created for a refused name

    # --- writing ---
    def test_creates_directories_0700_and_file_0600(self):
        old = os.umask(0o022)
        try:
            path = write_config(self.env, "boswas/compositor.toml", b"[input]\n")
        finally:
            os.umask(old)
        self.assertEqual(mode(self.config), 0o700)
        self.assertEqual(mode(self.config / "boswas"), 0o700)
        self.assertEqual(mode(path), 0o600)
        self.assertEqual(self.leftovers(self.config / "boswas"), [])

    def test_mode_is_0600_even_with_an_open_umask_and_an_existing_wider_file(self):
        target = self.config / "mimeapps.list"
        self.config.mkdir()
        target.write_bytes(b"old")
        os.chmod(target, 0o644)
        old = os.umask(0)
        try:
            write_config(self.env, "mimeapps.list", b"new")
        finally:
            os.umask(old)
        self.assertEqual(mode(target), 0o600)
        self.assertEqual(target.read_bytes(), b"new")

    def test_replaces_atomically(self):
        """The old file is replaced by a rename: a reader that had it open keeps the old, complete content."""
        path = Path(write_config(self.env, "boswas/appearance.toml", b"scheme = \"dark\"\n"))
        before = os.stat(path).st_ino
        with open(path, "rb") as reader:
            write_config(self.env, "boswas/appearance.toml", b"scheme = \"light\"\n")
            self.assertEqual(reader.read(), b"scheme = \"dark\"\n")
        self.assertEqual(path.read_bytes(), b"scheme = \"light\"\n")
        self.assertNotEqual(os.stat(path).st_ino, before)
        self.assertEqual(self.leftovers(path.parent), [])

    def test_empty_content_and_binary_content(self):
        path = Path(write_config(self.env, "mimeapps.list", b""))
        self.assertEqual(path.read_bytes(), b"")
        data = bytes(range(256)) * 4
        write_config(self.env, "mimeapps.list", data)
        self.assertEqual(path.read_bytes(), data)

    def test_content_must_be_bytes_and_bounded(self):
        for data in ("text", bytearray(b"x"), None, 5, [b"x"]):
            with self.assertRaises(UserFileError, msg=repr(data)):
                write_config(self.env, "mimeapps.list", data)
        write_config(self.env, "mimeapps.list", b"x" * userfiles.MAX_CONFIG)
        with self.assertRaises(UserFileError) as caught:
            write_config(self.env, "mimeapps.list", b"x" * (userfiles.MAX_CONFIG + 1))
        self.assertIn("256 KiB", str(caught.exception))
        self.assertEqual(os.path.getsize(self.config / "mimeapps.list"), userfiles.MAX_CONFIG)   # the old file stays

    # --- symbolic links ---
    def test_symlinked_directory_is_refused(self):
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        self.config.mkdir()
        os.symlink(elsewhere, self.config / "boswas")
        os.symlink(elsewhere, self.config / "autostart")
        for name in ("boswas/compositor.toml", "autostart/a.desktop"):
            with self.assertRaises(UserFileError) as caught:
                write_config(self.env, name, b"x")
            self.assertIn("symbolic link", str(caught.exception))
            with self.assertRaises(UserFileError):
                remove_config(self.env, name)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_symlinked_target_is_refused(self):
        victim = Path(self.tmp.name) / "victim"
        victim.write_bytes(b"keep")
        (self.config / "boswas").mkdir(parents=True)
        for target in (victim, Path(self.tmp.name) / "dangling"):
            link = self.config / "boswas" / "compositor.toml"
            os.symlink(target, link)
            with self.assertRaises(UserFileError) as caught:
                write_config(self.env, "boswas/compositor.toml", b"x")
            self.assertIn("symbolic link", str(caught.exception))
            with self.assertRaises(UserFileError):
                remove_config(self.env, "boswas/compositor.toml")
            self.assertTrue(os.path.islink(link))
            os.unlink(link)
        self.assertEqual(victim.read_bytes(), b"keep")
        self.assertFalse((Path(self.tmp.name) / "dangling").exists())
        self.assertEqual(self.leftovers(self.config / "boswas"), [])

    def test_the_config_directory_itself_may_be_a_symlink(self):
        real = Path(self.tmp.name) / "real-config"
        real.mkdir()
        os.symlink(real, self.config)
        write_config(self.env, "boswas/compositor.toml", b"x")
        self.assertEqual((real / "boswas" / "compositor.toml").read_bytes(), b"x")

    def test_target_that_is_not_a_regular_file_is_refused(self):
        (self.config / "mimeapps.list").mkdir(parents=True)
        with self.assertRaises(UserFileError) as caught:
            write_config(self.env, "mimeapps.list", b"x")
        self.assertIn("not a regular file", str(caught.exception))
        with self.assertRaises(UserFileError):
            remove_config(self.env, "mimeapps.list")
        os.mkfifo(self.config / "boswas")
        with self.assertRaises(UserFileError) as caught:
            write_config(self.env, "boswas/compositor.toml", b"x")
        self.assertIn("not a folder", str(caught.exception))

    # --- failures ---
    @unittest.skipIf(os.geteuid() == 0, "root writes into read-only directories")
    def test_read_only_directory(self):
        (self.config / "boswas").mkdir(parents=True)
        (self.config / "boswas" / "compositor.toml").write_bytes(b"old")
        os.chmod(self.config / "boswas", 0o500)
        try:
            with self.assertRaises(UserFileError) as caught:
                write_config(self.env, "boswas/compositor.toml", b"new")
            self.assertIn("could not be written", str(caught.exception))
            with self.assertRaises(UserFileError):
                remove_config(self.env, "boswas/compositor.toml")
            self.assertEqual((self.config / "boswas" / "compositor.toml").read_bytes(), b"old")
        finally:
            os.chmod(self.config / "boswas", 0o700)

    def test_failed_write_leaves_no_temporary_file_and_keeps_the_old_content(self):
        path = Path(write_config(self.env, "mimeapps.list", b"old"))
        real_replace = os.replace

        def failing(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        os.replace = failing
        try:
            with self.assertRaises(UserFileError) as caught:
                write_config(self.env, "mimeapps.list", b"new")
        finally:
            os.replace = real_replace
        self.assertIn("No space left on device", str(caught.exception))
        self.assertEqual(path.read_bytes(), b"old")
        self.assertEqual(self.leftovers(self.config), [])

    def test_errors_are_user_file_errors_never_os_errors(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_bytes(b"")
        for env in ({"HOME": str(blocker)}, {"HOME": "relative"}, {}, {"HOME": str(self.home / "missing")},
                    {"XDG_CONFIG_HOME": str(blocker / "sub")}, {"XDG_CONFIG_HOME": str(blocker)}):
            with self.assertRaises(UserFileError, msg=env):
                write_config(env, "mimeapps.list", b"x")

    # --- the config directory ---
    def test_xdg_config_home_only_when_absolute(self):
        other = Path(self.tmp.name) / "xdg"
        env = dict(self.env, XDG_CONFIG_HOME=str(other))
        self.assertEqual(write_config(env, "mimeapps.list", b"x"), str(other / "mimeapps.list"))
        self.assertEqual(mode(other), 0o700)
        self.assertFalse(self.config.exists())
        for relative in ("relative/config", "", "./x", "~/.config"):
            env = dict(self.env, XDG_CONFIG_HOME=relative)
            self.assertEqual(write_config(env, "mimeapps.list", b"y"), str(self.config / "mimeapps.list"))
            self.assertEqual(userfiles.config_dir(env), str(self.config))
        self.assertFalse(os.path.exists("relative"))

    # --- removing ---
    def test_remove_is_idempotent(self):
        remove_config(self.env, "boswas/compositor.toml.bak")           # no config directory at all
        self.assertFalse(self.config.exists())                         # and removing creates nothing
        path = write_config(self.env, "boswas/compositor.toml.bak", b"x")
        other = write_config(self.env, "boswas/compositor.toml", b"keep")
        remove_config(self.env, "boswas/compositor.toml.bak")
        self.assertFalse(os.path.exists(path))
        remove_config(self.env, "boswas/compositor.toml.bak")
        remove_config(self.env, "autostart/never-there.desktop")
        self.assertEqual(Path(other).read_bytes(), b"keep")
        self.assertFalse((self.config / "autostart").exists())


class FaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_and_replace(self):
        path = write_face(self.env, b"\x89PNG one")
        self.assertEqual(path, str(self.home / ".face"))
        self.assertEqual(mode(path), 0o644)
        write_face(self.env, b"\x89PNG two")
        self.assertEqual(Path(path).read_bytes(), b"\x89PNG two")
        self.assertEqual(sorted(os.listdir(self.home)), [".face"])

    def test_refusals(self):
        for data in (b"", "text", None, b"x" * (userfiles.MAX_FACE + 1)):
            with self.assertRaises(UserFileError, msg=type(data)):
                write_face(self.env, data)
        victim = Path(self.tmp.name) / "victim"
        victim.write_bytes(b"keep")
        os.symlink(victim, self.home / ".face")
        with self.assertRaises(UserFileError):
            write_face(self.env, b"\x89PNG")
        self.assertEqual(victim.read_bytes(), b"keep")
        with self.assertRaises(UserFileError):
            write_face({"HOME": "relative"}, b"\x89PNG")


if __name__ == "__main__":
    unittest.main()
