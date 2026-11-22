"""Date & time, region, default applications, start-up applications and recent files.

The parsers and file texts (timedate, defaultapps, userfiles) are tested
without Qt; the sections with a recording runner and a temporary home.

Run: QT_QPA_PLATFORM=offscreen python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402

from boswas_control_center import commands, defaultapps, timedate, userfiles  # noqa: E402

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    PYSIDE = True
except ImportError:
    PYSIDE = False

E = commands.EXECUTABLES
SHOW = ("Timezone=Europe/Berlin\nLocalRTC=no\nCanNTP=yes\nNTP=yes\nNTPSynchronized=yes\n"
        "TimeUSec=Mon 2026-11-16 09:30:00 CET\nRTCTimeUSec=Mon 2026-11-16 08:30:00 UTC\n")
STATUS = ("System Locale: LANG=en_GB.UTF-8\n               LC_TIME=de_DE.UTF-8\n               LC_NUMERIC=de_DE.UTF-8\n"
          "               LC_MONETARY=de_DE.UTF-8\n               LC_MEASUREMENT=de_DE.UTF-8\n"
          "               LC_PAPER=de_DE.UTF-8\n    VC Keymap: us\n   X11 Layout: us\n    X11 Model: pc105\n")
LOCALES = "C\nC.utf8\nPOSIX\nde_DE.utf8\nen_GB.utf8\nen_US.utf8\nsr_RS.utf8@latin\nde_DE\nde_DE.iso88591\nen_US.ISO-8859-1\n"
DESKTOP = "[Desktop Entry]\nType=Application\n"


class TimeTest(unittest.TestCase):
    def test_timedatectl(self):
        state = timedate.parse_timedatectl(SHOW)
        self.assertEqual(state, timedate.TimeState("Europe/Berlin", True, True, True))
        self.assertEqual(timedate.parse_timedatectl("Timezone=UTC\nNTP=no\nCanNTP=no\n"),
                         timedate.TimeState("UTC", False, False, False))
        for text in (None, 5, "", "garbage\n\n=x\nTimezone\n", "Timezone=../../etc/shadow\nNTP=maybe\n",
                     "Timezone=Europe/Berlin; reboot\n", "Timezone=" + "x" * 500 + "\n"):
            self.assertEqual(timedate.parse_timedatectl(text), timedate.TimeState(), repr(text)[:30])

    def test_time_zones(self):
        zones = timedate.timezones(["Europe/Berlin", "America/New_York", "America/Argentina/Buenos_Aires", "UTC",
                                    "Etc/UTC", "Etc/GMT+5", "posix/Europe/Berlin", "right/UTC", "localtime", "CET",
                                    "US/Eastern", "Factory", "Europe/../x", 7, "Asia/Kolkata"])
        self.assertEqual(zones, ["UTC", "America/Argentina/Buenos_Aires", "America/New_York", "Asia/Kolkata",
                                 "Europe/Berlin"])
        real = timedate.timezones()
        self.assertIn("Europe/Berlin", real)
        self.assertEqual(real[0], "UTC")
        for zone in real:
            self.assertEqual(commands.timedatectl_argv("set-timezone", zone)[-1], zone)   # every one can be set
        self.assertEqual(timedate.search_timezones("new york", zones), ["America/New_York"])
        self.assertEqual(timedate.search_timezones("AMERICA", zones), ["America/Argentina/Buenos_Aires",
                                                                      "America/New_York"])
        self.assertEqual(timedate.search_timezones("buenos argentina", zones), ["America/Argentina/Buenos_Aires"])
        self.assertEqual(timedate.search_timezones("", zones), zones)
        self.assertEqual(timedate.search_timezones("atlantis", zones), [])
        self.assertEqual(timedate.zone_words("America/Argentina/Buenos_Aires"), "Buenos Aires, Argentina (America)")
        self.assertEqual(timedate.zone_words("UTC"), "UTC")
        winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(timedate.utc_offset("Europe/Berlin", winter), "UTC+01:00")
        self.assertEqual(timedate.utc_offset("Europe/Berlin", summer), "UTC+02:00")
        self.assertEqual(timedate.utc_offset("Asia/Kolkata", winter), "UTC+05:30")
        self.assertEqual(timedate.utc_offset("America/New_York", winter), "UTC-05:00")
        self.assertEqual(timedate.utc_offset("No/Such_Zone", winter), "")
        self.assertEqual(timedate.zone_label("Asia/Kolkata", winter), "Kolkata (Asia) · UTC+05:30")

    def test_manual_time(self):
        self.assertTrue(timedate.valid_manual_time("2026-11-16 09:30:00"))
        for bad in ("2026-02-30 09:30:00", "2026-11-16", "1999-12-31 23:59:59", "2100-01-01 00:00:00",
                    "2026-11-16 25:00:00", "2026-11-16 09:30:00 ", "now", None, "2026-1-1 1:1:1"):
            self.assertFalse(timedate.valid_manual_time(bad), bad)

    def test_locales(self):
        self.assertEqual(timedate.parse_locale_list(LOCALES),
                         ["de_DE.UTF-8", "en_GB.UTF-8", "en_US.UTF-8", "sr_RS.UTF-8@latin", "C.UTF-8"])
        for text in (None, "", "../../x.utf8\n$(reboot).utf8\nde_DE.utf8; x\n" + "x" * 5000):
            self.assertEqual(timedate.parse_locale_list(text), [])
        values = timedate.parse_localectl(STATUS)
        self.assertEqual(values["LANG"], "en_GB.UTF-8")
        self.assertEqual(values["LC_PAPER"], "de_DE.UTF-8")
        self.assertNotIn("VC Keymap", values)
        self.assertEqual(timedate.locale_state(values), timedate.LocaleState("en_GB.UTF-8", "de_DE.UTF-8", False))
        self.assertEqual(timedate.locale_state({"LANG": "de_DE.UTF-8"}),
                         timedate.LocaleState("de_DE.UTF-8", "de_DE.UTF-8", False))
        self.assertTrue(timedate.locale_state({"LANG": "en_GB.UTF-8", "LC_TIME": "de_DE.UTF-8"}).mixed)
        self.assertTrue(timedate.locale_state(dict(values, LC_PAPER="en_US.UTF-8")).mixed)
        self.assertEqual(timedate.parse_localectl("System Locale: n/a\n    VC Keymap: n/a\n"), {})
        self.assertEqual(timedate.parse_localectl("System Locale: LANG=$(x)\n LC_TIME=../x\n LD_PRELOAD=a\n"), {})
        self.assertEqual(timedate.parse_localectl(None), {})
        self.assertEqual(timedate.set_locale_words("de_DE.UTF-8", "de_DE.UTF-8"), ("LANG=de_DE.UTF-8",))
        words = timedate.set_locale_words("en_GB.UTF-8", "de_DE.UTF-8")
        self.assertEqual(words, ("LANG=en_GB.UTF-8", "LC_TIME=de_DE.UTF-8", "LC_NUMERIC=de_DE.UTF-8",
                                 "LC_MONETARY=de_DE.UTF-8", "LC_MEASUREMENT=de_DE.UTF-8", "LC_PAPER=de_DE.UTF-8"))
        self.assertEqual(commands.localectl_argv("set-locale", *words)[2:], list(words))   # accepted as built
        with self.assertRaises(ValueError):
            timedate.set_locale_words("de_DE.UTF-8; reboot", "de_DE.UTF-8")

    def test_locale_names(self):
        definition = ('LC_IDENTIFICATION\ntitle      "German locale for Germany"\nlanguage   "German"\n'
                      'territory  "Germany"\nEND LC_IDENTIFICATION\n')
        self.assertEqual(timedate.identification(definition), ("German", "Germany"))
        self.assertEqual(timedate.identification('language "<U0044>eutsch"\nterritory "<UFFFFFFFF>X"\n'),
                         ("Deutsch", "X"))
        self.assertEqual(timedate.identification(None), ("", ""))
        self.assertEqual(timedate.locale_label("de_DE.UTF-8", ("German", "Germany")), "German (Germany)")
        self.assertEqual(timedate.locale_label("sr_RS.UTF-8@latin", ("Serbian", "Serbia")), "Serbian (Serbia, latin)")
        self.assertEqual(timedate.locale_label("xx_YY.UTF-8"), "xx_YY")
        self.assertEqual(timedate.locale_label("C.UTF-8"), "No regional format (C)")
        with tempfile.TemporaryDirectory() as root:
            fx.write(Path(root) / "usr/share/i18n/locales/de_DE", definition)
            names = timedate.read_locale_names(["de_DE.UTF-8", "en_GB.UTF-8", "../../etc/passwd", "C.UTF-8"], root)
        self.assertEqual(names, {"de_DE.UTF-8": ("German", "Germany")})


MIMEAPPS = ("# written by hand\n[Added Associations]\ntext/plain=org.example.Notes.desktop;other.desktop;\n\n"
            "[Default Applications]\ntext/html=old-browser.desktop\n# keep me\nimage/png=viewer.desktop\n"
            "x-scheme-handler/http=old-browser.desktop\n\n[Removed Associations]\ntext/plain=bad.desktop;\n"
            "[X-Vendor Group]\nSomething=else\n")


class DefaultAppsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.apps_dir = self.base / "usr/share/applications"
        self.env = {"HOME": str(self.home), "XDG_DATA_DIRS": str(self.base / "usr/share"),
                    "XDG_CONFIG_DIRS": str(self.base / "etc/xdg"), "PATH": "/usr/bin:/bin", "LANG": "en_GB.UTF-8"}
        fx.write(self.apps_dir / "firefox.desktop", DESKTOP + "Name=Firefox\nExec=firefox %u\nIcon=firefox\n"
                 "MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;application/xhtml+xml;\n")
        fx.write(self.apps_dir / "org.example.Web.desktop", DESKTOP + "Name=Web\nExec=web %u\n"
                 "MimeType=x-scheme-handler/https;x-scheme-handler/http;\n")
        fx.write(self.apps_dir / "com.boswas.TextEditor.desktop", DESKTOP + "Name=Text Editor\nExec=boswas-text %F\n"
                 "MimeType=text/plain;text/x-python;\n")
        fx.write(self.apps_dir / "com.boswas.Photos.desktop", DESKTOP + "Name=Photos\nExec=boswas-photos %F\n"
                 "MimeType=image/jpeg;image/png;image/gif;\n")
        fx.write(self.apps_dir / "hidden.desktop", DESKTOP + "Name=Hidden\nExec=x\nNoDisplay=true\n"
                 "MimeType=text/plain;\n")
        fx.write(self.apps_dir / "other-desktop.desktop", DESKTOP + "Name=Other\nExec=x\nOnlyShowIn=GNOME;\n"
                 "MimeType=text/plain;\n")
        fx.write(self.apps_dir / "evil.desktop", DESKTOP + "Name=Evil\nExec=x\nMimeType=text/plain\\n[x];../a;"
                 + "a/" + "b" * 500 + ";\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_installed_applications_and_candidates(self):
        apps = defaultapps.installed_apps(self.env)
        self.assertEqual([a.name for a in apps], ["Evil", "Firefox", "Photos", "Text Editor", "Web"])
        by = {a.desktop_id: a for a in apps}
        self.assertEqual(by["evil"].types, frozenset())                  # nothing that is not a MIME type
        browser = defaultapps.KIND_BY_KEY["browser"]
        self.assertEqual([a.name for a in defaultapps.candidates(browser, apps)], ["Firefox", "Web"])
        self.assertEqual([a.name for a in defaultapps.candidates(defaultapps.KIND_BY_KEY["text"], apps)],
                         ["Text Editor"])
        self.assertEqual(defaultapps.candidates(defaultapps.KIND_BY_KEY["pdf"], apps), [])
        self.assertEqual([k.key for k in defaultapps.KINDS],
                         ["browser", "mail", "files", "text", "images", "video", "music", "pdf"])

    def test_current_default_follows_the_lookup_order(self):
        apps = defaultapps.installed_apps(self.env)
        browser, text = defaultapps.KIND_BY_KEY["browser"], defaultapps.KIND_BY_KEY["text"]
        self.assertIsNone(defaultapps.current_default(browser, apps, defaultapps.read_defaults(self.env)))
        fx.write(self.apps_dir / "mimeapps.list", "[Default Applications]\nx-scheme-handler/https=firefox.desktop\n"
                 "text/plain=gone.desktop;com.boswas.TextEditor.desktop;\n")
        layers = defaultapps.read_defaults(self.env)
        self.assertEqual(defaultapps.current_default(browser, apps, layers), "firefox")
        self.assertEqual(defaultapps.current_default(text, apps, layers), "com.boswas.TextEditor")   # the first installed
        fx.write(self.base / "etc/xdg/mimeapps.list", "[Default Applications]\n"
                 "x-scheme-handler/https=org.example.Web.desktop\n")
        self.assertEqual(defaultapps.current_default(browser, apps, defaultapps.read_defaults(self.env)),
                         "org.example.Web")                              # /etc/xdg before the data directories
        fx.write(self.home / ".config/mimeapps.list", "[Default Applications]\n"
                 "x-scheme-handler/https=firefox.desktop\n")
        self.assertEqual(defaultapps.current_default(browser, apps, defaultapps.read_defaults(self.env)), "firefox")
        fx.write(self.home / ".config/boswas-mimeapps.list", "[Default Applications]\n"
                 "x-scheme-handler/https=org.example.Web.desktop\n")
        self.assertEqual(defaultapps.current_default(browser, apps, defaultapps.read_defaults(self.env)),
                         "org.example.Web")                              # the desktop's own list wins

    def test_with_default_keeps_the_rest_of_the_file(self):
        apps = {a.desktop_id: a for a in defaultapps.installed_apps(self.env)}
        browser = defaultapps.KIND_BY_KEY["browser"]
        text = defaultapps.with_default(MIMEAPPS, browser, apps["firefox"])
        self.assertEqual(text, (
            "# written by hand\n[Added Associations]\ntext/plain=org.example.Notes.desktop;other.desktop;\n\n"
            "[Default Applications]\ntext/html=firefox.desktop\n# keep me\nimage/png=viewer.desktop\n"
            "x-scheme-handler/http=firefox.desktop\nx-scheme-handler/https=firefox.desktop\n"
            "application/xhtml+xml=firefox.desktop\n\n[Removed Associations]\ntext/plain=bad.desktop;\n"
            "[X-Vendor Group]\nSomething=else\n"))
        self.assertEqual(defaultapps.with_default(text, browser, apps["firefox"]), text)       # idempotent
        # An application that declares only some of the kind's types gets only those.
        web = defaultapps.with_default(text, browser, apps["org.example.Web"])
        defaults = defaultapps.parse_defaults(web)
        self.assertEqual(defaults["x-scheme-handler/https"], ["org.example.Web"])
        self.assertEqual(defaults["x-scheme-handler/http"], ["org.example.Web"])
        self.assertEqual(defaults["text/html"], ["firefox"])              # not declared by Web: left as it was
        self.assertEqual(defaults["image/png"], ["viewer"])
        self.assertIn("# keep me", web)
        self.assertIn("[X-Vendor Group]\nSomething=else\n", web)

    def test_with_default_on_empty_and_odd_files(self):
        apps = {a.desktop_id: a for a in defaultapps.installed_apps(self.env)}
        text_kind = defaultapps.KIND_BY_KEY["text"]
        editor = apps["com.boswas.TextEditor"]
        self.assertEqual(defaultapps.with_default("", text_kind, editor),
                         "[Default Applications]\ntext/plain=com.boswas.TextEditor.desktop\n")
        self.assertEqual(defaultapps.with_default("[Added Associations]\na/b=c.desktop;", text_kind, editor),
                         "[Added Associations]\na/b=c.desktop;\n\n[Default Applications]\n"
                         "text/plain=com.boswas.TextEditor.desktop\n")
        self.assertEqual(defaultapps.with_default("[Default Applications]\n\n\n", text_kind, editor),
                         "[Default Applications]\ntext/plain=com.boswas.TextEditor.desktop\n\n\n")
        twice = "[Default Applications]\ntext/plain=a.desktop\n[Default Applications]\ntext/plain=b.desktop\n"
        self.assertEqual(defaultapps.with_default(twice, text_kind, editor),
                         "[Default Applications]\ntext/plain=com.boswas.TextEditor.desktop\n"
                         "[Default Applications]\ntext/plain=b.desktop\n")
        rubbish = "\x00\x01 not a key file \n=\n[\n]]\ntext/plain\n"
        result = defaultapps.with_default(rubbish, text_kind, editor)
        self.assertTrue(result.startswith(rubbish))                      # nothing of the old file is lost
        self.assertEqual(defaultapps.parse_defaults(result)["text/plain"], ["com.boswas.TextEditor"])
        with self.assertRaises(ValueError):
            defaultapps.with_default("", defaultapps.KIND_BY_KEY["browser"], editor)       # cannot open links
        with self.assertRaises(ValueError):
            defaultapps.with_default("", text_kind, defaultapps.App("--evil", "x", "", frozenset({"text/plain"})))
        self.assertEqual(defaultapps.parse_defaults(None), {})
        self.assertEqual(defaultapps.parse_defaults("[Default Applications]\n=x.desktop\na/b=\na/c=notdesktop\n"), {})


class StartupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.system = self.base / "etc/xdg/autostart"
        self.user = self.home / ".config/autostart"
        self.env = {"HOME": str(self.home), "XDG_CONFIG_DIRS": str(self.base / "etc/xdg"),
                    "XDG_DATA_DIRS": str(self.base / "usr/share"), "PATH": "/usr/bin:/bin"}
        fx.write(self.system / "agent.desktop", DESKTOP + "Name=Sync Agent\nComment=Keeps files in sync\n"
                 "Exec=sync-agent\nIcon=folder-remote\n")
        fx.write(self.system / "gnome-thing.desktop", DESKTOP + "Name=GNOME Thing\nExec=x\nOnlyShowIn=GNOME;\n")
        fx.write(self.system / "not-here.desktop", DESKTOP + "Name=Not Here\nExec=x\nNotShowIn=Boswas;\n")
        fx.write(self.system / "boswas-only.desktop", DESKTOP + "Name=Boswas Thing\nExec=x\nOnlyShowIn=Boswas;\n")
        fx.write(self.system / "off-by-default.desktop", DESKTOP + "Name=Off\nExec=x\nHidden=true\n")
        fx.write(self.system / "no-exec.desktop", DESKTOP + "Name=No Exec\n")
        fx.write(self.system / "notes.txt", "not an entry")
        fx.write(self.user / "mine.desktop", DESKTOP + "Name=My Tool\nExec=my-tool --tray\n")
        fx.write(self.user / "odd name!.desktop", DESKTOP + "Name=Odd\nExec=odd\n")

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self):
        return {e.ident: e for e in defaultapps.startup_entries(self.env)}

    def apply(self, ident: str, enabled: bool) -> None:
        entry = self.entries()[ident]
        user_text, system_text = defaultapps.read_texts(self.env, ident)
        text = defaultapps.switched(entry, enabled, user_text, system_text)
        if text is None:
            userfiles.remove_config(self.env, defaultapps.autostart_name(ident))
        else:
            userfiles.write_config(self.env, defaultapps.autostart_name(ident), text.encode())

    def test_entries_of_this_desktop(self):
        entries = self.entries()
        self.assertEqual(sorted(entries), ["agent", "boswas-only", "mine", "odd name!", "off-by-default"])
        agent = entries["agent"]
        self.assertEqual((agent.name, agent.comment, agent.enabled, agent.user, agent.system, agent.removable),
                         ("Sync Agent", "Keeps files in sync", True, False, True, False))
        self.assertTrue(entries["mine"].removable)
        self.assertFalse(entries["off-by-default"].enabled)
        self.assertFalse(entries["odd name!"].changeable)                # not a file name of ours
        self.assertEqual(defaultapps.startup_entries({"HOME": "relative"}), [])

    def test_switch_a_system_entry_off_and_on(self):
        self.apply("agent", False)
        override = (self.user / "agent.desktop").read_text()
        self.assertIn("Hidden=true", override)
        self.assertIn("X-Boswas-Autostart-Override=true", override)
        self.assertIn("Exec=sync-agent", override)                       # a whole entry, valid on its own
        self.assertEqual((self.system / "agent.desktop").read_text().count("Hidden"), 0)   # never touched
        entry = self.entries()["agent"]
        self.assertEqual((entry.enabled, entry.user, entry.system, entry.removable), (False, True, True, False))
        self.apply("agent", True)
        self.assertFalse((self.user / "agent.desktop").exists())         # our override is simply removed
        self.assertTrue(self.entries()["agent"].enabled)
        self.apply("agent", True)                                        # already on: nothing to do
        self.assertFalse((self.user / "agent.desktop").exists())

    def test_a_user_copy_of_a_system_entry_is_kept(self):
        fx.write(self.user / "agent.desktop", DESKTOP + "Name=Sync Agent (mine)\nExec=sync-agent --quiet\n"
                 "Hidden=true\n# my note\n")
        self.assertFalse(self.entries()["agent"].enabled)
        self.apply("agent", True)
        text = (self.user / "agent.desktop").read_text()
        self.assertIn("Hidden=false", text)                              # the user's own file is not deleted
        self.assertIn("Exec=sync-agent --quiet", text)
        self.assertIn("# my note", text)
        self.assertTrue(self.entries()["agent"].enabled)
        self.assertEqual(self.entries()["agent"].name, "Sync Agent (mine)")

    def test_switch_a_user_entry(self):
        self.apply("mine", False)
        self.assertEqual((self.user / "mine.desktop").read_text(),
                         DESKTOP + "Name=My Tool\nExec=my-tool --tray\nHidden=true\n")
        self.assertFalse(self.entries()["mine"].enabled)
        self.apply("mine", True)
        self.assertEqual((self.user / "mine.desktop").read_text(), DESKTOP + "Name=My Tool\nExec=my-tool --tray\n")

    def test_switch_on_a_system_entry_that_is_off_for_everybody(self):
        self.assertFalse(self.entries()["off-by-default"].enabled)
        self.apply("off-by-default", True)
        override = (self.user / "off-by-default.desktop").read_text()
        self.assertIn("Hidden=false", override)
        self.assertIn("X-Boswas-Autostart-Override=true", override)
        self.assertTrue(self.entries()["off-by-default"].enabled)
        self.apply("off-by-default", False)                              # back to the system's own state
        self.assertFalse((self.user / "off-by-default.desktop").exists())
        self.assertFalse(self.entries()["off-by-default"].enabled)
        with self.assertRaises(ValueError):
            defaultapps.switched(self.entries()["mine"], False, None, None)

    def test_set_key(self):
        text = "[Desktop Entry]\nName=A\nHidden=false\nHidden=true\n\n[Desktop Action x]\nHidden=keep\n"
        self.assertEqual(defaultapps._set_key(text, "Hidden", "true"),
                         "[Desktop Entry]\nName=A\nHidden=true\n\n[Desktop Action x]\nHidden=keep\n")
        self.assertEqual(defaultapps._set_key(text, "Hidden", None),
                         "[Desktop Entry]\nName=A\n\n[Desktop Action x]\nHidden=keep\n")
        self.assertEqual(defaultapps._set_key("Name=A\n", "Hidden", "true"),
                         "[Desktop Entry]\nType=Application\nHidden=true\nName=A\n")
        self.assertEqual(defaultapps._set_key("[Desktop Entry]\nName=A\n\n\n[Other]\n", "X", "1"),
                         "[Desktop Entry]\nName=A\nX=1\n\n\n[Other]\n")

    def test_added_and_names(self):
        text = defaultapps.added(DESKTOP + "Name=Photos\nExec=boswas-photos %F\nHidden=true\n")
        self.assertNotIn("Hidden", text)
        self.assertIn("X-Boswas-Autostart-Added=true", text)
        with self.assertRaises(ValueError):
            defaultapps.added("[Desktop Entry]\nType=Link\nURL=x\n")
        with self.assertRaises(ValueError):
            defaultapps.added(DESKTOP + "X=" + "y" * defaultapps.MAX_FILE)
        self.assertEqual(defaultapps.autostart_name("org.example.App"), "autostart/org.example.App.desktop")
        for bad in ("", ".hidden", "../x", "a/b", "a b", "x" * 200, None):
            with self.assertRaises(ValueError, msg=bad):
                defaultapps.autostart_name(bad)
            with self.assertRaises(ValueError, msg=bad):
                defaultapps.read_texts(self.env, bad)


class RecentFilesTest(unittest.TestCase):
    def test_count_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp}
            self.assertEqual(userfiles.recent_files_count(env), 0)
            self.assertEqual(userfiles.clear_recent_files(env), 0)       # no list: nothing to do, nothing created
            self.assertFalse((Path(tmp) / ".local").exists())
            path = fx.write(Path(tmp) / ".local/share/recently-used.xbel", '<?xml version="1.0"?>\n<xbel>\n'
                            '<bookmark href="file:///home/a/one.txt"><title>x</title></bookmark>\n'
                            '<bookmark href="file:///home/a/two.pdf"/>\n</xbel>\n')
            other = fx.write(Path(tmp) / ".local/share/keep.txt", "keep")
            self.assertEqual(userfiles.recent_files_count(env), 2)
            self.assertEqual(userfiles.clear_recent_files(env), 2)
            self.assertEqual(path.read_bytes(), userfiles.EMPTY_RECENT)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(userfiles.recent_files_count(env), 0)
            self.assertEqual(other.read_text(), "keep")
            import xml.dom.minidom
            xml.dom.minidom.parseString(userfiles.EMPTY_RECENT)          # still a well-formed list
            victim = fx.write(Path(tmp) / "victim", "<bookmark <bookmark ")
            os.unlink(path)
            os.symlink(victim, path)
            self.assertEqual(userfiles.recent_files_count(env), 0)
            with self.assertRaises(userfiles.UserFileError):
                userfiles.clear_recent_files(env)
            self.assertEqual(victim.read_text(), "<bookmark <bookmark ")
            xdg = dict(env, XDG_DATA_HOME=str(Path(tmp) / "data"))
            fx.write(Path(tmp) / "data/recently-used.xbel", "<bookmark ")
            self.assertEqual(userfiles.clear_recent_files(xdg), 1)
            self.assertEqual(userfiles.data_dir(dict(env, XDG_DATA_HOME="relative")), str(Path(tmp) / ".local/share"))


class FakeRunner:
    """Stands in for commands.CommandRunner: records, answers from a table, and can be held back."""

    def __init__(self):
        self.ran: list[list[str]] = []
        self.timeouts: list[float] = []
        self.show = SHOW
        self.status = STATUS
        self.locales = LOCALES
        self.failures: dict[str, commands.Completed] = {}
        self.lock = threading.Lock()

    def available(self, path: str) -> bool:
        return True

    def run(self, command: list[str], timeout: float = 30.0) -> commands.Completed:
        commands._check(command)
        with self.lock:
            self.ran.append(list(command))
            self.timeouts.append(timeout)
        words = command[1:]
        if words and words[0] in self.failures:
            return self.failures[words[0]]
        if command[0] == E["timedatectl"]:
            if words == ["show"]:
                return commands.Completed(0, self.show, "")
            if words[0] == "set-ntp":
                self.show = self.show.replace("\nNTP=yes" if words[1] == "false" else "\nNTP=no",
                                              "\nNTP=no" if words[1] == "false" else "\nNTP=yes")
            elif words[0] == "set-timezone":
                self.show = "\n".join(f"Timezone={words[1]}" if line.startswith("Timezone=") else line
                                      for line in self.show.splitlines()) + "\n"
            return commands.Completed(0, "", "")
        if command[0] == E["localectl"]:
            if words == ["status"]:
                return commands.Completed(0, self.status, "")
            self.status = "System Locale: " + "\n               ".join(words[1:]) + "\n    VC Keymap: us\n"
            return commands.Completed(0, "", "")
        if command[0] == E["locale"]:
            return commands.Completed(0, self.locales, "")
        return commands.Completed(1, "", "unexpected")

    def changes(self) -> list[list[str]]:
        return [c[1:] for c in self.ran if c[1] not in ("show", "status", "-a")]


if PYSIDE:
    import boswas_fluent as fluent
    from boswas_fluent import testing
    from boswas_control_center.ui import settings
    from boswas_control_center.ui.settings import system

    class SectionCase(testing.FluentTestCase):
        def setUp(self):
            super().setUp()
            self.host = QWidget()
            QVBoxLayout(self.host)
            self.runner = FakeRunner()

        def put(self, section):
            self.host.layout().addWidget(section)
            self.show(self.host, 900, 700)
            section.activate()
            self.settle(section)
            return section

        @staticmethod
        def settle(section) -> None:
            assert testing.wait_until(lambda: not section.loading and not section.busy, 10)
            testing.process(20)


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class DateTimeSectionTest(SectionCase if PYSIDE else unittest.TestCase):
    def section(self):
        return self.put(system.DateTimeSection(self.host, runner=self.runner,
                                               zones=["UTC", "America/New_York", "Asia/Kolkata", "Europe/Berlin",
                                                      "Europe/Paris"]))

    def test_shows_the_state_and_ticks(self):
        section = self.section()
        self.assertTrue(section.auto.isChecked())
        self.assertIn("synchronised", section.auto_card.description())
        self.assertTrue(section.zone_card.description().startswith("Berlin (Europe) · UTC+0"))
        self.assertFalse(section.manual_button.isEnabled())              # only while automatic time is off
        self.assertIn("Turn off automatic time", section.manual_card.description())
        self.assertRegex(section.clock.title(), r"\d{1,2}[:.]\d{2}[:.]\d{2}")
        self.assertTrue(section.clock.description())
        self.assertTrue(section._tick.isActive())
        shown = section.clock.title()
        self.assertTrue(testing.wait_until(lambda: section.clock.title() != shown, 3))     # the clock runs
        self.assertEqual(self.runner.changes(), [])                      # showing changes nothing
        section.deactivate()
        self.assertFalse(section._tick.isActive())
        self.assertFalse(section._poll.isActive())

    def test_automatic_time(self):
        section = self.section()
        testing.click(section.auto)
        self.settle(section)
        self.assertEqual(self.runner.changes(), [["set-ntp", "false"]])
        self.assertGreaterEqual(self.runner.timeouts[self.runner.ran.index([E["timedatectl"], "set-ntp", "false"])],
                                120)                                     # time to answer polkit's question
        self.assertFalse(section.auto.isChecked())
        self.assertTrue(section.manual_button.isEnabled())
        self.assertFalse(section.error.isVisibleTo(self.host))
        # Refused by polkit: the switch goes back and the reason is told.
        self.runner.failures["set-ntp"] = commands.Completed(1, "", "Failed to set ntp: Access denied\n")
        testing.click(section.auto)
        self.settle(section)
        self.assertFalse(section.auto.isChecked())
        self.assertEqual(section.error.message(), "Turning automatic time on was not allowed: administrator "
                                                  "authorization was not given.")

    def test_time_zone_dialog(self):
        section = self.section()
        testing.click(section.zone_button)
        dialog = section.dialog
        self.assertTrue(dialog.is_open())
        self.assertEqual(dialog.chosen(), "Europe/Berlin")               # the current one is selected
        self.assertEqual(dialog.list.count(), 5)
        testing.type_text(dialog.search, "new y")
        self.assertEqual(dialog.list.count(), 1)
        self.assertEqual(dialog.chosen(), "America/New_York")
        testing.type_text(dialog.search, "zzz")
        self.assertEqual(dialog.list.count(), 0)
        self.assertFalse(dialog.primary_button.isEnabled())
        dialog.search.clear()
        testing.type_text(dialog.search, "kolk")
        testing.click(dialog.primary_button)
        self.settle(section)
        self.assertEqual(self.runner.changes(), [["set-timezone", "Asia/Kolkata"]])
        self.assertEqual(section.zone_card.description(), "Kolkata (Asia) · UTC+05:30")
        self.assertIsNone(section.dialog)
        testing.click(section.zone_button)                               # Cancel changes nothing
        testing.click(section.dialog.close_button)
        self.settle(section)
        self.assertEqual(len(self.runner.changes()), 1)
        section.set_zone("../../etc/passwd")
        self.assertEqual(len(self.runner.changes()), 1)
        self.assertIn("not a time zone", section.error.message())

    def test_set_the_time_manually(self):
        self.runner.show = self.runner.show.replace("\nNTP=yes", "\nNTP=no")
        section = self.section()
        self.assertTrue(section.manual_button.isEnabled())
        testing.click(section.manual_button)
        dialog = section.dialog
        from PySide6.QtCore import QDate, QDateTime, QTime
        dialog.edit.setDateTime(QDateTime(QDate(2027, 3, 4), QTime(5, 6, 7)))
        testing.click(dialog.primary_button)
        self.settle(section)
        self.assertEqual(self.runner.changes(), [["set-time", "2027-03-04 05:06:07"]])
        self.runner.failures["set-time"] = commands.Completed(1, "", "Failed to set time: Automatic time "
                                                              "synchronization is enabled\n")
        section.set_time("2027-03-04 05:06:07")
        self.settle(section)
        self.assertIn("Setting the date and time failed: Failed to set time", section.error.message())
        section.set_time("yesterday")
        self.assertIn("not a date and time", section.error.message())
        self.assertEqual(len(self.runner.changes()), 2)

    def test_without_timedated(self):
        self.runner.failures["show"] = commands.Completed(1, "", "Failed to connect to bus: Host is down\n")
        section = self.section()
        self.assertTrue(section.notice.isVisibleTo(self.host))
        self.assertIn("not available", section.notice.message())
        self.assertFalse(section.auto.isEnabled())
        self.assertFalse(section.zone_button.isEnabled())
        self.assertRegex(section.clock.title(), r"\d")                   # the clock still shows the time

    def test_no_time_service_installed(self):
        self.runner.show = "Timezone=UTC\nCanNTP=no\nNTP=no\n"
        section = self.section()
        self.assertFalse(section.auto.isEnabled())
        self.assertIn("No time synchronisation service", section.auto_card.description())
        self.assertTrue(section.manual_button.isEnabled())


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class RegionSectionTest(SectionCase if PYSIDE else unittest.TestCase):
    def section(self):
        self.root = self.tmp / "root"
        fx.write(self.root / "usr/share/i18n/locales/de_DE", 'language "German"\nterritory "Germany"\n')
        fx.write(self.root / "usr/share/i18n/locales/en_GB", 'language "British English"\n'
                 'territory "United Kingdom"\n')
        return self.put(system.RegionSection(self.host, runner=self.runner, root=str(self.root)))

    def test_shows_generated_locales_and_the_current_ones(self):
        section = self.section()
        self.assertEqual([section.language.itemData(i) for i in range(section.language.count())],
                         ["de_DE.UTF-8", "en_GB.UTF-8", "en_US.UTF-8", "sr_RS.UTF-8@latin", "C.UTF-8"])
        self.assertEqual(section.language.currentText(), "British English (United Kingdom)")
        self.assertEqual(section.formats.currentText(), "German (Germany)")
        self.assertEqual(section.language.itemText(2), "en_US")           # no definition file: its own name
        self.assertEqual(self.runner.changes(), [])

    def test_changing_the_language_and_the_formats(self):
        section = self.section()
        section.language.setCurrentIndex(0)
        section.language.activated.emit(0)
        self.settle(section)
        self.assertEqual(self.runner.changes(), [["set-locale", "LANG=de_DE.UTF-8"]])      # same as the formats
        self.assertIn("sign out and in again", section.notice.message())
        self.assertEqual(section.notice.severity(), "success")
        self.assertEqual(section.language.currentData(), "de_DE.UTF-8")
        section.formats.setCurrentIndex(1)
        section.formats.activated.emit(1)
        self.settle(section)
        self.assertEqual(self.runner.changes()[-1],
                         ["set-locale", "LANG=de_DE.UTF-8", "LC_TIME=en_GB.UTF-8", "LC_NUMERIC=en_GB.UTF-8",
                          "LC_MONETARY=en_GB.UTF-8", "LC_MEASUREMENT=en_GB.UTF-8", "LC_PAPER=en_GB.UTF-8"])
        section.formats.activated.emit(1)                                # the same again: nothing to do
        self.settle(section)
        self.assertEqual(len(self.runner.changes()), 2)
        self.runner.failures["set-locale"] = commands.Completed(1, "", "Failed to issue method call: "
                                                                "Interactive authentication required.\n")
        section.language.setCurrentIndex(2)
        section.language.activated.emit(2)
        self.settle(section)
        self.assertIn("was not allowed", section.error.message())
        self.assertEqual(section.language.currentData(), "de_DE.UTF-8")  # back to what is in use

    def test_a_locale_in_use_that_is_not_generated_is_still_shown(self):
        self.runner.status = "System Locale: LANG=fr_FR.UTF-8\n"
        section = self.section()
        self.assertEqual(section.language.currentData(), "fr_FR.UTF-8")
        self.assertEqual(section.formats.currentData(), "fr_FR.UTF-8")

    def test_without_localed(self):
        self.runner.failures["status"] = commands.Completed(1, "", "Failed to connect to bus\n")
        section = self.section()
        self.assertIn("not available", section.notice.message())
        self.assertFalse(section.language.isEnabled())


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class ApplicationsSectionsTest(SectionCase if PYSIDE else unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.apps_dir = self.tmp / "usr/share/applications"
        self.env = {"HOME": str(self.home), "XDG_DATA_DIRS": str(self.tmp / "usr/share"),
                    "XDG_CONFIG_DIRS": str(self.tmp / "etc/xdg"), "PATH": "/usr/bin:/bin"}
        fx.write(self.apps_dir / "firefox.desktop", DESKTOP + "Name=Firefox\nExec=firefox %u\n"
                 "MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;\n")
        fx.write(self.apps_dir / "org.example.Web.desktop", DESKTOP + "Name=Web\nExec=web %u\n"
                 "MimeType=x-scheme-handler/https;x-scheme-handler/http;\n")
        fx.write(self.apps_dir / "com.boswas.TextEditor.desktop", DESKTOP + "Name=Text Editor\nExec=t %F\n"
                 "MimeType=text/plain;\n")
        fx.write(self.tmp / "etc/xdg/autostart/agent.desktop", DESKTOP + "Name=Sync Agent\nExec=sync-agent\n")
        fx.write(self.home / ".config/autostart/mine.desktop", DESKTOP + "Name=My Tool\nExec=my-tool\n")

    def test_default_applications(self):
        fx.write(self.home / ".config/mimeapps.list", MIMEAPPS)
        section = self.put(system.DefaultAppsSection(self.host, environ=self.env))
        browser, text, pdf = section.boxes["browser"], section.boxes["text"], section.boxes["pdf"]
        self.assertEqual([browser.itemText(i) for i in range(browser.count())], ["Firefox", "Web"])
        self.assertEqual(browser.currentIndex(), -1)                     # nothing set for https yet
        self.assertEqual(text.count(), 1)
        self.assertFalse(pdf.isEnabled())
        self.assertIn("No installed application declares this", section.cards["pdf"].description())
        browser.setCurrentIndex(1)
        browser.activated.emit(1)
        self.settle(section)
        written = (self.home / ".config/mimeapps.list").read_text()
        self.assertIn("x-scheme-handler/https=org.example.Web.desktop\n", written)
        self.assertIn("x-scheme-handler/http=org.example.Web.desktop\n", written)
        self.assertIn("text/html=old-browser.desktop\n", written)         # Web does not declare it
        self.assertIn("# written by hand\n[Added Associations]\n", written)
        self.assertIn("[X-Vendor Group]\nSomething=else\n", written)
        self.assertEqual(browser.currentText(), "Web")
        self.assertFalse(section.error.isVisibleTo(self.host))
        # Another program changes the file: the page follows.
        fx.write(self.home / ".config/mimeapps.list", "[Default Applications]\n"
                 "x-scheme-handler/https=firefox.desktop\n")
        self.assertTrue(testing.wait_until(lambda: browser.currentText() == "Firefox", 5))

    def test_default_applications_write_failure_snaps_back(self):
        section = self.put(system.DefaultAppsSection(self.host, environ=self.env))
        os.makedirs(self.home / ".config/mimeapps.list")                  # a folder where the file should be
        text = section.boxes["text"]
        text.setCurrentIndex(0)
        text.activated.emit(0)
        self.settle(section)
        self.assertTrue(section.error.isVisibleTo(self.host))
        self.assertIn("could not be read", section.error.message())
        os.rmdir(self.home / ".config/mimeapps.list")
        os.symlink(self.tmp / "elsewhere", self.home / ".config/mimeapps.list")           # a dangling link
        text.setCurrentIndex(0)
        text.activated.emit(0)
        self.settle(section)
        self.assertIn("Text Editor could not be made the default text editor", section.error.message())
        self.assertIn("symbolic link", section.error.message())
        self.assertFalse((self.tmp / "elsewhere").exists())
        self.assertEqual(text.currentIndex(), -1)                        # still not set

    def test_startup_applications(self):
        section = self.put(system.StartupSection(self.host, environ=self.env))
        self.assertEqual(section.list.keys(), ["mine", "agent"])          # by name: My Tool, Sync Agent
        mine, agent = section.list.row("mine"), section.list.row("agent")
        self.assertEqual(section.header.description(), "2 of 2 switched on")
        self.assertTrue(mine.switch.isChecked() and agent.switch.isChecked())
        self.assertTrue(mine.remove_button.isVisibleTo(self.host))
        self.assertFalse(agent.remove_button.isVisibleTo(self.host))     # a system-wide entry cannot be removed
        self.assertIn("all users", agent.description())
        testing.click(agent.switch)
        self.settle(section)
        self.assertIn("Hidden=true", (self.home / ".config/autostart/agent.desktop").read_text())
        self.assertIs(section.list.row("agent"), agent)
        self.assertFalse(agent.switch.isChecked())
        self.assertEqual(section.header.description(), "1 of 2 switched on")
        testing.click(agent.switch)
        self.settle(section)
        self.assertFalse((self.home / ".config/autostart/agent.desktop").exists())
        self.assertTrue(agent.switch.isChecked())
        with mock.patch.object(fluent, "ask", return_value=False):
            testing.click(mine.remove_button)
        self.assertTrue((self.home / ".config/autostart/mine.desktop").exists())
        with mock.patch.object(fluent, "ask", return_value=True) as ask:
            testing.click(mine.remove_button)
        self.settle(section)
        self.assertEqual(ask.call_args.args[1], "Remove My Tool?")
        self.assertFalse((self.home / ".config/autostart/mine.desktop").exists())
        self.assertEqual(section.list.keys(), ["agent"])

    def test_add_a_startup_application(self):
        section = self.put(system.StartupSection(self.host, environ=self.env))
        testing.click(section.add_button)
        dialog = section.dialog
        self.assertEqual([dialog.list.item(i).text() for i in range(dialog.list.count())],
                         ["Firefox", "Text Editor", "Web"])
        self.assertFalse(dialog.primary_button.isEnabled())
        testing.type_text(dialog.search, "text")
        self.assertEqual(dialog.chosen().desktop_id, "com.boswas.TextEditor")
        testing.click(dialog.primary_button)
        self.settle(section)
        added = (self.home / ".config/autostart/com.boswas.TextEditor.desktop").read_text()
        self.assertIn("Exec=t %F", added)
        self.assertIn("X-Boswas-Autostart-Added=true", added)
        self.assertEqual(section.list.keys(), ["mine", "agent", "com.boswas.TextEditor"])
        self.assertTrue(section.list.row("com.boswas.TextEditor").remove_button.isVisibleTo(self.host))
        testing.click(section.add_button)                                # what is there already is not offered again
        self.assertEqual([section.dialog.list.item(i).text() for i in range(section.dialog.list.count())],
                         ["Firefox", "Web"])
        section.dialog.reject()

    def test_startup_write_failure(self):
        section = self.put(system.StartupSection(self.host, environ=self.env))
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        os.rename(self.home / ".config/autostart", self.home / ".config/autostart.real")
        os.symlink(elsewhere, self.home / ".config/autostart")
        fx.write(self.tmp / "etc/xdg/autostart/agent.desktop", DESKTOP + "Name=Sync Agent\nExec=sync-agent\n")
        section.reload()
        self.settle(section)
        agent = section.list.row("agent")
        testing.click(agent.switch)
        self.settle(section)
        self.assertIn("could not be switched off", section.error.message())
        self.assertIn("symbolic link", section.error.message())
        self.assertTrue(agent.switch.isChecked())                        # back to the real state
        self.assertEqual(os.listdir(elsewhere), [])

    def test_recent_files(self):
        path = fx.write(self.home / ".local/share/recently-used.xbel", '<xbel><bookmark href="file:///a"/>'
                        '<bookmark href="file:///b"/><bookmark href="file:///c"/></xbel>')
        section = self.put(system.RecentFilesSection(self.host, environ=self.env))
        self.assertIn("names 3 files", section.card.description())
        testing.click(section.clear_button)
        self.settle(section)
        self.assertEqual(path.read_bytes(), userfiles.EMPTY_RECENT)
        self.assertIn("is empty", section.card.description())
        self.assertFalse(section.clear_button.isEnabled())
        self.assertEqual(section.notice.severity(), "success")
        fx.write(path, '<xbel><bookmark href="file:///new"/></xbel>')    # an application opens a file
        self.assertTrue(testing.wait_until(lambda: "names 1 file." in section.card.description(), 5))

    def test_registration(self):
        names = lambda page: [c.__name__ for c in settings.sections_for(page)        # noqa: E731
                              if c.__module__.endswith(".system")]
        self.assertEqual(names("date-time"), ["DateTimeSection"])
        self.assertEqual(names("region"), ["RegionSection"])
        self.assertEqual(names("default-apps"), ["DefaultAppsSection"])
        self.assertEqual(names("startup"), ["StartupSection"])
        self.assertEqual(names("recent-files"), ["RecentFilesSection"])
        self.assertFalse(hasattr(system, "NotificationsSection"))        # Notifications come with their own page
        self.assertEqual(settings.section_id(system.DateTimeSection), "date-time")
        self.assertEqual(settings.find("time zone")[0], system.DateTimeSection)
        self.assertEqual(settings.find("autostart")[0], system.StartupSection)
        self.assertEqual(settings.find("default browser")[0], system.DefaultAppsSection)


if __name__ == "__main__":
    unittest.main()
