"""Desktop entries: parsing, the installed applications, and XDG autostart with its overrides."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tm_fixtures import desktop_file  # noqa: E402

from boswas_apps.task_manager import desktopfile as df  # noqa: E402


class ParserTest(unittest.TestCase):
    def test_groups_keys_and_comments(self):
        entry = df.DesktopEntry("# comment\n\n[Desktop Entry]\nName = Files \nExec=files %U\nName=Second\n"
                                "junk line\n=nokey\n[Desktop Action new]\nName=New Window\nExec=files --new\n")
        self.assertEqual(entry.get("Name"), "Files")
        self.assertEqual(entry.get("Exec"), "files %U")
        self.assertEqual(entry.get("Name", group="Desktop Action new"), "New Window")
        self.assertIsNone(entry.get("Missing"))
        self.assertIsNone(entry.get(""))

    def test_keys_before_any_group_are_ignored(self):
        entry = df.DesktopEntry("Name=Stray\n[Other]\nName=Other\n")
        self.assertNotIn(df.MAIN, entry.groups)

    def test_localized_names(self):
        entry = df.DesktopEntry("[Desktop Entry]\nName=Files\nName[de]=Dateien\nName[de_AT]=Dateien AT\n"
                                "Name[sr@latin]=Fajlovi\n")
        self.assertEqual(entry.get("Name", df.locale_candidates({"LANG": "de_AT.UTF-8"})), "Dateien AT")
        self.assertEqual(entry.get("Name", df.locale_candidates({"LANG": "de_DE.UTF-8"})), "Dateien")
        self.assertEqual(entry.get("Name", df.locale_candidates({"LANG": "sr_RS@latin"})), "Fajlovi")
        self.assertEqual(entry.get("Name", df.locale_candidates({"LANG": "fr_FR.UTF-8"})), "Files")
        self.assertEqual(df.locale_candidates({"LANG": "C"}), [])
        self.assertEqual(df.locale_candidates({}), [])
        self.assertEqual(df.locale_candidates({"LC_ALL": "pt_BR.UTF-8@x", "LANG": "de"}), ["pt_BR@x", "pt_BR", "pt@x", "pt"])

    def test_escapes_booleans_and_lists(self):
        entry = df.DesktopEntry("[Desktop Entry]\nComment=a\\sb\\nc\\\\d\\q\nHidden=True\nNoDisplay=yes\n"
                                "OnlyShowIn=GNOME;Boswas;\nKeywords=a\\;b;c\nEmpty=\n")
        self.assertEqual(entry.get("Comment"), "a b\nc\\d\\q")
        self.assertTrue(entry.boolean("Hidden"))
        self.assertFalse(entry.boolean("NoDisplay"))
        self.assertTrue(entry.boolean("Missing", True))
        self.assertEqual(entry.list("OnlyShowIn"), ["GNOME", "Boswas"])
        self.assertEqual(entry.list("Keywords"), ["a;b", "c"])
        self.assertEqual(entry.list("Empty"), [])

    def test_exec_quoting_and_field_codes(self):
        argv = df.exec_argv
        self.assertEqual(argv("firefox %u"), ["firefox"])
        self.assertEqual(argv('"/opt/My App/run" --name "two words" %F'), ["/opt/My App/run", "--name", "two words"])
        self.assertEqual(argv('sh -c "echo \\"hi\\" \\$HOME \\\\ \\`x\\`"'), ["sh", "-c", 'echo "hi" $HOME \\ `x`'])
        self.assertEqual(argv("app --file=%f --rate=100%% %i %c %k"), ["app", "--file=", "--rate=100%"])
        self.assertEqual(argv('app ""'), ["app", ""])
        self.assertEqual(argv("  spaced   out  "), ["spaced", "out"])
        self.assertEqual(argv(""), [])
        self.assertEqual(argv(None), [])
        self.assertEqual(argv("app %"), ["app", "%"])
        self.assertEqual(argv('unterminated "quote here'), ["unterminated", "quote here"])

    def test_program_of(self):
        self.assertEqual(df.program_of(["/usr/bin/kate", "-b"]), "kate")
        self.assertEqual(df.program_of(["env", "A=1", "-u", "B=2", "/opt/x/app"]), "app")
        self.assertEqual(df.program_of(["env"]), "")
        self.assertEqual(df.program_of([]), "")

    def test_load_refuses_large_binary_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            big = Path(directory, "big.desktop")
            big.write_text("[Desktop Entry]\nName=x\n" + "#" * (df.MAX_ENTRY_BYTES + 1))
            binary = Path(directory, "bin.desktop")
            binary.write_bytes(b"[Desktop Entry]\nName=\0x\n")
            plain = Path(directory, "nogroup.desktop")
            plain.write_text("Name=x\n")
            latin = Path(directory, "latin.desktop")
            latin.write_bytes(b"[Desktop Entry]\nName=caf\xe9\nExec=x\n")
            self.assertIsNone(df.DesktopEntry.load(str(big)))
            self.assertIsNone(df.DesktopEntry.load(str(binary)))
            self.assertIsNone(df.DesktopEntry.load(str(plain)))
            self.assertIsNone(df.DesktopEntry.load(str(Path(directory, "missing.desktop"))))
            self.assertEqual(df.DesktopEntry.load(str(latin)).get("Name"), "caf�")


class TreeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tm-desktop-")
        self.root = Path(self._tmp.name)
        self.env = {
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "home/.config"),
            "XDG_CONFIG_DIRS": f"{self.root}/etc/xdg:{self.root}/etc/xdg2",
            "XDG_DATA_HOME": str(self.root / "home/.local/share"),
            "XDG_DATA_DIRS": f"{self.root}/usr/local/share:{self.root}/usr/share",
        }
        self.user = self.root / "home/.config/autostart"
        self.system = self.root / "etc/xdg/autostart"

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def startup(self) -> dict[str, df.StartupEntry]:
        return {entry.file_id: entry for entry in df.list_startup(self.env, which=lambda name: f"/usr/bin/{name}")}


class ApplicationsTest(TreeCase):
    def test_precedence_hidden_and_visibility(self):
        self.put("usr/share/applications/org.kde.kate.desktop", desktop_file("Kate", "kate -b %U", Icon="kate"))
        self.put("usr/share/applications/hidden.desktop", desktop_file("Gone", "gone", Hidden="true"))
        self.put("usr/share/applications/helper.desktop", desktop_file("Helper", "helper", NoDisplay="true"))
        self.put("usr/share/applications/gnome.desktop", desktop_file("OnlyGnome", "og", OnlyShowIn="GNOME;"))
        self.put("usr/share/applications/htop.desktop", desktop_file("Htop", "htop", Terminal="true"))
        self.put("usr/share/applications/link.desktop", "[Desktop Entry]\nType=Link\nName=Link\nURL=http://x\n")
        self.put("usr/share/applications/noexec.desktop", "[Desktop Entry]\nType=Application\nName=NoExec\n")
        self.put("usr/share/applications/kde/sub.desktop", desktop_file("Sub", "sub"))
        self.put("usr/local/share/applications/org.kde.kate.desktop", desktop_file("Kate Local", "/opt/kate/bin/kate"))
        self.put("home/.local/share/applications/mine.desktop", desktop_file("Mine", "env X=1 /home/a/bin/mine --x"))
        apps = {app.app_id: app for app in df.list_applications(self.env)}
        self.assertEqual(sorted(apps), ["gnome.desktop", "helper.desktop", "htop.desktop", "kde-sub.desktop",
                                        "mine.desktop", "org.kde.kate.desktop"])
        self.assertEqual((apps["org.kde.kate.desktop"].name, apps["org.kde.kate.desktop"].program), ("Kate Local", "kate"))
        self.assertEqual(apps["mine.desktop"].program, "mine")
        self.assertFalse(apps["helper.desktop"].visible)
        self.assertFalse(apps["gnome.desktop"].visible)
        self.assertTrue(apps["htop.desktop"].terminal)

    def test_app_index(self):
        apps = [
            df.Application("writer.desktop", "Writer", "w", "soffice", "libreoffice-writer", "/w", arguments=1),
            df.Application("office.desktop", "Office", "o", "soffice", "", "/o", arguments=0),
            df.Application("files.desktop", "Files", "f", "boswas-files", "com.boswas.Files", "/f"),
            df.Application("htop.desktop", "Htop", "h", "htop", "", "/h", terminal=True),
            df.Application("helper.desktop", "Helper", "h", "helper", "", "/h", visible=False),
        ]
        index = df.AppIndex(apps)
        self.assertEqual(index.match(["soffice"]).name, "Office")            # the plainest Exec wins
        self.assertEqual(index.match(["libreoffice-writer"]).name, "Writer")
        self.assertEqual(index.match(["python3", "boswas-files"]).name, "Files")
        self.assertEqual(index.match(["com.boswas.files"]).name, "Files")     # StartupWMClass, any case
        self.assertIsNone(index.match(["htop"]))
        self.assertIsNone(index.match(["helper"]))
        self.assertIsNone(index.match([]))


class AutostartListTest(TreeCase):
    def test_system_and_user_entries(self):
        self.put("etc/xdg/autostart/org.kde.kdeconnect.desktop", desktop_file("KDE Connect", "kdeconnectd", Icon="kdeconnect"))
        self.put("home/.config/autostart/mine.desktop", desktop_file("My Script", "/home/a/run.sh --quiet"))
        entries = self.startup()
        connect, mine = entries["org.kde.kdeconnect.desktop"], entries["mine.desktop"]
        self.assertEqual((connect.name, connect.publisher, connect.source, connect.enabled), ("KDE Connect", "KDE", "System", True))
        self.assertEqual((connect.user_path, connect.system_path), (None, str(self.system / "org.kde.kdeconnect.desktop")))
        self.assertEqual((mine.name, mine.publisher, mine.source, mine.command), ("My Script", "", "User", "/home/a/run.sh --quiet"))
        self.assertEqual(mine.path, str(self.user / "mine.desktop"))

    def test_user_file_overrides_and_hidden_disables(self):
        self.put("etc/xdg/autostart/a.desktop", desktop_file("A", "a"))
        self.put("home/.config/autostart/a.desktop", desktop_file("A changed", "a --mine", Hidden="true"))
        self.put("etc/xdg/autostart/b.desktop", desktop_file("B", "b"))
        self.put("home/.config/autostart/b.desktop", "[Desktop Entry]\nHidden=true\n")       # a bare override
        self.put("etc/xdg2/autostart/a.desktop", desktop_file("A lower", "a --lower"))
        entries = self.startup()
        self.assertEqual((entries["a.desktop"].name, entries["a.desktop"].enabled, entries["a.desktop"].command),
                         ("A changed", False, "a --mine"))
        self.assertEqual(entries["a.desktop"].system_path, str(self.system / "a.desktop"))
        self.assertEqual((entries["b.desktop"].name, entries["b.desktop"].enabled, entries["b.desktop"].source),
                         ("B", False, "System"))

    def test_only_show_in_and_not_show_in_for_boswas(self):
        self.put("etc/xdg/autostart/gnome.desktop", desktop_file("G", "g", OnlyShowIn="GNOME;Unity;"))
        self.put("etc/xdg/autostart/boswas.desktop", desktop_file("B", "b", OnlyShowIn="GNOME;Boswas;"))
        self.put("etc/xdg/autostart/notus.desktop", desktop_file("N", "n", NotShowIn="Boswas;"))
        self.put("etc/xdg/autostart/notkde.desktop", desktop_file("K", "k", NotShowIn="KDE;"))
        self.put("etc/xdg/autostart/hidden-gnome.desktop", desktop_file("HG", "hg", OnlyShowIn="GNOME;"))
        self.put("home/.config/autostart/hidden-gnome.desktop", "[Desktop Entry]\nHidden=true\n")
        self.assertEqual(sorted(self.startup()), ["boswas.desktop", "notkde.desktop"])
        self.env["XDG_CURRENT_DESKTOP"] = "KDE:Boswas"
        self.assertEqual(sorted(self.startup()), ["boswas.desktop"])
        self.env["XDG_CURRENT_DESKTOP"] = "GNOME"
        found = self.startup()
        self.assertEqual(sorted(found), ["boswas.desktop", "gnome.desktop", "hidden-gnome.desktop", "notkde.desktop",
                                         "notus.desktop"])
        self.assertFalse(found["hidden-gnome.desktop"].enabled)

    def test_try_exec_gnome_flag_and_things_that_are_not_entries(self):
        self.put("etc/xdg/autostart/absent.desktop", desktop_file("Absent", "x", TryExec="not-installed"))
        self.put("etc/xdg/autostart/present.desktop", desktop_file("Present", "x", TryExec="installed"))
        self.put("etc/xdg/autostart/gnome-off.desktop", desktop_file("Off", "x", **{"X__GNOME__Autostart__enabled": "false"}))
        self.put("etc/xdg/autostart/link.desktop", "[Desktop Entry]\nType=Link\nName=L\nURL=x\n")
        self.put("etc/xdg/autostart/noexec.desktop", "[Desktop Entry]\nType=Application\nName=NoExec\n")
        self.put("etc/xdg/autostart/README", "not an entry")
        self.put("etc/xdg/autostart/.hidden.desktop", desktop_file("Dot", "x"))
        self.put("home/.config/autostart/orphan.desktop", "[Desktop Entry]\nHidden=true\n")
        (self.system / "dir.desktop").mkdir()
        found = {e.file_id: e for e in df.list_startup(self.env, which=lambda n: "/usr/bin/installed" if n == "installed" else None)}
        self.assertEqual(sorted(found), ["gnome-off.desktop", "present.desktop"])
        self.assertFalse(found["gnome-off.desktop"].enabled)

    def test_no_autostart_directories(self):
        self.assertEqual(df.list_startup(self.env), [])

    def test_hostile_names_are_cleaned(self):
        self.put("etc/xdg/autostart/evil.desktop", desktop_file("Good‮eman\tname", "evil ‮ x", Icon="../../etc/passwd"))
        entry = self.startup()["evil.desktop"]
        self.assertEqual(entry.name, "Good�eman name")
        self.assertNotIn("‮", entry.command)

    def test_publisher(self):
        get = {"X-Publisher": "ACME Ltd"}.get
        self.assertEqual(df.publisher_of(get, "x.desktop"), "ACME Ltd")
        none = {}.get
        self.assertEqual(df.publisher_of(none, "org.gnome.Software.desktop"), "GNOME")
        self.assertEqual(df.publisher_of(none, "com.boswas.Files.desktop"), "Boswas")
        self.assertEqual(df.publisher_of(none, "io.example.App.desktop"), "Example")
        self.assertEqual(df.publisher_of(none, "firefox-esr.desktop"), "")
        self.assertEqual(df.publisher_of(none, "a.b.desktop"), "")


class AutostartChangeTest(TreeCase):
    def test_disable_a_system_entry_writes_a_user_override(self):
        system = self.put("etc/xdg/autostart/agent.desktop", "# keep me\n" + desktop_file("Agent", "agent --start", Icon="agent")
                          + "\n[Desktop Action stop]\nName=Stop\nExec=agent --stop\n")
        before = system.read_text()
        path = df.set_startup_enabled("agent.desktop", False, self.env)
        self.assertEqual(path, str(self.user / "agent.desktop"))
        self.assertEqual(system.read_text(), before)                      # the system's file is never touched
        text = Path(path).read_text()
        self.assertIn("\nHidden=true\n", text)
        self.assertLess(text.index("Hidden=true"), text.index("[Desktop Action stop]"))
        self.assertIn("# keep me", text)
        self.assertIn("Exec=agent --start", text)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        entry = self.startup()["agent.desktop"]
        self.assertEqual((entry.enabled, entry.name, entry.source, entry.user_path), (False, "Agent", "System", path))

    def test_enable_again_sets_hidden_false(self):
        self.put("etc/xdg/autostart/agent.desktop", desktop_file("Agent", "agent"))
        df.set_startup_enabled("agent.desktop", False, self.env)
        df.set_startup_enabled("agent.desktop", True, self.env)
        text = (self.user / "agent.desktop").read_text()
        self.assertEqual(text.count("Hidden="), 1)
        self.assertIn("Hidden=false", text)
        self.assertTrue(self.startup()["agent.desktop"].enabled)

    def test_disable_and_enable_a_user_entry_in_place(self):
        user = self.put("home/.config/autostart/mine.desktop", desktop_file("Mine", "mine", Hidden="false", Comment="c"))
        df.set_startup_enabled("mine.desktop", False, self.env)
        self.assertEqual(user.read_text().count("Hidden="), 1)
        self.assertFalse(self.startup()["mine.desktop"].enabled)
        df.set_startup_enabled("mine.desktop", True, self.env)
        self.assertTrue(self.startup()["mine.desktop"].enabled)
        self.assertIn("Comment=c", user.read_text())

    def test_enable_over_a_bare_override_restores_the_command(self):
        self.put("etc/xdg/autostart/agent.desktop", desktop_file("Agent", "agent"))
        self.put("home/.config/autostart/agent.desktop", "[Desktop Entry]\nHidden=true\n")
        df.set_startup_enabled("agent.desktop", True, self.env)
        entry = self.startup()["agent.desktop"]
        self.assertEqual((entry.enabled, entry.command), (True, "agent"))
        self.assertIn("Exec=agent", (self.user / "agent.desktop").read_text())

    def test_enable_an_entry_gnome_switched_off(self):
        self.put("home/.config/autostart/x.desktop", desktop_file("X", "x", **{"X__GNOME__Autostart__enabled": "false"}))
        df.set_startup_enabled("x.desktop", True, self.env)
        text = (self.user / "x.desktop").read_text()
        self.assertIn("X-GNOME-Autostart-enabled=true", text)
        self.assertTrue(self.startup()["x.desktop"].enabled)

    def test_symbolic_link_in_the_user_directory_is_replaced_not_followed(self):
        target = self.put("opt/app/app.desktop", desktop_file("App", "app"))
        self.user.mkdir(parents=True)
        os.symlink(target, self.user / "app.desktop")
        df.set_startup_enabled("app.desktop", False, self.env)
        self.assertFalse((self.user / "app.desktop").is_symlink())
        self.assertNotIn("Hidden", target.read_text())
        self.assertFalse(self.startup()["app.desktop"].enabled)

    def test_names_that_would_leave_the_directory_are_refused(self):
        self.put("etc/xdg/autostart/a.desktop", desktop_file("A", "a"))
        for name in ("../a.desktop", "a/b.desktop", "a", ".desktop", ".a.desktop", "a.desktop\0", "x" * 300 + ".desktop", ""):
            with self.subTest(name=name):
                with self.assertRaises(df.StartupError):
                    df.set_startup_enabled(name, False, self.env)
                with self.assertRaises(df.StartupError):
                    df.remove_startup(name, self.env)
        self.assertFalse(self.user.exists())

    def test_entry_that_is_gone(self):
        with self.assertRaises(df.StartupError) as caught:
            df.set_startup_enabled("gone.desktop", True, self.env)
        self.assertIn("no longer exists", str(caught.exception))

    def test_unwritable_directory_is_reported(self):
        self.put("etc/xdg/autostart/a.desktop", desktop_file("A", "a"))
        self.put("home/.config/autostart", "a file where the directory should be")
        with self.assertRaises(df.StartupError) as caught:
            df.set_startup_enabled("a.desktop", False, self.env)
        self.assertIn("could not be written", str(caught.exception))

    def test_add_from_an_installed_application(self):
        app = self.put("usr/share/applications/org.kde.kate.desktop", desktop_file("Kate", "kate %U", Icon="kate"))
        self.assertEqual(df.add_startup(str(app), self.env), "org.kde.kate.desktop")
        entry = self.startup()["org.kde.kate.desktop"]
        self.assertEqual((entry.name, entry.enabled, entry.source, entry.publisher), ("Kate", True, "User", "KDE"))
        with self.assertRaises(df.StartupError) as caught:
            df.add_startup(str(app), self.env)
        self.assertIn("already starts", str(caught.exception))
        df.set_startup_enabled("org.kde.kate.desktop", False, self.env)
        df.add_startup(str(app), self.env)                                 # adding a disabled one enables it
        self.assertTrue(self.startup()["org.kde.kate.desktop"].enabled)

    def test_add_refuses_what_is_not_an_application(self):
        link = self.put("usr/share/applications/link.desktop", "[Desktop Entry]\nType=Link\nName=L\n")
        odd = self.put("usr/share/applications/odd", desktop_file("Odd", "odd"))
        for path in (link, odd, self.root / "missing.desktop"):
            with self.assertRaises(df.StartupError):
                df.add_startup(str(path), self.env)

    def test_add_an_application_whose_file_says_hidden(self):
        app = self.put("opt/h.desktop", desktop_file("H", "h", Hidden="true"))
        df.add_startup(str(app), self.env)
        self.assertTrue(self.startup()["h.desktop"].enabled)

    def test_remove(self):
        self.put("etc/xdg/autostart/sys.desktop", desktop_file("Sys", "sys"))
        self.put("home/.config/autostart/mine.desktop", desktop_file("Mine", "mine"))
        df.remove_startup("mine.desktop", self.env)
        self.assertEqual(sorted(self.startup()), ["sys.desktop"])
        with self.assertRaises(df.StartupError):
            df.remove_startup("mine.desktop", self.env)
        with self.assertRaises(df.StartupError):
            df.remove_startup("sys.desktop", self.env)                     # only the user's own file can go
        self.assertTrue((self.system / "sys.desktop").exists())


class WithKeysTest(unittest.TestCase):
    def test_change_add_and_drop_duplicates(self):
        text = "[Desktop Entry]\nName=A\nHidden=false\nExec=a\nHidden=true\n\n[Other]\nHidden=keep\n"
        out = df.with_keys(text, {"Hidden": "true"})
        self.assertEqual(out, "[Desktop Entry]\nName=A\nHidden=true\nExec=a\n\n[Other]\nHidden=keep\n")
        out = df.with_keys("[Desktop Entry]\nName=A\n\n# tail\n[Other]\nX=1", {"Hidden": "true", "Y": "2"})
        self.assertEqual(out, "[Desktop Entry]\nName=A\nHidden=true\nY=2\n\n# tail\n[Other]\nX=1\n")

    def test_file_without_the_group_or_empty(self):
        self.assertEqual(df.with_keys("", {"Hidden": "true"}), "[Desktop Entry]\nHidden=true\n")
        self.assertEqual(df.with_keys("[X]\nA=1\n", {"Hidden": "true"}), "[Desktop Entry]\nHidden=true\n[X]\nA=1\n")
        self.assertEqual(df.with_keys("[Desktop Entry]", {"Hidden": "true"}), "[Desktop Entry]\nHidden=true\n")

    def test_result_parses_back(self):
        text = "[Desktop Entry]\nName[de]=Ä\nExec=x\n"
        entry = df.DesktopEntry(df.with_keys(text, {"Hidden": "true"}))
        self.assertTrue(entry.boolean("Hidden"))
        self.assertEqual(entry.get("Name", ["de"]), "Ä")


if __name__ == "__main__":
    unittest.main()
